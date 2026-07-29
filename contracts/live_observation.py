"""Versioned contract emitted by RayBet stream vision."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 4
COMEBACK_STATE_MIN_CONFIDENCE = 0.9


def is_canonical_net_worth_bucket(minimum: object, maximum: object) -> bool:
    """Return whether bounds represent a production HUD thousand bucket."""
    return (
        type(minimum) is int
        and type(maximum) is int
        and minimum >= 0
        and minimum % 1_000 == 0
        and maximum == minimum + 999
    )


class ComebackState(BaseModel):
    """Current-frame HUD facts required to prove an in-game disadvantage."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "unavailable"] = "unavailable"
    source: Literal["vision_hud"] | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    radiant_kills: int | None = Field(default=None, ge=0)
    dire_kills: int | None = Field(default=None, ge=0)
    radiant_net_worth: int | None = Field(default=None, ge=0)
    dire_net_worth: int | None = Field(default=None, ge=0)
    net_worth_advantage_side: Literal["radiant", "dire"] | None = None
    net_worth_advantage_min: int | None = Field(default=None, ge=0)
    net_worth_advantage_max: int | None = Field(default=None, ge=0)
    unavailable_reason: str | None = Field(default="live_state_not_provided")

    @classmethod
    def unavailable(cls, reason: str = "live_state_not_provided") -> "ComebackState":
        return cls(unavailable_reason=reason)

    @model_validator(mode="after")
    def status_matches_evidence(self) -> "ComebackState":
        exact_values = (
            self.radiant_net_worth,
            self.dire_net_worth,
        )
        advantage_values = (
            self.net_worth_advantage_min,
            self.net_worth_advantage_max,
        )
        exact_present = any(value is not None for value in exact_values)
        advantage_present = self.net_worth_advantage_side is not None or any(
            value is not None for value in advantage_values
        )
        exact_valid = not exact_present or all(
            value is not None for value in exact_values
        )
        advantage_valid = not advantage_present or (
            self.net_worth_advantage_side is not None
            and is_canonical_net_worth_bucket(*advantage_values)
        )
        if self.status == "available":
            if (
                self.source != "vision_hud"
                or self.confidence < COMEBACK_STATE_MIN_CONFIDENCE
                or self.radiant_kills is None
                or self.dire_kills is None
                or not exact_valid
                or not advantage_valid
                or (exact_present and advantage_present)
                or self.unavailable_reason is not None
            ):
                raise ValueError(
                    "available comeback state requires complete trusted HUD evidence"
                )
            return self
        if (
            self.source is not None
            or self.confidence != 0.0
            or self.radiant_kills is not None
            or self.dire_kills is not None
            or exact_present
            or advantage_present
            or not isinstance(self.unavailable_reason, str)
            or not self.unavailable_reason.strip()
        ):
            raise ValueError(
                "unavailable comeback state cannot contain inferred HUD values"
            )
        return self


class LiveObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        ge=1,
        le=SCHEMA_VERSION,
    )
    raybet_match_id: str = Field(min_length=1)
    map_number: int | None = Field(default=None, ge=1, le=10)
    captured_at_utc: datetime
    game_clock_seconds: int | None = Field(default=None, ge=-180, le=18_000)
    is_paused: bool | None = None
    radiant_hero_ids: list[int] = Field(default_factory=list, max_length=5)
    dire_hero_ids: list[int] = Field(default_factory=list, max_length=5)
    radiant_team_side: Literal["team_one", "team_two"] | None = None
    clock_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    draft_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_frame_ref: str
    source_frame_sha256: str | None = None
    source_frame_bytes: int | None = Field(default=None, gt=0)
    source_frame_path: str | None = None
    screen_state: str = "unknown"
    comeback_state: ComebackState = Field(default_factory=ComebackState.unavailable)

    @field_validator("captured_at_utc")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def unique_heroes(self) -> "LiveObservation":
        heroes = self.radiant_hero_ids + self.dire_hero_ids
        if any(hero_id <= 0 for hero_id in heroes):
            raise ValueError("hero IDs must be positive")
        if len(heroes) != len(set(heroes)):
            raise ValueError("hero IDs must be unique")
        integrity = (
            self.source_frame_sha256,
            self.source_frame_bytes,
            self.source_frame_path,
        )
        if any(value is not None for value in integrity) and any(
            value is None for value in integrity
        ):
            raise ValueError("vision frame integrity metadata must be complete")
        if all(value is not None for value in integrity):
            digest = str(self.source_frame_sha256)
            if (
                len(digest) != 64
                or digest != digest.casefold()
                or any(character not in "0123456789abcdef" for character in digest)
                or self.source_frame_ref != f"vision-frame:sha256:{digest}"
            ):
                raise ValueError("vision frame reference must match its SHA-256")
            if not str(self.source_frame_path).strip():
                raise ValueError("source_frame_path must be non-empty")
        return self

    @field_validator("source_frame_ref")
    @classmethod
    def non_empty_source_frame_ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_frame_ref must be non-empty")
        return value

    @property
    def is_confirmed(self) -> bool:
        return (
            self.map_number is not None
            and self.game_clock_seconds is not None
            and len(self.radiant_hero_ids) == 5
            and len(self.dire_hero_ids) == 5
            and all(hero_id > 0 for hero_id in self.radiant_hero_ids + self.dire_hero_ids)
            and self.clock_confidence >= 0.9
            and self.draft_confidence >= 0.9
            and bool(self.source_frame_ref.strip())
        )

    @property
    def is_hud_confirmed(self) -> bool:
        """Keep trusted HUD facts independent from full draft confirmation."""
        return (
            self.map_number is not None
            and self.game_clock_seconds is not None
            and self.clock_confidence >= COMEBACK_STATE_MIN_CONFIDENCE
            and self.screen_state == "game"
            and self.comeback_state.status == "available"
            and self.comeback_state.source == "vision_hud"
            and self.comeback_state.confidence >= COMEBACK_STATE_MIN_CONFIDENCE
            and bool(self.source_frame_ref.strip())
        )

    @property
    def is_strategy_ready(self) -> bool:
        return self.is_confirmed and self.radiant_team_side is not None
