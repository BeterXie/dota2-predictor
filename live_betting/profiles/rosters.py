"""Roster-version helpers based on historical match player membership."""

from __future__ import annotations

from database.session import PostgresSession


def latest_roster(
    connection: PostgresSession, team_id: int, as_of_start_time: int
) -> tuple[int, ...]:
    row = connection.execute(
        """SELECT m.match_id FROM matches m
           WHERE m.start_time < ? AND (m.radiant_team_id=? OR m.dire_team_id=?)
           ORDER BY m.start_time DESC LIMIT 1""",
        (as_of_start_time, team_id, team_id),
    ).fetchone()
    if not row:
        return ()
    players = connection.execute(
        """SELECT account_id FROM match_players
           WHERE match_id=? AND team_id=? AND account_id IS NOT NULL
           ORDER BY player_slot""",
        (row[0], team_id),
    ).fetchall()
    return tuple(int(player[0]) for player in players[:5])


def roster_history_weight(
    historical_roster: tuple[int, ...], current_roster: tuple[int, ...]
) -> float:
    if len(historical_roster) < 5 or len(current_roster) < 5:
        return 0.35
    overlap = len(set(historical_roster) & set(current_roster))
    if overlap == 5:
        return 1.0
    if overlap == 4:
        return 0.7
    if overlap == 3:
        return 0.25
    return 0.1
