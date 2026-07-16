from __future__ import annotations

import math

from fastapi import APIRouter, Query

from .. import queries
from ..schemas import LeagueBase, MatchSummary, PaginatedResponse, PaginationMeta

router = APIRouter(prefix="/api/leagues", tags=["leagues"])


@router.get("", response_model=list[LeagueBase])
def list_leagues():
    rows = queries.get_leagues()
    return [LeagueBase(**r) for r in rows]


@router.get("/{league_id}/matches", response_model=PaginatedResponse)
def league_matches(
    league_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    rows, total = queries.get_league_matches(league_id, page, page_size)
    total_pages = max(1, math.ceil(total / page_size)) if total > 0 else 1
    return PaginatedResponse(
        data=[MatchSummary(**r) for r in rows],
        pagination=PaginationMeta(page=page, page_size=page_size, total=total, total_pages=total_pages),
    )
