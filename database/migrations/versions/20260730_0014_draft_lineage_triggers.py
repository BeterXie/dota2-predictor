"""Install PostgreSQL draft-lineage revision triggers.

Revision ID: 20260730_0014
Revises: 20260730_0013
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "20260730_0014"
down_revision: str | None = "20260730_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEPENDENCY_TABLES = (
    "event_registry",
    "match_ingest_status",
    "matches",
    "raw_source_artifacts",
    "raw_source_observations",
    "player_map_facts",
    "match_players",
    "picks_bans",
    "player_role_assignments",
    "player_map_scores",
    "team_map_states",
)
ARTIFACT_TABLES = ("draft_model_runs", "draft_predictions")


def _trigger_name(kind: str, table_name: str, operation: str) -> str:
    return f"draft_lineage_{kind}_{table_name}_{operation.lower()}"


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE FUNCTION advance_draft_dependency_revision()
            RETURNS trigger AS $$
            DECLARE
                next_revision bigint;
                changed_at_text text;
            BEGIN
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
                    next_revision, NULL, TG_TABLE_NAME, TG_OP, changed_at_text
                );
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        text(
            """
            CREATE FUNCTION advance_draft_artifact_revision()
            RETURNS trigger AS $$
            BEGIN
                UPDATE draft_lineage_revisions
                   SET artifact_revision = artifact_revision + 1,
                       updated_at = replace(
                           to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                           '_', ':'
                       )
                 WHERE singleton = 1;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'draft lineage revision authority is missing';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        text(
            """
            CREATE FUNCTION guard_draft_lineage_change_append()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM draft_lineage_changes AS existing
                     WHERE existing.dependency_revision = NEW.dependency_revision
                ) OR NEW.dependency_revision IS DISTINCT FROM (
                    SELECT dependency_revision
                      FROM draft_lineage_revisions
                     WHERE singleton = 1
                ) THEN
                    RAISE EXCEPTION 'draft lineage changes are append-only';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        text(
            """
            CREATE FUNCTION reject_draft_lineage_change_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'draft lineage changes are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )

    for table_name in DEPENDENCY_TABLES:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                text(
                    f"""
                    CREATE TRIGGER {_trigger_name('dependency', table_name, operation)}
                    AFTER {operation} ON {table_name}
                    FOR EACH ROW EXECUTE FUNCTION advance_draft_dependency_revision()
                    """
                )
            )
    for table_name in ARTIFACT_TABLES:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                text(
                    f"""
                    CREATE TRIGGER {_trigger_name('artifact', table_name, operation)}
                    AFTER {operation} ON {table_name}
                    FOR EACH ROW EXECUTE FUNCTION advance_draft_artifact_revision()
                    """
                )
            )

    op.execute(
        text(
            """
            CREATE TRIGGER draft_lineage_changes_append_only
            BEFORE INSERT ON draft_lineage_changes
            FOR EACH ROW EXECUTE FUNCTION guard_draft_lineage_change_append()
            """
        )
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            text(
                f"""
                CREATE TRIGGER draft_lineage_changes_no_{operation.lower()}
                BEFORE {operation} ON draft_lineage_changes
                FOR EACH ROW EXECUTE FUNCTION reject_draft_lineage_change_mutation()
                """
            )
        )


def downgrade() -> None:
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            text(
                f"DROP TRIGGER draft_lineage_changes_no_{operation.lower()} "
                "ON draft_lineage_changes"
            )
        )
    op.execute(
        text(
            "DROP TRIGGER draft_lineage_changes_append_only "
            "ON draft_lineage_changes"
        )
    )
    for table_name in ARTIFACT_TABLES:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                text(
                    f"DROP TRIGGER {_trigger_name('artifact', table_name, operation)} "
                    f"ON {table_name}"
                )
            )
    for table_name in DEPENDENCY_TABLES:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                text(
                    f"DROP TRIGGER {_trigger_name('dependency', table_name, operation)} "
                    f"ON {table_name}"
                )
            )
    op.execute(text("DROP FUNCTION reject_draft_lineage_change_mutation()"))
    op.execute(text("DROP FUNCTION guard_draft_lineage_change_append()"))
    op.execute(text("DROP FUNCTION advance_draft_artifact_revision()"))
    op.execute(text("DROP FUNCTION advance_draft_dependency_revision()"))
