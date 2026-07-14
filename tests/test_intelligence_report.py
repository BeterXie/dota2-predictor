from __future__ import annotations

import sqlite3
import unittest

from event_intelligence.report import build_intelligence_report


class IntelligenceReportTests(unittest.TestCase):
    def test_current_coverage_does_not_sum_algorithm_versions(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE player_map_scores (score_version TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO player_map_scores VALUES (?)",
                (
                    ("player-score-v2+observed-role=role-v1",),
                    ("player-score-v3+observed-role=role-v1",),
                    ("player-score-v3+observed-role=role-v1",),
                ),
            )
            connection.execute(
                """CREATE TABLE draft_model_runs (
                       run_id TEXT PRIMARY KEY,
                       availability_mode TEXT NOT NULL,
                       configuration_json TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE draft_predictions (
                       run_id TEXT NOT NULL,
                       status TEXT NOT NULL
                   )"""
            )
            connection.executemany(
                "INSERT INTO draft_model_runs VALUES (?, ?, ?)",
                (
                    (
                        "v2",
                        "reconstructed_walk_forward",
                        '{"score_version":"player-score-v2+observed-role=role-v1"}',
                    ),
                    (
                        "v3",
                        "reconstructed_walk_forward",
                        '{"score_version":"player-score-v3+observed-role=role-v1"}',
                    ),
                ),
            )
            connection.executemany(
                "INSERT INTO draft_predictions VALUES (?, ?)",
                (("v2", "settled"), ("v3", "settled")),
            )

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


if __name__ == "__main__":
    unittest.main()
