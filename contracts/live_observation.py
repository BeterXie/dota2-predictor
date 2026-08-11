"""Versioned contract emitted by RayBet stream vision."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 5
COMEBACK_STATE_MIN_CONFIDENCE = 0.9
MAP_START_EVIDENCE_WINDOW_SECONDS = 180
PLAYER_NAME_MIN_CONFIDENCE = 0.7


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


class DraftPlayerNameplate(BaseModel):
    """OCR evidence for one left-to-right draft card, not a role position."""

    model_config = ConfigDict(extra="forbid")

    side: Literal["radiant", "dire"]
    visual_slot: int = Field(ge=1, le=5)
    source: Literal["vision_ocr"] = "vision_ocr"
    hero_id: int | None = Field(default=None, gt=0)
    raw_text: str | None = None
    observed_text: str | None = None
    verified_player_name: str | None = None
    identity_source_url: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    unavailable_reason: str | None = "ocr_not_run"

    @model_validator(mode="after")
    def accepted_name_requires_confident_ocr(self) -> "DraftPlayerNameplate":
        if self.raw_text is not None:
            self.raw_text = " ".join(self.raw_text.split()) or None
        if self.observed_text is not None:
            self.observed_text = " ".join(self.observed_text.split()) or None
        if self.verified_player_name is not None:
            self.verified_player_name = (
                " ".join(self.verified_player_name.split()) or None
            )
        if self.observed_text is not None:
            if (
                self.raw_text != self.observed_text
                or self.confidence < PLAYER_NAME_MIN_CONFIDENCE
                or self.unavailable_reason is not None
            ):
                raise ValueError(
                    "observed player text requires matching confident OCR evidence"
                )
        elif not isinstance(self.unavailable_reason, str) or not self.unavailable_reason.strip():
            raise ValueError("unavailable player nameplate requires a reason")
        if self.verified_player_name is not None:
            if (
                self.verified_player_name != self.observed_text
                or not isinstance(self.identity_source_url, str)
                or not self.identity_source_url.startswith(("https://", "http://"))
            ):
                raise ValueError(
                    "verified player name requires exact observed text and a source URL"
                )
        elif self.identity_source_url is not None:
            raise ValueError("unverified player name cannot contain an identity source")
        return self


class DraftPlayerNames(BaseModel):
    """Ten visual nameplates observed on one completed draft frame."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "partial", "unavailable"] = "unavailable"
    source: Literal["vision_ocr"] | None = None
    slots: list[DraftPlayerNameplate] = Field(default_factory=list, max_length=10)
    unavailable_reason: str | None = "draft_player_names_not_observed"

    @classmethod
    def unavailable(
        cls, reason: str = "draft_player_names_not_observed"
    ) -> "DraftPlayerNames":
        return cls(unavailable_reason=reason)

    @model_validator(mode="after")
    def status_matches_slots(self) -> "DraftPlayerNames":
        identities = {(slot.side, slot.visual_slot) for slot in self.slots}
        if len(identities) != len(self.slots):
            raise ValueError("draft player nameplate visual slots must be unique")
        accepted = sum(slot.observed_text is not None for slot in self.slots)
        if self.status == "available":
            if (
                self.source != "vision_ocr"
                or len(self.slots) != 10
                or accepted != 10
                or self.unavailable_reason is not None
            ):
                raise ValueError("available draft player names require ten accepted slots")
            return self
        if self.status == "partial":
            if (
                self.source != "vision_ocr"
                or len(self.slots) != 10
                or not 0 < accepted < 10
                or not isinstance(self.unavailable_reason, str)
                or not self.unavailable_reason.strip()
            ):
                raise ValueError("partial draft player names require ten audited slots")
            return self
        if (
            self.source not in ({None} if not self.slots else {"vision_ocr"})
            or accepted != 0
            or not isinstance(self.unavailable_reason, str)
            or not self.unavailable_reason.strip()
        ):
            raise ValueError("unavailable draft player names cannot contain accepted names")
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
    draft_player_names: DraftPlayerNames = Field(
        default_factory=DraftPlayerNames.unavailable
    )
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
    def is_draft_confirmed(self) -> bool:
        """Confirm a complete BP frame without granting live HUD authority."""
        return (
            self.map_number is not None
            and self.screen_state == "draft"
            and self.game_clock_seconds is None
            and self.is_paused is None
            and len(self.radiant_hero_ids) == 5
            and len(self.dire_hero_ids) == 5
            and all(
                hero_id > 0
                for hero_id in self.radiant_hero_ids + self.dire_hero_ids
            )
            and self.radiant_team_side in {"team_one", "team_two"}
            and self.clock_confidence == 0.0
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
