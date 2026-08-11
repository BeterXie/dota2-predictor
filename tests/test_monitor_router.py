from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers import control, monitor


class _Connection:
    def execute(self, query: str, params: tuple[object, ...] = ()) -> SimpleNamespace:
        assert "FROM raybet_matches" in query
        assert params == ("live-1",)
        return SimpleNamespace(fetchone=lambda: {"present": 1})

    def close(self) -> None:
        return None


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(monitor.router)
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 45678),
    )


def _slots() -> list[dict[str, object]]:
    return [
        {
            "team_id": 10 if index < 5 else 20,
            "side": "radiant" if index < 5 else "dire",
            "position": index % 5 + 1,
            "hero_id": index + 1,
            "player_id": None,
        }
        for index in range(10)
    ]


def test_locked_mapping_automatically_runs_verified_prediction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    calls: list[dict[str, object]] = []
    saved = {
        "raybet_match_id": "live-1",
        "map_number": 1,
        "version": 3,
        "source": "manual",
        "is_locked": True,
        "created_by": "local-operator",
        "created_at": "2026-08-10T07:00:00+00:00",
        "slots": _slots(),
    }
    monkeypatch.setattr(monitor.queries, "get_db", lambda: connection)
    monkeypatch.setattr(monitor, "save_live_draft_mapping", lambda *args, **kwargs: saved)

    def generate(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "created", "prediction": {"prediction_hash": "a" * 64}}

    monkeypatch.setattr(monitor, "generate_live_draft_prediction", generate)
    monkeypatch.setattr(
        monitor,
        "record_pregame_checkpoint",
        lambda *_args, **_kwargs: {"checkpoint_id": 9, "decision": "bet_team_a"},
    )
    with _client() as client:
        session_id, csrf_token, _ = control.control_sessions.issue()
        client.cookies.set(control._COOKIE_NAME, session_id)
        response = client.post(
            "/api/monitor/matches/live-1/maps/1/draft-mapping",
            headers={"X-Monitor-CSRF": csrf_token},
            json={
                "slots": _slots(),
                "is_locked": True,
                "actor": "local-operator",
                "evidence_source_url": "https://example.test/evidence/live-1/map-1",
            },
        )

    assert response.status_code == 200
    assert response.json()["prediction_automation"]["status"] == "created"
    assert response.json()["decision_checkpoint"] == {
        "checkpoint_id": 9,
        "decision": "bet_team_a",
    }
    assert len(calls) == 1
    assert calls[0]["raybet_match_id"] == "live-1"
    assert calls[0]["mapping_version"] == 3
    assert "game_clock_seconds" not in calls[0]
    assert "live_state_input_used" not in calls[0]


def test_locked_mapping_returns_complete_prediction_blocker_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    saved = {
        "raybet_match_id": "live-1",
        "map_number": 1,
        "version": 3,
        "source": "manual",
        "is_locked": True,
        "created_by": "local-operator",
        "created_at": "2026-08-10T07:00:00+00:00",
        "slots": _slots(),
    }
    monkeypatch.setattr(monitor.queries, "get_db", lambda: connection)
    monkeypatch.setattr(monitor, "save_live_draft_mapping", lambda *args, **kwargs: saved)

    def blocked(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ValueError("vision_draft_anchor_unavailable")

    monkeypatch.setattr(monitor, "generate_live_draft_prediction", blocked)
    monkeypatch.setattr(
        monitor,
        "record_pregame_checkpoint",
        lambda *_args, **_kwargs: {
            "checkpoint_id": 10,
            "decision": "skip",
            "reason": "pregame_prediction_unavailable",
        },
    )
    with _client() as client:
        session_id, csrf_token, _ = control.control_sessions.issue()
        client.cookies.set(control._COOKIE_NAME, session_id)
        response = client.post(
            "/api/monitor/matches/live-1/maps/1/draft-mapping",
            headers={"X-Monitor-CSRF": csrf_token},
            json={
                "slots": _slots(),
                "is_locked": True,
                "actor": "local-operator",
                "evidence_source_url": "https://example.test/evidence/live-1/map-1",
            },
        )

    assert response.status_code == 200
    assert response.json()["prediction_automation"] == {
        "status": "blocked",
        "missing_reason": "vision_draft_anchor_unavailable",
        "prediction": None,
    }
    assert response.json()["decision_checkpoint"]["decision"] == "skip"


def test_locked_mapping_rejects_missing_source_before_persistence() -> None:
    with _client() as client:
        session_id, csrf_token, _ = control.control_sessions.issue()
        client.cookies.set(control._COOKIE_NAME, session_id)
        response = client.post(
            "/api/monitor/matches/live-1/maps/1/draft-mapping",
            headers={"X-Monitor-CSRF": csrf_token},
            json={
                "slots": _slots(),
                "is_locked": True,
                "actor": "local-operator",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "locked manual draft mapping requires evidence_source_url"
    )


def test_prediction_request_rejects_client_supplied_live_state() -> None:
    with _client() as client:
        session_id, csrf_token, _ = control.control_sessions.issue()
        client.cookies.set(control._COOKIE_NAME, session_id)
        response = client.post(
            "/api/monitor/matches/live-1/maps/1/draft-prediction",
            headers={"X-Monitor-CSRF": csrf_token},
            json={
                "mapping_version": 3,
                "operator_identity": "local-operator",
                "confirmation_text": "confirmation",
                "game_clock_seconds": 120,
            },
        )

    assert response.status_code == 422


@pytest.mark.parametrize("prediction", [{"record_status": "paired"}, None])
def test_prediction_get_returns_independent_map_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
    prediction: dict[str, object] | None,
) -> None:
    connection = _Connection()
    checkpoint_calls: list[tuple[str, int]] = []
    checkpoints = [
        {
            "checkpoint_id": 7,
            "phase": "pregame",
            "checkpoint_minute": 0,
            "decision": "bet_team_a",
        }
    ]
    monkeypatch.setattr(monitor.queries, "get_db", lambda: connection)
    monkeypatch.setattr(
        monitor,
        "LiveDraftProspectiveBridgeRepository",
        lambda _connection: SimpleNamespace(
            load_prediction=lambda *_args: prediction,
        ),
    )

    def latest(_connection: object, match_id: str, map_number: int):
        checkpoint_calls.append((match_id, map_number))
        return checkpoints

    monkeypatch.setattr(monitor, "latest_map_checkpoints", latest)

    with _client() as client:
        response = client.get(
            "/api/monitor/matches/live-1/maps/1/draft-prediction",
            params={"mapping_version": 3},
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "available" if prediction is not None else "not_found",
        "prediction": prediction,
        "decision_checkpoints": checkpoints,
    }
    assert checkpoint_calls == [("live-1", 1)]


def test_acceptance_endpoint_exposes_three_series_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    expected = {
        "status": "incomplete",
        "required_consecutive_series": 3,
        "consecutive_accepted_series": 1,
        "goal_met": False,
        "series": [],
    }
    calls: list[tuple[object, int, bool]] = []
    monkeypatch.setattr(monitor.queries, "get_db", lambda: connection)

    def audit(
        received: object,
        *,
        limit: int,
        verify_frame_bytes: bool,
    ) -> dict[str, object]:
        calls.append((received, limit, verify_frame_bytes))
        return expected

    monkeypatch.setattr(monitor, "audit_acceptance_progress", audit)

    with _client() as client:
        response = client.get(
            "/api/monitor/acceptance",
            params={"limit": 7, "verify_frame_bytes": "true"},
        )

    assert response.status_code == 200
    assert response.json() == expected
    assert calls == [(connection, 7, True)]
