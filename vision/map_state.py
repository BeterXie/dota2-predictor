"""Temporal confirmation for game clock, pause, and map resets."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .clock_reader import ClockReading


@dataclass(frozen=True)
class ConfirmedClock:
    map_number: int
    seconds: int
    is_paused: bool
    confidence: float


class MapStateTracker:
    def __init__(
        self,
        confirmations: int = 2,
        pause_frames: int = 3,
        min_confidence: float = 0.55,
    ) -> None:
        self.confirmations = confirmations
        self.pause_frames = pause_frames
        self.map_number = 1
        self.last_seconds: int | None = None
        self.same_count = 0
        self._recent: deque[int] = deque(maxlen=confirmations)
        self.frozen = False
        self.min_confidence = min_confidence
        self.regression_count = 0

    def reset_map(self, map_number: int) -> None:
        self.map_number = map_number
        self.last_seconds = None
        self.same_count = 0
        self._recent.clear()
        self.frozen = False
        self.regression_count = 0

    def update(self, reading: ClockReading) -> ConfirmedClock | None:
        if (
            self.frozen
            or reading.seconds is None
            or reading.confidence < self.min_confidence
        ):
            return None
        seconds = reading.seconds
        if self.last_seconds is not None:
            if seconds > self.last_seconds + 10:
                return None
            if seconds < self.last_seconds - 3:
                self.regression_count += 1
                if self.regression_count >= 2:
                    self.frozen = True
                return None
        self.regression_count = 0
        self._recent.append(seconds)
        if self.last_seconds == seconds:
            self.same_count += 1
        else:
            self.same_count = 0
        self.last_seconds = seconds
        if len(self._recent) < self.confirmations:
            return None
        if max(self._recent) - min(self._recent) > 3:
            return None
        return ConfirmedClock(
            self.map_number,
            seconds,
            self.same_count >= self.pause_frames,
            min(0.99, 0.80 + reading.confidence * 0.20),
        )
