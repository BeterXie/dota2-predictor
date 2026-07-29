from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from live_betting.rosh_parity import RoshAnalysisError
from live_betting.rosh_parity_storage import (
    RoshHeroScoreRecord,
    RoshMinutePointRecord,
    RoshRunRecord,
    StoredRoshRun,
)
from prematch.stratz_official_profile import get_profile
from web import app as web_app
from web import queries
from web.schemas import RoshAnalysisRequest, RoshAnalysisRunResponse


def _stored() -> StoredRoshRun:
    profile = get_profile()
    run = RoshRunRecord(
        run_id="a" * 64,
        status="succeeded",
        mode="explicit_draft",
        match_id=None,
        date_time=1_785_000_000,
        draft_hash="b" * 64,
        draft={"radiant": [], "dire": []},
        rosh_profile_id=profile.rosh_profile_id,
        formula_version=profile.formula_version,
        request_profile_hash=profile.request_profile_hash,
        upstream_bundle_hash=profile.upstream_bundle_hash,
        scorer_source_hash=profile.scorer_source_hash,
        canonical_profile_hash=profile.canonical_profile_hash,
        serialization_version=profile.serialization_version,
        request_hash="c" * 64,
        request_manifest={},
        response_manifest=(),
        evidence_hash="d" * 64,
        collected_at="2026-07-29T00:00:00+00:00",
        radiant_team_score=-4.9,
        dire_team_score=-10.7,
        relative_advantage=5.8,
    )
    hero = RoshHeroScoreRecord(
        "RADIANT",
        1,
        54,
        1.25,
        1.3,
        {
            "position_base_diff": 1.0,
            "same_team_synergy": 0.2,
            "opponent_matchup_synergy": 0.05,
        },
    )
    minute = RoshMinutePointRecord(
        36,
        5.75,
        5.8,
        1.0,
        -2.0,
        2.75,
        {"rank_source_counts": {"DIVINE_IMMORTAL": 6}, "slots": []},
    )
    return StoredRoshRun(run, (hero,), (minute,), {})


def _request() -> RoshAnalysisRequest:
    return RoshAnalysisRequest(
        mode="explicit_draft",
        date_time=1_785_000_000,
        radiant=[
            {"hero_id": hero_id, "position_id": position}
            for position, hero_id in enumerate(range(1, 6), 1)
        ],
        dire=[
            {"hero_id": hero_id, "position_id": position}
            for position, hero_id in enumerate(range(6, 11), 1)
        ],
    )


def test_new_api_projection_returns_scores_and_never_win_probability(tmp_path: Path) -> None:
    database = tmp_path / "web.db"
    sqlite3.connect(database).close()
    orchestrator = SimpleNamespace(execute=lambda *_args: _stored())
    with (
        patch.object(queries, "DB_PATH", str(database)),
        patch.object(
            web_app,
            "_get_rosh_analysis_orchestrator",
            return_value=orchestrator,
        ),
    ):
        result = web_app.create_rosh_analysis(_request())

    payload = RoshAnalysisRunResponse.model_validate(result).model_dump(by_alias=True)
    assert payload["relative_advantage"] == 5.8
    assert payload["radiant_team_score"] == -4.9
    assert payload["dire_team_score"] == -10.7
    assert payload["hero_components"][0]["position_base_diff"] == 1.0
    assert payload["minute_points"][0]["display_score"] == 5.8
    assert "win_probability" not in str(payload)


def test_structured_predraft_error_has_no_run_id(tmp_path: Path) -> None:
    database = tmp_path / "web.db"
    sqlite3.connect(database).close()
    orchestrator = SimpleNamespace(
        execute=lambda *_args: (_ for _ in ()).throw(
            RoshAnalysisError("upstream_unavailable")
        )
    )
    with (
        patch.object(queries, "DB_PATH", str(database)),
        patch.object(
            web_app,
            "_get_rosh_analysis_orchestrator",
            return_value=orchestrator,
        ),
        pytest.raises(HTTPException) as raised,
    ):
        web_app.create_rosh_analysis(_request())

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "error_code": "upstream_unavailable",
        "message": "Rosh upstream is unavailable",
    }


def test_canonical_failure_exposes_only_public_run_identity(tmp_path: Path) -> None:
    database = tmp_path / "web.db"
    sqlite3.connect(database).close()
    orchestrator = SimpleNamespace(
        execute=lambda *_args: (_ for _ in ()).throw(
            RoshAnalysisError("source_data_incomplete", run_id="e" * 64)
        )
    )
    with (
        patch.object(queries, "DB_PATH", str(database)),
        patch.object(
            web_app,
            "_get_rosh_analysis_orchestrator",
            return_value=orchestrator,
        ),
        pytest.raises(HTTPException) as raised,
    ):
        web_app.create_rosh_analysis(_request())

    assert raised.value.detail["run_id"] == "e" * 64
    assert set(raised.value.detail) == {"error_code", "message", "run_id"}


def test_get_is_read_only_and_does_not_build_transport(tmp_path: Path) -> None:
    database = tmp_path / "web.db"
    sqlite3.connect(database).close()
    repository = SimpleNamespace(get=lambda run_id: _stored() if run_id == "a" * 64 else None)
    with (
        patch.object(queries, "DB_PATH", str(database)),
        patch.object(web_app, "RoshRunRepository", return_value=repository),
        patch.object(web_app, "_get_rosh_analysis_orchestrator") as builder,
    ):
        result = web_app.get_rosh_analysis("a" * 64)

    assert result["run_id"] == "a" * 64
    builder.assert_not_called()


def test_response_schema_has_no_probability_field() -> None:
    schema = RoshAnalysisRunResponse.model_json_schema()
    assert "probability" not in str(schema).lower()


def test_route_validation_failure_is_sanitized_and_structured() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/prematch/rosh-analysis",
            "raw_path": b"/api/prematch/rosh-analysis",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )
    error = RequestValidationError([{"type": "missing", "loc": ("body",)}])

    response = asyncio.run(web_app._request_validation_error(request, error))

    assert response.status_code == 400
    assert json.loads(response.body) == {
        "detail": {
            "error_code": "invalid_request",
            "message": "Rosh analysis request is invalid",
        }
    }
