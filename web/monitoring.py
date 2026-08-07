"""Read-only projections for the local live-monitoring console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote as url_quote

from live_betting.live_match_state import (
    latest_live_draft_mapping,
    live_draft_context,
    live_game_snapshots,
)
from live_betting.raybet_state import (
    infer_current_map_number,
    raybet_match_is_live,
    raybet_odds_is_open,
)
from live_betting.sanitize import stored_public_stream_url
from live_betting.process_control import MARKET_SOURCE_POLICY
from live_betting.strict_eligibility import (
    RAYBET_MATCH_HEAD_TO_HEAD,
    RAYBET_MATCH_NON_HEAD_TO_HEAD,
    classify_raybet_match_format,
    query_strict_live_eligibility,
)
from live_betting.vision_frame_registry import VISION_FRAME_REF_PREFIX
from sqlalchemy.exc import SQLAlchemyError

from database.session import DatabaseRow, PostgresSession

from .alerts import active_alerts


_LOCAL_TIMEZONE = timezone(timedelta(hours=8))
_OPEN_MATCH_STATUSES = {"1", "2", "open", "active", "running"}
_ENDED_MATCH_STATUSES = {"3", "5", "ended", "finished", "settled", "closed"}
_ENDED_STATUS_SQL = (
    "lower(status) IN ('3', '5', 'closed', 'ended', 'finished', 'settled')"
)
_UPCOMING_MATCH_STATUSES = {"1", "upcoming", "scheduled", "not_started"}
_HISTORY_SCHEDULE_GRACE = timedelta(hours=12)
_PREMATCH_HISTORY_SCHEDULE_GRACE = timedelta(hours=4)
_HISTORY_ACTIVITY_GRACE = timedelta(minutes=15)
_TIMESTAMP_ROUNDING_GRACE = timedelta(milliseconds=1)
_EXPECTED_HEALTH_COMPONENTS = {
    "raybet_worker": 45.0,
    "strict_ingest_worker": 90.0,
}
_PRIMARY_HEALTH_COMPONENTS = {
    "database",
    "raybet",
    "raybet_worker",
    "raybet_priority_odds_worker",
    "raybet_full_odds_worker",
    "strict_ingest",
    "strict_ingest_worker",
}
_RETIRED_HEALTH_COMPONENTS = {
    "historical_rosh",
    "historical_rosh_worker",
    "mail",
    "mail_worker",
    "mail_delivery",
    "shadow_worker",
}
_RAYBET_PAGE_ORIGINS = frozenset(
    {"https://ray086.com", "https://www.ray086.com"}
)
_RAYBET_PAGE_PREFIXES = ("/sports/esports", "/esports", "/dota2")
_RAYBET_PAGE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
_MAX_VISION_TIMELINE_POINTS = 5_000
_MATCH_COLUMN_NAMES = (
    "raybet_match_id",
    "tournament",
    "team_one",
    "team_two",
    "scheduled_at",
    "best_of",
    "status",
    "live_url",
    "raw_json",
    "updated_at",
)
_REALTIME_MATCH_LIMIT = 64
_REALTIME_BUCKET_LIMIT = 64
_REALTIME_REVIEW_LIMIT = 16
_REALTIME_CANDIDATE_LIMIT = 128
_REALTIME_HISTORY_LIMIT = 16
_REALTIME_VISION_SCAN_LIMIT = 256
_HISTORY_DEFAULT_LIMIT = 20
_HISTORY_MAX_LIMIT = 50
_HISTORY_SCAN_LIMIT = 200
_HISTORY_RAW_SCAN_LIMIT = 1000
_HISTORY_CURSOR_MAX_LENGTH = 768
_HISTORY_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$")
_HISTORY_CURSOR_DOMAIN = b"dota2-monitor-history-v1\0"
_HISTORY_CURSOR_SECRET = secrets.token_bytes(32)
_CURSOR_VOLATILE_FIELDS = frozenset({"cursor", "generated_at", "age_seconds"})
_SCHEDULE_UTC_JULIANDAY = "live_text_timestamp_utc(scheduled_at)"
_TIMELINE_KEY_SQL = f"""CAST(EXTRACT(EPOCH FROM COALESCE(
    ({_SCHEDULE_UTC_JULIANDAY}),
    live_text_timestamp_utc(updated_at),
    '1970-01-01T00:00:00+00:00'::timestamptz
)) * 1000 AS BIGINT)"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def derive_health(
    connection: PostgresSession,
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
    names = sorted(
        (set(by_component) | set(_EXPECTED_HEALTH_COMPONENTS))
        - _RETIRED_HEALTH_COMPONENTS
    )
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
        details = _json_object(row["details_json"])
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
        companion_informational = (
            component == "companion" and details.get("configured") is False
        )
        if companion_informational:
            status, freshness = "stopped", "informational"
        elif reported == "stopped" and freshness == "fresh":
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
                "details": details,
            }
        )
    return output


def build_monitor_snapshot(
    connection: PostgresSession,
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
    snapshot = {
        "generated_at": checked_at.isoformat(),
        "market_source_policy": MARKET_SOURCE_POLICY,
        "capabilities": _monitor_capabilities(health),
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
                if item["component"] in _PRIMARY_HEALTH_COMPONENTS
            ),
            "active_alerts": len(alerts),
        },
    }
    snapshot["cursor"] = _snapshot_cursor(snapshot)
    return snapshot


def _monitor_capabilities(
    health: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    statuses = {item["component"]: item["status"] for item in health}
    return {
        "direct_market_collection": {
            "required": True,
            "status": statuses.get("raybet", "stopped"),
        },
        "opendota_event_ingest": {
            "required": True,
            "status": statuses.get("strict_ingest", "stopped"),
        },
    }


def monitor_matches(
    connection: PostgresSession,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    checked_at = _aware_utc(now or utc_now())
    rows = _realtime_match_candidates(connection, checked_at)
    output = [
        _monitor_match(connection, row, checked_at)
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
    return output[:_REALTIME_MATCH_LIMIT]


def monitor_history_page(
    connection: PostgresSession,
    *,
    cursor: str | None = None,
    limit: int = _HISTORY_DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a bounded immutable-odds replay page.

    The first page fixes the eligibility clock.  Later pages recover that
    clock from the cursor, so a long pagination session cannot move matches
    across the archive boundary merely because wall time advanced.
    """

    if type(limit) is not int or not 1 <= limit <= _HISTORY_MAX_LIMIT:
        raise ValueError("history limit is out of range")
    if cursor is None:
        checked_at = _aware_utc(now or utc_now())
        before = None
    else:
        decoded = _decode_history_cursor(cursor)
        checked_at = decoded["checked_at"]
        before = (int(decoded["timeline_key"]), str(decoded["match_id"]))
        _require_history_cursor_anchor(connection, decoded)

    rows, raw_more, raw_anchor = _history_candidate_window(
        connection,
        checked_at=checked_at,
        before=before,
    )
    candidate_more = len(rows) > _HISTORY_SCAN_LIMIT
    candidates = rows[:_HISTORY_SCAN_LIMIT]
    items: list[dict[str, Any]] = []
    returned_rows: list[DatabaseRow] = []
    last_scanned: DatabaseRow | None = None
    found_extra = False
    for row in candidates:
        last_scanned = row
        item = _monitor_match(connection, row, checked_at)
        if item.get("history_eligible") is not True:
            continue
        if len(items) == limit:
            found_extra = True
            break
        items.append(item)
        returned_rows.append(row)

    has_more = found_extra or candidate_more or raw_more
    anchor: DatabaseRow | None = None
    if found_extra:
        anchor = returned_rows[-1]
    elif candidate_more:
        anchor = last_scanned
    elif raw_more:
        anchor = raw_anchor
    next_cursor = (
        _encode_history_cursor(anchor, checked_at)
        if has_more and anchor is not None
        else None
    )
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }


def monitor_match_detail(
    connection: PostgresSession,
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
    if row is None or not is_head_to_head_match_row(row):
        return None
    summary = _monitor_match(connection, row, checked_at)
    timeline = winner_timeline(
        connection,
        raybet_match_id,
        max_points=max_points,
        as_of=checked_at,
    )
    if summary["lifecycle"] == "ended" and timeline:
        summary["winner"] = {
            **(
                summary["winner"]
                if isinstance(summary.get("winner"), dict)
                else {}
            ),
            **timeline[-1],
            "complete": True,
        }
    latest_capture = _latest_capture_row(
        connection,
        raybet_match_id,
        now=checked_at,
    )
    draft_mapping = latest_live_draft_mapping(connection, raybet_match_id)
    draft_context = live_draft_context(
        connection,
        raybet_match_id,
        as_of=checked_at,
    )
    game_snapshots = live_game_snapshots(
        connection,
        raybet_match_id,
        limit=min(max_points, 1200),
    )
    provider_is_prematch = (
        str(summary["provider_status"]).casefold()
        in _UPCOMING_MATCH_STATUSES
    )
    return {
        **summary,
        "prematch_winner": (
            _current_winner(
                connection,
                raybet_match_id,
                provider_status=str(summary["provider_status"]),
                processing_status="audit_only",
                transport_only=True,
            )
            if provider_is_prematch
            else None
        ),
        "winner_timeline": timeline,
        "vision": _vision_timeline(
            connection,
            raybet_match_id,
            now=checked_at,
            max_points=max_points,
        ),
        "latest_capture": (
            _capture_point(latest_capture, raybet_match_id)
            if latest_capture is not None
            else None
        ),
        "draft_mapping": draft_mapping,
        "draft_context": draft_context,
        "game_snapshots": game_snapshots,
        "latest_game_snapshot": game_snapshots[-1] if game_snapshots else None,
        "markets": current_markets(
            connection,
            raybet_match_id,
            as_of=checked_at,
        ),
    }


def winner_timeline(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    max_points: int | None = 1200,
    period: str | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    cutoff = _aware_utc(as_of).isoformat() if as_of is not None else None
    ended_match = connection.execute(
        "SELECT 1 FROM raybet_matches WHERE raybet_match_id=? AND status='3'",
        (raybet_match_id,),
    ).fetchone() is not None
    authority_relations = {
        str(row[0])
        for row in connection.execute(
            """SELECT relation.relname
                 FROM pg_class AS relation
                 JOIN pg_namespace AS namespace
                   ON namespace.oid=relation.relnamespace
                WHERE namespace.nspname=current_schema()
                  AND relation.relkind IN ('r', 'p', 'v')
                  AND relation.relname IN (
                       'odds_transport_observations',
                       'odds_response_outcomes_effective'
                   )"""
        ).fetchall()
    }
    required_authority_relations = {
        "odds_transport_observations",
        "odds_response_outcomes_effective",
    }
    if (
        authority_relations
        and authority_relations != required_authority_relations
    ):
        return []
    if authority_relations:
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
                       AND snapshot.odds_group_id IS NOT DISTINCT FROM
                           outcome.odds_group_id
                      AND snapshot.price=outcome.price
                       AND snapshot.status IS NOT DISTINCT FROM outcome.status
                      AND snapshot.market_type=outcome.market_type
                      AND snapshot.period=outcome.period
                       AND snapshot.side IS NOT DISTINCT FROM outcome.side
                       AND snapshot.line IS NOT DISTINCT FROM outcome.line
                      AND snapshot.outcome_key=outcome.outcome_key
                      AND snapshot.supported=outcome.supported
                       AND snapshot.last_update IS NOT DISTINCT FROM
                           outcome.last_update
                     LEFT JOIN odds_alignments AS alignment
                       ON alignment.odds_snapshot_id=snapshot.id
                     WHERE transport.raybet_match_id=?
                       AND transport.source='direct'
                       AND (CAST(? AS text) IS NULL OR live_text_timestamp_utc(
                               transport.observed_at
                           )<=CAST(? AS timestamptz))
                       AND transport.timing_status='on_time'
                      AND transport.processing_status='processed'
                      AND outcome.market_type='winner'
                      AND outcome.supported=1
                      AND (CAST(? AS text) IS NULL OR outcome.period=?)
                    ORDER BY transport.observed_at, transport.observation_key,
                             outcome.period, outcome.odds_group_id,
                             outcome.odds_id""",
                    (raybet_match_id, cutoff, cutoff, period, period),
            )
        except SQLAlchemyError:
            return []
    else:
        has_alignment_relation = connection.execute(
            """SELECT 1 FROM information_schema.tables
                WHERE table_schema=current_schema()
                  AND table_name='odds_alignments'"""
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
                  AND (CAST(? AS text) IS NULL OR live_text_timestamp_utc(
                          odds.received_at
                      )<=
                      CAST(? AS timestamptz))
                  AND odds.market_type='winner' AND odds.supported=1
                  AND (CAST(? AS text) IS NULL OR odds.period=?)
                ORDER BY odds.received_at, odds.period,
                         odds.odds_group_id, odds.id""",
            (raybet_match_id, cutoff, cutoff, period, period),
        )

    grouped: dict[tuple[str, str, str, str], list[DatabaseRow]] = defaultdict(list)
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
    ended_signatures: dict[str, tuple[object, ...]] = {}
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
        point = {
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
        if ended_match:
            signature = (
                tuple(sorted(prices.items())),
                tuple(sorted(point["status"].items())),
                point["game_clock_seconds"],
                point["map_number"],
                None
                if point["alignment"] is None
                else tuple(sorted(point["alignment"].items())),
            )
            if ended_signatures.get(point_period) == signature:
                continue
            ended_signatures[point_period] = signature
        points.append(point)
    return points if max_points is None else _downsample(points, max_points)


def current_markets(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    cutoff = _aware_utc(as_of).isoformat() if as_of is not None else None
    if _has_transport_observations(connection, raybet_match_id):
        rows = _rows(
            connection,
            """WITH latest AS (
                   SELECT observation_key
                     FROM odds_transport_observations
                     WHERE raybet_match_id=?
                       AND source='direct'
                       AND (CAST(? AS text) IS NULL OR live_text_timestamp_utc(
                               observed_at
                           )<=
                           CAST(? AS timestamptz))
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
                   AND (CAST(? AS text) IS NULL OR live_text_timestamp_utc(
                           outcome.received_at
                       )<=
                       CAST(? AS timestamptz))
                ORDER BY CASE WHEN outcome.market_type='winner' THEN 0 ELSE 1 END,
                         outcome.period, outcome.market_type,
                         outcome.odds_group_id, outcome.outcome_key""",
            (
                raybet_match_id,
                cutoff,
                cutoff,
                raybet_match_id,
                cutoff,
                cutoff,
            ),
        )
    else:
        rows = _rows(
            connection,
            """WITH ranked AS (
                   SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY odds_id ORDER BY received_at DESC, id DESC
                    ) AS rank
                    FROM odds_snapshots WHERE raybet_match_id=?
                      AND (CAST(? AS text) IS NULL OR live_text_timestamp_utc(
                              received_at
                          )<=
                          CAST(? AS timestamptz))
               )
               SELECT odds_id, odds_group_id, received_at, price, status,
                      market_type, period, side, line, outcome_key, supported
                 FROM ranked WHERE rank=1
                ORDER BY CASE WHEN market_type='winner' THEN 0 ELSE 1 END,
                         period, market_type, odds_group_id, outcome_key""",
            (raybet_match_id, cutoff, cutoff),
        )
    return [dict(row) for row in rows]


def monitor_cursor(
    connection: PostgresSession,
    *,
    now: datetime | None = None,
) -> str:
    """Return the cursor for the bounded client-visible monitor snapshot."""

    return str(build_monitor_snapshot(connection, now=now)["cursor"])


def _snapshot_cursor(snapshot: dict[str, Any]) -> str:
    projection = _stable_cursor_projection(snapshot)
    payload = json.dumps(
        projection,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _stable_cursor_projection(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_cursor_projection(item)
            for key, item in value.items()
            if key not in _CURSOR_VOLATILE_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_cursor_projection(item) for item in value]
    return value


def mapping_revision(connection: PostgresSession) -> str:
    try:
        impact_revision = _latest_value(
            connection, "strict_live_mapping_impacts", "impact_id"
        )
    except SQLAlchemyError as error:
        if not _is_schema_missing_error(error):
            raise
        impact_revision = "unavailable"
    values = {
        "mapping": _latest_value(
            connection, "strict_live_map_mappings", "mapping_id"
        ),
        "approval": _latest_value(
            connection, "strict_live_automatic_evidence_approvals", "approval_id"
        ),
        "invalidation": _latest_value(
            connection, "strict_live_map_mapping_invalidations", "invalidation_id"
        ),
        "impact": impact_revision,
    }
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _selected_match_columns(alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(
        f"{prefix}{column} AS {column}" for column in _MATCH_COLUMN_NAMES
    )


def _realtime_match_candidates(
    connection: PostgresSession,
    checked_at: datetime,
) -> list[DatabaseRow]:
    """Return bounded candidates before any per-match projection queries."""

    selected = _selected_match_columns()
    qualified = _selected_match_columns("match_row")
    buckets: list[tuple[int, list[DatabaseRow]]] = []

    provider_live = _rows(
        connection,
        f"""SELECT 0 AS _candidate_rowid, {selected}
               FROM raybet_matches
              WHERE status='2'
                AND updated_at>=?
                AND updated_at<=?
              ORDER BY updated_at DESC, raybet_match_id DESC
              LIMIT ?""",
        (
            (checked_at - timedelta(seconds=90)).isoformat(),
            checked_at.isoformat(),
            _REALTIME_BUCKET_LIMIT,
        ),
    )
    buckets.append((0, provider_live))

    ended_review = _ended_review_match_candidates(connection, checked_at)
    buckets.append((1, ended_review))

    vision_ids = _recent_vision_match_ids(connection, checked_at)
    if vision_ids:
        placeholders = ",".join("?" for _ in vision_ids)
        vision_rows = _rows(
            connection,
            f"""SELECT 0 AS _candidate_rowid, {selected}
                   FROM raybet_matches
                  WHERE raybet_match_id IN ({placeholders})
                  LIMIT ?""",
            (*vision_ids, _REALTIME_BUCKET_LIMIT),
        )
        buckets.append((2, vision_rows))

    activity_rows = _rows(
        connection,
        f"""WITH production_activity AS (
               SELECT raybet_match_id, MAX(observed_at) AS latest_odds_activity_at
                 FROM odds_transport_observations
                WHERE source='direct'
                GROUP BY raybet_match_id
               UNION ALL
               SELECT activity.raybet_match_id, activity.latest_odds_activity_at
                 FROM raybet_match_odds_activity AS activity
                WHERE NOT EXISTS (
                      SELECT 1 FROM odds_transport_observations AS transport
                       WHERE transport.raybet_match_id=activity.raybet_match_id
                )
           )
           SELECT 0 AS _candidate_rowid, {qualified}
             FROM production_activity AS activity
             JOIN raybet_matches AS match_row
               ON match_row.raybet_match_id=activity.raybet_match_id
            WHERE activity.latest_odds_activity_at>=?
              AND activity.latest_odds_activity_at<=?
            ORDER BY activity.latest_odds_activity_at DESC,
                     activity.raybet_match_id DESC
            LIMIT ?""",
        (
            (checked_at - _HISTORY_ACTIVITY_GRACE).isoformat(),
            checked_at.isoformat(),
            _REALTIME_BUCKET_LIMIT,
        ),
    )
    buckets.append((3, activity_rows))

    upcoming = _rows(
        connection,
        f"""SELECT 0 AS _candidate_rowid, {selected}
               FROM raybet_matches
              WHERE {_SCHEDULE_UTC_JULIANDAY}>CAST(? AS timestamptz)
              ORDER BY {_SCHEDULE_UTC_JULIANDAY}, raybet_match_id
              LIMIT ?""",
        (
            (checked_at + timedelta(minutes=15)).isoformat(),
            _REALTIME_BUCKET_LIMIT,
        ),
    )
    buckets.append((4, upcoming))

    recent_metadata = _rows(
        connection,
        f"""SELECT 0 AS _candidate_rowid, {selected}
               FROM raybet_matches
              WHERE updated_at>=?
                AND updated_at<=?
              ORDER BY updated_at DESC, raybet_match_id DESC
              LIMIT ?""",
        (
            (checked_at - timedelta(hours=24)).isoformat(),
            checked_at.isoformat(),
            _REALTIME_BUCKET_LIMIT,
        ),
    )
    buckets.append((5, recent_metadata))

    recent_history = _rows(
        connection,
        f"""SELECT 0 AS _candidate_rowid, {selected}
               FROM raybet_matches
              WHERE {_TIMELINE_KEY_SQL}<=
                    CAST(EXTRACT(EPOCH FROM CAST(? AS timestamptz)) * 1000
                         AS BIGINT)
              ORDER BY {_TIMELINE_KEY_SQL} DESC, raybet_match_id DESC
              LIMIT ?""",
        (
            checked_at.isoformat(),
            _REALTIME_HISTORY_LIMIT,
        ),
    )
    buckets.append((6, recent_history))

    by_match: dict[str, tuple[int, DatabaseRow]] = {}
    for priority, rows in buckets:
        for row in rows:
            if not is_head_to_head_match_row(row):
                continue
            match_id = str(row["raybet_match_id"])
            previous = by_match.get(match_id)
            if previous is None or priority < previous[0]:
                by_match[match_id] = (priority, row)
    ordered = sorted(
        by_match.values(),
        key=lambda value: (
            value[0],
            -int(value[1]["_candidate_rowid"]),
            str(value[1]["raybet_match_id"]),
        ),
    )
    return [row for _, row in ordered[:_REALTIME_CANDIDATE_LIMIT]]


def _ended_review_match_candidates(
    connection: PostgresSession,
    checked_at: datetime,
) -> list[DatabaseRow]:
    """Return a bounded indexed window of contradictory ended rows."""

    selected = _selected_match_columns()
    future = _rows(
        connection,
        f"""SELECT 0 AS _candidate_rowid, {selected}
               FROM raybet_matches
              WHERE {_ENDED_STATUS_SQL}
                AND scheduled_at IS NOT NULL
                AND ({_SCHEDULE_UTC_JULIANDAY})>CAST(? AS timestamptz)
              ORDER BY ({_SCHEDULE_UTC_JULIANDAY}),
                       updated_at DESC, raybet_match_id DESC
              LIMIT ?""",
        (
            (checked_at - _TIMESTAMP_ROUNDING_GRACE).isoformat(),
            _REALTIME_BUCKET_LIMIT,
        ),
    )
    malformed = _rows(
        connection,
        f"""SELECT 0 AS _candidate_rowid, {selected}
               FROM raybet_matches
              WHERE {_ENDED_STATUS_SQL}
                AND scheduled_at IS NOT NULL
                AND ({_SCHEDULE_UTC_JULIANDAY}) IS NULL
                AND updated_at<=?
              ORDER BY updated_at DESC, raybet_match_id DESC
              LIMIT ?""",
        (checked_at.isoformat(), _REALTIME_BUCKET_LIMIT),
    )
    candidates = [
        row
        for row in (*future, *malformed)
        if not _ended_schedule_is_trustworthy(
            row["scheduled_at"],
            checked_at,
        )
    ]
    candidates.sort(
        key=lambda row: (
            (_parse_time(row["updated_at"]) or datetime.min.replace(tzinfo=timezone.utc)),
            int(row["_candidate_rowid"]),
            str(row["raybet_match_id"]),
        ),
        reverse=True,
    )
    return candidates[:_REALTIME_REVIEW_LIMIT]


def _recent_vision_match_ids(
    connection: PostgresSession,
    checked_at: datetime,
) -> tuple[str, ...]:
    rows = _rows(
        connection,
        """SELECT raybet_match_id, captured_at
             FROM vision_observations
            WHERE confirmed=1 AND screen_state='game'
              AND captured_at>=? AND captured_at<=?
            ORDER BY captured_at DESC, raybet_match_id DESC
            LIMIT ?""",
        (
            (checked_at - timedelta(seconds=120)).isoformat(),
            checked_at.isoformat(),
            _REALTIME_VISION_SCAN_LIMIT,
        ),
    )
    output: list[str] = []
    seen: set[str] = set()
    for row in rows:
        captured_at = _parse_time(row["captured_at"])
        match_id = str(row["raybet_match_id"])
        if (
            captured_at is None
            or captured_at > checked_at
            or not match_id
            or match_id in seen
        ):
            continue
        seen.add(match_id)
        output.append(match_id)
        if len(output) >= _REALTIME_BUCKET_LIMIT:
            break
    return tuple(output)


def _history_candidate_window(
    connection: PostgresSession,
    *,
    checked_at: datetime,
    before: tuple[int, str] | None,
) -> tuple[list[DatabaseRow], bool, DatabaseRow | None]:
    selected = _selected_match_columns()
    if before is None:
        keyset_clause = ""
        keyset_params: tuple[Any, ...] = ()
    else:
        keyset_clause = f"""AND (
            {_TIMELINE_KEY_SQL}<?
            OR (
                {_TIMELINE_KEY_SQL}=?
                AND raybet_match_id<?
            )
        )"""
        keyset_params = (before[0], before[0], before[1])
    raw_rows = _rows(
        connection,
        f"""SELECT {_TIMELINE_KEY_SQL} AS _timeline_key, {selected}
               FROM raybet_matches
              WHERE {_TIMELINE_KEY_SQL}<=
                    CAST(EXTRACT(EPOCH FROM CAST(? AS timestamptz)) * 1000
                         AS BIGINT)
                {keyset_clause}
              ORDER BY {_TIMELINE_KEY_SQL} DESC, raybet_match_id DESC
              LIMIT ?""",
        (
            checked_at.isoformat(),
            *keyset_params,
            _HISTORY_RAW_SCAN_LIMIT + 1,
        ),
    )
    raw_more = len(raw_rows) > _HISTORY_RAW_SCAN_LIMIT
    candidates: list[DatabaseRow] = []
    raw_anchor: DatabaseRow | None = None
    for row in raw_rows[:_HISTORY_RAW_SCAN_LIMIT]:
        raw_anchor = row
        if is_head_to_head_match_row(row):
            candidates.append(row)
            if len(candidates) > _HISTORY_SCAN_LIMIT:
                raw_more = True
                break
    return candidates, raw_more, raw_anchor


def is_head_to_head_match_row(row: DatabaseRow) -> bool:
    try:
        payload = json.loads(str(row["raw_json"]))
        if not isinstance(payload, dict):
            return False
        classification = classify_raybet_match_format(payload)
        if classification == RAYBET_MATCH_HEAD_TO_HEAD:
            return True
        if classification == RAYBET_MATCH_NON_HEAD_TO_HEAD:
            return False
        team_one = str(row["team_one"] or "").strip()
        team_two = str(row["team_two"] or "").strip()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        team_one
        and team_two
        and team_one.casefold() != team_two.casefold()
    )
def _encode_history_cursor(row: DatabaseRow, checked_at: datetime) -> str:
    payload = {
        "checked_at": checked_at.isoformat(),
        "match_id": str(row["raybet_match_id"]),
        "timeline_key": int(row["_timeline_key"]),
        "v": 1,
    }
    body = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    digest = hmac.new(
        _HISTORY_CURSOR_SECRET,
        _HISTORY_CURSOR_DOMAIN + body,
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{digest}"


def _decode_history_cursor(cursor: str) -> dict[str, Any]:
    if (
        not isinstance(cursor, str)
        or not 1 <= len(cursor) <= _HISTORY_CURSOR_MAX_LENGTH
        or _HISTORY_CURSOR_PATTERN.fullmatch(cursor) is None
    ):
        raise ValueError("invalid history cursor")
    encoded, supplied_digest = cursor.split(".", 1)
    try:
        padding = "=" * (-len(encoded) % 4)
        body = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError):
        raise ValueError("invalid history cursor") from None
    if base64.urlsafe_b64encode(body).decode("ascii").rstrip("=") != encoded:
        raise ValueError("invalid history cursor")
    expected_digest = hmac.new(
        _HISTORY_CURSOR_SECRET,
        _HISTORY_CURSOR_DOMAIN + body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_digest, expected_digest):
        raise ValueError("invalid history cursor")
    try:
        payload = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("invalid history cursor") from None
    if not isinstance(payload, dict) or set(payload) != {
        "checked_at",
        "match_id",
        "timeline_key",
        "v",
    }:
        raise ValueError("invalid history cursor")
    checked_at = _parse_time(payload["checked_at"])
    if (
        payload["v"] != 1
        or type(payload["timeline_key"]) is not int
        or not 0 <= payload["timeline_key"] <= 10**18
        or not isinstance(payload["match_id"], str)
        or not 1 <= len(payload["match_id"]) <= 256
        or checked_at is None
    ):
        raise ValueError("invalid history cursor")
    return {
        **payload,
        "checked_at": checked_at,
    }


def _require_history_cursor_anchor(
    connection: PostgresSession,
    cursor: dict[str, Any],
) -> None:
    row = connection.execute(
        f"""SELECT {_TIMELINE_KEY_SQL} AS timeline_key
              FROM raybet_matches WHERE raybet_match_id=?""",
        (cursor["match_id"],),
    ).fetchone()
    if (
        row is None
        or int(row["timeline_key"]) != cursor["timeline_key"]
    ):
        raise ValueError("history cursor anchor changed")


def _monitor_match(
    connection: PostgresSession,
    row: DatabaseRow,
    now: datetime,
) -> dict[str, Any]:
    match_id = str(row["raybet_match_id"])
    if _has_transport_observations(connection, match_id):
        latest_odds = _latest_row(
            connection,
            """SELECT observed_at FROM odds_transport_observations
                WHERE raybet_match_id=?
                  AND source='direct'
                  AND live_text_timestamp_utc(observed_at)<=CAST(? AS timestamptz)
                  AND timing_status='on_time'
                  AND processing_status='processed'
                ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (match_id, now.isoformat()),
        )
    else:
        latest_odds = _latest_row(
            connection,
            """SELECT received_at AS observed_at FROM odds_snapshots
                WHERE raybet_match_id=?
                  AND live_text_timestamp_utc(received_at)<=CAST(? AS timestamptz)
                ORDER BY received_at DESC, id DESC LIMIT 1""",
            (match_id, now.isoformat()),
        )
    latest_vision = _latest_valid_vision_row(connection, match_id, now=now)
    mapping_readiness = _mapping_readiness(connection, match_id, now)
    latest_odds_activity = _latest_odds_activity(connection, match_id, now=now)

    odds_readiness = _freshness(latest_odds, now, warning=15.0, stale=60.0)
    vision_readiness = _freshness(latest_vision, now, warning=20.0, stale=120.0)
    lifecycle = _lifecycle(
        str(row["status"] or ""),
        row["scheduled_at"],
        row["updated_at"],
        latest_vision,
        vision_readiness,
        checked_at=now,
    )
    vision_map_number = (
        int(latest_vision["map_number"])
        if lifecycle in {"live", "degraded"}
        and latest_vision is not None
        and type(latest_vision["map_number"]) is int
        and int(latest_vision["map_number"]) > 0
        else None
    )
    provider_map_number = (
        _provider_current_map_number(row)
        if lifecycle in {"live", "degraded"}
        else None
    )
    map_candidates = [
        value
        for value in (provider_map_number, vision_map_number)
        if value is not None
    ]
    current_map_number = max(map_candidates) if map_candidates else None
    current_winner = _current_winner(
        connection,
        match_id,
        provider_status=str(row["status"] or ""),
        preferred_period=(
            f"map_{current_map_number}" if current_map_number is not None else None
        ),
    )
    history_eligible = _history_eligible(
        lifecycle,
        str(row["status"] or ""),
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
        "current_map_number": current_map_number,
        "winner": current_winner,
        "latest_vision": (
            _vision_point(latest_vision, match_id) if latest_vision else None
        ),
        "readiness": {
            "odds": odds_readiness,
            "mapping": mapping_readiness,
            "vision": vision_readiness,
        },
    }


def _provider_current_map_number(row: DatabaseRow) -> int | None:
    try:
        payload = json.loads(str(row["raw_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    best_of = row["best_of"]
    if type(best_of) is not int or not 1 <= int(best_of) <= 5:
        return None
    indexes: set[int] = set()
    for team in payload.get("team") or []:
        if not isinstance(team, dict):
            continue
        score = team.get("score")
        manual = score.get("manualControlData") if isinstance(score, dict) else None
        raw_index = manual.get("currentIndex") if isinstance(manual, dict) else None
        if raw_index is None or isinstance(raw_index, bool):
            continue
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if str(index) != str(raw_index).strip() or not 1 <= index <= int(best_of):
            continue
        indexes.add(index)
    manual_map = indexes.pop() if len(indexes) == 1 else None
    if str(row["status"] or "") != "2":
        return manual_map
    try:
        settled_map = infer_current_map_number(payload, int(best_of))
    except ValueError:
        return None
    if settled_map is None:
        return None
    return max(value for value in (manual_map, settled_map) if value is not None)


def _watch_link(
    connection: PostgresSession,
    row: DatabaseRow,
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
    if str(row["status"] or "").strip().casefold() in _OPEN_MATCH_STATUSES:
        return {
            "kind": "stream_resolver",
            "availability": "available",
            "url": (
                "/api/monitor/matches/"
                f"{url_quote(str(row['raybet_match_id']), safe='')}"
                "/live-stream"
            ),
            "reason": "fresh_stream_resolution_available",
        }
    return {
        "kind": "none",
        "availability": "unavailable",
        "url": None,
        "reason": "no_safe_entry",
    }


def _captured_raybet_page_url(
    connection: PostgresSession,
    raybet_match_id: str,
) -> str | None:
    if not _relation_has_columns(
        connection,
        "browser_events",
        {
            "page_origin",
            "page_path",
            "raybet_match_id",
            "game_id",
            "recognized",
            "event_type",
            "processing_status",
            "captured_at",
            "event_id",
        },
    ):
        return None
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
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return None
        raise
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
    connection: PostgresSession,
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
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            rows = []
        else:
            raise
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
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    provider_status: str,
    processing_status: str = "processed",
    transport_only: bool = False,
    preferred_period: str | None = None,
) -> dict[str, Any] | None:
    normalized_status = provider_status.casefold()
    transport_columns = _relation_columns(
        connection, "odds_transport_observations"
    )
    required_transport_columns = {
        "observation_key",
        "raybet_match_id",
        "observed_at",
        "timing_status",
        "processing_status",
        "source",
    }
    if transport_columns is None:
        has_transport = False
    elif not required_transport_columns <= transport_columns:
        return None
    else:
        has_transport = connection.execute(
            """SELECT 1 FROM odds_transport_observations
                WHERE raybet_match_id=? LIMIT 1""",
            (raybet_match_id,),
        ).fetchone() is not None
    if transport_only and not has_transport:
        return None

    if has_transport:
        if not _relation_has_columns(
            connection,
            "odds_response_outcomes_effective",
            {
                "observation_key",
                "raybet_match_id",
                "odds_group_id",
                "side",
                "price",
                "status",
                "period",
                "odds_id",
                "market_type",
                "supported",
            },
        ):
            return None
        try:
            transport_limit = (
                "" if normalized_status in _ENDED_MATCH_STATUSES else "LIMIT 16"
            )
            quotes = _rows(
                connection,
                f"""WITH recent_transport AS (
                   SELECT observation_key, observed_at
                     FROM odds_transport_observations
                    WHERE raybet_match_id=?
                      AND source='direct'
                      AND timing_status='on_time'
                      AND processing_status=?
                    ORDER BY observed_at DESC, observation_key DESC
                    {transport_limit}
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
                (raybet_match_id, processing_status, raybet_match_id),
            )
        except SQLAlchemyError as error:
            # Once exact transport membership exists, a malformed response
            # schema cannot be replaced by carried-forward legacy snapshots.
            if _is_schema_missing_error(error):
                return None
            raise
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
    grouped: dict[tuple[str, str, str], dict[str, DatabaseRow]] = defaultdict(dict)
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
    by_period: dict[str, tuple[str, str, dict[str, DatabaseRow]]] = {}
    paired_by_period: dict[
        str, list[tuple[str, str, dict[str, DatabaseRow]]]
    ] = defaultdict(list)
    for (period, response_key, _group_id), sides in grouped.items():
        if set(sides) != {"team_one", "team_two"}:
            continue
        observed_at = str(next(iter(sides.values()))["received_at"])
        paired_by_period[period].append((observed_at, response_key, sides))
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
    if preferred_period and preferred_period not in by_period:
        return {
            "observed_at": str(quotes[0]["received_at"]),
            "period": preferred_period,
            "complete": False,
        }
    if preferred_period in by_period and preferred_period not in eligible_periods:
        observed_at, _response_key, _by_side = by_period[preferred_period]
        return {
            "observed_at": observed_at,
            "period": preferred_period,
            "complete": False,
        }
    if not eligible_periods:
        return {"observed_at": str(quotes[0]["received_at"]), "complete": False}
    if preferred_period in eligible_periods:
        period = preferred_period
    elif normalized_status in _UPCOMING_MATCH_STATUSES and "map_1" in eligible_periods:
        period = "map_1"
    elif normalized_status in _ENDED_MATCH_STATUSES:
        period = eligible_periods[-1]
    else:
        period = eligible_periods[0]

    observed_at, _response_key, by_side = by_period[period]
    if set(by_side) != {"team_one", "team_two"}:
        return {"observed_at": observed_at, "complete": False}
    prices = {side: float(by_side[side]["price"]) for side in by_side}
    if normalized_status in _ENDED_MATCH_STATUSES:
        final_signature = (
            tuple(sorted(prices.items())),
            tuple(sorted((side, str(by_side[side]["status"])) for side in by_side)),
        )
        for candidate_at, _candidate_key, candidate_sides in sorted(
            paired_by_period[period],
            key=lambda item: (item[0], item[1]),
            reverse=True,
        ):
            candidate_signature = (
                tuple(
                    sorted(
                        (side, float(candidate_sides[side]["price"]))
                        for side in candidate_sides
                    )
                ),
                tuple(
                    sorted(
                        (side, str(candidate_sides[side]["status"]))
                        for side in candidate_sides
                    )
                ),
            )
            if candidate_signature != final_signature:
                break
            observed_at = candidate_at
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


def _vision_timeline(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime | None = None,
    max_points: int = 1200,
) -> list[dict[str, Any]]:
    return [
        _vision_point(row, raybet_match_id)
        for row in _valid_vision_rows(
            connection,
            raybet_match_id,
            now=_aware_utc(now or utc_now()),
            max_points=max_points,
        )
    ]


def valid_vision_frame_observation(
    connection: PostgresSession,
    raybet_match_id: str,
    frame_ref: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    rows = _valid_vision_rows(
        connection,
        raybet_match_id,
        now=_aware_utc(now or utc_now()),
        max_points=1,
        source_frame_ref=frame_ref,
    )
    return _vision_point(rows[0], raybet_match_id) if rows else None


def valid_capture_frame_observation(
    connection: PostgresSession,
    raybet_match_id: str,
    frame_ref: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    row = _latest_capture_row(
        connection,
        raybet_match_id,
        now=_aware_utc(now or utc_now()),
        source_frame_ref=frame_ref,
    )
    return _capture_point(row, raybet_match_id) if row is not None else None


def _latest_capture_row(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime,
    source_frame_ref: str | None = None,
) -> DatabaseRow | None:
    frame_filter = (
        "AND observation.source_frame_ref=?" if source_frame_ref is not None else ""
    )
    params: tuple[Any, ...] = (raybet_match_id, now.isoformat())
    if source_frame_ref is not None:
        params += (source_frame_ref,)
    try:
        return connection.execute(
            f"""SELECT observation.captured_at,
                       observation.captured_at AS observed_at,
                       observation.map_number,
                       observation.game_clock_seconds,
                       observation.is_paused,
                       observation.radiant_team_side,
                       observation.clock_confidence,
                       observation.draft_confidence,
                       observation.radiant_hero_ids,
                       observation.dire_hero_ids,
                       observation.source_frame_ref,
                       observation.screen_state,
                       observation.confirmed,
                       frame.content_sha256 AS _frame_digest
                  FROM vision_observations AS observation
                  JOIN active_vision_frame_artifacts AS frame
                    ON frame.frame_ref=observation.source_frame_ref
                   AND frame.content_sha256=observation.source_frame_sha256
                   AND frame.byte_length=observation.source_frame_bytes
                 WHERE observation.raybet_match_id=?
                   AND live_text_timestamp_utc(
                           observation.captured_at
                       ) IS NOT NULL
                   AND live_text_timestamp_utc(observation.captured_at)<=
                       CAST(? AS timestamptz)
                   AND observation.source_frame_ref=
                       ? || frame.content_sha256
                   AND NOT EXISTS (
                        SELECT 1
                          FROM vision_observation_invalidations AS invalidation
                         WHERE invalidation.raybet_match_id=
                               observation.raybet_match_id
                           AND invalidation.captured_at=observation.captured_at
                           AND invalidation.source_frame_ref=
                               observation.source_frame_ref
                   )
                   {frame_filter}
                 ORDER BY observation.captured_at DESC,
                          observation.source_frame_ref DESC
                 LIMIT 1""",
            (*params[:2], VISION_FRAME_REF_PREFIX, *params[2:]),
        ).fetchone()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return None
        raise


def _latest_valid_vision_row(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime,
) -> DatabaseRow | None:
    rows = _valid_vision_rows(
        connection,
        raybet_match_id,
        now=now,
        max_points=1,
    )
    return rows[0] if rows else None


def _valid_vision_rows(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime,
    max_points: int,
    source_frame_ref: str | None = None,
) -> list[DatabaseRow]:
    if type(max_points) is not int or max_points <= 0:
        raise ValueError("max_points must be a positive integer")
    limit = min(max_points, _MAX_VISION_TIMELINE_POINTS)
    frame_filter = (
        "AND observation.source_frame_ref=?" if source_frame_ref is not None else ""
    )
    params: tuple[Any, ...] = (raybet_match_id, now.isoformat())
    if source_frame_ref is not None:
        params += (source_frame_ref,)
    params += (limit,)
    try:
        return list(
            connection.execute(
                f"""SELECT recent.*
                       FROM (
                            SELECT observation.captured_at,
                                   observation.captured_at AS observed_at,
                                   observation.map_number,
                                   observation.game_clock_seconds,
                                   observation.is_paused,
                                   observation.radiant_team_side,
                                   observation.clock_confidence,
                                   observation.draft_confidence,
                                   observation.radiant_hero_ids,
                                   observation.dire_hero_ids,
                                   observation.source_frame_ref,
                                   observation.screen_state,
                                   observation.confirmed,
                                   CASE
                                       WHEN frame.frame_ref IS NOT NULL
                                        AND observation.source_frame_ref=
                                            ? || frame.content_sha256
                                       THEN frame.content_sha256
                                       ELSE NULL
                                   END AS _frame_digest
                               FROM vision_observations AS observation
                               LEFT JOIN active_vision_frame_artifacts AS frame
                                ON frame.frame_ref=observation.source_frame_ref
                               AND frame.content_sha256=
                                   observation.source_frame_sha256
                               AND frame.byte_length=observation.source_frame_bytes
                              WHERE observation.raybet_match_id=?
                                AND observation.map_number IS NOT NULL
                                AND observation.game_clock_seconds IS NOT NULL
                                AND observation.screen_state='game'
                                AND observation.clock_confidence>=0.9
                               AND live_text_timestamp_utc(
                                       observation.captured_at
                                   ) IS NOT NULL
                               AND live_text_timestamp_utc(
                                       observation.captured_at
                                   )<=CAST(? AS timestamptz)
                                AND NOT EXISTS (
                                    SELECT 1
                                      FROM vision_observation_invalidations AS invalidation
                                     WHERE invalidation.raybet_match_id=
                                           observation.raybet_match_id
                                       AND invalidation.captured_at=
                                           observation.captured_at
                                       AND invalidation.source_frame_ref=
                                           observation.source_frame_ref
                               )
                               {frame_filter}
                             ORDER BY observation.captured_at DESC,
                                      observation.source_frame_ref DESC
                             LIMIT ?
                       ) AS recent
                      ORDER BY recent.captured_at, recent.source_frame_ref""",
                (VISION_FRAME_REF_PREFIX, *params),
            ).fetchall()
        )
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return []
        raise


def _vision_point(row: DatabaseRow, raybet_match_id: str) -> dict[str, Any]:
    point = dict(row)
    point["radiant_hero_ids"] = _vision_hero_ids(point.get("radiant_hero_ids"))
    point["dire_hero_ids"] = _vision_hero_ids(point.get("dire_hero_ids"))
    point["dynamic_state_authority"] = True
    point["draft_authority"] = bool(point.get("confirmed"))
    digest = point.pop("_frame_digest", None)
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        point["frame_digest"] = digest
        point["frame_url"] = (
            f"/api/monitor/matches/{url_quote(raybet_match_id, safe='')}"
            f"/vision-frames/{digest}.jpg"
        )
    else:
        point["frame_digest"] = None
        point["frame_url"] = None
    return point


def _capture_point(row: DatabaseRow, raybet_match_id: str) -> dict[str, Any]:
    point = dict(row)
    point["radiant_hero_ids"] = _vision_hero_ids(point.get("radiant_hero_ids"))
    point["dire_hero_ids"] = _vision_hero_ids(point.get("dire_hero_ids"))
    digest = point.pop("_frame_digest", None)
    point["strategy_authority"] = False
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        point["frame_digest"] = digest
        point["frame_url"] = (
            f"/api/monitor/matches/{url_quote(raybet_match_id, safe='')}"
            f"/captures/{digest}.jpg"
        )
    else:
        point["frame_digest"] = None
        point["frame_url"] = None
    return point


def _vision_hero_ids(value: object) -> list[int]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    heroes = [int(hero_id) for hero_id in parsed if type(hero_id) is int and hero_id > 0]
    return heroes if len(heroes) == len(set(heroes)) and len(heroes) <= 5 else []


def _freshness(
    row: DatabaseRow | None,
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
    latest_vision: DatabaseRow | None,
    vision_readiness: dict[str, Any],
    *,
    checked_at: datetime,
) -> str:
    normalized = provider_status.casefold()
    if normalized in _ENDED_MATCH_STATUSES:
        return (
            "ended"
            if _ended_schedule_is_trustworthy(scheduled_at, checked_at)
            else "degraded"
        )
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
    provider_status: str,
    scheduled_at: object,
    updated_at: object,
    *,
    odds_activity_at: datetime | None = None,
    checked_at: datetime,
) -> bool:
    """Expose old odds for replay without claiming provider settlement."""
    if lifecycle == "ended":
        return _ended_schedule_is_trustworthy(scheduled_at, checked_at)
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
    schedule_grace = (
        _PREMATCH_HISTORY_SCHEDULE_GRACE
        if provider_status.casefold() in _UPCOMING_MATCH_STATUSES
        else _HISTORY_SCHEDULE_GRACE
    )
    return (
        scheduled <= checked_at - schedule_grace
        and activity <= checked_at - _HISTORY_ACTIVITY_GRACE
    )


def _ended_schedule_is_trustworthy(
    scheduled_at: object,
    checked_at: datetime,
) -> bool:
    if scheduled_at is None:
        return True
    scheduled = _parse_schedule(scheduled_at)
    return scheduled is not None and scheduled <= checked_at


def _latest_odds_activity(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime,
) -> datetime | None:
    """Return the newest immutable odds activity for archive gating.

    ``updated_at`` belongs to the match-list metadata and can remain stale
    while the odds page continues emitting responses. Direct transport rows
    are authoritative once any transport exists for the match; legacy
    snapshots are considered only when the match has no transport records.
    """

    if _has_transport_observations(connection, raybet_match_id):
        row = _latest_row(
            connection,
            """SELECT observed_at
                 FROM odds_transport_observations
                WHERE raybet_match_id=? AND source='direct'
                  AND live_text_timestamp_utc(observed_at)<=CAST(? AS timestamptz)
                ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (raybet_match_id, now.isoformat()),
        )
        return _parse_time(row["observed_at"]) if row is not None else None
    row = _latest_row(
        connection,
        """SELECT received_at
             FROM odds_snapshots
            WHERE raybet_match_id=?
              AND live_text_timestamp_utc(received_at)<=CAST(? AS timestamptz)
            ORDER BY received_at DESC, id DESC LIMIT 1""",
        (raybet_match_id, now.isoformat()),
    )
    return _parse_time(row["received_at"]) if row is not None else None


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
    text = str(value)
    try:
        if len(text) == 19:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            if parsed.year < 1000 or parsed.strftime("%Y-%m-%d %H:%M:%S") != text:
                return None
            parsed = parsed.replace(tzinfo=_LOCAL_TIMEZONE)
        elif len(text) == 25:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
            if (
                parsed.year < 1000
                or parsed.isoformat(timespec="seconds") != text
                or parsed.utcoffset() != timedelta(0)
            ):
                return None
        else:
            return None
    except (OverflowError, ValueError):
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        return None


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
    connection: PostgresSession,
    raybet_match_id: str,
) -> bool:
    """Return any-source membership, which blocks unsafe legacy fallback."""
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


def _relation_columns(
    connection: PostgresSession,
    relation: str,
) -> frozenset[str] | None:
    if re.fullmatch(r"[A-Za-z0-9_]+", relation) is None:
        raise ValueError("invalid PostgreSQL relation name")
    exists = connection.execute(
        """SELECT 1
             FROM pg_class AS relation
             JOIN pg_namespace AS namespace
               ON namespace.oid=relation.relnamespace
            WHERE namespace.nspname=current_schema()
              AND relation.relkind IN ('r', 'p', 'v')
              AND relation.relname=?""",
        (relation,),
    ).fetchone()
    if exists is None:
        return None
    rows = connection.execute(
        """SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=?""",
        (relation,),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _relation_has_columns(
    connection: PostgresSession,
    relation: str,
    required: set[str] | frozenset[str],
) -> bool:
    columns = _relation_columns(connection, relation)
    return columns is not None and required <= columns


def _rows(
    connection: PostgresSession,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[DatabaseRow]:
    try:
        return list(connection.execute(query, params).fetchall())
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return []
        raise


def _latest_row(
    connection: PostgresSession,
    query: str,
    params: tuple[Any, ...],
) -> DatabaseRow | None:
    try:
        return connection.execute(query, params).fetchone()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return None
        raise


def _scalar(
    connection: PostgresSession,
    query: str,
    params: tuple[Any, ...] = (),
    *,
    default: Any = None,
) -> Any:
    try:
        row = connection.execute(query, params).fetchone()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return default
        raise
    return row[0] if row is not None else default


def _latest_value(connection: PostgresSession, table: str, column: str) -> Any:
    return _scalar(
        connection,
        f"SELECT {column} FROM {table} ORDER BY {column} DESC LIMIT 1",
        default=None,
    )


def _is_schema_missing_error(error: SQLAlchemyError) -> bool:
    cause = getattr(error, "orig", error)
    sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    return sqlstate in {"42P01", "42703"}


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
    "monitor_history_page",
    "monitor_match_detail",
    "monitor_matches",
    "winner_timeline",
]
