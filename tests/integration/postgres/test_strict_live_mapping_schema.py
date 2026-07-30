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
from sqlalchemy.exc import DBAPIError, IntegrityError

from database.engine import build_engine, require_database_url


ROOT = Path(__file__).resolve().parents[3]
STRICT_TABLES = {
    "strict_live_map_mappings",
    "strict_live_map_mapping_audit",
    "strict_live_automatic_evidence_approvals",
    "strict_live_map_mapping_invalidations",
    "strict_live_map_mapping_supersessions",
    "strict_live_mapping_impacts",
}


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_mapping_test_{uuid4().hex}"
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


def _seed_event(connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO event_registry (
                event_id, canonical_name, tier, prize_pool_usd,
                main_event_start_at, main_event_end_at, opendota_league_id,
                official_evidence_urls_json, evidence_status,
                scope_policy_version, scope, approval_status,
                approved_by, approved_at, reconciliation_status,
                included_stages_json, excluded_categories_json,
                created_at, updated_at
            ) VALUES (
                'event-1', 'Event One', 'tier_1', 1000000,
                '2026-07-01T00:00:00Z', '2026-07-10T00:00:00Z', 19543,
                '[]', 'manually_audited', 'scope-v1',
                'formal_main_event', 'approved', 'tester',
                '2026-06-01T00:00:00Z', 'not_required', '[]', '[]',
                '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z'
            )
            """
        )
    )


def _insert_mapping(connection) -> int:
    return connection.execute(
        text(
            """
            INSERT INTO strict_live_map_mappings (
                raybet_match_id, map_number, event_id, team_one_id, team_two_id,
                canonical_team_one_id, canonical_team_one_name,
                canonical_team_two_id, canonical_team_two_name,
                canonical_identity_json, canonical_identity_hash,
                crosswalk_evidence_json, crosswalk_evidence_hash, stage_scope,
                scheduled_at_utc, raybet_best_of, raybet_identity_json,
                raybet_identity_hash, raybet_metadata_updated_at, source,
                evidence_json, evidence_hash, mapping_version, accepted_by,
                accepted_at, recorded_at, created_at
            ) VALUES (
                'match-1', 1, 'event-1', 101, 202, 1001, 'Team One',
                2002, 'Team Two', '{}', :canonical_hash, '{}', :crosswalk_hash,
                'main_event', '2026-07-02T00:00:00Z', 3, '{}', :raybet_hash,
                '2026-07-01T12:00:00Z', 'manual', '{}', :evidence_hash,
                'strict-live-map-v3', 'tester', '2026-07-01T12:00:00Z',
                '2026-07-01T12:00:00Z', '2026-07-01T12:00:00Z'
            )
            RETURNING mapping_id
            """
        ),
        {
            "canonical_hash": "a" * 64,
            "crosswalk_hash": "b" * 64,
            "raybet_hash": "c" * 64,
            "evidence_hash": "d" * 64,
        },
    ).scalar_one()


def test_mapping_schema_preserves_cyclic_authority_foreign_keys(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert STRICT_TABLES <= set(inspector.get_table_names())

    mapping_targets = {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("strict_live_map_mappings")
    }
    approval_targets = {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys(
            "strict_live_automatic_evidence_approvals"
        )
    }
    assert "strict_live_automatic_evidence_approvals" in mapping_targets
    assert "strict_live_map_mappings" in approval_targets


def test_accepted_mapping_and_invalidation_are_append_only(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        _seed_event(connection)
        mapping_id = _insert_mapping(connection)
        invalidation_id = connection.execute(
            text(
                """
                INSERT INTO strict_live_map_mapping_invalidations (
                    mapping_id, reason, invalidated_by, invalidated_at, recorded_at
                ) VALUES (
                    :mapping_id, 'source correction', 'tester',
                    '2026-07-02T00:00:00Z', '2026-07-02T00:00:00Z'
                )
                RETURNING invalidation_id
                """
            ),
            {"mapping_id": mapping_id},
        ).scalar_one()

    with pytest.raises(DBAPIError, match="accepted strict live mappings are immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE strict_live_map_mappings SET source = 'mutated' "
                    "WHERE mapping_id = :mapping_id"
                ),
                {"mapping_id": mapping_id},
            )

    with pytest.raises(DBAPIError, match="invalidations are immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE strict_live_map_mapping_invalidations "
                    "SET reason = 'mutated' "
                    "WHERE invalidation_id = :invalidation_id"
                ),
                {"invalidation_id": invalidation_id},
            )


def test_candidate_mapping_audit_cannot_be_promoted_to_acceptance(
    postgres_engine: Engine,
) -> None:
    with pytest.raises(IntegrityError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO strict_live_map_mapping_audit (
                        raybet_match_id, map_number, match_method, decision,
                        reason, source, evidence_json, evidence_hash,
                        mapping_version, observed_at, recorded_at
                    ) VALUES (
                        'match-2', 1, 'candidate', 'accepted', 'not exact',
                        'test', '{}', :evidence_hash, 'strict-live-map-v3',
                        '2026-07-02T00:00:00Z', '2026-07-02T00:00:00Z'
                    )
                    """
                ),
                {"evidence_hash": "e" * 64},
            )
