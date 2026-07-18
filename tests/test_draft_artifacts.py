from __future__ import annotations

import json
import hashlib
import math
import random
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pytest

import event_intelligence.draft_artifacts as draft_artifacts
import event_intelligence.draft_model as draft_model
from event_intelligence.draft_artifacts import (
    CalibrationSample,
    assert_model_artifact_deployable,
    build_calibration_artifact,
    calibration_artifact_from_payload,
    canonical_hash,
    load_calibration_artifact_json,
    load_model_artifact_json,
    model_artifact_from_payload,
)
from event_intelligence.draft_model import (
    MODEL_ARTIFACT_VERSION,
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


def _resign_model(payload: dict) -> None:
    unsigned = deepcopy(payload)
    unsigned.pop("model_hash", None)
    payload["model_hash"] = canonical_hash(unsigned)


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


@lru_cache(maxsize=1)
def _passing_calibration_artifact():
    model = _model()
    fit = _calibration_samples(
        prefix="strict-fit",
        start=CUTOFF + timedelta(days=1),
        repeats=40,
    )
    evaluation = _calibration_samples(
        prefix="strict-evaluation",
        start=max(row.settled_at for row in fit) + timedelta(days=1),
        repeats=20,
    )
    return build_calibration_artifact(
        model,
        evidence_mode="prospective",
        source_ref="prospective-outcomes:strict-json-v1",
        fit_samples=fit,
        evaluation_samples=evaluation,
    )


def test_model_artifact_round_trip_rehydrates_every_parameter() -> None:
    model = _model()

    loaded = model_artifact_from_payload(model.to_payload())

    assert loaded == model
    assert loaded.artifact_version == MODEL_ARTIFACT_VERSION
    assert not loaded.audit_only
    assert loaded.has_replayable_training_corpus
    assert len(loaded.training_corpus) == loaded.support


def test_model_artifact_rejects_parameter_tampering() -> None:
    payload = _model().to_payload()
    payload["intercept"] = float(payload["intercept"]) + 0.01

    with pytest.raises(ValueError, match="hash"):
        model_artifact_from_payload(payload)


@pytest.mark.parametrize("field", ["coefficients", "support"])
def test_model_artifact_replay_rejects_resigned_derived_claims(field: str) -> None:
    payload = _model().to_payload()
    if field == "coefficients":
        payload["coefficients"]["hero_edge"] += 0.01
    else:
        payload["support"] += 1
    _resign_model(payload)

    with pytest.raises(ValueError, match="corpus|support"):
        model_artifact_from_payload(payload)


@pytest.mark.parametrize("operation", ["remove", "add"])
def test_model_artifact_rejects_resigned_training_row_changes(
    operation: str,
) -> None:
    payload = _model().to_payload()
    if operation == "remove":
        payload["training_corpus"].pop()
    else:
        added = deepcopy(payload["training_corpus"][-1])
        added["match_id"] += 100_000
        added["input_snapshot_hash"] = "f" * 64
        payload["training_corpus"].append(added)
    _resign_model(payload)

    with pytest.raises(ValueError, match="corpus|support"):
        model_artifact_from_payload(payload)


def test_model_artifact_rejects_resigned_forged_corpus_and_input_hash() -> None:
    original = _model()
    forged_rows = tuple(
        replace(
            row.to_training_row(),
            features={"hero_edge": 50.0},
        )
        if index == 0
        else row.to_training_row()
        for index, row in enumerate(original.training_corpus)
    )
    forged = fit_draft_model(
        forged_rows,
        FeatureSchema.from_names(("hero_edge",)),
        CUTOFF,
        10,
    )
    payload = original.to_payload()
    payload["training_corpus"] = forged.to_payload()["training_corpus"]
    payload["training_input_hash"] = forged.training_input_hash
    _resign_model(payload)

    with pytest.raises(ValueError, match="does not replay"):
        model_artifact_from_payload(payload)


def test_legacy_model_artifact_is_audit_only_and_cannot_deploy() -> None:
    payload = _model().to_payload()
    payload.pop("artifact_version")
    payload.pop("trainer_runtime")
    payload.pop("training_corpus")
    _resign_model(payload)

    loaded = model_artifact_from_payload(payload)

    assert loaded.audit_only
    assert not loaded.has_replayable_training_corpus
    with pytest.raises(ValueError, match="audit-only"):
        assert_model_artifact_deployable(loaded)


def test_model_artifact_rejects_unknown_top_level_and_corpus_keys() -> None:
    top_level = _model().to_payload()
    top_level["unknown"] = True
    corpus = _model().to_payload()
    corpus["training_corpus"][0]["unknown"] = True

    with pytest.raises(ValueError, match="unknown"):
        model_artifact_from_payload(top_level)
    with pytest.raises(ValueError, match="unknown"):
        model_artifact_from_payload(corpus)


def test_model_json_loader_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    payload = _model().to_payload()
    raw = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    duplicate = raw.replace(
        f'"support":{payload["support"]}',
        f'"support":{payload["support"]},"support":{payload["support"]}',
        1,
    )
    payload["intercept"] = float("nan")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_model_artifact_json(duplicate)
    with pytest.raises(ValueError, match="invalid JSON constant"):
        load_model_artifact_json(json.dumps(payload, allow_nan=True))


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


def test_calibration_artifact_rejects_unknown_keys_at_every_nested_level() -> None:
    mutations = (
        lambda payload: payload.__setitem__("unknown", True),
        lambda payload: payload["fit_samples"][0].__setitem__("unknown", True),
        lambda payload: payload["metrics"].__setitem__("unknown", True),
        lambda payload: payload["metrics"]["calibration_bins"][0].__setitem__(
            "unknown", True
        ),
        lambda payload: payload["gate"].__setitem__("unknown", True),
    )
    for mutate in mutations:
        payload = deepcopy(_passing_calibration_artifact().to_payload())
        mutate(payload)
        with pytest.raises(ValueError, match="unknown"):
            calibration_artifact_from_payload(payload)


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), float("-inf")])
def test_calibration_json_loader_rejects_nonfinite_numbers(constant: float) -> None:
    payload = _passing_calibration_artifact().to_payload()
    payload["slope"] = constant

    with pytest.raises(ValueError, match="invalid JSON constant"):
        load_calibration_artifact_json(json.dumps(payload, allow_nan=True))


def test_calibration_json_loader_rejects_duplicate_keys() -> None:
    payload = _passing_calibration_artifact().to_payload()
    raw = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    duplicate = raw.replace(
        f'"method":"{payload["method"]}"',
        f'"method":"{payload["method"]}","method":"{payload["method"]}"',
        1,
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_calibration_artifact_json(duplicate)


def test_bootstrap_ece_fast_path_is_bit_exact_to_full_metric_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort_generator = random.Random(20260717)
    samples = tuple(
        CalibrationSample(
            sample_id=f"bootstrap-equivalence-{index}",
            probability=cohort_generator.choice(
                (0.02, 0.1, 0.25, 0.5, 0.8, 0.95)
            ),
            outcome=cohort_generator.randrange(2),
            observed_at=CUTOFF + timedelta(days=1, minutes=index),
            settled_at=CUTOFF + timedelta(days=1, minutes=index, seconds=30),
            cluster_id=f"series-{cohort_generator.randrange(11)}",
            event_id=f"event-{cohort_generator.randrange(4)}",
        )
        for index in range(47)
    )
    probabilities = tuple(
        cohort_generator.choice((0.01, 0.08, 0.2, 0.51, 0.73, 0.91, 0.99))
        for _row in samples
    )
    seed_material = "bootstrap-equivalence-seed"
    monkeypatch.setattr(draft_artifacts, "CALIBRATION_BOOTSTRAP_SAMPLES", 73)

    grouped: dict[str, list[tuple[int, float]]] = {}
    for sample, probability in zip(samples, probabilities, strict=True):
        grouped.setdefault(sample.cluster_id, []).append(
            (sample.outcome, probability)
        )
    keys = sorted(grouped)
    generator = random.Random(
        int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    )
    estimates = []
    for _ in range(draft_artifacts.CALIBRATION_BOOTSTRAP_SAMPLES):
        selected = [
            row
            for _key in keys
            for row in grouped[keys[generator.randrange(len(keys))]]
        ]
        metrics = draft_model.evaluate_binary_predictions(
            (row[0] for row in selected),
            (row[1] for row in selected),
            ece_bins=draft_model.DEFAULT_ECE_BINS,
        )
        estimates.append(metrics.expected_calibration_error)
    estimates.sort()
    expected = estimates[math.ceil(0.90 * len(estimates)) - 1]

    actual = draft_artifacts._bootstrap_ece_upper(
        samples,
        probabilities,
        seed_material=seed_material,
    )

    assert actual == expected


def test_large_artifact_verification_caches_evict_beyond_two_bundles() -> None:
    expected_size = len(draft_model.LANDMARK_MINUTES) * 2
    model_verifier = draft_model._verify_model_artifact
    calibration_verifier = draft_artifacts._verify_calibration_artifact
    model_verifier.cache_clear()
    calibration_verifier.cache_clear()
    try:
        base = _model()
        rows = tuple(row.to_training_row() for row in base.training_corpus)
        schema = FeatureSchema.from_names(base.feature_names)
        models = tuple(
            fit_draft_model(
                rows,
                schema,
                CUTOFF + timedelta(seconds=index),
                10,
            )
            for index in range(expected_size + 1)
        )
        for model in models:
            model_verifier(model)
        model_info = model_verifier.cache_info()
        assert model_info.maxsize == expected_size
        assert model_info.currsize == expected_size
        model_verifier(models[0])
        assert model_verifier.cache_info().misses == model_info.misses + 1

        calibrations = tuple(
            build_calibration_artifact(
                base,
                evidence_mode="prospective",
                source_ref=f"cache-eviction:{index}",
                fit_samples=(),
                evaluation_samples=(),
            )
            for index in range(expected_size + 1)
        )
        for calibration in calibrations:
            calibration_verifier(calibration)
        calibration_info = calibration_verifier.cache_info()
        assert calibration_info.maxsize == expected_size
        assert calibration_info.currsize == expected_size
        calibration_verifier(calibrations[0])
        assert (
            calibration_verifier.cache_info().misses
            == calibration_info.misses + 1
        )

        with pytest.raises(ValueError, match="slope"):
            calibration_verifier(
                replace(calibrations[-1], slope=calibrations[-1].slope + 1.0)
            )
    finally:
        model_verifier.cache_clear()
        calibration_verifier.cache_clear()
