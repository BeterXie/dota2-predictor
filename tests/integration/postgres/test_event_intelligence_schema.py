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
EVENT_TABLES = {
    "intelligence_schema_version",
    "event_registry",
    "event_candidates",
    "raw_source_artifacts",
    "raw_source_artifact_relocations",
    "raw_source_observations",
    "match_ingest_status",
    "player_role_assignments",
    "player_map_facts",
    "player_map_scores",
    "historical_rosh_lineup_scores",
    "team_map_states",
    "team_style_profiles",
    "draft_model_runs",
    "draft_predictions",
    "draft_lineage_revisions",
    "draft_lineage_changes",
    "draft_prediction_validations",
    "notification_outbox",
    "service_health",
    "ingest_scheduler_checkpoints",
    "ingest_scheduler_retry_state",
    "strict_derived_status",
}
AUDIT_TRIGGERS = {
    "raw_source_artifact_relocations_guard_insert",
    "raw_source_artifact_relocations_immutable_update",
    "raw_source_artifact_relocations_immutable_delete",
    "raw_source_artifacts_identity_immutable",
    "raw_source_artifacts_relocation_required",
    "historical_rosh_lineup_scores_immutable_update",
    "historical_rosh_lineup_scores_immutable_delete",
}


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_event_test_{uuid4().hex}"
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


def test_event_schema_preserves_constraints_views_and_audit_triggers(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert EVENT_TABLES <= set(inspector.get_table_names())
    assert {"formal_events", "formal_map_eligibility"} <= set(
        inspector.get_view_names()
    )

    with postgres_engine.connect() as connection:
        versions = set(
            connection.execute(
                text("SELECT version FROM intelligence_schema_version")
            ).scalars()
        )
        lineage = connection.execute(
            text(
                "SELECT dependency_revision, artifact_revision "
                "FROM draft_lineage_revisions WHERE singleton = 1"
            )
        ).one()
        triggers = set(
            connection.execute(
                text(
                    "SELECT trigger_name FROM information_schema.triggers "
                    "WHERE trigger_schema = current_schema()"
                )
            ).scalars()
        )

    assert versions == {1, 10}
    assert lineage == (1, 1)
    assert AUDIT_TRIGGERS <= triggers


def test_raw_artifact_relocation_requires_append_only_audit(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
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
                    :event_id, :canonical_name, 'tier_1', 1000000,
                    :starts_at, :ends_at, :league_id,
                    '[]', 'manually_audited', 'scope-v1',
                    'formal_main_event', 'approved', 'tester', :approved_at,
                    'not_required', '[]', '[]', :created_at, :updated_at
                )
                """
            ),
            {
                "event_id": "event-1",
                "canonical_name": "Event One",
                "starts_at": "2026-07-01T00:00:00Z",
                "ends_at": "2026-07-10T00:00:00Z",
                "league_id": 19_543,
                "approved_at": "2026-06-01T00:00:00Z",
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO raw_source_artifacts (
                    artifact_id, content_hash, source, artifact_use, endpoint,
                    sanitized_request_identity, storage_path,
                    uncompressed_bytes, compressed_bytes, received_at,
                    schema_fingerprint, event_id, match_id, created_at
                ) VALUES (
                    'artifact-1', :content_hash, 'opendota', 'primary',
                    '/api/matches/1', 'request-1', 'raw/old.json.gz',
                    100, 50, :received_at, 'schema-v1', 'event-1', 1, :created_at
                )
                """
            ),
            {
                "content_hash": "a" * 64,
                "received_at": "2026-07-02T00:00:00Z",
                "created_at": "2026-07-02T00:00:00Z",
            },
        )

    with pytest.raises(DBAPIError, match="artifact identity is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE raw_source_artifacts SET content_hash = :content_hash "
                    "WHERE artifact_id = 'artifact-1'"
                ),
                {"content_hash": "b" * 64},
            )

    with pytest.raises(DBAPIError, match="relocation audit is required"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE raw_source_artifacts "
                    "SET storage_path = 'raw/new.json.gz' "
                    "WHERE artifact_id = 'artifact-1'"
                )
            )

    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO raw_source_artifact_relocations (
                    relocation_id, relocation_sequence, artifact_id,
                    content_hash, source, old_storage_path, new_storage_path,
                    uncompressed_bytes, compressed_bytes, schema_fingerprint,
                    reason, actor, relocated_at
                ) VALUES (
                    :relocation_id, 1, 'artifact-1', :content_hash, 'opendota',
                    'raw/old.json.gz', 'raw/new.json.gz', 100, 50, 'schema-v1',
                    'storage migration', 'tester', :relocated_at
                )
                """
            ),
            {
                "relocation_id": "c" * 64,
                "content_hash": "a" * 64,
                "relocated_at": "2026-07-03T00:00:00Z",
            },
        )
        connection.execute(
            text(
                "UPDATE raw_source_artifacts "
                "SET storage_path = 'raw/new.json.gz' "
                "WHERE artifact_id = 'artifact-1'"
            )
        )

    with postgres_engine.connect() as connection:
        storage_path = connection.execute(
            text(
                "SELECT storage_path FROM raw_source_artifacts "
                "WHERE artifact_id = 'artifact-1'"
            )
        ).scalar_one()
        formal_event_count = connection.execute(
            text("SELECT COUNT(*) FROM formal_events WHERE event_id = 'event-1'")
        ).scalar_one()
    assert storage_path == "raw/new.json.gz"
    assert formal_event_count == 1

    with pytest.raises(DBAPIError, match="relocation audit is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE raw_source_artifact_relocations "
                    "SET reason = 'mutated' WHERE artifact_id = 'artifact-1'"
                )
            )
