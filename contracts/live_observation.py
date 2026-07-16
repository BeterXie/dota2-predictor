"""Versioned contract emitted by RayBet stream vision."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 1


class LiveObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        ge=SCHEMA_VERSION,
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
    screen_state: str = "unknown"

    @field_validator("captured_at_utc")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def unique_heroes(self) -> "LiveObservation":
        heroes = self.radiant_hero_ids + self.dire_hero_ids
        if len(heroes) != len(set(heroes)):
            raise ValueError("hero IDs must be unique")
        return self

    @property
    def is_confirmed(self) -> bool:
        return (
            self.map_number is not None
            and self.game_clock_seconds is not None
            and len(self.radiant_hero_ids) == 5
            and len(self.dire_hero_ids) == 5
            and self.clock_confidence >= 0.9
            and self.draft_confidence >= 0.9
        )

    @property
    def is_strategy_ready(self) -> bool:
        return self.is_confirmed and self.radiant_team_side is not None
