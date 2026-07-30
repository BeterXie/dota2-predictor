"""Shared database query functions used by predict, prematch, and features modules.

Consolidates duplicated implementations of team rolling stats, H2H records,
team historical averages, and hero patch statistics.
"""

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from database.engine import build_engine
from database.session import PostgresSession, _bind_parameters

_WINDOW_SIZES: tuple[int, ...] = (10, 20, 50)


class QuerySession(PostgresSession):
    """Short-lived PostgreSQL session that owns its query engine."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def __enter__(self) -> "QuerySession":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        super().close()
        self.engine.dispose()


def connect(database_url: str | None) -> QuerySession:
    return QuerySession(build_engine(database_url))


def _read_sql_query(
    query: str,
    connection: QuerySession,
    parameters: list[object],
) -> pd.DataFrame:
    statement, bound = _bind_parameters(query, parameters)
    with connection.engine.connect() as sql_connection:
        return pd.read_sql_query(text(statement), sql_connection, params=bound)


def safe_float(value) -> float:
    if value is None:
        return np.nan
    if isinstance(value, float) and np.isnan(value):
        return np.nan
    return float(value)


# ---- Team rolling stats -------------------------------------------------------


def empty_team_rolling(
    window_sizes: tuple[int, ...] = _WINDOW_SIZES,
) -> dict:
    d = {}
    for n in window_sizes:
        d.update({
            f"team_win_rate_{n}": np.nan,
            f"team_avg_gpm_{n}": np.nan,
            f"team_avg_xpm_{n}": np.nan,
            f"team_net_worth_lead_10min_{n}": np.nan,
        })
    return d


def compute_team_rolling(
    db_path: str,
    team_id: int,
    before_time: int | None = None,
    window_sizes: tuple[int, ...] = _WINDOW_SIZES,
) -> dict:
    """Compute rolling stats for a single team based on matches before *before_time*."""
    with connect(db_path) as conn:
        params = [team_id, team_id]
        time_filter = ""
        if before_time is not None:
            time_filter = "AND m.start_time < ?"
            params.append(before_time)

        df = _read_sql_query(
            f"""
            SELECT
                m.match_id, m.start_time,
                CASE WHEN m.radiant_team_id = ? THEN 1 ELSE 0 END AS is_radiant,
                CASE
                    WHEN m.radiant_team_id = ? AND m.radiant_win IS TRUE THEN 1
                    WHEN m.dire_team_id  = ? AND m.radiant_win IS FALSE THEN 1
                    ELSE 0
                END AS win,
                COALESCE(ps.avg_gpm, 0) AS avg_gpm,
                COALESCE(ps.avg_xpm, 0) AS avg_xpm,
                COALESCE(
                    CASE WHEN m.radiant_team_id = ? THEN ga.value ELSE -ga.value END, 0
                ) AS net_worth_lead_10min
            FROM matches m
            LEFT JOIN (
                SELECT match_id, is_radiant,
                       AVG(gold_per_min * 1.0) AS avg_gpm,
                       AVG(xp_per_min * 1.0)   AS avg_xpm
                FROM match_players
                GROUP BY match_id, is_radiant
            ) ps ON ps.match_id = m.match_id
                AND ps.is_radiant = (m.radiant_team_id = ?)
            LEFT JOIN gold_advantage ga
                ON ga.match_id = m.match_id AND ga.time_min = 10
            WHERE (m.radiant_team_id = ? OR m.dire_team_id = ?)
                  {time_filter}
            ORDER BY m.start_time DESC
            """,
            conn,
            [team_id, team_id, team_id, team_id, team_id, team_id, team_id]
            + ([before_time] if before_time is not None else []),
        )

    if df.empty:
        return empty_team_rolling(window_sizes)

    result = {}
    for n in window_sizes:
        window = df.head(n)
        k = len(window)
        if k == 0:
            result.update({
                f"team_win_rate_{n}": np.nan,
                f"team_avg_gpm_{n}": np.nan,
                f"team_avg_xpm_{n}": np.nan,
                f"team_net_worth_lead_10min_{n}": np.nan,
            })
        else:
            result[f"team_win_rate_{n}"] = float(window["win"].mean())
            result[f"team_avg_gpm_{n}"] = float(window["avg_gpm"].mean())
            result[f"team_avg_xpm_{n}"] = float(window["avg_xpm"].mean())
            result[f"team_net_worth_lead_10min_{n}"] = float(
                window["net_worth_lead_10min"].mean()
            )
    return result


# ---- H2H ----------------------------------------------------------------------


def compute_h2h(
    db_path: str,
    team_a: int,
    team_b: int,
    before_time: int | None = None,
) -> dict:
    """Compute head-to-head record between *team_a* and *team_b*."""
    with connect(db_path) as conn:
        params: list = []
        time_filter = ""
        if before_time is not None:
            time_filter = "AND start_time < ?"
            params.append(before_time)

        rows = conn.execute(
            f"""
            SELECT radiant_team_id, dire_team_id, radiant_win
            FROM matches
            WHERE (
                (radiant_team_id = ? AND dire_team_id = ?)
                OR (radiant_team_id = ? AND dire_team_id = ?)
            ) {time_filter}
            """,
            [team_a, team_b, team_b, team_a] + params,
        ).fetchall()

    total = len(rows)
    if total == 0:
        return {"h2h_a_win_rate": np.nan, "h2h_match_count": 0}

    wins = 0
    for r in rows:
        if r["radiant_team_id"] == team_a and r["radiant_win"]:
            wins += 1
        elif r["dire_team_id"] == team_a and not r["radiant_win"]:
            wins += 1

    return {"h2h_a_win_rate": float(wins / total), "h2h_match_count": total}


# ---- Team historical averages -------------------------------------------------


def compute_team_historical_averages(
    db_path: str, team_id: int, n_matches: int = 20,
) -> dict:
    """Compute per-match team stat averages over the team's recent matches."""
    conn = connect(db_path)
    try:
        df = _read_sql_query(
            """
            SELECT
                mp.match_id,
                SUM(mp.kills)          AS total_kills,
                SUM(mp.deaths)         AS total_deaths,
                SUM(mp.assists)        AS total_assists,
                AVG(mp.gold_per_min)   AS avg_gpm,
                AVG(mp.xp_per_min)     AS avg_xpm,
                SUM(mp.net_worth)      AS total_net_worth,
                SUM(mp.last_hits)      AS total_last_hits,
                SUM(mp.denies)         AS total_denies,
                SUM(mp.hero_damage)    AS total_hero_damage,
                MAX(mp.net_worth)      AS max_net_worth,
                m.start_time
            FROM match_players mp
            JOIN matches m ON mp.match_id = m.match_id
            WHERE mp.team_id = ?
            GROUP BY mp.match_id
            ORDER BY m.start_time DESC
            LIMIT ?
            """,
            conn,
            [team_id, n_matches],
        )
    finally:
        conn.close()

    if df.empty:
        return {}

    return {
        "team_id": float(team_id),
        "total_kills": float(df["total_kills"].mean()),
        "total_deaths": float(df["total_deaths"].mean()),
        "total_assists": float(df["total_assists"].mean()),
        "avg_gpm": float(df["avg_gpm"].mean()),
        "avg_xpm": float(df["avg_xpm"].mean()),
        "total_net_worth": float(df["total_net_worth"].mean()),
        "total_last_hits": float(df["total_last_hits"].mean()),
        "total_denies": float(df["total_denies"].mean()),
        "total_hero_damage": float(df["total_hero_damage"].mean()),
        "max_net_worth": float(df["max_net_worth"].mean()),
        "gpm_std": 0.0,
        "first_blood": 0.0,
    }


# ---- Utility ------------------------------------------------------------------


def get_current_patch(db_path: str) -> int:
    """Return the latest patch number from the database, or 0 if empty."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT patch FROM matches WHERE patch IS NOT NULL "
            "ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# ---- Hero patch stats ---------------------------------------------------------


def compute_hero_patch_stats(
    db_path: str,
    hero_id: int,
    patch: int,
    before_time: int | None = None,
) -> dict:
    """Compute hero stats for a specific hero in a specific patch.

    Returns ``hero_win_rate_patch``, ``hero_avg_gpm_patch``,
    ``hero_pick_rate_patch``, ``hero_ban_rate_patch``.
    """
    with connect(db_path) as conn:
        params: list = [patch]
        time_filter = ""
        if before_time is not None:
            time_filter = "AND m.start_time < ?"
            params.append(before_time)

        total = conn.execute(
            f"SELECT COUNT(*) FROM matches WHERE patch = ? {time_filter}",
            params,
        ).fetchone()[0]

        if total == 0:
            return {
                "hero_win_rate_patch": np.nan,
                "hero_avg_gpm_patch": np.nan,
                "hero_pick_rate_patch": np.nan,
                "hero_ban_rate_patch": np.nan,
            }

        picks = conn.execute(
            f"""
            SELECT COUNT(DISTINCT mp.match_id)
            FROM match_players mp
            JOIN matches m ON m.match_id = mp.match_id
            WHERE mp.hero_id = ? AND m.patch = ? {time_filter}
            """,
            [hero_id] + params,
        ).fetchone()[0]

        bans = conn.execute(
            f"""
            SELECT COUNT(DISTINCT pb.match_id)
            FROM picks_bans pb
            JOIN matches m ON m.match_id = pb.match_id
            WHERE pb.hero_id = ? AND pb.is_pick IS FALSE
              AND m.patch = ? {time_filter}
            """,
            [hero_id] + params,
        ).fetchone()[0]

        wins = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM match_players mp
            JOIN matches m ON m.match_id = mp.match_id
            WHERE mp.hero_id = ? AND m.patch = ? {time_filter}
              AND (
                (mp.is_radiant IS TRUE AND m.radiant_win IS TRUE)
                OR (mp.is_radiant IS FALSE AND m.radiant_win IS FALSE)
              )
            """,
            [hero_id] + params,
        ).fetchone()[0]

        total_picks = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM match_players mp
            JOIN matches m ON m.match_id = mp.match_id
            WHERE mp.hero_id = ? AND m.patch = ? {time_filter}
            """,
            [hero_id] + params,
        ).fetchone()[0]

        avg_gpm = conn.execute(
            f"""
            SELECT AVG(mp.gold_per_min * 1.0)
            FROM match_players mp
            JOIN matches m ON m.match_id = mp.match_id
            WHERE mp.hero_id = ? AND m.patch = ? {time_filter}
            """,
            [hero_id] + params,
        ).fetchone()[0]

    return {
        "hero_win_rate_patch": float(wins / total_picks) if total_picks else np.nan,
        "hero_avg_gpm_patch": float(avg_gpm) if avg_gpm is not None else np.nan,
        "hero_pick_rate_patch": float(picks / total) if total else np.nan,
        "hero_ban_rate_patch": float(bans / total) if total else np.nan,
    }
