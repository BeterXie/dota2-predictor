"""Generate a compact JSON evaluation report for comeback shadow decisions."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from .evaluation import brier_score, log_loss, shadow_summary
from .health import read_health


def build_report(connection: sqlite3.Connection) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    decisions = connection.execute("SELECT * FROM strategy_decisions").fetchall()
    reasons = Counter(str(row["reason"]) for row in decisions)
    orders = connection.execute(
        """SELECT o.*, s.result, s.return_units, s.settled_at
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
    drawdown = _drawdown(orders)
    outbox = _group_counts(connection, "notification_outbox", "status")
    health = read_health(connection)
    strategy_versions = _group_counts(connection, "strategy_decisions", "strategy_version")
    try:
        strict_counts = {
            "accepted_mappings": int(connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mappings"
            ).fetchone()[0]),
            "mapping_audits": int(connection.execute(
                "SELECT COUNT(*) FROM strict_live_map_mapping_audit"
            ).fetchone()[0]),
        }
    except sqlite3.OperationalError:
        strict_counts = {"accepted_mappings": 0, "mapping_audits": 0}
    return {
        "decision_count": len(decisions),
        "eligible_decisions": sum(int(row["eligible"]) for row in decisions),
        "decision_reasons": dict(sorted(reasons.items())),
        "orders": shadow_summary(summary_rows),
        "settled_orders": settled,
        "brier_score": brier_score(probability_rows) if probability_rows else None,
        "log_loss": log_loss(probability_rows) if probability_rows else None,
        "maximum_drawdown_units": drawdown,
        "notification_outbox": outbox,
        "service_health": health,
        "strategy_versions": strategy_versions,
        "strict_scope": strict_counts,
        "stability_status": (
            "descriptive_only" if settled < 100 else
            "provisional" if settled < 500 else "stability_sample_reached"
        ),
        "minimum_stability_sample": 500,
    }


def _group_counts(
    connection: sqlite3.Connection, table: str, column: str
) -> dict[str, int]:
    try:
        rows = connection.execute(
            f"SELECT {column}, COUNT(*) AS count FROM {table} GROUP BY {column}"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


def _drawdown(rows: list[sqlite3.Row]) -> float:
    try:
        settled = sorted(
            (row for row in rows if row["return_units"] is not None),
            key=lambda row: str(row["settled_at"] or ""),
        )
    except (IndexError, KeyError):
        return 0.0
    bankroll = peak = worst = 0.0
    for row in settled:
        stake = float(row["stake"] or 0.0)
        bankroll += float(row["return_units"] or 0.0) * stake - stake
        peak = max(peak, bankroll)
        worst = max(worst, peak - bankroll)
    return worst


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
