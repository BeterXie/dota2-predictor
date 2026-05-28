"""Train a pre-match-only XGBoost model.

Reads the full feature set from data/features/*.parquet, filters to pre-match
features, trains on the subset, and saves to data/models/prematch_latest.pkl.

Usage:
    python -m prematch.train
"""

import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

# Add project root so we can import from train/
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from train.dataset import build_training_data, split_train_test
from train.evaluate import evaluate, print_report
from train.model import cross_validate, train

from .feature_list import PREMATCH_FEATURES


def _resolve_path(relative: str) -> str:
    base = Path(__file__).resolve().parent
    return str((base / relative).resolve())


def _save_prematch_model(
    model,
    feature_names: list[str],
    metrics: dict,
    imputer,
    models_dir: str,
) -> str:
    out_dir = Path(models_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    versioned = out_dir / f"prematch_v{timestamp}.pkl"
    latest = out_dir / "prematch_latest.pkl"

    bundle = {
        "model": model,
        "feature_names": feature_names,
        "metrics": metrics,
        "imputer": imputer,
        "timestamp": timestamp,
    }

    with open(versioned, "wb") as f:
        pickle.dump(bundle, f)

    with open(latest, "wb") as f:
        pickle.dump(bundle, f)

    return str(versioned)


def main() -> None:
    config_path = _resolve_path("config.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)

    db_path = _resolve_path(config["database"])
    models_dir = _resolve_path(config["models_dir"])

    # Use train module's config for model params (shared config)
    train_config_path = _resolve_path("../train/config.yaml")
    with open(train_config_path) as f:
        train_config = yaml.safe_load(f)

    features_dir = _resolve_path(train_config["features_dir"])
    model_params = train_config["model"]["params"]
    training_cfg = train_config["training"]

    print(f"Features dir: {features_dir}")
    print(f"Models dir:   {models_dir}")
    print(f"Database:     {db_path}")

    # Build full training matrix
    X, y, feature_names, start_times, imputer = build_training_data(
        features_dir, db_path
    )
    print(f"\nFull matrix: {X.shape[0]} matches x {X.shape[1]} features")

    # Filter to pre-match features only
    prematch_cols = [c for c in feature_names if c in PREMATCH_FEATURES]
    missing = [c for c in PREMATCH_FEATURES if c not in feature_names]
    if missing:
        print(f"Warning: {len(missing)} pre-match features not found in training data: {missing[:5]}...")

    X_pm = X[prematch_cols].copy()

    # Drop all-NaN columns (no signal)
    all_nan = X_pm.columns[X_pm.isna().all()].tolist()
    if all_nan:
        X_pm = X_pm.drop(columns=all_nan)
        prematch_cols = [c for c in prematch_cols if c not in all_nan]

    # Fill remaining NaN with 0
    X_pm = X_pm.fillna(0)

    print(f"Pre-match matrix: {X_pm.shape[0]} matches x {X_pm.shape[1]} features")
    print(f"Target distribution: {y.sum()} radiant wins / {len(y) - y.sum()} dire wins")

    # Time-based split
    X_train, X_test, y_train, y_test = split_train_test(
        X_pm, y, start_times, test_size=training_cfg["test_size"]
    )
    print(f"Train: {len(X_train)} matches  |  Test: {len(X_test)} matches")

    # Train model
    model = train(X_train, y_train, model_params)

    # Evaluate
    y_train_prob = model.predict_proba(X_train)[:, 1]
    train_metrics = evaluate(model, X_train, y_train, y_train_prob)

    y_test_prob = model.predict_proba(X_test)[:, 1]
    test_metrics = evaluate(model, X_test, y_test, y_test_prob)

    # Cross-validation
    cv_folds = min(training_cfg["cv_folds"], len(X_pm))
    cv_metrics = cross_validate(model, X_pm, y, n_folds=cv_folds)

    if cv_metrics:
        cv_summary = {
            k: {
                "mean": float(np.array(v).mean()),
                "std": float(np.array(v).std()),
                "values": [float(x) for x in v],
            }
            for k, v in cv_metrics.items()
        }
    else:
        cv_summary = {}

    metrics = {
        "train": train_metrics,
        "test": test_metrics,
        "cv": cv_summary,
    }

    print_report(train_metrics, test_metrics, cv_metrics)

    # Save model
    versioned_path = _save_prematch_model(
        model, prematch_cols, metrics, None, models_dir
    )
    print(f"Model saved to: {versioned_path}")
    print(f"Latest copy at: {os.path.join(models_dir, 'prematch_latest.pkl')}")


if __name__ == "__main__":
    main()
