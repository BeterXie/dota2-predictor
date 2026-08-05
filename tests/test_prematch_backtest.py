from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import event_intelligence.prematch_backtest as prematch_backtest
import event_intelligence.prematch_storage as prematch_storage
from event_intelligence.draft_features import ROLE_CONFIDENCE_MIN, AvailabilityMode
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
    DRAFT_RESIDUAL_PURE_SCHEMA,
)
from event_intelligence.prematch_backtest import (
    PREMATCH_BACKTEST_VERSION,
    PrematchBacktestTarget,
    PrematchCorpus,
    build_prematch_walk_forward,
    load_prematch_corpus,
)
from event_intelligence.prematch_features import (
    PREMATCH_FEATURE_VERSION,
    PREMATCH_MODEL_KINDS,
    PrematchFeatureSnapshot,
)
from event_intelligence.prematch_model import PredictionStatus
from event_intelligence.prematch_report import build_prematch_report
from event_intelligence.rosh_features import (
    ROSH_FEATURE_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
    replay_rosh_feature_snapshot,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
MODE = AvailabilityMode.RECONSTRUCTED.value


def _digest(number: int) -> str:
    return f"{number:064x}"


def _probability(logit: float) -> float:
    return 1.0 / (1.0 + math.exp(-logit))


def _snapshot(
    match_id: int,
    prediction_cutoff: datetime,
    *,
    team_base_logit: float,
    draft_signal: float,
    rosh_signal: float,
    rosh_available: bool = True,
    availability_mode: str = MODE,
) -> PrematchFeatureSnapshot:
    draft: dict[str, float | None] = {}
    for index, name in enumerate(DRAFT_RESIDUAL_PURE_SCHEMA):
        draft[name] = draft_signal if index == 0 else float(index) / 10.0
        draft[f"{name}__log1p_support"] = math.log1p(20.0 + index)
        draft[f"{name}__coverage"] = 0.8
        draft[f"{name}__missing"] = 0.0
    rosh: dict[str, float | None] = {}
    for index, name in enumerate(ROSH_FEATURE_SCHEMA):
        if rosh_available:
            value = (
                0.9
                if name == "coverage"
                else rosh_signal
                if index == 0
                else float(index)
            )
        else:
            value = 0.0 if name == "coverage" else None
        rosh[name] = value
        rosh[f"{name}__missing"] = 1.0 if value is None else 0.0
    return PrematchFeatureSnapshot(
        match_id=match_id,
        prediction_cutoff=prediction_cutoff,
        availability_mode=availability_mode,
        feature_version=PREMATCH_FEATURE_VERSION,
        team_base_logit=team_base_logit,
        team_rating_run_id=_digest(1_000 + match_id),
        team_rating_artifact_hash=_digest(2_000 + match_id),
        team_rating_prediction_input_hash=_digest(3_000 + match_id),
        team_rating_combined_training_input_hash=_digest(4_000 + match_id),
        team_rating_support=30,
        draft_residual_input_hash=_digest(5_000 + match_id),
        draft_residual_authority_fingerprint=_digest(6_000 + match_id),
        draft_residual_team_rating_input_hash=_digest(7_000 + match_id),
        draft_residual_feature_schema_hash=DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        draft_residual_model_schema_hash=DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
        draft_support=20,
        draft_coverage=0.8,
        draft_features=tuple(draft.items()),
        rosh_status="available" if rosh_available else "unavailable",
        rosh_missing_reason=None if rosh_available else "no_cutoff_legal_run",
        rosh_input_hash=_digest(8_000 + match_id),
        rosh_model_schema_hash=ROSH_MODEL_SCHEMA_HASH,
        rosh_run_id=_digest(9_000 + match_id) if rosh_available else None,
        rosh_evidence_hash=_digest(10_000 + match_id) if rosh_available else None,
        rosh_formula_version="formula-v1" if rosh_available else None,
        rosh_profile_hash=_digest(11_000 + match_id) if rosh_available else None,
        rosh_result_hash=_digest(12_000 + match_id) if rosh_available else None,
        rosh_coverage=0.9 if rosh_available else 0.0,
        rosh_features=tuple(rosh.items()),
    )


def _target(
    index: int,
    *,
    outcome: bool | None = None,
    rosh_available: bool = True,
    availability_mode: str = MODE,
) -> PrematchBacktestTarget:
    cutoff = START + timedelta(days=index)
    team_logit = (index % 5 - 2) * 0.2
    draft_signal = float(index % 7 - 3)
    rosh_signal = float((index * 3) % 9 - 4)
    actual = (
        team_logit + 0.6 * draft_signal + 0.25 * rosh_signal > 0.0
        if outcome is None
        else outcome
    )
    return PrematchBacktestTarget(
        match_id=index + 1,
        series_id=index // 2 + 1,
        event_id=f"event-{index // 8}",
        patch_id=f"7.{40 + index // 16}",
        prediction_cutoff=cutoff,
        completed_at=cutoff + timedelta(hours=1),
        result_usable_at=cutoff + timedelta(hours=2),
        cutoff_source="reconstructed_map_start",
        availability_mode=availability_mode,
        outcome=bool(actual),
        team_base_probability=_probability(team_logit),
        radiant_prior_probability=_probability((index - 5) / 50.0),
        snapshot=_snapshot(
            index + 1,
            cutoff,
            team_base_logit=team_logit,
            draft_signal=draft_signal,
            rosh_signal=rosh_signal,
            rosh_available=rosh_available,
            availability_mode=availability_mode,
        ),
        failure_reason=None,
    )


def _corpus(count: int = 12) -> PrematchCorpus:
    targets = tuple(_target(index, rosh_available=index < 3) for index in range(count))
    return PrematchCorpus(MODE, count, targets, dependency_revision=1)


def _run_payloads(result: object, match_id: int) -> dict[str, tuple[object, object]]:
    return {
        row.model_kind: (
            row.model_artifact.to_payload(),
            row.prediction.to_payload(),
        )
        for row in result.walk_forward_runs
        if row.target.match_id == match_id
    }


def test_walk_forward_uses_only_earlier_usable_results_and_exposes_oos_data() -> None:
    result = build_prematch_walk_forward(_corpus(), min_samples=4)

    assert result.backtest_version == PREMATCH_BACKTEST_VERSION
    assert len(result.walk_forward_runs) == 12 * len(PREMATCH_MODEL_KINDS)
    assert len(result.model_artifacts) == len(result.predictions) == 48
    assert tuple(row.model_artifact.model_kind for row in result.final_models) == (
        PREMATCH_MODEL_KINDS
    )
    for run in result.walk_forward_runs:
        assert all(
            row.match_id != run.target.match_id
            and row.prediction_cutoff < run.target.prediction_cutoff
            and row.completed_at < run.target.prediction_cutoff
            and row.result_usable_at <= run.target.prediction_cutoff
            for row in run.model_artifact.training_corpus
        )
        if run.prediction.status is PredictionStatus.PREDICTED:
            assert run.calibration_sample is not None
            assert run.calibration_sample.is_out_of_sample is True
            assert run.calibration_sample.model_hash == run.model_artifact.model_hash
        else:
            assert run.calibration_sample is None
    assert len(result.calibration_artifacts) == len(PREMATCH_MODEL_KINDS)
    assert all(
        all(sample.is_out_of_sample for sample in artifact.oos_samples)
        for artifact in result.calibration_artifacts
    )


def test_target_outcome_does_not_change_its_own_artifact_or_prediction() -> None:
    baseline = _corpus()
    changed_targets = list(baseline.targets)
    changed_targets[7] = replace(
        changed_targets[7], outcome=not changed_targets[7].outcome
    )
    changed = PrematchCorpus(MODE, baseline.formal_maps, tuple(changed_targets))

    first = build_prematch_walk_forward(baseline, min_samples=4)
    second = build_prematch_walk_forward(changed, min_samples=4)

    assert _run_payloads(first, 8) == _run_payloads(second, 8)


def test_future_target_does_not_change_historical_predictions() -> None:
    baseline = _corpus(10)
    extended = PrematchCorpus(MODE, 11, (*baseline.targets, _target(10)))

    first = build_prematch_walk_forward(baseline, min_samples=4)
    second = build_prematch_walk_forward(extended, min_samples=4)

    for match_id in range(1, 11):
        assert _run_payloads(first, match_id) == _run_payloads(second, match_id)


def test_unavailable_snapshot_remains_eligible_but_creates_no_model_run() -> None:
    available = _target(0)
    unavailable = PrematchBacktestTarget(
        match_id=2,
        series_id=1,
        event_id="event-0",
        patch_id="7.40",
        prediction_cutoff=START + timedelta(days=1),
        completed_at=START + timedelta(days=1, hours=1),
        result_usable_at=START + timedelta(days=1, hours=2),
        cutoff_source="reconstructed_map_start",
        availability_mode=MODE,
        outcome=False,
        team_base_probability=0.5,
        radiant_prior_probability=0.5,
        snapshot=None,
        failure_reason="team_rating_insufficient_evidence",
    )
    result = build_prematch_walk_forward(
        PrematchCorpus(MODE, 2, (available, unavailable)),
        min_samples=2,
    )

    assert result.corpus.eligible_targets == 2
    assert {row.target.match_id for row in result.walk_forward_runs} == {1}


def test_unavailable_target_uses_formal_map_timing_not_team_target_fields() -> None:
    prediction = SimpleNamespace(
        prediction_cutoff=START,
        raw_probability=0.5,
    )
    run = SimpleNamespace(
        artifact=SimpleNamespace(prediction=prediction),
        series_id=7,
        event_id="event-1",
        cutoff_source="reconstructed_map_start",
        availability_mode=MODE,
        eventual_radiant_win=True,
        radiant_prior_probability=0.5,
    )
    rating_map = SimpleNamespace(
        match_id=77,
        completed_at=START + timedelta(hours=1),
        result_usable_at=START + timedelta(hours=2),
        radiant_win=True,
    )

    target = prematch_backtest._unavailable_target(  # noqa: SLF001
        run,
        rating_map,
        patch_id=None,
        reason="team_rating_insufficient_evidence",
    )

    assert target.match_id == 77
    assert target.completed_at == START + timedelta(hours=1)
    assert target.result_usable_at == START + timedelta(hours=2)


def test_persistence_wrapper_saves_oos_and_final_records_and_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = build_prematch_walk_forward(_corpus(8), min_samples=2)
    report = build_prematch_report(result)
    calls: dict[str, object] = {}

    class FakeConnection:
        @contextmanager
        def transaction(self):
            yield

    connection = FakeConnection()

    def model_record(model, *, metrics=None):
        return SimpleNamespace(
            run_id=model.model_hash,
            model_hash=model.model_hash,
            metrics=metrics,
        )

    def calibration_record(artifact, *, model_hash):
        return SimpleNamespace(
            calibration_hash=artifact.calibration_hash,
            model_hash=model_hash,
        )

    def prediction_record(model, snapshot, *, cutoff_source, dependency_revision):
        prediction = prematch_backtest.predict_prematch(model, snapshot)
        return SimpleNamespace(
            run_id=model.model_hash,
            match_id=snapshot.match_id,
            status=(
                "predicted"
                if prediction.status is PredictionStatus.PREDICTED
                else "insufficient_evidence"
            ),
            dependency_revision=dependency_revision,
        )

    def validation_record(model_run, prediction, *, validated_at):
        return SimpleNamespace(run_id=model_run.run_id, match_id=prediction.match_id)

    def persist_records(connection_arg, **kwargs):
        calls["persist_connection"] = connection_arg
        calls["persist_kwargs"] = kwargs
        return SimpleNamespace(inserted=1)

    def settle(connection_arg, **kwargs):
        calls.setdefault("settlements", []).append(kwargs)
        return SimpleNamespace(updated=True, unchanged=False)

    monkeypatch.setattr(prematch_backtest, "_connection", lambda _value: connection)
    monkeypatch.setattr(
        prematch_storage, "build_prematch_model_run_record", model_record
    )
    monkeypatch.setattr(
        prematch_storage,
        "build_prematch_calibration_record",
        calibration_record,
    )
    monkeypatch.setattr(
        prematch_storage,
        "build_prematch_prediction_record",
        prediction_record,
    )
    monkeypatch.setattr(
        prematch_storage,
        "build_prematch_validation_record",
        validation_record,
    )
    monkeypatch.setattr(
        prematch_storage,
        "require_prematch_dependency_revision_current",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(prematch_storage, "persist_prematch_records", persist_records)
    monkeypatch.setattr(prematch_storage, "settle_prematch_prediction", settle)

    persisted = prematch_backtest.persist_prematch_backtest_result(
        result,
        object(),
        report=report,
        dry_run=False,
    )

    assert len(persisted.model_runs) >= len(result.final_models)
    assert len(persisted.predictions) == len(result.walk_forward_runs)
    assert len(persisted.validations) == len(result.walk_forward_runs)
    assert len(persisted.calibration_artifacts) == len(result.final_models)
    assert persisted.settled_predictions == sum(
        row.status == "predicted" for row in persisted.predictions
    )
    assert len(calls["settlements"]) == persisted.settled_predictions
    assert calls["persist_kwargs"]["dry_run"] is False
    records_by_hash = {row.run_id: row for row in persisted.model_runs}
    for final in result.final_models:
        metrics = records_by_hash[final.model_artifact.model_hash].metrics
        assert metrics["schema"] == "prematch-model-run-metrics/v1"
        assert metrics["bootstrap"]["samples"] == 1_000


def test_persistence_dry_run_does_not_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    result = build_prematch_walk_forward(_corpus(6), min_samples=2)
    report = build_prematch_report(result)

    class FakeConnection:
        @contextmanager
        def transaction(self):
            yield

    connection = FakeConnection()
    monkeypatch.setattr(prematch_backtest, "_connection", lambda _value: connection)
    monkeypatch.setattr(
        prematch_storage,
        "require_prematch_dependency_revision_current",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        prematch_storage,
        "build_prematch_model_run_record",
        lambda model, **_kwargs: SimpleNamespace(
            run_id=model.model_hash,
            model_hash=model.model_hash,
        ),
    )
    monkeypatch.setattr(
        prematch_storage,
        "build_prematch_calibration_record",
        lambda artifact, *, model_hash: SimpleNamespace(
            calibration_hash=artifact.calibration_hash,
            model_hash=model_hash,
        ),
    )
    monkeypatch.setattr(
        prematch_storage,
        "build_prematch_prediction_record",
        lambda model, snapshot, **_kwargs: SimpleNamespace(
            run_id=model.model_hash,
            match_id=snapshot.match_id,
            status="predicted",
        ),
    )
    monkeypatch.setattr(
        prematch_storage,
        "build_prematch_validation_record",
        lambda model_run, prediction, **_kwargs: SimpleNamespace(
            run_id=model_run.run_id,
            match_id=prediction.match_id,
        ),
    )
    monkeypatch.setattr(
        prematch_storage,
        "persist_prematch_records",
        lambda _connection, **kwargs: SimpleNamespace(**kwargs),
    )
    settle_calls: list[object] = []
    monkeypatch.setattr(
        prematch_storage,
        "settle_prematch_prediction",
        lambda *_args, **_kwargs: settle_calls.append(True),
    )

    persisted = prematch_backtest.persist_prematch_backtest_result(
        result,
        object(),
        report=report,
        dry_run=True,
    )

    assert persisted.settled_predictions == 0
    assert persisted.unchanged_settlements == 0
    assert settle_calls == []


def test_persistence_rejects_dependency_change_after_cpu_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = build_prematch_walk_forward(_corpus(6), min_samples=2)
    report = build_prematch_report(result)

    class FakeConnection:
        @contextmanager
        def transaction(self):
            yield

    persisted = False

    def reject_revision(*_args, **_kwargs):
        raise RuntimeError("prematch dependencies changed while result was rebuilding")

    def persist_records(*_args, **_kwargs):
        nonlocal persisted
        persisted = True
        raise AssertionError("persistence must not run")

    monkeypatch.setattr(
        prematch_backtest,
        "_connection",
        lambda _value: FakeConnection(),
    )
    monkeypatch.setattr(
        prematch_storage,
        "require_prematch_dependency_revision_current",
        reject_revision,
    )
    monkeypatch.setattr(
        prematch_storage,
        "persist_prematch_records",
        persist_records,
    )

    with pytest.raises(RuntimeError, match="dependencies changed"):
        prematch_backtest.persist_prematch_backtest_result(
            result,
            object(),
            report=report,
        )
    assert persisted is False


def test_corpus_rejects_mixed_evidence_modes() -> None:
    prospective = _target(0, availability_mode=AvailabilityMode.PROSPECTIVE.value)
    with pytest.raises(ValueError, match="cannot mix availability modes"):
        PrematchCorpus(MODE, 1, (prospective,))


def test_rosh_heroes_are_ordered_by_expected_position_and_missing_is_fail_closed() -> (
    None
):
    players = tuple(
        SimpleNamespace(
            expected_position=position,
            expected_position_confidence=1.0,
            hero_id=100 + position,
        )
        for position in (5, 2, 4, 1, 3)
    )
    assert prematch_backtest._heroes_by_expected_position(  # noqa: SLF001
        SimpleNamespace(players=players)
    ) == (101, 102, 103, 104, 105)
    incomplete = (
        *players[:-1],
        SimpleNamespace(
            expected_position=None,
            expected_position_confidence=0.0,
            hero_id=103,
        ),
    )
    assert (
        prematch_backtest._heroes_by_expected_position(  # noqa: SLF001
            SimpleNamespace(players=incomplete)
        )
        is None
    )
    low_confidence = (
        *players[:-1],
        SimpleNamespace(
            expected_position=3,
            expected_position_confidence=ROLE_CONFIDENCE_MIN - 0.01,
            hero_id=103,
        ),
    )
    assert (
        prematch_backtest._heroes_by_expected_position(  # noqa: SLF001
            SimpleNamespace(players=low_confidence)
        )
        is None
    )


def test_low_confidence_positions_use_replayable_rosh_unavailable_authority() -> (
    None
):
    radiant_players = tuple(
        SimpleNamespace(
            expected_position=position,
            expected_position_confidence=(
                ROLE_CONFIDENCE_MIN - 0.01 if position == 3 else 1.0
            ),
            hero_id=hero_id,
        )
        for position, hero_id in zip((1, 2, 3, 4, 5), (5, 1, 4, 2, 3), strict=True)
    )
    dire_players = tuple(
        SimpleNamespace(
            expected_position=position,
            expected_position_confidence=1.0,
            hero_id=hero_id,
        )
        for position, hero_id in zip(
            (1, 2, 3, 4, 5),
            (15, 11, 14, 12, 13),
            strict=True,
        )
    )
    target = SimpleNamespace(
        match_id=99,
        prediction_cutoff=START,
        availability_mode=AvailabilityMode.RECONSTRUCTED,
        radiant=SimpleNamespace(players=radiant_players),
        dire=SimpleNamespace(players=dire_players),
    )

    snapshot, authority = prematch_backtest._build_rosh_snapshot_with_authority(  # noqa: SLF001
        target,
        (),
        artifact_root="unused",
        match_links=(),
    )
    replayed = replay_rosh_feature_snapshot(
        authority,
        runs=(),
        artifact_root="unused",
    )

    assert replayed == snapshot
    assert snapshot.status == "unavailable"
    assert snapshot.missing_reason == "expected_positions_incomplete"
    assert snapshot.coverage == 0.0
    assert snapshot.run_id is None
    assert snapshot.profile_hash is None
    assert snapshot.result_hash is None
    assert all(
        getattr(snapshot, name) is None
        for name in ROSH_FEATURE_SCHEMA
        if name != "coverage"
    )


def test_formal_authorities_load_inside_one_transaction_and_replay_afterward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeConnection:
        in_read = False

        @contextmanager
        def transaction(self):
            self.in_read = True
            events.append("transaction:start")
            try:
                yield
            finally:
                self.in_read = False
                events.append("transaction:end")

    connection = FakeConnection()

    def team_loader(*_args, **_kwargs):
        assert connection.in_read
        events.append("team:load")
        return SimpleNamespace(formal_maps=0, maps=())

    def draft_loader(*_args, **_kwargs):
        assert connection.in_read
        events.append("draft:load")
        return SimpleNamespace(targets=(), maps=())

    def rosh_loader(*_args, **_kwargs):
        assert connection.in_read
        events.append("rosh:load")
        return (), ()

    def replay(_corpus):
        assert not connection.in_read
        events.append("team:replay")
        return ()

    monkeypatch.setattr(prematch_backtest, "_connection", lambda _value: connection)
    monkeypatch.setattr(prematch_backtest, "load_team_rating_corpus", team_loader)
    monkeypatch.setattr(prematch_backtest, "load_draft_corpus", draft_loader)
    monkeypatch.setattr(prematch_backtest, "_load_rosh_authority", rosh_loader)
    monkeypatch.setattr(
        prematch_backtest,
        "_prematch_dependency_revision",
        lambda _connection: 7,
    )
    monkeypatch.setattr(
        prematch_backtest, "build_team_rating_walk_forward_runs", replay
    )

    corpus = load_prematch_corpus(
        object(),
        artifact_root="unused",
        availability_mode=AvailabilityMode.RECONSTRUCTED,
    )

    assert corpus.formal_maps == 0
    assert corpus.dependency_revision == 7
    assert events == [
        "transaction:start",
        "team:load",
        "draft:load",
        "rosh:load",
        "transaction:end",
        "team:replay",
    ]
    assert not hasattr(PrematchBacktestTarget, "draft_authority_json")


def test_formal_corpus_fails_closed_when_dependency_revision_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        @contextmanager
        def transaction(self):
            yield

    connection = FakeConnection()
    revisions = iter((10, 11))
    monkeypatch.setattr(prematch_backtest, "_connection", lambda _value: connection)
    monkeypatch.setattr(
        prematch_backtest,
        "_prematch_dependency_revision",
        lambda _connection: next(revisions),
    )
    monkeypatch.setattr(
        prematch_backtest,
        "load_team_rating_corpus",
        lambda *_args, **_kwargs: SimpleNamespace(formal_maps=0),
    )
    monkeypatch.setattr(
        prematch_backtest,
        "load_draft_corpus",
        lambda *_args, **_kwargs: SimpleNamespace(targets=(), maps=()),
    )
    monkeypatch.setattr(
        prematch_backtest,
        "_load_rosh_authority",
        lambda *_args, **_kwargs: ((), ()),
    )

    with pytest.raises(ValueError, match="authority changed"):
        load_prematch_corpus(
            object(),
            artifact_root="unused",
            availability_mode=AvailabilityMode.RECONSTRUCTED,
        )
