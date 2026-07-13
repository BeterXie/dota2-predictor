"""Pure shadow-signal and fill decisions."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

from .models import ModelQuote, OddsSnapshot, ShadowOrder
from .pricing import market_key


OPEN_STATUSES = {1, 5, "1", "5", "open", "active", "running"}
SIGNAL_EXPIRY = timedelta(seconds=15)


def is_open(status: str | int | None) -> bool:
    return status in OPEN_STATUSES


def make_order(
    quote: ModelQuote,
    snapshot: OddsSnapshot,
    *,
    min_edge: float,
    signal_transport_key: str,
    signal_transport_at: datetime,
) -> ShadowOrder | None:
    if quote.edge < min_edge or not is_open(snapshot.status):
        return None
    if signal_transport_at != quote.quoted_at:
        raise ValueError("signal transport time must equal quote time")
    raw_key = "|".join(
        (
            quote.raybet_match_id,
            snapshot.odds_id,
            snapshot.odds_group_id or "",
            snapshot.market.outcome_key,
            market_key(
                snapshot.market.market_type,
                snapshot.market.period,
                snapshot.market.side,
                snapshot.market.line,
            ),
            quote.strategy_version,
            quote.input_ref,
        )
    )
    order_key = hashlib.sha256(raw_key.encode()).hexdigest()[:32]
    return ShadowOrder(
        order_key=order_key,
        raybet_match_id=quote.raybet_match_id,
        odds_id=snapshot.odds_id,
        market=snapshot.market,
        signaled_at=quote.quoted_at,
        model_probability=quote.model_probability,
        market_probability=quote.market_probability,
        signal_price=snapshot.price,
        signal_transport_key=signal_transport_key,
        signal_transport_at=signal_transport_at,
        expires_at=signal_transport_at + SIGNAL_EXPIRY,
        signal_odds_group_id=snapshot.odds_group_id,
        signal_outcome_key=snapshot.market.outcome_key,
        signal_identity_verified=True,
    )


def attempt_fill(
    order: ShadowOrder,
    snapshot: OddsSnapshot,
    *,
    observed_at: datetime | None = None,
    max_slippage: float = 0.03,
    max_age: timedelta = timedelta(seconds=15),
    now: datetime | None = None,
) -> ShadowOrder:
    if order.status != "pending":
        return order
    effective_at = observed_at or snapshot.received_at
    if effective_at <= order.signal_transport_at:
        return order
    if snapshot.odds_id != order.odds_id:
        return order
    if not order.signal_identity_verified:
        return replace(
            order,
            status="rejected",
            rejection_reason="signal_identity_unverified",
        )
    if market_key(
        snapshot.market.market_type,
        snapshot.market.period,
        snapshot.market.side,
        snapshot.market.line,
    ) != market_key(
        order.market.market_type,
        order.market.period,
        order.market.side,
        order.market.line,
    ):
        return replace(order, status="rejected", rejection_reason="market_mismatch")
    if (
        snapshot.odds_group_id != order.signal_odds_group_id
        or snapshot.market.outcome_key != order.signal_outcome_key
    ):
        return replace(
            order,
            status="rejected",
            rejection_reason="outcome_identity_mismatch",
        )
    if effective_at > order.expires_at:
        return replace(order, status="rejected", rejection_reason="fill_timeout")
    if not is_open(snapshot.status):
        return replace(order, status="rejected", rejection_reason="market_closed")
    drop = (order.signal_price - snapshot.price) / order.signal_price
    if drop > max_slippage:
        return replace(order, status="rejected", rejection_reason="slippage")
    return replace(
        order,
        status="filled",
        fill_price=snapshot.price,
        filled_at=effective_at,
    )
