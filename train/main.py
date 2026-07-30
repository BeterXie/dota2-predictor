"""Model training entry point.

Reads feature parquet files, builds a training matrix, trains an XGBoost
classifier with time-based split, cross-validates, evaluates, and saves the
model bundle to data/models/.
"""

import argparse
import os
from pathlib import Path

import numpy as np
import yaml

from database.engine import require_database_url

from .dataset import build_training_data, split_train_test
from .evaluate import evaluate, print_report
from .model import cross_validate, save_model, train


def _resolve_path(relative: str) -> str:
    """Resolve a config path relative to this module's directory."""
    base = Path(__file__).resolve().parent
    return str((base / relative).resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description="Train Dota 2 match predictor model")
    parser.add_argument(
        "--tune",
        action="store_true",
        help="Run Optuna hyperparameter tuning (not yet implemented)",
    )
    parser.parse_args()

    # Load config
    config_path = _resolve_path("config.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)

    features_dir = _resolve_path(config["features_dir"])
    models_dir = _resolve_path(config["models_dir"])
    database_url = require_database_url(config.get("database_url"))
    model_params = config["model"]["params"]
    training_cfg = config["training"]

    print(f"Features dir: {features_dir}")
    print(f"Models dir:   {models_dir}")
    print("Database:     PostgreSQL")

    # Build training matrix
    X, y, feature_names, start_times, imputer = build_training_data(
        features_dir, database_url
    )
    print(f"\nTraining matrix: {X.shape[0]} matches x {X.shape[1]} features")
    print(f"Target distribution: {y.sum()} radiant wins / {len(y) - y.sum()} dire wins")

    # Time-based split
    X_train, X_test, y_train, y_test = split_train_test(
        X, y, start_times, test_size=training_cfg["test_size"]
    )
    print(f"Train: {len(X_train)} matches  |  Test: {len(X_test)} matches")

    # Train model
    model = train(X_train, y_train, model_params)

    # Evaluate on train set
    y_train_prob = model.predict_proba(X_train)[:, 1]
    train_metrics = evaluate(model, X_train, y_train, y_train_prob)

    # Evaluate on test set
    y_test_prob = model.predict_proba(X_test)[:, 1]
    test_metrics = evaluate(model, X_test, y_test, y_test_prob)

    # Cross-validation
    cv_folds = min(training_cfg["cv_folds"], len(X))
    cv_metrics = cross_validate(model, X, y, n_folds=cv_folds)

    # Build summary metrics dict for saving
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

    # Report
    print_report(train_metrics, test_metrics, cv_metrics)

    # Save model
    versioned_path = save_model(
        model, feature_names, metrics, imputer, models_dir
    )
    print(f"Model saved to: {versioned_path}")
    print(f"Latest copy at: {os.path.join(models_dir, 'latest.pkl')}")


if __name__ == "__main__":
    main()
