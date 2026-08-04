"""Link immutable Rosh runs to provider match identities.

Revision ID: 20260802_0021
Revises: 20260801_0020
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260802_0021"
down_revision: str | None = "20260801_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rosh_run_match_links",
        sa.Column("link_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_match_id", sa.Text(), nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("rosh_analysis_runs.run_id"),
            nullable=False,
        ),
        sa.Column("map_number", sa.Integer()),
        sa.Column("linked_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("source", "source_match_id", "run_id"),
        sa.CheckConstraint("source IN ('raybet', 'opendota', 'stratz')"),
        sa.CheckConstraint("length(trim(source_match_id)) BETWEEN 1 AND 128"),
        sa.CheckConstraint("map_number IS NULL OR map_number BETWEEN 1 AND 5"),
        sa.CheckConstraint("live_text_timestamp_utc(linked_at) IS NOT NULL"),
    )
    op.create_index(
        "idx_rosh_run_match_link_lookup",
        "rosh_run_match_links",
        ["source", "source_match_id", sa.text("linked_at DESC")],
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER rosh_run_match_links_succeeded_run_insert
            BEFORE INSERT ON rosh_run_match_links
            FOR EACH ROW EXECUTE FUNCTION require_succeeded_rosh_run(
                'Rosh match link requires succeeded run'
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER rosh_run_match_links_append_only
            BEFORE UPDATE OR DELETE ON rosh_run_match_links
            FOR EACH ROW EXECUTE FUNCTION reject_live_match_input_mutation()
            """
        )
    )


def downgrade() -> None:
    op.drop_table("rosh_run_match_links")
