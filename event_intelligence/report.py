"""Strict-scope data coverage report helpers."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import Counter
from typing import Any, Collection

from database.session import DatabaseRow, PostgresSession

from .backtest import (
    BACKTEST_VERSION,
    CalibrationMetrics,
    EvaluationPoint,
    evaluate_points,
)
from .draft_features import FEATURE_VERSION as DRAFT_FEATURE_VERSION
from .draft_model import MODEL_VERSION as DRAFT_MODEL_VERSION
from .incremental import (
    ROLE_VERSION,
    SCORE_VERSION as CURRENT_SCORE_VERSION,
    CurrentDerivedScopes,
    current_derived_scopes,
    current_state_input_hashes,
    profile_weighting_is_current,
)
from .player_scoring import SCORE_VERSION as SCORE_FAMILY_VERSION
from .team_profiles import PROFILE_VERSION
from .team_states import LABEL_VERSION


_DRAFT_METRICS_LOCK = threading.Lock()
_DRAFT_METRICS_CACHE: tuple[str, tuple[dict[str, Any], ...]] | None = None


def build_intelligence_report(connection: PostgresSession) -> dict[str, Any]:
    """Summarize strict coverage without combining reconstructed/prospective rows."""
    if connection.in_transaction:
        return _build_intelligence_report(connection)
    with connection.transaction():
        return _build_intelligence_report(connection)


def _build_intelligence_report(connection: PostgresSession) -> dict[str, Any]:
    scopes = current_derived_scopes(connection)
    score_versions = _group(connection, "player_map_scores", "score_version")
    draft_rows = _draft_rows(connection, scopes.draft_predictions)
    draft_score_versions = dict(Counter(row["score_version"] for row in draft_rows))
    current_family_draft = [
        row for row in draft_rows if _is_score_family(row["score_version"])
    ]
    draft_modes = dict(
        Counter(row["availability_mode"] for row in current_family_draft)
    )
    draft_statuses = dict(
        Counter(row["prediction_status"] for row in current_family_draft)
    )
    profiles = _current_team_profiles(connection, scopes)
    return {
        "versions": {
            "role_assignment": ROLE_VERSION,
            "player_score": CURRENT_SCORE_VERSION,
            "team_state": LABEL_VERSION,
            "team_profile": PROFILE_VERSION,
            "draft_model": DRAFT_MODEL_VERSION,
            "draft_backtest": BACKTEST_VERSION,
            "draft_features": DRAFT_FEATURE_VERSION,
            "draft_score_family": SCORE_FAMILY_VERSION,
        },
        "formal_maps": _count(connection, "formal_map_eligibility"),
        "player_facts": _count(connection, "player_map_facts"),
        "player_scores": sum(
            count
            for version, count in score_versions.items()
            if version == SCORE_FAMILY_VERSION
            or version.startswith(f"{SCORE_FAMILY_VERSION}+")
        ),
        "player_score_rows": sum(score_versions.values()),
        "player_scores_by_version": score_versions,
        "player_rankings": _player_rankings(connection, scopes.player),
        "team_profiles": _count(connection, "team_style_profiles"),
        "team_style_profiles": profiles,
        "team_state_distribution": _team_state_distribution(
            connection, scopes.state
        ),
        "draft_predictions": sum(draft_modes.values()),
        "draft_prediction_rows": sum(draft_score_versions.values()),
        "draft_predictions_by_score_version": draft_score_versions,
        "draft_predictions_by_mode": draft_modes,
        "draft_predictions_by_status": draft_statuses,
        "draft_metrics": _draft_metrics(current_family_draft),
        "role_assignments_by_purpose": _group(
            connection, "player_role_assignments", "purpose"
        ),
        "strict_event_count": _count(connection, "event_registry"),
    }


def _count(connection: PostgresSession, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _group(connection: PostgresSession, table: str, column: str) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _scope_payload(match_ids: Collection[object]) -> str:
    return json.dumps(sorted(match_ids), separators=(",", ":"))


def _draft_rows(
    connection: PostgresSession,
    prediction_keys: Collection[tuple[str, int]],
) -> list[dict[str, Any]]:
    if not prediction_keys:
        return []
    rows = connection.execute(
            """SELECT run.run_id,
                      run.configuration_json::jsonb ->> 'score_version',
                      run.model_version,
                      run.configuration_json::jsonb ->> 'backtest_version',
                      run.configuration_json::jsonb ->> 'feature_version',
                      run.model_kind, run.horizon_minutes,
                      run.availability_mode, run.status, prediction.status,
                      prediction.probability, prediction.eventual_radiant_win,
                      prediction.match_id, status.series_id, status.event_id,
                      prediction.input_snapshot_hash
                 FROM draft_predictions AS prediction
                 JOIN draft_model_runs AS run ON run.run_id=prediction.run_id
                 JOIN match_ingest_status AS status
                   ON status.match_id=prediction.match_id
                ORDER BY 2, 3, 4, 5, 6, 7, 8, prediction.match_id,
                         run.run_id"""
        ).fetchall()
    allowed = set(prediction_keys)
    return [
        {
            "run_id": str(row[0]),
            "score_version": str(row[1]),
            "model_version": str(row[2]),
            "backtest_version": str(row[3]),
            "feature_version": str(row[4]),
            "model_kind": str(row[5]),
            "horizon_minutes": int(row[6]),
            "availability_mode": str(row[7]),
            "run_status": str(row[8]),
            "prediction_status": str(row[9]),
            "probability": row[10],
            "eventual_radiant_win": row[11],
            "match_id": int(row[12]),
            "series_id": None if row[13] is None else int(row[13]),
            "event_id": str(row[14]),
            "input_snapshot_hash": str(row[15]),
        }
        for row in rows
        if (str(row[0]), int(row[12])) in allowed
    ]


def _is_score_family(value: object) -> bool:
    version = str(value)
    return version == SCORE_FAMILY_VERSION or version.startswith(
        f"{SCORE_FAMILY_VERSION}+"
    )


def _player_rankings(
    connection: PostgresSession,
    match_ids: Collection[int],
) -> list[dict[str, Any]]:
    if not match_ids:
        return []
    rows = connection.execute(
            """SELECT account_id, position, COUNT(*) AS maps,
                      AVG(execution_score) AS execution_score,
                      AVG(result_adjusted_score) AS result_adjusted_score,
                      AVG(coverage) AS coverage,
                      AVG(role_confidence) AS role_confidence
                 FROM player_map_scores AS score
                 JOIN jsonb_array_elements_text(CAST(? AS jsonb)) AS current_scope(value)
                   ON CAST(current_scope.value AS BIGINT)=score.match_id
                WHERE score_version=? AND account_id IS NOT NULL
                  AND explanation_json::jsonb ->> 'ranking_eligible'='true'
                GROUP BY account_id, position
                ORDER BY execution_score DESC, maps DESC, account_id, position""",
            (_scope_payload(match_ids), CURRENT_SCORE_VERSION),
        ).fetchall()
    return [
        {
            "account_id": int(row[0]),
            "position": None if row[1] is None else int(row[1]),
            "maps": int(row[2]),
            "average_execution_score": float(row[3]),
            "average_result_adjusted_score": float(row[4]),
            "average_coverage": float(row[5]),
            "average_role_confidence": float(row[6]),
            "score_version": CURRENT_SCORE_VERSION,
        }
        for row in rows
    ]


def _json_payload(value: object, default: Any) -> Any:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _profile_payload(field: str, value: object, default: Any) -> Any:
    payload = _json_payload(value, default)
    if field in {"posterior_rates", "duration_quantiles"} and isinstance(
        payload, list
    ):
        return [
            {
                key: item
                for key, item in row.items()
                if not str(key).endswith("evidence")
            }
            if isinstance(row, dict)
            else row
            for row in payload
        ]
    if field == "weighting" and isinstance(payload, dict):
        maps = payload.get("maps")
        if isinstance(maps, list):
            return {
                **{key: item for key, item in payload.items() if key != "maps"},
                "map_count": len(maps),
                "total_weight": math.fsum(
                    float(row.get("total_weight", 0.0))
                    for row in maps
                    if isinstance(row, dict)
                ),
            }
    return payload


def _current_team_profiles(
    connection: PostgresSession,
    scopes: CurrentDerivedScopes,
) -> list[dict[str, Any]]:
    if not scopes.valid_profile_cutoffs:
        return []
    state_hashes = current_state_input_hashes(connection, scopes)
    rows = connection.execute(
            """SELECT profile.team_id, profile.profile_cutoff,
                      profile.profile_version,
                      profile.opportunity_counts_json,
                      profile.posterior_rates_json,
                      profile.duration_quantiles_json,
                      profile.weighting_json,
                      profile.effective_sample_size,
                      profile.input_hash, profile.created_at
                 FROM team_style_profiles AS profile
                 JOIN jsonb_array_elements_text(CAST(? AS jsonb)) AS current_cutoff(value)
                   ON current_cutoff.value=profile.profile_cutoff
                WHERE profile.profile_version=?
                ORDER BY profile.team_id, profile.profile_cutoff DESC,
                         profile.profile_id DESC""",
            (_scope_payload(scopes.valid_profile_cutoffs), PROFILE_VERSION),
        ).fetchall()
    selected: dict[int, DatabaseRow] = {}
    for row in rows:
        team_id = int(row[0])
        if team_id in selected:
            continue
        if profile_weighting_is_current(row[6], state_hashes):
            selected[team_id] = row
    return [
        {
            "team_id": int(row[0]),
            "profile_cutoff": str(row[1]),
            "profile_version": str(row[2]),
            "opportunity_counts": _profile_payload(
                "opportunity_counts", row[3], {}
            ),
            "posterior_rates": _profile_payload("posterior_rates", row[4], []),
            "duration_quantiles": _profile_payload(
                "duration_quantiles", row[5], []
            ),
            "weighting": _profile_payload("weighting", row[6], {}),
            "effective_sample_size": float(row[7]),
            "input_hash": str(row[8]),
            "created_at": str(row[9]),
        }
        for row in selected.values()
    ]


def _team_state_distribution(
    connection: PostgresSession,
    match_ids: Collection[int],
) -> dict[str, int]:
    if not match_ids:
        return {}
    rows = connection.execute(
            """SELECT label, COUNT(*)
                 FROM team_map_states AS state
                 JOIN jsonb_array_elements_text(CAST(? AS jsonb)) AS current_scope(value)
                   ON CAST(current_scope.value AS BIGINT)=state.match_id
                WHERE label_version=?
                GROUP BY label ORDER BY label""",
            (_scope_payload(match_ids), LABEL_VERSION),
        ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _draft_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    global _DRAFT_METRICS_CACHE
    fingerprint = hashlib.sha256(
        json.dumps(
            {"backtest_version": BACKTEST_VERSION, "rows": rows},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    with _DRAFT_METRICS_LOCK:
        if (
            _DRAFT_METRICS_CACHE is not None
            and _DRAFT_METRICS_CACHE[0] == fingerprint
        ):
            return _clone_draft_metrics(_DRAFT_METRICS_CACHE[1])

    grouped: dict[
        tuple[str, str, str, str, str, int, str], dict[str, Any]
    ] = {}
    for row in rows:
        key = (
            str(row["score_version"]),
            str(row["model_version"]),
            str(row["backtest_version"]),
            str(row["feature_version"]),
            str(row["model_kind"]),
            int(row["horizon_minutes"]),
            str(row["availability_mode"]),
        )
        value = grouped.setdefault(
            key,
            {
                "run_statuses": Counter(),
                "prediction_statuses": Counter(),
                "prediction_rows": 0,
                "points": [],
            },
        )
        value["run_statuses"][str(row["run_status"])] += 1
        value["prediction_statuses"][str(row["prediction_status"])] += 1
        value["prediction_rows"] += 1
        if (
            row["prediction_status"] == "settled"
            and row["probability"] is not None
            and row["eventual_radiant_win"] is not None
        ):
            value["points"].append(
                EvaluationPoint(
                    match_id=int(row["match_id"]),
                    series_id=row["series_id"],
                    event_id=str(row["event_id"]),
                    probability=float(row["probability"]),
                    outcome=bool(row["eventual_radiant_win"]),
                )
            )

    metrics = []
    for key in sorted(grouped):
        (
            score_version,
            model_version,
            backtest_version,
            feature_version,
            model_kind,
            horizon,
            mode,
        ) = key
        value = grouped[key]
        result = evaluate_points(
            value["points"],
            seed_material=(
                f"{score_version}:{model_version}:{backtest_version}:"
                f"{feature_version}:{model_kind}:{horizon}:{mode}"
            ),
        )
        validation_status, warnings = _validation_status(result)
        metrics.append(
            {
                "score_version": score_version,
                "model_version": model_version,
                "backtest_version": backtest_version,
                "feature_version": feature_version,
                "model_kind": model_kind,
                "horizon_minutes": horizon,
                "availability_mode": mode,
                "prediction_rows": int(value["prediction_rows"]),
                "settled_support": result.support,
                "run_statuses": dict(sorted(value["run_statuses"].items())),
                "prediction_statuses": dict(
                    sorted(value["prediction_statuses"].items())
                ),
                "brier_score": result.brier_score,
                "log_loss": result.log_loss,
                "ece_5_bin": result.ece_5_bin,
                "ece_90_upper": result.ece_90_upper,
                "auc": result.auc,
                "accuracy": result.accuracy,
                "validation_status": validation_status,
                "validation_warnings": warnings,
                "is_reconstructed": mode == "reconstructed_walk_forward",
            }
        )
    frozen = tuple(_clone_draft_metrics(metrics))
    with _DRAFT_METRICS_LOCK:
        _DRAFT_METRICS_CACHE = (fingerprint, frozen)
    return _clone_draft_metrics(frozen)


def _clone_draft_metrics(
    rows: Collection[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            **row,
            "run_statuses": dict(row["run_statuses"]),
            "prediction_statuses": dict(row["prediction_statuses"]),
            "validation_warnings": list(row["validation_warnings"]),
        }
        for row in rows
    ]


def _validation_status(result: CalibrationMetrics) -> tuple[str, list[str]]:
    warnings = [
        "support_below_100" if reason == "support<100" else str(reason)
        for reason in result.gate_failures
    ]
    if result.support < 100 and "support_below_100" not in warnings:
        warnings.insert(0, "support_below_100")
    if result.ece_90_upper is None and "ece_upper_bound_missing" not in warnings:
        warnings.append("ece_upper_bound_missing")
    if result.support < 100:
        return "unsupported", warnings
    point_failures = [
        reason for reason in warnings if reason != "ece_upper_bound_missing"
    ]
    if result.ece_90_upper is None and not point_failures:
        return "provisional", warnings
    if warnings:
        return "failed", warnings
    return "passed", warnings


__all__ = ["build_intelligence_report"]
