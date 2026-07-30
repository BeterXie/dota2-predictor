"""Create the strict event-intelligence schema.

Revision ID: 20260730_0002
Revises: 20260730_0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0002"
down_revision: str | None = "20260730_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intelligence_schema_version",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("applied_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "event_registry",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("canonical_name", sa.Text(), nullable=False, unique=True),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("prize_pool_usd", sa.BigInteger(), nullable=False),
        sa.Column("main_event_start_at", sa.Text(), nullable=False),
        sa.Column("main_event_end_at", sa.Text(), nullable=False),
        sa.Column("opendota_league_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column(
            "secondary_provider_ids_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("official_evidence_urls_json", sa.Text(), nullable=False),
        sa.Column("evidence_status", sa.Text(), nullable=False),
        sa.Column("scope_policy_version", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("approval_status", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text()),
        sa.Column("approved_at", sa.Text()),
        sa.Column("reconciliation_status", sa.Text(), nullable=False),
        sa.Column("expected_map_count", sa.Integer()),
        sa.Column("observed_map_count", sa.Integer()),
        sa.Column("public_map_count", sa.Integer()),
        sa.Column("reconciliation_note", sa.Text()),
        sa.Column("included_stages_json", sa.Text(), nullable=False),
        sa.Column("excluded_categories_json", sa.Text(), nullable=False),
        sa.Column(
            "include_internal_lcq",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "excludes_qualifiers",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "excludes_division_2",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "excludes_exhibitions",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "excludes_forfeits",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "excludes_void_remakes",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("tier = 'tier_1'"),
        sa.CheckConstraint("prize_pool_usd >= 1000000"),
        sa.CheckConstraint("main_event_start_at <= main_event_end_at"),
        sa.CheckConstraint(
            "evidence_status IN ('manually_audited', 'unverified')"
        ),
        sa.CheckConstraint(
            "scope IN ('formal_main_event', 'audit_only', 'excluded')"
        ),
        sa.CheckConstraint(
            "approval_status IN ('pending', 'approved', 'rejected')"
        ),
        sa.CheckConstraint(
            "reconciliation_status IN "
            "('not_required', 'reconciliation_pending', 'reconciled', "
            "'review_required')"
        ),
        sa.CheckConstraint(
            "expected_map_count IS NULL OR expected_map_count >= 0"
        ),
        sa.CheckConstraint(
            "observed_map_count IS NULL OR observed_map_count >= 0"
        ),
        sa.CheckConstraint("public_map_count IS NULL OR public_map_count >= 0"),
        sa.CheckConstraint("include_internal_lcq IN (0, 1)"),
        sa.CheckConstraint("excludes_qualifiers IN (0, 1)"),
        sa.CheckConstraint("excludes_division_2 IN (0, 1)"),
        sa.CheckConstraint("excludes_exhibitions IN (0, 1)"),
        sa.CheckConstraint("excludes_forfeits IN (0, 1)"),
        sa.CheckConstraint("excludes_void_remakes IN (0, 1)"),
        sa.CheckConstraint(
            "approval_status != 'approved' OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)"
        ),
    )
    op.create_table(
        "event_candidates",
        sa.Column("candidate_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column(
            "evidence_urls_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "evidence_status",
            sa.Text(),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("evidence_json", sa.Text()),
        sa.Column(
            "audit_status", sa.Text(), nullable=False, server_default="pending"
        ),
        sa.Column("audit_note", sa.Text()),
        sa.Column(
            "promoted_event_id",
            sa.Text(),
            sa.ForeignKey("event_registry.event_id"),
        ),
        sa.Column("discovered_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("source", "provider_event_id"),
        sa.CheckConstraint(
            "evidence_status IN ('manually_audited', 'unverified')"
        ),
        sa.CheckConstraint(
            "audit_status IN ('pending', 'approved', 'rejected', 'promoted')"
        ),
        sa.CheckConstraint(
            "audit_status != 'promoted' OR promoted_event_id IS NOT NULL"
        ),
    )
    op.create_table(
        "raw_source_artifacts",
        sa.Column("artifact_id", sa.Text(), primary_key=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("artifact_use", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("sanitized_request_identity", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False, unique=True),
        sa.Column("uncompressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("compressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_at", sa.Text()),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("first_usable_at", sa.Text()),
        sa.Column("schema_fingerprint", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), sa.ForeignKey("event_registry.event_id")),
        sa.Column("match_id", sa.BigInteger()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("source", "content_hash"),
        sa.CheckConstraint("length(content_hash) = 64"),
        sa.CheckConstraint("source IN ('opendota', 'stratz')"),
        sa.CheckConstraint(
            "artifact_use IN ('primary', 'fallback', 'cross_check')"
        ),
        sa.CheckConstraint("uncompressed_bytes >= 0"),
        sa.CheckConstraint("compressed_bytes >= 0"),
    )
    op.create_table(
        "raw_source_artifact_relocations",
        sa.Column("relocation_id", sa.Text(), primary_key=True),
        sa.Column("relocation_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "artifact_id",
            sa.Text(),
            sa.ForeignKey("raw_source_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("old_storage_path", sa.Text(), nullable=False),
        sa.Column("new_storage_path", sa.Text(), nullable=False),
        sa.Column("uncompressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("compressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("schema_fingerprint", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("relocated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("artifact_id", "relocation_sequence"),
        sa.CheckConstraint("length(relocation_id) = 64"),
        sa.CheckConstraint("relocation_sequence > 0"),
        sa.CheckConstraint("length(content_hash) = 64"),
        sa.CheckConstraint("source IN ('opendota', 'stratz')"),
        sa.CheckConstraint("uncompressed_bytes >= 0"),
        sa.CheckConstraint("compressed_bytes >= 0"),
        sa.CheckConstraint("length(trim(reason)) > 0"),
        sa.CheckConstraint("length(trim(actor)) > 0"),
        sa.CheckConstraint("old_storage_path != new_storage_path"),
    )
    op.create_table(
        "raw_source_observations",
        sa.Column("observation_id", sa.Text(), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.Text(),
            sa.ForeignKey("raw_source_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("artifact_use", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("sanitized_request_identity", sa.Text(), nullable=False),
        sa.Column("source_at", sa.Text()),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("first_usable_at", sa.Text()),
        sa.Column("schema_fingerprint", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), sa.ForeignKey("event_registry.event_id")),
        sa.Column("match_id", sa.BigInteger()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(content_hash) = 64"),
        sa.CheckConstraint("source IN ('opendota', 'stratz')"),
        sa.CheckConstraint(
            "artifact_use IN ('primary', 'fallback', 'cross_check')"
        ),
    )
    op.create_index(
        "idx_raw_observations_match_time",
        "raw_source_observations",
        ["match_id", "received_at"],
    )
    op.create_table(
        "match_ingest_status",
        sa.Column("match_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Text(),
            sa.ForeignKey("event_registry.event_id"),
            nullable=False,
        ),
        sa.Column("start_time", sa.BigInteger()),
        sa.Column("series_id", sa.BigInteger()),
        sa.Column("map_number", sa.Integer()),
        sa.Column("stage_name", sa.Text()),
        sa.Column("stage_scope", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column(
            "stage_in_scope", sa.SmallInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "has_valid_result", sa.SmallInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "is_exhibition", sa.SmallInteger(), nullable=False, server_default="0"
        ),
        sa.Column("is_forfeit", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "is_void_remake", sa.SmallInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "ingest_state", sa.Text(), nullable=False, server_default="discovered"
        ),
        sa.Column(
            "basic_result_state", sa.Text(), nullable=False, server_default="pending"
        ),
        sa.Column(
            "detailed_parse_state",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "cross_check_state", sa.Text(), nullable=False, server_default="pending"
        ),
        sa.Column(
            "reconciliation_status",
            sa.Text(),
            nullable=False,
            server_default="not_required",
        ),
        sa.Column(
            "missing_fields_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "latest_raw_artifact_id",
            sa.Text(),
            sa.ForeignKey("raw_source_artifacts.artifact_id"),
        ),
        sa.Column("latest_raw_content_hash", sa.Text()),
        sa.Column("normalizer_version", sa.Text()),
        sa.Column(
            "raw_artifact_version", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "attempt_generation", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.Text()),
        sa.Column("last_attempt_at", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("first_usable_at", sa.Text()),
        sa.Column(
            "player_readiness", sa.Text(), nullable=False, server_default="pending"
        ),
        sa.Column(
            "state_readiness", sa.Text(), nullable=False, server_default="pending"
        ),
        sa.Column(
            "draft_readiness", sa.Text(), nullable=False, server_default="pending"
        ),
        sa.Column("discovered_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("map_number IS NULL OR map_number > 0"),
        sa.CheckConstraint(
            "stage_scope IN ('main_event', 'internal_lcq', 'qualifier', "
            "'division_2', 'exhibition', 'unknown')"
        ),
        sa.CheckConstraint("stage_in_scope IN (0, 1)"),
        sa.CheckConstraint("has_valid_result IN (0, 1)"),
        sa.CheckConstraint("is_exhibition IN (0, 1)"),
        sa.CheckConstraint("is_forfeit IN (0, 1)"),
        sa.CheckConstraint("is_void_remake IN (0, 1)"),
        sa.CheckConstraint(
            "ingest_state IN ('discovered', 'basic_result', 'detail_pending', "
            "'detailed', 'cross_checked', 'complete', 'retryable', 'failed', "
            "'review_required')"
        ),
        sa.CheckConstraint(
            "basic_result_state IN "
            "('pending', 'ready', 'retryable', 'unscorable', 'review_required')"
        ),
        sa.CheckConstraint(
            "detailed_parse_state IN "
            "('pending', 'ready', 'retryable', 'unscorable', 'review_required')"
        ),
        sa.CheckConstraint(
            "cross_check_state IN "
            "('pending', 'ready', 'retryable', 'unscorable', 'review_required')"
        ),
        sa.CheckConstraint(
            "reconciliation_status IN ('not_required', "
            "'reconciliation_pending', 'reconciled', 'review_required')"
        ),
        sa.CheckConstraint(
            "latest_raw_content_hash IS NULL OR length(latest_raw_content_hash) = 64"
        ),
        sa.CheckConstraint("raw_artifact_version >= 0"),
        sa.CheckConstraint("attempt_generation >= 0"),
        sa.CheckConstraint("retry_count >= 0"),
        sa.CheckConstraint(
            "player_readiness IN "
            "('pending', 'ready', 'retryable', 'unscorable', 'review_required')"
        ),
        sa.CheckConstraint(
            "state_readiness IN "
            "('pending', 'ready', 'retryable', 'unscorable', 'review_required')"
        ),
        sa.CheckConstraint(
            "draft_readiness IN "
            "('pending', 'ready', 'retryable', 'unscorable', 'review_required')"
        ),
    )
    op.create_index(
        "idx_match_ingest_event", "match_ingest_status", ["event_id", "match_id"]
    )
    op.create_index(
        "idx_match_ingest_retry", "match_ingest_status", ["next_retry_at"]
    )
    _create_scoring_tables()
    _create_draft_tables()
    _create_operations_tables()
    _create_views()
    _create_audit_triggers()

    op.execute(
        sa.text(
            """
            INSERT INTO intelligence_schema_version (version, applied_at)
            VALUES
                (1, replace(
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                    '_', ':'
                )),
                (10, replace(
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                    '_', ':'
                ))
            """
        )
    )


def _create_scoring_tables() -> None:
    op.create_table(
        "player_role_assignments",
        sa.Column("assignment_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("player_slot", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.BigInteger()),
        sa.Column("team_id", sa.BigInteger()),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer()),
        sa.Column("assignment_source", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("input_cutoff", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("assignment_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "match_id", "player_slot", "purpose", "assignment_version"
        ),
        sa.CheckConstraint(
            "purpose IN ('observed_position', 'expected_position')"
        ),
        sa.CheckConstraint("position IS NULL OR position BETWEEN 1 AND 5"),
        sa.CheckConstraint("confidence BETWEEN 0.0 AND 1.0"),
    )
    op.create_table(
        "player_map_facts",
        sa.Column("fact_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("player_slot", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.BigInteger()),
        sa.Column("team_id", sa.BigInteger()),
        sa.Column("hero_id", sa.BigInteger()),
        sa.Column("is_radiant", sa.SmallInteger()),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column(
            "missing_fields_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column("coverage", sa.Double(), nullable=False),
        sa.Column(
            "source_artifact_id",
            sa.Text(),
            sa.ForeignKey("raw_source_artifacts.artifact_id"),
        ),
        sa.Column("source_content_hash", sa.Text()),
        sa.Column("fact_version", sa.Text(), nullable=False),
        sa.Column("first_usable_at", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("match_id", "player_slot", "fact_version"),
        sa.CheckConstraint("is_radiant IN (0, 1)"),
        sa.CheckConstraint("coverage BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint(
            "source_content_hash IS NULL OR length(source_content_hash) = 64"
        ),
    )
    op.create_table(
        "player_map_scores",
        sa.Column("score_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("player_slot", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.BigInteger()),
        sa.Column("position", sa.Integer()),
        sa.Column("execution_score", sa.Double(), nullable=False),
        sa.Column("result_adjusted_score", sa.Double(), nullable=False),
        sa.Column("component_facts_json", sa.Text(), nullable=False),
        sa.Column("component_scores_json", sa.Text(), nullable=False),
        sa.Column("weights_json", sa.Text(), nullable=False),
        sa.Column("coverage", sa.Double(), nullable=False),
        sa.Column("role_confidence", sa.Double(), nullable=False),
        sa.Column("benchmark_cutoff", sa.Text(), nullable=False),
        sa.Column("benchmark_hash", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("score_version", sa.Text(), nullable=False),
        sa.Column("explanation_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("match_id", "player_slot", "score_version"),
        sa.CheckConstraint("position IS NULL OR position BETWEEN 1 AND 5"),
        sa.CheckConstraint("execution_score BETWEEN 0.0 AND 100.0"),
        sa.CheckConstraint("result_adjusted_score BETWEEN 0.0 AND 100.0"),
        sa.CheckConstraint("coverage BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("role_confidence BETWEEN 0.0 AND 1.0"),
    )
    op.create_table(
        "historical_rosh_lineup_scores",
        sa.Column("score_key", sa.Text(), primary_key=True),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("radiant_hero_ids_json", sa.Text(), nullable=False),
        sa.Column("dire_hero_ids_json", sa.Text(), nullable=False),
        sa.Column("radiant_player_ids_json", sa.Text(), nullable=False),
        sa.Column("dire_player_ids_json", sa.Text(), nullable=False),
        sa.Column("pure_lineup_score", sa.Double(), nullable=False),
        sa.Column("current_player_adjusted_lineup_score", sa.Double()),
        sa.Column("effective_lineup_score", sa.Double(), nullable=False),
        sa.Column("scoring_mode", sa.Text(), nullable=False),
        sa.Column("player_coverage_count", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_week", sa.Integer(), nullable=False),
        sa.Column("source_as_of", sa.Text(), nullable=False),
        sa.Column("player_stats_as_of", sa.Text()),
        sa.Column("formula_version", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
        sa.Column(
            "backtest_eligible", sa.SmallInteger(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(score_key) = 64"),
        sa.CheckConstraint(
            "jsonb_typeof(radiant_hero_ids_json::jsonb) = 'array' AND "
            "jsonb_array_length(radiant_hero_ids_json::jsonb) = 5"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dire_hero_ids_json::jsonb) = 'array' AND "
            "jsonb_array_length(dire_hero_ids_json::jsonb) = 5"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(radiant_player_ids_json::jsonb) = 'array' AND "
            "jsonb_array_length(radiant_player_ids_json::jsonb) = 5"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dire_player_ids_json::jsonb) = 'array' AND "
            "jsonb_array_length(dire_player_ids_json::jsonb) = 5"
        ),
        sa.CheckConstraint("scoring_mode IN ('pure', 'current_player_adjusted')"),
        sa.CheckConstraint("player_coverage_count BETWEEN 0 AND 10"),
        sa.CheckConstraint("source_name = 'stratz'"),
        sa.CheckConstraint("source_week > 0"),
        sa.CheckConstraint("length(trim(formula_version)) > 0"),
        sa.CheckConstraint("jsonb_typeof(evidence_json::jsonb) = 'object'"),
        sa.CheckConstraint("length(evidence_hash) = 64"),
        sa.CheckConstraint("backtest_eligible = 0"),
        sa.CheckConstraint(
            "(scoring_mode = 'current_player_adjusted' "
            "AND player_coverage_count = 10 "
            "AND current_player_adjusted_lineup_score IS NOT NULL "
            "AND effective_lineup_score = current_player_adjusted_lineup_score "
            "AND player_stats_as_of IS NOT NULL) OR "
            "(scoring_mode = 'pure' "
            "AND player_coverage_count < 10 "
            "AND current_player_adjusted_lineup_score IS NULL "
            "AND effective_lineup_score = pure_lineup_score)"
        ),
    )
    op.create_index(
        "idx_historical_rosh_match_version",
        "historical_rosh_lineup_scores",
        ["match_id", "formula_version", sa.text("created_at DESC"), sa.text("score_key DESC")],
    )
    op.create_table(
        "team_map_states",
        sa.Column("state_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("team_id", sa.BigInteger()),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("max_lead", sa.Double()),
        sa.Column("max_deficit", sa.Double()),
        sa.Column("ahead_fraction", sa.Double()),
        sa.Column("behind_fraction", sa.Double()),
        sa.Column("even_fraction", sa.Double()),
        sa.Column("signed_auc", sa.Double()),
        sa.Column("absolute_auc", sa.Double()),
        sa.Column("crossings_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("first_significant_lead_at", sa.Integer()),
        sa.Column("first_significant_deficit_at", sa.Integer()),
        sa.Column("closeout_seconds", sa.Integer()),
        sa.Column(
            "objective_conversion_json", sa.Text(), nullable=False, server_default="{}"
        ),
        sa.Column("curve_coverage", sa.Double(), nullable=False),
        sa.Column("source_versions_json", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("label_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("match_id", "side", "label_version"),
        sa.CheckConstraint("side IN ('radiant', 'dire')"),
        sa.CheckConstraint(
            "label IN ('comeback', 'throw', 'stomp', 'stomp_loss', 'advantage', "
            "'disadvantage', 'even', 'state_unscorable')"
        ),
        sa.CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0"),
        sa.CheckConstraint(
            "ahead_fraction IS NULL OR ahead_fraction BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "behind_fraction IS NULL OR behind_fraction BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "even_fraction IS NULL OR even_fraction BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint("curve_coverage BETWEEN 0.0 AND 1.0"),
    )
    op.create_table(
        "team_style_profiles",
        sa.Column("profile_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_cutoff", sa.Text(), nullable=False),
        sa.Column("profile_version", sa.Text(), nullable=False),
        sa.Column("opportunity_counts_json", sa.Text(), nullable=False),
        sa.Column("posterior_rates_json", sa.Text(), nullable=False),
        sa.Column("duration_quantiles_json", sa.Text(), nullable=False),
        sa.Column("weighting_json", sa.Text(), nullable=False),
        sa.Column("effective_sample_size", sa.Double(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("team_id", "profile_cutoff", "profile_version"),
        sa.CheckConstraint("effective_sample_size >= 0.0"),
    )


def _create_draft_tables() -> None:
    op.create_table(
        "draft_model_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("model_kind", sa.Text(), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("availability_mode", sa.Text(), nullable=False),
        sa.Column("training_cutoff", sa.Text(), nullable=False),
        sa.Column("feature_schema_hash", sa.Text(), nullable=False),
        sa.Column("configuration_json", sa.Text(), nullable=False),
        sa.Column("metrics_json", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("model_kind IN ('pure_draft', 'context_adjusted')"),
        sa.CheckConstraint("horizon_minutes IN (10, 20, 30, 40, 50)"),
        sa.CheckConstraint(
            "availability_mode IN ('reconstructed_walk_forward', 'prospective')"
        ),
    )
    op.create_table(
        "draft_predictions",
        sa.Column("prediction_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("draft_model_runs.run_id"),
            nullable=False,
        ),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
        ),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column("cutoff_source", sa.Text(), nullable=False),
        sa.Column("input_snapshot_hash", sa.Text(), nullable=False),
        sa.Column("probability", sa.Double()),
        sa.Column("uncertainty", sa.Double()),
        sa.Column("support", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eventual_radiant_win", sa.SmallInteger()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("run_id", "match_id"),
        sa.CheckConstraint("probability IS NULL OR probability BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("uncertainty IS NULL OR uncertainty >= 0.0"),
        sa.CheckConstraint("support >= 0"),
        sa.CheckConstraint(
            "eventual_radiant_win IS NULL OR eventual_radiant_win IN (0, 1)"
        ),
        sa.CheckConstraint(
            "status IN ('predicted', 'insufficient_evidence', 'settled')"
        ),
    )
    op.create_index(
        "idx_draft_predictions_match",
        "draft_predictions",
        ["match_id", "run_id"],
    )
    op.create_table(
        "draft_lineage_revisions",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("dependency_revision", sa.BigInteger(), nullable=False),
        sa.Column("artifact_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton = 1"),
        sa.CheckConstraint("dependency_revision >= 1"),
        sa.CheckConstraint("artifact_revision >= 1"),
    )
    op.create_table(
        "draft_lineage_changes",
        sa.Column("dependency_revision", sa.BigInteger(), primary_key=True),
        sa.Column("affected_from_unix", sa.BigInteger()),
        sa.Column("source_relation", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.Text(), nullable=False),
        sa.CheckConstraint("dependency_revision >= 1"),
        sa.CheckConstraint(
            "affected_from_unix IS NULL OR affected_from_unix > 0"
        ),
        sa.CheckConstraint(
            "operation IN ('INSERT', 'UPDATE', 'DELETE', 'REPAIR', 'INITIALIZE')"
        ),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO draft_lineage_revisions (
                singleton, dependency_revision, artifact_revision, updated_at
            ) VALUES (
                1, 1, 1,
                replace(
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                    '_', ':'
                )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO draft_lineage_changes (
                dependency_revision, affected_from_unix,
                source_relation, operation, changed_at
            )
            SELECT 1, NULL, '__tracking__', 'INITIALIZE', updated_at
            FROM draft_lineage_revisions
            WHERE singleton = 1
            """
        )
    )
    op.create_table(
        "draft_prediction_validations",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=False),
        sa.Column("input_snapshot_hash", sa.Text(), nullable=False),
        sa.Column("artifact_fingerprint", sa.Text(), nullable=False),
        sa.Column("dependency_fingerprint", sa.Text(), nullable=False),
        sa.Column("dependency_revision", sa.BigInteger(), nullable=False),
        sa.Column("validation_version", sa.Text(), nullable=False),
        sa.Column("validated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "match_id"),
        sa.ForeignKeyConstraint(
            ["run_id", "match_id"],
            ["draft_predictions.run_id", "draft_predictions.match_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("length(input_snapshot_hash) = 64"),
        sa.CheckConstraint("length(artifact_fingerprint) = 64"),
        sa.CheckConstraint("length(dependency_fingerprint) = 64"),
        sa.CheckConstraint("dependency_revision >= 1"),
    )
    op.create_index(
        "idx_draft_prediction_validations_fingerprint",
        "draft_prediction_validations",
        ["validation_version", "dependency_fingerprint"],
    )


def _create_operations_tables() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("outbox_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("order_key", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default="email"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("statistics_cutoff", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("lease_token", sa.Text()),
        sa.Column("lease_until", sa.Text()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("sent_at", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("order_key", "event_type", "channel"),
        sa.CheckConstraint("event_type IN ('filled', 'settled')"),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'sent', 'dead_letter')"
        ),
        sa.CheckConstraint("attempt_count >= 0"),
    )
    op.create_index(
        "idx_notification_due",
        "notification_outbox",
        ["status", "next_attempt_at", "lease_until"],
    )
    op.create_table(
        "service_health",
        sa.Column("component", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("last_heartbeat_at", sa.Text()),
        sa.Column("last_success_at", sa.Text()),
        sa.Column("last_error_at", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('starting', 'healthy', 'degraded', 'unhealthy', 'stopped')"
        ),
    )
    op.create_table(
        "ingest_scheduler_checkpoints",
        sa.Column("checkpoint_key", sa.Text(), primary_key=True),
        sa.Column("checkpoint_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "ingest_scheduler_retry_state",
        sa.Column("checkpoint_key", sa.Text(), primary_key=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("failure_count > 0"),
    )
    op.create_table(
        "strict_derived_status",
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_content_hash", sa.Text(), nullable=False),
        sa.Column("role_assignment_version", sa.Text(), nullable=False),
        sa.Column("score_version", sa.Text(), nullable=False),
        sa.Column("team_state_version", sa.Text(), nullable=False),
        sa.Column("profile_version", sa.Text(), nullable=False),
        sa.Column("profile_cutoff", sa.Text(), nullable=False),
        sa.Column("derived_at", sa.Text(), nullable=False),
        sa.Column("normalizer_version", sa.Text(), nullable=False),
        sa.Column("benchmark_version", sa.Text(), nullable=False),
        sa.Column("profile_context_hash", sa.Text(), nullable=False),
        sa.CheckConstraint("length(source_content_hash) = 64"),
        sa.CheckConstraint("length(profile_context_hash) = 64"),
    )


def _create_views() -> None:
    op.execute(
        sa.text(
            """
            CREATE VIEW formal_events AS
            SELECT *
            FROM event_registry
            WHERE scope = 'formal_main_event'
              AND approval_status = 'approved'
              AND evidence_status = 'manually_audited'
              AND tier = 'tier_1'
              AND prize_pool_usd >= 1000000
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE VIEW formal_map_eligibility AS
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
                  OR (
                      m.stage_scope = 'internal_lcq'
                      AND e.include_internal_lcq = 1
                  )
              )
            """
        )
    )


def _create_audit_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_raw_source_artifact_relocation()
            RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM raw_source_artifacts AS artifact
                    WHERE artifact.artifact_id = NEW.artifact_id
                      AND artifact.content_hash = NEW.content_hash
                      AND artifact.source = NEW.source
                      AND artifact.storage_path = NEW.old_storage_path
                      AND artifact.uncompressed_bytes = NEW.uncompressed_bytes
                      AND artifact.compressed_bytes = NEW.compressed_bytes
                      AND artifact.schema_fingerprint = NEW.schema_fingerprint
                ) OR NEW.relocation_sequence != COALESCE(
                    (
                        SELECT MAX(existing.relocation_sequence) + 1
                        FROM raw_source_artifact_relocations AS existing
                        WHERE existing.artifact_id = NEW.artifact_id
                    ),
                    1
                ) THEN
                    RAISE EXCEPTION 'raw source relocation authority mismatch';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER raw_source_artifact_relocations_guard_insert
            BEFORE INSERT ON raw_source_artifact_relocations
            FOR EACH ROW EXECUTE FUNCTION guard_raw_source_artifact_relocation()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_raw_source_artifact_relocation_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'raw source relocation audit is immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER raw_source_artifact_relocations_immutable_{operation.lower()}
                BEFORE {operation} ON raw_source_artifact_relocations
                FOR EACH ROW
                EXECUTE FUNCTION reject_raw_source_artifact_relocation_mutation()
                """
            )
        )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_raw_source_artifact_identity()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
                    OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                    OR NEW.source IS DISTINCT FROM OLD.source
                    OR NEW.artifact_use IS DISTINCT FROM OLD.artifact_use
                    OR NEW.endpoint IS DISTINCT FROM OLD.endpoint
                    OR NEW.sanitized_request_identity IS DISTINCT FROM
                       OLD.sanitized_request_identity
                    OR NEW.uncompressed_bytes IS DISTINCT FROM OLD.uncompressed_bytes
                    OR NEW.compressed_bytes IS DISTINCT FROM OLD.compressed_bytes
                    OR NEW.source_at IS DISTINCT FROM OLD.source_at
                    OR NEW.received_at IS DISTINCT FROM OLD.received_at
                    OR NEW.schema_fingerprint IS DISTINCT FROM OLD.schema_fingerprint
                    OR NEW.event_id IS DISTINCT FROM OLD.event_id
                    OR NEW.match_id IS DISTINCT FROM OLD.match_id
                    OR NEW.created_at IS DISTINCT FROM OLD.created_at
                THEN
                    RAISE EXCEPTION 'raw source artifact identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER raw_source_artifacts_identity_immutable
            BEFORE UPDATE ON raw_source_artifacts
            FOR EACH ROW EXECUTE FUNCTION guard_raw_source_artifact_identity()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION require_raw_source_artifact_relocation()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.storage_path IS DISTINCT FROM OLD.storage_path
                   AND NOT EXISTS (
                       SELECT 1
                       FROM raw_source_artifact_relocations AS relocation
                       WHERE relocation.artifact_id = OLD.artifact_id
                         AND relocation.content_hash = OLD.content_hash
                         AND relocation.source = OLD.source
                         AND relocation.old_storage_path = OLD.storage_path
                         AND relocation.new_storage_path = NEW.storage_path
                         AND relocation.uncompressed_bytes = OLD.uncompressed_bytes
                         AND relocation.compressed_bytes = OLD.compressed_bytes
                         AND relocation.schema_fingerprint = OLD.schema_fingerprint
                         AND relocation.relocation_sequence = (
                             SELECT MAX(latest.relocation_sequence)
                             FROM raw_source_artifact_relocations AS latest
                             WHERE latest.artifact_id = OLD.artifact_id
                         )
                   ) THEN
                    RAISE EXCEPTION 'raw source artifact relocation audit is required';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER raw_source_artifacts_relocation_required
            BEFORE UPDATE OF storage_path ON raw_source_artifacts
            FOR EACH ROW EXECUTE FUNCTION require_raw_source_artifact_relocation()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_historical_rosh_lineup_score_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'historical Rosh lineup score is immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER historical_rosh_lineup_scores_immutable_{operation.lower()}
                BEFORE {operation} ON historical_rosh_lineup_scores
                FOR EACH ROW
                EXECUTE FUNCTION reject_historical_rosh_lineup_score_mutation()
                """
            )
        )


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW formal_map_eligibility"))
    op.execute(sa.text("DROP VIEW formal_events"))

    op.drop_table("strict_derived_status")
    op.drop_table("ingest_scheduler_retry_state")
    op.drop_table("ingest_scheduler_checkpoints")
    op.drop_table("service_health")
    op.drop_table("notification_outbox")
    op.drop_table("draft_prediction_validations")
    op.drop_table("draft_lineage_changes")
    op.drop_table("draft_lineage_revisions")
    op.drop_table("draft_predictions")
    op.drop_table("draft_model_runs")
    op.drop_table("team_style_profiles")
    op.drop_table("team_map_states")
    op.drop_table("historical_rosh_lineup_scores")
    op.drop_table("player_map_scores")
    op.drop_table("player_map_facts")
    op.drop_table("player_role_assignments")
    op.drop_table("match_ingest_status")
    op.drop_table("raw_source_observations")
    op.drop_table("raw_source_artifact_relocations")
    op.drop_table("raw_source_artifacts")
    op.drop_table("event_candidates")
    op.drop_table("event_registry")
    op.drop_table("intelligence_schema_version")

    op.execute(sa.text("DROP FUNCTION reject_historical_rosh_lineup_score_mutation()"))
    op.execute(sa.text("DROP FUNCTION require_raw_source_artifact_relocation()"))
    op.execute(sa.text("DROP FUNCTION guard_raw_source_artifact_identity()"))
    op.execute(sa.text("DROP FUNCTION reject_raw_source_artifact_relocation_mutation()"))
    op.execute(sa.text("DROP FUNCTION guard_raw_source_artifact_relocation()"))
