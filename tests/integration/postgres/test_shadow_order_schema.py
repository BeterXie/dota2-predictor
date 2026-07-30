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
from tests.integration.postgres.test_draft_authority_schema import _seed_deployment
from tests.integration.postgres.test_strict_live_mapping_schema import (
    _insert_mapping,
    _seed_event,
)


ROOT = Path(__file__).resolve().parents[3]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def postgres_engine() -> Iterator[Engine]:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    base_url = make_url(require_database_url(configured))
    database_name = f"dota2_predictor_order_test_{uuid4().hex}"
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


def _insert_pending_order(connection, order_key: str) -> None:
    _seed_event(connection)
    mapping_id = _insert_mapping(connection)
    deployment_key, model_hashes, calibration_hashes, feature_hash = (
        _seed_deployment(connection)
    )
    anchor_hash = _hash("anchor")
    anchor_frame_hash = _hash("anchor-frame")
    current_frame_hash = _hash("current-frame")
    anchor_frame_ref = f"vision-frame:sha256:{anchor_frame_hash}"
    current_frame_ref = f"vision-frame:sha256:{current_frame_hash}"
    connection.execute(
        text(
            """
            INSERT INTO vision_frame_artifacts (
                frame_ref, content_sha256, byte_length, storage_path,
                registered_at
            ) VALUES
                (:anchor_frame_ref, :anchor_frame_hash, 4096,
                 'vision/anchor.png', '2026-07-02T00:00:00Z'),
                (:current_frame_ref, :current_frame_hash, 4096,
                 'vision/current.png', '2026-07-02T00:01:00Z')
            """
        ),
        {
            "anchor_frame_ref": anchor_frame_ref,
            "anchor_frame_hash": anchor_frame_hash,
            "current_frame_ref": current_frame_ref,
            "current_frame_hash": current_frame_hash,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO vision_observations (
                raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids,
                radiant_team_side, clock_confidence, draft_confidence,
                source_frame_ref, source_frame_sha256, source_frame_bytes,
                screen_state, confirmed
            ) VALUES
                ('match-1', 1, '2026-07-02T00:00:00Z', 540, 0,
                 '[1,2,3,4,5]', '[6,7,8,9,10]', 'team_one', 0.95, 0.96,
                 :anchor_frame_ref, :anchor_frame_hash, 4096, 'game', 1),
                ('match-1', 1, '2026-07-02T00:01:00Z', 600, 0,
                 '[1,2,3,4,5]', '[6,7,8,9,10]', 'team_one', 0.95, 0.96,
                 :current_frame_ref, :current_frame_hash, 4096, 'game', 1)
            """
        ),
        {
            "anchor_frame_ref": anchor_frame_ref,
            "anchor_frame_hash": anchor_frame_hash,
            "current_frame_ref": current_frame_ref,
            "current_frame_hash": current_frame_hash,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO vision_draft_anchors (
                raybet_match_id, map_number, draft_hash, radiant_hero_ids,
                dire_hero_ids, radiant_team_side, team_side_anchored_at,
                team_side_source_frame_ref, anchored_at, source_frame_ref,
                status
            ) VALUES (
                'match-1', 1, :anchor_hash, '[1,2,3,4,5]',
                '[6,7,8,9,10]', 'team_one', '2026-07-02T00:00:00Z',
                :anchor_frame_ref, '2026-07-02T00:00:00Z',
                :anchor_frame_ref, 'anchored'
            )
            """
        ),
        {"anchor_hash": anchor_hash, "anchor_frame_ref": anchor_frame_ref},
    )
    curve_key = _hash("curve")
    target_snapshot_hash = _hash("target")
    input_snapshot_hash = _hash("input-snapshot")
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
                :curve_key, 'match-1', 1, :mapping_id, :lineup_hash,
                '[1,2,3,4,5]', '[6,7,8,9,10]',
                '2026-07-02T00:00:00Z', '2026-07-02T00:01:00Z',
                'prospective', '2026-07-02T00:01:00Z', 'team_one',
                :anchor_hash, :anchor_frame_ref, '2026-07-02T00:00:00Z',
                :anchor_frame_ref, '2026-07-02T00:00:00Z', :deployment_key,
                :target_snapshot_hash, '{}', :dependency_fingerprint, 1
            )
            """
        ),
        {
            "curve_key": curve_key,
            "mapping_id": mapping_id,
            "lineup_hash": _hash("lineup"),
            "anchor_hash": anchor_hash,
            "anchor_frame_ref": anchor_frame_ref,
            "deployment_key": deployment_key,
            "target_snapshot_hash": target_snapshot_hash,
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
                :input_snapshot_hash, '2026-07-02T00:01:00Z', 0.63,
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
            "input_snapshot_hash": input_snapshot_hash,
            "deployment_key": deployment_key,
            "model_input_hash": _hash("model-input"),
        },
    )
    artifact_hash = _hash("odds-artifact")
    response_state_hash = _hash("odds-response-state")
    normalized_state_hash = _hash("odds-normalized-state")
    connection.execute(
        text(
            """
            INSERT INTO odds_raw_artifacts (
                artifact_hash, source, storage_path, uncompressed_bytes,
                compressed_bytes, schema_fingerprint
            ) VALUES (
                :artifact_hash, 'raybet', 'raw/odds.json.gz', 100, 50,
                :schema_fingerprint
            )
            """
        ),
        {"artifact_hash": artifact_hash, "schema_fingerprint": _hash("schema")},
    )
    connection.execute(
        text(
            """
            INSERT INTO odds_response_states (
                response_state_hash, raybet_match_id, normalized_state_hash,
                normalized_state_hash_version, outcome_count
            ) VALUES (
                :response_state_hash, 'match-1', :normalized_state_hash, 2, 2
            )
            """
        ),
        {
            "response_state_hash": response_state_hash,
            "normalized_state_hash": normalized_state_hash,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO odds_response_state_outcomes (
                response_state_hash, odds_id, odds_group_id, price, status,
                market_type, period, side, outcome_key, supported
            ) VALUES (
                :response_state_hash, :odds_id, 'winner-map-1', :price,
                'open', 'winner', 'map_1', :side, :side, 1
            )
            """
        ),
        [
            {
                "response_state_hash": response_state_hash,
                "odds_id": "team-one",
                "price": 2.2,
                "side": "team_one",
            },
            {
                "response_state_hash": response_state_hash,
                "odds_id": "team-two",
                "price": 1.7,
                "side": "team_two",
            },
        ],
    )
    connection.execute(
        text(
            """
            INSERT INTO odds_transport_observations (
                observation_key, source, raybet_match_id, observed_at,
                normalized_state_hash, normalized_state_hash_version,
                response_state_hash, response_artifact_hash, timing_status,
                processing_status, normalized_change_count
            ) VALUES (
                'transport-1', 'direct', 'match-1',
                '2026-07-02T00:01:00Z', :normalized_state_hash, 2,
                :response_state_hash, :artifact_hash, 'on_time',
                'processing', 0
            )
            """
        ),
        {
            "normalized_state_hash": normalized_state_hash,
            "response_state_hash": response_state_hash,
            "artifact_hash": artifact_hash,
        },
    )
    connection.execute(
        text(
            "UPDATE odds_transport_observations "
            "SET processing_status = 'processed', normalized_change_count = 2 "
            "WHERE observation_key = 'transport-1'"
        )
    )
    market_probability = connection.execute(
        text(
            "SELECT underdog_probability "
            "FROM trusted_odds_winner_market_authority "
            "WHERE observation_key = 'transport-1'"
        )
    ).scalar_one()
    decision_key = _hash(f"decision:{order_key}")
    authority_revision = connection.execute(
        text(
            "SELECT authority_revision FROM draft_authority_revisions "
            "WHERE singleton = 1"
        )
    ).scalar_one()
    dependency_revision = connection.execute(
        text(
            "SELECT dependency_revision FROM draft_lineage_revisions "
            "WHERE singleton = 1"
        )
    ).scalar_one()
    decision_parameters = {
        "decision_key": decision_key,
        "market_probability": market_probability,
        "curve_key": curve_key,
        "source_ref": f"prospective-draft:{curve_key}",
        "landmark_key": landmark_key,
        "mapping_id": mapping_id,
        "deployment_key": deployment_key,
        "target_snapshot_hash": target_snapshot_hash,
        "feature_hash": feature_hash,
        "model_hash": model_hashes[10],
        "calibration_hash": calibration_hashes[10],
        "input_snapshot_hash": input_snapshot_hash,
        "authority_revision": authority_revision,
        "dependency_revision": dependency_revision,
        "current_frame_ref": current_frame_ref,
        "current_frame_hash": current_frame_hash,
    }
    connection.execute(
        text(
            """
            INSERT INTO strategy_decisions (
                decision_key, raybet_match_id, map_number, decided_at,
                underdog_side, market_probability, model_probability, edge,
                data_quality, eligible, reason, contributions_json, input_ref,
                strategy_version, draft_curve_key, draft_source_ref,
                draft_landmark_key, draft_landmark_horizon_minutes,
                draft_landmark_target, draft_landmark_radiant_probability,
                draft_landmark_quality, draft_landmark_uncertainty,
                draft_landmark_support, draft_radiant_team_side,
                draft_strict_mapping_id, draft_deployment_key,
                draft_target_snapshot_hash, draft_feature_hash,
                draft_model_hash, draft_calibration_hash, draft_model_version,
                draft_global_gate_ref, draft_input_snapshot_hash,
                draft_authority_revision, draft_dependency_revision,
                vision_raybet_match_id, vision_map_number, vision_captured_at,
                vision_source_frame_ref, vision_source_frame_sha256,
                vision_source_frame_bytes, vision_observed_game_clock_seconds,
                vision_aligned_game_clock_seconds, vision_is_paused,
                vision_radiant_hero_ids_json, vision_dire_hero_ids_json,
                vision_radiant_team_side, vision_clock_confidence,
                vision_draft_confidence, vision_screen_state, vision_confirmed,
                vision_transport_key, vision_transport_at,
                vision_alignment_method, vision_alignment_lag_seconds
            ) VALUES (
                :decision_key, 'match-1', 1, '2026-07-02T00:01:00Z',
                'team_one', :market_probability, 0.61,
                0.61 - :market_probability, 0.95, 1, 'eligible', '{}',
                'input-1', 'strategy-v1', :curve_key, :source_ref,
                :landmark_key, 10, 'radiant_win', 0.61, 0.9, 0.05, 100,
                'team_one', :mapping_id, :deployment_key,
                :target_snapshot_hash, :feature_hash, :model_hash,
                :calibration_hash, 'model-v1', 'global-gate-v1',
                :input_snapshot_hash, :authority_revision,
                :dependency_revision, 'match-1', 1,
                '2026-07-02T00:01:00Z', :current_frame_ref,
                :current_frame_hash, 4096, 600, 600, 0,
                '[1,2,3,4,5]', '[6,7,8,9,10]', 'team_one', 0.95, 0.96,
                'game', 1, 'transport-1', '2026-07-02T00:01:00Z',
                'anchor', 0.0
            )
            """
        ),
        decision_parameters,
    )
    connection.execute(
        text(
            """
            INSERT INTO shadow_map_attempts (
                raybet_match_id, map_number, order_key, status, created_at
            ) VALUES (
                'match-1', 1, :order_key, 'pending',
                '2026-07-02T00:01:00Z'
            )
            """
        ),
        {"order_key": order_key},
    )
    connection.execute(
        text(
            """
            INSERT INTO shadow_order_decision_lineage (
                order_key, decision_key, recorded_at
            ) VALUES (
                :order_key, :decision_key, '2026-07-02T00:01:00Z'
            )
            """
        ),
        {"order_key": order_key, "decision_key": decision_key},
    )
    connection.execute(
        text(
            """
            INSERT INTO shadow_orders (
                order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status, draft_curve_key,
                draft_source_ref, draft_landmark_key,
                draft_landmark_horizon_minutes, draft_landmark_target,
                draft_landmark_radiant_probability, draft_landmark_quality,
                draft_landmark_uncertainty, draft_landmark_support,
                draft_radiant_team_side, draft_strict_mapping_id,
                draft_deployment_key, draft_target_snapshot_hash,
                draft_feature_hash, draft_model_hash, draft_calibration_hash,
                draft_model_version, draft_global_gate_ref,
                draft_input_snapshot_hash, draft_authority_revision,
                draft_dependency_revision, vision_raybet_match_id,
                vision_map_number, vision_captured_at, vision_source_frame_ref,
                vision_source_frame_sha256, vision_source_frame_bytes,
                vision_observed_game_clock_seconds,
                vision_aligned_game_clock_seconds, vision_is_paused,
                vision_radiant_hero_ids_json, vision_dire_hero_ids_json,
                vision_radiant_team_side, vision_clock_confidence,
                vision_draft_confidence, vision_screen_state, vision_confirmed,
                vision_transport_key, vision_transport_at,
                vision_alignment_method, vision_alignment_lag_seconds
            ) VALUES (
                :order_key, 'match-1', :mapping_id, 'team-one',
                'winner|map_1|team_one|', '2026-07-02T00:01:00Z', 0.61,
                :market_probability, 2.2, 'transport-1',
                '2026-07-02T00:01:00Z', '2026-07-02T00:03:00Z',
                'winner-map-1', 'team_one', 1, 0.25, 'pending', :curve_key,
                :source_ref, :landmark_key, 10, 'radiant_win', 0.61, 0.9,
                0.05, 100, 'team_one', :mapping_id, :deployment_key,
                :target_snapshot_hash, :feature_hash, :model_hash,
                :calibration_hash, 'model-v1', 'global-gate-v1',
                :input_snapshot_hash, :authority_revision,
                :dependency_revision, 'match-1', 1,
                '2026-07-02T00:01:00Z', :current_frame_ref,
                :current_frame_hash, 4096, 600, 600, 0,
                '[1,2,3,4,5]', '[6,7,8,9,10]', 'team_one', 0.95, 0.96,
                'game', 1, 'transport-1', '2026-07-02T00:01:00Z',
                'anchor', 0.0
            )
            """
        ),
        {**decision_parameters, "order_key": order_key},
    )


def test_shadow_order_schema_links_all_declared_authorities(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert {
        "live_frames",
        "live_events",
        "model_quotes",
        "shadow_orders",
        "shadow_order_decision_lineage",
    } <= set(inspector.get_table_names())

    targets = {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("shadow_orders")
    }
    assert {
        "strict_live_map_mappings",
        "prospective_draft_curves",
        "prospective_draft_landmarks",
        "draft_deployment_bundles",
        "draft_model_artifacts",
        "draft_calibration_artifacts",
    } <= targets

    with postgres_engine.connect() as connection:
        trigger_names = set(
            connection.execute(
                text(
                    "SELECT trigger_name FROM information_schema.triggers "
                    "WHERE event_object_table = 'shadow_orders'"
                )
            ).scalars()
        )
    assert {
        "shadow_orders_terminal_immutable",
        "shadow_orders_immutable_delete",
        "strict_live_shadow_impact_after_insert",
        "shadow_orders_signal_identity_guard",
        "shadow_order_draft_authority_insert",
        "shadow_order_vision_authority_insert",
    } <= trigger_names


def test_pending_order_allows_one_terminal_transition_only(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        _insert_pending_order(connection, "order-1")
        connection.execute(
            text(
                "UPDATE shadow_orders "
                "SET status = 'filled', fill_price = 2.05, "
                "filled_at = '2026-07-30T00:01:00Z' "
                "WHERE order_key = 'order-1'"
            )
        )

    with postgres_engine.connect() as connection:
        state = connection.execute(
            text(
                "SELECT status, fill_price, filled_at, rejection_reason "
                "FROM shadow_orders WHERE order_key = 'order-1'"
            )
        ).one()
    assert state == ("filled", 2.05, "2026-07-30T00:01:00Z", None)

    with pytest.raises(DBAPIError, match="terminal state is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE shadow_orders "
                    "SET status = 'rejected', fill_price = NULL, filled_at = NULL, "
                    "rejection_reason = 'changed' WHERE order_key = 'order-1'"
                )
            )

    with pytest.raises(DBAPIError, match="shadow orders are immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM shadow_orders WHERE order_key = 'order-1'")
            )


def test_pending_order_payload_and_decision_lineage_are_immutable(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        _insert_pending_order(connection, "order-2")

    with pytest.raises(DBAPIError, match="terminal state is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE shadow_orders SET stake = 0.5 "
                    "WHERE order_key = 'order-2'"
                )
            )

    with pytest.raises(DBAPIError, match="decision lineage is immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE shadow_order_decision_lineage "
                    "SET decision_key = 'decision-2' WHERE order_key = 'order-2'"
                )
            )
