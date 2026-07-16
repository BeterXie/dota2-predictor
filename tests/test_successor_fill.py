from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from live_betting.markets import normalized_state_hash
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
MATCH_ID = "match-1"
TARGET_ID = "winner-one"


def snapshot(
    at: datetime,
    *,
    odds_id: str = TARGET_ID,
    price: float = 2.0,
    status: str | int | None = 1,
    side: str = "team_one",
    odds_group_id: str = "winner",
    outcome_key: str | None = None,
) -> OddsSnapshot:
    return OddsSnapshot(
        MATCH_ID,
        odds_id,
        odds_group_id,
        at,
        price,
        status,
        Market("winner", "map_1", side, None, outcome_key or side, True),
    )


class SuccessorFillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "live.db"
        self.store = LiveBettingStore(self.path)
        self.store.init_schema()
        self.strict_mapping_id = 1
        self.strict_context_patch = patch.object(
            self.store, "_strict_mapping_context_block_reason", return_value=None
        )
        self.strict_order_patch = patch.object(
            self.store, "_strict_mapping_block_reason_for_order", return_value=None
        )
        self.strict_context_patch.start()
        self.strict_order_patch.start()
        self.addCleanup(self.strict_order_patch.stop)
        self.addCleanup(self.strict_context_patch.stop)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def observe(
        self, key: str, at: datetime, rows: list[OddsSnapshot]
    ) -> tuple[str, int]:
        return self.store.store_odds_observation(
            source="direct",
            observation_key=key,
            source_event_id=None,
            raybet_match_id=MATCH_ID,
            observed_at=at,
            normalized_state_hash=normalized_state_hash(rows),
            snapshots=rows,
        )

    def insert_strict_mapping(self) -> int:
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS event_registry (event_id TEXT PRIMARY KEY)"
        )
        self.store.connection.execute(
            "INSERT INTO event_registry (event_id) VALUES ('event-test')"
        )
        identity_json = "{}"
        identity_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
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
               VALUES (?, 1, 'event-test', 101, 202, 101, 'Alpha', 202, 'Beta',
                       ?, ?, ?, ?, 'main_event', ?, 3, ?, ?, ?, 'test', ?, ?,
                       'test-v1', 'manual_exact', NULL, 'test', ?, ?, ?)""",
            (
                MATCH_ID,
                identity_json,
                identity_hash,
                identity_json,
                identity_hash,
                (NOW - timedelta(days=1)).isoformat(),
                identity_json,
                identity_hash,
                (NOW - timedelta(days=1)).isoformat(),
                identity_json,
                identity_hash,
                (NOW - timedelta(days=1)).isoformat(),
                (NOW - timedelta(days=1)).isoformat(),
                (NOW - timedelta(days=1)).isoformat(),
            ),
        )
        self.store.connection.commit()
        return int(cursor.lastrowid)

    def pending_order(self) -> ShadowOrder:
        signal = snapshot(NOW)
        self.observe("signal", NOW, [signal])
        order = ShadowOrder(
            order_key="order-1",
            raybet_match_id=MATCH_ID,
            odds_id=TARGET_ID,
            market=snapshot(NOW).market,
            signaled_at=NOW,
            model_probability=0.6,
            market_probability=0.5,
            signal_price=2.0,
            signal_transport_key="signal",
            signal_transport_at=NOW,
            expires_at=NOW + timedelta(seconds=15),
            signal_odds_group_id=signal.odds_group_id,
            signal_outcome_key=signal.market.outcome_key,
            signal_identity_verified=True,
        )
        self.assertTrue(
            self.store.insert_map_order(
                order, 1, strict_mapping_id=self.strict_mapping_id
            )
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT strict_mapping_id FROM shadow_orders WHERE order_key='order-1'"
            ).fetchone()[0],
            self.strict_mapping_id,
        )
        return order

    def statuses(self) -> tuple[str, str, str | None]:
        row = self.store.connection.execute(
            """SELECT o.status, a.status, o.rejection_reason
                 FROM shadow_orders o JOIN shadow_map_attempts a
                   ON a.order_key=o.order_key
                WHERE o.order_key='order-1'"""
        ).fetchone()
        return str(row[0]), str(row[1]), row[2]

    def test_legacy_writers_cannot_bypass_successor_fill(self) -> None:
        order = self.pending_order()
        forged = replace(
            order,
            status="filled",
            fill_price=2.0,
            filled_at=NOW + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(RuntimeError, "legacy order updater"):
            self.store.update_order(forged)
        self.assertFalse(
            self.store.insert_settlement(
                order.order_key,
                "win",
                2.0,
                NOW + timedelta(seconds=2),
                "forged-result",
            )
        )

        self.assertEqual(self.statuses(), ("pending", "pending", None))
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM settlements"
            ).fetchone()[0],
            0,
        )

    def test_every_response_persists_exact_membership_when_semantics_unchanged(self) -> None:
        first = [
            snapshot(NOW),
            snapshot(NOW, odds_id="winner-two", price=1.8, side="team_two"),
        ]
        later_at = NOW + timedelta(seconds=2)
        later = [
            snapshot(later_at),
            snapshot(later_at, odds_id="winner-two", price=1.8, side="team_two"),
        ]

        self.observe("first", NOW, first)
        timing, changes = self.observe("unchanged", later_at, later)

        self.assertEqual((timing, changes), ("on_time", 0))
        memberships = self.store.connection.execute(
            """SELECT observation_key, odds_id FROM odds_response_outcomes
               ORDER BY observation_key, odds_id"""
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in memberships],
            [
                ("first", "winner-one"),
                ("first", "winner-two"),
                ("unchanged", "winner-one"),
                ("unchanged", "winner-two"),
            ],
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM odds_snapshots"
            ).fetchone()[0],
            2,
        )

    def test_response_membership_is_immutable_and_duplicate_key_must_match(self) -> None:
        self.observe("response", NOW, [snapshot(NOW)])

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.store.execute(
                """UPDATE odds_response_outcomes SET price=3.0
                    WHERE observation_key='response'"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.store.execute(
                """DELETE FROM odds_response_outcomes
                    WHERE observation_key='response'"""
            )
        with self.assertRaisesRegex(ValueError, "membership or payload differs"):
            self.observe(
                "response",
                NOW,
                [snapshot(NOW, side="team_two")],
            )

    def test_processed_transport_identity_and_event_time_are_immutable(self) -> None:
        self.observe("response", NOW, [snapshot(NOW)])

        for statement in (
            """UPDATE odds_transport_observations
                   SET observed_at='2030-01-01T00:00:00+00:00'
                 WHERE observation_key='response'""",
            """UPDATE odds_transport_observations
                   SET timing_status='late'
                 WHERE observation_key='response'""",
            """UPDATE odds_transport_observations
                   SET normalized_change_count=999
                 WHERE observation_key='response'""",
            """DELETE FROM odds_transport_observations
                 WHERE observation_key='response'""",
        ):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    self.store.execute(statement)

    def test_order_insert_requires_exact_open_signal_response(self) -> None:
        signal = snapshot(NOW)
        self.observe("signal", NOW, [signal])
        wrong_price = ShadowOrder(
            order_key="wrong-price",
            raybet_match_id=MATCH_ID,
            odds_id=TARGET_ID,
            market=signal.market,
            signaled_at=NOW,
            model_probability=0.6,
            market_probability=0.5,
            signal_price=1.5,
            signal_transport_key="signal",
            signal_transport_at=NOW,
            expires_at=NOW + timedelta(seconds=15),
            signal_odds_group_id=signal.odds_group_id,
            signal_outcome_key=signal.market.outcome_key,
            signal_identity_verified=True,
        )
        self.assertFalse(
            self.store.insert_map_order(wrong_price, 1, strict_mapping_id=1)
        )

        closed_at = NOW + timedelta(seconds=1)
        closed = snapshot(closed_at, status="suspended")
        self.observe("closed-signal", closed_at, [closed])
        closed_order = ShadowOrder(
            order_key="closed-signal-order",
            raybet_match_id=MATCH_ID,
            odds_id=TARGET_ID,
            market=closed.market,
            signaled_at=closed_at,
            model_probability=0.6,
            market_probability=0.5,
            signal_price=closed.price,
            signal_transport_key="closed-signal",
            signal_transport_at=closed_at,
            expires_at=closed_at + timedelta(seconds=15),
            signal_odds_group_id=closed.odds_group_id,
            signal_outcome_key=closed.market.outcome_key,
            signal_identity_verified=True,
        )
        self.assertFalse(
            self.store.insert_map_order(closed_order, 1, strict_mapping_id=1)
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM shadow_map_attempts"
            ).fetchone()[0],
            0,
        )

    def test_response_outcome_time_must_equal_transport_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "transport time mismatch"):
            self.observe(
                "mismatched-time",
                NOW + timedelta(seconds=1),
                [snapshot(NOW)],
            )

    def test_complete_late_response_keeps_membership_but_is_not_a_successor(self) -> None:
        order = self.pending_order()
        newest_at = NOW + timedelta(seconds=4)
        self.observe("newest", newest_at, [snapshot(newest_at, price=2.1)])
        late_at = NOW + timedelta(seconds=2)
        timing, _ = self.observe("late", late_at, [snapshot(late_at, price=1.5)])

        self.assertEqual(timing, "late")
        membership = self.store.connection.execute(
            """SELECT price FROM odds_response_outcomes
               WHERE observation_key='late' AND odds_id=?""",
            (TARGET_ID,),
        ).fetchone()
        self.assertEqual(float(membership[0]), 1.5)
        resolved = self.store.process_pending_successor(order, watermark=newest_at)
        self.assertEqual((resolved.status, resolved.fill_price), ("filled", 2.1))

    def test_missing_target_in_first_successor_rejects_without_skipping(self) -> None:
        order = self.pending_order()
        first_at = NOW + timedelta(seconds=2)
        self.observe(
            "missing",
            first_at,
            [snapshot(first_at, odds_id="winner-two", price=1.7, side="team_two")],
        )
        second_at = NOW + timedelta(seconds=3)
        self.observe("favorable", second_at, [snapshot(second_at, price=2.2)])

        resolved = self.store.process_pending_successor(order, watermark=second_at)

        self.assertEqual(resolved.rejection_reason, "outcome_missing")
        self.assertEqual(self.statuses(), ("rejected", "rejected", "outcome_missing"))

    def test_first_successor_slippage_rejects_without_using_later_price(self) -> None:
        order = self.pending_order()
        first_at = NOW + timedelta(seconds=2)
        self.observe("adverse", first_at, [snapshot(first_at, price=1.8)])
        second_at = NOW + timedelta(seconds=3)
        self.observe("favorable", second_at, [snapshot(second_at, price=2.1)])

        resolved = self.store.process_pending_successor(order, watermark=second_at)

        self.assertEqual(resolved.rejection_reason, "slippage")
        self.assertIsNone(resolved.fill_price)

    def test_closed_first_successor_rejects_market_closed(self) -> None:
        order = self.pending_order()
        at = NOW + timedelta(seconds=2)
        self.observe("closed", at, [snapshot(at, status=5)])

        resolved = self.store.process_pending_successor(order, watermark=at)

        self.assertEqual(resolved.rejection_reason, "market_closed")
        self.assertEqual(self.statuses(), ("rejected", "rejected", "market_closed"))

    def test_reused_odds_id_for_another_market_rejects_market_mismatch(self) -> None:
        order = self.pending_order()
        at = NOW + timedelta(seconds=2)
        reused = OddsSnapshot(
            MATCH_ID,
            TARGET_ID,
            "other-market",
            at,
            2.5,
            5,
            Market(
                "kill_handicap",
                "map_2",
                "team_two",
                4.5,
                "team_two:4.5",
                True,
            ),
        )
        self.observe("reused-id", at, [reused])

        resolved = self.store.process_pending_successor(order, watermark=at)

        self.assertEqual(resolved.rejection_reason, "market_mismatch")
        self.assertEqual(self.statuses(), ("rejected", "rejected", "market_mismatch"))

    def test_same_market_coordinates_with_rebound_identity_are_rejected(self) -> None:
        order = self.pending_order()
        at = NOW + timedelta(seconds=2)
        rebound = snapshot(
            at,
            odds_group_id="replacement-winner-group",
            outcome_key="replacement-team-one",
        )
        self.observe("rebound-identity", at, [rebound])

        resolved = self.store.process_pending_successor(order, watermark=at)

        self.assertEqual(resolved.rejection_reason, "outcome_identity_mismatch")
        self.assertEqual(
            self.statuses(),
            ("rejected", "rejected", "outcome_identity_mismatch"),
        )

    def test_successor_at_expiry_fills(self) -> None:
        order = self.pending_order()
        at = order.expires_at
        self.observe("boundary", at, [snapshot(at, price=1.98)])

        resolved = self.store.process_pending_successor(order, watermark=at)

        self.assertEqual((resolved.status, resolved.fill_price, resolved.filled_at),
                         ("filled", 1.98, at))
        self.assertEqual(self.statuses(), ("filled", "filled", None))

    def test_wall_clock_after_expiry_without_transport_remains_pending(self) -> None:
        order = self.pending_order()

        resolved = self.store.process_pending_successor(
            order, watermark=order.expires_at + timedelta(microseconds=1)
        )

        self.assertIsNone(resolved)
        self.assertEqual(self.statuses(), ("pending", "pending", None))

    def test_empty_processed_successor_after_expiry_is_timeout(self) -> None:
        order = self.pending_order()
        at = order.expires_at + timedelta(microseconds=1)
        self.observe("empty-successor", at, [])

        resolved = self.store.process_pending_successor(order, watermark=at)

        self.assertEqual(resolved.rejection_reason, "fill_timeout")
        self.assertEqual(self.statuses(), ("rejected", "rejected", "fill_timeout"))

    def test_delayed_ingestion_within_expiry_matches_uninterrupted_result(self) -> None:
        order = self.pending_order()
        wall_clock = order.expires_at + timedelta(minutes=1)

        self.assertIsNone(
            self.store.process_pending_successor(order, watermark=wall_clock)
        )
        at = NOW + timedelta(seconds=2)
        self.observe("delayed-valid-successor", at, [snapshot(at, price=1.99)])

        resolved = self.store.process_pending_successor(order, watermark=at)

        self.assertEqual((resolved.status, resolved.fill_price), ("filled", 1.99))

    def test_first_successor_after_expiry_is_timeout(self) -> None:
        order = self.pending_order()
        at = order.expires_at + timedelta(seconds=1)
        self.observe("too-late", at, [snapshot(at, price=2.5)])

        resolved = self.store.process_pending_successor(order, watermark=at)

        self.assertEqual(resolved.rejection_reason, "fill_timeout")

    def test_restart_resolves_once_from_persisted_membership(self) -> None:
        order = self.pending_order()
        at = NOW + timedelta(seconds=2)
        self.observe("successor", at, [snapshot(at, price=1.99)])
        self.store.close()

        self.store = LiveBettingStore(self.path)
        self.store.init_schema()
        with (
            patch.object(
                self.store,
                "_strict_mapping_block_reason_for_order",
                return_value=None,
            ),
            patch.object(
                self.store,
                "_strict_mapping_context_block_reason",
                return_value=None,
            ),
        ):
            resolved = self.store.process_pending_successor(order, watermark=at)
            self.assertEqual(
                (resolved.status, resolved.fill_price), ("filled", 1.99)
            )
            self.assertIsNone(
                self.store.process_pending_successor(order, watermark=at)
            )

        self.store.close()
        self.store = LiveBettingStore(self.path)
        self.store.init_schema()
        self.assertEqual(self.statuses(), ("filled", "filled", None))

    def test_map_attempt_failure_rolls_back_order_transition(self) -> None:
        order = self.pending_order()
        at = NOW + timedelta(seconds=2)
        self.observe("successor", at, [snapshot(at, price=1.99)])

        with patch.object(
            self.store, "update_map_attempt", side_effect=RuntimeError("injected")
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.store.process_pending_successor(order, watermark=at)

        self.assertEqual(self.statuses(), ("pending", "pending", None))


class ShadowOrderMigrationTests(unittest.TestCase):
    def test_old_thirteen_column_table_upgrades_reentrantly_with_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """CREATE TABLE shadow_orders (
                    order_key TEXT PRIMARY KEY,
                    raybet_match_id TEXT NOT NULL,
                    odds_id TEXT NOT NULL,
                    market_key TEXT NOT NULL,
                    signaled_at TEXT NOT NULL,
                    model_probability REAL NOT NULL,
                    market_probability REAL NOT NULL,
                    signal_price REAL NOT NULL,
                    stake REAL NOT NULL,
                    status TEXT NOT NULL,
                    fill_price REAL,
                    filled_at TEXT,
                    rejection_reason TEXT
                );
                CREATE TABLE odds_transport_observations (
                    observation_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_event_id TEXT,
                    raybet_match_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    normalized_state_hash TEXT NOT NULL,
                    timing_status TEXT NOT NULL,
                    processing_status TEXT NOT NULL,
                    normalized_change_count INTEGER NOT NULL
                );
                CREATE TABLE odds_response_outcomes (
                    observation_key TEXT NOT NULL,
                    raybet_match_id TEXT NOT NULL,
                    odds_id TEXT NOT NULL,
                    odds_group_id TEXT,
                    received_at TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT,
                    market_type TEXT NOT NULL,
                    period TEXT NOT NULL,
                    side TEXT,
                    line REAL,
                    outcome_key TEXT NOT NULL,
                    supported INTEGER NOT NULL,
                    last_update TEXT,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (observation_key, odds_id)
                );"""
            )
            connection.execute(
                "INSERT INTO shadow_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-order",
                    MATCH_ID,
                    TARGET_ID,
                    "winner|map_1|team_one|",
                    NOW.isoformat(),
                    0.6,
                    0.5,
                    2.0,
                    1.0,
                    "pending",
                    None,
                    None,
                    None,
                ),
            )
            connection.execute(
                "INSERT INTO odds_transport_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "same-time-response",
                    "direct",
                    None,
                    MATCH_ID,
                    NOW.isoformat(),
                    "state-hash",
                    "on_time",
                    "processed",
                    1,
                ),
            )
            connection.execute(
                """INSERT INTO odds_response_outcomes VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "same-time-response",
                    MATCH_ID,
                    TARGET_ID,
                    "winner",
                    NOW.isoformat(),
                    2.0,
                    "5",
                    "winner",
                    "map_1",
                    "team_one",
                    None,
                    "team_one",
                    1,
                    None,
                    "{}",
                ),
            )
            connection.commit()
            connection.close()

            with LiveBettingStore(path) as store:
                store.init_schema()
                store.init_schema()
                columns = {
                    str(row["name"]): (int(row["notnull"]), row["dflt_value"])
                    for row in store.connection.execute(
                        "PRAGMA table_info(shadow_orders)"
                    )
                }
                for name in (
                    "signal_transport_key",
                    "signal_transport_at",
                    "expires_at",
                    "signal_identity_verified",
                ):
                    self.assertEqual(columns[name], (1, None))
                self.assertEqual(columns["strict_mapping_id"], (0, None))
                row = store.connection.execute(
                    """SELECT signal_transport_key, signal_transport_at, expires_at,
                              signal_odds_group_id, signal_outcome_key,
                              signal_identity_verified, strict_mapping_id
                         FROM shadow_orders WHERE order_key='legacy-order'"""
                ).fetchone()
                self.assertEqual(str(row[0]), "legacy:legacy-order")
                self.assertEqual(str(row[1]), NOW.isoformat())
                self.assertEqual(
                    str(row[2]), (NOW + timedelta(seconds=15)).isoformat()
                )
                self.assertIsNone(row[3])
                self.assertIsNone(row[4])
                self.assertEqual(int(row[5]), 0)
                self.assertIsNone(row[6])


if __name__ == "__main__":
    unittest.main()
