"""Allow the live writer to rebase a vision anchor inside one transaction.

Revision ID: 20260730_0015
Revises: 20260730_0014
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0015"
down_revision: str | None = "20260730_0014"
branch_labels: str | None = None
depends_on: str | None = None


def _guard_function(*, allow_internal_rebuild: bool) -> str:
    bypass = """
            IF current_setting(
                'dota2.allow_vision_anchor_rebuild', true
            ) = 'on' THEN
                RETURN NEW;
            END IF;
    """ if allow_internal_rebuild else ""
    return f"""
        CREATE OR REPLACE FUNCTION guard_vision_draft_anchor_transition()
        RETURNS trigger AS $$ BEGIN
            {bypass}
            IF NOT (
                (
                    OLD.status = 'anchored'
                    AND NEW.status = 'conflict'
                    AND OLD.raybet_match_id IS NOT DISTINCT FROM
                        NEW.raybet_match_id
                    AND OLD.map_number IS NOT DISTINCT FROM NEW.map_number
                    AND OLD.draft_hash IS NOT DISTINCT FROM NEW.draft_hash
                    AND OLD.radiant_hero_ids IS NOT DISTINCT FROM
                        NEW.radiant_hero_ids
                    AND OLD.dire_hero_ids IS NOT DISTINCT FROM NEW.dire_hero_ids
                    AND OLD.radiant_team_side IS NOT DISTINCT FROM
                        NEW.radiant_team_side
                    AND OLD.team_side_anchored_at IS NOT DISTINCT FROM
                        NEW.team_side_anchored_at
                    AND OLD.team_side_source_frame_ref IS NOT DISTINCT FROM
                        NEW.team_side_source_frame_ref
                    AND OLD.anchored_at IS NOT DISTINCT FROM NEW.anchored_at
                    AND OLD.source_frame_ref IS NOT DISTINCT FROM
                        NEW.source_frame_ref
                    AND OLD.conflict_at IS NULL
                    AND live_text_timestamp_utc(NEW.conflict_at) IS NOT NULL
                ) OR (
                    OLD.status = 'anchored'
                    AND NEW.status = 'anchored'
                    AND OLD.raybet_match_id IS NOT DISTINCT FROM
                        NEW.raybet_match_id
                    AND OLD.map_number IS NOT DISTINCT FROM NEW.map_number
                    AND OLD.draft_hash IS NOT DISTINCT FROM NEW.draft_hash
                    AND OLD.radiant_hero_ids IS NOT DISTINCT FROM
                        NEW.radiant_hero_ids
                    AND OLD.dire_hero_ids IS NOT DISTINCT FROM NEW.dire_hero_ids
                    AND OLD.radiant_team_side IS NULL
                    AND NEW.radiant_team_side IN ('team_one', 'team_two')
                    AND OLD.team_side_anchored_at IS NULL
                    AND live_text_timestamp_utc(NEW.team_side_anchored_at)
                        IS NOT NULL
                    AND OLD.team_side_source_frame_ref IS NULL
                    AND NULLIF(NEW.team_side_source_frame_ref, '') IS NOT NULL
                    AND OLD.anchored_at IS NOT DISTINCT FROM NEW.anchored_at
                    AND OLD.source_frame_ref IS NOT DISTINCT FROM
                        NEW.source_frame_ref
                    AND OLD.conflict_at IS NOT DISTINCT FROM NEW.conflict_at
                )
            ) THEN
                RAISE EXCEPTION 'vision draft anchor is immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """


def upgrade() -> None:
    op.execute(sa.text(_guard_function(allow_internal_rebuild=True)))


def downgrade() -> None:
    op.execute(sa.text(_guard_function(allow_internal_rebuild=False)))
