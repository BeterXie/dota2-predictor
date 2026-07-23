"""Read-only delivery queries for strict historical intelligence."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from event_intelligence.backtest import (
    BACKTEST_VERSION,
    DRAFT_VALIDATION_VERSION,
    EvaluationPoint,
    draft_lineage_tracking_is_current,
    draft_prediction_artifacts,
    evaluate_points,
)
from event_intelligence.benchmarks import BENCHMARK_VERSION
from event_intelligence.draft_features import FEATURE_VERSION as DRAFT_FEATURE_VERSION
from event_intelligence.draft_model import MODEL_VERSION as DRAFT_MODEL_VERSION
from event_intelligence.incremental import (
    CurrentDerivedScopes,
    ROLE_VERSION,
    SCORE_VERSION,
    StrictDerivedPipeline,
    _draft_prediction_artifacts_for_read,
    current_derived_scopes,
    current_state_input_hashes,
    profile_weighting_is_current,
)
from event_intelligence.player_scoring import score_version_for_role
from event_intelligence.roles import PROSPECTIVE_ASSIGNMENT_VERSION
from event_intelligence.storage import query_historical_rosh_lineup_score
from event_intelligence.team_profiles import PROFILE_VERSION
from event_intelligence.team_states import LABEL_VERSION
from live_betting.postmatch_monitor import has_trusted_confirmed_draft
from live_betting.stratz_rosh_client import ROSH_FORMULA_VERSION
from live_betting.strict_eligibility import (
    StrictLiveMapMapping,
    query_strict_live_eligibility,
    query_strict_mapping_snapshot,
)

from . import queries
from .monitoring import is_head_to_head_match_row, winner_timeline


MODEL_KINDS = ("pure_draft", "context_adjusted")
LANDMARK_MINUTES = (10, 20, 30, 40, 50)
DEFAULT_ECE_BINS = 5
MATCH_RATING_VERSION = "match-rating-v1-current-player-score-mean"
MATCH_RATING_ROUNDING = "decimal-half-up-2dp"
_MATCH_RATING_QUANTUM = Decimal("0.01")
STATE_JSON_COLUMNS = (
    "crossings_json",
    "objective_conversion_json",
    "source_versions_json",
)
PLAYER_PERFORMANCE_FIELDS = (
    "kills",
    "deaths",
    "assists",
    "gold_per_min",
    "xp_per_min",
    "net_worth",
    "last_hits",
    "denies",
    "hero_damage",
    "hero_healing",
    "tower_damage",
    "level",
    "lane_efficiency",
    "kda",
)
DRAFT_VERSION_PREDICATE = """run.model_version=?
AND json_valid(run.configuration_json)
AND CASE WHEN json_valid(run.configuration_json)
         THEN json_extract(run.configuration_json, '$.backtest_version') END=?
AND CASE WHEN json_valid(run.configuration_json)
         THEN json_extract(run.configuration_json, '$.feature_version') END=?
AND (
    (run.availability_mode=?
     AND CASE WHEN json_valid(run.configuration_json)
              THEN json_extract(run.configuration_json, '$.assignment_version') END=?
     AND CASE WHEN json_valid(run.configuration_json)
              THEN json_extract(run.configuration_json, '$.score_version') END=?)
    OR
    (run.availability_mode=?
     AND CASE WHEN json_valid(run.configuration_json)
              THEN json_extract(run.configuration_json, '$.assignment_version') END=?
     AND CASE WHEN json_valid(run.configuration_json)
              THEN json_extract(run.configuration_json, '$.score_version') END=?)
)"""
PROSPECTIVE_ROLE_VERSION = PROSPECTIVE_ASSIGNMENT_VERSION
DRAFT_COHORTS = (
    ("reconstructed_walk_forward", ROLE_VERSION, SCORE_VERSION),
    (
        "prospective",
        PROSPECTIVE_ROLE_VERSION,
        score_version_for_role(PROSPECTIVE_ROLE_VERSION),
    ),
)
DRAFT_VERSION_PARAMS = (
    DRAFT_MODEL_VERSION,
    BACKTEST_VERSION,
    DRAFT_FEATURE_VERSION,
    *(value for cohort in DRAFT_COHORTS for value in cohort),
)
_DRAFT_QUALITY_LOCK = threading.Lock()
_DRAFT_QUALITY_CACHE: tuple[str, tuple[dict[str, Any], ...]] | None = None


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    database = Path(queries.DB_PATH).resolve()
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _relation_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        """SELECT 1 FROM sqlite_master
             WHERE type IN ('table', 'view') AND name=?""",
        (name,),
    ).fetchone() is not None


def _relation_columns(connection: sqlite3.Connection, name: str) -> set[str]:
    if not _relation_exists(connection, name):
        return set()
    try:
        return {
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{name}")').fetchall()
        }
    except sqlite3.OperationalError:
        return set()


def _current_scopes(connection: sqlite3.Connection) -> CurrentDerivedScopes:
    return current_derived_scopes(connection)


def _targeted_scopes(
    connection: sqlite3.Connection,
    match_id: int,
) -> CurrentDerivedScopes:
    """Validate current derived lineage for one postmatch identity."""
    formal_columns = _relation_columns(connection, "formal_map_eligibility")
    if "match_id" not in formal_columns:
        return CurrentDerivedScopes(available=False)
    try:
        formal_row = connection.execute(
            "SELECT match_id FROM formal_map_eligibility WHERE match_id=?",
            (match_id,),
        ).fetchone()
    except sqlite3.Error:
        return CurrentDerivedScopes(available=False)
    formal = frozenset({match_id}) if formal_row is not None else frozenset()
    if formal_row is None:
        return CurrentDerivedScopes(available=True, formal=formal)

    required = {
        "formal_map_eligibility": {
            "match_id",
            "event_id",
            "player_readiness",
            "state_readiness",
            "draft_readiness",
        },
        "match_ingest_status": {
            "match_id",
            "event_id",
            "latest_raw_content_hash",
            "normalizer_version",
        },
        "strict_derived_status": {
            "match_id",
            "source_content_hash",
            "role_assignment_version",
            "score_version",
            "team_state_version",
            "profile_version",
            "profile_cutoff",
            "normalizer_version",
            "benchmark_version",
            "profile_context_hash",
        },
    }
    if any(
        not columns.issubset(_relation_columns(connection, relation))
        for relation, columns in required.items()
    ):
        return CurrentDerivedScopes(available=True, formal=formal)

    try:
        row = connection.execute(
            """SELECT derived.match_id, derived.source_content_hash,
                      derived.role_assignment_version, derived.score_version,
                      derived.team_state_version, derived.profile_version,
                      derived.profile_cutoff,
                      derived.normalizer_version AS derived_normalizer_version,
                      derived.benchmark_version, derived.profile_context_hash,
                      status.event_id, status.latest_raw_content_hash,
                      status.normalizer_version,
                      eligible.event_id AS eligible_event_id,
                      eligible.player_readiness, eligible.state_readiness,
                      eligible.draft_readiness
                 FROM strict_derived_status AS derived
                 JOIN match_ingest_status AS status USING(match_id)
                 LEFT JOIN formal_map_eligibility AS eligible USING(match_id)
                WHERE derived.match_id=?""",
            (match_id,),
        ).fetchone()
        if row is None:
            return CurrentDerivedScopes(available=True, formal=formal)
        event_id = str(row["event_id"])
        contexts = StrictDerivedPipeline._profile_context_hashes(
            connection, {event_id}
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return CurrentDerivedScopes(available=True, formal=formal)

    lineage_current = (
        row["eligible_event_id"] is not None
        and str(row["eligible_event_id"]) == event_id
        and row["latest_raw_content_hash"] is not None
        and row["source_content_hash"] == row["latest_raw_content_hash"]
        and row["role_assignment_version"] == ROLE_VERSION
        and row["score_version"] == SCORE_VERSION
        and row["team_state_version"] == LABEL_VERSION
        and row["profile_version"] == PROFILE_VERSION
        and row["derived_normalizer_version"] == row["normalizer_version"]
        and row["benchmark_version"] == BENCHMARK_VERSION
        and row["profile_context_hash"] == contexts.get(event_id)
    )
    if not lineage_current:
        return CurrentDerivedScopes(available=True, formal=formal)

    player_complete = False
    if {
        "match_id",
        "player_slot",
        "score_version",
    }.issubset(_relation_columns(connection, "player_map_scores")) and {
        "match_id",
        "player_slot",
        "purpose",
        "assignment_version",
        "position",
    }.issubset(_relation_columns(connection, "player_role_assignments")):
        try:
            scored = connection.execute(
                """SELECT COUNT(DISTINCT player_slot)
                     FROM player_map_scores
                    WHERE match_id=? AND score_version=?""",
                (match_id, SCORE_VERSION),
            ).fetchone()
            assigned = connection.execute(
                """SELECT COUNT(DISTINCT player_slot)
                     FROM player_role_assignments
                    WHERE match_id=? AND purpose='observed_position'
                      AND assignment_version=? AND position BETWEEN 1 AND 5""",
                (match_id, ROLE_VERSION),
            ).fetchone()
            player_complete = (
                scored is not None
                and assigned is not None
                and int(scored[0]) == 10
                and int(assigned[0]) == 10
            )
        except (TypeError, ValueError, sqlite3.Error):
            player_complete = False

    state_complete = False
    if {
        "match_id",
        "side",
        "label_version",
    }.issubset(_relation_columns(connection, "team_map_states")):
        try:
            state_count = connection.execute(
                """SELECT COUNT(DISTINCT side) FROM team_map_states
                    WHERE match_id=? AND label_version=?""",
                (match_id, LABEL_VERSION),
            ).fetchone()
            state_complete = state_count is not None and int(state_count[0]) == 2
        except (TypeError, ValueError, sqlite3.Error):
            state_complete = False

    state_eligible = row["state_readiness"] in {"ready", "unscorable"}
    player = (
        frozenset({match_id})
        if row["player_readiness"] == "ready" and player_complete
        else frozenset()
    )
    state = (
        frozenset({match_id})
        if state_eligible and state_complete
        else frozenset()
    )
    draft_predictions = (
        _targeted_draft_prediction_keys(connection, match_id)
        if row["draft_readiness"] == "ready"
        else frozenset()
    )
    draft = (
        frozenset({match_id})
        if any(candidate_match_id == match_id for _, candidate_match_id in draft_predictions)
        else frozenset()
    )
    cutoff = str(row["profile_cutoff"])
    return CurrentDerivedScopes(
        available=True,
        formal=formal,
        current=frozenset({match_id}),
        player=player,
        state=state,
        draft=draft,
        draft_predictions=draft_predictions,
        valid_profile_cutoffs=(
            frozenset({cutoff}) if state_eligible and state_complete else frozenset()
        ),
    )


def _targeted_draft_prediction_keys(
    connection: sqlite3.Connection,
    match_id: int,
) -> frozenset[tuple[str, int]]:
    required = {
        "draft_prediction_validations": {
            "run_id",
            "match_id",
            "input_snapshot_hash",
            "artifact_fingerprint",
            "dependency_fingerprint",
            "dependency_revision",
            "validation_version",
        },
        "draft_predictions": {
            "run_id",
            "match_id",
            "prediction_cutoff",
            "input_snapshot_hash",
        },
    }
    if any(
        not columns.issubset(_relation_columns(connection, relation))
        for relation, columns in required.items()
    ):
        return frozenset()
    try:
        if not draft_lineage_tracking_is_current(connection):
            return frozenset()
        artifacts = _draft_prediction_artifacts_for_read(
            connection, draft_prediction_artifacts
        )
        rows = connection.execute(
            """SELECT validation.run_id, validation.match_id,
                      validation.input_snapshot_hash,
                      validation.artifact_fingerprint
                 FROM draft_prediction_validations AS validation
                 JOIN draft_predictions AS prediction
                   ON prediction.run_id=validation.run_id
                  AND prediction.match_id=validation.match_id
                  AND prediction.input_snapshot_hash=
                      validation.input_snapshot_hash
                 JOIN draft_lineage_revisions AS lineage
                   ON lineage.singleton=1
                WHERE validation.match_id=?
                  AND validation.validation_version=?
                  AND validation.dependency_revision<=lineage.dependency_revision
                  AND strftime('%s', prediction.prediction_cutoff) IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM draft_lineage_changes AS change
                       WHERE change.dependency_revision>
                             validation.dependency_revision
                         AND (change.affected_from_unix IS NULL OR
                              change.affected_from_unix<=CAST(
                                  strftime('%s', prediction.prediction_cutoff)
                                  AS INTEGER
                              ))
                  )""",
            (match_id, DRAFT_VALIDATION_VERSION),
        ).fetchall()
    except (TypeError, ValueError, sqlite3.Error):
        return frozenset()
    return frozenset(
        (str(row[0]), int(row[1]))
        for row in rows
        if artifacts.get((str(row[0]), int(row[1])))
        == (str(row[2]), str(row[3]))
    )


def _scope_join(
    scopes: CurrentDerivedScopes,
    match_alias: str,
    readiness: str = "current",
) -> tuple[str, tuple[str, ...]]:
    match_ids = getattr(scopes, readiness)
    payload = json.dumps(sorted(match_ids), separators=(",", ":"))
    return (
        "JOIN json_each(?) AS current_scope "
        f"ON CAST(current_scope.value AS INTEGER)={match_alias}.match_id",
        (payload,),
    )


def _scope_condition(
    scopes: CurrentDerivedScopes,
    expression: str,
    readiness: str,
) -> tuple[str, tuple[str, ...]]:
    payload = json.dumps(sorted(getattr(scopes, readiness)), separators=(",", ":"))
    return (
        f"AND {expression} IN (SELECT CAST(value AS INTEGER) FROM json_each(?))",
        (payload,),
    )


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _pagination(page: int, page_size: int, total: int) -> dict[str, int]:
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, math.ceil(total / page_size)) if total else 1,
    }


def _state_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload.pop("state_id", None)
    payload.pop("input_hash", None)
    for column in STATE_JSON_COLUMNS:
        default: Any = [] if column != "objective_conversion_json" else {}
        payload[column.removesuffix("_json")] = _json_value(
            payload.pop(column, None), default
        )
    return payload


def _compact_list_state(row: sqlite3.Row, prefix: str) -> dict[str, Any] | None:
    label = row[f"{prefix}_state_label"]
    if label is None:
        return None
    return {
        "team_id": row[f"{prefix}_state_team_id"],
        "side": prefix,
        "label": label,
        "duration_seconds": row[f"{prefix}_state_duration_seconds"],
        "max_lead": row[f"{prefix}_state_max_lead"],
        "max_deficit": row[f"{prefix}_state_max_deficit"],
        "curve_coverage": row[f"{prefix}_state_curve_coverage"],
        "label_version": LABEL_VERSION,
    }


def _count(
    connection: sqlite3.Connection,
    relation: str,
    *,
    expression: str = "*",
    join: str = "",
    join_params: tuple[Any, ...] = (),
    where: str = "",
    params: tuple[Any, ...] = (),
) -> int:
    if not _relation_exists(connection, relation):
        return 0
    try:
        row = connection.execute(
            f"SELECT COUNT({expression}) FROM {relation} {join} {where}",
            (*join_params, *params),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error) or "no such column" in str(error):
            return 0
        raise
    return int(row[0]) if row else 0


def _current_profile_rows(
    connection: sqlite3.Connection,
    scopes: CurrentDerivedScopes,
) -> list[sqlite3.Row]:
    profile_columns = _relation_columns(connection, "team_style_profiles")
    if (
        not scopes.valid_profile_cutoffs
        or not {
            "team_id",
            "profile_cutoff",
            "profile_version",
            "weighting_json",
        }.issubset(profile_columns)
    ):
        return []
    state_hashes = current_state_input_hashes(connection, scopes)
    cutoff_payload = json.dumps(
        sorted(scopes.valid_profile_cutoffs), separators=(",", ":")
    )
    stable_order = (
        "profile.profile_id DESC"
        if "profile_id" in profile_columns
        else "profile.rowid DESC"
    )
    try:
        rows = connection.execute(
            f"""SELECT profile.* FROM team_style_profiles AS profile
                JOIN json_each(?) AS cutoff
                  ON CAST(cutoff.value AS TEXT)=profile.profile_cutoff
               WHERE profile.profile_version=?
               ORDER BY profile.team_id, profile.profile_cutoff DESC,
                        {stable_order}""",
            (cutoff_payload, PROFILE_VERSION),
        ).fetchall()
    except sqlite3.Error:
        return []
    selected: dict[int, sqlite3.Row] = {}
    for row in rows:
        team_id = int(row["team_id"])
        if team_id in selected:
            continue
        if profile_weighting_is_current(row["weighting_json"], state_hashes):
            selected[team_id] = row
    return list(selected.values())


def _quality_payload(
    rows: list[dict[str, Any]],
    *,
    availability_mode: str,
    assignment_version: str,
    score_version: str,
    model_kind: str,
    horizon_minutes: int,
) -> dict[str, Any]:
    if not rows:
        reason = (
            "prospective_data_missing"
            if availability_mode == "prospective"
            else "reconstructed_data_missing"
        )
        return {
            "availability_status": "missing",
            "is_reconstructed": availability_mode == "reconstructed_walk_forward",
            "support": 0,
            "eligible_targets": 0,
            "predicted": 0,
            "insufficient_evidence": 0,
            "brier_score": None,
            "log_loss": None,
            "ece_5_bin": None,
            "ece_90_upper": None,
            "auc": None,
            "accuracy": None,
            "status": "missing",
            "gate_failures": [reason],
        }

    points = tuple(
        EvaluationPoint(
            match_id=int(row["match_id"]),
            series_id=(
                int(row["series_id"]) if row.get("series_id") is not None else None
            ),
            event_id=str(row.get("event_id") or "unknown"),
            probability=float(row["probability"]),
            outcome=bool(row["eventual_radiant_win"]),
        )
        for row in rows
        if row["probability"] is not None
        and row["eventual_radiant_win"] is not None
        and row["status"] == "settled"
    )
    metrics = evaluate_points(
        points,
        seed_material=(
            f"{BACKTEST_VERSION}:{availability_mode}:"
            f"{assignment_version}:{score_version}:"
            f"{model_kind}:{horizon_minutes}"
        ),
    )
    return {
        "availability_status": "available",
        "is_reconstructed": availability_mode == "reconstructed_walk_forward",
        "support": metrics.support,
        "eligible_targets": len(rows),
        "predicted": sum(row["probability"] is not None for row in rows),
        "insufficient_evidence": sum(
            row["status"] == "insufficient_evidence" for row in rows
        ),
        "brier_score": metrics.brier_score,
        "log_loss": metrics.log_loss,
        "ece_5_bin": metrics.ece_5_bin,
        "ece_90_upper": metrics.ece_90_upper,
        "auc": metrics.auc,
        "accuracy": metrics.accuracy,
        "status": metrics.gate_status,
        "gate_failures": list(metrics.gate_failures),
    }


def _draft_quality_slices(
    connection: sqlite3.Connection,
    scopes: CurrentDerivedScopes | None = None,
) -> list[dict[str, Any]]:
    global _DRAFT_QUALITY_CACHE
    grouped: dict[
        tuple[str, int, str, str, str], list[dict[str, Any]]
    ] = {}
    source_rows: list[dict[str, Any]] = []
    scopes = scopes or _current_scopes(connection)
    if _relation_exists(connection, "draft_model_runs") and _relation_exists(
        connection, "draft_predictions"
    ):
        ingest_columns = _relation_columns(connection, "match_ingest_status")
        has_ingest_status = "match_id" in ingest_columns
        has_lineage_columns = {"series_id", "event_id"}.issubset(ingest_columns)
        lineage_columns = (
            "status.series_id, status.event_id"
            if has_lineage_columns
            else "NULL AS series_id, NULL AS event_id"
        )
        lineage_join = (
            "LEFT JOIN match_ingest_status AS status "
            "ON status.match_id=prediction.match_id"
            if has_ingest_status
            else ""
        )
        try:
            rows = connection.execute(
                f"""SELECT run.run_id, run.model_kind, run.horizon_minutes,
                           run.availability_mode, prediction.probability,
                           prediction.eventual_radiant_win, prediction.status,
                           prediction.match_id, {lineage_columns},
                           json_extract(
                               run.configuration_json, '$.assignment_version'
                           ) AS assignment_version,
                           json_extract(
                               run.configuration_json, '$.score_version'
                           ) AS score_version
                     FROM draft_predictions AS prediction
                     JOIN draft_model_runs AS run ON run.run_id=prediction.run_id
                     {lineage_join}
                    WHERE {DRAFT_VERSION_PREDICATE}
                    ORDER BY run.model_kind, run.horizon_minutes,
                             run.availability_mode, prediction.match_id""",
                DRAFT_VERSION_PARAMS,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for raw in rows:
            row = dict(raw)
            if (str(row["run_id"]), int(row["match_id"])) not in (
                scopes.draft_predictions
            ):
                continue
            source_rows.append(row)
            key = (
                str(row["model_kind"]),
                int(row["horizon_minutes"]),
                str(row["availability_mode"]),
                str(row["assignment_version"]),
                str(row["score_version"]),
            )
            grouped.setdefault(key, []).append(row)

    fingerprint = json.dumps(
        {
            "versions": DRAFT_VERSION_PARAMS,
            "rows": source_rows,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with _DRAFT_QUALITY_LOCK:
        if _DRAFT_QUALITY_CACHE is not None and _DRAFT_QUALITY_CACHE[0] == fingerprint:
            return _clone_quality_slices(_DRAFT_QUALITY_CACHE[1])

    slices: list[dict[str, Any]] = []
    for model_kind in MODEL_KINDS:
        for horizon in LANDMARK_MINUTES:
            for (
                availability_mode,
                assignment_version,
                score_version,
            ) in DRAFT_COHORTS:
                payload = _quality_payload(
                    grouped.get(
                        (
                            model_kind,
                            horizon,
                            availability_mode,
                            assignment_version,
                            score_version,
                        ),
                        [],
                    ),
                    availability_mode=availability_mode,
                    assignment_version=assignment_version,
                    score_version=score_version,
                    model_kind=model_kind,
                    horizon_minutes=horizon,
                )
                slices.append(
                    {
                        "model_kind": model_kind,
                        "horizon_minutes": horizon,
                        "availability_mode": availability_mode,
                        "assignment_version": assignment_version,
                        "score_version": score_version,
                        **payload,
                    }
                )
    frozen = tuple(slices)
    with _DRAFT_QUALITY_LOCK:
        _DRAFT_QUALITY_CACHE = (fingerprint, frozen)
    return _clone_quality_slices(frozen)


def _clone_quality_slices(
    slices: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    return [
        {
            **row,
            "gate_failures": list(row["gate_failures"]),
        }
        for row in slices
    ]


def get_overview() -> dict[str, Any]:
    with _database() as connection:
        scopes = _current_scopes(connection)
        player_join, player_scope_params = _scope_join(
            scopes, "player_map_scores", "player"
        )
        state_join, state_scope_params = _scope_join(
            scopes, "team_map_states", "state"
        )
        current_score_where = "WHERE score_version=?"
        current_state_where = "WHERE label_version=?"
        coverage = {
            "formal_maps": len(scopes.formal),
            "player_score_rows": _count(
                connection,
                "player_map_scores",
                join=player_join,
                join_params=player_scope_params,
                where=current_score_where,
                params=(SCORE_VERSION,),
            ),
            "scored_matches": _count(
                connection,
                "player_map_scores",
                expression="DISTINCT match_id",
                join=player_join,
                join_params=player_scope_params,
                where=current_score_where,
                params=(SCORE_VERSION,),
            ),
            "scored_players": _count(
                connection,
                "player_map_scores",
                expression="DISTINCT account_id",
                join=player_join,
                join_params=player_scope_params,
                where=current_score_where + " AND account_id IS NOT NULL",
                params=(SCORE_VERSION,),
            ),
            "ranking_eligible_scores": _count(
                connection,
                "player_map_scores",
                join=player_join,
                join_params=player_scope_params,
                where=current_score_where
                + " AND json_valid(explanation_json)"
                + " AND json_extract(explanation_json, '$.ranking_eligible')=1",
                params=(SCORE_VERSION,),
            ),
            "team_state_rows": _count(
                connection,
                "team_map_states",
                join=state_join,
                join_params=state_scope_params,
                where=current_state_where,
                params=(LABEL_VERSION,),
            ),
            "state_labeled_matches": _count(
                connection,
                "team_map_states",
                expression="DISTINCT match_id",
                join=state_join,
                join_params=state_scope_params,
                where=current_state_where,
                params=(LABEL_VERSION,),
            ),
            "team_profiles": 0,
            "profiled_teams": 0,
        }
        current_profiles = _current_profile_rows(connection, scopes)
        coverage["team_profiles"] = len(current_profiles)
        coverage["profiled_teams"] = len(
            {int(row["team_id"]) for row in current_profiles}
        )

        state_distribution: dict[str, int] = {}
        state_columns = _relation_columns(connection, "team_map_states")
        if {"match_id", "label", "label_version"}.issubset(state_columns):
            state_distribution = {
                str(row["label"]): int(row["count"])
                for row in connection.execute(
                    f"""SELECT label, COUNT(*) AS count
                         FROM team_map_states {state_join}
                        WHERE label_version=?
                        GROUP BY label ORDER BY label""",
                    (*state_scope_params, LABEL_VERSION),
                ).fetchall()
            }

        coverage["draft_prediction_rows"] = len(scopes.draft_predictions)
        coverage["draft_predicted_matches"] = len(
            {match_id for _, match_id in scopes.draft_predictions}
        )

        draft_quality = _draft_quality_slices(connection, scopes)
        return {
            "versions": {
                "player_score": SCORE_VERSION,
                "team_state": LABEL_VERSION,
                "team_profile": PROFILE_VERSION,
                "draft_score": SCORE_VERSION,
                "draft_model": DRAFT_MODEL_VERSION,
                "draft_backtest": BACKTEST_VERSION,
                "draft_features": DRAFT_FEATURE_VERSION,
            },
            "coverage": coverage,
            "team_state_distribution": state_distribution,
            "draft_cohorts": [
                {
                    "availability_mode": mode,
                    "assignment_version": assignment_version,
                    "score_version": score_version,
                }
                for mode, assignment_version, score_version in DRAFT_COHORTS
            ],
            "draft_quality": draft_quality,
            "availability": {
                "reconstructed_walk_forward": any(
                    row["availability_mode"] == "reconstructed_walk_forward"
                    and row["availability_status"] == "available"
                    for row in draft_quality
                ),
                "prospective": any(
                    row["availability_mode"] == "prospective"
                    and row["availability_status"] == "available"
                    for row in draft_quality
                ),
            },
        }


def list_matches(
    *,
    page: int,
    page_size: int,
    search: str | None = None,
    label: str | None = None,
    team_id: int | None = None,
) -> dict[str, Any]:
    with _database() as connection:
        match_columns = _relation_columns(connection, "matches")
        if "match_id" not in match_columns:
            return {"data": [], "pagination": _pagination(page, page_size, 0)}

        scopes = _current_scopes(connection)
        scope_join, scope_params = _scope_join(scopes, "m", "formal")
        state_table_columns = _relation_columns(connection, "team_map_states")
        has_states = {"match_id", "side", "label", "label_version"}.issubset(
            state_table_columns
        )
        team_table_columns = _relation_columns(connection, "teams")
        league_table_columns = _relation_columns(connection, "leagues")
        teams_join = (
            {"team_id", "name"}.issubset(team_table_columns)
            and {"radiant_team_id", "dire_team_id"}.issubset(match_columns)
        )
        leagues_join = (
            {"leagueid", "name"}.issubset(league_table_columns)
            and "leagueid" in match_columns
        )
        joins: list[str] = []
        params: list[Any] = []
        if has_states:
            radiant_scope, radiant_scope_params = _scope_condition(
                scopes, "radiant_state.match_id", "state"
            )
            dire_scope, dire_scope_params = _scope_condition(
                scopes, "dire_state.match_id", "state"
            )
            joins.extend(
                (
                    """LEFT JOIN team_map_states AS radiant_state
                           ON radiant_state.match_id=m.match_id
                          AND radiant_state.side='radiant'
                          AND radiant_state.label_version=? """
                    + radiant_scope,
                    """LEFT JOIN team_map_states AS dire_state
                           ON dire_state.match_id=m.match_id
                          AND dire_state.side='dire'
                          AND dire_state.label_version=? """
                    + dire_scope,
                )
            )
            params.extend(
                (
                    LABEL_VERSION,
                    *radiant_scope_params,
                    LABEL_VERSION,
                    *dire_scope_params,
                )
            )
        joins.append(scope_join)
        if teams_join:
            joins.extend(
                (
                    "LEFT JOIN teams AS radiant_team ON radiant_team.team_id=m.radiant_team_id",
                    "LEFT JOIN teams AS dire_team ON dire_team.team_id=m.dire_team_id",
                )
            )
        if leagues_join:
            joins.append("LEFT JOIN leagues AS league ON league.leagueid=m.leagueid")

        team_columns = (
            "radiant_team.name AS radiant_team_name, "
            "dire_team.name AS dire_team_name"
            if teams_join
            else "NULL AS radiant_team_name, NULL AS dire_team_name"
        )
        league_column = (
            "league.name AS league_name"
            if leagues_join
            else "NULL AS league_name"
        )
        def state_column(table_alias: str, prefix: str, name: str) -> str:
            output_name = f"{prefix}_state_{name}"
            if has_states and name in state_table_columns:
                return f"{table_alias}.{name} AS {output_name}"
            return f"NULL AS {output_name}"

        state_columns = ", ".join(
            state_column(table_alias, prefix, name)
            for table_alias, prefix in (
                ("radiant_state", "radiant"),
                ("dire_state", "dire"),
            )
            for name in (
                "team_id",
                "label",
                "duration_seconds",
                "max_lead",
                "max_deficit",
                "curve_coverage",
            )
        )
        where = ["1=1"]
        params.extend(scope_params)
        normalized_search = search.strip() if search and search.strip() else None
        if normalized_search:
            search_terms = ["CAST(m.match_id AS TEXT) LIKE ?"]
            if teams_join:
                search_terms.extend(
                    ("radiant_team.name LIKE ?", "dire_team.name LIKE ?")
                )
            if leagues_join:
                search_terms.append("league.name LIKE ?")
            where.append("(" + " OR ".join(search_terms) + ")")
            params.extend([f"%{normalized_search}%"] * len(search_terms))
        if label:
            if has_states:
                where.append("(radiant_state.label=? OR dire_state.label=?)")
                params.extend((label, label))
            else:
                where.append("0=1")
        if team_id is not None:
            if {"radiant_team_id", "dire_team_id"}.issubset(match_columns):
                where.append("(m.radiant_team_id=? OR m.dire_team_id=?)")
                params.extend((team_id, team_id))
            else:
                where.append("0=1")

        from_sql = "FROM matches AS m " + " ".join(joins)
        where_sql = "WHERE " + " AND ".join(where)
        total = int(
            connection.execute(
                f"SELECT COUNT(*) {from_sql} {where_sql}", tuple(params)
            ).fetchone()[0]
        )
        def match_column(name: str) -> str:
            return f"m.{name} AS {name}" if name in match_columns else f"NULL AS {name}"

        match_select = ", ".join(
            match_column(name)
            for name in (
                "match_id",
                "radiant_team_id",
                "dire_team_id",
                "radiant_win",
                "duration",
                "start_time",
                "leagueid",
                "radiant_score",
                "dire_score",
            )
        )
        rows = connection.execute(
            f"""SELECT {match_select},
                       {team_columns}, {league_column},
                       {state_columns}
                  {from_sql} {where_sql}
                 ORDER BY start_time DESC, match_id DESC
                 LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()

        data = []
        for row in rows:
            data.append(
                {
                    "match_id": row["match_id"],
                    "radiant_team_id": row["radiant_team_id"],
                    "dire_team_id": row["dire_team_id"],
                    "radiant_team_name": row["radiant_team_name"],
                    "dire_team_name": row["dire_team_name"],
                    "radiant_win": (
                        bool(row["radiant_win"])
                        if row["radiant_win"] is not None
                        else None
                    ),
                    "duration": row["duration"],
                    "start_time": row["start_time"],
                    "leagueid": row["leagueid"],
                    "league_name": row["league_name"],
                    "radiant_score": row["radiant_score"],
                    "dire_score": row["dire_score"],
                    "radiant_state": _compact_list_state(row, "radiant"),
                    "dire_state": _compact_list_state(row, "dire"),
                }
            )
        return {"data": data, "pagination": _pagination(page, page_size, total)}


def _match_row(connection: sqlite3.Connection, match_id: int) -> dict[str, Any] | None:
    match_columns = _relation_columns(connection, "matches")
    if "match_id" not in match_columns:
        return None
    team_columns = _relation_columns(connection, "teams")
    league_columns = _relation_columns(connection, "leagues")
    teams_join = (
        {"team_id", "name"}.issubset(team_columns)
        and {"radiant_team_id", "dire_team_id"}.issubset(match_columns)
    )
    leagues_join = (
        {"leagueid", "name"}.issubset(league_columns)
        and "leagueid" in match_columns
    )
    joins: list[str] = []
    if teams_join:
        joins.extend(
            (
                "LEFT JOIN teams AS radiant_team ON radiant_team.team_id=m.radiant_team_id",
                "LEFT JOIN teams AS dire_team ON dire_team.team_id=m.dire_team_id",
            )
        )
    if leagues_join:
        joins.append("LEFT JOIN leagues AS league ON league.leagueid=m.leagueid")
    team_names = (
        "radiant_team.name AS radiant_team_name, dire_team.name AS dire_team_name"
        if teams_join
        else "NULL AS radiant_team_name, NULL AS dire_team_name"
    )
    league_name = (
        "league.name AS league_name" if leagues_join else "NULL AS league_name"
    )
    def match_column(name: str) -> str:
        return f"m.{name} AS {name}" if name in match_columns else f"NULL AS {name}"

    selected_columns = ", ".join(
        match_column(name)
        for name in (
            "match_id",
            "radiant_team_id",
            "dire_team_id",
            "radiant_win",
            "duration",
            "start_time",
            "leagueid",
            "radiant_score",
            "dire_score",
        )
    )
    row = connection.execute(
        f"""SELECT {selected_columns},
                   {team_names}, {league_name}
              FROM matches AS m {' '.join(joins)}
             WHERE m.match_id=?""",
        (match_id,),
    ).fetchone()
    if row is None:
        return None
    payload = dict(row)
    raw_radiant_win = payload["radiant_win"]
    payload["radiant_win"] = (
        bool(raw_radiant_win)
        if type(raw_radiant_win) is int and raw_radiant_win in {0, 1}
        else None
    )
    return payload


def _match_states(
    connection: sqlite3.Connection, match_id: int
) -> dict[str, dict[str, Any] | None]:
    result: dict[str, dict[str, Any] | None] = {"radiant": None, "dire": None}
    state_columns = _relation_columns(connection, "team_map_states")
    if not {"match_id", "side", "label", "label_version"}.issubset(state_columns):
        return result
    selected_columns = ", ".join(
        f"state.{name} AS {name}" if name in state_columns else f"NULL AS {name}"
        for name in (
            "state_id",
            "match_id",
            "team_id",
            "side",
            "label",
            "duration_seconds",
            "max_lead",
            "max_deficit",
            "ahead_fraction",
            "behind_fraction",
            "even_fraction",
            "signed_auc",
            "absolute_auc",
            "crossings_json",
            "first_significant_lead_at",
            "first_significant_deficit_at",
            "closeout_seconds",
            "objective_conversion_json",
            "curve_coverage",
            "source_versions_json",
            "input_hash",
            "label_version",
            "created_at",
        )
    )
    rows = connection.execute(
        f"""SELECT {selected_columns}
             FROM team_map_states AS state
            WHERE state.match_id=? AND state.label_version=?
            ORDER BY state.side""",
        (match_id, LABEL_VERSION),
    ).fetchall()
    for row in rows:
        result[str(row["side"])] = _state_payload(row)
    return result


def _match_performance(
    connection: sqlite3.Connection, match_id: int
) -> list[dict[str, Any]]:
    match_player_columns = _relation_columns(connection, "match_players")
    if not {"match_id", "player_slot"}.issubset(match_player_columns):
        return []
    hero_columns = _relation_columns(connection, "heroes")
    has_heroes = (
        "hero_id" in match_player_columns
        and {"hero_id", "localized_name"}.issubset(hero_columns)
    )
    fact_columns = _relation_columns(connection, "player_map_facts")
    has_facts = {
        "fact_id",
        "match_id",
        "player_slot",
        "facts_json",
    }.issubset(fact_columns)

    def player_column(name: str) -> str:
        return f"player.{name}" if name in match_player_columns else "NULL"

    joins: list[str] = []
    if has_heroes:
        joins.append("LEFT JOIN heroes AS hero ON hero.hero_id=player.hero_id")
    if has_facts:
        fact_order = (
            "latest.created_at DESC, latest.fact_id DESC"
            if "created_at" in fact_columns
            else "latest.fact_id DESC"
        )
        joins.append(
            f"""LEFT JOIN player_map_facts AS fact
                    ON fact.fact_id=(
                        SELECT latest.fact_id FROM player_map_facts AS latest
                         WHERE latest.match_id=player.match_id
                           AND latest.player_slot=player.player_slot
                         ORDER BY {fact_order}
                         LIMIT 1
                    )"""
        )

    hero_column = (
        "hero.localized_name AS hero_name" if has_heroes else "NULL AS hero_name"
    )
    identity_column = (
        "CASE WHEN json_valid(fact.facts_json) THEN "
        "COALESCE(json_extract(fact.facts_json, '$.name'), "
        "json_extract(fact.facts_json, '$.personaname')) END AS player_name"
        if has_facts
        else "NULL AS player_name"
    )
    performance_columns = ", ".join(
        f"{player_column(field)} AS performance_{field}"
        for field in PLAYER_PERFORMANCE_FIELDS
    )
    rows = connection.execute(
        f"""SELECT player.player_slot,
                   {player_column('account_id')} AS account_id,
                   {player_column('team_id')} AS team_id,
                   {player_column('hero_id')} AS hero_id,
                   {player_column('is_radiant')} AS is_radiant,
                   {hero_column}, {identity_column}, {performance_columns}
              FROM match_players AS player {' '.join(joins)}
             WHERE player.match_id=?
             ORDER BY player.player_slot""",
        (match_id,),
    ).fetchall()
    result = []
    for row in rows:
        performance = {
            field: row[f"performance_{field}"] for field in PLAYER_PERFORMANCE_FIELDS
        }
        result.append(
            {
                "player_slot": row["player_slot"],
                "account_id": row["account_id"],
                "player_name": row["player_name"],
                "team_id": row["team_id"],
                "side": (
                    "radiant"
                    if row["is_radiant"] == 1
                    else "dire" if row["is_radiant"] == 0 else None
                ),
                "hero_id": row["hero_id"],
                "hero_name": row["hero_name"],
                "performance": (
                    performance
                    if any(value is not None for value in performance.values())
                    else None
                ),
            }
        )
    return result


def _match_players(
    connection: sqlite3.Connection,
    match_id: int,
    performance_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    score_columns = _relation_columns(connection, "player_map_scores")
    if not {"match_id", "player_slot", "score_version"}.issubset(score_columns):
        return []
    performance_by_slot = {
        row["player_slot"]: row
        for row in (
            performance_rows
            if performance_rows is not None
            else _match_performance(connection, match_id)
        )
    }
    def score_column(name: str) -> str:
        return f"score.{name} AS {name}" if name in score_columns else f"NULL AS {name}"

    selected_columns = ", ".join(
        score_column(name)
        for name in (
            "player_slot",
            "account_id",
            "position",
            "execution_score",
            "result_adjusted_score",
            "coverage",
            "role_confidence",
            "benchmark_cutoff",
            "score_version",
            "component_facts_json",
            "component_scores_json",
            "weights_json",
            "explanation_json",
        )
    )
    rows = connection.execute(
        f"""SELECT {selected_columns}
              FROM player_map_scores AS score
             WHERE score.match_id=? AND score.score_version=?
             ORDER BY score.player_slot""",
        (match_id, SCORE_VERSION),
    ).fetchall()
    result = []
    for row in rows:
        raw = performance_by_slot.get(row["player_slot"], {})
        explanation = _json_value(row["explanation_json"], {})
        result.append(
            {
                "player_slot": row["player_slot"],
                "account_id": (
                    row["account_id"]
                    if row["account_id"] is not None
                    else raw.get("account_id")
                ),
                "player_name": raw.get("player_name"),
                "team_id": raw.get("team_id"),
                "side": raw.get("side"),
                "hero_id": raw.get("hero_id"),
                "hero_name": raw.get("hero_name"),
                "performance": raw.get("performance"),
                "position": row["position"],
                "execution_score": _finite_number(row["execution_score"]),
                "result_adjusted_score": _finite_number(
                    row["result_adjusted_score"]
                ),
                "coverage": _finite_number(row["coverage"]),
                "role_confidence": _finite_number(row["role_confidence"]),
                "ranking_eligible": bool(explanation.get("ranking_eligible", False)),
                "benchmark_cutoff": row["benchmark_cutoff"],
                "score_version": row["score_version"],
                "component_facts": _json_value(row["component_facts_json"], {}),
                "component_scores": _json_value(row["component_scores_json"], []),
                "weights": _json_value(row["weights_json"], []),
                "explanation": explanation,
            }
        )
    return result


def _match_rosh_lineup_score(
    connection: sqlite3.Connection,
    match_id: int,
) -> dict[str, Any]:
    missing = {
        "status": "missing",
        "reason": "historical_rosh_lineup_score_missing",
        "data": None,
    }
    player_columns = _relation_columns(connection, "match_players")
    required = {"match_id", "player_slot", "hero_id", "account_id", "is_radiant"}
    if not required.issubset(player_columns):
        return missing
    try:
        rows = connection.execute(
            """SELECT player_slot, hero_id, account_id, is_radiant
                 FROM match_players
                WHERE match_id=?
                ORDER BY player_slot""",
            (match_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return missing
    expected_slots = (*range(5), *range(128, 133))
    if len(rows) != 10 or tuple(row["player_slot"] for row in rows) != expected_slots:
        return missing
    if any(
        type(row["hero_id"]) is not int
        or int(row["hero_id"]) <= 0
        or type(row["account_id"]) is not int
        or int(row["account_id"]) <= 0
        or row["is_radiant"] != (1 if index < 5 else 0)
        for index, row in enumerate(rows)
    ):
        return missing
    hero_ids = tuple(int(row["hero_id"]) for row in rows)
    player_ids = tuple(int(row["account_id"]) for row in rows)
    if len(set(hero_ids)) != 10 or len(set(player_ids)) != 10:
        return missing
    score = query_historical_rosh_lineup_score(
        connection,
        match_id=match_id,
        formula_version=ROSH_FORMULA_VERSION,
        radiant_hero_ids=hero_ids[:5],
        dire_hero_ids=hero_ids[5:],
        radiant_player_ids=player_ids[:5],
        dire_player_ids=player_ids[5:],
    )
    if score is None:
        return missing
    return {
        "status": "available",
        "reason": "historical_rosh_lineup_score_available",
        "data": {
            "pure_lineup_score": score.pure_lineup_score,
            "current_player_adjusted_lineup_score": (
                score.current_player_adjusted_lineup_score
            ),
            "effective_lineup_score": score.effective_lineup_score,
            "scoring_mode": score.scoring_mode,
            "player_coverage_count": score.player_coverage_count,
            "formula_version": score.formula_version,
            "source_name": score.source_name,
            "source_week": score.source_week,
            "source_as_of": score.source_as_of.isoformat(),
            "player_stats_as_of": (
                None
                if score.player_stats_as_of is None
                else score.player_stats_as_of.isoformat()
            ),
            "backtest_eligible": score.backtest_eligible,
            "pure_minute_table": score.evidence["pure_minute_table"],
            "current_player_adjusted_minute_table": score.evidence.get(
                "minute_table"
            ),
        },
    }


def _match_rating(
    player_scores: list[dict[str, Any]],
) -> dict[str, Any] | None:
    expected_radiant = frozenset(range(5))
    expected_dire = frozenset(range(128, 133))
    if len(player_scores) != 10:
        return None

    slots = [row.get("player_slot") for row in player_scores]
    if any(type(slot) is not int for slot in slots) or len(set(slots)) != 10:
        return None
    radiant_slots = frozenset(slot for slot in slots if 0 <= slot <= 4)
    dire_slots = frozenset(slot for slot in slots if 128 <= slot <= 132)
    if radiant_slots != expected_radiant or dire_slots != expected_dire:
        return None

    fields = ("execution_score", "result_adjusted_score", "coverage")
    for row in player_scores:
        if row.get("score_version") != SCORE_VERSION:
            return None
        if any(_finite_number(row.get(field)) is None for field in fields):
            return None
    cutoffs = {row.get("benchmark_cutoff") for row in player_scores}
    if (
        len(cutoffs) != 1
        or not isinstance(next(iter(cutoffs)), str)
        or not str(next(iter(cutoffs))).strip()
    ):
        return None
    benchmark_cutoff = str(next(iter(cutoffs)))

    def averages(rows: list[dict[str, Any]]) -> dict[str, float]:
        return {
            field: float(
                (
                    sum(Decimal(str(row[field])) for row in rows)
                    / Decimal(len(rows))
                ).quantize(_MATCH_RATING_QUANTUM, rounding=ROUND_HALF_UP)
            )
            for field in fields
        }

    radiant = [row for row in player_scores if int(row["player_slot"]) < 128]
    dire = [row for row in player_scores if int(row["player_slot"]) >= 128]
    return {
        "rating_version": MATCH_RATING_VERSION,
        "rounding": MATCH_RATING_ROUNDING,
        "source_score_version": SCORE_VERSION,
        "benchmark_cutoff": benchmark_cutoff,
        "player_count": len(player_scores),
        "overall": averages(player_scores),
        "radiant": averages(radiant),
        "dire": averages(dire),
    }


def _match_draft_predictions(
    connection: sqlite3.Connection,
    match_id: int,
    prediction_keys: frozenset[tuple[str, int]],
) -> list[dict[str, Any]]:
    if not _relation_exists(connection, "draft_model_runs") or not _relation_exists(
        connection, "draft_predictions"
    ):
        return []
    try:
        rows = connection.execute(
            f"""SELECT run.run_id, run.model_version, run.model_kind,
                      run.horizon_minutes,
                      run.availability_mode, run.training_cutoff,
                      run.status AS model_status,
                      CASE WHEN json_valid(run.configuration_json) THEN
                          json_extract(run.configuration_json, '$.assignment_version')
                      END AS assignment_version,
                      CASE WHEN json_valid(run.configuration_json) THEN
                          json_extract(run.configuration_json, '$.score_version')
                      END AS score_version,
                      prediction.prediction_cutoff, prediction.cutoff_source,
                      prediction.probability, prediction.uncertainty,
                      prediction.support, prediction.eventual_radiant_win,
                      prediction.status
                 FROM draft_predictions AS prediction
                 JOIN draft_model_runs AS run ON run.run_id=prediction.run_id
                   WHERE prediction.match_id=?
                     AND {DRAFT_VERSION_PREDICATE}
                   ORDER BY run.horizon_minutes, run.model_kind,
                         run.availability_mode""",
            (match_id, *DRAFT_VERSION_PARAMS),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {key: value for key, value in dict(row).items() if key != "run_id"}
        for row in rows
        if (str(row["run_id"]), match_id) in prediction_keys
    ]


def _match_detail_payload(
    connection: sqlite3.Connection,
    match_id: int,
    scopes: CurrentDerivedScopes,
) -> dict[str, Any] | None:
    match = _match_row(connection, match_id)
    if match is None:
        return None
    states = (
        _match_states(connection, match_id)
        if match_id in scopes.state
        else {"radiant": None, "dire": None}
    )
    player_performance = _match_performance(connection, match_id)
    player_scores = (
        _match_players(connection, match_id, player_performance)
        if match_id in scopes.player
        else []
    )
    draft_predictions = (
        _match_draft_predictions(connection, match_id, scopes.draft_predictions)
        if match_id in scopes.draft
        else []
    )
    return {
        "match": match,
        # Keep the flat match fields and state alias for older read-only
        # clients while the nested shape remains the canonical response.
        **match,
        "states": states,
        "radiant_state": states["radiant"],
        "dire_state": states["dire"],
        "player_performance": player_performance,
        "player_scores": player_scores,
        "match_rating": _match_rating(player_scores),
        "rosh_lineup_score": _match_rosh_lineup_score(connection, match_id),
        "draft_predictions": draft_predictions,
        "versions": {
            "match_rating": MATCH_RATING_VERSION,
            "player_score": SCORE_VERSION,
            "team_state": LABEL_VERSION,
            "team_profile": PROFILE_VERSION,
            "draft_score": SCORE_VERSION,
            "draft_model": DRAFT_MODEL_VERSION,
            "draft_backtest": BACKTEST_VERSION,
            "draft_features": DRAFT_FEATURE_VERSION,
            "rosh_lineup": ROSH_FORMULA_VERSION,
        },
        "cutoffs": {
            "player_score": sorted(
                {
                    str(row["benchmark_cutoff"])
                    for row in player_scores
                    if row.get("benchmark_cutoff") is not None
                }
            ),
            "draft_training": sorted(
                {
                    str(row["training_cutoff"])
                    for row in draft_predictions
                    if row.get("training_cutoff") is not None
                }
            ),
            "draft_prediction": sorted(
                {
                    str(row["prediction_cutoff"])
                    for row in draft_predictions
                    if row.get("prediction_cutoff") is not None
                }
            ),
        },
    }


def get_match(match_id: int) -> dict[str, Any] | None:
    with _database() as connection:
        scopes = _current_scopes(connection)
        if match_id not in scopes.formal:
            return None
        return _match_detail_payload(connection, match_id, scopes)


def _postmatch_base(
    raybet_match_id: str,
    map_number: int,
    odds_timeline: list[dict[str, Any]],
    *,
    checked_at: datetime,
) -> dict[str, Any]:
    return {
        "raybet_match_id": raybet_match_id,
        "map_number": map_number,
        "checked_at": checked_at.isoformat(),
        "status": "unavailable",
        "reason": "reconciliation_missing",
        "mapping": None,
        "reconciliation": None,
        "odds_timeline": odds_timeline,
        "postmatch": None,
        "warnings": [],
    }


def _downsample_timeline(
    points: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    if limit <= 1:
        raise ValueError("max_points must be greater than one")
    if len(points) <= limit:
        return points
    last = len(points) - 1
    indexes = sorted({round(index * last / (limit - 1)) for index in range(limit)})
    return [points[index] for index in indexes]


def _mapping_payload(mapping: StrictLiveMapMapping) -> dict[str, Any]:
    return {
        "mapping_id": mapping.mapping_id,
        "event_id": mapping.event_id,
        "acceptance_mode": mapping.acceptance_mode,
        "mapping_version": mapping.mapping_version,
        "canonical_teams": [
            {
                "side": "team_one",
                "team_id": mapping.canonical_team_one_id,
                "team_name": mapping.canonical_team_one_name,
            },
            {
                "side": "team_two",
                "team_id": mapping.canonical_team_two_id,
                "team_name": mapping.canonical_team_two_name,
            },
        ],
        "accepted_at": mapping.accepted_at.isoformat(),
        "recorded_at": mapping.recorded_at.isoformat(),
    }


def _reconciliation_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "strict_mapping_id",
            "dota_match_id",
            "raybet_winner_side",
            "opendota_winner_side",
            "raybet_evidence_ref",
            "opendota_evidence_ref",
            "status",
            "reason",
            "first_observed_at",
            "updated_at",
        )
    }


def _aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _confirmed_evidence_reason(
    connection: sqlite3.Connection,
    reconciliation: sqlite3.Row,
    *,
    raybet_match_id: str,
    map_number: int,
    mapping_recorded_at: datetime,
    reconciliation_first_observed_at: datetime,
    reconciliation_updated_at: datetime,
    read_cutoff: datetime,
) -> str | None:
    required = {
        "raybet_match_id",
        "map_number",
        "dota_match_id",
        "source",
        "status",
        "winner_side",
        "evidence_ref",
        "facts_json",
        "observed_at",
        "evidence_id",
    }
    if not required.issubset(
        _relation_columns(connection, "settlement_result_evidence")
    ):
        return "settlement_evidence_schema_unavailable"
    rows = connection.execute(
        """SELECT dota_match_id, source, status, winner_side,
                  evidence_ref, facts_json, observed_at
             FROM settlement_result_evidence
            WHERE raybet_match_id=? AND map_number=?
            ORDER BY source, observed_at, evidence_id""",
        (raybet_match_id, map_number),
    ).fetchall()
    dota_match_id = int(reconciliation["dota_match_id"])
    expected = {
        "raybet": (
            str(reconciliation["raybet_evidence_ref"]),
            reconciliation["raybet_winner_side"],
        ),
        "opendota": (
            str(reconciliation["opendota_evidence_ref"]),
            reconciliation["opendota_winner_side"],
        ),
    }
    for source, (evidence_ref, winner_side) in expected.items():
        exact = [
            row
            for row in rows
            if row["source"] == source and row["evidence_ref"] == evidence_ref
        ]
        if len(exact) != 1:
            return "settlement_evidence_missing"
        row = exact[0]
        if (
            isinstance(row["dota_match_id"], bool)
            or not isinstance(row["dota_match_id"], int)
            or row["dota_match_id"] != dota_match_id
            or row["status"] != "confirmed"
            or row["winner_side"] != winner_side
            or not isinstance(row["facts_json"], str)
        ):
            return "settlement_evidence_conflict"
        observed_at = _aware_timestamp(row["observed_at"])
        if (
            observed_at is None
            or observed_at < mapping_recorded_at
            or observed_at < reconciliation_first_observed_at
            or observed_at > reconciliation_updated_at
            or observed_at > read_cutoff
        ):
            return "settlement_evidence_causal_order_invalid"
        try:
            facts = json.loads(row["facts_json"])
        except json.JSONDecodeError:
            return "settlement_evidence_conflict"
        if not isinstance(facts, dict):
            return "settlement_evidence_conflict"
        if source == "raybet" and (
            facts.get("status") != "confirmed"
            or facts.get("winner_side") != winner_side
        ):
            return "settlement_evidence_conflict"
        if source == "opendota" and (
            isinstance(facts.get("dota_match_id"), bool)
            or facts.get("dota_match_id") != dota_match_id
            or facts.get("winner_side") != winner_side
        ):
            return "settlement_evidence_conflict"
    for row in rows:
        if row["source"] not in expected:
            return "settlement_evidence_conflict"
        if (
            isinstance(row["dota_match_id"], bool)
            or not isinstance(row["dota_match_id"], int)
            or row["dota_match_id"] != dota_match_id
        ):
            return "settlement_evidence_conflict"
        observed_at = _aware_timestamp(row["observed_at"])
        if (
            observed_at is None
            or observed_at < mapping_recorded_at
            or observed_at < reconciliation_first_observed_at
            or observed_at > reconciliation_updated_at
            or observed_at > read_cutoff
        ):
            return "settlement_evidence_causal_order_invalid"
        expected_winner = expected[str(row["source"])][1]
        if row["status"] in {"confirmed", "conflict"} and (
            row["winner_side"] is not None
            and row["winner_side"] != expected_winner
        ):
            return "settlement_evidence_conflict"
        try:
            facts = json.loads(row["facts_json"])
        except (TypeError, json.JSONDecodeError):
            return "settlement_evidence_conflict"
        if not isinstance(facts, dict):
            return "settlement_evidence_conflict"
        if row["source"] == "raybet" and (
            facts.get("status") != row["status"]
            or facts.get("winner_side") != row["winner_side"]
        ):
            return "settlement_evidence_conflict"
        if row["source"] == "opendota" and (
            facts.get("dota_match_id") != dota_match_id
            or facts.get("winner_side") != row["winner_side"]
        ):
            return "settlement_evidence_conflict"
    return None


def _duplicate_link_reason(
    connection: sqlite3.Connection,
    *,
    raybet_match_id: str,
    map_number: int,
    dota_match_id: int,
) -> str | None:
    duplicate = connection.execute(
        """SELECT 1 FROM settlement_reconciliations
            WHERE dota_match_id=?
              AND (raybet_match_id!=? OR map_number!=?)
            LIMIT 1""",
        (dota_match_id, raybet_match_id, map_number),
    ).fetchone()
    if duplicate is not None:
        return "opendota_match_link_conflict"
    map_result_columns = _relation_columns(connection, "map_results")
    if {"raybet_match_id", "map_number", "dota_match_id"}.issubset(
        map_result_columns
    ):
        conflict = connection.execute(
            """SELECT 1 FROM map_results
                WHERE (dota_match_id=?
                       AND (raybet_match_id!=? OR map_number!=?))
                   OR (raybet_match_id=? AND map_number=?
                       AND dota_match_id!=?)
                LIMIT 1""",
            (
                dota_match_id,
                raybet_match_id,
                map_number,
                raybet_match_id,
                map_number,
                dota_match_id,
            ),
        ).fetchone()
        if conflict is not None:
            return "opendota_match_link_conflict"
    return None


def _confirmed_map_result_reason(
    connection: sqlite3.Connection,
    reconciliation: sqlite3.Row,
    *,
    raybet_match_id: str,
    map_number: int,
    strict_mapping_id: int,
    mapping_recorded_at: datetime,
    reconciliation_first_observed_at: datetime,
    reconciliation_updated_at: datetime,
    read_cutoff: datetime,
) -> str | None:
    required = {
        "raybet_match_id",
        "map_number",
        "strict_mapping_id",
        "dota_match_id",
        "winner_side",
        "evidence_ref",
        "settled_at",
    }
    if not required.issubset(_relation_columns(connection, "map_results")):
        return "map_result_schema_unavailable"
    row = connection.execute(
        """SELECT strict_mapping_id, dota_match_id, winner_side,
                  evidence_ref, settled_at
             FROM map_results
            WHERE raybet_match_id=? AND map_number=?""",
        (raybet_match_id, map_number),
    ).fetchone()
    if row is None:
        return "map_result_missing"
    if (
        type(row["strict_mapping_id"]) is not int
        or int(row["strict_mapping_id"]) != strict_mapping_id
    ):
        return "map_result_mapping_lineage_unverified"
    if (
        type(row["dota_match_id"]) is not int
        or int(row["dota_match_id"]) != int(reconciliation["dota_match_id"])
        or row["winner_side"] != reconciliation["opendota_winner_side"]
        or row["evidence_ref"]
        != f"settlement-reconciliation:{raybet_match_id}:map:{map_number}"
    ):
        return "opendota_result_identity_conflict"
    settled_at = _aware_timestamp(row["settled_at"])
    if (
        settled_at is None
        or settled_at < mapping_recorded_at
        or settled_at < reconciliation_first_observed_at
        or settled_at > reconciliation_updated_at
        or settled_at > read_cutoff
    ):
        return "map_result_causal_order_invalid"
    return None


def _opendota_identity_reason(
    connection: sqlite3.Connection,
    mapping: StrictLiveMapMapping,
    reconciliation: sqlite3.Row,
    *,
    map_number: int,
    scopes: CurrentDerivedScopes,
) -> tuple[str | None, str | None]:
    dota_match_id = int(reconciliation["dota_match_id"])
    match = _match_row(connection, dota_match_id)
    if match is None:
        return "unavailable", "opendota_match_unavailable"
    if not scopes.available:
        return "unavailable", "opendota_scope_schema_unavailable"
    if dota_match_id not in scopes.formal:
        return "review", "opendota_match_out_of_scope"

    ingest_columns = _relation_columns(connection, "match_ingest_status")
    if not {"match_id", "event_id", "map_number"}.issubset(ingest_columns):
        return "unavailable", "opendota_ingest_schema_unavailable"
    selected = ["event_id", "map_number"]
    for optional in (
        "ingest_state",
        "reconciliation_status",
        "missing_fields_json",
    ):
        if optional in ingest_columns:
            selected.append(optional)
    ingest = connection.execute(
        f"SELECT {', '.join(selected)} FROM match_ingest_status WHERE match_id=?",
        (dota_match_id,),
    ).fetchone()
    if ingest is None:
        return "unavailable", "opendota_ingest_unavailable"
    if str(ingest["event_id"]) != mapping.event_id:
        return "review", "opendota_event_identity_conflict"
    if (
        isinstance(ingest["map_number"], bool)
        or not isinstance(ingest["map_number"], int)
        or ingest["map_number"] <= 0
        or ingest["map_number"] != map_number
    ):
        return "review", "opendota_map_number_conflict"
    if "ingest_state" in selected and ingest["ingest_state"] == "review_required":
        return "review", "opendota_ingest_review_required"
    if (
        "reconciliation_status" in selected
        and ingest["reconciliation_status"] == "review_required"
    ):
        return "review", "opendota_ingest_review_required"

    radiant_team_id = match.get("radiant_team_id")
    dire_team_id = match.get("dire_team_id")
    canonical_ids = {
        mapping.canonical_team_one_id,
        mapping.canonical_team_two_id,
    }
    if (
        isinstance(radiant_team_id, bool)
        or isinstance(dire_team_id, bool)
        or not isinstance(radiant_team_id, int)
        or not isinstance(dire_team_id, int)
        or {radiant_team_id, dire_team_id} != canonical_ids
    ):
        return "review", "opendota_team_identity_conflict"
    radiant_win = match.get("radiant_win")
    if not isinstance(radiant_win, bool):
        return "review", "opendota_result_identity_conflict"
    team_one_is_radiant = radiant_team_id == mapping.canonical_team_one_id
    team_one_won = radiant_win == team_one_is_radiant
    expected_winner = "team_one" if team_one_won else "team_two"
    if reconciliation["opendota_winner_side"] != expected_winner:
        return "review", "opendota_winner_identity_conflict"
    if (
        reconciliation["raybet_winner_side"] not in {"team_one", "team_two"}
        or reconciliation["raybet_winner_side"]
        != reconciliation["opendota_winner_side"]
    ):
        return "review", "reconciliation_winner_conflict"
    return None, None


def _curve_rows(
    connection: sqlite3.Connection,
    relation: str,
    match_id: int,
) -> list[dict[str, Any]]:
    if not {"match_id", "time_min", "value"}.issubset(
        _relation_columns(connection, relation)
    ):
        return []
    result = []
    for row in connection.execute(
        f"SELECT time_min, value FROM {relation} "
        "WHERE match_id=? AND time_min>=0 ORDER BY time_min",
        (match_id,),
    ).fetchall():
        minute = row["time_min"]
        value = _finite_number(row["value"])
        if (
            isinstance(minute, bool)
            or not isinstance(minute, int)
            or minute < 0
            or value is None
        ):
            continue
        result.append({"minute": minute, "value": value})
    return result


def _curve_value(rows: list[dict[str, Any]], game_time_seconds: int) -> Any:
    minute = game_time_seconds // 60
    value = None
    for row in rows:
        if row["minute"] > minute:
            break
        value = row["value"]
    return value


def _timeline_complete(
    rows: list[dict[str, Any]], duration_seconds: int | None
) -> bool:
    if duration_seconds is None or duration_seconds <= 0:
        return False
    end_minute = duration_seconds // 60 - 2
    if end_minute < 10:
        return False
    observed = {row["minute"] for row in rows}
    return all(minute in observed for minute in range(10, end_minute + 1))


def _odds_at_game_time(
    odds_timeline: list[dict[str, Any]],
    game_time_seconds: int,
    map_number: int,
) -> tuple[float | None, float | None]:
    selected: dict[str, Any] | None = None
    selected_clock = -1
    for point in odds_timeline:
        clock = point.get("game_clock_seconds")
        if isinstance(clock, bool) or not isinstance(clock, int):
            continue
        if point.get("map_number") != map_number:
            continue
        if selected_clock <= clock < game_time_seconds:
            selected = point
            selected_clock = clock
    if selected is None:
        return None, None
    probabilities = selected.get("probabilities")
    if not isinstance(probabilities, dict):
        return None, None
    return (
        _finite_number(probabilities.get("team_one")),
        _finite_number(probabilities.get("team_two")),
    )


def _event_row(
    *,
    game_time_seconds: int,
    event_type: str,
    side: str | None,
    label: str,
    details: dict[str, Any],
    gold: list[dict[str, Any]],
    xp: list[dict[str, Any]],
    odds_timeline: list[dict[str, Any]],
    map_number: int,
) -> dict[str, Any]:
    team_one_probability, team_two_probability = _odds_at_game_time(
        odds_timeline, game_time_seconds, map_number
    )
    return {
        "game_time_seconds": game_time_seconds,
        "event_type": event_type,
        "side": side,
        "label": label,
        "radiant_gold_adv": _curve_value(gold, game_time_seconds),
        "radiant_xp_adv": _curve_value(xp, game_time_seconds),
        "team_one_probability": team_one_probability,
        "team_two_probability": team_two_probability,
        "details": details,
    }


def _player_side(player_slot: Any) -> str | None:
    if isinstance(player_slot, bool) or not isinstance(player_slot, int):
        return None
    if 0 <= player_slot <= 4:
        return "radiant"
    if 128 <= player_slot <= 132:
        return "dire"
    return None


def _match_event_rows(
    connection: sqlite3.Connection,
    match_id: int,
    odds_timeline: list[dict[str, Any]],
    map_number: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    gold = _curve_rows(connection, "gold_advantage", match_id)
    xp = _curve_rows(connection, "xp_advantage", match_id)
    events: list[dict[str, Any]] = []
    minutes = sorted({row["minute"] for row in gold} | {row["minute"] for row in xp})
    for minute in minutes:
        events.append(
            _event_row(
                game_time_seconds=minute * 60,
                event_type="economy",
                side=None,
                label="economy_snapshot",
                details={"minute": minute},
                gold=gold,
                xp=xp,
                odds_timeline=odds_timeline,
                map_number=map_number,
            )
        )

    objective_columns = _relation_columns(connection, "objectives")
    objective_rows: list[sqlite3.Row] = []
    if {"match_id", "time", "type"}.issubset(objective_columns):
        selected = [
            name if name in objective_columns else f"NULL AS {name}"
            for name in ("time", "type", "unit", "key", "player_slot")
        ]
        objective_order = "time, id" if "id" in objective_columns else "time"
        objective_rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM objectives "
            f"WHERE match_id=? ORDER BY {objective_order}",
            (match_id,),
        ).fetchall()
    for row in objective_rows:
        time_value = _finite_number(row["time"])
        if time_value is None or time_value < 0:
            continue
        details = {
            key: row[key]
            for key in ("type", "unit", "key", "player_slot")
            if row[key] is not None
        }
        events.append(
            _event_row(
                game_time_seconds=int(time_value),
                event_type="objective",
                side=_player_side(row["player_slot"]),
                label=str(row["type"] or "objective"),
                details=details,
                gold=gold,
                xp=xp,
                odds_timeline=odds_timeline,
                map_number=map_number,
            )
        )

    teamfight_columns = _relation_columns(connection, "teamfights")
    teamfight_player_columns = _relation_columns(connection, "teamfight_players")
    teamfight_rows: list[sqlite3.Row] = []
    teamfights_available = {"id", "match_id", "start_time"}.issubset(
        teamfight_columns
    )
    if teamfights_available:
        selected = [
            name if name in teamfight_columns else f"NULL AS {name}"
            for name in ("id", "start_time", "end_time", "last_death", "deaths")
        ]
        teamfight_rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM teamfights "
            "WHERE match_id=? ORDER BY start_time, id",
            (match_id,),
        ).fetchall()
    teamfight_players_available = {
        "teamfight_id",
        "player_slot",
    }.issubset(teamfight_player_columns)
    for row in teamfight_rows:
        start = _finite_number(row["start_time"])
        if start is None or start < 0:
            continue
        players: list[dict[str, Any]] = []
        if teamfight_players_available:
            metrics = (
                "player_slot",
                "deaths",
                "buybacks",
                "damage",
                "healing",
                "gold_delta",
                "xp_delta",
                "kills",
            )
            selected = [
                name if name in teamfight_player_columns else f"NULL AS {name}"
                for name in metrics
            ]
            for player in connection.execute(
                f"SELECT {', '.join(selected)} FROM teamfight_players "
                "WHERE teamfight_id=? ORDER BY player_slot",
                (row["id"],),
            ).fetchall():
                payload = {key: player[key] for key in metrics}
                payload["side"] = _player_side(player["player_slot"])
                players.append(payload)
        events.append(
            _event_row(
                game_time_seconds=int(start),
                event_type="teamfight",
                side=None,
                label="teamfight",
                details={
                    "end_time": row["end_time"],
                    "last_death": row["last_death"],
                    "deaths": row["deaths"],
                    "players": players,
                },
                gold=gold,
                xp=xp,
                odds_timeline=odds_timeline,
                map_number=map_number,
            )
        )

    fact_columns = _relation_columns(connection, "player_map_facts")
    latest_facts: list[sqlite3.Row] = []
    if {"match_id", "player_slot", "facts_json"}.issubset(fact_columns):
        order = []
        if "created_at" in fact_columns:
            order.append("created_at DESC")
        if "fact_id" in fact_columns:
            order.append("fact_id DESC")
        if not order:
            order.append("rowid DESC")
        optional = [
            name if name in fact_columns else f"NULL AS {name}"
            for name in ("account_id", "team_id")
        ]
        latest_facts = connection.execute(
            f"""SELECT player_slot, {', '.join(optional)}, facts_json
                  FROM (
                       SELECT player_slot, {', '.join(optional)}, facts_json,
                              ROW_NUMBER() OVER (
                                  PARTITION BY player_slot
                                  ORDER BY {', '.join(order)}
                              ) AS fact_rank
                         FROM player_map_facts WHERE match_id=?
                  )
                 WHERE fact_rank=1 ORDER BY player_slot""",
            (match_id,),
        ).fetchall()
    buyback_logs_complete = len(latest_facts) == 10
    for row in latest_facts:
        try:
            facts = json.loads(row["facts_json"])
        except (TypeError, json.JSONDecodeError):
            buyback_logs_complete = False
            continue
        logs = facts.get("buyback_log") if isinstance(facts, dict) else None
        if not isinstance(logs, list):
            buyback_logs_complete = False
            continue
        for log in logs:
            if not isinstance(log, dict):
                buyback_logs_complete = False
                continue
            time_value = _finite_number(log.get("time"))
            if time_value is None or time_value < 0:
                buyback_logs_complete = False
                continue
            events.append(
                _event_row(
                    game_time_seconds=int(time_value),
                    event_type="buyback",
                    side=_player_side(row["player_slot"]),
                    label="buyback",
                    details={
                        "player_slot": row["player_slot"],
                        "account_id": row["account_id"],
                        "team_id": row["team_id"],
                    },
                    gold=gold,
                    xp=xp,
                    odds_timeline=odds_timeline,
                    map_number=map_number,
                )
            )

    missing: set[str] = set()
    ingest_columns = _relation_columns(connection, "match_ingest_status")
    if {"match_id", "missing_fields_json"}.issubset(ingest_columns):
        ingest = connection.execute(
            "SELECT missing_fields_json FROM match_ingest_status WHERE match_id=?",
            (match_id,),
        ).fetchone()
        parsed = _json_value(ingest[0] if ingest else None, [])
        if isinstance(parsed, list):
            missing = {str(value) for value in parsed}
    duration: int | None = None
    match_columns = _relation_columns(connection, "matches")
    if {"match_id", "duration"}.issubset(match_columns):
        match_row = connection.execute(
            "SELECT duration FROM matches WHERE match_id=?", (match_id,)
        ).fetchone()
        if (
            match_row is not None
            and not isinstance(match_row[0], bool)
            and isinstance(match_row[0], int)
        ):
            duration = match_row[0]
    availability = {
        "gold_advantage": (
            _timeline_complete(gold, duration)
            and "gold_timeline_incomplete" not in missing
        ),
        "xp_advantage": _timeline_complete(xp, duration),
        "objectives": (
            bool(objective_rows) and "objectives_incomplete" not in missing
        ),
        "teamfights": (
            teamfights_available and "teamfights_missing" not in missing
        ),
        "buybacks": (
            buyback_logs_complete and "buyback_logs_missing" not in missing
        ),
        "odds_game_clock_alignment": any(
            isinstance(point.get("game_clock_seconds"), int)
            and not isinstance(point.get("game_clock_seconds"), bool)
            and point.get("map_number") == map_number
            for point in odds_timeline
        ),
        "missing_reasons": sorted(missing),
    }
    event_order = {"economy": 0, "objective": 1, "teamfight": 2, "buyback": 3}
    events.sort(
        key=lambda row: (
            row["game_time_seconds"],
            event_order.get(str(row["event_type"]), 9),
            str(row["label"]),
        )
    )
    return events, availability


def get_raybet_postmatch(
    raybet_match_id: str,
    map_number: int,
    *,
    max_points: int = 1200,
) -> dict[str, Any] | None:
    """Return a strict, confirmed RayBet-map to OpenDota attribution."""
    clean_match_id = str(raybet_match_id).strip()
    read_cutoff = datetime.now(timezone.utc)
    with _database() as connection:
        raybet_columns = _relation_columns(connection, "raybet_matches")
        if not {
            "raybet_match_id",
            "team_one",
            "team_two",
            "raw_json",
        }.issubset(raybet_columns):
            return {
                **_postmatch_base(
                    clean_match_id,
                    map_number,
                    [],
                    checked_at=read_cutoff,
                ),
                "reason": "raybet_match_schema_unavailable",
            }
        match_row = connection.execute(
            """SELECT raybet_match_id, team_one, team_two, raw_json
                 FROM raybet_matches WHERE raybet_match_id=?""",
            (clean_match_id,),
        ).fetchone()
        if match_row is None or not is_head_to_head_match_row(match_row):
            return None
        full_odds = winner_timeline(
            connection,
            clean_match_id,
            max_points=None,
            period=f"map_{map_number}",
        )
        odds = _downsample_timeline(full_odds, max_points)
        response = _postmatch_base(
            clean_match_id,
            map_number,
            odds,
            checked_at=read_cutoff,
        )
        reconciliation_columns = _relation_columns(
            connection, "settlement_reconciliations"
        )
        required_reconciliation = {
            "raybet_match_id",
            "map_number",
            "strict_mapping_id",
            "dota_match_id",
            "raybet_winner_side",
            "opendota_winner_side",
            "raybet_evidence_ref",
            "opendota_evidence_ref",
            "status",
            "reason",
            "first_observed_at",
            "updated_at",
        }
        if not required_reconciliation.issubset(reconciliation_columns):
            response["reason"] = "reconciliation_schema_unavailable"
            return response
        reconciliation = connection.execute(
            """SELECT * FROM settlement_reconciliations
                WHERE raybet_match_id=? AND map_number=?""",
            (clean_match_id, map_number),
        ).fetchone()
        if reconciliation is None:
            eligibility = query_strict_live_eligibility(
                connection,
                raybet_match_id=clean_match_id,
                map_number=map_number,
                transport_observed_at=read_cutoff,
            )
            if eligibility.mapping is not None:
                response["mapping"] = _mapping_payload(eligibility.mapping)
            if not eligibility.eligible or eligibility.mapping is None:
                response["reason"] = eligibility.reason
                if eligibility.reason not in {
                    "accepted_mapping_missing",
                    "mapping_invalidated",
                    "strict_mapping_schema_missing",
                    "raybet_metadata_missing",
                    "canonical_team_missing",
                }:
                    response["status"] = "review"
            elif not has_trusted_confirmed_draft(
                connection,
                clean_match_id,
                map_number,
            ):
                response["reason"] = "waiting_for_confirmed_draft"
            return response
        response["reconciliation"] = _reconciliation_payload(reconciliation)
        strict_mapping_id = reconciliation["strict_mapping_id"]
        if type(strict_mapping_id) is not int or strict_mapping_id <= 0:
            response["status"] = "review"
            response["reason"] = "reconciliation_mapping_authority_missing"
            return response
        first_observed_at = _aware_timestamp(reconciliation["first_observed_at"])
        updated_at = _aware_timestamp(reconciliation["updated_at"])
        if (
            first_observed_at is None
            or updated_at is None
            or updated_at < first_observed_at
            or first_observed_at > read_cutoff
            or updated_at > read_cutoff
        ):
            response["status"] = "review"
            response["reason"] = "reconciliation_causal_order_invalid"
            return response
        historical_eligibility = query_strict_mapping_snapshot(
            connection,
            mapping_id=strict_mapping_id,
            observed_at=first_observed_at,
        )
        historical_mapping = historical_eligibility.mapping
        if (
            not historical_eligibility.eligible
            or historical_mapping is None
            or historical_mapping.mapping_id != strict_mapping_id
            or historical_mapping.raybet_match_id != clean_match_id
            or historical_mapping.map_number != map_number
        ):
            response["status"] = "review"
            response["reason"] = "reconciliation_mapping_lineage_unverified"
            return response
        response["mapping"] = _mapping_payload(historical_mapping)
        current_eligibility = query_strict_live_eligibility(
            connection,
            raybet_match_id=clean_match_id,
            map_number=map_number,
            transport_observed_at=read_cutoff,
        )
        if not current_eligibility.eligible or current_eligibility.mapping is None:
            response["warnings"].append(current_eligibility.reason)
        elif current_eligibility.mapping.mapping_id != strict_mapping_id:
            response["warnings"].append("current_mapping_changed")
        if reconciliation["status"] != "confirmed":
            response["status"] = "review"
            response["reason"] = (
                "reconciliation_pending"
                if reconciliation["status"] == "pending"
                else "reconciliation_review_required"
            )
            return response
        if (
            isinstance(reconciliation["dota_match_id"], bool)
            or not isinstance(reconciliation["dota_match_id"], int)
            or reconciliation["dota_match_id"] <= 0
        ):
            response["status"] = "review"
            response["reason"] = "opendota_match_identity_invalid"
            return response

        if (
            first_observed_at < historical_mapping.recorded_at
        ):
            response["status"] = "review"
            response["reason"] = "reconciliation_causal_order_invalid"
            return response
        evidence_reason = _confirmed_evidence_reason(
            connection,
            reconciliation,
            raybet_match_id=clean_match_id,
            map_number=map_number,
            mapping_recorded_at=historical_mapping.recorded_at,
            reconciliation_first_observed_at=first_observed_at,
            reconciliation_updated_at=updated_at,
            read_cutoff=read_cutoff,
        )
        if evidence_reason is not None:
            response["status"] = (
                "unavailable"
                if evidence_reason == "settlement_evidence_schema_unavailable"
                else "review"
            )
            response["reason"] = evidence_reason
            return response
        dota_match_id = int(reconciliation["dota_match_id"])
        map_result_reason = _confirmed_map_result_reason(
            connection,
            reconciliation,
            raybet_match_id=clean_match_id,
            map_number=map_number,
            strict_mapping_id=strict_mapping_id,
            mapping_recorded_at=historical_mapping.recorded_at,
            reconciliation_first_observed_at=first_observed_at,
            reconciliation_updated_at=updated_at,
            read_cutoff=read_cutoff,
        )
        if map_result_reason is not None:
            response["status"] = (
                "unavailable"
                if map_result_reason in {
                    "map_result_missing",
                    "map_result_schema_unavailable",
                }
                else "review"
            )
            response["reason"] = map_result_reason
            return response
        duplicate_reason = _duplicate_link_reason(
            connection,
            raybet_match_id=clean_match_id,
            map_number=map_number,
            dota_match_id=dota_match_id,
        )
        if duplicate_reason is not None:
            response["status"] = "review"
            response["reason"] = duplicate_reason
            return response

        scopes = _targeted_scopes(connection, dota_match_id)
        identity_status, identity_reason = _opendota_identity_reason(
            connection,
            historical_mapping,
            reconciliation,
            map_number=map_number,
            scopes=scopes,
        )
        if identity_reason is not None:
            response["status"] = str(identity_status)
            response["reason"] = identity_reason
            return response
        detail = _match_detail_payload(connection, dota_match_id, scopes)
        if detail is None:
            response["reason"] = "opendota_match_unavailable"
            return response
        events, event_availability = _match_event_rows(
            connection, dota_match_id, full_odds, map_number
        )
        response.update(
            {
                "status": "available",
                "reason": "confirmed_exact_link",
                "postmatch": {
                    **detail,
                    "events": events,
                    "event_availability": event_availability,
                },
            }
        )
        return response


def _player_identities(connection: sqlite3.Connection) -> dict[int, str]:
    fact_columns = _relation_columns(connection, "player_map_facts")
    if not {"account_id", "facts_json"}.issubset(fact_columns):
        return {}
    order_columns = []
    if "created_at" in fact_columns:
        order_columns.append("created_at DESC")
    if "fact_id" in fact_columns:
        order_columns.append("fact_id DESC")
    if not order_columns:
        order_columns.append("rowid DESC")
    rows = connection.execute(
        f"""SELECT account_id,
                  COALESCE(json_extract(facts_json, '$.name'),
                           json_extract(facts_json, '$.personaname')) AS player_name
                 FROM (
                       SELECT account_id, facts_json,
                              ROW_NUMBER() OVER (
                                  PARTITION BY account_id
                                  ORDER BY {', '.join(order_columns)}
                              ) AS identity_rank
                         FROM player_map_facts
                        WHERE account_id IS NOT NULL AND json_valid(facts_json)
                  )
            WHERE identity_rank=1"""
    ).fetchall()
    identities: dict[int, str] = {}
    for row in rows:
        if not row["player_name"]:
            continue
        try:
            account_id = int(row["account_id"])
        except (TypeError, ValueError):
            continue
        identities[account_id] = str(row["player_name"])
    return identities


def list_players(
    *,
    page: int,
    page_size: int,
    position: int | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    with _database() as connection:
        score_columns = _relation_columns(connection, "player_map_scores")
        required_score_columns = {
            "match_id",
            "account_id",
            "position",
            "execution_score",
            "result_adjusted_score",
            "coverage",
            "role_confidence",
            "score_version",
            "explanation_json",
        }
        if not required_score_columns.issubset(score_columns):
            return {"data": [], "pagination": _pagination(page, page_size, 0)}
        scopes = _current_scopes(connection)
        scope_join, scope_params = _scope_join(scopes, "score", "player")
        where = [
            "score.score_version=?",
            "score.account_id IS NOT NULL",
            "score.position BETWEEN 1 AND 5",
            "score.role_confidence>=0.7",
            "json_valid(score.explanation_json)",
            "json_extract(score.explanation_json, '$.ranking_eligible')=1",
        ]
        params: list[Any] = [*scope_params, SCORE_VERSION]
        if position is not None:
            where.append("score.position=?")
            params.append(position)
        benchmark_column = (
            "score.benchmark_cutoff AS benchmark_cutoff"
            if "benchmark_cutoff" in score_columns
            else "NULL AS benchmark_cutoff"
        )
        rows = connection.execute(
            f"""SELECT score.account_id, score.position,
                       score.execution_score, score.result_adjusted_score,
                       score.coverage, score.role_confidence,
                       {benchmark_column}
                  FROM player_map_scores AS score {scope_join}
                 WHERE {' AND '.join(where)}""",
            tuple(params),
        ).fetchall()
        identities = _player_identities(connection)

    aggregates: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        if any(
            row[field] is None
            for field in (
                "account_id",
                "position",
                "execution_score",
                "result_adjusted_score",
                "coverage",
                "role_confidence",
            )
        ):
            # A legacy row with incomplete numeric facts may still be shown in
            # match detail, but it cannot enter a numeric leaderboard average.
            continue
        try:
            account_id = int(row["account_id"])
            player_position = int(row["position"])
        except (TypeError, ValueError):
            continue
        aggregate = aggregates.setdefault(
            (account_id, player_position),
            {
                "account_id": account_id,
                "position": player_position,
                "map_count": 0,
                "execution_total": 0.0,
                "result_total": 0.0,
                "coverage_total": 0.0,
                "confidence_total": 0.0,
                "benchmark_cutoffs": set(),
            },
        )
        aggregate["map_count"] += 1
        aggregate["execution_total"] += float(row["execution_score"])
        aggregate["result_total"] += float(row["result_adjusted_score"])
        aggregate["coverage_total"] += float(row["coverage"])
        aggregate["confidence_total"] += float(row["role_confidence"])
        if row["benchmark_cutoff"] is not None:
            aggregate["benchmark_cutoffs"].add(str(row["benchmark_cutoff"]))

    leaderboard = []
    for aggregate in aggregates.values():
        maps = aggregate["map_count"]
        leaderboard.append(
            {
                "account_id": aggregate["account_id"],
                "player_name": identities.get(aggregate["account_id"]),
                "position": aggregate["position"],
                "map_count": maps,
                "average_execution_score": round(
                    aggregate["execution_total"] / maps, 4
                ),
                "average_result_adjusted_score": round(
                    aggregate["result_total"] / maps, 4
                ),
                "average_coverage": round(aggregate["coverage_total"] / maps, 6),
                "average_role_confidence": round(
                    aggregate["confidence_total"] / maps, 6
                ),
                "benchmark_cutoffs": sorted(aggregate["benchmark_cutoffs"]),
                "benchmark_cutoff_min": (
                    min(aggregate["benchmark_cutoffs"])
                    if aggregate["benchmark_cutoffs"]
                    else None
                ),
                "benchmark_cutoff_max": (
                    max(aggregate["benchmark_cutoffs"])
                    if aggregate["benchmark_cutoffs"]
                    else None
                ),
                "score_version": SCORE_VERSION,
            }
        )
    leaderboard.sort(
        key=lambda row: (
            -row["average_execution_score"],
            -row["map_count"],
            row["account_id"],
        )
    )
    for rank, row in enumerate(leaderboard, start=1):
        row["rank"] = rank

    normalized_search = search.strip().casefold() if search and search.strip() else None
    if normalized_search:
        leaderboard = [
            row
            for row in leaderboard
            if normalized_search in str(row["account_id"]).casefold()
            or normalized_search in (row["player_name"] or "").casefold()
        ]
    total = len(leaderboard)
    start = (page - 1) * page_size
    return {
        "data": leaderboard[start : start + page_size],
        "pagination": _pagination(page, page_size, total),
    }


def _compact_profile_json(field: str, value: Any) -> Any:
    parsed = _json_value(value, [] if field != "weighting" else {})
    if field == "posterior_rates" and isinstance(parsed, list):
        return [
            {
                key: item
                for key, item in row.items()
                if not key.endswith("evidence")
            }
            if isinstance(row, dict)
            else row
            for row in parsed
        ]
    if field == "weighting" and isinstance(parsed, dict):
        maps = parsed.get("maps")
        if isinstance(maps, list):
            return {
                **{key: item for key, item in parsed.items() if key != "maps"},
                "map_count": len(maps),
                "total_weight": math.fsum(
                    float(row.get("total_weight", 0.0))
                    for row in maps
                    if isinstance(row, dict)
                ),
            }
    return parsed


def list_teams() -> dict[str, Any]:
    with _database() as connection:
        if not _relation_exists(connection, "team_style_profiles"):
            return {"data": []}
        scopes = _current_scopes(connection)
        profiles = _current_profile_rows(connection, scopes)
        if not profiles:
            return {"data": []}

        team_columns = _relation_columns(connection, "teams")
        has_teams = "team_id" in team_columns
        teams: dict[int, dict[str, Any]] = {}
        if has_teams:
            team_ids = sorted({int(row["team_id"]) for row in profiles})
            placeholders = ",".join("?" for _ in team_ids)
            selected_team_columns = ", ".join(
                f"{name} AS {name}" if name in team_columns else f"NULL AS {name}"
                for name in ("team_id", "name", "tag", "logo_url")
            )
            for row in connection.execute(
                f"""SELECT {selected_team_columns} FROM teams
                      WHERE team_id IN ({placeholders})""",
                tuple(team_ids),
            ).fetchall():
                teams[int(row["team_id"])] = dict(row)

        state_counts: dict[int, dict[str, int]] = {}
        state_columns = _relation_columns(connection, "team_map_states")
        if {"match_id", "team_id", "label", "label_version"}.issubset(
            state_columns
        ):
            state_join, state_scope_params = _scope_join(
                scopes, "state", "state"
            )
            for row in connection.execute(
                f"""SELECT state.team_id, state.label,
                           COUNT(DISTINCT state.match_id) AS count
                      FROM team_map_states AS state {state_join}
                     WHERE state.label_version=? AND state.team_id IS NOT NULL
                     GROUP BY state.team_id, state.label""",
                (*state_scope_params, LABEL_VERSION),
            ).fetchall():
                state_counts.setdefault(int(row["team_id"]), {})[
                    str(row["label"])
                ] = int(row["count"])

        data = []
        for row in profiles:
            profile = dict(row)
            team_id = int(profile["team_id"])
            team = teams.get(team_id, {})
            data.append(
                {
                    "team_id": team_id,
                    "team_name": team.get("name"),
                    "team_tag": team.get("tag"),
                    "logo_url": team.get("logo_url"),
                    "profile_cutoff": profile["profile_cutoff"],
                    "profile_version": profile["profile_version"],
                    "opportunity_counts": _compact_profile_json(
                        "opportunity_counts", profile.get("opportunity_counts_json")
                    ),
                    "posterior_rates": _compact_profile_json(
                        "posterior_rates", profile.get("posterior_rates_json")
                    ),
                    "duration_quantiles": _compact_profile_json(
                        "duration_quantiles", profile.get("duration_quantiles_json")
                    ),
                    "weighting": _compact_profile_json(
                        "weighting", profile.get("weighting_json")
                    ),
                    "effective_sample_size": profile.get("effective_sample_size"),
                    "created_at": profile.get("created_at"),
                    "state_counts": state_counts.get(team_id, {}),
                }
            )
        data.sort(key=lambda row: (row["team_name"] or str(row["team_id"])))
        return {"data": data}


__all__ = [
    "MATCH_RATING_ROUNDING",
    "MATCH_RATING_VERSION",
    "get_match",
    "get_overview",
    "get_raybet_postmatch",
    "list_matches",
    "list_players",
    "list_teams",
]
