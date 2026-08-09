"""Filesystem-backed operator workflow for real-frame Vision calibration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from vision.hero_recognizer import DEFAULT_FEATURE_PATH
from vision.image_features import color_histogram, compute_phash
from vision.profile_features import promoted_profile_feature_path
from vision.stable_runtime import _LAYOUTS


@dataclass(frozen=True)
class CalibrationPaths:
    debug_root: Path
    calibration_root: Path
    observation_root: Path
    evidence_root: Path


class VisionCalibrationService:
    """Expose bounded labeling, evaluation, and explicit profile promotion."""

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
            evidence_root=data_root / "vision_evidence",
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
            "match_summaries": self._match_summaries(),
            "observation_root": str(self.paths.observation_root),
            "layout_profiles": sorted(_LAYOUTS),
            "production_feature_path": str(self.feature_path),
            "candidate_boundary": (
                "候选包保持隔离；只有满足双留出评估门槛后，才可显式推广到对应 UI Profile。"
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
        normalized_match_id = str(raybet_match_id or "").strip()
        if not normalized_match_id or type(map_number) is not int or map_number <= 0:
            raise ValueError(
                "calibration label requires a RayBet match ID and map number"
            )
        event = self._event(event_id)
        now = datetime.now(timezone.utc).isoformat()
        label = {
            "label_id": event_id,
            "event_id": event_id,
            "event_relative_path": event["relative_path"],
            "layout": event["layout"],
            "profile_id": event["profile_id"],
            "hero_ids": list(hero_ids),
            "raybet_match_id": normalized_match_id,
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

    def promote_candidate(
        self,
        candidate_id: str,
        *,
        evaluation_ids: tuple[str, ...],
    ) -> dict[str, object]:
        unique_ids = tuple(dict.fromkeys(evaluation_ids))
        if len(unique_ids) < 2:
            raise ValueError("promotion requires two held-out perception evaluations")
        candidate = self._candidate(candidate_id)
        profile_id = self._profile_id(
            candidate.get("profile_id") or candidate.get("layout")
        )
        if profile_id == "unknown":
            raise ValueError("candidate profile is unsupported")
        evaluations = [self._evaluation(evaluation_id) for evaluation_id in unique_ids]
        for evaluation in evaluations:
            if evaluation.get("candidate_id") != candidate_id:
                raise ValueError("promotion evaluation belongs to another candidate")
            if evaluation.get("mode") != "perception":
                raise ValueError("promotion evidence must use perception mode")
            if int(evaluation.get("total_files") or 0) < 20:
                raise ValueError("promotion evaluation requires at least 20 frames")
            if (
                int(evaluation.get("final_locked_slots") or 0) != 10
                or int(evaluation.get("final_correct_locked_slots") or 0) != 10
                or int(evaluation.get("wrong_lock_count") or 0) != 0
                or float(evaluation.get("accepted_precision") or 0.0) < 0.99
                or float(evaluation.get("exact_post_lock_rate") or 0.0) < 0.99
            ):
                raise ValueError("promotion evaluation did not meet the safety gate")

        candidate_root = self.paths.calibration_root / "candidates"
        source = candidate_root / f"{candidate_id}.npz"
        expected_hash = str(candidate.get("feature_sha256") or "")
        if not source.is_file() or self._sha256(source) != expected_hash:
            raise ValueError("candidate feature artifact failed integrity verification")

        promoted_root = self.paths.calibration_root / "promoted"
        promoted_root.mkdir(parents=True, exist_ok=True)
        feature_path = promoted_root / f"{profile_id}.npz"
        temporary = feature_path.with_suffix(".npz.part")
        shutil.copyfile(source, temporary)
        temporary.replace(feature_path)
        manifest = {
            "profile_id": profile_id,
            "candidate_id": candidate_id,
            "feature_sha256": expected_hash,
            "evaluation_ids": list(unique_ids),
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "promoted": True,
        }
        self._write_json(promoted_root / f"{profile_id}.json", manifest)
        if (
            promoted_profile_feature_path(
                profile_id,
                calibration_root=self.paths.calibration_root,
            )
            != feature_path
        ):
            raise RuntimeError("promoted profile feature artifact is unavailable")
        return manifest

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
        if layout_profile != label_profile:
            raise ValueError("evaluation layout must match the labeled UI profile")
        raybet_match_id = str(label.get("raybet_match_id") or "").strip()
        map_number = label.get("map_number")
        if not raybet_match_id or type(map_number) is not int or map_number <= 0:
            raise ValueError(
                "calibration label requires a RayBet match ID and map number"
            )
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
            map_number=map_number,
            captured_after=after,
            captured_before=before,
        )
        if context.get("raybet_match_id") != raybet_match_id:
            raise ValueError(
                "observation JSONL belongs to a different RayBet match"
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
                "raybet_match_id": raybet_match_id,
                "map_number": map_number,
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
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
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
        selected_paths: set[Path] = set()
        for path in paths:
            try:
                event = self._event_from_path(path)
            except (OSError, ValueError):
                continue
            events.append(event)
            selected_paths.add(path.resolve())
            if len(events) >= limit:
                break

        label_root = self.paths.calibration_root / "labels"
        if label_root.exists():
            debug_root = self.paths.debug_root.resolve()
            for label_path in sorted(label_root.glob("*.json")):
                label = self._optional_json(label_path)
                relative = label.get("event_relative_path") if label else None
                if not isinstance(relative, str) or not relative.strip():
                    continue
                metadata_path = (debug_root / relative / "metadata.json").resolve()
                try:
                    metadata_path.relative_to(debug_root)
                except ValueError:
                    continue
                if metadata_path in selected_paths or not metadata_path.is_file():
                    continue
                try:
                    event = self._event_from_path(metadata_path)
                except (OSError, ValueError):
                    continue
                if event.get("label") is None:
                    continue
                events.append(event)
                selected_paths.add(metadata_path)
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

    def _evaluation(self, evaluation_id: str) -> dict[str, object]:
        if re.fullmatch(r"[a-f0-9]{32}", evaluation_id) is None:
            raise ValueError("invalid evaluation identifier")
        path = self.paths.calibration_root / "evaluations" / f"{evaluation_id}.json"
        report = self._optional_json(path)
        summary = report.get("calibration_summary") if report else None
        if not isinstance(summary, dict) or summary.get("evaluation_id") != evaluation_id:
            raise KeyError(evaluation_id)
        return summary

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

    def _match_summaries(self) -> list[dict[str, object]]:
        """Build a read-only, one-row-per-game view of the Vision corpus.

        Observation JSONL remains the source of truth and is intentionally not
        changed.  The summary combines it with the optional capture heartbeat
        and evidence manifest sidecars so operators can understand a whole
        match without opening hundreds of individual frames.
        """

        if not self.paths.observation_root.exists():
            return []

        summaries: dict[str, dict[str, object]] = {}

        def ensure(match_id: str, observation_file: str | None = None) -> dict[str, object]:
            normalized = str(match_id or "").strip() or "unknown"
            return summaries.setdefault(
                normalized,
                {
                    "match_id": normalized,
                    "observation_file": observation_file,
                    "observation_count": 0,
                    "evidence_frame_count": 0,
                    "manifest_event_count": 0,
                    "periodic_count": 0,
                    "draft_started": False,
                    "game_started": False,
                    "ended_final": False,
                    "maps": set(),
                    "_evidence_refs": set(),
                    "_first_dt": None,
                    "_last_dt": None,
                    "_latest_screen_state": None,
                    "_latest_phase": None,
                    "_heartbeat_phase": None,
                    "_heartbeat_at": None,
                    "_layout_profile": None,
                    "_capture_status": None,
                },
            )

        def parsed_timestamp(value: object) -> datetime | None:
            if not isinstance(value, str) or not value.strip():
                return None
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            return parsed.astimezone(timezone.utc)

        def touch_time(summary: dict[str, object], value: object) -> datetime | None:
            parsed = parsed_timestamp(value)
            if parsed is None:
                return None
            first = summary.get("_first_dt")
            last = summary.get("_last_dt")
            if not isinstance(first, datetime) or parsed < first:
                summary["_first_dt"] = parsed
            if not isinstance(last, datetime) or parsed >= last:
                summary["_last_dt"] = parsed
                screen_state = summary.get("_pending_screen_state")
                phase = summary.get("_pending_phase")
                if screen_state is not None:
                    summary["_latest_screen_state"] = screen_state
                if phase is not None:
                    summary["_latest_phase"] = phase
                summary.pop("_pending_screen_state", None)
                summary.pop("_pending_phase", None)
            return parsed

        for path in sorted(self.paths.observation_root.glob("*.jsonl")):
            rows: list[dict[str, object]] = []
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            payload = json.loads(line)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if isinstance(payload, dict):
                            rows.append(payload)
            except OSError:
                continue
            match_id = next(
                (
                    str(row.get("raybet_match_id")).strip()
                    for row in rows
                    if str(row.get("raybet_match_id") or "").strip()
                ),
                path.stem,
            )
            summary = ensure(match_id, path.name)
            for row in rows:
                summary["observation_count"] = int(summary["observation_count"]) + 1
                captured_at = row.get("captured_at_utc") or row.get("captured_at")
                source_ref = str(row.get("source_frame_ref") or "").strip()
                if source_ref and (
                    source_ref.startswith("vision-frame:")
                    or row.get("source_frame_sha256")
                ):
                    refs = summary["_evidence_refs"]
                    if isinstance(refs, set):
                        refs.add(source_ref)
                map_number = row.get("map_number")
                if type(map_number) is int and map_number > 0:
                    maps = summary["maps"]
                    if isinstance(maps, set):
                        maps.add(map_number)
                summary["_pending_screen_state"] = row.get("screen_state")
                touch_time(summary, captured_at)

        for path in sorted(self.paths.evidence_root.glob("*.manifest.jsonl")):
            match_id = path.name[: -len(".manifest.jsonl")]
            summary = ensure(match_id, f"{match_id}.jsonl")
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            payload = json.loads(line)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if not isinstance(payload, dict):
                            continue
                        summary["manifest_event_count"] = int(summary["manifest_event_count"]) + 1
                        event = str(payload.get("event") or "").strip()
                        if event == "periodic_30s":
                            summary["periodic_count"] = int(summary["periodic_count"]) + 1
                        if event == "draft_started":
                            summary["draft_started"] = True
                        if event == "game_started":
                            summary["game_started"] = True
                        if event == "ended_final":
                            summary["ended_final"] = True
                        frame_ref = str(payload.get("frame_ref") or "").strip()
                        if frame_ref:
                            refs = summary["_evidence_refs"]
                            if isinstance(refs, set):
                                refs.add(frame_ref)
                        map_number = payload.get("map_number")
                        if type(map_number) is int and map_number > 0:
                            maps = summary["maps"]
                            if isinstance(maps, set):
                                maps.add(map_number)
                        summary["_pending_screen_state"] = payload.get("screen_state")
                        summary["_pending_phase"] = payload.get("phase")
                        touch_time(summary, payload.get("captured_at"))
            except OSError:
                continue

        for path in sorted(self.paths.observation_root.glob("*.heartbeat.json")):
            match_id = path.name[: -len(".heartbeat.json")]
            summary = ensure(match_id, f"{match_id}.jsonl")
            payload = self._optional_json(path)
            if payload is None:
                continue
            phase = str(payload.get("capture_phase") or "").strip()
            if phase:
                summary["_heartbeat_phase"] = phase
            status = str(payload.get("capture_status") or "").strip()
            if status:
                summary["_capture_status"] = status
            layout = payload.get("layout")
            if isinstance(layout, dict):
                profile = str(layout.get("profile") or "").strip()
                if profile:
                    summary["_layout_profile"] = profile
            screen = payload.get("screen")
            if isinstance(screen, dict):
                summary["_pending_screen_state"] = screen.get("state")
            else:
                summary["_pending_screen_state"] = payload.get("screen_state")
            summary["_pending_phase"] = phase or None
            heartbeat_at = touch_time(summary, payload.get("captured_at"))
            if heartbeat_at is not None:
                summary["_heartbeat_at"] = heartbeat_at

        phase_labels = {
            "waiting_for_stream": "等待直播流",
            "draft_candidate": "BP 候选确认",
            "draft_started": "BP 已开始",
            "game_started": "比赛进行中",
            "ended_grace": "结束宽限期",
            "closed": "已结束",
        }
        status_for_phase = {
            "waiting_for_stream": "waiting",
            "draft_candidate": "preparing",
            "draft_started": "draft",
            "game_started": "live",
            "ended_grace": "ending",
            "closed": "ended",
        }
        result: list[dict[str, object]] = []
        now = datetime.now(timezone.utc)
        for summary in summaries.values():
            phase = str(
                summary.get("_heartbeat_phase")
                or summary.get("_latest_phase")
                or ("game_started" if summary.get("_latest_screen_state") == "game" else "waiting_for_stream")
            )
            if summary.get("ended_final"):
                phase = "closed"
            heartbeat_at = summary.get("_heartbeat_at")
            heartbeat_fresh = bool(
                isinstance(heartbeat_at, datetime)
                and -15.0 <= (now - heartbeat_at).total_seconds() <= 180.0
            )
            status = (
                "ended"
                if phase == "closed"
                else status_for_phase.get(phase, "unknown")
                if heartbeat_fresh
                else "archived"
            )
            status_label = (
                "已结束"
                if status == "ended"
                else phase_labels.get(phase, "状态未知")
                if heartbeat_fresh
                else "历史观测"
            )
            first_dt = summary.get("_first_dt")
            last_dt = summary.get("_last_dt")
            refs = summary.get("_evidence_refs")
            maps = summary.get("maps")
            result.append(
                {
                    "match_id": summary["match_id"],
                    "observation_file": summary.get("observation_file"),
                    "status": status,
                    "status_label": status_label,
                    "phase": phase,
                    "observation_count": summary["observation_count"],
                    "evidence_frame_count": len(refs) if isinstance(refs, set) else 0,
                    "manifest_event_count": summary["manifest_event_count"],
                    "periodic_count": summary["periodic_count"],
                    "draft_started": bool(summary["draft_started"]),
                    "game_started": bool(summary["game_started"]),
                    "ended_final": bool(summary["ended_final"]),
                    "first_captured_at": first_dt.isoformat() if isinstance(first_dt, datetime) else None,
                    "last_captured_at": last_dt.isoformat() if isinstance(last_dt, datetime) else None,
                    "latest_screen_state": summary.get("_latest_screen_state"),
                    "layout_profile": summary.get("_layout_profile"),
                    "maps": sorted(maps) if isinstance(maps, set) else [],
                    "capture_status": summary.get("_capture_status"),
                    "heartbeat_fresh": heartbeat_fresh,
                }
            )
        return sorted(
            result,
            key=lambda row: str(row.get("last_captured_at") or row.get("match_id") or ""),
            reverse=True,
        )

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
