"""Deterministic settlement for supported Dota 2 markets."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Market


@dataclass(frozen=True)
class MapResult:
    winner: str
    team_one_kills: int
    team_two_kills: int
    duration_minutes: float
    first_to_kills: dict[int, str]


def reconcile_map_winners(
    *,
    raybet_status: str,
    raybet_winner: str | None,
    opendota_winner: str | None,
) -> tuple[str, str]:
    """Return the fail-closed state for normalized independent map results."""
    if raybet_status == "conflict":
        return "manual_review", "raybet_final_conflict"
    if raybet_status != "confirmed" or raybet_winner is None:
        return "pending", "raybet_final_missing"
    if raybet_winner not in {"team_one", "team_two"}:
        return "manual_review", "raybet_winner_invalid"
    if opendota_winner not in {"team_one", "team_two"}:
        return "pending", "opendota_winner_missing"
    if raybet_winner != opendota_winner:
        return "manual_review", "winner_conflict"
    return "confirmed", "sources_consistent"


def _asian_return(margin: float, line: float, price: float) -> tuple[str, float]:
    adjusted = margin + line
    if line * 2 % 1 == 0:
        if adjusted > 0:
            return "win", price
        if adjusted == 0:
            return "push", 1.0
        return "loss", 0.0
    lower = (line * 2 // 1) / 2
    upper = lower + 0.5
    outcomes = [_asian_return(margin, part, price) for part in (lower, upper)]
    returned = sum(item[1] for item in outcomes) / 2
    labels = {item[0] for item in outcomes}
    if labels == {"win", "push"}:
        return "half_win", returned
    if labels == {"loss", "push"}:
        return "half_loss", returned
    return outcomes[0][0], returned


def settle(market: Market, result: MapResult, price: float) -> tuple[str, float]:
    if market.market_type == "winner":
        won = market.side == result.winner
        return ("win", price) if won else ("loss", 0.0)

    if market.market_type in {"total_kills", "team_total_kills"}:
        if market.line is None or market.side not in {"over", "under"}:
            raise ValueError("invalid total market")
        if market.market_type == "team_total_kills":
            total = result.team_one_kills if "team_one" in market.outcome_key else result.team_two_kills
        else:
            total = result.team_one_kills + result.team_two_kills
        margin = total - market.line if market.side == "over" else market.line - total
        return _asian_return(margin, 0.0, price)

    if market.market_type == "kill_handicap":
        if market.line is None or market.side not in {"team_one", "team_two"}:
            raise ValueError("invalid kill handicap")
        margin = result.team_one_kills - result.team_two_kills
        if market.side == "team_two":
            margin = -margin
        return _asian_return(margin, market.line, price)

    if market.market_type == "race_to_kills":
        if market.line is None:
            raise ValueError("race target is required")
        won = result.first_to_kills.get(int(market.line)) == market.side
        return ("win", price) if won else ("loss", 0.0)

    if market.market_type == "duration":
        if market.line is None or market.side not in {"over", "under"}:
            raise ValueError("invalid duration market")
        margin = (result.duration_minutes - market.line if market.side == "over"
                  else market.line - result.duration_minutes)
        return _asian_return(margin, 0.0, price)

    raise ValueError(f"unsupported market: {market.market_type}")
