from __future__ import annotations

from fastapi.testclient import TestClient

from database.session import PostgresSession
from web import queries
from web.app import app


def test_intelligence_api_queries_alembic_schema(postgres_engine, monkeypatch) -> None:
    monkeypatch.setattr(queries, "get_db", lambda: PostgresSession(postgres_engine))
    app.state.milestone_revocation_config = None

    with TestClient(app) as client:
        overview = client.get("/api/intelligence/overview")
        matches = client.get("/api/intelligence/matches")
        players = client.get("/api/intelligence/players")
        teams = client.get("/api/intelligence/teams")
        missing = client.get("/api/intelligence/matches/999999")

    assert overview.status_code == 200
    assert matches.status_code == 200
    assert matches.json()["data"] == []
    assert players.status_code == 200
    assert players.json()["data"] == []
    assert teams.status_code == 200
    assert teams.json()["data"] == []
    assert missing.status_code == 404
