"""Cross-match aggregated features — rolling stats, hero patch stats, H2H records.

Computes team rolling windows (10/20/50 matches), hero per-patch statistics,
and head-to-head records between teams. All temporal aggregations use only
matches that occurred BEFORE the current match's start_time to prevent data
leakage.

Single-entity query functions are imported from shared.queries.
"""

from collections import defaultdict

import numpy as np
import pandas as pd
from sqlalchemy import text

from shared.queries import (
    connect as _connect,
    empty_team_rolling,
)

_WINDOW_SIZES = (10, 20, 50)


def _read_batch(connection, query: str) -> pd.DataFrame:
    """Read a batch through SQLAlchemy, which Pandas can consume directly."""
    with connection.engine.connect() as sql_connection:
        return pd.read_sql_query(text(query), sql_connection)


# ---------------------------------------------------------------------------
# Batch computation — enriches feature DataFrames with aggregated columns
# ---------------------------------------------------------------------------


def _empty_hero_patch() -> dict:
    return {
        "hero_win_rate_patch": np.nan,
        "hero_avg_gpm_patch": np.nan,
        "hero_pick_rate_patch": np.nan,
        "hero_ban_rate_patch": np.nan,
    }


def compute_and_merge_aggregates(
    matches_df: pd.DataFrame,
    team_df: pd.DataFrame,
    hero_df: pd.DataFrame,
    db_path: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Compute all aggregated features and merge into the feature DataFrames.

    Returns enriched (matches_df, team_df, hero_df) with rolling / patch / H2H
    columns added.
    """
    # --- Load supporting data from DB ---------------------------------------
    with _connect(db_path) as conn:
        raw_matches = _read_batch(
            conn,
            "SELECT match_id, radiant_team_id, dire_team_id, radiant_win, "
            "start_time, patch FROM matches WHERE start_time IS NOT NULL "
            "ORDER BY start_time",
        )
        # Per-match per-side player averages
        player_avgs = _read_batch(
            conn,
            "SELECT match_id, is_radiant, "
            "AVG(gold_per_min * 1.0) AS avg_gpm, "
            "AVG(xp_per_min * 1.0) AS avg_xpm "
            "FROM match_players GROUP BY match_id, is_radiant",
        )
        # Gold advantage at 10 min
        gold_10 = _read_batch(
            conn,
            "SELECT match_id, value FROM gold_advantage WHERE time_min = 10",
        )
        # All picks
        all_picks = _read_batch(
            conn,
            "SELECT mp.match_id, mp.hero_id, mp.gold_per_min, mp.is_radiant "
            "FROM match_players mp",
        )
        # All bans
        all_bans = _read_batch(
            conn,
            "SELECT pb.match_id, pb.hero_id "
            "FROM picks_bans pb WHERE pb.is_pick IS FALSE",
        )

    if raw_matches.empty:
        return matches_df, team_df, hero_df

    raw_matches = raw_matches.merge(
        gold_10.rename(columns={"value": "gold_adv_10"}), on="match_id", how="left"
    )
    raw_matches["gold_adv_10"] = raw_matches["gold_adv_10"].fillna(0).astype(float)

    # --- Build team-match history -------------------------------------------
    # One row per (match_id, team_id) with per-match stats for rolling windows.
    radiant_side = raw_matches.assign(
        team_id=raw_matches["radiant_team_id"],
        is_radiant=True,
        win=lambda df: df["radiant_win"].astype(int),
        net_worth_lead_10min=lambda df: df["gold_adv_10"],
    )[
        [
            "match_id",
            "team_id",
            "is_radiant",
            "start_time",
            "patch",
            "win",
            "net_worth_lead_10min",
        ]
    ]
    dire_side = raw_matches.assign(
        team_id=raw_matches["dire_team_id"],
        is_radiant=False,
        win=lambda df: (~df["radiant_win"]).astype(int),
        net_worth_lead_10min=lambda df: -df["gold_adv_10"],
    )[
        [
            "match_id",
            "team_id",
            "is_radiant",
            "start_time",
            "patch",
            "win",
            "net_worth_lead_10min",
        ]
    ]

    team_history = pd.concat([radiant_side, dire_side], ignore_index=True)

    # Merge GPM / XPM from player_avgs
    team_history = team_history.merge(
        player_avgs, on=["match_id", "is_radiant"], how="left"
    )
    team_history["avg_gpm"] = team_history["avg_gpm"].fillna(0).astype(float)
    team_history["avg_xpm"] = team_history["avg_xpm"].fillna(0).astype(float)

    # Sort by start_time for correct temporal ordering
    team_history = team_history.sort_values("start_time").reset_index(drop=True)

    # --- Team rolling stats -------------------------------------------------
    rolling_cols = []
    for n in _WINDOW_SIZES:
        for stat in ["win", "avg_gpm", "avg_xpm", "net_worth_lead_10min"]:
            col_name = f"team_{stat if stat != 'win' else 'win_rate'}_{n}"
            rolling_cols.append((stat, col_name, n))

    team_rolling_results: dict[tuple[int, int], dict] = {}

    for team_id, grp in team_history.groupby("team_id"):
        if team_id == 0:
            continue
        grp = grp.reset_index(drop=True)
        for stat, col_name, n in rolling_cols:
            if stat == "win":
                rolled = grp[stat].rolling(window=n, min_periods=1).mean().shift(1)
            else:
                rolled = grp[stat].rolling(window=n, min_periods=1).mean().shift(1)
            for i, (_, row) in enumerate(grp.iterrows()):
                mid = int(row["match_id"])
                key = (mid, int(team_id))
                val = rolled.iloc[i]
                if key not in team_rolling_results:
                    team_rolling_results[key] = {}
                team_rolling_results[key][col_name] = (
                    float(val) if not pd.isna(val) else np.nan
                )

    # --- Hero patch stats ---------------------------------------------------
    # Accumulate incrementally as matches progress.
    raw_matches_sorted = raw_matches.sort_values("start_time")
    hero_patch_state: dict[tuple[int, int], dict] = defaultdict(
        lambda: {"wins": 0, "picks": 0, "gpm_sum": 0.0, "gpm_count": 0}
    )
    patch_state: dict[int, dict] = defaultdict(
        lambda: {"total_matches": 0, "hero_pick_matches": defaultdict(set), "hero_ban_matches": defaultdict(set)}
    )

    hero_patch_results: dict[tuple[int, int, int], dict] = {}

    for _, match_row in raw_matches_sorted.iterrows():
        mid = int(match_row["match_id"])
        patch = int(match_row["patch"])
        radiant_win = bool(match_row["radiant_win"])

        patch_state[patch]["total_matches"] += 1

        # Get picks and bans for this match (already loaded)
        match_picks = all_picks[all_picks["match_id"] == mid]
        match_bans = all_bans[all_bans["match_id"] == mid]

        for _, pick in match_picks.iterrows():
            hero = int(pick["hero_id"])
            is_radiant = bool(pick["is_radiant"])
            won = (is_radiant and radiant_win) or (not is_radiant and not radiant_win)

            # Record stats based on state BEFORE this match
            hp_state = hero_patch_state.get((hero, patch), {"wins": 0, "picks": 0, "gpm_sum": 0.0, "gpm_count": 0})
            ps = patch_state[patch]

            total = ps["total_matches"] - 1  # exclude current match
            pick_matches = len(ps["hero_pick_matches"][hero])
            ban_matches = len(ps["hero_ban_matches"][hero])

            hero_patch_results[(mid, hero)] = {
                "hero_win_rate_patch": float(hp_state["wins"] / hp_state["picks"]) if hp_state["picks"] > 0 else np.nan,
                "hero_avg_gpm_patch": float(hp_state["gpm_sum"] / hp_state["gpm_count"]) if hp_state["gpm_count"] > 0 else np.nan,
                "hero_pick_rate_patch": float(pick_matches / total) if total > 0 else np.nan,
                "hero_ban_rate_patch": float(ban_matches / total) if total > 0 else np.nan,
            }

            # Update state AFTER recording
            hp_state["wins"] += 1 if won else 0
            hp_state["picks"] += 1
            hp_state["gpm_sum"] += float(pick["gold_per_min"])
            hp_state["gpm_count"] += 1
            hero_patch_state[(hero, patch)] = hp_state
            ps["hero_pick_matches"][hero].add(mid)

        for _, ban in match_bans.iterrows():
            hero = int(ban["hero_id"])
            patch_state[patch]["hero_ban_matches"][hero].add(mid)

    # --- H2H stats ----------------------------------------------------------
    h2h_state: dict[tuple[int, int], dict] = {}
    h2h_results: dict[int, dict] = {}

    for _, match_row in raw_matches_sorted.iterrows():
        mid = int(match_row["match_id"])
        r_id = int(match_row["radiant_team_id"])
        d_id = int(match_row["dire_team_id"])
        radiant_win = bool(match_row["radiant_win"])

        key = (r_id, d_id)
        if key in h2h_state:
            st = h2h_state[key]
            h2h_results[mid] = {
                "h2h_radiant_win_rate": float(st["radiant_wins"] / st["total"]) if st["total"] > 0 else np.nan,
                "h2h_match_count": st["total"],
            }
        else:
            h2h_results[mid] = {"h2h_radiant_win_rate": np.nan, "h2h_match_count": 0}

        # Update state
        if key not in h2h_state:
            h2h_state[key] = {"total": 0, "radiant_wins": 0}
        h2h_state[key]["total"] += 1
        if radiant_win:
            h2h_state[key]["radiant_wins"] += 1

    # --- Merge into feature DataFrames --------------------------------------
    # Team features
    team_agg_rows = []
    for _, row in team_df.iterrows():
        mid = int(row["match_id"])
        tid = int(row["team_id"])
        tr = team_rolling_results.get((mid, tid), empty_team_rolling())
        team_agg_rows.append(tr)
    team_agg_df = pd.DataFrame(team_agg_rows, index=team_df.index)
    team_df = pd.concat([team_df, team_agg_df], axis=1)

    # Hero features
    hero_agg_rows = []
    for _, row in hero_df.iterrows():
        mid = int(row["match_id"])
        hid = int(row["hero_id"])
        hp = hero_patch_results.get((mid, hid), _empty_hero_patch())
        hero_agg_rows.append(hp)
    hero_agg_df = pd.DataFrame(hero_agg_rows, index=hero_df.index)
    hero_df = pd.concat([hero_df, hero_agg_df], axis=1)

    # Match features
    h2h_agg_rows = []
    for _, row in matches_df.iterrows():
        mid = int(row["match_id"])
        h2h = h2h_results.get(mid, {"h2h_radiant_win_rate": np.nan, "h2h_match_count": 0})
        h2h_agg_rows.append(h2h)
    h2h_agg_df = pd.DataFrame(h2h_agg_rows, index=matches_df.index)
    matches_df = pd.concat([matches_df, h2h_agg_df], axis=1)

    return matches_df, team_df, hero_df
