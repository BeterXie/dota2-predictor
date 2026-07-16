"""Current strict-event coverage reporting from authoritative database rows."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ingest import MATCH_PROCESSOR_VERSION
from .incremental import ROLE_VERSION, SCORE_VERSION
from .team_profiles import PROFILE_VERSION
from .team_states import LABEL_VERSION
from shared.sqlite import connect


UTC = timezone.utc


def build_coverage_report(
    connection: sqlite3.Connection,
    *,
    database: Path,
    generated_at: datetime,
    include_integrity: bool = False,
) -> dict[str, Any]:
    generated_at = generated_at.astimezone(UTC)
    events = []
    for row in connection.execute(
        """SELECT registry.event_id, registry.canonical_name,
                  registry.prize_pool_usd, registry.opendota_league_id,
                  registry.expected_map_count, registry.observed_map_count,
                  registry.public_map_count, registry.reconciliation_status,
                  COUNT(eligible.match_id) AS formal_maps,
                  SUM(eligible.player_readiness='ready') AS player_ready,
                  SUM(eligible.state_readiness='ready') AS state_ready,
                  SUM(eligible.draft_readiness='ready') AS draft_ready
             FROM event_registry AS registry
             LEFT JOIN formal_map_eligibility AS eligible
               ON eligible.event_id=registry.event_id
            GROUP BY registry.event_id
            ORDER BY registry.main_event_start_at"""
    ):
        value = dict(row)
        event_id = str(row["event_id"])
        value["player_facts"] = connection.execute(
            """SELECT COUNT(*) FROM player_map_facts AS facts
                JOIN match_ingest_status AS status USING(match_id)
                WHERE status.event_id=?
                  AND facts.fact_version=? || ':' ||
                                         status.latest_raw_content_hash""",
            (event_id, MATCH_PROCESSOR_VERSION),
        ).fetchone()[0]
        value["player_scores"] = connection.execute(
            """SELECT COUNT(*) FROM player_map_scores AS scores
                JOIN match_ingest_status AS status USING(match_id)
                WHERE status.event_id=? AND scores.score_version=?""",
            (event_id, SCORE_VERSION),
        ).fetchone()[0]
        value["team_states"] = connection.execute(
            """SELECT COUNT(*) FROM team_map_states AS states
                JOIN match_ingest_status AS status USING(match_id)
                WHERE status.event_id=? AND states.label_version=?""",
            (event_id, LABEL_VERSION),
        ).fetchone()[0]
        events.append(value)

    report: dict[str, Any] = {
        "database": str(database.resolve()),
        "generated_at": generated_at.isoformat(),
        "versions": {
            "role_assignment": ROLE_VERSION,
            "match_processor": MATCH_PROCESSOR_VERSION,
            "player_score": SCORE_VERSION,
            "team_state": LABEL_VERSION,
            "team_profile": PROFILE_VERSION,
        },
        "events": events,
        "formal_maps": connection.execute(
            "SELECT COUNT(*) FROM formal_map_eligibility"
        ).fetchone()[0],
        "current_player_facts": connection.execute(
            """SELECT COUNT(*) FROM player_map_facts AS facts
                JOIN match_ingest_status AS status USING(match_id)
                WHERE facts.fact_version=? || ':' ||
                                         status.latest_raw_content_hash""",
            (MATCH_PROCESSOR_VERSION,),
        ).fetchone()[0],
        "current_player_scores": connection.execute(
            "SELECT COUNT(*) FROM player_map_scores WHERE score_version=?",
            (SCORE_VERSION,),
        ).fetchone()[0],
        "ranking_eligible_scores": connection.execute(
            """SELECT COUNT(*) FROM player_map_scores
                WHERE score_version=?
                  AND json_extract(explanation_json, '$.ranking_eligible')=1""",
            (SCORE_VERSION,),
        ).fetchone()[0],
        "current_team_states": connection.execute(
            "SELECT COUNT(*) FROM team_map_states WHERE label_version=?",
            (LABEL_VERSION,),
        ).fetchone()[0],
        "current_team_profiles": connection.execute(
            """SELECT COUNT(*) FROM team_style_profiles AS profile
                WHERE profile.profile_version=?
                  AND profile.profile_cutoff=(
                      SELECT MAX(current.profile_cutoff)
                        FROM team_style_profiles AS current
                       WHERE current.team_id=profile.team_id
                         AND current.profile_version=profile.profile_version
                  )""",
            (PROFILE_VERSION,),
        ).fetchone()[0],
        "derived_status_maps": connection.execute(
            "SELECT COUNT(*) FROM strict_derived_status"
        ).fetchone()[0],
        "raw_artifacts": connection.execute(
            "SELECT COUNT(*) FROM raw_source_artifacts"
        ).fetchone()[0],
        "raw_observations": connection.execute(
            "SELECT COUNT(*) FROM raw_source_observations"
        ).fetchone()[0],
        "event_candidates": connection.execute(
            "SELECT COUNT(*) FROM event_candidates"
        ).fetchone()[0],
        "pending_event_candidates": connection.execute(
            "SELECT COUNT(*) FROM event_candidates WHERE audit_status='pending'"
        ).fetchone()[0],
        "scheduler_checkpoints": {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT checkpoint_key, checkpoint_at "
                "FROM ingest_scheduler_checkpoints ORDER BY checkpoint_key"
            )
        },
        "issues": [
            dict(row)
            for row in connection.execute(
                """SELECT event_id, match_id, ingest_state, missing_fields_json,
                          player_readiness, state_readiness, draft_readiness,
                          reconciliation_status, last_error, next_retry_at
                     FROM match_ingest_status
                    WHERE ingest_state IN ('retryable', 'failed', 'review_required')
                       OR player_readiness<>'ready'
                       OR state_readiness<>'ready'
                       OR draft_readiness<>'ready'
                    ORDER BY event_id, match_id"""
            )
        ],
    }
    if include_integrity:
        report["integrity_check"] = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        report["foreign_key_errors"] = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
    return report


def write_coverage_report(
    database: Path,
    output: Path,
    *,
    generated_at: datetime | None = None,
    include_integrity: bool = False,
) -> dict[str, Any]:
    database = database.resolve()
    connection = connect(database, read_only=True, row_factory=sqlite3.Row)
    try:
        report = build_coverage_report(
            connection,
            database=database,
            generated_at=generated_at or datetime.now(UTC),
            include_integrity=include_integrity,
        )
    finally:
        connection.close()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return report


__all__ = ["build_coverage_report", "write_coverage_report"]
