from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from event_intelligence.storage import (
    CURRENT_SCHEMA_VERSION,
    IntelligenceStorage,
    query_historical_rosh_lineup_score,
    query_historical_rosh_lineup_score_for_match,
)
from live_betting.stratz_rosh_client import ROSH_FORMULA_VERSION


RADIANT_HEROES = (1, 2, 3, 4, 5)
DIRE_HEROES = (6, 7, 8, 9, 10)
RADIANT_PLAYERS = (101, 102, 103, 104, 105)
DIRE_PLAYERS = (201, 202, 203, 204, 205)
MATCH_END = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
PLAYER_STATS_AT = datetime(2026, 7, 22, 8, tzinfo=timezone.utc)
CREATED_AT = PLAYER_STATS_AT + timedelta(seconds=1)


def minute_bucket(score: float, *, player_adjustment: float = 0.0) -> dict:
    return {
        "minute": 60,
        "time_start": 59,
        "time_end": 60,
        "advantage_side": "radiant" if score > 0 else "dire" if score < 0 else "even",
        "advantage_percent": abs(score),
        "radiant_advantage": max(score, 0.0),
        "dire_advantage": max(-score, 0.0),
        "match_percentage": 50.0,
        "win_rate_graph": score,
        "hero_adjustment": score - player_adjustment,
        "hero_base_adjustment": score - player_adjustment,
        "hero_tempo_adjustment": 0.0,
        "synergy_adjustment": 0.0,
        "player_adjustment": player_adjustment,
    }


def score_evidence(*, formula_version: str = ROSH_FORMULA_VERSION) -> dict:
    return {
        "historical_match_id": 123,
        "source": "stratz",
        "formula_version": formula_version,
        "source_week": int(MATCH_END.timestamp()),
        "source_as_of": MATCH_END.isoformat(),
        "player_stats_as_of": PLAYER_STATS_AT.isoformat(),
        "retrospective": True,
        "current_player_adjustment_only": True,
        "backtest_eligible": False,
        "pure_minute_table": [minute_bucket(4.2)],
        "minute_table": [minute_bucket(5.1, player_adjustment=0.9)],
        "score": {
            "pure_lineup_score": 4.2,
            "current_player_adjusted_lineup_score": 5.1,
            "effective_lineup_score": 5.1,
            "scoring_mode": "current_player_adjusted",
            "player_coverage_count": 10,
        },
    }


@pytest.fixture
def storage(tmp_path):
    result = IntelligenceStorage(tmp_path / "historical-rosh.db")
    result.init_schema()
    event_id = str(
        result.connection.execute(
            "SELECT event_id FROM event_registry ORDER BY event_id LIMIT 1"
        ).fetchone()[0]
    )
    result.execute(
        """INSERT INTO match_ingest_status
           (match_id, event_id, discovered_at, updated_at)
           VALUES (123, ?, ?, ?)""",
        (event_id, MATCH_END.isoformat(), MATCH_END.isoformat()),
    )
    yield result
    result.close()


def insert_score(
    storage: IntelligenceStorage,
    *,
    formula_version: str = ROSH_FORMULA_VERSION,
    evidence: dict | None = None,
):
    return storage.insert_historical_rosh_lineup_score(
        match_id=123,
        radiant_hero_ids=RADIANT_HEROES,
        dire_hero_ids=DIRE_HEROES,
        radiant_player_ids=RADIANT_PLAYERS,
        dire_player_ids=DIRE_PLAYERS,
        pure_lineup_score=4.2,
        current_player_adjusted_lineup_score=5.1,
        effective_lineup_score=5.1,
        scoring_mode="current_player_adjusted",
        player_coverage_count=10,
        source_week=int(MATCH_END.timestamp()),
        source_as_of=MATCH_END,
        player_stats_as_of=PLAYER_STATS_AT,
        formula_version=formula_version,
        evidence=score_evidence(formula_version=formula_version) if evidence is None else evidence,
        created_at=CREATED_AT,
    )


def test_historical_rosh_storage_is_idempotent_versioned_and_identity_bound(
    storage: IntelligenceStorage,
) -> None:
    first = insert_score(storage)
    second = insert_score(storage)
    insert_score(storage, formula_version="dematus-rosh-old")

    assert first.score_key == second.score_key
    assert storage.connection.execute(
        "SELECT MAX(version) FROM intelligence_schema_version"
    ).fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert storage.connection.execute(
        "SELECT COUNT(*) FROM historical_rosh_lineup_scores"
    ).fetchone()[0] == 2
    current = query_historical_rosh_lineup_score(
        storage.connection,
        match_id=123,
        formula_version=ROSH_FORMULA_VERSION,
        radiant_hero_ids=RADIANT_HEROES,
        dire_hero_ids=DIRE_HEROES,
        radiant_player_ids=RADIANT_PLAYERS,
        dire_player_ids=DIRE_PLAYERS,
    )
    assert current == first
    assert current is not None and current.backtest_eligible is False
    assert query_historical_rosh_lineup_score_for_match(
        storage.connection,
        match_id=123,
        formula_version=ROSH_FORMULA_VERSION,
    ) == first
    assert query_historical_rosh_lineup_score(
        storage.connection,
        match_id=123,
        formula_version=ROSH_FORMULA_VERSION,
        radiant_hero_ids=RADIANT_HEROES,
        dire_hero_ids=DIRE_HEROES,
        radiant_player_ids=(999, *RADIANT_PLAYERS[1:]),
        dire_player_ids=DIRE_PLAYERS,
    ) is None


def test_historical_rosh_rows_are_append_only(storage: IntelligenceStorage) -> None:
    score = insert_score(storage)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        storage.connection.execute(
            "UPDATE historical_rosh_lineup_scores SET pure_lineup_score=0 WHERE score_key=?",
            (score.score_key,),
        )
    storage.connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        storage.connection.execute(
            "DELETE FROM historical_rosh_lineup_scores WHERE score_key=?",
            (score.score_key,),
        )
    storage.connection.rollback()


@pytest.mark.parametrize("mutation", ["score_conflict", "nonfinite_curve"])
def test_historical_rosh_rejects_contradictory_evidence(
    storage: IntelligenceStorage,
    mutation: str,
) -> None:
    evidence = score_evidence()
    if mutation == "score_conflict":
        evidence["score"]["pure_lineup_score"] = 99.0
    else:
        evidence["pure_minute_table"][0]["win_rate_graph"] = float("inf")

    with pytest.raises(ValueError, match="evidence does not match"):
        insert_score(storage, evidence=evidence)
