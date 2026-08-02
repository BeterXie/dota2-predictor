from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from event_intelligence.storage import IntelligenceStorage

from live_betting.comeback import no_signal_decision, score_comeback
from live_betting.m1_verifier import verify_m1_qualifying_rejection
from live_betting.markets import normalized_state_hash
from live_betting.profiles.draft_curve import DraftCurve, DraftPoint
from live_betting.profiles.player_form import PlayerForm
from live_betting.shadow_strategy import ComebackShadowStrategy
from live_betting.storage import LiveBettingStore
from live_betting.stratz_rosh_client import (
    ROSH_FORMULA_VERSION,
    canonical_evidence_hash,
    rosh_cache_week_start,
)
from live_betting.strict_eligibility import accept_strict_live_map_mapping
from live_betting.strategy_contract import (
    PROPOSED_STRATEGY_VERSION,
    parse_decision_payload,
    replay_persisted_decision,
    serialize_decision_payload,
    validate_strategy_contract,
)
from tests.test_rosh_strategy import (
    NOW,
    _draft_curve,
    _form,
    _observation,
    _persisted_row,
    _score,
    _snapshots,
    _style,
)
from live_betting.market_state import build_market_surface
from live_betting.report import build_report
from web.monitoring import monitor_match_detail
from live_betting.vision import VisionComebackState
from tests.draft_authority_fixture import (
    make_test_vision_observation,
    seed_test_draft_authority,
)
from tests.test_shadow_monitor_safety import (
    EVENT_ID,
    complete_snapshots,
    mapping_evidence,
    raw_odds_payload,
    raybet_metadata,
)


def _profile_ref(store: LiveBettingStore, team_id: int) -> dict[str, object]:
    cutoff = (NOW - timedelta(minutes=1)).isoformat()
    profile_version = "team-style-v2"
    profile_hash = hashlib.sha256(f"profile:{team_id}".encode()).hexdigest()
    store.connection.execute(
        """INSERT INTO team_style_profiles
           (team_id, profile_cutoff, profile_version, opportunity_counts_json,
            posterior_rates_json, duration_quantiles_json, weighting_json,
            effective_sample_size, input_hash, created_at)
           VALUES (?, ?, ?, '{}', '[]', '[]', '{}', 10, ?, ?)""",
        (team_id, cutoff, profile_version, profile_hash, cutoff),
    )
    account_ids = [team_id * 10 + value for value in range(1, 6)]
    score_refs = []
    for player_slot, account_id in enumerate(account_ids):
        match_id = team_id * 100 + player_slot
        score_hash = hashlib.sha256(
            f"score:{team_id}:{player_slot}".encode()
        ).hexdigest()
        store.connection.execute(
            """INSERT INTO match_ingest_status
               (match_id, event_id, discovered_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (match_id, EVENT_ID, cutoff, cutoff),
        )
        store.connection.execute(
            """INSERT INTO player_map_scores
               (match_id, player_slot, account_id, position, execution_score,
                result_adjusted_score, component_facts_json,
                component_scores_json, weights_json, coverage, role_confidence,
                benchmark_cutoff, benchmark_hash, input_hash, score_version,
                explanation_json, created_at)
               VALUES (?, ?, ?, ?, 50, 50, '{}', '{}', '{}', 1, 1, ?, ?, ?,
                       'player-score-v3', '{}', ?)""",
            (
                match_id, player_slot, account_id, player_slot + 1, cutoff,
                hashlib.sha256(f"benchmark:{team_id}".encode()).hexdigest(),
                score_hash, cutoff,
            ),
        )
        score_refs.append({
            "match_id": match_id,
            "player_slot": player_slot,
            "input_hash": score_hash,
            "score_version": "player-score-v3",
            "created_at": cutoff,
        })
    return {
        "team_style": {
            "team_id": team_id,
            "profile_cutoff": cutoff,
            "profile_version": profile_version,
            "input_hash": profile_hash,
            "effective_sample_size": 10,
        },
        "player_form": {
            "account_ids": account_ids,
            "cutoff": NOW.isoformat(),
            "score_refs": score_refs,
        },
    }


def _persisted_rosh_score(
    store: LiveBettingStore, mapping_id: int,
):
    started = NOW - timedelta(seconds=10)
    source_as_of = started + timedelta(seconds=2)
    bucket = {
        "minute": 20, "time_start": 20, "time_end": 20,
        "advantage_side": "dire", "advantage_percent": 5.0,
        "radiant_advantage": 0.0, "dire_advantage": 5.0,
        "match_percentage": 100.0, "win_rate_graph": -5.0,
        "hero_adjustment": -5.0, "hero_base_adjustment": -5.0,
        "hero_tempo_adjustment": 0.0, "synergy_adjustment": 0.0,
        "player_adjustment": 0.0,
    }
    evidence = {
        "source": "stratz", "formula_version": ROSH_FORMULA_VERSION,
        "source_week": int(started.timestamp()),
        "source_as_of": source_as_of.isoformat(),
        "cache_week_start": rosh_cache_week_start(started),
        "pure_minute_table": [bucket],
        "score": {
            "pure_lineup_score": -5.0,
            "player_adjusted_lineup_score": None,
            "effective_lineup_score": -5.0,
            "scoring_mode": "pure", "player_coverage_count": 0,
        },
    }
    fetched = SimpleNamespace(
        pure_lineup_score=-5.0, player_adjusted_lineup_score=None,
        effective_lineup_score=-5.0, scoring_mode="pure",
        player_coverage_count=0, stake_multiplier=0.5, stake_cap=0.5,
        formula_version=ROSH_FORMULA_VERSION, source_name="stratz",
        source_week=int(started.timestamp()),
        cache_week_start=rosh_cache_week_start(started), source_as_of=source_as_of,
        evidence=evidence, evidence_hash=canonical_evidence_hash(evidence),
    )
    draft_hash = store.rosh_draft_hash(
        (1, 2, 3, 4, 5), (6, 7, 8, 9, 10)
    )
    assert store._trusted_rosh_draft(
        raybet_match_id="match-1",
        map_number=1,
        strict_mapping_id=mapping_id,
        draft_hash=draft_hash,
        radiant_hero_ids=(1, 2, 3, 4, 5),
        dire_hero_ids=(6, 7, 8, 9, 10),
        as_of=NOW,
    )
    score = store.insert_rosh_lineup_score(
        fetched, raybet_match_id="match-1", map_number=1,
        strict_mapping_id=mapping_id,
        draft_hash=draft_hash,
        radiant_hero_ids=(1, 2, 3, 4, 5),
        dire_hero_ids=(6, 7, 8, 9, 10), created_at=NOW,
    )
    assert score is not None
    return score


def _full_authority_fixture(
    path: Path,
    *,
    rosh_case: str = "valid",
) -> tuple[LiveBettingStore, object]:
    current_at = NOW
    previous_at = NOW - timedelta(seconds=3)
    with IntelligenceStorage(path) as intelligence:
        intelligence.init_schema()
        intelligence.connection.execute(
            "UPDATE event_registry SET approved_at=? WHERE event_id=?",
            ((current_at - timedelta(days=30)).isoformat(), EVENT_ID),
        )
        intelligence.connection.commit()
    store = LiveBettingStore(path)
    store.init_schema()
    store.connection.execute(
        "CREATE TABLE IF NOT EXISTS teams (team_id INTEGER PRIMARY KEY, name TEXT)"
    )
    store.connection.executemany(
        "INSERT OR IGNORE INTO teams VALUES (?, ?)",
        ((10, "Canonical One"), (20, "Canonical Two")),
    )
    store.upsert_raybet_match(raybet_metadata(), current_at - timedelta(minutes=2))
    with patch(
        "live_betting.strict_eligibility._utc_now",
        return_value=current_at - timedelta(seconds=30),
    ):
        mapping = accept_strict_live_map_mapping(
            store.connection,
            raybet_match_id="match-1", map_number=1, event_id=EVENT_ID,
            team_one_id=1, team_two_id=2,
            canonical_team_one_id=10, canonical_team_two_id=20,
            source="test_fixture", evidence=mapping_evidence(),
            accepted_by="tester",
            accepted_at=current_at - timedelta(minutes=1),
        )
    state = VisionComebackState(
        "available", "vision_hud", 0.95, 14, 18, None, None, None,
        net_worth_advantage_side="dire",
        net_worth_advantage_min=5_000,
        net_worth_advantage_max=5_999,
    )
    previous_vision = replace(
        make_test_vision_observation(
            raybet_match_id="match-1", map_number=1,
            captured_at=previous_at, game_clock_seconds=20 * 60,
            radiant_team_side="team_one", label="m1-previous",
        ),
        comeback_state=state,
    )
    current_vision = replace(
        make_test_vision_observation(
            raybet_match_id="match-1", map_number=1,
            captured_at=current_at, game_clock_seconds=20 * 60 + 3,
            radiant_team_side="team_one", label="m1-current",
        ),
        comeback_state=state,
    )
    assert store.insert_vision_observation(previous_vision)
    assert store.insert_vision_observation(current_vision)
    authority = seed_test_draft_authority(
        store.connection, raybet_match_id="match-1", map_number=1,
        strict_mapping_id=mapping.mapping_id, observed_at=current_at,
        horizon_minutes=20, label="m1-full-authority",
    )
    point = DraftPoint(
        authority.horizon_minutes, authority.radiant_probability, 0.0, 0.0,
        authority.quality, validated=True, support=authority.support,
        calibration_ref=f"draft-calibration:{authority.calibration_hash}",
        input_refs=(authority.source_ref,), uncertainty=authority.uncertainty,
        feature_hash=authority.feature_hash, model_hash=authority.model_hash,
        calibration_hash=authority.calibration_hash,
        global_calibration_passed=True,
        global_gate_ref=authority.global_gate_ref,
        model_version=authority.model_version, model_kind="pure_draft",
        availability_mode="prospective",
        input_snapshot_hash=authority.input_snapshot_hash,
        landmark_key=authority.landmark_key, curve_key=authority.curve_key,
        deployment_key=authority.deployment_key,
        target_snapshot_hash=authority.target_snapshot_hash,
    )
    curve = DraftCurve(
        (point,), source_ref=authority.source_ref,
        authority_revision=authority.authority_revision,
        dependency_revision=authority.dependency_revision,
        curve_key=authority.curve_key, deployment_key=authority.deployment_key,
        target_snapshot_hash=authority.target_snapshot_hash,
        strict_mapping_id=authority.strict_mapping_id,
    )
    previous_rows = complete_snapshots(previous_at)
    current_rows = [
        replace(row, price=2.82)
        if row.odds_id == "winner-one"
        else row
        for row in complete_snapshots(current_at)
    ]
    for key, at, rows in (
        ("m1-previous-transport", previous_at, previous_rows),
        ("m1-current-transport", current_at, current_rows),
    ):
        store.store_odds_observation(
            source="direct", observation_key=key, source_event_id=None,
            raybet_match_id="match-1", observed_at=at,
            normalized_state_hash=normalized_state_hash(rows),
            snapshots=rows, raw_payload=raw_odds_payload(rows),
        )
    team_one_refs = _profile_ref(store, 10)
    team_two_refs = _profile_ref(store, 20)
    rosh_score = _persisted_rosh_score(store, mapping.mapping_id)
    if rosh_case == "missing":
        rosh_score = replace(rosh_score, score_key="d" * 64)
    elif rosh_case == "wrong_hash":
        rosh_score = replace(rosh_score, evidence_hash="d" * 64)
    elif rosh_case == "stale":
        rosh_score = replace(
            rosh_score,
            source_as_of=NOW - timedelta(minutes=16),
        )
    elif rosh_case == "wrong_event":
        wrong_key = "e" * 64
        store.connection.execute(
            """INSERT INTO rosh_lineup_scores
               SELECT ?, draft_hash, player_identity_hash, 'other-match',
                      map_number, strict_mapping_id, radiant_hero_ids_json,
                      dire_hero_ids_json, pure_lineup_score,
                      player_adjusted_lineup_score, effective_lineup_score,
                      scoring_mode, player_coverage_count, stake_multiplier,
                      formula_version, source_name, source_week,
                      cache_week_start, source_as_of, evidence_json,
                      evidence_hash, created_at
                 FROM rosh_lineup_scores WHERE score_key=?""",
            (wrong_key, rosh_score.score_key),
        )
        store.connection.commit()
        rosh_score = replace(rosh_score, score_key=wrong_key)
    elif rosh_case != "valid":
        raise ValueError(f"unknown Rosh fixture case: {rosh_case}")
    result = ComebackShadowStrategy(
        strategy_version=PROPOSED_STRATEGY_VERSION
    ).evaluate(
        snapshots=current_rows, previous_snapshots=previous_rows,
        observation=current_vision, previous_observation=previous_vision,
        underdog_style=_style(10), favorite_style=_style(20),
        underdog_form=PlayerForm(tuple(team_one_refs["player_form"]["account_ids"]), 0.1, {}, 5, 1.0),
        favorite_form=PlayerForm(tuple(team_two_refs["player_form"]["account_ids"]), 0.0, {}, 5, 1.0),
        draft_curve=curve, rosh_lineup_score=rosh_score,
        decided_at=current_at, map_already_attempted=False,
        signal_transport_key="m1-current-transport",
        previous_transport_key="m1-previous-transport",
        input_refs={
            "strict_live_eligibility": {
                "mapping_refs": {"strict_mapping_id": mapping.mapping_id}
            },
            "team_one_intelligence": team_one_refs,
            "team_two_intelligence": team_two_refs,
        },
    )
    assert result.decision.reason == "rosh_direction_opposes_underdog"
    assert store.insert_decision(
        result.decision, draft_authority=authority,
        vision_observation=current_vision,
        vision_transport_key="m1-current-transport",
    )
    return store, result.decision


def test_contract_survives_existing_storage_shape_without_migration() -> None:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    observation = _observation()
    surface = build_market_surface(_snapshots(NOW))
    decision = no_signal_decision(
        observation=observation,
        surface=surface,
        decided_at=NOW,
        reason="strict_live_ineligible:mapping_missing",
        strategy_version=PROPOSED_STRATEGY_VERSION,
    )
    assert store.insert_decision(decision)
    row = store.connection.execute(
        "SELECT * FROM strategy_decisions WHERE decision_key=?",
        (decision.decision_key,),
    ).fetchone()
    assert row is not None
    inputs = json.loads(row["contributions_json"])["__inputs__"]
    assert validate_strategy_contract(
        row["strategy_version"], inputs["strategy_contract"]
    )
    projection = build_report(store.connection)[
        "m1_strategy_contract_verifications"
    ]
    assert projection == [
        {
            "decision_key": decision.decision_key,
            "strategy_version": PROPOSED_STRATEGY_VERSION,
            "evaluator_hash": None,
            "policy_hash": None,
            "serialization_version": None,
            "m1_qualifying_rejection": False,
            "verifier_reason": "reason_not_allowlisted",
            "replay_reason": None,
        }
    ]

    missing_contract = SimpleNamespace(
        **{**decision.__dict__, "inputs": {}, "decision_key": "f" * 32}
    )
    assert not store.insert_decision(missing_contract)
    store.close()


def test_m1_allowlisted_rejection_still_requires_full_authority() -> None:
    decision = score_comeback(
        observation=_observation(),
        surface=build_market_surface(_snapshots(NOW)),
        underdog_style=_style(2),
        favorite_style=_style(1),
        underdog_form=_form(0.1),
        favorite_form=_form(0.0),
        draft_curve=_draft_curve(),
        decided_at=NOW,
        stable=True,
        strategy_version=PROPOSED_STRATEGY_VERSION,
        rosh_lineup_score=_score(pure=-5.0),
    )
    assert decision.reason == "edge_below_threshold"
    row = _persisted_row(decision)
    store = LiveBettingStore(":memory:")
    store.init_schema()
    columns = tuple(row)
    store.connection.execute(
        f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(row[column] for column in columns),
    )
    result = verify_m1_qualifying_rejection(
        store.connection, decision.decision_key
    )
    assert not result.m1_qualifying_rejection
    assert result.reason == "profile_or_model_refs_incomplete"
    assert result.replay_reason == "replayed"
    store.close()


def test_same_proposal_version_malformed_existing_contract_blocks_insert() -> None:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    observation = _observation()
    surface = build_market_surface(_snapshots(NOW))
    first = no_signal_decision(
        observation=observation,
        surface=surface,
        decided_at=NOW,
        reason="strict_live_ineligible:first",
        strategy_version=PROPOSED_STRATEGY_VERSION,
    )
    columns = (
        "decision_key", "raybet_match_id", "map_number", "decided_at",
        "underdog_side", "market_probability", "model_probability", "edge",
        "data_quality", "eligible", "reason", "contributions_json",
        "input_ref", "strategy_version",
    )
    store.connection.execute(
        f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        (
            first.decision_key, first.raybet_match_id, first.map_number,
            first.decided_at.isoformat(), first.underdog_side,
            first.market_probability, first.model_probability, first.edge,
            first.data_quality, int(first.eligible), first.reason, "{}",
            first.input_ref, first.strategy_version,
        ),
    )
    store.connection.commit()
    second = no_signal_decision(
        observation=observation,
        surface=surface,
        decided_at=NOW + timedelta(seconds=1),
        reason="strict_live_ineligible:second",
        strategy_version=PROPOSED_STRATEGY_VERSION,
    )
    assert not store.insert_decision(second)
    store.close()


def test_legacy_v4_row_does_not_authorize_or_collide_with_v5_proposal() -> None:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    observation = _observation()
    surface = build_market_surface(_snapshots(NOW))
    legacy = no_signal_decision(
        observation=observation,
        surface=surface,
        decided_at=NOW,
        reason="strict_live_ineligible:legacy",
    )
    proposal = no_signal_decision(
        observation=observation,
        surface=surface,
        decided_at=NOW + timedelta(seconds=1),
        reason="strict_live_ineligible:proposal",
        strategy_version=PROPOSED_STRATEGY_VERSION,
    )
    assert store.insert_decision(legacy)
    assert store.insert_decision(proposal)
    store.close()


def test_full_adr0002_authority_rejection_is_m1_qualifying(
    tmp_path: Path,
) -> None:
    store, decision = _full_authority_fixture(tmp_path / "m1-positive.db")
    try:
        result = verify_m1_qualifying_rejection(
            store.connection, decision.decision_key
        )
        assert result.m1_qualifying_rejection
        assert result.reason == "qualifying_strategy_rejection"
        assert result.replay_reason == "replayed"
        assert result.strategy_version == PROPOSED_STRATEGY_VERSION
        assert result.evaluator_hash is not None
        assert result.policy_hash is not None
        assert result.serialization_version is not None
    finally:
        store.close()


def test_score_comeback_persisted_jcs_bytes_replay_exactly(tmp_path: Path) -> None:
    store, decision = _full_authority_fixture(tmp_path / "jcs-replay-positive.db")
    try:
        row = store.connection.execute(
            "SELECT * FROM strategy_decisions WHERE decision_key=?",
            (decision.decision_key,),
        ).fetchone()
        assert row is not None
        payload = parse_decision_payload(
            str(row["contributions_json"]),
            strategy_version=decision.strategy_version,
        )
        assert row["contributions_json"] == serialize_decision_payload(
            payload, strategy_version=decision.strategy_version
        )
        replay = replay_persisted_decision(dict(row))
        assert replay.valid
        assert replay.reason == "replayed"
        assert replay.expected_reason == decision.reason
    finally:
        store.close()


def test_full_authority_report_projects_m1_qualifying_rejection(
    tmp_path: Path,
) -> None:
    store, decision = _full_authority_fixture(tmp_path / "m1-report-positive.db")
    try:
        projection = build_report(store.connection)[
            "m1_strategy_contract_verifications"
        ]
        verification = next(
            row for row in projection
            if row["decision_key"] == decision.decision_key
        )
        contract = decision.inputs["strategy_contract"]
        assert verification == {
            "decision_key": decision.decision_key,
            "strategy_version": PROPOSED_STRATEGY_VERSION,
            "evaluator_hash": contract["evaluator_hash"],
            "policy_hash": contract["policy_hash"],
            "serialization_version": contract["serialization_version"],
            "m1_qualifying_rejection": True,
            "verifier_reason": "qualifying_strategy_rejection",
            "replay_reason": "replayed",
        }
    finally:
        store.close()


def _decision_json_attack(raw: str, attack: str, strategy_version: str) -> str:
    payload = parse_decision_payload(raw, strategy_version=strategy_version)
    if attack == "duplicate_same_key":
        return '{"zz_attack":1,"zz_attack":1,' + raw[1:]
    if attack == "duplicate_different_key":
        return '{"zz_attack":1,"zz_attack":2,' + raw[1:]
    if attack == "whitespace":
        return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    if attack == "key_order":
        reversed_payload = dict(reversed(list(payload.items())))
        return json.dumps(
            reversed_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    if attack == "nan":
        return '{"zz_attack":NaN,' + raw[1:]
    if attack == "infinity":
        return '{"zz_attack":Infinity,' + raw[1:]
    if attack == "contract_drift":
        payload["__inputs__"]["strategy_contract"]["evaluator_hash"] = "0" * 64
    elif attack == "input_drift":
        payload["__inputs__"]["canonical_evaluator_inputs"]["observation"][
            "game_clock_seconds"
        ] += 1
    else:  # pragma: no cover - test helper guard
        raise AssertionError(attack)
    return serialize_decision_payload(payload, strategy_version=strategy_version)


@pytest.mark.parametrize(
    ("attack", "failure"),
    (
        ("duplicate_same_key", "decision_json_duplicate_key"),
        ("duplicate_different_key", "decision_json_duplicate_key"),
        ("whitespace", "decision_json_not_canonical"),
        ("key_order", "decision_json_not_canonical"),
        ("nan", "decision_json_non_finite_number"),
        ("infinity", "decision_json_non_finite_number"),
        ("contract_drift", "strategy_contract_invalid"),
        ("input_drift", "persisted_evaluator_output_mismatch"),
    ),
)
def test_v5_json_and_replay_attacks_are_excluded_from_every_projection(
    tmp_path: Path,
    attack: str,
    failure: str,
) -> None:
    store, decision = _full_authority_fixture(tmp_path / f"jcs-{attack}.db")
    try:
        row = store.connection.execute(
            "SELECT * FROM strategy_decisions WHERE decision_key=?",
            (decision.decision_key,),
        ).fetchone()
        assert row is not None
        attacked = _decision_json_attack(
            str(row["contributions_json"]), attack, decision.strategy_version
        )
        attacked_row = {**dict(row), "contributions_json": attacked}
        replay = replay_persisted_decision(attacked_row)
        assert not replay.valid
        assert replay.reason == failure

        store.connection.execute("DROP TRIGGER strategy_decisions_immutable_update")
        store.connection.execute(
            "UPDATE strategy_decisions SET contributions_json=? WHERE decision_key=?",
            (attacked, decision.decision_key),
        )
        store.connection.commit()

        verification = verify_m1_qualifying_rejection(
            store.connection, decision.decision_key
        )
        assert not verification.m1_qualifying_rejection
        assert verification.reason == "canonical_replay_failed"
        assert verification.replay_reason == failure

        report = build_report(store.connection)
        assert report["decision_count"] == 0
        assert report["decision_payload_invalid_count"] == 1
        assert report["decision_payload_exclusion_reasons"] == {failure: 1}
        assert report["decision_audit"]["exclusion_reasons"][failure] == 1

        detail = monitor_match_detail(
            store.connection,
            decision.raybet_match_id,
            now=NOW + timedelta(seconds=1),
        )
        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        assert strategy["status"] == "review"
        assert strategy["reason"] == "strategy_evidence_invalid"
        assert strategy["data"]["decisions"] == []
        assert strategy["data"]["excluded"][failure] == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "profile_case",
    ("stale", "wrong_team", "wrong_hash", "active_cutoff"),
)
def test_m1_persisted_profile_authority_tamper_matrix_fails_closed(
    tmp_path: Path,
    profile_case: str,
) -> None:
    store, decision = _full_authority_fixture(
        tmp_path / f"m1-profile-{profile_case}.db"
    )
    try:
        team_one = decision.inputs["team_one_intelligence"]
        style = team_one["team_style"]
        score = team_one["player_form"]["score_refs"][0]
        if profile_case == "stale":
            changed = store.connection.execute(
                """UPDATE team_style_profiles SET profile_cutoff=?
                     WHERE team_id=? AND profile_cutoff=? AND profile_version=?""",
                (
                    (NOW - timedelta(minutes=2)).isoformat(),
                    style["team_id"],
                    style["profile_cutoff"],
                    style["profile_version"],
                ),
            )
        elif profile_case == "wrong_team":
            wrong_account_id = decision.inputs["team_two_intelligence"][
                "player_form"
            ]["account_ids"][0]
            changed = store.connection.execute(
                """UPDATE player_map_scores SET account_id=?
                     WHERE match_id=? AND player_slot=? AND score_version=?""",
                (
                    wrong_account_id,
                    score["match_id"],
                    score["player_slot"],
                    score["score_version"],
                ),
            )
        elif profile_case == "wrong_hash":
            changed = store.connection.execute(
                """UPDATE team_style_profiles SET input_hash=?
                     WHERE team_id=? AND profile_cutoff=? AND profile_version=?""",
                (
                    "f" * 64,
                    style["team_id"],
                    style["profile_cutoff"],
                    style["profile_version"],
                ),
            )
        else:
            active_at = (NOW - timedelta(seconds=30)).isoformat()
            changed = store.connection.execute(
                """INSERT INTO team_style_profiles
                   (team_id, profile_cutoff, profile_version,
                    opportunity_counts_json, posterior_rates_json,
                    duration_quantiles_json, weighting_json,
                    effective_sample_size, input_hash, created_at)
                   SELECT team_id, ?, profile_version,
                          opportunity_counts_json, posterior_rates_json,
                          duration_quantiles_json, weighting_json,
                          effective_sample_size, ?, ?
                     FROM team_style_profiles
                    WHERE team_id=? AND profile_cutoff=? AND profile_version=?""",
                (
                    active_at,
                    hashlib.sha256(b"newer-active-profile").hexdigest(),
                    active_at,
                    style["team_id"],
                    style["profile_cutoff"],
                    style["profile_version"],
                ),
            )
        assert changed.rowcount == 1
        store.connection.commit()

        result = verify_m1_qualifying_rejection(
            store.connection, decision.decision_key
        )
        assert not result.m1_qualifying_rejection
        assert result.reason == "profile_or_model_refs_incomplete"
        assert result.replay_reason == "replayed"
    finally:
        store.close()


@pytest.mark.parametrize(
    "rosh_case",
    ("missing", "wrong_hash", "stale", "wrong_event"),
)
def test_m1_rosh_authority_tamper_matrix_fails_closed(
    tmp_path: Path,
    rosh_case: str,
) -> None:
    store, decision = _full_authority_fixture(
        tmp_path / f"m1-rosh-{rosh_case}.db",
        rosh_case=rosh_case,
    )
    try:
        result = verify_m1_qualifying_rejection(
            store.connection, decision.decision_key
        )
        assert not result.m1_qualifying_rejection
        assert result.reason == "rosh_authority_incomplete"
        assert result.replay_reason == "replayed"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("draft_case", "expected_reason"),
    (
        ("missing", "current_authority_incomplete"),
        ("future", "current_authority_incomplete"),
        ("artifact_hash_tamper", "current_authority_incomplete"),
        ("conflict", "authority_conflict"),
        ("expired", "authority_conflict"),
    ),
)
def test_m1_draft_authority_tamper_matrix_fails_closed(
    tmp_path: Path,
    draft_case: str,
    expected_reason: str,
) -> None:
    store, decision = _full_authority_fixture(
        tmp_path / f"m1-draft-{draft_case}.db"
    )
    try:
        row = store.connection.execute(
            "SELECT * FROM strategy_decisions WHERE decision_key=?",
            (decision.decision_key,),
        ).fetchone()
        assert row is not None
        if draft_case == "missing":
            store.connection.execute("PRAGMA foreign_keys=OFF")
            store.connection.execute(
                "DROP TRIGGER prospective_draft_landmarks_immutable_delete"
            )
            store.connection.execute(
                "DELETE FROM prospective_draft_landmarks WHERE landmark_key=?",
                (row["draft_landmark_key"],),
            )
        elif draft_case == "future":
            store.connection.execute(
                "DROP TRIGGER prospective_draft_curves_immutable_update"
            )
            store.connection.execute(
                "UPDATE prospective_draft_curves SET first_usable_at=? WHERE curve_key=?",
                ((NOW + timedelta(seconds=1)).isoformat(), row["draft_curve_key"]),
            )
        elif draft_case == "artifact_hash_tamper":
            store.connection.execute("PRAGMA foreign_keys=OFF")
            store.connection.execute(
                "DROP TRIGGER prospective_draft_landmarks_immutable_update"
            )
            store.connection.execute(
                "UPDATE prospective_draft_landmarks SET model_hash=? WHERE landmark_key=?",
                ("f" * 64, row["draft_landmark_key"]),
            )
        else:
            store.connection.execute(
                """INSERT INTO vision_derived_invalidations
                   (dependent_type, dependent_key, raybet_match_id, map_number,
                    reason, block_reason, recorded_at)
                   VALUES ('strategy_decision', ?, ?, ?, ?, ?, ?)""",
                (
                    decision.decision_key,
                    decision.raybet_match_id,
                    decision.map_number,
                    f"draft_landmark_{draft_case}",
                    f"draft_landmark_{draft_case}",
                    NOW.isoformat(),
                ),
            )
        store.connection.commit()

        result = verify_m1_qualifying_rejection(
            store.connection, decision.decision_key
        )
        assert not result.m1_qualifying_rejection
        assert result.reason == expected_reason
        assert result.replay_reason == "replayed"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("vision_case", "expected_reason"),
    (
        ("current_unconfirmed", "current_authority_incomplete"),
        ("current_frame_hash", "current_authority_incomplete"),
        ("current_side", "current_authority_incomplete"),
        ("current_clock", "current_authority_incomplete"),
        ("previous_unconfirmed", "current_authority_incomplete"),
        ("previous_frame_hash", "previous_vision_authority_incomplete"),
        ("previous_side", "current_authority_incomplete"),
        ("previous_clock", "previous_vision_authority_incomplete"),
        ("previous_stale", "current_authority_incomplete"),
        ("kills", "canonical_replay_failed"),
        ("economy", "canonical_replay_failed"),
    ),
)
def test_m1_vision_authority_tamper_matrix_fails_closed(
    tmp_path: Path,
    vision_case: str,
    expected_reason: str,
) -> None:
    store, decision = _full_authority_fixture(
        tmp_path / f"m1-vision-{vision_case}.db"
    )
    try:
        row = store.connection.execute(
            "SELECT * FROM strategy_decisions WHERE decision_key=?",
            (decision.decision_key,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["contributions_json"]))
        previous = payload["__inputs__"]["previous_vision"]
        if vision_case.startswith("current_"):
            field, value = {
                "current_unconfirmed": ("confirmed", 0),
                "current_frame_hash": ("source_frame_sha256", "f" * 64),
                "current_side": ("radiant_team_side", "team_two"),
                "current_clock": ("game_clock_seconds", 999),
            }[vision_case]
            if vision_case == "current_frame_hash":
                store.connection.execute(
                    "DROP TRIGGER vision_observation_frame_identity_immutable"
                )
            store.connection.execute(
                f"""UPDATE vision_observations SET {field}=?
                     WHERE raybet_match_id=? AND captured_at=?
                       AND source_frame_ref=?""",
                (
                    value,
                    decision.raybet_match_id,
                    row["vision_captured_at"],
                    row["vision_source_frame_ref"],
                ),
            )
        elif vision_case.startswith("previous_"):
            field, value = {
                "previous_unconfirmed": ("confirmed", 0),
                "previous_frame_hash": ("source_frame_sha256", "e" * 64),
                "previous_side": ("radiant_team_side", "team_two"),
                "previous_clock": ("game_clock_seconds", 999),
                "previous_stale": (
                    "captured_at",
                    (NOW - timedelta(minutes=3)).isoformat(),
                ),
            }[vision_case]
            if vision_case == "previous_frame_hash":
                store.connection.execute(
                    "DROP TRIGGER vision_observation_frame_identity_immutable"
                )
            store.connection.execute(
                f"""UPDATE vision_observations SET {field}=?
                     WHERE raybet_match_id=? AND captured_at=?
                       AND source_frame_ref=?""",
                (
                    value,
                    decision.raybet_match_id,
                    previous["captured_at"],
                    previous["source_frame_ref"],
                ),
            )
        else:
            state = payload["__inputs__"]["canonical_evaluator_inputs"][
                "observation"
            ]["comeback_state"]
            if vision_case == "kills":
                state["radiant_kills"] = 99
            else:
                state["net_worth_advantage_min"] = 1_000
            store.connection.execute(
                "DROP TRIGGER strategy_decisions_immutable_update"
            )
            store.connection.execute(
                "UPDATE strategy_decisions SET contributions_json=? WHERE decision_key=?",
                (store.json(payload), decision.decision_key),
            )
        store.connection.commit()

        result = verify_m1_qualifying_rejection(
            store.connection, decision.decision_key
        )
        assert not result.m1_qualifying_rejection
        assert result.reason == expected_reason
    finally:
        store.close()


@pytest.mark.parametrize(
    "market_case",
    (
        "missing_current",
        "missing_previous",
        "reused_key",
        "browser_current",
        "browser_previous",
    ),
)
def test_m1_direct_market_core_tamper_matrix_fails_closed(
    tmp_path: Path,
    market_case: str,
) -> None:
    store, decision = _full_authority_fixture(
        tmp_path / f"m1-market-{market_case}.db"
    )
    try:
        if market_case.startswith("missing_"):
            key = (
                "m1-current-transport"
                if market_case.endswith("current")
                else "m1-previous-transport"
            )
            store.connection.execute("PRAGMA foreign_keys=OFF")
            store.connection.execute(
                "DROP TRIGGER odds_transport_observations_immutable_delete"
            )
            store.connection.execute(
                "DELETE FROM odds_transport_observations WHERE observation_key=?",
                (key,),
            )
        elif market_case == "reused_key":
            row = store.connection.execute(
                "SELECT contributions_json FROM strategy_decisions WHERE decision_key=?",
                (decision.decision_key,),
            ).fetchone()
            payload = json.loads(str(row["contributions_json"]))
            payload["__inputs__"]["transport"]["previous_key"] = (
                "m1-current-transport"
            )
            store.connection.execute(
                "DROP TRIGGER strategy_decisions_immutable_update"
            )
            store.connection.execute(
                "UPDATE strategy_decisions SET contributions_json=? WHERE decision_key=?",
                (store.json(payload), decision.decision_key),
            )
        else:
            key = (
                "m1-current-transport"
                if market_case.endswith("current")
                else "m1-previous-transport"
            )
            store.connection.execute(
                "DROP TRIGGER odds_transport_observations_guard_update"
            )
            store.connection.execute(
                "UPDATE odds_transport_observations SET source='browser' WHERE observation_key=?",
                (key,),
            )
        store.connection.commit()

        result = verify_m1_qualifying_rejection(
            store.connection, decision.decision_key
        )
        assert not result.m1_qualifying_rejection
        assert result.reason in {
            "canonical_replay_failed",
            "current_authority_incomplete",
            "transport_authority_incomplete",
        }
    finally:
        store.close()


@pytest.mark.parametrize(
    "market_case",
    (
        "wrong_match_current",
        "wrong_match_previous",
        "wrong_time_current",
        "wrong_time_previous",
        "wrong_hash_current",
        "wrong_hash_previous",
        "price_current",
        "price_previous",
        "winner_missing_current",
        "winner_missing_previous",
        "wrong_map_previous",
        "late_previous",
        "unprocessed_previous",
        "stability_move",
    ),
)
def test_m1_direct_market_identity_tamper_matrix_fails_closed(
    tmp_path: Path,
    market_case: str,
) -> None:
    store, decision = _full_authority_fixture(
        tmp_path / f"m1-market-{market_case}.db"
    )
    try:
        target = (
            "m1-current-transport"
            if market_case.endswith("current")
            else "m1-previous-transport"
        )
        if market_case == "stability_move":
            target = "m1-previous-transport"
        transport = store.connection.execute(
            "SELECT * FROM odds_transport_observations WHERE observation_key=?",
            (target,),
        ).fetchone()
        assert transport is not None
        if market_case.startswith(("wrong_match_", "wrong_time_", "wrong_hash_")):
            store.connection.execute("PRAGMA foreign_keys=OFF")
            store.connection.execute(
                "DROP TRIGGER odds_transport_observations_guard_update"
            )
            if market_case.startswith("wrong_match_"):
                field, value = "raybet_match_id", "wrong-match"
            elif market_case.startswith("wrong_time_"):
                field, value = "observed_at", (
                    NOW + timedelta(seconds=1)
                    if target == "m1-current-transport"
                    else NOW - timedelta(seconds=4)
                ).isoformat()
            else:
                field, value = "response_state_hash", "f" * 64
            store.connection.execute(
                f"UPDATE odds_transport_observations SET {field}=? WHERE observation_key=?",
                (value, target),
            )
        elif market_case in {"late_previous", "unprocessed_previous"}:
            store.connection.execute(
                "DROP TRIGGER odds_transport_observations_guard_update"
            )
            field, value = (
                ("timing_status", "late")
                if market_case == "late_previous"
                else ("processing_status", "processing")
            )
            store.connection.execute(
                f"UPDATE odds_transport_observations SET {field}=? WHERE observation_key=?",
                (value, target),
            )
        else:
            state_hash = str(transport["response_state_hash"])
            market = store.connection.execute(
                """SELECT underdog_odds_id FROM trusted_odds_winner_market_authority
                    WHERE observation_key=? AND period='map_1'""",
                (target,),
            ).fetchone()
            assert market is not None
            odds_id = str(market["underdog_odds_id"])
            if market_case.startswith("winner_missing_"):
                store.connection.execute(
                    "DROP TRIGGER odds_response_state_outcomes_immutable_delete"
                )
                store.connection.execute(
                    """DELETE FROM odds_response_state_outcomes
                        WHERE response_state_hash=? AND odds_id=?""",
                    (state_hash, odds_id),
                )
            else:
                store.connection.execute(
                    "DROP TRIGGER odds_response_state_outcomes_immutable_update"
                )
                field, value = (
                    ("period", "map_2")
                    if market_case == "wrong_map_previous"
                    else ("price", 9.0)
                )
                store.connection.execute(
                    f"""UPDATE odds_response_state_outcomes SET {field}=?
                         WHERE response_state_hash=? AND odds_id=?""",
                    (value, state_hash, odds_id),
                )
        store.connection.commit()

        result = verify_m1_qualifying_rejection(
            store.connection, decision.decision_key
        )
        assert not result.m1_qualifying_rejection
        assert result.reason in {
            "current_authority_incomplete",
            "transport_authority_incomplete",
        }
    finally:
        store.close()
