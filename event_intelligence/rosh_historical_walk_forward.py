"""Research-only temporal audit for historical R.O.S.H. statistics queries."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from curl_cffi import requests as cffi_requests

from database.session import PostgresSession
from live_betting.rosh_parity import ExactArtifactReceipt, ExactByteArtifactStore
from prematch.stratz_rosh import (
    ROSH_BRACKET_BASIC,
    build_rosh_query_requests,
    normalize_rosh_analysis,
    score_rosh_picks,
)


ROSH_HISTORICAL_WALK_FORWARD_VERSION = "rosh-historical-walk-forward-v1"
ROSH_HISTORICAL_MODE = "reconstructed_walk_forward"
STRATZ_ENDPOINT = "https://api.stratz.com/graphql"
_ASSIGNMENT_VERSION = "role-assignment-v1-reconstructed-walk-forward"
_ROLE_CONFIDENCE_MIN = 0.7
_UTC = timezone.utc
_OPERATION_KEYS = (
    "heroes_meta_positions",
    "hero_stats_by_time_bracket",
    "synergy",
)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field} must be an RFC 3339 timestamp") from error
    else:
        raise ValueError(f"{field} must be an RFC 3339 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(_UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _receipt(receipt: ExactArtifactReceipt) -> dict[str, object]:
    return {
        "content_sha256": receipt.content_sha256,
        "gzip_sha256": receipt.gzip_sha256,
        "relative_path": receipt.relative_path,
        "byte_count": receipt.byte_count,
    }


@dataclass(frozen=True)
class WalkForwardTarget:
    match_id: int
    prediction_cutoff: datetime
    event_id: str
    patch: int | None
    series_id: int | None
    series_map_number: int | None
    radiant_expected: tuple[int, ...]
    dire_expected: tuple[int, ...]
    future_series_match_ids: tuple[int, ...] = ()
    selection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransportResponse:
    queried_at: datetime
    status_code: int
    body: bytes


class BatchTransport(Protocol):
    def fetch(self, request_body: bytes) -> TransportResponse: ...


class StratzBatchTransport:
    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("STRATZ token is required")
        self._token = token.strip()
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(_UTC))

    def fetch(self, request_body: bytes) -> TransportResponse:
        queried_at = _utc(self._clock(), "queried_at")
        try:
            response = cffi_requests.post(
                STRATZ_ENDPOINT,
                data=request_body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                impersonate="chrome120",
                timeout=self._timeout_seconds,
            )
        except Exception as error:
            raise RuntimeError(
                f"STRATZ temporal audit request failed: {type(error).__name__}"
            ) from None
        status = getattr(response, "status_code", None)
        body = getattr(response, "content", None)
        if type(status) is not int or not isinstance(body, (bytes, bytearray)):
            raise RuntimeError("STRATZ temporal audit response is unavailable")
        return TransportResponse(queried_at, status, bytes(body))


@dataclass(frozen=True)
class TemporalObservation:
    label: str
    queried_at: str
    statistics_cutoff: str
    prediction_cutoff: str
    status_code: int
    request_artifact: Mapping[str, object]
    response_artifact: Mapping[str, object]
    normalized_artifact: Mapping[str, object] | None
    operation_hashes: Mapping[str, str]
    normalized_hash: str | None
    result_hash: str | None
    pure_score: float | None
    offline_exact_replay: bool
    operation_temporal_provenance_complete: bool
    source_match_ids: tuple[int, ...]
    source_timestamps: tuple[str, ...]
    failure_reason: str | None


@dataclass(frozen=True)
class MapTemporalAudit:
    match_id: int
    event_id: str
    patch: int | None
    series_id: int | None
    series_map_number: int | None
    prediction_cutoff: str
    selection_reasons: tuple[str, ...]
    observations: tuple[TemporalObservation, ...]
    repeated_response_changed: bool | None
    historical_point_changed: bool | None
    temporal_provenance_complete: bool
    source_timestamps_within_cutoff: bool | None
    target_match_excluded: bool | None
    future_maps_excluded: bool | None
    gate_passed: bool
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True)
class TemporalAuditCount:
    value: str
    support: int


@dataclass(frozen=True)
class HistoricalWalkForwardAuditReport:
    version: str
    mode: str
    research_only: bool
    prospective: bool
    deployment_eligible: bool
    requested_maps: int
    audited_maps: int
    archived_requests: int
    archived_responses: int
    archived_normalized_statistics: int
    offline_exact_replays: int
    repeated_response_changes: int
    temporal_provenance_complete: int
    gate_passed: bool
    failure_reasons: tuple[TemporalAuditCount, ...]
    sample_events: tuple[str, ...]
    sample_patches: tuple[int, ...]
    sample_selection_reasons: tuple[str, ...]
    maps: tuple[MapTemporalAudit, ...]


def _load_targets(connection: PostgresSession) -> tuple[WalkForwardTarget, ...]:
    rows = connection.execute(
        """SELECT eligible.match_id, status.event_id, status.start_time,
                  status.series_id, game.patch, player.player_slot,
                  player.hero_id, player.is_radiant, role.position,
                  role.confidence, role.input_cutoff
             FROM formal_map_eligibility AS eligible
             JOIN match_ingest_status AS status
               ON status.match_id=eligible.match_id
             JOIN matches AS game ON game.match_id=eligible.match_id
             JOIN match_players AS player ON player.match_id=eligible.match_id
             JOIN player_role_assignments AS role
               ON role.match_id=player.match_id
              AND role.player_slot=player.player_slot
              AND role.purpose='expected_position'
              AND role.assignment_version=?
            WHERE eligible.draft_readiness='ready'
            ORDER BY status.start_time, eligible.match_id, player.player_slot""",
        (_ASSIGNMENT_VERSION,),
    ).fetchall()
    grouped: dict[int, list[Any]] = {}
    for row in rows:
        grouped.setdefault(int(row["match_id"]), []).append(row)
    targets: list[WalkForwardTarget] = []
    for match_id, match_rows in grouped.items():
        if len(match_rows) != 10:
            continue
        start_time = match_rows[0]["start_time"]
        if type(start_time) is not int or start_time <= 0:
            continue
        cutoff = datetime.fromtimestamp(start_time, tz=_UTC)
        sides: dict[bool, dict[int, int]] = {True: {}, False: {}}
        valid = True
        for row in match_rows:
            position = row["position"]
            hero_id = row["hero_id"]
            side = row["is_radiant"]
            confidence = row["confidence"]
            try:
                role_cutoff = _utc(row["input_cutoff"], "role input_cutoff")
            except ValueError:
                valid = False
                break
            if (
                type(position) is not int
                or position not in range(1, 6)
                or type(hero_id) is not int
                or hero_id <= 0
                or side not in (0, 1, False, True)
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or float(confidence) < _ROLE_CONFIDENCE_MIN
                or role_cutoff > cutoff
                or position in sides[bool(side)]
            ):
                valid = False
                break
            sides[bool(side)][position] = hero_id
        if not valid or any(set(side) != set(range(1, 6)) for side in sides.values()):
            continue
        radiant = tuple(sides[True][position] for position in range(1, 6))
        dire = tuple(sides[False][position] for position in range(1, 6))
        if len(set((*radiant, *dire))) != 10:
            continue
        series_id = match_rows[0]["series_id"]
        patch = match_rows[0]["patch"]
        targets.append(
            WalkForwardTarget(
                match_id=match_id,
                prediction_cutoff=cutoff,
                event_id=str(match_rows[0]["event_id"]),
                patch=patch if type(patch) is int and patch > 0 else None,
                series_id=(
                    series_id
                    if type(series_id) is int and series_id > 0
                    else None
                ),
                series_map_number=None,
                radiant_expected=radiant,
                dire_expected=dire,
            )
        )
    by_series: dict[int, list[WalkForwardTarget]] = {}
    for target in targets:
        if target.series_id is not None:
            by_series.setdefault(target.series_id, []).append(target)
    enriched: dict[int, WalkForwardTarget] = {target.match_id: target for target in targets}
    for series_targets in by_series.values():
        ordered = sorted(
            series_targets,
            key=lambda row: (row.prediction_cutoff, row.match_id),
        )
        for index, target in enumerate(ordered, 1):
            enriched[target.match_id] = replace(
                target,
                series_map_number=index,
                future_series_match_ids=tuple(row.match_id for row in ordered[index:]),
            )
    return tuple(
        sorted(
            enriched.values(),
            key=lambda row: (row.prediction_cutoff, row.match_id),
        )
    )


def select_temporal_sample(
    targets: Sequence[WalkForwardTarget],
    *,
    sample_size: int = 20,
) -> tuple[WalkForwardTarget, ...]:
    if sample_size != 20:
        raise ValueError("phase-one temporal sample size is fixed at 20")
    values = tuple(targets)
    if len(values) < sample_size:
        raise ValueError("not enough exact-position maps for temporal audit")
    reasons: dict[int, set[str]] = {}

    def add(target: WalkForwardTarget, reason: str) -> None:
        if target.match_id in reasons or len(reasons) < sample_size:
            reasons.setdefault(target.match_id, set()).add(reason)

    series: dict[int, list[WalkForwardTarget]] = {}
    for target in values:
        if target.series_id is not None:
            series.setdefault(target.series_id, []).append(target)
    triple = next(
        (
            sorted(rows, key=lambda row: int(row.series_map_number or 0))[:3]
            for _series_id, rows in sorted(series.items())
            if {row.series_map_number for row in rows}.issuperset({1, 2, 3})
        ),
        None,
    )
    if triple is None:
        raise ValueError("no exact-position series contains derived maps 1/2/3")
    for target in triple:
        add(target, "same_series_maps_1_2_3")

    def week_distance(target: WalkForwardTarget) -> int:
        value = target.prediction_cutoff
        seconds = (
            value.weekday() * 86_400
            + value.hour * 3_600
            + value.minute * 60
            + value.second
        )
        return min(seconds, 604_800 - seconds)

    for target in sorted(values, key=lambda row: (week_distance(row), row.match_id)):
        if len({row for row in reasons if "week_boundary" in reasons[row]}) >= 4:
            break
        add(target, "week_boundary")

    for patch in sorted({row.patch for row in values if row.patch is not None}):
        rows = [row for row in values if row.patch == patch]
        add(rows[len(rows) // 2], "multiple_patches")

    for event_id in sorted({row.event_id for row in values}):
        rows = [row for row in values if row.event_id == event_id]
        add(rows[len(rows) // 2], "multiple_events")
        if len({values_by_id.event_id for values_by_id in values if values_by_id.match_id in reasons}) >= 8:
            break

    ordered = sorted(values, key=lambda row: (row.prediction_cutoff, row.match_id))
    pairs = sorted(
        zip(ordered, ordered[1:]),
        key=lambda pair: (
            pair[1].prediction_cutoff - pair[0].prediction_cutoff,
            pair[0].match_id,
        ),
    )
    for first, second in pairs:
        if len(reasons) >= sample_size:
            break
        add(first, "consecutive_matches")
        add(second, "consecutive_matches")

    if len(reasons) < sample_size:
        step = max(1, len(ordered) // (sample_size - len(reasons) + 1))
        for target in ordered[::step]:
            add(target, "time_range_fill")
            if len(reasons) >= sample_size:
                break
    if len(reasons) != sample_size:
        raise ValueError("temporal sample selection did not produce exactly 20 maps")
    selected = {target.match_id: target for target in values if target.match_id in reasons}
    return tuple(
        replace(
            selected[match_id],
            selection_reasons=tuple(sorted(selection_reasons)),
        )
        for match_id, selection_reasons in sorted(
            reasons.items(),
            key=lambda item: (
                selected[item[0]].prediction_cutoff,
                item[0],
            ),
        )
    )


def load_temporal_sample(
    connection: PostgresSession,
) -> tuple[WalkForwardTarget, ...]:
    if not isinstance(connection, PostgresSession):
        raise ValueError("connection must be a PostgresSession")
    return select_temporal_sample(_load_targets(connection))


def _request_body(target: WalkForwardTarget, statistics_cutoff: datetime) -> bytes:
    cutoff = _utc(statistics_cutoff, "statistics_cutoff")
    if cutoff > target.prediction_cutoff:
        raise ValueError("statistics_cutoff must not exceed prediction_cutoff")
    requests = build_rosh_query_requests(
        (*target.radiant_expected, *target.dire_expected),
        int(cutoff.timestamp()),
        ROSH_BRACKET_BASIC,
    )
    payload = [
        {
            "operationName": requests[key]["operation_name"],
            "query": requests[key]["query"],
            "variables": requests[key]["variables"],
        }
        for key in _OPERATION_KEYS
    ]
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _batch_payload(body: bytes) -> list[Mapping[str, Any]]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("STRATZ batch response is invalid JSON") from error
    if not isinstance(value, list) or len(value) != len(_OPERATION_KEYS):
        raise ValueError("STRATZ batch response count is invalid")
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("errors"):
            raise ValueError("STRATZ batch response contains an error")
        if not isinstance(item.get("data"), Mapping):
            raise ValueError("STRATZ batch response item has no data")
        result.append(item)
    return result


def _provenance(value: object) -> tuple[tuple[int, ...], tuple[datetime, ...]]:
    match_ids: set[int] = set()
    timestamps: set[datetime] = set()

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).replace("_", "").casefold()
                if normalized in {"matchid", "sourceMatchId".casefold()}:
                    if type(nested) is int and nested > 0:
                        match_ids.add(nested)
                elif normalized in {
                    "startdatetime",
                    "enddatetime",
                    "sourcetimestamp",
                }:
                    try:
                        timestamp = (
                            datetime.fromtimestamp(nested, tz=_UTC)
                            if type(nested) is int
                            else _utc(nested, str(key))
                        )
                    except (OSError, OverflowError, ValueError):
                        pass
                    else:
                        timestamps.add(timestamp)
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(sorted(match_ids)), tuple(sorted(timestamps))


def _load_artifact(root: Path, manifest: Mapping[str, object]) -> bytes:
    relative = manifest.get("relative_path")
    if not isinstance(relative, str):
        raise ValueError("artifact relative_path is unavailable")
    path = root.joinpath(*relative.split("/"))
    compressed = path.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != manifest.get("gzip_sha256"):
        raise ValueError("artifact gzip hash mismatch")
    body = gzip.decompress(compressed)
    if hashlib.sha256(body).hexdigest() != manifest.get("content_sha256"):
        raise ValueError("artifact content hash mismatch")
    return body


def _result(
    target: WalkForwardTarget,
    analysis: Mapping[str, Any],
) -> tuple[float, str]:
    radiant = [
        {"heroId": hero_id, "positionId": position}
        for position, hero_id in enumerate(target.radiant_expected, 1)
    ]
    dire = [
        {"heroId": hero_id, "positionId": position}
        for position, hero_id in enumerate(target.dire_expected, 1)
    ]
    result = score_rosh_picks(radiant, dire, analysis)
    score = result.get("pure_lineup_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("archived normalized statistics produced no score")
    value = float(score)
    if not math.isfinite(value):
        raise ValueError("archived normalized statistics produced invalid score")
    return value, _hash(result)


def _observe(
    target: WalkForwardTarget,
    *,
    label: str,
    statistics_cutoff: datetime,
    transport: BatchTransport,
    artifacts: ExactByteArtifactStore,
    artifact_root: Path,
) -> TemporalObservation:
    cutoff = _utc(statistics_cutoff, "statistics_cutoff")
    request_body = _request_body(target, cutoff)
    request_receipt = artifacts.persist(request_body)
    response = transport.fetch(request_body)
    response_receipt = artifacts.persist(response.body)
    normalized_receipt: ExactArtifactReceipt | None = None
    operation_hashes: dict[str, str] = {}
    normalized_hash: str | None = None
    result_hash: str | None = None
    pure_score: float | None = None
    replayed = False
    operation_provenance_complete = False
    source_match_ids: tuple[int, ...] = ()
    source_timestamps: tuple[str, ...] = ()
    failure_reason: str | None = None
    try:
        if response.status_code != 200:
            raise ValueError(f"STRATZ returned HTTP {response.status_code}")
        batch = _batch_payload(response.body)
        operation_hashes = {
            key: _hash(item) for key, item in zip(_OPERATION_KEYS, batch, strict=True)
        }
        response_map = {
            key: item for key, item in zip(_OPERATION_KEYS, batch, strict=True)
        }
        operation_provenance = tuple(_provenance(item) for item in batch)
        operation_provenance_complete = all(
            match_ids and timestamps
            for match_ids, timestamps in operation_provenance
        )
        normalized = normalize_rosh_analysis(response_map)
        normalized_body = _canonical_json_bytes(normalized)
        normalized_receipt = artifacts.persist(normalized_body)
        normalized_hash = normalized_receipt.content_sha256
        all_match_ids, all_timestamps = _provenance(batch)
        source_match_ids = all_match_ids
        source_timestamps = tuple(_timestamp(value) for value in all_timestamps)
        archived_body = _load_artifact(artifact_root, _receipt(normalized_receipt))
        archived_analysis = json.loads(archived_body.decode("utf-8"))
        if not isinstance(archived_analysis, dict):
            raise ValueError("archived normalized statistics are invalid")
        first_score, first_hash = _result(target, archived_analysis)
        second_score, second_hash = _result(target, archived_analysis)
        if first_score != second_score or first_hash != second_hash:
            raise ValueError("offline replay is not deterministic")
        pure_score = first_score
        result_hash = first_hash
        replayed = True
    except (OSError, TypeError, ValueError) as error:
        failure_reason = str(error)
    return TemporalObservation(
        label=label,
        queried_at=_timestamp(response.queried_at),
        statistics_cutoff=_timestamp(cutoff),
        prediction_cutoff=_timestamp(target.prediction_cutoff),
        status_code=response.status_code,
        request_artifact=_receipt(request_receipt),
        response_artifact=_receipt(response_receipt),
        normalized_artifact=(
            None if normalized_receipt is None else _receipt(normalized_receipt)
        ),
        operation_hashes=operation_hashes,
        normalized_hash=normalized_hash,
        result_hash=result_hash,
        pure_score=pure_score,
        offline_exact_replay=replayed,
        operation_temporal_provenance_complete=operation_provenance_complete,
        source_match_ids=source_match_ids,
        source_timestamps=source_timestamps,
        failure_reason=failure_reason,
    )


def _audit_map(
    target: WalkForwardTarget,
    *,
    transport: BatchTransport,
    artifacts: ExactByteArtifactStore,
    artifact_root: Path,
    throttle_seconds: float,
) -> MapTemporalAudit:
    points = (
        ("cutoff_minus_7d", target.prediction_cutoff - timedelta(days=7)),
        ("prediction_cutoff", target.prediction_cutoff),
        ("prediction_cutoff_repeat", target.prediction_cutoff),
    )
    observations: list[TemporalObservation] = []
    for index, (label, cutoff) in enumerate(points):
        if index and throttle_seconds:
            time.sleep(throttle_seconds)
        observations.append(
            _observe(
                target,
                label=label,
                statistics_cutoff=cutoff,
                transport=transport,
                artifacts=artifacts,
                artifact_root=artifact_root,
            )
        )
    earlier, current, repeated = observations
    repeat_changed = (
        None
        if not current.response_artifact or not repeated.response_artifact
        else current.response_artifact["content_sha256"]
        != repeated.response_artifact["content_sha256"]
    )
    historical_changed = (
        None
        if earlier.normalized_hash is None or current.normalized_hash is None
        else earlier.normalized_hash != current.normalized_hash
    )
    provenance_complete = all(
        observation.operation_temporal_provenance_complete
        for observation in observations
        if observation.failure_reason is None
    ) and all(observation.failure_reason is None for observation in observations)
    timestamps_after_cutoff = any(
        _utc(timestamp, "source timestamp")
        > _utc(observation.statistics_cutoff, "statistics_cutoff")
        for observation in observations
        for timestamp in observation.source_timestamps
    )
    timestamps_within_cutoff = (
        not timestamps_after_cutoff if provenance_complete else None
    )
    observed_ids = {
        match_id
        for observation in observations
        for match_id in observation.source_match_ids
    }
    target_excluded = (
        target.match_id not in observed_ids if provenance_complete else None
    )
    future_excluded = (
        not observed_ids.intersection(target.future_series_match_ids)
        if provenance_complete
        else None
    )
    reasons: list[str] = []
    for observation in observations:
        if observation.failure_reason is not None:
            reasons.append(f"{observation.label}:{observation.failure_reason}")
        if _utc(observation.statistics_cutoff, "statistics_cutoff") > target.prediction_cutoff:
            reasons.append("statistics_cutoff_after_prediction_cutoff")
    if repeat_changed is True:
        reasons.append("repeated_historical_response_changed")
    if not provenance_complete:
        reasons.append("aggregate_response_lacks_temporal_match_provenance")
    if timestamps_after_cutoff:
        reasons.append("source_timestamp_after_statistics_cutoff")
    if target_excluded is False:
        reasons.append("target_match_present_in_statistics")
    if future_excluded is False:
        reasons.append("future_series_map_present_in_statistics")
    if not all(row.offline_exact_replay for row in observations):
        reasons.append("offline_exact_replay_incomplete")
    gate_passed = not reasons
    return MapTemporalAudit(
        match_id=target.match_id,
        event_id=target.event_id,
        patch=target.patch,
        series_id=target.series_id,
        series_map_number=target.series_map_number,
        prediction_cutoff=_timestamp(target.prediction_cutoff),
        selection_reasons=target.selection_reasons,
        observations=tuple(observations),
        repeated_response_changed=repeat_changed,
        historical_point_changed=historical_changed,
        temporal_provenance_complete=provenance_complete,
        source_timestamps_within_cutoff=timestamps_within_cutoff,
        target_match_excluded=target_excluded,
        future_maps_excluded=future_excluded,
        gate_passed=gate_passed,
        failure_reasons=tuple(dict.fromkeys(reasons)),
    )


def run_temporal_semantics_audit(
    sample: Sequence[WalkForwardTarget],
    *,
    transport: BatchTransport,
    artifact_root: str | Path,
    throttle_seconds: float = 1.0,
) -> HistoricalWalkForwardAuditReport:
    targets = tuple(sample)
    if len(targets) != 20:
        raise ValueError("phase-one temporal audit requires exactly 20 maps")
    if throttle_seconds < 0:
        raise ValueError("throttle_seconds cannot be negative")
    root = Path(artifact_root)
    artifacts = ExactByteArtifactStore(root)
    maps = tuple(
        _audit_map(
            target,
            transport=transport,
            artifacts=artifacts,
            artifact_root=root,
            throttle_seconds=throttle_seconds,
        )
        for target in targets
    )
    reasons = Counter(reason for row in maps for reason in row.failure_reasons)
    observations = tuple(item for row in maps for item in row.observations)
    return HistoricalWalkForwardAuditReport(
        version=ROSH_HISTORICAL_WALK_FORWARD_VERSION,
        mode=ROSH_HISTORICAL_MODE,
        research_only=True,
        prospective=False,
        deployment_eligible=False,
        requested_maps=20,
        audited_maps=len(maps),
        archived_requests=len(observations),
        archived_responses=len(observations),
        archived_normalized_statistics=sum(
            row.normalized_artifact is not None for row in observations
        ),
        offline_exact_replays=sum(row.offline_exact_replay for row in observations),
        repeated_response_changes=sum(row.repeated_response_changed is True for row in maps),
        temporal_provenance_complete=sum(row.temporal_provenance_complete for row in maps),
        gate_passed=all(row.gate_passed for row in maps),
        failure_reasons=tuple(
            TemporalAuditCount(value, support)
            for value, support in sorted(reasons.items())
        ),
        sample_events=tuple(sorted({row.event_id for row in maps})),
        sample_patches=tuple(sorted({row.patch for row in maps if row.patch is not None})),
        sample_selection_reasons=tuple(
            sorted({reason for row in maps for reason in row.selection_reasons})
        ),
        maps=maps,
    )


def report_as_dict(report: HistoricalWalkForwardAuditReport) -> dict[str, object]:
    return asdict(report)


__all__ = [
    "ROSH_HISTORICAL_MODE",
    "ROSH_HISTORICAL_WALK_FORWARD_VERSION",
    "BatchTransport",
    "HistoricalWalkForwardAuditReport",
    "MapTemporalAudit",
    "StratzBatchTransport",
    "TemporalObservation",
    "TransportResponse",
    "WalkForwardTarget",
    "load_temporal_sample",
    "report_as_dict",
    "run_temporal_semantics_audit",
    "select_temporal_sample",
]
