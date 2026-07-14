"""Append-only, non-actionable live prediction evidence and evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Sequence

from .engine import price_groups
from .evaluation import brier_score, log_loss
from .market_state import MarketSurface
from .models import OddsSnapshot
from .profiles.draft_curve import DraftCurve, DraftPoint, MAX_LANDMARK_AGE_MINUTES
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
_OPEN_STATUSES = {"1", "5", "open", "active", "running"}


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
) -> ManualClockEvidence:
    """Return a diagnostic clock only after strict continuity checks.

    The trust value never becomes actionable.  Raw payloads remain in
    ``browser_events`` for later review.
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
        """SELECT event_id, captured_at, payload_json, capture_reason,
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
    try:
        current_payload = json.loads(str(current[2]))
    except (TypeError, ValueError):
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
        current[3] != "diagnostic_untrusted"
        or current[4] != "audit_only"
        or current[5] != "diagnostic_untrusted"
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
        previous_payload = json.loads(str(previous[2]))
    except (TypeError, ValueError):
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


def _gate_failures(point: DraftPoint | None, curve: DraftCurve) -> tuple[str, ...]:
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
            or str(row.status).casefold() not in _OPEN_STATUSES
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
    failures = list(_gate_failures(point, draft_curve))
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
    gate_failures = tuple(dict.fromkeys(failures))
    gate_status = (
        "unavailable" if point is None else "failed" if gate_failures else "passed"
    )
    manual = manual_clock_evidence(
        store.connection,
        raybet_match_id=observation.raybet_match_id,
        map_number=observation.map_number,
        transport_key=transport_key,
        transport_hash=transport_hash,
        transport_at=observed_at,
    )
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
        created_at=created,
    )
    with store.transaction():
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
    """Build read-only coverage and accuracy metrics from settled research rows."""

    try:
        predictions = connection.execute(
            """SELECT gate_status, gate_failures_json, raw_model_probability,
                      market_probability FROM research_live_predictions"""
        ).fetchall()
    except sqlite3.OperationalError:
        predictions = []
    gate_statuses = Counter(str(row[0]) for row in predictions)
    gate_failures: Counter[str] = Counter()
    for row in predictions:
        try:
            reasons = json.loads(str(row[1]))
        except (TypeError, ValueError):
            reasons = ["invalid_gate_failure_json"]
        gate_failures.update(str(reason) for reason in reasons)
    try:
        price_rows = connection.execute(
            """SELECT prediction.market_probability, prediction.market_price,
                      label.market_probability, label.price
                 FROM research_price_labels AS label
                 JOIN research_live_predictions AS prediction
                   ON prediction.prediction_key=label.prediction_key"""
        ).fetchall()
        result_rows = connection.execute(
            """SELECT prediction.raw_model_probability,
                      prediction.market_probability, label.selected_side_win
                 FROM research_result_labels AS label
                 JOIN research_live_predictions AS prediction
                   ON prediction.prediction_key=label.prediction_key"""
        ).fetchall()
    except sqlite3.OperationalError:
        price_rows = []
        result_rows = []
    model_points = [
        (float(row[0]), int(row[2])) for row in result_rows if row[0] is not None
    ]
    market_points = [(float(row[1]), int(row[2])) for row in result_rows]
    price_probability_moves = [float(row[2]) - float(row[0]) for row in price_rows]
    price_moves = [float(row[3]) - float(row[1]) for row in price_rows]
    return {
        "predictions": len(predictions),
        "with_raw_model_probability": sum(row[2] is not None for row in predictions),
        "gate_statuses": dict(sorted(gate_statuses.items())),
        "gate_failures": dict(sorted(gate_failures.items())),
        "successor_price_labels": len(price_rows),
        "mean_successor_probability_move": (
            math.fsum(price_probability_moves) / len(price_probability_moves)
            if price_probability_moves
            else None
        ),
        "mean_successor_price_move": (
            math.fsum(price_moves) / len(price_moves) if price_moves else None
        ),
        "result_labels": len(result_rows),
        "scorable_model_results": len(model_points),
        "model_accuracy": _accuracy(model_points),
        "model_brier_score": brier_score(model_points) if model_points else None,
        "model_log_loss": log_loss(model_points) if model_points else None,
        "market_accuracy": _accuracy(market_points),
        "market_brier_score": brier_score(market_points) if market_points else None,
        "market_log_loss": log_loss(market_points) if market_points else None,
        "actionability": ACTIONABILITY,
    }


__all__ = [
    "ACTIONABILITY",
    "ManualClockEvidence",
    "RESEARCH_SCHEMA_VERSION",
    "ResearchPrediction",
    "ResearchPriceLabel",
    "ResearchWriteResult",
    "manual_clock_evidence",
    "record_research_prediction",
    "research_summary",
]
