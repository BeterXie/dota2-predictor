from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from event_intelligence.prematch_storage import PREMATCH_VALIDATION_VERSION
from web import prematch_predictions as prematch_api
from web import queries
from web.app import app


MODEL_HASH = "a" * 64
FEATURE_HASH = "b" * 64
TRAINING_HASH = "c" * 64
INPUT_HASH = "d" * 64
ARTIFACT_HASH = "e" * 64
DEPENDENCY_HASH = "f" * 64
CALIBRATION_HASH = "1" * 64
UTC = "2026-08-01T00:00:00+00:00"


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return None if not self.rows else self.rows[0]

    def fetchall(self):
        return list(self.rows)


class _ReadOnlySession:
    def __init__(
        self,
        *,
        models=(),
        predictions=(),
        match_ids=(),
        lineage_revision=7,
    ):
        self.models = list(models)
        self.predictions = list(predictions)
        self.match_ids = set(match_ids)
        self.lineage_revision = lineage_revision
        self.statements: list[str] = []
        self.closed = False

    @contextmanager
    def transaction(self):
        yield

    def execute(self, statement, params=()):
        normalized = " ".join(statement.split())
        self.statements.append(normalized)
        if normalized == "SET TRANSACTION READ ONLY":
            return _Result(())
        if normalized.startswith(
            "SELECT COUNT(*) AS total FROM prematch_model_runs AS run"
        ):
            return _Result(({"total": len(self.models)},))
        if normalized.startswith("SELECT COUNT(*) AS total"):
            return _Result(({"total": len(self.predictions)},))
        if normalized.startswith(
            "SELECT 1 AS present FROM match_ingest_status WHERE match_id="
        ):
            return _Result(({"present": 1},) if params[0] in self.match_ids else ())
        if normalized.startswith(
            "SELECT dependency_revision, artifact_revision "
            "FROM prematch_lineage_revisions"
        ):
            return _Result(
                (
                    {
                        "dependency_revision": self.lineage_revision,
                        "artifact_revision": 1,
                    },
                )
            )
        if "FROM prematch_calibration_artifacts" in normalized:
            return _Result(self.models)
        if "FROM prematch_predictions AS prediction" in normalized:
            return _Result(self.predictions)
        raise AssertionError(f"unexpected SQL: {normalized}")

    def close(self):
        self.closed = True


def _model_row(**overrides):
    row = {
        "run_id": MODEL_HASH,
        "model_hash": MODEL_HASH,
        "model_version": "prematch-offset-logistic-l2-v1",
        "artifact_version": "prematch-model-artifact-v1",
        "model_kind": "team_plus_draft_rosh",
        "availability_mode": "reconstructed_walk_forward",
        "training_cutoff": UTC,
        "feature_schema_hash": FEATURE_HASH,
        "training_input_hash": TRAINING_HASH,
        "metrics_json": json.dumps({"support": 606}),
        "status": "trained",
        "created_at": UTC,
        "calibration_hash": CALIBRATION_HASH,
        "calibration_version": "prematch-platt-calibration-v1",
        "calibration_fit_cutoff": UTC,
        "calibration_evaluation_cutoff": UTC,
        "calibration_fit_support": 40,
        "calibration_evaluation_support": 566,
        "calibration_parameters_json": json.dumps({"a": 0.1, "b": 0.9}),
        "calibration_metrics_json": json.dumps(
            {
                "gate_passed": False,
                "gate_reasons": ["calibration_gate_failed"],
            }
        ),
        "calibration_status": "failed",
        "calibration_created_at": UTC,
    }
    row.update(overrides)
    return row


def _prediction_row():
    prediction = {
        "status": "predicted",
        "reason": None,
        "raw_probability": 0.6,
        "parameter_uncertainty": 0.1,
        "team_base_logit": 0.0,
        "learned_intercept": 0.05,
        "draft_logit_delta": 0.02,
        "rosh_logit_delta": 0.03,
        "cluster_logit_delta": None,
        "total_adjustment": 0.1,
        "support": 40,
        "model_hash": MODEL_HASH,
        "input_snapshot_hash": INPUT_HASH,
        "missing_features": ["rosh_curve_range"],
        "top_contributions": [
            {
                "feature_name": "relative_advantage",
                "component": "rosh",
                "input_value": 0.4,
                "standardized_value": 0.5,
                "coefficient": 0.06,
                "log_odds_contribution": 0.03,
                "was_imputed": False,
            }
        ],
    }
    return {
        "run_id": MODEL_HASH,
        "model_hash": MODEL_HASH,
        "model_kind": "team_plus_draft_rosh",
        "model_status": "trained",
        "availability_mode": "reconstructed_walk_forward",
        "training_cutoff": UTC,
        "match_id": 42,
        "prediction_cutoff": UTC,
        "cutoff_source": "draft_complete",
        "input_snapshot_hash": INPUT_HASH,
        "artifact_fingerprint": ARTIFACT_HASH,
        "dependency_fingerprint": DEPENDENCY_HASH,
        "dependency_revision": 7,
        "calibration_hash": CALIBRATION_HASH,
        "team_base_probability": 0.5,
        "raw_probability": 0.6,
        "calibrated_probability": 0.58,
        "parameter_uncertainty": 0.1,
        "draft_logit_delta": 0.02,
        "rosh_logit_delta": 0.03,
        "cluster_logit_delta": None,
        "total_adjustment": 0.1,
        "coverage": 0.8,
        "support": 40,
        "prediction_json": json.dumps(prediction),
        "eventual_radiant_win": None,
        "result_usable_at": None,
        "settled_at": None,
        "status": "predicted",
        "validation_version": PREMATCH_VALIDATION_VERSION,
        "validated_at": UTC,
        "calibration_authority_hash": CALIBRATION_HASH,
        "calibration_status": "provisional",
        "calibration_evaluation_support": 99,
        "calibration_metrics_json": json.dumps({"gate_passed": False}),
    }


def _cluster_metadata():
    def assignment(hero_id, cluster_id):
        return {
            "hero_id": hero_id,
            "expected_role": f"position_{hero_id if hero_id <= 5 else hero_id - 5}",
            "expected_lane": "safe" if hero_id in {1, 5, 6, 10} else "mid",
            "cluster_id": cluster_id,
            "mapping_support": 50,
            "mapping_confidence": 0.9,
            "assignment_source": "hero_role_lane",
            "missing_reason": None,
            "evidence_ids": [f"mapping-{hero_id}"],
            "coverage": 1.0,
        }

    return {
        "cluster_artifact_hash": "2" * 64,
        "cluster_feature_snapshot_hash": "3" * 64,
        "cluster_feature_schema_hash": "4" * 64,
        "cluster_resource_version": "noxville-clusters-7.41-v1",
        "cluster_resource_hash": "5" * 64,
        "cluster_evidence_mode": "reconstructed_walk_forward",
        "cluster_coverage": 1.0,
        "cluster_support": 500,
        "cluster_missing_reason": None,
        "cluster_counts": {
            f"C{index}": {
                "radiant": float(index < 5),
                "dire": float(index >= 5),
                "difference": 1.0 if index < 5 else -1.0,
            }
            for index in range(10)
        },
        "cluster_assignments": {
            "radiant": [assignment(index + 1, f"C{index}") for index in range(5)],
            "dire": [assignment(index + 6, f"C{index + 5}") for index in range(5)],
        },
        "top_cluster_contributions": [
            {
                "feature_name": "cluster__cross_team_cluster_edge",
                "component": "cluster",
                "input_value": 0.4,
                "standardized_value": 0.5,
                "coefficient": 0.08,
                "log_odds_contribution": 0.04,
                "was_imputed": False,
            }
        ],
    }


def _install_session(monkeypatch, **kwargs):
    sessions: list[_ReadOnlySession] = []
    monkeypatch.delenv("PREMATCH_DEPLOYMENT_PATH", raising=False)

    def factory():
        session = _ReadOnlySession(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(queries, "get_db", factory)
    return sessions


def test_prematch_routes_serialize_only_current_authoritative_rows(monkeypatch) -> None:
    sessions = _install_session(
        monkeypatch,
        models=(_model_row(),),
        predictions=(_prediction_row(),),
        match_ids=(42,),
    )

    with TestClient(app) as client:
        models = client.get("/api/intelligence/prematch/models")
        predictions = client.get("/api/intelligence/prematch/predictions")
        match = client.get("/api/intelligence/prematch/matches/42")

    assert models.status_code == 200
    assert models.json()["pagination"] == {
        "page": 1,
        "page_size": 20,
        "total": 1,
        "total_pages": 1,
    }
    model = models.json()["data"][0]
    assert model["model_hash"] == MODEL_HASH
    assert model["calibration"]["status"] == "failed"
    assert model["calibration"]["gate_passed"] is False
    assert model["calibration"]["metrics"]["gate_passed"] is False
    assert model["runtime_ready"] is False
    assert model["runtime_block_reason"] == "deployment_not_configured"
    assert model["deployment_key"] is None

    assert predictions.status_code == 200
    prediction = predictions.json()["data"][0]
    assert prediction["team_base_probability"] == 0.5
    assert prediction["draft_logit_delta"] == 0.02
    assert prediction["rosh_logit_delta"] == 0.03
    assert prediction["raw_probability"] == 0.6
    assert prediction["calibrated_probability"] == 0.58
    assert prediction["calibration_status"] == "provisional"
    assert prediction["parameter_uncertainty"] == 0.1
    assert prediction["support"] == 40
    assert prediction["cluster_coverage"] == 0.0
    assert prediction["cluster_support"] == 0
    assert prediction["cluster_resource_version"] is None
    assert prediction["cluster_missing_reason"] == "cluster_evidence_unavailable"
    assert prediction["cluster_counts"] == {}
    assert prediction["cluster_assignments"] == {"radiant": [], "dire": []}
    assert prediction["top_cluster_contributions"] == []
    assert prediction["status"] == "predicted"
    assert prediction["availability_mode"] == "reconstructed_walk_forward"
    assert prediction["validation"]["validation_version"] == PREMATCH_VALIDATION_VERSION
    assert prediction["runtime_ready"] is False
    assert prediction["runtime_block_reason"] == "deployment_not_configured"
    assert prediction["deployment_key"] is None

    assert match.status_code == 200
    assert match.json()["match_id"] == 42
    assert match.json()["predictions"] == [prediction]

    statements = [statement for session in sessions for statement in session.statements]
    assert statements
    assert all(
        statement.startswith("SELECT") or statement == "SET TRANSACTION READ ONLY"
        for statement in statements
    )
    prediction_sql = " ".join(
        statement
        for statement in statements
        if "FROM prematch_predictions AS prediction" in statement
    )
    assert "prematch_lineage_revision_is_current" in prediction_sql
    assert "validation.dependency_revision=prediction.dependency_revision" in prediction_sql
    assert all(session.closed for session in sessions)


def test_prematch_routes_expose_cluster_analysis_from_prediction_artifact(
    monkeypatch,
) -> None:
    prediction_row = _prediction_row()
    artifact = json.loads(prediction_row["prediction_json"])
    artifact.update(_cluster_metadata())
    artifact["cluster_logit_delta"] = 0.04
    prediction_row.update(
        model_kind="team_plus_draft_rosh_clusters",
        cluster_logit_delta=0.04,
        prediction_json=json.dumps(artifact),
    )
    _install_session(
        monkeypatch,
        models=(_model_row(model_kind="team_plus_draft_rosh_clusters"),),
        predictions=(prediction_row,),
        match_ids=(42,),
    )

    with TestClient(app) as client:
        models = client.get(
            "/api/intelligence/prematch/models",
            params={"model_kind": "team_plus_draft_rosh_clusters"},
        )
        predictions = client.get("/api/intelligence/prematch/predictions")
        match = client.get("/api/intelligence/prematch/matches/42")

    assert models.status_code == 200
    assert models.json()["data"][0]["model_kind"] == "team_plus_draft_rosh_clusters"
    payload = predictions.json()["data"][0]
    assert payload["cluster_logit_delta"] == 0.04
    assert payload["cluster_coverage"] == 1.0
    assert payload["cluster_support"] == 500
    assert payload["cluster_resource_version"] == "noxville-clusters-7.41-v1"
    assert payload["cluster_evidence_mode"] == "reconstructed_walk_forward"
    assert payload["cluster_missing_reason"] is None
    assert payload["cluster_counts"]["C0"] == {
        "radiant": 1.0,
        "dire": 0.0,
        "difference": 1.0,
    }
    assert payload["cluster_assignments"]["dire"][0]["cluster_id"] == "C5"
    assert payload["top_cluster_contributions"][0]["component"] == "cluster"
    assert match.json()["predictions"][0] == payload


def test_prematch_predictions_fail_closed_on_partial_cluster_metadata(
    monkeypatch,
) -> None:
    prediction_row = _prediction_row()
    artifact = json.loads(prediction_row["prediction_json"])
    artifact.update(_cluster_metadata())
    artifact.pop("cluster_counts")
    prediction_row["prediction_json"] = json.dumps(artifact)
    _install_session(monkeypatch, predictions=(prediction_row,))

    with TestClient(app) as client:
        response = client.get("/api/intelligence/prematch/predictions")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Prematch prediction authority is unavailable"
    }


def test_shadow_cluster_delta_is_visible_without_replacing_default_model(
    monkeypatch,
) -> None:
    prediction_row = _prediction_row()
    artifact = json.loads(prediction_row["prediction_json"])
    artifact.update(_cluster_metadata())
    artifact["cluster_candidate_logit_delta"] = 0.07
    prediction_row["prediction_json"] = json.dumps(artifact)
    _install_session(monkeypatch, predictions=(prediction_row,))

    with TestClient(app) as client:
        response = client.get("/api/intelligence/prematch/predictions")

    assert response.status_code == 200
    payload = response.json()["data"][0]
    assert payload["model_kind"] == "team_plus_draft_rosh"
    assert payload["cluster_logit_delta"] == 0.07


def test_prematch_routes_have_stable_empty_and_missing_results(monkeypatch) -> None:
    _install_session(monkeypatch, match_ids=(42,))

    with TestClient(app) as client:
        models = client.get("/api/intelligence/prematch/models")
        predictions = client.get("/api/intelligence/prematch/predictions")
        empty_match = client.get("/api/intelligence/prematch/matches/42")
        missing_match = client.get("/api/intelligence/prematch/matches/99")

    assert models.json()["data"] == []
    assert models.json()["pagination"]["total"] == 0
    assert predictions.json()["data"] == []
    assert predictions.json()["pagination"]["total_pages"] == 0
    assert empty_match.status_code == 200
    assert empty_match.json() == {"match_id": 42, "predictions": []}
    assert missing_match.status_code == 404
    assert missing_match.json() == {"detail": "Prematch match not found"}


def test_prematch_routes_reject_invalid_parameters_before_database_access(
    monkeypatch,
) -> None:
    def unexpected_database_access():
        raise AssertionError("invalid requests must not query PostgreSQL")

    monkeypatch.setattr(queries, "get_db", unexpected_database_access)
    with TestClient(app) as client:
        responses = (
            client.get("/api/intelligence/prematch/models", params={"page": 0}),
            client.get(
                "/api/intelligence/prematch/models",
                params={"model_kind": "unknown"},
            ),
            client.get(
                "/api/intelligence/prematch/predictions",
                params={"page_size": 101},
            ),
            client.get(
                "/api/intelligence/prematch/predictions",
                params={"status": "unknown"},
            ),
            client.get("/api/intelligence/prematch/matches/0"),
        )

    assert {response.status_code for response in responses} == {422}


def test_prematch_models_fail_closed_on_inconsistent_identity(monkeypatch) -> None:
    _install_session(monkeypatch, models=(_model_row(model_hash="9" * 64),))

    with TestClient(app) as client:
        response = client.get("/api/intelligence/prematch/models")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Prematch prediction authority is unavailable"
    }


def test_prematch_predictions_fail_closed_without_calibration_authority(
    monkeypatch,
) -> None:
    prediction = _prediction_row()
    prediction["calibration_authority_hash"] = None
    _install_session(monkeypatch, predictions=(prediction,))

    with TestClient(app) as client:
        response = client.get("/api/intelligence/prematch/predictions")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Prematch prediction authority is unavailable"
    }


@pytest.mark.parametrize(
    ("status", "gate_passed", "evaluation_support"),
    (
        ("unsupported", False, 0),
        ("failed", False, 40),
        ("reconstructed_only", False, 100),
    ),
)
def test_prematch_predictions_report_blocked_calibration_as_not_runtime_ready(
    monkeypatch,
    status,
    gate_passed,
    evaluation_support,
) -> None:
    prediction = _prediction_row()
    prediction["calibration_status"] = status
    prediction["calibration_evaluation_support"] = evaluation_support
    prediction["calibration_metrics_json"] = json.dumps(
        {"gate_passed": gate_passed}
    )
    _install_session(monkeypatch, predictions=(prediction,))

    with TestClient(app) as client:
        response = client.get("/api/intelligence/prematch/predictions")

    assert response.status_code == 200
    prediction_payload = response.json()["data"][0]
    assert prediction_payload["calibration_status"] == status
    assert prediction_payload["runtime_ready"] is False
    assert prediction_payload["runtime_block_reason"] == "deployment_not_configured"


@pytest.mark.parametrize(
    ("runtime_error", "expected_ready"),
    ((None, True), ("prematch deployment dependency revision is stale", False)),
)
def test_prematch_routes_expose_automatically_checked_runtime_deployment(
    monkeypatch,
    tmp_path,
    runtime_error,
    expected_ready,
) -> None:
    model_row = _model_row(
        availability_mode="prospective",
        calibration_status="passed",
        calibration_metrics_json=json.dumps({"gate_passed": True}),
    )
    prediction_row = _prediction_row()
    prediction_row.update(
        availability_mode="prospective",
        calibration_status="passed",
        calibration_evaluation_support=100,
        calibration_metrics_json=json.dumps({"gate_passed": True}),
    )
    _install_session(
        monkeypatch,
        models=(model_row,),
        predictions=(prediction_row,),
        match_ids=(42,),
    )
    deployment_path = tmp_path / "prematch-deployment.json"
    deployment_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PREMATCH_DEPLOYMENT_PATH", str(deployment_path))
    deployment = SimpleNamespace(
        deployment_key="9" * 64,
        training_cutoff=datetime.fromisoformat(UTC),
        availability_mode="prospective",
        dependency_fingerprint=DEPENDENCY_HASH,
        dependency_revision=7,
        prematch_model_artifact=SimpleNamespace(model_hash=MODEL_HASH),
        calibration_artifact=SimpleNamespace(calibration_hash=CALIBRATION_HASH),
        feature_snapshot=SimpleNamespace(match_id=42, input_hash=INPUT_HASH),
    )
    monkeypatch.setattr(
        prematch_api,
        "load_frozen_prematch_deployment_json",
        lambda _payload: deployment,
    )
    checked_revisions: list[int] = []

    def assert_ready(_deployment, *, current_dependency_revision):
        assert _deployment is deployment
        checked_revisions.append(current_dependency_revision)
        if runtime_error is not None:
            raise RuntimeError(runtime_error)

    monkeypatch.setattr(
        prematch_api,
        "assert_frozen_prematch_deployment_deployable",
        assert_ready,
    )

    with TestClient(app) as client:
        models = client.get("/api/intelligence/prematch/models")
        predictions = client.get("/api/intelligence/prematch/predictions")
        match = client.get("/api/intelligence/prematch/matches/42")

    assert checked_revisions == [7, 7, 7]
    for payload in (
        models.json()["data"][0],
        predictions.json()["data"][0],
        match.json()["predictions"][0],
    ):
        assert payload["runtime_ready"] is expected_ready
        assert payload["runtime_block_reason"] == runtime_error
        assert payload["deployment_key"] == deployment.deployment_key
