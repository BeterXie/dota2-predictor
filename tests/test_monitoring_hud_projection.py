from __future__ import annotations

from datetime import datetime, timedelta, timezone

from contracts.live_observation import ComebackState, LiveObservation
from web.monitoring import _latest_hud_observations


NOW = datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc)


def test_latest_hud_observation_projects_available_hud_without_exact_networth(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VISION_OBSERVATION_DIR", str(tmp_path))
    observation = LiveObservation(
        raybet_match_id="match-1",
        map_number=2,
        captured_at_utc=NOW,
        game_clock_seconds=1522,
        is_paused=True,
        radiant_hero_ids=[],
        dire_hero_ids=[],
        clock_confidence=0.99,
        draft_confidence=0.0,
        source_frame_ref="vision-frame:sha256:" + "c" * 64,
        source_frame_sha256="c" * 64,
        source_frame_bytes=100,
        source_frame_path=str(tmp_path / "frame-c.jpg"),
        screen_state="game",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.99,
            radiant_kills=20,
            dire_kills=3,
            radiant_net_worth=None,
            dire_net_worth=None,
            net_worth_advantage_side="radiant",
            net_worth_advantage_min=22000,
            net_worth_advantage_max=22999,
            unavailable_reason=None,
        ),
    )
    (tmp_path / "match-1.jsonl").write_text(
        observation.model_dump_json() + "\n",
        encoding="utf-8",
    )

    points = _latest_hud_observations(
        "match-1",
        now=NOW + timedelta(seconds=1),
        valid_vision_points=[
            {
                "map_number": 2,
                "captured_at": NOW.isoformat(),
                "source_frame_ref": observation.source_frame_ref,
            }
        ],
    )
    point = points[2]

    assert point is not None
    assert point["status"] == "available"
    assert point["source"] == "vision_hud"
    assert point["observation_file"] == "match-1.jsonl"
    assert point["source_frame_ref"] == "vision-frame:sha256:" + "c" * 64
    assert point["frame_url"] is not None
    assert point["map_number"] == 2
    assert point["game_clock_seconds"] == 1522
    assert point["is_paused"] is True
    assert point["radiant_kills"] == 20
    assert point["dire_kills"] == 3
    assert point["net_worth_advantage_side"] == "radiant"
    assert point["net_worth_advantage_min"] == 22000
    assert point["net_worth_advantage_max"] == 22999
    assert point["radiant_net_worth"] is None
    assert point["dire_net_worth"] is None
    assert point["draft_confirmed"] is False
    assert point["radiant_hero_ids"] == []
    assert point["dire_hero_ids"] == []


def test_each_map_uses_its_own_latest_registered_hud(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VISION_OBSERVATION_DIR", str(tmp_path))
    map_one = LiveObservation(
        raybet_match_id="match-2",
        map_number=1,
        captured_at_utc=NOW - timedelta(hours=1),
        game_clock_seconds=2400,
        is_paused=False,
        clock_confidence=0.99,
        source_frame_ref="vision-frame:sha256:" + "a" * 64,
        source_frame_sha256="a" * 64,
        source_frame_bytes=100,
        source_frame_path=str(tmp_path / "frame-a.jpg"),
        screen_state="game",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.99,
            radiant_kills=42,
            dire_kills=30,
            net_worth_advantage_side="radiant",
            net_worth_advantage_min=8_000,
            net_worth_advantage_max=8_999,
            unavailable_reason=None,
        ),
    )
    map_two = LiveObservation(
        raybet_match_id="match-2",
        map_number=2,
        captured_at_utc=NOW,
        game_clock_seconds=1800,
        is_paused=False,
        clock_confidence=0.99,
        source_frame_ref="vision-frame:sha256:" + "b" * 64,
        source_frame_sha256="b" * 64,
        source_frame_bytes=100,
        source_frame_path=str(tmp_path / "frame-b.jpg"),
        screen_state="game",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.99,
            radiant_kills=30,
            dire_kills=20,
            net_worth_advantage_side="dire",
            net_worth_advantage_min=4_000,
            net_worth_advantage_max=4_999,
            unavailable_reason=None,
        ),
    )
    unregistered = LiveObservation(
        raybet_match_id="match-2",
        map_number=2,
        captured_at_utc=NOW + timedelta(seconds=1),
        game_clock_seconds=1801,
        clock_confidence=0.99,
        source_frame_ref="stream:match-2:unregistered",
        screen_state="game",
        comeback_state=ComebackState(
            status="available",
            source="vision_hud",
            confidence=0.99,
            radiant_kills=99,
            dire_kills=99,
            net_worth_advantage_side="dire",
            net_worth_advantage_min=99_000,
            net_worth_advantage_max=99_999,
            unavailable_reason=None,
        ),
    )
    later_unregistered = [
        LiveObservation(
            raybet_match_id="match-2",
            map_number=2,
            captured_at_utc=NOW + timedelta(seconds=sequence),
            game_clock_seconds=1801 + sequence,
            clock_confidence=0.99,
            source_frame_ref=f"stream:match-2:unregistered:{sequence}",
            screen_state="game",
            comeback_state=unregistered.comeback_state,
        ).model_dump_json()
        for sequence in range(2, 42)
    ]
    (tmp_path / "match-2.jsonl").write_text(
        map_one.model_dump_json()
        + "\n"
        + map_two.model_dump_json()
        + "\n"
        + unregistered.model_dump_json()
        + "\n"
        + "\n".join(later_unregistered)
        + "\n",
        encoding="utf-8",
    )

    points = _latest_hud_observations(
        "match-2",
        now=NOW + timedelta(seconds=45),
        valid_vision_points=[
            {
                "map_number": 1,
                "captured_at": map_one.captured_at_utc.isoformat(),
                "source_frame_ref": map_one.source_frame_ref,
            },
            {
                "map_number": 2,
                "captured_at": map_two.captured_at_utc.isoformat(),
                "source_frame_ref": map_two.source_frame_ref,
            },
        ],
        maximum_map_number=2,
    )

    assert set(points) == {1, 2}
    assert points[1]["radiant_kills"] == 42
    assert points[2]["status"] == "available"
    assert points[2]["map_number"] == 2
    assert points[2]["radiant_kills"] == 30
    assert points[2]["dire_kills"] == 20
    assert points[2]["frame_url"] is not None
