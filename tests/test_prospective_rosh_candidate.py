from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from event_intelligence.prospective_rosh_candidate import (
    _canonical_scorer_source_bytes,
    candidate_probability,
    load_frozen_prospective_rosh_candidate,
    load_prospective_rosh_candidate_json,
    verify_previous_parameterization_parity,
)
from event_intelligence.legacy_rosh_reconstruction import (
    LEGACY_ROSH_FORMULA_VERSION,
)
from event_intelligence.rosh_retrospective_utility import RetrospectiveRow
from scripts.freeze_prospective_rosh_candidate import build_parser


RESOURCE = (
    Path(__file__).parents[1]
    / "event_intelligence"
    / "resources"
    / "prospective_rosh_candidate_v1.json"
)


def test_frozen_scorer_source_identity_is_checkout_line_ending_independent() -> None:
    lf = b"first\nsecond\n"
    crlf = b"first\r\nsecond\r\n"

    assert _canonical_scorer_source_bytes(lf) == crlf
    assert _canonical_scorer_source_bytes(crlf) == crlf
    source = (Path(__file__).parents[1] / "prematch" / "stratz_rosh.py").read_bytes()
    linux_checkout = source.replace(b"\r\n", b"\n")
    frozen = json.loads(RESOURCE.read_text(encoding="utf-8"))
    assert hashlib.sha256(_canonical_scorer_source_bytes(linux_checkout)).hexdigest() == (
        frozen["scorer_source_hash"]
    )


def test_frozen_candidate_has_real_513_map_parameters_and_positive_sign() -> None:
    candidate = load_frozen_prospective_rosh_candidate()

    assert candidate.artifact_hash == (
        "84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d"
    )
    assert candidate.training_support == 513
    assert candidate.beta_rosh == pytest.approx(0.6692263354789106)
    assert candidate.score_mean == pytest.approx(0.5471734892787526)
    assert candidate.score_scale == pytest.approx(12.485361284192061)
    assert [row.beta_rosh for row in candidate.folds] == pytest.approx(
        [
            0.5857440718760298,
            0.6698525121972112,
            0.6589956902179078,
            0.7327795497885198,
            0.7039008112726385,
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


def test_score_direction_and_frozen_mean_are_unambiguous() -> None:
    candidate = load_frozen_prospective_rosh_candidate()
    p0 = 0.55
    positive, _, positive_contribution = candidate_probability(
        candidate,
        team_probability=p0,
        pure_rosh_score=10.0,
    )
    negative, _, negative_contribution = candidate_probability(
        candidate,
        team_probability=p0,
        pure_rosh_score=-10.0,
    )
    neutral, standardized, neutral_contribution = candidate_probability(
        candidate,
        team_probability=p0,
        pure_rosh_score=candidate.score_mean,
    )

    assert positive > p0
    assert positive_contribution > 0.0
    assert negative < p0
    assert negative_contribution < 0.0
    assert standardized == 0.0
    assert neutral_contribution == 0.0
    assert neutral == p0


def test_positive_beta_is_exactly_equivalent_on_513_rows() -> None:
    candidate = load_frozen_prospective_rosh_candidate()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = tuple(
        RetrospectiveRow(
            match_id=8_000_000_000 + index,
            score_key=f"{index + 1:064x}",
            formula_version=LEGACY_ROSH_FORMULA_VERSION,
            prediction_cutoff=start + timedelta(minutes=index),
            pure_lineup_score=float((index % 81) - 40),
            radiant_win=index % 2,
            series_id=1_000_000 + index // 2,
            series_key=f"series:{1_000_000 + index // 2}",
            event_id="parity-fixture",
            patch=60,
            month="2026-01",
            team_probability=0.2 + (index % 61) / 100.0,
        )
        for index in range(513)
    )

    assert verify_previous_parameterization_parity(rows, candidate) == (513, 0.0)


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
