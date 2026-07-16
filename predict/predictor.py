"""Load the trained model and generate predictions with explanations."""

import pickle
from pathlib import Path
from typing import Any

import pandas as pd


def load_model(models_dir: str, filename: str = "prematch_latest.pkl") -> dict:
    """Load a model bundle from a pickle file.

    Defaults to the pre-match model so that predictions only use features
    available before the match starts (team history, H2H, hero lineups).

    Returns the bundle dict with keys: model, feature_names, metrics, imputer, timestamp.
    """
    path = Path(models_dir) / filename
    if not path.exists():
        raise FileNotFoundError(f"Model not found: {path}")
    with open(path, "rb") as f:
        return pickle.load(f)


def _confidence_label(prob: float) -> str:
    """Map win probability to a confidence label."""
    margin = abs(prob - 0.5)
    if margin >= 0.25:
        return "high"
    elif margin >= 0.10:
        return "medium"
    return "low"


def _top_factors(
    model: Any,
    features: pd.DataFrame,
    feature_names: list[str],
    top_n: int = 5,
) -> list[dict]:
    """Identify the top contributing factors for this prediction.

    Uses the model's gain-based feature importances weighted by how far each
    feature value deviates from its expected neutral point (scaled by the
    feature's standard deviation across the row, effectively the absolute
    z-score within the context of what's available).

    Direction is inferred from the feature name prefix:
      - ``radiant_*``  → favours radiant when value is high
      - ``dire_*``     → favours dire when value is high
      - ``diff_*``     → positive favours radiant, negative favours dire
      - otherwise      → neutral

    Returns a list of dicts with keys: factor, impact, direction.
    """
    importances = _get_feature_importances(model, feature_names)

    row = features.iloc[0]
    scores: list[tuple[str, float, str]] = []

    for col in feature_names:
        imp = importances.get(col, 0.0)
        if imp <= 0:
            continue

        val = float(row.get(col, 0))
        abs_val = abs(val)

        # Impact = importance * |value| (normalised by max across features)
        scores.append((col, imp * abs_val, _direction(col, val)))

    if not scores:
        return []

    max_score = max(s[1] for s in scores)
    if max_score == 0:
        return []

    scores.sort(key=lambda x: x[1], reverse=True)
    top = scores[:top_n]

    return [
        {
            "factor": _simplify_name(name),
            "impact": round(score / max_score, 4),
            "direction": direction,
        }
        for name, score, direction in top
        if score > 0
    ]


def _get_feature_importances(model: Any, feature_names: list[str]) -> dict[str, float]:
    """Extract gain-based feature importances from the XGBoost model."""
    try:
        raw = model.get_booster().get_score(importance_type="gain")
        # Map f0, f1, ... to actual feature names
        return {
            feature_names[int(k[1:])]: v
            for k, v in raw.items()
            if k[0] == "f" and int(k[1:]) < len(feature_names)
        }
    except Exception:
        # Fallback: use the sklearn-style feature_importances_ array
        if hasattr(model, "feature_importances_"):
            return dict(zip(feature_names, model.feature_importances_))
        return {}


def _direction(name: str, value: float) -> str:
    """Infer which side a feature favours."""
    if name.startswith("radiant_"):
        return "radiant" if value > 0 else "dire"
    if name.startswith("dire_"):
        return "dire" if value > 0 else "radiant"
    if name.startswith("diff_"):
        return "radiant" if value > 0 else "dire"
    if "radiant" in name:
        return "radiant" if value > 0 else "dire"
    if "dire" in name:
        return "dire" if value > 0 else "radiant"
    return "neutral"


def _simplify_name(name: str) -> str:
    """Strip prefixes to get a human-readable factor name."""
    for prefix in ("radiant_", "dire_", "diff_", "radiant_avg_", "dire_avg_", "diff_avg_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def predict(
    bundle: dict,
    features: pd.DataFrame,
) -> dict:
    """Run prediction and return win probability, confidence, and top factors.

    Args:
        bundle: Model bundle from :func:`load_model`.
        features: Single-row DataFrame from :func:`feature_builder.build_features`.

    Returns:
        Dict with keys: radiant_win_prob, confidence, top_factors.
    """
    model = bundle["model"]
    feature_names = bundle["feature_names"]

    # Ensure column order
    X = features[feature_names]

    # Apply imputer if available
    imputer = bundle.get("imputer")
    if imputer is not None:
        X = pd.DataFrame(imputer.transform(X), columns=feature_names, index=X.index)

    prob = float(model.predict_proba(X)[0, 1])

    return {
        "radiant_win_prob": round(prob, 4),
        "confidence": _confidence_label(prob),
        "top_factors": _top_factors(model, X, feature_names),
    }
