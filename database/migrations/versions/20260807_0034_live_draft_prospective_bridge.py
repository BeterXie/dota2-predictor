"""Add the minimal live-draft prospective prediction and settlement bridge.

Revision ID: 20260807_0034
Revises: 20260807_0033
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0034"
down_revision: str | None = "20260807_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CANDIDATE_HASH = "84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d"


def _sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} ~ '^[0-9a-f]{{64}}$'")


def _optional_sha256(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IS NULL OR {column} ~ '^[0-9a-f]{{64}}$'")


def _utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"live_text_timestamp_utc({column}) IS NOT NULL")


def _optional_utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR live_text_timestamp_utc({column}) IS NOT NULL"
    )


def upgrade() -> None:
    op.create_table(
        "live_draft_prospective_predictions",
        sa.Column("prediction_hash", sa.Text(), primary_key=True),
        sa.Column("bridge_version", sa.Text(), nullable=False),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("mapping_hash", sa.Text(), nullable=False),
        sa.Column("operator_locked_at", sa.Text(), nullable=False),
        sa.Column("operator_identity", sa.Text(), nullable=False),
        sa.Column("confirmation_text", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.Text(), nullable=False),
        sa.Column("radiant_team_id", sa.BigInteger(), nullable=False),
        sa.Column("dire_team_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "team_rating_seed_hash",
            sa.Text(),
            sa.ForeignKey("prospective_team_rating_seeds.seed_hash"),
            nullable=False,
        ),
        sa.Column("team_rating_configuration_hash", sa.Text(), nullable=False),
        sa.Column("team_rating_base_state_hash", sa.Text(), nullable=False),
        sa.Column("team_rating_applied_manifest_hash", sa.Text(), nullable=False),
        sa.Column("team_rating_state_before_hash", sa.Text(), nullable=False),
        sa.Column("team_rating_training_input_hash", sa.Text(), nullable=False),
        sa.Column("team_rating_artifact_hash", sa.Text(), nullable=False),
        sa.Column("radiant_rating", sa.Double(), nullable=False),
        sa.Column("dire_rating", sa.Double(), nullable=False),
        sa.Column("rating_difference", sa.Double(), nullable=False),
        sa.Column("support", sa.Integer(), nullable=False),
        sa.Column("p0_probability", sa.Double(), nullable=False),
        sa.Column(
            "candidate_hash",
            sa.Text(),
            sa.ForeignKey("prospective_rosh_candidates.candidate_hash"),
            nullable=False,
        ),
        sa.Column("record_status", sa.Text(), nullable=False),
        sa.Column("p1_probability", sa.Double()),
        sa.Column("pure_rosh_score", sa.Double()),
        sa.Column("standardized_rosh_score", sa.Double()),
        sa.Column("rosh_logit_contribution", sa.Double()),
        sa.Column("rosh_request_manifest_hash", sa.Text()),
        sa.Column("rosh_response_manifest_hash", sa.Text()),
        sa.Column("rosh_evidence_hash", sa.Text()),
        sa.Column("rosh_statistics_cutoff", sa.Text()),
        sa.Column("rosh_available_at", sa.Text()),
        sa.Column("missing_reason", sa.Text()),
        sa.Column("game_clock_seconds", sa.Integer()),
        sa.Column("vision_frame_timestamp", sa.Text()),
        sa.Column("draft_state_marker", sa.Text()),
        sa.Column("live_state_input_used", sa.Boolean(), nullable=False),
        sa.Column("causal_status", sa.Text(), nullable=False),
        sa.Column("causal_reason", sa.Text()),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("raybet_match_id", "map_number", "mapping_version"),
        sa.CheckConstraint("bridge_version='live-draft-prospective-bridge-v1'"),
        sa.CheckConstraint("length(trim(raybet_match_id)) > 0"),
        sa.CheckConstraint("map_number BETWEEN 1 AND 5"),
        sa.CheckConstraint("mapping_version > 0"),
        sa.CheckConstraint("radiant_team_id > 0 AND dire_team_id > 0"),
        sa.CheckConstraint("radiant_team_id <> dire_team_id"),
        sa.CheckConstraint("support >= 0"),
        sa.CheckConstraint("p0_probability > 0 AND p0_probability < 1"),
        sa.CheckConstraint(f"candidate_hash='{_CANDIDATE_HASH}'"),
        sa.CheckConstraint("record_status IN ('paired', 'p0_only')"),
        sa.CheckConstraint(
            "(record_status='paired' AND p1_probability IS NOT NULL AND "
            "pure_rosh_score IS NOT NULL AND standardized_rosh_score IS NOT NULL AND "
            "rosh_logit_contribution IS NOT NULL AND rosh_request_manifest_hash IS NOT NULL AND "
            "rosh_response_manifest_hash IS NOT NULL AND rosh_evidence_hash IS NOT NULL AND "
            "rosh_statistics_cutoff IS NOT NULL AND rosh_available_at IS NOT NULL AND "
            "missing_reason IS NULL) OR "
            "(record_status='p0_only' AND p1_probability IS NULL AND "
            "pure_rosh_score IS NULL AND standardized_rosh_score IS NULL AND "
            "rosh_logit_contribution IS NULL AND rosh_request_manifest_hash IS NULL AND "
            "rosh_response_manifest_hash IS NULL AND rosh_evidence_hash IS NULL AND "
            "rosh_statistics_cutoff IS NULL AND rosh_available_at IS NULL AND "
            "length(trim(missing_reason)) > 0)"
        ),
        sa.CheckConstraint("p1_probability IS NULL OR (p1_probability > 0 AND p1_probability < 1)"),
        sa.CheckConstraint("game_clock_seconds IS NULL OR game_clock_seconds >= 0"),
        sa.CheckConstraint("causal_status IN ('eligible', 'unverified', 'ineligible')"),
        sa.CheckConstraint(
            "(causal_status='eligible' AND causal_reason IS NULL) OR "
            "(causal_status<>'eligible' AND length(trim(causal_reason)) > 0)"
        ),
        sa.CheckConstraint(
            "live_text_timestamp_utc(operator_locked_at) <= "
            "live_text_timestamp_utc(confirmed_at) AND "
            "live_text_timestamp_utc(confirmed_at) <= live_text_timestamp_utc(created_at)"
        ),
        sa.CheckConstraint(
            "rosh_statistics_cutoff IS NULL OR "
            "live_text_timestamp_utc(rosh_statistics_cutoff) <= "
            "live_text_timestamp_utc(rosh_available_at)"
        ),
        _sha256("prediction_hash"),
        _sha256("mapping_hash"),
        _sha256("team_rating_seed_hash"),
        _sha256("team_rating_configuration_hash"),
        _sha256("team_rating_base_state_hash"),
        _sha256("team_rating_applied_manifest_hash"),
        _sha256("team_rating_state_before_hash"),
        _sha256("team_rating_training_input_hash"),
        _sha256("team_rating_artifact_hash"),
        _sha256("candidate_hash"),
        _optional_sha256("rosh_request_manifest_hash"),
        _optional_sha256("rosh_response_manifest_hash"),
        _optional_sha256("rosh_evidence_hash"),
        _utc("operator_locked_at"),
        _utc("confirmed_at"),
        _optional_utc("rosh_statistics_cutoff"),
        _optional_utc("rosh_available_at"),
        _optional_utc("vision_frame_timestamp"),
        _utc("created_at"),
    )

    op.create_table(
        "live_draft_prospective_settlements",
        sa.Column("settlement_hash", sa.Text(), primary_key=True),
        sa.Column(
            "prediction_hash",
            sa.Text(),
            sa.ForeignKey("live_draft_prospective_predictions.prediction_hash"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "strict_mapping_id",
            sa.BigInteger(),
            sa.ForeignKey("strict_live_map_mappings.mapping_id"),
            nullable=False,
        ),
        sa.Column("dota_match_id", sa.BigInteger(), nullable=False),
        sa.Column("winner_side", sa.Text(), nullable=False),
        sa.Column("result_evidence_hash", sa.Text(), nullable=False),
        sa.Column("authoritative_actual_start", sa.Text(), nullable=False),
        sa.Column("result_usable_at", sa.Text(), nullable=False),
        sa.Column("post_settlement_causal_status", sa.Text(), nullable=False),
        sa.Column("post_settlement_causal_reason", sa.Text()),
        sa.Column("settled_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("strict_mapping_id > 0 AND dota_match_id > 0"),
        sa.CheckConstraint("winner_side IN ('radiant', 'dire')"),
        sa.CheckConstraint(
            "post_settlement_causal_status IN ('eligible', 'unverified', 'ineligible')"
        ),
        sa.CheckConstraint(
            "(post_settlement_causal_status='eligible' AND post_settlement_causal_reason IS NULL) OR "
            "(post_settlement_causal_status<>'eligible' AND "
            "length(trim(post_settlement_causal_reason)) > 0)"
        ),
        sa.CheckConstraint(
            "live_text_timestamp_utc(result_usable_at) <= live_text_timestamp_utc(settled_at) "
            "AND live_text_timestamp_utc(settled_at) <= live_text_timestamp_utc(created_at)"
        ),
        _sha256("settlement_hash"),
        _sha256("prediction_hash"),
        _sha256("result_evidence_hash"),
        _utc("authoritative_actual_start"),
        _utc("result_usable_at"),
        _utc("settled_at"),
        _utc("created_at"),
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_live_draft_prospective_prediction()
            RETURNS trigger AS $$
            DECLARE
                mapping_count integer;
                mapping_locked_count integer;
                mapping_created_at timestamptz;
                mapping_created_text text;
                mapping_operator text;
                mapping_radiant_team bigint;
                mapping_dire_team bigint;
                mapping_hero_count integer;
                mapping_radiant_position_count integer;
                mapping_dire_position_count integer;
                computed_mapping_hash text;
                candidate_profile text;
                candidate_formula text;
                seed_configuration_hash text;
                artifact jsonb;
            BEGIN
                SELECT count(*), count(*) FILTER (WHERE is_locked=1),
                       min(live_text_timestamp_utc(created_at)), min(created_at), min(created_by),
                       min(team_id) FILTER (WHERE side='radiant'),
                       min(team_id) FILTER (WHERE side='dire'), count(DISTINCT hero_id),
                       count(DISTINCT position) FILTER (WHERE side='radiant'),
                       count(DISTINCT position) FILTER (WHERE side='dire')
                  INTO mapping_count, mapping_locked_count, mapping_created_at,
                       mapping_created_text, mapping_operator, mapping_radiant_team,
                       mapping_dire_team, mapping_hero_count,
                       mapping_radiant_position_count, mapping_dire_position_count
                  FROM live_draft_mappings
                 WHERE raybet_match_id=NEW.raybet_match_id
                   AND map_number=NEW.map_number AND version=NEW.mapping_version;

                SELECT encode(sha256(convert_to(team_rating_canonical_json(
                    jsonb_build_object(
                        'raybet_match_id', NEW.raybet_match_id,
                        'map_number', NEW.map_number,
                        'version', NEW.mapping_version,
                        'is_locked', true,
                        'created_by', mapping_operator,
                        'created_at', mapping_created_text,
                        'slots', (
                            SELECT jsonb_agg(jsonb_build_object(
                                'side', side, 'position', position,
                                'team_id', team_id, 'hero_id', hero_id,
                                'player_id', player_id
                            ) ORDER BY CASE side WHEN 'radiant' THEN 0 ELSE 1 END, position)
                              FROM live_draft_mappings
                             WHERE raybet_match_id=NEW.raybet_match_id
                               AND map_number=NEW.map_number AND version=NEW.mapping_version
                        )
                    )), 'UTF8')), 'hex') INTO computed_mapping_hash;

                SELECT prospective_profile_id, formula
                  INTO candidate_profile, candidate_formula
                  FROM prospective_rosh_candidates WHERE candidate_hash=NEW.candidate_hash;
                SELECT configuration_hash INTO seed_configuration_hash
                  FROM prospective_team_rating_seeds WHERE seed_hash=NEW.team_rating_seed_hash;
                artifact := NEW.artifact_json::jsonb;

                IF mapping_count <> 10 OR mapping_locked_count <> 10 OR
                   mapping_hero_count <> 10 OR mapping_radiant_position_count <> 5 OR
                   mapping_dire_position_count <> 5 OR
                   mapping_created_at IS DISTINCT FROM live_text_timestamp_utc(NEW.operator_locked_at) OR
                   mapping_operator IS DISTINCT FROM NEW.operator_identity OR
                   mapping_radiant_team <> NEW.radiant_team_id OR
                   mapping_dire_team <> NEW.dire_team_id OR
                   computed_mapping_hash <> NEW.mapping_hash OR
                   seed_configuration_hash IS DISTINCT FROM NEW.team_rating_configuration_hash OR
                   candidate_profile <> 'legacy-dematus-pure-rosh-prospective-v1' OR
                   candidate_formula <> 'logit(P1)=logit(P0)+beta_rosh*standardized_pure_rosh_score' OR
                   (artifact->>'official_v2_compatible')::boolean IS DISTINCT FROM false OR
                   artifact->>'prediction_hash' <> NEW.prediction_hash OR
                   artifact->'identity'->>'raybet_match_id' <> NEW.raybet_match_id OR
                   (artifact->'identity'->>'map_number')::integer <> NEW.map_number OR
                   (artifact->'identity'->>'mapping_version')::integer <> NEW.mapping_version OR
                   artifact->'identity'->>'mapping_hash' <> NEW.mapping_hash OR
                   artifact->>'candidate_hash' <> NEW.candidate_hash OR
                   artifact->>'record_status' <> NEW.record_status OR
                   abs((artifact->>'p0_probability')::double precision - NEW.p0_probability) > 1e-12 OR
                   artifact->'model_inputs'->'excluded' IS NULL OR
                   artifact->'causal_evidence'->>'live_state_input_used' <> NEW.live_state_input_used::text OR
                   encode(sha256(convert_to(team_rating_canonical_json(
                       artifact - 'prediction_hash'), 'UTF8')), 'hex') <> NEW.prediction_hash
                THEN
                    RAISE EXCEPTION 'live draft prospective prediction authority disagrees';
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
            CREATE TRIGGER live_draft_prospective_predictions_insert_guard
            BEFORE INSERT ON live_draft_prospective_predictions
            FOR EACH ROW EXECUTE FUNCTION validate_live_draft_prospective_prediction()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_live_draft_prospective_settlement()
            RETURNS trigger AS $$
            DECLARE
                prediction_row live_draft_prospective_predictions%ROWTYPE;
                actual_start timestamptz;
                expected_status text;
                expected_reason text;
            BEGIN
                SELECT * INTO prediction_row FROM live_draft_prospective_predictions
                 WHERE prediction_hash=NEW.prediction_hash;
                SELECT to_timestamp(match_row.start_time) INTO actual_start
                  FROM map_results AS result
                  JOIN matches AS match_row ON match_row.match_id=result.dota_match_id
                 WHERE result.raybet_match_id=prediction_row.raybet_match_id
                   AND result.map_number=prediction_row.map_number
                   AND result.strict_mapping_id=NEW.strict_mapping_id
                   AND result.dota_match_id=NEW.dota_match_id
                   AND result.winner_side=CASE NEW.winner_side
                       WHEN 'radiant' THEN 'team_one' ELSE 'team_two' END;
                IF prediction_row.prediction_hash IS NULL OR actual_start IS NULL OR
                   actual_start <> live_text_timestamp_utc(NEW.authoritative_actual_start)
                THEN
                    RAISE EXCEPTION 'live draft settlement authority disagrees';
                END IF;
                IF prediction_row.causal_status='ineligible' OR
                   live_text_timestamp_utc(prediction_row.created_at) >= actual_start
                THEN
                    expected_status := 'ineligible';
                    expected_reason := CASE
                        WHEN prediction_row.causal_status='ineligible'
                        THEN prediction_row.causal_reason
                        ELSE 'prediction_not_before_authoritative_actual_start' END;
                ELSIF prediction_row.causal_status='eligible' THEN
                    expected_status := 'eligible'; expected_reason := NULL;
                ELSE
                    expected_status := 'unverified';
                    expected_reason := prediction_row.causal_reason;
                END IF;
                IF NEW.post_settlement_causal_status <> expected_status OR
                   NEW.post_settlement_causal_reason IS DISTINCT FROM expected_reason
                THEN
                    RAISE EXCEPTION 'live draft post-settlement causal audit disagrees';
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
            CREATE TRIGGER live_draft_prospective_settlements_insert_guard
            BEFORE INSERT ON live_draft_prospective_settlements
            FOR EACH ROW EXECUTE FUNCTION validate_live_draft_prospective_settlement()
            """
        )
    )

    for table in (
        "live_draft_prospective_predictions",
        "live_draft_prospective_settlements",
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
    op.drop_table("live_draft_prospective_settlements")
    op.execute(sa.text("DROP FUNCTION validate_live_draft_prospective_settlement()"))
    op.drop_table("live_draft_prospective_predictions")
    op.execute(sa.text("DROP FUNCTION validate_live_draft_prospective_prediction()"))
