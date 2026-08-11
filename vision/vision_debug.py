"""Rate-limited capture of real vision failures for corpus building."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
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
        if maximum_events < 1:
            raise ValueError("maximum_events must be positive")
        self._last_written: dict[str, float] = {}
        self._event_count = self._existing_event_count()

    def _existing_event_count(self) -> int:
        if not self.root.exists():
            return 0
        return sum(1 for _ in self.root.rglob("metadata.json"))

    def _protected_event_paths(self) -> set[str] | None:
        label_root = self.root.parent / "vision_calibration" / "labels"
        if not label_root.exists():
            return set()
        protected: set[str] = set()
        for path in label_root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return None
            relative = payload.get("event_relative_path") if isinstance(payload, dict) else None
            if not isinstance(relative, str) or not relative.strip():
                continue
            normalized = Path(relative.strip())
            if normalized.is_absolute() or ".." in normalized.parts:
                continue
            protected.add(normalized.as_posix())
        return protected

    def _make_room(self) -> bool:
        metadata_paths = list(self.root.rglob("metadata.json"))
        self._event_count = len(metadata_paths)
        if self._event_count < self.maximum_events:
            return True

        protected = self._protected_event_paths()
        if protected is None:
            return False
        oldest_first = sorted(
            metadata_paths,
            key=lambda path: (path.stat().st_mtime_ns, str(path)),
        )
        for metadata_path in oldest_first:
            relative = metadata_path.parent.relative_to(self.root).as_posix()
            if relative in protected:
                continue
            try:
                shutil.rmtree(metadata_path.parent)
            except OSError:
                continue
            self._event_count -= 1
            if self._event_count < self.maximum_events:
                return True
        return False

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
        raybet_match_id: str | None = None,
        map_number: int | None = None,
        captured_at_utc: datetime | None = None,
        source_frame_ref: str | None = None,
    ) -> bool:
        now = time.time()
        match_id = str(raybet_match_id or "").strip()
        has_identity = bool(match_id) and map_number is not None
        if bool(match_id) != (map_number is not None):
            raise ValueError("Vision debug identity requires both Series ID and Map number")
        if has_identity and (
            type(map_number) is not int
            or any(
                not character.isascii()
                or (not character.isalnum() and character not in {"-", "_"})
                for character in match_id
            )
            or not 1 <= int(map_number) <= 10
            or captured_at_utc is None
            or not isinstance(source_frame_ref, str)
            or not source_frame_ref.strip()
        ):
            raise ValueError("Vision debug identity is invalid")
        if captured_at_utc is not None and (
            captured_at_utc.tzinfo is None or captured_at_utc.utcoffset() is None
        ):
            raise ValueError("Vision debug capture time must be timezone-aware")
        captured_at = (
            captured_at_utc.astimezone(timezone.utc).timestamp()
            if captured_at_utc is not None
            else now
        )
        identity_key = f"{match_id}:map:{map_number}" if has_identity else "unassigned"
        key = f"{identity_key}:{layout_name or 'unknown'}:{reason}"
        previous = self._last_written.get(key)
        if previous is not None and now - previous < self.minimum_interval:
            return False
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.size == 0:
            return False
        if not self._make_room():
            return False

        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(captured_at))
        event_root = (
            self.root
            / "series"
            / match_id
            / f"map_{map_number}"
            / (layout_name or "unknown")
            if has_identity
            else self.root / (layout_name or "unknown")
        )
        event_dir = event_root / f"{stamp}_{reason}"
        suffix = 0
        while event_dir.exists():
            suffix += 1
            event_dir = event_root / f"{stamp}_{reason}_{suffix}"
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
            "captured_at": captured_at,
            "recorded_at": now,
            "reason": reason,
            "layout": layout_name,
            "raybet_match_id": match_id or None,
            "map_number": map_number if has_identity else None,
            "source_frame_ref": source_frame_ref if has_identity else None,
            "identity_status": "explicit_watcher_context" if has_identity else "missing",
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
