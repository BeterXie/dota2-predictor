"""Read raw match data from PostgreSQL into pandas DataFrames."""

import pandas as pd
from sqlalchemy import bindparam, text

from database.engine import build_engine


def _read(
    database_url: str | None,
    query: str,
    ids: list[int] | None = None,
) -> pd.DataFrame:
    engine = build_engine(database_url)
    try:
        statement = text(query)
        params = None
        if ids:
            statement = statement.bindparams(bindparam("ids", expanding=True))
            params = {"ids": ids}
        with engine.connect() as connection:
            return pd.read_sql_query(statement, connection, params=params)
    finally:
        engine.dispose()


def read_matches(database_url: str | None) -> pd.DataFrame:
    """Read all matches with non-null start_time, ordered by start_time."""
    return _read(
        database_url,
        "SELECT * FROM matches WHERE start_time IS NOT NULL ORDER BY start_time",
    )


def read_heroes(database_url: str | None) -> pd.DataFrame:
    """Read hero static data."""
    return _read(database_url, "SELECT * FROM heroes")


def read_players(
    database_url: str | None, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read match_players, optionally filtered to specific match_ids."""
    query = "SELECT * FROM match_players"
    if match_ids:
        query += " WHERE match_id IN :ids"
    return _read(database_url, query, match_ids)


def read_picks_bans(
    database_url: str | None, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read picks_bans, optionally filtered to specific match_ids."""
    query = "SELECT * FROM picks_bans"
    if match_ids:
        query += " WHERE match_id IN :ids"
    return _read(database_url, query, match_ids)


def read_gold_advantage(
    database_url: str | None, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read gold_advantage time series."""
    query = "SELECT * FROM gold_advantage"
    if match_ids:
        query += " WHERE match_id IN :ids"
    return _read(database_url, query + " ORDER BY match_id, time_min", match_ids)


def read_xp_advantage(
    database_url: str | None, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read xp_advantage time series."""
    query = "SELECT * FROM xp_advantage"
    if match_ids:
        query += " WHERE match_id IN :ids"
    return _read(database_url, query + " ORDER BY match_id, time_min", match_ids)


def read_objectives(
    database_url: str | None, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read objectives (building kills, roshan, etc.)."""
    query = "SELECT * FROM objectives"
    if match_ids:
        query += " WHERE match_id IN :ids"
    return _read(database_url, query, match_ids)


def read_teamfights(
    database_url: str | None, match_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read teamfights."""
    query = "SELECT * FROM teamfights"
    if match_ids:
        query += " WHERE match_id IN :ids"
    return _read(database_url, query, match_ids)


def read_teamfight_players(
    database_url: str | None, teamfight_ids: list[int] | None = None
) -> pd.DataFrame:
    """Read teamfight_players, optionally filtered to specific teamfight_ids."""
    query = "SELECT * FROM teamfight_players"
    if teamfight_ids:
        query += " WHERE teamfight_id IN :ids"
    return _read(database_url, query, teamfight_ids)
