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
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from event_intelligence.draft_artifacts import (
    DraftCalibrationArtifact,
    assert_model_calibration_compatible,
    canonical_hash,
    load_calibration_artifact_json,
    load_model_artifact_json,
)
from event_intelligence.deployment import (
    DEPLOYMENT_VERSION,
    FrozenDraftDeployment,
    load_prospective_history,
    split_calibration_samples,
)
from event_intelligence.draft_model import DraftModelArtifact, predict_draft
from event_intelligence.draft_features import (
    DraftFeatureSnapshot,
    load_draft_feature_artifact_json,
)

from ..draft_evidence import (
    draft_dependency_snapshot_reason,
    prospective_outcome_authority,
)
from ..draft_publisher import (
    DraftAnchor,
    _prospective_calibration_samples,
    build_live_draft_target,
    draft_anchor_frames_are_authoritative,
    load_frozen_deployment,
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
    landmark_key: str | None = None
    curve_key: str | None = None
    deployment_key: str | None = None
    target_snapshot_hash: str | None = None

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
            ("landmark_key", self.landmark_key),
            ("curve_key", self.curve_key),
            ("deployment_key", self.deployment_key),
            ("target_snapshot_hash", self.target_snapshot_hash),
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
    authority_revision: int | None = None
    dependency_revision: int | None = None
    curve_key: str | None = None
    deployment_key: str | None = None
    target_snapshot_hash: str | None = None
    strict_mapping_id: int | None = None

    def __post_init__(self) -> None:
        minutes = tuple(point.minute for point in self.points)
        if len(minutes) != len(set(minutes)):
            raise ValueError("draft curve cannot contain duplicate landmarks")
        for name, value in (
            ("authority_revision", self.authority_revision),
            ("dependency_revision", self.dependency_revision),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer or None")
        for name, value in (
            ("curve_key", self.curve_key),
            ("deployment_key", self.deployment_key),
            ("target_snapshot_hash", self.target_snapshot_hash),
        ):
            if value is not None and _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest or None")
        if self.curve_key is not None and self.source_ref != (
            f"prospective-draft:{self.curve_key}"
        ):
            raise ValueError("curve source_ref must identify its exact curve_key")
        if self.strict_mapping_id is not None and (
            isinstance(self.strict_mapping_id, bool)
            or not isinstance(self.strict_mapping_id, int)
            or self.strict_mapping_id < 1
        ):
            raise ValueError("strict_mapping_id must be a positive integer or None")

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
    try:
        authority_before = _draft_authority_generation(connection)
    except (sqlite3.Error, TypeError, ValueError):
        return _unavailable(as_of_start_time, "prospective_draft_authority_missing")

    lineup_hash = _lineup_hash(radiant_heroes, dire_heroes)
    try:
        curves = connection.execute(
            """SELECT curve_key, raybet_match_id, map_number, strict_mapping_id,
                      lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                      prediction_cutoff, first_usable_at, availability_mode,
                      created_at, radiant_team_side, anchor_draft_hash,
                      anchor_source_frame_ref, anchor_anchored_at,
                      anchor_team_side_source_frame_ref,
                      anchor_team_side_anchored_at,
                      deployment_key, target_snapshot_hash,
                      feature_snapshot_json, feature_dependency_fingerprint,
                      feature_dependency_revision
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
    ):
        return _unavailable(as_of_start_time, "prospective_draft_artifact_invalid")
    feature_snapshot = _curve_feature_snapshot(
        connection,
        row,
        target=target,
        as_of=as_of,
        radiant_heroes=radiant_heroes,
        dire_heroes=dire_heroes,
    )
    if feature_snapshot is None:
        return _unavailable(as_of_start_time, "prospective_draft_artifact_invalid")

    curve_key = str(row[0])
    try:
        deployment_generation_before = _draft_deployment_generation(
            connection,
            str(row[17]),
        )
        deployment = _live_frozen_deployment(connection, str(row[17]))
        if deployment.deployment_key != str(row[17]):
            raise ValueError("curve deployment identity does not match")
        feature_values, pure_coverage = _feature_values_from_snapshot(
            feature_snapshot,
            target_snapshot_hash=str(row[18]),
            prediction_cutoff=row[7],
        )
        landmarks = connection.execute(
            """SELECT horizon_minutes, radiant_probability, scaling_edge,
                      synergy_edge, quality, validation_status, support,
                      calibration_ref, input_refs_json, uncertainty,
                      validation_reason, feature_hash, model_hash,
                      calibration_hash, global_calibration_passed,
                      global_gate_ref, model_version, model_kind,
                      availability_mode, input_snapshot_hash, created_at,
                      raw_radiant_probability, deployment_key,
                      model_input_hash, raw_uncertainty, landmark_key
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
                deployment=deployment,
                curve_key=curve_key,
                    deployment_key=str(row[17]),
                    target_snapshot_hash=str(row[18]),
                feature_values=feature_values,
                pure_coverage=pure_coverage,
                prediction_cutoff=cutoff,
                curve_created_at=curve_created_at,
            )
            for item in landmarks
        )
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return _unavailable(as_of_start_time, "prospective_draft_artifact_invalid")
    if not points:
        return _unavailable(as_of_start_time, "prospective_draft_landmarks_missing")
    try:
        authority_after = _draft_authority_generation(connection)
        deployment_generation_after = _draft_deployment_generation(
            connection,
            str(row[17]),
        )
    except (sqlite3.Error, TypeError, ValueError):
        return _unavailable(as_of_start_time, "prospective_draft_authority_missing")
    if (
        authority_after[1:] != authority_before[1:]
        or deployment_generation_after != deployment_generation_before
    ):
        return _unavailable(as_of_start_time, "prospective_draft_authority_changed")
    return DraftCurve(
        points,
        source_ref=f"prospective-draft:{curve_key}",
        unavailable_reason=(
            None
            if any(point.passes_live_gate for point in points)
            else "prospective_draft_calibration_gate_not_passed"
        ),
        authority_revision=authority_after[1],
        dependency_revision=authority_after[2],
        curve_key=curve_key,
        deployment_key=str(row[17]),
        target_snapshot_hash=str(row[18]),
        strict_mapping_id=target[2],
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


@lru_cache(maxsize=1)
def _cached_history(
    connection: sqlite3.Connection,
    dependency_revision: int,
):
    if dependency_revision < 1:
        raise ValueError("draft dependency revision must be positive")
    return load_prospective_history(connection)


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


def _curve_feature_snapshot(
    connection: sqlite3.Connection,
    row: object,
    *,
    target: tuple[str, int, int],
    as_of: datetime,
    radiant_heroes: tuple[int, ...],
    dire_heroes: tuple[int, ...],
) -> DraftFeatureSnapshot | None:
    try:
        radiant_team_side = str(row[11])
        anchor_hash = str(row[12])
        anchor_frame = str(row[13])
        anchor_at = _utc_timestamp(row[14])
        team_side_frame = str(row[15])
        team_side_at = _utc_timestamp(row[16])
        deployment_key = str(row[17])
        target_snapshot_hash = str(row[18])
        cutoff = _utc_timestamp(row[7])
        feature_fingerprint = str(row[20])
        feature_revision = int(row[21])
    except (IndexError, TypeError, ValueError):
        return None
    if (
        radiant_team_side not in {"team_one", "team_two"}
        or _SHA256_RE.fullmatch(anchor_hash) is None
        or not anchor_frame
        or anchor_at > cutoff
        or not team_side_frame
        or team_side_at > cutoff
        or _SHA256_RE.fullmatch(deployment_key) is None
        or _SHA256_RE.fullmatch(target_snapshot_hash) is None
        or _SHA256_RE.fullmatch(feature_fingerprint) is None
        or feature_revision < 1
    ):
        return None
    if draft_dependency_snapshot_reason(
        connection,
        expected_revision=feature_revision,
        expected_fingerprint=feature_fingerprint,
        cutoff=cutoff,
    ) is not None:
        return None
    eligibility = query_strict_live_eligibility(
        connection,
        raybet_match_id=target[0],
        map_number=target[1],
        transport_observed_at=cutoff,
    )
    if (
        not eligibility.eligible
        or eligibility.mapping is None
        or eligibility.mapping.mapping_id != target[2]
    ):
        return None
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
            return None
        stored_radiant = tuple(json.loads(str(anchor[1])))
        stored_dire = tuple(json.loads(str(anchor[2])))
        stored_team_side_at = _utc_timestamp(anchor[4])
        stored_anchor_at = _utc_timestamp(anchor[6])
        has_conflict = connection.execute(
            """SELECT 1 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=? LIMIT 1""",
            (target[0], target[1]),
        ).fetchone()
        deployment = connection.execute(
            """SELECT training_cutoff, dependency_fingerprint,
                      dependency_revision, created_at
                 FROM draft_deployment_bundles
                WHERE deployment_key=?""",
            (deployment_key,),
        ).fetchone()
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return None
    expected_anchor_hash = hashlib.sha256(
        json.dumps(
            {"radiant": list(radiant_heroes), "dire": list(dire_heroes)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        deployment is None
        or has_conflict is not None
        or str(anchor[0]) != anchor_hash
        or anchor_hash != expected_anchor_hash
        or stored_radiant != radiant_heroes
        or stored_dire != dire_heroes
        or str(anchor[3]) != radiant_team_side
        or stored_team_side_at != team_side_at
        or str(anchor[5]) != team_side_frame
        or stored_anchor_at != anchor_at
        or str(anchor[7]) != anchor_frame
        or str(anchor[8]) != "anchored"
    ):
        return None
    try:
        deployment_cutoff = _utc_timestamp(deployment[0])
        deployment_fingerprint = str(deployment[1])
        deployment_revision = int(deployment[2])
        deployment_created = _utc_timestamp(deployment[3])
        draft_anchor = DraftAnchor(
            raybet_match_id=target[0],
            map_number=target[1],
            draft_hash=anchor_hash,
            radiant_heroes=stored_radiant,
            dire_heroes=stored_dire,
            radiant_team_side=radiant_team_side,
            anchored_at=stored_anchor_at,
            source_frame_ref=str(anchor[7]),
            team_side_anchored_at=stored_team_side_at,
            team_side_source_frame_ref=team_side_frame,
        )
    except (TypeError, ValueError):
        return None
    if (
        _SHA256_RE.fullmatch(deployment_fingerprint) is None
        or deployment_revision < 1
        or deployment_cutoff > deployment_created
        or deployment_created > cutoff
        or draft_dependency_snapshot_reason(
            connection,
            expected_revision=deployment_revision,
            expected_fingerprint=deployment_fingerprint,
            cutoff=deployment_cutoff,
        )
        is not None
        or not draft_anchor_frames_are_authoritative(connection, draft_anchor)
    ):
        return None
    try:
        before = _draft_authority_generation(connection)
        expected_target = build_live_draft_target(
            connection,
            draft_anchor,
            eligibility.mapping,
            cutoff,
        )
        history = _cached_history(connection, before[2])
        _feature_payload, feature_snapshot = load_draft_feature_artifact_json(
            str(row[19]),
            target=expected_target,
            history=history,
        )
        after = _draft_authority_generation(connection)
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return None
    if (
        after[1:] != before[1:]
        or feature_snapshot.match_id != expected_target.match_id
        or feature_snapshot.prediction_cutoff != cutoff
        or feature_snapshot.input_hash != target_snapshot_hash
    ):
        return None
    return feature_snapshot


def _curve_authority_matches(
    connection: sqlite3.Connection,
    row: object,
    *,
    target: tuple[str, int, int],
    as_of: datetime,
    radiant_heroes: tuple[int, ...],
    dire_heroes: tuple[int, ...],
) -> bool:
    return (
        _curve_feature_snapshot(
            connection,
            row,
            target=target,
            as_of=as_of,
            radiant_heroes=radiant_heroes,
            dire_heroes=dire_heroes,
        )
        is not None
    )


def _curve_row_by_key(connection: sqlite3.Connection, curve_key: str):
    return connection.execute(
        """SELECT curve_key, raybet_match_id, map_number, strict_mapping_id,
                  lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                  prediction_cutoff, first_usable_at, availability_mode,
                  created_at, radiant_team_side, anchor_draft_hash,
                  anchor_source_frame_ref, anchor_anchored_at,
                  anchor_team_side_source_frame_ref,
                  anchor_team_side_anchored_at,
                  deployment_key, target_snapshot_hash,
                  feature_snapshot_json, feature_dependency_fingerprint,
                  feature_dependency_revision
             FROM prospective_draft_curves
            WHERE curve_key=?""",
        (curve_key,),
    ).fetchone()


@lru_cache(maxsize=1024)
def _cached_curve_authority_matches_by_key(
    connection: sqlite3.Connection,
    curve_key: str,
    authority_revision: int,
    dependency_revision: int,
) -> bool:
    if authority_revision < 1 or dependency_revision < 1:
        return False
    row = _curve_row_by_key(connection, curve_key)
    if row is None:
        return False
    try:
        radiant_heroes = tuple(json.loads(str(row[5])))
        dire_heroes = tuple(json.loads(str(row[6])))
        as_of = _utc_timestamp(row[8])
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return _curve_authority_matches(
        connection,
        row,
        target=(str(row[1]), int(row[2]), int(row[3])),
        as_of=as_of,
        radiant_heroes=radiant_heroes,
        dire_heroes=dire_heroes,
    )


def prospective_curve_authority_matches(
    connection: sqlite3.Connection,
    curve_key: str,
) -> bool:
    try:
        before = _draft_authority_generation(connection)
        result = _cached_curve_authority_matches_by_key(
            connection,
            curve_key,
            before[1],
            before[2],
        )
        after = _draft_authority_generation(connection)
    except (sqlite3.Error, TypeError, ValueError):
        return False
    return after[1:] == before[1:] and result


def _curve_feature_snapshot_by_key(
    connection: sqlite3.Connection,
    curve_key: str,
) -> DraftFeatureSnapshot | None:
    try:
        before = _draft_authority_generation(connection)
        row = _curve_row_by_key(connection, curve_key)
        if row is None:
            return None
        radiant_heroes = tuple(json.loads(str(row[5])))
        dire_heroes = tuple(json.loads(str(row[6])))
        snapshot = _curve_feature_snapshot(
            connection,
            row,
            target=(str(row[1]), int(row[2]), int(row[3])),
            as_of=_utc_timestamp(row[8]),
            radiant_heroes=radiant_heroes,
            dire_heroes=dire_heroes,
        )
        after = _draft_authority_generation(connection)
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return None
    if after[1:] != before[1:]:
        return None
    return snapshot


def _feature_values_from_snapshot(
    snapshot: DraftFeatureSnapshot,
    *,
    target_snapshot_hash: str,
    prediction_cutoff: object,
) -> tuple[dict[str, float | None], float]:
    if (
        snapshot.availability_mode.value != "prospective"
        or snapshot.input_hash != target_snapshot_hash
        or snapshot.prediction_cutoff != _utc_timestamp(prediction_cutoff)
    ):
        raise ValueError("feature snapshot identity does not match curve")
    return snapshot.pure_values(), snapshot.pure_coverage


@lru_cache(maxsize=128)
def _cached_model(artifact_json: str) -> DraftModelArtifact:
    return load_model_artifact_json(artifact_json)


@lru_cache(maxsize=128)
def _cached_calibration(artifact_json: str) -> DraftCalibrationArtifact:
    return load_calibration_artifact_json(artifact_json)


def _verify_prospective_calibration_evidence(
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
        complete = _prospective_calibration_samples(
            connection,
            horizon_minutes=calibration.horizon_minutes,
            model_hash=calibration.model_hash,
        )
        expected_fit, expected_evaluation = split_calibration_samples(complete)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if (
        calibration.fit_samples != expected_fit
        or calibration.evaluation_samples != expected_evaluation
    ):
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
                      outcome.first_usable_at, curve.raybet_match_id,
                      mapping.event_id, curve.curve_key,
                      curve.target_snapshot_hash, curve.prediction_cutoff,
                      landmark.model_input_hash, landmark.raw_uncertainty,
                      curve.strict_mapping_id, curve.map_number,
                      outcome.settled_at, curve.radiant_team_side,
                      outcome.winner_side, outcome.evidence_ref,
                      outcome.evidence_hash, outcome.dota_match_id,
                      result.evidence_ref, result.settled_at,
                      reconciliation.first_observed_at, outcome.created_at,
                      raybet_evidence.source, raybet_evidence.status,
                      raybet_evidence.winner_side, raybet_evidence.evidence_ref,
                      raybet_evidence.facts_json, raybet_evidence.observed_at,
                      opendota_evidence.source, opendota_evidence.status,
                      opendota_evidence.winner_side,
                      opendota_evidence.evidence_ref,
                      opendota_evidence.facts_json,
                      opendota_evidence.observed_at
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
                   AND result.strict_mapping_id=curve.strict_mapping_id
                   AND result.dota_match_id=outcome.dota_match_id
                  AND result.winner_side=outcome.winner_side
                  JOIN settlement_reconciliations AS reconciliation
                    ON reconciliation.raybet_match_id=curve.raybet_match_id
                   AND reconciliation.map_number=curve.map_number
                   AND reconciliation.strict_mapping_id=curve.strict_mapping_id
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
                  )
                  AND NOT EXISTS (
                      SELECT 1
                        FROM prospective_draft_curves AS earlier
                        JOIN prospective_draft_landmarks AS earlier_landmark
                          ON earlier_landmark.curve_key=earlier.curve_key
                         AND earlier_landmark.horizon_minutes=?
                         AND earlier_landmark.model_hash=?
                        JOIN strict_live_map_mappings AS earlier_mapping
                          ON earlier_mapping.mapping_id=earlier.strict_mapping_id
                         AND earlier_mapping.raybet_match_id=
                             earlier.raybet_match_id
                         AND earlier_mapping.map_number=earlier.map_number
                        LEFT JOIN strict_live_map_mapping_invalidations
                                  AS earlier_invalidation
                          ON earlier_invalidation.mapping_id=
                              earlier_mapping.mapping_id
                       WHERE earlier.raybet_match_id=curve.raybet_match_id
                         AND earlier.map_number=curve.map_number
                         AND earlier_invalidation.mapping_id IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM vision_draft_conflicts AS conflict
                              WHERE conflict.raybet_match_id=
                                  earlier.raybet_match_id
                                AND conflict.map_number=earlier.map_number
                         )
                         AND (
                             julianday(earlier.first_usable_at)<
                                 julianday(curve.first_usable_at)
                             OR (
                                 julianday(earlier.first_usable_at)=
                                     julianday(curve.first_usable_at)
                                 AND earlier.curve_key<curve.curve_key
                             )
                         )
                  )""",
            (
                horizon,
                calibration.model_hash,
                curve_key,
                horizon,
                calibration.model_hash,
            ),
        ).fetchone()
        if row is None:
            return False
        feature_snapshot = _curve_feature_snapshot_by_key(connection, curve_key)
        if feature_snapshot is None:
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
            radiant_win, evidence_hash = prospective_outcome_authority(
                curve_key=curve_key,
                dota_match_id=int(row[18]),
                winner_side=str(row[15]),
                radiant_team_side=str(row[14]),
                map_result_ref=str(row[19]),
                reconciliation_observed_at=str(row[21]),
                evidence_rows=(
                    tuple(str(row[index]) for index in range(23, 29)),
                    tuple(str(row[index]) for index in range(29, 35)),
                ),
            )
            outcome_first_usable = _utc_timestamp(row[3])
            outcome_settled = _utc_timestamp(row[13])
            result_settled = _utc_timestamp(row[20])
            expected_first_usable = max(
                result_settled,
                _utc_timestamp(row[21]),
                _utc_timestamp(row[22]),
                _utc_timestamp(row[28]),
                _utc_timestamp(row[34]),
            )
        except (TypeError, ValueError):
            return False
        if (
            int(row[1]) != radiant_win
            or str(row[16]) != str(row[19])
            or str(row[17]) != evidence_hash
            or outcome_settled != result_settled
            or outcome_first_usable != expected_first_usable
        ):
            return False
        try:
            feature_values, _coverage = _feature_values_from_snapshot(
                feature_snapshot,
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
            radiant_win,
            _utc_timestamp(row[2]),
            outcome_first_usable,
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


def _draft_authority_generation(
    connection: sqlite3.Connection,
) -> tuple[int, int, int]:
    data_version_row = connection.execute("PRAGMA data_version").fetchone()
    authority_row = connection.execute(
        """SELECT authority_revision FROM draft_authority_revisions
            WHERE singleton=1"""
    ).fetchone()
    dependency_row = connection.execute(
        """SELECT dependency_revision FROM draft_lineage_revisions
            WHERE singleton=1"""
    ).fetchone()
    if data_version_row is None or authority_row is None or dependency_row is None:
        raise ValueError("draft authority revision is unavailable")
    return int(data_version_row[0]), int(authority_row[0]), int(dependency_row[0])


def _draft_deployment_generation(
    connection: sqlite3.Connection,
    deployment_key: str,
) -> tuple[int, tuple[int, ...]]:
    row = connection.execute(
        """SELECT deployment.artifact_revision,
                  lineage.dependency_revision,
                  bundle.dependency_revision,
                  bundle.training_cutoff
             FROM draft_deployment_revisions AS deployment
             JOIN draft_lineage_revisions AS lineage
               ON lineage.singleton=deployment.singleton
             JOIN draft_deployment_bundles AS bundle
               ON bundle.deployment_key=?
            WHERE deployment.singleton=1""",
        (deployment_key,),
    ).fetchone()
    if row is None or any(
        type(value) is not int or value < 1 for value in row[:3]
    ):
        raise ValueError("draft deployment revision is unavailable")
    artifact_revision = int(row[0])
    current_revision = int(row[1])
    stored_revision = int(row[2])
    cutoff = _utc_timestamp(row[3])
    if current_revision < stored_revision:
        return artifact_revision, (-1, current_revision, stored_revision)
    if current_revision == stored_revision:
        return artifact_revision, (1, stored_revision)
    changes = connection.execute(
        """SELECT COUNT(*), MIN(dependency_revision), MAX(dependency_revision),
                  MAX(CASE
                        WHEN affected_from_unix IS NULL
                          OR affected_from_unix<=?
                        THEN dependency_revision
                      END)
             FROM draft_lineage_changes
            WHERE dependency_revision>? AND dependency_revision<=?""",
        (int(cutoff.timestamp()), stored_revision, current_revision),
    ).fetchone()
    if changes is None:
        return artifact_revision, (-2, current_revision, 0, 0, 0)
    count = int(changes[0])
    minimum = 0 if changes[1] is None else int(changes[1])
    maximum = 0 if changes[2] is None else int(changes[2])
    relevant = stored_revision if changes[3] is None else int(changes[3])
    if (
        count != current_revision - stored_revision
        or minimum != stored_revision + 1
        or maximum != current_revision
    ):
        return artifact_revision, (
            -2,
            current_revision,
            count,
            minimum,
            maximum,
            relevant,
        )
    return artifact_revision, (1, relevant)


@lru_cache(maxsize=64)
def _cached_live_frozen_deployment(
    connection: sqlite3.Connection,
    deployment_key: str,
    artifact_revision: int,
    dependency_generation: tuple[int, ...],
) -> FrozenDraftDeployment:
    if artifact_revision < 1 or not dependency_generation:
        raise ValueError("draft deployment generation is invalid")
    deployment = load_frozen_deployment(
        connection,
        deployment_key=deployment_key,
    )
    if deployment is None:
        raise ValueError("curve references a missing deployment")
    return deployment


def _live_frozen_deployment(
    connection: sqlite3.Connection,
    deployment_key: str,
) -> FrozenDraftDeployment:
    before = _draft_deployment_generation(connection, deployment_key)
    if connection.in_transaction:
        deployment = load_frozen_deployment(
            connection,
            deployment_key=deployment_key,
        )
        if deployment is None:
            raise ValueError("curve references a missing deployment")
    else:
        deployment = _cached_live_frozen_deployment(
            connection,
            deployment_key,
            before[0],
            before[1],
        )
    if _draft_deployment_generation(connection, deployment_key) != before:
        raise ValueError("draft deployment authority changed during replay")
    return deployment


@lru_cache(maxsize=256)
def _cached_prospective_calibration_evidence_matches(
    connection: sqlite3.Connection,
    calibration_hash: str,
    calibration: DraftCalibrationArtifact,
    authority_revision: int,
    dependency_revision: int,
) -> bool:
    if calibration.calibration_hash != calibration_hash:
        return False
    return _verify_prospective_calibration_evidence(connection, calibration)


def _prospective_calibration_evidence_matches(
    connection: sqlite3.Connection,
    calibration: DraftCalibrationArtifact,
) -> bool:
    if calibration.evidence_mode != "prospective":
        return False
    try:
        before = _draft_authority_generation(connection)
        result = _cached_prospective_calibration_evidence_matches(
            connection,
            calibration.calibration_hash,
            calibration,
            before[1],
            before[2],
        )
        after = _draft_authority_generation(connection)
    except (sqlite3.Error, TypeError, ValueError):
        return False
    # data_version may advance for unrelated odds or health writes. Relevant
    # mutations atomically advance one of the two targeted revisions instead.
    if after[1:] != before[1:]:
        return False
    return result


def _draft_point_from_row(
    connection: sqlite3.Connection,
    row: object,
    *,
    deployment: FrozenDraftDeployment,
    curve_key: str,
    deployment_key: str,
    target_snapshot_hash: str,
    feature_values: dict[str, float | None],
    pure_coverage: float,
    prediction_cutoff: datetime,
    curve_created_at: datetime,
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
                  feature_schema_hash, training_input_hash, artifact_json,
                  created_at
             FROM draft_model_artifacts WHERE model_hash=?""",
        (str(row[12]),),
    ).fetchone()
    calibration_row = connection.execute(
        """SELECT model_hash, calibration_version, horizon_minutes,
                  evidence_mode, support, artifact_json, created_at
             FROM draft_calibration_artifacts WHERE calibration_hash=?""",
        (str(row[13]),),
    ).fetchone()
    deployment_row = connection.execute(
        """SELECT model_hashes_json, calibration_hashes_json,
                  training_cutoff, dependency_fingerprint,
                  dependency_revision, evidence_mode, created_at
             FROM draft_deployment_bundles WHERE deployment_key=?""",
        (deployment_key,),
    ).fetchone()
    if model_row is None or calibration_row is None or deployment_row is None:
        raise ValueError("landmark authority artifact is missing")
    horizon = int(row[0])
    model = deployment.model(horizon)
    calibration = deployment.calibration(horizon)
    assert_model_calibration_compatible(model, calibration)
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
    model_created_at = _utc_timestamp(model_row[6])
    calibration_created_at = _utc_timestamp(calibration_row[6])
    deployment_cutoff = _utc_timestamp(deployment_row[2])
    deployment_fingerprint = str(deployment_row[3])
    deployment_revision = int(deployment_row[4])
    deployment_evidence_mode = str(deployment_row[5])
    deployment_created_at = _utc_timestamp(deployment_row[6])
    landmark_created_at = _utc_timestamp(row[20])
    samples = (*calibration.fit_samples, *calibration.evaluation_samples)
    expected_keys = {str(value) for value in CHECKPOINTS}
    deployment_identity = {
        "deployment_version": DEPLOYMENT_VERSION,
        "training_cutoff": deployment_cutoff.isoformat(),
        "dependency_fingerprint": deployment_fingerprint,
        "dependency_revision": deployment_revision,
        "model_hashes": model_hashes,
        "calibration_hashes": calibration_hashes,
        "evidence_mode": deployment_evidence_mode,
    }
    if (
        not isinstance(model_hashes, dict)
        or not isinstance(calibration_hashes, dict)
        or deployment.deployment_key != deployment_key
        or deployment.training_cutoff != deployment_cutoff
        or deployment.dependency_fingerprint != deployment_fingerprint
        or deployment.dependency_revision != deployment_revision
        or deployment.evidence_mode != deployment_evidence_mode
        or set(model_hashes) != expected_keys
        or set(calibration_hashes) != expected_keys
        or canonical_hash(deployment_identity) != deployment_key
        or model_hashes.get(str(horizon)) != model.model_hash
        or calibration_hashes.get(str(horizon)) != calibration.calibration_hash
        or model.model_hash != str(row[12])
        or calibration.calibration_hash != str(row[13])
        or model.model_version != str(row[16])
        or model.model_kind != str(row[17])
        or model.feature_schema_hash != str(row[11])
        or horizon != model.horizon_minutes
        or str(row[18]) != "prospective"
        or deployment_evidence_mode != calibration.evidence_mode
        or deployment_cutoff != model.training_cutoff
        or model.training_cutoff > model_created_at
        or model_created_at > calibration_created_at
        or calibration_created_at > deployment_created_at
        or deployment_created_at > prediction_cutoff
        or prediction_cutoff > curve_created_at
        or curve_created_at > landmark_created_at
        or (samples and max(sample.settled_at for sample in samples) > calibration_created_at)
        or (
            calibration.evidence_mode == "prospective"
            and any(sample.observed_at < model_created_at for sample in samples)
        )
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
        landmark_key=str(row[25]),
        curve_key=curve_key,
        deployment_key=deployment_key,
        target_snapshot_hash=target_snapshot_hash,
    )


def _unavailable(as_of_start_time: int, reason: str) -> DraftCurve:
    return DraftCurve(
        (),
        source_ref=f"live-draft-unavailable:{as_of_start_time}",
        unavailable_reason=reason,
    )
