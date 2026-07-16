from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from event_intelligence.storage import IntelligenceStorage
from live_betting.markets import normalized_state_hash
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.notifications import claim
from live_betting.report import build_report
from live_betting.storage import LiveBettingStore
from live_betting.strict_read_gate import strict_read_gate
from live_betting.strict_eligibility import (
    StrictMappingConflictError,
    StrictMappingError,
    accept_strict_live_map_mapping,
    approve_automatic_exact_evidence,
    init_strict_live_eligibility_schema,
    invalidate_strict_live_map_mapping,
    query_strict_live_eligibility,
    record_strict_live_mapping_candidate,
)
from web.alerts import init_alert_schema
from web.monitoring import build_monitor_snapshot, monitor_cursor


EVENT_ID = "pgl-wallachia-s8-2026"
ACCEPTED_AT = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
RECORDED_AT = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
METADATA_AT = datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc)
SCHEDULE_RAW = "2026-04-20 12:00:00"
SCHEDULE_UTC = datetime(2026, 4, 20, 4, 0, tzinfo=timezone.utc)


def evidence(
    *,
    raybet_tournament: str = "PGL Wallachia Season 8",
    event_name: str = "PGL Wallachia Season 8",
    scheduled_at: str = SCHEDULE_RAW,
    scheduled_at_utc: datetime = SCHEDULE_UTC,
    stage_scope: str = "main_event",
    canonical_team_one_id: int = 101,
    canonical_team_one_name: str = "Alpha Canonical",
    canonical_team_two_id: int = 202,
    canonical_team_two_name: str = "Beta Canonical",
) -> dict[str, object]:
    return {
        "kind": "manual_cross_source_review",
        "raybet_url": "https://example.invalid/raybet/match-1",
        "official_event_url": "https://example.invalid/event",
        "tournament": {
            "raybet_name": raybet_tournament,
            "event_name": event_name,
        },
        "schedule": {
            "raybet_scheduled_at": scheduled_at,
            "utc_offset_minutes": 480,
            "scheduled_at_utc": scheduled_at_utc.isoformat(),
            "timezone_evidence": "audited RayBet UTC+08 display contract",
        },
        "stage": {
            "scope": stage_scope,
            "source_url": "https://example.invalid/event/stage",
        },
        "team_crosswalk": {
            "team_one": {
                "raybet_team_id": 501,
                "raybet_team_name": "Alpha",
                "canonical_team_id": canonical_team_one_id,
                "canonical_team_name": canonical_team_one_name,
                "source_url": "https://example.invalid/teams/alpha",
            },
            "team_two": {
                "raybet_team_id": 502,
                "raybet_team_name": "Beta",
                "canonical_team_id": canonical_team_two_id,
                "canonical_team_name": canonical_team_two_name,
                "source_url": "https://example.invalid/teams/beta",
            },
        },
    }


def raybet_payload(
    *,
    tournament: str = "PGL Wallachia Season 8",
    scheduled_at: str = SCHEDULE_RAW,
    best_of: int = 3,
    team_one_id: int = 501,
    team_one_name: str = "Alpha",
    team_two_id: int = 502,
    team_two_name: str = "Beta",
    stage: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "match-1",
        "game_id": 151,
        "tournament_name": tournament,
        "start_time": scheduled_at,
        "round": f"bo{best_of}",
        "team": [
            {"pos": 1, "team_id": team_one_id, "team_name": team_one_name},
            {"pos": 2, "team_id": team_two_id, "team_name": team_two_name},
        ],
    }
    if stage is not None:
        row["stage"] = stage
    return row


class StrictLiveEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "strict.db"
        with IntelligenceStorage(self.path) as storage:
            storage.init_schema()
        self.set_canonical_teams()
        self.upsert_raybet(updated_at=METADATA_AT)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        init_strict_live_eligibility_schema(self.connection)
        init_alert_schema(self.connection)
        self.clock = patch(
            "live_betting.strict_eligibility._utc_now", return_value=RECORDED_AT
        )
        self.clock.start()

    def tearDown(self) -> None:
        self.clock.stop()
        self.connection.close()
        self.directory.cleanup()

    def upsert_raybet(
        self,
        payload: dict[str, object] | None = None,
        *,
        updated_at: datetime = METADATA_AT,
    ) -> None:
        with LiveBettingStore(self.path) as store:
            store.init_schema()
            store.upsert_raybet_match(payload or raybet_payload(), updated_at)
            store.connection.commit()

    def set_canonical_teams(
        self,
        rows: tuple[tuple[int, str], ...] = (
            (101, "Alpha Canonical"),
            (202, "Beta Canonical"),
        ),
    ) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS teams (
                   team_id INTEGER PRIMARY KEY,
                   name TEXT,
                   tag TEXT,
                   logo_url TEXT,
                   updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        connection.execute("DELETE FROM teams")
        connection.executemany(
            "INSERT INTO teams(team_id, name) VALUES (?, ?)", rows
        )
        connection.commit()
        connection.close()

    def accept(self, **overrides: object):
        values: dict[str, object] = {
            "raybet_match_id": "match-1",
            "map_number": 1,
            "event_id": EVENT_ID,
            "team_one_id": 501,
            "team_two_id": 502,
            "canonical_team_one_id": 101,
            "canonical_team_two_id": 202,
            "source": "manual_event_team_audit",
            "evidence": evidence(),
            "accepted_by": "operator-a",
            "accepted_at": ACCEPTED_AT,
            "acceptance_mode": "manual_exact",
        }
        values.update(overrides)
        if (
            values["acceptance_mode"] == "automatic_exact"
            and "accepted_at" not in overrides
        ):
            values["accepted_at"] = RECORDED_AT
        return accept_strict_live_map_mapping(self.connection, **values)  # type: ignore[arg-type]

    def query(self, *, at: datetime = RECORDED_AT, map_number: int = 1):
        return query_strict_live_eligibility(
            self.connection,
            raybet_match_id="match-1",
            map_number=map_number,
            transport_observed_at=at,
        )

    def create_pending_order(
        self,
        mapping_id: int,
        *,
        order_key: str = "strict-order",
        map_number: int = 1,
        signal_at: datetime = RECORDED_AT,
        expected_insert: bool = True,
    ) -> ShadowOrder:
        signal = OddsSnapshot(
            "match-1",
            f"{order_key}-winner-one",
            f"{order_key}-winner-group",
            signal_at,
            2.5,
            1,
            Market(
                "winner", f"map_{map_number}", "team_one", None, "team_one", True
            ),
        )
        order = ShadowOrder(
            order_key=order_key,
            raybet_match_id="match-1",
            odds_id=signal.odds_id,
            market=signal.market,
            signaled_at=signal_at,
            model_probability=0.6,
            market_probability=0.4,
            signal_price=signal.price,
            signal_transport_key=f"{order_key}:signal",
            signal_transport_at=signal_at,
            expires_at=signal_at + timedelta(seconds=15),
            signal_odds_group_id=signal.odds_group_id,
            signal_outcome_key=signal.market.outcome_key,
            signal_identity_verified=True,
        )
        with LiveBettingStore(self.path) as store:
            store.store_odds_observation(
                source="direct",
                observation_key=order.signal_transport_key,
                source_event_id=None,
                raybet_match_id=order.raybet_match_id,
                observed_at=signal_at,
                normalized_state_hash=normalized_state_hash([signal]),
                snapshots=[signal],
            )
            self.assertEqual(
                store.insert_map_order(
                    order, map_number, strict_mapping_id=mapping_id
                ),
                expected_insert,
            )
        return order

    def create_confirmed_order_outputs(
        self, order: ShadowOrder, *, map_number: int, dota_match_id: int
    ) -> None:
        successor_at = order.signal_transport_at + timedelta(seconds=2)
        successor = OddsSnapshot(
            order.raybet_match_id,
            order.odds_id,
            order.signal_odds_group_id,
            successor_at,
            order.signal_price,
            1,
            order.market,
        )
        with LiveBettingStore(self.path) as store:
            store.store_odds_observation(
                source="direct",
                observation_key=f"{order.order_key}:successor",
                source_event_id=None,
                raybet_match_id=order.raybet_match_id,
                observed_at=successor_at,
                normalized_state_hash=normalized_state_hash([successor]),
                snapshots=[successor],
            )
            resolved = store.process_pending_successor(
                order, watermark=successor_at
            )
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.status, "filled")
            reconciliation = store.record_settlement_reconciliation(
                raybet_match_id=order.raybet_match_id,
                map_number=map_number,
                dota_match_id=dota_match_id,
                raybet_status="confirmed",
                raybet_winner_side="team_one",
                opendota_winner_side="team_one",
                raybet_evidence_ref=f"raybet:{dota_match_id}",
                opendota_evidence_ref=f"opendota:{dota_match_id}",
                raybet_facts={"winner": "team_one"},
                opendota_facts={"winner": "team_one"},
                status="confirmed",
                reason="sources_agree",
                observed_at=successor_at,
            )
            self.assertEqual(reconciliation["status"], "confirmed")
            self.assertTrue(
                store.enqueue_notification(
                    order_key=order.order_key,
                    event_type="monitor_alert",
                    payload={
                        "category": "paper_signal",
                        "source": {"order_key": order.order_key},
                    },
                    stats_cutoff_at=successor_at,
                    created_at=successor_at,
                )
            )
            self.assertTrue(
                store.insert_settlement(
                    order.order_key,
                    "win",
                    order.signal_price,
                    successor_at,
                    f"opendota:{dota_match_id}",
                )
            )
            store.connection.execute(
                """UPDATE notification_outbox
                      SET status='leased', lease_token='test-lease', lease_until=?
                    WHERE order_key=? AND event_type='filled'""",
                (
                    (successor_at + timedelta(minutes=5)).isoformat(),
                    order.order_key,
                ),
            )
            store.connection.commit()

    def assert_invalidation_quarantines_order(
        self,
        *,
        invalidated_mapping_id: int,
        impacted_mapping_id: int,
        order: ShadowOrder,
    ) -> None:
        invalidation_id = invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=invalidated_mapping_id,
            reason="operator withdrew mapping evidence",
            invalidated_by="operator-b",
            invalidated_at=RECORDED_AT,
        )
        settlement = self.connection.execute(
            """SELECT result, review_required FROM settlements
                WHERE order_key=?""",
            (order.order_key,),
        ).fetchone()
        self.assertEqual(tuple(settlement), ("win", 1))
        reconciliation = self.connection.execute(
            """SELECT status, reason, updated_at FROM settlement_reconciliations
                WHERE raybet_match_id=? AND map_number=?""",
            (order.raybet_match_id, int(order.market.period.removeprefix("map_"))),
        ).fetchone()
        self.assertEqual(
            tuple(reconciliation)[:2],
            ("manual_review", "strict_mapping_invalidated"),
        )
        outbox = [
            tuple(row)
            for row in self.connection.execute(
                """SELECT event_type, status, lease_token, lease_until, last_error
                     FROM notification_outbox WHERE order_key=?
                     ORDER BY event_type""",
                (order.order_key,),
            )
        ]
        self.assertEqual(
            outbox,
            [
                ("filled", "dead_letter", None, None, "strict_mapping_invalidated"),
                (
                    "monitor_alert",
                    "dead_letter",
                    None,
                    None,
                    "strict_mapping_invalidated",
                ),
                ("settled", "dead_letter", None, None, "strict_mapping_invalidated"),
            ],
        )
        self.assertEqual(
            [
                tuple(row)
                for row in self.connection.execute(
                    """SELECT action, actor, reason
                         FROM notification_outbox_audit ORDER BY audit_id"""
                )
            ],
            [
                (
                    "blocked",
                    "strict_mapping_invalidation",
                    "strict_mapping_invalidated",
                )
            ]
            * 3,
        )
        impact = self.connection.execute(
            """SELECT mapping_id, invalidation_id FROM strict_live_mapping_impacts
                WHERE dependent_type='shadow_order' AND dependent_key=?""",
            (order.order_key,),
        ).fetchone()
        self.assertEqual(tuple(impact), (impacted_mapping_id, invalidation_id))

        before_repeat = (
            tuple(settlement),
            tuple(reconciliation),
            outbox,
            self.connection.execute(
                "SELECT COUNT(*) FROM notification_outbox_audit"
            ).fetchone()[0],
        )
        repeated = invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=invalidated_mapping_id,
            reason="operator withdrew mapping evidence",
            invalidated_by="operator-b",
            invalidated_at=RECORDED_AT,
        )
        after_repeat = (
            tuple(
                self.connection.execute(
                    "SELECT result, review_required FROM settlements WHERE order_key=?",
                    (order.order_key,),
                ).fetchone()
            ),
            tuple(
                self.connection.execute(
                    """SELECT status, reason, updated_at
                         FROM settlement_reconciliations
                        WHERE raybet_match_id=? AND map_number=?""",
                    (
                        order.raybet_match_id,
                        int(order.market.period.removeprefix("map_")),
                    ),
                ).fetchone()
            ),
            [
                tuple(row)
                for row in self.connection.execute(
                    """SELECT event_type, status, lease_token, lease_until, last_error
                         FROM notification_outbox WHERE order_key=?
                         ORDER BY event_type""",
                    (order.order_key,),
                )
            ],
            self.connection.execute(
                "SELECT COUNT(*) FROM notification_outbox_audit"
            ).fetchone()[0],
        )
        self.assertEqual(repeated, invalidation_id)
        self.assertEqual(after_repeat, before_repeat)

    def test_schema_is_additive_and_accepted_identity_is_immutable(self) -> None:
        self.connection.execute("CREATE TABLE unrelated (value TEXT)")
        self.connection.execute("INSERT INTO unrelated VALUES ('kept')")
        self.connection.commit()
        init_strict_live_eligibility_schema(self.connection)

        mapping = self.accept()

        self.assertEqual(
            self.connection.execute("SELECT value FROM unrelated").fetchone()[0],
            "kept",
        )
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(strict_live_map_mappings)"
            )
        }
        self.assertTrue(
            {
                "raybet_identity_json",
                "raybet_identity_hash",
                "raybet_metadata_updated_at",
                "recorded_at",
                "scheduled_at_utc",
                "stage_scope",
            }
            <= columns
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.connection.execute(
                "UPDATE strict_live_map_mappings SET team_one_id=999 WHERE mapping_id=?",
                (mapping.mapping_id,),
            )
        self.connection.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
            self.connection.execute(
                "DELETE FROM strict_live_map_mappings WHERE mapping_id=?",
                (mapping.mapping_id,),
            )
        self.connection.rollback()

    def test_legacy_schema_migration_does_not_commit_caller_transaction(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                """CREATE TABLE strict_live_map_mappings (
                       mapping_id INTEGER PRIMARY KEY,
                       raybet_match_id TEXT NOT NULL,
                       map_number INTEGER NOT NULL,
                       UNIQUE (raybet_match_id, map_number)
                   )"""
            )
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
            connection.execute("INSERT INTO unrelated VALUES ('uncommitted')")

            with self.assertRaisesRegex(
                StrictMappingError,
                "strict_mapping_schema_migration_requires_clean_transaction",
            ):
                init_strict_live_eligibility_schema(connection)

            self.assertTrue(connection.in_transaction)
            connection.rollback()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0],
                0,
            )
            self.assertNotIn(
                "automatic_exact",
                str(connection.execute(
                    """SELECT sql FROM sqlite_master
                        WHERE type='table' AND name='strict_live_map_mappings'"""
                ).fetchone()[0]),
            )
        finally:
            connection.close()

    def test_unknown_candidate_and_fuzzy_are_never_eligible(self) -> None:
        self.assertEqual(self.query().reason, "accepted_mapping_missing")
        for method in ("candidate", "fuzzy"):
            record_strict_live_mapping_candidate(
                self.connection,
                raybet_match_id="match-1",
                map_number=1,
                source="name_search",
                evidence={"matched_text": "PGL Wallachia"},
                observed_at=ACCEPTED_AT,
                match_method=method,
                proposed_event_id=EVENT_ID,
                proposed_team_one_id=501,
                proposed_team_two_id=502,
                proposed_canonical_team_one_id=101,
                proposed_canonical_team_two_id=202,
            )

        self.assertEqual(self.query().reason, "accepted_mapping_missing")
        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                """SELECT match_method, decision
                   FROM strict_live_map_mapping_audit ORDER BY audit_id"""
            )],
            [("candidate", "audit_only"), ("fuzzy", "audit_only")],
        )
        with self.assertRaises(StrictMappingError):
            record_strict_live_mapping_candidate(
                self.connection,
                raybet_match_id="match-1",
                map_number=1,
                source="bad",
                evidence={"name": "Alpha"},
                observed_at=ACCEPTED_AT,
                match_method="manual_exact",
            )

    def test_accept_requires_exact_raybet_team_ids_and_order(self) -> None:
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_exact_team_ids_mismatch"
        ):
            self.accept(team_one_id=10, team_two_id=20)
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_exact_team_order_mismatch"
        ):
            self.accept(team_one_id=502, team_two_id=501)

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT reason FROM strict_live_map_mapping_audit ORDER BY audit_id"
            )],
            ["raybet_exact_team_ids_mismatch", "raybet_exact_team_order_mismatch"],
        )

    def test_accepted_mapping_is_causal_and_exposes_identity_refs(self) -> None:
        mapping = self.accept(accepted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

        before_recording = self.query(at=RECORDED_AT - timedelta(microseconds=1))
        self.assertFalse(before_recording.eligible)
        self.assertEqual(before_recording.reason, "mapping_not_yet_recorded")
        eligible = self.query()
        self.assertTrue(eligible.eligible)
        self.assertEqual(eligible.mapping, mapping)
        self.assertEqual(mapping.recorded_at, RECORDED_AT)
        self.assertEqual(mapping.raybet_metadata_updated_at, METADATA_AT)
        self.assertEqual(eligible.mapping_refs, eligible.input_refs())
        self.assertEqual(eligible.mapping_refs["strict_raybet_team_one_id"], 501)
        self.assertEqual(eligible.mapping_refs["strict_canonical_team_one_id"], 101)
        self.assertNotEqual(mapping.raybet_team_one_id, mapping.canonical_team_one_id)
        self.assertEqual(mapping.canonical_team_one_name, "Alpha Canonical")
        self.assertEqual(len(mapping.canonical_identity_hash), 64)
        self.assertEqual(len(mapping.crosswalk_evidence_hash), 64)
        self.assertEqual(len(mapping.raybet_identity_hash), 64)
        identity = json.loads(mapping.raybet_identity_json)
        self.assertEqual(identity["team_one"], {"name": "Alpha", "pos": 1, "team_id": 501})

        audit = self.connection.execute(
            """SELECT observed_at, recorded_at, raybet_metadata_updated_at
               FROM strict_live_map_mapping_audit"""
        ).fetchone()
        self.assertEqual(audit[0], datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat())
        self.assertEqual(audit[1], RECORDED_AT.isoformat())
        self.assertEqual(audit[2], METADATA_AT.isoformat())

    def test_future_accepted_or_metadata_times_cannot_be_backdated(self) -> None:
        with self.assertRaisesRegex(StrictMappingError, "accepted_at_in_future"):
            self.accept(accepted_at=RECORDED_AT + timedelta(seconds=1))

        self.upsert_raybet(updated_at=RECORDED_AT + timedelta(seconds=1))
        with self.assertRaisesRegex(StrictMappingError, "raybet_metadata_from_future"):
            self.accept(accepted_at=ACCEPTED_AT)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            0,
        )

    def test_identity_snapshot_detects_team_upsert_drift(self) -> None:
        mapping = self.accept()
        self.upsert_raybet(
            raybet_payload(team_one_id=503, team_one_name="Gamma"),
            updated_at=RECORDED_AT + timedelta(minutes=1),
        )

        result = self.query(at=RECORDED_AT + timedelta(minutes=2))

        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "raybet_metadata_drift")
        self.assertEqual(result.mapping_refs["strict_raybet_identity_hash"], mapping.raybet_identity_hash)

    def test_order_insert_rechecks_mapping_at_signal_transport_time(self) -> None:
        mapping = self.accept()

        self.create_pending_order(
            mapping.mapping_id,
            order_key="backdated-signal-order",
            signal_at=RECORDED_AT - timedelta(microseconds=1),
            expected_insert=False,
        )

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM shadow_orders WHERE order_key='backdated-signal-order'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM shadow_map_attempts
                    WHERE order_key='backdated-signal-order'"""
            ).fetchone()[0],
            0,
        )

    def test_order_insert_rechecks_current_raybet_metadata(self) -> None:
        mapping = self.accept()
        self.upsert_raybet(
            raybet_payload(team_one_id=503, team_one_name="Gamma"),
            updated_at=RECORDED_AT,
        )

        self.create_pending_order(
            mapping.mapping_id,
            order_key="metadata-drift-order",
            expected_insert=False,
        )

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM shadow_orders WHERE order_key='metadata-drift-order'"
            ).fetchone()[0],
            0,
        )

    def test_crosswalk_evidence_and_canonical_teams_are_required(self) -> None:
        missing_crosswalk = evidence()
        del missing_crosswalk["team_crosswalk"]
        with self.assertRaisesRegex(
            StrictMappingError, "team_crosswalk_evidence_missing"
        ):
            self.accept(evidence=missing_crosswalk)

        wrong_name = evidence(canonical_team_one_name="Wrong Canonical Team")
        with self.assertRaisesRegex(
            StrictMappingError, "team_crosswalk_evidence_mismatch"
        ):
            self.accept(evidence=wrong_name)

        with self.assertRaisesRegex(StrictMappingError, "canonical_team_missing"):
            self.accept(
                canonical_team_one_id=999,
                evidence=evidence(
                    canonical_team_one_id=999,
                    canonical_team_one_name="Unknown Canonical Team",
                ),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            0,
        )

    def test_canonical_crosswalk_drift_or_missing_team_is_fail_closed(self) -> None:
        mapping = self.accept()
        self.set_canonical_teams(
            ((101, "Alpha Renamed"), (202, "Beta Canonical"))
        )

        drift = self.query()

        self.assertFalse(drift.eligible)
        self.assertEqual(drift.reason, "canonical_team_metadata_drift")
        self.assertEqual(
            drift.mapping_refs["strict_canonical_identity_hash"],
            mapping.canonical_identity_hash,
        )

        self.set_canonical_teams(((101, "Alpha Canonical"),))
        missing = self.query()
        self.assertFalse(missing.eligible)
        self.assertEqual(missing.reason, "canonical_team_missing")

    def test_same_identity_repoll_does_not_create_false_drift(self) -> None:
        self.accept()
        self.upsert_raybet(updated_at=RECORDED_AT + timedelta(minutes=1))

        result = self.query(at=RECORDED_AT + timedelta(minutes=2))

        self.assertTrue(result.eligible)

    def test_map_number_must_not_exceed_raybet_best_of(self) -> None:
        with self.assertRaisesRegex(StrictMappingError, "map_number_exceeds_best_of"):
            self.accept(map_number=4)
        mapping = self.accept(map_number=3)

        self.assertEqual(mapping.raybet_best_of, 3)
        self.assertTrue(self.query(map_number=3).eligible)

    def test_schedule_must_be_audited_and_inside_event_window(self) -> None:
        outside_raw = "2026-05-20 12:00:00"
        outside_utc = datetime(2026, 5, 20, 4, 0, tzinfo=timezone.utc)
        self.upsert_raybet(raybet_payload(scheduled_at=outside_raw))
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_schedule_outside_event_window"
        ):
            self.accept(
                evidence=evidence(
                    scheduled_at=outside_raw, scheduled_at_utc=outside_utc
                )
            )

        self.upsert_raybet(raybet_payload())
        missing_timezone = evidence()
        del missing_timezone["schedule"]["timezone_evidence"]  # type: ignore[index]
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_schedule_timezone_evidence_missing"
        ):
            self.accept(evidence=missing_timezone)

    def test_tournament_alias_requires_explicit_exact_evidence(self) -> None:
        localized_name = "RayBet Localized PGL Season 8"
        self.upsert_raybet(raybet_payload(tournament=localized_name))
        with self.assertRaisesRegex(
            StrictMappingError, "raybet_tournament_evidence_mismatch"
        ):
            self.accept()

        mapping = self.accept(evidence=evidence(raybet_tournament=localized_name))

        self.assertEqual(
            json.loads(mapping.raybet_identity_json)["tournament"], localized_name
        )
        self.assertTrue(self.query().eligible)

    def test_qualifier_or_unapproved_stage_is_fail_closed(self) -> None:
        self.upsert_raybet(raybet_payload(stage="qualifier"))
        with self.assertRaisesRegex(StrictMappingError, "event_stage_excluded"):
            self.accept(evidence=evidence(stage_scope="qualifier"))

        self.upsert_raybet(raybet_payload())
        with self.assertRaisesRegex(StrictMappingError, "event_stage_not_included"):
            self.accept(evidence=evidence(stage_scope="play_in"))

    def test_event_policy_window_and_evidence_are_rechecked_by_query(self) -> None:
        self.accept()
        cases = (
            ("scope='audit_only'", "event_scope_not_formal_main_event"),
            ("approval_status='pending'", "event_not_approved"),
            ("evidence_status='unverified'", "event_evidence_not_manually_audited"),
            ("official_evidence_urls_json='[]'", "event_official_evidence_missing"),
            (
                "main_event_start_at='2026-04-21T00:00:00+00:00'",
                "raybet_schedule_outside_event_window",
            ),
            ("included_stages_json='[]'", "event_exclusion_policy_incomplete"),
        )
        for update, expected_reason in cases:
            with self.subTest(update=update):
                self.connection.execute(
                    f"UPDATE event_registry SET {update} WHERE event_id=?",  # noqa: S608
                    (EVENT_ID,),
                )
                result = self.query()
                self.assertFalse(result.eligible)
                self.assertEqual(result.reason, expected_reason)
                self.connection.rollback()

    def test_future_event_approval_is_not_knowable_at_transport(self) -> None:
        self.accept()
        self.connection.execute(
            "UPDATE event_registry SET approved_at=? WHERE event_id=?",
            ((RECORDED_AT + timedelta(hours=1)).isoformat(), EVENT_ID),
        )

        result = self.query()

        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "event_approval_not_yet_available")

    def test_exact_value_is_idempotent_across_restart_but_rebind_conflicts(self) -> None:
        first = self.accept()
        self.connection.close()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        init_strict_live_eligibility_schema(self.connection)

        second = self.accept(accepted_at=ACCEPTED_AT + timedelta(minutes=1))
        self.assertEqual(first, second)
        with self.assertRaisesRegex(
            StrictMappingConflictError, "accepted_mapping_rebind_forbidden"
        ):
            self.accept(team_two_id=503)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT decision FROM strict_live_map_mapping_audit ORDER BY audit_id"
            )],
            ["accepted", "idempotent", "conflict"],
        )

    def test_missing_raw_identity_and_invalid_inputs_fail_closed(self) -> None:
        self.connection.execute(
            "UPDATE raybet_matches SET raw_json='{}' WHERE raybet_match_id='match-1'"
        )
        self.connection.commit()
        self.assertEqual(self.query().reason, "accepted_mapping_missing")
        with self.assertRaisesRegex(StrictMappingError, "raybet_raw_match_id_mismatch"):
            self.accept()

        self.assertEqual(
            query_strict_live_eligibility(
                self.connection,
                raybet_match_id="match-1",
                map_number=0,
                transport_observed_at=RECORDED_AT,
            ).reason,
            "invalid_map_number",
        )
        self.assertEqual(
            query_strict_live_eligibility(
                self.connection,
                raybet_match_id="match-1",
                map_number=1,
                transport_observed_at=RECORDED_AT.replace(tzinfo=None),
            ).reason,
            "invalid_transport_time",
        )

    def test_query_is_pure_and_mapping_evidence_is_canonical(self) -> None:
        supplied = evidence()
        supplied["extra"] = {"z": 2, "a": 1}
        mapping = self.accept(evidence=copy.deepcopy(supplied))
        before = self.connection.total_changes

        for _ in range(3):
            self.assertTrue(self.query().eligible)

        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(json.loads(mapping.evidence_json), supplied)

    def test_automatic_exact_requires_preapproved_manual_exact_evidence(self) -> None:
        with self.assertRaisesRegex(
            StrictMappingError, "automatic_exact_evidence_not_preapproved"
        ):
            self.accept(map_number=2, acceptance_mode="automatic_exact")

        manual = self.accept(map_number=1)
        approval_id = approve_automatic_exact_evidence(
            self.connection,
            source_mapping_id=manual.mapping_id,
            approved_by="operator-b",
            approved_at=ACCEPTED_AT,
        )
        automatic = self.accept(
            map_number=2,
            acceptance_mode="automatic_exact",
            accepted_by="automatic-mapper",
        )

        self.assertGreater(approval_id, 0)
        self.assertEqual(automatic.acceptance_mode, "automatic_exact")
        self.assertEqual(automatic.automatic_approval_id, approval_id)
        self.assertTrue(self.query(map_number=2).eligible)
        self.assertEqual(
            self.query(map_number=2).mapping_refs["strict_mapping_acceptance_mode"],
            "automatic_exact",
        )

    def test_automatic_exact_rejects_mapping_accepted_before_approval_record(self) -> None:
        manual = self.accept(map_number=1)
        approve_automatic_exact_evidence(
            self.connection,
            source_mapping_id=manual.mapping_id,
            approved_by="operator-b",
            approved_at=ACCEPTED_AT,
        )

        with self.assertRaisesRegex(
            StrictMappingError,
            "automatic_exact_approval_causal_order_invalid",
        ):
            self.accept(
                map_number=2,
                acceptance_mode="automatic_exact",
                accepted_by="automatic-mapper",
                accepted_at=ACCEPTED_AT,
            )

        automatic = self.accept(
            map_number=2,
            acceptance_mode="automatic_exact",
            accepted_by="automatic-mapper",
            accepted_at=RECORDED_AT,
        )
        gate = strict_read_gate(
            self.connection,
            mapping_id_sql=str(automatic.mapping_id),
            raybet_match_id_sql="'match-1'",
            map_number_sql="2",
            signal_at_sql=f"'{RECORDED_AT.isoformat()}'",
            dependent_type="shadow_order",
            dependent_key_sql="'automatic-order'",
        )
        row = self.connection.execute(
            f"SELECT {gate.included_sql}, {gate.unverifiable_sql}"
        ).fetchone()
        self.assertEqual(tuple(row), (1, 0))

    def test_monitor_cursor_tracks_mapping_approval_and_invalidation(self) -> None:
        mapping = self.accept()
        before_approval = build_monitor_snapshot(
            self.connection, now=RECORDED_AT
        )

        approve_automatic_exact_evidence(
            self.connection,
            source_mapping_id=mapping.mapping_id,
            approved_by="operator-b",
            approved_at=ACCEPTED_AT,
        )
        after_approval = build_monitor_snapshot(self.connection, now=RECORDED_AT)

        self.assertNotEqual(before_approval["cursor"], after_approval["cursor"])
        self.assertNotEqual(
            before_approval["mapping_revision"], after_approval["mapping_revision"]
        )
        before_invalidation = monitor_cursor(self.connection)
        invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=mapping.mapping_id,
            reason="mapping evidence was withdrawn",
            invalidated_by="operator-c",
            invalidated_at=RECORDED_AT,
        )
        self.assertNotEqual(before_invalidation, monitor_cursor(self.connection))

    def test_automatic_request_cannot_reuse_manual_mapping_as_idempotent(self) -> None:
        manual = self.accept()

        with self.assertRaisesRegex(
            StrictMappingError, "automatic_exact_evidence_not_preapproved"
        ):
            self.accept(acceptance_mode="automatic_exact")

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            tuple(self.connection.execute(
                """SELECT match_method, decision, reason
                     FROM strict_live_map_mapping_audit ORDER BY audit_id DESC LIMIT 1"""
            ).fetchone()),
            (
                "automatic_exact",
                "rejected",
                "automatic_exact_evidence_not_preapproved",
            ),
        )
        self.assertEqual(manual.acceptance_mode, "manual_exact")

    def test_automatic_idempotency_requires_same_approval_lineage(self) -> None:
        manual = self.accept(map_number=1)
        approve_automatic_exact_evidence(
            self.connection,
            source_mapping_id=manual.mapping_id,
            approved_by="operator-b",
            approved_at=ACCEPTED_AT,
        )
        automatic = self.accept(
            map_number=2,
            acceptance_mode="automatic_exact",
            accepted_by="automatic-mapper",
        )
        repeated = self.accept(
            map_number=2,
            acceptance_mode="automatic_exact",
            accepted_by="automatic-mapper",
        )
        self.assertEqual(repeated, automatic)

        invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=manual.mapping_id,
            reason="replace approval source",
            invalidated_by="operator-c",
            invalidated_at=RECORDED_AT,
        )
        replacement = self.accept(map_number=1, accepted_by="operator-d")
        approve_automatic_exact_evidence(
            self.connection,
            source_mapping_id=replacement.mapping_id,
            approved_by="operator-e",
            approved_at=ACCEPTED_AT,
        )

        with self.assertRaisesRegex(
            StrictMappingConflictError, "accepted_mapping_rebind_forbidden"
        ):
            self.accept(
                map_number=2,
                acceptance_mode="automatic_exact",
                accepted_by="automatic-mapper",
            )

    def test_source_invalidation_flags_automatic_mapping_dependents(self) -> None:
        manual = self.accept(map_number=1)
        approve_automatic_exact_evidence(
            self.connection,
            source_mapping_id=manual.mapping_id,
            approved_by="operator-b",
            approved_at=ACCEPTED_AT,
        )
        automatic = self.accept(
            map_number=2,
            acceptance_mode="automatic_exact",
            accepted_by="automatic-mapper",
        )
        self.connection.execute(
            """INSERT INTO strategy_decisions VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "decision-map-2", "match-1", 2, RECORDED_AT.isoformat(),
                "team_one", 0.4, 0.5, 0.1, 1.0, 1, "test",
                json.dumps({"__inputs__": {"strict_live_eligibility": {
                    "mapping_refs": {"strict_mapping_id": automatic.mapping_id}
                }}}),
                "input-ref", "test-version",
            ),
        )
        self.connection.commit()

        invalidation_id = invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=manual.mapping_id,
            reason="source evidence was withdrawn",
            invalidated_by="operator-c",
            invalidated_at=RECORDED_AT,
        )
        self.connection.execute(
            """INSERT INTO strategy_decisions VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "decision-map-2-late", "match-1", 2, RECORDED_AT.isoformat(),
                "team_one", 0.4, 0.5, 0.1, 1.0, 1, "test",
                json.dumps({"__inputs__": {"strict_live_eligibility": {
                    "mapping_refs": {"strict_mapping_id": automatic.mapping_id}
                }}}),
                "input-ref-late", "test-version",
            ),
        )
        self.connection.commit()

        result = self.query(map_number=2)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "automatic_exact_approval_invalidated")
        self.assertEqual(
            {tuple(row) for row in self.connection.execute(
                """SELECT mapping_id, invalidation_id, dependent_type, dependent_key
                     FROM strict_live_mapping_impacts"""
            )},
            {
                (
                    automatic.mapping_id,
                    invalidation_id,
                    "strategy_decision",
                    "decision-map-2",
                ),
                (
                    automatic.mapping_id,
                    invalidation_id,
                    "strategy_decision",
                    "decision-map-2-late",
                ),
            },
        )

    def test_late_direct_dependents_after_invalidation_are_flagged(self) -> None:
        mapping = self.accept()
        invalidation_id = invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=mapping.mapping_id,
            reason="mapping invalidated before output commit",
            invalidated_by="operator-b",
            invalidated_at=RECORDED_AT,
        )
        mapping_inputs = {"__inputs__": {"strict_live_eligibility": {
            "mapping_refs": {"strict_mapping_id": mapping.mapping_id}
        }}}
        self.connection.execute(
            """INSERT INTO strategy_decisions VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "late-decision", "match-1", 1, RECORDED_AT.isoformat(),
                "team_one", 0.4, 0.5, 0.1, 1.0, 1, "test",
                json.dumps(mapping_inputs), "late-input", "test-version",
            ),
        )
        self.connection.execute(
            """INSERT INTO odds_transport_observations
               (observation_key, source, source_event_id, raybet_match_id,
                observed_at, normalized_state_hash, timing_status,
                processing_status, normalized_change_count)
               VALUES (?, 'direct', NULL, ?, ?, ?, 'on_time', 'processed', 0)""",
            (
                "late-transport",
                "match-1",
                RECORDED_AT.isoformat(),
                "a" * 64,
            ),
        )
        self.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "late-order", "match-1", mapping.mapping_id, "odds-1",
                "winner|map_1|team_one|", RECORDED_AT.isoformat(), 0.5, 0.4,
                2.5, "late-transport", RECORDED_AT.isoformat(),
                (RECORDED_AT + timedelta(seconds=15)).isoformat(),
                "group-1", "team_one", 1, 1.0, "pending",
            ),
        )
        self.connection.commit()
        prediction = SimpleNamespace(
            prediction_key="late-prediction",
            schema_version="research-live-v1",
            raybet_match_id="match-1",
            map_number=1,
            observed_at=RECORDED_AT,
            game_clock_seconds=600,
            game_minute=10.0,
            selected_side="team_one",
            market_probability=0.5,
            market_price=2.0,
            raw_model_probability=0.5,
            feature_hash=None,
            model_hash=None,
            calibration_hash=None,
            transport_key="late-transport",
            transport_hash="a" * 64,
            radiant_hero_ids=(1, 2, 3, 4, 5),
            dire_hero_ids=(6, 7, 8, 9, 10),
            radiant_team_side="team_one",
            strict_mapping_id=mapping.mapping_id,
            clock_source="vision",
            clock_trust="trusted_vision",
            manual_clock_event_id=None,
            manual_clock_seconds=None,
            manual_clock_trust="not_observed",
            manual_clock_validation="not_observed",
            actionability="research_only",
            gate_status="failed",
            gate_failures=("mapping_invalidated",),
            input_context_hash="b" * 64,
            created_at=RECORDED_AT,
        )
        with LiveBettingStore(self.path) as store:
            self.assertTrue(store.insert_research_prediction(prediction))

        self.assertEqual(
            {tuple(row) for row in self.connection.execute(
                """SELECT mapping_id, invalidation_id, dependent_type, dependent_key
                     FROM strict_live_mapping_impacts"""
            )},
            {
                (
                    mapping.mapping_id,
                    invalidation_id,
                    "strategy_decision",
                    "late-decision",
                ),
                (
                    mapping.mapping_id,
                    invalidation_id,
                    "research_prediction",
                    "late-prediction",
                ),
                (
                    mapping.mapping_id,
                    invalidation_id,
                    "shadow_order",
                    "late-order",
                ),
            },
        )

    def test_invalidation_is_append_only_and_flags_dependent_outputs(self) -> None:
        mapping = self.accept()
        mapping_refs = {"strict_mapping_id": mapping.mapping_id}
        self.connection.execute(
            """INSERT INTO strategy_decisions VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "decision-1", "match-1", 1, RECORDED_AT.isoformat(), "team_one",
                0.4, 0.5, 0.1, 1.0, 1, "test",
                json.dumps({"__inputs__": {"strict_live_eligibility": {
                    "mapping_refs": mapping_refs
                }}}),
                "input-ref", "test-version",
            ),
        )
        self.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at,
                model_probability, market_probability, signal_price,
                signal_transport_key, signal_transport_at, expires_at,
                signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "order-1", "match-1", mapping.mapping_id, "odds-1",
                "winner|map_1|team_one|",
                RECORDED_AT.isoformat(), 0.5, 0.4, 2.5, "transport-1",
                RECORDED_AT.isoformat(), (RECORDED_AT + timedelta(seconds=10)).isoformat(),
                "group-1", "team_one", 1, 1.0, "pending",
            ),
        )
        self.connection.execute(
            "INSERT INTO shadow_map_attempts VALUES (?, ?, ?, ?, ?)",
            ("match-1", 1, "order-1", "pending", RECORDED_AT.isoformat()),
        )
        self.connection.commit()

        invalidation_id = invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=mapping.mapping_id,
            reason="operator corrected canonical identity",
            invalidated_by="operator-b",
            invalidated_at=RECORDED_AT,
        )

        result = self.query()
        self.assertGreater(invalidation_id, 0)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "mapping_invalidated")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM strict_live_map_mappings").fetchone()[0],
            1,
        )
        self.assertEqual(
            {tuple(row) for row in self.connection.execute(
                "SELECT dependent_type, dependent_key FROM strict_live_mapping_impacts"
            )},
            {("strategy_decision", "decision-1"), ("shadow_order", "order-1")},
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.connection.execute(
                "UPDATE strict_live_map_mapping_invalidations SET reason='changed'"
            )
        self.connection.rollback()

        replacement = self.accept(accepted_by="operator-c")
        self.assertNotEqual(replacement.mapping_id, mapping.mapping_id)
        self.assertTrue(self.query().eligible)
        self.assertEqual(
            tuple(self.connection.execute(
                """SELECT previous_mapping_id, replacement_mapping_id
                     FROM strict_live_map_mapping_supersessions"""
            ).fetchone()),
            (mapping.mapping_id, replacement.mapping_id),
        )

    def test_mapping_invalidation_rejects_pending_order_before_successor(self) -> None:
        mapping = self.accept()
        order = self.create_pending_order(mapping.mapping_id)
        invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=mapping.mapping_id,
            reason="operator withdrew mapping evidence",
            invalidated_by="operator-b",
            invalidated_at=RECORDED_AT,
        )
        successor_at = RECORDED_AT + timedelta(seconds=2)
        successor = OddsSnapshot(
            "match-1",
            order.odds_id,
            order.signal_odds_group_id,
            successor_at,
            2.5,
            1,
            order.market,
        )

        with LiveBettingStore(self.path) as store:
            store.store_odds_observation(
                source="direct",
                observation_key="strict-order:successor",
                source_event_id=None,
                raybet_match_id=order.raybet_match_id,
                observed_at=successor_at,
                normalized_state_hash=normalized_state_hash([successor]),
                snapshots=[successor],
            )
            self.assertEqual(
                store.pending_order_block_reason(order.order_key),
                "strict_mapping_invalidated",
            )
            resolved = store.process_pending_successor(
                order, watermark=successor_at
            )
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.status, "rejected")
            self.assertEqual(
                resolved.rejection_reason, "strict_mapping_invalidated"
            )
            report = build_report(store.connection)

        self.assertEqual(report["orders"]["signals"], 0)
        self.assertEqual(
            report["order_audit"]["strict_mapping_invalidated_orders"], 1
        )

    def test_mapping_invalidation_suppresses_fill_mail_and_settlement(self) -> None:
        mapping = self.accept()
        order = self.create_pending_order(mapping.mapping_id, order_key="filled-order")
        successor_at = RECORDED_AT + timedelta(seconds=2)
        successor = OddsSnapshot(
            "match-1",
            order.odds_id,
            order.signal_odds_group_id,
            successor_at,
            2.5,
            1,
            order.market,
        )
        with LiveBettingStore(self.path) as store:
            store.store_odds_observation(
                source="direct",
                observation_key="filled-order:successor",
                source_event_id=None,
                raybet_match_id=order.raybet_match_id,
                observed_at=successor_at,
                normalized_state_hash=normalized_state_hash([successor]),
                snapshots=[successor],
            )
            resolved = store.process_pending_successor(
                order, watermark=successor_at
            )
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.status, "filled")

        invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=mapping.mapping_id,
            reason="operator withdrew mapping evidence",
            invalidated_by="operator-b",
            invalidated_at=RECORDED_AT,
        )

        with LiveBettingStore(self.path) as store:
            self.assertIsNone(claim(store.connection, now=successor_at))
            outbox = store.connection.execute(
                "SELECT status, last_error FROM notification_outbox"
            ).fetchone()
            self.assertEqual(
                tuple(outbox),
                ("dead_letter", "strict_mapping_invalidated"),
            )
            self.assertFalse(
                store.insert_settlement(
                    order.order_key,
                    "win",
                    2.5,
                    successor_at + timedelta(hours=1),
                    "test-result",
                )
            )

    def test_direct_invalidation_immediately_quarantines_existing_outputs(self) -> None:
        mapping = self.accept()
        order = self.create_pending_order(
            mapping.mapping_id, order_key="direct-quarantine-order"
        )
        self.create_confirmed_order_outputs(order, map_number=1, dota_match_id=1001)

        self.assert_invalidation_quarantines_order(
            invalidated_mapping_id=mapping.mapping_id,
            impacted_mapping_id=mapping.mapping_id,
            order=order,
        )

    def test_source_invalidation_quarantines_automatic_mapping_outputs(self) -> None:
        source = self.accept(map_number=1)
        approve_automatic_exact_evidence(
            self.connection,
            source_mapping_id=source.mapping_id,
            approved_by="operator-b",
            approved_at=ACCEPTED_AT,
        )
        automatic = self.accept(
            map_number=2,
            acceptance_mode="automatic_exact",
            accepted_by="automatic-mapper",
        )
        order = self.create_pending_order(
            automatic.mapping_id,
            order_key="automatic-quarantine-order",
            map_number=2,
        )
        self.create_confirmed_order_outputs(order, map_number=2, dota_match_id=1002)

        self.assert_invalidation_quarantines_order(
            invalidated_mapping_id=source.mapping_id,
            impacted_mapping_id=automatic.mapping_id,
            order=order,
        )

    def test_invalidation_preserves_existing_manual_review_reason(self) -> None:
        mapping = self.accept()
        order = self.create_pending_order(
            mapping.mapping_id, order_key="manual-review-order"
        )
        self.create_confirmed_order_outputs(order, map_number=1, dota_match_id=1003)
        self.connection.execute(
            """UPDATE settlement_reconciliations
                  SET status='manual_review', reason='existing_manual_reason'
                WHERE raybet_match_id='match-1' AND map_number=1"""
        )
        self.connection.commit()

        invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=mapping.mapping_id,
            reason="operator withdrew mapping evidence",
            invalidated_by="operator-b",
            invalidated_at=RECORDED_AT,
        )

        self.assertEqual(
            tuple(
                self.connection.execute(
                    """SELECT status, reason FROM settlement_reconciliations
                        WHERE raybet_match_id='match-1' AND map_number=1"""
                ).fetchone()
            ),
            ("manual_review", "existing_manual_reason"),
        )

    def test_non_null_mapping_gate_fails_closed_when_unverifiable(self) -> None:
        mapping = self.accept()
        order = self.create_pending_order(mapping.mapping_id)
        with LiveBettingStore(self.path) as store:
            self.assertEqual(
                store._strict_mapping_context_block_reason(
                    strict_mapping_id=mapping.mapping_id + 10_000,
                    raybet_match_id="match-1",
                    map_number=1,
                    signal_transport_at=RECORDED_AT,
                ),
                "strict_mapping_unverified",
            )
        self.connection.execute("DROP TABLE strict_live_mapping_impacts")
        self.connection.commit()

        with LiveBettingStore(self.path) as store:
            self.assertEqual(
                store.pending_order_block_reason(order.order_key),
                "strict_mapping_gate_unavailable",
            )

    def test_null_mapping_legacy_order_keeps_settlement_and_notification_semantics(
        self,
    ) -> None:
        self.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status, fill_price, filled_at)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-null-mapping",
                "match-1",
                "legacy-odds",
                "winner|map_1|team_one|",
                RECORDED_AT.isoformat(),
                0.6,
                0.4,
                2.5,
                "legacy-transport",
                RECORDED_AT.isoformat(),
                (RECORDED_AT + timedelta(seconds=15)).isoformat(),
                "legacy-group",
                "team_one",
                1,
                1.0,
                "filled",
                2.5,
                RECORDED_AT.isoformat(),
            ),
        )
        self.connection.execute(
            "INSERT INTO shadow_map_attempts VALUES (?, ?, ?, ?, ?)",
            (
                "match-1",
                1,
                "legacy-null-mapping",
                "filled",
                RECORDED_AT.isoformat(),
            ),
        )
        self.connection.commit()

        with LiveBettingStore(self.path) as store:
            self.assertIsNone(store.order_block_reason("legacy-null-mapping"))
            self.assertTrue(
                store.insert_settlement(
                    "legacy-null-mapping",
                    "win",
                    2.5,
                    RECORDED_AT,
                    "legacy-result",
                )
            )
            self.assertTrue(
                store.enqueue_notification(
                    order_key="legacy-null-mapping",
                    event_type="monitor_alert",
                    payload={
                        "category": "paper_signal",
                        "source": {"order_key": "legacy-null-mapping"},
                    },
                    stats_cutoff_at=RECORDED_AT,
                    created_at=RECORDED_AT,
                )
            )
            first = claim(store.connection, now=RECORDED_AT)
            second = claim(store.connection, now=RECORDED_AT)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(
            {first.event_type, second.event_type}, {"settled", "monitor_alert"}
        )

    def test_replacement_invalidation_does_not_claim_prior_shadow_order(self) -> None:
        original = self.accept()
        self.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "original-order", "match-1", original.mapping_id, "odds-1",
                "winner|map_1|team_one|", RECORDED_AT.isoformat(), 0.5, 0.4,
                2.5, "transport-original", RECORDED_AT.isoformat(),
                (RECORDED_AT + timedelta(seconds=15)).isoformat(),
                "group-1", "team_one", 1, 1.0, "pending",
            ),
        )
        self.connection.execute(
            "INSERT INTO shadow_map_attempts VALUES (?, ?, ?, ?, ?)",
            ("match-1", 1, "original-order", "pending", RECORDED_AT.isoformat()),
        )
        self.connection.commit()
        original_invalidation = invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=original.mapping_id,
            reason="replace original mapping",
            invalidated_by="operator-b",
            invalidated_at=RECORDED_AT,
        )
        replacement = self.accept(accepted_by="operator-c")

        invalidate_strict_live_map_mapping(
            self.connection,
            mapping_id=replacement.mapping_id,
            reason="replace the replacement mapping",
            invalidated_by="operator-d",
            invalidated_at=RECORDED_AT,
        )

        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                """SELECT mapping_id, invalidation_id, dependent_key
                     FROM strict_live_mapping_impacts
                    WHERE dependent_type='shadow_order'"""
            )],
            [(original.mapping_id, original_invalidation, "original-order")],
        )


if __name__ == "__main__":
    unittest.main()
