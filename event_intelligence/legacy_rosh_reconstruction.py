"""Offline reconstruction audit for unversioned legacy R.O.S.H. evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from database.session import PostgresSession
from prematch.stratz_rosh import score_rosh_picks

LEGACY_ROSH_RECONSTRUCTION_VERSION = "legacy-rosh-reconstruction-audit-v1"
LEGACY_ROSH_FORMULA_VERSION = (
    "dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793"
)
LEGACY_ROSH_FORMULA_ENTRY = "prematch.stratz_rosh.score_rosh_picks"
LEGACY_ROSH_EVIDENCE_SCHEMA = "unversioned-legacy-historical-rosh-evidence"
_FORMULA_INPUTS_KEY = "legacy_formula_inputs"
_REPLAY_TOLERANCE = 1e-9
_ROLE_CONFIDENCE_MIN = 0.7
_RECONSTRUCTED_ASSIGNMENT_VERSION = (
    "role-assignment-v1-reconstructed-walk-forward"
)
_UTC = timezone.utc
_CLASSIFICATIONS = (
    "exact_legacy_replayable",
    "partially_replayable",
    "score_only",
    "cutoff_unsafe",
)
_REQUIRED_ANALYSIS_KEYS = (
    "heroes_meta_positions",
    "hero_stats_by_time_bracket",
    "synergy",
)
_MINUTE_NUMERIC_FIELDS = (
    "advantage_percent",
    "radiant_advantage",
    "dire_advantage",
    "match_percentage",
    "win_rate_graph",
    "hero_adjustment",
    "hero_base_adjustment",
    "hero_tempo_adjustment",
    "synergy_adjustment",
    "player_adjustment",
)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
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


def _five_ids(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(parsed, list)
        or len(parsed) != 5
        or any(type(item) is not int or item <= 0 for item in parsed)
        or len(set(parsed)) != 5
    ):
        return None
    return tuple(parsed)


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        return {}
    return value


def _minute_table_complete(evidence: Mapping[str, Any]) -> bool:
    table = evidence.get("pure_minute_table")
    if not isinstance(table, list) or not table:
        return False
    previous_minute = 19
    for row in table:
        if not isinstance(row, Mapping):
            return False
        minute = row.get("minute")
        time_start = row.get("time_start")
        time_end = row.get("time_end")
        if (
            type(minute) is not int
            or type(time_start) is not int
            or type(time_end) is not int
            or not 20 <= time_start <= minute <= time_end <= 60
            or minute <= previous_minute
            or row.get("advantage_side") not in {"radiant", "dire", "even"}
        ):
            return False
        previous_minute = minute
        if any(
            isinstance(row.get(field), bool)
            or not isinstance(row.get(field), (int, float))
            or not math.isfinite(float(row[field]))
            for field in _MINUTE_NUMERIC_FIELDS
        ):
            return False
    return True


def _evidence_schema(evidence: Mapping[str, Any]) -> str:
    required = {
        "source",
        "source_week",
        "source_as_of",
        "formula_version",
        "historical_match_id",
        "response_hashes",
        "retrospective",
        "pure_minute_table",
        "score",
    }
    return (
        LEGACY_ROSH_EVIDENCE_SCHEMA
        if required.issubset(evidence)
        else "unknown-legacy-evidence"
    )


def _picks(
    value: object,
    expected: tuple[int, ...],
    field: str,
) -> tuple[dict[str, int], ...]:
    if not isinstance(value, list) or len(value) != 5:
        raise ValueError(f"{field} must contain five picks")
    by_position: dict[int, int] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} picks must be objects")
        hero_id = item.get("heroId")
        position_id = item.get("positionId")
        if (
            type(hero_id) is not int
            or hero_id <= 0
            or type(position_id) is not int
            or position_id not in range(1, 6)
            or position_id in by_position
        ):
            raise ValueError(f"{field} picks are invalid")
        by_position[position_id] = hero_id
    if tuple(by_position[position] for position in range(1, 6)) != expected:
        raise ValueError(f"{field} picks disagree with expected positions")
    return tuple(
        {"heroId": by_position[position], "positionId": position}
        for position in range(1, 6)
    )


def _formula_inputs(
    evidence: Mapping[str, Any],
    radiant_expected: tuple[int, ...],
    dire_expected: tuple[int, ...],
) -> tuple[
    tuple[dict[str, int], ...],
    tuple[dict[str, int], ...],
    dict[str, Any],
]:
    value = evidence.get(_FORMULA_INPUTS_KEY)
    if not isinstance(value, Mapping):
        raise ValueError("raw_formula_inputs_unavailable")
    radiant = _picks(value.get("radiant_picks"), radiant_expected, "radiant")
    dire = _picks(value.get("dire_picks"), dire_expected, "dire")
    analysis = value.get("analysis")
    if not isinstance(analysis, dict) or any(
        not isinstance(analysis.get(key), Mapping) for key in _REQUIRED_ANALYSIS_KEYS
    ):
        raise ValueError("raw_formula_analysis_incomplete")
    return radiant, dire, analysis


def recompute_legacy_pure_score(
    evidence: Mapping[str, Any],
    *,
    formula_version: str,
    radiant_expected: tuple[int, ...],
    dire_expected: tuple[int, ...],
) -> float:
    """Recompute from frozen v1 inputs without a database, profile, or network."""

    if formula_version != LEGACY_ROSH_FORMULA_VERSION:
        raise ValueError("legacy_formula_unavailable")
    radiant, dire, analysis = _formula_inputs(
        evidence,
        radiant_expected,
        dire_expected,
    )
    result = score_rosh_picks(radiant, dire, analysis)
    value = result.get("pure_lineup_score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("frozen_legacy_formula_produced_no_score")
    score = float(value)
    if not math.isfinite(score):
        raise ValueError("frozen_legacy_formula_produced_invalid_score")
    return score


@dataclass(frozen=True)
class LegacyRoshStoredRecord:
    match_id: int
    score_key: str
    formula_version: str
    prediction_cutoff: datetime
    source_week: int
    source_as_of: str
    evidence: Mapping[str, Any]
    evidence_hash: str
    stored_score: float
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    radiant_expected: tuple[int, ...]
    dire_expected: tuple[int, ...]
    event_id: str
    patch: int | None


@dataclass(frozen=True)
class _LegacyTarget:
    prediction_cutoff: datetime
    radiant_expected: tuple[int, ...]
    dire_expected: tuple[int, ...]
    event_id: str
    patch: int | None


@dataclass(frozen=True)
class LegacyRoshAuditRow:
    match_id: int
    score_key: str
    formula_version: str
    evidence_schema: str
    prediction_cutoff: str
    source_week: int
    source_as_of: str
    evidence_hash_valid: bool
    formula_available: bool
    minute_table_complete: bool
    required_inputs_complete: bool
    independent_replay_succeeded: bool
    recomputed_score: float | None
    stored_score: float
    absolute_difference: float | None
    classification: str
    missing_reason: str | None
    unsafe_reason: str | None
    event_id: str
    patch: int | None


@dataclass(frozen=True)
class LegacyRoshAuditStage:
    stage: str
    support: int


@dataclass(frozen=True)
class LegacyRoshAuditCount:
    value: str
    support: int


@dataclass(frozen=True)
class LegacyRoshAuditReport:
    version: str
    formula_entry: str
    stages: tuple[LegacyRoshAuditStage, ...]
    classifications: tuple[LegacyRoshAuditCount, ...]
    formula_versions: tuple[LegacyRoshAuditCount, ...]
    evidence_schemas: tuple[LegacyRoshAuditCount, ...]
    missing_reasons: tuple[LegacyRoshAuditCount, ...]
    unsafe_reasons: tuple[LegacyRoshAuditCount, ...]
    records: tuple[LegacyRoshAuditRow, ...]
    exact_support_scope: Mapping[str, Any] | None
    source_fingerprint_before: str
    source_fingerprint_after: str
    source_unchanged: bool


def _unsafe_reason(record: LegacyRoshStoredRecord) -> str | None:
    reasons: list[str] = []
    evidence = record.evidence
    if evidence.get("historical_match_id") != record.match_id:
        reasons.append("historical_match_identity_mismatch")
    if evidence.get("source_week") != record.source_week:
        reasons.append("source_week_mismatch")
    try:
        stored_source_at = _utc(record.source_as_of, "source_as_of")
        evidence_source_at = _utc(evidence.get("source_as_of"), "evidence source_as_of")
    except ValueError:
        reasons.append("source_as_of_invalid")
        stored_source_at = None
        evidence_source_at = None
    if stored_source_at is not None and evidence_source_at is not None:
        if stored_source_at != evidence_source_at:
            reasons.append("source_as_of_mismatch")
        if stored_source_at > record.prediction_cutoff:
            reasons.append("source_as_of_after_prediction_cutoff")
    try:
        source_week_at = datetime.fromtimestamp(record.source_week, tz=_UTC)
    except (OSError, OverflowError, ValueError):
        reasons.append("source_week_invalid")
    else:
        if source_week_at > record.prediction_cutoff:
            reasons.append("source_week_after_prediction_cutoff")
    return ";".join(dict.fromkeys(reasons)) or None


def classify_legacy_rosh_record(
    record: LegacyRoshStoredRecord,
) -> LegacyRoshAuditRow:
    evidence_hash_valid = hmac.compare_digest(
        _hash(record.evidence),
        record.evidence_hash,
    )
    formula_available = (
        record.formula_version == LEGACY_ROSH_FORMULA_VERSION
        and record.evidence.get("formula_version") == record.formula_version
    )
    minute_table_complete = _minute_table_complete(record.evidence)
    required_inputs_complete = False
    independent_replay_succeeded = False
    recomputed_score: float | None = None
    difference: float | None = None
    missing_reason: str | None = None

    if evidence_hash_valid and formula_available:
        try:
            _formula_inputs(
                record.evidence,
                record.radiant_expected,
                record.dire_expected,
            )
            required_inputs_complete = True
        except ValueError as error:
            missing_reason = str(error)
        if required_inputs_complete:
            try:
                first = recompute_legacy_pure_score(
                    record.evidence,
                    formula_version=record.formula_version,
                    radiant_expected=record.radiant_expected,
                    dire_expected=record.dire_expected,
                )
                second = recompute_legacy_pure_score(
                    record.evidence,
                    formula_version=record.formula_version,
                    radiant_expected=record.radiant_expected,
                    dire_expected=record.dire_expected,
                )
                if first != second:
                    missing_reason = "legacy_replay_nondeterministic"
                else:
                    recomputed_score = first
                    difference = abs(first - record.stored_score)
                    if difference <= _REPLAY_TOLERANCE:
                        independent_replay_succeeded = True
                    else:
                        missing_reason = "legacy_replay_score_mismatch"
            except (TypeError, ValueError) as error:
                missing_reason = f"legacy_replay_failed:{error}"
    elif not evidence_hash_valid:
        missing_reason = "evidence_hash_invalid"
    else:
        missing_reason = "legacy_formula_unavailable"

    unsafe_reason = _unsafe_reason(record)
    if unsafe_reason is not None:
        classification = "cutoff_unsafe"
    elif independent_replay_succeeded:
        classification = "exact_legacy_replayable"
    elif not evidence_hash_valid:
        classification = "score_only"
    elif required_inputs_complete or minute_table_complete or isinstance(
        record.evidence.get("response_hashes"), Mapping
    ):
        classification = "partially_replayable"
    else:
        classification = "score_only"
    return LegacyRoshAuditRow(
        match_id=record.match_id,
        score_key=record.score_key,
        formula_version=record.formula_version,
        evidence_schema=_evidence_schema(record.evidence),
        prediction_cutoff=_timestamp(record.prediction_cutoff),
        source_week=record.source_week,
        source_as_of=record.source_as_of,
        evidence_hash_valid=evidence_hash_valid,
        formula_available=formula_available,
        minute_table_complete=minute_table_complete,
        required_inputs_complete=required_inputs_complete,
        independent_replay_succeeded=independent_replay_succeeded,
        recomputed_score=recomputed_score,
        stored_score=record.stored_score,
        absolute_difference=difference,
        classification=classification,
        missing_reason=missing_reason,
        unsafe_reason=unsafe_reason,
        event_id=record.event_id,
        patch=record.patch,
    )


def _source_fingerprint(connection: PostgresSession) -> str:
    rows = connection.execute(
        """SELECT score_key, match_id, formula_version, source_week,
                  source_as_of, evidence_hash, pure_lineup_score
             FROM historical_rosh_lineup_scores
            ORDER BY score_key"""
    ).fetchall()
    return _hash([list(row) for row in rows])


def _load_candidates(
    connection: PostgresSession,
) -> tuple[LegacyRoshStoredRecord, ...]:
    role_rows = connection.execute(
        """SELECT eligible.match_id, status.event_id, status.start_time,
                  game.patch, player.player_slot, player.hero_id,
                  player.is_radiant, role.position, role.confidence,
                  role.input_cutoff
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
            ORDER BY eligible.match_id, player.player_slot""",
        (_RECONSTRUCTED_ASSIGNMENT_VERSION,),
    ).fetchall()
    roles_by_match: dict[int, list[Any]] = {}
    for row in role_rows:
        roles_by_match.setdefault(int(row["match_id"]), []).append(row)
    targets: dict[int, _LegacyTarget] = {}
    for match_id, match_rows in roles_by_match.items():
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
                input_cutoff = _utc(row["input_cutoff"], "role input_cutoff")
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
                or input_cutoff > cutoff
                or position in sides[bool(side)]
            ):
                valid = False
                break
            sides[bool(side)][position] = hero_id
        if not valid or any(set(side) != set(range(1, 6)) for side in sides.values()):
            continue
        radiant_expected = tuple(sides[True][position] for position in range(1, 6))
        dire_expected = tuple(sides[False][position] for position in range(1, 6))
        if len(set((*radiant_expected, *dire_expected))) != 10:
            continue
        patch = match_rows[0]["patch"]
        targets[match_id] = _LegacyTarget(
            prediction_cutoff=cutoff,
            radiant_expected=radiant_expected,
            dire_expected=dire_expected,
            event_id=str(match_rows[0]["event_id"]),
            patch=patch if type(patch) is int and patch > 0 else None,
        )
    rows = connection.execute(
        """SELECT score_key, match_id, radiant_hero_ids_json,
                  dire_hero_ids_json, pure_lineup_score, source_week,
                  source_as_of, formula_version, evidence_json, evidence_hash
             FROM historical_rosh_lineup_scores
            ORDER BY match_id, score_key"""
    ).fetchall()
    result: list[LegacyRoshStoredRecord] = []
    for row in rows:
        match_id = int(row["match_id"])
        target = targets.get(match_id)
        radiant = _five_ids(row["radiant_hero_ids_json"])
        dire = _five_ids(row["dire_hero_ids_json"])
        if (
            target is None
            or radiant is None
            or dire is None
            or len(set((*radiant, *dire))) != 10
        ):
            continue
        if (
            set(radiant) != set(target.radiant_expected)
            or set(dire) != set(target.dire_expected)
        ):
            continue
        result.append(
            LegacyRoshStoredRecord(
                match_id=match_id,
                score_key=str(row["score_key"]),
                formula_version=str(row["formula_version"]),
                prediction_cutoff=target.prediction_cutoff,
                source_week=int(row["source_week"]),
                source_as_of=str(row["source_as_of"]),
                evidence=_json_object(row["evidence_json"]),
                evidence_hash=str(row["evidence_hash"]),
                stored_score=float(row["pure_lineup_score"]),
                radiant_hero_ids=radiant,
                dire_hero_ids=dire,
                radiant_expected=target.radiant_expected,
                dire_expected=target.dire_expected,
                event_id=target.event_id,
                patch=target.patch,
            )
        )
    return tuple(result)


def _counts(values: Sequence[str]) -> tuple[LegacyRoshAuditCount, ...]:
    return tuple(
        LegacyRoshAuditCount(value, support)
        for value, support in sorted(Counter(values).items())
    )


def _classification_counts(
    records: Sequence[LegacyRoshAuditRow],
) -> tuple[LegacyRoshAuditCount, ...]:
    counts = Counter(row.classification for row in records)
    return tuple(
        LegacyRoshAuditCount(classification, counts[classification])
        for classification in _CLASSIFICATIONS
    )


def _scope(records: Sequence[LegacyRoshAuditRow]) -> Mapping[str, Any] | None:
    exact = [
        row for row in records if row.classification == "exact_legacy_replayable"
    ]
    if not exact:
        return None
    cutoffs = sorted(row.prediction_cutoff for row in exact)
    return {
        "support": len(exact),
        "events": sorted({row.event_id for row in exact}),
        "patches": sorted({row.patch for row in exact if row.patch is not None}),
        "prediction_cutoff_start": cutoffs[0],
        "prediction_cutoff_end": cutoffs[-1],
        "coverage": len(exact) / len(records) if records else 0.0,
    }


def audit_legacy_rosh_reconstruction(
    connection: PostgresSession,
    *,
    max_rows: int | None = None,
) -> LegacyRoshAuditReport:
    """Classify legacy evidence without writing or contacting external systems."""

    if not isinstance(connection, PostgresSession):
        raise ValueError("connection must be a PostgresSession")
    if max_rows is not None and (type(max_rows) is not int or max_rows < 1):
        raise ValueError("max_rows must be a positive integer")
    fingerprint_before = _source_fingerprint(connection)
    candidates = _load_candidates(connection)
    if max_rows is not None:
        candidates = candidates[:max_rows]
    records = tuple(classify_legacy_rosh_record(row) for row in candidates)
    fingerprint_after = _source_fingerprint(connection)
    source_unchanged = fingerprint_before == fingerprint_after
    if not source_unchanged:
        raise ValueError("legacy R.O.S.H. source changed during read-only audit")

    hash_valid = tuple(row for row in records if row.evidence_hash_valid)
    formula_available = tuple(row for row in hash_valid if row.formula_available)
    inputs_complete = tuple(
        row for row in formula_available if row.required_inputs_complete
    )
    replayed = tuple(
        row for row in inputs_complete if row.independent_replay_succeeded
    )
    cutoff_safe = tuple(row for row in replayed if row.unsafe_reason is None)
    exact = tuple(
        row
        for row in cutoff_safe
        if row.classification == "exact_legacy_replayable"
    )
    stages = (
        LegacyRoshAuditStage("candidate", len(records)),
        LegacyRoshAuditStage("evidence_hash_valid", len(hash_valid)),
        LegacyRoshAuditStage("legacy_formula_available", len(formula_available)),
        LegacyRoshAuditStage("required_inputs_complete", len(inputs_complete)),
        LegacyRoshAuditStage("independent_replay_succeeded", len(replayed)),
        LegacyRoshAuditStage("cutoff_safe", len(cutoff_safe)),
        LegacyRoshAuditStage("exact_legacy_replayable", len(exact)),
    )
    return LegacyRoshAuditReport(
        version=LEGACY_ROSH_RECONSTRUCTION_VERSION,
        formula_entry=LEGACY_ROSH_FORMULA_ENTRY,
        stages=stages,
        classifications=_classification_counts(records),
        formula_versions=_counts([row.formula_version for row in records]),
        evidence_schemas=_counts([row.evidence_schema for row in records]),
        missing_reasons=_counts(
            [row.missing_reason for row in records if row.missing_reason is not None]
        ),
        unsafe_reasons=_counts(
            [row.unsafe_reason for row in records if row.unsafe_reason is not None]
        ),
        records=records,
        exact_support_scope=_scope(records),
        source_fingerprint_before=fingerprint_before,
        source_fingerprint_after=fingerprint_after,
        source_unchanged=source_unchanged,
    )


def report_as_dict(report: LegacyRoshAuditReport) -> dict[str, object]:
    return asdict(report)


def report_as_markdown(report: LegacyRoshAuditReport) -> str:
    lines = [
        "# Legacy R.O.S.H. Reconstruction Audit",
        "",
        f"- Version: `{report.version}`",
        f"- Frozen formula entry: `{report.formula_entry}`",
        f"- Source unchanged: `{str(report.source_unchanged).lower()}`",
        "",
        "## Funnel",
        "",
        "| Stage | Support |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {row.stage} | {row.support} |" for row in report.stages)
    for title, counts in (
        ("Classifications", report.classifications),
        ("Missing Reasons", report.missing_reasons),
        ("Unsafe Reasons", report.unsafe_reasons),
    ):
        lines.extend(("", f"## {title}", "", "| Value | Support |", "| --- | ---: |"))
        lines.extend(f"| `{row.value}` | {row.support} |" for row in counts)
    return "\n".join(lines) + "\n"


__all__ = [
    "LEGACY_ROSH_EVIDENCE_SCHEMA",
    "LEGACY_ROSH_FORMULA_ENTRY",
    "LEGACY_ROSH_FORMULA_VERSION",
    "LEGACY_ROSH_RECONSTRUCTION_VERSION",
    "LegacyRoshAuditCount",
    "LegacyRoshAuditReport",
    "LegacyRoshAuditRow",
    "LegacyRoshAuditStage",
    "LegacyRoshStoredRecord",
    "audit_legacy_rosh_reconstruction",
    "classify_legacy_rosh_record",
    "recompute_legacy_pure_score",
    "report_as_dict",
    "report_as_markdown",
]
