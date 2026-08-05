"""Persist prematch model, prediction, calibration, and lineage evidence.

Revision ID: 20260805_0023
Revises: 20260805_0022
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0023"
down_revision: str | None = "20260805_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UTC_SUFFIX_CHECK = "({0} LIKE '%Z' OR {0} LIKE '%+00:00')"
_SHA256_CHECK = "{0} ~ '^[0-9a-f]{{64}}$'"
_FINITE_CHECK = (
    "{0} > '-Infinity'::double precision AND {0} < 'Infinity'::double precision"
)


def _utc_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"live_text_timestamp_utc({column}) IS NOT NULL AND "
        + _UTC_SUFFIX_CHECK.format(column)
    )


def _optional_utc_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR (live_text_timestamp_utc({column}) IS NOT NULL AND "
        + _UTC_SUFFIX_CHECK.format(column)
        + ")"
    )


def _sha256_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(_SHA256_CHECK.format(column))


def _optional_sha256_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IS NULL OR " + _SHA256_CHECK.format(column))


def _finite_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(_FINITE_CHECK.format(column))


def _optional_finite_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR (" + _FINITE_CHECK.format(column) + ")"
    )


def _json_object_check(column: str, *, optional: bool = False) -> sa.CheckConstraint:
    expression = f"jsonb_typeof({column}::jsonb) = 'object'"
    if optional:
        expression = f"{column} IS NULL OR ({expression})"
    return sa.CheckConstraint(expression)


def upgrade() -> None:
    op.create_table(
        "prematch_model_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("artifact_version", sa.Text(), nullable=False),
        sa.Column("model_kind", sa.Text(), nullable=False),
        sa.Column("availability_mode", sa.Text(), nullable=False),
        sa.Column("training_cutoff", sa.Text(), nullable=False),
        sa.Column("feature_schema_hash", sa.Text(), nullable=False),
        sa.Column("training_input_hash", sa.Text(), nullable=False),
        sa.Column("model_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.Column("metrics_json", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(trim(model_version)) > 0"),
        sa.CheckConstraint("length(trim(artifact_version)) > 0"),
        sa.CheckConstraint(
            "model_kind IN ('team_only', 'team_plus_draft', "
            "'team_plus_rosh', 'team_plus_draft_rosh')"
        ),
        sa.CheckConstraint(
            "availability_mode IN ('reconstructed_walk_forward', 'prospective')"
        ),
        sa.CheckConstraint("status IN ('trained', 'insufficient_evidence')"),
        sa.CheckConstraint("run_id = model_hash"),
        sa.CheckConstraint("artifact_json::jsonb ->> 'model_hash' = model_hash"),
        sa.CheckConstraint("artifact_json::jsonb ->> 'model_version' = model_version"),
        sa.CheckConstraint(
            "artifact_json::jsonb ->> 'artifact_version' = artifact_version"
        ),
        sa.CheckConstraint("artifact_json::jsonb ->> 'model_kind' = model_kind"),
        sa.CheckConstraint(
            "artifact_json::jsonb ->> 'availability_mode' = availability_mode"
        ),
        sa.CheckConstraint(
            "artifact_json::jsonb ->> 'training_cutoff' = training_cutoff"
        ),
        sa.CheckConstraint(
            "artifact_json::jsonb ->> 'feature_schema_hash' = feature_schema_hash"
        ),
        sa.CheckConstraint(
            "artifact_json::jsonb ->> 'training_input_hash' = training_input_hash"
        ),
        sa.CheckConstraint("artifact_json::jsonb ->> 'status' = status"),
        _sha256_check("run_id"),
        _sha256_check("feature_schema_hash"),
        _sha256_check("training_input_hash"),
        _sha256_check("model_hash"),
        _json_object_check("artifact_json"),
        _json_object_check("metrics_json", optional=True),
        _utc_check("training_cutoff"),
        _utc_check("created_at"),
    )
    op.create_index(
        "idx_prematch_model_runs_kind_cutoff",
        "prematch_model_runs",
        ["model_kind", "availability_mode", sa.text("training_cutoff DESC")],
    )

    op.create_table(
        "prematch_calibration_artifacts",
        sa.Column("calibration_key", sa.Text(), primary_key=True),
        sa.Column("model_kind", sa.Text(), nullable=False),
        sa.Column(
            "model_hash",
            sa.Text(),
            sa.ForeignKey("prematch_model_runs.model_hash"),
            nullable=False,
        ),
        sa.Column("calibration_version", sa.Text(), nullable=False),
        sa.Column("fit_cutoff", sa.Text()),
        sa.Column("evaluation_cutoff", sa.Text(), nullable=False),
        sa.Column("fit_support", sa.Integer(), nullable=False),
        sa.Column("evaluation_support", sa.Integer(), nullable=False),
        sa.Column("parameters_json", sa.Text()),
        sa.Column("metrics_json", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("calibration_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "model_kind IN ('team_only', 'team_plus_draft', "
            "'team_plus_rosh', 'team_plus_draft_rosh')"
        ),
        sa.CheckConstraint("length(trim(calibration_version)) > 0"),
        sa.CheckConstraint("fit_support >= 0"),
        sa.CheckConstraint("evaluation_support >= 0"),
        sa.CheckConstraint(
            "fit_cutoff IS NULL OR live_text_timestamp_utc(fit_cutoff) "
            "<= live_text_timestamp_utc(evaluation_cutoff)"
        ),
        sa.CheckConstraint(
            "status IN ('unsupported', 'failed', 'provisional', "
            "'reconstructed_only', 'shadow_collecting', 'passed')"
        ),
        sa.CheckConstraint("fit_cutoff IS NOT NULL OR status = 'unsupported'"),
        sa.CheckConstraint(
            "(status IN ('unsupported', 'failed')) = (parameters_json IS NULL)"
        ),
        sa.CheckConstraint(
            "parameters_json IS NULL OR (("
            "parameters_json::jsonb - ARRAY['a', 'b'] = '{}'::jsonb AND "
            "jsonb_typeof(parameters_json::jsonb -> 'a') = 'number' AND "
            "jsonb_typeof(parameters_json::jsonb -> 'b') = 'number' AND "
            "(parameters_json::jsonb ->> 'a')::double precision "
            "> '-Infinity'::double precision AND "
            "(parameters_json::jsonb ->> 'a')::double precision "
            "< 'Infinity'::double precision AND "
            "(parameters_json::jsonb ->> 'b')::double precision "
            "> '-Infinity'::double precision AND "
            "(parameters_json::jsonb ->> 'b')::double precision "
            "< 'Infinity'::double precision"
            ") IS TRUE)"
        ),
        sa.CheckConstraint(
            "((jsonb_typeof(artifact_json::jsonb -> 'parameters') = 'null' "
            "AND parameters_json IS NULL) OR "
            "(jsonb_typeof(artifact_json::jsonb -> 'parameters') = 'object' "
            "AND parameters_json IS NOT NULL AND "
            "artifact_json::jsonb -> 'parameters' = parameters_json::jsonb)) "
            "IS TRUE"
        ),
        sa.CheckConstraint("artifact_json::jsonb ->> 'model_kind' = model_kind"),
        sa.CheckConstraint(
            "artifact_json::jsonb ->> 'calibration_version' = calibration_version"
        ),
        sa.CheckConstraint(
            "artifact_json::jsonb ->> 'calibration_hash' = calibration_hash"
        ),
        sa.CheckConstraint("artifact_json::jsonb ->> 'input_hash' = input_hash"),
        sa.CheckConstraint("artifact_json::jsonb ->> 'status' = status"),
        sa.CheckConstraint(
            "artifact_json::jsonb ->> 'calibration_cutoff' = evaluation_cutoff"
        ),
        sa.CheckConstraint(
            "(artifact_json::jsonb ->> 'fit_cutoff') IS NOT DISTINCT FROM fit_cutoff"
        ),
        sa.CheckConstraint(
            "(artifact_json::jsonb ->> 'fit_support')::integer = fit_support"
        ),
        sa.CheckConstraint(
            "(artifact_json::jsonb ->> 'evaluation_support')::integer = "
            "evaluation_support"
        ),
        _sha256_check("calibration_key"),
        _sha256_check("model_hash"),
        _sha256_check("input_hash"),
        _sha256_check("calibration_hash"),
        _json_object_check("parameters_json", optional=True),
        _json_object_check("metrics_json"),
        _json_object_check("artifact_json"),
        _optional_utc_check("fit_cutoff"),
        _utc_check("evaluation_cutoff"),
        _utc_check("created_at"),
    )
    op.create_index(
        "idx_prematch_calibration_model_cutoff",
        "prematch_calibration_artifacts",
        ["model_hash", sa.text("evaluation_cutoff DESC")],
    )

    op.create_table(
        "prematch_predictions",
        sa.Column(
            "prediction_id",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("prematch_model_runs.run_id"),
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
        sa.Column("artifact_fingerprint", sa.Text(), nullable=False),
        sa.Column("dependency_fingerprint", sa.Text(), nullable=False),
        sa.Column("dependency_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "calibration_hash",
            sa.Text(),
            sa.ForeignKey("prematch_calibration_artifacts.calibration_hash"),
        ),
        sa.Column("team_base_probability", sa.Double(), nullable=False),
        sa.Column("raw_probability", sa.Double()),
        sa.Column("calibrated_probability", sa.Double()),
        sa.Column("parameter_uncertainty", sa.Double()),
        sa.Column("draft_logit_delta", sa.Double()),
        sa.Column("rosh_logit_delta", sa.Double()),
        sa.Column("cluster_logit_delta", sa.Double()),
        sa.Column("total_adjustment", sa.Double()),
        sa.Column("coverage", sa.Double(), nullable=False),
        sa.Column("support", sa.Integer(), nullable=False),
        sa.Column("prediction_json", sa.Text(), nullable=False),
        sa.Column("eventual_radiant_win", sa.SmallInteger()),
        sa.Column("result_usable_at", sa.Text()),
        sa.Column("settled_at", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("run_id", "match_id"),
        sa.CheckConstraint("length(trim(cutoff_source)) > 0"),
        sa.CheckConstraint("dependency_revision >= 1"),
        sa.CheckConstraint(
            "team_base_probability > 0.0 AND team_base_probability < 1.0"
        ),
        sa.CheckConstraint(
            "raw_probability IS NULL OR raw_probability BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "calibrated_probability IS NULL OR "
            "calibrated_probability BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "parameter_uncertainty IS NULL OR parameter_uncertainty >= 0.0"
        ),
        sa.CheckConstraint("coverage BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("support >= 0"),
        sa.CheckConstraint(
            "status IN ('predicted', 'insufficient_evidence', 'failed', 'settled')"
        ),
        sa.CheckConstraint(
            "(status = 'settled') = (eventual_radiant_win IS NOT NULL) AND "
            "(status = 'settled') = (result_usable_at IS NOT NULL) AND "
            "(status = 'settled') = (settled_at IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(status IN ('predicted', 'settled')) = (raw_probability IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(calibrated_probability IS NULL) = (calibration_hash IS NULL)"
        ),
        sa.CheckConstraint(
            "eventual_radiant_win IS NULL OR eventual_radiant_win IN (0, 1)"
        ),
        sa.CheckConstraint(
            "result_usable_at IS NULL OR settled_at IS NULL OR "
            "live_text_timestamp_utc(result_usable_at) "
            "<= live_text_timestamp_utc(settled_at)"
        ),
        _sha256_check("input_snapshot_hash"),
        _sha256_check("artifact_fingerprint"),
        _sha256_check("dependency_fingerprint"),
        _optional_sha256_check("calibration_hash"),
        _finite_check("team_base_probability"),
        _optional_finite_check("raw_probability"),
        _optional_finite_check("calibrated_probability"),
        _optional_finite_check("parameter_uncertainty"),
        _optional_finite_check("draft_logit_delta"),
        _optional_finite_check("rosh_logit_delta"),
        _optional_finite_check("cluster_logit_delta"),
        _optional_finite_check("total_adjustment"),
        _finite_check("coverage"),
        _json_object_check("prediction_json"),
        _optional_utc_check("result_usable_at"),
        _optional_utc_check("settled_at"),
        _utc_check("prediction_cutoff"),
        _utc_check("created_at"),
    )
    op.create_index(
        "idx_prematch_predictions_match_cutoff",
        "prematch_predictions",
        ["match_id", sa.text("prediction_cutoff DESC")],
    )
    op.create_index(
        "idx_prematch_predictions_run",
        "prematch_predictions",
        ["run_id", "match_id"],
    )

    op.create_table(
        "prematch_prediction_validations",
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
            ["prematch_predictions.run_id", "prematch_predictions.match_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("dependency_revision >= 1"),
        sa.CheckConstraint("length(trim(validation_version)) > 0"),
        _sha256_check("input_snapshot_hash"),
        _sha256_check("artifact_fingerprint"),
        _sha256_check("dependency_fingerprint"),
        _utc_check("validated_at"),
    )
    op.create_index(
        "idx_prematch_prediction_validations_fingerprint",
        "prematch_prediction_validations",
        ["validation_version", "dependency_fingerprint"],
    )

    op.create_table(
        "prematch_lineage_revisions",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("dependency_revision", sa.BigInteger(), nullable=False),
        sa.Column("artifact_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton = 1"),
        sa.CheckConstraint("dependency_revision >= 1"),
        sa.CheckConstraint("artifact_revision >= 1"),
        _utc_check("updated_at"),
    )
    op.create_table(
        "prematch_lineage_changes",
        sa.Column("dependency_revision", sa.BigInteger(), primary_key=True),
        sa.Column("affected_from_unix", sa.BigInteger()),
        sa.Column("source_relation", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.Text(), nullable=False),
        sa.CheckConstraint("dependency_revision >= 1"),
        sa.CheckConstraint("affected_from_unix IS NULL OR affected_from_unix > 0"),
        sa.CheckConstraint("length(trim(source_relation)) > 0"),
        sa.CheckConstraint(
            "operation IN ('INSERT', 'UPDATE', 'DELETE', 'REPAIR', 'INITIALIZE')"
        ),
        _utc_check("changed_at"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO prematch_lineage_revisions (
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
            INSERT INTO prematch_lineage_changes (
                dependency_revision, affected_from_unix,
                source_relation, operation, changed_at
            )
            SELECT 1, NULL, '__tracking__', 'INITIALIZE', updated_at
              FROM prematch_lineage_revisions
             WHERE singleton = 1
            """
        )
    )


def downgrade() -> None:
    op.drop_table("prematch_prediction_validations")
    op.drop_table("prematch_predictions")
    op.drop_table("prematch_calibration_artifacts")
    op.drop_table("prematch_model_runs")
    op.drop_table("prematch_lineage_changes")
    op.drop_table("prematch_lineage_revisions")
