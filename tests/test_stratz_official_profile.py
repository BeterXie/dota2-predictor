from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

import prematch.stratz_official_profile as profile_module
from prematch.stratz_official_profile import (
    ACTIVE_PROFILE_ID,
    CANONICAL_PROFILE_HASH,
    ENDPOINT,
    FORMULA_VERSION,
    FROZEN_ARTIFACT_HASHES,
    PRESENTATION_ROUNDING,
    PROFILE_ID,
    REQUEST_PROFILE_ARTIFACT,
    REQUEST_PROFILE_HASH,
    SCORER_SOURCE_HASH,
    SERIALIZATION_VERSION,
    V1_STATE,
    V2_FORMULA_VERSION,
    V2_PROFILE_ID,
    V2_REQUEST_PROFILE_ARTIFACT,
    V2_REQUEST_PROFILE_HASH,
    V2_STATE,
    ProfileError,
    build_official_request_plan,
    canonical_bytes,
    compute_request_hash,
    get_profile,
    query_documents,
    utc_elapsed_days,
    validate_active_profile,
    validate_canonical_request_plan,
    validate_draft,
)


FIXTURE = Path(__file__).parent / "fixtures" / "stratz_official_rosh" / "8904419709"
ACTIVATION = FIXTURE.parent / "2026-07-28-v2" / "activation.json"
SCORER = Path(profile_module.__file__).with_name("stratz_official_score.py")
V1_REQUEST_PROFILE_HASH = "9dee18e1e74b14bb08761ade1db59b9c408abc03d8c4e797b0116bf45bc0fceb"


def _golden_input() -> dict:
    return {
        "mode": "historical_match",
        "match_id": 8904419709,
        "date_time": 1784485548,
        "bracket_ids": ["IMMORTAL"],
    }


def _draft_input() -> dict:
    return {
        "mode": "explicit_draft",
        "date_time": 1784485548,
        "bracket_ids": ["IMMORTAL"],
        "radiant": [{"hero_id": i, "position_id": i - 10} for i in range(11, 16)],
        "dire": [{"hero_id": i, "position_id": i - 15} for i in range(16, 21)],
    }


def _started() -> datetime:
    return datetime(2026, 7, 28, 17, 15, 39, 576000, tzinfo=timezone.utc)


def _plan():
    return build_official_request_plan(_golden_input(), request_started_at=_started())


def _independent_profile_projection(profile) -> dict:
    return {
        "schema": "stratz-rosh-profile-identity/v2",
        "state": profile.state,
        "rosh_profile_id": profile.rosh_profile_id,
        "formula_version": profile.formula_version,
        "request_profile_hash": profile.request_profile_hash,
        "upstream_bundle_hash": profile.upstream_bundle_hash,
        "scorer_source_hash": profile.scorer_source_hash,
        "serialization_version": profile.serialization_version,
        "endpoint": ENDPOINT,
        "presentation_rounding": PRESENTATION_ROUNDING,
        "scorer_thresholds": {
            "position_reliability_count": 1000,
            "synergy_reliability_count": 100,
            "time_rank_fallback_count": 1000,
        },
        "captured_artifacts": dict(FROZEN_ARTIFACT_HASHES),
    }


def test_v1_artifact_and_identity_remain_frozen_for_audit() -> None:
    assert PROFILE_ID == "stratz-rosh-web-2026-07-28-v1"
    assert FORMULA_VERSION == "stratz-official-rosh/2026-07-28-v1"
    assert REQUEST_PROFILE_HASH == V1_REQUEST_PROFILE_HASH
    assert REQUEST_PROFILE_HASH == hashlib.sha256(
        canonical_bytes(REQUEST_PROFILE_ARTIFACT)
    ).hexdigest()
    assert REQUEST_PROFILE_ARTIFACT["scorer_identity"] == {"status": "unactivated"}
    v1 = get_profile(PROFILE_ID)
    assert v1.request_profile_hash == V1_REQUEST_PROFILE_HASH
    assert v1.state == V1_STATE == "frozen/unactivated/superseded-for-implementation"


def test_default_profile_is_active_v2_and_v1_runtime_paths_fail_closed() -> None:
    active = get_profile()
    assert ACTIVE_PROFILE_ID == V2_PROFILE_ID
    assert active.rosh_profile_id == "stratz-rosh-web-2026-07-28-v2"
    assert active.formula_version == V2_FORMULA_VERSION
    assert active.state == V2_STATE == "active"
    validate_active_profile(active)
    with pytest.raises(ProfileError, match="RoshParityProfile"):
        validate_active_profile(None)  # type: ignore[arg-type]
    with pytest.raises(ProfileError, match="active v2"):
        validate_active_profile(get_profile(PROFILE_ID))
    with pytest.raises(ProfileError, match="active v2"):
        build_official_request_plan(
            _golden_input(),
            profile=get_profile(PROFILE_ID),
            request_started_at=_started(),
        )
    with pytest.raises(ProfileError, match="active v2"):
        validate_canonical_request_plan(replace(_plan(), profile=get_profile(PROFILE_ID)))


def test_queries_are_fixture_exact_and_hashes_match_manifest() -> None:
    requests = json.loads((FIXTURE / "requests.json").read_text(encoding="utf-8"))
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    documents = query_documents()
    for captured, registered in zip(requests, manifest["operations"], strict=True):
        query = documents[captured["operationName"]]
        assert query == captured["query"]
        assert hashlib.sha256(query.encode()).hexdigest() == registered["query_sha256"]
    assert [row["operationName"] for row in requests] == [
        "GetMatchPicksBans",
        "HeroesMetaPositions",
        "GetMatchCountPreviousWeekDay",
        "Synergy",
        "GetHeroStatsByTime",
        "GetHeroStatsByTime",
    ]


def test_v2_request_scorer_and_canonical_profile_hashes_recompute_independently() -> None:
    active = get_profile()
    assert V2_REQUEST_PROFILE_ARTIFACT["scorer_identity"] == {
        "status": "active",
        "sha256": SCORER_SOURCE_HASH,
    }
    assert V2_REQUEST_PROFILE_HASH != REQUEST_PROFILE_HASH
    assert V2_REQUEST_PROFILE_HASH == hashlib.sha256(
        canonical_bytes(V2_REQUEST_PROFILE_ARTIFACT)
    ).hexdigest()
    assert active.request_profile_hash == V2_REQUEST_PROFILE_HASH
    assert SCORER_SOURCE_HASH == hashlib.sha256(SCORER.read_bytes()).hexdigest()
    assert active.scorer_source_hash == SCORER_SOURCE_HASH
    independent = hashlib.sha256(
        canonical_bytes(_independent_profile_projection(active))
    ).hexdigest()
    assert active.canonical_profile_hash == CANONICAL_PROFILE_HASH == independent


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("state", "candidate", "not active"),
        ("request_profile_hash", None, "incomplete hash"),
        ("request_profile_hash", "", "incomplete hash"),
        ("request_profile_hash", "placeholder", "incomplete hash"),
        ("scorer_source_hash", "0" * 64, "scorer source hash drift"),
        ("canonical_profile_hash", "0" * 64, "canonical profile hash drift"),
    ],
)
def test_active_validator_rejects_candidate_placeholder_null_and_drift(
    field: str,
    value,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = replace(get_profile(), **{field: value})
    monkeypatch.setattr(
        profile_module,
        "PROFILE_REGISTRY",
        MappingProxyType({PROFILE_ID: get_profile(PROFILE_ID), V2_PROFILE_ID: drifted}),
    )
    with pytest.raises(ProfileError, match=message):
        validate_active_profile(drifted)


def test_registry_missing_conflicting_and_null_profiles_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = get_profile()
    monkeypatch.setattr(profile_module, "PROFILE_REGISTRY", {})
    with pytest.raises(ProfileError, match="missing"):
        profile_module.get_profile()
    monkeypatch.setattr(profile_module, "PROFILE_REGISTRY", {V2_PROFILE_ID: None})
    with pytest.raises(ProfileError, match="missing"):
        profile_module.get_profile()
    conflicting = replace(active, rosh_profile_id="conflicting")
    monkeypatch.setattr(profile_module, "PROFILE_REGISTRY", {V2_PROFILE_ID: conflicting})
    with pytest.raises(ProfileError, match="key drift"):
        profile_module.get_profile()


def test_activation_artifact_is_canonical_and_binds_all_real_artifacts() -> None:
    raw = ACTIVATION.read_bytes()
    activation = json.loads(raw)
    assert raw.rstrip(b"\r\n") == canonical_bytes(activation)
    assert activation["schema"] == "stratz-rosh-profile-activation/v1"
    assert activation["state"] == "active"
    assert activation["hash_rules"] == {
        "algorithm": "sha256",
        "canonical_profile_hash_input": (
            "complete-profile-identity-excluding-canonical_profile_hash"
        ),
        "file_hash_input": "raw-file-bytes",
        "json_canonicalization": SERIALIZATION_VERSION,
    }
    profile = activation["profile"]
    active = get_profile()
    assert profile == {
        "canonical_profile_hash": active.canonical_profile_hash,
        "formula_version": active.formula_version,
        "request_profile_hash": active.request_profile_hash,
        "rosh_profile_id": active.rosh_profile_id,
        "scorer_source_hash": active.scorer_source_hash,
        "serialization_version": active.serialization_version,
        "upstream_bundle_hash": active.upstream_bundle_hash,
    }
    assert activation["request_profile_artifact"]["sha256"] == V2_REQUEST_PROFILE_HASH
    artifacts = activation["frozen_artifacts"]
    assert len(artifacts) == 6
    assert {row["name"]: row["sha256"] for row in artifacts} == dict(
        FROZEN_ARTIFACT_HASHES
    )
    for row in artifacts:
        target = ACTIVATION.parent / row["path"]
        assert target.is_file()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == row["sha256"]


def test_v2_golden_plan_freezes_full_request_identity() -> None:
    plan = _plan()
    assert plan.profile == get_profile()
    assert plan.request_started_at == _started()
    assert [operation.index for operation in plan.operations] == list(range(6))
    assert [operation.operation_name for operation in plan.operations] == [
        "GetMatchPicksBans",
        "HeroesMetaPositions",
        "GetMatchCountPreviousWeekDay",
        "Synergy",
        "GetHeroStatsByTime",
        "GetHeroStatsByTime",
    ]
    assert [dict(operation.variables) for operation in plan.operations] == [
        {"matchId": 8904419709},
        {"bracketIds": ("IMMORTAL",), "take": 7, "skip": 8},
        {"bracketIds": ("IMMORTAL",)},
        {"bracketBasicIds": "DIVINE_IMMORTAL", "matchLimit": 0, "take": 200},
        {"week": 1784485548},
        {"bracketBasicIds": "DIVINE_IMMORTAL", "week": 1784485548},
    ]
    assert plan.week_anchors == (1784485548, 1783880748, 1783275948, 1782671148)
    assert plan.request_hash == compute_request_hash(plan)
    validate_canonical_request_plan(plan)


def test_explicit_draft_plan_is_canonical_and_active() -> None:
    plan = build_official_request_plan(
        _draft_input(),
        request_started_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    assert [operation.operation_name for operation in plan.operations] == [
        "HeroesMetaPositions",
        "GetMatchCountPreviousWeekDay",
        "Synergy",
        "GetHeroStatsByTime",
        "GetHeroStatsByTime",
    ]
    assert plan.operations[0].variables["heroIds"] == tuple(range(11, 21))
    assert plan.operations[2].variables["heroIds"] == tuple(range(11, 21))
    validate_canonical_request_plan(plan)


@pytest.mark.parametrize("drift", ["query", "query_sha", "request_hash", "variables", "order", "index"])
def test_each_request_identity_drift_fails_closed(drift: str) -> None:
    plan = _plan()
    operations = list(plan.operations)
    request_hash = plan.request_hash
    if drift == "query":
        operations[0] = replace(operations[0], query=operations[0].query + " ")
    elif drift == "query_sha":
        operations[0] = replace(operations[0], query_sha256="0" * 64)
    elif drift == "request_hash":
        request_hash = "0" * 64
    elif drift == "variables":
        operations[0] = replace(operations[0], variables={"matchId": 1})
    elif drift == "order":
        operations[0], operations[1] = operations[1], operations[0]
    else:
        operations[0] = replace(operations[0], index=99)
    drifted = replace(plan, operations=tuple(operations), request_hash=request_hash)
    with pytest.raises(ProfileError, match="request plan drift"):
        validate_canonical_request_plan(drifted)


def test_joint_query_query_sha_and_request_hash_drift_fails_closed() -> None:
    plan = _plan()
    operations = list(plan.operations)
    query = operations[0].query + "\n"
    operations[0] = replace(
        operations[0],
        query=query,
        query_sha256=hashlib.sha256(query.encode()).hexdigest(),
    )
    drifted = replace(plan, operations=tuple(operations))
    drifted = replace(drifted, request_hash=compute_request_hash(drifted))
    with pytest.raises(ProfileError, match="request plan drift"):
        validate_canonical_request_plan(drifted)


def test_joint_variables_and_request_hash_drift_fails_closed() -> None:
    plan = _plan()
    operations = list(plan.operations)
    operations[0] = replace(operations[0], variables={"matchId": 8904419710})
    drifted = replace(plan, operations=tuple(operations))
    drifted = replace(drifted, request_hash=compute_request_hash(drifted))
    with pytest.raises(ProfileError, match="request plan drift"):
        validate_canonical_request_plan(drifted)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("elapsed_days", 7),
        ("metadata_mode", "weekly"),
        ("current_day_shift", 1),
        ("week_anchors", (1, 2, 3, 4)),
    ],
)
def test_planning_metadata_drift_fails_closed(field: str, value) -> None:
    with pytest.raises(ProfileError, match="request plan drift"):
        validate_canonical_request_plan(replace(_plan(), **{field: value}))


def test_frozen_request_time_and_dynamic_date_anchor_boundaries() -> None:
    date_time = int(datetime(2026, 7, 19, 12, tzinfo=timezone.utc).timestamp())
    value = dict(_golden_input(), date_time=date_time)
    same_day = build_official_request_plan(
        value,
        request_started_at=datetime(2026, 7, 19, 23, 59, 59, tzinfo=timezone.utc),
    )
    next_day = build_official_request_plan(
        value,
        request_started_at=datetime(2026, 7, 20, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert (same_day.elapsed_days, same_day.current_day_shift) == (0, 1)
    assert (next_day.elapsed_days, next_day.current_day_shift) == (0, 0)
    assert same_day.week_anchors[0] == date_time - 604800
    assert next_day.week_anchors[0] == date_time
    validate_canonical_request_plan(same_day)
    validate_canonical_request_plan(next_day)
    with pytest.raises(ProfileError, match="request plan drift"):
        validate_canonical_request_plan(
            replace(next_day, request_started_at=next_day.request_started_at + timedelta(days=1))
        )
    with pytest.raises(ProfileError, match="missing its frozen"):
        validate_canonical_request_plan(replace(next_day, request_started_at=None))


def test_elapsed_day_bounds_are_utc_and_wall_clock_independent() -> None:
    date_time = 1784485548
    source = datetime.fromtimestamp(date_time, timezone.utc)
    assert utc_elapsed_days(date_time, source + timedelta(hours=23, minutes=59)) == 0
    assert utc_elapsed_days(date_time, source + timedelta(days=25, hours=23)) == 25
    with pytest.raises(ProfileError, match="future"):
        utc_elapsed_days(date_time, source - timedelta(seconds=1))
    build_official_request_plan(
        _golden_input(), request_started_at=source + timedelta(days=25, hours=23)
    )
    with pytest.raises(ProfileError, match="older than 25 days"):
        build_official_request_plan(
            _golden_input(), request_started_at=source + timedelta(days=26)
        )


def test_invalid_and_noncanonical_drafts_fail_closed() -> None:
    with pytest.raises(ProfileError):
        validate_draft(_draft_input()["radiant"][:-1], _draft_input()["dire"])
    duplicate = _draft_input()
    duplicate["dire"][0] = {"hero_id": 11, "position_id": 1}
    with pytest.raises(ProfileError):
        build_official_request_plan(duplicate, request_started_at=_started())
    with pytest.raises(ProfileError, match="only IMMORTAL"):
        build_official_request_plan(
            dict(_golden_input(), bracket_ids=["DIVINE"]),
            request_started_at=_started(),
        )
    shuffled = _draft_input()
    shuffled["radiant"] = list(reversed(shuffled["radiant"]))
    plan = build_official_request_plan(shuffled, request_started_at=_started())
    assert [row["position_id"] for row in plan.analysis_input.radiant] == list(range(1, 6))


def test_plan_is_deeply_immutable_and_request_time_is_required() -> None:
    with pytest.raises(TypeError):
        build_official_request_plan(_golden_input())  # type: ignore[call-arg]
    plan = build_official_request_plan(_draft_input(), request_started_at=_started())
    with pytest.raises(TypeError):
        plan.analysis_input.radiant[0]["hero_id"] = 999  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.operations[0].variables["heroIds"] = (1,)  # type: ignore[index]


def test_request_hash_projection_binds_query_text_and_explicit_index() -> None:
    plan = _plan()
    changed_query = list(plan.operations)
    changed_query[0] = replace(changed_query[0], query=changed_query[0].query + " ")
    changed_index = list(plan.operations)
    changed_index[0] = replace(changed_index[0], index=10)
    assert compute_request_hash(replace(plan, operations=tuple(changed_query))) != plan.request_hash
    assert compute_request_hash(replace(plan, operations=tuple(changed_index))) != plan.request_hash
    assert b"Authorization" not in canonical_bytes(
        {"endpoint": ENDPOINT, "operations": []}
    )


def test_real_active_v2_golden_replay_preserves_versioned_result_hash() -> None:
    from prematch.stratz_official_score import (
        normalize_official_responses,
        score_official_rosh,
    )

    responses = json.loads(
        (FIXTURE / "responses.sanitized.json").read_text(encoding="utf-8")
    )
    plan = _plan()
    normalized = normalize_official_responses(plan, responses)
    result = score_official_rosh(normalized, plan.profile)
    assert result.formula_version == V2_FORMULA_VERSION
    assert result.result_hash == (
        "dfede8ca305703bf175699e7b9d504f319601042f88ecb3cb6b102e39de7d593"
    )
