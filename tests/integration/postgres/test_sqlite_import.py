from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from database.engine import build_engine, require_database_url
from scripts.migrate_sqlite_to_postgres import _import_order, migrate_sqlite_to_postgres


@pytest.fixture()
def postgres_database_url() -> Iterator[str]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_import_test_{uuid4().hex}"
    admin_engine = create_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    test_url = base_url.set(database=database_name)
    try:
        yield test_url.render_as_string(hide_password=False)
    finally:
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


def _create_source(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE heroes (
                hero_id INTEGER PRIMARY KEY,
                localized_name TEXT,
                hero_key TEXT
            );
            CREATE TABLE matches (
                match_id INTEGER PRIMARY KEY,
                radiant_win BOOLEAN
            );
            CREATE TABLE settlements (
                order_key TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                return_units REAL NOT NULL,
                settled_at TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                review_required INTEGER NOT NULL
            );
            CREATE TABLE draft_lineage_revisions (
                singleton INTEGER PRIMARY KEY,
                dependency_revision INTEGER NOT NULL,
                artifact_revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE draft_lineage_changes (
                dependency_revision INTEGER PRIMARY KEY,
                affected_from_unix INTEGER,
                source_relation TEXT NOT NULL,
                operation TEXT NOT NULL,
                changed_at TEXT NOT NULL
            );
            CREATE TABLE vision_draft_anchors (
                raybet_match_id TEXT NOT NULL,
                map_number INTEGER NOT NULL,
                draft_hash TEXT NOT NULL,
                radiant_hero_ids TEXT NOT NULL,
                dire_hero_ids TEXT NOT NULL,
                radiant_team_side TEXT,
                team_side_anchored_at TEXT,
                team_side_source_frame_ref TEXT,
                anchored_at TEXT NOT NULL,
                source_frame_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                conflict_at TEXT,
                PRIMARY KEY (raybet_match_id, map_number)
            );
            CREATE TABLE browser_events (
                event_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                capture_session_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                transport TEXT NOT NULL,
                event_type TEXT NOT NULL,
                raybet_match_id TEXT,
                game_id INTEGER,
                page_origin TEXT NOT NULL,
                page_path TEXT NOT NULL,
                source_path TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_artifact_hash TEXT,
                payload_storage TEXT NOT NULL,
                capture_reason TEXT,
                extension_version TEXT NOT NULL,
                recognized INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                processing_reason TEXT
            );
            CREATE TABLE odds_raw_artifacts (
                artifact_hash TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                uncompressed_bytes INTEGER NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                schema_fingerprint TEXT NOT NULL
            );
            CREATE TABLE odds_response_states (
                response_state_hash TEXT PRIMARY KEY,
                raybet_match_id TEXT NOT NULL,
                normalized_state_hash TEXT NOT NULL,
                normalized_state_hash_version INTEGER NOT NULL,
                original_legacy_normalized_state_hash TEXT,
                outcome_count INTEGER NOT NULL
            );
            CREATE TABLE odds_transport_observations (
                observation_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_event_id TEXT,
                raybet_match_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                normalized_state_hash TEXT NOT NULL,
                normalized_state_hash_version INTEGER NOT NULL,
                original_legacy_normalized_state_hash TEXT,
                response_state_hash TEXT,
                response_artifact_hash TEXT,
                timing_status TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                normalized_change_count INTEGER NOT NULL
            );
            CREATE TABLE raw_source_artifacts (
                artifact_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                source TEXT NOT NULL,
                artifact_use TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                sanitized_request_identity TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                uncompressed_bytes INTEGER NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                source_at TEXT,
                received_at TEXT NOT NULL,
                first_usable_at TEXT NOT NULL,
                schema_fingerprint TEXT NOT NULL,
                event_id TEXT,
                match_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE raw_source_artifact_relocations (
                relocation_id TEXT PRIMARY KEY,
                relocation_sequence INTEGER NOT NULL,
                artifact_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source TEXT NOT NULL,
                old_storage_path TEXT NOT NULL,
                new_storage_path TEXT NOT NULL,
                uncompressed_bytes INTEGER NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                schema_fingerprint TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                relocated_at TEXT NOT NULL
            );
            INSERT INTO heroes VALUES (1, 'Anti-Mage', 'antimage');
            INSERT INTO matches VALUES (8904419709, 1);
            INSERT INTO settlements VALUES (
                'manual-review-order', 'win', 2.2,
                '2026-07-30T01:00:00Z', 'manual-review-evidence', 1
            );
            INSERT INTO draft_lineage_revisions VALUES (
                1, 2, 1, '2026-07-30T01:02:00Z'
            );
            INSERT INTO draft_lineage_changes VALUES (
                1, NULL, '__tracking__', 'INITIALIZE', '2026-07-30T01:00:00Z'
            );
            INSERT INTO draft_lineage_changes VALUES (
                2, 1785000000, 'matches', 'INSERT', '2026-07-30T01:02:00Z'
            );
            INSERT INTO vision_draft_anchors VALUES (
                'legacy-match', 1,
                'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                '[1,2,3,4,5]',
                '[6,7,8,9,10]', NULL, NULL, NULL,
                '2026-07-30T01:03:00Z', 'legacy-frame-path', 'conflict',
                '2026-07-30T01:04:00Z'
            );
            INSERT INTO browser_events VALUES (
                'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                1, 'ffffffffffffffffffffffffffffffff',
                '2026-07-30T01:05:00Z', '2026-07-30T01:05:01Z',
                'fetch', 'match_list', 'legacy-match', 151,
                'https://example.test', '/', '/v2/match',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                2, '{}', NULL, 'legacy_inline', NULL, '0.1.0', 1,
                'audit_only', 'legacy_import'
            );
            INSERT INTO odds_raw_artifacts VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'raybet', 'legacy-payload.json.gz', 2, 1,
                'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
            );
            INSERT INTO odds_response_states VALUES (
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'legacy-match',
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                2,
                'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                0
            );
            INSERT INTO odds_transport_observations VALUES (
                '1111111111111111111111111111111111111111111111111111111111111111',
                'direct', NULL, 'legacy-match', '2026-07-30T01:06:00Z',
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                2,
                'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'on_time', 'processed', 0
            );
            INSERT INTO raw_source_artifacts VALUES (
                'source-artifact-1',
                'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                'opendota', 'primary', '/api/matches/1', '/api/matches/1',
                'new-source-path', 100, 50, NULL,
                '2026-07-30T01:07:00Z', '2026-07-30T01:07:00Z',
                'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                NULL, 1, '2026-07-30T01:07:00Z'
            );
            INSERT INTO raw_source_artifact_relocations VALUES (
                '9999999999999999999999999999999999999999999999999999999999999999',
                1, 'source-artifact-1',
                'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                'opendota', 'old-source-path', 'new-source-path', 100, 50,
                'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                'historical import', 'migration-test', '2026-07-30T01:08:00Z'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_import_order_respects_trigger_only_draft_authority_dependencies() -> None:
    class Inspector:
        @staticmethod
        def get_foreign_keys(_table: str) -> list[dict[str, object]]:
            return []

    order = _import_order(
        Inspector(),
        {
            "draft_calibration_artifacts",
            "draft_deployment_bundles",
            "draft_model_artifacts",
        },
    )

    assert order.index("draft_model_artifacts") < order.index(
        "draft_deployment_bundles"
    )
    assert order.index("draft_calibration_artifacts") < order.index(
        "draft_deployment_bundles"
    )


def test_import_order_respects_external_browser_payload_authority() -> None:
    class Inspector:
        @staticmethod
        def get_foreign_keys(_table: str) -> list[dict[str, object]]:
            return []

    order = _import_order(
        Inspector(), {"browser_events", "odds_raw_artifacts"}
    )

    assert order == ["odds_raw_artifacts", "browser_events"]


def test_sqlite_import_is_read_only_atomic_and_verified(
    tmp_path: Path,
    postgres_database_url: str,
) -> None:
    source = tmp_path / "legacy.sqlite"
    _create_source(source)
    source_before = source.read_bytes()

    dry_run = migrate_sqlite_to_postgres(
        source,
        postgres_database_url,
        dry_run=True,
    )
    assert dry_run.dry_run is True
    assert dry_run.target_revision is None
    assert dry_run.row_counts == {
        "browser_events": 1,
        "draft_lineage_changes": 2,
        "draft_lineage_revisions": 1,
        "heroes": 1,
        "matches": 1,
        "odds_raw_artifacts": 1,
        "odds_response_states": 1,
        "odds_transport_observations": 1,
        "raw_source_artifact_relocations": 1,
        "raw_source_artifacts": 1,
        "settlements": 1,
        "vision_draft_anchors": 1,
    }
    assert source.read_bytes() == source_before

    report = migrate_sqlite_to_postgres(source, postgres_database_url)

    assert report.dry_run is False
    assert report.target_revision == "20260805_0026"
    assert report.row_counts == {
        "browser_events": 1,
        "draft_lineage_changes": 2,
        "draft_lineage_revisions": 1,
        "heroes": 1,
        "matches": 1,
        "odds_raw_artifacts": 1,
        "odds_response_states": 1,
        "odds_transport_observations": 1,
        "raw_source_artifact_relocations": 1,
        "raw_source_artifacts": 1,
        "settlements": 1,
        "vision_draft_anchors": 1,
    }
    assert report.primary_key_ranges == {
        "draft_lineage_changes": (1, 2),
        "draft_lineage_revisions": (1, 1),
        "heroes": (1, 1),
        "matches": (8904419709, 8904419709),
    }
    assert report.business_counts == {
        "strategy_decisions": 0,
        "shadow_orders": 0,
        "settlements": 1,
        "active_alerts": 0,
    }
    assert "settlements" in report.critical_digests
    assert source.read_bytes() == source_before

    engine = build_engine(postgres_database_url)
    try:
        with engine.connect() as connection:
            hero = connection.execute(
                text(
                    "SELECT localized_name, hero_key FROM heroes "
                    "WHERE hero_id = 1"
                )
            ).one()
            match = connection.execute(
                text(
                    "SELECT radiant_win FROM matches "
                    "WHERE match_id = 8904419709"
                )
            ).scalar_one()
            settlement = connection.execute(
                text(
                    "SELECT order_key, review_required FROM settlements "
                    "WHERE order_key = 'manual-review-order'"
                )
            ).one()
            lineage_rows = connection.execute(
                text(
                    "SELECT dependency_revision, changed_at "
                    "FROM draft_lineage_changes ORDER BY dependency_revision"
                )
            ).all()
            lineage_revision = connection.execute(
                text(
                    "SELECT dependency_revision FROM draft_lineage_revisions "
                    "WHERE singleton = 1"
                )
            ).scalar_one()
            prematch_lineage_revision = connection.execute(
                text(
                    "SELECT dependency_revision, artifact_revision "
                    "FROM prematch_lineage_revisions WHERE singleton = 1"
                )
            ).one()
            prematch_lineage_changes = connection.execute(
                text(
                    "SELECT dependency_revision, source_relation, operation "
                    "FROM prematch_lineage_changes ORDER BY dependency_revision"
                )
            ).all()
            anchor = connection.execute(
                text(
                    "SELECT status, conflict_at, source_frame_ref "
                    "FROM vision_draft_anchors "
                    "WHERE raybet_match_id = 'legacy-match' AND map_number = 1"
                )
            ).one()
            browser_event = connection.execute(
                text(
                    "SELECT payload_storage, payload_json FROM browser_events "
                    "WHERE event_id = "
                    "'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'"
                )
            ).one()
            legacy_transport = connection.execute(
                text(
                    "SELECT normalized_state_hash_version, "
                    "original_legacy_normalized_state_hash "
                    "FROM odds_transport_observations "
                    "WHERE observation_key = "
                    "'1111111111111111111111111111111111111111111111111111111111111111'"
                )
            ).one()
            raw_source_path = connection.execute(
                text(
                    "SELECT storage_path FROM raw_source_artifacts "
                    "WHERE artifact_id = 'source-artifact-1'"
                )
            ).scalar_one()
        assert hero == ("Anti-Mage", "antimage")
        assert match is True
        assert settlement == ("manual-review-order", 1)
        assert lineage_rows == [
            (1, "2026-07-30T01:00:00Z"),
            (2, "2026-07-30T01:02:00Z"),
        ]
        assert lineage_revision == 2
        assert prematch_lineage_revision == (1, 1)
        assert prematch_lineage_changes == [(1, "__tracking__", "INITIALIZE")]
        assert anchor == (
            "conflict",
            "2026-07-30T01:04:00Z",
            "legacy-frame-path",
        )
        assert browser_event == ("legacy_inline", "{}")
        assert legacy_transport == (
            2,
            "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        )
        assert raw_source_path == "new-source-path"
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO heroes (hero_id, localized_name, hero_key) "
                    "VALUES (2, 'Axe', 'axe')"
                )
            )
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT dependency_revision FROM prematch_lineage_revisions "
                    "WHERE singleton = 1"
                )
            ).scalar_one() == 2
            assert connection.execute(
                text(
                    "SELECT source_relation FROM prematch_lineage_changes "
                    "WHERE dependency_revision = 2"
                )
            ).scalar_one() == "heroes"
        with pytest.raises(DBAPIError, match="external payload authority"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO browser_events "
                        "(event_id, schema_version, capture_session_id, captured_at, "
                        "received_at, transport, event_type, page_origin, page_path, "
                        "source_path, payload_hash, payload_bytes, payload_json, "
                        "payload_storage, extension_version, recognized, "
                        "processing_status) VALUES "
                        "('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                        "1, 'cccccccccccccccccccccccccccccccc', "
                        "'2026-07-30T02:00:00Z', '2026-07-30T02:00:01Z', "
                        "'fetch', 'match_list', 'https://example.test', '/', "
                        "'/v2/match', "
                        "'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', "
                        "2, '{}', 'legacy_inline', '0.1.0', 1, 'audit_only')"
                    )
                )
        with pytest.raises(DBAPIError, match="v2 response authority"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO odds_transport_observations "
                        "(observation_key, source, raybet_match_id, observed_at, "
                        "normalized_state_hash, normalized_state_hash_version, "
                        "original_legacy_normalized_state_hash, response_state_hash, "
                        "response_artifact_hash, timing_status, processing_status, "
                        "normalized_change_count) VALUES "
                        "('2222222222222222222222222222222222222222222222222222222222222222', "
                        "'direct', 'legacy-match', '2026-07-30T02:01:00Z', "
                        "'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', "
                        "2, "
                        "'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', "
                        "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                        "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                        "'on_time', 'processed', 0)"
                    )
                )
    finally:
        engine.dispose()
