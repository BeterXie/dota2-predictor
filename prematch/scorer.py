"""Pre-match prediction scorer.

Combines hero matchup, team form, draft profile, player skill, and early-game
signals into a single win-probability estimate.

Each sub-score is in [-1, 1] (positive = radiant favoured).
Weights are dynamically adjusted when data is missing.
"""

import sqlite3
from typing import Any

import numpy as np

# Default weights when all data is available
_DEFAULT_WEIGHTS = {
    "hero_matchup": 0.73,
    "team_form": 0.04,
    "draft_profile": 0.14,
    "player_skill": 0.09,
    "early_game": 0.00,       # needs more per-player data to be useful
}

# Dimensions used in draft profile and their weights within the component
# Using p75 benchmarks (above-average performance) to capture hero potential
_DRAFT_DIMENSION_WEIGHTS = {
    "tower_damage": 0.25,          # push / objective-taking (best single predictor)
    "hero_healing_per_min": 0.25,  # sustain / healing (correlates 0.39 with WR)
    "scaling_score": 0.20,         # early vs late game tendency
    "hero_damage_per_min": 0.15,   # team-fight damage output
    "gold_per_min": 0.15,          # farming / economic scaling
}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Component 1: Hero matchup advantage
# ---------------------------------------------------------------------------

def _hero_advantage(db_path: str, hero_a: int, hero_b: int) -> float:
    """Compute advantage of hero_a vs hero_b from hero_matchups table.

    Returns a value in roughly [-0.3, +0.3] representing win-rate delta.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT games_played, wins, synergy
               FROM hero_matchups
               WHERE hero_id = ? AND vs_hero_id = ?""",
            (hero_a, hero_b),
        ).fetchone()
    if not row:
        return 0.0

    synergy = row["synergy"]
    if synergy is not None:
        return float(synergy) / 100.0

    games = row["games_played"] or 0
    if games < 10:
        return 0.0

    vs_wr = float(row["wins"] or 0) / games

    with _connect(db_path) as conn:
        overall = conn.execute(
            """SELECT SUM(games_played) as total_games, SUM(wins) as total_wins
               FROM hero_matchups WHERE hero_id = ?""",
            (hero_a,),
        ).fetchone()
    total_games = overall["total_games"] or 0
    if total_games < 10:
        return 0.0
    overall_wr = float(overall["total_wins"] or 0) / total_games

    return vs_wr - overall_wr


def _compute_hero_matchup_score(
    db_path: str,
    radiant_heroes: list[int],
    dire_heroes: list[int],
) -> dict:
    """Compute hero matchup advantage from a 5v5 hero matrix.

    Returns a dict with:
        score: overall advantage in [-1, 1]
        confidence: 0-1 based on data coverage
        mean_adv: raw mean advantage
        matrix: 5x5 list-of-lists, each cell {radiant_id, dire_id, adv, rad_name, dir_name, verdict}
        radiant_heroes: list of {hero_id, name} for radiant
        dire_heroes: list of {hero_id, name} for dire
    """
    rad_names = _lookup_hero_names(db_path, radiant_heroes)
    dir_names = _lookup_hero_names(db_path, dire_heroes)

    advantages = []
    matrix = []
    for i, rh in enumerate(radiant_heroes):
        row = []
        for j, dh in enumerate(dire_heroes):
            adv = _hero_advantage(db_path, rh, dh)
            advantages.append(adv)
            row.append({
                "radiant_id": rh,
                "dire_id": dh,
                "adv": round(adv, 4),
                "radiant_name": rad_names.get(rh, f"Hero {rh}"),
                "dire_name": dir_names.get(dh, f"Hero {dh}"),
                "verdict": _verdict(adv),
            })
        matrix.append(row)

    arr = np.array(advantages)
    data_pairs = sum(1 for a in advantages if a != 0.0)
    coverage = data_pairs / 25.0 if advantages else 0.0

    if len(arr) == 0:
        return {
            "score": 0.0, "confidence": 0.0, "mean_adv": 0.0,
            "matrix": matrix,
            "radiant_heroes": [{"hero_id": h, "name": rad_names.get(h, f"Hero {h}")} for h in radiant_heroes],
            "dire_heroes": [{"hero_id": h, "name": dir_names.get(h, f"Hero {h}")} for h in dire_heroes],
        }

    mean_adv = float(arr.mean())

    dire_advantages = []
    for dh in dire_heroes:
        for rh in radiant_heroes:
            dire_advantages.append(_hero_advantage(db_path, dh, rh))

    dire_mean = float(np.array(dire_advantages).mean()) if dire_advantages else 0.0
    net_adv = mean_adv - dire_mean
    score = np.tanh(net_adv * 8.0)

    return {
        "score": round(score, 4),
        "confidence": round(coverage, 4),
        "mean_adv": round(net_adv, 4),
        "matrix": matrix,
        "radiant_heroes": [{"hero_id": h, "name": rad_names.get(h, f"Hero {h}")} for h in radiant_heroes],
        "dire_heroes": [{"hero_id": h, "name": dir_names.get(h, f"Hero {h}")} for h in dire_heroes],
    }


def _verdict(adv: float) -> str:
    """Human-readable verdict for a hero matchup."""
    if adv > 0.03:
        return "优势"
    elif adv > 0.01:
        return "略优"
    elif adv < -0.03:
        return "劣势"
    elif adv < -0.01:
        return "略劣"
    return "均势"


def _lookup_hero_names(db_path: str, hero_ids: list[int]) -> dict[int, str]:
    """Look up hero names from the database."""
    if not hero_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in hero_ids)
        rows = conn.execute(
            f"SELECT hero_id, localized_name FROM heroes WHERE hero_id IN ({placeholders})",
            hero_ids,
        ).fetchall()
    return {r["hero_id"]: r["localized_name"] for r in rows}


# ---------------------------------------------------------------------------
# Component 2: Team recent form
# ---------------------------------------------------------------------------

def _compute_team_form(
    db_path: str,
    radiant_id: int,
    dire_id: int,
    n_matches: int = 20,
) -> dict[str, float]:
    """Compute recent team form from win rate and performance in last N matches.

    Returns a dict with:
        score: form advantage in [-1, 1] (positive = radiant better form)
        radiant_win_rate: radiant's recent win rate
        dire_win_rate: dire's recent win rate
        confidence: 0-1 based on data availability
    """
    radiant_stats = _team_recent_stats(db_path, radiant_id, n_matches)
    dire_stats = _team_recent_stats(db_path, dire_id, n_matches)

    r_wr = radiant_stats.get("win_rate", 0.5)
    d_wr = dire_stats.get("win_rate", 0.5)

    # Win-rate differential, scaled to [-1, 1]
    wr_diff = r_wr - d_wr

    # GPM differential as secondary signal
    r_gpm = radiant_stats.get("avg_gpm", 0)
    d_gpm = dire_stats.get("avg_gpm", 0)
    gpm_diff = (r_gpm - d_gpm) / max(abs(r_gpm - d_gpm), 400) if r_gpm and d_gpm else 0.0
    gpm_diff = max(-1.0, min(1.0, gpm_diff))

    score = 0.7 * wr_diff + 0.3 * gpm_diff
    score = max(-1.0, min(1.0, score))

    confidence = min(
        radiant_stats.get("matches_used", 0) / n_matches,
        dire_stats.get("matches_used", 0) / n_matches,
    )

    return {
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "radiant_win_rate": round(r_wr, 4),
        "dire_win_rate": round(d_wr, 4),
        "radiant_avg_gpm": round(r_gpm, 1),
        "dire_avg_gpm": round(d_gpm, 1),
    }


def _team_recent_stats(
    db_path: str, team_id: int, n_matches: int = 20,
) -> dict[str, float]:
    """Get a team's win rate and average GPM from recent matches."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT m.radiant_team_id, m.dire_team_id, m.radiant_win
               FROM matches m
               WHERE (m.radiant_team_id = ? OR m.dire_team_id = ?)
                 AND m.start_time IS NOT NULL
               ORDER BY m.start_time DESC
               LIMIT ?""",
            (team_id, team_id, n_matches),
        ).fetchall()

    if not rows:
        return {"win_rate": 0.5, "avg_gpm": 0.0, "matches_used": 0}

    wins = 0
    for r in rows:
        if (r["radiant_team_id"] == team_id and r["radiant_win"]) or \
           (r["dire_team_id"] == team_id and not r["radiant_win"]):
            wins += 1

    # Also try to get GPM from recent matches
    with _connect(db_path) as conn2:
        gpm_row = conn2.execute(
            """SELECT AVG(mp.gold_per_min) as avg_gpm
               FROM match_players mp
               JOIN matches m ON mp.match_id = m.match_id
               WHERE mp.team_id = ?
                 AND m.start_time IS NOT NULL
               ORDER BY m.start_time DESC
               LIMIT ?""",
            (team_id, n_matches * 5),
        ).fetchone()

    avg_gpm = float(gpm_row["avg_gpm"]) if gpm_row and gpm_row["avg_gpm"] else 0.0

    return {
        "win_rate": wins / len(rows),
        "avg_gpm": avg_gpm,
        "matches_used": len(rows),
    }


# ---------------------------------------------------------------------------
# Component 3: Head-to-head record
# ---------------------------------------------------------------------------

def _compute_h2h_score(
    db_path: str,
    radiant_id: int,
    dire_id: int,
) -> dict[str, float]:
    """Compute head-to-head advantage from past encounters."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT radiant_team_id, dire_team_id, radiant_win
               FROM matches
               WHERE (radiant_team_id = ? AND dire_team_id = ?)
                  OR (radiant_team_id = ? AND dire_team_id = ?)
               ORDER BY start_time DESC""",
            (radiant_id, dire_id, dire_id, radiant_id),
        ).fetchall()

    total = len(rows)
    if total == 0:
        return {"score": 0.0, "confidence": 0.0, "total_matches": 0, "radiant_wins": 0}

    radiant_wins = 0
    for r in rows:
        if (r["radiant_team_id"] == radiant_id and r["radiant_win"]) or \
           (r["dire_team_id"] == radiant_id and not r["radiant_win"]):
            radiant_wins += 1

    h2h_wr = radiant_wins / total

    # Scale: diff from 0.5. If 8-2 record, diff = 0.3, score ≈ 0.6
    score = np.tanh((h2h_wr - 0.5) * 6.0)

    # Confidence based on sample size (saturates at ~20 matches)
    confidence = min(1.0, total / 20.0)

    return {
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "total_matches": total,
        "radiant_wins": radiant_wins,
        "h2h_win_rate": round(h2h_wr, 4),
    }


# ---------------------------------------------------------------------------
# Component 4: Team historical strength
# ---------------------------------------------------------------------------

def _compute_team_strength(
    db_path: str,
    radiant_id: int,
    dire_id: int,
) -> dict[str, float]:
    """Compute overall team strength from historical data."""
    r_stats = _team_overall_stats(db_path, radiant_id)
    d_stats = _team_overall_stats(db_path, dire_id)

    r_win_rate = r_stats.get("win_rate", 0.5)
    d_win_rate = d_stats.get("win_rate", 0.5)

    wr_diff = r_win_rate - d_win_rate
    score = np.tanh(wr_diff * 3.0)

    confidence = min(
        r_stats.get("total_matches", 0) / 30.0,
        d_stats.get("total_matches", 0) / 30.0,
        1.0,
    )

    return {
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "radiant_overall_win_rate": round(r_win_rate, 4),
        "dire_overall_win_rate": round(d_win_rate, 4),
        "radiant_total_matches": r_stats.get("total_matches", 0),
        "dire_total_matches": d_stats.get("total_matches", 0),
    }


def _team_overall_stats(db_path: str, team_id: int) -> dict[str, float]:
    """Get overall win rate and match count for a team."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE
                   WHEN (radiant_team_id = ? AND radiant_win = 1)
                     OR (dire_team_id = ? AND radiant_win = 0) THEN 1 ELSE 0
                 END) AS wins
               FROM matches
               WHERE radiant_team_id = ? OR dire_team_id = ?""",
            (team_id, team_id, team_id, team_id),
        ).fetchone()

    total = row["total"] or 0
    if total == 0:
        return {"win_rate": 0.5, "total_matches": 0}
    return {"win_rate": float(row["wins"] or 0) / total, "total_matches": total}


# ---------------------------------------------------------------------------
# Component 5: Draft profile (阵容画像)
# ---------------------------------------------------------------------------

def _compute_draft_profile(
    db_path: str,
    radiant_heroes: list[int],
    dire_heroes: list[int],
) -> dict[str, Any]:
    """Compute team composition profile using hero benchmarks and scaling scores.

    Compares radiant vs dire on 7 dimensions: push, damage, farm, sustain,
    experience, scaling (early vs late), and overall hero win rate.
    All data sourced from OpenDota API (millions of matches).

    Returns a dict with score, confidence, and per-dimension breakdown.
    """
    dim_weights = _DRAFT_DIMENSION_WEIGHTS

    # Fetch benchmarks and scaling scores for all heroes at once
    hero_ids = list(set(radiant_heroes + dire_heroes))
    benchmarks = _load_hero_benchmarks(db_path, hero_ids)
    scaling = _load_hero_scaling(db_path, hero_ids)
    win_rates = _load_hero_win_rates(db_path, hero_ids)

    dimensions = {}
    total_score = 0.0
    total_weight = 0.0
    data_points = 0

    for dim_name, dim_weight in dim_weights.items():
        if dim_name == "scaling_score":
            r_vals = [scaling.get(h, 0.0) for h in radiant_heroes]
            d_vals = [scaling.get(h, 0.0) for h in dire_heroes]
        elif dim_name == "win_rate":
            r_vals = [win_rates.get(h, 0.5) for h in radiant_heroes]
            d_vals = [win_rates.get(h, 0.5) for h in dire_heroes]
        else:
            r_vals = [benchmarks.get(h, {}).get(dim_name, 0) for h in radiant_heroes]
            d_vals = [benchmarks.get(h, {}).get(dim_name, 0) for h in dire_heroes]

        r_mean = np.mean(r_vals) if r_vals else 0
        d_mean = np.mean(d_vals) if d_vals else 0

        # Normalize by dimension-specific scale
        scale = _dimension_scale(dim_name)
        if scale > 0:
            diff = (r_mean - d_mean) / scale
        else:
            diff = r_mean - d_mean

        dim_score = np.tanh(diff * 0.8)
        total_score += dim_weight * dim_score
        total_weight += dim_weight

        # Data point coverage: fraction of heroes with non-zero data
        coverage = (sum(1 for v in r_vals if v != 0) + sum(1 for v in d_vals if v != 0)) / 10.0
        data_points += coverage

        dimensions[dim_name] = {
            "radiant_avg": round(float(r_mean), 2),
            "dire_avg": round(float(d_mean), 2),
            "score": round(float(dim_score), 4),
            "weight": dim_weight,
        }

    if total_weight > 0:
        total_score /= total_weight

    confidence = min(1.0, data_points / len(dim_weights))

    return {
        "score": round(float(total_score), 4),
        "confidence": round(float(confidence), 4),
        "dimensions": dimensions,
    }


def _dimension_scale(dim: str) -> float:
    """Return normalization scale for a dimension (approximate p75 inter-hero std dev)."""
    scales = {
        "tower_damage": 8000.0,
        "hero_damage_per_min": 300.0,
        "gold_per_min": 150.0,
        "hero_healing_per_min": 40.0,
        "scaling_score": 0.08,
    }
    return scales.get(dim, 1.0)


def _load_hero_benchmarks(db_path: str, hero_ids: list[int]) -> dict[int, dict[str, float]]:
    """Load p75 benchmarks for a list of hero IDs (above-average performance)."""
    if not hero_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in hero_ids)
        rows = conn.execute(
            f"SELECT hero_id, metric, p75 FROM hero_benchmarks WHERE hero_id IN ({placeholders})",
            hero_ids,
        ).fetchall()
    result: dict[int, dict[str, float]] = {}
    for r in rows:
        hid = r["hero_id"]
        if hid not in result:
            result[hid] = {}
        result[hid][r["metric"]] = float(r["p75"] or 0)
    return result


def _load_hero_scaling(db_path: str, hero_ids: list[int]) -> dict[int, float]:
    """Load scaling_score for a list of hero IDs."""
    if not hero_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in hero_ids)
        rows = conn.execute(
            f"SELECT hero_id, scaling_score FROM heroes WHERE hero_id IN ({placeholders})",
            hero_ids,
        ).fetchall()
    return {r["hero_id"]: float(r["scaling_score"] or 0) for r in rows}


def _load_hero_win_rates(db_path: str, hero_ids: list[int]) -> dict[int, float]:
    """Load global win_rate for a list of hero IDs (from OpenDota heroStats)."""
    if not hero_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in hero_ids)
        rows = conn.execute(
            f"SELECT hero_id, win_rate FROM heroes WHERE hero_id IN ({placeholders})",
            hero_ids,
        ).fetchall()
    return {r["hero_id"]: float(r["win_rate"] or 0.5) for r in rows}


# ---------------------------------------------------------------------------
# Component 6: Player skill (选手实力)
# ---------------------------------------------------------------------------

def _compute_player_skill(
    db_path: str,
    radiant_players: list[int],
    dire_players: list[int],
    before_time: int | None = None,
) -> dict[str, Any]:
    """Compute team-level player skill from historical KDA, GPM, and win rate.

    Args:
        db_path: Path to database.
        radiant_players: 5 account IDs for radiant side.
        dire_players: 5 account IDs for dire side.
        before_time: If set, only use matches before this Unix timestamp
                     (for backtesting to avoid data leakage).

    Returns:
        Dict with score, confidence, and per-team breakdown.
    """
    r_stats = [_player_historical_stats(db_path, aid, before_time) for aid in radiant_players]
    d_stats = [_player_historical_stats(db_path, aid, before_time) for aid in dire_players]

    def _team_score(stats_list: list[dict]) -> dict:
        valid = [s for s in stats_list if s["matches"] > 0]
        if not valid:
            return {"kda": 0, "gpm": 0, "win_rate": 0.5, "matches": 0, "players_with_data": 0}

        kda = np.mean([s["kda"] for s in valid])
        gpm = np.mean([s["gpm"] for s in valid])
        wr = np.mean([s["win_rate"] for s in valid])
        return {
            "kda": round(float(kda), 2),
            "gpm": round(float(gpm), 1),
            "win_rate": round(float(wr), 4),
            "matches": sum(s["matches"] for s in valid),
            "players_with_data": len(valid),
        }

    r_agg = _team_score(r_stats)
    d_agg = _team_score(d_stats)

    # Normalize each sub-component and combine
    kda_diff = np.tanh((r_agg["kda"] - d_agg["kda"]) / 2.5 * 0.8)
    gpm_diff = np.tanh((r_agg["gpm"] - d_agg["gpm"]) / 200.0 * 0.8)
    wr_diff = r_agg["win_rate"] - d_agg["win_rate"]  # already in [0,1]

    score = 0.35 * kda_diff + 0.25 * gpm_diff + 0.40 * wr_diff
    score = round(float(max(-1.0, min(1.0, score))), 4)

    # Confidence based on data availability
    total_players = len(radiant_players) + len(dire_players)
    players_with_data = r_agg["players_with_data"] + d_agg["players_with_data"]
    avg_matches = (r_agg["matches"] + d_agg["matches"]) / max(total_players, 1)
    confidence = min(1.0, (players_with_data / total_players) * min(1.0, avg_matches / 10.0))

    return {
        "score": score,
        "confidence": round(float(confidence), 4),
        "radiant": r_agg,
        "dire": d_agg,
    }


def _player_historical_stats(
    db_path: str, account_id: int, before_time: int | None = None
) -> dict:
    """Get a player's historical KDA, GPM, and win rate from past matches."""
    with _connect(db_path) as conn:
        if before_time:
            row = conn.execute("""
                SELECT
                    COUNT(*) as matches,
                    AVG(1.0 * mp.kills) as avg_k,
                    AVG(1.0 * mp.deaths) as avg_d,
                    AVG(1.0 * mp.assists) as avg_a,
                    AVG(1.0 * mp.gold_per_min) as avg_gpm,
                    SUM(CASE
                        WHEN (mp.is_radiant = 1 AND m.radiant_win = 1)
                          OR (mp.is_radiant = 0 AND m.radiant_win = 0) THEN 1 ELSE 0
                    END) as wins
                FROM match_players mp
                JOIN matches m ON mp.match_id = m.match_id
                WHERE mp.account_id = ? AND m.start_time < ?
            """, (account_id, before_time)).fetchone()
        else:
            row = conn.execute("""
                SELECT
                    COUNT(*) as matches,
                    AVG(1.0 * mp.kills) as avg_k,
                    AVG(1.0 * mp.deaths) as avg_d,
                    AVG(1.0 * mp.assists) as avg_a,
                    AVG(1.0 * mp.gold_per_min) as avg_gpm,
                    SUM(CASE
                        WHEN (mp.is_radiant = 1 AND m.radiant_win = 1)
                          OR (mp.is_radiant = 0 AND m.radiant_win = 0) THEN 1 ELSE 0
                    END) as wins
                FROM match_players mp
                JOIN matches m ON mp.match_id = m.match_id
                WHERE mp.account_id = ?
            """, (account_id,)).fetchone()

    matches = row["matches"] or 0
    if matches == 0:
        return {"kda": 0, "gpm": 0, "win_rate": 0.5, "matches": 0}

    avg_k = float(row["avg_k"] or 0)
    avg_d = float(row["avg_d"] or 0)
    avg_a = float(row["avg_a"] or 0)
    avg_gpm = float(row["avg_gpm"] or 0)
    wins = row["wins"] or 0

    kda = (avg_k + avg_a) / max(avg_d, 0.1)
    wr = wins / matches

    return {"kda": round(kda, 2), "gpm": round(avg_gpm, 1), "win_rate": round(wr, 4),
            "matches": matches}


# ---------------------------------------------------------------------------
# Component 7: Early game (前期对抗)
# ---------------------------------------------------------------------------

def _compute_early_game(
    db_path: str,
    radiant_players: list[int],
    dire_players: list[int],
    radiant_heroes: list[int],
    dire_heroes: list[int],
    before_time: int | None = None,
) -> dict[str, Any]:
    """Compute team early-game advantage from player historical 10-min stats.

    Uses position-weighted features:
      - Roles 1,3 (core): gold@10, lh@10, xp@10 (farm development)
      - Role 2 (mid): kills@10, xp@10, gold@10 (KDA + farm)
      - Roles 4,5 (support): obs@10, sen@10, assists@10 (vision + roaming)

    Args:
        db_path: Path to database.
        radiant_players: 5 account IDs for radiant.
        dire_players: 5 account IDs for dire.
        radiant_heroes: 5 hero IDs for radiant (for position inference).
        dire_heroes: 5 hero IDs for dire (for position inference).
        before_time: Unix timestamp for backtesting.

    Returns:
        Dict with score, confidence, and per-position breakdown.
    """
    # Get hero → role mapping from heroes table (primary_attr + roles hint)
    hero_roles = _load_hero_position_hints(db_path, radiant_heroes + dire_heroes)

    r_players = list(zip(radiant_players, radiant_heroes))
    d_players = list(zip(dire_players, dire_heroes))

    # Fetch historical early-game stats for all players
    r_stats = [_player_early_stats(db_path, aid, hid, hero_roles.get(hid), before_time)
               for aid, hid in r_players]
    d_stats = [_player_early_stats(db_path, aid, hid, hero_roles.get(hid), before_time)
               for aid, hid in d_players]

    # Position-specific aggregation
    def _position_score(stats_list: list[dict]) -> dict:
        """Compute position-weighted early game score for a team."""
        cores = [s for s in stats_list if s.get("position") in ("core",)]
        mids = [s for s in stats_list if s.get("position") == "mid"]
        supports = [s for s in stats_list if s.get("position") == "support"]

        result = {
            "core_score": 0.0, "mid_score": 0.0, "support_score": 0.0,
            "core_count": len(cores), "mid_count": len(mids), "support_count": len(supports),
            "total_players_with_data": 0,
        }

        # Core score: avg of gold@10 + lh@10 + xp@10 (normalized)
        if cores:
            core_metrics = []
            for s in cores:
                if s["matches"] > 0:
                    core_metrics.append({
                        "gold": s["avg_gold_10min"],
                        "lh": s["avg_lh_10min"],
                        "xp": s["avg_xp_10min"],
                    })
                    result["total_players_with_data"] += 1
            if core_metrics:
                avg_gold = np.mean([m["gold"] for m in core_metrics])
                avg_lh = np.mean([m["lh"] for m in core_metrics])
                avg_xp = np.mean([m["xp"] for m in core_metrics])
                result["core_score"] = (
                    0.4 * np.tanh(avg_gold / 3000.0) +
                    0.3 * np.tanh(avg_lh / 40.0) +
                    0.3 * np.tanh(avg_xp / 3500.0)
                )
                result["core_avg_gold"] = round(avg_gold, 0)
                result["core_avg_lh"] = round(avg_lh, 0)
                result["core_avg_xp"] = round(avg_xp, 0)

        # Mid score: kills@10 + xp@10 + gold@10
        if mids:
            mid_metrics = []
            for s in mids:
                if s["matches"] > 0:
                    mid_metrics.append({
                        "kills": s["avg_kills_10min"],
                        "xp": s["avg_xp_10min"],
                        "gold": s["avg_gold_10min"],
                    })
                    result["total_players_with_data"] += 1
            if mid_metrics:
                avg_k = np.mean([m["kills"] for m in mid_metrics])
                avg_xp = np.mean([m["xp"] for m in mid_metrics])
                avg_gold = np.mean([m["gold"] for m in mid_metrics])
                result["mid_score"] = (
                    0.4 * np.tanh(avg_k / 1.5) +
                    0.3 * np.tanh(avg_xp / 3500.0) +
                    0.3 * np.tanh(avg_gold / 3000.0)
                )
                result["mid_avg_kills"] = round(avg_k, 2)
                result["mid_avg_xp"] = round(avg_xp, 0)
                result["mid_avg_gold"] = round(avg_gold, 0)

        # Support score: deward@10 + obs@10 + assists@10 + kills@10
        if supports:
            sup_metrics = []
            for s in supports:
                if s["matches"] > 0:
                    sup_metrics.append({
                        "obs": s["avg_obs_10min"],
                        "sen": s["avg_sen_10min"],
                        "obs_kills": s.get("avg_obs_kills_10min", 0),
                        "sen_kills": s.get("avg_sen_kills_10min", 0),
                        "assists": s["avg_assists_10min"],
                        "kills": s["avg_kills_10min"],
                    })
                    result["total_players_with_data"] += 1
            if sup_metrics:
                avg_obs = np.mean([m["obs"] for m in sup_metrics])
                avg_sen = np.mean([m["sen"] for m in sup_metrics])
                avg_obs_kills = np.mean([m["obs_kills"] for m in sup_metrics])
                avg_sen_kills = np.mean([m["sen_kills"] for m in sup_metrics])
                avg_assists = np.mean([m["assists"] for m in sup_metrics])
                avg_kills = np.mean([m["kills"] for m in sup_metrics])
                result["support_score"] = (
                    0.25 * np.tanh(avg_obs_kills / 1.5) +   # 排眼 (deward)
                    0.15 * np.tanh(avg_sen_kills / 1.5) +
                    0.20 * np.tanh(avg_obs / 2.0) +          # 插眼 (ward)
                    0.10 * np.tanh(avg_sen / 2.0) +
                    0.20 * np.tanh(avg_assists / 2.0) +      # 游走
                    0.10 * np.tanh(avg_kills / 1.0)
                )
                result["sup_avg_obs_kills"] = round(avg_obs_kills, 2)
                result["sup_avg_sen_kills"] = round(avg_sen_kills, 2)
                result["sup_avg_obs"] = round(avg_obs, 2)
                result["sup_avg_sen"] = round(avg_sen, 2)
                result["sup_avg_assists"] = round(avg_assists, 2)
                result["sup_avg_kills"] = round(avg_kills, 2)

        return result

    r_pos = _position_score(r_stats)
    d_pos = _position_score(d_stats)

    # Combine position scores into team-level comparison
    core_diff = r_pos["core_score"] - d_pos["core_score"]
    mid_diff = r_pos["mid_score"] - d_pos["mid_score"]
    sup_diff = r_pos["support_score"] - d_pos["support_score"]

    # Weight: supports 40%, cores 35%, mid 25%
    r_count = r_pos["core_count"] + r_pos["mid_count"] + r_pos["support_count"]
    d_count = d_pos["core_count"] + d_pos["mid_count"] + d_pos["support_count"]

    if r_count >= 3 and d_count >= 3:
        score = 0.35 * core_diff + 0.25 * mid_diff + 0.40 * sup_diff
    else:
        # Fall back to simple avg if positions can't be determined
        core_valid = r_pos["core_count"] > 0 and d_pos["core_count"] > 0
        mid_valid = r_pos["mid_count"] > 0 and d_pos["mid_count"] > 0
        sup_valid = r_pos["support_count"] > 0 and d_pos["support_count"] > 0
        total_w = 0.0
        score = 0.0
        if core_valid:
            score += 0.35 * core_diff
            total_w += 0.35
        if mid_valid:
            score += 0.25 * mid_diff
            total_w += 0.25
        if sup_valid:
            score += 0.40 * sup_diff
            total_w += 0.40
        if total_w > 0:
            score /= total_w

    score = round(float(max(-1.0, min(1.0, score))), 4)

    # Confidence based on data coverage
    total_players = len(radiant_players) + len(dire_players)
    players_with_data = r_pos["total_players_with_data"] + d_pos["total_players_with_data"]
    confidence = min(1.0, players_with_data / max(total_players, 1))

    return {
        "score": score,
        "confidence": round(float(confidence), 4),
        "radiant": r_pos,
        "dire": d_pos,
    }


def _player_early_stats(
    db_path: str,
    account_id: int,
    hero_id: int,
    position_hint: str | None,
    before_time: int | None = None,
) -> dict:
    """Get a player's historical early-game stats from past matches."""
    with _connect(db_path) as conn:
        if before_time:
            row = conn.execute("""
                SELECT
                    COUNT(*) as matches,
                    AVG(1.0 * gold_10min) as avg_gold_10min,
                    AVG(1.0 * lh_10min) as avg_lh_10min,
                    AVG(1.0 * xp_10min) as avg_xp_10min,
                    AVG(1.0 * kills_10min) as avg_kills_10min,
                    AVG(1.0 * assists_10min) as avg_assists_10min,
                    AVG(1.0 * obs_placed_10min) as avg_obs_10min,
                    AVG(1.0 * sen_placed_10min) as avg_sen_10min,
                    AVG(1.0 * observer_kills_10min) as avg_obs_kills_10min,
                    AVG(1.0 * sentry_kills_10min) as avg_sen_kills_10min,
                    AVG(1.0 * lane_efficiency) as avg_lane_eff
                FROM match_players mp
                JOIN matches m ON mp.match_id = m.match_id
                WHERE mp.account_id = ? AND m.start_time < ?
                  AND mp.gold_10min IS NOT NULL
            """, (account_id, before_time)).fetchone()
        else:
            row = conn.execute("""
                SELECT
                    COUNT(*) as matches,
                    AVG(1.0 * gold_10min) as avg_gold_10min,
                    AVG(1.0 * lh_10min) as avg_lh_10min,
                    AVG(1.0 * xp_10min) as avg_xp_10min,
                    AVG(1.0 * kills_10min) as avg_kills_10min,
                    AVG(1.0 * assists_10min) as avg_assists_10min,
                    AVG(1.0 * obs_placed_10min) as avg_obs_10min,
                    AVG(1.0 * sen_placed_10min) as avg_sen_10min,
                    AVG(1.0 * observer_kills_10min) as avg_obs_kills_10min,
                    AVG(1.0 * sentry_kills_10min) as avg_sen_kills_10min,
                    AVG(1.0 * lane_efficiency) as avg_lane_eff
                FROM match_players mp
                WHERE mp.account_id = ? AND mp.gold_10min IS NOT NULL
            """, (account_id,)).fetchone()

    matches = row["matches"] or 0 if row else 0
    if matches == 0:
        return {
            "matches": 0, "position": position_hint or "unknown",
            "avg_gold_10min": 0, "avg_lh_10min": 0, "avg_xp_10min": 0,
            "avg_kills_10min": 0, "avg_assists_10min": 0,
            "avg_obs_10min": 0, "avg_sen_10min": 0,
            "avg_obs_kills_10min": 0, "avg_sen_kills_10min": 0,
            "avg_lane_eff": 0,
        }

    return {
        "matches": matches,
        "position": position_hint or "unknown",
        "avg_gold_10min": float(row["avg_gold_10min"] or 0),
        "avg_lh_10min": float(row["avg_lh_10min"] or 0),
        "avg_xp_10min": float(row["avg_xp_10min"] or 0),
        "avg_kills_10min": float(row["avg_kills_10min"] or 0),
        "avg_assists_10min": float(row["avg_assists_10min"] or 0),
        "avg_obs_10min": float(row["avg_obs_10min"] or 0),
        "avg_sen_10min": float(row["avg_sen_10min"] or 0),
        "avg_obs_kills_10min": float(row["avg_obs_kills_10min"] or 0),
        "avg_sen_kills_10min": float(row["avg_sen_kills_10min"] or 0),
        "avg_lane_eff": float(row["avg_lane_eff"] or 0),
    }


def _load_hero_position_hints(db_path: str, hero_ids: list[int]) -> dict[int, str]:
    """Infer likely position from hero attributes.

    Returns mapping of hero_id → position ('core', 'mid', 'support').
    Uses primary_attr and roles from the heroes table.
    """
    if not hero_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in hero_ids)
        rows = conn.execute(
            f"SELECT hero_id, primary_attr, roles FROM heroes WHERE hero_id IN ({placeholders})",
            hero_ids,
        ).fetchall()

    result = {}
    for r in rows:
        roles_str = (r["roles"] or "").lower()
        attr = (r["primary_attr"] or "").lower()

        # Heuristic position mapping
        if "carry" in roles_str:
            result[r["hero_id"]] = "core"
        elif "mid" in roles_str or "nuker" in roles_str:
            result[r["hero_id"]] = "mid"
        elif "support" in roles_str:
            result[r["hero_id"]] = "support"
        elif attr == "agi":
            result[r["hero_id"]] = "core"
        elif attr == "int":
            # Int heroes tend to be mids or supports
            if "disabler" in roles_str or "initiator" in roles_str:
                result[r["hero_id"]] = "support"
            else:
                result[r["hero_id"]] = "mid"
        else:  # str
            result[r["hero_id"]] = "core"

    return result


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

def predict_match(
    db_path: str,
    radiant_id: int,
    dire_id: int,
    radiant_heroes: list[int],
    dire_heroes: list[int],
    weights: dict[str, float] | None = None,
    radiant_players: list[int] | None = None,
    dire_players: list[int] | None = None,
    before_time: int | None = None,
) -> dict[str, Any]:
    """Predict the outcome of a Dota 2 match.

    Args:
        db_path: Path to data/dota2.db.
        radiant_id: Radiant team ID.
        dire_id: Dire team ID.
        radiant_heroes: 5 hero IDs for radiant.
        dire_heroes: 5 hero IDs for dire.
        weights: Optional custom weights for each component.
        radiant_players: Optional 5 account IDs for radiant (enables player skill).
        dire_players: Optional 5 account IDs for dire (enables player skill).
        before_time: Unix timestamp for backtesting (only use matches before this).
    Returns:
        Dict with prediction, confidence, and per-component breakdown.
    """
    _validate_lineups(
        radiant_id,
        dire_id,
        radiant_heroes,
        dire_heroes,
        radiant_players,
        dire_players,
    )
    w = _validated_weights(weights)

    # Compute all components
    hero = _compute_hero_matchup_score(db_path, radiant_heroes, dire_heroes)
    form = _compute_team_form(db_path, radiant_id, dire_id)
    draft = _compute_draft_profile(db_path, radiant_heroes, dire_heroes)

    components = {
        "hero_matchup": hero,
        "team_form": form,
        "draft_profile": draft,
    }

    # Player-based components: use if available, otherwise zero-weight them
    if radiant_players and dire_players:
        player = _compute_player_skill(db_path, radiant_players, dire_players, before_time)
        components["player_skill"] = player

        early = _compute_early_game(
            db_path, radiant_players, dire_players,
            radiant_heroes, dire_heroes, before_time,
        )
        components["early_game"] = early
    else:
        # Placeholder zero-confidence entries so _adjust_weights works
        components["player_skill"] = {"score": 0.0, "confidence": 0.0}
        components["early_game"] = {"score": 0.0, "confidence": 0.0}

    # Dynamic weight adjustment: if a component has low confidence, reduce
    # its weight and redistribute proportionally to confident components.
    adjusted = _adjust_weights(w, components)

    # Weighted sum
    total_score = sum(
        adjusted[name] * components[name]["score"] for name in adjusted
    )
    total_score = max(-1.0, min(1.0, total_score))

    # Convert to probability via sigmoid (scaled to avoid extreme outputs)
    win_prob = 1.0 / (1.0 + np.exp(-total_score * 0.5))  # sigmoid scale tuned via grid search

    # Overall confidence: weighted average of component confidences
    overall_confidence = sum(
        adjusted[name] * components[name].get("confidence", 0)
        for name in adjusted
    )
    confidence_label = "high" if overall_confidence > 0.6 else \
                       "medium" if overall_confidence > 0.3 else "low"

    return {
        "radiant_win_prob": round(float(win_prob), 4),
        "confidence": confidence_label,
        "confidence_score": round(float(overall_confidence), 4),
        "raw_score": round(float(total_score), 4),
        "components": components,
        "weights_used": adjusted,
    }


def _validated_weights(weights: dict[str, float] | None) -> dict[str, float]:
    if weights is None:
        return dict(_DEFAULT_WEIGHTS)
    expected = set(_DEFAULT_WEIGHTS)
    actual = set(weights)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "none"
        unknown = ",".join(sorted(actual - expected)) or "none"
        raise ValueError(
            f"weights must define exactly the scorer components "
            f"(missing={missing}; unknown={unknown})"
        )
    values = {name: float(weights[name]) for name in _DEFAULT_WEIGHTS}
    if any(not np.isfinite(value) or value < 0.0 for value in values.values()):
        raise ValueError("weights must be finite and non-negative")
    if sum(values.values()) <= 0.0:
        raise ValueError("at least one weight must be positive")
    return values


def _validate_lineups(
    radiant_id: int,
    dire_id: int,
    radiant_heroes: list[int],
    dire_heroes: list[int],
    radiant_players: list[int] | None,
    dire_players: list[int] | None,
) -> None:
    if radiant_id == dire_id:
        raise ValueError("radiant and dire teams must be different")
    heroes = radiant_heroes + dire_heroes
    if len(radiant_heroes) != 5 or len(dire_heroes) != 5:
        raise ValueError("each side must provide exactly five heroes")
    if any(hero_id <= 0 for hero_id in heroes) or len(set(heroes)) != 10:
        raise ValueError("draft must contain 10 distinct positive hero IDs")
    if (radiant_players is None) != (dire_players is None):
        raise ValueError("player rosters must be provided for both sides or neither")
    if radiant_players is None or dire_players is None:
        return
    players = radiant_players + dire_players
    if len(radiant_players) != 5 or len(dire_players) != 5:
        raise ValueError("each side must provide exactly five account IDs")
    if any(account_id <= 0 for account_id in players) or len(set(players)) != 10:
        raise ValueError("rosters must contain 10 distinct positive account IDs")


def _adjust_weights(
    weights: dict[str, float],
    components: dict[str, dict],
) -> dict[str, float]:
    """Reduce weight of low-confidence components, redistribute to others."""
    adjusted = dict(weights)
    leftover = 0.0
    total = 0.0

    for name in list(adjusted):
        conf = components[name].get("confidence", 0.0)
        # If confidence < 0.3, halve weight; if 0, remove entirely
        if conf <= 0.0:
            leftover += adjusted[name]
            adjusted[name] = 0.0
        elif conf < 0.3:
            half = adjusted[name] * 0.5
            leftover += half
            adjusted[name] = half
        total += adjusted[name]

    # Redistribute leftover to components with confidence > 0
    if leftover > 0 and total > 0:
        for name in adjusted:
            if components[name].get("confidence", 0) > 0:
                adjusted[name] += leftover * (adjusted[name] / total)

    # Normalize
    total = sum(adjusted.values())
    if total > 0:
        adjusted = {k: round(v / total, 4) for k, v in adjusted.items()}

    return adjusted
