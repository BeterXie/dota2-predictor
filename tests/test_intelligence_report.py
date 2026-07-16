from __future__ import annotations

import json
import math
import sqlite3
import unittest
from unittest.mock import patch

from event_intelligence.backtest import (
    BACKTEST_VERSION,
    CalibrationMetrics,
)
from event_intelligence.benchmarks import BENCHMARK_VERSION
from event_intelligence.draft_features import FEATURE_VERSION as DRAFT_FEATURE_VERSION
from event_intelligence.draft_model import MODEL_VERSION as DRAFT_MODEL_VERSION
from event_intelligence.incremental import (
    ROLE_VERSION,
    SCORE_VERSION,
    StrictDerivedPipeline,
)
from event_intelligence.report import build_intelligence_report
from event_intelligence.team_profiles import PROFILE_VERSION
from event_intelligence.team_states import LABEL_VERSION


SCHEMA = """
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
    series_id INTEGER,
    latest_raw_content_hash TEXT,
    normalizer_version TEXT
);
CREATE TABLE strict_derived_status (
    match_id INTEGER PRIMARY KEY,
    source_content_hash TEXT,
    role_assignment_version TEXT,
    score_version TEXT,
    team_state_version TEXT,
    profile_version TEXT,
    profile_cutoff TEXT,
    normalizer_version TEXT,
    benchmark_version TEXT,
    profile_context_hash TEXT
);
CREATE TABLE player_map_facts (match_id INTEGER);
CREATE TABLE player_role_assignments (
    match_id INTEGER,
    player_slot INTEGER,
    purpose TEXT,
    assignment_version TEXT,
    position INTEGER
);
CREATE TABLE player_map_scores (
    match_id INTEGER,
    player_slot INTEGER,
    account_id INTEGER,
    position INTEGER,
    execution_score REAL,
    result_adjusted_score REAL,
    coverage REAL,
    role_confidence REAL,
    explanation_json TEXT,
    score_version TEXT
);
CREATE TABLE team_map_states (
    match_id INTEGER,
    side TEXT,
    label TEXT,
    input_hash TEXT,
    label_version TEXT
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
    input_hash TEXT,
    created_at TEXT
);
CREATE TABLE draft_model_runs (
    run_id TEXT PRIMARY KEY,
    model_version TEXT,
    model_kind TEXT,
    horizon_minutes INTEGER,
    availability_mode TEXT,
    configuration_json TEXT,
    status TEXT
);
CREATE TABLE draft_predictions (
    run_id TEXT,
    match_id INTEGER,
    status TEXT,
    probability REAL,
    eventual_radiant_win INTEGER,
    input_snapshot_hash TEXT
);
"""


def _new_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.execute(
        """INSERT INTO event_registry VALUES
           ('event-1', 'tier_1', 1000000, 'strict-event-scope-v1',
            'formal_main_event', 'manually_audited', 'approved',
            '["main_event"]', '["qualifier"]', 1, 1, 1, 1, 1, 1)"""
    )
    return connection


def _profile_context_hash(connection: sqlite3.Connection) -> str:
    return StrictDerivedPipeline._profile_context_hashes(
        connection, {"event-1"}
    )["event-1"]


def _seed_map(
    connection: sqlite3.Connection,
    match_id: int,
    *,
    player_readiness: str = "ready",
    state_readiness: str = "ready",
    draft_readiness: str = "ready",
    source_current: bool = True,
    cutoff: str | None = None,
) -> None:
    source_hash = f"source-{match_id}"
    profile_cutoff = cutoff or f"cutoff-{match_id}"
    connection.execute(
        "INSERT INTO formal_map_eligibility VALUES (?, 'event-1', ?, ?, ?)",
        (match_id, player_readiness, state_readiness, draft_readiness),
    )
    connection.execute(
        "INSERT INTO match_ingest_status VALUES (?, 'event-1', ?, ?, 'normalizer-v1')",
        (match_id, match_id, source_hash),
    )
    connection.execute(
        """INSERT INTO strict_derived_status VALUES
           (?, ?, ?, ?, ?, ?, ?, 'normalizer-v1', ?, ?)""",
        (
            match_id,
            source_hash if source_current else f"stale-{match_id}",
            ROLE_VERSION,
            SCORE_VERSION,
            LABEL_VERSION,
            PROFILE_VERSION,
            profile_cutoff,
            BENCHMARK_VERSION,
            _profile_context_hash(connection),
        ),
    )


def _configuration(
    *,
    score_version: str = SCORE_VERSION,
    backtest_version: str = BACKTEST_VERSION,
    feature_version: str = DRAFT_FEATURE_VERSION,
) -> str:
    return json.dumps(
        {
            "score_version": score_version,
            "backtest_version": backtest_version,
            "feature_version": feature_version,
        },
        sort_keys=True,
    )


def _insert_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    model_version: str = DRAFT_MODEL_VERSION,
    model_kind: str = "pure_draft",
    horizon: int = 10,
    mode: str = "reconstructed_walk_forward",
    score_version: str = SCORE_VERSION,
    backtest_version: str = BACKTEST_VERSION,
    feature_version: str = DRAFT_FEATURE_VERSION,
) -> None:
    connection.execute(
        "INSERT INTO draft_model_runs VALUES (?, ?, ?, ?, ?, ?, 'fitted')",
        (
            run_id,
            model_version,
            model_kind,
            horizon,
            mode,
            _configuration(
                score_version=score_version,
                backtest_version=backtest_version,
                feature_version=feature_version,
            ),
        ),
    )


def _insert_prediction(
    connection: sqlite3.Connection,
    run_id: str,
    match_id: int,
    probability: float | None,
    outcome: int | None,
    *,
    status: str = "settled",
) -> None:
    connection.execute(
        "INSERT INTO draft_predictions VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, match_id, status, probability, outcome, f"{match_id:064x}"),
    )


class IntelligenceReportTests(unittest.TestCase):
    def setUp(self) -> None:
        def verified_keys(
            connection: sqlite3.Connection, match_ids: object
        ) -> frozenset[tuple[str, int]]:
            eligible = set(match_ids)
            return frozenset(
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT run_id, match_id FROM draft_predictions"
                ).fetchall()
                if int(row[1]) in eligible
            )

        self.draft_lineage_patch = patch(
            "event_intelligence.incremental._current_draft_prediction_keys",
            side_effect=verified_keys,
        )
        self.draft_lineage_patch.start()

    def tearDown(self) -> None:
        self.draft_lineage_patch.stop()

    def test_current_coverage_does_not_sum_algorithm_versions(self) -> None:
        connection = _new_connection()
        try:
            _seed_map(connection, 1)
            connection.executemany(
                "INSERT INTO player_map_scores VALUES (?, ?, NULL, NULL, 50, 50, 1, 1, '{}', ?)",
                (
                    (1, 0, "player-score-v2+observed-role=role-v1"),
                    (1, 1, "player-score-v3+observed-role=role-v1"),
                    (1, 2, "player-score-v3+observed-role=role-v1"),
                ),
            )
            _insert_run(
                connection,
                "v2",
                score_version="player-score-v2+observed-role=role-v1",
            )
            _insert_run(
                connection,
                "v3",
                score_version="player-score-v3+observed-role=role-v1",
            )
            _insert_prediction(connection, "v2", 1, 0.9, 1)
            _insert_prediction(connection, "v3", 1, 0.8, 1)

            report = build_intelligence_report(connection)

            self.assertEqual(report["player_scores"], 2)
            self.assertEqual(report["player_score_rows"], 3)
            self.assertEqual(
                report["player_scores_by_version"],
                {
                    "player-score-v2+observed-role=role-v1": 1,
                    "player-score-v3+observed-role=role-v1": 2,
                },
            )
            self.assertEqual(report["draft_predictions"], 1)
            self.assertEqual(report["draft_prediction_rows"], 2)
            self.assertEqual(
                report["draft_predictions_by_score_version"],
                {
                    "player-score-v2+observed-role=role-v1": 1,
                    "player-score-v3+observed-role=role-v1": 1,
                },
            )
            self.assertEqual(
                report["draft_predictions_by_mode"],
                {"reconstructed_walk_forward": 1},
            )
            self.assertEqual(report["draft_predictions_by_status"], {"settled": 1})
        finally:
            connection.close()

    def test_report_excludes_pending_and_stale_rows_from_every_delivery(self) -> None:
        connection = _new_connection()
        try:
            _seed_map(connection, 1, cutoff="cutoff-current")
            _seed_map(
                connection,
                2,
                player_readiness="pending",
                state_readiness="pending",
                draft_readiness="pending",
                cutoff="cutoff-pending",
            )
            _seed_map(
                connection,
                3,
                source_current=False,
                cutoff="cutoff-stale",
            )
            connection.executemany(
                "INSERT INTO player_map_scores VALUES (?, ?, ?, 2, ?, ?, 1, 0.9, '{\"ranking_eligible\":1}', ?)",
                (
                    (1, 0, 7, 60.0, 62.0, SCORE_VERSION),
                    (2, 0, 8, 99.0, 99.0, SCORE_VERSION),
                    (3, 0, 9, 98.0, 98.0, SCORE_VERSION),
                ),
            )
            connection.executemany(
                "INSERT INTO player_map_scores VALUES (1, ?, NULL, ?, 50, 50, 1, 1, '{\"ranking_eligible\":0}', ?)",
                (
                    (slot, (slot % 5) + 1, SCORE_VERSION)
                    for slot in range(1, 10)
                ),
            )
            connection.executemany(
                "INSERT INTO player_role_assignments VALUES (1, ?, 'observed_position', ?, ?)",
                (
                    (slot, ROLE_VERSION, (slot % 5) + 1)
                    for slot in range(10)
                ),
            )
            connection.executemany(
                "INSERT INTO team_map_states VALUES (?, ?, ?, ?, ?)",
                (
                    (1, "radiant", "comeback", "state-1-r", LABEL_VERSION),
                    (1, "dire", "throw", "state-1-d", LABEL_VERSION),
                    (2, "radiant", "stomp", "state-2", LABEL_VERSION),
                    (3, "radiant", "advantage", "state-3", LABEL_VERSION),
                ),
            )
            valid_weighting = json.dumps(
                {
                    "availability_mode": "prospective",
                    "maps": [
                        {
                            "match_id": 1,
                            "state_input_hash": "state-1-r",
                            "opponent_strength_evidence": [[1, "state-1-d"]],
                            "total_weight": 0.75,
                        }
                    ],
                }
            )
            stale_weighting = json.dumps(
                {
                    "maps": [
                        {
                            "match_id": 3,
                            "state_input_hash": "state-3",
                            "opponent_strength_evidence": [],
                            "total_weight": 99.0,
                        }
                    ]
                }
            )
            connection.executemany(
                "INSERT INTO team_style_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        1,
                        101,
                        "cutoff-current",
                        PROFILE_VERSION,
                        '{"comeback":2}',
                        '[{"mean":0.5,"prior_evidence":[1]}]',
                        "[]",
                        valid_weighting,
                        2.0,
                        "profile-current",
                        "2026-01-01",
                    ),
                    (
                        2,
                        202,
                        "cutoff-current",
                        PROFILE_VERSION,
                        '{"stale":9}',
                        "[]",
                        "[]",
                        stale_weighting,
                        9.0,
                        "profile-bad-weighting",
                        "2026-01-02",
                    ),
                    (
                        3,
                        303,
                        "cutoff-stale",
                        PROFILE_VERSION,
                        '{"stale":99}',
                        "[]",
                        "[]",
                        stale_weighting,
                        99.0,
                        "profile-stale-cutoff",
                        "2026-01-03",
                    ),
                ),
            )
            _insert_run(connection, "current")
            _insert_prediction(connection, "current", 1, 0.8, 1)
            _insert_prediction(connection, "current", 2, 0.99, 1)
            _insert_prediction(connection, "current", 3, 0.01, 0)

            report = build_intelligence_report(connection)

            self.assertEqual(report["formal_maps"], 3)
            self.assertEqual(report["player_score_rows"], 12)
            self.assertEqual(
                report["player_rankings"],
                [
                    {
                        "account_id": 7,
                        "position": 2,
                        "maps": 1,
                        "average_execution_score": 60.0,
                        "average_result_adjusted_score": 62.0,
                        "average_coverage": 1.0,
                        "average_role_confidence": 0.9,
                        "score_version": SCORE_VERSION,
                    }
                ],
            )
            self.assertEqual(
                report["team_state_distribution"], {"comeback": 1, "throw": 1}
            )
            self.assertEqual(report["team_profiles"], 3)
            self.assertEqual(len(report["team_style_profiles"]), 1)
            self.assertEqual(report["team_style_profiles"][0]["team_id"], 101)
            self.assertEqual(
                report["team_style_profiles"][0]["posterior_rates"],
                [{"mean": 0.5}],
            )
            self.assertEqual(
                report["team_style_profiles"][0]["weighting"],
                {
                    "availability_mode": "prospective",
                    "map_count": 1,
                    "total_weight": 0.75,
                },
            )
            self.assertEqual(report["draft_prediction_rows"], 1)
            self.assertEqual(report["draft_predictions"], 1)
            self.assertEqual(len(report["draft_metrics"]), 1)
            self.assertEqual(report["draft_metrics"][0]["settled_support"], 1)
        finally:
            connection.close()

    def test_draft_metrics_separate_every_protocol_dimension(self) -> None:
        connection = _new_connection()
        try:
            _seed_map(connection, 1)
            alternate_score = "player-score-v3+observed-role=alternate"
            variants = (
                ("base", {}),
                ("score", {"score_version": alternate_score}),
                ("model", {"model_version": "draft-model-v2"}),
                ("backtest", {"backtest_version": "draft-backtest-v2"}),
                ("feature", {"feature_version": "draft-features-v4"}),
                ("kind", {"model_kind": "context_adjusted"}),
                ("horizon", {"horizon": 20}),
                ("mode", {"mode": "prospective"}),
            )
            for run_id, overrides in variants:
                _insert_run(connection, run_id, **overrides)
                _insert_prediction(connection, run_id, 1, 0.8, 1)

            report = build_intelligence_report(connection)
            metrics = report["draft_metrics"]
            keys = {
                (
                    row["score_version"],
                    row["model_version"],
                    row["backtest_version"],
                    row["feature_version"],
                    row["model_kind"],
                    row["horizon_minutes"],
                    row["availability_mode"],
                )
                for row in metrics
            }

            self.assertEqual(len(metrics), len(variants))
            self.assertEqual(len(keys), len(variants))
            self.assertEqual(report["draft_prediction_rows"], len(variants))
            self.assertEqual(
                report["versions"],
                {
                    "role_assignment": ROLE_VERSION,
                    "player_score": SCORE_VERSION,
                    "team_state": LABEL_VERSION,
                    "team_profile": PROFILE_VERSION,
                    "draft_model": DRAFT_MODEL_VERSION,
                    "draft_backtest": BACKTEST_VERSION,
                    "draft_features": DRAFT_FEATURE_VERSION,
                    "draft_score_family": "player-score-v3",
                },
            )
        finally:
            connection.close()

    def test_draft_gate_uses_five_bin_ece_and_bootstrap_upper_bound(self) -> None:
        connection = _new_connection()
        try:
            for match_id in range(1, 101):
                _seed_map(connection, match_id)
            _insert_run(connection, "pass", model_version="draft-pass")
            _insert_run(connection, "ece-fail", model_version="draft-ece-fail")
            for match_id in range(1, 101):
                outcome = match_id % 2
                jitter = ((match_id - 1) // 2) / 10_000
                passing_probability = (
                    0.98 - jitter if outcome else 0.02 + jitter
                )
                failing_probability = 0.8 - jitter if outcome else 0.2 + jitter
                _insert_prediction(
                    connection, "pass", match_id, passing_probability, outcome
                )
                _insert_prediction(
                    connection, "ece-fail", match_id, failing_probability, outcome
                )

            report = build_intelligence_report(connection)
            metrics = {row["model_version"]: row for row in report["draft_metrics"]}
            passed = metrics["draft-pass"]
            failed = metrics["draft-ece-fail"]

            self.assertEqual(passed["validation_status"], "passed")
            self.assertLessEqual(passed["ece_5_bin"], 0.10)
            self.assertLessEqual(passed["ece_90_upper"], 0.15)
            self.assertEqual(passed["validation_warnings"], [])
            self.assertLess(failed["brier_score"], 0.25)
            self.assertLess(failed["log_loss"], math.log(2.0))
            self.assertGreater(failed["ece_5_bin"], 0.10)
            self.assertEqual(failed["validation_status"], "failed")
            self.assertIn("ece_above_0.10", failed["validation_warnings"])
        finally:
            connection.close()

    def test_only_missing_bootstrap_upper_can_be_provisional(self) -> None:
        connection = _new_connection()
        try:
            _seed_map(connection, 1)
            for run_id in (
                "complete",
                "missing-only",
                "missing-point-failure",
                "upper-too-high",
            ):
                _insert_run(connection, run_id, model_version=run_id)
                _insert_prediction(connection, run_id, 1, 0.8, 1)

            def fake_evaluation(
                _points: object, *, seed_material: str
            ) -> CalibrationMetrics:
                common = {
                    "support": 100,
                    "brier_score": 0.2,
                    "log_loss": 0.6,
                    "auc": 0.7,
                    "accuracy": 0.7,
                }
                if ":missing-only:" in seed_material:
                    return CalibrationMetrics(
                        **common,
                        ece_5_bin=0.05,
                        ece_90_upper=None,
                        gate_status="failed",
                        gate_failures=("ece_upper_bound_missing",),
                    )
                if ":missing-point-failure:" in seed_material:
                    return CalibrationMetrics(
                        **common,
                        ece_5_bin=0.2,
                        ece_90_upper=None,
                        gate_status="failed",
                        gate_failures=(
                            "ece_above_0.10",
                            "ece_upper_bound_missing",
                        ),
                    )
                if ":upper-too-high:" in seed_material:
                    return CalibrationMetrics(
                        **common,
                        ece_5_bin=0.05,
                        ece_90_upper=0.16,
                        gate_status="failed",
                        gate_failures=("ece_upper_bound_above_0.15",),
                    )
                return CalibrationMetrics(
                    **common,
                    ece_5_bin=0.05,
                    ece_90_upper=0.10,
                    gate_status="passed",
                    gate_failures=(),
                )

            with patch(
                "event_intelligence.report.evaluate_points",
                side_effect=fake_evaluation,
            ):
                report = build_intelligence_report(connection)
            statuses = {
                row["model_version"]: row["validation_status"]
                for row in report["draft_metrics"]
            }

            self.assertEqual(statuses["complete"], "passed")
            self.assertEqual(statuses["missing-only"], "provisional")
            self.assertEqual(statuses["missing-point-failure"], "failed")
            self.assertEqual(statuses["upper-too-high"], "failed")
        finally:
            connection.close()

    def test_draft_metric_cache_invalidates_when_a_prediction_changes(self) -> None:
        connection = _new_connection()
        try:
            _seed_map(connection, 1)
            _insert_run(connection, "cache", model_version="cache-model")
            _insert_prediction(connection, "cache", 1, 0.73, 1)
            evaluation = CalibrationMetrics(
                support=1,
                brier_score=0.1,
                log_loss=0.4,
                ece_5_bin=0.05,
                ece_90_upper=0.10,
                auc=None,
                accuracy=1.0,
                gate_status="unsupported",
                gate_failures=(),
            )

            with patch(
                "event_intelligence.report.evaluate_points",
                return_value=evaluation,
            ) as evaluator:
                build_intelligence_report(connection)
                build_intelligence_report(connection)
                self.assertEqual(evaluator.call_count, 1)

                connection.execute(
                    "UPDATE draft_predictions SET probability=0.74 WHERE run_id='cache'"
                )
                build_intelligence_report(connection)
                self.assertEqual(evaluator.call_count, 2)
        finally:
            connection.close()

    def test_report_fails_closed_when_lineage_is_unavailable(self) -> None:
        connection = _new_connection()
        try:
            connection.execute(
                "INSERT INTO match_ingest_status VALUES (9, 'event-1', 9, 'source-9', 'normalizer-v1')"
            )
            connection.execute(
                """INSERT INTO player_map_scores VALUES
                   (9, 0, 99, 1, 99, 99, 1, 1, '{"ranking_eligible":1}', ?)""",
                (SCORE_VERSION,),
            )
            connection.execute(
                "INSERT INTO team_map_states VALUES (9, 'radiant', 'stomp', 'state-9', ?)",
                (LABEL_VERSION,),
            )
            connection.execute(
                """INSERT INTO team_style_profiles VALUES
                   (9, 999, 'cutoff-9', ?, '{}', '[]', '[]',
                    '{"maps":[]}', 9, 'profile-9', '2026-01-09')""",
                (PROFILE_VERSION,),
            )
            _insert_run(connection, "orphan")
            _insert_prediction(connection, "orphan", 9, 0.99, 1)

            report = build_intelligence_report(connection)

            self.assertEqual(report["formal_maps"], 0)
            self.assertEqual(report["player_score_rows"], 1)
            self.assertEqual(report["player_rankings"], [])
            self.assertEqual(report["team_state_distribution"], {})
            self.assertEqual(report["team_profiles"], 1)
            self.assertEqual(report["team_style_profiles"], [])
            self.assertEqual(report["draft_prediction_rows"], 0)
            self.assertEqual(report["draft_metrics"], [])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
