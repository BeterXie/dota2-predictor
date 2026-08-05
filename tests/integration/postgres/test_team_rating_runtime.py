from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from database.session import PostgresSession
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.team_rating import RatingMapInput
from event_intelligence.team_rating_backtest import (
    LoadedTeamRatingMap,
    TeamRatingCorpus,
    TeamRatingParameters,
    TeamRatingSourceAuthority,
    build_team_rating_walk_forward_runs,
)
from event_intelligence.team_rating_storage import (
    TeamRatingPersistenceCounts,
    persist_team_rating_runs,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
CREATED_AT = datetime(2026, 8, 5, tzinfo=UTC)
PARAMETERS = TeamRatingParameters(400.0, 16.0, 180.0, 1.0)


def _map(match_id: int) -> RatingMapInput:
    started_at = START + timedelta(hours=2 * match_id)
    completed_at = started_at + timedelta(minutes=40)
    return RatingMapInput(
        match_id=match_id,
        series_id=100 + match_id // 2,
        event_id="team-rating-event",
        started_at=started_at,
        completed_at=completed_at,
        result_usable_at=completed_at + timedelta(minutes=1),
        radiant_team_id=10,
        dire_team_id=20,
        radiant_roster=(1, 2, 3, 4, 5),
        dire_roster=(6, 7, 8, 9, 10),
        radiant_win=bool(match_id % 2),
    )


def _runs(count: int):
    rows = tuple(_map(match_id) for match_id in range(1, count + 1))
    corpus = TeamRatingCorpus(
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        formal_maps=len(rows),
        maps=tuple(
            LoadedTeamRatingMap(
                row,
                row.started_at,
                "reconstructed_map_start",
                _source_authority(row),
            )
            for row in rows
        ),
    )
    return build_team_rating_walk_forward_runs(
        corpus,
        candidates=(PARAMETERS,),
    )


def _source_authority(row: RatingMapInput) -> TeamRatingSourceAuthority:
    content_hash = hashlib.sha256(f"source:{row.match_id}".encode()).hexdigest()
    return TeamRatingSourceAuthority(
        match_id=row.match_id,
        artifact_id=f"opendota:{content_hash}",
        content_hash=content_hash,
        artifact_usable_at=row.result_usable_at,
        observation_usable_at=row.result_usable_at,
    )


def _seed_match_authority(engine: Engine, count: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO event_registry (
                    event_id, canonical_name, tier, prize_pool_usd,
                    main_event_start_at, main_event_end_at,
                    opendota_league_id, official_evidence_urls_json,
                    evidence_status, scope_policy_version, scope,
                    approval_status, approved_by, approved_at,
                    reconciliation_status, included_stages_json,
                    excluded_categories_json, created_at, updated_at
                ) VALUES (
                    'team-rating-event', 'Team Rating Event', 'tier_1',
                    1000000, '2026-01-01T00:00:00Z',
                    '2026-01-10T00:00:00Z', 99001, '[]',
                    'manually_audited', 'scope-v1', 'formal_main_event',
                    'approved', 'tester', '2025-12-01T00:00:00Z',
                    'not_required', '[]', '[]',
                    '2025-12-01T00:00:00Z', '2025-12-01T00:00:00Z'
                )
                """
            )
        )
        for match_id in range(1, count + 1):
            row = _map(match_id)
            connection.execute(
                text(
                    """
                    INSERT INTO matches (
                        match_id, radiant_team_id, dire_team_id, radiant_win,
                        duration, start_time, series_id
                    ) VALUES (
                        :match_id, :radiant_team_id, :dire_team_id,
                        :radiant_win, :duration, :start_time, :series_id
                    )
                    """
                ),
                {
                    "match_id": match_id,
                    "radiant_team_id": row.radiant_team_id,
                    "dire_team_id": row.dire_team_id,
                    "radiant_win": row.radiant_win,
                    "duration": int(
                        (row.completed_at - row.started_at).total_seconds()
                    ),
                    "start_time": int(row.started_at.timestamp()),
                    "series_id": row.series_id,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO match_ingest_status (
                        match_id, event_id, start_time, series_id, map_number,
                        discovered_at, updated_at
                    ) VALUES (
                        :match_id, 'team-rating-event', :start_time,
                        :series_id, :map_number,
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                    )
                    """
                ),
                {
                    "match_id": match_id,
                    "start_time": int(row.started_at.timestamp()),
                    "series_id": 100 + match_id // 2,
                    "map_number": match_id,
                },
            )


def test_team_rating_runtime_is_idempotent_and_checkpoints_are_explicit(
    postgres_engine: Engine,
) -> None:
    _seed_match_authority(postgres_engine, 3)
    session = PostgresSession(postgres_engine)
    runs = _runs(2)
    try:
        first = persist_team_rating_runs(
            session,
            runs,
            created_at=CREATED_AT,
        )
        repeated = persist_team_rating_runs(
            session,
            tuple(reversed(runs)),
            created_at=CREATED_AT + timedelta(days=1),
        )
        assert (first.inserted_runs, first.inserted_predictions) == (2, 2)
        assert (repeated.unchanged_runs, repeated.unchanged_predictions) == (2, 2)
        assert session.execute(
            "SELECT COUNT(*) FROM team_rating_state_snapshots"
        ).scalar_one() == 0

        checkpoint = persist_team_rating_runs(
            session,
            runs,
            checkpoint_run_ids=(runs[-1].run_id,),
            created_at=CREATED_AT + timedelta(hours=1),
        )
        checkpoint_repeat = persist_team_rating_runs(
            session,
            runs,
            checkpoint_run_ids=(runs[-1].run_id,),
            created_at=CREATED_AT + timedelta(hours=2),
        )
        assert checkpoint.inserted_snapshots == 2
        assert checkpoint_repeat.unchanged_snapshots == 2
        stored = session.execute(
            """SELECT run_id, as_of, team_id, roster_json
                 FROM team_rating_state_snapshots ORDER BY team_id"""
        ).fetchall()
        assert {str(row["run_id"]) for row in stored} == {runs[-1].run_id}
        assert {int(row["team_id"]) for row in stored} == {10, 20}
        assert all(str(row["as_of"]) == runs[-1].artifact.training_cutoff.isoformat() for row in stored)
        assert all(str(row["roster_json"]).startswith("[") for row in stored)

        prediction = session.execute(
            """SELECT radiant_roster_continuity, dire_roster_continuity,
                      status, eventual_radiant_win
                 FROM team_rating_predictions
                WHERE run_id=?""",
            (runs[-1].run_id,),
        ).fetchone()
        assert prediction is not None
        assert tuple(prediction) == (1.0, 1.0, "settled", 0)
    finally:
        session.close()


def test_team_rating_runtime_conflict_rolls_back_prior_batch_inserts(
    postgres_engine: Engine,
) -> None:
    _seed_match_authority(postgres_engine, 3)
    session = PostgresSession(postgres_engine)
    initial = _runs(2)
    extended = _runs(3)
    try:
        persist_team_rating_runs(session, initial, created_at=CREATED_AT)
        conflicting = replace(extended[1], status="failed")

        with pytest.raises(ValueError, match="immutable Team Rating run conflict"):
            persist_team_rating_runs(
                session,
                (extended[2], conflicting),
                created_at=CREATED_AT + timedelta(hours=1),
            )

        assert session.execute(
            "SELECT COUNT(*) FROM team_rating_runs"
        ).scalar_one() == 2
        assert session.execute(
            "SELECT COUNT(*) FROM team_rating_predictions WHERE match_id=3"
        ).scalar_one() == 0

        prediction_conflict = replace(
            initial[1],
            eventual_radiant_win=not initial[1].eventual_radiant_win,
        )
        with pytest.raises(
            ValueError,
            match="immutable Team Rating prediction conflict",
        ):
            persist_team_rating_runs(
                session,
                (prediction_conflict,),
                created_at=CREATED_AT,
            )
        assert session.execute(
            "SELECT COUNT(*) FROM team_rating_predictions"
        ).scalar_one() == 2
    finally:
        session.close()


def test_team_rating_runtime_dry_run_does_not_write(
    postgres_engine: Engine,
) -> None:
    _seed_match_authority(postgres_engine, 1)
    session = PostgresSession(postgres_engine)
    run = _runs(1)[0]
    try:
        counts = persist_team_rating_runs(
            session,
            (run,),
            dry_run=True,
            checkpoint_run_ids=(run.run_id,),
            created_at=CREATED_AT,
        )
        assert (counts.inserted_runs, counts.inserted_predictions) == (1, 1)
        assert counts.inserted_snapshots == 0
        assert session.execute(
            "SELECT COUNT(*) FROM team_rating_runs"
        ).scalar_one() == 0
    finally:
        session.close()


def test_team_rating_runtime_concurrent_exact_duplicate_is_idempotent(
    postgres_engine: Engine,
) -> None:
    _seed_match_authority(postgres_engine, 2)
    run = _runs(2)[-1]
    barrier = Barrier(2)

    def persist() -> TeamRatingPersistenceCounts:
        session = PostgresSession(postgres_engine)
        try:
            barrier.wait()
            return persist_team_rating_runs(
                session,
                (run,),
                created_at=CREATED_AT,
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(persist), executor.submit(persist))
        results = tuple(future.result() for future in futures)

    assert sum(result.inserted_runs for result in results) == 1
    assert sum(result.unchanged_runs for result in results) == 1
    assert sum(result.inserted_predictions for result in results) == 1
    assert sum(result.unchanged_predictions for result in results) == 1
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM team_rating_runs")
            ).scalar_one()
            == 1
        )
        assert connection.execute(
            text("SELECT COUNT(*) FROM team_rating_predictions")
        ).scalar_one() == 1
