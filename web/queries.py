from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Use the web config.yaml path, falling back to env var, then computed default.
_WEB_DIR = Path(__file__).resolve().parent
_DEFAULT_DB = str(_WEB_DIR.parent / "data" / "dota2.db")
DB_PATH = os.environ.get("DATABASE_PATH", _DEFAULT_DB)


def init_db(db_path: str | None = None) -> None:
    """Configure the database path. Call once at application startup."""
    global DB_PATH
    if db_path:
        DB_PATH = db_path


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _safe_execute(query: str, params: tuple = (), fetch: str = "all") -> Any:
    """Execute a query, returning []/None on missing tables."""
    try:
        conn = get_db()
        cur = conn.execute(query, params)
        if fetch == "all":
            rows = cur.fetchall()
            conn.close()
            return [dict(r) for r in rows]
        elif fetch == "one":
            row = cur.fetchone()
            conn.close()
            return dict(row) if row else None
        elif fetch == "value":
            row = cur.fetchone()
            conn.close()
            return row[0] if row else None
    except sqlite3.OperationalError:
        logger.warning("Query failed (table may not exist): %s", query[:80])
        return [] if fetch == "all" else None


def _parse_date_to_ts(date_str: str) -> int:
    """Parse an ISO date string (e.g. '2025-01-01' or '2025-01-01T00:00:00')
    to a Unix timestamp. Accepts bare Unix timestamp strings for backwards
    compatibility."""
    try:
        ts = int(date_str)
        return ts
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(date_str)
    except ValueError:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# --- Matches ---

def get_matches(
    page: int = 1,
    page_size: int = 20,
    team_id: int | None = None,
    league_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[list[dict], int]:
    where: list[str] = []
    params: list[Any] = []

    if team_id is not None:
        where.append("(m.radiant_team_id = ? OR m.dire_team_id = ?)")
        params.extend([team_id, team_id])
    if league_id is not None:
        where.append("m.leagueid = ?")
        params.append(league_id)
    if date_from is not None:
        where.append("m.start_time >= ?")
        params.append(_parse_date_to_ts(date_from))
    if date_to is not None:
        where.append("m.start_time <= ?")
        params.append(_parse_date_to_ts(date_to))

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    count_query = f"SELECT COUNT(*) FROM matches m {where_clause}"

    total = _safe_execute(count_query, tuple(params), fetch="value") or 0
    total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 1

    data_query = f"""
        SELECT m.match_id, m.radiant_team_id, m.dire_team_id, m.radiant_win,
               m.duration, m.start_time, m.leagueid, m.radiant_score, m.dire_score,
               rt.name AS radiant_team_name, dt.name AS dire_team_name,
               l.name AS league_name
        FROM matches m
        LEFT JOIN teams rt ON m.radiant_team_id = rt.team_id
        LEFT JOIN teams dt ON m.dire_team_id = dt.team_id
        LEFT JOIN leagues l ON m.leagueid = l.leagueid
        {where_clause}
        ORDER BY m.start_time DESC
        LIMIT ? OFFSET ?
    """
    offset = (page - 1) * page_size
    params.extend([page_size, offset])
    rows = _safe_execute(data_query, tuple(params), fetch="all")
    return rows, total


def get_match_detail(match_id: int) -> dict | None:
    conn = get_db()
    try:
        match = conn.execute("""
            SELECT m.*, rt.name AS radiant_team_name, dt.name AS dire_team_name,
                   l.name AS league_name
            FROM matches m
            LEFT JOIN teams rt ON m.radiant_team_id = rt.team_id
            LEFT JOIN teams dt ON m.dire_team_id = dt.team_id
            LEFT JOIN leagues l ON m.leagueid = l.leagueid
            WHERE m.match_id = ?
        """, (match_id,)).fetchone()

        if not match:
            conn.close()
            return None

        result = dict(match)

        players = conn.execute("""
            SELECT mp.*, h.localized_name AS hero_name
            FROM match_players mp
            LEFT JOIN heroes h ON mp.hero_id = h.hero_id
            WHERE mp.match_id = ?
            ORDER BY mp.player_slot
        """, (match_id,)).fetchall()

        result["players"] = []
        for p in players:
            pd = dict(p)
            pd["items"] = [
                pd.pop("item_0", 0), pd.pop("item_1", 0), pd.pop("item_2", 0),
                pd.pop("item_3", 0), pd.pop("item_4", 0), pd.pop("item_5", 0),
            ]
            result["players"].append(pd)

        picks_bans = conn.execute("""
            SELECT pb.*, h.localized_name AS hero_name
            FROM picks_bans pb
            LEFT JOIN heroes h ON pb.hero_id = h.hero_id
            WHERE pb.match_id = ?
            ORDER BY pb.ord
        """, (match_id,)).fetchall()
        result["picks_bans"] = [dict(pb) for pb in picks_bans]

        gold_adv = conn.execute("""
            SELECT time_min, value FROM gold_advantage
            WHERE match_id = ? ORDER BY time_min
        """, (match_id,)).fetchall()
        result["gold_advantage"] = [dict(g) for g in gold_adv]

        conn.close()
        return result
    except sqlite3.OperationalError:
        conn.close()
        return None


# --- Teams ---

def get_teams() -> list[dict]:
    return _safe_execute("""
        SELECT t.team_id, t.name, t.tag, t.logo_url,
               COUNT(DISTINCT m.match_id) AS match_count
        FROM teams t
        LEFT JOIN matches m ON t.team_id IN (m.radiant_team_id, m.dire_team_id)
        GROUP BY t.team_id
        ORDER BY match_count DESC, t.name
    """, fetch="all")


def get_team_profile(team_id: int) -> dict | None:
    conn = get_db()
    try:
        team = conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
        if not team:
            conn.close()
            return None

        result = dict(team)

        # Overall stats
        stats = conn.execute("""
            SELECT
                COUNT(*) AS total_matches,
                SUM(CASE
                    WHEN (m.radiant_team_id = ? AND m.radiant_win = 1)
                      OR (m.dire_team_id = ? AND m.radiant_win = 0) THEN 1 ELSE 0
                END) AS wins,
                SUM(CASE
                    WHEN (m.radiant_team_id = ? AND m.radiant_win = 0)
                      OR (m.dire_team_id = ? AND m.radiant_win = 1) THEN 1 ELSE 0
                END) AS losses,
                AVG(m.duration) AS avg_duration
            FROM matches m
            WHERE m.radiant_team_id = ? OR m.dire_team_id = ?
        """, (team_id, team_id, team_id, team_id, team_id, team_id)).fetchone()

        if stats:
            result["total_matches"] = stats["total_matches"] or 0
            result["wins"] = stats["wins"] or 0
            result["losses"] = stats["losses"] or 0
            result["win_rate"] = round(result["wins"] / result["total_matches"], 4) if result["total_matches"] > 0 else 0.0
            result["avg_duration"] = round(stats["avg_duration"], 1) if stats["avg_duration"] else 0.0
        else:
            result["total_matches"] = result["wins"] = result["losses"] = 0
            result["win_rate"] = result["avg_duration"] = 0.0

        # Aggregated player stats across all matches for this team
        agg = conn.execute("""
            SELECT
                AVG(mp.kills) AS avg_kills,
                AVG(mp.deaths) AS avg_deaths,
                AVG(mp.assists) AS avg_assists,
                AVG(mp.gold_per_min) AS avg_gpm,
                AVG(mp.xp_per_min) AS avg_xpm
            FROM match_players mp
            JOIN matches m ON mp.match_id = m.match_id
            WHERE mp.team_id = ?
              AND m.match_id IN (
                SELECT match_id FROM matches WHERE radiant_team_id = ? OR dire_team_id = ?
              )
        """, (team_id, team_id, team_id)).fetchone()

        for col in ("avg_kills", "avg_deaths", "avg_assists", "avg_gpm", "avg_xpm"):
            result[col] = round(agg[col], 1) if agg and agg[col] is not None else 0.0

        # Recent matches
        recent = conn.execute("""
            SELECT m.match_id, m.radiant_team_id, m.dire_team_id, m.radiant_win,
                   m.duration, m.start_time, m.leagueid, m.radiant_score, m.dire_score,
                   rt.name AS radiant_team_name, dt.name AS dire_team_name,
                   l.name AS league_name
            FROM matches m
            LEFT JOIN teams rt ON m.radiant_team_id = rt.team_id
            LEFT JOIN teams dt ON m.dire_team_id = dt.team_id
            LEFT JOIN leagues l ON m.leagueid = l.leagueid
            WHERE m.radiant_team_id = ? OR m.dire_team_id = ?
            ORDER BY m.start_time DESC
            LIMIT 20
        """, (team_id, team_id)).fetchall()

        result["recent_matches"] = [dict(r) for r in recent]
        conn.close()
        return result
    except sqlite3.OperationalError:
        conn.close()
        return None


def get_team_matches(team_id: int, page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    total = _safe_execute(
        "SELECT COUNT(*) FROM matches WHERE radiant_team_id = ? OR dire_team_id = ?",
        (team_id, team_id), fetch="value"
    ) or 0

    offset = (page - 1) * page_size
    rows = _safe_execute("""
        SELECT m.match_id, m.radiant_team_id, m.dire_team_id, m.radiant_win,
               m.duration, m.start_time, m.leagueid, m.radiant_score, m.dire_score,
               rt.name AS radiant_team_name, dt.name AS dire_team_name,
               l.name AS league_name
        FROM matches m
        LEFT JOIN teams rt ON m.radiant_team_id = rt.team_id
        LEFT JOIN teams dt ON m.dire_team_id = dt.team_id
        LEFT JOIN leagues l ON m.leagueid = l.leagueid
        WHERE m.radiant_team_id = ? OR m.dire_team_id = ?
        ORDER BY m.start_time DESC
        LIMIT ? OFFSET ?
    """, (team_id, team_id, page_size, offset), fetch="all")
    return rows, total


# --- Leagues ---

def get_leagues() -> list[dict]:
    return _safe_execute("""
        SELECT l.leagueid, l.name, l.tier,
               COUNT(DISTINCT m.match_id) AS match_count
        FROM leagues l
        LEFT JOIN matches m ON l.leagueid = m.leagueid
        GROUP BY l.leagueid
        ORDER BY match_count DESC, l.name
    """, fetch="all")


def get_league_matches(league_id: int, page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    total = _safe_execute(
        "SELECT COUNT(*) FROM matches WHERE leagueid = ?", (league_id,), fetch="value"
    ) or 0

    offset = (page - 1) * page_size
    rows = _safe_execute("""
        SELECT m.match_id, m.radiant_team_id, m.dire_team_id, m.radiant_win,
               m.duration, m.start_time, m.leagueid, m.radiant_score, m.dire_score,
               rt.name AS radiant_team_name, dt.name AS dire_team_name,
               l.name AS league_name
        FROM matches m
        LEFT JOIN teams rt ON m.radiant_team_id = rt.team_id
        LEFT JOIN teams dt ON m.dire_team_id = dt.team_id
        LEFT JOIN leagues l ON m.leagueid = l.leagueid
        WHERE m.leagueid = ?
        ORDER BY m.start_time DESC
        LIMIT ? OFFSET ?
    """, (league_id, page_size, offset), fetch="all")
    return rows, total


# --- Heroes ---

def get_heroes() -> list[dict]:
    return _safe_execute("""
        SELECT h.hero_id, h.localized_name, h.primary_attr, h.attack_type, h.roles,
               COUNT(DISTINCT mp.match_id) AS match_count,
               COUNT(DISTINCT CASE WHEN m.radiant_win = mp.is_radiant THEN mp.match_id END) AS win_count,
               COALESCE(pb_counts.pick_count, 0) AS pick_count,
               COALESCE(pb_counts.ban_count, 0) AS ban_count
        FROM heroes h
        LEFT JOIN match_players mp ON h.hero_id = mp.hero_id
        LEFT JOIN matches m ON mp.match_id = m.match_id
        LEFT JOIN (
            SELECT hero_id,
                   COUNT(CASE WHEN is_pick = 1 THEN 1 END) AS pick_count,
                   COUNT(CASE WHEN is_pick = 0 THEN 1 END) AS ban_count
            FROM picks_bans
            GROUP BY hero_id
        ) pb_counts ON h.hero_id = pb_counts.hero_id
        GROUP BY h.hero_id
        ORDER BY match_count DESC, h.localized_name
    """, fetch="all")


def get_hero_grid() -> dict[str, list[dict]]:
    """Return heroes grouped by primary_attr with image URLs for the hero picker."""
    rows = _safe_execute("""
        SELECT hero_id, localized_name, primary_attr,
               COALESCE(NULLIF(hero_key, ''), '') AS hero_key
        FROM heroes
        ORDER BY localized_name
    """, fetch="all")
    grouped: dict[str, list[dict]] = {"str": [], "agi": [], "int": [], "all": []}
    IMG_BASE = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes"
    for r in rows:
        attr = r.get("primary_attr", "all") or "all"
        key = r.get("hero_key", "")
        grouped.setdefault(attr, []).append({
            "hero_id": r["hero_id"],
            "localized_name": r["localized_name"],
            "hero_key": key,
            "image_url": f"{IMG_BASE}/{key}.png" if key else "",
        })
    return grouped


def get_hero_detail(hero_id: int) -> dict | None:
    conn = get_db()
    try:
        hero = conn.execute("SELECT * FROM heroes WHERE hero_id = ?", (hero_id,)).fetchone()
        if not hero:
            conn.close()
            return None

        result = dict(hero)
        import json
        try:
            result["roles"] = json.loads(result.get("roles", "[]"))
        except (json.JSONDecodeError, TypeError):
            # Fallback for legacy Python-repr formatted roles
            try:
                import ast
                result["roles"] = ast.literal_eval(result.get("roles", "[]"))
            except (ValueError, SyntaxError):
                result["roles"] = []

        stats = conn.execute("""
            SELECT
                COUNT(DISTINCT mp.match_id) AS match_count,
                COUNT(DISTINCT CASE WHEN m.radiant_win = mp.is_radiant THEN mp.match_id END) AS win_count,
                AVG(mp.kills) AS avg_kills,
                AVG(mp.deaths) AS avg_deaths,
                AVG(mp.assists) AS avg_assists,
                AVG(mp.gold_per_min) AS avg_gpm,
                AVG(mp.xp_per_min) AS avg_xpm
            FROM match_players mp
            JOIN matches m ON mp.match_id = m.match_id
            WHERE mp.hero_id = ?
        """, (hero_id,)).fetchone()

        result["match_count"] = stats["match_count"] or 0 if stats else 0
        result["win_count"] = stats["win_count"] or 0 if stats else 0
        result["win_rate"] = round(result["win_count"] / result["match_count"], 4) if result["match_count"] > 0 else 0.0
        for col in ("avg_kills", "avg_deaths", "avg_assists", "avg_gpm", "avg_xpm"):
            result[col] = round(stats[col], 1) if stats and stats[col] is not None else 0.0

        # Pick/ban counts
        pb_stats = conn.execute("""
            SELECT
                COUNT(CASE WHEN is_pick = 1 THEN 1 END) AS pick_count,
                COUNT(CASE WHEN is_pick = 0 THEN 1 END) AS ban_count
            FROM picks_bans
            WHERE hero_id = ?
        """, (hero_id,)).fetchone()
        result["pick_count"] = pb_stats["pick_count"] or 0 if pb_stats else 0
        result["ban_count"] = pb_stats["ban_count"] or 0 if pb_stats else 0

        # Recent matches with this hero
        recent = conn.execute("""
            SELECT m.match_id, m.radiant_team_id, m.dire_team_id, m.radiant_win,
                   m.duration, m.start_time, m.leagueid, m.radiant_score, m.dire_score,
                   rt.name AS radiant_team_name, dt.name AS dire_team_name,
                   l.name AS league_name
            FROM match_players mp
            JOIN matches m ON mp.match_id = m.match_id
            LEFT JOIN teams rt ON m.radiant_team_id = rt.team_id
            LEFT JOIN teams dt ON m.dire_team_id = dt.team_id
            LEFT JOIN leagues l ON m.leagueid = l.leagueid
            WHERE mp.hero_id = ?
            ORDER BY m.start_time DESC
            LIMIT 20
        """, (hero_id,)).fetchall()
        result["recent_matches"] = [dict(r) for r in recent]

        conn.close()
        return result
    except sqlite3.OperationalError:
        conn.close()
        return None


# --- Head-to-Head ---

def get_head_to_head(team_a: int, team_b: int) -> dict:
    conn = get_db()
    try:
        team_a_info = conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_a,)).fetchone()
        team_b_info = conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_b,)).fetchone()

        result = {
            "team_a": dict(team_a_info) if team_a_info else {"team_id": team_a, "name": None, "tag": None, "logo_url": None},
            "team_b": dict(team_b_info) if team_b_info else {"team_id": team_b, "name": None, "tag": None, "logo_url": None},
        }

        h2h = conn.execute("""
            SELECT
                COUNT(*) AS total_matches,
                SUM(CASE
                    WHEN (m.radiant_team_id = ? AND m.radiant_win = 1)
                      OR (m.dire_team_id = ? AND m.radiant_win = 0) THEN 1 ELSE 0
                END) AS team_a_wins,
                SUM(CASE
                    WHEN (m.radiant_team_id = ? AND m.radiant_win = 1)
                      OR (m.dire_team_id = ? AND m.radiant_win = 0) THEN 1 ELSE 0
                END) AS team_b_wins,
                AVG(m.duration) AS avg_duration
            FROM matches m
            WHERE (m.radiant_team_id = ? AND m.dire_team_id = ?)
               OR (m.radiant_team_id = ? AND m.dire_team_id = ?)
        """, (team_a, team_a, team_b, team_b, team_a, team_b, team_b, team_a)).fetchone()

        if h2h:
            result["total_matches"] = h2h["total_matches"] or 0
            result["team_a_wins"] = h2h["team_a_wins"] or 0
            result["team_b_wins"] = h2h["team_b_wins"] or 0
            result["team_a_win_rate"] = round(result["team_a_wins"] / result["total_matches"], 4) if result["total_matches"] > 0 else 0.0
            result["avg_duration"] = round(h2h["avg_duration"], 1) if h2h["avg_duration"] else 0.0
        else:
            result["total_matches"] = result["team_a_wins"] = result["team_b_wins"] = 0
            result["team_a_win_rate"] = result["avg_duration"] = 0.0

        # Recent encounters
        recent = conn.execute("""
            SELECT m.match_id, m.radiant_team_id, m.dire_team_id, m.radiant_win,
                   m.duration, m.start_time, m.leagueid, m.radiant_score, m.dire_score,
                   rt.name AS radiant_team_name, dt.name AS dire_team_name,
                   l.name AS league_name
            FROM matches m
            LEFT JOIN teams rt ON m.radiant_team_id = rt.team_id
            LEFT JOIN teams dt ON m.dire_team_id = dt.team_id
            LEFT JOIN leagues l ON m.leagueid = l.leagueid
            WHERE (m.radiant_team_id = ? AND m.dire_team_id = ?)
               OR (m.radiant_team_id = ? AND m.dire_team_id = ?)
            ORDER BY m.start_time DESC
            LIMIT 20
        """, (team_a, team_b, team_b, team_a)).fetchall()
        result["recent_encounters"] = [dict(r) for r in recent]

        conn.close()
        return result
    except sqlite3.OperationalError:
        conn.close()
        return {
            "team_a": {"team_id": team_a, "name": None, "tag": None, "logo_url": None},
            "team_b": {"team_id": team_b, "name": None, "tag": None, "logo_url": None},
            "total_matches": 0, "team_a_wins": 0, "team_b_wins": 0,
            "team_a_win_rate": 0.0, "avg_duration": 0.0,
            "recent_encounters": [],
        }
