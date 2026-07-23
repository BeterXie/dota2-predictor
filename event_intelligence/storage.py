"""Additive SQLite schema for strict-event intelligence."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


BUSY_TIMEOUT_MS = 5_000
CURRENT_SCHEMA_VERSION = 10
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

CREATE TABLE IF NOT EXISTS raw_source_artifact_relocations (
    relocation_id TEXT PRIMARY KEY CHECK (length(relocation_id) = 64),
    relocation_sequence INTEGER NOT NULL CHECK (relocation_sequence > 0),
    artifact_id TEXT NOT NULL REFERENCES raw_source_artifacts(artifact_id),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    source TEXT NOT NULL CHECK (source IN ('opendota', 'stratz')),
    old_storage_path TEXT NOT NULL,
    new_storage_path TEXT NOT NULL,
    uncompressed_bytes INTEGER NOT NULL CHECK (uncompressed_bytes >= 0),
    compressed_bytes INTEGER NOT NULL CHECK (compressed_bytes >= 0),
    schema_fingerprint TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    relocated_at TEXT NOT NULL,
    UNIQUE (artifact_id, relocation_sequence),
    CHECK (old_storage_path != new_storage_path)
);

CREATE TRIGGER IF NOT EXISTS raw_source_artifact_relocations_guard_insert
BEFORE INSERT ON raw_source_artifact_relocations
WHEN NOT EXISTS (
        SELECT 1 FROM raw_source_artifacts AS artifact
         WHERE artifact.artifact_id=NEW.artifact_id
           AND artifact.content_hash=NEW.content_hash
           AND artifact.source=NEW.source
           AND artifact.storage_path=NEW.old_storage_path
           AND artifact.uncompressed_bytes=NEW.uncompressed_bytes
           AND artifact.compressed_bytes=NEW.compressed_bytes
           AND artifact.schema_fingerprint=NEW.schema_fingerprint
    )
    OR NEW.relocation_sequence != COALESCE(
        (SELECT MAX(existing.relocation_sequence) + 1
           FROM raw_source_artifact_relocations AS existing
          WHERE existing.artifact_id=NEW.artifact_id),
        1
    )
BEGIN
    SELECT RAISE(ABORT, 'raw source relocation authority mismatch');
END;

CREATE TRIGGER IF NOT EXISTS raw_source_artifact_relocations_immutable_update
BEFORE UPDATE ON raw_source_artifact_relocations
BEGIN
    SELECT RAISE(ABORT, 'raw source relocation audit is immutable');
END;

CREATE TRIGGER IF NOT EXISTS raw_source_artifact_relocations_immutable_delete
BEFORE DELETE ON raw_source_artifact_relocations
BEGIN
    SELECT RAISE(ABORT, 'raw source relocation audit is immutable');
END;

CREATE TRIGGER IF NOT EXISTS raw_source_artifacts_identity_immutable
BEFORE UPDATE ON raw_source_artifacts
WHEN NEW.artifact_id IS NOT OLD.artifact_id
    OR NEW.content_hash IS NOT OLD.content_hash
    OR NEW.source IS NOT OLD.source
    OR NEW.artifact_use IS NOT OLD.artifact_use
    OR NEW.endpoint IS NOT OLD.endpoint
    OR NEW.sanitized_request_identity IS NOT OLD.sanitized_request_identity
    OR NEW.uncompressed_bytes IS NOT OLD.uncompressed_bytes
    OR NEW.compressed_bytes IS NOT OLD.compressed_bytes
    OR NEW.source_at IS NOT OLD.source_at
    OR NEW.received_at IS NOT OLD.received_at
    OR NEW.schema_fingerprint IS NOT OLD.schema_fingerprint
    OR NEW.event_id IS NOT OLD.event_id
    OR NEW.match_id IS NOT OLD.match_id
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'raw source artifact identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS raw_source_artifacts_relocation_required
BEFORE UPDATE OF storage_path ON raw_source_artifacts
WHEN NEW.storage_path IS NOT OLD.storage_path
 AND NOT EXISTS (
     SELECT 1 FROM raw_source_artifact_relocations AS relocation
      WHERE relocation.artifact_id=OLD.artifact_id
        AND relocation.content_hash=OLD.content_hash
        AND relocation.source=OLD.source
        AND relocation.old_storage_path=OLD.storage_path
        AND relocation.new_storage_path=NEW.storage_path
        AND relocation.uncompressed_bytes=OLD.uncompressed_bytes
        AND relocation.compressed_bytes=OLD.compressed_bytes
        AND relocation.schema_fingerprint=OLD.schema_fingerprint
        AND relocation.relocation_sequence=(
            SELECT MAX(latest.relocation_sequence)
              FROM raw_source_artifact_relocations AS latest
             WHERE latest.artifact_id=OLD.artifact_id
        )
 )
BEGIN
    SELECT RAISE(ABORT, 'raw source artifact relocation audit is required');
END;

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

CREATE TABLE IF NOT EXISTS historical_rosh_lineup_scores (
    score_key TEXT PRIMARY KEY CHECK (length(score_key) = 64),
    match_id INTEGER NOT NULL
        REFERENCES match_ingest_status(match_id) ON DELETE CASCADE,
    radiant_hero_ids_json TEXT NOT NULL CHECK (
        json_valid(radiant_hero_ids_json)
        AND json_type(radiant_hero_ids_json) = 'array'
        AND json_array_length(radiant_hero_ids_json) = 5
    ),
    dire_hero_ids_json TEXT NOT NULL CHECK (
        json_valid(dire_hero_ids_json)
        AND json_type(dire_hero_ids_json) = 'array'
        AND json_array_length(dire_hero_ids_json) = 5
    ),
    radiant_player_ids_json TEXT NOT NULL CHECK (
        json_valid(radiant_player_ids_json)
        AND json_type(radiant_player_ids_json) = 'array'
        AND json_array_length(radiant_player_ids_json) = 5
    ),
    dire_player_ids_json TEXT NOT NULL CHECK (
        json_valid(dire_player_ids_json)
        AND json_type(dire_player_ids_json) = 'array'
        AND json_array_length(dire_player_ids_json) = 5
    ),
    pure_lineup_score REAL NOT NULL CHECK (
        typeof(pure_lineup_score) IN ('integer', 'real')
    ),
    current_player_adjusted_lineup_score REAL CHECK (
        current_player_adjusted_lineup_score IS NULL
        OR typeof(current_player_adjusted_lineup_score) IN ('integer', 'real')
    ),
    effective_lineup_score REAL NOT NULL CHECK (
        typeof(effective_lineup_score) IN ('integer', 'real')
    ),
    scoring_mode TEXT NOT NULL CHECK (
        scoring_mode IN ('pure', 'current_player_adjusted')
    ),
    player_coverage_count INTEGER NOT NULL CHECK (
        player_coverage_count BETWEEN 0 AND 10
    ),
    source_name TEXT NOT NULL CHECK (source_name = 'stratz'),
    source_week INTEGER NOT NULL CHECK (source_week > 0),
    source_as_of TEXT NOT NULL,
    player_stats_as_of TEXT,
    formula_version TEXT NOT NULL CHECK (length(trim(formula_version)) > 0),
    evidence_json TEXT NOT NULL CHECK (
        json_valid(evidence_json) AND json_type(evidence_json) = 'object'
    ),
    evidence_hash TEXT NOT NULL CHECK (length(evidence_hash) = 64),
    backtest_eligible INTEGER NOT NULL DEFAULT 0 CHECK (backtest_eligible = 0),
    created_at TEXT NOT NULL,
    CHECK (
        (scoring_mode = 'current_player_adjusted'
         AND player_coverage_count = 10
         AND current_player_adjusted_lineup_score IS NOT NULL
         AND effective_lineup_score = current_player_adjusted_lineup_score
         AND player_stats_as_of IS NOT NULL)
        OR
        (scoring_mode = 'pure'
         AND player_coverage_count < 10
         AND current_player_adjusted_lineup_score IS NULL
         AND effective_lineup_score = pure_lineup_score)
    )
);
CREATE INDEX IF NOT EXISTS idx_historical_rosh_match_version
    ON historical_rosh_lineup_scores(
        match_id, formula_version, created_at DESC, score_key DESC
    );
CREATE TRIGGER IF NOT EXISTS historical_rosh_lineup_scores_immutable_update
BEFORE UPDATE ON historical_rosh_lineup_scores
BEGIN
    SELECT RAISE(ABORT, 'historical Rosh lineup score is immutable');
END;
CREATE TRIGGER IF NOT EXISTS historical_rosh_lineup_scores_immutable_delete
BEFORE DELETE ON historical_rosh_lineup_scores
BEGIN
    SELECT RAISE(ABORT, 'historical Rosh lineup score is immutable');
END;

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
CREATE INDEX IF NOT EXISTS idx_draft_predictions_match
    ON draft_predictions(match_id, run_id);

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


@dataclass(frozen=True)
class HistoricalRoshLineupScore:
    score_key: str
    match_id: int
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    radiant_player_ids: tuple[int, ...]
    dire_player_ids: tuple[int, ...]
    pure_lineup_score: float
    current_player_adjusted_lineup_score: float | None
    effective_lineup_score: float
    scoring_mode: str
    player_coverage_count: int
    source_name: str
    source_week: int
    source_as_of: datetime
    player_stats_as_of: datetime | None
    formula_version: str
    evidence: Mapping[str, Any]
    evidence_hash: str
    backtest_eligible: bool
    created_at: datetime


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _aware_datetime(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _identity_ids(value: Sequence[Any], name: str) -> tuple[int, ...]:
    if len(value) != 5 or any(type(item) is not int or item <= 0 for item in value):
        raise ValueError(f"{name} must contain five positive integer IDs")
    result = tuple(int(item) for item in value)
    if len(set(result)) != 5:
        raise ValueError(f"{name} must contain five unique IDs")
    return result


_ROSH_MINUTE_NUMERIC_FIELDS = (
    "advantage_percent",
    "radiant_advantage",
    "dire_advantage",
    "match_percentage",
    "win_rate_graph",
    "hero_adjustment",
    "hero_base_adjustment",
    "hero_tempo_adjustment",
    "synergy_adjustment",
    "player_adjustment",
)


def _valid_rosh_minute_table(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    previous_minute = 19
    for bucket in value:
        if not isinstance(bucket, Mapping):
            return False
        minute = bucket.get("minute")
        time_start = bucket.get("time_start")
        time_end = bucket.get("time_end")
        if (
            type(minute) is not int
            or type(time_start) is not int
            or type(time_end) is not int
            or not 20 <= time_start <= minute <= time_end <= 60
            or minute <= previous_minute
            or bucket.get("advantage_side") not in {"radiant", "dire", "even"}
        ):
            return False
        previous_minute = minute
        if any(
            isinstance(bucket.get(field), bool)
            or not isinstance(bucket.get(field), (int, float))
            or not math.isfinite(float(bucket[field]))
            for field in _ROSH_MINUTE_NUMERIC_FIELDS
        ):
            return False
    return True


def _valid_historical_rosh_evidence(
    evidence: Mapping[str, Any],
    *,
    match_id: int,
    pure_score: float,
    adjusted_score: float | None,
    effective_score: float,
    scoring_mode: str,
    player_coverage_count: int,
    source_name: str,
    source_week: int,
    source_as_of: str,
    player_stats_as_of: str | None,
    formula_version: str,
) -> bool:
    metadata_keys = {
        "historical_match_id",
        "source",
        "formula_version",
        "source_week",
        "source_as_of",
        "player_stats_as_of",
        "retrospective",
        "current_player_adjustment_only",
        "backtest_eligible",
    }
    if not metadata_keys.issubset(evidence):
        return False
    expected_metadata = {
        "historical_match_id": match_id,
        "source": source_name,
        "formula_version": formula_version,
        "source_week": source_week,
        "source_as_of": source_as_of,
        "player_stats_as_of": player_stats_as_of,
        "retrospective": True,
        "current_player_adjustment_only": True,
        "backtest_eligible": False,
    }
    if (
        any(evidence.get(key) != value for key, value in expected_metadata.items())
        or type(evidence.get("historical_match_id")) is not int
        or type(evidence.get("source_week")) is not int
        or evidence.get("retrospective") is not True
        or evidence.get("current_player_adjustment_only") is not True
        or evidence.get("backtest_eligible") is not False
    ):
        return False
    score = evidence.get("score")
    expected_score = {
        "pure_lineup_score": pure_score,
        "current_player_adjusted_lineup_score": adjusted_score,
        "effective_lineup_score": effective_score,
        "scoring_mode": scoring_mode,
        "player_coverage_count": player_coverage_count,
    }
    if (
        not isinstance(score, Mapping)
        or not set(expected_score).issubset(score)
        or any(score.get(key) != value for key, value in expected_score.items())
        or any(
            isinstance(score.get(key), bool)
            or not isinstance(score.get(key), (int, float))
            or not math.isfinite(float(score[key]))
            for key in (
                "pure_lineup_score",
                "effective_lineup_score",
                "player_coverage_count",
            )
        )
        or type(score.get("player_coverage_count")) is not int
        or (
            adjusted_score is not None
            and (
                isinstance(score.get("current_player_adjusted_lineup_score"), bool)
                or not isinstance(
                    score.get("current_player_adjusted_lineup_score"), (int, float)
                )
                or not math.isfinite(
                    float(score["current_player_adjusted_lineup_score"])
                )
            )
        )
    ):
        return False
    pure_table = evidence.get("pure_minute_table")
    if (
        not _valid_rosh_minute_table(pure_table)
        or float(pure_table[-1]["win_rate_graph"]) != pure_score
    ):
        return False
    if scoring_mode == "current_player_adjusted":
        adjusted_table = evidence.get("minute_table")
        return bool(
            _valid_rosh_minute_table(adjusted_table)
            and adjusted_score is not None
            and float(adjusted_table[-1]["win_rate_graph"]) == adjusted_score
            and [row["minute"] for row in adjusted_table]
            == [row["minute"] for row in pure_table]
        )
    return "minute_table" not in evidence


def _historical_rosh_score_from_row(
    row: sqlite3.Row | Mapping[str, Any],
) -> HistoricalRoshLineupScore | None:
    try:
        payload = dict(row)
        radiant_heroes = _identity_ids(
            json.loads(str(payload["radiant_hero_ids_json"])), "radiant heroes"
        )
        dire_heroes = _identity_ids(
            json.loads(str(payload["dire_hero_ids_json"])), "dire heroes"
        )
        radiant_players = _identity_ids(
            json.loads(str(payload["radiant_player_ids_json"])), "radiant players"
        )
        dire_players = _identity_ids(
            json.loads(str(payload["dire_player_ids_json"])), "dire players"
        )
        if len(set((*radiant_heroes, *dire_heroes))) != 10:
            return None
        if len(set((*radiant_players, *dire_players))) != 10:
            return None
        evidence = json.loads(str(payload["evidence_json"]))
        if not isinstance(evidence, dict):
            return None
        evidence_hash = str(payload["evidence_hash"])
        if _sha256_json(evidence) != evidence_hash:
            return None
        source_as_of = _aware_datetime(payload["source_as_of"])
        created_at = _aware_datetime(payload["created_at"])
        raw_player_stats_as_of = payload["player_stats_as_of"]
        player_stats_as_of = (
            None
            if raw_player_stats_as_of is None
            else _aware_datetime(raw_player_stats_as_of)
        )
        if source_as_of is None or created_at is None:
            return None
        if raw_player_stats_as_of is not None and player_stats_as_of is None:
            return None
        pure = float(payload["pure_lineup_score"])
        raw_adjusted = payload["current_player_adjusted_lineup_score"]
        adjusted = None if raw_adjusted is None else float(raw_adjusted)
        effective = float(payload["effective_lineup_score"])
        score_values = (
            (pure, effective) if adjusted is None else (pure, effective, adjusted)
        )
        if any(not math.isfinite(value) for value in score_values):
            return None
        mode = str(payload["scoring_mode"])
        coverage = int(payload["player_coverage_count"])
        invariant = (
            mode == "current_player_adjusted"
            and coverage == 10
            and adjusted is not None
            and effective == adjusted
            and player_stats_as_of is not None
        ) or (
            mode == "pure"
            and 0 <= coverage < 10
            and adjusted is None
            and effective == pure
        )
        source_name = str(payload["source_name"])
        source_week = int(payload["source_week"])
        formula_version = str(payload["formula_version"])
        if (
            not invariant
            or int(payload["backtest_eligible"]) != 0
            or source_name != "stratz"
            or source_week <= 0
            or not formula_version.strip()
            or source_as_of > created_at
            or (player_stats_as_of is not None and player_stats_as_of > created_at)
            or not _valid_historical_rosh_evidence(
                evidence,
                match_id=int(payload["match_id"]),
                pure_score=pure,
                adjusted_score=adjusted,
                effective_score=effective,
                scoring_mode=mode,
                player_coverage_count=coverage,
                source_name=source_name,
                source_week=source_week,
                source_as_of=source_as_of.isoformat(),
                player_stats_as_of=(
                    None
                    if player_stats_as_of is None
                    else player_stats_as_of.isoformat()
                ),
                formula_version=formula_version,
            )
        ):
            return None
        return HistoricalRoshLineupScore(
            score_key=str(payload["score_key"]),
            match_id=int(payload["match_id"]),
            radiant_hero_ids=radiant_heroes,
            dire_hero_ids=dire_heroes,
            radiant_player_ids=radiant_players,
            dire_player_ids=dire_players,
            pure_lineup_score=pure,
            current_player_adjusted_lineup_score=adjusted,
            effective_lineup_score=effective,
            scoring_mode=mode,
            player_coverage_count=coverage,
            source_name=source_name,
            source_week=source_week,
            source_as_of=source_as_of,
            player_stats_as_of=player_stats_as_of,
            formula_version=formula_version,
            evidence=evidence,
            evidence_hash=evidence_hash,
            backtest_eligible=False,
            created_at=created_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def query_historical_rosh_lineup_score_for_match(
    connection: sqlite3.Connection,
    *,
    match_id: int,
    formula_version: str,
) -> HistoricalRoshLineupScore | None:
    relation = connection.execute(
        """SELECT 1 FROM sqlite_master
             WHERE type='table' AND name='historical_rosh_lineup_scores'"""
    ).fetchone()
    if relation is None:
        return None
    try:
        rows = connection.execute(
            """SELECT * FROM historical_rosh_lineup_scores
                WHERE match_id=? AND formula_version=?
                ORDER BY created_at DESC, score_key DESC""",
            (match_id, formula_version),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        parsed = _historical_rosh_score_from_row(row)
        if parsed is not None:
            return parsed
    return None


def query_historical_rosh_lineup_score(
    connection: sqlite3.Connection,
    *,
    match_id: int,
    formula_version: str,
    radiant_hero_ids: Sequence[int],
    dire_hero_ids: Sequence[int],
    radiant_player_ids: Sequence[int],
    dire_player_ids: Sequence[int],
) -> HistoricalRoshLineupScore | None:
    try:
        expected = (
            _identity_ids(radiant_hero_ids, "radiant heroes"),
            _identity_ids(dire_hero_ids, "dire heroes"),
            _identity_ids(radiant_player_ids, "radiant players"),
            _identity_ids(dire_player_ids, "dire players"),
        )
    except ValueError:
        return None
    relation = connection.execute(
        """SELECT 1 FROM sqlite_master
             WHERE type='table' AND name='historical_rosh_lineup_scores'"""
    ).fetchone()
    if relation is None:
        return None
    try:
        rows = connection.execute(
            """SELECT * FROM historical_rosh_lineup_scores
                WHERE match_id=? AND formula_version=?
                ORDER BY created_at DESC, score_key DESC""",
            (match_id, formula_version),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        parsed = _historical_rosh_score_from_row(row)
        if parsed is None:
            continue
        actual = (
            parsed.radiant_hero_ids,
            parsed.dire_hero_ids,
            parsed.radiant_player_ids,
            parsed.dire_player_ids,
        )
        if actual == expected:
            return parsed
    return None


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
        journal_mode = str(
            self.connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        if journal_mode != "wal":
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

    def init_schema(
        self,
        *,
        seed_events: bool = True,
        external_transaction: bool = False,
    ) -> None:
        self._reject_future_schema()
        transaction = (
            self._external_transaction()
            if external_transaction
            else self.transaction()
        )
        with transaction:
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

    @contextmanager
    def _external_transaction(self) -> Iterator[None]:
        if not self.connection.in_transaction:
            raise RuntimeError("external transaction is not active")
        self._transaction_depth += 1
        try:
            yield
        finally:
            self._transaction_depth -= 1

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

    def insert_historical_rosh_lineup_score(
        self,
        *,
        match_id: int,
        radiant_hero_ids: Sequence[int],
        dire_hero_ids: Sequence[int],
        radiant_player_ids: Sequence[int],
        dire_player_ids: Sequence[int],
        pure_lineup_score: float,
        current_player_adjusted_lineup_score: float | None,
        effective_lineup_score: float,
        scoring_mode: str,
        player_coverage_count: int,
        source_week: int,
        source_as_of: datetime,
        player_stats_as_of: datetime | None,
        formula_version: str,
        evidence: Mapping[str, Any],
        created_at: datetime,
        evidence_hash: str | None = None,
        source_name: str = "stratz",
    ) -> HistoricalRoshLineupScore:
        if type(match_id) is not int or match_id <= 0:
            raise ValueError("match_id must be a positive integer")
        radiant_heroes = _identity_ids(radiant_hero_ids, "radiant heroes")
        dire_heroes = _identity_ids(dire_hero_ids, "dire heroes")
        radiant_players = _identity_ids(radiant_player_ids, "radiant players")
        dire_players = _identity_ids(dire_player_ids, "dire players")
        if len(set((*radiant_heroes, *dire_heroes))) != 10:
            raise ValueError("historical Rosh hero IDs must be unique")
        if len(set((*radiant_players, *dire_players))) != 10:
            raise ValueError("historical Rosh player IDs must be unique")

        try:
            pure = float(pure_lineup_score)
            adjusted = (
                None
                if current_player_adjusted_lineup_score is None
                else float(current_player_adjusted_lineup_score)
            )
            effective = float(effective_lineup_score)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("historical Rosh scores must be finite numbers") from error
        score_values = (
            (pure, effective) if adjusted is None else (pure, effective, adjusted)
        )
        if any(not math.isfinite(value) for value in score_values):
            raise ValueError("historical Rosh scores must be finite numbers")
        if type(player_coverage_count) is not int:
            raise ValueError("player_coverage_count must be an integer")
        invariant = (
            scoring_mode == "current_player_adjusted"
            and player_coverage_count == 10
            and adjusted is not None
            and effective == adjusted
            and player_stats_as_of is not None
        ) or (
            scoring_mode == "pure"
            and 0 <= player_coverage_count < 10
            and adjusted is None
            and effective == pure
        )
        if not invariant:
            raise ValueError("historical Rosh scoring mode invariant failed")
        if source_name != "stratz":
            raise ValueError("historical Rosh source_name must be stratz")
        if type(source_week) is not int or source_week <= 0:
            raise ValueError("source_week must be a positive integer")
        if not isinstance(formula_version, str) or not formula_version.strip():
            raise ValueError("formula_version must be non-empty")

        source_at = _aware_datetime(source_as_of)
        player_stats_at = (
            None
            if player_stats_as_of is None
            else _aware_datetime(player_stats_as_of)
        )
        created = _aware_datetime(created_at)
        if source_at is None or created is None:
            raise ValueError("source_as_of and created_at must include timezones")
        if player_stats_as_of is not None and player_stats_at is None:
            raise ValueError("player_stats_as_of must include a timezone")
        if source_at > created or (
            player_stats_at is not None and player_stats_at > created
        ):
            raise ValueError("historical Rosh evidence cannot be newer than its row")

        evidence_payload = dict(evidence)
        if not _valid_historical_rosh_evidence(
            evidence_payload,
            match_id=match_id,
            pure_score=pure,
            adjusted_score=adjusted,
            effective_score=effective,
            scoring_mode=scoring_mode,
            player_coverage_count=player_coverage_count,
            source_name=source_name,
            source_week=source_week,
            source_as_of=source_at.isoformat(),
            player_stats_as_of=(
                None if player_stats_at is None else player_stats_at.isoformat()
            ),
            formula_version=formula_version,
        ):
            raise ValueError("historical Rosh evidence does not match score columns")
        try:
            evidence_json = _canonical_json(evidence_payload)
        except (TypeError, ValueError) as error:
            raise ValueError("evidence must be finite JSON") from error
        calculated_evidence_hash = hashlib.sha256(
            evidence_json.encode("utf-8")
        ).hexdigest()
        if evidence_hash is not None and evidence_hash != calculated_evidence_hash:
            raise ValueError("historical Rosh evidence hash mismatch")
        identity = {
            "match_id": match_id,
            "radiant_hero_ids": radiant_heroes,
            "dire_hero_ids": dire_heroes,
            "radiant_player_ids": radiant_players,
            "dire_player_ids": dire_players,
            "pure_lineup_score": pure,
            "current_player_adjusted_lineup_score": adjusted,
            "effective_lineup_score": effective,
            "scoring_mode": scoring_mode,
            "player_coverage_count": player_coverage_count,
            "source_name": source_name,
            "source_week": source_week,
            "source_as_of": source_at.isoformat(),
            "player_stats_as_of": (
                None if player_stats_at is None else player_stats_at.isoformat()
            ),
            "formula_version": formula_version,
            "evidence_hash": calculated_evidence_hash,
        }
        score_key = _sha256_json(identity)
        self.execute(
            """INSERT OR IGNORE INTO historical_rosh_lineup_scores
               (score_key, match_id, radiant_hero_ids_json,
                dire_hero_ids_json, radiant_player_ids_json,
                dire_player_ids_json, pure_lineup_score,
                current_player_adjusted_lineup_score, effective_lineup_score,
                scoring_mode, player_coverage_count, source_name, source_week,
                source_as_of, player_stats_as_of, formula_version,
                evidence_json, evidence_hash, backtest_eligible, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                score_key,
                match_id,
                _canonical_json(radiant_heroes),
                _canonical_json(dire_heroes),
                _canonical_json(radiant_players),
                _canonical_json(dire_players),
                pure,
                adjusted,
                effective,
                scoring_mode,
                player_coverage_count,
                source_name,
                source_week,
                source_at.isoformat(),
                None if player_stats_at is None else player_stats_at.isoformat(),
                formula_version,
                evidence_json,
                calculated_evidence_hash,
                created.isoformat(),
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM historical_rosh_lineup_scores WHERE score_key=?",
            (score_key,),
        ).fetchone()
        parsed = None if row is None else _historical_rosh_score_from_row(row)
        if parsed is None:
            raise RuntimeError("stored historical Rosh score failed validation")
        return parsed

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
