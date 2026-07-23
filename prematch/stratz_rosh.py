"""STRATZ-backed Rosh draft scoring.

Ported from upndrey/dematus with the author's permission as reported by the
project owner. Upstream source:
https://github.com/upndrey/dematus/blob/0e1e6651dd932055dee69c4fb44435774f619793/app/Services/Stratz/StratzService.php

This module is deliberately transport-free. It builds GraphQL requests,
normalizes response payloads, and reproduces the upstream scoring arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


ROSH_MIN_TIME = 20
ROSH_MAX_TIME = 60
ROSH_GRAPH_WINDOW_RADIUS = 1
ROSH_HERO_BASE_PRIOR_MATCH_COUNT = 500
ROSH_HERO_TEMPO_PRIOR_MATCH_COUNT = 500
ROSH_HERO_TEMPO_WEIGHT = 0.35
ROSH_HERO_ADJUSTMENT_WEIGHT = 2.0
ROSH_SYNERGY_RELIABILITY_MATCH_COUNT = 100
ROSH_SYNERGY_ADJUSTMENT_CAP = 30.0
ROSH_PLAYER_IMPACT_CAP = 1.5
ROSH_TEAM_PLAYER_ADJUSTMENT_CAP = 2.5

ROSH_BRACKET = "IMMORTAL"
ROSH_BRACKET_BASIC = "DIVINE_IMMORTAL"
WEEK_SECONDS = 604_800

ROSH_BRACKETS = {
    1: "HERALD",
    2: "GUARDIAN",
    3: "CRUSADER",
    4: "ARCHON",
    5: "LEGEND",
    6: "ANCIENT",
    7: "DIVINE",
    8: "IMMORTAL",
}
ROSH_BASIC_BRACKETS = {
    1: "HERALD_GUARDIAN",
    2: "HERALD_GUARDIAN",
    3: "CRUSADER_ARCHON",
    4: "CRUSADER_ARCHON",
    5: "LEGEND_ANCIENT",
    6: "LEGEND_ANCIENT",
    7: "DIVINE_IMMORTAL",
    8: "DIVINE_IMMORTAL",
}


MATCH_PICKS_BANS_QUERY = """\
query GetMatchPicksBans($matchId: Long!) {
  match(id: $matchId) {
    id
    gameMode
    regionId
    durationSeconds
    endDateTime
    lobbyType
    didRadiantWin
    radiantKills
    direKills
    bracket
    radiantTeam { id name }
    direTeam { id name }
    league { id displayName }
    players { heroId position steamAccountId }
    pickBans {
      heroId
      order
      isPick
      isRadiant
      bannedHeroId
      wasBannedSuccessfully
    }
  }
}
"""


HEROES_META_POSITIONS_QUERY = """\
query HeroesMetaPositionsByWeek($bracketBasicIds: [RankBracketBasicEnum], $week: Long, $heroIds: [Short]) {
  heroStats {
    heroesPos_1: stats(positionIds: [POSITION_1], bracketBasicIds: $bracketBasicIds, week: $week, heroIds: $heroIds) { heroId matchCount winCount }
    heroesPos_2: stats(positionIds: [POSITION_2], bracketBasicIds: $bracketBasicIds, week: $week, heroIds: $heroIds) { heroId matchCount winCount }
    heroesPos_3: stats(positionIds: [POSITION_3], bracketBasicIds: $bracketBasicIds, week: $week, heroIds: $heroIds) { heroId matchCount winCount }
    heroesPos_4: stats(positionIds: [POSITION_4], bracketBasicIds: $bracketBasicIds, week: $week, heroIds: $heroIds) { heroId matchCount winCount }
    heroesPos_5: stats(positionIds: [POSITION_5], bracketBasicIds: $bracketBasicIds, week: $week, heroIds: $heroIds) { heroId matchCount winCount }
    heroes: stats(bracketBasicIds: $bracketBasicIds, week: $week, heroIds: $heroIds) { heroId matchCount winCount }
  }
}
"""

HERO_STATS_BY_TIME_QUERY = """\
query GetHeroStatsByTime($bracketBasicIds: [RankBracketBasicEnum], $week: Long, $heroIds: [Short]) {
  heroStats {
    heroStatsByTime_1: stats(bracketBasicIds: $bracketBasicIds, positionIds: [POSITION_1], groupByTime: true, minTime: 20, maxTime: 62, week: $week, heroIds: $heroIds) { heroId time winCount matchCount }
    heroStatsByTime_2: stats(bracketBasicIds: $bracketBasicIds, positionIds: [POSITION_2], groupByTime: true, minTime: 20, maxTime: 62, week: $week, heroIds: $heroIds) { heroId time winCount matchCount }
    heroStatsByTime_3: stats(bracketBasicIds: $bracketBasicIds, positionIds: [POSITION_3], groupByTime: true, minTime: 20, maxTime: 62, week: $week, heroIds: $heroIds) { heroId time winCount matchCount }
    heroStatsByTime_4: stats(bracketBasicIds: $bracketBasicIds, positionIds: [POSITION_4], groupByTime: true, minTime: 20, maxTime: 62, week: $week, heroIds: $heroIds) { heroId time winCount matchCount }
    heroStatsByTime_5: stats(bracketBasicIds: $bracketBasicIds, positionIds: [POSITION_5], groupByTime: true, minTime: 20, maxTime: 62, week: $week, heroIds: $heroIds) { heroId time winCount matchCount }
  }
}
"""

SYNERGY_QUERY = """\
query Synergy(
  $bracketBasicIds: [RankBracketBasicEnum]
  $matchLimit: Int
  $take: Int
  $currentWeek: Long!
  $previousWeek1: Long!
  $previousWeek2: Long!
  $previousWeek3: Long!
  $heroIds: [Short]
) {
  heroStats {
    matchUp_Prev_Week_1: matchUp(bracketBasicIds: $bracketBasicIds, matchLimit: $matchLimit, take: $take, week: $currentWeek, heroIds: $heroIds) { heroId vs { heroId2 synergy matchCount } with { heroId2 synergy matchCount } }
    matchUp_Prev_Week_2: matchUp(bracketBasicIds: $bracketBasicIds, matchLimit: $matchLimit, take: $take, week: $previousWeek1, heroIds: $heroIds) { heroId vs { heroId2 synergy matchCount } with { heroId2 synergy matchCount } }
    matchUp_Prev_Week_3: matchUp(bracketBasicIds: $bracketBasicIds, matchLimit: $matchLimit, take: $take, week: $previousWeek2, heroIds: $heroIds) { heroId vs { heroId2 synergy matchCount } with { heroId2 synergy matchCount } }
    matchUp_Prev_Week_4: matchUp(bracketBasicIds: $bracketBasicIds, matchLimit: $matchLimit, take: $take, week: $previousWeek3, heroIds: $heroIds) { heroId vs { heroId2 synergy matchCount } with { heroId2 synergy matchCount } }
  }
}
"""

PLAYER_HIGHLIGHT_FIELDS = """\
      lastPlayed
      winCount
      matchCount
      impAllTime
      winCountLastMonth
      matchCountLastMonth
      impLastMonth
      winCountLastSixMonths
      matchCountLastSixMonths
      impLastSixMonths"""


def build_rosh_query_requests(
    hero_ids: Sequence[int],
    week: int,
    bracket_basic_id: str = ROSH_BRACKET_BASIC,
) -> dict[str, dict[str, Any]]:
    """Build the three GraphQL requests used by the upstream Rosh flow."""
    heroes = list(dict.fromkeys(int(hero_id) for hero_id in hero_ids))
    common = {
        "bracketBasicIds": bracket_basic_id,
        "week": int(week),
        "heroIds": heroes,
    }
    return {
        "heroes_meta_positions": {
            "operation_name": "HeroesMetaPositionsByWeek",
            "query": HEROES_META_POSITIONS_QUERY,
            "variables": dict(common),
        },
        "hero_stats_by_time_bracket": {
            "operation_name": "GetHeroStatsByTime",
            "query": HERO_STATS_BY_TIME_QUERY,
            "variables": dict(common),
        },
        "synergy": {
            "operation_name": "Synergy",
            "query": SYNERGY_QUERY,
            "variables": {
                "bracketBasicIds": bracket_basic_id,
                "matchLimit": 0,
                "take": 200,
                "currentWeek": int(week),
                "previousWeek1": int(week) - WEEK_SECONDS,
                "previousWeek2": int(week) - (2 * WEEK_SECONDS),
                "previousWeek3": int(week) - (3 * WEEK_SECONDS),
                "heroIds": heroes,
            },
        },
    }


def build_rosh_match_query_request(match_id: int) -> dict[str, Any]:
    """Build the historical GetMatchPicksBans request used upstream."""
    if not _is_int(match_id) or match_id <= 0:
        raise ValueError("match_id must be a positive integer")
    return {
        "operation_name": "GetMatchPicksBans",
        "query": MATCH_PICKS_BANS_QUERY,
        "variables": {"matchId": match_id},
    }


def build_rosh_match_context(
    match_id: int,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize historical match context and reproduce upstream pick rules."""
    request = build_rosh_match_query_request(match_id)
    data = response.get("data", response)
    match = data.get("match", {}) if isinstance(data, Mapping) else {}
    if not isinstance(match, Mapping):
        raise ValueError("STRATZ Rosh match response does not contain match data")
    if match.get("id") != match_id:
        raise ValueError("STRATZ Rosh match response ID does not match the request")
    bracket_value = match.get("bracket")
    week = match.get("endDateTime")
    if not _is_int(bracket_value) or bracket_value not in ROSH_BRACKETS:
        raise ValueError("STRATZ Rosh match response has an unsupported bracket")
    if not _is_int(week):
        raise ValueError("STRATZ Rosh match response does not contain endDateTime")
    picks = extract_rosh_picks_from_match(match)
    return {
        "match": dict(match),
        "request": request,
        "bracket": ROSH_BRACKETS[bracket_value],
        "bracket_basic": ROSH_BASIC_BRACKETS[bracket_value],
        "week": week,
        "radiant_picks": picks["radiant"],
        "dire_picks": picks["dire"],
        "hero_ids": list(
            dict.fromkeys(
                pick["heroId"]
                for pick in (*picks["radiant"], *picks["dire"])
            )
        ),
    }


def extract_rosh_picks_from_match(
    match: Mapping[str, Any],
) -> dict[str, list[dict[str, int]]]:
    """Parse position IDs, sort picks by draft order, and retain no-pick fallback."""
    players = match.get("players", [])
    player_rows = players if isinstance(players, list) else []
    positions: dict[int, int] = {}
    for player in player_rows:
        if not isinstance(player, Mapping):
            continue
        hero_id = player.get("heroId")
        position_id = extract_rosh_position_id(player.get("position"))
        if _is_int(hero_id) and position_id is not None:
            positions[hero_id] = position_id

    pick_rows: list[dict[str, Any]] = []
    raw_pick_bans = match.get("pickBans", [])
    for pick_ban in raw_pick_bans if isinstance(raw_pick_bans, list) else []:
        if not isinstance(pick_ban, Mapping) or pick_ban.get("isPick") is not True:
            continue
        hero_id = pick_ban.get("heroId")
        position_id = positions.get(hero_id) if _is_int(hero_id) else None
        if not _is_int(hero_id) or position_id is None:
            continue
        order = pick_ban.get("order")
        pick_rows.append(
            {
                "heroId": hero_id,
                "positionId": position_id,
                "isRadiant": bool(pick_ban.get("isRadiant")),
                "order": int(order) if _is_number(order) else 2**63 - 1,
            }
        )
    pick_rows.sort(key=lambda row: row["order"])
    radiant = [
        {"heroId": row["heroId"], "positionId": row["positionId"]}
        for row in pick_rows
        if row["isRadiant"]
    ]
    dire = [
        {"heroId": row["heroId"], "positionId": row["positionId"]}
        for row in pick_rows
        if not row["isRadiant"]
    ]
    if radiant or dire:
        return {"radiant": radiant, "dire": dire}

    fallback: list[dict[str, int]] = []
    for player in player_rows:
        if not isinstance(player, Mapping):
            continue
        hero_id = player.get("heroId")
        position_id = extract_rosh_position_id(player.get("position"))
        if _is_int(hero_id) and position_id is not None:
            fallback.append({"heroId": hero_id, "positionId": position_id})
    return {"radiant": fallback[:5], "dire": fallback[5:10]}


def extract_rosh_position_id(position: Any) -> int | None:
    if not isinstance(position, str) or "POSITION_" not in position:
        return None
    suffix = position.split("POSITION_", 1)[1]
    if not suffix.isdigit():
        return None
    position_id = int(suffix)
    return position_id if 1 <= position_id <= 5 else None


def position_ordered_rosh_heroes(
    picks: Sequence[Mapping[str, Any]],
) -> tuple[int, ...]:
    """Adapt explicit upstream position picks to score_rosh_lineups' slot API."""
    by_position = {
        pick.get("positionId"): pick.get("heroId")
        for pick in picks
        if _is_int(pick.get("positionId")) and _is_int(pick.get("heroId"))
    }
    if set(by_position) != {1, 2, 3, 4, 5}:
        raise ValueError("Rosh historical picks must resolve all five positions")
    return tuple(int(by_position[position]) for position in range(1, 6))


def build_player_highlights_query(
    players: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build dematus' aliased playerHeroHighlight batch request."""
    definitions: list[str] = []
    rows: list[str] = []
    variables: dict[str, int] = {}
    aliases: dict[str, int] = {}
    fallback_reasons: dict[int, str] = {}

    for index, player in enumerate(players):
        steam_account_id = player.get("steamAccountId")
        hero_id = player.get("heroId")
        if not _is_int(steam_account_id):
            fallback_reasons[index] = "player_not_selected"
            continue
        if player.get("isAnonymous") is True:
            fallback_reasons[index] = "player_is_anonymous"
            continue
        if not _is_int(hero_id):
            fallback_reasons[index] = "hero_not_selected"
            continue

        alias = f"player_{index}"
        definitions.extend(
            (
                f"${alias}SteamAccountId: Long!",
                f"${alias}HeroId: Short!",
            )
        )
        rows.append(
            f"    {alias}: playerHeroHighlight("
            f"steamAccountId: ${alias}SteamAccountId, "
            f"heroId: ${alias}HeroId) {{\n{PLAYER_HIGHLIGHT_FIELDS}\n    }}"
        )
        variables[f"{alias}SteamAccountId"] = int(steam_account_id)
        variables[f"{alias}HeroId"] = int(hero_id)
        aliases[alias] = index

    query = ""
    if rows:
        joined_rows = "\n".join(rows)
        query = (
            f"query PlayerHeroHighlights({', '.join(definitions)}) {{\n"
            "  plus {\n"
            f"{joined_rows}\n"
            "  }\n"
            "}"
        )
    return {
        "operation_name": "PlayerHeroHighlights",
        "query": query,
        "variables": variables,
        "aliases": aliases,
        "fallback_reasons": fallback_reasons,
    }


def normalize_rosh_analysis(
    responses: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Remove GraphQL envelopes from the three Rosh analysis responses."""
    normalized: dict[str, dict[str, Any]] = {}
    for key in (
        "heroes_meta_positions",
        "hero_stats_by_time_bracket",
        "synergy",
    ):
        response = responses.get(key, {})
        data = response.get("data", response)
        hero_stats = data.get("heroStats", {}) if isinstance(data, Mapping) else {}
        normalized[key] = dict(hero_stats) if isinstance(hero_stats, Mapping) else {}
    return normalized


def normalize_player_highlights_response(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[int, dict[str, Any] | None]:
    """Map an aliased batch response back to zero-based player slots."""
    data = response.get("data", response)
    plus = data.get("plus", {}) if isinstance(data, Mapping) else {}
    result: dict[int, dict[str, Any] | None] = {}
    for alias, index in request.get("aliases", {}).items():
        raw = plus.get(alias) if isinstance(plus, Mapping) else None
        result[int(index)] = (
            normalize_player_hero_highlight(raw) if isinstance(raw, Mapping) else None
        )
    return result


def normalize_player_hero_highlight(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize STRATZ playerHeroHighlight using dematus' recent-window rule."""
    match_count = max(0, _as_int(raw.get("matchCount")))
    win_count = max(0, _as_int(raw.get("winCount")))
    month_matches = max(0, _as_int(raw.get("matchCountLastMonth")))
    month_wins = max(0, _as_int(raw.get("winCountLastMonth")))
    six_month_matches = max(0, _as_int(raw.get("matchCountLastSixMonths")))
    six_month_wins = max(0, _as_int(raw.get("winCountLastSixMonths")))

    recent_window = "all_time"
    recent_matches = match_count
    recent_wins = win_count
    recent_imp = _optional_float(raw.get("impAllTime"))
    if month_matches > 0:
        recent_window = "last_month"
        recent_matches = month_matches
        recent_wins = month_wins
        recent_imp = _optional_float(raw.get("impLastMonth"))
    elif six_month_matches > 0:
        recent_window = "last_six_months"
        recent_matches = six_month_matches
        recent_wins = six_month_wins
        recent_imp = _optional_float(raw.get("impLastSixMonths"))

    return {
        "lastPlayed": _as_int(raw.get("lastPlayed")) if _is_number(raw.get("lastPlayed")) else None,
        "matchCount": match_count,
        "winCount": win_count,
        "winRate": _win_rate(win_count, match_count),
        "impAllTime": _round_optional(raw.get("impAllTime"), 2),
        "lastMonth": {
            "matchCount": month_matches,
            "winCount": month_wins,
            "winRate": _win_rate(month_wins, month_matches),
            "imp": _round_optional(raw.get("impLastMonth"), 2),
        },
        "lastSixMonths": {
            "matchCount": six_month_matches,
            "winCount": six_month_wins,
            "winRate": _win_rate(six_month_wins, six_month_matches),
            "imp": _round_optional(raw.get("impLastSixMonths"), 2),
        },
        "recentWindow": recent_window,
        "recentMatchCount": recent_matches,
        "recentWinCount": recent_wins,
        "recentWinRate": _win_rate(recent_wins, recent_matches),
        "recentImp": _php_round(recent_imp, 2) if recent_imp is not None else None,
    }


def calculate_player_impact(player_hero_stats: Mapping[str, Any] | None) -> float:
    """Calculate one player's hero correction using the upstream formula."""
    if player_hero_stats is None:
        return 0.0
    match_count = max(0, _as_int(player_hero_stats.get("matchCount")))
    win_rate = player_hero_stats.get("winRate")
    if not _is_number(win_rate) or match_count == 0:
        return 0.0

    recent_match_count = max(0, _as_int(player_hero_stats.get("recentMatchCount")))
    recent_win_rate = player_hero_stats.get("recentWinRate")
    overall_diff = float(win_rate) - 50.0
    recent_diff = (
        float(recent_win_rate) - 50.0
        if _is_number(recent_win_rate)
        else overall_diff
    )
    overall_confidence = _clamp(match_count / 30, 0.0, 1.0)
    recent_confidence = _clamp(recent_match_count / 10, 0.0, 1.0)
    imp_value = player_hero_stats.get("recentImp")
    if not _is_number(imp_value):
        imp_value = player_hero_stats.get("impAllTime", 0.0)
    imp_score = _clamp(float(imp_value or 0.0) / 20.0, -1.2, 1.2)
    impact = (
        (overall_diff * overall_confidence * 0.03)
        + (recent_diff * recent_confidence * 0.05)
        + (imp_score * 0.35)
    )
    return _php_round(_clamp(impact, -ROSH_PLAYER_IMPACT_CAP, ROSH_PLAYER_IMPACT_CAP), 2)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_int(value: Any) -> int:
    return int(value) if _is_number(value) else 0


def _optional_float(value: Any) -> float | None:
    return float(value) if _is_number(value) else None


def _round_optional(value: Any, digits: int) -> float | None:
    return _php_round(float(value), digits) if _is_number(value) else None


def _php_round(value: float, digits: int = 0) -> float:
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _win_rate(win_count: int, match_count: int) -> float | None:
    if match_count <= 0:
        return None
    return _php_round((win_count / match_count) * 100, 1)


def score_rosh_lineups(
    radiant_heroes: Sequence[int],
    dire_heroes: Sequence[int],
    analysis: Mapping[str, Mapping[str, Any]],
    *,
    radiant_player_highlights: Sequence[Mapping[str, Any] | None] | None = None,
    dire_player_highlights: Sequence[Mapping[str, Any] | None] | None = None,
    player_slot_statuses: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score two position-ordered lineups and return pure and adjusted curves."""
    if len(radiant_heroes) != 5 or len(dire_heroes) != 5:
        raise ValueError("Rosh scoring requires five position-ordered heroes per side")
    if (radiant_player_highlights is None) != (dire_player_highlights is None):
        raise ValueError("player highlights must be provided for both sides or neither")
    if radiant_player_highlights is not None and (
        len(radiant_player_highlights) != 5 or len(dire_player_highlights or ()) != 5
    ):
        raise ValueError("player highlights must contain five slots per side")

    radiant_picks = [
        {"heroId": int(hero_id), "positionId": index}
        for index, hero_id in enumerate(radiant_heroes, start=1)
    ]
    dire_picks = [
        {"heroId": int(hero_id), "positionId": index}
        for index, hero_id in enumerate(dire_heroes, start=1)
    ]
    return score_rosh_picks(
        radiant_picks,
        dire_picks,
        analysis,
        radiant_player_highlights=radiant_player_highlights,
        dire_player_highlights=dire_player_highlights,
        player_slot_statuses=player_slot_statuses,
    )


def score_rosh_picks(
    radiant_picks: Sequence[Mapping[str, Any]],
    dire_picks: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Mapping[str, Any]],
    *,
    radiant_player_highlights: Sequence[Mapping[str, Any] | None] | None = None,
    dire_player_highlights: Sequence[Mapping[str, Any] | None] | None = None,
    player_slot_statuses: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score explicit position picks, including upstream historical partials."""
    normalized_radiant = _normalize_rosh_picks(radiant_picks)
    normalized_dire = _normalize_rosh_picks(dire_picks)
    if (radiant_player_highlights is None) != (dire_player_highlights is None):
        raise ValueError("player highlights must be provided for both sides or neither")
    if radiant_player_highlights is not None and (
        len(radiant_player_highlights) != len(normalized_radiant)
        or len(dire_player_highlights or ()) != len(normalized_dire)
    ):
        raise ValueError("player highlights must align with explicit picks")
    player_analysis = _build_player_analysis(
        radiant_player_highlights,
        dire_player_highlights,
        player_slot_statuses,
    )
    pure_table = _build_minute_table(
        normalized_radiant,
        normalized_dire,
        analysis,
        player_adjustment=0.0,
    )
    adjusted_table = _build_minute_table(
        normalized_radiant,
        normalized_dire,
        analysis,
        player_adjustment=player_analysis["netAdjustment"],
    )

    pure_score = pure_table[-1]["win_rate_graph"] if pure_table else None
    adjusted_score = adjusted_table[-1]["win_rate_graph"] if adjusted_table else None
    return {
        "bracket": ROSH_BRACKET,
        "bracket_basic": ROSH_BRACKET_BASIC,
        "pure_lineup_score": pure_score,
        "player_adjusted_lineup_score": adjusted_score,
        "pure_minute_table": pure_table,
        "minute_table": adjusted_table,
        "player_analysis": player_analysis,
        "used_player_adjustment": bool(
            player_analysis["enabled"] and player_analysis["resolvedCount"] > 0
        ),
        "fell_back_to_pure_score": bool(
            player_analysis["enabled"] and player_analysis["resolvedCount"] == 0
        ),
    }


def _normalize_rosh_picks(
    picks: Sequence[Mapping[str, Any]],
) -> list[dict[str, int]]:
    normalized: list[dict[str, int]] = []
    for pick in picks:
        hero_id = pick.get("heroId")
        position_id = pick.get("positionId")
        if not _is_int(hero_id) or not _is_int(position_id):
            continue
        if hero_id <= 0 or not 1 <= position_id <= 5:
            continue
        normalized.append({"heroId": hero_id, "positionId": position_id})
    return normalized


def _build_player_analysis(
    radiant_highlights: Sequence[Mapping[str, Any] | None] | None,
    dire_highlights: Sequence[Mapping[str, Any] | None] | None,
    slot_statuses: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if radiant_highlights is None or dire_highlights is None:
        return {
            "enabled": False,
            "source": "plus.playerHeroHighlight",
            "selectedCount": 0,
            "resolvedCount": 0,
            "fallbackCount": 0,
            "radiantTotalImpact": 0.0,
            "direTotalImpact": 0.0,
            "netAdjustment": 0.0,
        }

    normalized_radiant = [_ensure_normalized_highlight(item) for item in radiant_highlights]
    normalized_dire = [_ensure_normalized_highlight(item) for item in dire_highlights]
    radiant_total = sum(calculate_player_impact(item) for item in normalized_radiant)
    dire_total = sum(calculate_player_impact(item) for item in normalized_dire)
    all_highlights = [*normalized_radiant, *normalized_dire]
    resolved_count = sum(item is not None for item in all_highlights)
    if slot_statuses is not None and len(slot_statuses) != 10:
        raise ValueError("player slot statuses must contain ten slots")
    selected_count = (
        sum(status.get("selected") is True for status in slot_statuses)
        if slot_statuses is not None
        else resolved_count
    )
    fallback_count = (
        sum(
            isinstance(status.get("fallback_reason"), str)
            and bool(str(status.get("fallback_reason")).strip())
            for status in slot_statuses
            if status.get("selected") is True
        )
        if slot_statuses is not None
        else 10 - resolved_count
    )
    net_adjustment = _php_round(
        _clamp(
            (radiant_total - dire_total) / 5,
            -ROSH_TEAM_PLAYER_ADJUSTMENT_CAP,
            ROSH_TEAM_PLAYER_ADJUSTMENT_CAP,
        ),
        1,
    )
    return {
        "enabled": True,
        "source": "plus.playerHeroHighlight",
        "selectedCount": selected_count,
        "resolvedCount": resolved_count,
        "fallbackCount": fallback_count,
        "radiantTotalImpact": _php_round(radiant_total, 2),
        "direTotalImpact": _php_round(dire_total, 2),
        "netAdjustment": net_adjustment,
    }


def _ensure_normalized_highlight(
    highlight: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if highlight is None:
        return None
    if "recentWindow" in highlight and "recentMatchCount" in highlight:
        return dict(highlight)
    return normalize_player_hero_highlight(highlight)


def _build_minute_table(
    radiant_picks: Sequence[Mapping[str, int]],
    dire_picks: Sequence[Mapping[str, int]],
    analysis: Mapping[str, Mapping[str, Any]],
    *,
    player_adjustment: float,
) -> list[dict[str, Any]]:
    position_data = _build_hero_position_data(
        analysis.get("heroes_meta_positions", {})
    )
    hero_base_adjustment = (
        _team_average_difference(
            _sum_position_effects(radiant_picks, position_data),
            len(radiant_picks),
            _sum_position_effects(dire_picks, position_data),
            len(dire_picks),
        )
        * ROSH_HERO_ADJUSTMENT_WEIGHT
    )
    graph = _build_computed_graph_data(
        analysis.get("hero_stats_by_time_bracket", {}),
        position_data,
    )
    if not graph:
        return []
    synergy_data = _build_synergy_data(analysis.get("synergy", {}))
    synergy_offset = _calculate_synergy_offset(
        radiant_picks,
        dire_picks,
        synergy_data,
    )

    minute_table: list[dict[str, Any]] = []
    for minute in sorted(graph):
        bucket = graph[minute]
        radiant_tempo_total = 0.0
        dire_tempo_total = 0.0
        minute_match_count = 0
        total_match_count = 0
        for pick in radiant_picks:
            stats = bucket["heroes"].get(pick["positionId"], {}).get(pick["heroId"])
            if stats is None:
                continue
            radiant_tempo_total += stats["tempo_effect"]
            minute_match_count += stats["match_count"]
            total_match_count += stats["total_match_count"]
        for pick in dire_picks:
            stats = bucket["heroes"].get(pick["positionId"], {}).get(pick["heroId"])
            if stats is None:
                continue
            dire_tempo_total += stats["tempo_effect"]
            minute_match_count += stats["match_count"]
            total_match_count += stats["total_match_count"]

        hero_tempo_adjustment = (
            _team_average_difference(
                radiant_tempo_total,
                len(radiant_picks),
                dire_tempo_total,
                len(dire_picks),
            )
            * ROSH_HERO_ADJUSTMENT_WEIGHT
        )
        hero_adjustment = hero_base_adjustment + hero_tempo_adjustment
        win_rate_graph = _php_round(
            hero_adjustment + synergy_offset + player_adjustment,
            1,
        )
        match_percentage = (
            _php_round((minute_match_count / total_match_count) * 100, 1)
            if total_match_count > 0
            else 0.0
        )
        minute_table.append(
            {
                "minute": bucket["time"],
                "time_start": bucket["time_start"],
                "time_end": bucket["time_end"],
                "advantage_side": (
                    "radiant" if win_rate_graph > 0 else "dire" if win_rate_graph < 0 else "even"
                ),
                "advantage_percent": _php_round(abs(win_rate_graph), 1),
                "radiant_advantage": _php_round(win_rate_graph, 1) if win_rate_graph > 0 else 0.0,
                "dire_advantage": _php_round(abs(win_rate_graph), 1) if win_rate_graph < 0 else 0.0,
                "match_percentage": match_percentage,
                "win_rate_graph": win_rate_graph,
                "hero_adjustment": _php_round(hero_adjustment, 1),
                "hero_base_adjustment": _php_round(hero_base_adjustment, 1),
                "hero_tempo_adjustment": _php_round(hero_tempo_adjustment, 1),
                "synergy_adjustment": _php_round(synergy_offset, 1),
                "player_adjustment": _php_round(player_adjustment, 1),
            }
        )
    return minute_table


def _build_hero_position_data(
    heroes_meta_positions: Mapping[str, Any],
) -> dict[int, dict[int, dict[str, float | int]]]:
    result: dict[int, dict[int, dict[str, float | int]]] = {}
    for position_id in range(1, 6):
        rows = heroes_meta_positions.get(f"heroesPos_{position_id}", [])
        for row in rows if isinstance(rows, list) else []:
            hero_id = row.get("heroId")
            match_count = row.get("matchCount")
            win_count = row.get("winCount")
            if (
                not _is_int(hero_id)
                or not _is_number(match_count)
                or not _is_number(win_count)
                or int(match_count) <= 0
            ):
                continue
            matches = int(match_count)
            raw_win_rate_diff = ((int(win_count) / matches) * 100) - 50
            confidence = matches / (matches + ROSH_HERO_BASE_PRIOR_MATCH_COUNT)
            result.setdefault(position_id, {})[hero_id] = {
                "match_count": matches,
                "raw_win_rate_diff": raw_win_rate_diff,
                "base_effect": raw_win_rate_diff * confidence,
            }
    return result


def _sum_position_effects(
    picks: Sequence[Mapping[str, int]],
    position_data: Mapping[int, Mapping[int, Mapping[str, float | int]]],
) -> float:
    return sum(
        float(
            position_data.get(pick["positionId"], {})
            .get(pick["heroId"], {})
            .get("base_effect", 0.0)
        )
        for pick in picks
    )


def _team_average_difference(
    radiant_total: float,
    radiant_count: int,
    dire_total: float,
    dire_count: int,
) -> float:
    radiant_average = radiant_total / radiant_count if radiant_count > 0 else 0.0
    dire_average = dire_total / dire_count if dire_count > 0 else 0.0
    return radiant_average - dire_average


def _build_computed_graph_data(
    hero_stats_by_time: Mapping[str, Any],
    position_data: Mapping[int, Mapping[int, Mapping[str, float | int]]],
) -> dict[int, dict[str, Any]]:
    graph: dict[int, dict[str, Any]] = {}
    for position_id in range(1, 6):
        raw_rows = hero_stats_by_time.get(f"heroStatsByTime_{position_id}", [])
        rows = [
            row
            for row in raw_rows if isinstance(raw_rows, list)
            if isinstance(row, Mapping)
            and _is_int(row.get("heroId"))
            and _is_number(row.get("time"))
            and _is_number(row.get("winCount"))
            and _is_number(row.get("matchCount"))
        ]
        rows.sort(key=lambda row: (row["heroId"], row["time"]))
        normalized_rows: list[dict[str, int]] = []
        total_match_count_by_hero: dict[int, int] = {}
        for index, row in enumerate(rows):
            hero_id = int(row["heroId"])
            match_count = int(row["matchCount"])
            win_count = int(row["winCount"])
            next_row = rows[index + 1] if index + 1 < len(rows) else None
            same_hero_next = (
                next_row is not None and int(next_row.get("heroId", -1)) == hero_id
            )
            minute_matches = (
                max(0, match_count - int(next_row.get("matchCount", 0)))
                if same_hero_next
                else match_count
            )
            minute_wins = (
                max(0, win_count - int(next_row.get("winCount", 0)))
                if same_hero_next
                else win_count
            )
            total_match_count_by_hero[hero_id] = (
                total_match_count_by_hero.get(hero_id, 0) + minute_matches
            )
            normalized_rows.append(
                {
                    "heroId": hero_id,
                    "time": int(row["time"]),
                    "matchCount": minute_matches,
                    "winCount": minute_wins,
                }
            )

        for index, row in enumerate(normalized_rows):
            hero_id = row["heroId"]
            minute = row["time"]
            if minute < ROSH_MIN_TIME or minute > ROSH_MAX_TIME:
                continue
            base_win_rate_diff = (
                position_data.get(position_id, {})
                .get(hero_id, {})
                .get("raw_win_rate_diff")
            )
            if not _is_number(base_win_rate_diff):
                continue
            bucket = graph.setdefault(
                minute,
                {
                    "time": minute,
                    "time_start": max(ROSH_MIN_TIME, minute - ROSH_GRAPH_WINDOW_RADIUS),
                    "time_end": min(ROSH_MAX_TIME, minute + ROSH_GRAPH_WINDOW_RADIUS),
                    "heroes": {},
                },
            )
            start = max(0, index - ROSH_GRAPH_WINDOW_RADIUS)
            end = min(len(normalized_rows), index + ROSH_GRAPH_WINDOW_RADIUS + 1)
            window_rows = [
                candidate
                for candidate in normalized_rows[start:end]
                if candidate["heroId"] == hero_id
            ]
            window_matches = sum(candidate["matchCount"] for candidate in window_rows)
            window_wins = sum(candidate["winCount"] for candidate in window_rows)
            if window_matches <= 0:
                continue
            duration_diff = ((window_wins / window_matches) * 100) - 50
            confidence = window_matches / (
                window_matches + ROSH_HERO_TEMPO_PRIOR_MATCH_COUNT
            )
            bucket["heroes"].setdefault(position_id, {})[hero_id] = {
                "hero_id": hero_id,
                "tempo_effect": (
                    (duration_diff - float(base_win_rate_diff))
                    * confidence
                    * ROSH_HERO_TEMPO_WEIGHT
                ),
                "match_count": row["matchCount"],
                "total_match_count": total_match_count_by_hero[hero_id],
            }
    return graph


def _build_synergy_data(
    raw_synergy: Mapping[str, Any],
) -> dict[str, dict[int, dict[int, dict[str, float | int]]]]:
    with_data: dict[int, dict[int, dict[str, float | int]]] = {}
    vs_data: dict[int, dict[int, dict[str, float | int]]] = {}
    for week_index in range(1, 5):
        rows = raw_synergy.get(f"matchUp_Prev_Week_{week_index}", [])
        for row in rows if isinstance(rows, list) else []:
            hero_id = row.get("heroId") if isinstance(row, Mapping) else None
            if not _is_int(hero_id):
                continue
            for key, target in (("with", with_data), ("vs", vs_data)):
                entries = row.get(key, [])
                for entry in entries if isinstance(entries, list) else []:
                    hero_id_2 = entry.get("heroId2") if isinstance(entry, Mapping) else None
                    match_count = entry.get("matchCount") if isinstance(entry, Mapping) else None
                    synergy = entry.get("synergy") if isinstance(entry, Mapping) else None
                    if (
                        not _is_int(hero_id_2)
                        or not _is_number(match_count)
                        or not _is_number(synergy)
                    ):
                        continue
                    _merge_synergy_entry(
                        target,
                        hero_id,
                        hero_id_2,
                        int(match_count),
                        float(synergy),
                    )
    return {
        "with": _apply_synergy_reliability(with_data),
        "vs": _apply_synergy_reliability(vs_data),
    }


def _merge_synergy_entry(
    lookup: dict[int, dict[int, dict[str, float | int]]],
    hero_id: int,
    hero_id_2: int,
    match_count: int,
    synergy: float,
) -> None:
    entry = lookup.setdefault(hero_id, {}).setdefault(
        hero_id_2,
        {"matchCount": 0, "synergy": 0.0},
    )
    current_count = int(entry["matchCount"])
    total_count = current_count + match_count
    if total_count <= 0:
        return
    weighted_synergy = (
        float(entry["synergy"]) * (current_count / total_count)
        + synergy * (match_count / total_count)
    )
    lookup[hero_id][hero_id_2] = {
        "matchCount": total_count,
        "synergy": weighted_synergy,
    }


def _apply_synergy_reliability(
    lookup: dict[int, dict[int, dict[str, float | int]]],
) -> dict[int, dict[int, dict[str, float | int]]]:
    for entries in lookup.values():
        for entry in entries.values():
            confidence = _clamp(
                int(entry["matchCount"]) / ROSH_SYNERGY_RELIABILITY_MATCH_COUNT,
                0.0,
                1.0,
            )
            entry["synergy"] = _php_round(float(entry["synergy"]) * confidence, 2)
    return lookup


def _calculate_synergy_offset(
    radiant_picks: Sequence[Mapping[str, int]],
    dire_picks: Sequence[Mapping[str, int]],
    synergy_data: Mapping[str, Mapping[int, Mapping[int, Mapping[str, float | int]]]],
) -> float:
    with_lookup = synergy_data.get("with", {})
    vs_lookup = synergy_data.get("vs", {})
    radiant_synergy = _sum_team_pair_synergies(radiant_picks, with_lookup)
    dire_synergy = _sum_team_pair_synergies(dire_picks, with_lookup)
    matchup_advantage = _sum_matchup_advantages(
        radiant_picks,
        dire_picks,
        vs_lookup,
    )
    return _clamp(
        radiant_synergy - dire_synergy + matchup_advantage,
        -ROSH_SYNERGY_ADJUSTMENT_CAP,
        ROSH_SYNERGY_ADJUSTMENT_CAP,
    )


def _sum_team_pair_synergies(
    picks: Sequence[Mapping[str, int]],
    lookup: Mapping[int, Mapping[int, Mapping[str, float | int]]],
) -> float:
    total = 0.0
    for left_index in range(len(picks)):
        for right_index in range(left_index + 1, len(picks)):
            total += _average_pair_synergy(
                picks[left_index]["heroId"],
                picks[right_index]["heroId"],
                lookup,
            )
    return total


def _sum_matchup_advantages(
    radiant_picks: Sequence[Mapping[str, int]],
    dire_picks: Sequence[Mapping[str, int]],
    lookup: Mapping[int, Mapping[int, Mapping[str, float | int]]],
) -> float:
    advantage = 0.0
    for radiant_pick in radiant_picks:
        for dire_pick in dire_picks:
            radiant_synergy = _lookup_synergy(
                lookup,
                radiant_pick["heroId"],
                dire_pick["heroId"],
            )
            dire_synergy = _lookup_synergy(
                lookup,
                dire_pick["heroId"],
                radiant_pick["heroId"],
            )
            if radiant_synergy is not None and dire_synergy is not None:
                advantage += (radiant_synergy - dire_synergy) / 2
            elif radiant_synergy is not None:
                advantage += radiant_synergy
            elif dire_synergy is not None:
                advantage -= dire_synergy
    return advantage


def _average_pair_synergy(
    hero_id: int,
    hero_id_2: int,
    lookup: Mapping[int, Mapping[int, Mapping[str, float | int]]],
) -> float:
    left = _lookup_synergy(lookup, hero_id, hero_id_2)
    right = _lookup_synergy(lookup, hero_id_2, hero_id)
    if left is not None and right is not None:
        return (left + right) / 2
    if left is not None:
        return left
    return right if right is not None else 0.0


def _lookup_synergy(
    lookup: Mapping[int, Mapping[int, Mapping[str, float | int]]],
    hero_id: int,
    hero_id_2: int,
) -> float | None:
    value = lookup.get(hero_id, {}).get(hero_id_2, {}).get("synergy")
    return float(value) if _is_number(value) else None
