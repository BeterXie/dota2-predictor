from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    match_count: int = 0


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
    radiant_heroes: list[int] = Field(min_length=5, max_length=5)
    dire_heroes: list[int] = Field(min_length=5, max_length=5)
    league_id: int | None = None
    source_match_id: int | None = Field(default=None, gt=0)
    radiant_players: list[int] | None = Field(default=None, min_length=5, max_length=5)
    dire_players: list[int] | None = Field(default=None, min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_lineups(self) -> "PrematchRequest":
        heroes = self.radiant_heroes + self.dire_heroes
        if any(hero_id <= 0 for hero_id in heroes) or len(set(heroes)) != 10:
            raise ValueError("draft must contain 10 distinct positive hero IDs")
        player_sides = (self.radiant_players, self.dire_players)
        if (player_sides[0] is None) != (player_sides[1] is None):
            raise ValueError("player rosters must be provided for both sides or neither")
        if self.radiant_players is not None and self.dire_players is not None:
            players = self.radiant_players + self.dire_players
            if any(account_id <= 0 for account_id in players) or len(set(players)) != 10:
                raise ValueError("rosters must contain 10 distinct positive account IDs")
            if self.source_match_id is None:
                raise ValueError("player rosters require a trusted source match")
        return self


class RoshAnalysisDraftSlot(BaseModel):
    hero_id: int = Field(gt=0)
    position_id: int = Field(ge=1, le=5)


class RoshAnalysisRequest(BaseModel):
    mode: Literal["historical_match", "explicit_draft"]
    date_time: int = Field(gt=0)
    bracket_ids: list[Literal["IMMORTAL"]] = Field(
        default_factory=lambda: ["IMMORTAL"],
        min_length=1,
        max_length=1,
    )
    rosh_profile_id: str = "stratz-rosh-web-2026-07-28-v2"
    match_id: int | None = Field(default=None, gt=0)
    radiant: list[RoshAnalysisDraftSlot] = Field(default_factory=list)
    dire: list[RoshAnalysisDraftSlot] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mode(self) -> "RoshAnalysisRequest":
        if self.mode == "historical_match":
            if self.match_id is None or self.radiant or self.dire:
                raise ValueError(
                    "historical_match requires match_id and no explicit draft"
                )
            return self
        if self.match_id is not None:
            raise ValueError("explicit_draft must not include match_id")
        rows = self.radiant + self.dire
        if len(self.radiant) != 5 or len(self.dire) != 5:
            raise ValueError("explicit_draft requires five heroes per side")
        if len({row.hero_id for row in rows}) != 10:
            raise ValueError("explicit_draft requires ten distinct heroes")
        for side in (self.radiant, self.dire):
            if {row.position_id for row in side} != set(range(1, 6)):
                raise ValueError("each side must cover positions 1 through 5")
        return self


class RoshAnalysisHeroComponent(BaseModel):
    team_side: Literal["RADIANT", "DIRE"]
    position_id: int
    hero_id: int
    position_base_diff: float
    same_team_synergy: float
    opponent_matchup_synergy: float
    raw_score: float
    display_score: float


class RoshAnalysisMinutePoint(BaseModel):
    minute: int
    radiant_time_delta: float
    dire_time_delta: float
    synergy_delta: float
    raw_score: float
    display_score: float
    rank_source_counts: dict[str, int]
    slots: list[dict[str, Any]]


class RoshAnalysisRunResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: Literal["rosh-analysis-run/v1"] = Field(
        default="rosh-analysis-run/v1",
        alias="schema",
    )
    run_id: str
    status: Literal["succeeded", "failed"]
    mode: Literal["historical_match", "explicit_draft"]
    match_id: int | None
    date_time: int
    draft_hash: str
    rosh_profile_id: str
    formula_version: str
    request_profile_hash: str
    upstream_bundle_hash: str
    scorer_source_hash: str
    canonical_profile_hash: str
    serialization_version: str
    evidence_hash: str
    collected_at: str
    radiant_team_score: float | None
    dire_team_score: float | None
    relative_advantage: float | None
    hero_components: list[RoshAnalysisHeroComponent]
    minute_points: list[RoshAnalysisMinutePoint]
    error_code: str | None


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
