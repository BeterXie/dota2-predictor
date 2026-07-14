from __future__ import annotations

import hashlib
import sqlite3
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from live_betting.browser_contract import (
    BrowserEvent,
    EventType,
    Transport,
    canonical_json,
    payload_sha256,
)
from live_betting.browser_ingest import BrowserEventIngestor
from live_betting.market_state import build_market_surface
from live_betting.markets import normalized_state_hash
from live_betting.models import Market, OddsSnapshot
from live_betting.profiles.draft_curve import DraftCurve, DraftPoint
from live_betting.research import (
    manual_clock_evidence,
    record_research_prediction,
    research_summary,
)
from live_betting.report import build_report
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
MATCH_ID = "38408499"


@dataclass(frozen=True)
class Mapping:
    mapping_id: int = 7

    def input_refs(self) -> dict[str, object]:
        return {
            "strict_mapping_id": self.mapping_id,
            "strict_event_id": "ewc-dota2-2026",
            "strict_canonical_identity_hash": "4" * 64,
        }


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
            MATCH_ID,
            odds_id,
            group,
            at,
            price,
            5,
            Market(market_type, "map_1", side, line, f"{side}:{line}", True),
        )
        for odds_id, group, market_type, side, line, price in definitions
    ]


def observation(at: datetime) -> VisionObservation:
    return VisionObservation(
        MATCH_ID,
        1,
        at,
        30 * 60,
        False,
        (1, 2, 3, 4, 5),
        (6, 7, 8, 9, 10),
        0.95,
        0.95,
        f"frame:{at.isoformat()}",
        "game",
        "team_one",
    )


def curve(*, global_passed: bool = True) -> DraftCurve:
    return DraftCurve(
        (
            DraftPoint(
                30,
                0.7,
                0.0,
                0.0,
                1.0,
                validated=True,
                support=500,
                calibration_ref="calibration:global",
                input_refs=("model:immutable", "features:immutable"),
                uncertainty=0.02,
                feature_hash="1" * 64,
                model_hash="2" * 64,
                calibration_hash="3" * 64,
                global_calibration_passed=global_passed,
                global_gate_ref="global-gate:passed" if global_passed else "",
            ),
        )
    )


class ResearchLivePredictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LiveBettingStore(":memory:")
        self.store.init_schema()

    def tearDown(self) -> None:
        self.store.close()

    def record_transport(
        self,
        key: str,
        at: datetime,
        rows: list[OddsSnapshot],
    ) -> str:
        state_hash = normalized_state_hash(rows)
        status, _ = self.store.store_odds_observation(
            source="direct",
            observation_key=key,
            source_event_id=None,
            raybet_match_id=MATCH_ID,
            observed_at=at,
            normalized_state_hash=state_hash,
            snapshots=rows,
        )
        self.assertEqual(status, "on_time")
        return state_hash

    def test_research_labels_are_append_only_and_never_create_shadow_orders(
        self,
    ) -> None:
        first_at = NOW
        first_rows = snapshots(first_at)
        first_hash = self.record_transport("first", first_at, first_rows)
        first = record_research_prediction(
            self.store,
            snapshots=first_rows,
            surface=build_market_surface(first_rows),
            observation=observation(first_at),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="first",
            transport_hash=first_hash,
            transport_at=first_at,
            created_at=first_at + timedelta(seconds=1),
        )
        self.assertIsNotNone(first)
        self.assertTrue(first.inserted)
        self.assertEqual(first.gate_status, "passed")
        self.store.init_schema()
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_live_predictions"
            ).fetchone()[0],
            1,
        )

        second_at = NOW + timedelta(seconds=10)
        second_rows = snapshots(second_at, underdog_price=2.8)
        second_hash = self.record_transport("second", second_at, second_rows)
        second = record_research_prediction(
            self.store,
            snapshots=second_rows,
            surface=build_market_surface(second_rows),
            observation=observation(second_at),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="second",
            transport_hash=second_hash,
            transport_at=second_at,
            created_at=second_at + timedelta(seconds=1),
        )
        self.assertIsNotNone(second)
        self.assertEqual(second.price_labels_inserted, 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_live_predictions"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_price_labels"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM shadow_orders"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM shadow_map_attempts"
            ).fetchone()[0],
            0,
        )

        result = SimpleNamespace(
            raybet_match_id=MATCH_ID,
            map_number=1,
            dota_match_id=9001,
            winner_side="team_two",
            team_one_kills=20,
            team_two_kills=35,
            duration_seconds=2400,
            evidence_ref="opendota:9001",
            settled_at=NOW + timedelta(hours=1),
        )
        self.assertTrue(self.store.insert_map_result(result))
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_result_labels"
            ).fetchone()[0],
            2,
        )
        summary = research_summary(self.store.connection)
        self.assertEqual(summary["predictions"], 2)
        self.assertEqual(summary["successor_price_labels"], 1)
        self.assertEqual(summary["result_labels"], 2)
        self.assertEqual(summary["scorable_model_results"], 2)
        self.assertEqual(summary["model_accuracy"], 0.0)
        self.assertEqual(summary["actionability"], "research_only")
        self.assertEqual(build_report(self.store.connection)["research"], summary)

        prediction_key = first.prediction_key
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.connection.execute(
                "UPDATE research_live_predictions SET gate_status='failed' WHERE prediction_key=?",
                (prediction_key,),
            )
        self.store.connection.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.connection.execute(
                "DELETE FROM research_live_predictions WHERE prediction_key=?",
                (prediction_key,),
            )
        self.store.connection.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.connection.execute("UPDATE research_price_labels SET price=9.0")
        self.store.connection.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.connection.execute("DELETE FROM research_result_labels")
        self.store.connection.rollback()

    def test_failed_global_gate_keeps_raw_probability_research_only(self) -> None:
        rows = snapshots(NOW)
        state_hash = self.record_transport("global-failed", NOW, rows)
        result = record_research_prediction(
            self.store,
            snapshots=rows,
            surface=build_market_surface(rows),
            observation=observation(NOW),
            draft_curve=curve(global_passed=False),
            strict_mapping=Mapping(),
            transport_key="global-failed",
            transport_hash=state_hash,
            transport_at=NOW,
            created_at=NOW + timedelta(seconds=1),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.gate_status, "failed")
        self.assertIn("global_calibration_gate_not_passed", result.gate_failures)
        row = self.store.connection.execute(
            """SELECT raw_model_probability, actionability, gate_status
                 FROM research_live_predictions"""
        ).fetchone()
        self.assertAlmostEqual(row["raw_model_probability"], 0.3)
        self.assertEqual(row["actionability"], "research_only")
        self.assertEqual(row["gate_status"], "failed")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM shadow_orders"
            ).fetchone()[0],
            0,
        )

    def insert_manual_event(
        self, *, captured_at: datetime, current_index: int, clock: str
    ) -> str:
        payload = {"currentIndex": current_index, "time": clock}
        event_id = hashlib.sha256(captured_at.isoformat().encode()).hexdigest()
        event = BrowserEvent(
            schema_version=1,
            event_id=event_id,
            capture_session_id="a" * 32,
            captured_at_utc=captured_at,
            page_origin="https://www.ray086.com",
            page_path="/dota2/live",
            source_path="/manualControlData",
            transport=Transport.PAGE_STATE,
            event_type=EventType.MANUAL_CONTROL,
            raybet_match_id=MATCH_ID,
            game_id=151,
            payload=payload,
            payload_hash=payload_sha256(payload),
            payload_bytes=len(canonical_json(payload)),
            capture_reason="diagnostic_untrusted",
            extension_version="1.0.0",
        )
        result = BrowserEventIngestor(
            clock=lambda: captured_at + timedelta(seconds=1)
        ).ingest(self.store, event)
        self.assertEqual(result.processing_status, "audit_only")
        return event_id

    def test_manual_clock_requires_monotonic_same_map_transport_alignment(self) -> None:
        transport_at = NOW
        rows = snapshots(transport_at)
        state_hash = self.record_transport("manual", transport_at, rows)
        self.insert_manual_event(
            captured_at=NOW - timedelta(seconds=10), current_index=1, clock="10:00"
        )
        latest_event = self.insert_manual_event(
            captured_at=NOW - timedelta(seconds=5), current_index=1, clock="10:05"
        )
        evidence = manual_clock_evidence(
            self.store.connection,
            raybet_match_id=MATCH_ID,
            map_number=1,
            transport_key="manual",
            transport_hash=state_hash,
            transport_at=transport_at,
        )
        self.assertEqual(evidence.event_id, latest_event)
        self.assertEqual(evidence.seconds, 605)
        self.assertEqual(evidence.trust, "diagnostic_untrusted")
        self.assertEqual(evidence.validation, "validated_diagnostic")

        mismatch = manual_clock_evidence(
            self.store.connection,
            raybet_match_id=MATCH_ID,
            map_number=2,
            transport_key="manual",
            transport_hash=state_hash,
            transport_at=transport_at,
        )
        self.assertIsNone(mismatch.seconds)
        self.assertEqual(mismatch.validation, "map_index_mismatch")
        wrong_transport = manual_clock_evidence(
            self.store.connection,
            raybet_match_id=MATCH_ID,
            map_number=1,
            transport_key="manual",
            transport_hash="f" * 64,
            transport_at=transport_at,
        )
        self.assertIsNone(wrong_transport.seconds)
        self.assertEqual(wrong_transport.trust, "not_observed")
        self.assertEqual(wrong_transport.validation, "transport_mismatch")

        later_at = NOW + timedelta(seconds=5)
        later_rows = snapshots(later_at)
        later_hash = self.record_transport("manual-later", later_at, later_rows)
        self.insert_manual_event(
            captured_at=NOW + timedelta(seconds=1), current_index=1, clock="09:59"
        )
        non_monotonic = manual_clock_evidence(
            self.store.connection,
            raybet_match_id=MATCH_ID,
            map_number=1,
            transport_key="manual-later",
            transport_hash=later_hash,
            transport_at=later_at,
        )
        self.assertIsNone(non_monotonic.seconds)
        self.assertEqual(non_monotonic.validation, "non_monotonic")


if __name__ == "__main__":
    unittest.main()
