"""Complete runtime authority parity and publish schema versions.

Revision ID: 20260730_0012
Revises: 20260730_0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0012"
down_revision: str | None = "20260730_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE_SCHEMA_VERSION = 12
RUNTIME_SCHEMA_VERSION = 1
RUNTIME_CONTRACT_DIGEST = (
    "eb58ed6794cd39cdf4b9947a9132f2c2683cb20c769770586e3ca5c9f093beb9"
)
PREVIOUS_RUNTIME_CONTRACT_DIGEST = (
    "7403fa6318b671f024b8765179b87e33ad0faf2b5d67ac6e90d1e689be3816fe"
)


def upgrade() -> None:
    _create_shadow_order_authority()
    _create_vision_draft_anchor_authority()
    _publish_schema_versions()


def _create_shadow_order_authority() -> None:
    op.execute(sa.text("""
        CREATE FUNCTION guard_shadow_order_signal_identity()
        RETURNS trigger AS $$ BEGIN
            IF NEW.strict_mapping_id IS NULL
               OR NEW.strict_mapping_id <= 0
               OR NULLIF(NEW.signal_transport_key, '') IS NULL
               OR live_text_timestamp_utc(NEW.signal_transport_at) IS NULL
               OR live_text_timestamp_utc(NEW.expires_at) IS NULL
               OR NEW.signal_identity_verified != 1
               OR NULLIF(NEW.signal_odds_group_id, '') IS NULL
               OR NULLIF(NEW.signal_outcome_key, '') IS NULL
            THEN
                RAISE EXCEPTION 'shadow order signal identity is required';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                OLD.raybet_match_id IS DISTINCT FROM NEW.raybet_match_id
                OR OLD.strict_mapping_id IS DISTINCT FROM NEW.strict_mapping_id
                OR OLD.odds_id IS DISTINCT FROM NEW.odds_id
                OR OLD.market_key IS DISTINCT FROM NEW.market_key
                OR OLD.signaled_at IS DISTINCT FROM NEW.signaled_at
                OR OLD.signal_price IS DISTINCT FROM NEW.signal_price
                OR OLD.signal_transport_key IS DISTINCT FROM
                    NEW.signal_transport_key
                OR OLD.signal_transport_at IS DISTINCT FROM NEW.signal_transport_at
                OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                OR OLD.signal_odds_group_id IS DISTINCT FROM
                    NEW.signal_odds_group_id
                OR OLD.signal_outcome_key IS DISTINCT FROM NEW.signal_outcome_key
                OR OLD.signal_identity_verified IS DISTINCT FROM
                    NEW.signal_identity_verified
            ) THEN
                RAISE EXCEPTION 'shadow order signal identity is immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER shadow_orders_signal_identity_guard
        BEFORE INSERT OR UPDATE ON shadow_orders
        FOR EACH ROW EXECUTE FUNCTION guard_shadow_order_signal_identity()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION require_shadow_order_draft_authority()
        RETURNS trigger AS $$ BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM prospective_draft_landmark_authority AS authority
                JOIN shadow_map_attempts AS attempt
                  ON attempt.order_key = NEW.order_key
                 AND attempt.raybet_match_id = NEW.raybet_match_id
                JOIN draft_authority_revisions AS revision
                  ON revision.singleton = 1
                JOIN draft_lineage_revisions AS lineage
                  ON lineage.singleton = 1
                WHERE authority.curve_key = NEW.draft_curve_key
                  AND authority.source_ref = NEW.draft_source_ref
                  AND authority.raybet_match_id = NEW.raybet_match_id
                  AND authority.map_number = attempt.map_number
                  AND authority.strict_mapping_id = NEW.strict_mapping_id
                  AND authority.strict_mapping_id = NEW.draft_strict_mapping_id
                  AND live_text_timestamp_utc(authority.first_usable_at) <=
                      live_text_timestamp_utc(NEW.signal_transport_at)
                  AND authority.radiant_team_side = NEW.draft_radiant_team_side
                  AND authority.deployment_key = NEW.draft_deployment_key
                  AND authority.target_snapshot_hash =
                      NEW.draft_target_snapshot_hash
                  AND authority.landmark_key = NEW.draft_landmark_key
                  AND authority.horizon_minutes =
                      NEW.draft_landmark_horizon_minutes
                  AND authority.landmark_target = NEW.draft_landmark_target
                  AND authority.radiant_probability =
                      NEW.draft_landmark_radiant_probability
                  AND authority.quality = NEW.draft_landmark_quality
                  AND authority.uncertainty IS NOT DISTINCT FROM
                      NEW.draft_landmark_uncertainty
                  AND authority.support = NEW.draft_landmark_support
                  AND authority.feature_hash = NEW.draft_feature_hash
                  AND authority.model_hash = NEW.draft_model_hash
                  AND authority.calibration_hash = NEW.draft_calibration_hash
                  AND authority.model_version = NEW.draft_model_version
                  AND authority.global_gate_ref = NEW.draft_global_gate_ref
                  AND authority.input_snapshot_hash =
                      NEW.draft_input_snapshot_hash
                  AND revision.authority_revision = NEW.draft_authority_revision
                  AND lineage.dependency_revision =
                      NEW.draft_dependency_revision
                  AND authority.feature_dependency_revision =
                      NEW.draft_dependency_revision
            ) THEN
                RAISE EXCEPTION 'shadow order draft authority is required';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER shadow_order_draft_authority_insert
        BEFORE INSERT ON shadow_orders
        FOR EACH ROW EXECUTE FUNCTION require_shadow_order_draft_authority()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION require_shadow_order_vision_authority()
        RETURNS trigger AS $$ BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM shadow_order_decision_lineage AS lineage
                JOIN strategy_decisions AS decision
                  ON decision.decision_key = lineage.decision_key
                 AND decision.eligible = 1
                JOIN verified_strategy_decision_vision_authority AS verified
                  ON verified.decision_key = decision.decision_key
                JOIN trusted_odds_winner_market_authority AS market
                  ON market.observation_key = decision.vision_transport_key
                 AND market.raybet_match_id = decision.raybet_match_id
                 AND market.period = 'map_' || decision.map_number
                WHERE lineage.order_key = NEW.order_key
                  AND NEW.raybet_match_id = decision.raybet_match_id
                  AND NEW.odds_id = market.underdog_odds_id
                  AND NEW.market_key = 'winner|' || market.period || '|' ||
                      market.underdog_side || '|'
                  AND NEW.model_probability = decision.model_probability
                  AND abs(
                      NEW.market_probability - market.underdog_probability
                  ) <= 1e-12
                  AND NEW.signal_price = market.underdog_price
                  AND NEW.signal_odds_group_id = market.odds_group_id
                  AND NEW.signal_outcome_key = market.underdog_side
                  AND NEW.signal_outcome_key = decision.underdog_side
                  AND NEW.signal_transport_key = decision.vision_transport_key
                  AND NEW.signal_transport_at = decision.vision_transport_at
                  AND NEW.vision_raybet_match_id IS NOT DISTINCT FROM
                      decision.vision_raybet_match_id
                  AND NEW.vision_map_number IS NOT DISTINCT FROM
                      decision.vision_map_number
                  AND NEW.vision_captured_at IS NOT DISTINCT FROM
                      decision.vision_captured_at
                  AND NEW.vision_source_frame_ref IS NOT DISTINCT FROM
                      decision.vision_source_frame_ref
                  AND NEW.vision_source_frame_sha256 IS NOT DISTINCT FROM
                      decision.vision_source_frame_sha256
                  AND NEW.vision_source_frame_bytes IS NOT DISTINCT FROM
                      decision.vision_source_frame_bytes
                  AND NEW.vision_observed_game_clock_seconds IS NOT DISTINCT FROM
                      decision.vision_observed_game_clock_seconds
                  AND NEW.vision_aligned_game_clock_seconds IS NOT DISTINCT FROM
                      decision.vision_aligned_game_clock_seconds
                  AND NEW.vision_is_paused IS NOT DISTINCT FROM
                      decision.vision_is_paused
                  AND NEW.vision_radiant_hero_ids_json IS NOT DISTINCT FROM
                      decision.vision_radiant_hero_ids_json
                  AND NEW.vision_dire_hero_ids_json IS NOT DISTINCT FROM
                      decision.vision_dire_hero_ids_json
                  AND NEW.vision_radiant_team_side IS NOT DISTINCT FROM
                      decision.vision_radiant_team_side
                  AND NEW.vision_clock_confidence IS NOT DISTINCT FROM
                      decision.vision_clock_confidence
                  AND NEW.vision_draft_confidence IS NOT DISTINCT FROM
                      decision.vision_draft_confidence
                  AND NEW.vision_screen_state IS NOT DISTINCT FROM
                      decision.vision_screen_state
                  AND NEW.vision_confirmed IS NOT DISTINCT FROM
                      decision.vision_confirmed
                  AND NEW.vision_transport_key IS NOT DISTINCT FROM
                      decision.vision_transport_key
                  AND NEW.vision_transport_at IS NOT DISTINCT FROM
                      decision.vision_transport_at
                  AND NEW.vision_alignment_method IS NOT DISTINCT FROM
                      decision.vision_alignment_method
                  AND NEW.vision_alignment_lag_seconds IS NOT DISTINCT FROM
                      decision.vision_alignment_lag_seconds
                  AND NOT EXISTS (
                      SELECT 1
                      FROM vision_derived_invalidations AS invalidation
                      WHERE (
                          invalidation.dependent_type = 'strategy_decision'
                          AND invalidation.dependent_key = decision.decision_key
                      ) OR (
                          invalidation.dependent_type = 'shadow_order'
                          AND invalidation.dependent_key = NEW.order_key
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'shadow order vision authority is required';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER shadow_order_vision_authority_insert
        BEFORE INSERT ON shadow_orders
        FOR EACH ROW EXECUTE FUNCTION require_shadow_order_vision_authority()
    """))


def _create_vision_draft_anchor_authority() -> None:
    op.execute(sa.text("""
        CREATE FUNCTION require_valid_vision_draft_anchor()
        RETURNS trigger AS $$ BEGIN
            IF NEW.status != 'anchored'
               OR NEW.conflict_at IS NOT NULL
               OR (
                    NEW.radiant_team_side IS NULL
                    AND (
                        NEW.team_side_anchored_at IS NOT NULL
                        OR NEW.team_side_source_frame_ref IS NOT NULL
                    )
               )
               OR (
                    NEW.radiant_team_side IS NOT NULL
                    AND (
                        live_text_timestamp_utc(NEW.team_side_anchored_at) IS NULL
                        OR NULLIF(NEW.team_side_source_frame_ref, '') IS NULL
                    )
               )
            THEN
                RAISE EXCEPTION 'vision draft anchor identity is invalid';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER vision_draft_anchor_insert_valid
        BEFORE INSERT ON vision_draft_anchors
        FOR EACH ROW EXECUTE FUNCTION require_valid_vision_draft_anchor()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION guard_vision_draft_anchor_transition()
        RETURNS trigger AS $$ BEGIN
            IF NOT (
                (
                    OLD.status = 'anchored'
                    AND NEW.status = 'conflict'
                    AND OLD.raybet_match_id IS NOT DISTINCT FROM
                        NEW.raybet_match_id
                    AND OLD.map_number IS NOT DISTINCT FROM NEW.map_number
                    AND OLD.draft_hash IS NOT DISTINCT FROM NEW.draft_hash
                    AND OLD.radiant_hero_ids IS NOT DISTINCT FROM
                        NEW.radiant_hero_ids
                    AND OLD.dire_hero_ids IS NOT DISTINCT FROM NEW.dire_hero_ids
                    AND OLD.radiant_team_side IS NOT DISTINCT FROM
                        NEW.radiant_team_side
                    AND OLD.team_side_anchored_at IS NOT DISTINCT FROM
                        NEW.team_side_anchored_at
                    AND OLD.team_side_source_frame_ref IS NOT DISTINCT FROM
                        NEW.team_side_source_frame_ref
                    AND OLD.anchored_at IS NOT DISTINCT FROM NEW.anchored_at
                    AND OLD.source_frame_ref IS NOT DISTINCT FROM
                        NEW.source_frame_ref
                    AND OLD.conflict_at IS NULL
                    AND live_text_timestamp_utc(NEW.conflict_at) IS NOT NULL
                ) OR (
                    OLD.status = 'anchored'
                    AND NEW.status = 'anchored'
                    AND OLD.raybet_match_id IS NOT DISTINCT FROM
                        NEW.raybet_match_id
                    AND OLD.map_number IS NOT DISTINCT FROM NEW.map_number
                    AND OLD.draft_hash IS NOT DISTINCT FROM NEW.draft_hash
                    AND OLD.radiant_hero_ids IS NOT DISTINCT FROM
                        NEW.radiant_hero_ids
                    AND OLD.dire_hero_ids IS NOT DISTINCT FROM NEW.dire_hero_ids
                    AND OLD.radiant_team_side IS NULL
                    AND NEW.radiant_team_side IN ('team_one', 'team_two')
                    AND OLD.team_side_anchored_at IS NULL
                    AND live_text_timestamp_utc(NEW.team_side_anchored_at)
                        IS NOT NULL
                    AND OLD.team_side_source_frame_ref IS NULL
                    AND NULLIF(NEW.team_side_source_frame_ref, '') IS NOT NULL
                    AND OLD.anchored_at IS NOT DISTINCT FROM NEW.anchored_at
                    AND OLD.source_frame_ref IS NOT DISTINCT FROM
                        NEW.source_frame_ref
                    AND OLD.conflict_at IS NOT DISTINCT FROM NEW.conflict_at
                )
            ) THEN
                RAISE EXCEPTION 'vision draft anchor is immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER vision_draft_anchor_identity_immutable
        BEFORE UPDATE ON vision_draft_anchors
        FOR EACH ROW EXECUTE FUNCTION guard_vision_draft_anchor_transition()
    """))


def _publish_schema_versions() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO live_schema_version (version, applied_at)
            VALUES (
                :version,
                replace(
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                    '_', ':'
                )
            )
            ON CONFLICT (version) DO NOTHING
            """
        ).bindparams(version=LIVE_SCHEMA_VERSION)
    )
    op.execute(
        sa.text(
            """
            UPDATE runtime_schema_version
            SET contract_digest = :contract_digest
            WHERE version = :version
            """
        ).bindparams(
            contract_digest=RUNTIME_CONTRACT_DIGEST,
            version=RUNTIME_SCHEMA_VERSION,
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE runtime_schema_version SET contract_digest = "
            ":contract_digest WHERE version = :version"
        ).bindparams(
            contract_digest=PREVIOUS_RUNTIME_CONTRACT_DIGEST,
            version=RUNTIME_SCHEMA_VERSION,
        )
    )
    op.execute(
        sa.text("DELETE FROM live_schema_version WHERE version = :version")
        .bindparams(version=LIVE_SCHEMA_VERSION)
    )
    op.execute(
        sa.text(
            "DROP TRIGGER vision_draft_anchor_identity_immutable "
            "ON vision_draft_anchors"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER vision_draft_anchor_insert_valid "
            "ON vision_draft_anchors"
        )
    )
    op.execute(sa.text("DROP FUNCTION guard_vision_draft_anchor_transition()"))
    op.execute(sa.text("DROP FUNCTION require_valid_vision_draft_anchor()"))
    op.execute(
        sa.text("DROP TRIGGER shadow_order_vision_authority_insert ON shadow_orders")
    )
    op.execute(
        sa.text("DROP TRIGGER shadow_order_draft_authority_insert ON shadow_orders")
    )
    op.execute(
        sa.text("DROP TRIGGER shadow_orders_signal_identity_guard ON shadow_orders")
    )
    op.execute(sa.text("DROP FUNCTION require_shadow_order_vision_authority()"))
    op.execute(sa.text("DROP FUNCTION require_shadow_order_draft_authority()"))
    op.execute(sa.text("DROP FUNCTION guard_shadow_order_signal_identity()"))
