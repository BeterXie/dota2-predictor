from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from event_intelligence.incremental import SCORE_VERSION
from event_intelligence.team_profiles import PROFILE_VERSION
from live_betting.models import Market, OddsSnapshot
from live_betting.profiles import DraftCurve, PlayerForm, TeamStyleProfile
from live_betting.profiles.draft_curve import DraftPoint, build_draft_curve
from live_betting.shadow_strategy import ComebackShadowStrategy
from live_betting.shadow_monitor import _profile_refs, _profiles
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation


NOW = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)
PROSPECTIVE_IDENTITY = {
    "model_version": "draft-logistic-l2-v1",
    "model_kind": "pure_draft",
    "availability_mode": "prospective",
    "input_snapshot_hash": "5" * 64,
}


def observation() -> VisionObservation:
    return VisionObservation(
        "match-1",
        1,
        NOW,
        30 * 60,
        False,
        (1, 2, 3, 4, 5),
        (6, 7, 8, 9, 10),
        0.95,
        0.95,
        "frame",
        "game",
        "team_one",
    )


def snapshots(
    at: datetime,
    *,
    favorite_price: float = 1.4,
    underdog_price: float = 3.0,
) -> list[OddsSnapshot]:
    definitions = (
        ("favorite", "winner", "winner", "team_one", None, favorite_price),
        ("underdog", "winner", "winner", "team_two", None, underdog_price),
        ("kh-one", "kills", "kill_handicap", "team_one", -5.5, 1.9),
        ("kh-two", "kills", "kill_handicap", "team_two", 5.5, 1.9),
        ("total-over", "total", "total_kills", "over", 50.5, 1.9),
        ("total-under", "total", "total_kills", "under", 50.5, 1.9),
        ("duration-over", "duration", "duration", "over", 36.5, 1.9),
        ("duration-under", "duration", "duration", "under", 36.5, 1.9),
    )
    return [
        OddsSnapshot(
            "match-1",
            odds_id,
            group,
            at,
            price,
            1,
            Market(market_type, "map_1", side, line, f"{side}:{line}", True),
        )
        for odds_id, group, market_type, side, line, price in definitions
    ]


def style(
    team_id: int,
    *,
    comeback: float = 0.18,
    throw: float = 0.16,
    closeout: float = 0.84,
    late: float = 0.35,
    quality: float = 1.0,
) -> TeamStyleProfile:
    return TeamStyleProfile(
        team_id, 100, comeback, throw, closeout, late, 36.0, quality
    )


def form(score: float = 0.0, quality: float = 1.0) -> PlayerForm:
    return PlayerForm((1, 2, 3, 4, 5), score, {}, 100, quality)


def insert_prospective_curve(
    store: LiveBettingStore,
    *,
    curve_digit: str,
    first_usable_at: datetime,
    validation_status: str = "passed",
    global_calibration_passed: bool = True,
    landmark_created_at: datetime | None = None,
) -> str:
    radiant = (1, 2, 3, 4, 5)
    dire = (6, 7, 8, 9, 10)
    encoded = json.dumps(
        {"dire": list(dire), "radiant": list(radiant)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    lineup_hash = hashlib.sha256(encoded).hexdigest()
    curve_key = curve_digit * 64
    landmark_key = hashlib.sha256(f"{curve_key}:30".encode()).hexdigest()
    store.connection.execute(
        """INSERT INTO prospective_draft_curves
           (curve_key, raybet_match_id, map_number, strict_mapping_id,
            lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
            prediction_cutoff, first_usable_at, availability_mode, created_at)
           VALUES (?, 'match-1', 1, 7, ?, ?, ?, ?, ?, 'prospective', ?)""",
        (
            curve_key,
            lineup_hash,
            json.dumps(list(radiant)),
            json.dumps(list(dire)),
            (first_usable_at - timedelta(seconds=1)).isoformat(),
            first_usable_at.isoformat(),
            first_usable_at.isoformat(),
        ),
    )
    store.connection.execute(
        """INSERT INTO prospective_draft_landmarks
           (landmark_key, curve_key, horizon_minutes, radiant_probability,
            scaling_edge, synergy_edge, quality, validation_status, support,
            calibration_ref, input_refs_json, uncertainty, validation_reason,
            feature_hash, model_hash, calibration_hash,
            global_calibration_passed, global_gate_ref, model_version,
            model_kind, availability_mode, input_snapshot_hash, created_at)
           VALUES (?, ?, 30, 0.7, 0.1, 0.05, 0.9, ?, 150,
                   'calibration:passed', ?, 0.02, NULL, ?, ?, ?, ?, ?,
                   'draft-logistic-l2-v1', 'pure_draft', 'prospective', ?, ?)""",
        (
            landmark_key,
            curve_key,
            validation_status,
            json.dumps(["model:immutable", "features:immutable"]),
            "1" * 64,
            "2" * 64,
            "3" * 64,
            int(global_calibration_passed),
            "global-gate:passed" if global_calibration_passed else "",
            "5" * 64,
            (landmark_created_at or first_usable_at).isoformat(),
        ),
    )
    store.connection.commit()
    return curve_key


class DraftCurveSelectionTests(unittest.TestCase):
    def test_uses_only_latest_validated_past_landmark(self) -> None:
        curve = DraftCurve(
            (
                DraftPoint(
                    10, 0.4, 0.0, 0.0, 1.0, validated=True, support=100,
                    calibration_ref="test:passed", input_refs=("test:model",),
                    uncertainty=0.0,
                    feature_hash="1" * 64, model_hash="2" * 64,
                    calibration_hash="3" * 64,
                    global_calibration_passed=True,
                    global_gate_ref="test:global-passed",
                    **PROSPECTIVE_IDENTITY,
                ),
                DraftPoint(
                    20,
                    0.8,
                    0.0,
                    0.0,
                    1.0,
                    validated=False,
                    support=500,
                    calibration_ref="failed",
                    input_refs=("model",),
                    uncertainty=0.05,
                ),
                DraftPoint(
                    30, 0.9, 0.0, 0.0, 1.0, validated=True, support=100,
                    calibration_ref="test:passed", input_refs=("test:model",),
                    uncertainty=0.0,
                    feature_hash="1" * 64, model_hash="2" * 64,
                    calibration_hash="3" * 64,
                    global_calibration_passed=True,
                    global_gate_ref="test:global-passed",
                    **PROSPECTIVE_IDENTITY,
                ),
            )
        )

        self.assertIsNone(curve.at(9 * 60 + 59))
        self.assertEqual(curve.at(15 * 60).minute, 10)
        self.assertIsNone(curve.at(20 * 60))
        self.assertEqual(
            curve.wait_reason(20 * 60), "required_draft_landmark_not_validated"
        )
        self.assertIsNone(curve.at(20 * 60 + 1))
        self.assertEqual(curve.at(30 * 60).minute, 30)

    def test_support_and_calibration_refs_are_live_gates(self) -> None:
        unsupported = DraftPoint(
            10,
            0.6,
            0.0,
            0.0,
            1.0,
            validated=True,
            support=99,
            calibration_ref="calibration",
            input_refs=("input",),
            uncertainty=0.01,
            feature_hash="1" * 64, model_hash="2" * 64,
            calibration_hash="3" * 64,
            global_calibration_passed=True,
            global_gate_ref="test:global-passed",
        )
        missing_ref = DraftPoint(
            20,
            0.6,
            0.0,
            0.0,
            1.0,
            validated=True,
            support=100,
            calibration_ref="",
            input_refs=("input",),
            uncertainty=0.01,
            feature_hash="1" * 64, model_hash="2" * 64,
            calibration_hash="3" * 64,
            global_calibration_passed=True,
            global_gate_ref="test:global-passed",
        )
        curve = DraftCurve((unsupported, missing_ref))

        self.assertIsNone(curve.at(20 * 60))
        self.assertEqual(
            curve.wait_reason(20 * 60), "required_draft_landmark_not_validated"
        )

    def test_global_calibration_gate_is_explicit_and_fail_closed(self) -> None:
        point = DraftPoint(
            10, 0.6, 0.0, 0.0, 1.0,
            validated=True, support=500,
            calibration_ref="slice:passed", input_refs=("input",),
            uncertainty=0.01,
            feature_hash="1" * 64, model_hash="2" * 64,
            calibration_hash="3" * 64,
            global_calibration_passed=False,
            global_gate_ref="",
        )
        self.assertFalse(point.passes_live_gate)
        self.assertIsNone(DraftCurve((point,)).at(10 * 60))

    def test_loads_exact_causally_available_prospective_artifact(self) -> None:
        with LiveBettingStore(":memory:") as store:
            store.init_schema()
            curve_key = insert_prospective_curve(
                store,
                curve_digit="a",
                first_usable_at=NOW - timedelta(seconds=5),
            )

            curve = build_draft_curve(
                store.connection,
                (1, 2, 3, 4, 5),
                (6, 7, 8, 9, 10),
                int(NOW.timestamp()),
                raybet_match_id="match-1",
                map_number=1,
                strict_mapping_id=7,
            )

            self.assertEqual(curve.source_ref, f"prospective-draft:{curve_key}")
            point = curve.at(30 * 60)
            self.assertIsNotNone(point)
            self.assertEqual(point.model_version, "draft-logistic-l2-v1")
            self.assertEqual(point.availability_mode, "prospective")
            self.assertEqual(point.input_snapshot_hash, "5" * 64)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                store.connection.execute(
                    "UPDATE prospective_draft_curves SET map_number=2"
                )

    def test_future_or_latest_failed_artifact_cannot_fall_back(self) -> None:
        with LiveBettingStore(":memory:") as store:
            store.init_schema()
            insert_prospective_curve(
                store,
                curve_digit="a",
                first_usable_at=NOW - timedelta(minutes=2),
            )
            insert_prospective_curve(
                store,
                curve_digit="b",
                first_usable_at=NOW - timedelta(minutes=1),
                validation_status="failed",
                global_calibration_passed=False,
            )
            insert_prospective_curve(
                store,
                curve_digit="c",
                first_usable_at=NOW + timedelta(minutes=1),
            )

            curve = build_draft_curve(
                store.connection,
                (1, 2, 3, 4, 5),
                (6, 7, 8, 9, 10),
                int(NOW.timestamp()),
                raybet_match_id="match-1",
                map_number=1,
                strict_mapping_id=7,
            )

            self.assertIsNone(curve.at(30 * 60))
            self.assertEqual(
                curve.wait_reason(30 * 60),
                "prospective_draft_calibration_gate_not_passed",
            )

    def test_missing_target_identity_remains_fail_closed(self) -> None:
        with LiveBettingStore(":memory:") as store:
            store.init_schema()
            curve = build_draft_curve(
                store.connection,
                (1, 2, 3, 4, 5),
                (6, 7, 8, 9, 10),
                int(NOW.timestamp()),
            )
            self.assertEqual(
                curve.unavailable_reason, "prospective_draft_target_missing"
            )

    def test_landmark_added_after_curve_availability_is_rejected(self) -> None:
        with LiveBettingStore(":memory:") as store:
            store.init_schema()
            insert_prospective_curve(
                store,
                curve_digit="a",
                first_usable_at=NOW - timedelta(minutes=2),
                landmark_created_at=NOW - timedelta(minutes=1),
            )
            curve = build_draft_curve(
                store.connection,
                (1, 2, 3, 4, 5),
                (6, 7, 8, 9, 10),
                int(NOW.timestamp()),
                raybet_match_id="match-1",
                map_number=1,
                strict_mapping_id=7,
            )
            self.assertEqual(
                curve.unavailable_reason, "prospective_draft_artifact_invalid"
            )


class StrictComebackStrategyTests(unittest.TestCase):
    def evaluate(
        self,
        *,
        previous: list[OddsSnapshot],
        current: list[OddsSnapshot],
        underdog_style: TeamStyleProfile,
        favorite_style: TeamStyleProfile,
        underdog_form: PlayerForm,
        favorite_form: PlayerForm,
        draft_probability: float,
        min_edge: float = 0.001,
    ):
        return ComebackShadowStrategy(min_edge=min_edge).evaluate(
            snapshots=current,
            previous_snapshots=previous,
            observation=observation(),
            previous_observation=observation(),
            underdog_style=underdog_style,
            favorite_style=favorite_style,
            underdog_form=underdog_form,
            favorite_form=favorite_form,
            draft_curve=DraftCurve(
                (
                    DraftPoint(
                        30,
                        draft_probability,
                        0.0,
                        0.0,
                        1.0,
                        validated=True,
                        support=250,
                        calibration_ref="calibration:passed",
                        input_refs=("model:immutable", "features:immutable"),
                        uncertainty=0.0,
                        feature_hash="1" * 64, model_hash="2" * 64,
                        calibration_hash="3" * 64,
                        global_calibration_passed=True,
                        global_gate_ref="test:global-passed",
                        **PROSPECTIVE_IDENTITY,
                    ),
                )
            ),
            decided_at=current[0].received_at,
            map_already_attempted=False,
            snapshot_observed_at=current[0].received_at,
            previous_snapshot_observed_at=previous[0].received_at,
            signal_transport_key="current",
            previous_transport_key="previous",
        )

    def test_stability_uses_devigged_probability_not_relative_odds(self) -> None:
        previous = snapshots(NOW)
        current = snapshots(
            NOW + timedelta(seconds=3),
            favorite_price=1.4 * 1.05,
            underdog_price=3.0 * 1.05,
        )
        result = self.evaluate(
            previous=previous,
            current=current,
            underdog_style=style(2, comeback=0.7),
            favorite_style=style(1, throw=0.5, closeout=0.5),
            underdog_form=form(0.5),
            favorite_form=form(-0.2),
            draft_probability=0.3,
            min_edge=0.08,
        )

        self.assertTrue(result.decision.eligible)
        self.assertIsNotNone(result.order)
        self.assertAlmostEqual(
            result.decision.inputs["stability"][
                "actual_absolute_devigged_probability_move"
            ],
            0.0,
        )

    def test_market_movement_alone_cannot_create_signal(self) -> None:
        result = self.evaluate(
            previous=snapshots(NOW),
            current=snapshots(
                NOW + timedelta(seconds=3),
                favorite_price=1.4,
                underdog_price=2.75,
            ),
            underdog_style=style(2),
            favorite_style=style(1),
            underdog_form=form(),
            favorite_form=form(),
            draft_probability=0.5,
        )

        self.assertEqual(
            result.decision.reason, "no_independent_positive_contribution"
        )
        self.assertIsNone(result.order)
        self.assertGreater(result.decision.contributions["market_movement"], 0.0)

    def test_large_devigged_move_fails_even_when_dog_odds_move_under_two_percent(self) -> None:
        result = self.evaluate(
            previous=snapshots(NOW),
            current=snapshots(
                NOW + timedelta(seconds=3),
                favorite_price=1.8,
                underdog_price=3.03,
            ),
            underdog_style=style(2, comeback=0.7),
            favorite_style=style(1, throw=0.5, closeout=0.5),
            underdog_form=form(0.5),
            favorite_form=form(-0.2),
            draft_probability=0.3,
        )

        self.assertEqual(
            result.decision.reason, "market_not_stable_two_snapshots"
        )
        self.assertGreater(
            result.decision.inputs["stability"][
                "actual_absolute_devigged_probability_move"
            ],
            0.02,
        )
        self.assertIsNone(result.order)

    def test_conservative_shrink_can_veto_positive_point_edge(self) -> None:
        result = self.evaluate(
            previous=snapshots(NOW),
            current=snapshots(NOW + timedelta(seconds=3)),
            underdog_style=style(2, comeback=0.7, quality=0.2),
            favorite_style=style(
                1, throw=0.5, closeout=0.5, quality=0.2
            ),
            underdog_form=form(),
            favorite_form=form(),
            # team_two is the underdog, so radiant=0.6 means underdog draft=0.4.
            draft_probability=0.6,
            min_edge=0.005,
        )

        self.assertGreater(result.decision.edge, 0.005)
        self.assertEqual(
            result.decision.reason, "conservative_probability_not_above_market"
        )
        self.assertLessEqual(
            result.decision.conservative_probability,
            result.decision.market_probability,
        )
        self.assertIsNone(result.order)


class VersionedLiveProfileTests(unittest.TestCase):
    def test_profiles_use_only_completed_and_available_versioned_rows(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE team_style_profiles (
                profile_id INTEGER PRIMARY KEY,
                team_id INTEGER,
                profile_cutoff TEXT,
                profile_version TEXT,
                posterior_rates_json TEXT,
                duration_quantiles_json TEXT,
                weighting_json TEXT,
                effective_sample_size REAL,
                input_hash TEXT,
                created_at TEXT
            );
            CREATE TABLE formal_map_eligibility (match_id INTEGER PRIMARY KEY);
            CREATE TABLE matches (
                match_id INTEGER PRIMARY KEY,
                radiant_team_id INTEGER,
                dire_team_id INTEGER,
                start_time INTEGER,
                duration INTEGER
            );
            CREATE TABLE match_ingest_status (
                match_id INTEGER PRIMARY KEY,
                latest_raw_artifact_id TEXT,
                latest_raw_content_hash TEXT,
                player_readiness TEXT
            );
            CREATE TABLE raw_source_artifacts (
                artifact_id TEXT,
                content_hash TEXT,
                first_usable_at TEXT
            );
            CREATE TABLE match_players (
                match_id INTEGER,
                team_id INTEGER,
                account_id INTEGER,
                player_slot INTEGER
            );
            CREATE TABLE player_map_scores (
                score_id INTEGER PRIMARY KEY,
                match_id INTEGER,
                player_slot INTEGER,
                account_id INTEGER,
                position INTEGER,
                execution_score REAL,
                coverage REAL,
                role_confidence REAL,
                benchmark_cutoff TEXT,
                input_hash TEXT,
                score_version TEXT,
                created_at TEXT
            );
            """
        )
        cutoff = NOW
        earlier = NOW - timedelta(hours=2)
        future = NOW + timedelta(hours=1)

        def rates(comeback: float) -> str:
            values = {
                "comeback_after_5000_deficit": comeback,
                "throw_after_5000_lead": 0.2,
                "closeout_after_5000_lead": 0.8,
                "reach_40_minutes": 0.4,
            }
            return json.dumps(
                [{"metric": name, "mean": value} for name, value in values.items()]
            )

        durations = json.dumps(
            [
                {"group": "win", "p50": 2400},
                {"group": "loss", "p50": 3000},
                {"group": "even", "p50": 2700},
            ]
        )
        connection.executemany(
            "INSERT INTO team_style_profiles VALUES (?, 10, ?, ?, ?, ?, ?, 25, ?, ?)",
            (
                (
                    1,
                    earlier.isoformat(),
                    PROFILE_VERSION,
                    rates(0.6),
                    durations,
                    json.dumps({"maps": [{"match_id": 1}]}),
                    "earlier-style",
                    earlier.isoformat(),
                ),
                (
                    2,
                    (NOW - timedelta(minutes=30)).isoformat(),
                    PROFILE_VERSION,
                    rates(0.99),
                    durations,
                    json.dumps({"maps": [{"match_id": 2}]}),
                    "future-created-style",
                    future.isoformat(),
                ),
            ),
        )
        completed_at = int((NOW - timedelta(days=1)).timestamp())
        connection.execute("INSERT INTO formal_map_eligibility VALUES (1)")
        connection.execute(
            "INSERT INTO matches VALUES (1, 10, 20, ?, 1800)",
            (completed_at - 1800,),
        )
        connection.execute(
            "INSERT INTO match_ingest_status VALUES (1, 'artifact', 'hash', 'ready')"
        )
        connection.execute(
            "INSERT INTO raw_source_artifacts VALUES ('artifact', 'hash', ?)",
            (earlier.isoformat(),),
        )
        for index in range(5):
            account_id = 100 + index
            connection.execute(
                "INSERT INTO match_players VALUES (1, 10, ?, ?)",
                (account_id, index),
            )
            connection.execute(
                """INSERT INTO player_map_scores VALUES
                   (?, 1, ?, ?, ?, 60, 1, 1, ?, ?, ?, ?)""",
                (
                    index + 1,
                    index,
                    account_id,
                    index + 1,
                    earlier.isoformat(),
                    f"score-{index}",
                    SCORE_VERSION,
                    earlier.isoformat(),
                ),
            )
        # A backdated target with an unavailable score version must not replace
        # the score that existed at the transport cutoff.
        connection.execute(
            """INSERT INTO player_map_scores VALUES
               (99, 1, 0, 100, 1, 100, 1, 1, ?, 'future-score', 'score-v2', ?)""",
            (earlier.isoformat(), future.isoformat()),
        )

        style_profile, player_form = _profiles(
            connection, 10, int(cutoff.timestamp())
        )
        refs = _profile_refs(style_profile, player_form)

        self.assertAlmostEqual(style_profile.comeback_rate, 0.6)
        self.assertAlmostEqual(style_profile.average_duration_minutes, 45.0)
        self.assertAlmostEqual(player_form.score, 0.2)
        self.assertEqual(player_form.matches, 5)
        self.assertEqual(len(refs["player_form"]["score_refs"]), 5)
        self.assertNotIn(
            "future-score",
            {row["input_hash"] for row in refs["player_form"]["score_refs"]},
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
