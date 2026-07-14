from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live_betting.event_detector import detect_events
from live_betting.alignment import align_snapshots
from live_betting.comeback import STRATEGY_VERSION
from live_betting.engine import build_orders, price_groups
from live_betting.episodes import EpisodeRow, chronological_split
from live_betting.evaluation import brier_score, log_loss, shadow_summary
from live_betting.markets import (
    normalize_market,
    normalized_state_hash,
    snapshots_from_payload,
)
from live_betting.models import (
    LiveFrame,
    Market,
    ModelQuote,
    OddsSnapshot,
)
from live_betting.pricing import devig
from live_betting.postmatch_monitor import _winner
from live_betting.profiles.draft_curve import DraftCurve, DraftPoint
from live_betting.profiles.player_form import PlayerForm
from live_betting.profiles.rosters import roster_history_weight
from live_betting.profiles.team_style import TeamStyleProfile, build_team_style
from live_betting.settlement import MapResult, settle
from live_betting.storage import LiveBettingStore
from live_betting.strategy import attempt_fill, make_order
from live_betting.shadow_strategy import ComebackShadowStrategy
from live_betting.vision import VisionObservation, parse_observation


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class MarketTests(unittest.TestCase):
    def test_normalizes_three_rollout_phases(self) -> None:
        winner = normalize_market(
            {"match_stage": "r1", "group_short_name": "Winner", "tag": "win"},
            "team_one",
        )
        total = normalize_market(
            {"match_stage": "r1", "group_short_name": "Total Kills", "tag": "ou",
             "value": "Over 45.5"}
        )
        handicap = normalize_market(
            {"match_stage": "r1", "group_short_name": "Kill Handicap", "tag": "hdp",
             "value": "-3.5"},
            "team_two",
        )
        race = normalize_market(
            {"match_stage": "r1", "group_short_name": "First Team to Get 10 Kills",
             "tag": "win"},
            "team_one",
        )
        duration = normalize_market(
            {"match_stage": "r1", "group_short_name": "Duration (Minutes)", "tag": "ou",
             "value": "Under 36.5"}
        )
        self.assertEqual(
            [winner.market_type, total.market_type, handicap.market_type,
             race.market_type, duration.market_type],
            ["winner", "total_kills", "kill_handicap", "race_to_kills", "duration"],
        )
        self.assertTrue(all(item.supported for item in (winner, total, handicap, race, duration)))

    def test_real_raybet_symbol_totals_and_live_status(self) -> None:
        market = normalize_market(
            {"match_stage": "r1", "group_short_name": "$T1 Total Kills", "tag": "ou",
             "value": "<24.5", "name": "小于<24.5"}
        )
        self.assertTrue(market.supported)
        self.assertEqual((market.side, market.line, market.outcome_key),
                         ("under", 24.5, "team_one:under:24.5"))

    def test_implausible_source_line_is_not_repaired_silently(self) -> None:
        market = normalize_market(
            {"match_stage": "r1", "group_short_name": "$T1 Total Kills", "tag": "ou",
             "value": ">235"}
        )
        self.assertFalse(market.supported)

    def test_series_winner_is_collection_only(self) -> None:
        market = normalize_market(
            {"match_stage": "final", "group_short_name": "Winner", "tag": "win"},
            "team_one",
        )
        self.assertFalse(market.supported)

    def test_unknown_market_is_preserved_as_unclassified(self) -> None:
        market = normalize_market(
            {"match_stage": "r2", "group_short_name": "Radiant Melee Heroes", "tag": "ou",
             "value": "Over 2.5"}
        )
        self.assertEqual(market.market_type, "unclassified")
        self.assertFalse(market.supported)

    def test_payload_maps_team_sides(self) -> None:
        payload = {"result": {"id": 10, "team": [
            {"team_id": 1, "pos": 1}, {"team_id": 2, "pos": 2}], "odds": [
            {"id": 5, "team_id": 2, "match_stage": "r1",
             "group_short_name": "Winner", "tag": "win", "odds": "2.10", "status": 5}]}}
        snapshot = snapshots_from_payload(payload)[0]
        self.assertEqual(snapshot.market.side, "team_two")


class EventTests(unittest.TestCase):
    def frame(self, sequence: str, game_time: int, one: int, two: int) -> LiveFrame:
        return LiveFrame("test", "m", "g", sequence, NOW, NOW, game_time, one, two)

    def test_derives_kills_and_race_once(self) -> None:
        events = detect_events(self.frame("1", 100, 4, 2), self.frame("2", 110, 5, 2))
        self.assertEqual([event.event_type for event in events], ["kill", "first_to_5_kills"])

    def test_rejects_score_regression(self) -> None:
        with self.assertRaises(ValueError):
            detect_events(self.frame("1", 100, 4, 2), self.frame("2", 110, 3, 2))


class StrategyTests(unittest.TestCase):
    def snapshot(self, received_at: datetime, price: float = 2.0, status: int = 1) -> OddsSnapshot:
        market = Market("winner", "map_1", "team_one", None, "team_one", True)
        return OddsSnapshot("m", "o", "g", received_at, price, status, market)

    def test_signal_fills_only_on_later_snapshot(self) -> None:
        snapshot = self.snapshot(NOW)
        quote = ModelQuote("m", "game", snapshot.market, 0.60, 0.50, 0.10, NOW, "v1", "frame1")
        order = make_order(
            quote, snapshot, min_edge=0.05,
            signal_transport_key="signal", signal_transport_at=NOW,
        )
        self.assertIsNotNone(order)
        self.assertEqual(attempt_fill(order, snapshot).status, "pending")
        filled = attempt_fill(order, self.snapshot(NOW + timedelta(seconds=3), 1.98))
        self.assertEqual(filled.status, "filled")
        self.assertEqual(filled.fill_price, 1.98)

    def test_slippage_rejects(self) -> None:
        snapshot = self.snapshot(NOW)
        quote = ModelQuote("m", "game", snapshot.market, 0.60, 0.50, 0.10, NOW, "v1", "frame1")
        order = make_order(
            quote, snapshot, min_edge=0.05,
            signal_transport_key="signal", signal_transport_at=NOW,
        )
        rejected = attempt_fill(order, self.snapshot(NOW + timedelta(seconds=3), 1.80))
        self.assertEqual((rejected.status, rejected.rejection_reason), ("rejected", "slippage"))

    def test_devig_sums_to_one(self) -> None:
        probabilities = devig([1.90, 1.95])
        self.assertAlmostEqual(sum(probabilities), 1.0)


class SettlementTests(unittest.TestCase):
    result = MapResult("team_one", 30, 20, 38.2, {5: "team_two", 10: "team_one", 15: "team_one"})

    def test_supported_markets_settle(self) -> None:
        self.assertEqual(settle(Market("winner", "map_1", "team_one", None, "x", True),
                                self.result, 1.8), ("win", 1.8))
        self.assertEqual(settle(Market("total_kills", "map_1", "over", 49.5, "x", True),
                                self.result, 1.9), ("win", 1.9))
        self.assertEqual(settle(Market("race_to_kills", "map_1", "team_two", 5, "x", True),
                                self.result, 2.1), ("win", 2.1))

    def test_quarter_handicap_half_win(self) -> None:
        result, returned = settle(
            Market("kill_handicap", "map_1", "team_one", -9.75, "x", True), self.result, 2.0
        )
        self.assertEqual(result, "half_win")
        self.assertEqual(returned, 1.5)

    def test_postmatch_team_side_mapping(self) -> None:
        detail = {
            "radiant_team_id": 10, "radiant_win": True,
            "radiant_score": 30, "dire_score": 20,
        }
        self.assertEqual(_winner(detail, 10, "team_two"), ("team_two", 30, 20))
        self.assertEqual(_winner(detail, 99, "team_one"), ("team_two", 20, 30))


class StorageTests(unittest.TestCase):
    def test_unchanged_odds_are_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                snapshot = OddsSnapshot(
                    "m", "o", "g", NOW, 2.0, 5,
                    Market("winner", "map_1", "team_one", None, "team_one", True),
                    last_update="same",
                )
                self.assertTrue(store.insert_odds(snapshot))
                self.assertFalse(store.insert_odds(snapshot))

    def test_duplicate_order_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                snapshot = OddsSnapshot(
                    "m", "o", "g", NOW, 2.0, 1,
                    Market("winner", "map_1", "team_one", None, "team_one", True),
                )
                quote = ModelQuote("m", "game", snapshot.market, 0.6, 0.5, 0.1,
                                   NOW, "v1", "frame")
                order = make_order(
                    quote, snapshot, min_edge=0.05,
                    signal_transport_key="signal", signal_transport_at=NOW,
                )
                store.store_odds_observation(
                    source="direct",
                    observation_key="signal",
                    source_event_id=None,
                    raybet_match_id="m",
                    observed_at=NOW,
                    normalized_state_hash=normalized_state_hash([snapshot]),
                    snapshots=[snapshot],
                )
                self.assertTrue(store.insert_order(order))
                self.assertFalse(store.insert_order(order))

    def test_only_one_shadow_order_can_reserve_a_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                first_snapshot = OddsSnapshot(
                    "m", "o", "g", NOW, 3.0, 1,
                    Market("winner", "map_1", "team_two", None, "team_two", True),
                )
                second_at = NOW + timedelta(seconds=3)
                second_snapshot = OddsSnapshot(
                    "m", "o", "g", second_at, 3.0, 1, first_snapshot.market,
                )
                quote = ModelQuote("m", "map_1", first_snapshot.market, 0.5, 0.3, 0.2,
                                   NOW, "v1", "frame")
                first = make_order(
                    quote, first_snapshot, min_edge=0.08,
                    signal_transport_key="signal-1", signal_transport_at=NOW,
                )
                second = make_order(
                    ModelQuote("m", "map_1", second_snapshot.market, 0.51, 0.3, 0.21,
                               second_at, "v1", "frame2"),
                    second_snapshot, min_edge=0.08,
                    signal_transport_key="signal-2",
                    signal_transport_at=second_at,
                )
                for key, at, row in (
                    ("signal-1", NOW, first_snapshot),
                    ("signal-2", second_at, second_snapshot),
                ):
                    store.store_odds_observation(
                        source="direct",
                        observation_key=key,
                        source_event_id=None,
                        raybet_match_id="m",
                        observed_at=at,
                        normalized_state_hash=normalized_state_hash([row]),
                        snapshots=[row],
                    )
                self.assertTrue(store.insert_map_order(first, 1))
                self.assertFalse(store.insert_map_order(second, 1))
                count = store.connection.execute(
                    "SELECT COUNT(*) FROM shadow_orders"
                ).fetchone()[0]
                self.assertEqual(count, 1)


class EngineTests(unittest.TestCase):
    def test_complete_group_is_devigged_and_can_signal(self) -> None:
        one = Market("winner", "map_1", "team_one", None, "team_one", True)
        two = Market("winner", "map_1", "team_two", None, "team_two", True)
        snapshots = [
            OddsSnapshot("m", "o1", "g", NOW, 1.90, 1, one),
            OddsSnapshot("m", "o2", "g", NOW, 1.95, 1, two),
        ]
        probabilities = price_groups(snapshots)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0)
        orders = build_orders(
            snapshots=snapshots,
            model_probabilities={"winner|map_1|team_one|": 0.60,
                                 "winner|map_1|team_two|": 0.40},
            provider_game_id="game", input_ref="frame", strategy_version="v1",
            quoted_at=NOW, signal_transport_key="signal",
            signal_transport_at=NOW, min_edge=0.05,
        )
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0][1].market.side, "team_one")

    def test_incomplete_group_cannot_signal(self) -> None:
        market = Market("winner", "map_1", "team_one", None, "team_one", True)
        snapshots = [OddsSnapshot("m", "o1", "g", NOW, 1.9, 1, market)]
        self.assertEqual(price_groups(snapshots), {})


class EvaluationTests(unittest.TestCase):
    def test_probability_metrics(self) -> None:
        rows = [(0.8, 1), (0.2, 0)]
        self.assertAlmostEqual(brier_score(rows), 0.04)
        self.assertLess(log_loss(rows), 0.25)

    def test_shadow_summary(self) -> None:
        summary = shadow_summary([
            {"status": "filled", "stake": 1, "return_units": 2},
            {"status": "rejected", "stake": 1},
        ])
        self.assertEqual(summary["fill_rate"], 0.5)
        self.assertEqual(summary["roi"], 1.0)

    def test_chronological_split_keeps_map_rows_together(self) -> None:
        rows = []
        for match_index in range(10):
            for second in (0, 10):
                decided = NOW + timedelta(minutes=match_index, seconds=second)
                rows.append(EpisodeRow(
                    str(match_index), 1, decided - timedelta(seconds=1), decided,
                    decided + timedelta(hours=1), {"edge": 0.1}, 1,
                ))
        splits = chronological_split(rows)
        owners = {}
        for split_index, split in enumerate(splits):
            for row in split:
                owners.setdefault(row.raybet_match_id, set()).add(split_index)
        self.assertTrue(all(len(value) == 1 for value in owners.values()))


class VisionContractTests(unittest.TestCase):
    def test_parses_confirmed_observation(self) -> None:
        observation = parse_observation({
            "schema_version": 1,
            "raybet_match_id": "123",
            "map_number": 2,
            "captured_at_utc": "2026-07-11T14:00:00Z",
            "game_clock_seconds": 1800,
            "is_paused": False,
            "radiant_hero_ids": [1, 2, 3, 4, 5],
            "dire_hero_ids": [6, 7, 8, 9, 10],
            "clock_confidence": 0.95,
            "draft_confidence": 0.96,
            "source_frame_ref": "frame.png",
            "screen_state": "game",
        })
        self.assertTrue(observation.is_confirmed)

    def test_rejects_future_schema(self) -> None:
        with self.assertRaises(ValueError):
            parse_observation({"schema_version": 2})


class AlignmentTests(unittest.TestCase):
    @staticmethod
    def observation(captured_at: datetime) -> VisionObservation:
        return VisionObservation(
            "m", 1, captured_at, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "frame", "game",
        )

    @staticmethod
    def snapshot(received_at: datetime) -> OddsSnapshot:
        return OddsSnapshot(
            "m", "o", "g", received_at, 2.5, 1,
            Market("winner", "map_1", "team_one", None, "team_one", True),
        )

    def test_never_uses_future_observation(self) -> None:
        rows = [(1, self.snapshot(NOW)),
                (2, self.snapshot(NOW + timedelta(seconds=5)))]
        aligned = align_snapshots(rows, [self.observation(NOW + timedelta(seconds=2))])
        self.assertFalse(aligned[0].usable)
        self.assertEqual(aligned[0].reason, "no_prior_confirmed_observation")
        self.assertTrue(aligned[1].usable)
        self.assertEqual(aligned[1].game_clock_seconds, 603)

    def test_rejects_cross_map_and_large_gap(self) -> None:
        observation = self.observation(NOW)
        cross_map = OddsSnapshot(
            "m", "o", "g", NOW + timedelta(seconds=1), 2.5, 1,
            Market("winner", "map_2", "team_one", None, "team_one", True),
        )
        aligned = align_snapshots(
            [(1, cross_map), (2, self.snapshot(NOW + timedelta(seconds=20)))],
            [observation],
        )
        self.assertEqual([row.reason for row in aligned], ["map_mismatch", "observation_gap"])


class ProfileTests(unittest.TestCase):
    def test_two_starter_changes_sharply_downweight_history(self) -> None:
        self.assertEqual(roster_history_weight((1, 2, 3, 4, 5), (1, 2, 3, 8, 9)), 0.25)

    def test_team_profile_is_as_of_and_shrunk(self) -> None:
        connection = __import__("sqlite3").connect(":memory:")
        connection.executescript("""
            CREATE TABLE matches(match_id INTEGER, duration INTEGER,
                radiant_team_id INTEGER, dire_team_id INTEGER,
                radiant_win INTEGER, start_time INTEGER);
            CREATE TABLE gold_advantage(match_id INTEGER, time_min INTEGER, value INTEGER);
            INSERT INTO matches VALUES (1, 2400, 10, 20, 1, 100);
            INSERT INTO matches VALUES (2, 2400, 10, 20, 0, 300);
            INSERT INTO gold_advantage VALUES (1, 20, -5000);
            INSERT INTO gold_advantage VALUES (2, 20, -5000);
        """)
        profile = build_team_style(connection, 10, 200)
        self.assertEqual(profile.matches, 1)
        self.assertGreater(profile.comeback_rate, 0.18)
        self.assertLess(profile.quality, 0.2)


class ComebackStrategyTests(unittest.TestCase):
    @staticmethod
    def style(team_id: int, comeback: float, throw: float, closeout: float) -> TeamStyleProfile:
        return TeamStyleProfile(team_id, 100, comeback, throw, closeout, 0.7, 42, 1.0)

    @staticmethod
    def observation(
        at: datetime = NOW, *, paused: bool | None = False
    ) -> VisionObservation:
        return VisionObservation(
            "m", 1, at, 1800, paused,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "frame", "game", "team_one",
        )

    @staticmethod
    def snapshots(at: datetime) -> list[OddsSnapshot]:
        return [
            OddsSnapshot("m", "fav", "winner", at, 1.40, 1,
                         Market("winner", "map_1", "team_one", None, "team_one", True)),
            OddsSnapshot("m", "dog", "winner", at, 3.00, 1,
                         Market("winner", "map_1", "team_two", None, "team_two", True)),
            OddsSnapshot("m", "kh-fav", "kill", at, 1.90, 1,
                         Market("kill_handicap", "map_1", "team_one", -5.5,
                                "team_one:-5.5", True)),
            OddsSnapshot("m", "kh-dog", "kill", at, 1.90, 1,
                         Market("kill_handicap", "map_1", "team_two", 5.5,
                                "team_two:5.5", True)),
            OddsSnapshot("m", "kills-over", "kills", at, 1.90, 1,
                         Market("total_kills", "map_1", "over", 50.5,
                                "over:50.5", True)),
            OddsSnapshot("m", "kills-under", "kills", at, 1.90, 1,
                         Market("total_kills", "map_1", "under", 50.5,
                                "under:50.5", True)),
            OddsSnapshot("m", "duration-over", "duration", at, 1.90, 1,
                         Market("duration", "map_1", "over", 36.5,
                                "over:36.5", True)),
            OddsSnapshot("m", "duration-under", "duration", at, 1.90, 1,
                         Market("duration", "map_1", "under", 36.5,
                                "under:36.5", True)),
        ]

    def strategy_kwargs(
        self,
        observation: VisionObservation | None = None,
        *,
        decided_at: datetime = NOW,
        signal_transport_key: str = "current",
    ) -> dict:
        return dict(
            observation=observation or self.observation(),
            underdog_style=self.style(2, 0.7, 0.2, 0.8),
            favorite_style=self.style(1, 0.2, 0.5, 0.5),
            underdog_form=PlayerForm((1, 2, 3, 4, 5), 0.5, {}, 100, 1.0),
            favorite_form=PlayerForm((6, 7, 8, 9, 10), -0.2, {}, 100, 1.0),
            draft_curve=DraftCurve(tuple(
                DraftPoint(
                    minute, 0.30, -0.2, -0.1, 1.0,
                    validated=True, support=100,
                    calibration_ref="test:passed", input_refs=("test:model",),
                    uncertainty=0.0,
                    feature_hash="1" * 64, model_hash="2" * 64,
                    calibration_hash="3" * 64,
                    global_calibration_passed=True,
                    global_gate_ref="test:global-passed",
                )
                for minute in (10, 20, 30, 40, 50)
            )),
            decided_at=decided_at,
            signal_transport_key=signal_transport_key,
        )

    def test_requires_two_stable_snapshots_and_one_attempt_per_map(self) -> None:
        strategy = ComebackShadowStrategy()
        kwargs = self.strategy_kwargs()
        previous = self.snapshots(NOW)
        current = self.snapshots(NOW + timedelta(seconds=3))
        first = strategy.evaluate(snapshots=previous,
                                  map_already_attempted=False, **kwargs)
        self.assertIsNone(first.order)
        self.assertEqual(
            first.decision.reason, "transport_identity_missing_or_reused"
        )
        second = strategy.evaluate(
            snapshots=current, previous_snapshots=previous,
            previous_observation=self.observation(),
            previous_transport_key="previous",
            map_already_attempted=False,
            **self.strategy_kwargs(decided_at=current[0].received_at),
        )
        self.assertTrue(second.decision.eligible)
        self.assertIsNotNone(second.order)
        self.assertEqual(second.order.status, "pending")
        self.assertEqual(second.decision.strategy_version, STRATEGY_VERSION)
        third = strategy.evaluate(
            snapshots=self.snapshots(NOW + timedelta(seconds=6)),
            previous_snapshots=current,
            previous_observation=self.observation(),
            previous_transport_key="previous",
            map_already_attempted=True,
            **self.strategy_kwargs(
                decided_at=NOW + timedelta(seconds=6),
                signal_transport_key="third",
            ),
        )
        self.assertIsNone(third.order)
        self.assertEqual(third.decision.reason, "map_already_attempted")

    def test_pause_or_unknown_pause_state_cannot_signal(self) -> None:
        previous = self.snapshots(NOW)
        current = self.snapshots(NOW + timedelta(seconds=3))
        for paused in (True, None):
            with self.subTest(paused=paused):
                result = ComebackShadowStrategy().evaluate(
                    snapshots=current,
                    previous_snapshots=previous,
                    previous_observation=self.observation(),
                    previous_transport_key="previous",
                    map_already_attempted=False,
                    **self.strategy_kwargs(
                        self.observation(paused=paused),
                        decided_at=current[0].received_at,
                    ),
                )
                self.assertIsNone(result.order)
                self.assertEqual(result.decision.reason, "stream_paused_or_unknown")

    def test_each_supporting_market_requires_a_complete_group(self) -> None:
        previous = self.snapshots(NOW)
        current = self.snapshots(NOW + timedelta(seconds=3))
        for market_type in ("kill_handicap", "total_kills", "duration"):
            with self.subTest(market_type=market_type):
                incomplete = []
                removed = False
                for row in current:
                    if row.market.market_type == market_type and not removed:
                        removed = True
                        continue
                    incomplete.append(row)
                result = ComebackShadowStrategy().evaluate(
                    snapshots=incomplete,
                    previous_snapshots=previous,
                    previous_observation=self.observation(),
                    previous_transport_key="previous",
                    map_already_attempted=False,
                    **self.strategy_kwargs(decided_at=current[0].received_at),
                )
                self.assertIsNone(result.order)
                self.assertEqual(result.decision.reason, "market_surface_incomplete")

    def test_incomplete_winner_group_cannot_be_evaluated(self) -> None:
        current = [
            row for row in self.snapshots(NOW + timedelta(seconds=3))
            if row.odds_id != "fav"
        ]
        with self.assertRaisesRegex(ValueError, "complete two-way map winner"):
            ComebackShadowStrategy().evaluate(
                snapshots=current,
                previous_snapshots=self.snapshots(NOW),
                previous_observation=self.observation(),
                previous_transport_key="previous",
                map_already_attempted=False,
                **self.strategy_kwargs(decided_at=current[0].received_at),
            )

    def test_closed_supporting_market_cannot_signal(self) -> None:
        previous = self.snapshots(NOW)
        current = self.snapshots(NOW + timedelta(seconds=3))
        closed = [
            OddsSnapshot(
                row.raybet_match_id, row.odds_id, row.odds_group_id,
                row.received_at, row.price,
                0 if row.odds_id == "duration-over" else row.status,
                row.market, row.last_update, row.raw,
            )
            for row in current
        ]
        result = ComebackShadowStrategy().evaluate(
            snapshots=closed,
            previous_snapshots=previous,
            previous_observation=self.observation(),
            previous_transport_key="previous",
            map_already_attempted=False,
            **self.strategy_kwargs(decided_at=current[0].received_at),
        )
        self.assertIsNone(result.order)
        self.assertEqual(result.decision.reason, "market_surface_incomplete")

    def test_same_transport_cannot_fake_stability(self) -> None:
        snapshots = self.snapshots(NOW)
        kwargs = self.strategy_kwargs(signal_transport_key="same")
        first = ComebackShadowStrategy().evaluate(
            snapshots=snapshots, map_already_attempted=False, **kwargs
        )
        repeated = ComebackShadowStrategy().evaluate(
            snapshots=snapshots,
            previous_snapshots=snapshots,
            previous_observation=self.observation(),
            previous_transport_key="same",
            map_already_attempted=False,
            **kwargs,
        )
        self.assertEqual(
            first.decision.reason, "transport_identity_missing_or_reused"
        )
        self.assertEqual(
            repeated.decision.reason, "transport_identity_missing_or_reused"
        )
        self.assertIsNone(repeated.order)

    def test_restart_replay_uses_explicit_previous_transport(self) -> None:
        previous = self.snapshots(NOW)
        current = self.snapshots(NOW + timedelta(seconds=3))
        result = ComebackShadowStrategy().evaluate(
            snapshots=current,
            previous_snapshots=previous,
            previous_observation=self.observation(),
            previous_transport_key="previous",
            map_already_attempted=False,
            **self.strategy_kwargs(decided_at=current[0].received_at),
        )
        self.assertTrue(result.decision.eligible)
        self.assertIsNotNone(result.order)

    def test_unchanged_prices_need_distinct_explicit_transport_times(self) -> None:
        snapshots = self.snapshots(NOW)
        result = ComebackShadowStrategy().evaluate(
            snapshots=snapshots,
            previous_snapshots=snapshots,
            previous_observation=self.observation(),
            snapshot_observed_at=NOW + timedelta(seconds=3),
            previous_snapshot_observed_at=NOW,
            previous_transport_key="previous",
            map_already_attempted=False,
            **self.strategy_kwargs(decided_at=NOW + timedelta(seconds=3)),
        )
        self.assertTrue(result.decision.eligible)
        self.assertIsNotNone(result.order)


if __name__ == "__main__":
    unittest.main()
