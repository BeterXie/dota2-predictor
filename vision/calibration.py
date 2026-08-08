"""Filesystem-backed operator workflow for real-frame Vision calibration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from vision.hero_recognizer import DEFAULT_FEATURE_PATH
from vision.image_features import color_histogram, compute_phash
from vision.stable_runtime import _LAYOUTS


@dataclass(frozen=True)
class CalibrationPaths:
    debug_root: Path
    calibration_root: Path
    observation_root: Path


class VisionCalibrationService:
    """Expose bounded corpus labeling and candidate evaluation without promotion."""

    def __init__(
        self,
        project_root: Path,
        *,
        feature_path: Path = DEFAULT_FEATURE_PATH,
        observation_root: Path | None = None,
    ) -> None:
        data_root = project_root.resolve() / "data" / "live_betting"
        configured_observation_root = os.environ.get("VISION_OBSERVATION_DIR", "").strip()
        resolved_observation_root = (
            observation_root
            if observation_root is not None
            else Path(configured_observation_root)
            if configured_observation_root
            else data_root / "vision_observations"
        ).expanduser().resolve()
        self.paths = CalibrationPaths(
            debug_root=data_root / "vision_debug",
            calibration_root=data_root / "vision_calibration",
            observation_root=resolved_observation_root,
        )
        self.feature_path = feature_path.resolve()

    def bootstrap(self, *, limit: int = 100) -> dict[str, object]:
        events = self._events(limit=limit)
        candidates = self._candidates()
        return {
            "events": events,
            "profiles": self._profiles(events, candidates),
            "candidates": candidates,
            "evaluations": self._evaluations(),
            "observation_files": self._observation_files(),
            "observation_root": str(self.paths.observation_root),
            "layout_profiles": sorted(_LAYOUTS),
            "production_feature_path": str(self.feature_path),
            "candidate_boundary": (
                "候选包仅作为隔离的校正产物保存，绝不会自动覆盖生产特征包。"
            ),
        }

    def save_label(
        self,
        event_id: str,
        *,
        hero_ids: tuple[int, ...],
        raybet_match_id: str | None,
        map_number: int | None,
        note: str | None,
    ) -> dict[str, object]:
        if (
            len(hero_ids) != 10
            or len(set(hero_ids)) != 10
            or any(hero_id <= 0 for hero_id in hero_ids)
        ):
            raise ValueError("calibration truth requires ten unique HUD-order heroes")
        event = self._event(event_id)
        now = datetime.now(timezone.utc).isoformat()
        label = {
            "label_id": event_id,
            "event_id": event_id,
            "event_relative_path": event["relative_path"],
            "layout": event["layout"],
            "profile_id": event["profile_id"],
            "hero_ids": list(hero_ids),
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "note": note,
            "updated_at": now,
        }
        path = self._label_path(event_id)
        self._write_json(path, label)
        return label

    def build_candidate(self, label_id: str) -> dict[str, object]:
        label = self._label(label_id)
        event = self._event(str(label["event_id"]))
        if label["layout"] not in _LAYOUTS:
            raise ValueError("candidate building requires a supported layout")
        crop_paths = [Path(str(path)) for path in event["crop_paths"]]
        if len(crop_paths) != 10:
            raise ValueError("candidate building requires ten retained hero crops")
        hero_ids = tuple(int(value) for value in label["hero_ids"])

        with np.load(str(self.feature_path)) as source:
            ids = np.asarray(source["ids"], dtype=np.int32).copy()
            names = (
                np.asarray(source["variant_names"]).astype(str).copy()
                if "variant_names" in source.files
                else np.asarray([str(int(hero_id)) for hero_id in ids])
            )
            hashes = np.asarray(source["hashes"], dtype=np.uint8).copy()
            histograms = np.asarray(source["histograms"], dtype=np.float32).copy()
            thumbnails = np.asarray(source["thumbnails"], dtype=np.uint8).copy()

        for hero_id, crop_path in zip(hero_ids, crop_paths, strict=True):
            base_rows = np.flatnonzero((ids == hero_id) & (names == str(hero_id)))
            if len(base_rows) != 1:
                raise ValueError(f"production feature pack has no unique base hero {hero_id}")
            crop = cv2.imread(str(crop_path))
            if crop is None or crop.size == 0:
                raise ValueError(f"unreadable calibration crop: {crop_path.name}")
            row = int(base_rows[0])
            hashes[row] = compute_phash(crop, hash_size=8)
            histograms[row] = color_histogram(crop)
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            thumbnails[row] = cv2.resize(
                gray,
                (48, 32),
                interpolation=cv2.INTER_AREA,
            )

        candidate_id = f"candidate-{label_id}-{uuid4().hex[:8]}"
        candidate_root = self.paths.calibration_root / "candidates"
        feature_path = candidate_root / f"{candidate_id}.npz"
        feature_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = feature_path.with_suffix(".npz.part")
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                ids=ids,
                variant_names=names,
                hashes=hashes,
                histograms=histograms,
                thumbnails=thumbnails,
            )
        temporary.replace(feature_path)

        candidate = {
            "candidate_id": candidate_id,
            "label_id": label_id,
            "layout": label["layout"],
            "profile_id": self._profile_id(label.get("layout")),
            "hero_ids": list(hero_ids),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "feature_sha256": self._sha256(feature_path),
            "production_feature_sha256": self._sha256(self.feature_path),
            "promoted": False,
        }
        self._write_json(candidate_root / f"{candidate_id}.json", candidate)
        return candidate

    def run_evaluation(
        self,
        *,
        label_id: str,
        candidate_id: str,
        observation_file: str,
        layout_profile: str,
        mode: str,
        captured_after: datetime | None = None,
        captured_before: datetime | None = None,
    ) -> dict[str, object]:
        if layout_profile not in _LAYOUTS:
            raise ValueError("unsupported layout profile")
        if mode not in {"perception", "runtime"}:
            raise ValueError("evaluation mode must be perception or runtime")
        label = self._label(label_id)
        candidate = self._candidate(candidate_id)
        label_profile = self._profile_id(label.get("layout"))
        candidate_profile = self._profile_id(
            candidate.get("profile_id") or candidate.get("layout")
        )
        if candidate_profile != label_profile:
            raise ValueError("candidate does not belong to the selected UI profile")
        observation_path = self._observation_path(observation_file)
        feature_path = (
            self.paths.calibration_root / "candidates" / f"{candidate_id}.npz"
        )
        if not feature_path.is_file():
            raise ValueError("candidate feature artifact is missing")

        from scripts.evaluate_hero_recognition import (  # noqa: PLC0415
            _observation_samples,
            evaluate,
        )

        after = self._timestamp(captured_after)
        before = self._timestamp(captured_before)
        samples, context = _observation_samples(
            observation_path,
            captured_after=after,
            captured_before=before,
        )
        report = evaluate(
            self.paths.observation_root,
            feature_path,
            samples=samples,
            truth_hero_ids=tuple(int(value) for value in label["hero_ids"]),
            truth_context={
                **context,
                "label_id": label_id,
                "candidate_id": candidate_id,
            },
            stable=True,
            layout_profile=layout_profile,
            runtime_gates=mode == "runtime",
        )
        truth = report.get("truth_evaluation")
        if not isinstance(truth, dict):
            raise RuntimeError("calibration evaluation did not produce truth metrics")
        evaluation_id = uuid4().hex
        summary = {
            "evaluation_id": evaluation_id,
            "label_id": label_id,
            "candidate_id": candidate_id,
            "observation_file": observation_file,
            "layout_profile": layout_profile,
            "mode": mode,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "total_files": report["total_files"],
            "trackable_frames": report["trackable_frames"],
            "best_candidate_accuracy": truth["best_candidate_accuracy"],
            "accepted_precision": truth["accepted_precision"],
            "final_locked_slots": truth["final_locked_slots"],
            "final_correct_locked_slots": truth["final_correct_locked_slots"],
            "wrong_lock_count": truth["wrong_lock_count"],
            "lock_latency_seconds": truth["lock_latency_seconds"],
            "exact_post_lock_rate": truth["exact_post_lock_rate"],
            "candidate_feature_sha256": candidate["feature_sha256"],
        }
        report["calibration_summary"] = summary
        self._write_json(
            self.paths.calibration_root / "evaluations" / f"{evaluation_id}.json",
            report,
        )
        return summary

    def read_event_asset(self, event_id: str, asset_name: str) -> Path:
        event = self._event(event_id)
        allowed = {"frame.jpg", *(Path(str(path)).name for path in event["crop_paths"])}
        if asset_name not in allowed:
            raise ValueError("unknown calibration event asset")
        path = Path(str(event["event_path"])) / asset_name
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _events(self, *, limit: int) -> list[dict[str, object]]:
        if not self.paths.debug_root.exists():
            return []
        paths = sorted(
            self.paths.debug_root.rglob("metadata.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        events: list[dict[str, object]] = []
        for path in paths:
            try:
                events.append(self._event_from_path(path))
            except (OSError, ValueError):
                continue
            if len(events) >= limit:
                break
        return events

    def _event(self, event_id: str) -> dict[str, object]:
        for event in self._events(limit=500):
            if event["event_id"] == event_id:
                return event
        raise KeyError(event_id)

    def _event_from_path(self, metadata_path: Path) -> dict[str, object]:
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError(f"invalid Vision debug metadata: {metadata_path}") from error
        if not isinstance(payload, dict):
            raise ValueError(f"invalid Vision debug metadata: {metadata_path}")
        event_path = metadata_path.parent.resolve()
        relative = event_path.relative_to(self.paths.debug_root.resolve()).as_posix()
        event_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
        raw_crops = payload.get("hero_crops", [])
        raw_crops = raw_crops if isinstance(raw_crops, list) else []
        hero_crops: list[str] = []
        for value in raw_crops:
            if (
                isinstance(value, str)
                and re.fullmatch(r"hero_slot_[0-9]{2}\.jpg", value)
                and value not in hero_crops
                and (event_path / value).is_file()
            ):
                hero_crops.append(value)
        crop_paths = [str(event_path / name) for name in hero_crops]
        diagnostics = payload.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        hud = diagnostics.get("hud")
        hud = hud if isinstance(hud, dict) else {}
        quality = diagnostics.get("frame_quality")
        quality = quality if isinstance(quality, dict) else {}
        layout_tracker = diagnostics.get("layout_tracker")
        layout_tracker = layout_tracker if isinstance(layout_tracker, dict) else {}
        try:
            captured_at = float(payload.get("captured_at", metadata_path.stat().st_mtime))
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid Vision debug timestamp: {metadata_path}") from error
        if not math.isfinite(captured_at):
            raise ValueError(f"invalid Vision debug timestamp: {metadata_path}")
        try:
            captured_at_iso = datetime.fromtimestamp(captured_at, timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError) as error:
            raise ValueError(f"invalid Vision debug timestamp: {metadata_path}") from error
        slot_diagnostics = hud.get("draft_slots", [])
        slot_diagnostics = (
            [row for row in slot_diagnostics if isinstance(row, dict)]
            if isinstance(slot_diagnostics, list)
            else []
        )
        label = self._optional_json(self._label_path(event_id))
        return {
            "event_id": event_id,
            "relative_path": relative,
            "event_path": str(event_path),
            "captured_at": captured_at_iso,
            "layout": payload.get("layout"),
            "profile_id": self._profile_id(payload.get("layout")),
            "reason": str(payload.get("reason") or "unknown"),
            "blocker_code": hud.get("blocker_code"),
            "screen_state": hud.get("screen_state"),
            "replay_gate_status": hud.get("replay_gate_status"),
            "layout_state": layout_tracker.get("state"),
            "quality_reason": quality.get("reason"),
            "quality_usable": quality.get("usable"),
            "crop_count": len(crop_paths),
            "crop_paths": crop_paths,
            "frame_url": f"/api/vision-calibration/events/{event_id}/assets/frame.jpg",
            "crop_urls": [
                f"/api/vision-calibration/events/{event_id}/assets/{name}"
                for name in hero_crops
            ],
            "slot_diagnostics": slot_diagnostics,
            "label": label,
        }

    def _candidates(self) -> list[dict[str, object]]:
        root = self.paths.calibration_root / "candidates"
        if not root.exists():
            return []
        rows = [self._optional_json(path) for path in root.glob("*.json")]
        return sorted(
            (row for row in rows if row is not None),
            key=lambda row: str(row.get("created_at") or ""),
            reverse=True,
        )

    @classmethod
    def _profiles(
        cls,
        events: list[dict[str, object]],
        candidates: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        profiles: dict[str, dict[str, object]] = {}
        for event in events:
            profile_id = str(event["profile_id"])
            profile = profiles.setdefault(
                profile_id,
                {
                    "profile_id": profile_id,
                    "layout": event.get("layout"),
                    "event_count": 0,
                    "labeled_event_count": 0,
                    "candidate_count": 0,
                    "latest_captured_at": event.get("captured_at"),
                },
            )
            profile["event_count"] = int(profile["event_count"]) + 1
            if event.get("label") is not None:
                profile["labeled_event_count"] = int(profile["labeled_event_count"]) + 1
            if str(event.get("captured_at") or "") > str(profile["latest_captured_at"] or ""):
                profile["latest_captured_at"] = event.get("captured_at")

        for candidate in candidates:
            profile_id = cls._profile_id(
                candidate.get("profile_id") or candidate.get("layout")
            )
            profile = profiles.setdefault(
                profile_id,
                {
                    "profile_id": profile_id,
                    "layout": candidate.get("layout"),
                    "event_count": 0,
                    "labeled_event_count": 0,
                    "candidate_count": 0,
                    "latest_captured_at": None,
                },
            )
            profile["candidate_count"] = int(profile["candidate_count"]) + 1

        return sorted(profiles.values(), key=lambda row: str(row["profile_id"]))

    @staticmethod
    def _profile_id(layout: object) -> str:
        value = str(layout or "unknown").strip()
        return value if value in _LAYOUTS else "unknown"

    def _candidate(self, candidate_id: str) -> dict[str, object]:
        if re.fullmatch(r"[a-z0-9-]{1,200}", candidate_id) is None:
            raise ValueError("invalid candidate identifier")
        path = self.paths.calibration_root / "candidates" / f"{candidate_id}.json"
        row = self._optional_json(path)
        if row is None or row.get("candidate_id") != candidate_id:
            raise KeyError(candidate_id)
        return row

    def _evaluations(self) -> list[dict[str, object]]:
        root = self.paths.calibration_root / "evaluations"
        if not root.exists():
            return []
        rows: list[dict[str, object]] = []
        for path in root.glob("*.json"):
            report = self._optional_json(path)
            summary = report.get("calibration_summary") if report else None
            if isinstance(summary, dict):
                rows.append(summary)
        return sorted(
            rows,
            key=lambda row: str(row.get("created_at") or ""),
            reverse=True,
        )[:50]

    def _observation_files(self) -> list[dict[str, object]]:
        if not self.paths.observation_root.exists():
            return []
        return [
            {"name": path.name, "bytes": path.stat().st_size}
            for path in sorted(self.paths.observation_root.glob("*.jsonl"))
        ]

    def _observation_path(self, name: str) -> Path:
        if Path(name).name != name or not name.endswith(".jsonl"):
            raise ValueError("invalid observation filename")
        path = self.paths.observation_root / name
        if not path.is_file():
            raise KeyError(name)
        return path

    def _label(self, label_id: str) -> dict[str, object]:
        row = self._optional_json(self._label_path(label_id))
        if row is None or row.get("label_id") != label_id:
            raise KeyError(label_id)
        return row

    def _label_path(self, event_id: str) -> Path:
        return self.paths.calibration_root / "labels" / f"{event_id}.json"

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _optional_json(path: Path) -> dict[str, object] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _timestamp(value: datetime | None) -> float | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluation timestamps must include a timezone")
        return value.timestamp()


__all__ = ["CalibrationPaths", "VisionCalibrationService"]
