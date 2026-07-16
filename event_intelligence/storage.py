"""Additive SQLite schema for strict-event intelligence."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence


BUSY_TIMEOUT_MS = 5_000
CURRENT_SCHEMA_VERSION = 8
CUTOFF_LINEAGE_SCHEMA_VERSION = 8


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intelligence_schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_registry (
    event_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL CHECK (tier = 'tier_1'),
    prize_pool_usd INTEGER NOT NULL CHECK (prize_pool_usd >= 1000000),
    main_event_start_at TEXT NOT NULL,
    main_event_end_at TEXT NOT NULL,
    opendota_league_id INTEGER NOT NULL UNIQUE,
    secondary_provider_ids_json TEXT NOT NULL DEFAULT '{}',
    official_evidence_urls_json TEXT NOT NULL,
    evidence_status TEXT NOT NULL
        CHECK (evidence_status IN ('manually_audited', 'unverified')),
    scope_policy_version TEXT NOT NULL,
    scope TEXT NOT NULL
        CHECK (scope IN ('formal_main_event', 'audit_only', 'excluded')),
    approval_status TEXT NOT NULL
        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approved_by TEXT,
    approved_at TEXT,
    reconciliation_status TEXT NOT NULL
        CHECK (reconciliation_status IN
               ('not_required', 'reconciliation_pending', 'reconciled',
                'review_required')),
    expected_map_count INTEGER CHECK (expected_map_count IS NULL OR expected_map_count >= 0),
    observed_map_count INTEGER CHECK (observed_map_count IS NULL OR observed_map_count >= 0),
    public_map_count INTEGER CHECK (public_map_count IS NULL OR public_map_count >= 0),
    reconciliation_note TEXT,
    included_stages_json TEXT NOT NULL,
    excluded_categories_json TEXT NOT NULL,
    include_internal_lcq INTEGER NOT NULL DEFAULT 0
        CHECK (include_internal_lcq IN (0, 1)),
    excludes_qualifiers INTEGER NOT NULL DEFAULT 1
        CHECK (excludes_qualifiers IN (0, 1)),
    excludes_division_2 INTEGER NOT NULL DEFAULT 1
        CHECK (excludes_division_2 IN (0, 1)),
    excludes_exhibitions INTEGER NOT NULL DEFAULT 1
        CHECK (excludes_exhibitions IN (0, 1)),
    excludes_forfeits INTEGER NOT NULL DEFAULT 1
        CHECK (excludes_forfeits IN (0, 1)),
    excludes_void_remakes INTEGER NOT NULL DEFAULT 1
        CHECK (excludes_void_remakes IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (main_event_start_at <= main_event_end_at),
    CHECK (approval_status != 'approved' OR
           (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS event_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    evidence_urls_json TEXT NOT NULL DEFAULT '[]',
    evidence_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (evidence_status IN ('manually_audited', 'unverified')),
    evidence_json TEXT,
    audit_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (audit_status IN ('pending', 'approved', 'rejected', 'promoted')),
    audit_note TEXT,
    promoted_event_id TEXT REFERENCES event_registry(event_id),
    discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source, provider_event_id),
    CHECK (audit_status != 'promoted' OR promoted_event_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS raw_source_artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    source TEXT NOT NULL CHECK (source IN ('opendota', 'stratz')),
    artifact_use TEXT NOT NULL CHECK (artifact_use IN ('primary', 'fallback', 'cross_check')),
    endpoint TEXT NOT NULL,
    sanitized_request_identity TEXT NOT NULL,
    storage_path TEXT NOT NULL UNIQUE,
    uncompressed_bytes INTEGER NOT NULL CHECK (uncompressed_bytes >= 0),
    compressed_bytes INTEGER NOT NULL CHECK (compressed_bytes >= 0),
    source_at TEXT,
    received_at TEXT NOT NULL,
    first_usable_at TEXT,
    schema_fingerprint TEXT NOT NULL,
    event_id TEXT REFERENCES event_registry(event_id),
    match_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (source, content_hash)
);

CREATE TABLE IF NOT EXISTS raw_source_observations (
    observation_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES raw_source_artifacts(artifact_id),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    source TEXT NOT NULL CHECK (source IN ('opendota', 'stratz')),
    artifact_use TEXT NOT NULL CHECK (artifact_use IN ('primary', 'fallback', 'cross_check')),
    endpoint TEXT NOT NULL,
    sanitized_request_identity TEXT NOT NULL,
    source_at TEXT,
    received_at TEXT NOT NULL,
    first_usable_at TEXT,
    schema_fingerprint TEXT NOT NULL,
    event_id TEXT REFERENCES event_registry(event_id),
    match_id INTEGER,
    http_status INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_observations_match_time
    ON raw_source_observations(match_id, received_at);

CREATE TABLE IF NOT EXISTS match_ingest_status (
    match_id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES event_registry(event_id),
    start_time INTEGER,
    series_id INTEGER,
    map_number INTEGER CHECK (map_number IS NULL OR map_number > 0),
    stage_name TEXT,
    stage_scope TEXT NOT NULL DEFAULT 'unknown'
        CHECK (stage_scope IN
               ('main_event', 'internal_lcq', 'qualifier', 'division_2',
                'exhibition', 'unknown')),
    stage_in_scope INTEGER NOT NULL DEFAULT 0 CHECK (stage_in_scope IN (0, 1)),
    has_valid_result INTEGER NOT NULL DEFAULT 0 CHECK (has_valid_result IN (0, 1)),
    is_exhibition INTEGER NOT NULL DEFAULT 0 CHECK (is_exhibition IN (0, 1)),
    is_forfeit INTEGER NOT NULL DEFAULT 0 CHECK (is_forfeit IN (0, 1)),
    is_void_remake INTEGER NOT NULL DEFAULT 0 CHECK (is_void_remake IN (0, 1)),
    ingest_state TEXT NOT NULL DEFAULT 'discovered'
        CHECK (ingest_state IN
               ('discovered', 'basic_result', 'detail_pending', 'detailed',
                'cross_checked', 'complete', 'retryable', 'failed',
                'review_required')),
    basic_result_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (basic_result_state IN
               ('pending', 'ready', 'retryable', 'unscorable', 'review_required')),
    detailed_parse_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (detailed_parse_state IN
               ('pending', 'ready', 'retryable', 'unscorable', 'review_required')),
    cross_check_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (cross_check_state IN
               ('pending', 'ready', 'retryable', 'unscorable', 'review_required')),
    reconciliation_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK (reconciliation_status IN
               ('not_required', 'reconciliation_pending', 'reconciled',
                'review_required')),
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    latest_raw_artifact_id TEXT REFERENCES raw_source_artifacts(artifact_id),
    latest_raw_content_hash TEXT CHECK (
        latest_raw_content_hash IS NULL OR length(latest_raw_content_hash) = 64
    ),
    normalizer_version TEXT,
    raw_artifact_version INTEGER NOT NULL DEFAULT 0 CHECK (raw_artifact_version >= 0),
    attempt_generation INTEGER NOT NULL DEFAULT 0 CHECK (attempt_generation >= 0),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_retry_at TEXT,
    last_attempt_at TEXT,
    last_error TEXT,
    first_usable_at TEXT,
    player_readiness TEXT NOT NULL DEFAULT 'pending'
        CHECK (player_readiness IN
               ('pending', 'ready', 'retryable', 'unscorable', 'review_required')),
    state_readiness TEXT NOT NULL DEFAULT 'pending'
        CHECK (state_readiness IN
               ('pending', 'ready', 'retryable', 'unscorable', 'review_required')),
    draft_readiness TEXT NOT NULL DEFAULT 'pending'
        CHECK (draft_readiness IN
               ('pending', 'ready', 'retryable', 'unscorable', 'review_required')),
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_match_ingest_event ON match_ingest_status(event_id, match_id);
CREATE INDEX IF NOT EXISTS idx_match_ingest_retry ON match_ingest_status(next_retry_at);

CREATE TABLE IF NOT EXISTS player_role_assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES match_ingest_status(match_id) ON DELETE CASCADE,
    player_slot INTEGER NOT NULL,
    account_id INTEGER,
    team_id INTEGER,
    purpose TEXT NOT NULL CHECK (purpose IN ('observed_position', 'expected_position')),
    position INTEGER CHECK (position IS NULL OR position BETWEEN 1 AND 5),
    assignment_source TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    input_cutoff TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    assignment_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (match_id, player_slot, purpose, assignment_version)
);

CREATE TABLE IF NOT EXISTS player_map_facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES match_ingest_status(match_id) ON DELETE CASCADE,
    player_slot INTEGER NOT NULL,
    account_id INTEGER,
    team_id INTEGER,
    hero_id INTEGER,
    is_radiant INTEGER CHECK (is_radiant IN (0, 1)),
    facts_json TEXT NOT NULL,
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    coverage REAL NOT NULL CHECK (coverage BETWEEN 0.0 AND 1.0),
    source_artifact_id TEXT REFERENCES raw_source_artifacts(artifact_id),
    source_content_hash TEXT CHECK (
        source_content_hash IS NULL OR length(source_content_hash) = 64
    ),
    fact_version TEXT NOT NULL,
    first_usable_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (match_id, player_slot, fact_version)
);

CREATE TABLE IF NOT EXISTS player_map_scores (
    score_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES match_ingest_status(match_id) ON DELETE CASCADE,
    player_slot INTEGER NOT NULL,
    account_id INTEGER,
    position INTEGER CHECK (position IS NULL OR position BETWEEN 1 AND 5),
    execution_score REAL NOT NULL CHECK (execution_score BETWEEN 0.0 AND 100.0),
    result_adjusted_score REAL NOT NULL CHECK (result_adjusted_score BETWEEN 0.0 AND 100.0),
    component_facts_json TEXT NOT NULL,
    component_scores_json TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    coverage REAL NOT NULL CHECK (coverage BETWEEN 0.0 AND 1.0),
    role_confidence REAL NOT NULL CHECK (role_confidence BETWEEN 0.0 AND 1.0),
    benchmark_cutoff TEXT NOT NULL,
    benchmark_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    score_version TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (match_id, player_slot, score_version)
);

CREATE TABLE IF NOT EXISTS team_map_states (
    state_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES match_ingest_status(match_id) ON DELETE CASCADE,
    team_id INTEGER,
    side TEXT NOT NULL CHECK (side IN ('radiant', 'dire')),
    label TEXT NOT NULL CHECK (label IN
        ('comeback', 'throw', 'stomp', 'stomp_loss', 'advantage',
         'disadvantage', 'even', 'state_unscorable')),
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    max_lead REAL,
    max_deficit REAL,
    ahead_fraction REAL CHECK (ahead_fraction IS NULL OR ahead_fraction BETWEEN 0.0 AND 1.0),
    behind_fraction REAL CHECK (behind_fraction IS NULL OR behind_fraction BETWEEN 0.0 AND 1.0),
    even_fraction REAL CHECK (even_fraction IS NULL OR even_fraction BETWEEN 0.0 AND 1.0),
    signed_auc REAL,
    absolute_auc REAL,
    crossings_json TEXT NOT NULL DEFAULT '[]',
    first_significant_lead_at INTEGER,
    first_significant_deficit_at INTEGER,
    closeout_seconds INTEGER,
    objective_conversion_json TEXT NOT NULL DEFAULT '{}',
    curve_coverage REAL NOT NULL CHECK (curve_coverage BETWEEN 0.0 AND 1.0),
    source_versions_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    label_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (match_id, side, label_version)
);

CREATE TABLE IF NOT EXISTS team_style_profiles (
    profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    profile_cutoff TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    opportunity_counts_json TEXT NOT NULL,
    posterior_rates_json TEXT NOT NULL,
    duration_quantiles_json TEXT NOT NULL,
    weighting_json TEXT NOT NULL,
    effective_sample_size REAL NOT NULL CHECK (effective_sample_size >= 0.0),
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (team_id, profile_cutoff, profile_version)
);

CREATE TABLE IF NOT EXISTS draft_model_runs (
    run_id TEXT PRIMARY KEY,
    model_version TEXT NOT NULL,
    model_kind TEXT NOT NULL CHECK (model_kind IN ('pure_draft', 'context_adjusted')),
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes IN (10, 20, 30, 40, 50)),
    availability_mode TEXT NOT NULL
        CHECK (availability_mode IN ('reconstructed_walk_forward', 'prospective')),
    training_cutoff TEXT NOT NULL,
    feature_schema_hash TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    metrics_json TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES draft_model_runs(run_id),
    match_id INTEGER NOT NULL REFERENCES match_ingest_status(match_id),
    prediction_cutoff TEXT NOT NULL,
    cutoff_source TEXT NOT NULL,
    input_snapshot_hash TEXT NOT NULL,
    probability REAL CHECK (probability IS NULL OR probability BETWEEN 0.0 AND 1.0),
    uncertainty REAL CHECK (uncertainty IS NULL OR uncertainty >= 0.0),
    support INTEGER NOT NULL DEFAULT 0 CHECK (support >= 0),
    eventual_radiant_win INTEGER CHECK (eventual_radiant_win IS NULL OR eventual_radiant_win IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('predicted', 'insufficient_evidence', 'settled')),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, match_id)
);

CREATE TABLE IF NOT EXISTS draft_lineage_revisions (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    dependency_revision INTEGER NOT NULL CHECK (dependency_revision >= 1),
    artifact_revision INTEGER NOT NULL CHECK (artifact_revision >= 1),
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO draft_lineage_revisions
    (singleton, dependency_revision, artifact_revision, updated_at)
VALUES (1, 1, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE IF NOT EXISTS draft_lineage_changes (
    dependency_revision INTEGER PRIMARY KEY CHECK (dependency_revision >= 1),
    affected_from_unix INTEGER
        CHECK (affected_from_unix IS NULL OR affected_from_unix > 0),
    source_relation TEXT NOT NULL,
    operation TEXT NOT NULL
        CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE', 'REPAIR', 'INITIALIZE')),
    changed_at TEXT NOT NULL
);
INSERT INTO draft_lineage_changes
    (dependency_revision, affected_from_unix,
     source_relation, operation, changed_at)
SELECT 1, NULL, '__tracking__', 'INITIALIZE', updated_at
  FROM draft_lineage_revisions
 WHERE singleton=1
   AND NOT EXISTS (
       SELECT 1 FROM draft_lineage_changes WHERE dependency_revision=1
   );

CREATE TABLE IF NOT EXISTS draft_prediction_validations (
    run_id TEXT NOT NULL,
    match_id INTEGER NOT NULL,
    input_snapshot_hash TEXT NOT NULL
        CHECK (length(input_snapshot_hash) = 64),
    artifact_fingerprint TEXT NOT NULL
        CHECK (length(artifact_fingerprint) = 64),
    dependency_fingerprint TEXT NOT NULL
        CHECK (length(dependency_fingerprint) = 64),
    dependency_revision INTEGER NOT NULL CHECK (dependency_revision >= 1),
    validation_version TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, match_id),
    FOREIGN KEY (run_id, match_id)
        REFERENCES draft_predictions(run_id, match_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_draft_prediction_validations_fingerprint
    ON draft_prediction_validations(validation_version, dependency_fingerprint);

CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('filled', 'settled')),
    channel TEXT NOT NULL DEFAULT 'email',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'sent', 'dead_letter')),
    recipient TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    statistics_cutoff TEXT NOT NULL,
    template_version TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_error TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (order_key, event_type, channel)
);
CREATE INDEX IF NOT EXISTS idx_notification_due
    ON notification_outbox(status, next_attempt_at, lease_until);

CREATE TABLE IF NOT EXISTS service_health (
    component TEXT PRIMARY KEY,
    status TEXT NOT NULL
        CHECK (status IN ('starting', 'healthy', 'degraded', 'unhealthy', 'stopped')),
    last_heartbeat_at TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_scheduler_checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    checkpoint_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_scheduler_retry_state (
    checkpoint_key TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL CHECK (failure_count > 0),
    next_retry_at TEXT NOT NULL,
    last_error TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strict_derived_status (
    match_id INTEGER PRIMARY KEY
        REFERENCES match_ingest_status(match_id) ON DELETE CASCADE,
    source_content_hash TEXT NOT NULL CHECK (length(source_content_hash) = 64),
    role_assignment_version TEXT NOT NULL,
    score_version TEXT NOT NULL,
    team_state_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    profile_cutoff TEXT NOT NULL,
    derived_at TEXT NOT NULL,
    normalizer_version TEXT NOT NULL,
    benchmark_version TEXT NOT NULL,
    profile_context_hash TEXT NOT NULL CHECK (length(profile_context_hash) = 64)
);

CREATE VIEW IF NOT EXISTS formal_events AS
SELECT *
FROM event_registry
WHERE scope = 'formal_main_event'
  AND approval_status = 'approved'
  AND evidence_status = 'manually_audited'
  AND tier = 'tier_1'
  AND prize_pool_usd >= 1000000;

CREATE VIEW IF NOT EXISTS formal_map_eligibility AS
SELECT
    m.match_id,
    m.event_id,
    e.opendota_league_id,
    m.stage_scope,
    m.ingest_state,
    m.player_readiness,
    m.state_readiness,
    m.draft_readiness
FROM match_ingest_status AS m
JOIN formal_events AS e ON e.event_id = m.event_id
WHERE m.stage_in_scope = 1
  AND m.has_valid_result = 1
  AND m.is_exhibition = 0
  AND m.is_forfeit = 0
  AND m.is_void_remake = 0
  AND (
      m.stage_scope = 'main_event'
      OR (m.stage_scope = 'internal_lcq' AND e.include_internal_lcq = 1)
  );
"""


class IntelligenceStorage:
    """One configured SQLite connection plus idempotent additive migrations."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.connection = (
            connection
            if connection is not None
            else sqlite3.connect(self.path, timeout=busy_timeout_ms / 1_000)
        )
        self._owns_connection = connection is None
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._transaction_depth = 0
        self._savepoint_sequence = 0

    def __enter__(self) -> "IntelligenceStorage":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def init_schema(self, *, seed_events: bool = True) -> None:
        self._reject_future_schema()
        with self.transaction():
            for statement in self._schema_statements(SCHEMA_SQL):
                self.connection.execute(statement)
            self._migrate_schema()
            if seed_events:
                from .registry import EventRegistry

                registry = EventRegistry(self)
                registry.seed_approved_events()
                self._verify_seeded_events(registry)
            self.connection.executemany(
                """INSERT OR IGNORE INTO intelligence_schema_version
                   (version, applied_at)
                   VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
                ((1,), (CURRENT_SCHEMA_VERSION,)),
            )

    def _reject_future_schema(self) -> None:
        exists = self.connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='intelligence_schema_version'"""
        ).fetchone()
        if exists is None:
            return
        row = self.connection.execute(
            "SELECT MAX(version) FROM intelligence_schema_version"
        ).fetchone()
        version = row[0] if row is not None else None
        if version is not None and int(version) > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than supported "
                f"version {CURRENT_SCHEMA_VERSION}"
            )

    @staticmethod
    def _schema_statements(script: str) -> Iterator[str]:
        buffer = ""
        for line in script.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statement = buffer.strip()
                if statement:
                    yield statement
                buffer = ""
        if buffer.strip():
            raise RuntimeError("incomplete intelligence schema statement")

    def _migrate_schema(self) -> None:
        prior_schema = self.connection.execute(
            "SELECT MAX(version) FROM intelligence_schema_version"
        ).fetchone()
        prior_version = prior_schema[0] if prior_schema is not None else None
        if (
            prior_version is not None
            and int(prior_version) < CUTOFF_LINEAGE_SCHEMA_VERSION
        ):
            # Pre-v8 proofs have no cutoff-scoped change journal and cannot be
            # promoted safely. The immutable runs/predictions remain intact.
            self.connection.execute("DELETE FROM draft_prediction_validations")

        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(match_ingest_status)")
        }
        if "start_time" not in columns:
            self.connection.execute(
                "ALTER TABLE match_ingest_status ADD COLUMN start_time INTEGER"
            )
        if "normalizer_version" not in columns:
            self.connection.execute(
                "ALTER TABLE match_ingest_status ADD COLUMN normalizer_version TEXT"
            )
        if "attempt_generation" not in columns:
            self.connection.execute(
                """ALTER TABLE match_ingest_status
                   ADD COLUMN attempt_generation INTEGER NOT NULL DEFAULT 0"""
            )
        self.connection.execute(
            """UPDATE match_ingest_status
                  SET normalizer_version='opendota-exact-v1'
                WHERE normalizer_version IS NULL
                  AND latest_raw_content_hash IS NOT NULL
                  AND (SELECT COUNT(*)
                         FROM player_map_facts AS facts
                        WHERE facts.match_id=match_ingest_status.match_id
                          AND facts.source_content_hash=
                              match_ingest_status.latest_raw_content_hash
                          AND facts.fact_version='opendota-exact-v1:' ||
                              match_ingest_status.latest_raw_content_hash) = 10"""
        )

        derived_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(strict_derived_status)"
            )
        }
        if "normalizer_version" not in derived_columns:
            self.connection.execute(
                "ALTER TABLE strict_derived_status ADD COLUMN normalizer_version TEXT"
            )
        if "benchmark_version" not in derived_columns:
            self.connection.execute(
                "ALTER TABLE strict_derived_status ADD COLUMN benchmark_version TEXT"
            )
        if "profile_context_hash" not in derived_columns:
            # Existing lineage cannot prove which registry metadata was used.
            # Leave this nullable so the incremental pipeline invalidates it
            # instead of blessing potentially stale profiles during migration.
            self.connection.execute(
                """ALTER TABLE strict_derived_status
                   ADD COLUMN profile_context_hash TEXT
                   CHECK (profile_context_hash IS NULL OR
                          length(profile_context_hash) = 64)"""
            )

        validation_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(draft_prediction_validations)"
            )
        }
        if validation_columns and "artifact_fingerprint" not in validation_columns:
            self.connection.execute(
                """ALTER TABLE draft_prediction_validations
                   ADD COLUMN artifact_fingerprint TEXT
                   CHECK (artifact_fingerprint IS NULL OR
                          length(artifact_fingerprint) = 64)"""
            )
        if validation_columns and "dependency_revision" not in validation_columns:
            self.connection.execute(
                """ALTER TABLE draft_prediction_validations
                   ADD COLUMN dependency_revision INTEGER
                   CHECK (dependency_revision IS NULL OR dependency_revision >= 1)"""
            )

        artifact_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(raw_source_artifacts)"
            )
        }
        if artifact_columns and "artifact_id" not in artifact_columns:
            raise RuntimeError(
                "pre-release raw artifact schema cannot distinguish source and hash"
            )

    @staticmethod
    def _verify_seeded_events(registry: object) -> None:
        from .registry import (
            APPROVED_EVENT_SEEDS,
            AUDITED_AT,
            EXCLUDED_CATEGORIES,
            SCOPE_POLICY_VERSION,
        )

        events = {event.event_id: event for event in registry.formal_events()}
        expected_ids = {str(seed["event_id"]) for seed in APPROVED_EVENT_SEEDS}
        if set(events) != expected_ids:
            raise RuntimeError(
                "approved event seed set conflicts with the audited registry"
            )
        for seed in APPROVED_EVENT_SEEDS:
            event = events[str(seed["event_id"])]
            expected_map_count = seed["expected_map_count"]
            expected = (
                str(seed["canonical_name"]),
                str(seed["tier"]),
                int(seed["prize_pool_usd"]),
                datetime.fromisoformat(str(seed["main_event_start_at"])),
                datetime.fromisoformat(str(seed["main_event_end_at"])),
                int(seed["opendota_league_id"]),
                (),
                tuple(seed["official_evidence_urls"]),
                "manually_audited",
                SCOPE_POLICY_VERSION,
                "formal_main_event",
                "approved",
                "manual_event_audit",
                datetime.fromisoformat(AUDITED_AT),
                (int(expected_map_count) if expected_map_count is not None else None),
                tuple(seed["included_stages"]),
                tuple(EXCLUDED_CATEGORIES),
                bool(seed["include_internal_lcq"]),
            )
            actual = (
                event.canonical_name,
                event.tier,
                event.prize_pool_usd,
                event.main_event_start_at,
                event.main_event_end_at,
                event.opendota_league_id,
                event.secondary_provider_ids,
                event.official_evidence_urls,
                event.evidence_status.value,
                event.scope_policy_version,
                event.scope.value,
                event.approval_status.value,
                event.approved_by,
                event.approved_at,
                event.expected_map_count,
                tuple(stage.value for stage in event.included_stages),
                event.excluded_categories,
                event.include_internal_lcq,
            )
            if actual != expected:
                raise RuntimeError(
                    f"approved event seed policy drift for {event.event_id}"
                )

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> sqlite3.Cursor:
        try:
            cursor = self.connection.execute(sql, parameters)
        except Exception:
            if self._transaction_depth == 0:
                self.connection.rollback()
            raise
        if self._transaction_depth == 0:
            self.connection.commit()
        return cursor

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._transaction_depth:
            self._savepoint_sequence += 1
            name = f"intelligence_{self._savepoint_sequence}"
            with self._savepoint(name):
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
            return

        self.connection.execute("BEGIN IMMEDIATE")
        self._transaction_depth = 1
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
        finally:
            self._transaction_depth = 0

    @contextmanager
    def _savepoint(self, name: str) -> Iterator[None]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid savepoint name")
        self.connection.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.connection.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.connection.execute(f"RELEASE SAVEPOINT {name}")
