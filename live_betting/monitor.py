"""RayBet collection loop."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from event_intelligence.raw_archive import sanitize_request_identity

from .direct_response_audit import (
    DirectResponseContext,
    DirectResponseDecision,
    audited_direct_request,
    record_direct_request_failure,
)
from .health import record_health
from .markets import normalized_state_hash, snapshots_from_payload
from .models import utc_now
from .raybet import (
    BASE_URL,
    DOTA2_GAME_ID,
    LIVE_MATCH_TYPES,
    PREMATCH_MATCH_TYPES,
    RayBetClient,
)
from .sanitize import sanitize_raybet_payload, verified_public_stream_url
from .storage import LiveBettingStore
from .strict_eligibility import (
    RAYBET_MATCH_NON_HEAD_TO_HEAD,
    classify_raybet_match_format,
)


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
LIVE_LIST_CACHE_TTL_SECONDS = 60.0
PREMATCH_COLLECTION_LEAD_TIME = timedelta(hours=2)
SCHEDULED_LIVE_FALLBACK_RETENTION = timedelta(hours=12)
RAYBET_SCHEDULE_TIMEZONE = timezone(timedelta(hours=8))
ODDS_BACKOFF_INITIAL_SECONDS = 3.0
ODDS_BACKOFF_MAX_SECONDS = 300.0
DEFAULT_PRIORITY_ODDS_INTERVAL_SECONDS = 8.0
DEFAULT_FULL_ODDS_INTERVAL_SECONDS = 120.0
MAX_PRIORITY_ODDS_WORKERS = 4
COMPLETED_TERMINAL_REJECTION_THRESHOLD = 3
_ODDS_REQUESTS_IN_FLIGHT: set[str] = set()
_ODDS_REQUEST_LOCK = threading.Lock()
_LIVE_ROWS: tuple[dict[str, Any], ...] = ()
_LIVE_ROWS_LOCK = threading.Lock()


@contextmanager
def _claim_odds_request(match_id: str) -> Iterator[bool]:
    with _ODDS_REQUEST_LOCK:
        claimed = match_id not in _ODDS_REQUESTS_IN_FLIGHT
        if claimed:
            _ODDS_REQUESTS_IN_FLIGHT.add(match_id)
    try:
        yield claimed
    finally:
        if claimed:
            with _ODDS_REQUEST_LOCK:
                _ODDS_REQUESTS_IN_FLIGHT.discard(match_id)


def _publish_live_rows(rows: list[dict[str, Any]]) -> None:
    snapshot = tuple(dict(row) for row in rows if isinstance(row, dict))
    with _LIVE_ROWS_LOCK:
        global _LIVE_ROWS
        _LIVE_ROWS = snapshot


def _live_rows_snapshot() -> list[dict[str, Any]]:
    with _LIVE_ROWS_LOCK:
        return [dict(row) for row in _LIVE_ROWS]


@dataclass(frozen=True)
class LiveListCache:
    rows: tuple[dict[str, Any], ...]
    fetched_at_utc: datetime
    expires_at_monotonic: float

    @classmethod
    def create(
        cls,
        rows: list[dict[str, Any]],
        *,
        fetched_at_utc: datetime,
        monotonic_now: float,
    ) -> "LiveListCache":
        if fetched_at_utc.tzinfo is None or fetched_at_utc.utcoffset() is None:
            raise ValueError("live list cache fetched_at_utc must be timezone-aware")
        return cls(
            rows=tuple(dict(row) for row in rows),
            fetched_at_utc=fetched_at_utc.astimezone(timezone.utc),
            expires_at_monotonic=monotonic_now + LIVE_LIST_CACHE_TTL_SECONDS,
        )

    def current_rows(self, monotonic_now: float) -> list[dict[str, Any]] | None:
        if monotonic_now >= self.expires_at_monotonic:
            return None
        return [dict(row) for row in self.rows]


@dataclass(frozen=True)
class RequestBackoffState:
    consecutive_failures: int
    retry_not_before_monotonic: float
    last_http_status: int | None
    last_provider_code: int | None
    last_failure_reason: str


class PerRequestBackoff:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._states: dict[tuple[str, str], RequestBackoffState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(endpoint: str, request_identity: str) -> tuple[str, str]:
        return (
            sanitize_request_identity(endpoint),
            sanitize_request_identity(request_identity),
        )

    def blocked(
        self,
        endpoint: str,
        request_identity: str,
        *,
        monotonic_now: float | None = None,
    ) -> bool:
        now = self._clock() if monotonic_now is None else monotonic_now
        with self._lock:
            state = self._states.get(self.key(endpoint, request_identity))
            return state is not None and now < state.retry_not_before_monotonic

    def record_success(self, endpoint: str, request_identity: str) -> None:
        with self._lock:
            self._states.pop(self.key(endpoint, request_identity), None)

    def record_failure(
        self,
        endpoint: str,
        request_identity: str,
        error: Exception,
        *,
        monotonic_now: float | None = None,
    ) -> bool:
        key = self.key(endpoint, request_identity)
        http_status, provider_code = _error_statuses(error)
        retryable = (
            bool(getattr(error, "raybet_transport_error", False))
            or isinstance(error, (TimeoutError, ConnectionError))
            or http_status == 429
            or (http_status is not None and 500 <= http_status <= 599)
            or provider_code == 429
            or (provider_code is not None and 500 <= provider_code <= 599)
        )
        if not retryable:
            with self._lock:
                self._states.pop(key, None)
            return False
        with self._lock:
            previous = self._states.get(key)
            failures = (previous.consecutive_failures if previous else 0) + 1
            delay = min(
                ODDS_BACKOFF_MAX_SECONDS,
                ODDS_BACKOFF_INITIAL_SECONDS * (2 ** min(failures - 1, 7)),
            )
            now = self._clock() if monotonic_now is None else monotonic_now
            self._states[key] = RequestBackoffState(
                consecutive_failures=failures,
                retry_not_before_monotonic=now + delay,
                last_http_status=http_status,
                last_provider_code=provider_code,
                last_failure_reason=type(error).__name__[:100],
            )
        return True

    def retain(self, keys: set[tuple[str, str]]) -> None:
        with self._lock:
            self._states = {
                key: value for key, value in self._states.items() if key in keys
            }

    def details(self, *, monotonic_now: float | None = None) -> dict[str, Any]:
        now = self._clock() if monotonic_now is None else monotonic_now
        with self._lock:
            states = sorted(self._states.items())
        entries = []
        for (endpoint, request_identity), state in states:
            entries.append(
                {
                    "endpoint": endpoint,
                    "request_identity": request_identity,
                    "consecutive_failures": state.consecutive_failures,
                    "retry_in_seconds": max(
                        0.0, state.retry_not_before_monotonic - now
                    ),
                    "last_http_status": state.last_http_status,
                    "last_provider_code": state.last_provider_code,
                    "last_failure_reason": state.last_failure_reason,
                }
            )
        return {"active": len(entries), "entries": entries}


@dataclass
class OddsChannelRuntime:
    channel: str
    interval_seconds: float
    enabled: bool
    thread: threading.Thread | None = None
    last_cycle_completed_monotonic: float | None = None
    last_cycle_healthy: bool | None = None
    cycle_duration_seconds: float | None = None
    maximum_request_duration_seconds: float | None = None
    last_successful_match_ids: tuple[str, ...] = ()
    active_match_ids: tuple[str, ...] = ()
    _last_success_by_match: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def stale_after_seconds(self) -> float:
        return max(15.0, self.interval_seconds * 2.0)

    def attach_thread(self, thread: threading.Thread | None) -> None:
        with self._lock:
            self.thread = thread
            self.last_cycle_completed_monotonic = None
            self.last_cycle_healthy = None

    def thread_alive(self) -> bool:
        with self._lock:
            thread = self.thread
        return thread is not None and thread.is_alive()

    def needs_restart(self) -> bool:
        return self.enabled and not self.thread_alive()

    def record_cycle(
        self,
        *,
        active_match_ids: list[str],
        successful_match_ids: list[str],
        cycle_duration_seconds: float,
        maximum_request_duration_seconds: float,
        healthy: bool,
        completed_monotonic: float | None = None,
    ) -> None:
        completed = (
            time.monotonic() if completed_monotonic is None else completed_monotonic
        )
        active = tuple(dict.fromkeys(active_match_ids))
        successful = tuple(dict.fromkeys(successful_match_ids))
        with self._lock:
            self.last_cycle_completed_monotonic = completed
            self.last_cycle_healthy = healthy
            self.cycle_duration_seconds = max(0.0, cycle_duration_seconds)
            self.maximum_request_duration_seconds = max(
                0.0, maximum_request_duration_seconds
            )
            self.last_successful_match_ids = successful
            self.active_match_ids = active
            self._last_success_by_match = {
                match_id: refreshed_at
                for match_id, refreshed_at in self._last_success_by_match.items()
                if match_id in active
            }
            for match_id in successful:
                self._last_success_by_match[match_id] = completed

    def available(self, *, monotonic_now: float | None = None) -> bool:
        now = time.monotonic() if monotonic_now is None else monotonic_now
        if not self.enabled or not self.thread_alive():
            return False
        with self._lock:
            completed = self.last_cycle_completed_monotonic
            healthy = self.last_cycle_healthy
        return (
            completed is not None
            and healthy is True
            and now - completed <= self.stale_after_seconds
        )

    def details(self, *, monotonic_now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if monotonic_now is None else monotonic_now
        alive = self.thread_alive()
        with self._lock:
            completed = self.last_cycle_completed_monotonic
            healthy = self.last_cycle_healthy
            active = self.active_match_ids
            refreshed = dict(self._last_success_by_match)
            cycle_duration = self.cycle_duration_seconds
            maximum_request_duration = self.maximum_request_duration_seconds
            last_successful = self.last_successful_match_ids
        ages = [
            now - refreshed[match_id]
            for match_id in active
            if match_id in refreshed
        ]
        unrefreshed = sum(match_id not in refreshed for match_id in active)
        if not self.enabled:
            state = "disabled"
        elif not alive:
            state = "dead"
        elif completed is None:
            state = "starting"
        elif now - completed > self.stale_after_seconds:
            state = "stale"
        elif not healthy:
            state = "degraded"
        else:
            state = "healthy"
        return {
            "component": f"raybet_{self.channel}_odds_worker",
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "thread_alive": alive,
            "state": state,
            "stale_after_seconds": self.stale_after_seconds,
            "cycle_duration_seconds": cycle_duration,
            "oldest_match_refresh_age_seconds": max(ages) if ages else None,
            "maximum_request_duration_seconds": maximum_request_duration,
            "last_successful_match_ids": list(last_successful),
            "unrefreshed_match_count": unrefreshed,
        }


def _error_statuses(error: Exception) -> tuple[int | None, int | None]:
    receipt = getattr(error, "raybet_response", None)
    raw_http = getattr(receipt, "http_status", None)
    if raw_http is None:
        raw_http = getattr(error, "raybet_http_status", None)
    raw_provider = getattr(receipt, "provider_code", None)
    http_status = raw_http if type(raw_http) is int else None
    provider_code = raw_provider if type(raw_provider) is int else None
    return http_status, provider_code


def _live_list_cache_details(
    cache: LiveListCache | None,
    *,
    monotonic_now: float,
    degraded: bool,
) -> dict[str, Any]:
    if cache is None:
        return {
            "state": "empty",
            "listed": 0,
            "ttl_seconds": LIVE_LIST_CACHE_TTL_SECONDS,
        }
    expires_in = max(0.0, cache.expires_at_monotonic - monotonic_now)
    state = "expired" if expires_in == 0 else ("degraded" if degraded else "fresh")
    return {
        "state": state,
        "listed": len(cache.rows),
        "fetched_at_utc": cache.fetched_at_utc.isoformat(),
        "expires_in_seconds": expires_in,
        "ttl_seconds": LIVE_LIST_CACHE_TTL_SECONDS,
    }


def _refresh_live_list_cache(
    store: LiveBettingStore,
    client: Any,
    cache: LiveListCache | None,
    *,
    monotonic_clock: Callable[[], float],
    wall_clock: Callable[[], datetime],
) -> tuple[LiveListCache, bool]:
    try:
        rows = _fetch_match_list(store, client, response_kind="live_match_list")
    except Exception:
        if cache is None or cache.current_rows(monotonic_clock()) is None:
            raise
        return cache, True
    return (
        LiveListCache.create(
            rows,
            fetched_at_utc=wall_clock(),
            monotonic_now=monotonic_clock(),
        ),
        False,
    )


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


class RayBetDirectResponseIdentityError(ValueError):
    """A direct response does not belong to the requested Dota match."""


def _require_store_archive_root(store: LiveBettingStore, raw_dir: Path) -> None:
    if raw_dir.resolve() != store.raw_archive_root:
        raise ValueError("collector raw directory does not match store archive root")


def _audit_match_list(
    store: LiveBettingStore,
    rows: list[dict[str, Any]],
    *,
    response_kind: str,
    observed_at: datetime,
) -> None:
    receipt = store.archive_response_payload(
        {"result": rows},
        observed_at=observed_at,
        match_id=None,
        response_kind=response_kind,
    )
    store.record_direct_response_audit(
        receipt,
        response_kind=response_kind,
        claimed_raybet_match_id=None,
        observed_raybet_match_id=None,
        disposition="audit_only",
        reason="match_list_observed",
        request_metadata={"aggregate": True},
        payload_kind="aggregate",
        sanitized=True,
    )


def _dota_match_rows(rows: list[Any]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            game_id = int(row.get("game_id") or 0)
        except (TypeError, ValueError):
            continue
        match_id = str(row.get("id") or "")
        if game_id == DOTA2_GAME_ID and match_id.isdigit():
            filtered.append(row)
    return filtered


def _fetch_provider_match_pages(
    store: LiveBettingStore,
    client: RayBetClient,
    *,
    response_kind: str,
    max_pages: int = 10,
    match_types: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    match_types = match_types or (
        LIVE_MATCH_TYPES
        if response_kind == "live_match_list"
        else (4,)
    )
    combined: dict[str, dict[str, Any]] = {}
    endpoint = f"{BASE_URL}/match"
    for match_type in match_types:
        seen_for_type: set[str] = set()
        for page in range(1, max_pages + 1):
            request_identity = (
                f"{endpoint}?match_type={match_type}&page={page}"
            )

            def validate(
                context: DirectResponseContext,
            ) -> DirectResponseDecision[list[Any]]:
                result = context.sanitized_payload.get("result")
                if not isinstance(result, list):
                    raise ValueError("RayBet match page result must be a list")
                return DirectResponseDecision(
                    result,
                    disposition="audit_only",
                    reason="match_page_observed",
                )

            page_rows = audited_direct_request(
                store,
                fetch=lambda match_type=match_type, page=page: (
                    client.match_page_response(match_type, page)
                ),
                process=validate,
                response_kind=response_kind,
                claimed_raybet_match_id=None,
                endpoint=endpoint,
                request_identity=request_identity,
                request_metadata={"match_type": match_type, "page": page},
            )
            if not page_rows:
                break
            for row in _dota_match_rows(page_rows):
                match_id = str(row["id"])
                if match_id not in seen_for_type:
                    combined[match_id] = row
                    seen_for_type.add(match_id)
    rows = list(combined.values())
    if response_kind == "live_match_list":
        rows.sort(key=lambda row: str(row.get("start_time") or ""))
    return rows


def _fetch_match_list(
    store: LiveBettingStore,
    client: Any,
    *,
    response_kind: str,
    match_types: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    if callable(getattr(client, "match_page_response", None)):
        return _fetch_provider_match_pages(
            store,
            client,
            response_kind=response_kind,
            match_types=match_types,
        )
    if match_types is not None:
        matches = getattr(client, "matches", None)
        combined: dict[str, dict[str, Any]] = {}
        if callable(matches):
            for match_type in match_types:
                for row in matches(match_type=match_type):
                    if isinstance(row, dict):
                        combined[str(row.get("id") or "")] = row
        elif match_types == PREMATCH_MATCH_TYPES and callable(
            getattr(client, "live_matches", None)
        ):
            for row in client.live_matches():
                if isinstance(row, dict) and str(row.get("status") or "") == "1":
                    combined[str(row.get("id") or "")] = row
        else:
            raise TypeError("RayBet client cannot fetch selected match types")
        rows = list(combined.values())
        _audit_match_list(
            store,
            rows,
            response_kind=response_kind,
            observed_at=utc_now(),
        )
        return rows
    fetch = (
        client.live_matches
        if response_kind == "live_match_list"
        else client.completed_matches
    )
    try:
        rows = fetch()
        if not isinstance(rows, list):
            raise ValueError("RayBet match list response must be a list")
    except Exception as error:
        record_direct_request_failure(
            store,
            response_kind=response_kind,
            claimed_raybet_match_id=None,
            error=error,
            observed_at=utc_now(),
        )
        raise
    _audit_match_list(
        store,
        rows,
        response_kind=response_kind,
        observed_at=utc_now(),
    )
    return rows


def _response_match_id(payload: Any) -> str | None:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    value = str(result.get("id") or "")
    return value or None


def _rejection_reason(error: Exception) -> str:
    if isinstance(error, RayBetDirectResponseIdentityError):
        return "identity_mismatch"
    if isinstance(error, ValueError):
        return "validation_failed"
    return f"processing_failed:{type(error).__name__}"


def _fingerprint(payload: dict[str, Any]) -> str:
    payload = sanitize_raybet_payload(payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _direct_observation_key(
    match_id: str, observed_at: datetime, payload_fingerprint: str
) -> str:
    value = f"direct\n{match_id}\n{observed_at.isoformat()}\n{payload_fingerprint}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fetch_odds(
    client: Any, match_id: str
) -> Any:
    if callable(getattr(client, "match_odds_response", None)):
        return client.match_odds_response(match_id)
    return client.match_odds(match_id)


def _scheduled_start_utc(list_row: dict[str, Any]) -> datetime | None:
    raw_start = list_row.get("start_time")
    if isinstance(raw_start, datetime):
        scheduled_at = raw_start
    elif isinstance(raw_start, str):
        start_text = raw_start.strip()
        try:
            if len(start_text) == 19:
                scheduled_at = datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S")
                scheduled_at = scheduled_at.replace(tzinfo=RAYBET_SCHEDULE_TIMEZONE)
            elif len(start_text) == 25:
                scheduled_at = datetime.fromisoformat(start_text)
            else:
                return None
        except ValueError:
            return None
    else:
        return None
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        return None
    return scheduled_at.astimezone(timezone.utc)


def _scheduled_live_fallback_due(
    list_row: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    if str(list_row.get("status") or "") != "1":
        return False
    scheduled_at = _scheduled_start_utc(list_row)
    if scheduled_at is None:
        return False
    elapsed = now.astimezone(timezone.utc) - scheduled_at
    return timedelta(0) <= elapsed <= SCHEDULED_LIVE_FALLBACK_RETENTION


def _scheduled_live_fallback_rows(
    store: LiveBettingStore,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    checked_at = now or utc_now()
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("scheduled live fallback clock must be timezone-aware")
    checked_at = checked_at.astimezone(timezone.utc)
    rows = store.connection.execute(
        """SELECT raybet_match_id, scheduled_at, status
             FROM raybet_matches
            WHERE COALESCE(CAST(status AS TEXT), '') IN ('1', '2')"""
    ).fetchall()
    fallback_rows: list[dict[str, Any]] = []
    for row in rows:
        match_id = str(row["raybet_match_id"] or "")
        fallback = {
            "id": match_id,
            "status": str(row["status"] or ""),
            "start_time": str(row["scheduled_at"] or ""),
            "_force_live_poll": True,
        }
        scheduled_at = _scheduled_start_utc(fallback)
        if not match_id.isdigit() or scheduled_at is None:
            continue
        if _scheduled_live_fallback_due(fallback, now=checked_at):
            fallback_rows.append(fallback)
    return fallback_rows


def _prematch_collection_due(
    store: LiveBettingStore,
    match_id: str,
    list_row: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("prematch collection clock must be timezone-aware")
    scheduled_at = _scheduled_start_utc(list_row)
    if scheduled_at is None:
        return False
    now_utc = now.astimezone(timezone.utc)
    until_start = scheduled_at - now_utc
    if not timedelta(0) < until_start <= PREMATCH_COLLECTION_LEAD_TIME:
        return False

    window_start = scheduled_at - PREMATCH_COLLECTION_LEAD_TIME
    row = store.connection.execute(
        """SELECT 1 FROM direct_response_audit
             WHERE response_kind='live_odds'
               AND claimed_raybet_match_id=?
               AND payload_kind='provider_response'
               AND disposition='audit_only'
               AND reason='prematch_observed'
               AND live_text_timestamp_utc(observed_at)>=
                   live_text_timestamp_utc(?)
               AND live_text_timestamp_utc(observed_at)<
                   live_text_timestamp_utc(?)
             LIMIT 1""",
        (match_id, window_start.isoformat(), scheduled_at.isoformat()),
    ).fetchone()
    return row is None


def _collect_odds_response(
    store: LiveBettingStore,
    client: Any,
    *,
    match_id: str,
    response_kind: str,
    list_row: dict[str, Any],
    audit_only: bool = False,
) -> tuple[int, int, str, bool]:
    endpoint = f"{BASE_URL}/odds"
    request_identity = f"{endpoint}?match_id={match_id}"

    def normalize(
        context: DirectResponseContext,
    ) -> DirectResponseDecision[tuple[int, int, str]]:
        payload = context.sanitized_payload
        raw_result = context.payload.get("result")
        observed_at = context.observed_at
        result = payload.get("result")
        observed_match_id = _response_match_id(payload)
        if (
            not isinstance(result, dict)
            or observed_match_id != match_id
            or type(result.get("game_id")) is not int
            or int(result["game_id"]) != DOTA2_GAME_ID
        ):
            raise RayBetDirectResponseIdentityError(
                f"{response_kind} response identity mismatch"
            )
        fingerprint = _fingerprint(payload)
        if (
            response_kind == "completed_odds"
            and classify_raybet_match_format(result)
            == RAYBET_MATCH_NON_HEAD_TO_HEAD
        ):
            return DirectResponseDecision(
                (0, 0, fingerprint, True),
                disposition="audit_only",
                reason="non_head_to_head_match",
                observed_raybet_match_id=observed_match_id,
            )
        snapshots = snapshots_from_payload(payload, received_at=observed_at)
        stored_result = dict(result)
        if (
            response_kind == "completed_odds"
            and stored_result.get("status") in (None, "")
        ):
            stored_result["status"] = list_row.get("status", 3)
        public_live_url = (
            verified_public_stream_url(raw_result.get("live_url"))
            if response_kind == "live_odds" and isinstance(raw_result, dict)
            else None
        )
        with store.transaction():
            store.upsert_raybet_match(
                stored_result,
                observed_at,
                public_live_url=public_live_url,
            )
            timing_status, changes = store.store_odds_observation(
                source="direct",
                observation_key=_direct_observation_key(
                    match_id, observed_at, fingerprint
                ),
                source_event_id=None,
                raybet_match_id=match_id,
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash(snapshots),
                snapshots=snapshots,
                raw_payload=payload,
                raw_artifact=context.receipt,
                audit_only=audit_only,
            )
        if audit_only:
            return DirectResponseDecision(
                (changes, len(snapshots), fingerprint, False),
                disposition="audit_only",
                reason="prematch_observed",
                observed_raybet_match_id=observed_match_id,
            )
        return DirectResponseDecision(
            (changes, len(snapshots), fingerprint, False),
            disposition="audit_only" if timing_status == "late" else "accepted",
            reason="late_transport" if timing_status == "late" else "normalized",
            observed_raybet_match_id=observed_match_id,
        )

    return audited_direct_request(
        store,
        fetch=lambda: _fetch_odds(client, match_id),
        process=normalize,
        response_kind=response_kind,
        claimed_raybet_match_id=match_id,
        endpoint=endpoint,
        request_identity=request_identity,
        request_metadata={"operation": response_kind},
        clock=utc_now,
        rejection_reason=_rejection_reason,
    )


def completed_refresh_due(
    completed_rows: list[dict[str, Any]] | None,
    monotonic_now: float,
    next_refresh: float,
) -> bool:
    """Return whether the low-frequency completed feed should be fetched."""
    return completed_rows is None or monotonic_now >= next_refresh


def _active_priority_match_ids(store: LiveBettingStore) -> set[str]:
    rows = store.connection.execute(
        """SELECT DISTINCT mapping.raybet_match_id
             FROM strict_live_map_mappings AS mapping
             JOIN raybet_matches AS match
               ON match.raybet_match_id=mapping.raybet_match_id
            WHERE COALESCE(CAST(match.status AS TEXT), '')!='3'
              AND NOT EXISTS (
                  SELECT 1
                    FROM strict_live_map_mapping_invalidations AS invalidation
                   WHERE invalidation.mapping_id=mapping.mapping_id
              )
              AND NOT EXISTS (
                  SELECT 1
                    FROM strict_live_map_mapping_supersessions AS supersession
                   WHERE supersession.previous_mapping_id=mapping.mapping_id
              )
            ORDER BY mapping.raybet_match_id"""
    ).fetchall()
    return {
        str(row[0])
        for row in rows
        if str(row[0] or "").isdigit()
    }


def _partition_live_rows(
    list_rows: list[dict[str, Any]],
    priority_match_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    priority: list[dict[str, Any]] = []
    full: list[dict[str, Any]] = []
    for row in list_rows:
        match_id = str(row.get("id") or "") if isinstance(row, dict) else ""
        if (
            match_id in priority_match_ids
            and (
                str(row.get("status") or "") == "2"
                or row.get("_force_live_poll") is True
            )
        ):
            priority.append(row)
        else:
            full.append(row)
    return priority, full


def _odds_channel_rows(
    store: LiveBettingStore,
    channel: str,
    *,
    priority_available: bool = True,
) -> tuple[list[dict[str, Any]], int, int]:
    live_rows = list(_live_rows_snapshot())
    checked_at = utc_now()
    for index, row in enumerate(live_rows):
        if _scheduled_live_fallback_due(row, now=checked_at):
            live_rows[index] = {
                **row,
                "_force_live_poll": True,
            }
    positions = {
        str(row.get("id") or ""): index
        for index, row in enumerate(live_rows)
        if isinstance(row, dict) and str(row.get("id") or "").isdigit()
    }
    for fallback in _scheduled_live_fallback_rows(store):
        match_id = str(fallback["id"])
        position = positions.get(match_id)
        if position is None:
            positions[match_id] = len(live_rows)
            live_rows.append(fallback)
            continue
        live_rows[position] = {
            **live_rows[position],
            "_force_live_poll": True,
        }
    priority, full = _partition_live_rows(
        live_rows,
        _active_priority_match_ids(store),
    )
    if channel == "priority":
        return priority, len(live_rows), 0
    if channel == "full":
        if not priority_available:
            return live_rows, len(live_rows), 0
        return full, len(live_rows), len(priority)
    raise ValueError("unknown odds collection channel")


def _empty_odds_summary(*, listed: int) -> dict[str, Any]:
    return {
        "matches": 0,
        "listed": listed,
        "odds": 0,
        "changed": 0,
        "errors": 0,
        "backoff_skipped": 0,
        "in_flight_skipped": 0,
        "prematch_collected": 0,
        "prematch_skipped": 0,
        "required_match_ids": [],
        "last_successful_match_ids": [],
        "maximum_request_duration_seconds": 0.0,
    }


def _odds_collection_cycle_healthy(summary: dict[str, Any]) -> bool:
    return (
        int(summary["errors"]) + int(summary["backoff_skipped"]) == 0
        and int(summary["matches"]) == len(summary["required_match_ids"])
    )


def _odds_channel_poll_interval(
    interval: float,
    channel: str,
    priority_interval: float,
) -> float:
    if channel != "full":
        return interval
    fallback_interval = (
        priority_interval
        if priority_interval > 0
        else DEFAULT_PRIORITY_ODDS_INTERVAL_SECONDS
    )
    return min(interval, fallback_interval)


def _collect_priority_row(
    database_url: str,
    raw_dir: Path,
    list_row: dict[str, Any],
    backoff: PerRequestBackoff,
) -> dict[str, Any]:
    with LiveBettingStore(
        database_url, raw_archive_root=raw_dir
    ) as store, RayBetClient() as client:
        return collect_once(
            store,
            client,
            raw_dir,
            list_rows=[list_row],
            audit_match_list=False,
            backoff=backoff,
            collector_name="raybet_priority_odds",
            retain_backoff=False,
        )


def _collect_priority_rows(
    database_url: str,
    raw_dir: Path,
    rows: list[dict[str, Any]],
    backoff: PerRequestBackoff,
    *,
    max_workers: int = MAX_PRIORITY_ODDS_WORKERS,
) -> dict[str, Any]:
    summary = _empty_odds_summary(listed=len(rows))
    endpoint = f"{BASE_URL}/odds"
    active_keys = {
        backoff.key(endpoint, f"{endpoint}?match_id={str(row.get('id') or '')}")
        for row in rows
        if isinstance(row, dict) and str(row.get("id") or "").isdigit()
    }
    backoff.retain(active_keys)
    if not rows:
        return summary

    workers = max(1, min(max_workers, len(rows)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _collect_priority_row,
                database_url,
                raw_dir,
                row,
                backoff,
            )
            for row in rows
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                summary["errors"] += 1
                logger.warning(
                    "priority RayBet odds worker failed (%s)",
                    type(error).__name__,
                )
                continue
            for key in (
                "matches",
                "odds",
                "changed",
                "errors",
                "backoff_skipped",
                "in_flight_skipped",
                "prematch_collected",
                "prematch_skipped",
            ):
                summary[key] += int(result[key])
            summary["last_successful_match_ids"].extend(
                result["last_successful_match_ids"]
            )
            summary["required_match_ids"].extend(
                result.get("required_match_ids", [])
            )
            summary["maximum_request_duration_seconds"] = max(
                summary["maximum_request_duration_seconds"],
                float(result["maximum_request_duration_seconds"]),
            )
    summary["last_successful_match_ids"].sort()
    summary["required_match_ids"].sort()
    return summary


def _odds_channel_loop(
    database_url: str,
    raw_dir: Path,
    interval: float,
    channel: str,
    runtime: OddsChannelRuntime,
    priority_runtime: OddsChannelRuntime,
) -> None:
    component = f"raybet_{channel}_odds_worker"
    collector = f"raybet_{channel}_odds"
    backoff = PerRequestBackoff()
    with LiveBettingStore(
        database_url, raw_archive_root=raw_dir
    ) as store, RayBetClient() as client:
        next_regular_cycle = 0.0
        while True:
            started = time.monotonic()
            summary = _empty_odds_summary(listed=0)
            source_listed = 0
            priority_excluded = 0
            priority_available = True
            rows: list[dict[str, Any]] = []
            try:
                if channel == "full":
                    priority_available = priority_runtime.available()
                    if priority_available and started < next_regular_cycle:
                        priority_poll_interval = _odds_channel_poll_interval(
                            interval,
                            channel,
                            priority_runtime.interval_seconds,
                        )
                        time.sleep(
                            min(
                                priority_poll_interval,
                                next_regular_cycle - started,
                            )
                        )
                        continue
                rows, source_listed, priority_excluded = _odds_channel_rows(
                    store,
                    channel,
                    priority_available=priority_available,
                )
                if channel == "priority":
                    summary = _collect_priority_rows(
                        database_url,
                        raw_dir,
                        rows,
                        backoff,
                    )
                else:
                    summary = collect_once(
                        store,
                        client,
                        raw_dir,
                        list_rows=rows,
                        audit_match_list=False,
                        backoff=backoff,
                        collector_name=collector,
                    )
                cycle_duration = time.monotonic() - started
                cycle_healthy = _odds_collection_cycle_healthy(summary)
                runtime.record_cycle(
                    active_match_ids=summary["required_match_ids"],
                    successful_match_ids=summary["last_successful_match_ids"],
                    cycle_duration_seconds=cycle_duration,
                    maximum_request_duration_seconds=summary[
                        "maximum_request_duration_seconds"
                    ],
                    healthy=cycle_healthy,
                )
                if channel == "full" and priority_available:
                    next_regular_cycle = started + interval
                now = utc_now()
                partial_errors = summary["errors"] + summary["backoff_skipped"]
                record_health(
                    store.connection,
                    component,
                    "healthy" if cycle_healthy else "degraded",
                    heartbeat_at=now,
                    success_at=now if cycle_healthy else None,
                    error_at=now if not cycle_healthy else None,
                    error=(
                        f"{partial_errors} {channel} odds collection error(s)"
                        if partial_errors
                        else (
                            f"incomplete {channel} odds collection cycle"
                            if not cycle_healthy
                            else None
                        )
                    ),
                    details={
                        "source": "worker",
                        "channel": channel,
                        "interval_seconds": interval,
                        "source_listed": source_listed,
                        "priority_excluded": priority_excluded,
                        "priority_fallback": (
                            channel == "full" and not priority_available
                        ),
                        "has_channel_match": bool(summary["listed"]),
                        **runtime.details(),
                        **summary,
                        "odds_backoff": backoff.details(),
                    },
                )
            except Exception as error:
                cycle_duration = time.monotonic() - started
                runtime.record_cycle(
                    active_match_ids=[
                        str(row.get("id") or "")
                        for row in rows
                        if isinstance(row, dict)
                        and str(row.get("id") or "").isdigit()
                    ],
                    successful_match_ids=[],
                    cycle_duration_seconds=cycle_duration,
                    maximum_request_duration_seconds=0.0,
                    healthy=False,
                )
                now = utc_now()
                record_health(
                    store.connection,
                    component,
                    "degraded",
                    heartbeat_at=now,
                    error_at=now,
                    error=type(error).__name__,
                    details={
                        "source": "worker",
                        "channel": channel,
                        "interval_seconds": interval,
                        "source_listed": source_listed,
                        "priority_excluded": priority_excluded,
                        "priority_fallback": (
                            channel == "full" and not priority_available
                        ),
                        "has_channel_match": False,
                        **runtime.details(),
                        **summary,
                        "odds_backoff": backoff.details(),
                    },
                )
                logger.warning(
                    "%s RayBet odds refresh failed (%s)",
                    channel,
                    type(error).__name__,
                )
            elapsed = time.monotonic() - started
            effective_interval = _odds_channel_poll_interval(
                interval,
                channel,
                priority_runtime.interval_seconds,
            )
            delay = (
                effective_interval
                if source_listed
                else min(effective_interval, 1.0)
            )
            time.sleep(max(0.0, delay - elapsed))


def _validate_odds_intervals(
    priority_interval: float,
    full_interval: float,
    *,
    once: bool,
) -> None:
    if not once and priority_interval <= 0 and full_interval <= 0:
        raise ValueError("at least one RayBet odds channel must be enabled")


def _start_odds_channel_thread(
    database_url: str,
    raw_dir: Path,
    runtime: OddsChannelRuntime,
    priority_runtime: OddsChannelRuntime,
) -> None:
    thread = threading.Thread(
        target=_odds_channel_loop,
        args=(
            database_url,
            raw_dir,
            runtime.interval_seconds,
            runtime.channel,
            runtime,
            priority_runtime,
        ),
        name=f"raybet-{runtime.channel}-odds",
        daemon=True,
    )
    runtime.attach_thread(thread)
    try:
        thread.start()
    except Exception:
        runtime.attach_thread(None)
        raise


def collect_once(
    store: LiveBettingStore,
    client: RayBetClient,
    raw_dir: Path,
    list_rows: list[dict[str, Any]] | None = None,
    raw_fingerprints: dict[str, str] | None = None,
    audit_match_list: bool = True,
    backoff: PerRequestBackoff | None = None,
    monotonic_now: float | None = None,
    wall_clock: Callable[[], datetime] = utc_now,
    collector_name: str = "raybet",
    retain_backoff: bool = True,
) -> dict[str, Any]:
    _require_store_archive_root(store, raw_dir)
    if list_rows is None:
        list_rows = _fetch_match_list(
            store,
            client,
            response_kind="live_match_list",
        )
        audit_match_list = False
    odds_count = 0
    changed_count = 0
    error_count = 0
    success_count = 0
    backoff_skipped = 0
    in_flight_skipped = 0
    prematch_collected = 0
    prematch_skipped = 0
    required_match_ids: list[str] = []
    successful_match_ids: list[str] = []
    request_durations: list[float] = []
    collection_now = wall_clock()
    raw_fingerprints = raw_fingerprints if raw_fingerprints is not None else {}
    if audit_match_list:
        _audit_match_list(
            store,
            list_rows,
            response_kind="live_match_list",
            observed_at=utc_now(),
        )
    endpoint = f"{BASE_URL}/odds"
    active_keys = {
        backoff.key(endpoint, f"{endpoint}?match_id={str(row.get('id') or '')}")
        for row in list_rows
        if backoff is not None
        and isinstance(row, dict)
        and str(row.get("id") or "").isdigit()
    }
    if backoff is not None and retain_backoff:
        backoff.retain(active_keys)
    for list_row in list_rows:
        match_id = ""
        request_identity = ""
        is_prematch = False
        request_started = time.monotonic()
        try:
            if not isinstance(list_row, dict):
                raise ValueError("live match list row is not an object")
            match_id = str(list_row.get("id") or "")
            if not match_id.isdigit():
                raise ValueError("live match list id is invalid")
            request_identity = f"{endpoint}?match_id={match_id}"
            is_prematch = (
                str(list_row.get("status") or "") == "1"
                and list_row.get("_force_live_poll") is not True
            )
            if is_prematch and not _prematch_collection_due(
                store, match_id, list_row, now=collection_now
            ):
                prematch_skipped += 1
                continue
            if backoff is not None and backoff.blocked(
                endpoint, request_identity, monotonic_now=monotonic_now
            ):
                backoff_skipped += 1
                continue
            with _claim_odds_request(match_id) as claimed:
                if not claimed:
                    in_flight_skipped += 1
                    continue
                required_match_ids.append(match_id)
                changes, snapshot_count, fingerprint, _ = _collect_odds_response(
                    store,
                    client,
                    match_id=match_id,
                    response_kind="live_odds",
                    list_row=list_row,
                    audit_only=is_prematch,
                )
            raw_fingerprints[match_id] = fingerprint
            changed_count += changes
            odds_count += snapshot_count
            success_count += 1
            successful_match_ids.append(match_id)
            prematch_collected += int(is_prematch)
            if backoff is not None:
                backoff.record_success(endpoint, request_identity)
        except Exception as error:
            error_count += 1
            if backoff is not None and request_identity and not is_prematch:
                backoff.record_failure(
                    endpoint,
                    request_identity,
                    error,
                    monotonic_now=monotonic_now,
                )
            logger.warning(
                "RayBet fetch failed for live match_id=%s (%s)",
                match_id or "<invalid>",
                type(error).__name__,
            )
        finally:
            if request_identity:
                request_durations.append(time.monotonic() - request_started)
    completed_at = utc_now()
    if error_count or backoff_skipped:
        store.record_collector(
            collector_name,
            success_at=completed_at if success_count else None,
            error_at=completed_at,
            error=(
                f"{error_count} live match(s) failed; "
                f"{backoff_skipped} in backoff"
            ),
        )
    else:
        store.record_collector(collector_name, success_at=completed_at)
    return {
        "matches": success_count,
        "listed": len(list_rows),
        "odds": odds_count,
        "changed": changed_count,
        "errors": error_count,
        "backoff_skipped": backoff_skipped,
        "in_flight_skipped": in_flight_skipped,
        "prematch_collected": prematch_collected,
        "prematch_skipped": prematch_skipped,
        "required_match_ids": required_match_ids,
        "last_successful_match_ids": successful_match_ids,
        "maximum_request_duration_seconds": max(request_durations, default=0.0),
    }


def collect_completed_once(
    store: LiveBettingStore,
    client: RayBetClient,
    raw_dir: Path,
    completed_rows: list[dict[str, Any]] | None = None,
    audit_match_list: bool = True,
) -> dict[str, int]:
    """Archive final odds for the low-frequency RayBet completed feed.

    RayBet exposes completed matches through ``match_type=4``.  They are
    deliberately collected outside ``collect_once`` so the live 3-second loop
    does not repeatedly enumerate and fetch the whole historical list.  A
    completed response is still stored as an ordinary immutable transport
    observation; its provider row carries status ``3`` and is therefore
    available to post-match reconciliation and the historical UI.
    """
    _require_store_archive_root(store, raw_dir)
    rows = (
        completed_rows
        if completed_rows is not None
        else _fetch_match_list(
            store,
            client,
            response_kind="completed_match_list",
        )
    )
    odds_count = 0
    changed_count = 0
    error_count = 0
    skipped_count = 0
    if completed_rows is not None and audit_match_list:
        _audit_match_list(
            store,
            rows,
            response_kind="completed_match_list",
            observed_at=utc_now(),
        )
    fetched_matches = 0
    for list_row in rows:
        match_id = ""
        try:
            if not isinstance(list_row, dict):
                raise ValueError("completed match list row is not an object")
            match_id = str(list_row.get("id") or "")
            if not match_id.isdigit():
                raise ValueError("completed match list id is invalid")
            _mark_match_seen_in_completed_feed(store, match_id)
            if _completed_odds_archived(store, match_id):
                skipped_count += 1
                continue
            changes, snapshot_count, _, skipped = _collect_odds_response(
                store,
                client,
                match_id=match_id,
                response_kind="completed_odds",
                list_row=list_row,
            )
            changed_count += changes
            odds_count += snapshot_count
            if skipped:
                skipped_count += 1
                continue
            fetched_matches += 1
        except Exception as error:
            error_count += 1
            logger.warning(
                "completed RayBet fetch failed for match_id=%s (%s)",
                match_id,
                type(error).__name__,
            )
    completed_at = utc_now()
    if error_count:
        store.record_collector(
            "raybet_completed",
            success_at=completed_at if fetched_matches else None,
            error_at=completed_at,
            error=f"{error_count} completed match(s) failed",
        )
    else:
        store.record_collector("raybet_completed", success_at=completed_at)
    return {
        "matches": fetched_matches,
        "listed": len(rows),
        "odds": odds_count,
        "changed": changed_count,
        "errors": error_count,
        "skipped": skipped_count,
    }


def _mark_match_seen_in_completed_feed(
    store: LiveBettingStore,
    match_id: str,
) -> None:
    with store.transaction():
        store.connection.execute(
            """UPDATE raybet_matches SET status='3'
                WHERE raybet_match_id=? AND status<>'3'""",
            (match_id,),
        )


def _completed_odds_archived(store: LiveBettingStore, match_id: str) -> bool:
    row = store.connection.execute(
        """SELECT 1
             FROM raybet_matches AS match
            WHERE match.raybet_match_id=?
              AND match.status='3'
              AND (
                  EXISTS (
                    SELECT 1
                      FROM direct_response_audit AS audit
                      JOIN odds_transport_observations AS transport
                        ON transport.raybet_match_id=
                           audit.claimed_raybet_match_id
                       AND transport.response_artifact_hash=audit.artifact_hash
                     WHERE audit.claimed_raybet_match_id=
                           match.raybet_match_id
                       AND audit.response_kind='completed_odds'
                       AND audit.disposition='accepted'
                       AND transport.source='direct'
                       AND transport.timing_status='on_time'
                       AND transport.processing_status='processed'
                       AND EXISTS (
                             SELECT 1
                               FROM odds_response_outcomes_effective AS outcome
                              WHERE outcome.observation_key=
                                    transport.observation_key
                                AND outcome.raybet_match_id=
                                    match.raybet_match_id
                       )
                  )
                  OR EXISTS (
                      SELECT audit.artifact_hash
                        FROM direct_response_audit AS audit
                       WHERE audit.claimed_raybet_match_id=
                             match.raybet_match_id
                         AND audit.response_kind='completed_odds'
                         AND audit.disposition='rejected'
                         AND audit.reason='validation_failed'
                       GROUP BY audit.artifact_hash
                      HAVING COUNT(*)>=?
                  )
              )
            LIMIT 1""",
        (match_id, COMPLETED_TERMINAL_REJECTION_THRESHOLD),
    ).fetchone()
    return row is not None


def resolve_data_paths(args: argparse.Namespace) -> argparse.Namespace:
    args.raw_dir = (
        Path(args.raw_dir).resolve()
        if args.raw_dir is not None
        else ROOT / "data" / "live_betting" / "raw-v2"
    )
    return args


def run(args: argparse.Namespace) -> int:
    load_dotenv()
    args = resolve_data_paths(args)
    raw_dir = args.raw_dir

    with LiveBettingStore(
        args.database_url, raw_archive_root=raw_dir
    ) as store, RayBetClient() as client:
        if not getattr(args, "schema_prepared", False):
            store.init_schema()
        priority_interval = float(
            getattr(
                args,
                "priority_odds_interval",
                DEFAULT_PRIORITY_ODDS_INTERVAL_SECONDS,
            )
        )
        full_interval = float(
            getattr(
                args,
                "full_odds_interval",
                DEFAULT_FULL_ODDS_INTERVAL_SECONDS,
            )
        )
        _validate_odds_intervals(
            priority_interval,
            full_interval,
            once=bool(args.once),
        )
        odds_runtimes = {
            "priority": OddsChannelRuntime(
                "priority",
                priority_interval,
                priority_interval > 0,
            ),
            "full": OddsChannelRuntime(
                "full",
                full_interval,
                full_interval > 0,
            ),
        }
        started_at = utc_now()
        record_health(
            store.connection,
            "raybet_worker",
            "starting",
            heartbeat_at=started_at,
            details={"source": "worker"},
        )
        failures = 0
        live_list_cache: LiveListCache | None = None
        live_list_degraded = False
        raw_fingerprints: dict[str, str] = {}
        odds_backoff = PerRequestBackoff()
        next_list_refresh = 0.0
        completed_rows: list[dict[str, Any]] | None = None
        next_completed_refresh = 0.0
        prematch_rows: list[dict[str, Any]] | None = None
        next_prematch_refresh = 0.0
        while True:
            odds_worker_restarts = 0
            odds_thread_start_errors = 0
            try:
                monotonic_now = time.monotonic()
                cached_rows = (
                    live_list_cache.current_rows(monotonic_now)
                    if live_list_cache is not None
                    else None
                )
                if cached_rows is None or monotonic_now >= next_list_refresh:
                    live_list_cache, live_list_degraded = _refresh_live_list_cache(
                        store,
                        client,
                        live_list_cache,
                        monotonic_clock=time.monotonic,
                        wall_clock=utc_now,
                    )
                    monotonic_now = time.monotonic()
                    next_list_refresh = monotonic_now + args.list_interval
                    cached_rows = live_list_cache.current_rows(monotonic_now)
                if cached_rows is None:
                    raise RuntimeError("live match list cache unavailable")
                _publish_live_rows(cached_rows)
                if not args.once:
                    for runtime in odds_runtimes.values():
                        if not runtime.needs_restart():
                            continue
                        restarting = runtime.thread is not None
                        try:
                            _start_odds_channel_thread(
                                args.database_url,
                                raw_dir,
                                runtime,
                                odds_runtimes["priority"],
                            )
                        except Exception as error:
                            odds_thread_start_errors += 1
                            logger.warning(
                                "%s RayBet odds thread failed to start (%s)",
                                runtime.channel,
                                type(error).__name__,
                            )
                        else:
                            odds_worker_restarts += int(restarting)
                completed_refresh_needed = False
                completed_summary = {
                    "matches": 0,
                    "listed": len(completed_rows or []),
                    "odds": 0,
                    "changed": 0,
                    "errors": 0,
                    "skipped": 0,
                }
                if completed_refresh_due(
                    completed_rows, monotonic_now, next_completed_refresh
                ):
                    next_completed_refresh = (
                        monotonic_now + args.completed_interval
                    )
                    try:
                        completed_rows = _fetch_match_list(
                            store,
                            client,
                            response_kind="completed_match_list",
                        )
                    except Exception as error:
                        completed_rows = completed_rows or []
                        completed_summary["errors"] = 1
                        completed_at = utc_now()
                        store.record_collector(
                            "raybet_completed",
                            error_at=completed_at,
                            error=f"completed list failed: {type(error).__name__}",
                        )
                        logger.warning(
                            "completed RayBet list refresh failed (%s)",
                            type(error).__name__,
                        )
                    else:
                        completed_refresh_needed = True
                summary = {
                    "matches": 0,
                    "listed": len(cached_rows),
                    "odds": 0,
                    "changed": 0,
                    "errors": 0,
                    "backoff_skipped": 0,
                    "in_flight_skipped": 0,
                    "prematch_collected": 0,
                    "prematch_skipped": 0,
                    "delegated": 0,
                }
                if args.once:
                    summary = collect_once(
                        store,
                        client,
                        raw_dir,
                        list_rows=cached_rows,
                        raw_fingerprints=raw_fingerprints,
                        audit_match_list=False,
                        backoff=odds_backoff,
                    )
                else:
                    summary["delegated"] = len(cached_rows)
                prematch_summary = {
                    "matches": 0,
                    "listed": len(prematch_rows or []),
                    "odds": 0,
                    "changed": 0,
                    "errors": 0,
                    "backoff_skipped": 0,
                    "prematch_collected": 0,
                    "prematch_skipped": 0,
                }
                if (
                    prematch_rows is None
                    or monotonic_now >= next_prematch_refresh
                ):
                    prematch_interval = float(
                        getattr(args, "prematch_interval", 60.0)
                    )
                    next_prematch_refresh = time.monotonic() + prematch_interval
                    try:
                        discovered_prematch = _fetch_match_list(
                            store,
                            client,
                            response_kind="live_match_list",
                            match_types=PREMATCH_MATCH_TYPES,
                        )
                        live_ids = {
                            str(row.get("id") or "")
                            for row in cached_rows
                            if isinstance(row, dict)
                        }
                        prematch_rows = [
                            row
                            for row in discovered_prematch
                            if str(row.get("id") or "") not in live_ids
                        ]
                        prematch_summary = collect_once(
                            store,
                            client,
                            raw_dir,
                            list_rows=prematch_rows,
                            raw_fingerprints=raw_fingerprints,
                            audit_match_list=False,
                        )
                    except Exception as error:
                        next_prematch_refresh = (
                            time.monotonic()
                            + min(prematch_interval, 60.0)
                        )
                        prematch_summary["errors"] = 1
                        logger.warning(
                            "prematch RayBet refresh failed (%s)",
                            type(error).__name__,
                        )
                if completed_refresh_needed:
                    try:
                        completed_summary = collect_completed_once(
                            store,
                            client,
                            raw_dir,
                            completed_rows=completed_rows,
                            audit_match_list=False,
                        )
                    except Exception as error:
                        completed_summary["errors"] = 1
                        completed_at = utc_now()
                        store.record_collector(
                            "raybet_completed",
                            error_at=completed_at,
                            error=f"completed collection failed: {type(error).__name__}",
                        )
                        logger.warning(
                            "completed RayBet collection failed (%s)",
                            type(error).__name__,
                        )
                failures = 0
                succeeded_at = utc_now()
                health_monotonic_now = time.monotonic()
                partial_errors = (
                    summary["errors"]
                    + summary["backoff_skipped"]
                    + prematch_summary["errors"]
                    + completed_summary["errors"]
                    + int(live_list_degraded)
                )
                priority_fallback_active = (
                    not args.once
                    and odds_runtimes["full"].enabled
                    and not odds_runtimes["priority"].available(
                        monotonic_now=health_monotonic_now
                    )
                )
                odds_runtime_details = {
                    channel: runtime.details(
                        monotonic_now=health_monotonic_now
                    )
                    for channel, runtime in odds_runtimes.items()
                }
                odds_runtime_degraded = (
                    odds_thread_start_errors > 0
                    or odds_worker_restarts > 0
                    or priority_fallback_active
                    or any(
                        details["state"] in {"dead", "stale", "degraded"}
                        for details in odds_runtime_details.values()
                    )
                )
                partial_errors += int(odds_runtime_degraded)
                worker_status = "degraded" if partial_errors else "healthy"
                collection_succeeded = any(
                    (
                        item["matches"] > 0
                        or item.get("delegated", 0) > 0
                        or item.get("skipped", 0) > 0
                        or item.get("in_flight_skipped", 0) > 0
                        or item.get("prematch_skipped", 0) > 0
                        or (item["listed"] == 0 and item["errors"] == 0)
                    )
                    for item in (summary, prematch_summary, completed_summary)
                )
                record_health(
                    store.connection,
                    "raybet_worker",
                    worker_status,
                    heartbeat_at=succeeded_at,
                    success_at=succeeded_at if collection_succeeded else None,
                    error_at=succeeded_at if partial_errors else None,
                    error=(
                        f"{partial_errors} match collection error(s)"
                        if partial_errors
                        else None
                    ),
                    details={
                        "source": "worker",
                        **summary,
                        "full_odds": odds_runtime_details["full"],
                        "priority_odds": odds_runtime_details["priority"],
                        "priority_fallback_active": priority_fallback_active,
                        "odds_worker_restarts": odds_worker_restarts,
                        "odds_thread_start_errors": odds_thread_start_errors,
                        "prematch": prematch_summary,
                        "completed": completed_summary,
                        "live_list_cache": _live_list_cache_details(
                            live_list_cache,
                            monotonic_now=health_monotonic_now,
                            degraded=live_list_degraded,
                        ),
                        "odds_backoff": odds_backoff.details(
                            monotonic_now=health_monotonic_now
                        ),
                    },
                )
                logger.info(
                    "collected matches=%d odds=%d changed=%d prematch=%d completed=%d",
                    summary["matches"], summary["odds"], summary["changed"],
                    prematch_summary["matches"],
                    completed_summary["matches"],
                )
            except Exception as exc:
                failures += 1
                now = utc_now()
                error_monotonic_now = time.monotonic()
                store.record_collector("raybet", error_at=now, error=f"{type(exc).__name__}: {exc}")
                record_health(
                    store.connection,
                    "raybet_worker",
                    "degraded",
                    heartbeat_at=now,
                    error_at=now,
                    error=type(exc).__name__,
                    details={
                        "source": "worker",
                        "consecutive_failures": failures,
                        "full_odds": odds_runtimes["full"].details(
                            monotonic_now=error_monotonic_now
                        ),
                        "priority_odds": odds_runtimes["priority"].details(
                            monotonic_now=error_monotonic_now
                        ),
                        "live_list_cache": _live_list_cache_details(
                            live_list_cache,
                            monotonic_now=error_monotonic_now,
                            degraded=True,
                        ),
                        "odds_backoff": odds_backoff.details(),
                    },
                )
                logger.error("RayBet collection failed: %s", exc)
                if args.once:
                    return 1
            if args.once:
                return 0
            delay = min(args.max_backoff, args.interval * (2 ** min(failures, 5)))
            time.sleep(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
    )
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--list-interval", type=float, default=15.0)
    parser.add_argument(
        "--priority-odds-interval",
        type=float,
        default=DEFAULT_PRIORITY_ODDS_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--full-odds-interval",
        type=float,
        default=DEFAULT_FULL_ODDS_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--prematch-interval",
        type=float,
        default=60.0,
        help="seconds between schedule discovery refreshes; odds are captured once inside T-2h",
    )
    parser.add_argument("--completed-interval", type=float, default=300.0)
    parser.add_argument("--max-backoff", type=float, default=300.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return resolve_data_paths(parser.parse_args())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    arguments = parse_args()
    raise SystemExit(run(arguments))
