from __future__ import annotations

import copy
import unittest

from event_intelligence.facts import (
    ComponentStatus,
    MatchIdentityError,
    extract_completed_match_facts,
)


def _player(slot: int, hero_id: int) -> dict:
    return {
        "account_id": 10_000 + slot,
        "player_slot": slot,
        "hero_id": hero_id,
        "kills": 0,
        "deaths": 1,
        "assists": 12,
        "gold_per_min": 500,
        "xp_per_min": 600,
        "net_worth": 15_000,
        "last_hits": 200,
        "denies": 5,
        "hero_damage": 20_000,
        "hero_healing": 0,
        "tower_damage": 2_000,
        "damage_taken": {"npc_dota_hero_axe": 900},
        "stuns": 12.5,
        "camps_stacked": 0,
        "rune_pickups": 3,
        "obs_placed": 0,
        "sen_placed": 2,
        "observer_kills": 1,
        "sentry_kills": 0,
        "lane_role": 1,
        "is_roaming": False,
        "gold_t": list(range(11)),
        "lh_t": [value * 2 for value in range(11)],
        "xp_t": [value * 3 for value in range(11)],
        "kills_log": [{"time": 599, "key": "npc_dota_hero_axe"}],
        "obs_log": [],
        "sen_log": [{"time": 600}],
        "buyback_log": [],
    }


def complete_match() -> dict:
    slots = [0, 1, 2, 3, 4, 128, 129, 130, 131, 132]
    picks = [
        {
            "is_pick": True,
            "hero_id": hero_id,
            "team": 0 if index < 5 else 1,
            "order": index,
        }
        for index, hero_id in enumerate(range(1, 11))
    ]
    return {
        "match_id": 8_001,
        "leagueid": 19_543,
        "series_id": 77,
        "series_type": 2,
        "start_time": 1_789_000_000,
        "duration": 1_800,
        "radiant_win": True,
        "radiant_team_id": 101,
        "dire_team_id": 202,
        "radiant_score": 31,
        "dire_score": 18,
        "patch": 60,
        "version": 21,
        "players": [_player(slot, hero_id) for slot, hero_id in zip(slots, range(1, 11))],
        "picks_bans": picks + [
            {"is_pick": False, "hero_id": 20, "team": 0, "order": 10}
        ],
        "radiant_gold_adv": [minute * 100 for minute in range(30)],
        "radiant_xp_adv": [minute * 80 for minute in range(30)],
        "objectives": [
            {
                "time": 601,
                "type": "CHAT_MESSAGE_ROSHAN_KILL",
                "team": 2,
                "key": "npc_dota_roshan",
            }
        ],
        "teamfights": [],
    }


class CompletedMatchFactsTests(unittest.TestCase):
    def test_extracts_exact_complete_facts_and_component_readiness(self) -> None:
        facts = extract_completed_match_facts(complete_match(), expected_match_id=8_001)

        self.assertEqual(facts.match_id, 8_001)
        self.assertEqual(facts.league_id, 19_543)
        self.assertEqual(len(facts.players), 10)
        self.assertEqual(len(facts.picks_bans), 11)
        self.assertEqual(len(facts.radiant_gold_adv or ()), 30)
        self.assertEqual(facts.completeness.player_count, 10)
        self.assertEqual(facts.completeness.pick_count, 10)
        self.assertTrue(facts.completeness.ten_players)
        self.assertTrue(facts.completeness.ten_picks)
        self.assertTrue(facts.completeness.gold_timeline)
        self.assertTrue(facts.completeness.objectives)
        self.assertEqual(facts.readiness.player_scoring.status, ComponentStatus.READY)
        self.assertEqual(facts.readiness.draft_model.status, ComponentStatus.READY)
        self.assertEqual(facts.readiness.team_state.status, ComponentStatus.READY)
        self.assertEqual(facts.readiness.objective_analysis.status, ComponentStatus.READY)
        self.assertEqual(len(facts.source_schema_fingerprint), 64)

        player = facts.players[0]
        self.assertEqual(player.kills, 0)
        self.assertEqual(player.hero_healing, 0)
        self.assertEqual(player.gold_at_10, 10)
        self.assertEqual(player.last_hits_at_10, 20)
        self.assertEqual(player.xp_at_10, 30)
        self.assertEqual(player.kills_at_10, 1)
        self.assertIsNone(player.deaths_at_10)
        self.assertIsNone(player.assists_at_10)
        self.assertEqual(player.observer_wards_at_10, 0)
        self.assertEqual(player.sentry_wards_at_10, 1)
        self.assertEqual(player.damage_taken, {"npc_dota_hero_axe": 900})

    def test_missing_source_fields_remain_none_and_reduce_readiness(self) -> None:
        payload = complete_match()
        payload["players"] = payload["players"][:-1]
        payload["picks_bans"] = [
            action
            for action in payload["picks_bans"]
            if not (action["is_pick"] and action["hero_id"] == 10)
        ]
        payload["radiant_gold_adv"][14] = None
        del payload["objectives"]
        for field in (
            "kills",
            "hero_damage",
            "damage_taken",
            "obs_placed",
            "obs_log",
            "gold_t",
        ):
            payload["players"][0].pop(field, None)

        facts = extract_completed_match_facts(payload)

        self.assertFalse(facts.completeness.ten_players)
        self.assertFalse(facts.completeness.ten_picks)
        self.assertFalse(facts.completeness.gold_timeline)
        self.assertFalse(facts.completeness.objectives)
        self.assertIsNone(facts.players[0].kills)
        self.assertIsNone(facts.players[0].hero_damage)
        self.assertIsNone(facts.players[0].damage_taken)
        self.assertIsNone(facts.players[0].observer_wards_placed)
        self.assertIsNone(facts.players[0].observer_wards_at_10)
        self.assertIsNone(facts.players[0].gold_at_10)
        self.assertEqual(facts.readiness.player_scoring.status, ComponentStatus.RETRYABLE)
        self.assertEqual(facts.readiness.draft_model.status, ComponentStatus.RETRYABLE)
        self.assertEqual(
            facts.readiness.team_state.status, ComponentStatus.UNSCORABLE
        )
        self.assertEqual(
            facts.readiness.objective_analysis.status,
            ComponentStatus.RETRYABLE,
        )

    def test_unparsed_version_is_retryable_even_when_summary_fields_exist(self) -> None:
        payload = complete_match()
        payload["version"] = None

        unparsed = extract_completed_match_facts(payload)
        payload["version"] = 21
        parsed = extract_completed_match_facts(payload)

        self.assertFalse(unparsed.completeness.source_parsed)
        self.assertEqual(
            unparsed.readiness.player_scoring.status,
            ComponentStatus.RETRYABLE,
        )
        self.assertEqual(parsed.readiness.player_scoring.status, ComponentStatus.READY)

    def test_never_prorates_end_of_match_totals_or_uses_last_early_sample(self) -> None:
        payload = complete_match()
        player = payload["players"][0]
        player["assists"] = 30
        player["observer_kills"] = 10
        player["gold_t"] = [100, 200, 300]

        facts = extract_completed_match_facts(payload)
        extracted = facts.players[0]

        self.assertIsNone(extracted.assists_at_10)
        self.assertIsNone(extracted.observer_kills_at_10)
        self.assertIsNone(extracted.gold_at_10)

    def test_duplicate_player_slots_and_picks_are_not_complete(self) -> None:
        payload = complete_match()
        payload["players"][-1]["player_slot"] = 0
        payload["picks_bans"][9]["hero_id"] = 1

        facts = extract_completed_match_facts(payload)

        self.assertEqual(facts.completeness.player_count, 10)
        self.assertEqual(facts.completeness.pick_count, 10)
        self.assertFalse(facts.completeness.ten_players)
        self.assertFalse(facts.completeness.ten_picks)

    def test_empty_objectives_are_incomplete_not_evidence_of_no_objectives(self) -> None:
        payload = complete_match()
        payload["objectives"] = []

        facts = extract_completed_match_facts(payload)

        self.assertFalse(facts.completeness.objectives)
        self.assertEqual(
            facts.readiness.objective_analysis.status,
            ComponentStatus.RETRYABLE,
        )

    def test_picks_must_match_player_heroes_and_sides(self) -> None:
        wrong_hero = complete_match()
        wrong_hero["picks_bans"][0]["hero_id"] = 99
        wrong_side = complete_match()
        wrong_side["picks_bans"][0]["team"] = 1
        wrong_side["picks_bans"][5]["team"] = 0

        self.assertFalse(extract_completed_match_facts(wrong_hero).completeness.ten_picks)
        self.assertFalse(extract_completed_match_facts(wrong_side).completeness.ten_picks)

    def test_player_and_postmatch_readiness_require_exact_component_sources(self) -> None:
        payload = complete_match()
        del payload["players"][0]["hero_damage"]
        del payload["players"][1]["buyback_log"]
        del payload["teamfights"]

        facts = extract_completed_match_facts(payload)

        self.assertEqual(
            facts.readiness.player_scoring.status,
            ComponentStatus.RETRYABLE,
        )
        self.assertIn("teamfights_missing", facts.readiness.postmatch_attribution.reasons)
        self.assertIn("buyback_logs_missing", facts.readiness.postmatch_attribution.reasons)

    def test_match_identity_mismatch_is_rejected_before_normalization(self) -> None:
        payload = copy.deepcopy(complete_match())
        with self.assertRaises(MatchIdentityError):
            extract_completed_match_facts(payload, expected_match_id=9_999)


if __name__ == "__main__":
    unittest.main()
