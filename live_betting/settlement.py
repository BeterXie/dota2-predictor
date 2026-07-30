"""Deterministic settlement for supported Dota 2 markets."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from database.session import DatabaseRow, PostgresSession

from .models import Market
from .vision_frame_registry import verify_bound_order_vision_frame


@dataclass(frozen=True)
class MapResult:
    winner: str
    team_one_kills: int
    team_two_kills: int
    duration_minutes: float
    first_to_kills: dict[int, str]


@dataclass(frozen=True)
class AuthoritativeSettlement:
    order_key: str
    raybet_match_id: str
    map_number: int
    strict_mapping_id: int
    dota_match_id: int
    winner_side: str
    result: str
    fill_price: float
    stake: float
    return_units: float
    return_amount: float
    settled_at: datetime
    map_result_evidence_ref: str
    raybet_evidence_ref: str
    opendota_evidence_ref: str
    raybet_evidence_id: int
    opendota_evidence_id: int
    raybet_observed_at: datetime
    opendota_observed_at: datetime
    first_usable_at: datetime
    reconciliation_updated_at: datetime


class SettlementAuthorityError(ValueError):
    """Raised when a formal settlement cannot be reproduced from authority."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _table_columns(connection: PostgresSession, table: str) -> set[str]:
    """Return a PostgreSQL table's columns without mutating its schema."""

    try:
        return {
            str(row[0])
            for row in connection.execute(
                """SELECT column_name
                     FROM information_schema.columns
                    WHERE table_schema=current_schema() AND table_name=?
                    ORDER BY ordinal_position""",
                (table,),
            )
        }
    except SQLAlchemyError:
        return set()


def _authority_time(value: Any, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise SettlementAuthorityError(reason) from error
    if parsed.tzinfo is None:
        raise SettlementAuthorityError(reason)
    return parsed.astimezone(timezone.utc)


def _authority_float(value: Any, reason: str, *, minimum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= minimum
    ):
        raise SettlementAuthorityError(reason)
    return float(value)


def _source_evidence(
    connection: PostgresSession,
    *,
    raybet_match_id: str,
    map_number: int,
    dota_match_id: int,
    winner_side: str,
    raybet_evidence_ref: str,
    opendota_evidence_ref: str,
    raybet_evidence_id: int,
    opendota_evidence_id: int,
    raybet_observed_at: datetime,
    opendota_observed_at: datetime,
    first_usable_at: datetime,
    filled_at: datetime | None,
    strict_mapping_id: int,
) -> dict[str, DatabaseRow]:
    evidence_columns = _table_columns(connection, "settlement_result_evidence")
    if not evidence_columns:
        raise SettlementAuthorityError("settlement_source_evidence_schema_missing")
    observed_at_sql = (
        "observed_at" if "observed_at" in evidence_columns else "NULL"
    )
    rows = connection.execute(
        f"""SELECT evidence_id, source, status, winner_side, evidence_ref,
                  facts_json, dota_match_id, {observed_at_sql} AS observed_at,
                  first_usable_at, raybet_audit_key, raybet_transport_key,
                  raybet_response_state_hash, raybet_response_artifact_hash,
                  opendota_artifact_id, opendota_observation_id,
                  opendota_content_hash
             FROM settlement_result_evidence
            WHERE raybet_match_id=? AND map_number=?
              AND ((source='raybet' AND evidence_ref=?)
                OR (source='opendota' AND evidence_ref=?))""",
        (
            raybet_match_id,
            map_number,
            raybet_evidence_ref,
            opendota_evidence_ref,
        ),
    ).fetchall()
    evidence = {str(row["source"]): row for row in rows}
    expected_refs = {
        "raybet": raybet_evidence_ref,
        "opendota": opendota_evidence_ref,
    }
    expected_ids = {
        "raybet": raybet_evidence_id,
        "opendota": opendota_evidence_id,
    }
    expected_observed = {
        "raybet": raybet_observed_at,
        "opendota": opendota_observed_at,
    }
    if len(rows) != 2 or set(evidence) != set(expected_refs):
        raise SettlementAuthorityError("settlement_source_evidence_missing")
    for source, expected_ref in expected_refs.items():
        row = evidence[source]
        if (
            str(row["status"]) != "confirmed"
            or type(row["evidence_id"]) is not int
            or int(row["evidence_id"]) != expected_ids[source]
            or str(row["winner_side"]) != winner_side
            or str(row["evidence_ref"]) != expected_ref
            or type(row["dota_match_id"]) is not int
            or int(row["dota_match_id"]) != dota_match_id
        ):
            raise SettlementAuthorityError("settlement_source_evidence_mismatch")
        try:
            facts = json.loads(str(row["facts_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SettlementAuthorityError(
                "settlement_source_evidence_invalid"
            ) from error
        if not isinstance(facts, dict):
            raise SettlementAuthorityError("settlement_source_evidence_invalid")
        expected_facts = {
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "strict_mapping_id": strict_mapping_id,
            "winner_side": winner_side,
            "dota_match_id": dota_match_id,
        }
        if any(facts.get(field) != expected for field, expected in expected_facts.items()):
            raise SettlementAuthorityError("settlement_source_evidence_mismatch")
        observed_at = _authority_time(
            row["observed_at"], "settlement_source_evidence_time_invalid"
        )
        usable_at = _authority_time(
            row["first_usable_at"],
            "settlement_source_evidence_time_invalid",
        )
        if (
            observed_at != expected_observed[source]
            or usable_at < observed_at
            or (source == "raybet" and usable_at != observed_at)
            or (filled_at is not None and usable_at <= filled_at)
        ):
            raise SettlementAuthorityError(
                "settlement_source_evidence_time_mismatch"
            )
    usable_times = {
        source: _authority_time(
            row["first_usable_at"],
            "settlement_source_evidence_time_invalid",
        )
        for source, row in evidence.items()
    }
    if max(usable_times.values()) != first_usable_at:
        raise SettlementAuthorityError(
            "settlement_source_evidence_time_mismatch"
        )
    return evidence


def resolve_authoritative_settlement(
    connection: PostgresSession,
    order_key: str,
) -> AuthoritativeSettlement:
    """Resolve one formal settlement entirely from immutable database facts."""

    order_columns = _table_columns(connection, "shadow_orders")
    if not order_columns or "filled_at" not in order_columns:
        raise SettlementAuthorityError("settlement_order_schema_missing")
    schema_reasons = {
        "shadow_map_attempts": "settlement_attempt_schema_missing",
        "strict_live_map_mappings": "settlement_strict_mapping_schema_missing",
        "settlement_reconciliations": "settlement_reconciliation_schema_missing",
        "map_results": "settlement_map_result_schema_missing",
        "settlement_result_evidence": "settlement_source_evidence_schema_missing",
    }
    for table, required in (
        (
            "shadow_map_attempts",
            {"order_key", "raybet_match_id", "map_number", "status"},
        ),
        ("strict_live_map_mappings", {"mapping_id", "raybet_match_id", "map_number"}),
        (
            "settlement_reconciliations",
            {
                "raybet_match_id",
                "map_number",
                "strict_mapping_id",
                "dota_match_id",
                "raybet_winner_side",
                "opendota_winner_side",
                "raybet_evidence_ref",
                "opendota_evidence_ref",
                "evidence_ref",
                "raybet_evidence_id",
                "opendota_evidence_id",
                "raybet_observed_at",
                "opendota_observed_at",
                "first_usable_at",
                "status",
                "first_observed_at",
                "updated_at",
            },
        ),
        (
            "map_results",
            {
                "raybet_match_id",
                "map_number",
                "strict_mapping_id",
                "dota_match_id",
                "winner_side",
                "evidence_ref",
                "reconciliation_ref",
                "raybet_evidence_id",
                "opendota_evidence_id",
                "raybet_evidence_ref",
                "opendota_evidence_ref",
                "raybet_observed_at",
                "opendota_observed_at",
                "first_usable_at",
                "settled_at",
            },
        ),
        (
            "settlement_result_evidence",
            {
                "raybet_match_id",
                "map_number",
                "dota_match_id",
                "source",
                "status",
                "winner_side",
                "evidence_ref",
                "facts_json",
                "observed_at",
            },
        ),
    ):
        if not required.issubset(_table_columns(connection, table)):
            raise SettlementAuthorityError(schema_reasons[table])
    order_columns = _table_columns(connection, "shadow_orders")
    has_market_source_lineage = {
        "signal_transport_key",
        "signal_transport_at",
    }.issubset(order_columns)
    signal_transport_sql = (
        "orders.signal_transport_key, orders.signal_transport_at,"
        if has_market_source_lineage
        else "NULL AS signal_transport_key, NULL AS signal_transport_at,"
    )
    filled_at_sql = "orders.filled_at"
    order = connection.execute(
        f"""SELECT orders.raybet_match_id, orders.strict_mapping_id,
                   orders.market_key, orders.signal_outcome_key,
                   orders.fill_price, orders.stake,
                   {signal_transport_sql}
                   {filled_at_sql} AS filled_at,
                  orders.status AS order_status,
                  attempt.raybet_match_id AS attempt_match_id,
                  attempt.map_number, attempt.status AS attempt_status
             FROM shadow_orders AS orders
             LEFT JOIN shadow_map_attempts AS attempt
               ON attempt.order_key=orders.order_key
            WHERE orders.order_key=?""",
        (order_key,),
    ).fetchone()
    if order is None:
        raise SettlementAuthorityError("settlement_order_missing")
    if has_market_source_lineage:
        try:
            direct_signal = connection.execute(
                """SELECT 1 FROM odds_transport_observations
                    WHERE observation_key=? AND source='direct'
                      AND raybet_match_id=? AND observed_at=?""",
                (
                    order["signal_transport_key"],
                    order["raybet_match_id"],
                    order["signal_transport_at"],
                ),
            ).fetchone()
        except SQLAlchemyError as error:
            raise SettlementAuthorityError(
                "settlement_order_market_source_invalid"
            ) from error
        if direct_signal is None:
            raise SettlementAuthorityError("settlement_order_market_source_invalid")
    try:
        verify_bound_order_vision_frame(connection, order_key)
    except (RuntimeError, TypeError, ValueError, SQLAlchemyError) as error:
        raise SettlementAuthorityError(
            "settlement_vision_frame_authority_invalid"
        ) from error
    if (
        str(order["order_status"]) != "filled"
        or str(order["attempt_status"]) != "filled"
        or str(order["attempt_match_id"] or "") != str(order["raybet_match_id"])
    ):
        raise SettlementAuthorityError("settlement_order_not_filled")
    strict_mapping_id = order["strict_mapping_id"]
    map_number = order["map_number"]
    if type(strict_mapping_id) is not int or strict_mapping_id <= 0:
        raise SettlementAuthorityError("settlement_strict_mapping_missing")
    if type(map_number) is not int or map_number <= 0:
        raise SettlementAuthorityError("settlement_map_identity_missing")
    raybet_match_id = str(order["raybet_match_id"])
    mapping = connection.execute(
        """SELECT 1 FROM strict_live_map_mappings
            WHERE mapping_id=? AND raybet_match_id=? AND map_number=?""",
        (strict_mapping_id, raybet_match_id, map_number),
    ).fetchone()
    if mapping is None:
        raise SettlementAuthorityError("settlement_strict_mapping_mismatch")

    first_observed_at_sql = "first_observed_at"
    reconciliation = connection.execute(
        f"""SELECT strict_mapping_id, dota_match_id, raybet_winner_side,
                  opendota_winner_side, raybet_evidence_ref,
                  opendota_evidence_ref, evidence_ref,
                  raybet_evidence_id, opendota_evidence_id,
                  raybet_observed_at, opendota_observed_at,
                  first_usable_at, status, {first_observed_at_sql}
                      AS first_observed_at, updated_at
             FROM settlement_reconciliations
            WHERE raybet_match_id=? AND map_number=?
              AND strict_mapping_id=?""",
        (raybet_match_id, map_number, strict_mapping_id),
    ).fetchone()
    if reconciliation is None:
        any_reconciliation = connection.execute(
            """SELECT strict_mapping_id FROM settlement_reconciliations
                WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        ).fetchone()
        raise SettlementAuthorityError(
            "settlement_reconciliation_mismatch"
            if any_reconciliation is not None
            else "settlement_reconciliation_missing"
        )
    if str(reconciliation["status"]) != "confirmed":
        raise SettlementAuthorityError("settlement_reconciliation_not_confirmed")
    if (
        type(reconciliation["strict_mapping_id"]) is not int
        or int(reconciliation["strict_mapping_id"]) != strict_mapping_id
        or type(reconciliation["dota_match_id"]) is not int
        or int(reconciliation["dota_match_id"]) <= 0
    ):
        raise SettlementAuthorityError("settlement_reconciliation_mismatch")
    dota_match_id = int(reconciliation["dota_match_id"])
    winner_side = str(reconciliation["opendota_winner_side"])
    if (
        winner_side not in {"team_one", "team_two"}
        or str(reconciliation["raybet_winner_side"]) != winner_side
    ):
        raise SettlementAuthorityError("settlement_reconciliation_winner_mismatch")
    raybet_evidence_ref = str(reconciliation["raybet_evidence_ref"] or "")
    opendota_evidence_ref = str(reconciliation["opendota_evidence_ref"] or "")
    if not raybet_evidence_ref or not opendota_evidence_ref:
        raise SettlementAuthorityError("settlement_reconciliation_evidence_missing")
    expected_result_ref = (
        f"settlement-reconciliation:{raybet_match_id}:map:{map_number}"
    )
    if str(reconciliation["evidence_ref"] or "") != expected_result_ref:
        raise SettlementAuthorityError("settlement_reconciliation_evidence_missing")
    if (
        type(reconciliation["raybet_evidence_id"]) is not int
        or type(reconciliation["opendota_evidence_id"]) is not int
    ):
        raise SettlementAuthorityError("settlement_reconciliation_evidence_missing")
    raybet_evidence_id = int(reconciliation["raybet_evidence_id"])
    opendota_evidence_id = int(reconciliation["opendota_evidence_id"])

    map_result = connection.execute(
        """SELECT strict_mapping_id, dota_match_id, winner_side,
                  team_one_kills, team_two_kills, duration_seconds,
                  evidence_ref, reconciliation_ref, raybet_evidence_id,
                  opendota_evidence_id, raybet_evidence_ref,
                  opendota_evidence_ref, raybet_observed_at,
                  opendota_observed_at, first_usable_at, settled_at
             FROM map_results
            WHERE raybet_match_id=? AND map_number=?
              AND strict_mapping_id=?""",
        (raybet_match_id, map_number, strict_mapping_id),
    ).fetchone()
    if map_result is None:
        any_map_result = connection.execute(
            """SELECT strict_mapping_id FROM map_results
                WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        ).fetchone()
        raise SettlementAuthorityError(
            "settlement_map_result_mismatch"
            if any_map_result is not None
            else "settlement_map_result_missing"
        )
    if (
        type(map_result["strict_mapping_id"]) is not int
        or int(map_result["strict_mapping_id"]) != strict_mapping_id
        or type(map_result["dota_match_id"]) is not int
        or int(map_result["dota_match_id"]) != dota_match_id
        or str(map_result["winner_side"]) != winner_side
    ):
        raise SettlementAuthorityError("settlement_map_result_mismatch")
    map_result_evidence_ref = str(map_result["evidence_ref"] or "")
    if map_result_evidence_ref != expected_result_ref:
        raise SettlementAuthorityError("settlement_map_result_evidence_mismatch")

    source_binding = {
        "reconciliation_ref": expected_result_ref,
        "raybet_evidence_id": raybet_evidence_id,
        "opendota_evidence_id": opendota_evidence_id,
        "raybet_evidence_ref": raybet_evidence_ref,
        "opendota_evidence_ref": opendota_evidence_ref,
        "raybet_observed_at": reconciliation["raybet_observed_at"],
        "opendota_observed_at": reconciliation["opendota_observed_at"],
        "first_usable_at": reconciliation["first_usable_at"],
    }
    if any(map_result[field] != value for field, value in source_binding.items()):
        raise SettlementAuthorityError("settlement_map_result_evidence_mismatch")

    settled_at = _authority_time(
        map_result["settled_at"], "settlement_map_result_time_invalid"
    )
    if order["filled_at"] is None:
        raise SettlementAuthorityError("settlement_order_time_invalid")
    filled_at = _authority_time(
        order["filled_at"], "settlement_order_time_invalid"
    )
    if filled_at >= settled_at:
        raise SettlementAuthorityError("settlement_time_order_invalid")
    raybet_observed_at = _authority_time(
        reconciliation["raybet_observed_at"],
        "settlement_reconciliation_time_invalid",
    )
    opendota_observed_at = _authority_time(
        reconciliation["opendota_observed_at"],
        "settlement_reconciliation_time_invalid",
    )
    first_usable_at = _authority_time(
        reconciliation["first_usable_at"],
        "settlement_reconciliation_time_invalid",
    )
    first_observed_at = _authority_time(
        reconciliation["first_observed_at"],
        "settlement_reconciliation_time_invalid",
    )
    if settled_at != first_usable_at:
        raise SettlementAuthorityError("settlement_reconciliation_time_mismatch")
    reconciliation_updated_at = _authority_time(
        reconciliation["updated_at"], "settlement_reconciliation_time_invalid"
    )
    if (
        first_observed_at > first_usable_at
        or reconciliation_updated_at < first_usable_at
    ):
        raise SettlementAuthorityError("settlement_reconciliation_time_mismatch")

    _source_evidence(
        connection,
        raybet_match_id=raybet_match_id,
        map_number=map_number,
        dota_match_id=dota_match_id,
        winner_side=winner_side,
        raybet_evidence_ref=raybet_evidence_ref,
        opendota_evidence_ref=opendota_evidence_ref,
        raybet_evidence_id=raybet_evidence_id,
        opendota_evidence_id=opendota_evidence_id,
        raybet_observed_at=raybet_observed_at,
        opendota_observed_at=opendota_observed_at,
        first_usable_at=first_usable_at,
        filled_at=filled_at,
        strict_mapping_id=strict_mapping_id,
    )

    parts = str(order["market_key"]).split("|", 3)
    if len(parts) != 4:
        raise SettlementAuthorityError("settlement_market_identity_invalid")
    market_type, period, side, line = parts
    if (
        market_type != "winner"
        or period != f"map_{map_number}"
        or side not in {"team_one", "team_two"}
        or line
        or str(order["signal_outcome_key"] or "") != side
    ):
        raise SettlementAuthorityError("settlement_market_identity_invalid")
    fill_price = _authority_float(
        order["fill_price"], "settlement_fill_price_invalid", minimum=1.0
    )
    stake = _authority_float(order["stake"], "settlement_stake_invalid", minimum=0.0)
    market = Market(market_type, period, side, None, side, True)
    result, return_units = settle(
        market,
        MapResult(winner_side, 0, 0, 0.0, {}),
        fill_price,
    )
    return AuthoritativeSettlement(
        order_key=order_key,
        raybet_match_id=raybet_match_id,
        map_number=map_number,
        strict_mapping_id=strict_mapping_id,
        dota_match_id=dota_match_id,
        winner_side=winner_side,
        result=result,
        fill_price=fill_price,
        stake=stake,
        return_units=return_units,
        return_amount=return_units * stake,
        settled_at=settled_at,
        map_result_evidence_ref=map_result_evidence_ref,
        raybet_evidence_ref=raybet_evidence_ref,
        opendota_evidence_ref=opendota_evidence_ref,
        raybet_evidence_id=raybet_evidence_id,
        opendota_evidence_id=opendota_evidence_id,
        raybet_observed_at=raybet_observed_at,
        opendota_observed_at=opendota_observed_at,
        first_usable_at=first_usable_at,
        reconciliation_updated_at=reconciliation_updated_at,
    )


_AUTHORITY_SNAPSHOT_FIELDS = (
    "order_key",
    "raybet_match_id",
    "map_number",
    "strict_mapping_id",
    "dota_match_id",
    "winner_side",
    "fill_price",
    "stake_units",
    "derived_result",
    "derived_return_units",
    "derived_return_amount",
    "map_result_evidence_ref",
    "raybet_evidence_ref",
    "opendota_evidence_ref",
    "raybet_evidence_id",
    "opendota_evidence_id",
    "raybet_observed_at",
    "opendota_observed_at",
    "first_usable_at",
    "reconciliation_updated_at",
    "settled_at",
)


def _authority_snapshot_values(
    authority: AuthoritativeSettlement,
) -> tuple[object, ...]:
    return (
        authority.order_key,
        authority.raybet_match_id,
        authority.map_number,
        authority.strict_mapping_id,
        authority.dota_match_id,
        authority.winner_side,
        authority.fill_price,
        authority.stake,
        authority.result,
        authority.return_units,
        authority.return_amount,
        authority.map_result_evidence_ref,
        authority.raybet_evidence_ref,
        authority.opendota_evidence_ref,
        authority.raybet_evidence_id,
        authority.opendota_evidence_id,
        authority.raybet_observed_at.isoformat(),
        authority.opendota_observed_at.isoformat(),
        authority.first_usable_at.isoformat(),
        authority.reconciliation_updated_at.isoformat(),
        authority.settled_at.isoformat(),
    )


def persist_authoritative_settlement_snapshot(
    connection: PostgresSession,
    authority: AuthoritativeSettlement,
) -> bool:
    """Persist the exact inputs used by a new formal settlement."""

    columns = _table_columns(connection, "settlement_authority")
    if not set(_AUTHORITY_SNAPSHOT_FIELDS).issubset(columns):
        raise SettlementAuthorityError("settlement_authority_schema_missing")
    values = list(_authority_snapshot_values(authority))
    insert_fields = list(_AUTHORITY_SNAPSHOT_FIELDS)
    for timestamp_field in ("recorded_at", "created_at"):
        if timestamp_field in columns:
            insert_fields.append(timestamp_field)
            values.append(authority.reconciliation_updated_at.isoformat())
    placeholders = ", ".join("?" for _ in insert_fields)
    cursor = connection.execute(
        f"INSERT INTO settlement_authority "
        f"({', '.join(insert_fields)}) VALUES ({placeholders}) "
        "ON CONFLICT DO NOTHING",
        tuple(values),
    )
    if cursor.rowcount == 1:
        return True
    existing = connection.execute(
        f"SELECT {', '.join(_AUTHORITY_SNAPSHOT_FIELDS)} "
        "FROM settlement_authority WHERE order_key=?",
        (authority.order_key,),
    ).fetchone()
    if existing is None or tuple(existing) != _authority_snapshot_values(authority):
        raise SettlementAuthorityError("settlement_authority_snapshot_conflict")
    return False


def record_settlement_authority_review(
    connection: PostgresSession,
    order_key: str,
    reason: str,
    *,
    actor: str,
) -> bool:
    """Append a fail-closed audit row when formal settlement is unavailable."""

    safe_reason = " ".join(str(reason).split())[:500] or "unspecified"
    safe_actor = " ".join(str(actor).split())[:100] or "unknown"
    columns = _table_columns(connection, "settlement_authority_audit")
    required = {"order_key", "status", "reason", "actor", "recorded_at"}
    if not required.issubset(columns):
        return False
    cursor = connection.execute(
        """INSERT INTO settlement_authority_audit
           (order_key, status, reason, actor, recorded_at)
           VALUES (?, 'manual_review', ?, ?, ?)
           ON CONFLICT DO NOTHING""",
        (
            order_key,
            safe_reason,
            safe_actor,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return cursor.rowcount == 1


def settle_authoritative_order(store: Any, order_key: str) -> bool:
    """Insert one formal settlement from database authority in one transaction."""

    with store.transaction():
        existing = store.connection.execute(
            "SELECT 1 FROM settlements WHERE order_key=?", (order_key,)
        ).fetchone()
        if existing is not None:
            reason = persisted_settlement_authority_reason(
                store.connection, order_key
            )
            if reason is not None:
                raise SettlementAuthorityError(reason)
            return False

        authority = resolve_authoritative_settlement(store.connection, order_key)
        persist_authoritative_settlement_snapshot(store.connection, authority)
        # Re-read under the same write transaction. This catches same-connection
        # fault injection and keeps the snapshot, ledger, and outbox indivisible.
        if resolve_authoritative_settlement(store.connection, order_key) != authority:
            raise SettlementAuthorityError("settlement_authority_changed")
        inserted = store.insert_settlement(
            order_key,
            authority.result,
            authority.return_units,
            authority.settled_at,
            authority.map_result_evidence_ref,
        )
        if not inserted:
            raise SettlementAuthorityError("settlement_ledger_write_failed")
        reason = persisted_settlement_authority_reason(store.connection, order_key)
        if reason is not None:
            raise SettlementAuthorityError(reason)
        return True


def persisted_settlement_authority_reason(
    connection: PostgresSession,
    order_key: str,
) -> str | None:
    """Revalidate a persisted formal settlement and its immutable snapshot."""

    try:
        authority = resolve_authoritative_settlement(connection, order_key)
    except SettlementAuthorityError as error:
        return error.reason
    except SQLAlchemyError:
        return "settlement_authority_unavailable"
    try:
        settlement = connection.execute(
            """SELECT result, return_units, settled_at, evidence_ref,
                      review_required
                 FROM settlements WHERE order_key=?""",
            (order_key,),
        ).fetchone()
    except SQLAlchemyError:
        return "settlement_ledger_schema_missing"
    if settlement is None:
        return "settlement_ledger_missing"
    if bool(settlement["review_required"]):
        return "settlement_manual_review"
    try:
        ledger_time = _authority_time(
            settlement["settled_at"], "settlement_ledger_time_invalid"
        )
    except SettlementAuthorityError as error:
        return error.reason
    if (
        str(settlement["result"]) != authority.result
        or isinstance(settlement["return_units"], bool)
        or not isinstance(settlement["return_units"], (int, float))
        or not math.isclose(
            float(settlement["return_units"]),
            authority.return_units,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or ledger_time != authority.settled_at
        or str(settlement["evidence_ref"]) != authority.map_result_evidence_ref
    ):
        return "settlement_ledger_authority_mismatch"
    try:
        snapshot = connection.execute(
            """SELECT raybet_match_id, map_number, strict_mapping_id,
                      dota_match_id, winner_side, fill_price, stake_units,
                      derived_result, derived_return_units,
                      derived_return_amount, map_result_evidence_ref,
                      raybet_evidence_ref, opendota_evidence_ref,
                      raybet_evidence_id, opendota_evidence_id,
                      raybet_observed_at, opendota_observed_at,
                      first_usable_at,
                      reconciliation_updated_at, settled_at
                 FROM settlement_authority WHERE order_key=?""",
            (order_key,),
        ).fetchone()
    except SQLAlchemyError:
        return "settlement_authority_schema_missing"
    if snapshot is None:
        return "settlement_authority_missing"
    scalar_expected = {
        "raybet_match_id": authority.raybet_match_id,
        "map_number": authority.map_number,
        "strict_mapping_id": authority.strict_mapping_id,
        "dota_match_id": authority.dota_match_id,
        "winner_side": authority.winner_side,
        "derived_result": authority.result,
        "map_result_evidence_ref": authority.map_result_evidence_ref,
        "raybet_evidence_ref": authority.raybet_evidence_ref,
        "opendota_evidence_ref": authority.opendota_evidence_ref,
        "raybet_evidence_id": authority.raybet_evidence_id,
        "opendota_evidence_id": authority.opendota_evidence_id,
    }
    if any(snapshot[field] != expected for field, expected in scalar_expected.items()):
        return "settlement_authority_snapshot_mismatch"
    numeric_expected = {
        "fill_price": authority.fill_price,
        "stake_units": authority.stake,
        "derived_return_units": authority.return_units,
        "derived_return_amount": authority.return_amount,
    }
    for field, expected in numeric_expected.items():
        value = snapshot[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not math.isclose(float(value), expected, rel_tol=0.0, abs_tol=1e-12)
        ):
            return "settlement_authority_snapshot_mismatch"
    try:
        snapshot_reconciliation_at = _authority_time(
            snapshot["reconciliation_updated_at"],
            "settlement_authority_snapshot_mismatch",
        )
        snapshot_settled_at = _authority_time(
            snapshot["settled_at"],
            "settlement_authority_snapshot_mismatch",
        )
        snapshot_raybet_observed_at = _authority_time(
            snapshot["raybet_observed_at"],
            "settlement_authority_snapshot_mismatch",
        )
        snapshot_opendota_observed_at = _authority_time(
            snapshot["opendota_observed_at"],
            "settlement_authority_snapshot_mismatch",
        )
        snapshot_first_usable_at = _authority_time(
            snapshot["first_usable_at"],
            "settlement_authority_snapshot_mismatch",
        )
    except SettlementAuthorityError as error:
        return error.reason
    if (
        snapshot_reconciliation_at != authority.reconciliation_updated_at
        or snapshot_settled_at != authority.settled_at
        or snapshot_raybet_observed_at != authority.raybet_observed_at
        or snapshot_opendota_observed_at != authority.opendota_observed_at
        or snapshot_first_usable_at != authority.first_usable_at
    ):
        return "settlement_authority_snapshot_mismatch"
    return None


def reconcile_map_winners(
    *,
    raybet_status: str,
    raybet_winner: str | None,
    opendota_winner: str | None,
) -> tuple[str, str]:
    """Return the fail-closed state for normalized independent map results."""
    if raybet_status == "conflict":
        return "manual_review", "raybet_final_conflict"
    if raybet_status != "confirmed" or raybet_winner is None:
        return "pending", "raybet_final_missing"
    if raybet_winner not in {"team_one", "team_two"}:
        return "manual_review", "raybet_winner_invalid"
    if opendota_winner not in {"team_one", "team_two"}:
        return "pending", "opendota_winner_missing"
    if raybet_winner != opendota_winner:
        return "manual_review", "winner_conflict"
    return "confirmed", "sources_consistent"


def _asian_return(margin: float, line: float, price: float) -> tuple[str, float]:
    adjusted = margin + line
    if line * 2 % 1 == 0:
        if adjusted > 0:
            return "win", price
        if adjusted == 0:
            return "push", 1.0
        return "loss", 0.0
    lower = (line * 2 // 1) / 2
    upper = lower + 0.5
    outcomes = [_asian_return(margin, part, price) for part in (lower, upper)]
    returned = sum(item[1] for item in outcomes) / 2
    labels = {item[0] for item in outcomes}
    if labels == {"win", "push"}:
        return "half_win", returned
    if labels == {"loss", "push"}:
        return "half_loss", returned
    return outcomes[0][0], returned


def settle(market: Market, result: MapResult, price: float) -> tuple[str, float]:
    if market.market_type == "winner":
        won = market.side == result.winner
        return ("win", price) if won else ("loss", 0.0)

    if market.market_type in {"total_kills", "team_total_kills"}:
        if market.line is None or market.side not in {"over", "under"}:
            raise ValueError("invalid total market")
        if market.market_type == "team_total_kills":
            total = (
                result.team_one_kills
                if "team_one" in market.outcome_key
                else result.team_two_kills
            )
        else:
            total = result.team_one_kills + result.team_two_kills
        margin = total - market.line if market.side == "over" else market.line - total
        return _asian_return(margin, 0.0, price)

    if market.market_type == "kill_handicap":
        if market.line is None or market.side not in {"team_one", "team_two"}:
            raise ValueError("invalid kill handicap")
        margin = result.team_one_kills - result.team_two_kills
        if market.side == "team_two":
            margin = -margin
        return _asian_return(margin, market.line, price)

    if market.market_type == "race_to_kills":
        if market.line is None:
            raise ValueError("race target is required")
        won = result.first_to_kills.get(int(market.line)) == market.side
        return ("win", price) if won else ("loss", 0.0)

    if market.market_type == "duration":
        if market.line is None or market.side not in {"over", "under"}:
            raise ValueError("invalid duration market")
        margin = (
            result.duration_minutes - market.line
            if market.side == "over"
            else market.line - result.duration_minutes
        )
        return _asian_return(margin, 0.0, price)

    raise ValueError(f"unsupported market: {market.market_type}")
