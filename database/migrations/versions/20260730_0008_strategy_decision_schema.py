"""Create immutable strategy decisions and authority checks.

Revision ID: 20260730_0008
Revises: 20260730_0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0008"
down_revision: str | None = "20260730_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_decisions",
        sa.Column("decision_key", sa.Text(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("underdog_side", sa.Text(), nullable=False),
        sa.Column("market_probability", sa.Double(), nullable=False),
        sa.Column("model_probability", sa.Double(), nullable=False),
        sa.Column("edge", sa.Double(), nullable=False),
        sa.Column("data_quality", sa.Double(), nullable=False),
        sa.Column("eligible", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("contributions_json", sa.Text(), nullable=False),
        sa.Column("input_ref", sa.Text(), nullable=False),
        sa.Column("strategy_version", sa.Text(), nullable=False),
        sa.Column("draft_curve_key", sa.Text(), sa.ForeignKey("prospective_draft_curves.curve_key")),
        sa.Column("draft_source_ref", sa.Text()),
        sa.Column("draft_landmark_key", sa.Text(), sa.ForeignKey("prospective_draft_landmarks.landmark_key")),
        sa.Column("draft_landmark_horizon_minutes", sa.Integer()),
        sa.Column("draft_landmark_target", sa.Text()),
        sa.Column("draft_landmark_radiant_probability", sa.Double()),
        sa.Column("draft_landmark_quality", sa.Double()),
        sa.Column("draft_landmark_uncertainty", sa.Double()),
        sa.Column("draft_landmark_support", sa.Integer()),
        sa.Column("draft_radiant_team_side", sa.Text()),
        sa.Column("draft_strict_mapping_id", sa.BigInteger(), sa.ForeignKey("strict_live_map_mappings.mapping_id")),
        sa.Column("draft_deployment_key", sa.Text(), sa.ForeignKey("draft_deployment_bundles.deployment_key")),
        sa.Column("draft_target_snapshot_hash", sa.Text()),
        sa.Column("draft_feature_hash", sa.Text()),
        sa.Column("draft_model_hash", sa.Text(), sa.ForeignKey("draft_model_artifacts.model_hash")),
        sa.Column("draft_calibration_hash", sa.Text(), sa.ForeignKey("draft_calibration_artifacts.calibration_hash")),
        sa.Column("draft_model_version", sa.Text()),
        sa.Column("draft_global_gate_ref", sa.Text()),
        sa.Column("draft_input_snapshot_hash", sa.Text()),
        sa.Column("draft_authority_revision", sa.BigInteger()),
        sa.Column("draft_dependency_revision", sa.BigInteger()),
        sa.Column("vision_raybet_match_id", sa.Text()),
        sa.Column("vision_map_number", sa.Integer()),
        sa.Column("vision_captured_at", sa.Text()),
        sa.Column("vision_source_frame_ref", sa.Text()),
        sa.Column("vision_source_frame_sha256", sa.Text()),
        sa.Column("vision_source_frame_bytes", sa.BigInteger()),
        sa.Column("vision_observed_game_clock_seconds", sa.Integer()),
        sa.Column("vision_aligned_game_clock_seconds", sa.Integer()),
        sa.Column("vision_is_paused", sa.SmallInteger()),
        sa.Column("vision_radiant_hero_ids_json", sa.Text()),
        sa.Column("vision_dire_hero_ids_json", sa.Text()),
        sa.Column("vision_radiant_team_side", sa.Text()),
        sa.Column("vision_clock_confidence", sa.Double()),
        sa.Column("vision_draft_confidence", sa.Double()),
        sa.Column("vision_screen_state", sa.Text()),
        sa.Column("vision_confirmed", sa.SmallInteger()),
        sa.Column("vision_transport_key", sa.Text()),
        sa.Column("vision_transport_at", sa.Text()),
        sa.Column("vision_alignment_method", sa.Text()),
        sa.Column("vision_alignment_lag_seconds", sa.Double()),
        sa.CheckConstraint("draft_landmark_horizon_minutes IS NULL OR draft_landmark_horizon_minutes IN (10, 20, 30, 40, 50)"),
        sa.CheckConstraint("draft_landmark_target IS NULL OR draft_landmark_target = 'radiant_win'"),
        sa.CheckConstraint("draft_landmark_radiant_probability IS NULL OR draft_landmark_radiant_probability BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("draft_landmark_quality IS NULL OR draft_landmark_quality BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("draft_landmark_uncertainty IS NULL OR draft_landmark_uncertainty BETWEEN 0.0 AND 0.5"),
        sa.CheckConstraint("draft_landmark_support IS NULL OR draft_landmark_support >= 100"),
        sa.CheckConstraint("draft_radiant_team_side IS NULL OR draft_radiant_team_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("draft_strict_mapping_id IS NULL OR draft_strict_mapping_id > 0"),
        sa.CheckConstraint("draft_target_snapshot_hash IS NULL OR length(draft_target_snapshot_hash) = 64"),
        sa.CheckConstraint("draft_feature_hash IS NULL OR length(draft_feature_hash) = 64"),
        sa.CheckConstraint("draft_input_snapshot_hash IS NULL OR length(draft_input_snapshot_hash) = 64"),
        sa.CheckConstraint("draft_authority_revision IS NULL OR draft_authority_revision >= 1"),
        sa.CheckConstraint("draft_dependency_revision IS NULL OR draft_dependency_revision >= 1"),
        sa.CheckConstraint("vision_map_number IS NULL OR vision_map_number > 0"),
        sa.CheckConstraint("vision_source_frame_sha256 IS NULL OR length(vision_source_frame_sha256) = 64"),
        sa.CheckConstraint("vision_source_frame_bytes IS NULL OR vision_source_frame_bytes > 0"),
        sa.CheckConstraint("vision_observed_game_clock_seconds IS NULL OR vision_observed_game_clock_seconds >= 0"),
        sa.CheckConstraint("vision_aligned_game_clock_seconds IS NULL OR vision_aligned_game_clock_seconds >= 0"),
        sa.CheckConstraint("vision_is_paused IS NULL OR vision_is_paused IN (0, 1)"),
        sa.CheckConstraint("vision_radiant_hero_ids_json IS NULL OR vision_radiant_hero_ids_json::jsonb IS NOT NULL"),
        sa.CheckConstraint("vision_dire_hero_ids_json IS NULL OR vision_dire_hero_ids_json::jsonb IS NOT NULL"),
        sa.CheckConstraint("vision_radiant_team_side IS NULL OR vision_radiant_team_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("vision_clock_confidence IS NULL OR vision_clock_confidence BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("vision_draft_confidence IS NULL OR vision_draft_confidence BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("vision_confirmed IS NULL OR vision_confirmed IN (0, 1)"),
        sa.CheckConstraint("vision_alignment_method IS NULL OR vision_alignment_method IN ('anchor', 'forward_projection')"),
        sa.CheckConstraint("vision_alignment_lag_seconds IS NULL OR vision_alignment_lag_seconds BETWEEN 0.0 AND 15.0"),
    )
    _create_verified_view()
    _create_strategy_triggers()


def _create_verified_view() -> None:
    op.execute(sa.text("""
        CREATE VIEW verified_strategy_decision_vision_authority AS
        SELECT decision.decision_key
        FROM strategy_decisions AS decision
        JOIN trusted_vision_observation_authority AS vision
          ON vision.raybet_match_id = decision.vision_raybet_match_id
         AND vision.map_number = decision.vision_map_number
         AND vision.captured_at = decision.vision_captured_at
         AND vision.source_frame_ref = decision.vision_source_frame_ref
         AND vision.source_frame_sha256 = decision.vision_source_frame_sha256
         AND vision.source_frame_bytes = decision.vision_source_frame_bytes
         AND vision.game_clock_seconds = decision.vision_observed_game_clock_seconds
         AND vision.is_paused = decision.vision_is_paused
         AND vision.radiant_hero_ids::jsonb = decision.vision_radiant_hero_ids_json::jsonb
         AND vision.dire_hero_ids::jsonb = decision.vision_dire_hero_ids_json::jsonb
         AND vision.radiant_team_side = decision.vision_radiant_team_side
         AND vision.clock_confidence = decision.vision_clock_confidence
         AND vision.draft_confidence = decision.vision_draft_confidence
         AND vision.screen_state = decision.vision_screen_state
         AND vision.confirmed = decision.vision_confirmed
        JOIN odds_transport_observations AS transport
          ON transport.observation_key = decision.vision_transport_key
         AND transport.raybet_match_id = decision.raybet_match_id
         AND transport.observed_at = decision.vision_transport_at
         AND transport.timing_status = 'on_time'
         AND transport.processing_status = 'processed'
        JOIN trusted_odds_winner_market_authority AS market
          ON market.observation_key = transport.observation_key
         AND market.raybet_match_id = transport.raybet_match_id
         AND market.period = 'map_' || decision.map_number
         AND market.response_state_hash = transport.response_state_hash
         AND market.response_artifact_hash = transport.response_artifact_hash
         AND market.underdog_side = decision.underdog_side
         AND abs(market.underdog_probability - decision.market_probability) <= 1.0e-12
        JOIN prospective_draft_curves AS curve
          ON curve.curve_key = decision.draft_curve_key
         AND curve.raybet_match_id = decision.raybet_match_id
         AND curve.map_number = decision.map_number
         AND curve.radiant_hero_ids_json::jsonb = decision.vision_radiant_hero_ids_json::jsonb
         AND curve.dire_hero_ids_json::jsonb = decision.vision_dire_hero_ids_json::jsonb
         AND curve.radiant_team_side = decision.vision_radiant_team_side
        JOIN vision_draft_anchors AS anchor
          ON anchor.raybet_match_id = curve.raybet_match_id
         AND anchor.map_number = curve.map_number
         AND anchor.status IN ('anchored', 'conflict')
         AND anchor.draft_hash = curve.anchor_draft_hash
         AND anchor.radiant_team_side = curve.radiant_team_side
         AND anchor.anchored_at = curve.anchor_anchored_at
         AND anchor.source_frame_ref = curve.anchor_source_frame_ref
         AND anchor.team_side_anchored_at = curve.anchor_team_side_anchored_at
         AND anchor.team_side_source_frame_ref = curve.anchor_team_side_source_frame_ref
        JOIN trusted_vision_observation_authority AS anchor_frame
          ON anchor_frame.raybet_match_id = anchor.raybet_match_id
         AND anchor_frame.map_number = anchor.map_number
         AND anchor_frame.captured_at = anchor.anchored_at
         AND anchor_frame.source_frame_ref = anchor.source_frame_ref
        JOIN trusted_vision_observation_authority AS side_frame
          ON side_frame.raybet_match_id = anchor.raybet_match_id
         AND side_frame.map_number = anchor.map_number
         AND side_frame.captured_at = anchor.team_side_anchored_at
         AND side_frame.source_frame_ref = anchor.team_side_source_frame_ref
        WHERE decision.eligible = 1
          AND decision.vision_raybet_match_id = decision.raybet_match_id
          AND decision.vision_map_number = decision.map_number
          AND decision.vision_confirmed = 1
          AND decision.vision_is_paused = 0
          AND decision.vision_screen_state = 'game'
          AND live_text_timestamp_utc(decision.vision_transport_at) = live_text_timestamp_utc(decision.decided_at)
          AND live_text_timestamp_utc(decision.vision_captured_at) <= live_text_timestamp_utc(decision.vision_transport_at)
          AND decision.vision_alignment_lag_seconds BETWEEN 0.0 AND 15.0
          AND abs(decision.vision_alignment_lag_seconds - extract(epoch FROM (live_text_timestamp_utc(decision.vision_transport_at) - live_text_timestamp_utc(decision.vision_captured_at)))) <= 0.001
          AND decision.vision_aligned_game_clock_seconds = trunc(decision.vision_observed_game_clock_seconds + decision.vision_alignment_lag_seconds)::integer
          AND decision.vision_alignment_method = CASE WHEN decision.vision_alignment_lag_seconds >= 1.0 THEN 'forward_projection' ELSE 'anchor' END
          AND live_text_timestamp_utc(curve.first_usable_at) <= live_text_timestamp_utc(decision.decided_at)
          AND anchor.radiant_hero_ids::jsonb = curve.radiant_hero_ids_json::jsonb
          AND anchor.dire_hero_ids::jsonb = curve.dire_hero_ids_json::jsonb
          AND anchor_frame.radiant_hero_ids::jsonb = curve.radiant_hero_ids_json::jsonb
          AND anchor_frame.dire_hero_ids::jsonb = curve.dire_hero_ids_json::jsonb
          AND anchor_frame.radiant_team_side = curve.radiant_team_side
          AND side_frame.radiant_team_side = curve.radiant_team_side
          AND (anchor.status = 'anchored' OR (live_text_timestamp_utc(anchor.conflict_at) IS NOT NULL AND live_text_timestamp_utc(anchor.conflict_at) > live_text_timestamp_utc(decision.vision_transport_at)))
          AND NOT EXISTS (
              SELECT 1 FROM vision_observations AS later
              WHERE later.raybet_match_id = vision.raybet_match_id
                AND (live_text_timestamp_utc(later.captured_at) IS NULL OR (
                    live_text_timestamp_utc(later.captured_at) <= live_text_timestamp_utc(decision.vision_transport_at)
                    AND live_text_timestamp_utc(later.captured_at) >= live_text_timestamp_utc(vision.captured_at)
                    AND NOT (later.captured_at = vision.captured_at AND later.source_frame_ref = vision.source_frame_ref)
                ))
          )
          AND NOT EXISTS (
              SELECT 1 FROM vision_draft_conflicts AS conflict
              WHERE conflict.raybet_match_id = curve.raybet_match_id
                AND conflict.map_number = curve.map_number
                AND (live_text_timestamp_utc(conflict.captured_at) IS NULL OR live_text_timestamp_utc(conflict.captured_at) <= live_text_timestamp_utc(decision.vision_transport_at))
          )
          AND NOT EXISTS (
              SELECT 1 FROM vision_derived_invalidations AS invalidation
              WHERE invalidation.dependent_type = 'strategy_decision'
                AND invalidation.dependent_key = decision.decision_key
          )
    """))


def _create_strategy_triggers() -> None:
    for operation in ("UPDATE", "DELETE"):
        op.execute(sa.text(f"""
            CREATE TRIGGER strategy_decisions_immutable_{operation.lower()}
            BEFORE {operation} ON strategy_decisions
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                'strategy decisions are immutable'
            )
        """))
    op.execute(sa.text("""
        CREATE FUNCTION require_strategy_draft_authority()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.eligible = 1 AND NOT EXISTS (
                SELECT 1
                FROM prospective_draft_landmark_authority AS authority
                JOIN draft_authority_revisions AS revision ON revision.singleton = 1
                JOIN draft_lineage_revisions AS lineage ON lineage.singleton = 1
                WHERE authority.curve_key = NEW.draft_curve_key
                  AND authority.source_ref = NEW.draft_source_ref
                  AND authority.raybet_match_id = NEW.raybet_match_id
                  AND authority.map_number = NEW.map_number
                  AND authority.strict_mapping_id = NEW.draft_strict_mapping_id
                  AND live_text_timestamp_utc(authority.first_usable_at) <= live_text_timestamp_utc(NEW.decided_at)
                  AND authority.radiant_team_side = NEW.draft_radiant_team_side
                  AND authority.deployment_key = NEW.draft_deployment_key
                  AND authority.target_snapshot_hash = NEW.draft_target_snapshot_hash
                  AND authority.landmark_key = NEW.draft_landmark_key
                  AND authority.horizon_minutes = NEW.draft_landmark_horizon_minutes
                  AND authority.landmark_target = NEW.draft_landmark_target
                  AND authority.radiant_probability = NEW.draft_landmark_radiant_probability
                  AND authority.quality = NEW.draft_landmark_quality
                  AND authority.uncertainty IS NOT DISTINCT FROM NEW.draft_landmark_uncertainty
                  AND authority.support = NEW.draft_landmark_support
                  AND authority.feature_hash = NEW.draft_feature_hash
                  AND authority.model_hash = NEW.draft_model_hash
                  AND authority.calibration_hash = NEW.draft_calibration_hash
                  AND authority.model_version = NEW.draft_model_version
                  AND authority.global_gate_ref = NEW.draft_global_gate_ref
                  AND authority.input_snapshot_hash = NEW.draft_input_snapshot_hash
                  AND revision.authority_revision = NEW.draft_authority_revision
                  AND lineage.dependency_revision = NEW.draft_dependency_revision
                  AND authority.feature_dependency_revision = NEW.draft_dependency_revision
            ) THEN
                RAISE EXCEPTION 'eligible strategy decision draft authority is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER strategy_decision_draft_authority_insert
        BEFORE INSERT ON strategy_decisions
        FOR EACH ROW EXECUTE FUNCTION require_strategy_draft_authority()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION require_strategy_vision_authority()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.eligible = 1 AND NOT EXISTS (
                SELECT 1 FROM verified_strategy_decision_vision_authority
                WHERE decision_key = NEW.decision_key
            ) THEN
                RAISE EXCEPTION 'eligible strategy decision vision authority is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE CONSTRAINT TRIGGER strategy_decision_vision_authority_insert
        AFTER INSERT ON strategy_decisions
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW EXECUTE FUNCTION require_strategy_vision_authority()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION record_invalidated_strategy_mapping_impact()
        RETURNS trigger AS $$
        DECLARE mapping_id_value bigint;
        BEGIN
            IF jsonb_typeof(NEW.contributions_json::jsonb) = 'object' THEN
                mapping_id_value := NULLIF(NEW.contributions_json::jsonb #>> '{__inputs__,strict_live_eligibility,mapping_refs,strict_mapping_id}', '')::bigint;
                IF mapping_id_value IS NOT NULL THEN
                    INSERT INTO strict_live_mapping_impacts (
                        mapping_id, invalidation_id, dependent_type,
                        dependent_key, reason, recorded_at
                    )
                    SELECT mapping_id_value, invalidation.invalidation_id,
                           'strategy_decision', NEW.decision_key,
                           invalidation.reason,
                           replace(to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'), '_', ':')
                    FROM strict_live_map_mapping_invalidations AS invalidation
                    WHERE invalidation.mapping_id = mapping_id_value
                    ON CONFLICT (mapping_id, dependent_type, dependent_key) DO NOTHING;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER strict_live_strategy_impact_after_insert
        AFTER INSERT ON strategy_decisions
        FOR EACH ROW EXECUTE FUNCTION record_invalidated_strategy_mapping_impact()
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW verified_strategy_decision_vision_authority"))
    op.drop_table("strategy_decisions")
    op.execute(sa.text("DROP FUNCTION record_invalidated_strategy_mapping_impact()"))
    op.execute(sa.text("DROP FUNCTION require_strategy_vision_authority()"))
    op.execute(sa.text("DROP FUNCTION require_strategy_draft_authority()"))
