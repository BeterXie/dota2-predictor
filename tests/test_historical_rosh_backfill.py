from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from event_intelligence.historical_rosh_backfill import (
    backfill_historical_rosh_scores,
    historical_rosh_score_should_append,
    persist_historical_rosh_score,
)
from event_intelligence.storage import (
    IntelligenceStorage,
    query_historical_rosh_lineup_score,
)
from live_betting.stratz_rosh_client import (
    ROSH_FORMULA_VERSION,
    FetchedHistoricalRoshLineupScore,
    FetchedHistoricalRoshScore,
    StratzRoshError,
    canonical_evidence_hash,
)


NOW = datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)
MATCH_END = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)


class FakeStorage:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.inserted: list[dict[str, Any]] = []

    def insert_historical_rosh_lineup_score(self, **values: Any) -> object:
        self.inserted.append(values)
        return object()


class FakeClient:
    def __init__(self, results: dict[int, FetchedHistoricalRoshScore]) -> None:
        self.results = results
        self.calls: list[tuple[int, bool]] = []

    def fetch_historical_match_score(
        self,
        match_id: int,
        *,
        include_current_player_adjustment: bool = False,
    ) -> FetchedHistoricalRoshScore:
        self.calls.append((match_id, include_current_player_adjustment))
        return self.results[match_id]


class SequenceClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def fetch_historical_match_score(
        self,
        _match_id: int,
        *,
        include_current_player_adjustment: bool = False,
    ) -> FetchedHistoricalRoshScore:
        del include_current_player_adjustment
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, FetchedHistoricalRoshScore)
        return outcome


def database(
    *match_ids: int,
    slot_hero_ids: tuple[int, ...] = tuple(range(1, 11)),
) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE formal_map_eligibility (match_id INTEGER PRIMARY KEY)"
    )
    connection.execute(
        """CREATE TABLE match_players (
               match_id INTEGER,
               player_slot INTEGER,
               hero_id INTEGER,
               account_id INTEGER,
               is_radiant INTEGER
           )"""
    )
    slots = [0, 1, 2, 3, 4, 128, 129, 130, 131, 132]
    for match_id in match_ids:
        connection.execute(
            "INSERT INTO formal_map_eligibility VALUES (?)", (match_id,)
        )
        connection.executemany(
            "INSERT INTO match_players VALUES (?, ?, ?, ?, ?)",
            [
                (
                    match_id,
                    slot,
                    slot_hero_ids[index],
                    slot_hero_ids[index] + 99,
                    int(index < 5),
                )
                for index, slot in enumerate(slots)
            ],
        )
    return connection


def fetched(
    match_id: int,
    *,
    mismatched_account: bool = False,
    player_coverage_count: int = 10,
) -> FetchedHistoricalRoshScore:
    players = []
    radiant_picks = []
    dire_picks = []
    for index in range(10):
        hero_id = index + 1
        account_id = index + 100
        if mismatched_account and index == 0:
            account_id = 999
        players.append(
            {
                "heroId": hero_id,
                "position": f"POSITION_{(index % 5) + 1}",
                "steamAccountId": account_id,
                "isAnonymous": False,
            }
        )
        pick = {"heroId": hero_id, "positionId": (index % 5) + 1}
        (radiant_picks if index < 5 else dire_picks).append(pick)
    current_adjusted = 56.0 if player_coverage_count == 10 else None
    mode = "current_player_adjusted" if current_adjusted is not None else "pure"
    effective = current_adjusted if current_adjusted is not None else 55.0

    def bucket(win_rate: float, player_adjustment: float) -> dict[str, object]:
        return {
            "minute": 60,
            "time_start": 60,
            "time_end": 60,
            "advantage_side": "radiant",
            "advantage_percent": 5.0,
            "radiant_advantage": 5.0,
            "dire_advantage": -5.0,
            "match_percentage": 100.0,
            "win_rate_graph": win_rate,
            "hero_adjustment": 5.0,
            "hero_base_adjustment": 3.0,
            "hero_tempo_adjustment": 2.0,
            "synergy_adjustment": 0.0,
            "player_adjustment": player_adjustment,
        }

    player_stats_as_of = NOW if player_coverage_count else None
    evidence: dict[str, object] = {
        "historical_match_id": match_id,
        "source": "stratz",
        "formula_version": ROSH_FORMULA_VERSION,
        "source_week": int(MATCH_END.timestamp()),
        "source_as_of": MATCH_END.isoformat(),
        "player_stats_as_of": (
            player_stats_as_of.isoformat()
            if player_stats_as_of is not None
            else None
        ),
        "retrospective": True,
        "current_player_adjustment_only": True,
        "backtest_eligible": False,
        "pure_minute_table": [bucket(55.0, 0.0)],
        "score": {
            "pure_lineup_score": 55.0,
            "current_player_adjusted_lineup_score": current_adjusted,
            "effective_lineup_score": effective,
            "scoring_mode": mode,
            "player_coverage_count": player_coverage_count,
        },
    }
    if current_adjusted is not None:
        evidence["minute_table"] = [bucket(56.0, 1.0)]
    score = FetchedHistoricalRoshLineupScore(
        pure_lineup_score=55.0,
        current_player_adjusted_lineup_score=current_adjusted,
        effective_lineup_score=effective,
        scoring_mode=mode,
        player_coverage_count=player_coverage_count,
        formula_version=ROSH_FORMULA_VERSION,
        source_name="stratz",
        source_week=int(MATCH_END.timestamp()),
        cache_week_start=int(MATCH_END.timestamp()),
        source_as_of=MATCH_END,
        player_stats_as_of=player_stats_as_of,
        evidence=evidence,
        evidence_hash=canonical_evidence_hash(evidence),
    )
    return FetchedHistoricalRoshScore(
        context={
            "match": {"id": match_id, "players": players},
            "radiant_picks": radiant_picks,
            "dire_picks": dire_picks,
        },
        score=score,
        minute_table=(),
    )


def test_single_match_backfill_persists_position_ordered_strict_identity() -> None:
    storage = FakeStorage(database(1))
    client = FakeClient({1: fetched(1)})

    report = backfill_historical_rosh_scores(
        storage,
        client,  # type: ignore[arg-type]
        match_id=1,
        clock=lambda: NOW,
        existing_query=lambda *_args: None,
    )

    assert (report.inserted, report.skipped, report.failed) == (1, 0, 0)
    assert client.calls == [(1, True)]
    assert storage.inserted[0]["radiant_hero_ids"] == (1, 2, 3, 4, 5)
    assert storage.inserted[0]["dire_player_ids"] == (105, 106, 107, 108, 109)
    assert storage.inserted[0]["scoring_mode"] == "current_player_adjusted"


def test_identity_mismatch_is_rejected_before_storage() -> None:
    storage = FakeStorage(database(1))
    client = FakeClient({1: fetched(1, mismatched_account=True)})

    report = backfill_historical_rosh_scores(
        storage,
        client,  # type: ignore[arg-type]
        match_id=1,
        clock=lambda: NOW,
        existing_query=lambda *_args: None,
    )

    assert report.failed == 1
    assert storage.inserted == []
    assert "identities disagree" in report.failures[0].error


def test_current_formula_is_skipped_before_network_for_resume() -> None:
    storage = FakeStorage(database(1))
    client = FakeClient({1: fetched(1)})

    report = backfill_historical_rosh_scores(
        storage,
        client,  # type: ignore[arg-type]
        match_id=1,
        existing_query=lambda *_args: SimpleNamespace(
            scoring_mode="current_player_adjusted",
            player_coverage_count=10,
        ),
    )

    assert (report.inserted, report.skipped, report.failed) == (0, 1, 0)
    assert client.calls == []
    assert storage.inserted == []


def test_one_failed_match_does_not_block_later_match() -> None:
    storage = FakeStorage(database(1, 2))
    client = FakeClient(
        {1: fetched(1, mismatched_account=True), 2: fetched(2)}
    )

    report = backfill_historical_rosh_scores(
        storage,
        client,  # type: ignore[arg-type]
        limit=2,
        clock=lambda: NOW,
        existing_query=lambda *_args: None,
        throttle_seconds=0,
    )

    assert (report.inserted, report.skipped, report.failed) == (1, 0, 1)
    assert [row["match_id"] for row in storage.inserted] == [2]


def test_consecutive_limited_batches_advance_past_completed_matches() -> None:
    storage = FakeStorage(database(1, 2, 3, 4))
    client = FakeClient({match_id: fetched(match_id) for match_id in range(1, 5)})
    completed: set[int] = set()

    def existing(
        _connection: sqlite3.Connection,
        local: object,
        _formula_version: str,
    ) -> object | None:
        if local.match_id not in completed:  # type: ignore[attr-defined]
            return None
        return SimpleNamespace(
            scoring_mode="current_player_adjusted",
            player_coverage_count=10,
        )

    def persist(
        _storage: object,
        match_id: int,
        _identity: object,
        _score: object,
        _created_at: datetime,
    ) -> bool:
        completed.add(match_id)
        return True

    first = backfill_historical_rosh_scores(
        storage,
        client,  # type: ignore[arg-type]
        limit=2,
        clock=lambda: NOW,
        existing_query=existing,
        persist_score=persist,
        throttle_seconds=0,
    )
    second = backfill_historical_rosh_scores(
        storage,
        client,  # type: ignore[arg-type]
        limit=2,
        clock=lambda: NOW,
        existing_query=existing,
        persist_score=persist,
        throttle_seconds=0,
    )

    assert (first.inserted, first.failed) == (2, 0)
    assert (second.inserted, second.failed) == (2, 0)
    assert [match_id for match_id, _adjusted in client.calls] == [1, 2, 3, 4]
    assert completed == {1, 2, 3, 4}


def test_pure_only_row_is_upgraded_by_default_current_adjustment_run() -> None:
    storage = FakeStorage(database(1))
    pure_client = FakeClient({1: fetched(1, player_coverage_count=0)})
    first = backfill_historical_rosh_scores(
        storage,
        pure_client,  # type: ignore[arg-type]
        match_id=1,
        include_current_player_adjustment=False,
        clock=lambda: NOW,
        existing_query=lambda *_args: None,
    )
    assert first.inserted == 1
    assert storage.inserted[-1]["scoring_mode"] == "pure"

    adjusted_client = FakeClient({1: fetched(1)})
    second = backfill_historical_rosh_scores(
        storage,
        adjusted_client,  # type: ignore[arg-type]
        match_id=1,
        clock=lambda: NOW,
        existing_query=lambda *_args: SimpleNamespace(
            scoring_mode="pure",
            player_coverage_count=0,
        ),
    )
    assert (second.inserted, second.skipped, second.failed) == (1, 0, 0)
    assert adjusted_client.calls == [(1, True)]
    assert storage.inserted[-1]["scoring_mode"] == "current_player_adjusted"


def test_nine_of_ten_row_retries_and_recovers_to_ten_of_ten() -> None:
    storage = FakeStorage(database(1))
    incomplete_client = FakeClient({1: fetched(1, player_coverage_count=9)})
    first = backfill_historical_rosh_scores(
        storage,
        incomplete_client,  # type: ignore[arg-type]
        match_id=1,
        clock=lambda: NOW,
        existing_query=lambda *_args: None,
    )
    assert first.inserted == 1
    assert storage.inserted[-1]["player_coverage_count"] == 9

    recovered_client = FakeClient({1: fetched(1)})
    second = backfill_historical_rosh_scores(
        storage,
        recovered_client,  # type: ignore[arg-type]
        match_id=1,
        clock=lambda: NOW,
        existing_query=lambda *_args: SimpleNamespace(
            scoring_mode="pure",
            player_coverage_count=9,
        ),
    )
    assert (second.inserted, second.skipped, second.failed) == (1, 0, 0)
    assert storage.inserted[-1]["player_coverage_count"] == 10


def test_partial_score_only_appends_when_coverage_strictly_improves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = SimpleNamespace(player_coverage_count=7)
    storage = FakeStorage(database(1))
    identity = {
        "radiant_hero_ids": (1, 2, 3, 4, 5),
        "dire_hero_ids": (6, 7, 8, 9, 10),
        "radiant_player_ids": (100, 101, 102, 103, 104),
        "dire_player_ids": (105, 106, 107, 108, 109),
    }
    monkeypatch.setattr(
        "event_intelligence.historical_rosh_backfill."
        "existing_historical_rosh_score_for_identity",
        lambda *_args, **_kwargs: existing,
    )

    lower = fetched(1, player_coverage_count=6).score
    same = fetched(1, player_coverage_count=7).score
    higher = fetched(1, player_coverage_count=8).score
    assert lower is not None and same is not None and higher is not None

    assert not historical_rosh_score_should_append(existing, lower)
    assert not persist_historical_rosh_score(storage, 1, identity, same, NOW)
    assert storage.inserted == []
    assert persist_historical_rosh_score(storage, 1, identity, higher, NOW)
    assert len(storage.inserted) == 1
    assert storage.inserted[0]["player_coverage_count"] == 8


def test_retryable_429_uses_bounded_backoff_then_succeeds() -> None:
    storage = FakeStorage(database(1))
    client = SequenceClient(
        [
            StratzRoshError(
                "STRATZ request returned HTTP 429",
                retryable=True,
                retry_after_seconds=0.75,
                category="http_429",
            ),
            fetched(1),
        ]
    )
    delays: list[float] = []

    report = backfill_historical_rosh_scores(
        storage,
        client,  # type: ignore[arg-type]
        match_id=1,
        clock=lambda: NOW,
        existing_query=lambda *_args: None,
        max_attempts=3,
        initial_backoff_seconds=0.5,
        throttle_seconds=0,
        sleep=delays.append,
    )

    assert (report.inserted, report.failed) == (1, 0)
    assert client.calls == 2
    assert delays == [0.75]


def test_graphql_business_error_is_not_retried_or_exposed() -> None:
    storage = FakeStorage(database(1))
    client = SequenceClient(
        [
            StratzRoshError(
                "STRATZ GraphQL request failed: secret-token",
                category="graphql_failure",
            )
        ]
    )
    delays: list[float] = []

    report = backfill_historical_rosh_scores(
        storage,
        client,  # type: ignore[arg-type]
        match_id=1,
        existing_query=lambda *_args: None,
        max_attempts=3,
        throttle_seconds=0,
        sleep=delays.append,
    )

    assert report.failed == 1
    assert client.calls == 1
    assert delays == []
    assert report.failures[0].error == "StratzRoshError: graphql_failure"
    assert "secret-token" not in report.failures[0].error


def test_slot_order_storage_matches_exact_api_and_old_position_order_does_not_skip() -> None:
    storage = IntelligenceStorage(":memory:")
    storage.init_schema()
    try:
        event_id = storage.connection.execute(
            "SELECT event_id FROM formal_events ORDER BY event_id LIMIT 1"
        ).fetchone()[0]
        storage.connection.execute(
            """INSERT INTO match_ingest_status
               (match_id, event_id, stage_scope, stage_in_scope,
                has_valid_result, discovered_at, updated_at)
               VALUES (1, ?, 'main_event', 1, 1, ?, ?)""",
            (event_id, MATCH_END.isoformat(), MATCH_END.isoformat()),
        )
        storage.connection.execute(
            """CREATE TABLE match_players (
                   match_id INTEGER,
                   player_slot INTEGER,
                   hero_id INTEGER,
                   account_id INTEGER,
                   is_radiant INTEGER
               )"""
        )
        slots = (0, 1, 2, 3, 4, 128, 129, 130, 131, 132)
        slot_heroes = (2, 1, 3, 4, 5, 7, 6, 8, 9, 10)
        storage.connection.executemany(
            "INSERT INTO match_players VALUES (1, ?, ?, ?, ?)",
            (
                (slot, hero_id, hero_id + 99, int(index < 5))
                for index, (slot, hero_id) in enumerate(zip(slots, slot_heroes))
            ),
        )
        storage.connection.commit()

        old = fetched(1).score
        assert old is not None
        storage.insert_historical_rosh_lineup_score(
            match_id=1,
            radiant_hero_ids=(1, 2, 3, 4, 5),
            dire_hero_ids=(6, 7, 8, 9, 10),
            radiant_player_ids=(100, 101, 102, 103, 104),
            dire_player_ids=(105, 106, 107, 108, 109),
            pure_lineup_score=old.pure_lineup_score,
            current_player_adjusted_lineup_score=(
                old.current_player_adjusted_lineup_score
            ),
            effective_lineup_score=old.effective_lineup_score,
            scoring_mode=old.scoring_mode,
            player_coverage_count=old.player_coverage_count,
            source_week=old.source_week,
            source_as_of=old.source_as_of,
            player_stats_as_of=old.player_stats_as_of,
            formula_version=old.formula_version,
            evidence=old.evidence,
            evidence_hash=old.evidence_hash,
            created_at=NOW,
        )

        client = FakeClient({1: fetched(1)})
        first = backfill_historical_rosh_scores(
            storage,
            client,  # type: ignore[arg-type]
            match_id=1,
            clock=lambda: NOW,
        )

        assert (first.inserted, first.skipped, first.failed) == (1, 0, 0)
        assert client.calls == [(1, True)]
        exact = query_historical_rosh_lineup_score(
            storage.connection,
            match_id=1,
            formula_version=ROSH_FORMULA_VERSION,
            radiant_hero_ids=(2, 1, 3, 4, 5),
            dire_hero_ids=(7, 6, 8, 9, 10),
            radiant_player_ids=(101, 100, 102, 103, 104),
            dire_player_ids=(106, 105, 107, 108, 109),
        )
        assert exact is not None
        assert exact.radiant_hero_ids == (2, 1, 3, 4, 5)
        assert exact.dire_player_ids == (106, 105, 107, 108, 109)

        client.calls.clear()
        second = backfill_historical_rosh_scores(
            storage,
            client,  # type: ignore[arg-type]
            match_id=1,
            clock=lambda: NOW,
        )
        assert (second.inserted, second.skipped, second.failed) == (0, 1, 0)
        assert client.calls == []
    finally:
        storage.close()
