"""Persist strictly replayable legacy-to-official R.O.S.H. bridge records.

Revision ID: 20260806_0030
Revises: 20260806_0029
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260806_0030"
down_revision: str | None = "20260806_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rosh_authority_bridge_records",
        sa.Column("bridge_key", sa.Text(), primary_key=True),
        sa.Column("bridge_version", sa.Text(), nullable=False),
        sa.Column(
            "legacy_score_key",
            sa.Text(),
            sa.ForeignKey("historical_rosh_lineup_scores.score_key"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("rosh_analysis_runs.run_id"),
            nullable=False,
        ),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
        ),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("radiant_player_ids_json", sa.Text()),
        sa.Column("dire_player_ids_json", sa.Text()),
        sa.Column("player_coverage_count", sa.Integer()),
        sa.Column("rosh_profile_id", sa.Text(), nullable=False),
        sa.Column("formula_version", sa.Text(), nullable=False),
        sa.Column("scorer_source_hash", sa.Text(), nullable=False),
        sa.Column("canonical_profile_hash", sa.Text(), nullable=False),
        sa.Column("input_artifact_hash", sa.Text(), nullable=False),
        sa.Column("response_artifact_hash", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.Text(), nullable=False),
        sa.Column("available_at", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer()),
        sa.Column("authority_json", sa.Text(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("legacy_score_key"),
        sa.UniqueConstraint("match_id", "run_id"),
        sa.UniqueConstraint("source", "source_match_id", "run_id"),
        sa.CheckConstraint("bridge_key ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint(
            "bridge_version = 'rosh-authority-bridge-v1'"
        ),
        sa.CheckConstraint("match_id > 0"),
        sa.CheckConstraint(
            "radiant_player_ids_json IS NULL OR "
            "(jsonb_typeof(radiant_player_ids_json::jsonb) = 'array' AND "
            "jsonb_array_length(radiant_player_ids_json::jsonb) = 5)"
        ),
        sa.CheckConstraint(
            "dire_player_ids_json IS NULL OR "
            "(jsonb_typeof(dire_player_ids_json::jsonb) = 'array' AND "
            "jsonb_array_length(dire_player_ids_json::jsonb) = 5)"
        ),
        sa.CheckConstraint(
            "player_coverage_count IS NULL OR "
            "player_coverage_count BETWEEN 0 AND 10"
        ),
        sa.CheckConstraint("length(trim(rosh_profile_id)) > 0"),
        sa.CheckConstraint("length(trim(formula_version)) > 0"),
        sa.CheckConstraint("scorer_source_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("canonical_profile_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("input_artifact_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("response_artifact_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("jsonb_typeof(draft_json::jsonb) = 'object'"),
        sa.CheckConstraint("jsonb_typeof(authority_json::jsonb) = 'object'"),
        sa.CheckConstraint("jsonb_typeof(snapshot_json::jsonb) = 'object'"),
        sa.CheckConstraint("source IN ('opendota', 'stratz')"),
        sa.CheckConstraint("length(trim(source_match_id)) BETWEEN 1 AND 128"),
        sa.CheckConstraint("map_number IS NULL OR map_number BETWEEN 1 AND 5"),
        sa.CheckConstraint(
            "live_text_timestamp_utc(generated_at) IS NOT NULL AND "
            "live_text_timestamp_utc(available_at) IS NOT NULL AND "
            "live_text_timestamp_utc(created_at) IS NOT NULL AND "
            "live_text_timestamp_utc(generated_at) <= "
            "live_text_timestamp_utc(available_at) AND "
            "live_text_timestamp_utc(available_at) <= "
            "live_text_timestamp_utc(prediction_cutoff)"
        ),
    )
    op.create_index(
        "idx_rosh_authority_bridge_match_cutoff",
        "rosh_authority_bridge_records",
        ["match_id", "prediction_cutoff", "run_id"],
    )
    op.create_index(
        "idx_rosh_authority_bridge_artifacts",
        "rosh_authority_bridge_records",
        ["input_artifact_hash", "response_artifact_hash"],
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER rosh_authority_bridge_records_immutable_update
            BEFORE UPDATE ON rosh_authority_bridge_records
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                'R.O.S.H. authority bridge record is immutable'
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER rosh_authority_bridge_records_immutable_delete
            BEFORE DELETE ON rosh_authority_bridge_records
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                'R.O.S.H. authority bridge record is immutable'
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER prematch_lineage_dependency_rosh_authority_bridge_records
            AFTER INSERT OR UPDATE OR DELETE ON rosh_authority_bridge_records
            FOR EACH ROW EXECUTE FUNCTION advance_prematch_dependency_revision()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DROP TRIGGER prematch_lineage_dependency_rosh_authority_bridge_records
            ON rosh_authority_bridge_records
            """
        )
    )
    op.execute(
        sa.text(
            """
            DROP TRIGGER rosh_authority_bridge_records_immutable_delete
            ON rosh_authority_bridge_records
            """
        )
    )
    op.execute(
        sa.text(
            """
            DROP TRIGGER rosh_authority_bridge_records_immutable_update
            ON rosh_authority_bridge_records
            """
        )
    )
    op.drop_index(
        "idx_rosh_authority_bridge_artifacts",
        table_name="rosh_authority_bridge_records",
    )
    op.drop_index(
        "idx_rosh_authority_bridge_match_cutoff",
        table_name="rosh_authority_bridge_records",
    )
    op.drop_table("rosh_authority_bridge_records")
