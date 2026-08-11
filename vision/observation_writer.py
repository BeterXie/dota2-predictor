"""Append-only JSONL persistence for visual observations."""

from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Callable
from pathlib import Path

from contracts.live_observation import LiveObservation
from live_betting.vision import VisionObservation, parse_observation


class ObservationWriter:
    def __init__(
        self,
        path: str | Path,
        *,
        sink: Callable[[VisionObservation], object] | None = None,
    ) -> None:
        self.path = Path(path)
        self.sink = sink

    def _persist(self, observation: LiveObservation) -> None:
        if self.sink is None:
            return
        self.sink(parse_observation(observation.model_dump(mode="json")))

    def append(self, observation: LiveObservation) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = observation.model_dump_json() + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._persist(observation)

    def replay_to_sink(self, *, limit: int = 2000) -> int:
        if limit < 1:
            raise ValueError("observation replay limit must be positive")
        if self.sink is None or not self.path.is_file():
            return 0
        with self.path.open(encoding="utf-8") as handle:
            lines = deque(handle, maxlen=limit)
        replayed = 0
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("observation row must be an object")
                self.sink(parse_observation(payload))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"invalid observation replay row {line_number}: {self.path}"
                ) from error
            replayed += 1
        return replayed
