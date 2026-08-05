from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from features.db_reader import read_matches
from features.store import to_db_materialized
from fetch.db import Database
from live_betting.vision_retention import prune_vision_evidence
from scripts.run_dota_shadow_service import service_once


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def _url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_feature_cache_is_alembic_managed_and_roundtrips(
    postgres_engine,
) -> None:
    database_url = _url(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO matches (match_id, start_time, duration, radiant_win) "
                "VALUES (42, 1785369600, 2400, TRUE)"
            )
        )

    matches = read_matches(database_url)
    assert matches["match_id"].tolist() == [42]

    cache = pd.DataFrame(
        [{"match_id": 42, "duration": 2400, "radiant_win": True}]
    )
    to_db_materialized(cache, "match_feature_cache", database_url)

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT match_id, duration, radiant_win "
                "FROM match_feature_cache"
            )
        ).one() == (42, 2400, True)


def test_auxiliary_fetch_facade_uses_postgres(postgres_engine) -> None:
    database = Database(engine=postgres_engine)
    database.init_db()
    database.insert_heroes(
        [
            {"id": 1, "localized_name": "One", "roles": []},
            {"id": 2, "localized_name": "Two", "roles": []},
        ]
    )
    database.insert_hero_matchups(
        1,
        [{"hero_id": 2, "games_played": 100, "wins": 55, "synergy": 0.1}],
    )

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT games_played, wins, synergy FROM hero_matchups "
                "WHERE hero_id=1 AND vs_hero_id=2"
            )
        ).one() == (100, 55, 0.1)
    database.close()


def test_vision_retention_reads_postgres_lineage(postgres_engine, tmp_path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()

    result = prune_vision_evidence(
        _url(postgres_engine),
        evidence_root,
        now=NOW,
        dry_run=True,
    )

    assert result.scanned_files == 0
    assert result.planned_deletions == ()
    assert result.unsafe_paths == 0


def test_service_health_probe_uses_postgres(postgres_engine) -> None:
    result = service_once(
        _url(postgres_engine),
        active_components=set(),
        health_only=True,
    )

    assert result == {"pending_orders": 0}
    with postgres_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT status FROM service_health WHERE component='database'"
            )
        ).scalar_one() == "healthy"
