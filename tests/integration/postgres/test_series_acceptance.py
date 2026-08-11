from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from contracts.live_observation import LiveObservation
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.series_acceptance import (
    _OfficialMapEvidence,
    _manifest_audit,
    _odds_audit,
    audit_series_acceptance,
)
from live_betting.storage import LiveBettingStore
from live_betting.vision import parse_observation
from live_betting.vision_frame_registry import publish_vision_frame_bytes
from scripts.watch_raybet_stream import _retain_observation_frame
from vision.observation_writer import ObservationWriter


NOW = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)


def _two_zero_payload() -> dict[str, object]:
    map_data = {
        "1": {"status": 2, "cmDate": "2026-08-11 11:00:00"},
        "2": {"status": 1, "cmDate": "2026-08-11 12:00:00"},
        "3": {"status": 0, "cmDate": ""},
    }
    return {
        "id": "acceptance-two-zero",
        "game_id": 151,
        "tournament_name": "Acceptance Cup",
        "tournament_short_name": "Acceptance Cup",
        "start_time": "2026-08-11 11:00:00",
        "round": "bo3",
        "status": 3,
        "team": [
            {
                "id": 11,
                "pos": 1,
                "team_name": "Team One",
                "score": {"manualControlData": {"data": map_data}},
            },
            {
                "id": 22,
                "pos": 2,
                "team_name": "Team Two",
                "score": {"manualControlData": {"data": map_data}},
            },
        ],
    }


def _insert_map_three_observation(
    store: LiveBettingStore,
    tmp_path,
    *,
    frame_bytes: bytes,
) -> LiveObservation:
    receipt = publish_vision_frame_bytes(tmp_path / "evidence", frame_bytes)
    observation = LiveObservation(
        raybet_match_id="acceptance-two-zero",
        map_number=3,
        captured_at_utc=NOW,
        game_clock_seconds=10,
        is_paused=False,
        clock_confidence=0.95,
        source_frame_ref=receipt.frame_ref,
        source_frame_sha256=receipt.content_sha256,
        source_frame_bytes=receipt.byte_length,
        source_frame_path=str(receipt.storage_path),
        screen_state="game",
    )
    store.insert_vision_observation(
        parse_observation(observation.model_dump(mode="json"))
    )
    return observation


def test_two_zero_series_rejects_production_vision_map_three(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(engine=postgres_engine, raw_archive_root=tmp_path / "raw")
    store.upsert_raybet_match(_two_zero_payload(), NOW)
    _insert_map_three_observation(
        store,
        tmp_path,
        frame_bytes=b"map-three-frame",
    )

    report = audit_series_acceptance(
        store.connection,
        "acceptance-two-zero",
        evidence_root=tmp_path / "evidence",
    )

    assert report["actual_map_numbers"] == [1, 2]
    assert report["extra_production_map_numbers"] == [3]
    assert report["market_only_map_numbers"] == []
    assert "non_played_map_has_production_evidence" in report["reasons"]
    assert report["status"] == "incomplete"
    store.close()


def test_two_zero_series_ignores_audited_invalidated_vision_map_three(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(engine=postgres_engine, raw_archive_root=tmp_path / "raw")
    store.upsert_raybet_match(_two_zero_payload(), NOW)
    observation = _insert_map_three_observation(
        store,
        tmp_path,
        frame_bytes=b"invalidated-map-three-frame",
    )
    with store.transaction():
        store.connection.execute(
            """INSERT INTO vision_observation_invalidations
               (raybet_match_id, captured_at, source_frame_ref,
                invalidated_at, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (
                observation.raybet_match_id,
                observation.captured_at_utc.isoformat(),
                observation.source_frame_ref,
                NOW.isoformat(),
                "exact_official_series_excludes_map",
            ),
        )

    report = audit_series_acceptance(
        store.connection,
        "acceptance-two-zero",
        evidence_root=tmp_path / "evidence",
    )

    assert report["actual_map_numbers"] == [1, 2]
    assert report["extra_production_map_numbers"] == []
    assert report["observed_map_numbers_by_table"]["vision_observations"] == []
    assert "non_played_map_has_production_evidence" not in report["reasons"]
    assert report["status"] == "incomplete"
    store.close()


def test_retained_map_sample_reaches_db_registry_and_manifest(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(engine=postgres_engine, raw_archive_root=tmp_path / "raw")
    evidence_root = tmp_path / "evidence"
    writer = ObservationWriter(
        tmp_path / "observations" / "retention-series.jsonl",
        sink=store.insert_vision_observation,
    )
    image = np.zeros((8, 8, 3), dtype=np.uint8)

    manifest_path = None
    for sequence in range(2):
        captured_at = NOW.replace(second=sequence)
        observation = LiveObservation(
            raybet_match_id="retention-series",
            map_number=1,
            captured_at_utc=captured_at,
            game_clock_seconds=10 + sequence,
            is_paused=False,
            clock_confidence=0.95,
            source_frame_ref=f"stream:retention:{sequence}",
            screen_state="game",
        )
        receipt, manifest_path = _retain_observation_frame(
            evidence_root,
            match_id="retention-series",
            image=image + sequence,
            observation=observation,
            triggers=(),
            captured_at=captured_at.timestamp(),
            screen_confidence=0.96,
        )
        assert observation.source_frame_ref == receipt.frame_ref
        assert manifest_path is not None
        writer.append(observation)

    assert manifest_path is not None
    assert manifest_path.is_file()
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 2
    audit = _manifest_audit(
        store.connection,
        "retention-series",
        1,
        evidence_root=evidence_root,
        verify_frame_bytes=True,
    )

    assert audit["status"] == "accepted"
    assert audit["database_sample_count"] == 2
    assert audit["manifest_sample_count"] == 2
    assert audit["registered_frame_error_count"] == 0
    assert audit["missing_manifest_sample_count"] == 0
    store.close()


def test_odds_audit_accepts_audited_pregame_and_one_sided_terminal_closure(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(engine=postgres_engine, raw_archive_root=tmp_path / "raw")
    match_id = "acceptance-odds"
    started_at = NOW

    def record(
        observation_key: str,
        observed_at: datetime,
        odds: list[dict[str, object]],
        *,
        audit_only: bool,
    ) -> None:
        result = {
            "id": match_id,
            "game_id": 151,
            "status": 3 if observation_key == "closing" else 2,
            "team": [
                {"team_id": 11, "pos": 1, "team_name": "Team One"},
                {"team_id": 22, "pos": 2, "team_name": "Team Two"},
            ],
            "odds": odds,
        }
        payload = {"result": result}
        snapshots = snapshots_from_payload(payload, received_at=observed_at)
        store.store_odds_observation(
            source="direct",
            observation_key=observation_key,
            source_event_id=None,
            raybet_match_id=match_id,
            observed_at=observed_at,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
            raw_payload=payload,
            audit_only=audit_only,
        )

    def winner(side: str, price: float, status: int) -> dict[str, object]:
        team_id = 11 if side == "team_one" else 22
        return {
            "id": f"{side}-{status}",
            "odds_group_id": "winner-map-1",
            "team_id": team_id,
            "match_stage": "r1",
            "group_short_name": "Winner",
            "tag": "win",
            "odds": price,
            "status": status,
        }

    record(
        "pregame",
        started_at - timedelta(seconds=30),
        [winner("team_one", 1.8, 1), winner("team_two", 2.0, 1)],
        audit_only=True,
    )
    record(
        "live",
        started_at + timedelta(minutes=5),
        [winner("team_one", 2.1, 1), winner("team_two", 1.7, 1)],
        audit_only=False,
    )
    record(
        "closing",
        started_at + timedelta(minutes=10),
        [winner("team_one", 13.34, 5)],
        audit_only=False,
    )

    audit = _odds_audit(
        store.connection,
        match_id,
        1,
        official_link=_OfficialMapEvidence(
            1,
            9001,
            started_at,
            "confirmed_map_result",
        ),
        duration_seconds=600,
    )

    assert audit["status"] == "accepted"
    assert audit["pregame_complete_observation_count"] == 1
    assert audit["pregame_processing_statuses"] == {"audit_only": 1}
    assert audit["live_complete_observation_count"] == 1
    assert audit["closing_complete_observation_count"] == 1
    assert audit["closing_quote_observation_key"] == "live"
    assert audit["closure_evidence_observation_count"] == 1
    store.close()
