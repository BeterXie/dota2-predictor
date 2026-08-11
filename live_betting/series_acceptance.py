"""Read-only acceptance audit for complete, independent RayBet Series and Maps."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from database.session import PostgresSession

from .map_decision_checkpoints import (
    LIVE_ODDS_MAX_AGE_SECONDS,
    LIVE_ODDS_VISION_GAP_MAX_SECONDS,
    LIVE_VISION_MAX_AGE_SECONDS,
    MINIMUM_EDGE,
    PREGAME_ODDS_MAX_AGE_SECONDS,
    STRATEGY_VERSION,
    checkpoint_evaluation_eligibility,
)
from .live_match_state import parse_sourced_manual_draft_authority
from .live_probability import MODEL_VERSION as LIVE_PROBABILITY_MODEL_VERSION
from .official_map_identity import (
    OfficialMapResolution,
    resolve_exact_official_map_links,
)
from .raybet_state import explicit_raybet_map_times
from .storage import LiveBettingStore
from .vision_frame_registry import VisionFrameReceipt, verify_vision_frame_receipt


REQUIRED_CONSECUTIVE_SERIES = 3
ENDED_SERIES_STATUSES = frozenset(
    {"3", "4", "5", "closed", "completed", "ended", "finished", "settled"}
)
DEFAULT_EVIDENCE_ROOT = (
    Path(__file__).resolve().parents[1] / "data" / "live_betting" / "vision_evidence"
)
_MAP_EVIDENCE_TABLES = (
    "vision_observations",
    "live_draft_mappings",
    "live_game_snapshots",
    "live_draft_prospective_predictions",
    "map_decision_checkpoints",
    "map_results",
)
_CLOSED_ODDS_STATUSES = (
    "3",
    "4",
    "5",
    "closed",
    "completed",
    "ended",
    "finished",
    "settled",
)


@dataclass(frozen=True)
class _OfficialMapEvidence:
    map_number: int
    dota_match_id: int
    official_start_time: datetime
    source: str


def audit_acceptance_progress(
    connection: PostgresSession,
    *,
    evidence_root: str | Path = DEFAULT_EVIDENCE_ROOT,
    limit: int = 10,
    verify_frame_bytes: bool = False,
) -> dict[str, object]:
    """Audit the newest watched, ended Series and calculate the current streak."""

    if not 1 <= limit <= 100:
        raise ValueError("acceptance audit limit must be between 1 and 100")
    root = Path(evidence_root).expanduser().resolve()
    match_ids = _candidate_series_ids(connection, root, limit=limit)
    series = [
        audit_series_acceptance(
            connection,
            match_id,
            evidence_root=root,
            verify_frame_bytes=verify_frame_bytes,
        )
        for match_id in match_ids
    ]
    consecutive = 0
    for item in series:
        if item["status"] != "accepted":
            break
        consecutive += 1
    failures = Counter(
        str(reason) for item in series for reason in item.get("reasons", [])
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "accepted" if consecutive >= REQUIRED_CONSECUTIVE_SERIES else "incomplete"
        ),
        "required_consecutive_series": REQUIRED_CONSECUTIVE_SERIES,
        "consecutive_accepted_series": consecutive,
        "goal_met": consecutive >= REQUIRED_CONSECUTIVE_SERIES,
        "audited_series_count": len(series),
        "failure_reason_counts": dict(sorted(failures.items())),
        "series": series,
    }


def audit_series_acceptance(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    evidence_root: str | Path = DEFAULT_EVIDENCE_ROOT,
    verify_frame_bytes: bool = False,
) -> dict[str, object]:
    """Fail closed unless one Series proves every required per-Map boundary."""

    match_id = str(raybet_match_id or "").strip()
    if not match_id:
        raise ValueError("RayBet Series ID is required")
    row = connection.execute(
        """SELECT raybet_match_id, tournament, team_one, team_two, scheduled_at,
                  best_of, status, raw_json, updated_at
             FROM raybet_matches WHERE raybet_match_id=?""",
        (match_id,),
    ).fetchone()
    if row is None:
        return {
            "raybet_match_id": match_id,
            "status": "incomplete",
            "reasons": ["raybet_series_not_found"],
            "actual_map_numbers": [],
            "official_match_ids": [],
            "maps": [],
        }

    reasons: list[str] = []
    provider_status = str(row["status"] or "").strip().casefold()
    if provider_status not in ENDED_SERIES_STATUSES:
        reasons.append("raybet_series_not_ended")
    explicit_maps = _explicit_map_numbers(row["raw_json"], row["best_of"])
    confirmed_links = _confirmed_result_links(connection, match_id)
    try:
        resolution = resolve_exact_official_map_links(connection, match_id)
    except Exception:
        resolution = OfficialMapResolution(
            "unlinked",
            "official_map_resolution_failed",
            (),
        )
    fallback_links = {
        link.map_number: _OfficialMapEvidence(
            map_number=link.map_number,
            dota_match_id=link.dota_match_id,
            official_start_time=link.official_start_time,
            source="raybet_explicit_map_time_unique",
        )
        for link in resolution.links
    }
    actual_maps = tuple(
        sorted(explicit_maps or set(confirmed_links) or set(resolution.map_numbers))
    )
    links = {**fallback_links, **confirmed_links}
    official_ids = [
        links[number].dota_match_id for number in actual_maps if number in links
    ]
    result_links_complete = bool(actual_maps) and set(actual_maps) == set(
        confirmed_links
    )
    official_status = "confirmed" if result_links_complete else resolution.status
    official_reason = (
        "confirmed_map_result" if result_links_complete else resolution.reason
    )
    if official_status != "confirmed":
        reasons.append(resolution.reason)
    if not actual_maps:
        reasons.append("actual_map_identity_unavailable")
    elif actual_maps != tuple(range(1, len(actual_maps) + 1)):
        reasons.append("actual_map_sequence_invalid")
    if len(official_ids) != len(set(official_ids)):
        reasons.append("official_match_id_reused_across_maps")

    observed_by_table = _observed_map_numbers(connection, match_id)
    observed_maps = (
        set().union(*observed_by_table.values()) if observed_by_table else set()
    )
    extra_maps = sorted(observed_maps - set(actual_maps))
    if extra_maps:
        reasons.append("non_played_map_has_production_evidence")
    market_maps = _market_map_numbers(connection, match_id)
    market_only_maps = sorted(market_maps - set(actual_maps))

    root = Path(evidence_root).expanduser().resolve()
    map_audits = [
        _audit_map(
            connection,
            match_id=match_id,
            map_number=map_number,
            official_link=links.get(map_number),
            evidence_root=root,
            verify_frame_bytes=verify_frame_bytes,
        )
        for map_number in actual_maps
    ]
    for item in map_audits:
        if item["status"] != "accepted":
            reasons.append(f"map_{item['map_number']}_incomplete")
    reasons = list(dict.fromkeys(reasons))
    return {
        "raybet_match_id": match_id,
        "tournament": str(row["tournament"] or ""),
        "team_one": str(row["team_one"] or ""),
        "team_two": str(row["team_two"] or ""),
        "scheduled_at": row["scheduled_at"],
        "updated_at": row["updated_at"],
        "provider_status": str(row["status"] or ""),
        "best_of": row["best_of"],
        "status": "accepted" if not reasons else "incomplete",
        "reasons": reasons,
        "official_identity": {
            "status": official_status,
            "reason": official_reason,
        },
        "actual_map_numbers": list(actual_maps),
        "official_match_ids": official_ids,
        "observed_map_numbers_by_table": {
            table: sorted(numbers) for table, numbers in observed_by_table.items()
        },
        "extra_production_map_numbers": extra_maps,
        "market_only_map_numbers": market_only_maps,
        "maps": map_audits,
    }


def _candidate_series_ids(
    connection: PostgresSession,
    evidence_root: Path,
    *,
    limit: int,
) -> list[str]:
    observed = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT raybet_match_id FROM vision_observations"
        ).fetchall()
        if str(row[0] or "").strip()
    }
    observed.update(_filesystem_series_ids(evidence_root))
    if not observed:
        return []
    rows = connection.execute(
        """SELECT raybet_match_id, status, scheduled_at
             FROM raybet_matches
            ORDER BY scheduled_at DESC NULLS LAST, raybet_match_id DESC"""
    ).fetchall()
    return [
        str(row["raybet_match_id"])
        for row in rows
        if str(row["raybet_match_id"]) in observed
        and str(row["status"] or "").strip().casefold() in ENDED_SERIES_STATUSES
    ][:limit]


def _explicit_map_numbers(raw_json: object, best_of: object) -> set[int]:
    if type(best_of) is not int:
        return set()
    try:
        payload = json.loads(str(raw_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    return set(explicit_raybet_map_times(payload, int(best_of)))


def _confirmed_result_links(
    connection: PostgresSession,
    match_id: str,
) -> dict[int, _OfficialMapEvidence]:
    rows = connection.execute(
        """SELECT result.map_number, result.dota_match_id, match.start_time
             FROM map_results AS result
             JOIN settlement_reconciliations AS reconciliation
               ON reconciliation.raybet_match_id=result.raybet_match_id
              AND reconciliation.map_number=result.map_number
              AND reconciliation.dota_match_id=result.dota_match_id
              AND reconciliation.status='confirmed'
             JOIN matches AS match ON match.match_id=result.dota_match_id
            WHERE result.raybet_match_id=?
            ORDER BY result.map_number""",
        (match_id,),
    ).fetchall()
    output: dict[int, _OfficialMapEvidence] = {}
    for row in rows:
        if (
            type(row["map_number"]) is not int
            or int(row["map_number"]) <= 0
            or type(row["dota_match_id"]) is not int
            or int(row["dota_match_id"]) <= 0
            or type(row["start_time"]) is not int
            or int(row["start_time"]) <= 0
        ):
            continue
        map_number = int(row["map_number"])
        output[map_number] = _OfficialMapEvidence(
            map_number=map_number,
            dota_match_id=int(row["dota_match_id"]),
            official_start_time=datetime.fromtimestamp(
                int(row["start_time"]),
                timezone.utc,
            ),
            source="confirmed_map_result",
        )
    return output


def _filesystem_series_ids(evidence_root: Path) -> set[str]:
    if not evidence_root.is_dir():
        return set()
    ids: set[str] = set()
    series_root = evidence_root / "series"
    if series_root.is_dir():
        ids.update(path.name for path in series_root.iterdir() if path.is_dir())
    suffix = ".manifest.jsonl"
    ids.update(
        path.name[: -len(suffix)]
        for path in evidence_root.glob(f"*{suffix}")
        if len(path.name) > len(suffix)
    )
    return ids


def _observed_map_numbers(
    connection: PostgresSession,
    match_id: str,
) -> dict[str, set[int]]:
    output: dict[str, set[int]] = {}
    for table in _MAP_EVIDENCE_TABLES:
        if table == "vision_observations":
            rows = connection.execute(
                """SELECT DISTINCT observation.map_number
                     FROM vision_observations AS observation
                    WHERE observation.raybet_match_id=?
                      AND observation.map_number IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                            FROM vision_observation_invalidations AS invalidation
                           WHERE invalidation.raybet_match_id=observation.raybet_match_id
                             AND invalidation.captured_at=observation.captured_at
                             AND invalidation.source_frame_ref=observation.source_frame_ref
                      )""",
                (match_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                f"""SELECT DISTINCT map_number FROM {table}
                     WHERE raybet_match_id=? AND map_number IS NOT NULL""",
                (match_id,),
            ).fetchall()
        output[table] = {
            int(row[0]) for row in rows if type(row[0]) is int and int(row[0]) > 0
        }
    return output


def _market_map_numbers(connection: PostgresSession, match_id: str) -> set[int]:
    rows = connection.execute(
        """SELECT DISTINCT period FROM odds_response_outcomes_effective
            WHERE raybet_match_id=? AND period LIKE 'map_%'""",
        (match_id,),
    ).fetchall()
    output: set[int] = set()
    for row in rows:
        period = str(row[0] or "")
        suffix = period.removeprefix("map_")
        if period.startswith("map_") and suffix.isascii() and suffix.isdigit():
            value = int(suffix)
            if value > 0:
                output.add(value)
    return output


def _audit_map(
    connection: PostgresSession,
    *,
    match_id: str,
    map_number: int,
    official_link: _OfficialMapEvidence | None,
    evidence_root: Path,
    verify_frame_bytes: bool,
) -> dict[str, object]:
    result = _result_audit(connection, match_id, map_number, official_link)
    confirmed_result_duration = (
        result.get("duration_seconds") if result.get("status") == "accepted" else None
    )
    roster = _roster_audit(connection, match_id, map_number)
    rosh = _rosh_audit(
        connection,
        match_id,
        map_number,
        roster.get("mapping_version"),
    )
    vision = _manifest_audit(
        connection,
        match_id,
        map_number,
        evidence_root=evidence_root,
        verify_frame_bytes=verify_frame_bytes,
    )
    odds = _odds_audit(
        connection,
        match_id,
        map_number,
        official_link=official_link,
        duration_seconds=confirmed_result_duration,
    )
    checkpoints = _checkpoint_audit(
        connection,
        match_id,
        map_number,
        duration_seconds=confirmed_result_duration,
        official_start_time=(
            None if official_link is None else official_link.official_start_time
        ),
        official_match_id=(
            None if official_link is None else official_link.dota_match_id
        ),
    )
    checks = {
        "result": result,
        "vision_retention": vision,
        "lineup": roster,
        "rosh": rosh,
        "odds": odds,
        "checkpoints": checkpoints,
    }
    reasons = [
        f"{name}:{check['reason']}"
        for name, check in checks.items()
        if check["status"] != "accepted"
    ]
    return {
        "map_number": map_number,
        "official_match_id": (
            None if official_link is None else official_link.dota_match_id
        ),
        "status": "accepted" if not reasons else "incomplete",
        "reasons": reasons,
        "checks": checks,
    }


def _result_audit(
    connection: PostgresSession,
    match_id: str,
    map_number: int,
    official_link: _OfficialMapEvidence | None,
) -> dict[str, object]:
    row = connection.execute(
        """SELECT result.dota_match_id, result.strict_mapping_id,
                  result.winner_side, result.duration_seconds,
                  result.raybet_evidence_id, result.opendota_evidence_id,
                  result.settled_at, reconciliation.status AS reconciliation_status,
                  reconciliation.reason AS reconciliation_reason
             FROM map_results AS result
             JOIN settlement_reconciliations AS reconciliation
               ON reconciliation.raybet_match_id=result.raybet_match_id
              AND reconciliation.map_number=result.map_number
              AND reconciliation.dota_match_id=result.dota_match_id
            WHERE result.raybet_match_id=? AND result.map_number=?""",
        (match_id, map_number),
    ).fetchone()
    if row is None:
        evidence = _official_result_evidence_audit(
            connection,
            match_id,
            map_number,
            official_link,
        )
        if evidence is not None:
            return evidence
        official_result = _official_match_result_detail(connection, official_link)
        if official_result is not None:
            return {
                "status": "incomplete",
                "reason": "official_match_result_not_settled",
                **official_result,
            }
        return {
            "status": "incomplete",
            "reason": "confirmed_map_result_missing",
            "duration_seconds": None,
        }
    reason = None
    if official_link is None:
        reason = "official_match_identity_unavailable"
    elif int(row["dota_match_id"]) != official_link.dota_match_id:
        reason = "confirmed_result_official_match_conflict"
    elif str(row["reconciliation_status"]) != "confirmed":
        reason = str(
            row["reconciliation_reason"] or "result_reconciliation_unconfirmed"
        )
    elif (
        type(row["strict_mapping_id"]) is not int
        or int(row["strict_mapping_id"]) <= 0
        or str(row["winner_side"]) not in {"team_one", "team_two"}
        or type(row["duration_seconds"]) is not int
        or int(row["duration_seconds"]) <= 0
        or type(row["raybet_evidence_id"]) is not int
        or type(row["opendota_evidence_id"]) is not int
    ):
        reason = "confirmed_map_result_incomplete"
    return {
        "status": "accepted" if reason is None else "incomplete",
        "reason": reason,
        "dota_match_id": int(row["dota_match_id"]),
        "strict_mapping_id": row["strict_mapping_id"],
        "winner_side": str(row["winner_side"]),
        "duration_seconds": row["duration_seconds"],
        "settled_at": row["settled_at"],
    }


def _official_result_evidence_audit(
    connection: PostgresSession,
    match_id: str,
    map_number: int,
    official_link: _OfficialMapEvidence | None,
) -> dict[str, object] | None:
    rows = connection.execute(
        """SELECT dota_match_id, winner_side, evidence_ref, facts_json,
                  observed_at, first_usable_at, opendota_artifact_id,
                  opendota_observation_id, opendota_content_hash
             FROM settlement_result_evidence
            WHERE raybet_match_id=? AND map_number=?
              AND source='opendota' AND status='confirmed'
            ORDER BY first_usable_at, evidence_id""",
        (match_id, map_number),
    ).fetchall()
    if not rows:
        return None
    if official_link is None:
        return {
            "status": "incomplete",
            "reason": "official_result_evidence_identity_unavailable",
            "duration_seconds": None,
            "evidence_count": len(rows),
        }
    official_detail = _official_match_result_detail(connection, official_link)
    if official_detail is None:
        return {
            "status": "incomplete",
            "reason": "official_result_evidence_identity_conflict",
            "duration_seconds": None,
            "evidence_count": len(rows),
        }
    identities: set[tuple[int, str, int]] = set()
    first_usable_values: list[datetime] = []
    evidence_refs: list[str] = []
    for row in rows:
        try:
            facts = json.loads(str(row["facts_json"]))
            observed_at = _parse_utc_timestamp(row["observed_at"])
            first_usable_at = _parse_utc_timestamp(row["first_usable_at"])
        except (TypeError, ValueError, json.JSONDecodeError):
            facts = None
            observed_at = None
            first_usable_at = None
        dota_match_id = row["dota_match_id"]
        winner_side = str(row["winner_side"] or "")
        duration = facts.get("duration_seconds") if isinstance(facts, dict) else None
        content_hash = str(row["opendota_content_hash"] or "")
        evidence_ref = str(row["evidence_ref"] or "")
        valid = (
            type(dota_match_id) is int
            and int(dota_match_id) == official_link.dota_match_id
            and winner_side in {"team_one", "team_two"}
            and type(duration) is int
            and int(duration) > 0
            and duration == official_detail["duration_seconds"]
            and isinstance(facts, dict)
            and facts.get("raybet_match_id") == match_id
            and facts.get("map_number") == map_number
            and facts.get("dota_match_id") == official_link.dota_match_id
            and facts.get("winner_side") == winner_side
            and facts.get("result_source") == "registered_opendota_match"
            and facts.get("identity_method") == "raybet_explicit_map_time_unique"
            and observed_at is not None
            and first_usable_at is not None
            and first_usable_at >= observed_at
            and bool(str(row["opendota_artifact_id"] or "").strip())
            and bool(str(row["opendota_observation_id"] or "").strip())
            and len(content_hash) == 64
            and evidence_ref
            == f"opendota:{official_link.dota_match_id}:sha256:{content_hash}"
        )
        if not valid:
            return {
                "status": "incomplete",
                "reason": "official_result_evidence_invalid",
                "duration_seconds": None,
                "evidence_count": len(rows),
            }
        identities.add((int(dota_match_id), winner_side, int(duration)))
        first_usable_values.append(first_usable_at)
        evidence_refs.append(evidence_ref)
    if len(identities) != 1:
        return {
            "status": "incomplete",
            "reason": "official_result_evidence_conflict",
            "duration_seconds": None,
            "evidence_count": len(rows),
        }
    dota_match_id, winner_side, duration_seconds = next(iter(identities))
    return {
        "status": "accepted",
        "reason": None,
        "dota_match_id": dota_match_id,
        "winner_side": winner_side,
        "duration_seconds": duration_seconds,
        "result_source": "verified_official_result_evidence",
        "result_recorded_at": min(first_usable_values).isoformat(),
        "evidence_count": len(rows),
        "evidence_refs": evidence_refs,
        "strict_mapping_id": None,
        "formal_settlement_status": "not_applicable_without_eligible_decision",
    }


def _parse_utc_timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(timezone.utc)


def _roster_audit(
    connection: PostgresSession,
    match_id: str,
    map_number: int,
) -> dict[str, object]:
    row = connection.execute(
        """WITH latest AS (
               SELECT MAX(version) AS version FROM live_draft_mappings
                WHERE raybet_match_id=? AND map_number=?
           )
           SELECT latest.version, COUNT(*) AS slot_count,
                  COUNT(*) FILTER (WHERE mapping.is_locked=1) AS locked_count,
                  COUNT(DISTINCT mapping.hero_id) AS hero_count,
                  COUNT(DISTINCT mapping.team_id) AS team_count,
                  COUNT(DISTINCT mapping.position)
                      FILTER (WHERE mapping.side='radiant') AS radiant_positions,
                  COUNT(DISTINCT mapping.position)
                      FILTER (WHERE mapping.side='dire') AS dire_positions,
                  COUNT(*) FILTER (WHERE mapping.side='radiant') AS radiant_slots,
                  COUNT(*) FILTER (WHERE mapping.side='dire') AS dire_slots,
                  MIN(mapping.created_by) AS created_by,
                  COUNT(DISTINCT mapping.created_by) AS actor_count,
                  MIN(mapping.source) AS mapping_source,
                  COUNT(DISTINCT mapping.source) AS source_count
             FROM latest
             LEFT JOIN live_draft_mappings AS mapping
               ON mapping.raybet_match_id=? AND mapping.map_number=?
              AND mapping.version=latest.version
            GROUP BY latest.version""",
        (match_id, map_number, match_id, map_number),
    ).fetchone()
    structurally_valid = row is not None and (
        type(row["version"]) is int
        and int(row["version"]) > 0
        and int(row["slot_count"]) == 10
        and int(row["locked_count"]) == 10
        and int(row["hero_count"]) == 10
        and int(row["team_count"]) == 2
        and int(row["radiant_positions"]) == 5
        and int(row["dire_positions"]) == 5
        and int(row["radiant_slots"]) == 5
        and int(row["dire_slots"]) == 5
        and int(row["actor_count"]) == 1
        and int(row["source_count"]) == 1
        and str(row["mapping_source"]) in {"manual", "manual_correction"}
        and bool(str(row["created_by"] or "").strip())
    )
    authority = (
        parse_sourced_manual_draft_authority(row["created_by"])
        if structurally_valid and row is not None
        else None
    )
    valid = structurally_valid and authority is not None
    reason = (
        None
        if valid
        else "manual_mapping_source_missing"
        if structurally_valid
        else "ten_hero_position_lock_missing"
    )
    return {
        "status": "accepted" if valid else "incomplete",
        "reason": reason,
        "mapping_version": (
            int(row["version"])
            if row is not None and type(row["version"]) is int
            else None
        ),
        "source_actor": None if authority is None else authority["actor"],
        "source_url": (
            None if authority is None else authority["evidence_source_url"]
        ),
        "source_kind": (
            None if row is None else row["mapping_source"]
        ),
    }


def _rosh_audit(
    connection: PostgresSession,
    match_id: str,
    map_number: int,
    mapping_version: object,
) -> dict[str, object]:
    if type(mapping_version) is not int:
        return {
            "status": "incomplete",
            "reason": "locked_mapping_unavailable",
        }
    row = connection.execute(
        """SELECT prediction_hash, record_status, p1_probability,
                  pure_rosh_score, standardized_rosh_score,
                  rosh_evidence_hash, missing_reason, causal_status
             FROM live_draft_prospective_predictions
            WHERE raybet_match_id=? AND map_number=? AND mapping_version=?""",
        (match_id, map_number, mapping_version),
    ).fetchone()
    valid = row is not None and (
        str(row["record_status"]) == "paired"
        and str(row["causal_status"]) == "eligible"
        and row["p1_probability"] is not None
        and row["pure_rosh_score"] is not None
        and row["standardized_rosh_score"] is not None
        and bool(str(row["rosh_evidence_hash"] or "").strip())
        and row["missing_reason"] is None
    )
    reason = (
        None
        if valid
        else str(row["missing_reason"] or "rosh_analysis_unavailable")
        if row is not None
        else "rosh_prediction_missing"
    )
    return {
        "status": "accepted" if valid else "incomplete",
        "reason": reason,
        "record_status": None if row is None else row["record_status"],
    }


def _manifest_audit(
    connection: PostgresSession,
    match_id: str,
    map_number: int,
    *,
    evidence_root: Path,
    verify_frame_bytes: bool,
) -> dict[str, object]:
    rows = connection.execute(
        """SELECT observation.captured_at, observation.source_frame_ref,
                  observation.source_frame_sha256, observation.source_frame_bytes,
                  frame.content_sha256 AS registered_sha256,
                  frame.byte_length AS registered_bytes,
                  frame.storage_path
             FROM vision_observations AS observation
             LEFT JOIN active_vision_frame_artifacts AS frame
               ON frame.frame_ref=observation.source_frame_ref
            WHERE observation.raybet_match_id=? AND observation.map_number=?
            ORDER BY live_text_timestamp_utc(observation.captured_at),
                     observation.source_frame_ref""",
        (match_id, map_number),
    ).fetchall()
    database_identities: set[tuple[str, int, str, str]] = set()
    registry_errors = 0
    unretained_samples = 0
    verified_paths: set[Path] = set()
    for row in rows:
        try:
            captured_at = _parse_utc(row["captured_at"]).isoformat()
        except ValueError:
            registry_errors += 1
            continue
        frame_ref = str(row["source_frame_ref"] or "")
        database_identities.add((match_id, map_number, captured_at, frame_ref))
        if (
            not frame_ref.startswith("vision-frame:sha256:")
            or row["source_frame_sha256"] is None
            or row["source_frame_bytes"] is None
        ):
            unretained_samples += 1
            registry_errors += 1
            continue
        if (
            row["registered_sha256"] is None
            or str(row["source_frame_sha256"] or "") != str(row["registered_sha256"])
            or row["source_frame_bytes"] != row["registered_bytes"]
            or row["storage_path"] is None
        ):
            registry_errors += 1
            continue
        path = Path(str(row["storage_path"]))
        if path in verified_paths:
            continue
        try:
            if verify_frame_bytes:
                verify_vision_frame_receipt(
                    VisionFrameReceipt(
                        frame_ref=frame_ref,
                        content_sha256=str(row["registered_sha256"]),
                        byte_length=int(row["registered_bytes"]),
                        storage_path=path,
                    )
                )
            elif not path.is_file() or path.stat().st_size != int(
                row["registered_bytes"]
            ):
                raise OSError(path)
        except (OSError, RuntimeError, TypeError, ValueError):
            registry_errors += 1
        verified_paths.add(path)

    manifest_rows = 0
    manifest_errors = 0
    manifest_identities: set[tuple[str, int, str, str]] = set()
    manifest_paths = sorted(
        (evidence_root / "series" / match_id / f"map_{map_number}").glob(
            "*/frames.jsonl"
        )
    )
    for path in manifest_paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            manifest_errors += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            manifest_rows += 1
            try:
                payload = json.loads(line)
                identity = payload["observation_identity"]
                captured_at = _parse_utc(identity["captured_at"]).isoformat()
                key = (
                    str(identity["raybet_match_id"]),
                    int(identity["map_number"]),
                    captured_at,
                    str(identity["source_frame_ref"]),
                )
                if (
                    payload.get("schema_version") != 1
                    or payload.get("source") != "raybet_hls"
                    or payload.get("raybet_match_id") != match_id
                    or payload.get("map_number") != map_number
                    or key[:2] != (match_id, map_number)
                ):
                    raise ValueError("manifest identity mismatch")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                manifest_errors += 1
                continue
            if key in manifest_identities:
                manifest_errors += 1
            manifest_identities.add(key)

    missing_manifest_rows = len(database_identities - manifest_identities)
    extra_manifest_rows = len(manifest_identities - database_identities)
    valid = (
        bool(rows)
        and registry_errors == 0
        and manifest_errors == 0
        and manifest_rows == len(database_identities)
        and missing_manifest_rows == 0
        and extra_manifest_rows == 0
    )
    reason = None
    if not rows:
        reason = "map_samples_missing"
    elif unretained_samples:
        reason = "unretained_map_sample_frame_missing"
    elif registry_errors:
        reason = "registered_frame_integrity_failed"
    elif manifest_errors:
        reason = "sample_manifest_invalid"
    elif (
        missing_manifest_rows
        or extra_manifest_rows
        or manifest_rows != len(database_identities)
    ):
        reason = "sample_manifest_database_mismatch"
    return {
        "status": "accepted" if valid else "incomplete",
        "reason": reason,
        "database_sample_count": len(database_identities),
        "manifest_sample_count": manifest_rows,
        "manifest_file_count": len(manifest_paths),
        "unretained_sample_count": unretained_samples,
        "registered_frame_error_count": registry_errors,
        "manifest_error_count": manifest_errors,
        "missing_manifest_sample_count": missing_manifest_rows,
        "extra_manifest_sample_count": extra_manifest_rows,
        "frame_bytes_verified": verify_frame_bytes,
    }


def _odds_audit(
    connection: PostgresSession,
    match_id: str,
    map_number: int,
    *,
    official_link: _OfficialMapEvidence | None,
    duration_seconds: object,
) -> dict[str, object]:
    period = f"map_{map_number}"
    status_placeholders = ", ".join("?" for _ in _CLOSED_ODDS_STATUSES)
    quote_rows = connection.execute(
        """SELECT transport.observation_key, transport.observed_at,
                    transport.processing_status,
                    COUNT(*) FILTER (
                        WHERE lower(trim(outcome.status::text))
                              IN ('1', 'open', 'active', 'running')
                    )=2 AS open_complete
              FROM odds_response_outcomes_effective AS outcome
              JOIN odds_transport_observations AS transport
                ON transport.observation_key=outcome.observation_key
               AND transport.raybet_match_id=outcome.raybet_match_id
             WHERE outcome.raybet_match_id=? AND outcome.period=?
               AND outcome.storage_version='v2'
               AND outcome.market_type='winner' AND outcome.supported=1
               AND outcome.price>1.0
               AND transport.source='direct'
               AND transport.timing_status='on_time'
               AND transport.processing_status IN ('audit_only', 'processed')
             GROUP BY transport.observation_key, transport.observed_at,
                      transport.processing_status,
                      outcome.odds_group_id, outcome.response_state_hash,
                      outcome.response_artifact_hash
            HAVING COUNT(*)=2 AND COUNT(DISTINCT outcome.side)=2
               AND COUNT(*) FILTER (WHERE outcome.side='team_one')=1
               AND COUNT(*) FILTER (WHERE outcome.side='team_two')=1
               AND COUNT(*) FILTER (WHERE outcome.outcome_key=outcome.side)=2""",
        (match_id, period),
    ).fetchall()
    pregame = live = closing = 0
    pregame_processing_statuses: Counter[str] = Counter()
    live_quotes: list[tuple[datetime, str]] = []
    closure_evidence_count = 0
    closure_first_observed_at: datetime | None = None
    duration_source = None
    if type(duration_seconds) is int and int(duration_seconds) > 0:
        duration = int(duration_seconds)
        duration_source = "confirmed_map_result"
    else:
        duration = _official_match_duration_seconds(connection, official_link)
        if duration is not None:
            duration_source = "official_match_detail"
    if official_link is not None and duration is not None:
        started_at = official_link.official_start_time
        ended_at = started_at + timedelta(seconds=duration)
        for row in quote_rows:
            try:
                observed_at = _parse_utc(row[1])
            except ValueError:
                continue
            processing_status = str(row[2])
            if not bool(row[3]):
                continue
            if observed_at < started_at:
                pregame += 1
                pregame_processing_statuses[processing_status] += 1
            elif processing_status == "processed" and observed_at <= ended_at:
                live += 1
                live_quotes.append((observed_at, str(row[0])))
        closure_row = connection.execute(
            f"""SELECT COUNT(DISTINCT transport.observation_key)
                          AS closure_observation_count,
                        MIN(live_text_timestamp_utc(transport.observed_at))
                          AS first_closure_observed_at
                   FROM odds_response_outcomes_effective AS outcome
                   JOIN odds_transport_observations AS transport
                     ON transport.observation_key=outcome.observation_key
                    AND transport.raybet_match_id=outcome.raybet_match_id
                  WHERE outcome.raybet_match_id=? AND outcome.period=?
                    AND outcome.storage_version='v2'
                    AND outcome.market_type='winner' AND outcome.supported=1
                    AND outcome.side IN ('team_one', 'team_two')
                    AND outcome.outcome_key=outcome.side
                    AND lower(trim(outcome.status::text)) IN ({status_placeholders})
                    AND transport.source='direct'
                    AND transport.timing_status='on_time'
                    AND transport.processing_status='processed'
                    AND live_text_timestamp_utc(transport.observed_at)>=
                        CAST(? AS timestamptz)""",
            (match_id, period, *_CLOSED_ODDS_STATUSES, ended_at.isoformat()),
        ).fetchone()
        if closure_row is not None:
            closure_evidence_count = int(closure_row[0] or 0)
            if closure_row[1] is not None:
                try:
                    closure_first_observed_at = _parse_utc(closure_row[1])
                except ValueError:
                    closure_evidence_count = 0
        closing = int(bool(live_quotes) and closure_evidence_count > 0)
    closing_quote = max(live_quotes, default=None)
    closing_age_before_end = (
        None
        if closing_quote is None or official_link is None or duration is None
        else max(
            0.0,
            (
                official_link.official_start_time
                + timedelta(seconds=duration)
                - closing_quote[0]
            ).total_seconds(),
        )
    )
    valid = pregame > 0 and live > 0 and closing > 0
    missing = []
    if pregame == 0:
        missing.append("pregame")
    if live == 0:
        missing.append("live")
    if closing == 0:
        missing.append("closing")
    return {
        "status": "accepted" if valid else "incomplete",
        "reason": None if valid else "missing_odds_phase:" + ",".join(missing),
        "source": "raybet_direct",
        "pregame_complete_observation_count": pregame,
        "pregame_processing_statuses": dict(sorted(pregame_processing_statuses.items())),
        "live_complete_observation_count": live,
        "closing_complete_observation_count": closing,
        "closing_quote_observation_key": None if closing_quote is None else closing_quote[1],
        "closing_quote_observed_at": (
            None if closing_quote is None else closing_quote[0].isoformat()
        ),
        "closing_age_before_end_seconds": closing_age_before_end,
        "closure_evidence_observation_count": closure_evidence_count,
        "closure_first_observed_at": (
            None
            if closure_first_observed_at is None
            else closure_first_observed_at.isoformat()
        ),
        "closing_definition": (
            "last_complete_processed_open_quote_with_post_end_closed_status"
        ),
        "duration_source": duration_source,
    }


def _official_match_duration_seconds(
    connection: PostgresSession,
    official_link: _OfficialMapEvidence | None,
) -> int | None:
    if official_link is None:
        return None
    row = connection.execute(
        "SELECT start_time, duration FROM matches WHERE match_id=?",
        (official_link.dota_match_id,),
    ).fetchone()
    if (
        row is None
        or type(row[0]) is not int
        or type(row[1]) is not int
        or int(row[0]) <= 0
        or int(row[1]) <= 0
    ):
        return None
    official_start = int(official_link.official_start_time.timestamp())
    if abs(int(row[0]) - official_start) > 1:
        return None
    return int(row[1])


def _official_match_result_detail(
    connection: PostgresSession,
    official_link: _OfficialMapEvidence | None,
) -> dict[str, object] | None:
    if official_link is None:
        return None
    row = connection.execute(
        "SELECT start_time, duration, radiant_win FROM matches WHERE match_id=?",
        (official_link.dota_match_id,),
    ).fetchone()
    if (
        row is None
        or type(row[0]) is not int
        or type(row[1]) is not int
        or row[2] not in {True, False, 0, 1}
        or int(row[0]) <= 0
        or int(row[1]) <= 0
    ):
        return None
    official_start = int(official_link.official_start_time.timestamp())
    if abs(int(row[0]) - official_start) > 1:
        return None
    return {
        "dota_match_id": official_link.dota_match_id,
        "result_source": "official_match_detail",
        "radiant_win": bool(row[2]),
        "duration_seconds": int(row[1]),
    }


def _checkpoint_audit(
    connection: PostgresSession,
    match_id: str,
    map_number: int,
    *,
    duration_seconds: object,
    official_start_time: object,
    official_match_id: object,
) -> dict[str, object]:
    valid_mapping_versions = {
        int(row[0])
        for row in connection.execute(
            """SELECT version FROM live_draft_mappings
                WHERE raybet_match_id=? AND map_number=?
                GROUP BY version
               HAVING COUNT(*)=10
                  AND COUNT(*) FILTER (WHERE is_locked=1)=10
                  AND COUNT(DISTINCT hero_id)=10
                  AND COUNT(DISTINCT team_id)=2
                  AND COUNT(*) FILTER (WHERE side='radiant')=5
                  AND COUNT(*) FILTER (WHERE side='dire')=5
                  AND COUNT(DISTINCT position)
                      FILTER (WHERE side='radiant')=5
                  AND COUNT(DISTINCT position)
                      FILTER (WHERE side='dire')=5""",
            (match_id, map_number),
        ).fetchall()
        if type(row[0]) is int
    }
    rows = connection.execute(
        """SELECT checkpoint.*, settlement.settlement_id,
                  settlement.raybet_match_id AS settlement_raybet_match_id,
                  settlement.map_number AS settlement_map_number,
                  settlement.dota_match_id AS settlement_dota_match_id,
                  settlement.outcome, settlement.profit_units,
                  settlement.result_source,
                  snapshot.raybet_match_id AS snapshot_raybet_match_id,
                  snapshot.map_number AS snapshot_map_number,
                  snapshot.captured_at AS snapshot_captured_at,
                  snapshot.game_time_seconds AS snapshot_game_time_seconds,
                  snapshot.networth_lead AS snapshot_networth_lead,
                  snapshot.radiant_kills AS snapshot_radiant_kills,
                  snapshot.dire_kills AS snapshot_dire_kills,
                  snapshot.screenshot_path AS snapshot_source_frame_ref
             FROM map_decision_checkpoints AS checkpoint
             LEFT JOIN map_decision_checkpoint_settlements AS settlement
               ON settlement.checkpoint_id=checkpoint.checkpoint_id
             LEFT JOIN live_game_snapshots AS snapshot
               ON snapshot.snapshot_id=checkpoint.vision_snapshot_id
            WHERE checkpoint.raybet_match_id=? AND checkpoint.map_number=?
            ORDER BY checkpoint.checkpoint_minute, checkpoint.checkpoint_id""",
        (match_id, map_number),
    ).fetchall()
    expected_live = (
        list(range(5, int(duration_seconds) // 60 + 1, 5))
        if type(duration_seconds) is int and int(duration_seconds) > 0
        else []
    )
    pregame_rows = [row for row in rows if str(row["phase"]) == "pregame"]
    live_rows = [row for row in rows if str(row["phase"]) == "live"]
    actual_live = [int(row["checkpoint_minute"]) for row in live_rows]
    row_errors = 0
    for row in rows:
        try:
            versions = json.loads(str(row["input_versions_json"]))
            features = json.loads(str(row["feature_availability_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            row_errors += 1
            continue
        phase = str(row["phase"])
        decision = str(row["decision"])
        row_mapping_version = row["mapping_version"]
        valid = (
            isinstance(versions, dict)
            and isinstance(features, dict)
            and versions.get("strategy_version") == STRATEGY_VERSION
            and type(row_mapping_version) is int
            and row_mapping_version in valid_mapping_versions
            and versions.get("mapping_version") == row_mapping_version
            and versions.get("odds_authority") == "trusted_odds_winner_market_authority"
            and versions.get("vision_authority")
            == "trusted_vision_observation_authority"
            and str(row["strategy_version"]) == STRATEGY_VERSION
            and float(row["assumed_stake_units"]) == 1.0
            and row["settlement_id"] is not None
            and str(row["settlement_raybet_match_id"] or "") == match_id
            and row["settlement_map_number"] == map_number
            and type(official_match_id) is int
            and row["settlement_dota_match_id"] == official_match_id
            and str(row["result_source"]) == "confirmed_map_result"
            and bool(str(row["reason"] or "").strip())
            and checkpoint_evaluation_eligibility(
                row["decided_at"],
                "3",
                official_start_time,
                duration_seconds,
            )[0]
        )
        if phase == "pregame":
            valid = valid and (
                int(row["checkpoint_minute"]) == 0
                and float(row["odds_max_age_seconds"]) == PREGAME_ODDS_MAX_AGE_SECONDS
                and row["vision_max_age_seconds"] is None
                and row["odds_vision_gap_max_seconds"] is None
            )
        elif phase == "live":
            valid = valid and (
                int(row["checkpoint_minute"]) >= 5
                and int(row["checkpoint_minute"]) % 5 == 0
                and float(row["odds_max_age_seconds"]) == LIVE_ODDS_MAX_AGE_SECONDS
                and float(row["vision_max_age_seconds"]) == LIVE_VISION_MAX_AGE_SECONDS
                and float(row["odds_vision_gap_max_seconds"])
                == LIVE_ODDS_VISION_GAP_MAX_SECONDS
                and row["vision_replay"] is False
                and isinstance(features.get("levels"), dict)
                and isinstance(features.get("objectives"), dict)
            )
            if bool(row["vision_trusted"]):
                valid = valid and all(
                    row[field] is not None
                    for field in (
                        "vision_snapshot_id",
                        "vision_source_frame_ref",
                        "vision_captured_at",
                        "vision_game_time_seconds",
                        "vision_networth_lead",
                        "vision_radiant_kills",
                        "vision_dire_kills",
                    )
                )
                valid = valid and (
                    str(row["snapshot_raybet_match_id"] or "") == match_id
                    and row["snapshot_map_number"] == map_number
                    and row["snapshot_captured_at"] == row["vision_captured_at"]
                    and row["snapshot_game_time_seconds"]
                    == row["vision_game_time_seconds"]
                    and row["snapshot_networth_lead"] == row["vision_networth_lead"]
                    and row["snapshot_radiant_kills"] == row["vision_radiant_kills"]
                    and row["snapshot_dire_kills"] == row["vision_dire_kills"]
                    and row["snapshot_source_frame_ref"]
                    == row["vision_source_frame_ref"]
                )
            else:
                valid = valid and decision == "skip"
        else:
            valid = False
        if decision != "skip":
            valid = valid and (
                row["odds_observation_key"] is not None
                and row["observed_price"] is not None
                and row["model_probability_team_one"] is not None
                and row["model_probability_team_two"] is not None
                and row["market_probability_team_one"] is not None
                and row["market_probability_team_two"] is not None
                and row["selected_edge"] is not None
                and float(row["selected_edge"]) >= MINIMUM_EDGE
            )
        valid = valid and _checkpoint_trace_is_consistent(
            dict(row),
            phase=phase,
            decision=decision,
            reason=str(row["reason"]),
            features=features,
        )
        if not valid:
            row_errors += 1
    missing_live = sorted(set(expected_live) - set(actual_live))
    extra_live = sorted(set(actual_live) - set(expected_live))
    valid = len(pregame_rows) == 1 and actual_live == expected_live and row_errors == 0
    reason = None
    if len(pregame_rows) != 1:
        reason = "pregame_checkpoint_missing"
    elif missing_live:
        reason = "live_checkpoint_missing"
    elif extra_live or actual_live != sorted(set(actual_live)):
        reason = "live_checkpoint_schedule_invalid"
    elif row_errors:
        reason = "checkpoint_trace_or_settlement_invalid"
    return {
        "status": "accepted" if valid else "incomplete",
        "reason": reason,
        "expected_live_minutes": expected_live,
        "actual_live_minutes": actual_live,
        "missing_live_minutes": missing_live,
        "extra_live_minutes": extra_live,
        "pregame_checkpoint_count": len(pregame_rows),
        "checkpoint_count": len(rows),
        "settled_checkpoint_count": sum(
            row["settlement_id"] is not None for row in rows
        ),
        "invalid_checkpoint_count": row_errors,
    }


def _checkpoint_trace_is_consistent(
    row: Mapping[str, object],
    *,
    phase: str,
    decision: str,
    reason: str,
    features: Mapping[str, object],
) -> bool:
    odds_age = row.get("odds_age_seconds")
    odds_max = float(row["odds_max_age_seconds"])
    odds_source_fields = (
        "odds_observation_key",
        "odds_group_id",
        "odds_observed_at",
        "odds_age_seconds",
        "market_probability_team_one",
        "market_probability_team_two",
    )
    odds_available = all(row.get(field) is not None for field in odds_source_fields)
    odds_payload_missing = all(row.get(field) is None for field in odds_source_fields)
    odds_fresh = odds_available and float(odds_age) <= odds_max
    model_available = (
        row.get("model_probability_team_one") is not None
        and row.get("model_probability_team_two") is not None
    )
    vision_source_fields = (
        "vision_snapshot_id",
        "vision_source_frame_ref",
        "vision_captured_at",
        "vision_game_time_seconds",
        "vision_networth_lead",
        "vision_radiant_kills",
        "vision_dire_kills",
        "vision_age_seconds",
    )
    vision_payload_missing = (
        row.get("vision_trusted") is False
        and row.get("vision_replay") is False
        and all(row.get(field) is None for field in vision_source_fields)
    )

    if phase == "pregame":
        if not vision_payload_missing:
            return False
        if decision != "skip":
            return (
                reason == "minimum_edge_met"
                and odds_fresh
                and model_available
                and row.get("selected_edge") is not None
                and float(row["selected_edge"]) >= MINIMUM_EDGE
            )
        if reason == "pregame_odds_unavailable":
            return model_available and odds_payload_missing
        if reason == "pregame_odds_stale":
            return model_available and odds_available and float(odds_age) > odds_max
        if reason == "edge_below_threshold":
            return (
                model_available
                and odds_fresh
                and row.get("selected_edge") is not None
                and float(row["selected_edge"]) < MINIMUM_EDGE
            )
        if reason == "pregame_authority_after_map_start":
            authority = features.get("pregame_authority")
            if not isinstance(authority, dict):
                return False
            authority_clock = authority.get("game_clock_seconds")
            return (
                row.get("selected_edge") is None
                and (
                    authority.get("draft_state_marker") == "in_game"
                    or (type(authority_clock) is int and authority_clock >= 0)
                )
            )
        return not model_available and row.get("selected_edge") is None

    if phase != "live":
        return False
    vision_trusted = bool(row.get("vision_trusted"))
    vision_age = row.get("vision_age_seconds")
    vision_max = float(row["vision_max_age_seconds"])
    vision_fresh = (
        vision_trusted and vision_age is not None and float(vision_age) <= vision_max
    )
    kills_available = (
        row.get("vision_radiant_kills") is not None
        and row.get("vision_dire_kills") is not None
    )
    direction_available = features.get("vision_direction") is True
    gap = row.get("odds_vision_gap_seconds")
    gap_max = float(row["odds_vision_gap_max_seconds"])
    gap_within_limit = gap is not None and float(gap) <= gap_max
    live_model_feature = features.get("live_probability_model")
    live_model_available = (
        isinstance(live_model_feature, Mapping)
        and live_model_feature.get("available") is True
        and live_model_feature.get("model_version")
        == LIVE_PROBABILITY_MODEL_VERSION
    )

    if decision != "skip":
        return (
            reason == "minimum_edge_met"
            and vision_fresh
            and kills_available
            and direction_available
            and odds_fresh
            and gap_within_limit
            and model_available
            and live_model_available
            and row.get("selected_edge") is not None
            and float(row["selected_edge"]) >= MINIMUM_EDGE
        )

    if reason == "trusted_vision_checkpoint_missing":
        return vision_payload_missing
    if reason == "live_vision_stale":
        return (
            vision_trusted and vision_age is not None and float(vision_age) > vision_max
        )
    if reason == "live_kills_unavailable":
        return vision_fresh and not kills_available
    if reason == "live_team_direction_unavailable":
        return vision_fresh and kills_available and not direction_available
    if reason == "live_odds_unavailable":
        return (
            vision_fresh
            and kills_available
            and direction_available
            and odds_payload_missing
        )
    if reason == "live_odds_stale":
        return (
            vision_fresh
            and kills_available
            and direction_available
            and odds_available
            and float(odds_age) > odds_max
        )
    if reason == "live_odds_vision_gap_exceeded":
        return (
            vision_fresh
            and kills_available
            and direction_available
            and odds_fresh
            and gap is not None
            and float(gap) > gap_max
        )
    if reason == "pregame_probability_unavailable":
        return (
            vision_fresh
            and kills_available
            and direction_available
            and odds_fresh
            and gap_within_limit
            and not model_available
        )
    if reason == "live_probability_checkpoint_minute_not_validated":
        return (
            vision_fresh
            and kills_available
            and direction_available
            and odds_fresh
            and gap_within_limit
            and not model_available
            and isinstance(live_model_feature, Mapping)
            and live_model_feature.get("available") is False
            and live_model_feature.get("reason") == reason
        )
    if reason == "edge_below_threshold":
        return (
            vision_fresh
            and kills_available
            and direction_available
            and odds_fresh
            and gap_within_limit
            and model_available
            and live_model_available
            and row.get("selected_edge") is not None
            and float(row["selected_edge"]) < MINIMUM_EDGE
        )
    return False


def _parse_utc(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError("timestamp is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--verify-frame-bytes", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with LiveBettingStore(args.database_url) as store:
        report = audit_acceptance_progress(
            store.connection,
            evidence_root=args.evidence_root,
            limit=args.limit,
            verify_frame_bytes=args.verify_frame_bytes,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return int(args.require_complete and not report["goal_met"])


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_EVIDENCE_ROOT",
    "REQUIRED_CONSECUTIVE_SERIES",
    "audit_acceptance_progress",
    "audit_series_acceptance",
]
