from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from database.session import PostgresSession
from event_intelligence.prospective_rosh_candidate import (
    load_frozen_prospective_rosh_candidate,
)
from event_intelligence.prospective_rosh_shadow import (
    ProspectiveRoshShadowRepository,
    build_shadow_prediction,
)
from event_intelligence.prospective_team_rating import (
    ProspectiveTeamRatingRepository,
    build_prospective_team_rating_run,
    build_prospective_team_rating_seed,
    produce_match,
)
from event_intelligence.team_rating import TEAM_RATING_VERSION, TeamRatingConfig


UTC = timezone.utc
HISTORY_ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)
SEED_CUTOFF = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
FROZEN_AT = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
TARGET_ORIGIN = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
CONFIG = TeamRatingConfig(
    initial_rating=1_500.0,
    scale=400.0,
    k_factor=16.0,
    inactivity_half_life_days=180.0,
    roster_carry_power=1.0,
    radiant_side_logit=0.0,
    config_version=TEAM_RATING_VERSION,
)


def _digest(value: int) -> str:
    return f"{value:064x}"


def _seed_formal_data(
    engine: Engine,
    *,
    history_count: int,
    target_count: int,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    history_ids = tuple(8_000_000_000 + index for index in range(history_count))
    target_ids = tuple(9_400_000_000 + index for index in range(target_count))
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
                    'prospective-team-event', 'Prospective Team Event',
                    'tier_1', 1000000, '2026-01-01T00:00:00Z',
                    '2026-08-20T00:00:00Z', 99400, '[]',
                    'manually_audited', 'scope-v1', 'formal_main_event',
                    'approved', 'tester', '2025-12-01T00:00:00Z',
                    'not_required', '[]', '[]',
                    '2025-12-01T00:00:00Z', '2025-12-01T00:00:00Z'
                )
                """
            )
        )
        for index, match_id in enumerate(history_ids):
            started_at = HISTORY_ORIGIN + timedelta(hours=index * 3)
            usable_at = started_at + timedelta(hours=1)
            connection.execute(
                text(
                    """
                    INSERT INTO matches (
                        match_id, radiant_team_id, dire_team_id, radiant_win,
                        duration, start_time, series_id, patch
                    ) VALUES (
                        :match_id, :radiant_team_id, :dire_team_id,
                        :radiant_win, 2400, :start_time, :series_id, 60
                    )
                    """
                ),
                {
                    "match_id": match_id,
                    "radiant_team_id": 10 + index % 4,
                    "dire_team_id": 20 + index % 5,
                    "radiant_win": bool(index % 2),
                    "start_time": int(started_at.timestamp()),
                    "series_id": 100_000 + index // 2,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO match_ingest_status (
                        match_id, event_id, start_time, series_id, map_number,
                        stage_scope, stage_in_scope, has_valid_result,
                        ingest_state, basic_result_state,
                        latest_raw_content_hash, first_usable_at,
                        discovered_at, updated_at
                    ) VALUES (
                        :match_id, 'prospective-team-event', :start_time,
                        :series_id, :map_number, 'main_event', 1, 1,
                        'complete', 'ready', :content_hash, :usable_at,
                        :usable_at, :usable_at
                    )
                    """
                ),
                {
                    "match_id": match_id,
                    "start_time": int(started_at.timestamp()),
                    "series_id": 100_000 + index // 2,
                    "map_number": index + 1,
                    "content_hash": _digest(index + 1),
                    "usable_at": usable_at.isoformat(),
                },
            )
        for index, match_id in enumerate(target_ids):
            started_at = TARGET_ORIGIN + timedelta(minutes=index * 5)
            connection.execute(
                text(
                    """
                    INSERT INTO matches (
                        match_id, radiant_team_id, dire_team_id, radiant_win,
                        duration, start_time, series_id, patch
                    ) VALUES (
                        :match_id, :radiant_team_id, :dire_team_id, NULL,
                        NULL, :start_time, :series_id, 60
                    )
                    """
                ),
                {
                    "match_id": match_id,
                    "radiant_team_id": 10 + index % 4,
                    "dire_team_id": 20 + index % 5,
                    "start_time": int(started_at.timestamp()),
                    "series_id": 200_000 + index // 3,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO match_ingest_status (
                        match_id, event_id, start_time, series_id, map_number,
                        stage_scope, stage_in_scope, discovered_at, updated_at
                    ) VALUES (
                        :match_id, 'prospective-team-event', :start_time,
                        :series_id, :map_number, 'main_event', 1,
                        '2026-08-07T00:00:00Z', '2026-08-07T00:00:00Z'
                    )
                    """
                ),
                {
                    "match_id": match_id,
                    "start_time": int(started_at.timestamp()),
                    "series_id": 200_000 + index // 3,
                    "map_number": index + 1,
                },
            )
    return history_ids, target_ids


def _store_seed(
    session: PostgresSession,
    repository: ProspectiveTeamRatingRepository,
) -> None:
    source_results = repository.load_seed_results(
        training_cutoff=SEED_CUTOFF,
        observed_at=FROZEN_AT,
    )
    seed = build_prospective_team_rating_seed(
        config=CONFIG,
        source_results=source_results,
        seed_as_of=SEED_CUTOFF,
        seed_training_cutoff=SEED_CUTOFF,
        frozen_at=FROZEN_AT,
    )
    assert repository.store_seed(seed)
    assert not repository.store_seed(seed)


@pytest.mark.parametrize("target_count", (20, 100))
def test_isolated_postgres_produces_idempotent_future_cohorts(
    postgres_engine: Engine,
    target_count: int,
) -> None:
    _history, target_ids = _seed_formal_data(
        postgres_engine,
        history_count=20,
        target_count=target_count,
    )
    session = PostgresSession(postgres_engine)
    repository = ProspectiveTeamRatingRepository(session)
    try:
        _store_seed(session, repository)
        results = []
        for index, match_id in enumerate(target_ids):
            cutoff = TARGET_ORIGIN + timedelta(minutes=index * 5)
            results.append(
                produce_match(
                    repository,
                    match_id,
                    now=cutoff - timedelta(minutes=2),
                )
            )
        assert all(result.status == "produced" for result in results)
        assert session.execute(
            "SELECT COUNT(*) FROM prospective_team_rating_authorities"
        ).scalar_one() == target_count
        assert session.execute(
            """SELECT COUNT(*) FROM team_rating_runs
                WHERE availability_mode='prospective' AND status='trained'"""
        ).scalar_one() == target_count
        assert session.execute(
            """SELECT COUNT(*) FROM team_rating_predictions
                WHERE status='predicted' AND eventual_radiant_win IS NULL"""
        ).scalar_one() == target_count

        repeated = produce_match(
            repository,
            target_ids[0],
            now=TARGET_ORIGIN + timedelta(days=1),
        )
        assert repeated.status == "unchanged"
    finally:
        session.close()


def test_transaction_rollback_append_only_hash_guards_and_cutoff_exclusions(
    postgres_engine: Engine,
) -> None:
    history_ids, target_ids = _seed_formal_data(
        postgres_engine,
        history_count=20,
        target_count=4,
    )
    session = PostgresSession(postgres_engine)
    repository = ProspectiveTeamRatingRepository(session)
    try:
        _store_seed(session, repository)
        with pytest.raises(DBAPIError, match="seed content hash disagrees"):
            session.execute(
                """INSERT INTO prospective_team_rating_seeds
                   SELECT ?, rating_version, configuration_json,
                          configuration_hash, seed_as_of,
                          seed_training_cutoff, source_manifest_json,
                          source_manifest_hash, state_json, state_hash,
                          artifact_json, ?, frozen_at, created_at
                     FROM prospective_team_rating_seeds LIMIT 1""",
                (_digest(700), _digest(700)),
            )
        with pytest.raises(RuntimeError, match="rollback proof"):
            with session.transaction():
                result = produce_match(
                    repository,
                    target_ids[0],
                    now=TARGET_ORIGIN - timedelta(minutes=2),
                )
                assert result.status == "produced"
                raise RuntimeError("rollback proof")
        assert repository.existing_authority(target_ids[0]) is None

        produced = produce_match(
            repository,
            target_ids[0],
            now=TARGET_ORIGIN - timedelta(minutes=2),
        )
        assert produced.status == "produced"
        authority = repository.existing_authority(target_ids[0])
        assert authority is not None
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                """UPDATE prospective_team_rating_authorities
                    SET artifact_hash=? WHERE authority_hash=?""",
                (_digest(999), authority["authority_hash"]),
            )
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                """DELETE FROM prospective_team_rating_authorities
                    WHERE authority_hash=?""",
                (authority["authority_hash"],),
            )

        seed = repository.load_seed(TARGET_ORIGIN)
        assert seed is not None
        target, _has_result = repository.load_target(target_ids[0])
        conflicting_run = build_prospective_team_rating_run(
            seed=seed,
            base_authority_hash=None,
            base_as_of=seed.seed_as_of,
            base_states=seed.states,
            applied_results=(),
            target=target,
            created_at=TARGET_ORIGIN - timedelta(minutes=3),
        )
        with pytest.raises(ValueError, match="immutable.*authority conflict"):
            repository.persist_run(conflicting_run)

        cutoff = TARGET_ORIGIN + timedelta(minutes=5)
        late = produce_match(
            repository,
            target_ids[1],
            now=cutoff + timedelta(seconds=1),
        )
        assert late.reason == "prediction_cutoff_passed"
        assert repository.existing_authority(target_ids[1]) is None

        target_result_at = TARGET_ORIGIN + timedelta(minutes=11)
        with session.transaction():
            session.execute(
                """UPDATE matches SET radiant_win=FALSE, duration=30
                    WHERE match_id=?""",
                (target_ids[2],),
            )
            session.execute(
                """UPDATE match_ingest_status
                    SET has_valid_result=1, basic_result_state='ready',
                        first_usable_at=?, latest_raw_content_hash=?
                    WHERE match_id=?""",
                (target_result_at.isoformat(), _digest(701), target_ids[2]),
            )
        excluded_target = produce_match(
            repository,
            target_ids[2],
            now=TARGET_ORIGIN + timedelta(minutes=9),
        )
        assert excluded_target.reason == "target_result_already_available"
        assert repository.existing_authority(target_ids[2]) is None

        with session.transaction():
            session.execute(
                """UPDATE match_ingest_status
                    SET first_usable_at='2026-08-09T00:00:00+00:00'
                    WHERE match_id=?""",
                (history_ids[-1],),
            )
        seed = repository.load_seed(TARGET_ORIGIN + timedelta(minutes=15))
        assert seed is not None
        base = repository.load_base_state(
            seed, TARGET_ORIGIN + timedelta(minutes=15)
        )
        applied = repository.load_results(
            after=base.as_of,
            cutoff=TARGET_ORIGIN + timedelta(minutes=15),
            observed_at=TARGET_ORIGIN + timedelta(minutes=13),
            target_match_id=target_ids[3],
        )
        assert history_ids[-1] not in {row.row.match_id for row in applied}
    finally:
        session.close()


def test_settlement_is_independent_and_rosh_shadow_consumes_p0(
    postgres_engine: Engine,
) -> None:
    _history, target_ids = _seed_formal_data(
        postgres_engine,
        history_count=20,
        target_count=1,
    )
    session = PostgresSession(postgres_engine)
    repository = ProspectiveTeamRatingRepository(session)
    rosh_repository = ProspectiveRoshShadowRepository(session)
    match_id = target_ids[0]
    try:
        _store_seed(session, repository)
        produced = produce_match(
            repository,
            match_id,
            now=TARGET_ORIGIN - timedelta(minutes=2),
        )
        assert produced.status == "produced"
        team_authority = repository.load_rosh_team_rating_authority(match_id)
        assert team_authority is not None

        candidate = load_frozen_prospective_rosh_candidate()
        assert rosh_repository.store_candidate(
            candidate,
            created_at=FROZEN_AT,
        )
        shadow = build_shadow_prediction(
            candidate,
            match_id=match_id,
            series_id=200_000,
            team_rating=team_authority,
            rosh_evidence=None,
            missing_reason="rosh_not_available_before_cutoff",
            created_at=TARGET_ORIGIN - timedelta(minutes=1),
        )
        with session.transaction():
            assert rosh_repository.store_prediction(shadow)

        usable_at = TARGET_ORIGIN + timedelta(minutes=41)
        with session.transaction():
            session.execute(
                """UPDATE matches SET radiant_win=TRUE, duration=2400
                    WHERE match_id=?""",
                (match_id,),
            )
            session.execute(
                """UPDATE match_ingest_status
                    SET has_valid_result=1, basic_result_state='ready',
                        first_usable_at=?, latest_raw_content_hash=?
                    WHERE match_id=?""",
                (usable_at.isoformat(), _digest(500), match_id),
            )
        assert repository.settle(
            match_id,
            settled_at=usable_at + timedelta(minutes=1),
        )
        prediction = session.execute(
            """SELECT status, eventual_radiant_win
                FROM team_rating_predictions WHERE prediction_id=?""",
            (team_authority.prediction_id,),
        ).fetchone()
        assert tuple(prediction) == ("predicted", None)
    finally:
        session.close()


def test_rosh_dependency_records_missing_p0_without_p1(
    postgres_engine: Engine,
) -> None:
    _history, target_ids = _seed_formal_data(
        postgres_engine,
        history_count=1,
        target_count=1,
    )
    session = PostgresSession(postgres_engine)
    repository = ProspectiveTeamRatingRepository(session)
    try:
        assert repository.resolve_rosh_team_rating_authority(
            target_ids[0],
            observed_at=TARGET_ORIGIN - timedelta(minutes=3),
        ) is None
        assert session.execute(
            """SELECT missing_reason
                FROM prospective_rosh_team_rating_failures"""
        ).scalar_one() == "prospective_team_rating_unavailable"
        assert session.execute(
            """SELECT COUNT(*)
                FROM prospective_rosh_shadow_predictions"""
        ).scalar_one() == 0
    finally:
        session.close()


def test_full_rebuild_uses_seed_base_and_cumulative_postseed_authority(
    postgres_engine: Engine,
) -> None:
    _history, target_ids = _seed_formal_data(
        postgres_engine,
        history_count=1,
        target_count=2,
    )
    result_id = 8_500_000_000
    result_started = HISTORY_ORIGIN - timedelta(hours=2)
    result_usable = TARGET_ORIGIN - timedelta(minutes=1)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO matches (
                       match_id, radiant_team_id, dire_team_id, radiant_win,
                       duration, start_time, series_id, patch
                   ) VALUES (:match_id, 10, 20, TRUE, 2400,
                             :start_time, 300000, 60)"""
            ),
            {"match_id": result_id, "start_time": int(result_started.timestamp())},
        )
        connection.execute(
            text(
                """INSERT INTO match_ingest_status (
                       match_id, event_id, start_time, series_id, map_number,
                       stage_scope, stage_in_scope, has_valid_result,
                       ingest_state, basic_result_state,
                       latest_raw_content_hash, first_usable_at,
                       discovered_at, updated_at
                   ) VALUES (:match_id, 'prospective-team-event', :start_time,
                             300000, 1, 'main_event', 1, 1, 'complete',
                             'ready', :content_hash, :usable_at,
                             :usable_at, :usable_at)"""
            ),
            {
                "match_id": result_id,
                "start_time": int(result_started.timestamp()),
                "content_hash": _digest(800),
                "usable_at": result_usable.isoformat(),
            },
        )
    session = PostgresSession(postgres_engine)
    repository = ProspectiveTeamRatingRepository(session)
    try:
        _store_seed(session, repository)
        first_observed = TARGET_ORIGIN - timedelta(minutes=2)
        assert produce_match(
            repository,
            target_ids[0],
            now=first_observed,
        ).status == "produced"
        first = session.execute(
            """SELECT available_at, applied_result_manifest_json
                FROM prospective_team_rating_authorities WHERE match_id=?""",
            (target_ids[0],),
        ).fetchone()
        assert first["available_at"] == first_observed.isoformat()
        assert json.loads(first["applied_result_manifest_json"]) == []

        assert produce_match(
            repository,
            target_ids[1],
            now=TARGET_ORIGIN + timedelta(minutes=3),
        ).status == "produced"
        second = session.execute(
            """SELECT base_as_of, applied_result_manifest_json, artifact_json
                FROM prospective_team_rating_authorities WHERE match_id=?""",
            (target_ids[1],),
        ).fetchone()
        assert second["base_as_of"] == SEED_CUTOFF.isoformat()
        manifest = json.loads(second["applied_result_manifest_json"])
        assert [row["result"]["match_id"] for row in manifest] == [result_id]
        replay_order = json.loads(second["artifact_json"])["rating_replay_order"]
        assert replay_order[0]["result"]["match_id"] == result_id
    finally:
        session.close()
