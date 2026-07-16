"""Feature engine entry point.

Reads raw match data from data/dota2.db, extracts features for every match,
and writes the resulting DataFrames to data/features/*.parquet and
corresponding materialized tables in the database.

Usage:
    python -m features.main [--force]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

from .db_reader import (
    read_matches,
    read_players,
    read_picks_bans,
    read_gold_advantage,
    read_xp_advantage,
    read_objectives,
    read_teamfights,
    read_teamfight_players,
)
from .parser import (
    extract_match_features,
    extract_team_features,
    extract_hero_features,
    extract_draft_features,
)
from .aggregator import compute_and_merge_aggregates
from .store import to_parquet, to_db_materialized


def _load_config() -> dict:
    config_path = Path(__file__).with_name("config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def run(db_path: str, features_dir: str) -> None:
    print(f"Reading matches from {db_path} ...")
    matches_df = read_matches(db_path)
    if matches_df.empty:
        print("No matches found in database.")
        return

    match_ids = matches_df["match_id"].tolist()
    print(f"Found {len(match_ids)} matches.")

    # Read all auxiliary data in bulk
    print("Loading auxiliary data ...")
    players_df = read_players(db_path, match_ids)
    picks_df = read_picks_bans(db_path, match_ids)
    gold_df = read_gold_advantage(db_path, match_ids)
    xp_df = read_xp_advantage(db_path, match_ids)
    obj_df = read_objectives(db_path, match_ids)
    tf_df = read_teamfights(db_path, match_ids)

    # teamfight_players needs teamfight IDs
    tf_ids = tf_df["id"].tolist() if len(tf_df) > 0 else []
    tfp_df = read_teamfight_players(db_path, tf_ids) if tf_ids else pd.DataFrame()

    # Group by match_id for fast per-match lookups
    players_by_match = dict(list(players_df.groupby("match_id"))) if len(players_df) > 0 else {}
    picks_by_match = dict(list(picks_df.groupby("match_id"))) if len(picks_df) > 0 else {}
    gold_by_match = dict(list(gold_df.groupby("match_id"))) if len(gold_df) > 0 else {}
    xp_by_match = dict(list(xp_df.groupby("match_id"))) if len(xp_df) > 0 else {}
    obj_by_match = dict(list(obj_df.groupby("match_id"))) if len(obj_df) > 0 else {}
    tf_by_match = dict(list(tf_df.groupby("match_id"))) if len(tf_df) > 0 else {}

    # Pre-group teamfight_players by teamfight_id for fast lookup
    tfp_by_tf = dict(list(tfp_df.groupby("teamfight_id"))) if len(tfp_df) > 0 else {}

    # Accumulators
    match_records = []
    team_records = []
    hero_records = []
    draft_records = []

    print("Extracting features ...")
    for i, (_, match_row) in enumerate(matches_df.iterrows()):
        mid = match_row["match_id"]

        match_players = players_by_match.get(mid, pd.DataFrame())
        match_picks = picks_by_match.get(mid, pd.DataFrame())
        match_gold = gold_by_match.get(mid, pd.DataFrame())
        match_xp = xp_by_match.get(mid, pd.DataFrame())
        match_obj = obj_by_match.get(mid, pd.DataFrame())
        match_tf = tf_by_match.get(mid, pd.DataFrame())

        # Gather teamfight_players for all teamfights in this match
        match_tfp_parts = []
        if len(match_tf) > 0:
            for tf_id in match_tf["id"]:
                if tf_id in tfp_by_tf:
                    match_tfp_parts.append(tfp_by_tf[tf_id])
        match_tfp = (
            pd.concat(match_tfp_parts, ignore_index=True)
            if match_tfp_parts
            else pd.DataFrame()
        )

        match_feat = extract_match_features(
            match_row, match_gold, match_xp, match_obj, match_tf, match_tfp,
            players_df=match_players,
        )
        match_records.append(match_feat)
        team_records.extend(
            extract_team_features(
                match_row, match_players, match_feat["first_blood_radiant"]
            )
        )
        hero_records.extend(extract_hero_features(match_players))
        draft_records.extend(extract_draft_features(match_picks))

        if (i + 1) % 500 == 0:
            print(f"  Processed {i + 1}/{len(match_ids)} matches ...")

    print(f"Processed {len(match_ids)} matches.")

    # Build DataFrames
    print("Building DataFrames ...")
    mf = pd.DataFrame(match_records)
    tf = pd.DataFrame(team_records)
    hf = pd.DataFrame(hero_records)
    df = pd.DataFrame(draft_records)

    # Compute aggregated features (team rolling, hero patch, H2H)
    print("Computing aggregated features ...")
    mf, tf, hf = compute_and_merge_aggregates(mf, tf, hf, db_path)

    # Write outputs
    print(f"Writing to {features_dir}/ ...")
    out_dir = str(Path(features_dir).resolve())

    to_parquet(mf, "match_features", out_dir)
    print(f"  match_features.parquet — {len(mf)} rows")

    to_parquet(tf, "team_features", out_dir)
    print(f"  team_features.parquet — {len(tf)} rows")

    to_parquet(hf, "hero_features", out_dir)
    print(f"  hero_features.parquet — {len(hf)} rows")

    to_parquet(df, "draft_features", out_dir)
    print(f"  draft_features.parquet — {len(df)} rows")

    # Write materialized tables to DB
    print(f"Writing materialized tables to {db_path} ...")
    to_db_materialized(mf, "match_feature_cache", db_path)
    to_db_materialized(tf, "team_feature_cache", db_path)
    to_db_materialized(hf, "hero_feature_cache", db_path)
    to_db_materialized(df, "draft_feature_cache", db_path)
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Feature engine")
    parser.add_argument("--force", action="store_true", help="Force rebuild")
    # The pipeline always rewrites its outputs; keep --force for CLI compatibility.
    parser.parse_args()

    config = _load_config()
    db_path = config.get("database", "data/dota2.db")
    features_dir = config.get("features_dir", "data/features")

    # Resolve relative paths from the features/ directory (where config.yaml lives)
    features_pkg = Path(__file__).resolve().parent
    db_path = str(features_pkg / db_path)
    features_dir = str(features_pkg / features_dir)

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    run(db_path, features_dir)


if __name__ == "__main__":
    main()
