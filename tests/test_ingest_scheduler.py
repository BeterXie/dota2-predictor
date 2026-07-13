from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from event_intelligence.scheduler import (
    ACTIVE_POLL_INTERVAL,
    RECENT_RESCAN_INTERVAL,
    RECENT_RESCAN_WINDOW,
    RETRY_DELAYS,
    IngestScheduler,
    next_retry_at,
)


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


class FakeScheduleStore:
    def __init__(self) -> None:
        self.values: dict[str, datetime] = {}

    def get_scheduler_checkpoint(self, key: str) -> datetime | None:
        return self.values.get(key)

    def set_scheduler_checkpoint(self, key: str, value: datetime) -> None:
        self.values[key] = value


class FakeIngestor:
    def __init__(self) -> None:
        self.active_calls: list[datetime] = []
        self.recent_calls: list[tuple[datetime, datetime]] = []

    async def poll_active(self, now: datetime) -> object:
        self.active_calls.append(now)
        return object()

    async def rescan_recent(self, since: datetime, now: datetime) -> object:
        self.recent_calls.append((since, now))
        return object()


class SchedulerTests(unittest.TestCase):
    def test_retry_sequence_is_exact_and_exhausts(self) -> None:
        self.assertEqual(
            RETRY_DELAYS,
            (
                timedelta(minutes=15),
                timedelta(hours=1),
                timedelta(hours=6),
                timedelta(hours=24),
                timedelta(hours=72),
            ),
        )
        attempted_at = NOW
        observed: list[datetime] = []
        for failure_count in range(1, 6):
            due = next_retry_at(attempted_at, failure_count)
            assert due is not None
            observed.append(due)
            attempted_at = due
        self.assertEqual(
            observed,
            [
                NOW + timedelta(minutes=15),
                NOW + timedelta(minutes=75),
                NOW + timedelta(hours=7, minutes=15),
                NOW + timedelta(hours=31, minutes=15),
                NOW + timedelta(hours=103, minutes=15),
            ],
        )
        self.assertIsNone(next_retry_at(attempted_at, 6))

    def test_persisted_failure_count_reproduces_retry_after_restart(self) -> None:
        first_due = next_retry_at(NOW, 3)
        restarted_due = next_retry_at(NOW, 3)
        self.assertEqual(first_due, restarted_due)

    def test_active_poll_and_recent_scan_use_persisted_checkpoints(self) -> None:
        store = FakeScheduleStore()
        first_ingestor = FakeIngestor()
        scheduler = IngestScheduler(first_ingestor, store)

        asyncio.run(scheduler.run_due(NOW))
        asyncio.run(scheduler.run_due(NOW + ACTIVE_POLL_INTERVAL - timedelta(seconds=1)))

        self.assertEqual(first_ingestor.active_calls, [NOW])
        self.assertEqual(first_ingestor.recent_calls, [(NOW - RECENT_RESCAN_WINDOW, NOW)])

        restarted_ingestor = FakeIngestor()
        restarted = IngestScheduler(restarted_ingestor, store)
        active_due = NOW + ACTIVE_POLL_INTERVAL
        asyncio.run(restarted.run_due(active_due))
        self.assertEqual(restarted_ingestor.active_calls, [active_due])
        self.assertEqual(restarted_ingestor.recent_calls, [])

        scan_due = NOW + RECENT_RESCAN_INTERVAL
        asyncio.run(restarted.run_due(scan_due))
        self.assertEqual(restarted_ingestor.recent_calls, [
            (scan_due - RECENT_RESCAN_WINDOW, scan_due)
        ])

    def test_failed_operation_does_not_advance_checkpoint(self) -> None:
        class FailingIngestor(FakeIngestor):
            async def poll_active(self, now: datetime) -> object:
                raise RuntimeError("temporary")

        store = FakeScheduleStore()
        scheduler = IngestScheduler(FailingIngestor(), store)

        with self.assertRaises(RuntimeError):
            asyncio.run(scheduler.run_due(NOW, include_recent=False))

        self.assertIsNone(store.get_scheduler_checkpoint("active_poll"))


if __name__ == "__main__":
    unittest.main()
