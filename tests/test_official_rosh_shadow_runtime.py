from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from live_betting.official_rosh_shadow_strategy import (
    OfficialRoshDirectionShadowStrategy,
)
from live_betting.report import build_report
from live_betting.rosh_evidence import official_rosh_draft_hash
from live_betting.rosh_parity_storage import (
    RoshHeroScoreRecord,
    RoshMinutePointRecord,
    RoshRunRecord,
    RoshRunRepository,
)
from live_betting.shadow_monitor import _record_official_rosh_shadow_evaluation
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


def _stored_run(store: LiveBettingStore):
    profile = get_profile()
    draft = _draft()
    run = RoshRunRecord(
        run_id=_hash("official-shadow-run"),
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
        request_hash=_hash("official-shadow-request"),
        request_manifest={
            "schema": "rosh-request-manifest/v1",
            "operations": ["GetMatchPicksBans"],
        },
        response_manifest=(
            {
                "operation_name": "GetMatchPicksBans",
                "request_artifact_hash": _hash("official-shadow-request-artifact"),
                "response_artifact_hash": _hash("official-shadow-response-artifact"),
                "collected_at": "2026-07-29T00:00:00Z",
                "relative_path": "stratz/GetMatchPicksBans.json",
            },
        ),
        evidence_hash=_hash("official-shadow-evidence"),
        collected_at="2026-07-29T00:00:00Z",
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

        report = build_report(store.connection)
        summary = report["official_rosh_v6_shadow"]
        assert summary["status"] == "shadow_only_no_calibration"
        assert summary["shadow_candidates"] == 1
        assert summary["rejections"] == 2
        assert summary["paper_orders"] == 0
        assert summary["m3_e_records"] == 0
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
