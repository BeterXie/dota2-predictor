"""Supervise bounded visual watchers for exact active RayBet Dota matches."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.sqlite import connect as connect_sqlite  # noqa: E402
from live_betting.browser_contract import (  # noqa: E402
    DOTA2_GAME_ID,
    RAYBET_ORIGINS,
)
from live_betting.health import record_health  # noqa: E402
from live_betting.raybet_state import raybet_match_is_live  # noqa: E402
from live_betting.sanitize import stored_public_stream_url  # noqa: E402
from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    bind_manager_child_authority,
    database_writer_authority,
    delegated_writer_process_environment,
    managed_child_command,
    service_data_paths,
    terminate_subprocess_tree,
)
from live_betting.vision_retention import prune_vision_evidence  # noqa: E402

OUTPUT_MAX_AGE = timedelta(seconds=90)
WATCHER_STARTUP_GRACE = timedelta(seconds=90)
RETENTION_INTERVAL_SECONDS = 60 * 60
MAX_CONCURRENT_WATCHERS = 4
WATCHER_MAX_START_FAILURES = 3
WATCHER_RETRY_DELAYS = (timedelta(seconds=30), timedelta(seconds=60))
VIDEO_SOURCE_PATHS = frozenset({"/live", "/video", "/playback", "/v2/video"})

Child = tuple[subprocess.Popen, object, object]
OutputSignature = tuple[int, int]


class AuthorityCleanupError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        process: subprocess.Popen | None = None,
        authority: object | None = None,
    ) -> None:
        super().__init__(message)
        self.process = process
        self.authority = authority


@dataclass(frozen=True)
class WatcherRetryState:
    attempts: int
    failure_reason: str
    last_exit_code: int | None
    failed_at: datetime
    retry_at: datetime | None
    exhausted: bool


def record_supervisor_health(
    database: Path,
    status: str,
    *,
    active_matches: int,
    error: str | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    connection = connect_sqlite(database)
    try:
        now = datetime.now(timezone.utc)
        health_details = {"active_watchers": active_matches}
        health_details.update(details or {})
        record_health(
            connection,
            "vision_worker",
            status,
            heartbeat_at=now,
            success_at=now if status == "healthy" else None,
            error_at=now if error is not None else None,
            error=error,
            details=health_details,
        )
        connection.commit()
    finally:
        connection.close()


def _output_signature(path: Path) -> OutputSignature | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _capture_heartbeat_path(output_dir: Path, match_id: str) -> Path:
    return output_dir / f"{match_id}.heartbeat.json"


def _capture_heartbeat(path: Path, match_id: str) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        captured_at = datetime.fromisoformat(str(payload["captured_at"]))
        confidence = payload["layout_confidence"]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        payload.get("schema_version") != 1
        or str(payload.get("match_id")) != match_id
        or payload.get("capture_status")
        not in {"producing_trusted", "capturing_unrecognized"}
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
        or not isinstance(payload.get("layout_profile"), str)
    ):
        return None
    return {**payload, "captured_at_value": captured_at.astimezone(timezone.utc)}


def supervisor_health(
    desired: set[str],
    children: Mapping[str, Child],
    output_dir: Path,
    *,
    started_at: Mapping[str, datetime] | None = None,
    output_baselines: Mapping[str, OutputSignature | None] | None = None,
    capture_baselines: Mapping[str, OutputSignature | None] | None = None,
    retry_states: Mapping[str, WatcherRetryState] | None = None,
    max_concurrent: int = MAX_CONCURRENT_WATCHERS,
    now: datetime | None = None,
) -> tuple[str, dict[str, object], str | None]:
    """Describe whether desired watchers are alive and producing fresh output."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started_at = started_at or {}
    output_baselines = output_baselines or {}
    capture_baselines = capture_baselines or {}
    retry_states = retry_states or {}
    running = {
        match_id
        for match_id in desired
        if (child := children.get(match_id)) is not None and child[0].poll() is None
    }
    producing: set[str] = set()
    capturing: set[str] = set()
    unrecognized: set[str] = set()
    watcher_details: dict[str, dict[str, object]] = {}
    stale: set[str] = set()
    capture_stalled_matches: set[str] = set()
    retrying: set[str] = set()
    exhausted: set[str] = set()
    queued: set[str] = set()
    missing: set[str] = set()

    for match_id in sorted(desired):
        child = children.get(match_id)
        is_running = child is not None and child[0].poll() is None

        path = output_dir / f"{match_id}.jsonl"
        signature = _output_signature(path)
        baseline_known = match_id in output_baselines
        has_current_output = signature is not None and (
            not baseline_known or signature != output_baselines[match_id]
        )
        output_at = (
            datetime.fromtimestamp(signature[1] / 1_000_000_000, timezone.utc)
            if signature is not None
            else None
        )
        output_age = (
            max(0.0, (now - output_at).total_seconds())
            if output_at is not None
            else None
        )
        heartbeat_path = _capture_heartbeat_path(output_dir, match_id)
        heartbeat_signature = _output_signature(heartbeat_path)
        heartbeat = _capture_heartbeat(heartbeat_path, match_id)
        heartbeat_is_current = heartbeat_signature is not None and (
            match_id not in capture_baselines
            or heartbeat_signature != capture_baselines[match_id]
        )
        capture_at = (
            heartbeat.get("captured_at_value") if heartbeat is not None else None
        )
        capture_age = (
            max(0.0, (now - capture_at).total_seconds())
            if isinstance(capture_at, datetime)
            else None
        )
        fresh_capture = bool(
            is_running
            and heartbeat_is_current
            and capture_age is not None
            and capture_age <= OUTPUT_MAX_AGE.total_seconds()
        )
        legacy_producing = bool(
            heartbeat_signature is None
            and is_running
            and has_current_output
            and output_age is not None
            and output_age <= OUTPUT_MAX_AGE.total_seconds()
        )
        capture_status = heartbeat.get("capture_status") if heartbeat else None
        is_capturing = fresh_capture or legacy_producing
        is_producing = bool(
            legacy_producing
            or (fresh_capture and capture_status == "producing_trusted")
        )
        if is_capturing:
            capturing.add(match_id)
        if is_producing:
            producing.add(match_id)

        started = started_at.get(match_id)
        startup_age = (
            max(0.0, (now - started.astimezone(timezone.utc)).total_seconds())
            if started is not None
            else None
        )
        retry = retry_states.get(match_id)
        retry_details = None
        if retry is not None:
            retry_details = {
                "attempts": retry.attempts,
                "max_attempts": WATCHER_MAX_START_FAILURES,
                "failure_reason": retry.failure_reason,
                "last_exit_code": retry.last_exit_code,
                "failed_at": retry.failed_at.isoformat(),
                "retry_at": retry.retry_at.isoformat() if retry.retry_at else None,
                "exhausted": retry.exhausted,
            }
        if not is_running and retry is not None and retry.exhausted:
            state = "failed"
            reason = f"{retry.failure_reason}_retry_exhausted"
            exhausted.add(match_id)
        elif (
            not is_running
            and retry is not None
            and retry.retry_at is not None
            and now < retry.retry_at
        ):
            state = "backoff"
            reason = f"{retry.failure_reason}_retry_scheduled"
            retrying.add(match_id)
        elif not is_running and len(running) >= max_concurrent:
            state = "queued"
            reason = "watcher_capacity_limited"
            queued.add(match_id)
        elif not is_running:
            state = "desired"
            reason = "watcher_not_running"
            missing.add(match_id)
        elif is_producing:
            state = "producing"
            reason = "fresh_output"
        elif is_capturing:
            state = "capturing"
            reason = "capturing_unrecognized"
            unrecognized.add(match_id)
        elif startup_age is not None and startup_age <= WATCHER_STARTUP_GRACE.total_seconds():
            state = "running"
            reason = "awaiting_first_output"
        else:
            state = "running"
            reason = "capture_stalled" if heartbeat_signature is not None else (
                "output_stale" if has_current_output else "no_current_output"
            )
            stale.add(match_id)
            if heartbeat_signature is not None:
                capture_stalled_matches.add(match_id)
        if not is_running:
            capture_state = "stream_failed"
        elif fresh_capture:
            capture_state = str(capture_status)
        elif legacy_producing:
            capture_state = "producing_trusted"
        elif startup_age is not None and startup_age <= WATCHER_STARTUP_GRACE.total_seconds():
            capture_state = "starting"
        else:
            capture_state = "capture_stalled"
        watcher_details[match_id] = {
            "state": state,
            "running": is_running,
            "producing": is_producing,
            "reason": reason,
            "capture_state": capture_state,
            "capture_updated_at": (
                capture_at.isoformat()
                if isinstance(capture_at, datetime)
                else None
            ),
            "capture_age_seconds": round(capture_age, 3) if capture_age is not None else None,
            "layout_profile": heartbeat.get("layout_profile") if heartbeat else None,
            "layout_confidence": heartbeat.get("layout_confidence") if heartbeat else None,
            "screen_state": heartbeat.get("screen_state") if heartbeat else None,
            "replay_gate_status": heartbeat.get("replay_gate_status") if heartbeat else None,
            "output_updated_at": output_at.isoformat() if output_at else None,
            "output_age_seconds": round(output_age, 3) if output_age is not None else None,
            "retry": retry_details,
        }

    waiting = running - producing - unrecognized - stale
    if not desired:
        status = "healthy"
        reason = "idle"
        error = None
    elif exhausted:
        status = "unhealthy"
        reason = "watcher_retry_exhausted"
        error = f"watcher retry exhausted: {','.join(sorted(exhausted))}"
    elif missing:
        status = "unhealthy"
        reason = "watchers_not_running"
        error = f"watchers not running: {','.join(sorted(missing))}"
    elif stale:
        status = "unhealthy"
        if capture_stalled_matches:
            reason = "watchers_capture_stalled"
            error = f"watchers capture stalled: {','.join(sorted(stale))}"
        else:
            reason = "watchers_not_producing"
            error = f"watchers not producing fresh output: {','.join(sorted(stale))}"
    elif retrying:
        status = "degraded"
        reason = "watcher_retry_scheduled"
        error = f"watcher startup retry scheduled: {','.join(sorted(retrying))}"
    elif queued:
        status = "degraded"
        reason = "watcher_capacity_limited"
        error = None
    elif unrecognized:
        status = "degraded"
        reason = "capturing_unrecognized"
        error = None
    elif producing == desired:
        status = "healthy"
        reason = "all_watchers_producing"
        error = None
    elif waiting == desired:
        status = "starting"
        reason = "awaiting_first_output"
        error = None
    else:
        status = "degraded"
        reason = "watchers_starting"
        error = None

    details: dict[str, object] = {
        "desired_watchers": len(desired),
        "running_watchers": len(running),
        "capturing_watchers": len(capturing),
        "producing_watchers": len(producing),
        "desired_match_ids": sorted(desired),
        "running_match_ids": sorted(running),
        "capturing_match_ids": sorted(capturing),
        "producing_match_ids": sorted(producing),
        "retrying_match_ids": sorted(retrying),
        "retry_exhausted_match_ids": sorted(exhausted),
        "queued_match_ids": sorted(queued),
        "max_concurrent_watchers": max_concurrent,
        "reason": reason,
        "watchers": watcher_details,
    }
    return status, details, error


def _exact_dota_live_payload(raw_json: object, match_id: str) -> bool:
    try:
        payload = json.loads(str(raw_json or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and str(payload.get("id") or "") == match_id
        and type(payload.get("game_id")) is int
        and payload["game_id"] == DOTA2_GAME_ID
        and str(payload.get("status") or "") == "2"
    )


def active_match_evidence(
    database: Path, *, now: datetime | None = None
) -> dict[str, str]:
    """Select exact live Dota rows; stream evidence labels but does not gate probes."""
    connection = connect_sqlite(database, read_only=True)
    try:
        rows = connection.execute(
            """SELECT matches.raybet_match_id, matches.status,
                      matches.updated_at, matches.live_url, matches.raw_json,
                      (
                          SELECT events.captured_at
                            FROM browser_events AS events
                           WHERE events.raybet_match_id=matches.raybet_match_id
                             AND events.game_id=?
                             AND events.event_type='video'
                             AND events.recognized=1
                             AND events.capture_reason IS NULL
                             AND events.processing_status='audit_only'
                             AND events.processing_reason='video_audit_only'
                             AND events.payload_storage='external'
                             AND events.payload_artifact_hash IS NOT NULL
                             AND events.page_origin IN (?, ?, ?, ?)
                             AND events.source_path IN (?, ?, ?, ?)
                           ORDER BY julianday(events.captured_at) DESC,
                                    events.event_id DESC
                           LIMIT 1
                      ) AS video_captured_at
                 FROM raybet_matches AS matches""",
            (
                DOTA2_GAME_ID,
                *sorted(RAYBET_ORIGINS),
                *sorted(VIDEO_SOURCE_PATHS),
            ),
        ).fetchall()
    finally:
        connection.close()
    evidence: dict[str, str] = {}
    for row in rows:
        match_id = str(row[0])
        if not raybet_match_is_live(row[1], row[2], now=now):
            continue
        if not _exact_dota_live_payload(row[4], match_id):
            continue
        if stored_public_stream_url(row[3], row[4]) is not None:
            reason = "verified_public_stream"
        elif row[5] is not None and raybet_match_is_live("2", row[5], now=now):
            reason = "fresh_browser_video"
        else:
            reason = "ephemeral_stream_refresh_probe"
        evidence[match_id] = reason
    return dict(sorted(evidence.items()))


def active_matches(database: Path, *, now: datetime | None = None) -> list[str]:
    return list(active_match_evidence(database, now=now))


def run_evidence_retention(
    database: Path,
    evidence_dir: Path,
    active_match_ids: set[str],
) -> dict[str, object]:
    """Apply the fixed policy while excluding every currently active match."""
    return prune_vision_evidence(
        database,
        evidence_dir,
        excluded_match_ids=active_match_ids,
        dry_run=False,
    ).as_dict()


def watcher_command(
    database: Path,
    match_id: str,
    output_dir: Path,
    evidence_dir: Path,
) -> list[str]:
    return managed_child_command([
        sys.executable,
        str(ROOT / "scripts" / "watch_raybet_stream.py"),
        "--match-id",
        match_id,
        "--database",
        str(database.resolve()),
        "--output",
        str(output_dir / f"{match_id}.jsonl"),
        "--evidence-dir",
        str(evidence_dir),
        "--interval",
        "1",
        "--evidence-interval",
        "30",
        "--refresh-url",
    ])


def spawn_watcher(
    database: Path,
    command: list[str],
    stdout: object,
    stderr: object,
    *,
    register: Callable[[subprocess.Popen, object], None] | None = None,
) -> tuple[subprocess.Popen, object]:
    """Spawn one exact watcher without making the child reacquire root locks."""

    command = managed_child_command(command)
    authority_context = delegated_writer_process_environment(
        database,
        role="vision_watcher",
        command=command,
    )
    authority_entered = True
    try:
        child_environment = authority_context.__enter__()
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=child_environment,
        )
        try:
            bind_manager_child_authority(child_environment, process)
            if register is not None:
                register(process, authority_context)
        except BaseException as bind_error:
            termination = terminate_subprocess_tree(process)
            if not termination.ok:
                raise AuthorityCleanupError(
                    "watcher authority binding failed and termination is unproven: "
                    f"{termination.detail}",
                    process=process,
                    authority=authority_context,
                ) from bind_error
            try:
                authority_context.__exit__(None, None, None)
            except BaseException as cleanup_error:
                raise AuthorityCleanupError(
                    "watcher authority cleanup failed after binding error",
                    process=process,
                    authority=authority_context,
                ) from cleanup_error
            authority_entered = False
            raise
    except AuthorityCleanupError:
        raise
    except BaseException:
        if authority_entered:
            try:
                authority_context.__exit__(None, None, None)
            except BaseException as cleanup_error:
                raise AuthorityCleanupError(
                    "watcher authority cleanup failed after spawn error",
                    authority=authority_context,
                ) from cleanup_error
        raise
    return process, authority_context


def reap_children(
    children: dict[str, Child],
    active: set[str],
    authorities: dict[str, object] | None = None,
) -> dict[str, int]:
    exited: dict[str, int] = {}
    for match_id, (process, stdout, stderr) in list(children.items()):
        if match_id not in active and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if match_id not in active or process.poll() is not None:
            exit_code = process.poll()
            exited[match_id] = int(exit_code) if exit_code is not None else -1
            stdout.close()
            stderr.close()
            if authorities is not None:
                authority = authorities.get(match_id)
                if authority is not None:
                    try:
                        authority.__exit__(None, None, None)
                    except Exception as error:
                        raise AuthorityCleanupError(
                            f"watcher authority cleanup failed: {match_id}"
                        ) from error
                    authorities.pop(match_id, None)
            children.pop(match_id)
    if authorities is not None:
        for match_id in tuple(set(authorities) - set(children)):
            authority = authorities[match_id]
            try:
                authority.__exit__(None, None, None)
            except BaseException as error:
                raise AuthorityCleanupError(
                    f"watcher authority cleanup failed: {match_id}",
                    authority=authority,
                ) from error
            authorities.pop(match_id, None)
    return exited


def watcher_retry_after_failure(
    previous: WatcherRetryState | None,
    *,
    exit_code: int | None,
    produced_output: bool,
    failed_at: datetime,
    failure_reason: str | None = None,
) -> WatcherRetryState:
    attempts = 1 if previous is None or produced_output else previous.attempts + 1
    exhausted = attempts >= WATCHER_MAX_START_FAILURES
    reason = failure_reason or (
        "source_refresh_failed" if exit_code == 2 else "watcher_startup_failed"
    )
    retry_at = None
    if not exhausted:
        retry_at = failed_at + WATCHER_RETRY_DELAYS[attempts - 1]
    return WatcherRetryState(
        attempts=attempts,
        failure_reason=reason,
        last_exit_code=exit_code,
        failed_at=failed_at,
        retry_at=retry_at,
        exhausted=exhausted,
    )


def startable_matches(
    active: set[str],
    children: Mapping[str, Child],
    retry_states: Mapping[str, WatcherRetryState],
    *,
    now: datetime,
    max_concurrent: int = MAX_CONCURRENT_WATCHERS,
) -> list[str]:
    slots = max(0, max_concurrent - len(children))
    if slots == 0:
        return []
    result: list[str] = []
    for match_id in sorted(active - set(children)):
        retry = retry_states.get(match_id)
        if retry is not None and (
            retry.exhausted
            or (retry.retry_at is not None and now < retry.retry_at)
        ):
            continue
        result.append(match_id)
        if len(result) >= slots:
            break
    return result


def resolve_data_paths(args: argparse.Namespace) -> argparse.Namespace:
    paths = service_data_paths(args.database)
    args.database = paths.database
    args.output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else paths.vision_observations
    )
    args.evidence_dir = (
        args.evidence_dir.resolve()
        if args.evidence_dir is not None
        else paths.vision_evidence
    )
    args.log_dir = (
        args.log_dir.resolve()
        if args.log_dir is not None
        else paths.vision_logs
    )
    return args


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return resolve_data_paths(parser.parse_args())


def _run_cli(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    children: dict[str, Child] = {}
    started_at: dict[str, datetime] = {}
    output_baselines: dict[str, OutputSignature | None] = {}
    capture_baselines: dict[str, OutputSignature | None] = {}
    retry_states: dict[str, WatcherRetryState] = {}
    child_authorities: dict[str, object] = {}
    last_retention_at: float | None = None
    retention_details: dict[str, object] = {"status": "pending"}
    try:
        while True:
            try:
                evidence = active_match_evidence(args.database)
                active = set(evidence)
                exited_output = {
                    match_id: (
                        _output_signature(args.output_dir / f"{match_id}.jsonl")
                        != output_baselines.get(match_id)
                        or _output_signature(
                            _capture_heartbeat_path(args.output_dir, match_id)
                        )
                        != capture_baselines.get(match_id)
                    )
                    for match_id, (process, _, _) in children.items()
                    if match_id in active and process.poll() is not None
                }
                exited = reap_children(children, active, child_authorities)
                failed_at = datetime.now(timezone.utc)
                for match_id, exit_code in exited.items():
                    if match_id not in active:
                        continue
                    retry_states[match_id] = watcher_retry_after_failure(
                        retry_states.get(match_id),
                        exit_code=exit_code,
                        produced_output=exited_output.get(match_id, False),
                        failed_at=failed_at,
                    )
                for match_id in set(retry_states) - active:
                    retry_states.pop(match_id, None)
                for match_id in set(started_at) - set(children):
                    started_at.pop(match_id, None)
                    output_baselines.pop(match_id, None)
                    capture_baselines.pop(match_id, None)
                for match_id in startable_matches(
                    active,
                    children,
                    retry_states,
                    now=failed_at,
                ):
                    stdout = (args.log_dir / f"{match_id}.stdout.log").open(
                        "a", encoding="utf-8"
                    )
                    stderr = (args.log_dir / f"{match_id}.stderr.log").open(
                        "a", encoding="utf-8"
                    )
                    output_baselines[match_id] = _output_signature(
                        args.output_dir / f"{match_id}.jsonl"
                    )
                    capture_baselines[match_id] = _output_signature(
                        _capture_heartbeat_path(args.output_dir, match_id)
                    )

                    def register_watcher(
                        spawned: subprocess.Popen,
                        authority: object,
                    ) -> None:
                        children[match_id] = (spawned, stdout, stderr)
                        child_authorities[match_id] = authority

                    try:
                        command = watcher_command(
                            args.database,
                            match_id,
                            args.output_dir,
                            args.evidence_dir,
                        )
                        process, authority_context = spawn_watcher(
                            args.database,
                            command,
                            stdout,
                            stderr,
                            register=register_watcher,
                        )
                    except AuthorityCleanupError as error:
                        if error.authority is not None:
                            child_authorities[match_id] = error.authority
                        if error.process is not None:
                            children[match_id] = (error.process, stdout, stderr)
                        raise
                    except BaseException as error:
                        stdout.close()
                        stderr.close()
                        output_baselines.pop(match_id, None)
                        capture_baselines.pop(match_id, None)
                        if not isinstance(error, Exception):
                            raise
                        retry_states[match_id] = watcher_retry_after_failure(
                            retry_states.get(match_id),
                            exit_code=None,
                            produced_output=False,
                            failed_at=datetime.now(timezone.utc),
                            failure_reason="watcher_spawn_failed",
                        )
                        continue
                    child_authorities[match_id] = authority_context
                    children[match_id] = (process, stdout, stderr)
                    started_at[match_id] = datetime.now(timezone.utc)
                monotonic_now = time.monotonic()
                if (
                    last_retention_at is None
                    or monotonic_now - last_retention_at >= RETENTION_INTERVAL_SECONDS
                ):
                    last_retention_at = monotonic_now
                    try:
                        retention_details = run_evidence_retention(
                            args.database, args.evidence_dir, active
                        )
                    except Exception as retention_error:
                        retention_details = {
                            "status": "error",
                            "error_type": type(retention_error).__name__,
                        }
                status, details, error = supervisor_health(
                    active,
                    children,
                    args.output_dir,
                    started_at=started_at,
                    output_baselines=output_baselines,
                    capture_baselines=capture_baselines,
                    retry_states=retry_states,
                )
                details["active_match_evidence"] = evidence
                for match_id in details["producing_match_ids"]:
                    retry_states.pop(str(match_id), None)
                details["evidence_retention"] = retention_details
                if retention_details.get("status") == "error":
                    if status == "healthy":
                        status = "degraded"
                    error = error or "vision evidence retention failed"
                record_supervisor_health(
                    args.database,
                    status,
                    active_matches=int(details["running_watchers"]),
                    error=error,
                    details=details,
                )
            except AuthorityCleanupError:
                raise
            except Exception as error:
                try:
                    record_supervisor_health(
                        args.database,
                        "unhealthy",
                        active_matches=len(children),
                        error=type(error).__name__,
                        details={"error_type": type(error).__name__},
                    )
                except Exception:
                    pass
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        reap_children(children, set(), child_authorities)
        try:
            record_supervisor_health(
                args.database, "stopped", active_matches=0
            )
        except Exception:
            pass


def main() -> int:
    args = _parse_args()
    with database_writer_authority(args.database):
        return _run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
