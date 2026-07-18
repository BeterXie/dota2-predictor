"""RayBet collection loop."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .direct_response_audit import (
    DirectResponseContext,
    DirectResponseDecision,
    audited_direct_request,
    record_direct_request_failure,
)
from .health import record_health
from .markets import normalized_state_hash, snapshots_from_payload
from .models import utc_now
from .raybet import BASE_URL, DOTA2_GAME_ID, RayBetClient
from .sanitize import sanitize_raybet_payload, verified_public_stream_url
from .storage import LiveBettingStore


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


class RayBetDirectResponseIdentityError(ValueError):
    """A direct response does not belong to the requested Dota match."""


def _require_store_archive_root(store: LiveBettingStore, raw_dir: Path) -> None:
    if raw_dir.resolve() != store.raw_archive_root:
        raise ValueError("collector raw directory does not match store archive root")


def _audit_match_list(
    store: LiveBettingStore,
    rows: list[dict[str, Any]],
    *,
    response_kind: str,
    observed_at: datetime,
) -> None:
    receipt = store.archive_response_payload(
        {"result": rows},
        observed_at=observed_at,
        match_id=None,
        response_kind=response_kind,
    )
    store.record_direct_response_audit(
        receipt,
        response_kind=response_kind,
        claimed_raybet_match_id=None,
        observed_raybet_match_id=None,
        disposition="audit_only",
        reason="match_list_observed",
        request_metadata={"aggregate": True},
        payload_kind="aggregate",
        sanitized=True,
    )


def _dota_match_rows(rows: list[Any]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            game_id = int(row.get("game_id") or 0)
        except (TypeError, ValueError):
            continue
        match_id = str(row.get("id") or "")
        if game_id == DOTA2_GAME_ID and match_id.isdigit():
            filtered.append(row)
    return filtered


def _fetch_provider_match_pages(
    store: LiveBettingStore,
    client: RayBetClient,
    *,
    response_kind: str,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    match_types = (1, 2) if response_kind == "live_match_list" else (4,)
    combined: dict[str, dict[str, Any]] = {}
    endpoint = f"{BASE_URL}/match"
    for match_type in match_types:
        seen_for_type: set[str] = set()
        for page in range(1, max_pages + 1):
            request_identity = (
                f"{endpoint}?match_type={match_type}&page={page}"
            )

            def validate(
                context: DirectResponseContext,
            ) -> DirectResponseDecision[list[Any]]:
                result = context.sanitized_payload.get("result")
                if not isinstance(result, list):
                    raise ValueError("RayBet match page result must be a list")
                return DirectResponseDecision(
                    result,
                    disposition="audit_only",
                    reason="match_page_observed",
                )

            page_rows = audited_direct_request(
                store,
                fetch=lambda match_type=match_type, page=page: (
                    client.match_page_response(match_type, page)
                ),
                process=validate,
                response_kind=response_kind,
                claimed_raybet_match_id=None,
                endpoint=endpoint,
                request_identity=request_identity,
                request_metadata={"match_type": match_type, "page": page},
            )
            if not page_rows:
                break
            for row in _dota_match_rows(page_rows):
                match_id = str(row["id"])
                if match_id not in seen_for_type:
                    combined[match_id] = row
                    seen_for_type.add(match_id)
    rows = list(combined.values())
    if response_kind == "live_match_list":
        rows.sort(key=lambda row: str(row.get("start_time") or ""))
    return rows


def _fetch_match_list(
    store: LiveBettingStore,
    client: Any,
    *,
    response_kind: str,
) -> list[dict[str, Any]]:
    if callable(getattr(client, "match_page_response", None)):
        return _fetch_provider_match_pages(
            store, client, response_kind=response_kind
        )
    fetch = (
        client.live_matches
        if response_kind == "live_match_list"
        else client.completed_matches
    )
    try:
        rows = fetch()
        if not isinstance(rows, list):
            raise ValueError("RayBet match list response must be a list")
    except Exception as error:
        record_direct_request_failure(
            store,
            response_kind=response_kind,
            claimed_raybet_match_id=None,
            error=error,
            observed_at=utc_now(),
        )
        raise
    _audit_match_list(
        store,
        rows,
        response_kind=response_kind,
        observed_at=utc_now(),
    )
    return rows


def _response_match_id(payload: Any) -> str | None:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    value = str(result.get("id") or "")
    return value or None


def _rejection_reason(error: Exception) -> str:
    if isinstance(error, RayBetDirectResponseIdentityError):
        return "identity_mismatch"
    if isinstance(error, ValueError):
        return "validation_failed"
    return f"processing_failed:{type(error).__name__}"


def _fingerprint(payload: dict[str, Any]) -> str:
    payload = sanitize_raybet_payload(payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _direct_observation_key(
    match_id: str, observed_at: datetime, payload_fingerprint: str
) -> str:
    value = f"direct\n{match_id}\n{observed_at.isoformat()}\n{payload_fingerprint}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fetch_odds(
    client: Any, match_id: str
) -> Any:
    if callable(getattr(client, "match_odds_response", None)):
        return client.match_odds_response(match_id)
    return client.match_odds(match_id)


def _collect_odds_response(
    store: LiveBettingStore,
    client: Any,
    *,
    match_id: str,
    response_kind: str,
    list_row: dict[str, Any],
) -> tuple[int, int, str]:
    endpoint = f"{BASE_URL}/odds"
    request_identity = f"{endpoint}?match_id={match_id}"

    def normalize(
        context: DirectResponseContext,
    ) -> DirectResponseDecision[tuple[int, int, str]]:
        payload = context.sanitized_payload
        raw_result = context.payload.get("result")
        observed_at = context.observed_at
        result = payload.get("result")
        observed_match_id = _response_match_id(payload)
        if (
            not isinstance(result, dict)
            or observed_match_id != match_id
            or type(result.get("game_id")) is not int
            or int(result["game_id"]) != DOTA2_GAME_ID
        ):
            raise RayBetDirectResponseIdentityError(
                f"{response_kind} response identity mismatch"
            )
        fingerprint = _fingerprint(payload)
        snapshots = snapshots_from_payload(payload, received_at=observed_at)
        stored_result = dict(result)
        if (
            response_kind == "completed_odds"
            and stored_result.get("status") in (None, "")
        ):
            stored_result["status"] = list_row.get("status", 3)
        public_live_url = (
            verified_public_stream_url(raw_result.get("live_url"))
            if response_kind == "live_odds" and isinstance(raw_result, dict)
            else None
        )
        with store.transaction():
            store.upsert_raybet_match(
                stored_result,
                observed_at,
                public_live_url=public_live_url,
            )
            timing_status, changes = store.store_odds_observation(
                source="direct",
                observation_key=_direct_observation_key(
                    match_id, observed_at, fingerprint
                ),
                source_event_id=None,
                raybet_match_id=match_id,
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash(snapshots),
                snapshots=snapshots,
                raw_payload=payload,
                raw_artifact=context.receipt,
            )
        return DirectResponseDecision(
            (changes, len(snapshots), fingerprint),
            disposition="audit_only" if timing_status == "late" else "accepted",
            reason="late_transport" if timing_status == "late" else "normalized",
            observed_raybet_match_id=observed_match_id,
        )

    return audited_direct_request(
        store,
        fetch=lambda: _fetch_odds(client, match_id),
        process=normalize,
        response_kind=response_kind,
        claimed_raybet_match_id=match_id,
        endpoint=endpoint,
        request_identity=request_identity,
        request_metadata={"operation": response_kind},
        clock=utc_now,
        rejection_reason=_rejection_reason,
    )


def completed_refresh_due(
    completed_rows: list[dict[str, Any]] | None,
    monotonic_now: float,
    next_refresh: float,
) -> bool:
    """Return whether the low-frequency completed feed should be fetched."""
    return completed_rows is None or monotonic_now >= next_refresh


def collect_once(
    store: LiveBettingStore,
    client: RayBetClient,
    raw_dir: Path,
    list_rows: list[dict[str, Any]] | None = None,
    raw_fingerprints: dict[str, str] | None = None,
    audit_match_list: bool = True,
) -> dict[str, int]:
    _require_store_archive_root(store, raw_dir)
    if list_rows is None:
        list_rows = _fetch_match_list(
            store,
            client,
            response_kind="live_match_list",
        )
        audit_match_list = False
    odds_count = 0
    changed_count = 0
    error_count = 0
    raw_fingerprints = raw_fingerprints if raw_fingerprints is not None else {}
    if audit_match_list:
        _audit_match_list(
            store,
            list_rows,
            response_kind="live_match_list",
            observed_at=utc_now(),
        )
    for list_row in list_rows:
        match_id = ""
        try:
            if not isinstance(list_row, dict):
                raise ValueError("live match list row is not an object")
            match_id = str(list_row.get("id") or "")
            if not match_id.isdigit():
                raise ValueError("live match list id is invalid")
            changes, snapshot_count, fingerprint = _collect_odds_response(
                store,
                client,
                match_id=match_id,
                response_kind="live_odds",
                list_row=list_row,
            )
            raw_fingerprints[match_id] = fingerprint
            changed_count += changes
            odds_count += snapshot_count
        except Exception as error:
            error_count += 1
            logger.warning(
                "RayBet fetch failed for live match_id=%s (%s)",
                match_id or "<invalid>",
                type(error).__name__,
            )
    completed_at = utc_now()
    if error_count:
        store.record_collector(
            "raybet",
            success_at=completed_at if len(list_rows) > error_count else None,
            error_at=completed_at,
            error=f"{error_count} live match(s) failed",
        )
    else:
        store.record_collector("raybet", success_at=completed_at)
    return {
        "matches": len(list_rows) - error_count,
        "listed": len(list_rows),
        "odds": odds_count,
        "changed": changed_count,
        "errors": error_count,
    }


def collect_completed_once(
    store: LiveBettingStore,
    client: RayBetClient,
    raw_dir: Path,
    completed_rows: list[dict[str, Any]] | None = None,
    audit_match_list: bool = True,
) -> dict[str, int]:
    """Archive final odds for the low-frequency RayBet completed feed.

    RayBet exposes completed matches through ``match_type=4``.  They are
    deliberately collected outside ``collect_once`` so the live 3-second loop
    does not repeatedly enumerate and fetch the whole historical list.  A
    completed response is still stored as an ordinary immutable transport
    observation; its provider row carries status ``3`` and is therefore
    available to post-match reconciliation and the historical UI.
    """
    _require_store_archive_root(store, raw_dir)
    rows = (
        completed_rows
        if completed_rows is not None
        else _fetch_match_list(
            store,
            client,
            response_kind="completed_match_list",
        )
    )
    odds_count = 0
    changed_count = 0
    error_count = 0
    if completed_rows is not None and audit_match_list:
        _audit_match_list(
            store,
            rows,
            response_kind="completed_match_list",
            observed_at=utc_now(),
        )
    fetched_matches = 0
    for list_row in rows:
        match_id = ""
        try:
            if not isinstance(list_row, dict):
                raise ValueError("completed match list row is not an object")
            match_id = str(list_row.get("id") or "")
            if not match_id.isdigit():
                raise ValueError("completed match list id is invalid")
            changes, snapshot_count, _ = _collect_odds_response(
                store,
                client,
                match_id=match_id,
                response_kind="completed_odds",
                list_row=list_row,
            )
            changed_count += changes
            odds_count += snapshot_count
            fetched_matches += 1
        except Exception as error:
            error_count += 1
            logger.warning(
                "completed RayBet fetch failed for match_id=%s (%s)",
                match_id,
                type(error).__name__,
            )
    completed_at = utc_now()
    if error_count:
        store.record_collector(
            "raybet_completed",
            success_at=completed_at if fetched_matches else None,
            error_at=completed_at,
            error=f"{error_count} completed match(s) failed",
        )
    else:
        store.record_collector("raybet_completed", success_at=completed_at)
    return {
        "matches": fetched_matches,
        "listed": len(rows),
        "odds": odds_count,
        "changed": changed_count,
        "errors": error_count,
    }


def run(args: argparse.Namespace) -> int:
    load_dotenv()
    db_path = Path(args.database)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(args.raw_dir)

    with LiveBettingStore(
        db_path, raw_archive_root=raw_dir
    ) as store, RayBetClient() as client:
        if not getattr(args, "schema_prepared", False):
            store.init_schema()
        started_at = utc_now()
        record_health(
            store.connection,
            "raybet_worker",
            "starting",
            heartbeat_at=started_at,
            details={"source": "worker"},
        )
        failures = 0
        list_rows: list[dict[str, Any]] | None = None
        raw_fingerprints: dict[str, str] = {}
        next_list_refresh = 0.0
        completed_rows: list[dict[str, Any]] | None = None
        next_completed_refresh = 0.0
        while True:
            try:
                monotonic_now = time.monotonic()
                if list_rows is None or monotonic_now >= next_list_refresh:
                    list_rows = _fetch_match_list(
                        store,
                        client,
                        response_kind="live_match_list",
                    )
                    next_list_refresh = monotonic_now + args.list_interval
                completed_refresh_needed = False
                completed_summary = {
                    "matches": 0,
                    "listed": len(completed_rows or []),
                    "odds": 0,
                    "changed": 0,
                    "errors": 0,
                }
                if completed_refresh_due(
                    completed_rows, monotonic_now, next_completed_refresh
                ):
                    next_completed_refresh = (
                        monotonic_now + args.completed_interval
                    )
                    try:
                        completed_rows = _fetch_match_list(
                            store,
                            client,
                            response_kind="completed_match_list",
                        )
                    except Exception as error:
                        completed_rows = completed_rows or []
                        completed_summary["errors"] = 1
                        completed_at = utc_now()
                        store.record_collector(
                            "raybet_completed",
                            error_at=completed_at,
                            error=f"completed list failed: {type(error).__name__}",
                        )
                        logger.warning(
                            "completed RayBet list refresh failed (%s)",
                            type(error).__name__,
                        )
                    else:
                        completed_refresh_needed = True
                summary = collect_once(
                    store, client, raw_dir, list_rows=list_rows,
                    raw_fingerprints=raw_fingerprints,
                    audit_match_list=False,
                )
                if completed_refresh_needed:
                    try:
                        completed_summary = collect_completed_once(
                            store,
                            client,
                            raw_dir,
                            completed_rows=completed_rows,
                            audit_match_list=False,
                        )
                    except Exception as error:
                        completed_summary["errors"] = 1
                        completed_at = utc_now()
                        store.record_collector(
                            "raybet_completed",
                            error_at=completed_at,
                            error=f"completed collection failed: {type(error).__name__}",
                        )
                        logger.warning(
                            "completed RayBet collection failed (%s)",
                            type(error).__name__,
                        )
                failures = 0
                succeeded_at = utc_now()
                partial_errors = summary["errors"] + completed_summary["errors"]
                worker_status = "degraded" if partial_errors else "healthy"
                collection_succeeded = any(
                    (
                        item["matches"] > 0
                        or (item["listed"] == 0 and item["errors"] == 0)
                    )
                    for item in (summary, completed_summary)
                )
                record_health(
                    store.connection,
                    "raybet_worker",
                    worker_status,
                    heartbeat_at=succeeded_at,
                    success_at=succeeded_at if collection_succeeded else None,
                    error_at=succeeded_at if partial_errors else None,
                    error=(
                        f"{partial_errors} match collection error(s)"
                        if partial_errors
                        else None
                    ),
                    details={
                        "source": "worker",
                        **summary,
                        "completed": completed_summary,
                    },
                )
                logger.info(
                    "collected matches=%d odds=%d changed=%d completed=%d",
                    summary["matches"], summary["odds"], summary["changed"],
                    completed_summary["matches"],
                )
            except Exception as exc:
                failures += 1
                now = utc_now()
                store.record_collector("raybet", error_at=now, error=f"{type(exc).__name__}: {exc}")
                record_health(
                    store.connection,
                    "raybet_worker",
                    "degraded",
                    heartbeat_at=now,
                    error_at=now,
                    error=type(exc).__name__,
                    details={"source": "worker", "consecutive_failures": failures},
                )
                logger.error("RayBet collection failed: %s", exc)
                if args.once:
                    return 1
            if args.once:
                return 0
            delay = min(args.max_backoff, args.interval * (2 ** min(failures, 5)))
            time.sleep(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "data" / "dota2.db"))
    parser.add_argument(
        "--raw-dir",
        default=str(ROOT / "data" / "live_betting" / "raw-v2"),
    )
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--list-interval", type=float, default=15.0)
    parser.add_argument("--completed-interval", type=float, default=300.0)
    parser.add_argument("--max-backoff", type=float, default=300.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(run(parse_args()))
