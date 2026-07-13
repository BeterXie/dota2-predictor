"""Conditional team comeback, throw, closeout, and duration tendencies."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class TeamStyleProfile:
    team_id: int
    matches: int
    comeback_rate: float
    throw_rate: float
    closeout_rate: float
    late_game_rate: float
    average_duration_minutes: float
    quality: float


def _posterior(successes: int, opportunities: int, prior: float, strength: int = 12) -> float:
    return (successes + prior * strength) / (opportunities + strength)


def build_team_style(
    connection: sqlite3.Connection,
    team_id: int,
    as_of_start_time: int,
    *,
    lead_threshold: int = 3000,
    roster_weight: float = 1.0,
) -> TeamStyleProfile:
    rows = connection.execute(
        """SELECT m.match_id, m.duration, m.radiant_team_id, m.radiant_win,
                  g.time_min, g.value
           FROM matches m JOIN gold_advantage g ON g.match_id=m.match_id
           WHERE m.start_time < ?
             AND (m.radiant_team_id=? OR m.dire_team_id=?)
             AND g.time_min IN (10, 20, 30)
           ORDER BY m.start_time DESC""",
        (as_of_start_time, team_id, team_id),
    ).fetchall()
    matches: dict[int, tuple[int, bool]] = {}
    comeback_wins = comeback_chances = 0
    throw_losses = throw_chances = 0
    closeout_wins = closeout_chances = 0
    for match_id, duration, radiant_team_id, radiant_win, _, advantage in rows:
        is_radiant = int(radiant_team_id or 0) == team_id
        won = bool(radiant_win) if is_radiant else not bool(radiant_win)
        signed_lead = int(advantage or 0) if is_radiant else -int(advantage or 0)
        matches[int(match_id)] = (int(duration or 0), won)
        if signed_lead <= -lead_threshold:
            comeback_chances += 1
            comeback_wins += int(won)
        if signed_lead >= lead_threshold:
            throw_chances += 1
            throw_losses += int(not won)
            closeout_chances += 1
            closeout_wins += int(won)
    durations = [duration / 60 for duration, _ in matches.values() if duration > 0]
    late = sum(duration >= 40 for duration in durations)
    count = len(matches)
    quality = min(1.0, math.sqrt(count / 100.0)) * max(0.0, min(1.0, roster_weight))
    return TeamStyleProfile(
        team_id=team_id,
        matches=count,
        comeback_rate=_posterior(comeback_wins, comeback_chances, 0.18),
        throw_rate=_posterior(throw_losses, throw_chances, 0.16),
        closeout_rate=_posterior(closeout_wins, closeout_chances, 0.84),
        late_game_rate=_posterior(late, len(durations), 0.35),
        average_duration_minutes=(sum(durations) / len(durations)) if durations else 36.0,
        quality=quality,
    )
