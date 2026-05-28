"""Join feature tables into a training matrix.

Reads data/features/*.parquet, pivots/aggregates to one row per match,
imputes missing values, and provides time-based train/test splits.
"""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.model_selection import TimeSeriesSplit


def _read_parquet(path: str) -> pd.DataFrame:
    """Read a parquet file, return empty DataFrame with warning if missing."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Feature file not found: {p}")
    return pd.read_parquet(p)


def _pivot_team_features(team_df: pd.DataFrame) -> pd.DataFrame:
    """Pivot team_features (2 rows/match) into one row with radiant_/dire_ prefixes.

    Returns a DataFrame indexed by match_id with columns like radiant_avg_gpm,
    dire_avg_gpm, diff_avg_gpm, etc.
    """
    if team_df.empty:
        return pd.DataFrame()

    radiant = team_df[team_df["is_radiant"]].copy()
    dire = team_df[~team_df["is_radiant"]].copy()

    drop_cols = ["match_id", "is_radiant"]
    rad_cols = [c for c in radiant.columns if c not in drop_cols]
    dir_cols = [c for c in dire.columns if c not in drop_cols]

    radiant = radiant.set_index("match_id")[rad_cols].add_prefix("radiant_")
    dire = dire.set_index("match_id")[dir_cols].add_prefix("dire_")

    pivoted = radiant.join(dire, how="inner")

    # Create diff features (radiant - dire) for numeric columns only
    for col in rad_cols:
        r_col = f"radiant_{col}"
        d_col = f"dire_{col}"
        if r_col in pivoted.columns and d_col in pivoted.columns:
            if pd.api.types.is_numeric_dtype(pivoted[r_col]) and not pd.api.types.is_bool_dtype(pivoted[r_col]):
                pivoted[f"diff_{col}"] = pivoted[r_col].astype(float) - pivoted[d_col].astype(float)

    return pivoted


def _aggregate_hero_features(hero_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate hero_features (10 rows/match) into match-level stats.

    Computes mean per side + radiant/dire diffs.
    Returns DataFrame indexed by match_id.
    """
    if hero_df.empty:
        return pd.DataFrame()

    id_cols = ["match_id", "hero_id", "player_slot", "team_id", "role"]
    agg_cols = [
        "kills", "deaths", "assists", "gpm", "xpm", "net_worth",
        "last_hits", "denies", "hero_damage", "hero_healing", "tower_damage",
        "level", "hero_win_rate_patch", "hero_avg_gpm_patch",
        "hero_pick_rate_patch", "hero_ban_rate_patch",
    ]
    agg_cols = [c for c in agg_cols if c in hero_df.columns]

    results = []
    for match_id, grp in hero_df.groupby("match_id"):
        rad = grp[grp["is_radiant"]]
        dire = grp[~grp["is_radiant"]]
        row = {}

        for col in agg_cols:
            r_val = rad[col].mean() if len(rad) > 0 else np.nan
            d_val = dire[col].mean() if len(dire) > 0 else np.nan
            row[f"radiant_avg_{col}"] = r_val
            row[f"dire_avg_{col}"] = d_val
            if pd.api.types.is_numeric_dtype(grp[col]):
                row[f"diff_avg_{col}"] = r_val - d_val

        # Per-role stats (radiant only)
        for role in range(1, 6):
            role_rad = rad[rad["role"] == role]
            if len(role_rad) > 0:
                row[f"radiant_role{role}_gpm"] = role_rad["gpm"].iloc[0]
                row[f"radiant_role{role}_xpm"] = role_rad["xpm"].iloc[0]
                row[f"radiant_role{role}_net_worth"] = role_rad["net_worth"].iloc[0]
                row[f"radiant_role{role}_kills"] = role_rad["kills"].iloc[0]
                row[f"radiant_role{role}_deaths"] = role_rad["deaths"].iloc[0]
                row[f"radiant_role{role}_assists"] = role_rad["assists"].iloc[0]
            else:
                for m in ["gpm", "xpm", "net_worth", "kills", "deaths", "assists"]:
                    row[f"radiant_role{role}_{m}"] = np.nan

        row["match_id"] = match_id
        results.append(row)

    agg = pd.DataFrame(results).set_index("match_id")
    return agg


def _aggregate_draft_features(draft_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate draft_features (24 rows/match) into match-level stats.

    Returns DataFrame indexed by match_id with draft-order-derived features.
    """
    if draft_df.empty:
        return pd.DataFrame()

    results = []
    for match_id, grp in draft_df.groupby("match_id"):
        row = {}
        picks = grp[grp["is_pick"]]
        bans = grp[~grp["is_pick"]]

        # Pick/ban counts per side
        row["radiant_pick_count"] = int((picks["team"] == 0).sum())
        row["dire_pick_count"] = int((picks["team"] == 1).sum())
        row["radiant_ban_count"] = int((bans["team"] == 0).sum())
        row["dire_ban_count"] = int((bans["team"] == 1).sum())

        # Average pick order per side
        rad_picks = picks[picks["team"] == 0]
        dir_picks = picks[picks["team"] == 1]
        row["radiant_avg_pick_order"] = rad_picks["order"].mean() if len(rad_picks) > 0 else np.nan
        row["dire_avg_pick_order"] = dir_picks["order"].mean() if len(dir_picks) > 0 else np.nan

        # First/last pick advantage
        if len(rad_picks) > 0 and len(dir_picks) > 0:
            row["radiant_first_pick"] = int(rad_picks["order"].min() < dir_picks["order"].min())
            row["radiant_last_pick"] = int(rad_picks["order"].max() > dir_picks["order"].max())

        # Hero diversity (unique heroes picked)
        row["radiant_unique_heroes"] = rad_picks["hero_id"].nunique() if len(rad_picks) > 0 else 0
        row["dire_unique_heroes"] = dir_picks["hero_id"].nunique() if len(dir_picks) > 0 else 0

        row["match_id"] = match_id
        results.append(row)

    agg = pd.DataFrame(results).set_index("match_id")
    return agg


def build_training_data(
    features_dir: str,
    db_path: str,
) -> tuple[pd.DataFrame, pd.Series, list[str], pd.Series]:
    """Read, join, and impute all feature tables.

    Returns:
        X: Feature matrix (DataFrame)
        y: Target labels (Series, 1 = radiant win)
        feature_names: List of column names
        start_times: Series indexed by match_id with unix timestamps
    """
    # Load feature tables
    match_df = _read_parquet(str(Path(features_dir) / "match_features.parquet"))
    team_df = _read_parquet(str(Path(features_dir) / "team_features.parquet"))
    hero_df = _read_parquet(str(Path(features_dir) / "hero_features.parquet"))
    draft_df = _read_parquet(str(Path(features_dir) / "draft_features.parquet"))

    # Build match-level features
    # Pivot team features
    team_pivoted = _pivot_team_features(team_df)

    # Aggregate hero features
    hero_agg = _aggregate_hero_features(hero_df)

    # Aggregate draft features
    draft_agg = _aggregate_draft_features(draft_df)

    # Join everything on match_id
    match_indexed = match_df.set_index("match_id")
    y = match_indexed["radiant_win"].astype(int)
    X = match_indexed.drop(columns=["radiant_win"])

    for side_df, suffix in [(team_pivoted, "team"), (hero_agg, "hero"), (draft_agg, "draft")]:
        if not side_df.empty:
            # Drop columns that already exist in X to avoid overlap errors
            overlap = [c for c in side_df.columns if c in X.columns]
            X = X.join(side_df.drop(columns=overlap), how="left")

    # Get start_time from database
    start_times = _get_start_times(db_path, X.index.tolist())

    # Store feature names before imputation
    feature_names = list(X.columns)

    # Drop columns that are all-NaN (no signal, happens with rolling stats when
    # there are too few matches)
    all_nan_cols = X.columns[X.isna().all()].tolist()
    if all_nan_cols:
        X = X.drop(columns=all_nan_cols)
        feature_names = [c for c in feature_names if c not in all_nan_cols]

    # Median imputation for remaining missing values
    has_missing = X.isna().any().any()
    if has_missing:
        imputer = SimpleImputer(strategy="median")
        X_imputed = pd.DataFrame(
            imputer.fit_transform(X),
            columns=X.columns,
            index=X.index,
        )
    else:
        imputer = None
        X_imputed = X.copy()

    # Ensure start_times aligns with X
    start_times = start_times.reindex(X_imputed.index)

    return X_imputed, y, feature_names, start_times, imputer


def _get_start_times(db_path: str, match_ids: list[int]) -> pd.Series:
    """Fetch match start_times from the database."""
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in match_ids)
        rows = conn.execute(
            f"SELECT match_id, start_time FROM matches WHERE match_id IN ({placeholders})",
            match_ids,
        ).fetchall()
        return pd.Series(
            {r[0]: r[1] for r in rows},
            name="start_time",
        )
    finally:
        conn.close()


def split_train_test(
    X: pd.DataFrame,
    y: pd.Series,
    start_times: pd.Series,
    test_size: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """Time-based split: earlier matches for training, later for testing.

    Sorts by start_time ascending, then takes the latest `test_size` fraction
    as the test set. This prevents future information from leaking into training.
    """
    order = start_times.sort_values()
    n_test = max(1, int(len(order) * test_size))
    test_ids = order.index[-n_test:].tolist()
    train_ids = order.index[:-n_test].tolist()

    if not train_ids:
        # Edge case: too few matches — train on all, test on same (report accordingly)
        train_ids = test_ids

    X_train, X_test = X.loc[train_ids], X.loc[test_ids]
    y_train, y_test = y.loc[train_ids], y.loc[test_ids]

    return X_train, X_test, y_train, y_test
