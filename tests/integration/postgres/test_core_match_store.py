from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError

from database.engine import build_engine, require_database_url
from fetch.postgres_store import CoreMatchStore


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_store_test_{uuid4().hex}"
    admin_engine = create_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    test_url = base_url.set(database=database_name)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        test_url.render_as_string(hide_password=False),
    )
    command.upgrade(config, "head")

    engine = build_engine(test_url.render_as_string(hide_password=False))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin_engine.dispose()


def completed_match(match_id: int = 8_001) -> dict:
    return {
        "match_id": match_id,
        "radiant_team_id": 101,
        "dire_team_id": 202,
        "radiant_win": True,
        "duration": 1_800,
        "game_mode": 2,
        "lobby_type": 1,
        "start_time": 1_789_000_000,
        "first_blood_time": 120,
        "leagueid": 19_543,
        "series_id": 77,
        "series_type": 2,
        "patch": 60,
        "region": 3,
        "radiant_score": 31,
        "dire_score": 18,
        "radiant_team": {
            "team_id": 101,
            "name": "Radiant Original",
            "tag": "RAD",
            "logo_url": "https://example.invalid/radiant.png",
        },
        "dire_team": {
            "team_id": 202,
            "name": "Dire Original",
            "tag": "DIRE",
            "logo_url": "https://example.invalid/dire.png",
        },
        "league": {
            "leagueid": 19_543,
            "name": "Original League",
            "tier": "premium",
        },
        "players": [
            {
                "account_id": 1_001,
                "player_slot": 0,
                "hero_id": 1,
                "kills": 5,
                "deaths": 2,
                "assists": 10,
                "gold_per_min": 600,
                "xp_per_min": 700,
                "net_worth": 18_000,
                "last_hits": 250,
                "denies": 8,
                "hero_damage": 20_000,
                "hero_healing": 0,
                "tower_damage": 3_000,
                "level": 25,
            }
        ],
        "picks_bans": [
            {"hero_id": 1, "is_pick": True, "team": 0, "order": 0}
        ],
        "teamfights": [
            {
                "start": 600,
                "end": 630,
                "last_death": 625,
                "deaths": 1,
                "players": [
                    {
                        "player_slot": 0,
                        "deaths": 0,
                        "buybacks": 0,
                        "damage": 1_000,
                        "healing": 0,
                        "gold_delta": 500,
                        "xp_delta": 600,
                        "kills": 1,
                    }
                ],
            }
        ],
        "radiant_gold_adv": [0, 500],
        "radiant_xp_adv": [0, 300],
        "objectives": [
            {
                "time": 700,
                "type": "CHAT_MESSAGE_TOWER_KILL",
                "unit": "npc_dota_goodguys_tower1_mid",
                "key": "tower",
                "player_slot": 0,
            }
        ],
        "chat": [
            {"time": 100, "player_slot": 0, "type": "chat", "key": "gg"}
        ],
    }


def test_store_upserts_heroes_and_replaces_match_children(
    postgres_engine: Engine,
) -> None:
    store = CoreMatchStore(engine=postgres_engine)
    store.insert_heroes(
        [
            {
                "id": 1,
                "name": "npc_dota_hero_antimage",
                "localized_name": "Anti-Mage",
                "primary_attr": "agi",
                "attack_type": "Melee",
                "roles": ["Carry"],
            }
        ]
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            text("UPDATE heroes SET pro_pick = 7 WHERE hero_id = 1")
        )
    store.insert_heroes(
        [
            {
                "id": 1,
                "name": "npc_dota_hero_antimage",
                "localized_name": "Anti-Mage Updated",
                "primary_attr": "agi",
                "attack_type": "Melee",
                "roles": ["Carry", "Escape"],
            }
        ]
    )

    match = completed_match()
    store.insert_match(match)
    assert store.hero_count() == 1
    assert store.is_fetched(match["match_id"])

    replacement = copy.deepcopy(match)
    replacement["duration"] = 2_400
    replacement["radiant_score"] = 45
    replacement["players"][0]["kills"] = 12
    replacement["radiant_gold_adv"] = [0, 750, 1_200]
    store.insert_match(replacement)

    with postgres_engine.connect() as connection:
        hero = connection.execute(
            text(
                "SELECT localized_name, pro_pick FROM heroes WHERE hero_id = 1"
            )
        ).one()
        persisted_match = connection.execute(
            text(
                "SELECT duration, radiant_score FROM matches WHERE match_id = :match_id"
            ),
            {"match_id": match["match_id"]},
        ).one()
        counts = {
            table: connection.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE match_id = :match_id"),
                {"match_id": match["match_id"]},
            ).scalar_one()
            for table in (
                "match_players",
                "picks_bans",
                "teamfights",
                "objectives",
                "chat",
            )
        }
        gold_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM gold_advantage WHERE match_id = :match_id"
            ),
            {"match_id": match["match_id"]},
        ).scalar_one()
        teamfight_player_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM teamfight_players AS tfp
                JOIN teamfights AS tf ON tf.id = tfp.teamfight_id
                WHERE tf.match_id = :match_id
                """
            ),
            {"match_id": match["match_id"]},
        ).scalar_one()

    assert hero == ("Anti-Mage Updated", 7)
    assert persisted_match == (2_400, 45)
    assert counts == {
        "match_players": 1,
        "picks_bans": 1,
        "teamfights": 1,
        "objectives": 1,
        "chat": 1,
    }
    assert gold_count == 3
    assert teamfight_player_count == 1


def test_failed_child_write_rolls_back_full_replacement(
    postgres_engine: Engine,
) -> None:
    store = CoreMatchStore(engine=postgres_engine)
    store.insert_heroes(
        [
            {
                "id": 1,
                "name": "npc_dota_hero_antimage",
                "localized_name": "Anti-Mage",
            }
        ]
    )
    original = completed_match()
    store.insert_match(original)

    replacement = copy.deepcopy(original)
    replacement["duration"] = 2_400
    replacement["radiant_team"]["name"] = "Uncommitted Replacement"
    replacement["players"][0]["hero_id"] = 999_999

    with pytest.raises(IntegrityError):
        store.insert_match(replacement)

    with postgres_engine.connect() as connection:
        duration = connection.execute(
            text("SELECT duration FROM matches WHERE match_id = :match_id"),
            {"match_id": original["match_id"]},
        ).scalar_one()
        team_name = connection.execute(
            text("SELECT name FROM teams WHERE team_id = 101")
        ).scalar_one()
        player_hero = connection.execute(
            text(
                "SELECT hero_id FROM match_players WHERE match_id = :match_id"
            ),
            {"match_id": original["match_id"]},
        ).scalar_one()

    assert duration == 1_800
    assert team_name == "Radiant Original"
    assert player_hero == 1
