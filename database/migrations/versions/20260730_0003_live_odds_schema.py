"""Create the live odds acquisition and source-audit schema.

Revision ID: 20260730_0003
Revises: 20260730_0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0003"
down_revision: str | None = "20260730_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_schema_version",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("applied_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "draft_authority_revisions",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("authority_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton = 1"),
        sa.CheckConstraint("authority_revision >= 1"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO draft_authority_revisions (
                singleton, authority_revision, updated_at
            ) VALUES (
                1, 1,
                replace(
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24_MI_SS.MS"Z"'),
                    '_', ':'
                )
            )
            """
        )
    )
    op.create_table(
        "provider_matches",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_match_id", sa.Text(), nullable=False),
        sa.Column("tournament", sa.Text()),
        sa.Column("team_one", sa.Text()),
        sa.Column("team_two", sa.Text()),
        sa.Column("scheduled_at", sa.Text()),
        sa.Column("best_of", sa.Integer()),
        sa.Column("status", sa.Text()),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "provider_match_id"),
    )
    op.create_table(
        "raybet_matches",
        sa.Column("raybet_match_id", sa.Text(), primary_key=True),
        sa.Column("tournament", sa.Text()),
        sa.Column("team_one", sa.Text()),
        sa.Column("team_two", sa.Text()),
        sa.Column("scheduled_at", sa.Text()),
        sa.Column("best_of", sa.Integer()),
        sa.Column("status", sa.Text()),
        sa.Column("live_url", sa.Text()),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    _create_timestamp_parser()
    op.create_index(
        "idx_raybet_matches_status_updated",
        "raybet_matches",
        ["status", sa.text("updated_at DESC"), sa.text("raybet_match_id DESC")],
    )
    op.create_index(
        "idx_raybet_matches_updated",
        "raybet_matches",
        [sa.text("updated_at DESC"), sa.text("raybet_match_id DESC")],
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX idx_raybet_matches_schedule_utc
            ON raybet_matches (
                live_text_timestamp_utc(scheduled_at),
                raybet_match_id
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX idx_raybet_matches_ended_schedule_review
            ON raybet_matches (
                live_text_timestamp_utc(scheduled_at),
                updated_at DESC,
                raybet_match_id DESC
            )
            WHERE lower(status) IN
                ('3', '5', 'closed', 'ended', 'finished', 'settled')
              AND scheduled_at IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX idx_raybet_matches_timeline
            ON raybet_matches (
                COALESCE(
                    live_text_timestamp_utc(scheduled_at),
                    live_text_timestamp_utc(updated_at),
                    '-infinity'::timestamptz
                ) DESC,
                raybet_match_id DESC
            )
            """
        )
    )
    op.create_table(
        "match_links",
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_match_id", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("raybet_match_id", "provider"),
    )
    op.create_table(
        "odds_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("odds_id", sa.Text(), nullable=False),
        sa.Column("odds_group_id", sa.Text()),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("price", sa.Double(), nullable=False),
        sa.Column("status", sa.Text()),
        sa.Column("market_type", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("side", sa.Text()),
        sa.Column("line", sa.Double()),
        sa.Column("outcome_key", sa.Text(), nullable=False),
        sa.Column("supported", sa.SmallInteger(), nullable=False),
        sa.Column("last_update", sa.Text()),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("raybet_match_id", "odds_id", "received_at"),
    )
    op.create_index(
        "idx_live_odds_match_time",
        "odds_snapshots",
        ["raybet_match_id", "received_at"],
    )
    _create_source_audit_tables()
    _create_normalized_response_tables()
    _create_odds_views()
    _create_odds_triggers()


def _create_timestamp_parser() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION live_text_timestamp_utc(value text)
            RETURNS timestamptz AS $$
            DECLARE
                parsed timestamptz;
            BEGIN
                IF value IS NULL OR btrim(value) = '' THEN
                    RETURN NULL;
                END IF;
                IF value ~
                    '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$'
                THEN
                    parsed := make_timestamptz(
                        substring(value, 1, 4)::integer,
                        substring(value, 6, 2)::integer,
                        substring(value, 9, 2)::integer,
                        substring(value, 12, 2)::integer,
                        substring(value, 15, 2)::integer,
                        substring(value, 18, 2)::double precision,
                        'Asia/Shanghai'
                    );
                    RETURN parsed;
                END IF;
                IF value ~
                    '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$'
                THEN
                    RETURN value::timestamptz;
                END IF;
                RETURN NULL;
            EXCEPTION WHEN OTHERS THEN
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE
            """
        )
    )


def _create_source_audit_tables() -> None:
    op.create_table(
        "odds_raw_artifacts",
        sa.Column("artifact_hash", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("uncompressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("compressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("schema_fingerprint", sa.Text(), nullable=False),
        sa.CheckConstraint("length(artifact_hash) = 64"),
        sa.CheckConstraint("source = 'raybet'"),
        sa.CheckConstraint("uncompressed_bytes >= 2"),
        sa.CheckConstraint("compressed_bytes > 0"),
        sa.CheckConstraint("length(schema_fingerprint) = 64"),
    )
    op.create_table(
        "direct_response_audit",
        sa.Column("audit_key", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("response_kind", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("claimed_raybet_match_id", sa.Text()),
        sa.Column("observed_raybet_match_id", sa.Text()),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("request_identity", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("provider_code", sa.Integer()),
        sa.Column(
            "request_metadata_json", sa.Text(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "payload_kind",
            sa.Text(),
            nullable=False,
            server_default="provider_response",
        ),
        sa.Column("sanitized", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "artifact_hash",
            sa.Text(),
            sa.ForeignKey("odds_raw_artifacts.artifact_hash"),
            nullable=False,
        ),
        sa.CheckConstraint("length(audit_key) = 64"),
        sa.CheckConstraint("source = 'direct'"),
        sa.CheckConstraint(
            "response_kind IN ('live_match_list', 'completed_match_list', "
            "'live_odds', 'completed_odds', 'final_odds')"
        ),
        sa.CheckConstraint("length(trim(endpoint)) > 0"),
        sa.CheckConstraint("length(trim(request_identity)) > 0"),
        sa.CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599"),
        sa.CheckConstraint(
            "jsonb_typeof(request_metadata_json::jsonb) = 'object'"
        ),
        sa.CheckConstraint(
            "payload_kind IN ('provider_response', 'request_failure', 'aggregate')"
        ),
        sa.CheckConstraint("sanitized IN (0, 1)"),
        sa.CheckConstraint(
            "disposition IN ('accepted', 'rejected', 'audit_only')"
        ),
    )
    op.create_index(
        "idx_direct_response_audit_kind_time",
        "direct_response_audit",
        ["response_kind", "observed_at"],
    )
    op.create_index(
        "idx_direct_response_audit_match_time",
        "direct_response_audit",
        ["claimed_raybet_match_id", "observed_at"],
    )
    op.create_table(
        "browser_events",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("capture_session_id", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.Text(), nullable=False),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("raybet_match_id", sa.Text()),
        sa.Column("game_id", sa.BigInteger()),
        sa.Column("page_origin", sa.Text(), nullable=False),
        sa.Column("page_path", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("payload_bytes", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "payload_artifact_hash",
            sa.Text(),
            sa.ForeignKey("odds_raw_artifacts.artifact_hash"),
        ),
        sa.Column(
            "payload_storage",
            sa.Text(),
            nullable=False,
            server_default="legacy_inline",
        ),
        sa.Column("capture_reason", sa.Text()),
        sa.Column("extension_version", sa.Text(), nullable=False),
        sa.Column("recognized", sa.SmallInteger(), nullable=False),
        sa.Column("processing_status", sa.Text(), nullable=False),
        sa.Column("processing_reason", sa.Text()),
        sa.CheckConstraint("payload_storage IN ('external', 'legacy_inline')"),
    )
    op.create_index(
        "idx_browser_events_match_time",
        "browser_events",
        ["raybet_match_id", "captured_at"],
    )
    op.create_index(
        "idx_browser_events_type_time",
        "browser_events",
        ["event_type", "captured_at"],
    )


def _create_normalized_response_tables() -> None:
    op.create_table(
        "odds_response_states",
        sa.Column("response_state_hash", sa.Text(), primary_key=True),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("normalized_state_hash", sa.Text(), nullable=False),
        sa.Column(
            "normalized_state_hash_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("original_legacy_normalized_state_hash", sa.Text()),
        sa.Column("outcome_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("length(response_state_hash) = 64"),
        sa.CheckConstraint("length(normalized_state_hash) = 64"),
        sa.CheckConstraint("normalized_state_hash_version IN (1, 2)"),
        sa.CheckConstraint(
            "original_legacy_normalized_state_hash IS NULL OR "
            "length(original_legacy_normalized_state_hash) = 64"
        ),
        sa.CheckConstraint("outcome_count >= 0"),
    )
    op.create_index(
        "idx_odds_response_states_normalized",
        "odds_response_states",
        ["raybet_match_id", "normalized_state_hash"],
    )
    op.create_table(
        "odds_response_state_outcomes",
        sa.Column(
            "response_state_hash",
            sa.Text(),
            sa.ForeignKey("odds_response_states.response_state_hash"),
            nullable=False,
        ),
        sa.Column("odds_id", sa.Text(), nullable=False),
        sa.Column("odds_group_id", sa.Text()),
        sa.Column("price", sa.Double(), nullable=False),
        sa.Column("status", sa.Text()),
        sa.Column("market_type", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("side", sa.Text()),
        sa.Column("line", sa.Double()),
        sa.Column("outcome_key", sa.Text(), nullable=False),
        sa.Column("supported", sa.SmallInteger(), nullable=False),
        sa.Column("last_update", sa.Text()),
        sa.PrimaryKeyConstraint("response_state_hash", "odds_id"),
        sa.CheckConstraint("supported IN (0, 1)"),
    )
    op.create_table(
        "odds_transport_observations",
        sa.Column("observation_key", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "source_event_id",
            sa.Text(),
            sa.ForeignKey("browser_events.event_id"),
        ),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("normalized_state_hash", sa.Text(), nullable=False),
        sa.Column(
            "normalized_state_hash_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("original_legacy_normalized_state_hash", sa.Text()),
        sa.Column(
            "response_state_hash",
            sa.Text(),
            sa.ForeignKey("odds_response_states.response_state_hash"),
        ),
        sa.Column(
            "response_artifact_hash",
            sa.Text(),
            sa.ForeignKey("odds_raw_artifacts.artifact_hash"),
        ),
        sa.Column("timing_status", sa.Text(), nullable=False),
        sa.Column("processing_status", sa.Text(), nullable=False),
        sa.Column("normalized_change_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("source IN ('direct', 'browser')"),
        sa.CheckConstraint("normalized_state_hash_version IN (1, 2)"),
        sa.CheckConstraint(
            "original_legacy_normalized_state_hash IS NULL OR "
            "length(original_legacy_normalized_state_hash) = 64"
        ),
    )
    op.create_index(
        "idx_odds_transport_match_time",
        "odds_transport_observations",
        ["raybet_match_id", "observed_at"],
    )
    op.create_index(
        "idx_odds_transport_hash_time",
        "odds_transport_observations",
        ["normalized_state_hash", "observed_at"],
    )
    op.create_table(
        "raybet_match_odds_activity",
        sa.Column("raybet_match_id", sa.Text(), primary_key=True),
        sa.Column("latest_odds_activity_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "idx_raybet_match_odds_activity_time",
        "raybet_match_odds_activity",
        [sa.text("latest_odds_activity_at DESC"), sa.text("raybet_match_id DESC")],
    )
    op.create_table(
        "odds_response_outcomes",
        sa.Column(
            "observation_key",
            sa.Text(),
            sa.ForeignKey("odds_transport_observations.observation_key"),
            nullable=False,
        ),
        sa.Column("raybet_match_id", sa.Text(), nullable=False),
        sa.Column("odds_id", sa.Text(), nullable=False),
        sa.Column("odds_group_id", sa.Text()),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("price", sa.Double(), nullable=False),
        sa.Column("status", sa.Text()),
        sa.Column("market_type", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("side", sa.Text()),
        sa.Column("line", sa.Double()),
        sa.Column("outcome_key", sa.Text(), nullable=False),
        sa.Column("supported", sa.SmallInteger(), nullable=False),
        sa.Column("last_update", sa.Text()),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("observation_key", "odds_id"),
    )
    op.create_index(
        "idx_odds_response_match_outcome",
        "odds_response_outcomes",
        ["raybet_match_id", "odds_id", "observation_key"],
    )


def _create_odds_views() -> None:
    op.execute(
        sa.text(
            """
            CREATE VIEW odds_response_outcomes_effective AS
            SELECT transport.observation_key,
                   transport.raybet_match_id,
                   outcome.odds_id,
                   outcome.odds_group_id,
                   transport.observed_at AS received_at,
                   outcome.price,
                   outcome.status,
                   outcome.market_type,
                   outcome.period,
                   outcome.side,
                   outcome.line,
                   outcome.outcome_key,
                   outcome.supported,
                   outcome.last_update,
                   NULL::text AS raw_json,
                   transport.response_state_hash,
                   transport.response_artifact_hash,
                   'v2'::text AS storage_version
            FROM odds_transport_observations AS transport
            JOIN odds_response_states AS state
              ON state.response_state_hash = transport.response_state_hash
             AND state.raybet_match_id = transport.raybet_match_id
             AND state.normalized_state_hash = transport.normalized_state_hash
            JOIN odds_response_state_outcomes AS outcome
              ON outcome.response_state_hash = state.response_state_hash
            UNION ALL
            SELECT legacy.observation_key,
                   legacy.raybet_match_id,
                   legacy.odds_id,
                   legacy.odds_group_id,
                   legacy.received_at,
                   legacy.price,
                   legacy.status,
                   legacy.market_type,
                   legacy.period,
                   legacy.side,
                   legacy.line,
                   legacy.outcome_key,
                   legacy.supported,
                   legacy.last_update,
                   legacy.raw_json,
                   NULL::text,
                   NULL::text,
                   'legacy'::text
            FROM odds_response_outcomes AS legacy
            JOIN odds_transport_observations AS transport
              ON transport.observation_key = legacy.observation_key
            WHERE transport.response_state_hash IS NULL
              AND transport.response_artifact_hash IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE VIEW trusted_odds_winner_market_authority AS
            WITH complete_group AS (
                SELECT outcome.observation_key,
                       outcome.raybet_match_id,
                       outcome.period,
                       outcome.odds_group_id,
                       outcome.response_state_hash,
                       outcome.response_artifact_hash,
                       MAX(outcome.odds_id) FILTER (
                           WHERE outcome.side = 'team_one'
                       ) AS team_one_odds_id,
                       MAX(outcome.odds_id) FILTER (
                           WHERE outcome.side = 'team_two'
                       ) AS team_two_odds_id,
                       MAX(outcome.price) FILTER (
                           WHERE outcome.side = 'team_one'
                       ) AS team_one_price,
                       MAX(outcome.price) FILTER (
                           WHERE outcome.side = 'team_two'
                       ) AS team_two_price
                FROM odds_response_outcomes_effective AS outcome
                WHERE outcome.storage_version = 'v2'
                  AND outcome.market_type = 'winner'
                  AND outcome.odds_group_id IS NOT NULL
                  AND trim(outcome.odds_group_id) != ''
                  AND outcome.response_state_hash IS NOT NULL
                  AND length(outcome.response_state_hash) = 64
                  AND outcome.response_artifact_hash IS NOT NULL
                  AND length(outcome.response_artifact_hash) = 64
                GROUP BY outcome.observation_key,
                         outcome.raybet_match_id,
                         outcome.period,
                         outcome.odds_group_id,
                         outcome.response_state_hash,
                         outcome.response_artifact_hash
                HAVING COUNT(*) = 2
                   AND COUNT(DISTINCT outcome.odds_id) = 2
                   AND COUNT(*) FILTER (WHERE outcome.side = 'team_one') = 1
                   AND COUNT(*) FILTER (WHERE outcome.side = 'team_two') = 1
                   AND COUNT(*) FILTER (
                       WHERE outcome.outcome_key = outcome.side
                   ) = 2
                   AND COUNT(*) FILTER (WHERE outcome.supported = 1) = 2
                   AND COUNT(*) FILTER (
                       WHERE lower(trim(outcome.status::text))
                             IN ('1', 'open', 'active', 'running')
                   ) = 2
                   AND COUNT(*) FILTER (WHERE outcome.price > 1.0) = 2
            )
            SELECT market.observation_key,
                   market.raybet_match_id,
                   market.period,
                   market.odds_group_id,
                   market.response_state_hash,
                   market.response_artifact_hash,
                   CASE WHEN market.team_one_price > market.team_two_price
                        THEN 'team_one' ELSE 'team_two' END AS underdog_side,
                   CASE WHEN market.team_one_price > market.team_two_price
                        THEN market.team_one_odds_id
                        ELSE market.team_two_odds_id END AS underdog_odds_id,
                   CASE WHEN market.team_one_price > market.team_two_price
                        THEN market.team_one_price
                        ELSE market.team_two_price END AS underdog_price,
                   CASE WHEN market.team_one_price > market.team_two_price
                        THEN (1.0 / market.team_one_price) /
                             ((1.0 / market.team_one_price) +
                              (1.0 / market.team_two_price))
                        ELSE (1.0 / market.team_two_price) /
                             ((1.0 / market.team_one_price) +
                              (1.0 / market.team_two_price))
                   END AS underdog_probability
            FROM complete_group AS market
            WHERE market.team_one_price != market.team_two_price
              AND (
                  SELECT COUNT(*)
                  FROM complete_group AS candidate
                  WHERE candidate.observation_key = market.observation_key
                    AND candidate.raybet_match_id = market.raybet_match_id
                    AND candidate.period = market.period
              ) = 1
            """
        )
    )


def _create_odds_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_immutable_live_row()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '%', TG_ARGV[0];
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    _create_reject_triggers(
        "odds_raw_artifacts",
        "odds_raw_artifacts_immutable",
        "odds raw artifact is immutable",
    )
    _create_reject_triggers(
        "direct_response_audit",
        "direct_response_audit_immutable",
        "direct response audit is immutable",
    )
    _create_reject_triggers(
        "odds_response_states",
        "odds_response_states_immutable",
        "odds response state is immutable",
    )
    _create_reject_triggers(
        "odds_response_state_outcomes",
        "odds_response_state_outcomes_immutable",
        "odds response state outcome is immutable",
    )
    _create_reject_triggers(
        "odds_response_outcomes",
        "odds_response_outcomes_immutable",
        "odds response outcome is immutable",
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER odds_response_outcomes_legacy_insert_disabled
            BEFORE INSERT ON odds_response_outcomes
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                'legacy odds response outcome writes are disabled'
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_browser_event_payload()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.event_id IS DISTINCT FROM OLD.event_id
                    OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
                    OR NEW.capture_session_id IS DISTINCT FROM OLD.capture_session_id
                    OR NEW.captured_at IS DISTINCT FROM OLD.captured_at
                    OR NEW.received_at IS DISTINCT FROM OLD.received_at
                    OR NEW.transport IS DISTINCT FROM OLD.transport
                    OR NEW.event_type IS DISTINCT FROM OLD.event_type
                    OR NEW.raybet_match_id IS DISTINCT FROM OLD.raybet_match_id
                    OR NEW.game_id IS DISTINCT FROM OLD.game_id
                    OR NEW.page_origin IS DISTINCT FROM OLD.page_origin
                    OR NEW.page_path IS DISTINCT FROM OLD.page_path
                    OR NEW.source_path IS DISTINCT FROM OLD.source_path
                    OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
                    OR NEW.payload_bytes IS DISTINCT FROM OLD.payload_bytes
                    OR NEW.payload_json IS DISTINCT FROM OLD.payload_json
                    OR NEW.payload_artifact_hash IS DISTINCT FROM
                       OLD.payload_artifact_hash
                    OR NEW.payload_storage IS DISTINCT FROM OLD.payload_storage
                    OR NEW.capture_reason IS DISTINCT FROM OLD.capture_reason
                    OR NEW.extension_version IS DISTINCT FROM OLD.extension_version
                    OR NEW.recognized IS DISTINCT FROM OLD.recognized
                THEN
                    RAISE EXCEPTION 'browser event payload is immutable';
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
            CREATE TRIGGER browser_events_immutable
            BEFORE UPDATE ON browser_events
            FOR EACH ROW EXECUTE FUNCTION guard_browser_event_payload()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION require_browser_event_external_payload()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.payload_storage != 'external'
                    OR NEW.payload_artifact_hash IS NULL
                    OR NEW.payload_json != '{}'
                THEN
                    RAISE EXCEPTION
                        'browser event external payload authority is required';
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
            CREATE TRIGGER browser_events_require_external_payload
            BEFORE INSERT ON browser_events
            FOR EACH ROW
            EXECUTE FUNCTION require_browser_event_external_payload()
            """
        )
    )
    _create_activity_triggers()
    _create_transport_authority_triggers()


def _create_activity_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION record_transport_odds_activity()
            RETURNS trigger AS $$
            BEGIN
                IF live_text_timestamp_utc(NEW.observed_at) IS NOT NULL THEN
                    INSERT INTO raybet_match_odds_activity (
                        raybet_match_id, latest_odds_activity_at
                    ) VALUES (
                        NEW.raybet_match_id, NEW.observed_at
                    )
                    ON CONFLICT (raybet_match_id) DO UPDATE SET
                        latest_odds_activity_at = EXCLUDED.latest_odds_activity_at
                    WHERE live_text_timestamp_utc(
                              EXCLUDED.latest_odds_activity_at
                          ) > live_text_timestamp_utc(
                              raybet_match_odds_activity.latest_odds_activity_at
                          );
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
            CREATE TRIGGER raybet_match_activity_from_transport
            AFTER INSERT ON odds_transport_observations
            FOR EACH ROW EXECUTE FUNCTION record_transport_odds_activity()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION record_snapshot_odds_activity()
            RETURNS trigger AS $$
            BEGIN
                IF live_text_timestamp_utc(NEW.received_at) IS NOT NULL THEN
                    INSERT INTO raybet_match_odds_activity (
                        raybet_match_id, latest_odds_activity_at
                    ) VALUES (
                        NEW.raybet_match_id, NEW.received_at
                    )
                    ON CONFLICT (raybet_match_id) DO UPDATE SET
                        latest_odds_activity_at = EXCLUDED.latest_odds_activity_at
                    WHERE live_text_timestamp_utc(
                              EXCLUDED.latest_odds_activity_at
                          ) > live_text_timestamp_utc(
                              raybet_match_odds_activity.latest_odds_activity_at
                          );
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
            CREATE TRIGGER raybet_match_activity_from_snapshot
            AFTER INSERT ON odds_snapshots
            FOR EACH ROW EXECUTE FUNCTION record_snapshot_odds_activity()
            """
        )
    )


def _create_transport_authority_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION guard_odds_transport_observation_update()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.observation_key IS DISTINCT FROM NEW.observation_key
                    OR OLD.source IS DISTINCT FROM NEW.source
                    OR OLD.source_event_id IS DISTINCT FROM NEW.source_event_id
                    OR OLD.raybet_match_id IS DISTINCT FROM NEW.raybet_match_id
                    OR OLD.observed_at IS DISTINCT FROM NEW.observed_at
                    OR OLD.normalized_state_hash IS DISTINCT FROM
                       NEW.normalized_state_hash
                    OR OLD.normalized_state_hash_version IS DISTINCT FROM
                       NEW.normalized_state_hash_version
                    OR OLD.original_legacy_normalized_state_hash IS DISTINCT FROM
                       NEW.original_legacy_normalized_state_hash
                    OR OLD.response_state_hash IS DISTINCT FROM NEW.response_state_hash
                    OR OLD.response_artifact_hash IS DISTINCT FROM
                       NEW.response_artifact_hash
                    OR OLD.timing_status IS DISTINCT FROM NEW.timing_status
                    OR NOT (
                        (OLD.processing_status IS NOT DISTINCT FROM
                         NEW.processing_status
                         AND OLD.normalized_change_count IS NOT DISTINCT FROM
                             NEW.normalized_change_count)
                        OR (
                            OLD.processing_status = 'processing'
                            AND NEW.processing_status = 'processed'
                            AND OLD.normalized_change_count = 0
                            AND NEW.normalized_change_count >= 0
                        )
                    )
                THEN
                    RAISE EXCEPTION 'odds transport observation is immutable';
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
            CREATE TRIGGER odds_transport_observations_guard_update
            BEFORE UPDATE ON odds_transport_observations
            FOR EACH ROW
            EXECUTE FUNCTION guard_odds_transport_observation_update()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION require_odds_transport_v2_state()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.response_state_hash IS NULL
                    OR NEW.response_artifact_hash IS NULL
                    OR NEW.normalized_state_hash_version != 2
                    OR NEW.original_legacy_normalized_state_hash IS NOT NULL
                    OR NOT EXISTS (
                        SELECT 1
                        FROM odds_response_states AS state
                        WHERE state.response_state_hash = NEW.response_state_hash
                          AND state.raybet_match_id = NEW.raybet_match_id
                          AND state.normalized_state_hash = NEW.normalized_state_hash
                          AND state.normalized_state_hash_version = 2
                          AND state.original_legacy_normalized_state_hash IS NULL
                    )
                    OR NOT EXISTS (
                        SELECT 1
                        FROM odds_raw_artifacts AS artifact
                        WHERE artifact.artifact_hash = NEW.response_artifact_hash
                    )
                THEN
                    RAISE EXCEPTION
                        'odds transport v2 response authority is required';
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
            CREATE TRIGGER odds_transport_observations_require_v2_state
            BEFORE INSERT ON odds_transport_observations
            FOR EACH ROW EXECUTE FUNCTION require_odds_transport_v2_state()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER odds_transport_observations_immutable_delete
            BEFORE DELETE ON odds_transport_observations
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                'odds transport observation is immutable'
            )
            """
        )
    )


def _create_reject_triggers(
    table: str,
    prefix: str,
    message: str,
) -> None:
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {prefix}_{operation.lower()}
                BEFORE {operation} ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_immutable_live_row(
                    '{message}'
                )
                """
            )
        )


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW trusted_odds_winner_market_authority"))
    op.execute(sa.text("DROP VIEW odds_response_outcomes_effective"))

    op.drop_table("odds_response_outcomes")
    op.drop_table("raybet_match_odds_activity")
    op.drop_table("odds_transport_observations")
    op.drop_table("odds_response_state_outcomes")
    op.drop_table("odds_response_states")
    op.drop_table("browser_events")
    op.drop_table("direct_response_audit")
    op.drop_table("odds_raw_artifacts")
    op.drop_table("odds_snapshots")
    op.drop_table("match_links")
    op.drop_table("raybet_matches")
    op.drop_table("provider_matches")
    op.drop_table("draft_authority_revisions")
    op.drop_table("live_schema_version")

    op.execute(sa.text("DROP FUNCTION require_odds_transport_v2_state()"))
    op.execute(sa.text("DROP FUNCTION guard_odds_transport_observation_update()"))
    op.execute(sa.text("DROP FUNCTION record_snapshot_odds_activity()"))
    op.execute(sa.text("DROP FUNCTION record_transport_odds_activity()"))
    op.execute(sa.text("DROP FUNCTION require_browser_event_external_payload()"))
    op.execute(sa.text("DROP FUNCTION guard_browser_event_payload()"))
    op.execute(sa.text("DROP FUNCTION reject_immutable_live_row()"))
    op.execute(sa.text("DROP FUNCTION live_text_timestamp_utc(text)"))
