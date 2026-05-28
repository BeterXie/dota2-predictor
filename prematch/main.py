"""Pre-match prediction CLI.

Predict a Dota 2 match outcome before it starts, using only pre-match data:
team history, H2H records, and hero lineup statistics.

Usage:
    python -m prematch.main \\
        --radiant 9247354 --dire 10150538 \\
        --radiant-heroes 1,2,3,4,5 --dire-heroes 6,7,8,9,10 \\
        [--league 19101]
"""

import argparse
import json
import pickle
import sys
from pathlib import Path

import yaml

from .feature_builder import build_prematch_features


def _load_config() -> dict:
    config_path = Path(__file__).with_name("config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def _load_prematch_model(models_dir: str) -> dict:
    """Load the pre-match model bundle from prematch_latest.pkl."""
    latest = Path(models_dir) / "prematch_latest.pkl"
    if not latest.exists():
        raise FileNotFoundError(
            f"Pre-match model not found: {latest}\n"
            f"Train it first: python -m prematch.train"
        )
    with open(latest, "rb") as f:
        return pickle.load(f)


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
    import sqlite3
    conn = sqlite3.connect(db_path)
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


def run(
    radiant_id: int,
    dire_id: int,
    league_id: int,
    radiant_heroes: list[int],
    dire_heroes: list[int],
    db_path: str,
    models_dir: str,
    predictions_dir: str,
) -> None:
    if radiant_id == dire_id:
        print("Error: radiant and dire teams must be different.", file=sys.stderr)
        sys.exit(1)

    _validate_team_exists(db_path, radiant_id)
    _validate_team_exists(db_path, dire_id)

    # Load pre-match model
    print(f"Loading pre-match model from {models_dir} ...")
    bundle = _load_prematch_model(models_dir)
    feature_names = bundle["feature_names"]
    print(
        f"Model expects {len(feature_names)} pre-match features "
        f"(version {bundle.get('timestamp', '?')})."
    )

    # Build features
    print(
        f"Building feature vector for radiant={radiant_id} dire={dire_id} "
        f"with hero lineups..."
    )
    features = build_prematch_features(
        radiant_id, dire_id, league_id,
        radiant_heroes, dire_heroes,
        db_path, feature_names,
    )

    # Predict (reuse predict module's predictor logic)
    print("Running prediction ...")
    import os as _os
    _project_root = Path(__file__).resolve().parent.parent
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))
    from predict.predictor import predict

    result = predict(bundle, features)

    # Output
    print(f"\nRadiant win probability: {result['radiant_win_prob']:.2%}")
    print(f"Confidence: {result['confidence']}")

    if result["top_factors"]:
        print("\nTop contributing factors:")
        for f in result["top_factors"]:
            print(
                f"  {f['factor']:40s} impact={f['impact']:.4f}  "
                f"direction={f['direction']}"
            )

    # Format and save
    from predict.output import format_output, save_prediction

    output = format_output(result, radiant_id, dire_id, league_id, bundle, db_path)
    file_path = save_prediction(output, predictions_dir)

    print(f"\nPrediction saved to {file_path}")
    from predict.output import _sanitize
    print(json.dumps(_sanitize(output), indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="Pre-match Dota 2 outcome prediction (hero lineups required)."
    )
    parser.add_argument("--radiant", type=int, required=True, help="Radiant team ID")
    parser.add_argument("--dire", type=int, required=True, help="Dire team ID")
    parser.add_argument(
        "--radiant-heroes",
        type=str,
        required=True,
        help="Comma-separated radiant hero IDs (5 required), e.g. 1,2,3,4,5",
    )
    parser.add_argument(
        "--dire-heroes",
        type=str,
        required=True,
        help="Comma-separated dire hero IDs (5 required), e.g. 6,7,8,9,10",
    )
    parser.add_argument(
        "--league", type=int, default=0, help="League ID (optional)"
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
    if not Path(models_dir, "prematch_latest.pkl").exists():
        print(
            f"Pre-match model not found: {Path(models_dir) / 'prematch_latest.pkl'}\n"
            f"Train it first: python -m prematch.train",
            file=sys.stderr,
        )
        sys.exit(1)

    run(
        args.radiant, args.dire, args.league,
        radiant_heroes, dire_heroes,
        db_path, models_dir, predictions_dir,
    )


if __name__ == "__main__":
    main()
