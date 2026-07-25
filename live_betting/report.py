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
from types import SimpleNamespace
from typing import Any

from shared.sqlite import connect

from .comeback import STRATEGY_VERSION as COMEBACK_ENTRY_STRATEGY_VERSION
from .comeback_entry import ComebackEntryPolicy, decide_comeback_entry
from .evaluation import brier_score, log_loss, shadow_summary
from .draft_authority import (
    authority_from_row,
    draft_landmark_authority_matches,
)
from .health import read_health
from .m1_verifier import verify_m1_qualifying_rejection
from .milestone_revocation import (
    MilestoneRevocationConfig,
    load_milestone_revocation_projection,
)
from .research import research_summary
from .service_coordination import add_single_database_argument
from .settlement import persisted_settlement_authority_reason
from .strategy_contract import (
    parse_decision_payload,
    persisted_decision_projection_failure,
)
from .strict_read_gate import StrictReadGate, strict_read_gate, table_has_columns
from .vision_frame_registry import verify_registered_vision_frame


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
    "kill_deficit_bucket": "persisted underdog kill deficit at signal",
    "net_worth_deficit_bucket": (
        "persisted canonical HUD economy bucket directed relative to the underdog"
    ),
    "rosh_underdog_probability_bucket": (
        "persisted Rosh probability for the selected underdog at signal"
    ),
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
    kill_deficit: float | None
    net_worth_deficit_min: int | None
    net_worth_deficit_max: int | None
    rosh_underdog_probability: float | None
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
    kill_deficit: float | None
    net_worth_deficit_min: int | None
    net_worth_deficit_max: int | None
    rosh_underdog_probability: float | None
    vision_quality: float | None
    signal_reason: str
    latency_seconds: float | None
    coverage: float | None
    slippage: float | None
    outcome: int | None


@dataclass(frozen=True)
class _EntryValidation:
    valid: bool
    invalid_reason: str | None = None
    inputs: Mapping[str, Any] | None = None
    hud_confirmed: bool = False
    controlled_deficit: bool = False
    rosh_direction_pass: bool = False
    row_eligible: bool = False
    game_minute: float | None = None
    kill_deficit: float | None = None
    net_worth_deficit_min: int | None = None
    net_worth_deficit_max: int | None = None
    rosh_underdog_probability: float | None = None
    underdog_price: float | None = None


def _decision_draft_authority_valid(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> bool:
    """Require every eligible decision to name a re-readable draft landmark."""

    if int(row["eligible"]) != 1:
        return True
    authority = authority_from_row(row)
    if authority is None:
        return False
    try:
        payload = parse_decision_payload(
            str(row["contributions_json"]),
            strategy_version=str(row["strategy_version"]),
        )
        mapping_id = int(
            payload["__inputs__"]["strict_live_eligibility"]["mapping_refs"][
                "strict_mapping_id"
            ]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return draft_landmark_authority_matches(
        connection,
        authority,
        raybet_match_id=str(row["raybet_match_id"]),
        map_number=int(row["map_number"]),
        strict_mapping_id=mapping_id,
        radiant_hero_ids=None,
        dire_hero_ids=None,
        observed_at=datetime.fromisoformat(str(row["decided_at"])),
        require_current_revisions=False,
        verify_curve=False,
    )


def _order_draft_authority_valid(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> bool:
    """Require an order's immutable authority to match its persisted decision."""

    authority = authority_from_row(row)
    if authority is None:
        return False
    try:
        lineage = connection.execute(
            """SELECT decision_key FROM shadow_order_decision_lineage
                WHERE order_key=?""",
            (str(row["order_key"]),),
        ).fetchone()
        if lineage is None:
            return False
        decision = connection.execute(
            """SELECT * FROM strategy_decisions WHERE decision_key=?""",
            (str(lineage[0]),),
        ).fetchone()
        if decision is None or int(decision["eligible"]) != 1:
            return False
        decision_authority = authority_from_row(decision)
        if decision_authority != authority:
            return False
        attempt_map = int(row["attempt_map_number"])
        mapping_id = int(row["strict_mapping_id"])
        return draft_landmark_authority_matches(
            connection,
            authority,
            raybet_match_id=str(row["raybet_match_id"]),
            map_number=attempt_map,
            strict_mapping_id=mapping_id,
            radiant_hero_ids=None,
            dire_hero_ids=None,
            observed_at=datetime.fromisoformat(str(row["signal_transport_at"])),
            require_current_revisions=False,
            verify_curve=False,
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return False


def _isolate_unverified_settlements(
    connection: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
) -> tuple[list[Mapping[str, object]], Counter[str]]:
    """Keep legacy orders visible while excluding unprovable result labels."""

    output: list[Mapping[str, object]] = []
    failures: Counter[str] = Counter()
    for row in rows:
        if (
            str(row["status"]) != "filled"
            or row["result"] is None
            or bool(row["review_required"])
        ):
            output.append(row)
            continue
        reason = persisted_settlement_authority_reason(
            connection, str(row["order_key"])
        )
        if reason is None:
            output.append(row)
            continue
        failures[reason] += 1
        isolated = dict(row)
        isolated["reconciliation_status"] = None
        isolated["settlement_authority_reason"] = reason
        output.append(isolated)
    return output, failures


def _revocation_keys(
    projection: Mapping[str, object], field: str
) -> set[str]:
    isolated = projection.get("isolated_keys")
    if not isinstance(isolated, Mapping):
        return set()
    values = isolated.get(field)
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values}


def _governance_lineage_statuses(
    connection: sqlite3.Connection,
    projection: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, str]]:
    """Project explicit record lineage through persisted order/decision lineage."""

    priority = {"active": 0, "review_required": 1, "revoked": 2}
    decisions: dict[str, str] = {}
    orders: dict[str, str] = {}

    def merge(target: dict[str, str], key: object, status: str) -> None:
        text = str(key)
        if priority.get(status, 0) > priority.get(target.get(text, "active"), 0):
            target[text] = status

    records = projection.get("records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, Mapping):
                continue
            status = str(record.get("governance_status", "review_required"))
            affected = record.get("affected")
            if not isinstance(affected, Mapping):
                continue
            for key in affected.get("decision_keys", []):
                merge(decisions, key, status)
            for field in ("order_keys", "settlement_keys"):
                for key in affected.get(field, []):
                    merge(orders, key, status)
            lineage = affected.get("sample_lineage")
            if isinstance(lineage, list):
                for item in lineage:
                    if not isinstance(item, Mapping):
                        continue
                    merge(decisions, item.get("decision_key"), status)
                    merge(orders, item.get("order_key"), status)
                    merge(orders, item.get("settlement_key"), status)
    if not decisions and not orders:
        return decisions, orders
    try:
        rows = connection.execute(
            "SELECT order_key, decision_key FROM shadow_order_decision_lineage"
        ).fetchall()
    except sqlite3.OperationalError:
        # A configured revocation with unreadable lineage cannot authorize any
        # potentially dependent decision/order surface.
        try:
            for row in connection.execute("SELECT decision_key FROM strategy_decisions"):
                merge(decisions, row[0], "review_required")
            for row in connection.execute("SELECT order_key FROM shadow_orders"):
                merge(orders, row[0], "review_required")
        except sqlite3.OperationalError:
            pass
        return decisions, orders
    changed = True
    while changed:
        changed = False
        for order_key, decision_key in rows:
            order_text = str(order_key)
            decision_text = str(decision_key)
            statuses = [
                status
                for status in (orders.get(order_text), decisions.get(decision_text))
                if status is not None
            ]
            if not statuses:
                continue
            status = max(statuses, key=lambda item: priority[item])
            before = (orders.get(order_text), decisions.get(decision_text))
            merge(orders, order_text, status)
            merge(decisions, decision_text, status)
            changed = changed or before != (
                orders.get(order_text), decisions.get(decision_text)
            )
    return decisions, orders


def _add_revocation_exclusion(
    audit: dict[str, object], count: int
) -> dict[str, object]:
    if count == 0:
        return audit
    output = dict(audit)
    reasons = dict(output["exclusion_reasons"])
    reasons["milestone_revocation"] = count
    output["exclusion_reasons"] = reasons
    if output["excluded_decisions" if "excluded_decisions" in output else "excluded_orders"] is not None:
        key = "excluded_decisions" if "excluded_decisions" in output else "excluded_orders"
        output[key] = int(output[key]) + count
    return output


def _add_decision_payload_exclusions(
    audit: dict[str, object], failures: Mapping[str, int]
) -> dict[str, object]:
    if not failures:
        return audit
    output = dict(audit)
    reasons = dict(output["exclusion_reasons"])
    reasons.update(sorted(failures.items()))
    output["exclusion_reasons"] = reasons
    if output["excluded_decisions"] is not None:
        output["excluded_decisions"] = int(output["excluded_decisions"]) + sum(
            failures.values()
        )
    return output


def build_report(
    connection: sqlite3.Connection,
    *,
    revocation_config: MilestoneRevocationConfig | None = None,
) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    governance = load_milestone_revocation_projection(
        config=revocation_config,
        connection=connection if revocation_config is not None else None,
    )
    decision_governance, order_governance = _governance_lineage_statuses(
        connection, governance
    )
    isolated_decision_keys = set(decision_governance)
    isolated_order_keys = set(order_governance)
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
    direct_decision_filter = (
        "EXISTS (SELECT 1 FROM odds_transport_observations AS transport "
        "WHERE transport.raybet_match_id=decision.raybet_match_id "
        "AND transport.observed_at=decision.decided_at "
        "AND (decision.vision_transport_key IS NULL "
        "OR transport.observation_key=decision.vision_transport_key) "
        "AND transport.source='direct')"
    )
    decision_payload_failure_by_key: dict[str, str] = {}
    try:
        payload_candidates = connection.execute(
            f"""SELECT decision.* FROM strategy_decisions AS decision
                 WHERE {direct_decision_filter}"""
        ).fetchall()
    except sqlite3.OperationalError:
        payload_candidates = []
    for candidate in payload_candidates:
        failure = persisted_decision_projection_failure(dict(candidate))
        if failure is not None:
            decision_payload_failure_by_key[str(candidate["decision_key"])] = failure
    strict_decision_filter = decision_strict_gate.included_sql
    try:
        decisions = connection.execute(
            f"""SELECT * FROM strategy_decisions AS decision
            WHERE {invalidation_filter}
              AND {direct_decision_filter}
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
                  AND {direct_decision_filter}
                  AND {strict_decision_filter}
                  AND 0"""
        ).fetchall()
    raw_decision_rows = decisions
    decision_payload_failures: Counter[str] = Counter(
        decision_payload_failure_by_key.values()
    )
    projection_valid_decisions = []
    for row in decisions:
        failure = decision_payload_failure_by_key.get(str(row["decision_key"]))
        if failure is None:
            projection_valid_decisions.append(row)
    draft_valid_decisions = [
        row for row in projection_valid_decisions
        if _decision_draft_authority_valid(connection, row)
    ]
    draft_authority_invalid_decision_count = (
        len(projection_valid_decisions) - len(draft_valid_decisions)
    )
    decisions = [
        row
        for row in draft_valid_decisions
        if str(row["decision_key"]) not in isolated_decision_keys
    ]
    governance_isolated_decision_count = len(draft_valid_decisions) - len(decisions)
    entry_validations = {
        str(row["decision_key"]): _validate_v4_entry(row)
        for row in decisions
        if str(row["strategy_version"]) == COMEBACK_ENTRY_STRATEGY_VERSION
    }
    eligible_decisions = 0
    for row in decisions:
        if int(row["eligible"]) != 1:
            continue
        if str(row["strategy_version"]) == COMEBACK_ENTRY_STRATEGY_VERSION:
            validation = entry_validations.get(str(row["decision_key"]))
            if validation is None or not validation.valid:
                continue
        eligible_decisions += 1
    reasons = Counter(str(row["reason"]) for row in decisions)
    reconciliation_available = table_has_columns(
        connection,
        "settlement_reconciliations",
        {
            "raybet_match_id",
            "map_number",
            "strict_mapping_id",
            "dota_match_id",
            "raybet_winner_side",
            "opendota_winner_side",
            "status",
        },
    ) and table_has_columns(
        connection,
        "map_results",
        {
            "raybet_match_id",
            "map_number",
            "strict_mapping_id",
            "dota_match_id",
            "winner_side",
        },
    )
    reconciliation_select = (
        """CASE
               WHEN reconciliation.status!='confirmed'
                 THEN reconciliation.status
               WHEN reconciled_result.dota_match_id IS NOT NULL
                 THEN reconciliation.status
               ELSE NULL
           END AS reconciliation_status"""
        if reconciliation_available
        else "NULL AS reconciliation_status"
    )
    reconciliation_join = (
        """LEFT JOIN settlement_reconciliations AS reconciliation
               ON reconciliation.raybet_match_id=o.raybet_match_id
              AND reconciliation.map_number=attempt.map_number
              AND reconciliation.strict_mapping_id=o.strict_mapping_id
            LEFT JOIN map_results AS reconciled_result
               ON reconciled_result.raybet_match_id=reconciliation.raybet_match_id
              AND reconciled_result.map_number=reconciliation.map_number
              AND reconciled_result.strict_mapping_id=
                  reconciliation.strict_mapping_id
              AND reconciled_result.dota_match_id=reconciliation.dota_match_id
              AND reconciled_result.winner_side=reconciliation.raybet_winner_side
              AND reconciled_result.winner_side=
                  reconciliation.opendota_winner_side"""
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
    direct_order_filter = (
        "EXISTS (SELECT 1 FROM odds_transport_observations AS transport "
        "WHERE transport.observation_key=o.signal_transport_key "
        "AND transport.raybet_match_id=o.raybet_match_id "
        "AND transport.observed_at=o.signal_transport_at "
        "AND transport.source='direct')"
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
              AND {direct_order_filter}
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
                AND {direct_order_filter}
                AND {strict_order_filter}
               AND 0"""
        ).fetchall()
    raw_order_rows = orders
    draft_valid_orders = [
        row for row in orders
        if _order_draft_authority_valid(connection, row)
    ]
    draft_authority_invalid_order_count = len(raw_order_rows) - len(draft_valid_orders)
    if isolated_decision_keys:
        try:
            linked_revoked_orders = connection.execute(
                """SELECT lineage.order_key
                     FROM shadow_order_decision_lineage AS lineage
                     JOIN json_each(?) AS revoked
                       ON revoked.value=lineage.decision_key""",
                (json.dumps(sorted(isolated_decision_keys)),),
            ).fetchall()
        except sqlite3.OperationalError:
            # A configured revocation must not leave dependent orders scored
            # when their lineage relation cannot be read.
            isolated_order_keys.update(
                str(row["order_key"]) for row in draft_valid_orders
            )
        else:
            isolated_order_keys.update(str(row[0]) for row in linked_revoked_orders)
    orders = [
        row
        for row in draft_valid_orders
        if str(row["order_key"]) not in isolated_order_keys
    ]
    governance_isolated_order_count = len(draft_valid_orders) - len(orders)
    orders, settlement_authority_failures = _isolate_unverified_settlements(
        connection, orders
    )
    decision_index = _decision_index(decisions)
    cohorts = _evaluation_cohorts(
        orders,
        decision_index,
        _vision_quality_index(connection, decisions),
        entry_validations,
    )
    scorable_orders = _scorable_orders(
        orders, decision_index, entry_validations
    )
    summary_rows = []
    settled = 0
    for row in scorable_orders:
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
    decision_audit = _add_decision_payload_exclusions(
        decision_audit, decision_payload_failures
    )
    order_audit = _add_revocation_exclusion(
        order_audit, governance_isolated_order_count
    )
    decision_audit = _add_revocation_exclusion(
        decision_audit, governance_isolated_decision_count
    )
    if (
        decision_audit["excluded_decisions"] is not None
        and decision_audit["raw_decisions"] is not None
    ):
        decision_audit["excluded_decisions"] = int(
            decision_audit["raw_decisions"]
        ) - int(decision_audit["included_decisions"])
    scorable_cohorts = [
        cohort for cohort in cohorts if cohort["identity_complete"]
    ]
    headline = scorable_cohorts[0] if len(scorable_cohorts) == 1 else None
    outbox = _group_counts(connection, "notification_outbox", "status")
    reconciliation = _settlement_reconciliation_counts(connection)
    settlement_authority_audit_log = _group_counts(
        connection, "settlement_authority_audit", "reason"
    )
    health = read_health(connection)
    strategy_versions = dict(sorted(Counter(
        str(row["strategy_version"]) for row in decisions
    ).items()))
    forward_entry_by_strategy_version = _forward_entry_evaluation(
        decisions, cohorts, entry_validations
    )
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
    try:
        m1_candidates = connection.execute(
            "SELECT decision_key FROM strategy_decisions ORDER BY decided_at, decision_key"
        ).fetchall()
    except sqlite3.OperationalError:
        m1_candidates = []
    m1_strategy_contract_verifications = []
    revoked_milestones = set(governance.get("revoked_milestones", []))
    review_milestones = set(governance.get("review_required_milestones", []))
    milestone_status = (
        "revoked"
        if "M1" in revoked_milestones
        else "review_required" if "M1" in review_milestones else "active"
    )
    governance_priority = {"active": 0, "review_required": 1, "revoked": 2}
    for candidate in m1_candidates:
        verification = verify_m1_qualifying_rejection(
            connection, str(candidate["decision_key"])
        )
        governance_status = max(
            (
                decision_governance.get(verification.decision_key, "active"),
                milestone_status,
            ),
            key=lambda status: governance_priority[status],
        )
        authorized = governance_status == "active"
        m1_strategy_contract_verifications.append(
            {
                "decision_key": verification.decision_key,
                "strategy_version": verification.strategy_version,
                "evaluator_hash": verification.evaluator_hash,
                "policy_hash": verification.policy_hash,
                "serialization_version": verification.serialization_version,
                "m1_qualifying_rejection": (
                    verification.m1_qualifying_rejection and authorized
                ),
                "governance_status": governance_status,
                "authorized": authorized,
                "verifier_reason": verification.reason,
                "replay_reason": verification.replay_reason,
            }
        )
    return {
        "decision_count": len(decisions),
        "draft_authority_invalid_decision_count": (
            draft_authority_invalid_decision_count
        ),
        "decision_payload_invalid_count": sum(decision_payload_failures.values()),
        "decision_payload_exclusion_reasons": dict(
            sorted(decision_payload_failures.items())
        ),
        "governance_isolated_decision_count": governance_isolated_decision_count,
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
        "raw_order_count": len(raw_order_rows),
        "draft_authority_invalid_order_count": (
            draft_authority_invalid_order_count
        ),
        "governance_isolated_order_count": governance_isolated_order_count,
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
        "settlement_authority_invalid_order_count": sum(
            settlement_authority_failures.values()
        ),
        "settlement_authority_audit": dict(
            sorted(settlement_authority_failures.items())
        ),
        "eligible_decisions": eligible_decisions,
        "decision_reasons": dict(sorted(reasons.items())),
        "orders": _order_summary(summary_rows, score=True),
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
        "settlement_authority_audit_log": settlement_authority_audit_log,
        "settlement_authority_audit_log_count": sum(
            settlement_authority_audit_log.values()
        ),
        "service_health": health,
        "strategy_versions": strategy_versions,
        "forward_entry_by_strategy_version": forward_entry_by_strategy_version,
        "m1_strategy_contract_verifications": (
            m1_strategy_contract_verifications
        ),
        "milestone_governance": governance,
        "strict_scope": strict_counts,
        "research": research_summary(connection),
        "stability_status": _headline_stability_status(
            scorable_cohorts, settled
        ),
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


def _linked_decision(
    order: Mapping[str, object],
    decision_index: Mapping[tuple[object, ...], list[sqlite3.Row]],
) -> sqlite3.Row | None:
    key = (
        str(order["raybet_match_id"]),
        int(order["attempt_map_number"]),
        str(order["signaled_at"]),
        float(order["model_probability"]),
        float(order["market_probability"]),
    )
    candidates = decision_index.get(key, [])
    return candidates[0] if len(candidates) == 1 else None


def _scorable_orders(
    orders: Sequence[Mapping[str, object]],
    decision_index: Mapping[tuple[object, ...], list[sqlite3.Row]],
    entry_validations: Mapping[str, _EntryValidation],
) -> list[Mapping[str, object]]:
    output = []
    for order in orders:
        decision = _linked_decision(order, decision_index)
        if decision is None:
            continue
        context = _decision_context(decision)
        if (
            order["strict_mapping_id"] is None
            or context.mapping_id is None
            or int(order["strict_mapping_id"]) != context.mapping_id
            or any(
                context.identity.get(field) in (None, "")
                for field in _COHORT_IDENTITY_FIELDS
            )
        ):
            continue
        if str(decision["strategy_version"]) == COMEBACK_ENTRY_STRATEGY_VERSION:
            validation = entry_validations.get(str(decision["decision_key"]))
            if validation is None or not validation.valid:
                continue
        output.append(order)
    return output


def _evaluation_cohorts(
    orders: Sequence[sqlite3.Row],
    decision_index: Mapping[tuple[object, ...], list[sqlite3.Row]],
    vision_quality: Mapping[tuple[str, str, str], float],
    entry_validations: Mapping[str, _EntryValidation],
) -> list[dict[str, object]]:
    grouped: dict[tuple[str | None, ...], dict[str, Any]] = {}
    for order in orders:
        decision = _linked_decision(order, decision_index)
        context = _decision_context(decision)
        identity = context.identity
        entry_status: str | None = None
        if (
            decision is not None
            and str(decision["strategy_version"])
            == COMEBACK_ENTRY_STRATEGY_VERSION
        ):
            validation = entry_validations.get(str(decision["decision_key"]))
            entry_status = (
                "valid" if validation is not None and validation.valid else "invalid"
            )
            identity["entry_evidence_status"] = entry_status
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
            for field in (
                *_COHORT_IDENTITY_FIELDS,
                "entry_evidence_status",
                "linkage_status",
            )
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
            kill_deficit=context.kill_deficit,
            net_worth_deficit_min=context.net_worth_deficit_min,
            net_worth_deficit_max=context.net_worth_deficit_max,
            rosh_underdog_probability=context.rosh_underdog_probability,
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
        )) and identity_key[-1] == "verified" and (
            cohort["identity"].get("strategy_version")
            != COMEBACK_ENTRY_STRATEGY_VERSION
            or cohort["identity"].get("entry_evidence_status") == "valid"
        )
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
            "orders": _order_summary(
                summary_rows, score=identity_complete
            ),
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
            identity, None, None, None, "unknown", None, None, None, None,
            None, None, "unknown", None
        )
    identity["strategy_version"] = str(decision["strategy_version"])
    authority = authority_from_row(decision)
    if authority is not None:
        identity.update({
            "model_version": authority.model_version,
            "model_kind": "pure_draft",
            "availability_mode": "prospective",
            "feature_hash": authority.feature_hash,
            "model_hash": authority.model_hash,
            "calibration_hash": authority.calibration_hash,
            "global_gate_ref": authority.global_gate_ref,
        })
    try:
        payload = parse_decision_payload(
            str(decision["contributions_json"]),
            strategy_version=str(decision["strategy_version"]),
        )
        inputs = payload["__inputs__"]
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
            None,
            None,
            None,
            None,
            str(decision["reason"]),
            _finite_or_none(decision["data_quality"]),
        )
    mapping_id = (
        authority.strict_mapping_id
        if authority is not None
        else None
    )
    event_id = strict.get("strict_event_id")
    selected_side = str(decision["underdog_side"])
    team = _strict_team_label(strict, selected_side)
    vision = inputs.get("vision")
    if not isinstance(vision, Mapping):
        vision = {}
    clock = _finite_or_none(vision.get("game_clock_seconds"))
    game_minute = None if clock is None or clock < 0.0 else clock / 60.0
    comeback_state = inputs.get("comeback_state")
    if not isinstance(comeback_state, Mapping):
        comeback_state = {}
    kill_deficit = _finite_or_none(comeback_state.get("kill_deficit"))
    raw_net_worth_deficit_min = comeback_state.get("net_worth_deficit_min")
    raw_net_worth_deficit_max = comeback_state.get("net_worth_deficit_max")
    net_worth_deficit_min = (
        raw_net_worth_deficit_min
        if type(raw_net_worth_deficit_min) is int
        else None
    )
    net_worth_deficit_max = (
        raw_net_worth_deficit_max
        if type(raw_net_worth_deficit_max) is int
        else None
    )
    comeback_entry = inputs.get("comeback_entry")
    if not isinstance(comeback_entry, Mapping):
        comeback_entry = {}
    rosh_underdog_probability = _finite_or_none(
        comeback_entry.get("rosh_underdog_probability")
    )
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
        kill_deficit=kill_deficit,
        net_worth_deficit_min=net_worth_deficit_min,
        net_worth_deficit_max=net_worth_deficit_max,
        rosh_underdog_probability=rosh_underdog_probability,
        vision_key=vision_key,
        signal_reason=str(decision["reason"]),
        coverage=_finite_or_none(decision["data_quality"]),
    )


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _forward_entry_evaluation(
    decisions: Sequence[sqlite3.Row],
    cohorts: Sequence[Mapping[str, object]],
    entry_validations: Mapping[str, _EntryValidation],
) -> dict[str, dict[str, object]]:
    """Expose the current v4 entry funnel without pooling strategy versions."""

    version = COMEBACK_ENTRY_STRATEGY_VERSION
    rows = [row for row in decisions if str(row["strategy_version"]) == version]
    counts: Counter[str] = Counter()
    buckets: dict[str, Counter[str]] = {
        "game_minute": Counter(),
        "kill_deficit": Counter(),
        "net_worth_deficit": Counter(),
        "rosh_underdog_probability": Counter(),
        "odds": Counter(),
    }
    rejections: Counter[str] = Counter()
    invalid_reasons: Counter[str] = Counter()
    for row in rows:
        validation = entry_validations.get(str(row["decision_key"]))
        valid = validation is not None and validation.valid
        counts["entry_evidence" if valid else "entry_evidence_invalid"] += 1
        if not valid:
            reason = (
                validation.invalid_reason
                if validation is not None and validation.invalid_reason
                else "entry_validation_missing"
            )
            invalid_reasons[reason] += 1
        assert validation is not None
        buckets["game_minute"][_minute_bucket(validation.game_minute)] += 1
        buckets["kill_deficit"][_kill_deficit_bucket(
            validation.kill_deficit
        )] += 1
        buckets["net_worth_deficit"][_net_worth_deficit_bucket(
            validation.net_worth_deficit_min,
            validation.net_worth_deficit_max,
        )] += 1
        buckets["rosh_underdog_probability"][_rosh_probability_bucket(
            validation.rosh_underdog_probability
        )] += 1
        buckets["odds"][_odds_bucket(
            validation.underdog_price
        )] += 1
        if valid and validation.hud_confirmed:
            counts["hud_confirmed"] += 1
        if valid and validation.controlled_deficit:
            counts["controlled_deficit"] += 1
        if valid and validation.rosh_direction_pass:
            counts["rosh_direction_pass"] += 1
        if valid and validation.row_eligible:
            counts["eligible"] += 1
        elif valid:
            rejections[str(row["reason"])] += 1

    version_cohorts = [
        cohort for cohort in cohorts
        if isinstance(cohort.get("identity"), Mapping)
        and cohort["identity"].get("strategy_version") == version
    ]
    return {version: {
        "candidate_count": len(rows),
        "entry_evidence_count": counts["entry_evidence"],
        "entry_evidence_invalid_count": counts["entry_evidence_invalid"],
        "entry_evidence_invalid_reasons": dict(sorted(invalid_reasons.items())),
        "hud_confirmed_count": counts["hud_confirmed"],
        "controlled_deficit_count": counts["controlled_deficit"],
        "rosh_direction_pass_count": counts["rosh_direction_pass"],
        "eligible_count": counts["eligible"],
        "rejection_reasons": dict(sorted(rejections.items())),
        "candidate_buckets": {
            name: dict(sorted(values.items())) for name, values in buckets.items()
        },
        "settled_performance": _version_settled_performance(version_cohorts),
    }}


def _invalid_entry(reason: str) -> _EntryValidation:
    return _EntryValidation(False, invalid_reason=reason)


def _validate_v4_entry(row: sqlite3.Row) -> _EntryValidation:
    """Rebuild the persisted v4 entry decision from its own frozen policy."""

    try:
        payload = parse_decision_payload(
            str(row["contributions_json"]),
            strategy_version=str(row["strategy_version"]),
        )
        inputs = payload["__inputs__"]
        state = inputs["comeback_state"]
        window = inputs["entry_window"]
        entry = inputs["comeback_entry"]
        market = inputs["market"]
        rosh = inputs["rosh_lineup_score"]
        vision = inputs["vision"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _invalid_entry("invalid_entry_json")
    if not isinstance(inputs, Mapping) or not all(
        isinstance(group, Mapping)
        for group in (state, window, entry, market, rosh, vision)
    ):
        return _invalid_entry("invalid_entry_structure")

    raw_policy = entry.get("policy")
    if not isinstance(raw_policy, Mapping):
        return _invalid_entry("invalid_entry_policy")
    integer_policy_fields = (
        "minimum_clock_seconds",
        "maximum_clock_seconds",
        "minimum_kill_deficit",
        "maximum_kill_deficit",
        "minimum_net_worth_deficit",
        "maximum_net_worth_deficit",
    )
    confidence = raw_policy.get("minimum_vision_confidence")
    if (
        set(raw_policy) != set(ComebackEntryPolicy.__dataclass_fields__)
        or any(
            type(raw_policy.get(name)) is not int or int(raw_policy[name]) < 0
            for name in integer_policy_fields
        )
        or _finite_or_none(confidence) is None
        or not 0.0 <= float(confidence) <= 1.0
        or int(raw_policy["minimum_clock_seconds"])
        > int(raw_policy["maximum_clock_seconds"])
        or int(raw_policy["minimum_kill_deficit"])
        > int(raw_policy["maximum_kill_deficit"])
        or int(raw_policy["minimum_net_worth_deficit"])
        > int(raw_policy["maximum_net_worth_deficit"])
    ):
        return _invalid_entry("invalid_entry_policy")
    policy = ComebackEntryPolicy(**dict(raw_policy))

    underdog_side = str(row["underdog_side"])
    radiant_team_side = vision.get("radiant_team_side")
    game_clock = window.get("game_clock_seconds")
    underdog_price = _finite_or_none(market.get("underdog_price"))
    if (
        underdog_side not in {"team_one", "team_two"}
        or state.get("underdog_side") != underdog_side
        or market.get("underdog_side") != underdog_side
        or radiant_team_side not in {"team_one", "team_two"}
        or type(game_clock) is not int
        or game_clock < 0
        or vision.get("game_clock_seconds") != game_clock
        or underdog_price is None
        or underdog_price <= 0.0
    ):
        return _invalid_entry("invalid_entry_identity")
    if (
        state.get("reason")
        == "vision_net_worth_exact_totals_not_production_evidence"
        or any(
            state.get(name) is not None
            for name in (
                "underdog_net_worth",
                "opponent_net_worth",
                "net_worth_deficit",
            )
        )
    ):
        return _invalid_entry("unsupported_exact_net_worth_evidence")

    persisted_probability = entry.get("rosh_underdog_probability")
    selected_score = rosh.get("selected_score")
    if selected_score is None:
        if persisted_probability is not None:
            return _invalid_entry("inconsistent_rosh_direction")
        rosh_probability = None
    else:
        score = _finite_or_none(selected_score)
        rosh_probability = _finite_or_none(persisted_probability)
        if score is None or rosh_probability is None or not 0.0 <= rosh_probability <= 1.0:
            return _invalid_entry("inconsistent_rosh_direction")
        radiant_probability = min(
            1.0 - 1e-6,
            max(1e-6, (50.0 + score) / 100.0),
        )
        expected_probability = (
            radiant_probability
            if underdog_side == radiant_team_side
            else 1.0 - radiant_probability
        )
        if not math.isclose(
            rosh_probability, expected_probability, rel_tol=1e-9, abs_tol=1e-9
        ):
            return _invalid_entry("inconsistent_rosh_direction")

    raw_state, state_valid = _raw_comeback_state(
        state,
        underdog_side=underdog_side,
        radiant_team_side=str(radiant_team_side),
    )
    if not state_valid:
        return _invalid_entry("invalid_comeback_state")
    canonical = decide_comeback_entry(
        SimpleNamespace(
            comeback_state=raw_state,
            screen_state="game",
            game_clock_seconds=game_clock,
            radiant_team_side=radiant_team_side,
        ),
        underdog_side=underdog_side,
        rosh_underdog_probability=rosh_probability,
        policy=policy,
    )
    canonical_inputs = canonical.as_inputs()
    if (
        dict(state) != canonical_inputs["comeback_state"]
        or dict(window) != canonical_inputs["entry_window"]
        or dict(entry) != canonical_inputs["comeback_entry"]
    ):
        return _invalid_entry("inconsistent_entry_contract")

    row_eligible_value = row["eligible"]
    row_reason = row["reason"]
    if (
        row_eligible_value not in (0, 1)
        or not isinstance(row_reason, str)
        or not row_reason
        or (bool(row_eligible_value) and (
            not canonical.eligible or row_reason != "eligible"
        ))
        or (not bool(row_eligible_value) and row_reason == "eligible")
    ):
        return _invalid_entry("inconsistent_row_entry_decision")

    return _EntryValidation(
        True,
        inputs=inputs,
        hud_confirmed=(
            canonical.situation.source_status == "available"
            and canonical.situation.source == "vision_hud"
            and canonical.situation.confidence >= policy.minimum_vision_confidence
        ),
        controlled_deficit=canonical.situation.controllable,
        rosh_direction_pass=(
            canonical.situation.controllable
            and rosh_probability is not None
            and rosh_probability > 0.5
        ),
        row_eligible=bool(row_eligible_value),
        game_minute=game_clock / 60.0,
        kill_deficit=(
            None
            if canonical.situation.kill_deficit is None
            else float(canonical.situation.kill_deficit)
        ),
        net_worth_deficit_min=canonical.situation.net_worth_deficit_min,
        net_worth_deficit_max=canonical.situation.net_worth_deficit_max,
        rosh_underdog_probability=rosh_probability,
        underdog_price=underdog_price,
    )


def _raw_comeback_state(
    state: Mapping[str, Any],
    *,
    underdog_side: str,
    radiant_team_side: str,
) -> tuple[object | None, bool]:
    expected_keys = {
        "controllable", "reason", "source_status", "source", "confidence",
        "underdog_side", "underdog_kills", "opponent_kills", "kill_deficit",
        "underdog_net_worth", "opponent_net_worth", "net_worth_deficit",
        "net_worth_advantage_side", "net_worth_deficit_min",
        "net_worth_deficit_max", "unavailable_reason",
    }
    if set(state) != expected_keys:
        return None, False
    if state.get("source_status") == "missing":
        return None, True
    if state.get("source_status") == "unavailable":
        return {
            "status": "unavailable",
            "source": None,
            "confidence": 0.0,
            "unavailable_reason": state.get("unavailable_reason"),
        }, True
    if state.get("source_status") != "available" or state.get("source") != "vision_hud":
        return None, False

    underdog_kills = state.get("underdog_kills")
    opponent_kills = state.get("opponent_kills")
    if any(
        type(value) is not int or value < 0
        for value in (underdog_kills, opponent_kills)
    ):
        return None, False
    underdog_is_radiant = underdog_side == radiant_team_side
    radiant_kills, dire_kills = (
        (underdog_kills, opponent_kills)
        if underdog_is_radiant
        else (opponent_kills, underdog_kills)
    )

    underdog_net_worth = state.get("underdog_net_worth")
    opponent_net_worth = state.get("opponent_net_worth")
    deficit_min = state.get("net_worth_deficit_min")
    deficit_max = state.get("net_worth_deficit_max")
    advantage_side = state.get("net_worth_advantage_side")
    raw = {
        "status": "available",
        "source": "vision_hud",
        "confidence": state.get("confidence"),
        "radiant_kills": radiant_kills,
        "dire_kills": dire_kills,
        "radiant_net_worth": None,
        "dire_net_worth": None,
        "net_worth_advantage_side": None,
        "net_worth_advantage_min": None,
        "net_worth_advantage_max": None,
        "unavailable_reason": None,
    }
    exact_values = (underdog_net_worth, opponent_net_worth)
    if all(type(value) is int and value >= 0 for value in exact_values):
        raw["radiant_net_worth"], raw["dire_net_worth"] = (
            exact_values if underdog_is_radiant else tuple(reversed(exact_values))
        )
    elif all(value is None for value in exact_values) and (
        advantage_side in {"radiant", "dire"}
        and type(deficit_min) is int
        and type(deficit_max) is int
        and deficit_min <= deficit_max
    ):
        underdog_radiant_side = "radiant" if underdog_is_radiant else "dire"
        if advantage_side == underdog_radiant_side:
            advantage_min, advantage_max = -deficit_max, -deficit_min
        else:
            advantage_min, advantage_max = deficit_min, deficit_max
        if advantage_min < 0 or advantage_max < advantage_min:
            return None, False
        raw["net_worth_advantage_side"] = advantage_side
        raw["net_worth_advantage_min"] = advantage_min
        raw["net_worth_advantage_max"] = advantage_max
    else:
        return None, False
    return raw, True


def _version_settled_performance(
    cohorts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    dimensions = (
        "odds_bucket",
        "game_minute_bucket",
        "kill_deficit_bucket",
        "net_worth_deficit_bucket",
        "rosh_underdog_probability_bucket",
    )
    isolated = [
        cohort for cohort in cohorts
        if isinstance(cohort.get("identity"), Mapping)
        and cohort["identity"].get("entry_evidence_status") != "valid"
    ]
    summaries = []
    for cohort in cohorts:
        if (
            not isinstance(cohort.get("identity"), Mapping)
            or cohort["identity"].get("entry_evidence_status") != "valid"
        ):
            continue
        stratified = cohort["stratified"]
        summaries.append({
            "identity": cohort["identity"],
            "identity_complete": cohort["identity_complete"],
            "orders": cohort["orders"],
            "settled_orders": cohort["settled_orders"],
            "event_count": cohort["event_count"],
            "brier_score": cohort["brier_score"],
            "log_loss": cohort["log_loss"],
            "market_brier_score": cohort["market_brier_score"],
            "roi": cohort["roi"],
            "stability_status": cohort["stability_status"],
            "buckets": {
                dimension: stratified[dimension] for dimension in dimensions
            },
        })
    return {
        "cohort_count": len(summaries),
        "settled_order_count": sum(
            int(summary["settled_orders"]) for summary in summaries
        ),
        "invalid_entry_order_count": sum(
            int(cohort["orders"]["signals"]) for cohort in isolated
        ),
        "invalid_entry_settled_order_count": sum(
            int(cohort["settled_orders"]) for cohort in isolated
        ),
        "cohorts": summaries,
    }


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
                      observation.source_frame_sha256,
                      observation.source_frame_bytes,
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
        try:
            verify_registered_vision_frame(
                connection,
                str(row[2]),
                expected_sha256=str(row[3]),
                expected_bytes=int(row[4]),
            )
        except (RuntimeError, TypeError, ValueError, sqlite3.Error):
            continue
        clock = _finite_or_none(row[5])
        draft = _finite_or_none(row[6])
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


def _order_summary(
    rows: Sequence[Mapping[str, object]], *, score: bool
) -> dict[str, object]:
    summary = dict(shadow_summary(rows))
    if score:
        return summary
    for field in ("stake_units", "return_units", "pnl_units", "roi"):
        summary[field] = None
    return summary


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
        "kill_deficit_bucket": lambda record: _kill_deficit_bucket(
            record.kill_deficit
        ),
        "net_worth_deficit_bucket": lambda record: _net_worth_deficit_bucket(
            record.net_worth_deficit_min,
            record.net_worth_deficit_max,
        ),
        "rosh_underdog_probability_bucket": lambda record: (
            _rosh_probability_bucket(record.rosh_underdog_probability)
        ),
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


def _kill_deficit_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value <= 0.0:
        return "<=0"
    if value < 2.0:
        return "1"
    if value < 5.0:
        return "2-4"
    if value < 8.0:
        return "5-7"
    if value <= 10.0:
        return "8-10"
    return "11+"


def _net_worth_deficit_bucket(minimum: object, maximum: object) -> str:
    if type(minimum) is not int or type(maximum) is not int:
        return "unknown"
    if minimum >= 0:
        direction = "underdog_deficit"
        bucket_minimum, bucket_maximum = minimum, maximum
    elif maximum <= 0:
        direction = "underdog_ahead"
        bucket_minimum, bucket_maximum = -maximum, -minimum
    else:
        return "unknown"
    if (
        bucket_minimum < 0
        or bucket_minimum % 1_000 != 0
        or bucket_maximum != bucket_minimum + 999
    ):
        return "unknown"
    label = "<1k" if bucket_minimum == 0 else f"{bucket_minimum // 1_000}k"
    return f"{direction}:{label}"


def _rosh_probability_bucket(value: float | None) -> str:
    if value is None or not 0.0 <= value <= 1.0:
        return "unknown"
    if value < 0.3:
        return "<0.30"
    if value <= 0.5:
        return "0.30-0.50"
    if value < 0.7:
        return "0.50-0.70"
    return "0.70+"


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
                      WHERE EXISTS (
                            SELECT 1
                              FROM odds_transport_observations AS transport
                             WHERE transport.raybet_match_id=
                                   decision.raybet_match_id
                               AND transport.observed_at=decision.decided_at
                               AND (
                                   decision.vision_transport_key IS NULL
                                   OR transport.observation_key=
                                      decision.vision_transport_key
                               )
                               AND transport.source='direct'
                      )
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
                      WHERE EXISTS (
                            SELECT 1
                              FROM odds_transport_observations AS transport
                             WHERE transport.observation_key=o.signal_transport_key
                               AND transport.raybet_match_id=o.raybet_match_id
                               AND transport.observed_at=o.signal_transport_at
                               AND transport.source='direct'
                      )
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, required=True)
    parser.add_argument("--revocation-ledger", type=Path)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--revocation-anchor", type=Path)
    parser.add_argument("--revocation-anchor-sha256")
    parser.add_argument("--pair-baseline-manifest", type=Path)
    parser.add_argument("--pair-baseline-manifest-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    connection = connect(args.database, read_only=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            raise RuntimeError("report connection did not enter query_only mode")
        governance_fields = (
            args.revocation_ledger,
            args.raw_root,
            args.revocation_anchor,
            args.revocation_anchor_sha256,
            args.pair_baseline_manifest,
            args.pair_baseline_manifest_sha256,
        )
        if not any(value is not None for value in governance_fields):
            report = build_report(connection)
        else:
            if any(value is None for value in governance_fields):
                raise ValueError("revocation report configuration is incomplete")
            report = build_report(
                connection,
                revocation_config=MilestoneRevocationConfig(
                    root=args.revocation_ledger,
                    database_path=args.database,
                    raw_root=args.raw_root,
                    expected_anchor=args.revocation_anchor,
                    expected_anchor_hash=args.revocation_anchor_sha256,
                    pair_manifest=args.pair_baseline_manifest,
                    expected_pair_manifest_hash=(
                        args.pair_baseline_manifest_sha256
                    ),
                ),
            )
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
