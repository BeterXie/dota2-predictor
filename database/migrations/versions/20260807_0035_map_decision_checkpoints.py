"""Add independent Map decision checkpoints and shadow settlements.

Revision ID: 20260807_0035
Revises: 20260807_0034
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0035"
down_revision: str | None = "20260807_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"live_text_timestamp_utc({column}) IS NOT NULL")


def _optional_utc(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IS NULL OR live_text_timestamp_utc({column}) IS NOT NULL"
    )


def upgrade() -> None:
    op.create_table(
        "map_decision_checkpoints",
        sa.Column(
            "checkpoint_id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
        ),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("mapping_version", sa.Integer()),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("checkpoint_minute", sa.Integer(), nullable=False),
        sa.Column("strategy_version", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("assumed_stake_units", sa.Double(), nullable=False),
        sa.Column("observed_price", sa.Double()),
        sa.Column("model_probability_team_one", sa.Double()),
        sa.Column("model_probability_team_two", sa.Double()),
        sa.Column("market_probability_team_one", sa.Double()),
        sa.Column("market_probability_team_two", sa.Double()),
        sa.Column("selected_edge", sa.Double()),
        sa.Column("odds_observation_key", sa.Text()),
        sa.Column("odds_group_id", sa.Text()),
        sa.Column("odds_observed_at", sa.Text()),
        sa.Column("odds_age_seconds", sa.Double()),
        sa.Column("odds_max_age_seconds", sa.Double(), nullable=False),
        sa.Column(
            "vision_snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("live_game_snapshots.snapshot_id"),
        ),
        sa.Column("vision_source_frame_ref", sa.Text()),
        sa.Column("vision_captured_at", sa.Text()),
        sa.Column("vision_game_time_seconds", sa.Integer()),
        sa.Column("vision_networth_lead", sa.BigInteger()),
        sa.Column("vision_radiant_kills", sa.Integer()),
        sa.Column("vision_dire_kills", sa.Integer()),
        sa.Column("vision_age_seconds", sa.Double()),
        sa.Column("vision_max_age_seconds", sa.Double()),
        sa.Column("odds_vision_gap_seconds", sa.Double()),
        sa.Column("odds_vision_gap_max_seconds", sa.Double()),
        sa.Column("vision_trusted", sa.Boolean(), nullable=False),
        sa.Column("vision_replay", sa.Boolean(), nullable=False),
        sa.Column("input_versions_json", sa.Text(), nullable=False),
        sa.Column("feature_availability_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "raybet_match_id",
            "map_number",
            "phase",
            "checkpoint_minute",
            "strategy_version",
        ),
        sa.CheckConstraint("length(trim(raybet_match_id)) > 0"),
        sa.CheckConstraint("map_number BETWEEN 1 AND 5"),
        sa.CheckConstraint("mapping_version IS NULL OR mapping_version > 0"),
        sa.CheckConstraint("phase IN ('pregame', 'live')"),
        sa.CheckConstraint(
            "(phase='pregame' AND checkpoint_minute=0) OR "
            "(phase='live' AND checkpoint_minute>=5 AND checkpoint_minute%5=0)"
        ),
        sa.CheckConstraint("strategy_version='map-decision-shadow-v1'"),
        sa.CheckConstraint("decision IN ('bet_team_a', 'bet_team_b', 'skip')"),
        sa.CheckConstraint("assumed_stake_units=1.0"),
        sa.CheckConstraint("observed_price IS NULL OR observed_price>1.0"),
        sa.CheckConstraint(
            "model_probability_team_one IS NULL OR "
            "model_probability_team_one BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "model_probability_team_two IS NULL OR "
            "model_probability_team_two BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "market_probability_team_one IS NULL OR "
            "market_probability_team_one BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint(
            "market_probability_team_two IS NULL OR "
            "market_probability_team_two BETWEEN 0.0 AND 1.0"
        ),
        sa.CheckConstraint("odds_age_seconds IS NULL OR odds_age_seconds>=0.0"),
        sa.CheckConstraint(
            "(phase='pregame' AND odds_max_age_seconds=150.0) OR "
            "(phase='live' AND odds_max_age_seconds=15.0)"
        ),
        sa.CheckConstraint(
            "vision_game_time_seconds IS NULL OR vision_game_time_seconds>=0"
        ),
        sa.CheckConstraint("vision_radiant_kills IS NULL OR vision_radiant_kills>=0"),
        sa.CheckConstraint("vision_dire_kills IS NULL OR vision_dire_kills>=0"),
        sa.CheckConstraint("vision_age_seconds IS NULL OR vision_age_seconds>=0.0"),
        sa.CheckConstraint(
            "(phase='pregame' AND vision_max_age_seconds IS NULL AND "
            "odds_vision_gap_max_seconds IS NULL) OR "
            "(phase='live' AND vision_max_age_seconds=5.0 AND "
            "odds_vision_gap_max_seconds=15.0)"
        ),
        sa.CheckConstraint(
            "odds_vision_gap_seconds IS NULL OR odds_vision_gap_seconds>=0.0"
        ),
        sa.CheckConstraint("vision_replay=FALSE"),
        sa.CheckConstraint("length(trim(reason)) > 0"),
        sa.CheckConstraint("jsonb_typeof(input_versions_json::jsonb)='object'"),
        sa.CheckConstraint("jsonb_typeof(feature_availability_json::jsonb)='object'"),
        sa.CheckConstraint(
            "(decision='skip') OR (observed_price IS NOT NULL AND "
            "model_probability_team_one IS NOT NULL AND "
            "model_probability_team_two IS NOT NULL AND "
            "market_probability_team_one IS NOT NULL AND "
            "market_probability_team_two IS NOT NULL AND "
            "selected_edge>=0.08)"
        ),
        _optional_utc("odds_observed_at"),
        _optional_utc("vision_captured_at"),
        _utc("decided_at"),
        _utc("created_at"),
    )
    op.create_index(
        "ix_map_decision_checkpoints_map_time",
        "map_decision_checkpoints",
        ["raybet_match_id", "map_number", "phase", "checkpoint_minute"],
    )

    op.create_table(
        "map_decision_checkpoint_settlements",
        sa.Column(
            "settlement_id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
        ),
        sa.Column(
            "checkpoint_id",
            sa.BigInteger(),
            sa.ForeignKey("map_decision_checkpoints.checkpoint_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("map_number", sa.Integer(), nullable=False),
        sa.Column("dota_match_id", sa.BigInteger(), nullable=False),
        sa.Column("winner_side", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("gross_return_units", sa.Double(), nullable=False),
        sa.Column("profit_units", sa.Double(), nullable=False),
        sa.Column("result_source", sa.Text(), nullable=False),
        sa.Column("result_recorded_at", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(trim(raybet_match_id)) > 0"),
        sa.CheckConstraint("map_number BETWEEN 1 AND 5"),
        sa.CheckConstraint("dota_match_id > 0"),
        sa.CheckConstraint("winner_side IN ('team_one', 'team_two')"),
        sa.CheckConstraint("outcome IN ('win', 'loss', 'skip')"),
        sa.CheckConstraint("gross_return_units>=0.0"),
        sa.CheckConstraint("result_source='confirmed_map_result'"),
        _utc("result_recorded_at"),
        _utc("settled_at"),
        _utc("created_at"),
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION validate_map_decision_checkpoint_settlement()
            RETURNS trigger AS $$
            DECLARE
                checkpoint map_decision_checkpoints%ROWTYPE;
                result_row map_results%ROWTYPE;
                expected_outcome text;
                expected_gross double precision;
                expected_profit double precision;
                selected_side text;
            BEGIN
                SELECT * INTO checkpoint FROM map_decision_checkpoints
                 WHERE checkpoint_id=NEW.checkpoint_id;
                SELECT result.* INTO result_row
                  FROM map_results AS result
                  JOIN settlement_reconciliations AS reconciliation
                    ON reconciliation.raybet_match_id=result.raybet_match_id
                   AND reconciliation.map_number=result.map_number
                   AND reconciliation.dota_match_id=result.dota_match_id
                   AND reconciliation.status='confirmed'
                 WHERE result.raybet_match_id=NEW.raybet_match_id
                   AND result.map_number=NEW.map_number
                   AND result.dota_match_id=NEW.dota_match_id;
                IF checkpoint.checkpoint_id IS NULL OR result_row.dota_match_id IS NULL OR
                   checkpoint.raybet_match_id<>NEW.raybet_match_id OR
                   checkpoint.map_number<>NEW.map_number OR
                   result_row.winner_side<>NEW.winner_side OR
                   live_text_timestamp_utc(result_row.settled_at) <>
                       live_text_timestamp_utc(NEW.result_recorded_at)
                THEN
                    RAISE EXCEPTION 'checkpoint settlement authority disagrees';
                END IF;
                IF checkpoint.decision='skip' THEN
                    expected_outcome := 'skip';
                    expected_gross := 0.0;
                    expected_profit := 0.0;
                ELSE
                    selected_side := CASE checkpoint.decision
                        WHEN 'bet_team_a' THEN 'team_one' ELSE 'team_two' END;
                    IF selected_side=NEW.winner_side THEN
                        expected_outcome := 'win';
                        expected_gross := checkpoint.observed_price;
                        expected_profit := checkpoint.observed_price - 1.0;
                    ELSE
                        expected_outcome := 'loss';
                        expected_gross := 0.0;
                        expected_profit := -1.0;
                    END IF;
                END IF;
                IF NEW.outcome<>expected_outcome OR
                   abs(NEW.gross_return_units-expected_gross)>1e-12 OR
                   abs(NEW.profit_units-expected_profit)>1e-12
                THEN
                    RAISE EXCEPTION 'checkpoint settlement calculation disagrees';
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
            CREATE TRIGGER map_decision_checkpoint_settlements_insert_guard
            BEFORE INSERT ON map_decision_checkpoint_settlements
            FOR EACH ROW EXECUTE FUNCTION validate_map_decision_checkpoint_settlement()
            """
        )
    )

    for table in (
        "map_decision_checkpoints",
        "map_decision_checkpoint_settlements",
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
    op.drop_table("map_decision_checkpoint_settlements")
    op.execute(sa.text("DROP FUNCTION validate_map_decision_checkpoint_settlement()"))
    op.drop_table("map_decision_checkpoints")
