"""Transactional notification outbox primitives for simulation events."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping

from sqlalchemy.exc import SQLAlchemyError

from database.session import DatabaseRow, PostgresSession

from .settlement import (
    persisted_settlement_authority_reason,
    record_settlement_authority_review,
)
from .vision_frame_registry import verify_bound_order_vision_frame


CHANNEL_EMAIL = "email"
EVENT_FILLED = "filled"
EVENT_SETTLED = "settled"
EVENT_MONITOR_ALERT = "monitor_alert"
EVENT_MONITOR_RECOVERY = "monitor_recovery"
TEMPLATE_VERSION = "dota2-shadow-email-v2"
MONITOR_TEMPLATE_VERSION = "dota2-monitor-email-v1"
DEFAULT_RECIPIENT = "599084618@qq.com"
RETRY_DELAYS = (60, 300, 1800, 7200, 43200)


class NotificationConflictError(ValueError):
    """Raised when one logical notification key has different immutable data."""


@dataclass(frozen=True)
class OutboxRecord:
    outbox_id: int
    order_key: str
    event_type: str
    channel: str
    payload_json: str
    stats_cutoff_at: datetime
    template_version: str
    recipient: str
    message_id: str
    status: str
    attempt_count: int
    next_attempt_at: datetime
    lease_token: str | None
    lease_until: datetime | None
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise ValueError("outbox payload must be an object")
        return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stable_message_id(
    order_key: str,
    event_type: str,
    channel: str = CHANNEL_EMAIL,
    template_version: str = TEMPLATE_VERSION,
) -> str:
    identity = "|".join((order_key, event_type, channel, template_version))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"<dota2-shadow-{digest}@localhost>"


def canonical_payload(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("notification payload must be a non-empty mapping")
    try:
        return json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
            default=_json_default,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("notification payload must be JSON serializable") from error


def simulation_payload(event_type: str, values: Mapping[str, Any]) -> dict[str, Any]:
    if event_type not in {EVENT_FILLED, EVENT_SETTLED}:
        raise ValueError(f"unsupported notification event: {event_type}")
    return {
        **dict(values),
        "simulation": True,
        "real_wager_placed": False,
        "event_type": event_type,
        "template_version": TEMPLATE_VERSION,
    }


def filled_order_payload(
    connection: PostgresSession, order_key: str
) -> dict[str, Any]:
    """Build an immutable, human-auditable entry notification payload."""
    order = connection.execute(
        """SELECT orders.*, attempt.map_number,
                  mapping.event_id, mapping.canonical_team_one_id,
                  mapping.canonical_team_one_name,
                  mapping.canonical_team_two_id,
                  mapping.canonical_team_two_name,
                  match.tournament AS raybet_tournament,
                  match.team_one AS raybet_team_one,
                  match.team_two AS raybet_team_two,
                  lineage.decision_key AS required_decision_key,
                  (
                      SELECT transport.observation_key
                        FROM odds_transport_observations AS transport
                       WHERE transport.raybet_match_id=orders.raybet_match_id
                         AND transport.source='direct'
                         AND transport.observed_at>orders.signal_transport_at
                         AND transport.observed_at=orders.filled_at
                         AND transport.timing_status='on_time'
                         AND transport.processing_status='processed'
                         AND EXISTS (
                               SELECT 1
                                 FROM odds_response_outcomes_effective AS outcome
                                WHERE outcome.observation_key=transport.observation_key
                                  AND outcome.raybet_match_id=orders.raybet_match_id
                                  AND outcome.odds_id=orders.odds_id
                         )
                       ORDER BY transport.observed_at, transport.observation_key
                       LIMIT 1
                  ) AS fill_transport_key
             FROM shadow_orders AS orders
             JOIN shadow_map_attempts AS attempt
               ON attempt.order_key=orders.order_key
             LEFT JOIN strict_live_map_mappings AS mapping
               ON mapping.mapping_id=orders.strict_mapping_id
             LEFT JOIN raybet_matches AS match
               ON match.raybet_match_id=orders.raybet_match_id
             LEFT JOIN shadow_order_decision_lineage AS lineage
               ON lineage.order_key=orders.order_key
            WHERE orders.order_key=?""",
        (order_key,),
    ).fetchone()
    if order is None:
        raise ValueError("notification order lineage is unavailable")
    try:
        verify_bound_order_vision_frame(connection, order_key)
    except (RuntimeError, TypeError, ValueError, SQLAlchemyError) as error:
        raise ValueError("notification vision frame authority is invalid") from error
    event_name = None
    if order["event_id"] and _table_has_column(
        connection, "event_registry", "canonical_name"
    ):
        event = connection.execute(
            "SELECT canonical_name FROM event_registry WHERE event_id=?",
            (order["event_id"],),
        ).fetchone()
        if event is not None:
            event_name = event["canonical_name"]

    decision, contributions, inputs, lineage_status = _decision_lineage(
        connection, order
    )
    if (
        order["required_decision_key"] is None
        or lineage_status != "verified"
        or not _formal_payload_is_complete(
            order,
            decision,
            inputs,
            order["fill_transport_key"],
        )
    ):
        raise ValueError("formal notification decision lineage is unavailable")
    vision = _mapping(inputs.get("vision"))
    quality = _mapping(inputs.get("quality"))
    landmark = _mapping(inputs.get("draft_landmark"))
    principal = sorted(
        (
            (str(key), float(value))
            for key, value in contributions.items()
            if not str(key).startswith("__")
            and not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        ),
        key=lambda item: (-abs(item[1]), item[0]),
    )[:5]
    aggregate_quality = (
        float(decision["data_quality"]) if decision is not None else None
    )
    if aggregate_quality is not None and "aggregate" not in quality:
        quality = {**quality, "aggregate": aggregate_quality}

    team_one_name = order["canonical_team_one_name"] or order["raybet_team_one"]
    team_two_name = order["canonical_team_two_name"] or order["raybet_team_two"]
    selected_side = order["signal_outcome_key"]
    selected_team = (
        team_one_name
        if selected_side == "team_one"
        else team_two_name
        if selected_side == "team_two"
        else None
    )
    return simulation_payload(
        EVENT_FILLED,
        {
            "raybet_match_id": str(order["raybet_match_id"]),
            "strict_mapping_id": order["strict_mapping_id"],
            "event_id": order["event_id"],
            "event_name": event_name or order["raybet_tournament"],
            "teams": {
                "team_one": {
                    "canonical_id": order["canonical_team_one_id"],
                    "name": team_one_name,
                },
                "team_two": {
                    "canonical_id": order["canonical_team_two_id"],
                    "name": team_two_name,
                },
            },
            "map_number": int(order["map_number"]),
            "trusted_game_time_seconds": vision.get("game_clock_seconds"),
            "vision_captured_at": vision.get("captured_at"),
            "source_frame_ref": vision.get("source_frame_ref"),
            "selected_side": selected_side,
            "selected_team": selected_team,
            "odds_id": str(order["odds_id"]),
            "market_key": str(order["market_key"]),
            "signal_transport_key": str(order["signal_transport_key"]),
            "fill_transport_key": order["fill_transport_key"],
            "signal_price": float(order["signal_price"]),
            "fill_price": order["fill_price"],
            "model_probability": float(order["model_probability"]),
            "market_probability": float(order["market_probability"]),
            "edge": float(order["model_probability"])
            - float(order["market_probability"]),
            "principal_contributions": dict(principal),
            "quality": quality or None,
            "model_version": landmark.get("model_version"),
            "model_kind": landmark.get("model_kind"),
            "model_hash": landmark.get("model_hash"),
            "strategy_version": (
                str(decision["strategy_version"]) if decision is not None else None
            ),
            "decision_key": (
                str(decision["decision_key"]) if decision is not None else None
            ),
            "decision_input_ref": (
                str(decision["input_ref"]) if decision is not None else None
            ),
            "decision_lineage_status": lineage_status,
            "signal_transport_at": str(order["signal_transport_at"]),
            "filled_at": order["filled_at"],
            "stake_units": float(order["stake"]),
            "order_key": str(order["order_key"]),
        },
    )


def _formal_payload_is_complete(
    order: DatabaseRow,
    decision: DatabaseRow | None,
    inputs: Mapping[str, Any],
    fill_transport_key: Any,
) -> bool:
    """Check the minimum immutable evidence required for a formal email."""
    if decision is None or not isinstance(fill_transport_key, str):
        return False
    if not fill_transport_key.strip():
        return False
    if not str(order["event_id"] or "").strip():
        return False
    for field in (
        "canonical_team_one_id",
        "canonical_team_one_name",
        "canonical_team_two_id",
        "canonical_team_two_name",
        "strict_mapping_id",
        "filled_at",
        "fill_price",
    ):
        value = order[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    for field in ("decision_key", "input_ref", "strategy_version"):
        if not str(decision[field] or "").strip():
            return False
    vision = _mapping(inputs.get("vision"))
    if (
        not str(vision.get("captured_at") or "").strip()
        or not str(vision.get("source_frame_ref") or "").strip()
        or vision.get("game_clock_seconds") is None
    ):
        return False
    landmark = _mapping(inputs.get("draft_landmark"))
    if landmark.get("status") == "wait":
        return False
    for field in ("model_version", "model_kind", "model_hash"):
        if not str(landmark.get(field) or "").strip():
            return False
    return True


def settled_order_payload(
    connection: PostgresSession,
    order_key: str,
    *,
    result: str,
    return_units: float,
    settled_at: datetime,
    evidence_ref: str,
) -> dict[str, Any]:
    """Add result and cutoff-bound cumulative statistics to entry lineage."""
    entry = _stored_entry_payload(connection, order_key)
    if entry is None:
        raise ValueError("stored entry notification lineage is unavailable")
    current_entry = filled_order_payload(connection, order_key)
    if entry != current_entry:
        raise ValueError("stored entry notification lineage is invalid")
    stake = float(entry["stake_units"])
    profit_loss = stake * (float(return_units) - 1.0)
    values = {
        key: value
        for key, value in entry.items()
        if key not in {"event_type", "template_version"}
    }
    values.update(
        {
            "result": result,
            "return_units": float(return_units),
            "profit_loss_units": profit_loss,
            "evidence_ref": evidence_ref,
            "settled_at": settled_at,
            "cumulative_shadow_statistics": _cumulative_shadow_statistics(
                connection, settled_at
            ),
        }
    )
    return simulation_payload(EVENT_SETTLED, values)


def _stored_entry_payload(
    connection: PostgresSession, order_key: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT payload_json, template_version FROM notification_outbox
            WHERE order_key=? AND event_type=? AND channel=?""",
        (order_key, EVENT_FILLED, CHANNEL_EMAIL),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("stored entry notification payload is invalid") from error
    if (
        not isinstance(payload, dict)
        or str(row["template_version"]) != TEMPLATE_VERSION
        or payload.get("template_version") != TEMPLATE_VERSION
        or payload.get("order_key") != order_key
        or payload.get("event_type") != EVENT_FILLED
        or payload.get("simulation") is not True
        or payload.get("real_wager_placed") is not False
        or payload.get("decision_lineage_status") != "verified"
        or not str(payload.get("decision_key") or "").strip()
        or not str(payload.get("decision_input_ref") or "").strip()
        or not str(payload.get("strategy_version") or "").strip()
        or not str(payload.get("fill_transport_key") or "").strip()
    ):
        raise ValueError("stored entry notification lineage is invalid")
    marker = connection.execute(
        """SELECT decision_key FROM shadow_order_decision_lineage
            WHERE order_key=?""",
        (order_key,),
    ).fetchone()
    if marker is None or str(marker["decision_key"]) != str(payload["decision_key"]):
        raise ValueError("stored entry notification lineage is invalid")
    return payload


def _decision_lineage(
    connection: PostgresSession, order: DatabaseRow
) -> tuple[DatabaseRow | None, dict[str, Any], dict[str, Any], str]:
    try:
        candidates = connection.execute(
            """SELECT * FROM strategy_decisions
                WHERE raybet_match_id=? AND map_number=? AND decided_at=?
                  AND underdog_side=? AND eligible=1
                  AND model_probability=? AND market_probability=?
                  AND (? IS NULL OR decision_key=?)
                ORDER BY decision_key""",
            (
                order["raybet_match_id"],
                order["map_number"],
                order["signal_transport_at"],
                order["signal_outcome_key"],
                order["model_probability"],
                order["market_probability"],
                order["required_decision_key"],
                order["required_decision_key"],
            ),
        ).fetchall()
    except SQLAlchemyError:
        return None, {}, {}, "unresolved"
    if not candidates:
        return None, {}, {}, "unresolved"
    verified: list[tuple[DatabaseRow, dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        identity = "|".join(
            (
                str(order["raybet_match_id"]),
                str(order["odds_id"]),
                str(order["signal_odds_group_id"] or ""),
                str(order["signal_outcome_key"] or ""),
                str(order["market_key"]),
                str(candidate["strategy_version"]),
                str(candidate["input_ref"]),
                str(float(order["stake"])),
            )
        )
        if hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32] != str(
            order["order_key"]
        ):
            continue
        try:
            contributions = json.loads(str(candidate["contributions_json"]))
            inputs = contributions["__inputs__"]
            mapping_id = inputs["strict_live_eligibility"]["mapping_refs"][
                "strict_mapping_id"
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            not isinstance(contributions, dict)
            or not isinstance(inputs, dict)
            or mapping_id != order["strict_mapping_id"]
        ):
            continue
        verified.append((candidate, contributions, inputs))
    if len(verified) > 1:
        return None, {}, {}, "ambiguous"
    if not verified:
        return None, {}, {}, "tampered"
    decision, contributions, inputs = verified[0]
    return decision, contributions, inputs, "verified"


def decision_lineage_block_reason(
    connection: PostgresSession, order_key: str
) -> str | None:
    """Require one immutable marker resolving to one exact eligible decision."""
    try:
        order = connection.execute(
            """SELECT orders.*, attempt.map_number,
                      lineage.decision_key AS required_decision_key
                 FROM shadow_orders AS orders
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                 LEFT JOIN shadow_order_decision_lineage AS lineage
                   ON lineage.order_key=orders.order_key
                WHERE orders.order_key=?""",
            (order_key,),
        ).fetchone()
    except SQLAlchemyError:
        return "decision_lineage_unavailable"
    if order is None or order["required_decision_key"] is None:
        return "decision_lineage_unavailable"
    _decision, _contributions, _inputs, status = _decision_lineage(connection, order)
    if status != "verified":
        return f"decision_lineage_{status}"
    return None


def _cumulative_shadow_statistics(
    connection: PostgresSession, cutoff: datetime
) -> dict[str, float | int]:
    cutoff_iso = _iso(cutoff)
    try:
        filled_rows = connection.execute(
            """SELECT order_key FROM shadow_orders
                WHERE status='filled'
                  AND live_text_timestamp_utc(filled_at)<=CAST(? AS timestamptz)""",
            (cutoff_iso,),
        ).fetchall()
        settled_rows = connection.execute(
            """SELECT orders.order_key, orders.stake, settlement.return_units
                 FROM settlements AS settlement
                 JOIN shadow_orders AS orders
                   ON orders.order_key=settlement.order_key
                WHERE orders.status='filled' AND settlement.review_required=0
                  AND live_text_timestamp_utc(settlement.settled_at)<=
                      CAST(? AS timestamptz)""",
            (cutoff_iso,),
        ).fetchall()
    except SQLAlchemyError:
        return _empty_shadow_statistics()
    filled = [
        row
        for row in filled_rows
        if not _order_excluded_from_statistics(connection, str(row["order_key"]))
    ]
    settled = [
        row
        for row in settled_rows
        if not _order_excluded_from_statistics(connection, str(row["order_key"]))
    ]
    stake_units = math.fsum(float(row["stake"]) for row in settled)
    returned_units = math.fsum(
        float(row["stake"]) * float(row["return_units"]) for row in settled
    )
    profit_loss_units = returned_units - stake_units
    return {
        "filled_orders": len(filled),
        "settled_orders": len(settled),
        "stake_units": stake_units,
        "return_units": returned_units,
        "profit_loss_units": profit_loss_units,
        "roi": profit_loss_units / stake_units if stake_units else 0.0,
    }


def _order_excluded_from_statistics(
    connection: PostgresSession, order_key: str
) -> bool:
    try:
        strict_reason = _strict_mapping_block_reason(connection, order_key)
    except SQLAlchemyError:
        return True
    if strict_reason is not None:
        return True
    try:
        invalidated = connection.execute(
            """SELECT 1 FROM vision_derived_invalidations
                WHERE dependent_type='shadow_order' AND dependent_key=? LIMIT 1""",
            (order_key,),
        ).fetchone()
        return invalidated is not None or _draft_conflict_for_order(
            connection, order_key
        )
    except SQLAlchemyError:
        # Legacy databases without every audit table cannot prove an order is
        # eligible for cumulative reporting.  Exclude it without blocking the
        # settlement transaction or inventing optimistic statistics.
        return True


def _empty_shadow_statistics() -> dict[str, float | int]:
    return {
        "filled_orders": 0,
        "settled_orders": 0,
        "stake_units": 0.0,
        "return_units": 0.0,
        "profit_loss_units": 0.0,
        "roi": 0.0,
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _table_has_column(
    connection: PostgresSession, table: str, column: str
) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name=? AND column_name=?""",
            (table, column),
        ).fetchone()
        is not None
    )


def enqueue(
    connection: PostgresSession,
    *,
    order_key: str,
    event_type: str,
    payload: Mapping[str, Any],
    stats_cutoff_at: datetime,
    created_at: datetime,
    recipient: str = DEFAULT_RECIPIENT,
    channel: str = CHANNEL_EMAIL,
    template_version: str = TEMPLATE_VERSION,
) -> bool:
    """Insert one immutable logical event; duplicate scheduling is harmless."""
    if not order_key or channel != CHANNEL_EMAIL:
        raise ValueError("invalid notification identity")
    if stats_cutoff_at.tzinfo is None or created_at.tzinfo is None:
        raise ValueError("notification times must be timezone-aware")
    if not recipient or any(char in recipient for char in "\r\n"):
        raise ValueError("recipient contains header control characters")
    payload_json = canonical_payload(payload)
    message_id = stable_message_id(order_key, event_type, channel, template_version)
    values = (
        order_key,
        event_type,
        channel,
        "pending",
        recipient,
        message_id,
        payload_json,
        _iso(stats_cutoff_at),
        template_version,
        None,
        None,
        0,
        _iso(created_at),
        None,
        None,
        _iso(created_at),
        _iso(created_at),
    )
    with _transaction(connection):
        if event_type in {EVENT_FILLED, EVENT_SETTLED}:
            try:
                verify_bound_order_vision_frame(connection, order_key)
            except (RuntimeError, TypeError, ValueError, SQLAlchemyError):
                return False
            if _strict_mapping_block_reason(connection, order_key) is not None:
                return False
            try:
                blocked = connection.execute(
                    """SELECT block_reason FROM vision_derived_invalidations
                        WHERE dependent_type='shadow_order' AND dependent_key=?
                        LIMIT 1""",
                    (order_key,),
                ).fetchone()
            except SQLAlchemyError:
                blocked = connection.execute(
                    """SELECT 1 FROM vision_derived_invalidations
                        WHERE dependent_type='shadow_order' AND dependent_key=?
                        LIMIT 1""",
                    (order_key,),
                ).fetchone()
            if blocked is not None:
                return False
            if _draft_conflict_for_order(connection, order_key):
                return False
        cursor = connection.execute(
            """INSERT INTO notification_outbox
               (order_key, event_type, channel, status, recipient, message_id,
                payload_json, statistics_cutoff, template_version, lease_token,
                lease_until, attempt_count, next_attempt_at, last_error, sent_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_key, event_type, channel) DO NOTHING""",
            values,
        )
        if cursor.rowcount == 1:
            return True
        existing = connection.execute(
            """SELECT recipient, message_id, payload_json, statistics_cutoff,
                      template_version, created_at
                 FROM notification_outbox
                WHERE order_key=? AND event_type=? AND channel=?""",
            (order_key, event_type, channel),
        ).fetchone()
        expected = (
            recipient,
            message_id,
            payload_json,
            _iso(stats_cutoff_at),
            template_version,
            _iso(created_at),
        )
        if existing is None or tuple(existing) != expected:
            raise NotificationConflictError(
                "notification logical key conflicts with immutable payload"
            )
        return False


def _formal_notification_block_reason(
    connection: PostgresSession,
    row: DatabaseRow,
    order_key: str,
) -> str | None:
    event_type = str(row["event_type"])
    try:
        verify_bound_order_vision_frame(connection, order_key)
    except (RuntimeError, TypeError, ValueError, SQLAlchemyError):
        return "vision_frame_integrity_failed"
    if str(row["template_version"]) != TEMPLATE_VERSION:
        return "formal_notification_template_unsupported"
    payload = json.loads(str(row["payload_json"]))
    if (
        not isinstance(payload, dict)
        or payload.get("template_version") != TEMPLATE_VERSION
        or payload.get("event_type") != event_type
        or payload.get("order_key") != order_key
        or payload.get("simulation") is not True
        or payload.get("real_wager_placed") is not False
        or payload.get("decision_lineage_status") != "verified"
        or not str(payload.get("decision_key") or "").strip()
        or not str(payload.get("decision_input_ref") or "").strip()
        or not str(payload.get("strategy_version") or "").strip()
        or not str(payload.get("fill_transport_key") or "").strip()
    ):
        return "formal_notification_payload_invalid"
    if event_type == EVENT_SETTLED:
        authority_reason = persisted_settlement_authority_reason(
            connection, order_key
        )
        if authority_reason is not None:
            record_settlement_authority_review(
                connection,
                order_key,
                authority_reason,
                actor="notification_gate",
            )
            return authority_reason
        try:
            baseline = _stored_entry_payload(connection, order_key)
        except (SQLAlchemyError, TypeError, ValueError, json.JSONDecodeError):
            return "formal_notification_decision_lineage_unavailable"
        if baseline is None:
            return "formal_notification_decision_lineage_unavailable"
        try:
            current_baseline = filled_order_payload(connection, order_key)
        except (TypeError, ValueError, json.JSONDecodeError):
            return "formal_notification_lineage_mismatch"
        except SQLAlchemyError:
            return "formal_notification_decision_lineage_unavailable"
        if baseline != current_baseline:
            return "formal_notification_lineage_mismatch"
    else:
        try:
            baseline = filled_order_payload(connection, order_key)
        except (SQLAlchemyError, TypeError, ValueError, json.JSONDecodeError):
            return "formal_notification_decision_lineage_unavailable"
    for key, value in baseline.items():
        if key not in {"event_type", "template_version"} and payload.get(key) != value:
            return "formal_notification_lineage_mismatch"
    event_time_field = "settled_at" if event_type == EVENT_SETTLED else "filled_at"
    try:
        event_time = _parse_time(payload.get(event_time_field))
        if (
            _parse_time(row["statistics_cutoff"]) != event_time
            or _parse_time(row["created_at"]) != event_time
        ):
            return "formal_notification_cutoff_mismatch"
    except (TypeError, ValueError):
        return "formal_notification_payload_invalid"
    if event_type == EVENT_SETTLED:
        if (
            not str(payload.get("result") or "").strip()
            or not str(payload.get("evidence_ref") or "").strip()
            or not _finite_number(payload.get("return_units"))
            or not _finite_number(payload.get("profit_loss_units"))
        ):
            return "formal_notification_payload_invalid"
        settlement = connection.execute(
            """SELECT result, return_units, settled_at, evidence_ref,
                      review_required
                 FROM settlements WHERE order_key=?""",
            (order_key,),
        ).fetchone()
        if (
            settlement is None
            or int(settlement["review_required"]) != 0
            or str(settlement["result"]) != str(payload["result"])
            or str(settlement["evidence_ref"]) != str(payload["evidence_ref"])
            or _parse_time(settlement["settled_at"]) != event_time
            or not math.isclose(
                float(settlement["return_units"]),
                float(payload["return_units"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not math.isclose(
                float(payload["profit_loss_units"]),
                float(baseline["stake_units"])
                * (float(settlement["return_units"]) - 1.0),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            return "formal_notification_settlement_mismatch"
    return None


def _blocked_reason(
    connection: PostgresSession, row: DatabaseRow
) -> str | None:
    """Return a sticky reason when a claimed event is no longer deliverable."""
    try:
        quarantine_prefix = "quarantine_intent:"
        last_error = str(row["last_error"] or "")
        if last_error.startswith(quarantine_prefix):
            return last_error.removeprefix(quarantine_prefix) or "quarantined"
        order_key, order_scoped = _notification_order_identity(row)
        if not order_scoped:
            return None
        event_type = str(row["event_type"])
        if (
            event_type in {EVENT_FILLED, EVENT_SETTLED}
            and str(row["template_version"]) != TEMPLATE_VERSION
        ):
            return "formal_notification_template_unsupported"
        strict_mapping = _strict_mapping_block_reason(
            connection,
            order_key,
            require_order=event_type in {EVENT_MONITOR_ALERT, EVENT_MONITOR_RECOVERY},
        )
        if strict_mapping is not None:
            return strict_mapping
        legacy_invalidation = False
        try:
            invalidated = connection.execute(
                """SELECT block_reason FROM vision_derived_invalidations
                    WHERE dependent_type='shadow_order' AND dependent_key=?
                    LIMIT 1""",
                (order_key,),
            ).fetchone()
        except SQLAlchemyError:
            # Databases created before stable gate codes remain draft-safe.
            legacy_invalidation = True
            invalidated = connection.execute(
                """SELECT 1 FROM vision_derived_invalidations
                    WHERE dependent_type='shadow_order' AND dependent_key=?
                    LIMIT 1""",
                (order_key,),
            ).fetchone()
        if invalidated is not None:
            return (
                "vision_draft_conflict"
                if legacy_invalidation
                else str(invalidated[0] or "vision_draft_conflict")
            )
        if _draft_conflict_for_order(connection, order_key):
            return "vision_draft_conflict"
        if event_type == EVENT_SETTLED:
            reviewed = connection.execute(
                """SELECT 1 FROM settlements
                    WHERE order_key=? AND review_required=1
                    LIMIT 1""",
                (order_key,),
            ).fetchone()
            if reviewed is not None:
                return "settlement_manual_review"
        if event_type in {EVENT_FILLED, EVENT_SETTLED}:
            formal_reason = _formal_notification_block_reason(
                connection, row, order_key
            )
            if formal_reason is not None:
                return formal_reason
        else:
            lineage_reason = decision_lineage_block_reason(connection, order_key)
            if lineage_reason is not None:
                return lineage_reason
    except (SQLAlchemyError, TypeError, ValueError, json.JSONDecodeError):
        return "notification_gate_unavailable"
    return None


def _notification_order_identity(row: DatabaseRow) -> tuple[str, bool]:
    event_type = str(row["event_type"])
    stored_order_key = str(row["order_key"])
    if event_type in {EVENT_FILLED, EVENT_SETTLED}:
        return stored_order_key, True
    if event_type not in {EVENT_MONITOR_ALERT, EVENT_MONITOR_RECOVERY}:
        return stored_order_key, False
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict) or payload.get("category") != "paper_signal":
        return stored_order_key, False
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ValueError("paper signal notification source is unavailable")
    paper_order_key = str(source.get("order_key") or "").strip()
    if not paper_order_key:
        raise ValueError("paper signal notification order key is unavailable")
    return paper_order_key, True


def _strict_mapping_block_reason(
    connection: PostgresSession,
    order_key: str,
    *,
    require_order: bool = False,
) -> str | None:
    """Delegate notification checks to the persisted-order strict gate."""
    from .storage import strict_order_mapping_block_reason

    return strict_order_mapping_block_reason(
        connection, order_key, require_order=require_order
    )


def _draft_conflict_for_order(
    connection: PostgresSession, order_key: str
) -> bool:
    """Return whether an order belongs to a map with ambiguous draft identity."""
    return (
        connection.execute(
            """SELECT 1
             FROM shadow_orders AS orders
             JOIN shadow_map_attempts AS attempt
               ON attempt.order_key=orders.order_key
             JOIN vision_draft_anchors AS anchor
               ON anchor.raybet_match_id=attempt.raybet_match_id
              AND anchor.map_number=attempt.map_number
            WHERE orders.order_key=? AND anchor.status='conflict'
              AND (
                    anchor.conflict_at IS NULL
                    OR live_text_timestamp_utc(anchor.conflict_at) IS NULL
                    OR live_text_timestamp_utc(
                           orders.signal_transport_at
                       ) IS NULL
                    OR live_text_timestamp_utc(anchor.conflict_at)
                         <= live_text_timestamp_utc(orders.signal_transport_at)
                    OR EXISTS (
                         SELECT 1 FROM vision_draft_conflicts AS conflict
                          WHERE conflict.raybet_match_id=anchor.raybet_match_id
                            AND conflict.map_number=anchor.map_number
                            AND (
                                  live_text_timestamp_utc(
                                      conflict.captured_at
                                  ) IS NULL
                                  OR live_text_timestamp_utc(
                                         conflict.captured_at
                                     ) <= live_text_timestamp_utc(
                                         orders.signal_transport_at
                                     )
                            )
                    )
              )
            LIMIT 1""",
            (order_key,),
        ).fetchone()
        is not None
    )


def _suppress_locked(
    connection: PostgresSession,
    outbox_id: int,
    reason: str,
    *,
    now: datetime,
) -> None:
    connection.execute(
        """UPDATE notification_outbox
              SET status='dead_letter', lease_token=NULL,
                  lease_until=NULL, last_error=?, updated_at=?
            WHERE outbox_id=? AND status IN ('pending', 'leased')""",
        (reason, _iso(now), outbox_id),
    )
    connection.execute(
        """INSERT INTO notification_outbox_audit
           (outbox_id, action, actor, reason, created_at)
           VALUES (?, 'blocked', 'notification_gate', ?, ?)""",
        (outbox_id, reason, _iso(now)),
    )


def quarantine_outbox(
    connection: PostgresSession,
    *,
    outbox_id: int,
    reason: str,
    actor: str,
    now: datetime,
    record_audit: bool = True,
) -> bool:
    """Quarantine pending work without stealing an active SMTP lease."""
    row = connection.execute(
        "SELECT status, last_error FROM notification_outbox WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    if row is None:
        return False
    status = str(row["status"])
    safe_reason = _safe_reason(reason)
    if status == "pending":
        changed = connection.execute(
            """UPDATE notification_outbox
                  SET status='dead_letter', lease_token=NULL, lease_until=NULL,
                      last_error=?, updated_at=?
                WHERE outbox_id=? AND status='pending'""",
            (safe_reason, _iso(now), outbox_id),
        )
        action = "blocked"
    elif status == "leased":
        quarantine_intent = f"quarantine_intent:{safe_reason}"
        if str(row["last_error"] or "") == quarantine_intent:
            return False
        changed = connection.execute(
            """UPDATE notification_outbox
                  SET last_error=?, updated_at=?
                WHERE outbox_id=? AND status='leased'""",
            (quarantine_intent, _iso(now), outbox_id),
        )
        action = "quarantine_intent"
    else:
        return False
    if changed.rowcount != 1:
        return False
    if record_audit:
        connection.execute(
            """INSERT INTO notification_outbox_audit
               (outbox_id, action, actor, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (outbox_id, action, _safe_reason(actor), safe_reason, _iso(now)),
        )
    return True


def claim(
    connection: PostgresSession,
    *,
    now: datetime | None = None,
    lease_seconds: int = 300,
) -> OutboxRecord | None:
    """Claim one due row; expired leases are reclaimable by a new token."""
    now = now or utc_now()
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    token = uuid.uuid4().hex
    lease_until = now + timedelta(seconds=lease_seconds)
    with _transaction(connection):
        while True:
            row = connection.execute(
                """SELECT * FROM notification_outbox
                    WHERE ((status='pending' AND next_attempt_at IS NOT NULL
                            AND live_text_timestamp_utc(next_attempt_at)<=
                                CAST(? AS timestamptz))
                       OR (status='leased' AND lease_until IS NOT NULL
                            AND live_text_timestamp_utc(lease_until)<=
                                CAST(? AS timestamptz)))
                    ORDER BY live_text_timestamp_utc(next_attempt_at), outbox_id
                    LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (_iso(now), _iso(now)),
            ).fetchone()
            if row is None:
                return None
            blocked = _blocked_reason(connection, row)
            if blocked is None:
                break
            _suppress_locked(connection, int(row["outbox_id"]), blocked, now=now)
        changed = connection.execute(
            """UPDATE notification_outbox
                  SET status='leased', lease_token=?, lease_until=? ,
                      attempt_count=attempt_count+1, updated_at=?
                WHERE outbox_id=?
                  AND (status='pending' OR
                       (status='leased' AND live_text_timestamp_utc(lease_until)<=
                           CAST(? AS timestamptz)))""",
            (token, _iso(lease_until), _iso(now), int(row["outbox_id"]), _iso(now)),
        )
        if changed.rowcount != 1:
            return None
        claimed = connection.execute(
            "SELECT * FROM notification_outbox WHERE outbox_id=?",
            (int(row["outbox_id"]),),
        ).fetchone()
        return _row(claimed) if claimed is not None else None


def mark_sent(
    connection: PostgresSession,
    *,
    outbox_id: int,
    lease_token: str,
    sent_at: datetime | None = None,
) -> bool:
    """Mark delivery complete, recording a post-send safety quarantine.

    SMTP has no transaction with SQLite.  If a draft or mapping conflict is
    observed after the server accepted the message, the message was still
    sent and must never be retried as an ordinary dead letter.  The durable
    audit action makes that race explicit for operators and reports.
    """
    sent_at = sent_at or utc_now()
    with _transaction(connection):
        row = connection.execute(
            """SELECT * FROM notification_outbox
                WHERE outbox_id=? AND status='leased' AND lease_token=?""",
            (outbox_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        blocked = _blocked_reason(connection, row)
        if blocked is not None:
            connection.execute(
                """UPDATE notification_outbox
                      SET status='sent', sent_at=?, lease_token=NULL,
                          lease_until=NULL, last_error=?, updated_at=?
                    WHERE outbox_id=? AND status='leased' AND lease_token=?""",
                (
                    _iso(sent_at),
                    f"sent_then_quarantined:{blocked}",
                    _iso(sent_at),
                    outbox_id,
                    lease_token,
                ),
            )
            connection.execute(
                """INSERT INTO notification_outbox_audit
                   (outbox_id, action, actor, reason, created_at)
                   VALUES (?, 'sent_then_quarantined', 'notification_gate', ?, ?)""",
                (outbox_id, blocked, _iso(sent_at)),
            )
            return False
        cursor = connection.execute(
            """UPDATE notification_outbox
                  SET status='sent', sent_at=?, lease_token=NULL,
                      lease_until=NULL, last_error=NULL, updated_at=?
                WHERE outbox_id=? AND status='leased' AND lease_token=?""",
            (_iso(sent_at), _iso(sent_at), outbox_id, lease_token),
        )
        return cursor.rowcount == 1


def ensure_sendable(
    connection: PostgresSession,
    *,
    outbox_id: int,
    lease_token: str,
    now: datetime | None = None,
) -> bool:
    """Recheck a lease immediately before SMTP I/O."""
    now = now or utc_now()
    with _transaction(connection):
        row = connection.execute(
            """SELECT * FROM notification_outbox
                WHERE outbox_id=? AND status='leased' AND lease_token=?
                  AND lease_until IS NOT NULL
                  AND live_text_timestamp_utc(lease_until)>
                      CAST(? AS timestamptz)""",
            (outbox_id, lease_token, _iso(now)),
        ).fetchone()
        if row is None:
            return False
        blocked = _blocked_reason(connection, row)
        if blocked is None:
            return True
        _suppress_locked(connection, outbox_id, blocked, now=now)
        return False


def mark_failure(
    connection: PostgresSession,
    *,
    outbox_id: int,
    lease_token: str,
    transient: bool,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Fence failure updates and apply the fixed 1m/5m/30m/2h/12h schedule."""
    now = now or utc_now()
    safe_reason = _safe_reason(reason)
    with _transaction(connection):
        row = connection.execute(
            """SELECT * FROM notification_outbox
                WHERE outbox_id=? AND status='leased' AND lease_token=?""",
            (outbox_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        blocked = _blocked_reason(connection, row)
        if blocked is not None:
            _suppress_locked(connection, outbox_id, blocked, now=now)
            return True
        attempt_count = int(row["attempt_count"])
        retry_index = attempt_count - 1
        should_retry = transient and retry_index < len(RETRY_DELAYS)
        if should_retry:
            status = "pending"
            next_at = now + timedelta(seconds=RETRY_DELAYS[retry_index])
            sent_at = None
        else:
            status = "dead_letter"
            next_at = now
            sent_at = None
        cursor = connection.execute(
            """UPDATE notification_outbox
                  SET status=?, next_attempt_at=?, lease_token=NULL,
                      lease_until=NULL, last_error=?, sent_at=?, updated_at=?
                WHERE outbox_id=? AND status='leased' AND lease_token=?""",
            (
                status,
                _iso(next_at),
                safe_reason,
                sent_at,
                _iso(now),
                outbox_id,
                lease_token,
            ),
        )
        return cursor.rowcount == 1


def requeue_dead_letter(
    connection: PostgresSession,
    *,
    outbox_id: int,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> bool:
    now = now or utc_now()
    actor = _safe_reason(actor)
    reason = _safe_reason(reason)
    with _transaction(connection):
        row = connection.execute(
            """SELECT * FROM notification_outbox
                WHERE outbox_id=? AND status='dead_letter'""",
            (outbox_id,),
        ).fetchone()
        if row is None:
            return False
        blocked = _blocked_reason(connection, row)
        if blocked is not None:
            connection.execute(
                """INSERT INTO notification_outbox_audit
                   (outbox_id, action, actor, reason, created_at)
                   VALUES (?, 'blocked', ?, ?, ?)""",
                (outbox_id, actor, blocked, _iso(now)),
            )
            return False
        cursor = connection.execute(
            """UPDATE notification_outbox
                  SET status='pending', next_attempt_at=?, lease_token=NULL,
                      lease_until=NULL, attempt_count=0, last_error=?, updated_at=?
                WHERE outbox_id=? AND status='dead_letter'""",
            (_iso(now), f"requeued:{reason}", _iso(now), outbox_id),
        )
        if cursor.rowcount != 1:
            return False
        connection.execute(
            """INSERT INTO notification_outbox_audit
               (outbox_id, action, actor, reason, created_at)
               VALUES (?, 'requeue', ?, ?, ?)""",
            (outbox_id, actor, reason, _iso(now)),
        )
        return True


def _row(row: DatabaseRow) -> OutboxRecord:
    return OutboxRecord(
        outbox_id=int(row["outbox_id"]),
        order_key=str(row["order_key"]),
        event_type=str(row["event_type"]),
        channel=str(row["channel"]),
        payload_json=str(row["payload_json"]),
        stats_cutoff_at=_parse_time(row["statistics_cutoff"]),
        template_version=str(row["template_version"]),
        recipient=str(row["recipient"]),
        message_id=str(row["message_id"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=_parse_time(row["next_attempt_at"]),
        lease_token=row["lease_token"],
        lease_until=_parse_time(row["lease_until"]) if row["lease_until"] else None,
        last_error=row["last_error"],
        created_at=_parse_time(row["created_at"]),
        sent_at=_parse_time(row["sent_at"]) if row["sent_at"] else None,
    )


@contextmanager
def _transaction(connection: PostgresSession) -> Iterator[None]:
    with connection.transaction():
        yield


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("outbox timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError(f"unsupported payload value: {type(value).__name__}")


def _safe_reason(value: str) -> str:
    text = " ".join(str(value).split())
    return text[:500] or "unspecified"


__all__ = [
    "CHANNEL_EMAIL",
    "DEFAULT_RECIPIENT",
    "EVENT_FILLED",
    "EVENT_SETTLED",
    "EVENT_MONITOR_ALERT",
    "EVENT_MONITOR_RECOVERY",
    "MONITOR_TEMPLATE_VERSION",
    "NotificationConflictError",
    "OutboxRecord",
    "RETRY_DELAYS",
    "TEMPLATE_VERSION",
    "canonical_payload",
    "claim",
    "decision_lineage_block_reason",
    "ensure_sendable",
    "enqueue",
    "filled_order_payload",
    "mark_failure",
    "mark_sent",
    "quarantine_outbox",
    "requeue_dead_letter",
    "settled_order_payload",
    "simulation_payload",
    "stable_message_id",
]
