from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from vision.frame_quality import FrameQualityTracker
from vision.hero_recognizer import DraftReading, HeroSlotDiagnostic
from vision.layout_tracker import LayoutTracker, StableLayoutState
from vision.scoreboard_reader import ReplayGateReading
from vision.stable_runtime import (
    StableDraftTracker,
    StableHudReader,
    StableHeroRecognizer,
    StableHLSStreamCapture,
    freeze_untrusted_draft_tracking,
    freeze_untrusted_live_hud_tracking,
    install_stable_runtime,
    require_target_team_confirmation,
)
from vision.vision_debug import VisionDebugSink


def _draft_reading(
    hero_ids: tuple[int, ...] = tuple(range(1, 11)),
) -> DraftReading:
    diagnostics = tuple(
        HeroSlotDiagnostic(
            "radiant" if index < 5 else "dire",
            index % 5 + 1,
            hero_id,
            0.90,
            0.70,
            0.20,
            True,
            "accepted",
            crop_hash=f"{index + 1:016x}",
        )
        for index, hero_id in enumerate(hero_ids)
    )
    return DraftReading((), (), 0.90, diagnostics)


def _lock_draft(tracker: StableDraftTracker) -> DraftReading:
    reading = _draft_reading()
    locked = None
    expected_states = ("observing", "provisional", "locked")
    for step in range(3):
        locked = tracker.update(
            reading,
            observed_at=float(step),
            source_frame_hash=f"frame-{step}",
        )
        assert all(
            status.state == expected_states[step] for status in tracker.slot_statuses
        )
    assert locked is not None
    return locked


class _FakeVideoCapture:
    def __init__(self, images: list[np.ndarray]) -> None:
        self.images = iter(images)

    def set(self, *_: object) -> bool:
        return True

    def read(self) -> tuple[bool, np.ndarray]:
        return True, next(self.images)

    def release(self) -> None:
        pass


def test_same_stream_distinct_frames_are_distinct_evidence() -> None:
    images = []
    for value in (10, 20, 30):
        image = np.full((24, 32, 3), value, dtype=np.uint8)
        images.append(image)
    native_capture = _FakeVideoCapture(images)
    capture = StableHLSStreamCapture(
        "https://play.ehome.gg/live/example.m3u8",
        capture_factory=lambda _: native_capture,
    )

    frames = [capture.read() for _ in range(3)]
    assert len({frame.source_hash for frame in frames}) == 1
    assert len({frame.frame_hash for frame in frames}) == 3

    tracker = StableDraftTracker(
        confirmations=2,
        minimum_evidence_interval=1.0,
        minimum_lock_interval=1.0,
    )
    locked = None
    for step, frame in enumerate(frames):
        locked = tracker.update(
            _draft_reading(),
            observed_at=float(step),
            source_frame_hash=frame.frame_hash,
        )
    assert locked is not None


def test_exact_frozen_frame_does_not_add_independent_evidence() -> None:
    tracker = StableDraftTracker(
        confirmations=2,
        minimum_evidence_interval=1.0,
        minimum_lock_interval=1.0,
    )

    for step in range(3):
        assert tracker.update(
            _draft_reading(),
            observed_at=float(step),
            source_frame_hash="identical-frame",
            game_clock_seconds=100 + step,
        ) is None

    assert all(status.duplicate_evidence_count == 2 for status in tracker.slot_statuses)


def test_layout_tracker_survives_short_detection_dropouts() -> None:
    tracker = LayoutTracker(acquire_confirmations=2, grace_frames=3)
    name = "epl_s39_live_1080p"

    assert tracker.update({name: 0.96}).layout_name is None
    assert tracker.update({name: 0.97}).layout_name == name

    first_miss = tracker.update({name: 0.2})
    second_miss = tracker.update({name: 0.1})
    assert first_miss.layout_name == name
    assert second_miss.layout_name == name
    assert first_miss.state == "degraded"

    recovered = tracker.update({name: 0.93})
    assert recovered.layout_name == name
    assert recovered.state == "locked"


def test_single_layout_miss_does_not_reset_locked_draft() -> None:
    layout_tracker = LayoutTracker(acquire_confirmations=1, grace_frames=2)
    draft_tracker = StableDraftTracker(
        confirmations=2,
        minimum_evidence_interval=1.0,
        minimum_lock_interval=1.0,
    )
    locked = _lock_draft(draft_tracker)

    name = "epl_s39_live_1080p"
    assert layout_tracker.update({name: 0.96}).layout_name == name
    missed = layout_tracker.update({name: 0.0})

    assert missed.layout_name == name
    assert missed.state == "degraded"
    assert draft_tracker.current_draft == locked


def test_layout_tracker_requires_sustained_challenger() -> None:
    tracker = LayoutTracker(
        acquire_confirmations=1,
        switch_confirmations=3,
        switch_margin=0.15,
    )
    left = "epl_s39_live_1080p"
    right = "wxc_gotf_2026_live_1080p"
    assert tracker.update({left: 0.96, right: 0.1}).layout_name == left

    for _ in range(2):
        state = tracker.update({left: 0.4, right: 0.96})
        assert state.layout_name == left
        assert state.state == "switching"

    switched = tracker.update({left: 0.4, right: 0.97})
    assert switched.layout_name == right
    assert switched.state == "locked"


def test_frame_quality_detects_exact_freeze() -> None:
    tracker = FrameQualityTracker(freeze_confirmations=3, minimum_sharpness=0.0)
    image = np.full((120, 200, 3), 100, dtype=np.uint8)
    assert not tracker.assess(image).frozen
    assert not tracker.assess(image).frozen
    frozen = tracker.assess(image)
    assert frozen.frozen
    assert frozen.reason == "frozen_frame"


def test_static_crop_can_accumulate_temporal_evidence_and_lock() -> None:
    tracker = StableDraftTracker(
        minimum_evidence_interval=1.0,
        minimum_lock_interval=2.0,
    )
    reading = _draft_reading()

    locked = None
    for step in range(7):
        locked = tracker.update(
            reading,
            observed_at=float(step),
            source_frame_hash=f"frame-{step}",
            game_clock_seconds=100,
        )

    assert locked is not None
    assert locked.radiant_hero_ids == (1, 2, 3, 4, 5)
    assert locked.dire_hero_ids == (6, 7, 8, 9, 10)
    assert all(status.state == "locked" for status in tracker.slot_statuses)


def test_replay_freezes_locked_lineup_and_live_tracking_resumes() -> None:
    tracker = StableDraftTracker(
        confirmations=2,
        minimum_evidence_interval=1.0,
        minimum_lock_interval=1.0,
    )
    locked = _lock_draft(tracker)

    assert not freeze_untrusted_live_hud_tracking(ReplayGateReading("replay", 0.99))
    assert freeze_untrusted_draft_tracking(tracker, locked) == locked

    assert freeze_untrusted_live_hud_tracking(ReplayGateReading("live", 0.99))
    resumed = tracker.update(
        _draft_reading(),
        observed_at=3.0,
        source_frame_hash="frame-after-replay",
    )
    assert resumed == locked


def test_stable_runtime_installs_replay_freeze_adapters() -> None:
    watcher_module = SimpleNamespace()
    install_stable_runtime(watcher_module)

    assert watcher_module.allow_live_hud_tracking is freeze_untrusted_live_hud_tracking
    assert watcher_module.draft_during_untrusted is freeze_untrusted_draft_tracking
    assert (
        watcher_module.allow_target_draft_tracking
        is require_target_team_confirmation
    )


def test_stable_draft_tracking_requires_confirmed_target_team() -> None:
    assert not require_target_team_confirmation(radiant_team_side=None)
    assert require_target_team_confirmation(radiant_team_side="team_one")
    assert require_target_team_confirmation(radiant_team_side="team_two")


def test_locked_lineup_survives_strong_ocr_jitter() -> None:
    tracker = StableDraftTracker(
        confirmations=2,
        minimum_evidence_interval=1.0,
        minimum_lock_interval=1.0,
    )
    locked = _lock_draft(tracker)
    jittered_ids = (11, 2, 3, 4, 5, 6, 7, 8, 9, 10)

    for step in range(3, 7):
        current = tracker.update(
            _draft_reading(jittered_ids),
            observed_at=float(step),
            source_frame_hash=f"jitter-{step}",
        )
        assert current == locked

    assert tracker.slot_statuses[0].state == "locked"
    assert tracker.slot_statuses[0].strong_conflict_count == 4


class _HeroRegion:
    def __init__(self, index: int) -> None:
        self.index = index

    def crop(self, _: np.ndarray) -> np.ndarray:
        return np.full((2, 2, 3), self.index, dtype=np.uint8)


def test_global_hero_assignment_never_duplicates_heroes() -> None:
    recognizer = StableHeroRecognizer.__new__(StableHeroRecognizer)
    regions = tuple(_HeroRegion(index) for index in range(10))
    recognizer.layout = SimpleNamespace(
        radiant_heroes=regions[:5],
        dire_heroes=regions[5:],
    )
    recognizer.ids = np.arange(1, 11, dtype=np.int32)

    def score_crop(crop: np.ndarray) -> SimpleNamespace:
        index = int(crop[0, 0, 0])
        scores = np.full(10, 0.10, dtype=np.float64)
        scores[index] = 0.99
        if index == 1:
            scores[0] = 0.96
            scores[1] = 0.94
        return SimpleNamespace(
            combined=scores,
            phash=scores,
            histogram=scores,
            pixel=scores,
            variants=np.asarray([str(hero_id) for hero_id in range(1, 11)]),
            variant_counts=np.ones(10, dtype=np.int32),
            crop_hash=f"{index + 1:016x}",
        )

    recognizer._score_crop = score_crop
    draft = recognizer.read(np.zeros((2, 2, 3), dtype=np.uint8))
    heroes = draft.radiant_hero_ids + draft.dire_hero_ids

    assert heroes == tuple(range(1, 11))
    assert len(set(heroes)) == 10
    assert draft.slot_diagnostics[1].best_hero_id == 2


def test_debug_capture_is_rate_limited_and_rolls_unlabeled_events_across_restarts(
    tmp_path: Path,
) -> None:
    image = np.full((24, 32, 3), 100, dtype=np.uint8)
    debug_root = tmp_path / "data" / "live_betting" / "vision_debug"
    sink = VisionDebugSink(debug_root, minimum_interval=30.0, maximum_events=1)

    assert sink.record(
        image,
        reason="draft_unconfirmed",
        layout_name="standard",
        diagnostics={"blocker": "draft_unconfirmed"},
    )
    assert not sink.record(
        image,
        reason="draft_unconfirmed",
        layout_name="standard",
        diagnostics={"blocker": "draft_unconfirmed"},
    )

    first_event = next(debug_root.rglob("metadata.json")).parent
    restarted = VisionDebugSink(debug_root, minimum_interval=0.0, maximum_events=1)
    assert restarted.record(
        image,
        reason="clock_unconfirmed",
        layout_name="standard",
        diagnostics={"blocker": "clock_unconfirmed"},
    )
    assert not first_event.exists()
    assert len(list(debug_root.rglob("metadata.json"))) == 1


def test_debug_capture_is_independent_per_series_and_map(tmp_path: Path) -> None:
    image = np.full((24, 32, 3), 100, dtype=np.uint8)
    debug_root = tmp_path / "data" / "live_betting" / "vision_debug"
    sink = VisionDebugSink(debug_root, minimum_interval=30.0, maximum_events=10)
    captured_at = datetime(2026, 8, 11, 4, 5, 6, tzinfo=timezone.utc)

    def record(match_id: str, map_number: int, frame_ref: str) -> bool:
        return sink.record(
            image,
            reason="draft_unconfirmed",
            layout_name="standard",
            diagnostics={"blocker": "draft_unconfirmed"},
            raybet_match_id=match_id,
            map_number=map_number,
            captured_at_utc=captured_at,
            source_frame_ref=frame_ref,
        )

    assert record("series-one", 1, "stream:series-one:map-1:1")
    assert not record("series-one", 1, "stream:series-one:map-1:2")
    assert record("series-one", 2, "stream:series-one:map-2:1")
    assert record("series-two", 1, "stream:series-two:map-1:1")

    metadata_paths = sorted(debug_root.rglob("metadata.json"))
    assert len(metadata_paths) == 3
    assert {
        path.parent.relative_to(debug_root).parts[:3]
        for path in metadata_paths
    } == {
        ("series", "series-one", "map_1"),
        ("series", "series-one", "map_2"),
        ("series", "series-two", "map_1"),
    }
    metadata = json.loads(
        next(
            path
            for path in metadata_paths
            if path.parent.relative_to(debug_root).parts[:3]
            == ("series", "series-one", "map_2")
        ).read_text(encoding="utf-8")
    )
    assert metadata["raybet_match_id"] == "series-one"
    assert metadata["map_number"] == 2
    assert metadata["captured_at"] == captured_at.timestamp()
    assert metadata["source_frame_ref"] == "stream:series-one:map-2:1"
    assert metadata["identity_status"] == "explicit_watcher_context"


def test_debug_capture_collision_suffix_stays_inside_its_map(tmp_path: Path) -> None:
    image = np.full((24, 32, 3), 100, dtype=np.uint8)
    debug_root = tmp_path / "vision_debug"
    sink = VisionDebugSink(debug_root, minimum_interval=0.0, maximum_events=10)
    captured_at = datetime(2026, 8, 11, 4, 5, 6, tzinfo=timezone.utc)
    payload = {
        "reason": "clock_unconfirmed",
        "layout_name": "standard",
        "diagnostics": {"blocker": "clock_unconfirmed"},
        "raybet_match_id": "series-one",
        "map_number": 2,
        "captured_at_utc": captured_at,
        "source_frame_ref": "stream:series-one:map-2:1",
    }

    assert sink.record(image, **payload)
    payload["source_frame_ref"] = "stream:series-one:map-2:2"
    assert sink.record(image, **payload)

    event_dirs = sorted(path.parent for path in debug_root.rglob("metadata.json"))
    assert len(event_dirs) == 2
    assert all(
        path.relative_to(debug_root).parts[:3]
        == ("series", "series-one", "map_2")
        for path in event_dirs
    )


def test_debug_capture_never_prunes_a_labeled_event(tmp_path: Path) -> None:
    image = np.full((24, 32, 3), 100, dtype=np.uint8)
    data_root = tmp_path / "data" / "live_betting"
    debug_root = data_root / "vision_debug"
    sink = VisionDebugSink(debug_root, minimum_interval=0.0, maximum_events=1)
    assert sink.record(
        image,
        reason="draft_unconfirmed",
        layout_name="standard",
        diagnostics={"blocker": "draft_unconfirmed"},
    )
    protected_event = next(debug_root.rglob("metadata.json")).parent
    relative = protected_event.relative_to(debug_root).as_posix()
    label_root = data_root / "vision_calibration" / "labels"
    label_root.mkdir(parents=True)
    (label_root / "label.json").write_text(
        json.dumps({"event_relative_path": relative}),
        encoding="utf-8",
    )

    restarted = VisionDebugSink(debug_root, minimum_interval=0.0, maximum_events=1)
    assert not restarted.record(
        image,
        reason="clock_unconfirmed",
        layout_name="standard",
        diagnostics={"blocker": "clock_unconfirmed"},
    )
    assert protected_event.exists()
    assert len(list(debug_root.rglob("metadata.json"))) == 1


def test_layout_switching_is_captured_with_tracker_diagnostics() -> None:
    recorded: dict[str, object] = {}

    class _DebugSink:
        def record(self, _: np.ndarray, **payload: object) -> bool:
            recorded.update(payload)
            return True

    reader = SimpleNamespace(
        debug_sink=_DebugSink(),
        last_frame_quality=None,
        last_layout_state=StableLayoutState(
            "epl_s39_live_1080p",
            0.40,
            "switching",
            challenger_name="wxc_gotf_2026_live_1080p",
            challenger_confidence=0.96,
            consecutive_support=2,
        ),
    )
    reading = SimpleNamespace(
        diagnostics=SimpleNamespace(blocker_code="ready", draft_failed_slots=()),
        selection=SimpleNamespace(layout=None, layout_name="epl_s39_live_1080p"),
    )

    StableHudReader._debug(
        reader,
        np.full((24, 32, 3), 100, dtype=np.uint8),
        reading,
    )

    assert recorded["reason"] == "layout_switching"
    diagnostics = recorded["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["layout_tracker"] == reader.last_layout_state


def test_stable_supervisor_targets_stable_watcher() -> None:
    from scripts.supervise_raybet_streams_stable import stable_watcher_command

    command = stable_watcher_command(
        "postgresql://example",
        "42",
        Path("output"),
        Path("evidence"),
    )
    assert Path(command[1]).name == "watch_raybet_stream_stable.py"
    assert "--refresh-url" in command


def test_stable_supervisor_installs_stable_command_before_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.supervise_raybet_streams_stable as stable_supervisor

    original = stable_supervisor.supervisor.watcher_command
    monkeypatch.setattr(stable_supervisor.supervisor, "watcher_command", original)

    def fake_main() -> int:
        command = stable_supervisor.supervisor.watcher_command(
            "postgresql://example",
            "42",
            Path("output"),
            Path("evidence"),
        )
        assert Path(command[1]).name == "watch_raybet_stream_stable.py"
        return 17

    monkeypatch.setattr(stable_supervisor.supervisor, "main", fake_main)
    assert stable_supervisor.main() == 17
