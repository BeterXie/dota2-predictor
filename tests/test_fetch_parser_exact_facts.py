from __future__ import annotations

import math
import unittest

from fetch.parser import (
    _count_events_before,
    _extract_10min_value,
    parse_match_basic,
    parse_players,
    parse_teamfights,
)


class ExactEarlyFactHelperTests(unittest.TestCase):
    def test_minute_ten_uses_index_ten_without_coercion(self) -> None:
        values = list(range(10)) + [123.5]

        self.assertEqual(_extract_10min_value(values), 123.5)
        self.assertNotEqual(_extract_10min_value(values), values[9])

    def test_minute_ten_requires_a_real_finite_numeric_sample(self) -> None:
        self.assertIsNone(_extract_10min_value(None))
        self.assertIsNone(_extract_10min_value("not-a-list"))
        self.assertIsNone(_extract_10min_value([]))
        self.assertIsNone(_extract_10min_value(list(range(10))))
        large_integer = 10**1_000
        self.assertEqual(
            _extract_10min_value(list(range(10)) + [large_integer]), large_integer
        )

        for invalid in (None, True, "100", math.nan, math.inf, -math.inf):
            values = list(range(10)) + [invalid]
            self.assertIsNone(_extract_10min_value(values), invalid)

    def test_event_count_distinguishes_missing_empty_and_exact_logs(self) -> None:
        self.assertIsNone(_count_events_before(None))
        self.assertIsNone(_count_events_before({"time": 1}))
        self.assertEqual(_count_events_before([]), 0)
        self.assertEqual(
            _count_events_before(
                [{"time": -90}, {"time": 599.5}, {"time": 600}, {"time": 601}]
            ),
            3,
        )

    def test_any_malformed_event_makes_the_log_unknown(self) -> None:
        malformed_logs = (
            [None],
            [{}],
            [{"time": None}],
            [{"time": True}],
            [{"time": "599"}],
            [{"time": math.nan}],
            [{"time": math.inf}],
            [{"time": 10}, {"key": "missing-time"}],
        )

        for log in malformed_logs:
            self.assertIsNone(_count_events_before(log), log)


class ParsePlayersExactEarlyFactsTests(unittest.TestCase):
    @staticmethod
    def _match(player: dict) -> dict:
        return {
            "match_id": 8_001,
            "duration": 3_600,
            "radiant_team_id": 101,
            "dire_team_id": 202,
            "players": [player],
        }

    @staticmethod
    def _player() -> dict:
        return {
            "account_id": 55,
            "player_slot": 0,
            "hero_id": 1,
            "kills": 40,
            "deaths": 30,
            "assists": 50,
            "observer_kills": 20,
            "sentry_kills": 10,
            "gold_per_min": 999,
            "gold_t": list(range(11)),
            "lh_t": [value * 2 for value in range(11)],
            "xp_t": [value * 3 for value in range(11)],
            "kills_log": [{"time": 600}, {"time": 601}],
            "deaths_log": [{"time": 599}],
            "assists_log": [],
            "obs_log": [{"time": -30}, {"time": 700}],
            "sen_log": [],
            "observer_kills_log": [{"time": 100}],
            "sentry_kills_log": [{"time": 601}],
        }

    def test_parse_players_emits_only_exact_early_values(self) -> None:
        row = parse_players(self._match(self._player()))[0]

        self.assertEqual(row["gold_10min"], 10)
        self.assertEqual(row["lh_10min"], 20)
        self.assertEqual(row["xp_10min"], 30)
        self.assertEqual(row["kills_10min"], 1)
        self.assertEqual(row["deaths_10min"], 1)
        self.assertEqual(row["assists_10min"], 0)
        self.assertEqual(row["obs_placed_10min"], 1)
        self.assertEqual(row["sen_placed_10min"], 0)
        self.assertEqual(row["observer_kills_10min"], 1)
        self.assertEqual(row["sentry_kills_10min"], 0)

    def test_final_totals_duration_and_gpm_never_fill_missing_logs(self) -> None:
        player = self._player()
        for field in (
            "kills_log",
            "deaths_log",
            "assists_log",
            "obs_log",
            "sen_log",
            "observer_kills_log",
            "sentry_kills_log",
        ):
            player.pop(field)

        row = parse_players(self._match(player))[0]

        for field in (
            "kills_10min",
            "deaths_10min",
            "assists_10min",
            "obs_placed_10min",
            "sen_placed_10min",
            "observer_kills_10min",
            "sentry_kills_10min",
        ):
            self.assertIsNone(row[field], field)
        self.assertEqual(row["kills"], 40)
        self.assertEqual(row["assists"], 50)
        self.assertEqual(row["gold_per_min"], 999)

    def test_malformed_log_does_not_affect_other_exact_fields(self) -> None:
        player = self._player()
        player["kills_log"] = [{"time": 1}, {"time": "bad"}]

        row = parse_players(self._match(player))[0]

        self.assertIsNone(row["kills_10min"])
        self.assertEqual(row["deaths_10min"], 1)
        self.assertEqual(row["assists_10min"], 0)

    def test_missing_summary_slot_and_teamfight_values_remain_unknown(self) -> None:
        match = self._match({"hero_id": 1})
        match["teamfights"] = [{"players": [{"player_slot": 0}]}]

        player = parse_players(match)[0]
        summary = parse_match_basic(match)
        _, teamfight_players = parse_teamfights(match)

        self.assertIsNone(player["player_slot"])
        self.assertIsNone(player["is_radiant"])
        self.assertIsNone(player["team_id"])
        for field in ("kills", "deaths", "assists"):
            self.assertIsNone(player[field])
        self.assertIsNone(summary["stomp"])
        self.assertIsNone(summary["comeback"])
        for field in (
            "deaths", "buybacks", "damage", "healing", "gold_delta", "xp_delta", "kills"
        ):
            self.assertIsNone(teamfight_players[0][field])


if __name__ == "__main__":
    unittest.main()
