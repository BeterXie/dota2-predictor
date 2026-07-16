from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from live_betting.notifications import (
    EVENT_MONITOR_ALERT,
    EVENT_MONITOR_RECOVERY,
    MONITOR_TEMPLATE_VERSION,
    decision_lineage_block_reason,
    enqueue,
)


_ALERT_TABLES = """
CREATE TABLE IF NOT EXISTS monitor_alert_candidates (
    dedupe_key TEXT PRIMARY KEY,
    first_detected_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_alert_incidents (
    incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    episode INTEGER NOT NULL CHECK (episode > 0),
    category TEXT NOT NULL CHECK (category IN ('operational', 'paper_signal')),
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'recovered')),
    first_detected_at TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL,
    recovered_at TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    source_json TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    UNIQUE (dedupe_key, episode)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_alert_active_key
    ON monitor_alert_incidents(dedupe_key) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_monitor_alert_status_opened
    ON monitor_alert_incidents(status, opened_at DESC);
CREATE TABLE IF NOT EXISTS monitor_alert_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES monitor_alert_incidents(incident_id),
    action TEXT NOT NULL CHECK (action IN ('opened', 'observed', 'acknowledged', 'recovered')),
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS monitor_alert_audit_no_update
BEFORE UPDATE ON monitor_alert_audit
BEGIN
    SELECT RAISE(ABORT, 'monitor alert audit rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS monitor_alert_audit_no_delete
BEFORE DELETE ON monitor_alert_audit
BEGIN
    SELECT RAISE(ABORT, 'monitor alert audit rows cannot be deleted');
END;
"""

_OBSERVATION_AUDIT_SECONDS = 60

_PAPER_SIGNAL_REQUIRED_COLUMNS = {
    "shadow_orders": {
        "order_key",
        "raybet_match_id",
        "strict_mapping_id",
        "market_key",
        "model_probability",
        "market_probability",
        "signal_price",
        "signaled_at",
        "signal_transport_at",
        "status",
    },
    "shadow_map_attempts": {"order_key", "raybet_match_id", "map_number"},
    "shadow_order_decision_lineage": {"order_key", "decision_key"},
    "strategy_decisions": {
        "decision_key",
        "raybet_match_id",
        "map_number",
        "decided_at",
        "underdog_side",
        "eligible",
        "model_probability",
        "market_probability",
        "strategy_version",
        "input_ref",
        "contributions_json",
    },
    "vision_derived_invalidations": {"dependent_type", "dependent_key"},
    "vision_draft_anchors": {
        "raybet_match_id",
        "map_number",
        "status",
        "conflict_at",
        "anchored_at",
        "source_frame_ref",
    },
    "vision_draft_conflicts": {"raybet_match_id", "map_number", "captured_at"},
    "strict_live_mapping_impacts": {"dependent_type", "dependent_key"},
    "strict_live_map_mapping_invalidations": {"invalidation_id", "mapping_id"},
    "strict_live_map_mappings": {
        "mapping_id",
        "raybet_match_id",
        "map_number",
        "acceptance_mode",
        "automatic_approval_id",
        "accepted_at",
    },
    "strict_live_automatic_evidence_approvals": {
        "approval_id",
        "source_mapping_id",
        "approved_at",
    },
}

_PAPER_SIGNAL_QUERY = """
    SELECT orders.order_key, orders.raybet_match_id, orders.market_key,
           orders.model_probability, orders.market_probability,
           orders.signal_price, orders.signaled_at
     FROM shadow_orders AS orders
     WHERE orders.status='pending'
       AND julianday(orders.signaled_at) IS NOT NULL
       AND julianday(orders.signal_transport_at) IS NOT NULL
       AND NOT EXISTS (
             SELECT 1 FROM strict_live_mapping_impacts AS impact
              WHERE impact.dependent_type='shadow_order'
                AND impact.dependent_key=orders.order_key
       )
       AND (
             orders.strict_mapping_id IS NOT NULL
             AND EXISTS (
                  SELECT 1
                    FROM strict_live_map_mappings AS mapping
                    JOIN shadow_map_attempts AS strict_attempt
                      ON strict_attempt.order_key=orders.order_key
                     AND strict_attempt.raybet_match_id=mapping.raybet_match_id
                     AND strict_attempt.map_number=mapping.map_number
                    LEFT JOIN strict_live_map_mapping_invalidations AS direct
                      ON direct.mapping_id=mapping.mapping_id
                    LEFT JOIN strict_live_automatic_evidence_approvals AS approval
                      ON approval.approval_id=mapping.automatic_approval_id
                    LEFT JOIN strict_live_map_mapping_invalidations AS source
                      ON source.mapping_id=approval.source_mapping_id
                   WHERE mapping.mapping_id=orders.strict_mapping_id
                     AND mapping.raybet_match_id=orders.raybet_match_id
                     AND julianday(mapping.accepted_at) IS NOT NULL
                     AND julianday(mapping.accepted_at)<=
                         julianday(orders.signal_transport_at)
                     AND direct.invalidation_id IS NULL
                     AND source.invalidation_id IS NULL
                     AND mapping.acceptance_mode IN
                         ('manual_exact', 'automatic_exact')
                     AND (
                           mapping.acceptance_mode='manual_exact'
                           OR (
                                approval.approval_id IS NOT NULL
                                AND julianday(approval.approved_at) IS NOT NULL
                                AND julianday(approval.approved_at)<=
                                    julianday(orders.signal_transport_at)
                           )
                     )
             )
       )
       AND NOT EXISTS (
             SELECT 1 FROM vision_derived_invalidations AS invalidation
              WHERE invalidation.dependent_type='shadow_order'
                AND invalidation.dependent_key=orders.order_key
       )
       AND EXISTS (
             SELECT 1
               FROM shadow_map_attempts AS attempt
               JOIN vision_draft_anchors AS anchor
                 ON anchor.raybet_match_id=attempt.raybet_match_id
                AND anchor.map_number=attempt.map_number
              WHERE attempt.order_key=orders.order_key
                AND anchor.source_frame_ref!=''
                AND julianday(anchor.anchored_at) IS NOT NULL
                AND julianday(anchor.anchored_at)<=
                    julianday(orders.signal_transport_at)
                AND (
                      anchor.status='anchored'
                      OR (
                           anchor.status='conflict'
                           AND anchor.conflict_at IS NOT NULL
                           AND julianday(anchor.conflict_at) IS NOT NULL
                           AND julianday(anchor.conflict_at)>
                               julianday(orders.signal_transport_at)
                           AND NOT EXISTS (
                                SELECT 1
                                  FROM vision_draft_conflicts AS conflict
                                 WHERE conflict.raybet_match_id=
                                       anchor.raybet_match_id
                                   AND conflict.map_number=anchor.map_number
                                   AND (
                                         julianday(conflict.captured_at) IS NULL
                                         OR julianday(conflict.captured_at)<=
                                            julianday(orders.signal_transport_at)
                                   )
                           )
                      )
                )
       )
"""


def init_alert_schema(connection: sqlite3.Connection) -> None:
    _migrate_notification_outbox(connection)
    connection.executescript(_ALERT_TABLES)
    connection.commit()


def reconcile_alerts(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    grace_seconds: int = 30,
    health: Sequence[Mapping[str, Any]] | None = None,
    email_recipient: str | None = None,
) -> list[dict[str, Any]]:
    now = _aware(now or datetime.now(timezone.utc))
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    init_alert_schema(connection)
    email_recipient = (
        str(email_recipient).strip()
        if email_recipient is not None
        else os.environ.get("DOTA2_ALERT_EMAIL_RECIPIENT", "").strip()
    )
    now_iso = now.isoformat()
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        conditions, paper_signal_available = _conditions(connection, health)
        for dedupe_key, condition in conditions.items():
            active = connection.execute(
                """SELECT incident_id FROM monitor_alert_incidents
                    WHERE dedupe_key=? AND status='active'""",
                (dedupe_key,),
            ).fetchone()
            if active is not None:
                incident_id = int(active[0])
                latest_audit = connection.execute(
                    """SELECT MAX(created_at) FROM monitor_alert_audit
                        WHERE incident_id=? AND action IN ('opened', 'observed')""",
                    (incident_id,),
                ).fetchone()[0]
                should_audit = (
                    latest_audit is None
                    or (now - _parse(latest_audit)).total_seconds()
                    >= _OBSERVATION_AUDIT_SECONDS
                )
                connection.execute(
                    """UPDATE monitor_alert_incidents
                          SET last_detected_at=?, severity=?, title=?, body=?,
                              source_json=?,
                              occurrence_count=occurrence_count+?
                        WHERE incident_id=?""",
                    (
                        now_iso,
                        condition["severity"],
                        condition["title"],
                        condition["body"],
                        _json(condition["source"]),
                        int(should_audit),
                        incident_id,
                    ),
                )
                if should_audit:
                    _audit(
                        connection,
                        incident_id,
                        "observed",
                        "system",
                        "condition persists",
                        now,
                    )
                continue

            first_detected = now
            if condition["category"] == "operational" and grace_seconds:
                candidate = connection.execute(
                    "SELECT first_detected_at FROM monitor_alert_candidates WHERE dedupe_key=?",
                    (dedupe_key,),
                ).fetchone()
                if candidate is None:
                    connection.execute(
                        """INSERT INTO monitor_alert_candidates
                           (dedupe_key, first_detected_at, last_detected_at, payload_json)
                           VALUES (?, ?, ?, ?)""",
                        (dedupe_key, now_iso, now_iso, _json(condition)),
                    )
                    continue
                first_detected = _parse(candidate[0])
                connection.execute(
                    """UPDATE monitor_alert_candidates
                          SET last_detected_at=?, payload_json=? WHERE dedupe_key=?""",
                    (now_iso, _json(condition), dedupe_key),
                )
                if (now - first_detected).total_seconds() < grace_seconds:
                    continue
                connection.execute(
                    "DELETE FROM monitor_alert_candidates WHERE dedupe_key=?",
                    (dedupe_key,),
                )

            episode = int(connection.execute(
                """SELECT COALESCE(MAX(episode), 0) + 1
                    FROM monitor_alert_incidents WHERE dedupe_key=?""",
                (dedupe_key,),
            ).fetchone()[0])
            cursor = connection.execute(
                """INSERT INTO monitor_alert_incidents
                   (dedupe_key, episode, category, severity, title, body, status,
                    first_detected_at, opened_at, last_detected_at, source_json)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (
                    dedupe_key,
                    episode,
                    condition["category"],
                    condition["severity"],
                    condition["title"],
                    condition["body"],
                    first_detected.isoformat(),
                    now_iso,
                    now_iso,
                    _json(condition["source"]),
                ),
            )
            incident_id = int(cursor.lastrowid)
            _audit(connection, incident_id, "opened", "system", condition["body"], now)
            _enqueue_email(
                connection,
                incident_id=incident_id,
                event_type=EVENT_MONITOR_ALERT,
                condition=condition,
                now=now,
                recipient=email_recipient,
            )

        placeholders = ",".join("?" for _ in conditions)
        active_rows = connection.execute(
            """SELECT incident_id, dedupe_key, category, severity, title, body,
                      source_json
                 FROM monitor_alert_incidents WHERE status='active'"""
        ).fetchall()
        for row in active_rows:
            if str(row[1]) in conditions:
                continue
            if str(row[2]) == "paper_signal" and not paper_signal_available:
                continue
            incident_id = int(row[0])
            connection.execute(
                """UPDATE monitor_alert_incidents
                      SET status='recovered', recovered_at=? WHERE incident_id=?""",
                (now_iso, incident_id),
            )
            _audit(connection, incident_id, "recovered", "system", "condition cleared", now)
            _enqueue_email(
                connection,
                incident_id=incident_id,
                event_type=EVENT_MONITOR_RECOVERY,
                condition={
                    "category": str(row[2]),
                    "severity": str(row[3]),
                    "title": str(row[4]),
                    "body": str(row[5]),
                    "source": _alert_source(row[6], str(row[1])),
                },
                now=now,
                recipient=email_recipient,
            )
        if conditions:
            connection.execute(
                f"DELETE FROM monitor_alert_candidates WHERE dedupe_key NOT IN ({placeholders})",
                tuple(conditions),
            )
        else:
            connection.execute("DELETE FROM monitor_alert_candidates")
    return active_alerts(connection)


def active_alerts(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            """SELECT incident_id, dedupe_key, episode, category, severity, title,
                      body, first_detected_at, opened_at, last_detected_at,
                      acknowledged_at, acknowledged_by, source_json, occurrence_count
                 FROM monitor_alert_incidents WHERE status='active'
                ORDER BY CASE WHEN acknowledged_at IS NULL THEN 0 ELSE 1 END,
                         CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                         opened_at DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "incident_id": int(row[0]),
            "dedupe_key": str(row[1]),
            "episode": int(row[2]),
            "category": str(row[3]),
            "severity": str(row[4]),
            "title": str(row[5]),
            "body": str(row[6]),
            "first_detected_at": str(row[7]),
            "opened_at": str(row[8]),
            "last_detected_at": str(row[9]),
            "acknowledged_at": row[10],
            "acknowledged_by": row[11],
            "source": json.loads(str(row[12])),
            "occurrence_count": int(row[13]),
        }
        for row in rows
    ]


def acknowledge_alert(
    connection: sqlite3.Connection,
    *,
    incident_id: int,
    actor: str,
    acknowledged_at: datetime | None = None,
) -> bool:
    acknowledged_at = _aware(acknowledged_at or datetime.now(timezone.utc))
    actor = " ".join(str(actor).split())[:100]
    if incident_id <= 0 or not actor:
        raise ValueError("valid incident and actor are required")
    with connection:
        changed = connection.execute(
            """UPDATE monitor_alert_incidents
                  SET acknowledged_at=?, acknowledged_by=?
                WHERE incident_id=? AND status='active' AND acknowledged_at IS NULL""",
            (acknowledged_at.isoformat(), actor, incident_id),
        )
        if changed.rowcount != 1:
            return False
        _audit(
            connection,
            incident_id,
            "acknowledged",
            actor,
            "operator acknowledged incident",
            acknowledged_at,
        )
        return True


def _conditions(
    connection: sqlite3.Connection,
    health: Sequence[Mapping[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    if health is None:
        try:
            rows = connection.execute(
                """SELECT component, status, last_error, last_heartbeat_at
                    FROM service_health"""
            ).fetchall()
            health = [
                {
                    "component": row[0],
                    "status": row[1],
                    "last_error": row[2],
                    "last_heartbeat_at": row[3],
                }
                for row in rows
            ]
        except sqlite3.OperationalError:
            health = []
    conditions: dict[str, dict[str, Any]] = {}
    for item in health:
        component = str(item.get("component") or "").strip()
        status = str(item.get("status") or "missing")
        last_error = str(item.get("last_error") or status)
        if (
            not component.endswith("_worker")
            or status in {"healthy", "starting"}
            or (
                component == "mail_worker"
                and last_error == "configuration_missing"
            )
        ):
            continue
        conditions[f"operational:{component}"] = {
            "category": "operational",
            "severity": "critical" if status in {"unhealthy", "stopped"} else "warning",
            "title": f"{component} 状态异常",
            "body": last_error,
            "source": {"component": component, "status": status, "last_error": last_error},
        }
    paper_conditions, paper_signal_available = _paper_signal_conditions(connection)
    conditions.update(paper_conditions)
    return conditions, paper_signal_available


def _paper_signal_conditions(
    connection: sqlite3.Connection,
) -> tuple[dict[str, dict[str, Any]], bool]:
    try:
        schema_issues = _paper_signal_schema_issues(connection)
    except sqlite3.Error as exc:
        return (
            _paper_signal_contract_failure(
                "schema_inspection_failed",
                [f"{type(exc).__name__}: {exc}"],
            ),
            False,
        )
    if schema_issues:
        return (
            _paper_signal_contract_failure("schema_incomplete", schema_issues),
            False,
        )

    try:
        rows = connection.execute(_PAPER_SIGNAL_QUERY).fetchall()
    except sqlite3.Error as exc:
        return (
            _paper_signal_contract_failure(
                "query_failed",
                [f"{type(exc).__name__}: {exc}"],
            ),
            False,
        )

    signal_conditions: dict[str, dict[str, Any]] = {}
    for row in rows:
        order_key = str(row[0])
        lineage_issue = decision_lineage_block_reason(connection, order_key)
        if lineage_issue is not None:
            return (
                _paper_signal_contract_failure(
                    "decision_lineage_invalid",
                    [f"order_key={order_key}: {lineage_issue}"],
                ),
                False,
            )
        try:
            model_probability = _paper_signal_probability(
                row[3], "model_probability"
            )
            market_probability = _paper_signal_probability(
                row[4], "market_probability"
            )
            signal_price = _paper_signal_price(row[5])
        except (TypeError, ValueError, OverflowError) as exc:
            return (
                _paper_signal_contract_failure(
                    "invalid_payload",
                    [f"order_key={order_key}: {exc}"],
                ),
                False,
            )
        signal_conditions[f"paper_signal:{order_key}"] = {
            "category": "paper_signal",
            "severity": "warning",
            "title": f"纸面信号 {row[1]}",
            "body": f"{row[2]} @ {signal_price:.2f}",
            "source": {
                "order_key": order_key,
                "raybet_match_id": str(row[1]),
                "market_key": str(row[2]),
                "model_probability": model_probability,
                "market_probability": market_probability,
                "signal_price": signal_price,
                "signaled_at": str(row[6]),
            },
        }
    return signal_conditions, True


def _alert_source(value: Any, dedupe_key: str) -> dict[str, Any]:
    try:
        source = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        source = None
    if isinstance(source, dict):
        return source
    return {"dedupe_key": dedupe_key}


def _paper_signal_schema_issues(connection: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for relation, required_columns in _PAPER_SIGNAL_REQUIRED_COLUMNS.items():
        relation_row = connection.execute(
            """SELECT type FROM sqlite_master
                 WHERE type IN ('table', 'view') AND name=?""",
            (relation,),
        ).fetchone()
        if relation_row is None:
            issues.append(f"missing_relation:{relation}")
            continue
        columns = {
            str(row[1])
            for row in connection.execute(
                f'PRAGMA table_info("{relation}")'
            ).fetchall()
        }
        for column in sorted(required_columns - columns):
            issues.append(f"missing_column:{relation}.{column}")
    return issues


def _paper_signal_contract_failure(
    reason: str,
    issues: Sequence[str],
) -> dict[str, dict[str, Any]]:
    issue_list = list(issues)
    return {
        "operational:paper_signal_contract": {
            "category": "operational",
            "severity": "critical",
            "title": "纸面信号数据契约异常",
            "body": "paper_signal suppressed: " + "; ".join(issue_list),
            "source": {
                "component": "paper_signal",
                "status": "unavailable",
                "reason": reason,
                "issues": issue_list,
            },
        }
    }


def _paper_signal_probability(value: Any, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be finite and between 0 and 1")
    return number


def _paper_signal_price(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 1.0:
        raise ValueError("signal_price must be finite and greater than 1")
    return number


def _enqueue_email(
    connection: sqlite3.Connection,
    *,
    incident_id: int,
    event_type: str,
    condition: Mapping[str, Any],
    now: datetime,
    recipient: str,
) -> None:
    if not recipient:
        return
    source = condition["source"]
    outbox_order_key = f"monitor-incident-{incident_id}"
    if condition["category"] == "paper_signal" and isinstance(source, Mapping):
        paper_order_key = str(source.get("order_key") or "").strip()
        if paper_order_key:
            outbox_order_key = paper_order_key
    enqueue(
        connection,
        order_key=outbox_order_key,
        event_type=event_type,
        payload={
            "incident_id": incident_id,
            "category": condition["category"],
            "severity": condition["severity"],
            "title": condition["title"],
            "body": condition["body"],
            "source": source,
            "event_type": event_type,
        },
        stats_cutoff_at=now,
        created_at=now,
        recipient=recipient,
        template_version=MONITOR_TEMPLATE_VERSION,
    )


def _audit(
    connection: sqlite3.Connection,
    incident_id: int,
    action: str,
    actor: str,
    detail: str,
    created_at: datetime,
) -> None:
    connection.execute(
        """INSERT INTO monitor_alert_audit
           (incident_id, action, actor, detail, created_at) VALUES (?, ?, ?, ?, ?)""",
        (incident_id, action, actor, detail[:500], created_at.isoformat()),
    )


def _migrate_notification_outbox(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='notification_outbox'"
    ).fetchone()
    if row is None:
        return
    nested = connection.in_transaction
    if nested:
        connection.execute("SAVEPOINT notification_outbox_monitor_migration")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        if "monitor_alert" not in str(row[0]):
            connection.execute(
                "DROP TRIGGER IF EXISTS notification_outbox_payload_immutable"
            )
            connection.execute("DROP TABLE IF EXISTS notification_outbox_monitor_v1")
            connection.execute(
                """CREATE TABLE notification_outbox_monitor_v1 (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_key TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK (event_type IN
                        ('filled', 'settled', 'monitor_alert', 'monitor_recovery')),
                    channel TEXT NOT NULL DEFAULT 'email',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'leased', 'sent', 'dead_letter')),
                    recipient TEXT NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    statistics_cutoff TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    lease_token TEXT,
                    lease_until TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    next_attempt_at TEXT,
                    last_error TEXT,
                    sent_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (order_key, event_type, channel)
                )"""
            )
            columns = (
                "outbox_id, order_key, event_type, channel, status, recipient, "
                "message_id, payload_json, statistics_cutoff, template_version, "
                "lease_token, lease_until, attempt_count, next_attempt_at, "
                "last_error, sent_at, created_at, updated_at"
            )
            connection.execute(
                f"""INSERT INTO notification_outbox_monitor_v1 ({columns})
                    SELECT {columns} FROM notification_outbox"""
            )
            connection.execute("DROP TABLE notification_outbox")
            connection.execute(
                "ALTER TABLE notification_outbox_monitor_v1 "
                "RENAME TO notification_outbox"
            )
        connection.execute("DROP INDEX IF EXISTS idx_notification_outbox_due")
        connection.execute(
            """CREATE INDEX idx_notification_outbox_due
                 ON notification_outbox(status, next_attempt_at, lease_until)"""
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS notification_outbox_payload_immutable"
        )
        connection.execute(
            """CREATE TRIGGER notification_outbox_payload_immutable
               BEFORE UPDATE ON notification_outbox
               WHEN OLD.order_key IS NOT NEW.order_key
                 OR OLD.event_type IS NOT NEW.event_type
                 OR OLD.channel IS NOT NEW.channel
                 OR OLD.payload_json IS NOT NEW.payload_json
                 OR OLD.statistics_cutoff IS NOT NEW.statistics_cutoff
                 OR OLD.template_version IS NOT NEW.template_version
                 OR OLD.recipient IS NOT NEW.recipient
                 OR OLD.message_id IS NOT NEW.message_id
               BEGIN
                   SELECT RAISE(ABORT, 'notification outbox payload is immutable');
               END"""
        )
    except Exception:
        if nested:
            connection.execute("ROLLBACK TO notification_outbox_monitor_migration")
            connection.execute("RELEASE notification_outbox_monitor_migration")
        else:
            connection.rollback()
        raise
    else:
        if nested:
            connection.execute("RELEASE notification_outbox_monitor_migration")
        else:
            connection.commit()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("alert times must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse(value: Any) -> datetime:
    return _aware(datetime.fromisoformat(str(value)))


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
