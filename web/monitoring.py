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
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote as url_quote

from contracts.live_observation import is_canonical_net_worth_bucket
from live_betting.comeback import STRATEGY_VERSION
from live_betting.comeback_entry import ComebackEntryPolicy
from live_betting.draft_authority import (
    DraftLandmarkAuthority,
    draft_landmark_authority_matches,
)
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
from live_betting.storage import query_rosh_lineup_score_for_trusted_draft
from live_betting.strategy_contract import (
    parse_decision_payload,
    persisted_decision_projection_failure,
    validate_strategy_contract,
)
from live_betting.stratz_rosh_client import ROSH_FORMULA_VERSION
from live_betting.strict_eligibility import (
    RAYBET_MATCH_HEAD_TO_HEAD,
    RAYBET_MATCH_NON_HEAD_TO_HEAD,
    classify_raybet_match_format,
    query_strict_mapping_snapshot,
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
_SQLITE_DATETIME_ROUNDING_GRACE = timedelta(milliseconds=1)
_EXPECTED_HEALTH_COMPONENTS = {
    "raybet_worker": 45.0,
    "strict_ingest_worker": 90.0,
    "historical_rosh_worker": 900.0,
}
_PRIMARY_HEALTH_COMPONENTS = {
    "database",
    "raybet",
    "raybet_worker",
    "raybet_priority_odds_worker",
    "raybet_full_odds_worker",
    "strict_ingest",
    "strict_ingest_worker",
    "historical_rosh",
    "historical_rosh_worker",
    "mail",
    "mail_worker",
    "mail_delivery",
}
_OPTIONAL_UNCONFIGURED_COMPONENTS = {"mail", "mail_worker", "mail_delivery"}
_RAYBET_PAGE_ORIGINS = frozenset(
    {"https://ray086.com", "https://www.ray086.com"}
)
_RAYBET_PAGE_PREFIXES = ("/sports/esports", "/esports", "/dota2")
_RAYBET_PAGE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
_MAX_VISION_TIMELINE_POINTS = 5_000
_MAX_DETAIL_DECISIONS = 200
_MAX_DETAIL_DECISION_SCAN = 1_000
_STRATEGY_SCAN_BATCH = 256
_MAX_TRUSTED_VISION_CANDIDATES = 256
_MAX_STRICT_MAPPING_CANDIDATES = 64
_MAX_PUBLIC_EVIDENCE_DEPTH = 10
_MAX_PUBLIC_EVIDENCE_NODES = 1_000
_MAX_PUBLIC_EVIDENCE_STRING = 8_192
_ANALYSIS_STATUSES = frozenset({"available", "waiting", "unavailable", "review"})
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECISION_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_INPUT_REF_RE = re.compile(r"^[0-9a-f]{24}$")
_INVALID_EVIDENCE = object()
_DEFAULT_COMEBACK_ENTRY_POLICY = asdict(ComebackEntryPolicy())
_LEGACY_COMEBACK_STRATEGY_VERSIONS = frozenset(
    {
        "comeback-shadow-v1",
        "comeback-shadow-v2",
        "comeback-shadow-v2-strict-landmarks",
        "comeback-shadow-v3",
        "comeback-shadow-v3-rosh-lineup",
    }
)
_CONTRIBUTION_KEYS = frozenset(
    {
        "team_style",
        "player_form",
        "draft_curve",
        "lineup_rosh",
        "late_game_style",
        "market_movement",
    }
)
_REQUIRED_CONTRIBUTION_KEYS = frozenset(
    {
        "team_style",
        "player_form",
        "draft_curve",
        "lineup_rosh",
        "late_game_style",
        "market_movement",
    }
)
_LEGACY_REQUIRED_CONTRIBUTION_KEYS = _REQUIRED_CONTRIBUTION_KEYS - {
    "lineup_rosh"
}
_INDEPENDENT_CONTRIBUTION_KEYS = frozenset(
    {"team_style", "player_form", "lineup_rosh", "late_game_style"}
)
_DRAFT_AUTHORITY_FIELDS = (
    "curve_key",
    "source_ref",
    "landmark_key",
    "landmark_horizon_minutes",
    "landmark_target",
    "landmark_radiant_probability",
    "landmark_quality",
    "landmark_uncertainty",
    "landmark_support",
    "radiant_team_side",
    "strict_mapping_id",
    "deployment_key",
    "target_snapshot_hash",
    "feature_hash",
    "model_hash",
    "calibration_hash",
    "model_version",
    "global_gate_ref",
    "input_snapshot_hash",
    "authority_revision",
    "dependency_revision",
)
_VISION_AUTHORITY_FIELDS = (
    "raybet_match_id",
    "map_number",
    "captured_at",
    "source_frame_ref",
    "source_frame_sha256",
    "source_frame_bytes",
    "observed_game_clock_seconds",
    "aligned_game_clock_seconds",
    "is_paused",
    "radiant_hero_ids_json",
    "dire_hero_ids_json",
    "radiant_team_side",
    "clock_confidence",
    "draft_confidence",
    "screen_state",
    "confirmed",
    "transport_key",
    "transport_at",
    "alignment_method",
    "alignment_lag_seconds",
)
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
    optional_components_unconfigured = any(
        item["component"] in _OPTIONAL_UNCONFIGURED_COMPONENTS
        and (
            item["last_error"] == "configuration_missing"
            or item["details"].get("configured") is False
            or item["details"].get("smtp_configured") is False
        )
        for item in health
    )
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
                and not (
                    item["component"] in _OPTIONAL_UNCONFIGURED_COMPONENTS
                    and optional_components_unconfigured
                )
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
        "historical_rosh": {
            "required": True,
            "status": statuses.get("historical_rosh", "stopped"),
        },
    }


def monitor_matches(
    connection: PostgresSession,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    checked_at = _aware_utc(now or utc_now())
    rows = _realtime_match_candidates(connection, checked_at)
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
    health_by_component = {
        item["component"]: item for item in derive_health(connection, now=checked_at)
    }
    items: list[dict[str, Any]] = []
    returned_rows: list[DatabaseRow] = []
    last_scanned: DatabaseRow | None = None
    found_extra = False
    for row in candidates:
        last_scanned = row
        item = _monitor_match(connection, row, checked_at, health_by_component)
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
    health_by_component = {
        item["component"]: item for item in derive_health(connection, now=checked_at)
    }
    summary = _monitor_match(connection, row, checked_at, health_by_component)
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
    strategy = _strategy_analysis(
        connection,
        raybet_match_id,
        summary["readiness"]["strategy"],
        lifecycle=str(summary["lifecycle"]),
        now=checked_at,
    )
    decisions = (
        list(strategy["data"]["decisions"])
        if isinstance(strategy.get("data"), dict)
        and isinstance(strategy["data"].get("decisions"), list)
        else []
    )
    trusted_context = _trusted_live_context(connection, raybet_match_id, checked_at)
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
        "latest_decision": decisions[-1] if decisions else None,
        "winner_timeline": timeline,
        "decisions": decisions,
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
        "analysis": {
            "odds": _odds_analysis(
                connection,
                raybet_match_id,
                timeline,
                lifecycle=str(summary["lifecycle"]),
                now=checked_at,
            ),
            "vision": _vision_analysis(trusted_context),
            "strategy": strategy,
            "lineup": _lineup_analysis(
                connection, raybet_match_id, trusted_context
            ),
        },
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
            (checked_at - _SQLITE_DATETIME_ROUNDING_GRACE).isoformat(),
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
    health: dict[str, dict[str, Any]],
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
    latest_decision = _latest_strategy_decision(connection, match_id, now=now)
    mapping_readiness = _mapping_readiness(connection, match_id, now)
    latest_odds_activity = _latest_odds_activity(connection, match_id, now=now)

    odds_readiness = _freshness(latest_odds, now, warning=15.0, stale=60.0)
    vision_readiness = _freshness(latest_vision, now, warning=20.0, stale=120.0)
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
        "latest_decision": dict(latest_decision) if latest_decision else None,
        "readiness": {
            "odds": odds_readiness,
            "mapping": mapping_readiness,
            "vision": vision_readiness,
            "model": decision_readiness,
            "strategy": strategy_readiness,
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


def _analysis_section(
    status: str,
    reason: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in _ANALYSIS_STATUSES:
        raise ValueError("invalid analysis section status")
    return {"status": status, "reason": reason, "data": data}


def _odds_analysis(
    connection: PostgresSession,
    raybet_match_id: str,
    timeline: list[dict[str, Any]],
    *,
    lifecycle: str,
    now: datetime,
) -> dict[str, Any]:
    if timeline:
        return _analysis_section(
            "available",
            "odds_available",
            {
                "point_count": len(timeline),
                "periods": sorted(
                    {str(point["period"]) for point in timeline},
                    key=_period_sort_key,
                ),
                "latest_observed_at": str(timeline[-1]["observed_at"]),
            },
        )
    try:
        if _has_transport_observations(connection, raybet_match_id):
            observed = bool(
                connection.execute(
                    """SELECT EXISTS(
                           SELECT 1 FROM odds_transport_observations
                            WHERE raybet_match_id=? AND source='direct'
                              AND live_text_timestamp_utc(observed_at)<=
                                  CAST(? AS timestamptz)
                       )""",
                    (raybet_match_id, now.isoformat()),
                ).fetchone()[0]
            )
        else:
            observed = bool(
                connection.execute(
                    """SELECT EXISTS(
                           SELECT 1 FROM odds_snapshots
                            WHERE raybet_match_id=?
                              AND live_text_timestamp_utc(received_at)<=
                                  CAST(? AS timestamptz)
                       )""",
                    (raybet_match_id, now.isoformat()),
                ).fetchone()[0]
            )
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return _analysis_section("unavailable", "odds_schema_missing")
        raise
    if observed:
        return _analysis_section("review", "winner_odds_incomplete")
    status = "unavailable" if lifecycle == "ended" else "waiting"
    return _analysis_section(status, "winner_odds_pending")


def _trusted_live_context(
    connection: PostgresSession,
    raybet_match_id: str,
    now: datetime,
) -> dict[str, Any]:
    try:
        anchor = connection.execute(
            """SELECT raybet_match_id, map_number, draft_hash,
                      radiant_hero_ids, dire_hero_ids, radiant_team_side,
                      team_side_anchored_at, team_side_source_frame_ref,
                      anchored_at, source_frame_ref, status, conflict_at
                 FROM vision_draft_anchors
                WHERE raybet_match_id=?
                ORDER BY map_number DESC, anchored_at DESC LIMIT 1""",
            (raybet_match_id,),
        ).fetchone()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return {"status": "unavailable", "reason": "vision_schema_missing"}
        raise
    if anchor is None:
        return {"status": "waiting", "reason": "trusted_vision_pending"}
    anchored_at = _parse_time(anchor["anchored_at"])
    if anchored_at is None:
        return {"status": "review", "reason": "draft_anchor_invalid"}
    if anchored_at > now:
        return {"status": "waiting", "reason": "trusted_vision_pending"}
    if str(anchor["status"]) == "conflict":
        return {"status": "review", "reason": "draft_conflict"}
    radiant = _five_hero_ids(anchor["radiant_hero_ids"])
    dire = _five_hero_ids(anchor["dire_hero_ids"])
    side = str(anchor["radiant_team_side"] or "")
    if (
        radiant is None
        or dire is None
        or len(set(radiant + dire)) != 10
        or side not in {"team_one", "team_two"}
        or re.fullmatch(r"[0-9a-f]{64}", str(anchor["draft_hash"])) is None
    ):
        return {"status": "review", "reason": "lineup_payload_invalid"}
    try:
        candidates = connection.execute(
            """SELECT observation.captured_at,
                      observation.game_clock_seconds,
                      observation.clock_confidence,
                      observation.draft_confidence,
                      observation.source_frame_ref,
                      observation.radiant_hero_ids,
                      observation.dire_hero_ids,
                      observation.radiant_team_side
                 FROM vision_observations AS observation
                WHERE observation.raybet_match_id=?
                  AND observation.map_number=?
                  AND observation.confirmed=1
                  AND observation.screen_state='game'
                  AND live_text_timestamp_utc(observation.captured_at)<=
                      CAST(? AS timestamptz)
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_observation_invalidations AS invalidation
                       WHERE invalidation.raybet_match_id=observation.raybet_match_id
                         AND invalidation.captured_at=observation.captured_at
                         AND invalidation.source_frame_ref=observation.source_frame_ref
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_draft_conflicts AS conflict
                       WHERE conflict.raybet_match_id=observation.raybet_match_id
                         AND conflict.map_number=observation.map_number
                         AND live_text_timestamp_utc(conflict.captured_at)<=
                             live_text_timestamp_utc(observation.captured_at)
                  )
                ORDER BY observation.captured_at DESC,
                         observation.source_frame_ref DESC
                LIMIT ?""",
            (
                raybet_match_id,
                int(anchor["map_number"]),
                now.isoformat(),
                _MAX_TRUSTED_VISION_CANDIDATES + 1,
            ),
        ).fetchall()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return {"status": "unavailable", "reason": "vision_schema_missing"}
        raise
    observation = next(
        (
            row
            for row in candidates[:_MAX_TRUSTED_VISION_CANDIDATES]
            if _five_hero_ids(row["radiant_hero_ids"]) == radiant
            and _five_hero_ids(row["dire_hero_ids"]) == dire
            and str(row["radiant_team_side"] or "") == side
        ),
        None,
    )
    if observation is None:
        if len(candidates) > _MAX_TRUSTED_VISION_CANDIDATES:
            return {
                "status": "review",
                "reason": "vision_candidate_limit_exceeded",
            }
        return {"status": "waiting", "reason": "trusted_vision_pending"}
    clock = observation["game_clock_seconds"]
    if type(clock) is not int or clock < 0:
        return {"status": "review", "reason": "trusted_vision_invalid"}
    return {
        "status": "available",
        "reason": "trusted_vision_available",
        "checked_at": now,
        "anchor": dict(anchor),
        "observation": dict(observation),
        "radiant": radiant,
        "dire": dire,
    }


def _vision_analysis(context: dict[str, Any]) -> dict[str, Any]:
    if context.get("status") != "available":
        return _analysis_section(
            str(context.get("status", "unavailable")),
            str(context.get("reason", "trusted_vision_pending")),
        )
    observation = context["observation"]
    anchor = context["anchor"]
    return _analysis_section(
        "available",
        "trusted_vision_available",
        {
            "map_number": int(anchor["map_number"]),
            "captured_at": str(observation["captured_at"]),
            "game_clock_seconds": int(observation["game_clock_seconds"]),
            "clock_confidence": float(observation["clock_confidence"]),
            "draft_confidence": float(observation["draft_confidence"]),
            "source_frame_ref": str(observation["source_frame_ref"]),
        },
    )


def _lineup_analysis(
    connection: PostgresSession,
    raybet_match_id: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("status") != "available":
        return _analysis_section(
            str(context.get("status", "unavailable")),
            str(context.get("reason", "trusted_vision_pending")),
        )
    anchor = context["anchor"]
    observation = context["observation"]
    evidence_at = _parse_time(observation["captured_at"])
    checked_at = context.get("checked_at")
    if evidence_at is None or not isinstance(checked_at, datetime):
        return _analysis_section("review", "trusted_vision_invalid")
    mapping = _trusted_mapping(
        connection,
        raybet_match_id,
        int(anchor["map_number"]),
        checked_at,
    )
    if mapping["status"] != "available":
        return _analysis_section(mapping["status"], mapping["reason"])
    mapping_id = int(mapping["mapping_id"])
    curve = _persisted_lineup_curve(
        connection,
        raybet_match_id=raybet_match_id,
        map_number=int(anchor["map_number"]),
        mapping_id=mapping_id,
        radiant=context["radiant"],
        dire=context["dire"],
        radiant_team_side=str(anchor["radiant_team_side"]),
        anchor=anchor,
        game_clock_seconds=int(observation["game_clock_seconds"]),
        now=checked_at,
    )
    rosh_score = query_rosh_lineup_score_for_trusted_draft(
        connection,
        raybet_match_id=raybet_match_id,
        map_number=int(anchor["map_number"]),
        strict_mapping_id=mapping_id,
        draft_hash=str(anchor["draft_hash"]),
        radiant_hero_ids=context["radiant"],
        dire_hero_ids=context["dire"],
        as_of=checked_at,
        formula_version=ROSH_FORMULA_VERSION,
    )
    scores = (
        _analysis_section("waiting", "rosh_lineup_score_pending")
        if rosh_score is None
        else _analysis_section(
            "available",
            "rosh_lineup_score_available",
            {
                "pure_lineup_score": rosh_score.pure_score,
                "player_adjusted_lineup_score": (
                    rosh_score.player_adjusted_score
                ),
                "effective_lineup_score": rosh_score.effective_score,
                "mode": rosh_score.mode,
                "player_coverage": rosh_score.player_coverage,
                "player_coverage_count": rosh_score.player_coverage_count,
                "stake_multiplier": rosh_score.stake_multiplier,
                "formula_version": rosh_score.formula_version,
                "source_as_of": rosh_score.source_as_of.isoformat(),
                "score_key": rosh_score.score_key,
                "player_identity_hash": rosh_score.player_identity_hash,
                "evidence_hash": rosh_score.evidence_hash,
                "stake_cap": rosh_score.stake_cap,
            },
        )
    )
    players = _rosh_player_identity(
        rosh_score,
        radiant=context["radiant"],
        dire=context["dire"],
    )
    return _analysis_section(
        "available",
        "lineup_available",
        {
            "as_of": evidence_at.isoformat(),
            "map_number": int(anchor["map_number"]),
            "radiant_team_side": str(anchor["radiant_team_side"]),
            "radiant": {"hero_ids": list(context["radiant"])},
            "dire": {"hero_ids": list(context["dire"])},
            "evidence": {
                "draft_hash": str(anchor["draft_hash"]),
                "anchor_source_frame_ref": str(anchor["source_frame_ref"]),
                "anchored_at": str(anchor["anchored_at"]),
                "strict_mapping_id": mapping_id,
            },
            "scores": scores,
            "active_curve": curve,
            "players": players,
        },
    )


def _rosh_player_identity(
    score: Any,
    *,
    radiant: tuple[int, ...],
    dire: tuple[int, ...],
) -> dict[str, Any]:
    if score is None or score.player_coverage_count == 0:
        return _analysis_section(
            "unavailable", "live_player_identity_unavailable"
        )
    slots = score.evidence.get("player_slots")
    if not isinstance(slots, list) or len(slots) != 10:
        return _analysis_section("review", "rosh_player_identity_evidence_invalid")
    expected_heroes = (*radiant, *dire)
    players: list[dict[str, Any]] = []
    seen_slots: set[int] = set()
    resolved_count = 0
    for value in slots:
        if not isinstance(value, dict):
            return _analysis_section(
                "review", "rosh_player_identity_evidence_invalid"
            )
        slot = value.get("slot")
        position = value.get("position")
        hero_id = value.get("hero_id")
        steam_id = value.get("steam_account_id")
        selected = value.get("selected")
        resolved = value.get("resolved")
        if (
            type(slot) is not int
            or slot not in range(10)
            or slot in seen_slots
            or value.get("side") != ("radiant" if slot < 5 else "dire")
            or type(position) is not int
            or position != (slot % 5) + 1
            or type(hero_id) is not int
            or hero_id != expected_heroes[slot]
            or (steam_id is not None and not _positive_int(steam_id))
            or type(selected) is not bool
            or type(resolved) is not bool
            or (resolved and (not selected or not _positive_int(steam_id)))
        ):
            return _analysis_section(
                "review", "rosh_player_identity_evidence_invalid"
            )
        seen_slots.add(slot)
        resolved_count += int(resolved)
        players.append(
            {
                "steam_account_id": steam_id,
                "side": value["side"],
                "position": position,
                "hero_id": hero_id,
                "status": (
                    "resolved"
                    if resolved
                    else "selected_unresolved"
                    if selected
                    else "unavailable"
                ),
            }
        )
    if resolved_count != score.player_coverage_count:
        return _analysis_section("review", "rosh_player_identity_evidence_invalid")
    return _analysis_section(
        "available",
        (
            "rosh_player_identity_available"
            if resolved_count == 10
            else "rosh_player_identity_partial"
        ),
        {"players": players},
    )


def _trusted_mapping(
    connection: PostgresSession,
    raybet_match_id: str,
    map_number: int,
    now: datetime,
) -> dict[str, Any]:
    try:
        rows = connection.execute(
            """SELECT mapping_id FROM strict_live_map_mappings
                WHERE raybet_match_id=? AND map_number=?
                ORDER BY mapping_id LIMIT ?""",
            (
                raybet_match_id,
                map_number,
                _MAX_STRICT_MAPPING_CANDIDATES + 1,
            ),
        ).fetchall()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return {"status": "unavailable", "reason": "strict_mapping_schema_missing"}
        raise
    if not rows:
        return {"status": "waiting", "reason": "strict_mapping_pending"}
    if len(rows) > _MAX_STRICT_MAPPING_CANDIDATES:
        return {
            "status": "review",
            "reason": "strict_mapping_candidate_limit_exceeded",
        }
    results = [
        query_strict_mapping_snapshot(
            connection, mapping_id=int(row[0]), observed_at=now
        )
        for row in rows
    ]
    valid = [
        result
        for result in results
        if result.eligible
        and result.mapping is not None
        and result.raybet_match_id == raybet_match_id
        and result.map_number == map_number
    ]
    if len(valid) == 1:
        return {
            "status": "available",
            "reason": "strict_mapping_available",
            "mapping_id": valid[0].mapping.mapping_id,
        }
    if len(valid) > 1:
        return {"status": "review", "reason": "strict_mapping_ambiguous"}
    reasons = {result.reason for result in results}
    if "strict_mapping_schema_missing" in reasons:
        return {"status": "unavailable", "reason": "strict_mapping_schema_missing"}
    return {"status": "review", "reason": sorted(reasons)[0]}


def _persisted_lineup_curve(
    connection: PostgresSession,
    *,
    raybet_match_id: str,
    map_number: int,
    mapping_id: int,
    radiant: tuple[int, ...],
    dire: tuple[int, ...],
    radiant_team_side: str,
    anchor: dict[str, Any],
    game_clock_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    try:
        rows = connection.execute(
            """SELECT curve_key, radiant_hero_ids_json, dire_hero_ids_json,
                      prediction_cutoff, first_usable_at, availability_mode,
                      created_at, radiant_team_side, anchor_draft_hash,
                      anchor_source_frame_ref, anchor_anchored_at,
                      anchor_team_side_source_frame_ref,
                       anchor_team_side_anchored_at, deployment_key,
                       target_snapshot_hash, feature_dependency_revision
                 FROM prospective_draft_curves
                WHERE raybet_match_id=? AND map_number=? AND strict_mapping_id=?
                ORDER BY first_usable_at DESC, curve_key DESC""",
            (raybet_match_id, map_number, mapping_id),
        ).fetchall()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return _analysis_section("unavailable", "lineup_curve_schema_missing")
        raise
    causal = []
    future_seen = False
    for row in rows:
        first_usable = _parse_time(row["first_usable_at"])
        created = _parse_time(row["created_at"])
        if first_usable is None or created is None:
            return _analysis_section("review", "lineup_curve_payload_invalid")
        if first_usable > now or created > now:
            future_seen = True
            continue
        causal.append(row)
    if not causal:
        reason = (
            "prospective_draft_artifact_not_yet_usable"
            if future_seen
            else "validated_live_draft_prediction_missing"
        )
        return _analysis_section("waiting", reason)
    row = causal[0]
    if (
        _five_hero_ids(row["radiant_hero_ids_json"]) != radiant
        or _five_hero_ids(row["dire_hero_ids_json"]) != dire
        or str(row["availability_mode"]) != "prospective"
        or str(row["radiant_team_side"]) != radiant_team_side
        or str(row["anchor_draft_hash"]) != str(anchor["draft_hash"])
        or str(row["anchor_source_frame_ref"]) != str(anchor["source_frame_ref"])
        or str(row["anchor_anchored_at"]) != str(anchor["anchored_at"])
    ):
        return _analysis_section("review", "lineup_curve_payload_invalid")
    try:
        revisions = connection.execute(
            """SELECT authority.authority_revision,
                      lineage.dependency_revision
                 FROM draft_authority_revisions AS authority
                 JOIN draft_lineage_revisions AS lineage
                   ON lineage.singleton=authority.singleton
                WHERE authority.singleton=1"""
        ).fetchone()
        landmarks = connection.execute(
            """SELECT landmark_key, horizon_minutes, radiant_probability,
                      quality, validation_status, support, uncertainty,
                      calibration_ref, input_refs_json,
                      feature_hash, model_hash, calibration_hash,
                      global_calibration_passed, global_gate_ref,
                      model_version, model_kind, availability_mode,
                      input_snapshot_hash, deployment_key, created_at
                 FROM prospective_draft_landmarks
                WHERE curve_key=? ORDER BY horizon_minutes""",
            (row["curve_key"],),
        ).fetchall()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return _analysis_section("unavailable", "lineup_curve_schema_missing")
        raise
    if revisions is None or not landmarks:
        return _analysis_section("waiting", "validated_live_draft_prediction_missing")
    try:
        persisted_dependency_revision = int(row["feature_dependency_revision"])
        current_dependency_revision = int(revisions[1])
    except (TypeError, ValueError):
        return _analysis_section("review", "lineup_curve_payload_invalid")
    if persisted_dependency_revision != current_dependency_revision:
        return _analysis_section(
            "unavailable", "lineup_curve_dependency_revision_stale"
        )
    points: list[dict[str, Any]] = []
    usable_horizons: list[int] = []
    for landmark in landmarks:
        try:
            horizon = int(landmark["horizon_minutes"])
            support = int(landmark["support"])
            globally_passed = int(landmark["global_calibration_passed"])
            landmark_created_at = _parse_time(landmark["created_at"])
            input_refs = json.loads(str(landmark["input_refs_json"]))
        except (TypeError, ValueError):
            return _analysis_section("review", "lineup_curve_payload_invalid")
        if (
            str(landmark["validation_status"]) != "passed"
            or globally_passed != 1
            or support < 100
            or landmark["uncertainty"] is None
            or not str(landmark["calibration_ref"] or "").strip()
            or not str(landmark["global_gate_ref"] or "").strip()
            or not isinstance(input_refs, list)
            or not input_refs
            or any(not isinstance(ref, str) or not ref.strip() for ref in input_refs)
        ):
            continue
        if landmark_created_at is None or landmark_created_at > now:
            continue
        try:
            authority = DraftLandmarkAuthority(
                curve_key=str(row["curve_key"]),
                source_ref=f"prospective-draft:{row['curve_key']}",
                landmark_key=str(landmark["landmark_key"]),
                horizon_minutes=horizon,
                target="radiant_win",
                radiant_probability=float(landmark["radiant_probability"]),
                quality=float(landmark["quality"]),
                uncertainty=float(landmark["uncertainty"]),
                support=support,
                radiant_team_side=radiant_team_side,
                strict_mapping_id=mapping_id,
                deployment_key=str(row["deployment_key"]),
                target_snapshot_hash=str(row["target_snapshot_hash"]),
                feature_hash=str(landmark["feature_hash"]),
                model_hash=str(landmark["model_hash"]),
                calibration_hash=str(landmark["calibration_hash"]),
                model_version=str(landmark["model_version"]),
                global_gate_ref=str(landmark["global_gate_ref"]),
                input_snapshot_hash=str(landmark["input_snapshot_hash"]),
                authority_revision=int(revisions[0]),
                dependency_revision=persisted_dependency_revision,
            )
        except (TypeError, ValueError):
            return _analysis_section("review", "lineup_curve_payload_invalid")
        if not draft_landmark_authority_matches(
            connection,
            authority,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            strict_mapping_id=mapping_id,
            radiant_hero_ids=radiant,
            dire_hero_ids=dire,
            observed_at=now,
            require_current_revisions=True,
            verify_curve=False,
        ):
            return _analysis_section("review", "lineup_curve_authority_invalid")
        if horizon * 60 <= game_clock_seconds:
            usable_horizons.append(horizon)
        points.append(
            {
                "landmark_key": str(landmark["landmark_key"]),
                "horizon_minutes": horizon,
                "radiant_probability": float(landmark["radiant_probability"]),
                "quality": float(landmark["quality"]),
                "support": support,
                "uncertainty": (
                    float(landmark["uncertainty"])
                    if landmark["uncertainty"] is not None
                    else None
                ),
                "model_version": str(landmark["model_version"]),
                "validation_status": str(landmark["validation_status"]),
                "conditional": horizon * 60 > game_clock_seconds,
                "active": False,
            }
        )
    if not points:
        return _analysis_section(
            "unavailable", "validated_live_draft_landmark_missing"
        )
    if not usable_horizons:
        return _analysis_section("waiting", "before_first_draft_landmark")
    active_horizon = max(usable_horizons)
    if game_clock_seconds / 60.0 - active_horizon > 10.0:
        return _analysis_section("waiting", "validated_draft_landmark_stale")
    for point in points:
        point["active"] = point["horizon_minutes"] == active_horizon
    return _analysis_section(
        "available",
        "active_curve_available",
        {
            "curve_key": str(row["curve_key"]),
            "first_usable_at": str(row["first_usable_at"]),
            "active_horizon_minutes": active_horizon,
            "points": points,
        },
    )


def _five_hero_ids(value: object) -> tuple[int, ...] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(parsed, list)
        or len(parsed) != 5
        or any(type(hero) is not int or hero <= 0 for hero in parsed)
        or len(set(parsed)) != 5
    ):
        return None
    return tuple(parsed)


def _strategy_analysis(
    connection: PostgresSession,
    raybet_match_id: str,
    _readiness: dict[str, Any],
    *,
    lifecycle: str,
    now: datetime,
) -> dict[str, Any]:
    required_relations = {
        "strategy_decisions": {
            "decision_key",
            "raybet_match_id",
            "map_number",
            "decided_at",
            "underdog_side",
            "market_probability",
            "model_probability",
            "edge",
            "data_quality",
            "eligible",
            "reason",
            "contributions_json",
            "input_ref",
            "strategy_version",
            *(f"draft_{field}" for field in _DRAFT_AUTHORITY_FIELDS),
            *(f"vision_{field}" for field in _VISION_AUTHORITY_FIELDS),
        },
        "vision_derived_invalidations": {"dependent_type", "dependent_key"},
        "strict_live_mapping_impacts": {"dependent_type", "dependent_key"},
        "verified_strategy_decision_vision_authority": {"decision_key"},
        "vision_draft_anchors": {
            "raybet_match_id",
            "map_number",
            "status",
            "conflict_at",
        },
        "vision_draft_conflicts": {"raybet_match_id", "map_number", "captured_at"},
    }
    if any(
        not _relation_has_columns(connection, relation, columns)
        for relation, columns in required_relations.items()
    ):
        return _analysis_section("unavailable", "strategy_schema_missing")
    draft_columns = ", ".join(
        f"decision.draft_{field}" for field in _DRAFT_AUTHORITY_FIELDS
    )
    vision_columns = ", ".join(
        f"decision.vision_{field}" for field in _VISION_AUTHORITY_FIELDS
    )
    vision_invalidated = """EXISTS (
        SELECT 1 FROM vision_derived_invalidations AS invalidation
         WHERE invalidation.dependent_type='strategy_decision'
           AND invalidation.dependent_key=decision.decision_key
    )"""
    mapping_impacted = """EXISTS (
        SELECT 1 FROM strict_live_mapping_impacts AS impact
         WHERE impact.dependent_type='strategy_decision'
           AND impact.dependent_key=decision.decision_key
    )"""
    draft_conflicted = """EXISTS (
        SELECT 1 FROM vision_draft_anchors AS anchor
         WHERE anchor.raybet_match_id=decision.raybet_match_id
           AND anchor.map_number=decision.map_number
           AND anchor.status='conflict'
           AND (
               anchor.conflict_at IS NULL
               OR live_text_timestamp_utc(anchor.conflict_at) IS NULL
               OR live_text_timestamp_utc(anchor.conflict_at)<=
                  live_text_timestamp_utc(decision.decided_at)
               OR EXISTS (
                   SELECT 1 FROM vision_draft_conflicts AS conflict
                    WHERE conflict.raybet_match_id=anchor.raybet_match_id
                      AND conflict.map_number=anchor.map_number
                      AND (
                          live_text_timestamp_utc(conflict.captured_at) IS NULL
                          OR live_text_timestamp_utc(conflict.captured_at)<=
                             live_text_timestamp_utc(decision.decided_at)
                      )
               )
           )
    )"""
    select_candidates = f"""SELECT decision.decision_key,
               decision.raybet_match_id, decision.map_number,
               decision.decided_at, decision.underdog_side,
               decision.market_probability, decision.model_probability,
               decision.edge, decision.data_quality, decision.eligible,
               decision.reason, decision.contributions_json,
               decision.input_ref, decision.strategy_version,
               {draft_columns}, {vision_columns},
               CASE WHEN {vision_invalidated} THEN 1 ELSE 0 END
                   AS _vision_invalidated,
               CASE WHEN {mapping_impacted} THEN 1 ELSE 0 END
                   AS _mapping_impacted,
               CASE WHEN {draft_conflicted} THEN 1 ELSE 0 END
                   AS _draft_conflicted
          FROM strategy_decisions AS decision
         WHERE decision.raybet_match_id=?
           AND live_text_timestamp_utc(decision.decided_at)<=
               CAST(? AS timestamptz)
           AND (
               CAST(? AS text) IS NULL
               OR decision.decided_at < ?
               OR (
                   decision.decided_at = ?
                   AND decision.decision_key < ?
               )
           )
         ORDER BY decision.decided_at DESC, decision.decision_key DESC
         LIMIT ?"""
    excluded = {
        "vision_invalidated": 0,
        "mapping_impacted": 0,
        "draft_conflicted": 0,
        "invalid_payload": 0,
    }
    excluded_decision_count = 0
    decisions_desc: list[dict[str, Any]] = []
    scanned_count = 0
    cursor_at: str | None = None
    cursor_key: str | None = None
    exhausted = False
    try:
        while (
            scanned_count < _MAX_DETAIL_DECISION_SCAN
            and len(decisions_desc) <= _MAX_DETAIL_DECISIONS
        ):
            batch_limit = min(
                _STRATEGY_SCAN_BATCH,
                _MAX_DETAIL_DECISION_SCAN - scanned_count,
            )
            rows = connection.execute(
                select_candidates,
                (
                    raybet_match_id,
                    now.isoformat(),
                    cursor_at,
                    cursor_at,
                    cursor_at,
                    cursor_key,
                    batch_limit,
                ),
            ).fetchall()
            if not rows:
                exhausted = True
                break
            for row in rows:
                scanned_count += 1
                cursor_at = str(row["decided_at"])
                cursor_key = str(row["decision_key"])
                flags = {
                    "vision_invalidated": bool(row["_vision_invalidated"]),
                    "mapping_impacted": bool(row["_mapping_impacted"]),
                    "draft_conflicted": bool(row["_draft_conflicted"]),
                }
                if any(flags.values()):
                    excluded_decision_count += 1
                    for reason, present in flags.items():
                        excluded[reason] += int(present)
                else:
                    payload_failure = persisted_decision_projection_failure(
                        dict(row)
                    )
                    if payload_failure is not None:
                        excluded[payload_failure] = (
                            excluded.get(payload_failure, 0) + 1
                        )
                        excluded_decision_count += 1
                    else:
                        decision = _public_strategy_decision(connection, row)
                    if payload_failure is None and decision is None:
                        excluded["invalid_payload"] += 1
                        excluded_decision_count += 1
                    elif payload_failure is None:
                        decisions_desc.append(decision)
                        if len(decisions_desc) > _MAX_DETAIL_DECISIONS:
                            break
            if len(decisions_desc) > _MAX_DETAIL_DECISIONS:
                break
            if len(rows) < batch_limit:
                exhausted = True
                break
        scan_limit_exceeded = False
        if (
            len(decisions_desc) <= _MAX_DETAIL_DECISIONS
            and scanned_count >= _MAX_DETAIL_DECISION_SCAN
            and not exhausted
        ):
            scan_limit_exceeded = connection.execute(
                select_candidates,
                (
                    raybet_match_id,
                    now.isoformat(),
                    cursor_at,
                    cursor_at,
                    cursor_at,
                    cursor_key,
                    1,
                ),
            ).fetchone() is not None
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return _analysis_section("unavailable", "strategy_schema_missing")
        raise
    has_more = len(decisions_desc) > _MAX_DETAIL_DECISIONS or scan_limit_exceeded
    decisions = list(reversed(decisions_desc[:_MAX_DETAIL_DECISIONS]))
    data = {
        "decisions": decisions,
        "excluded": excluded,
        "excluded_decision_count": excluded_decision_count,
        "displayed_count": len(decisions),
        "scanned_count": scanned_count,
        "has_more": has_more,
        "truncated": has_more,
        "count_scope": "recent_scanned_window",
    }
    if scan_limit_exceeded:
        return _analysis_section("review", "strategy_scan_limit_exceeded", data)
    if decisions:
        return _analysis_section("available", "strategy_available", data)
    if sum(excluded.values()):
        return _analysis_section("review", "strategy_evidence_invalid", data)
    if lifecycle == "ended":
        return _analysis_section("unavailable", "strategy_decision_missing", data)
    return _analysis_section("waiting", "strategy_decision_pending", data)


def _public_strategy_decision(
    connection: PostgresSession,
    row: DatabaseRow,
) -> dict[str, Any] | None:
    base = _strategy_base_values(row)
    if base is None:
        return None
    try:
        payload = parse_decision_payload(
            str(row["contributions_json"]),
            strategy_version=str(row["strategy_version"]),
        )
    except (RecursionError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    contributions = _finite_number_object(
        {
            key: value
            for key, value in payload.items()
            if key not in {"__inputs__", "__conservative__"}
        }
    )
    inputs = payload.get("__inputs__", {})
    if contributions is None or not isinstance(inputs, dict):
        return None
    conservative = _finite_number_object(
        payload.get(
            "__conservative__",
            inputs.get("conservative_contributions", {}),
        )
    )
    if conservative is None:
        return None
    public_inputs = _public_evidence_value(inputs)
    if public_inputs is _INVALID_EVIDENCE:
        return None
    if (
        set(contributions) - _CONTRIBUTION_KEYS
        or set(conservative) - _CONTRIBUTION_KEYS
    ):
        return None
    expected_model_probability = _strategy_probability(
        float(base["market_probability"]),
        contributions,
    )
    expected_conservative_probability = _strategy_probability(
        float(base["market_probability"]),
        conservative,
    )
    independent_positive = _independent_positive(contributions)
    contribution_keys = set(contributions)
    conservative_keys = set(conservative)
    contribution_schema_valid = (
        contribution_keys == conservative_keys
        and frozenset(contribution_keys)
        in (
            _LEGACY_REQUIRED_CONTRIBUTION_KEYS,
            _REQUIRED_CONTRIBUTION_KEYS,
        )
    )
    new_rosh_schema = contribution_keys == _REQUIRED_CONTRIBUTION_KEYS
    scored_payload_valid = (
        contribution_schema_valid
        and (
            not new_rosh_schema
            or (
                contributions["draft_curve"] == 0.0
                and conservative["draft_curve"] == 0.0
            )
        )
        and _valid_conservative_contributions(contributions, conservative)
        and expected_model_probability is not None
        and math.isclose(
            float(base["model_probability"]),
            expected_model_probability,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        and expected_conservative_probability is not None
    )
    eligible = int(base["eligible"]) == 1
    if eligible and (
        base["reason"] != "eligible"
        or not scored_payload_valid
        or not public_inputs
        or not independent_positive
    ):
        return None
    if not eligible and base["reason"] == "eligible":
        return None
    draft_authority = _authority_object(row, "draft", _DRAFT_AUTHORITY_FIELDS)
    vision_authority = _authority_object(row, "vision", _VISION_AUTHORITY_FIELDS)
    if draft_authority and not _valid_draft_authority(draft_authority):
        return None
    if vision_authority and not _valid_vision_authority(
        vision_authority,
        raybet_match_id=str(row["raybet_match_id"]),
        map_number=int(base["map_number"]),
        decided_at=str(base["decided_at"]),
    ):
        return None
    if eligible and (
        len(draft_authority) != len(_DRAFT_AUTHORITY_FIELDS)
        or len(vision_authority) != len(_VISION_AUTHORITY_FIELDS)
        or connection.execute(
            """SELECT 1 FROM verified_strategy_decision_vision_authority
                WHERE decision_key=?""",
            (base["decision_key"],),
        ).fetchone()
        is None
        or not _valid_strategy_inputs(
            connection,
            public_inputs,
            strategy_version=base["strategy_version"],
            raybet_match_id=str(row["raybet_match_id"]),
            map_number=int(base["map_number"]),
            decided_at=str(base["decided_at"]),
            market_probability=float(base["market_probability"]),
            draft_authority=draft_authority,
            vision_authority=vision_authority,
            conservative=conservative,
            expected_conservative_probability=expected_conservative_probability,
            independent_positive=independent_positive,
            require_eligible_gates=True,
        )
    ):
        return None
    if not eligible:
        no_signal = not contributions and not conservative
        if no_signal:
            if (
                draft_authority
                or vision_authority
                or not _valid_no_signal_decision(base, public_inputs)
            ):
                return None
        elif (
            not scored_payload_valid
            or len(draft_authority) != len(_DRAFT_AUTHORITY_FIELDS)
            or len(vision_authority) != len(_VISION_AUTHORITY_FIELDS)
            or not _valid_strategy_inputs(
                connection,
                public_inputs,
                strategy_version=base["strategy_version"],
                raybet_match_id=str(row["raybet_match_id"]),
                map_number=int(base["map_number"]),
                decided_at=str(base["decided_at"]),
                market_probability=float(base["market_probability"]),
                draft_authority=draft_authority,
                vision_authority=vision_authority,
                conservative=conservative,
                expected_conservative_probability=(
                    expected_conservative_probability
                ),
                independent_positive=independent_positive,
                require_eligible_gates=False,
            )
        ):
            return None
    return {
        **base,
        "contributions": contributions,
        "conservative_contributions": conservative,
        "inputs": public_inputs,
        "draft_authority": draft_authority,
        "vision_authority": vision_authority,
    }


def _strategy_base_values(row: DatabaseRow) -> dict[str, Any] | None:
    map_number = row["map_number"]
    eligible = row["eligible"]
    if type(map_number) is not int or map_number <= 0:
        return None
    if type(eligible) is not int or eligible not in {0, 1}:
        return None
    try:
        market_probability = float(row["market_probability"])
        model_probability = float(row["model_probability"])
        edge = float(row["edge"])
        data_quality = float(row["data_quality"])
    except (TypeError, ValueError):
        return None
    if (
        not all(
            math.isfinite(value)
            for value in (market_probability, model_probability, edge, data_quality)
        )
        or not 0.0 <= market_probability <= 1.0
        or not 0.0 <= model_probability <= 1.0
        or not 0.0 <= data_quality <= 1.0
        or not -1.0 <= edge <= 1.0
        or not math.isclose(
            edge,
            model_probability - market_probability,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    ):
        return None
    decided_at = str(row["decided_at"] or "")
    if _parse_time(decided_at) is None:
        return None
    decision_key = str(row["decision_key"] or "")
    input_ref = str(row["input_ref"] or "")
    reason = str(row["reason"] or "")
    strategy_version = str(row["strategy_version"] or "")
    if (
        _DECISION_KEY_RE.fullmatch(decision_key) is None
        or _INPUT_REF_RE.fullmatch(input_ref) is None
        or not _is_opaque_ref(reason)
        or not _is_opaque_ref(strategy_version)
        or not _is_opaque_ref(str(row["raybet_match_id"] or ""))
        or str(row["underdog_side"] or "") not in {"team_one", "team_two"}
    ):
        return None
    return {
        "decision_key": decision_key,
        "map_number": map_number,
        "decided_at": decided_at,
        "underdog_side": str(row["underdog_side"]),
        "market_probability": market_probability,
        "model_probability": model_probability,
        "edge": edge,
        "data_quality": data_quality,
        "eligible": eligible,
        "reason": reason,
        "input_ref": input_ref,
        "strategy_version": strategy_version,
    }


def _finite_number_object(value: object) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool) or not isinstance(
            item, (int, float)
        ):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        result[key] = number
    return result


def _strategy_probability(
    market_probability: float,
    contributions: dict[str, float],
) -> float | None:
    bounded = min(1.0 - 1e-6, max(1e-6, market_probability))
    try:
        score = math.log(bounded / (1.0 - bounded)) + math.fsum(
            contributions.values()
        )
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    if score >= 0.0:
        inverse = math.exp(-score)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(score)
    return exponent / (1.0 + exponent)


def _independent_positive(contributions: dict[str, float]) -> bool:
    lineup_value = contributions.get(
        "lineup_rosh", contributions.get("draft_curve", 0.0)
    )
    return (
        contributions.get("team_style", 0.0)
        + contributions.get("late_game_style", 0.0)
        > 0.0
        or contributions.get("player_form", 0.0) > 0.0
        or lineup_value > 0.0
    )


def _valid_conservative_contributions(
    raw: dict[str, float],
    conservative: dict[str, float],
) -> bool:
    tolerance = 1e-9
    if not math.isclose(
        conservative["market_movement"],
        raw["market_movement"],
        rel_tol=tolerance,
        abs_tol=tolerance,
    ):
        return False
    keys = set(_INDEPENDENT_CONTRIBUTION_KEYS)
    if "lineup_rosh" not in raw:
        keys.remove("lineup_rosh")
        keys.add("draft_curve")
    for key in keys:
        raw_value = raw[key]
        conservative_value = conservative[key]
        if raw_value <= 0.0:
            if not math.isclose(
                conservative_value,
                raw_value,
                rel_tol=tolerance,
                abs_tol=tolerance,
            ):
                return False
        elif (
            conservative_value < -tolerance
            or conservative_value > raw_value + tolerance
        ):
            return False
    return True


def _valid_no_signal_decision(
    base: dict[str, Any],
    inputs: Any,
) -> bool:
    if not isinstance(inputs, dict):
        return False
    market = inputs.get("market")
    vision = inputs.get("vision")
    if not isinstance(market, dict) or not isinstance(vision, dict):
        return False
    market_probability = market.get("underdog_probability")
    market_price = market.get("underdog_price")
    missing_markets = market.get("missing_markets")
    captured_at = _parse_time(vision.get("captured_at"))
    decided_at = _parse_time(base.get("decided_at"))
    game_clock = vision.get("game_clock_seconds")
    radiant_team_side = vision.get("radiant_team_side")
    return (
        math.isclose(
            float(base["model_probability"]),
            float(base["market_probability"]),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        and math.isclose(float(base["edge"]), 0.0, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(
            float(base["data_quality"]), 0.0, rel_tol=0.0, abs_tol=1e-9
        )
        and market.get("underdog_side") == base["underdog_side"]
        and _bounded_number(market_probability, 0.0, 1.0)
        and math.isclose(
            float(market_probability),
            float(base["market_probability"]),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        and _finite_number_greater_than(market_price, 1.0)
        and _bounded_number(market.get("quality"), 0.0, 1.0)
        and isinstance(missing_markets, list)
        and all(
            isinstance(item, str) and _is_opaque_ref(item)
            for item in missing_markets
        )
        and captured_at is not None
        and decided_at is not None
        and captured_at <= decided_at
        and isinstance(vision.get("source_frame_ref"), str)
        and re.fullmatch(
            rf"{re.escape(VISION_FRAME_REF_PREFIX)}[0-9a-f]{{64}}",
            vision["source_frame_ref"],
        )
        is not None
        and (game_clock is None or _nonnegative_int(game_clock))
        and radiant_team_side in {None, "team_one", "team_two"}
    )


def _valid_draft_authority(authority: dict[str, Any]) -> bool:
    digest_fields = (
        "curve_key",
        "landmark_key",
        "deployment_key",
        "target_snapshot_hash",
        "feature_hash",
        "model_hash",
        "calibration_hash",
        "input_snapshot_hash",
    )
    if any(not _is_sha256(authority.get(field)) for field in digest_fields):
        return False
    if authority.get("source_ref") != (
        f"prospective-draft:{authority['curve_key']}"
    ):
        return False
    if (
        authority.get("landmark_horizon_minutes") not in {10, 20, 30, 40, 50}
        or authority.get("landmark_target") != "radiant_win"
        or authority.get("radiant_team_side") not in {"team_one", "team_two"}
        or not _positive_int(authority.get("landmark_support"), minimum=100)
        or not _positive_int(authority.get("strict_mapping_id"))
        or not _positive_int(authority.get("authority_revision"))
        or not _positive_int(authority.get("dependency_revision"))
        or not _is_opaque_ref(authority.get("model_version"))
        or not _is_opaque_ref(authority.get("global_gate_ref"))
    ):
        return False
    return all(
        _bounded_number(authority.get(field), lower, upper)
        for field, lower, upper in (
            ("landmark_radiant_probability", 0.0, 1.0),
            ("landmark_quality", 0.0, 1.0),
            ("landmark_uncertainty", 0.0, 0.5),
        )
    )


def _valid_strategy_inputs(
    connection: PostgresSession,
    inputs: Any,
    *,
    strategy_version: str,
    raybet_match_id: str,
    map_number: int,
    decided_at: str,
    market_probability: float,
    draft_authority: dict[str, Any],
    vision_authority: dict[str, Any],
    conservative: dict[str, float],
    expected_conservative_probability: float,
    independent_positive: bool,
    require_eligible_gates: bool,
) -> bool:
    if not isinstance(inputs, dict):
        return False
    input_draft = inputs.get("draft_authority")
    input_vision = inputs.get("vision")
    strict = inputs.get("strict_live_eligibility")
    transport = inputs.get("transport")
    input_conservative = inputs.get("conservative_contributions")
    conservative_probability = inputs.get("conservative_probability")
    if not _valid_comeback_entry_inputs(
        inputs,
        strategy_version=strategy_version,
        require_eligible_gates=require_eligible_gates,
    ):
        return False
    if "lineup_rosh" in conservative and not _valid_rosh_strategy_input(
        inputs.get("rosh_lineup_score"),
        decided_at=decided_at,
        game_clock_seconds=(
            input_vision.get("game_clock_seconds")
            if isinstance(input_vision, dict)
            else None
        ),
        require_eligible_gates=require_eligible_gates,
    ):
        return False
    if (
        not isinstance(input_draft, dict)
        or not isinstance(input_vision, dict)
        or not isinstance(strict, dict)
        or not isinstance(transport, dict)
        or not isinstance(input_conservative, dict)
        or _finite_number_object(input_conservative) != conservative
        or not _bounded_number(conservative_probability, 0.0, 1.0)
        or not math.isclose(
            float(conservative_probability),
            expected_conservative_probability,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        or inputs.get("independent_positive") is not independent_positive
        or (
            require_eligible_gates
            and float(conservative_probability) <= market_probability
        )
        or (require_eligible_gates and not independent_positive)
    ):
        return False
    decision_time = _parse_time(decided_at)
    if decision_time is None:
        return False
    mapping_refs = strict.get("mapping_refs")
    mapping_id = draft_authority.get("strict_mapping_id")
    if not isinstance(mapping_refs, dict) or not _positive_int(mapping_id):
        return False
    mapping_snapshot = query_strict_mapping_snapshot(
        connection,
        mapping_id=mapping_id,
        observed_at=decision_time,
    )
    if (
        not mapping_snapshot.eligible
        or mapping_snapshot.mapping is None
        or mapping_snapshot.raybet_match_id != raybet_match_id
        or mapping_snapshot.map_number != map_number
        or mapping_refs != mapping_snapshot.mapping.input_refs()
    ):
        return False
    draft_pairs = (
        ("curve_key", "curve_key"),
        ("source_ref", "source_ref"),
        ("landmark_key", "landmark_key"),
        ("horizon_minutes", "landmark_horizon_minutes"),
        ("target", "landmark_target"),
        ("radiant_probability", "landmark_radiant_probability"),
        ("quality", "landmark_quality"),
        ("uncertainty", "landmark_uncertainty"),
        ("support", "landmark_support"),
        ("radiant_team_side", "radiant_team_side"),
        ("strict_mapping_id", "strict_mapping_id"),
        ("deployment_key", "deployment_key"),
        ("target_snapshot_hash", "target_snapshot_hash"),
        ("feature_hash", "feature_hash"),
        ("model_hash", "model_hash"),
        ("calibration_hash", "calibration_hash"),
        ("model_version", "model_version"),
        ("global_gate_ref", "global_gate_ref"),
        ("input_snapshot_hash", "input_snapshot_hash"),
        ("authority_revision", "authority_revision"),
        ("dependency_revision", "dependency_revision"),
    )
    if set(input_draft) != {input_key for input_key, _ in draft_pairs} or any(
        input_draft[input_key] != draft_authority.get(authority_key)
        for input_key, authority_key in draft_pairs
    ):
        return False
    if set(input_vision) != {
        "captured_at",
        "source_frame_ref",
        "game_clock_seconds",
        "radiant_team_side",
    }:
        return False
    transport_at = _parse_time(transport.get("current_at"))
    authority_transport_at = _parse_time(vision_authority.get("transport_at"))
    return (
        input_vision.get("captured_at") == vision_authority.get("captured_at")
        and input_vision.get("source_frame_ref")
        == vision_authority.get("source_frame_ref")
        and input_vision.get("game_clock_seconds")
        == vision_authority.get("aligned_game_clock_seconds")
        and input_vision.get("radiant_team_side")
        == vision_authority.get("radiant_team_side")
        and transport.get("current_key") == vision_authority.get("transport_key")
        and transport_at is not None
        and authority_transport_at is not None
        and transport_at == authority_transport_at == decision_time
    )


def _valid_comeback_entry_inputs(
    inputs: dict[str, Any],
    *,
    strategy_version: str,
    require_eligible_gates: bool,
) -> bool:
    names = {"comeback_state", "entry_window", "comeback_entry"}
    present = names.intersection(inputs)
    if not present:
        return strategy_version in _LEGACY_COMEBACK_STRATEGY_VERSIONS
    if present != names:
        return False
    state = inputs.get("comeback_state")
    window = inputs.get("entry_window")
    entry = inputs.get("comeback_entry")
    vision = inputs.get("vision")
    rosh = inputs.get("rosh_lineup_score")
    market = inputs.get("market")
    if not all(
        isinstance(value, dict)
        for value in (state, window, entry, vision, rosh, market)
    ):
        return False
    assert isinstance(state, dict)
    assert isinstance(window, dict)
    assert isinstance(entry, dict)
    assert isinstance(vision, dict)
    assert isinstance(rosh, dict)
    assert isinstance(market, dict)
    state_keys = {
        "controllable",
        "reason",
        "source_status",
        "source",
        "confidence",
        "underdog_side",
        "underdog_kills",
        "opponent_kills",
        "kill_deficit",
        "underdog_net_worth",
        "opponent_net_worth",
        "net_worth_deficit",
        "net_worth_advantage_side",
        "net_worth_deficit_min",
        "net_worth_deficit_max",
        "unavailable_reason",
    }
    window_keys = {
        "minimum_clock_seconds",
        "maximum_clock_seconds",
        "game_clock_seconds",
        "inside",
    }
    entry_keys = {"eligible", "reason", "rosh_underdog_probability", "policy"}
    policy_keys = {
        "minimum_clock_seconds",
        "maximum_clock_seconds",
        "minimum_kill_deficit",
        "maximum_kill_deficit",
        "minimum_net_worth_deficit",
        "maximum_net_worth_deficit",
        "minimum_vision_confidence",
    }
    policy = entry.get("policy")
    contract = validate_strategy_contract(
        strategy_version,
        inputs.get("strategy_contract"),
    )
    if contract is not None:
        expected_policy = asdict(contract.policy.entry)
    elif strategy_version == STRATEGY_VERSION:
        expected_policy = _DEFAULT_COMEBACK_ENTRY_POLICY
    elif strategy_version in _LEGACY_COMEBACK_STRATEGY_VERSIONS:
        expected_policy = None
    else:
        return False
    if (
        set(state) != state_keys
        or set(window) != window_keys
        or set(entry) != entry_keys
        or not isinstance(policy, dict)
        or set(policy) != policy_keys
        or (expected_policy is not None and policy != expected_policy)
    ):
        return False
    assert isinstance(policy, dict)
    minimum_clock = policy.get("minimum_clock_seconds")
    maximum_clock = policy.get("maximum_clock_seconds")
    minimum_kills = policy.get("minimum_kill_deficit")
    maximum_kills = policy.get("maximum_kill_deficit")
    minimum_net_worth = policy.get("minimum_net_worth_deficit")
    maximum_net_worth = policy.get("maximum_net_worth_deficit")
    minimum_confidence = policy.get("minimum_vision_confidence")
    if (
        any(
            not _nonnegative_int(value)
            for value in (
                minimum_clock,
                maximum_clock,
                minimum_kills,
                maximum_kills,
                minimum_net_worth,
                maximum_net_worth,
            )
        )
        or int(minimum_clock) > int(maximum_clock)
        or int(minimum_kills) > int(maximum_kills)
        or int(minimum_net_worth) > int(maximum_net_worth)
        or not _bounded_number(minimum_confidence, 0.0, 1.0)
    ):
        return False
    game_clock = window.get("game_clock_seconds")
    expected_inside = (
        _nonnegative_int(game_clock)
        and int(minimum_clock) <= int(game_clock) <= int(maximum_clock)
    )
    if (
        window.get("minimum_clock_seconds") != minimum_clock
        or window.get("maximum_clock_seconds") != maximum_clock
        or type(window.get("inside")) is not bool
        or window.get("inside") is not expected_inside
        or vision.get("game_clock_seconds") != game_clock
    ):
        return False

    controllable = state.get("controllable")
    confidence = state.get("confidence")
    underdog_side = state.get("underdog_side")
    if (
        type(controllable) is not bool
        or not _is_opaque_ref(state.get("reason"))
        or not _is_opaque_ref(state.get("source_status"))
        or (
            state.get("source") is not None
            and not _is_opaque_ref(state.get("source"))
        )
        or not _bounded_number(confidence, 0.0, 1.0)
        or underdog_side not in {"team_one", "team_two"}
        or market.get("underdog_side") != underdog_side
        or (
            state.get("unavailable_reason") is not None
            and not _is_opaque_ref(state.get("unavailable_reason"))
        )
    ):
        return False
    underdog_kills = state.get("underdog_kills")
    opponent_kills = state.get("opponent_kills")
    kill_deficit = state.get("kill_deficit")
    kills_available = all(
        _nonnegative_int(value) for value in (underdog_kills, opponent_kills)
    ) and type(kill_deficit) is int
    if kills_available:
        if int(kill_deficit) != int(opponent_kills) - int(underdog_kills):
            return False
    elif any(value is not None for value in (underdog_kills, opponent_kills, kill_deficit)):
        return False
    underdog_net_worth = state.get("underdog_net_worth")
    opponent_net_worth = state.get("opponent_net_worth")
    net_worth_deficit = state.get("net_worth_deficit")
    net_worth_available = all(
        _nonnegative_int(value)
        for value in (underdog_net_worth, opponent_net_worth)
    ) and type(net_worth_deficit) is int
    if net_worth_available:
        return False
    elif any(
        value is not None
        for value in (underdog_net_worth, opponent_net_worth, net_worth_deficit)
    ):
        return False
    advantage_side = state.get("net_worth_advantage_side")
    net_worth_deficit_min = state.get("net_worth_deficit_min")
    net_worth_deficit_max = state.get("net_worth_deficit_max")
    range_available = (
        type(net_worth_deficit_min) is int
        and type(net_worth_deficit_max) is int
        and int(net_worth_deficit_min) <= int(net_worth_deficit_max)
    )
    if net_worth_available:
        if (
            advantage_side is not None
            or not range_available
            or net_worth_deficit_min != net_worth_deficit
            or net_worth_deficit_max != net_worth_deficit
        ):
            return False
    elif advantage_side is not None:
        radiant_team_side = vision.get("radiant_team_side")
        if (
            advantage_side not in {"radiant", "dire"}
            or not range_available
            or radiant_team_side not in {"team_one", "team_two"}
        ):
            return False
        leader_is_underdog = (
            advantage_side == "radiant"
        ) is (underdog_side == radiant_team_side)
        raw_advantage_min, raw_advantage_max = (
            (-int(net_worth_deficit_max), -int(net_worth_deficit_min))
            if leader_is_underdog
            else (int(net_worth_deficit_min), int(net_worth_deficit_max))
        )
        if not is_canonical_net_worth_bucket(
            raw_advantage_min,
            raw_advantage_max,
        ):
            return False
        if leader_is_underdog:
            if int(net_worth_deficit_max) > 0:
                return False
        elif int(net_worth_deficit_min) < 0:
            return False
    elif range_available or any(
        value is not None
        for value in (net_worth_deficit_min, net_worth_deficit_max)
    ):
        return False
    economy_available = net_worth_available or advantage_side is not None
    if kills_available:
        if (
            state.get("source_status") != "available"
            or state.get("source") != "vision_hud"
            or state.get("unavailable_reason") is not None
            or float(confidence) < float(minimum_confidence)
        ):
            return False
        collapsed = int(kill_deficit) > int(maximum_kills) or (
            economy_available
            and int(net_worth_deficit_max) > int(maximum_net_worth)
        )
        not_material = int(kill_deficit) < int(minimum_kills) or (
            economy_available
            and int(net_worth_deficit_min) < int(minimum_net_worth)
        )
        if not economy_available:
            expected_state_reason = "vision_net_worth_evidence_missing"
        elif collapsed:
            expected_state_reason = "vision_situation_collapsed"
        elif not_material:
            expected_state_reason = "underdog_deficit_not_material"
        else:
            expected_state_reason = "controlled_deficit"
        expected_controllable = expected_state_reason == "controlled_deficit"
        if (
            state.get("reason") != expected_state_reason
            or controllable is not expected_controllable
        ):
            return False
    elif (
        economy_available
        or controllable
        or state.get("reason")
        in {
            "controlled_deficit",
            "vision_situation_collapsed",
            "underdog_deficit_not_material",
        }
    ):
        return False

    selected_score = rosh.get("selected_score")
    expected_rosh_probability: float | None = None
    radiant_team_side = vision.get("radiant_team_side")
    if selected_score is not None:
        if (
            _finite_number_object({"score": selected_score}) is None
            or radiant_team_side not in {"team_one", "team_two"}
        ):
            return False
        radiant_probability = min(
            1.0 - 1e-6,
            max(1e-6, (50.0 + float(selected_score)) / 100.0),
        )
        expected_rosh_probability = (
            radiant_probability
            if underdog_side == radiant_team_side
            else 1.0 - radiant_probability
        )
    persisted_rosh_probability = entry.get("rosh_underdog_probability")
    if expected_rosh_probability is None:
        if persisted_rosh_probability is not None:
            return False
    elif (
        not _bounded_number(persisted_rosh_probability, 0.0, 1.0)
        or not math.isclose(
            float(persisted_rosh_probability),
            expected_rosh_probability,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    ):
        return False
    expected_reason = str(state["reason"])
    if controllable:
        if not expected_inside:
            expected_reason = "comeback_entry_outside_time_window"
        elif expected_rosh_probability is None:
            expected_reason = "rosh_direction_unavailable"
        elif expected_rosh_probability <= 0.5:
            expected_reason = "rosh_direction_opposes_underdog"
        else:
            expected_reason = "eligible"
    expected_eligible = expected_reason == "eligible"
    return (
        type(entry.get("eligible")) is bool
        and entry.get("eligible") is expected_eligible
        and entry.get("reason") == expected_reason
        and (not require_eligible_gates or expected_eligible)
    )


def _valid_rosh_strategy_input(
    value: object,
    *,
    decided_at: str,
    game_clock_seconds: object,
    require_eligible_gates: bool,
) -> bool:
    if not isinstance(value, dict):
        return False
    actual_stake = value.get("actual_stake_multiplier")
    compatibility_stake = value.get("stake_multiplier")
    if value.get("status") == "unavailable":
        return (
            not require_eligible_gates
            and value.get("draft_matches_observation") is False
            and actual_stake == 0.0
            and compatibility_stake == 0.0
            and value.get("selected_score") is None
        )
    mode = value.get("mode")
    stake_cap = value.get("stake_cap")
    coverage = value.get("player_coverage")
    coverage_count = value.get("player_coverage_count")
    draft_matches = value.get("draft_matches_observation")
    selected_table = value.get("selected_table")
    selected_minute = value.get("selected_minute")
    selected_score = value.get("selected_score")
    match_percentage = value.get("match_percentage")
    source_as_of = _parse_time(value.get("source_as_of"))
    decision_time = _parse_time(decided_at)
    if (
        mode not in {"pure", "player_adjusted"}
        or type(draft_matches) is not bool
        or value.get("formula_version") != ROSH_FORMULA_VERSION
        or any(
            not _is_sha256(value.get(field))
            for field in (
                "score_key",
                "draft_hash",
                "player_identity_hash",
                "evidence_hash",
            )
        )
        or not _bounded_number(coverage, 0.0, 1.0)
        or not _nonnegative_int(coverage_count)
        or int(coverage_count) > 10
        or not math.isclose(
            float(coverage), int(coverage_count) / 10.0, abs_tol=1e-9
        )
        or not _bounded_number(stake_cap, 0.0, 1.0)
        or not _bounded_number(actual_stake, 0.0, 1.0)
        or compatibility_stake != actual_stake
        or source_as_of is None
        or decision_time is None
        or source_as_of > decision_time
        or not _is_opaque_ref(value.get("source_name"))
        or not _positive_int(value.get("source_week"))
        or not _positive_int(value.get("cache_week_start"))
        or _finite_number_object({"value": value.get("pure_score")}) is None
        or _finite_number_object({"value": value.get("effective_score")}) is None
    ):
        return False
    adjusted = value.get("player_adjusted_score")
    if mode == "player_adjusted":
        mode_valid = (
            int(coverage_count) == 10
            and _finite_number_object({"value": adjusted}) is not None
            and math.isclose(float(stake_cap), 1.0, abs_tol=1e-9)
        )
        expected_table = "minute_table"
    else:
        mode_valid = (
            int(coverage_count) < 10
            and adjusted is None
            and math.isclose(float(stake_cap), 0.5, abs_tol=1e-9)
        )
        expected_table = "pure_minute_table"
    if not mode_valid:
        return False

    if selected_score is None:
        return (
            not require_eligible_gates
            and actual_stake == 0.0
            and compatibility_stake == 0.0
            and selected_table is None
            and selected_minute is None
            and match_percentage is None
        )

    if (
        draft_matches is not True
        or _finite_number_object({"value": selected_score}) is None
        or not _positive_int(selected_minute)
        or not 20 <= int(selected_minute) <= 60
        or not _nonnegative_int(game_clock_seconds)
        or int(selected_minute) > int(game_clock_seconds) // 60
        or selected_table != expected_table
        or not _bounded_number(match_percentage, 0.0, 100.0)
    ):
        return False
    if mode == "player_adjusted":
        return (
            math.isclose(float(actual_stake), 1.0, abs_tol=1e-9)
        )
    return (
        0.1 <= float(actual_stake) <= 0.5
    )


def _valid_vision_authority(
    authority: dict[str, Any],
    *,
    raybet_match_id: str,
    map_number: int,
    decided_at: str,
) -> bool:
    frame_sha256 = authority.get("source_frame_sha256")
    frame_ref = authority.get("source_frame_ref")
    radiant = _five_hero_ids(authority.get("radiant_hero_ids_json"))
    dire = _five_hero_ids(authority.get("dire_hero_ids_json"))
    captured_at = _parse_time(authority.get("captured_at"))
    transport_at = _parse_time(authority.get("transport_at"))
    decision_time = _parse_time(decided_at)
    lag_seconds = authority.get("alignment_lag_seconds")
    observed_clock = authority.get("observed_game_clock_seconds")
    aligned_clock = authority.get("aligned_game_clock_seconds")
    if (
        authority.get("raybet_match_id") != raybet_match_id
        or authority.get("map_number") != map_number
        or not _is_sha256(frame_sha256)
        or frame_ref != f"{VISION_FRAME_REF_PREFIX}{frame_sha256}"
        or not _positive_int(authority.get("source_frame_bytes"))
        or not _nonnegative_int(authority.get("observed_game_clock_seconds"))
        or not _nonnegative_int(authority.get("aligned_game_clock_seconds"))
        or authority.get("is_paused") != 0
        or radiant is None
        or dire is None
        or len(set(radiant + dire)) != 10
        or authority.get("radiant_team_side") not in {"team_one", "team_two"}
        or authority.get("screen_state") != "game"
        or authority.get("confirmed") != 1
        or not _is_opaque_ref(authority.get("transport_key"))
        or authority.get("alignment_method") not in {"anchor", "forward_projection"}
        or captured_at is None
        or transport_at is None
        or decision_time is None
        or transport_at != decision_time
        or captured_at > transport_at
        or not _bounded_number(lag_seconds, 0.0, 15.0)
        or not math.isclose(
            float(lag_seconds),
            (transport_at - captured_at).total_seconds(),
            rel_tol=0.0,
            abs_tol=0.001,
        )
        or aligned_clock != int(int(observed_clock) + float(lag_seconds))
        or authority.get("alignment_method")
        != ("forward_projection" if float(lag_seconds) >= 1.0 else "anchor")
    ):
        return False
    return all(
        _bounded_number(authority.get(field), lower, upper)
        for field, lower, upper in (
            ("clock_confidence", 0.0, 1.0),
            ("draft_confidence", 0.0, 1.0),
            ("alignment_lag_seconds", 0.0, 15.0),
        )
    )


def _authority_object(
    row: DatabaseRow,
    prefix: str,
    fields: tuple[str, ...],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        value = row[f"{prefix}_{field}"]
        if value is None:
            continue
        if field.endswith("_json"):
            try:
                value = json.loads(str(value))
            except (TypeError, ValueError):
                return {}
        result[field] = value
    return result


def _public_evidence_value(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    if budget is None:
        budget = [_MAX_PUBLIC_EVIDENCE_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > _MAX_PUBLIC_EVIDENCE_DEPTH:
        return _INVALID_EVIDENCE
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _safe_evidence_key(key):
                return _INVALID_EVIDENCE
            lowered = key.casefold()
            if lowered.endswith("_path") or lowered in {
                "storage_path",
                "raw_json",
                "raw_payload",
            }:
                continue
            if not _valid_nested_reference(lowered, item):
                return _INVALID_EVIDENCE
            public = _public_evidence_value(
                item,
                depth=depth + 1,
                budget=budget,
            )
            if public is _INVALID_EVIDENCE:
                return _INVALID_EVIDENCE
            result[key] = public
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            public = _public_evidence_value(
                item,
                depth=depth + 1,
                budget=budget,
            )
            if public is _INVALID_EVIDENCE:
                return _INVALID_EVIDENCE
            result.append(public)
        return result
    if isinstance(value, float):
        return value if math.isfinite(value) else _INVALID_EVIDENCE
    if isinstance(value, str):
        return (
            value
            if len(value) <= _MAX_PUBLIC_EVIDENCE_STRING
            else _INVALID_EVIDENCE
        )
    if value is None or isinstance(value, (int, bool)):
        return value
    return _INVALID_EVIDENCE


def _valid_nested_reference(key: str, value: Any) -> bool:
    if value is None or not isinstance(value, str):
        return True
    if key == "source_frame_ref":
        return bool(
            re.fullmatch(
                rf"{re.escape(VISION_FRAME_REF_PREFIX)}[0-9a-f]{{64}}",
                value,
            )
        )
    if key.endswith("_hash") or key in {
        "curve_key",
        "landmark_key",
        "deployment_key",
    }:
        return _is_sha256(value)
    if key.endswith("_ref") or key.endswith("_key"):
        return _is_opaque_ref(value)
    return True


def _safe_evidence_key(value: str) -> bool:
    return (
        0 < len(value) <= 128
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _is_opaque_ref(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_REF_RE.fullmatch(value) is not None


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _positive_int(value: object, *, minimum: int = 1) -> bool:
    return type(value) is int and value >= minimum


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _bounded_number(value: object, lower: float, upper: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and lower <= float(value) <= upper
    )


def _finite_number_greater_than(value: object, lower: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > lower
    )


def _latest_strategy_decision(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime,
) -> DatabaseRow | None:
    required_relations = {
        "strategy_decisions": {
            "decision_key",
            "raybet_match_id",
            "map_number",
            "decided_at",
            "model_probability",
            "market_probability",
            "edge",
            "eligible",
            "reason",
            "strategy_version",
        },
        "vision_derived_invalidations": {"dependent_type", "dependent_key"},
        "strict_live_mapping_impacts": {"dependent_type", "dependent_key"},
        "vision_draft_anchors": {
            "raybet_match_id",
            "map_number",
            "status",
            "conflict_at",
        },
        "vision_draft_conflicts": {"raybet_match_id", "map_number", "captured_at"},
    }
    if any(
        not _relation_has_columns(connection, relation, columns)
        for relation, columns in required_relations.items()
    ):
        return None
    try:
        return connection.execute(
            """SELECT decided_at AS observed_at, map_number, model_probability,
                      market_probability, edge, eligible, reason, strategy_version
                 FROM strategy_decisions AS decision
                WHERE decision.raybet_match_id=?
                  AND live_text_timestamp_utc(decision.decided_at)<=
                      CAST(? AS timestamptz)
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
                               OR live_text_timestamp_utc(
                                      anchor.conflict_at
                                  ) IS NULL
                               OR live_text_timestamp_utc(
                                      decision.decided_at
                                  ) IS NULL
                               OR live_text_timestamp_utc(anchor.conflict_at)<=
                                  live_text_timestamp_utc(decision.decided_at)
                               OR EXISTS (
                                    SELECT 1
                                      FROM vision_draft_conflicts AS conflict
                                     WHERE conflict.raybet_match_id=
                                           anchor.raybet_match_id
                                       AND conflict.map_number=anchor.map_number
                                       AND (
                                             live_text_timestamp_utc(
                                                 conflict.captured_at
                                             ) IS NULL
                                             OR live_text_timestamp_utc(
                                                    conflict.captured_at
                                                )<=live_text_timestamp_utc(
                                                    decision.decided_at
                                                )
                                       )
                               )
                         )
                  )
                ORDER BY decision.decided_at DESC LIMIT 1""",
            (raybet_match_id, now.isoformat()),
        ).fetchone()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return None
        raise


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
