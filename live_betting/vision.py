"""Parse versioned JSONL observations emitted by the local vision watcher."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from contracts.live_observation import ComebackState, LiveObservation, SCHEMA_VERSION


@dataclass(frozen=True)
class VisionComebackState:
    status: str
    source: str | None
    confidence: float
    radiant_kills: int | None
    dire_kills: int | None
    radiant_net_worth: int | None
    dire_net_worth: int | None
    unavailable_reason: str | None
    net_worth_advantage_side: str | None = None
    net_worth_advantage_min: int | None = None
    net_worth_advantage_max: int | None = None

    @classmethod
    def unavailable(
        cls, reason: str = "live_state_not_provided"
    ) -> "VisionComebackState":
        return cls("unavailable", None, 0.0, None, None, None, None, reason)

    @classmethod
    def from_contract(cls, state: ComebackState) -> "VisionComebackState":
        return cls(
            status=state.status,
            source=state.source,
            confidence=state.confidence,
            radiant_kills=state.radiant_kills,
            dire_kills=state.dire_kills,
            radiant_net_worth=state.radiant_net_worth,
            dire_net_worth=state.dire_net_worth,
            unavailable_reason=state.unavailable_reason,
            net_worth_advantage_side=state.net_worth_advantage_side,
            net_worth_advantage_min=state.net_worth_advantage_min,
            net_worth_advantage_max=state.net_worth_advantage_max,
        )

    @property
    def is_available(self) -> bool:
        return self.status == "available"


@dataclass(frozen=True)
class VisionObservation:
    raybet_match_id: str
    map_number: int | None
    captured_at: datetime
    game_clock_seconds: int | None
    is_paused: bool | None
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    clock_confidence: float
    draft_confidence: float
    source_frame_ref: str
    screen_state: str
    radiant_team_side: str | None = None
    source_frame_sha256: str | None = None
    source_frame_bytes: int | None = None
    source_frame_path: str | None = None
    comeback_state: VisionComebackState = field(
        default_factory=VisionComebackState.unavailable
    )

    @property
    def is_confirmed(self) -> bool:
        heroes = self.radiant_hero_ids + self.dire_hero_ids
        return (
            self.map_number is not None
            and self.game_clock_seconds is not None
            and len(self.radiant_hero_ids) == 5
            and len(self.dire_hero_ids) == 5
            and all(type(hero_id) is int and hero_id > 0 for hero_id in heroes)
            and len(set(heroes)) == 10
            and self.clock_confidence >= 0.9
            and self.draft_confidence >= 0.9
            and isinstance(self.source_frame_ref, str)
            and bool(self.source_frame_ref.strip())
        )

    @property
    def is_hud_confirmed(self) -> bool:
        return (
            self.map_number is not None
            and self.game_clock_seconds is not None
            and self.clock_confidence >= 0.9
            and self.screen_state == "game"
            and self.comeback_state.is_available
            and self.comeback_state.source == "vision_hud"
            and self.comeback_state.confidence >= 0.9
            and isinstance(self.source_frame_ref, str)
            and bool(self.source_frame_ref.strip())
        )


def parse_observation(payload: dict) -> VisionObservation:
    schema_version = payload.get("schema_version")
    if schema_version not in {1, 2, 3, SCHEMA_VERSION}:
        raise ValueError(f"unsupported vision schema: {payload.get('schema_version')}")
    normalized = dict(payload)
    if schema_version in {1, 2}:
        normalized["comeback_state"] = ComebackState.unavailable(
            "legacy_schema_live_state_unavailable"
        ).model_dump()
    elif schema_version == 3 and isinstance(normalized.get("comeback_state"), dict):
        normalized["comeback_state"] = {
            **normalized["comeback_state"],
            "radiant_net_worth": None,
            "dire_net_worth": None,
            "net_worth_advantage_side": None,
            "net_worth_advantage_min": None,
            "net_worth_advantage_max": None,
        }
    elif "comeback_state" not in normalized:
        normalized["comeback_state"] = ComebackState.unavailable(
            "live_state_not_provided"
        ).model_dump()
    observation = LiveObservation.model_validate(normalized)
    captured = observation.captured_at_utc
    radiant = tuple(observation.radiant_hero_ids)
    dire = tuple(observation.dire_hero_ids)
    return VisionObservation(
        raybet_match_id=observation.raybet_match_id,
        map_number=observation.map_number,
        captured_at=captured.astimezone(timezone.utc),
        game_clock_seconds=observation.game_clock_seconds,
        is_paused=observation.is_paused,
        radiant_hero_ids=radiant,
        dire_hero_ids=dire,
        clock_confidence=observation.clock_confidence,
        draft_confidence=observation.draft_confidence,
        source_frame_ref=observation.source_frame_ref,
        screen_state=observation.screen_state,
        radiant_team_side=observation.radiant_team_side,
        source_frame_sha256=observation.source_frame_sha256,
        source_frame_bytes=observation.source_frame_bytes,
        source_frame_path=observation.source_frame_path,
        comeback_state=VisionComebackState.from_contract(
            observation.comeback_state
        ),
    )


def read_jsonl(path: str | Path) -> list[VisionObservation]:
    output = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                output.append(parse_observation(json.loads(line)))
    return output
