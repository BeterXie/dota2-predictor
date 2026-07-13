"""Strict-scope data coverage report helpers."""

from __future__ import annotations

import sqlite3
from typing import Any


def build_intelligence_report(connection: sqlite3.Connection) -> dict[str, Any]:
    """Summarize strict coverage without combining reconstructed/prospective rows."""
    return {
        "formal_maps": _count(connection, "formal_map_eligibility"),
        "player_facts": _count(connection, "player_map_facts"),
        "player_scores": _count(connection, "player_map_scores"),
        "team_profiles": _count(connection, "team_style_profiles"),
        "draft_predictions_by_mode": _draft_modes(connection),
        "draft_predictions_by_status": _group(
            connection, "draft_predictions", "status"
        ),
        "role_assignments_by_purpose": _group(
            connection, "player_role_assignments", "purpose"
        ),
        "strict_event_count": _count(connection, "event_registry"),
    }


def _count(connection: sqlite3.Connection, table: str) -> int:
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _group(connection: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    try:
        rows = connection.execute(
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


def _draft_modes(connection: sqlite3.Connection) -> dict[str, int]:
    try:
        rows = connection.execute(
            """SELECT run.availability_mode, COUNT(*)
                 FROM draft_predictions AS prediction
                 JOIN draft_model_runs AS run ON run.run_id=prediction.run_id
                GROUP BY run.availability_mode"""
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


__all__ = ["build_intelligence_report"]
