"""Separate manual live drafts from dynamic game state snapshots.

Revision ID: 20260801_0020
Revises: 20260801_0019
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0020"
down_revision: str | None = "20260801_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_draft_mappings",
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("hero_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("is_locked", sa.SmallInteger(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "raybet_match_id", "map_number", "version", "side", "position"
        ),
        sa.UniqueConstraint(
            "raybet_match_id", "map_number", "version", "hero_id"
        ),
        sa.CheckConstraint("raybet_match_id != ''"),
        sa.CheckConstraint("map_number BETWEEN 1 AND 5"),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("team_id > 0"),
        sa.CheckConstraint("side IN ('radiant', 'dire')"),
        sa.CheckConstraint("position BETWEEN 1 AND 5"),
        sa.CheckConstraint("hero_id > 0"),
        sa.CheckConstraint("player_id IS NULL OR player_id > 0"),
        sa.CheckConstraint("source IN ('manual', 'manual_correction')"),
        sa.CheckConstraint("is_locked IN (0, 1)"),
        sa.CheckConstraint("created_by != ''"),
        sa.CheckConstraint("live_text_timestamp_utc(created_at) IS NOT NULL"),
    )
    op.create_index(
        "idx_live_draft_mapping_latest",
        "live_draft_mappings",
        ["raybet_match_id", "map_number", sa.text("version DESC")],
    )
    op.create_index(
        "uq_live_draft_mapping_player",
        "live_draft_mappings",
        ["raybet_match_id", "map_number", "version", "player_id"],
        unique=True,
        postgresql_where=sa.text("player_id IS NOT NULL"),
    )

    op.create_table(
        "live_game_snapshots",
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("game_time_seconds", sa.Integer(), nullable=False),
        sa.Column("radiant_networth", sa.BigInteger(), nullable=False),
        sa.Column("dire_networth", sa.BigInteger(), nullable=False),
        sa.Column("networth_lead", sa.BigInteger(), nullable=False),
        sa.Column("radiant_kills", sa.Integer()),
        sa.Column("dire_kills", sa.Integer()),
        sa.Column("vision_confidence", sa.Double(), nullable=False),
        sa.Column("screenshot_path", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "raybet_match_id", "map_number", "captured_at", "source"
        ),
        sa.CheckConstraint("raybet_match_id != ''"),
        sa.CheckConstraint("map_number BETWEEN 1 AND 5"),
        sa.CheckConstraint("game_time_seconds >= 0"),
        sa.CheckConstraint("radiant_networth >= 0"),
        sa.CheckConstraint("dire_networth >= 0"),
        sa.CheckConstraint("networth_lead = radiant_networth - dire_networth"),
        sa.CheckConstraint("radiant_kills IS NULL OR radiant_kills >= 0"),
        sa.CheckConstraint("dire_kills IS NULL OR dire_kills >= 0"),
        sa.CheckConstraint("vision_confidence BETWEEN 0 AND 1"),
        sa.CheckConstraint("source IN ('vision', 'manual_correction')"),
        sa.CheckConstraint("live_text_timestamp_utc(captured_at) IS NOT NULL"),
        sa.CheckConstraint("live_text_timestamp_utc(created_at) IS NOT NULL"),
    )
    op.create_index(
        "idx_live_game_snapshot_timeline",
        "live_game_snapshots",
        ["raybet_match_id", "map_number", sa.text("captured_at DESC")],
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_live_match_input_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    for table in ("live_draft_mappings", "live_game_snapshots"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_live_match_input_mutation()
                """
            )
        )


def downgrade() -> None:
    for table in ("live_game_snapshots", "live_draft_mappings"):
        op.execute(sa.text(f"DROP TRIGGER {table}_append_only ON {table}"))
    op.execute(sa.text("DROP FUNCTION reject_live_match_input_mutation()"))
    op.drop_table("live_game_snapshots")
    op.drop_table("live_draft_mappings")
