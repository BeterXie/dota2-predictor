"""Create prospective outcomes and research-only labels.

Revision ID: 20260730_0011
Revises: 20260730_0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0011"
down_revision: str | None = "20260730_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prospective_draft_outcomes",
        sa.Column("curve_key", sa.Text(), sa.ForeignKey("prospective_draft_curves.curve_key"), primary_key=True),
        sa.Column("strict_mapping_id", sa.BigInteger(), nullable=False),
        sa.Column("dota_match_id", sa.BigInteger(), nullable=False),
        sa.Column("radiant_win", sa.SmallInteger(), nullable=False),
        sa.Column("winner_side", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.Text(), nullable=False),
        sa.Column("first_usable_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("strict_mapping_id > 0"),
        sa.CheckConstraint("radiant_win IN (0, 1)"),
        sa.CheckConstraint("winner_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("length(evidence_hash) = 64"),
    )
    _create_research_tables()
    _create_research_triggers()


def _create_research_tables() -> None:
    op.create_table(
        "research_live_predictions",
        sa.Column("prediction_key", sa.Text(), primary_key=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("game_clock_seconds", sa.Integer(), nullable=False),
        sa.Column("game_minute", sa.Double(), nullable=False),
        sa.Column("selected_side", sa.Text(), nullable=False),
        sa.Column("market_probability", sa.Double(), nullable=False),
        sa.Column("market_price", sa.Double(), nullable=False),
        sa.Column("raw_model_probability", sa.Double()),
        sa.Column("feature_hash", sa.Text()),
        sa.Column("model_hash", sa.Text()),
        sa.Column("calibration_hash", sa.Text()),
        sa.Column("transport_key", sa.Text(), sa.ForeignKey("odds_transport_observations.observation_key"), nullable=False),
        sa.Column("transport_hash", sa.Text(), nullable=False),
        sa.Column("radiant_hero_ids_json", sa.Text(), nullable=False),
        sa.Column("dire_hero_ids_json", sa.Text(), nullable=False),
        sa.Column("radiant_team_side", sa.Text()),
        sa.Column("strict_mapping_id", sa.BigInteger(), nullable=False),
        sa.Column("clock_source", sa.Text(), nullable=False),
        sa.Column("clock_trust", sa.Text(), nullable=False),
        sa.Column("manual_clock_event_id", sa.Text(), sa.ForeignKey("browser_events.event_id")),
        sa.Column("manual_clock_seconds", sa.Integer()),
        sa.Column("manual_clock_trust", sa.Text(), nullable=False),
        sa.Column("manual_clock_validation", sa.Text(), nullable=False),
        sa.Column("actionability", sa.Text(), nullable=False),
        sa.Column("gate_status", sa.Text(), nullable=False),
        sa.Column("gate_failures_json", sa.Text(), nullable=False),
        sa.Column("input_context_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("draft_curve_key", sa.Text(), sa.ForeignKey("prospective_draft_curves.curve_key")),
        sa.Column("draft_source_ref", sa.Text()),
        sa.Column("draft_landmark_key", sa.Text(), sa.ForeignKey("prospective_draft_landmarks.landmark_key")),
        sa.Column("draft_landmark_horizon_minutes", sa.Integer()),
        sa.Column("draft_landmark_target", sa.Text()),
        sa.Column("draft_landmark_radiant_probability", sa.Double()),
        sa.Column("draft_landmark_quality", sa.Double()),
        sa.Column("draft_landmark_uncertainty", sa.Double()),
        sa.Column("draft_landmark_support", sa.Integer()),
        sa.Column("draft_radiant_team_side", sa.Text()),
        sa.Column("draft_strict_mapping_id", sa.BigInteger(), sa.ForeignKey("strict_live_map_mappings.mapping_id")),
        sa.Column("draft_deployment_key", sa.Text(), sa.ForeignKey("draft_deployment_bundles.deployment_key")),
        sa.Column("draft_target_snapshot_hash", sa.Text()),
        sa.Column("draft_feature_hash", sa.Text()),
        sa.Column("draft_model_hash", sa.Text(), sa.ForeignKey("draft_model_artifacts.model_hash")),
        sa.Column("draft_calibration_hash", sa.Text(), sa.ForeignKey("draft_calibration_artifacts.calibration_hash")),
        sa.Column("draft_model_version", sa.Text()),
        sa.Column("draft_global_gate_ref", sa.Text()),
        sa.Column("draft_input_snapshot_hash", sa.Text()),
        sa.Column("draft_authority_revision", sa.BigInteger()),
        sa.Column("draft_dependency_revision", sa.BigInteger()),
        sa.CheckConstraint("map_number BETWEEN 1 AND 10"),
        sa.CheckConstraint("game_clock_seconds >= 0"),
        sa.CheckConstraint("game_minute >= 0.0"),
        sa.CheckConstraint("selected_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("market_probability BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("market_price > 1.0"),
        sa.CheckConstraint("raw_model_probability IS NULL OR raw_model_probability BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("feature_hash IS NULL OR length(feature_hash) = 64"),
        sa.CheckConstraint("model_hash IS NULL OR length(model_hash) = 64"),
        sa.CheckConstraint("calibration_hash IS NULL OR length(calibration_hash) = 64"),
        sa.CheckConstraint("length(transport_hash) = 64"),
        sa.CheckConstraint("radiant_team_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("clock_source = 'vision'"),
        sa.CheckConstraint("clock_trust = 'trusted_vision'"),
        sa.CheckConstraint("manual_clock_seconds IS NULL OR manual_clock_seconds >= 0"),
        sa.CheckConstraint("manual_clock_trust IN ('not_observed', 'diagnostic_untrusted')"),
        sa.CheckConstraint("actionability = 'research_only'"),
        sa.CheckConstraint("gate_status IN ('unavailable', 'failed', 'passed')"),
        sa.CheckConstraint("length(input_context_hash) = 64"),
        sa.CheckConstraint("draft_landmark_horizon_minutes IS NULL OR draft_landmark_horizon_minutes IN (10, 20, 30, 40, 50)"),
        sa.CheckConstraint("draft_landmark_target IS NULL OR draft_landmark_target = 'radiant_win'"),
        sa.CheckConstraint("draft_landmark_radiant_probability IS NULL OR draft_landmark_radiant_probability BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("draft_landmark_quality IS NULL OR draft_landmark_quality BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("draft_landmark_uncertainty IS NULL OR draft_landmark_uncertainty BETWEEN 0.0 AND 0.5"),
        sa.CheckConstraint("draft_landmark_support IS NULL OR draft_landmark_support >= 100"),
        sa.CheckConstraint(
            "draft_radiant_team_side IS NULL OR "
            "draft_radiant_team_side IN ('team_one', 'team_two')"
        ),
        sa.CheckConstraint(
            "draft_strict_mapping_id IS NULL OR draft_strict_mapping_id > 0"
        ),
        sa.CheckConstraint(
            "draft_target_snapshot_hash IS NULL OR "
            "length(draft_target_snapshot_hash) = 64"
        ),
        sa.CheckConstraint(
            "draft_feature_hash IS NULL OR length(draft_feature_hash) = 64"
        ),
        sa.CheckConstraint(
            "draft_input_snapshot_hash IS NULL OR "
            "length(draft_input_snapshot_hash) = 64"
        ),
        sa.CheckConstraint(
            "draft_authority_revision IS NULL OR draft_authority_revision >= 1"
        ),
        sa.CheckConstraint(
            "draft_dependency_revision IS NULL OR draft_dependency_revision >= 1"
        ),
    )
    op.create_index("idx_research_prediction_match_time", "research_live_predictions", ["raybet_match_id", "map_number", "observed_at"])
    op.create_table(
        "research_price_labels",
        sa.Column("label_key", sa.Text(), primary_key=True),
        sa.Column("prediction_key", sa.Text(), sa.ForeignKey("research_live_predictions.prediction_key"), nullable=False, unique=True),
        sa.Column("transport_key", sa.Text(), sa.ForeignKey("odds_transport_observations.observation_key"), nullable=False),
        sa.Column("transport_hash", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("selected_side", sa.Text(), nullable=False),
        sa.Column("price", sa.Double(), nullable=False),
        sa.Column("market_probability", sa.Double(), nullable=False),
        sa.Column("seconds_after_prediction", sa.Double(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(transport_hash) = 64"),
        sa.CheckConstraint("selected_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("price > 1.0"),
        sa.CheckConstraint("market_probability BETWEEN 0.0 AND 1.0"),
        sa.CheckConstraint("seconds_after_prediction > 0.0"),
    )
    op.create_index("idx_research_price_transport", "research_price_labels", ["transport_key", "observed_at"])
    op.create_table(
        "research_result_labels",
        sa.Column("label_key", sa.Text(), primary_key=True),
        sa.Column("prediction_key", sa.Text(), sa.ForeignKey("research_live_predictions.prediction_key"), nullable=False, unique=True),
        sa.Column("winner_side", sa.Text(), nullable=False),
        sa.Column("selected_side_win", sa.SmallInteger(), nullable=False),
        sa.Column("dota_match_id", sa.BigInteger(), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("strict_mapping_id", sa.BigInteger()),
        sa.Column("reconciliation_ref", sa.Text()),
        sa.Column("raybet_evidence_id", sa.BigInteger(), sa.ForeignKey("settlement_result_evidence.evidence_id")),
        sa.Column("opendota_evidence_id", sa.BigInteger(), sa.ForeignKey("settlement_result_evidence.evidence_id")),
        sa.Column("raybet_evidence_ref", sa.Text()),
        sa.Column("opendota_evidence_ref", sa.Text()),
        sa.Column("raybet_observed_at", sa.Text()),
        sa.Column("opendota_observed_at", sa.Text()),
        sa.Column("first_usable_at", sa.Text()),
        sa.Column("settled_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("winner_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("selected_side_win IN (0, 1)"),
    )


def _create_research_triggers() -> None:
    for table, prefix, message in (
        ("prospective_draft_outcomes", "prospective_draft_outcomes", "prospective draft outcome is immutable"),
        ("research_live_predictions", "research_live_predictions", "research prediction is append-only"),
        ("research_price_labels", "research_price_labels", "research price label is append-only"),
        ("research_result_labels", "research_result_labels", "research result label is append-only"),
    ):
        for operation in ("UPDATE", "DELETE"):
            op.execute(sa.text(f"CREATE TRIGGER {prefix}_no_{operation.lower()} BEFORE {operation} ON {table} FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row('{message}')"))
    op.execute(sa.text("""
        CREATE FUNCTION require_prospective_outcome_authority()
        RETURNS trigger AS $$ BEGIN
            IF live_text_timestamp_utc(NEW.first_usable_at) IS NULL
               OR live_text_timestamp_utc(NEW.created_at) IS NULL
               OR live_text_timestamp_utc(NEW.first_usable_at) > live_text_timestamp_utc(NEW.created_at)
               OR NOT EXISTS (
                   SELECT 1 FROM prospective_draft_curves AS curve
                   JOIN map_results AS result
                     ON result.raybet_match_id = curve.raybet_match_id
                    AND result.map_number = curve.map_number
                    AND result.strict_mapping_id = curve.strict_mapping_id
                    AND result.dota_match_id = NEW.dota_match_id
                    AND result.winner_side = NEW.winner_side
                    AND result.evidence_ref = NEW.evidence_ref
                    AND result.settled_at = NEW.settled_at
                    AND live_text_timestamp_utc(NEW.first_usable_at) >=
                        live_text_timestamp_utc(result.settled_at)
                   JOIN settlement_reconciliations AS reconciliation
                     ON reconciliation.raybet_match_id = curve.raybet_match_id
                    AND reconciliation.map_number = curve.map_number
                    AND reconciliation.strict_mapping_id = curve.strict_mapping_id
                    AND reconciliation.dota_match_id = NEW.dota_match_id
                    AND reconciliation.status = 'confirmed'
                    AND reconciliation.raybet_winner_side = NEW.winner_side
                    AND reconciliation.opendota_winner_side = NEW.winner_side
                    AND live_text_timestamp_utc(NEW.first_usable_at) >=
                        live_text_timestamp_utc(reconciliation.first_observed_at)
                   WHERE curve.curve_key = NEW.curve_key
                     AND curve.strict_mapping_id = NEW.strict_mapping_id
                     AND NEW.radiant_win = CASE WHEN NEW.winner_side = curve.radiant_team_side THEN 1 ELSE 0 END
                     AND EXISTS (
                         SELECT 1
                         FROM settlement_result_evidence AS evidence
                         WHERE evidence.raybet_match_id = curve.raybet_match_id
                           AND evidence.map_number = curve.map_number
                           AND evidence.dota_match_id = NEW.dota_match_id
                           AND evidence.source = 'raybet'
                           AND evidence.status = 'confirmed'
                           AND evidence.winner_side = NEW.winner_side
                           AND evidence.evidence_ref = reconciliation.raybet_evidence_ref
                           AND live_text_timestamp_utc(NEW.first_usable_at) >=
                               live_text_timestamp_utc(evidence.observed_at)
                     )
                     AND EXISTS (
                         SELECT 1
                         FROM settlement_result_evidence AS evidence
                         WHERE evidence.raybet_match_id = curve.raybet_match_id
                           AND evidence.map_number = curve.map_number
                           AND evidence.dota_match_id = NEW.dota_match_id
                           AND evidence.source = 'opendota'
                           AND evidence.status = 'confirmed'
                           AND evidence.winner_side = NEW.winner_side
                           AND evidence.evidence_ref = reconciliation.opendota_evidence_ref
                           AND live_text_timestamp_utc(NEW.first_usable_at) >=
                               live_text_timestamp_utc(evidence.observed_at)
                     )
               ) THEN RAISE EXCEPTION 'prospective draft outcome authority is required'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("CREATE TRIGGER prospective_draft_outcome_authority_insert BEFORE INSERT ON prospective_draft_outcomes FOR EACH ROW EXECUTE FUNCTION require_prospective_outcome_authority()"))
    op.execute(sa.text("""
        CREATE FUNCTION require_research_draft_authority()
        RETURNS trigger AS $$ BEGIN
            IF NEW.gate_status = 'passed' AND NOT EXISTS (
                SELECT 1 FROM prospective_draft_landmark_authority AS authority
                JOIN draft_authority_revisions AS revision ON revision.singleton = 1
                JOIN draft_lineage_revisions AS lineage ON lineage.singleton = 1
                WHERE authority.curve_key = NEW.draft_curve_key
                  AND authority.source_ref = NEW.draft_source_ref
                  AND authority.raybet_match_id = NEW.raybet_match_id
                  AND authority.map_number = NEW.map_number
                  AND authority.strict_mapping_id = NEW.strict_mapping_id
                  AND authority.strict_mapping_id = NEW.draft_strict_mapping_id
                  AND authority.radiant_hero_ids_json::jsonb = NEW.radiant_hero_ids_json::jsonb
                  AND authority.dire_hero_ids_json::jsonb = NEW.dire_hero_ids_json::jsonb
                  AND live_text_timestamp_utc(authority.first_usable_at) <=
                      live_text_timestamp_utc(NEW.observed_at)
                  AND authority.radiant_team_side = NEW.radiant_team_side
                  AND authority.radiant_team_side = NEW.draft_radiant_team_side
                  AND authority.deployment_key = NEW.draft_deployment_key
                  AND authority.target_snapshot_hash = NEW.draft_target_snapshot_hash
                  AND authority.landmark_key = NEW.draft_landmark_key
                  AND authority.horizon_minutes = NEW.draft_landmark_horizon_minutes
                  AND authority.landmark_target = NEW.draft_landmark_target
                  AND authority.radiant_probability =
                      NEW.draft_landmark_radiant_probability
                  AND authority.quality = NEW.draft_landmark_quality
                  AND authority.uncertainty = NEW.draft_landmark_uncertainty
                  AND authority.support = NEW.draft_landmark_support
                  AND authority.feature_hash = NEW.feature_hash
                  AND authority.feature_hash = NEW.draft_feature_hash
                  AND authority.model_hash = NEW.model_hash
                  AND authority.model_hash = NEW.draft_model_hash
                  AND authority.calibration_hash = NEW.calibration_hash
                  AND authority.calibration_hash = NEW.draft_calibration_hash
                  AND authority.model_version = NEW.draft_model_version
                  AND authority.global_gate_ref = NEW.draft_global_gate_ref
                  AND authority.input_snapshot_hash = NEW.draft_input_snapshot_hash
                  AND revision.authority_revision = NEW.draft_authority_revision
                  AND lineage.dependency_revision = NEW.draft_dependency_revision
                  AND authority.feature_dependency_revision =
                      NEW.draft_dependency_revision
            ) THEN RAISE EXCEPTION 'passed research prediction draft authority is required'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("CREATE TRIGGER research_prediction_draft_authority_insert BEFORE INSERT ON research_live_predictions FOR EACH ROW EXECUTE FUNCTION require_research_draft_authority()"))
    op.execute(sa.text("""
        CREATE FUNCTION require_research_price_authority()
        RETURNS trigger AS $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM research_live_predictions AS prediction
                JOIN odds_transport_observations AS transport
                  ON transport.observation_key = NEW.transport_key
                 AND transport.raybet_match_id = prediction.raybet_match_id
                 AND transport.observed_at = NEW.observed_at
                 AND transport.normalized_state_hash = NEW.transport_hash
                 AND transport.normalized_state_hash_version = 2
                 AND transport.original_legacy_normalized_state_hash IS NULL
                 AND transport.response_state_hash IS NOT NULL
                 AND transport.response_artifact_hash IS NOT NULL
                 AND transport.timing_status = 'on_time'
                 AND transport.processing_status = 'processed'
                JOIN trusted_odds_winner_market_authority AS market
                  ON market.observation_key = transport.observation_key
                 AND market.raybet_match_id = prediction.raybet_match_id
                 AND market.period = 'map_' || prediction.map_number
                 AND market.response_state_hash = transport.response_state_hash
                 AND market.response_artifact_hash = transport.response_artifact_hash
                 AND market.underdog_side = NEW.selected_side
                 AND market.underdog_price = NEW.price
                 AND abs(market.underdog_probability - NEW.market_probability) <= 1e-12
                WHERE prediction.prediction_key = NEW.prediction_key
                  AND prediction.selected_side = NEW.selected_side
                  AND live_text_timestamp_utc(prediction.observed_at) < live_text_timestamp_utc(NEW.observed_at)
                  AND abs(NEW.seconds_after_prediction - extract(epoch FROM (live_text_timestamp_utc(NEW.observed_at) - live_text_timestamp_utc(prediction.observed_at)))) <= 0.001
            ) THEN RAISE EXCEPTION 'research price label authority is required'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("CREATE TRIGGER research_price_label_authority_insert BEFORE INSERT ON research_price_labels FOR EACH ROW EXECUTE FUNCTION require_research_price_authority()"))
    _create_research_result_triggers()
    _create_research_mapping_impact_trigger()


def _create_research_result_triggers() -> None:
    op.execute(sa.text("""
        CREATE FUNCTION require_research_result_authority()
        RETURNS trigger AS $$ BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM research_live_predictions AS prediction
                JOIN map_results AS result
                  ON result.raybet_match_id = prediction.raybet_match_id
                 AND result.map_number = prediction.map_number
                 AND result.strict_mapping_id = prediction.strict_mapping_id
                JOIN settlement_reconciliations AS reconciliation
                  ON reconciliation.raybet_match_id = result.raybet_match_id
                 AND reconciliation.map_number = result.map_number
                 AND reconciliation.strict_mapping_id = result.strict_mapping_id
                 AND reconciliation.dota_match_id = result.dota_match_id
                 AND reconciliation.status = 'confirmed'
                JOIN settlement_result_evidence AS raybet_evidence
                  ON raybet_evidence.evidence_id = result.raybet_evidence_id
                 AND raybet_evidence.source = 'raybet'
                 AND raybet_evidence.status = 'confirmed'
                 AND raybet_evidence.winner_side = result.winner_side
                JOIN settlement_result_evidence AS opendota_evidence
                  ON opendota_evidence.evidence_id = result.opendota_evidence_id
                 AND opendota_evidence.source = 'opendota'
                 AND opendota_evidence.status = 'confirmed'
                 AND opendota_evidence.winner_side = result.winner_side
                WHERE prediction.prediction_key = NEW.prediction_key
                  AND NEW.winner_side = result.winner_side
                  AND NEW.selected_side_win = CASE
                      WHEN prediction.selected_side = result.winner_side THEN 1
                      ELSE 0
                  END
                  AND NEW.dota_match_id = result.dota_match_id
                  AND NEW.evidence_ref = result.evidence_ref
                  AND NEW.strict_mapping_id = result.strict_mapping_id
                  AND NEW.reconciliation_ref = result.reconciliation_ref
                  AND NEW.raybet_evidence_id = result.raybet_evidence_id
                  AND NEW.opendota_evidence_id = result.opendota_evidence_id
                  AND NEW.raybet_evidence_ref = result.raybet_evidence_ref
                  AND NEW.opendota_evidence_ref = result.opendota_evidence_ref
                  AND NEW.raybet_observed_at = result.raybet_observed_at
                  AND NEW.opendota_observed_at = result.opendota_observed_at
                  AND NEW.first_usable_at = result.first_usable_at
                  AND NEW.settled_at = result.settled_at
                  AND reconciliation.evidence_ref = result.reconciliation_ref
                  AND reconciliation.raybet_evidence_id = result.raybet_evidence_id
                  AND reconciliation.opendota_evidence_id = result.opendota_evidence_id
                  AND reconciliation.first_usable_at = result.first_usable_at
                  AND raybet_evidence.evidence_ref = result.raybet_evidence_ref
                  AND raybet_evidence.observed_at = result.raybet_observed_at
                  AND opendota_evidence.evidence_ref = result.opendota_evidence_ref
                  AND opendota_evidence.observed_at = result.opendota_observed_at
                  AND live_text_timestamp_utc(prediction.observed_at) <
                      live_text_timestamp_utc(result.first_usable_at)
            ) THEN
                RAISE EXCEPTION 'research result label authority is required';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER research_result_label_authority_insert
        BEFORE INSERT ON research_result_labels
        FOR EACH ROW EXECUTE FUNCTION require_research_result_authority()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION create_research_results_for_map()
        RETURNS trigger AS $$ BEGIN
            INSERT INTO research_result_labels (
                label_key, prediction_key, winner_side, selected_side_win,
                dota_match_id, evidence_ref, strict_mapping_id,
                reconciliation_ref, raybet_evidence_id, opendota_evidence_id,
                raybet_evidence_ref, opendota_evidence_ref,
                raybet_observed_at, opendota_observed_at, first_usable_at,
                settled_at, created_at
            )
            SELECT prediction.prediction_key || chr(58) || 'result',
                   prediction.prediction_key,
                   NEW.winner_side,
                   CASE WHEN prediction.selected_side = NEW.winner_side
                        THEN 1 ELSE 0 END,
                   NEW.dota_match_id, NEW.evidence_ref, NEW.strict_mapping_id,
                   NEW.reconciliation_ref, NEW.raybet_evidence_id,
                   NEW.opendota_evidence_id, NEW.raybet_evidence_ref,
                   NEW.opendota_evidence_ref, NEW.raybet_observed_at,
                   NEW.opendota_observed_at, NEW.first_usable_at,
                   NEW.settled_at, NEW.settled_at
            FROM research_live_predictions AS prediction
            WHERE prediction.raybet_match_id = NEW.raybet_match_id
              AND prediction.map_number = NEW.map_number
              AND prediction.strict_mapping_id = NEW.strict_mapping_id
              AND live_text_timestamp_utc(prediction.observed_at) <
                  live_text_timestamp_utc(NEW.settled_at)
            ON CONFLICT (prediction_key) DO NOTHING;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER research_result_from_map_result
        AFTER INSERT ON map_results
        FOR EACH ROW EXECUTE FUNCTION create_research_results_for_map()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION create_research_result_for_late_prediction()
        RETURNS trigger AS $$ BEGIN
            INSERT INTO research_result_labels (
                label_key, prediction_key, winner_side, selected_side_win,
                dota_match_id, evidence_ref, strict_mapping_id,
                reconciliation_ref, raybet_evidence_id, opendota_evidence_id,
                raybet_evidence_ref, opendota_evidence_ref,
                raybet_observed_at, opendota_observed_at, first_usable_at,
                settled_at, created_at
            )
            SELECT NEW.prediction_key || chr(58) || 'result', NEW.prediction_key,
                   result.winner_side,
                   CASE WHEN NEW.selected_side = result.winner_side
                        THEN 1 ELSE 0 END,
                   result.dota_match_id, result.evidence_ref,
                   result.strict_mapping_id, result.reconciliation_ref,
                   result.raybet_evidence_id, result.opendota_evidence_id,
                   result.raybet_evidence_ref, result.opendota_evidence_ref,
                   result.raybet_observed_at, result.opendota_observed_at,
                   result.first_usable_at, result.settled_at, NEW.created_at
            FROM map_results AS result
            WHERE result.raybet_match_id = NEW.raybet_match_id
              AND result.map_number = NEW.map_number
              AND result.strict_mapping_id = NEW.strict_mapping_id
              AND live_text_timestamp_utc(NEW.observed_at) <
                  live_text_timestamp_utc(result.settled_at)
            ON CONFLICT (prediction_key) DO NOTHING;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER research_result_from_late_prediction
        AFTER INSERT ON research_live_predictions
        FOR EACH ROW EXECUTE FUNCTION create_research_result_for_late_prediction()
    """))


def _create_research_mapping_impact_trigger() -> None:
    op.execute(sa.text("""
        CREATE FUNCTION record_invalidated_research_mapping_impact()
        RETURNS trigger AS $$ BEGIN
            INSERT INTO strict_live_mapping_impacts (
                mapping_id, invalidation_id, dependent_type,
                dependent_key, reason, recorded_at
            )
            SELECT NEW.strict_mapping_id, cause.invalidation_id,
                   'research_prediction', NEW.prediction_key, cause.reason,
                   replace(
                       to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                       '_', ':'
                   )
            FROM (
                SELECT invalidation.invalidation_id, invalidation.reason
                FROM strict_live_map_mapping_invalidations AS invalidation
                WHERE invalidation.mapping_id = NEW.strict_mapping_id
                UNION ALL
                SELECT invalidation.invalidation_id, invalidation.reason
                FROM strict_live_map_mappings AS mapping
                JOIN strict_live_automatic_evidence_approvals AS approval
                  ON approval.approval_id = mapping.automatic_approval_id
                JOIN strict_live_map_mapping_invalidations AS invalidation
                  ON invalidation.mapping_id = approval.source_mapping_id
                WHERE mapping.mapping_id = NEW.strict_mapping_id
                LIMIT 1
            ) AS cause
            ON CONFLICT (mapping_id, dependent_type, dependent_key) DO NOTHING;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER strict_live_research_impact_after_insert
        AFTER INSERT ON research_live_predictions
        FOR EACH ROW EXECUTE FUNCTION record_invalidated_research_mapping_impact()
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER research_result_from_map_result ON map_results"))
    op.drop_table("research_result_labels")
    op.drop_table("research_price_labels")
    op.drop_table("research_live_predictions")
    op.drop_table("prospective_draft_outcomes")
    for function in (
        "record_invalidated_research_mapping_impact",
        "create_research_result_for_late_prediction",
        "create_research_results_for_map",
        "require_research_result_authority",
        "require_research_price_authority",
        "require_research_draft_authority",
        "require_prospective_outcome_authority",
    ):
        op.execute(sa.text(f"DROP FUNCTION {function}()"))
