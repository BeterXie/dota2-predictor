from __future__ import annotations

import pytest

from database.engine import require_database_url


def test_database_url_is_required() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        require_database_url(environ={})


def test_plain_postgresql_url_selects_psycopg() -> None:
    assert require_database_url(
        "postgresql://dota2:secret@localhost/dota2_predictor",
    ) == "postgresql+psycopg://dota2:secret@localhost/dota2_predictor"


def test_sqlite_runtime_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="must use PostgreSQL"):
        require_database_url("sqlite:///data/dota2.db")
