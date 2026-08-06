"""Allow application-canonical prematch corpus JSON text.

Revision ID: 20260806_0029
Revises: 20260806_0028
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260806_0029"
down_revision: str | None = "20260806_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Python's canonical serializer and PostgreSQL jsonb use different numeric
    # spellings. Hash/replay validation remains in PrematchCorpusStore.
    op.drop_constraint(
        "prematch_training_corpus_rows_row_json_check1",
        "prematch_training_corpus_rows",
        type_="check",
    )


def downgrade() -> None:
    op.create_check_constraint(
        "prematch_training_corpus_rows_row_json_check1",
        "prematch_training_corpus_rows",
        "row_json=team_rating_canonical_json(row_json::jsonb)",
    )
