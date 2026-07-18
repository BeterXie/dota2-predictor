"""Fail-closed eligibility for exact, audited RayBet Dota 2 map mappings.

No name, substring, or fuzzy match can create an accepted mapping. Acceptance
requires exact RayBet team IDs and order from the raw match payload plus
structured human-audited event, schedule, and stage evidence. Automatic exact
acceptance is available only after that exact evidence bundle has been approved
from an eligible manual mapping.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Sequence


STRICT_MAPPING_VERSION = "strict-live-map-v3"
MINIMUM_PRIZE_POOL_USD = 1_000_000


_TABLE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS strict_live_map_mappings (
        mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
        raybet_match_id TEXT NOT NULL,
        map_number INTEGER NOT NULL CHECK (map_number > 0),
        event_id TEXT NOT NULL REFERENCES event_registry(event_id),
        team_one_id INTEGER NOT NULL CHECK (team_one_id > 0),
        team_two_id INTEGER NOT NULL CHECK (team_two_id > 0),
        canonical_team_one_id INTEGER NOT NULL CHECK (canonical_team_one_id > 0),
        canonical_team_one_name TEXT NOT NULL,
        canonical_team_two_id INTEGER NOT NULL CHECK (canonical_team_two_id > 0),
        canonical_team_two_name TEXT NOT NULL,
        canonical_identity_json TEXT NOT NULL,
        canonical_identity_hash TEXT NOT NULL
            CHECK (length(canonical_identity_hash) = 64),
        crosswalk_evidence_json TEXT NOT NULL,
        crosswalk_evidence_hash TEXT NOT NULL
            CHECK (length(crosswalk_evidence_hash) = 64),
        stage_scope TEXT NOT NULL
            CHECK (stage_scope IN ('main_event', 'internal_lcq')),
        scheduled_at_utc TEXT NOT NULL,
        raybet_best_of INTEGER NOT NULL CHECK (raybet_best_of > 0),
        raybet_identity_json TEXT NOT NULL,
        raybet_identity_hash TEXT NOT NULL
            CHECK (length(raybet_identity_hash) = 64),
        raybet_metadata_updated_at TEXT NOT NULL,
        source TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        evidence_hash TEXT NOT NULL CHECK (length(evidence_hash) = 64),
        mapping_version TEXT NOT NULL,
        acceptance_mode TEXT NOT NULL DEFAULT 'manual_exact'
            CHECK (acceptance_mode IN ('manual_exact', 'automatic_exact')),
        automatic_approval_id INTEGER
            REFERENCES strict_live_automatic_evidence_approvals(approval_id),
        accepted_by TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK (team_one_id != team_two_id),
        CHECK (canonical_team_one_id != canonical_team_two_id),
        CHECK (map_number <= raybet_best_of)
    )""",
    """CREATE TABLE IF NOT EXISTS strict_live_map_mapping_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        raybet_match_id TEXT NOT NULL,
        map_number INTEGER NOT NULL CHECK (map_number > 0),
        proposed_event_id TEXT,
        proposed_team_one_id INTEGER,
        proposed_team_two_id INTEGER,
        proposed_canonical_team_one_id INTEGER,
        proposed_canonical_team_two_id INTEGER,
        match_method TEXT NOT NULL
            CHECK (match_method IN
                   ('manual_exact', 'automatic_exact', 'candidate', 'fuzzy')),
        decision TEXT NOT NULL
            CHECK (decision IN
                   ('accepted', 'idempotent', 'audit_only', 'conflict', 'rejected')),
        reason TEXT NOT NULL,
        source TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        evidence_hash TEXT NOT NULL CHECK (length(evidence_hash) = 64),
        mapping_version TEXT NOT NULL,
        actor TEXT,
        observed_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        raybet_identity_hash TEXT,
        raybet_metadata_updated_at TEXT,
        canonical_identity_hash TEXT,
        crosswalk_evidence_hash TEXT,
        mapping_id INTEGER REFERENCES strict_live_map_mappings(mapping_id),
        CHECK (
            (match_method IN ('candidate', 'fuzzy') AND decision = 'audit_only')
            OR (match_method IN ('manual_exact', 'automatic_exact')
                AND decision != 'audit_only')
        )
    )""",
    """CREATE TABLE IF NOT EXISTS strict_live_automatic_evidence_approvals (
        approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_mapping_id INTEGER NOT NULL UNIQUE
            REFERENCES strict_live_map_mappings(mapping_id),
        raybet_match_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        team_one_id INTEGER NOT NULL,
        team_two_id INTEGER NOT NULL,
        canonical_team_one_id INTEGER NOT NULL,
        canonical_team_two_id INTEGER NOT NULL,
        raybet_identity_hash TEXT NOT NULL CHECK (length(raybet_identity_hash)=64),
        canonical_identity_hash TEXT NOT NULL CHECK (length(canonical_identity_hash)=64),
        crosswalk_evidence_hash TEXT NOT NULL CHECK (length(crosswalk_evidence_hash)=64),
        evidence_hash TEXT NOT NULL CHECK (length(evidence_hash)=64),
        approved_by TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS strict_live_map_mapping_invalidations (
        invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        mapping_id INTEGER NOT NULL UNIQUE
            REFERENCES strict_live_map_mappings(mapping_id),
        reason TEXT NOT NULL,
        invalidated_by TEXT NOT NULL,
        invalidated_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS strict_live_map_mapping_supersessions (
        supersession_id INTEGER PRIMARY KEY AUTOINCREMENT,
        previous_mapping_id INTEGER NOT NULL UNIQUE
            REFERENCES strict_live_map_mappings(mapping_id),
        replacement_mapping_id INTEGER NOT NULL UNIQUE
            REFERENCES strict_live_map_mappings(mapping_id),
        recorded_at TEXT NOT NULL,
        CHECK (previous_mapping_id != replacement_mapping_id)
    )""",
    """CREATE TABLE IF NOT EXISTS strict_live_mapping_impacts (
        impact_id INTEGER PRIMARY KEY AUTOINCREMENT,
        mapping_id INTEGER NOT NULL REFERENCES strict_live_map_mappings(mapping_id),
        invalidation_id INTEGER NOT NULL
            REFERENCES strict_live_map_mapping_invalidations(invalidation_id),
        dependent_type TEXT NOT NULL CHECK (dependent_type IN
            ('strategy_decision', 'research_prediction', 'shadow_order')),
        dependent_key TEXT NOT NULL,
        reason TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        UNIQUE (mapping_id, dependent_type, dependent_key)
    )""",
)

_MAPPING_ADDITIVE_COLUMNS = {
    "canonical_team_one_id": "INTEGER",
    "canonical_team_one_name": "TEXT",
    "canonical_team_two_id": "INTEGER",
    "canonical_team_two_name": "TEXT",
    "canonical_identity_json": "TEXT",
    "canonical_identity_hash": "TEXT",
    "crosswalk_evidence_json": "TEXT",
    "crosswalk_evidence_hash": "TEXT",
    "stage_scope": "TEXT",
    "scheduled_at_utc": "TEXT",
    "raybet_best_of": "INTEGER",
    "raybet_identity_json": "TEXT",
    "raybet_identity_hash": "TEXT",
    "raybet_metadata_updated_at": "TEXT",
    "recorded_at": "TEXT",
    "automatic_approval_id": "INTEGER",
}

_AUDIT_ADDITIVE_COLUMNS = {
    "proposed_canonical_team_one_id": "INTEGER",
    "proposed_canonical_team_two_id": "INTEGER",
    "recorded_at": "TEXT",
    "raybet_identity_hash": "TEXT",
    "raybet_metadata_updated_at": "TEXT",
    "canonical_identity_hash": "TEXT",
    "crosswalk_evidence_hash": "TEXT",
}

_INDEX_TRIGGER_STATEMENTS = (
    """CREATE INDEX IF NOT EXISTS idx_strict_live_mapping_event
       ON strict_live_map_mappings(event_id, recorded_at)""",
    """CREATE INDEX IF NOT EXISTS idx_strict_live_mapping_key
       ON strict_live_map_mappings(raybet_match_id, map_number, mapping_id)""",
    """CREATE INDEX IF NOT EXISTS idx_strict_live_mapping_audit_key
       ON strict_live_map_mapping_audit(raybet_match_id, map_number, recorded_at)""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_map_mappings_no_update
       BEFORE UPDATE ON strict_live_map_mappings
       BEGIN
           SELECT RAISE(ABORT, 'accepted strict live mappings are immutable');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_map_mappings_no_delete
       BEFORE DELETE ON strict_live_map_mappings
       BEGIN
           SELECT RAISE(ABORT, 'accepted strict live mappings cannot be deleted');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_mapping_audit_no_update
       BEFORE UPDATE ON strict_live_map_mapping_audit
       BEGIN
           SELECT RAISE(ABORT, 'strict live mapping audit rows are immutable');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_mapping_audit_no_delete
       BEFORE DELETE ON strict_live_map_mapping_audit
       BEGIN
           SELECT RAISE(ABORT, 'strict live mapping audit rows cannot be deleted');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_automatic_approval_no_update
       BEFORE UPDATE ON strict_live_automatic_evidence_approvals
       BEGIN
           SELECT RAISE(ABORT, 'strict automatic evidence approvals are immutable');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_automatic_approval_no_delete
       BEFORE DELETE ON strict_live_automatic_evidence_approvals
       BEGIN
           SELECT RAISE(ABORT, 'strict automatic evidence approvals cannot be deleted');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_mapping_invalidation_no_update
       BEFORE UPDATE ON strict_live_map_mapping_invalidations
       BEGIN
           SELECT RAISE(ABORT, 'strict mapping invalidations are immutable');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_mapping_invalidation_no_delete
       BEFORE DELETE ON strict_live_map_mapping_invalidations
       BEGIN
           SELECT RAISE(ABORT, 'strict mapping invalidations cannot be deleted');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_mapping_supersession_no_update
       BEFORE UPDATE ON strict_live_map_mapping_supersessions
       BEGIN
           SELECT RAISE(ABORT, 'strict mapping supersessions are immutable');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_mapping_supersession_no_delete
       BEFORE DELETE ON strict_live_map_mapping_supersessions
       BEGIN
           SELECT RAISE(ABORT, 'strict mapping supersessions cannot be deleted');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_mapping_impacts_no_update
       BEFORE UPDATE ON strict_live_mapping_impacts
       BEGIN
           SELECT RAISE(ABORT, 'strict mapping impacts are immutable');
       END""",
    """CREATE TRIGGER IF NOT EXISTS strict_live_mapping_impacts_no_delete
       BEFORE DELETE ON strict_live_mapping_impacts
       BEGIN
           SELECT RAISE(ABORT, 'strict mapping impacts cannot be deleted');
       END""",
)

_DEPENDENT_IMPACT_TRIGGERS = {
    "strategy_decisions": """CREATE TRIGGER strict_live_strategy_impact_after_insert
       AFTER INSERT ON strategy_decisions
       WHEN json_valid(NEW.contributions_json)
       BEGIN
           INSERT OR IGNORE INTO strict_live_mapping_impacts
               (mapping_id, invalidation_id, dependent_type, dependent_key,
                reason, recorded_at)
           SELECT CAST(json_extract(
                      NEW.contributions_json,
                      '$.__inputs__.strict_live_eligibility.mapping_refs.strict_mapping_id'
                  ) AS INTEGER),
                  cause.invalidation_id, 'strategy_decision', NEW.decision_key,
                  cause.reason, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             FROM (
                 SELECT invalidation.invalidation_id, invalidation.reason
                   FROM strict_live_map_mapping_invalidations AS invalidation
                  WHERE invalidation.mapping_id=CAST(json_extract(
                            NEW.contributions_json,
                            '$.__inputs__.strict_live_eligibility.mapping_refs.strict_mapping_id'
                        ) AS INTEGER)
                 UNION ALL
                 SELECT invalidation.invalidation_id, invalidation.reason
                   FROM strict_live_map_mappings AS mapping
                   JOIN strict_live_automatic_evidence_approvals AS approval
                     ON approval.approval_id=mapping.automatic_approval_id
                   JOIN strict_live_map_mapping_invalidations AS invalidation
                     ON invalidation.mapping_id=approval.source_mapping_id
                  WHERE mapping.mapping_id=CAST(json_extract(
                            NEW.contributions_json,
                            '$.__inputs__.strict_live_eligibility.mapping_refs.strict_mapping_id'
                        ) AS INTEGER)
                 LIMIT 1
             ) AS cause;
       END""",
    "research_live_predictions": """CREATE TRIGGER strict_live_research_impact_after_insert
       AFTER INSERT ON research_live_predictions
       BEGIN
           INSERT OR IGNORE INTO strict_live_mapping_impacts
               (mapping_id, invalidation_id, dependent_type, dependent_key,
                reason, recorded_at)
           SELECT NEW.strict_mapping_id, cause.invalidation_id,
                  'research_prediction', NEW.prediction_key, cause.reason,
                  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             FROM (
                 SELECT invalidation.invalidation_id, invalidation.reason
                   FROM strict_live_map_mapping_invalidations AS invalidation
                  WHERE invalidation.mapping_id=NEW.strict_mapping_id
                 UNION ALL
                 SELECT invalidation.invalidation_id, invalidation.reason
                   FROM strict_live_map_mappings AS mapping
                   JOIN strict_live_automatic_evidence_approvals AS approval
                     ON approval.approval_id=mapping.automatic_approval_id
                   JOIN strict_live_map_mapping_invalidations AS invalidation
                     ON invalidation.mapping_id=approval.source_mapping_id
                  WHERE mapping.mapping_id=NEW.strict_mapping_id
                 LIMIT 1
             ) AS cause;
       END""",
    "shadow_orders": """CREATE TRIGGER strict_live_shadow_impact_after_insert
       AFTER INSERT ON shadow_orders
       WHEN NEW.strict_mapping_id IS NOT NULL
       BEGIN
           INSERT OR IGNORE INTO strict_live_mapping_impacts
               (mapping_id, invalidation_id, dependent_type, dependent_key,
                reason, recorded_at)
           SELECT NEW.strict_mapping_id, cause.invalidation_id,
                  'shadow_order', NEW.order_key, cause.reason,
                  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             FROM (
                 SELECT invalidation.invalidation_id, invalidation.reason
                   FROM strict_live_map_mapping_invalidations AS invalidation
                  WHERE invalidation.mapping_id=NEW.strict_mapping_id
                 UNION ALL
                 SELECT invalidation.invalidation_id, invalidation.reason
                   FROM strict_live_map_mappings AS mapping
                   JOIN strict_live_automatic_evidence_approvals AS approval
                     ON approval.approval_id=mapping.automatic_approval_id
                   JOIN strict_live_map_mapping_invalidations AS invalidation
                     ON invalidation.mapping_id=approval.source_mapping_id
                  WHERE mapping.mapping_id=NEW.strict_mapping_id
                 LIMIT 1
             ) AS cause;
       END""",
}


class StrictMappingError(ValueError):
    """Base error for a rejected strict mapping write."""


class StrictMappingConflictError(StrictMappingError):
    """Raised when an accepted RayBet map key is assigned a different value."""


class _FailClosed(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _RayBetIdentity:
    raybet_match_id: str
    tournament: str
    team_one_id: int
    team_one_name: str
    team_two_id: int
    team_two_name: str
    scheduled_at_raw: str
    best_of: int
    raw_stage: str | None
    metadata_updated_at: datetime
    identity_json: str
    identity_hash: str


@dataclass(frozen=True)
class _CanonicalIdentity:
    team_one_id: int
    team_one_name: str
    team_two_id: int
    team_two_name: str
    identity_json: str
    identity_hash: str


@dataclass(frozen=True)
class _EventPolicy:
    event_id: str
    canonical_name: str
    scope: str
    approval_status: str
    evidence_status: str
    tier: str
    prize_pool_usd: int | float
    approved_by: str | None
    approved_at: datetime
    main_event_start_at: datetime
    main_event_end_at: datetime
    official_evidence_urls: tuple[str, ...]
    included_stages: tuple[str, ...]
    excluded_categories: tuple[str, ...]
    include_internal_lcq: bool
    exclusion_flags: tuple[bool, ...]


@dataclass(frozen=True)
class _MappingEvidence:
    stage_scope: str
    scheduled_at_utc: datetime


@dataclass(frozen=True)
class StrictLiveMapMapping:
    mapping_id: int
    raybet_match_id: str
    map_number: int
    event_id: str
    team_one_id: int
    team_two_id: int
    canonical_team_one_id: int
    canonical_team_one_name: str
    canonical_team_two_id: int
    canonical_team_two_name: str
    canonical_identity_json: str
    canonical_identity_hash: str
    crosswalk_evidence_json: str
    crosswalk_evidence_hash: str
    stage_scope: str
    scheduled_at_utc: datetime
    raybet_best_of: int
    raybet_identity_json: str
    raybet_identity_hash: str
    raybet_metadata_updated_at: datetime
    source: str
    evidence_json: str
    evidence_hash: str
    mapping_version: str
    accepted_by: str
    accepted_at: datetime
    recorded_at: datetime
    acceptance_mode: str
    automatic_approval_id: int | None

    @property
    def raybet_team_one_id(self) -> int:
        """Explicit alias for the legacy raw-provider field name."""
        return self.team_one_id

    @property
    def raybet_team_two_id(self) -> int:
        """Explicit alias for the legacy raw-provider field name."""
        return self.team_two_id

    def input_refs(self) -> dict[str, str | int]:
        """Return stable, JSON-serializable decision provenance."""
        return {
            "strict_mapping_id": self.mapping_id,
            "strict_event_id": self.event_id,
            "strict_raybet_team_one_id": self.team_one_id,
            "strict_raybet_team_two_id": self.team_two_id,
            "strict_canonical_team_one_id": self.canonical_team_one_id,
            "strict_canonical_team_one_name": self.canonical_team_one_name,
            "strict_canonical_team_two_id": self.canonical_team_two_id,
            "strict_canonical_team_two_name": self.canonical_team_two_name,
            "strict_canonical_identity_hash": self.canonical_identity_hash,
            "strict_crosswalk_evidence_hash": self.crosswalk_evidence_hash,
            "strict_stage_scope": self.stage_scope,
            "strict_scheduled_at_utc": self.scheduled_at_utc.isoformat(),
            "strict_raybet_best_of": self.raybet_best_of,
            "strict_raybet_identity_hash": self.raybet_identity_hash,
            "strict_raybet_metadata_updated_at": (
                self.raybet_metadata_updated_at.isoformat()
            ),
            "strict_mapping_source": self.source,
            "strict_mapping_evidence_hash": self.evidence_hash,
            "strict_mapping_version": self.mapping_version,
            "strict_mapping_acceptance_mode": self.acceptance_mode,
            "strict_automatic_approval_id": self.automatic_approval_id or "",
            "strict_mapping_accepted_at": self.accepted_at.isoformat(),
            "strict_mapping_recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True)
class StrictLiveEligibility:
    eligible: bool
    reason: str
    raybet_match_id: str
    map_number: int
    transport_observed_at: datetime | None
    mapping: StrictLiveMapMapping | None = None

    @property
    def mapping_refs(self) -> dict[str, str | int]:
        return self.mapping.input_refs() if self.mapping is not None else {}

    def input_refs(self) -> dict[str, str | int]:
        return self.mapping_refs


def init_strict_live_eligibility_schema(
    connection: sqlite3.Connection,
    *,
    external_transaction: bool = False,
) -> None:
    """Install the additive schema without modifying existing accepted rows."""
    _migrate_exact_mapping_tables(
        connection,
        external_transaction=external_transaction,
    )
    with _write_transaction(connection):
        for statement in _TABLE_STATEMENTS:
            connection.execute(statement)
        _add_missing_columns(
            connection, "strict_live_map_mappings", _MAPPING_ADDITIVE_COLUMNS
        )
        _add_missing_columns(
            connection, "strict_live_map_mapping_audit", _AUDIT_ADDITIVE_COLUMNS
        )
        for statement in _INDEX_TRIGGER_STATEMENTS:
            connection.execute(statement)
        for table, statement in _DEPENDENT_IMPACT_TRIGGERS.items():
            if not _table_exists(connection, table):
                continue
            if table == "shadow_orders" and not _table_has_column(
                connection, table, "strict_mapping_id"
            ):
                continue
            trigger = {
                "strategy_decisions": "strict_live_strategy_impact_after_insert",
                "research_live_predictions": "strict_live_research_impact_after_insert",
                "shadow_orders": "strict_live_shadow_impact_after_insert",
            }[table]
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            connection.execute(statement)


def _migrate_exact_mapping_tables(
    connection: sqlite3.Connection,
    *,
    external_transaction: bool = False,
) -> None:
    """Rebuild the two v3 CHECK/UNIQUE constrained tables without changing rows."""
    if not strict_live_mapping_schema_requires_rebuild(connection):
        return
    audit_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='strict_live_map_mapping_audit'"
    ).fetchone() is not None
    with _exact_mapping_rebuild_transaction(
        connection,
        external_transaction=external_transaction,
    ):
        for trigger in (
            "strict_live_map_mappings_no_update",
            "strict_live_map_mappings_no_delete",
            "strict_live_mapping_audit_no_update",
            "strict_live_mapping_audit_no_delete",
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("DROP TABLE IF EXISTS strict_live_map_mappings_v4")
        connection.execute(
            _TABLE_STATEMENTS[0].replace(
                "CREATE TABLE IF NOT EXISTS strict_live_map_mappings",
                "CREATE TABLE strict_live_map_mappings_v4",
                1,
            )
        )
        mapping_columns = (
            "mapping_id, raybet_match_id, map_number, event_id, team_one_id, "
            "team_two_id, canonical_team_one_id, canonical_team_one_name, "
            "canonical_team_two_id, canonical_team_two_name, canonical_identity_json, "
            "canonical_identity_hash, crosswalk_evidence_json, crosswalk_evidence_hash, "
            "stage_scope, scheduled_at_utc, raybet_best_of, raybet_identity_json, "
            "raybet_identity_hash, raybet_metadata_updated_at, source, evidence_json, "
            "evidence_hash, mapping_version, acceptance_mode, accepted_by, accepted_at, "
            "recorded_at, created_at"
        )
        connection.execute(
            f"""INSERT INTO strict_live_map_mappings_v4 ({mapping_columns}, automatic_approval_id)
                SELECT {mapping_columns}, NULL FROM strict_live_map_mappings"""
        )

        if audit_exists:
            connection.execute("DROP TABLE IF EXISTS strict_live_map_mapping_audit_v4")
            connection.execute(
                _TABLE_STATEMENTS[1].replace(
                    "CREATE TABLE IF NOT EXISTS strict_live_map_mapping_audit",
                    "CREATE TABLE strict_live_map_mapping_audit_v4",
                    1,
                )
            )
            audit_columns = (
                "audit_id, raybet_match_id, map_number, proposed_event_id, "
                "proposed_team_one_id, proposed_team_two_id, "
                "proposed_canonical_team_one_id, proposed_canonical_team_two_id, "
                "match_method, decision, reason, source, evidence_json, evidence_hash, "
                "mapping_version, actor, observed_at, recorded_at, raybet_identity_hash, "
                "raybet_metadata_updated_at, canonical_identity_hash, "
                "crosswalk_evidence_hash, mapping_id"
            )
            connection.execute(
                f"""INSERT INTO strict_live_map_mapping_audit_v4 ({audit_columns})
                    SELECT {audit_columns} FROM strict_live_map_mapping_audit"""
            )
            connection.execute("DROP TABLE strict_live_map_mapping_audit")
        connection.execute("DROP TABLE strict_live_map_mappings")
        connection.execute(
            "ALTER TABLE strict_live_map_mappings_v4 RENAME TO strict_live_map_mappings"
        )
        if audit_exists:
            connection.execute(
                "ALTER TABLE strict_live_map_mapping_audit_v4 "
                "RENAME TO strict_live_map_mapping_audit"
            )


def strict_live_mapping_schema_requires_rebuild(
    connection: sqlite3.Connection,
) -> bool:
    """Return whether the pre-v4 exact-mapping tables need an FK-off rebuild."""

    mapping_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='strict_live_map_mappings'"
    ).fetchone()
    if mapping_sql_row is None:
        return False
    mapping_sql = str(mapping_sql_row[0] or "")
    return not (
        "automatic_exact" in mapping_sql
        and "UNIQUE (raybet_match_id, map_number)" not in mapping_sql
    )


@contextmanager
def _exact_mapping_rebuild_transaction(
    connection: sqlite3.Connection,
    *,
    external_transaction: bool,
) -> Iterator[None]:
    if external_transaction:
        if not connection.in_transaction:
            raise StrictMappingError(
                "strict_mapping_schema_migration_requires_external_transaction"
            )
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise StrictMappingError(
                "strict_mapping_schema_migration_requires_foreign_keys_off"
            )
        yield
        return

    if connection.in_transaction:
        raise StrictMappingError(
            "strict_mapping_schema_migration_requires_clean_transaction"
        )
    foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")


def accept_strict_live_map_mapping(
    connection: sqlite3.Connection,
    *,
    raybet_match_id: str,
    map_number: int,
    event_id: str,
    team_one_id: int,
    team_two_id: int,
    canonical_team_one_id: int,
    canonical_team_two_id: int,
    source: str,
    evidence: Mapping[str, Any],
    accepted_by: str,
    accepted_at: datetime,
    mapping_version: str = STRICT_MAPPING_VERSION,
    acceptance_mode: str = "manual_exact",
) -> StrictLiveMapMapping:
    """Persist one immutable mapping after exact metadata and policy checks.

    ``team_one_id`` and ``team_two_id`` are retained compatibility names for
    the exact raw RayBet IDs. Canonical intelligence IDs are separate required
    arguments. ``recorded_at`` is generated inside this function and is the causal
    availability boundary. A caller-supplied, backdated ``accepted_at`` cannot
    make a mapping available to an earlier transport.
    """
    recorded_at = _aware_utc(_utc_now(), "recorded_at")
    values = _validated_mapping_values(
        raybet_match_id=raybet_match_id,
        map_number=map_number,
        event_id=event_id,
        team_one_id=team_one_id,
        team_two_id=team_two_id,
        canonical_team_one_id=canonical_team_one_id,
        canonical_team_two_id=canonical_team_two_id,
        source=source,
        evidence=evidence,
        accepted_by=accepted_by,
        accepted_at=accepted_at,
        mapping_version=mapping_version,
        acceptance_mode=acceptance_mode,
        recorded_at=recorded_at,
    )
    if values["accepted_at"] > recorded_at:
        raise StrictMappingError("accepted_at_in_future")

    error: StrictMappingError | None = None
    result: StrictLiveMapMapping | None = None
    with _write_transaction(connection):
        rows = _mapping_rows(
            connection, values["raybet_match_id"], values["map_number"]
        )
        superseded_mapping_id = _latest_invalidated_mapping_id(
            connection, values["raybet_match_id"], values["map_number"]
        )
        if len(rows) > 1:
            _insert_audit(
                connection, values, "conflict", "accepted_mapping_ambiguous", None
            )
            error = StrictMappingConflictError("accepted_mapping_ambiguous")
        elif rows:
            try:
                existing = _mapping_from_row(rows[0])
            except _FailClosed as failure:
                _insert_audit(
                    connection, values, "conflict", failure.reason, None
                )
                error = StrictMappingConflictError(failure.reason)
            else:
                if values["acceptance_mode"] == "automatic_exact":
                    try:
                        values["automatic_approval_id"] = _automatic_approval_id(
                            connection,
                            {
                                **values,
                                "raybet_identity_hash": existing.raybet_identity_hash,
                                "canonical_identity_hash": (
                                    existing.canonical_identity_hash
                                ),
                                "crosswalk_evidence_hash": (
                                    existing.crosswalk_evidence_hash
                                ),
                            },
                            recorded_at,
                        )
                    except _FailClosed as failure:
                        _insert_audit(
                            connection, values, "rejected", failure.reason, None
                        )
                        error = StrictMappingError(failure.reason)
                if error is None and _is_same_mapping_value(rows[0], values):
                    _insert_audit(
                        connection,
                        values,
                        "idempotent",
                        "same_value_already_accepted",
                        existing.mapping_id,
                    )
                    result = existing
                elif error is None:
                    _insert_audit(
                        connection,
                        values,
                        "conflict",
                        "accepted_mapping_rebind_forbidden",
                        existing.mapping_id,
                    )
                    error = StrictMappingConflictError(
                        "accepted_mapping_rebind_forbidden"
                    )
        else:
            try:
                identity = _read_raybet_identity(
                    connection, values["raybet_match_id"]
                )
                values.update(
                    {
                        "raybet_identity_json": identity.identity_json,
                        "raybet_identity_hash": identity.identity_hash,
                        "raybet_metadata_updated_at": identity.metadata_updated_at,
                        "raybet_metadata_updated_at_iso": (
                            identity.metadata_updated_at.isoformat()
                        ),
                    }
                )
                _validate_exact_teams(values, identity)
                canonical = _read_canonical_identity(
                    connection,
                    values["canonical_team_one_id"],
                    values["canonical_team_two_id"],
                )
                crosswalk_json, crosswalk_hash = _validate_crosswalk_evidence(
                    values["evidence"], identity, canonical
                )
                values.update(
                    {
                        "canonical_team_one_name": canonical.team_one_name,
                        "canonical_team_two_name": canonical.team_two_name,
                        "canonical_identity_json": canonical.identity_json,
                        "canonical_identity_hash": canonical.identity_hash,
                        "crosswalk_evidence_json": crosswalk_json,
                        "crosswalk_evidence_hash": crosswalk_hash,
                    }
                )
                if values["map_number"] > identity.best_of:
                    raise _FailClosed("map_number_exceeds_best_of")
                if identity.metadata_updated_at > recorded_at:
                    raise _FailClosed("raybet_metadata_from_future")

                event = _read_event_policy(connection, values["event_id"])
                reason = _formal_event_reason(event, recorded_at)
                if reason is not None:
                    raise _FailClosed(reason)
                mapping_evidence = _validate_mapping_evidence(
                    values["evidence"], identity, event
                )
                values.update(
                    {
                        "stage_scope": mapping_evidence.stage_scope,
                        "scheduled_at_utc": mapping_evidence.scheduled_at_utc,
                        "scheduled_at_utc_iso": (
                            mapping_evidence.scheduled_at_utc.isoformat()
                        ),
                        "raybet_best_of": identity.best_of,
                    }
                )
                if values["acceptance_mode"] == "automatic_exact":
                    values["automatic_approval_id"] = _automatic_approval_id(
                        connection, values, recorded_at
                    )
            except _FailClosed as failure:
                _insert_audit(
                    connection, values, "rejected", failure.reason, None
                )
                error = StrictMappingError(failure.reason)
            else:
                mapping_id = _insert_mapping(connection, values)
                _insert_audit(
                    connection,
                    values,
                    "accepted",
                    f"{values['acceptance_mode']}_mapping_accepted",
                    mapping_id,
                    match_method=values["acceptance_mode"],
                )
                if superseded_mapping_id is not None:
                    connection.execute(
                        """INSERT INTO strict_live_map_mapping_supersessions
                           (previous_mapping_id, replacement_mapping_id, recorded_at)
                           VALUES (?, ?, ?)""",
                        (superseded_mapping_id, mapping_id, values["recorded_at_iso"]),
                    )
                result = _mapping_from_values(mapping_id, values)

    if error is not None:
        raise error
    assert result is not None
    return result


def approve_automatic_exact_evidence(
    connection: sqlite3.Connection,
    *,
    source_mapping_id: int,
    approved_by: str,
    approved_at: datetime,
) -> int:
    """Approve one already eligible manual exact evidence bundle for automation."""
    source_mapping_id = _positive_integer(source_mapping_id, "source_mapping_id")
    approved_by = _required_text(approved_by, "approved_by")
    approved_at = _aware_utc(approved_at, "approved_at")
    recorded_at = _aware_utc(_utc_now(), "recorded_at")
    if approved_at > recorded_at:
        raise StrictMappingError("approved_at_in_future")
    row = _mapping_row_by_id(connection, source_mapping_id)
    if row is None:
        raise StrictMappingError("source_mapping_missing")
    try:
        mapping = _mapping_from_row(row)
    except _FailClosed as failure:
        raise StrictMappingError(failure.reason) from failure
    if mapping.acceptance_mode != "manual_exact":
        raise StrictMappingError("automatic_approval_requires_manual_exact_source")
    causal_reason = _approval_source_causal_reason(
        approval_approved_at=approved_at,
        approval_recorded_at=recorded_at,
        source_accepted_at=mapping.accepted_at,
        source_recorded_at=mapping.recorded_at,
        source_metadata_updated_at=mapping.raybet_metadata_updated_at,
    )
    if causal_reason is not None:
        raise StrictMappingError(causal_reason)
    eligible = query_strict_live_eligibility(
        connection,
        raybet_match_id=mapping.raybet_match_id,
        map_number=mapping.map_number,
        transport_observed_at=recorded_at,
    )
    if not eligible.eligible or eligible.mapping is None:
        raise StrictMappingError(f"source_mapping_not_eligible:{eligible.reason}")
    if eligible.mapping.mapping_id != mapping.mapping_id:
        raise StrictMappingError("source_mapping_superseded")

    with _write_transaction(connection):
        existing = connection.execute(
            """SELECT approval_id FROM strict_live_automatic_evidence_approvals
                WHERE source_mapping_id=?""",
            (mapping.mapping_id,),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        cursor = connection.execute(
            """INSERT INTO strict_live_automatic_evidence_approvals
               (source_mapping_id, raybet_match_id, event_id, team_one_id,
                team_two_id, canonical_team_one_id, canonical_team_two_id,
                raybet_identity_hash, canonical_identity_hash,
                crosswalk_evidence_hash, evidence_hash, approved_by, approved_at,
                recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mapping.mapping_id,
                mapping.raybet_match_id,
                mapping.event_id,
                mapping.team_one_id,
                mapping.team_two_id,
                mapping.canonical_team_one_id,
                mapping.canonical_team_two_id,
                mapping.raybet_identity_hash,
                mapping.canonical_identity_hash,
                mapping.crosswalk_evidence_hash,
                mapping.evidence_hash,
                approved_by,
                approved_at.isoformat(),
                recorded_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)


def get_strict_live_map_mapping(
    connection: sqlite3.Connection, mapping_id: int
) -> StrictLiveMapMapping | None:
    """Load one immutable mapping by identity, including invalidated history."""
    row = _mapping_row_by_id(
        connection, _positive_integer(mapping_id, "mapping_id")
    )
    if row is None:
        return None
    try:
        return _mapping_from_row(row)
    except _FailClosed as failure:
        raise StrictMappingError(failure.reason) from failure


def invalidate_strict_live_map_mapping(
    connection: sqlite3.Connection,
    *,
    mapping_id: int,
    reason: str,
    invalidated_by: str,
    invalidated_at: datetime,
) -> int:
    """Append an invalidation and atomically quarantine impacted order outputs."""
    mapping_id = _positive_integer(mapping_id, "mapping_id")
    reason = _required_text(reason, "reason")
    invalidated_by = _required_text(invalidated_by, "invalidated_by")
    invalidated_at = _aware_utc(invalidated_at, "invalidated_at")
    recorded_at = _aware_utc(_utc_now(), "recorded_at")
    if invalidated_at > recorded_at:
        raise StrictMappingError("invalidated_at_in_future")
    row = _mapping_row_by_id(connection, mapping_id)
    if row is None:
        raise StrictMappingError("mapping_missing")
    try:
        mapping = _mapping_from_row(row)
    except _FailClosed as failure:
        raise StrictMappingError(failure.reason) from failure

    with _write_transaction(connection):
        existing = connection.execute(
            """SELECT invalidation_id FROM strict_live_map_mapping_invalidations
                WHERE mapping_id=?""",
            (mapping_id,),
        ).fetchone()
        if existing is not None:
            invalidation_id = int(existing[0])
            _quarantine_mapping_order_dependents(
                connection,
                invalidation_id=invalidation_id,
                recorded_at=recorded_at,
            )
            return invalidation_id
        cursor = connection.execute(
            """INSERT INTO strict_live_map_mapping_invalidations
               (mapping_id, reason, invalidated_by, invalidated_at, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                mapping_id,
                reason,
                invalidated_by,
                invalidated_at.isoformat(),
                recorded_at.isoformat(),
            ),
        )
        invalidation_id = int(cursor.lastrowid)
        _record_mapping_impacts(
            connection,
            mapping=mapping,
            invalidation_id=invalidation_id,
            reason=reason,
            recorded_at=recorded_at,
        )
        if mapping.acceptance_mode == "manual_exact":
            for automatic_mapping in _automatic_mappings_for_source(
                connection, mapping.mapping_id
            ):
                _record_mapping_impacts(
                    connection,
                    mapping=automatic_mapping,
                    invalidation_id=invalidation_id,
                    reason=reason,
                    recorded_at=recorded_at,
                )
        _quarantine_mapping_order_dependents(
            connection,
            invalidation_id=invalidation_id,
            recorded_at=recorded_at,
        )
        return invalidation_id


def record_strict_live_mapping_candidate(
    connection: sqlite3.Connection,
    *,
    raybet_match_id: str,
    map_number: int,
    source: str,
    evidence: Mapping[str, Any],
    observed_at: datetime,
    match_method: str,
    proposed_event_id: str | None = None,
    proposed_team_one_id: int | None = None,
    proposed_team_two_id: int | None = None,
    proposed_canonical_team_one_id: int | None = None,
    proposed_canonical_team_two_id: int | None = None,
    actor: str | None = None,
    mapping_version: str = STRICT_MAPPING_VERSION,
) -> int:
    """Record candidate/fuzzy evidence without creating an accepted mapping."""
    raybet_match_id = _required_text(raybet_match_id, "raybet_match_id")
    map_number = _positive_integer(map_number, "map_number")
    source = _required_text(source, "source")
    mapping_version = _required_text(mapping_version, "mapping_version")
    observed_at = _aware_utc(observed_at, "observed_at")
    recorded_at = _aware_utc(_utc_now(), "recorded_at")
    if match_method not in {"candidate", "fuzzy"}:
        raise StrictMappingError("candidate match_method must be candidate or fuzzy")
    if proposed_event_id is not None:
        proposed_event_id = _required_text(proposed_event_id, "proposed_event_id")
    if proposed_team_one_id is not None:
        proposed_team_one_id = _positive_integer(
            proposed_team_one_id, "proposed_team_one_id"
        )
    if proposed_team_two_id is not None:
        proposed_team_two_id = _positive_integer(
            proposed_team_two_id, "proposed_team_two_id"
        )
    if proposed_canonical_team_one_id is not None:
        proposed_canonical_team_one_id = _positive_integer(
            proposed_canonical_team_one_id, "proposed_canonical_team_one_id"
        )
    if proposed_canonical_team_two_id is not None:
        proposed_canonical_team_two_id = _positive_integer(
            proposed_canonical_team_two_id, "proposed_canonical_team_two_id"
        )
    evidence_json, evidence_hash = _canonical_evidence(evidence)
    values: dict[str, Any] = {
        "raybet_match_id": raybet_match_id,
        "map_number": map_number,
        "event_id": proposed_event_id,
        "team_one_id": proposed_team_one_id,
        "team_two_id": proposed_team_two_id,
        "canonical_team_one_id": proposed_canonical_team_one_id,
        "canonical_team_two_id": proposed_canonical_team_two_id,
        "source": source,
        "evidence": evidence,
        "evidence_json": evidence_json,
        "evidence_hash": evidence_hash,
        "mapping_version": mapping_version,
        "accepted_by": actor,
        "accepted_at_iso": observed_at.isoformat(),
        "recorded_at_iso": recorded_at.isoformat(),
    }
    with _write_transaction(connection):
        return _insert_audit(
            connection,
            values,
            "audit_only",
            f"{match_method}_mapping_never_auto_accepted",
            None,
            match_method=match_method,
        )


def query_strict_live_eligibility(
    connection: sqlite3.Connection,
    *,
    raybet_match_id: str,
    map_number: int,
    transport_observed_at: datetime,
) -> StrictLiveEligibility:
    """Return eligibility using SELECTs and transport-time causal boundaries."""
    clean_match_id = str(raybet_match_id).strip() if raybet_match_id is not None else ""
    if not clean_match_id:
        return _ineligible("invalid_raybet_match_id", clean_match_id, map_number, None)
    if isinstance(map_number, bool) or not isinstance(map_number, int) or map_number <= 0:
        return _ineligible("invalid_map_number", clean_match_id, map_number, None)
    try:
        transport_at = _aware_utc(transport_observed_at, "transport_observed_at")
    except StrictMappingError:
        return _ineligible("invalid_transport_time", clean_match_id, map_number, None)

    try:
        rows = _mapping_rows(connection, clean_match_id, map_number)
    except sqlite3.OperationalError:
        return _ineligible(
            "strict_mapping_schema_missing", clean_match_id, map_number, transport_at
        )
    if not rows:
        invalidated_row = _latest_invalidated_mapping_row(
            connection, clean_match_id, map_number
        )
        if invalidated_row is not None:
            try:
                invalidated_mapping = _mapping_from_row(invalidated_row)
            except _FailClosed:
                invalidated_mapping = None
            return _ineligible(
                "mapping_invalidated",
                clean_match_id,
                map_number,
                transport_at,
                invalidated_mapping,
            )
        return _ineligible(
            "accepted_mapping_missing", clean_match_id, map_number, transport_at
        )
    if len(rows) != 1:
        return _ineligible(
            "accepted_mapping_ambiguous", clean_match_id, map_number, transport_at
        )
    try:
        mapping = _mapping_from_row(rows[0])
    except _FailClosed as failure:
        return _ineligible(failure.reason, clean_match_id, map_number, transport_at)

    causal_reason = _mapping_causal_reason(mapping, transport_at)
    if causal_reason is not None:
        return _ineligible(
            causal_reason, clean_match_id, map_number, transport_at, mapping
        )
    approval_reason = _automatic_mapping_approval_reason(
        connection, mapping, transport_at
    )
    if approval_reason is not None:
        return _ineligible(
            approval_reason, clean_match_id, map_number, transport_at, mapping
        )
    try:
        identity = _read_raybet_identity(connection, clean_match_id)
    except sqlite3.OperationalError:
        return _ineligible(
            "raybet_metadata_schema_missing",
            clean_match_id,
            map_number,
            transport_at,
            mapping,
        )
    except _FailClosed as failure:
        return _ineligible(
            failure.reason, clean_match_id, map_number, transport_at, mapping
        )
    integrity_reason = _mapping_identity_reason(mapping, identity)
    if integrity_reason is not None:
        return _ineligible(
            integrity_reason, clean_match_id, map_number, transport_at, mapping
        )
    try:
        canonical = _read_canonical_identity(
            connection,
            mapping.canonical_team_one_id,
            mapping.canonical_team_two_id,
        )
    except sqlite3.OperationalError:
        return _ineligible(
            "canonical_teams_schema_missing",
            clean_match_id,
            map_number,
            transport_at,
            mapping,
        )
    except _FailClosed as failure:
        return _ineligible(
            failure.reason, clean_match_id, map_number, transport_at, mapping
        )
    canonical_reason = _canonical_identity_reason(mapping, canonical)
    if canonical_reason is not None:
        return _ineligible(
            canonical_reason, clean_match_id, map_number, transport_at, mapping
        )

    try:
        event = _read_event_policy(connection, mapping.event_id)
    except sqlite3.OperationalError:
        return _ineligible(
            "event_registry_schema_missing",
            clean_match_id,
            map_number,
            transport_at,
            mapping,
        )
    except _FailClosed as failure:
        return _ineligible(
            failure.reason, clean_match_id, map_number, transport_at, mapping
        )
    event_reason = _formal_event_reason(event, transport_at)
    if event_reason is not None:
        return _ineligible(
            event_reason, clean_match_id, map_number, transport_at, mapping
        )
    try:
        evidence = json.loads(mapping.evidence_json)
        if not isinstance(evidence, dict):
            raise _FailClosed("mapping_evidence_invalid")
        crosswalk_json, crosswalk_hash = _validate_crosswalk_evidence(
            evidence, identity, canonical
        )
        if (
            crosswalk_json != mapping.crosswalk_evidence_json
            or crosswalk_hash != mapping.crosswalk_evidence_hash
        ):
            raise _FailClosed("crosswalk_evidence_snapshot_drift")
        checked = _validate_mapping_evidence(evidence, identity, event)
    except (json.JSONDecodeError, _FailClosed) as failure:
        reason = (
            failure.reason
            if isinstance(failure, _FailClosed)
            else "mapping_evidence_invalid"
        )
        return _ineligible(reason, clean_match_id, map_number, transport_at, mapping)
    if checked.stage_scope != mapping.stage_scope:
        return _ineligible(
            "mapping_stage_snapshot_drift",
            clean_match_id,
            map_number,
            transport_at,
            mapping,
        )
    if checked.scheduled_at_utc != mapping.scheduled_at_utc:
        return _ineligible(
            "mapping_schedule_snapshot_drift",
            clean_match_id,
            map_number,
            transport_at,
            mapping,
        )
    return StrictLiveEligibility(
        True, "eligible", clean_match_id, map_number, transport_at, mapping
    )


check_strict_live_eligibility = query_strict_live_eligibility


def query_strict_mapping_snapshot(
    connection: sqlite3.Connection,
    *,
    mapping_id: int,
    observed_at: datetime,
) -> StrictLiveEligibility:
    """Verify immutable mapping authority at a historical observation cutoff."""

    if type(mapping_id) is not int or mapping_id <= 0:
        return _ineligible("invalid_mapping_id", "", 0, None)
    try:
        cutoff = _aware_utc(observed_at, "observed_at")
    except StrictMappingError:
        return _ineligible("invalid_transport_time", "", 0, None)
    try:
        row = _mapping_row_by_id(connection, mapping_id)
    except sqlite3.OperationalError:
        return _ineligible("strict_mapping_schema_missing", "", 0, cutoff)
    if row is None:
        return _ineligible("accepted_mapping_missing", "", 0, cutoff)
    try:
        mapping = _mapping_from_row(row)
    except _FailClosed as failure:
        return _ineligible(failure.reason, "", 0, cutoff)

    causal_reason = _mapping_causal_reason(mapping, cutoff)
    if causal_reason is not None:
        return _ineligible(
            causal_reason,
            mapping.raybet_match_id,
            mapping.map_number,
            cutoff,
            mapping,
        )
    if not (
        mapping.raybet_metadata_updated_at <= mapping.recorded_at
        and mapping.accepted_at <= mapping.recorded_at
        and mapping.map_number <= mapping.raybet_best_of
    ):
        return _ineligible(
            "mapping_causal_order_invalid",
            mapping.raybet_match_id,
            mapping.map_number,
            cutoff,
            mapping,
        )

    snapshots = (
        (
            mapping.raybet_identity_json,
            mapping.raybet_identity_hash,
            "mapping_identity_hash_invalid",
        ),
        (
            mapping.canonical_identity_json,
            mapping.canonical_identity_hash,
            "canonical_identity_hash_invalid",
        ),
        (
            mapping.crosswalk_evidence_json,
            mapping.crosswalk_evidence_hash,
            "crosswalk_evidence_hash_invalid",
        ),
        (
            mapping.evidence_json,
            mapping.evidence_hash,
            "mapping_evidence_hash_invalid",
        ),
    )
    for payload, expected_hash, reason in snapshots:
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != expected_hash:
            return _ineligible(
                reason,
                mapping.raybet_match_id,
                mapping.map_number,
                cutoff,
                mapping,
            )
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            decoded = None
        if not isinstance(decoded, dict):
            return _ineligible(
                "mapping_identity_snapshot_invalid",
                mapping.raybet_match_id,
                mapping.map_number,
                cutoff,
                mapping,
            )

    try:
        invalidation = connection.execute(
            """SELECT invalidated_at
                 FROM strict_live_map_mapping_invalidations
                WHERE mapping_id=?""",
            (mapping_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return _ineligible(
            "strict_mapping_schema_missing",
            mapping.raybet_match_id,
            mapping.map_number,
            cutoff,
            mapping,
        )
    if invalidation is not None:
        try:
            invalidated_at = _parse_timestamp(str(_value(invalidation, 0, "invalidated_at")))
        except (TypeError, ValueError):
            return _ineligible(
                "mapping_invalidation_time_invalid",
                mapping.raybet_match_id,
                mapping.map_number,
                cutoff,
                mapping,
            )
        if invalidated_at <= cutoff:
            return _ineligible(
                "mapping_invalidated",
                mapping.raybet_match_id,
                mapping.map_number,
                cutoff,
                mapping,
            )

    approval_reason = _historical_automatic_mapping_approval_reason(
        connection, mapping, cutoff
    )
    if approval_reason is not None:
        return _ineligible(
            approval_reason,
            mapping.raybet_match_id,
            mapping.map_number,
            cutoff,
            mapping,
        )
    return StrictLiveEligibility(
        True,
        "historical_mapping_snapshot_verified",
        mapping.raybet_match_id,
        mapping.map_number,
        cutoff,
        mapping,
    )


def _read_raybet_identity(
    connection: sqlite3.Connection, raybet_match_id: str
) -> _RayBetIdentity:
    row = connection.execute(
        """SELECT tournament, team_one, team_two, scheduled_at, best_of,
                  raw_json, updated_at
           FROM raybet_matches WHERE raybet_match_id=?""",
        (raybet_match_id,),
    ).fetchone()
    if row is None:
        raise _FailClosed("raybet_metadata_missing")
    tournament = _row_text(row, 0, "tournament", "raybet_tournament_missing")
    row_team_one = _row_text(row, 1, "team_one", "raybet_team_metadata_incomplete")
    row_team_two = _row_text(row, 2, "team_two", "raybet_team_metadata_incomplete")
    scheduled_at = _row_text(
        row, 3, "scheduled_at", "raybet_scheduled_at_missing"
    )
    best_of = _strict_positive_int(_value(row, 4, "best_of"))
    if best_of is None:
        raise _FailClosed("raybet_best_of_missing")
    raw_text = _row_text(row, 5, "raw_json", "raybet_raw_metadata_missing")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise _FailClosed("raybet_raw_metadata_invalid") from error
    if not isinstance(raw, dict):
        raise _FailClosed("raybet_raw_metadata_invalid")
    if str(raw.get("id") or "") != raybet_match_id:
        raise _FailClosed("raybet_raw_match_id_mismatch")
    if type(raw.get("game_id")) is not int or raw["game_id"] != 151:
        raise _FailClosed("raybet_not_dota2")
    if raw.get("tournament_name") != tournament:
        raise _FailClosed("raybet_tournament_metadata_conflict")
    if raw.get("start_time") != scheduled_at:
        raise _FailClosed("raybet_schedule_metadata_conflict")
    if raw.get("round") != f"bo{best_of}":
        raise _FailClosed("raybet_best_of_metadata_conflict")

    teams = raw.get("team")
    if not isinstance(teams, list) or len(teams) != 2:
        raise _FailClosed("raybet_exact_team_metadata_missing")
    by_position: dict[int, Mapping[str, Any]] = {}
    for team in teams:
        if not isinstance(team, Mapping) or type(team.get("pos")) is not int:
            raise _FailClosed("raybet_exact_team_metadata_missing")
        position = team["pos"]
        if position not in {1, 2} or position in by_position:
            raise _FailClosed("raybet_exact_team_order_invalid")
        by_position[position] = team
    if set(by_position) != {1, 2}:
        raise _FailClosed("raybet_exact_team_order_invalid")
    one, two = by_position[1], by_position[2]
    one_id = _strict_positive_int(one.get("team_id"))
    two_id = _strict_positive_int(two.get("team_id"))
    if one_id is None or two_id is None:
        raise _FailClosed("raybet_exact_team_ids_missing")
    if one_id == two_id:
        raise _FailClosed("raybet_exact_team_ids_conflict")
    one_name = str(one.get("team_name") or "").strip()
    two_name = str(two.get("team_name") or "").strip()
    if not one_name or not two_name:
        raise _FailClosed("raybet_exact_team_names_missing")
    if one_name != row_team_one or two_name != row_team_two:
        raise _FailClosed("raybet_team_order_metadata_conflict")
    try:
        metadata_updated_at = _parse_timestamp(
            _row_text(row, 6, "updated_at", "raybet_metadata_time_missing")
        )
    except ValueError as error:
        raise _FailClosed("raybet_metadata_time_invalid") from error

    raw_stage_value = raw.get("stage")
    raw_stage = (
        str(raw_stage_value).strip() if raw_stage_value is not None else None
    )
    identity = {
        "best_of": best_of,
        "game_id": 151,
        "raybet_match_id": raybet_match_id,
        "raw_stage": raw_stage or None,
        "scheduled_at": scheduled_at,
        "team_one": {"name": one_name, "pos": 1, "team_id": one_id},
        "team_two": {"name": two_name, "pos": 2, "team_id": two_id},
        "tournament": tournament,
    }
    identity_json, identity_hash = _canonical_json(identity)
    return _RayBetIdentity(
        raybet_match_id,
        tournament,
        one_id,
        one_name,
        two_id,
        two_name,
        scheduled_at,
        best_of,
        raw_stage or None,
        metadata_updated_at,
        identity_json,
        identity_hash,
    )


def _read_canonical_identity(
    connection: sqlite3.Connection,
    team_one_id: int,
    team_two_id: int,
) -> _CanonicalIdentity:
    if team_one_id == team_two_id:
        raise _FailClosed("canonical_team_ids_conflict")
    try:
        rows = connection.execute(
            "SELECT team_id, name FROM teams WHERE team_id IN (?, ?)",
            (team_one_id, team_two_id),
        ).fetchall()
    except sqlite3.OperationalError as error:
        raise _FailClosed("canonical_teams_schema_missing") from error
    by_id: dict[int, str] = {}
    for row in rows:
        team_id = _strict_positive_int(_value(row, 0, "team_id"))
        name = str(_value(row, 1, "name") or "").strip()
        if team_id is None or not name:
            raise _FailClosed("canonical_team_metadata_invalid")
        by_id[team_id] = name
    if team_one_id not in by_id or team_two_id not in by_id:
        raise _FailClosed("canonical_team_missing")
    identity = {
        "team_one": {"name": by_id[team_one_id], "team_id": team_one_id},
        "team_two": {"name": by_id[team_two_id], "team_id": team_two_id},
    }
    identity_json, identity_hash = _canonical_json(identity)
    return _CanonicalIdentity(
        team_one_id,
        by_id[team_one_id],
        team_two_id,
        by_id[team_two_id],
        identity_json,
        identity_hash,
    )


def _read_event_policy(
    connection: sqlite3.Connection, event_id: str
) -> _EventPolicy:
    row = connection.execute(
        """SELECT canonical_name, scope, approval_status, evidence_status, tier,
                  prize_pool_usd, approved_by, approved_at,
                  main_event_start_at, main_event_end_at,
                  official_evidence_urls_json, included_stages_json,
                  excluded_categories_json, include_internal_lcq,
                  excludes_qualifiers, excludes_division_2,
                  excludes_exhibitions, excludes_forfeits,
                  excludes_void_remakes
           FROM event_registry WHERE event_id=?""",
        (event_id,),
    ).fetchone()
    if row is None:
        raise _FailClosed("formal_event_missing")
    try:
        approved_at = _parse_timestamp(str(_value(row, 7, "approved_at")))
        start_at = _parse_timestamp(str(_value(row, 8, "main_event_start_at")))
        end_at = _parse_timestamp(str(_value(row, 9, "main_event_end_at")))
        official = _json_string_tuple(_value(row, 10, "official_evidence_urls_json"))
        included = _json_string_tuple(_value(row, 11, "included_stages_json"))
        excluded = _json_string_tuple(_value(row, 12, "excluded_categories_json"))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise _FailClosed("event_scope_evidence_invalid") from error
    prize = _value(row, 5, "prize_pool_usd")
    if isinstance(prize, bool) or not isinstance(prize, (int, float)):
        raise _FailClosed("event_prize_invalid")
    return _EventPolicy(
        event_id=event_id,
        canonical_name=str(_value(row, 0, "canonical_name") or "").strip(),
        scope=str(_value(row, 1, "scope") or ""),
        approval_status=str(_value(row, 2, "approval_status") or ""),
        evidence_status=str(_value(row, 3, "evidence_status") or ""),
        tier=str(_value(row, 4, "tier") or ""),
        prize_pool_usd=prize,
        approved_by=(str(_value(row, 6, "approved_by")).strip()
                     if _value(row, 6, "approved_by") else None),
        approved_at=approved_at,
        main_event_start_at=start_at,
        main_event_end_at=end_at,
        official_evidence_urls=official,
        included_stages=included,
        excluded_categories=excluded,
        include_internal_lcq=bool(_value(row, 13, "include_internal_lcq")),
        exclusion_flags=tuple(
            _value(row, index, name) == 1
            for index, name in (
                (14, "excludes_qualifiers"),
                (15, "excludes_division_2"),
                (16, "excludes_exhibitions"),
                (17, "excludes_forfeits"),
                (18, "excludes_void_remakes"),
            )
        ),
    )


def _formal_event_reason(event: _EventPolicy, as_of: datetime) -> str | None:
    if event.scope != "formal_main_event":
        return "event_scope_not_formal_main_event"
    if event.approval_status != "approved":
        return "event_not_approved"
    if event.evidence_status != "manually_audited":
        return "event_evidence_not_manually_audited"
    if event.tier != "tier_1":
        return "event_tier_not_tier_1"
    if event.prize_pool_usd < MINIMUM_PRIZE_POOL_USD:
        return "event_prize_below_minimum"
    if not event.approved_by:
        return "event_approval_evidence_missing"
    if event.approved_at > as_of:
        return "event_approval_not_yet_available"
    if not event.canonical_name or not event.official_evidence_urls:
        return "event_official_evidence_missing"
    if event.main_event_start_at > event.main_event_end_at:
        return "event_time_window_invalid"
    if not event.included_stages or not all(event.exclusion_flags):
        return "event_exclusion_policy_incomplete"
    return None


def _validate_mapping_evidence(
    evidence: Mapping[str, Any], identity: _RayBetIdentity, event: _EventPolicy
) -> _MappingEvidence:
    if evidence.get("kind") != "manual_cross_source_review":
        raise _FailClosed("mapping_evidence_kind_invalid")
    for field in ("raybet_url", "official_event_url"):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise _FailClosed("mapping_source_evidence_missing")

    tournament = evidence.get("tournament")
    if not isinstance(tournament, Mapping):
        raise _FailClosed("tournament_evidence_missing")
    if tournament.get("raybet_name") != identity.tournament:
        raise _FailClosed("raybet_tournament_evidence_mismatch")
    if tournament.get("event_name") != event.canonical_name:
        raise _FailClosed("event_tournament_evidence_mismatch")

    schedule = evidence.get("schedule")
    if not isinstance(schedule, Mapping):
        raise _FailClosed("schedule_evidence_missing")
    if schedule.get("raybet_scheduled_at") != identity.scheduled_at_raw:
        raise _FailClosed("raybet_schedule_evidence_mismatch")
    offset = schedule.get("utc_offset_minutes")
    if type(offset) is not int or not -840 <= offset <= 840:
        raise _FailClosed("raybet_schedule_timezone_evidence_missing")
    if not isinstance(schedule.get("timezone_evidence"), str) or not schedule[
        "timezone_evidence"
    ].strip():
        raise _FailClosed("raybet_schedule_timezone_evidence_missing")
    try:
        calculated_utc = _schedule_to_utc(identity.scheduled_at_raw, offset)
        claimed_utc = _parse_timestamp(str(schedule.get("scheduled_at_utc") or ""))
    except ValueError as error:
        raise _FailClosed("raybet_schedule_evidence_invalid") from error
    if calculated_utc != claimed_utc:
        raise _FailClosed("raybet_schedule_utc_mismatch")
    if not event.main_event_start_at <= claimed_utc <= event.main_event_end_at:
        raise _FailClosed("raybet_schedule_outside_event_window")

    stage = evidence.get("stage")
    if not isinstance(stage, Mapping):
        raise _FailClosed("stage_evidence_missing")
    stage_scope = str(stage.get("scope") or "").strip()
    if not isinstance(stage.get("source_url"), str) or not stage[
        "source_url"
    ].strip():
        raise _FailClosed("stage_evidence_missing")
    if identity.raw_stage is not None and identity.raw_stage != stage_scope:
        raise _FailClosed("raybet_stage_evidence_mismatch")
    if stage_scope in event.excluded_categories:
        raise _FailClosed("event_stage_excluded")
    if stage_scope not in event.included_stages:
        raise _FailClosed("event_stage_not_included")
    if stage_scope == "internal_lcq" and not event.include_internal_lcq:
        raise _FailClosed("event_stage_not_included")
    if stage_scope not in {"main_event", "internal_lcq"}:
        raise _FailClosed("event_stage_not_formal")
    return _MappingEvidence(stage_scope, claimed_utc)


def _validate_crosswalk_evidence(
    evidence: Mapping[str, Any],
    raybet: _RayBetIdentity,
    canonical: _CanonicalIdentity,
) -> tuple[str, str]:
    crosswalk = evidence.get("team_crosswalk")
    if not isinstance(crosswalk, Mapping):
        raise _FailClosed("team_crosswalk_evidence_missing")
    expected = {
        "team_one": {
            "raybet_team_id": raybet.team_one_id,
            "raybet_team_name": raybet.team_one_name,
            "canonical_team_id": canonical.team_one_id,
            "canonical_team_name": canonical.team_one_name,
        },
        "team_two": {
            "raybet_team_id": raybet.team_two_id,
            "raybet_team_name": raybet.team_two_name,
            "canonical_team_id": canonical.team_two_id,
            "canonical_team_name": canonical.team_two_name,
        },
    }
    for side, required in expected.items():
        row = crosswalk.get(side)
        if not isinstance(row, Mapping):
            raise _FailClosed("team_crosswalk_evidence_missing")
        if any(row.get(field) != value for field, value in required.items()):
            raise _FailClosed("team_crosswalk_evidence_mismatch")
        if not isinstance(row.get("source_url"), str) or not row[
            "source_url"
        ].strip():
            raise _FailClosed("team_crosswalk_source_missing")
    return _canonical_json(dict(crosswalk))


def _automatic_approval_id(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
    as_of: datetime,
) -> int:
    row = connection.execute(
        """SELECT approval.approval_id, approval.approved_at,
                  approval.recorded_at, source.accepted_at,
                  source.recorded_at, source.raybet_metadata_updated_at
             FROM strict_live_automatic_evidence_approvals AS approval
             JOIN strict_live_map_mappings AS source
               ON source.mapping_id=approval.source_mapping_id
             LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
               ON invalidation.mapping_id=source.mapping_id
            WHERE approval.raybet_match_id=?
              AND approval.event_id=?
              AND approval.team_one_id=? AND approval.team_two_id=?
              AND approval.canonical_team_one_id=?
              AND approval.canonical_team_two_id=?
              AND approval.raybet_identity_hash=?
              AND approval.canonical_identity_hash=?
              AND approval.crosswalk_evidence_hash=?
              AND approval.evidence_hash=?
              AND source.acceptance_mode='manual_exact'
              AND invalidation.invalidation_id IS NULL
              AND approval.approved_at<=? AND approval.recorded_at<=?
            ORDER BY approval.approval_id DESC LIMIT 1""",
        (
            values["raybet_match_id"],
            values["event_id"],
            values["team_one_id"],
            values["team_two_id"],
            values["canonical_team_one_id"],
            values["canonical_team_two_id"],
            values["raybet_identity_hash"],
            values["canonical_identity_hash"],
            values["crosswalk_evidence_hash"],
            values["evidence_hash"],
            as_of.isoformat(),
            as_of.isoformat(),
        ),
    ).fetchone()
    if row is None:
        raise _FailClosed("automatic_exact_evidence_not_preapproved")
    causal_reason = _automatic_approval_causal_reason(
        approval_approved_at=row[1],
        approval_recorded_at=row[2],
        source_accepted_at=row[3],
        source_recorded_at=row[4],
        source_metadata_updated_at=row[5],
        mapping_accepted_at=values["accepted_at"],
        mapping_recorded_at=values["recorded_at"],
        transport_at=as_of,
    )
    if causal_reason is not None:
        raise _FailClosed(causal_reason)
    return int(row[0])


def _record_mapping_impacts(
    connection: sqlite3.Connection,
    *,
    mapping: StrictLiveMapMapping,
    invalidation_id: int,
    reason: str,
    recorded_at: datetime,
) -> None:
    dependents: set[tuple[str, str]] = set()
    if _table_has_columns(
        connection,
        "strategy_decisions",
        {"decision_key", "contributions_json", "raybet_match_id", "map_number"},
    ):
        rows = connection.execute(
            """SELECT decision_key, contributions_json FROM strategy_decisions
                WHERE raybet_match_id=? AND map_number=?""",
            (mapping.raybet_match_id, mapping.map_number),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row[1]))
                mapping_id = payload["__inputs__"]["strict_live_eligibility"][
                    "mapping_refs"
                ]["strict_mapping_id"]
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if mapping_id == mapping.mapping_id:
                dependents.add(("strategy_decision", str(row[0])))
    if _table_has_columns(
        connection,
        "research_live_predictions",
        {"prediction_key", "strict_mapping_id"},
    ):
        dependents.update(
            ("research_prediction", str(row[0]))
            for row in connection.execute(
                """SELECT prediction_key FROM research_live_predictions
                    WHERE strict_mapping_id=?""",
                (mapping.mapping_id,),
            ).fetchall()
        )
    if _table_has_columns(
        connection,
        "shadow_orders",
        {"order_key", "strict_mapping_id"},
    ):
        dependents.update(
            ("shadow_order", str(row[0]))
            for row in connection.execute(
                """SELECT order_key FROM shadow_orders
                    WHERE strict_mapping_id=?""",
                (mapping.mapping_id,),
            ).fetchall()
        )
    connection.executemany(
        """INSERT OR IGNORE INTO strict_live_mapping_impacts
           (mapping_id, invalidation_id, dependent_type, dependent_key, reason,
            recorded_at) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            (
                mapping.mapping_id,
                invalidation_id,
                dependent_type,
                dependent_key,
                reason,
                recorded_at.isoformat(),
            )
            for dependent_type, dependent_key in sorted(dependents)
        ),
    )


def _quarantine_mapping_order_dependents(
    connection: sqlite3.Connection,
    *,
    invalidation_id: int,
    recorded_at: datetime,
) -> None:
    order_keys = tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT dependent_key FROM strict_live_mapping_impacts
                WHERE invalidation_id=? AND dependent_type='shadow_order'
                ORDER BY dependent_key""",
            (invalidation_id,),
        ).fetchall()
    )
    if not order_keys:
        return

    recorded_at_iso = recorded_at.isoformat()
    block_reason = "strict_mapping_invalidated"
    if _table_has_columns(connection, "settlements", {"order_key", "review_required"}):
        connection.executemany(
            """UPDATE settlements SET review_required=1
                WHERE order_key=? AND review_required=0""",
            ((order_key,) for order_key in order_keys),
        )

    if _table_has_columns(
        connection,
        "shadow_map_attempts",
        {"order_key", "raybet_match_id", "map_number"},
    ) and _table_has_columns(
        connection,
        "settlement_reconciliations",
        {"raybet_match_id", "map_number", "status", "reason", "updated_at"},
    ):
        for order_key in order_keys:
            connection.execute(
                """UPDATE settlement_reconciliations
                      SET status='manual_review', reason=?, updated_at=?
                    WHERE status!='manual_review'
                      AND (raybet_match_id, map_number) IN (
                          SELECT raybet_match_id, map_number
                            FROM shadow_map_attempts WHERE order_key=?
                      )""",
                (block_reason, recorded_at_iso, order_key),
            )

    outbox_columns = {
        "outbox_id",
        "order_key",
        "event_type",
        "status",
        "lease_token",
        "lease_until",
        "last_error",
        "updated_at",
    }
    if not _table_has_columns(connection, "notification_outbox", outbox_columns):
        return
    audit_available = _table_has_columns(
        connection,
        "notification_outbox_audit",
        {"outbox_id", "action", "actor", "reason", "created_at"},
    )
    from .notifications import quarantine_outbox

    for order_key in order_keys:
        outbox_rows = connection.execute(
            """SELECT outbox_id FROM notification_outbox
                WHERE order_key=? AND status IN ('pending', 'leased')
                ORDER BY outbox_id""",
            (order_key,),
        ).fetchall()
        for row in outbox_rows:
            outbox_id = int(row[0])
            quarantine_outbox(
                connection,
                outbox_id=outbox_id,
                reason=block_reason,
                actor="strict_mapping_invalidation",
                now=recorded_at,
                record_audit=audit_available,
            )


def _automatic_mappings_for_source(
    connection: sqlite3.Connection, source_mapping_id: int
) -> tuple[StrictLiveMapMapping, ...]:
    rows = connection.execute(
        f"""SELECT {_MAPPING_COLUMNS}
              FROM strict_live_map_mappings AS m
              JOIN strict_live_automatic_evidence_approvals AS approval
                ON approval.approval_id=m.automatic_approval_id
              LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
                ON invalidation.mapping_id=m.mapping_id
             WHERE approval.source_mapping_id=?
               AND m.acceptance_mode='automatic_exact'
               AND invalidation.invalidation_id IS NULL
             ORDER BY m.mapping_id""",
        (source_mapping_id,),
    ).fetchall()
    return tuple(_mapping_from_row(row) for row in rows)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_has_column(
    connection: sqlite3.Connection, table: str, column: str
) -> bool:
    return any(
        str(row[1]) == column
        for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _table_has_columns(
    connection: sqlite3.Connection, table: str, columns: set[str]
) -> bool:
    if not _table_exists(connection, table):
        return False
    existing = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    return columns <= existing


def _mapping_causal_reason(
    mapping: StrictLiveMapMapping, transport_at: datetime
) -> str | None:
    if mapping.recorded_at > transport_at:
        return "mapping_not_yet_recorded"
    if mapping.raybet_metadata_updated_at > transport_at:
        return "raybet_metadata_not_yet_available"
    if mapping.accepted_at > transport_at:
        return "mapping_not_yet_accepted"
    return None


def _automatic_mapping_approval_reason(
    connection: sqlite3.Connection,
    mapping: StrictLiveMapMapping,
    transport_at: datetime,
) -> str | None:
    if mapping.acceptance_mode == "manual_exact":
        return None if mapping.automatic_approval_id is None else "manual_mapping_has_automatic_approval"
    if mapping.acceptance_mode != "automatic_exact":
        return "mapping_acceptance_mode_invalid"
    if mapping.automatic_approval_id is None:
        return "automatic_exact_approval_missing"
    row = connection.execute(
        """SELECT approval.raybet_match_id, approval.event_id,
                  approval.team_one_id, approval.team_two_id,
                  approval.canonical_team_one_id, approval.canonical_team_two_id,
                  approval.raybet_identity_hash, approval.canonical_identity_hash,
                  approval.crosswalk_evidence_hash, approval.evidence_hash,
                  approval.approved_at, approval.recorded_at,
                  invalidation.invalidation_id, source.accepted_at,
                  source.recorded_at, source.raybet_metadata_updated_at
             FROM strict_live_automatic_evidence_approvals AS approval
             JOIN strict_live_map_mappings AS source
               ON source.mapping_id=approval.source_mapping_id
             LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
               ON invalidation.mapping_id=source.mapping_id
            WHERE approval.approval_id=? AND source.acceptance_mode='manual_exact'""",
        (mapping.automatic_approval_id,),
    ).fetchone()
    if row is None:
        return "automatic_exact_approval_missing"
    expected = (
        mapping.raybet_match_id,
        mapping.event_id,
        mapping.team_one_id,
        mapping.team_two_id,
        mapping.canonical_team_one_id,
        mapping.canonical_team_two_id,
        mapping.raybet_identity_hash,
        mapping.canonical_identity_hash,
        mapping.crosswalk_evidence_hash,
        mapping.evidence_hash,
    )
    if tuple(row[:10]) != expected:
        return "automatic_exact_approval_mismatch"
    if row[12] is not None:
        return "automatic_exact_approval_invalidated"
    return _automatic_approval_causal_reason(
        approval_approved_at=row[10],
        approval_recorded_at=row[11],
        source_accepted_at=row[13],
        source_recorded_at=row[14],
        source_metadata_updated_at=row[15],
        mapping_accepted_at=mapping.accepted_at,
        mapping_recorded_at=mapping.recorded_at,
        transport_at=transport_at,
    )


def _historical_automatic_mapping_approval_reason(
    connection: sqlite3.Connection,
    mapping: StrictLiveMapMapping,
    transport_at: datetime,
) -> str | None:
    if mapping.acceptance_mode == "manual_exact":
        return None if mapping.automatic_approval_id is None else "manual_mapping_has_automatic_approval"
    if mapping.acceptance_mode != "automatic_exact":
        return "mapping_acceptance_mode_invalid"
    if mapping.automatic_approval_id is None:
        return "automatic_exact_approval_missing"
    try:
        row = connection.execute(
            """SELECT approval.raybet_match_id, approval.event_id,
                      approval.team_one_id, approval.team_two_id,
                      approval.canonical_team_one_id,
                      approval.canonical_team_two_id,
                      approval.raybet_identity_hash,
                      approval.canonical_identity_hash,
                      approval.crosswalk_evidence_hash, approval.evidence_hash,
                      approval.approved_at, approval.recorded_at,
                      invalidation.invalidated_at, source.accepted_at,
                      source.recorded_at, source.raybet_metadata_updated_at
                 FROM strict_live_automatic_evidence_approvals AS approval
                 JOIN strict_live_map_mappings AS source
                   ON source.mapping_id=approval.source_mapping_id
                 LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
                   ON invalidation.mapping_id=source.mapping_id
                WHERE approval.approval_id=?
                  AND source.acceptance_mode='manual_exact'""",
            (mapping.automatic_approval_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return "strict_mapping_schema_missing"
    if row is None:
        return "automatic_exact_approval_missing"
    expected = (
        mapping.raybet_match_id,
        mapping.event_id,
        mapping.team_one_id,
        mapping.team_two_id,
        mapping.canonical_team_one_id,
        mapping.canonical_team_two_id,
        mapping.raybet_identity_hash,
        mapping.canonical_identity_hash,
        mapping.crosswalk_evidence_hash,
        mapping.evidence_hash,
    )
    if tuple(row[:10]) != expected:
        return "automatic_exact_approval_mismatch"
    if row[12] is not None:
        try:
            invalidated_at = _parse_timestamp(str(row[12]))
        except (TypeError, ValueError):
            return "automatic_exact_approval_causal_order_invalid"
        if invalidated_at <= transport_at:
            return "automatic_exact_approval_invalidated"
    return _automatic_approval_causal_reason(
        approval_approved_at=row[10],
        approval_recorded_at=row[11],
        source_accepted_at=row[13],
        source_recorded_at=row[14],
        source_metadata_updated_at=row[15],
        mapping_accepted_at=mapping.accepted_at,
        mapping_recorded_at=mapping.recorded_at,
        transport_at=transport_at,
    )


def _automatic_approval_causal_reason(
    *,
    approval_approved_at: object,
    approval_recorded_at: object,
    source_accepted_at: object,
    source_recorded_at: object,
    source_metadata_updated_at: object,
    mapping_accepted_at: object,
    mapping_recorded_at: object,
    transport_at: datetime,
) -> str | None:
    source_reason = _approval_source_causal_reason(
        approval_approved_at=approval_approved_at,
        approval_recorded_at=approval_recorded_at,
        source_accepted_at=source_accepted_at,
        source_recorded_at=source_recorded_at,
        source_metadata_updated_at=source_metadata_updated_at,
    )
    if source_reason is not None:
        return source_reason
    try:
        approval_recorded = _parse_timestamp(str(approval_recorded_at))
        mapping_accepted = (
            mapping_accepted_at
            if isinstance(mapping_accepted_at, datetime)
            else _parse_timestamp(str(mapping_accepted_at))
        )
        mapping_recorded = (
            mapping_recorded_at
            if isinstance(mapping_recorded_at, datetime)
            else _parse_timestamp(str(mapping_recorded_at))
        )
    except (TypeError, ValueError):
        return "automatic_exact_approval_causal_order_invalid"
    if any(
        value.tzinfo is None or value.utcoffset() is None
        for value in (mapping_accepted, mapping_recorded, transport_at)
    ):
        return "automatic_exact_approval_causal_order_invalid"
    mapping_accepted = mapping_accepted.astimezone(timezone.utc)
    mapping_recorded = mapping_recorded.astimezone(timezone.utc)
    transport_at = transport_at.astimezone(timezone.utc)
    if not (
        approval_recorded <= mapping_accepted <= mapping_recorded
        and approval_recorded <= transport_at
    ):
        return "automatic_exact_approval_causal_order_invalid"
    return None


def _approval_source_causal_reason(
    *,
    approval_approved_at: object,
    approval_recorded_at: object,
    source_accepted_at: object,
    source_recorded_at: object,
    source_metadata_updated_at: object,
) -> str | None:
    try:
        approval_approved = _parse_timestamp(str(approval_approved_at))
        approval_recorded = _parse_timestamp(str(approval_recorded_at))
        source_accepted = _parse_timestamp(str(source_accepted_at))
        source_recorded = _parse_timestamp(str(source_recorded_at))
        source_metadata = _parse_timestamp(str(source_metadata_updated_at))
    except (TypeError, ValueError):
        return "automatic_exact_approval_causal_order_invalid"
    if not (
        approval_approved <= approval_recorded
        and source_accepted <= source_recorded <= approval_recorded
        and source_metadata <= source_recorded
    ):
        return "automatic_exact_approval_causal_order_invalid"
    return None


def _mapping_identity_reason(
    mapping: StrictLiveMapMapping, current: _RayBetIdentity
) -> str | None:
    identity_hash = hashlib.sha256(mapping.raybet_identity_json.encode("utf-8")).hexdigest()
    if identity_hash != mapping.raybet_identity_hash:
        return "mapping_identity_hash_invalid"
    evidence_hash = hashlib.sha256(mapping.evidence_json.encode("utf-8")).hexdigest()
    if evidence_hash != mapping.evidence_hash:
        return "mapping_evidence_hash_invalid"
    if current.identity_hash != mapping.raybet_identity_hash:
        return "raybet_metadata_drift"
    if (
        current.team_one_id != mapping.team_one_id
        or current.team_two_id != mapping.team_two_id
    ):
        return "raybet_metadata_drift"
    if current.best_of != mapping.raybet_best_of:
        return "raybet_metadata_drift"
    if mapping.map_number > mapping.raybet_best_of:
        return "map_number_exceeds_best_of"
    return None


def _canonical_identity_reason(
    mapping: StrictLiveMapMapping, current: _CanonicalIdentity
) -> str | None:
    identity_hash = hashlib.sha256(
        mapping.canonical_identity_json.encode("utf-8")
    ).hexdigest()
    if identity_hash != mapping.canonical_identity_hash:
        return "canonical_identity_hash_invalid"
    crosswalk_hash = hashlib.sha256(
        mapping.crosswalk_evidence_json.encode("utf-8")
    ).hexdigest()
    if crosswalk_hash != mapping.crosswalk_evidence_hash:
        return "crosswalk_evidence_hash_invalid"
    if current.identity_hash != mapping.canonical_identity_hash:
        return "canonical_team_metadata_drift"
    if (
        current.team_one_id != mapping.canonical_team_one_id
        or current.team_two_id != mapping.canonical_team_two_id
        or current.team_one_name != mapping.canonical_team_one_name
        or current.team_two_name != mapping.canonical_team_two_name
    ):
        return "canonical_team_metadata_drift"
    return None


def _validate_exact_teams(
    values: Mapping[str, Any], identity: _RayBetIdentity
) -> None:
    proposed = (values["team_one_id"], values["team_two_id"])
    exact = (identity.team_one_id, identity.team_two_id)
    if proposed == exact:
        return
    if proposed == tuple(reversed(exact)):
        raise _FailClosed("raybet_exact_team_order_mismatch")
    raise _FailClosed("raybet_exact_team_ids_mismatch")


_MAPPING_COLUMNS = """m.mapping_id, m.raybet_match_id, m.map_number, m.event_id,
    m.team_one_id, m.team_two_id, m.canonical_team_one_id, m.canonical_team_one_name,
    m.canonical_team_two_id, m.canonical_team_two_name, m.canonical_identity_json,
    m.canonical_identity_hash, m.crosswalk_evidence_json, m.crosswalk_evidence_hash,
    m.stage_scope, m.scheduled_at_utc, m.raybet_best_of, m.raybet_identity_json,
    m.raybet_identity_hash, m.raybet_metadata_updated_at, m.source, m.evidence_json,
    m.evidence_hash, m.mapping_version, m.accepted_by, m.accepted_at, m.recorded_at,
    m.acceptance_mode, m.automatic_approval_id"""

_MAPPING_SELECT = f"""SELECT {_MAPPING_COLUMNS}
    FROM strict_live_map_mappings AS m
    LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
      ON invalidation.mapping_id=m.mapping_id
    WHERE m.raybet_match_id=? AND m.map_number=?
      AND invalidation.invalidation_id IS NULL
    ORDER BY m.mapping_id LIMIT 2"""


def _mapping_rows(
    connection: sqlite3.Connection, raybet_match_id: str, map_number: int
) -> Sequence[sqlite3.Row | tuple[Any, ...]]:
    return connection.execute(
        _MAPPING_SELECT, (raybet_match_id, map_number)
    ).fetchall()


def _mapping_row_by_id(
    connection: sqlite3.Connection, mapping_id: int
) -> sqlite3.Row | tuple[Any, ...] | None:
    return connection.execute(
        f"""SELECT {_MAPPING_COLUMNS}
              FROM strict_live_map_mappings AS m WHERE m.mapping_id=?""",
        (mapping_id,),
    ).fetchone()


def _latest_invalidated_mapping_row(
    connection: sqlite3.Connection, raybet_match_id: str, map_number: int
) -> sqlite3.Row | tuple[Any, ...] | None:
    return connection.execute(
        f"""SELECT {_MAPPING_COLUMNS}
              FROM strict_live_map_mappings AS m
              JOIN strict_live_map_mapping_invalidations AS invalidation
                ON invalidation.mapping_id=m.mapping_id
             WHERE m.raybet_match_id=? AND m.map_number=?
             ORDER BY invalidation.invalidation_id DESC LIMIT 1""",
        (raybet_match_id, map_number),
    ).fetchone()


def _latest_invalidated_mapping_id(
    connection: sqlite3.Connection, raybet_match_id: str, map_number: int
) -> int | None:
    row = _latest_invalidated_mapping_row(connection, raybet_match_id, map_number)
    return int(_value(row, 0, "mapping_id")) if row is not None else None


def _mapping_from_row(row: sqlite3.Row | tuple[Any, ...]) -> StrictLiveMapMapping:
    required = (
        (6, "canonical_team_one_id"),
        (7, "canonical_team_one_name"),
        (8, "canonical_team_two_id"),
        (9, "canonical_team_two_name"),
        (10, "canonical_identity_json"),
        (11, "canonical_identity_hash"),
        (12, "crosswalk_evidence_json"),
        (13, "crosswalk_evidence_hash"),
        (14, "stage_scope"),
        (15, "scheduled_at_utc"),
        (16, "raybet_best_of"),
        (17, "raybet_identity_json"),
        (18, "raybet_identity_hash"),
        (19, "raybet_metadata_updated_at"),
        (26, "recorded_at"),
    )
    if any(_value(row, index, name) is None for index, name in required):
        raise _FailClosed("legacy_mapping_missing_strict_identity")
    try:
        mapping = StrictLiveMapMapping(
            mapping_id=int(_value(row, 0, "mapping_id")),
            raybet_match_id=str(_value(row, 1, "raybet_match_id")),
            map_number=int(_value(row, 2, "map_number")),
            event_id=str(_value(row, 3, "event_id")),
            team_one_id=int(_value(row, 4, "team_one_id")),
            team_two_id=int(_value(row, 5, "team_two_id")),
            canonical_team_one_id=int(_value(row, 6, "canonical_team_one_id")),
            canonical_team_one_name=str(_value(row, 7, "canonical_team_one_name")),
            canonical_team_two_id=int(_value(row, 8, "canonical_team_two_id")),
            canonical_team_two_name=str(_value(row, 9, "canonical_team_two_name")),
            canonical_identity_json=str(_value(row, 10, "canonical_identity_json")),
            canonical_identity_hash=str(_value(row, 11, "canonical_identity_hash")),
            crosswalk_evidence_json=str(_value(row, 12, "crosswalk_evidence_json")),
            crosswalk_evidence_hash=str(_value(row, 13, "crosswalk_evidence_hash")),
            stage_scope=str(_value(row, 14, "stage_scope")),
            scheduled_at_utc=_parse_timestamp(
                str(_value(row, 15, "scheduled_at_utc"))
            ),
            raybet_best_of=int(_value(row, 16, "raybet_best_of")),
            raybet_identity_json=str(_value(row, 17, "raybet_identity_json")),
            raybet_identity_hash=str(_value(row, 18, "raybet_identity_hash")),
            raybet_metadata_updated_at=_parse_timestamp(
                str(_value(row, 19, "raybet_metadata_updated_at"))
            ),
            source=str(_value(row, 20, "source")),
            evidence_json=str(_value(row, 21, "evidence_json")),
            evidence_hash=str(_value(row, 22, "evidence_hash")),
            mapping_version=str(_value(row, 23, "mapping_version")),
            accepted_by=str(_value(row, 24, "accepted_by")),
            accepted_at=_parse_timestamp(str(_value(row, 25, "accepted_at"))),
            recorded_at=_parse_timestamp(str(_value(row, 26, "recorded_at"))),
            acceptance_mode=str(_value(row, 27, "acceptance_mode")),
            automatic_approval_id=(
                int(_value(row, 28, "automatic_approval_id"))
                if _value(row, 28, "automatic_approval_id") is not None
                else None
            ),
        )
    except (TypeError, ValueError) as error:
        raise _FailClosed("mapping_identity_snapshot_invalid") from error
    if mapping.acceptance_mode not in {"manual_exact", "automatic_exact"}:
        raise _FailClosed("mapping_acceptance_mode_invalid")
    if mapping.acceptance_mode == "automatic_exact" and mapping.automatic_approval_id is None:
        raise _FailClosed("automatic_exact_approval_missing")
    return mapping


def _mapping_from_values(
    mapping_id: int, values: Mapping[str, Any]
) -> StrictLiveMapMapping:
    return StrictLiveMapMapping(
        mapping_id,
        values["raybet_match_id"],
        values["map_number"],
        values["event_id"],
        values["team_one_id"],
        values["team_two_id"],
        values["canonical_team_one_id"],
        values["canonical_team_one_name"],
        values["canonical_team_two_id"],
        values["canonical_team_two_name"],
        values["canonical_identity_json"],
        values["canonical_identity_hash"],
        values["crosswalk_evidence_json"],
        values["crosswalk_evidence_hash"],
        values["stage_scope"],
        values["scheduled_at_utc"],
        values["raybet_best_of"],
        values["raybet_identity_json"],
        values["raybet_identity_hash"],
        values["raybet_metadata_updated_at"],
        values["source"],
        values["evidence_json"],
        values["evidence_hash"],
        values["mapping_version"],
        values["accepted_by"],
        values["accepted_at"],
        values["recorded_at"],
        values["acceptance_mode"],
        values.get("automatic_approval_id"),
    )


def _insert_mapping(connection: sqlite3.Connection, values: Mapping[str, Any]) -> int:
    cursor = connection.execute(
        """INSERT INTO strict_live_map_mappings
           (raybet_match_id, map_number, event_id, team_one_id, team_two_id,
            canonical_team_one_id, canonical_team_one_name,
            canonical_team_two_id, canonical_team_two_name,
            canonical_identity_json, canonical_identity_hash,
            crosswalk_evidence_json, crosswalk_evidence_hash,
            stage_scope, scheduled_at_utc, raybet_best_of,
            raybet_identity_json, raybet_identity_hash,
            raybet_metadata_updated_at, source, evidence_json, evidence_hash,
             mapping_version, acceptance_mode, automatic_approval_id,
             accepted_by, accepted_at, recorded_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            values["raybet_match_id"],
            values["map_number"],
            values["event_id"],
            values["team_one_id"],
            values["team_two_id"],
            values["canonical_team_one_id"],
            values["canonical_team_one_name"],
            values["canonical_team_two_id"],
            values["canonical_team_two_name"],
            values["canonical_identity_json"],
            values["canonical_identity_hash"],
            values["crosswalk_evidence_json"],
            values["crosswalk_evidence_hash"],
            values["stage_scope"],
            values["scheduled_at_utc_iso"],
            values["raybet_best_of"],
            values["raybet_identity_json"],
            values["raybet_identity_hash"],
            values["raybet_metadata_updated_at_iso"],
            values["source"],
            values["evidence_json"],
            values["evidence_hash"],
            values["mapping_version"],
            values["acceptance_mode"],
            values.get("automatic_approval_id"),
            values["accepted_by"],
            values["accepted_at_iso"],
            values["recorded_at_iso"],
            values["recorded_at_iso"],
        ),
    )
    return int(cursor.lastrowid)


def _insert_audit(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
    decision: str,
    reason: str,
    mapping_id: int | None,
    *,
    match_method: str | None = None,
) -> int:
    match_method = match_method or str(values.get("acceptance_mode", "manual_exact"))
    cursor = connection.execute(
        """INSERT INTO strict_live_map_mapping_audit
           (raybet_match_id, map_number, proposed_event_id,
            proposed_team_one_id, proposed_team_two_id,
            proposed_canonical_team_one_id, proposed_canonical_team_two_id,
            match_method,
            decision, reason, source, evidence_json, evidence_hash,
            mapping_version, actor, observed_at, recorded_at,
            raybet_identity_hash, raybet_metadata_updated_at,
            canonical_identity_hash, crosswalk_evidence_hash, mapping_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            values["raybet_match_id"],
            values["map_number"],
            values.get("event_id"),
            values.get("team_one_id"),
            values.get("team_two_id"),
            values.get("canonical_team_one_id"),
            values.get("canonical_team_two_id"),
            match_method,
            decision,
            reason,
            values["source"],
            values["evidence_json"],
            values["evidence_hash"],
            values["mapping_version"],
            values.get("accepted_by"),
            values["accepted_at_iso"],
            values["recorded_at_iso"],
            values.get("raybet_identity_hash"),
            values.get("raybet_metadata_updated_at_iso"),
            values.get("canonical_identity_hash"),
            values.get("crosswalk_evidence_hash"),
            mapping_id,
        ),
    )
    return int(cursor.lastrowid)


def _validated_mapping_values(**raw: Any) -> dict[str, Any]:
    accepted_at = _aware_utc(raw["accepted_at"], "accepted_at")
    recorded_at = _aware_utc(raw["recorded_at"], "recorded_at")
    team_one_id = _positive_integer(raw["team_one_id"], "team_one_id")
    team_two_id = _positive_integer(raw["team_two_id"], "team_two_id")
    canonical_team_one_id = _positive_integer(
        raw["canonical_team_one_id"], "canonical_team_one_id"
    )
    canonical_team_two_id = _positive_integer(
        raw["canonical_team_two_id"], "canonical_team_two_id"
    )
    if team_one_id == team_two_id:
        raise StrictMappingError("team_one_id and team_two_id must be different")
    if canonical_team_one_id == canonical_team_two_id:
        raise StrictMappingError(
            "canonical_team_one_id and canonical_team_two_id must be different"
        )
    evidence_json, evidence_hash = _canonical_evidence(raw["evidence"])
    acceptance_mode = str(raw.get("acceptance_mode", "manual_exact"))
    if acceptance_mode not in {"manual_exact", "automatic_exact"}:
        raise StrictMappingError("invalid_acceptance_mode")
    return {
        "raybet_match_id": _required_text(raw["raybet_match_id"], "raybet_match_id"),
        "map_number": _positive_integer(raw["map_number"], "map_number"),
        "event_id": _required_text(raw["event_id"], "event_id"),
        "team_one_id": team_one_id,
        "team_two_id": team_two_id,
        "canonical_team_one_id": canonical_team_one_id,
        "canonical_team_two_id": canonical_team_two_id,
        "source": _required_text(raw["source"], "source"),
        "evidence": raw["evidence"],
        "evidence_json": evidence_json,
        "evidence_hash": evidence_hash,
        "mapping_version": _required_text(raw["mapping_version"], "mapping_version"),
        "acceptance_mode": acceptance_mode,
        "automatic_approval_id": None,
        "accepted_by": _required_text(raw["accepted_by"], "accepted_by"),
        "accepted_at": accepted_at,
        "accepted_at_iso": accepted_at.isoformat(),
        "recorded_at": recorded_at,
        "recorded_at_iso": recorded_at.isoformat(),
    }


def _is_same_mapping_value(
    row: sqlite3.Row | tuple[Any, ...], values: Mapping[str, Any]
) -> bool:
    return (
        str(_value(row, 3, "event_id")) == values["event_id"]
        and int(_value(row, 4, "team_one_id")) == values["team_one_id"]
        and int(_value(row, 5, "team_two_id")) == values["team_two_id"]
        and int(_value(row, 6, "canonical_team_one_id"))
        == values["canonical_team_one_id"]
        and int(_value(row, 8, "canonical_team_two_id"))
        == values["canonical_team_two_id"]
        and str(_value(row, 20, "source")) == values["source"]
        and str(_value(row, 21, "evidence_json")) == values["evidence_json"]
        and str(_value(row, 22, "evidence_hash")) == values["evidence_hash"]
        and str(_value(row, 23, "mapping_version")) == values["mapping_version"]
        and str(_value(row, 24, "accepted_by")) == values["accepted_by"]
        and str(_value(row, 27, "acceptance_mode")) == values["acceptance_mode"]
        and _value(row, 28, "automatic_approval_id")
        == values.get("automatic_approval_id")
    )


def _canonical_evidence(evidence: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(evidence, Mapping) or not evidence:
        raise StrictMappingError("evidence must be a non-empty mapping")
    try:
        return _canonical_json(dict(evidence))
    except (TypeError, ValueError) as error:
        raise StrictMappingError("evidence must be JSON serializable") from error


def _canonical_json(value: Any) -> tuple[str, str]:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _schedule_to_utc(value: str, offset_minutes: int) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    supplied_timezone = timezone(timedelta(minutes=offset_minutes))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=supplied_timezone)
    elif parsed.utcoffset() != supplied_timezone.utcoffset(None):
        raise ValueError("schedule offset does not match evidence")
    return parsed.astimezone(timezone.utc)


def _json_string_tuple(value: Any) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) and item.strip() for item in decoded
    ):
        raise ValueError("expected non-empty strings")
    return tuple(item.strip() for item in decoded)


def _row_text(
    row: sqlite3.Row | tuple[Any, ...], index: int, name: str, reason: str
) -> str:
    value = _value(row, index, name)
    if not isinstance(value, str) or not value.strip():
        raise _FailClosed(reason)
    return value.strip()


def _strict_positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _required_text(value: Any, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise StrictMappingError(f"{field} must be non-empty")
    return text


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StrictMappingError(f"{field} must be a positive integer")
    return value


def _aware_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StrictMappingError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(timezone.utc)


def _value(row: sqlite3.Row | tuple[Any, ...], index: int, name: str) -> Any:
    return row[name] if isinstance(row, sqlite3.Row) else row[index]


def _ineligible(
    reason: str,
    raybet_match_id: str,
    map_number: int,
    transport_observed_at: datetime | None,
    mapping: StrictLiveMapMapping | None = None,
) -> StrictLiveEligibility:
    return StrictLiveEligibility(
        False,
        reason,
        raybet_match_id,
        map_number,
        transport_observed_at,
        mapping,
    )


def _add_missing_columns(
    connection: sqlite3.Connection, table: str, columns: Mapping[str, str]
) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        savepoint = "strict_live_eligibility_write"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE {savepoint}")
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


__all__ = [
    "MINIMUM_PRIZE_POOL_USD",
    "STRICT_MAPPING_VERSION",
    "StrictLiveEligibility",
    "StrictLiveMapMapping",
    "StrictMappingConflictError",
    "StrictMappingError",
    "accept_strict_live_map_mapping",
    "check_strict_live_eligibility",
    "init_strict_live_eligibility_schema",
    "query_strict_live_eligibility",
    "record_strict_live_mapping_candidate",
    "strict_live_mapping_schema_requires_rebuild",
]
