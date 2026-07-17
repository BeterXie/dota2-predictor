"""Validated, causal draft landmarks for the live shadow strategy.

Historical walk-forward predictions are evaluation evidence.  They are not a
live prediction for a new lineup, so this module deliberately fails closed
until a separately persisted live landmark is both predicted and calibrated.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timezone

from event_intelligence.draft_artifacts import (
    DraftCalibrationArtifact,
    assert_model_calibration_compatible,
    calibration_artifact_from_payload,
    model_artifact_from_payload,
)
from event_intelligence.draft_model import DraftModelArtifact, predict_draft
from event_intelligence.draft_features import (
    FEATURE_SCHEMA_HASH,
    FEATURE_VERSION,
    PURE_FEATURE_SCHEMA,
)

from ..strict_eligibility import query_strict_live_eligibility


CHECKPOINTS = (10, 20, 30, 40, 50)
MIN_LANDMARK_SUPPORT = 100
MAX_LANDMARK_AGE_MINUTES = 10.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DraftPoint:
    minute: int
    radiant_probability: float
    scaling_edge: float
    synergy_edge: float
    quality: float
    validated: bool = False
    support: int = 0
    calibration_ref: str = ""
    input_refs: tuple[str, ...] = ()
    uncertainty: float | None = None
    validation_reason: str | None = None
    feature_hash: str | None = None
    model_hash: str | None = None
    calibration_hash: str | None = None
    global_calibration_passed: bool = False
    global_gate_ref: str = ""
    model_version: str = ""
    model_kind: str = ""
    availability_mode: str = ""
    input_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        if self.minute not in CHECKPOINTS:
            raise ValueError(f"draft point minute must be one of {CHECKPOINTS}")
        for name, value in (
            ("radiant_probability", self.radiant_probability),
            ("quality", self.quality),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        for name, value in (
            ("scaling_edge", self.scaling_edge),
            ("synergy_edge", self.synergy_edge),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not isinstance(self.validated, bool):
            raise ValueError("validated must be boolean")
        if isinstance(self.support, bool) or not isinstance(self.support, int):
            raise ValueError("support must be an integer")
        if self.support < 0:
            raise ValueError("support cannot be negative")
        if self.uncertainty is not None and (
            not isinstance(self.uncertainty, (int, float))
            or not math.isfinite(self.uncertainty)
            or not 0.0 <= float(self.uncertainty) <= 0.5
        ):
            raise ValueError("uncertainty must be between 0 and 0.5 or None")
        for name, value in (
            ("feature_hash", self.feature_hash),
            ("model_hash", self.model_hash),
            ("calibration_hash", self.calibration_hash),
            ("input_snapshot_hash", self.input_snapshot_hash),
        ):
            if value is not None and not _SHA256_RE.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest or None")
        if not isinstance(self.global_calibration_passed, bool):
            raise ValueError("global_calibration_passed must be boolean")
        if self.model_kind and self.model_kind != "pure_draft":
            raise ValueError("live draft landmarks must use a pure_draft model")
        if self.availability_mode and self.availability_mode != "prospective":
            raise ValueError("live draft landmarks must be prospective")

    @property
    def passes_live_gate(self) -> bool:
        return (
            self.validated
            and self.global_calibration_passed
            and self.support >= MIN_LANDMARK_SUPPORT
            and bool(self.calibration_ref.strip())
            and bool(self.global_gate_ref.strip())
            and bool(self.input_refs)
            and self.uncertainty is not None
            and self.feature_hash is not None
            and self.model_hash is not None
            and self.calibration_hash is not None
            and bool(self.model_version.strip())
            and self.model_kind == "pure_draft"
            and self.availability_mode == "prospective"
            and self.input_snapshot_hash is not None
        )


@dataclass(frozen=True)
class DraftCurve:
    points: tuple[DraftPoint, ...]
    source_ref: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        minutes = tuple(point.minute for point in self.points)
        if len(minutes) != len(set(minutes)):
            raise ValueError("draft curve cannot contain duplicate landmarks")

    def at(self, game_clock_seconds: int) -> DraftPoint | None:
        """Return only the newest validated, non-future, non-stale landmark."""
        if isinstance(game_clock_seconds, bool) or not isinstance(game_clock_seconds, int):
            raise ValueError("game clock must be an integer number of seconds")
        if game_clock_seconds < 0:
            raise ValueError("game clock cannot be negative")
        current_minute = game_clock_seconds / 60.0
        if current_minute < CHECKPOINTS[0]:
            return None
        available = tuple(
            point for point in self.points if point.minute <= current_minute
        )
        if not available:
            return None
        selected = max(available, key=lambda point: point.minute)
        if not selected.passes_live_gate:
            return None
        if current_minute - selected.minute > MAX_LANDMARK_AGE_MINUTES:
            return None
        return selected

    def wait_reason(self, game_clock_seconds: int) -> str | None:
        if self.at(game_clock_seconds) is not None:
            return None
        current_minute = game_clock_seconds / 60.0
        if current_minute < CHECKPOINTS[0]:
            return "before_first_draft_landmark"
        available = tuple(
            point for point in self.points if point.minute <= current_minute
        )
        if not available:
            return self.unavailable_reason or "no_validated_past_draft_landmark"
        selected = max(available, key=lambda point: point.minute)
        if not selected.passes_live_gate:
            return (
                selected.validation_reason
                or self.unavailable_reason
                or "required_draft_landmark_not_validated"
            )
        return "validated_draft_landmark_stale"


def build_draft_curve(
    connection: sqlite3.Connection,
    radiant_heroes: tuple[int, ...],
    dire_heroes: tuple[int, ...],
    as_of_start_time: int,
    *,
    raybet_match_id: str | None = None,
    map_number: int | None = None,
    strict_mapping_id: int | None = None,
) -> DraftCurve:
    """Load the latest causally available prospective artifact for this map.

    Historical ``draft_predictions`` are deliberately excluded: they describe
    their own settled target maps, not this live lineup.  Missing, future,
    reconstructed, identity-mismatched, or incompletely calibrated artifacts
    all fail closed.
    """
    if len(radiant_heroes) != 5 or len(dire_heroes) != 5:
        raise ValueError("draft curve requires two complete five-hero lineups")
    heroes = radiant_heroes + dire_heroes
    if len(set(heroes)) != 10 or any(
        isinstance(hero, bool) or not isinstance(hero, int) or hero <= 0
        for hero in heroes
    ):
        raise ValueError("draft curve requires ten unique positive hero IDs")
    if isinstance(as_of_start_time, bool) or not isinstance(as_of_start_time, int):
        raise ValueError("as_of_start_time must be an integer Unix timestamp")
    target = _target_identity(raybet_match_id, map_number, strict_mapping_id)
    if target is None:
        return _unavailable(as_of_start_time, "prospective_draft_target_missing")

    lineup_hash = _lineup_hash(radiant_heroes, dire_heroes)
    try:
        curves = connection.execute(
            """SELECT curve_key, raybet_match_id, map_number, strict_mapping_id,
                      lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                      prediction_cutoff, first_usable_at, availability_mode,
                      created_at, radiant_team_side, anchor_draft_hash,
                      anchor_source_frame_ref, anchor_anchored_at,
                      deployment_key, target_snapshot_hash,
                      feature_snapshot_json
                 FROM prospective_draft_curves
                WHERE raybet_match_id=? AND map_number=? AND strict_mapping_id=?
                  AND lineup_hash=?""",
            (*target, lineup_hash),
        ).fetchall()
    except sqlite3.OperationalError:
        return _unavailable(as_of_start_time, "validated_live_draft_prediction_missing")
    if not curves:
        return _unavailable(as_of_start_time, "validated_live_draft_prediction_missing")

    as_of = datetime.fromtimestamp(as_of_start_time, timezone.utc)
    usable: list[tuple[datetime, datetime, datetime, object]] = []
    future_seen = False
    for row in curves:
        try:
            cutoff = _utc_timestamp(row[7])
            first_usable = _utc_timestamp(row[8])
            created_at = _utc_timestamp(row[10])
        except (TypeError, ValueError):
            return _unavailable(as_of_start_time, "prospective_draft_artifact_invalid")
        if first_usable > as_of or created_at > as_of:
            future_seen = True
            continue
        usable.append((first_usable, cutoff, created_at, row))
    if not usable:
        reason = (
            "prospective_draft_artifact_not_yet_usable"
            if future_seen
            else "validated_live_draft_prediction_missing"
        )
        return _unavailable(as_of_start_time, reason)

    row = max(
        usable,
        key=lambda item: (item[0], item[1], item[2], str(item[3][0])),
    )[3]
    cutoff = _utc_timestamp(row[7])
    first_usable = _utc_timestamp(row[8])
    curve_created_at = _utc_timestamp(row[10])
    if (
        not _curve_identity_matches(
            row,
            target=target,
            lineup_hash=lineup_hash,
            radiant_heroes=radiant_heroes,
            dire_heroes=dire_heroes,
        )
        or cutoff > first_usable
        or curve_created_at < cutoff
        or curve_created_at > first_usable
        or first_usable > as_of
        or str(row[9]) != "prospective"
        or not _curve_authority_matches(
            connection,
            row,
            target=target,
            as_of=as_of,
            radiant_heroes=radiant_heroes,
            dire_heroes=dire_heroes,
        )
    ):
        return _unavailable(as_of_start_time, "prospective_draft_artifact_invalid")

    curve_key = str(row[0])
    try:
        feature_values, pure_coverage = _feature_values_from_curve(row)
        landmarks = connection.execute(
            """SELECT horizon_minutes, radiant_probability, scaling_edge,
                      synergy_edge, quality, validation_status, support,
                      calibration_ref, input_refs_json, uncertainty,
                      validation_reason, feature_hash, model_hash,
                      calibration_hash, global_calibration_passed,
                      global_gate_ref, model_version, model_kind,
                      availability_mode, input_snapshot_hash, created_at,
                      raw_radiant_probability, deployment_key,
                      model_input_hash, raw_uncertainty
                 FROM prospective_draft_landmarks
                WHERE curve_key=?
                ORDER BY horizon_minutes""",
            (curve_key,),
        ).fetchall()
        landmark_created = tuple(_utc_timestamp(item[20]) for item in landmarks)
        if any(
            created < cutoff or created > first_usable
            for created in landmark_created
        ):
            raise ValueError("prospective landmark availability is not causal")
        points = tuple(
            _draft_point_from_row(
                connection,
                item,
                curve_key=curve_key,
                deployment_key=str(row[15]),
                target_snapshot_hash=str(row[16]),
                feature_values=feature_values,
                pure_coverage=pure_coverage,
            )
            for item in landmarks
        )
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return _unavailable(as_of_start_time, "prospective_draft_artifact_invalid")
    if not points:
        return _unavailable(as_of_start_time, "prospective_draft_landmarks_missing")
    return DraftCurve(
        points,
        source_ref=f"prospective-draft:{curve_key}",
        unavailable_reason=(
            None
            if any(point.passes_live_gate for point in points)
            else "prospective_draft_calibration_gate_not_passed"
        ),
    )


def _target_identity(
    raybet_match_id: str | None,
    map_number: int | None,
    strict_mapping_id: int | None,
) -> tuple[str, int, int] | None:
    match_id = "" if raybet_match_id is None else str(raybet_match_id).strip()
    if (
        not match_id
        or isinstance(map_number, bool)
        or not isinstance(map_number, int)
        or map_number <= 0
        or isinstance(strict_mapping_id, bool)
        or not isinstance(strict_mapping_id, int)
        or strict_mapping_id <= 0
    ):
        return None
    return match_id, map_number, strict_mapping_id


def _lineup_hash(
    radiant_heroes: tuple[int, ...], dire_heroes: tuple[int, ...]
) -> str:
    payload = json.dumps(
        {"dire": list(dire_heroes), "radiant": list(radiant_heroes)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _utc_timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("prospective artifact timestamps must include UTC offsets")
    return parsed.astimezone(timezone.utc)


def _curve_identity_matches(
    row: object,
    *,
    target: tuple[str, int, int],
    lineup_hash: str,
    radiant_heroes: tuple[int, ...],
    dire_heroes: tuple[int, ...],
) -> bool:
    try:
        radiant = tuple(json.loads(str(row[5])))
        dire = tuple(json.loads(str(row[6])))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return (
        _SHA256_RE.fullmatch(str(row[0])) is not None
        and (str(row[1]), int(row[2]), int(row[3])) == target
        and str(row[4]) == lineup_hash
        and radiant == radiant_heroes
        and dire == dire_heroes
        and _lineup_hash(radiant, dire) == lineup_hash
    )


def _curve_authority_matches(
    connection: sqlite3.Connection,
    row: object,
    *,
    target: tuple[str, int, int],
    as_of: datetime,
    radiant_heroes: tuple[int, ...],
    dire_heroes: tuple[int, ...],
) -> bool:
    try:
        radiant_team_side = str(row[11])
        anchor_hash = str(row[12])
        anchor_frame = str(row[13])
        anchor_at = _utc_timestamp(row[14])
        deployment_key = str(row[15])
        target_snapshot_hash = str(row[16])
        cutoff = _utc_timestamp(row[7])
    except (IndexError, TypeError, ValueError):
        return False
    if (
        radiant_team_side not in {"team_one", "team_two"}
        or _SHA256_RE.fullmatch(anchor_hash) is None
        or not anchor_frame
        or anchor_at > cutoff
        or _SHA256_RE.fullmatch(deployment_key) is None
        or _SHA256_RE.fullmatch(target_snapshot_hash) is None
    ):
        return False
    eligibility = query_strict_live_eligibility(
        connection,
        raybet_match_id=target[0],
        map_number=target[1],
        transport_observed_at=as_of,
    )
    if (
        not eligibility.eligible
        or eligibility.mapping is None
        or eligibility.mapping.mapping_id != target[2]
    ):
        return False
    try:
        anchor = connection.execute(
            """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                      radiant_team_side, team_side_anchored_at,
                      team_side_source_frame_ref, anchored_at,
                      source_frame_ref, status
                 FROM vision_draft_anchors
                WHERE raybet_match_id=? AND map_number=?""",
            (target[0], target[1]),
        ).fetchone()
        if anchor is None:
            return False
        stored_radiant = tuple(json.loads(str(anchor[1])))
        stored_dire = tuple(json.loads(str(anchor[2])))
        team_side_at = _utc_timestamp(anchor[4])
        stored_anchor_at = _utc_timestamp(anchor[6])
        has_conflict = connection.execute(
            """SELECT 1 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=? LIMIT 1""",
            (target[0], target[1]),
        ).fetchone()
        deployment = connection.execute(
            """SELECT 1 FROM draft_deployment_bundles
                WHERE deployment_key=?""",
            (deployment_key,),
        ).fetchone()
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return False
    expected_anchor_hash = hashlib.sha256(
        json.dumps(
            {"radiant": list(radiant_heroes), "dire": list(dire_heroes)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return (
        deployment is not None
        and has_conflict is None
        and str(anchor[0]) == anchor_hash == expected_anchor_hash
        and stored_radiant == radiant_heroes
        and stored_dire == dire_heroes
        and str(anchor[3]) == radiant_team_side
        and team_side_at <= cutoff
        and stored_anchor_at == anchor_at
        and str(anchor[5])
        and str(anchor[7]) == anchor_frame
        and str(anchor[8]) == "anchored"
    )


def _feature_values_from_curve(row: object) -> tuple[dict[str, float | None], float]:
    return _feature_values_from_payload(
        str(row[17]),
        target_snapshot_hash=str(row[16]),
        prediction_cutoff=row[7],
    )


def _feature_values_from_payload(
    payload_json: str,
    *,
    target_snapshot_hash: str,
    prediction_cutoff: object,
) -> tuple[dict[str, float | None], float]:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("feature snapshot must be an object")
    if (
        payload.get("availability_mode") != "prospective"
        or payload.get("feature_version") != FEATURE_VERSION
        or payload.get("feature_schema_hash") != FEATURE_SCHEMA_HASH
        or payload.get("input_hash") != target_snapshot_hash
        or _utc_timestamp(payload.get("prediction_cutoff"))
        != _utc_timestamp(prediction_cutoff)
    ):
        raise ValueError("feature snapshot identity does not match curve")
    schema = payload.get("feature_schema")
    features = payload.get("pure_features")
    if not isinstance(schema, list) or not isinstance(features, list):
        raise ValueError("feature snapshot schema is incomplete")
    if not set(PURE_FEATURE_SCHEMA).issubset(set(schema)):
        raise ValueError("feature snapshot lacks the pure feature schema")
    values: dict[str, float | None] = {}
    for feature in features:
        if not isinstance(feature, dict):
            raise ValueError("feature snapshot row must be an object")
        name = feature.get("name")
        value = feature.get("value")
        support = feature.get("support")
        coverage = feature.get("coverage")
        evidence_ids = feature.get("evidence_ids")
        if (
            not isinstance(name, str)
            or name not in PURE_FEATURE_SCHEMA
            or name in values
            or isinstance(support, bool)
            or not isinstance(support, int)
            or support < 0
            or isinstance(coverage, bool)
            or not isinstance(coverage, (int, float))
            or not math.isfinite(float(coverage))
            or not 0.0 <= float(coverage) <= 1.0
            or not isinstance(evidence_ids, list)
            or any(not isinstance(ref, str) or not ref for ref in evidence_ids)
        ):
            raise ValueError("feature snapshot row is invalid")
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("feature snapshot value is invalid")
        values[name] = None if value is None else float(value)
    if set(values) != set(PURE_FEATURE_SCHEMA):
        raise ValueError("feature snapshot pure values are incomplete")
    coverage = payload.get("pure_coverage")
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, (int, float))
        or not math.isfinite(float(coverage))
        or not 0.0 <= float(coverage) <= 1.0
    ):
        raise ValueError("feature snapshot pure coverage is invalid")
    return values, float(coverage)


@lru_cache(maxsize=128)
def _cached_model(artifact_json: str) -> DraftModelArtifact:
    payload = json.loads(artifact_json)
    if not isinstance(payload, dict):
        raise ValueError("model artifact JSON must be an object")
    return model_artifact_from_payload(payload)


@lru_cache(maxsize=128)
def _cached_calibration(artifact_json: str) -> DraftCalibrationArtifact:
    payload = json.loads(artifact_json)
    if not isinstance(payload, dict):
        raise ValueError("calibration artifact JSON must be an object")
    return calibration_artifact_from_payload(payload)


def _prospective_calibration_evidence_matches(
    connection: sqlite3.Connection,
    calibration: DraftCalibrationArtifact,
) -> bool:
    if calibration.evidence_mode != "prospective":
        return False
    model_row = connection.execute(
        """SELECT artifact_json FROM draft_model_artifacts
            WHERE model_hash=? AND horizon_minutes=?""",
        (calibration.model_hash, calibration.horizon_minutes),
    ).fetchone()
    if model_row is None:
        return False
    try:
        model = _cached_model(str(model_row[0]))
        assert_model_calibration_compatible(model, calibration)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    for sample in (*calibration.fit_samples, *calibration.evaluation_samples):
        try:
            curve_key, horizon_text = sample.sample_id.rsplit(":", 1)
            horizon = int(horizon_text)
        except (TypeError, ValueError):
            return False
        if horizon != calibration.horizon_minutes:
            return False
        row = connection.execute(
            """SELECT landmark.raw_radiant_probability,
                      outcome.radiant_win, curve.first_usable_at,
                      outcome.settled_at, curve.raybet_match_id,
                      mapping.event_id, curve.feature_snapshot_json,
                      curve.target_snapshot_hash, curve.prediction_cutoff,
                      landmark.model_input_hash, landmark.raw_uncertainty,
                      curve.strict_mapping_id, curve.map_number
                 FROM prospective_draft_curves AS curve
                 JOIN prospective_draft_landmarks AS landmark
                   ON landmark.curve_key=curve.curve_key
                  AND landmark.horizon_minutes=?
                  AND landmark.model_hash=?
                 JOIN prospective_draft_outcomes AS outcome
                   ON outcome.curve_key=curve.curve_key
                  AND outcome.strict_mapping_id=curve.strict_mapping_id
                 JOIN map_results AS result
                   ON result.raybet_match_id=curve.raybet_match_id
                  AND result.map_number=curve.map_number
                  AND result.dota_match_id=outcome.dota_match_id
                  AND result.winner_side=outcome.winner_side
                 JOIN settlement_reconciliations AS reconciliation
                   ON reconciliation.raybet_match_id=curve.raybet_match_id
                  AND reconciliation.map_number=curve.map_number
                  AND reconciliation.dota_match_id=outcome.dota_match_id
                  AND reconciliation.status='confirmed'
                  AND reconciliation.raybet_winner_side=outcome.winner_side
                  AND reconciliation.opendota_winner_side=outcome.winner_side
                 JOIN settlement_result_evidence AS raybet_evidence
                   ON raybet_evidence.raybet_match_id=curve.raybet_match_id
                  AND raybet_evidence.map_number=curve.map_number
                  AND raybet_evidence.dota_match_id=outcome.dota_match_id
                  AND raybet_evidence.source='raybet'
                  AND raybet_evidence.status='confirmed'
                  AND raybet_evidence.winner_side=outcome.winner_side
                  AND raybet_evidence.evidence_ref=
                      reconciliation.raybet_evidence_ref
                 JOIN settlement_result_evidence AS opendota_evidence
                   ON opendota_evidence.raybet_match_id=curve.raybet_match_id
                  AND opendota_evidence.map_number=curve.map_number
                  AND opendota_evidence.dota_match_id=outcome.dota_match_id
                  AND opendota_evidence.source='opendota'
                  AND opendota_evidence.status='confirmed'
                  AND opendota_evidence.winner_side=outcome.winner_side
                  AND opendota_evidence.evidence_ref=
                      reconciliation.opendota_evidence_ref
                 JOIN strict_live_map_mappings AS mapping
                   ON mapping.mapping_id=curve.strict_mapping_id
                  AND mapping.raybet_match_id=curve.raybet_match_id
                  AND mapping.map_number=curve.map_number
                 JOIN vision_draft_anchors AS anchor
                   ON anchor.raybet_match_id=curve.raybet_match_id
                  AND anchor.map_number=curve.map_number
                  AND anchor.status='anchored'
                  AND anchor.draft_hash=curve.anchor_draft_hash
                  AND anchor.radiant_team_side=curve.radiant_team_side
                 LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
                   ON invalidation.mapping_id=mapping.mapping_id
                WHERE curve.curve_key=?
                  AND invalidation.mapping_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_draft_conflicts AS conflict
                       WHERE conflict.raybet_match_id=curve.raybet_match_id
                         AND conflict.map_number=curve.map_number
                  )""",
            (horizon, calibration.model_hash, curve_key),
        ).fetchone()
        if row is None:
            return False
        strict = query_strict_live_eligibility(
            connection,
            raybet_match_id=str(row[4]),
            map_number=int(row[12]),
            transport_observed_at=_utc_timestamp(row[2]),
        )
        if (
            not strict.eligible
            or strict.mapping is None
            or strict.mapping.mapping_id != int(row[11])
        ):
            return False
        try:
            feature_values, _coverage = _feature_values_from_payload(
                str(row[6]),
                target_snapshot_hash=str(row[7]),
                prediction_cutoff=row[8],
            )
            prediction = predict_draft(model, feature_values)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if (
            prediction.probability is None
            or prediction.uncertainty is None
            or prediction.input_snapshot_hash != str(row[9])
            or not math.isclose(
                prediction.probability,
                float(row[0]),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not math.isclose(
                prediction.uncertainty,
                float(row[10]),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            return False
        expected = (
            float(row[0]),
            int(row[1]),
            _utc_timestamp(row[2]),
            _utc_timestamp(row[3]),
            f"raybet:{row[4]}",
            str(row[5]),
        )
        if (
            not math.isclose(sample.probability, expected[0], rel_tol=0.0, abs_tol=1e-12)
            or sample.outcome != expected[1]
            or sample.observed_at != expected[2]
            or sample.settled_at != expected[3]
            or sample.cluster_id != expected[4]
            or sample.event_id != expected[5]
        ):
            return False
    return True


def _draft_point_from_row(
    connection: sqlite3.Connection,
    row: object,
    *,
    curve_key: str,
    deployment_key: str,
    target_snapshot_hash: str,
    feature_values: dict[str, float | None],
    pure_coverage: float,
) -> DraftPoint:
    refs = json.loads(str(row[8]))
    if not isinstance(refs, list) or not refs or any(
        not isinstance(ref, str) or not ref.strip() for ref in refs
    ):
        raise ValueError("prospective draft input refs must be non-empty strings")
    status = str(row[5])
    if status not in {"passed", "failed", "insufficient_evidence"}:
        raise ValueError("invalid prospective draft validation status")
    if str(row[22]) != deployment_key or str(row[19]) != target_snapshot_hash:
        raise ValueError("landmark deployment identity does not match curve")
    model_row = connection.execute(
        """SELECT model_version, model_kind, horizon_minutes,
                  feature_schema_hash, training_input_hash, artifact_json
             FROM draft_model_artifacts WHERE model_hash=?""",
        (str(row[12]),),
    ).fetchone()
    calibration_row = connection.execute(
        """SELECT model_hash, calibration_version, horizon_minutes,
                  evidence_mode, support, artifact_json
             FROM draft_calibration_artifacts WHERE calibration_hash=?""",
        (str(row[13]),),
    ).fetchone()
    deployment_row = connection.execute(
        """SELECT model_hashes_json, calibration_hashes_json
             FROM draft_deployment_bundles WHERE deployment_key=?""",
        (deployment_key,),
    ).fetchone()
    if model_row is None or calibration_row is None or deployment_row is None:
        raise ValueError("landmark authority artifact is missing")
    model = _cached_model(str(model_row[5]))
    calibration = _cached_calibration(str(calibration_row[5]))
    assert_model_calibration_compatible(model, calibration)
    horizon = int(row[0])
    if tuple(model_row[:5]) != (
        model.model_version,
        model.model_kind,
        model.horizon_minutes,
        model.feature_schema_hash,
        model.training_input_hash,
    ) or tuple(calibration_row[:5]) != (
        calibration.model_hash,
        calibration.calibration_version,
        calibration.horizon_minutes,
        calibration.evidence_mode,
        calibration.support,
    ):
        raise ValueError("artifact columns disagree with canonical JSON")
    model_hashes = json.loads(str(deployment_row[0]))
    calibration_hashes = json.loads(str(deployment_row[1]))
    if (
        not isinstance(model_hashes, dict)
        or not isinstance(calibration_hashes, dict)
        or model_hashes.get(str(horizon)) != model.model_hash
        or calibration_hashes.get(str(horizon)) != calibration.calibration_hash
        or model.model_hash != str(row[12])
        or calibration.calibration_hash != str(row[13])
        or model.model_version != str(row[16])
        or model.model_kind != str(row[17])
        or model.feature_schema_hash != str(row[11])
        or horizon != model.horizon_minutes
        or str(row[18]) != "prospective"
    ):
        raise ValueError("landmark artifact references disagree")
    prediction = predict_draft(model, feature_values)
    if prediction.probability is None or prediction.uncertainty is None:
        raise ValueError("landmark model could not reproduce a prediction")
    raw_probability = float(row[21])
    raw_uncertainty = float(row[24])
    if (
        prediction.input_snapshot_hash != str(row[23])
        or not math.isclose(
            prediction.probability,
            raw_probability,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or not math.isclose(
            prediction.uncertainty,
            raw_uncertainty,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("landmark raw prediction does not recompute")
    calibrated_probability = calibration.apply(raw_probability)
    published_uncertainty = min(
        0.5,
        raw_uncertainty * max(1.0, abs(calibration.slope)),
    )
    scaling = feature_values["scaling_40m_win_rate_diff"] or 0.0
    synergy = feature_values["synergy_win_rate_diff"] or 0.0
    if (
        not math.isclose(float(row[1]), calibrated_probability, rel_tol=0.0, abs_tol=1e-12)
        or not math.isclose(float(row[2]), scaling, rel_tol=0.0, abs_tol=1e-12)
        or not math.isclose(float(row[3]), synergy, rel_tol=0.0, abs_tol=1e-12)
        or not math.isclose(float(row[4]), pure_coverage, rel_tol=0.0, abs_tol=1e-12)
        or not math.isclose(float(row[9]), published_uncertainty, rel_tol=0.0, abs_tol=1e-12)
        or int(row[6]) != calibration.support
        or str(row[7]) != f"draft-calibration:{calibration.calibration_hash}"
        or str(row[15]) != f"draft-calibration:{calibration.calibration_hash}"
    ):
        raise ValueError("landmark derived values do not recompute")
    authoritative_gate = (
        calibration.passes_live_gate
        and calibration.support >= MIN_LANDMARK_SUPPORT
        and _prospective_calibration_evidence_matches(connection, calibration)
    )
    if bool(row[14]) != authoritative_gate or (status == "passed") != authoritative_gate:
        raise ValueError("persisted landmark gate disagrees with authority artifacts")
    return DraftPoint(
        minute=horizon,
        radiant_probability=calibrated_probability,
        scaling_edge=float(row[2]),
        synergy_edge=float(row[3]),
        quality=float(row[4]),
        validated=authoritative_gate,
        support=calibration.support,
        calibration_ref=str(row[7]),
        input_refs=(f"prospective-draft:{curve_key}", *tuple(refs)),
        uncertainty=None if row[9] is None else float(row[9]),
        validation_reason=None if row[10] is None else str(row[10]),
        feature_hash=None if row[11] is None else str(row[11]),
        model_hash=None if row[12] is None else str(row[12]),
        calibration_hash=None if row[13] is None else str(row[13]),
        global_calibration_passed=authoritative_gate,
        global_gate_ref=str(row[15]),
        model_version=str(row[16]),
        model_kind=str(row[17]),
        availability_mode=str(row[18]),
        input_snapshot_hash=None if row[19] is None else str(row[19]),
    )


def _unavailable(as_of_start_time: int, reason: str) -> DraftCurve:
    return DraftCurve(
        (),
        source_ref=f"live-draft-unavailable:{as_of_start_time}",
        unavailable_reason=reason,
    )
