from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from event_intelligence.ingest import completed_match_processing_result
from event_intelligence.ingest_adapters import (
    PostgresIngestAdapter,
    RegistryIngestAdapter,
)
from event_intelligence.raw_archive import RawArchive, canonical_json_bytes
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage
from fetch.postgres_store import CoreMatchStore


NOW = datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)


def _completed_payload(match_id: int, hero_start: int = 1) -> dict:
    slots = (0, 1, 2, 3, 4, 128, 129, 130, 131, 132)
    heroes = tuple(range(hero_start, hero_start + 10))
    players = [
        {
            "account_id": 10_000 + slot,
            "player_slot": slot,
            "hero_id": hero_id,
            "kills": 0,
            "deaths": 1,
            "assists": 12,
            "gold_per_min": 500,
            "xp_per_min": 600,
            "net_worth": 15_000,
            "last_hits": 200,
            "denies": 5,
            "hero_damage": 20_000,
            "hero_healing": 0,
            "tower_damage": 2_000,
            "damage_taken": {"npc_dota_hero_axe": 900},
            "stuns": 12.5,
            "camps_stacked": 0,
            "rune_pickups": 3,
            "obs_placed": 0,
            "sen_placed": 2,
            "observer_kills": 1,
            "sentry_kills": 0,
            "lane_role": 1,
            "is_roaming": False,
            "gold_t": list(range(11)),
            "lh_t": [value * 2 for value in range(11)],
            "xp_t": [value * 3 for value in range(11)],
            "kills_log": [],
            "obs_log": [],
            "sen_log": [],
            "buyback_log": [],
        }
        for slot, hero_id in zip(slots, heroes)
    ]
    return {
        "match_id": match_id,
        "radiant_team_id": 101,
        "dire_team_id": 202,
        "radiant_win": True,
        "duration": 1_800,
        "game_mode": 2,
        "lobby_type": 1,
        "start_time": int(NOW.timestamp()),
        "first_blood_time": 120,
        "leagueid": 19_543,
        "series_id": 77,
        "series_type": 2,
        "patch": 60,
        "region": 3,
        "radiant_score": 31,
        "dire_score": 18,
        "version": 21,
        "radiant_team": {"team_id": 101, "name": "Radiant", "tag": "RAD"},
        "dire_team": {"team_id": 202, "name": "Dire", "tag": "DIRE"},
        "league": {
            "leagueid": 19_543,
            "name": "PGL Wallachia S8",
            "tier": "premium",
        },
        "players": players,
        "picks_bans": [
            {
                "is_pick": True,
                "hero_id": hero_id,
                "team": 0 if index < 5 else 1,
                "order": index,
            }
            for index, hero_id in enumerate(heroes)
        ],
        "radiant_gold_adv": [minute * 100 for minute in range(30)],
        "radiant_xp_adv": [minute * 80 for minute in range(30)],
        "objectives": [
            {
                "time": 601,
                "type": "CHAT_MESSAGE_ROSHAN_KILL",
                "team": 2,
                "key": "npc_dota_roshan",
            }
        ],
        "teamfights": [],
        "chat": [],
    }


def _discover_and_archive(store, registry, tmp_path, payload):
    event = RegistryIngestAdapter(registry).approved_events(
        event_id="pgl-wallachia-s8-2026"
    )[0]
    assert store.record_discovered_match(
        event,
        {
            "match_id": payload["match_id"],
            "leagueid": payload["leagueid"],
            "start_time": payload["start_time"],
            "series_id": payload["series_id"],
        },
        NOW,
        "opendota_league",
    )
    return RawArchive(
        tmp_path / "raw",
        observation_sink=store.record_raw_artifact,
    ).archive_json(
        source="opendota",
        endpoint=f"/api/matches/{payload['match_id']}",
        request_identity=f"https://api.opendota.com/api/matches/{payload['match_id']}",
        payload_bytes=canonical_json_bytes(payload),
        observed_at=NOW,
        match_id=payload["match_id"],
        status_code=200,
        first_usable_at=NOW,
    )


def _record_success(store, payload, receipt):
    processing = completed_match_processing_result(payload, payload["match_id"])
    attempt = store.begin_ingest_attempt(payload["match_id"], NOW)
    return store.record_ingest_success(
        match_id=payload["match_id"],
        attempted_at=NOW,
        attempt_count=int(attempt),
        attempt_generation=attempt.generation,
        content_sha256=receipt.content_sha256,
        first_usable_at=NOW,
        payload=payload,
        facts=processing.facts,
        artifact_unchanged=False,
        detail_complete=processing.detail_complete,
        retryable=processing.retryable,
        missing_reasons=processing.missing_reasons,
        next_retry_at=None,
    )


def test_completed_match_ingest_is_atomic_on_postgres(postgres_engine, tmp_path) -> None:
    storage = IntelligenceStorage(engine=postgres_engine)
    storage.init_schema()
    registry = EventRegistry(storage)
    store = PostgresIngestAdapter(storage, registry)
    payload = _completed_payload(8_001)
    receipt = _discover_and_archive(store, registry, tmp_path, payload)

    assert _record_success(store, payload, receipt) == "normalized"

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM matches WHERE match_id = 8001")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM match_players WHERE match_id = 8001")
        ).scalar_one() == 10
        assert connection.execute(
            text("SELECT COUNT(*) FROM player_map_facts WHERE match_id = 8001")
        ).scalar_one() == 10
    storage.close()


def test_normalization_failure_preserves_raw_evidence(postgres_engine, tmp_path) -> None:
    class FailingCoreMatchStore(CoreMatchStore):
        def insert_match_with_connection(self, connection, match) -> None:
            super().insert_match_with_connection(connection, match)
            raise RuntimeError("injected child write failure")

    storage = IntelligenceStorage(engine=postgres_engine)
    storage.init_schema()
    registry = EventRegistry(storage)
    store = PostgresIngestAdapter(
        storage,
        registry,
        FailingCoreMatchStore(engine=postgres_engine),
    )
    payload = _completed_payload(8_002, hero_start=20)
    receipt = _discover_and_archive(store, registry, tmp_path, payload)

    with pytest.raises(RuntimeError, match="injected child write failure"):
        _record_success(store, payload, receipt)

    with postgres_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM matches")).scalar_one() == 0
        assert connection.execute(
            text("SELECT COUNT(*) FROM player_map_facts")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT COUNT(*) FROM raw_source_artifacts")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT ingest_state FROM match_ingest_status WHERE match_id = 8002")
        ).scalar_one() == "detail_pending"
    storage.close()
