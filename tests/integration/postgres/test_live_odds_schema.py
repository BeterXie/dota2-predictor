from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from database.engine import build_engine, require_database_url


ROOT = Path(__file__).resolve().parents[3]
ODDS_TABLES = {
    "live_schema_version",
    "draft_authority_revisions",
    "provider_matches",
    "raybet_matches",
    "match_links",
    "odds_snapshots",
    "odds_raw_artifacts",
    "direct_response_audit",
    "browser_events",
    "odds_response_states",
    "odds_response_state_outcomes",
    "odds_transport_observations",
    "raybet_match_odds_activity",
    "odds_response_outcomes",
}


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_odds_test_{uuid4().hex}"
    admin_engine = create_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    test_url = base_url.set(database=database_name)
    engine: Engine | None = None
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option(
            "sqlalchemy.url",
            test_url.render_as_string(hide_password=False),
        )
        command.upgrade(config, "head")
        engine = build_engine(test_url.render_as_string(hide_password=False))
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin_engine.dispose()


def test_live_odds_schema_has_authority_views_and_indexes(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert ODDS_TABLES <= set(inspector.get_table_names())
    assert {
        "odds_response_outcomes_effective",
        "trusted_odds_winner_market_authority",
    } <= set(inspector.get_view_names())

    index_names = {
        index["name"] for index in inspector.get_indexes("raybet_matches")
    }
    assert {
        "idx_raybet_matches_schedule_utc",
        "idx_raybet_matches_ended_schedule_review",
        "idx_raybet_matches_timeline",
    } <= index_names

    with postgres_engine.connect() as connection:
        authority_revision = connection.execute(
            text(
                "SELECT authority_revision FROM draft_authority_revisions "
                "WHERE singleton = 1"
            )
        ).scalar_one()
        live_versions = list(
            connection.execute(
                text("SELECT version FROM live_schema_version")
            ).scalars()
        )
        local_time = connection.execute(
            text(
                "SELECT live_text_timestamp_utc('2026-07-30 08:00:00') "
                "= '2026-07-30T00:00:00Z'::timestamptz"
            )
        ).scalar_one()

    assert authority_revision == 1
    assert live_versions == [12]
    assert local_time is True


def test_v2_transport_authority_is_immutable_and_updates_activity(
    postgres_engine: Engine,
) -> None:
    artifact_hash = "a" * 64
    response_state_hash = "b" * 64
    normalized_state_hash = "c" * 64

    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO odds_raw_artifacts (
                    artifact_hash, source, storage_path, uncompressed_bytes,
                    compressed_bytes, schema_fingerprint
                ) VALUES (
                    :artifact_hash, 'raybet', 'raw/odds.json.gz', 100, 50,
                    :schema_fingerprint
                )
                """
            ),
            {"artifact_hash": artifact_hash, "schema_fingerprint": "d" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO odds_response_states (
                    response_state_hash, raybet_match_id, normalized_state_hash,
                    normalized_state_hash_version, outcome_count
                ) VALUES (
                    :response_state_hash, 'match-1', :normalized_state_hash, 2, 2
                )
                """
            ),
            {
                "response_state_hash": response_state_hash,
                "normalized_state_hash": normalized_state_hash,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO odds_response_state_outcomes (
                    response_state_hash, odds_id, odds_group_id, price, status,
                    market_type, period, side, outcome_key, supported
                ) VALUES (
                    :response_state_hash, :odds_id, 'winner-map-1', :price,
                    'open', 'winner', 'map_1', :side, :side, 1
                )
                """
            ),
            [
                {
                    "response_state_hash": response_state_hash,
                    "odds_id": "team-one",
                    "price": 2.2,
                    "side": "team_one",
                },
                {
                    "response_state_hash": response_state_hash,
                    "odds_id": "team-two",
                    "price": 1.7,
                    "side": "team_two",
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO odds_transport_observations (
                    observation_key, source, raybet_match_id, observed_at,
                    normalized_state_hash, normalized_state_hash_version,
                    response_state_hash, response_artifact_hash, timing_status,
                    processing_status, normalized_change_count
                ) VALUES (
                    'observation-1', 'direct', 'match-1',
                    '2026-07-30T00:00:00Z', :normalized_state_hash, 2,
                    :response_state_hash, :artifact_hash, 'live', 'processing', 0
                )
                """
            ),
            {
                "normalized_state_hash": normalized_state_hash,
                "response_state_hash": response_state_hash,
                "artifact_hash": artifact_hash,
            },
        )

    with postgres_engine.connect() as connection:
        activity = connection.execute(
            text(
                "SELECT latest_odds_activity_at "
                "FROM raybet_match_odds_activity "
                "WHERE raybet_match_id = 'match-1'"
            )
        ).scalar_one()
        authority = connection.execute(
            text(
                "SELECT underdog_side, underdog_odds_id, underdog_price "
                "FROM trusted_odds_winner_market_authority "
                "WHERE observation_key = 'observation-1'"
            )
        ).one()
    assert activity == "2026-07-30T00:00:00Z"
    assert authority == ("team_one", "team-one", 2.2)

    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE odds_transport_observations "
                "SET processing_status = 'processed', normalized_change_count = 2 "
                "WHERE observation_key = 'observation-1'"
            )
        )

    with pytest.raises(DBAPIError, match="transport observation is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE odds_transport_observations "
                    "SET observed_at = '2026-07-30T00:01:00Z' "
                    "WHERE observation_key = 'observation-1'"
                )
            )

    with pytest.raises(DBAPIError, match="raw artifact is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE odds_raw_artifacts SET storage_path = 'mutated' "
                    "WHERE artifact_hash = :artifact_hash"
                ),
                {"artifact_hash": artifact_hash},
            )


def test_transport_rejects_missing_v2_authority(postgres_engine: Engine) -> None:
    with pytest.raises(DBAPIError, match="v2 response authority is required"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO odds_transport_observations (
                        observation_key, source, raybet_match_id, observed_at,
                        normalized_state_hash, normalized_state_hash_version,
                        timing_status, processing_status, normalized_change_count
                    ) VALUES (
                        'missing-authority', 'direct', 'match-2',
                        '2026-07-30T00:00:00Z', :normalized_state_hash, 2,
                        'live', 'processing', 0
                    )
                    """
                ),
                {"normalized_state_hash": "e" * 64},
            )
