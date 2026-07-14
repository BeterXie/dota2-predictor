"""Append-only JSONL persistence for visual observations."""

from __future__ import annotations

import os
from pathlib import Path

from contracts.live_observation import LiveObservation


class ObservationWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, observation: LiveObservation) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = observation.model_dump_json() + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
