from __future__ import annotations

import tempfile
import unittest
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from event_intelligence.storage import IntelligenceStorage
from live_betting.markets import normalized_state_hash
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.profiles import DraftCurve, PlayerForm, TeamStyleProfile
from live_betting.profiles.draft_curve import DraftPoint
from live_betting.shadow_monitor import _observation, persist_alignments, run_once
from live_betting.storage import LiveBettingStore
from live_betting.strict_eligibility import (
    accept_strict_live_map_mapping,
    init_strict_live_eligibility_schema,
)
from live_betting.vision import VisionObservation


NOW = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)
MISSING_VISION = Path("does-not-exist.jsonl")
EVENT_ID = "ewc-dota2-2026"
SCHEDULED_AT = "2026-07-12T12:00:00+00:00"


def raybet_metadata() -> dict[str, object]:
    return {
        "id": "match-1",
        "game_id": 151,
        "tournament_name": "Esports World Cup 2026",
        "start_time": SCHEDULED_AT,
        "round": "bo3",
        "stage": "main_event",
        "team": [
            {"pos": 1, "team_id": 1, "team_name": "One"},
            {"pos": 2, "team_id": 2, "team_name": "Two"},
        ],
    }


def mapping_evidence() -> dict[str, object]:
    return {
        "kind": "manual_cross_source_review",
        "raybet_url": "https://example.invalid/raybet/match-1",
        "official_event_url": "https://example.invalid/ewc",
        "tournament": {
            "raybet_name": "Esports World Cup 2026",
            "event_name": "Esports World Cup 2026",
        },
        "schedule": {
            "raybet_scheduled_at": SCHEDULED_AT,
            "utc_offset_minutes": 0,
            "timezone_evidence": "fixture stores an explicit UTC offset",
            "scheduled_at_utc": SCHEDULED_AT,
        },
        "stage": {
            "scope": "main_event",
            "source_url": "https://example.invalid/ewc/stage",
        },
        "team_crosswalk": {
            "team_one": {
                "raybet_team_id": 1,
                "raybet_team_name": "One",
                "canonical_team_id": 10,
                "canonical_team_name": "Canonical One",
                "source_url": "https://example.invalid/teams/one",
            },
            "team_two": {
                "raybet_team_id": 2,
                "raybet_team_name": "Two",
                "canonical_team_id": 20,
                "canonical_team_name": "Canonical Two",
                "source_url": "https://example.invalid/teams/two",
            },
        },
    }


def snapshots(at: datetime, *, status: int = 1) -> list[OddsSnapshot]:
    return [
        OddsSnapshot(
            "match-1", "winner-one", "winner-group", at, 2.8, status,
            Market("winner", "map_1", "team_one", None, "team_one", True),
            last_update="one",
        ),
        OddsSnapshot(
            "match-1", "winner-two", "winner-group", at, 1.5, status,
            Market("winner", "map_1", "team_two", None, "team_two", True),
            last_update="two",
        ),
    ]


def complete_snapshots(at: datetime, *, status: int = 1) -> list[OddsSnapshot]:
    rows = snapshots(at, status=status)
    for odds_id, group, market_type, side, line in (
        ("kh-one", "kh-group", "kill_handicap", "team_one", -5.5),
        ("kh-two", "kh-group", "kill_handicap", "team_two", 5.5),
        ("total-over", "total-group", "total_kills", "over", 50.5),
        ("total-under", "total-group", "total_kills", "under", 50.5),
        ("duration-over", "duration-group", "duration", "over", 36.5),
        ("duration-under", "duration-group", "duration", "under", 36.5),
    ):
        rows.append(
            OddsSnapshot(
                "match-1", odds_id, group, at, 1.9, status,
                Market(market_type, "map_1", side, line, f"{side}:{line}", True),
            )
        )
    return rows


def observation(at: datetime, *, frame: str = "frame") -> VisionObservation:
    return VisionObservation(
        "match-1", 1, at, 600, False,
        (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
        0.95, 0.95, frame, "game", "team_one",
    )


class ShadowMonitorSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "test.db"
        with IntelligenceStorage(path) as intelligence:
            intelligence.init_schema()
            intelligence.connection.execute(
                "UPDATE event_registry SET approved_at=? WHERE event_id=?",
                ((NOW - timedelta(days=30)).isoformat(), EVENT_ID),
            )
            intelligence.connection.commit()
        self.store = LiveBettingStore(path)
        self.store.init_schema()
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS teams (team_id INTEGER PRIMARY KEY, name TEXT)"
        )
        self.store.connection.executemany(
            "INSERT OR IGNORE INTO teams VALUES (?, ?)",
            ((10, "Canonical One"), (20, "Canonical Two")),
        )
        init_strict_live_eligibility_schema(self.store.connection)
        self.store.connection.commit()
        self.strict_mapping_context_patch = patch.object(
            self.store,
            "_strict_mapping_context_block_reason",
            return_value=None,
        )
        self.strict_mapping_order_patch = patch.object(
            self.store,
            "_strict_mapping_block_reason_for_order",
            return_value=None,
        )
        self.strict_mapping_context_patch.start()
        self.strict_mapping_order_patch.start()
        self.addCleanup(self.strict_mapping_order_patch.stop)
        self.addCleanup(self.strict_mapping_context_patch.stop)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def record_transport(
        self, at: datetime, *, key: str, status: int = 1,
        rows: list[OddsSnapshot] | None = None,
    ) -> list[OddsSnapshot]:
        rows = rows or snapshots(at, status=status)
        self.store.store_odds_observation(
            source="direct",
            observation_key=key,
            source_event_id=None,
            raybet_match_id="match-1",
            observed_at=at,
            normalized_state_hash=normalized_state_hash(rows),
            snapshots=rows,
        )
        return rows

    def insert_pending(self, signaled_at: datetime) -> ShadowOrder:
        signal = self.record_transport(signaled_at, key="signal")[0]
        market = signal.market
        order = ShadowOrder(
            order_key="order-1",
            raybet_match_id="match-1",
            odds_id="winner-one",
            market=market,
            signaled_at=signaled_at,
            model_probability=0.6,
            market_probability=0.5,
            signal_price=2.8,
            signal_transport_key="signal",
            signal_transport_at=signaled_at,
            expires_at=signaled_at + timedelta(seconds=15),
            signal_odds_group_id=signal.odds_group_id,
            signal_outcome_key=signal.market.outcome_key,
            signal_identity_verified=True,
        )
        self.assertTrue(
            self.store.insert_map_order(order, 1, strict_mapping_id=1)
        )
        return order

    def test_pending_fill_is_processed_without_any_vision(self) -> None:
        self.insert_pending(NOW)
        self.record_transport(NOW + timedelta(seconds=2), key="candidate")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(days=2),
        )

        self.assertEqual(result["status"], "shadow_filled")
        row = self.store.connection.execute(
            "SELECT status, filled_at FROM shadow_orders WHERE order_key='order-1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("filled", (NOW + timedelta(seconds=2)).isoformat()))

    def test_pending_rejection_is_processed_without_fresh_vision(self) -> None:
        self.insert_pending(NOW)
        self.record_transport(NOW + timedelta(seconds=2), key="closed", status=5)

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(days=2),
        )

        self.assertEqual(result["status"], "shadow_rejected")
        row = self.store.connection.execute(
            "SELECT status, rejection_reason FROM shadow_orders WHERE order_key='order-1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("rejected", "market_closed"))

    def test_pending_terminal_updates_roll_back_together(self) -> None:
        self.insert_pending(NOW)
        self.record_transport(NOW + timedelta(seconds=2), key="candidate")

        with (
            patch.object(
                self.store, "update_map_attempt", side_effect=RuntimeError("injected")
            ),
            self.assertRaisesRegex(RuntimeError, "injected"),
        ):
            run_once(self.store, Mock(), MISSING_VISION, now=NOW + timedelta(seconds=3))

        order = self.store.connection.execute(
            "SELECT status, filled_at FROM shadow_orders WHERE order_key='order-1'"
        ).fetchone()
        attempt = self.store.connection.execute(
            "SELECT status FROM shadow_map_attempts WHERE order_key='order-1'"
        ).fetchone()
        self.assertEqual(tuple(order), ("pending", None))
        self.assertEqual(attempt["status"], "pending")

    def test_stale_latest_transport_cannot_reach_strategy(self) -> None:
        self.store.insert_vision_observation(observation(NOW))
        self.record_transport(NOW + timedelta(seconds=1), key="old")
        strategy = Mock()

        result = run_once(
            self.store, strategy, MISSING_VISION,
            now=NOW + timedelta(seconds=17),
        )

        self.assertEqual(result["status"], "waiting_for_fresh_odds")
        strategy.evaluate.assert_not_called()

    def test_persisted_invalidation_cannot_be_reconfirmed_from_payload_fields(self) -> None:
        self.store.insert_vision_observation(observation(NOW))
        self.store.connection.execute(
            "UPDATE vision_observations SET confirmed=0 WHERE source_frame_ref='frame'"
        )
        row = self.store.connection.execute(
            "SELECT * FROM vision_observations WHERE source_frame_ref='frame'"
        ).fetchone()
        self.assertFalse(_observation(row).is_confirmed)

    def test_cross_session_draft_conflict_freezes_the_map(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = replace(
            original,
            captured_at=NOW + timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="conflict",
        )
        after_conflict = replace(
            original,
            captured_at=NOW + timedelta(seconds=2),
            source_frame_ref="after-conflict",
        )

        self.assertTrue(self.store.insert_vision_observation(original))
        self.assertTrue(self.store.insert_vision_observation(conflicting))
        self.assertTrue(self.store.insert_vision_observation(after_conflict))

        rows = self.store.connection.execute(
            """SELECT source_frame_ref, confirmed FROM vision_observations
                 ORDER BY captured_at"""
        ).fetchall()
        self.assertEqual(
            [(str(row[0]), int(row[1])) for row in rows],
            [("original", 1), ("conflict", 0), ("after-conflict", 0)],
        )
        anchor = self.store.connection.execute(
            "SELECT status, conflict_at FROM vision_draft_anchors"
        ).fetchone()
        self.assertEqual(anchor["status"], "conflict")
        self.assertIsNotNone(anchor["conflict_at"])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM vision_draft_conflicts"
            ).fetchone()[0],
            2,
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.store.connection.execute(
                "UPDATE vision_draft_anchors SET draft_hash=?",
                ("0" * 64,),
            )

    def test_draft_conflict_hides_confirmed_frames_from_live_monitor(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = replace(
            original,
            captured_at=NOW + timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="conflict",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=2), key="after-conflict")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertEqual(result["status"], "waiting_for_confirmed_vision")

    def test_draft_conflict_rejects_pending_order_before_successor_fill(self) -> None:
        original = observation(NOW - timedelta(seconds=2), frame="original")
        conflicting = replace(
            original,
            captured_at=NOW - timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="conflict",
        )
        self.insert_pending(NOW)
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=2), key="successor")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertEqual(result["status"], "shadow_rejected")
        row = self.store.connection.execute(
            "SELECT status, rejection_reason FROM shadow_orders WHERE order_key='order-1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("rejected", "vision_draft_conflict"))
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM vision_derived_invalidations
                   WHERE dependent_type='shadow_order' AND dependent_key='order-1'"""
            ).fetchone()[0],
            1,
        )

    def test_future_draft_conflict_does_not_reject_prior_pending_order(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = replace(
            original,
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="future-conflict",
        )
        self.insert_pending(NOW)
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=2), key="successor")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertEqual(result["status"], "shadow_filled")
        row = self.store.connection.execute(
            "SELECT status, filled_at FROM shadow_orders WHERE order_key='order-1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("filled", (NOW + timedelta(seconds=2)).isoformat()))
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM vision_derived_invalidations
                   WHERE dependent_type='shadow_order' AND dependent_key='order-1'"""
            ).fetchone()[0],
            0,
        )

    def test_future_draft_conflict_keeps_cutoff_vision_usable(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = replace(
            original,
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="future-conflict",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.store.upsert_raybet_match(
            raybet_metadata(), NOW - timedelta(minutes=2)
        )
        self.record_transport(NOW + timedelta(seconds=2), key="before-conflict")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertNotEqual(result["status"], "waiting_for_confirmed_vision")
        self.assertEqual(result["status"], "no_signal")
        self.assertEqual(result["reason"], "strict_live_ineligible")

    def test_storage_rejects_decision_at_or_after_draft_conflict(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = replace(
            original,
            captured_at=NOW + timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="conflict",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        base = dict(
            raybet_match_id="match-1",
            map_number=1,
            underdog_side="team_one",
            market_probability=0.4,
            model_probability=0.5,
            edge=0.1,
            data_quality=0.8,
            eligible=True,
            reason="eligible",
            contributions={"draft": 0.1},
            input_ref="input-1",
            strategy_version="strategy-1",
        )
        self.assertTrue(
            self.store.insert_decision(
                SimpleNamespace(
                    **base,
                    decision_key="decision-before-conflict",
                    decided_at=NOW,
                )
            )
        )
        self.assertFalse(
            self.store.insert_decision(
                SimpleNamespace(
                    **{**base, "decision_key": "decision-after-conflict"},
                    decided_at=NOW + timedelta(seconds=1),
                )
            )
        )

    def test_out_of_order_conflict_uses_earliest_capture_cutoff(self) -> None:
        original = observation(NOW, frame="original")
        first_conflict = replace(
            original,
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="conflict-late",
        )
        earlier_conflict = replace(
            original,
            captured_at=NOW + timedelta(seconds=5),
            radiant_hero_ids=(1, 2, 3, 4, 7),
            dire_hero_ids=(5, 6, 8, 9, 10),
            source_frame_ref="conflict-earlier",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(first_conflict)
        self.store.insert_vision_observation(earlier_conflict)
        decision = SimpleNamespace(
            decision_key="decision-out-of-order",
            raybet_match_id="match-1",
            map_number=1,
            decided_at=NOW + timedelta(seconds=7),
            underdog_side="team_one",
            market_probability=0.4,
            model_probability=0.5,
            edge=0.1,
            data_quality=0.8,
            eligible=True,
            reason="eligible",
            contributions={"draft": 0.1},
            input_ref="input-1",
            strategy_version="strategy-1",
        )

        self.assertFalse(self.store.insert_decision(decision))

    def test_out_of_order_conflict_is_excluded_from_causal_alignment_reads(self) -> None:
        original = observation(NOW, frame="original")
        first_conflict = replace(
            original,
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="conflict-late",
        )
        earlier_conflict = replace(
            original,
            captured_at=NOW + timedelta(seconds=5),
            radiant_hero_ids=(1, 2, 3, 4, 7),
            dire_hero_ids=(5, 6, 8, 9, 10),
            source_frame_ref="conflict-earlier",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(first_conflict)
        self.store.insert_vision_observation(earlier_conflict)
        self.record_transport(NOW + timedelta(seconds=7), key="before-late-conflict")

        self.assertEqual(
            persist_alignments(
                self.store, "match-1", as_of=NOW + timedelta(seconds=7)
            ),
            0,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM odds_alignments"
            ).fetchone()[0],
            0,
        )

    def test_pending_successor_rechecks_conflict_before_filling(self) -> None:
        order = self.insert_pending(NOW)
        original = observation(NOW - timedelta(seconds=2), frame="original")
        conflicting = replace(
            original,
            captured_at=NOW - timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
            source_frame_ref="conflict",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=2), key="successor")

        resolved = self.store.process_pending_successor(
            order, watermark=NOW + timedelta(seconds=2)
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.status, "rejected")
        self.assertEqual(resolved.rejection_reason, "vision_draft_conflict")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM shadow_orders WHERE order_key='order-1'"
            ).fetchone()[0],
            "rejected",
        )

    def test_transport_uses_latest_prior_vision_not_a_future_frame(self) -> None:
        self.store.insert_vision_observation(observation(NOW, frame="old"))
        self.record_transport(NOW + timedelta(seconds=1), key="before-new-frame")
        self.store.insert_vision_observation(
            observation(NOW + timedelta(seconds=2), frame="current")
        )
        strategy, _, _ = self._prepare_strategy_run(add_observation=False)

        with (
            patch("live_betting.shadow_monitor._profiles", side_effect=strategy.fake_profiles),
            patch(
                "live_betting.shadow_monitor.build_draft_curve",
                side_effect=strategy.fake_draft,
            ),
            patch.object(self.store, "insert_decision", return_value=True),
        ):
            result = run_once(
                self.store, strategy, MISSING_VISION,
                now=NOW + timedelta(seconds=3),
            )

        self.assertEqual(result["status"], "no_signal")
        aligned = strategy.evaluate.call_args.kwargs["observation"]
        self.assertEqual(aligned.source_frame_ref, "old")
        self.assertEqual(aligned.game_clock_seconds, 601)

    def test_transport_without_a_recent_prior_vision_waits_for_alignment(self) -> None:
        self.record_transport(NOW + timedelta(seconds=1), key="no-prior-frame")
        self.store.insert_vision_observation(
            observation(NOW + timedelta(seconds=2), frame="future")
        )
        strategy = Mock()

        result = run_once(
            self.store, strategy, MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertEqual(result["status"], "waiting_for_usable_alignment")
        self.assertEqual(result["reason"], "no_prior_confirmed_observation")
        strategy.evaluate.assert_not_called()

    def test_transport_more_than_fifteen_seconds_after_vision_is_unusable(self) -> None:
        self.store.insert_vision_observation(observation(NOW))
        self.record_transport(NOW + timedelta(seconds=16), key="vision-gap")
        strategy = Mock()

        result = run_once(
            self.store, strategy, MISSING_VISION,
            now=NOW + timedelta(seconds=17),
        )

        self.assertEqual(result["status"], "waiting_for_usable_alignment")
        self.assertEqual(result["reason"], "observation_gap")
        strategy.evaluate.assert_not_called()

    def test_missing_strict_mapping_is_persisted_as_structured_no_signal(self) -> None:
        self.store.insert_vision_observation(observation(NOW))
        self.store.upsert_raybet_match(
            raybet_metadata(), NOW - timedelta(minutes=2)
        )
        self.record_transport(NOW + timedelta(seconds=1), key="unmapped")
        strategy = Mock()

        with (
            patch("live_betting.shadow_monitor._profiles") as profiles,
            patch("live_betting.shadow_monitor.build_draft_curve") as draft,
        ):
            result = run_once(
                self.store,
                strategy,
                MISSING_VISION,
                now=NOW + timedelta(seconds=2),
            )

        self.assertEqual(result["status"], "no_signal")
        self.assertEqual(result["reason"], "strict_live_ineligible")
        self.assertEqual(result["reason_code"], "accepted_mapping_missing")
        profiles.assert_not_called()
        draft.assert_not_called()
        strategy.evaluate.assert_not_called()
        row = self.store.connection.execute(
            "SELECT reason, contributions_json FROM strategy_decisions"
        ).fetchone()
        self.assertEqual(row["reason"], "strict_live_ineligible:accepted_mapping_missing")
        persisted = __import__("json").loads(row["contributions_json"])
        self.assertEqual(
            persisted["__inputs__"]["transport"]["current_key"], "unmapped"
        )

    def test_missing_validated_landmark_is_persisted_before_profiles(self) -> None:
        strategy, _, _ = self._prepare_strategy_run()
        at = NOW + timedelta(seconds=1)
        self.record_transport(
            at, key="no-landmark", rows=complete_snapshots(at)
        )

        with patch("live_betting.shadow_monitor._profiles") as profiles:
            result = run_once(
                self.store,
                strategy,
                MISSING_VISION,
                now=NOW + timedelta(seconds=2),
            )

        self.assertEqual(result["status"], "no_signal")
        self.assertEqual(result["reason"], "draft_landmark_unavailable")
        self.assertEqual(
            result["reason_code"], "validated_live_draft_prediction_missing"
        )
        profiles.assert_not_called()
        strategy.evaluate.assert_not_called()
        row = self.store.connection.execute(
            "SELECT reason FROM strategy_decisions"
        ).fetchone()
        self.assertIn("validated_live_draft_prediction_missing", row["reason"])
        research = self.store.connection.execute(
            """SELECT actionability, raw_model_probability, feature_hash,
                      model_hash, calibration_hash, gate_status,
                      gate_failures_json, manual_clock_trust,
                      manual_clock_validation
                 FROM research_live_predictions"""
        ).fetchone()
        self.assertIsNotNone(research)
        self.assertEqual(research["actionability"], "research_only")
        self.assertIsNone(research["raw_model_probability"])
        self.assertIsNone(research["feature_hash"])
        self.assertIsNone(research["model_hash"])
        self.assertIsNone(research["calibration_hash"])
        self.assertEqual(research["gate_status"], "unavailable")
        self.assertIn(
            "validated_live_draft_prediction_missing",
            __import__("json").loads(research["gate_failures_json"]),
        )
        self.assertEqual(research["manual_clock_trust"], "not_observed")
        self.assertEqual(research["manual_clock_validation"], "not_observed")
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM shadow_orders").fetchone()[0],
            0,
        )

    def _prepare_strategy_run(
        self, *, add_observation: bool = True,
    ) -> tuple[Mock, list[int], list[int]]:
        if add_observation:
            self.store.insert_vision_observation(observation(NOW))
        self.store.upsert_raybet_match(
            raybet_metadata(), NOW - timedelta(minutes=2)
        )
        with patch(
            "live_betting.strict_eligibility._utc_now",
            return_value=NOW - timedelta(seconds=30),
        ):
            accept_strict_live_map_mapping(
                self.store.connection,
                raybet_match_id="match-1",
                map_number=1,
                event_id=EVENT_ID,
                team_one_id=1,
                team_two_id=2,
                canonical_team_one_id=10,
                canonical_team_two_id=20,
                source="test_fixture",
                evidence=mapping_evidence(),
                accepted_by="tester",
                accepted_at=NOW - timedelta(minutes=1),
            )
        strategy = Mock()
        strategy.evaluate.return_value = SimpleNamespace(
            decision=SimpleNamespace(
                reason="test", edge=0.0, data_quality=1.0,
                decision_key="decision", inputs={},
            ),
            order=None,
        )
        profile_times: list[int] = []
        draft_times: list[int] = []

        def fake_profiles(_connection: object, _team_id: int, as_of: int):
            profile_times.append(as_of)
            return (
                TeamStyleProfile(0, 0, 0.18, 0.16, 0.84, 0.35, 36.0, 0.0),
                PlayerForm((), 0.0, {}, 0, 0.0),
            )

        def fake_draft(
            _connection: object, _radiant: tuple[int, ...],
            _dire: tuple[int, ...], as_of: int,
            **_target: object,
        ) -> DraftCurve:
            draft_times.append(as_of)
            return DraftCurve((DraftPoint(
                10, 0.5, 0.0, 0.0, 0.0, validated=True, support=100,
                calibration_ref="test:passed", input_refs=("test:model",),
                uncertainty=0.0,
                feature_hash="1" * 64, model_hash="2" * 64,
                calibration_hash="3" * 64,
                global_calibration_passed=True,
                global_gate_ref="test:global-passed",
                model_version="draft-logistic-l2-v1",
                model_kind="pure_draft",
                availability_mode="prospective",
                input_snapshot_hash="5" * 64,
            ),))

        strategy.fake_profiles = fake_profiles
        strategy.fake_draft = fake_draft
        return strategy, profile_times, draft_times

    def test_transport_time_drives_profiles_draft_and_explicit_previous_state(self) -> None:
        strategy, profile_times, draft_times = self._prepare_strategy_run()
        first_at = NOW + timedelta(seconds=1)
        current_at = NOW + timedelta(seconds=4)
        self.record_transport(first_at, key="first")
        self.record_transport(current_at, key="unchanged")

        with (
            patch("live_betting.shadow_monitor._profiles", side_effect=strategy.fake_profiles),
            patch(
                "live_betting.shadow_monitor.build_draft_curve",
                side_effect=strategy.fake_draft,
            ),
            patch.object(self.store, "insert_decision", return_value=True),
        ):
            result = run_once(
                self.store, strategy, MISSING_VISION,
                now=NOW + timedelta(seconds=5),
            )

        self.assertEqual(result["status"], "no_signal")
        expected_as_of = int(current_at.timestamp())
        self.assertEqual(profile_times, [expected_as_of, expected_as_of])
        self.assertEqual(draft_times, [expected_as_of])
        call = strategy.evaluate.call_args.kwargs
        self.assertEqual(call["decided_at"], current_at)
        self.assertEqual(call["snapshot_observed_at"], current_at)
        self.assertEqual(call["previous_snapshot_observed_at"], first_at)
        self.assertIsNotNone(call["previous_snapshots"])
        self.assertEqual(call["observation"].game_clock_seconds, 604)
        self.assertEqual(call["previous_observation"].game_clock_seconds, 601)

    def test_repolling_one_transport_never_invents_previous_state(self) -> None:
        strategy, _, _ = self._prepare_strategy_run()
        only_at = NOW + timedelta(seconds=1)
        self.record_transport(only_at, key="only")

        for run_at in (NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)):
            with (
                patch(
                    "live_betting.shadow_monitor._profiles",
                    side_effect=strategy.fake_profiles,
                ),
                patch(
                    "live_betting.shadow_monitor.build_draft_curve",
                    side_effect=strategy.fake_draft,
                ),
                patch.object(self.store, "insert_decision", return_value=True),
            ):
                run_once(self.store, strategy, MISSING_VISION, now=run_at)

        self.assertEqual(strategy.evaluate.call_count, 2)
        for call in strategy.evaluate.call_args_list:
            self.assertIsNone(call.kwargs["previous_snapshots"])
            self.assertIsNone(call.kwargs["previous_snapshot_observed_at"])


if __name__ == "__main__":
    unittest.main()
