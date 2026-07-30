from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from database.engine import build_engine, require_database_url, transaction


ROOT = Path(__file__).resolve().parents[3]
CORE_TABLES = {
    "matches",
    "teams",
    "leagues",
    "heroes",
    "match_players",
    "picks_bans",
    "gold_advantage",
    "xp_advantage",
    "teamfights",
    "teamfight_players",
    "objectives",
    "chat",
    "hero_matchups",
    "hero_duration_stats",
    "hero_benchmarks",
}


@pytest.fixture(scope="module")
def postgres_database_url() -> Iterator[str]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_test_{uuid4().hex}"
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
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()",
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin_engine.dispose()


def test_core_migration_and_transaction_contract(postgres_database_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", postgres_database_url)
    command.upgrade(config, "head")

    engine = build_engine(postgres_database_url)
    try:
        inspector = inspect(engine)
        assert CORE_TABLES <= set(inspector.get_table_names())
        assert inspector.get_foreign_keys("match_players")
        player_columns = {
            column["name"]: column
            for column in inspector.get_columns("match_players")
        }
        assert player_columns["id"].get("identity") is not None

        with pytest.raises(RuntimeError, match="force rollback"):
            with transaction(engine) as connection:
                connection.execute(
                    text(
                        "INSERT INTO heroes (hero_id, localized_name) "
                        "VALUES (:hero_id, :localized_name)",
                    ),
                    {"hero_id": 1, "localized_name": "Anti-Mage"},
                )
                raise RuntimeError("force rollback")

        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM heroes")).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version"),
            ).scalar_one()
        assert count == 0
        assert revision == "20260730_0002"
    finally:
        engine.dispose()

    command.downgrade(config, "base")
    downgraded_engine = build_engine(postgres_database_url)
    try:
        assert CORE_TABLES.isdisjoint(inspect(downgraded_engine).get_table_names())
    finally:
        downgraded_engine.dispose()
