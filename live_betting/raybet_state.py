"""Freshness-scoped RayBet match and map-state interpretation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


LIVE_MATCH_STATUS = "2"
LIVE_MATCH_MAX_AGE = timedelta(seconds=90)
OPEN_ODDS_STATUSES = frozenset({"1", "open", "active", "running"})
RAYBET_TIMEZONE = timezone(timedelta(hours=8))


def explicit_raybet_map_times(
    payload: Mapping[str, Any],
    best_of: int,
) -> dict[int, datetime]:
    teams = payload.get("team")
    if not isinstance(teams, list) or len(teams) != 2:
        return {}
    controls: list[Mapping[str, Any]] = []
    for team in teams:
        if not isinstance(team, Mapping):
            return {}
        score = team.get("score")
        manual = score.get("manualControlData") if isinstance(score, Mapping) else None
        data = manual.get("data") if isinstance(manual, Mapping) else None
        if not isinstance(data, Mapping):
            return {}
        controls.append(data)

    output: dict[int, datetime] = {}
    for map_number in range(1, best_of + 1):
        entries = [control.get(str(map_number)) for control in controls]
        if any(not isinstance(entry, Mapping) for entry in entries):
            return {}
        statuses = {entry.get("status") for entry in entries}
        stamps = {str(entry.get("cmDate") or "").strip() for entry in entries}
        if len(statuses) != 1 or len(stamps) != 1:
            return {}
        status = next(iter(statuses))
        stamp = next(iter(stamps))
        if type(status) is not int:
            return {}
        if status == 0 and not stamp:
            if output and tuple(output) != tuple(range(1, map_number)):
                return {}
            continue
        if status not in {1, 2} or not stamp:
            return {}
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            return {}
        if parsed.tzinfo is not None or parsed.strftime("%Y-%m-%d %H:%M:%S") != stamp:
            return {}
        output[map_number] = parsed.replace(tzinfo=RAYBET_TIMEZONE).astimezone(
            timezone.utc
        )
    if tuple(output) != tuple(range(1, len(output) + 1)):
        return {}
    return output


def _aware_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def raybet_match_is_live(
    status: object,
    updated_at: datetime | str | None,
    *,
    now: datetime | None = None,
    max_age: timedelta = LIVE_MATCH_MAX_AGE,
) -> bool:
    """Accept only a fresh provider row explicitly marked live by RayBet."""
    if str(status) != LIVE_MATCH_STATUS or max_age <= timedelta(0):
        return False
    observed_at = _aware_utc(updated_at)
    checked_at = _aware_utc(now or datetime.now(timezone.utc))
    if observed_at is None or checked_at is None:
        return False
    age = checked_at - observed_at
    return timedelta(0) <= age <= max_age


def raybet_odds_is_open(status: object) -> bool:
    """Reject settled/suspended markets; RayBet status 5 is a final result."""
    return str(status).strip().casefold() in OPEN_ODDS_STATUSES


def infer_current_map_number(payload: dict[str, Any], best_of: int | None) -> int | None:
    """Return the first unsettled map in a live series using explicit results."""
    if type(best_of) is not int or not 1 <= best_of <= 10:
        return None

    # Local import keeps supervisor startup independent of the HTTP client stack.
    from .raybet import parse_raybet_map_final

    wins = {"team_one": 0, "team_two": 0}
    wins_required = best_of // 2 + 1
    for map_number in range(1, best_of + 1):
        evidence = parse_raybet_map_final(payload, map_number)
        if evidence.status == "conflict":
            raise ValueError(evidence.reason)
        if evidence.reason != "raybet_winner_market_not_settled" and (
            evidence.status != "confirmed"
        ):
            raise ValueError(evidence.reason)
        winner = evidence.winner_side or evidence.score_winner_side
        if winner is None:
            return map_number
        wins[winner] += 1
        if wins[winner] >= wins_required:
            return None
    return None
