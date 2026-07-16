"""Grid search for optimal 5-component scorer weights.

Computes component scores once per match, then tries thousands of weight
combinations in memory.

Usage:
    python scripts/grid_search.py [--mode basic|rolling] [--window 150] [--trials 20000]
"""

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sqlite3
import tempfile

import prematch.scorer as S
from live_betting.database_protocol import online_backup
from shared.sqlite import connect as connect_sqlite

COMPONENT_KEYS = ["hero_matchup", "team_form", "draft_profile", "player_skill", "early_game"]


def compute_basic_scores(db_path, matches):
    """Compute all 5 component scores in-sample (no time filter)."""
    results = []
    for i, m in enumerate(matches):
        try:
            hero = S._compute_hero_matchup_score(db_path, m["radiant_heroes"], m["dire_heroes"])
            form = S._compute_team_form(db_path, m["radiant_team_id"], m["dire_team_id"])
            draft = S._compute_draft_profile(db_path, m["radiant_heroes"], m["dire_heroes"])

            has_players = m.get("radiant_players") and m.get("dire_players")
            if has_players:
                player = S._compute_player_skill(db_path, m["radiant_players"], m["dire_players"])
                early = S._compute_early_game(
                    db_path, m["radiant_players"], m["dire_players"],
                    m["radiant_heroes"], m["dire_heroes"],
                )
            else:
                player = {"score": 0.0, "confidence": 0.0}
                early = {"score": 0.0, "confidence": 0.0}

            results.append({
                "actual": m["radiant_win"],
                "hero_matchup": hero["score"], "hero_matchup_conf": hero["confidence"],
                "team_form": form["score"], "team_form_conf": form["confidence"],
                "draft_profile": draft["score"], "draft_profile_conf": draft["confidence"],
                "player_skill": player["score"], "player_skill_conf": player["confidence"],
                "early_game": early["score"], "early_game_conf": early["confidence"],
            })
        except Exception:
            results.append(dict(actual=m["radiant_win"], **{f"{k}": 0 for k in COMPONENT_KEYS},
                                **{f"{k}_conf": 0 for k in COMPONENT_KEYS}))
        if (i + 1) % 50 == 0:
            print(f"  Computing: {i+1}/{len(matches)}")
    return results


def compute_rolling_scores(db_path, matches, window=150):
    """Compute causal components over an exact rolling match window."""
    if window < 1:
        raise ValueError("window must be at least one")
    matches_sorted = sorted(matches, key=lambda m: m["start_time"])

    # Build temp DB with static data only
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    Path(tmp_db.name).unlink()
    online_backup(Path(db_path), Path(tmp_db.name))

    tmp = connect_sqlite(tmp_db.name)
    for table in ["match_players", "picks_bans", "teamfights", "teamfight_players",
                   "gold_advantage", "xp_advantage", "objectives", "chat", "matches"]:
        tmp.execute(f"DELETE FROM {table}")
    tmp.commit()
    tmp.close()

    results = []
    for i, m in enumerate(matches_sorted):
        if i == 0:
            _copy_match(db_path, tmp_db.name, m["match_id"])
            results.append(dict(actual=m["radiant_win"], **{f"{k}": 0 for k in COMPONENT_KEYS},
                                **{f"{k}_conf": 0 for k in COMPONENT_KEYS}))
            continue

        try:
            form = S._compute_team_form(tmp_db.name, m["radiant_team_id"], m["dire_team_id"])

            has_players = m.get("radiant_players") and m.get("dire_players")
            if has_players:
                player = S._compute_player_skill(tmp_db.name, m["radiant_players"],
                                                 m["dire_players"], m["start_time"])
                early = S._compute_early_game(tmp_db.name, m["radiant_players"],
                                              m["dire_players"], m["radiant_heroes"],
                                              m["dire_heroes"], m["start_time"])
            else:
                player = {"score": 0.0, "confidence": 0.0}
                early = {"score": 0.0, "confidence": 0.0}

            results.append({
                "actual": m["radiant_win"],
                "hero_matchup": 0.0, "hero_matchup_conf": 0.0,
                "team_form": form["score"], "team_form_conf": form["confidence"],
                "draft_profile": 0.0, "draft_profile_conf": 0.0,
                "player_skill": player["score"], "player_skill_conf": player["confidence"],
                "early_game": early["score"], "early_game_conf": early["confidence"],
            })
        except Exception:
            results.append(dict(actual=m["radiant_win"], **{f"{k}": 0 for k in COMPONENT_KEYS},
                                **{f"{k}_conf": 0 for k in COMPONENT_KEYS}))

        _copy_match(db_path, tmp_db.name, m["match_id"])
        if i >= window:
            _delete_match(tmp_db.name, matches_sorted[i - window]["match_id"])

        if (i + 1) % 50 == 0:
            print(f"  Computing rolling: {i+1}/{len(matches_sorted)}")

    try:
        Path(tmp_db.name).unlink()
    except OSError:
        pass
    # Drop the first (no-data) entry
    return results[1:]


def evaluate(scores, weights, sigmoid_scale=1.0):
    """Evaluate weight combination against cached scores.

    Replicates _adjust_weights + scoring logic exactly.
    """
    correct = 0
    total = 0
    probabilities = []
    outcomes = []
    for s in scores:
        adjusted = dict(weights)
        leftover = 0.0
        w_total = 0.0

        for name in list(adjusted):
            conf = s.get(f"{name}_conf", 0.0)
            if conf <= 0.0:
                leftover += adjusted[name]
                adjusted[name] = 0.0
            elif conf < 0.3:
                half = adjusted[name] * 0.5
                leftover += half
                adjusted[name] = half
            w_total += adjusted[name]

        if leftover > 0 and w_total > 0:
            for name in adjusted:
                if s.get(f"{name}_conf", 0) > 0:
                    adjusted[name] += leftover * (adjusted[name] / w_total)

        w_total = sum(adjusted.values())
        if w_total > 0:
            adjusted = {k: v / w_total for k, v in adjusted.items()}

        total_score = sum(adjusted[n] * s[n] for n in adjusted)
        total_score = max(-1.0, min(1.0, total_score))
        win_prob = 1.0 / (1.0 + np.exp(-total_score * sigmoid_scale))

        if (win_prob > 0.5) == s["actual"]:
            correct += 1
        total += 1
        probabilities.append(float(win_prob))
        outcomes.append(int(bool(s["actual"])))

    if total == 0:
        return 0.0, correct, total, None, None
    brier = math.fsum(
        (probability - outcome) ** 2
        for probability, outcome in zip(probabilities, outcomes)
    ) / total
    epsilon = 1e-15
    log_loss = -math.fsum(
        outcome * math.log(min(1.0 - epsilon, max(epsilon, probability)))
        + (1 - outcome)
        * math.log(min(1.0 - epsilon, max(epsilon, 1.0 - probability)))
        for probability, outcome in zip(probabilities, outcomes)
    ) / total
    return correct / total, correct, total, brier, log_loss


def _copy_match(src_db, dst_db, match_id):
    src = connect_sqlite(src_db, read_only=True, row_factory=sqlite3.Row)
    dst = connect_sqlite(dst_db)
    row = src.execute("SELECT * FROM matches WHERE match_id=?", (match_id,)).fetchone()
    if row:
        cols = list(row.keys())
        dst.execute(f"INSERT OR REPLACE INTO matches ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(row))
    for p in src.execute("SELECT * FROM match_players WHERE match_id=?", (match_id,)).fetchall():
        cols = list(p.keys())
        dst.execute(f"INSERT OR REPLACE INTO match_players ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(p))
    dst.commit()
    src.close()
    dst.close()


def _delete_match(db_path, match_id):
    connection = connect_sqlite(db_path)
    connection.execute("DELETE FROM match_players WHERE match_id=?", (match_id,))
    connection.execute("DELETE FROM matches WHERE match_id=?", (match_id,))
    connection.commit()
    connection.close()


def _weight_candidates(keys, trials):
    def coarse(remaining_keys, remaining, values):
        if len(remaining_keys) == 1:
            yield {**values, remaining_keys[0]: remaining / 100.0}
            return
        name = remaining_keys[0]
        for value in range(0, remaining + 1, 10):
            yield from coarse(
                remaining_keys[1:], remaining - value, {**values, name: value / 100.0}
            )

    candidates = list(coarse(list(keys), 100, {}))
    for _ in range(trials):
        values = np.random.dirichlet(np.ones(len(keys)))
        candidates.append({name: round(values[i], 4) for i, name in enumerate(keys)})
    return candidates


def main():
    parser = argparse.ArgumentParser(description="Grid search 5-component scorer weights")
    parser.add_argument("--mode", choices=["basic", "rolling"], default="rolling")
    parser.add_argument("--window", type=int, default=150)
    parser.add_argument("--trials", type=int, default=30000)
    args = parser.parse_args()

    pkg_dir = Path(__file__).resolve().parent.parent
    db_path = str(pkg_dir / "data" / "dota2.db")

    from scripts.regression_test import get_matches
    matches = get_matches(db_path)
    print(f"Mode={args.mode}, {len(matches)} matches, {args.trials} random trials\n")

    if args.mode == "rolling":
        scores = compute_rolling_scores(db_path, matches, args.window)
        search_keys = ["team_form", "player_skill", "early_game"]
    else:
        scores = compute_basic_scores(db_path, matches)
        search_keys = COMPONENT_KEYS

    scores = [s for s in scores if any(s.get(k, 0) != 0 for k in COMPONENT_KEYS)]
    print(f"Valid predictions: {len(scores)}\n")

    if not scores:
        raise RuntimeError("no valid component scores were produced")

    best_acc = 0.0
    best_log_loss = math.inf
    best_brier = math.inf
    best_config = None

    np.random.seed(42)
    start = time.time()

    all_weights = []
    for partial in _weight_candidates(search_keys, args.trials):
        all_weights.append({name: partial.get(name, 0.0) for name in COMPONENT_KEYS})

    # Deduplicate
    seen = set()
    unique = []
    for w in all_weights:
        key = tuple(round(w[k], 3) for k in COMPONENT_KEYS)
        if key not in seen:
            seen.add(key)
            unique.append(w)

    sigmoid_scales = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]
    total_combos = len(unique) * len(sigmoid_scales)

    print(f"Testing {len(unique)} unique weights x {len(sigmoid_scales)} sigmoid scales = {total_combos} combinations...\n")

    for scale in sigmoid_scales:
        for w in unique:
            acc, correct, total, brier, log_loss = evaluate(scores, w, scale)
            assert brier is not None and log_loss is not None
            if (log_loss, brier, -acc) < (best_log_loss, best_brier, -best_acc):
                best_log_loss = log_loss
                best_brier = brier
                best_acc = acc
                best_config = (dict(w), scale)
                print(
                    f"  logloss={log_loss:.5f} brier={brier:.5f} "
                    f"acc={acc:.4f} ({correct}/{total})  "
                    f"w={dict({k: round(v,3) for k,v in w.items()})} "
                    f"sigmoid={scale}  ***"
                )

    elapsed = time.time() - start

    radiant_wins = sum(1 for s in scores if s["actual"])
    baseline = max(radiant_wins / len(scores), 1 - radiant_wins / len(scores))

    print(f"\n{'='*60}")
    print(f"Grid search complete ({elapsed:.0f}s)")
    print(f"Mode:        {args.mode}")
    print(f"Best acc:    {best_acc:.2%}")
    print(f"Best Brier:  {best_brier:.5f}")
    print(f"Best logloss:{best_log_loss:.5f}")
    print(f"Best weights: {best_config[0]}")
    print(f"Best sigmoid: {best_config[1]}")
    print(f"Baseline:    {baseline:.2%}")
    print(f"Gain:        {best_acc - baseline:+.2%}")


if __name__ == "__main__":
    main()
