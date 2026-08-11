from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from contracts.live_observation import LiveObservation
from event_intelligence.ingest_adapters import PostgresIngestAdapter
from event_intelligence.raw_archive import RawArchive
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage
from fetch.postgres_store import CoreMatchStore
from live_betting.official_map_identity import resolve_exact_official_map_links
from live_betting.postmatch_monitor import sync_exact_postmatch_candidate
from live_betting.storage import LiveBettingStore
from live_betting.vision import parse_observation
from live_betting.vision_frame_registry import publish_vision_frame_bytes


UTC = timezone.utc
RAYBET_TIMEZONE = timezone(timedelta(hours=8))


def _raybet_payload(match_id: str, starts: tuple[datetime, datetime]) -> dict:
    map_data = {
        str(index): {
            "status": 2 if index == 1 else 1,
            "cmDate": started.astimezone(RAYBET_TIMEZONE).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
        for index, started in enumerate(starts, 1)
    }
    map_data["3"] = {"status": 0, "cmDate": ""}
    return {
        "id": match_id,
        "game_id": 151,
        "tournament_name": "EPL大师赛",
        "tournament_short_name": "EPL Masters",
        "start_time": starts[0].astimezone(RAYBET_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "round": "bo3",
        "status": 3,
        "team": [
            {
                "id": 501,
                "team_id": 501,
                "pos": 1,
                "team_name": "Rune Eaters",
                "score": {"manualControlData": {"data": map_data}},
            },
            {
                "id": 502,
                "team_id": 502,
                "pos": 2,
                "team_name": "Na`Vi",
                "score": {"manualControlData": {"data": map_data}},
            },
        ],
    }


def _detail(match_id: int, started_at: datetime, *, radiant_win: bool) -> dict:
    return {
        "match_id": match_id,
        "start_time": int(started_at.timestamp()),
        "series_id": 9_700_001,
        "leagueid": 19_944,
        "duration": 2400,
        "radiant_win": radiant_win,
        "radiant_team_id": 36,
        "dire_team_id": 9_895_247,
        "radiant_score": 30 if radiant_win else 20,
        "dire_score": 20 if radiant_win else 30,
        "radiant_team": {"team_id": 36, "name": "Natus Vincere"},
        "dire_team": {"team_id": 9_895_247, "name": "Rune Eaters"},
        "league": {
            "leagueid": 19_944,
            "name": "EPL Masters 2026",
            "tier": "professional",
        },
        "players": [],
    }


def test_unverified_event_can_sync_postmatch_identity_without_formal_authority(
    postgres_engine,
    tmp_path,
) -> None:
    match_id = "postmatch-only-epl-series"
    starts = (
        datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 13, 20, tzinfo=UTC),
    )
    official = {
        9_100_001: _detail(9_100_001, starts[0] + timedelta(seconds=30), radiant_win=True),
        9_100_002: _detail(9_100_002, starts[1] + timedelta(seconds=30), radiant_win=False),
    }

    class Client:
        async def get_league_matches(self, league_id: int) -> list[dict]:
            assert league_id == 19_944
            return [
                {
                    "match_id": dota_match_id,
                    "start_time": detail["start_time"],
                    "series_id": detail["series_id"],
                    "leagueid": detail["leagueid"],
                }
                for dota_match_id, detail in official.items()
            ]

        async def get_match(self, dota_match_id: int) -> dict:
            return official[dota_match_id]

    store = LiveBettingStore(
        engine=postgres_engine,
        raw_archive_root=tmp_path / "live-raw",
    )
    store.upsert_raybet_match(_raybet_payload(match_id, starts), starts[1])
    receipt = publish_vision_frame_bytes(
        tmp_path / "evidence",
        b"old-watcher-map-three-frame",
    )
    map_three_observation = LiveObservation(
        raybet_match_id=match_id,
        map_number=3,
        captured_at_utc=starts[1] + timedelta(minutes=5),
        game_clock_seconds=120,
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
    store.insert_vision_observation(
        parse_observation(map_three_observation.model_dump(mode="json"))
    )
    assert store.connection.execute(
        """SELECT confirmed FROM vision_observations
            WHERE raybet_match_id=? AND map_number=3""",
        (match_id,),
    ).scalar_one() == 1
    with store.transaction():
        store.connection.execute(
            """INSERT INTO leagues (leagueid, name, tier)
               VALUES (19944, 'EPL Masters 2026', 'professional')"""
        )
    intelligence = IntelligenceStorage(engine=postgres_engine)
    registry = EventRegistry(intelligence)
    registry.discover_candidate(
        source="opendota_league_catalog",
        provider_event_id="19944",
        canonical_name="EPL Masters 2026",
        evidence_urls=("https://api.opendota.com/api/leagues/19944/matches",),
        evidence={"decision": "pending_manual_audit"},
        discovered_at=starts[1],
    )
    core = CoreMatchStore(engine=postgres_engine)
    ingest = PostgresIngestAdapter(intelligence, registry, core)
    archive = RawArchive(
        tmp_path / "raw-sources",
        observation_sink=ingest.record_raw_artifact,
    )

    result = asyncio.run(
        sync_exact_postmatch_candidate(
            store,
            Client(),  # type: ignore[arg-type]
            archive,
            core,
            match_id,
        )
    )
    resolution = resolve_exact_official_map_links(store.connection, match_id)

    assert result["status"] == "confirmed"
    assert result["details_synced"] == 2
    assert result["official_result_evidence"] == {
        "status": "confirmed",
        "reason": "verified_registered_opendota_result",
        "inserted": 2,
        "unchanged": 0,
        "map_numbers": [1, 2],
    }
    assert result["vision_observation_invalidations"] == 1
    assert result["vision_observation_deconfirmed"] == 1
    assert result["vision_invalidated_map_numbers"] == [3]
    assert [link.map_number for link in resolution.links] == [1, 2]
    assert [link.dota_match_id for link in resolution.links] == [9_100_001, 9_100_002]
    assert store.connection.execute(
        """SELECT reason FROM vision_observation_invalidations
            WHERE raybet_match_id=?""",
        (match_id,),
    ).scalar_one() == "exact_official_series_excludes_map"
    assert store.connection.execute(
        """SELECT confirmed FROM vision_observations
            WHERE raybet_match_id=? AND map_number=3""",
        (match_id,),
    ).scalar_one() == 0
    assert resolution.links[0].evidence()["team_name_evidence"][1] == {
        "raybet_name": "Na`Vi",
        "official_name": "Natus Vincere",
        "method": "sourced_alias",
        "source_url": "https://liquipedia.net/dota2/Natus_Vincere",
    }
    assert store.connection.execute(
        "SELECT COUNT(*) FROM strict_live_map_mappings WHERE raybet_match_id=?",
        (match_id,),
    ).scalar_one() == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM map_results WHERE raybet_match_id=?",
        (match_id,),
    ).scalar_one() == 0
    assert store.connection.execute(
        """SELECT COUNT(*) FROM settlement_result_evidence
            WHERE raybet_match_id=? AND source='opendota' AND status='confirmed'""",
        (match_id,),
    ).scalar_one() == 2
    assert store.connection.execute(
        """SELECT COUNT(*) FROM map_decision_checkpoint_settlements
            WHERE raybet_match_id=?""",
        (match_id,),
    ).scalar_one() == 0
    store.close()
