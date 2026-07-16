from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fetch.db import Database


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
        "league": {"leagueid": 19_543, "name": "Original League", "tier": "premium"},
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
        "chat": [{"time": 100, "player_slot": 0, "type": "chat", "key": "gg"}],
    }


def seed_hero(connection: sqlite3.Connection, hero_id: int) -> None:
    connection.execute(
        "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
        (hero_id, f"Hero {hero_id}"),
    )
    connection.commit()


def match_snapshot(connection: sqlite3.Connection, match_id: int) -> dict:
    child_tables = (
        "match_players",
        "picks_bans",
        "gold_advantage",
        "xp_advantage",
        "objectives",
        "chat",
        "teamfights",
    )
    return {
        "match": connection.execute(
            "SELECT duration, radiant_score, dire_score FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone(),
        "children": {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE match_id = ?", (match_id,)
            ).fetchone()[0]
            for table in child_tables
        },
        "teamfight_players": connection.execute(
            """SELECT COUNT(*) FROM teamfight_players tfp
               JOIN teamfights tf ON tf.id = tfp.teamfight_id
               WHERE tf.match_id = ?""",
            (match_id,),
        ).fetchone()[0],
        "teams": connection.execute(
            "SELECT team_id, name FROM teams ORDER BY team_id"
        ).fetchall(),
        "league": connection.execute(
            "SELECT leagueid, name FROM leagues WHERE leagueid = 19543"
        ).fetchone(),
    }


class FetchDatabaseTransactionTests(unittest.TestCase):
    def test_commit_false_keeps_every_match_write_in_external_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matches.db"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys=ON")
            database = Database(connection=connection)
            database.init_db()
            seed_hero(connection, 1)
            observer = sqlite3.connect(path)
            try:
                connection.execute("BEGIN")
                database.insert_match(completed_match(), commit=False)

                self.assertTrue(connection.in_transaction)
                self.assertEqual(
                    observer.execute(
                        "SELECT COUNT(*) FROM matches WHERE match_id = 8001"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    observer.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    observer.execute("SELECT COUNT(*) FROM leagues").fetchone()[0],
                    0,
                )

                connection.rollback()
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
                    0,
                )
            finally:
                observer.close()
                connection.close()

    def test_fault_in_child_write_allows_outer_rollback_of_full_replacement(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys=ON")
        database = Database(connection=connection)
        database.init_db()
        seed_hero(connection, 1)
        original = completed_match()
        database.insert_match(original)
        before = match_snapshot(connection, original["match_id"])

        replacement = copy.deepcopy(original)
        replacement["duration"] = 2_400
        replacement["radiant_score"] = 45
        replacement["radiant_team"]["name"] = "Radiant Replacement"
        replacement["league"]["name"] = "Replacement League"
        replacement["players"][0]["hero_id"] = 999_999

        connection.execute("BEGIN")
        with self.assertRaises(sqlite3.IntegrityError):
            database.insert_match(replacement, commit=False)
        connection.rollback()

        self.assertEqual(match_snapshot(connection, original["match_id"]), before)
        connection.close()

    def test_default_calls_still_commit_and_injected_connection_is_not_owned(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys=ON")
        database = Database(connection=connection)
        configured = database.connect()
        self.assertEqual(configured.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(configured.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        database.init_db()
        seed_hero(connection, 1)

        database.insert_team(
            {"team_id": 303, "name": "Standalone", "tag": "ONE", "logo_url": None}
        )
        self.assertFalse(connection.in_transaction)
        database.insert_league({"leagueid": 404, "name": "Standalone", "tier": "x"})
        self.assertFalse(connection.in_transaction)
        database.insert_match(completed_match())
        self.assertFalse(connection.in_transaction)

        database.close()
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        connection.close()

    def test_migration_does_not_hide_lock_or_io_errors(self) -> None:
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("database is locked")

        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            Database._migrate(connection)


if __name__ == "__main__":
    unittest.main()
