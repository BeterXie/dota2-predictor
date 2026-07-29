from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

from live_betting.storage import (
    CURRENT_SCHEMA_VERSION,
    LiveBettingStore,
    query_rosh_lineup_score_for_trusted_draft,
)
from live_betting.models import Market, ModelQuote, OddsSnapshot
from live_betting.strategy import make_order
from live_betting.stratz_rosh_client import (
    FetchedRoshLineupScore,
    ROSH_FORMULA_VERSION,
    canonical_evidence_hash,
    rosh_cache_week_start,
)


RADIANT = (1, 2, 3, 4, 5)
DIRE = (6, 7, 8, 9, 10)


def fetched_score(
    started: datetime,
    *,
    completed: datetime | None = None,
    pure: float = 12.5,
    adjusted: float | None = None,
    coverage: int = 0,
) -> FetchedRoshLineupScore:
    source_as_of = completed or started + timedelta(seconds=2)
    mode = "player_adjusted" if coverage == 10 else "pure"
    effective = adjusted if mode == "player_adjusted" else pure
    pure_bucket = {
        "minute": 60,
        "time_start": 59,
        "time_end": 60,
        "advantage_side": "radiant" if pure > 0 else "dire" if pure < 0 else "even",
        "advantage_percent": abs(pure),
        "radiant_advantage": max(pure, 0.0),
        "dire_advantage": max(-pure, 0.0),
        "match_percentage": 50.0,
        "win_rate_graph": pure,
        "hero_adjustment": pure,
        "hero_base_adjustment": pure,
        "hero_tempo_adjustment": 0.0,
        "synergy_adjustment": 0.0,
        "player_adjustment": 0.0,
    }
    evidence = {
        "source": "stratz",
        "formula_version": ROSH_FORMULA_VERSION,
        "source_week": int(started.timestamp()),
        "source_as_of": source_as_of.isoformat(),
        "cache_week_start": rosh_cache_week_start(started),
        "pure_minute_table": [pure_bucket],
        "score": {
            "pure_lineup_score": pure,
            "player_adjusted_lineup_score": adjusted if mode == "player_adjusted" else None,
            "effective_lineup_score": effective,
            "scoring_mode": mode,
            "player_coverage_count": coverage,
        },
    }
    if mode == "player_adjusted":
        evidence["minute_table"] = [
            {**pure_bucket, "win_rate_graph": adjusted, "player_adjustment": adjusted - pure}
        ]
    return FetchedRoshLineupScore(
        pure_lineup_score=pure,
        player_adjusted_lineup_score=adjusted if mode == "player_adjusted" else None,
        effective_lineup_score=float(effective),
        scoring_mode=mode,
        player_coverage_count=coverage,
        stake_multiplier=1.0 if mode == "player_adjusted" else 0.5,
        formula_version=ROSH_FORMULA_VERSION,
        source_name="stratz",
        source_week=int(started.timestamp()),
        cache_week_start=rosh_cache_week_start(started),
        source_as_of=source_as_of,
        evidence=evidence,
        evidence_hash=canonical_evidence_hash(evidence),
    )


@pytest.fixture
def store() -> LiveBettingStore:
    result = LiveBettingStore(":memory:")
    result.init_schema()
    result.connection.execute("PRAGMA foreign_keys=OFF")
    yield result
    result.close()


def insert(
    store: LiveBettingStore,
    score: FetchedRoshLineupScore,
    *,
    created_at: datetime,
    radiant_players: tuple[int | None, ...] | None = None,
    dire_players: tuple[int | None, ...] | None = None,
):
    draft_hash = store.rosh_draft_hash(RADIANT, DIRE)
    with patch.object(store, "_trusted_rosh_draft", return_value=True):
        return store.insert_rosh_lineup_score(
            score,
            raybet_match_id="1001",
            map_number=1,
            strict_mapping_id=7,
            draft_hash=draft_hash,
            radiant_hero_ids=RADIANT,
            dire_hero_ids=DIRE,
            radiant_player_ids=radiant_players,
            dire_player_ids=dire_players,
            created_at=created_at,
        )


def test_schema_current_is_append_only_and_does_not_cap_total_score(
    store: LiveBettingStore,
) -> None:
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    score = insert(
        store,
        fetched_score(started, pure=145.25),
        created_at=started + timedelta(minutes=2),
    )

    assert score is not None
    assert score.pure_lineup_score == 145.25
    assert store.connection.execute(
        "SELECT MAX(version) FROM live_schema_version"
    ).fetchone()[0] == CURRENT_SCHEMA_VERSION
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "UPDATE rosh_lineup_scores SET pure_lineup_score=0 WHERE score_key=?",
            (score.score_key,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "DELETE FROM rosh_lineup_scores WHERE score_key=?",
            (score.score_key,),
        )


def test_old_source_can_bind_after_anchor_but_future_source_is_rejected(
    store: LiveBettingStore,
) -> None:
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    assert insert(
        store,
        fetched_score(started, completed=started + timedelta(seconds=5)),
        created_at=started + timedelta(minutes=3),
    ) is not None
    assert insert(
        store,
        fetched_score(started, completed=started + timedelta(minutes=4)),
        created_at=started + timedelta(minutes=3),
    ) is None


def test_cache_is_ttl_bounded_and_player_identity_exact(
    store: LiveBettingStore,
) -> None:
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    players_a = (101, 102, 103, 104, 105)
    players_b = (201, 202, 203, 204, 205)
    score = fetched_score(started, adjusted=17.0, coverage=10)
    inserted = insert(
        store,
        score,
        radiant_players=players_a,
        dire_players=players_b,
        created_at=started + timedelta(seconds=3),
    )
    assert inserted is not None
    draft_hash = store.rosh_draft_hash(RADIANT, DIRE)

    assert store.find_rosh_lineup_score(
        draft_hash=draft_hash,
        formula_version=ROSH_FORMULA_VERSION,
        cache_week_start=rosh_cache_week_start(started),
        radiant_hero_ids=RADIANT,
        dire_hero_ids=DIRE,
        radiant_player_ids=players_a,
        dire_player_ids=players_b,
        as_of=started + timedelta(minutes=5),
    ) is not None
    assert store.find_rosh_lineup_score(
        draft_hash=draft_hash,
        formula_version=ROSH_FORMULA_VERSION,
        cache_week_start=rosh_cache_week_start(started),
        radiant_hero_ids=RADIANT,
        dire_hero_ids=DIRE,
        radiant_player_ids=players_b,
        dire_player_ids=players_a,
        as_of=started + timedelta(minutes=5),
    ) is None
    assert store.find_rosh_lineup_score(
        draft_hash=draft_hash,
        formula_version=ROSH_FORMULA_VERSION,
        cache_week_start=rosh_cache_week_start(started),
        radiant_hero_ids=RADIANT,
        dire_hero_ids=DIRE,
        radiant_player_ids=players_a,
        dire_player_ids=players_b,
        as_of=started + timedelta(minutes=16),
    ) is None


def test_read_helper_rejects_conflict_status_without_a_valid_future_cutoff(
    store: LiveBettingStore,
) -> None:
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    score = insert(
        store,
        fetched_score(started),
        created_at=started + timedelta(seconds=3),
    )
    assert score is not None
    draft_hash = store.rosh_draft_hash(RADIANT, DIRE)
    store.connection.execute(
        """INSERT INTO vision_draft_anchors
           (raybet_match_id, map_number, draft_hash, radiant_hero_ids,
            dire_hero_ids, radiant_team_side, team_side_anchored_at,
            team_side_source_frame_ref, anchored_at, source_frame_ref,
            status, conflict_at)
           VALUES ('1001', 1, ?, '[1,2,3,4,5]', '[6,7,8,9,10]',
                   'team_one', ?, 'frame', ?, 'frame', 'anchored', NULL)""",
        (draft_hash, started.isoformat(), started.isoformat()),
    )
    store.connection.execute("DROP TRIGGER vision_draft_anchor_identity_immutable")
    store.connection.execute(
        """UPDATE vision_draft_anchors
              SET status='conflict', conflict_at=NULL
            WHERE raybet_match_id='1001' AND map_number=1"""
    )
    mapping = SimpleNamespace(
        eligible=True,
        mapping=SimpleNamespace(raybet_match_id="1001", map_number=1),
    )

    with patch(
        "live_betting.strict_eligibility.query_strict_mapping_snapshot",
        return_value=mapping,
    ):
        result = query_rosh_lineup_score_for_trusted_draft(
            store.connection,
            raybet_match_id="1001",
            map_number=1,
            strict_mapping_id=7,
            draft_hash=draft_hash,
            radiant_hero_ids=RADIANT,
            dire_hero_ids=DIRE,
            as_of=started + timedelta(minutes=1),
        )

    assert result is None


def test_cache_partition_uses_query_start_week_across_week_boundary(
    store: LiveBettingStore,
) -> None:
    started = datetime(2026, 7, 26, 23, 59, 58, tzinfo=timezone.utc)
    completed = started + timedelta(seconds=4)
    score = fetched_score(started, completed=completed)
    inserted = insert(store, score, created_at=completed)

    assert inserted is not None
    assert inserted.cache_week_start == rosh_cache_week_start(started)
    assert inserted.cache_week_start != rosh_cache_week_start(completed)

    wrong_evidence = dict(score.evidence)
    wrong_evidence["cache_week_start"] = rosh_cache_week_start(completed)
    wrong_partition = replace(
        score,
        cache_week_start=rosh_cache_week_start(completed),
        evidence=wrong_evidence,
        evidence_hash=canonical_evidence_hash(wrong_evidence),
    )
    assert insert(store, wrong_partition, created_at=completed) is None


@pytest.mark.parametrize("mutation", ["minute_out_of_range", "missing_field"])
def test_invalid_minute_curve_evidence_is_rejected(
    store: LiveBettingStore,
    mutation: str,
) -> None:
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    score = fetched_score(started)
    evidence = dict(score.evidence)
    table = [dict(evidence["pure_minute_table"][0])]
    if mutation == "minute_out_of_range":
        table[0]["minute"] = 61
    else:
        table[0].pop("hero_adjustment")
    evidence["pure_minute_table"] = table
    corrupted = replace(
        score,
        evidence=evidence,
        evidence_hash=canonical_evidence_hash(evidence),
    )

    assert insert(
        store,
        corrupted,
        created_at=started + timedelta(seconds=3),
    ) is None


def test_decision_input_excludes_full_curves_and_exposes_stake_cap(
    store: LiveBettingStore,
) -> None:
    started = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    inserted = insert(
        store,
        fetched_score(started),
        created_at=started + timedelta(seconds=3),
    )

    assert inserted is not None
    input_ref = inserted.as_input_ref()
    assert input_ref["stake_cap"] == 0.5
    assert "pure_minute_table" not in input_ref["evidence"]
    assert "minute_table" not in input_ref["evidence"]


@pytest.mark.parametrize("stake", [0.1, 0.5, 1.0])
def test_order_decision_identity_includes_dynamic_stake(stake: float) -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    market = Market("winner", "map_1", "team_two", None, "team_two", True)
    snapshot = OddsSnapshot(
        "1001", "odds-1", "group-1", now, 2.5, 1, market
    )
    quote = ModelQuote(
        "1001", "map_1", market, 0.6, 0.4, 0.2, now, "strategy-v1", "input-1"
    )
    order = make_order(
        quote,
        snapshot,
        min_edge=0.01,
        signal_transport_key="transport-1",
        signal_transport_at=now,
        stake_multiplier=stake,
    )
    assert order is not None
    row = {
        "decision_key": "decision-1",
        "strategy_version": quote.strategy_version,
        "input_ref": quote.input_ref,
        "draft_strict_mapping_id": 7,
    }
    fake_store = SimpleNamespace(
        connection=MagicMock(),
        _iso=lambda value: value.isoformat(),
    )
    fake_store.connection.execute.return_value.fetchall.return_value = [row]

    matches = LiveBettingStore._matching_strategy_decision_candidates(
        fake_store, order, 1, 7
    )
    tampered = replace(order, stake=0.2 if stake != 0.2 else 0.3)
    tampered_matches = LiveBettingStore._matching_strategy_decision_candidates(
        fake_store, tampered, 1, 7
    )

    assert matches == [("decision-1", 7)]
    assert tampered_matches == []
