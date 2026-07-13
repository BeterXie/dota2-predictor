"""Typed records shared by strict-event intelligence components."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EventScope(str, Enum):
    FORMAL_MAIN_EVENT = "formal_main_event"
    AUDIT_ONLY = "audit_only"
    EXCLUDED = "excluded"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class EvidenceStatus(str, Enum):
    MANUALLY_AUDITED = "manually_audited"
    UNVERIFIED = "unverified"


class StageScope(str, Enum):
    MAIN_EVENT = "main_event"
    INTERNAL_LCQ = "internal_lcq"
    QUALIFIER = "qualifier"
    DIVISION_2 = "division_2"
    EXHIBITION = "exhibition"
    UNKNOWN = "unknown"


class IngestState(str, Enum):
    DISCOVERED = "discovered"
    BASIC_RESULT = "basic_result"
    DETAIL_PENDING = "detail_pending"
    DETAILED = "detailed"
    CROSS_CHECKED = "cross_checked"
    COMPLETE = "complete"
    RETRYABLE = "retryable"
    FAILED = "failed"
    REVIEW_REQUIRED = "review_required"


class ComponentReadiness(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RETRYABLE = "retryable"
    UNSCORABLE = "unscorable"
    REVIEW_REQUIRED = "review_required"


class ArtifactSource(str, Enum):
    OPENDOTA = "opendota"
    STRATZ = "stratz"


class ArtifactUse(str, Enum):
    PRIMARY = "primary"
    FALLBACK = "fallback"
    CROSS_CHECK = "cross_check"


class RolePurpose(str, Enum):
    OBSERVED_POSITION = "observed_position"
    EXPECTED_POSITION = "expected_position"


class ReconciliationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "reconciliation_pending"
    RECONCILED = "reconciled"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True)
class ArtifactProvenance:
    source: ArtifactSource
    use: ArtifactUse
    endpoint: str
    request_identity: str
    source_at: datetime | None
    received_at: datetime
    first_usable_at: datetime | None
    schema_fingerprint: str
    event_id: str | None = None
    match_id: int | None = None


@dataclass(frozen=True)
class RegisteredEvent:
    event_id: str
    canonical_name: str
    tier: str
    prize_pool_usd: int
    main_event_start_at: datetime
    main_event_end_at: datetime
    opendota_league_id: int
    secondary_provider_ids: tuple[tuple[str, str], ...]
    official_evidence_urls: tuple[str, ...]
    evidence_status: EvidenceStatus
    scope_policy_version: str
    scope: EventScope
    approval_status: ApprovalStatus
    approved_by: str
    approved_at: datetime
    reconciliation_status: ReconciliationStatus
    expected_map_count: int | None
    observed_map_count: int | None
    public_map_count: int | None
    reconciliation_note: str | None
    included_stages: tuple[StageScope, ...]
    excluded_categories: tuple[str, ...]
    include_internal_lcq: bool


@dataclass(frozen=True)
class EventCandidate:
    candidate_id: int
    source: str
    provider_event_id: str
    canonical_name: str
    evidence_urls: tuple[str, ...]
    evidence_status: EvidenceStatus
    approval_status: ApprovalStatus
    discovered_at: datetime
    last_seen_at: datetime
    promoted_event_id: str | None = None


@dataclass(frozen=True)
class FormalMatch:
    match_id: int
    event_id: str
    opendota_league_id: int
    stage_scope: StageScope
    ingest_state: IngestState
    player_readiness: ComponentReadiness
    state_readiness: ComponentReadiness
    draft_readiness: ComponentReadiness
