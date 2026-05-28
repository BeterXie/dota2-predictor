from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException, Query

from .. import queries
from ..schemas import MatchSummary, PaginatedResponse, PaginationMeta, TeamBase, TeamProfile

router = APIRouter(prefix="/api/teams", tags=["teams"])


@router.get("", response_model=list[TeamBase])
def list_teams():
    rows = queries.get_teams()
    return [TeamBase(**r) for r in rows]


@router.get("/{team_id}", response_model=TeamProfile)
def team_profile(team_id: int):
    data = queries.get_team_profile(team_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return TeamProfile(
        team_id=data.get("team_id"),
        name=data.get("name"),
        tag=data.get("tag"),
        logo_url=data.get("logo_url"),
        total_matches=data.get("total_matches", 0),
        wins=data.get("wins", 0),
        losses=data.get("losses", 0),
        win_rate=data.get("win_rate", 0.0),
        avg_duration=data.get("avg_duration", 0.0),
        avg_kills=data.get("avg_kills", 0.0),
        avg_deaths=data.get("avg_deaths", 0.0),
        avg_assists=data.get("avg_assists", 0.0),
        avg_gpm=data.get("avg_gpm", 0.0),
        avg_xpm=data.get("avg_xpm", 0.0),
        recent_matches=[MatchSummary(**m) for m in (data.get("recent_matches") or [])],
    )


@router.get("/{team_id}/matches", response_model=PaginatedResponse)
def team_matches(
    team_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    rows, total = queries.get_team_matches(team_id, page, page_size)
    total_pages = max(1, math.ceil(total / page_size)) if total > 0 else 1
    return PaginatedResponse(
        data=[MatchSummary(**r) for r in rows],
        pagination=PaginationMeta(page=page, page_size=page_size, total=total, total_pages=total_pages),
    )
