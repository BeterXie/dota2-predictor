from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError

from database.session import DatabaseRow
from web import queries


class _Result:
    def fetchall(self):
        return [DatabaseRow(("value",), (7,))]

    def fetchone(self):
        return DatabaseRow(("value",), (7,))


class _Session:
    def __init__(self, error: SQLAlchemyError | None = None) -> None:
        self.error = error
        self.closed = False

    def execute(self, query, params):
        if self.error is not None:
            raise self.error
        return _Result()

    def close(self) -> None:
        self.closed = True


def test_safe_execute_closes_session_on_success(monkeypatch) -> None:
    session = _Session()
    monkeypatch.setattr(queries, "get_db", lambda: session)

    assert queries._safe_execute("SELECT 7", fetch="value") == 7
    assert session.closed


def test_safe_execute_closes_session_and_surfaces_database_errors(monkeypatch) -> None:
    session = _Session(SQLAlchemyError("database unavailable"))
    monkeypatch.setattr(queries, "get_db", lambda: session)

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        queries._safe_execute("SELECT 7", fetch="value")
    assert session.closed


def test_safe_execute_closes_session_for_unsupported_fetch_mode(monkeypatch) -> None:
    session = _Session()
    monkeypatch.setattr(queries, "get_db", lambda: session)

    with pytest.raises(ValueError, match="unsupported fetch mode"):
        queries._safe_execute("SELECT 7", fetch="stream")
    assert session.closed


@pytest.mark.parametrize(
    ("query", "args"),
    [
        (queries.get_match_draft, (1,)),
        (queries.get_match_detail, (1,)),
        (queries.get_team_profile, (1,)),
        (queries.get_hero_detail, (1,)),
        (queries.get_head_to_head, (1, 2)),
    ],
)
def test_direct_queries_close_sessions_and_surface_database_errors(
    monkeypatch,
    query,
    args,
) -> None:
    session = _Session(SQLAlchemyError("database unavailable"))
    monkeypatch.setattr(queries, "get_db", lambda: session)

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        query(*args)
    assert session.closed
