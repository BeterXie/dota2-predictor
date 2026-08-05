from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from event_intelligence.draft_artifacts import (
    canonical_json_bytes as draft_canonical_json_bytes,
)
from event_intelligence.raw_archive import canonical_json_bytes
from event_intelligence.team_rating import (
    TEAM_RATING_VERSION,
    RatingMapInput,
    TeamRatingConfig,
)
from event_intelligence.team_rating_artifacts import (
    TEAM_RATING_ARTIFACT_SCHEMA,
    TEAM_RATING_ARTIFACT_VERSION,
    TEAM_RATING_RUNTIME_VERSIONS,
    assert_team_rating_artifact_deployable,
    build_team_rating_artifact,
    load_team_rating_artifact_json,
    team_rating_artifact_from_payload,
    verify_team_rating_artifact,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
RADIANT_ROSTER = (1, 2, 3, 4, 5)
DIRE_ROSTER = (6, 7, 8, 9, 10)


def _config(**changes: object) -> TeamRatingConfig:
    values = {
        "initial_rating": 1_500.0,
        "scale": 400.0,
        "k_factor": 24.0,
        "inactivity_half_life_days": 180.0,
        "roster_carry_power": 1.0,
        "radiant_side_logit": 0.05,
        "config_version": TEAM_RATING_VERSION,
    }
    values.update(changes)
    return TeamRatingConfig(**values)  # type: ignore[arg-type]


def _row(
    match_id: int,
    *,
    started_at: datetime | None = None,
    radiant_win: bool | None = None,
    result_usable_at: datetime | None | object = ...,
) -> RatingMapInput:
    started = started_at or START + timedelta(days=match_id)
    completed = started + timedelta(minutes=40)
    usable = (
        completed + timedelta(minutes=2)
        if result_usable_at is ...
        else result_usable_at
    )
    return RatingMapInput(
        match_id=match_id,
        series_id=200 + match_id // 2,
        event_id="event-a",
        started_at=started,
        completed_at=completed,
        result_usable_at=usable,  # type: ignore[arg-type]
        radiant_team_id=10,
        dire_team_id=20,
        radiant_roster=RADIANT_ROSTER,
        dire_roster=DIRE_ROSTER,
        radiant_win=(match_id % 2 == 1 if radiant_win is None else radiant_win),
    )


def _target(*, radiant_win: bool = True) -> RatingMapInput:
    return _row(
        999,
        started_at=START + timedelta(days=20),
        radiant_win=radiant_win,
        result_usable_at=None,
    )


def _artifact(rows: tuple[RatingMapInput, ...] | None = None):
    corpus = rows or (_row(1), _row(2), _row(3))
    target = _target()
    return build_team_rating_artifact(
        corpus,
        target=target,
        prediction_cutoff=target.started_at,
        training_cutoff=START + timedelta(days=10),
        config=_config(),
    )


def _resign(payload: dict) -> None:
    unsigned = deepcopy(payload)
    unsigned.pop("artifact_hash", None)
    payload["artifact_hash"] = hashlib.sha256(
        canonical_json_bytes(unsigned)
    ).hexdigest()


def test_artifact_versions_schema_and_runtime_are_explicit() -> None:
    artifact = _artifact()

    assert TEAM_RATING_ARTIFACT_VERSION == "team-rating-artifact-v1"
    assert TEAM_RATING_ARTIFACT_SCHEMA == "team-rating-artifact/v1"
    assert artifact.artifact_version == TEAM_RATING_ARTIFACT_VERSION
    assert artifact.rating_version == TEAM_RATING_VERSION
    assert artifact.runtime_versions == TEAM_RATING_RUNTIME_VERSIONS
    assert len(artifact.training_input_hash) == 64
    assert len(artifact.artifact_hash) == 64


def test_artifact_round_trip_replays_every_derived_value() -> None:
    artifact = _artifact()
    payload = artifact.to_payload()
    raw = artifact.canonical_bytes().decode("utf-8")

    verify_team_rating_artifact(artifact)
    assert_team_rating_artifact_deployable(artifact)
    assert team_rating_artifact_from_payload(payload) == artifact
    assert load_team_rating_artifact_json(raw) == artifact
    assert canonical_json_bytes(payload) == artifact.canonical_bytes()
    assert draft_canonical_json_bytes(payload) == artifact.canonical_bytes()


def test_artifact_is_order_invariant_duplicate_idempotent_and_deterministic() -> None:
    rows = (_row(1), _row(2), _row(3))

    first = _artifact(rows)
    second = _artifact((rows[2], rows[0], rows[1], rows[0]))
    repeated = _artifact(rows)

    assert first == second == repeated
    assert first.canonical_bytes() == repeated.canonical_bytes()
    assert tuple(row.match_id for row in first.ordered_training_corpus) == (1, 2, 3)


@pytest.mark.parametrize(
    ("name", "mutate", "message"),
    [
        (
            "config",
            lambda value: value["config"].__setitem__("initial_rating", 1_600.0),
            "input hash",
        ),
        (
            "corpus",
            lambda value: value["ordered_training_corpus"][0].__setitem__(
                "radiant_win", False
            ),
            "input hash",
        ),
        (
            "state",
            lambda value: value["state_before_target"][0].__setitem__(
                "rating", value["state_before_target"][0]["rating"] + 10.0
            ),
            "state does not replay",
        ),
        (
            "prediction",
            lambda value: value["prediction"].__setitem__(
                "raw_probability", 1.0 - value["prediction"]["raw_probability"]
            ),
            "prediction does not replay",
        ),
        (
            "runtime",
            lambda value: value["runtime_versions"].__setitem__(
                "python_version", "0.0.0"
            ),
            "runtime identity",
        ),
    ],
)
def test_resigned_critical_tampering_is_rejected(
    name: str,
    mutate,
    message: str,
) -> None:
    payload = deepcopy(_artifact().to_payload())
    mutate(payload)
    _resign(payload)

    with pytest.raises(ValueError, match=message):
        team_rating_artifact_from_payload(payload)


def test_artifact_hash_and_unknown_versions_fail_closed() -> None:
    artifact = _artifact()
    with pytest.raises(ValueError, match="artifact hash"):
        verify_team_rating_artifact(replace(artifact, artifact_hash="0" * 64))
    with pytest.raises(ValueError, match="artifact version"):
        assert_team_rating_artifact_deployable(
            replace(artifact, artifact_version="team-rating-artifact-v0")
        )

    payload = artifact.to_payload()
    payload["artifact_version"] = "team-rating-artifact-v2"
    with pytest.raises(ValueError, match="artifact version"):
        team_rating_artifact_from_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("unknown", True),
        lambda value: value["config"].__setitem__("unknown", True),
        lambda value: value["ordered_training_corpus"][0].__setitem__("unknown", True),
        lambda value: value["state_before_target"][0].__setitem__("unknown", True),
        lambda value: value["target"].__setitem__("unknown", True),
        lambda value: value["prediction"].__setitem__("unknown", True),
    ],
)
def test_unknown_fields_fail_closed(mutate) -> None:
    payload = deepcopy(_artifact().to_payload())
    mutate(payload)

    with pytest.raises(ValueError, match="unknown"):
        team_rating_artifact_from_payload(payload)


def test_json_loader_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    raw = _artifact().canonical_bytes().decode("utf-8")
    duplicate = raw.replace(
        '"artifact_version":"team-rating-artifact-v1"',
        '"artifact_version":"team-rating-artifact-v1",'
        '"artifact_version":"team-rating-artifact-v1"',
        1,
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_team_rating_artifact_json(duplicate)
    with pytest.raises(ValueError, match="invalid JSON constant"):
        load_team_rating_artifact_json(raw.replace("1500.0", "NaN", 1))


def test_target_outcome_and_postmatch_availability_do_not_change_artifact() -> None:
    target = _target(radiant_win=True)
    changed = replace(
        target,
        completed_at=target.completed_at + timedelta(hours=2),
        result_usable_at=target.completed_at + timedelta(hours=3),
        radiant_win=False,
    )
    kwargs = {
        "prediction_cutoff": target.started_at,
        "training_cutoff": START + timedelta(days=10),
        "config": _config(),
    }

    first = build_team_rating_artifact((_row(1), _row(2)), target=target, **kwargs)
    second = build_team_rating_artifact((_row(1), _row(2)), target=changed, **kwargs)

    assert first == second


def test_target_match_is_rejected_from_training_corpus() -> None:
    historical_collision = _row(999, started_at=START + timedelta(days=1))
    target = _target()

    with pytest.raises(ValueError, match="target match"):
        build_team_rating_artifact(
            (historical_collision,),
            target=target,
            prediction_cutoff=target.started_at,
            training_cutoff=START + timedelta(days=10),
            config=_config(),
        )


def test_training_and_prediction_cutoffs_are_strict_and_utc() -> None:
    target = _target()
    with pytest.raises(ValueError, match="training_cutoff cannot follow"):
        build_team_rating_artifact(
            (_row(1),),
            target=target,
            prediction_cutoff=target.started_at - timedelta(seconds=1),
            training_cutoff=target.started_at,
            config=_config(),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        build_team_rating_artifact(
            (_row(1),),
            target=target,
            prediction_cutoff=target.started_at.replace(tzinfo=None),
            training_cutoff=START + timedelta(days=10),
            config=_config(),
        )


def test_unavailable_and_post_cutoff_results_are_absent_from_artifact_corpus() -> None:
    unavailable = _row(1, result_usable_at=None)
    late = _row(2, started_at=START + timedelta(days=11))

    artifact = _artifact((unavailable, late))

    assert artifact.ordered_training_corpus == ()
    assert artifact.state_before_target == ()
    assert artifact.prediction.support == 0


def test_payload_requires_canonical_numeric_and_timestamp_representations() -> None:
    payload = deepcopy(_artifact().to_payload())
    payload["config"]["initial_rating"] = 1500
    _resign(payload)
    with pytest.raises(ValueError, match="hash|not canonical"):
        team_rating_artifact_from_payload(payload)

    payload = deepcopy(_artifact().to_payload())
    payload["training_cutoff"] = "2026-01-10T00:00:00"
    _resign(payload)
    with pytest.raises(ValueError, match="timezone-aware"):
        team_rating_artifact_from_payload(payload)
