from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import queries
from ..schemas import HeroDetail, HeroStats

router = APIRouter(prefix="/api/heroes", tags=["heroes"])


@router.get("", response_model=list[HeroStats])
def list_heroes():
    rows = queries.get_heroes()
    result = []
    for r in rows:
        match_count = r.get("match_count", 0)
        win_count = r.get("win_count", 0)
        result.append(HeroStats(
            hero_id=r.get("hero_id"),
            localized_name=r.get("localized_name", "Unknown"),
            match_count=match_count,
            win_count=win_count,
            win_rate=round(win_count / match_count, 4) if match_count > 0 else 0.0,
            pick_count=r.get("pick_count", 0),
            ban_count=r.get("ban_count", 0),
        ))
    return result


@router.get("/{hero_id}", response_model=HeroDetail)
def hero_detail(hero_id: int):
    data = queries.get_hero_detail(hero_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Hero not found")

    from ..schemas import MatchSummary

    return HeroDetail(
        hero_id=data.get("hero_id"),
        localized_name=data.get("localized_name", "Unknown"),
        primary_attr=data.get("primary_attr"),
        attack_type=data.get("attack_type"),
        roles=data.get("roles", []),
        match_count=data.get("match_count", 0),
        win_count=data.get("win_count", 0),
        win_rate=data.get("win_rate", 0.0),
        avg_kills=data.get("avg_kills", 0.0),
        avg_deaths=data.get("avg_deaths", 0.0),
        avg_assists=data.get("avg_assists", 0.0),
        avg_gpm=data.get("avg_gpm", 0.0),
        avg_xpm=data.get("avg_xpm", 0.0),
        recent_matches=[MatchSummary(**m) for m in (data.get("recent_matches") or [])],
    )
