"""Database writer — CREATE TABLE, INSERT, and fetch-status helpers."""

import json
import logging
import sqlite3
from pathlib import Path

from .parser import (
    parse_chat,
    parse_gold_adv,
    parse_league_info,
    parse_match_basic,
    parse_objectives,
    parse_picks_bans,
    parse_players,
    parse_team_info,
    parse_teamfights,
    parse_xp_adv,
)

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS matches (
    match_id        INTEGER PRIMARY KEY,
    radiant_team_id INTEGER,
    dire_team_id    INTEGER,
    radiant_win     BOOLEAN,
    duration        INTEGER,
    game_mode       INTEGER,
    lobby_type      INTEGER,
    start_time      INTEGER,
    first_blood_time INTEGER,
    leagueid        INTEGER,
    series_id       INTEGER,
    series_type     INTEGER,
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

CREATE INDEX IF NOT EXISTS idx_matches_radiant_team ON matches(radiant_team_id);
CREATE INDEX IF NOT EXISTS idx_matches_dire_team ON matches(dire_team_id);
CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(leagueid);
CREATE INDEX IF NOT EXISTS idx_matches_start_time ON matches(start_time);
CREATE INDEX IF NOT EXISTS idx_matches_series ON matches(series_id);

CREATE TABLE IF NOT EXISTS teams (
    team_id     INTEGER PRIMARY KEY,
    name        TEXT,
    tag         TEXT,
    logo_url    TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS leagues (
    leagueid    INTEGER PRIMARY KEY,
    name        TEXT,
    tier        TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS heroes (
    hero_id         INTEGER PRIMARY KEY,
    localized_name  TEXT,
    primary_attr    TEXT,
    attack_type     TEXT,
    roles           TEXT,
    hero_key        TEXT,
    pro_pick        INTEGER DEFAULT 0,
    pro_win         INTEGER DEFAULT 0,
    pro_ban         INTEGER DEFAULT 0,
    pub_pick        INTEGER DEFAULT 0,
    pub_win         INTEGER DEFAULT 0,
    turbo_picks     INTEGER DEFAULT 0,
    turbo_wins      INTEGER DEFAULT 0,
    win_rate        REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS match_players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER REFERENCES matches(match_id),
    account_id      INTEGER,
    player_slot     INTEGER,
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
    item_0          INTEGER,
    item_1          INTEGER,
    item_2          INTEGER,
    item_3          INTEGER,
    item_4          INTEGER,
    item_5          INTEGER,
    backpack_0      INTEGER,
    backpack_1      INTEGER,
    backpack_2      INTEGER,
    item_neutral    INTEGER,
    firstblood_claimed INTEGER,
    gold_10min      INTEGER,
    lh_10min        INTEGER,
    xp_10min        INTEGER,
    kills_10min     INTEGER,
    deaths_10min    INTEGER,
    assists_10min   INTEGER,
    obs_placed_10min INTEGER,
    sen_placed_10min INTEGER,
    lane_efficiency REAL,
    lane_role       INTEGER,
    is_roaming      BOOLEAN,
    kda             REAL,
    observer_kills_10min INTEGER,
    sentry_kills_10min   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_match_players_match ON match_players(match_id);
CREATE INDEX IF NOT EXISTS idx_match_players_hero ON match_players(hero_id);
CREATE INDEX IF NOT EXISTS idx_match_players_team ON match_players(team_id);
CREATE INDEX IF NOT EXISTS idx_match_players_account ON match_players(account_id);

CREATE TABLE IF NOT EXISTS picks_bans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER REFERENCES matches(match_id),
    hero_id     INTEGER REFERENCES heroes(hero_id),
    is_pick     BOOLEAN,
    team        INTEGER,
    ord         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_picks_bans_match ON picks_bans(match_id);

CREATE TABLE IF NOT EXISTS gold_advantage (
    match_id    INTEGER REFERENCES matches(match_id),
    time_min    INTEGER,
    value       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gold_adv_match ON gold_advantage(match_id);

CREATE TABLE IF NOT EXISTS xp_advantage (
    match_id    INTEGER REFERENCES matches(match_id),
    time_min    INTEGER,
    value       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_xp_adv_match ON xp_advantage(match_id);

CREATE TABLE IF NOT EXISTS teamfights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER REFERENCES matches(match_id),
    start_time  INTEGER,
    end_time    INTEGER,
    last_death  INTEGER,
    deaths      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_teamfights_match ON teamfights(match_id);

CREATE TABLE IF NOT EXISTS teamfight_players (
    teamfight_id INTEGER REFERENCES teamfights(id),
    player_slot  INTEGER,
    deaths       INTEGER,
    buybacks     INTEGER,
    damage       INTEGER,
    healing      INTEGER,
    gold_delta   INTEGER,
    xp_delta     INTEGER,
    kills        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tf_players_tf ON teamfight_players(teamfight_id);

CREATE TABLE IF NOT EXISTS objectives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER REFERENCES matches(match_id),
    time        INTEGER,
    type        TEXT,
    unit        TEXT,
    key         TEXT,
    player_slot INTEGER
);

CREATE INDEX IF NOT EXISTS idx_objectives_match ON objectives(match_id);

CREATE TABLE IF NOT EXISTS chat (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER REFERENCES matches(match_id),
    time        INTEGER,
    player_slot INTEGER,
    type        TEXT,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS hero_matchups (
    hero_id     INTEGER REFERENCES heroes(hero_id),
    vs_hero_id  INTEGER REFERENCES heroes(hero_id),
    games_played INTEGER,
    wins        INTEGER,
    synergy     REAL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (hero_id, vs_hero_id)
);

CREATE INDEX IF NOT EXISTS idx_hero_matchups_hero ON hero_matchups(hero_id);

CREATE TABLE IF NOT EXISTS hero_duration_stats (
    hero_id     INTEGER REFERENCES heroes(hero_id),
    duration_min INTEGER,
    games_played INTEGER,
    wins        INTEGER,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (hero_id, duration_min)
);

CREATE INDEX IF NOT EXISTS idx_hero_duration_hero ON hero_duration_stats(hero_id);

CREATE TABLE IF NOT EXISTS hero_benchmarks (
    hero_id     INTEGER REFERENCES heroes(hero_id),
    metric      TEXT,
    p50         REAL,
    p75         REAL,
    p90         REAL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (hero_id, metric)
);

CREATE INDEX IF NOT EXISTS idx_hero_benchmarks_hero ON hero_benchmarks(hero_id);

"""


class Database:
    """Manages the SQLite database connection and all write operations."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ):
        if db_path is None and connection is None:
            raise ValueError("db_path or connection is required")
        self.db_path = db_path
        self._conn = connection
        self._owns_connection = connection is None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.db_path is None:
                raise RuntimeError("injected database connection is unavailable")
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None and self._owns_connection:
            self._conn.close()
            self._conn = None

    def init_db(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA_SQL)
        self._migrate(conn)
        conn.commit()
        logger.info("Database schema initialized.")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add missing columns to existing tables."""
        migrations = [
            "ALTER TABLE heroes ADD COLUMN pro_pick INTEGER DEFAULT 0",
            "ALTER TABLE heroes ADD COLUMN pro_win INTEGER DEFAULT 0",
            "ALTER TABLE heroes ADD COLUMN pro_ban INTEGER DEFAULT 0",
            "ALTER TABLE heroes ADD COLUMN pub_pick INTEGER DEFAULT 0",
            "ALTER TABLE heroes ADD COLUMN pub_win INTEGER DEFAULT 0",
            "ALTER TABLE heroes ADD COLUMN turbo_picks INTEGER DEFAULT 0",
            "ALTER TABLE heroes ADD COLUMN turbo_wins INTEGER DEFAULT 0",
            "ALTER TABLE heroes ADD COLUMN win_rate REAL DEFAULT 0.0",
            # Early-game columns for match_players
            "ALTER TABLE match_players ADD COLUMN gold_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN lh_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN xp_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN kills_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN deaths_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN assists_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN obs_placed_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN sen_placed_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN lane_efficiency REAL",
            "ALTER TABLE match_players ADD COLUMN lane_role INTEGER",
            "ALTER TABLE match_players ADD COLUMN is_roaming BOOLEAN",
            "ALTER TABLE match_players ADD COLUMN kda REAL",
            "ALTER TABLE match_players ADD COLUMN observer_kills_10min INTEGER",
            "ALTER TABLE match_players ADD COLUMN sentry_kills_10min INTEGER",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists

    def is_fetched(self, match_id: int) -> bool:
        conn = self.connect()
        row = conn.execute(
            """SELECT 1 FROM matches m
               WHERE m.match_id = ?
                 AND EXISTS (SELECT 1 FROM match_players mp WHERE mp.match_id = m.match_id)""",
            (match_id,),
        ).fetchone()
        return row is not None

    def insert_heroes(self, heroes: list[dict]) -> None:
        conn = self.connect()
        for h in heroes:
            internal_name = h.get("name", "")
            hero_key = internal_name.replace("npc_dota_hero_", "") if internal_name else ""
            conn.execute(
                """INSERT OR REPLACE INTO heroes (hero_id, localized_name, primary_attr, attack_type, roles, hero_key)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    h["id"],
                    h.get("localized_name"),
                    h.get("primary_attr"),
                    h.get("attack_type"),
                    json.dumps(h.get("roles")),
                    hero_key,
                ),
            )
        conn.commit()
        logger.info("Inserted %d heroes.", len(heroes))

    def insert_team(self, team: dict, commit: bool = True) -> None:
        conn = self.connect()
        conn.execute(
            """INSERT OR REPLACE INTO teams (team_id, name, tag, logo_url)
               VALUES (:team_id, :name, :tag, :logo_url)""",
            team,
        )
        if commit:
            conn.commit()
        logger.debug("Inserted team %d (%s).", team["team_id"], team.get("name"))

    def insert_league(self, league: dict, commit: bool = True) -> None:
        conn = self.connect()
        conn.execute(
            """INSERT OR REPLACE INTO leagues (leagueid, name, tier)
               VALUES (:leagueid, :name, :tier)""",
            league,
        )
        if commit:
            conn.commit()
        logger.debug("Inserted league %d (%s).", league["leagueid"], league.get("name"))

    def insert_match(self, match: dict, commit: bool = True) -> None:
        """Parse a full match JSON and insert into all related tables."""
        conn = self.connect()
        match_id = match["match_id"]

        self._delete_match_children(conn, match_id)

        # Insert team and league metadata
        for side in ("radiant", "dire"):
            team = parse_team_info(match, side)
            if team:
                self.insert_team(team, commit=False)
        league = parse_league_info(match)
        if league:
            self.insert_league(league, commit=False)

        conn.execute(
            """INSERT OR REPLACE INTO matches (
                match_id, radiant_team_id, dire_team_id, radiant_win, duration,
                game_mode, lobby_type, start_time, first_blood_time, leagueid,
                series_id, series_type, patch, region, radiant_score, dire_score,
                stomp, comeback, tower_status_radiant, tower_status_dire,
                barracks_status_radiant, barracks_status_dire
            ) VALUES (
                :match_id, :radiant_team_id, :dire_team_id, :radiant_win, :duration,
                :game_mode, :lobby_type, :start_time, :first_blood_time, :leagueid,
                :series_id, :series_type, :patch, :region, :radiant_score, :dire_score,
                :stomp, :comeback, :tower_status_radiant, :tower_status_dire,
                :barracks_status_radiant, :barracks_status_dire
            )""",
            parse_match_basic(match),
        )

        players = parse_players(match)
        if players:
            conn.executemany(
                """INSERT INTO match_players (
                    match_id, account_id, player_slot, hero_id, is_radiant, team_id,
                    kills, deaths, assists, gold_per_min, xp_per_min, net_worth,
                    last_hits, denies, hero_damage, hero_healing, tower_damage, level,
                    item_0, item_1, item_2, item_3, item_4, item_5,
                    backpack_0, backpack_1, backpack_2, item_neutral, firstblood_claimed,
                    gold_10min, lh_10min, xp_10min,
                    kills_10min, deaths_10min, assists_10min,
                    obs_placed_10min, sen_placed_10min,
                    lane_efficiency, lane_role, is_roaming, kda,
                    observer_kills_10min, sentry_kills_10min
                ) VALUES (
                    :match_id, :account_id, :player_slot, :hero_id, :is_radiant, :team_id,
                    :kills, :deaths, :assists, :gold_per_min, :xp_per_min, :net_worth,
                    :last_hits, :denies, :hero_damage, :hero_healing, :tower_damage, :level,
                    :item_0, :item_1, :item_2, :item_3, :item_4, :item_5,
                    :backpack_0, :backpack_1, :backpack_2, :item_neutral, :firstblood_claimed,
                    :gold_10min, :lh_10min, :xp_10min,
                    :kills_10min, :deaths_10min, :assists_10min,
                    :obs_placed_10min, :sen_placed_10min,
                    :lane_efficiency, :lane_role, :is_roaming, :kda,
                    :observer_kills_10min, :sentry_kills_10min
                )""",
                players,
            )

        pb_rows = parse_picks_bans(match)
        if pb_rows:
            conn.executemany(
                """INSERT INTO picks_bans (match_id, hero_id, is_pick, team, ord)
                   VALUES (:match_id, :hero_id, :is_pick, :team, :ord)""",
                pb_rows,
            )

        tfs, tf_players = parse_teamfights(match)
        tf_id_map: dict[int, int] = {}
        for tf in tfs:
            local_idx = tf.pop("tf_local_idx")
            cur = conn.execute(
                """INSERT INTO teamfights (match_id, start_time, end_time, last_death, deaths)
                   VALUES (:match_id, :start_time, :end_time, :last_death, :deaths)""",
                tf,
            )
            tf_id_map[local_idx] = cur.lastrowid

        if tf_players:
            resolved = []
            for tp in tf_players:
                local_idx = tp.pop("tf_local_idx")
                tf_id = tf_id_map.get(local_idx)
                if tf_id is not None:
                    tp["teamfight_id"] = tf_id
                    resolved.append(tp)
            if resolved:
                conn.executemany(
                    """INSERT INTO teamfight_players (
                        teamfight_id, player_slot, deaths, buybacks, damage, healing,
                        gold_delta, xp_delta, kills
                    ) VALUES (
                        :teamfight_id, :player_slot, :deaths, :buybacks, :damage, :healing,
                        :gold_delta, :xp_delta, :kills
                    )""",
                    resolved,
                )

        gold_rows = parse_gold_adv(match)
        if gold_rows:
            conn.executemany(
                """INSERT INTO gold_advantage (match_id, time_min, value)
                   VALUES (:match_id, :time_min, :value)""",
                gold_rows,
            )

        xp_rows = parse_xp_adv(match)
        if xp_rows:
            conn.executemany(
                """INSERT INTO xp_advantage (match_id, time_min, value)
                   VALUES (:match_id, :time_min, :value)""",
                xp_rows,
            )

        obj_rows = parse_objectives(match)
        if obj_rows:
            conn.executemany(
                """INSERT INTO objectives (match_id, time, type, unit, key, player_slot)
                   VALUES (:match_id, :time, :type, :unit, :key, :player_slot)""",
                obj_rows,
            )

        chat_rows = parse_chat(match)
        if chat_rows:
            conn.executemany(
                """INSERT INTO chat (match_id, time, player_slot, type, message)
                   VALUES (:match_id, :time, :player_slot, :type, :message)""",
                chat_rows,
            )

        if commit:
            conn.commit()
        logger.info("Inserted match %d.", match_id)

    def insert_hero_matchups(self, hero_id: int, matchups: list[dict]) -> None:
        conn = self.connect()
        conn.executemany(
            """INSERT OR REPLACE INTO hero_matchups
               (hero_id, vs_hero_id, games_played, wins, synergy)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    hero_id,
                    m["hero_id"],
                    m.get("games_played", 0),
                    m.get("wins", 0),
                    m.get("synergy"),
                )
                for m in matchups
                if m.get("hero_id") != hero_id
            ],
        )
        conn.commit()
        logger.info("Inserted %d matchups for hero %d.", len(matchups) - 1, hero_id)

    def insert_hero_duration_stats(self, hero_id: int, durations: list[dict]) -> None:
        """Insert hero win rate by game duration from OpenDota /heroes/{id}/durations."""
        conn = self.connect()
        conn.executemany(
            """INSERT OR REPLACE INTO hero_duration_stats
               (hero_id, duration_min, games_played, wins)
               VALUES (?, ?, ?, ?)""",
            [
                (hero_id, d["duration_min"], d["games_played"], d["wins"])
                for d in durations
                if d.get("games_played", 0) >= 10
            ],
        )
        conn.commit()

    def insert_hero_benchmarks(self, hero_id: int, benchmarks: dict) -> None:
        """Insert hero performance benchmarks from OpenDota /benchmarks.

        API returns percentiles at [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99].
        We map: p50=0.5, p75=0.7 (nearest), p90=0.9.
        """
        conn = self.connect()
        rows = []
        for metric, percentiles in benchmarks.items():
            p_vals = {p["percentile"]: p["value"] for p in percentiles}
            rows.append((
                hero_id,
                metric,
                p_vals.get(0.5, 0),
                p_vals.get(0.7, p_vals.get(0.8, 0)),
                p_vals.get(0.9, 0),
            ))
        conn.executemany(
            """INSERT OR REPLACE INTO hero_benchmarks
               (hero_id, metric, p50, p75, p90)
               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        logger.info("Inserted %d benchmarks for hero %d.", len(rows), hero_id)

    def update_hero_stats(self, hero_stats: list[dict]) -> None:
        """Update heroes table with stats from /api/heroStats.

        Adds pro_pick, pro_win, pro_ban, and bracket-specific pick/win rates.
        """
        conn = self.connect()
        for hs in hero_stats:
            hero_id = hs["id"]
            conn.execute(
                """UPDATE heroes SET
                   pro_pick = ?, pro_win = ?, pro_ban = ?,
                   pub_pick = ?, pub_win = ?,
                   turbo_picks = ?, turbo_wins = ?
                   WHERE hero_id = ?""",
                (
                    hs.get("pro_pick", 0), hs.get("pro_win", 0), hs.get("pro_ban", 0),
                    hs.get("pub_pick", 0), hs.get("pub_win", 0),
                    hs.get("turbo_picks", 0), hs.get("turbo_wins", 0),
                    hero_id,
                ),
            )
        conn.commit()
        logger.info("Updated stats for %d heroes.", len(hero_stats))

    @staticmethod
    def _delete_match_children(conn: sqlite3.Connection, match_id: int) -> None:
        conn.execute(
            "DELETE FROM teamfight_players WHERE teamfight_id IN "
            "(SELECT id FROM teamfights WHERE match_id = ?)",
            (match_id,),
        )
        conn.execute("DELETE FROM teamfights WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM picks_bans WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM gold_advantage WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM xp_advantage WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM objectives WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM chat WHERE match_id = ?", (match_id,))
