from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from database.session import PostgresSession
from event_intelligence.prospective_rosh_candidate import (
    load_frozen_prospective_rosh_candidate,
)
from event_intelligence.prospective_rosh_shadow import (
    ProspectiveRoshShadowRepository,
    TeamRatingAuthority,
    archive_exact_artifacts,
    build_prospective_rosh_evidence,
    build_shadow_prediction,
    build_shadow_settlement,
)
from event_intelligence.raw_archive import canonical_json_bytes
from live_betting.rosh_parity import ExactByteArtifactStore
from prematch.stratz_rosh import build_rosh_query_requests


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "stratz-rosh.json"
MATCH_IDS = (9300000001, 9300000002)
SERIES_IDS = (1400001, 1400002)
MATCH_START = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
PREDICTION_CUTOFF = datetime(2026, 8, 7, 0, 50, tzinfo=UTC)


def _digest(value: int) -> str:
    return f"{value:064x}"


def _seed_authority(
    session: PostgresSession,
) -> tuple[TeamRatingAuthority, TeamRatingAuthority]:
    session.execute(
        """INSERT INTO event_registry (
               event_id, canonical_name, tier, prize_pool_usd,
               main_event_start_at, main_event_end_at, opendota_league_id,
               official_evidence_urls_json, evidence_status,
               scope_policy_version, scope, approval_status, approved_by,
               approved_at, reconciliation_status, included_stages_json,
               excluded_categories_json, created_at, updated_at
           ) VALUES (
               'prospective-rosh-event', 'Prospective Rosh Event', 'tier_1',
               1000000, '2026-08-07T00:00:00Z', '2026-08-10T00:00:00Z',
               99100, '[]', 'manually_audited', 'scope-v1',
               'formal_main_event', 'approved', 'tester',
               '2026-08-06T00:00:00Z', 'not_required', '[]', '[]',
               '2026-08-06T00:00:00Z', '2026-08-06T00:00:00Z'
           )"""
    )
    for index, (match_id, series_id) in enumerate(
        zip(MATCH_IDS, SERIES_IDS, strict=True),
        1,
    ):
        session.execute(
            """INSERT INTO matches (
                   match_id, radiant_team_id, dire_team_id, radiant_win,
                   duration, start_time, series_id, patch
               ) VALUES (?, 10, 20, NULL, NULL, ?, ?, 60)""",
            (match_id, int(MATCH_START.timestamp()), series_id),
        )
        session.execute(
            """INSERT INTO match_ingest_status (
                   match_id, event_id, start_time, series_id, map_number,
                   discovered_at, updated_at
               ) VALUES (?, 'prospective-rosh-event', ?, ?, ?,
                         '2026-08-06T23:00:00Z', '2026-08-06T23:00:00Z')""",
            (match_id, int(MATCH_START.timestamp()), series_id, index),
        )
    artifact_hash = _digest(2)
    session.execute(
        """INSERT INTO team_rating_runs (
               run_id, rating_version, artifact_version, availability_mode,
               training_cutoff, configuration_json, training_input_hash,
               metrics_json, status, created_at
           ) VALUES (?, 'team-rating-elo-v1', 'team-rating-artifact-v1',
                     'prospective', '2026-08-06T12:00:00Z', ?, ?, NULL,
                     'trained', '2026-08-06T12:01:00Z')""",
        (
            _digest(1),
            json.dumps(
                {"artifact_hash": artifact_hash, "config": {"scale": 400.0}},
                sort_keys=True,
                separators=(",", ":"),
            ),
            _digest(3),
        ),
    )
    authorities = []
    for index, match_id in enumerate(MATCH_IDS, 1):
        row = session.execute(
            """INSERT INTO team_rating_predictions (
                   run_id, match_id, prediction_cutoff, cutoff_source,
                   radiant_team_id, dire_team_id, radiant_rating, dire_rating,
                   rating_diff, raw_probability, radiant_roster_continuity,
                   dire_roster_continuity, support, input_hash,
                   eventual_radiant_win, status, created_at
               ) VALUES (?, ?, ?, 'prospective_formal_map', 10, 20,
                         1510.0, 1490.0, 20.0, 0.55, 1.0, 1.0, 100, ?,
                         NULL, 'predicted', '2026-08-07T00:49:00Z')
               RETURNING prediction_id""",
            (
                _digest(1),
                match_id,
                PREDICTION_CUTOFF.isoformat(),
                _digest(10 + index),
            ),
        ).fetchone()
        authorities.append(
            TeamRatingAuthority(
                prediction_id=int(row["prediction_id"]),
                run_id=_digest(1),
                prediction_cutoff=PREDICTION_CUTOFF,
                probability=0.55,
                rating_version="team-rating-elo-v1",
                artifact_version="team-rating-artifact-v1",
                artifact_hash=artifact_hash,
                input_hash=_digest(10 + index),
                training_input_hash=_digest(3),
            )
        )
    return tuple(authorities)  # type: ignore[return-value]


def _evidence(tmp_path: Path):
    candidate = load_frozen_prospective_rosh_candidate()
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    hero_ids = [*fixture["radiant_heroes"], *fixture["dire_heroes"]]
    statistics_cutoff = PREDICTION_CUTOFF - timedelta(minutes=10)
    requests = build_rosh_query_requests(
        hero_ids,
        int(statistics_cutoff.timestamp()),
    )
    store = ExactByteArtifactStore(tmp_path / "artifacts")
    request_artifacts = archive_exact_artifacts(
        store,
        {
            operation: canonical_json_bytes(payload)
            for operation, payload in requests.items()
        },
    )
    response_artifacts = archive_exact_artifacts(
        store,
        {
            operation: canonical_json_bytes(payload)
            for operation, payload in fixture["responses"].items()
        },
    )
    return build_prospective_rosh_evidence(
        candidate,
        artifact_root=store.root,
        radiant_heroes=fixture["radiant_heroes"],
        dire_heroes=fixture["dire_heroes"],
        request_artifacts=request_artifacts,
        response_artifacts=response_artifacts,
        statistics_cutoff=statistics_cutoff,
        available_at=PREDICTION_CUTOFF - timedelta(minutes=5),
    )


def test_prospective_rosh_schema_is_independent_append_only_ledger(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert {
        "prospective_rosh_candidates",
        "prospective_rosh_shadow_predictions",
        "prospective_rosh_shadow_settlements",
        "prospective_rosh_shadow_evaluations",
    } <= set(inspector.get_table_names())
    prediction_columns = {
        row["name"]
        for row in inspector.get_columns("prospective_rosh_shadow_predictions")
    }
    assert {
        "p0_probability",
        "p1_probability",
        "team_rating_artifact_hash",
        "rosh_radiant_heroes_json",
        "rosh_dire_heroes_json",
        "rosh_statistics_cutoff",
        "rosh_available_at",
        "missing_reason",
    } <= prediction_columns
    candidate_checks = " ".join(
        str(row["sqltext"])
        for row in inspector.get_check_constraints("prospective_rosh_candidates")
    )
    prediction_checks = " ".join(
        str(row["sqltext"])
        for row in inspector.get_check_constraints(
            "prospective_rosh_shadow_predictions"
        )
    )
    assert "beta_rosh > 0" in candidate_checks
    assert "beta_rosh > 0" in prediction_checks
    assert (
        "logit(P1)=logit(P0)+beta_rosh*standardized_pure_rosh_score"
        in candidate_checks
    )
    with postgres_engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgname LIKE "
                    "'prospective_rosh_%'"
                )
            )
        }
        prediction_guard = connection.execute(
            text(
                "SELECT pg_get_functiondef("
                "'validate_prospective_rosh_shadow_prediction()'::regprocedure)"
            )
        ).scalar_one()
    assert {
        "prospective_rosh_candidates_append_only",
        "prospective_rosh_shadow_predictions_insert_guard",
        "prospective_rosh_shadow_predictions_append_only",
        "prospective_rosh_shadow_settlements_insert_guard",
        "prospective_rosh_shadow_settlements_append_only",
        "prospective_rosh_shadow_evaluations_append_only",
    } <= triggers
    assert "expected_contribution :=\n                        candidate_beta *" in prediction_guard
    assert "expected_contribution :=\n                        -candidate_beta *" not in prediction_guard


def test_runtime_is_idempotent_fail_closed_append_only_and_transactional(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    candidate = load_frozen_prospective_rosh_candidate()
    evidence = _evidence(tmp_path)
    session = PostgresSession(postgres_engine)
    repository = ProspectiveRoshShadowRepository(session)
    try:
        with session.transaction():
            first_team, second_team = _seed_authority(session)
            assert repository.store_candidate(
                candidate,
                created_at=datetime(2026, 8, 6, 15, 0, tzinfo=UTC),
            )
            assert not repository.store_candidate(
                candidate,
                created_at=datetime(2026, 8, 6, 16, 0, tzinfo=UTC),
            )

        first = build_shadow_prediction(
            candidate,
            match_id=MATCH_IDS[0],
            series_id=SERIES_IDS[0],
            team_rating=first_team,
            rosh_evidence=evidence,
            created_at=PREDICTION_CUTOFF - timedelta(minutes=1),
        )
        with session.transaction():
            assert repository.store_prediction(first)
            assert not repository.store_prediction(first)

        second = build_shadow_prediction(
            candidate,
            match_id=MATCH_IDS[1],
            series_id=SERIES_IDS[1],
            team_rating=second_team,
            rosh_evidence=evidence,
            created_at=PREDICTION_CUTOFF - timedelta(minutes=1),
        )
        tampered = replace(
            second,
            prediction_hash="",
            p1_probability=float(second.p1_probability) + 0.01,
        )
        tampered = replace(
            tampered,
            prediction_hash=hashlib.sha256(
                canonical_json_bytes(tampered.to_payload(include_hash=False))
            ).hexdigest(),
        )
        with pytest.raises(DBAPIError, match="P1 replay disagrees"):
            with session.transaction():
                repository.store_prediction(tampered)

        p0_only = build_shadow_prediction(
            candidate,
            match_id=MATCH_IDS[1],
            series_id=SERIES_IDS[1],
            team_rating=second_team,
            rosh_evidence=None,
            missing_reason="rosh_not_available_before_cutoff",
            created_at=PREDICTION_CUTOFF - timedelta(minutes=1),
        )
        with pytest.raises(RuntimeError, match="rollback proof"):
            with session.transaction():
                assert repository.store_prediction(p0_only)
                raise RuntimeError("rollback proof")
        count = session.execute(
            "SELECT COUNT(*) AS support "
            "FROM prospective_rosh_shadow_predictions WHERE match_id=?",
            (MATCH_IDS[1],),
        ).fetchone()
        assert int(count["support"]) == 0

        result_usable_at = MATCH_START + timedelta(minutes=41)
        result_hash = _digest(99)
        with session.transaction():
            session.execute(
                "UPDATE matches SET radiant_win=TRUE, duration=2400 "
                "WHERE match_id=?",
                (MATCH_IDS[0],),
            )
            session.execute(
                """UPDATE match_ingest_status
                      SET has_valid_result=1, basic_result_state='ready',
                          first_usable_at=?, latest_raw_content_hash=?,
                          updated_at=?
                    WHERE match_id=?""",
                (
                    result_usable_at.isoformat(),
                    result_hash,
                    result_usable_at.isoformat(),
                    MATCH_IDS[0],
                ),
            )
        settlement = build_shadow_settlement(
            first,
            eventual_radiant_win=1,
            result_artifact_hash=result_hash,
            result_usable_at=result_usable_at,
            settled_at=result_usable_at + timedelta(minutes=1),
            created_at=result_usable_at + timedelta(minutes=2),
        )
        with session.transaction():
            assert repository.store_settlement(settlement)
            assert not repository.store_settlement(settlement)
            assert not repository.store_prediction(first)

        with pytest.raises(DBAPIError, match="append-only"):
            with session.transaction():
                session.execute(
                    "UPDATE prospective_rosh_shadow_predictions "
                    "SET p0_probability=0.6 WHERE prediction_hash=?",
                    (first.prediction_hash,),
                )
        with pytest.raises(DBAPIError, match="append-only"):
            with session.transaction():
                session.execute(
                    "DELETE FROM prospective_rosh_shadow_settlements "
                    "WHERE prediction_hash=?",
                    (first.prediction_hash,),
                )
    finally:
        session.close()
