"""Provider-neutral shadow strategy engine."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .models import ModelQuote, OddsSnapshot, ShadowOrder
from .pricing import devig, market_key
from .strategy import make_order


def price_groups(snapshots: list[OddsSnapshot]) -> dict[str, float]:
    """Return normalized market probabilities keyed by odds ID.

    Only complete groups with at least two open positive prices are usable.
    Group membership comes from RayBet's stable odds_group_id, not labels.
    """
    groups: dict[str, list[OddsSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.odds_group_id and snapshot.price > 1:
            groups[snapshot.odds_group_id].append(snapshot)
    probabilities: dict[str, float] = {}
    for rows in groups.values():
        if len(rows) < 2:
            continue
        normalized = devig(row.price for row in rows)
        probabilities.update({row.odds_id: probability for row, probability in zip(rows, normalized)})
    return probabilities


def build_orders(
    *,
    snapshots: list[OddsSnapshot],
    model_probabilities: dict[str, float],
    provider_game_id: str | None,
    input_ref: str,
    strategy_version: str,
    quoted_at: datetime,
    signal_transport_key: str,
    signal_transport_at: datetime,
    min_edge: float,
) -> list[tuple[ModelQuote, ShadowOrder]]:
    market_probabilities = price_groups(snapshots)
    output: list[tuple[ModelQuote, ShadowOrder]] = []
    for snapshot in snapshots:
        market = snapshot.market
        key = market_key(market.market_type, market.period, market.side, market.line)
        model_probability = model_probabilities.get(key)
        market_probability = market_probabilities.get(snapshot.odds_id)
        if not market.supported or model_probability is None or market_probability is None:
            continue
        if not 0 <= model_probability <= 1:
            raise ValueError(f"invalid model probability for {key}")
        quote = ModelQuote(
            raybet_match_id=snapshot.raybet_match_id,
            provider_game_id=provider_game_id,
            market=market,
            model_probability=model_probability,
            market_probability=market_probability,
            edge=model_probability - market_probability,
            quoted_at=quoted_at,
            strategy_version=strategy_version,
            input_ref=input_ref,
        )
        order = make_order(
            quote,
            snapshot,
            min_edge=min_edge,
            signal_transport_key=signal_transport_key,
            signal_transport_at=signal_transport_at,
        )
        if order:
            output.append((quote, order))
    return output
