from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from event_intelligence.draft_artifacts import (
    CalibrationSample,
    build_calibration_artifact,
    calibration_artifact_from_payload,
    model_artifact_from_payload,
)
from event_intelligence.draft_model import (
    DraftTrainingRow,
    FeatureSchema,
    fit_draft_model,
)


UTC = timezone.utc
CUTOFF = datetime(2026, 7, 1, tzinfo=UTC)


def _model():
    rows = tuple(
        DraftTrainingRow(
            match_id=index + 1,
            input_snapshot_hash=f"{index + 1:064x}",
            cutoff=CUTOFF - timedelta(days=60 - index),
            completed_at=CUTOFF - timedelta(days=59 - index),
            result_usable_at=CUTOFF - timedelta(days=58 - index),
            outcome=index % 2,
            duration_minutes=45.0,
            series_id=f"series-{index // 2}",
            features={"hero_edge": -1.0 if index % 2 == 0 else 1.0},
        )
        for index in range(40)
    )
    return fit_draft_model(
        rows,
        FeatureSchema.from_names(("hero_edge",)),
        CUTOFF,
        10,
    )


def _calibration_samples(
    *,
    prefix: str,
    start: datetime,
    repeats: int,
) -> tuple[CalibrationSample, ...]:
    rows = []
    probabilities = (0.05, 0.15, 0.25, 0.75, 0.95)
    for group, probability in enumerate(probabilities):
        outcomes = round(probability * repeats)
        for index in range(repeats):
            observed = start + timedelta(minutes=len(rows))
            rows.append(
                CalibrationSample(
                    sample_id=f"{prefix}-{group}-{index}",
                    probability=probability,
                    outcome=int(index < outcomes),
                    observed_at=observed,
                    settled_at=observed + timedelta(seconds=30),
                    cluster_id=f"series-{group}-{index // 2}",
                    event_id=f"event-{group % 2}",
                )
            )
    return tuple(rows)


def test_model_artifact_round_trip_rehydrates_every_parameter() -> None:
    model = _model()

    loaded = model_artifact_from_payload(model.to_payload())

    assert loaded == model


def test_model_artifact_rejects_parameter_tampering() -> None:
    payload = _model().to_payload()
    payload["intercept"] = float(payload["intercept"]) + 0.01

    with pytest.raises(ValueError, match="hash"):
        model_artifact_from_payload(payload)


def test_calibration_artifact_round_trip_recomputes_fit_metrics_and_gate() -> None:
    model = _model()
    fit = _calibration_samples(
        prefix="fit",
        start=CUTOFF + timedelta(days=1),
        repeats=40,
    )
    evaluation = _calibration_samples(
        prefix="evaluation",
        start=max(row.settled_at for row in fit) + timedelta(days=1),
        repeats=20,
    )
    artifact = build_calibration_artifact(
        model,
        evidence_mode="prospective",
        source_ref="prospective-outcomes:v1",
        fit_samples=fit,
        evaluation_samples=evaluation,
    )

    loaded = calibration_artifact_from_payload(artifact.to_payload())

    assert loaded == artifact
    assert loaded.gate.passed
    assert loaded.passes_live_gate
    assert loaded.support == 100


def test_reconstructed_calibration_can_never_authorize_live_strategy() -> None:
    model = _model()
    fit = _calibration_samples(
        prefix="fit",
        start=CUTOFF + timedelta(days=1),
        repeats=40,
    )
    evaluation = _calibration_samples(
        prefix="evaluation",
        start=max(row.settled_at for row in fit) + timedelta(days=1),
        repeats=20,
    )
    artifact = build_calibration_artifact(
        model,
        evidence_mode="reconstructed_walk_forward",
        source_ref="strict-draft-walk-forward-v1",
        fit_samples=fit,
        evaluation_samples=evaluation,
    )

    assert artifact.gate.passed
    assert not artifact.passes_live_gate


def test_calibration_artifact_rejects_sample_or_gate_tampering() -> None:
    model = _model()
    fit = _calibration_samples(
        prefix="fit",
        start=CUTOFF + timedelta(days=1),
        repeats=40,
    )
    evaluation = _calibration_samples(
        prefix="evaluation",
        start=max(row.settled_at for row in fit) + timedelta(days=1),
        repeats=20,
    )
    artifact = build_calibration_artifact(
        model,
        evidence_mode="prospective",
        source_ref="prospective-outcomes:v1",
        fit_samples=fit,
        evaluation_samples=evaluation,
    )
    sample_tamper = deepcopy(artifact.to_payload())
    sample_tamper["evaluation_samples"][0]["outcome"] ^= 1
    gate_tamper = deepcopy(artifact.to_payload())
    gate_tamper["gate"]["passed"] = not gate_tamper["gate"]["passed"]

    with pytest.raises(ValueError):
        calibration_artifact_from_payload(sample_tamper)
    with pytest.raises(ValueError, match="gate"):
        calibration_artifact_from_payload(gate_tamper)


def test_calibration_fit_and_evaluation_cohorts_must_be_disjoint() -> None:
    model = _model()
    sample = _calibration_samples(
        prefix="same",
        start=CUTOFF + timedelta(days=1),
        repeats=4,
    )[0]

    with pytest.raises(ValueError, match="disjoint"):
        build_calibration_artifact(
            model,
            evidence_mode="prospective",
            source_ref="prospective-outcomes:v1",
            fit_samples=(sample,),
            evaluation_samples=(sample,),
        )
