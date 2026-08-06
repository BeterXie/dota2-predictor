"""Current strict-event coverage reporting from authoritative database rows."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from database.session import PostgresSession

from .ingest import MATCH_PROCESSOR_VERSION
from .incremental import ROLE_VERSION, SCORE_VERSION
from .storage import ALEMBIC_HEAD, IntelligenceStorage
from .team_profiles import PROFILE_VERSION
from .team_states import LABEL_VERSION


UTC = timezone.utc


def _role_readiness_by_match(
    connection: PostgresSession,
) -> dict[int, dict[str, Any]]:
    readiness: dict[int, dict[str, Any]] = {}
    for row in connection.execute(
        """SELECT eligible.event_id, eligible.match_id,
                  COUNT(DISTINCT CASE
                    WHEN roles.purpose='expected_position'
                    THEN roles.player_slot END) AS expected_role_rows,
                  COUNT(DISTINCT CASE
                    WHEN roles.purpose='observed_position'
                    THEN roles.player_slot END) AS observed_role_rows,
                  COUNT(DISTINCT CASE
                    WHEN roles.purpose='expected_position'
                     AND roles.position IS NOT NULL
                    THEN roles.player_slot END) AS expected_position_rows,
                  COUNT(DISTINCT CASE
                    WHEN roles.purpose='observed_position'
                     AND roles.position IS NOT NULL
                    THEN roles.player_slot END) AS observed_position_rows
             FROM formal_map_eligibility AS eligible
             LEFT JOIN player_role_assignments AS roles
               ON roles.match_id=eligible.match_id
              AND roles.assignment_version=?
            GROUP BY eligible.event_id, eligible.match_id""",
        (ROLE_VERSION,),
    ):
        expected_rows = int(row["expected_role_rows"])
        observed_rows = int(row["observed_role_rows"])
        expected_positions = int(row["expected_position_rows"])
        observed_positions = int(row["observed_position_rows"])
        readiness[int(row["match_id"])] = {
            "event_id": str(row["event_id"]),
            "expected_role_rows": expected_rows,
            "observed_role_rows": observed_rows,
            "expected_position_rows": expected_positions,
            "observed_position_rows": observed_positions,
            "expected_role_ready": expected_rows == 10,
            "observed_role_ready": observed_rows == 10,
            "complete_positions": (
                expected_positions == 10 and observed_positions == 10
            ),
        }
    return readiness


def build_coverage_report(
    connection: PostgresSession,
    *,
    database: str,
    generated_at: datetime,
    include_integrity: bool = False,
) -> dict[str, Any]:
    generated_at = generated_at.astimezone(UTC)
    role_readiness = _role_readiness_by_match(connection)
    event_role_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "expected_role_ready_maps": 0,
            "observed_role_ready_maps": 0,
            "complete_position_maps": 0,
        }
    )
    for value in role_readiness.values():
        totals = event_role_totals[value["event_id"]]
        totals["expected_role_ready_maps"] += int(value["expected_role_ready"])
        totals["observed_role_ready_maps"] += int(value["observed_role_ready"])
        totals["complete_position_maps"] += int(value["complete_positions"])

    events = []
    for row in connection.execute(
        """SELECT registry.event_id, registry.canonical_name,
                  registry.prize_pool_usd, registry.opendota_league_id,
                  registry.expected_map_count, registry.observed_map_count,
                  registry.public_map_count, registry.reconciliation_status,
                  COUNT(eligible.match_id) AS formal_maps,
                  SUM(CASE WHEN eligible.player_readiness='ready' THEN 1 ELSE 0 END)
                    AS player_ready,
                  SUM(CASE WHEN eligible.state_readiness='ready' THEN 1 ELSE 0 END)
                    AS state_ready,
                  SUM(CASE WHEN eligible.draft_readiness='ready' THEN 1 ELSE 0 END)
                    AS draft_ready
             FROM event_registry AS registry
             LEFT JOIN formal_map_eligibility AS eligible
               ON eligible.event_id=registry.event_id
            GROUP BY registry.event_id
            ORDER BY registry.main_event_start_at"""
    ):
        value = dict(row)
        event_id = str(row["event_id"])
        value.update(event_role_totals[event_id])
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

    issue_rows = [
        dict(row)
        for row in connection.execute(
            """SELECT event_id, match_id, ingest_state, missing_fields_json,
                      player_readiness, state_readiness, draft_readiness,
                      reconciliation_status, last_error, next_retry_at
                 FROM match_ingest_status
                WHERE ingest_state IN ('retryable', 'failed', 'review_required')
                   OR player_readiness<>'ready'
                   OR state_readiness<>'ready'
                   OR draft_readiness<>'ready'"""
        )
    ]
    issues_by_match = {int(row["match_id"]): row for row in issue_rows}
    for status in connection.execute(
        """SELECT status.event_id, status.match_id, status.ingest_state,
                  status.missing_fields_json, status.player_readiness,
                  status.state_readiness, status.draft_readiness,
                  status.reconciliation_status, status.last_error,
                  status.next_retry_at
             FROM match_ingest_status AS status
             JOIN formal_map_eligibility AS eligible
               ON eligible.match_id=status.match_id"""
    ):
        match_id = int(status["match_id"])
        readiness = role_readiness[match_id]
        if (
            readiness["expected_role_ready"]
            and readiness["observed_role_ready"]
            and readiness["complete_positions"]
        ):
            continue
        issue = issues_by_match.get(match_id)
        if issue is None:
            issue = dict(status)
            issue_rows.append(issue)
            issues_by_match[match_id] = issue
        issue.update(
            {
                "expected_role_readiness": (
                    "ready" if readiness["expected_role_ready"] else "missing"
                ),
                "observed_role_readiness": (
                    "ready" if readiness["observed_role_ready"] else "missing"
                ),
                "complete_position_readiness": (
                    "ready" if readiness["complete_positions"] else "missing"
                ),
                "missing_expected_role_rows": max(
                    0, 10 - readiness["expected_role_rows"]
                ),
                "missing_observed_role_rows": max(
                    0, 10 - readiness["observed_role_rows"]
                ),
                "missing_position_values": max(
                    0,
                    20
                    - readiness["expected_position_rows"]
                    - readiness["observed_position_rows"],
                ),
            }
        )
    issue_rows.sort(key=lambda row: (str(row["event_id"]), int(row["match_id"])))

    report: dict[str, Any] = {
        "database": database,
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
        "expected_role_ready_maps": sum(
            value["expected_role_ready"] for value in role_readiness.values()
        ),
        "observed_role_ready_maps": sum(
            value["observed_role_ready"] for value in role_readiness.values()
        ),
        "complete_position_maps": sum(
            value["complete_positions"] for value in role_readiness.values()
        ),
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
                  AND explanation_json::jsonb ->> 'ranking_eligible'='true'""",
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
        "issues": issue_rows,
    }
    if include_integrity:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        invalid_constraints = int(
            connection.execute(
                "SELECT COUNT(*) FROM pg_constraint WHERE NOT convalidated"
            ).fetchone()[0]
        )
        report["integrity_check"] = (
            "ok"
            if revision is not None
            and str(revision[0]) == ALEMBIC_HEAD
            and invalid_constraints == 0
            else "failed"
        )
        report["schema_revision"] = None if revision is None else str(revision[0])
        report["invalid_constraints"] = invalid_constraints
    return report


def write_coverage_report(
    database_url: str | None,
    output: Path,
    *,
    generated_at: datetime | None = None,
    include_integrity: bool = False,
) -> dict[str, Any]:
    storage = IntelligenceStorage(database_url)
    try:
        storage.init_schema(seed_events=False)
        report = build_coverage_report(
            storage.connection,
            database=str(storage.engine.url.database),
            generated_at=generated_at or datetime.now(UTC),
            include_integrity=include_integrity,
        )
    finally:
        storage.close()
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
