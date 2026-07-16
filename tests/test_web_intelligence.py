from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from event_intelligence.backtest import BACKTEST_VERSION
from event_intelligence.benchmarks import BENCHMARK_VERSION
from event_intelligence.draft_features import FEATURE_VERSION as DRAFT_FEATURE_VERSION
from event_intelligence.draft_model import MODEL_VERSION as DRAFT_MODEL_VERSION
from event_intelligence.incremental import (
    ROLE_VERSION,
    SCORE_VERSION,
    StrictDerivedPipeline,
)
from event_intelligence.player_scoring import score_version_for_role
from event_intelligence.team_profiles import PROFILE_VERSION
from event_intelligence.team_states import LABEL_VERSION
from web import queries
from web.routers.intelligence import router


OLD_SCORE_VERSION = "player-score-v2+observed-role=role-v1"


SCHEMA = """
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
    account_id INTEGER,
    player_slot INTEGER,
    hero_id INTEGER,
    is_radiant INTEGER,
    team_id INTEGER,
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
    facts_json TEXT,
    created_at TEXT
);
CREATE TABLE player_map_scores (
    score_id INTEGER PRIMARY KEY,
    match_id INTEGER,
    player_slot INTEGER,
    account_id INTEGER,
    position INTEGER,
    execution_score REAL,
    result_adjusted_score REAL,
    component_facts_json TEXT,
    component_scores_json TEXT,
    weights_json TEXT,
    coverage REAL,
    role_confidence REAL,
    benchmark_cutoff TEXT,
    score_version TEXT,
    explanation_json TEXT
);
CREATE TABLE player_role_assignments (
    match_id INTEGER,
    player_slot INTEGER,
    purpose TEXT,
    assignment_version TEXT,
    position INTEGER
);
CREATE TABLE team_map_states (
    state_id INTEGER PRIMARY KEY,
    match_id INTEGER,
    team_id INTEGER,
    side TEXT,
    label TEXT,
    duration_seconds INTEGER,
    max_lead REAL,
    max_deficit REAL,
    ahead_fraction REAL,
    behind_fraction REAL,
    even_fraction REAL,
    signed_auc REAL,
    absolute_auc REAL,
    crossings_json TEXT,
    first_significant_lead_at INTEGER,
    first_significant_deficit_at INTEGER,
    closeout_seconds INTEGER,
    objective_conversion_json TEXT,
    curve_coverage REAL,
    source_versions_json TEXT,
    input_hash TEXT,
    label_version TEXT,
    created_at TEXT
);
CREATE TABLE team_style_profiles (
    profile_id INTEGER PRIMARY KEY,
    team_id INTEGER,
    profile_cutoff TEXT,
    profile_version TEXT,
    opportunity_counts_json TEXT,
    posterior_rates_json TEXT,
    duration_quantiles_json TEXT,
    weighting_json TEXT,
    effective_sample_size REAL,
    created_at TEXT
);
CREATE TABLE event_registry (
    event_id TEXT PRIMARY KEY,
    tier TEXT,
    prize_pool_usd REAL,
    scope_policy_version TEXT,
    scope TEXT,
    evidence_status TEXT,
    approval_status TEXT,
    included_stages_json TEXT,
    excluded_categories_json TEXT,
    include_internal_lcq INTEGER,
    excludes_qualifiers INTEGER,
    excludes_division_2 INTEGER,
    excludes_exhibitions INTEGER,
    excludes_forfeits INTEGER,
    excludes_void_remakes INTEGER
);
CREATE TABLE formal_map_eligibility (
    match_id INTEGER PRIMARY KEY,
    event_id TEXT,
    player_readiness TEXT,
    state_readiness TEXT,
    draft_readiness TEXT
);
CREATE TABLE match_ingest_status (
    match_id INTEGER PRIMARY KEY,
    event_id TEXT,
    latest_raw_content_hash TEXT,
    normalizer_version TEXT
);
CREATE TABLE strict_derived_status (
    match_id INTEGER PRIMARY KEY,
    source_content_hash TEXT,
    role_assignment_version TEXT,
    score_version TEXT,
    team_state_version TEXT,
    profile_cutoff TEXT,
    profile_version TEXT,
    normalizer_version TEXT,
    benchmark_version TEXT,
    profile_context_hash TEXT
);
CREATE TABLE draft_model_runs (
    run_id TEXT PRIMARY KEY,
    model_version TEXT,
    model_kind TEXT,
    horizon_minutes INTEGER,
    availability_mode TEXT,
    training_cutoff TEXT,
    configuration_json TEXT,
    status TEXT
);
CREATE TABLE draft_predictions (
    prediction_id INTEGER PRIMARY KEY,
    run_id TEXT,
    match_id INTEGER,
    prediction_cutoff TEXT,
    cutoff_source TEXT,
    probability REAL,
    uncertainty REAL,
    support INTEGER,
    eventual_radiant_win INTEGER,
    status TEXT,
    input_snapshot_hash TEXT
);
"""


def state_row(
    state_id: int,
    match_id: int,
    team_id: int,
    side: str,
    label: str,
    version: str,
) -> tuple[object, ...]:
    return (
        state_id,
        match_id,
        team_id,
        side,
        label,
        1800,
        7000.0,
        -4000.0,
        0.6,
        0.2,
        0.2,
        100.0,
        200.0,
        json.dumps([{"minute": 20, "from_band": "even", "to_band": "ahead"}]),
        1200,
        600,
        120,
        json.dumps({"roshan_opportunity": True}),
        1.0,
        json.dumps([["opendota", "source-version"]]),
        f"hash-{state_id}",
        version,
        "2026-07-14T00:00:00+00:00",
    )


class IntelligenceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "intelligence.db"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        self._seed(connection)
        connection.commit()
        connection.close()

        self.db_patch = patch.object(queries, "DB_PATH", str(self.path))
        self.db_patch.start()
        def verified_keys(
            current: sqlite3.Connection, match_ids: object
        ) -> frozenset[tuple[str, int]]:
            eligible = set(match_ids)
            return frozenset(
                (str(row[0]), int(row[1]))
                for row in current.execute(
                    "SELECT run_id, match_id FROM draft_predictions "
                    "WHERE run_id='current'"
                ).fetchall()
                if int(row[1]) in eligible
            )

        self.draft_lineage_patch = patch(
            "event_intelligence.incremental._current_draft_prediction_keys",
            side_effect=verified_keys,
        )
        self.draft_lineage_patch.start()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.draft_lineage_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    def _seed(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """INSERT INTO event_registry VALUES
               ('event-1', 'tier_1', 1000000, 'strict-event-scope-v1',
                'approved_formal', 'verified', 'approved', '["main_event"]',
                '["qualifier","division_2","exhibition"]', 1, 1, 1, 1, 1, 1)"""
        )
        connection.executemany(
            "INSERT INTO formal_map_eligibility VALUES (?, 'event-1', 'ready', 'ready', 'ready')",
            ((1,), (2,)),
        )
        connection.executemany(
            "INSERT INTO match_ingest_status VALUES (?, 'event-1', ?, 'normalizer-v1')",
            ((1, "source-1"), (2, "source-2")),
        )
        connection.executemany(
            "INSERT INTO teams VALUES (?, ?, ?, ?)",
            (
                (10, "Alpha", "A", "alpha.png"),
                (20, "Beta", "B", "beta.png"),
                (30, "Gamma", "G", "gamma.png"),
            ),
        )
        connection.execute("INSERT INTO leagues VALUES (7, 'Test League')")
        connection.executemany(
            "INSERT INTO matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1, 10, 20, 1, 2100, 100, 7, 30, 20),
                (2, 30, 10, 0, 2400, 200, 7, 15, 35),
            ),
        )
        connection.executemany(
            "INSERT INTO heroes VALUES (?, ?)",
            ((1, "Axe"), (2, "Bane"), (3, "Chen"), (4, "Doom")),
        )
        connection.executemany(
            """INSERT INTO match_players
                       (match_id, account_id, player_slot, hero_id, is_radiant,
                        team_id, kills, deaths, assists, gold_per_min,
                        xp_per_min, net_worth, last_hits, denies, hero_damage,
                        hero_healing, tower_damage, level, lane_efficiency, kda)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (1, 101, 0, 1, 1, 10, 8, 2, 11, 650, 720, 21000, 260, 12, 24000, 0, 9000, 25, 0.57, 9.5),
                (1, 102, 128, 2, 0, 20, 2, 8, 14, 410, 520, 13200, 95, 4, 11000, 1200, 800, 20, 0.43, 2.0),
                (2, 201, 0, 3, 1, 30, 5, 4, 18, 530, 610, 17500, 180, 8, 19000, 350, 3200, 23, 0.51, 5.75),
                (2, 202, 128, 4, 0, 10, 10, 3, 9, 700, 760, 22800, 285, 15, 28000, 0, 10500, 26, 0.62, 6.33),
            ),
        )
        connection.executemany(
            "INSERT INTO player_map_facts VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    1,
                    1,
                    0,
                    101,
                    json.dumps({"name": "Alice", "personaname": "Steam Alice"}),
                    "2026-07-14",
                ),
                (2, 1, 128, 102, json.dumps({"personaname": "Bob"}), "2026-07-14"),
                (3, 2, 0, 201, json.dumps({"personaname": "Carol"}), "2026-07-14"),
                (4, 2, 128, 202, json.dumps({"personaname": "Dora"}), "2026-07-14"),
            ),
        )
        scores = (
            (1, 1, 0, 101, 1, 80.0, 82.0, True),
            (2, 1, 128, 102, 2, 90.0, 91.0, False),
            (3, 2, 0, 201, 1, 70.0, 72.0, True),
            (4, 2, 128, 202, 5, 85.0, 84.0, True),
        )
        for score_id, match_id, slot, account_id, position, execution, result, eligible in scores:
            connection.execute(
                """INSERT INTO player_map_scores VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    score_id,
                    match_id,
                    slot,
                    account_id,
                    position,
                    execution,
                    result,
                    json.dumps({"farm": {"value": 1}}),
                    json.dumps([{"name": "farm", "score": execution}]),
                    json.dumps([["farm", 1.0]]),
                    0.9,
                    0.95,
                    "2026-07-13T00:00:00+00:00",
                    SCORE_VERSION,
                    json.dumps({"ranking_eligible": eligible}),
                ),
            )
        next_score_id = 100
        slots = (0, 1, 2, 3, 4, 128, 129, 130, 131, 132)
        for match_id in (1, 2):
            for index, slot in enumerate(slots):
                position = index % 5 + 1
                connection.execute(
                    "INSERT INTO player_role_assignments VALUES (?, ?, 'observed_position', ?, ?)",
                    (match_id, slot, ROLE_VERSION, position),
                )
                if slot in {0, 128}:
                    continue
                connection.execute(
                    """INSERT INTO player_map_scores VALUES
                       (?, ?, ?, NULL, ?, 50, 50, '{}', '[]', '[]', 0.8, 0.95,
                        '2026-07-13T00:00:00+00:00', ?,
                        '{"ranking_eligible":false}')""",
                    (next_score_id, match_id, slot, position, SCORE_VERSION),
                )
                next_score_id += 1
        connection.execute(
            """INSERT INTO player_map_scores VALUES
               (99, 1, 0, 101, 1, 99, 99, '{}', '[]', '[]', 1, 1,
                '2026-01-01', ?, '{"ranking_eligible":true}')""",
            (OLD_SCORE_VERSION,),
        )

        states = (
            state_row(1, 1, 10, "radiant", "comeback", LABEL_VERSION),
            state_row(2, 1, 20, "dire", "throw", LABEL_VERSION),
            state_row(3, 2, 30, "radiant", "advantage", LABEL_VERSION),
            state_row(4, 2, 10, "dire", "disadvantage", LABEL_VERSION),
            state_row(5, 1, 10, "radiant", "stomp", "team-state-v0"),
            state_row(6, 1, 20, "dire", "stomp_loss", "team-state-v0"),
        )
        connection.executemany(
            "INSERT INTO team_map_states VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            states,
        )

        profiles = (
            (
                1,
                10,
                "2026-07-12T00:00:00+00:00",
                PROFILE_VERSION,
                json.dumps([["comeback", 1]]),
                json.dumps([{"metric": "comeback", "mean": 0.2}]),
                json.dumps({"p50": 30}),
                json.dumps({"availability_mode": "prospective", "maps": []}),
                1.0,
                "2026-07-12",
            ),
            (
                2,
                10,
                "2026-07-14T00:00:00+00:00",
                PROFILE_VERSION,
                json.dumps([["comeback", 2]]),
                json.dumps(
                    [
                        {
                            "metric": "comeback",
                            "mean": 0.4,
                            "prior_evidence": [[1, "hidden-lineage"]],
                        }
                    ]
                ),
                json.dumps({"p50": 35}),
                json.dumps(
                    {
                        "availability_mode": "prospective",
                        "maps": [
                            {
                                "match_id": 1,
                                "state_input_hash": "hash-1",
                                "opponent_strength_evidence": [],
                                "total_weight": 0.75,
                            }
                        ],
                    }
                ),
                2.0,
                "2026-07-14",
            ),
            (
                3,
                30,
                "2026-07-14T00:00:00+00:00",
                PROFILE_VERSION,
                "[]",
                "[]",
                "{}",
                json.dumps({"availability_mode": "prospective", "maps": []}),
                1.0,
                "2026-07-14",
            ),
            (
                4,
                20,
                "2026-07-14T00:00:00+00:00",
                "team-style-v1",
                "[]",
                "[]",
                "{}",
                "{}",
                99.0,
                "2026-07-14",
            ),
            (
                5,
                10,
                "2026-07-15T00:00:00+00:00",
                PROFILE_VERSION,
                json.dumps([["stale", 99]]),
                "[]",
                "{}",
                json.dumps(
                    {
                        "availability_mode": "prospective",
                        "maps": [{"total_weight": 99.0}],
                    }
                ),
                99.0,
                "2026-07-15",
            ),
        )
        connection.executemany(
            "INSERT INTO team_style_profiles VALUES (?,?,?,?,?,?,?,?,?,?)", profiles
        )
        context_hash = StrictDerivedPipeline._profile_context_hashes(
            connection, {"event-1"}
        )["event-1"]
        connection.executemany(
            """INSERT INTO strict_derived_status VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    1,
                    "source-1",
                    ROLE_VERSION,
                    SCORE_VERSION,
                    LABEL_VERSION,
                    "2026-07-14T00:00:00+00:00",
                    PROFILE_VERSION,
                    "normalizer-v1",
                    BENCHMARK_VERSION,
                    context_hash,
                ),
                (
                    2,
                    "source-2",
                    ROLE_VERSION,
                    SCORE_VERSION,
                    LABEL_VERSION,
                    "2026-07-14T00:00:00+00:00",
                    PROFILE_VERSION,
                    "normalizer-v1",
                    BENCHMARK_VERSION,
                    context_hash,
                ),
            ),
        )

        current_configuration = json.dumps(
            {
                "backtest_version": BACKTEST_VERSION,
                "feature_version": DRAFT_FEATURE_VERSION,
                "assignment_version": ROLE_VERSION,
                "score_version": SCORE_VERSION,
            }
        )
        old_configuration = json.dumps({"score_version": OLD_SCORE_VERSION})
        wrong_backtest_configuration = json.dumps(
            {
                "backtest_version": "strict-draft-walk-forward-v2",
                "feature_version": DRAFT_FEATURE_VERSION,
                "assignment_version": ROLE_VERSION,
                "score_version": SCORE_VERSION,
            }
        )
        connection.executemany(
            "INSERT INTO draft_model_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "current",
                    DRAFT_MODEL_VERSION,
                    "pure_draft",
                    10,
                    "reconstructed_walk_forward",
                    "2026-07-13",
                    current_configuration,
                    "trained",
                ),
                (
                    "old",
                    "draft-v0",
                    "pure_draft",
                    10,
                    "reconstructed_walk_forward",
                    "2026-01-01",
                    old_configuration,
                    "trained",
                ),
                (
                    "wrong-model",
                    "draft-logistic-l2-v2",
                    "pure_draft",
                    10,
                    "reconstructed_walk_forward",
                    "2026-07-13",
                    current_configuration,
                    "trained",
                ),
                (
                    "wrong-backtest",
                    DRAFT_MODEL_VERSION,
                    "pure_draft",
                    10,
                    "reconstructed_walk_forward",
                    "2026-07-13",
                    wrong_backtest_configuration,
                    "trained",
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO draft_predictions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1, "current", 1, "2026-07-13", "map_start", 0.8, 0.1, 100, 1, "settled", "a" * 64),
                (2, "current", 2, "2026-07-13", "map_start", 0.2, 0.1, 100, 0, "settled", "b" * 64),
                (3, "old", 1, "2026-01-01", "map_start", 0.99, 0.1, 999, 1, "settled", "c" * 64),
                (4, "wrong-model", 1, "2026-07-13", "map_start", 0.99, 0.1, 999, 1, "settled", "d" * 64),
                (5, "wrong-backtest", 1, "2026-07-13", "map_start", 0.99, 0.1, 999, 1, "settled", "e" * 64),
            ),
        )

    def test_overview_uses_only_current_versions_and_marks_missing_prospective(self) -> None:
        response = self.client.get("/api/intelligence/overview")
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["versions"]["player_score"], SCORE_VERSION)
        self.assertEqual(payload["coverage"]["player_score_rows"], 20)
        self.assertEqual(payload["coverage"]["draft_prediction_rows"], 2)
        self.assertNotIn("stomp", payload["team_state_distribution"])
        self.assertEqual(payload["team_state_distribution"]["comeback"], 1)

        reconstructed = next(
            row
            for row in payload["draft_quality"]
            if row["model_kind"] == "pure_draft"
            and row["horizon_minutes"] == 10
            and row["availability_mode"] == "reconstructed_walk_forward"
        )
        prospective = next(
            row
            for row in payload["draft_quality"]
            if row["model_kind"] == "pure_draft"
            and row["horizon_minutes"] == 10
            and row["availability_mode"] == "prospective"
        )
        self.assertEqual(reconstructed["support"], 2)
        self.assertEqual(reconstructed["assignment_version"], ROLE_VERSION)
        self.assertEqual(reconstructed["score_version"], SCORE_VERSION)
        self.assertAlmostEqual(reconstructed["brier_score"], 0.04)
        self.assertEqual(reconstructed["status"], "unsupported")
        self.assertTrue(reconstructed["is_reconstructed"])
        self.assertEqual(prospective["status"], "missing")
        self.assertIn("prospective_data_missing", prospective["gate_failures"])
        self.assertFalse(payload["availability"]["prospective"])

    def test_match_list_pairs_current_states_and_supports_filters_and_pagination(self) -> None:
        response = self.client.get("/api/intelligence/matches?page=1&page_size=1")
        payload = response.json()
        self.assertEqual(payload["pagination"]["total"], 2)
        self.assertEqual(payload["pagination"]["total_pages"], 2)
        self.assertEqual(payload["data"][0]["match_id"], 2)
        self.assertEqual(payload["data"][0]["radiant_state"]["label"], "advantage")
        self.assertEqual(payload["data"][0]["dire_state"]["label"], "disadvantage")

        filtered = self.client.get(
            "/api/intelligence/matches",
            params={"label": "comeback", "team_id": 10, "search": "Alpha"},
        ).json()
        self.assertEqual(filtered["pagination"]["total"], 1)
        self.assertEqual(filtered["data"][0]["match_id"], 1)
        self.assertEqual(filtered["data"][0]["radiant_state"]["label"], "comeback")
        self.assertEqual(filtered["data"][0]["dire_state"]["label"], "throw")

    def test_match_detail_has_parsed_current_intelligence_and_404(self) -> None:
        response = self.client.get("/api/intelligence/matches/1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["match"]["match_id"], 1)
        self.assertEqual(payload["radiant_state"]["label"], "comeback")
        self.assertIsInstance(payload["radiant_state"]["crossings"], list)
        self.assertTrue(
            payload["radiant_state"]["objective_conversion"]["roshan_opportunity"]
        )
        self.assertEqual(
            [row["player_slot"] for row in payload["player_performance"]],
            [0, 128],
        )
        self.assertEqual(payload["player_performance"][0]["player_name"], "Alice")
        self.assertEqual(payload["player_performance"][0]["hero_name"], "Axe")
        self.assertEqual(len(payload["player_scores"]), 10)
        self.assertEqual(payload["player_scores"][0]["player_name"], "Alice")
        self.assertEqual(payload["player_scores"][0]["hero_name"], "Axe")
        self.assertEqual(
            payload["player_scores"][0]["performance"],
            {
                "kills": 8,
                "deaths": 2,
                "assists": 11,
                "gold_per_min": 650,
                "xp_per_min": 720,
                "net_worth": 21000,
                "last_hits": 260,
                "denies": 12,
                "hero_damage": 24000,
                "hero_healing": 0,
                "tower_damage": 9000,
                "level": 25,
                "lane_efficiency": 0.57,
                "kda": 9.5,
            },
        )
        self.assertEqual(
            payload["player_performance"][0]["performance"],
            payload["player_scores"][0]["performance"],
        )
        self.assertIsInstance(payload["player_scores"][0]["component_facts"], dict)
        self.assertEqual(len(payload["draft_predictions"]), 1)
        self.assertEqual(payload["draft_predictions"][0]["probability"], 0.8)
        self.assertEqual(
            payload["draft_predictions"][0]["assignment_version"], ROLE_VERSION
        )
        self.assertEqual(
            payload["draft_predictions"][0]["score_version"], SCORE_VERSION
        )
        self.assertNotIn("configuration_json", payload["draft_predictions"][0])
        self.assertEqual(
            self.client.get("/api/intelligence/matches/999").status_code, 404
        )

    def test_match_detail_exposes_delivery_versions_and_cutoff_contract(self) -> None:
        payload = self.client.get("/api/intelligence/matches/1").json()

        self.assertEqual(payload["versions"]["player_score"], SCORE_VERSION)
        self.assertEqual(payload["versions"]["team_state"], LABEL_VERSION)
        self.assertEqual(payload["versions"]["draft_model"], DRAFT_MODEL_VERSION)
        self.assertTrue(any(value.startswith("2026-07-13") for value in payload["cutoffs"]["player_score"]))
        self.assertTrue(any(value.startswith("2026-07-13") for value in payload["cutoffs"]["draft_training"]))
        self.assertTrue(any(value.startswith("2026-07-13") for value in payload["cutoffs"]["draft_prediction"]))
        self.assertEqual(payload["states"]["radiant"]["label"], "comeback")
        self.assertEqual(payload["match_id"], 1)
        self.assertEqual(payload["player_performance"][0]["performance"]["kills"], 8)

    def test_match_detail_keeps_independent_performance_when_score_columns_are_legacy(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """ALTER TABLE player_map_scores RENAME TO player_map_scores_current;
               CREATE TABLE player_map_scores (
                   match_id INTEGER,
                   player_slot INTEGER,
                   account_id INTEGER,
                   position INTEGER,
                   execution_score REAL,
                   result_adjusted_score REAL,
                   coverage REAL,
                   role_confidence REAL,
                   score_version TEXT
               );
               INSERT INTO player_map_scores
                   (match_id, player_slot, account_id, position,
                    execution_score, result_adjusted_score, coverage,
                    role_confidence, score_version)
               SELECT match_id, player_slot, account_id, position,
                      execution_score, result_adjusted_score, coverage,
                      role_confidence, score_version
                 FROM player_map_scores_current;"""
        )
        connection.commit()
        connection.close()

        response = self.client.get("/api/intelligence/matches/1")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload["player_performance"]), 2)
        self.assertEqual(payload["player_performance"][0]["performance"]["kills"], 8)
        self.assertEqual(len(payload["player_scores"]), 10)
        self.assertEqual(payload["player_scores"][0]["performance"]["kills"], 8)
        self.assertEqual(payload["player_scores"][0]["component_facts"], {})

    def test_match_detail_nulls_missing_legacy_match_columns(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """ALTER TABLE matches RENAME TO matches_current;
               CREATE TABLE matches (
                   match_id INTEGER PRIMARY KEY,
                   radiant_team_id INTEGER,
                   dire_team_id INTEGER,
                   radiant_win INTEGER,
                   start_time INTEGER
               );
               INSERT INTO matches
                   (match_id, radiant_team_id, dire_team_id, radiant_win, start_time)
               SELECT match_id, radiant_team_id, dire_team_id, radiant_win, start_time
                 FROM matches_current;"""
        )
        connection.commit()
        connection.close()

        list_response = self.client.get("/api/intelligence/matches")
        response = self.client.get("/api/intelligence/matches/1")

        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertIsNone(list_response.json()["data"][0]["duration"])
        self.assertIsNone(list_response.json()["data"][0]["radiant_score"])
        self.assertEqual(response.status_code, 200, response.text)
        match = response.json()["match"]
        self.assertIsNone(match["duration"])
        self.assertIsNone(match["radiant_score"])
        self.assertIsNone(match["dire_score"])

    def test_match_detail_nulls_missing_legacy_state_columns(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """ALTER TABLE team_map_states RENAME TO team_map_states_current;
               CREATE TABLE team_map_states (
                   match_id INTEGER,
                   team_id INTEGER,
                   side TEXT,
                   label TEXT,
                   label_version TEXT
               );
               INSERT INTO team_map_states (match_id, team_id, side, label, label_version)
               SELECT match_id, team_id, side, label, label_version
                 FROM team_map_states_current;"""
        )
        connection.commit()
        connection.close()

        response = self.client.get("/api/intelligence/matches/1")

        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()["radiant_state"]
        self.assertEqual(state["label"], "comeback")
        self.assertIsNone(state["curve_coverage"])

    def test_player_leaderboard_degrades_when_legacy_facts_lack_created_at(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """ALTER TABLE player_map_facts RENAME TO player_map_facts_current;
               CREATE TABLE player_map_facts (
                   fact_id INTEGER PRIMARY KEY,
                   match_id INTEGER,
                   player_slot INTEGER,
                   account_id INTEGER,
                   facts_json TEXT
               );
               INSERT INTO player_map_facts (fact_id, match_id, player_slot, account_id, facts_json)
               SELECT fact_id, match_id, player_slot, account_id, facts_json
                 FROM player_map_facts_current;"""
        )
        connection.commit()
        connection.close()

        response = self.client.get("/api/intelligence/players")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pagination"]["total"], 3)

    def test_match_detail_omits_performance_for_legacy_player_schema(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """ALTER TABLE match_players RENAME TO match_players_current;
               CREATE TABLE match_players AS
               SELECT match_id, account_id, player_slot, hero_id,
                      is_radiant, team_id
                 FROM match_players_current;"""
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/intelligence/matches/1").json()
        self.assertEqual(payload["player_performance"][0]["player_name"], "Alice")
        self.assertEqual(payload["player_performance"][0]["hero_name"], "Axe")
        self.assertIsNone(payload["player_performance"][0]["performance"])
        self.assertEqual(payload["player_scores"][0]["player_name"], "Alice")
        self.assertEqual(payload["player_scores"][0]["hero_name"], "Axe")
        self.assertIsNone(payload["player_scores"][0]["performance"])

    def test_match_detail_degrades_on_malformed_legacy_json(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE player_map_facts SET facts_json='not-json' WHERE match_id=1 AND player_slot=0"
        )
        connection.execute(
            "UPDATE draft_model_runs SET configuration_json='not-json' WHERE run_id='current'"
        )
        connection.commit()
        connection.close()

        response = self.client.get("/api/intelligence/matches/1")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIsNone(payload["player_performance"][0]["player_name"])
        self.assertEqual(payload["draft_predictions"], [])

    def test_match_detail_accepts_validated_prospective_v3_run(self) -> None:
        prospective_role_version = "role-assignment-v1-prospective"
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE draft_model_runs
                  SET availability_mode='prospective', configuration_json=?
                WHERE run_id='current'""",
            (
                json.dumps(
                    {
                        "backtest_version": BACKTEST_VERSION,
                        "feature_version": DRAFT_FEATURE_VERSION,
                        "assignment_version": prospective_role_version,
                        "score_version": score_version_for_role(
                            prospective_role_version
                        ),
                    }
                ),
            ),
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/intelligence/matches/1").json()

        self.assertEqual(len(payload["draft_predictions"]), 1)
        self.assertEqual(
            payload["draft_predictions"][0]["availability_mode"], "prospective"
        )
        self.assertEqual(
            payload["draft_predictions"][0]["assignment_version"],
            prospective_role_version,
        )
        self.assertEqual(
            payload["draft_predictions"][0]["score_version"],
            score_version_for_role(prospective_role_version),
        )

    def test_match_detail_rejects_unapproved_same_family_cohort(self) -> None:
        role_version = "role-assignment-v2-prospective"
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE draft_model_runs
                  SET availability_mode='prospective', configuration_json=?
                WHERE run_id='current'""",
            (
                json.dumps(
                    {
                        "backtest_version": BACKTEST_VERSION,
                        "feature_version": DRAFT_FEATURE_VERSION,
                        "assignment_version": role_version,
                        "score_version": score_version_for_role(role_version),
                    }
                ),
            ),
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/intelligence/matches/1").json()

        self.assertEqual(payload["draft_predictions"], [])

    def test_player_leaderboard_excludes_old_and_ineligible_scores(self) -> None:
        first = self.client.get(
            "/api/intelligence/players", params={"page": 1, "page_size": 1}
        ).json()
        self.assertEqual(first["pagination"]["total"], 3)
        self.assertEqual(first["data"][0]["account_id"], 202)
        self.assertEqual(first["data"][0]["average_execution_score"], 85.0)
        self.assertEqual(
            first["data"][0]["benchmark_cutoffs"], ["2026-07-13T00:00:00+00:00"]
        )
        self.assertEqual(
            first["data"][0]["benchmark_cutoff_min"], "2026-07-13T00:00:00+00:00"
        )

        position_one = self.client.get(
            "/api/intelligence/players", params={"position": 1, "search": "Alice"}
        ).json()
        self.assertEqual(position_one["pagination"]["total"], 1)
        self.assertEqual(position_one["data"][0]["account_id"], 101)
        self.assertEqual(position_one["data"][0]["average_execution_score"], 80.0)

    def test_player_leaderboard_keeps_positions_separate(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE player_map_scores
                  SET account_id=101, position=2, execution_score=40,
                      result_adjusted_score=42, role_confidence=0.9,
                      explanation_json='{"ranking_eligible":true}'
                WHERE match_id=2 AND player_slot=1 AND score_version=?""",
            (SCORE_VERSION,),
        )
        connection.commit()
        connection.close()

        payload = self.client.get(
            "/api/intelligence/players", params={"search": "Alice"}
        ).json()
        self.assertEqual(payload["pagination"]["total"], 2)
        by_position = {row["position"]: row for row in payload["data"]}
        self.assertEqual(by_position[1]["map_count"], 1)
        self.assertEqual(by_position[1]["average_execution_score"], 80.0)
        self.assertEqual(by_position[2]["map_count"], 1)
        self.assertEqual(by_position[2]["average_execution_score"], 40.0)

    def test_teams_use_latest_current_profile_and_parsed_compact_json(self) -> None:
        payload = self.client.get("/api/intelligence/teams").json()
        self.assertEqual(len(payload["data"]), 2)
        alpha = next(row for row in payload["data"] if row["team_id"] == 10)
        self.assertEqual(alpha["profile_cutoff"], "2026-07-14T00:00:00+00:00")
        self.assertEqual(alpha["opportunity_counts"], [["comeback", 2]])
        self.assertIsInstance(alpha["posterior_rates"], list)
        self.assertNotIn("prior_evidence", alpha["posterior_rates"][0])
        self.assertEqual(alpha["weighting"]["map_count"], 1)
        self.assertAlmostEqual(alpha["weighting"]["total_weight"], 0.75)
        self.assertEqual(alpha["state_counts"]["comeback"], 1)
        self.assertEqual(alpha["state_counts"]["disadvantage"], 1)

    def test_delivery_fails_closed_without_formal_scope_relation(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE formal_map_eligibility")
        connection.commit()
        connection.close()

        overview = self.client.get("/api/intelligence/overview").json()
        self.assertEqual(overview["coverage"]["formal_maps"], 0)
        self.assertEqual(overview["coverage"]["player_score_rows"], 0)
        self.assertEqual(overview["coverage"]["team_state_rows"], 0)
        self.assertEqual(overview["coverage"]["team_profiles"], 0)
        self.assertEqual(overview["coverage"]["draft_prediction_rows"], 0)
        self.assertEqual(
            self.client.get("/api/intelligence/matches").json()["data"], []
        )
        self.assertEqual(
            self.client.get("/api/intelligence/players").json()["data"], []
        )
        self.assertEqual(
            self.client.get("/api/intelligence/teams").json()["data"], []
        )
        self.assertEqual(
            self.client.get("/api/intelligence/matches/1").status_code, 404
        )

    def test_pending_lineage_keeps_raw_match_but_hides_all_stale_derivatives(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE match_ingest_status
                  SET latest_raw_content_hash='changed-source' WHERE match_id=1"""
        )
        connection.commit()
        connection.close()

        matches = self.client.get("/api/intelligence/matches").json()["data"]
        self.assertEqual({row["match_id"] for row in matches}, {1, 2})
        pending = next(row for row in matches if row["match_id"] == 1)
        self.assertIsNone(pending["radiant_state"])
        self.assertIsNone(pending["dire_state"])

        detail = self.client.get("/api/intelligence/matches/1").json()
        self.assertIsNone(detail["radiant_state"])
        self.assertIsNone(detail["dire_state"])
        self.assertEqual(detail["player_scores"], [])
        self.assertEqual(
            [row["player_slot"] for row in detail["player_performance"]],
            [0, 128],
        )
        self.assertEqual(detail["player_performance"][0]["player_name"], "Alice")
        self.assertEqual(detail["player_performance"][0]["hero_name"], "Axe")
        self.assertEqual(detail["player_performance"][0]["performance"]["kills"], 8)
        self.assertEqual(detail["draft_predictions"], [])

        overview = self.client.get("/api/intelligence/overview").json()
        self.assertEqual(overview["coverage"]["player_score_rows"], 10)
        self.assertEqual(overview["coverage"]["team_state_rows"], 2)
        self.assertEqual(overview["coverage"]["draft_prediction_rows"], 1)
        self.assertEqual(overview["coverage"]["team_profiles"], 0)
        self.assertEqual(
            self.client.get("/api/intelligence/teams").json()["data"], []
        )

    def test_leaderboard_rechecks_role_confidence_boundary(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """UPDATE player_map_scores
                  SET account_id=303, position=3, execution_score=100,
                      result_adjusted_score=100, role_confidence=0.69,
                      explanation_json='{"ranking_eligible":true}'
                WHERE match_id=2 AND player_slot=1 AND score_version=?""",
            (SCORE_VERSION,),
        )
        connection.commit()
        connection.close()

        payload = self.client.get("/api/intelligence/players").json()
        self.assertNotIn(303, {row["account_id"] for row in payload["data"]})

    def test_team_state_counts_are_unique_maps_and_reads_do_not_enable_wal(self) -> None:
        connection = sqlite3.connect(self.path)
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        connection.execute(
            "INSERT INTO team_map_states VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            state_row(77, 1, 10, "radiant", "comeback", LABEL_VERSION),
        )
        connection.commit()
        connection.close()
        modified_at = self.path.stat().st_mtime_ns

        teams = self.client.get("/api/intelligence/teams").json()["data"]
        alpha = next(row for row in teams if row["team_id"] == 10)
        self.assertEqual(alpha["state_counts"]["comeback"], 1)
        self.assertEqual(self.path.stat().st_mtime_ns, modified_at)
        self.assertFalse(Path(f"{self.path}-wal").exists())

        verification = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                str(verification.execute("PRAGMA journal_mode").fetchone()[0]),
                journal_mode,
            )
        finally:
            verification.close()


if __name__ == "__main__":
    unittest.main()
