from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from event_intelligence.storage import IntelligenceStorage
from live_betting.engine import price_groups
from live_betting.health import record_health
from live_betting.markets import normalized_state_hash
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.notifications import claim
from live_betting.smtp_delivery import SMTPConfig, build_message
from live_betting.runtime_schema import prepare_runtime_schema
from live_betting.storage import LiveBettingStore
from live_betting.strict_eligibility import (
    accept_strict_live_map_mapping,
    approve_automatic_exact_evidence,
    invalidate_strict_live_map_mapping,
)
from tests.draft_authority_fixture import (
    make_test_vision_observation,
    seed_test_draft_authority,
)
from web.alerts import (
    acknowledge_alert,
    active_alerts,
    reconcile_alerts,
)


NOW = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)


class MonitorAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "alerts.db"
        self.store = LiveBettingStore(self.database)
        self.store.init_schema()
        prepare_runtime_schema(self.store.connection)

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
        accepted_at: datetime = NOW - timedelta(seconds=2),
    ) -> int:
        connection.commit()
        database_path = Path(
            str(connection.execute("PRAGMA database_list").fetchone()[2])
        )
        IntelligenceStorage(database_path, connection=connection).init_schema()
        connection.execute(
            """CREATE TABLE IF NOT EXISTS teams (
                   team_id INTEGER PRIMARY KEY,
                   name TEXT,
                   tag TEXT,
                   logo_url TEXT,
                   updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        connection.executemany(
            "INSERT OR IGNORE INTO teams(team_id, name) VALUES (?, ?)",
            ((101, "Alpha"), (202, "Beta")),
        )
        store = LiveBettingStore(database_path, connection=connection)
        if connection.execute(
            "SELECT 1 FROM raybet_matches WHERE raybet_match_id=?",
            (raybet_match_id,),
        ).fetchone() is None:
            store.upsert_raybet_match(
                {
                    "id": raybet_match_id,
                    "game_id": 151,
                    "tournament_name": "PGL Wallachia Season 8",
                    "start_time": "2026-04-20 12:00:00",
                    "round": "bo3",
                    "stage": "main_event",
                    "status": 1,
                    "team": [
                        {"pos": 1, "team_id": 101, "team_name": "Alpha"},
                        {"pos": 2, "team_id": 202, "team_name": "Beta"},
                    ],
                },
                accepted_at - timedelta(seconds=1),
            )
        evidence = {
            "kind": "manual_cross_source_review",
            "raybet_url": f"https://example.invalid/raybet/{raybet_match_id}",
            "official_event_url": "https://www.pglesports.com/",
            "tournament": {
                "raybet_name": "PGL Wallachia Season 8",
                "event_name": "PGL Wallachia Season 8",
            },
            "schedule": {
                "raybet_scheduled_at": "2026-04-20 12:00:00",
                "utc_offset_minutes": 480,
                "scheduled_at_utc": "2026-04-20T04:00:00+00:00",
                "timezone_evidence": "audited RayBet UTC+08 display contract",
            },
            "stage": {
                "scope": "main_event",
                "source_url": "https://www.pglesports.com/",
            },
            "team_crosswalk": {
                "team_one": {
                    "raybet_team_id": 101,
                    "raybet_team_name": "Alpha",
                    "canonical_team_id": 101,
                    "canonical_team_name": "Alpha",
                    "source_url": "https://example.invalid/teams/alpha",
                },
                "team_two": {
                    "raybet_team_id": 202,
                    "raybet_team_name": "Beta",
                    "canonical_team_id": 202,
                    "canonical_team_name": "Beta",
                    "source_url": "https://example.invalid/teams/beta",
                },
            },
        }
        with patch(
            "live_betting.strict_eligibility._utc_now",
            return_value=accepted_at,
        ):
            mapping = accept_strict_live_map_mapping(
                connection,
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                event_id="pgl-wallachia-s8-2026",
                team_one_id=101,
                team_two_id=202,
                canonical_team_one_id=101,
                canonical_team_two_id=202,
                source="monitor_alert_fixture",
                evidence=evidence,
                accepted_by="test",
                accepted_at=accepted_at,
                acceptance_mode=acceptance_mode,
            )
        if (
            automatic_approval_id is not None
            and mapping.automatic_approval_id != automatic_approval_id
        ):
            raise AssertionError("automatic mapping approval lineage differs")
        return mapping.mapping_id

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
                f"winner|map_{map_number}|team_one|",
                strategy_version,
                input_ref,
                "1.0",
            )
        )
        persisted_order_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        if strict_mapping_id is None and create_strict_mapping:
            strict_mapping_id = self._insert_strict_mapping(
                connection,
                raybet_match_id=raybet_match_id,
                map_number=map_number,
            )
        if strict_mapping_id is None:
            raise AssertionError("pending order fixture requires a strict mapping")
        connection.commit()
        database_path = Path(
            str(connection.execute("PRAGMA database_list").fetchone()[2])
        )
        store = LiveBettingStore(database_path, connection=connection)
        anchor_vision = make_test_vision_observation(
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            captured_at=signal_at - timedelta(seconds=1),
            game_clock_seconds=599,
            label=f"monitor-anchor:{order_key}",
        )
        signal_vision = make_test_vision_observation(
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            captured_at=signal_at,
            game_clock_seconds=600,
            label=f"monitor-signal:{order_key}",
        )
        if not store.insert_vision_observation(anchor_vision):
            raise AssertionError("monitor anchor frame was not inserted")
        if not store.insert_vision_observation(signal_vision):
            raise AssertionError("monitor signal frame was not inserted")
        authority = seed_test_draft_authority(
            connection,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            strict_mapping_id=strict_mapping_id,
            observed_at=signal_at,
            label=f"monitor-alert:{order_key}",
        )
        valid_signal_price = (
            float(signal_price)
            if isinstance(signal_price, (int, float))
            and not isinstance(signal_price, bool)
            and math.isfinite(float(signal_price))
            and float(signal_price) > 1.0
            else 2.5
        )
        requested_market_probability = (
            float(market_probability)
            if isinstance(market_probability, (int, float))
            and not isinstance(market_probability, bool)
            and math.isfinite(float(market_probability))
            and 0.0 < float(market_probability) < 1.0
            else 0.4
        )
        opposite_price = (
            requested_market_probability
            * valid_signal_price
            / (1.0 - requested_market_probability)
        )
        signal_market = Market(
            "winner", f"map_{map_number}", "team_one", None, "team_one", True
        )
        opposite_market = Market(
            "winner", f"map_{map_number}", "team_two", None, "team_two", True
        )
        signal = OddsSnapshot(
            raybet_match_id,
            f"odds-{order_key}",
            f"group-{order_key}",
            signal_at,
            valid_signal_price,
            1,
            signal_market,
        )
        opposite = OddsSnapshot(
            raybet_match_id,
            f"odds-{order_key}-opposite",
            f"group-{order_key}",
            signal_at,
            opposite_price,
            1,
            opposite_market,
        )
        snapshots = [signal, opposite]
        transport_key = f"transport-{order_key}"
        store.store_odds_observation(
            source="direct",
            observation_key=transport_key,
            source_event_id=None,
            raybet_match_id=raybet_match_id,
            observed_at=signal_at,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
            raw_payload={
                "result": {
                    "id": raybet_match_id,
                    "game_id": 151,
                    "team": [
                        {"team_id": 101, "team_name": "Alpha", "pos": 1},
                        {"team_id": 202, "team_name": "Beta", "pos": 2},
                    ],
                    "odds": [
                        {
                            "id": row.odds_id,
                            "odds_group_id": row.odds_group_id,
                            "team_id": 101 if row.market.side == "team_one" else 202,
                            "match_stage": f"r{map_number}",
                            "group_short_name": "Winner",
                            "tag": "win",
                            "odds": str(row.price),
                            "status": row.status,
                        }
                        for row in snapshots
                    ],
                }
            },
        )
        persisted_market_probability = price_groups(snapshots)[signal.odds_id]
        persisted_model_probability = (
            float(model_probability)
            if isinstance(model_probability, (int, float))
            and not isinstance(model_probability, bool)
            and math.isfinite(float(model_probability))
            and 0.0 <= float(model_probability) <= 1.0
            else 0.58
        )
        decision_key = f"paper-decision:{order_key}"
        decision = SimpleNamespace(
            decision_key=decision_key,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            decided_at=signal_at,
            underdog_side="team_one",
            market_probability=persisted_market_probability,
            model_probability=persisted_model_probability,
            edge=persisted_model_probability - persisted_market_probability,
            data_quality=0.8,
            eligible=True,
            reason="eligible",
            contributions={
                "__inputs__": {
                    "draft_authority": asdict(authority),
                    "strict_live_eligibility": {
                        "mapping_refs": {"strict_mapping_id": strict_mapping_id}
                    },
                    "vision": {
                        "captured_at": signal_at.isoformat(),
                        "source_frame_ref": signal_vision.source_frame_ref,
                        "game_clock_seconds": 600,
                    },
                }
            },
            input_ref=input_ref,
            strategy_version=strategy_version,
        )
        if not store.insert_decision(
            decision,
            draft_authority=authority,
            vision_observation=signal_vision,
            vision_transport_key=transport_key,
        ):
            raise AssertionError("monitor strategy decision was not inserted")
        order = ShadowOrder(
            order_key=persisted_order_key,
            raybet_match_id=raybet_match_id,
            odds_id=signal.odds_id,
            market=signal_market,
            signaled_at=signal_at,
            model_probability=persisted_model_probability,
            market_probability=persisted_market_probability,
            signal_price=valid_signal_price,
            signal_transport_key=transport_key,
            signal_transport_at=signal_at,
            expires_at=signal_at + timedelta(seconds=15),
            signal_odds_group_id=signal.odds_group_id,
            signal_outcome_key=signal_market.outcome_key,
            signal_identity_verified=True,
        )
        if not store.insert_map_order(
            order,
            map_number,
            strict_mapping_id=strict_mapping_id,
            draft_authority=authority,
            decision_key=decision_key,
        ):
            raise AssertionError("monitor pending order was not inserted")
        if not insert_decision_lineage:
            connection.execute(
                "DROP TRIGGER shadow_order_decision_lineage_immutable_delete"
            )
            connection.execute(
                "DELETE FROM shadow_order_decision_lineage WHERE order_key=?",
                (persisted_order_key,),
            )
        if not insert_vision_anchor:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """DELETE FROM vision_draft_anchors
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")
        elif vision_anchor_at is not None:
            connection.execute("DROP TRIGGER vision_draft_anchor_identity_immutable")
            connection.execute(
                """UPDATE vision_draft_anchors SET anchored_at=?
                    WHERE raybet_match_id=? AND map_number=?""",
                (vision_anchor_at.isoformat(), raybet_match_id, map_number),
            )
        if not isinstance(model_probability, (int, float)) or isinstance(
            model_probability, bool
        ):
            connection.execute("DROP TRIGGER strategy_decisions_immutable_update")
            connection.execute("DROP TRIGGER shadow_orders_terminal_immutable")
            connection.execute(
                "UPDATE strategy_decisions SET model_probability=? WHERE decision_key=?",
                (model_probability, decision_key),
            )
            connection.execute(
                "UPDATE shadow_orders SET model_probability=? WHERE order_key=?",
                (model_probability, persisted_order_key),
            )
        connection.commit()
        return strict_mapping_id

    @staticmethod
    def _persisted_order_key(
        order_key: str = "order-alert",
        raybet_match_id: str = "match-1",
        map_number: int = 1,
    ) -> str:
        identity = "|".join(
            (
                raybet_match_id,
                f"odds-{order_key}",
                f"group-{order_key}",
                "team_one",
                f"winner|map_{map_number}|team_one|",
                "paper-test-v1",
                f"paper-input:{order_key}",
                "1.0",
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    def _insert_automatic_approval(
        self,
        connection: sqlite3.Connection,
        *,
        source_mapping_id: int,
    ) -> int:
        approved_at = NOW - timedelta(seconds=1)
        with patch(
            "live_betting.strict_eligibility._utc_now",
            return_value=approved_at,
        ):
            approval_id = approve_automatic_exact_evidence(
                connection,
                source_mapping_id=source_mapping_id,
                approved_by="test",
                approved_at=approved_at,
            )
        connection.commit()
        return approval_id

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

    @staticmethod
    def _remove_strict_mapping(
        connection: sqlite3.Connection,
        mapping_id: int,
    ) -> None:
        """Simulate a corrupt legacy orphan after first proving a valid graph."""
        connection.commit()
        connection.execute("DROP TRIGGER strict_live_map_mappings_no_delete")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DELETE FROM strict_live_map_mappings WHERE mapping_id=?",
            (mapping_id,),
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")

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

    def test_alert_reconciliation_never_attempts_schema_ddl(self) -> None:
        schema_actions = {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
        }
        attempted: list[int] = []

        def authorizer(
            action: int,
            _arg1: str | None,
            _arg2: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action in schema_actions:
                attempted.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.connection.set_authorizer(authorizer)
        try:
            reconcile_alerts(
                self.store.connection,
                now=NOW,
                grace_seconds=0,
                health=[],
            )
        finally:
            self.store.connection.set_authorizer(None)

        self.assertEqual(attempted, [])

    def test_health_query_failure_never_recovers_existing_operational_alert(
        self,
    ) -> None:
        record_health(
            self.store.connection,
            "raybet_worker",
            "degraded",
            heartbeat_at=NOW,
            error_at=NOW,
            error="timeout",
        )
        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=0)
        record_health(
            self.store.connection,
            "raybet_worker",
            "healthy",
            heartbeat_at=NOW + timedelta(seconds=1),
            success_at=NOW + timedelta(seconds=1),
        )

        class FaultingConnection:
            def __init__(self, wrapped: sqlite3.Connection) -> None:
                self.wrapped = wrapped

            def execute(
                self, sql: str, parameters: tuple[object, ...] = ()
            ) -> sqlite3.Cursor:
                if "FROM service_health" in sql:
                    raise sqlite3.OperationalError(
                        "injected service health query failure"
                    )
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
            now=NOW + timedelta(seconds=1),
            grace_seconds=0,
        )

        statuses = {
            str(row[0]): str(row[1])
            for row in self.store.connection.execute(
                "SELECT dedupe_key, status FROM monitor_alert_incidents"
            )
        }
        self.assertEqual(statuses["operational:raybet_worker"], "active")
        self.assertEqual(
            statuses["operational:service_health_contract"],
            "active",
        )

        reconcile_alerts(
            self.store.connection,
            now=NOW + timedelta(seconds=2),
            grace_seconds=0,
        )
        statuses = {
            str(row[0]): str(row[1])
            for row in self.store.connection.execute(
                "SELECT dedupe_key, status FROM monitor_alert_incidents"
            )
        }
        self.assertEqual(statuses["operational:raybet_worker"], "recovered")
        self.assertEqual(
            statuses["operational:service_health_contract"],
            "recovered",
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

    def test_acknowledgement_rejects_runtime_contract_drift(self) -> None:
        self._insert_pending_order(self.store.connection)
        reconcile_alerts(
            self.store.connection,
            now=NOW,
            grace_seconds=0,
        )
        alert = active_alerts(self.store.connection)[0]
        self.store.connection.execute(
            "DROP TRIGGER monitor_alert_audit_no_update"
        )
        self.store.connection.commit()

        with self.assertRaisesRegex(RuntimeError, "missing objects"):
            acknowledge_alert(
                self.store.connection,
                incident_id=alert["incident_id"],
                actor="local-operator",
                acknowledged_at=NOW + timedelta(seconds=1),
            )

        self.assertIsNone(
            self.store.connection.execute(
                """SELECT acknowledged_at FROM monitor_alert_incidents
                    WHERE incident_id=?""",
                (alert["incident_id"],),
            ).fetchone()[0]
        )

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
                        prepare_runtime_schema(store.connection)
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
                        prepare_runtime_schema(store.connection)
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
        self.store.connection.commit()
        conflicting_frame = make_test_vision_observation(
            raybet_match_id="match-conflicted",
            map_number=1,
            captured_at=NOW - timedelta(milliseconds=500),
            game_clock_seconds=600,
            radiant_hero_ids=(11, 12, 13, 14, 15),
            dire_hero_ids=(16, 17, 18, 19, 20),
            label="monitor-alert:conflicting-frame",
        )
        self.assertTrue(self.store.insert_vision_observation(conflicting_frame))

        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=0)

        self.assertEqual(active_alerts(self.store.connection), [])

    def test_paper_signal_requires_verified_strict_mapping(self) -> None:
        mapping_id = self._insert_pending_order(
            self.store.connection,
            order_key="order-orphan-mapping",
            raybet_match_id="match-orphan-mapping",
        )
        assert mapping_id is not None
        self._remove_strict_mapping(self.store.connection, mapping_id)

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
        )
        self._insert_pending_order(
            self.store.connection,
            order_key="order-future-mapping",
            raybet_match_id="match-future-mapping",
            strict_mapping_id=future_mapping_id,
        )
        self.store.connection.execute(
            "DROP TRIGGER strict_live_map_mappings_no_update"
        )
        self.store.connection.execute(
            """UPDATE strict_live_map_mappings SET accepted_at=?
                WHERE mapping_id=?""",
            ((NOW + timedelta(seconds=1)).isoformat(), future_mapping_id),
        )
        self.store.connection.commit()
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
                        prepare_runtime_schema(store.connection)
                        source_mapping_id = None
                        strict_mapping_id = None
                        if case == "automatic_source":
                            source_mapping_id = self._insert_strict_mapping(
                                store.connection,
                                raybet_match_id="match-automatic_source",
                            )
                            approval_id = self._insert_automatic_approval(
                                store.connection,
                                source_mapping_id=source_mapping_id,
                            )
                            strict_mapping_id = self._insert_strict_mapping(
                                store.connection,
                                raybet_match_id="match-automatic_source",
                                map_number=2,
                                acceptance_mode="automatic_exact",
                                automatic_approval_id=approval_id,
                                accepted_at=NOW,
                            )
                        order_mapping_id = self._insert_pending_order(
                            store.connection,
                            order_key=f"order-{case}",
                            raybet_match_id=f"match-{case}",
                            strict_mapping_id=strict_mapping_id,
                            map_number=2 if case == "automatic_source" else 1,
                        )
                        assert order_mapping_id is not None
                        if case == "direct":
                            with patch(
                                "live_betting.strict_eligibility._utc_now",
                                return_value=NOW,
                            ):
                                invalidate_strict_live_map_mapping(
                                    store.connection,
                                    mapping_id=order_mapping_id,
                                    reason="test",
                                    invalidated_by="operator",
                                    invalidated_at=NOW,
                                )
                        elif case == "impact":
                            cause_mapping_id = self._insert_strict_mapping(
                                store.connection,
                                raybet_match_id="match-impact-cause",
                            )
                            with patch(
                                "live_betting.strict_eligibility._utc_now",
                                return_value=NOW,
                            ):
                                invalidation_id = invalidate_strict_live_map_mapping(
                                    store.connection,
                                    mapping_id=cause_mapping_id,
                                    reason="test",
                                    invalidated_by="operator",
                                    invalidated_at=NOW,
                                )
                            store.connection.execute(
                                """INSERT INTO strict_live_mapping_impacts
                                   (mapping_id, invalidation_id, dependent_type,
                                    dependent_key, reason, recorded_at)
                                   VALUES (?, ?, 'shadow_order', ?, 'test', ?)""",
                                (
                                    order_mapping_id,
                                    invalidation_id,
                                    self._persisted_order_key(
                                        "order-impact", "match-impact"
                                    ),
                                    NOW.isoformat(),
                                ),
                            )
                        else:
                            assert source_mapping_id is not None
                            with patch(
                                "live_betting.strict_eligibility._utc_now",
                                return_value=NOW,
                            ):
                                invalidate_strict_live_map_mapping(
                                    store.connection,
                                    mapping_id=source_mapping_id,
                                    reason="test",
                                    invalidated_by="operator",
                                    invalidated_at=NOW,
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
        mapping_id = self._insert_pending_order(self.store.connection)
        assert mapping_id is not None
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

        self._remove_strict_mapping(self.store.connection, mapping_id)

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

    def test_current_runtime_schema_rejects_missing_outbox_artifacts(self) -> None:
        self.store.connection.execute(
            "DROP TRIGGER notification_outbox_payload_immutable"
        )
        self.store.connection.execute("DROP INDEX idx_notification_outbox_due")
        self.store.connection.commit()

        with self.assertRaisesRegex(RuntimeError, "missing objects"):
            prepare_runtime_schema(self.store.connection)

        objects = {
            (str(row[0]), str(row[1]))
            for row in self.store.connection.execute(
                """SELECT type, name FROM sqlite_master
                    WHERE name IN ('idx_notification_outbox_due',
                                   'notification_outbox_payload_immutable')"""
            )
        }
        self.assertEqual(objects, set())

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
                prepare_runtime_schema(FaultingConnection(connection))  # type: ignore[arg-type]

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
                    WHERE type='table'
                      AND name='notification_outbox__runtime_schema_v1'"""
            ).fetchone())

            prepare_runtime_schema(connection)

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
