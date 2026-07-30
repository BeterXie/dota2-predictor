from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from database.engine import build_engine, require_database_url


ROOT = Path(__file__).resolve().parents[3]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_vision_test_{uuid4().hex}"
    admin_engine = create_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    test_url = base_url.set(database=database_name)
    engine: Engine | None = None
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option(
            "sqlalchemy.url",
            test_url.render_as_string(hide_password=False),
        )
        command.upgrade(config, "head")
        engine = build_engine(test_url.render_as_string(hide_password=False))
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin_engine.dispose()


def test_vision_and_rosh_authority_tables_exist(postgres_engine: Engine) -> None:
    inspector = inspect(postgres_engine)
    assert {
        "vision_frame_artifacts",
        "vision_frame_artifact_relocations",
        "vision_frame_artifact_retirements",
        "vision_observations",
        "vision_observation_invalidations",
        "vision_draft_anchors",
        "vision_draft_conflicts",
        "rosh_lineup_scores",
        "rosh_analysis_runs",
        "rosh_hero_scores",
        "rosh_minute_points",
        "official_rosh_shadow_evaluations",
        "vision_derived_invalidations",
        "odds_alignments",
    } <= set(inspector.get_table_names())
    assert {
        "active_vision_frame_artifacts",
        "trusted_vision_observation_authority",
        "verified_strategy_decision_vision_authority",
    } <= set(inspector.get_view_names())


def test_strategy_decision_is_immutable_and_eligible_rows_fail_closed(
    postgres_engine: Engine,
) -> None:
    statement = text(
        """
        INSERT INTO strategy_decisions (
            decision_key, raybet_match_id, map_number, decided_at,
            underdog_side, market_probability, model_probability, edge,
            data_quality, eligible, reason, contributions_json,
            input_ref, strategy_version
        ) VALUES (
            :decision_key, 'match-1', 1, '2026-07-30T00:00:00Z',
            'team_one', 0.48, 0.61, 0.13, 0.9, :eligible,
            :reason, '{}', 'input-1', 'strategy-v1'
        )
        """
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            statement,
            {
                "decision_key": "decision-rejected",
                "eligible": 0,
                "reason": "edge_below_threshold",
            },
        )

    with pytest.raises(DBAPIError, match="strategy decisions are immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE strategy_decisions SET reason = 'mutated' "
                    "WHERE decision_key = 'decision-rejected'"
                )
            )

    with pytest.raises(DBAPIError, match="draft authority is required"):
        with postgres_engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "decision_key": "decision-eligible-without-authority",
                    "eligible": 1,
                    "reason": "eligible",
                },
            )


def test_trusted_vision_requires_active_frame_and_no_invalidation(
    postgres_engine: Engine,
) -> None:
    content_hash = _hash("frame")
    frame_ref = f"vision-frame:sha256:{content_hash}"
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO vision_frame_artifacts (
                    frame_ref, content_sha256, byte_length,
                    storage_path, registered_at
                ) VALUES (
                    :frame_ref, :content_hash, 4096, 'vision/frame.png',
                    '2026-07-30T00:00:00Z'
                )
                """
            ),
            {"frame_ref": frame_ref, "content_hash": content_hash},
        )
        connection.execute(
            text(
                """
                INSERT INTO vision_observations (
                    raybet_match_id, map_number, captured_at,
                    game_clock_seconds, is_paused, radiant_hero_ids,
                    dire_hero_ids, radiant_team_side, clock_confidence,
                    draft_confidence, source_frame_ref, source_frame_sha256,
                    source_frame_bytes, screen_state, confirmed
                ) VALUES (
                    'match-1', 1, '2026-07-30T00:01:00Z', 600, 0,
                    '[1,2,3,4,5]', '[6,7,8,9,10]', 'team_one', 0.95, 0.96,
                    :frame_ref, :content_hash, 4096, 'game', 1
                )
                """
            ),
            {"frame_ref": frame_ref, "content_hash": content_hash},
        )

    with postgres_engine.connect() as connection:
        trusted = connection.execute(
            text(
                "SELECT raybet_match_id, map_number, game_clock_seconds "
                "FROM trusted_vision_observation_authority"
            )
        ).one()
    assert trusted == ("match-1", 1, 600)

    with pytest.raises(DBAPIError, match="frame identity is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE vision_observations SET source_frame_bytes = 4097 "
                    "WHERE raybet_match_id = 'match-1'"
                )
            )

    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO vision_observation_invalidations (
                    raybet_match_id, captured_at, source_frame_ref,
                    invalidated_at, reason
                ) VALUES (
                    'match-1', '2026-07-30T00:01:00Z', :frame_ref,
                    '2026-07-30T00:02:00Z', 'draft conflict'
                )
                """
            ),
            {"frame_ref": frame_ref},
        )

    with postgres_engine.connect() as connection:
        trusted_count = connection.execute(
            text("SELECT COUNT(*) FROM trusted_vision_observation_authority")
        ).scalar_one()
    assert trusted_count == 0

    with pytest.raises(DBAPIError, match="invalidation audit is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE vision_observation_invalidations "
                    "SET reason = 'mutated' WHERE raybet_match_id = 'match-1'"
                )
            )


def _insert_rosh_run(connection, *, run_id: str, status: str) -> None:
    succeeded = status == "succeeded"
    connection.execute(
        text(
            """
            INSERT INTO rosh_analysis_runs (
                run_id, status, mode, match_id, date_time, draft_hash,
                draft_json, rosh_profile_id, formula_version,
                request_profile_hash, upstream_bundle_hash, scorer_source_hash,
                canonical_profile_hash, serialization_version, request_hash,
                request_manifest_json, response_manifest_json,
                radiant_team_score, dire_team_score, relative_advantage,
                result_json, evidence_hash, error_code, collected_at
            ) VALUES (
                :run_id, :status, 'explicit_draft', NULL, 1784485548,
                :draft_hash, '{}', 'official-v1', 'formula-v1',
                :request_profile_hash, :upstream_bundle_hash,
                :scorer_source_hash, :canonical_profile_hash, 'jcs-v1',
                :request_hash, '{}', :response_manifest_json,
                :radiant_team_score, :dire_team_score, :relative_advantage,
                :result_json, :evidence_hash, :error_code,
                '2026-07-30T00:00:00Z'
            )
            """
        ),
        {
            "run_id": run_id,
            "status": status,
            "draft_hash": _hash(f"draft-{run_id}"),
            "request_profile_hash": _hash(f"profile-{run_id}"),
            "upstream_bundle_hash": _hash(f"bundle-{run_id}"),
            "scorer_source_hash": _hash(f"scorer-{run_id}"),
            "canonical_profile_hash": _hash(f"canonical-{run_id}"),
            "request_hash": _hash(f"request-{run_id}"),
            "response_manifest_json": "[{}]" if succeeded else "[]",
            "radiant_team_score": 4.2 if succeeded else None,
            "dire_team_score": -1.6 if succeeded else None,
            "relative_advantage": 5.8 if succeeded else None,
            "result_json": "{}" if succeeded else None,
            "evidence_hash": _hash(f"evidence-{run_id}"),
            "error_code": None if succeeded else "upstream_failed",
        },
    )


def test_rosh_children_require_succeeded_immutable_run(
    postgres_engine: Engine,
) -> None:
    succeeded_run = _hash("succeeded-run")
    failed_run = _hash("failed-run")
    with postgres_engine.begin() as connection:
        _insert_rosh_run(connection, run_id=succeeded_run, status="succeeded")
        _insert_rosh_run(connection, run_id=failed_run, status="failed")
        connection.execute(
            text(
                """
                INSERT INTO rosh_hero_scores (
                    run_id, team_side, position_id, hero_id,
                    raw_score, display_score, components_json
                ) VALUES (
                    :run_id, 'RADIANT', 1, 1, 1.25, 1.3, '{}'
                )
                """
            ),
            {"run_id": succeeded_run},
        )
        connection.execute(
            text(
                """
                INSERT INTO rosh_minute_points (
                    run_id, minute, raw_score, display_score,
                    radiant_time_delta, dire_time_delta,
                    synergy_delta, source_audit_json
                ) VALUES (
                    :run_id, 10, 2.45, 2.5, 1.1, -0.4, 1.75, '{}'
                )
                """
            ),
            {"run_id": succeeded_run},
        )

    with pytest.raises(DBAPIError, match="requires succeeded run"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO rosh_hero_scores (
                        run_id, team_side, position_id, hero_id,
                        raw_score, display_score, components_json
                    ) VALUES (
                        :run_id, 'DIRE', 1, 2, -1.0, -1.0, '{}'
                    )
                    """
                ),
                {"run_id": failed_run},
            )

    with pytest.raises(DBAPIError, match="analysis run is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE rosh_analysis_runs SET formula_version = 'mutated' "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": succeeded_run},
            )
