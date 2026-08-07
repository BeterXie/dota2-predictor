from __future__ import annotations

import numpy as np
from pathlib import Path

from vision.frame_quality import FrameQualityTracker
from vision.layout_tracker import LayoutTracker
from vision.hero_recognizer import DraftReading, HeroSlotDiagnostic
from vision.stable_runtime import StableDraftTracker


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


def test_draft_tracker_accepts_progressing_clock_on_same_stream_identity() -> None:
    tracker = StableDraftTracker(confirmations=2)
    draft = DraftReading(
        (1, 2, 3, 4, 5),
        (6, 7, 8, 9, 10),
        0.95,
    )

    assert tracker.update(
        draft,
        observed_at=0.0,
        source_frame_hash="same-stream",
        game_clock_seconds=100,
    ) is None
    assert tracker.update(
        draft,
        observed_at=3.0,
        source_frame_hash="same-stream",
        game_clock_seconds=103,
    ) is None
    locked = tracker.update(
        draft,
        observed_at=6.0,
        source_frame_hash="same-stream",
        game_clock_seconds=106,
    )
    assert locked is not None
    assert locked.radiant_hero_ids == (1, 2, 3, 4, 5)
    assert locked.dire_hero_ids == (6, 7, 8, 9, 10)


def test_draft_tracker_static_crop_can_reach_default_support() -> None:
    tracker = StableDraftTracker(
        minimum_evidence_interval=1.0,
        minimum_lock_interval=5.0,
    )
    diagnostics = tuple(
        HeroSlotDiagnostic(
            "radiant" if index < 5 else "dire",
            index % 5 + 1,
            index + 1,
            0.90,
            0.70,
            0.20,
            True,
            "accepted",
            crop_hash=f"{index + 1:016x}",
        )
        for index in range(10)
    )
    reading = DraftReading((), (), 0.90, diagnostics)

    for step in range(5):
        assert tracker.update(
            reading,
            observed_at=float(step),
            source_frame_hash=f"frame-{step}",
            game_clock_seconds=100 + step,
        ) is None

    locked = None
    for step in range(5, 11):
        locked = tracker.update(
            reading,
            observed_at=float(step),
            source_frame_hash=f"frame-{step}",
            game_clock_seconds=100 + step,
        )
        if locked is not None:
            break
    assert locked is not None
    assert locked.radiant_hero_ids == (1, 2, 3, 4, 5)
    assert locked.dire_hero_ids == (6, 7, 8, 9, 10)


def test_stable_supervisor_targets_stable_watcher() -> None:
    from scripts.supervise_raybet_streams_stable import stable_watcher_command

    command = stable_watcher_command(
        "postgresql://example",
        "42",
        Path("/tmp/output"),
        Path("/tmp/evidence"),
    )
    assert command[1].endswith("scripts/watch_raybet_stream_stable.py")
    assert "--refresh-url" in command
