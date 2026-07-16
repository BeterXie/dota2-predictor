"""Parse versioned JSONL observations emitted by the local vision watcher."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from contracts.live_observation import SCHEMA_VERSION


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


def parse_observation(payload: dict) -> VisionObservation:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported vision schema: {payload.get('schema_version')}")
    captured = datetime.fromisoformat(str(payload["captured_at_utc"]).replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("captured_at_utc must be timezone-aware")
    radiant = tuple(int(value) for value in payload.get("radiant_hero_ids") or [])
    dire = tuple(int(value) for value in payload.get("dire_hero_ids") or [])
    if any(hero_id <= 0 for hero_id in radiant + dire):
        raise ValueError("hero IDs must be positive")
    if len(set(radiant + dire)) != len(radiant + dire):
        raise ValueError("hero IDs must be unique")
    radiant_team_side = payload.get("radiant_team_side")
    if radiant_team_side not in {None, "team_one", "team_two"}:
        raise ValueError("radiant_team_side must be team_one, team_two, or null")
    source_frame_ref = str(payload.get("source_frame_ref") or "")
    if not source_frame_ref.strip():
        raise ValueError("source_frame_ref must be non-empty")
    return VisionObservation(
        raybet_match_id=str(payload["raybet_match_id"]),
        map_number=payload.get("map_number"),
        captured_at=captured.astimezone(timezone.utc),
        game_clock_seconds=payload.get("game_clock_seconds"),
        is_paused=payload.get("is_paused"),
        radiant_hero_ids=radiant,
        dire_hero_ids=dire,
        clock_confidence=float(payload.get("clock_confidence") or 0),
        draft_confidence=float(payload.get("draft_confidence") or 0),
        source_frame_ref=source_frame_ref,
        screen_state=str(payload.get("screen_state") or "unknown"),
        radiant_team_side=radiant_team_side,
    )


def read_jsonl(path: str | Path) -> list[VisionObservation]:
    output = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                output.append(parse_observation(json.loads(line)))
    return output
