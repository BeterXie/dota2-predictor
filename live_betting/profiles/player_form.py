"""Time-decayed, role-aware recent player form."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerForm:
    account_ids: tuple[int, ...]
    score: float
    role_scores: dict[str, float]
    matches: int
    quality: float


def build_player_form(
    connection: sqlite3.Connection,
    account_ids: tuple[int, ...],
    as_of_start_time: int,
    *,
    half_life_days: float = 30.0,
) -> PlayerForm:
    if not account_ids:
        return PlayerForm((), 0.0, {}, 0, 0.0)
    placeholders = ",".join("?" for _ in account_ids)
    rows = connection.execute(
        f"""SELECT mp.account_id, mp.lane_role, mp.kills, mp.deaths, mp.assists,
                   mp.gold_per_min, mp.xp_per_min, m.start_time
            FROM match_players mp JOIN matches m ON m.match_id=mp.match_id
            WHERE mp.account_id IN ({placeholders}) AND m.start_time < ?
            ORDER BY m.start_time DESC""",
        (*account_ids, as_of_start_time),
    ).fetchall()
    weighted: list[tuple[float, float, str]] = []
    per_player: dict[int, int] = {}
    for account_id, role, kills, deaths, assists, gpm, xpm, start_time in rows:
        account_id = int(account_id)
        if per_player.get(account_id, 0) >= 20:
            continue
        per_player[account_id] = per_player.get(account_id, 0) + 1
        age_days = max(0.0, (as_of_start_time - int(start_time)) / 86400)
        weight = 0.5 ** (age_days / half_life_days)
        kda = (int(kills or 0) + int(assists or 0)) / max(1, int(deaths or 0))
        raw = ((float(gpm or 0) - 450) / 220 +
               (float(xpm or 0) - 550) / 280 + (kda - 3.0) / 4) / 3
        weighted.append((math.tanh(raw), weight, str(role or "unknown")))
    if not weighted:
        return PlayerForm(account_ids, 0.0, {}, 0, 0.0)
    total_weight = sum(weight for _, weight, _ in weighted)
    score = sum(value * weight for value, weight, _ in weighted) / total_weight
    role_scores: dict[str, float] = {}
    for role in {row[2] for row in weighted}:
        role_rows = [(value, weight) for value, weight, row_role in weighted if row_role == role]
        role_scores[role] = sum(value * weight for value, weight in role_rows) / sum(
            weight for _, weight in role_rows
        )
    quality = min(1.0, math.sqrt(len(weighted) / max(1, len(account_ids) * 20)))
    return PlayerForm(account_ids, score, role_scores, len(weighted), quality)
