from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers import control, vision_calibration


class StubCalibrationService:
    def bootstrap(self, *, limit: int) -> dict[str, object]:
        return {"limit": limit, "events": [], "candidates": [], "evaluations": []}

    def read_event_asset(self, event_id: str, asset_name: str) -> Path:
        raise FileNotFoundError((event_id, asset_name))

    def save_label(self, event_id: str, **kwargs: object) -> dict[str, object]:
        return {"event_id": event_id, **kwargs}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(vision_calibration.router)
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 45678),
    )


def test_bootstrap_is_loopback_only_and_asset_paths_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vision_calibration, "calibration_service", StubCalibrationService())
    with _client() as client:
        response = client.get("/api/vision-calibration/bootstrap?limit=7")
        assert response.status_code == 200
        assert response.json()["limit"] == 7

        traversal = client.get(
            "/api/vision-calibration/events/0123456789abcdef0123/assets/metadata.json"
        )
        assert traversal.status_code in {404, 422}


def test_mutation_requires_control_session_and_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vision_calibration, "calibration_service", StubCalibrationService())
    with _client() as client:
        payload = {
            "hero_ids": list(range(1, 11)),
            "raybet_match_id": "38416120",
            "map_number": 2,
        }
        forbidden = client.post(
            "/api/vision-calibration/events/0123456789abcdef0123/label",
            json=payload,
        )
        assert forbidden.status_code == 403

        session_id, csrf_token, _ = control.control_sessions.issue()
        client.cookies.set(control._COOKIE_NAME, session_id)
        accepted = client.post(
            "/api/vision-calibration/events/0123456789abcdef0123/label",
            headers={"X-Monitor-CSRF": csrf_token},
            json=payload,
        )
        assert accepted.status_code == 200
        assert accepted.json()["event_id"] == "0123456789abcdef0123"
        assert accepted.json()["raybet_match_id"] == "38416120"
        assert accepted.json()["map_number"] == 2
