from __future__ import annotations

from dataclasses import replace

import pytest

import live_betting.strategy_contract as strategy_contract_module
from live_betting.strategy_contract import (
    DecisionPayloadError,
    REGISTERED_STRATEGY_CONTRACTS,
    DEPLOYED_STRATEGY_VERSION,
    PROPOSED_STRATEGY_VERSION,
    SERIALIZATION_VERSION,
    PolicyEvaluation,
    build_strategy_contract,
    evaluate_policy_reason,
    parse_decision_payload,
    serialize_decision_payload,
    validate_strategy_contract,
)


def _evaluation(**overrides: object) -> PolicyEvaluation:
    defaults: dict[str, object] = {
        "observation_confirmed": True,
        "team_side_confirmed": True,
        "stream_unpaused": True,
        "market_surface_complete": True,
        "underdog_price": 3.0,
        "stable_two_snapshots": True,
        "situation_controllable": True,
        "situation_reason": "controlled_deficit",
        "rosh_lineup_available": True,
        "rosh_matches_draft": True,
        "rosh_minute_score_available": True,
        "entry_eligible": True,
        "entry_reason": "eligible",
        "draft_point_available": True,
        "draft_wait_reason": None,
        "draft_passes_live_gate": True,
        "data_quality": 0.8,
        "independent_positive": True,
        "edge": 0.12,
        "conservative_probability": 0.55,
        "market_probability": 0.4,
    }
    return PolicyEvaluation(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_default_contract_is_content_addressed_and_round_trips() -> None:
    contract = build_strategy_contract(strategy_version=PROPOSED_STRATEGY_VERSION)
    assert contract.strategy_version == PROPOSED_STRATEGY_VERSION
    assert contract.serialization_version == SERIALIZATION_VERSION
    assert len(contract.evaluator_hash) == 64
    assert len(contract.policy_hash) == 64
    registered = REGISTERED_STRATEGY_CONTRACTS[PROPOSED_STRATEGY_VERSION]
    assert contract.evaluator_hash == registered.evaluator_hash
    assert contract.policy_hash == registered.policy_hash
    assert contract.serialization_version == registered.serialization_version
    assert validate_strategy_contract(
        PROPOSED_STRATEGY_VERSION, contract.as_input_ref()
    ) == contract


def test_evaluator_identity_is_stable_across_windows_crlf_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read_bytes = strategy_contract_module.Path.read_bytes
    evaluator_files = {
        "canonical_comeback_evaluator.py",
        "comeback.py",
        "comeback_entry.py",
        "shadow_strategy.py",
    }

    def read_crlf(path: strategy_contract_module.Path) -> bytes:
        value = original_read_bytes(path)
        if path.name in evaluator_files:
            return value.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return value

    monkeypatch.setattr(strategy_contract_module.Path, "read_bytes", read_crlf)

    contract = build_strategy_contract(strategy_version=PROPOSED_STRATEGY_VERSION)
    registered = REGISTERED_STRATEGY_CONTRACTS[PROPOSED_STRATEGY_VERSION]
    assert contract.evaluator_hash == registered.evaluator_hash


def test_same_version_policy_or_evaluator_drift_fails_closed() -> None:
    contract = build_strategy_contract(strategy_version=PROPOSED_STRATEGY_VERSION)
    policy_drift = contract.as_input_ref()
    policy_drift["policy_artifact"]["parameters"]["minimum_edge"] = 0.01
    assert validate_strategy_contract(PROPOSED_STRATEGY_VERSION, policy_drift) is None

    evaluator_drift = contract.as_input_ref()
    evaluator_drift["evaluator_hash"] = "0" * 64
    assert validate_strategy_contract(PROPOSED_STRATEGY_VERSION, evaluator_drift) is None


def test_unregistered_policy_variant_is_rejected() -> None:
    with pytest.raises(ValueError, match="unregistered strategy policy variant"):
        build_strategy_contract(
            strategy_version=PROPOSED_STRATEGY_VERSION,
            minimum_edge=0.01,
        )


def test_evaluator_source_drift_with_same_version_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = strategy_contract_module._source_artifact()
    drifted = {**original, "entrypoint": "changed.without.new.version"}
    monkeypatch.setattr(strategy_contract_module, "_source_artifact", lambda: drifted)
    with pytest.raises(RuntimeError, match="registered strategy contract drift"):
        build_strategy_contract(strategy_version=PROPOSED_STRATEGY_VERSION)


def test_input_ref_does_not_expose_contract_mutable_artifacts() -> None:
    contract = build_strategy_contract(strategy_version=PROPOSED_STRATEGY_VERSION)
    first = contract.as_input_ref()
    first["policy_artifact"]["parameters"]["minimum_edge"] = 0.01
    first["evaluator_artifact"]["entrypoint"] = "tampered"

    second = contract.as_input_ref()
    assert second["policy_artifact"]["parameters"]["minimum_edge"] == 0.08
    assert second["evaluator_artifact"]["entrypoint"] != "tampered"
    assert validate_strategy_contract(PROPOSED_STRATEGY_VERSION, second) == contract


def test_deployed_v4_is_not_silently_authorized_as_v5() -> None:
    assert DEPLOYED_STRATEGY_VERSION not in REGISTERED_STRATEGY_CONTRACTS
    with pytest.raises(ValueError, match="unregistered strategy version"):
        build_strategy_contract(strategy_version=DEPLOYED_STRATEGY_VERSION)


def test_policy_reason_uses_single_reason_precedence() -> None:
    contract = build_strategy_contract(strategy_version=PROPOSED_STRATEGY_VERSION)
    assert evaluate_policy_reason(_evaluation(), contract.policy) == "eligible"
    assert evaluate_policy_reason(
        _evaluation(edge=0.01), contract.policy
    ) == "edge_below_threshold"
    assert evaluate_policy_reason(
        _evaluation(stable_two_snapshots=False, edge=0.01), contract.policy
    ) == "market_not_stable_two_snapshots"
    assert evaluate_policy_reason(
        _evaluation(map_already_attempted=True), contract.policy
    ) == "map_already_attempted"


def test_registered_decision_payload_uses_exact_rfc8785_serialization() -> None:
    payload = {"z": 0.0, "a": {"unicode": "逆转", "value": 1.0}}

    raw = serialize_decision_payload(
        payload, strategy_version=PROPOSED_STRATEGY_VERSION
    )

    assert raw == '{"a":{"unicode":"逆转","value":1},"z":0}'
    assert parse_decision_payload(
        raw, strategy_version=PROPOSED_STRATEGY_VERSION
    ) == {"a": {"unicode": "逆转", "value": 1}, "z": 0}


@pytest.mark.parametrize(
    ("raw", "reason"),
    (
        ('{"a":1,"a":1}', "decision_json_duplicate_key"),
        ('{"a":1,"a":2}', "decision_json_duplicate_key"),
        ('{"a": 1}', "decision_json_not_canonical"),
        ('{"b":1,"a":2}', "decision_json_not_canonical"),
        ('{"a":NaN}', "decision_json_non_finite_number"),
        ('{"a":Infinity}', "decision_json_non_finite_number"),
    ),
)
def test_registered_decision_payload_rejects_ambiguous_or_noncanonical_json(
    raw: str,
    reason: str,
) -> None:
    with pytest.raises(DecisionPayloadError) as raised:
        parse_decision_payload(
            raw, strategy_version=PROPOSED_STRATEGY_VERSION
        )
    assert raised.value.reason == reason
