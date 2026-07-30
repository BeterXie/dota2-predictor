"""PostgreSQL storage for OpenDota match and hero ingestion."""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from database.engine import build_engine

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


class CoreMatchStore:
    """Persist the core OpenDota dataset through one PostgreSQL engine."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if database_url is not None and engine is not None:
            raise ValueError("database_url and engine are mutually exclusive")
        self.engine = engine or build_engine(database_url)
        self._owns_engine = engine is None

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    def hero_count(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT COUNT(*) FROM heroes")
            ).scalar_one()

    def is_fetched(self, match_id: int) -> bool:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT 1
                    FROM matches AS m
                    WHERE m.match_id = :match_id
                      AND EXISTS (
                          SELECT 1
                          FROM match_players AS mp
                          WHERE mp.match_id = m.match_id
                      )
                    """
                ),
                {"match_id": match_id},
            ).first()
        return row is not None

    def insert_heroes(self, heroes: list[dict]) -> None:
        rows = []
        for hero in heroes:
            internal_name = hero.get("name", "")
            hero_key = (
                internal_name.replace("npc_dota_hero_", "")
                if internal_name
                else ""
            )
            rows.append(
                {
                    "hero_id": hero["id"],
                    "localized_name": hero.get("localized_name"),
                    "primary_attr": hero.get("primary_attr"),
                    "attack_type": hero.get("attack_type"),
                    "roles": json.dumps(hero.get("roles")),
                    "hero_key": hero_key,
                }
            )
        if not rows:
            return

        statement = text(
            """
            INSERT INTO heroes (
                hero_id, localized_name, primary_attr, attack_type, roles, hero_key
            ) VALUES (
                :hero_id, :localized_name, :primary_attr, :attack_type, :roles, :hero_key
            )
            ON CONFLICT (hero_id) DO UPDATE SET
                localized_name = EXCLUDED.localized_name,
                primary_attr = EXCLUDED.primary_attr,
                attack_type = EXCLUDED.attack_type,
                roles = EXCLUDED.roles,
                hero_key = EXCLUDED.hero_key
            """
        )
        with self.engine.begin() as connection:
            connection.execute(statement, rows)
        logger.info("Inserted %d heroes.", len(rows))

    def insert_match(self, match: dict) -> None:
        """Replace one match and all of its children atomically."""

        with self.engine.begin() as connection:
            self.insert_match_with_connection(connection, match)

        logger.info("Inserted match %d.", match["match_id"])

    def insert_match_with_connection(
        self,
        connection: Connection,
        match: dict,
    ) -> None:
        """Replace one match inside the caller's existing transaction."""

        match_id = match["match_id"]
        self._delete_match_children(connection, match_id)

        for side in ("radiant", "dire"):
            team = parse_team_info(match, side)
            if team:
                self._upsert_team(connection, team)

        league = parse_league_info(match)
        if league:
            self._upsert_league(connection, league)

        connection.execute(
            text(
                """
                INSERT INTO matches (
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
                )
                ON CONFLICT (match_id) DO UPDATE SET
                    radiant_team_id = EXCLUDED.radiant_team_id,
                    dire_team_id = EXCLUDED.dire_team_id,
                    radiant_win = EXCLUDED.radiant_win,
                    duration = EXCLUDED.duration,
                    game_mode = EXCLUDED.game_mode,
                    lobby_type = EXCLUDED.lobby_type,
                    start_time = EXCLUDED.start_time,
                    first_blood_time = EXCLUDED.first_blood_time,
                    leagueid = EXCLUDED.leagueid,
                    series_id = EXCLUDED.series_id,
                    series_type = EXCLUDED.series_type,
                    patch = EXCLUDED.patch,
                    region = EXCLUDED.region,
                    radiant_score = EXCLUDED.radiant_score,
                    dire_score = EXCLUDED.dire_score,
                    stomp = EXCLUDED.stomp,
                    comeback = EXCLUDED.comeback,
                    tower_status_radiant = EXCLUDED.tower_status_radiant,
                    tower_status_dire = EXCLUDED.tower_status_dire,
                    barracks_status_radiant = EXCLUDED.barracks_status_radiant,
                    barracks_status_dire = EXCLUDED.barracks_status_dire,
                    fetched_at = CURRENT_TIMESTAMP
                """
            ),
            parse_match_basic(match),
        )

        self._insert_match_children(connection, match)

    @staticmethod
    def _upsert_team(connection: Connection, team: dict) -> None:
        connection.execute(
            text(
                """
                INSERT INTO teams (team_id, name, tag, logo_url)
                VALUES (:team_id, :name, :tag, :logo_url)
                ON CONFLICT (team_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    tag = EXCLUDED.tag,
                    logo_url = EXCLUDED.logo_url,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            team,
        )

    @staticmethod
    def _upsert_league(connection: Connection, league: dict) -> None:
        connection.execute(
            text(
                """
                INSERT INTO leagues (leagueid, name, tier)
                VALUES (:leagueid, :name, :tier)
                ON CONFLICT (leagueid) DO UPDATE SET
                    name = EXCLUDED.name,
                    tier = EXCLUDED.tier,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            league,
        )

    @classmethod
    def _insert_match_children(cls, connection: Connection, match: dict) -> None:
        cls._execute_many(
            connection,
            """
            INSERT INTO match_players (
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
            )
            """,
            parse_players(match),
        )
        cls._execute_many(
            connection,
            """
            INSERT INTO picks_bans (match_id, hero_id, is_pick, team, ord)
            VALUES (:match_id, :hero_id, :is_pick, :team, :ord)
            """,
            parse_picks_bans(match),
        )

        teamfights, teamfight_players = parse_teamfights(match)
        teamfight_ids: dict[int, int] = {}
        for teamfight in teamfights:
            local_index = teamfight["tf_local_idx"]
            values = {
                key: value
                for key, value in teamfight.items()
                if key != "tf_local_idx"
            }
            teamfight_ids[local_index] = connection.execute(
                text(
                    """
                    INSERT INTO teamfights (
                        match_id, start_time, end_time, last_death, deaths
                    ) VALUES (
                        :match_id, :start_time, :end_time, :last_death, :deaths
                    )
                    RETURNING id
                    """
                ),
                values,
            ).scalar_one()

        resolved_teamfight_players = []
        for player in teamfight_players:
            teamfight_id = teamfight_ids.get(player["tf_local_idx"])
            if teamfight_id is None:
                continue
            resolved_teamfight_players.append(
                {
                    **{
                        key: value
                        for key, value in player.items()
                        if key != "tf_local_idx"
                    },
                    "teamfight_id": teamfight_id,
                }
            )
        cls._execute_many(
            connection,
            """
            INSERT INTO teamfight_players (
                teamfight_id, player_slot, deaths, buybacks, damage, healing,
                gold_delta, xp_delta, kills
            ) VALUES (
                :teamfight_id, :player_slot, :deaths, :buybacks, :damage, :healing,
                :gold_delta, :xp_delta, :kills
            )
            """,
            resolved_teamfight_players,
        )
        cls._execute_many(
            connection,
            """
            INSERT INTO gold_advantage (match_id, time_min, value)
            VALUES (:match_id, :time_min, :value)
            """,
            parse_gold_adv(match),
        )
        cls._execute_many(
            connection,
            """
            INSERT INTO xp_advantage (match_id, time_min, value)
            VALUES (:match_id, :time_min, :value)
            """,
            parse_xp_adv(match),
        )
        cls._execute_many(
            connection,
            """
            INSERT INTO objectives (match_id, time, type, unit, key, player_slot)
            VALUES (:match_id, :time, :type, :unit, :key, :player_slot)
            """,
            parse_objectives(match),
        )
        cls._execute_many(
            connection,
            """
            INSERT INTO chat (match_id, time, player_slot, type, message)
            VALUES (:match_id, :time, :player_slot, :type, :message)
            """,
            parse_chat(match),
        )

    @staticmethod
    def _execute_many(
        connection: Connection,
        statement: str,
        rows: list[dict],
    ) -> None:
        if rows:
            connection.execute(text(statement), rows)

    @staticmethod
    def _delete_match_children(connection: Connection, match_id: int) -> None:
        parameters = {"match_id": match_id}
        connection.execute(
            text(
                """
                DELETE FROM teamfight_players
                WHERE teamfight_id IN (
                    SELECT id FROM teamfights WHERE match_id = :match_id
                )
                """
            ),
            parameters,
        )
        for table in (
            "teamfights",
            "match_players",
            "picks_bans",
            "gold_advantage",
            "xp_advantage",
            "objectives",
            "chat",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE match_id = :match_id"),
                parameters,
            )
