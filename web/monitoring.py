"""Read-only projections for the local live-monitoring console."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from live_betting.raybet_state import raybet_match_is_live, raybet_odds_is_open
from live_betting.sanitize import stored_public_stream_url
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
_RAYBET_PAGE_ORIGINS = frozenset(
    {"https://ray086.com", "https://www.ray086.com"}
)
_RAYBET_PAGE_PREFIXES = ("/sports/esports", "/esports", "/dota2")
_RAYBET_PAGE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")


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
    all_counts = _lifecycle_counts(matches)
    live_view = [item for item in matches if not _is_historical_match(item)]
    history_view = [item for item in matches if _is_historical_match(item)]
    return {
        "generated_at": checked_at.isoformat(),
        "cursor": monitor_cursor(connection),
        "mapping_revision": mapping_revision(connection),
        "health": health,
        "matches": matches,
        "alerts": alerts,
        "summary": {
            **all_counts,
            # Keep the original all-match counters for older clients.  The
            # view-specific counters prevent a history-eligible degraded match
            # from inflating the live dashboard's degraded badge.
            "live_view": _lifecycle_counts(live_view),
            "history_view": _lifecycle_counts(history_view),
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
                  scheduled_at, best_of, status, live_url, raw_json, updated_at
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
            _match_time_sort_value(item, descending=_is_historical_match(item)),
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
                  scheduled_at, best_of, status, live_url, raw_json, updated_at
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
    max_points: int | None = 1200,
    period: str | None = None,
) -> list[dict[str, Any]]:
    authority_relations = {
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
                 WHERE type IN ('table', 'view')
                   AND name IN (
                       'odds_transport_observations',
                       'odds_response_outcomes_effective'
                   )"""
        ).fetchall()
    }
    if authority_relations:
        if authority_relations != {
            "odds_transport_observations",
            "odds_response_outcomes_effective",
        }:
            return []
        try:
            rows = _rows(
                connection,
                """SELECT transport.observation_key AS response_key,
                          transport.observed_at AS received_at,
                          outcome.price, outcome.status, outcome.period,
                          outcome.side, outcome.odds_id, outcome.odds_group_id,
                          alignment.map_number, alignment.game_clock_seconds,
                          alignment.raybet_match_id AS alignment_raybet_match_id,
                          alignment.observation_captured_at,
                          alignment.method AS alignment_method,
                          alignment.lag_seconds,
                          alignment.usable AS alignment_usable
                     FROM odds_transport_observations AS transport
                     JOIN odds_response_outcomes_effective AS outcome
                       ON outcome.observation_key=transport.observation_key
                      AND outcome.raybet_match_id=transport.raybet_match_id
                     LEFT JOIN odds_snapshots AS snapshot
                       ON snapshot.raybet_match_id=transport.raybet_match_id
                      AND snapshot.odds_id=outcome.odds_id
                      AND snapshot.received_at=transport.observed_at
                      AND snapshot.odds_group_id IS outcome.odds_group_id
                      AND snapshot.price=outcome.price
                      AND snapshot.status IS outcome.status
                      AND snapshot.market_type=outcome.market_type
                      AND snapshot.period=outcome.period
                      AND snapshot.side IS outcome.side
                      AND snapshot.line IS outcome.line
                      AND snapshot.outcome_key=outcome.outcome_key
                      AND snapshot.supported=outcome.supported
                      AND snapshot.last_update IS outcome.last_update
                     LEFT JOIN odds_alignments AS alignment
                       ON alignment.odds_snapshot_id=snapshot.id
                    WHERE transport.raybet_match_id=?
                      AND transport.timing_status='on_time'
                      AND transport.processing_status='processed'
                      AND outcome.market_type='winner'
                      AND outcome.supported=1
                      AND (? IS NULL OR outcome.period=?)
                    ORDER BY transport.observed_at, transport.observation_key,
                             outcome.period, outcome.odds_group_id,
                             outcome.odds_id""",
                (raybet_match_id, period, period),
            )
        except sqlite3.OperationalError:
            # A partial or malformed response-authority schema must not revive
            # carried-forward legacy snapshots as product data.
            return []
    else:
        has_alignment_relation = connection.execute(
            """SELECT 1 FROM sqlite_master
                 WHERE type IN ('table', 'view') AND name='odds_alignments'"""
        ).fetchone() is not None
        alignment_columns = (
            """alignment.map_number, alignment.game_clock_seconds,
               alignment.raybet_match_id AS alignment_raybet_match_id,
               alignment.observation_captured_at,
               alignment.method AS alignment_method,
               alignment.lag_seconds,
               alignment.usable AS alignment_usable"""
            if has_alignment_relation
            else """NULL AS map_number, NULL AS game_clock_seconds,
               NULL AS alignment_raybet_match_id,
               NULL AS observation_captured_at,
               NULL AS alignment_method, NULL AS lag_seconds,
               NULL AS alignment_usable"""
        )
        alignment_join = (
            """LEFT JOIN odds_alignments AS alignment
                   ON alignment.odds_snapshot_id=odds.id"""
            if has_alignment_relation
            else ""
        )
        rows = _rows(
            connection,
            f"""SELECT '' AS response_key, odds.received_at, odds.price,
                      odds.status, odds.period, odds.side, odds.odds_id,
                      odds.odds_group_id, {alignment_columns}
                 FROM odds_snapshots AS odds
                 {alignment_join}
                WHERE odds.raybet_match_id=?
                  AND odds.market_type='winner' AND odds.supported=1
                  AND (? IS NULL OR odds.period=?)
                ORDER BY odds.received_at, odds.period,
                         odds.odds_group_id, odds.id""",
            (raybet_match_id, period, period),
        )

    grouped: dict[tuple[str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        side = str(row["side"] or "")
        if side not in {"team_one", "team_two"}:
            continue
        odds_group_id = str(row["odds_group_id"] or "")
        if not odds_group_id.strip():
            continue
        key = (
            str(row["received_at"]),
            str(row["response_key"]),
            str(row["period"]),
            odds_group_id,
        )
        grouped[key].append(row)

    points: list[dict[str, Any]] = []
    for (
        observed_at,
        _response_key,
        point_period,
        _odds_group_id,
    ), response_quotes in sorted(grouped.items()):
        if len(response_quotes) != 2:
            continue
        quotes = {str(quote["side"]): quote for quote in response_quotes}
        if set(quotes) != {"team_one", "team_two"}:
            continue
        prices = {side: float(quotes[side]["price"]) for side in quotes}
        if any(not math.isfinite(price) or price <= 1.0 for price in prices.values()):
            continue
        inverse = {side: 1.0 / price for side, price in prices.items()}
        total = sum(inverse.values())
        aligned_quotes = tuple(quotes[side] for side in ("team_one", "team_two"))
        usable_aligned_quotes = tuple(
            quote
            for quote in aligned_quotes
            if (
                quote["alignment_usable"] == 1
                and quote["alignment_raybet_match_id"] == raybet_match_id
                and type(quote["map_number"]) is int
                and quote["map_number"] > 0
                and type(quote["game_clock_seconds"]) is int
                and quote["game_clock_seconds"] >= 0
            )
        )
        explicit_alignment_count = sum(
            quote["alignment_usable"] is not None for quote in aligned_quotes
        )
        alignment_identities = {
            (
                quote["alignment_raybet_match_id"],
                quote["map_number"],
                quote["game_clock_seconds"],
                quote["observation_captured_at"],
                quote["alignment_method"],
                quote["lag_seconds"],
            )
            for quote in usable_aligned_quotes
        }
        aligned = (
            usable_aligned_quotes[0]
            if len(alignment_identities) == 1
            and all(
                (
                    quote["alignment_raybet_match_id"],
                    quote["map_number"],
                    quote["game_clock_seconds"],
                    quote["observation_captured_at"],
                    quote["alignment_method"],
                    quote["lag_seconds"],
                )
                in alignment_identities
                for quote in usable_aligned_quotes
            )
            and len(usable_aligned_quotes) == explicit_alignment_count
            and (
                bool(authority_relations)
                or len(usable_aligned_quotes) == len(aligned_quotes)
            )
            else None
        )
        points.append(
            {
                "observed_at": observed_at,
                "period": point_period,
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
    return points if max_points is None else _downsample(points, max_points)


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
                 JOIN odds_response_outcomes_effective AS outcome
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
        "browser_page": _browser_page_revision(connection),
        "vision": _vision_revision(connection),
        "vision_invalidation": _append_only_revision(
            connection,
            "vision_observation_invalidations",
            time_column="invalidated_at",
        ),
        "vision_draft_conflict": _append_only_revision(
            connection,
            "vision_draft_conflicts",
            id_column="conflict_id",
            time_column="recorded_at",
        ),
        "vision_derived_invalidation": _append_only_revision(
            connection,
            "vision_derived_invalidations",
            time_column="recorded_at",
        ),
        "decision": _max_value(connection, "strategy_decisions", "decided_at"),
        "health": _max_value(connection, "service_health", "updated_at"),
        "mapping": mapping_revision(connection),
        "control": _max_value(connection, "monitor_control_audit", "audit_id"),
        "alerts": _max_value(connection, "monitor_alert_audit", "audit_id"),
    }
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def mapping_revision(connection: sqlite3.Connection) -> str:
    try:
        impact_revision = _max_value(
            connection, "strict_live_mapping_impacts", "impact_id"
        )
    except sqlite3.OperationalError:
        impact_revision = "unavailable"
    values = {
        "mapping": _max_value(connection, "strict_live_map_mappings", "mapping_id"),
        "approval": _max_value(
            connection, "strict_live_automatic_evidence_approvals", "approval_id"
        ),
        "invalidation": _max_value(
            connection, "strict_live_map_mapping_invalidations", "invalidation_id"
        ),
        "impact": impact_revision,
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
                         AND NOT EXISTS (
                             SELECT 1 FROM vision_draft_conflicts AS conflict
                              WHERE conflict.raybet_match_id=anchor.raybet_match_id
                                AND conflict.map_number=anchor.map_number
                                AND (
                                      julianday(conflict.captured_at) IS NULL
                                      OR julianday(conflict.captured_at)<=julianday(?)
                                )
                         )
                     )
              )
            WHERE observation.raybet_match_id=?
              AND julianday(observation.captured_at)<=julianday(?)
              AND NOT EXISTS (
                  SELECT 1
                    FROM vision_observation_invalidations AS invalidation
                    JOIN vision_observations AS invalidated
                      ON invalidated.raybet_match_id=invalidation.raybet_match_id
                     AND invalidated.captured_at=invalidation.captured_at
                     AND invalidated.source_frame_ref=invalidation.source_frame_ref
                   WHERE invalidated.raybet_match_id=observation.raybet_match_id
                     AND invalidated.map_number=observation.map_number
                     AND (
                           julianday(invalidation.captured_at) IS NULL
                           OR julianday(observation.captured_at) IS NULL
                           OR julianday(invalidation.captured_at)>=
                              julianday(observation.captured_at)
                     )
              )
            ORDER BY observation.captured_at DESC LIMIT 1""",
         (now.isoformat(), now.isoformat(), match_id, now.isoformat()),
    )
    latest_decision = _latest_strategy_decision(connection, match_id)
    mapping_readiness = _mapping_readiness(connection, match_id, now)
    latest_odds_activity = _latest_odds_activity(connection, match_id)

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
        odds_activity_at=latest_odds_activity,
        checked_at=now,
    )
    watch_link = _watch_link(connection, row)
    return {
        "raybet_match_id": match_id,
        "tournament": row["tournament"],
        "team_one": row["team_one"],
        "team_two": row["team_two"],
        "scheduled_at": row["scheduled_at"],
        "best_of": row["best_of"],
        "provider_status": str(row["status"] or ""),
        # Legacy clients receive no ambiguous stream URL.  New clients use the
        # provenance-bearing link contract below.
        "live_url": None,
        "watch_link": watch_link,
        "updated_at": row["updated_at"],
        "latest_odds_activity_at": (
            latest_odds_activity.isoformat() if latest_odds_activity else None
        ),
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


def _watch_link(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, str | None]:
    match_page = _captured_raybet_page_url(
        connection, str(row["raybet_match_id"])
    )
    if match_page is not None:
        return {
            "kind": "match_page",
            "availability": "available",
            "url": match_page,
            "reason": "captured_raybet_match_page",
        }
    public_stream = stored_public_stream_url(row["live_url"], row["raw_json"])
    if public_stream is not None:
        return {
            "kind": "public_stream",
            "availability": "available",
            "url": public_stream,
            "reason": "verified_unsigned_stream",
        }
    return {
        "kind": "none",
        "availability": "unavailable",
        "url": None,
        "reason": "no_safe_entry",
    }


def _captured_raybet_page_url(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> str | None:
    try:
        rows = connection.execute(
            """SELECT page_origin, page_path
                 FROM browser_events
                WHERE raybet_match_id=?
                  AND game_id=151
                  AND recognized=1
                  AND event_type IN ('odds', 'market_update', 'video')
                  AND processing_status IN ('processed', 'audit_only')
                ORDER BY captured_at DESC, event_id DESC
                LIMIT 50""",
            (raybet_match_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for event in rows:
        url = _safe_raybet_page_url(event["page_origin"], event["page_path"])
        if url is not None:
            return url
    return None


def _safe_raybet_page_url(origin: object, path: object) -> str | None:
    if origin not in _RAYBET_PAGE_ORIGINS or not isinstance(path, str):
        return None
    if not _RAYBET_PAGE_PATH_RE.fullmatch(path) or path.startswith("//"):
        return None
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return None
    if not any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in _RAYBET_PAGE_PREFIXES
    ):
        return None
    return f"{origin}{path}"


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
    except sqlite3.OperationalError:
        try:
            transport_relation_exists = connection.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type IN ('table', 'view')
                      AND name='odds_transport_observations'"""
            ).fetchone() is not None
        except sqlite3.OperationalError:
            return None
        if transport_relation_exists:
            return None
        has_transport = False

    if has_transport:
        try:
            quotes = _rows(
                connection,
                """WITH recent_transport AS (
                   SELECT observation_key, observed_at
                     FROM odds_transport_observations
                    WHERE raybet_match_id=?
                      AND timing_status='on_time'
                      AND processing_status='processed'
                    ORDER BY observed_at DESC, observation_key DESC
                    LIMIT 16
               )
               SELECT outcome.observation_key, outcome.odds_group_id,
                      outcome.side, outcome.price, outcome.status,
                      outcome.period, recent_transport.observed_at AS received_at,
                      outcome.odds_id AS id
                 FROM recent_transport
                 JOIN odds_response_outcomes_effective AS outcome
                   ON outcome.observation_key=recent_transport.observation_key
                WHERE outcome.raybet_match_id=?
                  AND outcome.market_type='winner'
                  AND outcome.supported=1
                ORDER BY recent_transport.observed_at DESC,
                         recent_transport.observation_key DESC,
                         outcome.odds_id DESC""",
                (raybet_match_id, raybet_match_id),
            )
        except sqlite3.OperationalError:
            # Once exact transport membership exists, a malformed response
            # schema cannot be replaced by carried-forward legacy snapshots.
            return None
    else:
        quotes = _rows(
            connection,
            """SELECT NULL AS observation_key, odds_group_id,
                      side, price, status, period, received_at, id
                 FROM odds_snapshots
                WHERE raybet_match_id=? AND market_type='winner' AND supported=1
                ORDER BY received_at DESC, id DESC""",
            (raybet_match_id,),
        )
    exact_responses = has_transport
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
    try:
        rows = connection.execute(
            """SELECT decision.decision_key, decision.map_number,
                      decision.decided_at, decision.underdog_side,
                      market_probability, model_probability, edge, data_quality,
                      eligible, decision.reason, contributions_json, input_ref,
                      strategy_version
                 FROM strategy_decisions AS decision
                WHERE decision.raybet_match_id=?
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_derived_invalidations AS invalidation
                       WHERE invalidation.dependent_type='strategy_decision'
                         AND invalidation.dependent_key=decision.decision_key
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM strict_live_mapping_impacts AS impact
                       WHERE impact.dependent_type='strategy_decision'
                         AND impact.dependent_key=decision.decision_key
                  )
                  AND NOT EXISTS (
                      SELECT 1
                        FROM vision_draft_anchors AS anchor
                       WHERE anchor.raybet_match_id=decision.raybet_match_id
                         AND anchor.map_number=decision.map_number
                         AND anchor.status='conflict'
                         AND (
                               anchor.conflict_at IS NULL
                               OR julianday(anchor.conflict_at) IS NULL
                               OR julianday(decision.decided_at) IS NULL
                               OR julianday(anchor.conflict_at)<=
                                  julianday(decision.decided_at)
                               OR EXISTS (
                                    SELECT 1
                                      FROM vision_draft_conflicts AS conflict
                                     WHERE conflict.raybet_match_id=
                                           anchor.raybet_match_id
                                       AND conflict.map_number=anchor.map_number
                                       AND (
                                             julianday(conflict.captured_at) IS NULL
                                             OR julianday(conflict.captured_at)<=
                                                julianday(decision.decided_at)
                                       )
                               )
                         )
                  )
                ORDER BY decision.decided_at, decision.decision_key""",
            (raybet_match_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def _latest_strategy_decision(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> sqlite3.Row | None:
    try:
        return connection.execute(
            """SELECT decided_at AS observed_at, map_number, model_probability,
                      market_probability, edge, eligible, reason, strategy_version
                 FROM strategy_decisions AS decision
                WHERE decision.raybet_match_id=?
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_derived_invalidations AS invalidation
                       WHERE invalidation.dependent_type='strategy_decision'
                         AND invalidation.dependent_key=decision.decision_key
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM strict_live_mapping_impacts AS impact
                       WHERE impact.dependent_type='strategy_decision'
                         AND impact.dependent_key=decision.decision_key
                  )
                  AND NOT EXISTS (
                      SELECT 1
                        FROM vision_draft_anchors AS anchor
                       WHERE anchor.raybet_match_id=decision.raybet_match_id
                         AND anchor.map_number=decision.map_number
                         AND anchor.status='conflict'
                         AND (
                               anchor.conflict_at IS NULL
                               OR julianday(anchor.conflict_at) IS NULL
                               OR julianday(decision.decided_at) IS NULL
                               OR julianday(anchor.conflict_at)<=
                                  julianday(decision.decided_at)
                               OR EXISTS (
                                    SELECT 1
                                      FROM vision_draft_conflicts AS conflict
                                     WHERE conflict.raybet_match_id=
                                           anchor.raybet_match_id
                                       AND conflict.map_number=anchor.map_number
                                       AND (
                                             julianday(conflict.captured_at) IS NULL
                                             OR julianday(conflict.captured_at)<=
                                                julianday(decision.decided_at)
                                       )
                               )
                         )
                  )
                ORDER BY decision.decided_at DESC LIMIT 1""",
            (raybet_match_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


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
    odds_activity_at: datetime | None = None,
    checked_at: datetime,
) -> bool:
    """Expose old odds for replay without claiming provider settlement."""
    if lifecycle == "ended":
        return True
    if lifecycle != "degraded":
        return False
    scheduled = _parse_schedule(scheduled_at)
    metadata_activity = _parse_time(updated_at)
    if updated_at is not None and metadata_activity is None:
        # An unparseable provider timestamp is not evidence of staleness.
        return False
    activity_candidates = [
        value
        for value in (metadata_activity, odds_activity_at)
        if value is not None
    ]
    if scheduled is None or not activity_candidates:
        return False
    activity = max(activity_candidates)
    return (
        scheduled <= checked_at - _HISTORY_SCHEDULE_GRACE
        and activity <= checked_at - _HISTORY_ACTIVITY_GRACE
    )


def _latest_odds_activity(
    connection: sqlite3.Connection,
    raybet_match_id: str,
) -> datetime | None:
    """Return the newest immutable odds activity for archive gating.

    ``updated_at`` belongs to the match-list metadata and can remain stale
    while the odds page continues emitting responses.  Both transport rows
    and legacy normalized snapshots are considered; processing failures still
    count as activity so a broken feed cannot be silently archived.
    """

    candidates: list[datetime] = []
    transport = _latest_row(
        connection,
        """SELECT observed_at
             FROM odds_transport_observations
            WHERE raybet_match_id=?
            ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
        (raybet_match_id,),
    )
    if transport is not None:
        parsed = _parse_time(transport["observed_at"])
        if parsed is not None:
            candidates.append(parsed)
    snapshot = _latest_row(
        connection,
        """SELECT received_at
             FROM odds_snapshots
            WHERE raybet_match_id=?
            ORDER BY received_at DESC, id DESC LIMIT 1""",
        (raybet_match_id,),
    )
    if snapshot is not None:
        parsed = _parse_time(snapshot["received_at"])
        if parsed is not None:
            candidates.append(parsed)
    return max(candidates) if candidates else None


def _is_historical_match(match: dict[str, Any]) -> bool:
    return str(match.get("lifecycle")) == "ended" or bool(
        match.get("history_eligible")
    )


def _lifecycle_counts(matches: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(matches),
        "upcoming": sum(item.get("lifecycle") == "upcoming" for item in matches),
        "live": sum(item.get("lifecycle") == "live" for item in matches),
        "degraded": sum(item.get("lifecycle") == "degraded" for item in matches),
        "ended": sum(item.get("lifecycle") == "ended" for item in matches),
    }


def _match_time_sort_value(
    match: dict[str, Any],
    *,
    descending: bool,
) -> float:
    parsed = _parse_schedule(match.get("scheduled_at"))
    if parsed is None:
        parsed = _parse_time(match.get("updated_at"))
    if parsed is None:
        parsed = _parse_time(match.get("latest_odds_activity_at"))
    if parsed is None:
        return float("inf") if descending else float("-inf")
    timestamp = parsed.timestamp()
    return -timestamp if descending else timestamp


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


def _vision_revision(connection: sqlite3.Connection) -> list[Any] | None:
    try:
        row = connection.execute(
            """SELECT COUNT(*) AS row_count,
                      COALESCE(SUM(CASE WHEN confirmed=1 THEN 1 ELSE 0 END), 0)
                          AS confirmed_count,
                      MAX(captured_at) AS latest_captured_at,
                      MAX(CASE WHEN confirmed=1 THEN captured_at END)
                          AS latest_confirmed_at
                 FROM vision_observations"""
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return None
        raise
    if row is None:
        return None
    return [int(row[0]), int(row[1]), row[2], row[3]]


def _browser_page_revision(connection: sqlite3.Connection) -> list[Any] | None:
    try:
        row = connection.execute(
            """SELECT COUNT(*), MAX(event_id), MAX(captured_at)
                 FROM browser_events
                WHERE game_id=151
                  AND recognized=1
                  AND event_type IN ('odds', 'market_update', 'video')
                  AND processing_status IN ('processed', 'audit_only')"""
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return [int(row[0]), row[1], row[2]]


def _append_only_revision(
    connection: sqlite3.Connection,
    table: str,
    *,
    time_column: str,
    id_column: str = "rowid",
) -> list[Any] | None:
    try:
        row = connection.execute(
            f"""SELECT COUNT(*) AS row_count,
                       MAX({id_column}) AS latest_id,
                       MAX({time_column}) AS latest_at
                  FROM {table}"""
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return None
        raise
    if row is None:
        return None
    return [int(row[0]), row[1], row[2]]


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
