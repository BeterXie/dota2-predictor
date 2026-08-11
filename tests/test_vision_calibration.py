from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import pytest

import scripts.evaluate_hero_recognition as evaluator
from scripts.evaluate_hero_recognition import EvidenceSample
from vision.calibration import VisionCalibrationService
from vision.profile_features import promoted_profile_feature_path


def _feature_pack(path: Path) -> None:
    ids = np.arange(1, 11, dtype=np.int32)
    np.savez_compressed(
        path,
        ids=ids,
        hashes=np.zeros((10, 64), dtype=np.uint8),
        histograms=np.zeros((10, 384), dtype=np.float32),
        thumbnails=np.zeros((10, 32, 48), dtype=np.uint8),
    )


def _debug_event(
    project_root: Path,
    *,
    event_name: str = "20260808T120000_ambiguous_match",
    captured_at: float = 1_785_600_000.0,
    raybet_match_id: str = "42",
    map_number: int = 1,
) -> Path:
    event_root = (
        project_root
        / "data"
        / "live_betting"
        / "vision_debug"
        / "series"
        / raybet_match_id
        / f"map_{map_number}"
        / "standard_dota_hud_1080p"
        / event_name
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
                "captured_at": captured_at,
                "reason": "ambiguous_match",
                "layout": "standard_dota_hud_1080p",
                "raybet_match_id": raybet_match_id,
                "map_number": map_number,
                "source_frame_ref": f"stream:{raybet_match_id}:{event_name}",
                "identity_status": "explicit_watcher_context",
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
    assert event["profile_id"] == "standard_dota_hud_1080p"
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
    assert candidate["profile_id"] == "standard_dota_hud_1080p"
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
        assert calibrated["ids"].tolist() == list(range(1, 11)) * 2
        assert calibrated["variant_names"][:10].tolist() == [
            str(hero_id) for hero_id in range(1, 11)
        ]
        assert all(
            name.startswith(f"{hero_id}__calibration_")
            for hero_id, name in zip(
                range(1, 11),
                calibrated["variant_names"][10:].tolist(),
                strict=True,
            )
        )
        assert np.array_equal(calibrated["hashes"][:10], production["hashes"])
    assert candidate["added_variant_count"] == 10


def test_calibration_promotes_only_two_safe_evaluations_to_its_profile(
    tmp_path: Path,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event_id = str(service.bootstrap()["events"][0]["event_id"])
    label = service.save_label(
        event_id,
        hero_ids=tuple(range(1, 11)),
        raybet_match_id="42",
        map_number=1,
        note=None,
    )
    candidate = service.build_candidate(str(label["label_id"]))
    evaluation_ids = ("a" * 32, "b" * 32)
    evaluation_root = service.paths.calibration_root / "evaluations"
    evaluation_root.mkdir(parents=True)
    for evaluation_id in evaluation_ids:
        summary = {
            "evaluation_id": evaluation_id,
            "candidate_id": candidate["candidate_id"],
            "mode": "perception",
            "total_files": 20,
            "final_locked_slots": 10,
            "final_correct_locked_slots": 10,
            "wrong_lock_count": 0,
            "accepted_precision": 1.0,
            "exact_post_lock_rate": 1.0,
        }
        (evaluation_root / f"{evaluation_id}.json").write_text(
            json.dumps({"calibration_summary": summary}),
            encoding="utf-8",
        )

    promoted = service.promote_candidate(
        str(candidate["candidate_id"]),
        evaluation_ids=evaluation_ids,
    )

    assert promoted["profile_id"] == "standard_dota_hud_1080p"
    promoted_candidate = next(
        row
        for row in service.bootstrap()["candidates"]
        if row["candidate_id"] == candidate["candidate_id"]
    )
    assert promoted_candidate["promoted"] is True
    assert promoted_candidate["promoted_at"] == promoted["promoted_at"]
    assert promoted_candidate["promotion_evaluation_ids"] == list(evaluation_ids)
    assert promoted_profile_feature_path(
        "standard_dota_hud_1080p",
        calibration_root=service.paths.calibration_root,
    ).is_file()
    assert (
        promoted_profile_feature_path(
            "wxc_gotf_2026_live_1080p",
            calibration_root=service.paths.calibration_root,
        )
        is None
    )

    second_event_name = "20260808T130000_ambiguous_match"
    second_event_root = _debug_event(
        tmp_path,
        event_name=second_event_name,
        captured_at=1_785_603_600.0,
        raybet_match_id="43",
        map_number=2,
    )
    for index in range(10):
        crop = np.full((48, 64, 3), 210 - index * 11, dtype=np.uint8)
        cv2.rectangle(crop, (5 + index, 6), (30 + index, 35), (20, 220, 90), -1)
        assert cv2.imwrite(
            str(second_event_root / f"hero_slot_{index + 1:02d}.jpg"), crop
        )
    second_event = next(
        event
        for event in service.bootstrap()["events"]
        if str(event["relative_path"]).endswith(second_event_name)
    )
    second_label = service.save_label(
        str(second_event["event_id"]),
        hero_ids=tuple(range(1, 11)),
        raybet_match_id="43",
        map_number=2,
        note=None,
    )
    second_candidate = service.build_candidate(str(second_label["label_id"]))
    second_candidate_path = (
        service.paths.calibration_root
        / "candidates"
        / f"{second_candidate['candidate_id']}.npz"
    )
    with np.load(second_candidate_path) as accumulated:
        assert accumulated["ids"].shape == (30,)
        assert all(
            np.count_nonzero(accumulated["ids"] == hero_id) == 3
            for hero_id in range(1, 11)
        )
        assert all(
            np.count_nonzero(
                np.char.startswith(
                    accumulated["variant_names"].astype(str),
                    f"{hero_id}__calibration_",
                )
            ) == 2
            for hero_id in range(1, 11)
        )
    assert second_candidate["base_feature_sha256"] == promoted["feature_sha256"]
    assert second_candidate["added_variant_count"] == 10


def test_calibration_accumulates_variants_on_an_unpromoted_same_profile_candidate(
    tmp_path: Path,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path, event_name="20260808T120000_first_appearance")
    second_root = _debug_event(
        tmp_path,
        event_name="20260808T130000_second_appearance",
        captured_at=1_785_603_600.0,
        raybet_match_id="43",
    )
    for index in range(10):
        crop = np.full((48, 64, 3), 210 - index * 11, dtype=np.uint8)
        cv2.rectangle(crop, (5 + index, 6), (30 + index, 35), (20, 220, 90), -1)
        assert cv2.imwrite(str(second_root / f"hero_slot_{index + 1:02d}.jpg"), crop)

    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    events = {
        str(event["relative_path"]).split("/")[-1]: str(event["event_id"])
        for event in service.bootstrap()["events"]
    }
    first_id = events["20260808T120000_first_appearance"]
    second_id = events["20260808T130000_second_appearance"]
    for event_id, match_id in ((first_id, "42"), (second_id, "43")):
        service.save_label(
            event_id,
            hero_ids=tuple(range(1, 11)),
            raybet_match_id=match_id,
            map_number=1,
            note=None,
        )
    first = service.build_candidate(first_id)
    second = service.build_candidate(
        second_id,
        base_candidate_id=str(first["candidate_id"]),
    )

    second_path = (
        service.paths.calibration_root
        / "candidates"
        / f"{second['candidate_id']}.npz"
    )
    with np.load(second_path) as accumulated:
        assert accumulated["ids"].shape == (30,)
        assert all(
            np.count_nonzero(accumulated["ids"] == hero_id) == 3
            for hero_id in range(1, 11)
        )
    assert second["base_candidate_id"] == first["candidate_id"]
    assert second["base_feature_sha256"] == first["feature_sha256"]
    assert second["added_variant_count"] == 10
    assert not (service.paths.calibration_root / "promoted").exists()


def test_calibration_rejects_base_candidate_from_another_profile(
    tmp_path: Path,
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
    candidate_path = (
        service.paths.calibration_root
        / "candidates"
        / f"{candidate['candidate_id']}.json"
    )
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    payload["profile_id"] = "wxc_gotf_2026_live_1080p"
    candidate_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="another UI profile"):
        service.build_candidate(
            event_id,
            base_candidate_id=str(candidate["candidate_id"]),
        )


def test_calibration_rejects_unsafe_profile_promotion(tmp_path: Path) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event_id = str(service.bootstrap()["events"][0]["event_id"])
    label = service.save_label(
        event_id,
        hero_ids=tuple(range(1, 11)),
        raybet_match_id="42",
        map_number=1,
        note=None,
    )
    candidate = service.build_candidate(str(label["label_id"]))
    evaluation_root = service.paths.calibration_root / "evaluations"
    evaluation_root.mkdir(parents=True)
    for evaluation_id, wrong_locks in (("a" * 32, 0), ("b" * 32, 1)):
        summary = {
            "evaluation_id": evaluation_id,
            "candidate_id": candidate["candidate_id"],
            "mode": "perception",
            "total_files": 20,
            "final_locked_slots": 10,
            "final_correct_locked_slots": 10 - wrong_locks,
            "wrong_lock_count": wrong_locks,
            "accepted_precision": 1.0,
            "exact_post_lock_rate": 1.0,
        }
        (evaluation_root / f"{evaluation_id}.json").write_text(
            json.dumps({"calibration_summary": summary}),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="safety gate"):
        service.promote_candidate(
            str(candidate["candidate_id"]),
            evaluation_ids=("a" * 32, "b" * 32),
        )


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


def test_calibration_requires_match_and_map_context_for_label(tmp_path: Path) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event_id = str(service.bootstrap()["events"][0]["event_id"])

    with pytest.raises(ValueError, match="RayBet match ID and map number"):
        service.save_label(
            event_id,
            hero_ids=tuple(range(1, 11)),
            raybet_match_id=None,
            map_number=None,
            note=None,
        )


@pytest.mark.parametrize(
    ("raybet_match_id", "map_number"),
    (("43", 1), ("42", 2)),
)
def test_calibration_rejects_label_identity_that_differs_from_event(
    tmp_path: Path,
    raybet_match_id: str,
    map_number: int,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(tmp_path, raybet_match_id="42", map_number=1)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event_id = str(service.bootstrap()["events"][0]["event_id"])

    with pytest.raises(ValueError, match="identity differs"):
        service.save_label(
            event_id,
            hero_ids=tuple(range(1, 11)),
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            note=None,
        )


def test_calibration_rejects_legacy_event_without_explicit_identity(
    tmp_path: Path,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    source = _debug_event(tmp_path)
    legacy = (
        tmp_path
        / "data"
        / "live_betting"
        / "vision_debug"
        / "standard_dota_hud_1080p"
        / source.name
    )
    legacy.parent.mkdir(parents=True)
    source.replace(legacy)
    metadata_path = legacy / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    for key in (
        "raybet_match_id",
        "map_number",
        "source_frame_ref",
        "identity_status",
    ):
        metadata.pop(key, None)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event = service.bootstrap()["events"][0]

    assert event["identity_status"] == "missing"
    with pytest.raises(ValueError, match="no verified Series/Map identity"):
        service.save_label(
            str(event["event_id"]),
            hero_ids=tuple(range(1, 11)),
            raybet_match_id="42",
            map_number=1,
            note=None,
        )


def test_promoted_feature_pack_requires_verified_source_identity(
    tmp_path: Path,
) -> None:
    promoted_root = tmp_path / "promoted"
    promoted_root.mkdir()
    feature_path = promoted_root / "standard_dota_hud_1080p.npz"
    _feature_pack(feature_path)
    (promoted_root / "standard_dota_hud_1080p.json").write_text(
        json.dumps(
            {
                "profile_id": "standard_dota_hud_1080p",
                "feature_sha256": "not-relevant-without-verified-identity",
            }
        ),
        encoding="utf-8",
    )

    assert (
        promoted_profile_feature_path(
            "standard_dota_hud_1080p",
            calibration_root=tmp_path,
        )
        is None
    )


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


def test_calibration_keeps_labeled_events_outside_the_recent_limit(
    tmp_path: Path,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    older = _debug_event(
        tmp_path,
        event_name="20260808T120000_labeled",
        captured_at=1_785_600_000.0,
    )
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    event_id = str(service.bootstrap()["events"][0]["event_id"])
    service.save_label(
        event_id,
        hero_ids=tuple(range(1, 11)),
        raybet_match_id="42",
        map_number=1,
        note=None,
    )
    newer = _debug_event(
        tmp_path,
        event_name="20260809T120000_recent",
        captured_at=1_785_686_400.0,
    )
    os.utime(older / "metadata.json", (1, 1))
    os.utime(newer / "metadata.json", (2, 2))

    bootstrap = service.bootstrap(limit=1)

    names = [str(event["relative_path"]).split("/")[-1] for event in bootstrap["events"]]
    assert names == ["20260809T120000_recent", "20260808T120000_labeled"]
    profile = bootstrap["profiles"][0]
    assert profile["event_count"] == 2
    assert profile["labeled_event_count"] == 1


def test_calibration_uses_shared_observation_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    observation_root = tmp_path / "shared" / "vision_observations"
    observation_root.mkdir(parents=True)
    (observation_root / "38417147.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("VISION_OBSERVATION_DIR", str(observation_root))

    service = VisionCalibrationService(tmp_path / "project", feature_path=feature_path)
    bootstrap = service.bootstrap()

    assert service.paths.observation_root == observation_root.resolve()
    assert bootstrap["observation_root"] == str(observation_root.resolve())
    assert bootstrap["observation_files"] == [
        {
            "name": "38417147.jsonl",
            "bytes": (observation_root / "38417147.jsonl").stat().st_size,
        }
    ]


def test_calibration_summarizes_a_complete_match_corpus(tmp_path: Path) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    service.paths.observation_root.mkdir(parents=True)
    observation_path = service.paths.observation_root / "38422524.jsonl"
    observation_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "raybet_match_id": "38422524",
                        "captured_at_utc": "2026-08-09T13:40:00Z",
                        "map_number": 1,
                        "screen_state": "draft",
                        "source_frame_ref": "vision-frame:sha256:first",
                        "source_frame_sha256": "first",
                    }
                ),
                "not-json",
                json.dumps(
                    {
                        "raybet_match_id": "38422524",
                        "captured_at_utc": "2026-08-09T13:41:00Z",
                        "map_number": 1,
                        "screen_state": "game",
                        "source_frame_ref": "stream:one:2",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    service.paths.evidence_root.mkdir(parents=True)
    (service.paths.evidence_root / "38422524.manifest.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "draft_started",
                        "phase": "draft_started",
                        "captured_at": "2026-08-09T13:40:00+00:00",
                        "frame_ref": "vision-frame:sha256:first",
                        "screen_state": "draft",
                        "map_number": 1,
                    }
                ),
                json.dumps(
                    {
                        "event": "game_started",
                        "phase": "game_started",
                        "captured_at": "2026-08-09T13:41:30+00:00",
                        "frame_ref": "vision-frame:sha256:second",
                        "screen_state": "game",
                        "map_number": 1,
                    }
                ),
                json.dumps(
                    {
                        "event": "periodic_30s",
                        "phase": "game_started",
                        "captured_at": "2026-08-09T13:42:00+00:00",
                        "frame_ref": "vision-frame:sha256:third",
                        "screen_state": "game",
                        "map_number": 1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (service.paths.observation_root / "38422524.heartbeat.json").write_text(
        json.dumps(
            {
                "capture_phase": "game_started",
                "capture_status": "producing_trusted",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "layout": {"profile": "epl_masters_live_1080p"},
                "screen": {"state": "game"},
            }
        ),
        encoding="utf-8",
    )

    summaries = service.bootstrap()["match_summaries"]

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["match_id"] == "38422524"
    assert summary["status"] == "live"
    assert summary["status_label"] == "比赛进行中"
    assert summary["observation_count"] == 2
    assert summary["evidence_frame_count"] == 3
    assert summary["manifest_event_count"] == 3
    assert summary["periodic_count"] == 1
    assert summary["draft_started"] is True
    assert summary["game_started"] is True
    assert summary["maps"] == [1]
    assert summary["layout_profile"] == "epl_masters_live_1080p"
    assert summary["latest_screen_state"] == "game"


def test_calibration_rejects_candidate_from_another_ui_profile(tmp_path: Path) -> None:
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
    candidate_path = (
        service.paths.calibration_root
        / "candidates"
        / f"{candidate['candidate_id']}.json"
    )
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    payload["profile_id"] = "wxc_gotf_2026_live_1080p"
    candidate_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="UI profile"):
        service.run_evaluation(
            label_id=event_id,
            candidate_id=str(candidate["candidate_id"]),
            observation_file="holdout.jsonl",
            layout_profile="standard_dota_hud_1080p",
            mode="perception",
        )


def test_calibration_candidate_reuses_across_matches_in_same_ui_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_path = tmp_path / "features.npz"
    _feature_pack(feature_path)
    _debug_event(
        tmp_path,
        event_name="20260808T120000_match_one",
        raybet_match_id="match-one",
    )
    _debug_event(
        tmp_path,
        event_name="20260808T120100_match_two",
        captured_at=1_785_600_060.0,
        raybet_match_id="match-two",
    )
    service = VisionCalibrationService(tmp_path, feature_path=feature_path)
    events = service.bootstrap()["events"]
    ids = {str(event["relative_path"]).split("/")[-1]: str(event["event_id"]) for event in events}
    first_id = ids["20260808T120000_match_one"]
    second_id = ids["20260808T120100_match_two"]
    service.save_label(
        first_id,
        hero_ids=tuple(range(1, 11)),
        raybet_match_id="match-one",
        map_number=1,
        note=None,
    )
    service.save_label(
        second_id,
        hero_ids=tuple(range(11, 21)),
        raybet_match_id="match-two",
        map_number=1,
        note=None,
    )
    candidate = service.build_candidate(first_id)
    observation_root = service.paths.observation_root
    observation_root.mkdir(parents=True)
    observation_path = observation_root / "holdout.jsonl"
    observation_path.write_text("{}\n", encoding="utf-8")
    sample = EvidenceSample(tmp_path / "frame.jpg", 0.0, "frame")
    monkeypatch.setattr(
        evaluator,
        "_observation_samples",
        lambda *_args, **_kwargs: ([sample], {"raybet_match_id": "match-two"}),
    )
    monkeypatch.setattr(
        evaluator,
        "evaluate",
        lambda *_args, **_kwargs: {
            "total_files": 1,
            "trackable_frames": 1,
            "truth_evaluation": {
                "best_candidate_accuracy": 1.0,
                "accepted_precision": 1.0,
                "final_locked_slots": 10,
                "final_correct_locked_slots": 10,
                "wrong_lock_count": 0,
                "lock_latency_seconds": 1.0,
                "exact_post_lock_rate": 1.0,
            },
        },
    )

    result = service.run_evaluation(
        label_id=second_id,
        candidate_id=str(candidate["candidate_id"]),
        observation_file=observation_path.name,
        layout_profile="standard_dota_hud_1080p",
        mode="perception",
    )

    assert result["label_id"] == second_id
    assert result["candidate_id"] == candidate["candidate_id"]


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

    observation_options: dict[str, object] = {}

    def fake_observation_samples(*_args: object, **kwargs: object) -> tuple[list[EvidenceSample], dict[str, object]]:
        observation_options.update(kwargs)
        return [sample], {"raybet_match_id": "42"}

    monkeypatch.setattr(evaluator, "_observation_samples", fake_observation_samples)
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
    assert result["raybet_match_id"] == "42"
    assert result["map_number"] == 1
    assert observation_options["map_number"] == 1
    assert len(service.bootstrap()["evaluations"]) == 1


def test_calibration_evaluation_rejects_observation_from_another_match(
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
    service.paths.observation_root.mkdir(parents=True)
    observation_path = service.paths.observation_root / "other.jsonl"
    observation_path.write_text("{}\n", encoding="utf-8")
    sample = EvidenceSample(tmp_path / "frame.jpg", 0.0, "frame")
    monkeypatch.setattr(
        evaluator,
        "_observation_samples",
        lambda *_args, **_kwargs: ([sample], {"raybet_match_id": "different"}),
    )

    with pytest.raises(ValueError, match="different RayBet match"):
        service.run_evaluation(
            label_id=event_id,
            candidate_id=str(candidate["candidate_id"]),
            observation_file=observation_path.name,
            layout_profile="standard_dota_hud_1080p",
            mode="perception",
        )


def test_calibration_evaluation_rejects_layout_mismatch(tmp_path: Path) -> None:
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

    with pytest.raises(ValueError, match="layout must match"):
        service.run_evaluation(
            label_id=event_id,
            candidate_id=str(candidate["candidate_id"]),
            observation_file="42.jsonl",
            layout_profile="wxc_gotf_2026_live_1080p",
            mode="perception",
        )
