from datetime import datetime, timezone

from web.monitoring import _vision_readiness


def test_upcoming_vision_readiness_exposes_watch_window() -> None:
    readiness = _vision_readiness(
        None,
        provider_status="1",
        scheduled_at="2026-08-11 20:00:00",
        now=datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert readiness == {
        "status": "missing",
        "observed_at": None,
        "age_seconds": None,
        "reason": "waiting_for_watch_window",
        "watch_starts_at": "2026-08-11T11:30:00+00:00",
    }


def test_upcoming_vision_readiness_marks_probe_window_open() -> None:
    readiness = _vision_readiness(
        None,
        provider_status="1",
        scheduled_at="2026-08-11 20:00:00",
        now=datetime(2026, 8, 11, 11, 30, tzinfo=timezone.utc),
    )

    assert readiness["reason"] == "stream_probe_pending"
    assert readiness["watch_starts_at"] == "2026-08-11T11:30:00+00:00"
