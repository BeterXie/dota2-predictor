"""Prediction module entry point.

Usage:
    python -m predict.main --radiant ID --dire ID [--league ID]
"""

import argparse
import json
import sys
from pathlib import Path

import yaml
from shared.sqlite import connect as connect_sqlite

from .feature_builder import build_features
from .output import _sanitize, format_output, save_prediction
from .predictor import load_model, predict


def _load_config() -> dict:
    config_path = Path(__file__).with_name("config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def _validate_team_exists(db_path: str, team_id: int) -> None:
    conn = connect_sqlite(db_path, read_only=True)
    try:
        row = conn.execute("SELECT 1 FROM matches WHERE radiant_team_id = ? OR dire_team_id = ? LIMIT 1",
                           (team_id, team_id)).fetchone()
        if not row:
            print(f"Warning: Team {team_id} has no matches in the database. "
                  f"Prediction will use default values and may be inaccurate.", file=sys.stderr)
    finally:
        conn.close()


def run(
    radiant_id: int,
    dire_id: int,
    league_id: int,
    db_path: str,
    models_dir: str,
    predictions_dir: str,
) -> None:
    if radiant_id == dire_id:
        print("Error: radiant and dire teams must be different.", file=sys.stderr)
        sys.exit(1)

    _validate_team_exists(db_path, radiant_id)
    _validate_team_exists(db_path, dire_id)

    print(f"Loading model from {models_dir} ...")
    bundle = load_model(models_dir)

    feature_names = bundle["feature_names"]
    print(f"Model expects {len(feature_names)} features (version {bundle.get('timestamp', '?')}).")

    print(f"Building feature vector for radiant={radiant_id} dire={dire_id} ...")
    features = build_features(radiant_id, dire_id, league_id, db_path, feature_names)

    print("Running prediction ...")
    result = predict(bundle, features)

    print(f"\nRadiant win probability: {result['radiant_win_prob']:.2%}")
    print(f"Confidence: {result['confidence']}")

    if result["top_factors"]:
        print("\nTop contributing factors:")
        for f in result["top_factors"]:
            print(f"  {f['factor']:40s} impact={f['impact']:.4f}  direction={f['direction']}")

    output = format_output(result, radiant_id, dire_id, league_id, bundle, db_path)
    file_path = save_prediction(output, predictions_dir)

    print(f"\nPrediction saved to {file_path}")
    print(json.dumps(_sanitize(output), indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="Predict Dota 2 match outcome between two teams."
    )
    parser.add_argument(
        "--radiant", type=int, required=True, help="Radiant team ID"
    )
    parser.add_argument(
        "--dire", type=int, required=True, help="Dire team ID"
    )
    parser.add_argument(
        "--league", type=int, default=0, help="League ID (optional)"
    )
    args = parser.parse_args()

    config = _load_config()
    pkg_dir = Path(__file__).resolve().parent

    db_path = str(pkg_dir / config.get("database", "../data/dota2.db"))
    models_dir = str(pkg_dir / config.get("models_dir", "../data/models"))
    predictions_dir = str(pkg_dir / config.get("predictions_dir", "../data/predictions"))

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    if not Path(models_dir, "prematch_latest.pkl").exists():
        print(
            f"Pre-match model not found: {Path(models_dir) / 'prematch_latest.pkl'}\n"
            f"Train it first: python -m prematch.train",
            file=sys.stderr,
        )
        sys.exit(1)

    run(args.radiant, args.dire, args.league, db_path, models_dir, predictions_dir)


if __name__ == "__main__":
    main()
