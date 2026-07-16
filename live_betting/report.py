"""Generate a compact JSON evaluation report for comeback shadow decisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evaluation import brier_score, log_loss, shadow_summary
from .health import read_health
from .research import research_summary
from .strict_read_gate import StrictReadGate, strict_read_gate, table_has_columns


_COHORT_IDENTITY_FIELDS = (
    "strategy_version",
    "model_version",
    "model_kind",
    "availability_mode",
    "feature_hash",
    "model_hash",
    "calibration_hash",
    "global_gate_ref",
)
_BOOTSTRAP_ITERATIONS = 1_000
_STRICT_MAPPING_JSON_PATH = (
    "$.__inputs__.strict_live_eligibility.mapping_refs.strict_mapping_id"
)
_STRATIFICATION_DEFINITIONS = {
    "team": "selected canonical team from strict mapping refs",
    "odds_bucket": "signal decimal price",
    "game_minute_bucket": "trusted vision game clock at signal",
    "vision_quality_bucket": "minimum clock/draft confidence for the signal frame",
    "signal_reason": "persisted strategy decision reason",
    "latency_bucket": "vision capture to signal transport seconds",
    "coverage_bucket": "persisted aggregate decision data_quality",
    "rejection": "persisted shadow order status and rejection reason",
    "slippage_bucket": "(signal_price - fill_price) / signal_price",
}


@dataclass(frozen=True)
class _DecisionContext:
    identity: dict[str, object | None]
    event_id: str | None
    mapping_id: int | None
    selected_side: str | None
    selected_team: str
    game_minute: float | None
    vision_key: tuple[str, str, str] | None
    signal_reason: str
    coverage: float | None


@dataclass(frozen=True)
class _OrderRecord:
    row: dict[str, object]
    series_id: str
    event_id: str | None
    selected_team: str
    game_minute: float | None
    vision_quality: float | None
    signal_reason: str
    latency_seconds: float | None
    coverage: float | None
    slippage: float | None
    outcome: int | None


def build_report(connection: sqlite3.Connection) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    invalidation_available = _table_exists(
        connection, "vision_derived_invalidations"
    )
    decision_mapping_id_sql = (
        "CASE WHEN json_valid(decision.contributions_json) "
        f"THEN json_extract(decision.contributions_json, '{_STRICT_MAPPING_JSON_PATH}') "
        "ELSE NULL END"
    )
    decision_legacy_mapping_sql = (
        "CASE WHEN json_valid(decision.contributions_json) THEN "
        f"json_type(decision.contributions_json, '{_STRICT_MAPPING_JSON_PATH}') "
        "IS NULL OR "
        f"json_type(decision.contributions_json, '{_STRICT_MAPPING_JSON_PATH}')="
        "'null' ELSE 0 END"
    )
    decision_strict_gate = strict_read_gate(
        connection,
        mapping_id_sql=decision_mapping_id_sql,
        raybet_match_id_sql="decision.raybet_match_id",
        map_number_sql="decision.map_number",
        signal_at_sql="decision.decided_at",
        dependent_type="strategy_decision",
        dependent_key_sql="decision.decision_key",
        legacy_mapping_sql=decision_legacy_mapping_sql,
    )
    invalidation_filter = (
        "NOT EXISTS ("
        "SELECT 1 FROM vision_derived_invalidations AS invalidation "
        "WHERE invalidation.dependent_type='strategy_decision' "
        "AND invalidation.dependent_key=decision.decision_key)"
        if invalidation_available
        else "0"
    )
    strict_decision_filter = decision_strict_gate.included_sql
    try:
        decisions = connection.execute(
            f"""SELECT * FROM strategy_decisions AS decision
            WHERE {invalidation_filter}
              AND {strict_decision_filter}
              AND NOT EXISTS (
                SELECT 1 FROM vision_draft_anchors AS anchor
                 WHERE anchor.raybet_match_id=decision.raybet_match_id
                   AND anchor.map_number=decision.map_number
                   AND anchor.status='conflict'
                   AND (
                         anchor.conflict_at IS NULL
                         OR julianday(anchor.conflict_at) IS NULL
                         OR julianday(decision.decided_at) IS NULL
                         OR julianday(anchor.conflict_at)<=julianday(decision.decided_at)
                         OR EXISTS (
                              SELECT 1 FROM vision_draft_conflicts AS conflict
                               WHERE conflict.raybet_match_id=anchor.raybet_match_id
                                 AND conflict.map_number=anchor.map_number
                                 AND (
                                       julianday(conflict.captured_at) IS NULL
                                       OR julianday(conflict.captured_at)
                                            <=julianday(decision.decided_at)
                                 )
                         )
                   )
                )"""
        ).fetchall()
    except sqlite3.OperationalError:
        # A legacy/malformed draft schema must not release rows into metrics.
        decisions = connection.execute(
            f"""SELECT * FROM strategy_decisions AS decision
                WHERE {invalidation_filter}
                  AND {strict_decision_filter}
                  AND 0"""
        ).fetchall()
    reasons = Counter(str(row["reason"]) for row in decisions)
    reconciliation_available = _table_exists(
        connection, "settlement_reconciliations"
    )
    reconciliation_select = (
        "reconciliation.status AS reconciliation_status"
        if reconciliation_available
        else "NULL AS reconciliation_status"
    )
    reconciliation_join = (
        """LEFT JOIN settlement_reconciliations AS reconciliation
               ON reconciliation.raybet_match_id=o.raybet_match_id
              AND reconciliation.map_number=attempt.map_number"""
        if reconciliation_available
        else ""
    )
    order_invalidation_filter = (
        "NOT EXISTS ("
        "SELECT 1 FROM vision_derived_invalidations AS invalidation "
        "WHERE invalidation.dependent_type='shadow_order' "
        "AND invalidation.dependent_key=o.order_key)"
        if invalidation_available
        else "0"
    )
    order_strict_gate = strict_read_gate(
        connection,
        mapping_id_sql="o.strict_mapping_id",
        raybet_match_id_sql="o.raybet_match_id",
        map_number_sql="attempt.map_number",
        signal_at_sql="o.signal_transport_at",
        dependent_type="shadow_order",
        dependent_key_sql="o.order_key",
    )
    strict_order_filter = order_strict_gate.included_sql
    mapping_projection_available = table_has_columns(
        connection,
        "strict_live_map_mappings",
        {
            "mapping_id",
            "event_id",
            "canonical_team_one_id",
            "canonical_team_one_name",
            "canonical_team_two_id",
            "canonical_team_two_name",
        },
    )
    mapping_projection = (
        "mapping.event_id AS strict_event_id, "
        "mapping.canonical_team_one_id, "
        "mapping.canonical_team_one_name, "
        "mapping.canonical_team_two_id, "
        "mapping.canonical_team_two_name"
        if mapping_projection_available
        else "NULL AS strict_event_id, NULL AS canonical_team_one_id, "
        "NULL AS canonical_team_one_name, NULL AS canonical_team_two_id, "
        "NULL AS canonical_team_two_name"
    )
    mapping_join = (
        "LEFT JOIN strict_live_map_mappings AS mapping "
        "ON mapping.mapping_id=o.strict_mapping_id"
        if mapping_projection_available
        else ""
    )
    try:
        orders = connection.execute(
            f"""SELECT o.*, attempt.map_number AS attempt_map_number,
                  settlement.result, settlement.return_units,
                  settlement.settled_at, settlement.review_required,
                  {mapping_projection},
                  {reconciliation_select}
             FROM shadow_orders AS o
             JOIN shadow_map_attempts AS attempt ON attempt.order_key=o.order_key
             LEFT JOIN settlements AS settlement
               ON settlement.order_key=o.order_key
             {mapping_join}
             {reconciliation_join}
            WHERE {order_invalidation_filter}
              AND {strict_order_filter}
              AND NOT EXISTS (
                SELECT 1 FROM vision_draft_anchors AS anchor
                 WHERE anchor.raybet_match_id=o.raybet_match_id
                   AND anchor.map_number=attempt.map_number
                   AND anchor.status='conflict'
                   AND (
                         anchor.conflict_at IS NULL
                         OR julianday(anchor.conflict_at) IS NULL
                         OR julianday(o.signal_transport_at) IS NULL
                         OR julianday(anchor.conflict_at)<=julianday(o.signal_transport_at)
                         OR EXISTS (
                              SELECT 1 FROM vision_draft_conflicts AS conflict
                               WHERE conflict.raybet_match_id=anchor.raybet_match_id
                                 AND conflict.map_number=anchor.map_number
                                 AND (
                                       julianday(conflict.captured_at) IS NULL
                                       OR julianday(conflict.captured_at)
                                            <=julianday(o.signal_transport_at)
                                 )
                         )
                   )
                )"""
        ).fetchall()
    except sqlite3.OperationalError:
        orders = connection.execute(
            f"""SELECT o.*, attempt.map_number AS attempt_map_number,
                  settlement.result, settlement.return_units,
                  settlement.settled_at, settlement.review_required,
                  {mapping_projection},
                  {reconciliation_select}
             FROM shadow_orders AS o
             JOIN shadow_map_attempts AS attempt ON attempt.order_key=o.order_key
             LEFT JOIN settlements AS settlement
               ON settlement.order_key=o.order_key
             {mapping_join}
            {reconciliation_join}
             WHERE {order_invalidation_filter}
               AND {strict_order_filter}
               AND 0"""
        ).fetchall()
    decision_index = _decision_index(decisions)
    cohorts = _evaluation_cohorts(
        orders,
        decision_index,
        _vision_quality_index(connection, decisions),
    )
    summary_rows = []
    settled = 0
    for row in orders:
        summary = dict(row)
        outcome = _binary_outcome(row)
        if outcome is None:
            summary["return_units"] = None
        else:
            settled += 1
        summary_rows.append(summary)
    order_audit = _order_audit_counts(
        connection,
        included_order_count=len(orders),
        scored_order_count=settled,
        strict_gate=order_strict_gate,
    )
    decision_audit = _decision_audit_counts(
        connection,
        included_decision_count=len(decisions),
        strict_gate=decision_strict_gate,
    )
    headline = cohorts[0] if len(cohorts) == 1 and cohorts[0]["identity_complete"] else None
    outbox = _group_counts(connection, "notification_outbox", "status")
    reconciliation = _settlement_reconciliation_counts(connection)
    health = read_health(connection)
    strategy_versions = dict(sorted(Counter(
        str(row["strategy_version"]) for row in decisions
    ).items()))
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
        "decision_audit": decision_audit,
        "raw_decision_count": decision_audit["raw_decisions"],
        "included_decision_count": decision_audit["included_decisions"],
        "invalidated_decision_count": decision_audit["invalidated_decisions"],
        "strict_mapping_invalidated_decision_count": decision_audit[
            "strict_mapping_invalidated_decisions"
        ],
        "strict_mapping_unverifiable_decision_count": decision_audit[
            "strict_mapping_unverifiable_decisions"
        ],
        "draft_conflict_decision_count": decision_audit[
            "draft_conflict_decisions"
        ],
        "order_audit": order_audit,
        # Flat aliases keep the two safety-critical counts discoverable for
        # consumers that do not yet understand the nested audit object.
        "invalidated_order_count": order_audit["invalidated_orders"],
        "strict_mapping_invalidated_order_count": order_audit[
            "strict_mapping_invalidated_orders"
        ],
        "strict_mapping_unverifiable_order_count": order_audit[
            "strict_mapping_unverifiable_orders"
        ],
        "review_required_order_count": order_audit["review_required_orders"],
        "eligible_decisions": sum(int(row["eligible"]) for row in decisions),
        "decision_reasons": dict(sorted(reasons.items())),
        "orders": shadow_summary(summary_rows),
        "settled_orders": settled,
        "brier_score": None if headline is None else headline["brier_score"],
        "log_loss": None if headline is None else headline["log_loss"],
        "maximum_drawdown_units": (
            None if headline is None else headline["maximum_drawdown_units"]
        ),
        "confidence_intervals_90": (
            None if headline is None else headline["confidence_intervals_90"]
        ),
        "event_sensitivity": (
            None if headline is None else headline["event_sensitivity"]
        ),
        "evaluation_cohorts": cohorts,
        "notification_outbox": outbox,
        "settlement_reconciliation": reconciliation,
        "service_health": health,
        "strategy_versions": strategy_versions,
        "strict_scope": strict_counts,
        "research": research_summary(connection),
        "stability_status": _headline_stability_status(cohorts, settled),
        "minimum_stability_sample": 500,
        "minimum_stability_events": 2,
    }


def _decision_index(
    decisions: Sequence[sqlite3.Row],
) -> dict[tuple[object, ...], list[sqlite3.Row]]:
    index: dict[tuple[object, ...], list[sqlite3.Row]] = {}
    for row in decisions:
        key = (
            str(row["raybet_match_id"]),
            int(row["map_number"]),
            str(row["decided_at"]),
            float(row["model_probability"]),
            float(row["market_probability"]),
        )
        index.setdefault(key, []).append(row)
    return index


def _evaluation_cohorts(
    orders: Sequence[sqlite3.Row],
    decision_index: Mapping[tuple[object, ...], list[sqlite3.Row]],
    vision_quality: Mapping[tuple[str, str, str], float],
) -> list[dict[str, object]]:
    grouped: dict[tuple[str | None, ...], dict[str, Any]] = {}
    for order in orders:
        key = (
            str(order["raybet_match_id"]),
            int(order["attempt_map_number"]),
            str(order["signaled_at"]),
            float(order["model_probability"]),
            float(order["market_probability"]),
        )
        candidates = decision_index.get(key, [])
        decision = candidates[0] if len(candidates) == 1 else None
        context = _decision_context(decision)
        identity = context.identity
        order_mapping_id = order["strict_mapping_id"]
        if (
            decision is None
            or order_mapping_id is None
            or context.mapping_id is None
            or int(order_mapping_id) != context.mapping_id
        ):
            identity = {
                field: identity.get(field) if decision is not None else None
                for field in _COHORT_IDENTITY_FIELDS
            }
            identity["linkage_status"] = "decision_identity_unresolved"
        else:
            identity["linkage_status"] = "verified"
        identity_key = tuple(
            None if identity.get(field) in (None, "") else str(identity[field])
            for field in (*_COHORT_IDENTITY_FIELDS, "linkage_status")
        )
        cohort = grouped.setdefault(
            identity_key,
            {"identity": identity, "records": []},
        )
        strict_event = order["strict_event_id"] or context.event_id
        team = context.selected_team
        if team == "unknown":
            team = _mapping_team_label(order, context.selected_side)
        quality = (
            None
            if context.vision_key is None
            else vision_quality.get(context.vision_key)
        )
        latency = _vision_to_signal_latency(
            context.vision_key,
            str(order["signaled_at"]),
        )
        row = dict(order)
        outcome = _binary_outcome(row)
        cohort["records"].append(_OrderRecord(
            row=row,
            series_id=str(order["raybet_match_id"]),
            event_id=None if not strict_event else str(strict_event),
            selected_team=team,
            game_minute=context.game_minute,
            vision_quality=quality,
            signal_reason=context.signal_reason,
            latency_seconds=latency,
            coverage=context.coverage,
            slippage=_slippage(row),
            outcome=outcome,
        ))

    output = []
    for identity_key, cohort in sorted(
        grouped.items(), key=lambda item: tuple(value or "" for value in item[0])
    ):
        records: list[_OrderRecord] = cohort["records"]
        events = sorted({
            record.event_id
            for record in records
            if record.outcome is not None and record.event_id is not None
        })
        identity_complete = all(identity_key[index] for index in range(
            len(_COHORT_IDENTITY_FIELDS)
        )) and identity_key[-1] == "verified"
        summary_rows = [_summary_row(record) for record in records]
        metrics = _record_metrics(records, score=identity_complete)
        bootstrap = _series_cluster_bootstrap(
            records,
            cohort["identity"],
            identity_complete=identity_complete,
        )
        sensitivity = _event_sensitivity(
            records,
            identity_complete=identity_complete,
        )
        failures = _promotion_gate_failures(
            settled=int(metrics["settled_orders"]),
            event_count=len(events),
            identity_complete=identity_complete,
            bootstrap_status=str(bootstrap["status"]),
            sensitivity_status=str(sensitivity["status"]),
        )
        output.append({
            "identity": cohort["identity"],
            "identity_complete": identity_complete,
            "orders": shadow_summary(summary_rows),
            "settled_orders": metrics["settled_orders"],
            "event_count": len(events),
            "events": events,
            "series_count": metrics["series_count"],
            "brier_score": metrics["brier_score"],
            "log_loss": metrics["log_loss"],
            "market_brier_score": metrics["market_brier_score"],
            "market_log_loss": metrics["market_log_loss"],
            "brier_improvement_vs_market": metrics[
                "brier_improvement_vs_market"
            ],
            "log_loss_improvement_vs_market": metrics[
                "log_loss_improvement_vs_market"
            ],
            "roi": metrics["roi"],
            "calibration": metrics["calibration"],
            "maximum_drawdown_units": (
                _drawdown(summary_rows)
                if identity_complete and int(metrics["settled_orders"]) > 0
                else None
            ),
            "confidence_intervals_90": bootstrap,
            "event_sensitivity": sensitivity,
            "stratified": _stratified(records, identity_complete),
            "stratification_definitions": _STRATIFICATION_DEFINITIONS,
            "stability_status": _cohort_stability_status(
                int(metrics["settled_orders"]), len(events), identity_complete
            ),
            "promotion_gate_status": "not_passed",
            "promotion_gate_failures": failures,
        })
    return output


def _decision_context(decision: sqlite3.Row | None) -> _DecisionContext:
    identity: dict[str, object | None] = {
        field: None for field in _COHORT_IDENTITY_FIELDS
    }
    if decision is None:
        return _DecisionContext(
            identity, None, None, None, "unknown", None, None, "unknown", None
        )
    identity["strategy_version"] = str(decision["strategy_version"])
    try:
        payload = json.loads(str(decision["contributions_json"]))
        inputs = payload["__inputs__"]
        landmark = inputs["draft_landmark"]
        strict = inputs["strict_live_eligibility"]["mapping_refs"]
    except (KeyError, TypeError, ValueError):
        return _DecisionContext(
            identity,
            None,
            None,
            str(decision["underdog_side"]),
            "unknown",
            None,
            None,
            str(decision["reason"]),
            _finite_or_none(decision["data_quality"]),
        )
    for field in _COHORT_IDENTITY_FIELDS[1:]:
        value = landmark.get(field)
        identity[field] = None if value in (None, "") else str(value)
    try:
        mapping_id = int(strict["strict_mapping_id"])
    except (KeyError, TypeError, ValueError):
        mapping_id = None
    event_id = strict.get("strict_event_id")
    selected_side = str(decision["underdog_side"])
    team = _strict_team_label(strict, selected_side)
    vision = inputs.get("vision")
    if not isinstance(vision, Mapping):
        vision = {}
    clock = _finite_or_none(vision.get("game_clock_seconds"))
    game_minute = None if clock is None or clock < 0.0 else clock / 60.0
    captured_at = vision.get("captured_at")
    frame_ref = vision.get("source_frame_ref")
    vision_key = (
        (
            str(decision["raybet_match_id"]),
            str(captured_at),
            str(frame_ref),
        )
        if captured_at and frame_ref
        else None
    )
    return _DecisionContext(
        identity=identity,
        event_id=None if not event_id else str(event_id),
        mapping_id=mapping_id,
        selected_side=selected_side,
        selected_team=team,
        game_minute=game_minute,
        vision_key=vision_key,
        signal_reason=str(decision["reason"]),
        coverage=_finite_or_none(decision["data_quality"]),
    )


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _strict_team_label(strict: Mapping[str, object], side: str) -> str:
    prefix = (
        "strict_canonical_team_one"
        if side == "team_one"
        else "strict_canonical_team_two" if side == "team_two" else None
    )
    if prefix is None:
        return "unknown"
    team_id = strict.get(f"{prefix}_id")
    name = strict.get(f"{prefix}_name")
    if team_id in (None, "") or name in (None, ""):
        return "unknown"
    return f"{team_id}:{name}"


def _mapping_team_label(order: sqlite3.Row, side: str | None) -> str:
    if side not in {"team_one", "team_two"}:
        return "unknown"
    suffix = "one" if side == "team_one" else "two"
    team_id = order[f"canonical_team_{suffix}_id"]
    name = order[f"canonical_team_{suffix}_name"]
    if team_id is None or name is None:
        return "unknown"
    return f"{team_id}:{name}"


def _vision_quality_index(
    connection: sqlite3.Connection,
    decisions: Sequence[sqlite3.Row],
) -> dict[tuple[str, str, str], float]:
    requested = sorted({
        context.vision_key
        for context in (_decision_context(decision) for decision in decisions)
        if context.vision_key is not None
    })
    if not requested:
        return {}
    payload = json.dumps([
        {"match": key[0], "captured_at": key[1], "frame_ref": key[2]}
        for key in requested
    ], separators=(",", ":"))
    try:
        rows = connection.execute(
            """SELECT observation.raybet_match_id, observation.captured_at,
                      observation.source_frame_ref,
                      observation.clock_confidence,
                      observation.draft_confidence
                 FROM json_each(?) AS requested
                 JOIN vision_observations AS observation
                   ON observation.raybet_match_id=
                      json_extract(requested.value, '$.match')
                  AND observation.captured_at=
                      json_extract(requested.value, '$.captured_at')
                  AND observation.source_frame_ref=
                      json_extract(requested.value, '$.frame_ref')
                WHERE observation.confirmed=1
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_draft_anchors AS anchor
                       WHERE anchor.raybet_match_id=observation.raybet_match_id
                         AND anchor.map_number=observation.map_number
                         AND anchor.status='conflict'
                         AND (
                               anchor.conflict_at IS NULL
                               OR julianday(anchor.conflict_at) IS NULL
                               OR julianday(anchor.conflict_at)<=
                                  julianday(observation.captured_at)
                               OR EXISTS (
                                    SELECT 1 FROM vision_draft_conflicts AS conflict
                                     WHERE conflict.raybet_match_id=anchor.raybet_match_id
                                       AND conflict.map_number=anchor.map_number
                                       AND (
                                             julianday(conflict.captured_at) IS NULL
                                             OR julianday(conflict.captured_at)<=
                                                julianday(observation.captured_at)
                                       )
                               )
                         )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_observation_invalidations AS invalidation
                       WHERE invalidation.raybet_match_id=observation.raybet_match_id
                         AND invalidation.captured_at=observation.captured_at
                         AND invalidation.source_frame_ref=observation.source_frame_ref
                      )""",
            (payload,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    output = {}
    for row in rows:
        clock = _finite_or_none(row[3])
        draft = _finite_or_none(row[4])
        if clock is None or draft is None:
            continue
        output[(str(row[0]), str(row[1]), str(row[2]))] = min(clock, draft)
    return output


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _vision_to_signal_latency(
    vision_key: tuple[str, str, str] | None,
    signaled_at: str,
) -> float | None:
    if vision_key is None:
        return None
    captured = _parse_timestamp(vision_key[1])
    signal = _parse_timestamp(signaled_at)
    if captured is None or signal is None:
        return None
    seconds = (signal - captured).total_seconds()
    return seconds if seconds >= 0.0 else None


def _slippage(row: Mapping[str, object]) -> float | None:
    if str(row["status"]) != "filled" or row["fill_price"] is None:
        return None
    signal_price = _finite_or_none(row["signal_price"])
    fill_price = _finite_or_none(row["fill_price"])
    if signal_price is None or signal_price <= 0.0 or fill_price is None:
        return None
    return (signal_price - fill_price) / signal_price


def _summary_row(record: _OrderRecord) -> dict[str, object]:
    row = dict(record.row)
    if record.outcome is None:
        row["return_units"] = None
    return row


def _binary_outcome(row: Mapping[str, object]) -> int | None:
    if (
        str(row["status"]) != "filled"
        or row["result"] is None
        or bool(row["review_required"])
        or str(row["reconciliation_status"]) != "confirmed"
    ):
        return None
    result = str(row["result"])
    if result in {"win", "half_win"}:
        return 1
    if result in {"loss", "half_loss"}:
        return 0
    return None


def _calibration(rows: Sequence[tuple[float, int]]) -> dict[str, object]:
    ordered = sorted(rows, key=lambda row: (row[0], row[1]))
    bin_count = min(5, len(ordered))
    bins = []
    weighted_error = 0.0
    for index in range(bin_count):
        start = index * len(ordered) // bin_count
        end = (index + 1) * len(ordered) // bin_count
        values = ordered[start:end]
        mean_probability = sum(row[0] for row in values) / len(values)
        observed_rate = sum(row[1] for row in values) / len(values)
        weighted_error += len(values) * abs(mean_probability - observed_rate)
        bins.append({
            "count": len(values),
            "mean_probability": mean_probability,
            "observed_rate": observed_rate,
        })
    return {
        "method": "five_equal_count_bins",
        "expected_calibration_error": weighted_error / len(ordered),
        "bins": bins,
    }


def _record_metrics(
    records: Sequence[_OrderRecord], *, score: bool = True, calibrate: bool = True
) -> dict[str, object]:
    settled = [record for record in records if record.outcome is not None]
    base = {
        "settled_orders": len(settled),
        "series_count": len({record.series_id for record in settled}),
        "brier_score": None,
        "log_loss": None,
        "market_brier_score": None,
        "market_log_loss": None,
        "brier_improvement_vs_market": None,
        "log_loss_improvement_vs_market": None,
        "roi": None,
        "calibration": None,
    }
    if not score or not settled:
        return base
    model_points = [
        (float(record.row["model_probability"]), int(record.outcome))
        for record in settled
    ]
    market_points = [
        (float(record.row["market_probability"]), int(record.outcome))
        for record in settled
    ]
    model_brier = brier_score(model_points)
    model_log_loss = log_loss(model_points)
    market_brier = brier_score(market_points)
    market_log_loss = log_loss(market_points)
    stake = math.fsum(float(record.row["stake"] or 0.0) for record in settled)
    returned = math.fsum(
        float(record.row["return_units"] or 0.0)
        * float(record.row["stake"] or 0.0)
        for record in settled
    )
    return {
        **base,
        "brier_score": model_brier,
        "log_loss": model_log_loss,
        "market_brier_score": market_brier,
        "market_log_loss": market_log_loss,
        "brier_improvement_vs_market": market_brier - model_brier,
        "log_loss_improvement_vs_market": market_log_loss - model_log_loss,
        "roi": (returned - stake) / stake if stake else None,
        "calibration": _calibration(model_points) if calibrate else None,
    }


_BOOTSTRAP_METRICS = (
    "brier_score",
    "log_loss",
    "market_brier_score",
    "market_log_loss",
    "brier_improvement_vs_market",
    "log_loss_improvement_vs_market",
    "roi",
)


def _series_cluster_bootstrap(
    records: Sequence[_OrderRecord],
    identity: Mapping[str, object],
    *,
    identity_complete: bool,
) -> dict[str, object]:
    settled = [record for record in records if record.outcome is not None]
    clusters: dict[str, list[_OrderRecord]] = {}
    for record in settled:
        clusters.setdefault(record.series_id, []).append(record)
    base = {
        "method": "series_cluster_percentile",
        "series_unit": "raybet_match_id",
        "confidence_level": 0.90,
        "iterations": 0,
        "series_count": len(clusters),
        "settled_orders": len(settled),
        "status": (
            "identity_incomplete"
            if not identity_complete
            else "no_settled_orders" if not settled else "insufficient_series"
        ),
        "metrics": {},
    }
    if not identity_complete:
        return base
    if len(clusters) < 2:
        return base
    cluster_ids = sorted(clusters)
    seed_payload = json.dumps(
        {field: identity.get(field) for field in _COHORT_IDENTITY_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_payload).digest()[:8], "big")
    generator = random.Random(seed)
    samples: dict[str, list[float]] = {name: [] for name in _BOOTSTRAP_METRICS}
    for _ in range(_BOOTSTRAP_ITERATIONS):
        replicate = []
        for _cluster in cluster_ids:
            replicate.extend(clusters[generator.choice(cluster_ids)])
        metrics = _record_metrics(replicate, calibrate=False)
        for name in _BOOTSTRAP_METRICS:
            value = metrics[name]
            if value is not None:
                samples[name].append(float(value))
    point = _record_metrics(settled, calibrate=False)
    intervals = {
        name: {
            "lower": _percentile(values, 0.05),
            "point": point[name],
            "upper": _percentile(values, 0.95),
        }
        for name, values in samples.items()
        if values
    }
    return {
        **base,
        "iterations": _BOOTSTRAP_ITERATIONS,
        "status": "computed",
        "metrics": intervals,
    }


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _event_sensitivity(
    records: Sequence[_OrderRecord], *, identity_complete: bool
) -> dict[str, object]:
    settled = [record for record in records if record.outcome is not None]
    events = sorted({
        record.event_id for record in settled if record.event_id is not None
    })
    base = {
        "method": "leave_one_event_out",
        "event_count": len(events),
        "status": "identity_incomplete" if not identity_complete else "insufficient_events",
        "slices": [],
        "worst_case": None,
    }
    if not identity_complete or len(events) < 2:
        return base
    full = _record_metrics(settled, calibrate=False)
    slices = []
    for event_id in events:
        remaining = [record for record in settled if record.event_id != event_id]
        metrics = _record_metrics(remaining, calibrate=False)
        slices.append({
            "held_out_event": event_id,
            "remaining_events": len({
                record.event_id
                for record in remaining
                if record.event_id is not None
            }),
            "settled_orders": metrics["settled_orders"],
            "brier_score": metrics["brier_score"],
            "log_loss": metrics["log_loss"],
            "brier_improvement_vs_market": metrics[
                "brier_improvement_vs_market"
            ],
            "log_loss_improvement_vs_market": metrics[
                "log_loss_improvement_vs_market"
            ],
            "roi": metrics["roi"],
            "delta_from_full": {
                name: _metric_delta(metrics[name], full[name])
                for name in (
                    "brier_score",
                    "log_loss",
                    "brier_improvement_vs_market",
                    "log_loss_improvement_vs_market",
                    "roi",
                )
            },
        })
    return {
        **base,
        "status": "computed",
        "slices": slices,
        "worst_case": {
            "brier_score": _extreme(slices, "brier_score", maximum=True),
            "log_loss": _extreme(slices, "log_loss", maximum=True),
            "brier_improvement_vs_market": _extreme(
                slices, "brier_improvement_vs_market", maximum=False
            ),
            "log_loss_improvement_vs_market": _extreme(
                slices, "log_loss_improvement_vs_market", maximum=False
            ),
            "roi": _extreme(slices, "roi", maximum=False),
        },
    }


def _metric_delta(value: object, baseline: object) -> float | None:
    if value is None or baseline is None:
        return None
    return float(value) - float(baseline)


def _extreme(
    rows: Sequence[Mapping[str, object]], key: str, *, maximum: bool
) -> float | None:
    values = [float(row[key]) for row in rows if row[key] is not None]
    if not values:
        return None
    return max(values) if maximum else min(values)


def _stratified(
    records: Sequence[_OrderRecord], identity_complete: bool
) -> dict[str, list[dict[str, object]]]:
    dimensions = {
        "team": lambda record: record.selected_team,
        "odds_bucket": lambda record: _odds_bucket(record.row["signal_price"]),
        "game_minute_bucket": lambda record: _minute_bucket(record.game_minute),
        "vision_quality_bucket": lambda record: _quality_bucket(
            record.vision_quality, vision=True
        ),
        "signal_reason": lambda record: record.signal_reason,
        "latency_bucket": lambda record: _latency_bucket(record.latency_seconds),
        "coverage_bucket": lambda record: _quality_bucket(
            record.coverage, vision=False
        ),
        "rejection": _rejection_bucket,
        "slippage_bucket": _slippage_bucket,
    }
    return {
        name: _stratum(records, bucket, identity_complete)
        for name, bucket in dimensions.items()
    }


def _stratum(
    records: Sequence[_OrderRecord],
    bucket: Any,
    identity_complete: bool,
) -> list[dict[str, object]]:
    grouped: dict[str, list[_OrderRecord]] = {}
    for record in records:
        grouped.setdefault(str(bucket(record)), []).append(record)
    output = []
    for label, rows in sorted(grouped.items()):
        metrics = _record_metrics(
            rows, score=identity_complete, calibrate=False
        )
        slippage = [row.slippage for row in rows if row.slippage is not None]
        latency = [
            row.latency_seconds for row in rows if row.latency_seconds is not None
        ]
        output.append({
            "bucket": label,
            "orders": len(rows),
            "filled": sum(str(row.row["status"]) == "filled" for row in rows),
            "rejected": sum(str(row.row["status"]) == "rejected" for row in rows),
            "settled_orders": metrics["settled_orders"],
            "brier_score": metrics["brier_score"],
            "log_loss": metrics["log_loss"],
            "market_brier_score": metrics["market_brier_score"],
            "roi": metrics["roi"],
            "mean_slippage": (
                math.fsum(slippage) / len(slippage) if slippage else None
            ),
            "mean_latency_seconds": (
                math.fsum(latency) / len(latency) if latency else None
            ),
        })
    return output


def _odds_bucket(value: object) -> str:
    odds = _finite_or_none(value)
    if odds is None:
        return "unknown"
    if odds < 2.5:
        return "<2.50"
    if odds < 4.0:
        return "2.50-3.99"
    if odds < 6.0:
        return "4.00-5.99"
    if odds < 9.0:
        return "6.00-8.99"
    if odds <= 12.0:
        return "9.00-12.00"
    return ">12.00"


def _minute_bucket(value: float | None) -> str:
    if value is None or value < 0.0:
        return "unknown"
    if value < 10.0:
        return "<10"
    if value < 20.0:
        return "10-19"
    if value < 30.0:
        return "20-29"
    if value < 40.0:
        return "30-39"
    if value < 50.0:
        return "40-49"
    return "50+"


def _quality_bucket(value: float | None, *, vision: bool) -> str:
    if value is None or value < 0.0 or value > 1.0:
        return "unknown"
    if vision:
        if value < 0.90:
            return "<0.90"
        if value < 0.95:
            return "0.90-0.949"
        if value < 0.98:
            return "0.95-0.979"
        return "0.98-1.00"
    if value < 0.20:
        return "<0.20"
    if value < 0.40:
        return "0.20-0.39"
    if value < 0.60:
        return "0.40-0.59"
    if value < 0.80:
        return "0.60-0.79"
    return "0.80-1.00"


def _latency_bucket(value: float | None) -> str:
    if value is None or value < 0.0:
        return "unknown"
    if value <= 1.0:
        return "0-1s"
    if value <= 3.0:
        return "1-3s"
    if value <= 5.0:
        return "3-5s"
    if value <= 10.0:
        return "5-10s"
    if value <= 30.0:
        return "10-30s"
    return ">30s"


def _rejection_bucket(record: _OrderRecord) -> str:
    status = str(record.row["status"])
    if status != "rejected":
        return status
    reason = record.row["rejection_reason"]
    return f"rejected:{reason or 'unknown'}"


def _slippage_bucket(record: _OrderRecord) -> str:
    if str(record.row["status"]) == "rejected" and (
        record.row["rejection_reason"] == "slippage"
    ):
        return "rejected_slippage"
    value = record.slippage
    if value is None:
        return "unavailable"
    if value < -0.001:
        return "favorable"
    if value <= 0.001:
        return "flat_within_0.1pct"
    if value <= 0.01:
        return "adverse_0.1-1pct"
    if value <= 0.03:
        return "adverse_1-3pct"
    return "adverse_over_3pct"


def _promotion_gate_failures(
    *,
    settled: int,
    event_count: int,
    identity_complete: bool,
    bootstrap_status: str,
    sensitivity_status: str,
) -> list[str]:
    failures = []
    if not identity_complete:
        failures.append("frozen_strategy_model_identity_incomplete")
    if settled < 500:
        failures.append("settled_forward_orders_below_500")
    if event_count < 2:
        failures.append("cross_event_evidence_missing")
    if bootstrap_status != "computed":
        failures.append("series_cluster_bootstrap_90_ci_missing")
    if sensitivity_status != "computed":
        failures.append("leave_one_event_out_sensitivity_missing")
    failures.extend((
        "forward_calibration_promotion_gate_not_recorded",
        "market_baseline_promotion_gate_not_approved",
        "return_slippage_drawdown_gate_not_approved",
    ))
    return failures


def _cohort_stability_status(
    settled: int, event_count: int, identity_complete: bool
) -> str:
    if not identity_complete:
        return "cohort_identity_incomplete"
    if settled < 100:
        return "descriptive_only"
    if settled < 500:
        return "experimental"
    if event_count < 2:
        return "stability_blocked_single_event"
    return "stability_review_required"


def _headline_stability_status(
    cohorts: Sequence[Mapping[str, object]], settled: int
) -> str:
    if not cohorts:
        return "descriptive_only" if settled < 100 else "cohort_identity_missing"
    if len(cohorts) != 1:
        return "incompatible_cohorts_not_pooled"
    return str(cohorts[0]["stability_status"])


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


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        """SELECT 1 FROM sqlite_master
             WHERE type='table' AND name=?""",
        (table,),
    ).fetchone() is not None


def _settlement_reconciliation_counts(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    counts = {"pending": 0, "confirmed": 0, "manual_review": 0}
    try:
        rows = connection.execute(
            """SELECT status, COUNT(*)
                 FROM settlement_reconciliations
                GROUP BY status"""
        ).fetchall()
    except sqlite3.OperationalError:
        return counts
    for status, count in rows:
        if str(status) in counts:
            counts[str(status)] = int(count)
    return counts


def _invalidated_count(
    connection: sqlite3.Connection, dependent_type: str
) -> int | None:
    try:
        return int(
            connection.execute(
                """SELECT COUNT(*) FROM vision_derived_invalidations
                    WHERE dependent_type=?""",
                (dependent_type,),
            ).fetchone()[0]
        )
    except sqlite3.OperationalError:
        return None


def _decision_audit_counts(
    connection: sqlite3.Connection,
    *,
    included_decision_count: int,
    strict_gate: StrictReadGate,
) -> dict[str, object]:
    """Expose raw and excluded decision denominators without changing metrics."""
    invalidation_available = _table_exists(
        connection, "vision_derived_invalidations"
    )
    conflict_available = (
        _table_exists(connection, "vision_draft_anchors")
        and _table_exists(connection, "vision_draft_conflicts")
    )
    unknown_reasons: list[str] = []
    if not invalidation_available:
        unknown_reasons.append("vision_derived_invalidations_table_missing")
    if not conflict_available:
        unknown_reasons.append("vision_draft_conflict_tables_missing")
    unknown_reasons.extend(strict_gate.unknown_reasons)
    invalidated_expr = (
        "EXISTS ("
        "SELECT 1 FROM vision_derived_invalidations AS invalidation "
        "WHERE invalidation.dependent_type='strategy_decision' "
        "AND invalidation.dependent_key=decision.decision_key)"
        if invalidation_available
        else "0"
    )
    conflict_expr = (
        "EXISTS ("
        "SELECT 1 FROM vision_draft_anchors AS anchor "
        "WHERE anchor.raybet_match_id=decision.raybet_match_id "
        "AND anchor.map_number=decision.map_number "
        "AND anchor.status='conflict' "
        "AND ("
        "anchor.conflict_at IS NULL "
        "OR julianday(anchor.conflict_at) IS NULL "
        "OR julianday(decision.decided_at) IS NULL "
        "OR julianday(anchor.conflict_at)<=julianday(decision.decided_at) "
        "OR EXISTS ("
        "SELECT 1 FROM vision_draft_conflicts AS conflict "
        "WHERE conflict.raybet_match_id=anchor.raybet_match_id "
        "AND conflict.map_number=anchor.map_number "
        "AND (julianday(conflict.captured_at) IS NULL "
        "OR julianday(conflict.captured_at)<=julianday(decision.decided_at))"
        ")"
        ")"
        ")"
        if conflict_available
        else "0"
    )
    strict_mapping_expr = strict_gate.invalidated_sql
    strict_unverifiable_expr = strict_gate.unverifiable_sql
    try:
        row = connection.execute(
            f"""WITH decision_audit AS (
                    SELECT decision.decision_key,
                           CASE WHEN {invalidated_expr}
                                THEN 1 ELSE 0 END AS invalidated,
                           CASE WHEN {conflict_expr}
                                THEN 1 ELSE 0 END AS draft_conflict,
                           CASE WHEN {strict_mapping_expr}
                                THEN 1 ELSE 0 END AS strict_mapping_invalidated,
                           CASE WHEN {strict_unverifiable_expr}
                                THEN 1 ELSE 0 END AS strict_mapping_unverifiable
                      FROM strategy_decisions AS decision
                )
                SELECT COUNT(*) AS raw_decisions,
                       COALESCE(SUM(invalidated), 0) AS invalidated_decisions,
                       COALESCE(SUM(draft_conflict), 0)
                            AS draft_conflict_decisions,
                       COALESCE(SUM(strict_mapping_invalidated), 0)
                            AS strict_mapping_invalidated_decisions,
                       COALESCE(SUM(strict_mapping_unverifiable), 0)
                            AS strict_mapping_unverifiable_decisions,
                       COALESCE(SUM(
                           CASE WHEN invalidated=1 OR draft_conflict=1
                                     OR strict_mapping_invalidated=1
                                     OR strict_mapping_unverifiable=1
                                THEN 1 ELSE 0 END
                       ), 0) AS excluded_decisions
                  FROM decision_audit"""
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        unknown_reasons.append("decision_audit_query_failed")
        raw = invalidated = draft_conflict = strict_mapping_invalidated = None
        strict_mapping_unverifiable = excluded = None
    else:
        raw = int(row["raw_decisions"])
        invalidated = int(row["invalidated_decisions"])
        draft_conflict = int(row["draft_conflict_decisions"])
        strict_mapping_invalidated = int(
            row["strict_mapping_invalidated_decisions"]
        )
        strict_mapping_unverifiable = int(
            row["strict_mapping_unverifiable_decisions"]
        )
        excluded = int(row["excluded_decisions"])
        if not invalidation_available:
            invalidated = None
        if not conflict_available:
            draft_conflict = None
        if not strict_gate.available:
            strict_mapping_invalidated = None
        if (
            invalidated is None
            or draft_conflict is None
            or strict_mapping_invalidated is None
        ):
            excluded = (
                raw - included_decision_count
                if not strict_gate.available and raw is not None
                else None
            )
    return {
        "status": "available" if not unknown_reasons else "unavailable",
        "unknown_reasons": unknown_reasons,
        "raw_decisions": raw,
        "included_decisions": int(included_decision_count),
        "excluded_decisions": excluded,
        "invalidated_decisions": invalidated,
        "draft_conflict_decisions": draft_conflict,
        "strict_mapping_invalidated_decisions": strict_mapping_invalidated,
        "strict_mapping_unverifiable_decisions": strict_mapping_unverifiable,
        "exclusion_reasons": {
            "vision_derived_invalidation": invalidated,
            "vision_draft_conflict": draft_conflict,
            "strict_mapping_invalidated": strict_mapping_invalidated,
            "strict_mapping_unverifiable": strict_mapping_unverifiable,
        },
    }


def _order_audit_counts(
    connection: sqlite3.Connection,
    *,
    included_order_count: int,
    scored_order_count: int,
    strict_gate: StrictReadGate,
) -> dict[str, object]:
    """Return report denominators and fail-closed order audit counts.

    Evaluation deliberately omits orders whose causal vision evidence was
    invalidated or whose signal follows a draft conflict.  Those rows still
    need an explicit denominator and reason count so a report cannot make an
    invalidation look like an ordinary missing order.  Review-required orders
    remain in the operational order list but are excluded from scored
    outcomes; count them independently for the same reason.  Missing audit
    tables or a failed audit query produce ``unavailable``/``null`` values,
    never a fabricated zero.
    """
    invalidation_available = _table_exists(
        connection, "vision_derived_invalidations"
    )
    conflict_available = (
        _table_exists(connection, "vision_draft_anchors")
        and _table_exists(connection, "vision_draft_conflicts")
    )
    unknown_reasons: list[str] = []
    if not invalidation_available:
        unknown_reasons.append("vision_derived_invalidations_table_missing")
    if not conflict_available:
        unknown_reasons.append("vision_draft_conflict_tables_missing")
    unknown_reasons.extend(strict_gate.unknown_reasons)
    invalidated_expr = (
        "EXISTS ("
        "SELECT 1 FROM vision_derived_invalidations AS invalidation "
        "WHERE invalidation.dependent_type='shadow_order' "
        "AND invalidation.dependent_key=o.order_key)"
        if invalidation_available
        else "0"
    )
    conflict_expr = (
        "EXISTS ("
        "SELECT 1 FROM vision_draft_anchors AS anchor "
        "WHERE anchor.raybet_match_id=o.raybet_match_id "
        "AND anchor.map_number=attempt.map_number "
        "AND anchor.status='conflict' "
        "AND ("
        "anchor.conflict_at IS NULL "
        "OR julianday(anchor.conflict_at) IS NULL "
        "OR julianday(o.signal_transport_at) IS NULL "
        "OR julianday(anchor.conflict_at)<=julianday(o.signal_transport_at) "
        "OR EXISTS ("
        "SELECT 1 FROM vision_draft_conflicts AS conflict "
        "WHERE conflict.raybet_match_id=anchor.raybet_match_id "
        "AND conflict.map_number=anchor.map_number "
        "AND (julianday(conflict.captured_at) IS NULL "
        "OR julianday(conflict.captured_at)<=julianday(o.signal_transport_at))"
        ")"
        ")"
        ")"
        if conflict_available
        else "0"
    )
    strict_mapping_expr = strict_gate.invalidated_sql
    strict_unverifiable_expr = strict_gate.unverifiable_sql
    try:
        row = connection.execute(
            f"""WITH order_audit AS (
                    SELECT o.order_key,
                           CASE WHEN {invalidated_expr}
                                THEN 1 ELSE 0 END AS invalidated,
                           CASE WHEN {conflict_expr}
                                THEN 1 ELSE 0 END AS draft_conflict,
                           CASE WHEN {strict_mapping_expr}
                                THEN 1 ELSE 0 END AS strict_mapping_invalidated,
                           CASE WHEN {strict_unverifiable_expr}
                                THEN 1 ELSE 0 END AS strict_mapping_unverifiable,
                           settlement.review_required,
                           settlement.result
                      FROM shadow_orders AS o
                      JOIN shadow_map_attempts AS attempt
                        ON attempt.order_key=o.order_key
                      LEFT JOIN settlements AS settlement
                        ON settlement.order_key=o.order_key
                )
                SELECT COUNT(*) AS total_orders,
                       COALESCE(SUM(invalidated), 0) AS invalidated_orders,
                       COALESCE(SUM(draft_conflict), 0) AS draft_conflict_orders,
                       COALESCE(SUM(strict_mapping_invalidated), 0)
                           AS strict_mapping_invalidated_orders,
                       COALESCE(SUM(strict_mapping_unverifiable), 0)
                           AS strict_mapping_unverifiable_orders,
                       COALESCE(SUM(
                           CASE WHEN invalidated=1 OR draft_conflict=1
                                     OR strict_mapping_invalidated=1
                                     OR strict_mapping_unverifiable=1
                                THEN 1 ELSE 0 END
                       ), 0) AS excluded_orders,
                       COALESCE(SUM(
                           CASE WHEN review_required=1 OR result='review'
                                THEN 1 ELSE 0 END
                       ), 0) AS review_required_orders
                  FROM order_audit"""
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        unknown_reasons.append("order_audit_query_failed")
        total = invalidated = draft_conflict = None
        strict_mapping_invalidated = strict_mapping_unverifiable = None
        excluded = review_required = None
    else:
        total = int(row["total_orders"])
        invalidated = int(row["invalidated_orders"])
        draft_conflict = int(row["draft_conflict_orders"])
        strict_mapping_invalidated = int(
            row["strict_mapping_invalidated_orders"]
        )
        strict_mapping_unverifiable = int(
            row["strict_mapping_unverifiable_orders"]
        )
        excluded = int(row["excluded_orders"])
        review_required = int(row["review_required_orders"])
        if not invalidation_available:
            invalidated = None
        if not conflict_available:
            draft_conflict = None
        if not strict_gate.available:
            strict_mapping_invalidated = None
        if (
            invalidated is None
            or draft_conflict is None
            or strict_mapping_invalidated is None
        ):
            excluded = (
                total - included_order_count
                if not strict_gate.available and total is not None
                else None
            )
    return {
        "status": "available" if not unknown_reasons else "unavailable",
        "unknown_reasons": unknown_reasons,
        "total_orders": total,
        "included_orders": int(included_order_count),
        "scored_orders": int(scored_order_count),
        "excluded_orders": excluded,
        "invalidated_orders": invalidated,
        "draft_conflict_orders": draft_conflict,
        "strict_mapping_invalidated_orders": strict_mapping_invalidated,
        "strict_mapping_unverifiable_orders": strict_mapping_unverifiable,
        "review_required_orders": review_required,
        "exclusion_reasons": {
            "vision_derived_invalidation": invalidated,
            "vision_draft_conflict": draft_conflict,
            "strict_mapping_invalidated": strict_mapping_invalidated,
            "strict_mapping_unverifiable": strict_mapping_unverifiable,
        },
    }


def _drawdown(rows: Sequence[Mapping[str, object]]) -> float:
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
    connection = sqlite3.connect(args.database, timeout=5.0)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
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
