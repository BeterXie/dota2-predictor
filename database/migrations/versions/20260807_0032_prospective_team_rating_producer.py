"""Add operational prospective Team Rating authority ledgers.

Revision ID: 20260807_0032
Revises: 20260806_0031
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0032"
down_revision: str | None = "20260806_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} ~ '^[0-9a-f]{{64}}$'")


def _optional_sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR {column} ~ '^[0-9a-f]{{64}}$'"
    )


def _utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"live_text_timestamp_utc({column}) IS NOT NULL AND "
        f"({column} LIKE '%Z' OR {column} LIKE '%+00:00')"
    )


def _optional_utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR "
        f"(live_text_timestamp_utc({column}) IS NOT NULL AND "
        f"({column} LIKE '%Z' OR {column} LIKE '%+00:00'))"
    )


def _canonical_object(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"jsonb_typeof({column}::jsonb)='object' AND "
        f"{column}=team_rating_canonical_json({column}::jsonb)"
    )


def _canonical_array(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"jsonb_typeof({column}::jsonb)='array' AND "
        f"{column}=team_rating_canonical_json({column}::jsonb)"
    )


def upgrade() -> None:
    op.create_table(
        "prospective_team_rating_seeds",
        sa.Column("seed_hash", sa.Text(), primary_key=True),
        sa.Column("rating_version", sa.Text(), nullable=False),
        sa.Column("configuration_json", sa.Text(), nullable=False),
        sa.Column("configuration_hash", sa.Text(), nullable=False),
        sa.Column("seed_as_of", sa.Text(), nullable=False),
        sa.Column("seed_training_cutoff", sa.Text(), nullable=False),
        sa.Column("source_manifest_json", sa.Text(), nullable=False),
        sa.Column("source_manifest_hash", sa.Text(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.Column("artifact_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("frozen_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("rating_version='team-rating-elo-v1'"),
        sa.CheckConstraint("seed_hash=artifact_hash"),
        sa.CheckConstraint(
            "live_text_timestamp_utc(seed_training_cutoff) <= "
            "live_text_timestamp_utc(seed_as_of) AND "
            "live_text_timestamp_utc(seed_as_of) <= "
            "live_text_timestamp_utc(frozen_at) AND "
            "live_text_timestamp_utc(frozen_at) <= "
            "live_text_timestamp_utc(created_at)"
        ),
        _canonical_object("configuration_json"),
        _canonical_array("source_manifest_json"),
        _canonical_array("state_json"),
        _canonical_object("artifact_json"),
        _sha256("seed_hash"),
        _sha256("configuration_hash"),
        _sha256("source_manifest_hash"),
        _sha256("state_hash"),
        _sha256("artifact_hash"),
        _utc("seed_as_of"),
        _utc("seed_training_cutoff"),
        _utc("frozen_at"),
        _utc("created_at"),
    )
    op.create_index(
        "idx_prospective_team_rating_seeds_frozen",
        "prospective_team_rating_seeds",
        [sa.text("frozen_at DESC")],
    )

    op.create_table(
        "prospective_team_rating_authorities",
        sa.Column("authority_hash", sa.Text(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("team_rating_runs.run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "prediction_id",
            sa.BigInteger(),
            sa.ForeignKey("team_rating_predictions.prediction_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column(
            "seed_hash",
            sa.Text(),
            sa.ForeignKey("prospective_team_rating_seeds.seed_hash"),
            nullable=False,
        ),
        sa.Column("configuration_hash", sa.Text(), nullable=False),
        sa.Column(
            "base_authority_hash",
            sa.Text(),
            sa.ForeignKey("prospective_team_rating_authorities.authority_hash"),
        ),
        sa.Column("base_as_of", sa.Text(), nullable=False),
        sa.Column("base_state_hash", sa.Text(), nullable=False),
        sa.Column("applied_result_manifest_json", sa.Text(), nullable=False),
        sa.Column("applied_result_manifest_hash", sa.Text(), nullable=False),
        sa.Column("state_before_json", sa.Text(), nullable=False),
        sa.Column("state_before_hash", sa.Text(), nullable=False),
        sa.Column("target_manifest_json", sa.Text(), nullable=False),
        sa.Column("target_manifest_hash", sa.Text(), nullable=False),
        sa.Column("training_input_hash", sa.Text(), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.Column("artifact_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("available_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("authority_hash=artifact_hash"),
        sa.CheckConstraint("match_id > 0"),
        sa.CheckConstraint("series_id > 0"),
        sa.CheckConstraint(
            "live_text_timestamp_utc(base_as_of) <= "
            "live_text_timestamp_utc(prediction_cutoff) AND "
            "live_text_timestamp_utc(created_at) <= "
            "live_text_timestamp_utc(available_at) AND "
            "live_text_timestamp_utc(available_at) < "
            "live_text_timestamp_utc(prediction_cutoff)"
        ),
        _canonical_array("applied_result_manifest_json"),
        _canonical_array("state_before_json"),
        _canonical_object("target_manifest_json"),
        _canonical_object("artifact_json"),
        _sha256("authority_hash"),
        _sha256("run_id"),
        _sha256("seed_hash"),
        _sha256("configuration_hash"),
        _optional_sha256("base_authority_hash"),
        _sha256("base_state_hash"),
        _sha256("applied_result_manifest_hash"),
        _sha256("state_before_hash"),
        _sha256("target_manifest_hash"),
        _sha256("training_input_hash"),
        _sha256("artifact_hash"),
        _utc("prediction_cutoff"),
        _utc("base_as_of"),
        _utc("available_at"),
        _utc("created_at"),
    )
    op.create_index(
        "idx_prospective_team_rating_authority_cutoff",
        "prospective_team_rating_authorities",
        ["seed_hash", sa.text("prediction_cutoff DESC"), "match_id"],
    )

    op.create_table(
        "prospective_team_rating_attempts",
        sa.Column("attempt_hash", sa.Text(), primary_key=True),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
        ),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempted_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("retry_at", sa.Text()),
        sa.Column("terminal", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("match_id", "attempt_number"),
        sa.CheckConstraint("match_id > 0"),
        sa.CheckConstraint("attempt_number > 0"),
        sa.CheckConstraint("length(trim(reason)) BETWEEN 1 AND 200"),
        sa.CheckConstraint("terminal IN (0, 1)"),
        sa.CheckConstraint(
            "live_text_timestamp_utc(attempted_at) <= "
            "live_text_timestamp_utc(created_at) AND "
            "(retry_at IS NULL OR "
            " live_text_timestamp_utc(attempted_at) < "
            " live_text_timestamp_utc(retry_at)) AND "
            "(terminal=0 OR retry_at IS NULL OR "
            " live_text_timestamp_utc(retry_at) <= "
            " live_text_timestamp_utc(prediction_cutoff))"
        ),
        _sha256("attempt_hash"),
        _utc("prediction_cutoff"),
        _utc("attempted_at"),
        _optional_utc("retry_at"),
        _utc("created_at"),
    )
    op.create_index(
        "idx_prospective_team_rating_attempt_retry",
        "prospective_team_rating_attempts",
        ["terminal", "retry_at", "match_id"],
    )

    op.create_table(
        "prospective_rosh_team_rating_failures",
        sa.Column("failure_hash", sa.Text(), primary_key=True),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
        ),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column("missing_reason", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("match_id", "prediction_cutoff", "missing_reason"),
        sa.CheckConstraint("match_id > 0"),
        sa.CheckConstraint(
            "missing_reason='prospective_team_rating_unavailable'"
        ),
        sa.CheckConstraint(
            "live_text_timestamp_utc(observed_at) <= "
            "live_text_timestamp_utc(created_at)"
        ),
        _sha256("failure_hash"),
        _utc("prediction_cutoff"),
        _utc("observed_at"),
        _utc("created_at"),
    )

    op.create_table(
        "prospective_team_rating_settlements",
        sa.Column("settlement_hash", sa.Text(), primary_key=True),
        sa.Column(
            "authority_hash",
            sa.Text(),
            sa.ForeignKey("prospective_team_rating_authorities.authority_hash"),
            nullable=False,
            unique=True,
        ),
        sa.Column("eventual_radiant_win", sa.SmallInteger(), nullable=False),
        sa.Column("result_artifact_hash", sa.Text(), nullable=False),
        sa.Column("result_usable_at", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("eventual_radiant_win IN (0, 1)"),
        sa.CheckConstraint(
            "live_text_timestamp_utc(result_usable_at) <= "
            "live_text_timestamp_utc(settled_at) AND "
            "live_text_timestamp_utc(settled_at) <= "
            "live_text_timestamp_utc(created_at)"
        ),
        _sha256("settlement_hash"),
        _sha256("authority_hash"),
        _sha256("result_artifact_hash"),
        _utc("result_usable_at"),
        _utc("settled_at"),
        _utc("created_at"),
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_prospective_team_rating_seed()
            RETURNS trigger AS $$
            DECLARE
                artifact jsonb;
                result_row jsonb;
                source_match_id bigint;
                source_outcome boolean;
                source_hash text;
                source_usable_at timestamptz;
                stored_outcome boolean;
                source_start timestamptz;
                source_duration integer;
                source_radiant_team_id bigint;
                source_dire_team_id bigint;
                source_series_id bigint;
                source_match_series_id bigint;
                source_event_id text;
            BEGIN
                IF NEW.configuration_hash <> encode(
                       sha256(convert_to(NEW.configuration_json, 'UTF8')), 'hex'
                   ) OR
                   NEW.source_manifest_hash <> encode(
                       sha256(convert_to(NEW.source_manifest_json, 'UTF8')), 'hex'
                   ) OR
                   NEW.state_hash <> encode(
                       sha256(convert_to(NEW.state_json, 'UTF8')), 'hex'
                   ) OR
                   NEW.artifact_hash <> encode(
                       sha256(convert_to(NEW.artifact_json, 'UTF8')), 'hex'
                   )
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating seed content hash disagrees';
                END IF;
                artifact := NEW.artifact_json::jsonb;
                IF artifact->>'version' <> 'prospective-team-rating-v1' OR
                   artifact->>'rating_version' <> NEW.rating_version OR
                   artifact->>'configuration_hash' <> NEW.configuration_hash OR
                   artifact->>'seed_as_of' <> NEW.seed_as_of OR
                   artifact->>'seed_training_cutoff' <>
                       NEW.seed_training_cutoff OR
                   artifact->>'source_manifest_hash' <>
                       NEW.source_manifest_hash OR
                   artifact->>'state_hash' <> NEW.state_hash OR
                   artifact->>'frozen_at' <> NEW.frozen_at OR
                   team_rating_canonical_json(artifact->'configuration') <>
                       NEW.configuration_json OR
                   team_rating_canonical_json(artifact->'source_manifest') <>
                       NEW.source_manifest_json OR
                   team_rating_canonical_json(artifact->'states') <>
                       NEW.state_json
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating seed artifact disagrees';
                END IF;
                FOR result_row IN
                    SELECT value
                      FROM jsonb_array_elements(
                           NEW.source_manifest_json::jsonb
                      ) AS value
                LOOP
                    source_match_id :=
                        (result_row->'result'->>'match_id')::bigint;
                    stored_outcome :=
                        (result_row->'result'->>'radiant_win')::boolean;
                    SELECT match_row.radiant_win,
                           status.latest_raw_content_hash,
                           live_text_timestamp_utc(status.first_usable_at),
                           to_timestamp(match_row.start_time),
                           match_row.duration, match_row.radiant_team_id,
                           match_row.dire_team_id, status.series_id,
                           match_row.series_id,
                           status.event_id
                      INTO source_outcome, source_hash, source_usable_at,
                           source_start, source_duration,
                           source_radiant_team_id, source_dire_team_id,
                           source_series_id, source_match_series_id,
                           source_event_id
                      FROM formal_map_eligibility AS eligible
                      JOIN matches AS match_row
                        ON match_row.match_id=eligible.match_id
                      JOIN match_ingest_status AS status
                        ON status.match_id=eligible.match_id
                     WHERE eligible.match_id=source_match_id;
                    IF source_outcome IS NULL OR
                       source_start IS NULL OR source_duration IS NULL OR
                       source_duration <= 0 OR
                       source_radiant_team_id IS NULL OR
                       source_dire_team_id IS NULL OR
                       source_outcome IS DISTINCT FROM stored_outcome OR
                       source_hash IS DISTINCT FROM
                           result_row->>'source_artifact_hash' OR
                       source_usable_at IS NULL OR
                       source_usable_at > live_text_timestamp_utc(
                           NEW.seed_training_cutoff
                       ) OR
                       live_text_timestamp_utc(
                           result_row->'result'->>'result_usable_at'
                       ) <> source_usable_at OR
                       live_text_timestamp_utc(
                           result_row->'result'->>'started_at'
                       ) <> source_start OR
                       live_text_timestamp_utc(
                           result_row->'result'->>'completed_at'
                       ) <> source_start +
                           make_interval(secs=>source_duration) OR
                       (result_row->'result'->>'radiant_team_id')::bigint <>
                           source_radiant_team_id OR
                       (result_row->'result'->>'dire_team_id')::bigint <>
                           source_dire_team_id OR
                       (result_row->'result'->>'series_id')::bigint
                           IS DISTINCT FROM source_series_id OR
                       source_match_series_id IS DISTINCT FROM
                           source_series_id OR
                       result_row->'result'->>'event_id' <>
                           source_event_id OR
                       live_text_timestamp_utc(result_row->>'observed_at') <
                           source_usable_at OR
                       live_text_timestamp_utc(result_row->>'observed_at') >
                           live_text_timestamp_utc(NEW.frozen_at)
                    THEN
                        RAISE EXCEPTION
                            'prospective Team Rating seed source authority disagrees';
                    END IF;
                END LOOP;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER prospective_team_rating_seeds_insert_guard
            BEFORE INSERT ON prospective_team_rating_seeds
            FOR EACH ROW EXECUTE FUNCTION validate_prospective_team_rating_seed()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_prospective_team_rating_authority()
            RETURNS trigger AS $$
            DECLARE
                artifact jsonb;
                run_row team_rating_runs%ROWTYPE;
                prediction_row team_rating_predictions%ROWTYPE;
                seed_row prospective_team_rating_seeds%ROWTYPE;
                base_row prospective_team_rating_authorities%ROWTYPE;
                target_row jsonb;
                target_start timestamptz;
                target_outcome boolean;
                target_has_result smallint;
                target_result_usable timestamptz;
                target_series_id bigint;
                target_ingest_series_id bigint;
                target_event_id text;
                result_row jsonb;
                result_match_id bigint;
                result_outcome boolean;
                result_hash text;
                result_usable timestamptz;
                stored_result_usable timestamptz;
                stored_result_start timestamptz;
                previous_result_usable timestamptz;
                previous_result_start timestamptz;
                previous_result_match_id bigint;
                result_duration integer;
                result_radiant_team_id bigint;
                result_dire_team_id bigint;
                result_series_id bigint;
                result_match_series_id bigint;
                result_event_id text;
                result_authority_start timestamptz;
                state_row jsonb;
                snapshot_row team_rating_state_snapshots%ROWTYPE;
            BEGIN
                IF NEW.applied_result_manifest_hash <> encode(
                       sha256(convert_to(
                           NEW.applied_result_manifest_json, 'UTF8'
                       )), 'hex'
                   ) OR
                   NEW.state_before_hash <> encode(
                       sha256(convert_to(NEW.state_before_json, 'UTF8')), 'hex'
                   ) OR
                   NEW.target_manifest_hash <> encode(
                       sha256(convert_to(NEW.target_manifest_json, 'UTF8')), 'hex'
                   ) OR
                   NEW.artifact_hash <> encode(
                       sha256(convert_to(NEW.artifact_json, 'UTF8')), 'hex'
                   )
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating authority content hash disagrees';
                END IF;

                SELECT * INTO run_row FROM team_rating_runs
                 WHERE run_id=NEW.run_id;
                SELECT * INTO prediction_row FROM team_rating_predictions
                 WHERE prediction_id=NEW.prediction_id;
                SELECT * INTO seed_row FROM prospective_team_rating_seeds
                 WHERE seed_hash=NEW.seed_hash;
                IF run_row.run_id IS NULL OR
                   run_row.availability_mode <> 'prospective' OR
                   run_row.status <> 'trained' OR
                   run_row.rating_version <> 'team-rating-elo-v1' OR
                   run_row.artifact_version <>
                       'prospective-team-rating-artifact-v1' OR
                   run_row.run_id <> encode(sha256(convert_to(
                       '{"artifact_hash":"' || NEW.artifact_hash ||
                       '","availability_mode":"prospective",' ||
                       '"schema":"prospective-team-rating-run/v1"}',
                       'UTF8'
                   )), 'hex') OR
                   run_row.training_input_hash <> NEW.training_input_hash OR
                   live_text_timestamp_utc(run_row.training_cutoff) <>
                       live_text_timestamp_utc(NEW.available_at) OR
                   run_row.configuration_json::jsonb->>'artifact_hash' <>
                       NEW.artifact_hash OR
                   prediction_row.prediction_id IS NULL OR
                   prediction_row.run_id <> NEW.run_id OR
                   prediction_row.match_id <> NEW.match_id OR
                   live_text_timestamp_utc(prediction_row.prediction_cutoff) <>
                       live_text_timestamp_utc(NEW.prediction_cutoff) OR
                   prediction_row.cutoff_source <>
                       'prospective_scheduled_start' OR
                   prediction_row.status <> 'predicted' OR
                   prediction_row.raw_probability IS NULL OR
                   prediction_row.eventual_radiant_win IS NOT NULL OR
                   seed_row.seed_hash IS NULL OR
                   seed_row.configuration_hash <> NEW.configuration_hash OR
                   live_text_timestamp_utc(seed_row.frozen_at) >=
                       live_text_timestamp_utc(NEW.prediction_cutoff)
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating parent authority disagrees';
                END IF;

                IF NEW.base_authority_hash IS NULL THEN
                    IF live_text_timestamp_utc(NEW.base_as_of) <>
                           live_text_timestamp_utc(seed_row.seed_as_of) OR
                       NEW.base_state_hash <> seed_row.state_hash
                    THEN
                        RAISE EXCEPTION
                            'prospective Team Rating seed state disagrees';
                    END IF;
                ELSE
                    SELECT * INTO base_row
                      FROM prospective_team_rating_authorities
                     WHERE authority_hash=NEW.base_authority_hash;
                    IF base_row.authority_hash IS NULL OR
                       base_row.seed_hash <> NEW.seed_hash OR
                       base_row.configuration_hash <> NEW.configuration_hash OR
                       live_text_timestamp_utc(base_row.available_at) <>
                           live_text_timestamp_utc(NEW.base_as_of) OR
                       base_row.state_before_hash <> NEW.base_state_hash
                    THEN
                        RAISE EXCEPTION
                            'prospective Team Rating recovered state disagrees';
                    END IF;
                END IF;

                target_row := NEW.target_manifest_json::jsonb;
                IF (target_row->'target'->>'match_id')::bigint <>
                       NEW.match_id OR
                   (target_row->'target'->>'series_id')::bigint IS DISTINCT FROM
                       NEW.series_id OR
                   target_row->>'prediction_cutoff' <>
                       NEW.prediction_cutoff OR
                   target_row->>'cutoff_source' <>
                       'prospective_scheduled_start' OR
                   (target_row->'target'->>'radiant_team_id')::bigint <>
                       prediction_row.radiant_team_id OR
                   (target_row->'target'->>'dire_team_id')::bigint <>
                       prediction_row.dire_team_id OR
                   live_text_timestamp_utc(
                       target_row->'target'->>'started_at'
                   ) <> live_text_timestamp_utc(NEW.prediction_cutoff)
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating target manifest disagrees';
                END IF;
                SELECT CASE WHEN target.start_time IS NULL OR
                                      target.start_time <= 0
                                 THEN NULL
                                 ELSE to_timestamp(target.start_time) END,
                       target.radiant_win, status.has_valid_result,
                       live_text_timestamp_utc(status.first_usable_at),
                       target.series_id, status.series_id, status.event_id
                  INTO target_start, target_outcome, target_has_result,
                       target_result_usable, target_series_id,
                       target_ingest_series_id, target_event_id
                  FROM matches AS target
                  JOIN match_ingest_status AS status
                    ON status.match_id=target.match_id
                  JOIN formal_events AS event ON event.event_id=status.event_id
                 WHERE target.match_id=NEW.match_id
                   AND status.stage_in_scope=1
                   AND status.is_exhibition=0
                   AND status.is_forfeit=0
                   AND status.is_void_remake=0
                   AND (status.stage_scope='main_event' OR
                        (status.stage_scope='internal_lcq' AND
                         event.include_internal_lcq=1))
                 FOR SHARE OF target, status;
                IF target_start IS NULL OR
                   live_text_timestamp_utc(NEW.prediction_cutoff) <>
                       target_start OR
                   target_series_id IS DISTINCT FROM NEW.series_id OR
                   target_ingest_series_id IS DISTINCT FROM NEW.series_id OR
                   target_row->'target'->>'event_id' <> target_event_id OR
                   target_outcome IS NOT NULL OR target_has_result <> 0 OR
                   target_result_usable IS NOT NULL
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating target timing is invalid';
                END IF;

                previous_result_usable := NULL;
                previous_result_start := NULL;
                previous_result_match_id := NULL;
                FOR result_row IN
                    SELECT value FROM jsonb_array_elements(
                        NEW.applied_result_manifest_json::jsonb
                    ) AS value
                LOOP
                    result_match_id :=
                        (result_row->'result'->>'match_id')::bigint;
                    stored_result_usable := live_text_timestamp_utc(
                        result_row->'result'->>'result_usable_at'
                    );
                    stored_result_start := live_text_timestamp_utc(
                        result_row->'result'->>'started_at'
                    );
                    IF result_match_id=NEW.match_id OR
                       stored_result_usable <=
                           live_text_timestamp_utc(NEW.base_as_of) OR
                       stored_result_usable >
                           live_text_timestamp_utc(NEW.prediction_cutoff) OR
                       (previous_result_usable IS NOT NULL AND
                        (stored_result_usable, stored_result_start,
                         result_match_id) <=
                        (previous_result_usable, previous_result_start,
                         previous_result_match_id))
                    THEN
                        RAISE EXCEPTION
                            'prospective Team Rating result order is invalid';
                    END IF;
                    SELECT match_row.radiant_win,
                           status.latest_raw_content_hash,
                           live_text_timestamp_utc(status.first_usable_at),
                           match_row.duration, match_row.radiant_team_id,
                           match_row.dire_team_id, status.series_id,
                           match_row.series_id, status.event_id,
                           to_timestamp(match_row.start_time)
                      INTO result_outcome, result_hash, result_usable,
                           result_duration, result_radiant_team_id,
                           result_dire_team_id, result_series_id,
                           result_match_series_id, result_event_id,
                           result_authority_start
                      FROM formal_map_eligibility AS eligible
                      JOIN matches AS match_row
                        ON match_row.match_id=eligible.match_id
                      JOIN match_ingest_status AS status
                        ON status.match_id=eligible.match_id
                     WHERE eligible.match_id=result_match_id;
                    IF result_outcome IS NULL OR
                       result_authority_start IS NULL OR
                       result_duration IS NULL OR result_duration <= 0 OR
                       result_radiant_team_id IS NULL OR
                       result_dire_team_id IS NULL OR
                       result_outcome IS DISTINCT FROM
                           (result_row->'result'->>'radiant_win')::boolean OR
                       result_hash IS DISTINCT FROM
                           result_row->>'source_artifact_hash' OR
                       result_usable IS DISTINCT FROM stored_result_usable OR
                       stored_result_start <> result_authority_start OR
                       live_text_timestamp_utc(
                           result_row->'result'->>'completed_at'
                       ) <> stored_result_start +
                           make_interval(secs=>result_duration) OR
                       (result_row->'result'->>'radiant_team_id')::bigint <>
                           result_radiant_team_id OR
                       (result_row->'result'->>'dire_team_id')::bigint <>
                           result_dire_team_id OR
                       (result_row->'result'->>'series_id')::bigint
                           IS DISTINCT FROM result_series_id OR
                       result_match_series_id IS DISTINCT FROM
                           result_series_id OR
                       result_row->'result'->>'event_id' <> result_event_id OR
                       result_usable > live_text_timestamp_utc(
                           result_row->>'observed_at'
                       ) OR
                       live_text_timestamp_utc(result_row->>'observed_at') >=
                           live_text_timestamp_utc(NEW.prediction_cutoff)
                    THEN
                        RAISE EXCEPTION
                            'prospective Team Rating result authority disagrees';
                    END IF;
                    previous_result_usable := stored_result_usable;
                    previous_result_start := stored_result_start;
                    previous_result_match_id := result_match_id;
                END LOOP;

                FOR state_row IN
                    SELECT value FROM jsonb_array_elements(
                        NEW.state_before_json::jsonb
                    ) AS value
                LOOP
                    SELECT * INTO snapshot_row
                      FROM team_rating_state_snapshots
                     WHERE run_id=NEW.run_id
                       AND team_id=(state_row->>'team_id')::bigint;
                    IF snapshot_row.snapshot_key IS NULL OR
                       snapshot_row.rating IS DISTINCT FROM
                           (state_row->>'rating')::double precision OR
                       snapshot_row.maps_seen IS DISTINCT FROM
                           (state_row->>'maps_seen')::integer OR
                       snapshot_row.roster_json <>
                           team_rating_canonical_json(state_row->'roster') OR
                       snapshot_row.state_hash <> encode(sha256(convert_to(
                           team_rating_canonical_json(jsonb_build_object(
                               'rating_version', 'team-rating-elo-v1',
                               'state', state_row
                           )), 'UTF8')), 'hex')
                    THEN
                        RAISE EXCEPTION
                            'prospective Team Rating state snapshot disagrees';
                    END IF;
                END LOOP;

                artifact := NEW.artifact_json::jsonb;
                IF artifact->>'version' <> 'prospective-team-rating-v1' OR
                   artifact->>'artifact_version' <>
                       'prospective-team-rating-artifact-v1' OR
                   artifact->>'availability_mode' <> 'prospective' OR
                   artifact->>'seed_hash' <> NEW.seed_hash OR
                   artifact->>'configuration_hash' <> NEW.configuration_hash OR
                   artifact->>'base_state_hash' <> NEW.base_state_hash OR
                   artifact->>'applied_result_manifest_hash' <>
                       NEW.applied_result_manifest_hash OR
                   artifact->>'state_before_hash' <> NEW.state_before_hash OR
                   artifact->>'target_manifest_hash' <>
                       NEW.target_manifest_hash OR
                   artifact->>'training_input_hash' <> NEW.training_input_hash OR
                   (artifact->'prediction'->>'match_id')::bigint <>
                       NEW.match_id OR
                   artifact->'prediction'->>'input_hash' <>
                       prediction_row.input_hash OR
                   abs((artifact->'prediction'->>'raw_probability')::double precision -
                       prediction_row.raw_probability) > 1e-12 OR
                   team_rating_canonical_json(artifact->'applied_results') <>
                       NEW.applied_result_manifest_json OR
                   team_rating_canonical_json(artifact->'state_before_target') <>
                       NEW.state_before_json OR
                   team_rating_canonical_json(artifact->'target') <>
                       NEW.target_manifest_json
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating artifact replay disagrees';
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
            CREATE TRIGGER prospective_team_rating_authorities_insert_guard
            BEFORE INSERT ON prospective_team_rating_authorities
            FOR EACH ROW
            EXECUTE FUNCTION validate_prospective_team_rating_authority()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_prospective_team_rating_settlement()
            RETURNS trigger AS $$
            DECLARE
                target_match_id bigint;
                target_cutoff timestamptz;
                target_outcome boolean;
                target_start timestamptz;
                target_duration integer;
                target_has_result smallint;
                target_result_state text;
                target_result_usable timestamptz;
                target_result_hash text;
                prediction_outcome smallint;
                prediction_status text;
            BEGIN
                IF NEW.settlement_hash <> encode(sha256(convert_to(
                       '{"authority_hash":"' || NEW.authority_hash ||
                       '","eventual_radiant_win":' ||
                       CASE WHEN NEW.eventual_radiant_win=1
                            THEN 'true' ELSE 'false' END ||
                       ',"result_artifact_hash":"' ||
                       NEW.result_artifact_hash ||
                       '","result_usable_at":"' || NEW.result_usable_at ||
                       '","settled_at":"' || NEW.settled_at ||
                       '","version":"prospective-team-rating-v1"}',
                       'UTF8'
                   )), 'hex')
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating settlement hash disagrees';
                END IF;
                SELECT authority.match_id,
                       live_text_timestamp_utc(authority.prediction_cutoff),
                       prediction.eventual_radiant_win, prediction.status
                  INTO target_match_id, target_cutoff,
                       prediction_outcome, prediction_status
                  FROM prospective_team_rating_authorities AS authority
                  JOIN team_rating_predictions AS prediction
                    ON prediction.prediction_id=authority.prediction_id
                 WHERE authority.authority_hash=NEW.authority_hash;
                SELECT target.radiant_win,
                       CASE WHEN target.start_time IS NULL OR
                                      target.start_time <= 0
                            THEN NULL ELSE to_timestamp(target.start_time) END,
                       target.duration, status.has_valid_result,
                       status.basic_result_state,
                       live_text_timestamp_utc(status.first_usable_at),
                       status.latest_raw_content_hash
                  INTO target_outcome, target_start, target_duration,
                       target_has_result, target_result_state,
                       target_result_usable, target_result_hash
                  FROM matches AS target
                  JOIN match_ingest_status AS status
                    ON status.match_id=target.match_id
                 WHERE target.match_id=target_match_id
                 FOR SHARE OF target, status;
                IF target_match_id IS NULL OR target_outcome IS NULL OR
                   target_start IS NULL OR target_duration IS NULL OR
                   target_duration <= 0 OR target_has_result <> 1 OR
                   target_result_state <> 'ready' OR
                   target_result_usable IS NULL OR target_result_hash IS NULL OR
                   prediction_status <> 'predicted' OR
                   prediction_outcome IS NOT NULL OR
                   (NEW.eventual_radiant_win=1) IS DISTINCT FROM target_outcome OR
                   NEW.result_artifact_hash <> target_result_hash OR
                   live_text_timestamp_utc(NEW.result_usable_at) <>
                       target_result_usable OR
                   target_result_usable <
                       target_start + make_interval(secs=>target_duration) OR
                   target_result_usable <= target_cutoff
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating settlement authority disagrees';
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
            CREATE TRIGGER prospective_team_rating_settlements_insert_guard
            BEFORE INSERT ON prospective_team_rating_settlements
            FOR EACH ROW
            EXECUTE FUNCTION validate_prospective_team_rating_settlement()
            """
        )
    )

    for table in (
        "prospective_team_rating_seeds",
        "prospective_team_rating_authorities",
        "prospective_team_rating_attempts",
        "prospective_rosh_team_rating_failures",
        "prospective_team_rating_settlements",
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
    op.drop_table("prospective_team_rating_settlements")
    op.drop_table("prospective_rosh_team_rating_failures")
    op.drop_table("prospective_team_rating_attempts")
    op.drop_table("prospective_team_rating_authorities")
    op.drop_table("prospective_team_rating_seeds")
    op.execute(
        sa.text("DROP FUNCTION validate_prospective_team_rating_settlement()")
    )
    op.execute(
        sa.text("DROP FUNCTION validate_prospective_team_rating_authority()")
    )
    op.execute(sa.text("DROP FUNCTION validate_prospective_team_rating_seed()"))
