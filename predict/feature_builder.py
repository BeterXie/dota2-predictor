"""Build pre-match feature vector from database.

Queries team historical stats, rolling aggregates, and H2H records, then
assembles a feature dict whose keys match the model's expected feature_names.
"""

import sqlite3

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Inline query helpers (duplicated from features/aggregator to keep modules
# independent — no cross-module imports)
# ---------------------------------------------------------------------------

_WINDOW_SIZES = (10, 20, 50)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _empty_team_rolling(window_sizes: tuple[int, ...] = _WINDOW_SIZES) -> dict:
    d = {}
    for n in window_sizes:
        d.update({
            f"team_win_rate_{n}": np.nan,
            f"team_avg_gpm_{n}": np.nan,
            f"team_avg_xpm_{n}": np.nan,
            f"team_net_worth_lead_10min_{n}": np.nan,
        })
    return d


def _compute_team_rolling(
    db_path: str,
    team_id: int,
    before_time: int | None = None,
    window_sizes: tuple[int, ...] = _WINDOW_SIZES,
) -> dict:
    """Compute rolling stats for a single team based on matches before *before_time*."""
    with _connect(db_path) as conn:
        params = [team_id, team_id]
        time_filter = ""
        if before_time is not None:
            time_filter = "AND m.start_time < ?"
            params.append(before_time)

        df = pd.read_sql_query(
            f"""
            SELECT
                m.match_id, m.start_time,
                CASE WHEN m.radiant_team_id = ? THEN 1 ELSE 0 END AS is_radiant,
                CASE
                    WHEN m.radiant_team_id = ? AND m.radiant_win = 1 THEN 1
                    WHEN m.dire_team_id  = ? AND m.radiant_win = 0 THEN 1
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
                AND ps.is_radiant = (CASE WHEN m.radiant_team_id = ? THEN 1 ELSE 0 END)
            LEFT JOIN gold_advantage ga
                ON ga.match_id = m.match_id AND ga.time_min = 10
            WHERE (m.radiant_team_id = ? OR m.dire_team_id = ?)
                  {time_filter}
            ORDER BY m.start_time DESC
            """,
            conn,
            params=[team_id, team_id, team_id, team_id, team_id, team_id, team_id]
            + ([before_time] if before_time is not None else []),
        )

    if df.empty:
        return _empty_team_rolling(window_sizes)

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


def _compute_h2h(
    db_path: str,
    team_a: int,
    team_b: int,
    before_time: int | None = None,
) -> dict:
    """Compute head-to-head record between *team_a* and *team_b*."""
    with _connect(db_path) as conn:
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


def get_current_patch(db_path: str) -> int:
    """Return the latest patch number from the database, or 0 if empty."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT patch FROM matches WHERE patch IS NOT NULL ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _get_team_historical_averages(db_path: str, team_id: int, n_matches: int = 20) -> dict:
    """Compute per-match team stat averages over the team's recent matches.

    Returns a dict with keys matching the unpivoted team_features column names
    (e.g. total_kills, avg_gpm, gpm_std, etc.).
    """
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
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
            params=[team_id, n_matches],
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
    features: dict[str, float] = {name: 0.0 for name in feature_names}

    # -- Identity columns -------------------------------------------------------
    _set_if_present(features, "radiant_team_id", float(radiant_id))
    _set_if_present(features, "dire_team_id", float(dire_id))
    _set_if_present(features, "league_id", float(league_id))
    _set_if_present(features, "patch", float(get_current_patch(db_path)))
    _set_if_present(features, "series_id", 0.0)

    # -- Team rolling stats (win_rate, avg_gpm, avg_xpm, net_worth_lead) --------
    r_rolling = _compute_team_rolling(db_path, radiant_id)
    d_rolling = _compute_team_rolling(db_path, dire_id)

    for prefix, rolling in [("radiant_", r_rolling), ("dire_", d_rolling)]:
        for key, value in rolling.items():
            feat = f"{prefix}{key}"
            _set_if_present(features, feat, _safe_float(value))

    # diff columns for rolling stats
    for key in r_rolling:
        diff_feat = f"diff_{key}"
        rv = _safe_float(r_rolling.get(key, np.nan))
        dv = _safe_float(d_rolling.get(key, np.nan))
        _set_if_present(features, diff_feat, rv - dv)

    # -- H2H -------------------------------------------------------------------
    h2h = _compute_h2h(db_path, radiant_id, dire_id)
    _set_if_present(features, "h2h_match_count", float(h2h.get("h2h_match_count", 0)))
    _set_if_present(
        features,
        "h2h_radiant_win_rate",
        _safe_float(h2h.get("h2h_a_win_rate", np.nan)),
    )

    # -- Team historical averages (per-match stats) -----------------------------
    r_avgs = _get_team_historical_averages(db_path, radiant_id)
    d_avgs = _get_team_historical_averages(db_path, dire_id)

    for prefix, avgs in [("radiant_", r_avgs), ("dire_", d_avgs)]:
        for key, value in avgs.items():
            feat = f"{prefix}{key}"
            _set_if_present(features, feat, _safe_float(value))

    for key in r_avgs:
        diff_feat = f"diff_{key}"
        if diff_feat in features:
            rv = _safe_float(r_avgs.get(key, 0))
            dv = _safe_float(d_avgs.get(key, 0))
            features[diff_feat] = rv - dv

    return pd.DataFrame([features], columns=feature_names)


def _set_if_present(features: dict, name: str, value: float) -> None:
    if name in features:
        features[name] = value


def _safe_float(value) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0.0
    return float(value)
