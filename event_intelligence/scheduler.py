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
CANDIDATE_SCAN_INTERVAL = timedelta(days=1)


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

    def set_scheduler_checkpoint(self, key: str, value: datetime) -> None:
        """Persist success and atomically clear retry state for the same key."""
        ...

    def get_scheduler_retry_state(
        self, key: str
    ) -> SchedulerRetryState | None: ...

    def set_scheduler_retry_state(
        self, key: str, state: SchedulerRetryState, updated_at: datetime
    ) -> None: ...


class ScheduledIngestor(Protocol):
    def poll_active(self, now: datetime) -> Awaitable[object]: ...

    def rescan_recent(self, since: datetime, now: datetime) -> Awaitable[object]: ...

    def discover_event_candidates(self, now: datetime) -> Awaitable[object]: ...


@dataclass(frozen=True)
class SchedulerRetryState:
    failure_count: int
    next_retry_at: datetime
    last_error: str
    failed_at: datetime | None = None


@dataclass(frozen=True)
class ScheduleRun:
    active_polled: bool
    recent_rescanned: bool
    candidate_scanned: bool = False
    changed_match_ids: tuple[int, ...] = ()
    candidate_error: str | None = None
    candidate_retry_at: datetime | None = None
    candidate_error_at: datetime | None = None


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
        candidate_scanned = False
        changed_match_ids: set[int] = set()
        candidate_error = None
        candidate_retry_at = None
        candidate_error_at = None

        active_checkpoint = self._store.get_scheduler_checkpoint("active_poll")
        if include_active and self._is_due(
            active_checkpoint, now, ACTIVE_POLL_INTERVAL
        ):
            result = await self._ingestor.poll_active(now)
            changed_match_ids.update(getattr(result, "changed_match_ids", ()))
            self._store.set_scheduler_checkpoint("active_poll", now)
            active_polled = True

        recent_checkpoint = self._store.get_scheduler_checkpoint("recent_rescan")
        if include_recent and self._is_due(
            recent_checkpoint, now, RECENT_RESCAN_INTERVAL
        ):
            result = await self._ingestor.rescan_recent(now - RECENT_RESCAN_WINDOW, now)
            changed_match_ids.update(getattr(result, "changed_match_ids", ()))
            self._store.set_scheduler_checkpoint("recent_rescan", now)
            recent_rescanned = True

        candidate_checkpoint = self._store.get_scheduler_checkpoint("candidate_scan")
        candidate_retry = self._store.get_scheduler_retry_state("candidate_scan")
        if candidate_retry is not None:
            candidate_error = candidate_retry.last_error
            candidate_retry_at = _utc(candidate_retry.next_retry_at)
            candidate_error_at = (
                None
                if candidate_retry.failed_at is None
                else _utc(candidate_retry.failed_at)
            )
        retry_due = candidate_retry_at is None or candidate_retry_at <= now
        if self._is_due(
            candidate_checkpoint, now, CANDIDATE_SCAN_INTERVAL
        ) and retry_due:
            try:
                await self._ingestor.discover_event_candidates(now)
            except Exception as error:
                failure_count = (
                    1
                    if candidate_retry is None
                    else candidate_retry.failure_count + 1
                )
                retry_count = min(failure_count, len(RETRY_DELAYS))
                retry_at = next_retry_at(now, retry_count)
                assert retry_at is not None
                candidate_error = (
                    " ".join(str(error).split()) or type(error).__name__
                )[:500]
                candidate_retry_at = retry_at
                candidate_error_at = now
                self._store.set_scheduler_retry_state(
                    "candidate_scan",
                    SchedulerRetryState(
                        failure_count,
                        retry_at,
                        candidate_error,
                        now,
                    ),
                    now,
                )
            else:
                self._store.set_scheduler_checkpoint("candidate_scan", now)
                candidate_scanned = True
                candidate_error = None
                candidate_retry_at = None
                candidate_error_at = None

        return ScheduleRun(
            active_polled,
            recent_rescanned,
            candidate_scanned,
            tuple(sorted(changed_match_ids)),
            candidate_error,
            candidate_retry_at,
            candidate_error_at,
        )
