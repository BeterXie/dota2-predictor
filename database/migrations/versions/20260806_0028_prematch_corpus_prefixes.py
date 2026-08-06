"""Deduplicate prematch training corpora with content-addressed prefixes.

Revision ID: 20260806_0028
Revises: 20260805_0027
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260806_0028"
down_revision: str | None = "20260805_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODEL_KINDS = (
    "'team_only', 'team_plus_draft', 'team_plus_rosh', "
    "'team_plus_draft_rosh', 'team_plus_draft_rosh_clusters'"
)
_MODES = "'reconstructed_walk_forward', 'prospective'"


def _sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({column})=64 AND {column} ~ '^[0-9a-f]{{64}}$'"
    )


def upgrade() -> None:
    op.create_table(
        "prematch_training_corpus_rows",
        sa.Column("row_hash", sa.Text(), primary_key=True),
        sa.Column("model_kind", sa.Text(), nullable=False),
        sa.Column("row_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        _sha256("row_hash"),
        sa.CheckConstraint(f"model_kind IN ({_MODEL_KINDS})"),
        sa.CheckConstraint("jsonb_typeof(row_json::jsonb)='object'"),
        sa.CheckConstraint(
            "row_json=team_rating_canonical_json(row_json::jsonb)"
        ),
        sa.CheckConstraint(
            "live_text_timestamp_utc(created_at) IS NOT NULL"
        ),
    )
    op.create_table(
        "prematch_training_corpus_prefixes",
        sa.Column("prefix_hash", sa.Text(), primary_key=True),
        sa.Column("model_kind", sa.Text(), nullable=False),
        sa.Column("availability_mode", sa.Text(), nullable=False),
        sa.Column(
            "parent_prefix_hash",
            sa.Text(),
            sa.ForeignKey("prematch_training_corpus_prefixes.prefix_hash"),
        ),
        sa.Column(
            "row_hash",
            sa.Text(),
            sa.ForeignKey("prematch_training_corpus_rows.row_hash"),
            nullable=False,
        ),
        sa.Column("support", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        _sha256("prefix_hash"),
        sa.CheckConstraint(
            "parent_prefix_hash IS NULL OR "
            "(length(parent_prefix_hash)=64 AND "
            "parent_prefix_hash ~ '^[0-9a-f]{64}$')"
        ),
        _sha256("row_hash"),
        sa.CheckConstraint(f"model_kind IN ({_MODEL_KINDS})"),
        sa.CheckConstraint(f"availability_mode IN ({_MODES})"),
        sa.CheckConstraint("support > 0"),
        sa.CheckConstraint(
            "(support=1 AND parent_prefix_hash IS NULL) OR "
            "(support>1 AND parent_prefix_hash IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "live_text_timestamp_utc(created_at) IS NOT NULL"
        ),
    )
    op.create_index(
        "idx_prematch_corpus_prefix_parent",
        "prematch_training_corpus_prefixes",
        ["parent_prefix_hash"],
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_prematch_corpus_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'prematch corpus authority is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    for table in (
        "prematch_training_corpus_rows",
        "prematch_training_corpus_prefixes",
    ):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION guard_prematch_corpus_immutable()
                """
            )
        )


def downgrade() -> None:
    for table in (
        "prematch_training_corpus_prefixes",
        "prematch_training_corpus_rows",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS guard_prematch_corpus_immutable()"))
    op.drop_index(
        "idx_prematch_corpus_prefix_parent",
        table_name="prematch_training_corpus_prefixes",
    )
    op.drop_table("prematch_training_corpus_prefixes")
    op.drop_table("prematch_training_corpus_rows")
