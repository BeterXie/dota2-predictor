"""Read-only projections for the local live-monitoring console."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from live_betting.raybet_state import raybet_match_is_live, raybet_odds_is_open
from live_betting.strict_eligibility import query_strict_live_eligibility

from .alerts import active_alerts


_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_OPEN_MATCH_STATUSES = {"1", "2", "open", "active", "running"}
_ENDED_MATCH_STATUSES = {"3", "5", "ended", "finished", "settled", "closed"}
_UPCOMING_MATCH_STATUSES = {"1", "upcoming", "scheduled", "not_started"}
_HISTORY_SCHEDULE_GRACE = timedelta(hours=12)
_HISTORY_ACTIVITY_GRACE = timedelta(minutes=15)
_EXPECTED_HEALTH_COMPONENTS = {
    "raybet_worker": 45.0,
    "shadow_worker": 45.0,
}
_OPTIONAL_UNCONFIGURED_COMPONENTS = {"mail", "mail_worker"}


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
    alerts = active_alerts(connection)
    return {
        "generated_at": checked_at.isoformat(),
        "cursor": monitor_cursor(connection),
        "mapping_revision": mapping_revision(connection),
        "health": health,
        "matches": matches,
        "alerts": alerts,
        "summary": {
            "total": len(matches),
            "upcoming": sum(item["lifecycle"] == "upcoming" for item in matches),
            "live": sum(item["lifecycle"] == "live" for item in matches),
            "degraded": sum(item["lifecycle"] == "degraded" for item in matches),
            "ended": sum(item["lifecycle"] == "ended" for item in matches),
            "unhealthy_components": sum(
                item["status"] in {"degraded", "unhealthy", "stopped"}
                for item in health
                if not (
                    item["component"] in _OPTIONAL_UNCONFIGURED_COMPONENTS
                    and item["last_error"] == "configuration_missing"
                )
            ),
            "active_alerts": len(alerts),
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
                      odds.period, odds.side, odds.odds_id, odds.odds_group_id,
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
    grouped: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        side = str(row["side"] or "")
        if side not in {"team_one", "team_two"}:
            continue
        key = (
            str(row["received_at"]),
            str(row["period"]),
            str(row["odds_group_id"] or ""),
        )
        grouped[key][side] = row

    points: list[dict[str, Any]] = []
    for (observed_at, period, _odds_group_id), quotes in sorted(grouped.items()):
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
    if _has_transport_observations(connection, raybet_match_id):
        rows = _rows(
            connection,
            """WITH latest AS (
                   SELECT observation_key
                     FROM odds_transport_observations
                    WHERE raybet_match_id=?
                      AND timing_status='on_time'
                      AND processing_status='processed'
                    ORDER BY observed_at DESC, observation_key DESC LIMIT 1
               )
               SELECT outcome.odds_id, outcome.odds_group_id,
                      outcome.received_at, outcome.price, outcome.status,
                      outcome.market_type, outcome.period, outcome.side,
                      outcome.line, outcome.outcome_key, outcome.supported
                 FROM latest
                 JOIN odds_response_outcomes AS outcome
                   ON outcome.observation_key=latest.observation_key
                WHERE outcome.raybet_match_id=?
                ORDER BY CASE WHEN outcome.market_type='winner' THEN 0 ELSE 1 END,
                         outcome.period, outcome.market_type,
                         outcome.odds_group_id, outcome.outcome_key""",
            (raybet_match_id, raybet_match_id),
        )
    else:
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
        "transport": _latest_transport_identity(connection),
        "vision": _max_value(connection, "vision_observations", "captured_at"),
        "decision": _max_value(connection, "strategy_decisions", "decided_at"),
        "health": _max_value(connection, "service_health", "updated_at"),
        "mapping": mapping_revision(connection),
        "control": _max_value(connection, "monitor_control_audit", "audit_id"),
        "alerts": _max_value(connection, "monitor_alert_audit", "audit_id"),
    }
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def mapping_revision(connection: sqlite3.Connection) -> str:
    values = {
        "mapping": _max_value(connection, "strict_live_map_mappings", "mapping_id"),
        "approval": _max_value(
            connection, "strict_live_automatic_evidence_approvals", "approval_id"
        ),
        "invalidation": _max_value(
            connection, "strict_live_map_mapping_invalidations", "invalidation_id"
        ),
    }
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _monitor_match(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
    health: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    match_id = str(row["raybet_match_id"])
    if _has_transport_observations(connection, match_id):
        latest_odds = _latest_row(
            connection,
            """SELECT observed_at FROM odds_transport_observations
                WHERE raybet_match_id=?
                  AND timing_status='on_time'
                  AND processing_status='processed'
                ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (match_id,),
        )
    else:
        latest_odds = _latest_row(
            connection,
            """SELECT received_at AS observed_at FROM odds_snapshots
                WHERE raybet_match_id=? ORDER BY received_at DESC, id DESC LIMIT 1""",
            (match_id,),
        )
    latest_vision = _latest_row(
        connection,
        """SELECT observation.captured_at AS observed_at,
                  observation.map_number, observation.game_clock_seconds,
                  observation.screen_state, observation.confirmed,
                  observation.clock_confidence, observation.draft_confidence
             FROM vision_observations AS observation
             JOIN vision_draft_anchors AS anchor
               ON anchor.raybet_match_id=observation.raybet_match_id
              AND anchor.map_number=observation.map_number
              AND (
                    anchor.status='anchored'
                    OR (
                        anchor.status='conflict'
                        AND anchor.conflict_at IS NOT NULL
                        AND julianday(anchor.conflict_at) IS NOT NULL
                        AND julianday(anchor.conflict_at)>julianday(?)
                    )
              )
            WHERE observation.raybet_match_id=?
              AND julianday(observation.captured_at)<=julianday(?)
            ORDER BY observation.captured_at DESC LIMIT 1""",
        (now.isoformat(), match_id, now.isoformat()),
    )
    latest_decision = _latest_row(
        connection,
        """SELECT decided_at AS observed_at, map_number, model_probability,
                  market_probability, edge, eligible, reason, strategy_version
             FROM strategy_decisions AS decision
            WHERE raybet_match_id=?
              AND NOT EXISTS (
                  SELECT 1 FROM vision_derived_invalidations AS invalidation
                   WHERE invalidation.dependent_type='strategy_decision'
                     AND invalidation.dependent_key=decision.decision_key
              )
            ORDER BY decided_at DESC LIMIT 1""",
        (match_id,),
    )
    mapping_readiness = _mapping_readiness(connection, match_id, now)

    odds_readiness = _freshness(latest_odds, now, warning=15.0, stale=60.0)
    vision_readiness = _freshness(latest_vision, now, warning=20.0, stale=120.0)
    if latest_vision and not bool(latest_vision["confirmed"]):
        vision_readiness["status"] = "unconfirmed"
    decision_readiness = _freshness(latest_decision, now, warning=30.0, stale=120.0)
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
        row["updated_at"],
        latest_vision,
        vision_readiness,
        checked_at=now,
    )
    current_winner = _current_winner(
        connection, match_id, provider_status=str(row["status"] or "")
    )
    history_eligible = _history_eligible(
        lifecycle,
        row["scheduled_at"],
        row["updated_at"],
        checked_at=now,
    )
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
        "history_eligible": history_eligible,
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


def _mapping_readiness(
    connection: sqlite3.Connection,
    raybet_match_id: str,
    now: datetime,
) -> dict[str, Any]:
    try:
        rows = connection.execute(
            """SELECT mapping.map_number
                 FROM strict_live_map_mappings AS mapping
                WHERE mapping.raybet_match_id=?
                  AND NOT EXISTS (
                      SELECT 1 FROM strict_live_map_mapping_invalidations AS invalidation
                       WHERE invalidation.mapping_id=mapping.mapping_id
                  )
                ORDER BY mapping.map_number""",
            (raybet_match_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        return {"status": "missing", "count": 0, "total_count": 0, "reasons": []}
    results = [
        query_strict_live_eligibility(
            connection,
            raybet_match_id=raybet_match_id,
            map_number=int(row[0]),
            transport_observed_at=now,
        )
        for row in rows
    ]
    valid_count = sum(result.eligible for result in results)
    reasons = sorted({result.reason for result in results if not result.eligible})
    return {
        "status": "ready" if valid_count == len(rows) else "invalid",
        "count": valid_count,
        "total_count": len(rows),
        "reasons": reasons,
    }


def _current_winner(
    connection: sqlite3.Connection,
    raybet_match_id: str,
    *,
    provider_status: str,
) -> dict[str, Any] | None:
    try:
        has_transport = connection.execute(
            """SELECT 1 FROM odds_transport_observations
                WHERE raybet_match_id=? LIMIT 1""",
            (raybet_match_id,),
        ).fetchone() is not None
        quotes = _rows(
            connection,
            """SELECT outcome.observation_key, outcome.odds_group_id,
                      outcome.side, outcome.price, outcome.status,
                      outcome.period, transport.observed_at AS received_at,
                      outcome.odds_id AS id
                 FROM odds_transport_observations AS transport
                 JOIN odds_response_outcomes AS outcome
                   ON outcome.observation_key=transport.observation_key
                WHERE outcome.raybet_match_id=?
                  AND outcome.market_type='winner'
                  AND outcome.supported=1
                  AND transport.timing_status='on_time'
                  AND transport.processing_status='processed'
                ORDER BY transport.observed_at DESC,
                         transport.observation_key DESC, outcome.odds_id DESC""",
            (raybet_match_id,),
        )
    except sqlite3.OperationalError:
        has_transport = False
        quotes = []
    exact_responses = has_transport
    if not exact_responses:
        quotes = _rows(
            connection,
            """SELECT NULL AS observation_key, odds_group_id,
                      side, price, status, period, received_at, id
                 FROM odds_snapshots
                WHERE raybet_match_id=? AND market_type='winner' AND supported=1
                ORDER BY received_at DESC, id DESC""",
            (raybet_match_id,),
        )
    if not quotes:
        return None
    grouped: dict[tuple[str, str, str], dict[str, sqlite3.Row]] = defaultdict(dict)
    for quote in quotes:
        side = str(quote["side"] or "")
        if side in {"team_one", "team_two"}:
            response_key = (
                str(quote["observation_key"])
                if exact_responses
                else str(quote["received_at"])
            )
            key = (
                str(quote["period"] or ""),
                response_key,
                str(quote["odds_group_id"] or ""),
            )
            grouped[key].setdefault(side, quote)
    by_period: dict[str, tuple[str, str, dict[str, sqlite3.Row]]] = {}
    for (period, response_key, _group_id), sides in grouped.items():
        if set(sides) != {"team_one", "team_two"}:
            continue
        observed_at = str(next(iter(sides.values()))["received_at"])
        current = by_period.get(period)
        candidate_key = (observed_at, response_key)
        current_key = (current[0], current[1]) if current is not None else None
        if current_key is None or candidate_key > current_key:
            by_period[period] = (observed_at, response_key, sides)
    paired_periods = [
        period
        for period in sorted(by_period, key=_period_sort_key)
    ]
    if not paired_periods:
        return {"observed_at": str(quotes[0]["received_at"]), "complete": False}

    normalized_status = provider_status.casefold()
    if normalized_status in _ENDED_MATCH_STATUSES:
        eligible_periods = [
            period
            for period in paired_periods
            if all(str(quote["status"]) == "5" for quote in by_period[period][2].values())
        ]
    else:
        eligible_periods = [
            period
            for period in paired_periods
            if all(
                raybet_odds_is_open(quote["status"])
                for quote in by_period[period][2].values()
            )
        ]
    if not eligible_periods:
        return {"observed_at": str(quotes[0]["received_at"]), "complete": False}
    if normalized_status in _UPCOMING_MATCH_STATUSES and "map_1" in eligible_periods:
        period = "map_1"
    elif normalized_status in _ENDED_MATCH_STATUSES:
        period = eligible_periods[-1]
    else:
        period = eligible_periods[0]

    observed_at, _response_key, by_side = by_period[period]
    if set(by_side) != {"team_one", "team_two"}:
        return {"observed_at": observed_at, "complete": False}
    prices = {side: float(by_side[side]["price"]) for side in by_side}
    if any(price <= 1.0 for price in prices.values()):
        return {"observed_at": observed_at, "complete": False, "prices": prices}
    inverse = {side: 1.0 / price for side, price in prices.items()}
    total = sum(inverse.values())
    return {
        "observed_at": observed_at,
        "period": period,
        "complete": True,
        "prices": prices,
        "probabilities": {
            side: round(value / total, 8) for side, value in inverse.items()
        },
    }


def _period_sort_key(period: str) -> tuple[int, int | str]:
    prefix, separator, suffix = period.partition("_")
    if prefix == "map" and separator and suffix.isdigit():
        return (0, int(suffix))
    return (1, period)


def _strategy_decisions(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in _rows(
            connection,
            """SELECT decision.decision_key, decision.map_number,
                      decision.decided_at, decision.underdog_side,
                      market_probability, model_probability, edge, data_quality,
                      eligible, decision.reason, contributions_json, input_ref,
                      strategy_version,
                      CASE WHEN invalidation.dependent_key IS NULL THEN 0 ELSE 1 END
                          AS vision_invalidated
                 FROM strategy_decisions AS decision
                 LEFT JOIN vision_derived_invalidations AS invalidation
                   ON invalidation.dependent_type='strategy_decision'
                  AND invalidation.dependent_key=decision.decision_key
                WHERE decision.raybet_match_id=?
                ORDER BY decision.decided_at, decision.decision_key""",
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
    updated_at: object,
    latest_vision: sqlite3.Row | None,
    vision_readiness: dict[str, Any],
    *,
    checked_at: datetime,
) -> str:
    normalized = provider_status.casefold()
    if normalized in _ENDED_MATCH_STATUSES:
        return "ended"
    if raybet_match_is_live(provider_status, updated_at, now=checked_at):
        return "live"
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


def _history_eligible(
    lifecycle: str,
    scheduled_at: object,
    updated_at: object,
    *,
    checked_at: datetime,
) -> bool:
    """Expose old odds for replay without claiming provider settlement."""
    if lifecycle == "ended":
        return True
    if lifecycle != "degraded":
        return False
    scheduled = _parse_schedule(scheduled_at)
    activity = _parse_time(updated_at)
    if scheduled is None or activity is None:
        return False
    return (
        scheduled <= checked_at - _HISTORY_SCHEDULE_GRACE
        and activity <= checked_at - _HISTORY_ACTIVITY_GRACE
    )


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


def _has_transport_observations(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> bool:
    return bool(
        _scalar(
            connection,
            """SELECT EXISTS(
                   SELECT 1 FROM odds_transport_observations
                    WHERE raybet_match_id=?
               )""",
            (raybet_match_id,),
            default=0,
        )
    )


def _latest_transport_identity(connection: sqlite3.Connection) -> list[str] | None:
    row = _latest_row(
        connection,
        """SELECT observed_at, observation_key
             FROM odds_transport_observations
            WHERE timing_status='on_time' AND processing_status='processed'
            ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
        (),
    )
    if row is None:
        return None
    return [str(row["observed_at"]), str(row["observation_key"])]


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
    "mapping_revision",
    "monitor_cursor",
    "monitor_match_detail",
    "monitor_matches",
    "winner_timeline",
]
