from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from live_betting.official_rosh_shadow_strategy import (
    OfficialRoshDirectionShadowStrategy,
)
from live_betting.health import record_health
from live_betting.report import build_report
from live_betting.rosh_evidence import official_rosh_draft_hash
from live_betting.rosh_parity_storage import (
    RoshHeroScoreRecord,
    RoshMinutePointRecord,
    RoshRunRecord,
    RoshRunRepository,
)
from live_betting.shadow_monitor import (
    _official_rosh_analysis_date_time,
    _record_official_rosh_shadow_evaluation,
)
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation
from prematch.stratz_official_profile import get_profile


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _draft() -> dict[str, list[dict[str, int]]]:
    return {
        "radiant": [
            {"hero_id": position, "position_id": position}
            for position in range(1, 6)
        ],
        "dire": [
            {"hero_id": position + 5, "position_id": position}
            for position in range(1, 6)
        ],
    }


def _stored_run(
    store: LiveBettingStore,
    *,
    collected_at: str = "2026-07-29T00:00:00Z",
    label: str = "official-shadow",
):
    profile = get_profile()
    draft = _draft()
    run = RoshRunRecord(
        run_id=_hash(f"{label}-run"),
        status="succeeded",
        mode="explicit_draft",
        match_id=None,
        date_time=1_784_485_548,
        draft_hash=official_rosh_draft_hash(
            tuple(range(1, 6)),
            tuple(range(6, 11)),
        ),
        draft=draft,
        rosh_profile_id=profile.rosh_profile_id,
        formula_version=profile.formula_version,
        request_profile_hash=profile.request_profile_hash,
        upstream_bundle_hash=profile.upstream_bundle_hash,
        scorer_source_hash=profile.scorer_source_hash,
        canonical_profile_hash=profile.canonical_profile_hash,
        serialization_version=profile.serialization_version,
        request_hash=_hash(f"{label}-request"),
        request_manifest={
            "schema": "rosh-request-manifest/v1",
            "operations": ["GetMatchPicksBans"],
        },
        response_manifest=(
            {
                "operation_name": "GetMatchPicksBans",
                "request_artifact_hash": _hash(f"{label}-request-artifact"),
                "response_artifact_hash": _hash(f"{label}-response-artifact"),
                "collected_at": collected_at,
                "relative_path": "stratz/GetMatchPicksBans.json",
            },
        ),
        evidence_hash=_hash(f"{label}-evidence"),
        collected_at=collected_at,
        radiant_team_score=-4.9,
        dire_team_score=-10.7,
        relative_advantage=5.8,
    )
    heroes = tuple(
        RoshHeroScoreRecord(
            team_side=side,
            position_id=position,
            hero_id=position + offset,
            raw_score=0.1 * position,
            display_score=0.1 * position,
            components={
                "position_base_diff": 0.1,
                "same_team_synergy": 0.2,
                "opponent_matchup_synergy": -0.2,
            },
        )
        for side, offset in (("RADIANT", 0), ("DIRE", 5))
        for position in range(1, 6)
    )
    minutes = tuple(
        RoshMinutePointRecord(
            minute=minute,
            raw_score=-5.5,
            display_score=-5.5,
            radiant_time_delta=-1.0,
            dire_time_delta=1.0,
            synergy_delta=-5.5,
            source_audit={"rank_source_counts": {}, "slots": []},
        )
        for minute in range(20, 61)
    )
    return RoshRunRepository(store.connection).write_succeeded(run, heroes, minutes)


def _observation(
    *,
    radiant: tuple[int, ...] = tuple(range(1, 6)),
    dire: tuple[int, ...] = tuple(range(6, 11)),
) -> VisionObservation:
    return VisionObservation(
        raybet_match_id="raybet-1001",
        map_number=1,
        captured_at=datetime(2026, 7, 29, 0, 0, 30, tzinfo=timezone.utc),
        game_clock_seconds=36 * 60 + 59,
        is_paused=False,
        radiant_hero_ids=radiant,
        dire_hero_ids=dire,
        clock_confidence=0.99,
        draft_confidence=0.99,
        source_frame_ref="vision-frame:official-rosh-v6",
        screen_state="game",
        radiant_team_side="team_one",
    )


def test_runtime_records_candidate_and_rejection_without_probability_or_order() -> None:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    try:
        stored = _stored_run(store)
        strategy = OfficialRoshDirectionShadowStrategy()
        decided_at = datetime(2026, 7, 29, 0, 1, tzinfo=timezone.utc)

        candidate = _record_official_rosh_shadow_evaluation(
            store,
            strategy,
            _observation(),
            map_number=1,
            underdog_side="team_two",
            decided_at=decided_at,
            transport_key="transport-official-rosh-1",
        )
        duplicate = _record_official_rosh_shadow_evaluation(
            store,
            strategy,
            _observation(),
            map_number=1,
            underdog_side="team_two",
            decided_at=decided_at,
            transport_key="transport-official-rosh-1",
        )
        rejection = _record_official_rosh_shadow_evaluation(
            store,
            strategy,
            _observation(
                radiant=tuple(range(11, 16)),
                dire=tuple(range(16, 21)),
            ),
            map_number=1,
            underdog_side="team_two",
            decided_at=decided_at,
            transport_key="transport-official-rosh-2",
        )
        opposing = _record_official_rosh_shadow_evaluation(
            store,
            strategy,
            _observation(),
            map_number=1,
            underdog_side="team_one",
            decided_at=decided_at,
            transport_key="transport-official-rosh-3",
        )

        assert candidate["status"] == "shadow_candidate"
        assert candidate["reason"] == "calibrated_probability_unavailable"
        assert candidate["analysis_run_id"] == stored.run.run_id
        assert candidate["inserted"] is True
        assert duplicate["evaluation_key"] == candidate["evaluation_key"]
        assert duplicate["inserted"] is False
        assert rejection["status"] == "rejected"
        assert rejection["reason"] == "rosh_analysis_unavailable"
        assert opposing["status"] == "rejected"
        assert opposing["reason"] == "rosh_direction_opposes_underdog"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM official_rosh_shadow_evaluations"
        ).fetchone()[0] == 3
        records = [
            json.loads(str(row[0]))
            for row in store.connection.execute(
                "SELECT record_json FROM official_rosh_shadow_evaluations"
            )
        ]
        assert all(record["calibrated_probability"] is None for record in records)
        assert all(record["edge"] is None for record in records)
        assert all(record["stake_multiplier"] is None for record in records)
        assert all(record["paper_order"] is None for record in records)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM strategy_decisions"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM shadow_orders"
        ).fetchone()[0] == 0

        record_health(
            store.connection,
            "shadow_worker",
            "healthy",
            heartbeat_at=decided_at,
            success_at=decided_at,
            details={"official_rosh_status": "pending"},
        )
        report = build_report(store.connection)
        summary = report["official_rosh_v6_shadow"]
        assert summary["status"] == "shadow_only_no_calibration"
        assert summary["shadow_candidates"] == 1
        assert summary["rejections"] == 2
        assert summary["paper_orders"] == 0
        assert summary["m3_e_records"] == 0
        assert summary["analysis_statuses"] == {
            "pending": 0,
            "succeeded": 2,
            "failed": 0,
            "unavailable": 1,
        }
        assert summary["analysis_runs"] == {"succeeded": 1, "failed": 0}
        assert summary["current_analysis_status"] == "pending"
        assert report["strategy_versions"] == {}

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute(
                "UPDATE official_rosh_shadow_evaluations SET reason='changed'"
            )
    finally:
        store.close()


def test_runtime_helper_uses_market_side_mapping_for_dire_underdog() -> None:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    try:
        _stored_run(store)
        result = _record_official_rosh_shadow_evaluation(
            store,
            OfficialRoshDirectionShadowStrategy(),
            _observation(),
            map_number=1,
            underdog_side="team_two",
            decided_at=datetime(2026, 7, 29, 0, 1, tzinfo=timezone.utc),
            transport_key="transport-side-mapping",
        )

        assert result["status"] == "shadow_candidate"
        record = json.loads(
            str(
                store.connection.execute(
                    "SELECT record_json FROM official_rosh_shadow_evaluations"
                ).fetchone()[0]
            )
        )
        evidence = record["rosh_direction_evidence"]
        assert evidence["selected_minute"] == 36
        assert evidence["underdog_side"] == "DIRE"
        assert evidence["underdog_direction_score"] == 5.5
    finally:
        store.close()


def test_pending_submission_uses_exact_official_draft_identity() -> None:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    coordinator = MagicMock()
    coordinator.poll_or_submit.return_value = SimpleNamespace(
        status="pending", attempts=1
    )
    try:
        result = _record_official_rosh_shadow_evaluation(
            store,
            OfficialRoshDirectionShadowStrategy(),
            _observation(),
            map_number=1,
            underdog_side="team_two",
            decided_at=datetime(2026, 7, 29, 0, 1, tzinfo=timezone.utc),
            transport_key="transport-pending",
            analysis_date_time=1_785_283_200,
            request_started_at=datetime(2026, 7, 29, 0, 1, 1, tzinfo=timezone.utc),
            run_coordinator=coordinator,
        )

        assert result["analysis_status"] == "pending"
        assert result["status"] == "rejected"
        assert result["reason"] == "rosh_analysis_unavailable"
        submitted_key = coordinator.poll_or_submit.call_args.args[0]
        assert submitted_key.draft_hash == official_rosh_draft_hash(
            tuple(range(1, 6)), tuple(range(6, 11))
        )
        assert submitted_key.date_time == 1_785_283_200
        record = json.loads(
            str(
                store.connection.execute(
                    "SELECT record_json FROM official_rosh_shadow_evaluations"
                ).fetchone()[0]
            )
        )
        assert record["calibrated_probability"] is None
        assert record["edge"] is None
        assert record["stake_multiplier"] is None
        assert record["paper_order"] is None
    finally:
        store.close()


def test_future_run_is_not_consumed_until_the_next_legal_transport() -> None:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    try:
        stored = _stored_run(
            store,
            collected_at="2026-07-29T00:01:30Z",
            label="future-evidence",
        )
        old_transport = _record_official_rosh_shadow_evaluation(
            store,
            OfficialRoshDirectionShadowStrategy(),
            _observation(),
            map_number=1,
            underdog_side="team_two",
            decided_at=datetime(2026, 7, 29, 0, 1, tzinfo=timezone.utc),
            transport_key="transport-before-collection",
        )
        next_transport = _record_official_rosh_shadow_evaluation(
            store,
            OfficialRoshDirectionShadowStrategy(),
            _observation(),
            map_number=1,
            underdog_side="team_two",
            decided_at=datetime(2026, 7, 29, 0, 2, tzinfo=timezone.utc),
            transport_key="transport-after-collection",
        )

        assert old_transport["analysis_status"] == "unavailable"
        assert old_transport["analysis_run_id"] is None
        assert old_transport["reason"] == "rosh_analysis_unavailable"
        assert next_transport["analysis_status"] == "succeeded"
        assert next_transport["analysis_run_id"] == stored.run.run_id
        assert next_transport["status"] == "shadow_candidate"
        identity_run = RoshRunRepository(
            store.connection
        ).get_succeeded_for_explicit_identity(
            stored.run.draft_hash,
            rosh_profile_id=stored.run.rosh_profile_id,
            canonical_profile_hash=stored.run.canonical_profile_hash,
            date_time=stored.run.date_time,
        )
        assert identity_run is not None
        assert identity_run.run.run_id == stored.run.run_id
    finally:
        store.close()


def test_stable_analysis_time_comes_from_matching_anchored_draft() -> None:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    observation = _observation()
    anchored_at = "2026-07-29T00:00:10+00:00"
    try:
        store.connection.execute(
            """INSERT INTO vision_draft_anchors
               (raybet_match_id, map_number, draft_hash, radiant_hero_ids,
                dire_hero_ids, radiant_team_side, team_side_anchored_at,
                team_side_source_frame_ref, anchored_at, source_frame_ref,
                status, conflict_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'anchored', NULL)""",
            (
                observation.raybet_match_id,
                1,
                _hash("vision-anchor"),
                json.dumps(list(observation.radiant_hero_ids)),
                json.dumps(list(observation.dire_hero_ids)),
                "team_one",
                anchored_at,
                "vision-frame:official-rosh-v6",
                anchored_at,
                "vision-frame:official-rosh-v6",
            ),
        )

        assert _official_rosh_analysis_date_time(
            store, observation, map_number=1
        ) == int(datetime.fromisoformat(anchored_at).timestamp())
        assert (
            _official_rosh_analysis_date_time(
                store,
                _observation(radiant=(2, 1, 3, 4, 5)),
                map_number=1,
            )
            is None
        )
    finally:
        store.close()
