from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from live_betting.health import record_health
from live_betting.notifications import claim
from live_betting.smtp_delivery import SMTPConfig, build_message
from live_betting.storage import LiveBettingStore
from web.alerts import (
    acknowledge_alert,
    active_alerts,
    init_alert_schema,
    reconcile_alerts,
)


NOW = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)


class MonitorAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "alerts.db"
        self.store = LiveBettingStore(self.database)
        self.store.init_schema()
        init_alert_schema(self.store.connection)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _insert_strict_mapping(
        self,
        connection: sqlite3.Connection,
        *,
        raybet_match_id: str,
        map_number: int = 1,
        acceptance_mode: str = "manual_exact",
        automatic_approval_id: int | None = None,
        accepted_at: datetime = NOW,
    ) -> int:
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            cursor = connection.execute(
                """INSERT INTO strict_live_map_mappings
                   (raybet_match_id, map_number, event_id, team_one_id,
                    team_two_id, canonical_team_one_id, canonical_team_one_name,
                    canonical_team_two_id, canonical_team_two_name,
                    canonical_identity_json, canonical_identity_hash,
                    crosswalk_evidence_json, crosswalk_evidence_hash,
                    stage_scope, scheduled_at_utc, raybet_best_of,
                    raybet_identity_json, raybet_identity_hash,
                    raybet_metadata_updated_at, source, evidence_json,
                    evidence_hash, mapping_version, acceptance_mode,
                    automatic_approval_id, accepted_by, accepted_at,
                    recorded_at, created_at)
                   VALUES (?, ?, 'event-test', 101, 202, 101, 'Alpha',
                           202, 'Beta', '{}', ?, '{}', ?, 'main_event', ?, 3,
                           '{}', ?, ?, 'test', '{}', ?, 'test-v1', ?, ?,
                           'test', ?, ?, ?)""",
                (
                    raybet_match_id,
                    map_number,
                    "a" * 64,
                    "b" * 64,
                    NOW.isoformat(),
                    "c" * 64,
                    NOW.isoformat(),
                    "d" * 64,
                    acceptance_mode,
                    automatic_approval_id,
                    accepted_at.isoformat(),
                    accepted_at.isoformat(),
                    accepted_at.isoformat(),
                ),
            )
            connection.commit()
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        return int(cursor.lastrowid)

    def _insert_pending_order(
        self,
        connection: sqlite3.Connection,
        *,
        order_key: str = "order-alert",
        raybet_match_id: str = "match-1",
        model_probability: object = 0.58,
        market_probability: object = 0.40,
        signal_price: object = 2.5,
        signal_at: datetime = NOW,
        strict_mapping_id: int | None = None,
        map_number: int = 1,
        create_strict_mapping: bool = True,
        insert_vision_anchor: bool = True,
        vision_anchor_at: datetime | None = None,
        insert_decision_lineage: bool = True,
    ) -> int | None:
        strategy_version = "paper-test-v1"
        input_ref = f"paper-input:{order_key}"
        identity = "|".join(
            (
                raybet_match_id,
                f"odds-{order_key}",
                f"group-{order_key}",
                "team_one",
                "winner|map_1|team_one|",
                strategy_version,
                input_ref,
            )
        )
        persisted_order_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        if strict_mapping_id is None and create_strict_mapping:
            strict_mapping_id = self._insert_strict_mapping(
                connection,
                raybet_match_id=raybet_match_id,
                map_number=map_number,
            )
        connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                persisted_order_key,
                raybet_match_id,
                strict_mapping_id,
                f"odds-{order_key}",
                "winner|map_1|team_one|",
                signal_at.isoformat(),
                model_probability,
                market_probability,
                signal_price,
                f"transport-{order_key}",
                signal_at.isoformat(),
                (signal_at + timedelta(seconds=15)).isoformat(),
                f"group-{order_key}",
                "team_one",
                1,
                1.0,
                "pending",
            ),
        )
        connection.execute(
            """INSERT INTO shadow_map_attempts
               (raybet_match_id, map_number, order_key, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (
                raybet_match_id,
                map_number,
                persisted_order_key,
                signal_at.isoformat(),
            ),
        )
        if insert_decision_lineage:
            decision_key = f"paper-decision:{order_key}"
            connection.execute(
                """INSERT INTO strategy_decisions
                   (decision_key, raybet_match_id, map_number, decided_at,
                    underdog_side, market_probability, model_probability, edge,
                    data_quality, eligible, reason, contributions_json, input_ref,
                    strategy_version)
                   VALUES (?, ?, ?, ?, 'team_one', ?, ?, 0.1, 0.8, 1,
                           'eligible', ?, ?, ?)""",
                (
                    decision_key,
                    raybet_match_id,
                    map_number,
                    signal_at.isoformat(),
                    market_probability,
                    model_probability,
                    json.dumps(
                        {
                            "__inputs__": {
                                "strict_live_eligibility": {
                                    "mapping_refs": {
                                        "strict_mapping_id": strict_mapping_id
                                    }
                                }
                            }
                        },
                        sort_keys=True,
                    ),
                    input_ref,
                    strategy_version,
                ),
            )
            connection.execute(
                """INSERT INTO shadow_order_decision_lineage
                   (order_key, decision_key, recorded_at) VALUES (?, ?, ?)""",
                (persisted_order_key, decision_key, signal_at.isoformat()),
            )
        if insert_vision_anchor:
            connection.execute(
                """INSERT INTO vision_draft_anchors
                   (raybet_match_id, map_number, draft_hash, radiant_hero_ids,
                    dire_hero_ids, anchored_at, source_frame_ref, status,
                    conflict_at)
                   VALUES (?, ?, ?, '[]', '[]', ?, ?, 'anchored', NULL)""",
                (
                    raybet_match_id,
                    map_number,
                    "a" * 64,
                    (
                        vision_anchor_at or signal_at - timedelta(seconds=1)
                    ).isoformat(),
                    f"frame-{order_key}",
                ),
            )
        connection.commit()
        return strict_mapping_id

    @staticmethod
    def _persisted_order_key(
        order_key: str = "order-alert", raybet_match_id: str = "match-1"
    ) -> str:
        identity = "|".join(
            (
                raybet_match_id,
                f"odds-{order_key}",
                f"group-{order_key}",
                "team_one",
                "winner|map_1|team_one|",
                "paper-test-v1",
                f"paper-input:{order_key}",
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    def _insert_automatic_approval(
        self,
        connection: sqlite3.Connection,
        *,
        source_mapping_id: int,
    ) -> int:
        cursor = connection.execute(
            """INSERT INTO strict_live_automatic_evidence_approvals
               (source_mapping_id, raybet_match_id, event_id, team_one_id,
                team_two_id, canonical_team_one_id, canonical_team_two_id,
                raybet_identity_hash, canonical_identity_hash,
                crosswalk_evidence_hash, evidence_hash, approved_by,
                approved_at, recorded_at)
               VALUES (?, 'match-source', 'event-test', 101, 202, 101, 202,
                       ?, ?, ?, ?, 'test', ?, ?)""",
            (
                source_mapping_id,
                "e" * 64,
                "f" * 64,
                "1" * 64,
                "2" * 64,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)

    def _assert_no_paper_signal(self, connection: sqlite3.Connection) -> None:
        self.assertEqual(
            connection.execute(
                """SELECT COUNT(*) FROM monitor_alert_incidents
                    WHERE status='active' AND category='paper_signal'"""
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE json_extract(payload_json, '$.category')='paper_signal'"""
            ).fetchone()[0],
            0,
        )

    def test_operational_alert_uses_grace_dedupes_and_recovers(self) -> None:
        record_health(
            self.store.connection,
            "raybet_worker",
            "degraded",
            heartbeat_at=NOW,
            error_at=NOW,
            error="timeout",
        )

        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=30)
        reconcile_alerts(
            self.store.connection,
            now=NOW + timedelta(seconds=29),
            grace_seconds=30,
        )
        self.assertEqual(active_alerts(self.store.connection), [])

        reconcile_alerts(
            self.store.connection,
            now=NOW + timedelta(seconds=31),
            grace_seconds=30,
        )
        reconcile_alerts(
            self.store.connection,
            now=NOW + timedelta(seconds=95),
            grace_seconds=30,
        )
        alerts = active_alerts(self.store.connection)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["dedupe_key"], "operational:raybet_worker")
        self.assertEqual(alerts[0]["occurrence_count"], 2)

        recovered_at = NOW + timedelta(seconds=96)
        record_health(
            self.store.connection,
            "raybet_worker",
            "healthy",
            heartbeat_at=recovered_at,
            success_at=recovered_at,
        )
        reconcile_alerts(self.store.connection, now=recovered_at, grace_seconds=30)

        self.assertEqual(active_alerts(self.store.connection), [])
        self.assertEqual(
            [row[0] for row in self.store.connection.execute(
                "SELECT action FROM monitor_alert_audit ORDER BY audit_id"
            )],
            ["opened", "observed", "recovered"],
        )

    def test_missing_optional_smtp_does_not_remain_an_operational_alert(self) -> None:
        record_health(
            self.store.connection,
            "mail_worker",
            "unhealthy",
            heartbeat_at=NOW,
            error_at=NOW,
            error="smtp_authentication_failed",
        )
        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=0)
        self.assertEqual(
            active_alerts(self.store.connection)[0]["dedupe_key"],
            "operational:mail_worker",
        )

        later = NOW + timedelta(seconds=1)
        record_health(
            self.store.connection,
            "mail_worker",
            "degraded",
            heartbeat_at=later,
            error_at=later,
            error="configuration_missing",
        )
        reconcile_alerts(self.store.connection, now=later, grace_seconds=0)

        self.assertEqual(active_alerts(self.store.connection), [])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM monitor_alert_incidents WHERE dedupe_key=?",
                ("operational:mail_worker",),
            ).fetchone()[0],
            "recovered",
        )

    def test_paper_signal_opens_immediately_and_can_be_acknowledged(self) -> None:
        order_key = self._persisted_order_key()
        self._insert_pending_order(self.store.connection)

        reconcile_alerts(
            self.store.connection,
            now=NOW,
            grace_seconds=30,
            email_recipient="ops@example.com",
        )
        alert = active_alerts(self.store.connection)[0]

        self.assertEqual(alert["category"], "paper_signal")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT order_key FROM notification_outbox"
            ).fetchone()[0],
            order_key,
        )
        self.assertTrue(
            acknowledge_alert(
                self.store.connection,
                incident_id=alert["incident_id"],
                actor="local-operator",
                acknowledged_at=NOW + timedelta(seconds=1),
            )
        )
        acknowledged = active_alerts(self.store.connection)[0]
        self.assertIsNotNone(acknowledged["acknowledged_at"])

    def test_paper_signal_fails_closed_when_lineage_relation_is_missing(self) -> None:
        relations = (
            "shadow_orders",
            "shadow_map_attempts",
            "vision_derived_invalidations",
            "vision_draft_anchors",
            "vision_draft_conflicts",
            "strict_live_mapping_impacts",
            "strict_live_map_mapping_invalidations",
            "strict_live_map_mappings",
            "strict_live_automatic_evidence_approvals",
            "shadow_order_decision_lineage",
            "strategy_decisions",
        )
        for relation in relations:
            with self.subTest(relation=relation):
                with tempfile.TemporaryDirectory() as directory:
                    store = LiveBettingStore(Path(directory) / "alerts.db")
                    try:
                        store.init_schema()
                        self._insert_pending_order(store.connection)
                        store.connection.execute("PRAGMA foreign_keys=OFF")
                        store.connection.execute(f'DROP TABLE "{relation}"')
                        store.connection.commit()
                        store.connection.execute("PRAGMA foreign_keys=ON")

                        reconcile_alerts(
                            store.connection,
                            now=NOW,
                            grace_seconds=0,
                            email_recipient="ops@example.com",
                        )

                        self._assert_no_paper_signal(store.connection)
                        row = store.connection.execute(
                            """SELECT source_json FROM monitor_alert_incidents
                                WHERE dedupe_key='operational:paper_signal_contract'
                                  AND status='active'"""
                        ).fetchone()
                        self.assertIsNotNone(row)
                        assert row is not None
                        source = json.loads(str(row[0]))
                        self.assertIn(
                            f"missing_relation:{relation}", source["issues"]
                        )
                    finally:
                        store.close()

    def test_schema_outage_keeps_existing_paper_incident_unknown(self) -> None:
        order_key = self._persisted_order_key()
        self._insert_pending_order(self.store.connection)
        reconcile_alerts(
            self.store.connection,
            now=NOW,
            grace_seconds=0,
            email_recipient="ops@example.com",
        )
        self.store.connection.execute("PRAGMA foreign_keys=OFF")
        self.store.connection.execute("DROP TABLE vision_draft_conflicts")
        self.store.connection.commit()
        self.store.connection.execute("PRAGMA foreign_keys=ON")

        reconcile_alerts(
            self.store.connection,
            now=NOW + timedelta(seconds=1),
            grace_seconds=0,
            email_recipient="ops@example.com",
        )

        paper = self.store.connection.execute(
            """SELECT status, recovered_at FROM monitor_alert_incidents
                WHERE dedupe_key=?""",
            (f"paper_signal:{order_key}",),
        ).fetchone()
        self.assertEqual(tuple(paper), ("active", None))
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE event_type='monitor_recovery'
                      AND json_extract(payload_json, '$.category')='paper_signal'"""
            ).fetchone()[0],
            0,
        )
        self.assertIsNotNone(
            self.store.connection.execute(
                """SELECT 1 FROM monitor_alert_incidents
                    WHERE dedupe_key='operational:paper_signal_contract'
                      AND status='active'"""
                ).fetchone()
        )
        record = claim(
            self.store.connection,
            now=NOW + timedelta(seconds=1),
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.payload["category"], "operational")

    def test_paper_signal_fails_closed_when_lineage_column_is_missing(self) -> None:
        missing_columns = {
            "shadow_orders": "strict_mapping_id",
            "shadow_map_attempts": "map_number",
            "vision_derived_invalidations": "dependent_key",
            "vision_draft_anchors": "conflict_at",
            "vision_draft_conflicts": "captured_at",
            "strict_live_mapping_impacts": "dependent_key",
            "strict_live_map_mapping_invalidations": "invalidation_id",
            "strict_live_map_mappings": "acceptance_mode",
            "strict_live_automatic_evidence_approvals": "source_mapping_id",
            "shadow_order_decision_lineage": "decision_key",
            "strategy_decisions": "contributions_json",
        }
        for relation, missing_column in missing_columns.items():
            with self.subTest(relation=relation, column=missing_column):
                with tempfile.TemporaryDirectory() as directory:
                    store = LiveBettingStore(Path(directory) / "alerts.db")
                    try:
                        store.init_schema()
                        self._insert_pending_order(store.connection)
                        columns = [
                            str(row[1])
                            for row in store.connection.execute(
                                f'PRAGMA table_info("{relation}")'
                            ).fetchall()
                            if str(row[1]) != missing_column
                        ]
                        definitions = ", ".join(
                            f'"{column}" TEXT' for column in columns
                        )
                        store.connection.execute("PRAGMA foreign_keys=OFF")
                        store.connection.execute(f'DROP TABLE "{relation}"')
                        store.connection.execute(
                            f'CREATE TABLE "{relation}" ({definitions})'
                        )
                        store.connection.commit()
                        store.connection.execute("PRAGMA foreign_keys=ON")

                        reconcile_alerts(
                            store.connection,
                            now=NOW,
                            grace_seconds=0,
                            email_recipient="ops@example.com",
                        )

                        self._assert_no_paper_signal(store.connection)
                        source = json.loads(
                            str(
                                store.connection.execute(
                                    """SELECT source_json
                                         FROM monitor_alert_incidents
                                        WHERE dedupe_key=
                                              'operational:paper_signal_contract'"""
                                ).fetchone()[0]
                            )
                        )
                        self.assertIn(
                            f"missing_column:{relation}.{missing_column}",
                            source["issues"],
                        )
                    finally:
                        store.close()

    def test_paper_signal_fails_closed_when_query_fails(self) -> None:
        self._insert_pending_order(self.store.connection)

        class FaultingConnection:
            def __init__(self, wrapped: sqlite3.Connection) -> None:
                self.wrapped = wrapped

            def execute(
                self, sql: str, parameters: tuple[object, ...] = ()
            ) -> sqlite3.Cursor:
                if (
                    "FROM shadow_orders AS orders" in sql
                    and "strict_live_mapping_impacts" in sql
                ):
                    raise sqlite3.OperationalError("injected paper query failure")
                return self.wrapped.execute(sql, parameters)

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return self.wrapped.__exit__(exc_type, exc_value, traceback)

            def __getattr__(self, name: str):
                return getattr(self.wrapped, name)

        reconcile_alerts(
            FaultingConnection(self.store.connection),  # type: ignore[arg-type]
            now=NOW,
            grace_seconds=0,
            email_recipient="ops@example.com",
        )

        self._assert_no_paper_signal(self.store.connection)
        source = json.loads(
            str(
                self.store.connection.execute(
                    """SELECT source_json FROM monitor_alert_incidents
                        WHERE dedupe_key='operational:paper_signal_contract'"""
                ).fetchone()[0]
            )
        )
        self.assertEqual(source["reason"], "query_failed")

    def test_paper_signal_fails_closed_when_numeric_payload_is_invalid(self) -> None:
        self._insert_pending_order(
            self.store.connection,
            model_probability="not-a-number",
        )

        reconcile_alerts(
            self.store.connection,
            now=NOW,
            grace_seconds=0,
            email_recipient="ops@example.com",
        )

        self._assert_no_paper_signal(self.store.connection)
        source = json.loads(
            str(
                self.store.connection.execute(
                    """SELECT source_json FROM monitor_alert_incidents
                        WHERE dedupe_key='operational:paper_signal_contract'"""
                ).fetchone()[0]
            )
        )
        self.assertEqual(source["reason"], "invalid_payload")

    def test_paper_signal_excludes_invalidated_and_conflicted_orders(self) -> None:
        self._insert_pending_order(
            self.store.connection,
            order_key="order-invalidated",
            raybet_match_id="match-invalidated",
        )
        self._insert_pending_order(
            self.store.connection,
            order_key="order-conflicted",
            raybet_match_id="match-conflicted",
            insert_vision_anchor=False,
        )
        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES ('shadow_order', ?, ?, 1, 'draft_conflict',
                       'vision_draft_conflict', ?)""",
            (
                self._persisted_order_key(
                    "order-invalidated", "match-invalidated"
                ),
                "match-invalidated",
                NOW.isoformat(),
            ),
        )
        self.store.connection.execute(
            """INSERT INTO vision_draft_anchors
               (raybet_match_id, map_number, draft_hash, radiant_hero_ids,
                dire_hero_ids, anchored_at, source_frame_ref, status, conflict_at)
               VALUES (?, 1, ?, '[]', '[]', ?, 'frame-1', 'anchored', NULL)""",
            (
                "match-conflicted",
                "a" * 64,
                (NOW - timedelta(seconds=10)).isoformat(),
            ),
        )
        self.store.connection.execute(
            """UPDATE vision_draft_anchors
                  SET status='conflict', conflict_at=?
                WHERE raybet_match_id='match-conflicted' AND map_number=1""",
            ((NOW + timedelta(seconds=10)).isoformat(),),
        )
        self.store.connection.execute(
            """INSERT INTO vision_draft_conflicts
               (raybet_match_id, map_number, captured_at, source_frame_ref,
                observed_draft_hash, radiant_hero_ids, dire_hero_ids, reason,
                recorded_at)
               VALUES (?, 1, ?, 'frame-2', ?, '[]', '[]', 'mismatch', ?)""",
            (
                "match-conflicted",
                (NOW - timedelta(seconds=1)).isoformat(),
                "b" * 64,
                NOW.isoformat(),
            ),
        )
        self.store.connection.commit()

        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=0)

        self.assertEqual(active_alerts(self.store.connection), [])

    def test_paper_signal_requires_verified_strict_mapping(self) -> None:
        self._insert_pending_order(
            self.store.connection,
            order_key="order-orphan-mapping",
            raybet_match_id="match-orphan-mapping",
            strict_mapping_id=999,
        )

        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=0)

        self.assertEqual(active_alerts(self.store.connection), [])

    def test_paper_signal_requires_exact_decision_lineage(self) -> None:
        order_key = self._persisted_order_key()
        self._insert_pending_order(
            self.store.connection,
            insert_decision_lineage=False,
        )

        reconcile_alerts(
            self.store.connection,
            now=NOW,
            grace_seconds=0,
            email_recipient="ops@example.com",
        )

        self._assert_no_paper_signal(self.store.connection)
        row = self.store.connection.execute(
            """SELECT source_json FROM monitor_alert_incidents
                WHERE dedupe_key='operational:paper_signal_contract'"""
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        source = json.loads(str(row[0]))
        self.assertEqual(source["reason"], "decision_lineage_invalid")
        self.assertEqual(
            source["issues"],
            [f"order_key={order_key}: decision_lineage_unavailable"],
        )

    def test_paper_signal_requires_a_vision_draft_anchor(self) -> None:
        self._insert_pending_order(
            self.store.connection,
            insert_vision_anchor=False,
        )

        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=0)

        self.assertEqual(active_alerts(self.store.connection), [])

    def test_paper_signal_rejects_future_lineage_evidence(self) -> None:
        future_mapping_id = self._insert_strict_mapping(
            self.store.connection,
            raybet_match_id="match-future-mapping",
            accepted_at=NOW + timedelta(seconds=1),
        )
        self._insert_pending_order(
            self.store.connection,
            order_key="order-future-mapping",
            raybet_match_id="match-future-mapping",
            strict_mapping_id=future_mapping_id,
        )
        self._insert_pending_order(
            self.store.connection,
            order_key="order-future-anchor",
            raybet_match_id="match-future-anchor",
            vision_anchor_at=NOW + timedelta(seconds=1),
        )

        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=0)

        self.assertEqual(active_alerts(self.store.connection), [])

    def test_paper_signal_excludes_all_strict_mapping_invalidation_paths(self) -> None:
        cases = ("direct", "impact", "automatic_source")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as directory:
                    store = LiveBettingStore(Path(directory) / "alerts.db")
                    try:
                        store.init_schema()
                        source_mapping_id = None
                        strict_mapping_id = None
                        if case == "automatic_source":
                            source_mapping_id = self._insert_strict_mapping(
                                store.connection,
                                raybet_match_id="match-source",
                            )
                            approval_id = self._insert_automatic_approval(
                                store.connection,
                                source_mapping_id=source_mapping_id,
                            )
                            strict_mapping_id = self._insert_strict_mapping(
                                store.connection,
                                raybet_match_id="match-automatic_source",
                                acceptance_mode="automatic_exact",
                                automatic_approval_id=approval_id,
                            )
                        order_mapping_id = self._insert_pending_order(
                            store.connection,
                            order_key=f"order-{case}",
                            raybet_match_id=f"match-{case}",
                            strict_mapping_id=strict_mapping_id,
                        )
                        assert order_mapping_id is not None
                        if case == "direct":
                            store.connection.execute(
                                """INSERT INTO strict_live_map_mapping_invalidations
                                   (mapping_id, reason, invalidated_by,
                                    invalidated_at, recorded_at)
                                   VALUES (?, 'test', 'operator', ?, ?)""",
                                (
                                    order_mapping_id,
                                    NOW.isoformat(),
                                    NOW.isoformat(),
                                ),
                            )
                        elif case == "impact":
                            cause_mapping_id = self._insert_strict_mapping(
                                store.connection,
                                raybet_match_id="match-impact-cause",
                            )
                            cursor = store.connection.execute(
                                """INSERT INTO strict_live_map_mapping_invalidations
                                   (mapping_id, reason, invalidated_by,
                                    invalidated_at, recorded_at)
                                   VALUES (?, 'test', 'operator', ?, ?)""",
                                (
                                    cause_mapping_id,
                                    NOW.isoformat(),
                                    NOW.isoformat(),
                                ),
                            )
                            store.connection.execute(
                                """INSERT INTO strict_live_mapping_impacts
                                   (mapping_id, invalidation_id, dependent_type,
                                    dependent_key, reason, recorded_at)
                                   VALUES (?, ?, 'shadow_order', ?, 'test', ?)""",
                                (
                                    order_mapping_id,
                                    int(cursor.lastrowid),
                                    self._persisted_order_key(
                                        "order-impact", "match-impact"
                                    ),
                                    NOW.isoformat(),
                                ),
                            )
                        else:
                            assert source_mapping_id is not None
                            store.connection.execute(
                                """INSERT INTO strict_live_map_mapping_invalidations
                                   (mapping_id, reason, invalidated_by,
                                    invalidated_at, recorded_at)
                                   VALUES (?, 'test', 'operator', ?, ?)""",
                                (
                                    source_mapping_id,
                                    NOW.isoformat(),
                                    NOW.isoformat(),
                                ),
                            )
                        store.connection.commit()

                        reconcile_alerts(
                            store.connection,
                            now=NOW,
                            grace_seconds=0,
                        )

                        self.assertEqual(active_alerts(store.connection), [])
                    finally:
                        store.close()

    def test_paper_signal_gate_and_enqueue_share_one_write_transaction(self) -> None:
        paper_order_key = self._persisted_order_key()
        self._insert_pending_order(self.store.connection)
        contender = sqlite3.connect(self.database, timeout=0)
        writer_errors: list[str] = []

        class LockCheckingConnection:
            def __init__(self, wrapped: sqlite3.Connection) -> None:
                self.wrapped = wrapped
                self.checked = False

            def execute(
                self, sql: str, parameters: tuple[object, ...] = ()
            ) -> sqlite3.Cursor:
                if not self.checked and "FROM shadow_orders AS orders" in sql:
                    self.checked = True
                    try:
                        contender.execute(
                            """INSERT INTO vision_derived_invalidations
                               (dependent_type, dependent_key, raybet_match_id,
                                map_number, reason, recorded_at)
                               VALUES ('shadow_order', ?, 'match-1',
                                       1, 'racing_invalidation', ?)""",
                            (paper_order_key, NOW.isoformat()),
                        )
                        contender.commit()
                    except sqlite3.OperationalError as exc:
                        writer_errors.append(str(exc))
                        contender.rollback()
                return self.wrapped.execute(sql, parameters)

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return self.wrapped.__exit__(exc_type, exc_value, traceback)

            def __getattr__(self, name: str):
                return getattr(self.wrapped, name)

        try:
            reconcile_alerts(
                LockCheckingConnection(  # type: ignore[arg-type]
                    self.store.connection
                ),
                now=NOW,
                grace_seconds=0,
                email_recipient="ops@example.com",
            )
            self.assertTrue(any("locked" in error for error in writer_errors))
            contender.execute(
                """INSERT INTO vision_derived_invalidations
                   (dependent_type, dependent_key, raybet_match_id, map_number,
                    reason, recorded_at)
                   VALUES ('shadow_order', ?, 'match-1', 1,
                           'post_commit_invalidation', ?)""",
                (paper_order_key, (NOW + timedelta(seconds=1)).isoformat()),
            )
            contender.commit()
        finally:
            contender.close()

        self.assertIsNone(
            claim(self.store.connection, now=NOW + timedelta(seconds=2))
        )
        self.assertEqual(
            tuple(
                self.store.connection.execute(
                    """SELECT order_key, status, last_error
                         FROM notification_outbox"""
                ).fetchone()
            ),
            (paper_order_key, "dead_letter", "strict_mapping_unverified"),
        )

    def test_email_outbox_uses_monitor_template_when_recipient_is_configured(self) -> None:
        record_health(
            self.store.connection,
            "vision_worker",
            "unhealthy",
            heartbeat_at=NOW,
            error_at=NOW,
            error="capture_failed",
        )
        reconcile_alerts(
            self.store.connection,
            now=NOW,
            grace_seconds=0,
            email_recipient="ops@example.com",
        )

        record = claim(self.store.connection, now=NOW)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.event_type, "monitor_alert")
        message = build_message(record, SMTPConfig("sender@example.com", "auth-code"))
        self.assertIn("监控告警", str(message["Subject"]))
        self.assertIn("capture_failed", message.get_content())

    def test_unacknowledged_alerts_sort_before_acknowledged_alerts(self) -> None:
        self.store.connection.executemany(
            """INSERT INTO monitor_alert_incidents
               (dedupe_key, episode, category, severity, title, body, status,
                first_detected_at, opened_at, last_detected_at,
                acknowledged_at, acknowledged_by, source_json, occurrence_count)
               VALUES (?, 1, 'operational', ?, ?, 'body', 'active',
                       ?, ?, ?, ?, ?, '{}', 1)""",
            (
                (
                    "acknowledged-critical", "critical", "acknowledged",
                    NOW.isoformat(), (NOW + timedelta(minutes=2)).isoformat(),
                    (NOW + timedelta(minutes=2)).isoformat(),
                    (NOW + timedelta(minutes=3)).isoformat(), "operator",
                ),
                (
                    "unacknowledged-warning", "warning", "unacknowledged",
                    NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), None, None,
                ),
            ),
        )
        self.store.connection.commit()

        self.assertEqual(
            [alert["dedupe_key"] for alert in active_alerts(self.store.connection)],
            ["unacknowledged-warning", "acknowledged-critical"],
        )

    def test_current_outbox_schema_repairs_missing_artifacts(self) -> None:
        self.store.connection.execute(
            "DROP TRIGGER notification_outbox_payload_immutable"
        )
        self.store.connection.execute("DROP INDEX idx_notification_outbox_due")
        self.store.connection.commit()

        init_alert_schema(self.store.connection)

        objects = {
            (str(row[0]), str(row[1]))
            for row in self.store.connection.execute(
                """SELECT type, name FROM sqlite_master
                    WHERE name IN ('idx_notification_outbox_due',
                                   'notification_outbox_payload_immutable')"""
            )
        }
        self.assertEqual(
            objects,
            {
                ("index", "idx_notification_outbox_due"),
                ("trigger", "notification_outbox_payload_immutable"),
            },
        )

    def test_legacy_outbox_migration_is_atomic_and_retryable(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                """CREATE TABLE notification_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_key TEXT NOT NULL,
                    event_type TEXT NOT NULL
                        CHECK (event_type IN ('filled', 'settled')),
                    channel TEXT NOT NULL DEFAULT 'email',
                    status TEXT NOT NULL DEFAULT 'pending',
                    recipient TEXT NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    statistics_cutoff TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    lease_token TEXT,
                    lease_until TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    sent_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (order_key, event_type, channel)
                )"""
            )
            connection.execute(
                """INSERT INTO notification_outbox
                   (outbox_id, order_key, event_type, channel, status, recipient,
                    message_id, payload_json, statistics_cutoff,
                    template_version, attempt_count, created_at, updated_at)
                   VALUES (7, 'order-7', 'filled', 'email', 'pending',
                           'ops@example.com', '<message-7@example.com>', '{}',
                           ?, 'legacy-v1', 0, ?, ?)""",
                (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )
            connection.commit()

            class FaultingConnection:
                def __init__(self, wrapped: sqlite3.Connection) -> None:
                    self.wrapped = wrapped

                def execute(
                    self, sql: str, parameters: tuple[object, ...] = ()
                ) -> sqlite3.Cursor:
                    if sql.lstrip().startswith(
                        "CREATE INDEX idx_notification_outbox_due"
                    ):
                        raise sqlite3.OperationalError("injected index failure")
                    return self.wrapped.execute(sql, parameters)

                def __getattr__(self, name: str):
                    return getattr(self.wrapped, name)

            with self.assertRaises(sqlite3.OperationalError):
                init_alert_schema(FaultingConnection(connection))  # type: ignore[arg-type]

            legacy_sql = str(connection.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='notification_outbox'"""
            ).fetchone()[0])
            self.assertNotIn("monitor_alert", legacy_sql)
            self.assertEqual(
                connection.execute(
                    "SELECT outbox_id FROM notification_outbox"
                ).fetchone()[0],
                7,
            )
            self.assertIsNone(connection.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='notification_outbox_monitor_v1'"""
            ).fetchone())

            init_alert_schema(connection)

            upgraded_sql = str(connection.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='notification_outbox'"""
            ).fetchone()[0])
            self.assertIn("monitor_alert", upgraded_sql)
            self.assertEqual(
                connection.execute(
                    "SELECT outbox_id FROM notification_outbox"
                ).fetchone()[0],
                7,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
