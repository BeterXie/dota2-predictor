"""Rate-limited capture of real vision failures for corpus building."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


class VisionDebugSink:
    def __init__(
        self,
        root: str | Path,
        *,
        minimum_interval: float = 30.0,
        maximum_events: int = 500,
    ) -> None:
        self.root = Path(root)
        self.minimum_interval = minimum_interval
        self.maximum_events = maximum_events
        self._last_written: dict[str, float] = {}
        self._event_count = self._existing_event_count()

    def _existing_event_count(self) -> int:
        if not self.root.exists():
            return 0
        return sum(1 for _ in self.root.rglob("metadata.json"))

    @staticmethod
    def _serializable(value: object) -> object:
        if is_dataclass(value):
            return {
                key: VisionDebugSink._serializable(item)
                for key, item in asdict(value).items()
            }
        if isinstance(value, tuple):
            return [VisionDebugSink._serializable(item) for item in value]
        if isinstance(value, list):
            return [VisionDebugSink._serializable(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): VisionDebugSink._serializable(item)
                for key, item in value.items()
            }
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def record(
        self,
        image: np.ndarray,
        *,
        reason: str,
        layout_name: str | None,
        diagnostics: object,
        hero_regions: Iterable[object] = (),
    ) -> bool:
        now = time.time()
        key = f"{layout_name or 'unknown'}:{reason}"
        previous = self._last_written.get(key)
        if previous is not None and now - previous < self.minimum_interval:
            return False
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.size == 0:
            return False
        self._event_count = max(self._event_count, self._existing_event_count())
        if self._event_count >= self.maximum_events:
            return False

        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
        event_dir = self.root / (layout_name or "unknown") / f"{stamp}_{reason}"
        suffix = 0
        while event_dir.exists():
            suffix += 1
            event_dir = self.root / (layout_name or "unknown") / f"{stamp}_{reason}_{suffix}"
        event_dir.mkdir(parents=True, exist_ok=False)

        if not cv2.imwrite(str(event_dir / "frame.jpg"), image):
            return False

        crop_paths: list[str] = []
        for index, region in enumerate(hero_regions):
            try:
                crop = region.crop(image)
            except (AttributeError, ValueError):
                continue
            if crop.size == 0:
                continue
            path = event_dir / f"hero_slot_{index + 1:02d}.jpg"
            if cv2.imwrite(str(path), crop):
                crop_paths.append(path.name)

        metadata = {
            "captured_at": now,
            "reason": reason,
            "layout": layout_name,
            "hero_crops": crop_paths,
            "diagnostics": self._serializable(diagnostics),
        }
        (event_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._last_written[key] = now
        self._event_count += 1
        return True
