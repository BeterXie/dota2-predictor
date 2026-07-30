"""Small, durable component-health API shared by the local supervisor."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from database.session import PostgresSession


HEALTH_STATUSES = {"starting", "healthy", "degraded", "unhealthy", "stopped"}


def record_health(
    connection: PostgresSession,
    component: str,
    status: str,
    *,
    heartbeat_at: datetime,
    success_at: datetime | None = None,
    error_at: datetime | None = None,
    error: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    if status not in HEALTH_STATUSES:
        raise ValueError(f"unsupported health status: {status}")
    if not component.strip():
        raise ValueError("component is required")
    details_json = json.dumps(
        dict(details or {}), sort_keys=True, separators=(",", ":"), default=str
    )
    now = heartbeat_at.astimezone(timezone.utc).isoformat()
    with connection.transaction():
        connection.execute(
            """INSERT INTO service_health
               (component, status, last_heartbeat_at, last_success_at,
                last_error_at, last_error, details_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(component) DO UPDATE SET
                 status=excluded.status,
                 last_heartbeat_at=excluded.last_heartbeat_at,
                 last_success_at=COALESCE(excluded.last_success_at,
                                          service_health.last_success_at),
                 last_error_at=excluded.last_error_at,
                 last_error=excluded.last_error,
                 details_json=excluded.details_json,
                 updated_at=excluded.updated_at""",
            (
                component,
                status,
                now,
                success_at.astimezone(timezone.utc).isoformat() if success_at else None,
                error_at.astimezone(timezone.utc).isoformat() if error_at else None,
                _safe_error(error),
                details_json,
                now,
            ),
        )


def read_health(connection: PostgresSession) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM service_health ORDER BY component"
    ).fetchall()
    return [
        {
            "component": str(row["component"]),
            "status": str(row["status"]),
            "last_heartbeat_at": row["last_heartbeat_at"],
            "last_success_at": row["last_success_at"],
            "last_error_at": row["last_error_at"],
            "last_error": row["last_error"],
            "details": json.loads(str(row["details_json"])),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _safe_error(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())[:500]

__all__ = ["HEALTH_STATUSES", "read_health", "record_health"]
