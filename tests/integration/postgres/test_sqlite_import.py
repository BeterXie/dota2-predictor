from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from database.engine import build_engine, require_database_url
from scripts.migrate_sqlite_to_postgres import migrate_sqlite_to_postgres


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
            INSERT INTO heroes VALUES (1, 'Anti-Mage', 'antimage');
            INSERT INTO matches VALUES (8904419709, 1);
            INSERT INTO settlements VALUES (
                'manual-review-order', 'win', 2.2,
                '2026-07-30T01:00:00Z', 'manual-review-evidence', 1
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


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
    assert dry_run.row_counts == {"heroes": 1, "matches": 1, "settlements": 1}
    assert source.read_bytes() == source_before

    report = migrate_sqlite_to_postgres(source, postgres_database_url)

    assert report.dry_run is False
    assert report.target_revision == "20260730_0013"
    assert report.row_counts == {"heroes": 1, "matches": 1, "settlements": 1}
    assert report.primary_key_ranges == {
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
        assert hero == ("Anti-Mage", "antimage")
        assert match is True
        assert settlement == ("manual-review-order", 1)
    finally:
        engine.dispose()
