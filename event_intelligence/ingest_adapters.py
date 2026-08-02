"""Concrete registry and PostgreSQL ports for strict event ingestion."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from database.session import DatabaseRow
from fetch.postgres_store import CoreMatchStore

from .facts import CompletedMatchFacts, ComponentStatus
from .ingest import (
    MATCH_PROCESSOR_VERSION,
    ApprovedEvent,
    IngestAttempt,
    ScopeDecision,
)
from .models import RegisteredEvent, StageScope
from .raw_archive import ArtifactReceipt, verify_raw_artifact_file
from .registry import EventRegistry, SCOPE_POLICY_VERSION
from .scheduler import SchedulerRetryState, next_retry_at
from .storage import IntelligenceStorage


_LEAGUE_ENDPOINT = re.compile(r"/api/leagues/(\d+)/matches$")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _true(value: object) -> bool:
    return value is True or value == 1 or (
        isinstance(value, str) and value.lower() in {"true", "yes", "1"}
    )


def _stage_scope(summary: Mapping[str, object]) -> StageScope:
    value = summary.get("stage_scope", summary.get("stage"))
    if value is None or value == "":
        return StageScope.MAIN_EVENT
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "main": StageScope.MAIN_EVENT,
        "main_event": StageScope.MAIN_EVENT,
        "group_stage": StageScope.MAIN_EVENT,
        "playoffs": StageScope.MAIN_EVENT,
        "lcq": StageScope.INTERNAL_LCQ,
        "internal_lcq": StageScope.INTERNAL_LCQ,
        "qualifier": StageScope.QUALIFIER,
        "qualifiers": StageScope.QUALIFIER,
        "division_2": StageScope.DIVISION_2,
        "exhibition": StageScope.EXHIBITION,
    }
    return aliases.get(normalized, StageScope.UNKNOWN)


def _artifact_id(source: str, content_hash: str) -> str:
    return f"{source}:{content_hash}"


@dataclass(frozen=True)
class PostgresIngestStatus:
    event_id: str
    match_id: int
    attempt_count: int
    detail_complete: bool
    next_retry_at: datetime | None
    content_sha256: str | None
    start_time: int | None
    missing_reasons: tuple[str, ...]
    processor_version: str | None
    attempt_generation: int


class RegistryIngestAdapter:
    """Expose only manually approved registry rows to the ingest coordinator."""

    def __init__(self, registry: EventRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _approved_event(event: RegisteredEvent) -> ApprovedEvent:
        return ApprovedEvent(
            event_id=event.event_id,
            league_id=event.opendota_league_id,
            stage_starts_at=event.main_event_start_at,
            stage_ends_at=event.main_event_end_at,
        )

    def approved_events(
        self, event_id: str | None = None, active_at: datetime | None = None
    ) -> list[ApprovedEvent]:
        records = self.registry.formal_events()
        output = [
            self._approved_event(record)
            for record in records
            if event_id is None or record.event_id == event_id
        ]
        if active_at is not None:
            output = [event for event in output if event.active_at(active_at)]
        return output

    def classify_discovered_match(
        self, event: ApprovedEvent, summary: Mapping[str, object]
    ) -> ScopeDecision:
        match_id = _integer(summary.get("match_id"))
        if match_id is None or match_id <= 0:
            return ScopeDecision(False, "invalid_match_id")
        league_id = _integer(summary.get("leagueid", summary.get("league_id")))
        if league_id != event.league_id:
            return ScopeDecision(False, "league_mismatch")
        start_time = _integer(summary.get("start_time"))
        if start_time is None or start_time <= 0:
            return ScopeDecision(False, "missing_start_time")
        started_at = datetime.fromtimestamp(start_time, timezone.utc)
        if not event.contains(started_at):
            return ScopeDecision(False, "outside_stage_boundaries")

        record = self.registry.get_by_event_id(event.event_id)
        if record is None:
            return ScopeDecision(False, "event_not_approved")
        stage = _stage_scope(summary)
        if stage not in record.included_stages:
            return ScopeDecision(False, f"excluded_stage:{stage.value}")
        if _true(summary.get("is_exhibition")):
            return ScopeDecision(False, "exhibition")
        if _true(summary.get("is_forfeit")):
            return ScopeDecision(False, "forfeit")
        if _true(summary.get("is_void_remake")):
            return ScopeDecision(False, "void_remake")
        return ScopeDecision(True, "approved_registry_scope")


class PostgresIngestAdapter:
    """Persist ingest state and normalize accepted versions in one transaction."""

    def __init__(
        self,
        storage: IntelligenceStorage,
        registry: EventRegistry,
        core_match_store: CoreMatchStore | None = None,
    ) -> None:
        self.storage = storage
        self.connection = storage.connection
        self.registry = registry
        self.registry_port = RegistryIngestAdapter(registry)
        self.core_match_store = core_match_store or CoreMatchStore(engine=storage.engine)

    def record_discovered_match(
        self,
        event: ApprovedEvent,
        summary: Mapping[str, object],
        discovered_at: datetime,
        source: str,
    ) -> bool:
        values = dict(summary)
        match_id = _integer(values.get("match_id"))
        if match_id is None or match_id <= 0:
            return False
        if source == "legacy_audit":
            if not self._table_exists("matches"):
                return False
            row = self.connection.execute(
                "SELECT leagueid, start_time, series_id FROM matches WHERE match_id=?",
                (match_id,),
            ).fetchone()
            if row is None:
                return False
            values.update(
                leagueid=row["leagueid"],
                start_time=row["start_time"],
                series_id=row["series_id"],
            )
        decision = self.registry_port.classify_discovered_match(event, values)
        if not decision.formal:
            self.record_candidate_match(event, values, decision.reason, discovered_at)
            return False

        timestamp = _iso(discovered_at)
        stage = _stage_scope(values).value
        with self.storage.transaction():
            existing = self.connection.execute(
                "SELECT event_id FROM match_ingest_status WHERE match_id=?", (match_id,)
            ).fetchone()
            if existing is not None and str(existing["event_id"]) != event.event_id:
                raise ValueError(f"match {match_id} already belongs to another event")
            cursor = self.connection.execute(
                """INSERT INTO match_ingest_status
                   (match_id, event_id, start_time, series_id, stage_scope,
                    stage_in_scope, has_valid_result, is_exhibition, is_forfeit,
                    is_void_remake, discovered_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, 0, 0, 0, 0, ?, ?)
                   ON CONFLICT (match_id) DO NOTHING""",
                (
                    match_id,
                    event.event_id,
                    _integer(values.get("start_time")),
                    _integer(values.get("series_id")),
                    stage,
                    timestamp,
                    timestamp,
                ),
            )
        return cursor.rowcount == 1

    def record_candidate_match(
        self,
        event: ApprovedEvent,
        summary: Mapping[str, object],
        reason: str,
        discovered_at: datetime,
    ) -> None:
        actual_league = _integer(summary.get("leagueid", summary.get("league_id")))
        match_id = _integer(summary.get("match_id"))
        match_segment = f":match:{match_id}" if match_id is not None else ""
        provider_id = f"{actual_league or event.league_id}:{reason}{match_segment}"
        evidence = {
            "reason": reason,
            "match_id": match_id,
            "expected_league_id": event.league_id,
            "observed_league_id": actual_league,
            "start_time": _integer(summary.get("start_time")),
        }
        league_for_url = actual_league or event.league_id
        self.registry.discover_candidate(
            source="opendota",
            provider_event_id=provider_id,
            canonical_name=f"Audit-only OpenDota discovery ({reason})",
            evidence_urls=(f"https://www.opendota.com/leagues/{league_for_url}",),
            evidence=evidence,
            discovered_at=discovered_at,
        )

    def record_event_candidate(
        self,
        summary: Mapping[str, object],
        reason: str,
        discovered_at: datetime,
    ) -> bool:
        league_id = _integer(summary.get("leagueid"))
        name = str(summary.get("name") or "").strip()
        if league_id is None or league_id <= 0 or not name:
            return False
        if self.registry.get_by_league_id(league_id) is not None:
            return False
        source = "opendota_league_catalog"
        provider_id = str(league_id)
        existing = self.connection.execute(
            "SELECT 1 FROM event_candidates WHERE source=? AND provider_event_id=?",
            (source, provider_id),
        ).fetchone()
        self.registry.discover_candidate(
            source=source,
            provider_event_id=provider_id,
            canonical_name=name,
            evidence_urls=(f"https://www.opendota.com/leagues/{league_id}",),
            evidence={
                "reason": reason,
                "source_fields": dict(summary),
                "scope_policy_version": SCOPE_POLICY_VERSION,
                "scope_starts_at": "2026-04-01T00:00:00+00:00",
                "missing_required_evidence": [
                    "formal_main_event_scope",
                    "main_event_dates",
                    "prize_pool_usd_at_least_1000000",
                    "tier_1",
                ],
                "decision": "pending_manual_audit",
            },
            discovered_at=discovered_at,
        )
        return existing is None

    def list_legacy_match_ids(self, event: ApprovedEvent) -> tuple[int, ...]:
        if not self._table_exists("matches"):
            return ()
        start = int(event.stage_starts_at.timestamp())
        end = int(event.stage_ends_at.timestamp())
        rows = self.connection.execute(
            """SELECT match_id FROM matches
               WHERE leagueid=? AND start_time BETWEEN ? AND ?
               ORDER BY match_id""",
            (event.league_id, start, end),
        ).fetchall()
        return tuple(int(row["match_id"]) for row in rows)

    def get_ingest_status(self, match_id: int) -> PostgresIngestStatus | None:
        row = self.connection.execute(
            """SELECT event_id, match_id, retry_count, detailed_parse_state,
                      next_retry_at, latest_raw_content_hash, start_time,
                      missing_fields_json, normalizer_version, attempt_generation
               FROM match_ingest_status WHERE match_id=?""",
            (match_id,),
        ).fetchone()
        if row is None:
            return None
        missing = tuple(str(value) for value in json.loads(row["missing_fields_json"]))
        return PostgresIngestStatus(
            event_id=str(row["event_id"]),
            match_id=int(row["match_id"]),
            attempt_count=int(row["retry_count"]),
            detail_complete=str(row["detailed_parse_state"]) in {"ready", "unscorable"},
            next_retry_at=_datetime(row["next_retry_at"]),
            content_sha256=row["latest_raw_content_hash"],
            start_time=row["start_time"],
            missing_reasons=missing,
            processor_version=row["normalizer_version"],
            attempt_generation=int(row["attempt_generation"]),
        )

    def validate_match_payload(
        self, match_id: int, payload: Mapping[str, object]
    ) -> ScopeDecision:
        row = self.connection.execute(
            "SELECT event_id FROM match_ingest_status WHERE match_id=?", (match_id,)
        ).fetchone()
        if row is None:
            return ScopeDecision(False, "match_not_discovered")
        event = self.registry.get_by_event_id(str(row["event_id"]))
        if event is None:
            return ScopeDecision(False, "event_not_approved")
        approved = RegistryIngestAdapter._approved_event(event)
        if _integer(payload.get("match_id")) != match_id:
            return ScopeDecision(False, "match_identity_mismatch")
        return self.registry_port.classify_discovered_match(approved, payload)

    def record_scope_rejection(
        self,
        match_id: int,
        payload: Mapping[str, object],
        reason: str,
        rejected_at: datetime,
        *,
        attempt_count: int | None = None,
        attempt_generation: int | None = None,
    ) -> None:
        with self.storage.transaction():
            row = self.connection.execute(
                """SELECT event_id, retry_count, last_attempt_at, attempt_generation
                   FROM match_ingest_status
                   WHERE match_id=?""",
                (match_id,),
            ).fetchone()
            if row is None or (
                attempt_count is not None
                and (
                    int(row["retry_count"]) != attempt_count
                    or row["last_attempt_at"] != _iso(rejected_at)
                    or (
                        attempt_generation is not None
                        and int(row["attempt_generation"]) != attempt_generation
                    )
                )
            ):
                return
            record = self.registry.get_by_event_id(str(row["event_id"]))
            if record is None:
                return
            event = RegistryIngestAdapter._approved_event(record)
            self.record_candidate_match(event, payload, reason, rejected_at)
            self.connection.execute(
                """UPDATE match_ingest_status
                   SET stage_in_scope=0, ingest_state='review_required',
                       reconciliation_status='review_required', next_retry_at=NULL,
                       last_error=?, updated_at=?
                   WHERE match_id=?""",
                (reason, _iso(rejected_at), match_id),
            )

    def begin_ingest_attempt(
        self, match_id: int, attempted_at: datetime
    ) -> IngestAttempt:
        with self.storage.transaction():
            row = self.connection.execute(
                """SELECT retry_count, attempt_generation
                   FROM match_ingest_status WHERE match_id=?""",
                (match_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"formal discovered match not found: {match_id}")
            attempt_count = int(row["retry_count"]) + 1
            attempt_generation = int(row["attempt_generation"]) + 1
            fallback = next_retry_at(attempted_at, attempt_count)
            self.connection.execute(
                """UPDATE match_ingest_status
                   SET retry_count=?, attempt_generation=?, last_attempt_at=?, next_retry_at=?,
                       ingest_state='detail_pending', updated_at=?
                   WHERE match_id=?""",
                (
                    attempt_count,
                    attempt_generation,
                    _iso(attempted_at),
                    _iso(fallback),
                    _iso(attempted_at),
                    match_id,
                ),
            )
        return IngestAttempt(attempt_count, attempt_generation)

    def record_ingest_success(self, **values: object) -> str:
        with self.storage.transaction():
            return self._record_ingest_success(**values)

    def _record_ingest_success(self, **values: object) -> str:
        match_id = int(values["match_id"])
        attempt_count = int(values["attempt_count"])
        attempt_generation = values.get("attempt_generation")
        payload = values.get("payload")
        facts = values.get("facts")
        content_hash = str(values["content_sha256"])
        processor_version = str(
            values.get("processor_version") or MATCH_PROCESSOR_VERSION
        )
        incoming_missing = tuple(str(value) for value in values["missing_reasons"])
        row = self.connection.execute(
            """SELECT latest_raw_content_hash, detailed_parse_state,
                       player_readiness, state_readiness, draft_readiness,
                       missing_fields_json, retry_count, normalizer_version,
                       last_attempt_at, attempt_generation
                FROM match_ingest_status WHERE match_id=?""",
            (match_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"formal discovered match not found: {match_id}")
        if (
            int(row["retry_count"]) != attempt_count
            or row["last_attempt_at"] != _iso(values["attempted_at"])  # type: ignore[arg-type]
            or (
                attempt_generation is not None
                and int(row["attempt_generation"]) != int(attempt_generation)
            )
        ):
            return "superseded"

        artifact_unchanged = bool(values["artifact_unchanged"])
        if artifact_unchanged:
            with self.storage.transaction():
                self.connection.execute(
                    """UPDATE match_ingest_status
                       SET ingest_state=CASE detailed_parse_state
                             WHEN 'ready' THEN 'detailed'
                             WHEN 'unscorable' THEN 'detailed'
                             WHEN 'review_required' THEN 'review_required'
                             ELSE 'retryable' END,
                           retry_count=CASE WHEN ? THEN 0 ELSE retry_count END,
                           next_retry_at=?, last_attempt_at=?, last_error=NULL, updated_at=?
                       WHERE match_id=?""",
                    (
                        bool(values["detail_complete"]),
                        _iso(values.get("next_retry_at")),  # type: ignore[arg-type]
                        _iso(values["attempted_at"]),  # type: ignore[arg-type]
                        _iso(values["attempted_at"]),  # type: ignore[arg-type]
                        match_id,
                    ),
                )
            return "unchanged"
        accept, correction = self._accept_version(
            row,
            facts,
            incoming_missing,
            artifact_unchanged,
            processor_version,
        )
        if not accept:
            with self.storage.transaction():
                self.connection.execute(
                    """UPDATE match_ingest_status
                       SET ingest_state=CASE detailed_parse_state
                             WHEN 'ready' THEN 'detailed'
                             WHEN 'unscorable' THEN 'detailed'
                             WHEN 'review_required' THEN 'review_required'
                             ELSE 'retryable' END,
                           reconciliation_status=CASE WHEN ? THEN 'review_required'
                                                      ELSE reconciliation_status END,
                           retry_count=CASE WHEN ? THEN 0 ELSE retry_count END,
                           last_error=?, next_retry_at=?, last_attempt_at=?, updated_at=?
                       WHERE match_id=?""",
                    (
                        correction,
                        bool(values["detail_complete"]),
                        "content_correction_pending" if correction else "less_complete_raw_version",
                        _iso(values.get("next_retry_at")),  # type: ignore[arg-type]
                        _iso(values["attempted_at"]),  # type: ignore[arg-type]
                        _iso(values["attempted_at"]),  # type: ignore[arg-type]
                        match_id,
                    ),
                )
            return "review_required" if correction else "retained_more_complete"

        if not isinstance(payload, Mapping) or not isinstance(facts, CompletedMatchFacts):
            raise TypeError("accepted normalized version requires payload and exact facts")
        artifact_key = _artifact_id("opendota", content_hash)
        if self.connection.execute(
            "SELECT 1 FROM raw_source_artifacts WHERE artifact_id=?", (artifact_key,)
        ).fetchone() is None:
            raise ValueError("raw artifact must be persisted before normalization")

        detailed_state = (
            "ready"
            if bool(values["detail_complete"])
            else "retryable" if bool(values["retryable"]) else "review_required"
        )
        ingest_state = (
            "detailed"
            if detailed_state == "ready"
            else "retryable" if detailed_state == "retryable" else "review_required"
        )
        first_usable_at = values.get("first_usable_at")
        with self.storage.transaction():
            hero_ids = sorted(
                {
                    hero_id
                    for hero_id in (
                        *(player.hero_id for player in facts.players),
                        *(action.hero_id for action in facts.picks_bans),
                    )
                    if hero_id is not None
                }
            )
            self.connection.executemany(
                "INSERT INTO heroes (hero_id) VALUES (?) ON CONFLICT (hero_id) DO NOTHING",
                [(hero_id,) for hero_id in hero_ids],
            )
            self.core_match_store.insert_match_with_connection(
                self.connection.active_connection,
                copy.deepcopy(dict(payload)),
            )
            self._insert_exact_player_facts(
                facts,
                artifact_key,
                content_hash,
                first_usable_at,  # type: ignore[arg-type]
                processor_version,
            )
            self.connection.execute(
                """UPDATE match_ingest_status
                   SET start_time=COALESCE(?, start_time), series_id=COALESCE(?, series_id),
                       has_valid_result=?, ingest_state=?, basic_result_state=?,
                       detailed_parse_state=?, latest_raw_artifact_id=?,
                        latest_raw_content_hash=?, raw_artifact_version=raw_artifact_version+1,
                        normalizer_version=?,
                        retry_count=CASE WHEN ? THEN 0 ELSE retry_count END,
                       next_retry_at=?, last_attempt_at=?, last_error=NULL,
                       first_usable_at=COALESCE(first_usable_at, ?),
                       missing_fields_json=?, player_readiness=?, state_readiness=?,
                       draft_readiness=?, updated_at=?
                   WHERE match_id=?""",
                (
                    facts.start_time,
                    facts.series_id,
                    int(facts.completeness.basic_result),
                    ingest_state,
                    facts.readiness.normalization.status.value,
                    detailed_state,
                    artifact_key,
                    content_hash,
                    processor_version,
                    bool(values["detail_complete"]),
                    _iso(values.get("next_retry_at")),  # type: ignore[arg-type]
                    _iso(values["attempted_at"]),  # type: ignore[arg-type]
                    _iso(first_usable_at),  # type: ignore[arg-type]
                    self._json(incoming_missing),
                    facts.readiness.player_scoring.status.value,
                    facts.readiness.team_state.status.value,
                    facts.readiness.draft_model.status.value,
                    _iso(values["attempted_at"]),  # type: ignore[arg-type]
                    match_id,
                ),
            )
        return "normalized"

    @staticmethod
    def _readiness_score(values: Sequence[str]) -> tuple[int, ...]:
        scores = {"pending": 0, "retryable": 0, "review_required": 1, "unscorable": 2, "ready": 2}
        return tuple(scores.get(value, 0) for value in values)

    def _accept_version(
        self,
        current: DatabaseRow,
        facts: object,
        incoming_missing: tuple[str, ...],
        artifact_unchanged: bool,
        processor_version: str,
    ) -> tuple[bool, bool]:
        if artifact_unchanged:
            return False, False
        if current["latest_raw_content_hash"] is None:
            return True, False
        if not isinstance(facts, CompletedMatchFacts):
            return False, False
        old_values = (
            str(current["detailed_parse_state"]),
            str(current["player_readiness"]),
            str(current["state_readiness"]),
            str(current["draft_readiness"]),
        )
        new_values = (
            "ready" if not any(
                assessment.status in {ComponentStatus.RETRYABLE, ComponentStatus.REVIEW_REQUIRED}
                for assessment in vars(facts.readiness).values()
            ) else "retryable",
            facts.readiness.player_scoring.status.value,
            facts.readiness.team_state.status.value,
            facts.readiness.draft_model.status.value,
        )
        old_score = self._readiness_score(old_values)
        new_score = self._readiness_score(new_values)
        old_missing = set(json.loads(current["missing_fields_json"]))
        new_missing = set(incoming_missing)
        no_degradation = all(new >= old for new, old in zip(new_score, old_score))
        improved = any(new > old for new, old in zip(new_score, old_score))
        missing_improved = new_missing < old_missing
        processor_upgrade = current["normalizer_version"] != processor_version
        if no_degradation and new_missing <= old_missing and (
            improved or missing_improved or processor_upgrade
        ):
            return True, False
        same_completeness = new_score == old_score and new_missing == old_missing
        return False, same_completeness

    def _insert_exact_player_facts(
        self,
        facts: CompletedMatchFacts,
        artifact_key: str,
        content_hash: str,
        first_usable_at: datetime | None,
        processor_version: str,
    ) -> None:
        version = f"{processor_version}:{content_hash}"
        created_at = _iso(first_usable_at or datetime.now(timezone.utc))
        for player in facts.players:
            if player.player_slot is None:
                continue
            values = asdict(player)
            missing = tuple(key for key, value in values.items() if value is None)
            coverage = (len(values) - len(missing)) / len(values)
            self.connection.execute(
                """INSERT INTO player_map_facts
                   (match_id, player_slot, account_id, team_id, hero_id, is_radiant,
                    facts_json, missing_fields_json, coverage, source_artifact_id,
                    source_content_hash, fact_version, first_usable_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (match_id, player_slot, fact_version) DO NOTHING""",
                (
                    facts.match_id,
                    player.player_slot,
                    player.account_id,
                    player.team_id,
                    player.hero_id,
                    None if player.is_radiant is None else int(player.is_radiant),
                    self._json(values),
                    self._json(missing),
                    coverage,
                    artifact_key,
                    content_hash,
                    version,
                    _iso(first_usable_at),
                    created_at,
                ),
            )

    def record_ingest_failure(self, **values: object) -> None:
        match_id = int(values["match_id"])
        attempt_count = int(values["attempt_count"])
        attempt_generation = values.get("attempt_generation")
        next_at = values.get("next_retry_at")
        ingest_state = "retryable" if next_at is not None else "failed"
        generation_clause = (
            "" if attempt_generation is None else " AND attempt_generation=?"
        )
        with self.storage.transaction():
            self.connection.execute(
                f"""UPDATE match_ingest_status
                   SET ingest_state=CASE
                         WHEN ingest_state='review_required' THEN ingest_state
                         ELSE ? END,
                       next_retry_at=?, last_attempt_at=?, last_error=?, updated_at=?
                   WHERE match_id=? AND retry_count=? AND last_attempt_at=?
                     {generation_clause}""",
                (
                    ingest_state,
                    _iso(next_at),  # type: ignore[arg-type]
                    _iso(values["attempted_at"]),  # type: ignore[arg-type]
                    str(values["error"])[:500],
                    _iso(values["attempted_at"]),  # type: ignore[arg-type]
                    match_id,
                    attempt_count,
                    _iso(values["attempted_at"]),  # type: ignore[arg-type]
                    *((attempt_generation,) if attempt_generation is not None else ()),
                ),
            )

    def list_due_match_ids(
        self,
        now: datetime,
        event_ids: tuple[str, ...] | None = None,
        started_since: datetime | None = None,
    ) -> tuple[int, ...]:
        clauses = ["next_retry_at IS NOT NULL", "next_retry_at<=?"]
        parameters: list[object] = [_iso(now)]
        if event_ids:
            clauses.append("event_id IN (" + ",".join("?" for _ in event_ids) + ")")
            parameters.extend(event_ids)
        if started_since is not None:
            clauses.append("start_time>=?")
            parameters.append(int(_utc(started_since).timestamp()))
        rows = self.connection.execute(
            "SELECT match_id FROM match_ingest_status WHERE "
            + " AND ".join(clauses)
            + " ORDER BY match_id",
            parameters,
        ).fetchall()
        return tuple(int(row["match_id"]) for row in rows)

    def list_recent_rescan_match_ids(
        self,
        since: datetime,
        now: datetime,
        event_ids: tuple[str, ...] | None,
    ) -> tuple[int, ...]:
        clauses = [
            "stage_in_scope=1",
            "start_time BETWEEN ? AND ?",
            "is_exhibition=0",
            "is_forfeit=0",
            "is_void_remake=0",
        ]
        parameters: list[object] = [
            int(_utc(since).timestamp()),
            int(_utc(now).timestamp()),
        ]
        if event_ids:
            clauses.append("event_id IN (" + ",".join("?" for _ in event_ids) + ")")
            parameters.extend(event_ids)
        rows = self.connection.execute(
            "SELECT match_id FROM match_ingest_status WHERE "
            + " AND ".join(clauses)
            + " ORDER BY match_id",
            parameters,
        ).fetchall()
        return tuple(int(row["match_id"]) for row in rows)

    def get_scheduler_checkpoint(self, key: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT checkpoint_at FROM ingest_scheduler_checkpoints WHERE checkpoint_key=?",
            (key,),
        ).fetchone()
        return _datetime(row["checkpoint_at"]) if row is not None else None

    def set_scheduler_checkpoint(self, key: str, value: datetime) -> None:
        timestamp = _iso(value)
        with self.storage.transaction():
            self.connection.execute(
                """INSERT INTO ingest_scheduler_checkpoints
                   (checkpoint_key, checkpoint_at, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(checkpoint_key) DO UPDATE SET
                       checkpoint_at=excluded.checkpoint_at,
                       updated_at=excluded.updated_at""",
                (key, timestamp, timestamp),
            )
            self.connection.execute(
                "DELETE FROM ingest_scheduler_retry_state WHERE checkpoint_key=?",
                (key,),
            )

    def get_scheduler_retry_state(self, key: str) -> SchedulerRetryState | None:
        row = self.connection.execute(
            """SELECT failure_count, next_retry_at, last_error, updated_at
                 FROM ingest_scheduler_retry_state WHERE checkpoint_key=?""",
            (key,),
        ).fetchone()
        if row is None:
            return None
        retry_at = _datetime(row["next_retry_at"])
        assert retry_at is not None
        return SchedulerRetryState(
            int(row["failure_count"]),
            retry_at,
            str(row["last_error"]),
            _datetime(row["updated_at"]),
        )

    def set_scheduler_retry_state(
        self,
        key: str,
        state: SchedulerRetryState,
        updated_at: datetime,
    ) -> None:
        timestamp = _iso(updated_at)
        with self.storage.transaction():
            self.connection.execute(
                """INSERT INTO ingest_scheduler_retry_state
                   (checkpoint_key, failure_count, next_retry_at, last_error,
                    updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(checkpoint_key) DO UPDATE SET
                       failure_count=excluded.failure_count,
                       next_retry_at=excluded.next_retry_at,
                       last_error=excluded.last_error,
                       updated_at=excluded.updated_at""",
                (
                    key,
                    state.failure_count,
                    _iso(state.next_retry_at),
                    state.last_error,
                    timestamp,
                ),
            )

    def record_raw_artifact(self, receipt: ArtifactReceipt) -> None:
        artifact_key = _artifact_id(receipt.source, receipt.content_sha256)
        event_id = self._event_id_for_receipt(receipt)
        created_at = _iso(receipt.observed_at)
        storage_path = str(Path(receipt.path).resolve())
        verify_raw_artifact_file(
            receipt.path,
            content_hash=receipt.content_sha256,
            uncompressed_bytes=receipt.byte_count,
            compressed_bytes=receipt.compressed_byte_count,
            expected_schema_fingerprint=receipt.schema_fingerprint,
        )
        with self.storage.transaction():
            artifact = self.connection.execute(
                """SELECT content_hash, source, artifact_use, storage_path,
                          uncompressed_bytes, compressed_bytes, schema_fingerprint,
                          first_usable_at
                     FROM raw_source_artifacts WHERE artifact_id=?""",
                (artifact_key,),
            ).fetchone()
            artifact_authority = {
                "content_hash": receipt.content_sha256,
                "source": receipt.source,
                "artifact_use": "primary",
                "storage_path": storage_path,
                "uncompressed_bytes": receipt.byte_count,
                "compressed_bytes": receipt.compressed_byte_count,
                "schema_fingerprint": receipt.schema_fingerprint,
            }
            if artifact is None:
                self.connection.execute(
                    """INSERT INTO raw_source_artifacts
                       (artifact_id, content_hash, source, artifact_use, endpoint,
                        sanitized_request_identity, storage_path, uncompressed_bytes,
                        compressed_bytes, source_at, received_at, first_usable_at,
                        schema_fingerprint, event_id, match_id, created_at)
                       VALUES (?, ?, ?, 'primary', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        artifact_key,
                        receipt.content_sha256,
                        receipt.source,
                        receipt.endpoint,
                        receipt.request_identity,
                        storage_path,
                        receipt.byte_count,
                        receipt.compressed_byte_count,
                        _iso(receipt.source_timestamp),
                        _iso(receipt.observed_at),
                        _iso(receipt.first_usable_at),
                        receipt.schema_fingerprint,
                        event_id,
                        receipt.match_id,
                        created_at,
                    ),
                )
            else:
                mismatched = [
                    field
                    for field, expected in artifact_authority.items()
                    if artifact[field] != expected
                ]
                if receipt.artifact_created:
                    mismatched.append("registered_file_was_missing")
                if mismatched:
                    if receipt.artifact_created:
                        receipt.path.unlink(missing_ok=True)
                    raise RuntimeError(
                        "raw source artifact conflict differs from persisted authority: "
                        + ", ".join(mismatched)
                    )
                verify_raw_artifact_file(
                    artifact["storage_path"],
                    content_hash=str(artifact["content_hash"]),
                    uncompressed_bytes=int(artifact["uncompressed_bytes"]),
                    compressed_bytes=int(artifact["compressed_bytes"]),
                    expected_schema_fingerprint=str(artifact["schema_fingerprint"]),
                )
                if artifact["first_usable_at"] is None and receipt.first_usable_at is not None:
                    self.connection.execute(
                        """UPDATE raw_source_artifacts SET first_usable_at=?
                            WHERE artifact_id=? AND first_usable_at IS NULL""",
                        (_iso(receipt.first_usable_at), artifact_key),
                    )

            observation_authority = {
                "artifact_id": artifact_key,
                "content_hash": receipt.content_sha256,
                "source": receipt.source,
                "artifact_use": "primary",
                "endpoint": receipt.endpoint,
                "sanitized_request_identity": receipt.request_identity,
                "source_at": _iso(receipt.source_timestamp),
                "received_at": _iso(receipt.observed_at),
                "schema_fingerprint": receipt.schema_fingerprint,
                "event_id": event_id,
                "match_id": receipt.match_id,
                "http_status": receipt.status_code,
                "created_at": created_at,
            }
            observation = self.connection.execute(
                """SELECT artifact_id, content_hash, source, artifact_use, endpoint,
                          sanitized_request_identity, source_at, received_at,
                          first_usable_at, schema_fingerprint, event_id, match_id,
                          http_status, created_at
                     FROM raw_source_observations WHERE observation_id=?""",
                (receipt.observation_id,),
            ).fetchone()
            if observation is None:
                self.connection.execute(
                    """INSERT INTO raw_source_observations
                       (observation_id, artifact_id, content_hash, source, artifact_use,
                        endpoint, sanitized_request_identity, source_at, received_at,
                        first_usable_at, schema_fingerprint, event_id, match_id,
                        http_status, created_at)
                       VALUES (?, ?, ?, ?, 'primary', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        receipt.observation_id,
                        artifact_key,
                        receipt.content_sha256,
                        receipt.source,
                        receipt.endpoint,
                        receipt.request_identity,
                        _iso(receipt.source_timestamp),
                        _iso(receipt.observed_at),
                        _iso(receipt.first_usable_at),
                        receipt.schema_fingerprint,
                        event_id,
                        receipt.match_id,
                        receipt.status_code,
                        created_at,
                    ),
                )
                return

            mismatched = [
                field
                for field, expected in observation_authority.items()
                if observation[field] != expected
            ]
            if mismatched:
                raise RuntimeError(
                    "raw source observation conflict differs from persisted authority: "
                    + ", ".join(mismatched)
                )
            first_usable_at = _iso(receipt.first_usable_at)
            if observation["first_usable_at"] is None and first_usable_at is not None:
                self.connection.execute(
                    """UPDATE raw_source_observations SET first_usable_at=?
                        WHERE observation_id=? AND first_usable_at IS NULL""",
                    (first_usable_at, receipt.observation_id),
                )
            elif (
                observation["first_usable_at"] is not None
                and first_usable_at is not None
                and observation["first_usable_at"] != first_usable_at
            ):
                raise RuntimeError(
                    "raw source observation first usable time conflicts with authority"
                )

    def _event_id_for_receipt(self, receipt: ArtifactReceipt) -> str | None:
        if receipt.match_id is not None:
            row = self.connection.execute(
                "SELECT event_id FROM match_ingest_status WHERE match_id=?",
                (receipt.match_id,),
            ).fetchone()
            if row is not None:
                return str(row["event_id"])
        matched = _LEAGUE_ENDPOINT.search(receipt.endpoint)
        if matched:
            event = self.registry.get_by_league_id(int(matched.group(1)))
            if event is not None:
                return event.event_id
        return None

    def reconcile_event(
        self, event: ApprovedEvent, observed_match_ids: set[int], checked_at: datetime
    ) -> None:
        record = self.registry.get_by_event_id(event.event_id)
        if record is None:
            raise LookupError(f"approved event not found: {event.event_id}")
        observed = len(observed_match_ids)
        reconciled = (
            record.expected_map_count is not None
            and observed == record.expected_map_count
            and (record.public_map_count is None or observed == record.public_map_count)
        )
        status = "reconciled" if reconciled else "reconciliation_pending"
        with self.storage.transaction():
            self.connection.execute(
                """UPDATE event_registry
                   SET observed_map_count=?, reconciliation_status=?, updated_at=?
                   WHERE event_id=?""",
                (observed, status, _iso(checked_at), event.event_id),
            )
            self.connection.execute(
                """UPDATE match_ingest_status SET reconciliation_status=?, updated_at=?
                   WHERE event_id=?""",
                (status, _iso(checked_at), event.event_id),
            )

    def _table_exists(self, name: str) -> bool:
        return self.connection.execute(
            "SELECT to_regclass(?)", (name,)
        ).fetchone()[0] is not None

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=lambda item: item.value if hasattr(item, "value") else str(item),
        )


# Transitional import aliases; both names use PostgreSQL exclusively.
SQLiteIngestStatus = PostgresIngestStatus
SQLiteIngestAdapter = PostgresIngestAdapter
