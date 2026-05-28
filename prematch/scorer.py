"""Pre-match prediction scorer.

Combines three core signals into a single win-probability estimate:
  1. Hero matchup advantage (ban/pick counter scoring) — 40% weight
  2. Team recent form (win rate + performance)      — 35% weight
  3. Head-to-head record                            — 15% weight
  4. Team historical strength (overall stats)       — 10% weight

Each sub-score is in [-1, 1] (positive = radiant favoured).
Weights are dynamically adjusted when data is missing.
"""

import sqlite3
from typing import Any

import numpy as np

# Default weights when all data is available
_DEFAULT_WEIGHTS = {
    "hero_matchup": 0.40,
    "team_form": 0.35,
    "h2h": 0.15,
    "team_strength": 0.10,
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
) -> dict[str, float]:
    """Compute hero matchup advantage from a 5v5 hero matrix.

    Returns a dict with:
        score: overall advantage in [-1, 1]
        confidence: 0-1 based on data coverage
        details: dict with per-pair breakdown
    """
    pairs = []
    advantages = []
    for rh in radiant_heroes:
        for dh in dire_heroes:
            adv = _hero_advantage(db_path, rh, dh)
            pairs.append((rh, dh, adv))
            advantages.append(adv)

    arr = np.array(advantages)
    data_pairs = sum(1 for a in advantages if a != 0.0)
    coverage = data_pairs / 25.0 if advantages else 0.0

    if len(arr) == 0:
        return {"score": 0.0, "confidence": 0.0, "mean_adv": 0.0}

    mean_adv = float(arr.mean())

    # Dire perspective: inverse
    dire_advantages = []
    for dh in dire_heroes:
        for rh in radiant_heroes:
            dire_advantages.append(_hero_advantage(db_path, dh, rh))

    dire_mean = float(np.array(dire_advantages).mean()) if dire_advantages else 0.0
    net_adv = mean_adv - dire_mean

    # Scale: typical advantage is ±0.05, max is ~±0.15. Map to [-1, 1].
    # Use tanh scaling to avoid extreme values while preserving sign.
    score = np.tanh(net_adv * 8.0)

    # Top favourable/unfavourable matchups for explainability
    sorted_pairs = sorted(pairs, key=lambda x: x[2], reverse=True)
    best = sorted_pairs[:3]
    worst = sorted_pairs[-3:]

    return {
        "score": round(score, 4),
        "confidence": round(coverage, 4),
        "mean_adv": round(net_adv, 4),
        "best_matchups": [(r, d, round(a, 4)) for r, d, a in best],
        "worst_matchups": [(r, d, round(a, 4)) for r, d, a in reversed(worst)],
    }


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

    match_ids = [r["match_id"] for r in rows if "match_id" in r.keys()]

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
# Main scorer
# ---------------------------------------------------------------------------

def predict_match(
    db_path: str,
    radiant_id: int,
    dire_id: int,
    radiant_heroes: list[int],
    dire_heroes: list[int],
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Predict the outcome of a Dota 2 match.

    Args:
        db_path: Path to data/dota2.db.
        radiant_id: Radiant team ID.
        dire_id: Dire team ID.
        radiant_heroes: 5 hero IDs for radiant.
        dire_heroes: 5 hero IDs for dire.
        weights: Optional custom weights for each component.

    Returns:
        Dict with prediction, confidence, and per-component breakdown.
    """
    w = weights or _DEFAULT_WEIGHTS

    # Compute each component
    hero = _compute_hero_matchup_score(db_path, radiant_heroes, dire_heroes)
    form = _compute_team_form(db_path, radiant_id, dire_id)
    h2h = _compute_h2h_score(db_path, radiant_id, dire_id)
    strength = _compute_team_strength(db_path, radiant_id, dire_id)

    components = {
        "hero_matchup": hero,
        "team_form": form,
        "h2h": h2h,
        "team_strength": strength,
    }

    # Dynamic weight adjustment: if a component has low confidence, reduce
    # its weight and redistribute proportionally to confident components.
    adjusted = _adjust_weights(w, components)

    # Weighted sum
    total_score = sum(
        adjusted[name] * components[name]["score"] for name in adjusted
    )
    total_score = max(-1.0, min(1.0, total_score))

    # Convert to probability via sigmoid (scaled to avoid extreme outputs)
    win_prob = 1.0 / (1.0 + np.exp(-total_score * 3.0))

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
