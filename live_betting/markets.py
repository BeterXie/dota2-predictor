"""Normalize RayBet Dota 2 markets without guessing ambiguous outcomes."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from collections.abc import Sequence
from typing import Any

from .models import Market, OddsSnapshot, utc_now


NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
RACE_RE = re.compile(r"first team to (?:get )?(\d+) kills", re.IGNORECASE)


def _number(value: Any) -> float | None:
    match = NUMBER_RE.search(str(value or ""))
    return float(match.group()) if match else None


def _period(stage: str) -> str:
    stage = stage.strip().lower()
    if stage == "final":
        return "series"
    if re.fullmatch(r"r\d+", stage):
        return f"map_{stage[1:]}"
    return stage or "unknown"


def _side(value: str, name: str = "") -> str | None:
    text = f"{value} {name}".strip().lower()
    if text.startswith(("over", "o ", ">", "大于")) or " over " in f" {text} ":
        return "over"
    if text.startswith(("under", "u ", "<", "小于")) or " under " in f" {text} ":
        return "under"
    return None


def normalize_market(item: dict[str, Any], team_side: str | None = None) -> Market:
    stage = str(item.get("match_stage") or "")
    group = str(item.get("group_short_name") or item.get("group_name") or "").strip()
    group_lower = group.lower()
    tag = str(item.get("tag") or "").lower()
    value = str(item.get("value") or "").strip()
    name = str(item.get("name") or "").strip()
    period = _period(stage)

    if group_lower in {"winner", "1x2"} and tag in {"win", "wdl"}:
        side = team_side
        if value.lower() == "draw" or (not side and tag == "wdl"):
            side = "draw"
        supported = (
            side in {"team_one", "team_two"}
            and group_lower == "winner"
            and period.startswith("map_")
        )
        return Market("winner", period, side, None, side or value, supported,
                      None if supported else "series_draw_or_side_unresolved")

    if "total kills" in group_lower and tag == "ou":
        side = _side(value, name)
        line = _number(value) or _number(name)
        market_type = "team_total_kills" if "$t" in group_lower or team_side else "total_kills"
        if "$t1" in group_lower:
            team_side = "team_one"
        elif "$t2" in group_lower:
            team_side = "team_two"
        outcome = f"{team_side or 'both'}:{side}:{line}"
        supported = side in {"over", "under"} and line is not None and line <= 150
        return Market(market_type, period, side, line, outcome, supported,
                      None if supported else "total_kills_parse_failed")

    if "kill handicap" in group_lower and tag == "hdp":
        line = _number(value)
        supported = team_side in {"team_one", "team_two"} and line is not None
        return Market("kill_handicap", period, team_side, line,
                      f"{team_side}:{line}", supported,
                      None if supported else "kill_handicap_parse_failed")

    race = RACE_RE.search(group)
    if race and tag == "win":
        target = float(race.group(1))
        supported = team_side in {"team_one", "team_two"}
        return Market("race_to_kills", period, team_side, target,
                      f"{team_side}:{int(target)}", supported,
                      None if supported else "race_side_unresolved")

    if "duration" in group_lower and tag == "ou":
        side = _side(value, name)
        line = _number(value) or _number(name)
        supported = side in {"over", "under"} and line is not None and line <= 120
        return Market("duration", period, side, line, f"{side}:{line}", supported,
                      None if supported else "duration_parse_failed")

    key = ":".join(part for part in (period, group, tag, value, name) if part)
    return Market("unclassified", period, team_side, _number(value), key, False,
                  "dedicated_model_or_parser_required")


def snapshots_from_payload(
    payload: dict[str, Any], received_at: datetime | None = None
) -> list[OddsSnapshot]:
    result = payload.get("result") or {}
    match_id = str(result.get("id") or "")
    teams = sorted(result.get("team") or [], key=lambda row: int(row.get("pos") or 0))
    side_by_id = {
        str(team.get("team_id")): "team_one" if index == 0 else "team_two"
        for index, team in enumerate(teams[:2])
    }
    received_at = received_at or utc_now()
    snapshots: list[OddsSnapshot] = []
    for item in result.get("odds") or []:
        try:
            price = float(item.get("odds"))
        except (TypeError, ValueError):
            continue
        odds_id = str(item.get("odds_id") or item.get("id") or "")
        if not odds_id or price <= 1.0:
            continue
        team_side = side_by_id.get(str(item.get("team_id")))
        market = normalize_market(item, team_side)
        snapshots.append(
            OddsSnapshot(
                raybet_match_id=match_id,
                odds_id=odds_id,
                odds_group_id=str(item.get("odds_group_id") or "") or None,
                received_at=received_at,
                price=price,
                status=item.get("status"),
                market=market,
                last_update=str(item.get("last_update") or "") or None,
                raw=item,
            )
        )
    return snapshots


def normalized_state_hash(snapshots: Sequence[OddsSnapshot]) -> str:
    """Hash only normalized market state, independent of response metadata."""
    state = sorted(
        (
            snapshot.odds_id,
            snapshot.price,
            str(snapshot.status),
            snapshot.last_update,
        )
        for snapshot in snapshots
    )
    canonical = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
