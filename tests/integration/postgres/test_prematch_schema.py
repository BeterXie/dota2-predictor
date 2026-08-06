from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from event_intelligence.prematch_calibration import (
    build_prematch_calibration_artifact,
)
from event_intelligence.prematch_storage import build_prematch_calibration_record
from event_intelligence.raw_archive import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[3]
MODE = "reconstructed_walk_forward"
PREMATCH_TABLES = {
    "prematch_training_corpus_rows",
    "prematch_training_corpus_prefixes",
    "prematch_model_runs",
    "prematch_predictions",
    "prematch_calibration_artifacts",
    "prematch_prediction_validations",
    "prematch_lineage_revisions",
    "prematch_lineage_changes",
}


def _columns(engine: Engine, table: str) -> tuple[str, ...]:
    return tuple(column["name"] for column in inspect(engine).get_columns(table))


def _insert_parent_model(
    connection,
    *,
    model_hash: str = "a" * 64,
    artifact_json: str | None = None,
) -> None:
    artifact = (
        artifact_json
        or canonical_json_bytes(
            {
                "artifact_version": "prematch-model-artifact-v1",
                "availability_mode": MODE,
                "feature_schema_hash": "b" * 64,
                "model_hash": model_hash,
                "model_kind": "team_only",
                "model_version": "prematch-offset-logistic-l2-v2",
                "status": "trained",
                "training_cutoff": "2026-01-01T00:00:00+00:00",
                "training_input_hash": "c" * 64,
            }
        ).decode()
    )
    connection.execute(
        text(
            """
            INSERT INTO prematch_model_runs (
                run_id, model_version, artifact_version, model_kind,
                availability_mode, training_cutoff, feature_schema_hash,
                training_input_hash, model_hash, artifact_json,
                metrics_json, status, created_at
            ) VALUES (
                :model_hash, 'prematch-offset-logistic-l2-v2',
                'prematch-model-artifact-v1', 'team_only', :mode,
                '2026-01-01T00:00:00+00:00', :feature_hash,
                :training_hash, :model_hash, :artifact, NULL, 'trained',
                '2026-08-05T00:00:00+00:00'
            )
            """
        ),
        {
            "model_hash": model_hash,
            "mode": MODE,
            "feature_hash": "b" * 64,
            "training_hash": "c" * 64,
            "artifact": artifact,
        },
    )


def test_prematch_schema_has_tables_columns_indexes_and_triggers(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert PREMATCH_TABLES <= set(inspector.get_table_names())
    assert _columns(postgres_engine, "prematch_model_runs") == (
        "run_id",
        "model_version",
        "artifact_version",
        "model_kind",
        "availability_mode",
        "training_cutoff",
        "feature_schema_hash",
        "training_input_hash",
        "model_hash",
        "artifact_json",
        "metrics_json",
        "status",
        "created_at",
    )
    assert _columns(postgres_engine, "prematch_training_corpus_rows") == (
        "row_hash",
        "model_kind",
        "row_json",
        "created_at",
    )
    assert _columns(postgres_engine, "prematch_training_corpus_prefixes") == (
        "prefix_hash",
        "model_kind",
        "availability_mode",
        "parent_prefix_hash",
        "row_hash",
        "support",
        "created_at",
    )
    assert _columns(postgres_engine, "prematch_predictions") == (
        "prediction_id",
        "run_id",
        "match_id",
        "prediction_cutoff",
        "cutoff_source",
        "input_snapshot_hash",
        "artifact_fingerprint",
        "dependency_fingerprint",
        "dependency_revision",
        "calibration_hash",
        "team_base_probability",
        "raw_probability",
        "calibrated_probability",
        "parameter_uncertainty",
        "draft_logit_delta",
        "rosh_logit_delta",
        "cluster_logit_delta",
        "total_adjustment",
        "coverage",
        "support",
        "prediction_json",
        "eventual_radiant_win",
        "result_usable_at",
        "settled_at",
        "status",
        "created_at",
    )
    assert _columns(postgres_engine, "prematch_calibration_artifacts") == (
        "calibration_key",
        "model_kind",
        "model_hash",
        "calibration_version",
        "fit_cutoff",
        "evaluation_cutoff",
        "fit_support",
        "evaluation_support",
        "parameters_json",
        "metrics_json",
        "input_hash",
        "calibration_hash",
        "artifact_json",
        "status",
        "created_at",
    )
    assert _columns(postgres_engine, "prematch_prediction_validations") == (
        "run_id",
        "match_id",
        "input_snapshot_hash",
        "artifact_fingerprint",
        "dependency_fingerprint",
        "dependency_revision",
        "validation_version",
        "validated_at",
    )
    indexes = {
        row["name"] for table in PREMATCH_TABLES for row in inspector.get_indexes(table)
    }
    assert {
        "idx_prematch_model_runs_kind_cutoff",
        "idx_prematch_predictions_match_cutoff",
        "idx_prematch_predictions_run",
        "idx_prematch_calibration_model_cutoff",
        "idx_prematch_prediction_validations_fingerprint",
        "idx_prematch_corpus_prefix_parent",
    } <= indexes
    calibration_checks = {
        row["name"]
        for row in inspector.get_check_constraints("prematch_calibration_artifacts")
    }
    assert "ck_prematch_calibration_parameters_by_status" in calibration_checks
    with postgres_engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        triggers = set(
            connection.execute(
                text(
                    "SELECT trigger_name FROM information_schema.triggers "
                    "WHERE trigger_schema=current_schema()"
                )
            ).scalars()
        )
        assert revision == "20260806_0030"
    assert {
        "prematch_predictions_mutation_guard",
        "prematch_calibration_mode_guard",
        "prematch_training_corpus_rows_append_only",
        "prematch_training_corpus_prefixes_append_only",
        "prematch_prediction_validations_claim_guard",
        "prematch_model_runs_append_only",
        "prematch_calibration_artifacts_append_only",
        "prematch_prediction_validations_append_only",
        "prematch_lineage_changes_append_only",
    } <= triggers


def test_json_object_checks_preserve_jcs_numeric_text_and_reject_arrays(
    postgres_engine: Engine,
) -> None:
    model_hash = "d" * 64
    artifact = canonical_json_bytes(
        {
            "artifact_version": "prematch-model-artifact-v1",
            "availability_mode": MODE,
            "feature_schema_hash": "b" * 64,
            "model_hash": model_hash,
            "model_kind": "team_only",
            "model_version": "prematch-offset-logistic-l2-v2",
            "numeric_probe": 1e-15,
            "status": "trained",
            "training_cutoff": "2026-01-01T00:00:00+00:00",
            "training_input_hash": "c" * 64,
        }
    ).decode()
    assert "1e-15" in artifact
    with postgres_engine.begin() as connection:
        _insert_parent_model(
            connection,
            model_hash=model_hash,
            artifact_json=artifact,
        )
    with postgres_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT artifact_json FROM prematch_model_runs WHERE run_id=:run_id"),
            {"run_id": model_hash},
        ).scalar_one()
    assert stored == artifact

    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            _insert_parent_model(
                connection,
                model_hash="e" * 64,
                artifact_json="[]",
            )


def test_corpus_rows_preserve_application_canonical_float_text(
    postgres_engine: Engine,
) -> None:
    row_json = canonical_json_bytes(
        {
            "availability_mode": MODE,
            "team_base_logit": 0.01617391412884206,
        }
    ).decode()
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO prematch_training_corpus_rows
                       (row_hash, model_kind, row_json, created_at)
                     VALUES (:row_hash, 'team_plus_draft_rosh', :row_json,
                             '2026-08-06T00:00:00+00:00')"""
            ),
            {"row_hash": "f" * 64, "row_json": row_json},
        )
    with postgres_engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT row_json FROM prematch_training_corpus_rows "
                "WHERE row_hash=:row_hash"
            ),
            {"row_hash": "f" * 64},
        ).scalar_one()
    assert stored == row_json

    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """INSERT INTO prematch_training_corpus_rows
                           (row_hash, model_kind, row_json, created_at)
                         VALUES (:row_hash, 'team_only', '[]',
                                 '2026-08-06T00:00:00+00:00')"""
                ),
                {"row_hash": "1" * 64},
            )


def test_unsupported_calibration_preserves_null_fit_cutoff_and_reconstructed_cannot_pass(
    postgres_engine: Engine,
) -> None:
    artifact = build_prematch_calibration_artifact(
        (),
        datetime(2026, 2, 1, tzinfo=timezone.utc),
        model_kind="team_only",
        availability_mode=MODE,
    )
    record = build_prematch_calibration_record(artifact, model_hash="a" * 64)
    with postgres_engine.begin() as connection:
        _insert_parent_model(connection)
        connection.execute(
            text(
                """
                INSERT INTO prematch_calibration_artifacts (
                    calibration_key, model_kind, model_hash,
                    calibration_version, fit_cutoff, evaluation_cutoff,
                    fit_support, evaluation_support, parameters_json,
                    metrics_json, input_hash, calibration_hash,
                    artifact_json, status, created_at
                ) VALUES (
                    :key, :kind, :model_hash, :version, :fit_cutoff,
                    :evaluation_cutoff, :fit_support, :evaluation_support,
                    :parameters_json, :metrics_json, :input_hash,
                    :calibration_hash, :artifact_json, :status,
                    '2026-08-05T00:00:00+00:00'
                )
                """
            ),
            {
                "key": record.calibration_key,
                "kind": record.model_kind,
                "model_hash": record.model_hash,
                "version": record.calibration_version,
                "fit_cutoff": None,
                "evaluation_cutoff": record.evaluation_cutoff.isoformat(),
                "fit_support": record.fit_support,
                "evaluation_support": record.evaluation_support,
                "parameters_json": None,
                "metrics_json": record.metrics_json,
                "input_hash": record.input_hash,
                "calibration_hash": record.calibration_hash,
                "artifact_json": record.artifact_json,
                "status": record.status,
            },
        )
    with postgres_engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT fit_cutoff, parameters_json, status "
                "FROM prematch_calibration_artifacts"
            )
        ).one()
    assert tuple(stored) == (None, None, "unsupported")

    payload = deepcopy(artifact.to_payload())
    payload["status"] = "passed"
    payload["fit_cutoff"] = "2026-01-15T00:00:00+00:00"
    payload["calibration_hash"] = "d" * 64
    with pytest.raises(DBAPIError, match="reconstructed calibration"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO prematch_calibration_artifacts (
                        calibration_key, model_kind, model_hash,
                        calibration_version, fit_cutoff, evaluation_cutoff,
                        fit_support, evaluation_support, parameters_json,
                        metrics_json, input_hash, calibration_hash,
                        artifact_json, status, created_at
                        ) VALUES (
                            :key, 'team_only', :model_hash,
                            :version, :fit_cutoff, :evaluation_cutoff,
                            20, 100, :parameters_json, :metrics_json, :input_hash,
                            :calibration_hash, :artifact_json, 'passed',
                            '2026-08-05T00:00:00+00:00'
                    )
                    """
                ),
                {
                    "key": "e" * 64,
                    "model_hash": "a" * 64,
                    "version": payload["calibration_version"],
                    "fit_cutoff": payload["fit_cutoff"],
                    "evaluation_cutoff": payload["calibration_cutoff"],
                    "parameters_json": '{"a":0.0,"b":1.0}',
                    "metrics_json": "{}",
                    "input_hash": payload["input_hash"],
                    "calibration_hash": payload["calibration_hash"],
                    "artifact_json": canonical_json_bytes(payload).decode(),
                },
            )


def test_calibration_parameters_and_mode_must_match_artifact_claims(
    postgres_engine: Engine,
) -> None:
    artifact = build_prematch_calibration_artifact(
        (),
        datetime(2026, 2, 1, tzinfo=timezone.utc),
        model_kind="team_only",
        availability_mode=MODE,
    )
    base_payload = deepcopy(artifact.to_payload())
    base_payload.update(
        {
            "status": "provisional",
            "fit_cutoff": "2026-01-15T00:00:00+00:00",
            "parameters": {"a": 0.0, "b": 1.0},
            "calibration_hash": "d" * 64,
        }
    )
    statement = text(
        """
        INSERT INTO prematch_calibration_artifacts (
            calibration_key, model_kind, model_hash,
            calibration_version, fit_cutoff, evaluation_cutoff,
            fit_support, evaluation_support, parameters_json,
            metrics_json, input_hash, calibration_hash,
            artifact_json, status, created_at
        ) VALUES (
            :key, 'team_only', :model_hash, :version,
            :fit_cutoff, :evaluation_cutoff, 0, 0, :parameters_json,
            '{}', :input_hash, :calibration_hash, :artifact_json,
            'provisional', '2026-08-05T00:00:00+00:00'
        )
        """
    )
    with postgres_engine.begin() as connection:
        _insert_parent_model(connection)

    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "key": "e" * 64,
                    "model_hash": "a" * 64,
                    "version": base_payload["calibration_version"],
                    "fit_cutoff": base_payload["fit_cutoff"],
                    "evaluation_cutoff": base_payload["calibration_cutoff"],
                    "parameters_json": '{"a":0.0,"b":2.0}',
                    "input_hash": base_payload["input_hash"],
                    "calibration_hash": base_payload["calibration_hash"],
                    "artifact_json": canonical_json_bytes(base_payload).decode(),
                },
            )

    mode_mismatch = deepcopy(base_payload)
    mode_mismatch["availability_mode"] = "prospective"
    mode_mismatch["calibration_hash"] = "f" * 64
    with pytest.raises(DBAPIError, match="availability mode disagrees"):
        with postgres_engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "key": "1" * 64,
                    "model_hash": "a" * 64,
                    "version": mode_mismatch["calibration_version"],
                    "fit_cutoff": mode_mismatch["fit_cutoff"],
                    "evaluation_cutoff": mode_mismatch["calibration_cutoff"],
                    "parameters_json": '{"a":0.0,"b":1.0}',
                    "input_hash": mode_mismatch["input_hash"],
                    "calibration_hash": mode_mismatch["calibration_hash"],
                    "artifact_json": canonical_json_bytes(mode_mismatch).decode(),
                },
            )


def test_prematch_append_only_tables_and_lineage_changes_reject_mutation(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        _insert_parent_model(connection)
    for statement in (
        "UPDATE prematch_model_runs SET status='insufficient_evidence'",
        "DELETE FROM prematch_lineage_changes",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            with postgres_engine.begin() as connection:
                connection.execute(text(statement))


def test_prematch_migrations_downgrade_and_reupgrade(postgres_engine: Engine) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        postgres_engine.url.render_as_string(hide_password=False),
    )
    postgres_engine.dispose()
    command.downgrade(config, "20260805_0022")
    assert not (PREMATCH_TABLES & set(inspect(postgres_engine).get_table_names()))
    command.upgrade(config, "head")
    assert PREMATCH_TABLES <= set(inspect(postgres_engine).get_table_names())
