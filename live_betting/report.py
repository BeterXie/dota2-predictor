"""Generate a compact JSON evaluation report for comeback shadow decisions."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from .evaluation import brier_score, log_loss, shadow_summary


def build_report(connection: sqlite3.Connection) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    decisions = connection.execute("SELECT * FROM strategy_decisions").fetchall()
    reasons = Counter(str(row["reason"]) for row in decisions)
    orders = connection.execute(
        """SELECT o.*, s.result, s.return_units
           FROM shadow_orders o LEFT JOIN settlements s ON s.order_key=o.order_key"""
    ).fetchall()
    summary_rows = [dict(row) for row in orders]
    probability_rows = []
    for row in orders:
        if row["result"] is None:
            continue
        won = 1 if str(row["result"]) in {"win", "half_win"} else 0
        probability_rows.append((float(row["model_probability"]), won))
    settled = len(probability_rows)
    return {
        "decision_count": len(decisions),
        "eligible_decisions": sum(int(row["eligible"]) for row in decisions),
        "decision_reasons": dict(sorted(reasons.items())),
        "orders": shadow_summary(summary_rows),
        "settled_orders": settled,
        "brier_score": brier_score(probability_rows) if probability_rows else None,
        "log_loss": log_loss(probability_rows) if probability_rows else None,
        "stability_status": (
            "descriptive_only" if settled < 100 else
            "provisional" if settled < 500 else "stability_sample_reached"
        ),
        "minimum_stability_sample": 500,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    connection = sqlite3.connect(args.database)
    try:
        report = build_report(connection)
    finally:
        connection.close()
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
