from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from event_intelligence.incremental import CurrentDerivedScopes
from web import intelligence, queries
from web.routers.monitor import router


SCHEMA = """
CREATE TABLE raybet_matches (
    raybet_match_id TEXT PRIMARY KEY,
    team_one TEXT NOT NULL DEFAULT 'Alpha',
    team_two TEXT NOT NULL DEFAULT 'Beta',
    raw_json TEXT NOT NULL DEFAULT
        '{"team":[{"pos":1,"team_name":"Alpha"},{"pos":2,"team_name":"Beta"}]}'
);
CREATE TABLE odds_snapshots (
    id INTEGER PRIMARY KEY,
    raybet_match_id TEXT,
    received_at TEXT,
    price REAL,
    status TEXT,
    period TEXT,
    side TEXT,
    odds_id TEXT,
    odds_group_id TEXT,
    market_type TEXT,
    supported INTEGER
);
CREATE TABLE odds_alignments (
    odds_snapshot_id INTEGER,
    raybet_match_id TEXT,
    map_number INTEGER,
    game_clock_seconds INTEGER,
    observation_captured_at TEXT,
    method TEXT,
    lag_seconds REAL,
    usable INTEGER,
    reason TEXT
);
CREATE TABLE settlement_reconciliations (
    raybet_match_id TEXT,
    map_number INTEGER,
    strict_mapping_id INTEGER,
    dota_match_id INTEGER,
    raybet_winner_side TEXT,
    opendota_winner_side TEXT,
    raybet_evidence_ref TEXT,
    opendota_evidence_ref TEXT,
    status TEXT,
    reason TEXT,
    first_observed_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (raybet_match_id, map_number)
);
CREATE TABLE settlement_result_evidence (
    evidence_id INTEGER PRIMARY KEY,
    raybet_match_id TEXT,
    map_number INTEGER,
    dota_match_id INTEGER,
    source TEXT,
    status TEXT,
    winner_side TEXT,
    evidence_ref TEXT,
    facts_json TEXT,
    observed_at TEXT
);
CREATE TABLE map_results (
    raybet_match_id TEXT,
    map_number INTEGER,
    strict_mapping_id INTEGER,
    dota_match_id INTEGER,
    winner_side TEXT,
    team_one_kills INTEGER,
    team_two_kills INTEGER,
    duration_seconds INTEGER,
    evidence_ref TEXT,
    settled_at TEXT
);
CREATE TABLE matches (
    match_id INTEGER PRIMARY KEY,
    radiant_team_id INTEGER,
    dire_team_id INTEGER,
    radiant_win INTEGER,
    duration INTEGER,
    start_time INTEGER,
    leagueid INTEGER,
    radiant_score INTEGER,
    dire_score INTEGER
);
CREATE TABLE teams (
    team_id INTEGER PRIMARY KEY,
    name TEXT,
    tag TEXT,
    logo_url TEXT
);
CREATE TABLE leagues (leagueid INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE heroes (hero_id INTEGER PRIMARY KEY, localized_name TEXT);
CREATE TABLE match_players (
    match_id INTEGER,
    player_slot INTEGER,
    account_id INTEGER,
    team_id INTEGER,
    hero_id INTEGER,
    is_radiant INTEGER,
    kills INTEGER,
    deaths INTEGER,
    assists INTEGER,
    gold_per_min INTEGER,
    xp_per_min INTEGER,
    net_worth INTEGER,
    last_hits INTEGER,
    denies INTEGER,
    hero_damage INTEGER,
    hero_healing INTEGER,
    tower_damage INTEGER,
    level INTEGER,
    lane_efficiency REAL,
    kda REAL
);
CREATE TABLE player_map_facts (
    fact_id INTEGER PRIMARY KEY,
    match_id INTEGER,
    player_slot INTEGER,
    account_id INTEGER,
    team_id INTEGER,
    facts_json TEXT,
    created_at TEXT
);
CREATE TABLE match_ingest_status (
    match_id INTEGER PRIMARY KEY,
    event_id TEXT,
    map_number INTEGER,
    ingest_state TEXT,
    reconciliation_status TEXT,
    missing_fields_json TEXT
);
CREATE TABLE gold_advantage (match_id INTEGER, time_min INTEGER, value REAL);
CREATE TABLE xp_advantage (match_id INTEGER, time_min INTEGER, value REAL);
CREATE TABLE objectives (
    id INTEGER PRIMARY KEY,
    match_id INTEGER,
    time INTEGER,
    type TEXT,
    unit TEXT,
    key TEXT,
    player_slot INTEGER
);
CREATE TABLE teamfights (
    id INTEGER PRIMARY KEY,
    match_id INTEGER,
    start_time INTEGER,
    end_time INTEGER,
    last_death INTEGER,
    deaths INTEGER
);
CREATE TABLE teamfight_players (
    teamfight_id INTEGER,
    player_slot INTEGER,
    deaths INTEGER,
    buybacks INTEGER,
    damage INTEGER,
    healing INTEGER,
    gold_delta INTEGER,
    xp_delta INTEGER,
    kills INTEGER
);
"""


NOW = "2026-07-17T00:00:00+00:00"


class RayBetPostmatchLinkApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "postmatch.db"
        connection = sqlite3.connect(self.path)
        connection.executescript(SCHEMA)
        self._seed(connection)
        connection.commit()
        connection.close()

        self.mapping = SimpleNamespace(
            mapping_id=7,
            raybet_match_id="ray-1",
            map_number=1,
            event_id="event-1",
            acceptance_mode="manual_exact",
            mapping_version="strict-live-map-v3",
            canonical_team_one_id=10,
            canonical_team_one_name="Alpha",
            canonical_team_two_id=20,
            canonical_team_two_name="Beta",
            accepted_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        eligibility = SimpleNamespace(
            eligible=True,
            reason="eligible",
            mapping=self.mapping,
        )
        scopes = CurrentDerivedScopes(
            available=True,
            formal=frozenset({9001}),
        )
        self.path_patch = patch.object(queries, "DB_PATH", str(self.path))
        self.gate_patch = patch.object(
            intelligence,
            "query_strict_live_eligibility",
            return_value=eligibility,
        )
        self.snapshot_patch = patch.object(
            intelligence,
            "query_strict_mapping_snapshot",
            return_value=eligibility,
        )
        self.scope_patch = patch.object(
            intelligence,
            "_targeted_scopes",
            return_value=scopes,
        )
        self.path_patch.start()
        self.gate_patch.start()
        self.snapshot_patch.start()
        self.scope_patch.start()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.scope_patch.stop()
        self.snapshot_patch.stop()
        self.gate_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    @staticmethod
    def _seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO raybet_matches (raybet_match_id) VALUES ('ray-1')"
        )
        connection.executemany(
            """INSERT INTO odds_snapshots VALUES
               (?, 'ray-1', ?, ?, '5', 'map_1', ?, ?, 'winner-map-1',
                'winner', 1)""",
            (
                (1, NOW, 2.0, "team_one", "one"),
                (2, NOW, 3.0, "team_two", "two"),
            ),
        )
        connection.executemany(
            """INSERT INTO odds_alignments VALUES
               (?, 'ray-1', 1, 600, ?, 'vision_exact', 0, 1, NULL)""",
            ((1, NOW), (2, NOW)),
        )
        connection.executemany(
            "INSERT INTO teams VALUES (?, ?, ?, NULL)",
            ((10, "Alpha", "A"), (20, "Beta", "B")),
        )
        connection.execute("INSERT INTO leagues VALUES (1, 'Event One')")
        connection.execute(
            "INSERT INTO matches VALUES (9001, 10, 20, 1, 2400, 1, 1, 30, 20)"
        )
        connection.execute("INSERT INTO heroes VALUES (1, 'Axe')")
        connection.execute(
            """INSERT INTO match_players VALUES
               (9001, 0, 101, 10, 1, 1, 8, 2, 10, 650, 700, 20000,
                250, 10, 22000, 0, 8000, 25, 0.55, 9.0)"""
        )
        connection.execute(
            """INSERT INTO player_map_facts VALUES
               (1, 9001, 0, 101, 10, ?, ?)""",
            (
                json.dumps(
                    {
                        "name": "Alice",
                        "buyback_log": [{"time": 650}],
                    }
                ),
                NOW,
            ),
        )
        connection.executemany(
            "INSERT INTO player_map_facts VALUES (?, 9001, ?, ?, ?, ?, ?)",
            (
                (
                    index + 2,
                    slot,
                    101 + index + 1,
                    10 if slot < 128 else 20,
                    json.dumps({"buyback_log": []}),
                    NOW,
                )
                for index, slot in enumerate((1, 2, 3, 4, 128, 129, 130, 131, 132))
            ),
        )
        connection.execute(
            """INSERT INTO match_ingest_status VALUES
               (9001, 'event-1', 1, 'complete', 'reconciled', '[]')"""
        )
        connection.executemany(
            "INSERT INTO gold_advantage VALUES (9001, ?, ?)",
            ((0, 0), (10, 5000)),
        )
        connection.executemany(
            "INSERT INTO xp_advantage VALUES (9001, ?, ?)",
            ((0, 0), (10, 3000)),
        )
        connection.execute(
            """INSERT INTO objectives VALUES
               (1, 9001, 620, 'CHAT_MESSAGE_ROSHAN_KILL', 'npc_dota_roshan',
                'Roshan', 0)"""
        )
        connection.execute("INSERT INTO teamfights VALUES (1, 9001, 630, 660, 655, 3)")
        connection.execute(
            """INSERT INTO teamfight_players VALUES
               (1, 0, 0, 1, 5000, 0, 1000, 500, 2)"""
        )

    def _insert_reconciliation(
        self,
        *,
        status: str = "confirmed",
        reason: str = "sources_agree",
        observed_at: str = NOW,
        strict_mapping_id: int | None = 7,
    ) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """INSERT INTO settlement_reconciliations VALUES
               ('ray-1', 1, ?, 9001, 'team_one', 'team_one',
                'raybet:9001', 'opendota:9001', ?, ?, ?, ?)""",
            (strict_mapping_id, status, reason, observed_at, observed_at),
        )
        if status == "confirmed":
            connection.execute(
                """INSERT INTO map_results VALUES
                   ('ray-1', 1, ?, 9001, 'team_one', 30, 20, 2400,
                    'settlement-reconciliation:ray-1:map:1', ?)""",
                (strict_mapping_id, observed_at),
            )
        connection.commit()
        connection.close()

    def _insert_evidence(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.executemany(
            """INSERT INTO settlement_result_evidence VALUES
               (?, 'ray-1', 1, 9001, ?, 'confirmed', 'team_one', ?, ?, ?)""",
            (
                (
                    1,
                    "raybet",
                    "raybet:9001",
                    json.dumps({"status": "confirmed", "winner_side": "team_one"}),
                    NOW,
                ),
                (
                    2,
                    "opendota",
                    "opendota:9001",
                    json.dumps({"dota_match_id": 9001, "winner_side": "team_one"}),
                    NOW,
                ),
            ),
        )
        connection.commit()
        connection.close()

    def test_missing_confirmed_draft_reports_exact_wait_reason(self) -> None:
        response = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["reason"], "waiting_for_confirmed_draft")
        self.assertIsNotNone(datetime.fromisoformat(payload["checked_at"]))
        self.assertIsNone(payload["postmatch"])
        self.assertEqual(payload["mapping"]["mapping_id"], 7)

    def test_out_right_match_never_enters_postmatch_api(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """INSERT INTO raybet_matches
               (raybet_match_id, team_one, team_two, raw_json)
               VALUES ('38401042', 'Team 1', 'Team 2', ?)""",
            (
                json.dumps(
                    {
                        "id": "38401042",
                        "match_short_name": "Outright",
                        "team": [
                            {
                                "pos": position,
                                "team_name": f"Team {position}",
                            }
                            for position in range(1, 25)
                        ],
                    }
                ),
            ),
        )
        connection.commit()
        connection.close()

        response = self.client.get(
            "/api/monitor/matches/38401042/maps/1/postmatch"
        )

        self.assertEqual(response.status_code, 404)

    def test_trusted_draft_still_never_fuzzy_links_without_reconciliation(self) -> None:
        with patch.object(
            intelligence,
            "has_trusted_confirmed_draft",
            return_value=True,
        ):
            response = self.client.get(
                "/api/monitor/matches/ray-1/maps/1/postmatch"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["reason"], "reconciliation_missing")
        self.assertIsNone(payload["postmatch"])
        self.assertEqual(payload["mapping"]["mapping_id"], 7)

    def test_pending_and_manual_review_do_not_expose_postmatch(self) -> None:
        for status, reason, expected_reason in (
            ("pending", "waiting_for_raybet", "reconciliation_pending"),
            ("manual_review", "winner_conflict", "reconciliation_review_required"),
        ):
            with self.subTest(status=status):
                connection = sqlite3.connect(self.path)
                connection.execute("DELETE FROM settlement_reconciliations")
                connection.commit()
                connection.close()
                self._insert_reconciliation(status=status, reason=reason)

                payload = self.client.get(
                    "/api/monitor/matches/ray-1/maps/1/postmatch"
                ).json()

                self.assertEqual(payload["status"], "review")
                self.assertEqual(payload["reason"], expected_reason)
                self.assertEqual(payload["reconciliation"]["reason"], reason)
                self.assertIsNone(payload["postmatch"])

    def test_confirmed_exact_link_aggregates_postmatch_and_minute_events(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()

        response = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "available")
        self.assertEqual(payload["reason"], "confirmed_exact_link")
        self.assertEqual(payload["postmatch"]["match"]["match_id"], 9001)
        self.assertEqual(
            payload["postmatch"]["player_performance"][0]["player_name"], "Alice"
        )
        event = next(
            row
            for row in payload["postmatch"]["events"]
            if row["event_type"] == "objective"
        )
        self.assertEqual(event["game_time_seconds"], 620)
        self.assertEqual(event["radiant_gold_adv"], 5000.0)
        self.assertEqual(event["radiant_xp_adv"], 3000.0)
        self.assertAlmostEqual(event["team_one_probability"], 0.6)
        self.assertAlmostEqual(event["team_two_probability"], 0.4)
        self.assertTrue(payload["postmatch"]["event_availability"]["buybacks"])
        self.assertEqual(
            {row["event_type"] for row in payload["postmatch"]["events"]},
            {"economy", "objective", "teamfight", "buyback"},
        )

    def test_mismatched_alignment_map_never_populates_event_probability(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE odds_alignments SET map_number=2")
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        event = next(
            row
            for row in payload["postmatch"]["events"]
            if row["event_type"] == "objective"
        )
        self.assertIsNone(event["team_one_probability"])
        self.assertIsNone(event["team_two_probability"])
        self.assertFalse(
            payload["postmatch"]["event_availability"]["odds_game_clock_alignment"]
        )

    def test_one_sided_alignment_identity_never_marks_pair_as_aligned(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE odds_alignments
                  SET map_number=2, game_clock_seconds=900
                WHERE odds_snapshot_id=2"""
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        event = next(
            row
            for row in payload["postmatch"]["events"]
            if row["event_type"] == "objective"
        )
        self.assertIsNone(event["team_one_probability"])
        self.assertIsNone(event["team_two_probability"])
        self.assertFalse(
            payload["postmatch"]["event_availability"]["odds_game_clock_alignment"]
        )

    def test_same_second_odds_are_not_treated_as_pre_event(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE odds_alignments SET game_clock_seconds=620")
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        event = next(
            row
            for row in payload["postmatch"]["events"]
            if row["event_type"] == "objective"
        )
        self.assertIsNone(event["team_one_probability"])
        self.assertIsNone(event["team_two_probability"])

    def test_missing_opendota_map_number_is_review(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE match_ingest_status SET map_number=NULL WHERE match_id=9001"
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "opendota_map_number_conflict")
        self.assertIsNone(payload["postmatch"])

    def test_non_boolean_opendota_result_is_review(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE matches SET radiant_win=2 WHERE match_id=9001")
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "opendota_result_identity_conflict")
        self.assertIsNone(payload["postmatch"])

    def test_future_reconciliation_is_review(self) -> None:
        future = "2099-01-01T00:00:00+00:00"
        self._insert_reconciliation(observed_at=future)
        self._insert_evidence()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "reconciliation_causal_order_invalid")
        self.assertIsNone(payload["postmatch"])

    def test_evidence_after_reconciliation_update_is_review(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE settlement_result_evidence
                  SET observed_at='2099-01-01T00:00:00+00:00'"""
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "settlement_evidence_causal_order_invalid")
        self.assertIsNone(payload["postmatch"])

    def test_confirmed_row_with_conflicting_evidence_is_review(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE settlement_result_evidence SET winner_side='team_two'
                WHERE source='opendota'"""
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "settlement_evidence_conflict")
        self.assertIsNone(payload["postmatch"])

    def test_confirmed_columns_cannot_hide_conflicting_evidence_json(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE settlement_result_evidence SET facts_json=?
                WHERE source='opendota'""",
            (json.dumps({"dota_match_id": 9001, "winner_side": "team_two"}),),
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "settlement_evidence_conflict")
        self.assertIsNone(payload["postmatch"])

    def test_reconciliation_before_mapping_acceptance_is_review(self) -> None:
        self._insert_reconciliation(observed_at="2026-07-15T00:00:00+00:00")
        self._insert_evidence()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "reconciliation_causal_order_invalid")
        self.assertIsNone(payload["postmatch"])

    def test_reconciliation_must_resolve_to_same_historical_mapping(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        replacement = SimpleNamespace(**{**vars(self.mapping), "mapping_id": 8})
        historical = SimpleNamespace(
            eligible=True, reason="eligible", mapping=replacement
        )
        with patch.object(
            intelligence,
            "query_strict_mapping_snapshot",
            return_value=historical,
        ):
            payload = self.client.get(
                "/api/monitor/matches/ray-1/maps/1/postmatch"
            ).json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "reconciliation_mapping_lineage_unverified")
        self.assertIsNone(payload["postmatch"])

    def test_duplicate_opendota_link_is_review(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute(
            """INSERT INTO settlement_reconciliations VALUES
               ('ray-2', 1, 8, 9001, 'team_one', 'team_one',
                'raybet:other', 'opendota:9001', 'manual_review',
                'opendota_match_link_conflict', ?, ?)""",
            (NOW, NOW),
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/monitor/matches/ray-1/maps/1/postmatch").json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "opendota_match_link_conflict")
        self.assertIsNone(payload["postmatch"])

    def test_legacy_reconciliation_without_mapping_authority_is_review(self) -> None:
        self._insert_reconciliation(strict_mapping_id=None)
        self._insert_evidence()

        payload = self.client.get(
            "/api/monitor/matches/ray-1/maps/1/postmatch"
        ).json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(
            payload["reason"], "reconciliation_mapping_authority_missing"
        )
        self.assertIsNone(payload["postmatch"])

    def test_later_live_mapping_invalidation_warns_without_hiding_history(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        current = SimpleNamespace(
            eligible=False,
            reason="mapping_invalidated",
            mapping=self.mapping,
        )
        with patch.object(
            intelligence,
            "query_strict_live_eligibility",
            return_value=current,
        ):
            payload = self.client.get(
                "/api/monitor/matches/ray-1/maps/1/postmatch"
            ).json()

        self.assertEqual(payload["status"], "available")
        self.assertEqual(payload["warnings"], ["mapping_invalidated"])
        self.assertEqual(payload["postmatch"]["match"]["match_id"], 9001)

    def test_map_result_mapping_mismatch_is_review(self) -> None:
        self._insert_reconciliation()
        self._insert_evidence()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE map_results SET strict_mapping_id=8")
        connection.commit()
        connection.close()

        payload = self.client.get(
            "/api/monitor/matches/ray-1/maps/1/postmatch"
        ).json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "map_result_mapping_lineage_unverified")
        self.assertIsNone(payload["postmatch"])

    def test_ineligible_strict_mapping_is_never_bypassed(self) -> None:
        ineligible = SimpleNamespace(
            eligible=False,
            reason="accepted_mapping_ambiguous",
            mapping=None,
        )
        with patch.object(
            intelligence,
            "query_strict_live_eligibility",
            return_value=ineligible,
        ):
            payload = self.client.get(
                "/api/monitor/matches/ray-1/maps/1/postmatch"
            ).json()

        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["reason"], "accepted_mapping_ambiguous")
        self.assertIsNone(payload["reconciliation"])
        self.assertIsNone(payload["postmatch"])

    def test_unknown_raybet_match_is_404_and_map_number_is_validated(self) -> None:
        self.assertEqual(
            self.client.get(
                "/api/monitor/matches/unknown/maps/1/postmatch"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/monitor/matches/ray-1/maps/0/postmatch").status_code,
            422,
        )


if __name__ == "__main__":
    unittest.main()
