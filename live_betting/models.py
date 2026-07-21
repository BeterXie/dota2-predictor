"""Provider-neutral live betting data models."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


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
class RoshLineupScore:
    score_key: str
    draft_hash: str
    player_identity_hash: str
    pure_lineup_score: float
    player_adjusted_lineup_score: float | None
    effective_lineup_score: float
    scoring_mode: str
    player_coverage_count: int
    stake_multiplier: float
    formula_version: str
    source_name: str
    source_week: int
    cache_week_start: int
    source_as_of: datetime
    evidence_hash: str
    evidence: Mapping[str, Any] = field(compare=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("pure_lineup_score", self.pure_lineup_score),
            ("effective_lineup_score", self.effective_lineup_score),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite")
        adjusted = self.player_adjusted_lineup_score
        if adjusted is not None and (
            isinstance(adjusted, bool)
            or not isinstance(adjusted, (int, float))
            or not math.isfinite(float(adjusted))
        ):
            raise ValueError("player_adjusted_lineup_score must be finite")
        if (
            isinstance(self.player_coverage_count, bool)
            or not isinstance(self.player_coverage_count, int)
            or not 0 <= self.player_coverage_count <= 10
        ):
            raise ValueError("player_coverage_count must be between 0 and 10")
        if (
            isinstance(self.stake_multiplier, bool)
            or not isinstance(self.stake_multiplier, (int, float))
            or float(self.stake_multiplier) not in {0.5, 1.0}
        ):
            raise ValueError("Rosh stake cap must be 0.5 or 1.0")
        if self.scoring_mode == "player_adjusted":
            if (
                self.player_coverage_count != 10
                or adjusted is None
                or self.effective_lineup_score != adjusted
                or self.stake_multiplier != 1.0
            ):
                raise ValueError(
                    "player-adjusted score requires full coverage and full stake"
                )
        elif self.scoring_mode == "pure":
            if (
                self.player_coverage_count >= 10
                or adjusted is not None
                or self.effective_lineup_score != self.pure_lineup_score
                or self.stake_multiplier != 0.5
            ):
                raise ValueError(
                    "pure score requires incomplete coverage and half stake"
                )
        else:
            raise ValueError("scoring_mode must be pure or player_adjusted")
        for name, value in (
            ("score_key", self.score_key),
            ("draft_hash", self.draft_hash),
            ("player_identity_hash", self.player_identity_hash),
            ("evidence_hash", self.evidence_hash),
        ):
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not self.formula_version.strip() or self.source_name != "stratz":
            raise ValueError("Rosh score source identity is invalid")
        if (
            isinstance(self.source_week, bool)
            or not isinstance(self.source_week, int)
            or self.source_week <= 0
        ):
            raise ValueError("source_week must be positive")
        if (
            isinstance(self.cache_week_start, bool)
            or not isinstance(self.cache_week_start, int)
            or self.cache_week_start <= 0
        ):
            raise ValueError("cache_week_start must be positive")
        if self.source_as_of.tzinfo is None or self.source_as_of.utcoffset() is None:
            raise ValueError("source_as_of must be timezone-aware")
        if not isinstance(self.evidence, Mapping):
            raise ValueError("evidence must be a mapping")

    @property
    def pure_score(self) -> float:
        return float(self.pure_lineup_score)

    @property
    def player_adjusted_score(self) -> float | None:
        return (
            float(self.player_adjusted_lineup_score)
            if self.player_adjusted_lineup_score is not None
            else None
        )

    @property
    def effective_score(self) -> float:
        return float(self.effective_lineup_score)

    @property
    def mode(self) -> str:
        return self.scoring_mode

    @property
    def player_coverage(self) -> float:
        return self.player_coverage_count / 10.0

    @property
    def stake_cap(self) -> float:
        """Maximum allowed stake, not the strategy's actual order stake."""
        return float(self.stake_multiplier)

    def as_input_ref(self) -> dict[str, Any]:
        return {
            "score_key": self.score_key,
            "draft_hash": self.draft_hash,
            "player_identity_hash": self.player_identity_hash,
            "pure_score": self.pure_score,
            "player_adjusted_score": self.player_adjusted_score,
            "effective_score": self.effective_score,
            "mode": self.mode,
            "player_coverage": self.player_coverage,
            "player_coverage_count": self.player_coverage_count,
            "stake_cap": self.stake_cap,
            # Compatibility for existing consumers; this value is a cap.
            "stake_multiplier": self.stake_cap,
            "formula_version": self.formula_version,
            "source_name": self.source_name,
            "source_week": self.source_week,
            "cache_week_start": self.cache_week_start,
            "source_as_of": self.source_as_of.isoformat(),
            "evidence_hash": self.evidence_hash,
            "evidence": {
                key: value
                for key, value in self.evidence.items()
                if key not in {"pure_minute_table", "minute_table"}
            },
        }


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
        if (
            isinstance(self.stake, bool)
            or not isinstance(self.stake, (int, float))
            or not math.isfinite(float(self.stake))
            or not 0.0 < float(self.stake) <= 1.0
        ):
            raise ValueError("shadow order stake must be greater than 0 and at most 1")
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
