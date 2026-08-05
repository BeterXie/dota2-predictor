from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import event_intelligence.prematch_shadow as shadow
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.prematch_model import PrematchTrainingRow


UTC = timezone.utc
NOW = datetime(2026, 8, 5, tzinfo=UTC)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return None if not self._rows else self._rows[0]


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement, _params=()):
        return _Result(self.rows)


@dataclass(frozen=True)
class _Prediction:
    match_id: int
    prediction_cutoff: datetime
    raw_probability: float
    calibrated_probability: float
    team_base_probability: float
    prediction_json: str


def _settled_rows(
    count: int,
    *,
    candidate_better: bool,
) -> list[dict[str, object]]:
    rows = []
    start_time = int((NOW - timedelta(hours=1)).timestamp())
    for index in range(count):
        outcome = index % 2 == 0
        candidate = (
            (0.8 if outcome else 0.2)
            if candidate_better
            else (0.55 if outcome else 0.45)
        )
        team_only = 0.65 if outcome else 0.35
        event_id = f"event-{index % 5}"
        patch = f"7.4{index % 2}"
        rows.append(
            {
                "match_id": index + 1,
                "team_base_probability": team_only,
                "raw_probability": candidate,
                "calibrated_probability": candidate,
                "coverage": 0.9,
                "rosh_logit_delta": 0.1,
                "prediction_json": json.dumps(
                    {
                        "candidate_probability": candidate,
                        "team_only_probability": team_only,
                        "team_only_model_hash": "b" * 64,
                        "series_id": index // 2,
                        "event_id": event_id,
                        "patch": patch,
                        "missing_features": [],
                    }
                ),
                "eventual_radiant_win": outcome,
                "result_usable_at": (NOW - timedelta(minutes=2)).isoformat(),
                "settled_at": (NOW - timedelta(minutes=1)).isoformat(),
                "status": "settled",
                "event_id": event_id,
                "series_id": index // 2,
                "patch": patch,
                "start_time": start_time,
                "duration": 30 * 60,
                "is_current": True,
            }
        )
    return rows


def test_shadow_context_freezes_candidate_baseline_and_match_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prediction = _Prediction(
        match_id=42,
        prediction_cutoff=NOW,
        raw_probability=0.7,
        calibrated_probability=0.6,
        team_base_probability=0.55,
        prediction_json=json.dumps({"status": "predicted"}, separators=(",", ":")),
    )
    connection = _Connection(
        ({"series_id": 9, "event_id": "event-a", "patch": "7.41"},)
    )
    monkeypatch.setattr(
        shadow,
        "_team_only_comparison",
        lambda *_args, **_kwargs: (0.58, "b" * 64),
    )

    frozen = shadow._freeze_shadow_context(
        connection,
        prediction,
        SimpleNamespace(
            model_kind="team_plus_draft_rosh",
            model_hash="a" * 64,
        ),
        SimpleNamespace(),
    )
    payload = json.loads(frozen.prediction_json)

    assert payload == {
        "candidate_probability": 0.7,
        "candidate_with_cluster_model_hash": None,
        "candidate_with_cluster_probability": None,
        "candidate_without_cluster_model_hash": "a" * 64,
        "candidate_without_cluster_probability": 0.7,
        "cluster_candidate_logit_delta": None,
        "event_id": "event-a",
        "match_id": 42,
        "patch": "7.41",
        "prediction_cutoff": NOW.isoformat(),
        "series_id": 9,
        "status": "predicted",
        "team_only_probability": 0.58,
        "team_only_model_hash": "b" * 64,
        "top_cluster_contributions": [],
    }


def test_team_only_comparison_replays_candidate_corpus_with_same_fit_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    training_row = PrematchTrainingRow(
        match_id=1,
        input_snapshot_hash="a" * 64,
        prediction_cutoff=NOW - timedelta(days=2),
        completed_at=NOW - timedelta(days=2) + timedelta(hours=1),
        result_usable_at=NOW - timedelta(days=2) + timedelta(hours=2),
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
        outcome=True,
        series_id="series-a",
        event_id="event-a",
        patch_id="7.41",
        team_base_logit=0.2,
        features={"draft_signal": 1.0},
    )
    candidate = SimpleNamespace(
        model_kind="team_plus_draft_rosh",
        training_corpus=(SimpleNamespace(to_training_row=lambda: training_row),),
        training_cutoff=NOW - timedelta(days=1),
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
        min_samples=20,
        l2_regularization=2.5,
    )
    baseline = SimpleNamespace(model_hash="c" * 64)
    captured = {}

    def fake_fit(rows, training_cutoff, **kwargs):
        captured["rows"] = tuple(rows)
        captured["training_cutoff"] = training_cutoff
        captured.update(kwargs)
        return baseline

    monkeypatch.setattr(shadow, "fit_prematch_model", fake_fit)
    monkeypatch.setattr(
        shadow,
        "predict_prematch",
        lambda model, snapshot: (
            SimpleNamespace(raw_probability=0.57)
            if model is baseline and snapshot == "snapshot"
            else pytest.fail("wrong comparator prediction inputs")
        ),
    )

    probability, model_hash = shadow._team_only_comparison(
        candidate,
        "snapshot",
        candidate_probability=0.61,
    )

    assert probability == 0.57
    assert model_hash == "c" * 64
    assert captured["rows"][0].features == {}
    assert captured["training_cutoff"] == candidate.training_cutoff
    assert captured["model_kind"] == "team_only"
    assert captured["availability_mode"] == candidate.availability_mode
    assert captured["min_samples"] == candidate.min_samples
    assert captured["l2_regularization"] == candidate.l2_regularization


def test_reconstructed_deployment_cannot_enter_prospective_shadow() -> None:
    deployment = SimpleNamespace(
        availability_mode=AvailabilityMode.RECONSTRUCTED.value
    )
    with pytest.raises(ValueError, match="prospective deployment"):
        shadow.collect_prematch_shadow(
            _Connection(()),
            deployment,
            observed_at=NOW,
        )


def test_settlement_uses_only_cutoff_usable_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        shadow,
        "settle_prematch_prediction",
        lambda connection, **kwargs: (
            calls.append((connection, kwargs))
            or SimpleNamespace(updated=True, unchanged=False)
        ),
    )
    monkeypatch.setattr(shadow, "record_health", lambda *_args, **_kwargs: None)
    connection = _Connection(
        (
            {
                "run_id": "run-a",
                "match_id": 42,
                "radiant_win": True,
                "start_time": int((NOW - timedelta(hours=1)).timestamp()),
                "duration": 30 * 60,
                "artifact_id": "artifact-a",
                "content_hash": "a" * 64,
                "artifact_usable_at": (NOW - timedelta(minutes=5)).isoformat(),
                "observation_usable_at": (
                    NOW - timedelta(minutes=1)
                ).isoformat(),
            },
        )
    )

    result = shadow.settle_ready_prematch_shadows(
        connection,
        observed_at=NOW,
    )

    assert result == shadow.PrematchShadowSettlement(1, 1, 0)
    assert calls[0][1]["result_usable_at"] == NOW - timedelta(minutes=1)
    assert calls[0][1]["settled_at"] == NOW


def test_metrics_and_prospective_gate_use_settled_event_patch_support() -> None:
    rows = _settled_rows(200, candidate_better=True)

    metrics = shadow.load_prematch_shadow_metrics(_Connection(tuple(rows)))
    decision = shadow.evaluate_prematch_prospective_gate(
        metrics,
        calibration_gate_passed=True,
    )

    assert metrics.settled_support == 200
    assert metrics.formal_events == 5
    assert metrics.patches == 2
    assert metrics.single_event_share == 0.2
    assert metrics.brier_score == pytest.approx(0.04)
    assert metrics.paired_support == 200
    assert metrics.paired_brier.delta is not None
    assert metrics.paired_brier.delta < 0.0
    assert metrics.paired_brier.ci_90.upper is not None
    assert metrics.paired_brier.ci_90.upper < 0.0
    assert metrics.paired_log_loss.delta is not None
    assert metrics.paired_log_loss.delta < 0.0
    assert metrics.paired_log_loss.ci_90.upper is not None
    assert metrics.paired_log_loss.ci_90.upper < 0.0
    assert metrics.incremental_gate_passed
    assert decision == shadow.PrematchProspectiveDecision("passed", ())
    assert shadow.evaluate_prematch_prospective_gate(
        metrics,
        calibration_gate_passed=False,
    ).status == "failed"


def test_prospective_gate_collects_until_paired_support_is_sufficient() -> None:
    metrics = shadow.load_prematch_shadow_metrics(
        _Connection(tuple(_settled_rows(10, candidate_better=True)))
    )

    decision = shadow.evaluate_prematch_prospective_gate(
        metrics,
        calibration_gate_passed=True,
    )

    assert not metrics.incremental_gate_passed
    assert decision.status == "collecting"
    assert "paired_prospective_maps_below_200" in decision.reasons


def test_prospective_gate_rejects_non_improving_paired_intervals() -> None:
    metrics = shadow.load_prematch_shadow_metrics(
        _Connection(tuple(_settled_rows(200, candidate_better=False)))
    )

    decision = shadow.evaluate_prematch_prospective_gate(
        metrics,
        calibration_gate_passed=True,
    )

    assert not metrics.incremental_gate_passed
    assert metrics.paired_brier.delta is not None
    assert metrics.paired_brier.delta > 0.0
    assert decision.status == "failed"
    assert "prospective_incremental_gate_failed" in decision.reasons


def test_cluster_shadow_uses_same_cutoff_pair_and_collects_incremental_metrics() -> None:
    rows = _settled_rows(200, candidate_better=True)
    for row in rows:
        payload = json.loads(str(row["prediction_json"]))
        outcome = bool(row["eventual_radiant_win"])
        payload.update(
            {
                "candidate_without_cluster_probability": (
                    0.75 if outcome else 0.25
                ),
                "candidate_without_cluster_model_hash": "c" * 64,
                "candidate_with_cluster_probability": 0.85 if outcome else 0.15,
                "candidate_with_cluster_model_hash": "d" * 64,
                "cluster_feature_snapshot_hash": "e" * 64,
                "cluster_resource_hash": "f" * 64,
            }
        )
        row["prediction_json"] = json.dumps(payload)

    metrics = shadow.load_prematch_shadow_metrics(_Connection(tuple(rows)))

    assert metrics.cluster_paired_support == 200
    assert metrics.cluster_candidate_brier_score == pytest.approx(0.0225)
    assert metrics.no_cluster_candidate_brier_score == pytest.approx(0.0625)
    assert metrics.cluster_paired_brier.delta is not None
    assert metrics.cluster_paired_brier.delta < 0.0
    assert metrics.cluster_paired_brier.ci_90.upper is not None
    assert metrics.cluster_paired_brier.ci_90.upper < 0.0
    assert metrics.cluster_incremental_gate_passed
    assert metrics.cluster_status == "passed"


def test_cluster_shadow_marks_mature_non_improving_sample_failed() -> None:
    rows = _settled_rows(200, candidate_better=True)
    for row in rows:
        payload = json.loads(str(row["prediction_json"]))
        outcome = bool(row["eventual_radiant_win"])
        payload.update(
            {
                "candidate_without_cluster_probability": (
                    0.85 if outcome else 0.15
                ),
                "candidate_without_cluster_model_hash": "c" * 64,
                "candidate_with_cluster_probability": 0.75 if outcome else 0.25,
                "candidate_with_cluster_model_hash": "d" * 64,
                "cluster_feature_snapshot_hash": "e" * 64,
                "cluster_resource_hash": "f" * 64,
            }
        )
        row["prediction_json"] = json.dumps(payload)

    metrics = shadow.load_prematch_shadow_metrics(_Connection(tuple(rows)))

    assert metrics.cluster_paired_support == 200
    assert metrics.cluster_paired_brier.delta is not None
    assert metrics.cluster_paired_brier.delta > 0.0
    assert not metrics.cluster_incremental_gate_passed
    assert metrics.cluster_status == "failed"
