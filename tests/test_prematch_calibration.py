from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest

import event_intelligence.prematch_calibration as calibration
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.draft_model import evaluate_binary_predictions
from event_intelligence.prematch_calibration import (
    CALIBRATION_MIN_EVALUATION_SUPPORT,
    CALIBRATION_MIN_FIT_SUPPORT,
    PREMATCH_CALIBRATION_VERSION,
    CalibrationStatus,
    PrematchCalibrationSample,
    _apply_prematch_calibration,
    _load_and_apply_prematch_calibration_json,
    build_prematch_calibration_artifact,
    load_prematch_calibration_artifact_json,
    replay_prematch_calibration_artifact,
)
from event_intelligence.raw_archive import canonical_json_bytes


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
MODEL_KIND = "team_only"


def _digest(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _sample(
    index: int,
    *,
    outcome: int | None = None,
    mode: str = AvailabilityMode.PROSPECTIVE.value,
    series_size: int = 1,
    prediction_cutoff: datetime | None = None,
    result_usable_at: datetime | None = None,
) -> PrematchCalibrationSample:
    series_index = index // series_size
    observed_outcome = index % 2 if outcome is None else outcome
    cutoff = prediction_cutoff or START + timedelta(days=index)
    usable = result_usable_at or cutoff + timedelta(hours=1)
    raw_probability = (
        0.12 + (index % 5) * 0.01
        if observed_outcome == 0
        else 0.82 + (index % 5) * 0.01
    )
    return PrematchCalibrationSample(
        match_id=10_000 + index,
        series_id=f"series-{series_index:04d}",
        event_id=f"event-{series_index % 5}",
        patch_id=f"7.4{series_index % 2}",
        model_kind=MODEL_KIND,
        availability_mode=mode,
        prediction_cutoff=cutoff,
        result_usable_at=usable,
        raw_probability=raw_probability,
        outcome=observed_outcome,
        model_hash=_digest(f"model-{index // 10}"),
        input_snapshot_hash=_digest(f"input-{index}"),
    )


def _samples(
    count: int,
    *,
    mode: str = AvailabilityMode.PROSPECTIVE.value,
    series_size: int = 1,
    outcome: int | None = None,
) -> tuple[PrematchCalibrationSample, ...]:
    return tuple(
        _sample(
            index,
            outcome=outcome,
            mode=mode,
            series_size=series_size,
        )
        for index in range(count)
    )


def _cutoff(count: int) -> datetime:
    return START + timedelta(days=count, hours=2)


def _artifact_hash(payload: dict[str, object]) -> str:
    value = dict(payload)
    value.pop("calibration_hash", None)
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def test_builds_chronological_series_complete_platt_artifact_and_applies_extremes() -> (
    None
):
    rows = _samples(200, series_size=2)
    artifact = build_prematch_calibration_artifact(
        reversed(rows),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert artifact.calibration_version == PREMATCH_CALIBRATION_VERSION
    assert artifact.status is CalibrationStatus.PASSED
    assert artifact.fit_support == CALIBRATION_MIN_FIT_SUPPORT == 100
    assert artifact.evaluation_support == CALIBRATION_MIN_EVALUATION_SUPPORT == 100
    assert artifact.fit_cutoff is not None
    assert artifact.evaluation_start is not None
    assert artifact.fit_cutoff < artifact.evaluation_start
    assert set(artifact.fit_series_ids).isdisjoint(artifact.evaluation_series_ids)
    assert {row.series_id for row in artifact.fit_samples} == set(
        artifact.fit_series_ids
    )
    assert {row.series_id for row in artifact.evaluation_samples} == set(
        artifact.evaluation_series_ids
    )
    assert artifact.parameters is not None
    assert artifact.raw_metrics == evaluate_binary_predictions(
        (row.outcome for row in artifact.evaluation_samples),
        (row.raw_probability for row in artifact.evaluation_samples),
        ece_bins=5,
    )
    assert artifact.calibrated_metrics is not None
    assert artifact.gate_passed is True
    assert artifact.ece_90_upper is not None
    assert artifact.ece_90_upper >= (
        artifact.calibrated_metrics.expected_calibration_error or 0.0
    )
    assert artifact.origin_model_hashes == tuple(
        sorted(set(artifact.origin_model_hashes))
    )
    assert len(artifact.oos_stream_hash) == 64
    assert len(artifact.calibration_hash) == 64

    for raw_probability in (0.0, 1.0):
        applied = _apply_prematch_calibration(
            artifact,
            raw_probability,
            prediction_cutoff=artifact.calibration_cutoff + timedelta(days=1),
            availability_mode=AvailabilityMode.PROSPECTIVE.value,
            model_hash=_digest("current-model"),
            input_snapshot_hash=_digest(f"target-{raw_probability}"),
        )
        assert applied.calibrated_probability is not None
        assert 0.0 <= applied.calibrated_probability <= 1.0
        a, b = artifact.parameters
        clipped = min(1.0 - 1e-15, max(1e-15, raw_probability))
        expected = 1.0 / (
            1.0 + math.exp(-(a + b * (math.log(clipped) - math.log1p(-clipped))))
        )
        assert applied.calibrated_probability == pytest.approx(expected)


def test_json_load_and_apply_replays_once_and_rejects_noncanonical_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = build_prematch_calibration_artifact(
        _samples(200),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    payload_json = canonical_json_bytes(artifact.to_payload()).decode("utf-8")
    cutoff = artifact.calibration_cutoff + timedelta(days=1)
    application = _apply_prematch_calibration(
        artifact,
        0.7,
        prediction_cutoff=cutoff,
        availability_mode=artifact.availability_mode,
        model_hash=_digest("current-model"),
        input_snapshot_hash=_digest("target"),
    )
    replay = calibration.replay_prematch_calibration_artifact
    calls = 0

    def counted_replay(value):
        nonlocal calls
        calls += 1
        return replay(value)

    monkeypatch.setattr(
        calibration,
        "replay_prematch_calibration_artifact",
        counted_replay,
    )
    loaded_application = _load_and_apply_prematch_calibration_json(
        payload_json,
        0.7,
        prediction_cutoff=cutoff,
        availability_mode=artifact.availability_mode,
        model_hash=_digest("current-model"),
        input_snapshot_hash=_digest("target"),
    )
    assert loaded_application == application
    assert calls == 1

    with pytest.raises(ValueError, match="canonical"):
        _load_and_apply_prematch_calibration_json(
            " " + payload_json,
            0.7,
            prediction_cutoff=cutoff,
            availability_mode=artifact.availability_mode,
            model_hash=_digest("current-model"),
            input_snapshot_hash=_digest("target"),
        )


def test_reconstructed_numeric_pass_never_becomes_passed() -> None:
    artifact = build_prematch_calibration_artifact(
        _samples(200, mode=AvailabilityMode.RECONSTRUCTED.value),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
    )

    assert artifact.gate_passed is True
    assert artifact.status is CalibrationStatus.RECONSTRUCTED_ONLY
    assert artifact.status is not CalibrationStatus.PASSED


def test_evaluation_support_gate_is_fixed_at_100() -> None:
    below = build_prematch_calibration_artifact(
        _samples(199),
        _cutoff(199),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    exact = build_prematch_calibration_artifact(
        _samples(200),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert below.fit_support == 100
    assert below.evaluation_support == 99
    assert below.status is CalibrationStatus.PROVISIONAL
    assert below.reason == "evaluation_support_below_100"
    assert "evaluation_support_below_100" in below.gate_reasons
    assert exact.evaluation_support == 100
    assert exact.status is CalibrationStatus.PASSED


@pytest.mark.parametrize(
    "mode",
    (AvailabilityMode.PROSPECTIVE.value, AvailabilityMode.RECONSTRUCTED.value),
)
def test_sufficient_gate_failure_is_failed_and_not_applied(mode: str) -> None:
    rows = _samples(200, mode=mode)
    adversarial = tuple(
        row if index < 100 else replace(row, outcome=1 - row.outcome)
        for index, row in enumerate(rows)
    )
    artifact = build_prematch_calibration_artifact(
        adversarial,
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=mode,
    )

    assert artifact.evaluation_support == CALIBRATION_MIN_EVALUATION_SUPPORT
    assert artifact.parameters is not None
    assert artifact.gate_passed is False
    assert artifact.status is CalibrationStatus.FAILED
    assert artifact.reason == "calibration_gate_failed"
    application = _apply_prematch_calibration(
        artifact,
        0.7,
        prediction_cutoff=artifact.calibration_cutoff + timedelta(days=1),
        availability_mode=mode,
        model_hash=_digest("current-model"),
        input_snapshot_hash=_digest("target"),
    )
    assert application.status is CalibrationStatus.FAILED
    assert application.calibrated_probability is None


def test_optimizer_failure_is_failed_without_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calibration, "_fit_platt", lambda _rows: None)
    artifact = build_prematch_calibration_artifact(
        _samples(200),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert artifact.status is CalibrationStatus.FAILED
    assert artifact.reason == "optimizer_failed"
    assert artifact.parameters is None


def test_minimum_fit_support_no_legal_split_and_single_class_are_unsupported() -> None:
    too_small = build_prematch_calibration_artifact(
        _samples(100),
        _cutoff(100),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    interleaved = (
        _sample(
            200,
            prediction_cutoff=START,
            result_usable_at=START + timedelta(days=10),
        ),
        _sample(
            201,
            prediction_cutoff=START + timedelta(days=5),
            result_usable_at=START + timedelta(days=6),
        ),
    )
    no_split = build_prematch_calibration_artifact(
        interleaved,
        START + timedelta(days=20),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    single_class = build_prematch_calibration_artifact(
        _samples(200, outcome=0),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert too_small.status is CalibrationStatus.UNSUPPORTED
    assert too_small.reason == "fit_support_below_100"
    assert no_split.status is CalibrationStatus.UNSUPPORTED
    assert no_split.reason == "no_legal_series_split"
    assert single_class.status is CalibrationStatus.UNSUPPORTED
    assert single_class.reason == "single_class_split"
    assert single_class.parameters is None
    assert single_class.calibrated_metrics is None


def test_splitter_continues_past_first_supported_single_class_boundary() -> None:
    rows = tuple(
        _sample(index, outcome=0 if index < 100 else index % 2)
        for index in range(210)
    )

    artifact = build_prematch_calibration_artifact(
        rows,
        _cutoff(210),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert artifact.reason is None
    assert artifact.fit_support == 102
    assert artifact.evaluation_support == 108
    assert {row.outcome for row in artifact.fit_samples} == {0, 1}
    assert {row.outcome for row in artifact.evaluation_samples} == {0, 1}
    assert artifact.parameters is not None


def test_future_samples_and_outcomes_do_not_change_bounded_artifact() -> None:
    rows = _samples(200)
    cutoff = _cutoff(200)
    baseline = build_prematch_calibration_artifact(
        rows,
        cutoff,
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    future_zero = _sample(
        500,
        outcome=0,
        prediction_cutoff=cutoff + timedelta(days=1),
        result_usable_at=cutoff + timedelta(days=2),
    )
    future_one = replace(
        future_zero,
        outcome=1,
        raw_probability=0.9,
        model_hash=_digest("future-model-changed"),
        input_snapshot_hash=_digest("future-input-changed"),
    )
    with_zero = build_prematch_calibration_artifact(
        (*rows, future_zero),
        cutoff,
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    with_one = build_prematch_calibration_artifact(
        (future_one, *reversed(rows)),
        cutoff,
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert with_zero == baseline
    assert with_one == baseline


def test_evaluation_outcomes_never_change_split_or_fitted_parameters() -> None:
    rows = _samples(200)
    baseline = build_prematch_calibration_artifact(
        rows,
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    changed_rows = tuple(
        row if index < 100 else replace(row, outcome=1 - row.outcome)
        for index, row in enumerate(rows)
    )
    changed = build_prematch_calibration_artifact(
        changed_rows,
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert changed.fit_samples == baseline.fit_samples
    assert changed.fit_cutoff == baseline.fit_cutoff
    assert changed.evaluation_start == baseline.evaluation_start
    assert changed.parameters == baseline.parameters
    assert changed.oos_stream_hash != baseline.oos_stream_hash


def test_reverse_monotonic_fit_is_failed_and_never_applied() -> None:
    rows = _samples(200)
    reversed_fit = tuple(
        replace(row, outcome=1 - row.outcome) if index < 100 else row
        for index, row in enumerate(rows)
    )
    artifact = build_prematch_calibration_artifact(
        reversed_fit,
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert artifact.fit_support == 100
    assert artifact.parameters is not None and artifact.parameters[1] < 0.0
    assert artifact.status is CalibrationStatus.FAILED
    assert artifact.reason == "reverse_monotonic_calibration"
    assert artifact.gate_reasons == ("reverse_monotonic_calibration",)
    application = _apply_prematch_calibration(
        artifact,
        0.7,
        prediction_cutoff=artifact.calibration_cutoff + timedelta(days=1),
        availability_mode=artifact.availability_mode,
        model_hash=_digest("current-model"),
        input_snapshot_hash=_digest("target"),
    )
    assert application.calibrated_probability is None


def test_apply_enforces_time_mode_and_never_uses_identity_for_unsupported() -> None:
    trained = build_prematch_calibration_artifact(
        _samples(200),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    assert trained.fit_cutoff is not None
    with pytest.raises(ValueError, match="not usable"):
        _apply_prematch_calibration(
            trained,
            0.6,
            prediction_cutoff=trained.calibration_cutoff,
            availability_mode=AvailabilityMode.PROSPECTIVE.value,
            model_hash=_digest("model"),
            input_snapshot_hash=_digest("input"),
        )
    with pytest.raises(ValueError, match="availability modes"):
        _apply_prematch_calibration(
            trained,
            0.6,
            prediction_cutoff=trained.calibration_cutoff + timedelta(days=1),
            availability_mode=AvailabilityMode.RECONSTRUCTED.value,
            model_hash=_digest("model"),
            input_snapshot_hash=_digest("input"),
        )

    unsupported = build_prematch_calibration_artifact(
        _samples(200, outcome=0),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    applied = _apply_prematch_calibration(
        unsupported,
        0.73,
        prediction_cutoff=unsupported.calibration_cutoff + timedelta(days=1),
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
        model_hash=_digest("model"),
        input_snapshot_hash=_digest("input"),
    )
    assert applied.status is CalibrationStatus.UNSUPPORTED
    assert applied.raw_probability == 0.73
    assert applied.calibrated_probability is None


def test_unbound_calibration_application_helpers_are_private() -> None:
    assert "apply_prematch_calibration" not in calibration.__all__
    assert "load_and_apply_prematch_calibration_json" not in calibration.__all__
    assert not hasattr(calibration, "apply_prematch_calibration")
    assert not hasattr(calibration, "load_and_apply_prematch_calibration_json")


def test_json_load_and_full_refit_replay_are_exact_and_deterministic() -> None:
    artifact = build_prematch_calibration_artifact(
        _samples(200),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    payload_json = canonical_json_bytes(artifact.to_payload()).decode("utf-8")

    loaded = load_prematch_calibration_artifact_json(payload_json)
    replayed = replay_prematch_calibration_artifact(loaded)
    reordered = build_prematch_calibration_artifact(
        reversed(_samples(200)),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )

    assert loaded == artifact
    assert replayed == artifact
    assert reordered == artifact
    assert loaded.ece_90_upper == artifact.ece_90_upper


@pytest.mark.parametrize("tamper", ["hash", "parameter", "runtime", "version"])
def test_loader_rejects_hash_parameter_runtime_and_version_tampering(
    tamper: str,
) -> None:
    artifact = build_prematch_calibration_artifact(
        _samples(200),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    payload = copy.deepcopy(artifact.to_payload())
    if tamper == "hash":
        payload["calibration_hash"] = "0" * 64
    elif tamper == "parameter":
        assert isinstance(payload["parameters"], dict)
        payload["parameters"]["a"] += 0.5
        payload["calibration_hash"] = _artifact_hash(payload)
    elif tamper == "runtime":
        assert isinstance(payload["runtime_versions"], dict)
        payload["runtime_versions"]["scipy"] = "0.0-tampered"
        payload["calibration_hash"] = _artifact_hash(payload)
    else:
        payload["calibration_version"] = "prematch-platt-v999"
        payload["calibration_hash"] = _artifact_hash(payload)

    with pytest.raises(ValueError):
        load_prematch_calibration_artifact_json(
            canonical_json_bytes(payload).decode("utf-8")
        )


def test_strict_json_rejects_duplicate_keys_and_nonfinite_constants() -> None:
    artifact = build_prematch_calibration_artifact(
        _samples(200),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    canonical = json.dumps(artifact.to_payload(), separators=(",", ":"))
    duplicate = '{"artifact_schema":"duplicate",' + canonical[1:]
    nonfinite = canonical.replace('"fit_support":100', '"fit_support":NaN', 1)

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_prematch_calibration_artifact_json(duplicate)
    with pytest.raises(ValueError, match="invalid JSON constant"):
        load_prematch_calibration_artifact_json(nonfinite)


def test_json_loader_accepts_only_canonical_serialization() -> None:
    artifact = build_prematch_calibration_artifact(
        _samples(200),
        _cutoff(200),
        model_kind=MODEL_KIND,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    payload = artifact.to_payload()
    canonical = canonical_json_bytes(payload).decode("utf-8")
    reordered = json.dumps(
        dict(reversed(tuple(payload.items()))),
        allow_nan=False,
        separators=(",", ":"),
    )
    alternate_number = canonical.replace(
        '"bootstrap_confidence":0.9',
        '"bootstrap_confidence":9e-1',
        1,
    )

    assert load_prematch_calibration_artifact_json(canonical) == artifact
    assert reordered != canonical
    assert alternate_number != canonical
    for noncanonical in (f" {canonical}", reordered, alternate_number):
        with pytest.raises(ValueError, match="JSON is not canonical"):
            load_prematch_calibration_artifact_json(noncanonical)


def test_gate_thresholds_are_strict_for_brier_log_loss_and_inclusive_for_ece() -> None:
    metrics = evaluate_binary_predictions(
        (index % 2 for index in range(100)),
        (0.5 for _index in range(100)),
        ece_bins=5,
    )
    failed, reasons = calibration._gate(metrics, 0.15)
    assert failed is False
    assert "brier_not_below_0.25" in reasons
    assert "log_loss_not_below_ln2" in reasons

    passing_metrics = replace(
        metrics,
        brier_score=0.249999,
        log_loss=math.log(2.0) - 1e-6,
        expected_calibration_error=0.10,
    )
    passed, reasons = calibration._gate(passing_metrics, 0.15)
    assert passed is True
    assert reasons == ()
    passed, reasons = calibration._gate(passing_metrics, 0.150001)
    assert passed is False
    assert reasons == ("ece_90_upper_above_0.15",)


def test_stream_rejects_mixed_mode_kind_and_non_oos_claims() -> None:
    with pytest.raises(ValueError, match="out-of-sample"):
        replace(_sample(0), is_out_of_sample=False)

    mixed_mode = (
        *_samples(199),
        _sample(119, mode=AvailabilityMode.RECONSTRUCTED.value),
    )
    with pytest.raises(ValueError, match="availability modes"):
        build_prematch_calibration_artifact(
            mixed_mode,
            _cutoff(200),
            model_kind=MODEL_KIND,
            availability_mode=AvailabilityMode.PROSPECTIVE.value,
        )

    mixed_kind = replace(_sample(119), model_kind="team_plus_draft")
    with pytest.raises(ValueError, match="model kinds"):
        build_prematch_calibration_artifact(
            (*_samples(199), mixed_kind),
            _cutoff(200),
            model_kind=MODEL_KIND,
            availability_mode=AvailabilityMode.PROSPECTIVE.value,
        )


def test_sample_rejects_naive_time_and_result_at_prediction_cutoff() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(_sample(0), prediction_cutoff=START.replace(tzinfo=None))
    with pytest.raises(ValueError, match="after prediction"):
        replace(
            _sample(0),
            result_usable_at=_sample(0).prediction_cutoff,
        )
