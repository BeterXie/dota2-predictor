from __future__ import annotations

import copy
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_intelligence.storage import IntelligenceStorage
from live_betting.storage import LiveBettingStore
from scripts.accept_strict_live_mapping import main


EVENT_ID = "pgl-wallachia-s8-2026"
SCHEDULE_RAW = "2026-04-20 12:00:00"


def evidence() -> dict[str, object]:
    return {
        "kind": "manual_cross_source_review",
        "raybet_url": "https://example.invalid/raybet/match-1",
        "official_event_url": "https://example.invalid/event",
        "tournament": {
            "raybet_name": "PGL Wallachia Season 8",
            "event_name": "PGL Wallachia Season 8",
        },
        "schedule": {
            "raybet_scheduled_at": SCHEDULE_RAW,
            "utc_offset_minutes": 480,
            "scheduled_at_utc": "2026-04-20T04:00:00+00:00",
            "timezone_evidence": "audited RayBet UTC+08 display contract",
        },
        "stage": {
            "scope": "main_event",
            "source_url": "https://example.invalid/event/stage",
        },
        "team_crosswalk": {
            "team_one": {
                "raybet_team_id": 501,
                "raybet_team_name": "Alpha",
                "canonical_team_id": 101,
                "canonical_team_name": "Alpha Canonical",
                "source_url": "https://example.invalid/teams/alpha",
            },
            "team_two": {
                "raybet_team_id": 502,
                "raybet_team_name": "Beta",
                "canonical_team_id": 202,
                "canonical_team_name": "Beta Canonical",
                "source_url": "https://example.invalid/teams/beta",
            },
        },
    }


class AcceptStrictLiveMappingCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database = root / "strict.db"
        self.evidence_path = root / "evidence.json"
        self.write_evidence(evidence())

        with IntelligenceStorage(self.database) as storage:
            storage.init_schema()
        with LiveBettingStore(self.database) as store:
            store.init_schema()
            store.connection.execute(
                """CREATE TABLE IF NOT EXISTS teams (
                       team_id INTEGER PRIMARY KEY,
                       name TEXT,
                       tag TEXT,
                       logo_url TEXT,
                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            store.connection.executemany(
                "INSERT INTO teams(team_id, name) VALUES (?, ?)",
                ((101, "Alpha Canonical"), (202, "Beta Canonical")),
            )
            store.upsert_raybet_match(
                {
                    "id": "match-1",
                    "game_id": 151,
                    "tournament_name": "PGL Wallachia Season 8",
                    "start_time": SCHEDULE_RAW,
                    "round": "bo3",
                    "team": [
                        {"pos": 1, "team_id": 501, "team_name": "Alpha"},
                        {"pos": 2, "team_id": 502, "team_name": "Beta"},
                    ],
                },
                datetime.now(timezone.utc) - timedelta(minutes=1),
            )
            store.connection.commit()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write_evidence(self, value: object) -> None:
        self.evidence_path.write_text(json.dumps(value), encoding="utf-8")

    def arguments(self, *map_numbers: int) -> list[str]:
        values = [
            "--database",
            str(self.database),
            "--evidence",
            str(self.evidence_path),
            "--raybet-match-id",
            "match-1",
            "--raybet-team-one-id",
            "501",
            "--raybet-team-two-id",
            "502",
            "--canonical-team-one-id",
            "101",
            "--canonical-team-two-id",
            "202",
            "--event-id",
            EVENT_ID,
            "--source",
            "manual_event_team_audit",
            "--actor",
            "operator-a",
        ]
        for map_number in map_numbers:
            values.extend(("--map-number", str(map_number)))
        return values

    def invoke(self, *map_numbers: int) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(self.arguments(*map_numbers))
        return result, stdout.getvalue(), stderr.getvalue()

    def rows(self, query: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(query).fetchall()
        finally:
            connection.close()

    def test_accepts_multiple_maps_atomically_with_current_utc_time(self) -> None:
        before = datetime.now(timezone.utc)
        result, output, error = self.invoke(1, 2, 3)
        after = datetime.now(timezone.utc)

        self.assertEqual(result, 0)
        self.assertEqual(error, "")
        report = json.loads(output)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["map_numbers"], [1, 2, 3])
        self.assertEqual(len(report["mapping_ids"]), 3)
        self.assertNotIn("raybet_url", output)
        self.assertNotIn("manual_cross_source_review", output)

        mappings = self.rows(
            "SELECT map_number, accepted_at FROM strict_live_map_mappings ORDER BY map_number"
        )
        self.assertEqual([row["map_number"] for row in mappings], [1, 2, 3])
        accepted_at = {datetime.fromisoformat(row["accepted_at"]) for row in mappings}
        self.assertEqual(len(accepted_at), 1)
        self.assertTrue(before <= accepted_at.pop() <= after)

    def test_idempotent_rerun_keeps_mappings_and_appends_audit(self) -> None:
        first_result, first_output, _ = self.invoke(1, 2, 3)
        second_result, second_output, second_error = self.invoke(1, 2, 3)

        self.assertEqual((first_result, second_result), (0, 0))
        self.assertEqual(second_error, "")
        self.assertEqual(
            json.loads(second_output)["mapping_ids"],
            json.loads(first_output)["mapping_ids"],
        )
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS count FROM strict_live_map_mappings")[0][
                "count"
            ],
            3,
        )
        self.assertEqual(
            [
                (row["map_number"], row["decision"], row["reason"])
                for row in self.rows(
                    """SELECT map_number, decision, reason
                       FROM strict_live_map_mapping_audit ORDER BY audit_id"""
                )
            ],
            [
                (1, "accepted", "manual_exact_mapping_accepted"),
                (2, "accepted", "manual_exact_mapping_accepted"),
                (3, "accepted", "manual_exact_mapping_accepted"),
                (1, "idempotent", "same_value_already_accepted"),
                (2, "idempotent", "same_value_already_accepted"),
                (3, "idempotent", "same_value_already_accepted"),
            ],
        )

    def test_invalid_evidence_fails_closed_without_partial_batch(self) -> None:
        invalid = copy.deepcopy(evidence())
        del invalid["team_crosswalk"]
        self.write_evidence(invalid)

        result, output, error = self.invoke(1, 2, 3)

        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertEqual(
            json.loads(error),
            {
                "reason": "team_crosswalk_evidence_missing",
                "status": "rejected",
            },
        )
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS count FROM strict_live_map_mappings")[0][
                "count"
            ],
            0,
        )
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS count FROM strict_live_map_mapping_audit")[0][
                "count"
            ],
            0,
        )

    def test_later_invalid_map_rolls_back_earlier_maps(self) -> None:
        result, output, error = self.invoke(1, 4)

        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(error)["reason"], "map_number_exceeds_best_of")
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS count FROM strict_live_map_mappings")[0][
                "count"
            ],
            0,
        )
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS count FROM strict_live_map_mapping_audit")[0][
                "count"
            ],
            0,
        )


if __name__ == "__main__":
    unittest.main()
