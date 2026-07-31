"""Manually audited event registry and strict-scope queries."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence, TYPE_CHECKING

from database.session import DatabaseRow

from .models import (
    ApprovalStatus,
    ComponentReadiness,
    EventCandidate,
    EventScope,
    EvidenceStatus,
    FormalMatch,
    IngestState,
    ReconciliationStatus,
    RegisteredEvent,
    StageScope,
)

if TYPE_CHECKING:
    from .storage import IntelligenceStorage


SCOPE_POLICY_VERSION = "strict-t1-usd1m-main-event-v1"
AUDITED_AT = "2026-07-13T00:00:00+00:00"
REGISTRY_UPDATED_AT = "2026-07-13T20:56:00+00:00"
EXCLUDED_CATEGORIES = (
    "qualifier",
    "division_2",
    "exhibition",
    "forfeit",
    "void_remake",
)


APPROVED_EVENT_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "event_id": "pgl-wallachia-s8-2026",
        "canonical_name": "PGL Wallachia Season 8",
        "tier": "tier_1",
        "prize_pool_usd": 1_000_000,
        "main_event_start_at": "2026-04-18T00:00:00+00:00",
        "main_event_end_at": "2026-04-26T23:59:59+00:00",
        "opendota_league_id": 19543,
        "official_evidence_urls": ("https://www.pglesports.com/",),
        "expected_map_count": 119,
        "observed_map_count": 119,
        "public_map_count": 119,
        "reconciliation_status": "reconciled",
        "reconciliation_note": None,
        "included_stages": ("main_event",),
        "include_internal_lcq": 0,
    },
    {
        "event_id": "dreamleague-s29-2026",
        "canonical_name": "DreamLeague Season 29",
        "tier": "tier_1",
        "prize_pool_usd": 1_000_000,
        "main_event_start_at": "2026-05-13T00:00:00+00:00",
        "main_event_end_at": "2026-05-24T23:59:59+00:00",
        "opendota_league_id": 19696,
        "official_evidence_urls": ("https://www.dreamhack.com/",),
        "expected_map_count": 185,
        "observed_map_count": 185,
        "public_map_count": 185,
        "reconciliation_status": "reconciled",
        "reconciliation_note": None,
        "included_stages": ("main_event",),
        "include_internal_lcq": 0,
    },
    {
        "event_id": "blast-slam-vii-2026",
        "canonical_name": "BLAST SLAM VII",
        "tier": "tier_1",
        "prize_pool_usd": 1_000_000,
        "main_event_start_at": "2026-05-26T00:00:00+00:00",
        "main_event_end_at": "2026-06-07T23:59:59+00:00",
        "opendota_league_id": 19101,
        "official_evidence_urls": ("https://blast.tv/dota",),
        "expected_map_count": 102,
        "observed_map_count": 102,
        "public_map_count": 102,
        "reconciliation_status": "reconciled",
        "reconciliation_note": "The event's internal LCQ is an included main-event stage.",
        "included_stages": ("main_event", "internal_lcq"),
        "include_internal_lcq": 1,
    },
    {
        "event_id": "ewc-dota2-2026",
        "canonical_name": "Esports World Cup 2026",
        "tier": "tier_1",
        "prize_pool_usd": 2_000_000,
        "main_event_start_at": "2026-07-07T00:00:00+00:00",
        "main_event_end_at": "2026-07-19T23:59:59+00:00",
        "opendota_league_id": 19785,
        "official_evidence_urls": (
            "https://www.esportsworldcup.com/en/competitions/2026/dota2",
            "https://blast.tv/dota/tournaments/esports-world-cup-2026-dota-2",
        ),
        "expected_map_count": None,
        "observed_map_count": 120,
        "public_map_count": 120,
        "reconciliation_status": "reconciliation_pending",
        "reconciliation_note": (
            "The event remains active through 2026-07-19 UTC. Official data "
            "contains 120 completed game objects through the group stage; the "
            "58-second LGD-VP tiebreaker record has no game object and is "
            "excluded. The final map count remains unknown."
        ),
        "included_stages": ("main_event",),
        "include_internal_lcq": 0,
    },
    {
        "event_id": "games-of-the-future-2026",
        "canonical_name": "Games of the Future 2026",
        "tier": "tier_1",
        "prize_pool_usd": 1_000_000,
        "main_event_start_at": "2026-07-31T00:00:00+00:00",
        "main_event_end_at": "2026-08-05T23:59:59+00:00",
        "opendota_league_id": 19917,
        "official_evidence_urls": (
            "https://gofuture.games/news/item/"
            "dota-2-returns-for-gotf-2026-with-1m-prize-pool/",
        ),
        "expected_map_count": None,
        "observed_map_count": 2,
        "public_map_count": 2,
        "reconciliation_status": "reconciliation_pending",
        "reconciliation_note": (
            "The Dota 2 event remains active through 2026-08-05 UTC. "
            "Two OpenDota maps were public when the event was approved; "
            "the final map count remains unknown."
        ),
        "included_stages": ("main_event",),
        "include_internal_lcq": 0,
    },
)


class EventRegistry:
    """Queries only approved rows; discovery remains in an isolated table."""

    def __init__(self, storage: "IntelligenceStorage") -> None:
        self.storage = storage
        self.connection = storage.connection

    def seed_approved_events(self) -> None:
        with self._transaction():
            self._migrate_known_seed_corrections()
            for seed in APPROVED_EVENT_SEEDS:
                self.connection.execute(
                    """INSERT INTO event_registry
                    (event_id, canonical_name, tier, prize_pool_usd,
                     main_event_start_at, main_event_end_at, opendota_league_id,
                     secondary_provider_ids_json, official_evidence_urls_json,
                     evidence_status, scope_policy_version, scope, approval_status,
                     approved_by, approved_at, reconciliation_status,
                     expected_map_count, observed_map_count, public_map_count,
                     reconciliation_note, included_stages_json,
                     excluded_categories_json, include_internal_lcq,
                     excludes_qualifiers, excludes_division_2,
                     excludes_exhibitions, excludes_forfeits,
                     excludes_void_remakes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, 'manually_audited', ?,
                            'formal_main_event', 'approved', 'manual_event_audit', ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, 1, 1, ?, ?)
                    ON CONFLICT (event_id) DO NOTHING""",
                    (
                        seed["event_id"],
                        seed["canonical_name"],
                        seed["tier"],
                        seed["prize_pool_usd"],
                        seed["main_event_start_at"],
                        seed["main_event_end_at"],
                        seed["opendota_league_id"],
                        self._json(seed["official_evidence_urls"]),
                        SCOPE_POLICY_VERSION,
                        AUDITED_AT,
                        seed["reconciliation_status"],
                        seed["expected_map_count"],
                        seed["observed_map_count"],
                        seed["public_map_count"],
                        seed["reconciliation_note"],
                        self._json(seed["included_stages"]),
                        self._json(EXCLUDED_CATEGORIES),
                        seed["include_internal_lcq"],
                        AUDITED_AT,
                        REGISTRY_UPDATED_AT,
                    ),
                )
                self.connection.execute(
                    """UPDATE event_candidates
                          SET evidence_status='manually_audited',
                              audit_status='promoted',
                              audit_note='matched approved event registry seed',
                              promoted_event_id=?
                        WHERE source='opendota_league_catalog'
                          AND provider_event_id=?
                          AND audit_status IN ('pending', 'approved', 'promoted')""",
                    (seed["event_id"], str(seed["opendota_league_id"])),
                )

    def _migrate_known_seed_corrections(self) -> None:
        ewc = next(
            seed
            for seed in APPROVED_EVENT_SEEDS
            if seed["event_id"] == "ewc-dota2-2026"
        )
        legacy_note = (
            "OpenDota exposes 120 maps while the audited public count is 121; "
            "the unmatched or duplicate map remains unresolved."
        )
        interim_note = (
            "The event remains active through 2026-07-19 UTC. OpenDota's 120 "
            "maps and the audited public count of 121 are interim; the final "
            "map count and the current one-map discrepancy remain unresolved."
        )
        self.connection.execute(
            """UPDATE event_registry
               SET prize_pool_usd=?, main_event_end_at=?,
                   official_evidence_urls_json=?, expected_map_count=?,
                   public_map_count=?,
                   reconciliation_note=CASE
                       WHEN reconciliation_note=? THEN ?
                       ELSE reconciliation_note
                   END,
                   updated_at=CASE
                       WHEN updated_at > ? THEN updated_at
                       ELSE ?
                   END
               WHERE event_id='ewc-dota2-2026'
                 AND canonical_name='Esports World Cup 2026'
                 AND tier='tier_1'
                 AND prize_pool_usd=1000000
                 AND main_event_start_at='2026-07-07T00:00:00+00:00'
                 AND main_event_end_at='2026-07-12T23:59:59+00:00'
                 AND opendota_league_id=19785
                 AND official_evidence_urls_json=?
                 AND expected_map_count=120
                 AND public_map_count=121""",
            (
                ewc["prize_pool_usd"],
                ewc["main_event_end_at"],
                self._json(ewc["official_evidence_urls"]),
                ewc["expected_map_count"],
                ewc["public_map_count"],
                legacy_note,
                ewc["reconciliation_note"],
                REGISTRY_UPDATED_AT,
                REGISTRY_UPDATED_AT,
                self._json(("https://www.esportsworldcup.com/",)),
            ),
        )
        self.connection.execute(
            """UPDATE event_registry
               SET public_map_count=?,
                   reconciliation_note=?,
                   updated_at=CASE
                       WHEN updated_at > ? THEN updated_at
                       ELSE ?
                   END
               WHERE event_id='ewc-dota2-2026'
                 AND canonical_name='Esports World Cup 2026'
                 AND tier='tier_1'
                 AND prize_pool_usd=2000000
                 AND main_event_start_at='2026-07-07T00:00:00+00:00'
                 AND main_event_end_at='2026-07-19T23:59:59+00:00'
                 AND opendota_league_id=19785
                 AND official_evidence_urls_json=?
                 AND expected_map_count IS NULL
                 AND observed_map_count=120
                 AND public_map_count=121
                 AND reconciliation_note=?""",
            (
                ewc["public_map_count"],
                ewc["reconciliation_note"],
                REGISTRY_UPDATED_AT,
                REGISTRY_UPDATED_AT,
                self._json(ewc["official_evidence_urls"]),
                interim_note,
            ),
        )

    def formal_events(self) -> tuple[RegisteredEvent, ...]:
        rows = self.connection.execute(
            """SELECT * FROM formal_events
               ORDER BY main_event_start_at, opendota_league_id"""
        ).fetchall()
        return tuple(self._registered_event(row) for row in rows)

    def get_by_league_id(self, opendota_league_id: int) -> RegisteredEvent | None:
        row = self.connection.execute(
            "SELECT * FROM formal_events WHERE opendota_league_id=?",
            (opendota_league_id,),
        ).fetchone()
        return self._registered_event(row) if row is not None else None

    def get_by_event_id(self, event_id: str) -> RegisteredEvent | None:
        row = self.connection.execute(
            "SELECT * FROM formal_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return self._registered_event(row) if row is not None else None

    def is_formal_league(self, opendota_league_id: int) -> bool:
        return self.get_by_league_id(opendota_league_id) is not None

    def discover_candidate(
        self,
        *,
        source: str,
        provider_event_id: str,
        canonical_name: str,
        evidence_urls: Sequence[str] = (),
        evidence: Mapping[str, Any] | None = None,
        discovered_at: datetime,
    ) -> int:
        observed_at = self._iso(discovered_at)
        with self._transaction():
            self.connection.execute(
                """INSERT INTO event_candidates
                (source, provider_event_id, canonical_name, evidence_urls_json,
                 evidence_status, evidence_json, audit_status, discovered_at,
                 last_seen_at)
                VALUES (?, ?, ?, ?, 'unverified', ?, 'pending', ?, ?)
                ON CONFLICT(source, provider_event_id) DO UPDATE SET
                    canonical_name=excluded.canonical_name,
                    evidence_urls_json=excluded.evidence_urls_json,
                    evidence_json=excluded.evidence_json,
                    last_seen_at=excluded.last_seen_at""",
                (
                    source,
                    provider_event_id,
                    canonical_name,
                    self._json(tuple(evidence_urls)),
                    self._json(evidence) if evidence is not None else None,
                    observed_at,
                    observed_at,
                ),
            )
            row = self.connection.execute(
                """SELECT candidate_id FROM event_candidates
                   WHERE source=? AND provider_event_id=?""",
                (source, provider_event_id),
            ).fetchone()
        assert row is not None
        return int(row["candidate_id"])

    def candidates(self) -> tuple[EventCandidate, ...]:
        rows = self.connection.execute(
            "SELECT * FROM event_candidates ORDER BY discovered_at, candidate_id"
        ).fetchall()
        return tuple(self._candidate(row) for row in rows)

    def formal_matches(self) -> tuple[FormalMatch, ...]:
        rows = self.connection.execute(
            "SELECT * FROM formal_map_eligibility ORDER BY match_id"
        ).fetchall()
        return tuple(
            FormalMatch(
                match_id=int(row["match_id"]),
                event_id=str(row["event_id"]),
                opendota_league_id=int(row["opendota_league_id"]),
                stage_scope=StageScope(row["stage_scope"]),
                ingest_state=IngestState(row["ingest_state"]),
                player_readiness=ComponentReadiness(row["player_readiness"]),
                state_readiness=ComponentReadiness(row["state_readiness"]),
                draft_readiness=ComponentReadiness(row["draft_readiness"]),
            )
            for row in rows
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self.storage.transaction():
            yield

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("discovered_at must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @classmethod
    def _registered_event(cls, row: DatabaseRow) -> RegisteredEvent:
        secondary = json.loads(row["secondary_provider_ids_json"])
        return RegisteredEvent(
            event_id=str(row["event_id"]),
            canonical_name=str(row["canonical_name"]),
            tier=str(row["tier"]),
            prize_pool_usd=int(row["prize_pool_usd"]),
            main_event_start_at=cls._datetime(row["main_event_start_at"]),
            main_event_end_at=cls._datetime(row["main_event_end_at"]),
            opendota_league_id=int(row["opendota_league_id"]),
            secondary_provider_ids=tuple(
                sorted((str(key), str(value)) for key, value in secondary.items())
            ),
            official_evidence_urls=tuple(
                json.loads(row["official_evidence_urls_json"])
            ),
            evidence_status=EvidenceStatus(row["evidence_status"]),
            scope_policy_version=str(row["scope_policy_version"]),
            scope=EventScope(row["scope"]),
            approval_status=ApprovalStatus(row["approval_status"]),
            approved_by=str(row["approved_by"]),
            approved_at=cls._datetime(row["approved_at"]),
            reconciliation_status=ReconciliationStatus(row["reconciliation_status"]),
            expected_map_count=row["expected_map_count"],
            observed_map_count=row["observed_map_count"],
            public_map_count=row["public_map_count"],
            reconciliation_note=row["reconciliation_note"],
            included_stages=tuple(
                StageScope(value) for value in json.loads(row["included_stages_json"])
            ),
            excluded_categories=tuple(json.loads(row["excluded_categories_json"])),
            include_internal_lcq=bool(row["include_internal_lcq"]),
        )

    @classmethod
    def _candidate(cls, row: DatabaseRow) -> EventCandidate:
        audit_status = row["audit_status"]
        approval = (
            ApprovalStatus.APPROVED
            if audit_status in ("approved", "promoted")
            else ApprovalStatus(audit_status)
        )
        return EventCandidate(
            candidate_id=int(row["candidate_id"]),
            source=str(row["source"]),
            provider_event_id=str(row["provider_event_id"]),
            canonical_name=str(row["canonical_name"]),
            evidence_urls=tuple(json.loads(row["evidence_urls_json"])),
            evidence_status=EvidenceStatus(row["evidence_status"]),
            approval_status=approval,
            discovered_at=cls._datetime(row["discovered_at"]),
            last_seen_at=cls._datetime(row["last_seen_at"]),
            promoted_event_id=row["promoted_event_id"],
        )
