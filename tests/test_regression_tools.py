from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prematch.scorer import _validate_lineups, _validated_weights
from scripts import grid_search
from scripts.regression_test import (
    _causal_backtest_weights,
    _parse_weights,
    get_matches,
)


class RegressionDataTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = Path(handle.name)
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            CREATE TABLE matches (
                match_id INTEGER PRIMARY KEY,
                radiant_team_id INTEGER,
                dire_team_id INTEGER,
                radiant_win INTEGER,
                start_time INTEGER
            );
            CREATE TABLE match_players (
                match_id INTEGER,
                hero_id INTEGER,
                is_radiant INTEGER,
                account_id INTEGER,
                player_slot INTEGER
            );
            CREATE TABLE picks_bans (match_id INTEGER);
            CREATE TABLE teamfights (match_id INTEGER);
            CREATE TABLE teamfight_players (teamfight_id INTEGER);
            CREATE TABLE gold_advantage (match_id INTEGER);
            CREATE TABLE xp_advantage (match_id INTEGER);
            CREATE TABLE objectives (match_id INTEGER);
            CREATE TABLE chat (match_id INTEGER);
            """
        )
        connection.execute(
            "INSERT INTO matches VALUES (1, 10, 20, 1, 100)"
        )
        rows = []
        for index, hero_id in enumerate((50, 10, 40, 20, 30)):
            rows.append((1, hero_id, 1, 1_000 + hero_id, index))
        for index, hero_id in enumerate((100, 60, 90, 70, 80), start=128):
            rows.append((1, hero_id, 0, 2_000 + hero_id, index))
        connection.executemany(
            "INSERT INTO match_players VALUES (?, ?, ?, ?, ?)", rows
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)

    def test_match_loader_keeps_each_account_paired_with_its_hero(self) -> None:
        match = get_matches(str(self.path))[0]
        self.assertEqual(match["radiant_heroes"], [50, 10, 40, 20, 30])
        self.assertEqual(
            list(zip(match["radiant_heroes"], match["radiant_players"])),
            [(hero_id, 1_000 + hero_id) for hero_id in (50, 10, 40, 20, 30)],
        )
        self.assertEqual(
            list(zip(match["dire_heroes"], match["dire_players"])),
            [(hero_id, 2_000 + hero_id) for hero_id in (100, 60, 90, 70, 80)],
        )

    def test_weight_parser_uses_all_current_components(self) -> None:
        weights = _parse_weights("0.4,0.2,0.1,0.2,0.1")
        self.assertEqual(set(weights), set(grid_search.COMPONENT_KEYS))
        with self.assertRaisesRegex(ValueError, "exactly 5"):
            _parse_weights("0.4,0.3,0.2,0.1")
        with self.assertRaisesRegex(ValueError, "unknown=h2h"):
            _validated_weights({
                "hero_matchup": 0.4,
                "team_form": 0.3,
                "h2h": 0.2,
                "team_strength": 0.1,
            })

    def test_causal_backtest_rejects_unversioned_static_features(self) -> None:
        with self.assertRaisesRegex(ValueError, "draft_profile,hero_matchup"):
            _causal_backtest_weights(
                {
                    "hero_matchup": 0.1,
                    "team_form": 0.3,
                    "draft_profile": 0.1,
                    "player_skill": 0.4,
                    "early_game": 0.1,
                }
            )

    def test_scorer_rejects_duplicate_or_partial_lineups(self) -> None:
        with self.assertRaisesRegex(ValueError, "10 distinct"):
            _validate_lineups(
                10,
                20,
                [1, 2, 3, 4, 5],
                [5, 6, 7, 8, 9],
                None,
                None,
            )
        with self.assertRaisesRegex(ValueError, "both sides"):
            _validate_lineups(
                10,
                20,
                [1, 2, 3, 4, 5],
                [6, 7, 8, 9, 10],
                [11, 12, 13, 14, 15],
                None,
            )

    def test_rolling_window_is_applied_and_static_features_are_not_read(self) -> None:
        connection = sqlite3.connect(self.path)
        for match_id in (2, 3, 4):
            connection.execute(
                "INSERT INTO matches VALUES (?, 10, 20, ?, ?)",
                (match_id, match_id % 2, match_id * 100),
            )
            connection.execute(
                "INSERT INTO match_players VALUES (?, 1, 1, ?, 0)",
                (match_id, match_id),
            )
        connection.commit()
        connection.close()
        matches = [
            {
                "match_id": match_id,
                "radiant_team_id": 10,
                "dire_team_id": 20,
                "radiant_win": bool(match_id % 2),
                "start_time": match_id * 100,
                "radiant_heroes": [1],
                "dire_heroes": [2],
                "radiant_players": [],
                "dire_players": [],
            }
            for match_id in (1, 2, 3, 4)
        ]
        original_delete = grid_search._delete_match
        with (
            patch.object(
                grid_search.S,
                "_compute_hero_matchup_score",
                side_effect=AssertionError("static hero feature was read"),
            ),
            patch.object(
                grid_search.S,
                "_compute_draft_profile",
                side_effect=AssertionError("static draft feature was read"),
            ),
            patch.object(
                grid_search.S,
                "_compute_team_form",
                return_value={"score": 0.2, "confidence": 1.0},
            ),
            patch.object(
                grid_search, "_delete_match", wraps=original_delete
            ) as delete_match,
        ):
            scores = grid_search.compute_rolling_scores(
                str(self.path), matches, window=1
            )
        self.assertEqual(len(scores), 3)
        self.assertEqual(delete_match.call_count, 3)
        self.assertTrue(all(row["hero_matchup_conf"] == 0.0 for row in scores))
        self.assertTrue(all(row["draft_profile_conf"] == 0.0 for row in scores))


class GridEvaluationTests(unittest.TestCase):
    def test_sigmoid_scale_is_selected_by_probability_loss(self) -> None:
        scores = [{
            "actual": True,
            "team_form": 0.4,
            "team_form_conf": 1.0,
            "hero_matchup": 0.0,
            "hero_matchup_conf": 0.0,
            "draft_profile": 0.0,
            "draft_profile_conf": 0.0,
            "player_skill": 0.0,
            "player_skill_conf": 0.0,
            "early_game": 0.0,
            "early_game_conf": 0.0,
        }]
        weights = {name: 0.0 for name in grid_search.COMPONENT_KEYS}
        weights["team_form"] = 1.0
        low = grid_search.evaluate(scores, weights, sigmoid_scale=0.5)
        high = grid_search.evaluate(scores, weights, sigmoid_scale=5.0)
        self.assertEqual(low[:3], high[:3])
        self.assertLess(high[3], low[3])
        self.assertLess(high[4], low[4])


if __name__ == "__main__":
    unittest.main()
