"""Persist the frozen prospective R.O.S.H. research shadow ledger.

Revision ID: 20260806_0031
Revises: 20260806_0030
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260806_0031"
down_revision: str | None = "20260806_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SHA256 = "{0} ~ '^[0-9a-f]{{64}}$'"
_FINITE = (
    "{0} > '-Infinity'::double precision AND "
    "{0} < 'Infinity'::double precision"
)


def _sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(_SHA256.format(column))


def _optional_sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR ({_SHA256.format(column)})"
    )


def _finite(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(_FINITE.format(column))


def _optional_finite(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR ({_FINITE.format(column)})"
    )


def _utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"live_text_timestamp_utc({column}) IS NOT NULL"
    )


def _optional_utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR live_text_timestamp_utc({column}) IS NOT NULL"
    )


def upgrade() -> None:
    op.create_table(
        "prospective_rosh_candidates",
        sa.Column("candidate_hash", sa.Text(), primary_key=True),
        sa.Column("artifact_version", sa.Text(), nullable=False),
        sa.Column("candidate_version", sa.Text(), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("retrospective_formula_version", sa.Text(), nullable=False),
        sa.Column("prospective_profile_id", sa.Text(), nullable=False),
        sa.Column("prospective_profile_hash", sa.Text(), nullable=False),
        sa.Column("scorer_source_hash", sa.Text(), nullable=False),
        sa.Column("training_support", sa.Integer(), nullable=False),
        sa.Column("training_cohort_hash", sa.Text(), nullable=False),
        sa.Column("training_cutoff", sa.Text(), nullable=False),
        sa.Column("frozen_at", sa.Text(), nullable=False),
        sa.Column("prospective_start_at", sa.Text(), nullable=False),
        sa.Column("score_mean", sa.Double(), nullable=False),
        sa.Column("score_scale", sa.Double(), nullable=False),
        sa.Column("beta_rosh", sa.Double(), nullable=False),
        sa.Column("fit_log_loss", sa.Double(), nullable=False),
        sa.Column("retrospective_initialized", sa.Boolean(), nullable=False),
        sa.Column("prospective_unvalidated", sa.Boolean(), nullable=False),
        sa.Column("shadow_only", sa.Boolean(), nullable=False),
        sa.Column("not_deployment_eligible", sa.Boolean(), nullable=False),
        sa.Column("deployment_eligible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "artifact_version = 'prospective-rosh-candidate-artifact-v1'"
        ),
        sa.CheckConstraint(
            "candidate_version = 'prospective-rosh-candidate-v1'"
        ),
        sa.CheckConstraint(
            "formula = "
            "'logit(P1)=logit(P0)+beta_rosh*standardized_pure_rosh_score'"
        ),
        sa.CheckConstraint("jsonb_typeof(artifact_json::jsonb) = 'object'"),
        sa.CheckConstraint("length(trim(retrospective_formula_version)) > 0"),
        sa.CheckConstraint("length(trim(prospective_profile_id)) > 0"),
        sa.CheckConstraint("training_support = 513"),
        sa.CheckConstraint("score_scale > 0.0"),
        sa.CheckConstraint("beta_rosh > 0.0"),
        sa.CheckConstraint("fit_log_loss >= 0.0"),
        sa.CheckConstraint("retrospective_initialized IS TRUE"),
        sa.CheckConstraint("prospective_unvalidated IS TRUE"),
        sa.CheckConstraint("shadow_only IS TRUE"),
        sa.CheckConstraint("not_deployment_eligible IS TRUE"),
        sa.CheckConstraint("deployment_eligible IS FALSE"),
        sa.CheckConstraint(
            "live_text_timestamp_utc(training_cutoff) < "
            "live_text_timestamp_utc(frozen_at) AND "
            "live_text_timestamp_utc(frozen_at) < "
            "live_text_timestamp_utc(prospective_start_at) AND "
            "live_text_timestamp_utc(frozen_at) <= "
            "live_text_timestamp_utc(created_at)"
        ),
        _sha256("candidate_hash"),
        _sha256("prospective_profile_hash"),
        _sha256("scorer_source_hash"),
        _sha256("training_cohort_hash"),
        _finite("score_mean"),
        _finite("score_scale"),
        _finite("beta_rosh"),
        _finite("fit_log_loss"),
        _utc("training_cutoff"),
        _utc("frozen_at"),
        _utc("prospective_start_at"),
        _utc("created_at"),
    )

    op.create_table(
        "prospective_rosh_shadow_predictions",
        sa.Column("prediction_hash", sa.Text(), primary_key=True),
        sa.Column(
            "candidate_hash",
            sa.Text(),
            sa.ForeignKey("prospective_rosh_candidates.candidate_hash"),
            nullable=False,
        ),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("match_ingest_status.match_id"),
            nullable=False,
        ),
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("prediction_cutoff", sa.Text(), nullable=False),
        sa.Column("record_status", sa.Text(), nullable=False),
        sa.Column("p0_probability", sa.Double(), nullable=False),
        sa.Column("p1_probability", sa.Double()),
        sa.Column("pure_rosh_score", sa.Double()),
        sa.Column("standardized_rosh_score", sa.Double()),
        sa.Column("rosh_logit_contribution", sa.Double()),
        sa.Column("beta_rosh", sa.Double(), nullable=False),
        sa.Column("score_mean", sa.Double(), nullable=False),
        sa.Column("score_scale", sa.Double(), nullable=False),
        sa.Column(
            "team_rating_prediction_id",
            sa.BigInteger(),
            sa.ForeignKey("team_rating_predictions.prediction_id"),
            nullable=False,
        ),
        sa.Column(
            "team_rating_run_id",
            sa.Text(),
            sa.ForeignKey("team_rating_runs.run_id"),
            nullable=False,
        ),
        sa.Column("team_rating_version", sa.Text(), nullable=False),
        sa.Column("team_rating_artifact_version", sa.Text(), nullable=False),
        sa.Column("team_rating_artifact_hash", sa.Text(), nullable=False),
        sa.Column("team_rating_input_hash", sa.Text(), nullable=False),
        sa.Column("team_rating_training_input_hash", sa.Text(), nullable=False),
        sa.Column("rosh_profile_id", sa.Text()),
        sa.Column("rosh_profile_hash", sa.Text()),
        sa.Column("rosh_formula_version", sa.Text()),
        sa.Column("rosh_scorer_source_hash", sa.Text()),
        sa.Column("rosh_evidence_hash", sa.Text()),
        sa.Column("rosh_radiant_heroes_json", sa.Text()),
        sa.Column("rosh_dire_heroes_json", sa.Text()),
        sa.Column("rosh_request_artifacts_json", sa.Text()),
        sa.Column("rosh_request_manifest_hash", sa.Text()),
        sa.Column("rosh_response_artifacts_json", sa.Text()),
        sa.Column("rosh_response_manifest_hash", sa.Text()),
        sa.Column("rosh_statistics_cutoff", sa.Text()),
        sa.Column("rosh_available_at", sa.Text()),
        sa.Column("missing_reason", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("candidate_hash", "match_id"),
        sa.CheckConstraint("match_id > 0"),
        sa.CheckConstraint("series_id > 0"),
        sa.CheckConstraint("record_status IN ('paired', 'p0_only')"),
        sa.CheckConstraint(
            "p0_probability > 0.0 AND p0_probability < 1.0"
        ),
        sa.CheckConstraint(
            "p1_probability IS NULL OR "
            "(p1_probability > 0.0 AND p1_probability < 1.0)"
        ),
        sa.CheckConstraint("beta_rosh > 0.0"),
        sa.CheckConstraint("score_scale > 0.0"),
        sa.CheckConstraint("length(trim(team_rating_version)) > 0"),
        sa.CheckConstraint("length(trim(team_rating_artifact_version)) > 0"),
        sa.CheckConstraint(
            "rosh_radiant_heroes_json IS NULL OR "
            "(jsonb_array_length(rosh_radiant_heroes_json::jsonb) = 5 AND "
            "team_rating_roster_is_valid(rosh_radiant_heroes_json::jsonb))"
        ),
        sa.CheckConstraint(
            "rosh_dire_heroes_json IS NULL OR "
            "(jsonb_array_length(rosh_dire_heroes_json::jsonb) = 5 AND "
            "team_rating_roster_is_valid(rosh_dire_heroes_json::jsonb))"
        ),
        sa.CheckConstraint(
            "(rosh_request_artifacts_json IS NULL) = "
            "(rosh_request_manifest_hash IS NULL)"
        ),
        sa.CheckConstraint(
            "(rosh_response_artifacts_json IS NULL) = "
            "(rosh_response_manifest_hash IS NULL)"
        ),
        sa.CheckConstraint(
            "rosh_request_artifacts_json IS NULL OR "
            "(jsonb_typeof(rosh_request_artifacts_json::jsonb) = 'array' AND "
            "jsonb_array_length(rosh_request_artifacts_json::jsonb) = 3)"
        ),
        sa.CheckConstraint(
            "rosh_response_artifacts_json IS NULL OR "
            "(jsonb_typeof(rosh_response_artifacts_json::jsonb) = 'array' AND "
            "jsonb_array_length(rosh_response_artifacts_json::jsonb) = 3)"
        ),
        sa.CheckConstraint(
            "(record_status = 'paired' AND "
            "p1_probability IS NOT NULL AND pure_rosh_score IS NOT NULL AND "
            "standardized_rosh_score IS NOT NULL AND "
            "rosh_logit_contribution IS NOT NULL AND "
            "rosh_profile_id IS NOT NULL AND rosh_profile_hash IS NOT NULL AND "
            "rosh_formula_version IS NOT NULL AND "
            "rosh_scorer_source_hash IS NOT NULL AND "
            "rosh_evidence_hash IS NOT NULL AND "
            "rosh_radiant_heroes_json IS NOT NULL AND "
            "rosh_dire_heroes_json IS NOT NULL AND "
            "rosh_request_artifacts_json IS NOT NULL AND "
            "rosh_response_artifacts_json IS NOT NULL AND "
            "rosh_statistics_cutoff IS NOT NULL AND "
            "rosh_available_at IS NOT NULL AND missing_reason IS NULL) OR "
            "(record_status = 'p0_only' AND "
            "p1_probability IS NULL AND pure_rosh_score IS NULL AND "
            "standardized_rosh_score IS NULL AND "
            "rosh_logit_contribution IS NULL AND "
            "rosh_profile_id IS NULL AND rosh_profile_hash IS NULL AND "
            "rosh_formula_version IS NULL AND "
            "rosh_scorer_source_hash IS NULL AND "
            "rosh_evidence_hash IS NULL AND "
            "rosh_radiant_heroes_json IS NULL AND "
            "rosh_dire_heroes_json IS NULL AND "
            "rosh_request_artifacts_json IS NULL AND "
            "rosh_response_artifacts_json IS NULL AND "
            "rosh_statistics_cutoff IS NULL AND "
            "rosh_available_at IS NULL AND "
            "length(trim(missing_reason)) > 0)"
        ),
        sa.CheckConstraint(
            "live_text_timestamp_utc(created_at) <= "
            "live_text_timestamp_utc(prediction_cutoff)"
        ),
        _sha256("prediction_hash"),
        _sha256("candidate_hash"),
        _sha256("team_rating_run_id"),
        _sha256("team_rating_artifact_hash"),
        _sha256("team_rating_input_hash"),
        _sha256("team_rating_training_input_hash"),
        _optional_sha256("rosh_profile_hash"),
        _optional_sha256("rosh_scorer_source_hash"),
        _optional_sha256("rosh_evidence_hash"),
        _optional_sha256("rosh_request_manifest_hash"),
        _optional_sha256("rosh_response_manifest_hash"),
        _finite("p0_probability"),
        _optional_finite("p1_probability"),
        _optional_finite("pure_rosh_score"),
        _optional_finite("standardized_rosh_score"),
        _optional_finite("rosh_logit_contribution"),
        _finite("beta_rosh"),
        _finite("score_mean"),
        _finite("score_scale"),
        _utc("prediction_cutoff"),
        _optional_utc("rosh_statistics_cutoff"),
        _optional_utc("rosh_available_at"),
        _utc("created_at"),
    )
    op.create_index(
        "idx_prospective_rosh_shadow_prediction_cutoff",
        "prospective_rosh_shadow_predictions",
        ["candidate_hash", "prediction_cutoff", "match_id"],
    )

    op.create_table(
        "prospective_rosh_shadow_settlements",
        sa.Column("settlement_hash", sa.Text(), primary_key=True),
        sa.Column(
            "prediction_hash",
            sa.Text(),
            sa.ForeignKey("prospective_rosh_shadow_predictions.prediction_hash"),
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
        _sha256("prediction_hash"),
        _sha256("result_artifact_hash"),
        _utc("result_usable_at"),
        _utc("settled_at"),
        _utc("created_at"),
    )

    op.create_table(
        "prospective_rosh_shadow_evaluations",
        sa.Column("evaluation_hash", sa.Text(), primary_key=True),
        sa.Column(
            "candidate_hash",
            sa.Text(),
            sa.ForeignKey("prospective_rosh_candidates.candidate_hash"),
            nullable=False,
        ),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("paired_support", sa.Integer(), nullable=False),
        sa.Column("window_manifest_json", sa.Text(), nullable=False),
        sa.Column("window_manifest_hash", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("report_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("candidate_hash", "stage"),
        sa.CheckConstraint("stage IN (20, 100, 200)"),
        sa.CheckConstraint("paired_support = stage"),
        sa.CheckConstraint(
            "jsonb_typeof(window_manifest_json::jsonb) = 'object'"
        ),
        sa.CheckConstraint("jsonb_typeof(report_json::jsonb) = 'object'"),
        _sha256("evaluation_hash"),
        _sha256("candidate_hash"),
        _sha256("window_manifest_hash"),
        _sha256("report_hash"),
        _utc("created_at"),
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_prospective_rosh_shadow_prediction()
            RETURNS trigger AS $$
            DECLARE
                candidate_frozen_at timestamptz;
                candidate_start_at timestamptz;
                candidate_beta double precision;
                candidate_mean double precision;
                candidate_scale double precision;
                candidate_profile_id text;
                candidate_profile_hash text;
                candidate_formula_version text;
                candidate_scorer_hash text;
                team_match_id bigint;
                team_run_id text;
                team_cutoff timestamptz;
                team_probability double precision;
                team_input_hash text;
                team_status text;
                team_outcome smallint;
                team_mode text;
                team_version text;
                team_artifact_version text;
                team_artifact_hash text;
                team_training_hash text;
                team_run_status text;
                target_series_id bigint;
                ingest_series_id bigint;
                target_started_at timestamptz;
                target_outcome boolean;
                target_has_result smallint;
                target_result_usable_at timestamptz;
                expected_standardized double precision;
                expected_contribution double precision;
                expected_probability double precision;
            BEGIN
                SELECT live_text_timestamp_utc(frozen_at),
                       live_text_timestamp_utc(prospective_start_at),
                       beta_rosh, score_mean, score_scale,
                       prospective_profile_id, prospective_profile_hash,
                       retrospective_formula_version, scorer_source_hash
                  INTO candidate_frozen_at, candidate_start_at,
                       candidate_beta, candidate_mean, candidate_scale,
                       candidate_profile_id, candidate_profile_hash,
                       candidate_formula_version, candidate_scorer_hash
                  FROM prospective_rosh_candidates
                 WHERE candidate_hash = NEW.candidate_hash;
                IF candidate_frozen_at IS NULL THEN
                    RAISE EXCEPTION 'prospective R.O.S.H. candidate is unavailable';
                END IF;
                IF candidate_frozen_at >=
                       live_text_timestamp_utc(NEW.prediction_cutoff) OR
                   candidate_start_at >
                       live_text_timestamp_utc(NEW.prediction_cutoff)
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. candidate was not frozen before cutoff';
                END IF;
                IF NEW.beta_rosh IS DISTINCT FROM candidate_beta OR
                   NEW.score_mean IS DISTINCT FROM candidate_mean OR
                   NEW.score_scale IS DISTINCT FROM candidate_scale
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. frozen parameters disagree';
                END IF;

                SELECT prediction.match_id, prediction.run_id,
                       live_text_timestamp_utc(prediction.prediction_cutoff),
                       prediction.raw_probability, prediction.input_hash,
                       prediction.status, prediction.eventual_radiant_win,
                       run.availability_mode, run.rating_version,
                       run.artifact_version,
                       run.configuration_json::jsonb->>'artifact_hash',
                       run.training_input_hash, run.status
                  INTO team_match_id, team_run_id, team_cutoff,
                       team_probability, team_input_hash, team_status,
                       team_outcome, team_mode, team_version,
                       team_artifact_version, team_artifact_hash,
                       team_training_hash, team_run_status
                  FROM team_rating_predictions AS prediction
                  JOIN team_rating_runs AS run ON run.run_id = prediction.run_id
                 WHERE prediction.prediction_id = NEW.team_rating_prediction_id;
                IF team_match_id IS NULL OR team_mode <> 'prospective' OR
                   team_match_id <> NEW.match_id OR
                   team_run_id <> NEW.team_rating_run_id OR
                   team_cutoff <>
                       live_text_timestamp_utc(NEW.prediction_cutoff) OR
                   team_probability IS NULL OR
                   abs(team_probability - NEW.p0_probability) > 1e-12 OR
                   team_input_hash <> NEW.team_rating_input_hash OR
                   team_training_hash <> NEW.team_rating_training_input_hash OR
                   team_version <> NEW.team_rating_version OR
                   team_artifact_version <> NEW.team_rating_artifact_version OR
                   team_artifact_hash <> NEW.team_rating_artifact_hash OR
                   team_run_status <> 'trained' OR
                   team_status <> 'predicted' OR team_outcome IS NOT NULL
                THEN
                    RAISE EXCEPTION
                        'prospective Team Rating prediction authority disagrees';
                END IF;

                SELECT target_match.series_id,
                       ingest.series_id,
                       CASE
                           WHEN target_match.start_time IS NULL OR
                                target_match.start_time <= 0
                           THEN NULL
                           ELSE to_timestamp(target_match.start_time)
                       END,
                       target_match.radiant_win,
                       ingest.has_valid_result,
                       live_text_timestamp_utc(ingest.first_usable_at)
                  INTO target_series_id, ingest_series_id, target_started_at,
                       target_outcome, target_has_result,
                       target_result_usable_at
                  FROM matches AS target_match
                  JOIN match_ingest_status AS ingest
                    ON ingest.match_id = target_match.match_id
                 WHERE target_match.match_id = NEW.match_id
                 FOR SHARE OF target_match, ingest;
                IF target_series_id IS NULL OR ingest_series_id IS NULL OR
                   target_series_id <> NEW.series_id OR
                   ingest_series_id <> NEW.series_id
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. series authority disagrees';
                END IF;
                IF target_started_at IS NULL OR
                   live_text_timestamp_utc(NEW.prediction_cutoff) >
                       target_started_at
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. cutoff follows target map start';
                END IF;
                IF target_outcome IS NOT NULL OR target_has_result <> 0 OR
                   target_result_usable_at IS NOT NULL
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. prediction follows target result';
                END IF;

                IF NEW.rosh_available_at IS NOT NULL AND
                   live_text_timestamp_utc(NEW.rosh_available_at) >
                       live_text_timestamp_utc(NEW.prediction_cutoff)
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. evidence follows prediction cutoff';
                END IF;
                IF NEW.record_status = 'paired' THEN
                    IF live_text_timestamp_utc(NEW.rosh_statistics_cutoff) >
                           live_text_timestamp_utc(NEW.rosh_available_at)
                    THEN
                        RAISE EXCEPTION
                            'prospective R.O.S.H. statistics cutoff follows availability';
                    END IF;
                    IF NEW.rosh_profile_id <> candidate_profile_id OR
                       NEW.rosh_profile_hash <> candidate_profile_hash OR
                       NEW.rosh_formula_version <> candidate_formula_version OR
                       NEW.rosh_scorer_source_hash <> candidate_scorer_hash
                    THEN
                        RAISE EXCEPTION
                            'prospective R.O.S.H. scorer identity disagrees';
                    END IF;
                    expected_standardized :=
                        (NEW.pure_rosh_score - candidate_mean) / candidate_scale;
                    expected_contribution :=
                        candidate_beta * expected_standardized;
                    expected_probability := 1.0 / (
                        1.0 + exp(-(
                            ln(NEW.p0_probability / (1.0 - NEW.p0_probability)) +
                            expected_contribution
                        ))
                    );
                    IF abs(
                           NEW.standardized_rosh_score - expected_standardized
                       ) > 1e-12 OR
                       abs(
                           NEW.rosh_logit_contribution - expected_contribution
                       ) > 1e-12 OR
                       abs(NEW.p1_probability - expected_probability) > 1e-12
                    THEN
                        RAISE EXCEPTION
                            'prospective R.O.S.H. P1 replay disagrees';
                    END IF;
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
            CREATE TRIGGER prospective_rosh_shadow_predictions_insert_guard
            BEFORE INSERT ON prospective_rosh_shadow_predictions
            FOR EACH ROW
            EXECUTE FUNCTION validate_prospective_rosh_shadow_prediction()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_prospective_rosh_shadow_settlement()
            RETURNS trigger AS $$
            DECLARE
                target_match_id bigint;
                target_cutoff timestamptz;
                target_outcome boolean;
                target_started_at timestamptz;
                target_duration integer;
                target_has_result smallint;
                target_basic_state text;
                target_result_usable_at timestamptz;
                target_artifact_hash text;
            BEGIN
                SELECT match_id, live_text_timestamp_utc(prediction_cutoff)
                  INTO target_match_id, target_cutoff
                  FROM prospective_rosh_shadow_predictions
                 WHERE prediction_hash = NEW.prediction_hash;
                IF target_match_id IS NULL THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. shadow prediction is unavailable';
                END IF;
                SELECT target_match.radiant_win,
                       CASE
                           WHEN target_match.start_time IS NULL OR
                                target_match.start_time <= 0
                           THEN NULL
                           ELSE to_timestamp(target_match.start_time)
                       END,
                       target_match.duration,
                       ingest.has_valid_result,
                       ingest.basic_result_state,
                       live_text_timestamp_utc(ingest.first_usable_at),
                       ingest.latest_raw_content_hash
                  INTO target_outcome, target_started_at, target_duration,
                       target_has_result, target_basic_state,
                       target_result_usable_at, target_artifact_hash
                  FROM matches AS target_match
                  JOIN match_ingest_status AS ingest
                    ON ingest.match_id = target_match.match_id
                 WHERE target_match.match_id = target_match_id
                 FOR SHARE OF target_match, ingest;
                IF target_outcome IS NULL OR target_has_result <> 1 OR
                   target_basic_state <> 'ready' OR
                   target_result_usable_at IS NULL OR
                   target_artifact_hash IS NULL
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. authoritative result is unavailable';
                END IF;
                IF (NEW.eventual_radiant_win = 1) IS DISTINCT FROM
                       target_outcome OR
                   NEW.result_artifact_hash <> target_artifact_hash OR
                   live_text_timestamp_utc(NEW.result_usable_at) <>
                       target_result_usable_at
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. settlement authority disagrees';
                END IF;
                IF target_started_at IS NULL OR target_duration IS NULL OR
                   target_duration <= 0 OR
                   target_result_usable_at <
                       target_started_at + make_interval(secs => target_duration) OR
                   target_result_usable_at <= target_cutoff
                THEN
                    RAISE EXCEPTION
                        'prospective R.O.S.H. settlement timing is invalid';
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
            CREATE TRIGGER prospective_rosh_shadow_settlements_insert_guard
            BEFORE INSERT ON prospective_rosh_shadow_settlements
            FOR EACH ROW
            EXECUTE FUNCTION validate_prospective_rosh_shadow_settlement()
            """
        )
    )

    for table in (
        "prospective_rosh_candidates",
        "prospective_rosh_shadow_predictions",
        "prospective_rosh_shadow_settlements",
        "prospective_rosh_shadow_evaluations",
    ):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                    '{table} is append-only'
                )
                """
            )
        )


def downgrade() -> None:
    op.drop_table("prospective_rosh_shadow_evaluations")
    op.drop_table("prospective_rosh_shadow_settlements")
    op.drop_table("prospective_rosh_shadow_predictions")
    op.drop_table("prospective_rosh_candidates")
    op.execute(
        sa.text("DROP FUNCTION validate_prospective_rosh_shadow_settlement()")
    )
    op.execute(
        sa.text("DROP FUNCTION validate_prospective_rosh_shadow_prediction()")
    )
