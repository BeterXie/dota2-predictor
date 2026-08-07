"""Operational alerts for the retained live collection runtime."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy.exc import SQLAlchemyError

from database.session import PostgresSession
from live_betting.runtime_schema import verify_runtime_schema


_MONITORED_COMPONENTS = frozenset(
    {
        "raybet_worker",
        "raybet_priority_odds_worker",
        "raybet_full_odds_worker",
        "strict_ingest_worker",
        "stream_supervisor",
        "vision_worker",
    }
)


def reconcile_alerts(
    connection: PostgresSession,
    *,
    now: datetime | None = None,
    health: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    observed = _aware(now or datetime.now(timezone.utc))
    verify_runtime_schema(connection)
    conditions = _conditions(connection, health)
    with connection.transaction():
        for dedupe_key, condition in conditions.items():
            row = connection.execute(
                """SELECT incident_id FROM monitor_alert_incidents
                    WHERE dedupe_key=? AND status='active'""",
                (dedupe_key,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """UPDATE monitor_alert_incidents
                          SET last_detected_at=?, severity=?, title=?, body=?,
                              source_json=?, occurrence_count=occurrence_count+1
                        WHERE incident_id=?""",
                    (
                        observed.isoformat(),
                        condition["severity"],
                        condition["title"],
                        condition["body"],
                        _json(condition["source"]),
                        int(row[0]),
                    ),
                )
                continue
            inserted = connection.execute(
                """INSERT INTO monitor_alert_incidents
                   (dedupe_key, episode, category, severity, title, body, status,
                    first_detected_at, opened_at, last_detected_at, source_json)
                   VALUES (?,
                           COALESCE((SELECT MAX(episode)+1 FROM monitor_alert_incidents
                                      WHERE dedupe_key=?), 1),
                           'operational', ?, ?, ?, 'active', ?, ?, ?, ?)
                   RETURNING incident_id""",
                (
                    dedupe_key,
                    dedupe_key,
                    condition["severity"],
                    condition["title"],
                    condition["body"],
                    observed.isoformat(),
                    observed.isoformat(),
                    observed.isoformat(),
                    _json(condition["source"]),
                ),
            ).fetchone()
            if inserted is None:
                raise RuntimeError("alert incident insert did not return an identity")
            _audit(connection, int(inserted[0]), "opened", "system", condition["body"], observed)

        active = connection.execute(
            """SELECT incident_id, dedupe_key FROM monitor_alert_incidents
                WHERE status='active' AND category='operational'"""
        ).fetchall()
        for incident_id, dedupe_key in active:
            if str(dedupe_key) in conditions:
                continue
            connection.execute(
                """UPDATE monitor_alert_incidents
                      SET status='recovered', recovered_at=? WHERE incident_id=?""",
                (observed.isoformat(), int(incident_id)),
            )
            _audit(connection, int(incident_id), "recovered", "system", "condition cleared", observed)
    return active_alerts(connection)


def active_alerts(connection: PostgresSession) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            """SELECT incident_id, dedupe_key, episode, category, severity, title,
                      body, first_detected_at, opened_at, last_detected_at,
                      acknowledged_at, acknowledged_by, source_json, occurrence_count
                 FROM monitor_alert_incidents
                WHERE status='active' AND category='operational'
                ORDER BY CASE WHEN acknowledged_at IS NULL THEN 0 ELSE 1 END,
                         CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                         opened_at DESC"""
        ).fetchall()
    except SQLAlchemyError:
        return []
    return [
        {
            "incident_id": int(row[0]),
            "dedupe_key": str(row[1]),
            "episode": int(row[2]),
            "category": "operational",
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
    connection: PostgresSession,
    *,
    incident_id: int,
    actor: str,
    acknowledged_at: datetime | None = None,
) -> bool:
    observed = _aware(acknowledged_at or datetime.now(timezone.utc))
    normalized_actor = " ".join(str(actor).split())[:100]
    if incident_id <= 0 or not normalized_actor:
        raise ValueError("valid incident and actor are required")
    verify_runtime_schema(connection)
    with connection.transaction():
        changed = connection.execute(
            """UPDATE monitor_alert_incidents
                  SET acknowledged_at=?, acknowledged_by=?
                WHERE incident_id=? AND status='active' AND category='operational'
                  AND acknowledged_at IS NULL""",
            (observed.isoformat(), normalized_actor, incident_id),
        )
        if changed.rowcount != 1:
            return False
        _audit(
            connection,
            incident_id,
            "acknowledged",
            normalized_actor,
            "operator acknowledged incident",
            observed,
        )
    return True


def _conditions(
    connection: PostgresSession,
    health: Sequence[Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if health is None:
        rows = connection.execute(
            """SELECT component, status, last_error FROM service_health"""
        ).fetchall()
        health = [
            {"component": row[0], "status": row[1], "last_error": row[2]}
            for row in rows
        ]
    result: dict[str, dict[str, Any]] = {}
    for item in health:
        component = str(item.get("component") or "").strip()
        status = str(item.get("status") or "missing")
        if component not in _MONITORED_COMPONENTS or status in {"healthy", "starting"}:
            continue
        error = str(item.get("last_error") or status)
        result[f"operational:{component}"] = {
            "severity": "critical" if status in {"unhealthy", "stopped"} else "warning",
            "title": f"{component} 状态异常",
            "body": error,
            "source": {"component": component, "status": status, "last_error": error},
        }
    return result


def _audit(
    connection: PostgresSession,
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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("alert times must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
