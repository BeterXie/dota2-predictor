"""Read-only projections for the local live-monitoring console."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_OPEN_MATCH_STATUSES = {"1", "5", "open", "active", "running"}
_ENDED_MATCH_STATUSES = {"2", "ended", "finished", "settled", "closed"}
_EXPECTED_HEALTH_COMPONENTS = {
    "raybet_worker": 45.0,
    "shadow_worker": 45.0,
    "mail_worker": 90.0,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def derive_health(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return reported health with freshness-derived status.

    A historical ``healthy`` row is not evidence that its worker is still alive.
    """

    checked_at = _aware_utc(now or utc_now())
    rows = _rows(
        connection,
        "SELECT * FROM service_health ORDER BY component",
    )
    by_component = {str(row["component"]): row for row in rows}
    names = sorted(set(by_component) | set(_EXPECTED_HEALTH_COMPONENTS))
    output: list[dict[str, Any]] = []
    for component in names:
        row = by_component.get(component)
        if row is None:
            output.append(
                {
                    "component": component,
                    "status": "stopped",
                    "reported_status": None,
                    "freshness": "missing",
                    "age_seconds": None,
                    "last_heartbeat_at": None,
                    "last_success_at": None,
                    "last_error_at": None,
                    "last_error": None,
                    "details": {},
                }
            )
            continue

        reported = str(row["status"])
        heartbeat = _parse_time(row["last_heartbeat_at"])
        age = max(0.0, (checked_at - heartbeat).total_seconds()) if heartbeat else None
        limit = _EXPECTED_HEALTH_COMPONENTS.get(component, 120.0)
        if heartbeat is None:
            status, freshness = "unhealthy", "missing"
        elif age is not None and age > limit * 2:
            status, freshness = "unhealthy", "stale"
        elif age is not None and age > limit:
            status, freshness = "degraded", "delayed"
        else:
            status = reported
            freshness = "fresh"
        if reported == "stopped" and freshness == "fresh":
            status = "stopped"

        output.append(
            {
                "component": component,
                "status": status,
                "reported_status": reported,
                "freshness": freshness,
                "age_seconds": round(age, 3) if age is not None else None,
                "last_heartbeat_at": row["last_heartbeat_at"],
                "last_success_at": row["last_success_at"],
                "last_error_at": row["last_error_at"],
                "last_error": row["last_error"],
                "details": _json_object(row["details_json"]),
            }
        )
    return output


def build_monitor_snapshot(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    checked_at = _aware_utc(now or utc_now())
    health = derive_health(connection, now=checked_at)
    matches = monitor_matches(connection, now=checked_at)
    return {
        "generated_at": checked_at.isoformat(),
        "cursor": monitor_cursor(connection),
        "health": health,
        "matches": matches,
        "summary": {
            "total": len(matches),
            "upcoming": sum(item["lifecycle"] == "upcoming" for item in matches),
            "live": sum(item["lifecycle"] == "live" for item in matches),
            "degraded": sum(item["lifecycle"] == "degraded" for item in matches),
            "ended": sum(item["lifecycle"] == "ended" for item in matches),
            "unhealthy_components": sum(
                item["status"] in {"degraded", "unhealthy", "stopped"}
                for item in health
                if item["component"] in _EXPECTED_HEALTH_COMPONENTS
            ),
        },
    }


def monitor_matches(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    checked_at = _aware_utc(now or utc_now())
    rows = _rows(
        connection,
        """SELECT raybet_match_id, tournament, team_one, team_two,
                  scheduled_at, best_of, status, live_url, updated_at
             FROM raybet_matches
            ORDER BY scheduled_at DESC, raybet_match_id DESC""",
    )
    health_by_component = {
        item["component"]: item for item in derive_health(connection, now=checked_at)
    }
    output = [
        _monitor_match(connection, row, checked_at, health_by_component)
        for row in rows
    ]
    lifecycle_order = {"live": 0, "degraded": 1, "upcoming": 2, "ended": 3}
    output.sort(
        key=lambda item: (
            lifecycle_order.get(str(item["lifecycle"]), 9),
            str(item.get("scheduled_at") or ""),
            str(item["raybet_match_id"]),
        )
    )
    return output


def monitor_match_detail(
    connection: sqlite3.Connection,
    raybet_match_id: str,
    *,
    now: datetime | None = None,
    max_points: int = 1200,
) -> dict[str, Any] | None:
    checked_at = _aware_utc(now or utc_now())
    row = connection.execute(
        """SELECT raybet_match_id, tournament, team_one, team_two,
                  scheduled_at, best_of, status, live_url, updated_at
             FROM raybet_matches WHERE raybet_match_id=?""",
        (raybet_match_id,),
    ).fetchone()
    if row is None:
        return None
    health_by_component = {
        item["component"]: item for item in derive_health(connection, now=checked_at)
    }
    summary = _monitor_match(connection, row, checked_at, health_by_component)
    return {
        **summary,
        "winner_timeline": winner_timeline(
            connection, raybet_match_id, max_points=max_points
        ),
        "decisions": _strategy_decisions(connection, raybet_match_id),
        "vision": _vision_timeline(connection, raybet_match_id),
        "markets": current_markets(connection, raybet_match_id),
    }


def winner_timeline(
    connection: sqlite3.Connection,
    raybet_match_id: str,
    *,
    max_points: int = 1200,
) -> list[dict[str, Any]]:
    rows = _rows(
        connection,
        """SELECT odds.id, odds.received_at, odds.price, odds.status,
                  odds.period, odds.side, odds.odds_id,
                  alignment.map_number, alignment.game_clock_seconds,
                  alignment.method AS alignment_method,
                  alignment.lag_seconds, alignment.usable AS alignment_usable
             FROM odds_snapshots AS odds
             LEFT JOIN odds_alignments AS alignment
               ON alignment.odds_snapshot_id=odds.id
            WHERE odds.raybet_match_id=?
              AND odds.market_type='winner' AND odds.supported=1
            ORDER BY odds.received_at, odds.period, odds.id""",
        (raybet_match_id,),
    )
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        side = str(row["side"] or "")
        if side not in {"team_one", "team_two"}:
            continue
        key = (str(row["received_at"]), str(row["period"]))
        grouped[key][side] = row

    points: list[dict[str, Any]] = []
    for (observed_at, period), quotes in sorted(grouped.items()):
        if set(quotes) != {"team_one", "team_two"}:
            continue
        prices = {side: float(quotes[side]["price"]) for side in quotes}
        if any(price <= 1.0 for price in prices.values()):
            continue
        inverse = {side: 1.0 / price for side, price in prices.items()}
        total = sum(inverse.values())
        aligned = next(
            (
                quote
                for quote in quotes.values()
                if quote["alignment_usable"]
                and quote["game_clock_seconds"] is not None
            ),
            None,
        )
        points.append(
            {
                "observed_at": observed_at,
                "period": period,
                "prices": prices,
                "probabilities": {
                    side: round(value / total, 8) for side, value in inverse.items()
                },
                "status": {side: str(quotes[side]["status"]) for side in quotes},
                "game_clock_seconds": (
                    int(aligned["game_clock_seconds"]) if aligned is not None else None
                ),
                "map_number": int(aligned["map_number"]) if aligned is not None else None,
                "alignment": (
                    {
                        "method": aligned["alignment_method"],
                        "lag_seconds": aligned["lag_seconds"],
                    }
                    if aligned is not None
                    else None
                ),
            }
        )
    return _downsample(points, max_points)


def current_markets(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> list[dict[str, Any]]:
    rows = _rows(
        connection,
        """WITH ranked AS (
               SELECT *, ROW_NUMBER() OVER (
                   PARTITION BY odds_id ORDER BY received_at DESC, id DESC
               ) AS rank
               FROM odds_snapshots WHERE raybet_match_id=?
           )
           SELECT odds_id, odds_group_id, received_at, price, status,
                  market_type, period, side, line, outcome_key, supported
             FROM ranked WHERE rank=1
            ORDER BY CASE WHEN market_type='winner' THEN 0 ELSE 1 END,
                     period, market_type, odds_group_id, outcome_key""",
        (raybet_match_id,),
    )
    return [dict(row) for row in rows]


def monitor_cursor(connection: sqlite3.Connection) -> str:
    values = {
        "match": _max_value(connection, "raybet_matches", "updated_at"),
        "odds": _max_value(connection, "odds_snapshots", "id"),
        "vision": _max_value(connection, "vision_observations", "captured_at"),
        "decision": _max_value(connection, "strategy_decisions", "decided_at"),
        "health": _max_value(connection, "service_health", "updated_at"),
        "mapping": _max_value(connection, "strict_live_map_mappings", "mapping_id"),
        "control": _max_value(connection, "monitor_control_audit", "audit_id"),
    }
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _monitor_match(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
    health: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    match_id = str(row["raybet_match_id"])
    latest_odds = _latest_row(
        connection,
        """SELECT received_at AS observed_at FROM odds_snapshots
            WHERE raybet_match_id=? ORDER BY received_at DESC, id DESC LIMIT 1""",
        (match_id,),
    )
    latest_vision = _latest_row(
        connection,
        """SELECT captured_at AS observed_at, map_number, game_clock_seconds,
                  screen_state, confirmed, clock_confidence, draft_confidence
             FROM vision_observations WHERE raybet_match_id=?
            ORDER BY captured_at DESC LIMIT 1""",
        (match_id,),
    )
    latest_decision = _latest_row(
        connection,
        """SELECT decided_at AS observed_at, map_number, model_probability,
                  market_probability, edge, eligible, reason, strategy_version
             FROM strategy_decisions WHERE raybet_match_id=?
            ORDER BY decided_at DESC LIMIT 1""",
        (match_id,),
    )
    mapping_count = int(
        _scalar(
            connection,
            "SELECT COUNT(*) FROM strict_live_map_mappings WHERE raybet_match_id=?",
            (match_id,),
            default=0,
        )
    )

    odds_readiness = _freshness(latest_odds, now, warning=15.0, stale=60.0)
    vision_readiness = _freshness(latest_vision, now, warning=20.0, stale=120.0)
    if latest_vision and not bool(latest_vision["confirmed"]):
        vision_readiness["status"] = "unconfirmed"
    decision_readiness = _freshness(latest_decision, now, warning=30.0, stale=120.0)
    mapping_readiness = {
        "status": "ready" if mapping_count else "missing",
        "count": mapping_count,
    }
    shadow_health = health.get("shadow_worker", {"status": "stopped"})
    strategy_readiness = {
        "status": (
            "ready" if shadow_health.get("status") == "healthy" else shadow_health.get("status", "stopped")
        ),
        "component": "shadow_worker",
    }
    lifecycle = _lifecycle(
        str(row["status"] or ""),
        row["scheduled_at"],
        latest_vision,
        vision_readiness,
        checked_at=now,
    )
    current_winner = _current_winner(connection, match_id)
    return {
        "raybet_match_id": match_id,
        "tournament": row["tournament"],
        "team_one": row["team_one"],
        "team_two": row["team_two"],
        "scheduled_at": row["scheduled_at"],
        "best_of": row["best_of"],
        "provider_status": str(row["status"] or ""),
        "live_url": row["live_url"],
        "updated_at": row["updated_at"],
        "lifecycle": lifecycle,
        "winner": current_winner,
        "latest_vision": dict(latest_vision) if latest_vision else None,
        "latest_decision": dict(latest_decision) if latest_decision else None,
        "readiness": {
            "odds": odds_readiness,
            "mapping": mapping_readiness,
            "vision": vision_readiness,
            "model": decision_readiness,
            "strategy": strategy_readiness,
        },
    }


def _current_winner(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT received_at FROM odds_snapshots
            WHERE raybet_match_id=? AND market_type='winner' AND supported=1
            ORDER BY received_at DESC, id DESC LIMIT 1""",
        (raybet_match_id,),
    ).fetchone()
    if row is None:
        return None
    observed_at = str(row["received_at"])
    quotes = _rows(
        connection,
        """SELECT side, price, status, period FROM odds_snapshots
            WHERE raybet_match_id=? AND market_type='winner' AND supported=1
              AND received_at=? ORDER BY id""",
        (raybet_match_id, observed_at),
    )
    by_side: dict[str, sqlite3.Row] = {}
    for quote in quotes:
        side = str(quote["side"] or "")
        if side in {"team_one", "team_two"}:
            by_side[side] = quote
    if set(by_side) != {"team_one", "team_two"}:
        return {"observed_at": observed_at, "complete": False}
    prices = {side: float(by_side[side]["price"]) for side in by_side}
    if any(price <= 1.0 for price in prices.values()):
        return {"observed_at": observed_at, "complete": False, "prices": prices}
    inverse = {side: 1.0 / price for side, price in prices.items()}
    total = sum(inverse.values())
    return {
        "observed_at": observed_at,
        "period": str(next(iter(by_side.values()))["period"]),
        "complete": True,
        "prices": prices,
        "probabilities": {
            side: round(value / total, 8) for side, value in inverse.items()
        },
    }


def _strategy_decisions(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in _rows(
            connection,
            """SELECT decision_key, map_number, decided_at, underdog_side,
                      market_probability, model_probability, edge, data_quality,
                      eligible, reason, contributions_json, input_ref,
                      strategy_version
                 FROM strategy_decisions WHERE raybet_match_id=?
                ORDER BY decided_at, decision_key""",
            (raybet_match_id,),
        )
    ]


def _vision_timeline(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in _rows(
            connection,
            """SELECT captured_at, map_number, game_clock_seconds, is_paused,
                      radiant_team_side, clock_confidence, draft_confidence,
                      source_frame_ref, screen_state, confirmed
                 FROM vision_observations WHERE raybet_match_id=?
                ORDER BY captured_at, source_frame_ref""",
            (raybet_match_id,),
        )
    ]


def _freshness(
    row: sqlite3.Row | None,
    now: datetime,
    *,
    warning: float,
    stale: float,
) -> dict[str, Any]:
    if row is None:
        return {"status": "missing", "observed_at": None, "age_seconds": None}
    observed_at = _parse_time(row["observed_at"])
    if observed_at is None:
        return {"status": "invalid", "observed_at": row["observed_at"], "age_seconds": None}
    age = max(0.0, (now - observed_at).total_seconds())
    status = "ready" if age <= warning else "delayed" if age <= stale else "stale"
    return {
        "status": status,
        "observed_at": observed_at.isoformat(),
        "age_seconds": round(age, 3),
    }


def _lifecycle(
    provider_status: str,
    scheduled_at: object,
    latest_vision: sqlite3.Row | None,
    vision_readiness: dict[str, Any],
    *,
    checked_at: datetime,
) -> str:
    normalized = provider_status.casefold()
    if normalized in _ENDED_MATCH_STATUSES:
        return "ended"
    if (
        latest_vision is not None
        and bool(latest_vision["confirmed"])
        and str(latest_vision["screen_state"]) == "game"
        and vision_readiness["status"] in {"ready", "delayed"}
    ):
        return "live"
    scheduled = _parse_schedule(scheduled_at)
    if scheduled is not None and scheduled > checked_at + timedelta(minutes=15):
        return "upcoming"
    if normalized in _OPEN_MATCH_STATUSES:
        return "degraded"
    return "degraded"


def _parse_schedule(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_LOCAL_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("monitor timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _rows(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[sqlite3.Row]:
    try:
        return list(connection.execute(query, params).fetchall())
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return []
        raise


def _latest_row(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...],
) -> sqlite3.Row | None:
    try:
        return connection.execute(query, params).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return None
        raise


def _scalar(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...] = (),
    *,
    default: Any = None,
) -> Any:
    try:
        row = connection.execute(query, params).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return default
        raise
    return row[0] if row is not None else default


def _max_value(connection: sqlite3.Connection, table: str, column: str) -> Any:
    return _scalar(connection, f"SELECT MAX({column}) FROM {table}", default=None)


def _json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _downsample(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 1:
        raise ValueError("max_points must be greater than one")
    if len(points) <= limit:
        return points
    last = len(points) - 1
    indexes = sorted({round(index * last / (limit - 1)) for index in range(limit)})
    return [points[index] for index in indexes]


__all__ = [
    "build_monitor_snapshot",
    "current_markets",
    "derive_health",
    "monitor_cursor",
    "monitor_match_detail",
    "monitor_matches",
    "winner_timeline",
]
