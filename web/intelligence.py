"""Read-only delivery queries for strict historical intelligence."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from event_intelligence.backtest import (
    BACKTEST_VERSION,
    EvaluationPoint,
    evaluate_points,
)
from event_intelligence.draft_features import FEATURE_VERSION as DRAFT_FEATURE_VERSION
from event_intelligence.draft_model import MODEL_VERSION as DRAFT_MODEL_VERSION
from event_intelligence.incremental import (
    CurrentDerivedScopes,
    ROLE_VERSION,
    SCORE_VERSION,
    current_derived_scopes,
    current_state_input_hashes,
    profile_weighting_is_current,
)
from event_intelligence.player_scoring import score_version_for_role
from event_intelligence.roles import PROSPECTIVE_ASSIGNMENT_VERSION
from event_intelligence.team_profiles import PROFILE_VERSION
from event_intelligence.team_states import LABEL_VERSION

from . import queries


MODEL_KINDS = ("pure_draft", "context_adjusted")
LANDMARK_MINUTES = (10, 20, 30, 40, 50)
DEFAULT_ECE_BINS = 5
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
    row = connection.execute(
        f"SELECT COUNT({expression}) FROM {relation} {join} {where}",
        (*join_params, *params),
    ).fetchone()
    return int(row[0]) if row else 0


def _current_profile_rows(
    connection: sqlite3.Connection,
    scopes: CurrentDerivedScopes,
) -> list[sqlite3.Row]:
    if (
        not scopes.valid_profile_cutoffs
        or not _relation_exists(connection, "team_style_profiles")
    ):
        return []
    state_hashes = current_state_input_hashes(connection, scopes)
    cutoff_payload = json.dumps(
        sorted(scopes.valid_profile_cutoffs), separators=(",", ":")
    )
    try:
        rows = connection.execute(
            """SELECT profile.* FROM team_style_profiles AS profile
                JOIN json_each(?) AS cutoff
                  ON CAST(cutoff.value AS TEXT)=profile.profile_cutoff
               WHERE profile.profile_version=?
               ORDER BY profile.team_id, profile.profile_cutoff DESC,
                        profile.profile_id DESC""",
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
        if _relation_exists(connection, "team_map_states"):
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
        if not _relation_exists(connection, "matches"):
            return {"data": [], "pagination": _pagination(page, page_size, 0)}

        scopes = _current_scopes(connection)
        scope_join, scope_params = _scope_join(scopes, "m", "formal")
        has_states = _relation_exists(connection, "team_map_states")
        teams_join = _relation_exists(connection, "teams")
        leagues_join = _relation_exists(connection, "leagues")
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
        state_columns = (
            "radiant_state.team_id AS radiant_state_team_id, "
            "radiant_state.label AS radiant_state_label, "
            "radiant_state.duration_seconds AS radiant_state_duration_seconds, "
            "radiant_state.max_lead AS radiant_state_max_lead, "
            "radiant_state.max_deficit AS radiant_state_max_deficit, "
            "radiant_state.curve_coverage AS radiant_state_curve_coverage, "
            "dire_state.team_id AS dire_state_team_id, "
            "dire_state.label AS dire_state_label, "
            "dire_state.duration_seconds AS dire_state_duration_seconds, "
            "dire_state.max_lead AS dire_state_max_lead, "
            "dire_state.max_deficit AS dire_state_max_deficit, "
            "dire_state.curve_coverage AS dire_state_curve_coverage"
            if has_states
            else "NULL AS radiant_state_team_id, NULL AS radiant_state_label, "
            "NULL AS radiant_state_duration_seconds, NULL AS radiant_state_max_lead, "
            "NULL AS radiant_state_max_deficit, NULL AS radiant_state_curve_coverage, "
            "NULL AS dire_state_team_id, NULL AS dire_state_label, "
            "NULL AS dire_state_duration_seconds, NULL AS dire_state_max_lead, "
            "NULL AS dire_state_max_deficit, NULL AS dire_state_curve_coverage"
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
            where.append("(m.radiant_team_id=? OR m.dire_team_id=?)")
            params.extend((team_id, team_id))

        from_sql = "FROM matches AS m " + " ".join(joins)
        where_sql = "WHERE " + " AND ".join(where)
        total = int(
            connection.execute(
                f"SELECT COUNT(*) {from_sql} {where_sql}", tuple(params)
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT m.match_id, m.radiant_team_id, m.dire_team_id,
                       m.radiant_win, m.duration, m.start_time, m.leagueid,
                       m.radiant_score, m.dire_score,
                       {team_columns}, {league_column},
                       {state_columns}
                  {from_sql} {where_sql}
                 ORDER BY m.start_time DESC, m.match_id DESC
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
    if not _relation_exists(connection, "matches"):
        return None
    teams_join = _relation_exists(connection, "teams")
    leagues_join = _relation_exists(connection, "leagues")
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
    team_columns = (
        "radiant_team.name AS radiant_team_name, dire_team.name AS dire_team_name"
        if teams_join
        else "NULL AS radiant_team_name, NULL AS dire_team_name"
    )
    league_column = (
        "league.name AS league_name" if leagues_join else "NULL AS league_name"
    )
    row = connection.execute(
        f"""SELECT m.match_id, m.radiant_team_id, m.dire_team_id,
                   m.radiant_win, m.duration, m.start_time, m.leagueid,
                   m.radiant_score, m.dire_score,
                   {team_columns}, {league_column}
              FROM matches AS m {' '.join(joins)}
             WHERE m.match_id=?""",
        (match_id,),
    ).fetchone()
    if row is None:
        return None
    payload = dict(row)
    if payload["radiant_win"] is not None:
        payload["radiant_win"] = bool(payload["radiant_win"])
    return payload


def _match_states(
    connection: sqlite3.Connection, match_id: int
) -> dict[str, dict[str, Any] | None]:
    result: dict[str, dict[str, Any] | None] = {"radiant": None, "dire": None}
    if not _relation_exists(connection, "team_map_states"):
        return result
    rows = connection.execute(
        """SELECT state_id, match_id, team_id, side, label, duration_seconds,
                  max_lead, max_deficit, ahead_fraction, behind_fraction,
                  even_fraction, signed_auc, absolute_auc, crossings_json,
                  first_significant_lead_at, first_significant_deficit_at,
                  closeout_seconds, objective_conversion_json, curve_coverage,
                  source_versions_json, input_hash, label_version, created_at
             FROM team_map_states
            WHERE match_id=? AND label_version=?
            ORDER BY side""",
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
    if not _relation_exists(connection, "player_map_scores"):
        return []
    performance_by_slot = {
        row["player_slot"]: row
        for row in (
            performance_rows
            if performance_rows is not None
            else _match_performance(connection, match_id)
        )
    }
    rows = connection.execute(
        """SELECT score.player_slot, score.account_id, score.position,
                   score.execution_score,
                   score.result_adjusted_score, score.coverage,
                   score.role_confidence, score.benchmark_cutoff,
                   score.score_version, score.component_facts_json,
                   score.component_scores_json, score.weights_json,
                   score.explanation_json
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
                "execution_score": row["execution_score"],
                "result_adjusted_score": row["result_adjusted_score"],
                "coverage": row["coverage"],
                "role_confidence": row["role_confidence"],
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


def get_match(match_id: int) -> dict[str, Any] | None:
    with _database() as connection:
        scopes = _current_scopes(connection)
        if match_id not in scopes.formal:
            return None
        match = _match_row(connection, match_id)
        if match is None:
            return None
        states = (
            _match_states(connection, match_id)
            if match_id in scopes.state
            else {"radiant": None, "dire": None}
        )
        player_performance = _match_performance(connection, match_id)
        return {
            "match": match,
            "radiant_state": states["radiant"],
            "dire_state": states["dire"],
            "player_performance": player_performance,
            "player_scores": (
                _match_players(connection, match_id, player_performance)
                if match_id in scopes.player
                else []
            ),
            "draft_predictions": (
                _match_draft_predictions(
                    connection, match_id, scopes.draft_predictions
                )
                if match_id in scopes.draft
                else []
            ),
        }


def _player_identities(connection: sqlite3.Connection) -> dict[int, str]:
    if not _relation_exists(connection, "player_map_facts"):
        return {}
    rows = connection.execute(
        """SELECT account_id,
                  COALESCE(json_extract(facts_json, '$.name'),
                           json_extract(facts_json, '$.personaname')) AS player_name
             FROM (
                   SELECT account_id, facts_json,
                          ROW_NUMBER() OVER (
                              PARTITION BY account_id
                              ORDER BY created_at DESC, fact_id DESC
                          ) AS identity_rank
                     FROM player_map_facts
                    WHERE account_id IS NOT NULL AND json_valid(facts_json)
                  )
            WHERE identity_rank=1"""
    ).fetchall()
    return {
        int(row["account_id"]): str(row["player_name"])
        for row in rows
        if row["player_name"]
    }


def list_players(
    *,
    page: int,
    page_size: int,
    position: int | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    with _database() as connection:
        if not _relation_exists(connection, "player_map_scores"):
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
        rows = connection.execute(
            f"""SELECT score.account_id, score.position,
                       score.execution_score, score.result_adjusted_score,
                       score.coverage, score.role_confidence
                  FROM player_map_scores AS score {scope_join}
                 WHERE {' AND '.join(where)}""",
            tuple(params),
        ).fetchall()
        identities = _player_identities(connection)

    aggregates: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        account_id = int(row["account_id"])
        player_position = int(row["position"])
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
            },
        )
        aggregate["map_count"] += 1
        aggregate["execution_total"] += float(row["execution_score"])
        aggregate["result_total"] += float(row["result_adjusted_score"])
        aggregate["coverage_total"] += float(row["coverage"])
        aggregate["confidence_total"] += float(row["role_confidence"])

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

        has_teams = _relation_exists(connection, "teams")
        teams: dict[int, dict[str, Any]] = {}
        if has_teams:
            team_ids = sorted({int(row["team_id"]) for row in profiles})
            placeholders = ",".join("?" for _ in team_ids)
            for row in connection.execute(
                f"""SELECT team_id, name, tag, logo_url FROM teams
                     WHERE team_id IN ({placeholders})""",
                tuple(team_ids),
            ).fetchall():
                teams[int(row["team_id"])] = dict(row)

        state_counts: dict[int, dict[str, int]] = {}
        if _relation_exists(connection, "team_map_states"):
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
            team_id = int(row["team_id"])
            team = teams.get(team_id, {})
            data.append(
                {
                    "team_id": team_id,
                    "team_name": team.get("name"),
                    "team_tag": team.get("tag"),
                    "logo_url": team.get("logo_url"),
                    "profile_cutoff": row["profile_cutoff"],
                    "profile_version": row["profile_version"],
                    "opportunity_counts": _compact_profile_json(
                        "opportunity_counts", row["opportunity_counts_json"]
                    ),
                    "posterior_rates": _compact_profile_json(
                        "posterior_rates", row["posterior_rates_json"]
                    ),
                    "duration_quantiles": _compact_profile_json(
                        "duration_quantiles", row["duration_quantiles_json"]
                    ),
                    "weighting": _compact_profile_json(
                        "weighting", row["weighting_json"]
                    ),
                    "effective_sample_size": row["effective_sample_size"],
                    "created_at": row["created_at"],
                    "state_counts": state_counts.get(team_id, {}),
                }
            )
        data.sort(key=lambda row: (row["team_name"] or str(row["team_id"])))
        return {"data": data}


__all__ = [
    "get_match",
    "get_overview",
    "list_matches",
    "list_players",
    "list_teams",
]
