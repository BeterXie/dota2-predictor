from __future__ import annotations

import copy
import gzip
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

import event_intelligence.rosh_features as rosh_features
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.rosh_features import (
    ROSH_FEATURE_SCHEMA,
    ROSH_FEATURE_VERSION,
    ROSH_MODEL_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
    ROSH_UNAVAILABLE_AUTHORITY_SCHEMA,
    RoshFeatureTarget,
    RoshProspectiveTimingAuthority,
    RoshRequestPlanWitness,
    RoshResponseTiming,
    build_rosh_feature_snapshot,
    build_rosh_feature_snapshot_with_authority,
    build_unavailable_rosh_feature_snapshot_with_authority,
    project_rosh_features,
    replay_rosh_feature_snapshot,
)
from live_betting.rosh_evidence import official_rosh_draft_hash
from live_betting.rosh_parity import ExactByteArtifactStore
from live_betting.rosh_parity_storage import (
    RoshHeroScoreRecord,
    RoshMinutePointRecord,
    RoshRunMatchLink,
    RoshRunRecord,
    StoredRoshRun,
)
from live_betting.stratz_rosh_client import OfficialRoshBatch
from prematch.stratz_official_profile import (
    build_official_request_plan,
    canonical_bytes,
    get_profile,
)
from prematch.stratz_official_score import (
    ALL_RANK_FALLBACK,
    DIVINE_IMMORTAL,
    DraftSlot,
    MinutePoint,
    MinuteSlot,
    NormalizedRoshInputs,
    OfficialRoshResult,
    PositionAggregate,
    normalize_official_responses,
    score_official_rosh,
)


UTC = timezone.utc
STARTED_AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
COLLECTED_AT = STARTED_AT + timedelta(seconds=5)
DATE_TIME = 1784485548
MATCH_ID = 8904419709
RADIANT = (54, 120, 28, 90, 123)
DIRE = (145, 74, 96, 79, 87)
FIXTURE = Path(__file__).parent / "fixtures" / "stratz_official_rosh" / str(MATCH_ID)


@dataclass
class _RunBundle:
    stored: StoredRoshRun
    artifact_root: Path


def _historical_input() -> dict[str, object]:
    return {
        "mode": "historical_match",
        "match_id": MATCH_ID,
        "date_time": DATE_TIME,
        "bracket_ids": ["IMMORTAL"],
    }


def _explicit_input() -> dict[str, object]:
    return {
        "mode": "explicit_draft",
        "date_time": DATE_TIME,
        "bracket_ids": ["IMMORTAL"],
        "radiant": [
            {"hero_id": hero_id, "position_id": position_id}
            for position_id, hero_id in enumerate(RADIANT, 1)
        ],
        "dire": [
            {"hero_id": hero_id, "position_id": position_id}
            for position_id, hero_id in enumerate(DIRE, 1)
        ],
    }


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _plan_body(plan: Any) -> bytes:
    return json.dumps(
        [
            {
                "operationName": operation.operation_name,
                "variables": _json_value(operation.variables),
                "query": operation.query,
            }
            for operation in plan.operations
        ],
        separators=(",", ":"),
    ).encode()


def _historical_batch() -> OfficialRoshBatch:
    request = (FIXTURE / "requests.json").read_bytes()
    response = (FIXTURE / "responses.sanitized.json").read_bytes()
    return OfficialRoshBatch(
        request_body=request,
        response_body=response,
        responses=tuple(json.loads(response)),
        collected_at=COLLECTED_AT,
        diagnostics={},
    )


def _explicit_batch() -> OfficialRoshBatch:
    plan = build_official_request_plan(
        _explicit_input(),
        request_started_at=STARTED_AT,
    )
    responses = tuple(
        json.loads((FIXTURE / "responses.sanitized.json").read_bytes())[1:]
    )
    response_body = json.dumps(responses, separators=(",", ":")).encode()
    return OfficialRoshBatch(
        request_body=_plan_body(plan),
        response_body=response_body,
        responses=responses,
        collected_at=COLLECTED_AT,
        diagnostics={},
    )


def _run(tmp_path: Path, *, prospective: bool = False) -> _RunBundle:
    analysis_input = _explicit_input() if prospective else _historical_input()
    plan = build_official_request_plan(
        analysis_input,
        request_started_at=STARTED_AT,
    )
    batch = _explicit_batch() if prospective else _historical_batch()
    normalized = normalize_official_responses(plan, batch.responses)
    profile = get_profile()
    result = score_official_rosh(normalized, profile)
    artifact_root = tmp_path / "artifacts"
    artifacts = ExactByteArtifactStore(artifact_root)
    request_receipt = artifacts.persist(batch.request_body)
    response_receipt = artifacts.persist(batch.response_body)
    request_artifact = {
        "content_sha256": request_receipt.content_sha256,
        "gzip_sha256": request_receipt.gzip_sha256,
        "relative_path": request_receipt.relative_path,
        "byte_count": request_receipt.byte_count,
    }
    request_manifest = {
        "schema": "rosh-request-manifest/v1",
        "request_hash": plan.request_hash,
        "request_body_sha256": hashlib.sha256(batch.request_body).hexdigest(),
        "operations": [
            {
                "index": operation.index,
                "operation_name": operation.operation_name,
                "query_sha256": operation.query_sha256,
                "variables": _json_value(operation.variables),
            }
            for operation in plan.operations
        ],
        "request_artifact": request_artifact,
    }
    collected_at = COLLECTED_AT.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    response_manifest = tuple(
        {
            "operation_name": operation.operation_name,
            "operation_index": operation.index,
            "request_artifact_hash": request_receipt.content_sha256,
            "response_artifact_hash": response_receipt.content_sha256,
            "collected_at": collected_at,
            "relative_path": response_receipt.relative_path,
            "request_relative_path": request_receipt.relative_path,
            "response_gzip_sha256": response_receipt.gzip_sha256,
        }
        for operation in plan.operations
    )
    draft = {
        side.lower(): [
            {"hero_id": slot.hero_id, "position_id": slot.position_id}
            for slot in normalized.draft
            if slot.team_side == side
        ]
        for side in ("RADIANT", "DIRE")
    }
    draft_hash = official_rosh_draft_hash(RADIANT, DIRE)
    analysis_identity = {
        "schema": "rosh-analysis-identity/v1",
        "mode": plan.analysis_input.mode,
        "match_id": plan.analysis_input.match_id,
        "date_time": plan.analysis_input.date_time,
        "draft_hash": draft_hash,
        "request_hash": plan.request_hash,
        "profile": {
            "rosh_profile_id": profile.rosh_profile_id,
            "formula_version": profile.formula_version,
            "request_profile_hash": profile.request_profile_hash,
            "upstream_bundle_hash": profile.upstream_bundle_hash,
            "scorer_source_hash": profile.scorer_source_hash,
            "canonical_profile_hash": profile.canonical_profile_hash,
            "serialization_version": profile.serialization_version,
        },
    }
    evidence_hash = hashlib.sha256(
        canonical_bytes(
            {
                "schema": "rosh-analysis-evidence/v1",
                "analysis_identity": analysis_identity,
                "request_artifact_hash": request_receipt.content_sha256,
                "response_artifact_hash": response_receipt.content_sha256,
                "result_hash": result.result_hash,
                "status": "succeeded",
            }
        )
    ).hexdigest()
    run_id = hashlib.sha256(
        canonical_bytes(
            {
                "schema": "rosh-analysis-run-id/v1",
                "evidence_hash": evidence_hash,
                "status": "succeeded",
            }
        )
    ).hexdigest()
    run = RoshRunRecord(
        run_id=run_id,
        status="succeeded",
        mode=plan.analysis_input.mode,
        match_id=plan.analysis_input.match_id,
        date_time=plan.analysis_input.date_time,
        draft_hash=draft_hash,
        draft=draft,
        rosh_profile_id=profile.rosh_profile_id,
        formula_version=profile.formula_version,
        request_profile_hash=profile.request_profile_hash,
        upstream_bundle_hash=profile.upstream_bundle_hash,
        scorer_source_hash=profile.scorer_source_hash,
        canonical_profile_hash=profile.canonical_profile_hash,
        serialization_version=profile.serialization_version,
        request_hash=plan.request_hash,
        request_manifest=request_manifest,
        response_manifest=response_manifest,
        evidence_hash=evidence_hash,
        collected_at=collected_at,
        radiant_team_score=result.radiant_team_score,
        dire_team_score=result.dire_team_score,
        relative_advantage=result.relative_advantage,
    )
    heroes = tuple(
        RoshHeroScoreRecord(
            team_side=row.team_side,
            position_id=row.position_id,
            hero_id=row.hero_id,
            raw_score=row.raw_score,
            display_score=row.display_score,
            components={
                "position_base_diff": row.position_base_diff,
                "same_team_synergy": row.same_team_synergy,
                "opponent_matchup_synergy": row.opponent_matchup_synergy,
            },
        )
        for row in result.hero_scores
    )
    minutes = tuple(
        RoshMinutePointRecord(
            minute=row.minute,
            raw_score=row.raw_score,
            display_score=row.display_score,
            radiant_time_delta=row.radiant_time_delta,
            dire_time_delta=row.dire_time_delta,
            synergy_delta=row.synergy_delta,
            source_audit={
                "rank_source_counts": dict(row.rank_source_counts),
                "slots": [slot.projection() for slot in row.slots],
            },
        )
        for row in result.minute_points
    )
    hero_projection = [row.projection() for row in result.hero_scores]
    minute_projection = [row.projection() for row in result.minute_points]
    stored_result = {
        "radiant_team_score": result.radiant_team_score,
        "dire_team_score": result.dire_team_score,
        "relative_advantage": result.relative_advantage,
        "hero_scores": hero_projection,
        "minute_points": minute_projection,
    }
    return _RunBundle(StoredRoshRun(run, heroes, minutes, stored_result), artifact_root)


@pytest.fixture
def historical_run(tmp_path: Path) -> Any:
    return _run(tmp_path)


def _historical_target() -> RoshFeatureTarget:
    return RoshFeatureTarget(
        match_id=MATCH_ID,
        date_time=DATE_TIME,
        prediction_cutoff=datetime.fromtimestamp(DATE_TIME, UTC) - timedelta(hours=2),
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        radiant_hero_ids=RADIANT,
        dire_hero_ids=DIRE,
    )


def _witness(stored: StoredRoshRun) -> RoshRequestPlanWitness:
    return RoshRequestPlanWitness.from_run(
        stored,
        request_started_at=STARTED_AT,
    )


def _prospective_target() -> RoshFeatureTarget:
    return RoshFeatureTarget(
        match_id=999_001,
        date_time=DATE_TIME,
        prediction_cutoff=COLLECTED_AT + timedelta(seconds=1),
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
        radiant_hero_ids=RADIANT,
        dire_hero_ids=DIRE,
        match_source="stratz",
        source_match_id="999001",
    )


def test_expected_position_unavailable_authority_is_canonical_and_replayable(
    tmp_path: Path,
) -> None:
    local_cutoff = datetime(2026, 7, 28, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    snapshot, authority = build_unavailable_rosh_feature_snapshot_with_authority(
        match_id=MATCH_ID,
        prediction_cutoff=local_cutoff,
        availability_mode=AvailabilityMode.RECONSTRUCTED,
        radiant_hero_ids=reversed(RADIANT),
        dire_hero_ids={*DIRE},
    )
    repeated, repeated_authority = (
        build_unavailable_rosh_feature_snapshot_with_authority(
            match_id=MATCH_ID,
            prediction_cutoff=local_cutoff.astimezone(UTC),
            availability_mode=AvailabilityMode.RECONSTRUCTED.value,
            radiant_hero_ids=sorted(RADIANT),
            dire_hero_ids=reversed(DIRE),
        )
    )

    assert authority["schema"] == ROSH_UNAVAILABLE_AUTHORITY_SCHEMA
    assert authority["prediction_cutoff"] == local_cutoff.astimezone(UTC).isoformat()
    assert authority["radiant_hero_ids"] == sorted(RADIANT)
    assert authority["dire_hero_ids"] == sorted(DIRE)
    assert len(str(authority["authority_hash"])) == 64
    assert repeated_authority == authority
    assert repeated == snapshot
    assert snapshot.status == "unavailable"
    assert snapshot.missing_reason == "expected_positions_incomplete"
    assert snapshot.coverage == 0.0
    assert snapshot.run_id is None
    assert snapshot.profile_hash is None
    assert snapshot.result_hash is None
    projection = project_rosh_features(snapshot)
    for name in ROSH_FEATURE_SCHEMA:
        if name == "coverage":
            assert projection[name] == 0.0
            assert projection[f"{name}__missing"] == 0.0
        else:
            assert projection[name] is None
            assert projection[f"{name}__missing"] == 1.0
    assert (
        replay_rosh_feature_snapshot(
            authority,
            runs=(),
            artifact_root=tmp_path,
        )
        == snapshot
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update(match_id=MATCH_ID + 1),
        lambda value: value.update(authority_hash="0" * 64),
        lambda value: value.update(missing_reason="run_unavailable"),
        lambda value: value["radiant_hero_ids"].reverse(),
    ),
)
def test_expected_position_unavailable_authority_rejects_tampering(
    tmp_path: Path,
    mutate,
) -> None:
    _snapshot, authority = build_unavailable_rosh_feature_snapshot_with_authority(
        match_id=MATCH_ID,
        prediction_cutoff=STARTED_AT,
        availability_mode=AvailabilityMode.RECONSTRUCTED,
        radiant_hero_ids=RADIANT,
        dire_hero_ids=DIRE,
    )
    changed = copy.deepcopy(authority)
    mutate(changed)

    with pytest.raises(ValueError):
        replay_rosh_feature_snapshot(
            changed,
            runs=(),
            artifact_root=tmp_path,
        )


def test_expected_position_unavailable_authority_rejects_other_reasons() -> None:
    with pytest.raises(ValueError, match="unsupported.*reason"):
        build_unavailable_rosh_feature_snapshot_with_authority(
            match_id=MATCH_ID,
            prediction_cutoff=STARTED_AT,
            availability_mode=AvailabilityMode.RECONSTRUCTED,
            radiant_hero_ids=RADIANT,
            dire_hero_ids=DIRE,
            reason="run_unavailable",
        )


def _prospective_link(
    stored: StoredRoshRun,
    *,
    linked_at: datetime = COLLECTED_AT,
) -> RoshRunMatchLink:
    return RoshRunMatchLink(
        source="stratz",
        source_match_id="999001",
        run_id=stored.run.run_id,
        map_number=None,
        linked_at=linked_at.isoformat(),
    )


def _prospective_timing(
    stored: StoredRoshRun,
    *,
    response_first_usable_at: datetime = COLLECTED_AT,
) -> RoshProspectiveTimingAuthority:
    request = stored.run.request_manifest["request_artifact"]
    return RoshProspectiveTimingAuthority(
        run_id=stored.run.run_id,
        request_started_at=STARTED_AT,
        request_first_usable_at=STARTED_AT,
        request_hash=stored.run.request_hash,
        request_artifact_hash=request["content_sha256"],
        responses=tuple(
            RoshResponseTiming(
                operation_index=row["operation_index"],
                operation_name=row["operation_name"],
                response_artifact_hash=row["response_artifact_hash"],
                first_usable_at=response_first_usable_at,
            )
            for row in stored.run.response_manifest
        ),
    )


def _clone_run(
    stored: StoredRoshRun,
    *,
    digit: str,
    date_time: int | None = None,
    collected_at: datetime | None = None,
) -> StoredRoshRun:
    return replace(
        stored,
        run=replace(
            stored.run,
            run_id=digit * 64,
            evidence_hash=hex((int(digit, 16) + 1) % 16)[2:] * 64,
            date_time=stored.run.date_time if date_time is None else date_time,
            collected_at=(
                stored.run.collected_at
                if collected_at is None
                else collected_at.isoformat()
            ),
        ),
    )


def test_golden_reconstructed_replays_archives_and_projects_fixed_schema(
    historical_run: _RunBundle,
) -> None:
    assert set(historical_run.stored.result or {}) == {
        "radiant_team_score",
        "dire_team_score",
        "relative_advantage",
        "hero_scores",
        "minute_points",
    }
    snapshot, authority = build_rosh_feature_snapshot_with_authority(
        _historical_target(),
        [historical_run.stored],
        artifact_root=historical_run.artifact_root,
        request_plan_witness=_witness(historical_run.stored),
    )

    assert snapshot.status == "available"
    assert snapshot.feature_version == ROSH_FEATURE_VERSION
    assert snapshot.formula_version == historical_run.stored.run.formula_version
    assert snapshot.relative_advantage == 5.8
    assert (snapshot.score_20, snapshot.score_30) == (-7.0, -5.7)
    assert (snapshot.score_40, snapshot.score_50) == (-5.6, -5.8)
    assert snapshot.slope_20_40 == pytest.approx(0.07)
    assert snapshot.slope_30_50 == pytest.approx(-0.005)
    curve = tuple(row.display_score for row in historical_run.stored.minute_points)
    assert snapshot.curve_min == min(curve)
    assert snapshot.curve_max == max(curve)
    assert snapshot.curve_range == max(curve) - min(curve)
    assert snapshot.direction_flip_count == 0
    assert snapshot.position_min_support is not None
    assert snapshot.synergy_min_support == 499
    assert snapshot.coverage == 1.0
    expected_fallbacks = sum(
        slot["source"] == ALL_RANK_FALLBACK
        for point in historical_run.stored.minute_points
        for slot in point.source_audit["slots"]
    )
    expected_slots = sum(
        len(point.source_audit["slots"])
        for point in historical_run.stored.minute_points
    )
    assert snapshot.rank_fallback_ratio == expected_fallbacks / expected_slots

    projection = project_rosh_features(snapshot)
    assert tuple(projection) == ROSH_MODEL_SCHEMA
    assert projection["relative_advantage"] == 5.8
    assert projection["relative_advantage__missing"] == 0.0
    assert (
        replay_rosh_feature_snapshot(
            authority,
            runs=[historical_run.stored],
            artifact_root=historical_run.artifact_root,
        )
        == snapshot
    )


def test_exact_minute_slopes_flips_fallback_and_coverage_are_frozen() -> None:
    slot_rank = MinuteSlot("RADIANT", 1, 1, DIVINE_IMMORTAL, 10, 0.0)
    slot_fallback = MinuteSlot(
        "RADIANT",
        1,
        1,
        ALL_RANK_FALLBACK,
        10,
        0.0,
    )
    minutes = (
        MinutePoint(20, 0.0, 0.0, 0.0, -1.0, -1.0, {}, (slot_rank,)),
        MinutePoint(30, 0.0, 0.0, 0.0, 0.0, 0.0, {}, (slot_fallback,)),
        MinutePoint(40, 0.0, 0.0, 0.0, 2.0, 2.0, {}, (slot_rank,)),
    )
    normalized = NormalizedRoshInputs(
        draft=(
            DraftSlot("RADIANT", 1, 1),
            DraftSlot("DIRE", 2, 2),
        ),
        position_stats=(PositionAggregate(1, 1, 1, 2),),
        synergy_samples=(),
        all_rank_time_stats=(),
        rank_time_stats=(),
    )
    result = OfficialRoshResult(
        "formula",
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        (),
        minutes,
        "0" * 64,
    )

    signals = rosh_features._feature_signals(normalized, result)

    assert signals["score_50"] is None
    assert signals["slope_20_40"] == pytest.approx(0.15)
    assert signals["slope_30_50"] is None
    assert signals["direction_flip_count"] == 1
    assert signals["position_min_support"] == 0
    assert signals["rank_fallback_ratio"] == pytest.approx(1 / 3)
    assert signals["coverage"] == 0.75


def test_old_run_without_request_plan_witness_is_unavailable(
    historical_run: _RunBundle,
) -> None:
    snapshot = build_rosh_feature_snapshot(
        _historical_target(),
        [historical_run.stored],
        artifact_root=historical_run.artifact_root,
    )

    assert snapshot.status == "unavailable"
    assert snapshot.missing_reason == "request_plan_witness_unavailable"
    assert snapshot.coverage == 0.0
    assert all(
        getattr(snapshot, name) is None
        for name in ROSH_FEATURE_SCHEMA
        if name != "coverage"
    )
    projection = project_rosh_features(snapshot)
    assert projection["relative_advantage"] is None
    assert projection["relative_advantage__missing"] == 1.0
    assert projection["coverage"] == 0.0


def test_request_plan_witness_must_reproduce_archived_semantics(
    historical_run: _RunBundle,
) -> None:
    valid = _witness(historical_run.stored)
    drifted = replace(
        valid,
        request_started_at=valid.request_started_at + timedelta(days=1),
    )

    snapshot = build_rosh_feature_snapshot(
        _historical_target(),
        [historical_run.stored],
        artifact_root=historical_run.artifact_root,
        request_plan_witness=drifted,
    )

    assert snapshot.status == "unavailable"
    assert snapshot.missing_reason in {
        "request_hash_mismatch",
        "request_semantics_mismatch",
    }


def test_response_gzip_and_content_tampering_fail_closed(
    historical_run: _RunBundle,
) -> None:
    response = historical_run.stored.run.response_manifest[0]
    path = historical_run.artifact_root.joinpath(*Path(response["relative_path"]).parts)
    original = path.read_bytes()
    path.write_bytes(original + b"tamper")

    gzip_snapshot = build_rosh_feature_snapshot(
        _historical_target(),
        [historical_run.stored],
        artifact_root=historical_run.artifact_root,
        request_plan_witness=_witness(historical_run.stored),
    )
    assert gzip_snapshot.missing_reason == "artifact_gzip_hash_mismatch"

    changed_body = gzip.decompress(original).replace(b'"data"', b'"dAta"', 1)
    changed_gzip = gzip.compress(changed_body, compresslevel=9, mtime=0)
    path.write_bytes(changed_gzip)
    manifest = tuple(
        {**row, "response_gzip_sha256": hashlib.sha256(changed_gzip).hexdigest()}
        for row in historical_run.stored.run.response_manifest
    )
    changed = replace(
        historical_run.stored,
        run=replace(historical_run.stored.run, response_manifest=manifest),
    )
    content_snapshot = build_rosh_feature_snapshot(
        _historical_target(),
        [changed],
        artifact_root=historical_run.artifact_root,
        request_plan_witness=_witness(changed),
    )
    assert content_snapshot.missing_reason == "artifact_content_hash_mismatch"


def test_archive_path_traversal_fails_closed(historical_run: _RunBundle) -> None:
    manifest = tuple(
        {**row, "relative_path": "../outside.json.gz"}
        for row in historical_run.stored.run.response_manifest
    )
    changed = replace(
        historical_run.stored,
        run=replace(historical_run.stored.run, response_manifest=manifest),
    )

    snapshot = build_rosh_feature_snapshot(
        _historical_target(),
        [changed],
        artifact_root=historical_run.artifact_root,
        request_plan_witness=_witness(changed),
    )

    assert snapshot.status == "unavailable"
    assert snapshot.missing_reason == "artifact_path_invalid"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda stored: replace(
                stored,
                run=replace(stored.run, canonical_profile_hash="0" * 64),
            ),
            "profile_mismatch",
        ),
        (
            lambda stored: replace(
                stored,
                result={**stored.result, "schema": "tampered"},
            ),
            "stored_result_mismatch",
        ),
        (
            lambda stored: replace(
                stored,
                minute_points=(
                    replace(
                        stored.minute_points[0],
                        display_score=stored.minute_points[0].display_score + 1.0,
                    ),
                    *stored.minute_points[1:],
                ),
            ),
            "stored_minute_rows_mismatch",
        ),
        (
            lambda stored: replace(
                stored,
                hero_scores=(
                    replace(
                        stored.hero_scores[0],
                        display_score=stored.hero_scores[0].display_score + 1.0,
                    ),
                    *stored.hero_scores[1:],
                ),
            ),
            "stored_hero_rows_mismatch",
        ),
        (
            lambda stored: replace(
                stored,
                run=replace(
                    stored.run,
                    draft={
                        **stored.run.draft,
                        "radiant": [
                            {**stored.run.draft["radiant"][0], "hero_id": 999},
                            *stored.run.draft["radiant"][1:],
                        ],
                    },
                ),
            ),
            "draft_hash_mismatch",
        ),
        (
            lambda stored: replace(
                stored,
                run=replace(stored.run, evidence_hash="0" * 64),
            ),
            "evidence_hash_mismatch",
        ),
        (
            lambda stored: replace(
                stored,
                run=replace(stored.run, run_id="0" * 64),
            ),
            "run_id_mismatch",
        ),
    ],
)
def test_profile_result_rows_and_evidence_are_recomputed(
    historical_run: _RunBundle,
    mutate: Any,
    reason: str,
) -> None:
    changed = mutate(historical_run.stored)
    snapshot = build_rosh_feature_snapshot(
        _historical_target(),
        [changed],
        artifact_root=historical_run.artifact_root,
        request_plan_witness=_witness(changed),
    )

    assert snapshot.status == "unavailable"
    assert snapshot.missing_reason == reason


def test_multiple_runs_require_explicit_run_id(historical_run: _RunBundle) -> None:
    duplicate = replace(
        historical_run.stored,
        run=replace(
            historical_run.stored.run,
            run_id="1" * 64,
            evidence_hash="2" * 64,
        ),
    )
    runs = [historical_run.stored, duplicate]

    ambiguous = build_rosh_feature_snapshot(
        _historical_target(),
        runs,
        artifact_root=historical_run.artifact_root,
        request_plan_witness=_witness(historical_run.stored),
    )
    selected = build_rosh_feature_snapshot(
        _historical_target(),
        runs,
        artifact_root=historical_run.artifact_root,
        run_id=historical_run.stored.run.run_id,
        request_plan_witness=_witness(historical_run.stored),
    )

    assert ambiguous.missing_reason == "ambiguous_runs"
    assert selected.status == "available"
    assert selected.run_id == historical_run.stored.run.run_id


def test_prospective_requires_linked_cutoff_legal_timing(tmp_path: Path) -> None:
    bundle = _run(tmp_path, prospective=True)
    target = _prospective_target()
    link = _prospective_link(bundle.stored)
    missing = build_rosh_feature_snapshot(
        target,
        [bundle.stored],
        artifact_root=bundle.artifact_root,
        match_links=[link],
    )
    available = build_rosh_feature_snapshot(
        target,
        [bundle.stored],
        artifact_root=bundle.artifact_root,
        match_links=[link],
        prospective_timing=_prospective_timing(bundle.stored),
    )
    late = build_rosh_feature_snapshot(
        target,
        [bundle.stored],
        artifact_root=bundle.artifact_root,
        match_links=[link],
        prospective_timing=_prospective_timing(
            bundle.stored,
            response_first_usable_at=target.prediction_cutoff + timedelta(seconds=1),
        ),
    )
    late_link = build_rosh_feature_snapshot(
        target,
        [bundle.stored],
        artifact_root=bundle.artifact_root,
        match_links=[
            _prospective_link(
                bundle.stored,
                linked_at=target.prediction_cutoff + timedelta(seconds=1),
            )
        ],
        prospective_timing=_prospective_timing(bundle.stored),
    )

    assert missing.missing_reason == "prospective_timing_unavailable"
    assert available.status == "available"
    assert available.availability_mode == "prospective"
    assert late.missing_reason == "prospective_cutoff_violation"
    assert late_link.missing_reason == "requested_run_not_found"


def test_bounded_prospective_authority_ignores_external_future_and_unrelated_rows(
    tmp_path: Path,
) -> None:
    bundle = _run(tmp_path, prospective=True)
    target = _prospective_target()
    link = _prospective_link(bundle.stored)
    timing = _prospective_timing(bundle.stored)
    baseline, authority = build_rosh_feature_snapshot_with_authority(
        target,
        [bundle.stored],
        artifact_root=bundle.artifact_root,
        match_links=[link],
        prospective_timing=timing,
    )
    unrelated = _clone_run(
        bundle.stored,
        digit="3",
        date_time=DATE_TIME + 1,
    )
    future_at = target.prediction_cutoff + timedelta(minutes=1)
    future = _clone_run(
        bundle.stored,
        digit="5",
        collected_at=future_at,
    )
    extra_links = [
        link,
        RoshRunMatchLink(
            source="stratz",
            source_match_id="unrelated",
            run_id=unrelated.run.run_id,
            map_number=None,
            linked_at=COLLECTED_AT.isoformat(),
        ),
        RoshRunMatchLink(
            source="stratz",
            source_match_id="999001",
            run_id=future.run.run_id,
            map_number=None,
            linked_at=future_at.isoformat(),
        ),
        _prospective_link(bundle.stored, linked_at=future_at),
    ]

    expanded, expanded_authority = build_rosh_feature_snapshot_with_authority(
        target,
        [bundle.stored, unrelated, future],
        artifact_root=bundle.artifact_root,
        match_links=extra_links,
        prospective_timing=timing,
    )
    replayed = replay_rosh_feature_snapshot(
        authority,
        runs=[bundle.stored, unrelated, future],
        artifact_root=bundle.artifact_root,
        match_links=extra_links,
    )

    assert baseline.status == "available"
    assert expanded == baseline
    assert expanded.input_hash == baseline.input_hash
    assert expanded_authority == authority
    assert replayed == baseline


def test_explicit_reconstructed_run_id_bounds_authority_and_external_replay(
    historical_run: _RunBundle,
) -> None:
    target = _historical_target()
    selected_id = historical_run.stored.run.run_id
    baseline, authority = build_rosh_feature_snapshot_with_authority(
        target,
        [historical_run.stored],
        artifact_root=historical_run.artifact_root,
        run_id=selected_id,
        request_plan_witness=_witness(historical_run.stored),
    )
    other = _clone_run(historical_run.stored, digit="7")

    expanded, expanded_authority = build_rosh_feature_snapshot_with_authority(
        target,
        [historical_run.stored, other],
        artifact_root=historical_run.artifact_root,
        run_id=selected_id,
        request_plan_witness=_witness(historical_run.stored),
    )
    replayed = replay_rosh_feature_snapshot(
        authority,
        runs=[historical_run.stored, other],
        artifact_root=historical_run.artifact_root,
    )

    assert baseline.status == "available"
    assert expanded == baseline
    assert expanded_authority == authority
    assert replayed == baseline


def test_reconstructed_and_prospective_runs_never_cross_modes(
    historical_run: _RunBundle,
) -> None:
    target = _prospective_target()
    link = _prospective_link(historical_run.stored)

    snapshot = build_rosh_feature_snapshot(
        target,
        [historical_run.stored],
        artifact_root=historical_run.artifact_root,
        match_links=[link],
    )

    assert snapshot.status == "unavailable"
    assert snapshot.missing_reason == "run_unavailable"


def test_external_replay_rejects_authority_claim_drift(
    historical_run: _RunBundle,
) -> None:
    snapshot, authority = build_rosh_feature_snapshot_with_authority(
        _historical_target(),
        [historical_run.stored],
        artifact_root=historical_run.artifact_root,
        request_plan_witness=_witness(historical_run.stored),
    )
    assert snapshot.status == "available"
    changed = copy.deepcopy(authority)
    changed["candidate_run_ids"] = []

    with pytest.raises(ValueError, match="candidate authority"):
        replay_rosh_feature_snapshot(
            changed,
            runs=[historical_run.stored],
            artifact_root=historical_run.artifact_root,
        )


def test_rosh_model_schema_is_stable_and_legacy_scores_are_not_an_input() -> None:
    assert ROSH_FEATURE_SCHEMA == (
        "relative_advantage",
        "score_20",
        "score_30",
        "score_40",
        "score_50",
        "slope_20_40",
        "slope_30_50",
        "curve_min",
        "curve_max",
        "curve_range",
        "direction_flip_count",
        "position_min_support",
        "synergy_min_support",
        "rank_fallback_ratio",
        "coverage",
    )
    assert len(ROSH_MODEL_SCHEMA) == 30
    assert len(ROSH_MODEL_SCHEMA_HASH) == 64
    source = Path(rosh_features.__file__).read_text(encoding="utf-8")
    assert "historical_rosh_lineup_scores" not in source
