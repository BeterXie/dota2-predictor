from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import event_intelligence.prematch_model as prematch_model
import event_intelligence.prematch_storage as prematch_storage
from database.session import DatabaseResult, DatabaseRow
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_MODEL_SCHEMA,
    DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
)
from event_intelligence.prematch_calibration import (
    PrematchCalibrationSample,
    apply_prematch_calibration,
    build_prematch_calibration_artifact,
)
from event_intelligence.prematch_features import (
    PREMATCH_FEATURE_VERSION,
    PrematchFeatureSnapshot,
)
from event_intelligence.prematch_model import PrematchTrainingRow, fit_prematch_model
from event_intelligence.prematch_storage import (
    PREMATCH_VALIDATION_VERSION,
    build_prematch_calibration_record,
    build_prematch_model_run_record,
    build_prematch_prediction_record,
    build_prematch_validation_record,
    persist_prematch_records,
    prematch_dependency_fingerprint,
)
from event_intelligence.rosh_features import (
    ROSH_FEATURE_SCHEMA,
    ROSH_MODEL_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
TRAINING_CUTOFF = START + timedelta(days=30)
TARGET_CUTOFF = TRAINING_CUTOFF + timedelta(days=1)
CREATED_AT = datetime(2026, 8, 5, tzinfo=UTC)
MODE = AvailabilityMode.RECONSTRUCTED.value


def _digest(number: int) -> str:
    return f"{number:064x}"


def _model(*, l2_regularization: float = 1.0):
    rows = tuple(
        PrematchTrainingRow(
            match_id=index + 1,
            input_snapshot_hash=_digest(index + 1),
            prediction_cutoff=START + timedelta(days=index),
            completed_at=START + timedelta(days=index, hours=1),
            result_usable_at=START + timedelta(days=index, hours=2),
            availability_mode=MODE,
            outcome=index % 2,
            series_id=f"series-{index // 2}",
            event_id="event-a",
            patch_id="7.40",
            team_base_logit=(index % 5 - 2) * 0.1,
            features={},
        )
        for index in range(24)
    )
    return fit_prematch_model(
        rows,
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=10,
        l2_regularization=l2_regularization,
    )


def _snapshot() -> PrematchFeatureSnapshot:
    draft = tuple(
        (name, 0.0 if not name.endswith("__missing") else 0.0)
        for name in DRAFT_RESIDUAL_MODEL_SCHEMA
    )
    rosh: list[tuple[str, float | None]] = []
    for name in ROSH_FEATURE_SCHEMA:
        value = 0.0 if name == "coverage" else None
        rosh.append((name, value))
        rosh.append((f"{name}__missing", 0.0 if value is not None else 1.0))
    assert tuple(name for name, _value in rosh) == ROSH_MODEL_SCHEMA
    return PrematchFeatureSnapshot(
        match_id=100,
        prediction_cutoff=TARGET_CUTOFF,
        availability_mode=MODE,
        feature_version=PREMATCH_FEATURE_VERSION,
        team_base_logit=0.2,
        team_rating_run_id=_digest(101),
        team_rating_artifact_hash=_digest(102),
        team_rating_prediction_input_hash=_digest(103),
        team_rating_combined_training_input_hash=_digest(104),
        team_rating_support=24,
        draft_residual_input_hash=_digest(105),
        draft_residual_authority_fingerprint=_digest(106),
        draft_residual_team_rating_input_hash=_digest(107),
        draft_residual_feature_schema_hash=DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        draft_residual_model_schema_hash=DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
        draft_support=20,
        draft_coverage=0.8,
        draft_features=draft,
        rosh_status="unavailable",
        rosh_missing_reason="archive_missing",
        rosh_input_hash=_digest(108),
        rosh_model_schema_hash=ROSH_MODEL_SCHEMA_HASH,
        rosh_run_id=None,
        rosh_evidence_hash=None,
        rosh_formula_version=None,
        rosh_profile_hash=None,
        rosh_result_hash=None,
        rosh_coverage=0.0,
        rosh_features=tuple(rosh),
    )


def _records():
    model = _model()
    snapshot = _snapshot()
    run = build_prematch_model_run_record(model, metrics={"brier": 0.24})
    calibration_artifact = build_prematch_calibration_artifact(
        (),
        TARGET_CUTOFF,
        model_kind=model.model_kind,
        availability_mode=model.availability_mode,
    )
    calibration = build_prematch_calibration_record(
        calibration_artifact,
        model_hash=model.model_hash,
    )
    prediction = build_prematch_prediction_record(
        model,
        snapshot,
        cutoff_source="reconstructed_map_start",
        dependency_revision=1,
    )
    validation = build_prematch_validation_record(
        run,
        prediction,
        validated_at=CREATED_AT,
    )
    return model, snapshot, run, calibration, prediction, validation


def _result(
    columns: Sequence[str], values: tuple[object, ...] | None
) -> DatabaseResult:
    rows = () if values is None else (DatabaseRow(columns, values),)
    return DatabaseResult(rows, len(rows), tuple(columns))


class _MemoryConnection:
    def __init__(self) -> None:
        self.runs: dict[str, tuple[object, ...]] = {}
        self.calibrations: dict[str, tuple[object, ...]] = {}
        self.predictions: dict[tuple[str, int], tuple[object, ...]] = {}
        self.validations: dict[tuple[str, int], tuple[object, ...]] = {}
        self.current_dependency_revisions = {1}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        before = deepcopy(
            (self.runs, self.calibrations, self.predictions, self.validations)
        )
        try:
            yield
        except BaseException:
            self.runs, self.calibrations, self.predictions, self.validations = before
            raise

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> DatabaseResult:
        sql = " ".join(statement.split())
        values = tuple(parameters)
        if sql.startswith("SELECT prematch_lineage_revision_is_current"):
            return _result(
                ("is_current",),
                (int(values[0]) in self.current_dependency_revisions,),
            )
        if sql.startswith("SELECT model_version"):
            columns = (
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
            )
            return _result(columns, self.runs.get(str(values[0])))
        if sql.startswith("INSERT INTO prematch_model_runs"):
            key = str(values[0])
            if key in self.runs:
                return _result(("run_id",), None)
            self.runs[key] = values[1:-1]
            return _result(("run_id",), (key,))
        if sql.startswith("SELECT model_kind, availability_mode, training_cutoff"):
            parent = next(
                (row for row in self.runs.values() if str(row[7]) == str(values[0])),
                None,
            )
            selected = None if parent is None else (parent[2], parent[3], parent[4])
            return _result(
                ("model_kind", "availability_mode", "training_cutoff"),
                selected,
            )
        if sql.startswith("SELECT model_kind, model_hash, calibration_version"):
            columns = (
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
            )
            return _result(columns, self.calibrations.get(str(values[0])))
        if sql.startswith("INSERT INTO prematch_calibration_artifacts"):
            key = str(values[0])
            if key in self.calibrations:
                return _result(("calibration_key",), None)
            self.calibrations[key] = values[1:-1]
            return _result(("calibration_key",), (key,))
        if sql.startswith("SELECT match_id"):
            columns = (
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
            )
            return _result(
                columns, self.predictions.get((str(values[0]), int(values[1])))
            )
        if sql.startswith("INSERT INTO prematch_predictions"):
            key = (str(values[0]), int(values[1]))
            if key in self.predictions:
                return _result(("prediction_id",), None)
            self.predictions[key] = values[1:-1]
            return _result(("prediction_id",), (len(self.predictions),))
        if sql.startswith("SELECT input_snapshot_hash"):
            columns = (
                "input_snapshot_hash",
                "artifact_fingerprint",
                "dependency_fingerprint",
                "dependency_revision",
                "validation_version",
                "validated_at",
            )
            return _result(
                columns, self.validations.get((str(values[0]), int(values[1])))
            )
        if sql.startswith("INSERT INTO prematch_prediction_validations"):
            key = (str(values[0]), int(values[1]))
            if key in self.validations:
                return _result(("run_id",), None)
            self.validations[key] = values[2:]
            return _result(("run_id",), (key[0],))
        raise AssertionError(sql)


def test_records_bind_full_replayable_artifacts_and_dependency_identity() -> None:
    model, snapshot, run, calibration, prediction, validation = _records()

    assert json.loads(run.artifact_json) == model.to_payload()
    assert calibration.fit_cutoff is None
    assert calibration.parameters_json is None
    assert json.loads(calibration.artifact_json)["oos_samples"] == []
    assert prediction.dependency_fingerprint == prematch_dependency_fingerprint(
        snapshot
    )
    assert validation.validation_version == PREMATCH_VALIDATION_VERSION

    changed = replace(snapshot, team_rating_artifact_hash=_digest(999), input_hash="")
    assert prematch_dependency_fingerprint(changed) != prediction.dependency_fingerprint


def test_prediction_record_rejects_all_derivable_payload_drift() -> None:
    _model_value, _snapshot_value, _run, _calibration, prediction, _validation = (
        _records()
    )
    payload = json.loads(prediction.prediction_json)
    payload["total_adjustment"] = float(payload["total_adjustment"] or 0.0) + 0.25
    with pytest.raises(ValueError, match="prediction artifact"):
        replace(
            prediction,
            prediction_json=prematch_storage._canonical_json(payload),  # noqa: SLF001
        )
    with pytest.raises(ValueError, match="prediction artifact"):
        replace(
            prediction,
            team_base_probability=prediction.team_base_probability + 0.01,
        )


def test_model_and_calibration_tampering_fail_before_persistence() -> None:
    model, _snapshot_value, run, calibration, _prediction, _validation = _records()
    payload = json.loads(run.artifact_json)
    payload["intercept"] = float(payload["intercept"]) + 0.1
    with pytest.raises(ValueError, match="hash|replay"):
        replace(run, artifact_json=json.dumps(payload, separators=(",", ":")))

    artifact = build_prematch_calibration_artifact(
        (),
        TARGET_CUTOFF,
        model_kind=model.model_kind,
        availability_mode=model.availability_mode,
    )
    with pytest.raises(ValueError, match="hash"):
        build_prematch_calibration_record(
            replace(artifact, calibration_hash="f" * 64),
            model_hash=model.model_hash,
        )
    assert calibration.status == "unsupported"


def test_model_run_builder_performs_one_strict_public_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model()
    strict_load = prematch_storage.load_prematch_model_artifact_json
    calls = 0

    def counted_load(payload_json: str):
        nonlocal calls
        calls += 1
        return strict_load(payload_json)

    monkeypatch.setattr(
        prematch_storage,
        "load_prematch_model_artifact_json",
        counted_load,
    )
    record = build_prematch_model_run_record(model)
    assert record.model_hash == model.model_hash
    assert calls == 1


def test_atomic_persistence_is_idempotent_and_conflicts_roll_back() -> None:
    _model_value, _snapshot_value, run, calibration, prediction, validation = _records()
    connection = _MemoryConnection()

    first = persist_prematch_records(
        connection,  # type: ignore[arg-type]
        model_runs=(run,),
        calibration_artifacts=(calibration,),
        predictions=(prediction,),
        validations=(validation,),
        created_at=CREATED_AT,
    )
    repeated = persist_prematch_records(
        connection,  # type: ignore[arg-type]
        model_runs=(run, run),
        calibration_artifacts=(calibration,),
        predictions=(prediction,),
        validations=(validation,),
        created_at=CREATED_AT + timedelta(days=1),
    )
    assert (
        first.inserted_model_runs,
        first.inserted_calibrations,
        first.inserted_predictions,
        first.inserted_validations,
    ) == (1, 1, 1, 1)
    assert (
        repeated.unchanged_model_runs,
        repeated.unchanged_calibrations,
        repeated.unchanged_predictions,
        repeated.unchanged_validations,
    ) == (1, 1, 1, 1)

    second_run = build_prematch_model_run_record(_model(l2_regularization=2.0))
    conflict = replace(prediction, dependency_fingerprint="f" * 64)
    with pytest.raises(ValueError, match="prediction conflict"):
        persist_prematch_records(
            connection,  # type: ignore[arg-type]
            model_runs=(second_run,),
            predictions=(conflict,),
            created_at=CREATED_AT,
        )
    assert second_run.run_id not in connection.runs
    assert set(connection.runs) == {run.run_id}


def test_future_only_revision_change_preserves_current_stored_revision() -> None:
    _model_value, _snapshot_value, run, _calibration, prediction, validation = (
        _records()
    )
    connection = _MemoryConnection()
    persist_prematch_records(
        connection,  # type: ignore[arg-type]
        model_runs=(run,),
        predictions=(prediction,),
        validations=(validation,),
        created_at=CREATED_AT,
    )
    connection.current_dependency_revisions = {1, 2}

    repeated = persist_prematch_records(
        connection,  # type: ignore[arg-type]
        model_runs=(run,),
        predictions=(replace(prediction, dependency_revision=2),),
        validations=(replace(validation, dependency_revision=2),),
        created_at=CREATED_AT + timedelta(days=1),
    )

    assert repeated.unchanged_predictions == 1
    assert repeated.unchanged_validations == 1
    stored_prediction = connection.predictions[(run.run_id, prediction.match_id)]
    stored_validation = connection.validations[(run.run_id, prediction.match_id)]
    assert stored_prediction[6] == 1
    assert stored_validation[3] == 1


def test_prediction_builder_rejects_claimed_dependency_drift() -> None:
    model = _model()
    with pytest.raises(ValueError, match="fingerprint"):
        build_prematch_prediction_record(
            model,
            _snapshot(),
            cutoff_source="reconstructed_map_start",
            dependency_fingerprint="f" * 64,
            dependency_revision=1,
        )


def test_prediction_builder_relies_on_predict_for_model_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model()
    fit_model = prematch_model.fit_prematch_model
    calls = 0

    def counted_fit(*args, **kwargs):
        nonlocal calls
        calls += 1
        return fit_model(*args, **kwargs)

    monkeypatch.setattr(
        prematch_model,
        "fit_prematch_model",
        counted_fit,
    )
    record = build_prematch_prediction_record(
        model,
        _snapshot(),
        cutoff_source="reconstructed_map_start",
        dependency_revision=1,
    )
    assert record.raw_probability is not None
    assert calls == 1


def test_prediction_builder_applies_supported_replayed_calibration() -> None:
    model = _model()
    snapshot = _snapshot()
    samples = tuple(
        PrematchCalibrationSample(
            match_id=1_000 + index,
            series_id=f"series-{index:03d}",
            event_id=f"event-{index % 5}",
            patch_id=f"7.4{index % 2}",
            model_kind=model.model_kind,
            availability_mode=MODE,
            prediction_cutoff=START + timedelta(hours=index),
            result_usable_at=START + timedelta(hours=index, minutes=30),
            raw_probability=0.15 if index % 2 == 0 else 0.85,
            outcome=index % 2,
            model_hash=_digest(200 + index // 10),
            input_snapshot_hash=_digest(300 + index),
        )
        for index in range(120)
    )
    artifact = build_prematch_calibration_artifact(
        samples,
        START + timedelta(hours=120, minutes=31),
        model_kind=model.model_kind,
        availability_mode=MODE,
    )
    calibration = build_prematch_calibration_record(
        artifact,
        model_hash=model.model_hash,
    )
    record = build_prematch_prediction_record(
        model,
        snapshot,
        cutoff_source="reconstructed_map_start",
        dependency_revision=1,
        calibration=calibration,
    )
    assert record.raw_probability is not None
    expected = apply_prematch_calibration(
        artifact,
        record.raw_probability,
        prediction_cutoff=snapshot.prediction_cutoff,
        availability_mode=snapshot.availability_mode,
        model_hash=model.model_hash,
        input_snapshot_hash=snapshot.input_hash,
    )
    assert record.calibration_hash == artifact.calibration_hash
    assert record.calibrated_probability == expected.calibrated_probability


def test_team_base_probability_is_stable_for_extreme_finite_logits() -> None:
    model = _model()
    snapshot = replace(_snapshot(), team_base_logit=700.0, input_hash="")
    record = build_prematch_prediction_record(
        model,
        snapshot,
        cutoff_source="reconstructed_map_start",
        dependency_revision=1,
    )
    assert math.isfinite(record.team_base_probability)
    assert 0.0 < record.team_base_probability < 1.0
