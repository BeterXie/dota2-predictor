"""Provider-neutral RayBet collection models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
