"""Regression test: run prematch scorer on all historical matches and measure accuracy.

Usage:
    python scripts/regression_test.py                    # basic accuracy (in-sample)
    python scripts/regression_test.py --time-split 0.7   # train/test split by time (no leakage)
    python scripts/regression_test.py --rolling          # rolling window backtest (strictest)
"""

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prematch.scorer as _scorer
from live_betting.database_protocol import online_backup
from shared.sqlite import connect as connect_sqlite


COMPONENT_KEYS = tuple(_scorer._DEFAULT_WEIGHTS)
CAUSAL_BACKTEST_WEIGHTS = {
    name: value if name in {"team_form", "player_skill", "early_game"} else 0.0
    for name, value in _scorer._DEFAULT_WEIGHTS.items()
}


def get_matches(db_path: str) -> list[dict]:
    """Fetch all matches with hero picks and player account IDs from the database."""
    conn = connect_sqlite(db_path, read_only=True, row_factory=sqlite3.Row)

    matches = conn.execute(
        """SELECT match_id, radiant_team_id, dire_team_id, radiant_win, start_time
           FROM matches
           WHERE start_time IS NOT NULL
           ORDER BY start_time ASC"""
    ).fetchall()

    result = []
    for m in matches:
        players = conn.execute(
            """SELECT hero_id, is_radiant, account_id, player_slot
                 FROM match_players WHERE match_id = ? ORDER BY player_slot""",
            (m["match_id"],),
        ).fetchall()
        r_heroes = [p["hero_id"] for p in players if p["is_radiant"]]
        d_heroes = [p["hero_id"] for p in players if not p["is_radiant"]]
        r_accounts = [p["account_id"] for p in players if p["is_radiant"]]
        d_accounts = [p["account_id"] for p in players if not p["is_radiant"]]
        if len(r_heroes) == 5 and len(d_heroes) == 5:
            result.append({
                "match_id": m["match_id"],
                "radiant_team_id": m["radiant_team_id"],
                "dire_team_id": m["dire_team_id"],
                "radiant_win": bool(m["radiant_win"]),
                "start_time": m["start_time"],
                "radiant_heroes": r_heroes,
                "dire_heroes": d_heroes,
                "radiant_players": r_accounts,
                "dire_players": d_accounts,
            })

    conn.close()
    return result


def run_basic_test(
    db_path: str,
    matches: list[dict],
    *,
    weights: dict[str, float] | None = None,
    include_players: bool = True,
) -> dict:
    """Run predictions on all matches (in-sample, may have minor leakage)."""
    correct = 0
    total = 0
    results = []

    print(f"Testing {len(matches)} matches...")
    for i, m in enumerate(matches):
        try:
            pred = _scorer.predict_match(
                db_path,
                m["radiant_team_id"],
                m["dire_team_id"],
                m["radiant_heroes"],
                m["dire_heroes"],
                weights=weights,
                radiant_players=m.get("radiant_players") if include_players else None,
                dire_players=m.get("dire_players") if include_players else None,
            )
            predicted_win = pred["radiant_win_prob"] > 0.5
            actual_win = m["radiant_win"]
            is_correct = predicted_win == actual_win

            if is_correct:
                correct += 1
            total += 1

            results.append({
                **m,
                "pred_prob": pred["radiant_win_prob"],
                "confidence": pred["confidence"],
                "confidence_score": pred["confidence_score"],
                "predicted_radiant_win": predicted_win,
                "correct": is_correct,
            })

            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(matches)} | accuracy so far: {correct/total:.2%}")

        except Exception as e:
            print(f"  Error on match {m['match_id']}: {e}", file=sys.stderr)

    accuracy = correct / total if total > 0 else 0
    return {"accuracy": accuracy, "correct": correct, "total": total, "results": results}


def run_time_split_test(
    db_path: str,
    matches: list[dict],
    split_ratio: float = 0.7,
    *,
    weights: dict[str, float] | None = None,
    include_players: bool = True,
) -> dict:
    """Split by time: first split_ratio for training, rest for testing.

    Only causal match-history components are enabled because the database does
    not retain point-in-time snapshots for global hero aggregates.
    """
    split_idx = int(len(matches) * split_ratio)
    train_matches = matches[:split_idx]
    test_matches = matches[split_idx:]

    print(f"Time split: {len(train_matches)} train / {len(test_matches)} test")
    print("(Using only training-period data from a temporary DB copy)")
    weights = _causal_backtest_weights(weights)

    import tempfile

    # Create a temp DB with only training-period data
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()

    Path(tmp_db.name).unlink()
    online_backup(Path(db_path), Path(tmp_db.name))

    # Remove future matches from temp DB
    conn = connect_sqlite(tmp_db.name)
    test_match_ids = [m["match_id"] for m in test_matches]
    for mid in test_match_ids:
        conn.execute("DELETE FROM match_players WHERE match_id = ?", (mid,))
        conn.execute("DELETE FROM picks_bans WHERE match_id = ?", (mid,))
        conn.execute("DELETE FROM teamfight_players WHERE teamfight_id IN "
                     "(SELECT id FROM teamfights WHERE match_id = ?)", (mid,))
        conn.execute("DELETE FROM teamfights WHERE match_id = ?", (mid,))
        conn.execute("DELETE FROM gold_advantage WHERE match_id = ?", (mid,))
        conn.execute("DELETE FROM xp_advantage WHERE match_id = ?", (mid,))
        conn.execute("DELETE FROM objectives WHERE match_id = ?", (mid,))
        conn.execute("DELETE FROM chat WHERE match_id = ?", (mid,))
        conn.execute("DELETE FROM matches WHERE match_id = ?", (mid,))
    conn.commit()
    conn.close()

    correct = 0
    total = 0
    results = []

    print(f"Testing {len(test_matches)} matches...")
    for i, m in enumerate(test_matches):
        try:
            pred = _scorer.predict_match(
                tmp_db.name,
                m["radiant_team_id"],
                m["dire_team_id"],
                m["radiant_heroes"],
                m["dire_heroes"],
                weights=weights,
                radiant_players=m.get("radiant_players") if include_players else None,
                dire_players=m.get("dire_players") if include_players else None,
            )
            predicted_win = pred["radiant_win_prob"] > 0.5
            is_correct = predicted_win == m["radiant_win"]

            if is_correct:
                correct += 1
            total += 1

            results.append({
                **m,
                "pred_prob": pred["radiant_win_prob"],
                "confidence": pred["confidence"],
                "predicted_radiant_win": predicted_win,
                "correct": is_correct,
            })

            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(test_matches)} | accuracy so far: {correct/total:.2%}")

        except Exception as e:
            print(f"  Error on match {m['match_id']}: {e}", file=sys.stderr)

    # Cleanup
    try:
        Path(tmp_db.name).unlink()
    except OSError:
        pass

    accuracy = correct / total if total > 0 else 0
    return {"accuracy": accuracy, "correct": correct, "total": total, "results": results}


def run_rolling_test(
    db_path: str,
    matches: list[dict],
    window: int = 100,
    *,
    weights: dict[str, float] | None = None,
    include_players: bool = True,
) -> dict:
    """Rolling window backtest: for each match, use only the prior `window` matches.

    Global hero aggregates are disabled because they are not versioned by
    availability time in the current schema.
    """
    import tempfile

    if window < 1:
        raise ValueError("window must be at least one")
    weights = _causal_backtest_weights(weights)

    # Sort by time
    matches = sorted(matches, key=lambda m: m["start_time"])

    # Create base DB with hero_matchups and heroes (static data only)
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    Path(tmp_db.name).unlink()
    online_backup(Path(db_path), Path(tmp_db.name))

    # Remove ALL matches from temp DB (will add back per-iteration)
    conn = connect_sqlite(tmp_db.name)
    conn.execute("DELETE FROM match_players")
    conn.execute("DELETE FROM picks_bans")
    conn.execute("DELETE FROM teamfights")
    conn.execute("DELETE FROM teamfight_players")
    conn.execute("DELETE FROM gold_advantage")
    conn.execute("DELETE FROM xp_advantage")
    conn.execute("DELETE FROM objectives")
    conn.execute("DELETE FROM chat")
    conn.execute("DELETE FROM matches")
    conn.commit()
    conn.close()

    correct = 0
    total = 0
    results = []

    print(f"Rolling window backtest (window={window}): {len(matches)} matches...")
    for i, m in enumerate(matches):
        if i == 0:
            # First match: can't predict (no data yet), but include it for future
            _insert_match_into_db(db_path, tmp_db.name, m["match_id"])
            continue

        try:
            pred = _scorer.predict_match(
                tmp_db.name,
                m["radiant_team_id"],
                m["dire_team_id"],
                m["radiant_heroes"],
                m["dire_heroes"],
                weights=weights,
                radiant_players=m.get("radiant_players") if include_players else None,
                dire_players=m.get("dire_players") if include_players else None,
                before_time=m["start_time"],
            )
            predicted_win = pred["radiant_win_prob"] > 0.5
            is_correct = predicted_win == m["radiant_win"]

            if is_correct:
                correct += 1
            total += 1

            results.append({
                **m,
                "pred_prob": pred["radiant_win_prob"],
                "confidence": pred["confidence"],
                "predicted_radiant_win": predicted_win,
                "correct": is_correct,
            })

        except Exception as e:
            print(f"  Error on match {m['match_id']}: {e}", file=sys.stderr)

        # After prediction, insert this match into the temp DB for future predictions
        _insert_match_into_db(db_path, tmp_db.name, m["match_id"])

        # Trim old matches beyond window
        if i >= window:
            _trim_old_matches(tmp_db.name, matches[i - window]["match_id"])

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(matches)} | accuracy so far: {correct/total:.2%}" if total > 0 else f"  {i+1}/{len(matches)} | no predictions yet")

    # Cleanup
    try:
        Path(tmp_db.name).unlink()
    except OSError:
        pass

    accuracy = correct / total if total > 0 else 0
    return {"accuracy": accuracy, "correct": correct, "total": total, "results": results}


def _insert_match_into_db(src_db: str, dst_db: str, match_id: int) -> None:
    """Copy a single match and its related data from src to dst DB."""
    src = connect_sqlite(src_db, read_only=True, row_factory=sqlite3.Row)
    dst = connect_sqlite(dst_db)

    # Insert match
    row = src.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if row:
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row.keys())
        dst.execute(
            f"INSERT OR REPLACE INTO matches ({cols}) VALUES ({placeholders})",
            tuple(row),
        )

    # Insert match_players
    players = src.execute("SELECT * FROM match_players WHERE match_id = ?", (match_id,)).fetchall()
    for p in players:
        cols = ", ".join(p.keys())
        placeholders = ", ".join("?" for _ in p.keys())
        dst.execute(
            f"INSERT OR REPLACE INTO match_players ({cols}) VALUES ({placeholders})",
            tuple(p),
        )

    # Insert picks_bans
    pbs = src.execute("SELECT * FROM picks_bans WHERE match_id = ?", (match_id,)).fetchall()
    for pb in pbs:
        cols = ", ".join(pb.keys())
        placeholders = ", ".join("?" for _ in pb.keys())
        dst.execute(
            f"INSERT OR REPLACE INTO picks_bans ({cols}) VALUES ({placeholders})",
            tuple(pb),
        )

    # Also copy teams if needed
    row_dict = dict(row) if row else {}
    for side, team_key in [("radiant", "radiant_team_id"), ("dire", "dire_team_id")]:
        tid = row_dict.get(team_key)
        if tid:
            team = src.execute("SELECT * FROM teams WHERE team_id = ?", (tid,)).fetchone()
            if team:
                cols = ", ".join(team.keys())
                placeholders = ", ".join("?" for _ in team.keys())
                dst.execute(
                    f"INSERT OR REPLACE INTO teams ({cols}) VALUES ({placeholders})",
                    tuple(team),
                )

    dst.commit()
    src.close()
    dst.close()


def _trim_old_matches(db_path: str, oldest_to_keep: int) -> None:
    """Delete matches older than the given match_id from the DB."""
    conn = connect_sqlite(db_path)
    conn.execute("DELETE FROM match_players WHERE match_id = ?", (oldest_to_keep,))
    conn.execute("DELETE FROM matches WHERE match_id = ?", (oldest_to_keep,))
    conn.commit()
    conn.close()


def _causal_backtest_weights(
    weights: dict[str, float] | None,
) -> dict[str, float]:
    selected = dict(CAUSAL_BACKTEST_WEIGHTS if weights is None else weights)
    _scorer._validated_weights(selected)
    unavailable = {
        name for name in ("hero_matchup", "draft_profile") if selected[name] != 0.0
    }
    if unavailable:
        raise ValueError(
            "causal backtests require zero weight for unversioned components: "
            + ",".join(sorted(unavailable))
        )
    return selected


def _parse_weights(value: str) -> dict[str, float]:
    values = [float(item) for item in value.split(",")]
    if len(values) != len(COMPONENT_KEYS):
        raise ValueError(f"--weights requires exactly {len(COMPONENT_KEYS)} values")
    weights = dict(zip(COMPONENT_KEYS, values))
    _scorer._validated_weights(weights)
    return weights


def print_report(result: dict, label: str = "BASIC") -> None:
    """Pretty-print regression test report."""
    print(f"\n{'='*60}")
    print(f"  {label} REGRESSION TEST REPORT")
    print(f"{'='*60}")
    print(f"  Total predictions: {result['total']}")
    print(f"  Correct predictions: {result['correct']}")
    print(f"  Accuracy: {result['accuracy']:.2%}")

    if result["total"] == 0:
        return

    results = result["results"]

    # By confidence level
    by_conf = defaultdict(lambda: {"correct": 0, "total": 0})
    for r in results:
        by_conf[r["confidence"]]["total"] += 1
        if r["correct"]:
            by_conf[r["confidence"]]["correct"] += 1

    print("\n  Accuracy by confidence level:")
    for level in ["high", "medium", "low"]:
        if level in by_conf:
            acc = by_conf[level]["correct"] / by_conf[level]["total"]
            print(f"    {level:7s}: {by_conf[level]['correct']:3d}/{by_conf[level]['total']:3d} = {acc:.2%}")

    # Calibration check: group by predicted probability buckets
    probs = [r["pred_prob"] for r in results]
    actuals = [r["radiant_win"] for r in results]
    print(f"\n  Mean predicted probability: {np.mean(probs):.4f}")
    print(f"  Actual radiant win rate:    {np.mean(actuals):.4f}")

    # Win rate when radiant was predicted to win
    pred_radiant = [r for r in results if r["predicted_radiant_win"]]
    pred_dire = [r for r in results if not r["predicted_radiant_win"]]
    if pred_radiant:
        r_acc = sum(1 for r in pred_radiant if r["correct"]) / len(pred_radiant)
        print(f"\n  When predicted Radiant win ({len(pred_radiant)} matches): {r_acc:.2%} correct")
    if pred_dire:
        d_acc = sum(1 for r in pred_dire if r["correct"]) / len(pred_dire)
        print(f"  When predicted Dire win    ({len(pred_dire)} matches): {d_acc:.2%} correct")

    # Brier score (lower is better)
    brier = np.mean([(p - a) ** 2 for p, a in zip(probs, actuals)])
    print(f"\n  Brier score: {brier:.4f} (0 = perfect, 0.25 = random)")

    # Baseline: always predict radiant
    radiant_actual = sum(1 for r in results if r["radiant_win"])
    baseline_radiant = radiant_actual / len(results)
    baseline_dire = 1 - baseline_radiant
    baseline_acc = max(baseline_radiant, baseline_dire)
    print(f"\n  Baseline accuracy (always predict majority): {baseline_acc:.2%}")
    print(f"    Radiant wins: {radiant_actual}/{len(results)} = {baseline_radiant:.2%}")


def main():
    parser = argparse.ArgumentParser(description="Regression test prematch scorer")
    parser.add_argument("--db", default=None, help="Path to database")
    parser.add_argument("--time-split", type=float, default=None,
                        help="Train/test split ratio by time (e.g. 0.7)")
    parser.add_argument("--rolling", action="store_true",
                        help="Rolling window backtest (no data leakage)")
    parser.add_argument("--window", type=int, default=100,
                        help="Rolling window size (default: 100)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of matches to test")
    parser.add_argument("--weights", type=str, default=None,
                        help="Comma-separated weights: hero_matchup,team_form,draft_profile,player_skill,early_game")
    parser.add_argument("--no-players", action="store_true",
                        help="Disable player-based components (player_skill, early_game)")
    args = parser.parse_args()

    pkg_dir = Path(__file__).resolve().parent.parent
    db_path = args.db or str(pkg_dir / "data" / "dota2.db")

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    # Parse custom weights
    custom_weights = None
    if args.weights:
        try:
            custom_weights = _parse_weights(args.weights)
            if args.rolling or args.time_split is not None:
                custom_weights = _causal_backtest_weights(custom_weights)
        except ValueError as error:
            parser.error(str(error))
        print(f"Custom weights: {custom_weights}")

    print(f"Loading matches from {db_path}...")
    matches = get_matches(db_path)
    print(f"Found {len(matches)} matches with hero data.")

    if args.limit:
        matches = matches[:args.limit]
        print(f"Limited to {args.limit} matches.")

    start = time.time()

    if args.rolling:
        result = run_rolling_test(
            db_path,
            matches,
            args.window,
            weights=custom_weights,
            include_players=not args.no_players,
        )
        label = f"ROLLING (window={args.window})"
    elif args.time_split is not None:
        result = run_time_split_test(
            db_path,
            matches,
            args.time_split,
            weights=custom_weights,
            include_players=not args.no_players,
        )
        label = f"TIME-SPLIT ({args.time_split:.0%} train)"
    else:
        result = run_basic_test(
            db_path,
            matches,
            weights=custom_weights,
            include_players=not args.no_players,
        )
        label = "BASIC (in-sample)"

    elapsed = time.time() - start
    print(f"\nTest completed in {elapsed:.1f}s")

    print_report(result, label)


if __name__ == "__main__":
    main()
