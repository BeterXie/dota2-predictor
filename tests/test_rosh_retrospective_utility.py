from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from event_intelligence.rosh_retrospective_utility import (
    ALLOWED_CONCLUSIONS,
    CanonicalSelection,
    CohortLoadResult,
    LegacyPureScore,
    RetrospectiveRow,
    analysis_as_markdown,
    analyze_incremental,
    analyze_standalone,
    build_analysis,
    canonicalize_legacy_scores,
    cross_validate_increment,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
FORMULA = "dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793"


def _legacy(
    match_id: int,
    score: float,
    *,
    score_key: str,
    source_as_of: str,
) -> LegacyPureScore:
    return LegacyPureScore(
        match_id=match_id,
        score_key=score_key,
        formula_version=FORMULA,
        source_week=1_700_000_000,
        source_as_of=source_as_of,
        prediction_cutoff=START + timedelta(hours=match_id),
        pure_lineup_score=score,
        event_id="event-a",
        patch=60,
    )


def _rows(count: int = 50) -> tuple[RetrospectiveRow, ...]:
    rows: list[RetrospectiveRow] = []
    for index in range(count):
        score = float(index - count / 2)
        outcome = int(score > 0)
        rows.append(
            RetrospectiveRow(
                match_id=10_000 + index,
                score_key=f"score-{index:03d}",
                formula_version=FORMULA,
                prediction_cutoff=START + timedelta(hours=index),
                pure_lineup_score=score,
                radiant_win=outcome,
                series_id=index // 2,
                series_key=f"series:{index // 2}",
                event_id=f"event-{index % 3}",
                patch=59 + (index % 2),
                month=f"2026-{1 + index // 20:02d}",
                team_probability=0.5,
            )
        )
    return tuple(rows)


def test_canonical_selection_is_result_independent_and_deterministic() -> None:
    rows = (
        _legacy(
            1,
            3.0,
            score_key="later",
            source_as_of="2026-02-01T00:00:00Z",
        ),
        _legacy(
            1,
            2.0,
            score_key="earlier",
            source_as_of="2026-01-01T00:00:00Z",
        ),
        _legacy(
            2,
            -1.0,
            score_key="only",
            source_as_of="2026-01-02T00:00:00Z",
        ),
    )

    selected, summary = canonicalize_legacy_scores(tuple(reversed(rows)))

    assert [row.score_key for row in selected] == ["earlier", "only"]
    assert summary.rows_before == 3
    assert summary.rows_after == 2
    assert summary.duplicate_groups == 1
    assert summary.conflicting_score_groups == 1
    assert "outcome" not in summary.rule


def test_standalone_uses_frozen_radiant_direction_and_neutral_point() -> None:
    report = analyze_standalone(_rows(), bootstrap_samples=30)

    assert report["formula_direction"] == (
        "positive_favors_radiant_negative_favors_dire"
    )
    assert report["neutral_point"] == 0.0
    assert report["auc"] == 1.0
    assert report["point_biserial_correlation"] > 0.8
    assert report["neutral_threshold_accuracy"] == pytest.approx(0.99)
    assert report["quintile_monotonicity"]["spearman_rho"] > 0.8
    assert len(report["deciles"]) == 10


def test_grouped_cv_keeps_team_offset_and_fits_standardization_in_train_fold() -> None:
    rows = _rows()
    report = cross_validate_increment(rows)
    oof = report["oof_predictions"]

    assert len(report["folds"]) == 5
    assert len(oof) == len(rows)
    assert all(row["m0_team_probability"] == 0.5 for row in oof)
    assert len({row["fold"] for row in oof}) == 5
    for series_key in {row.series_key for row in rows}:
        assert len({row["fold"] for row in oof if row["series_key"] == series_key}) == 1
    for fold in report["folds"]:
        train_scores = [
            row.pure_lineup_score
            for row, prediction in zip(rows, oof, strict=True)
            if prediction["fold"] != fold["fold"]
        ]
        assert fold["train_score_mean"] == pytest.approx(
            sum(train_scores) / len(train_scores)
        )
    assert report["m1"]["log_loss"] < report["m0"]["log_loss"]


def test_incremental_analysis_is_deterministic_and_uses_allowed_conclusion_inputs() -> None:
    first, first_sanity = analyze_incremental(
        _rows(), bootstrap_samples=20, sanity_permutations=4
    )
    second, second_sanity = analyze_incremental(
        _rows(), bootstrap_samples=20, sanity_permutations=4
    )

    assert first == second
    assert first_sanity == second_sanity
    assert first["model_m1"].startswith("logit(P_team) + beta")
    assert first["delta_m1_minus_m0"]["log_loss"] < 0.0
    assert first["slice_direction_stability"]["event"]["log_loss"][
        "evaluable_slices"
    ] == 3
    assert ALLOWED_CONCLUSIONS == {
        "no retrospective association detected",
        "standalone retrospective association only",
        "incremental retrospective information beyond Team Rating",
        "unstable / inconclusive retrospective evidence",
    }


def test_final_report_uses_only_allowed_retrospective_claims() -> None:
    rows = _rows()
    cohort = CohortLoadResult(
        candidates=rows,
        paired=rows,
        canonical_selection=CanonicalSelection(
            rows_before=len(rows),
            duplicate_groups=0,
            duplicate_rows=0,
            conflicting_score_groups=0,
            rows_after=len(rows),
            removed_rows=0,
            rule="earliest_source_as_of_then_source_week_then_score_key",
        ),
        evidence_hash_valid=len(rows),
        formal_valid_results=len(rows),
        missing_team_rating=0,
        formula_versions=((FORMULA, len(rows)),),
        source_unchanged=True,
    )

    analysis = build_analysis(
        cohort,
        bootstrap_samples=20,
        sanity_permutations=4,
    )
    markdown = analysis_as_markdown(analysis)

    assert analysis["conclusion"] in ALLOWED_CONCLUSIONS
    assert analysis["rosh_input_fields"] == ["pure_lineup_score"]
    assert analysis["forbidden_fields_used"] == []
    assert "not leakage-free OOS evidence" in markdown
    assert "does not authorize a model change" in markdown
