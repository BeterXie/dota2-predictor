from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pytest

from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.prematch_artifacts import (
    canonical_json_bytes,
    canonical_hash,
    load_prematch_model_artifact_json,
    prematch_model_artifact_from_payload,
    replay_prematch_model_artifact,
)
from event_intelligence.prematch_model import (
    PREMATCH_MODEL_ARTIFACT_VERSION,
    PREMATCH_MODEL_VERSION,
    PrematchTrainingRow,
    fit_prematch_model,
)
from event_intelligence.rosh_features import ROSH_FEATURE_SCHEMA


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
TRAINING_CUTOFF = START + timedelta(days=80)
MODE = AvailabilityMode.RECONSTRUCTED.value


def _digest(number: int) -> str:
    return f"{number:064x}"


def _features(index: int) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for feature_index, name in enumerate(ROSH_FEATURE_SCHEMA):
        missing = name == "score_20" and index % 6 == 0
        value = None if missing else float((index + feature_index) % 11 - 5)
        result[name] = value
        result[f"{name}__missing"] = 1.0 if missing else 0.0
    return result


def _rows(count: int = 36) -> tuple[PrematchTrainingRow, ...]:
    rows = []
    for index in range(count):
        prediction_cutoff = START + timedelta(days=index)
        offset = (index % 5 - 2) * 0.2
        features = _features(index)
        outcome = int(offset + 0.35 * float(features["relative_advantage"]) > 0)
        rows.append(
            PrematchTrainingRow(
                match_id=index + 1,
                input_snapshot_hash=_digest(index + 1),
                prediction_cutoff=prediction_cutoff,
                completed_at=prediction_cutoff + timedelta(hours=1),
                result_usable_at=prediction_cutoff + timedelta(hours=2),
                availability_mode=MODE,
                outcome=outcome,
                series_id=f"series-{index // 2}",
                event_id=f"event-{index % 4}",
                patch_id=f"7.{40 + index // 18}",
                team_base_logit=offset,
                features=features,
            )
        )
    return tuple(rows)


@lru_cache(maxsize=1)
def _model():
    return fit_prematch_model(
        _rows(),
        TRAINING_CUTOFF,
        model_kind="team_plus_rosh",
        availability_mode=MODE,
        min_samples=10,
    )


def _resign(payload: dict) -> None:
    unsigned = deepcopy(payload)
    unsigned.pop("model_hash", None)
    payload["model_hash"] = canonical_hash(unsigned)


def test_round_trip_rehydrates_every_field_and_full_refit() -> None:
    model = _model()

    loaded = prematch_model_artifact_from_payload(model.to_payload())
    replayed = replay_prematch_model_artifact(loaded)

    assert loaded == model
    assert replayed == model
    assert loaded.artifact_version == PREMATCH_MODEL_ARTIFACT_VERSION
    assert loaded.model_version == PREMATCH_MODEL_VERSION
    assert len(loaded.training_corpus) == loaded.support


def test_round_trip_supports_empty_schema_and_insufficient_artifacts() -> None:
    team_only_rows = tuple(replace(row, features={}) for row in _rows(20))
    team_only = fit_prematch_model(
        team_only_rows,
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
    )
    insufficient = fit_prematch_model(
        _rows(3),
        TRAINING_CUTOFF,
        model_kind="team_plus_rosh",
        availability_mode=MODE,
        min_samples=10,
    )

    assert prematch_model_artifact_from_payload(team_only.to_payload()) == team_only
    assert (
        prematch_model_artifact_from_payload(insufficient.to_payload()) == insufficient
    )


def test_json_loader_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    payload = _model().to_payload()
    raw = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    duplicate = raw.replace(
        f'"support":{payload["support"]}',
        f'"support":{payload["support"]},"support":{payload["support"]}',
        1,
    )
    nonfinite = deepcopy(payload)
    nonfinite["intercept"] = float("nan")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_prematch_model_artifact_json(duplicate)
    with pytest.raises(ValueError, match="invalid JSON constant"):
        load_prematch_model_artifact_json(json.dumps(nonfinite, allow_nan=True))


def test_json_loader_accepts_only_canonical_serialization() -> None:
    model = _model()
    payload = model.to_payload()
    canonical = canonical_json_bytes(payload).decode("utf-8")
    reordered = json.dumps(
        dict(reversed(tuple(payload.items()))),
        allow_nan=False,
        separators=(",", ":"),
    )
    alternate_number = canonical.replace(
        '"l2_regularization":1.0',
        '"l2_regularization":1e0',
        1,
    )

    assert load_prematch_model_artifact_json(canonical) == model
    assert reordered != canonical
    assert alternate_number != canonical
    for noncanonical in (f" {canonical}", reordered, alternate_number):
        with pytest.raises(ValueError, match="JSON is not canonical"):
            load_prematch_model_artifact_json(noncanonical)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload.__setitem__("unknown", True), "unknown"),
        (
            lambda payload: payload["solver"].__setitem__("unknown", True),
            "unknown",
        ),
        (
            lambda payload: payload["training_corpus"][0].__setitem__("unknown", True),
            "unknown",
        ),
        (
            lambda payload: payload["training_corpus"][0]["features"].__setitem__(
                "unknown", 1.0
            ),
            "unknown",
        ),
    ),
)
def test_unknown_keys_fail_closed_at_every_nested_level(mutation, message: str) -> None:
    payload = deepcopy(_model().to_payload())
    mutation(payload)
    with pytest.raises(ValueError, match=message):
        prematch_model_artifact_from_payload(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("artifact_version", "prematch-model-artifact-v999", "artifact version"),
        ("model_version", "prematch-offset-v999", "model version"),
        ("feature_version", "prematch-features-v999", "feature version"),
        ("model_kind", "dynamic", "model kind"),
        ("availability_mode", "mixed", "availability mode"),
    ),
)
def test_unknown_versions_kinds_and_modes_fail_closed(
    field: str,
    value: str,
    message: str,
) -> None:
    payload = deepcopy(_model().to_payload())
    payload[field] = value
    with pytest.raises(ValueError, match=message):
        prematch_model_artifact_from_payload(payload)


@pytest.mark.parametrize("operation", ("remove", "add", "reorder"))
def test_resigned_corpus_add_remove_and_reorder_tampering_fails(operation: str) -> None:
    payload = deepcopy(_model().to_payload())
    if operation == "remove":
        payload["training_corpus"].pop()
    elif operation == "add":
        added = deepcopy(payload["training_corpus"][-1])
        added["match_id"] += 100_000
        added["input_snapshot_hash"] = "f" * 64
        payload["training_corpus"].append(added)
    else:
        payload["training_corpus"][0], payload["training_corpus"][1] = (
            payload["training_corpus"][1],
            payload["training_corpus"][0],
        )
    _resign(payload)

    with pytest.raises(ValueError, match="support|replay"):
        prematch_model_artifact_from_payload(payload)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda row: row.__setitem__(
            "prediction_cutoff",
            (
                datetime.fromisoformat(row["prediction_cutoff"]) + timedelta(seconds=1)
            ).isoformat(),
        ),
        lambda row: row.__setitem__(
            "result_usable_at",
            (
                datetime.fromisoformat(row["result_usable_at"]) + timedelta(seconds=1)
            ).isoformat(),
        ),
        lambda row: row.__setitem__(
            "team_base_logit", float(row["team_base_logit"]) + 0.5
        ),
        lambda row: row.__setitem__("input_snapshot_hash", "e" * 64),
    ),
)
def test_resigned_timestamp_offset_and_snapshot_tampering_fails(mutation) -> None:
    payload = deepcopy(_model().to_payload())
    mutation(payload["training_corpus"][0])
    _resign(payload)

    with pytest.raises(ValueError, match="replay"):
        prematch_model_artifact_from_payload(payload)


@pytest.mark.parametrize("field", ("coefficients", "covariance", "intercept"))
def test_resigned_derived_parameter_tampering_fails_full_refit(field: str) -> None:
    payload = deepcopy(_model().to_payload())
    if field == "coefficients":
        name = next(iter(payload["coefficients"]))
        payload["coefficients"][name] += 0.01
    elif field == "covariance":
        payload["logit_covariance"][0][0] += 0.01
    else:
        payload["intercept"] += 0.01
    _resign(payload)

    with pytest.raises(ValueError, match="replay"):
        prematch_model_artifact_from_payload(payload)


def test_runtime_solver_and_hash_tampering_fail_closed() -> None:
    runtime = deepcopy(_model().to_payload())
    runtime["trainer_runtime"]["scipy"] = "0.0-forged"
    _resign(runtime)
    solver = deepcopy(_model().to_payload())
    solver["solver"]["method"] = "forged"
    model_hash = deepcopy(_model().to_payload())
    model_hash["model_hash"] = "0" * 64

    with pytest.raises(ValueError, match="runtime"):
        prematch_model_artifact_from_payload(runtime)
    with pytest.raises(ValueError, match="solver"):
        prematch_model_artifact_from_payload(solver)
    with pytest.raises(ValueError, match="model hash"):
        prematch_model_artifact_from_payload(model_hash)


def test_training_hash_and_mode_claims_cannot_be_resigned_independently() -> None:
    training_hash = deepcopy(_model().to_payload())
    training_hash["training_input_hash"] = "a" * 64
    _resign(training_hash)
    mode = deepcopy(_model().to_payload())
    mode["training_corpus"][0]["availability_mode"] = AvailabilityMode.PROSPECTIVE.value
    _resign(mode)

    with pytest.raises(ValueError, match="replay"):
        prematch_model_artifact_from_payload(training_hash)
    with pytest.raises(ValueError, match="mix availability modes"):
        prematch_model_artifact_from_payload(mode)


def test_resigned_raw_and_missing_flag_disagreement_fails_refit() -> None:
    payload = deepcopy(_model().to_payload())
    first = payload["training_corpus"][0]
    assert first["features"]["score_20"] is None
    first["features"]["score_20__missing"] = 0.0
    _resign(payload)

    with pytest.raises(ValueError, match="missing flag.*disagrees"):
        prematch_model_artifact_from_payload(payload)


def test_noncanonical_timestamp_representation_fails_closed() -> None:
    payload = deepcopy(_model().to_payload())
    payload["training_cutoff"] = payload["training_cutoff"].replace("+00:00", "Z")
    _resign(payload)

    with pytest.raises(ValueError, match="hash|canonical"):
        prematch_model_artifact_from_payload(payload)
