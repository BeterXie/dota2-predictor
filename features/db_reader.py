"""Read raw match data from SQLite into pandas DataFrames."""

import sqlite3

import pandas as pd
from shared.sqlite import connect as connect_sqlite


def _connect(db_path: str) -> sqlite3.Connection:
    return connect_sqlite(db_path, read_only=True, row_factory=sqlite3.Row)


def read_matches(db_path: str) -> pd.DataFrame:
    """Read all matches with non-null start_time, ordered by start_time."""
    with _connect(db_path) as conn:
        return pd.read_sql_query(
            "SELECT * FROM matches WHERE start_time IS NOT NULL ORDER BY start_time",
            conn,
        )


def read_heroes(db_path: str) -> pd.DataFrame:
    """Read hero static data."""
    with _connect(db_path) as conn:
        return pd.read_sql_query("SELECT * FROM heroes", conn)


def read_players(db_path: str, match_ids: list[int] | None = None) -> pd.DataFrame:
    """Read match_players, optionally filtered to specific match_ids."""
    with _connect(db_path) as conn:
        if match_ids:
            placeholders = ",".join("?" for _ in match_ids)
            return pd.read_sql_query(
                f"SELECT * FROM match_players WHERE match_id IN ({placeholders})",
                conn,
                params=match_ids,
            )
        return pd.read_sql_query("SELECT * FROM match_players", conn)


def read_picks_bans(
    db_path: str, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read picks_bans, optionally filtered to specific match_ids."""
    with _connect(db_path) as conn:
        if match_ids:
            placeholders = ",".join("?" for _ in match_ids)
            return pd.read_sql_query(
                f"SELECT * FROM picks_bans WHERE match_id IN ({placeholders})",
                conn,
                params=match_ids,
            )
        return pd.read_sql_query("SELECT * FROM picks_bans", conn)


def read_gold_advantage(
    db_path: str, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read gold_advantage time series."""
    with _connect(db_path) as conn:
        if match_ids:
            placeholders = ",".join("?" for _ in match_ids)
            return pd.read_sql_query(
                f"SELECT * FROM gold_advantage WHERE match_id IN ({placeholders}) ORDER BY match_id, time_min",
                conn,
                params=match_ids,
            )
        return pd.read_sql_query(
            "SELECT * FROM gold_advantage ORDER BY match_id, time_min", conn
        )


def read_xp_advantage(
    db_path: str, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read xp_advantage time series."""
    with _connect(db_path) as conn:
        if match_ids:
            placeholders = ",".join("?" for _ in match_ids)
            return pd.read_sql_query(
                f"SELECT * FROM xp_advantage WHERE match_id IN ({placeholders}) ORDER BY match_id, time_min",
                conn,
                params=match_ids,
            )
        return pd.read_sql_query(
            "SELECT * FROM xp_advantage ORDER BY match_id, time_min", conn
        )


def read_objectives(
    db_path: str, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read objectives (building kills, roshan, etc.)."""
    with _connect(db_path) as conn:
        if match_ids:
            placeholders = ",".join("?" for _ in match_ids)
            return pd.read_sql_query(
                f"SELECT * FROM objectives WHERE match_id IN ({placeholders})",
                conn,
                params=match_ids,
            )
        return pd.read_sql_query("SELECT * FROM objectives", conn)


def read_teamfights(
    db_path: str, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read teamfights."""
    with _connect(db_path) as conn:
        if match_ids:
            placeholders = ",".join("?" for _ in match_ids)
            return pd.read_sql_query(
                f"SELECT * FROM teamfights WHERE match_id IN ({placeholders})",
                conn,
                params=match_ids,
            )
        return pd.read_sql_query("SELECT * FROM teamfights", conn)


def read_teamfight_players(
    db_path: str, teamfight_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read teamfight_players, optionally filtered to specific teamfight_ids."""
    with _connect(db_path) as conn:
        if teamfight_ids:
            placeholders = ",".join("?" for _ in teamfight_ids)
            return pd.read_sql_query(
                f"SELECT * FROM teamfight_players WHERE teamfight_id IN ({placeholders})",
                conn,
                params=teamfight_ids,
            )
        return pd.read_sql_query("SELECT * FROM teamfight_players", conn)
