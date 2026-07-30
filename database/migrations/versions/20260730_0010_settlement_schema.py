"""Create settlement evidence, reconciliation, and ledger authority.

Revision ID: 20260730_0010
Revises: 20260730_0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0010"
down_revision: str | None = "20260730_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_evidence_and_reconciliation()
    _create_map_results()
    _create_settlement_ledger()
    _create_settlement_triggers()


def _create_evidence_and_reconciliation() -> None:
    op.create_table(
        "settlement_result_evidence",
        sa.Column("evidence_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("dota_match_id", sa.BigInteger()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("winner_side", sa.Text()),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("first_usable_at", sa.Text()),
        sa.Column("raybet_audit_key", sa.Text(), sa.ForeignKey("direct_response_audit.audit_key")),
        sa.Column("raybet_transport_key", sa.Text(), sa.ForeignKey("odds_transport_observations.observation_key")),
        sa.Column("raybet_response_state_hash", sa.Text(), sa.ForeignKey("odds_response_states.response_state_hash")),
        sa.Column("raybet_response_artifact_hash", sa.Text(), sa.ForeignKey("odds_raw_artifacts.artifact_hash")),
        sa.Column("opendota_artifact_id", sa.Text(), sa.ForeignKey("raw_source_artifacts.artifact_id")),
        sa.Column("opendota_observation_id", sa.Text(), sa.ForeignKey("raw_source_observations.observation_id")),
        sa.Column("opendota_content_hash", sa.Text()),
        sa.UniqueConstraint("raybet_match_id", "map_number", "source", "evidence_ref"),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint("source IN ('raybet', 'opendota')"),
        sa.CheckConstraint("status IN ('confirmed', 'pending', 'conflict')"),
        sa.CheckConstraint("winner_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("facts_json::jsonb IS NOT NULL"),
        sa.CheckConstraint("opendota_content_hash IS NULL OR length(opendota_content_hash) = 64"),
    )
    op.create_index(
        "idx_settlement_result_evidence_map",
        "settlement_result_evidence",
        ["raybet_match_id", "map_number", "source", "observed_at"],
    )
    op.create_table(
        "settlement_reconciliations",
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("strict_mapping_id", sa.BigInteger(), sa.ForeignKey("strict_live_map_mappings.mapping_id")),
        sa.Column("dota_match_id", sa.BigInteger(), nullable=False),
        sa.Column("raybet_winner_side", sa.Text()),
        sa.Column("opendota_winner_side", sa.Text(), nullable=False),
        sa.Column("raybet_evidence_ref", sa.Text(), nullable=False),
        sa.Column("opendota_evidence_ref", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.Text()),
        sa.Column("raybet_evidence_id", sa.BigInteger(), sa.ForeignKey("settlement_result_evidence.evidence_id")),
        sa.Column("opendota_evidence_id", sa.BigInteger(), sa.ForeignKey("settlement_result_evidence.evidence_id")),
        sa.Column("raybet_observed_at", sa.Text()),
        sa.Column("opendota_observed_at", sa.Text()),
        sa.Column("first_usable_at", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("first_observed_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("raybet_match_id", "map_number"),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint("strict_mapping_id IS NULL OR strict_mapping_id > 0"),
        sa.CheckConstraint("raybet_winner_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("opendota_winner_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("status IN ('pending', 'confirmed', 'manual_review')"),
    )
    op.create_index(
        "idx_settlement_reconciliations_dota_match",
        "settlement_reconciliations",
        ["dota_match_id"],
    )


def _create_map_results() -> None:
    op.create_table(
        "shadow_map_attempts",
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("order_key", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("raybet_match_id", "map_number"),
    )
    op.create_table(
        "map_results",
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("strict_mapping_id", sa.BigInteger(), sa.ForeignKey("strict_live_map_mappings.mapping_id")),
        sa.Column("dota_match_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("winner_side", sa.Text(), nullable=False),
        sa.Column("team_one_kills", sa.Integer()),
        sa.Column("team_two_kills", sa.Integer()),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("reconciliation_ref", sa.Text()),
        sa.Column("raybet_evidence_id", sa.BigInteger(), sa.ForeignKey("settlement_result_evidence.evidence_id")),
        sa.Column("opendota_evidence_id", sa.BigInteger(), sa.ForeignKey("settlement_result_evidence.evidence_id")),
        sa.Column("raybet_evidence_ref", sa.Text()),
        sa.Column("opendota_evidence_ref", sa.Text()),
        sa.Column("raybet_observed_at", sa.Text()),
        sa.Column("opendota_observed_at", sa.Text()),
        sa.Column("first_usable_at", sa.Text()),
        sa.Column("settled_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("raybet_match_id", "map_number"),
        sa.CheckConstraint("strict_mapping_id IS NULL OR strict_mapping_id > 0"),
    )


def _create_settlement_ledger() -> None:
    op.create_table(
        "settlement_authority_audit",
        sa.Column("audit_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("order_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("order_key", "status", "reason", "actor"),
        sa.CheckConstraint("status = 'manual_review'"),
    )
    op.create_table(
        "settlement_authority",
        sa.Column("order_key", sa.Text(), sa.ForeignKey("shadow_orders.order_key"), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("strict_mapping_id", sa.BigInteger(), sa.ForeignKey("strict_live_map_mappings.mapping_id"), nullable=False),
        sa.Column("dota_match_id", sa.BigInteger(), nullable=False),
        sa.Column("winner_side", sa.Text(), nullable=False),
        sa.Column("fill_price", sa.Double(), nullable=False),
        sa.Column("stake_units", sa.Double(), nullable=False),
        sa.Column("derived_result", sa.Text(), nullable=False),
        sa.Column("derived_return_units", sa.Double(), nullable=False),
        sa.Column("derived_return_amount", sa.Double(), nullable=False),
        sa.Column("map_result_evidence_ref", sa.Text(), nullable=False),
        sa.Column("raybet_evidence_ref", sa.Text(), nullable=False),
        sa.Column("opendota_evidence_ref", sa.Text(), nullable=False),
        sa.Column("raybet_evidence_id", sa.BigInteger(), sa.ForeignKey("settlement_result_evidence.evidence_id"), nullable=False),
        sa.Column("opendota_evidence_id", sa.BigInteger(), sa.ForeignKey("settlement_result_evidence.evidence_id"), nullable=False),
        sa.Column("raybet_observed_at", sa.Text(), nullable=False),
        sa.Column("opendota_observed_at", sa.Text(), nullable=False),
        sa.Column("first_usable_at", sa.Text(), nullable=False),
        sa.Column("reconciliation_updated_at", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.CheckConstraint("map_number > 0"),
        sa.CheckConstraint("strict_mapping_id > 0"),
        sa.CheckConstraint("dota_match_id > 0"),
        sa.CheckConstraint("winner_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("fill_price > 1.0"),
        sa.CheckConstraint("stake_units > 0.0"),
        sa.CheckConstraint("derived_result IN ('win', 'loss')"),
        sa.CheckConstraint("derived_return_units >= 0.0"),
        sa.CheckConstraint("derived_return_amount >= 0.0"),
    )
    op.create_table(
        "settlements",
        sa.Column("order_key", sa.Text(), primary_key=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("return_units", sa.Double(), nullable=False),
        sa.Column("settled_at", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("review_required", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "notification_outbox_audit",
        sa.Column("audit_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("outbox_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )


def _create_settlement_triggers() -> None:
    for table, prefix, message in (
        ("settlement_result_evidence", "settlement_result_evidence", "settlement result evidence is append-only"),
        ("map_results", "map_results", "map results are immutable"),
        ("settlement_authority", "settlement_authority", "settlement authority is immutable"),
        ("settlement_authority_audit", "settlement_authority_audit", "settlement authority audit is immutable"),
    ):
        for operation in ("UPDATE", "DELETE"):
            op.execute(sa.text(f"""
                CREATE TRIGGER {prefix}_immutable_{operation.lower()}
                BEFORE {operation} ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row('{message}')
            """))
    op.execute(sa.text("""
        CREATE FUNCTION require_settlement_source_evidence()
        RETURNS trigger AS $$
        BEGIN
            IF live_text_timestamp_utc(NEW.observed_at) IS NULL
               OR live_text_timestamp_utc(NEW.first_usable_at) IS NULL
               OR live_text_timestamp_utc(NEW.first_usable_at) < live_text_timestamp_utc(NEW.observed_at)
               OR (NEW.source = 'raybet' AND (
                    NEW.raybet_audit_key IS NULL
                    OR NEW.raybet_response_artifact_hash IS NULL
                    OR NEW.opendota_artifact_id IS NOT NULL
                    OR NEW.opendota_observation_id IS NOT NULL
                    OR NEW.opendota_content_hash IS NOT NULL
                    OR live_text_timestamp_utc(NEW.first_usable_at) != live_text_timestamp_utc(NEW.observed_at)
                    OR NOT EXISTS (
                        SELECT 1 FROM direct_response_audit AS audit
                        WHERE audit.audit_key = NEW.raybet_audit_key
                          AND audit.source = 'direct'
                          AND audit.response_kind = 'final_odds'
                          AND audit.claimed_raybet_match_id = NEW.raybet_match_id
                          AND audit.observed_raybet_match_id = NEW.raybet_match_id
                          AND audit.disposition IN ('accepted', 'audit_only')
                          AND audit.observed_at = NEW.observed_at
                          AND audit.artifact_hash = NEW.raybet_response_artifact_hash
                    )
               )) OR (NEW.source = 'opendota' AND (
                    NEW.opendota_artifact_id IS NULL
                    OR NEW.opendota_observation_id IS NULL
                    OR NEW.opendota_content_hash IS NULL
                    OR NEW.evidence_ref != 'opendota:' || NEW.dota_match_id || ':sha256:' || NEW.opendota_content_hash
                    OR NEW.raybet_audit_key IS NOT NULL
                    OR NEW.raybet_transport_key IS NOT NULL
                    OR NEW.raybet_response_state_hash IS NOT NULL
                    OR NEW.raybet_response_artifact_hash IS NOT NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM raw_source_observations AS observation
                        JOIN raw_source_artifacts AS artifact
                          ON artifact.artifact_id = observation.artifact_id
                         AND artifact.content_hash = observation.content_hash
                        WHERE observation.observation_id = NEW.opendota_observation_id
                          AND observation.artifact_id = NEW.opendota_artifact_id
                          AND observation.content_hash = NEW.opendota_content_hash
                          AND observation.source = 'opendota'
                          AND observation.artifact_use = 'primary'
                          AND observation.match_id = NEW.dota_match_id
                          AND observation.received_at = NEW.observed_at
                          AND observation.first_usable_at = NEW.first_usable_at
                          AND artifact.first_usable_at = NEW.first_usable_at
                    )
               )) THEN
                RAISE EXCEPTION 'settlement source evidence authority is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER settlement_result_evidence_authority_insert
        BEFORE INSERT ON settlement_result_evidence
        FOR EACH ROW EXECUTE FUNCTION require_settlement_source_evidence()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION require_confirmed_reconciliation_authority()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.strict_mapping_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM strict_live_map_mappings AS mapping
                WHERE mapping.mapping_id = NEW.strict_mapping_id
                  AND mapping.raybet_match_id = NEW.raybet_match_id
                  AND mapping.map_number = NEW.map_number
            ) OR (NEW.status = 'confirmed' AND (
                NEW.evidence_ref != 'settlement-reconciliation:' || NEW.raybet_match_id || ':map:' || NEW.map_number
                OR NOT EXISTS (
                    SELECT 1 FROM settlement_result_evidence AS raybet
                    JOIN settlement_result_evidence AS opendota ON opendota.evidence_id = NEW.opendota_evidence_id
                    WHERE raybet.evidence_id = NEW.raybet_evidence_id
                      AND raybet.source = 'raybet' AND opendota.source = 'opendota'
                      AND raybet.status = 'confirmed' AND opendota.status = 'confirmed'
                      AND raybet.raybet_match_id = NEW.raybet_match_id
                      AND opendota.raybet_match_id = NEW.raybet_match_id
                      AND raybet.map_number = NEW.map_number AND opendota.map_number = NEW.map_number
                      AND raybet.dota_match_id = NEW.dota_match_id AND opendota.dota_match_id = NEW.dota_match_id
                      AND raybet.winner_side = NEW.raybet_winner_side
                      AND opendota.winner_side = NEW.opendota_winner_side
                      AND raybet.evidence_ref = NEW.raybet_evidence_ref
                      AND opendota.evidence_ref = NEW.opendota_evidence_ref
                )
            )) THEN
                RAISE EXCEPTION 'settlement reconciliation authority is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER settlement_reconciliation_authority_insert
        BEFORE INSERT ON settlement_reconciliations
        FOR EACH ROW EXECUTE FUNCTION require_confirmed_reconciliation_authority()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION guard_settlement_reconciliation_update()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'manual_review' AND NEW.status != 'manual_review' THEN
                RAISE EXCEPTION 'settlement reconciliation manual review is immutable';
            END IF;
            IF OLD.status = 'confirmed' AND OLD IS DISTINCT FROM NEW THEN
                RAISE EXCEPTION 'settlement reconciliation authority is immutable';
            END IF;
            IF OLD.raybet_match_id IS DISTINCT FROM NEW.raybet_match_id
               OR OLD.map_number IS DISTINCT FROM NEW.map_number
               OR OLD.strict_mapping_id IS DISTINCT FROM NEW.strict_mapping_id THEN
                RAISE EXCEPTION 'settlement reconciliation mapping is immutable';
            END IF;
            IF OLD.status != 'confirmed' AND NEW.status = 'confirmed' AND (
                NEW.evidence_ref != 'settlement-reconciliation:' || NEW.raybet_match_id || ':map:' || NEW.map_number
                OR NOT EXISTS (
                    SELECT 1 FROM settlement_result_evidence AS raybet
                    JOIN settlement_result_evidence AS opendota
                      ON opendota.evidence_id = NEW.opendota_evidence_id
                    WHERE raybet.evidence_id = NEW.raybet_evidence_id
                      AND raybet.source = 'raybet'
                      AND opendota.source = 'opendota'
                      AND raybet.status = 'confirmed'
                      AND opendota.status = 'confirmed'
                      AND raybet.raybet_match_id = NEW.raybet_match_id
                      AND opendota.raybet_match_id = NEW.raybet_match_id
                      AND raybet.map_number = NEW.map_number
                      AND opendota.map_number = NEW.map_number
                      AND raybet.dota_match_id = NEW.dota_match_id
                      AND opendota.dota_match_id = NEW.dota_match_id
                      AND raybet.winner_side = NEW.raybet_winner_side
                      AND opendota.winner_side = NEW.opendota_winner_side
                )
            ) THEN
                RAISE EXCEPTION 'settlement reconciliation authority is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER settlement_reconciliation_authority_update
        BEFORE UPDATE ON settlement_reconciliations
        FOR EACH ROW EXECUTE FUNCTION guard_settlement_reconciliation_update()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION require_map_result_authority()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.strict_mapping_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM settlement_reconciliations AS reconciliation
                WHERE reconciliation.raybet_match_id = NEW.raybet_match_id
                  AND reconciliation.map_number = NEW.map_number
                  AND reconciliation.strict_mapping_id = NEW.strict_mapping_id
                  AND reconciliation.dota_match_id = NEW.dota_match_id
                  AND reconciliation.status = 'confirmed'
                  AND reconciliation.raybet_winner_side = NEW.winner_side
                  AND reconciliation.opendota_winner_side = NEW.winner_side
                  AND reconciliation.evidence_ref = NEW.evidence_ref
                  AND reconciliation.evidence_ref = NEW.reconciliation_ref
                  AND reconciliation.raybet_evidence_id = NEW.raybet_evidence_id
                  AND reconciliation.opendota_evidence_id = NEW.opendota_evidence_id
                  AND reconciliation.first_usable_at = NEW.first_usable_at
                  AND live_text_timestamp_utc(NEW.settled_at) = live_text_timestamp_utc(NEW.first_usable_at)
            ) THEN
                RAISE EXCEPTION 'map result mapping authority is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER map_result_mapping_authority_insert
        BEFORE INSERT ON map_results
        FOR EACH ROW EXECUTE FUNCTION require_map_result_authority()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION require_settlement_authority_chain()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM shadow_orders AS orders
                JOIN shadow_map_attempts AS attempt ON attempt.order_key = orders.order_key
                JOIN map_results AS result
                  ON result.raybet_match_id = orders.raybet_match_id
                 AND result.map_number = attempt.map_number
                JOIN settlement_reconciliations AS reconciliation
                  ON reconciliation.raybet_match_id = result.raybet_match_id
                 AND reconciliation.map_number = result.map_number
                WHERE orders.order_key = NEW.order_key
                  AND orders.status = 'filled' AND attempt.status = 'filled'
                  AND orders.strict_mapping_id = NEW.strict_mapping_id
                  AND result.strict_mapping_id = NEW.strict_mapping_id
                  AND result.dota_match_id = NEW.dota_match_id
                  AND result.winner_side = NEW.winner_side
                  AND orders.fill_price = NEW.fill_price
                  AND orders.stake = NEW.stake_units
                  AND NEW.derived_result = CASE WHEN orders.signal_outcome_key = result.winner_side THEN 'win' ELSE 'loss' END
                  AND abs(NEW.derived_return_units - CASE WHEN orders.signal_outcome_key = result.winner_side THEN orders.fill_price ELSE 0.0 END) <= 1e-12
                  AND abs(NEW.derived_return_amount - NEW.derived_return_units * orders.stake) <= 1e-12
                  AND NEW.map_result_evidence_ref = result.evidence_ref
                  AND NEW.raybet_evidence_id = reconciliation.raybet_evidence_id
                  AND NEW.opendota_evidence_id = reconciliation.opendota_evidence_id
            ) THEN
                RAISE EXCEPTION 'settlement authority chain is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER settlement_authority_insert_guard
        BEFORE INSERT ON settlement_authority
        FOR EACH ROW EXECUTE FUNCTION require_settlement_authority_chain()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION require_formal_settlement_authority()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.review_required = 0 AND NOT EXISTS (
                SELECT 1 FROM settlement_authority AS authority
                WHERE authority.order_key = NEW.order_key
                  AND authority.derived_result = NEW.result
                  AND abs(authority.derived_return_units - NEW.return_units) <= 1e-12
                  AND authority.settled_at = NEW.settled_at
                  AND authority.map_result_evidence_ref = NEW.evidence_ref
            ) THEN
                RAISE EXCEPTION 'formal settlement authority is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER settlements_authority_insert_guard
        BEFORE INSERT ON settlements
        FOR EACH ROW EXECUTE FUNCTION require_formal_settlement_authority()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION guard_settlement_update()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.order_key IS DISTINCT FROM NEW.order_key
               OR OLD.result IS DISTINCT FROM NEW.result
               OR OLD.return_units IS DISTINCT FROM NEW.return_units
               OR OLD.settled_at IS DISTINCT FROM NEW.settled_at
               OR OLD.evidence_ref IS DISTINCT FROM NEW.evidence_ref
               OR NOT (OLD.review_required = NEW.review_required OR (OLD.review_required = 0 AND NEW.review_required = 1))
            THEN RAISE EXCEPTION 'settlement core state is immutable'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("CREATE TRIGGER settlements_core_immutable BEFORE UPDATE ON settlements FOR EACH ROW EXECUTE FUNCTION guard_settlement_update()"))
    op.execute(sa.text("CREATE TRIGGER settlements_immutable_delete BEFORE DELETE ON settlements FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row('settlements are immutable')"))


def downgrade() -> None:
    op.drop_table("notification_outbox_audit")
    op.drop_table("settlements")
    op.drop_table("settlement_authority")
    op.drop_table("settlement_authority_audit")
    op.drop_table("map_results")
    op.drop_table("shadow_map_attempts")
    op.drop_table("settlement_reconciliations")
    op.drop_table("settlement_result_evidence")
    for function in (
        "guard_settlement_update", "require_formal_settlement_authority",
        "require_settlement_authority_chain", "require_map_result_authority",
        "guard_settlement_reconciliation_update",
        "require_confirmed_reconciliation_authority",
        "require_settlement_source_evidence",
    ):
        op.execute(sa.text(f"DROP FUNCTION {function}()"))
