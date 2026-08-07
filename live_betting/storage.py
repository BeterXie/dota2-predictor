"""PostgreSQL persistence for live collection and shadow orders."""

from __future__ import annotations

import hashlib
import gzip
import json
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database.engine import build_engine
from database.session import DatabaseResult, DatabaseRow, PostgresSession

from event_intelligence.raw_archive import (
    ArtifactReceipt,
    RawArchive,
    canonical_json_value_bytes,
    schema_fingerprint,
)

from .draft_authority import (
    DraftLandmarkAuthority,
    authority_from_row,
    draft_landmark_authority_matches,
)
from .models import (
    LiveEvent,
    LiveFrame,
    Market,
    OddsSnapshot,
    ProviderMatch,
    RoshLineupScore,
    ShadowOrder,
)
from .live_match_state import append_live_game_snapshot
from .odds_response_authority import (
    canonical_state_outcomes,
    response_artifact_identity as canonical_response_artifact_identity,
    response_state_identity as canonical_response_state_identity,
    snapshot_derived_payload,
)
from .pricing import market_key
from .sanitize import (
    PUBLIC_STREAM_EVIDENCE_KEY,
    public_stream_evidence,
    sanitize_raybet_payload,
)
from .strategy import attempt_fill, is_open
from .strategy_contract import (
    OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION,
    REGISTERED_STRATEGY_CONTRACTS,
    canonical_bytes,
    parse_decision_payload,
    serialize_decision_payload,
    validate_official_rosh_strategy_contract,
    validate_strategy_contract,
)
from .strict_eligibility import (
    RAYBET_MATCH_NON_HEAD_TO_HEAD,
    classify_raybet_match_format,
    strict_raybet_head_to_head_teams,
)
from .vision_frame_registry import (
    VisionFrameReceipt,
    register_vision_frame_artifact,
    verify_bound_order_vision_frame,
    verify_registered_vision_frame,
)


CURRENT_SCHEMA_VERSION = 12
ALEMBIC_HEAD = "20260807_0032"
VISION_DRAFT_CONFLICT_REASON = "confirmed_draft_conflict"
ROSH_LINEUP_CACHE_TTL = timedelta(minutes=15)
ROSH_FETCH_MAX_DURATION = timedelta(minutes=10)

_SCORED_DECISION_CONTRIBUTION_KEYS = frozenset(
    {
        "team_style",
        "player_form",
        "draft_curve",
        "lineup_rosh",
        "late_game_style",
        "market_movement",
    }
)

_DIRECT_RESPONSE_ENDPOINTS = {
    "live_match_list": "https://raybet.local/v2/match/live",
    "completed_match_list": "https://raybet.local/v2/match/completed",
    "live_odds": "https://raybet.local/v2/odds",
    "completed_odds": "https://raybet.local/v2/odds",
    "final_odds": "https://raybet.local/v2/odds",
}


_DRAFT_AUTHORITY_COLUMNS = (
    "draft_curve_key",
    "draft_source_ref",
    "draft_landmark_key",
    "draft_landmark_horizon_minutes",
    "draft_landmark_target",
    "draft_landmark_radiant_probability",
    "draft_landmark_quality",
    "draft_landmark_uncertainty",
    "draft_landmark_support",
    "draft_radiant_team_side",
    "draft_strict_mapping_id",
    "draft_deployment_key",
    "draft_target_snapshot_hash",
    "draft_feature_hash",
    "draft_model_hash",
    "draft_calibration_hash",
    "draft_model_version",
    "draft_global_gate_ref",
    "draft_input_snapshot_hash",
    "draft_authority_revision",
    "draft_dependency_revision",
)

_VISION_AUTHORITY_COLUMNS = (
    "vision_raybet_match_id",
    "vision_map_number",
    "vision_captured_at",
    "vision_source_frame_ref",
    "vision_source_frame_sha256",
    "vision_source_frame_bytes",
    "vision_observed_game_clock_seconds",
    "vision_aligned_game_clock_seconds",
    "vision_is_paused",
    "vision_radiant_hero_ids_json",
    "vision_dire_hero_ids_json",
    "vision_radiant_team_side",
    "vision_clock_confidence",
    "vision_draft_confidence",
    "vision_screen_state",
    "vision_confirmed",
    "vision_transport_key",
    "vision_transport_at",
    "vision_alignment_method",
    "vision_alignment_lag_seconds",
)

@dataclass(frozen=True)
class VisionDecisionAuthority:
    raybet_match_id: str
    map_number: int
    captured_at: str
    source_frame_ref: str
    source_frame_sha256: str
    source_frame_bytes: int
    observed_game_clock_seconds: int
    aligned_game_clock_seconds: int
    is_paused: int
    radiant_hero_ids_json: str
    dire_hero_ids_json: str
    radiant_team_side: str
    clock_confidence: float
    draft_confidence: float
    screen_state: str
    confirmed: int
    transport_key: str
    transport_at: str
    alignment_method: str
    alignment_lag_seconds: float


def _vision_authority_values(
    authority: VisionDecisionAuthority | None,
) -> tuple[object | None, ...]:
    if authority is None:
        return (None,) * len(_VISION_AUTHORITY_COLUMNS)
    return (
        authority.raybet_match_id,
        authority.map_number,
        authority.captured_at,
        authority.source_frame_ref,
        authority.source_frame_sha256,
        authority.source_frame_bytes,
        authority.observed_game_clock_seconds,
        authority.aligned_game_clock_seconds,
        authority.is_paused,
        authority.radiant_hero_ids_json,
        authority.dire_hero_ids_json,
        authority.radiant_team_side,
        authority.clock_confidence,
        authority.draft_confidence,
        authority.screen_state,
        authority.confirmed,
        authority.transport_key,
        authority.transport_at,
        authority.alignment_method,
        authority.alignment_lag_seconds,
    )


def _draft_authority_values(
    authority: DraftLandmarkAuthority | None,
) -> tuple[object | None, ...]:
    if authority is None:
        return (None,) * len(_DRAFT_AUTHORITY_COLUMNS)
    return (
        authority.curve_key,
        authority.source_ref,
        authority.landmark_key,
        authority.horizon_minutes,
        authority.target,
        authority.radiant_probability,
        authority.quality,
        authority.uncertainty,
        authority.support,
        authority.radiant_team_side,
        authority.strict_mapping_id,
        authority.deployment_key,
        authority.target_snapshot_hash,
        authority.feature_hash,
        authority.model_hash,
        authority.calibration_hash,
        authority.model_version,
        authority.global_gate_ref,
        authority.input_snapshot_hash,
        authority.authority_revision,
        authority.dependency_revision,
    )


def _has_scored_decision_contributions(decision: Any) -> bool:
    contributions = getattr(decision, "contributions", None)
    if not isinstance(contributions, Mapping):
        return False
    scored = {
        key: value
        for key, value in contributions.items()
        if not str(key).startswith("__")
    }
    if set(scored) != _SCORED_DECISION_CONTRIBUTION_KEYS:
        return False
    values = tuple(scored.values())
    return (
        all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            for value in values
        )
        and float(scored["draft_curve"]) == 0.0
    )


def _decision_contract(decision: Any) -> Any | None:
    inputs = getattr(decision, "inputs", None)
    if not isinstance(inputs, Mapping):
        contributions = getattr(decision, "contributions", None)
        inputs = (
            contributions.get("__inputs__")
            if isinstance(contributions, Mapping)
            else None
        )
    return inputs.get("strategy_contract") if isinstance(inputs, Mapping) else None


def _contract_identity(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, Mapping):
        return None
    fields = ("evaluator_hash", "policy_hash", "serialization_version")
    if any(not isinstance(value.get(field), str) for field in fields):
        return None
    return tuple(str(value[field]) for field in fields)  # type: ignore[return-value]


def _load_odds_raw_artifact(
    connection: PostgresSession,
    raw_archive_root: Path,
    artifact_hash: str,
) -> Any:
    row = connection.execute(
        """SELECT storage_path, uncompressed_bytes
             FROM odds_raw_artifacts WHERE artifact_hash=?""",
        (artifact_hash,),
    ).fetchone()
    if row is None:
        raise RuntimeError("response raw artifact metadata is missing")
    relative_path = Path(str(row["storage_path"]))
    if relative_path.is_absolute():
        raise RuntimeError("response raw artifact path must be relative")
    path = (raw_archive_root / relative_path).resolve()
    try:
        path.relative_to(raw_archive_root)
    except ValueError as error:
        raise RuntimeError("response raw artifact path escapes archive root") from error
    if path.name != f"{artifact_hash}.json.gz":
        raise RuntimeError("response raw artifact path is invalid")
    try:
        RawArchive._verify(path, artifact_hash)
        payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("response raw artifact is corrupt") from error
    encoded = canonical_json_value_bytes(payload)
    if len(encoded) != int(row["uncompressed_bytes"]):
        raise RuntimeError("response raw artifact byte count mismatch")
    return payload


def read_browser_event_payload(
    connection: PostgresSession,
    raw_archive_root: str | Path,
    event_id: str,
) -> dict[str, Any]:
    """Load one browser event through external-v2 or legacy-inline storage."""
    row = connection.execute(
        """SELECT payload_storage, payload_artifact_hash, payload_json,
                  payload_hash, payload_bytes
             FROM browser_events WHERE event_id=?""",
        (event_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("browser event is missing")
    if str(row["payload_storage"]) == "external":
        if row["payload_artifact_hash"] is None:
            raise RuntimeError("browser event artifact reference is missing")
        payload = _load_odds_raw_artifact(
            connection,
            Path(raw_archive_root).resolve(),
            str(row["payload_artifact_hash"]),
        )
    else:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("legacy browser payload is invalid") from error
    if not isinstance(payload, dict):
        raise RuntimeError("browser payload is not an object")
    from .browser_contract import canonical_json, payload_sha256

    if payload_sha256(payload) != str(row["payload_hash"]):
        raise RuntimeError("browser payload hash mismatch")
    if len(canonical_json(payload)) != int(row["payload_bytes"]):
        raise RuntimeError("browser payload byte count mismatch")
    return payload


def _valid_confirmed_vision_payload(
    radiant_hero_ids: object,
    dire_hero_ids: object,
    source_frame_ref: object,
) -> bool:
    """Validate the immutable inputs required for a confirmed draft frame."""
    if not isinstance(radiant_hero_ids, (list, tuple)):
        return False
    if not isinstance(dire_hero_ids, (list, tuple)):
        return False
    if not isinstance(source_frame_ref, str) or not source_frame_ref.strip():
        return False
    heroes = tuple(radiant_hero_ids) + tuple(dire_hero_ids)
    return (
        len(radiant_hero_ids) == 5
        and len(dire_hero_ids) == 5
        and all(type(hero_id) is int and hero_id > 0 for hero_id in heroes)
        and len(set(heroes)) == 10
    )


def _vision_hero_ids(value: object) -> tuple[int, ...] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list):
        return None
    heroes = tuple(parsed)
    if not all(type(hero_id) is int and hero_id > 0 for hero_id in heroes):
        return None
    return heroes


def _trusted_vision_row(row: DatabaseRow) -> bool:
    radiant = _vision_hero_ids(row["radiant_hero_ids"])
    dire = _vision_hero_ids(row["dire_hero_ids"])
    if radiant is None or dire is None:
        return False
    heroes = radiant + dire
    return (
        int(row["confirmed"]) == 1
        and type(row["map_number"]) is int
        and int(row["map_number"]) > 0
        and type(row["game_clock_seconds"]) is int
        and int(row["game_clock_seconds"]) >= 0
        and type(row["is_paused"]) is int
        and int(row["is_paused"]) == 0
        and str(row["screen_state"]) == "game"
        and str(row["source_frame_ref"]).strip() != ""
        and isinstance(row["source_frame_sha256"], str)
        and len(str(row["source_frame_sha256"])) == 64
        and str(row["source_frame_sha256"]).casefold()
        == str(row["source_frame_sha256"])
        and all(
            character in "0123456789abcdef"
            for character in str(row["source_frame_sha256"])
        )
        and type(row["source_frame_bytes"]) is int
        and int(row["source_frame_bytes"]) > 0
        and str(row["radiant_team_side"]) in {"team_one", "team_two"}
        and isinstance(row["clock_confidence"], (int, float))
        and not isinstance(row["clock_confidence"], bool)
        and 0.9 <= float(row["clock_confidence"]) <= 1.0
        and isinstance(row["draft_confidence"], (int, float))
        and not isinstance(row["draft_confidence"], bool)
        and 0.9 <= float(row["draft_confidence"]) <= 1.0
        and len(radiant) == 5
        and len(dire) == 5
        and len(set(heroes)) == 10
    )


def _vision_authority_from_decision_row(
    row: DatabaseRow,
) -> VisionDecisionAuthority | None:
    if any(row[name] is None for name in _VISION_AUTHORITY_COLUMNS):
        return None
    try:
        return VisionDecisionAuthority(
            raybet_match_id=str(row["vision_raybet_match_id"]),
            map_number=int(row["vision_map_number"]),
            captured_at=str(row["vision_captured_at"]),
            source_frame_ref=str(row["vision_source_frame_ref"]),
            source_frame_sha256=str(row["vision_source_frame_sha256"]),
            source_frame_bytes=int(row["vision_source_frame_bytes"]),
            observed_game_clock_seconds=int(
                row["vision_observed_game_clock_seconds"]
            ),
            aligned_game_clock_seconds=int(
                row["vision_aligned_game_clock_seconds"]
            ),
            is_paused=int(row["vision_is_paused"]),
            radiant_hero_ids_json=str(row["vision_radiant_hero_ids_json"]),
            dire_hero_ids_json=str(row["vision_dire_hero_ids_json"]),
            radiant_team_side=str(row["vision_radiant_team_side"]),
            clock_confidence=float(row["vision_clock_confidence"]),
            draft_confidence=float(row["vision_draft_confidence"]),
            screen_state=str(row["vision_screen_state"]),
            confirmed=int(row["vision_confirmed"]),
            transport_key=str(row["vision_transport_key"]),
            transport_at=str(row["vision_transport_at"]),
            alignment_method=str(row["vision_alignment_method"]),
            alignment_lag_seconds=float(row["vision_alignment_lag_seconds"]),
        )
    except (TypeError, ValueError):
        return None


def strict_mapping_context_block_reason(
    connection: PostgresSession,
    *,
    strict_mapping_id: int,
    raybet_match_id: str,
    map_number: int,
    signal_transport_at: datetime | str,
) -> str | None:
    """Return a stable gate code for one causally bound strict mapping."""
    try:
        transport_at = (
            signal_transport_at
            if isinstance(signal_transport_at, datetime)
            else datetime.fromisoformat(str(signal_transport_at).replace("Z", "+00:00"))
        )
        if transport_at.tzinfo is None or transport_at.utcoffset() is None:
            return "strict_mapping_unverified"
        transport_at = transport_at.astimezone(timezone.utc)
        from .strict_eligibility import query_strict_live_eligibility

        eligibility = query_strict_live_eligibility(
            connection,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            transport_observed_at=transport_at,
        )
    except (SQLAlchemyError, TypeError, ValueError, OverflowError):
        return "strict_mapping_gate_unavailable"
    if not eligibility.eligible:
        if eligibility.reason in {
            "mapping_invalidated",
            "automatic_exact_approval_invalidated",
        }:
            return "strict_mapping_invalidated"
        if eligibility.reason.endswith("_schema_missing"):
            return "strict_mapping_gate_unavailable"
        return "strict_mapping_unverified"
    if (
        eligibility.mapping is None
        or eligibility.mapping.mapping_id != strict_mapping_id
    ):
        return "strict_mapping_unverified"
    return None


def strict_order_mapping_block_reason(
    connection: PostgresSession,
    order_key: str,
    *,
    require_order: bool = False,
) -> str | None:
    """Apply the shared strict gate to a persisted order's causal inputs."""
    try:
        order = connection.execute(
            """SELECT strict_mapping_id, raybet_match_id, signal_transport_at
                 FROM shadow_orders WHERE order_key=?""",
            (order_key,),
        ).fetchone()
    except SQLAlchemyError:
        return "strict_mapping_gate_unavailable"
    if order is None:
        return "strict_mapping_unverified" if require_order else None
    if order["strict_mapping_id"] is None:
        return "strict_mapping_unverified"
    try:
        attempt = connection.execute(
            "SELECT map_number FROM shadow_map_attempts WHERE order_key=?",
            (order_key,),
        ).fetchone()
        if attempt is None:
            return "strict_mapping_unverified"
        impacted = connection.execute(
            """SELECT 1 FROM strict_live_mapping_impacts
                WHERE dependent_type='shadow_order' AND dependent_key=?
                LIMIT 1""",
            (order_key,),
        ).fetchone()
    except SQLAlchemyError:
        return "strict_mapping_gate_unavailable"
    if impacted is not None:
        return "strict_mapping_invalidated"
    try:
        strict_mapping_id = int(order["strict_mapping_id"])
        map_number = int(attempt["map_number"])
    except (TypeError, ValueError, OverflowError):
        return "strict_mapping_unverified"
    if strict_mapping_id <= 0 or map_number <= 0:
        return "strict_mapping_unverified"
    return strict_mapping_context_block_reason(
        connection,
        strict_mapping_id=strict_mapping_id,
        raybet_match_id=str(order["raybet_match_id"]),
        map_number=map_number,
        signal_transport_at=order["signal_transport_at"],
    )


_ROSH_MINUTE_NUMERIC_FIELDS = (
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


def _valid_rosh_minute_table(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    previous_minute = 19
    for bucket in value:
        if not isinstance(bucket, Mapping):
            return False
        minute = bucket.get("minute")
        time_start = bucket.get("time_start")
        time_end = bucket.get("time_end")
        if (
            type(minute) is not int
            or type(time_start) is not int
            or type(time_end) is not int
            or not 20 <= time_start <= minute <= time_end <= 60
            or minute <= previous_minute
            or bucket.get("advantage_side") not in {"radiant", "dire", "even"}
        ):
            return False
        previous_minute = minute
        for field in _ROSH_MINUTE_NUMERIC_FIELDS:
            item = bucket.get(field)
            if (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
            ):
                return False
    return True


def _valid_rosh_score_evidence(
    evidence: Mapping[str, Any],
    *,
    pure_score: float,
    adjusted_score: float | None,
    effective_score: float,
    scoring_mode: str,
    player_coverage_count: int,
) -> bool:
    score = evidence.get("score")
    pure_table = evidence.get("pure_minute_table")
    if not isinstance(score, Mapping) or not _valid_rosh_minute_table(pure_table):
        return False
    expected = {
        "pure_lineup_score": pure_score,
        "player_adjusted_lineup_score": adjusted_score,
        "effective_lineup_score": effective_score,
        "scoring_mode": scoring_mode,
        "player_coverage_count": player_coverage_count,
    }
    if any(score.get(key) != value for key, value in expected.items()):
        return False
    if float(pure_table[-1]["win_rate_graph"]) != pure_score:
        return False
    if scoring_mode == "player_adjusted":
        adjusted_table = evidence.get("minute_table")
        return bool(
            _valid_rosh_minute_table(adjusted_table)
            and adjusted_score is not None
            and float(adjusted_table[-1]["win_rate_graph"]) == adjusted_score
            and [row["minute"] for row in adjusted_table]
            == [row["minute"] for row in pure_table]
        )
    return "minute_table" not in evidence


def query_rosh_lineup_score_for_trusted_draft(
    connection: PostgresSession,
    *,
    raybet_match_id: str,
    map_number: int,
    strict_mapping_id: int,
    draft_hash: str,
    radiant_hero_ids: Sequence[int],
    dire_hero_ids: Sequence[int],
    as_of: datetime,
    formula_version: str | None = None,
    radiant_player_ids: Sequence[int | None] | None = None,
    dire_player_ids: Sequence[int | None] | None = None,
) -> RoshLineupScore | None:
    """Read a current Rosh score without mutating or configuring a connection."""
    if (
        not raybet_match_id
        or type(map_number) is not int
        or map_number <= 0
        or type(strict_mapping_id) is not int
        or strict_mapping_id <= 0
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
    ):
        return None
    try:
        calculated_hash = LiveBettingStore.rosh_draft_hash(
            radiant_hero_ids, dire_hero_ids
        )
        if (radiant_player_ids is None) != (dire_player_ids is None):
            return None
        player_identity_hash = (
            LiveBettingStore.rosh_player_identity_hash(
                radiant_player_ids, dire_player_ids
            )
            if radiant_player_ids is not None
            else None
        )
    except ValueError:
        return None
    if calculated_hash != draft_hash:
        return None
    try:
        anchor_cursor = connection.execute(
            """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                      anchored_at, status, conflict_at
                 FROM vision_draft_anchors
                WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        )
        anchor_row = anchor_cursor.fetchone()
        if anchor_row is None:
            return None
        anchor = anchor_row
        radiant_json = LiveBettingStore.json(list(radiant_hero_ids))
        dire_json = LiveBettingStore.json(list(dire_hero_ids))
        if (
            str(anchor["draft_hash"]) != draft_hash
            or str(anchor["radiant_hero_ids"]) != radiant_json
            or str(anchor["dire_hero_ids"]) != dire_json
        ):
            return None
        anchored_at = datetime.fromisoformat(
            str(anchor["anchored_at"]).replace("Z", "+00:00")
        )
        if (
            anchored_at.tzinfo is None
            or anchored_at.utcoffset() is None
            or anchored_at > as_of
        ):
            return None
        status = str(anchor["status"])
        if status != "anchored":
            if status != "conflict" or anchor["conflict_at"] is None:
                return None
            conflict_time = datetime.fromisoformat(
                str(anchor["conflict_at"]).replace("Z", "+00:00")
            )
            if (
                conflict_time.tzinfo is None
                or conflict_time.utcoffset() is None
                or conflict_time <= as_of
            ):
                return None
            effective_conflict = connection.execute(
                """SELECT 1 FROM vision_draft_conflicts
                    WHERE raybet_match_id=? AND map_number=?
                      AND (
                            live_text_timestamp_utc(captured_at) IS NULL
                            OR live_text_timestamp_utc(captured_at)<=
                               CAST(? AS timestamptz)
                      )
                    LIMIT 1""",
                (raybet_match_id, map_number, as_of.isoformat()),
            ).fetchone()
            if effective_conflict is not None:
                return None
        from .strict_eligibility import query_strict_mapping_snapshot

        mapping_snapshot = query_strict_mapping_snapshot(
            connection,
            mapping_id=strict_mapping_id,
            observed_at=as_of,
        )
        if (
            not mapping_snapshot.eligible
            or mapping_snapshot.mapping is None
            or mapping_snapshot.mapping.raybet_match_id != raybet_match_id
            or mapping_snapshot.mapping.map_number != map_number
        ):
            return None
        formula_sql = " AND formula_version=?" if formula_version else ""
        player_sql = (
            " AND player_identity_hash=?"
            if player_identity_hash is not None
            else ""
        )
        parameters: list[Any] = [
            raybet_match_id,
            map_number,
            strict_mapping_id,
            draft_hash,
        ]
        if player_identity_hash is not None:
            parameters.append(player_identity_hash)
        parameters.extend(
            (
                radiant_json,
                dire_json,
                as_of.isoformat(),
                as_of.isoformat(),
            )
        )
        if formula_version:
            parameters.append(formula_version)
        score_cursor = connection.execute(
            f"""SELECT * FROM rosh_lineup_scores
                 WHERE raybet_match_id=? AND map_number=?
                   AND strict_mapping_id=? AND draft_hash=?
                   {player_sql}
                   AND radiant_hero_ids_json=? AND dire_hero_ids_json=?
                   AND live_text_timestamp_utc(source_as_of)<=
                       CAST(? AS timestamptz)
                   AND live_text_timestamp_utc(created_at)<=
                       CAST(? AS timestamptz)
                   {formula_sql}
                 ORDER BY source_as_of DESC, created_at DESC, score_key DESC""",
            tuple(parameters),
        )
        for row in score_cursor.fetchall():
            score = LiveBettingStore._rosh_score_from_row(row)
            if score is not None and score.source_as_of <= as_of:
                return score
        return None
    except (SQLAlchemyError, TypeError, ValueError):
        return None


class LiveBettingStore:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        raw_archive_root: str | Path | None = None,
    ) -> None:
        if database_url is not None and engine is not None:
            raise ValueError("database_url and engine are mutually exclusive")
        self.engine = engine or build_engine(database_url)
        self._owns_engine = engine is None
        self.connection = PostgresSession(self.engine)
        if raw_archive_root is None:
            raw_archive_root = Path("data") / "live_betting" / "raw-v2"
        self.raw_archive_root = Path(raw_archive_root).resolve()
        self.raw_archive = RawArchive(self.raw_archive_root)
        self._transaction_depth = 0

    def close(self) -> None:
        self.connection.close()
        if self._owns_engine:
            self.engine.dispose()

    def __enter__(self) -> "LiveBettingStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def init_schema(self, *, external_transaction: bool = False) -> None:
        if external_transaction and not self.connection.in_transaction:
            raise RuntimeError("external transaction is not active")
        revision = self.connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        if revision is None or str(revision[0]) != ALEMBIC_HEAD:
            actual = None if revision is None else str(revision[0])
            raise RuntimeError(
                f"PostgreSQL schema revision {actual!r} is not {ALEMBIC_HEAD}"
            )
        live_version = self.connection.execute(
            "SELECT MAX(version) FROM live_schema_version"
        ).fetchone()
        actual_live = None if live_version is None else live_version[0]
        if actual_live is None or int(actual_live) != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"live schema version {actual_live!r} is not "
                f"{CURRENT_SCHEMA_VERSION}"
            )

    @staticmethod
    def _stored_shadow_order(row: DatabaseRow) -> ShadowOrder:
        market_parts = str(row["market_key"]).split("|")
        if len(market_parts) != 4:
            raise ValueError("stored shadow market key is invalid")
        line = float(market_parts[3]) if market_parts[3] else None
        market = Market(
            market_parts[0],
            market_parts[1],
            market_parts[2] or None,
            line,
            str(row["signal_outcome_key"] or ""),
            True,
        )
        return ShadowOrder(
            order_key=str(row["order_key"]),
            raybet_match_id=str(row["raybet_match_id"]),
            odds_id=str(row["odds_id"]),
            market=market,
            signaled_at=datetime.fromisoformat(str(row["signaled_at"])),
            model_probability=float(row["model_probability"]),
            market_probability=float(row["market_probability"]),
            signal_price=float(row["signal_price"]),
            signal_transport_key=str(row["signal_transport_key"]),
            signal_transport_at=datetime.fromisoformat(
                str(row["signal_transport_at"])
            ),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            signal_odds_group_id=row["signal_odds_group_id"],
            signal_outcome_key=row["signal_outcome_key"],
            signal_identity_verified=bool(row["signal_identity_verified"]),
            stake=float(row["stake"]),
            status=str(row["status"]),
            fill_price=(
                float(row["fill_price"]) if row["fill_price"] is not None else None
            ),
            filled_at=(
                datetime.fromisoformat(str(row["filled_at"]))
                if row["filled_at"] is not None
                else None
            ),
            rejection_reason=row["rejection_reason"],
        )

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> DatabaseResult:
        try:
            cursor = self.connection.execute(sql, parameters)
        except Exception:
            if self._transaction_depth == 0:
                self.connection.rollback()
            raise
        if self._transaction_depth == 0:
            self.connection.commit()
        return cursor

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit a unit of work atomically while supporting nested callers."""
        with self.connection.transaction():
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1

    @contextmanager
    def savepoint(self, name: str) -> Iterator[None]:
        """Create a named rollback boundary inside an active transaction."""
        if self._transaction_depth == 0:
            raise RuntimeError("savepoint requires an active transaction")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid savepoint name")
        with self.connection.transaction():
            yield

    @staticmethod
    def _event_value(event: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
        if isinstance(event, Mapping):
            return event.get(name, default)
        return getattr(event, name, default)

    @staticmethod
    def _iso(value: datetime | str) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
            return value.isoformat()
        return str(value)

    def _draft_conflict_state(
        self, raybet_match_id: str, map_number: int,
    ) -> tuple[bool, str | None]:
        """Return whether a map has a draft conflict and its earliest cutoff.

        Conflict rows can arrive out of capture order.  Causal readers derive
        the cutoff from rows that conflict with the rebuilt canonical anchor
        and fail closed on missing schema or malformed timestamps.  If an
        operator froze a map without an intrinsic draft mismatch, every audit
        row remains effective.
        """
        try:
            anchor = self.connection.execute(
                """SELECT draft_hash, radiant_team_side, status, conflict_at
                     FROM vision_draft_anchors
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            rows = self.connection.execute(
                """SELECT captured_at, observed_draft_hash,
                          observed_radiant_team_side
                     FROM vision_draft_conflicts
                    WHERE raybet_match_id=? AND map_number=?
                    ORDER BY conflict_id""",
                (raybet_match_id, map_number),
            ).fetchall()
        except SQLAlchemyError:
            return True, None
        if anchor is None:
            return (True, None) if rows else (False, None)
        status = str(anchor["status"])
        if status not in {"anchored", "conflict"}:
            return True, None
        parsed_rows: list[tuple[datetime, str, bool]] = []
        for row in rows:
            value = str(row["captured_at"])
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return True, None
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return True, None
            intrinsic = str(row["observed_draft_hash"]) != str(
                anchor["draft_hash"]
            ) or (
                anchor["radiant_team_side"] in {"team_one", "team_two"}
                and row["observed_radiant_team_side"]
                in {"team_one", "team_two"}
                and row["observed_radiant_team_side"]
                != anchor["radiant_team_side"]
            )
            normalized = timestamp.astimezone(timezone.utc)
            parsed_rows.append((normalized, normalized.isoformat(), intrinsic))
        if status == "anchored" and not parsed_rows:
            return False, None

        parsed: list[tuple[datetime, str]] = [
            (timestamp, value)
            for timestamp, value, intrinsic in parsed_rows
            if intrinsic
        ]
        if not parsed:
            parsed = [
                (timestamp, value) for timestamp, value, _intrinsic in parsed_rows
            ]
        if status == "conflict" and anchor["conflict_at"] is not None:
            value = str(anchor["conflict_at"])
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return True, None
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return True, None
            normalized = timestamp.astimezone(timezone.utc)
            parsed.append((normalized, normalized.isoformat()))
        if not parsed:
            return True, None
        return True, min(parsed)[1]

    @staticmethod
    def _draft_event_key(captured_at: object, source_frame_ref: object) -> tuple[datetime, str] | None:
        """Return the deterministic event-time ordering key for one frame."""
        try:
            parsed = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc), str(source_frame_ref)

    def _rebuild_vision_draft_anchor(
        self, observation: Any, anchor: DatabaseRow,
    ) -> bool:
        """Rebuild a draft anchor in deterministic browser event-time order.

        Browser capture time is the event order.  Rebuilding for every confirmed
        frame is necessary because a frame can arrive before an already-recorded
        conflict or team-side observation while still being later than the draft
        anchor.  All candidate facts remain append-only in the observation and
        conflict tables.
        """
        match_id = str(observation.raybet_match_id)
        map_number = int(observation.map_number)
        candidates: dict[tuple[str, str], dict[str, Any]] = {}

        def add_candidate(
            captured_at: object,
            source_frame_ref: object,
            draft_hash: object,
            radiant_hero_ids: object,
            dire_hero_ids: object,
            radiant_team_side: object,
        ) -> None:
            key = (str(captured_at), str(source_frame_ref))
            event_key = self._draft_event_key(*key)
            if event_key is None or not str(source_frame_ref).strip():
                return
            try:
                radiant = json.loads(str(radiant_hero_ids))
                dire = json.loads(str(dire_hero_ids))
            except (TypeError, ValueError, json.JSONDecodeError):
                return
            if not _valid_confirmed_vision_payload(
                radiant, dire, source_frame_ref
            ):
                return
            payload = self.json({"radiant": radiant, "dire": dire})
            calculated_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if str(draft_hash) != calculated_hash:
                return
            side = radiant_team_side if radiant_team_side in {"team_one", "team_two"} else None
            candidates[key] = {
                "captured_at": event_key[0],
                "source_frame_ref": str(source_frame_ref),
                "draft_hash": calculated_hash,
                "radiant_json": self.json(radiant),
                "dire_json": self.json(dire),
                "radiant_team_side": side,
            }

        add_candidate(
            anchor["anchored_at"],
            anchor["source_frame_ref"],
            anchor["draft_hash"],
            anchor["radiant_hero_ids"],
            anchor["dire_hero_ids"],
            anchor["radiant_team_side"],
        )
        conflict_rows = self.connection.execute(
            """SELECT captured_at, source_frame_ref, observed_draft_hash,
                      radiant_hero_ids, dire_hero_ids,
                      observed_radiant_team_side
                 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        ).fetchall()
        conflict_keys = {
            (str(row["captured_at"]), str(row["source_frame_ref"]))
            for row in conflict_rows
        }
        for row in conflict_rows:
            add_candidate(
                row["captured_at"],
                row["source_frame_ref"],
                row["observed_draft_hash"],
                row["radiant_hero_ids"],
                row["dire_hero_ids"],
                row["observed_radiant_team_side"],
            )
        for row in self.connection.execute(
            """SELECT captured_at, source_frame_ref, radiant_hero_ids,
                      dire_hero_ids, radiant_team_side
                 FROM vision_observations
                WHERE raybet_match_id=? AND map_number=? AND confirmed=1""",
            (match_id, map_number),
        ).fetchall():
            key = (str(row["captured_at"]), str(row["source_frame_ref"]))
            if key not in conflict_keys:
                try:
                    payload = self.json({
                        "radiant": json.loads(str(row["radiant_hero_ids"])),
                        "dire": json.loads(str(row["dire_hero_ids"])),
                    })
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                add_candidate(
                    row["captured_at"],
                    row["source_frame_ref"],
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    row["radiant_hero_ids"],
                    row["dire_hero_ids"],
                    row["radiant_team_side"],
                )
        add_candidate(
            self._iso(observation.captured_at),
            observation.source_frame_ref,
            hashlib.sha256(
                self.json({
                    "radiant": list(observation.radiant_hero_ids),
                    "dire": list(observation.dire_hero_ids),
                }).encode("utf-8")
            ).hexdigest(),
            self.json(list(observation.radiant_hero_ids)),
            self.json(list(observation.dire_hero_ids)),
            observation.radiant_team_side,
        )

        invalidated = {
            (str(row["captured_at"]), str(row["source_frame_ref"]))
            for row in self.connection.execute(
                """SELECT invalidation.captured_at,
                          invalidation.source_frame_ref
                     FROM vision_observation_invalidations AS invalidation
                     JOIN vision_observations AS observation
                       ON observation.raybet_match_id=
                          invalidation.raybet_match_id
                      AND observation.captured_at=invalidation.captured_at
                      AND observation.source_frame_ref=
                          invalidation.source_frame_ref
                    WHERE invalidation.raybet_match_id=?
                      AND observation.map_number=?""",
                (match_id, map_number),
            ).fetchall()
        }
        candidates = {
            key: value for key, value in candidates.items() if key not in invalidated
        }
        ordered = sorted(
            candidates.values(),
            key=lambda value: (value["captured_at"], value["source_frame_ref"]),
        )
        if not ordered:
            return False
        canonical = ordered[0]
        canonical_hash = canonical["draft_hash"]
        canonical_side: str | None = None
        side_source: dict[str, Any] | None = None
        conflict_candidates: list[dict[str, Any]] = []
        for candidate in ordered:
            if candidate["draft_hash"] != canonical_hash:
                conflict_candidates.append(candidate)
                continue
            side = candidate["radiant_team_side"]
            if side is None:
                continue
            if canonical_side is None:
                canonical_side = side
                side_source = candidate
            elif side != canonical_side:
                conflict_candidates.append(candidate)

        existing_conflict = bool(anchor["status"] == "conflict")
        conflict_cutoff = min(
            (candidate["captured_at"] for candidate in conflict_candidates),
            default=None,
        )
        if existing_conflict and conflict_cutoff is None:
            old_cutoff = self._draft_event_key(
                anchor["conflict_at"], anchor["source_frame_ref"]
            )
            if old_cutoff is not None:
                conflict_cutoff = old_cutoff[0]
            else:
                conflict_cutoff = min(
                    (value["captured_at"] for value in candidates.values()),
                    default=None,
                )
        has_conflict = conflict_cutoff is not None
        for candidate in conflict_candidates:
            self.connection.execute(
                """INSERT INTO vision_draft_conflicts
                   (raybet_match_id, map_number, captured_at,
                    source_frame_ref, observed_draft_hash,
                    radiant_hero_ids, dire_hero_ids,
                    observed_radiant_team_side, reason, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (
                    match_id,
                    map_number,
                    candidate["captured_at"].isoformat(),
                    candidate["source_frame_ref"],
                    candidate["draft_hash"],
                    candidate["radiant_json"],
                    candidate["dire_json"],
                    candidate["radiant_team_side"],
                    VISION_DRAFT_CONFLICT_REASON,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        if has_conflict:
            cutoff = conflict_cutoff
            for candidate in ordered:
                if candidate["captured_at"] < cutoff:
                    continue
                if candidate["draft_hash"] != canonical_hash or candidate in conflict_candidates:
                    continue
                if (
                    candidate["captured_at"], candidate["source_frame_ref"]
                ) == (
                    canonical["captured_at"], canonical["source_frame_ref"]
                ):
                    continue
                self.connection.execute(
                    """INSERT INTO vision_draft_conflicts
                       (raybet_match_id, map_number, captured_at,
                        source_frame_ref, observed_draft_hash,
                        radiant_hero_ids, dire_hero_ids,
                        observed_radiant_team_side, reason, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT DO NOTHING""",
                    (
                        match_id,
                        map_number,
                        candidate["captured_at"].isoformat(),
                        candidate["source_frame_ref"],
                        candidate["draft_hash"],
                        candidate["radiant_json"],
                        candidate["dire_json"],
                        candidate["radiant_team_side"],
                        VISION_DRAFT_CONFLICT_REASON,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

        side_time = side_source["captured_at"].isoformat() if side_source else None
        side_ref = side_source["source_frame_ref"] if side_source else None
        status = "conflict" if has_conflict else "anchored"
        conflict_at = conflict_cutoff.isoformat() if conflict_cutoff else None
        self.connection.execute(
            "SET LOCAL dota2.allow_vision_anchor_rebuild = 'on'"
        )
        self.connection.execute(
            """UPDATE vision_draft_anchors
                  SET draft_hash=?, radiant_hero_ids=?, dire_hero_ids=?,
                      radiant_team_side=?, team_side_anchored_at=?,
                      team_side_source_frame_ref=?, anchored_at=?,
                      source_frame_ref=?, status=?, conflict_at=?
                WHERE raybet_match_id=? AND map_number=?""",
            (
                canonical["draft_hash"],
                canonical["radiant_json"],
                canonical["dire_json"],
                canonical_side,
                side_time,
                side_ref,
                canonical["captured_at"].isoformat(),
                canonical["source_frame_ref"],
                status,
                conflict_at,
                match_id,
                map_number,
            ),
        )
        self.connection.execute(
            "SET LOCAL dota2.allow_vision_anchor_rebuild = 'off'"
        )

        for row in self.connection.execute(
            """SELECT captured_at, source_frame_ref, radiant_hero_ids,
                      dire_hero_ids, radiant_team_side
                 FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        ).fetchall():
            key = (str(row["captured_at"]), str(row["source_frame_ref"]))
            candidate = candidates.get(key)
            if candidate is None or key in invalidated:
                continue
            trusted = candidate["draft_hash"] == canonical_hash
            if (
                trusted
                and canonical_side is not None
                and candidate["radiant_team_side"] is not None
                and candidate["radiant_team_side"] != canonical_side
            ):
                trusted = False
            if conflict_cutoff is not None and candidate["captured_at"] >= conflict_cutoff:
                trusted = False
            self.connection.execute(
                """UPDATE vision_observations SET confirmed=?
                    WHERE raybet_match_id=? AND captured_at=?
                      AND source_frame_ref=?""",
                (int(trusted), match_id, row["captured_at"], row["source_frame_ref"]),
            )

        if has_conflict:
            self._invalidate_draft_dependents(
                match_id,
                map_number,
                VISION_DRAFT_CONFLICT_REASON,
                conflict_at,
            )
        current_key = (
            self._iso(observation.captured_at), str(observation.source_frame_ref)
        )
        current = candidates.get(current_key)
        if current is None:
            return False
        trusted = current["draft_hash"] == canonical_hash
        if (
            trusted
            and canonical_side is not None
            and current["radiant_team_side"] is not None
            and current["radiant_team_side"] != canonical_side
        ):
            trusted = False
        if conflict_cutoff is not None and current["captured_at"] >= conflict_cutoff:
            trusted = False
        return trusted

    def _curve_anchor_authority_matches(
        self,
        authority: DraftLandmarkAuthority,
        *,
        causal_cutoff: datetime | str | None = None,
    ) -> bool:
        curve = self.connection.execute(
            """SELECT raybet_match_id, map_number, strict_mapping_id,
                      radiant_hero_ids_json, dire_hero_ids_json,
                      radiant_team_side, anchor_draft_hash,
                      anchor_source_frame_ref, anchor_anchored_at,
                      anchor_team_side_source_frame_ref,
                      anchor_team_side_anchored_at
                 FROM prospective_draft_curves WHERE curve_key=?""",
            (authority.curve_key,),
        ).fetchone()
        if curve is None:
            return False
        radiant = _vision_hero_ids(curve["radiant_hero_ids_json"])
        dire = _vision_hero_ids(curve["dire_hero_ids_json"])
        if radiant is None or dire is None:
            return False
        anchor = self.connection.execute(
            """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                      radiant_team_side, team_side_anchored_at,
                      team_side_source_frame_ref, anchored_at,
                      source_frame_ref, status
                 FROM vision_draft_anchors
                WHERE raybet_match_id=? AND map_number=?""",
            (str(curve["raybet_match_id"]), int(curve["map_number"])),
        ).fetchone()
        if anchor is None:
            return False
        draft_frame = self.connection.execute(
            """SELECT * FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?
                  AND captured_at=? AND source_frame_ref=?""",
            (
                str(curve["raybet_match_id"]),
                int(curve["map_number"]),
                str(anchor["anchored_at"]),
                str(anchor["source_frame_ref"]),
            ),
        ).fetchone()
        side_frame = self.connection.execute(
            """SELECT * FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?
                  AND captured_at=? AND source_frame_ref=?""",
            (
                str(curve["raybet_match_id"]),
                int(curve["map_number"]),
                str(anchor["team_side_anchored_at"]),
                str(anchor["team_side_source_frame_ref"]),
            ),
        ).fetchone()
        expected_hash = hashlib.sha256(
            self.json(
                {"radiant": list(radiant), "dire": list(dire)}
            ).encode("utf-8")
        ).hexdigest()
        if authority.source_ref != f"prospective-draft:{authority.curve_key}":
            return False
        if (
            str(curve["raybet_match_id"]) == ""
            or int(curve["map_number"]) < 1
            or int(curve["strict_mapping_id"]) != authority.strict_mapping_id
            or str(curve["radiant_team_side"]) != authority.radiant_team_side
            or draft_frame is None
            or side_frame is None
            or not _trusted_vision_row(draft_frame)
            or not _trusted_vision_row(side_frame)
            or str(anchor["status"])
            not in (
                {"anchored"}
                if causal_cutoff is None
                else {"anchored", "conflict"}
            )
            or str(anchor["draft_hash"]) != expected_hash
            or str(curve["anchor_draft_hash"]) != expected_hash
            or _vision_hero_ids(anchor["radiant_hero_ids"]) != radiant
            or _vision_hero_ids(anchor["dire_hero_ids"]) != dire
            or str(anchor["radiant_team_side"]) != authority.radiant_team_side
            or str(curve["anchor_source_frame_ref"])
            != str(anchor["source_frame_ref"])
            or str(curve["anchor_anchored_at"]) != str(anchor["anchored_at"])
            or str(curve["anchor_team_side_source_frame_ref"])
            != str(anchor["team_side_source_frame_ref"])
            or str(curve["anchor_team_side_anchored_at"])
            != str(anchor["team_side_anchored_at"])
            or _vision_hero_ids(draft_frame["radiant_hero_ids"]) != radiant
            or _vision_hero_ids(draft_frame["dire_hero_ids"]) != dire
            or str(draft_frame["radiant_team_side"])
            != authority.radiant_team_side
            or str(side_frame["radiant_team_side"])
            != authority.radiant_team_side
        ):
            return False
        if causal_cutoff is None:
            if self.connection.execute(
                """SELECT 1 FROM vision_draft_conflicts
                    WHERE raybet_match_id=? AND map_number=? LIMIT 1""",
                (str(curve["raybet_match_id"]), int(curve["map_number"])),
            ).fetchone() is not None:
                return False
        elif self._draft_conflict_effective_at(
            str(curve["raybet_match_id"]),
            int(curve["map_number"]),
            causal_cutoff,
        ):
            return False
        for frame in (draft_frame, side_frame):
            try:
                verify_registered_vision_frame(
                    self.connection,
                    str(frame["source_frame_ref"]),
                    expected_sha256=str(frame["source_frame_sha256"]),
                    expected_bytes=int(frame["source_frame_bytes"]),
                )
            except (RuntimeError, TypeError, ValueError):
                return False
            if self.connection.execute(
                """SELECT 1 FROM vision_observation_invalidations
                    WHERE raybet_match_id=? AND captured_at=?
                      AND source_frame_ref=?""",
                (
                    str(frame["raybet_match_id"]),
                    str(frame["captured_at"]),
                    str(frame["source_frame_ref"]),
                ),
            ).fetchone() is not None:
                return False
        return True

    def _derive_decision_vision_authority(
        self,
        decision: Any,
        *,
        vision_observation: Any,
        vision_transport_key: str,
        draft_authority: DraftLandmarkAuthority,
    ) -> VisionDecisionAuthority | None:
        try:
            captured_at = self._iso(vision_observation.captured_at)
            source_frame_ref = str(vision_observation.source_frame_ref).strip()
            decision_match_id = str(decision.raybet_match_id)
            decision_map_number = int(decision.map_number)
            decision_underdog_side = str(decision.underdog_side)
            decision_market_probability = float(decision.market_probability)
            decided_at = datetime.fromisoformat(
                self._iso(decision.decided_at).replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError):
            return None
        if (
            not source_frame_ref
            or not vision_transport_key.strip()
            or decision_underdog_side not in {"team_one", "team_two"}
            or not math.isfinite(decision_market_probability)
            or decided_at.tzinfo is None
            or decided_at.utcoffset() is None
        ):
            return None
        decided_at = decided_at.astimezone(timezone.utc)
        row = self.connection.execute(
            """SELECT * FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?
                  AND captured_at=? AND source_frame_ref=?""",
            (
                decision_match_id,
                decision_map_number,
                captured_at,
                source_frame_ref,
            ),
        ).fetchone()
        if row is None or not _trusted_vision_row(row):
            return None
        try:
            frame_receipt = verify_registered_vision_frame(
                self.connection,
                str(row["source_frame_ref"]),
                expected_sha256=str(row["source_frame_sha256"]),
                expected_bytes=int(row["source_frame_bytes"]),
            )
        except (RuntimeError, TypeError, ValueError):
            return None
        if self.connection.execute(
            """SELECT 1 FROM vision_observation_invalidations
                WHERE raybet_match_id=? AND captured_at=?
                  AND source_frame_ref=?""",
            (decision_match_id, captured_at, source_frame_ref),
        ).fetchone() is not None:
            return None
        transport = self.connection.execute(
            """SELECT observed_at FROM odds_transport_observations
                WHERE observation_key=? AND raybet_match_id=?
                  AND source='direct'
                  AND timing_status='on_time'
                  AND processing_status='processed'""",
            (vision_transport_key, decision_match_id),
        ).fetchone()
        if transport is None:
            return None
        try:
            transport_at = datetime.fromisoformat(
                str(transport["observed_at"]).replace("Z", "+00:00")
            )
            frame_at = datetime.fromisoformat(
                captured_at.replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if (
            transport_at.tzinfo is None
            or transport_at.utcoffset() is None
            or frame_at.tzinfo is None
            or frame_at.utcoffset() is None
        ):
            return None
        transport_at = transport_at.astimezone(timezone.utc)
        frame_at = frame_at.astimezone(timezone.utc)
        if transport_at != decided_at:
            return None
        lag_seconds = (transport_at - frame_at).total_seconds()
        if lag_seconds < 0.0 or lag_seconds > 15.0:
            return None
        if self.connection.execute(
            """SELECT 1 FROM trusted_odds_winner_market_authority
                WHERE observation_key=? AND raybet_match_id=? AND period=?
                  AND underdog_side=?
                  AND abs(underdog_probability-?)<=1.0e-12""",
            (
                vision_transport_key,
                decision_match_id,
                f"map_{decision_map_number}",
                decision_underdog_side,
                decision_market_probability,
            ),
        ).fetchone() is None:
            return None
        if self.connection.execute(
            """SELECT 1 FROM vision_observations
                WHERE raybet_match_id=?
                  AND live_text_timestamp_utc(captured_at) IS NULL
                LIMIT 1""",
            (decision_match_id,),
        ).fetchone() is not None:
            return None
        latest = self.connection.execute(
            """SELECT captured_at, source_frame_ref,
                      live_text_timestamp_utc(captured_at) AS captured_instant
                 FROM vision_observations
                WHERE raybet_match_id=?
                  AND live_text_timestamp_utc(captured_at)<=
                      CAST(? AS timestamptz)
                ORDER BY live_text_timestamp_utc(captured_at) DESC,
                         captured_at DESC,
                         source_frame_ref
                LIMIT 2""",
            (decision_match_id, transport_at.isoformat()),
        ).fetchall()
        if (
            not latest
            or (
                len(latest) > 1
                and latest[1]["captured_instant"] == latest[0]["captured_instant"]
            )
            or str(latest[0]["captured_at"]) != captured_at
            or str(latest[0]["source_frame_ref"]) != source_frame_ref
        ):
            return None
        if self._draft_conflict_at_or_before(
            decision_match_id,
            decision_map_number,
            decided_at,
        ):
            return None
        if self.connection.execute(
            """SELECT 1 FROM vision_derived_invalidations
                WHERE dependent_type='strategy_decision'
                  AND dependent_key=?""",
            (str(decision.decision_key),),
        ).fetchone() is not None:
            return None
        radiant = _vision_hero_ids(row["radiant_hero_ids"])
        dire = _vision_hero_ids(row["dire_hero_ids"])
        if radiant is None or dire is None:
            return None
        curve = self.connection.execute(
            """SELECT radiant_hero_ids_json, dire_hero_ids_json,
                      radiant_team_side, first_usable_at
                 FROM prospective_draft_curves WHERE curve_key=?""",
            (draft_authority.curve_key,),
        ).fetchone()
        if (
            curve is None
            or _vision_hero_ids(curve["radiant_hero_ids_json"]) != radiant
            or _vision_hero_ids(curve["dire_hero_ids_json"]) != dire
            or str(curve["radiant_team_side"]) != str(row["radiant_team_side"])
        ):
            return None
        try:
            curve_usable_at = datetime.fromisoformat(
                str(curve["first_usable_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if (
            curve_usable_at.tzinfo is None
            or curve_usable_at.utcoffset() is None
            or curve_usable_at.astimezone(timezone.utc) > decided_at
        ):
            return None
        if not self._curve_anchor_authority_matches(draft_authority):
            return None
        if not draft_landmark_authority_matches(
            self.connection,
            draft_authority,
            raybet_match_id=decision_match_id,
            map_number=decision_map_number,
            strict_mapping_id=draft_authority.strict_mapping_id,
            radiant_hero_ids=radiant,
            dire_hero_ids=dire,
            observed_at=decided_at,
            require_current_revisions=True,
            verify_curve=False,
        ):
            return None
        aligned_clock = int(int(row["game_clock_seconds"]) + lag_seconds)
        try:
            caller_identity = (
                str(vision_observation.raybet_match_id),
                int(vision_observation.map_number),
                self._iso(vision_observation.captured_at),
                str(vision_observation.source_frame_ref),
                str(vision_observation.source_frame_sha256),
                int(vision_observation.source_frame_bytes),
                int(vision_observation.game_clock_seconds),
                tuple(vision_observation.radiant_hero_ids),
                tuple(vision_observation.dire_hero_ids),
                str(vision_observation.radiant_team_side),
                str(vision_observation.screen_state),
            )
        except (AttributeError, TypeError, ValueError):
            return None
        if caller_identity != (
            decision_match_id,
            decision_map_number,
            captured_at,
            source_frame_ref,
            frame_receipt.content_sha256,
            frame_receipt.byte_length,
            aligned_clock,
            radiant,
            dire,
            str(row["radiant_team_side"]),
            str(row["screen_state"]),
        ):
            return None
        return VisionDecisionAuthority(
            raybet_match_id=decision_match_id,
            map_number=decision_map_number,
            captured_at=captured_at,
            source_frame_ref=source_frame_ref,
            source_frame_sha256=frame_receipt.content_sha256,
            source_frame_bytes=frame_receipt.byte_length,
            observed_game_clock_seconds=int(row["game_clock_seconds"]),
            aligned_game_clock_seconds=aligned_clock,
            is_paused=int(row["is_paused"]),
            radiant_hero_ids_json=str(row["radiant_hero_ids"]),
            dire_hero_ids_json=str(row["dire_hero_ids"]),
            radiant_team_side=str(row["radiant_team_side"]),
            clock_confidence=float(row["clock_confidence"]),
            draft_confidence=float(row["draft_confidence"]),
            screen_state=str(row["screen_state"]),
            confirmed=int(row["confirmed"]),
            transport_key=vision_transport_key,
            transport_at=str(transport["observed_at"]),
            alignment_method=(
                "forward_projection" if lag_seconds >= 1.0 else "anchor"
            ),
            alignment_lag_seconds=lag_seconds,
        )

    def _decision_vision_authority(
        self,
        decision_key: str,
    ) -> VisionDecisionAuthority | None:
        row = self.connection.execute(
            """SELECT decision.*
                 FROM strategy_decisions AS decision
                 JOIN verified_strategy_decision_vision_authority AS verified
                   ON verified.decision_key=decision.decision_key
                 JOIN odds_transport_observations AS transport
                   ON transport.observation_key=decision.vision_transport_key
                  AND transport.raybet_match_id=decision.raybet_match_id
                  AND transport.observed_at=decision.vision_transport_at
                  AND transport.source='direct'
                WHERE decision.decision_key=?""",
            (decision_key,),
        ).fetchone()
        if row is None:
            return None
        authority = _vision_authority_from_decision_row(row)
        if authority is None:
            return None
        try:
            verify_registered_vision_frame(
                self.connection,
                authority.source_frame_ref,
                expected_sha256=authority.source_frame_sha256,
                expected_bytes=authority.source_frame_bytes,
            )
        except (RuntimeError, TypeError, ValueError):
            return None
        return authority

    def _order_vision_authority_block_reason(
        self,
        order_key: str,
    ) -> str | None:
        lineage = self.connection.execute(
            """SELECT decision_key FROM shadow_order_decision_lineage
                WHERE order_key=?""",
            (order_key,),
        ).fetchone()
        order_row = self.connection.execute(
            "SELECT * FROM shadow_orders WHERE order_key=?",
            (order_key,),
        ).fetchone()
        attempt = self.connection.execute(
            """SELECT map_number FROM shadow_map_attempts WHERE order_key=?""",
            (order_key,),
        ).fetchone()
        if lineage is None or order_row is None or attempt is None:
            return "vision_authority_unverifiable"
        try:
            signal_transport_at = datetime.fromisoformat(
                str(order_row["signal_transport_at"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return "vision_authority_unverifiable"
        if (
            signal_transport_at.tzinfo is None
            or signal_transport_at.utcoffset() is None
        ):
            return "vision_authority_unverifiable"
        decision_key = str(lineage["decision_key"])
        decision_vision = self._decision_vision_authority(decision_key)
        order_vision = _vision_authority_from_decision_row(order_row)
        if decision_vision is None or order_vision != decision_vision:
            return "vision_authority_unverifiable"
        try:
            verify_bound_order_vision_frame(self.connection, order_key)
        except (RuntimeError, TypeError, ValueError, SQLAlchemyError):
            return "vision_frame_integrity_failed"
        if (
            str(order_row["signal_transport_key"])
            != decision_vision.transport_key
            or str(order_row["signal_transport_at"]) != decision_vision.transport_at
        ):
            return "vision_authority_unverifiable"
        draft_authority = self._decision_draft_authority(decision_key)
        radiant = _vision_hero_ids(decision_vision.radiant_hero_ids_json)
        dire = _vision_hero_ids(decision_vision.dire_hero_ids_json)
        if draft_authority is None or radiant is None or dire is None:
            return "vision_authority_unverifiable"
        if not self._curve_anchor_authority_matches(
            draft_authority,
            causal_cutoff=signal_transport_at,
        ):
            return "vision_authority_unverifiable"
        if not draft_landmark_authority_matches(
            self.connection,
            draft_authority,
            raybet_match_id=str(order_row["raybet_match_id"]),
            map_number=int(attempt["map_number"]),
            strict_mapping_id=int(order_row["strict_mapping_id"]),
            radiant_hero_ids=radiant,
            dire_hero_ids=dire,
            observed_at=signal_transport_at,
            # The order snapshots the revisions that were current at signal
            # time. Later append-only outcomes advance the global clock but
            # do not invalidate that immutable historical authority.
            require_current_revisions=False,
            verify_curve=False,
        ):
            return "vision_authority_unverifiable"
        return None

    def _draft_conflict_at_or_before(
        self,
        raybet_match_id: str,
        map_number: int,
        at: datetime | str | None,
    ) -> bool:
        if self._draft_conflict_effective_at(raybet_match_id, map_number, at):
            return True
        return self._vision_observation_invalidated_at_or_before(
            raybet_match_id, map_number, at
        )

    def _draft_conflict_effective_at(
        self,
        raybet_match_id: str,
        map_number: int,
        at: datetime | str | None,
    ) -> bool:
        """Apply a draft conflict only at or after its event-time cutoff."""
        conflicted, cutoff = self._draft_conflict_state(raybet_match_id, map_number)
        if conflicted:
            if cutoff is None or at is None:
                return True
            try:
                target = datetime.fromisoformat(
                    self._iso(at).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return True
            if target.tzinfo is None or target.utcoffset() is None:
                return True
            if datetime.fromisoformat(cutoff) <= target.astimezone(timezone.utc):
                return True
        return False

    def _vision_observation_invalidated_at_or_before(
        self,
        raybet_match_id: str,
        map_number: int,
        at: datetime | str | None,
    ) -> bool:
        """Fail closed when no trusted frame survives an observation audit.

        An invalidated frame does not permanently poison a map: a later
        confirmed frame can restore the causal stream.  Until such a frame is
        present at the requested event-time cutoff, new derived writes remain
        blocked.
        """
        try:
            rows = self.connection.execute(
                """SELECT invalidation.captured_at
                     FROM vision_observation_invalidations AS invalidation
                     JOIN vision_observations AS observation
                       ON observation.raybet_match_id=invalidation.raybet_match_id
                      AND observation.captured_at=invalidation.captured_at
                      AND observation.source_frame_ref=invalidation.source_frame_ref
                    WHERE invalidation.raybet_match_id=?
                      AND observation.map_number=?""",
                (raybet_match_id, map_number),
            ).fetchall()
        except SQLAlchemyError:
            # A missing or unreadable audit table cannot prove that the
            # lineage is clean.  All causal writers must fail closed until
            # schema repair/migration has completed.
            return True
        if not rows:
            return False
        if at is None:
            return True
        try:
            target = datetime.fromisoformat(
                self._iso(at).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return True
        if target.tzinfo is None or target.utcoffset() is None:
            return True
        target = target.astimezone(timezone.utc)
        invalidated: list[datetime] = []
        for row in rows:
            try:
                captured = datetime.fromisoformat(
                    str(row["captured_at"]).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return True
            if captured.tzinfo is None or captured.utcoffset() is None:
                return True
            captured = captured.astimezone(timezone.utc)
            if captured <= target:
                invalidated.append(captured)
        if not invalidated:
            return False
        latest_invalidated = max(invalidated)
        try:
            valid_rows = self.connection.execute(
                """SELECT observation.captured_at
                     FROM vision_observations AS observation
                    WHERE observation.raybet_match_id=?
                      AND observation.map_number=?
                      AND observation.confirmed=1
                       AND observation.captured_at::timestamptz<=
                           CAST(? AS timestamptz)
                      AND NOT EXISTS (
                           SELECT 1
                             FROM vision_observation_invalidations AS invalidation
                            WHERE invalidation.raybet_match_id=observation.raybet_match_id
                              AND invalidation.captured_at=observation.captured_at
                              AND invalidation.source_frame_ref=observation.source_frame_ref
                      )
                    ORDER BY observation.captured_at DESC""",
                (raybet_match_id, map_number, target.isoformat()),
            ).fetchall()
        except SQLAlchemyError:
            return True
        for row in valid_rows:
            try:
                captured = datetime.fromisoformat(
                    str(row["captured_at"]).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return True
            if captured.tzinfo is None or captured.utcoffset() is None:
                return True
            if captured.astimezone(timezone.utc) > latest_invalidated:
                return False
        return True

    def _vision_derived_block_reason(self, order_key: str) -> str | None:
        try:
            row = self.connection.execute(
                """SELECT block_reason FROM vision_derived_invalidations
                    WHERE dependent_type='shadow_order' AND dependent_key=?
                    LIMIT 1""",
                (order_key,),
            ).fetchone()
        except SQLAlchemyError:
            # Legacy databases may not have the stable gate-code column yet.
            # The row itself is still a durable invalidation, so preserve the
            # conservative draft-conflict fence until migration runs.
            try:
                row = self.connection.execute(
                    """SELECT 1 FROM vision_derived_invalidations
                        WHERE dependent_type='shadow_order'
                          AND dependent_key=?
                        LIMIT 1""",
                    (order_key,),
                ).fetchone()
            except SQLAlchemyError:
                return "vision_draft_conflict"
            return "vision_draft_conflict" if row is not None else None
        if row is None:
            return None
        return str(row["block_reason"] or "vision_draft_conflict")

    def vision_block_reason_for_order(self, order_key: str) -> str | None:
        """Return the stable vision gate code that blocks one order lineage."""
        derived = self._vision_derived_block_reason(order_key)
        if derived is not None:
            return derived
        try:
            row = self.connection.execute(
                """SELECT orders.raybet_match_id, attempt.map_number,
                          orders.signal_transport_at
                     FROM shadow_orders AS orders
                     JOIN shadow_map_attempts AS attempt
                       ON attempt.order_key=orders.order_key
                    WHERE orders.order_key=?""",
                (order_key,),
            ).fetchone()
        except SQLAlchemyError:
            return "vision_draft_conflict"
        if row is not None:
            if self._vision_observation_invalidated_at_or_before(
                str(row["raybet_match_id"]),
                int(row["map_number"]),
                row["signal_transport_at"],
            ):
                return "vision_observation_invalidated"
            if self._draft_conflict_effective_at(
                str(row["raybet_match_id"]),
                int(row["map_number"]),
                row["signal_transport_at"],
            ):
                return "vision_draft_conflict"
        return self._order_vision_authority_block_reason(order_key)

    def _strict_mapping_context_block_reason(
        self,
        *,
        strict_mapping_id: int,
        raybet_match_id: str,
        map_number: int,
        signal_transport_at: datetime | str,
    ) -> str | None:
        return strict_mapping_context_block_reason(
            self.connection,
            strict_mapping_id=strict_mapping_id,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            signal_transport_at=signal_transport_at,
        )

    def _strict_mapping_block_reason_for_order(self, order_key: str) -> str | None:
        return strict_order_mapping_block_reason(self.connection, order_key)

    def order_block_reason(self, order_key: str) -> str | None:
        """Return the first stable safety gate that blocks an order lineage."""
        strict = self._strict_mapping_block_reason_for_order(order_key)
        if strict is not None:
            return strict
        return self.vision_block_reason_for_order(order_key)

    def _order_draft_conflict_effective_at(
        self, order_key: str, at: datetime | str | None
    ) -> bool:
        row = self.connection.execute(
            """SELECT attempt.raybet_match_id, attempt.map_number
                 FROM shadow_map_attempts AS attempt
                WHERE attempt.order_key=?""",
            (order_key,),
        ).fetchone()
        if row is None:
            return False
        return self._draft_conflict_effective_at(
            str(row["raybet_match_id"]), int(row["map_number"]), at
        )

    @staticmethod
    def _scalar(value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value

    def upsert_provider_match(self, match: ProviderMatch, updated_at: datetime) -> None:
        self.execute(
            """INSERT INTO provider_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
            (match.provider, match.provider_match_id, match.tournament, match.team_one,
             match.team_two, match.scheduled_at.isoformat() if match.scheduled_at else None,
             match.best_of, match.status, self.json(sanitize_raybet_payload(match.raw)),
             updated_at.isoformat()),
        )

    def upsert_raybet_match(
        self,
        row: dict[str, Any],
        updated_at: datetime,
        *,
        public_live_url: object = None,
    ) -> None:
        safe_row = sanitize_raybet_payload(row)
        if not isinstance(safe_row, dict):
            raise ValueError("RayBet match payload must be an object")
        safe_row.pop(PUBLIC_STREAM_EVIDENCE_KEY, None)
        evidence = public_stream_evidence(public_live_url)
        if evidence is None:
            safe_row.pop("live_url", None)
        else:
            safe_row["live_url"] = evidence["url"]
            safe_row[PUBLIC_STREAM_EVIDENCE_KEY] = evidence
        team_one_row, team_two_row = self._raybet_teams_for_write(safe_row)
        team_one = str(team_one_row.get("team_name") or "")
        team_two = str(team_two_row.get("team_name") or "")
        round_name = str(safe_row.get("round") or "").lower()
        best_of = int(round_name[2:]) if round_name.startswith("bo") and round_name[2:].isdigit() else None
        self.execute(
            """INSERT INTO raybet_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raybet_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              live_url=excluded.live_url, raw_json=excluded.raw_json,
              updated_at=excluded.updated_at
            WHERE excluded.updated_at::timestamptz IS NOT NULL
              AND (
                    raybet_matches.updated_at::timestamptz IS NULL
                    OR excluded.updated_at::timestamptz >=
                       raybet_matches.updated_at::timestamptz
              )""",
            (
                str(safe_row.get("id")),
                str(safe_row.get("tournament_name") or ""),
                team_one,
                team_two,
                safe_row.get("start_time"),
                best_of,
                str(safe_row.get("status") or ""),
                safe_row.get("live_url"),
                self.json(safe_row),
                updated_at.isoformat(),
            ),
        )

    def insert_browser_raybet_match(
        self, row: dict[str, Any], updated_at: datetime
    ) -> bool:
        """Insert sanitized browser metadata without replacing direct-owned data."""
        safe_row = sanitize_raybet_payload(row)
        if not isinstance(safe_row, dict):
            raise ValueError("RayBet browser metadata must be an object")
        team_one_row, team_two_row = self._raybet_teams_for_write(safe_row)
        team_one = str(team_one_row.get("team_name") or "")
        team_two = str(team_two_row.get("team_name") or "")
        round_name = str(safe_row.get("round") or "").lower()
        best_of = (
            int(round_name[2:])
            if round_name.startswith("bo") and round_name[2:].isdigit()
            else None
        )
        cursor = self.execute(
            """INSERT INTO raybet_matches
            (raybet_match_id, tournament, team_one, team_two, scheduled_at, best_of,
             status, live_url, raw_json, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
             ON CONFLICT DO NOTHING""",
            (
                str(safe_row.get("id")),
                str(safe_row.get("tournament_name") or ""),
                team_one,
                team_two,
                safe_row.get("start_time"),
                best_of,
                str(safe_row.get("status") or ""),
                self.json(safe_row),
                updated_at.isoformat(),
            ),
        )
        return cursor.rowcount == 1

    def _raybet_teams_for_write(
        self,
        safe_row: dict[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Resolve an explicit pair or reuse one already stored for this match."""
        if (
            classify_raybet_match_format(safe_row) == RAYBET_MATCH_NON_HEAD_TO_HEAD
            and str(safe_row.get("match_short_name") or "").strip().casefold()
            == "outright"
        ):
            raise ValueError("raybet_non_head_to_head_match")
        if "team" in safe_row:
            return strict_raybet_head_to_head_teams(safe_row)

        match_id = str(safe_row.get("id") or "").strip()
        existing = self.connection.execute(
            """SELECT tournament, team_one, team_two, scheduled_at,
                      best_of, status, raw_json
                 FROM raybet_matches WHERE raybet_match_id=?""",
            (match_id,),
        ).fetchone()
        if existing is None:
            raise ValueError("raybet_exact_team_metadata_missing")
        team_one = str(existing["team_one"] or "").strip()
        team_two = str(existing["team_two"] or "").strip()
        if (
            not team_one
            or not team_two
            or team_one.casefold() == team_two.casefold()
        ):
            raise ValueError("raybet_existing_team_identity_invalid")
        try:
            existing_payload = json.loads(str(existing["raw_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("raybet_existing_team_identity_invalid") from error
        if not isinstance(existing_payload, dict):
            raise ValueError("raybet_existing_team_identity_invalid")
        if (
            classify_raybet_match_format(existing_payload)
            == RAYBET_MATCH_NON_HEAD_TO_HEAD
            and str(existing_payload.get("match_short_name") or "")
            .strip()
            .casefold()
            == "outright"
        ):
            raise ValueError("raybet_non_head_to_head_match")
        for key in (
            "game_id",
            "tournament_id",
            "tournament_name",
            "match_name",
            "match_short_name",
            "start_time",
            "round",
            "status",
        ):
            if key not in safe_row and key in existing_payload:
                safe_row[key] = existing_payload[key]
        if "tournament_name" not in safe_row and existing["tournament"]:
            safe_row["tournament_name"] = str(existing["tournament"])
        if "start_time" not in safe_row and existing["scheduled_at"] is not None:
            safe_row["start_time"] = existing["scheduled_at"]
        if "round" not in safe_row and existing["best_of"] is not None:
            safe_row["round"] = f"bo{int(existing['best_of'])}"
        if "status" not in safe_row and existing["status"] is not None:
            safe_row["status"] = existing["status"]
        if "team" in existing_payload:
            existing_one, existing_two = strict_raybet_head_to_head_teams(
                existing_payload
            )
            safe_row["team"] = [dict(existing_one), dict(existing_two)]
        else:
            safe_row["team"] = [
                {"pos": 1, "team_name": team_one},
                {"pos": 2, "team_name": team_two},
            ]
        return strict_raybet_head_to_head_teams(safe_row)

    def insert_browser_event(
        self,
        event: Mapping[str, Any] | Any,
        *,
        received_at: datetime,
        recognized: bool,
        raw_artifact: ArtifactReceipt | None = None,
        processing_status: str = "pending",
        processing_reason: str | None = None,
    ) -> bool:
        captured_at = self._event_value(
            event, "captured_at_utc", self._event_value(event, "captured_at")
        )
        payload = self._event_value(event, "payload", {})
        if raw_artifact is None:
            raw_artifact = self.archive_response_payload(
                payload,
                observed_at=captured_at,
                match_id=self._event_value(event, "raybet_match_id"),
            )
        self._register_raw_artifact(raw_artifact)
        cursor = self.execute(
            """INSERT INTO browser_events
            (event_id, schema_version, capture_session_id, captured_at, received_at,
             transport, event_type, raybet_match_id, game_id, page_origin, page_path,
             source_path, payload_hash, payload_bytes, payload_json,
             payload_artifact_hash, payload_storage, capture_reason,
             extension_version, recognized, processing_status, processing_reason)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT DO NOTHING""",
            (
                str(self._event_value(event, "event_id")),
                int(self._event_value(event, "schema_version")),
                str(self._event_value(event, "capture_session_id")),
                self._iso(captured_at),
                self._iso(received_at),
                str(self._scalar(self._event_value(event, "transport"))),
                str(self._scalar(self._event_value(event, "event_type"))),
                self._event_value(event, "raybet_match_id"),
                self._event_value(event, "game_id"),
                str(self._event_value(event, "page_origin")),
                str(self._event_value(event, "page_path")),
                str(self._event_value(event, "source_path")),
                str(self._event_value(event, "payload_hash")),
                int(self._event_value(event, "payload_bytes")),
                self.json({}),
                raw_artifact.content_sha256,
                "external",
                self._event_value(event, "capture_reason"),
                str(self._event_value(event, "extension_version")),
                int(recognized),
                processing_status,
                processing_reason,
            ),
        )
        return cursor.rowcount == 1

    def browser_event_identity_matches(self, event: Mapping[str, Any] | Any) -> bool:
        """Check immutable retry identity before treating an event ID as duplicate."""
        event_id = str(self._event_value(event, "event_id"))
        row = self.connection.execute(
            """SELECT schema_version, capture_session_id, captured_at, transport,
                      event_type, raybet_match_id, game_id, page_origin, page_path,
                      source_path, payload_hash, payload_bytes, capture_reason,
                      extension_version, payload_storage, payload_artifact_hash,
                      payload_json
                 FROM browser_events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        if row is None:
            return False
        captured_at = self._event_value(
            event, "captured_at_utc", self._event_value(event, "captured_at")
        )
        expected = (
            int(self._event_value(event, "schema_version")),
            str(self._event_value(event, "capture_session_id")),
            self._iso(captured_at),
            str(self._scalar(self._event_value(event, "transport"))),
            str(self._scalar(self._event_value(event, "event_type"))),
            self._event_value(event, "raybet_match_id"),
            self._event_value(event, "game_id"),
            str(self._event_value(event, "page_origin")),
            str(self._event_value(event, "page_path")),
            str(self._event_value(event, "source_path")),
            str(self._event_value(event, "payload_hash")),
            int(self._event_value(event, "payload_bytes")),
            self._event_value(event, "capture_reason"),
            str(self._event_value(event, "extension_version")),
        )
        if tuple(row[:14]) != expected:
            return False
        if str(row["payload_storage"]) == "external":
            try:
                return self._read_raw_artifact(str(row["payload_artifact_hash"])) == self._event_value(
                    event, "payload", {}
                )
            except (RuntimeError, ValueError, TypeError):
                return False
        return self.json(self._event_value(event, "payload", {})) == str(
            row["payload_json"]
        )

    def update_browser_event_status(
        self, event_id: str, status: str, reason: str | None = None
    ) -> bool:
        cursor = self.execute(
            """UPDATE browser_events
               SET processing_status=?, processing_reason=? WHERE event_id=?""",
            (status, reason, event_id),
        )
        return cursor.rowcount == 1

    def observation_timing_status(
        self, raybet_match_id: str, observed_at: datetime, *, source: str
    ) -> str:
        source_filter = " AND source='direct'" if source == "direct" else ""
        newest = self.connection.execute(
            f"""SELECT observed_at FROM odds_transport_observations
               WHERE raybet_match_id=? AND timing_status!='late'
               {source_filter}
               ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (raybet_match_id,),
        ).fetchone()
        if newest and self._iso(observed_at) < str(newest["observed_at"]):
            return "late"
        return "on_time"

    def insert_transport_observation(
        self,
        *,
        observation_key: str,
        source: str,
        source_event_id: str | None,
        raybet_match_id: str,
        observed_at: datetime,
        normalized_state_hash: str,
        response_state_hash: str,
        response_artifact_hash: str,
        timing_status: str,
        processing_status: str,
        normalized_change_count: int,
        normalized_state_hash_version: int = 2,
        original_legacy_normalized_state_hash: str | None = None,
    ) -> bool:
        cursor = self.execute(
            """INSERT INTO odds_transport_observations
            (observation_key, source, source_event_id, raybet_match_id, observed_at,
             normalized_state_hash, normalized_state_hash_version,
             original_legacy_normalized_state_hash, response_state_hash,
             response_artifact_hash, timing_status, processing_status,
             normalized_change_count)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT DO NOTHING""",
            (
                observation_key,
                source,
                source_event_id,
                raybet_match_id,
                self._iso(observed_at),
                normalized_state_hash,
                normalized_state_hash_version,
                original_legacy_normalized_state_hash,
                response_state_hash,
                response_artifact_hash,
                timing_status,
                processing_status,
                normalized_change_count,
            ),
        )
        return cursor.rowcount == 1

    def store_odds_observation(
        self,
        *,
        source: str,
        observation_key: str,
        source_event_id: str | None,
        raybet_match_id: str,
        observed_at: datetime,
        normalized_state_hash: str,
        snapshots: Sequence[OddsSnapshot],
        raw_payload: Mapping[str, Any] | None = None,
        raw_artifact: ArtifactReceipt | None = None,
        audit_only: bool = False,
    ) -> tuple[str, int]:
        """Atomically retain transport, deduplicated state, and exact raw evidence."""
        from .markets import (
            is_closed_odds_member,
            normalized_state_hash as compute_normalized_state_hash,
            snapshots_from_payload,
            snapshot_state_outcome,
        )

        seen_odds_ids: set[str] = set()
        for snapshot in snapshots:
            if snapshot.raybet_match_id != raybet_match_id:
                raise ValueError("response outcome match id mismatch")
            if snapshot.received_at != observed_at:
                raise ValueError("response outcome transport time mismatch")
            if snapshot.odds_id in seen_odds_ids:
                raise ValueError("duplicate odds id in one response")
            if not math.isfinite(snapshot.price):
                raise ValueError("response outcome price must be finite")
            if snapshot.market.line is not None and not math.isfinite(
                float(snapshot.market.line)
            ):
                raise ValueError("response outcome line must be finite")
            seen_odds_ids.add(snapshot.odds_id)

        validated_payload = self._validated_response_payload(
            raw_payload=raw_payload,
            raw_artifact=raw_artifact,
        )
        raw_snapshots = snapshots_from_payload(
            validated_payload,
            received_at=observed_at,
        )
        result = validated_payload.get("result")
        raw_members = result.get("odds") if isinstance(result, dict) else None
        if not isinstance(result, dict) or str(result.get("id") or "") != raybet_match_id:
            raise ValueError("response raw payload match id mismatch")
        closed_member_count = (
            sum(is_closed_odds_member(member) for member in raw_members)
            if isinstance(raw_members, list)
            else 0
        )
        if (
            not isinstance(raw_members, list)
            or len(raw_members) != len(raw_snapshots) + closed_member_count
        ):
            raise ValueError("response raw payload contains unparsed odds members")
        caller_members = canonical_state_outcomes(
            snapshot_state_outcome(snapshot) for snapshot in snapshots
        )
        raw_semantic_members = canonical_state_outcomes(
            snapshot_state_outcome(snapshot) for snapshot in raw_snapshots
        )
        if caller_members != raw_semantic_members:
            raise ValueError("response snapshots do not match raw semantic membership")
        snapshots = raw_snapshots
        raw_payload = validated_payload

        computed_normalized_hash = compute_normalized_state_hash(snapshots)
        if normalized_state_hash != computed_normalized_hash:
            raise ValueError("normalized state hash does not match response membership")
        state_hash, state_values = self._response_state_identity(
            raybet_match_id,
            normalized_state_hash,
            snapshots,
        )
        artifact_hash, _, artifact_receipt = self._response_artifact_identity(
            raybet_match_id,
            snapshots,
            raw_payload=raw_payload,
            raw_artifact=raw_artifact,
        )

        with self.transaction():
            existing = self.connection.execute(
                """SELECT source, source_event_id, raybet_match_id, observed_at,
                          normalized_state_hash, normalized_state_hash_version,
                          original_legacy_normalized_state_hash, response_state_hash,
                          response_artifact_hash, timing_status,
                          normalized_change_count
                   FROM odds_transport_observations WHERE observation_key=?""",
                (observation_key,),
            ).fetchone()
            if existing:
                identity = (
                    str(existing["source"]),
                    existing["source_event_id"],
                    str(existing["raybet_match_id"]),
                    str(existing["observed_at"]),
                    str(existing["normalized_state_hash"]),
                    int(existing["normalized_state_hash_version"]),
                    existing["original_legacy_normalized_state_hash"],
                )
                expected = (
                    source,
                    source_event_id,
                    raybet_match_id,
                    self._iso(observed_at),
                    normalized_state_hash,
                    2,
                    None,
                )
                if identity != expected:
                    raise ValueError("observation key already belongs to another response")
                storage_refs = (
                    existing["response_state_hash"],
                    existing["response_artifact_hash"],
                )
                if storage_refs != (None, None) and storage_refs != (
                    state_hash,
                    artifact_hash,
                ):
                    raise ValueError(
                        "observation key response membership or payload differs"
                    )
                persisted_outcomes = self.connection.execute(
                    """SELECT raybet_match_id, odds_id, odds_group_id, received_at,
                              price, status, market_type, period, side, line,
                              outcome_key, supported, last_update
                         FROM odds_response_outcomes_effective
                        WHERE observation_key=? ORDER BY odds_id""",
                    (observation_key,),
                ).fetchall()
                if not persisted_outcomes:
                    if snapshots:
                        raise ValueError(
                            "observation key response membership or payload differs"
                        )
                actual_outcomes = [tuple(row) for row in persisted_outcomes]
                expected_outcomes = sorted(
                    (
                        self._effective_response_outcome_values(snapshot)
                        for snapshot in snapshots
                    ),
                    key=lambda values: str(values[1]),
                )
                if actual_outcomes != expected_outcomes:
                    raise ValueError(
                        "observation key response membership or payload differs"
                    )
                if storage_refs == (None, None):
                    legacy_raw = self.connection.execute(
                        """SELECT raw_json FROM odds_response_outcomes
                            WHERE observation_key=? ORDER BY odds_id""",
                        (observation_key,),
                    ).fetchall()
                    expected_raw = sorted(
                        self._snapshot_raw_json(snapshot) for snapshot in snapshots
                    )
                    if sorted(str(row[0]) for row in legacy_raw) != expected_raw:
                        raise ValueError(
                            "observation key response membership or payload differs"
                        )
                return str(existing["timing_status"]), 0

            if artifact_receipt is None:
                artifact_receipt = self.archive_response_payload(
                    raw_payload
                    if raw_payload is not None
                    else snapshot_derived_payload(
                        raybet_match_id,
                        (snapshot.raw for snapshot in snapshots),
                    ),
                    observed_at=observed_at,
                    match_id=raybet_match_id,
                )
                if artifact_receipt.content_sha256 != artifact_hash:
                    raise ValueError("response artifact hash mismatch")
            self._register_raw_artifact(artifact_receipt)
            self._persist_response_state(
                state_hash,
                raybet_match_id,
                normalized_state_hash,
                state_values,
            )
            timing_status = self.observation_timing_status(
                raybet_match_id, observed_at, source=source
            )
            processing_status = (
                "audit_only"
                if audit_only or timing_status == "late"
                else "processing"
            )
            inserted = self.insert_transport_observation(
                observation_key=observation_key,
                source=source,
                source_event_id=source_event_id,
                raybet_match_id=raybet_match_id,
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash,
                response_state_hash=state_hash,
                response_artifact_hash=artifact_hash,
                timing_status=timing_status,
                processing_status=processing_status,
                normalized_change_count=0,
            )
            if not inserted:
                return timing_status, 0

            change_count = 0
            if timing_status != "late" and not audit_only:
                change_count = sum(int(self.insert_odds(snapshot)) for snapshot in snapshots)
                processing_status = "processed"
            self.execute(
                """UPDATE odds_transport_observations
                   SET processing_status=?, normalized_change_count=?
                   WHERE observation_key=?""",
                (processing_status, change_count, observation_key),
            )
            return timing_status, change_count

    def _response_state_identity(
        self,
        raybet_match_id: str,
        normalized_state_hash: str,
        snapshots: Sequence[OddsSnapshot],
    ) -> tuple[str, list[tuple[Any, ...]]]:
        state_hash, values, _ = canonical_response_state_identity(
            raybet_match_id,
            normalized_state_hash,
            (
                self._response_state_outcome_values(snapshot)
                for snapshot in snapshots
            ),
        )
        return state_hash, list(values)

    def _validated_response_payload(
        self,
        *,
        raw_payload: Mapping[str, Any] | None,
        raw_artifact: ArtifactReceipt | None,
    ) -> dict[str, Any]:
        if raw_payload is None and raw_artifact is None:
            raise ValueError("exact raw response evidence is required")

        supplied_payload: dict[str, Any] | None = None
        supplied_hash: str | None = None
        supplied_bytes: bytes | None = None
        if raw_payload is not None:
            supplied_hash, supplied_bytes, sanitized = (
                canonical_response_artifact_identity(raw_payload)
            )
            if not isinstance(sanitized, dict):
                raise ValueError("response raw payload must be an object")
            supplied_payload = sanitized

        if raw_artifact is None:
            assert supplied_payload is not None
            return supplied_payload

        if raw_artifact.source != "raybet":
            raise ValueError("RayBet artifact source is required")
        RawArchive._verify(raw_artifact.path, raw_artifact.content_sha256)
        try:
            compressed = raw_artifact.path.read_bytes()
            canonical = gzip.decompress(compressed)
            artifact_payload = json.loads(canonical.decode("utf-8"))
        except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("response raw artifact is corrupt") from error
        artifact_hash, artifact_bytes, sanitized_artifact = (
            canonical_response_artifact_identity(artifact_payload)
        )
        if not isinstance(sanitized_artifact, dict):
            raise ValueError("response raw artifact must contain an object")
        if (
            artifact_hash != raw_artifact.content_sha256
            or artifact_bytes != canonical
            or len(canonical) != raw_artifact.byte_count
            or len(compressed) != raw_artifact.compressed_byte_count
            or schema_fingerprint(artifact_payload) != raw_artifact.schema_fingerprint
        ):
            raise ValueError("response raw artifact metadata mismatch")
        if supplied_payload is not None and (
            supplied_hash != artifact_hash or supplied_bytes != artifact_bytes
        ):
            raise ValueError("response raw artifact does not match payload")
        return sanitized_artifact

    def _response_artifact_identity(
        self,
        raybet_match_id: str,
        snapshots: Sequence[OddsSnapshot],
        *,
        raw_payload: Mapping[str, Any] | None,
        raw_artifact: ArtifactReceipt | None,
    ) -> tuple[str, bytes, ArtifactReceipt | None]:
        if raw_artifact is not None:
            if raw_payload is not None:
                payload_hash, _, _ = canonical_response_artifact_identity(
                    raw_payload
                )
                if payload_hash != raw_artifact.content_sha256:
                    raise ValueError("response raw artifact does not match payload")
            return raw_artifact.content_sha256, b"", raw_artifact
        if raw_payload is None:
            payload: Any = snapshot_derived_payload(
                raybet_match_id,
                (snapshot.raw for snapshot in snapshots),
            )
        else:
            payload = raw_payload
        artifact_hash, encoded, _ = canonical_response_artifact_identity(payload)
        return artifact_hash, encoded, None

    def archive_response_payload(
        self,
        payload: Any,
        *,
        observed_at: datetime,
        match_id: str | None,
        response_kind: str = "live_odds",
        endpoint: str | None = None,
        request_identity: str | None = None,
        status_code: int | None = None,
    ) -> ArtifactReceipt:
        try:
            default_endpoint = _DIRECT_RESPONSE_ENDPOINTS[response_kind]
        except KeyError as error:
            raise ValueError("direct response kind is invalid") from error
        endpoint = endpoint or default_endpoint
        request_identity = request_identity or (
            f"{endpoint}?match_id={match_id}" if match_id else endpoint
        )
        artifact_hash, encoded, _ = canonical_response_artifact_identity(payload)
        numeric_match = int(match_id) if match_id and str(match_id).isdigit() else None
        receipt = self.raw_archive.archive_json(
            source="raybet",
            endpoint=endpoint,
            request_identity=request_identity,
            payload_bytes=encoded,
            observed_at=observed_at,
            match_id=numeric_match,
            status_code=status_code,
        )
        if receipt.content_sha256 != artifact_hash:
            raise RuntimeError("response artifact hash mismatch")
        return receipt

    def _register_raw_artifact(self, receipt: ArtifactReceipt) -> None:
        if receipt.source != "raybet":
            raise ValueError("RayBet artifact source is required")
        RawArchive._verify(receipt.path, receipt.content_sha256)
        try:
            receipt.path.resolve().relative_to(self.raw_archive_root)
        except ValueError:
            try:
                canonical = gzip.decompress(receipt.path.read_bytes())
            except (OSError, EOFError) as error:
                raise ValueError("response artifact is corrupt") from error
            receipt = self.raw_archive.archive_json(
                source="raybet",
                endpoint=receipt.endpoint,
                request_identity=receipt.request_identity,
                payload_bytes=canonical,
                observed_at=receipt.observed_at,
                match_id=receipt.match_id,
                status_code=receipt.status_code,
                source_timestamp=receipt.source_timestamp,
                first_usable_at=receipt.first_usable_at,
            )
        relative_path = receipt.path.resolve().relative_to(self.raw_archive_root)
        self.execute(
            """INSERT INTO odds_raw_artifacts
               (artifact_hash, source, storage_path, uncompressed_bytes,
                compressed_bytes, schema_fingerprint)
               VALUES (?, 'raybet', ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                receipt.content_sha256,
                relative_path.as_posix(),
                receipt.byte_count,
                receipt.compressed_byte_count,
                receipt.schema_fingerprint,
            ),
        )
        row = self.connection.execute(
            """SELECT source, storage_path, uncompressed_bytes,
                      compressed_bytes, schema_fingerprint
                 FROM odds_raw_artifacts WHERE artifact_hash=?""",
            (receipt.content_sha256,),
        ).fetchone()
        if row is None:
            raise RuntimeError("response artifact insert was not durable")
        identity = (
            str(row["source"]),
            int(row["uncompressed_bytes"]),
            int(row["compressed_bytes"]),
            str(row["schema_fingerprint"]),
        )
        expected = (
            "raybet",
            receipt.byte_count,
            receipt.compressed_byte_count,
            receipt.schema_fingerprint,
        )
        if identity != expected:
            raise ValueError("response artifact hash collision or content mismatch")
        existing_relative = Path(str(row["storage_path"]))
        if existing_relative.is_absolute():
            raise ValueError("response artifact path must be relative")
        existing_path = (self.raw_archive_root / existing_relative).resolve()
        try:
            existing_path.relative_to(self.raw_archive_root)
        except ValueError as error:
            raise ValueError("response artifact path escapes archive root") from error
        try:
            existing_content = gzip.decompress(existing_path.read_bytes())
            incoming_content = gzip.decompress(receipt.path.read_bytes())
        except (OSError, EOFError) as error:
            raise ValueError("response artifact is corrupt") from error
        if existing_content != incoming_content:
            raise ValueError("response artifact hash collision or content mismatch")

    def record_direct_response_audit(
        self,
        receipt: ArtifactReceipt,
        *,
        response_kind: str,
        claimed_raybet_match_id: str | None,
        observed_raybet_match_id: str | None,
        disposition: str,
        reason: str,
        provider_code: int | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        payload_kind: str = "provider_response",
        sanitized: bool = True,
    ) -> str:
        if response_kind not in _DIRECT_RESPONSE_ENDPOINTS:
            raise ValueError("direct response kind is invalid")
        if disposition not in {"accepted", "rejected", "audit_only"}:
            raise ValueError("direct response disposition is invalid")
        if payload_kind not in {
            "provider_response",
            "request_failure",
            "aggregate",
        }:
            raise ValueError("direct response payload kind is invalid")
        if type(sanitized) is not bool:
            raise ValueError("direct response sanitized flag must be boolean")
        if provider_code is not None and type(provider_code) is not int:
            raise ValueError("direct response provider code must be an integer")
        metadata = sanitize_raybet_payload(dict(request_metadata or {}))
        if not isinstance(metadata, dict):
            raise ValueError("direct response request metadata must be an object")
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized_reason = " ".join(str(reason).split())[:200]
        if not normalized_reason:
            raise ValueError("direct response reason is required")
        identity = (
            "direct-response-audit-v2",
            response_kind,
            self._iso(receipt.observed_at),
            claimed_raybet_match_id or "",
            observed_raybet_match_id or "",
            receipt.endpoint,
            receipt.request_identity,
            "" if receipt.status_code is None else str(receipt.status_code),
            "" if provider_code is None else str(provider_code),
            metadata_json,
            payload_kind,
            str(int(sanitized)),
            disposition,
            normalized_reason,
            receipt.content_sha256,
        )
        audit_key = hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
        values = (
            audit_key,
            "direct",
            response_kind,
            self._iso(receipt.observed_at),
            claimed_raybet_match_id,
            observed_raybet_match_id,
            receipt.endpoint,
            receipt.request_identity,
            receipt.status_code,
            provider_code,
            metadata_json,
            payload_kind,
            int(sanitized),
            disposition,
            normalized_reason,
            receipt.content_sha256,
        )
        with self.transaction():
            self._register_raw_artifact(receipt)
            self.execute(
                """INSERT INTO direct_response_audit
                   (audit_key, source, response_kind, observed_at,
                    claimed_raybet_match_id, observed_raybet_match_id,
                    endpoint, request_identity, http_status, provider_code,
                    request_metadata_json, payload_kind, sanitized,
                    disposition, reason, artifact_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                values,
            )
            existing = self.connection.execute(
                """SELECT audit_key, source, response_kind, observed_at,
                          claimed_raybet_match_id, observed_raybet_match_id,
                          endpoint, request_identity, http_status, provider_code,
                          request_metadata_json, payload_kind, sanitized,
                          disposition, reason, artifact_hash
                     FROM direct_response_audit WHERE audit_key=?""",
                (audit_key,),
            ).fetchone()
            if existing is None or tuple(existing) != values:
                raise ValueError("direct response audit key was reused")
        return audit_key

    def direct_response_payload(self, audit_key: str) -> Any:
        """Replay the sanitized JSON value for one immutable direct audit."""
        row = self.connection.execute(
            "SELECT artifact_hash FROM direct_response_audit WHERE audit_key=?",
            (audit_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("direct response audit is missing")
        return self._read_raw_artifact(str(row["artifact_hash"]))

    def _read_raw_artifact(self, artifact_hash: str) -> Any:
        return _load_odds_raw_artifact(
            self.connection,
            self.raw_archive_root,
            artifact_hash,
        )

    def browser_event_payload(self, event_id: str) -> dict[str, Any]:
        """Load and revalidate an externalized browser payload."""
        return read_browser_event_payload(
            self.connection,
            self.raw_archive_root,
            event_id,
        )

    def response_raw_payload(self, observation_key: str) -> dict[str, Any]:
        """Return the exact raw response envelope for one transport observation."""
        row = self.connection.execute(
            """SELECT response_artifact_hash, source, raybet_match_id
                 FROM odds_transport_observations WHERE observation_key=?""",
            (observation_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("transport observation is missing")
        if row["response_artifact_hash"] is not None:
            payload = self._read_raw_artifact(str(row["response_artifact_hash"]))
            if not isinstance(payload, dict):
                raise RuntimeError("response raw artifact is not an object")
            return payload
        # Legacy rows have no external artifact. Reconstruct only the exact
        # retained outcome members; callers must treat the envelope as partial.
        raw_rows = self.connection.execute(
            """SELECT raw_json FROM odds_response_outcomes
                WHERE observation_key=? ORDER BY odds_id""",
            (observation_key,),
        ).fetchall()
        odds: list[Any] = []
        for raw_row in raw_rows:
            try:
                value = json.loads(str(raw_row["raw_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("legacy response outcome is invalid") from error
            odds.append(value)
        return {
            "result": {
                "id": str(row["raybet_match_id"]),
                "odds": odds,
            }
        }

    def response_outcomes(
        self,
        observation_key: str,
        *,
        raybet_match_id: str | None = None,
        period: str | None = None,
        include_raw: bool = False,
    ) -> list[dict[str, Any]]:
        """Read one exact response through the v2-first compatibility API."""
        clauses = ["observation_key=?"]
        parameters: list[Any] = [observation_key]
        if raybet_match_id is not None:
            clauses.append("raybet_match_id=?")
            parameters.append(raybet_match_id)
        if period is not None:
            clauses.append("period=?")
            parameters.append(period)
        rows = self.connection.execute(
            """SELECT observation_key, raybet_match_id, odds_id, odds_group_id,
                      received_at, price, status, market_type, period, side,
                      line, outcome_key, supported, last_update, raw_json,
                      response_state_hash, response_artifact_hash, storage_version
                 FROM odds_response_outcomes_effective WHERE """
            + " AND ".join(clauses)
            + " ORDER BY odds_id",
            tuple(parameters),
        ).fetchall()
        result = [dict(row) for row in rows]
        if not include_raw or not result:
            return result
        if result[0]["storage_version"] == "legacy":
            return result
        payload = self.response_raw_payload(observation_key)
        envelope = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        raw_items = envelope.get("odds") if isinstance(envelope, dict) else None
        if not isinstance(raw_items, list):
            raise RuntimeError("response raw artifact has no odds array")
        by_id: dict[str, Any] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("odds_id") or item.get("id") or "")
            if item_id:
                if item_id in by_id:
                    raise RuntimeError("response raw artifact has duplicate odds id")
                by_id[item_id] = item
        for row in result:
            item = by_id.get(str(row["odds_id"]))
            if item is None:
                raise RuntimeError("response raw artifact is missing an outcome")
            row["raw_json"] = json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        return result

    def _persist_response_state(
        self,
        state_hash: str,
        raybet_match_id: str,
        normalized_state_hash: str,
        outcomes: Sequence[tuple[Any, ...]],
        *,
        normalized_state_hash_version: int = 2,
        original_legacy_normalized_state_hash: str | None = None,
    ) -> None:
        conflicts = self.connection.execute(
            """SELECT response_state_hash FROM odds_response_states
                WHERE raybet_match_id=? AND normalized_state_hash=?
                  AND normalized_state_hash_version=?
                  AND original_legacy_normalized_state_hash IS NOT DISTINCT FROM ?""",
            (
                raybet_match_id,
                normalized_state_hash,
                normalized_state_hash_version,
                original_legacy_normalized_state_hash,
            ),
        ).fetchall()
        if conflicts and {str(row[0]) for row in conflicts} != {state_hash}:
            raise ValueError(
                "normalized state hash maps to a different response manifest"
            )
        self.execute(
            """INSERT INTO odds_response_states
               (response_state_hash, raybet_match_id, normalized_state_hash,
                normalized_state_hash_version,
                original_legacy_normalized_state_hash, outcome_count)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                state_hash,
                raybet_match_id,
                normalized_state_hash,
                normalized_state_hash_version,
                original_legacy_normalized_state_hash,
                len(outcomes),
            ),
        )
        for outcome in outcomes:
            self.execute(
                """INSERT INTO odds_response_state_outcomes
                   (response_state_hash, odds_id, odds_group_id, price, status,
                    market_type, period, side, line, outcome_key, supported,
                    last_update)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING""",
                (state_hash, *outcome),
            )
        state = self.connection.execute(
            """SELECT raybet_match_id, normalized_state_hash,
                      normalized_state_hash_version,
                      original_legacy_normalized_state_hash, outcome_count
                 FROM odds_response_states WHERE response_state_hash=?""",
            (state_hash,),
        ).fetchone()
        persisted = self.connection.execute(
            """SELECT odds_id, odds_group_id, price, status, market_type,
                      period, side, line, outcome_key, supported, last_update
                 FROM odds_response_state_outcomes
                WHERE response_state_hash=? ORDER BY odds_id""",
            (state_hash,),
        ).fetchall()
        if state is None or (
            str(state["raybet_match_id"]),
            str(state["normalized_state_hash"]),
            int(state["normalized_state_hash_version"]),
            state["original_legacy_normalized_state_hash"],
            int(state["outcome_count"]),
        ) != (
            raybet_match_id,
            normalized_state_hash,
            normalized_state_hash_version,
            original_legacy_normalized_state_hash,
            len(outcomes),
        ):
            raise ValueError("response state hash collision or content mismatch")
        if [tuple(row) for row in persisted] != list(outcomes):
            raise ValueError("response state hash collision or content mismatch")

    @staticmethod
    def _snapshot_raw_json(snapshot: OddsSnapshot) -> str:
        return json.dumps(
            sanitize_raybet_payload(snapshot.raw),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )

    def _response_state_outcome_values(
        self, snapshot: OddsSnapshot
    ) -> tuple[Any, ...]:
        from .markets import snapshot_state_outcome

        return snapshot_state_outcome(snapshot)

    def _effective_response_outcome_values(
        self, snapshot: OddsSnapshot
    ) -> tuple[Any, ...]:
        state = self._response_state_outcome_values(snapshot)
        return (
            snapshot.raybet_match_id,
            state[0],
            state[1],
            self._iso(snapshot.received_at),
            *state[2:],
        )

    def upsert_match_link(
        self, raybet_match_id: str, provider: str, provider_match_id: str,
        confidence: float, status: str, reason: str, created_at: datetime,
    ) -> None:
        self.execute(
            """INSERT INTO match_links VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raybet_match_id, provider) DO UPDATE SET
              provider_match_id=CASE WHEN match_links.status='accepted'
                THEN match_links.provider_match_id ELSE excluded.provider_match_id END,
              confidence=excluded.confidence, status=CASE WHEN match_links.status='accepted'
                THEN match_links.status ELSE excluded.status END, reason=excluded.reason""",
            (raybet_match_id, provider, provider_match_id, confidence, status, reason,
             created_at.isoformat()),
        )

    def insert_odds(self, snapshot: OddsSnapshot) -> bool:
        market = snapshot.market
        previous = self.connection.execute(
            """SELECT odds_group_id, price, status, market_type, period,
                      side, line, outcome_key, supported, last_update
                 FROM odds_snapshots
            WHERE raybet_match_id=? AND odds_id=? AND received_at<=?
            ORDER BY received_at DESC, id DESC LIMIT 1""",
            (snapshot.raybet_match_id, snapshot.odds_id,
             self._iso(snapshot.received_at)),
        ).fetchone()
        current = (
            snapshot.odds_group_id,
            snapshot.price,
            str(snapshot.status),
            market.market_type,
            market.period,
            market.side,
            market.line,
            market.outcome_key,
            int(market.supported),
            snapshot.last_update,
        )
        if previous and tuple(previous) == current:
            return False
        cursor = self.execute(
            """INSERT INTO odds_snapshots
            (raybet_match_id, odds_id, odds_group_id, received_at, price, status,
             market_type, period, side, line, outcome_key, supported, last_update, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""",
            (snapshot.raybet_match_id, snapshot.odds_id, snapshot.odds_group_id,
              self._iso(snapshot.received_at), snapshot.price, str(snapshot.status),
             market.market_type, market.period, market.side, market.line,
             market.outcome_key, int(market.supported), snapshot.last_update,
             self.json(sanitize_raybet_payload(snapshot.raw))),
        )
        return cursor.rowcount == 1

    def next_fill_candidate(self, order: ShadowOrder) -> DatabaseRow | None:
        """Return the target outcome only from the first eligible response."""
        return self.connection.execute(
            """WITH successor AS (
                   SELECT observation_key, raybet_match_id, observed_at
                     FROM odds_transport_observations
                     WHERE raybet_match_id=? AND source='direct' AND observed_at>?
                      AND timing_status='on_time'
                      AND processing_status='processed'
                    ORDER BY observed_at, observation_key LIMIT 1
               )
               SELECT outcome.*, successor.observed_at AS transport_observed_at,
                      successor.observation_key AS transport_observation_key
                 FROM successor
                 JOIN odds_response_outcomes_effective outcome
                   ON outcome.observation_key=successor.observation_key
                  AND outcome.raybet_match_id=successor.raybet_match_id
                WHERE outcome.odds_id=?""",
            (
                order.raybet_match_id,
                self._iso(order.signal_transport_at),
                order.odds_id,
            ),
        ).fetchone()

    def processed_transport_watermark(
        self, raybet_match_id: str, *, as_of: datetime
    ) -> datetime | None:
        """Return persisted event-time progress, never the worker wall clock."""
        row = self.connection.execute(
            """SELECT observed_at FROM odds_transport_observations
                 WHERE raybet_match_id=? AND source='direct' AND observed_at<=?
                   AND timing_status='on_time'
                   AND processing_status='processed'
                 ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (raybet_match_id, self._iso(as_of)),
        ).fetchone()
        return (
            datetime.fromisoformat(str(row["observed_at"]))
            if row is not None
            else None
        )

    def _signal_identity_matches(self, order: ShadowOrder) -> bool:
        if not order.signal_identity_verified:
            return False
        row = self.connection.execute(
            """SELECT transport.raybet_match_id, transport.observed_at,
                      transport.timing_status, transport.processing_status,
                      outcome.odds_group_id, outcome.outcome_key,
                      outcome.price, outcome.status, outcome.supported,
                      outcome.market_type, outcome.period, outcome.side,
                      outcome.line
                 FROM odds_transport_observations AS transport
                 JOIN odds_response_outcomes_effective AS outcome
                   ON outcome.observation_key=transport.observation_key
                WHERE transport.observation_key=? AND transport.source='direct'
                  AND outcome.raybet_match_id=? AND outcome.odds_id=?""",
            (
                order.signal_transport_key,
                order.raybet_match_id,
                order.odds_id,
            ),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["raybet_match_id"]) == order.raybet_match_id
            and str(row["observed_at"]) == self._iso(order.signal_transport_at)
            and str(row["timing_status"]) == "on_time"
            and str(row["processing_status"]) == "processed"
            and str(row["odds_group_id"] or "") == order.signal_odds_group_id
            and str(row["outcome_key"] or "") == order.signal_outcome_key
            and float(row["price"]) == order.signal_price
            and is_open(row["status"])
            and bool(row["supported"])
            and market_key(
                str(row["market_type"]),
                str(row["period"]),
                row["side"],
                row["line"],
            )
            == market_key(
                order.market.market_type,
                order.market.period,
                order.market.side,
                order.market.line,
            )
        )

    def _signal_market_authority_matches(
        self,
        order: ShadowOrder,
        map_number: int,
    ) -> bool:
        row = self.connection.execute(
            """SELECT market.underdog_side, market.underdog_odds_id,
                      market.underdog_price, market.underdog_probability,
                      market.odds_group_id, market.period
                 FROM trusted_odds_winner_market_authority AS market
                 JOIN odds_transport_observations AS transport
                   ON transport.observation_key=market.observation_key
                  AND transport.raybet_match_id=market.raybet_match_id
                  AND transport.source='direct'
                WHERE market.observation_key=? AND market.raybet_match_id=?
                  AND market.period=?""",
            (
                order.signal_transport_key,
                order.raybet_match_id,
                f"map_{map_number}",
            ),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["underdog_side"]) == order.signal_outcome_key
            and str(row["underdog_side"]) == order.market.side
            and str(row["underdog_odds_id"]) == order.odds_id
            and str(row["odds_group_id"]) == str(order.signal_odds_group_id or "")
            and float(row["underdog_price"]) == order.signal_price
            and abs(
                float(row["underdog_probability"])-order.market_probability
            ) <= 1.0e-12
            and market_key(
                "winner",
                str(row["period"]),
                str(row["underdog_side"]),
                None,
            )
            == market_key(
                order.market.market_type,
                order.market.period,
                order.market.side,
                order.market.line,
            )
        )

    def process_pending_successor(
        self,
        order: ShadowOrder,
        *,
        watermark: datetime,
        max_slippage: float = 0.03,
    ) -> ShadowOrder | None:
        """Resolve a pending order from its exact first visible successor.

        A returned order was transitioned atomically with its map attempt. None
        means that the order remains pending or another worker already resolved it.
        """
        with self.transaction():
            current = self.connection.execute(
                """SELECT raybet_match_id, odds_id, signal_transport_key,
                          signal_transport_at, expires_at,
                          signal_odds_group_id, signal_outcome_key,
                          signal_identity_verified, status
                     FROM shadow_orders WHERE order_key=?""",
                (order.order_key,),
            ).fetchone()
            if current is None:
                raise ValueError("shadow order is not persisted")
            if str(current["status"]) != "pending":
                return None
            persisted_identity = (
                str(current["raybet_match_id"]),
                str(current["odds_id"]),
                str(current["signal_transport_key"]),
                str(current["signal_transport_at"]),
                str(current["expires_at"]),
                current["signal_odds_group_id"],
                current["signal_outcome_key"],
                bool(current["signal_identity_verified"]),
            )
            requested_identity = (
                order.raybet_match_id,
                order.odds_id,
                order.signal_transport_key,
                self._iso(order.signal_transport_at),
                self._iso(order.expires_at),
                order.signal_odds_group_id,
                order.signal_outcome_key,
                order.signal_identity_verified,
            )
            if persisted_identity != requested_identity:
                raise ValueError("shadow order does not match persisted signal identity")

            map_row = self.connection.execute(
                """SELECT raybet_match_id, map_number
                     FROM shadow_map_attempts
                    WHERE order_key=? AND status='pending'""",
                (order.order_key,),
            ).fetchone()
            successor = self.connection.execute(
                """SELECT observation_key, raybet_match_id, observed_at
                     FROM odds_transport_observations
                     WHERE raybet_match_id=? AND source='direct' AND observed_at>?
                      AND observed_at<=?
                      AND timing_status='on_time'
                      AND processing_status='processed'
                    ORDER BY observed_at, observation_key LIMIT 1""",
                (
                    order.raybet_match_id,
                    self._iso(order.signal_transport_at),
                    self._iso(watermark),
                ),
            ).fetchone()
            authority_cutoff = (
                str(successor["observed_at"])
                if successor is not None
                else watermark
            )
            block_reason = (
                self.pending_order_block_reason(
                    order.order_key, as_of=authority_cutoff
                )
                if map_row
                else None
            )
            draft_conflict = block_reason is not None
            signal_is_valid = not draft_conflict and self._signal_identity_matches(order)

            resolved: ShadowOrder | None = None
            if draft_conflict:
                resolved = replace(
                    order,
                    status="rejected",
                    rejection_reason=block_reason or "vision_draft_conflict",
                )
            elif not signal_is_valid:
                resolved = replace(
                    order,
                    status="rejected",
                    rejection_reason="signal_identity_unverified",
                )
            elif successor is not None:
                observed_at = datetime.fromisoformat(str(successor["observed_at"]))
                if observed_at > order.expires_at:
                    resolved = replace(
                        order,
                        status="rejected",
                        rejection_reason="fill_timeout",
                    )
                else:
                    outcome = self.connection.execute(
                        """SELECT * FROM odds_response_outcomes_effective
                            WHERE observation_key=? AND raybet_match_id=?
                              AND odds_id=?""",
                        (
                            str(successor["observation_key"]),
                            order.raybet_match_id,
                            order.odds_id,
                        ),
                    ).fetchone()
                    if outcome is None:
                        resolved = replace(
                            order,
                            status="rejected",
                            rejection_reason="outcome_missing",
                        )
                    else:
                        resolved = attempt_fill(
                            order,
                            self._response_snapshot(outcome),
                            observed_at=observed_at,
                            max_slippage=max_slippage,
                            now=observed_at,
                        )

            if resolved is None or resolved.status == "pending":
                return None
            order_update = self.connection.execute(
                """UPDATE shadow_orders
                      SET status=?, fill_price=?, filled_at=?, rejection_reason=?
                    WHERE order_key=? AND status='pending'""",
                (
                    resolved.status,
                    resolved.fill_price,
                    self._iso(resolved.filled_at) if resolved.filled_at else None,
                    resolved.rejection_reason,
                    resolved.order_key,
                ),
            )
            if order_update.rowcount != 1:
                return None
            if not self.update_map_attempt(
                resolved.order_key, resolved.status, expected_status="pending"
            ):
                raise RuntimeError("pending order has no matching pending map attempt")
            if resolved.status == "filled":
                map_row = self.connection.execute(
                    """SELECT map_number FROM shadow_map_attempts
                        WHERE order_key=?""",
                    (resolved.order_key,),
                ).fetchone()
                if map_row is None or resolved.filled_at is None:
                    raise RuntimeError("filled order is missing map provenance")
                from .notifications import EVENT_FILLED, filled_order_payload

                self.enqueue_notification(
                    order_key=resolved.order_key,
                    event_type=EVENT_FILLED,
                    payload=filled_order_payload(
                        self.connection, resolved.order_key
                    ),
                    stats_cutoff_at=resolved.filled_at,
                    created_at=resolved.filled_at,
                )
            return resolved

    @staticmethod
    def _response_snapshot(row: DatabaseRow) -> OddsSnapshot:
        market = Market(
            str(row["market_type"]),
            str(row["period"]),
            row["side"],
            row["line"],
            str(row["outcome_key"]),
            bool(row["supported"]),
        )
        return OddsSnapshot(
            str(row["raybet_match_id"]),
            str(row["odds_id"]),
            row["odds_group_id"],
            datetime.fromisoformat(str(row["received_at"])),
            float(row["price"]),
            row["status"],
            market,
            row["last_update"],
            (
                json.loads(str(row["raw_json"]))
                if row["raw_json"] is not None
                else {}
            ),
        )

    def insert_frame(self, frame: LiveFrame) -> None:
        self.execute(
            """INSERT INTO live_frames VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""",
            (frame.provider, frame.provider_match_id, frame.provider_game_id,
             frame.sequence or "", frame.source_at.isoformat() if frame.source_at else None,
             frame.received_at.isoformat(), frame.game_time, frame.team_one_kills,
             frame.team_two_kills, frame.team_one_gold, frame.team_two_gold, frame.state,
             self.json(frame.raw)),
        )

    def insert_event(self, event: LiveEvent) -> None:
        self.execute(
            """INSERT INTO live_events VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""",
            (event.provider, event.provider_event_id, event.provider_match_id,
             event.provider_game_id, event.event_type,
             event.source_at.isoformat() if event.source_at else None,
             event.received_at.isoformat(), event.game_time, event.team, event.player,
             event.value, self.json(event.raw)),
        )

    @staticmethod
    def _rosh_score_from_row(row: DatabaseRow) -> RoshLineupScore | None:
        from .stratz_rosh_client import canonical_evidence_hash

        try:
            evidence = json.loads(str(row["evidence_json"]))
            if (
                not isinstance(evidence, dict)
                or canonical_evidence_hash(evidence) != str(row["evidence_hash"])
            ):
                return None
            pure_score = float(row["pure_lineup_score"])
            adjusted_score = (
                float(row["player_adjusted_lineup_score"])
                if row["player_adjusted_lineup_score"] is not None
                else None
            )
            effective_score = float(row["effective_lineup_score"])
            scoring_mode = str(row["scoring_mode"])
            player_coverage_count = int(row["player_coverage_count"])
            if not _valid_rosh_score_evidence(
                evidence,
                pure_score=pure_score,
                adjusted_score=adjusted_score,
                effective_score=effective_score,
                scoring_mode=scoring_mode,
                player_coverage_count=player_coverage_count,
            ):
                return None
            source_as_of = datetime.fromisoformat(str(row["source_as_of"]))
            return RoshLineupScore(
                score_key=str(row["score_key"]),
                draft_hash=str(row["draft_hash"]),
                player_identity_hash=str(row["player_identity_hash"]),
                pure_lineup_score=pure_score,
                player_adjusted_lineup_score=adjusted_score,
                effective_lineup_score=effective_score,
                scoring_mode=scoring_mode,
                player_coverage_count=player_coverage_count,
                stake_multiplier=float(row["stake_multiplier"]),
                formula_version=str(row["formula_version"]),
                source_name=str(row["source_name"]),
                source_week=int(row["source_week"]),
                cache_week_start=int(row["cache_week_start"]),
                source_as_of=source_as_of,
                evidence_hash=str(row["evidence_hash"]),
                evidence=evidence,
            )
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            return None

    @staticmethod
    def rosh_draft_hash(
        radiant_hero_ids: Sequence[int],
        dire_hero_ids: Sequence[int],
    ) -> str:
        radiant = tuple(radiant_hero_ids)
        dire = tuple(dire_hero_ids)
        heroes = (*radiant, *dire)
        if (
            len(radiant) != 5
            or len(dire) != 5
            or any(type(hero_id) is not int or hero_id <= 0 for hero_id in heroes)
            or len(set(heroes)) != 10
        ):
            raise ValueError("Rosh score requires ten unique positive hero IDs")
        payload = LiveBettingStore.json(
            {"radiant": list(radiant), "dire": list(dire)}
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def rosh_player_identity_hash(
        radiant_player_ids: Sequence[int | None] | None = None,
        dire_player_ids: Sequence[int | None] | None = None,
    ) -> str:
        def slots(
            values: Sequence[int | None] | None,
            side: str,
        ) -> list[int | None]:
            result = list(values) if values is not None else [None] * 5
            if len(result) != 5 or any(
                value is not None
                and (type(value) is not int or value <= 0)
                for value in result
            ):
                raise ValueError(
                    f"{side} player IDs must contain five positive IDs or nulls"
                )
            return result

        payload = LiveBettingStore.json(
            {
                "radiant": slots(radiant_player_ids, "radiant"),
                "dire": slots(dire_player_ids, "dire"),
            }
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _trusted_rosh_draft(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        strict_mapping_id: int,
        draft_hash: str,
        radiant_hero_ids: Sequence[int],
        dire_hero_ids: Sequence[int],
        as_of: datetime,
    ) -> bool:
        if (
            not raybet_match_id
            or type(map_number) is not int
            or map_number <= 0
            or type(strict_mapping_id) is not int
            or strict_mapping_id <= 0
            or as_of.tzinfo is None
            or as_of.utcoffset() is None
        ):
            return False
        try:
            calculated_hash = self.rosh_draft_hash(
                radiant_hero_ids, dire_hero_ids
            )
        except ValueError:
            return False
        if calculated_hash != draft_hash:
            return False
        anchor = self.connection.execute(
            """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                      anchored_at, status, conflict_at
                 FROM vision_draft_anchors
                WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        ).fetchone()
        if anchor is None:
            return False
        if (
            str(anchor["draft_hash"]) != draft_hash
            or str(anchor["radiant_hero_ids"])
            != self.json(list(radiant_hero_ids))
            or str(anchor["dire_hero_ids"])
            != self.json(list(dire_hero_ids))
        ):
            return False
        try:
            anchored_at = datetime.fromisoformat(str(anchor["anchored_at"]))
        except ValueError:
            return False
        if anchored_at > as_of:
            return False
        if self._draft_conflict_at_or_before(
            raybet_match_id, map_number, as_of
        ):
            return False
        from .strict_eligibility import query_strict_mapping_snapshot

        mapping_snapshot = query_strict_mapping_snapshot(
            self.connection,
            mapping_id=strict_mapping_id,
            observed_at=as_of,
        )
        return bool(
            mapping_snapshot.eligible
            and mapping_snapshot.mapping is not None
            and mapping_snapshot.mapping.raybet_match_id == raybet_match_id
            and mapping_snapshot.mapping.map_number == map_number
        )

    def find_rosh_lineup_score(
        self,
        *,
        draft_hash: str,
        formula_version: str,
        cache_week_start: int,
        radiant_hero_ids: Sequence[int],
        dire_hero_ids: Sequence[int],
        as_of: datetime,
        radiant_player_ids: Sequence[int | None] | None = None,
        dire_player_ids: Sequence[int | None] | None = None,
    ) -> RoshLineupScore | None:
        """Return a non-future same-week score reusable for an identical draft."""
        if (
            not re.fullmatch(r"[0-9a-f]{64}", draft_hash)
            or not formula_version.strip()
            or type(cache_week_start) is not int
            or cache_week_start <= 0
            or as_of.tzinfo is None
            or as_of.utcoffset() is None
        ):
            return None
        if self.rosh_draft_hash(radiant_hero_ids, dire_hero_ids) != draft_hash:
            return None
        try:
            player_identity_hash = self.rosh_player_identity_hash(
                radiant_player_ids, dire_player_ids
            )
        except ValueError:
            return None
        rows = self.connection.execute(
            """SELECT * FROM rosh_lineup_scores
                WHERE draft_hash=? AND player_identity_hash=?
                  AND formula_version=?
                  AND cache_week_start=?
                  AND live_text_timestamp_utc(source_as_of)>=CAST(? AS timestamptz)
                  AND live_text_timestamp_utc(source_as_of)<=CAST(? AS timestamptz)
                  AND live_text_timestamp_utc(created_at)<=CAST(? AS timestamptz)
                ORDER BY source_as_of DESC, created_at DESC, score_key DESC""",
            (
                draft_hash,
                player_identity_hash,
                formula_version,
                cache_week_start,
                self._iso(as_of - ROSH_LINEUP_CACHE_TTL),
                self._iso(as_of),
                self._iso(as_of),
            ),
        ).fetchall()
        radiant_json = self.json(list(radiant_hero_ids))
        dire_json = self.json(list(dire_hero_ids))
        for row in rows:
            if (
                str(row["radiant_hero_ids_json"]) != radiant_json
                or str(row["dire_hero_ids_json"]) != dire_json
            ):
                continue
            score = self._rosh_score_from_row(row)
            if score is not None and score.source_as_of <= as_of:
                return score
        return None

    def get_rosh_lineup_score_for_trusted_draft(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        strict_mapping_id: int,
        draft_hash: str,
        radiant_hero_ids: Sequence[int],
        dire_hero_ids: Sequence[int],
        as_of: datetime,
        formula_version: str | None = None,
        radiant_player_ids: Sequence[int | None] | None = None,
        dire_player_ids: Sequence[int | None] | None = None,
    ) -> RoshLineupScore | None:
        """Read only a score bound to the exact currently trusted draft."""
        return query_rosh_lineup_score_for_trusted_draft(
            self.connection,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            strict_mapping_id=strict_mapping_id,
            draft_hash=draft_hash,
            radiant_hero_ids=radiant_hero_ids,
            dire_hero_ids=dire_hero_ids,
            as_of=as_of,
            formula_version=formula_version,
            radiant_player_ids=radiant_player_ids,
            dire_player_ids=dire_player_ids,
        )

    def insert_rosh_lineup_score(
        self,
        score: Any,
        *,
        raybet_match_id: str,
        map_number: int,
        strict_mapping_id: int,
        draft_hash: str,
        radiant_hero_ids: Sequence[int],
        dire_hero_ids: Sequence[int],
        created_at: datetime,
        radiant_player_ids: Sequence[int | None] | None = None,
        dire_player_ids: Sequence[int | None] | None = None,
    ) -> RoshLineupScore | None:
        """Append a finite score only when its draft lineage is still trusted."""
        from .stratz_rosh_client import (
            canonical_evidence_hash,
            rosh_cache_week_start,
        )

        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        try:
            pure = float(score.pure_lineup_score)
            adjusted = (
                float(score.player_adjusted_lineup_score)
                if score.player_adjusted_lineup_score is not None
                else None
            )
            effective = float(score.effective_lineup_score)
            coverage = int(score.player_coverage_count)
            stake_multiplier = float(score.stake_cap)
            mode = str(score.scoring_mode)
            formula_version = str(score.formula_version)
            source_name = str(score.source_name)
            source_week = int(score.source_week)
            cache_week_start = int(score.cache_week_start)
            source_as_of = score.source_as_of
            evidence = dict(score.evidence)
            evidence_hash = str(score.evidence_hash)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        if (
            any(not math.isfinite(value) for value in (pure, effective))
            or (adjusted is not None and not math.isfinite(adjusted))
            or source_as_of.tzinfo is None
            or source_as_of.utcoffset() is None
            or source_as_of > created_at
            or source_name != "stratz"
            or not formula_version.strip()
            or canonical_evidence_hash(evidence) != evidence_hash
            or not _valid_rosh_score_evidence(
                evidence,
                pure_score=pure,
                adjusted_score=adjusted,
                effective_score=effective,
                scoring_mode=mode,
                player_coverage_count=coverage,
            )
        ):
            return None
        query_started_at = datetime.fromtimestamp(source_week, tz=timezone.utc)
        if (
            query_started_at > source_as_of
            or source_as_of - query_started_at > ROSH_FETCH_MAX_DURATION
            or cache_week_start != rosh_cache_week_start(query_started_at)
            or evidence.get("source") != source_name
            or evidence.get("source_week") != source_week
            or evidence.get("cache_week_start") != cache_week_start
            or evidence.get("formula_version") != formula_version
            or evidence.get("source_as_of") != self._iso(source_as_of)
        ):
            return None
        try:
            player_identity_hash = self.rosh_player_identity_hash(
                radiant_player_ids, dire_player_ids
            )
        except ValueError:
            return None
        if mode == "player_adjusted":
            invariant = (
                coverage == 10
                and adjusted is not None
                and effective == adjusted
                and stake_multiplier == 1.0
            )
        else:
            invariant = (
                mode == "pure"
                and 0 <= coverage < 10
                and adjusted is None
                and effective == pure
                and stake_multiplier == 0.5
            )
        if not invariant or not self._trusted_rosh_draft(
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            strict_mapping_id=strict_mapping_id,
            draft_hash=draft_hash,
            radiant_hero_ids=radiant_hero_ids,
            dire_hero_ids=dire_hero_ids,
            as_of=created_at,
        ):
            return None
        identity = self.json(
            {
                "draft_hash": draft_hash,
                "evidence_hash": evidence_hash,
                "formula_version": formula_version,
                "map_number": map_number,
                "raybet_match_id": raybet_match_id,
                "strict_mapping_id": strict_mapping_id,
                "player_identity_hash": player_identity_hash,
            }
        )
        score_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.execute(
            """INSERT INTO rosh_lineup_scores
               (score_key, draft_hash, player_identity_hash,
                raybet_match_id, map_number,
                strict_mapping_id, radiant_hero_ids_json,
                dire_hero_ids_json, pure_lineup_score,
                player_adjusted_lineup_score, effective_lineup_score,
                scoring_mode, player_coverage_count, stake_multiplier,
                formula_version, source_name, source_week, cache_week_start,
                source_as_of, evidence_json, evidence_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                score_key,
                draft_hash,
                player_identity_hash,
                raybet_match_id,
                map_number,
                strict_mapping_id,
                self.json(list(radiant_hero_ids)),
                self.json(list(dire_hero_ids)),
                pure,
                adjusted,
                effective,
                mode,
                coverage,
                stake_multiplier,
                formula_version,
                source_name,
                source_week,
                cache_week_start,
                self._iso(source_as_of),
                self.json(evidence),
                evidence_hash,
                self._iso(created_at),
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM rosh_lineup_scores WHERE score_key=?",
            (score_key,),
        ).fetchone()
        return None if row is None else self._rosh_score_from_row(row)

    def insert_order(self, order: ShadowOrder) -> bool:
        """Reject the legacy writer that cannot bind an order to a map mapping."""
        del order
        return False

    def update_order(self, order: ShadowOrder) -> None:
        """Reject the legacy updater that bypasses successor verification."""
        del order
        raise RuntimeError(
            "legacy order updater is disabled; use process_pending_successor"
        )

    def record_collector(
        self, collector: str, *, success_at: datetime | None = None,
        error_at: datetime | None = None, error: str | None = None,
        cursor: str | None = None, gap: bool = False,
    ) -> None:
        self.execute(
            """INSERT INTO collector_runs
            (collector, last_success_at, last_error_at, last_error, cursor, gap_detected)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(collector) DO UPDATE SET
              last_success_at=COALESCE(excluded.last_success_at, collector_runs.last_success_at),
              last_error_at=COALESCE(excluded.last_error_at, collector_runs.last_error_at),
              last_error=excluded.last_error, cursor=COALESCE(excluded.cursor, collector_runs.cursor),
              gap_detected=excluded.gap_detected""",
            (collector, success_at.isoformat() if success_at else None,
             error_at.isoformat() if error_at else None, error, cursor, int(gap)),
        )

    def insert_vision_observation(self, observation: Any) -> bool:
        captured_at = self._iso(observation.captured_at)
        radiant_json = self.json(list(observation.radiant_hero_ids))
        dire_json = self.json(list(observation.dire_hero_ids))
        radiant_team_side = observation.radiant_team_side
        if radiant_team_side not in {None, "team_one", "team_two"}:
            raise ValueError(
                "radiant_team_side must be team_one, team_two, or null"
            )
        frame_sha256 = getattr(observation, "source_frame_sha256", None)
        frame_bytes = getattr(observation, "source_frame_bytes", None)
        frame_path = getattr(observation, "source_frame_path", None)
        frame_values = (frame_sha256, frame_bytes, frame_path)
        if any(value is not None for value in frame_values) and any(
            value is None for value in frame_values
        ):
            raise ValueError("vision frame integrity metadata must be complete")
        frame_receipt = None
        if all(value is not None for value in frame_values):
            frame_receipt = VisionFrameReceipt(
                frame_ref=str(observation.source_frame_ref),
                content_sha256=str(frame_sha256),
                byte_length=int(frame_bytes),
                storage_path=Path(str(frame_path)),
            )
        stored_confirmed = _valid_confirmed_vision_payload(
            observation.radiant_hero_ids,
            observation.dire_hero_ids,
            observation.source_frame_ref,
        ) and bool(observation.is_confirmed) and frame_receipt is not None
        with self.transaction():
            if frame_receipt is not None:
                register_vision_frame_artifact(
                    self.connection,
                    frame_receipt,
                    registered_at=observation.captured_at,
                )
            if stored_confirmed and observation.map_number is not None:
                draft_payload = self.json(
                    {
                        "radiant": list(observation.radiant_hero_ids),
                        "dire": list(observation.dire_hero_ids),
                    }
                )
                draft_hash = hashlib.sha256(draft_payload.encode("utf-8")).hexdigest()
                anchor = self.connection.execute(
                    """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                              radiant_team_side, team_side_anchored_at,
                              team_side_source_frame_ref, anchored_at,
                              source_frame_ref, status, conflict_at
                         FROM vision_draft_anchors
                        WHERE raybet_match_id=? AND map_number=?""",
                    (observation.raybet_match_id, observation.map_number),
                ).fetchone()
                if anchor is None:
                    self.connection.execute(
                        """INSERT INTO vision_draft_anchors
                           (raybet_match_id, map_number, draft_hash,
                            radiant_hero_ids, dire_hero_ids,
                            radiant_team_side, team_side_anchored_at,
                            team_side_source_frame_ref, anchored_at,
                            source_frame_ref, status, conflict_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   'anchored', NULL)""",
                        (
                            observation.raybet_match_id,
                            observation.map_number,
                            draft_hash,
                            radiant_json,
                            dire_json,
                            radiant_team_side,
                            captured_at if radiant_team_side is not None else None,
                            observation.source_frame_ref
                            if radiant_team_side is not None
                            else None,
                            captured_at,
                            observation.source_frame_ref,
                        ),
                    )
                else:
                    stored_confirmed = self._rebuild_vision_draft_anchor(
                        observation, anchor
                    )
            cursor = self.connection.execute(
                """INSERT INTO vision_observations
                (raybet_match_id, map_number, captured_at, game_clock_seconds,
                 is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                 clock_confidence, draft_confidence, source_frame_ref,
                 source_frame_sha256, source_frame_bytes, screen_state,
                 confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT DO NOTHING""",
                (
                    observation.raybet_match_id,
                    observation.map_number,
                    captured_at,
                    observation.game_clock_seconds,
                    None
                    if observation.is_paused is None
                    else int(observation.is_paused),
                    radiant_json,
                    dire_json,
                    radiant_team_side,
                    observation.clock_confidence,
                    observation.draft_confidence,
                    observation.source_frame_ref,
                    None if frame_receipt is None else frame_receipt.content_sha256,
                    None if frame_receipt is None else frame_receipt.byte_length,
                    observation.screen_state,
                    int(stored_confirmed),
                ),
            )
            inserted = cursor.rowcount == 1
        state = observation.comeback_state
        if (
            inserted
            and observation.map_number is not None
            and observation.game_clock_seconds is not None
            and observation.screen_state == "game"
            and observation.clock_confidence >= 0.9
            and state.is_available
            and state.confidence >= 0.9
            and type(state.radiant_net_worth) is int
            and type(state.dire_net_worth) is int
        ):
            try:
                append_live_game_snapshot(
                    self.connection,
                    raybet_match_id=observation.raybet_match_id,
                    map_number=observation.map_number,
                    game_time_seconds=observation.game_clock_seconds,
                    radiant_networth=state.radiant_net_worth,
                    dire_networth=state.dire_net_worth,
                    radiant_kills=state.radiant_kills,
                    dire_kills=state.dire_kills,
                    vision_confidence=min(
                        observation.clock_confidence,
                        state.confidence,
                    ),
                    screenshot_path=observation.source_frame_ref,
                    source="vision",
                    captured_at=observation.captured_at,
                )
            except ValueError:
                pass
        return inserted

    def _invalidate_vision_dependents(
        self,
        raybet_match_id: str,
        map_number: int,
        reason: str,
        conflict_at: str | None,
        *,
        block_reason: str,
        block_actor: str,
    ) -> None:
        """Append fail-closed invalidations for a causal vision cutoff."""
        if not block_reason.strip():
            raise ValueError("block_reason is required")
        if not block_actor.strip():
            raise ValueError("block_actor is required")
        recorded_time = datetime.now(timezone.utc)
        recorded_at = recorded_time.isoformat()
        dependent_queries = (
            (
                "odds_alignment",
                """SELECT alignment.odds_snapshot_id
                     FROM odds_alignments AS alignment
                     JOIN odds_snapshots AS snapshot
                       ON snapshot.id=alignment.odds_snapshot_id
                    WHERE alignment.raybet_match_id=?
                      AND alignment.map_number=?
                      AND (
                            CAST(? AS timestamptz) IS NULL
                            OR live_text_timestamp_utc(snapshot.received_at) IS NULL
                            OR live_text_timestamp_utc(snapshot.received_at)>=
                               CAST(? AS timestamptz)
                            OR live_text_timestamp_utc(
                                   alignment.observation_captured_at
                               ) IS NULL
                            OR live_text_timestamp_utc(
                                   alignment.observation_captured_at
                               )>=CAST(? AS timestamptz)
                      )""",
            ),
            (
                "strategy_decision",
                """SELECT decision_key FROM strategy_decisions
                    WHERE raybet_match_id=? AND map_number=?
                      AND (
                            CAST(? AS timestamptz) IS NULL
                            OR live_text_timestamp_utc(decided_at) IS NULL
                            OR live_text_timestamp_utc(decided_at)>=
                               CAST(? AS timestamptz)
                      )""",
            ),
            (
                "research_prediction",
                """SELECT prediction_key FROM research_live_predictions
                    WHERE raybet_match_id=? AND map_number=?
                      AND (
                            CAST(? AS timestamptz) IS NULL
                            OR live_text_timestamp_utc(observed_at) IS NULL
                            OR live_text_timestamp_utc(observed_at)>=
                               CAST(? AS timestamptz)
                      )""",
            ),
        )
        for dependent_type, query in dependent_queries:
            try:
                rows = self.connection.execute(
                    query,
                    (
                        (raybet_match_id, map_number, conflict_at, conflict_at, conflict_at)
                        if dependent_type == "odds_alignment"
                        else (raybet_match_id, map_number, conflict_at, conflict_at)
                    ),
                ).fetchall()
            except SQLAlchemyError:
                if dependent_type == "strategy_decision":
                    raise
                continue
            self.connection.executemany(
                """INSERT INTO vision_derived_invalidations
                   (dependent_type, dependent_key, raybet_match_id, map_number,
                    reason, block_reason, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING""",
                [
                    (
                        dependent_type,
                        str(row[0]),
                        raybet_match_id,
                        map_number,
                        reason,
                        block_reason,
                        recorded_at,
                    )
                    for row in rows
                ],
            )
        rows = self.connection.execute(
            """SELECT orders.order_key
                 FROM shadow_orders AS orders
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                WHERE attempt.raybet_match_id=? AND attempt.map_number=?
                  AND (
                        CAST(? AS timestamptz) IS NULL
                        OR live_text_timestamp_utc(
                               orders.signal_transport_at
                           ) IS NULL
                        OR live_text_timestamp_utc(
                               orders.signal_transport_at
                           )>=CAST(? AS timestamptz)
                  )""",
            (raybet_match_id, map_number, conflict_at, conflict_at),
        ).fetchall()
        order_keys = [str(row[0]) for row in rows]
        self.connection.executemany(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
                VALUES ('shadow_order', ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING""",
            [
                (
                    order_key,
                    raybet_match_id,
                    map_number,
                    reason,
                    block_reason,
                    recorded_at,
                )
                for order_key in order_keys
            ],
        )
        for order_key in order_keys:
            self.connection.execute(
                """UPDATE settlements
                      SET review_required=1
                    WHERE order_key=?""",
                (order_key,),
            )
            self.connection.execute(
                """UPDATE settlement_reconciliations
                          SET status='manual_review',
                              reason=CASE WHEN status='manual_review'
                                          THEN reason
                                          ELSE ? END,
                              updated_at=?
                        WHERE raybet_match_id=? AND map_number=?""",
                (block_reason, recorded_at, raybet_match_id, map_number),
            )
            outbox_rows = self.connection.execute(
                """SELECT outbox_id FROM notification_outbox
                    WHERE order_key=? AND event_type IN ('filled', 'settled')
                      AND status IN ('pending', 'leased')""",
                (order_key,),
            ).fetchall()
            for outbox_row in outbox_rows:
                outbox_id = int(outbox_row[0])
                self.quarantine_notification(
                    outbox_id=outbox_id,
                    reason=block_reason,
                    actor=block_actor,
                    now=recorded_time,
                )

    def _invalidate_draft_dependents(
        self,
        raybet_match_id: str,
        map_number: int,
        reason: str,
        conflict_at: str | None,
    ) -> None:
        """Append fail-closed invalidations for a draft conflict."""
        self._invalidate_vision_dependents(
            raybet_match_id,
            map_number,
            reason,
            conflict_at,
            block_reason="vision_draft_conflict",
            block_actor="vision_conflict",
        )
        self._review_settlements_after_draft_conflict(
            raybet_match_id, map_number, conflict_at
        )

    def _review_settlements_after_draft_conflict(
        self,
        raybet_match_id: str,
        map_number: int,
        conflict_at: str | None,
    ) -> None:
        """Quarantine results observed after a draft conflict event-time."""
        recorded_time = datetime.now(timezone.utc)
        recorded_at = recorded_time.isoformat()
        rows = self.connection.execute(
            """SELECT settlement.order_key
                 FROM settlements AS settlement
                 JOIN shadow_orders AS orders
                   ON orders.order_key=settlement.order_key
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                WHERE attempt.raybet_match_id=? AND attempt.map_number=?
                  AND (
                        CAST(? AS timestamptz) IS NULL
                        OR live_text_timestamp_utc(
                               settlement.settled_at
                           ) IS NULL
                        OR live_text_timestamp_utc(
                               settlement.settled_at
                           )>=CAST(? AS timestamptz)
                  )""",
            (raybet_match_id, map_number, conflict_at, conflict_at),
        ).fetchall()
        order_keys = [str(row["order_key"]) for row in rows]
        for order_key in order_keys:
            self.connection.execute(
                """UPDATE settlements SET review_required=1
                    WHERE order_key=?""",
                (order_key,),
            )
            outbox_rows = self.connection.execute(
                """SELECT outbox_id FROM notification_outbox
                    WHERE order_key=? AND event_type='settled'
                      AND status IN ('pending', 'leased')""",
                (order_key,),
            ).fetchall()
            for outbox_row in outbox_rows:
                outbox_id = int(outbox_row["outbox_id"])
                self.quarantine_notification(
                    outbox_id=outbox_id,
                    reason="vision_draft_conflict",
                    actor="vision_conflict",
                    now=recorded_time,
                )
        self.connection.execute(
            """UPDATE settlement_reconciliations
                  SET status='manual_review',
                      reason=CASE WHEN status='manual_review'
                                  THEN reason
                                  ELSE 'vision_draft_conflict' END,
                      updated_at=?
                WHERE raybet_match_id=? AND map_number=?
                  AND (
                        CAST(? AS timestamptz) IS NULL
                        OR live_text_timestamp_utc(first_observed_at) IS NULL
                        OR live_text_timestamp_utc(first_observed_at)>=
                           CAST(? AS timestamptz)
                  )""",
            (recorded_at, raybet_match_id, map_number, conflict_at, conflict_at),
        )

    def insert_alignment(self, alignment: Any) -> bool:
        values = (
            alignment.odds_snapshot_id,
            alignment.raybet_match_id,
            alignment.map_number,
            alignment.game_clock_seconds,
            alignment.observation_captured_at.isoformat()
            if alignment.observation_captured_at
            else None,
            alignment.method,
            alignment.lag_seconds,
            int(alignment.usable),
            alignment.reason,
        )
        with self.transaction():
            existing = self.connection.execute(
                "SELECT * FROM odds_alignments WHERE odds_snapshot_id=?",
                (alignment.odds_snapshot_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise ValueError("odds alignment identity conflict")
                return False
            if alignment.map_number is not None:
                snapshot = self.connection.execute(
                    "SELECT received_at FROM odds_snapshots WHERE id=?",
                    (alignment.odds_snapshot_id,),
                ).fetchone()
                blocked = self._draft_conflict_at_or_before(
                    str(alignment.raybet_match_id),
                    int(alignment.map_number),
                    alignment.observation_captured_at,
                )
                if snapshot is not None:
                    blocked = blocked or self._draft_conflict_at_or_before(
                        str(alignment.raybet_match_id),
                        int(alignment.map_number),
                        snapshot["received_at"],
                    )
                elif self._draft_conflict_state(
                    str(alignment.raybet_match_id), int(alignment.map_number)
                )[0]:
                    blocked = True
                if blocked:
                    return False
            self.connection.execute(
                """INSERT INTO odds_alignments VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            return True

    def insert_decision(
        self,
        decision: Any,
        *,
        draft_authority: DraftLandmarkAuthority | None = None,
        vision_observation: Any | None = None,
        vision_transport_key: str | None = None,
    ) -> bool:
        strategy_version = str(getattr(decision, "strategy_version", ""))
        contract_value = _decision_contract(decision)
        contract = None
        if strategy_version in REGISTERED_STRATEGY_CONTRACTS:
            contract = validate_strategy_contract(strategy_version, contract_value)
            if contract is None:
                return False
        stored_contributions = dict(decision.contributions)
        decision_inputs = getattr(decision, "inputs", None)
        if isinstance(decision_inputs, Mapping):
            stored_contributions.setdefault("__inputs__", dict(decision_inputs))
            conservative = decision_inputs.get("conservative_contributions")
            if isinstance(conservative, Mapping):
                stored_contributions.setdefault(
                    "__conservative__", dict(conservative)
                )
        try:
            contributions_json = (
                serialize_decision_payload(
                    stored_contributions,
                    strategy_version=strategy_version,
                )
                if strategy_version in REGISTERED_STRATEGY_CONTRACTS
                else self.json(stored_contributions)
            )
        except (TypeError, ValueError):
            return False
        columns = (
            "decision_key",
            "raybet_match_id",
            "map_number",
            "decided_at",
            "underdog_side",
            "market_probability",
            "model_probability",
            "edge",
            "data_quality",
            "eligible",
            "reason",
            "contributions_json",
            "input_ref",
            "strategy_version",
            *_DRAFT_AUTHORITY_COLUMNS,
            *_VISION_AUTHORITY_COLUMNS,
        )
        base_values = (
            decision.decision_key,
            decision.raybet_match_id,
            decision.map_number,
            decision.decided_at.isoformat(),
            decision.underdog_side,
            decision.market_probability,
            decision.model_probability,
            decision.edge,
            decision.data_quality,
            int(decision.eligible),
            decision.reason,
            contributions_json,
            decision.input_ref,
            decision.strategy_version,
        )
        requires_bound_authority = bool(
            decision.eligible
        ) or _has_scored_decision_contributions(decision)
        with self.transaction():
            if contract is not None:
                expected_identity = (
                    contract.evaluator_hash,
                    contract.policy_hash,
                    contract.serialization_version,
                )
                existing_rows = self.connection.execute(
                    """SELECT contributions_json FROM strategy_decisions
                         WHERE strategy_version=?""",
                    (strategy_version,),
                ).fetchall()
                for existing in existing_rows:
                    try:
                        payload = parse_decision_payload(
                            str(existing["contributions_json"]),
                            strategy_version=strategy_version,
                        )
                        existing_value = payload["__inputs__"]["strategy_contract"]
                    except (KeyError, TypeError, ValueError):
                        return False
                    identity = _contract_identity(existing_value)
                    if identity is None or identity != expected_identity:
                        return False
            bound_authority: DraftLandmarkAuthority | None = None
            if isinstance(draft_authority, DraftLandmarkAuthority):
                if draft_landmark_authority_matches(
                    self.connection,
                    draft_authority,
                    raybet_match_id=str(decision.raybet_match_id),
                    map_number=int(decision.map_number),
                    strict_mapping_id=draft_authority.strict_mapping_id,
                    radiant_hero_ids=None,
                    dire_hero_ids=None,
                    observed_at=decision.decided_at,
                    require_current_revisions=True,
                    verify_curve=False,
                ):
                    bound_authority = draft_authority
            if requires_bound_authority and bound_authority is None:
                return False
            bound_vision: VisionDecisionAuthority | None = None
            if (
                requires_bound_authority
                and bound_authority is not None
                and vision_observation is not None
                and isinstance(vision_transport_key, str)
            ):
                bound_vision = self._derive_decision_vision_authority(
                    decision,
                    vision_observation=vision_observation,
                    vision_transport_key=vision_transport_key,
                    draft_authority=bound_authority,
                )
            if requires_bound_authority and bound_vision is None:
                return False
            values = (
                *base_values,
                *_draft_authority_values(bound_authority),
                *_vision_authority_values(bound_vision),
            )
            existing = self.connection.execute(
                f"SELECT {', '.join(columns)} FROM strategy_decisions "
                "WHERE decision_key=?",
                (decision.decision_key,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise ValueError("strategy decision identity conflict")
                return False
            if self._draft_conflict_at_or_before(
                str(decision.raybet_match_id),
                int(decision.map_number),
                decision.decided_at,
            ):
                return False
            self.connection.execute(
                f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            return True

    def insert_official_rosh_shadow_evaluation(
        self,
        evaluation: Any,
        *,
        raybet_match_id: str,
        map_number: int,
        decided_at: datetime,
        transport_key: str,
        observation_draft_hash: str,
        source_run_id: str | None,
    ) -> tuple[str, bool] | None:
        """Append one v6 M3-C shadow record without touching decision/order rows."""

        if not isinstance(raybet_match_id, str) or not raybet_match_id.strip():
            raise ValueError("raybet_match_id must be non-empty")
        if type(map_number) is not int or map_number <= 0:
            raise ValueError("map_number must be a positive integer")
        if (
            not isinstance(decided_at, datetime)
            or decided_at.tzinfo is None
            or decided_at.utcoffset() is None
        ):
            raise ValueError("decided_at must be timezone-aware")
        if not isinstance(transport_key, str) or not transport_key.strip():
            raise ValueError("transport_key must be non-empty")
        if not re.fullmatch(r"[0-9a-f]{64}", observation_draft_hash):
            raise ValueError("observation_draft_hash must be a SHA-256 digest")
        if source_run_id is not None and not re.fullmatch(
            r"[0-9a-f]{64}", source_run_id
        ):
            raise ValueError("source_run_id must be a SHA-256 digest")

        strategy_version = str(getattr(evaluation, "strategy_version", ""))
        if strategy_version != OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION:
            raise ValueError("unregistered official Rosh strategy version")
        as_record = getattr(evaluation, "as_record", None)
        if not callable(as_record):
            raise ValueError("official Rosh evaluation record is unavailable")
        record = as_record()
        if not isinstance(record, Mapping):
            raise ValueError("official Rosh evaluation record must be a mapping")
        candidate_hash = str(record.get("candidate_hash", ""))
        status = str(record.get("status", ""))
        reason = str(record.get("reason", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_hash):
            raise ValueError("candidate_hash must be a SHA-256 digest")
        if status not in {"shadow_candidate", "rejected"} or not reason:
            raise ValueError("official Rosh evaluation status is invalid")
        if any(
            record.get(field) is not None
            for field in (
                "calibration_artifact_ref",
                "calibrated_probability",
                "edge",
                "stake_multiplier",
                "paper_order",
            )
        ):
            raise ValueError("v6 evaluation must not contain probability, stake, or order")
        if validate_official_rosh_strategy_contract(
            strategy_version,
            record.get("strategy_contract"),
        ) is None:
            raise ValueError("official Rosh strategy contract is invalid")
        cohort = record.get("cohort")
        if not isinstance(cohort, Mapping) or dict(cohort) != {
            "m3_c": "shadow_candidate_or_rejection",
            "m3_e": None,
        }:
            raise ValueError("official Rosh evaluation cohort is invalid")
        evidence = record.get("rosh_direction_evidence")
        if evidence is not None:
            if not isinstance(evidence, Mapping):
                raise ValueError("Rosh direction evidence must be a mapping")
            if evidence.get("draft_hash") != observation_draft_hash:
                raise ValueError("Rosh direction evidence draft hash mismatch")
            if evidence.get("analysis_run_id") != source_run_id:
                raise ValueError("Rosh direction evidence run identity mismatch")
        if status == "shadow_candidate" and evidence is None:
            raise ValueError("shadow candidate requires Rosh direction evidence")

        decided_at_value = decided_at.astimezone(timezone.utc).isoformat()
        context = {
            "schema": "official-rosh-shadow-evaluation-context/v1",
            "candidate_hash": candidate_hash,
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "decided_at": decided_at_value,
            "transport_key": transport_key,
            "observation_draft_hash": observation_draft_hash,
            "source_run_id": source_run_id,
            "strategy_version": strategy_version,
        }
        evaluation_key = hashlib.sha256(canonical_bytes(context)).hexdigest()
        record_json = canonical_bytes(record).decode("utf-8")
        columns = (
            "evaluation_key",
            "candidate_hash",
            "raybet_match_id",
            "map_number",
            "decided_at",
            "transport_key",
            "observation_draft_hash",
            "source_run_id",
            "strategy_version",
            "status",
            "reason",
            "record_json",
        )
        values = (
            evaluation_key,
            candidate_hash,
            raybet_match_id,
            map_number,
            decided_at_value,
            transport_key,
            observation_draft_hash,
            source_run_id,
            strategy_version,
            status,
            reason,
            record_json,
        )
        with self.transaction():
            existing = self.connection.execute(
                f"SELECT {', '.join(columns)} "
                "FROM official_rosh_shadow_evaluations WHERE evaluation_key=?",
                (evaluation_key,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise ValueError("official Rosh evaluation identity conflict")
                return evaluation_key, False
            if self._draft_conflict_at_or_before(
                raybet_match_id,
                map_number,
                decided_at,
            ):
                return None
            self.connection.execute(
                f"INSERT INTO official_rosh_shadow_evaluations "
                f"({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            return evaluation_key, True

    def insert_research_prediction(self, prediction: Any) -> bool:
        """Append one non-actionable live prediction without touching order tables."""
        with self.transaction():
            if self._draft_conflict_at_or_before(
                str(prediction.raybet_match_id),
                int(prediction.map_number),
                prediction.observed_at,
            ):
                return False
            authority = getattr(prediction, "draft_authority", None)
            if authority is not None and not isinstance(
                authority, DraftLandmarkAuthority
            ):
                return False
            authority_valid = authority is not None and (
                draft_landmark_authority_matches(
                    self.connection,
                    authority,
                    raybet_match_id=str(prediction.raybet_match_id),
                    map_number=int(prediction.map_number),
                    strict_mapping_id=int(prediction.strict_mapping_id),
                    radiant_hero_ids=tuple(prediction.radiant_hero_ids),
                    dire_hero_ids=tuple(prediction.dire_hero_ids),
                    observed_at=prediction.observed_at,
                    require_current_revisions=True,
                    verify_curve=False,
                )
            )
            if prediction.gate_status == "passed" and (
                not authority_valid
                or prediction.feature_hash != authority.feature_hash
                or prediction.model_hash != authority.model_hash
                or prediction.calibration_hash != authority.calibration_hash
            ):
                return False
            bound_authority = authority if authority_valid else None
            authority_columns = ", ".join(_DRAFT_AUTHORITY_COLUMNS)
            authority_placeholders = ", ".join(
                "?" for _ in _DRAFT_AUTHORITY_COLUMNS
            )
            cursor = self.connection.execute(
                f"""INSERT INTO research_live_predictions
               (prediction_key, schema_version, raybet_match_id, map_number,
                observed_at, game_clock_seconds, game_minute, selected_side,
                market_probability, market_price, raw_model_probability,
                feature_hash, model_hash, calibration_hash, transport_key,
                transport_hash, radiant_hero_ids_json, dire_hero_ids_json,
                radiant_team_side, strict_mapping_id, clock_source, clock_trust,
                manual_clock_event_id, manual_clock_seconds, manual_clock_trust,
                manual_clock_validation, actionability, gate_status,
                gate_failures_json, input_context_hash, created_at,
                {authority_columns})
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       {authority_placeholders})
               ON CONFLICT(prediction_key) DO NOTHING""",
                (
                    prediction.prediction_key,
                    prediction.schema_version,
                    prediction.raybet_match_id,
                    prediction.map_number,
                    self._iso(prediction.observed_at),
                    prediction.game_clock_seconds,
                    prediction.game_minute,
                    prediction.selected_side,
                    prediction.market_probability,
                    prediction.market_price,
                    prediction.raw_model_probability,
                    prediction.feature_hash,
                    prediction.model_hash,
                    prediction.calibration_hash,
                    prediction.transport_key,
                    prediction.transport_hash,
                    self.json(list(prediction.radiant_hero_ids)),
                    self.json(list(prediction.dire_hero_ids)),
                    prediction.radiant_team_side,
                    prediction.strict_mapping_id,
                    prediction.clock_source,
                    prediction.clock_trust,
                    prediction.manual_clock_event_id,
                    prediction.manual_clock_seconds,
                    prediction.manual_clock_trust,
                    prediction.manual_clock_validation,
                    prediction.actionability,
                    prediction.gate_status,
                    self.json(list(prediction.gate_failures)),
                    prediction.input_context_hash,
                    self._iso(prediction.created_at),
                    *_draft_authority_values(bound_authority),
                ),
            )
            return cursor.rowcount == 1

    def insert_research_price_label(self, label: Any) -> bool:
        if self.connection.execute(
            """SELECT 1
                 FROM research_live_predictions AS prediction
                 JOIN odds_transport_observations AS transport
                   ON transport.observation_key=?
                  AND transport.raybet_match_id=prediction.raybet_match_id
                  AND transport.observed_at=?
                  AND transport.source='direct'
                WHERE prediction.prediction_key=?""",
            (
                label.transport_key,
                self._iso(label.observed_at),
                label.prediction_key,
            ),
        ).fetchone() is None:
            return False
        cursor = self.execute(
            """INSERT INTO research_price_labels
               (label_key, prediction_key, transport_key, transport_hash,
                observed_at, selected_side, price, market_probability,
                seconds_after_prediction, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(prediction_key) DO NOTHING""",
            (
                label.label_key,
                label.prediction_key,
                label.transport_key,
                label.transport_hash,
                self._iso(label.observed_at),
                label.selected_side,
                label.price,
                label.market_probability,
                label.seconds_after_prediction,
                self._iso(label.created_at),
            ),
        )
        return cursor.rowcount == 1

    def reserve_map_attempt(
        self, raybet_match_id: str, map_number: int, order_key: str,
        status: str, created_at: datetime,
    ) -> bool:
        cursor = self.execute(
            """INSERT INTO shadow_map_attempts VALUES (?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (raybet_match_id, map_number, order_key, status, created_at.isoformat()),
        )
        return cursor.rowcount == 1

    def update_map_attempt(
        self,
        order_key: str,
        status: str,
        *,
        expected_status: str | None = None,
    ) -> bool:
        if expected_status is None:
            cursor = self.execute(
                "UPDATE shadow_map_attempts SET status=? WHERE order_key=?",
                (status, order_key),
            )
        else:
            cursor = self.execute(
                """UPDATE shadow_map_attempts SET status=?
                    WHERE order_key=? AND status=?""",
                (status, order_key, expected_status),
            )
        return cursor.rowcount == 1

    def has_map_attempt(self, raybet_match_id: str, map_number: int) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM shadow_map_attempts
               WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        ).fetchone()
        return row is not None

    def pending_order_has_draft_conflict(self, order_key: str) -> bool:
        return self.pending_order_block_reason(order_key) is not None

    def pending_order_block_reason(
        self,
        order_key: str,
        *,
        as_of: datetime | str | None = None,
    ) -> str | None:
        row = self.connection.execute(
            """SELECT orders.*, attempt.map_number AS attempt_map_number
                 FROM shadow_orders AS orders
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                WHERE attempt.order_key=? AND attempt.status='pending'
            """,
            (order_key,),
        ).fetchone()
        if row is None:
            return None
        historical_reason = self.order_block_reason(order_key)
        if historical_reason is not None:
            return historical_reason
        authority = authority_from_row(row)
        lineage = self.connection.execute(
            """SELECT decision_key FROM shadow_order_decision_lineage
                WHERE order_key=?""",
            (order_key,),
        ).fetchone()
        decision_authority = (
            None
            if lineage is None
            else self._decision_draft_authority(str(lineage[0]))
        )
        if authority is None or authority != decision_authority:
            return "draft_authority_unverifiable"
        try:
            authority_valid = draft_landmark_authority_matches(
                self.connection,
                authority,
                raybet_match_id=str(row["raybet_match_id"]),
                map_number=int(row["attempt_map_number"]),
                strict_mapping_id=int(row["strict_mapping_id"]),
                radiant_hero_ids=None,
                dire_hero_ids=None,
                observed_at=datetime.fromisoformat(
                    str(row["signal_transport_at"])
                ),
                require_current_revisions=False,
                verify_curve=False,
            )
        except (TypeError, ValueError):
            authority_valid = False
        if not authority_valid:
            return "draft_authority_unverifiable"
        strict = self._strict_mapping_block_reason_for_order(order_key)
        if strict is not None:
            return strict
        derived = self._vision_derived_block_reason(order_key)
        if derived is not None:
            return derived
        if self._vision_observation_invalidated_at_or_before(
            str(row["raybet_match_id"]),
            int(row["attempt_map_number"]),
            as_of,
        ):
            return "vision_observation_invalidated"
        if self._draft_conflict_at_or_before(
            str(row["raybet_match_id"]),
            int(row["attempt_map_number"]),
            as_of,
        ):
            return "vision_draft_conflict"
        return None

    def reject_pending_order(
        self,
        order: ShadowOrder,
        *,
        reason: str,
    ) -> ShadowOrder | None:
        """Atomically reject one persisted pending order without scheduling mail."""
        if not reason.strip():
            raise ValueError("rejection reason is required")
        resolved = replace(
            order,
            status="rejected",
            fill_price=None,
            filled_at=None,
            rejection_reason=reason,
        )
        with self.transaction():
            cursor = self.connection.execute(
                """UPDATE shadow_orders
                      SET status='rejected', fill_price=NULL, filled_at=NULL,
                          rejection_reason=?
                    WHERE order_key=? AND status='pending'""",
                (reason, order.order_key),
            )
            if cursor.rowcount != 1:
                return None
            if not self.update_map_attempt(
                order.order_key, "rejected", expected_status="pending"
            ):
                raise RuntimeError("pending order has no matching pending map attempt")
        return resolved

    def _matching_strategy_decision_candidates(
        self,
        order: ShadowOrder,
        map_number: int,
        strict_mapping_id: int | None = None,
    ) -> list[tuple[str, int]]:
        """Find decisions that cryptographically own an order."""
        try:
            rows = self.connection.execute(
                """SELECT decision.decision_key, decision.strategy_version,
                          decision.input_ref, decision.draft_strict_mapping_id
                     FROM strategy_decisions AS decision
                     JOIN verified_strategy_decision_vision_authority AS vision
                       ON vision.decision_key=decision.decision_key
                     JOIN odds_transport_observations AS transport
                       ON transport.observation_key=decision.vision_transport_key
                      AND transport.raybet_match_id=decision.raybet_match_id
                      AND transport.observed_at=decision.vision_transport_at
                      AND transport.source='direct'
                    WHERE decision.raybet_match_id=? AND decision.map_number=?
                      AND decision.decided_at=? AND decision.underdog_side=?
                      AND decision.eligible=1 AND decision.model_probability=?
                      AND decision.market_probability=?
                    ORDER BY decision.decision_key""",
                (
                    order.raybet_match_id,
                    map_number,
                    self._iso(order.signal_transport_at),
                    order.signal_outcome_key,
                    order.model_probability,
                    order.market_probability,
                ),
            ).fetchall()
        except SQLAlchemyError:
            return []
        matches: list[tuple[str, int]] = []
        for row in rows:
            identity = "|".join(
                (
                    order.raybet_match_id,
                    order.odds_id,
                    order.signal_odds_group_id or "",
                    order.signal_outcome_key or "",
                    market_key(
                        order.market.market_type,
                        order.market.period,
                        order.market.side,
                        order.market.line,
                    ),
                    str(row["strategy_version"]),
                    str(row["input_ref"]),
                    str(float(order.stake)),
                )
            )
            if hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32] != order.order_key:
                continue
            try:
                mapping_id = int(row["draft_strict_mapping_id"])
            except (TypeError, ValueError):
                continue
            if mapping_id <= 0 or (
                strict_mapping_id is not None and mapping_id != strict_mapping_id
            ):
                continue
            matches.append((str(row["decision_key"]), mapping_id))
        return matches

    def _matching_strategy_decision_key(
        self,
        order: ShadowOrder,
        map_number: int,
        strict_mapping_id: int,
    ) -> str | None:
        matches = self._matching_strategy_decision_candidates(
            order, map_number, strict_mapping_id
        )
        return matches[0][0] if len(matches) == 1 else None

    def current_draft_authority(self) -> tuple[int, int]:
        row = self.connection.execute(
            """SELECT authority.authority_revision,
                      lineage.dependency_revision
                 FROM draft_authority_revisions AS authority
                 JOIN draft_lineage_revisions AS lineage
                   ON lineage.singleton=authority.singleton
                WHERE authority.singleton=1"""
        ).fetchone()
        if (
            row is None
            or any(type(value) is not int or value < 1 for value in row)
        ):
            raise RuntimeError("draft authority revisions are unavailable")
        return int(row[0]), int(row[1])

    def _decision_draft_authority(
        self,
        decision_key: str,
    ) -> DraftLandmarkAuthority | None:
        row = self.connection.execute(
            """SELECT * FROM strategy_decisions
                WHERE decision_key=?""",
            (decision_key,),
        ).fetchone()
        return None if row is None else authority_from_row(row)

    def insert_map_order(
        self,
        order: ShadowOrder,
        map_number: int,
        *,
        strict_mapping_id: int,
        draft_authority: (
            DraftLandmarkAuthority | tuple[int, int] | None
        ) = None,
        decision_key: str | None = None,
    ) -> bool:
        """Atomically reserve a map and persist its only shadow order."""
        if (
            isinstance(strict_mapping_id, bool)
            or not isinstance(strict_mapping_id, int)
            or strict_mapping_id <= 0
        ):
            raise ValueError("strict_mapping_id must be a positive integer")
        if (
            order.status != "pending"
            or order.fill_price is not None
            or order.filled_at is not None
            or order.rejection_reason is not None
        ):
            return False
        numeric_values = (
            order.model_probability,
            order.market_probability,
            order.signal_price,
            order.stake,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in numeric_values
        ):
            return False
        if (
            not 0.0 <= float(order.model_probability) <= 1.0
            or not 0.0 <= float(order.market_probability) <= 1.0
            or float(order.signal_price) <= 1.0
            or not 0.0 < float(order.stake) <= 1.0
        ):
            return False
        if (
            not self._signal_identity_matches(order)
            or not self._signal_market_authority_matches(order, map_number)
        ):
            return False
        if decision_key is not None:
            decision_key = str(decision_key).strip()
            if not decision_key:
                raise ValueError("decision_key must be non-empty when provided")
        with self.transaction():
            if self._strict_mapping_context_block_reason(
                strict_mapping_id=strict_mapping_id,
                raybet_match_id=order.raybet_match_id,
                map_number=map_number,
                signal_transport_at=order.signal_transport_at,
            ) is not None:
                return False
            if self._draft_conflict_at_or_before(
                order.raybet_match_id,
                map_number,
                order.signal_transport_at,
            ):
                return False
            matched_decision_key = self._matching_strategy_decision_key(
                order, map_number, strict_mapping_id
            )
            if matched_decision_key is None or (
                decision_key is not None and decision_key != matched_decision_key
            ):
                return False
            decision_key = matched_decision_key
            persisted_authority = self._decision_draft_authority(decision_key)
            persisted_vision = self._decision_vision_authority(decision_key)
            if persisted_authority is None or persisted_vision is None:
                return False
            if (
                persisted_vision.transport_key != order.signal_transport_key
                or persisted_vision.transport_at
                != self._iso(order.signal_transport_at)
            ):
                return False
            if isinstance(draft_authority, DraftLandmarkAuthority):
                if draft_authority != persisted_authority:
                    return False
            elif isinstance(draft_authority, tuple):
                if (
                    len(draft_authority) != 2
                    or any(type(value) is not int for value in draft_authority)
                    or draft_authority != (
                        persisted_authority.authority_revision,
                        persisted_authority.dependency_revision,
                    )
                ):
                    return False
            elif draft_authority is not None:
                raise ValueError("draft_authority must be exact authority or revisions")
            radiant = _vision_hero_ids(persisted_vision.radiant_hero_ids_json)
            dire = _vision_hero_ids(persisted_vision.dire_hero_ids_json)
            if radiant is None or dire is None:
                return False
            if not self._curve_anchor_authority_matches(persisted_authority):
                return False
            if not draft_landmark_authority_matches(
                self.connection,
                persisted_authority,
                raybet_match_id=order.raybet_match_id,
                map_number=map_number,
                strict_mapping_id=strict_mapping_id,
                radiant_hero_ids=radiant,
                dire_hero_ids=dire,
                observed_at=order.signal_transport_at,
                require_current_revisions=True,
                verify_curve=False,
            ):
                return False
            reserved = self.connection.execute(
                """INSERT INTO shadow_map_attempts
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (order.raybet_match_id, map_number, order.order_key,
                 order.status, order.signaled_at.isoformat()),
            )
            if reserved.rowcount != 1:
                return False
            self.connection.execute(
                """INSERT INTO shadow_order_decision_lineage
                   (order_key, decision_key, recorded_at)
                   VALUES (?, ?, ?)""",
                (order.order_key, decision_key, self._iso(order.signaled_at)),
            )
            self.connection.execute(
                f"""INSERT INTO shadow_orders
                (order_key, raybet_match_id, strict_mapping_id, odds_id,
                 market_key, signaled_at,
                 model_probability, market_probability, signal_price,
                 signal_transport_key, signal_transport_at, expires_at,
                 signal_odds_group_id, signal_outcome_key,
                 signal_identity_verified, stake, status, fill_price, filled_at,
                 rejection_reason, {', '.join(_DRAFT_AUTHORITY_COLUMNS)},
                 {', '.join(_VISION_AUTHORITY_COLUMNS)})
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        {', '.join('?' for _ in _DRAFT_AUTHORITY_COLUMNS)},
                        {', '.join('?' for _ in _VISION_AUTHORITY_COLUMNS)})""",
                (order.order_key, order.raybet_match_id, strict_mapping_id,
                 order.odds_id,
                 market_key(order.market.market_type, order.market.period,
                            order.market.side, order.market.line),
                 order.signaled_at.isoformat(), order.model_probability,
                 order.market_probability, order.signal_price,
                 order.signal_transport_key, self._iso(order.signal_transport_at),
                 self._iso(order.expires_at), order.signal_odds_group_id,
                 order.signal_outcome_key, int(order.signal_identity_verified),
                 order.stake, order.status,
                 order.fill_price,
                 self._iso(order.filled_at) if order.filled_at else None,
                  order.rejection_reason,
                  *_draft_authority_values(persisted_authority),
                  *_vision_authority_values(persisted_vision)),
            )
            return True

    def insert_map_result(self, result: Any, *, strict_mapping_id: int) -> bool:
        if type(strict_mapping_id) is not int or strict_mapping_id <= 0:
            raise ValueError("strict_mapping_id must be a positive integer")
        reconciliation = self.connection.execute(
            """SELECT evidence_ref, raybet_evidence_id, opendota_evidence_id,
                      raybet_evidence_ref, opendota_evidence_ref,
                      raybet_observed_at, opendota_observed_at, first_usable_at
                 FROM settlement_reconciliations
                WHERE raybet_match_id=? AND map_number=?
                  AND strict_mapping_id=? AND dota_match_id=?
                  AND status='confirmed'
                  AND raybet_winner_side=? AND opendota_winner_side=?""",
            (
                result.raybet_match_id,
                result.map_number,
                strict_mapping_id,
                result.dota_match_id,
                result.winner_side,
                result.winner_side,
            ),
        ).fetchone()
        if (
            reconciliation is None
            or str(reconciliation["evidence_ref"] or "") != result.evidence_ref
            or str(reconciliation["first_usable_at"] or "")
            != self._iso(result.settled_at)
        ):
            return False
        cursor = self.execute(
            """INSERT INTO map_results
               (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                winner_side, team_one_kills, team_two_kills, duration_seconds,
                evidence_ref, reconciliation_ref, raybet_evidence_id,
                opendota_evidence_id, raybet_evidence_ref,
                opendota_evidence_ref, raybet_observed_at,
                opendota_observed_at, first_usable_at, settled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                result.raybet_match_id,
                result.map_number,
                strict_mapping_id,
                result.dota_match_id,
                result.winner_side,
                result.team_one_kills,
                result.team_two_kills,
                result.duration_seconds,
                result.evidence_ref,
                reconciliation["evidence_ref"],
                reconciliation["raybet_evidence_id"],
                reconciliation["opendota_evidence_id"],
                reconciliation["raybet_evidence_ref"],
                reconciliation["opendota_evidence_ref"],
                reconciliation["raybet_observed_at"],
                reconciliation["opendota_observed_at"],
                reconciliation["first_usable_at"],
                result.settled_at.isoformat(),
            ),
        )
        return cursor.rowcount == 1

    def _settlement_source_authority_valid(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        strict_mapping_id: int,
        dota_match_id: int,
        raybet_status: str,
        raybet_winner_side: str | None,
        opendota_winner_side: str,
        raybet_evidence_ref: str,
        opendota_evidence_ref: str,
        opendota_facts: Mapping[str, object],
        raybet_observed: str,
        opendota_observed: str,
        opendota_first_usable: str,
        raybet_audit_key: str,
        raybet_transport_key: str | None,
        raybet_response_state_hash: str | None,
        raybet_response_artifact_hash: str,
        opendota_artifact_id: str,
        opendota_observation_id: str,
        opendota_content_hash: str,
    ) -> bool:
        """Re-read both immutable raw payloads before creating result authority."""

        try:
            mapping = self.connection.execute(
                """SELECT team_one_id, team_two_id, canonical_team_one_id,
                          canonical_team_two_id
                     FROM strict_live_map_mappings
                    WHERE mapping_id=? AND raybet_match_id=? AND map_number=?""",
                (strict_mapping_id, raybet_match_id, map_number),
            ).fetchone()
            if mapping is None or any(
                type(mapping[field]) is not int or int(mapping[field]) <= 0
                for field in (
                    "team_one_id", "team_two_id",
                    "canonical_team_one_id", "canonical_team_two_id",
                )
            ):
                return False
            audit = self.connection.execute(
                """SELECT observed_at, claimed_raybet_match_id,
                          observed_raybet_match_id, disposition, artifact_hash
                     FROM direct_response_audit
                    WHERE audit_key=? AND source='direct'
                      AND response_kind='final_odds'""",
                (raybet_audit_key,),
            ).fetchone()
            if (
                audit is None
                or str(audit["observed_at"]) != raybet_observed
                or str(audit["claimed_raybet_match_id"] or "")
                != raybet_match_id
                or str(audit["observed_raybet_match_id"] or "")
                != raybet_match_id
                or str(audit["disposition"])
                not in {"accepted", "audit_only"}
                or str(audit["artifact_hash"])
                != raybet_response_artifact_hash
            ):
                return False
            raw_response = self.direct_response_payload(raybet_audit_key)
            if not isinstance(raw_response, dict):
                return False
            raw_result = raw_response.get("result")
            if not isinstance(raw_result, dict):
                return False
            from .raybet import parse_raybet_map_final

            parsed_final = parse_raybet_map_final(
                raw_result,
                map_number,
                observed_at=datetime.fromisoformat(raybet_observed),
                expected_match_id=raybet_match_id,
                expected_team_ids=(
                    int(mapping["team_one_id"]),
                    int(mapping["team_two_id"]),
                ),
            )
            if (
                parsed_final.status != raybet_status
                or parsed_final.winner_side != raybet_winner_side
                or parsed_final.evidence_ref != raybet_evidence_ref
            ):
                return False
            if raybet_transport_key is not None:
                transport = self.connection.execute(
                    """SELECT response_state_hash, response_artifact_hash,
                              observed_at, raybet_match_id, source,
                              normalized_state_hash_version,
                              original_legacy_normalized_state_hash,
                              processing_status
                         FROM odds_transport_observations
                        WHERE observation_key=?""",
                    (raybet_transport_key,),
                ).fetchone()
                if (
                    transport is None
                    or transport["response_state_hash"]
                    != raybet_response_state_hash
                    or transport["response_artifact_hash"]
                    != raybet_response_artifact_hash
                    or str(transport["observed_at"]) != raybet_observed
                    or str(transport["raybet_match_id"]) != raybet_match_id
                    or str(transport["source"]) != "direct"
                    or int(transport["normalized_state_hash_version"]) != 2
                    or transport["original_legacy_normalized_state_hash"]
                    is not None
                    or str(transport["processing_status"]) != "processed"
                ):
                    return False

            source = self.connection.execute(
                """SELECT observation.source, observation.artifact_use,
                          observation.endpoint,
                          observation.sanitized_request_identity,
                          observation.match_id, observation.content_hash,
                          observation.received_at,
                          observation.first_usable_at,
                          artifact.storage_path, artifact.content_hash
                              AS artifact_content_hash,
                          artifact.source AS artifact_source,
                          artifact.artifact_use AS artifact_use,
                          artifact.match_id AS artifact_match_id,
                          artifact.first_usable_at AS artifact_first_usable_at
                     FROM raw_source_observations AS observation
                     JOIN raw_source_artifacts AS artifact
                       ON artifact.artifact_id=observation.artifact_id
                    WHERE observation.observation_id=?
                      AND observation.artifact_id=?""",
                (opendota_observation_id, opendota_artifact_id),
            ).fetchone()
            if (
                source is None
                or str(source["source"]) != "opendota"
                or str(source["artifact_source"]) != "opendota"
                or str(source["artifact_use"]) != "primary"
                or int(source["match_id"]) != dota_match_id
                or int(source["artifact_match_id"]) != dota_match_id
                or str(source["content_hash"]) != opendota_content_hash
                or str(source["artifact_content_hash"])
                != opendota_content_hash
                or str(source["received_at"]) != opendota_observed
                or str(source["first_usable_at"]) != opendota_first_usable
                or str(source["artifact_first_usable_at"])
                != opendota_first_usable
                or str(source["endpoint"]) != f"/api/matches/{dota_match_id}"
                or str(source["sanitized_request_identity"])
                != f"/api/matches/{dota_match_id}"
            ):
                return False
            source_path = Path(str(source["storage_path"]))
            if not source_path.is_absolute():
                return False
            RawArchive._verify(source_path, opendota_content_hash)
            raw_detail = json.loads(gzip.decompress(source_path.read_bytes()))
            if not isinstance(raw_detail, dict):
                return False
            if (
                type(raw_detail.get("match_id")) is not int
                or int(raw_detail["match_id"]) != dota_match_id
                or opendota_evidence_ref
                != f"opendota:{dota_match_id}:sha256:{opendota_content_hash}"
            ):
                return False
            radiant_team_id = raw_detail.get("radiant_team_id")
            dire_team_id = raw_detail.get("dire_team_id")
            radiant_win = raw_detail.get("radiant_win")
            if (
                type(radiant_team_id) is not int
                or type(dire_team_id) is not int
                or type(radiant_win) not in {bool, int}
                or radiant_win not in {True, False, 0, 1}
                or {radiant_team_id, dire_team_id}
                != {
                    int(mapping["canonical_team_one_id"]),
                    int(mapping["canonical_team_two_id"]),
                }
            ):
                return False
            radiant_side = (
                "team_one"
                if radiant_team_id == int(mapping["canonical_team_one_id"])
                else "team_two"
            )
            raw_winner_side = (
                radiant_side
                if bool(radiant_win)
                else ("team_two" if radiant_side == "team_one" else "team_one")
            )
            score_by_side = {
                radiant_side: raw_detail.get("radiant_score"),
                "team_two" if radiant_side == "team_one" else "team_one": (
                    raw_detail.get("dire_score")
                ),
            }
            if (
                raw_winner_side != opendota_winner_side
                or score_by_side["team_one"]
                != opendota_facts.get("team_one_kills")
                or score_by_side["team_two"]
                != opendota_facts.get("team_two_kills")
                or raw_detail.get("duration")
                != opendota_facts.get("duration_seconds")
            ):
                return False
        except (
            KeyError,
            OSError,
            EOFError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            SQLAlchemyError,
        ):
            return False
        return True

    def record_settlement_reconciliation(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        strict_mapping_id: int,
        dota_match_id: int,
        raybet_status: str,
        raybet_winner_side: str | None,
        opendota_winner_side: str,
        raybet_evidence_ref: str,
        opendota_evidence_ref: str,
        raybet_facts: Mapping[str, object],
        opendota_facts: Mapping[str, object],
        status: str,
        reason: str,
        raybet_observed_at: datetime,
        opendota_observed_at: datetime,
        opendota_first_usable_at: datetime,
        raybet_audit_key: str | None,
        raybet_transport_key: str | None,
        raybet_response_state_hash: str | None,
        raybet_response_artifact_hash: str | None,
        opendota_artifact_id: str | None,
        opendota_observation_id: str | None,
        opendota_content_hash: str | None,
    ) -> DatabaseRow:
        """Persist both source facts and a sticky fail-closed resolution."""
        if type(strict_mapping_id) is not int or strict_mapping_id <= 0:
            raise ValueError("strict_mapping_id must be a positive integer")
        for field, value in (
            ("raybet_observed_at", raybet_observed_at),
            ("opendota_observed_at", opendota_observed_at),
            ("opendota_first_usable_at", opendota_first_usable_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field} must be timezone-aware")
        raybet_observed_at = raybet_observed_at.astimezone(timezone.utc)
        opendota_observed_at = opendota_observed_at.astimezone(timezone.utc)
        opendota_first_usable_at = opendota_first_usable_at.astimezone(
            timezone.utc
        )
        if opendota_first_usable_at < opendota_observed_at:
            raise ValueError(
                "OpenDota first usable time cannot precede its observation"
            )
        first_usable_at = max(raybet_observed_at, opendota_first_usable_at)
        raybet_observed = self._iso(raybet_observed_at)
        opendota_observed = self._iso(opendota_observed_at)
        opendota_first_usable = self._iso(opendota_first_usable_at)
        first_usable = self._iso(first_usable_at)
        reconciliation_ref = (
            f"settlement-reconciliation:{raybet_match_id}:map:{map_number}"
        )
        expected_identity = {
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "strict_mapping_id": strict_mapping_id,
            "dota_match_id": dota_match_id,
        }
        raybet_facts_map = dict(raybet_facts)
        opendota_facts_map = dict(opendota_facts)
        raybet_authority_facts = {
            **expected_identity,
            "winner_side": raybet_winner_side,
            "observed_at": raybet_observed,
            "audit_key": raybet_audit_key,
            "transport_key": raybet_transport_key,
            "response_state_hash": raybet_response_state_hash,
            "response_artifact_hash": raybet_response_artifact_hash,
        }
        opendota_authority_facts = {
            **expected_identity,
            "winner_side": opendota_winner_side,
            "observed_at": opendota_observed,
            "first_usable_at": opendota_first_usable,
            "artifact_id": opendota_artifact_id,
            "observation_id": opendota_observation_id,
            "content_hash": opendota_content_hash,
        }
        raybet_identity_conflict = any(
            key in raybet_facts_map and raybet_facts_map[key] != value
            for key, value in raybet_authority_facts.items()
        ) or (
            "winner_side" in raybet_facts_map
            and raybet_facts_map["winner_side"] != raybet_winner_side
        )
        opendota_identity_conflict = any(
            key in opendota_facts_map and opendota_facts_map[key] != value
            for key, value in opendota_authority_facts.items()
        ) or (
            "winner_side" in opendota_facts_map
            and opendota_facts_map["winner_side"] != opendota_winner_side
        )
        facts_identity_conflict = (
            raybet_identity_conflict or opendota_identity_conflict
        )
        for key, value in raybet_authority_facts.items():
            raybet_facts_map.setdefault(key, value)
        for key, value in opendota_authority_facts.items():
            opendota_facts_map.setdefault(key, value)
        raybet_facts_json = self.json(raybet_facts_map)
        opendota_facts_json = self.json(opendota_facts_map)
        transport_refs = (
            raybet_transport_key,
            raybet_response_state_hash,
        )
        transport_refs_complete = all(transport_refs) or not any(transport_refs)
        source_authority_supplied = bool(
            raybet_audit_key
            and raybet_response_artifact_hash
            and transport_refs_complete
            and opendota_artifact_id
            and opendota_observation_id
            and opendota_content_hash
        )
        source_authority_complete = source_authority_supplied
        if source_authority_complete:
            source_authority_complete = self._settlement_source_authority_valid(
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                strict_mapping_id=strict_mapping_id,
                dota_match_id=dota_match_id,
                raybet_status=raybet_status,
                raybet_winner_side=raybet_winner_side,
                opendota_winner_side=opendota_winner_side,
                raybet_evidence_ref=raybet_evidence_ref,
                opendota_evidence_ref=opendota_evidence_ref,
                opendota_facts=opendota_facts_map,
                raybet_observed=raybet_observed,
                opendota_observed=opendota_observed,
                opendota_first_usable=opendota_first_usable,
                raybet_audit_key=str(raybet_audit_key),
                raybet_transport_key=raybet_transport_key,
                raybet_response_state_hash=raybet_response_state_hash,
                raybet_response_artifact_hash=str(
                    raybet_response_artifact_hash
                ),
                opendota_artifact_id=str(opendota_artifact_id),
                opendota_observation_id=str(opendota_observation_id),
                opendota_content_hash=str(opendota_content_hash),
            )
        if status != "manual_review" and not source_authority_complete:
            status, reason = (
                "manual_review",
                (
                    "source_authority_invalid"
                    if source_authority_supplied
                    else "source_authority_missing"
                ),
            )
        with self.transaction():
            existing = self.connection.execute(
                """SELECT * FROM settlement_reconciliations
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            mapping_authority = self.connection.execute(
                """SELECT 1 FROM strict_live_map_mappings
                    WHERE mapping_id=? AND raybet_match_id=? AND map_number=?""",
                (strict_mapping_id, raybet_match_id, map_number),
            ).fetchone()
            if mapping_authority is None:
                raise ValueError("strict mapping does not match the reconciliation")
            legacy_mapping_authority = False
            if existing is not None:
                existing_mapping_id = existing["strict_mapping_id"]
                legacy_mapping_authority = (
                    type(existing_mapping_id) is not int
                    or int(existing_mapping_id) <= 0
                )
                if (
                    not legacy_mapping_authority
                    and int(existing_mapping_id) != strict_mapping_id
                ):
                    self.connection.execute(
                        """UPDATE settlement_reconciliations
                              SET status='manual_review',
                                  reason='mapping_lineage_conflict',
                                  updated_at=?
                            WHERE raybet_match_id=? AND map_number=?""",
                        (first_usable, raybet_match_id, map_number),
                    )
                    self.connection.execute(
                        """UPDATE settlements SET review_required=1
                            WHERE order_key IN (
                                SELECT order_key FROM shadow_map_attempts
                                 WHERE raybet_match_id=? AND map_number=?
                            )""",
                        (raybet_match_id, map_number),
                    )
                    row = self.connection.execute(
                        """SELECT * FROM settlement_reconciliations
                            WHERE raybet_match_id=? AND map_number=?""",
                        (raybet_match_id, map_number),
                    ).fetchone()
                    assert row is not None
                    return row
            lineage = self.connection.execute(
                """SELECT orders.order_key, orders.signal_transport_at
                     FROM shadow_orders AS orders
                     JOIN shadow_map_attempts AS attempt
                       ON attempt.order_key=orders.order_key
                    WHERE attempt.raybet_match_id=? AND attempt.map_number=?""",
                (raybet_match_id, map_number),
            ).fetchall()
            blocked_reason = None
            for row in lineage:
                candidate = self.order_block_reason(str(row["order_key"]))
                if candidate is not None:
                    blocked_reason = candidate
                    break
            settlement_event_at = (
                existing["first_usable_at"] or existing["first_observed_at"]
                if existing is not None and existing["status"] == "confirmed"
                else first_usable
            )
            if self._draft_conflict_effective_at(
                raybet_match_id, map_number, settlement_event_at
            ):
                blocked_reason = "vision_draft_conflict"
            if status != "manual_review" and facts_identity_conflict:
                status, reason = "manual_review", "source_facts_identity_conflict"
            if status != "manual_review" and blocked_reason is not None:
                status, reason = "manual_review", blocked_reason
            evidence_ids: dict[str, int] = {}
            if source_authority_complete:
                evidence_rows = (
                    (
                        "raybet", raybet_status, raybet_winner_side,
                        raybet_evidence_ref, raybet_facts_json,
                        raybet_observed, raybet_observed,
                        raybet_audit_key, raybet_transport_key,
                        raybet_response_state_hash,
                        raybet_response_artifact_hash,
                        None, None, None,
                    ),
                    (
                        "opendota", "confirmed", opendota_winner_side,
                        opendota_evidence_ref, opendota_facts_json,
                        opendota_observed, opendota_first_usable,
                        None, None, None, None,
                        opendota_artifact_id, opendota_observation_id,
                        opendota_content_hash,
                    ),
                )
                try:
                    with self.transaction():
                        for values in evidence_rows:
                            self.connection.execute(
                                """INSERT INTO settlement_result_evidence
                                   (raybet_match_id, map_number, dota_match_id,
                                    source, status, winner_side, evidence_ref,
                                    facts_json, observed_at, first_usable_at,
                                    raybet_audit_key, raybet_transport_key,
                                    raybet_response_state_hash,
                                    raybet_response_artifact_hash,
                                    opendota_artifact_id,
                                    opendota_observation_id,
                                    opendota_content_hash)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                           ?, ?, ?, ?)
                                   ON CONFLICT DO NOTHING""",
                                (raybet_match_id, map_number, dota_match_id, *values),
                            )
                            source = str(values[0])
                            evidence_ref = str(values[3])
                            persisted = self.connection.execute(
                                """SELECT evidence_id, status, winner_side,
                                          facts_json, observed_at, first_usable_at,
                                          raybet_audit_key, raybet_transport_key,
                                          raybet_response_state_hash,
                                          raybet_response_artifact_hash,
                                          opendota_artifact_id,
                                          opendota_observation_id,
                                          opendota_content_hash
                                     FROM settlement_result_evidence
                                    WHERE raybet_match_id=? AND map_number=?
                                      AND source=? AND evidence_ref=?""",
                                (
                                    raybet_match_id, map_number, source,
                                    evidence_ref,
                                ),
                            ).fetchone()
                            if persisted is None or tuple(persisted)[1:] != (
                                values[1], values[2], values[4], *values[5:]
                            ):
                                raise ValueError(
                                    "settlement evidence reference was reused"
                                )
                            evidence_ids[source] = int(persisted["evidence_id"])
                except (IntegrityError, ValueError):
                    evidence_ids.clear()
                    source_authority_complete = False
                    status, reason = "manual_review", "source_authority_invalid"

            linked_elsewhere = self.connection.execute(
                """SELECT raybet_match_id, map_number
                     FROM settlement_reconciliations
                    WHERE dota_match_id=?
                      AND (raybet_match_id!=? OR map_number!=?)
                    UNION
                   SELECT raybet_match_id, map_number
                     FROM map_results
                    WHERE dota_match_id=?
                      AND (raybet_match_id!=? OR map_number!=?)""",
                (
                    dota_match_id,
                    raybet_match_id,
                    map_number,
                    dota_match_id,
                    raybet_match_id,
                    map_number,
                ),
            ).fetchall()
            link_conflict = bool(linked_elsewhere)
            if link_conflict:
                self.connection.execute(
                    """UPDATE settlement_reconciliations
                          SET status='manual_review',
                              reason=CASE
                                WHEN status='manual_review' THEN reason
                                ELSE 'opendota_match_link_conflict'
                              END,
                              updated_at=?
                        WHERE dota_match_id=?
                          AND (raybet_match_id!=? OR map_number!=?)""",
                    (first_usable, dota_match_id, raybet_match_id, map_number),
                )
                for linked in linked_elsewhere:
                    self.connection.execute(
                        """UPDATE settlements SET review_required=1
                            WHERE order_key IN (
                                SELECT order_key FROM shadow_map_attempts
                                 WHERE raybet_match_id=? AND map_number=?
                            )""",
                        (linked["raybet_match_id"], linked["map_number"]),
                    )

            effective_status = "manual_review" if link_conflict else status
            effective_reason = (
                "opendota_match_link_conflict" if link_conflict else reason
            )
            effective_dota_match_id = dota_match_id
            effective_raybet_winner = raybet_winner_side
            effective_opendota_winner = opendota_winner_side
            effective_raybet_ref = raybet_evidence_ref
            effective_opendota_ref = opendota_evidence_ref
            effective_raybet_evidence_id = evidence_ids.get("raybet")
            effective_opendota_evidence_id = evidence_ids.get("opendota")
            effective_raybet_observed = raybet_observed
            effective_opendota_observed = opendota_observed
            effective_first_usable = first_usable
            effective_first_observed = first_usable
            effective_updated = first_usable
            if legacy_mapping_authority:
                effective_status = "manual_review"
                effective_reason = "legacy_mapping_authority_missing"
                effective_dota_match_id = int(existing["dota_match_id"])
                effective_raybet_winner = existing["raybet_winner_side"]
                effective_opendota_winner = str(existing["opendota_winner_side"])
                effective_raybet_ref = str(existing["raybet_evidence_ref"])
                effective_opendota_ref = str(existing["opendota_evidence_ref"])
                effective_raybet_evidence_id = existing["raybet_evidence_id"]
                effective_opendota_evidence_id = existing["opendota_evidence_id"]
                effective_raybet_observed = existing["raybet_observed_at"]
                effective_opendota_observed = existing["opendota_observed_at"]
                effective_first_usable = existing["first_usable_at"]
                effective_first_observed = existing["first_observed_at"]
            elif existing is not None and existing["status"] == "manual_review":
                effective_status = "manual_review"
                effective_reason = str(existing["reason"])
                effective_dota_match_id = int(existing["dota_match_id"])
                effective_raybet_winner = existing["raybet_winner_side"]
                effective_opendota_winner = str(existing["opendota_winner_side"])
                effective_raybet_ref = str(existing["raybet_evidence_ref"])
                effective_opendota_ref = str(existing["opendota_evidence_ref"])
                effective_raybet_evidence_id = existing["raybet_evidence_id"]
                effective_opendota_evidence_id = existing["opendota_evidence_id"]
                effective_raybet_observed = existing["raybet_observed_at"]
                effective_opendota_observed = existing["opendota_observed_at"]
                effective_first_usable = existing["first_usable_at"]
                effective_first_observed = existing["first_observed_at"]
            elif existing is not None and existing["status"] == "confirmed":
                candidate_identity = (
                    dota_match_id, raybet_winner_side, opendota_winner_side,
                    raybet_evidence_ref, opendota_evidence_ref,
                    effective_raybet_evidence_id,
                    effective_opendota_evidence_id,
                    raybet_observed, opendota_observed, first_usable,
                )
                existing_identity = (
                    int(existing["dota_match_id"]),
                    existing["raybet_winner_side"],
                    str(existing["opendota_winner_side"]),
                    str(existing["raybet_evidence_ref"]),
                    str(existing["opendota_evidence_ref"]),
                    existing["raybet_evidence_id"],
                    existing["opendota_evidence_id"],
                    existing["raybet_observed_at"],
                    existing["opendota_observed_at"],
                    existing["first_usable_at"],
                )
                if effective_status != "confirmed" or candidate_identity != existing_identity:
                    effective_status = "manual_review"
                    effective_reason = (
                        "opendota_match_link_conflict"
                        if link_conflict
                        else (
                            reason
                            if reason in {
                                "stored_map_result_conflict",
                                "map_result_persistence_conflict",
                            }
                            else "source_result_changed"
                        )
                    )
                else:
                    effective_reason = str(existing["reason"])
                effective_dota_match_id = int(existing["dota_match_id"])
                effective_raybet_winner = existing["raybet_winner_side"]
                effective_opendota_winner = str(existing["opendota_winner_side"])
                effective_raybet_ref = str(existing["raybet_evidence_ref"])
                effective_opendota_ref = str(existing["opendota_evidence_ref"])
                effective_raybet_evidence_id = existing["raybet_evidence_id"]
                effective_opendota_evidence_id = existing["opendota_evidence_id"]
                effective_raybet_observed = existing["raybet_observed_at"]
                effective_opendota_observed = existing["opendota_observed_at"]
                effective_first_usable = existing["first_usable_at"]
                effective_first_observed = existing["first_observed_at"]
                effective_updated = (
                    str(existing["updated_at"])
                    if effective_status == "confirmed"
                    else first_usable
                )

            self.connection.execute(
                """INSERT INTO settlement_reconciliations
                   (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                    raybet_winner_side, opendota_winner_side,
                    raybet_evidence_ref, opendota_evidence_ref, evidence_ref,
                    raybet_evidence_id, opendota_evidence_id,
                    raybet_observed_at, opendota_observed_at, first_usable_at,
                    status, reason, first_observed_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(raybet_match_id, map_number) DO UPDATE SET
                     dota_match_id=excluded.dota_match_id,
                     raybet_winner_side=excluded.raybet_winner_side,
                     opendota_winner_side=excluded.opendota_winner_side,
                     raybet_evidence_ref=excluded.raybet_evidence_ref,
                     opendota_evidence_ref=excluded.opendota_evidence_ref,
                     evidence_ref=excluded.evidence_ref,
                     raybet_evidence_id=excluded.raybet_evidence_id,
                     opendota_evidence_id=excluded.opendota_evidence_id,
                     raybet_observed_at=excluded.raybet_observed_at,
                     opendota_observed_at=excluded.opendota_observed_at,
                     first_usable_at=excluded.first_usable_at,
                     status=excluded.status,
                     reason=excluded.reason,
                     updated_at=excluded.updated_at""",
                (
                    raybet_match_id,
                    map_number,
                    strict_mapping_id,
                    effective_dota_match_id,
                    effective_raybet_winner,
                    effective_opendota_winner,
                    effective_raybet_ref,
                    effective_opendota_ref,
                    reconciliation_ref,
                    effective_raybet_evidence_id,
                    effective_opendota_evidence_id,
                    effective_raybet_observed,
                    effective_opendota_observed,
                    effective_first_usable,
                    effective_status,
                    effective_reason,
                    effective_first_observed,
                    effective_updated,
                ),
            )
            if effective_status == "manual_review":
                self.connection.execute(
                    """UPDATE settlements SET review_required=1
                        WHERE order_key IN (
                            SELECT order_key FROM shadow_map_attempts
                             WHERE raybet_match_id=? AND map_number=?
                        )""",
                    (raybet_match_id, map_number),
                )
            row = self.connection.execute(
                """SELECT * FROM settlement_reconciliations
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            assert row is not None
            return row

    def enqueue_notification(
        self,
        *,
        order_key: str,
        event_type: str,
        payload: Mapping[str, Any],
        stats_cutoff_at: datetime,
        created_at: datetime,
    ) -> bool:
        from .notifications import enqueue

        return enqueue(
            self.connection,
            order_key=order_key,
            event_type=event_type,
            payload=payload,
            stats_cutoff_at=stats_cutoff_at,
            created_at=created_at,
        )

    def quarantine_notification(
        self,
        *,
        outbox_id: int,
        reason: str,
        actor: str,
        now: datetime,
    ) -> bool:
        from .notifications import quarantine_outbox

        return quarantine_outbox(
            self.connection,
            outbox_id=outbox_id,
            reason=reason,
            actor=actor,
            now=now,
        )

    def insert_settlement(
        self, order_key: str, result: str, return_units: float,
        settled_at: datetime, evidence_ref: str, review_required: bool = False,
    ) -> bool:
        with self.transaction():
            existing = self.connection.execute(
                "SELECT 1 FROM settlements WHERE order_key=?", (order_key,)
            ).fetchone()
            if existing is not None:
                return False
            if not review_required:
                from .settlement import (
                    SettlementAuthorityError,
                    persist_authoritative_settlement_snapshot,
                    record_settlement_authority_review,
                    resolve_authoritative_settlement,
                )

                try:
                    authority = resolve_authoritative_settlement(
                        self.connection, order_key
                    )
                except SettlementAuthorityError as error:
                    record_settlement_authority_review(
                        self.connection,
                        order_key,
                        error.reason,
                        actor="settlement_writer",
                    )
                    return False
                except SQLAlchemyError:
                    record_settlement_authority_review(
                        self.connection,
                        order_key,
                        "settlement_authority_unavailable",
                        actor="settlement_writer",
                    )
                    return False
                caller_settled_at = settled_at
                caller_evidence_ref = evidence_ref
                settled_at = authority.settled_at
                evidence_ref = authority.map_result_evidence_ref
                try:
                    if (
                        not isinstance(caller_settled_at, datetime)
                        or caller_settled_at.tzinfo is None
                        or caller_settled_at.utcoffset() is None
                    ):
                        caller_time = None
                    else:
                        caller_time = self._iso(caller_settled_at)
                except (AttributeError, TypeError, ValueError):
                    caller_time = None
                caller_mismatch = (
                    str(result) != authority.result
                    or isinstance(return_units, bool)
                    or not isinstance(return_units, (int, float))
                    or not math.isfinite(float(return_units))
                    or not math.isclose(
                        float(return_units),
                        authority.return_units,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                    or caller_time != authority.settled_at.isoformat()
                    or str(caller_evidence_ref) != authority.map_result_evidence_ref
                )
                if caller_mismatch:
                    record_settlement_authority_review(
                        self.connection,
                        order_key,
                        "settlement_caller_authority_mismatch",
                        actor="settlement_writer",
                    )
                    return False
                blocked_reason = self.order_block_reason(order_key)
                if self._order_draft_conflict_effective_at(
                    order_key, authority.settled_at
                ):
                    blocked_reason = blocked_reason or "vision_draft_conflict"
                if blocked_reason is not None:
                    record_settlement_authority_review(
                        self.connection,
                        order_key,
                        blocked_reason,
                        actor="settlement_writer",
                    )
                    result = "review"
                    return_units = 0.0
                    review_required = True
                else:
                    try:
                        persist_authoritative_settlement_snapshot(
                            self.connection, authority
                        )
                    except (SettlementAuthorityError, IntegrityError) as error:
                        reason = (
                            error.reason
                            if isinstance(error, SettlementAuthorityError)
                            else "settlement_authority_trigger_rejected"
                        )
                        record_settlement_authority_review(
                            self.connection,
                            order_key,
                            reason,
                            actor="settlement_writer",
                        )
                        return False
                    # Caller-provided formal fields are never authoritative.
                    result = authority.result
                    return_units = authority.return_units
            cursor = self.connection.execute(
                """INSERT INTO settlements VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (order_key, result, return_units, settled_at.isoformat(), evidence_ref,
                 int(review_required)),
            )
            if cursor.rowcount != 1:
                return False
            if not review_required:
                from .notifications import EVENT_SETTLED, settled_order_payload

                self.enqueue_notification(
                    order_key=order_key,
                    event_type=EVENT_SETTLED,
                    payload=settled_order_payload(
                        self.connection,
                        order_key,
                        result=result,
                        return_units=return_units,
                        settled_at=settled_at,
                        evidence_ref=evidence_ref,
                    ),
                    stats_cutoff_at=settled_at,
                    created_at=settled_at,
                )
            return True

    def insert_settlement_review(
        self,
        order_key: str,
        *,
        settled_at: datetime,
        evidence_ref: str,
        reason: str,
        actor: str = "settlement_writer",
    ) -> bool:
        """Persist an audited manual-review marker without scheduling result mail."""

        from .settlement import record_settlement_authority_review

        with self.transaction():
            order = self.connection.execute(
                "SELECT 1 FROM shadow_orders WHERE order_key=?", (order_key,)
            ).fetchone()
            if order is None:
                record_settlement_authority_review(
                    self.connection, order_key, reason, actor=actor
                )
                return False
            record_settlement_authority_review(
                self.connection, order_key, reason, actor=actor
            )
            existing = self.connection.execute(
                "SELECT review_required FROM settlements WHERE order_key=?",
                (order_key,),
            ).fetchone()
            if existing is not None:
                if int(existing["review_required"]) == 0:
                    self.connection.execute(
                        """UPDATE settlements SET review_required=1
                            WHERE order_key=?""",
                        (order_key,),
                    )
                    return True
                return False
            try:
                if settled_at.tzinfo is None or settled_at.utcoffset() is None:
                    return False
                settled_iso = self._iso(settled_at)
            except (AttributeError, TypeError, ValueError):
                return False
            evidence = str(evidence_ref or "").strip()
            if not evidence:
                evidence = f"manual-review:{order_key}:{reason}"
            cursor = self.connection.execute(
                """INSERT INTO settlements
                   (order_key, result, return_units, settled_at, evidence_ref,
                    review_required)
                   VALUES (?, 'review', 0.0, ?, ?, 1)""",
                (order_key, settled_iso, evidence),
            )
            return cursor.rowcount == 1
