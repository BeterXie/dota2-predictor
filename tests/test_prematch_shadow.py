from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import event_intelligence.prematch_shadow as shadow
from event_intelligence.draft_features import AvailabilityMode


UTC = timezone.utc
NOW = datetime(2026, 8, 5, tzinfo=UTC)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement, _params=()):
        return _Result(self.rows)


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
                "first_usable_at": (NOW - timedelta(minutes=1)).isoformat(),
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
    rows = []
    for index in range(200):
        outcome = index % 2 == 0
        rows.append(
            {
                "raw_probability": 0.75 if outcome else 0.25,
                "calibrated_probability": 0.8 if outcome else 0.2,
                "coverage": 0.9,
                "rosh_logit_delta": 0.1,
                "prediction_json": json.dumps({"missing_features": []}),
                "eventual_radiant_win": outcome,
                "result_usable_at": (NOW - timedelta(minutes=2)).isoformat(),
                "settled_at": (NOW - timedelta(minutes=1)).isoformat(),
                "status": "settled",
                "event_id": f"event-{index % 5}",
                "patch": f"7.4{index % 2}",
                "is_current": True,
            }
        )

    metrics = shadow.load_prematch_shadow_metrics(_Connection(tuple(rows)))
    decision = shadow.evaluate_prematch_prospective_gate(
        metrics,
        calibration_gate_passed=True,
        incremental_gate_passed=True,
    )

    assert metrics.settled_support == 200
    assert metrics.formal_events == 5
    assert metrics.patches == 2
    assert metrics.single_event_share == 0.2
    assert metrics.brier_score == pytest.approx(0.04)
    assert decision == shadow.PrematchProspectiveDecision("passed", ())
    assert shadow.evaluate_prematch_prospective_gate(
        metrics,
        calibration_gate_passed=False,
        incremental_gate_passed=False,
    ).status == "unsupported"
