"""Add Alembic-managed hero scaling columns.

Revision ID: 20260730_0017
Revises: 20260730_0016
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0017"
down_revision: str | None = "20260730_0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "heroes", sa.Column("early_wr", sa.Double(), server_default="0.5")
    )
    op.add_column(
        "heroes", sa.Column("mid_wr", sa.Double(), server_default="0.5")
    )
    op.add_column(
        "heroes", sa.Column("late_wr", sa.Double(), server_default="0.5")
    )
    op.add_column(
        "heroes", sa.Column("scaling_score", sa.Double(), server_default="0")
    )


def downgrade() -> None:
    op.drop_column("heroes", "scaling_score")
    op.drop_column("heroes", "late_wr")
    op.drop_column("heroes", "mid_wr")
    op.drop_column("heroes", "early_wr")
