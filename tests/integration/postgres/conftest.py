from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from database.engine import build_engine, require_database_url


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_session_test_{uuid4().hex}"
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
