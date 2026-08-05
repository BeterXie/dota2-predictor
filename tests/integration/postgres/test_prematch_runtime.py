from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from database.session import PostgresSession
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_MODEL_SCHEMA,
    DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
)
from event_intelligence.prematch_backtest import (
    PrematchBacktestTarget,
    PrematchCorpus,
    build_prematch_walk_forward,
    persist_prematch_backtest_result,
)
from event_intelligence.prematch_calibration import (
    PrematchCalibrationSample,
    build_prematch_calibration_artifact,
)
from event_intelligence.prematch_features import (
    PREMATCH_FEATURE_VERSION,
    PrematchFeatureSnapshot,
)
from event_intelligence.prematch_model import PrematchTrainingRow, fit_prematch_model
from event_intelligence.prematch_report import build_prematch_report
from event_intelligence.prematch_shadow import load_prematch_shadow_metrics
from event_intelligence.prematch_storage import (
    build_prematch_calibration_record,
    build_prematch_model_run_record,
    build_prematch_prediction_record,
    build_prematch_validation_record,
    current_prematch_lineage_revisions,
    load_prematch_model_artifact,
    persist_prematch_records,
    prematch_artifact_fingerprint,
    prematch_prediction_is_stale,
    settle_prematch_prediction,
)
from event_intelligence.raw_archive import canonical_json_bytes
from event_intelligence.rosh_features import (
    ROSH_FEATURE_SCHEMA,
    ROSH_MODEL_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
TRAINING_CUTOFF = START + timedelta(days=30)
TARGET_CUTOFF = TRAINING_CUTOFF + timedelta(days=1)
CREATED_AT = datetime(2026, 8, 5, tzinfo=UTC)
MODE = AvailabilityMode.RECONSTRUCTED.value

_INSERT_PREDICTION = text(
    """
    INSERT INTO prematch_predictions (
        run_id, match_id, prediction_cutoff, cutoff_source,
        input_snapshot_hash, artifact_fingerprint,
        dependency_fingerprint, dependency_revision,
        calibration_hash, team_base_probability, raw_probability,
        calibrated_probability, parameter_uncertainty,
        draft_logit_delta, rosh_logit_delta, cluster_logit_delta,
        total_adjustment, coverage, support, prediction_json,
        eventual_radiant_win, result_usable_at, settled_at, status,
        created_at
    ) VALUES (
        :run_id, :match_id, :prediction_cutoff, :cutoff_source,
        :input_snapshot_hash, :artifact_fingerprint,
        :dependency_fingerprint, :dependency_revision,
        :calibration_hash, :team_base_probability, :raw_probability,
        :calibrated_probability, :parameter_uncertainty,
        :draft_logit_delta, :rosh_logit_delta, :cluster_logit_delta,
        :total_adjustment, :coverage, :support, :prediction_json,
        :eventual_radiant_win, :result_usable_at, :settled_at, :status,
        :created_at
    )
    """
)


def _digest(number: int) -> str:
    return f"{number:064x}"


def _model(*, l2_regularization: float = 1.0):
    rows = tuple(
        PrematchTrainingRow(
            match_id=index + 1,
            input_snapshot_hash=_digest(index + 1),
            prediction_cutoff=START + timedelta(days=index),
            completed_at=START + timedelta(days=index, hours=1),
            result_usable_at=START + timedelta(days=index, hours=2),
            availability_mode=MODE,
            outcome=index % 2,
            series_id=f"series-{index // 2}",
            event_id="prematch-event",
            patch_id="7.40",
            team_base_logit=(index % 5 - 2) * 0.1,
            features={},
        )
        for index in range(24)
    )
    return fit_prematch_model(
        rows,
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=10,
        l2_regularization=l2_regularization,
    )


def _snapshot() -> PrematchFeatureSnapshot:
    draft = tuple((name, 0.0) for name in DRAFT_RESIDUAL_MODEL_SCHEMA)
    rosh: list[tuple[str, float | None]] = []
    for name in ROSH_FEATURE_SCHEMA:
        value = 0.0 if name == "coverage" else None
        rosh.append((name, value))
        rosh.append((f"{name}__missing", 0.0 if value is not None else 1.0))
    assert tuple(name for name, _value in rosh) == ROSH_MODEL_SCHEMA
    return PrematchFeatureSnapshot(
        match_id=100,
        prediction_cutoff=TARGET_CUTOFF,
        availability_mode=MODE,
        feature_version=PREMATCH_FEATURE_VERSION,
        team_base_logit=0.2,
        team_rating_run_id=_digest(101),
        team_rating_artifact_hash=_digest(102),
        team_rating_prediction_input_hash=_digest(103),
        team_rating_combined_training_input_hash=_digest(104),
        team_rating_support=24,
        draft_residual_input_hash=_digest(105),
        draft_residual_authority_fingerprint=_digest(106),
        draft_residual_team_rating_input_hash=_digest(107),
        draft_residual_feature_schema_hash=DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        draft_residual_model_schema_hash=DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
        draft_support=20,
        draft_coverage=0.8,
        draft_features=draft,
        rosh_status="unavailable",
        rosh_missing_reason="archive_missing",
        rosh_input_hash=_digest(108),
        rosh_model_schema_hash=ROSH_MODEL_SCHEMA_HASH,
        rosh_run_id=None,
        rosh_evidence_hash=None,
        rosh_formula_version=None,
        rosh_profile_hash=None,
        rosh_result_hash=None,
        rosh_coverage=0.0,
        rosh_features=tuple(rosh),
    )


def _seed_target(engine: Engine) -> None:
    start_unix = int(TARGET_CUTOFF.timestamp())
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO event_registry (
                    event_id, canonical_name, tier, prize_pool_usd,
                    main_event_start_at, main_event_end_at,
                    opendota_league_id, official_evidence_urls_json,
                    evidence_status, scope_policy_version, scope,
                    approval_status, approved_by, approved_at,
                    reconciliation_status, included_stages_json,
                    excluded_categories_json, created_at, updated_at
                ) VALUES (
                    'prematch-event', 'Prematch Event', 'tier_1', 1000000,
                    '2026-01-01T00:00:00Z', '2026-12-31T00:00:00Z',
                    99100, '[]', 'manually_audited', 'scope-v1',
                    'formal_main_event', 'approved', 'tester',
                    '2025-12-01T00:00:00Z', 'not_required', '[]', '[]',
                    '2025-12-01T00:00:00Z', '2025-12-01T00:00:00Z'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO matches (
                    match_id, radiant_team_id, dire_team_id, radiant_win,
                    duration, start_time, series_id
                ) VALUES (100, 10, 20, true, 3600, :start_time, 500)
                """
            ),
            {"start_time": start_unix},
        )
        connection.execute(
            text(
                """
                INSERT INTO match_ingest_status (
                    match_id, event_id, start_time, series_id, map_number,
                    has_valid_result, discovered_at, updated_at
                ) VALUES (
                    100, 'prematch-event', :start_time, 500, 1, 1,
                    '2026-02-01T00:00:00Z', '2026-02-01T00:00:00Z'
                )
                """
            ),
            {"start_time": start_unix},
        )


def _backtest_targets() -> tuple[PrematchBacktestTarget, ...]:
    logits = (-0.2, 0.2, -0.1, 0.1)
    targets: list[PrematchBacktestTarget] = []
    for index, team_base_logit in enumerate(logits):
        cutoff = TARGET_CUTOFF + timedelta(days=index)
        snapshot = replace(
            _snapshot(),
            match_id=100 + index,
            prediction_cutoff=cutoff,
            team_base_logit=team_base_logit,
            input_hash="",
        )
        targets.append(
            PrematchBacktestTarget(
                match_id=100 + index,
                series_id=500 + index // 2,
                event_id="prematch-event",
                patch_id="7.40",
                prediction_cutoff=cutoff,
                completed_at=cutoff + timedelta(hours=1),
                result_usable_at=cutoff + timedelta(hours=1, minutes=1),
                cutoff_source="reconstructed_map_start",
                availability_mode=MODE,
                outcome=index % 2 == 0,
                team_base_probability=1.0 / (1.0 + math.exp(-team_base_logit)),
                radiant_prior_probability=0.5,
                snapshot=snapshot,
                failure_reason=None,
            )
        )
    return tuple(targets)


def _seed_backtest_targets(
    engine: Engine,
    targets: tuple[PrematchBacktestTarget, ...],
    *,
    mismatch_match_id: int | None,
) -> None:
    _seed_target(engine)
    with engine.begin() as connection:
        for target in targets[1:]:
            radiant_win = (
                not target.outcome
                if target.match_id == mismatch_match_id
                else target.outcome
            )
            start_unix = int(target.prediction_cutoff.timestamp())
            connection.execute(
                text(
                    """
                    INSERT INTO matches (
                        match_id, radiant_team_id, dire_team_id, radiant_win,
                        duration, start_time, series_id
                    ) VALUES (
                        :match_id, 10, 20, :radiant_win, 3600,
                        :start_time, :series_id
                    )
                    """
                ),
                {
                    "match_id": target.match_id,
                    "radiant_win": radiant_win,
                    "start_time": start_unix,
                    "series_id": target.series_id,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO match_ingest_status (
                        match_id, event_id, start_time, series_id, map_number,
                        has_valid_result, discovered_at, updated_at
                    ) VALUES (
                        :match_id, 'prematch-event', :start_time, :series_id,
                        :map_number, 1,
                        '2026-02-01T00:00:00Z', '2026-02-01T00:00:00Z'
                    )
                    """
                ),
                {
                    "match_id": target.match_id,
                    "start_time": start_unix,
                    "series_id": target.series_id,
                    "map_number": target.match_id - 99,
                },
            )


def _records(session: PostgresSession):
    model = _model()
    snapshot = _snapshot()
    run = build_prematch_model_run_record(model)
    dependency_revision, _artifact_revision = current_prematch_lineage_revisions(
        session
    )
    prediction = build_prematch_prediction_record(
        model,
        snapshot,
        cutoff_source="reconstructed_map_start",
        dependency_revision=dependency_revision,
    )
    validation = build_prematch_validation_record(
        run,
        prediction,
        validated_at=CREATED_AT,
    )
    return model, run, prediction, validation


def _prediction_values(prediction, **overrides: object) -> dict[str, object]:
    values = {
        "run_id": prediction.run_id,
        "match_id": prediction.match_id,
        "prediction_cutoff": prediction.prediction_cutoff.isoformat(),
        "cutoff_source": prediction.cutoff_source,
        "input_snapshot_hash": prediction.input_snapshot_hash,
        "artifact_fingerprint": prediction.artifact_fingerprint,
        "dependency_fingerprint": prediction.dependency_fingerprint,
        "dependency_revision": prediction.dependency_revision,
        "calibration_hash": prediction.calibration_hash,
        "team_base_probability": prediction.team_base_probability,
        "raw_probability": prediction.raw_probability,
        "calibrated_probability": prediction.calibrated_probability,
        "parameter_uncertainty": prediction.parameter_uncertainty,
        "draft_logit_delta": prediction.draft_logit_delta,
        "rosh_logit_delta": prediction.rosh_logit_delta,
        "cluster_logit_delta": prediction.cluster_logit_delta,
        "total_adjustment": prediction.total_adjustment,
        "coverage": prediction.coverage,
        "support": prediction.support,
        "prediction_json": prediction.prediction_json,
        "eventual_radiant_win": prediction.eventual_radiant_win,
        "result_usable_at": prediction.result_usable_at,
        "settled_at": prediction.settled_at,
        "status": prediction.status,
        "created_at": CREATED_AT.isoformat(),
    }
    values.update(overrides)
    return values


def _supported_calibration(model):
    start = TRAINING_CUTOFF - timedelta(days=10)
    samples = tuple(
        PrematchCalibrationSample(
            match_id=2_000 + index,
            series_id=f"calibration-series-{index:03d}",
            event_id=f"calibration-event-{index % 5}",
            patch_id=f"7.4{index % 2}",
            model_kind=model.model_kind,
            availability_mode=MODE,
            prediction_cutoff=start + timedelta(hours=index),
            result_usable_at=start + timedelta(hours=index, minutes=30),
            raw_probability=0.15 if index % 2 == 0 else 0.85,
            outcome=index % 2,
            model_hash=_digest(700 + index // 10),
            input_snapshot_hash=_digest(800 + index),
        )
        for index in range(120)
    )
    artifact = build_prematch_calibration_artifact(
        samples,
        TRAINING_CUTOFF + timedelta(minutes=1),
        model_kind=model.model_kind,
        availability_mode=MODE,
    )
    return build_prematch_calibration_record(
        artifact,
        model_hash=model.model_hash,
    )


def _failed_calibration(model):
    start = TRAINING_CUTOFF - timedelta(days=10)
    samples = tuple(
        PrematchCalibrationSample(
            match_id=3_000 + index,
            series_id=f"failed-calibration-series-{index:03d}",
            event_id=f"calibration-event-{index % 5}",
            patch_id=f"7.4{index % 2}",
            model_kind=model.model_kind,
            availability_mode=MODE,
            prediction_cutoff=start + timedelta(hours=index),
            result_usable_at=start + timedelta(hours=index, minutes=30),
            raw_probability=0.15 if index % 2 == 0 else 0.85,
            outcome=index % 2 if index < 20 else 1 - index % 2,
            model_hash=_digest(900 + index // 10),
            input_snapshot_hash=_digest(1_000 + index),
        )
        for index in range(120)
    )
    artifact = build_prematch_calibration_artifact(
        samples,
        TRAINING_CUTOFF + timedelta(minutes=3),
        model_kind=model.model_kind,
        availability_mode=MODE,
    )
    assert artifact.status.value == "failed"
    assert artifact.parameters is not None
    return build_prematch_calibration_record(
        artifact,
        model_hash=model.model_hash,
    )


def test_postgres_persistence_replay_idempotency_and_controlled_settlement(
    postgres_engine: Engine,
) -> None:
    _seed_target(postgres_engine)
    session = PostgresSession(postgres_engine)
    try:
        model, run, prediction, validation = _records(session)
        first = persist_prematch_records(
            session,
            model_runs=(run,),
            predictions=(prediction,),
            validations=(validation,),
            created_at=CREATED_AT,
        )
        repeated = persist_prematch_records(
            session,
            model_runs=(run,),
            predictions=(prediction,),
            validations=(validation,),
            created_at=CREATED_AT + timedelta(days=1),
        )
        assert (
            first.inserted_model_runs,
            first.inserted_predictions,
            first.inserted_validations,
        ) == (1, 1, 1)
        assert (
            repeated.unchanged_model_runs,
            repeated.unchanged_predictions,
            repeated.unchanged_validations,
        ) == (1, 1, 1)
        assert load_prematch_model_artifact(session, run.run_id) == model

        usable = TARGET_CUTOFF + timedelta(hours=1, minutes=1)
        settled_at = usable + timedelta(minutes=1)
        changed = settle_prematch_prediction(
            session,
            run_id=run.run_id,
            match_id=100,
            eventual_radiant_win=True,
            result_usable_at=usable,
            settled_at=settled_at,
        )
        unchanged = settle_prematch_prediction(
            session,
            run_id=run.run_id,
            match_id=100,
            eventual_radiant_win=True,
            result_usable_at=usable,
            settled_at=settled_at,
        )
        assert changed.updated and unchanged.unchanged
        post_settlement = persist_prematch_records(
            session,
            predictions=(prediction,),
            created_at=CREATED_AT,
        )
        assert post_settlement.unchanged_predictions == 1
        with pytest.raises(ValueError, match="settlement conflict"):
            settle_prematch_prediction(
                session,
                run_id=run.run_id,
                match_id=100,
                eventual_radiant_win=False,
                result_usable_at=usable,
                settled_at=settled_at,
            )
    finally:
        session.close()


def test_supported_calibration_and_calibrated_prediction_persist_together(
    postgres_engine: Engine,
) -> None:
    _seed_target(postgres_engine)
    session = PostgresSession(postgres_engine)
    try:
        model = _model()
        run = build_prematch_model_run_record(model)
        calibration = _supported_calibration(model)
        dependency_revision, _artifact_revision = current_prematch_lineage_revisions(
            session
        )
        prediction = build_prematch_prediction_record(
            model,
            _snapshot(),
            cutoff_source="reconstructed_map_start",
            dependency_revision=dependency_revision,
            calibration=calibration,
        )
        validation = build_prematch_validation_record(
            run,
            prediction,
            validated_at=CREATED_AT,
        )
        counts = persist_prematch_records(
            session,
            model_runs=(run,),
            calibration_artifacts=(calibration,),
            predictions=(prediction,),
            validations=(validation,),
            created_at=CREATED_AT,
        )
        assert (
            counts.inserted_model_runs,
            counts.inserted_calibrations,
            counts.inserted_predictions,
            counts.inserted_validations,
        ) == (1, 1, 1, 1)
        stored = session.execute(
            """SELECT calibration_hash, calibrated_probability, status
                 FROM prematch_predictions WHERE run_id=? AND match_id=?""",
            (run.run_id, prediction.match_id),
        ).fetchone()
        assert stored is not None
        assert tuple(stored) == (
            calibration.calibration_hash,
            prediction.calibrated_probability,
            "predicted",
        )
    finally:
        session.close()


def test_direct_sql_rejects_prediction_claim_or_fingerprint_disagreements(
    postgres_engine: Engine,
) -> None:
    _seed_target(postgres_engine)
    session = PostgresSession(postgres_engine)
    try:
        _model_value, run, prediction, _validation = _records(session)
        persist_prematch_records(
            session,
            model_runs=(run,),
            created_at=CREATED_AT,
        )
    finally:
        session.close()

    payload = json.loads(prediction.prediction_json)
    cases = (
        ("status", {"status": "failed"}, {}),
        ("raw_probability", {"raw_probability": 0.0}, {}),
        (
            "parameter_uncertainty",
            {"parameter_uncertainty": prediction.parameter_uncertainty + 1.0},
            {},
        ),
        ("draft_logit_delta", {"draft_logit_delta": 0.25}, {}),
        ("rosh_logit_delta", {"rosh_logit_delta": 0.25}, {}),
        ("cluster_logit_delta", {"cluster_logit_delta": 0.25}, {}),
        (
            "total_adjustment",
            {"total_adjustment": prediction.total_adjustment + 0.25},
            {},
        ),
        ("team_base_probability", {}, {"team_base_probability": 0.25}),
        ("artifact_fingerprint", {}, {"artifact_fingerprint": _digest(999_999)}),
    )
    for _name, payload_overrides, column_overrides in cases:
        tampered = {**payload, **payload_overrides}
        values = _prediction_values(
            prediction,
            prediction_json=canonical_json_bytes(tampered).decode(),
            **column_overrides,
        )
        with pytest.raises(DBAPIError, match="prediction artifact disagrees"):
            with postgres_engine.begin() as connection:
                connection.execute(_INSERT_PREDICTION, values)

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM prematch_predictions")
        ).scalar_one() == 0


def test_validation_cannot_certify_prediction_with_contradictory_claims(
    postgres_engine: Engine,
) -> None:
    _seed_target(postgres_engine)
    session = PostgresSession(postgres_engine)
    try:
        _model_value, run, prediction, validation = _records(session)
        persist_prematch_records(
            session,
            model_runs=(run,),
            created_at=CREATED_AT,
        )
    finally:
        session.close()

    payload = json.loads(prediction.prediction_json)
    payload["raw_probability"] = 0.0
    contradictory = _prediction_values(
        prediction,
        prediction_json=canonical_json_bytes(payload).decode(),
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE prematch_predictions DISABLE TRIGGER "
                "prematch_predictions_mutation_guard"
            )
        )
        connection.execute(_INSERT_PREDICTION, contradictory)
        connection.execute(
            text(
                "ALTER TABLE prematch_predictions ENABLE TRIGGER "
                "prematch_predictions_mutation_guard"
            )
        )

    with pytest.raises(DBAPIError, match="claims are inconsistent"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO prematch_prediction_validations (
                        run_id, match_id, input_snapshot_hash,
                        artifact_fingerprint, dependency_fingerprint,
                        dependency_revision, validation_version, validated_at
                    ) VALUES (
                        :run_id, :match_id, :input_snapshot_hash,
                        :artifact_fingerprint, :dependency_fingerprint,
                        :dependency_revision, :validation_version, :validated_at
                    )
                    """
                ),
                {
                    "run_id": validation.run_id,
                    "match_id": validation.match_id,
                    "input_snapshot_hash": validation.input_snapshot_hash,
                    "artifact_fingerprint": validation.artifact_fingerprint,
                    "dependency_fingerprint": validation.dependency_fingerprint,
                    "dependency_revision": validation.dependency_revision,
                    "validation_version": validation.validation_version,
                    "validated_at": validation.validated_at.isoformat(),
                },
            )


def test_direct_sql_rejects_unusable_cross_model_or_noncausal_calibration(
    postgres_engine: Engine,
) -> None:
    _seed_target(postgres_engine)
    session = PostgresSession(postgres_engine)
    try:
        model_a, run_a, prediction_a, _validation = _records(session)
        model_b = _model(l2_regularization=2.0)
        run_b = build_prematch_model_run_record(model_b)
        supported_b = _supported_calibration(model_b)
        failed_a = _failed_calibration(model_a)
        unsupported_artifact = build_prematch_calibration_artifact(
            (),
            TRAINING_CUTOFF + timedelta(minutes=2),
            model_kind=model_a.model_kind,
            availability_mode=MODE,
        )
        unsupported_a = build_prematch_calibration_record(
            unsupported_artifact,
            model_hash=model_a.model_hash,
        )
        dependency_revision, _artifact_revision = (
            current_prematch_lineage_revisions(session)
        )
        calibrated_b = build_prematch_prediction_record(
            model_b,
            _snapshot(),
            cutoff_source="reconstructed_map_start",
            dependency_revision=dependency_revision,
            calibration=supported_b,
        )
        persist_prematch_records(
            session,
            model_runs=(run_a, run_b),
            calibration_artifacts=(supported_b, unsupported_a, failed_a),
            created_at=CREATED_AT,
        )
    finally:
        session.close()

    parameters = json.loads(supported_b.parameters_json)
    raw = prediction_a.raw_probability
    clipped = min(1.0 - 1e-15, max(1e-15, raw))
    score = parameters["a"] + parameters["b"] * (
        math.log(clipped) - math.log1p(-clipped)
    )
    cross_model_probability = 1.0 / (1.0 + math.exp(-score))
    attempts = (
        _prediction_values(
            prediction_a,
            calibration_hash=supported_b.calibration_hash,
            calibrated_probability=cross_model_probability,
            artifact_fingerprint=prematch_artifact_fingerprint(
                model_hash=model_a.model_hash,
                calibration_hash=supported_b.calibration_hash,
            ),
        ),
        _prediction_values(
            prediction_a,
            calibration_hash=unsupported_a.calibration_hash,
            calibrated_probability=raw,
            artifact_fingerprint=prematch_artifact_fingerprint(
                model_hash=model_a.model_hash,
                calibration_hash=unsupported_a.calibration_hash,
            ),
        ),
        _prediction_values(
            prediction_a,
            calibration_hash=failed_a.calibration_hash,
            calibrated_probability=raw,
            artifact_fingerprint=prematch_artifact_fingerprint(
                model_hash=model_a.model_hash,
                calibration_hash=failed_a.calibration_hash,
            ),
        ),
        _prediction_values(
            calibrated_b,
            prediction_cutoff=supported_b.evaluation_cutoff.isoformat(),
        ),
        _prediction_values(
            calibrated_b,
            calibrated_probability=(
                0.0 if calibrated_b.calibrated_probability > 0.5 else 1.0
            ),
        ),
    )
    for values in attempts:
        with pytest.raises(DBAPIError, match="prediction artifact disagrees"):
            with postgres_engine.begin() as connection:
                connection.execute(_INSERT_PREDICTION, values)


def test_postgres_conflict_rolls_back_prior_model_insert(
    postgres_engine: Engine,
) -> None:
    _seed_target(postgres_engine)
    session = PostgresSession(postgres_engine)
    try:
        _model_value, run, prediction, validation = _records(session)
        persist_prematch_records(
            session,
            model_runs=(run,),
            predictions=(prediction,),
            validations=(validation,),
            created_at=CREATED_AT,
        )
        second = build_prematch_model_run_record(_model(l2_regularization=2.0))
        conflict = replace(prediction, dependency_fingerprint="f" * 64)
        with pytest.raises(ValueError, match="prediction conflict"):
            persist_prematch_records(
                session,
                model_runs=(second,),
                predictions=(conflict,),
                created_at=CREATED_AT,
            )
        assert (
            session.execute("SELECT COUNT(*) FROM prematch_model_runs").scalar_one()
            == 1
        )
        assert (
            session.execute("SELECT COUNT(*) FROM prematch_predictions").scalar_one()
            == 1
        )
    finally:
        session.close()


def test_backtest_wrapper_rolls_back_every_record_when_later_settlement_fails(
    postgres_engine: Engine,
) -> None:
    targets = _backtest_targets()
    _seed_backtest_targets(
        postgres_engine,
        targets,
        mismatch_match_id=targets[-1].match_id,
    )
    session = PostgresSession(postgres_engine)
    try:
        revisions_before = current_prematch_lineage_revisions(session)
        result = build_prematch_walk_forward(
            PrematchCorpus(
                MODE,
                len(targets),
                targets,
                dependency_revision=revisions_before[0],
            ),
            min_samples=2,
        )
        predicted = tuple(
            row
            for row in result.walk_forward_runs
            if row.prediction.raw_probability is not None
        )
        assert len(predicted) >= 5
        report = build_prematch_report(result)
        with pytest.raises(DBAPIError, match="target result authority disagrees"):
            persist_prematch_backtest_result(
                result,
                session,
                report=report,
                created_at=CREATED_AT,
                validated_at=CREATED_AT,
            )
        assert current_prematch_lineage_revisions(session) == revisions_before
        for table in (
            "prematch_model_runs",
            "prematch_calibration_artifacts",
            "prematch_predictions",
            "prematch_prediction_validations",
        ):
            assert session.execute(f"SELECT COUNT(*) FROM {table}").scalar_one() == 0
    finally:
        session.close()


def test_backtest_wrapper_rejects_dependency_change_before_any_artifact_write(
    postgres_engine: Engine,
) -> None:
    targets = _backtest_targets()
    _seed_backtest_targets(
        postgres_engine,
        targets,
        mismatch_match_id=None,
    )
    session = PostgresSession(postgres_engine)
    try:
        load_revision, _artifact_revision = current_prematch_lineage_revisions(session)
        result = build_prematch_walk_forward(
            PrematchCorpus(
                MODE,
                len(targets),
                targets,
                dependency_revision=load_revision,
            ),
            min_samples=2,
        )
        report = build_prematch_report(result)
        _insert_team_rating_dependency(
            session,
            number=99,
            cutoff=result.evaluation_cutoff - timedelta(seconds=1),
        )
        with pytest.raises(RuntimeError, match="dependencies changed"):
            persist_prematch_backtest_result(
                result,
                session,
                report=report,
                created_at=CREATED_AT,
                validated_at=CREATED_AT,
            )
        for table in (
            "prematch_model_runs",
            "prematch_calibration_artifacts",
            "prematch_predictions",
            "prematch_prediction_validations",
        ):
            assert session.execute(f"SELECT COUNT(*) FROM {table}").scalar_one() == 0
    finally:
        session.close()


def _insert_team_rating_dependency(
    session: PostgresSession,
    *,
    number: int,
    cutoff: datetime,
) -> None:
    session.execute(
        """INSERT INTO team_rating_runs
           (run_id, rating_version, artifact_version, availability_mode,
            training_cutoff, configuration_json, training_input_hash,
            metrics_json, status, created_at)
           VALUES (?, 'team-rating-elo-v1', 'team-rating-artifact-v1',
                   'reconstructed_walk_forward', ?, '{}', ?, NULL,
                   'trained', '2026-08-05T00:00:00+00:00')""",
        (_digest(500 + number), cutoff.isoformat(), _digest(600 + number)),
    )
    session.commit()


def test_future_dependency_change_does_not_stale_but_past_change_does(
    postgres_engine: Engine,
) -> None:
    _seed_target(postgres_engine)
    session = PostgresSession(postgres_engine)
    try:
        _model_value, run, prediction, validation = _records(session)
        persist_prematch_records(
            session,
            model_runs=(run,),
            predictions=(prediction,),
            validations=(validation,),
            created_at=CREATED_AT,
        )
        assert not prematch_prediction_is_stale(
            session,
            run_id=run.run_id,
            match_id=100,
        )
        _insert_team_rating_dependency(
            session,
            number=1,
            cutoff=TARGET_CUTOFF + timedelta(days=1),
        )
        assert not prematch_prediction_is_stale(
            session,
            run_id=run.run_id,
            match_id=100,
        )
        current_revision, _artifact_revision = current_prematch_lineage_revisions(
            session
        )
        assert current_revision > prediction.dependency_revision
        repeated = persist_prematch_records(
            session,
            predictions=(
                replace(prediction, dependency_revision=current_revision),
            ),
            validations=(
                replace(validation, dependency_revision=current_revision),
            ),
            created_at=CREATED_AT,
        )
        assert (
            repeated.unchanged_predictions,
            repeated.unchanged_validations,
        ) == (1, 1)
        stored_revision = session.execute(
            """SELECT dependency_revision FROM prematch_predictions
                 WHERE run_id=? AND match_id=?""",
            (run.run_id, prediction.match_id),
        ).scalar_one()
        assert stored_revision == prediction.dependency_revision
        _insert_team_rating_dependency(
            session,
            number=2,
            cutoff=TARGET_CUTOFF - timedelta(seconds=1),
        )
        assert prematch_prediction_is_stale(
            session,
            run_id=run.run_id,
            match_id=100,
        )
    finally:
        session.close()


def test_current_revision_cannot_substitute_for_model_archive_replay(
    postgres_engine: Engine,
) -> None:
    fake_hash = "f" * 64
    artifact = canonical_json_bytes(
        {
            "artifact_version": "prematch-model-artifact-v1",
            "availability_mode": MODE,
            "feature_schema_hash": "b" * 64,
            "model_hash": fake_hash,
            "model_kind": "team_only",
            "model_version": "prematch-offset-logistic-l2-v1",
            "status": "trained",
            "training_cutoff": TRAINING_CUTOFF.isoformat(),
            "training_input_hash": "c" * 64,
        }
    ).decode()
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO prematch_model_runs (
                    run_id, model_version, artifact_version, model_kind,
                    availability_mode, training_cutoff, feature_schema_hash,
                    training_input_hash, model_hash, artifact_json,
                    metrics_json, status, created_at
                ) VALUES (
                    :hash, 'prematch-offset-logistic-l2-v1',
                    'prematch-model-artifact-v1', 'team_only', :mode,
                    :cutoff, :feature_hash, :training_hash, :hash,
                    :artifact, NULL, 'trained',
                    '2026-08-05T00:00:00+00:00'
                )
                """
            ),
            {
                "hash": fake_hash,
                "mode": MODE,
                "cutoff": TRAINING_CUTOFF.isoformat(),
                "feature_hash": "b" * 64,
                "training_hash": "c" * 64,
                "artifact": artifact,
            },
        )
    session = PostgresSession(postgres_engine)
    try:
        dependency_revision, artifact_revision = current_prematch_lineage_revisions(
            session
        )
        assert dependency_revision >= 1 and artifact_revision >= 1
        with pytest.raises(ValueError, match="keys|artifact|model"):
            load_prematch_model_artifact(session, fake_hash)
    finally:
        session.close()


def test_direct_sql_noncanonical_model_artifact_fails_application_load(
    postgres_engine: Engine,
) -> None:
    run = build_prematch_model_run_record(_model())
    noncanonical = json.dumps(json.loads(run.artifact_json), indent=2)
    assert noncanonical != run.artifact_json
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO prematch_model_runs (
                    run_id, model_version, artifact_version, model_kind,
                    availability_mode, training_cutoff, feature_schema_hash,
                    training_input_hash, model_hash, artifact_json,
                    metrics_json, status, created_at
                ) VALUES (
                    :run_id, :model_version, :artifact_version, :model_kind,
                    :availability_mode, :training_cutoff, :feature_schema_hash,
                    :training_input_hash, :model_hash, :artifact_json,
                    NULL, :status, :created_at
                )
                """
            ),
            {
                "run_id": run.run_id,
                "model_version": run.model_version,
                "artifact_version": run.artifact_version,
                "model_kind": run.model_kind,
                "availability_mode": run.availability_mode,
                "training_cutoff": run.training_cutoff.isoformat(),
                "feature_schema_hash": run.feature_schema_hash,
                "training_input_hash": run.training_input_hash,
                "model_hash": run.model_hash,
                "artifact_json": noncanonical,
                "status": run.status,
                "created_at": CREATED_AT.isoformat(),
            },
        )
    session = PostgresSession(postgres_engine)
    try:
        with pytest.raises(ValueError, match="canonical"):
            load_prematch_model_artifact(session, run.run_id)
    finally:
        session.close()


def test_prediction_update_outside_settlement_is_rejected(
    postgres_engine: Engine,
) -> None:
    _seed_target(postgres_engine)
    session = PostgresSession(postgres_engine)
    try:
        _model_value, run, prediction, validation = _records(session)
        persist_prematch_records(
            session,
            model_runs=(run,),
            predictions=(prediction,),
            validations=(validation,),
            created_at=CREATED_AT,
        )
    finally:
        session.close()
    with pytest.raises(DBAPIError, match="settlement transition"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE prematch_predictions SET coverage=0.5 WHERE run_id=:run_id"
                ),
                {"run_id": run.run_id},
            )
    assert math.isfinite(prediction.team_base_probability)


def test_shadow_metrics_query_runs_on_postgres(postgres_engine: Engine) -> None:
    session = PostgresSession(postgres_engine)
    try:
        metrics = load_prematch_shadow_metrics(session)
    finally:
        session.close()

    assert metrics.prediction_support == 0
    assert metrics.paired_support == 0
    assert metrics.cluster_status == "collecting"
