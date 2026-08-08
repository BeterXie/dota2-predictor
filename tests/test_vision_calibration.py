from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

import scripts.evaluate_hero_recognition as evaluator
from scripts.evaluate_hero_recognition import EvidenceSample
from vision.calibration import VisionCalibrationService


def _feature_pack(path: Path) -> None:
    ids = np.arange(1, 11, dtype=np.int32)
    np.savez_compressed(
        path,
        ids=ids,
        hashes=np.zeros((10, 64), dtype=np.uint8),
        histograms=np.zeros((10, 384), dtype=np.float32),
        thumbnails=np.zeros((10, 32, 48), dtype=np.uint8),
    )


def _debug_event(project_root: Path) -> Path:
    event_root = (
        project_root
        / "data"
        / "live_betting"
        / "vision_debug"
        / "standard_dota_hud_1080p"
        / "20260808T120000_ambiguous_match"
    )
    event_root.mkdir(parents=True)
    frame = np.full((1080, 1920, 3), 80, dtype=np.uint8)
    assert cv2.imwrite(str(event_root / "frame.jpg"), frame)
    crops: list[str] = []
    for index in range(10):
        name = f"hero_slot_{index + 1:02d}.jpg"
        crop = np.full((48, 64, 3), 30 + index * 15, dtype=np.uint8)
        cv2.circle(crop, (12 + index, 20), 8, (240, 100, 30), -1)
        assert cv2.imwrite(str(event_root / name), crop)
        crops.append(name)
    (event_root / "metadata.json").write_text(
        json.dumps(
            {
                "captured_at": 1_785_600_000.0,
                "reason": "ambiguous_match",
                "layout": "standard_dota_hud_1080p",
                "hero_crops": crops,
                "diagnostics": {
                    "hud": {
                        "blocker_code": "draft_incomplete",
                        "screen_state": "game",
                        "replay_gate_status": "live",
                        "draft_slots": [],
                    },
                    "frame_quality": {"usable": True, "reason": None},
                    "layout_tracker": {"state": "locked"},
                },
            }
        ),
        encoding="utf-8",
    )
    return event_root


def test_calibration_labels_real_event_and_builds_isolated_candidate(
    tmp_path: Path,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)

    bootstrap = service.bootstrap()
    event = bootstrap["events"][0]
    assert event["crop_count"] == 10
    event_id = str(event["event_id"])
    label = service.save_label(
        event_id,
        hero_ids=tuple(range(1, 11)),
        raybet_match_id="42",
        map_number=1,
        note="clear target-live frame",
    )
    candidate = service.build_candidate(str(label["label_id"]))

    assert candidate["promoted"] is False
    assert candidate["production_feature_sha256"] != candidate["feature_sha256"]
    candidate_path = (
        service.paths.calibration_root
        / "candidates"
        / f"{candidate['candidate_id']}.npz"
    )
    assert candidate_path.is_file()
    with np.load(feature_path) as production, np.load(candidate_path) as calibrated:
        assert np.count_nonzero(production["hashes"]) == 0
        assert np.count_nonzero(calibrated["hashes"]) > 0


def test_calibration_rejects_duplicate_truth_and_unknown_assets(tmp_path: Path) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event_id = str(service.bootstrap()["events"][0]["event_id"])

    with pytest.raises(ValueError, match="ten unique"):
        service.save_label(
            event_id,
            hero_ids=(1,) * 10,
            raybet_match_id=None,
            map_number=None,
            note=None,
        )
    with pytest.raises(ValueError, match="unknown calibration event asset"):
        service.read_event_asset(event_id, "metadata.json")


def test_calibration_skips_malformed_debug_metadata(tmp_path: Path) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path)
    broken = (
        tmp_path
        / "data"
        / "live_betting"
        / "vision_debug"
        / "standard_dota_hud_1080p"
        / "broken"
    )
    broken.mkdir(parents=True)
    (broken / "metadata.json").write_text(
        '{"captured_at": "not-a-timestamp"}',
        encoding="utf-8",
    )

    service = VisionCalibrationService(tmp_path, feature_path=feature_path)

    assert len(service.bootstrap()["events"]) == 1


def test_calibration_rejects_candidate_from_another_label(tmp_path: Path) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event_id = str(service.bootstrap()["events"][0]["event_id"])
    service.save_label(
        event_id,
        hero_ids=tuple(range(1, 11)),
        raybet_match_id=None,
        map_number=None,
        note=None,
    )
    candidate = service.build_candidate(event_id)
    candidate_path = (
        service.paths.calibration_root
        / "candidates"
        / f"{candidate['candidate_id']}.json"
    )
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    payload["label_id"] = "fedcba9876543210fedc"
    candidate_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not belong"):
        service.run_evaluation(
            label_id=event_id,
            candidate_id=str(candidate["candidate_id"]),
            observation_file="holdout.jsonl",
            layout_profile="standard_dota_hud_1080p",
            mode="perception",
        )


def test_calibration_evaluation_persists_truth_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event_id = str(service.bootstrap()["events"][0]["event_id"])
    service.save_label(
        event_id,
        hero_ids=tuple(range(1, 11)),
        raybet_match_id="42",
        map_number=1,
        note=None,
    )
    candidate = service.build_candidate(event_id)
    observation_root = service.paths.observation_root
    observation_root.mkdir(parents=True)
    observation_path = observation_root / "42.jsonl"
    observation_path.write_text("{}\n", encoding="utf-8")
    sample = EvidenceSample(tmp_path / "frame.jpg", 0.0, "frame")

    monkeypatch.setattr(
        evaluator,
        "_observation_samples",
        lambda *_args, **_kwargs: ([sample], {"raybet_match_id": "42"}),
    )
    monkeypatch.setattr(
        evaluator,
        "evaluate",
        lambda *_args, **_kwargs: {
            "total_files": 12,
            "trackable_frames": 12,
            "truth_evaluation": {
                "best_candidate_accuracy": 1.0,
                "accepted_precision": 1.0,
                "final_locked_slots": 10,
                "final_correct_locked_slots": 10,
                "wrong_lock_count": 0,
                "lock_latency_seconds": 4.0,
                "exact_post_lock_rate": 1.0,
            },
        },
    )

    result = service.run_evaluation(
        label_id=event_id,
        candidate_id=str(candidate["candidate_id"]),
        observation_file=observation_path.name,
        layout_profile="standard_dota_hud_1080p",
        mode="perception",
    )

    assert result["final_correct_locked_slots"] == 10
    assert result["wrong_lock_count"] == 0
    assert len(service.bootstrap()["evaluations"]) == 1
