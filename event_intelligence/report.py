"""Strict-scope data coverage report helpers."""

from __future__ import annotations

import sqlite3
from typing import Any

from .player_scoring import SCORE_VERSION


def build_intelligence_report(connection: sqlite3.Connection) -> dict[str, Any]:
    """Summarize strict coverage without combining reconstructed/prospective rows."""
    score_versions = _group(connection, "player_map_scores", "score_version")
    draft_modes = _draft_modes(connection)
    draft_score_versions = _draft_score_versions(connection)
    return {
        "formal_maps": _count(connection, "formal_map_eligibility"),
        "player_facts": _count(connection, "player_map_facts"),
        "player_scores": sum(
            count
            for version, count in score_versions.items()
            if version == SCORE_VERSION or version.startswith(f"{SCORE_VERSION}+")
        ),
        "player_score_rows": sum(score_versions.values()),
        "player_scores_by_version": score_versions,
        "team_profiles": _count(connection, "team_style_profiles"),
        "draft_predictions": sum(draft_modes.values()),
        "draft_prediction_rows": sum(draft_score_versions.values()),
        "draft_predictions_by_score_version": draft_score_versions,
        "draft_predictions_by_mode": draft_modes,
        "draft_predictions_by_status": _draft_statuses(connection),
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
                WHERE json_extract(run.configuration_json, '$.score_version')
                      LIKE ?
                GROUP BY run.availability_mode""",
            (f"{SCORE_VERSION}+%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


def _draft_statuses(connection: sqlite3.Connection) -> dict[str, int]:
    try:
        rows = connection.execute(
            """SELECT prediction.status, COUNT(*)
                 FROM draft_predictions AS prediction
                 JOIN draft_model_runs AS run ON run.run_id=prediction.run_id
                WHERE json_extract(run.configuration_json, '$.score_version')
                      LIKE ?
                GROUP BY prediction.status""",
            (f"{SCORE_VERSION}+%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


def _draft_score_versions(connection: sqlite3.Connection) -> dict[str, int]:
    try:
        rows = connection.execute(
            """SELECT json_extract(run.configuration_json, '$.score_version'),
                      COUNT(*)
                 FROM draft_predictions AS prediction
                 JOIN draft_model_runs AS run ON run.run_id=prediction.run_id
                GROUP BY json_extract(
                    run.configuration_json, '$.score_version'
                )"""
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


__all__ = ["build_intelligence_report"]
