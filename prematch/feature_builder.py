"""Build pre-match feature vector from database and hero lineup.

Queries team historical stats, rolling aggregates, H2H records, and hero patch
statistics, then assembles a feature dict whose keys match the model's expected
feature_names (pre-match subset).
"""

import sqlite3

import numpy as np
import pandas as pd

from shared.queries import (
    compute_h2h,
    compute_hero_patch_stats,
    compute_team_historical_averages,
    compute_team_rolling,
    connect as _db_connect,
    get_current_patch,
    safe_float,
)


def _set_if_present(features: dict, name: str, value: float) -> None:
    if name in features:
        features[name] = value


# ---- Hero patch stats ---------------------------------------------------

def _compute_hero_patch_features(
    db_path: str,
    hero_ids: list[int],
    patch: int,
) -> dict:
    """Compute average hero patch stats for a 5-hero lineup."""
    stats_keys = [
        "hero_win_rate_patch",
        "hero_avg_gpm_patch",
        "hero_pick_rate_patch",
        "hero_ban_rate_patch",
    ]
    accum = {k: 0.0 for k in stats_keys}
    valid = 0

    for hid in hero_ids:
        s = compute_hero_patch_stats(db_path, hid, patch)
        if s.get("hero_win_rate_patch") is not None and not (
            isinstance(s["hero_win_rate_patch"], float)
            and np.isnan(s["hero_win_rate_patch"])
        ):
            for k in stats_keys:
                v = s.get(k, np.nan)
                accum[k] += v if not (isinstance(v, float) and np.isnan(v)) else 0.0
            valid += 1

    if valid == 0:
        return {
            "avg_hero_win_rate_patch": 0.0,
            "avg_hero_avg_gpm_patch": 0.0,
            "avg_hero_pick_rate_patch": 0.0,
            "avg_hero_ban_rate_patch": 0.0,
        }

    return {k: accum[k] / valid for k in stats_keys}


# ---- Hero counter features (from OpenDota matchup data) -----------------


def _compute_hero_advantage(db_path: str, hero_id: int, vs_hero_id: int) -> float:
    """Compute advantage of hero_id vs vs_hero_id from hero_matchups table.

    If synergy is available (from Stratz), use it directly.
    Otherwise compute advantage = win_rate_vs_hero - overall_win_rate.
    """
    with _db_connect(db_path) as conn:
        row = conn.execute(
            "SELECT games_played, wins, synergy FROM hero_matchups "
            "WHERE hero_id = ? AND vs_hero_id = ?",
            (hero_id, vs_hero_id),
        ).fetchone()

    if not row:
        return 0.0

    synergy = row["synergy"]
    if synergy is not None:
        return float(synergy) / 100.0

    with _db_connect(db_path) as conn:
        overall = conn.execute(
            "SELECT SUM(games_played) as total_games, SUM(wins) as total_wins "
            "FROM hero_matchups WHERE hero_id = ?",
            (hero_id,),
        ).fetchone()

    overall_games = overall["total_games"] if overall else 0
    if not overall_games:
        return 0.0

    overall_wr = float(overall["total_wins"] or 0) / overall_games
    vs_games = row["games_played"] or 0
    if vs_games == 0:
        return 0.0

    vs_wr = float(row["wins"] or 0) / vs_games
    return vs_wr - overall_wr


def _compute_hero_counter_features(
    db_path: str,
    radiant_heroes: list[int],
    dire_heroes: list[int],
) -> dict:
    """Compute hero-vs-hero counter features from the hero_matchups table.

    Builds a 5x5 advantage matrix where M[i][j] = advantage of radiant_hero[i]
    vs dire_hero[j], then extracts summary statistics.

    Returns 12 features.
    """
    matrix: list[float] = []
    for rh in radiant_heroes:
        for dh in dire_heroes:
            matrix.append(_compute_hero_advantage(db_path, rh, dh))

    arr = np.array(matrix)
    radiant_avg = float(arr.mean()) if len(arr) > 0 else 0.0
    radiant_min = float(arr.min()) if len(arr) > 0 else 0.0
    radiant_max = float(arr.max()) if len(arr) > 0 else 0.0
    radiant_std = float(arr.std()) if len(arr) > 0 else 0.0

    dire_matrix: list[float] = []
    for dh in dire_heroes:
        for rh in radiant_heroes:
            dire_matrix.append(_compute_hero_advantage(db_path, dh, rh))

    if dire_matrix:
        dire_arr = np.array(dire_matrix)
        dire_avg = float(dire_arr.mean())
        dire_min = float(dire_arr.min())
        dire_max = float(dire_arr.max())
        dire_std = float(dire_arr.std())
    else:
        dire_avg = dire_min = dire_max = dire_std = 0.0

    return {
        "radiant_avg_hero_advantage": radiant_avg,
        "dire_avg_hero_advantage": dire_avg,
        "diff_avg_hero_advantage": radiant_avg - dire_avg,
        "radiant_min_hero_advantage": radiant_min,
        "radiant_max_hero_advantage": radiant_max,
        "dire_min_hero_advantage": dire_min,
        "dire_max_hero_advantage": dire_max,
        "diff_min_hero_advantage": radiant_min - dire_min,
        "diff_max_hero_advantage": radiant_max - dire_max,
        "radiant_hero_advantage_std": radiant_std,
        "dire_hero_advantage_std": dire_std,
        "diff_hero_advantage_std": radiant_std - dire_std,
    }


def _empty_counter_features() -> dict:
    return {
        "radiant_avg_hero_advantage": 0.0,
        "dire_avg_hero_advantage": 0.0,
        "diff_avg_hero_advantage": 0.0,
        "radiant_min_hero_advantage": 0.0,
        "radiant_max_hero_advantage": 0.0,
        "dire_min_hero_advantage": 0.0,
        "dire_max_hero_advantage": 0.0,
        "diff_min_hero_advantage": 0.0,
        "diff_max_hero_advantage": 0.0,
        "radiant_hero_advantage_std": 0.0,
        "dire_hero_advantage_std": 0.0,
        "diff_hero_advantage_std": 0.0,
    }


COUNTER_FEATURE_NAMES = list(_empty_counter_features().keys())


def build_counter_features_for_matches(
    db_path: str,
    match_ids: list[int],
) -> pd.DataFrame:
    """Build hero counter features for a list of historical matches."""
    with _db_connect(db_path) as conn:
        placeholders = ",".join("?" for _ in match_ids)
        rows = conn.execute(
            f"""SELECT match_id, hero_id, is_radiant
                FROM match_players
                WHERE match_id IN ({placeholders})
                ORDER BY match_id, is_radiant DESC""",
            match_ids,
        ).fetchall()

    match_heroes: dict[int, dict[str, list[int]]] = {}
    for r in rows:
        mid = r["match_id"]
        if mid not in match_heroes:
            match_heroes[mid] = {"radiant": [], "dire": []}
        side = "radiant" if r["is_radiant"] else "dire"
        if len(match_heroes[mid][side]) < 5:
            match_heroes[mid][side].append(r["hero_id"])

    records = []
    for mid in sorted(match_ids):
        heroes = match_heroes.get(mid)
        if heroes is None or len(heroes["radiant"]) < 5 or len(heroes["dire"]) < 5:
            row = _empty_counter_features()
        else:
            row = _compute_hero_counter_features(
                db_path, heroes["radiant"][:5], heroes["dire"][:5]
            )
        row["match_id"] = mid
        records.append(row)

    return pd.DataFrame(records).set_index("match_id")


# ---- Main builder --------------------------------------------------------


def build_prematch_features(
    radiant_id: int,
    dire_id: int,
    league_id: int,
    radiant_heroes: list[int],
    dire_heroes: list[int],
    db_path: str,
    feature_names: list[str],
) -> pd.DataFrame:
    """Build a single-row DataFrame whose columns match *feature_names* exactly.

    Args:
        radiant_id: Radiant team ID.
        dire_id: Dire team ID.
        league_id: League ID (0 if unknown).
        radiant_heroes: List of 5 hero IDs for radiant.
        dire_heroes: List of 5 hero IDs for dire.
        db_path: Path to data/dota2.db.
        feature_names: Ordered list of feature names the model expects.

    Returns:
        A (1, N) DataFrame ready for prediction.
    """
    features: dict[str, float] = {name: np.nan for name in feature_names}

    # ---- Identity columns ------------------------------------------------
    _set_if_present(features, "radiant_team_id", float(radiant_id))
    _set_if_present(features, "dire_team_id", float(dire_id))
    _set_if_present(features, "league_id", float(league_id))
    _set_if_present(features, "patch", float(get_current_patch(db_path)))
    _set_if_present(features, "series_id", 0.0)

    # ---- Team rolling stats ----------------------------------------------
    r_rolling = compute_team_rolling(db_path, radiant_id)
    d_rolling = compute_team_rolling(db_path, dire_id)

    for prefix, rolling in [("radiant_", r_rolling), ("dire_", d_rolling)]:
        for key, value in rolling.items():
            feat = f"{prefix}{key}"
            _set_if_present(features, feat, safe_float(value))

    for key in r_rolling:
        diff_feat = f"diff_{key}"
        rv = safe_float(r_rolling.get(key, np.nan))
        dv = safe_float(d_rolling.get(key, np.nan))
        _set_if_present(features, diff_feat, rv - dv)

    # ---- H2H -------------------------------------------------------------
    h2h = compute_h2h(db_path, radiant_id, dire_id)
    _set_if_present(features, "h2h_match_count", float(h2h.get("h2h_match_count", 0)))
    _set_if_present(
        features,
        "h2h_radiant_win_rate",
        safe_float(h2h.get("h2h_a_win_rate", np.nan)),
    )

    # ---- Team historical averages ----------------------------------------
    r_avgs = compute_team_historical_averages(db_path, radiant_id)
    d_avgs = compute_team_historical_averages(db_path, dire_id)

    for prefix, avgs in [("radiant_", r_avgs), ("dire_", d_avgs)]:
        for key, value in avgs.items():
            feat = f"{prefix}{key}"
            _set_if_present(features, feat, safe_float(value))

    for key in r_avgs:
        diff_feat = f"diff_{key}"
        if diff_feat in features:
            rv = safe_float(r_avgs.get(key, np.nan))
            dv = safe_float(d_avgs.get(key, np.nan))
            features[diff_feat] = rv - dv

    # ---- Hero patch stats ------------------------------------------------
    patch = int(get_current_patch(db_path))
    r_hero_stats = _compute_hero_patch_features(db_path, radiant_heroes, patch)
    d_hero_stats = _compute_hero_patch_features(db_path, dire_heroes, patch)

    for prefix, hero_stats in [("radiant_", r_hero_stats), ("dire_", d_hero_stats)]:
        for key, value in hero_stats.items():
            feat = f"{prefix}_{key}"
            _set_if_present(features, feat, safe_float(value))

    for key in r_hero_stats:
        diff_feat = f"diff_{key}"
        if diff_feat in features:
            rv = safe_float(r_hero_stats.get(key, np.nan))
            dv = safe_float(d_hero_stats.get(key, np.nan))
            features[diff_feat] = rv - dv

    # ---- Hero counter features --------------------------------------------
    counter = _compute_hero_counter_features(db_path, radiant_heroes, dire_heroes)
    for key, value in counter.items():
        _set_if_present(features, key, safe_float(value))

    df = pd.DataFrame([features], columns=feature_names)
    # Match training behaviour: NaN → 0 (prematch model has no imputer; XGBoost
    # was trained with fillna(0) so prediction must use the same convention.)
    return df.fillna(0.0)
