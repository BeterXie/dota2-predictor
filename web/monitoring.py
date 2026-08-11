"""Read-only projections for the local live-monitoring console."""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import math
import os
import re
import secrets
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote

from contracts.live_observation import MAP_START_EVIDENCE_WINDOW_SECONDS
from live_betting.live_match_state import (
    latest_live_draft_mapping,
    live_draft_context,
    live_game_snapshots,
)
from live_betting.raybet import DOTA2_GAME_ID, parse_raybet_map_final
from live_betting.map_decision_checkpoints import latest_map_checkpoints
from live_betting.official_map_identity import (
    ExactOfficialMapLink,
    resolve_exact_official_map_links,
)
from live_betting.raybet_state import (
    explicit_raybet_map_times,
    infer_current_map_number,
    raybet_match_is_live,
    raybet_odds_is_open,
)
from live_betting.sanitize import stored_public_stream_url
from live_betting.vision import VisionObservation, parse_observation
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
from event_intelligence.raw_registry import verify_registered_raw_source_artifact
from fetch.stratz_detail import StratzDetailError, stratz_player_positions

from .alerts import reconcile_alerts
from .match_identity import match_display_name, observation_file_name


_LOCAL_TIMEZONE = timezone(timedelta(hours=8))
_OPEN_MATCH_STATUSES = {"1", "2", "open", "active", "running"}
_ENDED_MATCH_STATUSES = {"3", "5", "ended", "finished", "settled", "closed"}
_ENDED_STATUS_SQL = (
    "lower(status) IN ('3', '5', 'closed', 'ended', 'finished', 'settled')"
)
_UPCOMING_MATCH_STATUSES = {"1", "upcoming", "scheduled", "not_started"}
_VISION_PREMATCH_WATCH_WINDOW = timedelta(minutes=30)
_HISTORY_SCHEDULE_GRACE = timedelta(hours=12)
_PREMATCH_HISTORY_SCHEDULE_GRACE = timedelta(hours=4)
_HISTORY_ACTIVITY_GRACE = timedelta(minutes=15)
_TIMESTAMP_ROUNDING_GRACE = timedelta(milliseconds=1)
_EXPECTED_HEALTH_COMPONENTS = {
    "raybet_worker": 45.0,
    "strict_ingest_worker": 90.0,
}
_PRIMARY_HEALTH_COMPONENTS = {
    "raybet_worker",
    "raybet_priority_odds_worker",
    "raybet_full_odds_worker",
    "strict_ingest_worker",
    "postmatch_worker",
    "map_decision_worker",
    "vision_worker",
}
_RETIRED_HEALTH_COMPONENTS = {
    "companion",
    "database",
    "draft_publisher",
    "draft_publisher_worker",
    "historical_rosh",
    "historical_rosh_worker",
    "mail",
    "mail_worker",
    "mail_delivery",
    "postmatch",
    "raybet",
    "shadow",
    "shadow_worker",
    "strict_ingest",
    "vision",
}
_RAYBET_PAGE_ORIGINS = frozenset(
    {"https://ray086.com", "https://www.ray086.com"}
)
_RAYBET_PAGE_PREFIXES = ("/sports/esports", "/esports", "/dota2")
_RAYBET_PAGE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
_MAX_VISION_TIMELINE_POINTS = 5_000
_JSONL_REVERSE_CHUNK_BYTES = 1_048_576
_ODDS_GAP_THRESHOLD_SECONDS = 150.0
_MAP_PERIOD_PATTERN = re.compile(r"^map_([1-5])$")
_DEFAULT_VISION_OBSERVATION_DIR = (
    Path("data") / "live_betting" / "vision_observations"
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
        reported_limit = details.get("stale_after_seconds")
        limit = (
            float(reported_limit)
            if isinstance(reported_limit, (int, float)) and reported_limit > 0
            else _EXPECTED_HEALTH_COMPONENTS.get(component, 120.0)
        )
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
    alerts = reconcile_alerts(connection, now=checked_at, health=health)
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
            "status": statuses.get("raybet_worker", "stopped"),
        },
        "opendota_event_ingest": {
            "required": True,
            "status": statuses.get("strict_ingest_worker", "stopped"),
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
    if not connection.in_transaction:
        with connection.transaction():
            return monitor_match_detail(
                connection,
                raybet_match_id,
                now=checked_at,
                max_points=max_points,
            )
    row = connection.execute(
        """SELECT raybet_match_id, tournament, team_one, team_two,
                  scheduled_at, best_of, status, live_url, raw_json, updated_at
             FROM raybet_matches WHERE raybet_match_id=?""",
        (raybet_match_id,),
    ).fetchone()
    if row is None or not is_head_to_head_match_row(row):
        return None
    summary = _monitor_match(connection, row, checked_at)
    maximum_live_map = (
        int(summary["current_map_number"])
        if summary["lifecycle"] in {"live", "degraded"}
        and type(summary.get("current_map_number")) is int
        else None
    )
    collection_timeline = winner_timeline(
        connection,
        raybet_match_id,
        max_points=None,
        as_of=checked_at,
        deduplicate_ended=False,
    )
    prematch_timeline = winner_timeline(
        connection,
        raybet_match_id,
        max_points=None,
        as_of=checked_at,
        processing_status="audit_only",
        deduplicate_ended=False,
    )
    latest_capture = _latest_capture_row(
        connection,
        raybet_match_id,
        now=checked_at,
        maximum_map_number=maximum_live_map,
    )
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
    if maximum_live_map is not None:
        game_snapshots = [
            snapshot
            for snapshot in game_snapshots
            if type(snapshot.get("map_number")) is int
            and int(snapshot["map_number"]) <= maximum_live_map
        ]
    vision = _vision_timeline(
        connection,
        raybet_match_id,
        now=checked_at,
        max_points=min(
            _MAX_VISION_TIMELINE_POINTS,
            max_points * max(1, int(row["best_of"] or 1)),
        ),
        maximum_map_number=maximum_live_map,
    )
    latest_capture_point = (
        _capture_point(latest_capture, raybet_match_id)
        if latest_capture is not None
        else None
    )
    latest_huds = _latest_hud_observations(
        raybet_match_id,
        now=checked_at,
        valid_vision_points=vision,
        maximum_map_number=maximum_live_map,
    )
    vision_runtime = _vision_runtime_status(
        connection,
        raybet_match_id,
        now=checked_at,
    )
    if (
        maximum_live_map is not None
        and vision_runtime is not None
        and type(vision_runtime.get("map_number")) is int
        and int(vision_runtime["map_number"]) > maximum_live_map
    ):
        vision_runtime = None
    markets = current_markets(connection, raybet_match_id, as_of=checked_at)
    postmatch = _postmatch_detail(connection, raybet_match_id)
    raybet_final_map_numbers = _confirmed_raybet_final_map_numbers(row)
    games, market_evidence = _series_game_details(
        connection,
        summary=summary,
        prematch_timeline=prematch_timeline,
        collection_timeline=collection_timeline,
        vision=vision,
        latest_capture=latest_capture_point,
        game_snapshots=game_snapshots,
        latest_huds=latest_huds,
        vision_runtime=vision_runtime,
        markets=markets,
        postmatch=postmatch,
        raybet_final_map_numbers=raybet_final_map_numbers,
        max_points=max_points,
    )
    return {
        **summary,
        "draft_context": draft_context,
        "postmatch": postmatch,
        "games": games,
        "market_evidence": market_evidence,
    }


def _postmatch_detail(
    connection: PostgresSession,
    raybet_match_id: str,
) -> dict[str, Any]:
    """Project exact OpenDota map links without mixing provider authority."""

    linked_rows = connection.execute(
        """SELECT result.map_number, result.dota_match_id,
                  result.winner_side, result.team_one_kills,
                  result.team_two_kills, result.duration_seconds,
                  result.settled_at, match.radiant_team_id,
                  match.dire_team_id, match.radiant_win, match.duration,
                  match.start_time, match.leagueid, match.radiant_score,
                  match.dire_score, match.fetched_at,
                  radiant.name AS radiant_team_name,
                  dire.name AS dire_team_name, league.name AS league_name
             FROM map_results AS result
             LEFT JOIN matches AS match ON match.match_id=result.dota_match_id
             LEFT JOIN teams AS radiant ON radiant.team_id=match.radiant_team_id
             LEFT JOIN teams AS dire ON dire.team_id=match.dire_team_id
             LEFT JOIN leagues AS league ON league.leagueid=match.leagueid
            WHERE result.raybet_match_id=?
            ORDER BY result.map_number""",
        (raybet_match_id,),
    ).fetchall()
    reconciliation_rows = connection.execute(
        """SELECT map_number, status, reason, dota_match_id, updated_at
             FROM settlement_reconciliations
            WHERE raybet_match_id=?
            ORDER BY map_number""",
        (raybet_match_id,),
    ).fetchall()

    exact_resolution = None
    exact_evidence: dict[int, dict[str, object]] = {}
    if not linked_rows and not reconciliation_rows:
        exact_resolution = resolve_exact_official_map_links(
            connection,
            raybet_match_id,
        )
        if exact_resolution.status == "confirmed":
            linked_rows = _resolved_official_postmatch_rows(
                connection,
                exact_resolution.links,
            )
            exact_evidence = {
                link.map_number: link.evidence()
                for link in exact_resolution.links
            }
    identity_source = (
        "map_results"
        if linked_rows and not exact_evidence
        else "raybet_explicit_map_time"
        if linked_rows
        else "waiting"
    )

    games = []
    for linked_row in linked_rows:
        if linked_row["dota_match_id"] is None:
            continue
        game = _postmatch_game(connection, linked_row)
        map_number = int(game["map_number"])
        evidence = exact_evidence.get(map_number)
        game["identity_reason"] = (
            "raybet_explicit_map_time_unique"
            if evidence is not None
            else "confirmed_map_result"
        )
        game["identity_evidence"] = evidence or {
            "method": "confirmed_settlement_reconciliation",
            "official_source": "confirmed_map_result",
        }
        games.append(game)
    unresolved = [
        {
            "map_number": int(row["map_number"]),
            "status": str(row["status"]),
            "reason": str(row["reason"] or ""),
            "official_match_id": (
                str(row["dota_match_id"])
                if row["dota_match_id"] is not None
                else None
            ),
            "updated_at": row["updated_at"],
        }
        for row in reconciliation_rows
        if str(row["status"]) != "confirmed"
    ]
    if (
        not unresolved
        and not games
        and exact_resolution is not None
        and exact_resolution.map_numbers
    ):
        unresolved = [
            {
                "map_number": map_number,
                "status": "unlinked",
                "reason": exact_resolution.reason,
                "official_match_id": None,
                "updated_at": None,
            }
            for map_number in exact_resolution.map_numbers
        ]
    has_review = any(row["status"] == "manual_review" for row in unresolved)
    has_ingested_game = any(game["status"] == "available" for game in games)
    enrichment_statuses = {
        str(game["enrichment"]["status"])
        for game in games
        if isinstance(game.get("enrichment"), dict)
    }
    if "available" in enrichment_statuses:
        stratz_status, stratz_reason = "available", "player_positions_available"
    elif "blocked" in enrichment_statuses:
        blocker_reasons = {
            str(game["enrichment"].get("reason") or "optional_enrichment_source_blocked")
            for game in games
            if isinstance(game.get("enrichment"), dict)
            and game["enrichment"].get("status") == "blocked"
        }
        stratz_status = "blocked"
        stratz_reason = (
            next(iter(blocker_reasons))
            if len(blocker_reasons) == 1
            else "optional_enrichment_source_blocked"
        )
    elif "invalid" in enrichment_statuses:
        stratz_status, stratz_reason = "invalid", "optional_enrichment_invalid"
    elif "partial" in enrichment_statuses:
        stratz_status, stratz_reason = "partial", "player_positions_missing"
    else:
        stratz_status, stratz_reason = (
            "not_available",
            "optional_enrichment_not_ingested",
        )
    if games and unresolved:
        status, reason = "partial", "some_maps_unresolved"
    elif games:
        status, reason = (
            "available",
            "exact_opendota_maps_available",
        )
    elif has_review:
        status, reason = "review", "postmatch_identity_requires_review"
    else:
        status, reason = "waiting", "exact_opendota_map_not_available"

    opendota_status = (
        "available"
        if has_ingested_game
        else "linked_not_ingested"
        if games
        else "waiting_for_exact_link"
    )
    return {
        "status": status,
        "reason": reason,
        "identity_source": identity_source,
        "sources": {
            "canonical": {
                "provider": "opendota",
                "role": "canonical_postmatch",
                "status": opendota_status,
                "reason": (
                    "exact_map_details_available"
                    if has_ingested_game
                    else "exact_map_detail_not_ingested"
                    if games
                    else "exact_map_link_not_confirmed"
                ),
            },
            "enhancement": {
                "provider": "stratz",
                "role": "optional_enrichment",
                "status": stratz_status,
                "reason": stratz_reason,
            },
        },
        "games": games,
        "unresolved_maps": unresolved,
    }


def _resolved_official_postmatch_rows(
    connection: PostgresSession,
    links: tuple[ExactOfficialMapLink, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for link in links:
        row = connection.execute(
            """SELECT ? AS map_number, match.match_id AS dota_match_id,
                      match.radiant_team_id, match.dire_team_id,
                      match.radiant_win, match.duration, match.start_time,
                      match.leagueid, match.radiant_score, match.dire_score,
                      match.fetched_at, radiant.name AS radiant_team_name,
                      dire.name AS dire_team_name, league.name AS league_name
                 FROM matches AS match
                 LEFT JOIN teams AS radiant
                   ON radiant.team_id=match.radiant_team_id
                 LEFT JOIN teams AS dire ON dire.team_id=match.dire_team_id
                 LEFT JOIN leagues AS league ON league.leagueid=match.leagueid
                WHERE match.match_id=? AND match.series_id=?
                  AND match.leagueid=?""",
            (
                link.map_number,
                link.dota_match_id,
                link.official_series_id,
                link.league_id,
            ),
        ).fetchone()
        if row is None:
            return []
        rows.append(dict(row))
    return rows


def _postmatch_game(
    connection: PostgresSession,
    row: DatabaseRow,
) -> dict[str, Any]:
    match_id = int(row["dota_match_id"])
    stratz_positions, stratz_enrichment = _stratz_enrichment(
        connection,
        match_id,
    )
    player_rows = connection.execute(
        """SELECT player.player_slot, player.account_id, player.is_radiant,
                  player.team_id, player.hero_id, hero.localized_name AS hero_name,
                  hero.hero_key, player.kills, player.deaths, player.assists,
                  player.gold_per_min, player.xp_per_min, player.net_worth,
                  player.last_hits, player.denies, player.hero_damage,
                  player.hero_healing, player.tower_damage, player.level,
                  player.item_0, player.item_1,
                  player.item_2, player.item_3, player.item_4, player.item_5
             FROM match_players AS player
             LEFT JOIN heroes AS hero ON hero.hero_id=player.hero_id
            WHERE player.match_id=?
            ORDER BY player.player_slot""",
        (match_id,),
    ).fetchall()
    player_identities = _opendota_player_identities(connection, match_id)
    historical_averages = _player_historical_averages(
        connection,
        player_rows,
        before_start_time=row["start_time"],
    )
    players = [
        _postmatch_player(
            player,
            stratz_positions,
            player_identities,
            historical_averages,
        )
        for player in player_rows
        if player["player_slot"] is not None and player["hero_id"] is not None
    ]
    draft = [
        {
            "order": int(action["ord"]),
            "is_pick": bool(action["is_pick"]),
            "side": "radiant" if int(action["team"]) == 0 else "dire",
            "hero_id": int(action["hero_id"]),
            "hero_name": str(action["hero_name"] or f"Hero {action['hero_id']}"),
            "hero_key": str(action["hero_key"] or ""),
        }
        for action in connection.execute(
            """SELECT draft.ord, draft.is_pick, draft.team, draft.hero_id,
                      hero.localized_name AS hero_name, hero.hero_key
                 FROM picks_bans AS draft
                 LEFT JOIN heroes AS hero ON hero.hero_id=draft.hero_id
                WHERE draft.match_id=?
                ORDER BY draft.ord""",
            (match_id,),
        ).fetchall()
        if action["ord"] is not None
        and action["team"] in (0, 1)
        and action["hero_id"] is not None
    ]
    gold = _advantage_points(connection, "gold_advantage", match_id)
    xp = _advantage_points(connection, "xp_advantage", match_id)
    objectives = [
        {
            "time_seconds": objective["time"],
            "type": str(objective["type"] or ""),
            "unit": str(objective["unit"] or ""),
            "key": str(objective["key"] or ""),
            "player_slot": objective["player_slot"],
        }
        for objective in connection.execute(
            """SELECT time, type, unit, key, player_slot
                 FROM objectives WHERE match_id=? ORDER BY time, id""",
            (match_id,),
        ).fetchall()
    ]
    teamfights = [
        {
            "start_time": fight["start_time"],
            "end_time": fight["end_time"],
            "last_death": fight["last_death"],
            "deaths": fight["deaths"],
            "kills": int(fight["kills"] or 0),
            "damage": int(fight["damage"] or 0),
            "healing": int(fight["healing"] or 0),
            "gold_delta": int(fight["gold_delta"] or 0),
            "xp_delta": int(fight["xp_delta"] or 0),
        }
        for fight in connection.execute(
            """SELECT fight.id, fight.start_time, fight.end_time,
                      fight.last_death, fight.deaths,
                      SUM(player.kills) AS kills,
                      SUM(player.damage) AS damage,
                      SUM(player.healing) AS healing,
                      SUM(player.gold_delta) AS gold_delta,
                      SUM(player.xp_delta) AS xp_delta
                 FROM teamfights AS fight
                 LEFT JOIN teamfight_players AS player
                   ON player.teamfight_id=fight.id
                WHERE fight.match_id=?
                GROUP BY fight.id, fight.start_time, fight.end_time,
                         fight.last_death, fight.deaths
                ORDER BY fight.start_time, fight.id""",
            (match_id,),
        ).fetchall()
    ]
    core_available = row["radiant_win"] is not None and row["duration"] is not None
    named_players = sum(player["player_name"] is not None for player in players)
    history_eligible = sum(player["account_id"] is not None for player in players)
    players_with_history = sum(
        player["historical_average"] is not None for player in players
    )
    positioned_players = sum(player["position"] is not None for player in players)
    return {
        "map_number": int(row["map_number"]),
        "official_match_id": str(match_id),
        "status": "available" if core_available else "linked_not_ingested",
        "source": "opendota",
        "enrichment": stratz_enrichment,
        "fetched_at": row["fetched_at"],
        "result": (
            {
                "radiant_team_id": row["radiant_team_id"],
                "dire_team_id": row["dire_team_id"],
                "radiant_team_name": row["radiant_team_name"],
                "dire_team_name": row["dire_team_name"],
                "radiant_win": bool(row["radiant_win"]),
                "duration_seconds": int(row["duration"]),
                "start_time": row["start_time"],
                "league_id": row["leagueid"],
                "league_name": row["league_name"],
                "radiant_score": row["radiant_score"],
                "dire_score": row["dire_score"],
            }
            if core_available
            else None
        ),
        "players": players,
        "draft": draft,
        "advantages": {"gold": gold, "xp": xp},
        "objectives": objectives,
        "teamfights": teamfights,
        "availability": {
            "result": "available" if core_available else "missing",
            "players": _row_availability(len(players), expected=10),
            "player_names": (
                _row_availability(named_players, expected=len(players))
                if players
                else "missing"
            ),
            "historical_averages": (
                _row_availability(players_with_history, expected=history_eligible)
                if history_eligible
                else "missing"
            ),
            "positions": (
                _row_availability(positioned_players, expected=len(players))
                if players
                else "missing"
            ),
            "draft": "available" if draft else "missing",
            "gold_advantage": "available" if gold else "missing",
            "xp_advantage": "available" if xp else "missing",
            "objectives": "available" if objectives else "missing",
            "teamfights": "available" if teamfights else "missing",
        },
    }


def _postmatch_player(
    player: DatabaseRow,
    stratz_positions: dict[tuple[int, int], int],
    player_identities: dict[tuple[int, int], dict[str, str]],
    historical_averages: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    account_id = (
        int(player["account_id"])
        if type(player["account_id"]) is int and int(player["account_id"]) > 0
        else None
    )
    hero_id = int(player["hero_id"])
    player_slot = int(player["player_slot"])
    player_identity = player_identities.get((player_slot, hero_id), {})
    stratz_position = (
        stratz_positions.get((account_id, hero_id))
        if account_id is not None
        else None
    )
    return {
        "player_slot": player_slot,
        "account_id": account_id,
        "player_name": player_identity.get("name"),
        "player_name_source": player_identity.get("source"),
        "side": "radiant" if bool(player["is_radiant"]) else "dire",
        "team_id": int(player["team_id"]) if player["team_id"] is not None else None,
        "hero_id": hero_id,
        "hero_name": str(player["hero_name"] or f"Hero {hero_id}"),
        "hero_key": str(player["hero_key"] or ""),
        "kills": player["kills"],
        "deaths": player["deaths"],
        "assists": player["assists"],
        "gold_per_min": player["gold_per_min"],
        "xp_per_min": player["xp_per_min"],
        "net_worth": player["net_worth"],
        "last_hits": player["last_hits"],
        "denies": player["denies"],
        "hero_damage": player["hero_damage"],
        "hero_healing": player["hero_healing"],
        "tower_damage": player["tower_damage"],
        "level": player["level"],
        # OpenDota lane_role is a lane classification, not a farm-priority
        # position.  Only STRATZ's explicit POSITION_1..POSITION_5 value is
        # allowed into the player-facing role field.
        "position": stratz_position,
        "position_source": "stratz" if stratz_position is not None else None,
        "historical_average": (
            historical_averages.get(account_id) if account_id is not None else None
        ),
        "items": [
            int(player[column])
            for column in (
                "item_0", "item_1", "item_2", "item_3", "item_4", "item_5"
            )
            if player[column] is not None and int(player[column]) > 0
        ],
    }


def _opendota_player_identities(
    connection: PostgresSession,
    match_id: int,
) -> dict[tuple[int, int], dict[str, str]]:
    """Read display names from the verified raw OpenDota match response."""

    row = connection.execute(
        """SELECT artifact_id
             FROM raw_source_artifacts
            WHERE source='opendota' AND artifact_use='primary'
              AND endpoint=? AND match_id=?
            ORDER BY received_at DESC, artifact_id DESC
            LIMIT 1""",
        (f"/api/matches/{match_id}", match_id),
    ).fetchone()
    if row is None:
        return {}
    try:
        path = verify_registered_raw_source_artifact(
            connection,
            str(row["artifact_id"]),
        )
        payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return {}
    if not isinstance(payload, dict) or payload.get("match_id") != match_id:
        return {}

    identities: dict[tuple[int, int], dict[str, str]] = {}
    for raw_player in payload.get("players") or []:
        if not isinstance(raw_player, dict):
            continue
        player_slot = raw_player.get("player_slot")
        hero_id = raw_player.get("hero_id")
        if type(player_slot) is not int or type(hero_id) is not int or hero_id <= 0:
            continue
        for field, source in (
            ("name", "opendota_name"),
            ("personaname", "opendota_personaname"),
        ):
            value = raw_player.get(field)
            if isinstance(value, str) and 0 < len(value.strip()) <= 100:
                identities[(player_slot, hero_id)] = {
                    "name": value.strip(),
                    "source": source,
                }
                break
    return identities


def _player_historical_averages(
    connection: PostgresSession,
    player_rows: list[DatabaseRow],
    *,
    before_start_time: object,
) -> dict[int, dict[str, Any]]:
    """Return pre-match averages over all collected maps for known accounts."""

    if type(before_start_time) is not int or before_start_time <= 0:
        return {}
    account_ids = sorted(
        {
            int(player["account_id"])
            for player in player_rows
            if type(player["account_id"]) is int and int(player["account_id"]) > 0
        }
    )
    if not account_ids:
        return {}
    placeholders = ", ".join("?" for _ in account_ids)
    rows = connection.execute(
        f"""SELECT player.account_id,
                   COUNT(DISTINCT player.match_id) AS sample_size,
                   MIN(match.start_time) AS sample_start_time,
                   MAX(match.start_time) AS sample_end_time,
                   AVG(player.kills::double precision) AS kills,
                   AVG(player.deaths::double precision) AS deaths,
                   AVG(player.assists::double precision) AS assists,
                   AVG(player.gold_per_min::double precision) AS gold_per_min,
                   AVG(player.xp_per_min::double precision) AS xp_per_min,
                   AVG(player.net_worth::double precision) AS net_worth,
                   AVG(player.last_hits::double precision) AS last_hits,
                   AVG(player.hero_damage::double precision) AS hero_damage,
                   AVG(player.tower_damage::double precision) AS tower_damage
              FROM match_players AS player
              JOIN matches AS match ON match.match_id=player.match_id
             WHERE player.account_id IN ({placeholders})
               AND match.start_time>0
               AND match.start_time<?
               AND match.radiant_win IS NOT NULL
             GROUP BY player.account_id""",
        (*account_ids, before_start_time),
    ).fetchall()
    metric_names = (
        "kills",
        "deaths",
        "assists",
        "gold_per_min",
        "xp_per_min",
        "net_worth",
        "last_hits",
        "hero_damage",
        "tower_damage",
    )
    return {
        int(row["account_id"]): {
            "sample_size": int(row["sample_size"]),
            "source": "opendota_collected_history",
            "cutoff": "before_match_start",
            "sample_start_date": datetime.fromtimestamp(
                int(row["sample_start_time"]),
                tz=timezone.utc,
            ).date().isoformat(),
            "sample_end_date": datetime.fromtimestamp(
                int(row["sample_end_time"]),
                tz=timezone.utc,
            ).date().isoformat(),
            **{
                metric: (
                    round(float(row[metric]), 1)
                    if row[metric] is not None
                    else None
                )
                for metric in metric_names
            },
        }
        for row in rows
        if row["account_id"] is not None and int(row["sample_size"] or 0) > 0
    }


def _stratz_enrichment(
    connection: PostgresSession,
    match_id: int,
) -> tuple[dict[tuple[int, int], int], dict[str, Any]]:
    row = connection.execute(
        """SELECT artifact_id, received_at
             FROM raw_source_artifacts
            WHERE source='stratz' AND artifact_use='primary'
              AND endpoint='/graphql/match-detail-enrichment'
              AND match_id=?
            ORDER BY received_at DESC, artifact_id DESC
            LIMIT 1""",
        (match_id,),
    ).fetchone()
    if row is None:
        blocker = _stratz_source_blocker(connection)
        if blocker is not None:
            return {}, {
                "provider": "stratz",
                "status": "blocked",
                "reason": blocker[0],
                "observed_at": blocker[1],
            }
        return {}, {
            "provider": "stratz",
            "status": "not_available",
            "reason": "optional_enrichment_not_ingested",
            "observed_at": None,
        }
    try:
        path = verify_registered_raw_source_artifact(
            connection,
            str(row["artifact_id"]),
        )
        payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        positions = stratz_player_positions(payload, expected_match_id=match_id)
    except (
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
        StratzDetailError,
    ):
        return {}, {
            "provider": "stratz",
            "status": "invalid",
            "reason": "optional_enrichment_invalid",
            "observed_at": row["received_at"],
        }
    return positions, {
        "provider": "stratz",
        "status": "available" if positions else "partial",
        "reason": (
            "player_positions_available"
            if positions
            else "player_positions_missing"
        ),
        "observed_at": row["received_at"],
    }


def _stratz_source_blocker(
    connection: PostgresSession,
) -> tuple[str, object] | None:
    row = connection.execute(
        """SELECT last_error_at, details_json
             FROM service_health
            WHERE component='postmatch_worker' AND status IN ('degraded', 'unhealthy')"""
    ).fetchone()
    if row is None:
        return None
    try:
        details = json.loads(str(row["details_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    enrichment = details.get("stratz_enrichment") if isinstance(details, dict) else None
    reasons = enrichment.get("failure_reasons") if isinstance(enrichment, dict) else None
    if not isinstance(reasons, list):
        return None
    stable_reasons = sorted(
        reason.strip()
        for value in reasons
        if isinstance(value, str) and (reason := value.strip())
    )
    if not stable_reasons:
        return None
    return stable_reasons[0], row["last_error_at"]


def _advantage_points(
    connection: PostgresSession,
    table: str,
    match_id: int,
) -> list[dict[str, int]]:
    if table not in {"gold_advantage", "xp_advantage"}:
        raise ValueError("unsupported advantage table")
    return [
        {"minute": int(row["time_min"]), "value": int(row["value"])}
        for row in connection.execute(
            f"SELECT time_min, value FROM {table} WHERE match_id=? ORDER BY time_min",
            (match_id,),
        ).fetchall()
        if row["time_min"] is not None and row["value"] is not None
    ]


def _row_availability(count: int, *, expected: int) -> str:
    if count >= expected:
        return "available"
    return "partial" if count else "missing"


def winner_timeline(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    max_points: int | None = 1200,
    period: str | None = None,
    as_of: datetime | None = None,
    processing_status: str = "processed",
    deduplicate_ended: bool = True,
) -> list[dict[str, Any]]:
    if processing_status not in {"processed", "audit_only"}:
        raise ValueError("unsupported odds processing status")
    cutoff = _aware_utc(as_of).isoformat() if as_of is not None else None
    ended_match = deduplicate_ended and connection.execute(
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
                       AND transport.processing_status=?
                      AND outcome.market_type='winner'
                      AND outcome.supported=1
                      AND (CAST(? AS text) IS NULL OR outcome.period=?)
                    ORDER BY transport.observed_at, transport.observation_key,
                             outcome.period, outcome.odds_group_id,
                             outcome.odds_id""",
                    (
                        raybet_match_id,
                        cutoff,
                        cutoff,
                        processing_status,
                        period,
                        period,
                    ),
                )
        except SQLAlchemyError:
            return []
    else:
        if processing_status != "processed":
            return []
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
        points.append(point)
    if ended_match:
        points = _deduplicate_winner_timeline(points)
    return points if max_points is None else _downsample(points, max_points)


def _deduplicate_winner_timeline(
    points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    signatures: dict[str, tuple[object, ...]] = {}
    output: list[dict[str, Any]] = []
    for point in points:
        period = str(point["period"])
        signature = (
            tuple(sorted(point["prices"].items())),
            tuple(sorted(point["status"].items())),
            point["game_clock_seconds"],
            point["map_number"],
            None
            if point["alignment"] is None
            else tuple(sorted(point["alignment"].items())),
        )
        if signatures.get(period) == signature:
            continue
        signatures[period] = signature
        output.append(point)
    return output


def _series_game_details(
    connection: PostgresSession,
    *,
    summary: dict[str, Any],
    prematch_timeline: list[dict[str, Any]],
    collection_timeline: list[dict[str, Any]],
    vision: list[dict[str, Any]],
    latest_capture: dict[str, Any] | None,
    game_snapshots: list[dict[str, Any]],
    latest_huds: dict[int, dict[str, Any]],
    vision_runtime: dict[str, Any] | None,
    markets: list[dict[str, Any]],
    postmatch: dict[str, Any],
    raybet_final_map_numbers: set[int],
    max_points: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    match_id = str(summary["raybet_match_id"])
    map_numbers = _locked_draft_map_numbers(connection, match_id)
    map_numbers.update(
        int(point["map_number"])
        for point in (*vision, *game_snapshots)
        if type(point.get("map_number")) is int
    )
    map_numbers.update(
        int(item["map_number"])
        for item in (*postmatch["games"], *postmatch["unresolved_maps"])
    )
    map_numbers.update(raybet_final_map_numbers)
    if (
        summary.get("lifecycle") == "live"
        and type(summary.get("current_map_number")) is int
    ):
        map_numbers.add(int(summary["current_map_number"]))

    market_map_numbers = {
        value
        for point in (*prematch_timeline, *collection_timeline)
        if (value := _map_number_from_period(str(point["period"]))) is not None
    }
    market_map_numbers.update(
        value
        for market in markets
        if (value := _map_number_from_period(str(market["period"]))) is not None
    )

    games: list[dict[str, Any]] = []
    for map_number in sorted(map_numbers):
        period = f"map_{map_number}"
        raw_game_timeline = [
            point for point in collection_timeline if point["period"] == period
        ]
        game_timeline = (
            _deduplicate_winner_timeline(raw_game_timeline)
            if summary["lifecycle"] == "ended"
            else raw_game_timeline
        )
        game_timeline = _downsample(game_timeline, max_points)
        game_prematch = [
            point for point in prematch_timeline if point["period"] == period
        ]
        game_vision = [
            point for point in vision if point["map_number"] == map_number
        ]
        snapshots = [
            point
            for point in game_snapshots
            if point["map_number"] == map_number
        ]
        mapping = latest_live_draft_mapping(
            connection,
            match_id,
            map_number=map_number,
        )
        game_postmatch = _postmatch_for_game(postmatch, map_number)
        official_match_id, link_status, link_reason = _map_link_identity(
            game_postmatch
        )
        state = _series_game_state(
            lifecycle=str(summary["lifecycle"]),
            current_map_number=summary.get("current_map_number"),
            map_number=map_number,
            has_postmatch=bool(game_postmatch["games"]),
            has_confirmed_raybet_final=map_number in raybet_final_map_numbers,
        )
        capture = (
            latest_capture
            if latest_capture is not None
            and latest_capture.get("map_number") == map_number
            else None
        )
        hud = latest_huds.get(map_number)
        runtime = (
            vision_runtime
            if vision_runtime is not None
            and vision_runtime.get("map_number") == map_number
            else None
        )
        games.append(
            {
                "game_id": f"{match_id}:map_{map_number}",
                "map_id": f"{match_id}:map_{map_number}",
                "map_number": map_number,
                "period": period,
                "official_match_id": official_match_id,
                "link_status": link_status,
                "link_reason": link_reason,
                "play_evidence": [
                    source
                    for source, available in (
                        ("locked_draft_mapping", mapping is not None and mapping["is_locked"]),
                        ("verified_game_frame", bool(game_vision)),
                        ("trusted_game_snapshot", bool(snapshots)),
                        ("raybet_final_market", map_number in raybet_final_map_numbers),
                        ("official_map_result", bool(game_postmatch["games"])),
                        (
                            "provider_live_map",
                            summary.get("lifecycle") == "live"
                            and summary.get("current_map_number") == map_number,
                        ),
                    )
                    if available
                ],
                "state": state,
                "winner": _latest_game_winner(game_timeline, state=state),
                "prematch_winner": _latest_game_winner(
                    game_prematch,
                    state="scheduled",
                ),
                "winner_timeline": game_timeline,
                "odds_coverage": _odds_coverage_summary(
                    game_prematch,
                    raw_game_timeline,
                    game_state=state,
                ),
                "vision": game_vision,
                "latest_vision": game_vision[-1] if game_vision else None,
                "latest_capture": capture,
                "draft_mapping": mapping,
                "game_snapshots": snapshots,
                "latest_game_snapshot": snapshots[-1] if snapshots else None,
                "latest_hud_observation": hud,
                "vision_runtime": runtime,
                "markets": [
                    market for market in markets if market["period"] == period
                ],
                "postmatch": game_postmatch,
                "decision_checkpoints": latest_map_checkpoints(
                    connection,
                    match_id,
                    map_number,
                ),
            }
        )
    market_evidence = [
        _market_only_map_evidence(
            match_id=match_id,
            map_number=map_number,
            prematch_timeline=prematch_timeline,
            collection_timeline=collection_timeline,
            markets=markets,
        )
        for map_number in sorted(market_map_numbers - map_numbers)
    ]
    return games, market_evidence


def _locked_draft_map_numbers(
    connection: PostgresSession,
    raybet_match_id: str,
) -> set[int]:
    try:
        rows = connection.execute(
            """WITH latest AS (
                   SELECT map_number, MAX(version) AS version
                     FROM live_draft_mappings
                    WHERE raybet_match_id=?
                    GROUP BY map_number
               )
               SELECT mapping.map_number
                 FROM live_draft_mappings AS mapping
                 JOIN latest
                   ON latest.map_number=mapping.map_number
                  AND latest.version=mapping.version
                WHERE mapping.raybet_match_id=?
                GROUP BY mapping.map_number
               HAVING COUNT(*)=10
                  AND COUNT(DISTINCT mapping.hero_id)=10
                  AND COUNT(*) FILTER (WHERE mapping.is_locked=1)=10
                  AND COUNT(*) FILTER (WHERE mapping.side='radiant')=5
                  AND COUNT(*) FILTER (WHERE mapping.side='dire')=5
                  AND COUNT(DISTINCT mapping.position)
                      FILTER (WHERE mapping.side='radiant')=5
                  AND COUNT(DISTINCT mapping.position)
                      FILTER (WHERE mapping.side='dire')=5""",
            (raybet_match_id, raybet_match_id),
        ).fetchall()
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return set()
        raise
    return {int(row["map_number"]) for row in rows}


def _map_link_identity(
    postmatch: dict[str, Any],
) -> tuple[str | None, str, str]:
    if postmatch["games"]:
        game = postmatch["games"][0]
        return (
            str(game["official_match_id"]),
            "confirmed",
            str(game.get("identity_reason") or "exact_map_result"),
        )
    if postmatch["unresolved_maps"]:
        unresolved = postmatch["unresolved_maps"][0]
        official_match_id = unresolved.get("official_match_id")
        return (
            str(official_match_id) if official_match_id is not None else None,
            "unlinked",
            str(unresolved.get("reason") or "exact_map_link_unresolved"),
        )
    return None, "unlinked", "exact_official_match_id_not_available"


def _market_only_map_evidence(
    *,
    match_id: str,
    map_number: int,
    prematch_timeline: list[dict[str, Any]],
    collection_timeline: list[dict[str, Any]],
    markets: list[dict[str, Any]],
) -> dict[str, Any]:
    period = f"map_{map_number}"
    prematch = [point for point in prematch_timeline if point["period"] == period]
    live = [point for point in collection_timeline if point["period"] == period]
    return {
        "market_id": f"{match_id}:{period}",
        "map_number": map_number,
        "period": period,
        "status": "market_only",
        "reason": "no_play_evidence",
        "prematch_winner": _latest_game_winner(prematch, state="scheduled"),
        "winner_timeline": live,
        "odds_coverage": _odds_coverage_summary(
            prematch,
            live,
            game_state="unconfirmed",
        ),
        "markets": [market for market in markets if market["period"] == period],
    }


def _map_number_from_period(period: str) -> int | None:
    matched = _MAP_PERIOD_PATTERN.fullmatch(period)
    return int(matched.group(1)) if matched else None


def _series_game_state(
    *,
    lifecycle: str,
    current_map_number: object,
    map_number: int,
    has_postmatch: bool,
    has_confirmed_raybet_final: bool,
) -> str:
    if has_postmatch or has_confirmed_raybet_final:
        return "ended"
    if type(current_map_number) is int:
        if map_number < current_map_number:
            return "ended"
        if map_number == current_map_number and lifecycle in {"live", "degraded"}:
            return "live"
    if lifecycle == "upcoming":
        return "scheduled"
    return "unconfirmed" if lifecycle == "ended" else "scheduled"


def _confirmed_raybet_final_map_numbers(row: DatabaseRow) -> set[int]:
    best_of = row["best_of"]
    if type(best_of) is not int or not 1 <= int(best_of) <= 5:
        return set()
    try:
        payload = json.loads(str(row["raw_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    confirmed = set()
    for map_number in range(1, int(best_of) + 1):
        try:
            evidence = parse_raybet_map_final(payload, map_number)
        except (TypeError, ValueError):
            continue
        if (
            evidence.status == "confirmed"
            and evidence.reason == "raybet_final_confirmed"
            and evidence.winner_side in {"team_one", "team_two"}
        ):
            confirmed.add(map_number)
    return confirmed


def _latest_game_winner(
    points: list[dict[str, Any]],
    *,
    state: str,
) -> dict[str, Any] | None:
    if not points:
        return None
    eligible = [
        point
        for point in points
        if (
            all(str(value) == "5" for value in point["status"].values())
            if state == "ended"
            else all(raybet_odds_is_open(value) for value in point["status"].values())
        )
    ]
    if not eligible:
        return None
    point = max(eligible, key=lambda item: _parse_time(item["observed_at"]))
    return {**point, "complete": True}


def _postmatch_for_game(
    postmatch: dict[str, Any],
    map_number: int,
) -> dict[str, Any]:
    games = [game for game in postmatch["games"] if game["map_number"] == map_number]
    unresolved = [
        item
        for item in postmatch["unresolved_maps"]
        if item["map_number"] == map_number
    ]
    if games:
        status = "available" if games[0]["status"] == "available" else "partial"
        reason = f"map_{map_number}_postmatch_available"
    elif unresolved:
        status = "waiting"
        reason = str(unresolved[0]["reason"])
    else:
        status = "waiting"
        reason = f"map_{map_number}_not_observed"
    return {
        **postmatch,
        "status": status,
        "reason": reason,
        "games": games,
        "unresolved_maps": unresolved,
    }


def _odds_coverage_summary(
    prematch_points: list[dict[str, Any]],
    live_points: list[dict[str, Any]],
    *,
    game_state: str,
) -> dict[str, Any]:
    prematch = _odds_phase_coverage(prematch_points)
    live = _odds_phase_coverage(live_points)
    if not live_points:
        live["status"] = (
            "pending" if game_state in {"scheduled", "live"} else "missing"
        )

    if game_state == "unconfirmed" and live_points:
        point = max(
            live_points,
            key=lambda item: _parse_time(item["observed_at"]),
        )
        closing = {
            "status": "unconfirmed",
            "observed_at": point["observed_at"],
            "prices": point["prices"],
            "probabilities": point["probabilities"],
        }
    elif game_state == "ended":
        if live_points:
            point = max(
                live_points,
                key=lambda item: _parse_time(item["observed_at"]),
            )
            closing = {
                "status": "available",
                "observed_at": point["observed_at"],
                "prices": point["prices"],
                "probabilities": point["probabilities"],
            }
        else:
            closing = {
                "status": "missing",
                "observed_at": None,
                "prices": None,
                "probabilities": None,
            }
    else:
        closing = {
            "status": "pending",
            "observed_at": None,
            "prices": None,
            "probabilities": None,
        }

    return {
        "source": "raybet_direct",
        "gap_threshold_seconds": _ODDS_GAP_THRESHOLD_SECONDS,
        "prematch": prematch,
        "live": live,
        "closing": closing,
    }


def _odds_phase_coverage(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return {
            "status": "missing",
            "complete_snapshot_count": 0,
            "observation_count": 0,
            "first_observed_at": None,
            "last_observed_at": None,
            "gap_count": 0,
            "longest_gap_seconds": None,
            "periods": [],
        }

    by_period: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        by_period[str(point["period"])].append(point)
    observation_times = sorted(
        {str(point["observed_at"]) for point in points},
        key=_parse_time,
    )
    gap_count, longest_gap = _odds_gap_metrics(observation_times)
    periods = []
    for period in sorted(by_period, key=_period_sort_key):
        period_points = by_period[period]
        period_times = sorted(
            {str(point["observed_at"]) for point in period_points},
            key=_parse_time,
        )
        period_gap_count, period_longest_gap = _odds_gap_metrics(period_times)
        periods.append(
            {
                "period": period,
                "complete_snapshot_count": len(period_points),
                "observation_count": len(period_times),
                "first_observed_at": period_times[0],
                "last_observed_at": period_times[-1],
                "gap_count": period_gap_count,
                "longest_gap_seconds": period_longest_gap,
            }
        )
    return {
        "status": "available",
        "complete_snapshot_count": len(points),
        "observation_count": len(observation_times),
        "first_observed_at": observation_times[0],
        "last_observed_at": observation_times[-1],
        "gap_count": gap_count,
        "longest_gap_seconds": longest_gap,
        "periods": periods,
    }


def _odds_gap_metrics(observed_at_values: list[str]) -> tuple[int, float | None]:
    timestamps = sorted(
        value for value in map(_parse_time, observed_at_values) if value
    )
    gaps = [
        (current - previous).total_seconds()
        for previous, current in zip(timestamps, timestamps[1:])
        if (current - previous).total_seconds() > _ODDS_GAP_THRESHOLD_SECONDS
    ]
    return len(gaps), round(max(gaps), 3) if gaps else None


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
            if str(row["status"] or "").casefold() == "unlisted":
                continue
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
        if str(row["status"] or "").casefold() == "unlisted":
            continue
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
    provider_status = str(row["status"] or "")
    provider_map_limit = (
        _provider_current_map_number(row) if provider_status == "2" else None
    )
    latest_vision = _latest_valid_vision_row(
        connection,
        match_id,
        now=now,
        maximum_map_number=provider_map_limit,
    )
    mapping_readiness = _mapping_readiness(connection, match_id, now)
    latest_odds_activity = _latest_odds_activity(connection, match_id, now=now)

    if provider_status.casefold() in _UPCOMING_MATCH_STATUSES:
        latest_prematch_odds = _latest_row(
            connection,
            """SELECT observed_at FROM odds_transport_observations
                WHERE raybet_match_id=?
                  AND source='direct'
                  AND live_text_timestamp_utc(observed_at)<=CAST(? AS timestamptz)
                  AND timing_status='on_time'
                  AND processing_status='audit_only'
                ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (match_id, now.isoformat()),
        )
        odds_readiness = _freshness(
            latest_prematch_odds,
            now,
            warning=150.0,
            stale=300.0,
        )
    else:
        odds_readiness = _freshness(latest_odds, now, warning=15.0, stale=60.0)
    vision_readiness = _vision_readiness(
        latest_vision,
        provider_status=provider_status,
        scheduled_at=row["scheduled_at"],
        now=now,
    )
    lifecycle = _lifecycle(
        provider_status,
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
        provider_map_limit
        if lifecycle in {"live", "degraded"}
        else None
    )
    current_map_number = (
        provider_map_number if provider_map_number is not None else vision_map_number
    )
    current_winner = _current_winner(
        connection,
        match_id,
        provider_status=provider_status,
        preferred_period=(
            f"map_{current_map_number}" if current_map_number is not None else None
        ),
    )
    prematch_winner = (
        _current_winner(
            connection,
            match_id,
            provider_status=provider_status,
            processing_status="audit_only",
            transport_only=True,
        )
        if provider_status.casefold() in _UPCOMING_MATCH_STATUSES
        else None
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
        "display_name": match_display_name(
            raybet_match_id=match_id,
            team_one=str(row["team_one"] or "") or None,
            team_two=str(row["team_two"] or "") or None,
            tournament=str(row["tournament"] or "") or None,
        ),
        "observation_file": observation_file_name(match_id),
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
        "prematch_winner": prematch_winner,
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
    resolver_url = _stream_resolver_url(row)
    if resolver_url is not None:
        return {
            "kind": "stream_resolver",
            "availability": "available",
            "url": resolver_url,
            "reason": "fresh_stream_resolution_available",
        }
    return {
        "kind": "none",
        "availability": "unavailable",
        "url": None,
        "reason": "no_safe_entry",
    }


def _stream_resolver_url(row: DatabaseRow) -> str | None:
    match_id = str(row["raybet_match_id"] or "").strip()
    if not match_id.isdigit() or str(row["status"] or "") != "2":
        return None
    try:
        payload = json.loads(str(row["raw_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("game_id") != DOTA2_GAME_ID:
        return None
    return f"/api/monitor/matches/{url_quote(match_id, safe='')}/live-stream"


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
    maximum_map_number: int | None = None,
) -> list[dict[str, Any]]:
    return [
        _vision_point(row, raybet_match_id)
        for row in _valid_vision_rows(
            connection,
            raybet_match_id,
            now=_aware_utc(now or utc_now()),
            max_points=max_points,
            maximum_map_number=maximum_map_number,
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
    maximum_map_number: int | None = None,
) -> DatabaseRow | None:
    frame_filter = (
        "AND observation.source_frame_ref=?" if source_frame_ref is not None else ""
    )
    map_filter = (
        "AND observation.map_number<=?" if maximum_map_number is not None else ""
    )
    params: tuple[Any, ...] = (raybet_match_id,)
    if maximum_map_number is not None:
        if type(maximum_map_number) is not int or maximum_map_number <= 0:
            raise ValueError("maximum_map_number must be a positive integer")
        params += (maximum_map_number,)
    params += (now.isoformat(),)
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
                   {map_filter}
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
            (
                *params,
                VISION_FRAME_REF_PREFIX,
                *(() if source_frame_ref is None else (source_frame_ref,)),
            ),
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
    maximum_map_number: int | None = None,
) -> DatabaseRow | None:
    rows = _valid_vision_rows(
        connection,
        raybet_match_id,
        now=now,
        max_points=1,
        maximum_map_number=maximum_map_number,
    )
    return rows[0] if rows else None


def _latest_hud_observations(
    raybet_match_id: str,
    *,
    now: datetime,
    valid_vision_points: list[dict[str, Any]],
    maximum_map_number: int | None = None,
) -> dict[int, dict[str, Any]]:
    valid_frame_keys = {
        (int(point["map_number"]), captured_at, str(point["source_frame_ref"]))
        for point in valid_vision_points
        if type(point.get("map_number")) is int
        and int(point["map_number"]) > 0
        and (captured_at := _parse_time(point.get("captured_at"))) is not None
        and isinstance(point.get("source_frame_ref"), str)
        and str(point["source_frame_ref"]).startswith(VISION_FRAME_REF_PREFIX)
    }
    expected_maps = {
        map_number
        for map_number, _, _ in valid_frame_keys
        if maximum_map_number is None or map_number <= maximum_map_number
    }
    latest_by_map: dict[int, dict[str, Any]] = {}
    path = _vision_observation_path(raybet_match_id)
    for payload in _reverse_jsonl_payloads(path):
        try:
            observation = parse_observation(payload)
        except (TypeError, ValueError):
            continue
        if observation.raybet_match_id != raybet_match_id:
            continue
        map_number = observation.map_number
        if (
            map_number is None
            or map_number not in expected_maps
            or map_number in latest_by_map
        ):
            continue
        if (
            maximum_map_number is not None
            and map_number > maximum_map_number
        ):
            continue
        captured_at = _aware_utc(observation.captured_at)
        if captured_at > now + _TIMESTAMP_ROUNDING_GRACE:
            continue
        if (
            map_number,
            captured_at,
            observation.source_frame_ref,
        ) not in valid_frame_keys:
            continue
        latest_by_map[map_number] = _hud_observation_point(
            observation,
            observation_file=observation_file_name(raybet_match_id),
        )
        if latest_by_map.keys() == expected_maps:
            break
    return latest_by_map


def _vision_runtime_status(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    health = next(
        (
            item
            for item in derive_health(connection, now=now)
            if item["component"] == "vision_worker"
        ),
        None,
    )
    if health is None:
        return None
    watchers = health["details"].get("watchers")
    if not isinstance(watchers, dict):
        return None
    watcher = watchers.get(raybet_match_id)
    if not isinstance(watcher, dict):
        return None
    fields = {
        key: value if isinstance(value, str) and value else None
        for key in (
            "capture_state",
            "reason",
            "blocker_code",
            "replay_gate_status",
            "screen_state",
        )
        if (value := watcher.get(key)) is not None
    }
    return {
        "worker_status": health["status"],
        "freshness": health["freshness"],
        "observed_at": health["last_heartbeat_at"],
        "map_number": (
            int(watcher["map_number"])
            if type(watcher.get("map_number")) is int
            else None
        ),
        **fields,
    }


def _vision_observation_path(raybet_match_id: str) -> Path:
    configured = os.environ.get("VISION_OBSERVATION_DIR", "").strip()
    root = Path(configured) if configured else _DEFAULT_VISION_OBSERVATION_DIR
    return root / observation_file_name(raybet_match_id)


def _reverse_jsonl_payloads(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            remainder = b""
            while position > 0:
                read_size = min(position, _JSONL_REVERSE_CHUNK_BYTES)
                position -= read_size
                handle.seek(position)
                block = handle.read(read_size) + remainder
                lines = block.split(b"\n")
                remainder = lines[0]
                for raw_line in reversed(lines[1:]):
                    payload = _jsonl_payload(raw_line)
                    if payload is not None:
                        yield payload
            payload = _jsonl_payload(remainder)
            if payload is not None:
                yield payload
    except OSError:
        return


def _jsonl_payload(raw_line: bytes) -> dict[str, Any] | None:
    if not raw_line.strip():
        return None
    try:
        payload = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _hud_observation_point(
    observation: VisionObservation,
    *,
    observation_file: str,
) -> dict[str, Any]:
    comeback_state = observation.comeback_state
    clock_confirmed = (
        observation.map_number is not None
        and observation.game_clock_seconds is not None
        and observation.clock_confidence >= 0.9
        and observation.screen_state == "game"
    )
    hud_confirmed = observation.is_hud_confirmed
    draft_confirmed = observation.is_confirmed
    frame_digest = (
        observation.source_frame_ref.removeprefix(VISION_FRAME_REF_PREFIX)
        if observation.source_frame_ref.startswith(VISION_FRAME_REF_PREFIX)
        else None
    )
    frame_url = (
        f"/api/monitor/matches/{url_quote(observation.raybet_match_id, safe='')}"
        f"/vision-frames/{frame_digest}.jpg"
        if frame_digest is not None and re.fullmatch(r"[0-9a-f]{64}", frame_digest)
        else None
    )
    return {
        "status": "available" if hud_confirmed else "unavailable",
        "source": comeback_state.source if hud_confirmed else None,
        "observation_file": observation_file,
        "source_frame_ref": observation.source_frame_ref,
        "frame_url": frame_url,
        "captured_at": observation.captured_at.isoformat(),
        "map_number": observation.map_number if clock_confirmed else None,
        "game_clock_seconds": (
            observation.game_clock_seconds if clock_confirmed else None
        ),
        "is_paused": observation.is_paused if clock_confirmed else None,
        "screen_state": observation.screen_state,
        "clock_confidence": round(observation.clock_confidence, 6),
        "draft_confidence": round(observation.draft_confidence, 6),
        "hud_confidence": round(comeback_state.confidence, 6),
        "draft_confirmed": draft_confirmed,
        "radiant_hero_count": len(observation.radiant_hero_ids),
        "dire_hero_count": len(observation.dire_hero_ids),
        "radiant_hero_ids": (
            list(observation.radiant_hero_ids) if draft_confirmed else []
        ),
        "dire_hero_ids": (
            list(observation.dire_hero_ids) if draft_confirmed else []
        ),
        "radiant_kills": comeback_state.radiant_kills if hud_confirmed else None,
        "dire_kills": comeback_state.dire_kills if hud_confirmed else None,
        "radiant_net_worth": comeback_state.radiant_net_worth
        if hud_confirmed
        else None,
        "dire_net_worth": comeback_state.dire_net_worth if hud_confirmed else None,
        "net_worth_advantage_side": (
            comeback_state.net_worth_advantage_side if hud_confirmed else None
        ),
        "net_worth_advantage_min": (
            comeback_state.net_worth_advantage_min if hud_confirmed else None
        ),
        "net_worth_advantage_max": (
            comeback_state.net_worth_advantage_max if hud_confirmed else None
        ),
        "unavailable_reason": None
        if hud_confirmed
        else _hud_unavailable_reason(observation),
    }


def _hud_unavailable_reason(observation: VisionObservation) -> str:
    comeback_state = observation.comeback_state
    if observation.screen_state != "game":
        return f"screen_state_{observation.screen_state}"
    if (
        observation.map_number is None
        or observation.game_clock_seconds is None
        or observation.clock_confidence < 0.9
    ):
        return "clock_unconfirmed"
    if comeback_state.status != "available":
        return comeback_state.unavailable_reason or "hud_unavailable"
    if comeback_state.source != "vision_hud":
        return "hud_source_unavailable"
    if comeback_state.confidence < 0.9:
        return "hud_confidence_low"
    return "hud_unavailable"


def _valid_vision_rows(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime,
    max_points: int,
    source_frame_ref: str | None = None,
    maximum_map_number: int | None = None,
) -> list[DatabaseRow]:
    if type(max_points) is not int or max_points <= 0:
        raise ValueError("max_points must be a positive integer")
    limit = min(max_points, _MAX_VISION_TIMELINE_POINTS)
    frame_filter = (
        "AND observation.source_frame_ref=?" if source_frame_ref is not None else ""
    )
    map_filter = (
        "AND observation.map_number<=?" if maximum_map_number is not None else ""
    )
    provider_start_times = _provider_map_start_times(connection, raybet_match_id)
    provider_boundary_clauses = "".join(
        """
        OR (
            observation.map_number=?
            AND live_text_timestamp_utc(observation.captured_at)>=
                CAST(? AS timestamptz)
        )"""
        for map_number in provider_start_times
        if map_number > 1
    )
    params: list[Any] = [raybet_match_id]
    if maximum_map_number is not None:
        if type(maximum_map_number) is not int or maximum_map_number <= 0:
            raise ValueError("maximum_map_number must be a positive integer")
        params.append(maximum_map_number)
    params.extend(
        [
            VISION_FRAME_REF_PREFIX,
            -MAP_START_EVIDENCE_WINDOW_SECONDS,
            MAP_START_EVIDENCE_WINDOW_SECONDS,
            VISION_FRAME_REF_PREFIX,
        ]
    )
    for map_number, started_at in provider_start_times.items():
        if map_number > 1:
            params.extend((map_number, started_at.isoformat()))
    params.append(now.isoformat())
    if source_frame_ref is not None:
        params.append(source_frame_ref)
    params.append(limit)
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
                                   frame.content_sha256 AS _frame_digest
                               FROM vision_observations AS observation
                               JOIN active_vision_frame_artifacts AS frame
                                ON frame.frame_ref=observation.source_frame_ref
                               AND frame.content_sha256=
                                   observation.source_frame_sha256
                               AND frame.byte_length=observation.source_frame_bytes
                              WHERE observation.raybet_match_id=?
                                {map_filter}
                                AND observation.map_number IS NOT NULL
                                AND observation.game_clock_seconds IS NOT NULL
                                AND observation.screen_state='game'
                                AND observation.clock_confidence>=0.9
                                AND observation.source_frame_ref=
                                    ? || frame.content_sha256
                                AND (
                                    observation.map_number=1
                                    OR EXISTS (
                                        SELECT 1
                                          FROM vision_observations AS map_start
                                          JOIN active_vision_frame_artifacts AS start_frame
                                            ON start_frame.frame_ref=
                                               map_start.source_frame_ref
                                           AND start_frame.content_sha256=
                                               map_start.source_frame_sha256
                                           AND start_frame.byte_length=
                                               map_start.source_frame_bytes
                                         WHERE map_start.raybet_match_id=
                                               observation.raybet_match_id
                                           AND map_start.map_number=
                                               observation.map_number
                                           AND map_start.game_clock_seconds
                                               BETWEEN ? AND ?
                                           AND map_start.screen_state='game'
                                           AND map_start.clock_confidence>=0.9
                                           AND map_start.source_frame_ref=
                                               ? || start_frame.content_sha256
                                           AND live_text_timestamp_utc(
                                                   map_start.captured_at
                                               )<=live_text_timestamp_utc(
                                                   observation.captured_at
                                               )
                                           AND NOT EXISTS (
                                               SELECT 1
                                                 FROM vision_observation_invalidations
                                                      AS start_invalidation
                                                WHERE start_invalidation.raybet_match_id=
                                                      map_start.raybet_match_id
                                                  AND start_invalidation.captured_at=
                                                      map_start.captured_at
                                                  AND start_invalidation.source_frame_ref=
                                                      map_start.source_frame_ref
                                           )
                                    )
                                    {provider_boundary_clauses}
                                )
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
                tuple(params),
            ).fetchall()
        )
    except SQLAlchemyError as error:
        if _is_schema_missing_error(error):
            return []
        raise


def _provider_map_start_times(
    connection: PostgresSession,
    raybet_match_id: str,
) -> dict[int, datetime]:
    row = connection.execute(
        "SELECT raw_json, best_of FROM raybet_matches WHERE raybet_match_id=?",
        (raybet_match_id,),
    ).fetchone()
    if row is None or type(row[1]) is not int:
        return {}
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return explicit_raybet_map_times(payload, int(row[1]))


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


def _vision_readiness(
    row: DatabaseRow | None,
    *,
    provider_status: str,
    scheduled_at: object,
    now: datetime,
) -> dict[str, Any]:
    readiness = _freshness(row, now, warning=20.0, stale=120.0)
    if row is not None:
        return readiness
    scheduled = _parse_schedule(scheduled_at)
    if provider_status.casefold() in _UPCOMING_MATCH_STATUSES and scheduled is not None:
        watch_starts_at = scheduled - _VISION_PREMATCH_WATCH_WINDOW
        return {
            **readiness,
            "reason": (
                "waiting_for_watch_window"
                if now < watch_starts_at
                else "stream_probe_pending"
            ),
            "watch_starts_at": watch_starts_at.isoformat(),
        }
    return {
        **readiness,
        "reason": "stream_probe_pending",
        "watch_starts_at": None,
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
