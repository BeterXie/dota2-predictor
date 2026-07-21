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


class Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

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
