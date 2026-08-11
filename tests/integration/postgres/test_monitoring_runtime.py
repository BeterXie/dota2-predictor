from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from contracts.live_observation import ComebackState, LiveObservation
from database.session import PostgresSession
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.map_decision_checkpoints import (
    _checkpoint_snapshot,
    _due_live_checkpoint_minutes,
    latest_map_checkpoints,
    record_due_checkpoints,
    record_pregame_checkpoint,
    settle_open_checkpoints,
)
from live_betting.health import record_health
from live_betting.live_match_state import DraftSlotInput, save_live_draft_mapping
from live_betting.storage import LiveBettingStore
from live_betting.vision_frame_registry import publish_vision_frame_bytes
from fetch.postgres_store import CoreMatchStore
from event_intelligence.ingest_adapters import PostgresIngestAdapter
from event_intelligence.raw_archive import RawArchive, canonical_json_bytes
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage
from web import queries
from web.app import app
from web.control import ControlService
from web.routers import monitor as monitor_router
from scripts.watch_raybet_stream import _write_sample_manifest
from web.monitoring import (
    build_monitor_snapshot,
    derive_health,
    monitor_history_page,
    monitor_match_detail,
    monitor_matches,
    winner_timeline,
    _postmatch_game,
    _stratz_enrichment,
)
from vision.observation_writer import ObservationWriter


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def test_empty_checkpoint_settlement_query_uses_postgres(postgres_engine) -> None:
    connection = PostgresSession(postgres_engine)
    try:
        assert settle_open_checkpoints(connection, settled_at=NOW) == {
            "settled": 0,
            "unchanged": 0,
        }
    finally:
        connection.close()


def test_checkpoint_settlement_stops_at_exact_official_map_end(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "official-end-cutoff-series"
    dota_match_id = 9_001_001
    official_start = NOW - timedelta(hours=1)
    duration_seconds = 30 * 60
    before_end = official_start + timedelta(seconds=duration_seconds - 1)
    after_end = official_start + timedelta(seconds=duration_seconds + 1)
    store.upsert_raybet_match(
        _raybet_match(match_id, status=3, scheduled_at=official_start),
        NOW,
    )
    store.connection.commit()
    connection = store.connection
    fixture_tables = ("settlement_reconciliations", "map_results")
    connection.begin()
    try:
        for table in fixture_tables:
            connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        with connection.transaction():
            connection.execute(
                "INSERT INTO matches (match_id, start_time) VALUES (?, ?)",
                (dota_match_id, int(official_start.timestamp())),
            )
            connection.execute(
                """INSERT INTO settlement_reconciliations
                   (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                    raybet_winner_side, opendota_winner_side,
                    raybet_evidence_ref, opendota_evidence_ref, evidence_ref,
                    status, reason, first_observed_at, updated_at)
                   VALUES (?, 1, NULL, ?, 'team_one', 'team_one',
                           'raybet:test', 'opendota:test',
                           'settlement-reconciliation:official-end-cutoff-series:map:1',
                           'confirmed', 'sources_consistent', ?, ?)""",
                (match_id, dota_match_id, NOW.isoformat(), NOW.isoformat()),
            )
            connection.execute(
                """INSERT INTO map_results
                   (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                    winner_side, duration_seconds, evidence_ref,
                    reconciliation_ref, settled_at)
                   VALUES (?, 1, NULL, ?, 'team_one', ?,
                           'settlement-reconciliation:official-end-cutoff-series:map:1',
                           'settlement-reconciliation:official-end-cutoff-series:map:1',
                           ?)""",
                (match_id, dota_match_id, duration_seconds, NOW.isoformat()),
            )
            for phase, minute, decided_at, odds_age, vision_age, gap in (
                ("pregame", 0, before_end, 150.0, None, None),
                ("live", 5, after_end, 15.0, 5.0, 15.0),
            ):
                connection.execute(
                    """INSERT INTO map_decision_checkpoints
                       (raybet_match_id, map_number, mapping_version,
                        phase, checkpoint_minute, strategy_version, decision,
                        assumed_stake_units, odds_max_age_seconds,
                        vision_max_age_seconds, odds_vision_gap_max_seconds,
                        vision_trusted, vision_replay, input_versions_json,
                        feature_availability_json, reason, decided_at, created_at)
                       VALUES (?, 1, NULL, ?, ?, 'map-decision-shadow-v1', 'skip',
                               1.0, ?, ?, ?, FALSE, FALSE, '{}', '{}',
                               'test_skip', ?, ?)""",
                    (
                        match_id,
                        phase,
                        minute,
                        odds_age,
                        vision_age,
                        gap,
                        decided_at.isoformat(),
                        decided_at.isoformat(),
                    ),
                )
    finally:
        for table in reversed(fixture_tables):
            connection.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
    connection.commit()

    result = settle_open_checkpoints(connection, settled_at=NOW)
    settled = connection.execute(
        """SELECT checkpoint.phase, checkpoint.checkpoint_minute
             FROM map_decision_checkpoint_settlements AS settlement
             JOIN map_decision_checkpoints AS checkpoint
               ON checkpoint.checkpoint_id=settlement.checkpoint_id
            WHERE settlement.raybet_match_id=?""",
        (match_id,),
    ).fetchall()

    assert result == {"settled": 1, "unchanged": 0}
    assert [tuple(row) for row in settled] == [("pregame", 0)]
    store.close()


def _raybet_match(match_id: str, *, status: int, scheduled_at: datetime) -> dict:
    return {
        "id": match_id,
        "game_id": 151,
        "tournament_name": "PostgreSQL Integration Cup",
        "start_time": scheduled_at.isoformat(),
        "round": "bo3",
        "status": status,
        "team": [
            {"id": 11, "pos": 1, "team_name": "Radiant Five"},
            {"id": 22, "pos": 2, "team_name": "Dire Five"},
        ],
    }


def _live_second_map_payload(
    match_id: str,
    *,
    map_started_at: datetime,
) -> dict:
    payload = _raybet_match(
        match_id,
        status=2,
        scheduled_at=map_started_at - timedelta(hours=1),
    )
    raybet_timezone = timezone(timedelta(hours=8))
    map_times = {
        "1": {
            "status": 2,
            "cmDate": (map_started_at - timedelta(hours=1))
            .astimezone(raybet_timezone)
            .strftime("%Y-%m-%d %H:%M:%S"),
        },
        "2": {
            "status": 1,
            "cmDate": map_started_at.astimezone(raybet_timezone).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        },
        "3": {"status": 0, "cmDate": ""},
    }
    for index, team in enumerate(payload["team"]):
        team["team_id"] = int(team["id"])
        team["score"] = {
            "r1": 1 if index == 0 else 0,
            "manualControlData": {"currentIndex": 2, "data": map_times},
        }
    return payload


def test_monitor_list_detail_and_history_use_postgres(postgres_engine, tmp_path) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.upsert_raybet_match(
        _raybet_match("1001", status=2, scheduled_at=NOW - timedelta(hours=1)),
        NOW,
    )
    store.upsert_raybet_match(
        _raybet_match(
            "history-match",
            status=3,
            scheduled_at=NOW - timedelta(days=2),
        ),
        NOW - timedelta(days=1),
    )
    store.connection.commit()

    matches = monitor_matches(store.connection, now=NOW)
    assert any(item["raybet_match_id"] == "1001" for item in matches)

    detail = monitor_match_detail(store.connection, "1001", now=NOW)
    assert detail is not None
    assert detail["raybet_match_id"] == "1001"
    assert detail["lifecycle"] in {"live", "degraded"}
    assert detail["watch_link"] == {
        "kind": "stream_resolver",
        "availability": "available",
        "url": "/api/monitor/matches/1001/live-stream",
        "reason": "fresh_stream_resolution_available",
    }

    history = monitor_history_page(store.connection, now=NOW)
    assert [item["raybet_match_id"] for item in history["items"]] == [
        "history-match"
    ]
    store.close()


def test_observation_writer_persists_live_jsonl_and_draft_anchor(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.upsert_raybet_match(
        _raybet_match("vision-live", status=2, scheduled_at=NOW - timedelta(minutes=5)),
        NOW,
    )
    store.connection.commit()
    receipt = publish_vision_frame_bytes(tmp_path / "evidence", b"encoded-frame")
    observation = LiveObservation(
        raybet_match_id="vision-live",
        map_number=1,
        captured_at_utc=NOW,
        game_clock_seconds=90,
        is_paused=False,
        radiant_hero_ids=[1, 2, 3, 4, 5],
        dire_hero_ids=[6, 7, 8, 9, 10],
        radiant_team_side="team_one",
        clock_confidence=0.96,
        draft_confidence=0.97,
        source_frame_ref=receipt.frame_ref,
        source_frame_sha256=receipt.content_sha256,
        source_frame_bytes=receipt.byte_length,
        source_frame_path=str(receipt.storage_path),
        screen_state="game",
    )
    path = tmp_path / "observations" / "vision-live.jsonl"

    ObservationWriter(
        path,
        sink=store.insert_vision_observation,
    ).append(observation)

    stored = store.connection.execute(
        """SELECT confirmed, source_frame_ref FROM vision_observations
             WHERE raybet_match_id='vision-live'"""
    ).fetchone()
    anchor = store.connection.execute(
        """SELECT status, radiant_hero_ids, dire_hero_ids
             FROM vision_draft_anchors
            WHERE raybet_match_id='vision-live' AND map_number=1"""
    ).fetchone()
    assert stored is not None
    assert tuple(stored) == (1, receipt.frame_ref)
    assert anchor is not None
    assert tuple(anchor) == ("anchored", "[1,2,3,4,5]", "[6,7,8,9,10]")
    assert path.read_text(encoding="utf-8").count("\n") == 1
    store.close()


def test_draft_authority_is_independent_per_map_and_not_live_authority(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "independent-draft-series"
    store.upsert_raybet_match(
        _raybet_match(match_id, status=2, scheduled_at=NOW - timedelta(minutes=5)),
        NOW,
    )
    store.connection.commit()
    writer = ObservationWriter(
        tmp_path / "observations" / f"{match_id}.jsonl",
        sink=store.insert_vision_observation,
    )

    for map_number, hero_offset in ((1, 0), (2, 10)):
        receipt = publish_vision_frame_bytes(
            tmp_path / "evidence",
            f"draft-map-{map_number}".encode("ascii"),
        )
        writer.append(
            LiveObservation(
                raybet_match_id=match_id,
                map_number=map_number,
                captured_at_utc=NOW + timedelta(seconds=map_number),
                radiant_hero_ids=[hero_offset + value for value in range(1, 6)],
                dire_hero_ids=[hero_offset + value for value in range(6, 11)],
                radiant_team_side="team_one",
                draft_confidence=0.97,
                source_frame_ref=receipt.frame_ref,
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
                source_frame_path=str(receipt.storage_path),
                screen_state="draft",
            )
        )

    for offset, screen_state, radiant in (
        (3, "replay", [31, 32, 33, 34, 35]),
        (4, "unknown", [31, 32, 33, 34, 35]),
        (5, "draft", [31, 32, 33, 34]),
    ):
        receipt = publish_vision_frame_bytes(
            tmp_path / "evidence",
            f"rejected-draft-{offset}".encode("ascii"),
        )
        writer.append(
            LiveObservation(
                raybet_match_id=match_id,
                map_number=3,
                captured_at_utc=NOW + timedelta(seconds=offset),
                radiant_hero_ids=radiant,
                dire_hero_ids=[36, 37, 38, 39, 40],
                radiant_team_side="team_one",
                draft_confidence=0.97,
                source_frame_ref=receipt.frame_ref,
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
                source_frame_path=str(receipt.storage_path),
                screen_state=screen_state,
            )
        )

    anchors = store.connection.execute(
        """SELECT map_number, radiant_hero_ids, dire_hero_ids
             FROM vision_draft_anchors
            WHERE raybet_match_id=? ORDER BY map_number""",
        (match_id,),
    ).fetchall()
    confirmed = store.connection.execute(
        """SELECT map_number, confirmed
             FROM vision_observations
            WHERE raybet_match_id=?
            ORDER BY map_number, captured_at""",
        (match_id,),
    ).fetchall()
    live_authority_count = store.connection.execute(
        """SELECT COUNT(*) FROM trusted_vision_observation_authority
            WHERE raybet_match_id=?""",
        (match_id,),
    ).scalar_one()

    assert [tuple(row) for row in anchors] == [
        (1, "[1,2,3,4,5]", "[6,7,8,9,10]"),
        (2, "[11,12,13,14,15]", "[16,17,18,19,20]"),
    ]
    assert [tuple(row) for row in confirmed] == [
        (1, 1),
        (2, 1),
        (3, 0),
        (3, 0),
        (3, 0),
    ]
    assert live_authority_count == 0
    store.close()


def test_identical_frame_content_retains_each_map_sample_event(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "sample-retention-series"
    store.upsert_raybet_match(
        _raybet_match(match_id, status=2, scheduled_at=NOW - timedelta(minutes=5)),
        NOW,
    )
    store.connection.commit()
    evidence_root = tmp_path / "evidence"
    receipt = publish_vision_frame_bytes(evidence_root, b"identical-encoded-frame")
    writer = ObservationWriter(
        tmp_path / "observations" / f"{match_id}.jsonl",
        sink=store.insert_vision_observation,
    )
    manifest_path = None

    for offset in (0, 1):
        observation = LiveObservation(
            raybet_match_id=match_id,
            map_number=1,
            captured_at_utc=NOW + timedelta(seconds=offset),
            game_clock_seconds=90 + offset,
            is_paused=False,
            clock_confidence=0.96,
            source_frame_ref=receipt.frame_ref,
            source_frame_sha256=receipt.content_sha256,
            source_frame_bytes=receipt.byte_length,
            source_frame_path=str(receipt.storage_path),
            screen_state="game",
        )
        manifest_path = _write_sample_manifest(
            evidence_root,
            observation=observation,
            receipt=receipt,
            lifecycle_events=(),
        )
        writer.append(observation)

    rows = store.connection.execute(
        """SELECT captured_at, source_frame_ref
             FROM vision_observations
            WHERE raybet_match_id=? AND map_number=1
            ORDER BY captured_at""",
        (match_id,),
    ).fetchall()

    assert len(rows) == 2
    assert {str(row["source_frame_ref"]) for row in rows} == {receipt.frame_ref}
    assert manifest_path is not None
    manifest_rows = manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(manifest_rows) == 2
    assert [
        json.loads(line)["observation_identity"]["captured_at"]
        for line in manifest_rows
    ] == [NOW.isoformat(), (NOW + timedelta(seconds=1)).isoformat()]
    store.close()


def test_same_map_number_is_isolated_by_raybet_series(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    series = (
        ("series-a", 90, list(range(1, 6)), list(range(6, 11))),
        ("series-b", 180, list(range(11, 16)), list(range(16, 21))),
    )
    for match_id, game_clock_seconds, radiant_heroes, dire_heroes in series:
        store.upsert_raybet_match(
            _raybet_match(match_id, status=2, scheduled_at=NOW - timedelta(minutes=5)),
            NOW,
        )
        receipt = publish_vision_frame_bytes(
            tmp_path / "evidence",
            f"frame-{match_id}".encode("ascii"),
        )
        ObservationWriter(
            tmp_path / "observations" / f"{match_id}.jsonl",
            sink=store.insert_vision_observation,
        ).append(
            LiveObservation(
                raybet_match_id=match_id,
                map_number=1,
                captured_at_utc=NOW,
                game_clock_seconds=game_clock_seconds,
                is_paused=False,
                radiant_hero_ids=radiant_heroes,
                dire_hero_ids=dire_heroes,
                radiant_team_side="team_one",
                clock_confidence=0.96,
                draft_confidence=0.97,
                source_frame_ref=receipt.frame_ref,
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
                source_frame_path=str(receipt.storage_path),
                screen_state="game",
            )
        )

    details = {
        match_id: monitor_match_detail(store.connection, match_id, now=NOW)
        for match_id, *_ in series
    }

    for match_id, game_clock_seconds, radiant_heroes, dire_heroes in series:
        detail = details[match_id]
        assert detail is not None
        assert detail["raybet_match_id"] == match_id
        assert len(detail["games"]) == 1
        game = detail["games"][0]
        assert game["game_id"] == f"{match_id}:map_1"
        assert game["map_id"] == f"{match_id}:map_1"
        assert game["map_number"] == 1
        assert game["latest_vision"]["game_clock_seconds"] == game_clock_seconds
        assert game["latest_vision"]["radiant_hero_ids"] == radiant_heroes
        assert game["latest_vision"]["dire_hero_ids"] == dire_heroes
    assert details["series-a"]["games"][0]["latest_vision"]["source_frame_ref"] != (
        details["series-b"]["games"][0]["latest_vision"]["source_frame_ref"]
    )
    store.close()


def test_each_map_projects_only_its_own_latest_registered_hud(
    postgres_engine,
    tmp_path,
    monkeypatch,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "independent-map-hud-series"
    store.upsert_raybet_match(
        _raybet_match(match_id, status=2, scheduled_at=NOW - timedelta(hours=2)),
        NOW,
    )
    observation_root = tmp_path / "observations"
    monkeypatch.setenv("VISION_OBSERVATION_DIR", str(observation_root))
    writer = ObservationWriter(
        observation_root / f"{match_id}.jsonl",
        sink=store.insert_vision_observation,
    )

    def append_registered(
        *,
        map_number: int,
        captured_at: datetime,
        game_clock_seconds: int,
        radiant_kills: int,
    ) -> None:
        receipt = publish_vision_frame_bytes(
            tmp_path / "evidence",
            f"map-{map_number}-{game_clock_seconds}".encode("ascii"),
        )
        writer.append(
            LiveObservation(
                raybet_match_id=match_id,
                map_number=map_number,
                captured_at_utc=captured_at,
                game_clock_seconds=game_clock_seconds,
                is_paused=False,
                radiant_team_side="team_one",
                clock_confidence=0.96,
                source_frame_ref=receipt.frame_ref,
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
                source_frame_path=str(receipt.storage_path),
                screen_state="game",
                comeback_state=ComebackState(
                    status="available",
                    source="vision_hud",
                    confidence=0.96,
                    radiant_kills=radiant_kills,
                    dire_kills=5,
                    radiant_net_worth=50_000 + game_clock_seconds,
                    dire_net_worth=49_000 + game_clock_seconds,
                    unavailable_reason=None,
                ),
            )
        )

    append_registered(
        map_number=1,
        captured_at=NOW - timedelta(hours=1),
        game_clock_seconds=1800,
        radiant_kills=31,
    )
    append_registered(
        map_number=2,
        captured_at=NOW - timedelta(minutes=2),
        game_clock_seconds=20,
        radiant_kills=1,
    )
    append_registered(
        map_number=2,
        captured_at=NOW - timedelta(minutes=1),
        game_clock_seconds=360,
        radiant_kills=12,
    )
    for sequence in range(40):
        writer.append(
            LiveObservation(
                raybet_match_id=match_id,
                map_number=2,
                captured_at_utc=NOW + timedelta(seconds=sequence),
                game_clock_seconds=361 + sequence,
                is_paused=False,
                clock_confidence=0.96,
                source_frame_ref=f"stream:unregistered:{sequence}",
                screen_state="game",
                comeback_state=ComebackState(
                    status="available",
                    source="vision_hud",
                    confidence=0.96,
                    radiant_kills=99,
                    dire_kills=99,
                    radiant_net_worth=99_999,
                    dire_net_worth=99_999,
                    unavailable_reason=None,
                ),
            )
        )

    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert detail is not None
    games = {game["map_number"]: game for game in detail["games"]}
    assert set(games) == {1, 2}
    assert games[1]["latest_hud_observation"]["radiant_kills"] == 31
    assert games[2]["latest_hud_observation"]["radiant_kills"] == 12
    assert games[1]["latest_hud_observation"]["source_frame_ref"] != (
        games[2]["latest_hud_observation"]["source_frame_ref"]
    )
    assert games[2]["latest_hud_observation"]["frame_url"] is not None
    store.close()


def test_later_map_vision_requires_registered_start_boundary(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "map-boundary-series"
    store.upsert_raybet_match(
        _raybet_match(match_id, status=2, scheduled_at=NOW - timedelta(hours=1)),
        NOW,
    )
    writer = ObservationWriter(
        tmp_path / "observations" / f"{match_id}.jsonl",
        sink=store.insert_vision_observation,
    )
    registered = (
        (NOW - timedelta(minutes=3), 2400, b"previous-map-frame"),
        (NOW - timedelta(minutes=2), 20, b"map-start-frame"),
        (NOW - timedelta(minutes=1), 360, b"map-live-frame"),
    )
    for captured_at, game_clock_seconds, frame_bytes in registered:
        receipt = publish_vision_frame_bytes(tmp_path / "evidence", frame_bytes)
        writer.append(
            LiveObservation(
                raybet_match_id=match_id,
                map_number=2,
                captured_at_utc=captured_at,
                game_clock_seconds=game_clock_seconds,
                is_paused=False,
                radiant_team_side="team_one",
                clock_confidence=0.96,
                source_frame_ref=receipt.frame_ref,
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
                source_frame_path=str(receipt.storage_path),
                screen_state="game",
            )
        )
    writer.append(
        LiveObservation(
            raybet_match_id=match_id,
            map_number=2,
            captured_at_utc=NOW,
            game_clock_seconds=420,
            is_paused=False,
            radiant_team_side="team_one",
            clock_confidence=0.96,
            source_frame_ref="stream:unregistered:4",
            screen_state="game",
        )
    )

    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert detail is not None
    assert len(detail["games"]) == 1
    assert detail["games"][0]["game_id"] == f"{match_id}:map_2"
    vision = detail["games"][0]["vision"]
    assert [point["game_clock_seconds"] for point in vision] == [20, 360]
    assert all(
        point["source_frame_ref"].startswith("vision-frame:sha256:")
        for point in vision
    )
    store.close()


def test_later_map_checkpoints_require_trusted_start_boundary(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "checkpoint-boundary-series"
    store.upsert_raybet_match(
        _raybet_match(match_id, status=2, scheduled_at=NOW - timedelta(hours=1)),
        NOW,
    )
    writer = ObservationWriter(
        tmp_path / "observations" / f"{match_id}.jsonl",
        sink=store.insert_vision_observation,
    )
    mapping = {
        "raybet_match_id": match_id,
        "map_number": 2,
        "version": 1,
    }

    def append_trusted(captured_at: datetime, game_clock_seconds: int) -> None:
        receipt = publish_vision_frame_bytes(
            tmp_path / "evidence",
            f"checkpoint-{game_clock_seconds}".encode("ascii"),
        )
        writer.append(
            LiveObservation(
                raybet_match_id=match_id,
                map_number=2,
                captured_at_utc=captured_at,
                game_clock_seconds=game_clock_seconds,
                is_paused=False,
                radiant_hero_ids=[1, 2, 3, 4, 5],
                dire_hero_ids=[6, 7, 8, 9, 10],
                radiant_team_side="team_one",
                clock_confidence=0.96,
                draft_confidence=0.97,
                source_frame_ref=receipt.frame_ref,
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
                source_frame_path=str(receipt.storage_path),
                screen_state="game",
                comeback_state=ComebackState(
                    status="available",
                    source="vision_hud",
                    confidence=0.96,
                    radiant_kills=8,
                    dire_kills=5,
                    radiant_net_worth=51_000,
                    dire_net_worth=50_000,
                    unavailable_reason=None,
                ),
            )
        )

    append_trusted(NOW - timedelta(minutes=4), 2400)
    assert _due_live_checkpoint_minutes(store.connection, mapping, now=NOW) == []

    append_trusted(NOW - timedelta(minutes=2), 120)
    append_trusted(NOW - timedelta(minutes=1), 300)

    assert _due_live_checkpoint_minutes(store.connection, mapping, now=NOW) == [5]
    snapshot = _checkpoint_snapshot(store.connection, match_id, 2, 5)
    assert snapshot is not None
    assert snapshot["game_time_seconds"] == 300
    receipt = publish_vision_frame_bytes(
        tmp_path / "evidence",
        b"newer-map-three-frame",
    )
    writer.append(
        LiveObservation(
            raybet_match_id=match_id,
            map_number=3,
            captured_at_utc=NOW,
            game_clock_seconds=60,
            is_paused=False,
            radiant_hero_ids=[11, 12, 13, 14, 15],
            dire_hero_ids=[16, 17, 18, 19, 20],
            radiant_team_side="team_one",
            clock_confidence=0.96,
            draft_confidence=0.97,
            source_frame_ref=receipt.frame_ref,
            source_frame_sha256=receipt.content_sha256,
            source_frame_bytes=receipt.byte_length,
            source_frame_path=str(receipt.storage_path),
            screen_state="game",
        )
    )
    assert _due_live_checkpoint_minutes(store.connection, mapping, now=NOW) == []
    store.close()


def test_live_provider_map_rejects_newer_vision_identity(
    postgres_engine,
    tmp_path,
    monkeypatch,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "provider-map-series"
    payload = _raybet_match(
        match_id,
        status=2,
        scheduled_at=NOW - timedelta(hours=1),
    )
    for team in payload["team"]:
        team["score"] = {"manualControlData": {"currentIndex": 2}}
    store.upsert_raybet_match(payload, NOW)
    receipt = publish_vision_frame_bytes(
        tmp_path / "evidence",
        b"wrong-newer-map-frame",
    )
    ObservationWriter(
        tmp_path / "observations" / f"{match_id}.jsonl",
        sink=store.insert_vision_observation,
    ).append(
        LiveObservation(
            raybet_match_id=match_id,
            map_number=3,
            captured_at_utc=NOW,
            game_clock_seconds=60,
            is_paused=False,
            radiant_team_side="team_one",
            clock_confidence=0.96,
            source_frame_ref=receipt.frame_ref,
            source_frame_sha256=receipt.content_sha256,
            source_frame_bytes=receipt.byte_length,
            source_frame_path=str(receipt.storage_path),
            screen_state="game",
        )
    )
    monkeypatch.setattr("web.monitoring.infer_current_map_number", lambda *_args: 2)

    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert detail is not None
    assert detail["current_map_number"] == 2
    assert detail["latest_vision"] is None
    assert [game["map_number"] for game in detail["games"]] == [2]
    assert detail["games"][0]["play_evidence"] == ["provider_live_map"]
    store.close()


def test_no_vision_checkpoints_are_isolated_to_current_started_map(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "920001"
    store.upsert_raybet_match(
        _live_second_map_payload(
            match_id,
            map_started_at=NOW - timedelta(minutes=10, seconds=1),
        ),
        NOW,
    )
    slots = [
        DraftSlotInput(
            team_id=11 if hero_id <= 5 else 22,
            side="radiant" if hero_id <= 5 else "dire",
            position=hero_id if hero_id <= 5 else hero_id - 5,
            hero_id=hero_id,
            player_id=1000 + hero_id,
        )
        for hero_id in range(1, 11)
    ]
    save_live_draft_mapping(
        store.connection,
        raybet_match_id=match_id,
        map_number=1,
        slots=slots,
        is_locked=True,
        actor="integration-test",
        evidence_source_url="https://example.test/evidence/920001/map-1",
        created_at=NOW - timedelta(hours=1),
    )
    mapping = save_live_draft_mapping(
        store.connection,
        raybet_match_id=match_id,
        map_number=2,
        slots=slots,
        is_locked=True,
        actor="integration-test",
        evidence_source_url="https://example.test/evidence/920001/map-2",
        created_at=NOW,
    )
    receipt = publish_vision_frame_bytes(
        tmp_path / "evidence",
        b"completed-map-one-frame",
    )
    ObservationWriter(
        tmp_path / "observations" / f"{match_id}.jsonl",
        sink=store.insert_vision_observation,
    ).append(
        LiveObservation(
            raybet_match_id=match_id,
            map_number=1,
            captured_at_utc=NOW - timedelta(minutes=1),
            game_clock_seconds=600,
            is_paused=False,
            radiant_team_side="team_one",
            clock_confidence=0.96,
            source_frame_ref=receipt.frame_ref,
            source_frame_sha256=receipt.content_sha256,
            source_frame_bytes=receipt.byte_length,
            source_frame_path=str(receipt.storage_path),
            screen_state="game",
            comeback_state=ComebackState(
                status="available",
                source="vision_hud",
                confidence=0.96,
                radiant_kills=14,
                dire_kills=9,
                radiant_net_worth=58_000,
                dire_net_worth=54_000,
                unavailable_reason=None,
            ),
        )
    )

    assert (
        _due_live_checkpoint_minutes(
            store.connection,
            {**mapping, "map_number": 1},
            now=NOW,
        )
        == []
    )
    assert (
        _due_live_checkpoint_minutes(
            store.connection,
            {**mapping, "map_number": 3},
            now=NOW,
        )
        == []
    )

    result = record_due_checkpoints(store.connection, now=NOW)
    checkpoints = latest_map_checkpoints(store.connection, match_id, 2)

    assert result == {"created": 2, "unchanged": 0}
    assert [(row["phase"], row["checkpoint_minute"]) for row in checkpoints] == [
        ("live", 5),
        ("live", 10),
    ]
    assert {row["decision"] for row in checkpoints} == {"skip"}
    assert {row["reason"] for row in checkpoints} == {
        "trusted_vision_checkpoint_missing"
    }
    assert all(row["vision_snapshot_id"] is None for row in checkpoints)
    assert all(row["vision_game_time_seconds"] is None for row in checkpoints)
    assert (
        store.connection.execute(
            """SELECT COUNT(*) FROM map_decision_checkpoints
            WHERE raybet_match_id=? AND map_number IN (1, 3)""",
            (match_id,),
        ).scalar_one()
        == 0
    )
    store.close()


def test_pregame_fallback_uses_only_latest_locked_map(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "pregame-provider-unavailable"
    store.upsert_raybet_match(
        _raybet_match(
            match_id,
            status=2,
            scheduled_at=NOW - timedelta(hours=2),
        ),
        NOW,
    )
    slots = [
        DraftSlotInput(
            team_id=11 if hero_id <= 5 else 22,
            side="radiant" if hero_id <= 5 else "dire",
            position=hero_id if hero_id <= 5 else hero_id - 5,
            hero_id=hero_id,
            player_id=2000 + hero_id,
        )
        for hero_id in range(1, 11)
    ]
    mappings = {}
    for map_number, created_at in (
        (1, NOW - timedelta(hours=1)),
        (2, NOW - timedelta(minutes=5)),
    ):
        mappings[map_number] = save_live_draft_mapping(
            store.connection,
            raybet_match_id=match_id,
            map_number=map_number,
            slots=slots,
            is_locked=True,
            actor="integration-test",
            evidence_source_url=(
                f"https://example.test/evidence/{match_id}/map-{map_number}"
            ),
            created_at=created_at,
        )

    with pytest.raises(ValueError, match="pregame_target_is_not_latest_locked_map"):
        record_pregame_checkpoint(
            store.connection,
            mapping=mappings[1],
            prediction=None,
            decided_at=NOW,
        )

    result = record_due_checkpoints(store.connection, now=NOW)

    assert result == {"created": 1, "unchanged": 0}
    assert latest_map_checkpoints(store.connection, match_id, 1) == []
    map_two = latest_map_checkpoints(store.connection, match_id, 2)
    assert [(row["phase"], row["checkpoint_minute"]) for row in map_two] == [
        ("pregame", 0)
    ]
    assert map_two[0]["decision"] == "skip"
    assert map_two[0]["reason"] == "pregame_prediction_unavailable"
    store.close()


def test_trusted_vision_clock_remains_authoritative_during_midmap_restart(
    postgres_engine,
    tmp_path,
    monkeypatch,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "920002"
    payload = _raybet_match(
        match_id,
        status=2,
        scheduled_at=NOW - timedelta(hours=2),
    )
    raybet_timezone = timezone(timedelta(hours=8))
    map_times = {
        "1": {
            "status": 2,
            "cmDate": (NOW - timedelta(hours=2))
            .astimezone(raybet_timezone)
            .strftime("%Y-%m-%d %H:%M:%S"),
        },
        "2": {
            "status": 1,
            "cmDate": (NOW - timedelta(minutes=30))
            .astimezone(raybet_timezone)
            .strftime("%Y-%m-%d %H:%M:%S"),
        },
        "3": {"status": 0, "cmDate": ""},
    }
    for index, team in enumerate(payload["team"]):
        team["team_id"] = int(team["id"])
        team["score"] = {
            "r1": 1 if index == 0 else 0,
            "manualControlData": {
                "currentIndex": 2,
                "data": map_times,
            },
        }
    store.upsert_raybet_match(payload, NOW)
    receipt = publish_vision_frame_bytes(
        tmp_path / "evidence",
        b"verified-midmap-frame",
    )
    ObservationWriter(
        tmp_path / "observations" / f"{match_id}.jsonl",
        sink=store.insert_vision_observation,
    ).append(
        LiveObservation(
            raybet_match_id=match_id,
            map_number=2,
            captured_at_utc=NOW,
            game_clock_seconds=300,
            is_paused=False,
            radiant_hero_ids=[1, 2, 3, 4, 5],
            dire_hero_ids=[6, 7, 8, 9, 10],
            radiant_team_side="team_one",
            clock_confidence=0.96,
            draft_confidence=0.97,
            source_frame_ref=receipt.frame_ref,
            source_frame_sha256=receipt.content_sha256,
            source_frame_bytes=receipt.byte_length,
            source_frame_path=str(receipt.storage_path),
            screen_state="game",
            comeback_state=ComebackState(
                status="available",
                source="vision_hud",
                confidence=0.96,
                radiant_kills=8,
                dire_kills=5,
                radiant_net_worth=51_000,
                dire_net_worth=50_000,
                unavailable_reason=None,
            ),
        )
    )
    monkeypatch.setattr("web.monitoring.infer_current_map_number", lambda *_args: 2)
    mapping = {
        "raybet_match_id": match_id,
        "map_number": 2,
        "version": 1,
    }

    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert detail is not None
    assert detail["current_map_number"] == 2
    assert [game["map_number"] for game in detail["games"]] == [2]
    assert detail["games"][0]["latest_vision"]["game_clock_seconds"] == 300
    assert _due_live_checkpoint_minutes(store.connection, mapping, now=NOW) == [5]
    snapshot = _checkpoint_snapshot(store.connection, match_id, 2, 5)
    assert snapshot is not None
    assert snapshot["game_time_seconds"] == 300
    store.close()


def test_near_start_prematch_snapshot_stays_visible_without_live_promotion(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "near-start-prematch"
    observed_at = NOW - timedelta(minutes=4)
    payload = _raybet_match(
        match_id,
        status=1,
        scheduled_at=NOW + timedelta(minutes=5),
    )
    payload["team"] = [
        {"team_id": 11, "pos": 1, "team_name": "Radiant Five"},
        {"team_id": 22, "pos": 2, "team_name": "Dire Five"},
    ]
    payload["odds"] = [
        {
            "id": "prematch-winner-one",
            "odds_group_id": "prematch-winner-map-1",
            "team_id": 11,
            "match_stage": "r1",
            "group_short_name": "Winner",
            "tag": "win",
            "odds": 1.65,
            "status": 1,
        },
        {
            "id": "prematch-winner-two",
            "odds_group_id": "prematch-winner-map-1",
            "team_id": 22,
            "match_stage": "r1",
            "group_short_name": "Winner",
            "tag": "win",
            "odds": 2.19,
            "status": 1,
        },
    ]
    store.upsert_raybet_match(payload, observed_at)
    response_payload = {"result": payload}
    snapshots = snapshots_from_payload(
        response_payload,
        received_at=observed_at,
    )
    store.store_odds_observation(
        source="direct",
        observation_key="near-start-prematch-response",
        source_event_id=None,
        raybet_match_id=match_id,
        observed_at=observed_at,
        normalized_state_hash=normalized_state_hash(snapshots),
        snapshots=snapshots,
        raw_payload=response_payload,
        audit_only=True,
    )

    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert detail is not None
    assert detail["lifecycle"] == "degraded"
    assert detail["winner"] is None
    assert detail["prematch_winner"] == {
        "observed_at": observed_at.isoformat(),
        "period": "map_1",
        "complete": True,
        "prices": {"team_one": 1.65, "team_two": 2.19},
        "probabilities": {
            "team_one": 0.5703125,
            "team_two": 0.4296875,
        },
    }
    assert detail["readiness"]["odds"] == {
        "status": "delayed",
        "observed_at": observed_at.isoformat(),
        "age_seconds": 240.0,
    }
    assert detail["watch_link"] == {
        "kind": "none",
        "availability": "unavailable",
        "url": None,
        "reason": "no_safe_entry",
    }
    assert detail["games"] == []
    assert len(detail["market_evidence"]) == 1
    market = detail["market_evidence"][0]
    assert market["market_id"] == "near-start-prematch:map_1"
    assert market["status"] == "market_only"
    assert market["reason"] == "no_play_evidence"
    assert market["markets"] == []
    assert market["winner_timeline"] == []
    assert market["odds_coverage"]["source"] == "raybet_direct"
    assert market["odds_coverage"]["prematch"] == {
        "status": "available",
        "complete_snapshot_count": 1,
        "observation_count": 1,
        "first_observed_at": observed_at.isoformat(),
        "last_observed_at": observed_at.isoformat(),
        "gap_count": 0,
        "longest_gap_seconds": None,
        "periods": [
            {
                "period": "map_1",
                "complete_snapshot_count": 1,
                "observation_count": 1,
                "first_observed_at": observed_at.isoformat(),
                "last_observed_at": observed_at.isoformat(),
                "gap_count": 0,
                "longest_gap_seconds": None,
            }
        ],
    }
    assert market["odds_coverage"]["live"]["status"] == "missing"
    assert market["odds_coverage"]["closing"]["status"] == "pending"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE raybet_match_id=?",
        (match_id,),
    ).scalar_one() == 0
    assert store.connection.execute(
        """SELECT processing_status FROM odds_transport_observations
            WHERE raybet_match_id=?""",
        (match_id,),
    ).scalar_one() == "audit_only"
    store.close()


def test_ended_bo3_with_two_observed_maps_keeps_r3_as_market_only(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "two-map-series"
    observed_at = NOW - timedelta(minutes=1)
    payload = _raybet_match(
        match_id,
        status=3,
        scheduled_at=NOW - timedelta(hours=2),
    )
    payload["team"] = [
        {"team_id": 11, "pos": 1, "team_name": "Radiant Five"},
        {"team_id": 22, "pos": 2, "team_name": "Dire Five"},
    ]
    payload["odds"] = [
        {
            "id": f"r3-winner-{side}",
            "odds_group_id": "winner-map-3",
            "team_id": team_id,
            "match_stage": "r3",
            "group_short_name": "Winner",
            "tag": "win",
            "odds": odds,
            "status": 5,
        }
        for side, team_id, odds in (("one", 11, 1.7), ("two", 22, 2.1))
    ]
    store.upsert_raybet_match(payload, observed_at)
    response_payload = {"result": payload}
    snapshots = snapshots_from_payload(response_payload, received_at=observed_at)
    store.store_odds_observation(
        source="direct",
        observation_key="two-map-series-r3-market",
        source_event_id=None,
        raybet_match_id=match_id,
        observed_at=observed_at,
        normalized_state_hash=normalized_state_hash(snapshots),
        snapshots=snapshots,
        raw_payload=response_payload,
    )

    observation_path = tmp_path / "observations" / f"{match_id}.jsonl"
    writer = ObservationWriter(observation_path, sink=store.insert_vision_observation)
    observations = (
        (1, 600, NOW - timedelta(minutes=4)),
        (2, 120, NOW - timedelta(minutes=3, seconds=30)),
        (2, 1200, NOW - timedelta(minutes=3)),
    )
    for map_number, game_clock_seconds, captured_at in observations:
        receipt = publish_vision_frame_bytes(
            tmp_path / "evidence",
            f"encoded-frame-map-{map_number}-{game_clock_seconds}".encode("ascii"),
        )
        writer.append(
            LiveObservation(
                raybet_match_id=match_id,
                map_number=map_number,
                captured_at_utc=captured_at,
                game_clock_seconds=game_clock_seconds,
                is_paused=False,
                radiant_hero_ids=[1, 2, 3, 4, 5],
                dire_hero_ids=[6, 7, 8, 9, 10],
                radiant_team_side="team_one",
                clock_confidence=0.96,
                draft_confidence=0.97,
                source_frame_ref=receipt.frame_ref,
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
                source_frame_path=str(receipt.storage_path),
                screen_state="game",
            )
        )

    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert detail is not None
    assert [game["map_number"] for game in detail["games"]] == [1, 2]
    assert all(game["link_status"] == "unlinked" for game in detail["games"])
    assert [market["map_number"] for market in detail["market_evidence"]] == [3]
    assert detail["market_evidence"][0]["status"] == "market_only"
    assert detail["market_evidence"][0]["reason"] == "no_play_evidence"
    store.close()


def test_monitor_api_uses_postgres_session(postgres_engine, tmp_path, monkeypatch) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.upsert_raybet_match(
        _raybet_match("1002", status=2, scheduled_at=NOW - timedelta(hours=1)),
        NOW,
    )
    store.connection.commit()
    store.close()

    monkeypatch.setattr(queries, "get_db", lambda: PostgresSession(postgres_engine))
    stream_url = "https://play.ehome.gg/live/1002.m3u8?expires=1&sig=test"
    monkeypatch.setattr(
        monitor_router,
        "_fresh_live_stream_url",
        lambda match_id: stream_url if match_id == "1002" else None,
    )

    with TestClient(app) as client:
        bootstrap = client.get("/api/monitor/bootstrap")
        detail = client.get("/api/monitor/matches/1002")
        stream = client.get(
            "/api/monitor/matches/1002/live-stream",
            follow_redirects=False,
        )

    assert bootstrap.status_code == 200
    assert any(
        item["raybet_match_id"] == "1002"
        for item in bootstrap.json()["matches"]
    )
    assert detail.status_code == 200
    assert detail.json()["raybet_match_id"] == "1002"
    assert stream.status_code == 307
    assert stream.headers["location"] == stream_url
    assert stream.headers["cache-control"] == "no-store"
    assert stream.headers["referrer-policy"] == "no-referrer"


def test_monitor_health_uses_current_worker_heartbeats(postgres_engine) -> None:
    connection = PostgresSession(postgres_engine)
    stale = NOW - timedelta(days=1)
    for component in (
        "companion",
        "database",
        "draft_publisher",
        "draft_publisher_worker",
        "raybet",
        "strict_ingest",
        "vision",
    ):
        record_health(
            connection,
            component,
            "healthy",
            heartbeat_at=stale,
            success_at=stale,
        )
    for component in ("raybet_worker", "strict_ingest_worker", "vision_worker"):
        record_health(
            connection,
            component,
            "healthy",
            heartbeat_at=NOW,
            success_at=NOW,
        )
    record_health(
        connection,
        "raybet_full_odds_worker",
        "healthy",
        heartbeat_at=NOW - timedelta(seconds=170),
        success_at=NOW - timedelta(seconds=170),
        details={"stale_after_seconds": 240.0},
    )

    health = derive_health(connection, now=NOW + timedelta(seconds=10))
    snapshot = build_monitor_snapshot(connection, now=NOW + timedelta(seconds=10))
    by_component = {item["component"]: item for item in health}

    assert not {
        "companion",
        "database",
        "draft_publisher",
        "draft_publisher_worker",
        "raybet",
        "strict_ingest",
        "vision",
    } & by_component.keys()
    assert by_component["raybet_worker"]["status"] == "healthy"
    assert by_component["raybet_full_odds_worker"]["status"] == "healthy"
    assert by_component["raybet_full_odds_worker"]["freshness"] == "fresh"
    assert by_component["strict_ingest_worker"]["status"] == "healthy"
    assert snapshot["capabilities"] == {
        "direct_market_collection": {"required": True, "status": "healthy"},
        "opendota_event_ingest": {"required": True, "status": "healthy"},
    }
    assert snapshot["summary"]["unhealthy_components"] == 0
    connection.close()


def test_control_uses_supervisor_heartbeat_authority(
    postgres_engine,
    tmp_path,
) -> None:
    connection = PostgresSession(postgres_engine)
    heartbeat = datetime.now(timezone.utc)
    record_health(
        connection,
        "raybet_worker",
        "degraded",
        heartbeat_at=heartbeat,
        error_at=heartbeat,
        error="upstream_degraded",
    )

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError(
            "fresh supervisor heartbeat must block a duplicate process"
        )

    service = ControlService(
        project_dir=tmp_path,
        popen_factory=unexpected_spawn,
    )

    statuses = {
        item["component"]: item for item in service.statuses(connection)
    }
    start = service.execute(
        connection,
        component="raybet_collector",
        action="start",
        request_id="supervisor-authority-start",
        client_host="127.0.0.1",
    )

    assert statuses["raybet_collector"]["status"] == "running"
    assert statuses["raybet_collector"]["pid"] is None
    assert statuses["raybet_collector"]["started_at"] is not None
    assert statuses["raybet_collector"]["detail"] == "由 Supervisor 托管"
    assert statuses["raybet_collector"]["control_allowed"] is False
    assert start["ok"] is False
    assert start["status"] == "running"
    assert start["detail"] == "由 Supervisor 托管"
    service.close()
    connection.close()


def test_ended_match_timeline_collapses_unchanged_final_transport(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    match_id = "ended-timeline-match"
    first = NOW - timedelta(minutes=10)
    second = NOW - timedelta(minutes=5)
    payload = _raybet_match(
        match_id,
        status=3,
        scheduled_at=NOW - timedelta(hours=2),
    )
    payload["team"] = [
        {"team_id": 11, "pos": 1, "team_name": "Radiant Five"},
        {"team_id": 22, "pos": 2, "team_name": "Dire Five"},
    ]
    payload["odds"] = [
        {
            "id": "winner-one",
            "odds_group_id": "winner-map-1",
            "team_id": 11,
            "match_stage": "r1",
            "group_short_name": "Winner",
            "tag": "win",
            "odds": 1.8,
            "status": 5,
        },
        {
            "id": "winner-two",
            "odds_group_id": "winner-map-1",
            "team_id": 22,
            "match_stage": "r1",
            "group_short_name": "Winner",
            "tag": "win",
            "odds": 2.0,
            "status": 5,
        },
    ]
    store.upsert_raybet_match(payload, first)
    response_payload = {"result": payload}
    for index, observed_at in enumerate((first, second), start=1):
        snapshots = snapshots_from_payload(
            response_payload,
            received_at=observed_at,
        )
        store.store_odds_observation(
            source="direct",
            observation_key=f"ended-response-{index}",
            source_event_id=None,
            raybet_match_id=match_id,
            observed_at=observed_at,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
            raw_payload=response_payload,
        )

    timeline = winner_timeline(store.connection, match_id)
    summary = next(
        item
        for item in monitor_matches(store.connection, now=NOW)
        if item["raybet_match_id"] == match_id
    )
    detail = monitor_match_detail(store.connection, match_id, now=NOW)

    assert [point["observed_at"] for point in timeline] == [first.isoformat()]
    assert timeline[0]["prices"] == {"team_one": 1.8, "team_two": 2.0}
    assert timeline[0]["probabilities"] == {
        "team_one": 0.52631579,
        "team_two": 0.47368421,
    }
    assert sum(timeline[0]["probabilities"].values()) == 1.0
    assert summary["winner"]["observed_at"] == first.isoformat()
    assert detail is not None
    assert detail["winner"]["observed_at"] == first.isoformat()
    assert detail["games"] == []
    assert detail["market_evidence"][0]["market_id"] == (
        "ended-timeline-match:map_1"
    )
    assert detail["market_evidence"][0]["status"] == "market_only"
    assert detail["market_evidence"][0]["odds_coverage"]["closing"] == {
        "status": "unconfirmed",
        "observed_at": second.isoformat(),
        "prices": {"team_one": 1.8, "team_two": 2.0},
        "probabilities": {
            "team_one": 0.52631579,
            "team_two": 0.47368421,
        },
    }
    store.close()


def test_stale_prematch_leaves_live_view_after_four_hours(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    stale_at = NOW - timedelta(hours=5)
    store.upsert_raybet_match(
        _raybet_match(
            "stale-prematch",
            status=1,
            scheduled_at=stale_at,
        ),
        stale_at,
    )
    store.connection.commit()

    snapshot = build_monitor_snapshot(store.connection, now=NOW)
    match = next(
        item
        for item in snapshot["matches"]
        if item["raybet_match_id"] == "stale-prematch"
    )

    assert match["lifecycle"] == "degraded"
    assert match["history_eligible"] is True
    history_ids = {
        item["raybet_match_id"]
        for item in monitor_history_page(store.connection, now=NOW)["items"]
    }
    assert "stale-prematch" in history_ids
    store.close()


def test_postmatch_game_projects_one_exact_opendota_match(
    postgres_engine,
    tmp_path,
) -> None:
    core = CoreMatchStore(engine=postgres_engine)
    core.insert_heroes([
        {
            "id": hero_id,
            "name": f"npc_dota_hero_test_{hero_id}",
            "localized_name": f"Hero {hero_id}",
        }
        for hero_id in range(1, 11)
    ])
    players = [
        {
            "account_id": 1000 + index,
            "name": f"Player {index + 1}",
            "personaname": f"Steam Player {index + 1}",
            "player_slot": index if index < 5 else 128 + index - 5,
            "hero_id": index + 1,
            "kills": index,
            "deaths": 1,
            "assists": 10 - index,
            "gold_per_min": 500 + index,
            "xp_per_min": 600 + index,
            "net_worth": 15000 + index,
            "last_hits": 200 + index,
            "denies": index,
            "hero_damage": 20000 + index,
            "hero_healing": 0,
            "tower_damage": 3000 + index,
            "level": 25,
            "lane_role": (index % 5) + 1,
            "item_0": 50 + index,
        }
        for index in range(10)
    ]
    opendota_detail = {
        "match_id": 9001,
        "radiant_team_id": 101,
        "dire_team_id": 202,
        "radiant_team": {"team_id": 101, "name": "Radiant Five"},
        "dire_team": {"team_id": 202, "name": "Dire Five"},
        "radiant_win": True,
        "duration": 2400,
        "start_time": 1786096800,
        "leagueid": 77,
        "series_id": 700,
        "series_type": 1,
        "league": {"leagueid": 77, "name": "Exact Cup"},
        "radiant_score": 31,
        "dire_score": 18,
        "players": players,
        "picks_bans": [
            {
                "hero_id": hero_id,
                "is_pick": True,
                "team": 0 if hero_id <= 5 else 1,
                "order": hero_id - 1,
            }
            for hero_id in range(1, 11)
        ],
        "radiant_gold_adv": [0, 200, 500],
        "radiant_xp_adv": [0, 100, 300],
        "objectives": [{
            "time": 700,
            "type": "CHAT_MESSAGE_ROSHAN_KILL",
            "unit": "npc_dota_roshan",
            "key": "Roshan",
            "player_slot": 0,
        }],
        "teamfights": [{
            "start": 600,
            "end": 630,
            "last_death": 625,
            "deaths": 1,
            "players": [{
                "player_slot": 0,
                "deaths": 0,
                "buybacks": 0,
                "damage": 1000,
                "healing": 0,
                "gold_delta": 500,
                "xp_delta": 600,
                "kills": 1,
            }],
        }],
    }
    core.insert_match({
        **opendota_detail,
        "match_id": 8999,
        "start_time": 1786093200,
        "series_id": 699,
        "players": [
            {
                **player,
                "kills": 2 + index,
                "deaths": 2,
                "assists": 7,
                "gold_per_min": 450 + index,
                "xp_per_min": 520 + index,
                "net_worth": 12000 + index,
                "last_hits": 160 + index,
                "hero_damage": 15000 + index,
                "tower_damage": 1800 + index,
            }
            for index, player in enumerate(players)
        ],
    })
    core.insert_match(opendota_detail)
    intelligence = IntelligenceStorage(engine=postgres_engine)
    ingest = PostgresIngestAdapter(
        intelligence,
        EventRegistry(intelligence),
        core,
    )
    archive = RawArchive(tmp_path / "raw", observation_sink=ingest.record_raw_artifact)
    archive.archive_json(
        source="opendota",
        endpoint="/api/matches/9001",
        request_identity="/api/matches/9001",
        payload_bytes=canonical_json_bytes(opendota_detail),
        observed_at=NOW,
        match_id=9001,
        status_code=200,
        first_usable_at=NOW,
    )
    stratz_payload = {
        "data": {
            "match": {
                "id": 9001,
                "players": [
                    {
                        "steamAccountId": 1000 + index,
                        "heroId": index + 1,
                        "position": f"POSITION_{(index % 5) + 1}",
                    }
                    for index in range(10)
                ],
            }
        }
    }
    archive.archive_json(
        source="stratz",
        endpoint="/graphql/match-detail-enrichment",
        request_identity="https://api.stratz.com/graphql?match_id=9001",
        payload_bytes=canonical_json_bytes(stratz_payload),
        observed_at=NOW,
        match_id=9001,
        status_code=200,
        first_usable_at=NOW,
    )
    connection = PostgresSession(postgres_engine)
    match = connection.execute(
        """SELECT 1 AS map_number, match.match_id AS dota_match_id,
                  match.radiant_team_id, match.dire_team_id,
                  match.radiant_win, match.duration, match.start_time,
                  match.leagueid, match.radiant_score, match.dire_score,
                  match.fetched_at, radiant.name AS radiant_team_name,
                  dire.name AS dire_team_name, league.name AS league_name
             FROM matches AS match
             LEFT JOIN teams AS radiant ON radiant.team_id=match.radiant_team_id
             LEFT JOIN teams AS dire ON dire.team_id=match.dire_team_id
             LEFT JOIN leagues AS league ON league.leagueid=match.leagueid
            WHERE match.match_id=?""",
        (9001,),
    ).fetchone()
    assert match is not None

    game = _postmatch_game(connection, match)

    assert game["official_match_id"] == "9001"
    assert game["result"]["radiant_score"] == 31
    assert game["availability"] == {
        "result": "available",
        "players": "available",
        "player_names": "available",
        "historical_averages": "available",
        "positions": "available",
        "draft": "available",
        "gold_advantage": "available",
        "xp_advantage": "available",
        "objectives": "available",
        "teamfights": "available",
    }
    assert len(game["players"]) == 10
    assert game["players"][0]["player_name"] == "Player 1"
    assert game["players"][0]["player_name_source"] == "opendota_name"
    assert game["players"][0]["hero_name"] == "Hero 1"
    assert game["players"][0]["position"] == 1
    assert game["players"][0]["position_source"] == "stratz"
    assert game["players"][0]["historical_average"] == {
        "sample_size": 1,
        "source": "opendota_collected_history",
        "cutoff": "before_match_start",
        "sample_start_date": "2026-08-07",
        "sample_end_date": "2026-08-07",
        "kills": 2.0,
        "deaths": 2.0,
        "assists": 7.0,
        "gold_per_min": 450.0,
        "xp_per_min": 520.0,
        "net_worth": 12000.0,
        "last_hits": 160.0,
        "hero_damage": 15000.0,
        "tower_damage": 1800.0,
    }
    assert game["enrichment"]["status"] == "available"
    assert game["advantages"]["gold"][-1] == {"minute": 2, "value": 500}
    assert game["objectives"][0]["type"] == "CHAT_MESSAGE_ROSHAN_KILL"
    assert game["teamfights"][0]["damage"] == 1000

    connection.close()
    intelligence.close()


def test_postmatch_reports_global_stratz_source_blocker(postgres_engine) -> None:
    connection = PostgresSession(postgres_engine)
    failed_at = NOW + timedelta(minutes=1)
    try:
        record_health(
            connection,
            "postmatch_worker",
            "degraded",
            heartbeat_at=failed_at,
            error_at=failed_at,
            error="stratz_enrichment_failed",
            details={
                "source": "worker",
                "stratz_enrichment": {
                    "configured": True,
                    "failed": 2,
                    "failure_reasons": ["stratz_http_403"],
                },
            },
        )

        positions, enrichment = _stratz_enrichment(connection, 9999)
    finally:
        connection.close()

    assert positions == {}
    assert enrichment == {
        "provider": "stratz",
        "status": "blocked",
        "reason": "stratz_http_403",
        "observed_at": failed_at.isoformat(),
    }


def test_unlisted_match_is_hidden_from_monitor_lists(
    postgres_engine,
    tmp_path,
) -> None:
    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "raw",
    )
    store.upsert_raybet_match(
        _raybet_match(
            "unlisted-match",
            status=1,
            scheduled_at=NOW + timedelta(hours=1),
        ),
        NOW,
    )
    store.connection.execute(
        "UPDATE raybet_matches SET status='unlisted' WHERE raybet_match_id=?",
        ("unlisted-match",),
    )
    store.connection.commit()

    snapshot_ids = {
        item["raybet_match_id"]
        for item in build_monitor_snapshot(store.connection, now=NOW)["matches"]
    }
    history_ids = {
        item["raybet_match_id"]
        for item in monitor_history_page(store.connection, now=NOW)["items"]
    }

    assert "unlisted-match" not in snapshot_ids
    assert "unlisted-match" not in history_ids
    store.close()
