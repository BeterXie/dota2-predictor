from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from live_betting.markets import normalized_state_hash
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.notifications import (
    EVENT_FILLED,
    EVENT_SETTLED,
    RETRY_DELAYS,
    claim,
    enqueue,
    mark_failure,
    mark_sent,
    requeue_dead_letter,
    simulation_payload,
    stable_message_id,
)
from live_betting.smtp_delivery import SMTPConfig
from live_betting.storage import LiveBettingStore
from scripts.run_notification_worker import run_once as run_notification_once


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)


class NotificationOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = LiveBettingStore(Path(self.directory.name) / "outbox.db")
        self.store.init_schema()

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def payload(self, event_type: str = EVENT_FILLED) -> dict[str, object]:
        return simulation_payload(
            event_type,
            {"raybet_match_id": "match-1", "value": 1},
        )

    def add(self, event_type: str = EVENT_FILLED) -> bool:
        return enqueue(
            self.store.connection,
            order_key="order-1",
            event_type=event_type,
            payload=self.payload(event_type),
            stats_cutoff_at=NOW,
            created_at=NOW,
        )

    def test_logical_key_and_message_id_are_idempotent(self) -> None:
        self.assertTrue(self.add())
        self.assertFalse(self.add())
        row = self.store.connection.execute(
            "SELECT message_id, status, attempt_count FROM notification_outbox"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (stable_message_id("order-1", EVENT_FILLED), "pending", 0),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.store.connection.execute(
                "UPDATE notification_outbox SET payload_json='{}' WHERE outbox_id=1"
            )

    def test_simulation_markers_cannot_be_overridden(self) -> None:
        payload = simulation_payload(
            EVENT_FILLED,
            {
                "simulation": False,
                "real_wager_placed": True,
                "event_type": EVENT_SETTLED,
                "template_version": "unsafe",
            },
        )
        self.assertTrue(payload["simulation"])
        self.assertFalse(payload["real_wager_placed"])
        self.assertEqual(payload["event_type"], EVENT_FILLED)

    def test_claim_fencing_and_expired_lease_recovery(self) -> None:
        self.assertTrue(self.add())
        first = claim(self.store.connection, now=NOW, lease_seconds=30)
        self.assertIsNotNone(first)
        self.assertIsNone(claim(self.store.connection, now=NOW + timedelta(seconds=1)))
        assert first is not None and first.lease_token is not None
        self.assertFalse(
            mark_sent(
                self.store.connection,
                outbox_id=first.outbox_id,
                lease_token="stale-token",
                sent_at=NOW,
            )
        )
        second = claim(
            self.store.connection,
            now=NOW + timedelta(seconds=31),
            lease_seconds=30,
        )
        self.assertIsNotNone(second)
        assert second is not None and second.lease_token is not None
        self.assertFalse(
            mark_sent(
                self.store.connection,
                outbox_id=first.outbox_id,
                lease_token=first.lease_token,
                sent_at=NOW,
            )
        )
        self.assertTrue(
            mark_sent(
                self.store.connection,
                outbox_id=second.outbox_id,
                lease_token=second.lease_token,
                sent_at=NOW + timedelta(seconds=31),
            )
        )

    def test_transient_retry_schedule_and_permanent_dead_letter(self) -> None:
        self.assertTrue(self.add())
        expected = NOW
        for index, delay in enumerate(RETRY_DELAYS, start=1):
            record = claim(self.store.connection, now=expected)
            self.assertIsNotNone(record)
            assert record is not None and record.lease_token is not None
            self.assertTrue(
                mark_failure(
                    self.store.connection,
                    outbox_id=record.outbox_id,
                    lease_token=record.lease_token,
                    transient=True,
                    reason="network_failure",
                    now=expected,
                )
            )
            row = self.store.connection.execute(
                "SELECT status, next_attempt_at, attempt_count FROM notification_outbox"
            ).fetchone()
            self.assertEqual(row[0], "pending")
            self.assertEqual(int(row[2]), index)
            expected = expected + timedelta(seconds=delay)
        record = claim(self.store.connection, now=expected)
        self.assertIsNotNone(record)
        assert record is not None and record.lease_token is not None
        self.assertTrue(
            mark_failure(
                self.store.connection,
                outbox_id=record.outbox_id,
                lease_token=record.lease_token,
                transient=True,
                reason="network_failure",
                now=expected,
            )
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM notification_outbox"
            ).fetchone()[0],
            "dead_letter",
        )

    def test_permanent_failure_and_audited_requeue(self) -> None:
        self.assertTrue(self.add())
        record = claim(self.store.connection, now=NOW)
        assert record is not None and record.lease_token is not None
        self.assertTrue(
            mark_failure(
                self.store.connection,
                outbox_id=record.outbox_id,
                lease_token=record.lease_token,
                transient=False,
                reason="authentication_failure",
                now=NOW,
            )
        )
        self.assertTrue(
            requeue_dead_letter(
                self.store.connection,
                outbox_id=record.outbox_id,
                actor="operator",
                reason="credentials_fixed",
                now=NOW + timedelta(minutes=1),
            )
        )
        row = self.store.connection.execute(
            "SELECT status, last_error FROM notification_outbox"
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", "requeued:credentials_fixed"))
        audit = self.store.connection.execute(
            "SELECT action, actor, reason FROM notification_outbox_audit"
        ).fetchone()
        self.assertEqual(tuple(audit), ("requeue", "operator", "credentials_fixed"))
        retried_at = NOW + timedelta(minutes=1)
        retried = claim(self.store.connection, now=retried_at)
        assert retried is not None and retried.lease_token is not None
        self.assertEqual(retried.attempt_count, 1)
        self.assertTrue(
            mark_failure(
                self.store.connection,
                outbox_id=retried.outbox_id,
                lease_token=retried.lease_token,
                transient=True,
                reason="network_failure",
                now=retried_at,
            )
        )
        row = self.store.connection.execute(
            "SELECT status, attempt_count, next_attempt_at FROM notification_outbox"
        ).fetchone()
        self.assertEqual(tuple(row[:2]), ("pending", 1))
        self.assertEqual(
            datetime.fromisoformat(str(row[2])),
            retried_at + timedelta(seconds=RETRY_DELAYS[0]),
        )

    def test_worker_reports_dead_letter_when_retry_budget_is_exhausted(self) -> None:
        due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertTrue(
            enqueue(
                self.store.connection,
                order_key="exhausted-order",
                event_type=EVENT_FILLED,
                payload=self.payload(),
                stats_cutoff_at=due_at,
                created_at=due_at,
            )
        )
        self.store.connection.execute(
            "UPDATE notification_outbox SET attempt_count=? WHERE order_key=?",
            (len(RETRY_DELAYS), "exhausted-order"),
        )
        with patch(
            "scripts.run_notification_worker.send_message",
            side_effect=OSError("network unavailable"),
        ):
            result = run_notification_once(
                self.store,
                SMTPConfig("sender@qq.com", "not-a-real-credential"),
            )
        self.assertEqual(result["status"], "dead_letter")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM notification_outbox WHERE order_key=?",
                ("exhausted-order",),
            ).fetchone()[0],
            "dead_letter",
        )

    def _order(self) -> ShadowOrder:
        signal = OddsSnapshot(
            "match-1",
            "winner-one",
            "winner-group",
            NOW,
            2.0,
            5,
            Market("winner", "map_1", "team_one", None, "team_one", True),
        )
        self.store.store_odds_observation(
            source="direct",
            observation_key="signal",
            source_event_id=None,
            raybet_match_id="match-1",
            observed_at=NOW,
            normalized_state_hash=normalized_state_hash([signal]),
            snapshots=[signal],
        )
        return ShadowOrder(
            order_key="order-1",
            raybet_match_id="match-1",
            odds_id="winner-one",
            market=signal.market,
            signaled_at=NOW,
            model_probability=0.6,
            market_probability=0.5,
            signal_price=2.0,
            signal_transport_key="signal",
            signal_transport_at=NOW,
            expires_at=NOW + timedelta(seconds=15),
            signal_odds_group_id="winner-group",
            signal_outcome_key="team_one",
            signal_identity_verified=True,
        )

    def test_fill_and_settlement_schedule_notifications_atomically(self) -> None:
        order = self._order()
        self.assertTrue(self.store.insert_map_order(order, 1))
        successor_at = NOW + timedelta(seconds=2)
        successor = OddsSnapshot(
            "match-1", "winner-one", "winner-group", successor_at, 1.99, 5,
            order.market,
        )
        self.store.store_odds_observation(
            source="direct",
            observation_key="successor",
            source_event_id=None,
            raybet_match_id="match-1",
            observed_at=successor_at,
            normalized_state_hash=normalized_state_hash([successor]),
            snapshots=[successor],
        )
        resolved = self.store.process_pending_successor(order, watermark=successor_at)
        self.assertEqual(resolved.status, "filled")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT event_type FROM notification_outbox"
            ).fetchone()[0],
            EVENT_FILLED,
        )
        self.assertTrue(
            self.store.insert_settlement(
                "order-1", "win", 1.99, successor_at, "opendota:1"
            )
        )
        events = [row[0] for row in self.store.connection.execute(
            "SELECT event_type FROM notification_outbox ORDER BY outbox_id"
        )]
        self.assertEqual(events, [EVENT_FILLED, EVENT_SETTLED])

    def test_review_required_settlement_does_not_mail(self) -> None:
        self.assertTrue(
            self.store.insert_settlement(
                "unknown-order", "review", 0.0, NOW, "conflict", True
            )
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM notification_outbox"
            ).fetchone()[0],
            0,
        )

    def test_fill_and_outbox_roll_back_together_on_notification_failure(self) -> None:
        order = self._order()
        self.assertTrue(self.store.insert_map_order(order, 1))
        successor_at = NOW + timedelta(seconds=2)
        successor = OddsSnapshot(
            "match-1", "winner-one", "winner-group", successor_at, 1.99, 5,
            order.market,
        )
        self.store.store_odds_observation(
            source="direct",
            observation_key="successor",
            source_event_id=None,
            raybet_match_id="match-1",
            observed_at=successor_at,
            normalized_state_hash=normalized_state_hash([successor]),
            snapshots=[successor],
        )
        with patch.object(
            self.store,
            "enqueue_notification",
            side_effect=RuntimeError("outbox failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "outbox failure"):
                self.store.process_pending_successor(order, watermark=successor_at)
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status FROM shadow_orders WHERE order_key='order-1'"
            ).fetchone()),
            ("pending",),
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status FROM shadow_map_attempts WHERE order_key='order-1'"
            ).fetchone()),
            ("pending",),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM notification_outbox"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
