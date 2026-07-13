"""Validated, causal draft landmarks for the live shadow strategy.

Historical walk-forward predictions are evaluation evidence.  They are not a
live prediction for a new lineup, so this module deliberately fails closed
until a separately persisted live landmark is both predicted and calibrated.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass


CHECKPOINTS = (10, 20, 30, 40, 50)
MIN_LANDMARK_SUPPORT = 100
MAX_LANDMARK_AGE_MINUTES = 10.0


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

    @property
    def passes_live_gate(self) -> bool:
        return (
            self.validated
            and self.support >= MIN_LANDMARK_SUPPORT
            and bool(self.calibration_ref.strip())
            and bool(self.input_refs)
            and self.uncertainty is not None
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
        candidates = tuple(
            point
            for point in self.points
            if point.passes_live_gate and point.minute <= current_minute
        )
        if not candidates:
            return None
        selected = max(candidates, key=lambda point: point.minute)
        if current_minute - selected.minute > MAX_LANDMARK_AGE_MINUTES:
            return None
        return selected

    def wait_reason(self, game_clock_seconds: int) -> str | None:
        if self.at(game_clock_seconds) is not None:
            return None
        current_minute = game_clock_seconds / 60.0
        if current_minute < CHECKPOINTS[0]:
            return "before_first_draft_landmark"
        usable_past = tuple(
            point
            for point in self.points
            if point.passes_live_gate and point.minute <= current_minute
        )
        if not usable_past:
            return self.unavailable_reason or "no_validated_past_draft_landmark"
        return "validated_draft_landmark_stale"


def build_draft_curve(
    connection: sqlite3.Connection,
    radiant_heroes: tuple[int, ...],
    dire_heroes: tuple[int, ...],
    as_of_start_time: int,
) -> DraftCurve:
    """Fail closed until a calibrated live prediction artifact exists.

    ``draft_predictions`` contains settled walk-forward predictions for their
    historical target maps. Reusing one of those probabilities, or the old
    ad-hoc hero win-rate heuristic, would not be a prediction for this live
    draft. A future live predictor can construct ``DraftPoint`` rows with the
    immutable model/calibration/input references required above.
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
    # Retain the connection in the API because the eventual live predictor will
    # read immutable model artifacts through this boundary.
    del connection
    return DraftCurve(
        (),
        source_ref=f"live-draft-unavailable:{as_of_start_time}",
        unavailable_reason="validated_live_draft_prediction_missing",
    )
