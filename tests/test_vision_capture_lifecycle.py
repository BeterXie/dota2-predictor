from __future__ import annotations

from vision.capture_lifecycle import CaptureLifecycle


def _observe(
    tracker: CaptureLifecycle,
    captured_at: float,
    *,
    state: str,
    clock: int | None = None,
    scoreboard: bool = False,
    map_number: int | None = None,
) -> tuple[str, ...]:
    triggers = tracker.observe(
        captured_at=captured_at,
        screen_state=state,
        screen_confidence=0.95 if state in {"draft", "game"} else 0.3,
        layout_supported=True,
        replay_gate_status="live" if state == "game" else "untrusted",
        game_clock_seconds=clock,
        scoreboard_ready=scoreboard,
        map_number=map_number,
    )
    return tuple(item.event for item in triggers)


def _confirm_draft(tracker: CaptureLifecycle) -> None:
    assert _observe(tracker, 0.0, state="draft") == ()
    assert _observe(tracker, 1.0, state="draft") == ()
    assert _observe(tracker, 2.0, state="unknown") == ()
    assert _observe(tracker, 3.0, state="draft") == ()
    assert _observe(tracker, 4.0, state="draft") == ("draft_started",)


def test_single_draft_frame_does_not_start_capture() -> None:
    tracker = CaptureLifecycle()

    assert _observe(tracker, 10.0, state="draft") == ()
    assert tracker.phase == "draft_candidate"


def test_four_of_five_draft_frames_start_once() -> None:
    tracker = CaptureLifecycle()

    _confirm_draft(tracker)

    assert tracker.phase == "draft_started"
    assert _observe(tracker, 5.0, state="draft") == ()


def test_game_start_is_confirmed_and_map_two_draft_cannot_restart_bp() -> None:
    tracker = CaptureLifecycle()
    _confirm_draft(tracker)

    assert _observe(
        tracker, 5.0, state="game", clock=0, scoreboard=True, map_number=1
    ) == ()
    assert _observe(
        tracker, 6.0, state="game", clock=1, scoreboard=True, map_number=1
    ) == ()
    assert _observe(
        tracker, 7.0, state="game", clock=2, scoreboard=True, map_number=1
    ) == ("game_started",)
    assert all(
        _observe(tracker, time, state="draft") == ()
        for time in (8.0, 9.0, 10.0, 11.0, 12.0)
    )


def test_periodic_capture_is_strict_and_does_not_backfill() -> None:
    tracker = CaptureLifecycle(evidence_interval=30.0)
    _confirm_draft(tracker)

    assert _observe(tracker, 33.9, state="unknown") == ()
    assert _observe(tracker, 34.0, state="unknown") == ("periodic_30s",)
    assert _observe(tracker, 100.0, state="unknown") == ("periodic_30s",)
    assert _observe(tracker, 129.9, state="unknown") == ()
    assert _observe(tracker, 130.0, state="unknown") == ("periodic_30s",)


def test_completed_match_emits_final_frame_and_closes_after_grace() -> None:
    tracker = CaptureLifecycle(end_grace_seconds=90.0)
    _confirm_draft(tracker)

    tracker.mark_provider_complete(20.0)

    assert tracker.phase == "ended_grace"
    assert _observe(tracker, 21.0, state="unknown") == ("ended_final",)
    assert not tracker.should_close(109.9)
    assert tracker.should_close(110.0)


def test_manifest_restore_prevents_duplicate_lifecycle_events() -> None:
    tracker = CaptureLifecycle()
    tracker.restore(
        [
            {
                "event": "game_started",
                "captured_at": "2026-08-09T12:00:00+00:00",
            }
        ]
    )

    assert tracker.phase == "game_started"
    assert all(
        _observe(tracker, time, state="draft") == ()
        for time in (1.0, 2.0, 3.0, 4.0, 5.0)
    )
