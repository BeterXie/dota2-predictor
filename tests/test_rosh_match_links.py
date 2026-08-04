from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_betting.rosh_parity_storage import (
    RoshRunMatchLink,
    RoshRunRecord,
    StoredRoshRun,
)
from web import app as web_app
from web import queries
from web.schemas import (
    RoshAnalysisRecordPageResponse,
    RoshAnalysisRequest,
)


def _stored() -> StoredRoshRun:
    return StoredRoshRun(
        run=RoshRunRecord(
            run_id="a" * 64,
            status="succeeded",
            mode="explicit_draft",
            match_id=None,
            date_time=1_785_000_000,
            draft_hash="b" * 64,
            draft={"radiant": [], "dire": []},
            rosh_profile_id="stratz-rosh-web-2026-07-28-v2",
            formula_version="stratz-official-rosh/2026-07-28-v2",
            request_profile_hash="c" * 64,
            upstream_bundle_hash="d" * 64,
            scorer_source_hash="e" * 64,
            canonical_profile_hash="f" * 64,
            serialization_version="rfc8785-jcs/v1",
            request_hash="1" * 64,
            request_manifest={},
            response_manifest=(),
            evidence_hash="2" * 64,
            collected_at="2026-07-29T00:00:00+00:00",
            radiant_team_score=4.2,
            dire_team_score=-1.6,
            relative_advantage=5.8,
        ),
        hero_scores=(),
        minute_points=(),
        result={},
    )


def _live_request() -> RoshAnalysisRequest:
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
        match_links=[
            {"source": "raybet", "source_match_id": "38417786", "map_number": 3}
        ],
    )


def test_create_links_succeeded_run_without_leaking_links_into_official_request() -> None:
    connection = Mock()
    execute = Mock(return_value=_stored())
    repository = Mock()
    with (
        patch.object(queries, "get_db", return_value=connection),
        patch.object(
            web_app,
            "_get_rosh_analysis_orchestrator",
            return_value=SimpleNamespace(execute=execute),
        ),
        patch.object(web_app, "RoshRunRepository", return_value=repository),
    ):
        result = web_app.create_rosh_analysis(_live_request())

    assert "match_links" not in execute.call_args.args[0]
    repository.link_matches.assert_called_once_with(
        "a" * 64,
        [{"source": "raybet", "source_match_id": "38417786", "map_number": 3}],
        linked_at="2026-07-29T00:00:00+00:00",
    )
    assert result["run_id"] == "a" * 64
    connection.close.assert_called_once()


def test_historical_request_links_opendota_and_stratz_official_match_ids() -> None:
    request = RoshAnalysisRequest(
        mode="historical_match",
        match_id=8904419709,
        date_time=1_785_000_000,
        match_links=[
            {"source": "opendota", "source_match_id": "8904419709"}
        ],
    )

    assert web_app._rosh_match_links(request) == [
        {"source": "opendota", "source_match_id": "8904419709", "map_number": None},
        {"source": "stratz", "source_match_id": "8904419709", "map_number": None},
    ]


def test_record_query_returns_all_provider_links_for_the_same_run() -> None:
    connection = Mock()
    links = (
        RoshRunMatchLink("raybet", "38417786", "a" * 64, 3, "2026-07-29T00:00:00Z"),
        RoshRunMatchLink("opendota", "8904419709", "a" * 64, None, "2026-07-29T00:00:00Z"),
        RoshRunMatchLink("stratz", "8904419709", "a" * 64, None, "2026-07-29T00:00:00Z"),
    )
    repository = Mock()
    repository.get_match_records.return_value = ((_stored(), links),)
    with (
        patch.object(queries, "get_db", return_value=connection),
        patch.object(web_app, "RoshRunRepository", return_value=repository),
    ):
        result = web_app.get_rosh_analysis_records("raybet", "38417786")

    payload = RoshAnalysisRecordPageResponse.model_validate(result)
    assert payload.records[0].run.run_id == "a" * 64
    assert {link.source for link in payload.records[0].links} == {
        "raybet",
        "opendota",
        "stratz",
    }
    repository.get_match_records.assert_called_once_with("raybet", "38417786")
    connection.close.assert_called_once()
