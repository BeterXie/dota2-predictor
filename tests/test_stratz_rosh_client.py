from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from live_betting.live_player_identity import (
    canonical_live_player_identity_evidence_hash,
)
from live_betting.stratz_rosh_client import (
    StratzRoshClient,
    StratzRoshError,
)
from prematch.stratz_rosh import MATCH_PICKS_BANS_QUERY


class Response:
    def __init__(
        self,
        payload: dict[str, Any],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if content is None
            else content
        )

    def json(self) -> dict[str, Any]:
        return self.payload


def fixture() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "stratz-rosh.json"
    return json.loads(path.read_text(encoding="utf-8"))


def identity_evidence(data: dict[str, Any], fetched_at: datetime) -> dict[str, Any]:
    radiant_players = [100, 101, 102, 103, 104]
    dire_players = [200, 201, 202, 203, 204]
    values = {
        "source_name": "opendota_live",
        "source_match_id": 123,
        "radiant_team_id": 10,
        "dire_team_id": 20,
        "fetched_at": fetched_at,
    }
    return {
        **values,
        "evidence_hash": canonical_live_player_identity_evidence_hash(
            radiant_team_id=values["radiant_team_id"],
            dire_team_id=values["dire_team_id"],
            radiant_hero_ids=data["radiant_heroes"],
            dire_hero_ids=data["dire_heroes"],
            radiant_player_ids=radiant_players,
            dire_player_ids=dire_players,
            source_match_id=values["source_match_id"],
            source_name=values["source_name"],
            fetched_at=fetched_at,
        ),
    }


def historical_match_payload(
    data: dict[str, Any], *, missing_player_index: int | None = None
) -> dict[str, Any]:
    heroes = [*data["radiant_heroes"], *data["dire_heroes"]]
    players = []
    picks = []
    for index, hero_id in enumerate(heroes):
        player_id = 100 + index
        players.append(
            {
                "heroId": hero_id,
                "position": f"POSITION_{(index % 5) + 1}",
                "steamAccountId": (
                    None if index == missing_player_index else player_id
                ),
            }
        )
        picks.append(
            {
                "heroId": hero_id,
                "order": index,
                "isPick": True,
                "isRadiant": index < 5,
            }
        )
    return {
        "data": {
            "match": {
                "id": 123,
                "bracket": 8,
                "endDateTime": data["week"],
                "players": players,
                "pickBans": picks,
            }
        }
    }


def historical_post(
    data: dict[str, Any],
    *,
    missing_player_index: int | None = None,
    player_calls: list[dict[str, Any]] | None = None,
) -> Any:
    highlight = next(
        row for row in data["radiant_player_highlights"] if row is not None
    )

    def post(_url: str, **kwargs: Any) -> Response:
        operation = kwargs["json"]["operationName"]
        if operation == "GetMatchPicksBans":
            return Response(
                historical_match_payload(
                    data, missing_player_index=missing_player_index
                )
            )
        if operation == "PlayerHeroHighlights":
            if player_calls is not None:
                player_calls.append(kwargs["json"])
            return Response(
                {
                    "data": {
                        "plus": {
                            f"player_{index}": highlight for index in range(10)
                        }
                    }
                }
            )
        key = {
            "HeroesMetaPositionsByWeek": "heroes_meta_positions",
            "GetHeroStatsByTime": "hero_stats_by_time_bracket",
            "Synergy": "synergy",
        }[operation]
        return Response(data["responses"][key])

    return post


def test_legacy_lineup_batch_preserves_exact_request_and_response_bytes() -> None:
    data = fixture()
    collected_at = datetime(2026, 8, 7, 1, 2, 3, tzinfo=timezone.utc)
    sent: list[bytes] = []
    raw_by_operation: dict[str, bytes] = {}

    def post(_url: str, **kwargs: Any) -> Response:
        body = bytes(kwargs["data"])
        sent.append(body)
        payload = json.loads(body)
        operation = payload["operationName"]
        key = {
            "HeroesMetaPositionsByWeek": "heroes_meta_positions",
            "GetHeroStatsByTime": "hero_stats_by_time_bracket",
            "Synergy": "synergy",
        }[operation]
        raw = ("  " + json.dumps(data["responses"][key]) + "\n").encode("utf-8")
        raw_by_operation[key] = raw
        return Response(data["responses"][key], content=raw)

    result = StratzRoshClient(
        "private-token",
        post=post,
        clock=lambda: collected_at,
    ).fetch_legacy_lineup_batch(
        data["radiant_heroes"],
        data["dire_heroes"],
        statistics_cutoff=collected_at - timedelta(seconds=30),
    )

    assert result.collected_at == collected_at
    assert tuple(result.request_bodies) == (
        "heroes_meta_positions",
        "hero_stats_by_time_bracket",
        "synergy",
    )
    assert tuple(result.response_bodies) == tuple(result.request_bodies)
    assert list(result.request_bodies.values()) == sent
    assert dict(result.response_bodies) == raw_by_operation
    for body in sent:
        payload = json.loads(body)
        assert set(payload) == {"operationName", "query", "variables"}
        assert payload["variables"].get("week", payload["variables"].get("currentWeek")) <= int(
            (collected_at - timedelta(seconds=30)).timestamp()
        )


def test_historical_match_query_requests_player_identity_fields() -> None:
    assert "steamAccountId" in MATCH_PICKS_BANS_QUERY
    assert "isAnonymous" not in MATCH_PICKS_BANS_QUERY


def test_historical_score_defaults_to_pure_and_marks_retrospective() -> None:
    data = fixture()
    player_calls: list[dict[str, Any]] = []
    fetched_at = datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc)

    result = StratzRoshClient(
        "private-token",
        post=historical_post(data, player_calls=player_calls),
        clock=lambda: fetched_at,
    ).fetch_historical_match_score(123)

    assert player_calls == []
    assert result.score is not None
    assert result.score.scoring_mode == "pure"
    assert result.score.current_player_adjusted_lineup_score is None
    assert result.score.player_coverage_count == 0
    assert result.score.evidence["player_stats_as_of"] is None
    assert result.score.evidence["retrospective"] is True
    assert result.score.evidence["current_player_adjustment_only"] is True
    assert result.score.evidence["backtest_eligible"] is False


def test_historical_score_uses_current_adjustment_only_at_ten_of_ten() -> None:
    data = fixture()
    fetched_at = datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc)

    result = StratzRoshClient(
        "private-token",
        post=historical_post(data),
        clock=lambda: fetched_at,
    ).fetch_historical_match_score(123, include_current_player_adjustment=True)

    assert result.score is not None
    assert result.score.scoring_mode == "current_player_adjusted"
    assert result.score.current_player_adjusted_lineup_score is not None
    assert result.score.effective_lineup_score == (
        result.score.current_player_adjusted_lineup_score
    )
    assert result.score.player_coverage_count == 10
    assert result.score.player_stats_as_of == fetched_at
    assert result.score.evidence["player_stats_as_of"] == fetched_at.isoformat()


def test_historical_score_falls_back_to_pure_at_nine_of_ten() -> None:
    data = fixture()
    fetched_at = datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc)

    result = StratzRoshClient(
        "private-token",
        post=historical_post(data, missing_player_index=4),
        clock=lambda: fetched_at,
    ).fetch_historical_match_score(123, include_current_player_adjustment=True)

    assert result.score is not None
    assert result.score.scoring_mode == "pure"
    assert result.score.current_player_adjusted_lineup_score is None
    assert result.score.effective_lineup_score == result.score.pure_lineup_score
    assert result.score.player_coverage_count == 9
    assert result.score.evidence["player_slots"][4]["fallback_reason"] == (
        "player_identity_unavailable"
    )


def test_live_queries_use_request_timestamp_and_completion_source_time() -> None:
    data = fixture()
    started = datetime(2026, 7, 21, 12, 34, 56, tzinfo=timezone.utc)
    completed = started + timedelta(seconds=2)
    calls: list[dict[str, Any]] = []

    def post(_url: str, **kwargs: Any) -> Response:
        calls.append(kwargs)
        operation = kwargs["json"]["operationName"]
        key = {
            "HeroesMetaPositionsByWeek": "heroes_meta_positions",
            "GetHeroStatsByTime": "hero_stats_by_time_bracket",
            "Synergy": "synergy",
        }[operation]
        return Response(data["responses"][key])

    result = StratzRoshClient(
        "private-token",
        post=post,
        clock=lambda: completed,
    ).fetch_lineup_score(
        data["radiant_heroes"],
        data["dire_heroes"],
        as_of=started,
    )

    assert result.source_week == int(started.timestamp())
    assert result.source_as_of == completed
    assert result.scoring_mode == "pure"
    assert result.effective_lineup_score == result.pure_lineup_score
    assert result.player_adjusted_lineup_score is None
    assert result.stake_multiplier == 0.5
    assert result.stake_cap == 0.5
    assert result.evidence["pure_minute_table"]
    assert "minute_table" not in result.evidence
    assert all(
        call["json"]["variables"].get(
            "week", call["json"]["variables"].get("currentWeek")
        )
        == int(started.timestamp())
        for call in calls
    )
    assert all(call["impersonate"] == "chrome120" for call in calls)
    assert all(call["headers"]["Authorization"] == "Bearer private-token" for call in calls)


def test_partial_player_errors_retry_only_transient_aliases() -> None:
    data = fixture()
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    raw_highlight = data["radiant_player_highlights"][0]
    player_calls: list[dict[str, Any]] = []

    def post(_url: str, **kwargs: Any) -> Response:
        operation = kwargs["json"]["operationName"]
        if operation != "PlayerHeroHighlights":
            key = {
                "HeroesMetaPositionsByWeek": "heroes_meta_positions",
                "GetHeroStatsByTime": "hero_stats_by_time_bracket",
                "Synergy": "synergy",
            }[operation]
            return Response(data["responses"][key])
        player_calls.append(kwargs["json"])
        variables = kwargs["json"]["variables"]
        if len(variables) == 20:
            plus = {"player_0": raw_highlight}
            plus.update({f"player_{index}": raw_highlight for index in range(4, 10)})
            return Response(
                {
                    "data": {"plus": plus},
                    "errors": [
                        {
                            "message": "player id is missing or anonymous",
                            "path": ["plus", "player_1"],
                        },
                        {
                            "message": "unsupported value",
                            "path": ["plus", "player_2"],
                        },
                        {
                            "message": "temporary upstream failure",
                            "path": ["plus", "player_3"],
                        },
                    ],
                }
            )
        return Response({"data": {"plus": {"player_0": raw_highlight}}})

    result = StratzRoshClient(
        "private-token",
        post=post,
        clock=lambda: started + timedelta(seconds=1),
    ).fetch_lineup_score(
        data["radiant_heroes"],
        data["dire_heroes"],
        radiant_player_ids=[100, 101, 102, 103, 104],
        dire_player_ids=[200, 201, 202, 203, 204],
        player_identity_evidence=identity_evidence(
            data, started - timedelta(seconds=1)
        ),
        as_of=started,
    )

    slots = result.evidence["player_slots"]
    assert len(player_calls) == 2
    assert slots[1]["fallback_reason"] == "player_missing_or_anonymous_in_stratz"
    assert slots[2]["fallback_reason"] == "player_stats_request_failed"
    assert slots[3]["resolved"] is True
    assert result.player_coverage_count == 8
    assert result.scoring_mode == "pure"
    assert result.player_adjusted_lineup_score is None


def test_empty_alias_without_error_is_not_retried() -> None:
    data = fixture()
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    player_calls = 0

    def post(_url: str, **kwargs: Any) -> Response:
        nonlocal player_calls
        operation = kwargs["json"]["operationName"]
        if operation == "PlayerHeroHighlights":
            player_calls += 1
            return Response({"data": {"plus": {}}})
        key = {
            "HeroesMetaPositionsByWeek": "heroes_meta_positions",
            "GetHeroStatsByTime": "hero_stats_by_time_bracket",
            "Synergy": "synergy",
        }[operation]
        return Response(data["responses"][key])

    result = StratzRoshClient(
        "private-token", post=post, clock=lambda: started
    ).fetch_lineup_score(
        data["radiant_heroes"],
        data["dire_heroes"],
        radiant_player_ids=[100, None, None, None, None],
        as_of=started,
    )

    assert player_calls == 1
    assert result.evidence["player_slots"][0]["fallback_reason"] == (
        "player_hero_stats_missing"
    )


def test_transport_failure_never_exposes_token() -> None:
    secret = "do-not-log-this-token"

    def post(_url: str, **_kwargs: Any) -> Response:
        raise RuntimeError(secret)

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient(secret, post=post).fetch_lineup_score(
            [1, 2, 3, 4, 5],
            [6, 7, 8, 9, 10],
            as_of=datetime.now(timezone.utc),
        )

    assert secret not in str(caught.value)
    assert caught.value.retryable is True
    assert caught.value.category == "network_failure"


def test_http_429_exposes_structured_retry_after_without_response_body() -> None:
    def post(_url: str, **_kwargs: Any) -> Response:
        return Response(
            {"secret": "must-not-escape"},
            status_code=429,
            headers={"Retry-After": "7"},
        )

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient("private-token", post=post).fetch_lineup_score(
            [1, 2, 3, 4, 5],
            [6, 7, 8, 9, 10],
            as_of=datetime.now(timezone.utc),
        )

    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 7.0
    assert caught.value.category == "http_429"
    assert "must-not-escape" not in str(caught.value)


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (400, "http_failure"),
        (401, "http_auth_failure"),
        (403, "http_auth_failure"),
    ],
)
def test_http_auth_failures_are_distinct_from_single_request_failures(
    status_code: int,
    category: str,
) -> None:
    def post(_url: str, **_kwargs: Any) -> Response:
        return Response({}, status_code=status_code)

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient("private-token", post=post).fetch_lineup_score(
            [1, 2, 3, 4, 5],
            [6, 7, 8, 9, 10],
            as_of=datetime.now(timezone.utc),
        )

    assert caught.value.retryable is False
    assert caught.value.category == category


def test_stop_callback_prevents_a_second_graphql_request() -> None:
    data = fixture()
    stopped = False
    calls: list[str] = []

    def post(_url: str, **kwargs: Any) -> Response:
        nonlocal stopped
        operation = kwargs["json"]["operationName"]
        calls.append(operation)
        stopped = True
        key = {
            "HeroesMetaPositionsByWeek": "heroes_meta_positions",
            "GetHeroStatsByTime": "hero_stats_by_time_bracket",
            "Synergy": "synergy",
        }[operation]
        return Response(data["responses"][key])

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient(
            "private-token",
            post=post,
            stop_requested=lambda: stopped,
        ).fetch_lineup_score(
            data["radiant_heroes"],
            data["dire_heroes"],
            as_of=datetime.now(timezone.utc),
        )

    assert calls == ["HeroesMetaPositionsByWeek"]
    assert caught.value.retryable is False
    assert caught.value.category == "request_cancelled"


def test_graphql_auth_failure_is_not_suppressed_for_partial_requests() -> None:
    def post(_url: str, **_kwargs: Any) -> Response:
        return Response(
            {
                "errors": [
                    {
                        "message": "sensitive auth detail",
                        "extensions": {"code": "UNAUTHENTICATED"},
                    }
                ]
            }
        )

    client = StratzRoshClient("private-token", post=post)
    with pytest.raises(StratzRoshError) as caught:
        client._request(
            {"query": "query Test { test }", "operation_name": "Test"},
            allow_partial=True,
        )

    assert caught.value.category == "graphql_auth_failure"
    assert "sensitive auth detail" not in str(caught.value)


@pytest.mark.parametrize(
    ("code", "retryable", "category"),
    [
        ("RATE_LIMITED", True, "graphql_rate_limited"),
        ("INTERNAL_SERVER_ERROR", True, "graphql_internal_server_error"),
        ("UNAUTHENTICATED", False, "graphql_auth_failure"),
        ("FORBIDDEN", False, "graphql_auth_failure"),
        ("PERMISSION_DENIED", False, "graphql_auth_failure"),
        ("BAD_USER_INPUT", False, "graphql_failure"),
    ],
)
def test_graphql_retry_policy_uses_only_safe_extension_codes(
    code: str,
    retryable: bool,
    category: str,
) -> None:
    def post(_url: str, **_kwargs: Any) -> Response:
        return Response(
            {
                "errors": [
                    {
                        "message": "sensitive upstream detail",
                        "extensions": {"code": code},
                    }
                ]
            }
        )

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient("private-token", post=post).fetch_lineup_score(
            [1, 2, 3, 4, 5],
            [6, 7, 8, 9, 10],
            as_of=datetime.now(timezone.utc),
        )

    assert caught.value.retryable is retryable
    assert caught.value.category == category
    assert "sensitive upstream detail" not in str(caught.value)


def test_full_player_coverage_persists_both_curves_and_identity_evidence() -> None:
    data = fixture()
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    highlight = next(
        row
        for row in data["radiant_player_highlights"]
        if row is not None
    )
    highlights = [highlight] * 10

    def post(_url: str, **kwargs: Any) -> Response:
        operation = kwargs["json"]["operationName"]
        if operation == "PlayerHeroHighlights":
            return Response(
                {
                    "data": {
                        "plus": {
                            f"player_{index}": highlight
                            for index, highlight in enumerate(highlights)
                        }
                    }
                }
            )
        key = {
            "HeroesMetaPositionsByWeek": "heroes_meta_positions",
            "GetHeroStatsByTime": "hero_stats_by_time_bracket",
            "Synergy": "synergy",
        }[operation]
        return Response(data["responses"][key])

    identity = identity_evidence(data, started - timedelta(seconds=1))
    result = StratzRoshClient(
        "private-token", post=post, clock=lambda: started + timedelta(seconds=1)
    ).fetch_lineup_score(
        data["radiant_heroes"],
        data["dire_heroes"],
        radiant_player_ids=[100, 101, 102, 103, 104],
        dire_player_ids=[200, 201, 202, 203, 204],
        player_identity_evidence=identity,
        as_of=started,
    )

    assert result.scoring_mode == "player_adjusted"
    assert result.stake_cap == 1.0
    assert result.evidence["pure_minute_table"]
    assert result.evidence["minute_table"]
    assert result.evidence["player_identity_evidence"] == {
        **identity,
        "fetched_at": identity["fetched_at"].isoformat(),
    }


def test_tampered_player_identity_evidence_falls_back_to_pure() -> None:
    data = fixture()
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    player_calls = 0

    def post(_url: str, **kwargs: Any) -> Response:
        nonlocal player_calls
        operation = kwargs["json"]["operationName"]
        if operation == "PlayerHeroHighlights":
            player_calls += 1
            raise AssertionError("tampered identity must not query player stats")
        key = {
            "HeroesMetaPositionsByWeek": "heroes_meta_positions",
            "GetHeroStatsByTime": "hero_stats_by_time_bracket",
            "Synergy": "synergy",
        }[operation]
        return Response(data["responses"][key])

    tampered = identity_evidence(data, started - timedelta(seconds=1))
    tampered["evidence_hash"] = "d" * 64
    result = StratzRoshClient(
        "private-token", post=post, clock=lambda: started + timedelta(seconds=1)
    ).fetch_lineup_score(
        data["radiant_heroes"],
        data["dire_heroes"],
        radiant_player_ids=[100, 101, 102, 103, 104],
        dire_player_ids=[200, 201, 202, 203, 204],
        player_identity_evidence=tampered,
        as_of=started,
    )

    assert player_calls == 0
    assert result.scoring_mode == "pure"
    assert result.player_coverage_count == 0
    assert result.evidence["player_slots"][0]["fallback_reason"] == (
        "player_identity_evidence_invalid"
    )
