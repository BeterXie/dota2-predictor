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
        self._jump_recent: deque[int] = deque(maxlen=confirmations)
        self.frozen = False
        self.min_confidence = min_confidence
        self.regression_count = 0

    def reset_map(self, map_number: int) -> None:
        self.map_number = map_number
        self.last_seconds = None
        self.same_count = 0
        self._recent.clear()
        self._jump_recent.clear()
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
                self._jump_recent.append(seconds)
                if (
                    len(self._jump_recent) < self.confirmations
                    or max(self._jump_recent) - min(self._jump_recent) > 3
                ):
                    return None
                # OCR may be unavailable long enough for a legitimate jump.
                # Resume only after a fresh, internally consistent sequence;
                # callers must not refresh the old confirmation meanwhile.
                self._recent.clear()
                self._recent.extend(self._jump_recent)
                self._jump_recent.clear()
                self.last_seconds = seconds
                self.same_count = 0
                self.regression_count = 0
                return ConfirmedClock(
                    self.map_number,
                    seconds,
                    False,
                    min(0.99, 0.80 + reading.confidence * 0.20),
                )
            self._jump_recent.clear()
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
