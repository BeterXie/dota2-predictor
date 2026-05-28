from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# --- Pagination ---

class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class PaginatedResponse(BaseModel):
    data: list[Any]
    pagination: PaginationMeta


# --- Heroes ---

class HeroBase(BaseModel):
    hero_id: int
    localized_name: str
    primary_attr: str | None = None
    attack_type: str | None = None
    roles: list[str] | None = None


class HeroStats(BaseModel):
    hero_id: int
    localized_name: str
    match_count: int = 0
    win_count: int = 0
    win_rate: float = 0.0
    pick_count: int = 0
    ban_count: int = 0


class HeroDetail(HeroBase):
    match_count: int = 0
    win_count: int = 0
    win_rate: float = 0.0
    avg_kills: float = 0.0
    avg_deaths: float = 0.0
    avg_assists: float = 0.0
    avg_gpm: float = 0.0
    avg_xpm: float = 0.0
    recent_matches: list[MatchSummary] = []


# --- Leagues ---

class LeagueBase(BaseModel):
    leagueid: int
    name: str | None = None
    tier: str | None = None


# --- Teams ---

class TeamBase(BaseModel):
    team_id: int
    name: str | None = None
    tag: str | None = None
    logo_url: str | None = None
    match_count: int = 0


class TeamProfile(TeamBase):
    total_matches: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_duration: float = 0.0
    avg_kills: float = 0.0
    avg_deaths: float = 0.0
    avg_assists: float = 0.0
    avg_gpm: float = 0.0
    avg_xpm: float = 0.0
    recent_matches: list[MatchSummary] = []


class H2HComparison(BaseModel):
    team_a: TeamBase
    team_b: TeamBase
    total_matches: int = 0
    team_a_wins: int = 0
    team_b_wins: int = 0
    team_a_win_rate: float = 0.0
    avg_duration: float = 0.0
    recent_encounters: list[MatchSummary] = []


# --- Matches ---

class MatchSummary(BaseModel):
    match_id: int
    radiant_team_id: int | None = None
    dire_team_id: int | None = None
    radiant_team_name: str | None = None
    dire_team_name: str | None = None
    radiant_win: bool | None = None
    duration: int | None = None
    start_time: int | None = None
    leagueid: int | None = None
    league_name: str | None = None
    radiant_score: int | None = None
    dire_score: int | None = None


class MatchPlayer(BaseModel):
    account_id: int | None = None
    player_slot: int
    hero_id: int | None = None
    hero_name: str | None = None
    is_radiant: bool
    team_id: int | None = None
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    gold_per_min: int = 0
    xp_per_min: int = 0
    net_worth: int = 0
    last_hits: int = 0
    denies: int = 0
    hero_damage: int = 0
    hero_healing: int = 0
    tower_damage: int = 0
    level: int = 0
    items: list[int] = []


class PickBan(BaseModel):
    hero_id: int | None = None
    hero_name: str | None = None
    is_pick: bool
    team: int
    ord: int


class GoldAdvantagePoint(BaseModel):
    time_min: int
    value: int


class MatchDetail(BaseModel):
    match_id: int
    radiant_team_id: int | None = None
    dire_team_id: int | None = None
    radiant_team_name: str | None = None
    dire_team_name: str | None = None
    radiant_win: bool | None = None
    duration: int | None = None
    game_mode: int | None = None
    start_time: int | None = None
    first_blood_time: int | None = None
    leagueid: int | None = None
    league_name: str | None = None
    series_id: int | None = None
    series_type: int | None = None
    patch: int | None = None
    region: int | None = None
    radiant_score: int | None = None
    dire_score: int | None = None
    players: list[MatchPlayer] = []
    picks_bans: list[PickBan] = []
    gold_advantage: list[GoldAdvantagePoint] = []


# --- Predictions ---

class PredictionRequest(BaseModel):
    team_a: int
    team_b: int
    league_id: int | None = None


class PrematchRequest(BaseModel):
    radiant_id: int
    dire_id: int
    radiant_heroes: list[int]
    dire_heroes: list[int]
    league_id: int | None = None


class PredictionFactor(BaseModel):
    factor: str
    impact: float
    direction: str


class PredictionModel(BaseModel):
    version: str
    auc: float
    accuracy: float


class PredictionResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    prediction_id: str
    created_at: str
    match: dict[str, Any]
    prediction: dict[str, Any]
    model_info: dict[str, Any] | None = None
