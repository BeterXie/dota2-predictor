"""Provider-neutral live betting data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProviderMatch:
    provider: str
    provider_match_id: str
    tournament: str
    team_one: str
    team_two: str
    scheduled_at: datetime | None
    best_of: int | None = None
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class LiveFrame:
    provider: str
    provider_match_id: str
    provider_game_id: str | None
    sequence: str | None
    source_at: datetime | None
    received_at: datetime
    game_time: int | None
    team_one_kills: int | None
    team_two_kills: int | None
    team_one_gold: int | None = None
    team_two_gold: int | None = None
    state: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class LiveEvent:
    provider: str
    provider_event_id: str
    provider_match_id: str
    provider_game_id: str | None
    event_type: str
    source_at: datetime | None
    received_at: datetime
    game_time: int | None
    team: str | None = None
    player: str | None = None
    value: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Market:
    market_type: str
    period: str
    side: str | None
    line: float | None
    outcome_key: str
    supported: bool
    reason: str | None = None


@dataclass(frozen=True)
class OddsSnapshot:
    raybet_match_id: str
    odds_id: str
    odds_group_id: str | None
    received_at: datetime
    price: float
    status: str | int | None
    market: Market
    last_update: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class ModelQuote:
    raybet_match_id: str
    provider_game_id: str | None
    market: Market
    model_probability: float
    market_probability: float
    edge: float
    quoted_at: datetime
    strategy_version: str
    input_ref: str


@dataclass(frozen=True)
class ShadowOrder:
    order_key: str
    raybet_match_id: str
    odds_id: str
    market: Market
    signaled_at: datetime
    model_probability: float
    market_probability: float
    signal_price: float
    signal_transport_key: str
    signal_transport_at: datetime
    expires_at: datetime
    signal_odds_group_id: str | None
    signal_outcome_key: str | None
    signal_identity_verified: bool
    stake: float = 1.0
    status: str = "pending"
    fill_price: float | None = None
    filled_at: datetime | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.signal_transport_key:
            raise ValueError("signal_transport_key is required")
        if self.signal_transport_at != self.signaled_at:
            raise ValueError("signal transport time must equal signal time")
        if self.expires_at != self.signaled_at + timedelta(seconds=15):
            raise ValueError("shadow order expiry must be exactly 15 seconds")
        if not isinstance(self.signal_identity_verified, bool):
            raise ValueError("signal_identity_verified must be boolean")
        if self.signal_identity_verified and (
            not self.signal_odds_group_id or not self.signal_outcome_key
        ):
            raise ValueError("verified signal provider identity is required")
        if (
            self.signal_identity_verified
            and self.signal_outcome_key != self.market.outcome_key
        ):
            raise ValueError("signal outcome identity must match the market")
        if not self.signal_identity_verified and (
            self.signal_odds_group_id is not None
            or self.signal_outcome_key is not None
        ):
            raise ValueError("unverified signal identity must remain empty")
