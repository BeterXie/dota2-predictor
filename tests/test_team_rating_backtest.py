from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from database.session import DatabaseResult, DatabaseRow
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.storage import IntelligenceStorage
from event_intelligence.team_rating import RatingMapInput
from event_intelligence.team_rating import (
    TeamRatingConfig,
    predict_team_rating,
    replay_team_ratings,
)
import event_intelligence.team_rating_backtest as team_rating_backtest
from event_intelligence.team_rating_backtest import (
    TEAM_RATING_BACKTEST_VERSION,
    TEAM_RATING_PARAMETER_GRID,
    LoadedTeamRatingMap,
    TeamRatingCorpus,
    TeamRatingParameters,
    TeamRatingSourceAuthority,
    TeamRatingWalkForwardRun,
    build_team_rating_report,
    build_team_rating_walk_forward_runs,
    combined_team_rating_training_input_hash,
    evaluate_team_rating_runs,
    load_team_rating_corpus,
    report_as_markdown,
    run_team_rating_backtest,
    select_team_rating_parameters,
    team_rating_authority_fingerprint,
    team_rating_run_id,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
RADIANT_ROSTER = (1, 2, 3, 4, 5)
DIRE_ROSTER = (6, 7, 8, 9, 10)
PARAMETERS = TeamRatingParameters(400.0, 16.0, 180.0, 1.0)


def _row(
    match_id: int,
    *,
    started_at: datetime | None = None,
    result_usable_at: datetime | None | object = ...,
    radiant_win: bool = True,
    radiant_team_id: int = 10,
    dire_team_id: int = 20,
    series_id: int | None = 100,
    radiant_roster: tuple[int, ...] = RADIANT_ROSTER,
    dire_roster: tuple[int, ...] = DIRE_ROSTER,
) -> RatingMapInput:
    started = started_at or START + timedelta(hours=2 * match_id)
    completed = started + timedelta(minutes=40)
    usable = (
        completed + timedelta(minutes=1)
        if result_usable_at is ...
        else result_usable_at
    )
    return RatingMapInput(
        match_id=match_id,
        series_id=series_id,
        event_id="event-a",
        started_at=started,
        completed_at=completed,
        result_usable_at=usable,  # type: ignore[arg-type]
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_roster=radiant_roster,
        dire_roster=dire_roster,
        radiant_win=radiant_win,
    )


def _source(
    row: RatingMapInput,
    *,
    source_revision: str = "v1",
) -> TeamRatingSourceAuthority:
    content_hash = hashlib.sha256(
        f"formal-map:{row.match_id}:{source_revision}".encode("utf-8")
    ).hexdigest()
    return TeamRatingSourceAuthority(
        match_id=row.match_id,
        artifact_id=f"opendota:{content_hash}",
        content_hash=content_hash,
        artifact_usable_at=row.result_usable_at,
        observation_usable_at=row.result_usable_at,
    )


def _corpus(
    *rows: RatingMapInput,
    sources: dict[int, TeamRatingSourceAuthority] | None = None,
) -> TeamRatingCorpus:
    source_index = sources or {row.match_id: _source(row) for row in rows}
    return TeamRatingCorpus(
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        formal_maps=len(rows),
        maps=tuple(
            LoadedTeamRatingMap(
                row,
                row.started_at,
                "reconstructed_map_start",
                source_index[row.match_id],
            )
            for row in rows
        ),
    )


def test_full_parameter_grid_is_frozen_and_complete() -> None:
    assert TEAM_RATING_BACKTEST_VERSION == "team-rating-walk-forward-v1"
    assert len(TEAM_RATING_PARAMETER_GRID) == 3 * 4 * 4 * 3 == 144
    assert len(set(TEAM_RATING_PARAMETER_GRID)) == 144


def test_vectorized_prefix_replay_matches_per_cutoff_reference() -> None:
    rows = tuple(
        _row(
            index + 1,
            started_at=START + timedelta(hours=2 * index),
            result_usable_at=START + timedelta(hours=2 * index, minutes=40),
            radiant_win=bool(index % 3),
            radiant_team_id=10 + index % 4,
            dire_team_id=10 + (index + 1) % 4,
        )
        for index in range(12)
    )
    candidates = (
        TeamRatingParameters(200.0, 8.0, None, 1.0),
        PARAMETERS,
        TeamRatingParameters(300.0, 24.0, 90.0, 2.0),
    )
    histories = {
        row.match_id: team_rating_backtest._earlier_training_corpus(  # noqa: SLF001
            rows,
            row.started_at,
        )
        for row in rows
    }
    probabilities = team_rating_backtest._vectorized_candidate_probabilities(  # noqa: SLF001
        rows,
        candidates,
        histories,
        batch_size=2,
    )
    selections = team_rating_backtest._walk_forward_parameter_selections(  # noqa: SLF001
        rows,
        candidates,
        probabilities,
    )

    for parameter_index, parameters in enumerate(candidates):
        for map_index, target in enumerate(rows):
            reference = team_rating_backtest._inner_candidate_probability(  # noqa: SLF001
                histories[target.match_id],
                target,
                parameters,
            )
            assert probabilities[parameter_index, map_index] == pytest.approx(
                reference,
                abs=1e-12,
            )
    for target in rows:
        history = histories[target.match_id]
        reference = select_team_rating_parameters(
            history,
            candidate_probabilities={
                (parameters, inner.match_id): float(
                    probabilities[parameter_index, rows.index(inner)]
                )
                for parameter_index, parameters in enumerate(candidates)
                for inner in history
            },
            candidates=candidates,
        )
        optimized = selections[target.match_id]
        assert optimized.parameters == reference.parameters
        assert optimized.support == reference.support
        assert optimized.log_loss == pytest.approx(reference.log_loss, abs=1e-15)
        assert optimized.brier_score == pytest.approx(
            reference.brier_score,
            abs=1e-15,
        )


def test_parameter_selection_uses_log_loss_then_brier() -> None:
    first = _row(1, radiant_win=True)
    second = _row(2, radiant_win=False)
    better = TeamRatingParameters(400.0, 16.0, None, 1.0)
    worse = TeamRatingParameters(200.0, 32.0, 90.0, 2.0)
    probabilities = {
        (better, first.match_id): 0.8,
        (better, second.match_id): 0.2,
        (worse, first.match_id): 0.6,
        (worse, second.match_id): 0.4,
    }

    selection = select_team_rating_parameters(
        (first, second),
        candidate_probabilities=probabilities,
        candidates=(worse, better),
    )

    assert selection.parameters == better
    assert selection.support == 2
    assert selection.log_loss is not None
    assert selection.brier_score is not None


def test_parameter_selection_prefers_simpler_mechanisms_without_inner_support() -> None:
    simple = TeamRatingParameters(400.0, 16.0, None, 1.0)
    complex_value = TeamRatingParameters(200.0, 8.0, 90.0, 2.0)

    selection = select_team_rating_parameters(
        (),
        candidate_probabilities={},
        candidates=(complex_value, simple),
    )

    assert selection.parameters == simple
    assert selection.support == 0
    assert selection.log_loss is None


@pytest.mark.parametrize(
    "parameters",
    (
        TeamRatingParameters(200.0, 8.0, None, 0.5),
        TeamRatingParameters(300.0, 24.0, 90.0, 1.0),
        TeamRatingParameters(400.0, 32.0, 365.0, 2.0),
    ),
)
def test_inner_grid_replay_matches_pr1_probability(
    parameters: TeamRatingParameters,
) -> None:
    first = _row(1, radiant_win=True)
    assert first.result_usable_at is not None
    second = _row(
        2,
        started_at=first.result_usable_at + timedelta(days=100),
        radiant_win=False,
    )
    assert second.result_usable_at is not None
    target = _row(
        3,
        started_at=second.result_usable_at + timedelta(days=50),
        radiant_team_id=20,
        dire_team_id=10,
    )
    history = (first, second)
    side_logit = team_rating_backtest.estimate_radiant_side_logit(1, 2)
    config = TeamRatingConfig(
        initial_rating=1_500.0,
        scale=parameters.scale,
        k_factor=parameters.k_factor,
        inactivity_half_life_days=parameters.inactivity_half_life_days,
        roster_carry_power=parameters.roster_carry_power,
        radiant_side_logit=side_logit,
        config_version="team-rating-elo-v1",
    )
    states = replay_team_ratings(history, target.started_at, config)
    expected = predict_team_rating(
        states,
        target,
        target.started_at,
        config,
    ).raw_probability

    actual = team_rating_backtest._inner_candidate_probability(
        history,
        target,
        parameters,
    )

    assert actual == pytest.approx(expected, abs=1e-15)


def test_inner_grid_replay_matches_changed_and_missing_roster_semantics() -> None:
    first = _row(1, radiant_win=True)
    assert first.result_usable_at is not None
    second = _row(
        2,
        started_at=first.result_usable_at + timedelta(days=30),
        radiant_roster=(11, 12, 13, 14, 15),
        dire_roster=(),
        radiant_win=False,
    )
    assert second.result_usable_at is not None
    target = _row(
        3,
        started_at=second.result_usable_at + timedelta(days=30),
        radiant_roster=(),
        dire_roster=(6, 7, 8, 16, 17),
    )
    history = (first, second)
    config = TeamRatingConfig(
        initial_rating=1_500.0,
        scale=PARAMETERS.scale,
        k_factor=PARAMETERS.k_factor,
        inactivity_half_life_days=PARAMETERS.inactivity_half_life_days,
        roster_carry_power=PARAMETERS.roster_carry_power,
        radiant_side_logit=team_rating_backtest.estimate_radiant_side_logit(1, 2),
        config_version="team-rating-elo-v1",
    )
    expected = predict_team_rating(
        replay_team_ratings(history, target.started_at, config),
        target,
        target.started_at,
        config,
    ).raw_probability

    assert team_rating_backtest._inner_candidate_probability(
        history, target, PARAMETERS
    ) == pytest.approx(expected, abs=1e-15)


def test_inner_grid_replay_rejects_negative_inactivity_like_pr1() -> None:
    first = _row(1)
    overlapping = _row(
        2,
        started_at=first.started_at + timedelta(minutes=20),
    )
    target = _row(3, started_at=first.completed_at + timedelta(days=1))

    with pytest.raises(ValueError, match="cannot precede"):
        team_rating_backtest._inner_candidate_probability(
            (first, overlapping),
            target,
            PARAMETERS,
        )


def test_outer_walk_forward_excludes_target_and_honors_result_availability() -> None:
    first = _row(1, series_id=55)
    assert first.result_usable_at is not None
    second = _row(
        2,
        started_at=first.result_usable_at + timedelta(minutes=5),
        series_id=55,
        radiant_win=False,
    )

    runs = build_team_rating_walk_forward_runs(
        _corpus(first, second),
        candidates=(PARAMETERS,),
    )

    assert len(runs) == 2
    assert runs[0].selection.support == 0
    assert runs[0].artifact.ordered_training_corpus == ()
    assert runs[1].selection.support == 1
    assert tuple(
        row.match_id for row in runs[1].artifact.ordered_training_corpus
    ) == (first.match_id,)
    assert all(
        run.artifact.target.match_id
        not in {row.match_id for row in run.artifact.ordered_training_corpus}
        for run in runs
    )


def test_same_series_result_after_target_cutoff_cannot_update_or_tune() -> None:
    first = _row(
        1,
        result_usable_at=START + timedelta(hours=10),
        series_id=55,
    )
    second = _row(
        2,
        started_at=first.completed_at + timedelta(minutes=5),
        series_id=55,
        radiant_win=False,
    )

    runs = build_team_rating_walk_forward_runs(
        _corpus(first, second),
        candidates=(PARAMETERS,),
    )

    assert runs[1].selection.support == 0
    assert runs[1].artifact.ordered_training_corpus == ()
    assert runs[1].artifact.prediction.support == 0


def test_map_completed_exactly_at_cutoff_is_not_earlier_history() -> None:
    first = _row(1)
    second = _row(
        2,
        started_at=first.completed_at,
        radiant_win=False,
    )

    runs = build_team_rating_walk_forward_runs(
        _corpus(first, second),
        candidates=(PARAMETERS,),
    )

    assert runs[1].selection.support == 0
    assert runs[1].artifact.ordered_training_corpus == ()


def test_target_outcome_and_future_rows_do_not_change_historical_prediction() -> None:
    first = _row(1)
    assert first.result_usable_at is not None
    target = _row(2, started_at=first.result_usable_at + timedelta(minutes=5))
    changed_target = replace(target, radiant_win=not target.radiant_win)
    future = _row(3, started_at=target.completed_at + timedelta(days=1))

    baseline = build_team_rating_walk_forward_runs(
        _corpus(first, target),
        candidates=(PARAMETERS,),
    )[1]
    changed = build_team_rating_walk_forward_runs(
        _corpus(first, changed_target),
        candidates=(PARAMETERS,),
    )[1]
    extended = build_team_rating_walk_forward_runs(
        _corpus(first, target, future),
        candidates=(PARAMETERS,),
    )[1]

    assert baseline.artifact == changed.artifact == extended.artifact
    assert baseline.run_id == changed.run_id == extended.run_id
    assert baseline.selection == changed.selection == extended.selection


def test_authority_manifest_binds_target_and_ordered_training_sources() -> None:
    first = _row(1)
    assert first.result_usable_at is not None
    target = _row(2, started_at=first.result_usable_at + timedelta(minutes=5))

    baseline = build_team_rating_walk_forward_runs(
        _corpus(first, target),
        candidates=(PARAMETERS,),
    )[1]
    revised_sources = {
        first.match_id: _source(first, source_revision="v2"),
        target.match_id: _source(target),
    }
    revised = build_team_rating_walk_forward_runs(
        _corpus(first, target, sources=revised_sources),
        candidates=(PARAMETERS,),
    )[1]

    assert tuple(
        source.match_id for source in baseline.ordered_training_sources
    ) == tuple(row.match_id for row in baseline.artifact.ordered_training_corpus)
    assert baseline.target_source_authority.match_id == target.match_id
    assert baseline.authority_fingerprint == team_rating_authority_fingerprint(
        target_source=baseline.target_source_authority,
        ordered_training_sources=baseline.ordered_training_sources,
    )
    assert baseline.combined_training_input_hash == (
        combined_team_rating_training_input_hash(
            artifact_training_input_hash=baseline.artifact.training_input_hash,
            authority_fingerprint=baseline.authority_fingerprint,
        )
    )
    assert baseline.run_id == team_rating_run_id(
        availability_mode=baseline.availability_mode,
        artifact_hash=baseline.artifact.artifact_hash,
        authority_fingerprint=baseline.authority_fingerprint,
    )
    assert revised.artifact == baseline.artifact
    assert revised.authority_fingerprint != baseline.authority_fingerprint
    assert revised.combined_training_input_hash != baseline.combined_training_input_hash
    assert revised.run_id != baseline.run_id
    with pytest.raises(ValueError, match="authority fingerprint"):
        replace(baseline, authority_fingerprint="0" * 64)
    with pytest.raises(ValueError, match="combined Team Rating"):
        replace(baseline, combined_training_input_hash="0" * 64)
    with pytest.raises(ValueError, match="run_id"):
        replace(baseline, run_id="0" * 64)


def test_authority_manifest_rejects_invalid_hash_and_target_in_training() -> None:
    row = _row(1)
    source = _source(row)

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(source, content_hash="A" * 64)
    with pytest.raises(ValueError, match="target source"):
        team_rating_authority_fingerprint(
            target_source=source,
            ordered_training_sources=(source,),
        )


def _db_row(values: dict[str, object]) -> DatabaseRow:
    return DatabaseRow(tuple(values), tuple(values.values()))


class _CorpusConnection:
    def __init__(
        self,
        base_rows: tuple[DatabaseRow, ...],
        player_rows: tuple[DatabaseRow, ...],
        *,
        formal_count: int | None = None,
    ) -> None:
        self.base_rows = base_rows
        self.player_rows = player_rows
        self.formal_count = len(base_rows) if formal_count is None else formal_count

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> DatabaseResult:
        assert parameters == ()
        if "SELECT COUNT(*) FROM formal_map_eligibility" in statement:
            return DatabaseResult(
                (_db_row({"count": self.formal_count}),),
                1,
                ("count",),
            )
        if "JOIN match_players AS player" in statement:
            return DatabaseResult(self.player_rows, len(self.player_rows))
        if "artifact.first_usable_at AS artifact_usable_at" in statement:
            return DatabaseResult(self.base_rows, len(self.base_rows))
        raise AssertionError(statement)


def _formal_fixture(
    *,
    artifact_usable_at: datetime | None | object = ...,
    observation_usable_at: datetime | None | object = ...,
):
    started = START + timedelta(days=1)
    completed = started + timedelta(minutes=40)
    default_usable = completed + timedelta(minutes=2)
    artifact_usable = (
        default_usable if artifact_usable_at is ... else artifact_usable_at
    )
    observation_usable = (
        default_usable
        if observation_usable_at is ...
        else observation_usable_at
    )
    content_hash = hashlib.sha256(b"formal-map").hexdigest()
    base = _db_row(
        {
            "match_id": 101,
            "event_id": "event-a",
            "series_id": 501,
            "latest_raw_artifact_id": f"opendota:{content_hash}",
            "latest_raw_content_hash": content_hash,
            "start_time": int(started.timestamp()),
            "duration": 40 * 60,
            "radiant_win": True,
            "patch": 60,
            "radiant_team_id": 10,
            "dire_team_id": 20,
            "artifact_usable_at": (
                None if artifact_usable is None else artifact_usable.isoformat()
            ),
            "observation_usable_at": (
                None
                if observation_usable is None
                else observation_usable.isoformat()
            ),
        }
    )
    players = tuple(
        _db_row(
            {
                "match_id": 101,
                "account_id": index + 1,
                "player_slot": index if index < 5 else 128 + index - 5,
                "is_radiant": index < 5,
                "team_id": 10 if index < 5 else 20,
            }
        )
        for index in range(10)
    )
    return base, players, started, completed, artifact_usable, observation_usable


def test_formal_loader_reconstructs_completion_but_never_promotes_to_prospective() -> None:
    base, players, started, completed, artifact_usable, observation_usable = (
        _formal_fixture(
            artifact_usable_at=START + timedelta(days=1, minutes=43),
            observation_usable_at=START + timedelta(days=1, minutes=42),
        )
    )
    connection = _CorpusConnection((base,), players)

    reconstructed = load_team_rating_corpus(
        connection,  # type: ignore[arg-type]
        availability_mode=AvailabilityMode.RECONSTRUCTED,
    )
    prospective = load_team_rating_corpus(
        connection,  # type: ignore[arg-type]
        availability_mode=AvailabilityMode.PROSPECTIVE,
    )

    assert reconstructed.targets[0].prediction_cutoff == started
    assert reconstructed.maps[0].row.result_usable_at == completed
    assert reconstructed.maps[0].row.radiant_roster == RADIANT_ROSTER
    assert reconstructed.maps[0].patch == 60
    assert reconstructed.maps[0].source_authority.artifact_id == base[
        "latest_raw_artifact_id"
    ]
    assert reconstructed.maps[0].source_authority.content_hash == base[
        "latest_raw_content_hash"
    ]
    assert prospective.targets == ()
    assert prospective.maps[0].row.result_usable_at == artifact_usable
    assert artifact_usable > observation_usable
    assert prospective.availability_mode == "prospective"

    later_observation, players, _started, _completed, _artifact, observation = (
        _formal_fixture(
            artifact_usable_at=START + timedelta(days=1, minutes=42),
            observation_usable_at=START + timedelta(days=1, minutes=43),
        )
    )
    prospective = load_team_rating_corpus(
        _CorpusConnection((later_observation,), players),  # type: ignore[arg-type]
        availability_mode=AvailabilityMode.PROSPECTIVE,
    )
    assert prospective.maps[0].row.result_usable_at == observation


def test_formal_loader_keeps_incomplete_roster_unknown() -> None:
    base, players, _started, _completed, _artifact, _observation = _formal_fixture()

    corpus = load_team_rating_corpus(
        _CorpusConnection((base,), players[:-1]),  # type: ignore[arg-type]
        availability_mode=AvailabilityMode.RECONSTRUCTED,
    )

    assert corpus.maps[0].row.radiant_roster == RADIANT_ROSTER
    assert corpus.maps[0].row.dire_roster == ()


def test_formal_loader_rejects_impossible_availability_and_missing_authority() -> None:
    base, players, _started, _completed, _artifact, _observation = _formal_fixture(
        artifact_usable_at=START,
        observation_usable_at=START,
    )
    with pytest.raises(ValueError, match="precedes completion"):
        load_team_rating_corpus(
            _CorpusConnection((base,), players),  # type: ignore[arg-type]
            availability_mode=AvailabilityMode.PROSPECTIVE,
        )

    valid, players, _started, _completed, _artifact, _observation = _formal_fixture()
    with pytest.raises(ValueError, match="lacks its exact"):
        load_team_rating_corpus(
            _CorpusConnection((valid,), players, formal_count=2),  # type: ignore[arg-type]
            availability_mode=AvailabilityMode.RECONSTRUCTED,
        )


@pytest.mark.parametrize(
    ("artifact_usable_at", "observation_usable_at"),
    ((None, ...), (..., None), (None, None)),
)
def test_prospective_result_availability_requires_both_exact_authorities(
    artifact_usable_at: datetime | None | object,
    observation_usable_at: datetime | None | object,
) -> None:
    base, players, _started, _completed, _artifact, _observation = _formal_fixture(
        artifact_usable_at=artifact_usable_at,
        observation_usable_at=observation_usable_at,
    )

    corpus = load_team_rating_corpus(
        _CorpusConnection((base,), players),  # type: ignore[arg-type]
        availability_mode=AvailabilityMode.PROSPECTIVE,
    )

    assert corpus.maps[0].row.result_usable_at is None


def _metric_runs(count: int = 40) -> tuple[TeamRatingWalkForwardRun, ...]:
    rows = tuple(
        _row(
            index + 1,
            radiant_win=bool(index % 2),
            series_id=index // 2 + 1,
        )
        for index in range(count)
    )
    runs = build_team_rating_walk_forward_runs(
        _corpus(*rows),
        candidates=(PARAMETERS,),
    )
    adjusted = []
    for run in runs:
        probability = 0.9 if run.eventual_radiant_win else 0.1
        prediction = replace(run.artifact.prediction, raw_probability=probability)
        artifact = replace(run.artifact, prediction=prediction)
        adjusted.append(
            replace(run, artifact=artifact, radiant_prior_probability=0.5)
        )
    return tuple(adjusted)


def test_series_cluster_metrics_and_paired_gate_are_deterministic() -> None:
    runs = _metric_runs()

    first = evaluate_team_rating_runs(runs, bootstrap_samples=200)
    second = evaluate_team_rating_runs(tuple(reversed(runs)), bootstrap_samples=200)

    assert first == second
    baselines, deltas, gate = first
    assert tuple(row.model_name for row in baselines) == (
        "constant_50",
        "radiant_prior",
        "team_rating",
    )
    assert all(row.support == len(runs) - 1 for row in baselines)
    assert all(row.delta is not None and row.delta < 0.0 for row in deltas)
    assert all(row.ci_90.upper is not None and row.ci_90.upper < 0.0 for row in deltas)
    assert gate.status == "passed"
    assert gate.failures == ()


def test_report_serialization_includes_metrics_parameters_and_gate() -> None:
    corpus = _corpus(*(_row(index + 1) for index in range(3)))
    runs = build_team_rating_walk_forward_runs(corpus, candidates=(PARAMETERS,))

    report = build_team_rating_report(
        corpus,
        runs,
        dry_run=True,
        bootstrap_samples=20,
    )
    markdown = report_as_markdown(report)

    assert report.formal_maps == 3
    assert report.eligible_targets == 3
    assert report.evaluated_targets == 2
    assert report.insufficient_targets == 1
    assert report.failed_targets == 0
    assert report.evaluation_coverage == pytest.approx(2 / 3)
    assert report.selected_parameter_counts == (
        (
            '{"inactivity_half_life_days":180.0,"k_factor":16.0,'
            '"roster_carry_power":1.0,"scale":400.0}',
            3,
        ),
    )
    assert "constant_50" in markdown
    assert "Paired Series-cluster Bootstrap" in markdown
    assert report.bootstrap_samples == 20
    assert "Bootstrap samples: 20" in markdown
    assert "Evaluated targets: 2" in markdown
    assert "Insufficient targets: 1" in markdown
    assert "Failed targets: 0" in markdown
    assert "Evaluation coverage: 0.666667" in markdown
    assert "## Baseline 90% Series-cluster Bootstrap Intervals" in markdown
    assert "## Diagnostic Slices" in markdown
    assert "## Probability Distribution" in markdown
    assert "## Calibration Bins" in markdown
    assert sum(row.support for row in report.probability_bins) == 2
    assert sum(row.support for row in report.calibration_bins) == 2
    assert {
        (row.dimension, row.value)
        for row in report.diagnostic_slices
    } >= {
        ("event", "event-a"),
        ("month", "2026-01"),
        ("patch", "unknown"),
        ("team_experience", "new_team_involved"),
    }
    assert "| constant_50 |" in markdown
    assert "  - 90% CI:" not in markdown
    assert f"`{report.gate.status}`" in markdown


def test_evaluation_excludes_insufficient_runs_and_zero_support_is_unsupported() -> None:
    run = build_team_rating_walk_forward_runs(
        _corpus(_row(1)),
        candidates=(PARAMETERS,),
    )[0]

    baselines, deltas, gate = evaluate_team_rating_runs(
        (run,),
        bootstrap_samples=20,
    )

    assert run.status == "insufficient_evidence"
    assert all(row.support == 0 for row in baselines)
    assert deltas == ()
    assert gate.status == "unsupported"
    assert gate.failures == ("support=0",)


def test_evaluation_fails_closed_when_any_target_run_failed() -> None:
    runs = _metric_runs()
    failed = replace(runs[0], status="failed")

    _baselines, _deltas, gate = evaluate_team_rating_runs(
        (failed, *runs[1:]),
        bootstrap_samples=20,
    )

    assert gate.status == "failed"
    assert "failed_targets=1" in gate.failures


def test_empty_report_has_zero_evaluation_coverage() -> None:
    corpus = TeamRatingCorpus(
        AvailabilityMode.RECONSTRUCTED.value,
        0,
        (),
    )

    report = build_team_rating_report(
        corpus,
        (),
        dry_run=True,
        bootstrap_samples=20,
    )

    assert report.eligible_targets == 0
    assert report.evaluated_targets == 0
    assert report.insufficient_targets == 0
    assert report.failed_targets == 0
    assert report.evaluation_coverage == 0.0
    assert report.gate.status == "unsupported"


def test_programmatic_formal_backtest_rejects_prospective_before_database_access() -> None:
    uninitialized_storage = object.__new__(IntelligenceStorage)

    with pytest.raises(ValueError, match="only supports reconstructed"):
        run_team_rating_backtest(
            uninitialized_storage,
            availability_mode=AvailabilityMode.PROSPECTIVE,
        )


def test_naive_cutoff_and_mode_mismatch_fail_closed() -> None:
    row = _row(1)
    with pytest.raises(ValueError, match="timezone-aware"):
        LoadedTeamRatingMap(
            row,
            row.started_at.replace(tzinfo=None),
            "reconstructed_map_start",
            _source(row),
        )
    with pytest.raises(ValueError, match="count"):
        TeamRatingCorpus(
            AvailabilityMode.RECONSTRUCTED.value,
            2,
            (
                LoadedTeamRatingMap(
                    row,
                    row.started_at,
                    "reconstructed_map_start",
                    _source(row),
                ),
            ),
        )
    with pytest.raises(ValueError):
        TeamRatingCorpus("mixed", 0, ())
    with pytest.raises(ValueError, match="prospective targets"):
        TeamRatingCorpus(
            AvailabilityMode.PROSPECTIVE.value,
            1,
            (
                LoadedTeamRatingMap(
                    row,
                    row.started_at,
                    "reconstructed_map_start",
                    _source(row),
                ),
            ),
        )
