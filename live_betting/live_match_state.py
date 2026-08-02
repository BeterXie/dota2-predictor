"""Manual live-draft authority and append-only dynamic game snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from database.session import PostgresSession


@dataclass(frozen=True)
class DraftSlotInput:
    team_id: int
    side: str
    position: int
    hero_id: int
    player_id: int | None = None


def live_draft_context(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    """Resolve the two canonical teams and their latest known positional rosters."""
    match_id = _required_text(raybet_match_id, "raybet_match_id")
    match = connection.execute(
        """SELECT team_one, team_two, raw_json
             FROM raybet_matches WHERE raybet_match_id=?""",
        (match_id,),
    ).fetchone()
    if match is None:
        return None
    teams, source = _draft_context_teams(connection, match_id, match)
    if len(teams) != 2 or teams[0]["team_id"] == teams[1]["team_id"]:
        return {
            "status": "unavailable",
            "reason": "canonical_teams_unresolved",
            "source": source,
            "teams": [],
        }
    cutoff = int(_aware_utc(as_of or datetime.now(timezone.utc)).timestamp())
    return {
        "status": "ready",
        "reason": "draft_context_ready",
        "source": source,
        "teams": [
            {
                **team,
                **_latest_positional_roster(
                    connection,
                    int(team["team_id"]),
                    cutoff,
                ),
            }
            for team in teams
        ],
    }


def _draft_context_teams(
    connection: PostgresSession,
    match_id: str,
    match: Any,
) -> tuple[list[dict[str, Any]], str]:
    mapping = connection.execute(
        """SELECT mapping.canonical_team_one_id,
                  mapping.canonical_team_one_name,
                  mapping.canonical_team_two_id,
                  mapping.canonical_team_two_name
             FROM strict_live_map_mappings AS mapping
             LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
               ON invalidation.mapping_id=mapping.mapping_id
            WHERE mapping.raybet_match_id=?
              AND invalidation.mapping_id IS NULL
            ORDER BY mapping.map_number DESC, mapping.accepted_at DESC LIMIT 1""",
        (match_id,),
    ).fetchone()
    if mapping is not None:
        return [
            {
                "match_side": "team_one",
                "team_id": int(mapping[0]),
                "team_name": str(mapping[1]),
            },
            {
                "match_side": "team_two",
                "team_id": int(mapping[2]),
                "team_name": str(mapping[3]),
            },
        ], "strict_mapping"

    try:
        payload = json.loads(str(match[2]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return [], "raybet_exact_name"
    raw_teams = payload.get("team") if isinstance(payload, dict) else None
    if not isinstance(raw_teams, list):
        return [], "raybet_exact_name"
    ordered = sorted(
        (team for team in raw_teams if isinstance(team, dict)),
        key=lambda team: int(team.get("pos", 99)),
    )
    if len(ordered) != 2:
        return [], "raybet_exact_name"
    catalog = connection.execute(
        "SELECT team_id, name, tag FROM teams ORDER BY team_id"
    ).fetchall()
    identity_mappings = _raybet_identity_mappings(connection, ordered)
    used_identity_mapping = False
    result: list[dict[str, Any]] = []
    fallback_names = (str(match[0] or ""), str(match[1] or ""))
    for index, raw_team in enumerate(ordered):
        raybet_team_id = int(raw_team.get("team_id") or 0)
        mapped_team_id = identity_mappings.get(raybet_team_id)
        if mapped_team_id is not None:
            matches = [row for row in catalog if int(row[0]) == mapped_team_id]
            used_identity_mapping = True
        else:
            names = {
                str(value).strip().casefold()
                for value in (
                    raw_team.get("team_name"),
                    raw_team.get("team_short_name"),
                    fallback_names[index],
                )
                if str(value or "").strip()
            }
            matches = [
                row
                for row in catalog
                if str(row[1] or "").strip().casefold() in names
                or str(row[2] or "").strip().casefold() in names
            ]
        if len(matches) != 1:
            return [], "raybet_exact_name"
        result.append(
            {
                "match_side": "team_one" if index == 0 else "team_two",
                "team_id": int(matches[0][0]),
                "team_name": str(matches[0][1] or fallback_names[index]),
            }
        )
    return result, (
        "raybet_identity_mapping_v2"
        if used_identity_mapping
        else "raybet_exact_name"
    )


def _raybet_identity_mappings(
    connection: PostgresSession,
    ordered_teams: list[dict[str, Any]],
) -> dict[int, int]:
    team_ids = [int(team.get("team_id") or 0) for team in ordered_teams]
    if len(team_ids) != 2 or any(team_id <= 0 for team_id in team_ids):
        return {}
    relation = connection.execute(
        "SELECT to_regclass('team_identity_mappings_v2')"
    ).fetchone()
    if relation is None or relation[0] is None:
        return {}
    rows = connection.execute(
        """SELECT DISTINCT ON (raybet_team_id)
                  raybet_team_id, canonical_team_id
             FROM team_identity_mappings_v2
            WHERE raybet_team_id IN (?, ?)
            ORDER BY raybet_team_id, observed_at DESC, mapping_id DESC""",
        tuple(team_ids),
    ).fetchall()
    return {int(row[0]): int(row[1]) for row in rows}


def _latest_positional_roster(
    connection: PostgresSession,
    team_id: int,
    cutoff_epoch: int,
) -> dict[str, Any]:
    matches = connection.execute(
        """SELECT match_id
             FROM matches
            WHERE start_time<? AND (radiant_team_id=? OR dire_team_id=?)
            ORDER BY start_time DESC LIMIT 12""",
        (cutoff_epoch, team_id, team_id),
    ).fetchall()
    roster_match_id: int | None = None
    roster_rows: list[Any] = []
    for match in matches:
        rows = connection.execute(
            """SELECT account_id, player_slot
                 FROM match_players
                WHERE match_id=? AND team_id=? AND account_id IS NOT NULL
                ORDER BY player_slot""",
            (int(match[0]), team_id),
        ).fetchall()
        account_ids = [int(row[0]) for row in rows]
        if len(account_ids) == 5 and len(set(account_ids)) == 5:
            roster_match_id = int(match[0])
            roster_rows = list(rows)
            break
    if roster_match_id is None:
        return {"roster_match_id": None, "players": []}

    candidates: list[dict[str, Any]] = []
    for account_id, player_slot in roster_rows:
        role = connection.execute(
            """SELECT assignment.position, assignment.confidence,
                      assignment.assignment_source
                 FROM player_role_assignments AS assignment
                 JOIN matches AS historical
                   ON historical.match_id=assignment.match_id
                WHERE assignment.account_id=?
                  AND assignment.position BETWEEN 1 AND 5
                  AND historical.start_time<?
                ORDER BY historical.start_time DESC,
                         CASE assignment.purpose
                              WHEN 'expected_position' THEN 0 ELSE 1 END,
                         assignment.created_at DESC LIMIT 1""",
            (int(account_id), cutoff_epoch),
        ).fetchone()
        candidates.append(
            {
                "player_id": int(account_id),
                "player_name": _latest_player_name(connection, int(account_id)),
                "player_slot": int(player_slot),
                "position": int(role[0]) if role is not None else None,
                "confidence": float(role[1]) if role is not None else 0.0,
                "position_source": str(role[2]) if role is not None else "roster_order",
            }
        )

    used_positions: set[int] = set()
    unresolved: list[dict[str, Any]] = []
    for player in sorted(
        candidates,
        key=lambda value: (-float(value["confidence"]), int(value["player_slot"])),
    ):
        position = player["position"]
        if isinstance(position, int) and position not in used_positions:
            used_positions.add(position)
        else:
            player["position"] = None
            player["confidence"] = 0.0
            player["position_source"] = "roster_order"
            unresolved.append(player)
    available = iter(sorted(set(range(1, 6)) - used_positions))
    for player in sorted(unresolved, key=lambda value: int(value["player_slot"])):
        player["position"] = next(available)
    for player in candidates:
        player.pop("player_slot")
    return {
        "roster_match_id": roster_match_id,
        "players": sorted(candidates, key=lambda value: int(value["position"])),
    }


def _latest_player_name(
    connection: PostgresSession,
    account_id: int,
) -> str | None:
    row = connection.execute(
        """SELECT COALESCE(
                      NULLIF(facts_json::jsonb ->> 'name', ''),
                      NULLIF(facts_json::jsonb ->> 'personaname', '')
                  ) AS player_name
             FROM player_map_facts
            WHERE account_id=?
            ORDER BY created_at DESC, fact_id DESC LIMIT 1""",
        (account_id,),
    ).fetchone()
    return str(row[0]).strip() if row is not None and str(row[0]).strip() else None


def save_live_draft_mapping(
    connection: PostgresSession,
    *,
    raybet_match_id: str,
    map_number: int,
    slots: Iterable[DraftSlotInput],
    is_locked: bool,
    actor: str,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    normalized = _validated_slots(slots)
    match_id = _required_text(raybet_match_id, "raybet_match_id")
    created_by = _required_text(actor, "actor")
    if not 1 <= map_number <= 5:
        raise ValueError("map_number must be between 1 and 5")
    observed_at = _aware_utc(created_at or datetime.now(timezone.utc)).isoformat()
    lock_key = f"live-draft:{match_id}:{map_number}"
    with connection.transaction():
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (lock_key,),
        )
        row = connection.execute(
            """SELECT COALESCE(MAX(version), 0) + 1
                 FROM live_draft_mappings
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        ).fetchone()
        version = int(row[0])
        source = "manual" if version == 1 else "manual_correction"
        connection.executemany(
            """INSERT INTO live_draft_mappings
               (raybet_match_id, map_number, version, team_id, side, position,
                hero_id, player_id, source, is_locked, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    match_id,
                    map_number,
                    version,
                    slot.team_id,
                    slot.side,
                    slot.position,
                    slot.hero_id,
                    slot.player_id,
                    source,
                    int(is_locked),
                    created_by,
                    observed_at,
                )
                for slot in normalized
            ],
        )
    mapping = latest_live_draft_mapping(
        connection,
        match_id,
        map_number=map_number,
    )
    if mapping is None or mapping["version"] != version:
        raise RuntimeError("saved live draft mapping is unavailable")
    return mapping


def latest_live_draft_mapping(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    map_number: int | None = None,
) -> dict[str, Any] | None:
    match_id = _required_text(raybet_match_id, "raybet_match_id")
    if map_number is None:
        version_row = connection.execute(
            """SELECT map_number, version
                 FROM live_draft_mappings
                WHERE raybet_match_id=?
                ORDER BY map_number DESC, version DESC LIMIT 1""",
            (match_id,),
        ).fetchone()
    else:
        version_row = connection.execute(
            """SELECT map_number, version
                 FROM live_draft_mappings
                WHERE raybet_match_id=? AND map_number=?
                ORDER BY version DESC LIMIT 1""",
            (match_id, map_number),
        ).fetchone()
    if version_row is None:
        return None
    selected_map = int(version_row[0])
    version = int(version_row[1])
    rows = connection.execute(
        """SELECT team_id, side, position, hero_id, player_id, source,
                  is_locked, created_by, created_at
             FROM live_draft_mappings
            WHERE raybet_match_id=? AND map_number=? AND version=?
            ORDER BY CASE side WHEN 'radiant' THEN 0 ELSE 1 END, position""",
        (match_id, selected_map, version),
    ).fetchall()
    if len(rows) != 10:
        return None
    return {
        "raybet_match_id": match_id,
        "map_number": selected_map,
        "version": version,
        "source": str(rows[0][5]),
        "is_locked": bool(rows[0][6]),
        "created_by": str(rows[0][7]),
        "created_at": str(rows[0][8]),
        "slots": [
            {
                "team_id": int(row[0]),
                "side": str(row[1]),
                "position": int(row[2]),
                "hero_id": int(row[3]),
                "player_id": None if row[4] is None else int(row[4]),
            }
            for row in rows
        ],
    }


def append_live_game_snapshot(
    connection: PostgresSession,
    *,
    raybet_match_id: str,
    map_number: int,
    game_time_seconds: int,
    radiant_networth: int,
    dire_networth: int,
    radiant_kills: int | None,
    dire_kills: int | None,
    vision_confidence: float,
    screenshot_path: str | None,
    source: str,
    captured_at: datetime,
    actor: str | None = None,
) -> dict[str, Any]:
    match_id = _required_text(raybet_match_id, "raybet_match_id")
    if not 1 <= map_number <= 5:
        raise ValueError("map_number must be between 1 and 5")
    if type(game_time_seconds) is not int or game_time_seconds < 0:
        raise ValueError("game_time_seconds must be non-negative")
    if type(radiant_networth) is not int or radiant_networth < 0:
        raise ValueError("radiant_networth must be non-negative")
    if type(dire_networth) is not int or dire_networth < 0:
        raise ValueError("dire_networth must be non-negative")
    if source not in {"vision", "manual_correction"}:
        raise ValueError("unsupported live game snapshot source")
    if not 0.0 <= vision_confidence <= 1.0:
        raise ValueError("vision_confidence must be between 0 and 1")
    if source == "vision" and vision_confidence < 0.9:
        raise ValueError("low-confidence Vision snapshots are rejected")
    for value, name in (
        (radiant_kills, "radiant_kills"),
        (dire_kills, "dire_kills"),
    ):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be non-negative")
    captured = _aware_utc(captured_at)
    if source == "vision":
        _validate_vision_progression(
            connection,
            match_id,
            map_number,
            game_time_seconds,
            radiant_networth,
            dire_networth,
        )
    created_at = datetime.now(timezone.utc).isoformat()
    networth_lead = radiant_networth - dire_networth
    with connection.transaction():
        row = connection.execute(
            """INSERT INTO live_game_snapshots
               (raybet_match_id, map_number, game_time_seconds,
                radiant_networth, dire_networth, networth_lead,
                radiant_kills, dire_kills, vision_confidence, screenshot_path,
                source, captured_at, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING RETURNING snapshot_id""",
            (
                match_id,
                map_number,
                game_time_seconds,
                radiant_networth,
                dire_networth,
                networth_lead,
                radiant_kills,
                dire_kills,
                vision_confidence,
                screenshot_path,
                source,
                captured.isoformat(),
                actor,
                created_at,
            ),
        ).fetchone()
    if row is None:
        existing = connection.execute(
            """SELECT snapshot_id FROM live_game_snapshots
                WHERE raybet_match_id=? AND map_number=?
                  AND captured_at=? AND source=?""",
            (match_id, map_number, captured.isoformat(), source),
        ).fetchone()
        if existing is None:
            raise RuntimeError("live game snapshot was not persisted")
        snapshot_id = int(existing[0])
    else:
        snapshot_id = int(row[0])
    snapshot = live_game_snapshot(connection, snapshot_id)
    if snapshot is None:
        raise RuntimeError("live game snapshot is unavailable")
    return snapshot


def live_game_snapshot(
    connection: PostgresSession,
    snapshot_id: int,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM live_game_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    return None if row is None else dict(row)


def live_game_snapshots(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    map_number: int | None = None,
    limit: int = 120,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 1200:
        raise ValueError("limit must be between 1 and 1200")
    params: list[object] = [_required_text(raybet_match_id, "raybet_match_id")]
    map_filter = ""
    if map_number is not None:
        map_filter = "AND map_number=?"
        params.append(map_number)
    params.append(limit)
    rows = connection.execute(
        f"""SELECT * FROM (
                SELECT * FROM live_game_snapshots
                 WHERE raybet_match_id=? {map_filter}
                 ORDER BY captured_at DESC, snapshot_id DESC LIMIT ?
            ) AS recent
            ORDER BY captured_at, snapshot_id""",
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _validated_slots(slots: Iterable[DraftSlotInput]) -> tuple[DraftSlotInput, ...]:
    rows = tuple(slots)
    if len(rows) != 10:
        raise ValueError("exactly ten draft slots are required")
    heroes: set[int] = set()
    players: set[int] = set()
    teams_by_side: dict[str, set[int]] = {"radiant": set(), "dire": set()}
    positions_by_side: dict[str, set[int]] = {"radiant": set(), "dire": set()}
    for slot in rows:
        if slot.side not in teams_by_side:
            raise ValueError("draft side must be radiant or dire")
        if not 1 <= slot.position <= 5:
            raise ValueError("draft position must be between 1 and 5")
        if slot.team_id <= 0 or slot.hero_id <= 0:
            raise ValueError("team_id and hero_id must be positive")
        if slot.player_id is not None and slot.player_id <= 0:
            raise ValueError("player_id must be positive")
        if slot.hero_id in heroes:
            raise ValueError("draft heroes must be globally unique")
        if slot.player_id is not None and slot.player_id in players:
            raise ValueError("draft players must be globally unique")
        if slot.position in positions_by_side[slot.side]:
            raise ValueError("draft positions must be unique per side")
        heroes.add(slot.hero_id)
        if slot.player_id is not None:
            players.add(slot.player_id)
        teams_by_side[slot.side].add(slot.team_id)
        positions_by_side[slot.side].add(slot.position)
    if any(positions != set(range(1, 6)) for positions in positions_by_side.values()):
        raise ValueError("each side must contain positions 1 through 5")
    if any(len(teams) != 1 for teams in teams_by_side.values()):
        raise ValueError("each side must contain exactly one team")
    if teams_by_side["radiant"] == teams_by_side["dire"]:
        raise ValueError("radiant and dire teams must differ")
    return tuple(sorted(rows, key=lambda slot: (slot.side != "radiant", slot.position)))


def _validate_vision_progression(
    connection: PostgresSession,
    match_id: str,
    map_number: int,
    game_time_seconds: int,
    radiant_networth: int,
    dire_networth: int,
) -> None:
    previous = connection.execute(
        """SELECT game_time_seconds, radiant_networth, dire_networth
             FROM live_game_snapshots
            WHERE raybet_match_id=? AND map_number=?
            ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1""",
        (match_id, map_number),
    ).fetchone()
    if previous is None:
        return
    previous_clock = int(previous[0])
    if game_time_seconds < previous_clock - 3:
        raise ValueError("Vision game time moved backwards")
    clock_delta = max(0, game_time_seconds - previous_clock)
    maximum_change = max(5000, clock_delta * 1000 + 2000)
    for current, prior in (
        (radiant_networth, int(previous[1])),
        (dire_networth, int(previous[2])),
    ):
        if current < prior - 1000:
            raise ValueError("Vision team networth decreased unexpectedly")
        if abs(current - prior) > maximum_change:
            raise ValueError("Vision team networth jumped unexpectedly")


def _required_text(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
