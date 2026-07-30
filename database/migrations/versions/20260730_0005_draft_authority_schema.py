"""Create prospective draft deployment authority tables.

Revision ID: 20260730_0005
Revises: 20260730_0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0005"
down_revision: str | None = "20260730_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "draft_deployment_revisions",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("artifact_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton = 1"),
        sa.CheckConstraint("artifact_revision >= 1"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO draft_deployment_revisions (
                singleton, artifact_revision, updated_at
            ) VALUES (
                1, 1,
                replace(
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                    '_', ':'
                )
            )
            """
        )
    )
    op.create_table(
        "draft_model_artifacts",
        sa.Column("model_hash", sa.Text(), primary_key=True),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("model_kind", sa.Text(), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("training_cutoff", sa.Text(), nullable=False),
        sa.Column("feature_schema_hash", sa.Text(), nullable=False),
        sa.Column("training_input_hash", sa.Text(), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(model_hash) = 64"),
        sa.CheckConstraint("model_kind = 'pure_draft'"),
        sa.CheckConstraint("horizon_minutes IN (10, 20, 30, 40, 50)"),
        sa.CheckConstraint("length(feature_schema_hash) = 64"),
        sa.CheckConstraint("length(training_input_hash) = 64"),
        sa.CheckConstraint("artifact_json::jsonb IS NOT NULL"),
    )
    op.create_table(
        "draft_calibration_artifacts",
        sa.Column("calibration_hash", sa.Text(), primary_key=True),
        sa.Column("model_hash", sa.Text(), nullable=False),
        sa.Column("calibration_version", sa.Text(), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("evidence_mode", sa.Text(), nullable=False),
        sa.Column("support", sa.Integer(), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(calibration_hash) = 64"),
        sa.CheckConstraint("length(model_hash) = 64"),
        sa.CheckConstraint("horizon_minutes IN (10, 20, 30, 40, 50)"),
        sa.CheckConstraint(
            "evidence_mode IN ('reconstructed_walk_forward', 'prospective')"
        ),
        sa.CheckConstraint("support >= 0"),
        sa.CheckConstraint("artifact_json::jsonb IS NOT NULL"),
    )
    op.create_table(
        "draft_deployment_bundles",
        sa.Column("deployment_key", sa.Text(), primary_key=True),
        sa.Column("model_hashes_json", sa.Text(), nullable=False),
        sa.Column("calibration_hashes_json", sa.Text(), nullable=False),
        sa.Column("training_cutoff", sa.Text(), nullable=False),
        sa.Column("dependency_fingerprint", sa.Text(), nullable=False),
        sa.Column("dependency_revision", sa.BigInteger(), nullable=False),
        sa.Column("evidence_mode", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(deployment_key) = 64"),
        sa.CheckConstraint("model_hashes_json::jsonb IS NOT NULL"),
        sa.CheckConstraint("calibration_hashes_json::jsonb IS NOT NULL"),
        sa.CheckConstraint("length(dependency_fingerprint) = 64"),
        sa.CheckConstraint("dependency_revision >= 1"),
        sa.CheckConstraint(
            "evidence_mode IN ('reconstructed_walk_forward', 'prospective')"
        ),
    )
    _create_deployment_triggers()
    _create_prospective_tables()
    _create_prospective_triggers()
    _create_authority_view()


def _create_deployment_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION require_draft_deployment_bundle_authority()
            RETURNS trigger AS $$
            BEGIN
                IF jsonb_typeof(NEW.model_hashes_json::jsonb) != 'object'
                    OR jsonb_typeof(NEW.calibration_hashes_json::jsonb) != 'object'
                    OR (
                        SELECT COUNT(*)
                        FROM jsonb_each(NEW.model_hashes_json::jsonb)
                    ) != 5
                    OR (
                        SELECT COUNT(*)
                        FROM jsonb_each(NEW.calibration_hashes_json::jsonb)
                    ) != 5
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_each(NEW.model_hashes_json::jsonb) AS reference
                        WHERE reference.key NOT IN ('10', '20', '30', '40', '50')
                           OR jsonb_typeof(reference.value) != 'string'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_each(
                            NEW.calibration_hashes_json::jsonb
                        ) AS reference
                        WHERE reference.key NOT IN ('10', '20', '30', '40', '50')
                           OR jsonb_typeof(reference.value) != 'string'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_each_text(
                            NEW.model_hashes_json::jsonb
                        ) AS reference
                        LEFT JOIN draft_model_artifacts AS model
                          ON model.model_hash = reference.value
                         AND model.horizon_minutes = reference.key::integer
                        WHERE model.model_hash IS NULL
                           OR model.training_cutoff != NEW.training_cutoff
                           OR model.artifact_json::jsonb ->> 'artifact_version'
                              != 'draft-model-artifact-v2'
                           OR jsonb_typeof(
                               model.artifact_json::jsonb -> 'training_corpus'
                           ) != 'array'
                           OR jsonb_array_length(
                               model.artifact_json::jsonb -> 'training_corpus'
                           ) != (model.artifact_json::jsonb ->> 'support')::integer
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_each_text(
                            NEW.calibration_hashes_json::jsonb
                        ) AS reference
                        LEFT JOIN draft_calibration_artifacts AS calibration
                          ON calibration.calibration_hash = reference.value
                         AND calibration.horizon_minutes = reference.key::integer
                        WHERE calibration.calibration_hash IS NULL
                           OR calibration.evidence_mode != NEW.evidence_mode
                           OR calibration.model_hash != (
                               NEW.model_hashes_json::jsonb ->> reference.key
                           )
                    )
                THEN
                    RAISE EXCEPTION
                        'draft deployment bundle authority is required';
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
            CREATE TRIGGER draft_deployment_bundle_authority_insert
            BEFORE INSERT ON draft_deployment_bundles
            FOR EACH ROW
            EXECUTE FUNCTION require_draft_deployment_bundle_authority()
            """
        )
    )
    for table, prefix, message in (
        (
            "draft_model_artifacts",
            "draft_model_artifacts_immutable",
            "draft model artifact is immutable",
        ),
        (
            "draft_calibration_artifacts",
            "draft_calibration_artifacts_immutable",
            "draft calibration artifact is immutable",
        ),
        (
            "draft_deployment_bundles",
            "draft_deployment_bundles_immutable",
            "draft deployment bundle is immutable",
        ),
    ):
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
            CREATE FUNCTION bump_draft_deployment_revision()
            RETURNS trigger AS $$
            BEGIN
                UPDATE draft_deployment_revisions
                SET artifact_revision = artifact_revision + 1,
                    updated_at = replace(
                        to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                                'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                        '_', ':'
                    )
                WHERE singleton = 1;
                RETURN COALESCE(NEW, OLD);
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    for table, label in (
        ("draft_model_artifacts", "model"),
        ("draft_calibration_artifacts", "calibration"),
        ("draft_deployment_bundles", "bundle"),
    ):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER draft_deployment_revision_{label}_{operation.lower()}
                    AFTER {operation} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION bump_draft_deployment_revision()
                    """
                )
            )


def _create_prospective_tables() -> None:
    op.create_table(
        "prospective_draft_curves",
        sa.Column("curve_key", sa.Text(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("strict_mapping_id", sa.BigInteger(), nullable=False),
        sa.Column("lineup_hash", sa.Text(), nullable=False),
        sa.Column("radiant_hero_ids_json", sa.Text(), nullable=False),
        sa.Column("dire_hero_ids_json", sa.Text(), nullable=False),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column("first_usable_at", sa.Text(), nullable=False),
        sa.Column("availability_mode", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("radiant_team_side", sa.Text()),
        sa.Column("anchor_draft_hash", sa.Text()),
        sa.Column("anchor_source_frame_ref", sa.Text()),
        sa.Column("anchor_anchored_at", sa.Text()),
        sa.Column("anchor_team_side_source_frame_ref", sa.Text()),
        sa.Column("anchor_team_side_anchored_at", sa.Text()),
        sa.Column("deployment_key", sa.Text()),
        sa.Column("target_snapshot_hash", sa.Text()),
        sa.Column("feature_snapshot_json", sa.Text()),
        sa.Column("feature_dependency_fingerprint", sa.Text()),
        sa.Column("feature_dependency_revision", sa.BigInteger()),
        sa.UniqueConstraint(
            "raybet_match_id",
            "map_number",
            "strict_mapping_id",
            "lineup_hash",
            "first_usable_at",
            "curve_key",
        ),
        sa.CheckConstraint("length(curve_key) = 64"),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint("strict_mapping_id > 0"),
        sa.CheckConstraint("length(lineup_hash) = 64"),
        sa.CheckConstraint("availability_mode = 'prospective'"),
        sa.CheckConstraint(
            "radiant_team_side IS NULL OR "
            "radiant_team_side IN ('team_one', 'team_two')"
        ),
        sa.CheckConstraint(
            "anchor_draft_hash IS NULL OR length(anchor_draft_hash) = 64"
        ),
        sa.CheckConstraint(
            "deployment_key IS NULL OR length(deployment_key) = 64"
        ),
        sa.CheckConstraint(
            "target_snapshot_hash IS NULL OR length(target_snapshot_hash) = 64"
        ),
        sa.CheckConstraint(
            "feature_snapshot_json IS NULL OR "
            "feature_snapshot_json::jsonb IS NOT NULL"
        ),
        sa.CheckConstraint(
            "feature_dependency_fingerprint IS NULL OR "
            "length(feature_dependency_fingerprint) = 64"
        ),
        sa.CheckConstraint(
            "feature_dependency_revision IS NULL OR feature_dependency_revision >= 1"
        ),
    )
    op.create_index(
        "idx_prospective_draft_curve_target",
        "prospective_draft_curves",
        [
            "raybet_match_id",
            "map_number",
            "strict_mapping_id",
            "lineup_hash",
            "first_usable_at",
        ],
    )
    op.create_table(
        "prospective_draft_landmarks",
        sa.Column("landmark_key", sa.Text(), primary_key=True),
        sa.Column(
            "curve_key",
            sa.Text(),
            sa.ForeignKey("prospective_draft_curves.curve_key"),
            nullable=False,
        ),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("radiant_probability", sa.Double(), nullable=False),
        sa.Column("scaling_edge", sa.Double(), nullable=False),
        sa.Column("synergy_edge", sa.Double(), nullable=False),
        sa.Column("quality", sa.Double(), nullable=False),
        sa.Column("validation_status", sa.Text(), nullable=False),
        sa.Column("support", sa.Integer(), nullable=False),
        sa.Column("calibration_ref", sa.Text(), nullable=False),
        sa.Column("input_refs_json", sa.Text(), nullable=False),
        sa.Column("uncertainty", sa.Double()),
        sa.Column("validation_reason", sa.Text()),
        sa.Column("feature_hash", sa.Text(), nullable=False),
        sa.Column("model_hash", sa.Text(), nullable=False),
        sa.Column("calibration_hash", sa.Text(), nullable=False),
        sa.Column("global_calibration_passed", sa.SmallInteger(), nullable=False),
        sa.Column("global_gate_ref", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("model_kind", sa.Text(), nullable=False),
        sa.Column("availability_mode", sa.Text(), nullable=False),
        sa.Column("input_snapshot_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("raw_radiant_probability", sa.Double()),
        sa.Column("deployment_key", sa.Text()),
        sa.Column("model_input_hash", sa.Text()),
        sa.Column("raw_uncertainty", sa.Double()),
        sa.UniqueConstraint("curve_key", "horizon_minutes"),
        sa.CheckConstraint("length(landmark_key) = 64"),
        sa.CheckConstraint("horizon_minutes IN (10, 20, 30, 40, 50)"),
        sa.CheckConstraint("radiant_probability BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("quality BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint(
            "validation_status IN ('passed', 'failed', 'insufficient_evidence')"
        ),
        sa.CheckConstraint("support >= 0"),
        sa.CheckConstraint("uncertainty IS NULL OR uncertainty BETWEEN 0.0 AND 0.5"),
        sa.CheckConstraint("length(feature_hash) = 64"),
        sa.CheckConstraint("length(model_hash) = 64"),
        sa.CheckConstraint("length(calibration_hash) = 64"),
        sa.CheckConstraint("global_calibration_passed IN (0, 1)"),
        sa.CheckConstraint("model_kind = 'pure_draft'"),
        sa.CheckConstraint("availability_mode = 'prospective'"),
        sa.CheckConstraint("length(input_snapshot_hash) = 64"),
        sa.CheckConstraint(
            "raw_radiant_probability IS NULL OR "
            "raw_radiant_probability BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "deployment_key IS NULL OR length(deployment_key) = 64"
        ),
        sa.CheckConstraint(
            "model_input_hash IS NULL OR length(model_input_hash) = 64"
        ),
        sa.CheckConstraint(
            "raw_uncertainty IS NULL OR raw_uncertainty BETWEEN 0.0 AND 0.5"
        ),
    )


def _create_prospective_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION require_prospective_draft_curve_authority()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.radiant_team_side IS NULL
                    OR NEW.anchor_draft_hash IS NULL
                    OR NULLIF(NEW.anchor_source_frame_ref, '') IS NULL
                    OR NEW.anchor_anchored_at IS NULL
                    OR NULLIF(NEW.anchor_team_side_source_frame_ref, '') IS NULL
                    OR NEW.anchor_team_side_anchored_at IS NULL
                    OR NEW.deployment_key IS NULL
                    OR NEW.target_snapshot_hash IS NULL
                    OR NEW.feature_snapshot_json IS NULL
                    OR NEW.feature_dependency_fingerprint IS NULL
                    OR NEW.feature_dependency_revision IS NULL
                    OR live_text_timestamp_utc(NEW.prediction_cutoff) IS NULL
                    OR live_text_timestamp_utc(NEW.first_usable_at) IS NULL
                    OR live_text_timestamp_utc(NEW.created_at) IS NULL
                    OR live_text_timestamp_utc(NEW.anchor_anchored_at) IS NULL
                    OR live_text_timestamp_utc(
                        NEW.anchor_team_side_anchored_at
                    ) IS NULL
                    OR live_text_timestamp_utc(NEW.anchor_anchored_at) >
                       live_text_timestamp_utc(NEW.prediction_cutoff)
                    OR live_text_timestamp_utc(
                        NEW.anchor_team_side_anchored_at
                    ) > live_text_timestamp_utc(NEW.prediction_cutoff)
                    OR live_text_timestamp_utc(NEW.prediction_cutoff) >
                       live_text_timestamp_utc(NEW.first_usable_at)
                    OR live_text_timestamp_utc(NEW.first_usable_at) >
                       live_text_timestamp_utc(NEW.created_at)
                    OR NOT EXISTS (
                        SELECT 1
                        FROM draft_deployment_bundles AS deployment
                        WHERE deployment.deployment_key = NEW.deployment_key
                          AND live_text_timestamp_utc(deployment.created_at) <=
                              live_text_timestamp_utc(NEW.prediction_cutoff)
                    )
                THEN
                    RAISE EXCEPTION
                        'prospective draft curve authority is required';
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
            CREATE TRIGGER prospective_draft_curve_authority_insert
            BEFORE INSERT ON prospective_draft_curves
            FOR EACH ROW
            EXECUTE FUNCTION require_prospective_draft_curve_authority()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION require_prospective_draft_landmark_authority()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.raw_radiant_probability IS NULL
                    OR NEW.deployment_key IS NULL
                    OR NEW.model_input_hash IS NULL
                    OR NOT EXISTS (
                        SELECT 1
                        FROM prospective_draft_curves AS curve
                        WHERE curve.curve_key = NEW.curve_key
                          AND curve.deployment_key = NEW.deployment_key
                    )
                    OR NOT EXISTS (
                        SELECT 1
                        FROM draft_model_artifacts AS model
                        WHERE model.model_hash = NEW.model_hash
                          AND model.horizon_minutes = NEW.horizon_minutes
                          AND model.model_version = NEW.model_version
                          AND model.model_kind = NEW.model_kind
                          AND model.feature_schema_hash = NEW.feature_hash
                    )
                    OR NOT EXISTS (
                        SELECT 1
                        FROM draft_calibration_artifacts AS calibration
                        WHERE calibration.calibration_hash = NEW.calibration_hash
                          AND calibration.model_hash = NEW.model_hash
                          AND calibration.horizon_minutes = NEW.horizon_minutes
                          AND calibration.support = NEW.support
                    )
                    OR NOT EXISTS (
                        SELECT 1
                        FROM draft_deployment_bundles AS deployment
                        WHERE deployment.deployment_key = NEW.deployment_key
                          AND deployment.model_hashes_json::jsonb ->>
                              NEW.horizon_minutes::text = NEW.model_hash
                          AND deployment.calibration_hashes_json::jsonb ->>
                              NEW.horizon_minutes::text = NEW.calibration_hash
                    )
                THEN
                    RAISE EXCEPTION
                        'prospective draft landmark authority is required';
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
            CREATE TRIGGER prospective_draft_landmark_authority_insert
            BEFORE INSERT ON prospective_draft_landmarks
            FOR EACH ROW
            EXECUTE FUNCTION require_prospective_draft_landmark_authority()
            """
        )
    )
    for table, prefix, message in (
        (
            "prospective_draft_curves",
            "prospective_draft_curves_immutable",
            "prospective draft curve is immutable",
        ),
        (
            "prospective_draft_landmarks",
            "prospective_draft_landmarks_immutable",
            "prospective draft landmark is immutable",
        ),
    ):
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


def _create_authority_view() -> None:
    op.execute(
        sa.text(
            """
            CREATE VIEW prospective_draft_landmark_authority AS
            SELECT curve.curve_key,
                   'prospective-draft:' || curve.curve_key AS source_ref,
                   curve.raybet_match_id,
                   curve.map_number,
                   curve.strict_mapping_id,
                   curve.radiant_hero_ids_json,
                   curve.dire_hero_ids_json,
                   curve.first_usable_at,
                   curve.radiant_team_side,
                   curve.deployment_key,
                   curve.target_snapshot_hash,
                   curve.feature_dependency_revision,
                   landmark.landmark_key,
                   landmark.horizon_minutes,
                   'radiant_win'::text AS landmark_target,
                   landmark.radiant_probability,
                   landmark.quality,
                   landmark.uncertainty,
                   landmark.support,
                   landmark.feature_hash,
                   landmark.model_hash,
                   landmark.calibration_hash,
                   landmark.model_version,
                   landmark.global_gate_ref,
                   landmark.input_snapshot_hash
            FROM prospective_draft_curves AS curve
            JOIN prospective_draft_landmarks AS landmark
              ON landmark.curve_key = curve.curve_key
            JOIN draft_model_artifacts AS model
              ON model.model_hash = landmark.model_hash
             AND model.model_version = landmark.model_version
             AND model.model_kind = 'pure_draft'
             AND model.horizon_minutes = landmark.horizon_minutes
             AND model.feature_schema_hash = landmark.feature_hash
            JOIN draft_calibration_artifacts AS calibration
              ON calibration.calibration_hash = landmark.calibration_hash
             AND calibration.model_hash = landmark.model_hash
             AND calibration.horizon_minutes = landmark.horizon_minutes
             AND calibration.support = landmark.support
            JOIN draft_deployment_bundles AS deployment
              ON deployment.deployment_key = curve.deployment_key
            WHERE curve.availability_mode = 'prospective'
              AND landmark.validation_status = 'passed'
              AND landmark.global_calibration_passed = 1
              AND landmark.model_kind = 'pure_draft'
              AND landmark.availability_mode = 'prospective'
              AND landmark.deployment_key = curve.deployment_key
              AND deployment.model_hashes_json::jsonb ->>
                  landmark.horizon_minutes::text = landmark.model_hash
              AND deployment.calibration_hashes_json::jsonb ->>
                  landmark.horizon_minutes::text = landmark.calibration_hash
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW prospective_draft_landmark_authority"))
    op.drop_table("prospective_draft_landmarks")
    op.drop_table("prospective_draft_curves")
    op.drop_table("draft_deployment_bundles")
    op.drop_table("draft_calibration_artifacts")
    op.drop_table("draft_model_artifacts")
    op.drop_table("draft_deployment_revisions")

    op.execute(sa.text("DROP FUNCTION require_prospective_draft_landmark_authority()"))
    op.execute(sa.text("DROP FUNCTION require_prospective_draft_curve_authority()"))
    op.execute(sa.text("DROP FUNCTION bump_draft_deployment_revision()"))
    op.execute(sa.text("DROP FUNCTION require_draft_deployment_bundle_authority()"))
