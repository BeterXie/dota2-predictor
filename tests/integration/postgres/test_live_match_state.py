from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import DBAPIError

from live_betting.live_match_state import (
    DraftSlotInput,
    append_live_game_snapshot,
    latest_live_draft_mapping,
    live_draft_context,
    live_game_snapshots,
    save_live_draft_mapping,
)
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _slots(*, last_hero_id: int = 10) -> list[DraftSlotInput]:
    return [
        DraftSlotInput(
            team_id=11 if index < 5 else 22,
            side="radiant" if index < 5 else "dire",
            position=(index % 5) + 1,
            hero_id=last_hero_id if index == 9 else index + 1,
            player_id=100 + index,
        )
        for index in range(10)
    ]


def test_manual_draft_versions_and_dynamic_snapshots_are_append_only(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    first = save_live_draft_mapping(
        store.connection,
        raybet_match_id="live-1",
        map_number=1,
        slots=_slots(),
        is_locked=False,
        actor="operator",
        created_at=NOW,
    )
    second = save_live_draft_mapping(
        store.connection,
        raybet_match_id="live-1",
        map_number=1,
        slots=_slots(last_hero_id=11),
        is_locked=True,
        actor="operator",
        created_at=NOW + timedelta(seconds=1),
    )

    assert first["version"] == 1
    assert first["is_locked"] is False
    assert second["version"] == 2
    assert second["is_locked"] is True
    assert second["slots"][-1]["hero_id"] == 11
    assert latest_live_draft_mapping(
        store.connection,
        "live-1",
        map_number=1,
    ) == second

    manual = append_live_game_snapshot(
        store.connection,
        raybet_match_id="live-1",
        map_number=1,
        game_time_seconds=600,
        radiant_networth=20_000,
        dire_networth=19_000,
        radiant_kills=5,
        dire_kills=4,
        vision_confidence=1.0,
        screenshot_path=None,
        source="manual_correction",
        captured_at=NOW,
        actor="operator",
    )
    vision = append_live_game_snapshot(
        store.connection,
        raybet_match_id="live-1",
        map_number=1,
        game_time_seconds=610,
        radiant_networth=20_500,
        dire_networth=19_400,
        radiant_kills=5,
        dire_kills=4,
        vision_confidence=0.95,
        screenshot_path="vision-frame:sha256:" + "a" * 64,
        source="vision",
        captured_at=NOW + timedelta(seconds=10),
    )

    assert manual["networth_lead"] == 1000
    assert vision["networth_lead"] == 1100
    assert [row["snapshot_id"] for row in live_game_snapshots(
        store.connection,
        "live-1",
    )] == [manual["snapshot_id"], vision["snapshot_id"]]

    with pytest.raises(ValueError, match="moved backwards"):
        append_live_game_snapshot(
            store.connection,
            raybet_match_id="live-1",
            map_number=1,
            game_time_seconds=500,
            radiant_networth=20_600,
            dire_networth=19_500,
            radiant_kills=5,
            dire_kills=4,
            vision_confidence=0.95,
            screenshot_path=None,
            source="vision",
            captured_at=NOW + timedelta(seconds=20),
        )

    with pytest.raises(DBAPIError, match="append-only"):
        with store.connection.transaction():
            store.connection.execute(
                "UPDATE live_game_snapshots SET game_time_seconds=601 "
                "WHERE snapshot_id=?",
                (manual["snapshot_id"],),
            )
    store.close()


def test_manual_draft_rejects_duplicate_heroes(postgres_engine, tmp_path) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    invalid = _slots()
    invalid[-1] = DraftSlotInput(22, "dire", 5, 1, 109)

    with pytest.raises(ValueError, match="heroes must be globally unique"):
        save_live_draft_mapping(
            store.connection,
            raybet_match_id="live-2",
            map_number=1,
            slots=invalid,
            is_locked=True,
            actor="operator",
        )
    store.close()


def test_live_draft_context_resolves_teams_and_players_without_manual_ids(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    with store.connection.transaction():
        store.connection.executemany(
            "INSERT INTO teams (team_id, name, tag) VALUES (?, ?, ?)",
            [(11, "Alpha Gaming", "AG"), (22, "Beta", "B")],
        )
        store.connection.execute(
            """INSERT INTO raybet_matches
               (raybet_match_id, team_one, team_two, best_of, status,
                raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "live-context",
                "Alpha",
                "Beta",
                3,
                "2",
                '{"team":['
                '{"pos":1,"team_name":"Alpha","team_short_name":"Alpha Gaming"},'
                '{"pos":2,"team_name":"Beta","team_short_name":"Beta"}'
                "]}",
                NOW.isoformat(),
            ),
        )
        store.connection.executemany(
            """INSERT INTO matches
               (match_id, radiant_team_id, dire_team_id, start_time)
               VALUES (?, ?, ?, ?)""",
            [(101, 11, 99, 100), (202, 98, 22, 200)],
        )
        store.connection.executemany(
            """INSERT INTO match_players
               (match_id, account_id, player_slot, is_radiant, team_id)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (101, 1000 + position, position - 1, True, 11)
                for position in range(1, 6)
            ]
            + [
                (202, 2000 + position, 127 + position, False, 22)
                for position in range(1, 6)
            ],
        )

    context = live_draft_context(
        store.connection,
        "live-context",
        as_of=NOW,
    )

    assert context is not None
    assert context["status"] == "ready"
    assert context["source"] == "raybet_exact_name"
    assert [team["team_id"] for team in context["teams"]] == [11, 22]
    assert [
        player["player_id"] for player in context["teams"][0]["players"]
    ] == [1001, 1002, 1003, 1004, 1005]
    assert [
        player["position"] for player in context["teams"][1]["players"]
    ] == [1, 2, 3, 4, 5]
    store.close()
