"""Defer the strict automatic-mapping foreign-key cycle for atomic imports.

Revision ID: 20260730_0013
Revises: 20260730_0012
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260730_0013"
down_revision: str | None = "20260730_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAPPING_APPROVAL_FK = "fk_strict_live_mapping_automatic_approval"
APPROVAL_MAPPING_FK = (
    "strict_live_automatic_evidence_approvals_source_mapping_id_fkey"
)


def upgrade() -> None:
    op.drop_constraint(
        MAPPING_APPROVAL_FK,
        "strict_live_map_mappings",
        type_="foreignkey",
    )
    op.drop_constraint(
        APPROVAL_MAPPING_FK,
        "strict_live_automatic_evidence_approvals",
        type_="foreignkey",
    )
    op.create_foreign_key(
        MAPPING_APPROVAL_FK,
        "strict_live_map_mappings",
        "strict_live_automatic_evidence_approvals",
        ["automatic_approval_id"],
        ["approval_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        APPROVAL_MAPPING_FK,
        "strict_live_automatic_evidence_approvals",
        "strict_live_map_mappings",
        ["source_mapping_id"],
        ["mapping_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        MAPPING_APPROVAL_FK,
        "strict_live_map_mappings",
        type_="foreignkey",
    )
    op.drop_constraint(
        APPROVAL_MAPPING_FK,
        "strict_live_automatic_evidence_approvals",
        type_="foreignkey",
    )
    op.create_foreign_key(
        MAPPING_APPROVAL_FK,
        "strict_live_map_mappings",
        "strict_live_automatic_evidence_approvals",
        ["automatic_approval_id"],
        ["approval_id"],
    )
    op.create_foreign_key(
        APPROVAL_MAPPING_FK,
        "strict_live_automatic_evidence_approvals",
        "strict_live_map_mappings",
        ["source_mapping_id"],
        ["mapping_id"],
    )
