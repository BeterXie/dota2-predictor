"""Features derived only from the contemporaneous RayBet market surface."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .engine import price_groups
from .models import OddsSnapshot
from .raybet_state import raybet_odds_is_open


@dataclass(frozen=True)
class MarketSurface:
    underdog_side: str
    underdog_price: float
    underdog_probability: float
    probability_move: float
    kill_handicap: float | None
    total_kills: float | None
    duration_minutes: float | None
    quality: float
    missing_markets: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_markets


def _complete_groups(
    snapshots: list[OddsSnapshot], market_type: str
) -> list[list[OddsSnapshot]]:
    required_sides = {
        "winner": {"team_one", "team_two"},
        "kill_handicap": {"team_one", "team_two"},
        "total_kills": {"over", "under"},
        "duration": {"over", "under"},
    }[market_type]
    groups: dict[str, list[OddsSnapshot]] = defaultdict(list)
    for row in snapshots:
        if row.market.market_type == market_type and row.odds_group_id:
            groups[row.odds_group_id].append(row)
    return [
        rows for rows in groups.values()
        if required_sides <= {row.market.side for row in rows}
        and all(
            row.market.supported
            and row.price > 1
            and raybet_odds_is_open(row.status)
            for row in rows
        )
    ]


def _near_even_line(
    snapshots: list[OddsSnapshot], market_type: str, side: str | None = None
) -> float | None:
    complete_ids = {
        row.odds_id for rows in _complete_groups(snapshots, market_type) for row in rows
    }
    candidates = [
        row for row in snapshots
        if row.market.market_type == market_type
        and row.market.supported
        and row.odds_id in complete_ids
        and row.market.line is not None
        and (side is None or row.market.side == side)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(row.price - 2.0)).market.line


def build_market_surface(
    snapshots: list[OddsSnapshot],
    previous: "MarketSurface | None" = None,
) -> MarketSurface:
    probabilities = price_groups(snapshots)
    winner_groups = _complete_groups(snapshots, "winner")
    if len(winner_groups) != 1 or len(winner_groups[0]) != 2:
        raise ValueError("a complete two-way map winner market is required")
    winners = winner_groups[0]
    if any(row.odds_id not in probabilities for row in winners):
        raise ValueError("a priceable two-way map winner market is required")
    underdog = max(winners, key=lambda row: row.price)
    probability = probabilities[underdog.odds_id]
    move = 0.0
    if previous and previous.underdog_side == underdog.market.side:
        move = probability - previous.underdog_probability
    features = [
        _near_even_line(snapshots, "kill_handicap", underdog.market.side),
        _near_even_line(snapshots, "total_kills"),
        _near_even_line(snapshots, "duration"),
    ]
    missing = tuple(
        market_type for market_type, value in zip(
            ("kill_handicap", "total_kills", "duration"), features
        ) if value is None
    )
    quality = 0.55 + sum(value is not None for value in features) * 0.15
    return MarketSurface(
        underdog_side=str(underdog.market.side),
        underdog_price=underdog.price,
        underdog_probability=probability,
        probability_move=move,
        kill_handicap=features[0],
        total_kills=features[1],
        duration_minutes=features[2],
        quality=min(1.0, quality),
        missing_markets=missing,
    )
