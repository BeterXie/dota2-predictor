"""Append-only, non-actionable live prediction evidence and evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .engine import price_groups
from .evaluation import brier_score, log_loss
from .draft_authority import (
    DraftLandmarkAuthority,
    authority_from_curve,
    draft_landmark_authority_matches,
)
from .market_state import MarketSurface
from .models import OddsSnapshot
from .profiles.draft_curve import DraftCurve, DraftPoint, MAX_LANDMARK_AGE_MINUTES
from .raybet_state import raybet_odds_is_open
from .strict_read_gate import strict_read_gate, table_has_columns
from .vision import VisionObservation

if TYPE_CHECKING:
    from .storage import LiveBettingStore
    from .strict_eligibility import StrictLiveMapMapping


RESEARCH_SCHEMA_VERSION = "research-live-prediction-v1"
ACTIONABILITY = "research_only"
MANUAL_CLOCK_TRUST = "diagnostic_untrusted"
MAX_MANUAL_TRANSPORT_LAG = timedelta(seconds=15)
MAX_MANUAL_EVENT_GAP = timedelta(seconds=15)
MAX_MANUAL_RATE_DRIFT_SECONDS = 10.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CLOCK_RE = re.compile(r"^(\d{1,3}):(\d{2})$")
def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_hash(value: object, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest or None")
    return value


def _clock_seconds(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return (
            int(value)
            if math.isfinite(value) and value >= 0 and value.is_integer()
            else None
        )
    if not isinstance(value, str):
        return None
    clean = value.strip()
    if clean.isdigit():
        return int(clean)
    match = _CLOCK_RE.fullmatch(clean)
    if match is None:
        return None
    minutes, seconds = (int(part) for part in match.groups())
    return minutes * 60 + seconds if seconds < 60 else None


@dataclass(frozen=True)
class ManualClockEvidence:
    event_id: str | None
    seconds: int | None
    current_index: int | None
    trust: str
    validation: str


@dataclass(frozen=True)
class ResearchPrediction:
    prediction_key: str
    schema_version: str
    raybet_match_id: str
    map_number: int
    observed_at: datetime
    game_clock_seconds: int
    game_minute: float
    selected_side: str
    market_probability: float
    market_price: float
    raw_model_probability: float | None
    feature_hash: str | None
    model_hash: str | None
    calibration_hash: str | None
    transport_key: str
    transport_hash: str
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    radiant_team_side: str | None
    strict_mapping_id: int
    clock_source: str
    clock_trust: str
    manual_clock_event_id: str | None
    manual_clock_seconds: int | None
    manual_clock_trust: str
    manual_clock_validation: str
    actionability: str
    gate_status: str
    gate_failures: tuple[str, ...]
    input_context_hash: str
    draft_authority: DraftLandmarkAuthority | None
    created_at: datetime


@dataclass(frozen=True)
class ResearchPriceLabel:
    label_key: str
    prediction_key: str
    transport_key: str
    transport_hash: str
    observed_at: datetime
    selected_side: str
    price: float
    market_probability: float
    seconds_after_prediction: float
    created_at: datetime


@dataclass(frozen=True)
class ResearchWriteResult:
    prediction_key: str
    inserted: bool
    price_labels_inserted: int
    gate_status: str
    gate_failures: tuple[str, ...]


def manual_clock_evidence(
    connection: sqlite3.Connection,
    *,
    raybet_match_id: str,
    map_number: int,
    transport_key: str,
    transport_hash: str,
    transport_at: datetime,
    payload_reader: Callable[[str], Mapping[str, object]] | None = None,
) -> ManualClockEvidence:
    """Return a diagnostic clock only after strict continuity checks.

    The trust value never becomes actionable. Externalized payloads must be
    loaded through the store's hash- and size-verifying reader.
    """

    observed_at = _utc(transport_at, "transport_at")
    transport = connection.execute(
        """SELECT normalized_state_hash, observed_at
             FROM odds_transport_observations
            WHERE observation_key=? AND raybet_match_id=?
              AND timing_status='on_time' AND processing_status='processed'""",
        (transport_key, raybet_match_id),
    ).fetchone()
    if (
        transport is None
        or str(transport[0]) != transport_hash
        or datetime.fromisoformat(str(transport[1])) != observed_at
    ):
        return ManualClockEvidence(
            None, None, None, "not_observed", "transport_mismatch"
        )

    rows = connection.execute(
        """SELECT event_id, captured_at, capture_reason,
                  processing_status, processing_reason
             FROM browser_events
            WHERE raybet_match_id=? AND event_type='manual_control'
              AND captured_at<=?
            ORDER BY captured_at DESC, event_id DESC LIMIT 2""",
        (raybet_match_id, observed_at.isoformat()),
    ).fetchall()
    if not rows:
        return ManualClockEvidence(None, None, None, "not_observed", "not_observed")

    current = rows[0]
    event_id = str(current[0])
    if payload_reader is None:
        return ManualClockEvidence(
            event_id,
            None,
            None,
            MANUAL_CLOCK_TRUST,
            "payload_reader_unavailable",
        )
    try:
        current_payload = payload_reader(event_id)
    except (RuntimeError, TypeError, ValueError):
        return ManualClockEvidence(
            event_id, None, None, MANUAL_CLOCK_TRUST, "invalid_payload"
        )
    current_index = (
        current_payload.get("currentIndex")
        if isinstance(current_payload, dict)
        else None
    )
    current_seconds = (
        _clock_seconds(current_payload.get("time"))
        if isinstance(current_payload, dict)
        else None
    )
    if (
        current[2] != "diagnostic_untrusted"
        or current[3] != "audit_only"
        or current[4] != "diagnostic_untrusted"
    ):
        return ManualClockEvidence(
            event_id,
            None,
            current_index if isinstance(current_index, int) else None,
            MANUAL_CLOCK_TRUST,
            "diagnostic_contract_mismatch",
        )
    if len(rows) < 2:
        return ManualClockEvidence(
            event_id,
            None,
            current_index if isinstance(current_index, int) else None,
            MANUAL_CLOCK_TRUST,
            "insufficient_history",
        )

    previous = rows[1]
    try:
        previous_payload = payload_reader(str(previous[0]))
    except (RuntimeError, TypeError, ValueError):
        previous_payload = None
    previous_index = (
        previous_payload.get("currentIndex")
        if isinstance(previous_payload, dict)
        else None
    )
    previous_seconds = (
        _clock_seconds(previous_payload.get("time"))
        if isinstance(previous_payload, dict)
        else None
    )
    if (
        isinstance(current_index, bool)
        or not isinstance(current_index, int)
        or isinstance(previous_index, bool)
        or not isinstance(previous_index, int)
        or current_index != map_number
        or previous_index != map_number
    ):
        return ManualClockEvidence(
            event_id,
            None,
            current_index if isinstance(current_index, int) else None,
            MANUAL_CLOCK_TRUST,
            "map_index_mismatch",
        )
    if current_seconds is None or previous_seconds is None:
        return ManualClockEvidence(
            event_id,
            None,
            current_index,
            MANUAL_CLOCK_TRUST,
            "invalid_time",
        )

    current_at = datetime.fromisoformat(str(current[1]))
    previous_at = datetime.fromisoformat(str(previous[1]))
    event_gap = current_at - previous_at
    transport_lag = observed_at - current_at
    if (
        event_gap <= timedelta(0)
        or event_gap > MAX_MANUAL_EVENT_GAP
        or transport_lag < timedelta(0)
        or transport_lag > MAX_MANUAL_TRANSPORT_LAG
    ):
        return ManualClockEvidence(
            event_id,
            None,
            current_index,
            MANUAL_CLOCK_TRUST,
            "transport_or_sequence_gap",
        )
    if current_seconds < previous_seconds:
        return ManualClockEvidence(
            event_id,
            None,
            current_index,
            MANUAL_CLOCK_TRUST,
            "non_monotonic",
        )
    if (
        current_seconds - previous_seconds
        > event_gap.total_seconds() + MAX_MANUAL_RATE_DRIFT_SECONDS
    ):
        return ManualClockEvidence(
            event_id,
            None,
            current_index,
            MANUAL_CLOCK_TRUST,
            "clock_rate_inconsistent",
        )
    return ManualClockEvidence(
        event_id,
        current_seconds,
        current_index,
        MANUAL_CLOCK_TRUST,
        "validated_diagnostic",
    )


def _research_point(curve: DraftCurve, game_clock_seconds: int) -> DraftPoint | None:
    current_minute = game_clock_seconds / 60.0
    candidates = tuple(
        point
        for point in curve.points
        if point.minute <= current_minute
        and current_minute - point.minute <= MAX_LANDMARK_AGE_MINUTES
    )
    return max(candidates, key=lambda point: point.minute) if candidates else None


def _gate_failures(
    point: DraftPoint | None,
    curve: DraftCurve,
    authority: DraftLandmarkAuthority | None,
) -> tuple[str, ...]:
    if point is None:
        return (curve.unavailable_reason or "live_draft_prediction_missing",)
    failures = []
    if not point.validated:
        failures.append("draft_point_not_validated")
    if not getattr(point, "global_calibration_passed", False):
        failures.append("global_calibration_gate_not_passed")
    if point.support < 100:
        failures.append("draft_support_below_100")
    if not point.calibration_ref.strip():
        failures.append("calibration_ref_missing")
    if not getattr(point, "global_gate_ref", "").strip():
        failures.append("global_gate_ref_missing")
    if not point.input_refs:
        failures.append("draft_input_refs_missing")
    if point.uncertainty is None:
        failures.append("draft_uncertainty_missing")
    for name in ("feature_hash", "model_hash", "calibration_hash"):
        if not getattr(point, name, None):
            failures.append(f"{name}_missing")
    if not getattr(point, "model_version", "").strip():
        failures.append("model_version_missing")
    if getattr(point, "model_kind", "") != "pure_draft":
        failures.append("model_kind_not_pure_draft")
    if getattr(point, "availability_mode", "") != "prospective":
        failures.append("prospective_artifact_required")
    if not getattr(point, "input_snapshot_hash", None):
        failures.append("input_snapshot_hash_missing")
    if authority is None:
        failures.append("draft_landmark_authority_missing")
    return tuple(failures)


def _winner_quotes(
    snapshots: Sequence[OddsSnapshot],
) -> dict[str, tuple[float, float]]:
    probabilities = price_groups(snapshots)
    output: dict[str, tuple[float, float]] = {}
    for row in snapshots:
        side = row.market.side
        if (
            row.market.market_type != "winner"
            or side not in {"team_one", "team_two"}
            or not row.market.supported
            or row.price <= 1.0
            or not raybet_odds_is_open(row.status)
            or row.odds_id not in probabilities
        ):
            continue
        if side in output:
            raise ValueError("multiple complete winner quotes for one side")
        output[str(side)] = (float(row.price), float(probabilities[row.odds_id]))
    if set(output) != {"team_one", "team_two"}:
        raise ValueError("research prediction requires a complete winner market")
    return output


def _append_successor_price_labels(
    store: LiveBettingStore,
    *,
    raybet_match_id: str,
    map_number: int,
    transport_key: str,
    transport_hash: str,
    transport_at: datetime,
    snapshots: Sequence[OddsSnapshot],
    created_at: datetime,
) -> int:
    quotes = _winner_quotes(snapshots)
    rows = store.connection.execute(
        """SELECT prediction.prediction_key, prediction.selected_side,
                  prediction.observed_at
             FROM research_live_predictions AS prediction
             LEFT JOIN research_price_labels AS label
               ON label.prediction_key=prediction.prediction_key
            WHERE prediction.raybet_match_id=? AND prediction.map_number=?
              AND prediction.observed_at<? AND label.prediction_key IS NULL
            ORDER BY prediction.observed_at, prediction.prediction_key""",
        (raybet_match_id, map_number, transport_at.isoformat()),
    ).fetchall()
    inserted = 0
    for row in rows:
        selected_side = str(row[1])
        price, probability = quotes[selected_side]
        prediction_at = datetime.fromisoformat(str(row[2]))
        elapsed = (transport_at - prediction_at).total_seconds()
        if elapsed <= 0:
            continue
        prediction_key = str(row[0])
        label = ResearchPriceLabel(
            label_key=_canonical_hash(
                {
                    "schema": RESEARCH_SCHEMA_VERSION,
                    "prediction_key": prediction_key,
                    "transport_key": transport_key,
                    "label": "successor_price",
                }
            ),
            prediction_key=prediction_key,
            transport_key=transport_key,
            transport_hash=transport_hash,
            observed_at=transport_at,
            selected_side=selected_side,
            price=price,
            market_probability=probability,
            seconds_after_prediction=elapsed,
            created_at=created_at,
        )
        inserted += int(store.insert_research_price_label(label))
    return inserted


def append_research_successor_price_labels(
    store: LiveBettingStore,
    *,
    raybet_match_id: str,
    map_number: int,
    transport_key: str,
    transport_hash: str,
    transport_at: datetime,
    snapshots: Sequence[OddsSnapshot],
    created_at: datetime,
) -> int:
    """Label prior predictions from the first later complete winner quote."""
    observed_at = _utc(transport_at, "transport_at")
    created = _utc(created_at, "created_at")
    if not _SHA256_RE.fullmatch(transport_hash):
        raise ValueError("transport_hash must be a lowercase SHA-256 digest")
    with store.transaction():
        return _append_successor_price_labels(
            store,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            transport_key=transport_key,
            transport_hash=transport_hash,
            transport_at=observed_at,
            snapshots=snapshots,
            created_at=created,
        )


def record_research_prediction(
    store: LiveBettingStore,
    *,
    snapshots: Sequence[OddsSnapshot],
    surface: MarketSurface,
    observation: VisionObservation,
    draft_curve: DraftCurve,
    strict_mapping: StrictLiveMapMapping,
    transport_key: str,
    transport_hash: str,
    transport_at: datetime,
    created_at: datetime,
) -> ResearchWriteResult | None:
    """Append research evidence only when strict, complete context is available."""

    if (
        not surface.complete
        or observation.map_number is None
        or observation.game_clock_seconds is None
        or not observation.is_confirmed
        or observation.screen_state != "game"
        or len(observation.radiant_hero_ids) != 5
        or len(observation.dire_hero_ids) != 5
        or len(set(observation.radiant_hero_ids + observation.dire_hero_ids)) != 10
    ):
        return None
    observed_at = _utc(transport_at, "transport_at")
    created = _utc(created_at, "created_at")
    if observation.captured_at > observed_at:
        return None
    if not _SHA256_RE.fullmatch(transport_hash):
        raise ValueError("transport_hash must be a lowercase SHA-256 digest")

    quotes = _winner_quotes(snapshots)
    selected_side = surface.underdog_side
    market_price, market_probability = quotes[selected_side]
    point = _research_point(draft_curve, observation.game_clock_seconds)
    authority = authority_from_curve(
        draft_curve,
        point,
        radiant_team_side=observation.radiant_team_side,
    )
    failures = list(_gate_failures(point, draft_curve, authority))
    raw_probability = None
    if point is not None and observation.radiant_team_side in {"team_one", "team_two"}:
        raw_probability = (
            point.radiant_probability
            if selected_side == observation.radiant_team_side
            else 1.0 - point.radiant_probability
        )
    elif point is not None:
        failures.append("radiant_team_side_missing")

    feature_hash = _optional_hash(getattr(point, "feature_hash", None), "feature_hash")
    model_hash = _optional_hash(getattr(point, "model_hash", None), "model_hash")
    calibration_hash = _optional_hash(
        getattr(point, "calibration_hash", None), "calibration_hash"
    )
    manual = manual_clock_evidence(
        store.connection,
        raybet_match_id=observation.raybet_match_id,
        map_number=observation.map_number,
        transport_key=transport_key,
        transport_hash=transport_hash,
        transport_at=observed_at,
        payload_reader=store.browser_event_payload,
    )
    with store.transaction():
        authority_verified = authority is not None and (
            draft_landmark_authority_matches(
                store.connection,
                authority,
                raybet_match_id=observation.raybet_match_id,
                map_number=observation.map_number,
                strict_mapping_id=strict_mapping.mapping_id,
                radiant_hero_ids=observation.radiant_hero_ids,
                dire_hero_ids=observation.dire_hero_ids,
                observed_at=observed_at,
                require_current_revisions=True,
                verify_curve=False,
            )
        )
        if authority is not None and not authority_verified:
            failures.append("draft_landmark_authority_unverifiable")
        gate_failures = tuple(dict.fromkeys(failures))
        gate_status = (
            "unavailable"
            if point is None
            else "failed"
            if gate_failures
            else "passed"
        )
        bound_authority = authority if authority_verified else None
        context: dict[str, Any] = {
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "raybet_match_id": observation.raybet_match_id,
            "map_number": observation.map_number,
            "observed_at": observed_at.isoformat(),
            "game_clock_seconds": observation.game_clock_seconds,
            "selected_side": selected_side,
            "market_probability": market_probability,
            "market_price": market_price,
            "raw_model_probability": raw_probability,
            "feature_hash": feature_hash,
            "model_hash": model_hash,
            "calibration_hash": calibration_hash,
            "model_version": None if point is None else point.model_version,
            "model_kind": None if point is None else point.model_kind,
            "availability_mode": None if point is None else point.availability_mode,
            "input_snapshot_hash": (
                None if point is None else point.input_snapshot_hash
            ),
            "transport_key": transport_key,
            "transport_hash": transport_hash,
            "radiant_hero_ids": list(observation.radiant_hero_ids),
            "dire_hero_ids": list(observation.dire_hero_ids),
            "radiant_team_side": observation.radiant_team_side,
            "strict_mapping_refs": strict_mapping.input_refs(),
            "vision_ref": observation.source_frame_ref,
            "manual_clock": {
                "event_id": manual.event_id,
                "seconds": manual.seconds,
                "current_index": manual.current_index,
                "trust": manual.trust,
                "validation": manual.validation,
            },
            "actionability": ACTIONABILITY,
            "gate_status": gate_status,
            "gate_failures": list(gate_failures),
            "draft_landmark_authority": (
                None if bound_authority is None else asdict(bound_authority)
            ),
        }
        input_context_hash = _canonical_hash(context)
        prediction_key = _canonical_hash(
            {
                "schema_version": RESEARCH_SCHEMA_VERSION,
                "transport_key": transport_key,
                "input_context_hash": input_context_hash,
            }
        )
        prediction = ResearchPrediction(
            prediction_key=prediction_key,
            schema_version=RESEARCH_SCHEMA_VERSION,
            raybet_match_id=observation.raybet_match_id,
            map_number=observation.map_number,
            observed_at=observed_at,
            game_clock_seconds=observation.game_clock_seconds,
            game_minute=observation.game_clock_seconds / 60.0,
            selected_side=selected_side,
            market_probability=market_probability,
            market_price=market_price,
            raw_model_probability=raw_probability,
            feature_hash=feature_hash,
            model_hash=model_hash,
            calibration_hash=calibration_hash,
            transport_key=transport_key,
            transport_hash=transport_hash,
            radiant_hero_ids=observation.radiant_hero_ids,
            dire_hero_ids=observation.dire_hero_ids,
            radiant_team_side=observation.radiant_team_side,
            strict_mapping_id=strict_mapping.mapping_id,
            clock_source="vision",
            clock_trust="trusted_vision",
            manual_clock_event_id=manual.event_id,
            manual_clock_seconds=manual.seconds,
            manual_clock_trust=manual.trust,
            manual_clock_validation=manual.validation,
            actionability=ACTIONABILITY,
            gate_status=gate_status,
            gate_failures=gate_failures,
            input_context_hash=input_context_hash,
            draft_authority=bound_authority,
            created_at=created,
        )
        price_labels = _append_successor_price_labels(
            store,
            raybet_match_id=observation.raybet_match_id,
            map_number=observation.map_number,
            transport_key=transport_key,
            transport_hash=transport_hash,
            transport_at=observed_at,
            snapshots=snapshots,
            created_at=created,
        )
        inserted = store.insert_research_prediction(prediction)
    return ResearchWriteResult(
        prediction_key, inserted, price_labels, gate_status, gate_failures
    )


def _accuracy(rows: Sequence[tuple[float, int]]) -> float | None:
    if not rows:
        return None
    return sum(
        int((probability >= 0.5) == bool(outcome)) for probability, outcome in rows
    ) / len(rows)


def research_summary(connection: sqlite3.Connection) -> dict[str, object]:
    """Build fail-closed coverage and accuracy metrics from research rows."""

    strict_gate = strict_read_gate(
        connection,
        mapping_id_sql="prediction.strict_mapping_id",
        raybet_match_id_sql="prediction.raybet_match_id",
        map_number_sql="prediction.map_number",
        signal_at_sql="prediction.observed_at",
        dependent_type="research_prediction",
        dependent_key_sql="prediction.prediction_key",
    )
    vision_invalidation_reason = _required_schema_reason(
        connection,
        "vision_derived_invalidations",
        {"dependent_type", "dependent_key"},
    )
    draft_anchor_reason = _required_schema_reason(
        connection,
        "vision_draft_anchors",
        {"raybet_match_id", "map_number", "status"},
    )
    draft_conflict_reason = _required_schema_reason(
        connection,
        "vision_draft_conflicts",
        {"raybet_match_id", "map_number"},
    )
    vision_available = all(
        reason is None
        for reason in (
            vision_invalidation_reason,
            draft_anchor_reason,
            draft_conflict_reason,
        )
    )
    vision_invalidated_sql = (
        "(EXISTS ("
        "SELECT 1 FROM vision_derived_invalidations AS invalidation "
        "WHERE invalidation.dependent_type='research_prediction' "
        "AND invalidation.dependent_key=prediction.prediction_key) "
        "OR EXISTS ("
        "SELECT 1 FROM vision_draft_anchors AS anchor "
        "WHERE anchor.raybet_match_id=prediction.raybet_match_id "
        "AND anchor.map_number=prediction.map_number "
        "AND anchor.status='conflict' "
        "AND (anchor.conflict_at IS NULL "
        "OR julianday(anchor.conflict_at) IS NULL "
        "OR julianday(prediction.observed_at) IS NULL "
        "OR julianday(anchor.conflict_at)<=julianday(prediction.observed_at) "
        "OR EXISTS ("
        "SELECT 1 FROM vision_draft_conflicts AS conflict "
        "WHERE conflict.raybet_match_id=anchor.raybet_match_id "
        "AND conflict.map_number=anchor.map_number "
        "AND (julianday(conflict.captured_at) IS NULL "
        "OR julianday(conflict.captured_at)<=julianday(prediction.observed_at))"
        "))))"
        if vision_available
        else "0"
    )
    vision_included_sql = (
        f"NOT ({vision_invalidated_sql})" if vision_available else "0"
    )
    authority_columns = {
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
    }
    authority_reason = _required_schema_reason(
        connection, "research_live_predictions", authority_columns
    )
    authority_available = authority_reason is None
    authority_sql = (
        "(prediction.gate_status<>'passed' OR ("
        "prediction.draft_curve_key IS NOT NULL "
        "AND prediction.draft_source_ref='prospective-draft:'||prediction.draft_curve_key "
        "AND prediction.draft_landmark_key IS NOT NULL "
        "AND prediction.draft_landmark_target='radiant_win' "
        "AND prediction.draft_deployment_key IS NOT NULL "
        "AND prediction.draft_target_snapshot_hash IS NOT NULL "
        "AND prediction.draft_feature_hash=prediction.feature_hash "
        "AND prediction.draft_model_hash=prediction.model_hash "
        "AND prediction.draft_calibration_hash=prediction.calibration_hash "
        "AND prediction.draft_model_version IS NOT NULL "
        "AND prediction.draft_global_gate_ref IS NOT NULL "
        "AND prediction.draft_input_snapshot_hash IS NOT NULL "
        "AND prediction.draft_authority_revision>=1 "
        "AND prediction.draft_dependency_revision>=1 "
        "AND EXISTS ("
        "SELECT 1 FROM prospective_draft_curves AS curve "
        "JOIN prospective_draft_landmarks AS landmark "
        "ON landmark.curve_key=curve.curve_key "
        "JOIN draft_model_artifacts AS model "
        "ON model.model_hash=landmark.model_hash "
        "JOIN draft_calibration_artifacts AS calibration "
        "ON calibration.calibration_hash=landmark.calibration_hash "
        "JOIN draft_deployment_bundles AS deployment "
        "ON deployment.deployment_key=curve.deployment_key "
        "WHERE curve.curve_key=prediction.draft_curve_key "
        "AND landmark.landmark_key=prediction.draft_landmark_key "
        "AND curve.raybet_match_id=prediction.raybet_match_id "
        "AND curve.map_number=prediction.map_number "
        "AND curve.strict_mapping_id=prediction.strict_mapping_id "
        "AND json(curve.radiant_hero_ids_json)=json(prediction.radiant_hero_ids_json) "
        "AND json(curve.dire_hero_ids_json)=json(prediction.dire_hero_ids_json) "
        "AND curve.radiant_team_side=prediction.radiant_team_side "
        "AND curve.availability_mode='prospective' "
        "AND julianday(curve.prediction_cutoff)<=julianday(curve.first_usable_at) "
        "AND julianday(curve.first_usable_at)<=julianday(prediction.observed_at) "
        "AND curve.deployment_key=prediction.draft_deployment_key "
        "AND curve.target_snapshot_hash=prediction.draft_target_snapshot_hash "
        "AND curve.feature_dependency_revision=prediction.draft_dependency_revision "
        "AND landmark.horizon_minutes=prediction.draft_landmark_horizon_minutes "
        "AND landmark.radiant_probability=prediction.draft_landmark_radiant_probability "
        "AND landmark.quality=prediction.draft_landmark_quality "
        "AND landmark.uncertainty=prediction.draft_landmark_uncertainty "
        "AND landmark.support=prediction.draft_landmark_support "
        "AND landmark.validation_status='passed' "
        "AND landmark.global_calibration_passed=1 "
        "AND landmark.deployment_key=prediction.draft_deployment_key "
        "AND landmark.feature_hash=prediction.draft_feature_hash "
        "AND landmark.model_hash=prediction.draft_model_hash "
        "AND landmark.calibration_hash=prediction.draft_calibration_hash "
        "AND landmark.model_version=prediction.draft_model_version "
        "AND landmark.global_gate_ref=prediction.draft_global_gate_ref "
        "AND landmark.input_snapshot_hash=prediction.draft_input_snapshot_hash "
        "AND model.model_version=prediction.draft_model_version "
        "AND model.model_kind='pure_draft' "
        "AND model.feature_schema_hash=prediction.draft_feature_hash "
        "AND calibration.model_hash=prediction.draft_model_hash "
        "AND calibration.horizon_minutes=landmark.horizon_minutes "
        "AND calibration.support=landmark.support "
        "AND json_extract(deployment.model_hashes_json,'$.'||landmark.horizon_minutes)=landmark.model_hash "
        "AND json_extract(deployment.calibration_hashes_json,'$.'||landmark.horizon_minutes)=landmark.calibration_hash"
        ")))"
        if authority_available
        else "0"
    )
    included_prediction_sql = (
        f"(({strict_gate.included_sql}) AND ({vision_included_sql}) "
        f"AND ({authority_sql}))"
    )

    prediction_unknown = list(strict_gate.unknown_reasons)
    prediction_unknown.extend(
        reason
        for reason in (
            vision_invalidation_reason,
            draft_anchor_reason,
            draft_conflict_reason,
        )
        if reason is not None
    )
    if authority_reason is not None:
        prediction_unknown.append(authority_reason)
    try:
        prediction_audit_rows = connection.execute(
            f"""SELECT prediction.gate_status,
                       prediction.gate_failures_json,
                       prediction.raw_model_probability,
                       prediction.market_probability,
                       CASE WHEN {strict_gate.invalidated_sql}
                            THEN 1 ELSE 0 END AS strict_invalidated,
                       CASE WHEN {strict_gate.unverifiable_sql}
                            THEN 1 ELSE 0 END AS strict_unverifiable,
                       CASE WHEN {vision_invalidated_sql}
                            THEN 1 ELSE 0 END AS vision_invalidated,
                       CASE WHEN {included_prediction_sql}
                            THEN 1 ELSE 0 END AS included
                  FROM research_live_predictions AS prediction"""
        ).fetchall()
    except sqlite3.OperationalError:
        prediction_audit_rows = []
        prediction_unknown.append("research_prediction_audit_query_failed")
        raw_predictions = _safe_table_count(
            connection, "research_live_predictions"
        )
        prediction_reason_counts = {
            "strict_mapping_invalidated": None,
            "strict_mapping_unverifiable": None,
            "vision_invalidated": None,
        }
    else:
        raw_predictions = len(prediction_audit_rows)
        prediction_reason_counts = {
            "strict_mapping_invalidated": (
                sum(int(row[4]) for row in prediction_audit_rows)
                if strict_gate.available
                else None
            ),
            "strict_mapping_unverifiable": sum(
                int(row[5]) for row in prediction_audit_rows
            ),
            "vision_invalidated": (
                sum(int(row[6]) for row in prediction_audit_rows)
                if vision_available
                else None
            ),
        }
    predictions = [row for row in prediction_audit_rows if int(row[7]) == 1]
    gate_statuses = Counter(str(row[0]) for row in predictions)
    gate_failures: Counter[str] = Counter()
    for row in predictions:
        try:
            reasons = json.loads(str(row[1]))
        except (TypeError, ValueError):
            reasons = ["invalid_gate_failure_json"]
        gate_failures.update(str(reason) for reason in reasons)

    raw_price_labels = _safe_table_count(connection, "research_price_labels")
    price_unknown = list(prediction_unknown)
    try:
        price_audit_rows = connection.execute(
            f"""SELECT prediction.market_probability,
                       prediction.market_price,
                       label.market_probability,
                       label.price,
                       CASE WHEN {strict_gate.invalidated_sql}
                            THEN 1 ELSE 0 END AS strict_invalidated,
                       CASE WHEN {strict_gate.unverifiable_sql}
                            THEN 1 ELSE 0 END AS strict_unverifiable,
                       CASE WHEN {vision_invalidated_sql}
                            THEN 1 ELSE 0 END AS vision_invalidated,
                       CASE WHEN {included_prediction_sql}
                            THEN 1 ELSE 0 END AS included
                  FROM research_price_labels AS label
                  JOIN research_live_predictions AS prediction
                    ON prediction.prediction_key=label.prediction_key"""
        ).fetchall()
    except sqlite3.OperationalError:
        price_audit_rows = []
        price_unknown.append("research_price_label_audit_query_failed")
        price_reason_counts = {
            "strict_mapping_invalidated": None,
            "strict_mapping_unverifiable": None,
            "vision_invalidated": None,
            "prediction_lineage_unverifiable": None,
        }
    else:
        price_reason_counts = {
            "strict_mapping_invalidated": (
                sum(int(row[4]) for row in price_audit_rows)
                if strict_gate.available
                else None
            ),
            "strict_mapping_unverifiable": sum(
                int(row[5]) for row in price_audit_rows
            ),
            "vision_invalidated": (
                sum(int(row[6]) for row in price_audit_rows)
                if vision_available
                else None
            ),
            "prediction_lineage_unverifiable": (
                None
                if raw_price_labels is None
                else raw_price_labels - len(price_audit_rows)
            ),
        }
    price_rows = [row for row in price_audit_rows if int(row[7]) == 1]

    reconciliation_reason = _required_schema_reason(
        connection,
        "settlement_reconciliations",
        {
            "raybet_match_id",
            "map_number",
            "strict_mapping_id",
            "dota_match_id",
            "evidence_ref",
            "raybet_evidence_id",
            "opendota_evidence_id",
            "raybet_evidence_ref",
            "opendota_evidence_ref",
            "raybet_observed_at",
            "opendota_observed_at",
            "first_usable_at",
            "status",
        },
    )
    map_result_reason = _required_schema_reason(
        connection,
        "map_results",
        {
            "raybet_match_id",
            "map_number",
            "strict_mapping_id",
            "dota_match_id",
            "winner_side",
            "evidence_ref",
            "reconciliation_ref",
            "raybet_evidence_id",
            "opendota_evidence_id",
            "raybet_evidence_ref",
            "opendota_evidence_ref",
            "raybet_observed_at",
            "opendota_observed_at",
            "first_usable_at",
        },
    )
    source_evidence_reason = _required_schema_reason(
        connection,
        "settlement_result_evidence",
        {
            "evidence_id",
            "raybet_match_id",
            "map_number",
            "dota_match_id",
            "source",
            "status",
            "winner_side",
            "evidence_ref",
            "observed_at",
            "first_usable_at",
            "raybet_audit_key",
            "raybet_response_artifact_hash",
            "opendota_artifact_id",
            "opendota_observation_id",
            "opendota_content_hash",
        },
    )
    result_label_reason = _required_schema_reason(
        connection,
        "research_result_labels",
        {
            "strict_mapping_id",
            "reconciliation_ref",
            "raybet_evidence_id",
            "opendota_evidence_id",
            "raybet_evidence_ref",
            "opendota_evidence_ref",
            "raybet_observed_at",
            "opendota_observed_at",
            "first_usable_at",
        },
    )
    reconciliation_available = (
        reconciliation_reason is None
        and map_result_reason is None
        and source_evidence_reason is None
        and result_label_reason is None
    )
    reconciliation_confirmed_sql = (
        "EXISTS ("
        "SELECT 1 FROM settlement_reconciliations AS reconciliation "
        "JOIN map_results AS result "
        "ON result.raybet_match_id=reconciliation.raybet_match_id "
        "AND result.map_number=reconciliation.map_number "
        "AND result.strict_mapping_id=reconciliation.strict_mapping_id "
        "AND result.dota_match_id=reconciliation.dota_match_id "
        "JOIN settlement_result_evidence AS raybet_evidence "
        "ON raybet_evidence.evidence_id=result.raybet_evidence_id "
        "AND raybet_evidence.source='raybet' "
        "AND raybet_evidence.status='confirmed' "
        "JOIN settlement_result_evidence AS opendota_evidence "
        "ON opendota_evidence.evidence_id=result.opendota_evidence_id "
        "AND opendota_evidence.source='opendota' "
        "AND opendota_evidence.status='confirmed' "
        "WHERE reconciliation.raybet_match_id=prediction.raybet_match_id "
        "AND reconciliation.map_number=prediction.map_number "
        "AND reconciliation.strict_mapping_id=prediction.strict_mapping_id "
        "AND reconciliation.dota_match_id=label.dota_match_id "
        "AND label.strict_mapping_id=prediction.strict_mapping_id "
        "AND result.winner_side=label.winner_side "
        "AND result.evidence_ref=label.evidence_ref "
        "AND result.reconciliation_ref=label.reconciliation_ref "
        "AND result.raybet_evidence_id=label.raybet_evidence_id "
        "AND result.opendota_evidence_id=label.opendota_evidence_id "
        "AND result.raybet_evidence_ref=label.raybet_evidence_ref "
        "AND result.opendota_evidence_ref=label.opendota_evidence_ref "
        "AND result.raybet_observed_at=label.raybet_observed_at "
        "AND result.opendota_observed_at=label.opendota_observed_at "
        "AND result.first_usable_at=label.first_usable_at "
        "AND result.settled_at=label.settled_at "
        "AND reconciliation.evidence_ref=label.reconciliation_ref "
        "AND reconciliation.raybet_evidence_id=label.raybet_evidence_id "
        "AND reconciliation.opendota_evidence_id=label.opendota_evidence_id "
        "AND reconciliation.raybet_evidence_ref=label.raybet_evidence_ref "
        "AND reconciliation.opendota_evidence_ref=label.opendota_evidence_ref "
        "AND reconciliation.raybet_observed_at=label.raybet_observed_at "
        "AND reconciliation.opendota_observed_at=label.opendota_observed_at "
        "AND reconciliation.first_usable_at=label.first_usable_at "
        "AND raybet_evidence.evidence_ref=label.raybet_evidence_ref "
        "AND raybet_evidence.observed_at=label.raybet_observed_at "
        "AND raybet_evidence.winner_side=label.winner_side "
        "AND opendota_evidence.evidence_ref=label.opendota_evidence_ref "
        "AND opendota_evidence.observed_at=label.opendota_observed_at "
        "AND opendota_evidence.winner_side=label.winner_side "
        "AND reconciliation.status='confirmed')"
        if reconciliation_available
        else "0"
    )
    temporal_valid_sql = (
        "(julianday(prediction.observed_at) IS NOT NULL "
        "AND julianday(label.settled_at) IS NOT NULL "
        "AND julianday(prediction.observed_at)<julianday(label.settled_at))"
    )
    included_result_sql = (
        f"(({included_prediction_sql}) AND ({temporal_valid_sql}) "
        f"AND ({reconciliation_confirmed_sql}))"
    )
    raw_result_labels = _safe_table_count(connection, "research_result_labels")
    result_unknown = list(prediction_unknown)
    if reconciliation_reason is not None:
        result_unknown.append(reconciliation_reason)
    if map_result_reason is not None:
        result_unknown.append(map_result_reason)
    if source_evidence_reason is not None:
        result_unknown.append(source_evidence_reason)
    if result_label_reason is not None:
        result_unknown.append(result_label_reason)
    try:
        result_audit_rows = connection.execute(
            f"""SELECT prediction.raw_model_probability,
                       prediction.market_probability,
                       label.selected_side_win,
                       prediction.feature_hash,
                       prediction.model_hash,
                       prediction.calibration_hash,
                       prediction.gate_status,
                       CASE WHEN {strict_gate.invalidated_sql}
                            THEN 1 ELSE 0 END AS strict_invalidated,
                       CASE WHEN {strict_gate.unverifiable_sql}
                            THEN 1 ELSE 0 END AS strict_unverifiable,
                       CASE WHEN {vision_invalidated_sql}
                            THEN 1 ELSE 0 END AS vision_invalidated,
                       CASE WHEN {temporal_valid_sql}
                            THEN 1 ELSE 0 END AS temporal_valid,
                       CASE WHEN {reconciliation_confirmed_sql}
                            THEN 1 ELSE 0 END AS reconciliation_confirmed,
                       CASE WHEN {included_result_sql}
                            THEN 1 ELSE 0 END AS included
                  FROM research_result_labels AS label
                  JOIN research_live_predictions AS prediction
                    ON prediction.prediction_key=label.prediction_key"""
        ).fetchall()
    except sqlite3.OperationalError:
        result_audit_rows = []
        result_unknown.append("research_result_label_audit_query_failed")
        result_reason_counts = {
            "strict_mapping_invalidated": None,
            "strict_mapping_unverifiable": None,
            "vision_invalidated": None,
            "temporal_invalid": None,
            "reconciliation_not_confirmed": None,
            "prediction_lineage_unverifiable": None,
        }
    else:
        result_reason_counts = {
            "strict_mapping_invalidated": (
                sum(int(row[7]) for row in result_audit_rows)
                if strict_gate.available
                else None
            ),
            "strict_mapping_unverifiable": sum(
                int(row[8]) for row in result_audit_rows
            ),
            "vision_invalidated": (
                sum(int(row[9]) for row in result_audit_rows)
                if vision_available
                else None
            ),
            "temporal_invalid": sum(
                int(row[10]) == 0 for row in result_audit_rows
            ),
            "reconciliation_not_confirmed": (
                sum(int(row[11]) == 0 for row in result_audit_rows)
                if reconciliation_available
                else None
            ),
            "prediction_lineage_unverifiable": (
                None
                if raw_result_labels is None
                else raw_result_labels - len(result_audit_rows)
            ),
        }
    result_rows = [row for row in result_audit_rows if int(row[12]) == 1]
    cohort_points: dict[
        tuple[str | None, str | None, str | None, str],
        dict[str, list[tuple[float, int]]],
    ] = {}
    for row in result_rows:
        if row[0] is None:
            continue
        key = (
            None if row[3] is None else str(row[3]),
            None if row[4] is None else str(row[4]),
            None if row[5] is None else str(row[5]),
            str(row[6]),
        )
        cohort = cohort_points.setdefault(key, {"model": [], "market": []})
        cohort["model"].append((float(row[0]), int(row[2])))
        cohort["market"].append((float(row[1]), int(row[2])))
    model_cohorts = []
    for (feature_hash, model_hash, calibration_hash, gate_status), points in sorted(
        cohort_points.items(), key=lambda item: tuple("" if value is None else value for value in item[0])
    ):
        model_points = points["model"]
        market_points = points["market"]
        identity_complete = all(
            value is not None
            for value in (feature_hash, model_hash, calibration_hash)
        )
        model_cohorts.append({
            "feature_hash": feature_hash,
            "model_hash": model_hash,
            "calibration_hash": calibration_hash,
            "gate_status": gate_status,
            "identity_complete": identity_complete,
            "results": len(model_points),
            "accuracy": _accuracy(model_points),
            "brier_score": brier_score(model_points),
            "log_loss": log_loss(model_points),
            "market_accuracy": _accuracy(market_points),
            "market_brier_score": brier_score(market_points),
            "market_log_loss": log_loss(market_points),
        })
    passed_cohorts = [
        cohort
        for cohort in model_cohorts
        if cohort["gate_status"] == "passed" and cohort["identity_complete"]
    ]
    headline = passed_cohorts[0] if len(passed_cohorts) == 1 else None
    price_probability_moves = [float(row[2]) - float(row[0]) for row in price_rows]
    price_moves = [float(row[3]) - float(row[1]) for row in price_rows]
    prediction_audit = _research_audit(
        raw=raw_predictions,
        included=len(predictions),
        unknown_reasons=prediction_unknown,
        reason_counts=prediction_reason_counts,
        raw_key="raw_predictions",
        included_key="included_predictions",
        excluded_key="excluded_predictions",
    )
    price_audit = _research_audit(
        raw=raw_price_labels,
        included=len(price_rows),
        unknown_reasons=price_unknown,
        reason_counts=price_reason_counts,
        raw_key="raw_price_labels",
        included_key="included_price_labels",
        excluded_key="excluded_price_labels",
    )
    result_audit = _research_audit(
        raw=raw_result_labels,
        included=len(result_rows),
        unknown_reasons=result_unknown,
        reason_counts=result_reason_counts,
        raw_key="raw_result_labels",
        included_key="included_result_labels",
        excluded_key="excluded_result_labels",
    )
    unavailable_reasons = list(dict.fromkeys(
        [*prediction_unknown, *price_unknown, *result_unknown]
    ))
    return {
        "predictions": len(predictions),
        "raw_predictions": raw_predictions,
        "included_predictions": len(predictions),
        "excluded_predictions": prediction_audit["excluded_predictions"],
        "prediction_audit": prediction_audit,
        "with_raw_model_probability": sum(row[2] is not None for row in predictions),
        "gate_statuses": dict(sorted(gate_statuses.items())),
        "gate_failures": dict(sorted(gate_failures.items())),
        "successor_price_labels": len(price_rows),
        "raw_successor_price_labels": raw_price_labels,
        "included_successor_price_labels": len(price_rows),
        "excluded_successor_price_labels": price_audit["excluded_price_labels"],
        "price_label_audit": price_audit,
        "mean_successor_probability_move": (
            math.fsum(price_probability_moves) / len(price_probability_moves)
            if price_probability_moves
            else None
        ),
        "mean_successor_price_move": (
            math.fsum(price_moves) / len(price_moves) if price_moves else None
        ),
        "result_labels": len(result_rows),
        "raw_result_labels": raw_result_labels,
        "included_result_labels": len(result_rows),
        "excluded_result_labels": result_audit["excluded_result_labels"],
        "result_label_audit": result_audit,
        "scorable_model_results": sum(
            int(cohort["results"]) for cohort in passed_cohorts
        ),
        "model_accuracy": None if headline is None else headline["accuracy"],
        "model_brier_score": None if headline is None else headline["brier_score"],
        "model_log_loss": None if headline is None else headline["log_loss"],
        "model_cohorts": model_cohorts,
        "market_accuracy": None if headline is None else headline["market_accuracy"],
        "market_brier_score": (
            None if headline is None else headline["market_brier_score"]
        ),
        "market_log_loss": (
            None if headline is None else headline["market_log_loss"]
        ),
        "actionability": ACTIONABILITY,
        "audit_status": "available" if not unavailable_reasons else "unavailable",
        "unavailable_reasons": unavailable_reasons,
    }


def _required_schema_reason(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
) -> str | None:
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except sqlite3.OperationalError:
        return f"{table}_schema_inspection_failed"
    if exists is None:
        return f"{table}_table_missing"
    if not table_has_columns(connection, table, columns):
        return f"{table}_columns_missing"
    return None


def _safe_table_count(
    connection: sqlite3.Connection, table: str
) -> int | None:
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return None


def _research_audit(
    *,
    raw: int | None,
    included: int,
    unknown_reasons: Sequence[str],
    reason_counts: dict[str, int | None],
    raw_key: str,
    included_key: str,
    excluded_key: str,
) -> dict[str, object]:
    return {
        "status": "available" if not unknown_reasons else "unavailable",
        "unknown_reasons": list(dict.fromkeys(unknown_reasons)),
        raw_key: raw,
        included_key: included,
        excluded_key: None if raw is None else raw - included,
        "exclusion_reasons": reason_counts,
    }


__all__ = [
    "ACTIONABILITY",
    "append_research_successor_price_labels",
    "ManualClockEvidence",
    "RESEARCH_SCHEMA_VERSION",
    "ResearchPrediction",
    "ResearchPriceLabel",
    "ResearchWriteResult",
    "manual_clock_evidence",
    "record_research_prediction",
    "research_summary",
]
