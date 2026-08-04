"""Persist immutable Team Rating runs, predictions, and checkpoints.

Revision ID: 20260805_0022
Revises: 20260802_0021
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0022"
down_revision: str | None = "20260802_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UTC_SUFFIX_CHECK = "({0} LIKE '%Z' OR {0} LIKE '%+00:00')"
_SHA256_CHECK = "{0} ~ '^[0-9a-f]{{64}}$'"


def _utc_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"live_text_timestamp_utc({column}) IS NOT NULL AND "
        + _UTC_SUFFIX_CHECK.format(column)
    )


def _sha256_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(_SHA256_CHECK.format(column))


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION team_rating_canonical_json(value jsonb)
            RETURNS text AS $$
            DECLARE
                value_type text;
                canonical text;
            BEGIN
                value_type := jsonb_typeof(value);
                IF value_type = 'object' THEN
                    SELECT '{' || COALESCE(
                               string_agg(
                                   to_jsonb(member.key)::text || ':' ||
                                   team_rating_canonical_json(member.value),
                                   ',' ORDER BY member.key COLLATE "C"
                               ),
                               ''
                           ) || '}'
                      INTO canonical
                      FROM jsonb_each(value) AS member(key, value);
                    RETURN canonical;
                END IF;
                IF value_type = 'array' THEN
                    SELECT '[' || COALESCE(
                               string_agg(
                                   team_rating_canonical_json(element.value),
                                   ',' ORDER BY element.position
                               ),
                               ''
                           ) || ']'
                      INTO canonical
                      FROM jsonb_array_elements(value) WITH ORDINALITY
                           AS element(value, position);
                    RETURN canonical;
                END IF;
                RETURN value::text;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE STRICT
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION team_rating_roster_is_valid(value jsonb)
            RETURNS boolean AS $$
            DECLARE
                roster_size integer;
            BEGIN
                IF jsonb_typeof(value) != 'array' THEN
                    RETURN false;
                END IF;
                roster_size := jsonb_array_length(value);
                IF roster_size = 0 THEN
                    RETURN true;
                END IF;
                IF roster_size != 5 THEN
                    RETURN false;
                END IF;
                RETURN NOT EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements(value) AS player(value)
                            WHERE player.value::text !~ '^[1-9][0-9]*$'
                       ) AND (
                           SELECT COUNT(*) = COUNT(DISTINCT player.value::text)
                             FROM jsonb_array_elements(value) AS player(value)
                       );
            END;
            $$ LANGUAGE plpgsql IMMUTABLE STRICT
            """
        )
    )
    op.create_table(
        "team_rating_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("rating_version", sa.Text(), nullable=False),
        sa.Column("artifact_version", sa.Text(), nullable=False),
        sa.Column("availability_mode", sa.Text(), nullable=False),
        sa.Column("training_cutoff", sa.Text(), nullable=False),
        sa.Column("configuration_json", sa.Text(), nullable=False),
        sa.Column("training_input_hash", sa.Text(), nullable=False),
        sa.Column("metrics_json", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(trim(rating_version)) > 0"),
        sa.CheckConstraint("length(trim(artifact_version)) > 0"),
        sa.CheckConstraint(
            "availability_mode IN "
            "('reconstructed_walk_forward', 'prospective')"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(configuration_json::jsonb) = 'object' AND "
            "configuration_json = "
            "team_rating_canonical_json(configuration_json::jsonb)"
        ),
        sa.CheckConstraint(
            "metrics_json IS NULL OR "
            "(jsonb_typeof(metrics_json::jsonb) = 'object' AND "
            "metrics_json = team_rating_canonical_json(metrics_json::jsonb))"
        ),
        sa.CheckConstraint(
            "status IN ('trained', 'insufficient_evidence', 'failed')"
        ),
        _sha256_check("run_id"),
        _sha256_check("training_input_hash"),
        _utc_check("training_cutoff"),
        _utc_check("created_at"),
    )
    op.create_index(
        "idx_team_rating_runs_mode_cutoff",
        "team_rating_runs",
        ["availability_mode", sa.text("training_cutoff DESC")],
    )

    op.create_table(
        "team_rating_predictions",
        sa.Column(
            "prediction_id",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
        ),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("team_rating_runs.run_id"),
            nullable=False,
        ),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
        ),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column("cutoff_source", sa.Text(), nullable=False),
        sa.Column("radiant_team_id", sa.BigInteger(), nullable=False),
        sa.Column("dire_team_id", sa.BigInteger(), nullable=False),
        sa.Column("radiant_rating", sa.Double(), nullable=False),
        sa.Column("dire_rating", sa.Double(), nullable=False),
        sa.Column("rating_diff", sa.Double(), nullable=False),
        sa.Column("raw_probability", sa.Double()),
        sa.Column("radiant_roster_continuity", sa.Double()),
        sa.Column("dire_roster_continuity", sa.Double()),
        sa.Column("support", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("eventual_radiant_win", sa.SmallInteger()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("run_id", "match_id"),
        sa.CheckConstraint("length(trim(cutoff_source)) > 0"),
        sa.CheckConstraint("radiant_team_id > 0"),
        sa.CheckConstraint("dire_team_id > 0"),
        sa.CheckConstraint("radiant_team_id <> dire_team_id"),
        sa.CheckConstraint(
            "radiant_rating > '-Infinity'::double precision AND "
            "radiant_rating < 'Infinity'::double precision"
        ),
        sa.CheckConstraint(
            "dire_rating > '-Infinity'::double precision AND "
            "dire_rating < 'Infinity'::double precision"
        ),
        sa.CheckConstraint(
            "rating_diff > '-Infinity'::double precision AND "
            "rating_diff < 'Infinity'::double precision"
        ),
        sa.CheckConstraint("rating_diff = radiant_rating - dire_rating"),
        sa.CheckConstraint(
            "raw_probability IS NULL OR raw_probability BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "radiant_roster_continuity IS NULL OR "
            "radiant_roster_continuity BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "dire_roster_continuity IS NULL OR "
            "dire_roster_continuity BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint("support >= 0"),
        sa.CheckConstraint(
            "eventual_radiant_win IS NULL OR eventual_radiant_win IN (0, 1)"
        ),
        sa.CheckConstraint(
            "status IN "
            "('predicted', 'insufficient_evidence', 'settled', 'failed')"
        ),
        sa.CheckConstraint(
            "(status = 'settled') = (eventual_radiant_win IS NOT NULL)"
        ),
        sa.CheckConstraint(
            "(status IN ('predicted', 'settled')) = "
            "(raw_probability IS NOT NULL)"
        ),
        _sha256_check("input_hash"),
        _utc_check("prediction_cutoff"),
        _utc_check("created_at"),
    )
    op.create_index(
        "idx_team_rating_predictions_match_cutoff",
        "team_rating_predictions",
        ["match_id", sa.text("prediction_cutoff DESC")],
    )
    op.create_index(
        "idx_team_rating_predictions_run",
        "team_rating_predictions",
        ["run_id", "match_id"],
    )

    op.create_table(
        "team_rating_state_snapshots",
        sa.Column("snapshot_key", sa.Text(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("team_rating_runs.run_id"),
            nullable=False,
        ),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("rating", sa.Double(), nullable=False),
        sa.Column("maps_seen", sa.Integer(), nullable=False),
        sa.Column("roster_json", sa.Text(), nullable=False),
        sa.Column("last_observed_at", sa.Text()),
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("run_id", "as_of", "team_id"),
        sa.CheckConstraint("team_id > 0"),
        sa.CheckConstraint(
            "rating > '-Infinity'::double precision AND "
            "rating < 'Infinity'::double precision"
        ),
        sa.CheckConstraint("maps_seen >= 0"),
        sa.CheckConstraint(
            "jsonb_typeof(roster_json::jsonb) = 'array' AND "
            "roster_json = team_rating_canonical_json(roster_json::jsonb) AND "
            "team_rating_roster_is_valid(roster_json::jsonb)"
        ),
        sa.CheckConstraint(
            "last_observed_at IS NULL OR "
            "(live_text_timestamp_utc(last_observed_at) IS NOT NULL AND "
            + _UTC_SUFFIX_CHECK.format("last_observed_at")
            + " AND live_text_timestamp_utc(last_observed_at) "
            "<= live_text_timestamp_utc(as_of))"
        ),
        _sha256_check("snapshot_key"),
        _sha256_check("state_hash"),
        _utc_check("as_of"),
        _utc_check("created_at"),
    )
    op.create_index(
        "idx_team_rating_state_team_as_of",
        "team_rating_state_snapshots",
        ["team_id", sa.text("as_of DESC")],
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_team_rating_child_cutoff()
            RETURNS trigger AS $$
            DECLARE
                run_cutoff timestamptz;
                run_mode text;
                prediction_at timestamptz;
                target_started_at timestamptz;
                target_radiant_team_id bigint;
                target_dire_team_id bigint;
                target_radiant_win boolean;
                prediction_status text;
                cutoff_source text;
            BEGIN
                SELECT live_text_timestamp_utc(training_cutoff), availability_mode
                  INTO run_cutoff, run_mode
                  FROM team_rating_runs
                 WHERE run_id = NEW.run_id;
                IF run_cutoff IS NULL OR run_mode IS NULL THEN
                    RAISE EXCEPTION 'Team Rating parent run cutoff is unavailable';
                END IF;
                IF TG_TABLE_NAME = 'team_rating_predictions' THEN
                    prediction_at := live_text_timestamp_utc(
                        to_jsonb(NEW) ->> 'prediction_cutoff'
                    );
                    cutoff_source := to_jsonb(NEW) ->> 'cutoff_source';
                    prediction_status := to_jsonb(NEW) ->> 'status';
                    IF run_mode = 'reconstructed_walk_forward' AND
                       cutoff_source IS DISTINCT FROM 'reconstructed_map_start'
                    THEN
                        RAISE EXCEPTION
                            'Team Rating reconstructed cutoff source is invalid';
                    END IF;
                    IF run_mode = 'prospective' AND
                       (cutoff_source IS NULL OR
                        cutoff_source !~ '^prospective_[a-z0-9][a-z0-9_]*$')
                    THEN
                        RAISE EXCEPTION
                            'Team Rating prospective cutoff source is invalid';
                    END IF;
                    SELECT CASE
                               WHEN start_time IS NULL OR start_time <= 0
                               THEN NULL
                               ELSE to_timestamp(start_time)
                           END,
                           radiant_team_id,
                           dire_team_id,
                           radiant_win
                      INTO target_started_at,
                           target_radiant_team_id,
                           target_dire_team_id,
                           target_radiant_win
                      FROM matches
                     WHERE match_id =
                           (to_jsonb(NEW) ->> 'match_id')::bigint;
                    IF target_started_at IS NULL THEN
                        RAISE EXCEPTION
                            'Team Rating target map start is unavailable';
                    END IF;
                    IF target_radiant_team_id IS NULL OR
                       target_dire_team_id IS NULL
                    THEN
                        RAISE EXCEPTION
                            'Team Rating target team authority is unavailable';
                    END IF;
                    IF (to_jsonb(NEW) ->> 'radiant_team_id')::bigint <>
                           target_radiant_team_id OR
                       (to_jsonb(NEW) ->> 'dire_team_id')::bigint <>
                           target_dire_team_id
                    THEN
                        RAISE EXCEPTION
                            'Team Rating target team authority disagrees';
                    END IF;
                    IF prediction_status = 'settled' AND
                       (target_radiant_win IS NULL OR
                        (((to_jsonb(NEW) ->> 'eventual_radiant_win')::smallint = 1)
                         IS DISTINCT FROM target_radiant_win))
                    THEN
                        RAISE EXCEPTION
                            'Team Rating target result authority disagrees';
                    END IF;
                    IF prediction_at < run_cutoff THEN
                        RAISE EXCEPTION
                            'Team Rating prediction cutoff precedes training cutoff';
                    END IF;
                    IF prediction_at > target_started_at THEN
                        RAISE EXCEPTION
                            'Team Rating prediction cutoff follows target map start';
                    END IF;
                END IF;
                IF TG_TABLE_NAME = 'team_rating_state_snapshots' AND
                   live_text_timestamp_utc(to_jsonb(NEW) ->> 'as_of')
                       <> run_cutoff
                THEN
                    RAISE EXCEPTION
                        'Team Rating snapshot as_of must equal training cutoff';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER team_rating_predictions_cutoff_guard
            BEFORE INSERT ON team_rating_predictions
            FOR EACH ROW EXECUTE FUNCTION validate_team_rating_child_cutoff()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER team_rating_state_snapshots_cutoff_guard
            BEFORE INSERT ON team_rating_state_snapshots
            FOR EACH ROW EXECUTE FUNCTION validate_team_rating_child_cutoff()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_team_rating_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    for table in (
        "team_rating_runs",
        "team_rating_predictions",
        "team_rating_state_snapshots",
    ):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_team_rating_mutation()
                """
            )
        )


def downgrade() -> None:
    op.drop_table("team_rating_state_snapshots")
    op.drop_table("team_rating_predictions")
    op.drop_table("team_rating_runs")
    op.execute(sa.text("DROP FUNCTION IF EXISTS validate_team_rating_child_cutoff()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_team_rating_mutation()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS team_rating_roster_is_valid(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS team_rating_canonical_json(jsonb)"))
