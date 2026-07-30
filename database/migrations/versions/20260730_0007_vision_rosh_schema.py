"""Create vision evidence and Rosh analysis authority tables.

Revision ID: 20260730_0007
Revises: 20260730_0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0007"
down_revision: str | None = "20260730_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collector_runs",
        sa.Column("collector", sa.Text(), primary_key=True),
        sa.Column("last_success_at", sa.Text()),
        sa.Column("last_error_at", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("cursor", sa.Text()),
        sa.Column("gap_detected", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    _create_vision_frame_tables()
    _create_vision_observation_tables()
    _create_rosh_lineup_table()
    _create_rosh_analysis_tables()
    _create_official_rosh_shadow_table()
    _create_derived_tables()
    _create_vision_views()
    _create_vision_rosh_triggers()


def _create_vision_frame_tables() -> None:
    op.create_table(
        "vision_frame_artifacts",
        sa.Column("frame_ref", sa.Text(), primary_key=True),
        sa.Column("content_sha256", sa.Text(), nullable=False, unique=True),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False, unique=True),
        sa.Column("registered_at", sa.Text(), nullable=False),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint(
            "frame_ref = 'vision-frame:sha256:' || content_sha256"
        ),
        sa.CheckConstraint("byte_length > 0"),
        sa.CheckConstraint("storage_path != ''"),
        sa.CheckConstraint("live_text_timestamp_utc(registered_at) IS NOT NULL"),
    )
    op.create_table(
        "vision_frame_artifact_relocations",
        sa.Column("relocation_id", sa.Text(), primary_key=True),
        sa.Column("relocation_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "frame_ref",
            sa.Text(),
            sa.ForeignKey("vision_frame_artifacts.frame_ref"),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("old_storage_path", sa.Text(), nullable=False),
        sa.Column("new_storage_path", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("relocated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("frame_ref", "relocation_sequence"),
        sa.CheckConstraint("length(relocation_id) = 64"),
        sa.CheckConstraint("relocation_sequence > 0"),
        sa.CheckConstraint("length(content_sha256) = 64"),
        sa.CheckConstraint("byte_length > 0"),
        sa.CheckConstraint("old_storage_path != ''"),
        sa.CheckConstraint("new_storage_path != ''"),
        sa.CheckConstraint("reason != ''"),
        sa.CheckConstraint("actor != ''"),
        sa.CheckConstraint("live_text_timestamp_utc(relocated_at) IS NOT NULL"),
        sa.CheckConstraint("old_storage_path != new_storage_path"),
    )
    op.create_table(
        "vision_frame_artifact_retirements",
        sa.Column("retirement_id", sa.Text(), primary_key=True),
        sa.Column(
            "frame_ref",
            sa.Text(),
            sa.ForeignKey("vision_frame_artifacts.frame_ref"),
            nullable=False,
            unique=True,
        ),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("retired_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(retirement_id) = 64"),
        sa.CheckConstraint("length(content_sha256) = 64"),
        sa.CheckConstraint("byte_length > 0"),
        sa.CheckConstraint("storage_path != ''"),
        sa.CheckConstraint("reason != ''"),
        sa.CheckConstraint("actor != ''"),
        sa.CheckConstraint("live_text_timestamp_utc(retired_at) IS NOT NULL"),
    )


def _create_vision_observation_tables() -> None:
    op.create_table(
        "vision_observations",
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer()),
        sa.Column("captured_at", sa.Text(), nullable=False),
        sa.Column("game_clock_seconds", sa.Integer()),
        sa.Column("is_paused", sa.SmallInteger()),
        sa.Column("radiant_hero_ids", sa.Text(), nullable=False),
        sa.Column("dire_hero_ids", sa.Text(), nullable=False),
        sa.Column("radiant_team_side", sa.Text()),
        sa.Column("clock_confidence", sa.Double(), nullable=False),
        sa.Column("draft_confidence", sa.Double(), nullable=False),
        sa.Column("source_frame_ref", sa.Text(), nullable=False),
        sa.Column("source_frame_sha256", sa.Text()),
        sa.Column("source_frame_bytes", sa.BigInteger()),
        sa.Column("screen_state", sa.Text(), nullable=False),
        sa.Column("confirmed", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "raybet_match_id", "captured_at", "source_frame_ref"
        ),
        sa.CheckConstraint(
            "source_frame_sha256 IS NULL OR length(source_frame_sha256) = 64"
        ),
        sa.CheckConstraint(
            "source_frame_bytes IS NULL OR source_frame_bytes > 0"
        ),
    )
    op.create_index(
        "idx_vision_match_map_time",
        "vision_observations",
        ["raybet_match_id", "map_number", "captured_at"],
    )
    op.create_index(
        "idx_vision_confirmed_game_captured",
        "vision_observations",
        [sa.text("captured_at DESC"), sa.text("raybet_match_id DESC")],
        postgresql_where=sa.text("confirmed = 1 AND screen_state = 'game'"),
    )
    op.create_table(
        "vision_observation_invalidations",
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.Text(), nullable=False),
        sa.Column("source_frame_ref", sa.Text(), nullable=False),
        sa.Column("invalidated_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "raybet_match_id", "captured_at", "source_frame_ref"
        ),
        sa.ForeignKeyConstraint(
            ["raybet_match_id", "captured_at", "source_frame_ref"],
            [
                "vision_observations.raybet_match_id",
                "vision_observations.captured_at",
                "vision_observations.source_frame_ref",
            ],
        ),
    )
    op.create_table(
        "vision_draft_anchors",
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("draft_hash", sa.Text(), nullable=False),
        sa.Column("radiant_hero_ids", sa.Text(), nullable=False),
        sa.Column("dire_hero_ids", sa.Text(), nullable=False),
        sa.Column("radiant_team_side", sa.Text()),
        sa.Column("team_side_anchored_at", sa.Text()),
        sa.Column("team_side_source_frame_ref", sa.Text()),
        sa.Column("anchored_at", sa.Text(), nullable=False),
        sa.Column("source_frame_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("conflict_at", sa.Text()),
        sa.PrimaryKeyConstraint("raybet_match_id", "map_number"),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint("length(draft_hash) = 64"),
        sa.CheckConstraint(
            "radiant_team_side IN ('team_one', 'team_two')"
        ),
        sa.CheckConstraint("status IN ('anchored', 'conflict')"),
    )
    op.create_table(
        "vision_draft_conflicts",
        sa.Column("conflict_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.Text(), nullable=False),
        sa.Column("source_frame_ref", sa.Text(), nullable=False),
        sa.Column("observed_draft_hash", sa.Text(), nullable=False),
        sa.Column("radiant_hero_ids", sa.Text(), nullable=False),
        sa.Column("dire_hero_ids", sa.Text(), nullable=False),
        sa.Column("observed_radiant_team_side", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "raybet_match_id", "map_number", "captured_at", "source_frame_ref"
        ),
        sa.ForeignKeyConstraint(
            ["raybet_match_id", "map_number"],
            ["vision_draft_anchors.raybet_match_id", "vision_draft_anchors.map_number"],
        ),
        sa.CheckConstraint("length(observed_draft_hash) = 64"),
        sa.CheckConstraint(
            "observed_radiant_team_side IN ('team_one', 'team_two')"
        ),
    )


def _create_rosh_lineup_table() -> None:
    op.create_table(
        "rosh_lineup_scores",
        sa.Column("score_key", sa.Text(), primary_key=True),
        sa.Column("draft_hash", sa.Text(), nullable=False),
        sa.Column("player_identity_hash", sa.Text(), nullable=False),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column(
            "strict_mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
            nullable=False,
        ),
        sa.Column("radiant_hero_ids_json", sa.Text(), nullable=False),
        sa.Column("dire_hero_ids_json", sa.Text(), nullable=False),
        sa.Column("pure_lineup_score", sa.Double(), nullable=False),
        sa.Column("player_adjusted_lineup_score", sa.Double()),
        sa.Column("effective_lineup_score", sa.Double(), nullable=False),
        sa.Column("scoring_mode", sa.Text(), nullable=False),
        sa.Column("player_coverage_count", sa.Integer(), nullable=False),
        sa.Column("stake_multiplier", sa.Double(), nullable=False),
        sa.Column("formula_version", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_week", sa.Integer(), nullable=False),
        sa.Column("cache_week_start", sa.BigInteger(), nullable=False),
        sa.Column("source_as_of", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(score_key) = 64"),
        sa.CheckConstraint("length(draft_hash) = 64"),
        sa.CheckConstraint("length(player_identity_hash) = 64"),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint("strict_mapping_id > 0"),
        sa.CheckConstraint(
            "jsonb_typeof(radiant_hero_ids_json::jsonb) = 'array' AND "
            "jsonb_array_length(radiant_hero_ids_json::jsonb) = 5"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dire_hero_ids_json::jsonb) = 'array' AND "
            "jsonb_array_length(dire_hero_ids_json::jsonb) = 5"
        ),
        sa.CheckConstraint("scoring_mode IN ('pure', 'player_adjusted')"),
        sa.CheckConstraint("player_coverage_count BETWEEN 0 AND 10"),
        sa.CheckConstraint("stake_multiplier IN (0.5, 1.0)"),
        sa.CheckConstraint("length(trim(formula_version)) > 0"),
        sa.CheckConstraint("source_name = 'stratz'"),
        sa.CheckConstraint("source_week > 0"),
        sa.CheckConstraint("cache_week_start > 0"),
        sa.CheckConstraint("jsonb_typeof(evidence_json::jsonb) = 'object'"),
        sa.CheckConstraint("length(evidence_hash) = 64"),
        sa.CheckConstraint(
            "(scoring_mode = 'player_adjusted' "
            "AND player_coverage_count = 10 "
            "AND player_adjusted_lineup_score IS NOT NULL "
            "AND effective_lineup_score = player_adjusted_lineup_score "
            "AND stake_multiplier = 1.0) OR "
            "(scoring_mode = 'pure' "
            "AND player_coverage_count < 10 "
            "AND player_adjusted_lineup_score IS NULL "
            "AND effective_lineup_score = pure_lineup_score "
            "AND stake_multiplier = 0.5)"
        ),
    )
    op.create_index(
        "idx_rosh_lineup_scores_cache",
        "rosh_lineup_scores",
        [
            "draft_hash",
            "player_identity_hash",
            "formula_version",
            "cache_week_start",
            sa.text("created_at DESC"),
            sa.text("score_key DESC"),
        ],
    )
    op.create_index(
        "idx_rosh_lineup_scores_map",
        "rosh_lineup_scores",
        [
            "raybet_match_id",
            "map_number",
            "strict_mapping_id",
            sa.text("created_at DESC"),
            sa.text("score_key DESC"),
        ],
    )


def _create_rosh_analysis_tables() -> None:
    op.create_table(
        "rosh_analysis_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("match_id", sa.BigInteger()),
        sa.Column("date_time", sa.BigInteger(), nullable=False),
        sa.Column("draft_hash", sa.Text(), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("rosh_profile_id", sa.Text(), nullable=False),
        sa.Column("formula_version", sa.Text(), nullable=False),
        sa.Column("request_profile_hash", sa.Text(), nullable=False),
        sa.Column("upstream_bundle_hash", sa.Text(), nullable=False),
        sa.Column("scorer_source_hash", sa.Text(), nullable=False),
        sa.Column("canonical_profile_hash", sa.Text(), nullable=False),
        sa.Column("serialization_version", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("request_manifest_json", sa.Text(), nullable=False),
        sa.Column("response_manifest_json", sa.Text(), nullable=False),
        sa.Column("radiant_team_score", sa.Double()),
        sa.Column("dire_team_score", sa.Double()),
        sa.Column("relative_advantage", sa.Double()),
        sa.Column("result_json", sa.Text()),
        sa.Column("evidence_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("error_code", sa.Text()),
        sa.Column("collected_at", sa.Text(), nullable=False),
        sa.CheckConstraint("run_id ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("status IN ('succeeded', 'failed')"),
        sa.CheckConstraint("mode IN ('historical_match', 'explicit_draft')"),
        sa.CheckConstraint("date_time >= 0"),
        sa.CheckConstraint("draft_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("jsonb_typeof(draft_json::jsonb) = 'object'"),
        sa.CheckConstraint("length(trim(rosh_profile_id)) > 0"),
        sa.CheckConstraint("length(trim(formula_version)) > 0"),
        sa.CheckConstraint("request_profile_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("upstream_bundle_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("scorer_source_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("canonical_profile_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("length(trim(serialization_version)) > 0"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("jsonb_typeof(request_manifest_json::jsonb) = 'object'"),
        sa.CheckConstraint("jsonb_typeof(response_manifest_json::jsonb) = 'array'"),
        sa.CheckConstraint(
            "result_json IS NULL OR jsonb_typeof(result_json::jsonb) = 'object'"
        ),
        sa.CheckConstraint("evidence_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("length(trim(collected_at)) > 0"),
        sa.CheckConstraint(
            "(mode = 'historical_match' AND match_id IS NOT NULL AND match_id > 0) "
            "OR (mode = 'explicit_draft' AND match_id IS NULL)"
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' "
            "AND jsonb_array_length(response_manifest_json::jsonb) > 0 "
            "AND radiant_team_score BETWEEN -1.0e308 AND 1.0e308 "
            "AND dire_team_score BETWEEN -1.0e308 AND 1.0e308 "
            "AND relative_advantage BETWEEN -1.0e308 AND 1.0e308 "
            "AND result_json IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND radiant_team_score IS NULL "
            "AND dire_team_score IS NULL AND relative_advantage IS NULL "
            "AND result_json IS NULL AND error_code IS NOT NULL "
            "AND length(trim(error_code)) > 0)"
        ),
    )
    op.create_index(
        "idx_rosh_runs_match_profile",
        "rosh_analysis_runs",
        ["match_id", "rosh_profile_id", "date_time"],
    )
    op.create_index(
        "idx_rosh_runs_draft_profile",
        "rosh_analysis_runs",
        ["draft_hash", "rosh_profile_id", "date_time"],
    )
    op.create_table(
        "rosh_hero_scores",
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("rosh_analysis_runs.run_id"),
            nullable=False,
        ),
        sa.Column("team_side", sa.Text(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("hero_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_score", sa.Double(), nullable=False),
        sa.Column("display_score", sa.Double(), nullable=False),
        sa.Column("components_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "team_side", "position_id"),
        sa.UniqueConstraint("run_id", "hero_id"),
        sa.CheckConstraint("team_side IN ('RADIANT', 'DIRE')"),
        sa.CheckConstraint("position_id BETWEEN 1 AND 5"),
        sa.CheckConstraint("hero_id > 0"),
        sa.CheckConstraint("raw_score BETWEEN -1.0e308 AND 1.0e308"),
        sa.CheckConstraint("display_score BETWEEN -1.0e308 AND 1.0e308"),
        sa.CheckConstraint("jsonb_typeof(components_json::jsonb) = 'object'"),
    )
    op.create_table(
        "rosh_minute_points",
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("rosh_analysis_runs.run_id"),
            nullable=False,
        ),
        sa.Column("minute", sa.Integer(), nullable=False),
        sa.Column("raw_score", sa.Double(), nullable=False),
        sa.Column("display_score", sa.Double(), nullable=False),
        sa.Column("radiant_time_delta", sa.Double(), nullable=False),
        sa.Column("dire_time_delta", sa.Double(), nullable=False),
        sa.Column("synergy_delta", sa.Double(), nullable=False),
        sa.Column("source_audit_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "minute"),
        sa.CheckConstraint("minute >= 0"),
        sa.CheckConstraint("raw_score BETWEEN -1.0e308 AND 1.0e308"),
        sa.CheckConstraint("display_score BETWEEN -1.0e308 AND 1.0e308"),
        sa.CheckConstraint("radiant_time_delta BETWEEN -1.0e308 AND 1.0e308"),
        sa.CheckConstraint("dire_time_delta BETWEEN -1.0e308 AND 1.0e308"),
        sa.CheckConstraint("synergy_delta BETWEEN -1.0e308 AND 1.0e308"),
        sa.CheckConstraint("jsonb_typeof(source_audit_json::jsonb) = 'object'"),
    )


def _create_official_rosh_shadow_table() -> None:
    op.create_table(
        "official_rosh_shadow_evaluations",
        sa.Column("evaluation_key", sa.Text(), primary_key=True),
        sa.Column("candidate_hash", sa.Text(), nullable=False),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("transport_key", sa.Text(), nullable=False),
        sa.Column("observation_draft_hash", sa.Text(), nullable=False),
        sa.Column(
            "source_run_id",
            sa.Text(),
            sa.ForeignKey("rosh_analysis_runs.run_id"),
        ),
        sa.Column("strategy_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.CheckConstraint("evaluation_key ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("candidate_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("length(trim(raybet_match_id)) > 0"),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint("length(trim(decided_at)) > 0"),
        sa.CheckConstraint("length(trim(transport_key)) > 0"),
        sa.CheckConstraint("observation_draft_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint(
            "strategy_version = 'comeback-shadow-v6-official-rosh-direction'"
        ),
        sa.CheckConstraint("status IN ('shadow_candidate', 'rejected')"),
        sa.CheckConstraint("length(trim(reason)) > 0"),
        sa.CheckConstraint(
            "jsonb_typeof(record_json::jsonb) = 'object' "
            "AND record_json::jsonb ->> 'schema' = "
            "'official-rosh-shadow-candidate/v1' "
            "AND record_json::jsonb ->> 'candidate_hash' = candidate_hash "
            "AND record_json::jsonb ->> 'strategy_version' = strategy_version "
            "AND record_json::jsonb ->> 'status' = status "
            "AND record_json::jsonb ->> 'reason' = reason "
            "AND jsonb_typeof(record_json::jsonb -> 'calibration_artifact_ref') "
            "= 'null' "
            "AND jsonb_typeof(record_json::jsonb -> 'calibrated_probability') "
            "= 'null' "
            "AND jsonb_typeof(record_json::jsonb -> 'edge') = 'null' "
            "AND jsonb_typeof(record_json::jsonb -> 'stake_multiplier') = 'null' "
            "AND jsonb_typeof(record_json::jsonb -> 'paper_order') = 'null' "
            "AND record_json::jsonb #>> '{cohort,m3_c}' = "
            "'shadow_candidate_or_rejection' "
            "AND jsonb_typeof(record_json::jsonb #> '{cohort,m3_e}') = 'null' "
            "AND ((jsonb_typeof(record_json::jsonb -> "
            "'rosh_direction_evidence') = 'null' AND status = 'rejected') "
            "OR (jsonb_typeof(record_json::jsonb -> "
            "'rosh_direction_evidence') = 'object' "
            "AND record_json::jsonb #>> "
            "'{rosh_direction_evidence,analysis_run_id}' = source_run_id "
            "AND record_json::jsonb #>> "
            "'{rosh_direction_evidence,draft_hash}' = observation_draft_hash))"
        ),
    )
    op.create_index(
        "idx_official_rosh_shadow_match_time",
        "official_rosh_shadow_evaluations",
        ["raybet_match_id", "map_number", "decided_at", "strategy_version"],
    )
    op.create_index(
        "idx_official_rosh_shadow_draft",
        "official_rosh_shadow_evaluations",
        ["observation_draft_hash", "decided_at", "strategy_version"],
    )


def _create_derived_tables() -> None:
    op.create_table(
        "vision_derived_invalidations",
        sa.Column("dependent_type", sa.Text(), nullable=False),
        sa.Column("dependent_key", sa.Text(), nullable=False),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "block_reason",
            sa.Text(),
            nullable=False,
            server_default="vision_draft_conflict",
        ),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("dependent_type", "dependent_key"),
        sa.CheckConstraint(
            "dependent_type IN ('odds_alignment', 'strategy_decision', "
            "'research_prediction', 'shadow_order')"
        ),
    )
    op.create_table(
        "odds_alignments",
        sa.Column("odds_snapshot_id", sa.BigInteger(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer()),
        sa.Column("game_clock_seconds", sa.Integer()),
        sa.Column("observation_captured_at", sa.Text()),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("lag_seconds", sa.Double()),
        sa.Column("usable", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text()),
    )
    op.create_index(
        "idx_alignment_match_map_time",
        "odds_alignments",
        ["raybet_match_id", "map_number", "game_clock_seconds"],
    )


def _create_vision_views() -> None:
    op.execute(
        sa.text(
            """
            CREATE VIEW active_vision_frame_artifacts AS
            SELECT artifact.frame_ref,
                   artifact.content_sha256,
                   artifact.byte_length,
                   COALESCE(
                       (
                           SELECT relocation.new_storage_path
                           FROM vision_frame_artifact_relocations AS relocation
                           WHERE relocation.frame_ref = artifact.frame_ref
                           ORDER BY relocation.relocation_sequence DESC
                           LIMIT 1
                       ),
                       artifact.storage_path
                   ) AS storage_path,
                   artifact.registered_at
            FROM vision_frame_artifacts AS artifact
            WHERE NOT EXISTS (
                SELECT 1
                FROM vision_frame_artifact_retirements AS retirement
                WHERE retirement.frame_ref = artifact.frame_ref
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE VIEW trusted_vision_observation_authority AS
            SELECT observation.*
            FROM vision_observations AS observation
            JOIN active_vision_frame_artifacts AS frame
              ON frame.frame_ref = observation.source_frame_ref
             AND frame.content_sha256 = observation.source_frame_sha256
             AND frame.byte_length = observation.source_frame_bytes
            WHERE observation.confirmed = 1
              AND live_text_timestamp_utc(observation.captured_at) IS NOT NULL
              AND observation.map_number IS NOT NULL
              AND observation.map_number > 0
              AND observation.game_clock_seconds >= 0
              AND observation.is_paused = 0
              AND observation.screen_state = 'game'
              AND observation.source_frame_ref != ''
              AND observation.radiant_team_side IN ('team_one', 'team_two')
              AND observation.clock_confidence BETWEEN 0.9 AND 1.0
              AND observation.draft_confidence BETWEEN 0.9 AND 1.0
              AND jsonb_typeof(observation.radiant_hero_ids::jsonb) = 'array'
              AND jsonb_typeof(observation.dire_hero_ids::jsonb) = 'array'
              AND jsonb_array_length(observation.radiant_hero_ids::jsonb) = 5
              AND jsonb_array_length(observation.dire_hero_ids::jsonb) = 5
              AND NOT EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements_text(
                      observation.radiant_hero_ids::jsonb
                  ) AS hero(value)
                  WHERE hero.value !~ '^[0-9]+$' OR hero.value::bigint <= 0
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements_text(
                      observation.dire_hero_ids::jsonb
                  ) AS hero(value)
                  WHERE hero.value !~ '^[0-9]+$' OR hero.value::bigint <= 0
              )
              AND (
                  SELECT COUNT(DISTINCT hero_id)
                  FROM (
                      SELECT value AS hero_id
                      FROM jsonb_array_elements_text(
                          observation.radiant_hero_ids::jsonb
                      )
                      UNION ALL
                      SELECT value AS hero_id
                      FROM jsonb_array_elements_text(
                          observation.dire_hero_ids::jsonb
                      )
                  ) AS heroes
              ) = 10
              AND NOT EXISTS (
                  SELECT 1
                  FROM vision_observation_invalidations AS invalidation
                  WHERE invalidation.raybet_match_id = observation.raybet_match_id
                    AND invalidation.captured_at = observation.captured_at
                    AND invalidation.source_frame_ref = observation.source_frame_ref
              )
            """
        )
    )


def _create_vision_rosh_triggers() -> None:
    immutable_contracts = (
        ("vision_frame_artifacts", "vision_frame_artifacts", "vision frame registry is immutable"),
        (
            "vision_frame_artifact_relocations",
            "vision_frame_relocations",
            "vision frame relocation audit is immutable",
        ),
        (
            "vision_frame_artifact_retirements",
            "vision_frame_retirements",
            "vision frame retirement audit is immutable",
        ),
        (
            "vision_observation_invalidations",
            "vision_observation_invalidations_immutable",
            "vision invalidation audit is immutable",
        ),
        (
            "vision_draft_conflicts",
            "vision_draft_conflicts_immutable",
            "vision draft conflict is immutable",
        ),
        ("rosh_lineup_scores", "rosh_lineup_scores_immutable", "Rosh lineup score is immutable"),
        ("rosh_analysis_runs", "rosh_analysis_runs_immutable", "Rosh analysis run is immutable"),
        ("rosh_hero_scores", "rosh_hero_scores_immutable", "Rosh hero score is immutable"),
        ("rosh_minute_points", "rosh_minute_points_immutable", "Rosh minute point is immutable"),
        (
            "official_rosh_shadow_evaluations",
            "official_rosh_shadow_evaluations_immutable",
            "official Rosh shadow evaluations are immutable",
        ),
        (
            "vision_derived_invalidations",
            "vision_derived_invalidations_immutable",
            "vision derived invalidation is immutable",
        ),
    )
    for table, prefix, message in immutable_contracts:
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER {prefix}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                        '{message}'
                    )
                    """
                )
            )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_vision_observation_frame_identity()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.source_frame_ref IS DISTINCT FROM NEW.source_frame_ref
                    OR OLD.source_frame_sha256 IS DISTINCT FROM
                       NEW.source_frame_sha256
                    OR OLD.source_frame_bytes IS DISTINCT FROM NEW.source_frame_bytes
                THEN
                    RAISE EXCEPTION
                        'vision observation frame identity is immutable';
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
            CREATE TRIGGER vision_observation_frame_identity_immutable
            BEFORE UPDATE ON vision_observations
            FOR EACH ROW EXECUTE FUNCTION guard_vision_observation_frame_identity()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION require_succeeded_rosh_run()
            RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM rosh_analysis_runs AS run
                    WHERE run.run_id = NEW.run_id AND run.status = 'succeeded'
                ) THEN
                    RAISE EXCEPTION '%', TG_ARGV[0];
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
            CREATE TRIGGER rosh_hero_scores_succeeded_run_insert
            BEFORE INSERT ON rosh_hero_scores
            FOR EACH ROW EXECUTE FUNCTION require_succeeded_rosh_run(
                'Rosh hero score requires succeeded run'
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER rosh_minute_points_succeeded_run_insert
            BEFORE INSERT ON rosh_minute_points
            FOR EACH ROW EXECUTE FUNCTION require_succeeded_rosh_run(
                'Rosh minute point requires succeeded run'
            )
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW trusted_vision_observation_authority"))
    op.execute(sa.text("DROP VIEW active_vision_frame_artifacts"))

    op.drop_table("odds_alignments")
    op.drop_table("vision_derived_invalidations")
    op.drop_table("official_rosh_shadow_evaluations")
    op.drop_table("rosh_minute_points")
    op.drop_table("rosh_hero_scores")
    op.drop_table("rosh_analysis_runs")
    op.drop_table("rosh_lineup_scores")
    op.drop_table("vision_draft_conflicts")
    op.drop_table("vision_draft_anchors")
    op.drop_table("vision_observation_invalidations")
    op.drop_table("vision_observations")
    op.drop_table("vision_frame_artifact_retirements")
    op.drop_table("vision_frame_artifact_relocations")
    op.drop_table("vision_frame_artifacts")
    op.drop_table("collector_runs")

    op.execute(sa.text("DROP FUNCTION require_succeeded_rosh_run()"))
    op.execute(sa.text("DROP FUNCTION guard_vision_observation_frame_identity()"))
