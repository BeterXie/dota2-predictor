"""Allow the C0-C9 cluster ablation in existing prematch tables.

Revision ID: 20260805_0026
Revises: 20260805_0025
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260805_0026"
down_revision: str | None = "20260805_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BASE_MODEL_KINDS = (
    "'team_only', 'team_plus_draft', 'team_plus_rosh', "
    "'team_plus_draft_rosh'"
)
_CLUSTER_MODEL_KINDS = _BASE_MODEL_KINDS + ", 'team_plus_draft_rosh_clusters'"


def _replace_model_kind_check(table: str, allowed: str) -> None:
    name = f"{table}_model_kind_check"
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"model_kind IN ({allowed})")


def upgrade() -> None:
    _replace_model_kind_check("prematch_model_runs", _CLUSTER_MODEL_KINDS)
    _replace_model_kind_check("prematch_calibration_artifacts", _CLUSTER_MODEL_KINDS)


def downgrade() -> None:
    _replace_model_kind_check("prematch_calibration_artifacts", _BASE_MODEL_KINDS)
    _replace_model_kind_check("prematch_model_runs", _BASE_MODEL_KINDS)
