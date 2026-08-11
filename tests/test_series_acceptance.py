from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contracts.live_observation import LiveObservation
import live_betting.series_acceptance as acceptance
from live_betting.series_acceptance import (
    _OfficialMapEvidence,
    _audit_map,
    _candidate_series_ids,
    _checkpoint_audit,
    _checkpoint_trace_is_consistent,
    _filesystem_series_ids,
    _manifest_audit,
    _odds_audit,
    _result_audit,
    _roster_audit,
)
from live_betting.live_match_state import sourced_manual_draft_authority
from live_betting.vision_frame_registry import publish_vision_frame_bytes
from scripts.watch_raybet_stream import _write_sample_manifest


class _Result:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def fetchall(self) -> list[object]:
        return self.rows

    def fetchone(self) -> object | None:
        return self.rows[0] if self.rows else None


class _Connection:
    def execute(self, query: str, _parameters=()) -> _Result:
        if "SELECT DISTINCT raybet_match_id FROM vision_observations" in query:
            return _Result([("older-failed",), ("newest-pass",)])
        if "FROM raybet_matches" in query:
            assert "ORDER BY scheduled_at DESC NULLS LAST" in query
            assert "updated_at" not in query
            return _Result(
                [
                    {
                        "raybet_match_id": "newest-pass",
                        "status": "3",
                        "scheduled_at": "2026-08-11 12:00:00",
                    },
                    {
                        "raybet_match_id": "filesystem-only",
                        "status": "finished",
                        "scheduled_at": "2026-08-11 11:00:00",
                    },
                    {
                        "raybet_match_id": "older-failed",
                        "status": "3",
                        "scheduled_at": "2026-08-11 10:00:00",
                    },
                    {
                        "raybet_match_id": "still-live",
                        "status": "2",
                        "scheduled_at": "2026-08-11 13:00:00",
                    },
                ]
            )
        raise AssertionError(query)


def test_candidate_series_are_watched_ended_and_newest_first(tmp_path: Path) -> None:
    (tmp_path / "series" / "filesystem-only").mkdir(parents=True)

    assert _candidate_series_ids(  # type: ignore[arg-type]
        _Connection(),
        tmp_path,
        limit=2,
    ) == ["newest-pass", "filesystem-only"]


def test_filesystem_series_ids_include_map_tree_and_lifecycle_manifest(
    tmp_path: Path,
) -> None:
    (tmp_path / "series" / "series-one" / "map_1").mkdir(parents=True)
    (tmp_path / "series-two.manifest.jsonl").write_text("{}\n", encoding="utf-8")

    assert _filesystem_series_ids(tmp_path) == {"series-one", "series-two"}


def test_manifest_audit_requires_one_explicit_row_per_database_sample(
    tmp_path: Path,
) -> None:
    captured_at = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
    evidence_root = tmp_path / "evidence"
    receipt = publish_vision_frame_bytes(evidence_root, b"encoded-frame")
    observation = LiveObservation(
        raybet_match_id="series-one",
        map_number=2,
        captured_at_utc=captured_at,
        game_clock_seconds=10,
        is_paused=False,
        clock_confidence=0.95,
        source_frame_ref=receipt.frame_ref,
        source_frame_sha256=receipt.content_sha256,
        source_frame_bytes=receipt.byte_length,
        source_frame_path=str(receipt.storage_path),
        screen_state="game",
    )
    _write_sample_manifest(
        evidence_root,
        observation=observation,
        receipt=receipt,
        lifecycle_events=(),
    )
    row = {
        "captured_at": captured_at.isoformat(),
        "source_frame_ref": receipt.frame_ref,
        "source_frame_sha256": receipt.content_sha256,
        "source_frame_bytes": receipt.byte_length,
        "registered_sha256": receipt.content_sha256,
        "registered_bytes": receipt.byte_length,
        "storage_path": str(receipt.storage_path),
    }

    class _ManifestConnection:
        def execute(self, _query: str, _parameters=()) -> _Result:
            return _Result([row])

    accepted = _manifest_audit(
        _ManifestConnection(),  # type: ignore[arg-type]
        "series-one",
        2,
        evidence_root=evidence_root,
        verify_frame_bytes=True,
    )
    assert accepted["status"] == "accepted"
    assert accepted["database_sample_count"] == 1
    assert accepted["manifest_sample_count"] == 1

    second_row = {
        **row,
        "captured_at": (captured_at + timedelta(seconds=1)).isoformat(),
    }

    class _MissingManifestConnection:
        def execute(self, _query: str, _parameters=()) -> _Result:
            return _Result([row, second_row])

    incomplete = _manifest_audit(
        _MissingManifestConnection(),  # type: ignore[arg-type]
        "series-one",
        2,
        evidence_root=evidence_root,
        verify_frame_bytes=False,
    )
    assert incomplete["status"] == "incomplete"
    assert incomplete["reason"] == "sample_manifest_database_mismatch"
    assert incomplete["missing_manifest_sample_count"] == 1


def test_manifest_audit_identifies_unretained_map_samples(tmp_path: Path) -> None:
    captured_at = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)

    class _UnretainedConnection:
        def execute(self, _query: str, _parameters=()) -> _Result:
            return _Result(
                [
                    {
                        "captured_at": captured_at.isoformat(),
                        "source_frame_ref": "stream:legacy:1",
                        "source_frame_sha256": None,
                        "source_frame_bytes": None,
                        "registered_sha256": None,
                        "registered_bytes": None,
                        "storage_path": None,
                    }
                ]
            )

    audit = _manifest_audit(
        _UnretainedConnection(),  # type: ignore[arg-type]
        "series-one",
        1,
        evidence_root=tmp_path,
        verify_frame_bytes=False,
    )

    assert audit["status"] == "incomplete"
    assert audit["reason"] == "unretained_map_sample_frame_missing"
    assert audit["database_sample_count"] == 1
    assert audit["unretained_sample_count"] == 1
    assert audit["registered_frame_error_count"] == 1


def test_checkpoint_audit_preserves_valid_historical_mapping_version() -> None:
    def checkpoint_row(mapping_version: int) -> dict[str, object]:
        return {
            "phase": "pregame",
            "checkpoint_minute": 0,
            "mapping_version": mapping_version,
            "input_versions_json": json.dumps(
                {
                    "strategy_version": "map-decision-shadow-v1",
                    "mapping_version": mapping_version,
                    "odds_authority": "trusted_odds_winner_market_authority",
                    "vision_authority": "trusted_vision_observation_authority",
                }
            ),
            "feature_availability_json": "{}",
            "strategy_version": "map-decision-shadow-v1",
            "assumed_stake_units": 1.0,
            "settlement_id": 17,
            "settlement_raybet_match_id": "series-one",
            "settlement_map_number": 2,
            "settlement_dota_match_id": 9002,
            "result_source": "confirmed_map_result",
            "reason": "pregame_prediction_unavailable",
            "decided_at": "2026-08-11T03:00:00+00:00",
            "odds_max_age_seconds": 150.0,
            "odds_age_seconds": None,
            "odds_observation_key": None,
            "odds_group_id": None,
            "odds_observed_at": None,
            "vision_max_age_seconds": None,
            "odds_vision_gap_max_seconds": None,
            "decision": "skip",
            "observed_price": None,
            "model_probability_team_one": None,
            "model_probability_team_two": None,
            "market_probability_team_one": None,
            "market_probability_team_two": None,
            "selected_edge": None,
            "vision_trusted": False,
            "vision_replay": False,
            "vision_snapshot_id": None,
            "vision_source_frame_ref": None,
            "vision_captured_at": None,
            "vision_game_time_seconds": None,
            "vision_networth_lead": None,
            "vision_radiant_kills": None,
            "vision_dire_kills": None,
            "vision_age_seconds": None,
            "snapshot_raybet_match_id": None,
            "snapshot_map_number": None,
            "snapshot_captured_at": None,
            "snapshot_game_time_seconds": None,
            "snapshot_networth_lead": None,
            "snapshot_radiant_kills": None,
            "snapshot_dire_kills": None,
            "snapshot_source_frame_ref": None,
        }

    class _CheckpointConnection:
        def __init__(
            self,
            mapping_version: int,
            overrides: dict[str, object] | None = None,
        ) -> None:
            self.mapping_version = mapping_version
            self.overrides = overrides or {}

        def execute(self, query: str, _parameters=()) -> _Result:
            if "SELECT version FROM live_draft_mappings" in query:
                return _Result([(1,), (2,)])
            if "FROM map_decision_checkpoints AS checkpoint" in query:
                return _Result(
                    [{**checkpoint_row(self.mapping_version), **self.overrides}]
                )
            raise AssertionError(query)

    accepted = _checkpoint_audit(
        _CheckpointConnection(1),  # type: ignore[arg-type]
        "series-one",
        2,
        duration_seconds=299,
        official_start_time="2026-08-11T02:59:00+00:00",
        official_match_id=9002,
    )
    invalid = _checkpoint_audit(
        _CheckpointConnection(3),  # type: ignore[arg-type]
        "series-one",
        2,
        duration_seconds=299,
        official_start_time="2026-08-11T02:59:00+00:00",
        official_match_id=9002,
    )
    stale_skip = _checkpoint_audit(
        _CheckpointConnection(
            1,
            {
                "reason": "pregame_odds_stale",
                "odds_age_seconds": 151.0,
                "odds_observation_key": "odds-1",
                "odds_group_id": "winner-map-2",
                "odds_observed_at": "2026-08-11T02:59:00+00:00",
                "model_probability_team_one": 0.6,
                "model_probability_team_two": 0.4,
                "market_probability_team_one": 0.55,
                "market_probability_team_two": 0.45,
            },
        ),  # type: ignore[arg-type]
        "series-one",
        2,
        duration_seconds=299,
        official_start_time="2026-08-11T02:59:00+00:00",
        official_match_id=9002,
    )
    stale_bet = _checkpoint_audit(
        _CheckpointConnection(
            1,
            {
                "decision": "bet_team_a",
                "reason": "minimum_edge_met",
                "odds_age_seconds": 151.0,
                "odds_observation_key": "odds-1",
                "odds_group_id": "winner-map-2",
                "odds_observed_at": "2026-08-11T02:59:00+00:00",
                "observed_price": 1.8,
                "model_probability_team_one": 0.65,
                "model_probability_team_two": 0.35,
                "market_probability_team_one": 0.55,
                "market_probability_team_two": 0.45,
                "selected_edge": 0.1,
            },
        ),  # type: ignore[arg-type]
        "series-one",
        2,
        duration_seconds=299,
        official_start_time="2026-08-11T02:59:00+00:00",
        official_match_id=9002,
    )
    postgame = _checkpoint_audit(
        _CheckpointConnection(
            1,
            {"decided_at": "2026-08-11T03:05:00+00:00"},
        ),  # type: ignore[arg-type]
        "series-one",
        2,
        duration_seconds=299,
        official_start_time="2026-08-11T02:59:00+00:00",
        official_match_id=9002,
    )
    cross_map_settlement = _checkpoint_audit(
        _CheckpointConnection(
            1,
            {"settlement_map_number": 1},
        ),  # type: ignore[arg-type]
        "series-one",
        2,
        duration_seconds=299,
        official_start_time="2026-08-11T02:59:00+00:00",
        official_match_id=9002,
    )

    assert accepted["status"] == "accepted"
    assert accepted["invalid_checkpoint_count"] == 0
    assert invalid["status"] == "incomplete"
    assert invalid["reason"] == "checkpoint_trace_or_settlement_invalid"
    assert invalid["invalid_checkpoint_count"] == 1
    assert stale_skip["status"] == "accepted"
    assert stale_bet["status"] == "incomplete"
    assert stale_bet["invalid_checkpoint_count"] == 1
    assert postgame["status"] == "incomplete"
    assert postgame["invalid_checkpoint_count"] == 1
    assert cross_map_settlement["status"] == "incomplete"
    assert cross_map_settlement["invalid_checkpoint_count"] == 1


def test_live_checkpoint_reason_must_match_recorded_threshold_values() -> None:
    row = {
        "odds_age_seconds": 1.0,
        "odds_max_age_seconds": 15.0,
        "odds_observation_key": "odds-5",
        "odds_group_id": "winner-map-2",
        "odds_observed_at": "2026-08-11T03:04:59+00:00",
        "model_probability_team_one": 0.6,
        "model_probability_team_two": 0.4,
        "market_probability_team_one": 0.55,
        "market_probability_team_two": 0.45,
        "vision_trusted": True,
        "vision_replay": False,
        "vision_age_seconds": 6.0,
        "vision_max_age_seconds": 5.0,
        "vision_radiant_kills": 8,
        "vision_dire_kills": 5,
        "odds_vision_gap_seconds": 1.0,
        "odds_vision_gap_max_seconds": 15.0,
    }
    features = {
        "vision_direction": True,
        "live_probability_model": {"available": False},
    }

    assert _checkpoint_trace_is_consistent(
        row,
        phase="live",
        decision="skip",
        reason="live_vision_stale",
        features=features,
    )
    assert not _checkpoint_trace_is_consistent(
        {**row, "vision_age_seconds": 4.0},
        phase="live",
        decision="skip",
        reason="live_vision_stale",
        features=features,
    )
    assert _checkpoint_trace_is_consistent(
        {
            **row,
            "vision_age_seconds": 1.0,
            "odds_vision_gap_seconds": 16.0,
        },
        phase="live",
        decision="skip",
        reason="live_odds_vision_gap_exceeded",
        features=features,
    )
    validated_features = {
        "vision_direction": True,
        "live_probability_model": {
            "available": True,
            "model_version": "vision-gold-lead-logit-v1",
        },
    }
    assert _checkpoint_trace_is_consistent(
        {
            **row,
            "vision_age_seconds": 1.0,
            "observed_price": 1.8,
            "selected_edge": 0.1,
        },
        phase="live",
        decision="bet_team_a",
        reason="minimum_edge_met",
        features=validated_features,
    )
    assert _checkpoint_trace_is_consistent(
        {
            **row,
            "vision_age_seconds": 1.0,
            "selected_edge": 0.05,
        },
        phase="live",
        decision="skip",
        reason="edge_below_threshold",
        features=validated_features,
    )
    missing_vision = {
        **row,
        "vision_trusted": False,
        "vision_age_seconds": None,
        "vision_radiant_kills": None,
        "vision_dire_kills": None,
    }
    assert _checkpoint_trace_is_consistent(
        missing_vision,
        phase="live",
        decision="skip",
        reason="trusted_vision_checkpoint_missing",
        features=features,
    )
    assert not _checkpoint_trace_is_consistent(
        {**missing_vision, "vision_game_time_seconds": 300},
        phase="live",
        decision="skip",
        reason="trusted_vision_checkpoint_missing",
        features=features,
    )
    missing_odds = {
        **row,
        "vision_age_seconds": 1.0,
        "odds_observation_key": None,
        "odds_group_id": None,
        "odds_observed_at": None,
        "odds_age_seconds": None,
        "market_probability_team_one": None,
        "market_probability_team_two": None,
    }
    assert _checkpoint_trace_is_consistent(
        missing_odds,
        phase="live",
        decision="skip",
        reason="live_odds_unavailable",
        features=features,
    )
    assert not _checkpoint_trace_is_consistent(
        {**missing_odds, "market_probability_team_one": 0.55},
        phase="live",
        decision="skip",
        reason="live_odds_unavailable",
        features=features,
    )


def test_pregame_after_map_start_skip_requires_recorded_authority_clock() -> None:
    row = {
        "odds_age_seconds": 1.0,
        "odds_max_age_seconds": 150.0,
        "odds_observation_key": "odds-1",
        "odds_group_id": "winner-map-1",
        "odds_observed_at": "2026-08-11T03:00:01+00:00",
        "market_probability_team_one": 0.55,
        "market_probability_team_two": 0.45,
        "model_probability_team_one": 0.65,
        "model_probability_team_two": 0.35,
        "selected_edge": None,
        "vision_trusted": False,
        "vision_replay": False,
        "vision_snapshot_id": None,
        "vision_source_frame_ref": None,
        "vision_captured_at": None,
        "vision_game_time_seconds": None,
        "vision_networth_lead": None,
        "vision_radiant_kills": None,
        "vision_dire_kills": None,
        "vision_age_seconds": None,
    }

    assert _checkpoint_trace_is_consistent(
        row,
        phase="pregame",
        decision="skip",
        reason="pregame_authority_after_map_start",
        features={
            "pregame_authority": {
                "draft_state_marker": "in_game",
                "game_clock_seconds": 1,
            }
        },
    )
    assert not _checkpoint_trace_is_consistent(
        row,
        phase="pregame",
        decision="skip",
        reason="pregame_authority_after_map_start",
        features={"pregame_authority": {}},
    )


def test_odds_audit_requires_last_open_quote_and_post_end_closure_evidence() -> None:
    started_at = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    official = _OfficialMapEvidence(2, 9002, started_at, "confirmed_map_result")
    pregame = (
        "pregame",
        started_at - timedelta(seconds=30),
        "audit_only",
        True,
    )
    live = ("live", started_at + timedelta(minutes=5), "processed", True)

    class _OddsConnection:
        def __init__(self, closure_count: int) -> None:
            self.closure_count = closure_count

        def execute(self, query: str, parameters=()) -> _Result:
            if "AS closure_observation_count" in query:
                assert "outcome.price>1.0" not in query
                assert parameters[:2] == ("series-one", "map_2")
                assert parameters[2:-1] == acceptance._CLOSED_ODDS_STATUSES
                return _Result(
                    [
                        (
                            self.closure_count,
                            started_at + timedelta(minutes=10)
                            if self.closure_count
                            else None,
                        )
                    ]
                )
            assert "transport.observed_at" in query
            assert "transport.processing_status IN ('audit_only', 'processed')" in query
            assert parameters == ("series-one", "map_2")
            return _Result([pregame, live])

    early = _odds_audit(
        _OddsConnection(0),  # type: ignore[arg-type]
        "series-one",
        2,
        official_link=official,
        duration_seconds=600,
    )
    accepted = _odds_audit(
        _OddsConnection(1),  # type: ignore[arg-type]
        "series-one",
        2,
        official_link=official,
        duration_seconds=600,
    )

    assert early["status"] == "incomplete"
    assert early["reason"] == "missing_odds_phase:closing"
    assert early["closing_complete_observation_count"] == 0
    assert accepted["status"] == "accepted"
    assert accepted["closing_complete_observation_count"] == 1
    assert accepted["pregame_processing_statuses"] == {"audit_only": 1}
    assert accepted["closing_quote_observation_key"] == "live"
    assert accepted["closure_evidence_observation_count"] == 1


def test_odds_audit_does_not_treat_audit_only_quote_as_live() -> None:
    started_at = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    official = _OfficialMapEvidence(1, 9001, started_at, "confirmed_map_result")
    rows = [
        ("pregame", started_at - timedelta(seconds=30), "audit_only", True),
        ("live-audit", started_at + timedelta(minutes=5), "audit_only", True),
    ]

    class _OddsConnection:
        def execute(self, query: str, parameters=()) -> _Result:
            if "AS closure_observation_count" in query:
                return _Result([(1, started_at + timedelta(minutes=10))])
            return _Result(rows)

    audit = _odds_audit(
        _OddsConnection(),  # type: ignore[arg-type]
        "series-one",
        1,
        official_link=official,
        duration_seconds=600,
    )

    assert audit["pregame_complete_observation_count"] == 1
    assert audit["live_complete_observation_count"] == 0
    assert audit["closing_complete_observation_count"] == 0
    assert audit["reason"] == "missing_odds_phase:live,closing"


def test_odds_audit_uses_exact_official_duration_when_result_is_not_synced() -> None:
    started_at = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    official = _OfficialMapEvidence(2, 9002, started_at, "raybet_explicit_map_time_unique")
    rows = [
        ("pregame", started_at - timedelta(seconds=30), "audit_only", True),
        ("live", started_at + timedelta(minutes=5), "processed", True),
    ]

    class _OddsConnection:
        def execute(self, query: str, parameters=()) -> _Result:
            if "AS closure_observation_count" in query:
                return _Result([(1, started_at + timedelta(minutes=10))])
            if "transport.processing_status IN ('audit_only', 'processed')" in query:
                assert parameters == ("series-one", "map_2")
                return _Result(rows)
            if "SELECT start_time, duration FROM matches" in query:
                assert parameters == (9002,)
                return _Result([(int(started_at.timestamp()), 600)])
            raise AssertionError(query)

    audit = _odds_audit(
        _OddsConnection(),  # type: ignore[arg-type]
        "series-one",
        2,
        official_link=official,
        duration_seconds=None,
    )

    assert audit["status"] == "accepted"
    assert audit["duration_source"] == "official_match_detail"
    assert audit["pregame_complete_observation_count"] == 1
    assert audit["live_complete_observation_count"] == 1
    assert audit["closing_complete_observation_count"] == 1


def test_result_audit_reports_unsettled_official_match_detail() -> None:
    started_at = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    official = _OfficialMapEvidence(1, 9001, started_at, "raybet_explicit_map_time_unique")

    class _ResultConnection:
        def execute(self, query: str, parameters=()) -> _Result:
            if "FROM map_results AS result" in query:
                assert parameters == ("series-one", 1)
                return _Result([])
            if "FROM settlement_result_evidence" in query:
                assert parameters == ("series-one", 1)
                return _Result([])
            if "SELECT start_time, duration, radiant_win FROM matches" in query:
                assert parameters == (9001,)
                return _Result([(int(started_at.timestamp()), 1800, True)])
            raise AssertionError(query)

    audit = _result_audit(
        _ResultConnection(),  # type: ignore[arg-type]
        "series-one",
        1,
        official,
    )

    assert audit["status"] == "incomplete"
    assert audit["reason"] == "official_match_result_not_settled"
    assert audit["result_source"] == "official_match_detail"
    assert audit["dota_match_id"] == 9001
    assert audit["duration_seconds"] == 1800
    assert audit["radiant_win"] is True


def test_result_audit_rejects_official_match_detail_with_start_mismatch() -> None:
    started_at = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    official = _OfficialMapEvidence(1, 9001, started_at, "raybet_explicit_map_time_unique")

    class _ResultConnection:
        def execute(self, query: str, _parameters=()) -> _Result:
            if "FROM map_results AS result" in query:
                return _Result([])
            if "FROM settlement_result_evidence" in query:
                return _Result([])
            if "SELECT start_time, duration, radiant_win FROM matches" in query:
                return _Result([(int(started_at.timestamp()) + 2, 1800, True)])
            raise AssertionError(query)

    audit = _result_audit(
        _ResultConnection(),  # type: ignore[arg-type]
        "series-one",
        1,
        official,
    )

    assert audit["status"] == "incomplete"
    assert audit["reason"] == "confirmed_map_result_missing"
    assert audit["duration_seconds"] is None


def test_result_audit_accepts_verified_independent_official_evidence() -> None:
    started_at = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    observed_at = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
    official = _OfficialMapEvidence(1, 9001, started_at, "raybet_explicit_map_time_unique")
    content_hash = "a" * 64
    facts = {
        "raybet_match_id": "series-one",
        "map_number": 1,
        "dota_match_id": 9001,
        "winner_side": "team_two",
        "duration_seconds": 1800,
        "identity_method": "raybet_explicit_map_time_unique",
        "result_source": "registered_opendota_match",
    }

    class _ResultConnection:
        def execute(self, query: str, parameters=()) -> _Result:
            if "FROM map_results AS result" in query:
                return _Result([])
            if "FROM settlement_result_evidence" in query:
                assert parameters == ("series-one", 1)
                return _Result(
                    [
                        {
                            "dota_match_id": 9001,
                            "winner_side": "team_two",
                            "evidence_ref": f"opendota:9001:sha256:{content_hash}",
                            "facts_json": json.dumps(facts),
                            "observed_at": observed_at.isoformat(),
                            "first_usable_at": observed_at.isoformat(),
                            "opendota_artifact_id": "opendota:artifact",
                            "opendota_observation_id": "observation",
                            "opendota_content_hash": content_hash,
                        }
                    ]
                )
            if "SELECT start_time, duration, radiant_win FROM matches" in query:
                return _Result([(int(started_at.timestamp()), 1800, False)])
            raise AssertionError(query)

    audit = _result_audit(
        _ResultConnection(),  # type: ignore[arg-type]
        "series-one",
        1,
        official,
    )

    assert audit["status"] == "accepted"
    assert audit["reason"] is None
    assert audit["result_source"] == "verified_official_result_evidence"
    assert audit["dota_match_id"] == 9001
    assert audit["winner_side"] == "team_two"
    assert audit["duration_seconds"] == 1800
    assert audit["strict_mapping_id"] is None


def test_roster_audit_requires_sourced_manual_lock() -> None:
    authority = sourced_manual_draft_authority(
        "operator",
        "https://example.test/evidence/series-one/map-1",
    )
    row = {
        "version": 2,
        "slot_count": 10,
        "locked_count": 10,
        "hero_count": 10,
        "team_count": 2,
        "radiant_positions": 5,
        "dire_positions": 5,
        "radiant_slots": 5,
        "dire_slots": 5,
        "created_by": authority,
        "actor_count": 1,
        "mapping_source": "manual_correction",
        "source_count": 1,
    }

    class _RosterConnection:
        def execute(self, query: str, parameters=()) -> _Result:
            assert "LEFT JOIN live_draft_mappings AS mapping" in query
            assert parameters == ("series-one", 1, "series-one", 1)
            return _Result([row])

    audit = _roster_audit(
        _RosterConnection(),  # type: ignore[arg-type]
        "series-one",
        1,
    )

    assert audit == {
        "status": "accepted",
        "reason": None,
        "mapping_version": 2,
        "source_actor": "operator",
        "source_url": "https://example.test/evidence/series-one/map-1",
        "source_kind": "manual_correction",
    }


def test_roster_audit_rejects_unsourced_legacy_manual_lock() -> None:
    row = {
        "version": 1,
        "slot_count": 10,
        "locked_count": 10,
        "hero_count": 10,
        "team_count": 2,
        "radiant_positions": 5,
        "dire_positions": 5,
        "radiant_slots": 5,
        "dire_slots": 5,
        "created_by": "operator",
        "actor_count": 1,
        "mapping_source": "manual",
        "source_count": 1,
    }

    class _RosterConnection:
        def execute(self, _query: str, _parameters=()) -> _Result:
            return _Result([row])

    audit = _roster_audit(
        _RosterConnection(),  # type: ignore[arg-type]
        "series-one",
        1,
    )

    assert audit["status"] == "incomplete"
    assert audit["reason"] == "manual_mapping_source_missing"
    assert audit["mapping_version"] == 1
    assert audit["source_actor"] is None
    assert audit["source_url"] is None


def test_unsettled_official_result_duration_is_not_confirmed_downstream(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        acceptance,
        "_result_audit",
        lambda *_args: {
            "status": "incomplete",
            "reason": "official_match_result_not_settled",
            "duration_seconds": 1800,
        },
    )
    monkeypatch.setattr(
        acceptance,
        "_roster_audit",
        lambda *_args: {
            "status": "accepted",
            "reason": None,
            "mapping_version": 1,
        },
    )
    monkeypatch.setattr(
        acceptance,
        "_rosh_audit",
        lambda *_args: {"status": "accepted", "reason": None},
    )
    monkeypatch.setattr(
        acceptance,
        "_manifest_audit",
        lambda *_args, **_kwargs: {"status": "accepted", "reason": None},
    )

    def odds_audit(*_args, duration_seconds: object, **_kwargs) -> dict[str, object]:
        captured["odds_duration_seconds"] = duration_seconds
        return {"status": "accepted", "reason": None}

    def checkpoint_audit(
        *_args,
        duration_seconds: object,
        **_kwargs,
    ) -> dict[str, object]:
        captured["checkpoint_duration_seconds"] = duration_seconds
        return {"status": "accepted", "reason": None}

    monkeypatch.setattr(acceptance, "_odds_audit", odds_audit)
    monkeypatch.setattr(acceptance, "_checkpoint_audit", checkpoint_audit)

    audit = _audit_map(
        object(),  # type: ignore[arg-type]
        match_id="series-one",
        map_number=1,
        official_link=None,
        evidence_root=tmp_path,
        verify_frame_bytes=False,
    )

    assert audit["status"] == "incomplete"
    assert audit["checks"]["result"]["reason"] == "official_match_result_not_settled"
    assert captured["odds_duration_seconds"] is None
    assert captured["checkpoint_duration_seconds"] is None


def test_progress_requires_three_newest_series_in_a_row(
    monkeypatch,
    tmp_path: Path,
) -> None:
    match_ids = ["newest", "second", "third", "older-failed"]
    statuses = {
        "newest": "accepted",
        "second": "accepted",
        "third": "accepted",
        "older-failed": "incomplete",
    }
    monkeypatch.setattr(
        acceptance,
        "_candidate_series_ids",
        lambda *_args, **_kwargs: match_ids,
    )
    monkeypatch.setattr(
        acceptance,
        "audit_series_acceptance",
        lambda _connection, match_id, **_kwargs: {
            "raybet_match_id": match_id,
            "status": statuses[match_id],
            "reasons": [] if statuses[match_id] == "accepted" else ["failed"],
        },
    )

    report = acceptance.audit_acceptance_progress(
        object(),  # type: ignore[arg-type]
        evidence_root=tmp_path,
    )

    assert report["goal_met"] is True
    assert report["consecutive_accepted_series"] == 3
    assert report["failure_reason_counts"] == {"failed": 1}
