"""Deterministic timing policy for strict completed-match ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Protocol


RETRY_DELAYS = (
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(hours=72),
)
ACTIVE_POLL_INTERVAL = timedelta(minutes=15)
RECENT_RESCAN_INTERVAL = timedelta(days=1)
RECENT_RESCAN_WINDOW = timedelta(days=7)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("scheduler timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def next_retry_at(attempted_at: datetime, failure_count: int) -> datetime | None:
    """Return the next retry from the persisted one-based failure count."""
    if failure_count < 1:
        raise ValueError("failure_count must be at least one")
    if failure_count > len(RETRY_DELAYS):
        return None
    return _utc(attempted_at) + RETRY_DELAYS[failure_count - 1]


class ScheduleStore(Protocol):
    def get_scheduler_checkpoint(self, key: str) -> datetime | None: ...

    def set_scheduler_checkpoint(self, key: str, value: datetime) -> None: ...


class ScheduledIngestor(Protocol):
    def poll_active(self, now: datetime) -> Awaitable[object]: ...

    def rescan_recent(self, since: datetime, now: datetime) -> Awaitable[object]: ...


@dataclass(frozen=True)
class ScheduleRun:
    active_polled: bool
    recent_rescanned: bool


class IngestScheduler:
    """Runs due work and checkpoints only successful operations."""

    def __init__(self, ingestor: ScheduledIngestor, store: ScheduleStore) -> None:
        self._ingestor = ingestor
        self._store = store

    @staticmethod
    def _is_due(last_run: datetime | None, now: datetime, interval: timedelta) -> bool:
        return last_run is None or _utc(last_run) + interval <= now

    async def run_due(
        self,
        now: datetime,
        *,
        include_active: bool = True,
        include_recent: bool = True,
    ) -> ScheduleRun:
        now = _utc(now)
        active_polled = False
        recent_rescanned = False

        active_checkpoint = self._store.get_scheduler_checkpoint("active_poll")
        if include_active and self._is_due(
            active_checkpoint, now, ACTIVE_POLL_INTERVAL
        ):
            await self._ingestor.poll_active(now)
            self._store.set_scheduler_checkpoint("active_poll", now)
            active_polled = True

        recent_checkpoint = self._store.get_scheduler_checkpoint("recent_rescan")
        if include_recent and self._is_due(
            recent_checkpoint, now, RECENT_RESCAN_INTERVAL
        ):
            await self._ingestor.rescan_recent(now - RECENT_RESCAN_WINDOW, now)
            self._store.set_scheduler_checkpoint("recent_rescan", now)
            recent_rescanned = True

        return ScheduleRun(active_polled, recent_rescanned)
