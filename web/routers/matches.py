from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException, Query

from .. import queries
from ..schemas import GoldAdvantagePoint, MatchDetail, MatchPlayer, MatchSummary, PaginatedResponse, PaginationMeta, PickBan

router = APIRouter(prefix="/api/matches", tags=["matches"])


@router.get("", response_model=PaginatedResponse)
def list_matches(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    team_id: int | None = None,
    league_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    rows, total = queries.get_matches(page, page_size, team_id, league_id, date_from, date_to)
    total_pages = max(1, math.ceil(total / page_size)) if total > 0 else 1

    return PaginatedResponse(
        data=[MatchSummary(**r) for r in rows],
        pagination=PaginationMeta(page=page, page_size=page_size, total=total, total_pages=total_pages),
    )


@router.get("/{match_id}", response_model=MatchDetail)
def match_detail(match_id: int):
    data = queries.get_match_detail(match_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Match not found")

    return MatchDetail(
        match_id=data.get("match_id"),
        radiant_team_id=data.get("radiant_team_id"),
        dire_team_id=data.get("dire_team_id"),
        radiant_team_name=data.get("radiant_team_name"),
        dire_team_name=data.get("dire_team_name"),
        radiant_win=bool(data.get("radiant_win")) if data.get("radiant_win") is not None else None,
        duration=data.get("duration"),
        game_mode=data.get("game_mode"),
        start_time=data.get("start_time"),
        first_blood_time=data.get("first_blood_time"),
        leagueid=data.get("leagueid"),
        league_name=data.get("league_name"),
        series_id=data.get("series_id"),
        series_type=data.get("series_type"),
        patch=data.get("patch"),
        region=data.get("region"),
        radiant_score=data.get("radiant_score"),
        dire_score=data.get("dire_score"),
        players=[MatchPlayer(**p) for p in (data.get("players") or [])],
        picks_bans=[PickBan(**pb) for pb in (data.get("picks_bans") or [])],
        gold_advantage=[GoldAdvantagePoint(**g) for g in (data.get("gold_advantage") or [])],
    )
