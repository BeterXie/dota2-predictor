from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

import cv2
import numpy as np
import pytest

import scripts.evaluate_hero_recognition as evaluator

from scripts.evaluate_hero_recognition import (
    EvidenceSample,
    _observation_samples,
    _validate_exact_mapping,
    _validate_single_map_clocks,
    _render_variant_usage,
    evaluate,
    main,
)
from vision.hero_recognizer import DraftReading


def test_exact_mapping_accepts_reversed_radiant_team_order() -> None:
    mapping = _validate_exact_mapping(
        raybet_match_id="42",
        map_number=2,
        opendota_match_id=1234,
        mapping_source="manual_exact",
        raybet_team_one_name="Alpha",
        raybet_team_two_name="Bravo",
        team_one_id=100,
        team_two_id=200,
        radiant_team_id=200,
        dire_team_id=100,
    )

    assert mapping.report_context(10) == {
        "raybet_match_id": "42",
        "raybet_map_number": 2,
        "opendota_match_id": 1234,
        "mapping_source": "manual_exact",
        "mapping_id": None,
        "raybet_team_one_name": "Alpha",
        "raybet_team_two_name": "Bravo",
        "team_one_id": 100,
        "team_two_id": 200,
        "opendota_radiant_team_id": 200,
        "opendota_dire_team_id": 100,
        "truth_hero_count": 10,
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"map_number": 0},
        {"mapping_source": "time_nearest"},
        {"raybet_team_one_name": ""},
        {"team_two_id": 100},
        {"radiant_team_id": 300},
    ],
)
def test_exact_mapping_rejects_incomplete_or_mismatched_identity(
    updates: dict[str, Any],
) -> None:
    values: dict[str, Any] = {
        "raybet_match_id": "42",
        "map_number": 1,
        "opendota_match_id": 1234,
        "mapping_source": "manual_exact",
        "raybet_team_one_name": "Alpha",
        "raybet_team_two_name": "Bravo",
        "team_one_id": 100,
        "team_two_id": 200,
        "radiant_team_id": 100,
        "dire_team_id": 200,
    }
    values.update(updates)

    with pytest.raises(ValueError):
        _validate_exact_mapping(**values)


def test_exact_mapping_rejects_cross_map_clock_reset() -> None:
    with pytest.raises(ValueError, match="game clock reset"):
        _validate_single_map_clocks([60, 600, None, 1_800, 120])

    _validate_single_map_clocks([None, 60, 600, None, 1_800])


def test_database_mode_requires_explicit_mapping_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_hero_recognition.py",
            "--raybet-match-id",
            "42",
            "--map-number",
            "1",
            "--opendota-match-id",
            "1234",
        ],
    )

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 2


def test_variant_usage_reports_selection_and_truth_outcomes() -> None:
    usage = {
        (91, "91"): Counter(
            selected=3,
            accepted=1,
            correct=2,
            wrong=1,
            accepted_correct=1,
        ),
        (91, "91__inset16"): Counter(
            selected=5,
            accepted=4,
            correct=4,
            wrong=1,
            accepted_correct=4,
        ),
    }

    assert _render_variant_usage(usage, {91: 2}, include_truth=True) == [
        {
            "hero_id": 91,
            "variant": "91",
            "hero_variant_count": 2,
            "selected": 3,
            "accepted": 1,
            "correct": 2,
            "wrong": 1,
            "accepted_correct": 1,
            "accepted_wrong": 0,
        },
        {
            "hero_id": 91,
            "variant": "91__inset16",
            "hero_variant_count": 2,
            "selected": 5,
            "accepted": 4,
            "correct": 4,
            "wrong": 1,
            "accepted_correct": 4,
            "accepted_wrong": 0,
        },
    ]


def test_cli_selects_stable_runtime_and_forced_layout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_evaluate(*_: object, **options: object) -> dict[str, object]:
        captured.update(options)
        return {"status": "ok"}

    monkeypatch.setattr(evaluator, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_hero_recognition.py",
            "--stable",
            "--perception-only",
            "--layout-profile",
            "wxc_gotf_2026_live_1080p",
        ],
    )

    assert main() == 0
    assert captured["stable"] is True
    assert captured["runtime_gates"] is False
    assert captured["layout_profile"] == "wxc_gotf_2026_live_1080p"
    assert '"status": "ok"' in capsys.readouterr().out


def test_perception_only_evaluation_does_not_run_ocr_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_path = tmp_path / "frame.jpg"
    assert cv2.imwrite(
        str(frame_path), np.full((1080, 1920, 3), 100, dtype=np.uint8)
    )

    class FakeRecognizer:
        def __init__(self, *_: object) -> None:
            pass

        def read(self, _: np.ndarray) -> DraftReading:
            return DraftReading((1, 2, 3, 4, 5), (6, 7, 8, 9, 10), 0.9)

    def fail_scoreboard(*_: object, **__: object) -> None:
        raise AssertionError("perception-only evaluation must not initialize OCR")

    monkeypatch.setattr(evaluator, "StableHeroRecognizer", FakeRecognizer)
    monkeypatch.setattr(evaluator, "ScoreboardReader", fail_scoreboard)

    report = evaluate(
        tmp_path,
        tmp_path / "unused.npz",
        samples=[EvidenceSample(frame_path, 0.0, "frame-1")],
        stable=True,
        layout_profile="wxc_gotf_2026_live_1080p",
        runtime_gates=False,
    )

    assert report["evaluation_mode"] == "perception"
    assert report["trackable_frames"] == 1


def test_runtime_evaluation_freezes_until_target_identity_is_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_path = tmp_path / "frame.jpg"
    assert cv2.imwrite(
        str(frame_path), np.full((1080, 1920, 3), 100, dtype=np.uint8)
    )
    update_calls = 0

    class FakeTracker:
        slot_statuses: tuple[object, ...] = ()
        current_draft = None

        def reset(self) -> None:
            pass

        def update(self, *_: object, **__: object) -> None:
            nonlocal update_calls
            update_calls += 1

    class FakeRecognizer:
        def __init__(self, *_: object) -> None:
            pass

        def read(self, _: np.ndarray) -> DraftReading:
            return DraftReading((), (), 0.0)

    class FakeScoreboardReader:
        def __init__(self, *_: object) -> None:
            pass

        def read_replay_gate(self, _: np.ndarray) -> object:
            return type("ReplayGate", (), {"status": "live"})()

    class FakeFrameQualityTracker:
        def assess(self, _: np.ndarray) -> object:
            return type("Quality", (), {"usable": True})()

    monkeypatch.setattr(evaluator, "StableDraftTracker", FakeTracker)
    monkeypatch.setattr(evaluator, "StableHeroRecognizer", FakeRecognizer)
    monkeypatch.setattr(evaluator, "ScoreboardReader", FakeScoreboardReader)
    monkeypatch.setattr(evaluator, "FrameQualityTracker", FakeFrameQualityTracker)
    monkeypatch.setattr(evaluator, "classify_screen_state", lambda *_: ("game", 1.0))

    report = evaluate(
        tmp_path,
        tmp_path / "unused.npz",
        samples=[EvidenceSample(frame_path, 0.0, "frame-1", 30, False)],
        stable=True,
        layout_profile="wxc_gotf_2026_live_1080p",
    )

    assert update_calls == 0
    assert report["target_identity_confirmed_frames"] == 0
    assert report["trackable_frames"] == 0
    assert report["tracking_blocker_counts"] == {"target_identity_unconfirmed": 1}


def test_observation_jsonl_loads_retained_frames_and_context(tmp_path: Path) -> None:
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"frame")
    observation_path = tmp_path / "observations.jsonl"
    rows = [
        {
            "raybet_match_id": "42",
            "map_number": 1,
            "captured_at_utc": "2026-08-01T00:00:00Z",
            "game_clock_seconds": 30,
            "source_frame_sha256": "abc",
            "source_frame_path": str(frame_path),
        },
        {
            "raybet_match_id": "42",
            "map_number": None,
            "captured_at_utc": "2026-08-01T00:00:01Z",
            "game_clock_seconds": None,
            "source_frame_sha256": None,
            "source_frame_path": None,
        },
    ]
    observation_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    samples, context = _observation_samples(observation_path)

    assert samples == [
        EvidenceSample(frame_path, 1_785_542_400.0, "abc", 30, False)
    ]
    assert context["raybet_match_id"] == "42"
    assert context["map_numbers"] == [1]
    assert context["retained_frames"] == 1
    assert context["missing_frames"] == 1


def test_observation_jsonl_selects_map_inferred_from_clock_reset(tmp_path: Path) -> None:
    frame_paths = []
    for index in range(4):
        frame_path = tmp_path / f"frame-{index}.jpg"
        frame_path.write_bytes(b"frame")
        frame_paths.append(frame_path)
    observation_path = tmp_path / "38416120.jsonl"
    rows = [
        {
            "raybet_match_id": "38416120",
            "map_number": 1,
            "captured_at_utc": f"2026-08-01T00:00:0{index}Z",
            "game_clock_seconds": clock,
            "source_frame_sha256": f"frame-{index}",
            "source_frame_path": str(frame_paths[index]),
        }
        for index, clock in enumerate((1820, 1830, 44, 54))
    ]
    observation_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    samples, context = _observation_samples(observation_path, map_number=2)

    assert [sample.path for sample in samples] == frame_paths[2:]
    assert context["map_numbers"] == [1, 2]
    assert context["source_map_numbers"] == [1]
    assert context["selected_map_number"] == 2
    assert context["clock_reset_count"] == 1
    assert context["selected_jsonl_rows"] == 2


def test_observation_jsonl_prefers_multiple_explicit_map_segments_over_clock_noise(
    tmp_path: Path,
) -> None:
    frame_paths = []
    for index in range(4):
        frame_path = tmp_path / f"frame-{index}.jpg"
        frame_path.write_bytes(b"frame")
        frame_paths.append(frame_path)
    observation_path = tmp_path / "38422524.jsonl"
    rows = [
        {
            "raybet_match_id": "38422524",
            "map_number": map_number,
            "captured_at_utc": f"2026-08-01T00:00:0{index}Z",
            "game_clock_seconds": clock,
            "source_frame_sha256": f"frame-{index}",
            "source_frame_path": str(frame_paths[index]),
        }
        for index, (map_number, clock) in enumerate(
            ((1, 1000), (1, 700), (2, 44), (2, 54))
        )
    ]
    observation_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    samples, context = _observation_samples(observation_path, map_number=2)

    assert [sample.path for sample in samples] == frame_paths[2:]
    assert context["map_numbers"] == [1, 2]
    assert context["source_map_numbers"] == [1, 2]
    assert context["selected_jsonl_rows"] == 2


def test_observation_jsonl_requires_map_for_multiple_inferred_segments(
    tmp_path: Path,
) -> None:
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"frame")
    observation_path = tmp_path / "38416120.jsonl"
    rows = [
        {
            "raybet_match_id": "38416120",
            "map_number": 1,
            "captured_at_utc": f"2026-08-01T00:00:0{index}Z",
            "game_clock_seconds": clock,
            "source_frame_sha256": f"frame-{index}",
            "source_frame_path": str(frame_path),
        }
        for index, clock in enumerate((1820, 44))
    ]
    observation_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="multiple map segments"):
        _observation_samples(observation_path)


def test_cli_evaluates_observation_jsonl_with_explicit_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation_path = tmp_path / "observations.jsonl"
    observation_path.write_text("{}\n", encoding="utf-8")
    samples = [EvidenceSample(tmp_path / "frame.jpg", 0.0, "frame")]
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        evaluator,
        "_observation_samples",
        lambda _path, **_filters: (samples, {"source": "observation_jsonl"}),
    )

    def fake_evaluate(*_: object, **options: object) -> dict[str, object]:
        captured.update(options)
        return {"status": "ok"}

    monkeypatch.setattr(evaluator, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_hero_recognition.py",
            "--stable",
            "--observation-jsonl",
            str(observation_path),
            "--truth-hero-ids",
            "51",
            "77",
            "65",
            "123",
            "53",
            "36",
            "74",
            "96",
            "7",
            "3",
        ],
    )

    assert main() == 0
    assert captured["samples"] == samples
    assert captured["truth_hero_ids"] == (51, 77, 65, 123, 53, 36, 74, 96, 7, 3)
