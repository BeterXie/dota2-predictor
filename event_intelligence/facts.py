"""Source-exact completed-match facts and component readiness assessment."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .models import ComponentReadiness as ComponentStatus
from .opendota import MatchIdentityError
from .raw_archive import schema_fingerprint


Number = int | float


@dataclass(frozen=True)
class ComponentAssessment:
    status: ComponentStatus
    reasons: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status is ComponentStatus.READY


@dataclass(frozen=True)
class MatchComponentReadiness:
    normalization: ComponentAssessment
    player_scoring: ComponentAssessment
    draft_model: ComponentAssessment
    team_state: ComponentAssessment
    objective_analysis: ComponentAssessment
    postmatch_attribution: ComponentAssessment


@dataclass(frozen=True)
class PlayerFacts:
    account_id: int | None
    name: str | None
    personaname: str | None
    player_slot: int | None
    hero_id: int | None
    is_radiant: bool | None
    team_id: int | None
    kills: int | None
    deaths: int | None
    assists: int | None
    gold_per_min: int | None
    xp_per_min: int | None
    net_worth: int | None
    last_hits: int | None
    denies: int | None
    hero_damage: int | None
    hero_healing: int | None
    tower_damage: int | None
    damage_taken: Number | dict[str, Any] | None
    stuns: Number | None
    camps_stacked: int | None
    rune_pickups: int | None
    observer_wards_placed: int | None
    sentry_wards_placed: int | None
    observer_kills: int | None
    sentry_kills: int | None
    level: int | None
    lane_role: int | None
    is_roaming: bool | None
    gold_timeline: tuple[Number | None, ...] | None
    last_hits_timeline: tuple[Number | None, ...] | None
    xp_timeline: tuple[Number | None, ...] | None
    gold_at_10: Number | None
    last_hits_at_10: Number | None
    xp_at_10: Number | None
    kills_at_10: int | None
    deaths_at_10: int | None
    assists_at_10: int | None
    observer_wards_at_10: int | None
    sentry_wards_at_10: int | None
    observer_kills_at_10: int | None
    sentry_kills_at_10: int | None
    buyback_log: tuple[dict[str, Any], ...] | None

    @property
    def damage_taken_total(self) -> Number | None:
        if _is_number(self.damage_taken):
            return self.damage_taken
        if isinstance(self.damage_taken, dict):
            values = tuple(self.damage_taken.values())
            if values and all(_is_number(value) for value in values):
                return sum(values)
        return None


@dataclass(frozen=True)
class DraftActionFacts:
    is_pick: bool | None
    hero_id: int | None
    team: int | None
    order: int | None


@dataclass(frozen=True)
class ObjectiveFacts:
    time_seconds: Number | None
    type: str | None
    unit: str | None
    key: Any
    player_slot: int | None
    team: int | None


@dataclass(frozen=True)
class MatchCompleteness:
    source_parsed: bool
    basic_result: bool
    player_count: int
    pick_count: int
    ten_players: bool
    ten_picks: bool
    gold_timeline: bool
    xp_timeline: bool
    objectives: bool
    teamfights: bool
    gold_required_minutes: int
    gold_observed_minutes: int
    objective_count: int | None


@dataclass(frozen=True)
class CompletedMatchFacts:
    match_id: int
    league_id: int | None
    series_id: int | None
    series_type: int | None
    start_time: int | None
    duration: int | None
    radiant_win: bool | None
    radiant_team_id: int | None
    dire_team_id: int | None
    radiant_score: int | None
    dire_score: int | None
    patch: int | None
    source_version: int | None
    players: tuple[PlayerFacts, ...]
    picks_bans: tuple[DraftActionFacts, ...]
    radiant_gold_adv: tuple[Number | None, ...] | None
    radiant_xp_adv: tuple[Number | None, ...] | None
    objectives: tuple[ObjectiveFacts, ...] | None
    teamfights: tuple[dict[str, Any], ...] | None
    completeness: MatchCompleteness
    readiness: MatchComponentReadiness
    source_schema_fingerprint: str


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_int(value: Any) -> int | None:
    value = _int(value)
    return value if value is not None and value > 0 else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: Any) -> Number | None:
    return value if _is_number(value) else None


def _number_series(value: Any) -> tuple[Number | None, ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(_number(item) for item in value)


def _at_minute(
    values: tuple[Number | None, ...] | None,
    minute: int,
) -> Number | None:
    if values is None or len(values) <= minute:
        return None
    return values[minute]


def _exact_log_count(value: Any, max_time: int = 600) -> int | None:
    if not isinstance(value, list):
        return None
    times: list[Number] = []
    for event in value:
        if not isinstance(event, dict) or not _is_number(event.get("time")):
            return None
        times.append(event["time"])
    return sum(time <= max_time for time in times)


def _dict_log(value: Any) -> tuple[dict[str, Any], ...] | None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return tuple(copy.deepcopy(item) for item in value)


def _side_from_slot(slot: int | None) -> bool | None:
    if slot in {0, 1, 2, 3, 4}:
        return True
    if slot in {128, 129, 130, 131, 132}:
        return False
    return None


def _damage_taken(value: Any) -> Number | dict[str, Any] | None:
    if _is_number(value):
        return value
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return None


def _extract_player(
    value: dict[str, Any],
    radiant_team_id: int | None,
    dire_team_id: int | None,
) -> PlayerFacts:
    slot = _int(value.get("player_slot"))
    is_radiant = _side_from_slot(slot)
    team_id = _positive_int(value.get("team_id"))
    if team_id is None and is_radiant is True:
        team_id = radiant_team_id
    elif team_id is None and is_radiant is False:
        team_id = dire_team_id

    gold_timeline = _number_series(value.get("gold_t"))
    last_hits_timeline = _number_series(value.get("lh_t"))
    xp_timeline = _number_series(value.get("xp_t"))
    return PlayerFacts(
        account_id=_int(value.get("account_id")),
        name=_string(value.get("name")),
        personaname=_string(value.get("personaname")),
        player_slot=slot,
        hero_id=_positive_int(value.get("hero_id")),
        is_radiant=is_radiant,
        team_id=team_id,
        kills=_int(value.get("kills")),
        deaths=_int(value.get("deaths")),
        assists=_int(value.get("assists")),
        gold_per_min=_int(value.get("gold_per_min")),
        xp_per_min=_int(value.get("xp_per_min")),
        net_worth=_int(value.get("net_worth")),
        last_hits=_int(value.get("last_hits")),
        denies=_int(value.get("denies")),
        hero_damage=_int(value.get("hero_damage")),
        hero_healing=_int(value.get("hero_healing")),
        tower_damage=_int(value.get("tower_damage")),
        damage_taken=_damage_taken(value.get("damage_taken")),
        stuns=_number(value.get("stuns")),
        camps_stacked=_int(value.get("camps_stacked")),
        rune_pickups=_int(value.get("rune_pickups")),
        observer_wards_placed=_int(value.get("obs_placed")),
        sentry_wards_placed=_int(value.get("sen_placed")),
        observer_kills=_int(value.get("observer_kills")),
        sentry_kills=_int(value.get("sentry_kills")),
        level=_int(value.get("level")),
        lane_role=_int(value.get("lane_role")),
        is_roaming=_bool(value.get("is_roaming")),
        gold_timeline=gold_timeline,
        last_hits_timeline=last_hits_timeline,
        xp_timeline=xp_timeline,
        gold_at_10=_at_minute(gold_timeline, 10),
        last_hits_at_10=_at_minute(last_hits_timeline, 10),
        xp_at_10=_at_minute(xp_timeline, 10),
        kills_at_10=_exact_log_count(value.get("kills_log")),
        deaths_at_10=_exact_log_count(value.get("deaths_log")),
        assists_at_10=_exact_log_count(value.get("assists_log")),
        observer_wards_at_10=_exact_log_count(value.get("obs_log")),
        sentry_wards_at_10=_exact_log_count(value.get("sen_log")),
        observer_kills_at_10=_exact_log_count(value.get("observer_kills_log")),
        sentry_kills_at_10=_exact_log_count(value.get("sentry_kills_log")),
        buyback_log=_dict_log(value.get("buyback_log")),
    )


def _extract_draft_action(value: dict[str, Any]) -> DraftActionFacts:
    return DraftActionFacts(
        is_pick=_bool(value.get("is_pick")),
        hero_id=_positive_int(value.get("hero_id")),
        team=_int(value.get("team")),
        order=_int(value.get("order")),
    )


def _extract_objective(value: dict[str, Any]) -> ObjectiveFacts:
    objective_type = value.get("type")
    unit = value.get("unit")
    return ObjectiveFacts(
        time_seconds=_number(value.get("time")),
        type=objective_type if isinstance(objective_type, str) else None,
        unit=unit if isinstance(unit, str) else None,
        key=copy.deepcopy(value.get("key")),
        player_slot=_int(value.get("player_slot", value.get("slot"))),
        team=_int(value.get("team")),
    )


def _timeline_coverage(
    values: tuple[Number | None, ...] | None,
    duration: int | None,
) -> tuple[bool, int, int]:
    if duration is None or duration <= 0:
        return False, 0, 0
    end_minute = duration // 60 - 2
    if end_minute < 10:
        return False, 0, 0
    required = tuple(range(10, end_minute + 1))
    observed = 0
    if values is not None:
        observed = sum(
            minute < len(values) and _is_number(values[minute]) for minute in required
        )
    return observed == len(required), len(required), observed


def _assessment(*reasons: str, unscorable: bool = False) -> ComponentAssessment:
    filtered = tuple(reason for reason in reasons if reason)
    if not filtered:
        return ComponentAssessment(ComponentStatus.READY)
    status = ComponentStatus.UNSCORABLE if unscorable else ComponentStatus.RETRYABLE
    return ComponentAssessment(status, filtered)


def extract_completed_match_facts(
    payload: dict[str, Any],
    *,
    expected_match_id: int | None = None,
) -> CompletedMatchFacts:
    """Extract only source-recorded facts; absence never becomes a numeric zero."""
    if not isinstance(payload, dict):
        raise TypeError("completed match payload must be a JSON object")
    match_id = _positive_int(payload.get("match_id"))
    if match_id is None:
        raise MatchIdentityError("completed match payload has no valid match_id")
    if expected_match_id is not None and _positive_int(expected_match_id) is None:
        raise ValueError("expected_match_id must be a positive integer or None")
    if expected_match_id is not None and match_id != expected_match_id:
        raise MatchIdentityError(
            f"expected match {expected_match_id}, payload contained {match_id}"
        )

    league_id = _positive_int(payload.get("leagueid"))
    duration = _positive_int(payload.get("duration"))
    radiant_win = _bool(payload.get("radiant_win"))
    radiant_team_id = _positive_int(payload.get("radiant_team_id"))
    dire_team_id = _positive_int(payload.get("dire_team_id"))
    source_version = _positive_int(payload.get("version"))

    raw_players = payload.get("players")
    player_rows = raw_players if isinstance(raw_players, list) else []
    players = tuple(
        _extract_player(row, radiant_team_id, dire_team_id)
        for row in player_rows
        if isinstance(row, dict)
    )
    raw_actions = payload.get("picks_bans")
    action_rows = raw_actions if isinstance(raw_actions, list) else []
    picks_bans = tuple(
        _extract_draft_action(row) for row in action_rows if isinstance(row, dict)
    )

    radiant_gold_adv = _number_series(payload.get("radiant_gold_adv"))
    radiant_xp_adv = _number_series(payload.get("radiant_xp_adv"))
    raw_objectives = payload.get("objectives")
    objectives = (
        tuple(_extract_objective(row) for row in raw_objectives if isinstance(row, dict))
        if isinstance(raw_objectives, list)
        else None
    )
    raw_teamfights = payload.get("teamfights")
    teamfights = _dict_log(raw_teamfights)

    expected_slots = {0, 1, 2, 3, 4, 128, 129, 130, 131, 132}
    player_slots = {player.player_slot for player in players}
    player_heroes = [player.hero_id for player in players]
    ten_players = (
        len(player_rows) == 10
        and len(players) == 10
        and player_slots == expected_slots
        and all(hero is not None for hero in player_heroes)
        and len(set(player_heroes)) == 10
    )
    picks = [action for action in picks_bans if action.is_pick is True]
    pick_heroes = [action.hero_id for action in picks]
    pick_teams = [action.team for action in picks]
    radiant_player_heroes = {
        player.hero_id for player in players if player.is_radiant is True
    }
    dire_player_heroes = {
        player.hero_id for player in players if player.is_radiant is False
    }
    radiant_pick_heroes = {
        action.hero_id for action in picks if action.team == 0
    }
    dire_pick_heroes = {
        action.hero_id for action in picks if action.team == 1
    }
    ten_picks = (
        len(picks) == 10
        and all(hero is not None for hero in pick_heroes)
        and len(set(pick_heroes)) == 10
        and pick_teams.count(0) == 5
        and pick_teams.count(1) == 5
        and radiant_pick_heroes == radiant_player_heroes
        and dire_pick_heroes == dire_player_heroes
    )

    gold_complete, gold_required, gold_observed = _timeline_coverage(
        radiant_gold_adv, duration
    )
    xp_complete, _, _ = _timeline_coverage(radiant_xp_adv, duration)
    objectives_complete = (
        isinstance(raw_objectives, list)
        and len(raw_objectives) > 0
        and len(objectives or ()) == len(raw_objectives)
        and all(
            objective.time_seconds is not None and bool(objective.type)
            for objective in objectives or ()
        )
    )
    source_parsed = source_version is not None
    basic_result = (
        duration is not None
        and radiant_win is not None
        and radiant_team_id is not None
        and dire_team_id is not None
        and radiant_team_id != dire_team_id
    )

    completeness = MatchCompleteness(
        source_parsed=source_parsed,
        basic_result=basic_result,
        player_count=len(player_rows),
        pick_count=len(picks),
        ten_players=ten_players,
        ten_picks=ten_picks,
        gold_timeline=gold_complete,
        xp_timeline=xp_complete,
        objectives=objectives_complete,
        teamfights=teamfights is not None,
        gold_required_minutes=gold_required,
        gold_observed_minutes=gold_observed,
        objective_count=len(raw_objectives) if isinstance(raw_objectives, list) else None,
    )

    parse_reason = "" if source_parsed else "source_unparsed"
    basic_reason = "" if basic_result else "basic_result_incomplete"
    normalization = _assessment(parse_reason, basic_reason)
    required_player_fields = (
        "kills",
        "deaths",
        "assists",
        "gold_per_min",
        "xp_per_min",
        "last_hits",
        "hero_damage",
        "tower_damage",
    )
    player_components_complete = ten_players and all(
        all(getattr(player, field) is not None for field in required_player_fields)
        for player in players
    )
    player_scoring = _assessment(
        parse_reason,
        basic_reason,
        "" if ten_players else "ten_players_incomplete",
        "" if player_components_complete else "player_components_incomplete",
    )
    draft_model = _assessment(
        parse_reason,
        basic_reason,
        "" if ten_picks else "ten_picks_incomplete",
    )
    state_window_too_short = duration is not None and duration // 60 - 2 < 10
    team_state = _assessment(
        parse_reason,
        basic_reason,
        "" if gold_complete else "gold_timeline_incomplete",
        unscorable=state_window_too_short and source_parsed and basic_result,
    )
    objective_analysis = _assessment(
        parse_reason,
        basic_reason,
        "" if objectives_complete else "objectives_incomplete",
    )
    postmatch_reasons = tuple(
        reason
        for assessment in (player_scoring, draft_model, team_state, objective_analysis)
        for reason in assessment.reasons
    ) + (
        () if teamfights is not None else ("teamfights_missing",)
    ) + (
        ()
        if ten_players and all(player.buyback_log is not None for player in players)
        else ("buyback_logs_missing",)
    )
    postmatch_attribution = _assessment(*dict.fromkeys(postmatch_reasons))

    return CompletedMatchFacts(
        match_id=match_id,
        league_id=league_id,
        series_id=_positive_int(payload.get("series_id")),
        series_type=_int(payload.get("series_type")),
        start_time=_positive_int(payload.get("start_time")),
        duration=duration,
        radiant_win=radiant_win,
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_score=_int(payload.get("radiant_score")),
        dire_score=_int(payload.get("dire_score")),
        patch=_positive_int(payload.get("patch")),
        source_version=source_version,
        players=players,
        picks_bans=picks_bans,
        radiant_gold_adv=radiant_gold_adv,
        radiant_xp_adv=radiant_xp_adv,
        objectives=objectives,
        teamfights=teamfights,
        completeness=completeness,
        readiness=MatchComponentReadiness(
            normalization=normalization,
            player_scoring=player_scoring,
            draft_model=draft_model,
            team_state=team_state,
            objective_analysis=objective_analysis,
            postmatch_attribution=postmatch_attribution,
        ),
        source_schema_fingerprint=schema_fingerprint(payload),
    )
