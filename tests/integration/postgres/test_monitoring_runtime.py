from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from database.session import PostgresSession
from live_betting.storage import LiveBettingStore
from web import queries
from web.app import app
from web.monitoring import monitor_history_page, monitor_match_detail, monitor_matches


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _raybet_match(match_id: str, *, status: int, scheduled_at: datetime) -> dict:
    return {
        "id": match_id,
        "tournament_name": "PostgreSQL Integration Cup",
        "start_time": scheduled_at.isoformat(),
        "round": "bo3",
        "status": status,
        "team": [
            {"id": 11, "pos": 1, "team_name": "Radiant Five"},
            {"id": 22, "pos": 2, "team_name": "Dire Five"},
        ],
    }


def test_monitor_list_detail_and_history_use_postgres(postgres_engine, tmp_path) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.upsert_raybet_match(
        _raybet_match("live-match", status=2, scheduled_at=NOW - timedelta(hours=1)),
        NOW,
    )
    store.upsert_raybet_match(
        _raybet_match(
            "history-match",
            status=3,
            scheduled_at=NOW - timedelta(days=2),
        ),
        NOW - timedelta(days=1),
    )
    store.connection.commit()

    matches = monitor_matches(store.connection, now=NOW)
    assert any(item["raybet_match_id"] == "live-match" for item in matches)

    detail = monitor_match_detail(store.connection, "live-match", now=NOW)
    assert detail is not None
    assert detail["raybet_match_id"] == "live-match"
    assert detail["lifecycle"] in {"live", "degraded"}

    history = monitor_history_page(store.connection, now=NOW)
    assert [item["raybet_match_id"] for item in history["items"]] == [
        "history-match"
    ]
    store.close()


def test_monitor_api_uses_postgres_session(postgres_engine, tmp_path, monkeypatch) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.upsert_raybet_match(
        _raybet_match("api-match", status=2, scheduled_at=NOW - timedelta(hours=1)),
        NOW,
    )
    store.connection.commit()
    store.close()

    monkeypatch.setattr(queries, "get_db", lambda: PostgresSession(postgres_engine))
    app.state.milestone_revocation_config = None

    with TestClient(app) as client:
        bootstrap = client.get("/api/monitor/bootstrap")
        detail = client.get("/api/monitor/matches/api-match")

    assert bootstrap.status_code == 200
    assert any(
        item["raybet_match_id"] == "api-match"
        for item in bootstrap.json()["matches"]
    )
    assert detail.status_code == 200
    assert detail.json()["raybet_match_id"] == "api-match"
