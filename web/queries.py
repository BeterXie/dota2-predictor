from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from database.engine import build_engine, require_database_url
from database.session import PostgresSession

logger = logging.getLogger(__name__)

DATABASE_URL: str | None = None
_ENGINE: Engine | None = None


def init_db(database_url: str | None = None) -> None:
    """Configure the PostgreSQL engine once at application startup."""
    global DATABASE_URL, _ENGINE
    configured = require_database_url(database_url)
    if _ENGINE is not None:
        _ENGINE.dispose()
    DATABASE_URL = configured
    _ENGINE = build_engine(configured)


def get_db() -> PostgresSession:
    global DATABASE_URL, _ENGINE
    if _ENGINE is None:
        init_db()
    assert _ENGINE is not None
    return PostgresSession(_ENGINE)


def _safe_execute(query: str, params: tuple = (), fetch: str = "all") -> Any:
    """Execute a query and close its session before returning or raising."""
    conn = get_db()
    try:
        cur = conn.execute(query, params)
        if fetch == "all":
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        elif fetch == "one":
            row = cur.fetchone()
            return dict(row) if row else None
        elif fetch == "value":
            row = cur.fetchone()
            return row[0] if row else None
        raise ValueError(f"unsupported fetch mode: {fetch}")
    except SQLAlchemyError:
        logger.exception("PostgreSQL query failed: %s", query[:80])
        raise
    finally:
        conn.close()



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


def get_team_grid() -> list[dict[str, object]]:
    """Return canonical teams for explicit operator selection."""
    rows = _safe_execute(
        """SELECT team_id, name, tag
             FROM teams
            WHERE team_id > 0
              AND (NULLIF(BTRIM(name), '') IS NOT NULL
                   OR NULLIF(BTRIM(tag), '') IS NOT NULL)
            ORDER BY COALESCE(NULLIF(BTRIM(name), ''), BTRIM(tag)), team_id""",
        fetch="all",
    )
    return [
        {
            "team_id": int(row["team_id"]),
            "team_name": str(row.get("name") or row.get("tag") or "").strip(),
            "tag": str(row.get("tag") or "").strip() or None,
        }
        for row in rows
    ]
