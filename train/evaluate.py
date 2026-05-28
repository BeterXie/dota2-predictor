"""Model evaluation metrics."""

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    log_loss,
    roc_auc_score,
)


def evaluate(
    model,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    y_prob: np.ndarray | None = None,
) -> dict[str, float]:
    """Compute evaluation metrics for a trained model.

    Args:
        model: Trained classifier with predict/predict_proba.
        X_test: Test feature matrix.
        y_test: True labels.
        y_prob: Pre-computed probabilities for class 1. If None, computed from model.

    Returns:
        Dict with accuracy, f1, roc_auc, log_loss.
    """
    if y_prob is None:
        y_prob = model.predict_proba(X_test)[:, 1]

    y_pred = (y_prob >= 0.5).astype(int)
    n_classes = len(np.unique(y_test))

    roc_auc = float(roc_auc_score(y_test, y_prob)) if n_classes > 1 else float("nan")

    try:
        ll = float(log_loss(y_test, y_prob, labels=[0, 1]))
    except ValueError:
        ll = float("nan")

    metrics = {
        "accuracy": float(accuracy_score(y_test, y_pred)),
        "f1": float(f1_score(y_test, y_pred, zero_division=0)),
        "roc_auc": roc_auc,
        "log_loss": ll,
    }
    return metrics


def _fmt_cv(metric_name: str, values: list[float]) -> str:
    arr = np.array(values)
    return f"{arr.mean():.4f} +/- {arr.std():.4f}"


def print_report(
    train_metrics: dict[str, float],
    test_metrics: dict[str, float],
    cv_metrics: dict[str, list[float]],
) -> None:
    """Print formatted evaluation report."""
    print("\n" + "=" * 55)
    print("  Model Evaluation Report")
    print("=" * 55)

    print("\n--- Train Set ---")
    for k, v in train_metrics.items():
        print(f"  {k:12s}: {v:.4f}")

    print("\n--- Test Set ---")
    for k, v in test_metrics.items():
        print(f"  {k:12s}: {v:.4f}")

    if cv_metrics:
        print(f"\n--- {len(next(iter(cv_metrics.values())))}-Fold CV (time-based) ---")
        for k, vals in cv_metrics.items():
            print(f"  {k:12s}: {_fmt_cv(k, vals)}")

    print("=" * 55 + "\n")
