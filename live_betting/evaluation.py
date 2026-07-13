"""Calibration and shadow-order evaluation metrics."""

from __future__ import annotations

import math
from collections.abc import Iterable


def brier_score(rows: Iterable[tuple[float, int]]) -> float:
    values = [(probability - outcome) ** 2 for probability, outcome in rows]
    if not values:
        raise ValueError("at least one settled prediction is required")
    return sum(values) / len(values)


def log_loss(rows: Iterable[tuple[float, int]], epsilon: float = 1e-15) -> float:
    values = []
    for probability, outcome in rows:
        probability = min(1 - epsilon, max(epsilon, probability))
        values.append(-(outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability)))
    if not values:
        raise ValueError("at least one settled prediction is required")
    return sum(values) / len(values)


def shadow_summary(orders: Iterable[dict]) -> dict[str, float | int]:
    rows = list(orders)
    filled = [row for row in rows if row.get("status") == "filled"]
    settled = [row for row in filled if row.get("return_units") is not None]
    stake = sum(float(row.get("stake") or 0) for row in settled)
    returned = sum(float(row.get("return_units") or 0) * float(row.get("stake") or 0)
                   for row in settled)
    pnl = returned - stake
    return {
        "signals": len(rows),
        "filled": len(filled),
        "settled": len(settled),
        "fill_rate": len(filled) / len(rows) if rows else 0.0,
        "stake_units": stake,
        "return_units": returned,
        "pnl_units": pnl,
        "roi": pnl / stake if stake else 0.0,
    }
