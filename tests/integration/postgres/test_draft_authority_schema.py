from __future__ import annotations

import hashlib
import json
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
HORIZONS = (10, 20, 30, 40, 50)
DRAFT_AUTHORITY_TABLES = {
    "draft_deployment_revisions",
    "draft_model_artifacts",
    "draft_calibration_artifacts",
    "draft_deployment_bundles",
    "prospective_draft_curves",
    "prospective_draft_landmarks",
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_draft_test_{uuid4().hex}"
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


def _seed_deployment(connection) -> tuple[str, dict[int, str], dict[int, str], str]:
    feature_hash = _hash("feature-schema")
    model_hashes = {horizon: _hash(f"model-{horizon}") for horizon in HORIZONS}
    calibration_hashes = {
        horizon: _hash(f"calibration-{horizon}") for horizon in HORIZONS
    }
    model_artifact = json.dumps(
        {
            "artifact_version": "draft-model-artifact-v2",
            "support": 100,
            "training_corpus": list(range(100)),
        },
        separators=(",", ":"),
    )
    connection.execute(
        text(
            """
            INSERT INTO draft_model_artifacts (
                model_hash, model_version, model_kind, horizon_minutes,
                training_cutoff, feature_schema_hash, training_input_hash,
                artifact_json, created_at
            ) VALUES (
                :model_hash, 'model-v1', 'pure_draft', :horizon_minutes,
                '2026-06-30T00:00:00Z', :feature_schema_hash,
                :training_input_hash, :artifact_json, '2026-07-01T00:00:00Z'
            )
            """
        ),
        [
            {
                "model_hash": model_hashes[horizon],
                "horizon_minutes": horizon,
                "feature_schema_hash": feature_hash,
                "training_input_hash": _hash(f"training-{horizon}"),
                "artifact_json": model_artifact,
            }
            for horizon in HORIZONS
        ],
    )
    connection.execute(
        text(
            """
            INSERT INTO draft_calibration_artifacts (
                calibration_hash, model_hash, calibration_version,
                horizon_minutes, evidence_mode, support, artifact_json, created_at
            ) VALUES (
                :calibration_hash, :model_hash, 'calibration-v1',
                :horizon_minutes, 'prospective', 100, '{}',
                '2026-07-01T00:00:00Z'
            )
            """
        ),
        [
            {
                "calibration_hash": calibration_hashes[horizon],
                "model_hash": model_hashes[horizon],
                "horizon_minutes": horizon,
            }
            for horizon in HORIZONS
        ],
    )
    deployment_key = _hash("deployment")
    connection.execute(
        text(
            """
            INSERT INTO draft_deployment_bundles (
                deployment_key, model_hashes_json, calibration_hashes_json,
                training_cutoff, dependency_fingerprint, dependency_revision,
                evidence_mode, created_at
            ) VALUES (
                :deployment_key, :model_hashes_json, :calibration_hashes_json,
                '2026-06-30T00:00:00Z', :dependency_fingerprint, 1,
                'prospective', '2026-07-01T00:00:00Z'
            )
            """
        ),
        {
            "deployment_key": deployment_key,
            "model_hashes_json": json.dumps(
                {str(key): value for key, value in model_hashes.items()},
                separators=(",", ":"),
            ),
            "calibration_hashes_json": json.dumps(
                {str(key): value for key, value in calibration_hashes.items()},
                separators=(",", ":"),
            ),
            "dependency_fingerprint": _hash("dependency"),
        },
    )
    return deployment_key, model_hashes, calibration_hashes, feature_hash


def test_draft_schema_requires_complete_five_horizon_bundle(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert DRAFT_AUTHORITY_TABLES <= set(inspector.get_table_names())
    assert "prospective_draft_landmark_authority" in inspector.get_view_names()

    with postgres_engine.begin() as connection:
        deployment_key, model_hashes, _, _ = _seed_deployment(connection)

    with postgres_engine.connect() as connection:
        revision = connection.execute(
            text(
                "SELECT artifact_revision FROM draft_deployment_revisions "
                "WHERE singleton = 1"
            )
        ).scalar_one()
    assert revision == 12

    with pytest.raises(DBAPIError, match="bundle authority is required"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO draft_deployment_bundles (
                        deployment_key, model_hashes_json,
                        calibration_hashes_json, training_cutoff,
                        dependency_fingerprint, dependency_revision,
                        evidence_mode, created_at
                    ) VALUES (
                        :deployment_key, :model_hashes_json, '{}',
                        '2026-06-30T00:00:00Z', :dependency_fingerprint, 1,
                        'prospective', '2026-07-01T00:00:00Z'
                    )
                    """
                ),
                {
                    "deployment_key": _hash("invalid-deployment"),
                    "model_hashes_json": json.dumps(
                        {"10": model_hashes[10]}, separators=(",", ":")
                    ),
                    "dependency_fingerprint": _hash("invalid-dependency"),
                },
            )

    with pytest.raises(DBAPIError, match="deployment bundle is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE draft_deployment_bundles "
                    "SET evidence_mode = 'reconstructed_walk_forward' "
                    "WHERE deployment_key = :deployment_key"
                ),
                {"deployment_key": deployment_key},
            )


def test_prospective_curve_and_landmark_require_deployed_authority(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        deployment_key, model_hashes, calibration_hashes, feature_hash = (
            _seed_deployment(connection)
        )
        curve_key = _hash("curve")
        connection.execute(
            text(
                """
                INSERT INTO prospective_draft_curves (
                    curve_key, raybet_match_id, map_number, strict_mapping_id,
                    lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                    prediction_cutoff, first_usable_at, availability_mode,
                    created_at, radiant_team_side, anchor_draft_hash,
                    anchor_source_frame_ref, anchor_anchored_at,
                    anchor_team_side_source_frame_ref,
                    anchor_team_side_anchored_at, deployment_key,
                    target_snapshot_hash, feature_snapshot_json,
                    feature_dependency_fingerprint, feature_dependency_revision
                ) VALUES (
                    :curve_key, 'match-1', 1, 1, :lineup_hash,
                    '[1,2,3,4,5]', '[6,7,8,9,10]',
                    '2026-07-02T00:00:00Z', '2026-07-02T00:01:00Z',
                    'prospective', '2026-07-02T00:02:00Z', 'team_one',
                    :anchor_draft_hash, 'frame-1', '2026-07-01T23:59:00Z',
                    'frame-1', '2026-07-01T23:59:00Z', :deployment_key,
                    :target_snapshot_hash, '{}', :dependency_fingerprint, 1
                )
                """
            ),
            {
                "curve_key": curve_key,
                "lineup_hash": _hash("lineup"),
                "anchor_draft_hash": _hash("anchor"),
                "deployment_key": deployment_key,
                "target_snapshot_hash": _hash("target"),
                "dependency_fingerprint": _hash("dependency"),
            },
        )
        landmark_key = _hash("landmark-10")
        connection.execute(
            text(
                """
                INSERT INTO prospective_draft_landmarks (
                    landmark_key, curve_key, horizon_minutes,
                    radiant_probability, scaling_edge, synergy_edge, quality,
                    validation_status, support, calibration_ref,
                    input_refs_json, uncertainty, feature_hash, model_hash,
                    calibration_hash, global_calibration_passed, global_gate_ref,
                    model_version, model_kind, availability_mode,
                    input_snapshot_hash, created_at, raw_radiant_probability,
                    deployment_key, model_input_hash, raw_uncertainty
                ) VALUES (
                    :landmark_key, :curve_key, 10, 0.61, 0.08, 0.03, 0.9,
                    'passed', 100, 'calibration-v1', '{}', 0.05,
                    :feature_hash, :model_hash, :calibration_hash, 1,
                    'global-gate-v1', 'model-v1', 'pure_draft', 'prospective',
                    :input_snapshot_hash, '2026-07-02T00:02:00Z', 0.63,
                    :deployment_key, :model_input_hash, 0.06
                )
                """
            ),
            {
                "landmark_key": landmark_key,
                "curve_key": curve_key,
                "feature_hash": feature_hash,
                "model_hash": model_hashes[10],
                "calibration_hash": calibration_hashes[10],
                "input_snapshot_hash": _hash("input-snapshot"),
                "deployment_key": deployment_key,
                "model_input_hash": _hash("model-input"),
            },
        )

    with postgres_engine.connect() as connection:
        authority = connection.execute(
            text(
                "SELECT landmark_key, horizon_minutes, radiant_probability "
                "FROM prospective_draft_landmark_authority "
                "WHERE curve_key = :curve_key"
            ),
            {"curve_key": curve_key},
        ).one()
    assert authority == (landmark_key, 10, 0.61)

    with pytest.raises(DBAPIError, match="draft curve is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE prospective_draft_curves "
                    "SET radiant_team_side = 'team_two' "
                    "WHERE curve_key = :curve_key"
                ),
                {"curve_key": curve_key},
            )
