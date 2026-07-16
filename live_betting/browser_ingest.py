"""Transactional dispatch from validated browser events into live storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from .browser_contract import BrowserEvent, EventType, Transport
from .markets import normalized_state_hash, snapshots_from_payload
from .models import OddsSnapshot
from .storage import LiveBettingStore


@dataclass(frozen=True)
class BrowserIngestResult:
    event_id: str
    outcome: str
    processing_status: str
    reason: str | None = None
    timing_status: str | None = None
    normalized_change_count: int = 0


class BrowserNormalizationError(ValueError):
    pass


MAX_FUTURE_CAPTURE_SKEW = timedelta(seconds=5)


def _validated_odds_result(event: BrowserEvent) -> dict[str, object]:
    if event.transport in {Transport.FETCH, Transport.XHR} and event.source_path not in {
        "/v2/odds", "/odds"
    }:
        raise BrowserNormalizationError("odds source path is not an odds endpoint")
    if event.transport is Transport.PAGE_STATE:
        raise BrowserNormalizationError("page-state transport cannot normalize odds")
    result = event.payload.get("result")
    if not isinstance(result, dict):
        raise BrowserNormalizationError("missing odds result")
    if str(result.get("id") or "") != event.raybet_match_id:
        raise BrowserNormalizationError("payload match id mismatch")
    payload_game_id = result.get("game_id")
    if payload_game_id is not None and (
        type(payload_game_id) is not int or payload_game_id != 151
    ):
        raise BrowserNormalizationError("payload is not Dota 2")
    odds = result.get("odds")
    if not isinstance(odds, list) or not all(isinstance(row, dict) for row in odds):
        raise BrowserNormalizationError("odds must be an array of objects")
    teams = result.get("team", [])
    if not isinstance(teams, list) or not all(isinstance(row, dict) for row in teams):
        raise BrowserNormalizationError("teams must be an array of objects")
    try:
        for team in teams:
            int(team.get("pos") or 0)
    except (TypeError, ValueError) as error:
        raise BrowserNormalizationError("invalid team position") from error
    return result


class BrowserEventIngestor:
    """Process one event at a time; callers own batch sequencing and connection scope."""

    def __init__(
        self,
        parser: Callable[[dict, datetime | None], Sequence[OddsSnapshot]] | None = None,
        state_hasher: Callable[[Sequence[OddsSnapshot]], str] = normalized_state_hash,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.parser = parser or snapshots_from_payload
        self.state_hasher = state_hasher
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def ingest(self, store: LiveBettingStore, event: BrowserEvent) -> BrowserIngestResult:
        received_at = self.clock()
        recognized = event.event_type is not EventType.UNKNOWN
        with store.transaction():
            inserted = store.insert_browser_event(
                event, received_at=received_at, recognized=recognized,
            )
            if not inserted:
                if not store.browser_event_identity_matches(event):
                    return BrowserIngestResult(
                        event.event_id, "rejected", "error", "event_id_conflict"
                    )
                return BrowserIngestResult(
                    event.event_id, "duplicate", "duplicate", "duplicate_event_id"
                )

            if event.captured_at_utc > received_at + MAX_FUTURE_CAPTURE_SKEW:
                store.update_browser_event_status(
                    event.event_id, "audit_only", "future_observation"
                )
                return BrowserIngestResult(
                    event.event_id, "accepted", "audit_only", "future_observation"
                )

            audit_reason = self._audit_only_reason(event)
            if audit_reason is not None:
                store.update_browser_event_status(event.event_id, "audit_only", audit_reason)
                return BrowserIngestResult(
                    event.event_id, "accepted", "audit_only", audit_reason
                )

            try:
                with store.savepoint("browser_normalization"):
                    result = _validated_odds_result(event)
                    store.insert_browser_raybet_match(result, event.captured_at_utc)
                    try:
                        snapshots = list(self.parser(event.payload, event.captured_at_utc))
                        state_hash = self.state_hasher(snapshots)
                    except Exception as error:
                        raise BrowserNormalizationError("odds parser failed") from error
                    if any(row.raybet_match_id != event.raybet_match_id for row in snapshots):
                        raise BrowserNormalizationError("normalized match id mismatch")
                    try:
                        timing_status, change_count = store.store_odds_observation(
                            source="browser",
                            observation_key=event.event_id,
                            source_event_id=event.event_id,
                            raybet_match_id=event.raybet_match_id or "",
                            observed_at=event.captured_at_utc,
                            normalized_state_hash=state_hash,
                            snapshots=snapshots,
                        )
                    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
                        raise BrowserNormalizationError("odds normalization failed") from error
            except BrowserNormalizationError:
                store.update_browser_event_status(
                    event.event_id, "error", "normalization_failed"
                )
                return BrowserIngestResult(
                    event.event_id, "accepted", "error", "normalization_failed"
                )

            if timing_status == "late":
                status, reason = "audit_only", "late_observation"
            else:
                status, reason = "processed", None
            store.update_browser_event_status(event.event_id, status, reason)
            return BrowserIngestResult(
                event.event_id, "accepted", status, reason,
                timing_status, change_count,
            )

    @staticmethod
    def _audit_only_reason(event: BrowserEvent) -> str | None:
        if event.capture_reason is not None:
            return event.capture_reason
        return {
            EventType.MATCH_LIST: "match_list_audit_only",
            EventType.MARKET_UPDATE: "partial_market_update",
            EventType.VIDEO: "video_audit_only",
            EventType.MANUAL_CONTROL: "diagnostic_untrusted",
            EventType.UNKNOWN: "unknown_event",
        }.get(event.event_type)


def ingest_browser_event(
    store: LiveBettingStore, event: BrowserEvent, *, received_at: datetime | None = None
) -> BrowserIngestResult:
    """Convenience entry point for the companion's request-local store."""
    clock = (lambda: received_at) if received_at is not None else None
    return BrowserEventIngestor(clock=clock).ingest(store, event)
