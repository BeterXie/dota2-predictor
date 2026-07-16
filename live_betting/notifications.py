"""Transactional notification outbox primitives for simulation events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping


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


def enqueue(
    connection: sqlite3.Connection,
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
            if _strict_mapping_block_reason(connection, order_key) is not None:
                return False
            try:
                blocked = connection.execute(
                    """SELECT block_reason FROM vision_derived_invalidations
                        WHERE dependent_type='shadow_order' AND dependent_key=?
                        LIMIT 1""",
                    (order_key,),
                ).fetchone()
            except sqlite3.OperationalError:
                blocked = connection.execute(
                    """SELECT 1 FROM vision_derived_invalidations
                        WHERE dependent_type='shadow_order' AND dependent_key=?
                        LIMIT 1""",
                    (order_key,),
                ).fetchone()
            if blocked is not None:
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


def _blocked_reason(connection: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Return a sticky reason when a claimed event is no longer deliverable."""
    try:
        order_key, order_scoped = _notification_order_identity(row)
        if not order_scoped:
            return None
        strict_mapping = _strict_mapping_block_reason(
            connection,
            order_key,
            require_order=str(row["event_type"])
            in {EVENT_MONITOR_ALERT, EVENT_MONITOR_RECOVERY},
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
        except sqlite3.OperationalError:
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
        direct_conflict = connection.execute(
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
                        OR julianday(anchor.conflict_at) IS NULL
                        OR julianday(orders.signal_transport_at) IS NULL
                        OR julianday(anchor.conflict_at)
                             <= julianday(orders.signal_transport_at)
                        OR EXISTS (
                             SELECT 1 FROM vision_draft_conflicts AS conflict
                              WHERE conflict.raybet_match_id=anchor.raybet_match_id
                                AND conflict.map_number=anchor.map_number
                                AND (
                                      julianday(conflict.captured_at) IS NULL
                                      OR julianday(conflict.captured_at)
                                           <= julianday(orders.signal_transport_at)
                                )
                        )
                  )
                LIMIT 1""",
            (order_key,),
        ).fetchone()
        if direct_conflict is not None:
            return "vision_draft_conflict"
        if str(row["event_type"]) == EVENT_SETTLED:
            reviewed = connection.execute(
                """SELECT 1 FROM settlements
                    WHERE order_key=? AND review_required=1
                    LIMIT 1""",
                (order_key,),
            ).fetchone()
            if reviewed is not None:
                return "settlement_manual_review"
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return "notification_gate_unavailable"
    return None


def _notification_order_identity(row: sqlite3.Row) -> tuple[str, bool]:
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
    connection: sqlite3.Connection,
    order_key: str,
    *,
    require_order: bool = False,
) -> str | None:
    """Delegate notification checks to the persisted-order strict gate."""
    from .storage import strict_order_mapping_block_reason

    return strict_order_mapping_block_reason(
        connection, order_key, require_order=require_order
    )


def _suppress_locked(
    connection: sqlite3.Connection,
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


def claim(
    connection: sqlite3.Connection,
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
                            AND next_attempt_at<=?)
                       OR (status='leased' AND lease_until IS NOT NULL
                            AND lease_until<=?))
                    ORDER BY next_attempt_at, outbox_id LIMIT 1""",
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
                       (status='leased' AND lease_until<=?))""",
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
    connection: sqlite3.Connection,
    *,
    outbox_id: int,
    lease_token: str,
    sent_at: datetime | None = None,
) -> bool:
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
            _suppress_locked(connection, outbox_id, blocked, now=sent_at)
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
    connection: sqlite3.Connection,
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
                WHERE outbox_id=? AND status='leased' AND lease_token=?""",
            (outbox_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        blocked = _blocked_reason(connection, row)
        if blocked is None:
            return True
        _suppress_locked(connection, outbox_id, blocked, now=now)
        return False


def mark_failure(
    connection: sqlite3.Connection,
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
            """SELECT attempt_count FROM notification_outbox
                WHERE outbox_id=? AND status='leased' AND lease_token=?""",
            (outbox_id, lease_token),
        ).fetchone()
        if row is None:
            return False
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
    connection: sqlite3.Connection,
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


def _row(row: sqlite3.Row) -> OutboxRecord:
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
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        name = f"notification_{uuid.uuid4().hex}"
        connection.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {name}")
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


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
    "ensure_sendable",
    "enqueue",
    "mark_failure",
    "mark_sent",
    "requeue_dead_letter",
    "simulation_payload",
    "stable_message_id",
]
