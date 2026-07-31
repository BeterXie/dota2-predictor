"""Preserve the earliest match start affected by lineage changes.

Revision ID: 20260801_0019
Revises: 20260731_0018
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "20260801_0019"
down_revision: str | None = "20260731_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _trigger_sql(matches_affected_from: str) -> str:
    return f"""
        CREATE OR REPLACE FUNCTION advance_draft_dependency_revision()
        RETURNS trigger AS $$
        DECLARE
            next_revision bigint;
            changed_at_text text;
            affected_from bigint;
            target_match_id bigint;
            target_event_id text;
            new_row jsonb;
            old_row jsonb;
            scoped boolean := false;
            old_scoped boolean := false;
            no_impact bigint := 9223372036854775807;
        BEGIN
            IF TG_OP != 'DELETE' THEN
                new_row := to_jsonb(NEW);
            END IF;
            IF TG_OP != 'INSERT' THEN
                old_row := to_jsonb(OLD);
            END IF;
            IF TG_OP = 'UPDATE' AND new_row = old_row THEN
                RETURN NEW;
            END IF;

            IF TG_TABLE_NAME = 'event_registry' THEN
                target_event_id := COALESCE(
                    new_row->>'event_id', old_row->>'event_id'
                );
                SELECT EXISTS (
                    SELECT 1
                      FROM event_registry AS event
                     WHERE event.event_id=target_event_id
                       AND event.scope='formal_main_event'
                       AND event.approval_status='approved'
                       AND event.evidence_status='manually_audited'
                       AND event.tier='tier_1'
                       AND event.prize_pool_usd>=1000000
                ) INTO scoped;
                old_scoped := old_row IS NOT NULL
                    AND old_row->>'scope'='formal_main_event'
                    AND old_row->>'approval_status'='approved'
                    AND old_row->>'evidence_status'='manually_audited'
                    AND old_row->>'tier'='tier_1'
                    AND COALESCE((old_row->>'prize_pool_usd')::bigint, 0)
                        >= 1000000;
                IF NOT scoped AND NOT old_scoped THEN
                    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END IF;
                SELECT MIN(draft_lineage_match_impact(status.match_id))
                  INTO affected_from
                  FROM match_ingest_status AS status
                 WHERE status.event_id=target_event_id
                   AND status.stage_in_scope=1
                   AND status.has_valid_result=1
                   AND status.is_exhibition=0
                   AND status.is_forfeit=0
                   AND status.is_void_remake=0
                   AND status.draft_readiness='ready'
                   AND status.stage_scope IN ('main_event', 'internal_lcq');
                affected_from := COALESCE(affected_from, no_impact);

            ELSIF TG_TABLE_NAME = 'match_ingest_status' THEN
                target_match_id := COALESCE(
                    (new_row->>'match_id')::bigint,
                    (old_row->>'match_id')::bigint
                );
                SELECT EXISTS (
                    SELECT 1
                      FROM formal_map_eligibility AS eligible
                     WHERE eligible.match_id=target_match_id
                       AND eligible.draft_readiness='ready'
                ) INTO scoped;
                IF old_row IS NOT NULL THEN
                    SELECT EXISTS (
                        SELECT 1
                          FROM event_registry AS event
                         WHERE event.event_id=old_row->>'event_id'
                           AND event.scope='formal_main_event'
                           AND event.approval_status='approved'
                           AND event.evidence_status='manually_audited'
                           AND event.tier='tier_1'
                           AND event.prize_pool_usd>=1000000
                    ) INTO old_scoped;
                    old_scoped := old_scoped
                        AND old_row->>'stage_in_scope'='1'
                        AND old_row->>'has_valid_result'='1'
                        AND old_row->>'is_exhibition'='0'
                        AND old_row->>'is_forfeit'='0'
                        AND old_row->>'is_void_remake'='0'
                        AND old_row->>'draft_readiness'='ready'
                        AND old_row->>'stage_scope'
                            IN ('main_event', 'internal_lcq');
                END IF;
                IF NOT scoped AND NOT old_scoped THEN
                    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END IF;
                affected_from := draft_lineage_match_impact(target_match_id);

            ELSIF TG_TABLE_NAME IN (
                'raw_source_artifacts', 'raw_source_observations'
            ) THEN
                SELECT MIN(draft_lineage_match_impact(status.match_id))
                  INTO affected_from
                  FROM match_ingest_status AS status
                  JOIN formal_map_eligibility AS eligible
                    ON eligible.match_id=status.match_id
                 WHERE eligible.draft_readiness='ready'
                   AND (
                       (
                           new_row IS NOT NULL
                           AND status.latest_raw_artifact_id=
                               new_row->>'artifact_id'
                           AND status.latest_raw_content_hash=
                               new_row->>'content_hash'
                       ) OR (
                           old_row IS NOT NULL
                           AND status.latest_raw_artifact_id=
                               old_row->>'artifact_id'
                           AND status.latest_raw_content_hash=
                               old_row->>'content_hash'
                       )
                   );
                IF affected_from IS NULL THEN
                    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END IF;

            ELSE
                target_match_id := COALESCE(
                    (new_row->>'match_id')::bigint,
                    (old_row->>'match_id')::bigint
                );
                SELECT EXISTS (
                    SELECT 1
                      FROM formal_map_eligibility AS eligible
                     WHERE eligible.match_id=target_match_id
                       AND eligible.draft_readiness='ready'
                ) INTO scoped;
                IF NOT scoped THEN
                    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END IF;
                {matches_affected_from}
            END IF;

            changed_at_text := replace(
                to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                '_', ':'
            );
            UPDATE draft_lineage_revisions
               SET dependency_revision = dependency_revision + 1,
                   updated_at = changed_at_text
             WHERE singleton = 1
             RETURNING dependency_revision INTO next_revision;
            IF next_revision IS NULL THEN
                RAISE EXCEPTION 'draft lineage revision authority is missing';
            END IF;
            INSERT INTO draft_lineage_changes (
                dependency_revision, affected_from_unix,
                source_relation, operation, changed_at
            ) VALUES (
                next_revision, affected_from,
                TG_TABLE_NAME, TG_OP, changed_at_text
            );
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """


def upgrade() -> None:
    op.execute(
        text(
            _trigger_sql(
                """
                IF TG_TABLE_NAME = 'matches' THEN
                    SELECT MIN(candidate.start_time)
                      INTO affected_from
                      FROM (VALUES
                          ((new_row->>'start_time')::bigint),
                          ((old_row->>'start_time')::bigint),
                          (draft_lineage_match_impact(target_match_id))
                      ) AS candidate(start_time)
                     WHERE candidate.start_time>0;
                ELSE
                    affected_from := draft_lineage_match_impact(target_match_id);
                END IF;
                """
            )
        )
    )


def downgrade() -> None:
    op.execute(
        text(
            _trigger_sql(
                """
                affected_from := draft_lineage_match_impact(target_match_id);
                IF TG_TABLE_NAME = 'matches' AND affected_from IS NULL THEN
                    affected_from := COALESCE(
                        (new_row->>'start_time')::bigint,
                        (old_row->>'start_time')::bigint
                    );
                END IF;
                """
            )
        )
    )
