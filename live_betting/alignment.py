"""Causal alignment of RayBet odds snapshots to confirmed visual clocks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .models import OddsSnapshot
from .vision import VisionObservation


MAP_RE = re.compile(r"map_(\d+)")


@dataclass(frozen=True)
class OddsAlignment:
    odds_snapshot_id: int
    raybet_match_id: str
    map_number: int | None
    game_clock_seconds: int | None
    observation_captured_at: datetime | None
    method: str
    lag_seconds: float | None
    usable: bool
    reason: str


def _map_number(period: str) -> int | None:
    match = MAP_RE.fullmatch(period)
    return int(match.group(1)) if match else None


def align_snapshots(
    rows: list[tuple[int, OddsSnapshot]],
    observations: list[VisionObservation],
    *,
    max_projection_seconds: float = 15.0,
) -> list[OddsAlignment]:
    """Align using only observations captured at or before each odds receipt."""
    confirmed = sorted(
        (row for row in observations if row.is_confirmed and row.screen_state == "game"),
        key=lambda row: row.captured_at,
    )
    output: list[OddsAlignment] = []
    cursor = 0
    latest_by_match: dict[str, VisionObservation] = {}
    for snapshot_id, snapshot in sorted(rows, key=lambda row: row[1].received_at):
        while cursor < len(confirmed) and confirmed[cursor].captured_at <= snapshot.received_at:
            latest_by_match[confirmed[cursor].raybet_match_id] = confirmed[cursor]
            cursor += 1
        latest = latest_by_match.get(snapshot.raybet_match_id)
        expected_map = _map_number(snapshot.market.period)
        if latest is None:
            output.append(OddsAlignment(snapshot_id, snapshot.raybet_match_id, expected_map,
                                         None, None, "none", None, False,
                                         "no_prior_confirmed_observation"))
            continue
        lag = (snapshot.received_at - latest.captured_at).total_seconds()
        if expected_map is None or latest.map_number != expected_map:
            output.append(OddsAlignment(snapshot_id, snapshot.raybet_match_id, expected_map,
                                         None, latest.captured_at, "none", lag, False,
                                         "map_mismatch"))
            continue
        if lag < 0:
            output.append(OddsAlignment(snapshot_id, snapshot.raybet_match_id, expected_map,
                                         None, latest.captured_at, "none", lag, False,
                                         "future_observation"))
            continue
        if lag > max_projection_seconds:
            output.append(OddsAlignment(snapshot_id, snapshot.raybet_match_id, expected_map,
                                         None, latest.captured_at, "none", lag, False,
                                         "observation_gap"))
            continue
        if latest.is_paused:
            game_time = latest.game_clock_seconds
            method = "paused_anchor"
        else:
            game_time = int(latest.game_clock_seconds + lag)
            method = "forward_projection" if lag >= 1 else "anchor"
        output.append(OddsAlignment(snapshot_id, snapshot.raybet_match_id, expected_map,
                                     game_time, latest.captured_at, method, lag, True, "ok"))
    return output
