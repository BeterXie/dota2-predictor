"""PostgreSQL compatibility facade for auxiliary fetch commands."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.engine import Engine

from database.engine import build_engine
from database.session import PostgresSession
from live_betting.storage import ALEMBIC_HEAD

from .postgres_store import CoreMatchStore


logger = logging.getLogger(__name__)


class Database:
    """Expose the legacy fetch API on one PostgreSQL session."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        connection: PostgresSession | None = None,
        engine: Engine | None = None,
    ) -> None:
        configured = sum(
            value is not None for value in (database_url, connection, engine)
        )
        if configured != 1:
            raise ValueError(
                "provide exactly one of database_url, connection, or engine"
            )
        if connection is not None:
            self.engine = connection.engine
            self._connection = connection
            self._owns_connection = False
            self._owns_engine = False
        else:
            self.engine = engine or build_engine(database_url)
            self._connection = PostgresSession(self.engine)
            self._owns_connection = True
            self._owns_engine = engine is None

    def connect(self) -> PostgresSession:
        return self._connection

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()
        if self._owns_engine:
            self.engine.dispose()

    def init_db(self, *, external_transaction: bool = False) -> None:
        if external_transaction and not self._connection.in_transaction:
            raise RuntimeError("external transaction is not active")
        revision = self._connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        actual = None if revision is None else str(revision[0])
        if actual != ALEMBIC_HEAD:
            raise RuntimeError(
                f"PostgreSQL schema revision {actual!r} is not {ALEMBIC_HEAD}"
            )

    def is_fetched(self, match_id: int) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM matches AS match
                WHERE match.match_id=?
                  AND EXISTS (
                      SELECT 1 FROM match_players AS player
                       WHERE player.match_id=match.match_id
                  )""",
            (match_id,),
        ).fetchone()
        return row is not None

    def insert_heroes(self, heroes: list[dict]) -> None:
        rows = []
        for hero in heroes:
            internal_name = str(hero.get("name") or "")
            rows.append(
                (
                    hero["id"],
                    hero.get("localized_name"),
                    hero.get("primary_attr"),
                    hero.get("attack_type"),
                    json.dumps(hero.get("roles")),
                    internal_name.replace("npc_dota_hero_", ""),
                )
            )
        if not rows:
            return
        with self._connection.transaction():
            self._connection.executemany(
                """INSERT INTO heroes
                   (hero_id, localized_name, primary_attr, attack_type, roles,
                    hero_key)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (hero_id) DO UPDATE SET
                       localized_name=EXCLUDED.localized_name,
                       primary_attr=EXCLUDED.primary_attr,
                       attack_type=EXCLUDED.attack_type,
                       roles=EXCLUDED.roles,
                       hero_key=EXCLUDED.hero_key""",
                rows,
            )

    def insert_match(self, match: dict, commit: bool = True) -> None:
        if not commit:
            raise ValueError("PostgreSQL fetch writes are always atomic")
        core = CoreMatchStore(engine=self.engine)
        with self._connection.transaction():
            core.insert_match_with_connection(
                self._connection.active_connection,
                match,
            )

    def insert_team(self, team: dict, commit: bool = True) -> None:
        if not commit:
            raise ValueError("PostgreSQL fetch writes are always atomic")
        with self._connection.transaction():
            self._connection.execute(
                """INSERT INTO teams (team_id, name, tag, logo_url)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (team_id) DO UPDATE SET
                       name=EXCLUDED.name,
                       tag=EXCLUDED.tag,
                       logo_url=EXCLUDED.logo_url,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    team["team_id"],
                    team.get("name"),
                    team.get("tag"),
                    team.get("logo_url"),
                ),
            )

    def insert_league(self, league: dict, commit: bool = True) -> None:
        if not commit:
            raise ValueError("PostgreSQL fetch writes are always atomic")
        with self._connection.transaction():
            self._connection.execute(
                """INSERT INTO leagues (leagueid, name, tier)
                   VALUES (?, ?, ?)
                   ON CONFLICT (leagueid) DO UPDATE SET
                       name=EXCLUDED.name,
                       tier=EXCLUDED.tier,
                       updated_at=CURRENT_TIMESTAMP""",
                (league["leagueid"], league.get("name"), league.get("tier")),
            )

    def insert_hero_matchups(self, hero_id: int, matchups: list[dict]) -> None:
        rows = [
            (
                hero_id,
                matchup["hero_id"],
                matchup.get("games_played", 0),
                matchup.get("wins", 0),
                matchup.get("synergy"),
            )
            for matchup in matchups
            if matchup.get("hero_id") != hero_id
        ]
        self._upsert_many(
            """INSERT INTO hero_matchups
               (hero_id, vs_hero_id, games_played, wins, synergy)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (hero_id, vs_hero_id) DO UPDATE SET
                   games_played=EXCLUDED.games_played,
                   wins=EXCLUDED.wins,
                   synergy=EXCLUDED.synergy,
                   updated_at=CURRENT_TIMESTAMP""",
            rows,
        )
        logger.info("Inserted %d matchups for hero %d.", len(rows), hero_id)

    def insert_hero_duration_stats(
        self, hero_id: int, durations: list[dict]
    ) -> None:
        rows = [
            (hero_id, row["duration_min"], row["games_played"], row["wins"])
            for row in durations
            if row.get("games_played", 0) >= 10
        ]
        self._upsert_many(
            """INSERT INTO hero_duration_stats
               (hero_id, duration_min, games_played, wins)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (hero_id, duration_min) DO UPDATE SET
                   games_played=EXCLUDED.games_played,
                   wins=EXCLUDED.wins,
                   updated_at=CURRENT_TIMESTAMP""",
            rows,
        )

    def insert_hero_benchmarks(self, hero_id: int, benchmarks: dict) -> None:
        rows = []
        for metric, percentiles in benchmarks.items():
            values = {point["percentile"]: point["value"] for point in percentiles}
            rows.append(
                (
                    hero_id,
                    metric,
                    values.get(0.5, 0),
                    values.get(0.7, values.get(0.8, 0)),
                    values.get(0.9, 0),
                )
            )
        self._upsert_many(
            """INSERT INTO hero_benchmarks
               (hero_id, metric, p50, p75, p90)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (hero_id, metric) DO UPDATE SET
                   p50=EXCLUDED.p50,
                   p75=EXCLUDED.p75,
                   p90=EXCLUDED.p90,
                   updated_at=CURRENT_TIMESTAMP""",
            rows,
        )

    def update_hero_stats(self, hero_stats: list[dict]) -> None:
        rows = [
            (
                hero.get("pro_pick", 0),
                hero.get("pro_win", 0),
                hero.get("pro_ban", 0),
                hero.get("pub_pick", 0),
                hero.get("pub_win", 0),
                hero.get("turbo_picks", 0),
                hero.get("turbo_wins", 0),
                hero["id"],
            )
            for hero in hero_stats
        ]
        self._upsert_many(
            """UPDATE heroes SET
                   pro_pick=?, pro_win=?, pro_ban=?, pub_pick=?, pub_win=?,
                   turbo_picks=?, turbo_wins=?
               WHERE hero_id=?""",
            rows,
        )

    def _upsert_many(
        self,
        statement: str,
        rows: Sequence[Sequence[Any] | Mapping[str, Any]],
    ) -> None:
        if not rows:
            return
        with self._connection.transaction():
            self._connection.executemany(statement, rows)


__all__ = ["Database"]
