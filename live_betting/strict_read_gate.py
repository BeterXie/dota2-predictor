"""Read-only causal validation for persisted strict mapping references."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


_STRICT_READ_SCHEMA = {
    "strict_live_map_mappings": frozenset({
        "mapping_id",
        "raybet_match_id",
        "map_number",
        "event_id",
        "team_one_id",
        "team_two_id",
        "canonical_team_one_id",
        "canonical_team_one_name",
        "canonical_team_two_id",
        "canonical_team_two_name",
        "raybet_identity_hash",
        "canonical_identity_hash",
        "crosswalk_evidence_hash",
        "evidence_hash",
        "raybet_metadata_updated_at",
        "acceptance_mode",
        "automatic_approval_id",
        "accepted_at",
        "recorded_at",
    }),
    "strict_live_map_mapping_invalidations": frozenset({
        "invalidation_id",
        "mapping_id",
    }),
    "strict_live_automatic_evidence_approvals": frozenset({
        "approval_id",
        "source_mapping_id",
        "raybet_match_id",
        "event_id",
        "team_one_id",
        "team_two_id",
        "canonical_team_one_id",
        "canonical_team_two_id",
        "raybet_identity_hash",
        "canonical_identity_hash",
        "crosswalk_evidence_hash",
        "evidence_hash",
        "approved_at",
        "recorded_at",
    }),
    "strict_live_mapping_impacts": frozenset({
        "mapping_id",
        "invalidation_id",
        "dependent_type",
        "dependent_key",
    }),
}
_DEPENDENT_TYPES = frozenset({
    "strategy_decision",
    "research_prediction",
    "shadow_order",
})


@dataclass(frozen=True)
class StrictReadGate:
    """SQL predicates and availability for one persisted dependent type."""

    available: bool
    unknown_reasons: tuple[str, ...]
    included_sql: str
    invalidated_sql: str
    unverifiable_sql: str


def strict_read_gate(
    connection: sqlite3.Connection,
    *,
    mapping_id_sql: str,
    raybet_match_id_sql: str,
    map_number_sql: str,
    signal_at_sql: str,
    dependent_type: str,
    dependent_key_sql: str,
    legacy_mapping_sql: str | None = None,
) -> StrictReadGate:
    """Build a fail-closed gate for an internally defined SQL row alias.

    Callers own the SQL expressions; this function is deliberately not a
    general query builder. A genuine NULL mapping keeps legacy behavior. Any
    non-NULL reference must prove its full mapping and approval lineage as of
    the signal timestamp.
    """

    if dependent_type not in _DEPENDENT_TYPES:
        raise ValueError(f"unsupported strict dependent type: {dependent_type}")
    legacy_sql = legacy_mapping_sql or f"({mapping_id_sql}) IS NULL"
    mapped_sql = f"NOT ({legacy_sql})"
    unknown_reasons = _strict_schema_reasons(connection)
    if unknown_reasons:
        return StrictReadGate(
            available=False,
            unknown_reasons=unknown_reasons,
            included_sql=f"({legacy_sql})",
            invalidated_sql="0",
            unverifiable_sql=f"({mapped_sql})",
        )

    mapping_id_valid = (
        f"(typeof({mapping_id_sql})='integer' AND ({mapping_id_sql})>0)"
    )
    impact = (
        "EXISTS ("
        "SELECT 1 FROM strict_live_mapping_impacts AS strict_impact "
        f"WHERE strict_impact.dependent_type='{dependent_type}' "
        f"AND strict_impact.dependent_key={dependent_key_sql})"
    )
    direct_invalidation = (
        "EXISTS ("
        "SELECT 1 FROM strict_live_map_mapping_invalidations AS strict_direct "
        f"WHERE strict_direct.mapping_id={mapping_id_sql})"
    )
    source_invalidation = (
        "EXISTS ("
        "SELECT 1 FROM strict_live_map_mappings AS strict_automatic "
        "JOIN strict_live_automatic_evidence_approvals AS strict_source_approval "
        "ON strict_source_approval.approval_id="
        "strict_automatic.automatic_approval_id "
        "JOIN strict_live_map_mapping_invalidations AS strict_source_invalidation "
        "ON strict_source_invalidation.mapping_id="
        "strict_source_approval.source_mapping_id "
        f"WHERE strict_automatic.mapping_id={mapping_id_sql})"
    )
    invalidated = (
        f"(({mapped_sql}) AND (({impact}) OR ({direct_invalidation}) "
        f"OR ({source_invalidation})))"
    )
    signal_aware = _aware_timestamp_sql(signal_at_sql)
    mapping_accepted_aware = _aware_timestamp_sql("strict_mapping.accepted_at")
    mapping_recorded_aware = _aware_timestamp_sql("strict_mapping.recorded_at")
    metadata_aware = _aware_timestamp_sql(
        "strict_mapping.raybet_metadata_updated_at"
    )
    approval_approved_aware = _aware_timestamp_sql(
        "strict_approval.approved_at"
    )
    approval_recorded_aware = _aware_timestamp_sql(
        "strict_approval.recorded_at"
    )
    source_accepted_aware = _aware_timestamp_sql("strict_source.accepted_at")
    source_recorded_aware = _aware_timestamp_sql("strict_source.recorded_at")
    source_metadata_aware = _aware_timestamp_sql(
        "strict_source.raybet_metadata_updated_at"
    )
    valid_mapping = f"""EXISTS (
        SELECT 1
          FROM strict_live_map_mappings AS strict_mapping
          LEFT JOIN strict_live_automatic_evidence_approvals AS strict_approval
            ON strict_approval.approval_id=strict_mapping.automatic_approval_id
          LEFT JOIN strict_live_map_mappings AS strict_source
            ON strict_source.mapping_id=strict_approval.source_mapping_id
         WHERE strict_mapping.mapping_id={mapping_id_sql}
           AND strict_mapping.raybet_match_id={raybet_match_id_sql}
           AND strict_mapping.map_number={map_number_sql}
           AND {signal_aware}
           AND {mapping_accepted_aware}
           AND {mapping_recorded_aware}
           AND {metadata_aware}
           AND julianday(strict_mapping.accepted_at)<=julianday({signal_at_sql})
           AND julianday(strict_mapping.recorded_at)<=julianday({signal_at_sql})
           AND julianday(strict_mapping.raybet_metadata_updated_at)
               <=julianday({signal_at_sql})
           AND julianday(strict_mapping.accepted_at)
               <=julianday(strict_mapping.recorded_at)
           AND julianday(strict_mapping.raybet_metadata_updated_at)
               <=julianday(strict_mapping.recorded_at)
           AND (
                (strict_mapping.acceptance_mode='manual_exact'
                 AND strict_mapping.automatic_approval_id IS NULL)
                OR
                (strict_mapping.acceptance_mode='automatic_exact'
                 AND strict_mapping.automatic_approval_id IS NOT NULL
                 AND strict_approval.approval_id IS NOT NULL
                 AND strict_source.mapping_id IS NOT NULL
                 AND strict_source.acceptance_mode='manual_exact'
                 AND strict_source.automatic_approval_id IS NULL
                 AND strict_approval.raybet_match_id=
                     strict_mapping.raybet_match_id
                 AND strict_approval.event_id=strict_mapping.event_id
                 AND strict_approval.team_one_id=strict_mapping.team_one_id
                 AND strict_approval.team_two_id=strict_mapping.team_two_id
                 AND strict_approval.canonical_team_one_id=
                     strict_mapping.canonical_team_one_id
                 AND strict_approval.canonical_team_two_id=
                     strict_mapping.canonical_team_two_id
                 AND strict_approval.raybet_identity_hash=
                     strict_mapping.raybet_identity_hash
                 AND strict_approval.canonical_identity_hash=
                     strict_mapping.canonical_identity_hash
                 AND strict_approval.crosswalk_evidence_hash=
                     strict_mapping.crosswalk_evidence_hash
                 AND strict_approval.evidence_hash=strict_mapping.evidence_hash
                 AND {approval_approved_aware}
                 AND {approval_recorded_aware}
                 AND julianday(strict_approval.approved_at)
                     <=julianday({signal_at_sql})
                 AND julianday(strict_approval.recorded_at)
                     <=julianday({signal_at_sql})
                 AND julianday(strict_approval.approved_at)
                     <=julianday(strict_approval.recorded_at)
                 AND julianday(strict_approval.recorded_at)
                     <=julianday(strict_mapping.accepted_at)
                 AND {source_accepted_aware}
                 AND {source_recorded_aware}
                 AND {source_metadata_aware}
                 AND julianday(strict_source.accepted_at)
                     <=julianday(strict_approval.recorded_at)
                 AND julianday(strict_source.recorded_at)
                     <=julianday(strict_approval.recorded_at)
                 AND julianday(strict_source.raybet_metadata_updated_at)
                     <=julianday(strict_approval.recorded_at))
           )
    )"""
    verified = f"(({mapping_id_valid}) AND ({valid_mapping}))"
    unverifiable = (
        f"(({mapped_sql}) AND NOT ({invalidated}) AND NOT ({verified}))"
    )
    included = (
        f"(({legacy_sql}) OR (NOT ({invalidated}) AND ({verified})))"
    )
    return StrictReadGate(
        available=True,
        unknown_reasons=(),
        included_sql=included,
        invalidated_sql=invalidated,
        unverifiable_sql=unverifiable,
    )


def table_has_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: frozenset[str] | set[str],
) -> bool:
    """Return whether a table exists with every requested column."""

    try:
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
    except sqlite3.OperationalError:
        return False
    return bool(existing) and set(columns).issubset(existing)


def _aware_timestamp_sql(value_sql: str) -> str:
    """Match the timezone-aware ISO contract before SQLite date coercion."""

    return (
        f"(typeof({value_sql})='text' AND julianday({value_sql}) IS NOT NULL "
        f"AND (({value_sql}) GLOB '*Z' "
        f"OR ({value_sql}) GLOB '*[+-][0-9][0-9]:[0-9][0-9]'))"
    )


def _strict_schema_reasons(connection: sqlite3.Connection) -> tuple[str, ...]:
    reasons: list[str] = []
    for table, required_columns in _STRICT_READ_SCHEMA.items():
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        except sqlite3.OperationalError:
            return ("strict_mapping_schema_inspection_failed",)
        if exists is None:
            reasons.append(f"{table}_table_missing")
        elif not table_has_columns(connection, table, required_columns):
            reasons.append(f"{table}_columns_missing")
    return tuple(reasons)


__all__ = ["StrictReadGate", "strict_read_gate", "table_has_columns"]
