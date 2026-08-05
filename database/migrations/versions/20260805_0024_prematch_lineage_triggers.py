"""Install prematch lineage, validation, and settlement triggers.

Revision ID: 20260805_0024
Revises: 20260805_0023
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "20260805_0024"
down_revision: str | None = "20260805_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MATCH_DEPENDENCY_TABLES = (
    "match_ingest_status",
    "matches",
    "player_map_facts",
    "match_players",
    "picks_bans",
    "player_role_assignments",
    "player_map_scores",
    "team_map_states",
    "team_rating_predictions",
)
DEPENDENCY_TABLES = (
    "event_registry",
    "heroes",
    "raw_source_artifacts",
    "raw_source_observations",
    "team_rating_runs",
    "rosh_analysis_runs",
    "rosh_hero_scores",
    "rosh_minute_points",
    "rosh_run_match_links",
    *MATCH_DEPENDENCY_TABLES,
)
ARTIFACT_TABLES = (
    "prematch_model_runs",
    "prematch_predictions",
    "prematch_calibration_artifacts",
)
APPEND_ONLY_TABLES = (
    "prematch_model_runs",
    "prematch_calibration_artifacts",
    "prematch_prediction_validations",
)


def _trigger_name(kind: str, table_name: str, operation: str) -> str:
    return f"prematch_lineage_{kind}_{table_name}_{operation.lower()}"


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE FUNCTION prematch_lineage_revision_is_current(
                claimed_revision bigint,
                prediction_cutoff text
            ) RETURNS boolean AS $$
            DECLARE
                current_revision bigint;
                cutoff_unix bigint;
            BEGIN
                SELECT dependency_revision
                  INTO current_revision
                  FROM prematch_lineage_revisions
                 WHERE singleton = 1;
                cutoff_unix := CAST(EXTRACT(EPOCH FROM
                    live_text_timestamp_utc(prediction_cutoff)) AS bigint);
                IF current_revision IS NULL OR cutoff_unix IS NULL OR
                   claimed_revision < 1 OR claimed_revision > current_revision
                THEN
                    RETURN false;
                END IF;
                RETURN NOT EXISTS (
                    SELECT 1
                      FROM prematch_lineage_changes AS change
                     WHERE change.dependency_revision > claimed_revision
                       AND (
                           change.affected_from_unix IS NULL OR
                           change.affected_from_unix <= cutoff_unix
                       )
                );
            END;
            $$ LANGUAGE plpgsql STABLE STRICT
            """
        )
    )
    op.execute(
        text(
            """
            CREATE FUNCTION advance_prematch_dependency_revision()
            RETURNS trigger AS $$
            DECLARE
                next_revision bigint;
                changed_at_text text;
                affected_from bigint;
                target_match_id bigint;
                target_event_id text;
                target_run_id text;
                new_row jsonb;
                old_row jsonb;
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

                IF TG_TABLE_NAME = 'heroes' THEN
                    affected_from := NULL;
                ELSIF TG_TABLE_NAME = 'event_registry' THEN
                    target_event_id := COALESCE(
                        new_row->>'event_id', old_row->>'event_id'
                    );
                    SELECT MIN(COALESCE(match.start_time, status.start_time))
                      INTO affected_from
                      FROM match_ingest_status AS status
                      LEFT JOIN matches AS match
                        ON match.match_id = status.match_id
                     WHERE status.event_id = target_event_id;
                    affected_from := COALESCE(affected_from, no_impact);
                ELSIF TG_TABLE_NAME IN (
                    'raw_source_artifacts', 'raw_source_observations'
                ) THEN
                    SELECT MIN(COALESCE(match.start_time, status.start_time))
                      INTO affected_from
                      FROM match_ingest_status AS status
                      LEFT JOIN matches AS match
                        ON match.match_id = status.match_id
                     WHERE (
                         new_row IS NOT NULL AND
                         status.latest_raw_artifact_id = new_row->>'artifact_id' AND
                         status.latest_raw_content_hash = new_row->>'content_hash'
                     ) OR (
                         old_row IS NOT NULL AND
                         status.latest_raw_artifact_id = old_row->>'artifact_id' AND
                         status.latest_raw_content_hash = old_row->>'content_hash'
                     );
                    affected_from := COALESCE(affected_from, no_impact);
                ELSIF TG_TABLE_NAME = 'team_rating_runs' THEN
                    affected_from := CAST(EXTRACT(EPOCH FROM
                        live_text_timestamp_utc(COALESCE(
                            new_row->>'training_cutoff',
                            old_row->>'training_cutoff'
                        ))) AS bigint);
                ELSIF TG_TABLE_NAME = 'rosh_analysis_runs' THEN
                    affected_from := COALESCE(
                        (new_row->>'date_time')::bigint,
                        (old_row->>'date_time')::bigint,
                        no_impact
                    );
                ELSIF TG_TABLE_NAME IN ('rosh_hero_scores', 'rosh_minute_points') THEN
                    target_run_id := COALESCE(
                        new_row->>'run_id', old_row->>'run_id'
                    );
                    SELECT date_time INTO affected_from
                      FROM rosh_analysis_runs
                     WHERE run_id = target_run_id;
                    affected_from := COALESCE(affected_from, no_impact);
                ELSIF TG_TABLE_NAME = 'rosh_run_match_links' THEN
                    target_run_id := COALESCE(
                        new_row->>'run_id', old_row->>'run_id'
                    );
                    SELECT date_time INTO affected_from
                      FROM rosh_analysis_runs
                     WHERE run_id = target_run_id;
                    affected_from := COALESCE(affected_from, no_impact);
                ELSE
                    target_match_id := COALESCE(
                        (new_row->>'match_id')::bigint,
                        (old_row->>'match_id')::bigint
                    );
                    IF TG_TABLE_NAME = 'matches' THEN
                        SELECT MIN(candidate.start_time)
                          INTO affected_from
                          FROM (VALUES
                              ((new_row->>'start_time')::bigint),
                              ((old_row->>'start_time')::bigint)
                          ) AS candidate(start_time)
                         WHERE candidate.start_time > 0;
                    ELSIF TG_TABLE_NAME = 'team_rating_predictions' THEN
                        affected_from := CAST(EXTRACT(EPOCH FROM
                            live_text_timestamp_utc(COALESCE(
                                new_row->>'prediction_cutoff',
                                old_row->>'prediction_cutoff'
                            ))) AS bigint);
                    ELSE
                        SELECT COALESCE(match.start_time, status.start_time)
                          INTO affected_from
                          FROM match_ingest_status AS status
                          LEFT JOIN matches AS match
                            ON match.match_id = status.match_id
                         WHERE status.match_id = target_match_id;
                    END IF;
                    affected_from := COALESCE(affected_from, no_impact);
                END IF;

                changed_at_text := replace(
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                    '_', ':'
                );
                UPDATE prematch_lineage_revisions
                   SET dependency_revision = dependency_revision + 1,
                       updated_at = changed_at_text
                 WHERE singleton = 1
                 RETURNING dependency_revision INTO next_revision;
                IF next_revision IS NULL THEN
                    RAISE EXCEPTION 'prematch lineage revision authority is missing';
                END IF;
                INSERT INTO prematch_lineage_changes (
                    dependency_revision, affected_from_unix,
                    source_relation, operation, changed_at
                ) VALUES (
                    next_revision, affected_from,
                    TG_TABLE_NAME, TG_OP, changed_at_text
                );
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        text(
            """
            CREATE FUNCTION advance_prematch_artifact_revision()
            RETURNS trigger AS $$
            BEGIN
                UPDATE prematch_lineage_revisions
                   SET artifact_revision = artifact_revision + 1,
                       updated_at = replace(
                           to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                           '_', ':'
                       )
                 WHERE singleton = 1;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'prematch lineage revision authority is missing';
                END IF;
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        text(
            """
            CREATE FUNCTION prematch_prediction_claims_are_consistent(
                prediction prematch_predictions
            ) RETURNS boolean AS $$
            DECLARE
                payload jsonb;
                parent_model_hash text;
                parent_mode text;
                calibration_model_hash text;
                calibration_mode text;
                calibration_status text;
                calibration_cutoff timestamptz;
                calibration_parameters jsonb;
                artifact_parameters jsonb;
                team_base_logit double precision;
                expected_team_base_probability double precision;
                expected_total_adjustment double precision;
                expected_raw_probability double precision;
                clipped_raw_probability double precision;
                raw_logit double precision;
                calibration_a double precision;
                calibration_b double precision;
                calibration_score double precision;
                expected_calibrated_probability double precision;
                expected_artifact_fingerprint text;
                numeric_tolerance CONSTANT double precision := 1e-12;
                probability_epsilon CONSTANT double precision := 1e-15;
            BEGIN
                SELECT model_hash, availability_mode
                  INTO parent_model_hash, parent_mode
                  FROM prematch_model_runs
                 WHERE run_id = prediction.run_id;
                IF parent_model_hash IS NULL OR parent_mode IS NULL THEN
                    RETURN false;
                END IF;

                payload := prediction.prediction_json::jsonb;
                IF (
                    jsonb_typeof(payload) = 'object' AND
                    jsonb_typeof(payload->'model_hash') = 'string' AND
                    payload->>'model_hash' = parent_model_hash AND
                    jsonb_typeof(payload->'input_snapshot_hash') = 'string' AND
                    payload->>'input_snapshot_hash' =
                        prediction.input_snapshot_hash AND
                    jsonb_typeof(payload->'status') = 'string' AND
                    payload->>'status' = prediction.status AND
                    jsonb_typeof(payload->'support') = 'number' AND
                    (payload->>'support')::integer = prediction.support AND
                    (
                        (prediction.raw_probability IS NULL AND
                         jsonb_typeof(payload->'raw_probability') = 'null') OR
                        (prediction.raw_probability IS NOT NULL AND
                         jsonb_typeof(payload->'raw_probability') = 'number' AND
                         abs((payload->>'raw_probability')::double precision -
                             prediction.raw_probability) <= numeric_tolerance)
                    ) AND
                    (
                        (prediction.parameter_uncertainty IS NULL AND
                         jsonb_typeof(payload->'parameter_uncertainty') = 'null') OR
                        (prediction.parameter_uncertainty IS NOT NULL AND
                         jsonb_typeof(payload->'parameter_uncertainty') = 'number' AND
                         abs((payload->>'parameter_uncertainty')::double precision -
                             prediction.parameter_uncertainty) <= numeric_tolerance)
                    ) AND
                    (
                        (prediction.draft_logit_delta IS NULL AND
                         jsonb_typeof(payload->'draft_logit_delta') = 'null') OR
                        (prediction.draft_logit_delta IS NOT NULL AND
                         jsonb_typeof(payload->'draft_logit_delta') = 'number' AND
                         abs((payload->>'draft_logit_delta')::double precision -
                             prediction.draft_logit_delta) <= numeric_tolerance)
                    ) AND
                    (
                        (prediction.rosh_logit_delta IS NULL AND
                         jsonb_typeof(payload->'rosh_logit_delta') = 'null') OR
                        (prediction.rosh_logit_delta IS NOT NULL AND
                         jsonb_typeof(payload->'rosh_logit_delta') = 'number' AND
                         abs((payload->>'rosh_logit_delta')::double precision -
                             prediction.rosh_logit_delta) <= numeric_tolerance)
                    ) AND
                    (
                        (prediction.cluster_logit_delta IS NULL AND
                         jsonb_typeof(payload->'cluster_logit_delta') = 'null') OR
                        (prediction.cluster_logit_delta IS NOT NULL AND
                         jsonb_typeof(payload->'cluster_logit_delta') = 'number' AND
                         abs((payload->>'cluster_logit_delta')::double precision -
                             prediction.cluster_logit_delta) <= numeric_tolerance)
                    ) AND
                    (
                        (prediction.total_adjustment IS NULL AND
                         jsonb_typeof(payload->'total_adjustment') = 'null') OR
                        (prediction.total_adjustment IS NOT NULL AND
                         jsonb_typeof(payload->'total_adjustment') = 'number' AND
                         abs((payload->>'total_adjustment')::double precision -
                             prediction.total_adjustment) <= numeric_tolerance)
                    )
                ) IS NOT TRUE THEN
                    RETURN false;
                END IF;
                expected_artifact_fingerprint := encode(
                    sha256(convert_to(
                        '{"calibration_hash":' ||
                        CASE
                            WHEN prediction.calibration_hash IS NULL THEN 'null'
                            ELSE '"' || prediction.calibration_hash || '"'
                        END ||
                        ',"domain":"prematch-artifact-fingerprint/v1",' ||
                        '"model_hash":"' || parent_model_hash || '"}',
                        'UTF8'
                    )),
                    'hex'
                );
                IF prediction.artifact_fingerprint IS DISTINCT FROM
                   expected_artifact_fingerprint
                THEN
                    RETURN false;
                END IF;

                IF jsonb_typeof(payload->'team_base_logit') IS DISTINCT FROM 'number'
                THEN
                    RETURN false;
                END IF;
                team_base_logit :=
                    (payload->>'team_base_logit')::double precision;
                IF (
                    team_base_logit > '-Infinity'::double precision AND
                    team_base_logit < 'Infinity'::double precision
                ) IS NOT TRUE THEN
                    RETURN false;
                END IF;
                IF team_base_logit >= 0.0 THEN
                    expected_team_base_probability :=
                        1.0 / (1.0 + exp(-team_base_logit));
                ELSE
                    expected_team_base_probability :=
                        exp(team_base_logit) / (1.0 + exp(team_base_logit));
                END IF;
                expected_team_base_probability := least(
                    greatest(expected_team_base_probability, probability_epsilon),
                    1.0 - probability_epsilon
                );
                IF abs(expected_team_base_probability -
                       prediction.team_base_probability) > numeric_tolerance
                THEN
                    RETURN false;
                END IF;

                IF prediction.raw_probability IS NOT NULL THEN
                    IF (
                        jsonb_typeof(payload->'learned_intercept') = 'number' AND
                        prediction.total_adjustment IS NOT NULL
                    ) IS NOT TRUE THEN
                        RETURN false;
                    END IF;
                    expected_total_adjustment :=
                        (payload->>'learned_intercept')::double precision +
                        COALESCE(prediction.draft_logit_delta, 0.0) +
                        COALESCE(prediction.rosh_logit_delta, 0.0) +
                        COALESCE(prediction.cluster_logit_delta, 0.0);
                    IF abs(expected_total_adjustment -
                           prediction.total_adjustment) > numeric_tolerance
                    THEN
                        RETURN false;
                    END IF;
                    IF team_base_logit + prediction.total_adjustment >= 0.0 THEN
                        expected_raw_probability := 1.0 / (
                            1.0 + exp(-(
                                team_base_logit + prediction.total_adjustment
                            ))
                        );
                    ELSE
                        expected_raw_probability := exp(
                            team_base_logit + prediction.total_adjustment
                        ) / (1.0 + exp(
                            team_base_logit + prediction.total_adjustment
                        ));
                    END IF;
                    IF abs(expected_raw_probability -
                           prediction.raw_probability) > numeric_tolerance
                    THEN
                        RETURN false;
                    END IF;
                ELSIF (
                    jsonb_typeof(payload->'learned_intercept') != 'null' OR
                    prediction.parameter_uncertainty IS NOT NULL OR
                    prediction.draft_logit_delta IS NOT NULL OR
                    prediction.rosh_logit_delta IS NOT NULL OR
                    prediction.cluster_logit_delta IS NOT NULL OR
                    prediction.total_adjustment IS NOT NULL
                ) IS NOT FALSE THEN
                    RETURN false;
                END IF;

                IF prediction.calibration_hash IS NULL THEN
                    RETURN prediction.calibrated_probability IS NULL;
                END IF;
                IF prediction.raw_probability IS NULL OR
                   prediction.calibrated_probability IS NULL
                THEN
                    RETURN false;
                END IF;
                SELECT calibration.model_hash,
                       calibration.artifact_json::jsonb->>'availability_mode',
                       calibration.status,
                       live_text_timestamp_utc(calibration.evaluation_cutoff),
                       calibration.parameters_json::jsonb,
                       calibration.artifact_json::jsonb->'parameters'
                  INTO calibration_model_hash, calibration_mode,
                       calibration_status, calibration_cutoff,
                       calibration_parameters, artifact_parameters
                  FROM prematch_calibration_artifacts AS calibration
                 WHERE calibration.calibration_hash =
                       prediction.calibration_hash;
                IF (
                    calibration_model_hash = parent_model_hash AND
                    calibration_mode = parent_mode AND
                    calibration_status IN (
                        'provisional', 'reconstructed_only',
                        'shadow_collecting', 'passed'
                    ) AND
                    calibration_cutoff <
                        live_text_timestamp_utc(prediction.prediction_cutoff) AND
                    jsonb_typeof(calibration_parameters) = 'object' AND
                    calibration_parameters - ARRAY['a', 'b'] = '{}'::jsonb AND
                    jsonb_typeof(calibration_parameters->'a') = 'number' AND
                    jsonb_typeof(calibration_parameters->'b') = 'number' AND
                    calibration_parameters = artifact_parameters
                ) IS NOT TRUE THEN
                    RETURN false;
                END IF;
                calibration_a :=
                    (calibration_parameters->>'a')::double precision;
                calibration_b :=
                    (calibration_parameters->>'b')::double precision;
                IF (
                    calibration_a > '-Infinity'::double precision AND
                    calibration_a < 'Infinity'::double precision AND
                    calibration_b > '-Infinity'::double precision AND
                    calibration_b < 'Infinity'::double precision
                ) IS NOT TRUE THEN
                    RETURN false;
                END IF;
                clipped_raw_probability := least(
                    1.0 - probability_epsilon,
                    greatest(probability_epsilon, prediction.raw_probability)
                );
                raw_logit := ln(clipped_raw_probability) -
                    ln(1.0 - clipped_raw_probability);
                calibration_score := calibration_a + calibration_b * raw_logit;
                IF (
                    calibration_score > '-Infinity'::double precision AND
                    calibration_score < 'Infinity'::double precision
                ) IS NOT TRUE THEN
                    RETURN false;
                END IF;
                IF calibration_score >= 0.0 THEN
                    expected_calibrated_probability :=
                        1.0 / (1.0 + exp(-calibration_score));
                ELSE
                    expected_calibrated_probability :=
                        exp(calibration_score) / (1.0 + exp(calibration_score));
                END IF;
                RETURN abs(expected_calibrated_probability -
                           prediction.calibrated_probability) <= numeric_tolerance;
            END;
            $$ LANGUAGE plpgsql STABLE STRICT
            """
        )
    )
    op.execute(
        text(
            """
            CREATE FUNCTION guard_prematch_prediction_mutation()
            RETURNS trigger AS $$
            DECLARE
                run_cutoff timestamptz;
                run_mode text;
                parent_model_hash text;
                prediction_at timestamptz;
                target_started_at timestamptz;
                target_completed_at timestamptz;
                target_radiant_win boolean;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'prematch_predictions is append-only';
                END IF;
                IF TG_OP = 'UPDATE' THEN
                    IF OLD.status != 'predicted' OR NEW.status != 'settled' OR
                       OLD.eventual_radiant_win IS NOT NULL OR
                       NEW.eventual_radiant_win IS NULL OR
                       NEW.result_usable_at IS NULL OR NEW.settled_at IS NULL OR
                       (to_jsonb(NEW) - ARRAY[
                           'eventual_radiant_win', 'result_usable_at',
                           'settled_at', 'status'
                       ]) IS DISTINCT FROM
                       (to_jsonb(OLD) - ARRAY[
                           'eventual_radiant_win', 'result_usable_at',
                           'settled_at', 'status'
                       ])
                    THEN
                        RAISE EXCEPTION
                            'prematch prediction update is not a settlement transition';
                    END IF;
                    SELECT radiant_win,
                           CASE WHEN start_time IS NULL OR start_time <= 0 OR
                                          duration IS NULL OR duration <= 0
                                THEN NULL
                                ELSE to_timestamp(start_time + duration)
                           END
                      INTO target_radiant_win, target_completed_at
                      FROM matches WHERE match_id = NEW.match_id;
                    IF target_radiant_win IS NULL OR
                       ((NEW.eventual_radiant_win = 1)
                           IS DISTINCT FROM target_radiant_win)
                    THEN
                        RAISE EXCEPTION
                            'prematch target result authority disagrees';
                    END IF;
                    IF target_completed_at IS NULL OR
                       live_text_timestamp_utc(NEW.result_usable_at)
                           < target_completed_at OR
                       live_text_timestamp_utc(NEW.settled_at)
                           < live_text_timestamp_utc(NEW.result_usable_at)
                    THEN
                        RAISE EXCEPTION
                            'prematch settlement timing is invalid';
                    END IF;
                    RETURN NEW;
                END IF;

                IF NEW.status = 'settled' THEN
                    RAISE EXCEPTION
                        'prematch predictions must transition to settled';
                END IF;
                SELECT live_text_timestamp_utc(training_cutoff),
                       availability_mode, model_hash
                  INTO run_cutoff, run_mode, parent_model_hash
                  FROM prematch_model_runs WHERE run_id = NEW.run_id;
                IF run_cutoff IS NULL OR run_mode IS NULL OR
                   parent_model_hash IS NULL
                THEN
                    RAISE EXCEPTION 'prematch parent model run is unavailable';
                END IF;
                prediction_at := live_text_timestamp_utc(NEW.prediction_cutoff);
                SELECT CASE WHEN start_time IS NULL OR start_time <= 0
                            THEN NULL ELSE to_timestamp(start_time) END
                  INTO target_started_at
                  FROM matches WHERE match_id = NEW.match_id;
                IF target_started_at IS NULL THEN
                    RAISE EXCEPTION 'prematch target map start is unavailable';
                END IF;
                IF prediction_at < run_cutoff OR prediction_at > target_started_at THEN
                    RAISE EXCEPTION 'prematch prediction cutoff is invalid';
                END IF;
                IF run_mode = 'reconstructed_walk_forward' AND
                   NEW.cutoff_source != 'reconstructed_map_start'
                THEN
                    RAISE EXCEPTION 'prematch reconstructed cutoff source is invalid';
                END IF;
                IF run_mode = 'prospective' AND
                   NEW.cutoff_source !~ '^prospective_[a-z0-9][a-z0-9_]*$'
                THEN
                    RAISE EXCEPTION 'prematch prospective cutoff source is invalid';
                END IF;
                IF NOT prematch_lineage_revision_is_current(
                    NEW.dependency_revision, NEW.prediction_cutoff
                ) THEN
                    RAISE EXCEPTION 'prematch dependency revision is stale';
                END IF;
                IF NOT prematch_prediction_claims_are_consistent(NEW) THEN
                    RAISE EXCEPTION 'prematch prediction artifact disagrees';
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
            CREATE FUNCTION validate_prematch_prediction_lineage()
            RETURNS trigger AS $$
            DECLARE
                prediction prematch_predictions%ROWTYPE;
            BEGIN
                SELECT * INTO prediction
                  FROM prematch_predictions
                 WHERE run_id = NEW.run_id AND match_id = NEW.match_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'prematch prediction is unavailable';
                END IF;
                IF NOT prematch_prediction_claims_are_consistent(prediction) THEN
                    RAISE EXCEPTION
                        'prematch prediction claims are inconsistent';
                END IF;
                IF NEW.validation_version != 'prematch-input-lineage-v1' OR
                   NEW.input_snapshot_hash IS DISTINCT FROM
                       prediction.input_snapshot_hash OR
                   NEW.artifact_fingerprint IS DISTINCT FROM
                       prediction.artifact_fingerprint OR
                   NEW.dependency_fingerprint IS DISTINCT FROM
                       prediction.dependency_fingerprint OR
                   NEW.dependency_revision IS DISTINCT FROM
                       prediction.dependency_revision
                THEN
                    RAISE EXCEPTION 'prematch validation claims disagree';
                END IF;
                IF NOT prematch_lineage_revision_is_current(
                    NEW.dependency_revision, prediction.prediction_cutoff
                ) THEN
                    RAISE EXCEPTION 'prematch validation dependency is stale';
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
            CREATE FUNCTION guard_prematch_calibration_insert()
            RETURNS trigger AS $$
            DECLARE
                model_mode text;
                parent_model_kind text;
                parent_training_cutoff timestamptz;
            BEGIN
                SELECT availability_mode, model_kind,
                       live_text_timestamp_utc(training_cutoff)
                  INTO model_mode, parent_model_kind, parent_training_cutoff
                  FROM prematch_model_runs WHERE model_hash = NEW.model_hash;
                IF model_mode IS NULL THEN
                    RAISE EXCEPTION 'prematch calibration parent model is unavailable';
                END IF;
                IF NEW.model_kind IS DISTINCT FROM parent_model_kind THEN
                    RAISE EXCEPTION
                        'prematch calibration model kind disagrees';
                END IF;
                IF NEW.artifact_json::jsonb->>'availability_mode'
                       IS DISTINCT FROM model_mode
                THEN
                    RAISE EXCEPTION
                        'prematch calibration availability mode disagrees';
                END IF;
                IF parent_training_cutoff >
                   live_text_timestamp_utc(NEW.evaluation_cutoff)
                THEN
                    RAISE EXCEPTION
                        'prematch calibration predates final model';
                END IF;
                IF model_mode = 'reconstructed_walk_forward' AND
                   NEW.status = 'passed'
                THEN
                    RAISE EXCEPTION
                        'reconstructed calibration cannot be marked passed';
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
            CREATE FUNCTION guard_prematch_lineage_change_append()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM prematch_lineage_changes AS existing
                     WHERE existing.dependency_revision = NEW.dependency_revision
                ) OR NEW.dependency_revision IS DISTINCT FROM (
                    SELECT dependency_revision
                      FROM prematch_lineage_revisions WHERE singleton = 1
                ) THEN
                    RAISE EXCEPTION 'prematch lineage changes are append-only';
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
            CREATE FUNCTION reject_prematch_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
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
                    CREATE TRIGGER {_trigger_name("dependency", table_name, operation)}
                    AFTER {operation} ON {table_name}
                    FOR EACH ROW EXECUTE FUNCTION advance_prematch_dependency_revision()
                    """
                )
            )
    for table_name in ARTIFACT_TABLES:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                text(
                    f"""
                    CREATE TRIGGER {_trigger_name("artifact", table_name, operation)}
                    AFTER {operation} ON {table_name}
                    FOR EACH ROW EXECUTE FUNCTION advance_prematch_artifact_revision()
                    """
                )
            )

    op.execute(
        text(
            """
            CREATE TRIGGER prematch_predictions_mutation_guard
            BEFORE INSERT OR UPDATE OR DELETE ON prematch_predictions
            FOR EACH ROW EXECUTE FUNCTION guard_prematch_prediction_mutation()
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TRIGGER prematch_prediction_validations_claim_guard
            BEFORE INSERT ON prematch_prediction_validations
            FOR EACH ROW EXECUTE FUNCTION validate_prematch_prediction_lineage()
            """
        )
    )
    for table_name in APPEND_ONLY_TABLES:
        op.execute(
            text(
                f"""
                CREATE TRIGGER {table_name}_append_only
                BEFORE UPDATE OR DELETE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION reject_prematch_mutation()
                """
            )
        )
    op.execute(
        text(
            """
            CREATE TRIGGER prematch_calibration_mode_guard
            BEFORE INSERT ON prematch_calibration_artifacts
            FOR EACH ROW EXECUTE FUNCTION guard_prematch_calibration_insert()
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TRIGGER prematch_lineage_changes_append_only
            BEFORE INSERT ON prematch_lineage_changes
            FOR EACH ROW EXECUTE FUNCTION guard_prematch_lineage_change_append()
            """
        )
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            text(
                f"""
                CREATE TRIGGER prematch_lineage_changes_no_{operation.lower()}
                BEFORE {operation} ON prematch_lineage_changes
                FOR EACH ROW EXECUTE FUNCTION reject_prematch_mutation()
                """
            )
        )


def downgrade() -> None:
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            text(
                f"DROP TRIGGER prematch_lineage_changes_no_{operation.lower()} "
                "ON prematch_lineage_changes"
            )
        )
    op.execute(
        text(
            "DROP TRIGGER prematch_lineage_changes_append_only "
            "ON prematch_lineage_changes"
        )
    )
    op.execute(
        text(
            "DROP TRIGGER prematch_calibration_mode_guard "
            "ON prematch_calibration_artifacts"
        )
    )
    for table_name in APPEND_ONLY_TABLES:
        op.execute(text(f"DROP TRIGGER {table_name}_append_only ON {table_name}"))
    op.execute(
        text(
            "DROP TRIGGER prematch_prediction_validations_claim_guard "
            "ON prematch_prediction_validations"
        )
    )
    op.execute(
        text("DROP TRIGGER prematch_predictions_mutation_guard ON prematch_predictions")
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
    op.execute(text("DROP FUNCTION reject_prematch_mutation()"))
    op.execute(text("DROP FUNCTION guard_prematch_lineage_change_append()"))
    op.execute(text("DROP FUNCTION guard_prematch_calibration_insert()"))
    op.execute(text("DROP FUNCTION validate_prematch_prediction_lineage()"))
    op.execute(text("DROP FUNCTION guard_prematch_prediction_mutation()"))
    op.execute(
        text(
            "DROP FUNCTION IF EXISTS "
            "prematch_prediction_claims_are_consistent(prematch_predictions)"
        )
    )
    op.execute(text("DROP FUNCTION advance_prematch_artifact_revision()"))
    op.execute(text("DROP FUNCTION advance_prematch_dependency_revision()"))
    op.execute(text("DROP FUNCTION prematch_lineage_revision_is_current(bigint, text)"))
