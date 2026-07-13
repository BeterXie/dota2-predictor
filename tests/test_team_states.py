from __future__ import annotations

import unittest

from event_intelligence.team_states import (
    LABEL_VERSION,
    Side,
    TeamObjective,
    TeamStateLabel,
    build_team_map_states,
)


def _curve(duration_minutes: int, segments: tuple[tuple[int, int, int], ...]) -> list[int]:
    values = [0] * duration_minutes
    for start, end, value in segments:
        for minute in range(start, end + 1):
            values[minute] = value
    return values


def _states(
    curve: list[int | None],
    *,
    duration_minutes: int = 31,
    radiant_win: bool = True,
    objectives: tuple[TeamObjective, ...] | None = (),
):
    return build_team_map_states(
        match_id=101,
        duration_seconds=duration_minutes * 60,
        radiant_win=radiant_win,
        radiant_team_id=11,
        dire_team_id=22,
        radiant_gold_adv=curve,
        objectives=objectives,
        source_versions={"opendota": "hash-1"},
    )


class TeamStateLabelTests(unittest.TestCase):
    def test_comeback_and_throw_have_highest_precedence(self) -> None:
        curve = _curve(31, ((10, 12, -5_000), (16, 29, 10_000)))

        radiant, dire = _states(curve)

        self.assertIs(radiant.label, TeamStateLabel.COMEBACK)
        self.assertIs(dire.label, TeamStateLabel.THROW)
        self.assertEqual(radiant.first_significant_deficit_at, 10 * 60)
        self.assertEqual(dire.first_significant_lead_at, 10 * 60)

    def test_stomp_and_stomp_loss_require_exact_sixty_percent(self) -> None:
        curve = _curve(31, ((10, 21, 12_000),))

        radiant, dire = _states(curve)

        self.assertIs(radiant.label, TeamStateLabel.STOMP)
        self.assertIs(dire.label, TeamStateLabel.STOMP_LOSS)

    def test_stomp_must_start_before_minute_twenty(self) -> None:
        starts_at_19 = _curve(41, ((19, 36, 20_000),))
        starts_at_20 = _curve(41, ((20, 37, 20_000),))

        early, _ = _states(starts_at_19, duration_minutes=41)
        late, _ = _states(starts_at_20, duration_minutes=41)

        self.assertIs(early.label, TeamStateLabel.STOMP)
        self.assertIs(late.label, TeamStateLabel.ADVANTAGE)

    def test_advantage_and_disadvantage_include_twenty_five_percent_boundary(self) -> None:
        curve = _curve(31, ((10, 14, 5_000),))

        radiant, dire = _states(curve)

        self.assertEqual(radiant.ahead_fraction, 0.25)
        self.assertIs(radiant.label, TeamStateLabel.ADVANTAGE)
        self.assertIs(dire.label, TeamStateLabel.DISADVANTAGE)

    def test_two_minute_excursion_does_not_create_a_state(self) -> None:
        curve = _curve(31, ((10, 11, -5_000),))

        radiant, dire = _states(curve)

        self.assertIs(radiant.label, TeamStateLabel.EVEN)
        self.assertIs(dire.label, TeamStateLabel.EVEN)
        self.assertIsNone(radiant.first_significant_deficit_at)

    def test_three_minute_excursion_is_sustained(self) -> None:
        curve = _curve(31, ((10, 12, -5_000),))

        radiant, dire = _states(curve)

        self.assertIs(radiant.label, TeamStateLabel.COMEBACK)
        self.assertIs(dire.label, TeamStateLabel.THROW)

    def test_incomplete_smoothing_window_is_unscorable(self) -> None:
        curve: list[int | None] = _curve(31, ())
        curve[17] = None

        radiant, dire = _states(curve)

        self.assertIs(radiant.label, TeamStateLabel.UNSCORABLE)
        self.assertIs(dire.label, TeamStateLabel.UNSCORABLE)
        self.assertEqual(radiant.unscorable_reason, "gold_timeline_incomplete")
        self.assertLess(radiant.curve_coverage, 1.0)
        self.assertIsNone(radiant.signed_auc)


class TeamStateFactTests(unittest.TestCase):
    def test_radiant_and_dire_facts_are_exact_sign_mirrors(self) -> None:
        curve = _curve(31, ((10, 12, -5_000), (16, 20, 7_000)))

        radiant, dire = _states(curve)

        self.assertEqual(radiant.smoothed_curve, tuple((m, -v) for m, v in dire.smoothed_curve))
        self.assertEqual(radiant.max_lead, -dire.max_deficit)
        self.assertEqual(radiant.max_deficit, -dire.max_lead)
        self.assertEqual(radiant.ahead_fraction, dire.behind_fraction)
        self.assertEqual(radiant.behind_fraction, dire.ahead_fraction)
        self.assertEqual(radiant.signed_auc, -dire.signed_auc)
        self.assertEqual(radiant.absolute_auc, dire.absolute_auc)
        self.assertEqual(radiant.input_hash, dire.input_hash)
        self.assertEqual(radiant.label_version, LABEL_VERSION)

    def test_thresholds_crossings_closeout_and_source_versions_are_retained(self) -> None:
        curve = _curve(31, ((10, 12, -11_000), (16, 20, 11_000)))

        radiant, _ = _states(curve)

        self.assertTrue(radiant.threshold(3_000).had_deficit)
        self.assertTrue(radiant.threshold(5_000).had_deficit)
        self.assertTrue(radiant.threshold(10_000).had_deficit)
        self.assertTrue(radiant.threshold(10_000).had_lead)
        self.assertEqual(radiant.closeout_seconds, 31 * 60 - 16 * 60)
        self.assertGreaterEqual(len(radiant.crossings), 2)
        self.assertEqual(radiant.source_versions, (("opendota", "hash-1"),))
        self.assertEqual(len(radiant.input_hash), 64)

    def test_objective_conversion_is_one_map_level_opportunity(self) -> None:
        objectives = (
            TeamObjective(700, Side.RADIANT, "roshan"),
            TeamObjective(800, Side.RADIANT, "roshan"),
            TeamObjective(850, Side.RADIANT, "tower"),
            TeamObjective(1_000, Side.RADIANT, "high_ground"),
        )

        radiant, dire = _states(_curve(31, ()), objectives=objectives)

        conversion = radiant.objective_conversion
        self.assertTrue(conversion.source_complete)
        self.assertTrue(conversion.roshan_opportunity)
        self.assertTrue(conversion.tower_after_roshan)
        self.assertEqual(conversion.tower_after_roshan_seconds, 150)
        self.assertTrue(conversion.high_ground_after_roshan)
        self.assertEqual(conversion.high_ground_after_roshan_seconds, 300)
        self.assertTrue(conversion.win_after_roshan)
        self.assertFalse(dire.objective_conversion.roshan_opportunity)

    def test_missing_objective_source_stays_unknown(self) -> None:
        radiant, _ = _states(_curve(31, ()), objectives=None)

        self.assertFalse(radiant.objective_conversion.source_complete)
        self.assertIsNone(radiant.objective_conversion.roshan_opportunity)


if __name__ == "__main__":
    unittest.main()
