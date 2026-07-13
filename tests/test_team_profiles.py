from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile

from event_intelligence.team_profiles import (
    CLOSEOUT_5K_RATE,
    REACH_40_RATE,
    REACH_50_RATE,
    AvailabilityMode,
    BetaPrior,
    ProfileMap,
    build_team_style_profile,
    comeback_metric,
    derive_causal_event_patch_priors,
    throw_metric,
)
from event_intelligence.team_states import Side, TeamObjective, build_team_map_states
from event_intelligence.storage import IntelligenceStorage
from fetch.db import Database
from scripts.build_strict_team_profiles import (
    StrictMap,
    _load_strict_maps,
    _objectives,
    _opponent_strength,
    build_strict_profiles,
)


UTC = timezone.utc
CUTOFF = datetime(2026, 7, 13, 12, tzinfo=UTC)


def _state(
    match_id: int,
    *,
    duration_minutes: int = 31,
    won: bool = True,
    segments: tuple[tuple[int, int, int], ...] = (),
):
    curve = [0] * duration_minutes
    for start, end, value in segments:
        for minute in range(start, end + 1):
            curve[minute] = value
    return build_team_map_states(
        match_id=match_id,
        duration_seconds=duration_minutes * 60,
        radiant_win=won,
        radiant_team_id=1,
        dire_team_id=2,
        radiant_gold_adv=curve,
        objectives=(),
        source_versions={"opendota": f"hash-{match_id}"},
    )[0]


def _profile_map(
    state,
    *,
    completed_at: datetime = CUTOFF - timedelta(days=1),
    first_usable_at: datetime | None = CUTOFF - timedelta(hours=1),
    patch: int | None = 60,
    roster: tuple[int, ...] = (1, 2, 3, 4, 5),
    opponent_weight: float = 1.0,
    event_weight: float = 1.0,
) -> ProfileMap:
    return ProfileMap(
        state=state,
        completed_at=completed_at,
        first_usable_at=first_usable_at,
        event_id="event-a",
        patch=patch,
        roster=roster,
        opponent_strength_weight=opponent_weight,
        event_strength_weight=event_weight,
    )


class TeamProfileOpportunityTests(unittest.TestCase):
    def test_one_map_creates_one_opportunity_per_threshold(self) -> None:
        state = _state(1, segments=((10, 12, -12_000),))

        profile = build_team_style_profile(
            team_id=1, cutoff=CUTOFF, maps=[_profile_map(state)]
        )

        for threshold in (3_000, 5_000, 10_000):
            rate = profile.rate(comeback_metric(threshold))
            self.assertEqual(rate.opportunities, 1)
            self.assertEqual(rate.successes, 1)
        self.assertEqual(profile.rate(REACH_40_RATE).opportunities, 1)
        self.assertEqual(profile.rate(REACH_50_RATE).opportunities, 1)

    def test_lead_creates_throw_and_closeout_opportunities_once(self) -> None:
        state = _state(2, won=False, segments=((10, 20, 12_000),))

        profile = build_team_style_profile(
            team_id=1, cutoff=CUTOFF, maps=[_profile_map(state)]
        )

        for threshold in (3_000, 5_000, 10_000):
            rate = profile.rate(throw_metric(threshold))
            self.assertEqual(rate.opportunities, 1)
            self.assertEqual(rate.successes, 1)
        closeout = profile.rate(CLOSEOUT_5K_RATE)
        self.assertEqual(closeout.opportunities, 1)
        self.assertEqual(closeout.successes, 0)

    def test_beta_binomial_uses_weighted_opportunities_and_applicable_prior(self) -> None:
        state = _state(3, segments=((10, 12, -5_000),))
        prior = BetaPrior(2.0, 3.0, "event-a/patch-60")

        profile = build_team_style_profile(
            team_id=1,
            cutoff=CUTOFF,
            maps=[_profile_map(state)],
            priors={comeback_metric(3_000): prior},
        )

        rate = profile.rate(comeback_metric(3_000))
        self.assertAlmostEqual(rate.posterior_alpha, 2.0 + rate.weighted_successes)
        self.assertAlmostEqual(
            rate.posterior_beta,
            3.0 + rate.weighted_opportunities - rate.weighted_successes,
        )
        self.assertEqual(rate.prior_scope, "event-a/patch-60")
        self.assertGreater(rate.mean, prior.alpha / (prior.alpha + prior.beta))


class TeamProfileWeightingTests(unittest.TestCase):
    def test_all_approved_weight_components_are_multiplied(self) -> None:
        state = _state(10)
        row = _profile_map(
            state,
            completed_at=CUTOFF - timedelta(days=45),
            patch=59,
            roster=(1, 2, 3, 8, 9),
            opponent_weight=1.2,
            event_weight=0.8,
        )

        profile = build_team_style_profile(
            team_id=1,
            cutoff=CUTOFF,
            maps=[row],
            target_roster=(1, 2, 3, 4, 5),
            target_patch=60,
        )

        weight = profile.weighting[0]
        self.assertAlmostEqual(weight.age_weight, 0.5)
        self.assertAlmostEqual(weight.roster_overlap_weight, 0.6)
        self.assertAlmostEqual(weight.patch_weight, 0.5)
        self.assertAlmostEqual(weight.total_weight, 0.5 * 0.6 * 0.5 * 1.2 * 0.8)
        self.assertAlmostEqual(profile.effective_sample_size, 1.0)

    def test_causal_priors_exclude_target_team_and_audit_sparse_fallback(self) -> None:
        target = _profile_map(
            replace(
                _state(11, segments=((10, 12, -5_000),)),
                team_id=999,
                opponent_id=998,
            )
        )
        others = [
            _profile_map(
                _state(match_id, segments=((10, 12, -5_000),)),
                completed_at=CUTOFF - timedelta(days=match_id),
            )
            for match_id in range(12, 17)
        ]
        priors = derive_causal_event_patch_priors(
            team_id=999,
            cutoff=CUTOFF,
            maps=[target, *others],
            target_event_id="event-a",
            target_patch=60,
        )

        prior = priors[comeback_metric(3_000)]
        self.assertEqual(prior.scope, "event_patch:event-a:60:n=5")
        self.assertNotEqual(prior.alpha, prior.beta)
        self.assertEqual(tuple(row[0] for row in prior.evidence), tuple(range(12, 17)))
        self.assertEqual(
            priors[throw_metric(10_000)].scope,
            "neutral:insufficient_causal_event_patch_data",
        )


class StrictObjectiveNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE objectives (
                   id INTEGER PRIMARY KEY, match_id INTEGER, time INTEGER,
                   type TEXT, key TEXT, player_slot INTEGER)"""
        )
        self.match = StrictMap(
            match_id=7001,
            event_id="event-a",
            start_time=1,
            duration=2_000,
            radiant_win=True,
            radiant_team_id=1,
            dire_team_id=2,
            patch=60,
            first_usable_at=CUTOFF,
            source_version="hash",
            objective_source_complete=True,
            event_tier="tier_1",
            prize_pool_usd=1_000_000,
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_roshan_kill_aegis_and_stolen_are_one_stolen_side_opportunity(self) -> None:
        self.connection.executemany(
            """INSERT INTO objectives(match_id, time, type, player_slot)
               VALUES (7001, ?, ?, ?)""",
            [
                (900, "CHAT_MESSAGE_ROSHAN_KILL", 0),
                (900, "CHAT_MESSAGE_AEGIS", 0),
                (901, "CHAT_MESSAGE_AEGIS_STOLEN", 129),
            ],
        )

        objectives = _objectives(self.connection, self.match)

        self.assertEqual(objectives, (TeamObjective(901, Side.DIRE, "roshan"),))

    def test_delayed_aegis_is_same_roshan_and_denied_aegis_is_no_opportunity(self) -> None:
        self.connection.executemany(
            """INSERT INTO objectives(match_id, time, type, player_slot)
               VALUES (7001, ?, ?, ?)""",
            [
                (900, "CHAT_MESSAGE_ROSHAN_KILL", None),
                (970, "CHAT_MESSAGE_AEGIS", 0),
                (1_500, "CHAT_MESSAGE_ROSHAN_KILL", None),
                (1_501, "CHAT_MESSAGE_DENIED_AEGIS", 129),
            ],
        )

        objectives = _objectives(self.connection, self.match)

        self.assertEqual(objectives, (TeamObjective(970, Side.RADIANT, "roshan"),))

    def test_t3_t4_and_barracks_are_high_ground_and_key_controls_side(self) -> None:
        self.connection.executemany(
            """INSERT INTO objectives(match_id, time, type, key, player_slot)
               VALUES (7001, ?, 'building_kill', ?, ?)""",
            [
                (1_000, "npc_dota_badguys_tower3_mid", 129),
                (1_010, "npc_dota_goodguys_tower4", 0),
                (1_020, "npc_dota_badguys_melee_rax_top", None),
            ],
        )

        objectives = _objectives(self.connection, self.match)

        self.assertEqual(
            objectives,
            (
                TeamObjective(1_000, Side.RADIANT, "high_ground"),
                TeamObjective(1_010, Side.DIRE, "high_ground"),
                TeamObjective(1_020, Side.RADIANT, "high_ground"),
            ),
        )

    def test_unknown_building_key_cannot_be_substring_matched(self) -> None:
        self.connection.execute(
            """INSERT INTO objectives(match_id, time, type, key)
               VALUES (7001, 1000, 'building_kill',
                       'npc_dota_badguys_tower10_mid')"""
        )

        self.assertIsNone(_objectives(self.connection, self.match))

    def test_conflicting_same_priority_roshan_ownership_is_incomplete(self) -> None:
        self.connection.executemany(
            """INSERT INTO objectives(match_id, time, type, player_slot)
               VALUES (7001, ?, ?, ?)""",
            [
                (900, "CHAT_MESSAGE_ROSHAN_KILL", None),
                (901, "CHAT_MESSAGE_AEGIS_STOLEN", 0),
                (902, "AEGIS_STOLEN", 129),
            ],
        )

        self.assertIsNone(_objectives(self.connection, self.match))

    def test_unknown_or_side_less_objective_makes_source_incomplete(self) -> None:
        self.connection.execute(
            """INSERT INTO objectives(match_id, time, type)
               VALUES (7001, 900, 'CHAT_MESSAGE_ROSHAN_KILL')"""
        )
        self.assertIsNone(_objectives(self.connection, self.match))
        self.connection.execute("DELETE FROM objectives")
        self.connection.execute(
            """INSERT INTO objectives(match_id, time, type)
               VALUES (7001, 900, 'NEW_OBJECTIVE_TYPE')"""
        )
        self.assertIsNone(_objectives(self.connection, self.match))

    def test_future_and_unavailable_maps_cannot_change_an_earlier_profile(self) -> None:
        base = _profile_map(_state(20))
        future_completed = _profile_map(
            _state(21), completed_at=CUTOFF + timedelta(seconds=1)
        )
        future_available = _profile_map(
            _state(22), first_usable_at=CUTOFF + timedelta(seconds=1)
        )
        unavailable = _profile_map(_state(23), first_usable_at=None)

        before = build_team_style_profile(team_id=1, cutoff=CUTOFF, maps=[base])
        after = build_team_style_profile(
            team_id=1,
            cutoff=CUTOFF,
            maps=[future_available, base, unavailable, future_completed],
        )

        self.assertEqual(before, after)

    def test_opponent_strength_uses_only_history_available_by_profile_cutoff(self) -> None:
        target = _profile_map(
            replace(_state(26), opponent_id=2),
            completed_at=CUTOFF - timedelta(days=1),
            first_usable_at=CUTOFF - timedelta(days=1) + timedelta(minutes=1),
        )
        earlier = _profile_map(
            replace(_state(27), team_id=2, opponent_id=3, won=True),
            completed_at=CUTOFF - timedelta(hours=2),
            first_usable_at=CUTOFF - timedelta(hours=1),
        )
        too_late = _profile_map(
            replace(_state(28), team_id=2, opponent_id=4, won=False),
            completed_at=CUTOFF - timedelta(hours=3),
            first_usable_at=CUTOFF + timedelta(seconds=1),
        )

        weight, scope, evidence = _opponent_strength(
            target, [target, earlier, too_late], CUTOFF
        )

        self.assertAlmostEqual(weight, 0.5 + 2 / 3)
        self.assertEqual(scope, "causal_cutoff_beta_1_1:n=1")
        self.assertEqual(evidence[0][0], 27)
        self.assertEqual(evidence[0][1], earlier.state.input_hash)

    def test_reconstructed_mode_can_use_missing_availability_timestamp(self) -> None:
        row = _profile_map(_state(24), first_usable_at=None)

        prospective = build_team_style_profile(
            team_id=1, cutoff=CUTOFF, maps=[row]
        )
        reconstructed = build_team_style_profile(
            team_id=1,
            cutoff=CUTOFF,
            maps=[row],
            availability_mode=AvailabilityMode.RECONSTRUCTED,
        )

        self.assertEqual(prospective.rate(REACH_40_RATE).opportunities, 0)
        self.assertEqual(reconstructed.rate(REACH_40_RATE).opportunities, 1)

    def test_availability_cannot_precede_map_completion(self) -> None:
        completed = CUTOFF - timedelta(days=1)
        row = _profile_map(
            _state(25),
            completed_at=completed,
            first_usable_at=completed - timedelta(seconds=1),
        )

        with self.assertRaisesRegex(ValueError, "precedes map completion"):
            build_team_style_profile(team_id=1, cutoff=CUTOFF, maps=[row])


class TeamProfileQuantileTests(unittest.TestCase):
    def test_weighted_quantiles_are_stored_by_result_and_state(self) -> None:
        rows = [
            _profile_map(_state(30, duration_minutes=20, won=True)),
            _profile_map(_state(31, duration_minutes=30, won=False)),
            _profile_map(_state(32, duration_minutes=40, won=True)),
            _profile_map(_state(33, duration_minutes=50, won=False)),
            _profile_map(
                _state(34, won=True, segments=((10, 14, 5_000),))
            ),
            _profile_map(
                _state(35, won=False, segments=((10, 14, -5_000),))
            ),
        ]

        profile = build_team_style_profile(team_id=1, cutoff=CUTOFF, maps=rows)

        even = profile.quantiles("even")
        self.assertEqual(even.count, 4)
        self.assertEqual((even.p25, even.p50, even.p75), (1_200.0, 1_800.0, 2_400.0))
        self.assertEqual(profile.quantiles("win").count, 3)
        self.assertEqual(profile.quantiles("loss").count, 3)
        self.assertEqual(profile.quantiles("advantage").count, 1)
        self.assertEqual(profile.quantiles("disadvantage").count, 1)

    def test_input_order_and_exact_duplicates_do_not_change_profile(self) -> None:
        first = _profile_map(_state(40))
        second = _profile_map(_state(41, duration_minutes=40))

        normal = build_team_style_profile(
            team_id=1, cutoff=CUTOFF, maps=[first, second]
        )
        reordered = build_team_style_profile(
            team_id=1, cutoff=CUTOFF, maps=[second, first, first]
        )

        self.assertEqual(normal, reordered)
        self.assertEqual(len(normal.input_hash), 64)


class StrictTeamProfileCliTests(unittest.TestCase):
    def test_builds_only_formal_maps_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "strict.db"
            storage = IntelligenceStorage(database)
            storage.init_schema()
            Database(connection=storage.connection).init_db()
            completed_at = CUTOFF - timedelta(days=1)
            start_time = int((completed_at - timedelta(minutes=31)).timestamp())
            usable = completed_at + timedelta(minutes=1)
            basic_usable = completed_at + timedelta(seconds=1)
            content_hash = "a" * 64
            with storage.transaction():
                storage.connection.execute(
                    """INSERT INTO raw_source_artifacts
                       (artifact_id, content_hash, source, artifact_use, endpoint,
                        sanitized_request_identity, storage_path, uncompressed_bytes,
                        compressed_bytes, received_at, first_usable_at,
                        schema_fingerprint, event_id, match_id, created_at)
                       VALUES (?, ?, 'opendota', 'primary', 'match/9001', 'match/9001',
                               ?, 1, 1, ?, ?, 'schema',
                               'pgl-wallachia-s8-2026', 9001, ?)""",
                    (
                        content_hash,
                        content_hash,
                        str(Path(directory) / "raw.gz"),
                        usable.isoformat(),
                        usable.isoformat(),
                        usable.isoformat(),
                    ),
                )
                storage.connection.execute(
                    """INSERT INTO raw_source_observations
                       (observation_id, artifact_id, content_hash, source, artifact_use,
                        endpoint, sanitized_request_identity, received_at,
                        first_usable_at, schema_fingerprint, event_id, match_id, created_at)
                       VALUES ('obs-9001', ?, ?, 'opendota', 'primary', 'match/9001',
                               'match/9001', ?, ?, 'schema',
                               'pgl-wallachia-s8-2026', 9001, ?)""",
                    (
                        content_hash,
                        content_hash,
                        usable.isoformat(),
                        usable.isoformat(),
                        usable.isoformat(),
                    ),
                )
                storage.connection.execute(
                    """INSERT INTO match_ingest_status
                       (match_id, event_id, start_time, stage_scope, stage_in_scope,
                        has_valid_result, ingest_state, basic_result_state,
                         detailed_parse_state, first_usable_at, state_readiness,
                         latest_raw_artifact_id, latest_raw_content_hash,
                         discovered_at, updated_at, missing_fields_json)
                       VALUES (9001, 'pgl-wallachia-s8-2026', ?, 'main_event', 1,
                               1, 'detailed', 'ready', 'ready', ?, 'ready', ?, ?,
                               ?, ?, '[]')""",
                    (
                        start_time,
                        basic_usable.isoformat(),
                        content_hash,
                        content_hash,
                        usable.isoformat(),
                        usable.isoformat(),
                    ),
                )
                storage.connection.execute(
                    """INSERT INTO matches
                       (match_id, radiant_team_id, dire_team_id, radiant_win,
                        duration, start_time, leagueid, patch)
                       VALUES (9001, 101, 202, 1, 1860, ?, 19543, 60)""",
                    (start_time,),
                )
                storage.connection.execute(
                    """INSERT INTO matches
                       (match_id, radiant_team_id, dire_team_id, radiant_win,
                        duration, start_time, leagueid, patch)
                       VALUES (9999, 303, 404, 1, 1860, ?, 12345, 60)""",
                    (start_time,),
                )
                storage.connection.executemany(
                    "INSERT INTO gold_advantage(match_id, time_min, value) VALUES (9001, ?, ?)",
                    [(minute, 5_000 if 10 <= minute <= 14 else 0) for minute in range(31)],
                )
                storage.connection.execute(
                    """INSERT INTO objectives(match_id, time, type, player_slot)
                       VALUES (9001, 900, 'CHAT_MESSAGE_AEGIS', 0)"""
                )
                storage.connection.execute(
                    """INSERT INTO objectives(match_id, time, type, key)
                       VALUES (9001, 1000, 'building_kill',
                               'npc_dota_badguys_tower2_mid')"""
                )
            storage.close()

            audit_connection = sqlite3.connect(database)
            audit_connection.row_factory = sqlite3.Row
            try:
                loaded = _load_strict_maps(audit_connection)
                self.assertEqual(loaded[0].first_usable_at, usable)
                self.assertNotEqual(loaded[0].first_usable_at, basic_usable)
            finally:
                audit_connection.close()

            first = build_strict_profiles(database, CUTOFF)
            connection = sqlite3.connect(database)
            try:
                before = connection.execute(
                    "SELECT created_at FROM team_style_profiles ORDER BY team_id"
                ).fetchall()
            finally:
                connection.close()
            second = build_strict_profiles(database, CUTOFF)

            self.assertEqual(first, second)
            self.assertEqual(first.formal_maps, 1)
            self.assertEqual(first.state_rows, 2)
            self.assertEqual(first.profile_rows, 2)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT created_at FROM team_style_profiles ORDER BY team_id"
                    ).fetchall(),
                    before,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM team_map_states").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM team_style_profiles").fetchone()[0],
                    2,
                )
                conversion = json.loads(
                    connection.execute(
                        """SELECT objective_conversion_json FROM team_map_states
                           WHERE match_id=9001 AND side='radiant'"""
                    ).fetchone()[0]
                )
                self.assertTrue(conversion["roshan_opportunity"])
                self.assertTrue(conversion["tower_after_roshan"])
                weighting = json.loads(
                    connection.execute(
                        """SELECT weighting_json FROM team_style_profiles
                           WHERE team_id=101"""
                    ).fetchone()[0]
                )
                self.assertEqual(
                    weighting["maps"][0]["opponent_strength_scope"],
                    "neutral:no_causal_opponent_maps",
                )
                posterior = json.loads(
                    connection.execute(
                        """SELECT posterior_rates_json FROM team_style_profiles
                           WHERE team_id=101"""
                    ).fetchone()[0]
                )
                self.assertTrue(
                    all(row["prior_scope"].startswith("neutral:") for row in posterior)
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
