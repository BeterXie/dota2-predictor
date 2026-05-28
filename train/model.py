"""XGBoost classifier wrapper with training, cross-validation, and model persistence."""

import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

from .evaluate import evaluate


def train(
    X: pd.DataFrame,
    y: pd.Series,
    params: dict,
) -> XGBClassifier:
    """Train an XGBoost classifier."""
    model = XGBClassifier(**params)
    model.fit(X, y)
    return model


def cross_validate(
    model: XGBClassifier,
    X: pd.DataFrame,
    y: pd.Series,
    n_folds: int = 5,
    random_state: int = 42,
) -> dict[str, list[float]]:
    """Time-series cross-validation.

    Uses TimeSeriesSplit to respect temporal ordering. For each fold, trains
    on earlier matches and evaluates on later matches.
    """
    n_samples = len(X)
    if n_samples < 3:
        # Not enough data for meaningful CV; return empty metrics
        return {}

    if n_samples < n_folds + 1:
        n_folds = max(2, n_samples - 1)

    tscv = TimeSeriesSplit(n_splits=n_folds)
    params = model.get_params()
    metrics_list: dict[str, list[float]] = {
        "accuracy": [],
        "f1": [],
        "roc_auc": [],
        "log_loss": [],
    }

    for train_idx, test_idx in tscv.split(X):
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr, y_te = y.iloc[train_idx], y.iloc[test_idx]

        fold_model = XGBClassifier(**params)
        fold_model.fit(X_tr, y_tr)
        y_prob = fold_model.predict_proba(X_te)[:, 1]

        fold_metrics = evaluate(fold_model, X_te, y_te, y_prob)
        for k in metrics_list:
            if k in fold_metrics:
                metrics_list[k].append(fold_metrics[k])

    return metrics_list


def save_model(
    model: XGBClassifier,
    feature_names: list[str],
    metrics: dict,
    imputer,
    models_dir: str,
) -> str:
    """Save model, feature names, metrics, and imputer to a versioned pickle file.

    Also copies to latest.pkl. Returns the versioned file path.
    """
    out_dir = Path(models_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    versioned = out_dir / f"model_v{timestamp}.pkl"
    latest = out_dir / "latest.pkl"

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
