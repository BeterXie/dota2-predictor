from __future__ import annotations

from fastapi.testclient import TestClient

from database.session import PostgresSession
from web import queries
from web.app import app


def test_prematch_read_api_queries_fresh_alembic_schema(
    postgres_engine,
    monkeypatch,
) -> None:
    monkeypatch.setattr(queries, "get_db", lambda: PostgresSession(postgres_engine))

    with TestClient(app) as client:
        models = client.get("/api/intelligence/prematch/models")
        predictions = client.get("/api/intelligence/prematch/predictions")
        filtered_models = client.get(
            "/api/intelligence/prematch/models",
            params={
                "model_kind": "team_only",
                "availability_mode": "reconstructed_walk_forward",
                "status": "trained",
            },
        )
        filtered_predictions = client.get(
            "/api/intelligence/prematch/predictions",
            params={
                "model_kind": "team_plus_draft_rosh",
                "availability_mode": "prospective",
                "status": "predicted",
            },
        )
        missing = client.get("/api/intelligence/prematch/matches/999999")

    assert models.status_code == 200
    assert models.json()["data"] == []
    assert predictions.status_code == 200
    assert predictions.json()["data"] == []
    assert filtered_models.status_code == 200
    assert filtered_models.json()["data"] == []
    assert filtered_predictions.status_code == 200
    assert filtered_predictions.json()["data"] == []
    assert missing.status_code == 404
