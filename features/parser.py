"""Single-match feature extraction.

Each `extract_*` function takes raw DataFrames for a single match and returns
one or more dicts whose keys match the column schemas in DESIGN.md.
"""

import numpy as np
import pandas as pd

from .hero_roles import infer_roles


def _safe_int(val, default=0):
    try:
        return int(float(val)) if pd.notna(val) else default
    except (ValueError, TypeError):
        return default


def _first_blood_radiant(players_df: pd.DataFrame) -> bool:
    """Determine which team got first blood from player data."""
    if players_df.empty:
        return False
    fb_col = "firstblood_claimed"
    if fb_col not in players_df.columns:
        return False
    fb_players = players_df[players_df[fb_col].notna() & (players_df[fb_col] != 0)]
    if fb_players.empty:
        return False
    return bool(fb_players.iloc[0]["is_radiant"])


def _count_sign_swings(series: pd.Series) -> int:
    """Count how many times the series crosses zero."""
    signs = np.sign(series.values)
    non_zero = signs != 0
    if non_zero.sum() < 2:
        return 0
    changes = (signs[1:] != signs[:-1]) & non_zero[1:] & non_zero[:-1]
    return int(changes.sum())


def _gold_adv_at_minute(gold_adv_df: pd.DataFrame, minute: int) -> int:
    if gold_adv_df.empty or "time_min" not in gold_adv_df.columns:
        return 0
    row = gold_adv_df[gold_adv_df["time_min"] == minute]
    if row.empty:
        return 0
    return _safe_int(row["value"].iloc[0])


def _xp_adv_at_minute(xp_adv_df: pd.DataFrame, minute: int) -> int:
    if xp_adv_df.empty or "time_min" not in xp_adv_df.columns:
        return 0
    row = xp_adv_df[xp_adv_df["time_min"] == minute]
    if row.empty:
        return 0
    return _safe_int(row["value"].iloc[0])


def extract_match_features(
    match_row: pd.Series,
    gold_adv_df: pd.DataFrame,
    xp_adv_df: pd.DataFrame,
    objectives_df: pd.DataFrame,
    teamfights_df: pd.DataFrame,
    tf_players_df: pd.DataFrame,
    players_df: pd.DataFrame | None = None,
) -> dict:
    """Extract match-level features for a single match."""

    if players_df is not None and not players_df.empty:
        fb_radiant = _first_blood_radiant(players_df)
    else:
        fb_radiant = False

    # Gold advantage stats
    gold_values = gold_adv_df["value"] if len(gold_adv_df) > 0 and "value" in gold_adv_df.columns else pd.Series([0])

    # Tower / barracks kills from objectives
    if len(objectives_df) > 0 and "type" in objectives_df.columns:
        buildings = objectives_df[objectives_df["type"] == "building_kill"]
        keys = buildings["key"].fillna("")

        radiant_tower_kills = int(keys.str.contains("badguys_tower").sum())
        dire_tower_kills = int(keys.str.contains("goodguys_tower").sum())
        radiant_barracks_kills = int(keys.str.contains("badguys.*rax").sum())
        dire_barracks_kills = int(keys.str.contains("goodguys.*rax").sum())

        radiant_tower_times = buildings.loc[
            keys.str.contains("badguys_tower"), "time"
        ]
        dire_tower_times = buildings.loc[
            keys.str.contains("goodguys_tower"), "time"
        ]
        radiant_first_tower_time = (
            int(radiant_tower_times.min()) if len(radiant_tower_times) > 0 else 0
        )
        dire_first_tower_time = (
            int(dire_tower_times.min()) if len(dire_tower_times) > 0 else 0
        )
    else:
        radiant_tower_kills = 0
        dire_tower_kills = 0
        radiant_barracks_kills = 0
        dire_barracks_kills = 0
        radiant_first_tower_time = 0
        dire_first_tower_time = 0

    # Teamfight analysis
    teamfight_count = len(teamfights_df)
    radiant_tf_wins = 0
    radiant_tf_kills = 0
    radiant_tf_deaths = 0

    if teamfight_count > 0 and len(tf_players_df) > 0:
        tf_players = tf_players_df.copy()
        tf_players["is_radiant"] = tf_players["player_slot"].between(0, 4)
        # sum kills by side within each teamfight
        for tf_id, group in tf_players.groupby("teamfight_id"):
            rad_k = int(group.loc[group["is_radiant"], "kills"].sum())
            dire_k = int(group.loc[~group["is_radiant"], "kills"].sum())
            if rad_k > dire_k:
                radiant_tf_wins += 1
            radiant_tf_kills += rad_k
            radiant_tf_deaths += int(group.loc[group["is_radiant"], "deaths"].sum())

    radiant_tf_kd_ratio = (
        round(radiant_tf_kills / radiant_tf_deaths, 4)
        if radiant_tf_deaths > 0
        else float(radiant_tf_kills) if radiant_tf_kills > 0 else 0.0
    )

    return {
        "match_id": int(match_row["match_id"]),
        "duration": _safe_int(match_row.get("duration", 0)),
        "radiant_win": bool(match_row.get("radiant_win", False)),
        "first_blood_radiant": fb_radiant,
        "first_blood_time": _safe_int(match_row.get("first_blood_time", 0)),
        "radiant_gold_adv_10min": _gold_adv_at_minute(gold_adv_df, 10),
        "radiant_xp_adv_10min": _xp_adv_at_minute(xp_adv_df, 10),
        "radiant_gold_adv_max": int(gold_values.max()) if len(gold_values) > 0 else 0,
        "radiant_gold_adv_min": int(gold_values.min()) if len(gold_values) > 0 else 0,
        "radiant_gold_adv_mean": float(gold_values.mean()) if len(gold_values) > 0 else 0.0,
        "gold_adv_swings": _count_sign_swings(gold_values),
        "radiant_tower_kills": radiant_tower_kills,
        "dire_tower_kills": dire_tower_kills,
        "radiant_barracks_kills": radiant_barracks_kills,
        "dire_barracks_kills": dire_barracks_kills,
        "radiant_first_tower_time": radiant_first_tower_time,
        "dire_first_tower_time": dire_first_tower_time,
        "teamfight_count": teamfight_count,
        "radiant_teamfight_wins": radiant_tf_wins,
        "radiant_tf_kd_ratio": radiant_tf_kd_ratio,
        "stomp_value": _safe_int(match_row.get("stomp", 0)),
        "comeback_value": _safe_int(match_row.get("comeback", 0)),
        "radiant_score": _safe_int(match_row.get("radiant_score", 0)),
        "dire_score": _safe_int(match_row.get("dire_score", 0)),
        "patch": _safe_int(match_row.get("patch", 0)),
        "radiant_team_id": _safe_int(match_row.get("radiant_team_id", 0)),
        "dire_team_id": _safe_int(match_row.get("dire_team_id", 0)),
        "league_id": _safe_int(match_row.get("leagueid", 0)),
        "series_id": _safe_int(match_row.get("series_id", 0)),
    }


def extract_team_features(
    match_row: pd.Series,
    players_df: pd.DataFrame,
    first_blood_radiant: bool = False,
) -> list[dict]:
    """Extract team-level features (2 rows: radiant + dire).

    first_blood_radiant should come from extract_match_features.
    """

    results = []
    for is_radiant in (True, False):
        side_players = players_df[players_df["is_radiant"] == is_radiant]
        if side_players.empty:
            continue

        gpm = side_players["gold_per_min"]
        xpm = side_players["xp_per_min"]
        nw = side_players["net_worth"]
        team_id = (
            _safe_int(match_row["radiant_team_id"])
            if is_radiant
            else _safe_int(match_row["dire_team_id"])
        )

        results.append(
            {
                "match_id": _safe_int(match_row["match_id"]),
                "is_radiant": is_radiant,
                "team_id": team_id if team_id else 0,
                "total_kills": int(side_players["kills"].sum()),
                "total_deaths": int(side_players["deaths"].sum()),
                "total_assists": int(side_players["assists"].sum()),
                "avg_gpm": float(gpm.mean()) if len(gpm) > 0 else 0.0,
                "avg_xpm": float(xpm.mean()) if len(xpm) > 0 else 0.0,
                "total_net_worth": int(nw.sum()),
                "total_last_hits": int(side_players["last_hits"].sum()),
                "total_denies": int(side_players["denies"].sum()),
                "gpm_std": float(gpm.std(ddof=0)) if len(gpm) > 0 else 0.0,
                "max_net_worth": int(nw.max()) if len(nw) > 0 else 0,
                "total_hero_damage": int(side_players["hero_damage"].sum()),
                "first_blood": (is_radiant and first_blood_radiant)
                or (not is_radiant and not first_blood_radiant),
            }
        )

    return results


def extract_hero_features(players_df: pd.DataFrame) -> list[dict]:
    """Extract hero-level features (10 rows per match)."""

    roles = infer_roles(players_df)
    records = []

    for i, (_, player) in enumerate(players_df.iterrows()):
        records.append(
            {
                "match_id": _safe_int(player["match_id"]),
                "hero_id": _safe_int(player["hero_id"]),
                "player_slot": _safe_int(player["player_slot"]),
                "is_radiant": bool(player["is_radiant"]),
                "team_id": _safe_int(player.get("team_id", 0)),
                "kills": _safe_int(player["kills"]),
                "deaths": _safe_int(player["deaths"]),
                "assists": _safe_int(player["assists"]),
                "gpm": _safe_int(player["gold_per_min"]),
                "xpm": _safe_int(player["xp_per_min"]),
                "net_worth": _safe_int(player["net_worth"]),
                "last_hits": _safe_int(player["last_hits"]),
                "denies": _safe_int(player["denies"]),
                "hero_damage": _safe_int(player["hero_damage"]),
                "hero_healing": _safe_int(player["hero_healing"]),
                "tower_damage": _safe_int(player["tower_damage"]),
                "level": _safe_int(player["level"]),
                "role": _safe_int(roles.iloc[i]),
            }
        )

    return records


def _get_phase(order: int) -> str:
    """Map draft order (0-indexed) to phase label."""
    if order <= 5:
        return "ban1"
    elif order <= 9:
        return "pick1"
    elif order <= 15:
        return "ban2"
    elif order <= 19:
        return "pick2"
    elif order <= 21:
        return "ban3"
    else:
        return "pick3"


def extract_draft_features(picks_bans_df: pd.DataFrame) -> list[dict]:
    """Extract draft features (up to 24 rows per match)."""

    records = []
    for _, row in picks_bans_df.iterrows():
        order = _safe_int(row["ord"])
        records.append(
            {
                "match_id": _safe_int(row["match_id"]),
                "order": order,
                "is_pick": bool(row["is_pick"]),
                "hero_id": _safe_int(row["hero_id"]),
                "team": _safe_int(row["team"]),
                "phase": _get_phase(order),
            }
        )

    return records
