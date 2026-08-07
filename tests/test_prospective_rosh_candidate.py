from __future__ import annotations

import pytest

from event_intelligence.prospective_rosh_candidate import (
    candidate_probability,
    load_frozen_prospective_rosh_candidate,
    verify_prospective_rosh_candidate,
)


def test_frozen_candidate_identity_and_parameters_are_immutable() -> None:
    candidate = load_frozen_prospective_rosh_candidate()

    verify_prospective_rosh_candidate(candidate)
    assert candidate.artifact_hash == (
        "84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d"
    )
    assert candidate.training_support == 513
    assert candidate.beta_rosh == pytest.approx(0.6692263354789106)
    assert candidate.score_mean == pytest.approx(0.5471734892787526)
    assert candidate.score_scale == pytest.approx(12.485361284192061)
    assert all(fold.beta_rosh > 0 for fold in candidate.folds)


def test_candidate_direction_and_neutral_point_are_explicit() -> None:
    candidate = load_frozen_prospective_rosh_candidate()
    p0 = 0.57

    neutral, _, _ = candidate_probability(
        candidate,
        team_probability=p0,
        pure_rosh_score=candidate.score_mean,
    )
    positive, _, _ = candidate_probability(
        candidate,
        team_probability=p0,
        pure_rosh_score=candidate.score_mean + candidate.score_scale,
    )
    negative, _, _ = candidate_probability(
        candidate,
        team_probability=p0,
        pure_rosh_score=candidate.score_mean - candidate.score_scale,
    )

    assert neutral == pytest.approx(p0)
    assert positive > p0
    assert negative < p0
