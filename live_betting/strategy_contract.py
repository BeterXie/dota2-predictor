"""Content-addressed executable contract for the comeback shadow strategy."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

import rfc8785

from .canonical_comeback_evaluator import (
    ComebackStrategyPolicy,
    PolicyEvaluation,
    evaluate_policy_reason,
    strategy_probability,
)
from .comeback_entry import ComebackEntryPolicy

if TYPE_CHECKING:
    from .market_state import MarketSurface
    from .models import RoshLineupScore
    from .profiles.draft_curve import DraftCurve
    from .profiles.player_form import PlayerForm
    from .profiles.team_style import TeamStyleProfile
    from .vision import VisionObservation


DEPLOYED_STRATEGY_VERSION = "comeback-shadow-v4-controlled-entry"
PROPOSED_STRATEGY_VERSION = "comeback-shadow-v5-executable-contract"
SERIALIZATION_VERSION = "rfc8785-jcs-v1"
EVALUATOR_VERSION = "comeback-shadow-canonical-evaluator-v2"
POLICY_SCHEMA = "dota2-comeback-strategy-policy-v1"
EVALUATOR_SCHEMA = "dota2-comeback-evaluator-artifact-v1"
CONTRACT_SCHEMA = "dota2-executable-strategy-contract-v1"
EVALUATOR_INPUT_SCHEMA = "dota2-comeback-evaluator-inputs-v1"


@dataclass(frozen=True)
class RegisteredStrategyIdentity:
    evaluator_hash: str
    policy_hash: str
    serialization_version: str


REGISTERED_STRATEGY_CONTRACTS: Mapping[str, RegisteredStrategyIdentity] = (
    MappingProxyType(
        {
            PROPOSED_STRATEGY_VERSION: RegisteredStrategyIdentity(
                evaluator_hash="c2d2f741e3b172b1fda1ca161619961e597070388d46d97848391b3f2f91ad24",
                policy_hash="6e0c8a278378ee4c070f5d11204ca23397f54c7b6b703b544adaf105a259d696",
                serialization_version=SERIALIZATION_VERSION,
            )
        }
    )
)


@dataclass(frozen=True)
class StrategyContract:
    strategy_version: str
    evaluator_version: str
    evaluator_hash: str
    serialization_version: str
    policy_hash: str
    policy_artifact: dict[str, Any]
    evaluator_artifact: dict[str, Any]
    policy: ComebackStrategyPolicy

    def as_input_ref(self) -> dict[str, Any]:
        return deepcopy({
            "schema": CONTRACT_SCHEMA,
            "strategy_version": self.strategy_version,
            "evaluator_version": self.evaluator_version,
            "evaluator_hash": self.evaluator_hash,
            "serialization_version": self.serialization_version,
            "policy_hash": self.policy_hash,
            "policy_artifact": self.policy_artifact,
            "evaluator_artifact": self.evaluator_artifact,
        })


@dataclass(frozen=True)
class ReplayResult:
    valid: bool
    reason: str
    expected_reason: str | None = None
    contract: StrategyContract | None = None


class DecisionPayloadError(ValueError):
    """Stable fail-closed reason for persisted decision JSON."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _DuplicateJsonKey(ValueError):
    pass


class _NonFiniteJsonNumber(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(value)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _NonFiniteJsonNumber(value)
    return parsed


def _reject_json_constant(value: str) -> None:
    raise _NonFiniteJsonNumber(value)


def parse_decision_payload(
    raw: str | bytes,
    *,
    strategy_version: str,
) -> dict[str, Any]:
    """Strictly parse decision JSON and enforce JCS for registered contracts."""

    try:
        raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        text = raw_bytes.decode("utf-8")
    except (AttributeError, UnicodeDecodeError, UnicodeEncodeError) as error:
        raise DecisionPayloadError("decision_json_invalid") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_float=_finite_json_float,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJsonKey as error:
        raise DecisionPayloadError("decision_json_duplicate_key") from error
    except _NonFiniteJsonNumber as error:
        raise DecisionPayloadError("decision_json_non_finite_number") from error
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DecisionPayloadError("decision_json_invalid") from error
    if not isinstance(value, dict):
        raise DecisionPayloadError("decision_json_root_not_object")
    if strategy_version in REGISTERED_STRATEGY_CONTRACTS:
        try:
            encoded = canonical_bytes(value)
        except (OverflowError, TypeError, ValueError) as error:
            raise DecisionPayloadError("decision_json_invalid") from error
        if encoded != raw_bytes:
            raise DecisionPayloadError("decision_json_not_canonical")
    return value


def serialize_decision_payload(
    value: Mapping[str, Any],
    *,
    strategy_version: str,
) -> str:
    """Serialize one registered decision payload as exact RFC 8785/JCS text."""

    if strategy_version not in REGISTERED_STRATEGY_CONTRACTS:
        raise ValueError("unregistered strategy version has no canonical write contract")
    try:
        encoded = canonical_bytes(dict(value))
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError("decision payload is not RFC 8785 serializable") from error
    # Keep the writer and every replay consumer on the exact same boundary.
    parse_decision_payload(encoded, strategy_version=strategy_version)
    return encoded.decode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_artifact() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    files = (
        "canonical_comeback_evaluator.py",
        "comeback.py",
        "comeback_entry.py",
        "shadow_strategy.py",
    )
    return {
        "schema": EVALUATOR_SCHEMA,
        "evaluator_version": EVALUATOR_VERSION,
        "entrypoint": "live_betting.shadow_strategy.ComebackShadowStrategy.evaluate",
        "policy_boundary": "live_betting.canonical_comeback_evaluator.evaluate_policy_reason",
        "source_files": [
            {
                "path": f"live_betting/{name}",
                "sha256": _sha256((root / name).read_bytes()),
            }
            for name in files
        ],
    }


def _policy_parameters(policy: ComebackStrategyPolicy) -> dict[str, Any]:
    value = asdict(policy)
    value["valid_economy_buckets"] = [f"{value}k" for value in range(1, 10)]
    value["invalid_economy_buckets"] = ["0k", "10k", "11k"]
    value["economy_bucket_width"] = 1_000
    return value


def build_strategy_contract(
    *,
    strategy_version: str,
    minimum_edge: float = ComebackStrategyPolicy.minimum_edge,
    stability_tolerance: float = ComebackStrategyPolicy.stability_tolerance,
) -> StrategyContract:
    policy = ComebackStrategyPolicy(
        minimum_edge=float(minimum_edge),
        stability_tolerance=float(stability_tolerance),
    )
    parameters = _policy_parameters(policy)
    default = ComebackStrategyPolicy()
    if policy != default:
        raise ValueError("unregistered strategy policy variant")
    if strategy_version not in REGISTERED_STRATEGY_CONTRACTS:
        raise ValueError("unregistered strategy version")
    policy_artifact = {
        "schema": POLICY_SCHEMA,
        "strategy_version": strategy_version,
        "serialization_version": SERIALIZATION_VERSION,
        "reason_precedence": [
            "vision_not_confirmed",
            "team_side_not_confirmed",
            "stream_paused_or_unknown",
            "market_surface_incomplete",
            "odds_outside_range",
            "market_not_stable_two_snapshots",
            "situation_reason",
            "rosh_lineup_score_unavailable",
            "rosh_lineup_draft_mismatch",
            "rosh_minute_score_unavailable",
            "entry_reason",
            "draft_wait_reason",
            "draft_landmark_support_or_calibration_failed",
            "insufficient_data_quality",
            "no_independent_positive_contribution",
            "edge_below_threshold",
            "conservative_probability_not_above_market",
            "transport_identity_missing_or_reused",
            "map_already_attempted",
        ],
        "parameters": parameters,
    }
    evaluator_artifact = _source_artifact()
    contract = StrategyContract(
        strategy_version=strategy_version,
        evaluator_version=EVALUATOR_VERSION,
        evaluator_hash=_sha256(canonical_bytes(evaluator_artifact)),
        serialization_version=SERIALIZATION_VERSION,
        policy_hash=_sha256(canonical_bytes(policy_artifact)),
        policy_artifact=policy_artifact,
        evaluator_artifact=evaluator_artifact,
        policy=policy,
    )
    registered = REGISTERED_STRATEGY_CONTRACTS.get(strategy_version)
    actual = RegisteredStrategyIdentity(
        evaluator_hash=contract.evaluator_hash,
        policy_hash=contract.policy_hash,
        serialization_version=contract.serialization_version,
    )
    if registered != actual:
        raise RuntimeError("registered strategy contract drift")
    return contract


def _policy_from_artifact(value: object) -> ComebackStrategyPolicy | None:
    if not isinstance(value, Mapping):
        return None
    expected_keys = {
        "schema",
        "strategy_version",
        "serialization_version",
        "reason_precedence",
        "parameters",
    }
    if set(value) != expected_keys or value.get("schema") != POLICY_SCHEMA:
        return None
    parameters = value.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    raw = dict(parameters)
    for key in (
        "valid_economy_buckets",
        "invalid_economy_buckets",
        "economy_bucket_width",
    ):
        raw.pop(key, None)
    entry = raw.get("entry")
    if not isinstance(entry, Mapping):
        return None
    try:
        raw["entry"] = ComebackEntryPolicy(**dict(entry))
        policy = ComebackStrategyPolicy(**raw)
    except (TypeError, ValueError):
        return None
    return policy if _policy_parameters(policy) == dict(parameters) else None


def validate_strategy_contract(
    strategy_version: str,
    value: object,
) -> StrategyContract | None:
    if not isinstance(value, Mapping):
        return None
    required = {
        "schema",
        "strategy_version",
        "evaluator_version",
        "evaluator_hash",
        "serialization_version",
        "policy_hash",
        "policy_artifact",
        "evaluator_artifact",
    }
    if set(value) != required or value.get("schema") != CONTRACT_SCHEMA:
        return None
    policy = _policy_from_artifact(value.get("policy_artifact"))
    if policy is None:
        return None
    if strategy_version not in REGISTERED_STRATEGY_CONTRACTS:
        return None
    try:
        expected = build_strategy_contract(
            strategy_version=strategy_version,
            minimum_edge=policy.minimum_edge,
            stability_tolerance=policy.stability_tolerance,
        )
    except (RuntimeError, ValueError):
        return None
    return expected if dict(value) == expected.as_input_ref() and strategy_version == expected.strategy_version else None


def decision_identity(
    *,
    raybet_match_id: str,
    map_number: int,
    decided_at: datetime,
    underdog_side: str,
    model_probability: float,
    reason: str,
    inputs: Mapping[str, Any],
    strategy_version: str,
) -> tuple[str, str]:
    payload = {
        "match": raybet_match_id,
        "map": map_number,
        "decided_at": decided_at.isoformat(),
        "side": underdog_side,
        "probability": round(model_probability, 10),
        "reason": reason,
        "inputs": _jsonable(inputs),
        "version": strategy_version,
    }
    canonical = (
        canonical_bytes(payload)
        if strategy_version in REGISTERED_STRATEGY_CONTRACTS
        else json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    )
    input_ref = hashlib.sha256(canonical).hexdigest()[:24]
    decision_key = hashlib.sha256(
        f"{raybet_match_id}|{map_number}|{input_ref}".encode()
    ).hexdigest()[:32]
    return decision_key, input_ref


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def canonical_evaluator_inputs(
    *,
    observation: VisionObservation,
    surface: MarketSurface,
    underdog_style: TeamStyleProfile,
    favorite_style: TeamStyleProfile,
    underdog_form: PlayerForm,
    favorite_form: PlayerForm,
    draft_curve: DraftCurve,
    decided_at: datetime,
    stable: bool,
    input_refs: Mapping[str, Any],
    rosh_lineup_score: RoshLineupScore | None,
) -> dict[str, Any]:
    return {
        "schema": EVALUATOR_INPUT_SCHEMA,
        "observation": _jsonable(asdict(observation)),
        "surface": _jsonable(asdict(surface)),
        "underdog_style": _jsonable(asdict(underdog_style)),
        "favorite_style": _jsonable(asdict(favorite_style)),
        "underdog_form": _jsonable(asdict(underdog_form)),
        "favorite_form": _jsonable(asdict(favorite_form)),
        "draft_curve": _jsonable(asdict(draft_curve)),
        "decided_at": decided_at.isoformat(),
        "stable": stable,
        "input_refs": _jsonable(dict(input_refs)),
        "rosh_lineup_score": (
            _jsonable(asdict(rosh_lineup_score))
            if rosh_lineup_score is not None
            else None
        ),
    }


def _exact_dataclass_values(cls: type[Any], value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("canonical evaluator object must be a mapping")
    expected = {item.name for item in fields(cls)}
    if set(value) != expected:
        raise ValueError("canonical evaluator object fields differ")
    return dict(value)


def _rebuild_canonical_evaluator_inputs(
    value: object,
) -> dict[str, Any]:
    from .market_state import MarketSurface
    from .models import RoshLineupScore
    from .profiles.draft_curve import DraftCurve, DraftPoint
    from .profiles.player_form import PlayerForm
    from .profiles.team_style import TeamStyleProfile
    from .vision import VisionComebackState, VisionObservation

    if not isinstance(value, Mapping) or set(value) != {
        "schema", "observation", "surface", "underdog_style",
        "favorite_style", "underdog_form", "favorite_form", "draft_curve",
        "decided_at", "stable", "input_refs", "rosh_lineup_score",
    } or value.get("schema") != EVALUATOR_INPUT_SCHEMA:
        raise ValueError("canonical evaluator inputs are malformed")

    observation_values = _exact_dataclass_values(
        VisionObservation, value["observation"]
    )
    state_values = _exact_dataclass_values(
        VisionComebackState, observation_values["comeback_state"]
    )
    observation_values["comeback_state"] = VisionComebackState(**state_values)
    observation_values["captured_at"] = datetime.fromisoformat(
        str(observation_values["captured_at"])
    )
    observation_values["radiant_hero_ids"] = tuple(
        observation_values["radiant_hero_ids"]
    )
    observation_values["dire_hero_ids"] = tuple(
        observation_values["dire_hero_ids"]
    )
    observation = VisionObservation(**observation_values)

    surface_values = _exact_dataclass_values(MarketSurface, value["surface"])
    surface_values["missing_markets"] = tuple(surface_values["missing_markets"])
    surface = MarketSurface(**surface_values)

    def profile(cls: type[Any], raw: object) -> Any:
        values = _exact_dataclass_values(cls, raw)
        if cls is PlayerForm:
            values["account_ids"] = tuple(values["account_ids"])
            if not isinstance(values["role_scores"], Mapping):
                raise ValueError("player role scores must be a mapping")
            values["role_scores"] = dict(values["role_scores"])
        return cls(**values)

    draft_values = _exact_dataclass_values(DraftCurve, value["draft_curve"])
    point_values = draft_values["points"]
    if not isinstance(point_values, list):
        raise ValueError("draft points must be a list")
    rebuilt_points = []
    for raw_point in point_values:
        raw = _exact_dataclass_values(DraftPoint, raw_point)
        raw["input_refs"] = tuple(raw["input_refs"])
        rebuilt_points.append(DraftPoint(**raw))
    draft_values["points"] = tuple(rebuilt_points)
    draft_curve = DraftCurve(**draft_values)

    raw_rosh = value["rosh_lineup_score"]
    rosh = None
    if raw_rosh is not None:
        rosh_values = _exact_dataclass_values(RoshLineupScore, raw_rosh)
        rosh_values["source_as_of"] = datetime.fromisoformat(
            str(rosh_values["source_as_of"])
        )
        if not isinstance(rosh_values["evidence"], Mapping):
            raise ValueError("Rosh evidence must be a mapping")
        rosh_values["evidence"] = dict(rosh_values["evidence"])
        rosh = RoshLineupScore(**rosh_values)

    input_refs = value["input_refs"]
    if not isinstance(input_refs, Mapping) or not isinstance(value["stable"], bool):
        raise ValueError("canonical evaluator controls are malformed")
    rebuilt = {
        "observation": observation,
        "surface": surface,
        "underdog_style": profile(TeamStyleProfile, value["underdog_style"]),
        "favorite_style": profile(TeamStyleProfile, value["favorite_style"]),
        "underdog_form": profile(PlayerForm, value["underdog_form"]),
        "favorite_form": profile(PlayerForm, value["favorite_form"]),
        "draft_curve": draft_curve,
        "decided_at": datetime.fromisoformat(str(value["decided_at"])),
        "stable": value["stable"],
        "input_refs": dict(input_refs),
        "rosh_lineup_score": rosh,
    }
    if canonical_bytes(canonical_evaluator_inputs(**rebuilt)) != canonical_bytes(dict(value)):
        raise ValueError("canonical evaluator inputs do not round trip")
    return rebuilt


def replay_persisted_decision(row: Mapping[str, Any]) -> ReplayResult:
    try:
        strategy_version = str(row["strategy_version"])
        payload = parse_decision_payload(
            str(row["contributions_json"]),
            strategy_version=strategy_version,
        )
        inputs = payload["__inputs__"]
        contract = validate_strategy_contract(
            strategy_version, inputs["strategy_contract"]
        )
    except DecisionPayloadError as error:
        return ReplayResult(False, error.reason)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ReplayResult(False, "strategy_contract_or_inputs_invalid")
    if contract is None:
        return ReplayResult(False, "strategy_contract_invalid")
    try:
        evaluator_inputs = _rebuild_canonical_evaluator_inputs(
            inputs["canonical_evaluator_inputs"]
        )
        from .comeback import score_comeback

        replayed = score_comeback(
            **evaluator_inputs,
            strategy_version=contract.strategy_version,
            min_edge=contract.policy.minimum_edge,
        )
    except (KeyError, TypeError, ValueError, RuntimeError, OverflowError):
        return ReplayResult(False, "canonical_evaluator_inputs_invalid", contract=contract)
    raw = {key: value for key, value in payload.items() if not key.startswith("__")}
    conservative = payload.get("__conservative__")
    if not isinstance(conservative, Mapping):
        return ReplayResult(False, "conservative_contributions_missing", contract=contract)
    try:
        raw_numbers = {str(key): float(value) for key, value in raw.items()}
        conservative_numbers = {str(key): float(value) for key, value in conservative.items()}
        market_probability = float(row["market_probability"])
        model_probability = float(row["model_probability"])
        edge = float(row["edge"])
        data_quality = float(row["data_quality"])
        decided_at = datetime.fromisoformat(str(row["decided_at"]))
    except (TypeError, ValueError):
        return ReplayResult(False, "persisted_numeric_identity_invalid", contract=contract)
    expected_model = replayed.model_probability
    expected_conservative = replayed.conservative_probability
    if (
        not math.isclose(model_probability, expected_model, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(edge, model_probability - market_probability, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(replayed.market_probability, market_probability, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(replayed.edge, edge, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(replayed.data_quality, data_quality, rel_tol=1e-9, abs_tol=1e-9)
        or raw_numbers != replayed.contributions
        or conservative_numbers != replayed.inputs.get("conservative_contributions")
        or canonical_bytes(_jsonable(inputs))
        != canonical_bytes(_jsonable(replayed.inputs))
    ):
        return ReplayResult(False, "persisted_evaluator_output_mismatch", contract=contract)
    expected_reason = replayed.reason
    expected_eligible = replayed.eligible
    if str(row["reason"]) != expected_reason or bool(row["eligible"]) is not expected_eligible:
        return ReplayResult(False, "persisted_policy_result_mismatch", expected_reason, contract)
    if (
        str(row["raybet_match_id"]) != replayed.raybet_match_id
        or int(row["map_number"]) != replayed.map_number
        or decided_at != replayed.decided_at
        or str(row["underdog_side"]) != replayed.underdog_side
        or str(row["strategy_version"]) != replayed.strategy_version
        or row["decision_key"] != replayed.decision_key
        or row["input_ref"] != replayed.input_ref
    ):
        return ReplayResult(False, "persisted_decision_identity_mismatch", expected_reason, contract)
    return ReplayResult(True, "replayed", expected_reason, contract)


def persisted_decision_projection_failure(
    row: Mapping[str, Any],
) -> str | None:
    """Return why a decision must be excluded from report/monitor projections."""

    try:
        strategy_version = str(row["strategy_version"])
        payload = parse_decision_payload(
            str(row["contributions_json"]),
            strategy_version=strategy_version,
        )
    except (KeyError, TypeError) as error:
        return "decision_json_invalid"
    except DecisionPayloadError as error:
        return error.reason
    if strategy_version not in REGISTERED_STRATEGY_CONTRACTS:
        return None
    inputs = payload.get("__inputs__")
    if not isinstance(inputs, Mapping):
        return "strategy_contract_or_inputs_invalid"
    contract = validate_strategy_contract(
        strategy_version, inputs.get("strategy_contract")
    )
    if contract is None:
        return "strategy_contract_invalid"
    contributions = {
        key: value for key, value in payload.items() if not key.startswith("__")
    }
    conservative = payload.get("__conservative__")
    scored = bool(contributions) or bool(conservative) or (
        "canonical_evaluator_inputs" in inputs
    ) or bool(row["eligible"])
    if not scored:
        return None
    replay = replay_persisted_decision(row)
    return None if replay.valid else replay.reason
