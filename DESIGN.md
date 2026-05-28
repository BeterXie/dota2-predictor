# Dota 2 Match Predictor — Design Document

## Project Goal

Predict the outcome of upcoming Dota 2 matches based on historical match detail data from the OpenDota API. The system ingests completed matches into a database, extracts features, trains a model, outputs predictions, and serves data for web display.

## Tech Stack

- **Language**: Python 3.11+
- **Data Fetching**: `httpx` (async HTTP) + OpenDota API
- **Database**: SQLite via `sqlite3` (standard library, zero setup; migrate to PostgreSQL later if needed)
- **Data Processing**: `pandas`, `numpy`
- **Modeling**: `scikit-learn` (baseline), `xgboost` (production)
- **Web Backend**: FastAPI (serves match data + predictions to frontend pages)
- **Config**: YAML config file per module

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    OpenDota API                          │
└────────┬──────────────┬──────────────┬──────────────────┘
         │              │              │
    ┌────▼────┐   ┌─────▼─────┐  ┌────▼─────┐
    │ Module A│   │ Module B  │  │ Module C │
    │ Fetch   │   │ Feature   │  │ Model    │
    │ Matches │──▶│ Engine    │─▶│ Training │
    └────┬─────┘   └─────┬─────┘  └────┬─────┘
         │               │              │
    ┌────▼───────────────▼──────────────▼─────┐
    │              SQLite Database             │
    │  (raw match data + feature views)        │
    └────┬─────────────────────────────────────┘
         │
    ┌────▼─────┐   ┌──────────┐
    │ Module E │   │ Frontend │
    │ FastAPI  │◀──│ Web Page │
    │ Server   │   │ (future) │
    └──────────┘   └──────────┘
```

## Shared Data Contract

All modules communicate through the **SQLite database** and **Parquet files**. No module imports another.

```
data/
├── dota2.db                   # Main database (raw + structured data)
├── features/                  # Module B writes, C+D read (ML-optimized)
│   ├── match_features.parquet
│   ├── team_features.parquet
│   ├── hero_features.parquet
│   └── draft_features.parquet
├── models/                    # Module C writes, D reads
│   ├── model_v{timestamp}.pkl
│   └── latest.pkl
└── predictions/               # Module D writes, E reads
    └── {date}_{match_id}.json
```

---

## Database Schema (`data/dota2.db`)

SQLite database with these tables. Module A writes raw data, Module B reads raw + writes feature views, Module E reads everything for web display.

### Raw Data Tables (Module A populates)

```sql
-- Match basic info
CREATE TABLE matches (
    match_id        INTEGER PRIMARY KEY,
    radiant_team_id INTEGER,
    dire_team_id    INTEGER,
    radiant_win     BOOLEAN,
    duration        INTEGER,        -- seconds
    game_mode       INTEGER,
    lobby_type      INTEGER,
    start_time      INTEGER,        -- unix timestamp
    first_blood_time INTEGER,
    leagueid        INTEGER,
    series_id       INTEGER,
    series_type     INTEGER,        -- 0=BO1, 1=BO3, 2=BO5
    patch           INTEGER,
    region          INTEGER,
    radiant_score   INTEGER,
    dire_score      INTEGER,
    stomp           INTEGER,
    comeback        INTEGER,
    tower_status_radiant INTEGER,
    tower_status_dire    INTEGER,
    barracks_status_radiant INTEGER,
    barracks_status_dire    INTEGER,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_matches_radiant_team ON matches(radiant_team_id);
CREATE INDEX idx_matches_dire_team ON matches(dire_team_id);
CREATE INDEX idx_matches_league ON matches(leagueid);
CREATE INDEX idx_matches_start_time ON matches(start_time);
CREATE INDEX idx_matches_series ON matches(series_id);

-- Teams cache
CREATE TABLE teams (
    team_id     INTEGER PRIMARY KEY,
    name        TEXT,
    tag         TEXT,
    logo_url    TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Leagues cache
CREATE TABLE leagues (
    leagueid    INTEGER PRIMARY KEY,
    name        TEXT,
    tier        TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Heroes cache (static, populated once from /api/heroes)
CREATE TABLE heroes (
    hero_id         INTEGER PRIMARY KEY,
    localized_name  TEXT,
    primary_attr    TEXT,
    attack_type     TEXT,
    roles           TEXT    -- JSON array of role strings
);

-- Players per match
CREATE TABLE match_players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER REFERENCES matches(match_id),
    account_id      INTEGER,
    player_slot     INTEGER,        -- 0-4 radiant, 128-132 dire
    hero_id         INTEGER REFERENCES heroes(hero_id),
    is_radiant      BOOLEAN,
    team_id         INTEGER,
    kills           INTEGER,
    deaths          INTEGER,
    assists         INTEGER,
    gold_per_min    INTEGER,
    xp_per_min      INTEGER,
    net_worth       INTEGER,
    last_hits       INTEGER,
    denies          INTEGER,
    hero_damage     INTEGER,
    hero_healing    INTEGER,
    tower_damage    INTEGER,
    level           INTEGER,
    -- Items (inventory at game end)
    item_0          INTEGER,
    item_1          INTEGER,
    item_2          INTEGER,
    item_3          INTEGER,
    item_4          INTEGER,
    item_5          INTEGER,
    backpack_0      INTEGER,
    backpack_1      INTEGER,
    backpack_2      INTEGER,
    item_neutral    INTEGER
);

CREATE INDEX idx_match_players_match ON match_players(match_id);
CREATE INDEX idx_match_players_hero ON match_players(hero_id);
CREATE INDEX idx_match_players_team ON match_players(team_id);

-- Picks and bans
CREATE TABLE picks_bans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER REFERENCES matches(match_id),
    hero_id     INTEGER REFERENCES heroes(hero_id),
    is_pick     BOOLEAN,
    team        INTEGER,       -- 0=radiant, 1=dire
    ord         INTEGER        -- draft order 0-23
);

CREATE INDEX idx_picks_bans_match ON picks_bans(match_id);

-- Gold advantage time series (sampled every ~60s)
CREATE TABLE gold_advantage (
    match_id    INTEGER REFERENCES matches(match_id),
    time_min    INTEGER,          -- minute index (0, 1, 2, ...)
    value       INTEGER           -- positive = radiant ahead
);

CREATE INDEX idx_gold_adv_match ON gold_advantage(match_id);

-- XP advantage time series
CREATE TABLE xp_advantage (
    match_id    INTEGER REFERENCES matches(match_id),
    time_min    INTEGER,
    value       INTEGER
);

CREATE INDEX idx_xp_adv_match ON xp_advantage(match_id);

-- Teamfights
CREATE TABLE teamfights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER REFERENCES matches(match_id),
    start_time  INTEGER,          -- seconds
    end_time    INTEGER,
    last_death  INTEGER,
    deaths      INTEGER
);

CREATE INDEX idx_teamfights_match ON teamfights(match_id);

-- Individual contributions within a teamfight
CREATE TABLE teamfight_players (
    teamfight_id INTEGER REFERENCES teamfights(id),
    player_slot  INTEGER,
    deaths       INTEGER,
    buybacks     INTEGER,
    damage       INTEGER,
    healing      INTEGER,
    gold_delta   INTEGER,
    xp_delta     INTEGER,
    kills        INTEGER     -- number of kills this player got in the fight
);

CREATE INDEX idx_tf_players_tf ON teamfight_players(teamfight_id);

-- Objectives (building kills, roshan, etc.)
CREATE TABLE objectives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER REFERENCES matches(match_id),
    time        INTEGER,           -- seconds
    type        TEXT,              -- building_kill, etc.
    unit        TEXT,              -- killer unit
    key         TEXT,              -- building key (e.g. npc_dota_goodguys_tower1_bot)
    player_slot INTEGER
);

CREATE INDEX idx_objectives_match ON objectives(match_id);

-- Chat / all-chat messages (optional, for flavor)
CREATE TABLE chat (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER REFERENCES matches(match_id),
    time        INTEGER,
    player_slot INTEGER,
    type        TEXT,              -- chat, chatwheel, etc.
    message     TEXT
);
```

### Feature Views (Module B creates)

These are SQL views on top of raw tables, plus materialized tables for computed aggregations:

```sql
-- Materialized: pre-computed match-level features (same as match_features.parquet)
CREATE TABLE match_feature_cache (
    match_id INTEGER PRIMARY KEY REFERENCES matches(match_id),
    -- denormalized columns per the feature schema below
    ...
);
```

---

## Module A: Data Fetcher (`fetch/`)

**Goal**: Given league IDs, team IDs, or match IDs, download match detail from OpenDota API and insert into SQLite database tables.

**Independent from**: B, C, D, E

**Inputs** (from `fetch/config.yaml`):
```yaml
database: "../data/dota2.db"

leagues: [19101, 19000]
teams: [9247354, 10150538]
specific_matches: []
date_range:
  start: "2025-01-01"
  end: "2025-12-31"
```

**Key classes/functions**:
```
fetch/
├── __init__.py
├── config.yaml
├── main.py              # python -m fetch.main [--force] [--match-id X]
├── client.py            # OpenDotaClient — rate-limited async HTTP
│   ├── get_match(match_id) -> dict
│   ├── get_league_matches(league_id) -> list[dict]
│   ├── get_team_matches(team_id) -> list[dict]
│   ├── get_heroes() -> list[dict]
│   └── get_hero_stats() -> list[dict]
├── discover.py          # Match discoverer — finds match IDs to fetch
│   └── discover(league_ids, team_ids) -> set[int]
├── db.py                # Database writer (singleton connection)
│   ├── init_db()                    # CREATE TABLE IF NOT EXISTS
│   ├── insert_match(match_json)     # Parse & insert into all tables
│   ├── insert_heroes(heroes_json)   # Populate heroes table
│   ├── is_fetched(match_id) -> bool
│   └── mark_fetched(match_id)
└── parser.py            # JSON parser — API response -> flat dicts for DB insert
    ├── parse_match_basic(json) -> dict
    ├── parse_players(json) -> list[dict]
    ├── parse_picks_bans(json) -> list[dict]
    ├── parse_teamfights(json) -> list[dict]
    ├── parse_objectives(json) -> list[dict]
    ├── parse_gold_adv(json) -> list[dict]
    └── parse_xp_adv(json) -> list[dict]
```

**Constraints**:
- Rate limit: max 50 requests/minute (OpenDota free tier)
- Retry with exponential backoff on 429/5xx
- Check `matches.fetched_at` before fetching; `--force` to re-fetch
- Use `INSERT OR REPLACE` for idempotent re-fetch
- Run `init_db()` on first run to create all tables

---

## Module B: Feature Engine (`features/`)

**Goal**: Read raw data from SQLite, compute ML features, write to both Parquet files and DB materialized views.

**Independent from**: A (reads its DB output), C, D, E

**Inputs**:
- `data/dota2.db` — all raw data tables
- `fetch/config.yaml` for DB path

**Outputs**:
```
data/features/
├── match_features.parquet       # One row per match
├── team_features.parquet         # One row per team per match
├── hero_features.parquet         # One row per hero pick per match
└── draft_features.parquet        # One row per pick/ban action
```

And corresponding materialized tables in `dota2.db` for web queries.

### Feature Tables Schema

#### `match_features` (1 row per match)
| Column | Type | Description |
|--------|------|-------------|
| match_id | int64 | PK |
| duration | int32 | Game length (seconds) |
| radiant_win | bool | **Target variable** |
| first_blood_radiant | bool | Radiant got first blood |
| first_blood_time | int32 | Seconds |
| radiant_gold_adv_10min | int32 | Gold lead at ~10 min |
| radiant_xp_adv_10min | int32 | XP lead at ~10 min |
| radiant_gold_adv_max | int32 | Max radiant gold lead |
| radiant_gold_adv_min | int32 | Min (max deficit) |
| radiant_gold_adv_mean | float64 | Mean gold lead |
| gold_adv_swings | int32 | Times lead crosses zero |
| radiant_tower_kills | int32 | |
| dire_tower_kills | int32 | |
| radiant_barracks_kills | int32 | |
| dire_barracks_kills | int32 | |
| radiant_first_tower_time | int32 | Seconds |
| dire_first_tower_time | int32 | Seconds |
| teamfight_count | int32 | Total teamfights |
| radiant_teamfight_wins | int32 | Fights radiant won |
| radiant_tf_kd_ratio | float64 | Kill/death ratio in teamfights |
| stomp_value | int32 | Final gold difference |
| comeback_value | int32 | Max deficit overcome |
| radiant_score | int32 | |
| dire_score | int32 | |
| patch | int32 | |
| radiant_team_id | int32 | |
| dire_team_id | int32 | |
| league_id | int32 | |
| series_id | int32 | |

#### `team_features` (2 rows per match: radiant + dire)
| Column | Type | Description |
|--------|------|-------------|
| match_id | int64 | FK |
| is_radiant | bool | |
| team_id | int32 | |
| total_kills | int32 | Sum of 5 players |
| total_deaths | int32 | |
| total_assists | int32 | |
| avg_gpm | float64 | Mean GPM |
| avg_xpm | float64 | Mean XPM |
| total_net_worth | int32 | |
| total_last_hits | int32 | |
| total_denies | int32 | |
| gpm_std | float64 | GPM std dev (farm distribution) |
| max_net_worth | int32 | Carry net worth |
| total_hero_damage | int32 | |
| first_blood | bool | |

#### `hero_features` (10 rows per match)
| Column | Type | Description |
|--------|------|-------------|
| match_id | int64 | FK |
| hero_id | int32 | |
| player_slot | int32 | |
| is_radiant | bool | |
| team_id | int32 | |
| kills | int32 | |
| deaths | int32 | |
| assists | int32 | |
| gpm | int32 | |
| xpm | int32 | |
| net_worth | int32 | |
| last_hits | int32 | |
| denies | int32 | |
| hero_damage | int32 | |
| hero_healing | int32 | |
| tower_damage | int32 | |
| level | int32 | |
| role | int32 | 1-5 (inferred from farm priority) |

#### `draft_features` (24 rows per match)
| Column | Type | Description |
|--------|------|-------------|
| match_id | int64 | FK |
| order | int32 | Draft order 0-23 |
| is_pick | bool | Pick or Ban |
| hero_id | int32 | |
| team | int32 | 0=radiant, 1=dire |
| phase | str | ban1/pick1/ban2/pick2/ban3/pick3 |

### Aggregated Features (cross-match rolling stats)

1. **Team rolling N** (last 10/20/50 matches):
   - `team_win_rate_N`, `team_avg_gpm_N`, `team_avg_xpm_N`, `team_avg_net_worth_lead_10min_N`

2. **Hero rolling stats** (per patch):
   - `hero_win_rate_patch`, `hero_avg_gpm_patch`, `hero_pick_rate_patch`, `hero_ban_rate_patch`

3. **Head-to-head**:
   - `h2h_win_rate` — team A vs team B historical win rate
   - `h2h_match_count` — number of prior encounters

**Key classes/functions**:
```
features/
├── __init__.py
├── config.yaml
├── main.py              # python -m features.main [--force]
├── db_reader.py         # Read raw data from SQLite -> pandas DataFrames
│   ├── read_matches(league_ids=None, date_range=None) -> DataFrame
│   ├── read_players(match_ids) -> DataFrame
│   └── read_draft(match_ids) -> DataFrame
├── parser.py            # Single-match feature extraction
│   ├── extract_match_features(match_row, gold_adv, xp_adv, objectives) -> dict
│   ├── extract_team_features(match_row, players_df) -> list[dict]
│   ├── extract_hero_features(players_df) -> list[dict]
│   └── extract_draft_features(picks_bans_df) -> list[dict]
├── aggregator.py        # Rolling stats across matches
│   ├── compute_team_rolling(db, team_id, window_sizes=[10,20,50]) -> dict
│   ├── compute_hero_rolling(db, hero_id, patch) -> dict
│   └── compute_h2h(db, team_a, team_b) -> dict
├── store.py             # Write features to Parquet + DB materialized tables
│   ├── to_parquet(df, name)
│   └── to_db_materialized(df, table_name)
└── hero_roles.py        # Static hero -> role mapping
```

---

## Module C: Model Training (`train/`)

**Goal**: Read feature parquet files, train XGBoost classifier, evaluate, save model.

**Independent from**: A, B (reads their output), D, E

**Inputs**:
- `data/features/match_features.parquet`
- `data/features/team_features.parquet`
- `data/features/hero_features.parquet`
- `data/features/draft_features.parquet`

**Outputs**:
- `data/models/model_v{YYYYMMDD_HHMMSS}.pkl`
- `data/models/latest.pkl`

**Key classes/functions**:
```
train/
├── __init__.py
├── config.yaml
├── main.py              # python -m train.main [--tune]
├── dataset.py           # Join feature tables into X, y
│   ├── build_training_data() -> (X, y, feature_names)
│   └── split_train_test(X, y, method="time") -> splits
├── model.py             # XGBoost wrapper
│   ├── train(X, y, params) -> model
│   ├── cross_validate(model, X, y, n_folds=5) -> metrics
│   └── save_model(model, feature_names, metrics, path)
├── evaluate.py          # Metrics + SHAP
│   ├── evaluate(model, X_test, y_test) -> dict
│   ├── plot_confusion_matrix(...)
│   ├── plot_feature_importance(...)
│   └── plot_shap_summary(...)
└── tune.py              # Optuna hyperparameter tuning
```

**Metrics**: Accuracy, Precision, Recall, F1, ROC-AUC, Log Loss, Brier Score

---

## Module D: Prediction (`predict/`)

**Goal**: Given two teams, build feature vector from DB + latest model, output win probability.

**Independent from**: A, B (reads their outputs), C (reads model), E

**Outputs**:
- `data/predictions/{date}_{match_id}.json`
- Console output

**Key classes/functions**:
```
predict/
├── __init__.py
├── config.yaml
├── main.py              # python -m predict.main --radiant ID --dire ID --league ID
├── predictor.py         # Load model + predict
│   ├── load_model() -> model
│   └── predict(radiant_id, dire_id, league_id) -> Prediction
├── feature_builder.py   # Build feature vector from DB
│   ├── build_team_features(team_id, db) -> dict
│   ├── build_head_to_head(team_a, team_b, db) -> dict
│   └── build_lineup_features(hero_ids_rad, hero_ids_dire) -> dict
└── output.py            # Format prediction as JSON
```

**Prediction output format**:
```json
{
  "prediction_id": "20260527_1",
  "created_at": "2026-05-27T12:00:00Z",
  "match": {
    "radiant_team": {"id": 9247354, "name": "Team Falcons"},
    "dire_team": {"id": 10150538, "name": "LGD Gaming"},
    "league": {"id": 19101, "name": "DreamLeague S26"},
    "best_of": 3
  },
  "prediction": {
    "radiant_win_prob": 0.62,
    "confidence": "medium",
    "top_factors": [
      {"factor": "team_recent_win_rate", "impact": 0.15, "direction": "radiant"},
      {"factor": "avg_gpm_diff_10", "impact": 0.10, "direction": "radiant"},
      {"factor": "draft_advantage", "impact": 0.08, "direction": "dire"}
    ]
  },
  "model": {"version": "20260527_080000", "auc": 0.73, "accuracy": 0.68}
}
```

---

## Module E: Web API (`web/`)

**Goal**: Serve match data and predictions to a frontend page via REST API.

**Independent from**: A, B, C, D (reads DB + predictions JSON)

**Endpoints**:
```
GET  /api/matches                          # List matches (paginated, filterable)
GET  /api/matches/{match_id}               # Match detail (players, draft, gold graph data)
GET  /api/teams                            # List teams
GET  /api/teams/{team_id}                  # Team profile + recent matches + stats
GET  /api/teams/{team_id}/matches          # Team match history
GET  /api/leagues                          # List leagues
GET  /api/leagues/{league_id}/matches      # League matches
GET  /api/heroes                           # Hero list with stats
GET  /api/heroes/{hero_id}                 # Hero detail + win rate by patch
GET  /api/predictions                      # List recent predictions
GET  /api/predictions/{id}                 # Single prediction detail
POST /api/predict                          # Request a new prediction (team_a, team_b)
GET  /api/stats/head-to-head?team_a=X&team_b=Y  # H2H comparison
```

**Key classes/functions**:
```
web/
├── __init__.py
├── config.yaml
├── main.py              # python -m web.main -> uvicorn
├── app.py               # FastAPI app
├── routers/
│   ├── matches.py       # /api/matches endpoints
│   ├── teams.py         # /api/teams endpoints
│   ├── heroes.py        # /api/heroes endpoints
│   ├── leagues.py       # /api/leagues endpoints
│   └── predictions.py   # /api/predictions endpoints
├── queries.py           # SQL query builders (thin layer over sqlite3)
│   ├── get_match_detail(match_id) -> dict
│   ├── get_team_profile(team_id) -> dict
│   ├── get_h2h(team_a, team_b) -> dict
│   ├── search_matches(filters) -> list[dict]
│   └── get_hero_stats(hero_id) -> dict
└── schemas.py           # Pydantic response models
```

**Frontend setup** (future, not in initial scope):
```
web/
├── static/
│   ├── index.html       # SPA entry point
│   ├── app.js            # Vanilla JS or htmx (keep it simple)
│   └── style.css
```

---

## Development Order & Parallel Sessions

```
Phase 1 (all parallel — no dependencies)
├── Session 1: Module A — Fetcher
│   Task: "Implement fetch/ module. Write client.py (async httpx), 
│          parser.py (JSON->DB dicts), db.py (CREATE TABLE + INSERT).
│          Populate heroes table from /api/heroes. Follow DESIGN.md."
│
├── Session 2: Module B — Feature Engine (parser + store)
│   Task: "Implement features/parser.py and store.py. Read from SQLite
│          dota2.db, extract 4 feature DataFrames per DESIGN.md schemas,
│          write to data/features/*.parquet. Include hero role mapping."
│
└── Session 3: Module E — Web API (scaffolding)
│   Task: "Implement web/ module. Set up FastAPI app with routers for 
│          matches, teams, heroes, leagues. Write SQL queries in
│          queries.py. Focus on GET endpoints. Follow DESIGN.md."

Phase 2 (depends on Phase 1)
├── Session 4: Module B — Aggregator (rolling stats)
│   Task: "Implement features/aggregator.py. Compute team rolling stats
│          (10/20/50 match windows), hero patch stats, H2H stats."
│   Depends on: DB has enough matches from Session 1
│
├── Session 5: Module C — Model Training
│   Task: "Implement train/ module. Read parquet features, build training
│          matrix, train XGBoost, cross-validate, save model."
│   Depends on: Parquet files from Session 2
│
└── Session 6: Module D — Prediction
│   Task: "Implement predict/ module. Load model, build feature vectors
│          from DB, output win probability."
│   Depends on: Model from Session 5, DB schema from Session 1

Phase 3 (integration)
└── Session 7: End-to-end test + frontend
│   Task: "Fetch real data -> build features -> train -> predict -> 
│          verify via web API. Build simple HTML dashboard."
```

### Session Startup Prompts

Copy-paste these to each Claude Code session:

**Session 1 — fetch/**:
> Implement the OpenDota data fetcher module at `dota2-predictor/fetch/`. 
> Read the full DESIGN.md first. Build: `client.py` (async httpx, 50 req/min rate limit, retry on 429), 
> `parser.py` (parse API JSON into flat dicts for DB insert), `db.py` (CREATE TABLE statements matching 
> the DESIGN.md schema, INSERT OR REPLACE logic, `is_fetched()` check). Also fetch `/api/heroes` 
> and populate the heroes table. Write `main.py` that reads `config.yaml`, discovers matches from 
> league/team IDs, fetches them, and stores everything in `data/dota2.db`. 
> Do NOT reference other modules. Test with match ID 8826468180.

**Session 2 — features/parser.py + store.py**:
> Implement `parser.py` and `store.py` in `dota2-predictor/features/`. Read raw match data from 
> `data/dota2.db` SQLite tables. Extract features into 4 pandas DataFrames matching the column schemas 
> in DESIGN.md (match_features, team_features, hero_features, draft_features). 
> Write output to `data/features/*.parquet`. Include hero role inference (pos 1-5 based on farm priority).
> Do NOT implement rolling aggregations yet. Read DESIGN.md for exact column definitions.

**Session 3 — web/ API**:
> Implement the web API module at `dota2-predictor/web/`. Set up a FastAPI application with routers 
> for matches, teams, heroes, and leagues. Write `queries.py` with SQLite queries that read from 
> `data/dota2.db`. Implement GET endpoints: match list (paginated, filterable by team/league/date), 
> match detail (with players, draft, gold graph data), team profile with stats, H2H comparison. 
> Use Pydantic schemas for response models. Follow DESIGN.md Module E specs. 
> Serve a simple index.html at `/` that lists recent matches.

**Session 4 — features/aggregator.py**:
> Implement `features/aggregator.py` in `dota2-predictor/features/`. Compute cross-match aggregated 
> features from `data/dota2.db`: team rolling stats (win rate, avg GPM/XPM over last 10/20/50 matches), 
> hero patch stats (win rate, pick rate, ban rate), and head-to-head records between any two teams. 
> Write results to the feature parquet files. See DESIGN.md "Aggregated Features" section.

**Session 5 — train/**:
> Implement `train/` module at `dota2-predictor/train/`. Read `data/features/*.parquet`, join into 
> a training matrix. Handle missing values (median imputation). Train XGBoost classifier with 
> time-based train/test split (split by match start_time, not random). Cross-validate (5-fold). 
> Report Accuracy, F1, ROC-AUC, Log Loss. Save model + feature_names list + metrics dict to 
> `data/models/model_v{timestamp}.pkl` and copy to `latest.pkl`. 
> See DESIGN.md Module C for config params and full specs.

**Session 6 — predict/**:
> Implement `predict/` module at `dota2-predictor/predict/`. Load model from `data/models/latest.pkl`. 
> Accept `--radiant TEAM_ID --dire TEAM_ID` via CLI. Fetch team recent stats and H2H from 
> `data/dota2.db`, build feature vector (ensure feature order matches training), run prediction, 
> output win probability + confidence + top contributing factors in JSON format to 
> `data/predictions/`. See DESIGN.md Module D for output format.

---

## API Reference: Key OpenDota Endpoints

| Endpoint | Use |
|----------|-----|
| `GET /api/matches/{match_id}` | Full match detail |
| `GET /api/leagues/{league_id}/matches` | All matches in a league |
| `GET /api/teams/{team_id}/matches` | Recent matches for a team |
| `GET /api/teams/{team_id}/heroes` | Team hero stats |
| `GET /api/heroes` | Hero ID to name mapping |
| `GET /api/heroStats` | Hero stats per patch |
| `GET /api/players/{account_id}/matches` | Player match history |
| `GET /api/live` | Currently live pro matches |

---

## Configuration Convention

Every module has its own `config.yaml` but shares the DB path via `.env`:

```bash
# .env at project root
DATA_DIR=./data
DATABASE_PATH=./data/dota2.db
OPENDOTA_BASE_URL=https://api.opendota.com
OPENDOTA_RATE_LIMIT=50
```

Common pattern for DB access:
```python
import os, sqlite3
DB_PATH = os.environ.get("DATABASE_PATH", "./data/dota2.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
```

---

## Non-Goals (for now)

- Real-time / live prediction during matches
- Player-level tracking across team roster changes
- In-game event prediction (first blood, next tower, etc.)
- Authentication / user accounts on the web frontend

---

## Estimated Effort

| Module | Effort | Key Risk |
|--------|--------|----------|
| A: Fetcher | 3-4h | Rate limiting, partial failure recovery |
| B: Feature Engine | 5-6h | Feature schema correctness, edge cases |
| C: Model Training | 3-4h | Data leakage via time-based splits |
| D: Prediction | 2-3h | Feature name alignment with training |
| E: Web API | 3-4h | Query performance on large datasets |
| Integration | 2-3h | End-to-end validation |

**Total sequential**: ~18-24h. **With 3-4 parallel sessions**: ~6-8h.

---

## Notes for Claude Code Sessions

1. Each session `cd dota2-predictor` and works within its module directory
2. Use `python -m {module}.main` pattern for all entry points
3. Update `DESIGN.md` if you change a table schema or data contract
4. `data/` is gitignored; commit code only
5. Tag commits: `[fetch] Add rate limiting`, `[features] Parse teamfights`, `[web] Add match detail endpoint`
6. Run `python -m fetch.main` with a small scope first (1 league, 10 matches) to validate before scaling
