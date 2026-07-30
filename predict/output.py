"""Format prediction results as JSON and persist to disk."""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from shared.queries import connect


def _lookup_team_name(database_url: str | None, team_id: int) -> str | None:
    conn = connect(database_url)
    try:
        row = conn.execute(
            "SELECT name FROM teams WHERE team_id = ?", (team_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _lookup_league_name(database_url: str | None, league_id: int) -> str | None:
    if not league_id:
        return None
    conn = connect(database_url)
    try:
        row = conn.execute(
            "SELECT name FROM leagues WHERE leagueid = ?", (league_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _next_prediction_id(output_dir: str, today_str: str) -> str:
    """Generate a sequential prediction id for today: ``YYYYMMDD_N``."""
    out_path = Path(output_dir)
    if not out_path.exists():
        return f"{today_str}_1"
    existing = [f.stem for f in out_path.glob(f"{today_str}_*.json")]
    if not existing:
        return f"{today_str}_1"
    nums = []
    for e in existing:
        try:
            nums.append(int(e.split("_")[-1]))
        except ValueError:
            continue
    return f"{today_str}_{max(nums) + 1}"


def format_output(
    prediction: dict,
    radiant_id: int,
    dire_id: int,
    league_id: int,
    bundle: dict,
    database_url: str | None,
) -> dict:
    """Assemble the final prediction output dict per DESIGN.md Module D."""
    now = datetime.now(timezone.utc)

    metrics = bundle.get("metrics", {})
    test_metrics = metrics.get("test", {}) if isinstance(metrics, dict) else {}

    return {
        "prediction_id": "",  # filled after we know the output dir
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "match": {
            "radiant_team": {
                "id": radiant_id,
                "name": _lookup_team_name(database_url, radiant_id),
            },
            "dire_team": {
                "id": dire_id,
                "name": _lookup_team_name(database_url, dire_id),
            },
            "league": {
                "id": league_id,
                "name": _lookup_league_name(database_url, league_id),
            },
            "best_of": 3,
        },
        "prediction": prediction,
        "model": {
            "version": bundle.get("timestamp", "unknown"),
            "auc": test_metrics.get("roc_auc"),
            "accuracy": test_metrics.get("accuracy"),
        },
    }


def _sanitize(obj):
    """Recursively replace NaN/Infinity with None and convert numpy types to native Python."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return float(obj)
    if hasattr(obj, 'item'):  # numpy scalar → native Python
        return _sanitize(obj.item())
    return obj


def save_prediction(output: dict, predictions_dir: str) -> str:
    """Write prediction JSON to ``data/predictions/`` and return the file path."""
    out_path = Path(predictions_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y%m%d")
    pred_id = _next_prediction_id(str(out_path), today_str)
    output["prediction_id"] = pred_id

    clean = _sanitize(output)
    file_path = out_path / f"{pred_id}.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)

    return str(file_path)
