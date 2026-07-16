from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import intelligence


router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


@router.get("/overview")
def overview():
    return intelligence.get_overview()


@router.get("/matches")
def matches(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, max_length=100),
    label: str | None = Query(None, max_length=32),
    team_id: int | None = None,
):
    return intelligence.list_matches(
        page=page,
        page_size=page_size,
        search=search,
        label=label,
        team_id=team_id,
    )


@router.get("/matches/{match_id}")
def match(match_id: int):
    result = intelligence.get_match(match_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return result


@router.get("/players")
def players(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    position: int | None = Query(None, ge=1, le=5),
    search: str | None = Query(None, max_length=100),
):
    return intelligence.list_players(
        page=page,
        page_size=page_size,
        position=position,
        search=search,
    )


@router.get("/teams")
def teams():
    return intelligence.list_teams()
