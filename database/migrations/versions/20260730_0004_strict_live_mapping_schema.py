"""Create strict live map-mapping authority tables.

Revision ID: 20260730_0004
Revises: 20260730_0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0004"
down_revision: str | None = "20260730_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strict_live_map_mappings",
        sa.Column("mapping_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column(
            "event_id",
            sa.Text(),
            sa.ForeignKey("event_registry.event_id"),
            nullable=False,
        ),
        sa.Column("team_one_id", sa.BigInteger(), nullable=False),
        sa.Column("team_two_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_team_one_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_team_one_name", sa.Text(), nullable=False),
        sa.Column("canonical_team_two_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_team_two_name", sa.Text(), nullable=False),
        sa.Column("canonical_identity_json", sa.Text(), nullable=False),
        sa.Column("canonical_identity_hash", sa.Text(), nullable=False),
        sa.Column("crosswalk_evidence_json", sa.Text(), nullable=False),
        sa.Column("crosswalk_evidence_hash", sa.Text(), nullable=False),
        sa.Column("stage_scope", sa.Text(), nullable=False),
        sa.Column("scheduled_at_utc", sa.Text(), nullable=False),
        sa.Column("raybet_best_of", sa.Integer(), nullable=False),
        sa.Column("raybet_identity_json", sa.Text(), nullable=False),
        sa.Column("raybet_identity_hash", sa.Text(), nullable=False),
        sa.Column("raybet_metadata_updated_at", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
        sa.Column("mapping_version", sa.Text(), nullable=False),
        sa.Column(
            "acceptance_mode",
            sa.Text(),
            nullable=False,
            server_default="manual_exact",
        ),
        sa.Column("automatic_approval_id", sa.BigInteger()),
        sa.Column("accepted_by", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint("team_one_id > 0"),
        sa.CheckConstraint("team_two_id > 0"),
        sa.CheckConstraint("canonical_team_one_id > 0"),
        sa.CheckConstraint("canonical_team_two_id > 0"),
        sa.CheckConstraint("length(canonical_identity_hash) = 64"),
        sa.CheckConstraint("length(crosswalk_evidence_hash) = 64"),
        sa.CheckConstraint("stage_scope IN ('main_event', 'internal_lcq')"),
        sa.CheckConstraint("raybet_best_of > 0"),
        sa.CheckConstraint("length(raybet_identity_hash) = 64"),
        sa.CheckConstraint("length(evidence_hash) = 64"),
        sa.CheckConstraint("acceptance_mode IN ('manual_exact', 'automatic_exact')"),
        sa.CheckConstraint("team_one_id != team_two_id"),
        sa.CheckConstraint("canonical_team_one_id != canonical_team_two_id"),
        sa.CheckConstraint("map_number <= raybet_best_of"),
    )
    op.create_table(
        "strict_live_map_mapping_audit",
        sa.Column("audit_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("proposed_event_id", sa.Text()),
        sa.Column("proposed_team_one_id", sa.BigInteger()),
        sa.Column("proposed_team_two_id", sa.BigInteger()),
        sa.Column("proposed_canonical_team_one_id", sa.BigInteger()),
        sa.Column("proposed_canonical_team_two_id", sa.BigInteger()),
        sa.Column("match_method", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
        sa.Column("mapping_version", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text()),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.Column("raybet_identity_hash", sa.Text()),
        sa.Column("raybet_metadata_updated_at", sa.Text()),
        sa.Column("canonical_identity_hash", sa.Text()),
        sa.Column("crosswalk_evidence_hash", sa.Text()),
        sa.Column(
            "mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
        ),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint(
            "match_method IN ('manual_exact', 'automatic_exact', 'candidate', 'fuzzy')"
        ),
        sa.CheckConstraint(
            "decision IN ('accepted', 'idempotent', 'audit_only', 'conflict', "
            "'rejected')"
        ),
        sa.CheckConstraint("length(evidence_hash) = 64"),
        sa.CheckConstraint(
            "(match_method IN ('candidate', 'fuzzy') AND decision = 'audit_only') "
            "OR (match_method IN ('manual_exact', 'automatic_exact') "
            "AND decision != 'audit_only')"
        ),
    )
    op.create_table(
        "strict_live_automatic_evidence_approvals",
        sa.Column("approval_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "source_mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("team_one_id", sa.BigInteger(), nullable=False),
        sa.Column("team_two_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_team_one_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_team_two_id", sa.BigInteger(), nullable=False),
        sa.Column("raybet_identity_hash", sa.Text(), nullable=False),
        sa.Column("canonical_identity_hash", sa.Text(), nullable=False),
        sa.Column("crosswalk_evidence_hash", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(raybet_identity_hash) = 64"),
        sa.CheckConstraint("length(canonical_identity_hash) = 64"),
        sa.CheckConstraint("length(crosswalk_evidence_hash) = 64"),
        sa.CheckConstraint("length(evidence_hash) = 64"),
    )
    op.create_foreign_key(
        "fk_strict_live_mapping_automatic_approval",
        "strict_live_map_mappings",
        "strict_live_automatic_evidence_approvals",
        ["automatic_approval_id"],
        ["approval_id"],
    )
    op.create_table(
        "strict_live_map_mapping_invalidations",
        sa.Column("invalidation_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("invalidated_by", sa.Text(), nullable=False),
        sa.Column("invalidated_at", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "strict_live_map_mapping_supersessions",
        sa.Column("supersession_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "previous_mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "replacement_mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.CheckConstraint("previous_mapping_id != replacement_mapping_id"),
    )
    op.create_table(
        "strict_live_mapping_impacts",
        sa.Column("impact_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
            nullable=False,
        ),
        sa.Column(
            "invalidation_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mapping_invalidations.invalidation_id"),
            nullable=False,
        ),
        sa.Column("dependent_type", sa.Text(), nullable=False),
        sa.Column("dependent_key", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("mapping_id", "dependent_type", "dependent_key"),
        sa.CheckConstraint(
            "dependent_type IN "
            "('strategy_decision', 'research_prediction', 'shadow_order')"
        ),
    )

    op.create_index(
        "idx_strict_live_mapping_event",
        "strict_live_map_mappings",
        ["event_id", "recorded_at"],
    )
    op.create_index(
        "idx_strict_live_mapping_key",
        "strict_live_map_mappings",
        ["raybet_match_id", "map_number", "mapping_id"],
    )
    op.create_index(
        "idx_strict_live_mapping_audit_key",
        "strict_live_map_mapping_audit",
        ["raybet_match_id", "map_number", "recorded_at"],
    )
    _create_immutability_triggers()


def _create_immutability_triggers() -> None:
    trigger_contracts = (
        (
            "strict_live_map_mappings",
            "strict_live_map_mappings",
            "accepted strict live mappings are immutable",
            "accepted strict live mappings cannot be deleted",
        ),
        (
            "strict_live_map_mapping_audit",
            "strict_live_mapping_audit",
            "strict live mapping audit rows are immutable",
            "strict live mapping audit rows cannot be deleted",
        ),
        (
            "strict_live_automatic_evidence_approvals",
            "strict_live_automatic_approval",
            "strict automatic evidence approvals are immutable",
            "strict automatic evidence approvals cannot be deleted",
        ),
        (
            "strict_live_map_mapping_invalidations",
            "strict_live_mapping_invalidation",
            "strict mapping invalidations are immutable",
            "strict mapping invalidations cannot be deleted",
        ),
        (
            "strict_live_map_mapping_supersessions",
            "strict_live_mapping_supersession",
            "strict mapping supersessions are immutable",
            "strict mapping supersessions cannot be deleted",
        ),
        (
            "strict_live_mapping_impacts",
            "strict_live_mapping_impacts",
            "strict mapping impacts are immutable",
            "strict mapping impacts cannot be deleted",
        ),
    )
    for table, prefix, update_message, delete_message in trigger_contracts:
        for operation, message in (
            ("UPDATE", update_message),
            ("DELETE", delete_message),
        ):
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER {prefix}_no_{operation.lower()}
                    BEFORE {operation} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                        '{message}'
                    )
                    """
                )
            )


def downgrade() -> None:
    op.drop_table("strict_live_mapping_impacts")
    op.drop_table("strict_live_map_mapping_supersessions")
    op.drop_table("strict_live_map_mapping_invalidations")
    op.drop_constraint(
        "fk_strict_live_mapping_automatic_approval",
        "strict_live_map_mappings",
        type_="foreignkey",
    )
    op.drop_table("strict_live_automatic_evidence_approvals")
    op.drop_table("strict_live_map_mapping_audit")
    op.drop_table("strict_live_map_mappings")
