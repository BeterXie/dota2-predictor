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
                      created_at
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

    curve_key = str(row[0])
    try:
        landmarks = connection.execute(
            """SELECT horizon_minutes, radiant_probability, scaling_edge,
                      synergy_edge, quality, validation_status, support,
                      calibration_ref, input_refs_json, uncertainty,
                      validation_reason, feature_hash, model_hash,
                      calibration_hash, global_calibration_passed,
                      global_gate_ref, model_version, model_kind,
                      availability_mode, input_snapshot_hash, created_at
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
        points = tuple(_draft_point_from_row(item, curve_key) for item in landmarks)
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


def _draft_point_from_row(row: object, curve_key: str) -> DraftPoint:
    refs = json.loads(str(row[8]))
    if not isinstance(refs, list) or not refs or any(
        not isinstance(ref, str) or not ref.strip() for ref in refs
    ):
        raise ValueError("prospective draft input refs must be non-empty strings")
    status = str(row[5])
    if status not in {"passed", "failed", "insufficient_evidence"}:
        raise ValueError("invalid prospective draft validation status")
    return DraftPoint(
        minute=int(row[0]),
        radiant_probability=float(row[1]),
        scaling_edge=float(row[2]),
        synergy_edge=float(row[3]),
        quality=float(row[4]),
        validated=status == "passed",
        support=int(row[6]),
        calibration_ref=str(row[7]),
        input_refs=(f"prospective-draft:{curve_key}", *tuple(refs)),
        uncertainty=None if row[9] is None else float(row[9]),
        validation_reason=None if row[10] is None else str(row[10]),
        feature_hash=None if row[11] is None else str(row[11]),
        model_hash=None if row[12] is None else str(row[12]),
        calibration_hash=None if row[13] is None else str(row[13]),
        global_calibration_passed=bool(row[14]),
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
