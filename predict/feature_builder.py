"""Build pre-match feature vector from database.

Queries team historical stats, rolling aggregates, and H2H records, then
assembles a feature dict whose keys match the model's expected feature_names.
"""

import numpy as np
import pandas as pd

from shared.queries import (
    compute_h2h,
    compute_team_historical_averages,
    compute_team_rolling,
    get_current_patch,
    safe_float,
)


def build_features(
    radiant_id: int,
    dire_id: int,
    league_id: int,
    db_path: str,
    feature_names: list[str],
) -> pd.DataFrame:
    """Build a single-row DataFrame whose columns match *feature_names* exactly.

    Args:
        radiant_id: Radiant team ID.
        dire_id: Dire team ID.
        league_id: League ID (0 if unknown).
        db_path: Path to data/dota2.db.
        feature_names: Ordered list of feature names the model expects.

    Returns:
        A (1, N) DataFrame ready for prediction.
    """
    features: dict[str, float] = {name: np.nan for name in feature_names}

    # -- Identity columns -------------------------------------------------------
    _set_if_present(features, "radiant_team_id", float(radiant_id))
    _set_if_present(features, "dire_team_id", float(dire_id))
    _set_if_present(features, "league_id", float(league_id))
    _set_if_present(features, "patch", float(get_current_patch(db_path)))
    _set_if_present(features, "series_id", 0.0)

    # -- Team rolling stats (win_rate, avg_gpm, avg_xpm, net_worth_lead) --------
    r_rolling = compute_team_rolling(db_path, radiant_id)
    d_rolling = compute_team_rolling(db_path, dire_id)

    for prefix, rolling in [("radiant_", r_rolling), ("dire_", d_rolling)]:
        for key, value in rolling.items():
            feat = f"{prefix}{key}"
            _set_if_present(features, feat, safe_float(value))

    # diff columns for rolling stats
    for key in r_rolling:
        diff_feat = f"diff_{key}"
        rv = safe_float(r_rolling.get(key, np.nan))
        dv = safe_float(d_rolling.get(key, np.nan))
        _set_if_present(features, diff_feat, rv - dv)

    # -- H2H -------------------------------------------------------------------
    h2h = compute_h2h(db_path, radiant_id, dire_id)
    _set_if_present(features, "h2h_match_count", float(h2h.get("h2h_match_count", 0)))
    _set_if_present(
        features,
        "h2h_radiant_win_rate",
        safe_float(h2h.get("h2h_a_win_rate", np.nan)),
    )

    # -- Team historical averages (per-match stats) -----------------------------
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

    df = pd.DataFrame([features], columns=feature_names)
    # Match training behaviour: NaN → 0 (prematch model has no imputer; XGBoost
    # was trained with fillna(0) so prediction must use the same convention.)
    return df.fillna(0.0)


def _set_if_present(features: dict, name: str, value: float) -> None:
    if name in features:
        features[name] = value
