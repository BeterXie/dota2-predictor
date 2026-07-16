from __future__ import annotations

import hashlib
import sqlite3
import unittest
from dataclasses import dataclass, replace
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
    _winner_quotes,
    append_research_successor_price_labels,
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
            1,
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


def curve(*, global_passed: bool = True, model_hash: str = "2" * 64) -> DraftCurve:
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
                model_hash=model_hash,
                calibration_hash="3" * 64,
                global_calibration_passed=global_passed,
                global_gate_ref="global-gate:passed" if global_passed else "",
                model_version="draft-logistic-l2-v1",
                model_kind="pure_draft",
                availability_mode="prospective",
                input_snapshot_hash="5" * 64,
            ),
        )
    )


class ResearchLivePredictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LiveBettingStore(":memory:")
        self.store.init_schema()
        self.insert_strict_mapping()

    def tearDown(self) -> None:
        self.store.close()

    def insert_strict_mapping(self) -> None:
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS event_registry (event_id TEXT PRIMARY KEY)"
        )
        self.store.connection.execute(
            "INSERT INTO event_registry (event_id) VALUES ('ewc-dota2-2026')"
        )
        identity_json = "{}"
        identity_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        available_at = (NOW - timedelta(days=1)).isoformat()
        self.store.connection.execute(
            """INSERT INTO strict_live_map_mappings
               (mapping_id, raybet_match_id, map_number, event_id,
                team_one_id, team_two_id, canonical_team_one_id,
                canonical_team_one_name, canonical_team_two_id,
                canonical_team_two_name, canonical_identity_json,
                canonical_identity_hash, crosswalk_evidence_json,
                crosswalk_evidence_hash, stage_scope, scheduled_at_utc,
                raybet_best_of, raybet_identity_json, raybet_identity_hash,
                raybet_metadata_updated_at, source, evidence_json,
                evidence_hash, mapping_version, acceptance_mode,
                automatic_approval_id, accepted_by, accepted_at, recorded_at,
                created_at)
               VALUES (7, ?, 1, 'ewc-dota2-2026', 101, 202, 101, 'Alpha',
                       202, 'Beta', ?, ?, ?, ?, 'main_event', ?, 3, ?, ?, ?,
                       'test', ?, ?, 'test-v1', 'manual_exact', NULL, 'test',
                       ?, ?, ?)""",
            (
                MATCH_ID,
                identity_json,
                identity_hash,
                identity_json,
                identity_hash,
                available_at,
                identity_json,
                identity_hash,
                available_at,
                identity_json,
                identity_hash,
                available_at,
                available_at,
                available_at,
            ),
        )
        self.store.connection.commit()

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

    def record_reconciliation(
        self,
        dota_match_id: int,
        settled_at: datetime,
        *,
        status: str = "confirmed",
        reason: str = "source_winners_agree",
    ) -> None:
        self.store.record_settlement_reconciliation(
            raybet_match_id=MATCH_ID,
            map_number=1,
            dota_match_id=dota_match_id,
            raybet_status="confirmed",
            raybet_winner_side="team_two",
            opendota_winner_side="team_two",
            raybet_evidence_ref=f"raybet:final:{dota_match_id}",
            opendota_evidence_ref=f"opendota:{dota_match_id}",
            raybet_facts={"winner_side": "team_two"},
            opendota_facts={"winner_side": "team_two"},
            status=status,
            reason=reason,
            observed_at=settled_at,
        )

    def test_settled_status_five_is_not_a_research_quote(self) -> None:
        settled = [replace(row, status=5) for row in snapshots(NOW)]
        with self.assertRaisesRegex(ValueError, "complete winner market"):
            _winner_quotes(settled)

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
        self.record_reconciliation(result.dota_match_id, result.settled_at)
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

    def test_prediction_after_settlement_is_not_result_labeled(self) -> None:
        result = SimpleNamespace(
            raybet_match_id=MATCH_ID,
            map_number=1,
            dota_match_id=9_001,
            winner_side="team_two",
            team_one_kills=20,
            team_two_kills=35,
            duration_seconds=2_400,
            evidence_ref="opendota:9001",
            settled_at=NOW,
        )
        self.assertTrue(self.store.insert_map_result(result))

        observed_at = NOW + timedelta(seconds=5)
        rows = snapshots(observed_at)
        state_hash = self.record_transport("post-settlement", observed_at, rows)
        prediction = record_research_prediction(
            self.store,
            snapshots=rows,
            surface=build_market_surface(rows),
            observation=observation(observed_at),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="post-settlement",
            transport_hash=state_hash,
            transport_at=observed_at,
            created_at=observed_at,
        )
        self.assertIsNotNone(prediction)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_result_labels"
            ).fetchone()[0],
            0,
        )
        self.store.connection.execute(
            """INSERT INTO research_result_labels
               (label_key, prediction_key, winner_side, selected_side_win,
                dota_match_id, evidence_ref, settled_at, created_at)
               VALUES (?, ?, 'team_two', 1, 9001, 'legacy:invalid', ?, ?)""",
            (
                f"{prediction.prediction_key}:legacy-result",
                prediction.prediction_key,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        self.store.connection.commit()
        self.assertEqual(research_summary(self.store.connection)["result_labels"], 0)

    def test_backfilled_result_does_not_label_later_prediction(self) -> None:
        observed_at = NOW + timedelta(seconds=5)
        rows = snapshots(observed_at)
        state_hash = self.record_transport("before-backfill", observed_at, rows)
        record_research_prediction(
            self.store,
            snapshots=rows,
            surface=build_market_surface(rows),
            observation=observation(observed_at),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="before-backfill",
            transport_hash=state_hash,
            transport_at=observed_at,
            created_at=observed_at,
        )
        self.assertTrue(self.store.insert_map_result(SimpleNamespace(
            raybet_match_id=MATCH_ID,
            map_number=1,
            dota_match_id=9_003,
            winner_side="team_two",
            team_one_kills=20,
            team_two_kills=35,
            duration_seconds=2_400,
            evidence_ref="opendota:9003",
            settled_at=NOW,
        )))
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_result_labels"
            ).fetchone()[0],
            0,
        )

    def test_manual_review_reconciliation_removes_result_from_scoring(self) -> None:
        rows = snapshots(NOW)
        state_hash = self.record_transport("manual-review", NOW, rows)
        prediction = record_research_prediction(
            self.store,
            snapshots=rows,
            surface=build_market_surface(rows),
            observation=observation(NOW),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="manual-review",
            transport_hash=state_hash,
            transport_at=NOW,
            created_at=NOW,
        )
        self.assertIsNotNone(prediction)
        settled_at = NOW + timedelta(hours=1)
        self.record_reconciliation(9_004, settled_at)
        self.assertTrue(self.store.insert_map_result(SimpleNamespace(
            raybet_match_id=MATCH_ID,
            map_number=1,
            dota_match_id=9_004,
            winner_side="team_two",
            team_one_kills=20,
            team_two_kills=35,
            duration_seconds=2_400,
            evidence_ref="settlement-reconciliation:38408499:map:1",
            settled_at=settled_at,
        )))
        self.assertEqual(research_summary(self.store.connection)["result_labels"], 1)

        self.record_reconciliation(
            9_004,
            settled_at,
            status="manual_review",
            reason="strict_live_mapping_invalidated",
        )

        summary = research_summary(self.store.connection)
        self.assertEqual(summary["result_labels"], 0)
        self.assertEqual(summary["scorable_model_results"], 0)
        self.assertEqual(summary["raw_result_labels"], 1)
        self.assertEqual(summary["included_result_labels"], 0)
        self.assertEqual(summary["excluded_result_labels"], 1)
        self.assertEqual(
            summary["result_label_audit"]["exclusion_reasons"][
                "reconciliation_not_confirmed"
            ],
            1,
        )

    def test_mapping_invalidation_removes_confirmed_research_lineage(self) -> None:
        rows = snapshots(NOW)
        state_hash = self.record_transport("strict-invalidated", NOW, rows)
        prediction = record_research_prediction(
            self.store,
            snapshots=rows,
            surface=build_market_surface(rows),
            observation=observation(NOW),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="strict-invalidated",
            transport_hash=state_hash,
            transport_at=NOW,
            created_at=NOW,
        )
        self.assertIsNotNone(prediction)
        settled_at = NOW + timedelta(hours=1)
        self.record_reconciliation(9_005, settled_at)
        self.assertTrue(self.store.insert_map_result(SimpleNamespace(
            raybet_match_id=MATCH_ID,
            map_number=1,
            dota_match_id=9_005,
            winner_side="team_two",
            team_one_kills=20,
            team_two_kills=35,
            duration_seconds=2_400,
            evidence_ref="settlement-reconciliation:38408499:map:1",
            settled_at=settled_at,
        )))
        self.assertEqual(research_summary(self.store.connection)["result_labels"], 1)

        self.store.connection.execute(
            """INSERT INTO strict_live_map_mapping_invalidations
               (mapping_id, reason, invalidated_by, invalidated_at, recorded_at)
               VALUES (7, 'withdrawn evidence', 'test', ?, ?)""",
            (settled_at.isoformat(), settled_at.isoformat()),
        )
        self.store.connection.commit()

        summary = research_summary(self.store.connection)
        self.assertEqual(summary["raw_predictions"], 1)
        self.assertEqual(summary["included_predictions"], 0)
        self.assertEqual(summary["excluded_predictions"], 1)
        self.assertEqual(
            summary["prediction_audit"]["exclusion_reasons"][
                "strict_mapping_invalidated"
            ],
            1,
        )
        self.assertEqual(summary["raw_result_labels"], 1)
        self.assertEqual(summary["included_result_labels"], 0)
        self.assertEqual(summary["excluded_result_labels"], 1)
        self.assertEqual(
            summary["result_label_audit"]["exclusion_reasons"][
                "strict_mapping_invalidated"
            ],
            1,
        )

    def test_missing_strict_invalidation_schema_is_audited_fail_closed(self) -> None:
        rows = snapshots(NOW)
        state_hash = self.record_transport("strict-schema-missing", NOW, rows)
        self.assertIsNotNone(record_research_prediction(
            self.store,
            snapshots=rows,
            surface=build_market_surface(rows),
            observation=observation(NOW),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="strict-schema-missing",
            transport_hash=state_hash,
            transport_at=NOW,
            created_at=NOW,
        ))
        self.store.connection.execute(
            "DROP TABLE strict_live_map_mapping_invalidations"
        )
        self.store.connection.commit()

        summary = research_summary(self.store.connection)

        self.assertEqual(summary["audit_status"], "unavailable")
        self.assertIn(
            "strict_live_map_mapping_invalidations_table_missing",
            summary["unavailable_reasons"],
        )
        self.assertEqual(summary["raw_predictions"], 1)
        self.assertEqual(summary["included_predictions"], 0)
        self.assertEqual(summary["excluded_predictions"], 1)
        self.assertIsNone(
            summary["prediction_audit"]["exclusion_reasons"][
                "strict_mapping_invalidated"
            ]
        )
        self.assertEqual(
            summary["prediction_audit"]["exclusion_reasons"][
                "strict_mapping_unverifiable"
            ],
            1,
        )

    def test_vision_invalidation_has_an_explicit_prediction_denominator(self) -> None:
        rows = snapshots(NOW)
        state_hash = self.record_transport("vision-invalidated", NOW, rows)
        prediction = record_research_prediction(
            self.store,
            snapshots=rows,
            surface=build_market_surface(rows),
            observation=observation(NOW),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="vision-invalidated",
            transport_hash=state_hash,
            transport_at=NOW,
            created_at=NOW,
        )
        self.assertIsNotNone(prediction)
        assert prediction is not None
        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, recorded_at)
               VALUES ('research_prediction', ?, ?, 1,
                       'vision_observation_invalidated', ?)""",
            (prediction.prediction_key, MATCH_ID, NOW.isoformat()),
        )
        self.store.connection.commit()

        summary = research_summary(self.store.connection)

        self.assertEqual(summary["raw_predictions"], 1)
        self.assertEqual(summary["included_predictions"], 0)
        self.assertEqual(summary["excluded_predictions"], 1)
        self.assertEqual(
            summary["prediction_audit"]["exclusion_reasons"][
                "vision_invalidated"
            ],
            1,
        )

    def test_first_later_winner_quote_labels_without_auxiliary_markets(self) -> None:
        first_rows = snapshots(NOW)
        first_hash = self.record_transport("first-winner", NOW, first_rows)
        record_research_prediction(
            self.store,
            snapshots=first_rows,
            surface=build_market_surface(first_rows),
            observation=observation(NOW),
            draft_curve=curve(),
            strict_mapping=Mapping(),
            transport_key="first-winner",
            transport_hash=first_hash,
            transport_at=NOW,
            created_at=NOW,
        )

        successor_at = NOW + timedelta(seconds=5)
        winner_rows = snapshots(successor_at)[:2]
        successor_hash = self.record_transport(
            "winner-only", successor_at, winner_rows
        )
        inserted = append_research_successor_price_labels(
            self.store,
            raybet_match_id=MATCH_ID,
            map_number=1,
            transport_key="winner-only",
            transport_hash=successor_hash,
            transport_at=successor_at,
            snapshots=winner_rows,
            created_at=successor_at,
        )
        self.assertEqual(inserted, 1)
        label = self.store.connection.execute(
            "SELECT transport_key, seconds_after_prediction FROM research_price_labels"
        ).fetchone()
        self.assertEqual(tuple(label), ("winner-only", 5.0))

    def test_summary_separates_model_and_calibration_cohorts(self) -> None:
        for index, model_hash in enumerate(("2" * 64, "4" * 64)):
            observed_at = NOW + timedelta(seconds=index * 10)
            rows = snapshots(observed_at, underdog_price=3.0 - index * 0.1)
            key = f"cohort-{index}"
            state_hash = self.record_transport(key, observed_at, rows)
            record_research_prediction(
                self.store,
                snapshots=rows,
                surface=build_market_surface(rows),
                observation=observation(observed_at),
                draft_curve=curve(model_hash=model_hash),
                strict_mapping=Mapping(),
                transport_key=key,
                transport_hash=state_hash,
                transport_at=observed_at,
                created_at=observed_at,
            )
        settled_at = NOW + timedelta(hours=1)
        self.record_reconciliation(9_002, settled_at)
        self.assertTrue(self.store.insert_map_result(SimpleNamespace(
            raybet_match_id=MATCH_ID,
            map_number=1,
            dota_match_id=9_002,
            winner_side="team_two",
            team_one_kills=20,
            team_two_kills=35,
            duration_seconds=2_400,
            evidence_ref="opendota:9002",
            settled_at=settled_at,
        )))
        summary = research_summary(self.store.connection)
        self.assertEqual(len(summary["model_cohorts"]), 2)
        self.assertTrue(all(
            cohort["identity_complete"] for cohort in summary["model_cohorts"]
        ))
        self.assertEqual(summary["scorable_model_results"], 2)
        self.assertIsNone(summary["model_accuracy"])
        self.assertIsNone(summary["model_brier_score"])
        self.assertIsNone(summary["model_log_loss"])

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
