"""Strict, incremental ingestion for manually approved Dota 2 events."""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol, Sequence

from .scheduler import next_retry_at


MATCH_PROCESSOR_VERSION = "opendota-exact-v1"


class IngestAttempt(int):
    """Retry ordinal carrying an immutable, monotonic fencing generation."""

    generation: int

    def __new__(cls, retry_count: int, generation: int) -> "IngestAttempt":
        value = int.__new__(cls, retry_count)
        value.generation = generation
        return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("ingest timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ApprovedEvent:
    event_id: str
    league_id: int
    stage_starts_at: datetime
    stage_ends_at: datetime

    def __post_init__(self) -> None:
        start = _utc(self.stage_starts_at)
        end = _utc(self.stage_ends_at)
        if start > end:
            raise ValueError("event stage starts after it ends")
        object.__setattr__(self, "stage_starts_at", start)
        object.__setattr__(self, "stage_ends_at", end)

    def contains(self, started_at: datetime) -> bool:
        return self.stage_starts_at <= _utc(started_at) <= self.stage_ends_at

    def active_at(self, at: datetime) -> bool:
        return self.contains(at)


@dataclass(frozen=True)
class ScopeDecision:
    formal: bool
    reason: str


class MatchScopeError(ValueError):
    """A fetched detail contradicts its approved event identity or boundary."""


@dataclass(frozen=True)
class MatchProcessingResult:
    facts: object
    detail_complete: bool
    retryable: bool
    missing_reasons: tuple[str, ...] = ()
    first_usable: bool = True
    processor_version: str = MATCH_PROCESSOR_VERSION

    def __post_init__(self) -> None:
        if self.detail_complete and self.retryable:
            raise ValueError("a complete detail cannot be retryable")


@dataclass(frozen=True)
class IngestReport:
    events: int = 0
    discovered: int = 0
    candidates: int = 0
    attempted: int = 0
    completed: int = 0
    retryable: int = 0
    failed: int = 0
    unchanged: int = 0
    changed_match_ids: tuple[int, ...] = ()

    def plus(self, **changes: object) -> "IngestReport":
        values = self.__dict__.copy()
        for key, value in changes.items():
            if key == "changed_match_ids":
                values[key] = tuple(
                    sorted({*values[key], *(int(item) for item in value)})  # type: ignore[arg-type]
                )
            else:
                values[key] += int(value)  # type: ignore[operator]
        return IngestReport(**values)


@dataclass(frozen=True)
class CandidateDiscoveryReport:
    catalog_rows: int = 0
    candidates_seen: int = 0
    candidates_created: int = 0


class EventRegistryPort(Protocol):
    def approved_events(
        self, event_id: str | None = None, active_at: datetime | None = None
    ) -> Sequence[ApprovedEvent]: ...

    def classify_discovered_match(
        self, event: ApprovedEvent, summary: Mapping[str, object]
    ) -> ScopeDecision: ...


class StoredStatus(Protocol):
    event_id: str
    match_id: int
    attempt_count: int
    detail_complete: bool
    next_retry_at: datetime | None
    content_sha256: str | None
    processor_version: str | None


class IngestStorePort(Protocol):
    def record_discovered_match(
        self,
        event: ApprovedEvent,
        summary: Mapping[str, object],
        discovered_at: datetime,
        source: str,
    ) -> bool: ...

    def record_candidate_match(
        self,
        event: ApprovedEvent,
        summary: Mapping[str, object],
        reason: str,
        discovered_at: datetime,
    ) -> None: ...

    def record_event_candidate(
        self,
        summary: Mapping[str, object],
        reason: str,
        discovered_at: datetime,
    ) -> bool: ...

    def list_legacy_match_ids(self, event: ApprovedEvent) -> Sequence[int]: ...

    def get_ingest_status(self, match_id: int) -> StoredStatus | None: ...

    def begin_ingest_attempt(self, match_id: int, attempted_at: datetime) -> int: ...

    def record_ingest_success(self, **values: object) -> str | None: ...

    def record_ingest_failure(self, **values: object) -> None: ...

    def list_due_match_ids(
        self,
        now: datetime,
        event_ids: tuple[str, ...] | None = None,
        started_since: datetime | None = None,
    ) -> Sequence[int]: ...


class SourceResponse(Protocol):
    endpoint: str
    request_identity: str
    received_at: datetime
    status_code: int
    payload: object
    canonical_json: bytes
    content_sha256: str


class CompletedMatchClient(Protocol):
    async def fetch_leagues(self) -> SourceResponse: ...

    async def fetch_league_matches(self, league_id: int) -> SourceResponse: ...

    async def fetch_match(self, match_id: int) -> SourceResponse: ...


class RawArchivePort(Protocol):
    def archive_json(self, **values: object) -> object: ...


MatchProcessor = Callable[[object, int], MatchProcessingResult]


_CATALOG_ID_FLOOR = 19_000
_CATALOG_ID_CEILING = 65_000
_POTENTIAL_STRICT_EVENT = re.compile(
    r"(?i)(?:\b2026\b|\bpgl\b|dreamleague|\bblast\b|\besl\b|"
    r"esports world cup|the international|fissure|betboom dacha|"
    r"riyadh masters|games of the future|elite league|clavision)"
)


def completed_match_processing_result(
    payload: object, expected_match_id: int
) -> MatchProcessingResult:
    """Adapt exact extracted facts to the scheduler's terminal/retry decision."""
    from .facts import ComponentStatus, extract_completed_match_facts

    if not isinstance(payload, dict):
        raise TypeError("completed match response must be a JSON object")
    facts = extract_completed_match_facts(
        payload, expected_match_id=expected_match_id
    )
    assessments = tuple(vars(facts.readiness).values())
    retryable = any(
        assessment.status is ComponentStatus.RETRYABLE for assessment in assessments
    ) or facts.readiness.team_state.status is ComponentStatus.UNSCORABLE
    review_required = any(
        assessment.status is ComponentStatus.REVIEW_REQUIRED
        for assessment in assessments
    )
    missing_reasons = tuple(
        dict.fromkeys(
            reason
            for assessment in assessments
            for reason in assessment.reasons
        )
    )
    return MatchProcessingResult(
        facts=facts,
        detail_complete=not retryable and not review_required,
        retryable=retryable,
        missing_reasons=missing_reasons,
        first_usable=facts.readiness.normalization.ready,
    )


_QUERY_VALUE = re.compile(r"([?&][A-Za-z0-9_.~-]+)=([^&\s]*)")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s]+@")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_HEADER = re.compile(
    r"(?i)((?:x-api-key|auth_token|client_secret|authorization|signature|sig)\s*[:=]\s*)"
    r"[^\s,;]+"
)


def sanitize_ingest_error(error: BaseException) -> str:
    """Bound persisted errors and remove common credential forms."""
    text = _URL_USERINFO.sub(r"\1<redacted>@", str(error))
    text = _QUERY_VALUE.sub(r"\1=<redacted>", text)
    text = _BEARER.sub(r"\1<redacted>", text)
    text = _SECRET_HEADER.sub(r"\1<redacted>", text)
    return text.replace("\r", " ").replace("\n", " ")[:500]


class StrictEventIngestor:
    """Coordinates registry, source, raw archive, and normalized status writes."""

    def __init__(
        self,
        registry: EventRegistryPort,
        store: IngestStorePort,
        archive: RawArchivePort,
        client: CompletedMatchClient,
        *,
        processor: MatchProcessor,
        clock: Callable[[], datetime],
        max_concurrency: int = 3,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self._registry = registry
        self._store = store
        self._archive = archive
        self._client = client
        self._processor = processor
        self._clock = clock
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_league(self, league_id: int) -> SourceResponse:
        async with self._semaphore:
            return await self._client.fetch_league_matches(league_id)

    async def _fetch_catalog(self) -> SourceResponse:
        async with self._semaphore:
            return await self._client.fetch_leagues()

    async def _fetch_match(self, match_id: int) -> SourceResponse:
        async with self._semaphore:
            return await self._client.fetch_match(match_id)

    def _archive_response(
        self,
        response: SourceResponse,
        *,
        match_id: int | None,
        first_usable_at: datetime | None,
    ) -> None:
        self._archive.archive_json(
            source="opendota",
            endpoint=response.endpoint,
            request_identity=response.request_identity,
            payload_bytes=response.canonical_json,
            observed_at=_utc(response.received_at),
            match_id=match_id,
            source_timestamp=None,
            first_usable_at=first_usable_at,
            status_code=response.status_code,
        )

    async def _discover_event(
        self, event: ApprovedEvent, now: datetime
    ) -> tuple[set[int], set[int], int]:
        response = await self._fetch_league(event.league_id)
        self._archive_response(
            response, match_id=None, first_usable_at=_utc(response.received_at)
        )
        if not isinstance(response.payload, list):
            raise ValueError(f"league {event.league_id} response is not a list")

        new_ids: set[int] = set()
        formal_ids: set[int] = set()
        candidate_count = 0
        for item in response.payload:
            if not isinstance(item, Mapping) or "match_id" not in item:
                continue
            summary = dict(item)
            try:
                match_id = int(summary["match_id"])
            except (TypeError, ValueError):
                continue
            decision = self._registry.classify_discovered_match(event, summary)
            if not decision.formal:
                self._store.record_candidate_match(
                    event, summary, decision.reason, now
                )
                candidate_count += 1
                continue
            formal_ids.add(match_id)
            if self._store.record_discovered_match(
                event, summary, now, "opendota_league"
            ):
                new_ids.add(match_id)

        # Legacy presence is evidence to re-fetch, never evidence of completeness.
        for match_id in self._store.list_legacy_match_ids(event):
            match_id = int(match_id)
            summary = {"match_id": match_id, "leagueid": event.league_id}
            if self._store.record_discovered_match(event, summary, now, "legacy_audit"):
                new_ids.add(match_id)
        return new_ids, formal_ids, candidate_count

    async def discover_event_candidates(
        self, now: datetime
    ) -> CandidateDiscoveryReport:
        """Over-capture plausible catalog rows into the pending audit queue."""
        now = _utc(now)
        response = await self._fetch_catalog()
        self._archive_response(
            response, match_id=None, first_usable_at=_utc(response.received_at)
        )
        if not isinstance(response.payload, list):
            raise ValueError("OpenDota league catalog response is not a list")
        approved_ids = {
            int(event.league_id) for event in self._registry.approved_events()
        }
        max_approved = max(approved_ids, default=0)
        seen = created = 0
        for item in response.payload:
            if not isinstance(item, Mapping):
                continue
            try:
                league_id = int(item.get("leagueid"))
            except (TypeError, ValueError):
                continue
            name = str(item.get("name") or "").strip()
            if (
                league_id <= 0
                or league_id in approved_ids
                or league_id < _CATALOG_ID_FLOOR
                or league_id >= _CATALOG_ID_CEILING
                or (league_id <= max_approved and not _POTENTIAL_STRICT_EVENT.search(name))
            ):
                continue
            seen += 1
            created += self._store.record_event_candidate(
                dict(item), "missing_required_strict_evidence", now
            )
        return CandidateDiscoveryReport(len(response.payload), seen, created)

    async def _ingest_match(self, match_id: int, now: datetime) -> IngestReport:
        status_before = self._store.get_ingest_status(match_id)
        if status_before is None:
            return IngestReport(failed=1)

        attempt = self._store.begin_ingest_attempt(match_id, now)
        attempt_count = int(attempt)
        attempt_generation = getattr(attempt, "generation", None)
        try:
            response = await self._fetch_match(match_id)
            validator = getattr(self._store, "validate_match_payload", None)
            if validator is not None:
                decision = validator(match_id, response.payload)
                if not decision.formal:
                    self._archive_response(
                        response, match_id=match_id, first_usable_at=None
                    )
                    reject = getattr(self._store, "record_scope_rejection", None)
                    if reject is not None:
                        reject(
                            match_id,
                            response.payload,
                            decision.reason,
                            now,
                            attempt_count=attempt_count,
                            attempt_generation=attempt_generation,
                        )
                    raise MatchScopeError(decision.reason)
            processing: MatchProcessingResult | None = None
            try:
                processing = self._processor(response.payload, match_id)
            finally:
                first_usable_at = (
                    _utc(response.received_at)
                    if processing is not None and processing.first_usable
                    else None
                )
                self._archive_response(
                    response,
                    match_id=match_id,
                    first_usable_at=first_usable_at,
                )

            unchanged = (
                status_before.content_sha256 == response.content_sha256
                and getattr(
                    status_before, "processor_version", MATCH_PROCESSOR_VERSION
                )
                == processing.processor_version
            )
            retry_at = (
                next_retry_at(now, attempt_count) if processing.retryable else None
            )
            outcome = self._store.record_ingest_success(
                match_id=match_id,
                attempted_at=now,
                attempt_count=attempt_count,
                content_sha256=response.content_sha256,
                first_usable_at=(
                    _utc(response.received_at) if processing.first_usable else None
                ),
                facts=None if unchanged else processing.facts,
                payload=response.payload,
                artifact_unchanged=unchanged,
                detail_complete=processing.detail_complete,
                retryable=processing.retryable,
                missing_reasons=processing.missing_reasons,
                next_retry_at=retry_at,
                processor_version=processing.processor_version,
                attempt_generation=attempt_generation,
            )
            return IngestReport(
                attempted=1,
                completed=int(processing.detail_complete),
                retryable=int(processing.retryable),
                unchanged=int(unchanged),
                changed_match_ids=(
                    (match_id,)
                    if outcome == "normalized" or (outcome is None and not unchanged)
                    else ()
                ),
            )
        except Exception as error:
            retry_at = (
                None
                if isinstance(error, MatchScopeError)
                else next_retry_at(now, attempt_count)
            )
            self._store.record_ingest_failure(
                match_id=match_id,
                attempted_at=now,
                attempt_count=attempt_count,
                error=sanitize_ingest_error(error),
                next_retry_at=retry_at,
                attempt_generation=attempt_generation,
            )
            return IngestReport(attempted=1, failed=1, retryable=int(retry_at is not None))

    async def run_once(
        self,
        *,
        event_id: str | None = None,
        match_id: int | None = None,
        active_only: bool = False,
        reconcile: bool = False,
        recent_since: datetime | None = None,
        now: datetime | None = None,
    ) -> IngestReport:
        now = _utc(now or self._clock())
        events = list(
            self._registry.approved_events(
                event_id=event_id, active_at=now if active_only else None
            )
        )
        if event_id is not None and not events:
            raise LookupError(f"approved event not found: {event_id}")

        report = IngestReport(events=len(events))
        newly_discovered: set[int] = set()
        observed_by_event: dict[str, set[int]] = {}
        discoveries = await asyncio.gather(
            *(self._discover_event(event, now) for event in events)
        )
        for event, (new_ids, formal_ids, candidate_count) in zip(events, discoveries):
            newly_discovered.update(new_ids)
            observed_by_event[event.event_id] = formal_ids
            report = report.plus(
                discovered=len(new_ids), candidates=candidate_count
            )

        event_ids = tuple(event.event_id for event in events)
        due_ids = set(
            int(value)
            for value in self._store.list_due_match_ids(
                now,
                event_ids=event_ids or None,
                started_since=recent_since,
            )
        )
        recent_ids: set[int] = set()
        if recent_since is not None and hasattr(
            self._store, "list_recent_rescan_match_ids"
        ):
            recent = self._store.list_recent_rescan_match_ids(  # type: ignore[attr-defined]
                _utc(recent_since), now, event_ids or None
            )
            recent_ids.update(int(value) for value in recent)
            due_ids.update(recent_ids)

        selected = newly_discovered | due_ids
        if match_id is not None:
            if self._store.get_ingest_status(match_id) is None:
                raise LookupError(f"formal discovered match not found: {match_id}")
            selected = {int(match_id)}

        tasks = []
        for selected_id in sorted(selected):
            status = self._store.get_ingest_status(selected_id)
            if status is None:
                continue
            if (
                status.detail_complete
                and match_id is None
                and selected_id not in recent_ids
            ):
                continue
            tasks.append(self._ingest_match(selected_id, now))
        for result in await asyncio.gather(*tasks):
            report = report.plus(
                attempted=result.attempted,
                completed=result.completed,
                retryable=result.retryable,
                failed=result.failed,
                unchanged=result.unchanged,
                changed_match_ids=result.changed_match_ids,
            )

        if reconcile and hasattr(self._store, "reconcile_event"):
            for event in events:
                outcome = self._store.reconcile_event(  # type: ignore[attr-defined]
                    event, observed_by_event.get(event.event_id, set()), now
                )
                if inspect.isawaitable(outcome):
                    await outcome
        return report

    async def poll_active(self, now: datetime) -> IngestReport:
        return await self.run_once(active_only=True, now=now)

    async def rescan_recent(self, since: datetime, now: datetime) -> IngestReport:
        return await self.run_once(recent_since=since, now=now)
