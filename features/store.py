"""Write feature DataFrames to Parquet files and DB materialized tables."""

from pathlib import Path

import pandas as pd
from sqlalchemy import text

from database.engine import build_engine


_PARQUET_DTYPE_MAP = {
    "match_features": {
        "match_id": "int64",
        "duration": "int32",
        "radiant_win": "bool",
        "first_blood_radiant": "bool",
        "first_blood_time": "int32",
        "radiant_gold_adv_10min": "int32",
        "radiant_xp_adv_10min": "int32",
        "radiant_gold_adv_max": "int32",
        "radiant_gold_adv_min": "int32",
        "radiant_gold_adv_mean": "float64",
        "gold_adv_swings": "int32",
        "radiant_tower_kills": "int32",
        "dire_tower_kills": "int32",
        "radiant_barracks_kills": "int32",
        "dire_barracks_kills": "int32",
        "radiant_first_tower_time": "int32",
        "dire_first_tower_time": "int32",
        "teamfight_count": "int32",
        "radiant_teamfight_wins": "int32",
        "radiant_tf_kd_ratio": "float64",
        "stomp_value": "int32",
        "comeback_value": "int32",
        "radiant_score": "int32",
        "dire_score": "int32",
        "patch": "int32",
        "radiant_team_id": "int32",
        "dire_team_id": "int32",
        "league_id": "int32",
        "series_id": "int32",
        "h2h_radiant_win_rate": "float64",
        "h2h_match_count": "int32",
    },
    "team_features": {
        "match_id": "int64",
        "is_radiant": "bool",
        "team_id": "int32",
        "total_kills": "int32",
        "total_deaths": "int32",
        "total_assists": "int32",
        "avg_gpm": "float64",
        "avg_xpm": "float64",
        "total_net_worth": "int32",
        "total_last_hits": "int32",
        "total_denies": "int32",
        "gpm_std": "float64",
        "max_net_worth": "int32",
        "total_hero_damage": "int32",
        "first_blood": "bool",
        "team_win_rate_10": "float64",
        "team_avg_gpm_10": "float64",
        "team_avg_xpm_10": "float64",
        "team_net_worth_lead_10min_10": "float64",
        "team_win_rate_20": "float64",
        "team_avg_gpm_20": "float64",
        "team_avg_xpm_20": "float64",
        "team_net_worth_lead_10min_20": "float64",
        "team_win_rate_50": "float64",
        "team_avg_gpm_50": "float64",
        "team_avg_xpm_50": "float64",
        "team_net_worth_lead_10min_50": "float64",
    },
    "hero_features": {
        "match_id": "int64",
        "hero_id": "int32",
        "player_slot": "int32",
        "is_radiant": "bool",
        "team_id": "int32",
        "kills": "int32",
        "deaths": "int32",
        "assists": "int32",
        "gpm": "int32",
        "xpm": "int32",
        "net_worth": "int32",
        "last_hits": "int32",
        "denies": "int32",
        "hero_damage": "int32",
        "hero_healing": "int32",
        "tower_damage": "int32",
        "level": "int32",
        "role": "int32",
        "hero_win_rate_patch": "float64",
        "hero_avg_gpm_patch": "float64",
        "hero_pick_rate_patch": "float64",
        "hero_ban_rate_patch": "float64",
    },
    "draft_features": {
        "match_id": "int64",
        "order": "int32",
        "is_pick": "bool",
        "hero_id": "int32",
        "team": "int32",
        "phase": "str",
    },
}


def _apply_dtypes(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Cast columns to the expected types for a given feature table."""
    dtype_map = _PARQUET_DTYPE_MAP.get(name)
    if dtype_map is None:
        return df
    for col, dtype in dtype_map.items():
        if col not in df.columns:
            continue
        if dtype == "bool":
            df[col] = df[col].astype(bool)
        else:
            df[col] = df[col].astype(dtype)
    # Keep only the columns defined in the schema, in schema order
    ordered_cols = [c for c in dtype_map if c in df.columns]
    return df[ordered_cols]


def to_parquet(df: pd.DataFrame, name: str, features_dir: str) -> None:
    """Write a feature DataFrame to a Parquet file.

    Args:
        df: DataFrame with feature data.
        name: Base name (e.g. 'match_features') — '.parquet' is appended.
        features_dir: Directory to write into (created if missing).
    """
    out_dir = Path(features_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    typed_df = _apply_dtypes(df, name)
    path = out_dir / f"{name}.parquet"
    typed_df.to_parquet(path, index=False)


def to_db_materialized(
    df: pd.DataFrame, table_name: str, database_url: str | None
) -> None:
    """Replace one Alembic-managed PostgreSQL feature cache atomically.

    Args:
        df: DataFrame with feature data.
        table_name: Target table name (e.g. 'match_feature_cache').
        database_url: PostgreSQL URL; defaults to ``DATABASE_URL``.
    """
    allowed_tables = {
        "match_feature_cache",
        "team_feature_cache",
        "hero_feature_cache",
        "draft_feature_cache",
    }
    if table_name not in allowed_tables:
        raise ValueError(f"unsupported feature cache table: {table_name}")
    # "match_feature_cache" -> "match_features", etc.
    feature_name = table_name.replace("_feature_cache", "_features")
    typed_df = _apply_dtypes(df, feature_name)
    engine = build_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'DELETE FROM "{table_name}"'))
            if not typed_df.empty:
                typed_df.to_sql(
                    table_name,
                    connection,
                    if_exists="append",
                    index=False,
                    method="multi",
                )
    finally:
        engine.dispose()
