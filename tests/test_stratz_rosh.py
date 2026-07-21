import json
from pathlib import Path

import pytest

from prematch.stratz_rosh import (
    WEEK_SECONDS,
    build_rosh_match_context,
    extract_rosh_picks_from_match,
    build_player_highlights_query,
    build_rosh_query_requests,
    calculate_player_impact,
    normalize_player_hero_highlight,
    normalize_player_highlights_response,
    normalize_rosh_analysis,
    score_rosh_lineups,
    score_rosh_picks,
)


@pytest.fixture(scope="module")
def rosh_fixture() -> dict:
    fixture_path = Path(__file__).parent / "fixtures" / "stratz-rosh.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def test_builds_current_week_immortal_rosh_queries(rosh_fixture: dict) -> None:
    week = rosh_fixture["week"]
    hero_ids = [*rosh_fixture["radiant_heroes"], *rosh_fixture["dire_heroes"]]

    requests = build_rosh_query_requests(hero_ids, week)

    assert requests["heroes_meta_positions"]["variables"] == {
        "bracketBasicIds": "DIVINE_IMMORTAL",
        "week": week,
        "heroIds": hero_ids,
    }
    assert "POSITION_5" in requests["heroes_meta_positions"]["query"]
    assert "groupByTime: true" in requests["hero_stats_by_time_bracket"]["query"]
    assert "minTime: 20" in requests["hero_stats_by_time_bracket"]["query"]
    assert "maxTime: 62" in requests["hero_stats_by_time_bracket"]["query"]
    assert requests["synergy"]["variables"] == {
        "bracketBasicIds": "DIVINE_IMMORTAL",
        "matchLimit": 0,
        "take": 200,
        "currentWeek": week,
        "previousWeek1": week - WEEK_SECONDS,
        "previousWeek2": week - 2 * WEEK_SECONDS,
        "previousWeek3": week - 3 * WEEK_SECONDS,
        "heroIds": hero_ids,
    }
    assert "matchUp_Prev_Week_4" in requests["synergy"]["query"]


def test_normalizes_graphql_envelopes_and_scores_position_tempo_synergy(
    rosh_fixture: dict,
) -> None:
    analysis = normalize_rosh_analysis(rosh_fixture["responses"])

    result = score_rosh_lineups(
        rosh_fixture["radiant_heroes"],
        rosh_fixture["dire_heroes"],
        analysis,
    )

    assert sorted(analysis["heroes_meta_positions"]) == [
        "heroesPos_1",
        "heroesPos_2",
        "heroesPos_3",
        "heroesPos_4",
        "heroesPos_5",
    ]
    assert result["pure_lineup_score"] == pytest.approx(8.7)
    assert result["player_adjusted_lineup_score"] == result["pure_lineup_score"]
    assert result["pure_minute_table"] == [
        {
            "minute": 20,
            "time_start": 20,
            "time_end": 21,
            "advantage_side": "radiant",
            "advantage_percent": 12.7,
            "radiant_advantage": 12.7,
            "dire_advantage": 0.0,
            "match_percentage": 33.3,
            "win_rate_graph": 12.7,
            "hero_adjustment": 8.7,
            "hero_base_adjustment": 6.7,
            "hero_tempo_adjustment": 2.0,
            "synergy_adjustment": 4.0,
            "player_adjustment": 0.0,
        },
        {
            "minute": 21,
            "time_start": 20,
            "time_end": 22,
            "advantage_side": "radiant",
            "advantage_percent": 10.7,
            "radiant_advantage": 10.7,
            "dire_advantage": 0.0,
            "match_percentage": 33.3,
            "win_rate_graph": 10.7,
            "hero_adjustment": 6.7,
            "hero_base_adjustment": 6.7,
            "hero_tempo_adjustment": 0.0,
            "synergy_adjustment": 4.0,
            "player_adjustment": 0.0,
        },
        {
            "minute": 22,
            "time_start": 21,
            "time_end": 23,
            "advantage_side": "radiant",
            "advantage_percent": 8.7,
            "radiant_advantage": 8.7,
            "dire_advantage": 0.0,
            "match_percentage": 33.3,
            "win_rate_graph": 8.7,
            "hero_adjustment": 4.7,
            "hero_base_adjustment": 6.7,
            "hero_tempo_adjustment": -2.0,
            "synergy_adjustment": 4.0,
            "player_adjustment": 0.0,
        },
    ]


def test_player_highlight_query_and_response_normalization() -> None:
    players = [
        {"steamAccountId": 100, "heroId": 1, "isAnonymous": False},
        {"steamAccountId": 200, "heroId": 2, "isAnonymous": True},
        {"steamAccountId": None, "heroId": 3},
    ]
    request = build_player_highlights_query(players)
    raw_highlight = {
        "matchCount": 30,
        "winCount": 18,
        "matchCountLastMonth": 10,
        "winCountLastMonth": 7,
        "impLastMonth": 10,
    }

    normalized = normalize_player_highlights_response(
        request,
        {"data": {"plus": {"player_0": raw_highlight}}},
    )

    assert request["variables"] == {"player_0SteamAccountId": 100, "player_0HeroId": 1}
    assert "player_0: playerHeroHighlight" in request["query"]
    assert request["fallback_reasons"] == {
        1: "player_is_anonymous",
        2: "player_not_selected",
    }
    assert normalized[0]["recentWindow"] == "last_month"
    assert normalized[0]["winRate"] == 60.0
    assert normalized[0]["recentWinRate"] == 70.0
    assert calculate_player_impact(normalized[0]) == pytest.approx(1.48)

    fixture_analysis = {
        "heroes_meta_positions": {},
        "hero_stats_by_time_bracket": {},
        "synergy": {},
    }
    # Normalized highlights are valid scorer inputs and must not lose their
    # selected recent window when crossing the client/scorer boundary.
    result = score_rosh_lineups(
        [1, 2, 3, 4, 5],
        [6, 7, 8, 9, 10],
        fixture_analysis,
        radiant_player_highlights=[normalized[0], None, None, None, None],
        dire_player_highlights=[None] * 5,
    )
    assert result["player_analysis"]["radiantTotalImpact"] == pytest.approx(1.48)


def test_player_adjustment_and_missing_players_fall_back_to_pure(
    rosh_fixture: dict,
) -> None:
    analysis = normalize_rosh_analysis(rosh_fixture["responses"])
    adjusted = score_rosh_lineups(
        rosh_fixture["radiant_heroes"],
        rosh_fixture["dire_heroes"],
        analysis,
        radiant_player_highlights=rosh_fixture["radiant_player_highlights"],
        dire_player_highlights=rosh_fixture["dire_player_highlights"],
    )
    fallback = score_rosh_lineups(
        rosh_fixture["radiant_heroes"],
        rosh_fixture["dire_heroes"],
        analysis,
        radiant_player_highlights=[None] * 5,
        dire_player_highlights=[None] * 5,
    )

    radiant = normalize_player_hero_highlight(
        rosh_fixture["radiant_player_highlights"][0]
    )
    dire = normalize_player_hero_highlight(rosh_fixture["dire_player_highlights"][0])
    assert calculate_player_impact(radiant) == pytest.approx(1.48)
    assert calculate_player_impact(dire) == pytest.approx(-0.68)
    assert adjusted["player_analysis"]["netAdjustment"] == 0.4
    assert adjusted["player_adjusted_lineup_score"] == pytest.approx(9.1)
    assert adjusted["used_player_adjustment"] is True
    assert fallback["fell_back_to_pure_score"] is True
    assert fallback["player_adjusted_lineup_score"] == fallback["pure_lineup_score"]


def test_selected_failed_players_are_distinct_from_resolved_players(
    rosh_fixture: dict,
) -> None:
    statuses = [
        {
            "selected": True,
            "resolved": False,
            "fallback_reason": "player_stats_request_failed",
        }
        for _ in range(10)
    ]
    result = score_rosh_lineups(
        rosh_fixture["radiant_heroes"],
        rosh_fixture["dire_heroes"],
        normalize_rosh_analysis(rosh_fixture["responses"]),
        radiant_player_highlights=[None] * 5,
        dire_player_highlights=[None] * 5,
        player_slot_statuses=statuses,
    )

    assert result["player_analysis"]["selectedCount"] == 10
    assert result["player_analysis"]["resolvedCount"] == 0
    assert result["player_analysis"]["fallbackCount"] == 10


def test_player_team_and_synergy_caps_are_preserved(rosh_fixture: dict) -> None:
    extreme_positive = {
        "matchCount": 1000,
        "winCount": 1000,
        "matchCountLastMonth": 1000,
        "winCountLastMonth": 1000,
        "impLastMonth": 1000,
    }
    extreme_negative = {
        "matchCount": 1000,
        "winCount": 0,
        "matchCountLastMonth": 1000,
        "winCountLastMonth": 0,
        "impLastMonth": -1000,
    }
    assert calculate_player_impact(
        normalize_player_hero_highlight(extreme_positive)
    ) == 1.5
    assert calculate_player_impact(
        normalize_player_hero_highlight(extreme_negative)
    ) == -1.5

    analysis = normalize_rosh_analysis(rosh_fixture["responses"])
    analysis["synergy"] = {
        "matchUp_Prev_Week_1": [
            {
                "heroId": 1,
                "with": [
                    {"heroId2": 2, "synergy": 1000.0, "matchCount": 100}
                ],
                "vs": [],
            }
        ]
    }
    result = score_rosh_lineups(
        rosh_fixture["radiant_heroes"],
        rosh_fixture["dire_heroes"],
        analysis,
        radiant_player_highlights=[extreme_positive] * 5,
        dire_player_highlights=[extreme_negative] * 5,
    )
    assert result["player_analysis"]["netAdjustment"] == 2.5
    assert result["minute_table"][-1]["synergy_adjustment"] == 30.0


def test_missing_base_rows_and_minute_60_boundary_do_not_get_invented() -> None:
    analysis = {
        "heroes_meta_positions": {
            "heroesPos_1": [{"heroId": 1, "matchCount": 100, "winCount": 60}],
        },
        "hero_stats_by_time_bracket": {
            "heroStatsByTime_1": [
                {"heroId": 1, "time": 60, "winCount": 6, "matchCount": 10},
                {"heroId": 1, "time": 61, "winCount": 0, "matchCount": 0},
            ],
        },
        "synergy": {},
    }
    result = score_rosh_lineups(
        [1, 2, 3, 4, 5],
        [6, 7, 8, 9, 10],
        analysis,
    )

    assert [row["minute"] for row in result["minute_table"]] == [60]
    assert result["minute_table"][0]["time_end"] == 60


def test_total_score_is_not_capped_at_100() -> None:
    analysis = {
        "heroes_meta_positions": {
            "heroesPos_1": [
                {"heroId": 1, "matchCount": 1000, "winCount": 5000},
                {"heroId": 6, "matchCount": 1000, "winCount": 0},
            ],
        },
        "hero_stats_by_time_bracket": {
            "heroStatsByTime_1": [
                {"heroId": 1, "time": 60, "winCount": 10, "matchCount": 10},
                {"heroId": 6, "time": 60, "winCount": 0, "matchCount": 10},
            ],
        },
        "synergy": {},
    }
    result = score_rosh_lineups(
        [1, 2, 3, 4, 5],
        [6, 7, 8, 9, 10],
        analysis,
    )

    assert result["pure_lineup_score"] > 100.0


def test_historical_picks_sort_by_order_and_partial_picks_do_not_fallback() -> None:
    match = {
        "bracket": 8,
        "endDateTime": 1_800_000_000,
        "players": [
            {"heroId": 1, "position": "POSITION_1"},
            {"heroId": 2, "position": "POSITION_2"},
            {"heroId": 6, "position": "POSITION_1"},
            {"heroId": 7, "position": "POSITION_2"},
        ],
        "pickBans": [
            {"heroId": 2, "isPick": True, "isRadiant": True, "order": 4},
            {"heroId": 1, "isPick": True, "isRadiant": True, "order": 1},
            {"heroId": 6, "isPick": False, "isRadiant": False, "order": 2},
        ],
    }
    picks = extract_rosh_picks_from_match(match)
    context = build_rosh_match_context(123, {"data": {"match": match}})
    scored = score_rosh_picks(picks["radiant"], picks["dire"], {})

    assert [pick["heroId"] for pick in picks["radiant"]] == [1, 2]
    assert picks["dire"] == []
    assert context["hero_ids"] == [1, 2]
    assert scored["pure_minute_table"] == []


def test_historical_no_pick_rows_fall_back_to_player_order() -> None:
    players = [
        {"heroId": hero_id, "position": f"POSITION_{(index % 5) + 1}"}
        for index, hero_id in enumerate(range(1, 11))
    ]
    picks = extract_rosh_picks_from_match({"players": players, "pickBans": []})

    assert [pick["heroId"] for pick in picks["radiant"]] == [1, 2, 3, 4, 5]
    assert [pick["heroId"] for pick in picks["dire"]] == [6, 7, 8, 9, 10]
