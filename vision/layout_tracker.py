"""Temporal hysteresis for broadcast-layout selection.

The detector is intentionally allowed to be noisy.  This tracker owns the
product decision about when a layout becomes stable enough to use, when a
short miss should merely degrade confidence, and when a challenger is strong
enough to replace the active layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping


LayoutTrackerState = Literal[
    "acquiring",
    "locked",
    "degraded",
    "switching",
    "unsupported",
]


@dataclass(frozen=True)
class StableLayoutState:
    layout_name: str | None
    confidence: float
    state: LayoutTrackerState
    challenger_name: str | None = None
    challenger_confidence: float = 0.0
    consecutive_support: int = 0
    consecutive_misses: int = 0


class LayoutTracker:
    """Convert noisy per-frame scores into one sticky layout decision."""

    def __init__(
        self,
        *,
        acquire_threshold: float = 0.90,
        acquire_confirmations: int = 3,
        retain_threshold: float = 0.60,
        grace_frames: int = 8,
        switch_threshold: float = 0.92,
        switch_margin: float = 0.15,
        switch_confirmations: int = 5,
    ) -> None:
        if not 0.0 <= retain_threshold <= acquire_threshold <= 1.0:
            raise ValueError("layout thresholds must satisfy retain <= acquire <= 1")
        if not 0.0 <= switch_threshold <= 1.0:
            raise ValueError("switch_threshold must be between zero and one")
        if acquire_confirmations < 1 or switch_confirmations < 1:
            raise ValueError("layout confirmations must be positive")
        if grace_frames < 0:
            raise ValueError("grace_frames must not be negative")
        if switch_margin < 0.0:
            raise ValueError("switch_margin must not be negative")

        self.acquire_threshold = acquire_threshold
        self.acquire_confirmations = acquire_confirmations
        self.retain_threshold = retain_threshold
        self.grace_frames = grace_frames
        self.switch_threshold = switch_threshold
        self.switch_margin = switch_margin
        self.switch_confirmations = switch_confirmations

        self._active: str | None = None
        self._candidate: str | None = None
        self._candidate_count = 0
        self._challenger: str | None = None
        self._challenger_count = 0
        self._misses = 0

    def reset(self) -> None:
        self._active = None
        self._candidate = None
        self._candidate_count = 0
        self._challenger = None
        self._challenger_count = 0
        self._misses = 0

    @staticmethod
    def _winner(scores: Mapping[str, float]) -> tuple[str | None, float]:
        valid = [
            (name, float(score))
            for name, score in scores.items()
            if name and 0.0 <= float(score) <= 1.0
        ]
        return max(valid, key=lambda item: item[1]) if valid else (None, 0.0)

    def update(self, scores: Mapping[str, float]) -> StableLayoutState:
        winner_name, winner_score = self._winner(scores)

        if self._active is None:
            if winner_name is None or winner_score < self.acquire_threshold:
                self._candidate = None
                self._candidate_count = 0
                return StableLayoutState(None, winner_score, "unsupported")

            if winner_name == self._candidate:
                self._candidate_count += 1
            else:
                self._candidate = winner_name
                self._candidate_count = 1

            if self._candidate_count < self.acquire_confirmations:
                return StableLayoutState(
                    None,
                    winner_score,
                    "acquiring",
                    challenger_name=winner_name,
                    challenger_confidence=winner_score,
                    consecutive_support=self._candidate_count,
                )

            self._active = winner_name
            self._candidate = None
            self._candidate_count = 0
            self._misses = 0
            return StableLayoutState(
                self._active,
                winner_score,
                "locked",
                consecutive_support=self.acquire_confirmations,
            )

        active_score = float(scores.get(self._active, 0.0))
        challenger_name = winner_name if winner_name != self._active else None
        challenger_score = winner_score if challenger_name is not None else 0.0
        challenger_is_strong = (
            challenger_name is not None
            and challenger_score >= self.switch_threshold
            and challenger_score - active_score >= self.switch_margin
        )

        if challenger_is_strong:
            if challenger_name == self._challenger:
                self._challenger_count += 1
            else:
                self._challenger = challenger_name
                self._challenger_count = 1

            if self._challenger_count >= self.switch_confirmations:
                self._active = challenger_name
                self._challenger = None
                self._challenger_count = 0
                self._misses = 0
                return StableLayoutState(
                    self._active,
                    challenger_score,
                    "locked",
                    consecutive_support=self.switch_confirmations,
                )

            return StableLayoutState(
                self._active,
                active_score,
                "switching",
                challenger_name=challenger_name,
                challenger_confidence=challenger_score,
                consecutive_support=self._challenger_count,
                consecutive_misses=self._misses,
            )

        self._challenger = None
        self._challenger_count = 0

        if active_score >= self.retain_threshold:
            self._misses = 0
            return StableLayoutState(self._active, active_score, "locked")

        self._misses += 1
        if self._misses <= self.grace_frames:
            return StableLayoutState(
                self._active,
                active_score,
                "degraded",
                consecutive_misses=self._misses,
            )

        previous = self._active
        self._active = None
        self._candidate = None
        self._candidate_count = 0
        self._misses = 0
        return StableLayoutState(
            None,
            active_score,
            "unsupported",
            challenger_name=previous,
        )
