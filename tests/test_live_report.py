from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone

from live_betting.report import build_report
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)


class LiveReportCohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LiveBettingStore(":memory:")
        self.store.init_schema()
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS event_registry (event_id TEXT PRIMARY KEY)"
        )

    def tearDown(self) -> None:
        self.store.close()

    def insert_strict_mapping(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        event_id: str,
        available_at: str | None = None,
    ) -> int:
        self.store.connection.execute(
            "INSERT OR IGNORE INTO event_registry (event_id) VALUES (?)",
            (event_id,),
        )
        identity_json = "{}"
        identity_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        available_at = available_at or (NOW - timedelta(days=1)).isoformat()
        cursor = self.store.connection.execute(
            """INSERT INTO strict_live_map_mappings
               (raybet_match_id, map_number, event_id, team_one_id, team_two_id,
                canonical_team_one_id, canonical_team_one_name,
                canonical_team_two_id, canonical_team_two_name,
                canonical_identity_json, canonical_identity_hash,
                crosswalk_evidence_json, crosswalk_evidence_hash, stage_scope,
                scheduled_at_utc, raybet_best_of, raybet_identity_json,
                raybet_identity_hash, raybet_metadata_updated_at, source,
                evidence_json, evidence_hash, mapping_version, acceptance_mode,
                automatic_approval_id, accepted_by, accepted_at, recorded_at,
                created_at)
               VALUES (?, ?, ?, 101, 202, 10, 'Canonical One',
                       20, 'Canonical Two', ?, ?, ?, ?, 'main_event', ?, 5,
                       ?, ?, ?, 'test', ?, ?, 'test-v1', 'manual_exact', NULL,
                       'test', ?, ?, ?)""",
            (
                raybet_match_id,
                map_number,
                event_id,
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
        return int(cursor.lastrowid)

    def test_empty_report_has_no_synthetic_evaluation_evidence(self) -> None:
        report = build_report(self.store.connection)

        self.assertEqual(report["evaluation_cohorts"], [])
        self.assertIsNone(report["confidence_intervals_90"])
        self.assertIsNone(report["event_sensitivity"])
        self.assertEqual(report["stability_status"], "descriptive_only")

    def insert_settled_order(
        self,
        index: int,
        *,
        strategy_version: str = "strategy-v1",
        model_hash: str = "2" * 64,
        event_id: str = "event-one",
        include_complete_identity: bool = True,
        series_id: str | None = None,
        map_number: int = 1,
        game_clock_seconds: int = 30 * 60,
        vision_quality: float = 0.96,
        latency_seconds: float = 3.0,
        coverage: float = 0.85,
        signal_price: float = 3.0,
        fill_price: float | None = 3.0,
        order_status: str = "filled",
        rejection_reason: str | None = None,
        strict_available_at: str | None = None,
        settlement_result: str | None = None,
        settlement_review_required: bool = False,
    ) -> None:
        match_id = series_id or f"match-{index}"
        order_key = f"order-{index}"
        decided_at = NOW + timedelta(seconds=index)
        strict_mapping_id = self.insert_strict_mapping(
            raybet_match_id=match_id,
            map_number=map_number,
            event_id=event_id,
            available_at=strict_available_at,
        )
        captured_at = decided_at - timedelta(seconds=latency_seconds)
        probability = 0.7
        outcome = index % 2 == 0
        result = "win" if outcome else "loss"
        return_units = 2.0 if outcome else 0.0
        if settlement_result is not None:
            result = settlement_result
            if result == "review":
                return_units = 0.0
        landmark = {
            "model_version": "draft-logistic-l2-v1",
            "model_kind": "pure_draft",
            "availability_mode": "prospective",
            "feature_hash": "1" * 64,
            "model_hash": model_hash,
            "calibration_hash": "3" * 64,
            "global_gate_ref": "global-gate:passed",
        }
        if not include_complete_identity:
            landmark.pop("model_version")
        contributions = json.dumps({
            "draft_curve": 0.1,
            "__inputs__": {
                "draft_landmark": landmark,
                "strict_live_eligibility": {
                    "mapping_refs": {
                        "strict_mapping_id": strict_mapping_id,
                        "strict_event_id": event_id,
                        "strict_canonical_team_one_id": 10,
                        "strict_canonical_team_one_name": "Canonical One",
                        "strict_canonical_team_two_id": 20,
                        "strict_canonical_team_two_name": "Canonical Two",
                    }
                },
                "vision": {
                    "captured_at": captured_at.isoformat(),
                    "source_frame_ref": f"frame-{index}",
                    "game_clock_seconds": game_clock_seconds,
                },
            },
        })
        self.store.connection.execute(
            """INSERT INTO strategy_decisions
               (decision_key, raybet_match_id, map_number, decided_at,
                underdog_side, market_probability, model_probability, edge,
                data_quality, eligible, reason, contributions_json, input_ref,
                strategy_version)
               VALUES (?, ?, ?, ?, 'team_two', 0.4, ?, ?, ?, 1,
                       'eligible', ?, ?, ?)""",
            (
                f"decision-{index}",
                match_id,
                map_number,
                decided_at.isoformat(),
                probability,
                probability - 0.4,
                coverage,
                contributions,
                f"input-{index}",
                strategy_version,
            ),
        )
        self.store.connection.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                clock_confidence, draft_confidence, source_frame_ref,
                screen_state, confirmed)
               VALUES (?, ?, ?, ?, 0, '[1,2,3,4,5]', '[6,7,8,9,10]',
                       'team_one', ?, ?, ?, 'game', 1)""",
            (
                match_id,
                map_number,
                captured_at.isoformat(),
                game_clock_seconds,
                vision_quality,
                vision_quality,
                f"frame-{index}",
            ),
        )
        self.store.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status, fill_price, filled_at,
                rejection_reason)
               VALUES (?, ?, ?, ?, 'winner|map_1|team_two|', ?, ?, 0.4,
                       ?, ?, ?, ?, 'winner-group', 'team_two', 1, 1.0,
                       ?, ?, ?, ?)""",
            (
                order_key,
                match_id,
                strict_mapping_id,
                f"odds-{index}",
                decided_at.isoformat(),
                probability,
                signal_price,
                f"transport-{index}",
                decided_at.isoformat(),
                (decided_at + timedelta(seconds=15)).isoformat(),
                order_status,
                fill_price,
                (
                    (decided_at + timedelta(seconds=3)).isoformat()
                    if order_status == "filled"
                    else None
                ),
                rejection_reason,
            ),
        )
        self.store.connection.execute(
            """INSERT INTO shadow_map_attempts
               (raybet_match_id, map_number, order_key, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (match_id, map_number, order_key, order_status, decided_at.isoformat()),
        )
        if order_status != "filled":
            return
        self.store.connection.execute(
            """INSERT INTO settlements
               (order_key, result, return_units, settled_at, evidence_ref,
                review_required)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                order_key,
                result,
                return_units,
                (decided_at + timedelta(hours=1)).isoformat(),
                f"result:{index}",
                int(settlement_review_required),
            ),
        )
        self.store.connection.execute(
            """INSERT INTO settlement_reconciliations
               (raybet_match_id, map_number, dota_match_id,
                raybet_winner_side, opendota_winner_side,
                raybet_evidence_ref, opendota_evidence_ref, status, reason,
                first_observed_at, updated_at)
               VALUES (?, ?, ?, 'team_two', 'team_two', ?, ?, 'confirmed',
                       'sources_consistent', ?, ?)""",
            (
                match_id,
                map_number,
                100_000 + index,
                f"raybet-final:{index}",
                f"opendota-result:{index}",
                decided_at.isoformat(),
                decided_at.isoformat(),
            ),
        )

    def test_incompatible_model_cohorts_are_never_pooled(self) -> None:
        self.insert_settled_order(1, model_hash="2" * 64)
        self.insert_settled_order(2, model_hash="4" * 64)
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(len(report["evaluation_cohorts"]), 2)
        self.assertTrue(all(
            cohort["confidence_intervals_90"]["status"]
            == "insufficient_series"
            for cohort in report["evaluation_cohorts"]
        ))
        self.assertTrue(all(
            cohort["confidence_intervals_90"]["series_count"] == 1
            for cohort in report["evaluation_cohorts"]
        ))
        self.assertIsNone(report["brier_score"])
        self.assertIsNone(report["log_loss"])
        self.assertIsNone(report["maximum_drawdown_units"])
        self.assertEqual(
            report["stability_status"], "incompatible_cohorts_not_pooled"
        )

    def test_incomplete_frozen_identity_has_no_probability_headline(self) -> None:
        self.insert_settled_order(1, include_complete_identity=False)
        self.store.connection.commit()

        report = build_report(self.store.connection)
        json.dumps(report, allow_nan=False)
        cohort = report["evaluation_cohorts"][0]

        self.assertFalse(cohort["identity_complete"])
        self.assertIsNone(cohort["brier_score"])
        self.assertIsNone(report["brier_score"])
        self.assertEqual(report["stability_status"], "cohort_identity_incomplete")

    def test_series_bootstrap_and_leave_one_event_out_are_cohort_local(self) -> None:
        self.insert_settled_order(
            1, series_id="series-a", map_number=1, event_id="event-a"
        )
        self.insert_settled_order(
            2, series_id="series-a", map_number=2, event_id="event-a"
        )
        self.insert_settled_order(
            3, series_id="series-b", map_number=1, event_id="event-b"
        )
        self.insert_settled_order(
            4, series_id="series-b", map_number=2, event_id="event-b"
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)
        cohort = report["evaluation_cohorts"][0]
        interval = cohort["confidence_intervals_90"]
        sensitivity = cohort["event_sensitivity"]

        self.assertEqual(interval["status"], "computed")
        self.assertEqual(interval["series_count"], 2)
        self.assertEqual(interval["iterations"], 1000)
        self.assertIn("brier_score", interval["metrics"])
        self.assertIn("roi", interval["metrics"])
        self.assertEqual(
            interval,
            build_report(self.store.connection)["evaluation_cohorts"][0][
                "confidence_intervals_90"
            ],
        )
        self.assertEqual(sensitivity["status"], "computed")
        self.assertEqual(
            {row["held_out_event"] for row in sensitivity["slices"]},
            {"event-a", "event-b"},
        )
        self.assertNotIn(
            "series_cluster_bootstrap_90_ci_missing",
            cohort["promotion_gate_failures"],
        )
        self.assertNotIn(
            "leave_one_event_out_sensitivity_missing",
            cohort["promotion_gate_failures"],
        )

    def test_operational_strata_use_persisted_evidence(self) -> None:
        self.insert_settled_order(
            1,
            series_id="series-a",
            map_number=1,
            game_clock_seconds=25 * 60,
            vision_quality=0.96,
            latency_seconds=2.0,
            coverage=0.85,
            signal_price=3.0,
            fill_price=2.94,
        )
        self.insert_settled_order(
            2,
            series_id="series-a",
            map_number=2,
            game_clock_seconds=35 * 60,
            vision_quality=0.99,
            latency_seconds=6.0,
            coverage=0.65,
            signal_price=4.5,
            fill_price=None,
            order_status="rejected",
            rejection_reason="slippage",
        )
        self.store.connection.commit()

        strata = build_report(self.store.connection)["evaluation_cohorts"][0][
            "stratified"
        ]
        buckets = {
            name: {row["bucket"] for row in rows}
            for name, rows in strata.items()
        }

        self.assertEqual(buckets["team"], {"20:Canonical Two"})
        self.assertEqual(buckets["odds_bucket"], {"2.50-3.99", "4.00-5.99"})
        self.assertEqual(
            buckets["game_minute_bucket"], {"20-29", "30-39"}
        )
        self.assertEqual(
            buckets["vision_quality_bucket"], {"0.95-0.979", "0.98-1.00"}
        )
        self.assertEqual(buckets["signal_reason"], {"eligible"})
        self.assertEqual(buckets["latency_bucket"], {"1-3s", "5-10s"})
        self.assertEqual(
            buckets["coverage_bucket"], {"0.60-0.79", "0.80-1.00"}
        )
        self.assertEqual(
            buckets["rejection"], {"filled", "rejected:slippage"}
        )
        self.assertEqual(
            buckets["slippage_bucket"],
            {"adverse_1-3pct", "rejected_slippage"},
        )

    def test_five_hundred_orders_from_one_event_cannot_claim_stability(self) -> None:
        for index in range(500):
            self.insert_settled_order(index, event_id="only-event")
        self.store.connection.commit()

        report = build_report(self.store.connection)
        cohort = report["evaluation_cohorts"][0]

        self.assertEqual(cohort["settled_orders"], 500)
        self.assertEqual(cohort["event_count"], 1)
        self.assertEqual(
            cohort["stability_status"], "stability_blocked_single_event"
        )
        self.assertEqual(cohort["promotion_gate_status"], "not_passed")
        self.assertIn(
            "cross_event_evidence_missing", cohort["promotion_gate_failures"]
        )
        self.assertNotEqual(report["stability_status"], "stable")

    def test_review_results_are_unscored_and_reconciliation_is_visible(self) -> None:
        self.insert_settled_order(
            1,
            settlement_result="review",
            settlement_review_required=True,
        )
        self.store.connection.execute(
            """UPDATE settlement_reconciliations
                  SET status='manual_review', reason='test_conflict'
                WHERE raybet_match_id='match-1' AND map_number=1"""
        )
        self.store.connection.execute(
            """INSERT INTO settlement_reconciliations
               (raybet_match_id, map_number, dota_match_id,
                raybet_winner_side, opendota_winner_side,
                raybet_evidence_ref, opendota_evidence_ref, status, reason,
                first_observed_at, updated_at)
               VALUES ('reconcile-pending', 1, 10001, NULL, 'team_one',
                       'raybet:pending', 'opendota:pending', 'pending', 'test',
                       ?, ?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["settled_orders"], 0)
        self.assertEqual(report["evaluation_cohorts"][0]["settled_orders"], 0)
        self.assertEqual(report["orders"]["settled"], 0)
        self.assertEqual(
            report["settlement_reconciliation"],
            {"pending": 1, "confirmed": 0, "manual_review": 1},
        )

    def test_order_audit_counts_invalidated_orders_separately_from_evaluation(self) -> None:
        self.insert_settled_order(1)
        self.insert_settled_order(
            2,
            settlement_result="review",
            settlement_review_required=True,
        )
        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, recorded_at)
               VALUES ('shadow_order', 'order-1', 'match-1', 1,
                       'vision_draft_conflict', ?)""",
            (NOW.isoformat(),),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "available")
        self.assertEqual(audit["unknown_reasons"], [])
        self.assertEqual(audit["total_orders"], 2)
        self.assertEqual(audit["included_orders"], 1)
        self.assertEqual(audit["scored_orders"], 0)
        self.assertEqual(audit["excluded_orders"], 1)
        self.assertEqual(audit["invalidated_orders"], 1)
        self.assertEqual(audit["draft_conflict_orders"], 0)
        self.assertEqual(audit["review_required_orders"], 1)
        self.assertEqual(report["invalidated_order_count"], 1)
        self.assertEqual(report["review_required_order_count"], 1)
        self.assertEqual(
            audit["exclusion_reasons"],
            {
                "vision_derived_invalidation": 1,
                "vision_draft_conflict": 0,
                "strict_mapping_invalidated": 0,
                "strict_mapping_unverifiable": 0,
            },
        )
        self.assertEqual(report["orders"]["signals"], 1)
        self.assertEqual(report["settled_orders"], 0)

    def test_decision_audit_counts_vision_invalidation(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, recorded_at)
               VALUES ('strategy_decision', 'decision-1', 'match-1', 1,
                       'vision_observation_invalidated', ?)""",
            (NOW.isoformat(),),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["decision_audit"]

        self.assertEqual(audit["status"], "available")
        self.assertEqual(audit["raw_decisions"], 1)
        self.assertEqual(audit["included_decisions"], 0)
        self.assertEqual(audit["excluded_decisions"], 1)
        self.assertEqual(audit["invalidated_decisions"], 1)
        self.assertEqual(audit["draft_conflict_decisions"], 0)
        self.assertEqual(report["raw_decision_count"], 1)
        self.assertEqual(report["included_decision_count"], 0)
        self.assertEqual(report["invalidated_decision_count"], 1)

    def test_order_audit_preserves_signal_before_future_conflict(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute(
            """INSERT INTO vision_draft_anchors
               (raybet_match_id, map_number, draft_hash, radiant_hero_ids,
                dire_hero_ids, anchored_at, source_frame_ref, status, conflict_at)
               VALUES ('match-1', 1, ?, '[1,2,3,4,5]', '[6,7,8,9,10]',
                       ?, 'anchor-frame', 'anchored', NULL)""",
            (
                "a" * 64,
                NOW.isoformat(),
            ),
        )
        self.store.connection.execute(
            """UPDATE vision_draft_anchors
                  SET status='conflict', conflict_at=?
                WHERE raybet_match_id='match-1' AND map_number=1""",
            ((NOW + timedelta(minutes=1)).isoformat(),),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "available")
        self.assertEqual(audit["total_orders"], 1)
        self.assertEqual(audit["included_orders"], 1)
        self.assertEqual(audit["scored_orders"], 1)
        self.assertEqual(audit["excluded_orders"], 0)
        self.assertEqual(audit["invalidated_orders"], 0)
        self.assertEqual(audit["draft_conflict_orders"], 0)
        self.assertEqual(report["orders"]["signals"], 1)

        decision_audit = report["decision_audit"]
        self.assertEqual(decision_audit["status"], "available")
        self.assertEqual(decision_audit["raw_decisions"], 1)
        self.assertEqual(decision_audit["included_decisions"], 1)
        self.assertEqual(decision_audit["excluded_decisions"], 0)
        self.assertEqual(decision_audit["invalidated_decisions"], 0)
        self.assertEqual(decision_audit["draft_conflict_decisions"], 0)
        self.assertEqual(report["draft_conflict_decision_count"], 0)

    def test_pre_migration_missing_reconciliation_table_fails_closed(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute("DROP TABLE settlement_reconciliations")
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["settled_orders"], 0)
        self.assertEqual(report["orders"]["settled"], 0)
        self.assertEqual(
            report["settlement_reconciliation"],
            {"pending": 0, "confirmed": 0, "manual_review": 0},
        )

    def test_order_audit_marks_missing_conflict_table_unknown(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute("DROP TABLE vision_draft_conflicts")
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "unavailable")
        self.assertIn(
            "vision_draft_conflict_tables_missing", audit["unknown_reasons"]
        )
        self.assertEqual(audit["total_orders"], 1)
        self.assertEqual(audit["included_orders"], 0)
        self.assertEqual(audit["scored_orders"], 0)
        self.assertIsNone(audit["draft_conflict_orders"])
        self.assertIsNone(audit["excluded_orders"])
        self.assertEqual(report["settled_orders"], 0)

    def test_order_audit_marks_missing_invalidation_table_unknown(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute("DROP TABLE vision_derived_invalidations")
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "unavailable")
        self.assertIn(
            "vision_derived_invalidations_table_missing", audit["unknown_reasons"]
        )
        self.assertEqual(audit["total_orders"], 1)
        self.assertEqual(audit["included_orders"], 0)
        self.assertEqual(audit["scored_orders"], 0)
        self.assertIsNone(audit["invalidated_orders"])
        self.assertIsNone(audit["excluded_orders"])
        self.assertIsNone(report["invalidated_decision_count"])
        decision_audit = report["decision_audit"]
        self.assertEqual(decision_audit["status"], "unavailable")
        self.assertIn(
            "vision_derived_invalidations_table_missing",
            decision_audit["unknown_reasons"],
        )
        self.assertEqual(decision_audit["raw_decisions"], 1)
        self.assertEqual(decision_audit["included_decisions"], 0)
        self.assertIsNone(decision_audit["invalidated_decisions"])
        self.assertIsNone(decision_audit["excluded_decisions"])

    def test_order_audit_marks_missing_anchor_table_fail_closed(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute("DROP TABLE vision_draft_anchors")
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "unavailable")
        self.assertIn(
            "vision_draft_conflict_tables_missing", audit["unknown_reasons"]
        )
        self.assertEqual(audit["total_orders"], 1)
        self.assertEqual(audit["included_orders"], 0)
        self.assertEqual(audit["scored_orders"], 0)
        self.assertEqual(report["settled_orders"], 0)
        self.assertEqual(report["invalidated_order_count"], 0)

    def test_strict_schema_loss_excludes_mapped_rows_without_impact_fallback(
        self,
    ) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute(
            "DROP TABLE strict_live_map_mapping_invalidations"
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["decision_count"], 0)
        self.assertEqual(report["orders"]["signals"], 0)
        for audit, raw_key, unverifiable_key in (
            (
                report["decision_audit"],
                "raw_decisions",
                "strict_mapping_unverifiable_decisions",
            ),
            (
                report["order_audit"],
                "total_orders",
                "strict_mapping_unverifiable_orders",
            ),
        ):
            self.assertEqual(audit["status"], "unavailable")
            self.assertIn(
                "strict_live_map_mapping_invalidations_table_missing",
                audit["unknown_reasons"],
            )
            self.assertEqual(audit[raw_key], 1)
            self.assertEqual(audit["excluded_decisions" if raw_key == "raw_decisions" else "excluded_orders"], 1)
            self.assertIsNone(
                audit[
                    "strict_mapping_invalidated_decisions"
                    if raw_key == "raw_decisions"
                    else "strict_mapping_invalidated_orders"
                ]
            )
            self.assertEqual(audit[unverifiable_key], 1)

    def test_naive_strict_mapping_timestamp_is_unverifiable(self) -> None:
        self.insert_settled_order(
            1,
            strict_available_at=(NOW - timedelta(days=1))
            .replace(tzinfo=None)
            .isoformat(),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["decision_count"], 0)
        self.assertEqual(report["orders"]["signals"], 0)
        self.assertEqual(
            report["decision_audit"]["strict_mapping_unverifiable_decisions"],
            1,
        )
        self.assertEqual(
            report["order_audit"]["strict_mapping_unverifiable_orders"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
