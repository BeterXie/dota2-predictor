"""PostgreSQL engine and transaction helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


DATABASE_URL_ENV = "DATABASE_URL"
POSTGRESQL_SCHEME = "postgresql+psycopg://"


def require_database_url(
    value: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the configured PostgreSQL URL and reject legacy backends."""

    source = os.environ if environ is None else environ
    configured = (value if value is not None else source.get(DATABASE_URL_ENV, "")).strip()
    if not configured:
        raise RuntimeError(f"{DATABASE_URL_ENV} is required")
    if configured.startswith("postgresql://"):
        configured = POSTGRESQL_SCHEME + configured.removeprefix("postgresql://")
    if not configured.startswith(POSTGRESQL_SCHEME):
        raise ValueError("DATABASE_URL must use PostgreSQL with the psycopg driver")
    return configured


def build_engine(database_url: str | None = None) -> Engine:
    """Build the single runtime database engine."""

    return create_engine(
        require_database_url(database_url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    """Yield one SQLAlchemy connection inside an atomic transaction."""

    with engine.begin() as connection:
        yield connection


@contextmanager
def advisory_lock(engine: Engine, name: str) -> Iterator[None]:
    """Hold one PostgreSQL session advisory lock for a named runtime task."""

    normalized = name.strip()
    if not normalized:
        raise ValueError("advisory lock name is required")
    with engine.connect() as connection:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:name, 0))"),
            {"name": normalized},
        ).scalar_one()
        if not acquired:
            raise RuntimeError(f"database task lock is already held: {normalized}")
        try:
            yield
        finally:
            released = connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:name, 0))"),
                {"name": normalized},
            ).scalar_one()
            if not released:
                raise RuntimeError(
                    f"database task lock was not held at release: {normalized}"
                )
