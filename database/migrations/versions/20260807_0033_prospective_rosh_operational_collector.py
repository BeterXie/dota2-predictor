"""Add operational R.O.S.H. collection attempts and causal audits.

Revision ID: 20260807_0033
Revises: 20260807_0032
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0033"
down_revision: str | None = "20260807_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SHA256 = "{0} ~ '^[0-9a-f]{{64}}$'"


def _sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(_SHA256.format(column))


def _optional_sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IS NULL OR ({_SHA256.format(column)})")


def _utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"live_text_timestamp_utc({column}) IS NOT NULL")


def _optional_utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR live_text_timestamp_utc({column}) IS NOT NULL"
    )


def upgrade() -> None:
    op.create_table(
        "prospective_rosh_collection_attempts",
        sa.Column("attempt_hash", sa.Text(), primary_key=True),
        sa.Column(
            "candidate_hash",
            sa.Text(),
            sa.ForeignKey("prospective_rosh_candidates.candidate_hash"),
            nullable=False,
        ),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
        ),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempted_at", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("missing_reason", sa.Text()),
        sa.Column("retry_at", sa.Text()),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("request_artifacts_json", sa.Text()),
        sa.Column("request_manifest_hash", sa.Text()),
        sa.Column("response_artifacts_json", sa.Text()),
        sa.Column("response_manifest_hash", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("candidate_hash", "match_id", "attempt_number"),
        sa.CheckConstraint("match_id > 0"),
        sa.CheckConstraint("attempt_number > 0"),
        sa.CheckConstraint(
            "status IN ('retry_scheduled', 'terminal_failure', "
            "'paired_stored', 'p0_only_stored', 'idempotency_unchanged')"
        ),
        sa.CheckConstraint("missing_reason IS NULL OR length(trim(missing_reason)) > 0"),
        sa.CheckConstraint(
            "(status='retry_scheduled' AND terminal IS FALSE AND retry_at IS NOT NULL) "
            "OR (status<>'retry_scheduled' AND terminal IS TRUE AND retry_at IS NULL)"
        ),
        sa.CheckConstraint(
            "(request_artifacts_json IS NULL) = (request_manifest_hash IS NULL)"
        ),
        sa.CheckConstraint(
            "(response_artifacts_json IS NULL) = (response_manifest_hash IS NULL)"
        ),
        sa.CheckConstraint(
            "request_artifacts_json IS NULL OR "
            "(jsonb_typeof(request_artifacts_json::jsonb)='array' AND "
            "jsonb_array_length(request_artifacts_json::jsonb)=3)"
        ),
        sa.CheckConstraint(
            "response_artifacts_json IS NULL OR "
            "(jsonb_typeof(response_artifacts_json::jsonb)='array' AND "
            "jsonb_array_length(response_artifacts_json::jsonb)=3)"
        ),
        _sha256("attempt_hash"),
        _sha256("candidate_hash"),
        _optional_sha256("request_manifest_hash"),
        _optional_sha256("response_manifest_hash"),
        _utc("prediction_cutoff"),
        _utc("attempted_at"),
        _optional_utc("retry_at"),
        _utc("created_at"),
    )
    op.create_index(
        "idx_prospective_rosh_collection_retry",
        "prospective_rosh_collection_attempts",
        ["candidate_hash", "terminal", "retry_at", "match_id"],
    )

    op.create_table(
        "prospective_rosh_causal_audits",
        sa.Column("audit_hash", sa.Text(), primary_key=True),
        sa.Column(
            "prediction_hash",
            sa.Text(),
            sa.ForeignKey("prospective_rosh_shadow_predictions.prediction_hash"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "settlement_hash",
            sa.Text(),
            sa.ForeignKey("prospective_rosh_shadow_settlements.settlement_hash"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
        ),
        sa.Column("authoritative_actual_start", sa.Text(), nullable=False),
        sa.Column("prediction_created_at", sa.Text(), nullable=False),
        sa.Column("causal_eligible", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.Text()),
        sa.Column("audited_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("match_id > 0"),
        sa.CheckConstraint(
            "(causal_eligible IS TRUE AND exclusion_reason IS NULL) OR "
            "(causal_eligible IS FALSE AND "
            "exclusion_reason='prediction_not_before_actual_start')"
        ),
        _sha256("audit_hash"),
        _sha256("prediction_hash"),
        _sha256("settlement_hash"),
        _utc("authoritative_actual_start"),
        _utc("prediction_created_at"),
        _utc("audited_at"),
        _utc("created_at"),
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_prospective_rosh_causal_audit()
            RETURNS trigger AS $$
            DECLARE
                stored_match_id bigint;
                stored_prediction_created_at timestamptz;
                stored_settlement_hash text;
                stored_actual_start timestamptz;
                expected_eligible boolean;
                settlement_created_at timestamptz;
            BEGIN
                SELECT prediction.match_id,
                       live_text_timestamp_utc(prediction.created_at),
                       settlement.settlement_hash,
                       live_text_timestamp_utc(settlement.created_at)
                  INTO stored_match_id, stored_prediction_created_at,
                       stored_settlement_hash, settlement_created_at
                  FROM prospective_rosh_shadow_predictions AS prediction
                  JOIN prospective_rosh_shadow_settlements AS settlement
                    ON settlement.prediction_hash=prediction.prediction_hash
                 WHERE prediction.prediction_hash=NEW.prediction_hash;
                SELECT CASE WHEN target.start_time IS NULL OR target.start_time <= 0
                            THEN NULL ELSE to_timestamp(target.start_time) END
                  INTO stored_actual_start
                  FROM matches AS target
                 WHERE target.match_id=NEW.match_id
                 FOR SHARE;
                expected_eligible :=
                    stored_prediction_created_at < stored_actual_start;
                IF stored_match_id IS NULL OR stored_match_id <> NEW.match_id OR
                   stored_settlement_hash IS DISTINCT FROM NEW.settlement_hash OR
                   stored_actual_start IS NULL OR
                   stored_prediction_created_at <>
                       live_text_timestamp_utc(NEW.prediction_created_at) OR
                   stored_actual_start <>
                       live_text_timestamp_utc(NEW.authoritative_actual_start) OR
                   NEW.causal_eligible IS DISTINCT FROM expected_eligible OR
                   live_text_timestamp_utc(NEW.audited_at) < settlement_created_at OR
                   (expected_eligible AND NEW.exclusion_reason IS NOT NULL) OR
                   (NOT expected_eligible AND NEW.exclusion_reason <>
                       'prediction_not_before_actual_start')
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. causal audit authority disagrees';
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
            CREATE TRIGGER prospective_rosh_causal_audits_insert_guard
            BEFORE INSERT ON prospective_rosh_causal_audits
            FOR EACH ROW
            EXECUTE FUNCTION validate_prospective_rosh_causal_audit()
            """
        )
    )

    for table in (
        "prospective_rosh_collection_attempts",
        "prospective_rosh_causal_audits",
    ):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                    '{table} is append-only'
                )
                """
            )
        )


def downgrade() -> None:
    op.drop_table("prospective_rosh_causal_audits")
    op.execute(sa.text("DROP FUNCTION validate_prospective_rosh_causal_audit()"))
    op.drop_table("prospective_rosh_collection_attempts")
