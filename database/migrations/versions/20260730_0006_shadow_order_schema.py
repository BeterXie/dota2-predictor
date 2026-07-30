"""Create live frames, quotes, and immutable shadow orders.

Revision ID: 20260730_0006
Revises: 20260730_0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0006"
down_revision: str | None = "20260730_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_frames",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_match_id", sa.Text(), nullable=False),
        sa.Column("provider_game_id", sa.Text()),
        sa.Column("sequence", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_at", sa.Text()),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("game_time", sa.Integer()),
        sa.Column("team_one_kills", sa.Integer()),
        sa.Column("team_two_kills", sa.Integer()),
        sa.Column("team_one_gold", sa.BigInteger()),
        sa.Column("team_two_gold", sa.BigInteger()),
        sa.Column("state", sa.Text()),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "provider", "provider_match_id", "provider_game_id", "sequence"
        ),
    )
    op.create_table(
        "live_events",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("provider_match_id", sa.Text(), nullable=False),
        sa.Column("provider_game_id", sa.Text()),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source_at", sa.Text()),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("game_time", sa.Integer()),
        sa.Column("team", sa.Text()),
        sa.Column("player", sa.Text()),
        sa.Column("value", sa.Double()),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "provider_event_id"),
    )
    op.create_table(
        "model_quotes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("provider_game_id", sa.Text()),
        sa.Column("market_key", sa.Text(), nullable=False),
        sa.Column("model_probability", sa.Double(), nullable=False),
        sa.Column("market_probability", sa.Double(), nullable=False),
        sa.Column("edge", sa.Double(), nullable=False),
        sa.Column("quoted_at", sa.Text(), nullable=False),
        sa.Column("strategy_version", sa.Text(), nullable=False),
        sa.Column("input_ref", sa.Text(), nullable=False),
    )
    _create_shadow_orders()
    _create_shadow_order_triggers()


def _create_shadow_orders() -> None:
    op.create_table(
        "shadow_orders",
        sa.Column("order_key", sa.Text(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column(
            "strict_mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
        ),
        sa.Column("odds_id", sa.Text(), nullable=False),
        sa.Column("market_key", sa.Text(), nullable=False),
        sa.Column("signaled_at", sa.Text(), nullable=False),
        sa.Column("model_probability", sa.Double(), nullable=False),
        sa.Column("market_probability", sa.Double(), nullable=False),
        sa.Column("signal_price", sa.Double(), nullable=False),
        sa.Column("signal_transport_key", sa.Text(), nullable=False),
        sa.Column("signal_transport_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("signal_odds_group_id", sa.Text()),
        sa.Column("signal_outcome_key", sa.Text()),
        sa.Column("signal_identity_verified", sa.SmallInteger(), nullable=False),
        sa.Column("stake", sa.Double(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("fill_price", sa.Double()),
        sa.Column("filled_at", sa.Text()),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column(
            "draft_curve_key",
            sa.Text(),
            sa.ForeignKey("prospective_draft_curves.curve_key"),
        ),
        sa.Column("draft_source_ref", sa.Text()),
        sa.Column(
            "draft_landmark_key",
            sa.Text(),
            sa.ForeignKey("prospective_draft_landmarks.landmark_key"),
        ),
        sa.Column("draft_landmark_horizon_minutes", sa.Integer()),
        sa.Column("draft_landmark_target", sa.Text()),
        sa.Column("draft_landmark_radiant_probability", sa.Double()),
        sa.Column("draft_landmark_quality", sa.Double()),
        sa.Column("draft_landmark_uncertainty", sa.Double()),
        sa.Column("draft_landmark_support", sa.Integer()),
        sa.Column("draft_radiant_team_side", sa.Text()),
        sa.Column(
            "draft_strict_mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
        ),
        sa.Column(
            "draft_deployment_key",
            sa.Text(),
            sa.ForeignKey("draft_deployment_bundles.deployment_key"),
        ),
        sa.Column("draft_target_snapshot_hash", sa.Text()),
        sa.Column("draft_feature_hash", sa.Text()),
        sa.Column(
            "draft_model_hash",
            sa.Text(),
            sa.ForeignKey("draft_model_artifacts.model_hash"),
        ),
        sa.Column(
            "draft_calibration_hash",
            sa.Text(),
            sa.ForeignKey("draft_calibration_artifacts.calibration_hash"),
        ),
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
        sa.CheckConstraint("signal_identity_verified IN (0, 1)"),
        sa.CheckConstraint("stake > 0.0 AND stake <= 1.0"),
        sa.CheckConstraint(
            "draft_landmark_horizon_minutes IS NULL OR "
            "draft_landmark_horizon_minutes IN (10, 20, 30, 40, 50)"
        ),
        sa.CheckConstraint(
            "draft_landmark_target IS NULL OR "
            "draft_landmark_target = 'radiant_win'"
        ),
        sa.CheckConstraint(
            "draft_landmark_radiant_probability IS NULL OR "
            "draft_landmark_radiant_probability BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "draft_landmark_quality IS NULL OR "
            "draft_landmark_quality BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "draft_landmark_uncertainty IS NULL OR "
            "draft_landmark_uncertainty BETWEEN 0.0 AND 0.5"
        ),
        sa.CheckConstraint(
            "draft_landmark_support IS NULL OR draft_landmark_support >= 100"
        ),
        sa.CheckConstraint(
            "draft_radiant_team_side IS NULL OR "
            "draft_radiant_team_side IN ('team_one', 'team_two')"
        ),
        sa.CheckConstraint(
            "draft_strict_mapping_id IS NULL OR draft_strict_mapping_id > 0"
        ),
        sa.CheckConstraint(
            "draft_target_snapshot_hash IS NULL OR "
            "length(draft_target_snapshot_hash) = 64"
        ),
        sa.CheckConstraint(
            "draft_feature_hash IS NULL OR length(draft_feature_hash) = 64"
        ),
        sa.CheckConstraint(
            "draft_input_snapshot_hash IS NULL OR "
            "length(draft_input_snapshot_hash) = 64"
        ),
        sa.CheckConstraint(
            "draft_authority_revision IS NULL OR draft_authority_revision >= 1"
        ),
        sa.CheckConstraint(
            "draft_dependency_revision IS NULL OR draft_dependency_revision >= 1"
        ),
        sa.CheckConstraint("vision_map_number IS NULL OR vision_map_number > 0"),
        sa.CheckConstraint(
            "vision_source_frame_sha256 IS NULL OR "
            "length(vision_source_frame_sha256) = 64"
        ),
        sa.CheckConstraint(
            "vision_source_frame_bytes IS NULL OR vision_source_frame_bytes > 0"
        ),
        sa.CheckConstraint(
            "vision_observed_game_clock_seconds IS NULL OR "
            "vision_observed_game_clock_seconds >= 0"
        ),
        sa.CheckConstraint(
            "vision_aligned_game_clock_seconds IS NULL OR "
            "vision_aligned_game_clock_seconds >= 0"
        ),
        sa.CheckConstraint("vision_is_paused IS NULL OR vision_is_paused IN (0, 1)"),
        sa.CheckConstraint(
            "vision_radiant_hero_ids_json IS NULL OR "
            "vision_radiant_hero_ids_json::jsonb IS NOT NULL"
        ),
        sa.CheckConstraint(
            "vision_dire_hero_ids_json IS NULL OR "
            "vision_dire_hero_ids_json::jsonb IS NOT NULL"
        ),
        sa.CheckConstraint(
            "vision_radiant_team_side IS NULL OR "
            "vision_radiant_team_side IN ('team_one', 'team_two')"
        ),
        sa.CheckConstraint(
            "vision_clock_confidence IS NULL OR "
            "vision_clock_confidence BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "vision_draft_confidence IS NULL OR "
            "vision_draft_confidence BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint("vision_confirmed IS NULL OR vision_confirmed IN (0, 1)"),
        sa.CheckConstraint(
            "vision_alignment_method IS NULL OR "
            "vision_alignment_method IN ('anchor', 'forward_projection')"
        ),
        sa.CheckConstraint(
            "vision_alignment_lag_seconds IS NULL OR "
            "vision_alignment_lag_seconds BETWEEN 0.0 AND 15.0"
        ),
    )
    op.create_table(
        "shadow_order_decision_lineage",
        sa.Column("order_key", sa.Text(), primary_key=True),
        sa.Column("decision_key", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
    )


def _create_shadow_order_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_shadow_order_terminal_transition()
            RETURNS trigger AS $$
            BEGIN
                IF NOT (
                    OLD.order_key IS NOT DISTINCT FROM NEW.order_key
                    AND OLD.raybet_match_id IS NOT DISTINCT FROM NEW.raybet_match_id
                    AND OLD.strict_mapping_id IS NOT DISTINCT FROM NEW.strict_mapping_id
                    AND OLD.odds_id IS NOT DISTINCT FROM NEW.odds_id
                    AND OLD.market_key IS NOT DISTINCT FROM NEW.market_key
                    AND OLD.signaled_at IS NOT DISTINCT FROM NEW.signaled_at
                    AND OLD.model_probability IS NOT DISTINCT FROM NEW.model_probability
                    AND OLD.market_probability IS NOT DISTINCT FROM NEW.market_probability
                    AND OLD.signal_price IS NOT DISTINCT FROM NEW.signal_price
                    AND OLD.signal_transport_key IS NOT DISTINCT FROM
                        NEW.signal_transport_key
                    AND OLD.signal_transport_at IS NOT DISTINCT FROM
                        NEW.signal_transport_at
                    AND OLD.expires_at IS NOT DISTINCT FROM NEW.expires_at
                    AND OLD.signal_odds_group_id IS NOT DISTINCT FROM
                        NEW.signal_odds_group_id
                    AND OLD.signal_outcome_key IS NOT DISTINCT FROM
                        NEW.signal_outcome_key
                    AND OLD.signal_identity_verified IS NOT DISTINCT FROM
                        NEW.signal_identity_verified
                    AND OLD.stake IS NOT DISTINCT FROM NEW.stake
                    AND OLD.draft_curve_key IS NOT DISTINCT FROM NEW.draft_curve_key
                    AND OLD.draft_source_ref IS NOT DISTINCT FROM NEW.draft_source_ref
                    AND OLD.draft_landmark_key IS NOT DISTINCT FROM
                        NEW.draft_landmark_key
                    AND OLD.draft_landmark_horizon_minutes IS NOT DISTINCT FROM
                        NEW.draft_landmark_horizon_minutes
                    AND OLD.draft_landmark_target IS NOT DISTINCT FROM
                        NEW.draft_landmark_target
                    AND OLD.draft_landmark_radiant_probability IS NOT DISTINCT FROM
                        NEW.draft_landmark_radiant_probability
                    AND OLD.draft_landmark_quality IS NOT DISTINCT FROM
                        NEW.draft_landmark_quality
                    AND OLD.draft_landmark_uncertainty IS NOT DISTINCT FROM
                        NEW.draft_landmark_uncertainty
                    AND OLD.draft_landmark_support IS NOT DISTINCT FROM
                        NEW.draft_landmark_support
                    AND OLD.draft_radiant_team_side IS NOT DISTINCT FROM
                        NEW.draft_radiant_team_side
                    AND OLD.draft_strict_mapping_id IS NOT DISTINCT FROM
                        NEW.draft_strict_mapping_id
                    AND OLD.draft_deployment_key IS NOT DISTINCT FROM
                        NEW.draft_deployment_key
                    AND OLD.draft_target_snapshot_hash IS NOT DISTINCT FROM
                        NEW.draft_target_snapshot_hash
                    AND OLD.draft_feature_hash IS NOT DISTINCT FROM
                        NEW.draft_feature_hash
                    AND OLD.draft_model_hash IS NOT DISTINCT FROM NEW.draft_model_hash
                    AND OLD.draft_calibration_hash IS NOT DISTINCT FROM
                        NEW.draft_calibration_hash
                    AND OLD.draft_model_version IS NOT DISTINCT FROM
                        NEW.draft_model_version
                    AND OLD.draft_global_gate_ref IS NOT DISTINCT FROM
                        NEW.draft_global_gate_ref
                    AND OLD.draft_input_snapshot_hash IS NOT DISTINCT FROM
                        NEW.draft_input_snapshot_hash
                    AND OLD.draft_authority_revision IS NOT DISTINCT FROM
                        NEW.draft_authority_revision
                    AND OLD.draft_dependency_revision IS NOT DISTINCT FROM
                        NEW.draft_dependency_revision
                    AND OLD.vision_raybet_match_id IS NOT DISTINCT FROM
                        NEW.vision_raybet_match_id
                    AND OLD.vision_map_number IS NOT DISTINCT FROM NEW.vision_map_number
                    AND OLD.vision_captured_at IS NOT DISTINCT FROM
                        NEW.vision_captured_at
                    AND OLD.vision_source_frame_ref IS NOT DISTINCT FROM
                        NEW.vision_source_frame_ref
                    AND OLD.vision_source_frame_sha256 IS NOT DISTINCT FROM
                        NEW.vision_source_frame_sha256
                    AND OLD.vision_source_frame_bytes IS NOT DISTINCT FROM
                        NEW.vision_source_frame_bytes
                    AND OLD.vision_observed_game_clock_seconds IS NOT DISTINCT FROM
                        NEW.vision_observed_game_clock_seconds
                    AND OLD.vision_aligned_game_clock_seconds IS NOT DISTINCT FROM
                        NEW.vision_aligned_game_clock_seconds
                    AND OLD.vision_is_paused IS NOT DISTINCT FROM NEW.vision_is_paused
                    AND OLD.vision_radiant_hero_ids_json IS NOT DISTINCT FROM
                        NEW.vision_radiant_hero_ids_json
                    AND OLD.vision_dire_hero_ids_json IS NOT DISTINCT FROM
                        NEW.vision_dire_hero_ids_json
                    AND OLD.vision_radiant_team_side IS NOT DISTINCT FROM
                        NEW.vision_radiant_team_side
                    AND OLD.vision_clock_confidence IS NOT DISTINCT FROM
                        NEW.vision_clock_confidence
                    AND OLD.vision_draft_confidence IS NOT DISTINCT FROM
                        NEW.vision_draft_confidence
                    AND OLD.vision_screen_state IS NOT DISTINCT FROM
                        NEW.vision_screen_state
                    AND OLD.vision_confirmed IS NOT DISTINCT FROM NEW.vision_confirmed
                    AND OLD.vision_transport_key IS NOT DISTINCT FROM
                        NEW.vision_transport_key
                    AND OLD.vision_transport_at IS NOT DISTINCT FROM
                        NEW.vision_transport_at
                    AND OLD.vision_alignment_method IS NOT DISTINCT FROM
                        NEW.vision_alignment_method
                    AND OLD.vision_alignment_lag_seconds IS NOT DISTINCT FROM
                        NEW.vision_alignment_lag_seconds
                    AND OLD.status = 'pending'
                    AND OLD.fill_price IS NULL
                    AND OLD.filled_at IS NULL
                    AND OLD.rejection_reason IS NULL
                    AND (
                        (
                            NEW.status = 'pending'
                            AND NEW.fill_price IS NULL
                            AND NEW.filled_at IS NULL
                            AND NEW.rejection_reason IS NULL
                        )
                        OR (
                            NEW.status = 'filled'
                            AND NEW.fill_price > 1.0
                            AND NULLIF(NEW.filled_at, '') IS NOT NULL
                            AND NEW.rejection_reason IS NULL
                        )
                        OR (
                            NEW.status = 'rejected'
                            AND NEW.fill_price IS NULL
                            AND NEW.filled_at IS NULL
                            AND NULLIF(NEW.rejection_reason, '') IS NOT NULL
                        )
                    )
                ) THEN
                    RAISE EXCEPTION 'shadow order terminal state is immutable';
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
            CREATE TRIGGER shadow_orders_terminal_immutable
            BEFORE UPDATE ON shadow_orders
            FOR EACH ROW EXECUTE FUNCTION guard_shadow_order_terminal_transition()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER shadow_orders_immutable_delete
            BEFORE DELETE ON shadow_orders
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                'shadow orders are immutable'
            )
            """
        )
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER shadow_order_decision_lineage_immutable_{operation.lower()}
                BEFORE {operation} ON shadow_order_decision_lineage
                FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                    'shadow order decision lineage is immutable'
                )
                """
            )
        )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION record_invalidated_shadow_mapping_impact()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.strict_mapping_id IS NOT NULL THEN
                    INSERT INTO strict_live_mapping_impacts (
                        mapping_id, invalidation_id, dependent_type,
                        dependent_key, reason, recorded_at
                    )
                    SELECT NEW.strict_mapping_id,
                           cause.invalidation_id,
                           'shadow_order',
                           NEW.order_key,
                           cause.reason,
                           replace(
                               to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                                       'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                               '_', ':'
                           )
                    FROM (
                        SELECT invalidation.invalidation_id, invalidation.reason
                        FROM strict_live_map_mapping_invalidations AS invalidation
                        WHERE invalidation.mapping_id = NEW.strict_mapping_id
                        UNION ALL
                        SELECT invalidation.invalidation_id, invalidation.reason
                        FROM strict_live_map_mappings AS mapping
                        JOIN strict_live_automatic_evidence_approvals AS approval
                          ON approval.approval_id = mapping.automatic_approval_id
                        JOIN strict_live_map_mapping_invalidations AS invalidation
                          ON invalidation.mapping_id = approval.source_mapping_id
                        WHERE mapping.mapping_id = NEW.strict_mapping_id
                        LIMIT 1
                    ) AS cause
                    ON CONFLICT (mapping_id, dependent_type, dependent_key)
                    DO NOTHING;
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
            CREATE TRIGGER strict_live_shadow_impact_after_insert
            AFTER INSERT ON shadow_orders
            FOR EACH ROW EXECUTE FUNCTION record_invalidated_shadow_mapping_impact()
            """
        )
    )


def downgrade() -> None:
    op.drop_table("shadow_order_decision_lineage")
    op.drop_table("shadow_orders")
    op.drop_table("model_quotes")
    op.drop_table("live_events")
    op.drop_table("live_frames")

    op.execute(sa.text("DROP FUNCTION record_invalidated_shadow_mapping_impact()"))
    op.execute(sa.text("DROP FUNCTION guard_shadow_order_terminal_transition()"))
