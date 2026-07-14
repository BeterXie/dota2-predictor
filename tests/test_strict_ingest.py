from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from event_intelligence.ingest import (
    ApprovedEvent,
    MatchProcessingResult,
    ScopeDecision,
    StrictEventIngestor,
    sanitize_ingest_error,
)


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FakeResponse:
    endpoint: str
    request_identity: str
    received_at: datetime
    status_code: int
    payload: object

    @property
    def canonical_json(self) -> bytes:
        return json.dumps(
            self.payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json).hexdigest()


class FakeRegistry:
    def __init__(self, events: list[ApprovedEvent]) -> None:
        self.events = events

    def approved_events(
        self, event_id: str | None = None, active_at: datetime | None = None
    ) -> list[ApprovedEvent]:
        events = [event for event in self.events if event_id in (None, event.event_id)]
        if active_at is not None:
            events = [event for event in events if event.active_at(active_at)]
        return events

    def classify_discovered_match(
        self, event: ApprovedEvent, summary: dict
    ) -> ScopeDecision:
        if int(summary.get("leagueid", -1)) != event.league_id:
            return ScopeDecision(False, "league_mismatch")
        started = datetime.fromtimestamp(int(summary["start_time"]), timezone.utc)
        if not event.contains(started):
            return ScopeDecision(False, "outside_stage_boundaries")
        return ScopeDecision(True, "approved_registry_scope")


class FakeClient:
    def __init__(self, league_payload: list[dict], detail_payloads: dict[int, dict]) -> None:
        self.league_payload = league_payload
        self.detail_payloads = detail_payloads
        self.detail_calls: list[int] = []

    async def fetch_league_matches(self, league_id: int) -> FakeResponse:
        return FakeResponse(
            f"/api/leagues/{league_id}/matches",
            f"GET /api/leagues/{league_id}/matches",
            NOW,
            200,
            self.league_payload,
        )

    async def fetch_match(self, match_id: int) -> FakeResponse:
        self.detail_calls.append(match_id)
        return FakeResponse(
            f"/api/matches/{match_id}",
            f"GET /api/matches/{match_id}",
            NOW,
            200,
            self.detail_payloads[match_id],
        )


class FakeArchive:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def archive_json(self, **values: object) -> object:
        self.calls.append(values)
        return object()


@dataclass
class FakeStatus:
    event_id: str
    match_id: int
    attempt_count: int = 0
    detail_complete: bool = False
    next_retry_at: datetime | None = None
    content_sha256: str | None = None


class FakeStore:
    def __init__(self) -> None:
        self.statuses: dict[int, FakeStatus] = {}
        self.candidates: list[tuple[int, str]] = []
        self.call_order: list[tuple[str, int]] = []
        self.successes: list[dict] = []
        self.failures: list[dict] = []
        self.legacy: dict[str, list[int]] = {}
        self.reconciled: list[str] = []
        self.recent_rescan_ids: list[int] = []

    def record_discovered_match(
        self, event: ApprovedEvent, summary: dict, discovered_at: datetime, source: str
    ) -> bool:
        match_id = int(summary["match_id"])
        self.call_order.append(("record", match_id))
        if match_id in self.statuses:
            return False
        self.statuses[match_id] = FakeStatus(event.event_id, match_id)
        return True

    def record_candidate_match(
        self, event: ApprovedEvent, summary: dict, reason: str, discovered_at: datetime
    ) -> None:
        self.candidates.append((int(summary["match_id"]), reason))

    def list_legacy_match_ids(self, event: ApprovedEvent) -> list[int]:
        return self.legacy.get(event.event_id, [])

    def get_ingest_status(self, match_id: int) -> FakeStatus | None:
        return self.statuses.get(match_id)

    def begin_ingest_attempt(self, match_id: int, attempted_at: datetime) -> int:
        self.call_order.append(("fetch", match_id))
        status = self.statuses[match_id]
        status.attempt_count += 1
        return status.attempt_count

    def record_ingest_success(self, **values: object) -> None:
        self.successes.append(values)
        status = self.statuses[int(values["match_id"])]
        status.detail_complete = bool(values["detail_complete"])
        status.next_retry_at = values["next_retry_at"]  # type: ignore[assignment]
        status.content_sha256 = str(values["content_sha256"])

    def record_ingest_failure(self, **values: object) -> None:
        self.failures.append(values)
        self.statuses[int(values["match_id"])].next_retry_at = values[
            "next_retry_at"
        ]  # type: ignore[assignment]

    def list_due_match_ids(
        self,
        now: datetime,
        event_ids: tuple[str, ...] | None = None,
        started_since: datetime | None = None,
    ) -> list[int]:
        return [
            match_id
            for match_id, status in self.statuses.items()
            if not status.detail_complete
            and status.next_retry_at is not None
            and status.next_retry_at <= now
            and (event_ids is None or status.event_id in event_ids)
        ]

    def mark_event_reconciled(self, event_id: str, checked_at: datetime) -> None:
        self.reconciled.append(event_id)

    def list_recent_rescan_match_ids(
        self,
        since: datetime,
        now: datetime,
        event_ids: tuple[str, ...] | None,
    ) -> list[int]:
        return list(self.recent_rescan_ids)


def event() -> ApprovedEvent:
    return ApprovedEvent(
        event_id="wallachia-s8",
        league_id=19543,
        stage_starts_at=NOW - timedelta(days=30),
        stage_ends_at=NOW + timedelta(days=1),
    )


def ready_processor(payload: object, expected_match_id: int) -> MatchProcessingResult:
    assert isinstance(payload, dict)
    assert int(payload["match_id"]) == expected_match_id
    return MatchProcessingResult(payload, detail_complete=True, retryable=False)


class StrictIngestTests(unittest.TestCase):
    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_records_formal_id_before_fetch_and_keeps_mismatch_candidate_only(self) -> None:
        started = int(NOW.timestamp())
        summaries = [
            {"match_id": 101, "leagueid": 19543, "start_time": started},
            {"match_id": 102, "leagueid": 99999, "start_time": started},
        ]
        store = FakeStore()
        client = FakeClient(summaries, {101: {"match_id": 101}})
        ingestor = StrictEventIngestor(
            FakeRegistry([event()]), store, FakeArchive(), client,
            processor=ready_processor, clock=lambda: NOW,
        )

        report = self.run_async(ingestor.run_once())

        self.assertEqual(client.detail_calls, [101])
        self.assertEqual(store.call_order[:2], [("record", 101), ("fetch", 101)])
        self.assertEqual(store.candidates, [(102, "league_mismatch")])
        self.assertNotIn(102, store.statuses)
        self.assertEqual((report.discovered, report.completed), (1, 1))
        self.assertEqual(report.changed_match_ids, (101,))

    def test_legacy_match_with_partial_player_row_is_refetched(self) -> None:
        store = FakeStore()
        store.legacy[event().event_id] = [201]
        client = FakeClient([], {201: {"match_id": 201}})
        ingestor = StrictEventIngestor(
            FakeRegistry([event()]), store, FakeArchive(), client,
            processor=ready_processor, clock=lambda: NOW,
        )

        self.run_async(ingestor.run_once())

        self.assertEqual(client.detail_calls, [201])
        self.assertEqual(store.call_order[:2], [("record", 201), ("fetch", 201)])

    def test_retryable_parse_persists_exact_next_retry(self) -> None:
        summary = {
            "match_id": 301,
            "leagueid": event().league_id,
            "start_time": int(NOW.timestamp()),
        }
        store = FakeStore()
        client = FakeClient([summary], {301: {"match_id": 301, "version": None}})

        def incomplete(payload: object, expected_match_id: int) -> MatchProcessingResult:
            return MatchProcessingResult(
                payload, detail_complete=False, retryable=True,
                missing_reasons=("opendota_unparsed",),
            )

        ingestor = StrictEventIngestor(
            FakeRegistry([event()]), store, FakeArchive(), client,
            processor=incomplete, clock=lambda: NOW,
        )

        self.run_async(ingestor.run_once())

        self.assertEqual(
            store.statuses[301].next_retry_at, NOW + timedelta(minutes=15)
        )
        self.assertEqual(store.successes[0]["missing_reasons"], ("opendota_unparsed",))

    def test_retry_sequence_continues_from_persisted_state_after_restart(self) -> None:
        summary = {
            "match_id": 302,
            "leagueid": event().league_id,
            "start_time": int(NOW.timestamp()),
        }
        store = FakeStore()
        client = FakeClient([summary], {302: {"match_id": 302}})

        def incomplete(payload: object, expected_match_id: int) -> MatchProcessingResult:
            return MatchProcessingResult(payload, False, True, ("source_unparsed",))

        first = StrictEventIngestor(
            FakeRegistry([event()]), store, FakeArchive(), client,
            processor=incomplete, clock=lambda: NOW,
        )
        self.run_async(first.run_once())

        retry_time = NOW + timedelta(minutes=15)
        restarted = StrictEventIngestor(
            FakeRegistry([event()]), store, FakeArchive(), client,
            processor=incomplete, clock=lambda: retry_time,
        )
        self.run_async(restarted.run_once())

        self.assertEqual(client.detail_calls, [302, 302])
        self.assertEqual(store.statuses[302].attempt_count, 2)
        self.assertEqual(
            store.statuses[302].next_retry_at, retry_time + timedelta(hours=1)
        )

    def test_complete_existing_match_is_not_fetched_or_reinserted(self) -> None:
        summary = {
            "match_id": 401,
            "leagueid": event().league_id,
            "start_time": int(NOW.timestamp()),
        }
        store = FakeStore()
        store.statuses[401] = FakeStatus(event().event_id, 401, detail_complete=True)
        client = FakeClient([summary], {401: {"match_id": 401}})
        ingestor = StrictEventIngestor(
            FakeRegistry([event()]), store, FakeArchive(), client,
            processor=ready_processor, clock=lambda: NOW,
        )

        self.run_async(ingestor.run_once())

        self.assertEqual(client.detail_calls, [])
        self.assertEqual(store.successes, [])

    def test_daily_recent_rescan_refetches_complete_map_without_reinserting_unchanged(self) -> None:
        summary = {
            "match_id": 402,
            "leagueid": event().league_id,
            "start_time": int(NOW.timestamp()),
        }
        store = FakeStore()
        payload = {"match_id": 402}
        response_hash = FakeResponse("", "", NOW, 200, payload).content_sha256
        store.statuses[402] = FakeStatus(
            event().event_id,
            402,
            detail_complete=True,
            content_sha256=response_hash,
        )
        store.recent_rescan_ids = [402]
        client = FakeClient([summary], {402: payload})
        ingestor = StrictEventIngestor(
            FakeRegistry([event()]), store, FakeArchive(), client,
            processor=ready_processor, clock=lambda: NOW,
        )

        report = self.run_async(
            ingestor.rescan_recent(NOW - timedelta(days=7), NOW)
        )

        self.assertEqual(client.detail_calls, [402])
        self.assertEqual(report.unchanged, 1)
        self.assertEqual(report.changed_match_ids, ())
        self.assertIsNone(store.successes[0]["facts"])

    def test_detail_fetches_are_bounded_by_configured_concurrency(self) -> None:
        current = maximum = 0

        class BlockingClient(FakeClient):
            async def fetch_match(self, match_id: int) -> FakeResponse:
                nonlocal current, maximum
                current += 1
                maximum = max(maximum, current)
                await asyncio.sleep(0.001)
                current -= 1
                return await super().fetch_match(match_id)

        summaries = [
            {
                "match_id": match_id,
                "leagueid": event().league_id,
                "start_time": int(NOW.timestamp()),
            }
            for match_id in range(500, 506)
        ]
        store = FakeStore()
        client = BlockingClient(
            summaries, {row["match_id"]: {"match_id": row["match_id"]} for row in summaries}
        )
        ingestor = StrictEventIngestor(
            FakeRegistry([event()]), store, FakeArchive(), client,
            processor=ready_processor, clock=lambda: NOW, max_concurrency=2,
        )

        self.run_async(ingestor.run_once())

        self.assertEqual(maximum, 2)

    def test_persisted_error_is_bounded_and_redacts_credentials(self) -> None:
        secret = "do-not-store"
        error = RuntimeError(
            f"GET https://user:{secret}@api.example/match?api_key={secret}&x={secret} "
            f"&client_secret={secret}&auth_token={secret}&x-api-key={secret}&sig={secret} "
            f"&credential={secret} "
            f"Authorization: Bearer {secret} x-api-key: {secret}\nfailed"
        )
        sanitized = sanitize_ingest_error(error)
        self.assertNotIn(secret, sanitized)
        self.assertNotIn("\n", sanitized)
        self.assertLessEqual(len(sanitized), 500)


if __name__ == "__main__":
    unittest.main()
