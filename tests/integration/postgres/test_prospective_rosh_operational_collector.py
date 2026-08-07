from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from database.session import PostgresSession
from event_intelligence.prospective_rosh_candidate import (
    load_frozen_prospective_rosh_candidate,
)
from event_intelligence.prospective_rosh_collector import (
    ProspectiveRoshCollectorRepository,
    run_collector_once,
)
from event_intelligence.prospective_rosh_shadow import (
    ProspectiveRoshShadowRepository,
    build_shadow_prediction,
)
from event_intelligence.prospective_team_rating import (
    ProspectiveTeamRatingRepository,
    produce_match,
)
from event_intelligence.raw_archive import canonical_json_bytes
from event_intelligence.roles import PROSPECTIVE_ASSIGNMENT_VERSION
from live_betting.stratz_rosh_client import FetchedLegacyRoshBatch
from prematch.stratz_rosh import build_rosh_query_requests
from tests.integration.postgres.test_prospective_team_rating_producer import (
    TARGET_ORIGIN,
    _digest,
    _seed_formal_data,
    _store_seed,
)


FIXTURE = Path(__file__).parents[2] / "fixtures" / "stratz-rosh.json"


class FixtureTransport:
    def __init__(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.calls = 0

    def fetch_legacy_lineup_batch(
        self,
        radiant_heroes: Any,
        dire_heroes: Any,
        *,
        statistics_cutoff: datetime,
    ) -> FetchedLegacyRoshBatch:
        self.calls += 1
        requests = build_rosh_query_requests(
            (*radiant_heroes, *dire_heroes),
            int(statistics_cutoff.timestamp()),
        )
        return FetchedLegacyRoshBatch(
            request_bodies={
                operation: canonical_json_bytes(payload)
                for operation, payload in requests.items()
            },
            response_bodies={
                operation: canonical_json_bytes(payload)
                for operation, payload in self.fixture["responses"].items()
            },
            collected_at=statistics_cutoff + timedelta(seconds=1),
        )


class ForbiddenTransport:
    def fetch_legacy_lineup_batch(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("STRATZ transport must not be called")


def _seed_lineups(
    session: PostgresSession,
    match_ids: tuple[int, ...],
    *,
    available_at: datetime,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    heroes = [*fixture["radiant_heroes"], *fixture["dire_heroes"]]
    with session.transaction():
        for hero_id in heroes:
            session.execute(
                """INSERT INTO heroes (hero_id, localized_name)
                   VALUES (?, ?) ON CONFLICT (hero_id) DO NOTHING""",
                (hero_id, f"Hero {hero_id}"),
            )
        for match_id in match_ids:
            for index, hero_id in enumerate(heroes):
                is_radiant = index < 5
                player_slot = index if is_radiant else 128 + index - 5
                position = index % 5 + 1
                session.execute(
                    """INSERT INTO match_players
                       (match_id, account_id, player_slot, hero_id,
                        is_radiant, team_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        match_id,
                        100_000 + index,
                        player_slot,
                        hero_id,
                        is_radiant,
                        10 if is_radiant else 20,
                    ),
                )
                session.execute(
                    """INSERT INTO player_role_assignments
                       (match_id, player_slot, account_id, team_id, purpose,
                        position, assignment_source, confidence, input_cutoff,
                        input_hash, assignment_version, created_at)
                       VALUES (?, ?, ?, ?, 'expected_position', ?, 'fixture',
                               0.99, ?, ?, ?, ?)""",
                    (
                        match_id,
                        player_slot,
                        100_000 + index,
                        10 if is_radiant else 20,
                        position,
                        available_at.isoformat(),
                        _digest(match_id + index),
                        PROSPECTIVE_ASSIGNMENT_VERSION,
                        available_at.isoformat(),
                    ),
                )


def _produce_p0(
    session: PostgresSession,
    target_ids: tuple[int, ...],
) -> ProspectiveTeamRatingRepository:
    repository = ProspectiveTeamRatingRepository(session)
    _store_seed(session, repository)
    for index, match_id in enumerate(target_ids):
        cutoff = TARGET_ORIGIN + timedelta(minutes=index * 5)
        result = produce_match(
            repository,
            match_id,
            now=cutoff - timedelta(minutes=10),
        )
        assert result.status == "produced"
    return repository


def test_paired_collection_idempotency_settlement_and_causal_exclusion(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    _history, target_ids = _seed_formal_data(
        postgres_engine,
        history_count=5,
        target_count=2,
    )
    session = PostgresSession(postgres_engine)
    artifact_root = tmp_path / "artifacts"
    try:
        team_repository = _produce_p0(session, target_ids)
        _seed_lineups(
            session,
            target_ids,
            available_at=TARGET_ORIGIN - timedelta(minutes=9),
        )
        repository = ProspectiveRoshCollectorRepository(session)
        transport = FixtureTransport()
        reports = []
        for index, match_id in enumerate(target_ids):
            cutoff = TARGET_ORIGIN + timedelta(minutes=index * 5)
            reports.append(
                run_collector_once(
                    repository,
                    transport,
                    artifact_root=artifact_root,
                    now=cutoff - timedelta(minutes=8),
                    match_id=match_id,
                    acceptance_limit=5,
                )
            )
        assert [report.paired for report in reports] == [1, 1], reports
        assert transport.calls == 2
        assert session.execute(
            "SELECT COUNT(*) FROM prospective_rosh_shadow_predictions"
        ).scalar_one() == 2
        assert session.execute(
            "SELECT COUNT(*) FROM prospective_rosh_collection_attempts"
        ).scalar_one() == 4

        repeated = run_collector_once(
            repository,
            transport,
            artifact_root=artifact_root,
            now=TARGET_ORIGIN - timedelta(minutes=7),
            match_id=target_ids[0],
            acceptance_limit=5,
        )
        assert repeated.unchanged == 1
        assert transport.calls == 2

        candidate = load_frozen_prospective_rosh_candidate()
        authority = team_repository.load_rosh_team_rating_authority(target_ids[0])
        assert authority is not None
        conflicting = build_shadow_prediction(
            candidate,
            match_id=target_ids[0],
            series_id=200_000,
            team_rating=authority,
            rosh_evidence=None,
            missing_reason="conflicting_content",
            created_at=TARGET_ORIGIN - timedelta(minutes=6),
        )
        with pytest.raises(ValueError, match="immutable.*match prediction conflict"):
            ProspectiveRoshShadowRepository(session).store_prediction(conflicting)

        attempt_count = session.execute(
            "SELECT COUNT(*) FROM prospective_rosh_collection_attempts"
        ).scalar_one()
        with pytest.raises(RuntimeError, match="rollback proof"):
            with session.transaction():
                repository.record_attempt(
                    candidate,
                    match_id=target_ids[0],
                    prediction_cutoff=TARGET_ORIGIN,
                    attempted_at=TARGET_ORIGIN - timedelta(minutes=5),
                    status="idempotency_unchanged",
                    missing_reason=None,
                )
                raise RuntimeError("rollback proof")
        assert session.execute(
            "SELECT COUNT(*) FROM prospective_rosh_collection_attempts"
        ).scalar_one() == attempt_count

        first_prediction_created = TARGET_ORIGIN - timedelta(minutes=8) + timedelta(
            seconds=1
        )
        result_usable = TARGET_ORIGIN + timedelta(hours=1)
        with session.transaction():
            for index, match_id in enumerate(target_ids):
                actual_start = (
                    TARGET_ORIGIN + timedelta(minutes=index * 5)
                    if index == 0
                    else first_prediction_created + timedelta(minutes=5)
                )
                session.execute(
                    """UPDATE matches
                          SET radiant_win=?, duration=2400, start_time=?
                        WHERE match_id=?""",
                    (bool(index % 2), int(actual_start.timestamp()), match_id),
                )
                session.execute(
                    """UPDATE match_ingest_status
                          SET has_valid_result=1, basic_result_state='ready',
                              first_usable_at=?, latest_raw_content_hash=?
                        WHERE match_id=?""",
                    (
                        (result_usable + timedelta(minutes=index * 5)).isoformat(),
                        _digest(900 + index),
                        match_id,
                    ),
                )
        settlements, audits = repository.settle_and_audit_ready(
            candidate.artifact_hash,
            observed_at=result_usable + timedelta(hours=1),
            limit=5,
        )
        assert (settlements, audits) == (2, 2)
        acceptance = repository.acceptance_rows(
            candidate,
            artifact_root=artifact_root,
            limit=5,
        )
        assert acceptance[0]["actual_start_causal_audit"] == "passed"
        assert acceptance[1]["actual_start_causal_audit"] == (
            "prediction_not_before_actual_start"
        )
        assert all(row["settlement"] is True for row in acceptance)
        assert all(row["exact_replay"] is True for row in acceptance)
        loaded = ProspectiveRoshShadowRepository(session).load_settled_rows(
            candidate.artifact_hash
        )
        assert [row.causal_eligible for row in loaded] == [True, False]

        attempt_hash = session.execute(
            "SELECT attempt_hash FROM prospective_rosh_collection_attempts LIMIT 1"
        ).scalar_one()
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                """UPDATE prospective_rosh_collection_attempts
                      SET missing_reason='tampered' WHERE attempt_hash=?""",
                (attempt_hash,),
            )
        audit_hash = session.execute(
            "SELECT audit_hash FROM prospective_rosh_causal_audits LIMIT 1"
        ).scalar_one()
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                "DELETE FROM prospective_rosh_causal_audits WHERE audit_hash=?",
                (audit_hash,),
            )
    finally:
        session.close()


def test_missing_p0_and_incomplete_lineup_never_create_p1(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    _history, target_ids = _seed_formal_data(
        postgres_engine,
        history_count=2,
        target_count=2,
    )
    session = PostgresSession(postgres_engine)
    try:
        team_repository = ProspectiveTeamRatingRepository(session)
        _store_seed(session, team_repository)
        assert produce_match(
            team_repository,
            target_ids[1],
            now=TARGET_ORIGIN - timedelta(minutes=5),
        ).status == "produced"
        repository = ProspectiveRoshCollectorRepository(session)

        no_p0 = run_collector_once(
            repository,
            ForbiddenTransport(),
            artifact_root=tmp_path / "artifacts",
            now=TARGET_ORIGIN - timedelta(minutes=10),
            match_id=target_ids[0],
            acceptance_limit=5,
        )
        assert no_p0.retry_scheduled == 1
        assert no_p0.results[0].missing_reason == "prospective_team_rating_unavailable"

        second_cutoff = TARGET_ORIGIN + timedelta(minutes=5)
        early = run_collector_once(
            repository,
            ForbiddenTransport(),
            artifact_root=tmp_path / "artifacts",
            now=second_cutoff - timedelta(minutes=10),
            match_id=target_ids[1],
            acceptance_limit=5,
        )
        assert early.results[0].missing_reason == "ten_heroes_incomplete"
        finalized = run_collector_once(
            repository,
            ForbiddenTransport(),
            artifact_root=tmp_path / "artifacts",
            now=second_cutoff - timedelta(minutes=1),
            match_id=target_ids[1],
            acceptance_limit=5,
        )
        assert finalized.p0_only == 1
        row = session.execute(
            """SELECT record_status, p1_probability, missing_reason
                 FROM prospective_rosh_shadow_predictions"""
        ).fetchone()
        assert tuple(row) == ("p0_only", None, "ten_heroes_incomplete")
    finally:
        session.close()


def test_acceptance_cap_stops_new_collection_at_five(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    _history, target_ids = _seed_formal_data(
        postgres_engine,
        history_count=2,
        target_count=6,
    )
    session = PostgresSession(postgres_engine)
    try:
        _produce_p0(session, target_ids)
        _seed_lineups(
            session,
            target_ids,
            available_at=TARGET_ORIGIN - timedelta(minutes=11),
        )
        repository = ProspectiveRoshCollectorRepository(session)
        report = run_collector_once(
            repository,
            FixtureTransport(),
            artifact_root=tmp_path / "artifacts",
            now=TARGET_ORIGIN - timedelta(minutes=10),
            limit=10,
            acceptance_limit=5,
        )
        assert report.scanned == 5
        assert report.acceptance_collected == 5
        assert report.acceptance_stopped is False
        assert session.execute(
            "SELECT COUNT(*) FROM prospective_rosh_shadow_predictions"
        ).scalar_one() == 5
        assert session.execute(
            """SELECT COUNT(*) FROM prospective_rosh_shadow_predictions
                WHERE match_id=?""",
            (target_ids[-1],),
        ).scalar_one() == 0
    finally:
        session.close()
