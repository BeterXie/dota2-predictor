from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from event_intelligence.prospective_rosh_candidate import (
    candidate_probability,
    load_frozen_prospective_rosh_candidate,
    load_prospective_rosh_candidate_json,
)
from scripts.freeze_prospective_rosh_candidate import build_parser


RESOURCE = (
    Path(__file__).parents[1]
    / "event_intelligence"
    / "resources"
    / "prospective_rosh_candidate_v1.json"
)


def test_frozen_candidate_has_real_513_map_parameters_and_negative_sign() -> None:
    candidate = load_frozen_prospective_rosh_candidate()

    assert candidate.artifact_hash == (
        "e34c8dcce4e26a0fff3d9e34967233e215377ba8aaae250cb1a5f149d6428f6a"
    )
    assert candidate.training_support == 513
    assert candidate.beta_rosh == pytest.approx(-0.6692263354789106)
    assert candidate.score_mean == pytest.approx(0.5471734892787526)
    assert candidate.score_scale == pytest.approx(12.485361284192061)
    assert [row.beta_rosh for row in candidate.folds] == pytest.approx(
        [
            -0.5857440718760298,
            -0.6698525121972112,
            -0.6589956902179078,
            -0.7327795497885198,
            -0.7039008112726385,
        ]
    )
    high, standardized, contribution = candidate_probability(
        candidate,
        team_probability=0.5,
        pure_rosh_score=candidate.score_mean + candidate.score_scale,
    )
    assert standardized == pytest.approx(1.0)
    assert contribution == pytest.approx(0.6692263354789106)
    assert high > 0.5


def test_candidate_artifact_is_canonical_and_profile_drift_is_rejected() -> None:
    body = RESOURCE.read_text(encoding="utf-8").rstrip("\n")
    candidate = load_prospective_rosh_candidate_json(body)
    assert candidate.canonical_bytes().decode("utf-8") == body

    tampered = replace(candidate, scorer_source_hash="f" * 64)
    payload = tampered.to_payload()
    with pytest.raises(ValueError, match="artifact hash mismatch|profile drift"):
        load_prospective_rosh_candidate_json(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )


def test_freeze_cli_exposes_no_training_search_or_deployment_switches() -> None:
    parser = build_parser()
    args = parser.parse_args(
        (
            "--retrospective-analysis",
            "analysis.json",
            "--output",
            "candidate.json",
            "--frozen-at",
            "2026-08-06T14:15:00Z",
            "--prospective-start-at",
            "2026-08-06T14:30:00Z",
        )
    )
    assert args.retrospective_analysis == Path("analysis.json")
    for forbidden in (
        "features",
        "event_weight",
        "month_weight",
        "deploy",
        "calibrate",
        "order",
    ):
        assert not hasattr(args, forbidden)
