"""Pre-match prediction CLI.

Predict a Dota 2 match outcome before it starts using a weighted scoring system
that combines hero matchup advantage, team recent form, H2H records, and team
historical strength.

Usage:
    python -m prematch.main \\
        --radiant 9247354 --dire 10150538 \\
        --radiant-heroes 1,2,3,4,5 --dire-heroes 6,7,8,9,10 \\
        [--league 19101] [--ml]
"""

import argparse
import json
import sys
from pathlib import Path

import yaml
from shared.sqlite import connect as connect_sqlite

from .scorer import predict_match

_WEIGHT_KEYS = (
    "hero_matchup",
    "team_form",
    "draft_profile",
    "player_skill",
    "early_game",
)


def _load_config() -> dict:
    config_path = Path(__file__).with_name("config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def _parse_hero_list(raw: str, side: str) -> list[int]:
    """Parse a comma-separated hero ID string into a list of ints."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 5:
        print(
            f"Error: --{side}-heroes requires exactly 5 hero IDs, got {len(parts)}.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return [int(p) for p in parts]
    except ValueError:
        print(
            f"Error: --{side}-heroes must be comma-separated integers, got: {raw}",
            file=sys.stderr,
        )
        sys.exit(1)


def _validate_team_exists(db_path: str, team_id: int) -> None:
    conn = connect_sqlite(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM matches WHERE radiant_team_id = ? OR dire_team_id = ? LIMIT 1",
            (team_id, team_id),
        ).fetchone()
        if not row:
            print(
                f"Warning: Team {team_id} has no matches in the database. "
                f"Prediction will use default values and may be inaccurate.",
                file=sys.stderr,
            )
    finally:
        conn.close()


def _run_scorer(
    radiant_id: int,
    dire_id: int,
    league_id: int,
    radiant_heroes: list[int],
    dire_heroes: list[int],
    db_path: str,
    predictions_dir: str,
    weights: dict[str, float] | None = None,
) -> None:
    """Run prediction using the heuristic scoring system."""
    print("Computing prediction scores from hero matchups, team form, H2H...")
    result = predict_match(
        db_path,
        radiant_id,
        dire_id,
        radiant_heroes,
        dire_heroes,
        weights=weights,
    )

    # Output
    print(f"\n  Radiant win probability: {result['radiant_win_prob']:.2%}")
    print(f"  Confidence: {result['confidence']} ({result['confidence_score']:.2f})")

    print("\n  Component breakdown:")
    for name, comp in result["components"].items():
        w = result["weights_used"][name]
        score = comp["score"]
        bar = "█" * int(abs(score) * 20) if abs(score) > 0.01 else "─"
        side = "R" if score > 0 else ("D" if score < 0 else " ")
        print(f"  [{w:.0%}] {name:16s} {score:+.4f} {bar} {side}")
        if "radiant_win_rate" in comp:
            print(f"       radiant recent WR={comp['radiant_win_rate']:.0%}  "
                  f"dire={comp['dire_win_rate']:.0%}")
        if "total_matches" in comp and comp["total_matches"] > 0:
            print(f"       H2H: {comp.get('radiant_wins',0)}-"
                  f"{comp['total_matches']-comp.get('radiant_wins',0)} "
                  f"({comp['total_matches']} matches)")
        if "best_matchups" in comp and comp["best_matchups"]:
            r, d, a = comp["best_matchups"][0]
            print(f"       best matchup: hero {r} vs {d} ({a:+.4f})")

    # Save prediction
    from predict.output import format_output, save_prediction, _sanitize

    # Wrap result to match the expected format
    prediction = {
        "radiant_win_prob": result["radiant_win_prob"],
        "confidence": result["confidence"],
        "top_factors": _format_factors(result),
    }

    output = format_output(prediction, radiant_id, dire_id, league_id,
                           {"timestamp": "scorer", "metrics": {}}, db_path)
    file_path = save_prediction(output, predictions_dir)

    print(f"\n  Prediction saved to {file_path}")
    print(json.dumps(_sanitize(output), indent=2, ensure_ascii=False))


def _format_factors(result: dict) -> list[dict]:
    """Extract top contributing factors from the scoring result."""
    factors = []
    for name, comp in result["components"].items():
        score = comp["score"]
        if abs(score) < 0.01:
            continue
        direction = "radiant" if score > 0 else "dire"
        factors.append({
            "factor": name,
            "impact": round(abs(score), 4),
            "direction": direction,
        })
    factors.sort(key=lambda x: x["impact"], reverse=True)
    return factors


def _parse_weights(raw: str) -> dict[str, float]:
    """Parse the five scorer weights in the documented component order."""
    values = [part.strip() for part in raw.split(",")]
    if len(values) != len(_WEIGHT_KEYS):
        raise ValueError(
            f"--weights requires {len(_WEIGHT_KEYS)} comma-separated values "
            f"({','.join(_WEIGHT_KEYS)})"
        )
    try:
        parsed = [float(value) for value in values]
    except ValueError as exc:
        raise ValueError("--weights values must be numbers") from exc
    return dict(zip(_WEIGHT_KEYS, parsed))


def _run_ml(
    radiant_id: int,
    dire_id: int,
    league_id: int,
    radiant_heroes: list[int],
    dire_heroes: list[int],
    db_path: str,
    models_dir: str,
    predictions_dir: str,
) -> None:
    """Run prediction using the XGBoost ML model (fallback)."""
    from predict.predictor import load_model, predict
    from .feature_builder import build_prematch_features

    bundle = load_model(models_dir)
    feature_names = bundle["feature_names"]
    print(f"Model expects {len(feature_names)} features "
          f"(version {bundle.get('timestamp', '?')}).")

    features = build_prematch_features(
        radiant_id, dire_id, league_id,
        radiant_heroes, dire_heroes,
        db_path, feature_names,
    )

    result = predict(bundle, features)

    print(f"\nRadiant win probability: {result['radiant_win_prob']:.2%}")
    print(f"Confidence: {result['confidence']}")

    if result["top_factors"]:
        print("\nTop contributing factors:")
        for f in result["top_factors"]:
            print(f"  {f['factor']:40s} impact={f['impact']:.4f}  "
                  f"direction={f['direction']}")

    from predict.output import format_output, save_prediction, _sanitize
    output = format_output(result, radiant_id, dire_id, league_id, bundle, db_path)
    file_path = save_prediction(output, predictions_dir)
    print(f"\nPrediction saved to {file_path}")
    print(json.dumps(_sanitize(output), indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="Pre-match Dota 2 outcome prediction (hero lineups required)."
    )
    parser.add_argument("--radiant", type=int, required=True, help="Radiant team ID")
    parser.add_argument("--dire", type=int, required=True, help="Dire team ID")
    parser.add_argument(
        "--radiant-heroes", type=str, required=True,
        help="Comma-separated radiant hero IDs (5 required), e.g. 1,2,3,4,5",
    )
    parser.add_argument(
        "--dire-heroes", type=str, required=True,
        help="Comma-separated dire hero IDs (5 required), e.g. 6,7,8,9,10",
    )
    parser.add_argument("--league", type=int, default=0, help="League ID (optional)")
    parser.add_argument(
        "--ml", action="store_true",
        help="Use XGBoost ML model instead of heuristic scorer",
    )
    parser.add_argument(
        "--weights", type=str, default=None,
        help="Custom weights: hero_matchup,team_form,draft_profile,player_skill,early_game (comma-sep)",
    )
    args = parser.parse_args()

    radiant_heroes = _parse_hero_list(args.radiant_heroes, "radiant")
    dire_heroes = _parse_hero_list(args.dire_heroes, "dire")

    config = _load_config()
    pkg_dir = Path(__file__).resolve().parent

    db_path = str(pkg_dir / config.get("database", "../data/dota2.db"))
    models_dir = str(pkg_dir / config.get("models_dir", "../data/models"))
    predictions_dir = str(pkg_dir / config.get("predictions_dir", "../data/predictions"))

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    if args.ml:
        if not Path(models_dir, "prematch_latest.pkl").exists():
            print(
                f"Pre-match model not found: {Path(models_dir) / 'prematch_latest.pkl'}\n"
                f"Train it first: python -m prematch.train",
                file=sys.stderr,
            )
            sys.exit(1)
        _run_ml(args.radiant, args.dire, args.league,
                radiant_heroes, dire_heroes,
                db_path, models_dir, predictions_dir)
    else:
        try:
            custom_weights = _parse_weights(args.weights) if args.weights else None
        except ValueError as exc:
            parser.error(str(exc))
        _run_scorer(args.radiant, args.dire, args.league,
                    radiant_heroes, dire_heroes,
                    db_path, predictions_dir, custom_weights)


if __name__ == "__main__":
    main()
