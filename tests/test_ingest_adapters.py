from __future__ import annotations

import copy
import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from event_intelligence.benchmarks import BENCHMARK_VERSION
from event_intelligence.facts import extract_completed_match_facts
from event_intelligence.incremental import (
    ROLE_VERSION,
    SCORE_VERSION,
    StrictDerivedPipeline,
    current_derived_scopes,
)
from event_intelligence.ingest import (
    MATCH_PROCESSOR_VERSION,
    IngestReport,
    StrictEventIngestor,
    completed_match_processing_result,
)
from event_intelligence.ingest_adapters import (
    RegistryIngestAdapter,
    SQLiteIngestAdapter,
)
from event_intelligence.raw_archive import RawArchive, canonical_json_bytes
from event_intelligence.raw_registry import relocate_raw_source_artifacts
from event_intelligence.opendota import OpenDotaAdapter
from event_intelligence.registry import EventRegistry
from event_intelligence.scheduler import ScheduleRun, SchedulerRetryState
from event_intelligence.storage import IntelligenceStorage
from event_intelligence.team_profiles import PROFILE_VERSION
from event_intelligence.team_states import LABEL_VERSION
from fetch.db import Database
from scripts.run_strict_event_ingest import (
    Runtime,
    _record_runtime_health,
    build_default_runtime,
    build_parser,
    resolve_data_paths,
    run,
)


NOW = datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)


def completed_payload(match_id: int = 8_001, hero_start: int = 1) -> dict:
    slots = (0, 1, 2, 3, 4, 128, 129, 130, 131, 132)
    heroes = tuple(range(hero_start, hero_start + 10))
    players = []
    for slot, hero_id in zip(slots, heroes):
        players.append({
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
        })
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
        "league": {"leagueid": 19_543, "name": "PGL Wallachia S8", "tier": "premium"},
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
        "objectives": [{
            "time": 601,
            "type": "CHAT_MESSAGE_ROSHAN_KILL",
            "team": 2,
            "key": "npc_dota_roshan",
        }],
        "teamfights": [],
        "chat": [],
    }


class IngestAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "strict.db"
        self.storage = IntelligenceStorage(self.path)
        self.storage.init_schema()
        self.legacy = Database(connection=self.storage.connection)
        self.legacy.init_db()
        self.registry = EventRegistry(self.storage)
        self.registry_port = RegistryIngestAdapter(self.registry)
        self.store = SQLiteIngestAdapter(self.storage, self.registry, self.legacy)
        self.archive = RawArchive(
            Path(self.directory.name) / "raw",
            observation_sink=self.store.record_raw_artifact,
        )

    def tearDown(self) -> None:
        self.storage.close()
        self.directory.cleanup()

    def pgl_event(self):
        return self.registry_port.approved_events(event_id="pgl-wallachia-s8-2026")[0]

    def discover(self, match_id: int = 8_001) -> bool:
        summary = {
            "match_id": match_id,
            "leagueid": 19_543,
            "start_time": int(NOW.timestamp()),
            "series_id": 77,
        }
        return self.store.record_discovered_match(
            self.pgl_event(), summary, NOW, "opendota_league"
        )

    def archive_payload(self, payload: dict, observed_at: datetime = NOW):
        return self.archive.archive_json(
            source="opendota",
            endpoint=f"/api/matches/{payload['match_id']}",
            request_identity=(
                f"https://api.opendota.com/api/matches/{payload['match_id']}?api_key=secret"
            ),
            payload_bytes=canonical_json_bytes(payload),
            observed_at=observed_at,
            match_id=payload["match_id"],
            status_code=200,
            first_usable_at=observed_at,
        )

    def seed_heroes(self, first: int = 1) -> None:
        self.storage.connection.executemany(
            "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
            [(hero_id, f"Hero {hero_id}") for hero_id in range(first, first + 10)],
        )
        self.storage.connection.commit()

    def test_registry_scope_and_candidate_never_create_formal_status(self) -> None:
        events = self.registry_port.approved_events()
        self.assertEqual([event.league_id for event in events], [19543, 19696, 19101, 19785])
        self.assertEqual(self.registry_port.approved_events(active_at=NOW), [events[0]])

        approved = {"match_id": 1, "leagueid": 19543, "start_time": int(NOW.timestamp())}
        outside = dict(approved, match_id=2, start_time=int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp()))
        mismatch = dict(approved, match_id=3, leagueid=99999)
        self.assertTrue(self.registry_port.classify_discovered_match(events[0], approved).formal)
        self.assertEqual(
            self.registry_port.classify_discovered_match(events[0], outside).reason,
            "outside_stage_boundaries",
        )
        decision = self.registry_port.classify_discovered_match(events[0], mismatch)
        self.store.record_candidate_match(events[0], mismatch, decision.reason, NOW)

        self.assertEqual(self.registry.formal_matches(), ())
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM event_candidates").fetchone()[0],
            1,
        )

    def test_catalog_discovery_is_pending_only_and_preserves_source_fields(self) -> None:
        catalog = [
            {"leagueid": 19_543, "name": "PGL Wallachia 2026 Season 8"},
            {"leagueid": 19_719, "name": "The International 2026", "ticket": "ti"},
            {"leagueid": 20_002, "name": "Unknown Future League"},
            {"leagueid": 18_999, "name": "PGL Historical"},
        ]

        class CatalogClient:
            async def get_leagues(self) -> list[dict]:
                return catalog

            async def get_league_matches(self, league_id: int) -> list[dict]:
                raise AssertionError("catalog discovery must not fetch matches")

            async def get_match(self, match_id: int) -> dict:
                raise AssertionError("catalog discovery must not fetch details")

        ingestor = StrictEventIngestor(
            self.registry_port,
            self.store,
            self.archive,
            OpenDotaAdapter(CatalogClient(), clock=lambda: NOW),
            processor=completed_match_processing_result,
            clock=lambda: NOW,
        )

        first = asyncio.run(ingestor.discover_event_candidates(NOW))
        second = asyncio.run(
            ingestor.discover_event_candidates(NOW + timedelta(hours=1))
        )

        self.assertEqual((first.catalog_rows, first.candidates_seen), (4, 2))
        self.assertEqual((first.candidates_created, second.candidates_created), (2, 0))
        rows = self.storage.connection.execute(
            """SELECT provider_event_id, evidence_status, audit_status, evidence_json
                 FROM event_candidates
                WHERE source='opendota_league_catalog'
                ORDER BY provider_event_id"""
        ).fetchall()
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in rows],
            [("19719", "unverified", "pending"), ("20002", "unverified", "pending")],
        )
        evidence = json.loads(rows[0][3])
        self.assertEqual(evidence["source_fields"]["ticket"], "ti")
        self.assertEqual(evidence["decision"], "pending_manual_audit")
        self.assertEqual(len(self.registry.formal_events()), 4)
        self.assertIsNotNone(
            self.storage.connection.execute(
                "SELECT 1 FROM raw_source_observations WHERE endpoint='/api/leagues'"
            ).fetchone()
        )
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM match_ingest_status").fetchone()[0],
            0,
        )

    def test_rejected_match_candidates_keep_distinct_match_evidence(self) -> None:
        event = self.pgl_event()
        for match_id in (101, 102):
            summary = {
                "match_id": match_id,
                "leagueid": event.league_id,
                "start_time": int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp()),
            }
            decision = self.registry_port.classify_discovered_match(event, summary)
            self.store.record_candidate_match(event, summary, decision.reason, NOW)

        rows = self.storage.connection.execute(
            "SELECT provider_event_id, evidence_json FROM event_candidates ORDER BY candidate_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {json.loads(row["evidence_json"])["match_id"] for row in rows},
            {101, 102},
        )

    def test_legacy_status_retry_recent_rescan_and_checkpoint_survive_restart(self) -> None:
        self.storage.connection.execute(
            "INSERT INTO matches (match_id, leagueid, start_time) VALUES (?, ?, ?)",
            (7_001, 19_543, int(NOW.timestamp())),
        )
        self.storage.connection.commit()
        self.assertEqual(self.store.list_legacy_match_ids(self.pgl_event()), (7_001,))

        self.assertTrue(self.discover(7_001))
        status = self.store.get_ingest_status(7_001)
        assert status is not None
        self.assertEqual(status.start_time, int(NOW.timestamp()))
        self.assertEqual(self.store.begin_ingest_attempt(7_001, NOW), 1)
        self.store.record_ingest_failure(
            match_id=7_001,
            attempted_at=NOW,
            attempt_count=1,
            error="temporary",
            next_retry_at=NOW + timedelta(minutes=15),
        )
        self.assertEqual(self.store.list_due_match_ids(NOW + timedelta(minutes=14)), ())
        self.assertEqual(self.store.list_due_match_ids(NOW + timedelta(minutes=15)), (7_001,))

        self.storage.connection.execute(
            "UPDATE match_ingest_status SET detailed_parse_state='ready', next_retry_at=NULL WHERE match_id=7001"
        )
        self.storage.connection.commit()
        self.assertTrue(self.store.get_ingest_status(7_001).detail_complete)  # type: ignore[union-attr]
        self.assertEqual(
            self.store.list_recent_rescan_match_ids(NOW - timedelta(days=1), NOW, None),
            (7_001,),
        )

        self.store.set_scheduler_checkpoint("active_poll", NOW)
        retry = SchedulerRetryState(
            2,
            NOW + timedelta(hours=1),
            "catalog unavailable",
            NOW,
        )
        self.store.set_scheduler_retry_state("candidate_scan", retry, NOW)
        reopened = IntelligenceStorage(self.path)
        try:
            reopened_store = SQLiteIngestAdapter(
                reopened, EventRegistry(reopened), Database(connection=reopened.connection)
            )
            self.assertEqual(reopened_store.get_scheduler_checkpoint("active_poll"), NOW)
            self.assertEqual(
                reopened_store.get_scheduler_retry_state("candidate_scan"), retry
            )
            reopened_store.set_scheduler_checkpoint("candidate_scan", NOW)
            self.assertIsNone(
                reopened_store.get_scheduler_retry_state("candidate_scan")
            )
        finally:
            reopened.close()

    def test_raw_artifact_and_every_observation_are_persisted_without_secret(self) -> None:
        payload = completed_payload()
        self.discover()
        first = self.archive_payload(payload)
        second = self.archive_payload(payload, NOW + timedelta(seconds=1))
        third = self.archive.archive_json(
            source="opendota",
            endpoint=f"/api/matches/{payload['match_id']}",
            request_identity=f"/api/matches/{payload['match_id']}?limit=1",
            payload_bytes=canonical_json_bytes(payload),
            observed_at=NOW,
            match_id=payload["match_id"],
            status_code=200,
        )

        self.assertEqual(first.content_sha256, second.content_sha256)
        self.assertNotEqual(first.observation_id, third.observation_id)
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM raw_source_artifacts").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM raw_source_observations").fetchone()[0],
            3,
        )
        identities = [
            row[0]
            for row in self.storage.connection.execute(
                "SELECT sanitized_request_identity FROM raw_source_observations"
            )
        ]
        self.assertTrue(all("secret" not in identity for identity in identities))
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT event_id FROM raw_source_observations LIMIT 1"
            ).fetchone()[0],
            "pgl-wallachia-s8-2026",
        )

    def test_raw_observation_can_be_promoted_once_to_first_usable(self) -> None:
        payload = completed_payload()
        self.discover()
        initial = self.archive.archive_json(
            source="opendota",
            endpoint=f"/api/matches/{payload['match_id']}",
            request_identity=f"/api/matches/{payload['match_id']}",
            payload_bytes=canonical_json_bytes(payload),
            observed_at=NOW,
            match_id=payload["match_id"],
            status_code=200,
        )
        promoted = self.archive.archive_json(
            source="opendota",
            endpoint=f"/api/matches/{payload['match_id']}",
            request_identity=f"/api/matches/{payload['match_id']}",
            payload_bytes=canonical_json_bytes(payload),
            observed_at=NOW,
            match_id=payload["match_id"],
            status_code=200,
            first_usable_at=NOW + timedelta(seconds=1),
        )

        self.assertEqual(initial.observation_id, promoted.observation_id)
        row = self.storage.connection.execute(
            "SELECT first_usable_at FROM raw_source_observations WHERE observation_id=?",
            (initial.observation_id,),
        ).fetchone()
        self.assertEqual(row["first_usable_at"], (NOW + timedelta(seconds=1)).isoformat())

    def test_raw_artifact_conflict_cannot_implicitly_relocate_or_recreate(self) -> None:
        payload = completed_payload()
        self.discover()
        first = self.archive_payload(payload)
        observation_count = self.storage.connection.execute(
            "SELECT COUNT(*) FROM raw_source_observations"
        ).fetchone()[0]

        other = RawArchive(
            Path(self.directory.name) / "other-raw",
            observation_sink=self.store.record_raw_artifact,
        )
        with self.assertRaisesRegex(RuntimeError, "persisted authority"):
            other.archive_json(
                source="opendota",
                endpoint=f"/api/matches/{payload['match_id']}",
                request_identity=f"/api/matches/{payload['match_id']}",
                payload_bytes=canonical_json_bytes(payload),
                observed_at=NOW + timedelta(seconds=1),
                match_id=payload["match_id"],
                status_code=200,
            )
        self.assertEqual(list((Path(self.directory.name) / "other-raw").rglob("*.json.gz")), [])
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM raw_source_observations"
            ).fetchone()[0],
            observation_count,
        )

        first.path.unlink()
        recreated = RawArchive(
            Path(self.directory.name) / "raw",
            observation_sink=self.store.record_raw_artifact,
        )
        with self.assertRaisesRegex(RuntimeError, "registered_file_was_missing"):
            recreated.archive_json(
                source="opendota",
                endpoint=f"/api/matches/{payload['match_id']}",
                request_identity=f"/api/matches/{payload['match_id']}",
                payload_bytes=canonical_json_bytes(payload),
                observed_at=NOW + timedelta(seconds=2),
                match_id=payload["match_id"],
                status_code=200,
            )
        self.assertFalse(first.path.exists())

    def test_raw_artifact_relocation_is_verified_and_durably_audited(self) -> None:
        payload = completed_payload()
        self.discover()
        receipt = self.archive_payload(payload)
        artifact_id = f"opendota:{receipt.content_sha256}"
        relocation_root = Path(self.directory.name) / "relocated-raw"
        destination = relocation_root / receipt.path.name
        destination.parent.mkdir()
        shutil.copy2(receipt.path, destination)

        relocation_ids = relocate_raw_source_artifacts(
            self.storage.connection,
            {artifact_id: destination},
            allowed_new_roots=[relocation_root],
            reason="database bundle restore",
            actor="test_restore",
            relocated_at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(len(relocation_ids), 1)
        row = self.storage.connection.execute(
            """SELECT artifact_id, content_hash, source, old_storage_path,
                      new_storage_path, reason, actor, relocation_sequence
                 FROM raw_source_artifact_relocations"""
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                artifact_id,
                receipt.content_sha256,
                "opendota",
                str(receipt.path.resolve()),
                str(destination.resolve()),
                "database bundle restore",
                "test_restore",
                1,
            ),
        )
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT storage_path FROM raw_source_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()[0],
            str(destination.resolve()),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit is immutable"):
            self.storage.connection.execute(
                "UPDATE raw_source_artifact_relocations SET actor='forged'"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit is required"):
            self.storage.connection.execute(
                "UPDATE raw_source_artifacts SET storage_path='forged' WHERE artifact_id=?",
                (artifact_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "identity is immutable"):
            self.storage.connection.execute(
                "UPDATE raw_source_artifacts SET compressed_bytes=0 WHERE artifact_id=?",
                (artifact_id,),
            )

    def test_same_payload_hash_from_two_sources_has_two_artifacts(self) -> None:
        payload = completed_payload()
        self.discover()
        canonical = canonical_json_bytes(payload)
        self.archive_payload(payload)
        self.archive.archive_json(
            source="stratz",
            endpoint="/graphql",
            request_identity="https://api.stratz.com/graphql?auth_token=secret",
            payload_bytes=canonical,
            observed_at=NOW,
            match_id=payload["match_id"],
            status_code=200,
        )

        rows = self.storage.connection.execute(
            "SELECT artifact_id, source, content_hash FROM raw_source_artifacts ORDER BY source"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["content_hash"], rows[1]["content_hash"])
        self.assertNotEqual(rows[0]["artifact_id"], rows[1]["artifact_id"])

    def test_detail_identity_mismatch_becomes_candidate_and_review(self) -> None:
        self.discover()
        payload = completed_payload()
        payload["leagueid"] = 99999
        decision = self.store.validate_match_payload(payload["match_id"], payload)
        self.assertFalse(decision.formal)
        self.assertEqual(decision.reason, "league_mismatch")

        self.store.record_scope_rejection(
            payload["match_id"], payload, decision.reason, NOW
        )

        row = self.storage.connection.execute(
            "SELECT stage_in_scope, ingest_state FROM match_ingest_status WHERE match_id=8001"
        ).fetchone()
        self.assertEqual(tuple(row), (0, "review_required"))
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM event_candidates").fetchone()[0],
            1,
        )

    def test_identity_mismatch_response_is_archived_before_scope_rejection(self) -> None:
        summary = {
            "match_id": 8_001,
            "leagueid": 19_543,
            "start_time": int(NOW.timestamp()),
        }

        class MismatchedClient:
            async def get_league_matches(self, league_id: int) -> list[dict]:
                return [summary]

            async def get_match(self, match_id: int) -> dict:
                payload = completed_payload(match_id=9_999)
                return payload

        client = OpenDotaAdapter(MismatchedClient(), clock=lambda: NOW)
        ingestor = StrictEventIngestor(
            self.registry_port,
            self.store,
            self.archive,
            client,
            processor=completed_match_processing_result,
            clock=lambda: NOW,
        )

        report = asyncio.run(
            ingestor.run_once(event_id="pgl-wallachia-s8-2026")
        )

        self.assertEqual(report.failed, 1)
        row = self.storage.connection.execute(
            "SELECT stage_in_scope, ingest_state, next_retry_at FROM match_ingest_status WHERE match_id=8001"
        ).fetchone()
        self.assertEqual(tuple(row), (0, "review_required", None))
        detail = self.storage.connection.execute(
            """SELECT match_id, first_usable_at FROM raw_source_observations
               WHERE endpoint='/api/matches/8001'"""
        ).fetchone()
        self.assertEqual(tuple(detail), (8_001, None))
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
            0,
        )

    def test_changed_map_runs_durable_derived_pipeline_idempotently(self) -> None:
        payload = completed_payload()
        payload["start_time"] = int((NOW - timedelta(hours=1)).timestamp())
        summary = {
            "match_id": payload["match_id"],
            "leagueid": payload["leagueid"],
            "start_time": payload["start_time"],
        }

        class CompletedClient:
            async def get_leagues(self) -> list[dict]:
                return []

            async def get_league_matches(self, league_id: int) -> list[dict]:
                return [summary]

            async def get_match(self, match_id: int) -> dict:
                return payload

        ingestor = StrictEventIngestor(
            self.registry_port,
            self.store,
            self.archive,
            OpenDotaAdapter(CompletedClient(), clock=lambda: NOW),
            processor=completed_match_processing_result,
            clock=lambda: NOW,
        )
        ingest_report = asyncio.run(
            ingestor.run_once(event_id="pgl-wallachia-s8-2026")
        )
        self.assertEqual(ingest_report.changed_match_ids, (8_001,))

        pipeline = StrictDerivedPipeline(self.path)
        first = pipeline.run(ingest_report.changed_match_ids)
        second = pipeline.run(())
        requested_again = pipeline.run(ingest_report.changed_match_ids)

        self.assertEqual((first.derived_maps, first.assignment_rows), (1, 20))
        self.assertEqual((first.score_rows, first.state_rows), (10, 2))
        self.assertEqual(second.derived_maps, 0)
        self.assertEqual(requested_again.derived_maps, 1)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM player_map_scores WHERE score_version=?",
                    (SCORE_VERSION,),
                ).fetchone()[0],
                10,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM strict_derived_status").fetchone()[0],
                1,
            )
            status = connection.execute(
                """SELECT normalizer_version, benchmark_version
                     FROM strict_derived_status WHERE match_id=8001"""
            ).fetchone()
            self.assertEqual(
                tuple(status), (MATCH_PROCESSOR_VERSION, BENCHMARK_VERSION)
            )
            connection.execute(
                """UPDATE strict_derived_status
                      SET normalizer_version='old-normalizer'
                    WHERE match_id=8001"""
            )
            connection.commit()
            self.assertEqual(pipeline._pending_ids(connection), {8_001})
            connection.execute(
                """UPDATE strict_derived_status
                      SET normalizer_version=?, benchmark_version='old-benchmark'
                    WHERE match_id=8001""",
                (MATCH_PROCESSOR_VERSION,),
            )
            connection.commit()
            self.assertEqual(pipeline._pending_ids(connection), {8_001})
            sources = pipeline._source_snapshots(connection, {8_001})
            connection.execute(
                """UPDATE player_map_scores
                      SET explanation_json='{"benchmark_version":"old-benchmark"}'
                    WHERE match_id=8001 AND player_slot=0"""
            )
            connection.commit()
            with self.assertRaisesRegex(RuntimeError, "benchmark version mismatch"):
                pipeline._verify_derived(connection, sources)
            connection.execute(
                """UPDATE player_map_scores
                      SET explanation_json=json_set(
                          explanation_json, '$.benchmark_version', ?)
                    WHERE match_id=8001 AND player_slot=0""",
                (BENCHMARK_VERSION,),
            )
            connection.execute(
                """UPDATE match_ingest_status
                      SET state_readiness='retryable' WHERE match_id=8001"""
            )
            connection.commit()
            player_only = pipeline.run((8_001,), force=True)
            self.assertEqual(player_only.score_rows, 10)
            self.assertEqual(player_only.state_rows, 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM team_style_profiles").fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_changed_earlier_map_rederives_all_causal_successors(self) -> None:
        earlier = completed_payload(8_001, 1)
        later = completed_payload(8_002, 11)
        earlier["start_time"] = int((NOW - timedelta(hours=2)).timestamp())
        later["start_time"] = int((NOW - timedelta(hours=1)).timestamp())
        payloads = {8_001: earlier, 8_002: later}

        class CompletedClient:
            async def get_leagues(self) -> list[dict]:
                return []

            async def get_league_matches(self, league_id: int) -> list[dict]:
                return [
                    {
                        "match_id": payload["match_id"],
                        "leagueid": payload["leagueid"],
                        "start_time": payload["start_time"],
                    }
                    for payload in payloads.values()
                ]

            async def get_match(self, match_id: int) -> dict:
                return payloads[match_id]

        ingestor = StrictEventIngestor(
            self.registry_port,
            self.store,
            self.archive,
            OpenDotaAdapter(CompletedClient(), clock=lambda: NOW),
            processor=completed_match_processing_result,
            clock=lambda: NOW,
        )
        first_ingest = asyncio.run(
            ingestor.run_once(event_id="pgl-wallachia-s8-2026")
        )
        self.assertEqual(first_ingest.changed_match_ids, (8_001, 8_002))
        pipeline = StrictDerivedPipeline(self.path)
        self.assertEqual(pipeline.run(first_ingest.changed_match_ids).derived_maps, 2)

        self.storage.connection.execute(
            """UPDATE strict_derived_status
                  SET source_content_hash=? WHERE match_id=8001""",
            ("0" * 64,),
        )
        self.storage.connection.commit()

        rerun = pipeline.run(())
        self.assertEqual((rerun.pending_maps, rerun.derived_maps), (2, 2))

        self.storage.connection.execute(
            "UPDATE match_ingest_status SET stage_in_scope=0 WHERE match_id=8001"
        )
        self.storage.connection.commit()
        after_retirement = pipeline.run(())
        self.assertEqual(after_retirement.derived_maps, 1)
        self.assertEqual(
            [
                int(row[0])
                for row in self.storage.connection.execute(
                    "SELECT match_id FROM strict_derived_status ORDER BY match_id"
                )
            ],
            [8_002],
        )
        scopes = current_derived_scopes(self.storage.connection)
        self.assertEqual(scopes.formal, frozenset({8_002}))
        self.assertEqual(scopes.player, frozenset({8_002}))
        self.assertEqual(scopes.state, frozenset({8_002}))

    def test_state_and_player_components_complete_independently(self) -> None:
        payload = completed_payload()
        payload["start_time"] = int((NOW - timedelta(hours=1)).timestamp())
        summary = {
            "match_id": payload["match_id"],
            "leagueid": payload["leagueid"],
            "start_time": payload["start_time"],
        }

        class CompletedClient:
            async def get_leagues(self) -> list[dict]:
                return []

            async def get_league_matches(self, league_id: int) -> list[dict]:
                return [summary]

            async def get_match(self, match_id: int) -> dict:
                return payload

        ingestor = StrictEventIngestor(
            self.registry_port,
            self.store,
            self.archive,
            OpenDotaAdapter(CompletedClient(), clock=lambda: NOW),
            processor=completed_match_processing_result,
            clock=lambda: NOW,
        )
        ingested = asyncio.run(
            ingestor.run_once(event_id="pgl-wallachia-s8-2026")
        )
        self.storage.connection.execute(
            """UPDATE match_ingest_status
                  SET player_readiness='retryable', state_readiness='ready'
                WHERE match_id=8001"""
        )
        self.storage.connection.commit()

        pipeline = StrictDerivedPipeline(self.path)
        state_only = pipeline.run(ingested.changed_match_ids)
        self.assertEqual(state_only.assignment_rows, 0)
        self.assertEqual(state_only.score_rows, 0)
        self.assertEqual(state_only.state_rows, 2)
        scopes = current_derived_scopes(self.storage.connection)
        self.assertEqual(scopes.state, frozenset({8_001}))
        self.assertEqual(scopes.player, frozenset())
        original_cutoff = self.storage.connection.execute(
            "SELECT profile_cutoff FROM strict_derived_status WHERE match_id=8001"
        ).fetchone()[0]

        self.storage.connection.execute(
            """UPDATE match_ingest_status SET player_readiness='ready'
                 WHERE match_id=8001"""
        )
        self.storage.connection.commit()
        player_only = pipeline.run(())
        self.assertEqual(player_only.score_rows, 10)
        self.assertEqual(player_only.state_rows, 0)
        self.assertIsNone(player_only.profile_cutoff)
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT profile_cutoff FROM strict_derived_status WHERE match_id=8001"
            ).fetchone()[0],
            original_cutoff,
        )
        scopes = current_derived_scopes(self.storage.connection)
        self.assertEqual(scopes.state, frozenset({8_001}))
        self.assertEqual(scopes.player, frozenset({8_001}))

    def test_missing_gold_curve_delivers_unscorable_state_and_stays_retryable(
        self,
    ) -> None:
        payload = completed_payload()
        payload["start_time"] = int((NOW - timedelta(hours=1)).timestamp())
        payload.pop("radiant_gold_adv")
        summary = {
            "match_id": payload["match_id"],
            "leagueid": payload["leagueid"],
            "start_time": payload["start_time"],
        }

        class CompletedClient:
            async def get_leagues(self) -> list[dict]:
                return []

            async def get_league_matches(self, league_id: int) -> list[dict]:
                return [summary]

            async def get_match(self, match_id: int) -> dict:
                return payload

        ingestor = StrictEventIngestor(
            self.registry_port,
            self.store,
            self.archive,
            OpenDotaAdapter(CompletedClient(), clock=lambda: NOW),
            processor=completed_match_processing_result,
            clock=lambda: NOW,
        )
        report = asyncio.run(
            ingestor.run_once(event_id="pgl-wallachia-s8-2026")
        )
        self.assertEqual(report.retryable, 1)
        status = self.storage.connection.execute(
            """SELECT state_readiness, next_retry_at
                 FROM match_ingest_status WHERE match_id=8001"""
        ).fetchone()
        self.assertEqual(status["state_readiness"], "unscorable")
        self.assertIsNotNone(status["next_retry_at"])

        derived = StrictDerivedPipeline(self.path).run(report.changed_match_ids)
        self.assertEqual(derived.state_rows, 2)
        labels = {
            str(row[0])
            for row in self.storage.connection.execute(
                "SELECT label FROM team_map_states WHERE match_id=8001"
            )
        }
        self.assertEqual(labels, {"state_unscorable"})
        self.assertEqual(
            current_derived_scopes(self.storage.connection).state,
            frozenset({8_001}),
        )

    def test_registry_profile_context_change_invalidates_only_affected_event(self) -> None:
        rows = (
            (8_001, "pgl-wallachia-s8-2026", "a" * 64),
            (8_002, "ewc-dota2-2026", "b" * 64),
        )
        self.storage.connection.executemany(
            """INSERT INTO match_ingest_status
               (match_id, event_id, start_time, stage_scope, stage_in_scope,
                has_valid_result, ingest_state, latest_raw_content_hash,
                normalizer_version, player_readiness, state_readiness,
                discovered_at, updated_at)
               VALUES (?, ?, 1, 'main_event', 1, 1, 'detailed', ?, ?,
                       'ready', 'ready', ?, ?)""",
            (
                (
                    match_id,
                    event_id,
                    content_hash,
                    MATCH_PROCESSOR_VERSION,
                    NOW.isoformat(),
                    NOW.isoformat(),
                )
                for match_id, event_id, content_hash in rows
            ),
        )
        pipeline = StrictDerivedPipeline(self.path)
        snapshots = pipeline._source_snapshots(
            self.storage.connection, {match_id for match_id, _, _ in rows}
        )
        self.storage.connection.executemany(
            """INSERT INTO strict_derived_status
               (match_id, source_content_hash, role_assignment_version,
                score_version, team_state_version, profile_version,
                profile_cutoff, derived_at, normalizer_version,
                benchmark_version, profile_context_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    match_id,
                    content_hash,
                    ROLE_VERSION,
                    SCORE_VERSION,
                    LABEL_VERSION,
                    PROFILE_VERSION,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    MATCH_PROCESSOR_VERSION,
                    BENCHMARK_VERSION,
                    snapshots[match_id].profile_context_hash,
                )
                for match_id, _, content_hash in rows
            ),
        )
        self.storage.connection.commit()
        self.assertEqual(
            pipeline._pending_components(self.storage.connection).base,
            frozenset(),
        )

        included_stages = self.storage.connection.execute(
            """SELECT included_stages_json FROM event_registry
                 WHERE event_id='ewc-dota2-2026'"""
        ).fetchone()[0]
        self.storage.connection.execute(
            """UPDATE event_registry SET included_stages_json=?
                 WHERE event_id='ewc-dota2-2026'""",
            (json.dumps(json.loads(included_stages), indent=2),),
        )
        self.storage.connection.commit()
        self.assertEqual(
            pipeline._pending_components(self.storage.connection).base,
            frozenset(),
        )

        self.storage.connection.execute(
            """UPDATE event_registry
                  SET prize_pool_usd=prize_pool_usd + 1000000
                WHERE event_id='ewc-dota2-2026'"""
        )
        self.storage.connection.commit()

        self.assertEqual(
            pipeline._pending_components(self.storage.connection).base,
            frozenset({8_002}),
        )
        with self.assertRaisesRegex(RuntimeError, "source version changed"):
            pipeline._verify_source_snapshots(self.storage.connection, snapshots)

    def test_source_change_during_derivation_cannot_complete_lineage(self) -> None:
        payload = completed_payload()
        payload["start_time"] = int((NOW - timedelta(hours=1)).timestamp())
        summary = {
            "match_id": payload["match_id"],
            "leagueid": payload["leagueid"],
            "start_time": payload["start_time"],
        }

        class CompletedClient:
            async def get_leagues(self) -> list[dict]:
                return []

            async def get_league_matches(self, league_id: int) -> list[dict]:
                return [summary]

            async def get_match(self, match_id: int) -> dict:
                return payload

        ingestor = StrictEventIngestor(
            self.registry_port,
            self.store,
            self.archive,
            OpenDotaAdapter(CompletedClient(), clock=lambda: NOW),
            processor=completed_match_processing_result,
            clock=lambda: NOW,
        )
        ingest_report = asyncio.run(
            ingestor.run_once(event_id="pgl-wallachia-s8-2026")
        )

        from scripts.build_strict_team_profiles import (
            build_strict_profiles as real_build_profiles,
        )

        def change_source(*args: object, **kwargs: object) -> object:
            report = real_build_profiles(*args, **kwargs)
            connection = sqlite3.connect(self.path)
            try:
                connection.execute(
                    """UPDATE match_ingest_status
                          SET latest_raw_content_hash=? WHERE match_id=8001""",
                    ("f" * 64,),
                )
                connection.commit()
            finally:
                connection.close()
            return report

        with patch(
            "scripts.build_strict_team_profiles.build_strict_profiles",
            side_effect=change_source,
        ):
            with self.assertRaisesRegex(RuntimeError, "source version changed"):
                StrictDerivedPipeline(self.path).run(ingest_report.changed_match_ids)

        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM strict_derived_status"
            ).fetchone()[0],
            0,
        )
        verification = sqlite3.connect(self.path)
        verification.row_factory = sqlite3.Row
        try:
            sources = StrictDerivedPipeline._source_snapshots(verification, {8_001})
            with self.assertRaisesRegex(RuntimeError, "derived source hash mismatch"):
                StrictDerivedPipeline._verify_derived(verification, sources)
        finally:
            verification.close()

    def test_nonselected_earlier_dependency_change_aborts_later_rebuild(self) -> None:
        earlier = completed_payload(8_001, 1)
        later = completed_payload(8_002, 11)
        earlier["start_time"] = int((NOW - timedelta(hours=2)).timestamp())
        later["start_time"] = int((NOW - timedelta(hours=1)).timestamp())
        payloads = {8_001: earlier, 8_002: later}

        class CompletedClient:
            async def get_leagues(self) -> list[dict]:
                return []

            async def get_league_matches(self, league_id: int) -> list[dict]:
                return [
                    {
                        "match_id": payload["match_id"],
                        "leagueid": payload["leagueid"],
                        "start_time": payload["start_time"],
                    }
                    for payload in payloads.values()
                ]

            async def get_match(self, match_id: int) -> dict:
                return payloads[match_id]

        ingestor = StrictEventIngestor(
            self.registry_port,
            self.store,
            self.archive,
            OpenDotaAdapter(CompletedClient(), clock=lambda: NOW),
            processor=completed_match_processing_result,
            clock=lambda: NOW,
        )
        report = asyncio.run(
            ingestor.run_once(event_id="pgl-wallachia-s8-2026")
        )
        pipeline = StrictDerivedPipeline(self.path)
        pipeline.run(report.changed_match_ids)

        from scripts.build_strict_team_profiles import (
            build_strict_profiles as real_build_profiles,
        )

        def change_earlier_dependency(*args: object, **kwargs: object) -> object:
            built = real_build_profiles(*args, **kwargs)
            connection = sqlite3.connect(self.path)
            try:
                connection.execute(
                    """UPDATE match_ingest_status
                          SET latest_raw_content_hash=? WHERE match_id=8001""",
                    ("e" * 64,),
                )
                connection.commit()
            finally:
                connection.close()
            return built

        with patch(
            "scripts.build_strict_team_profiles.build_strict_profiles",
            side_effect=change_earlier_dependency,
        ):
            with self.assertRaisesRegex(RuntimeError, "source version changed"):
                pipeline.run((8_002,), force=True)

        status_hash = self.storage.connection.execute(
            "SELECT source_content_hash FROM strict_derived_status WHERE match_id=8001"
        ).fetchone()[0]
        latest_hash = self.storage.connection.execute(
            "SELECT latest_raw_content_hash FROM match_ingest_status WHERE match_id=8001"
        ).fetchone()[0]
        self.assertNotEqual(status_hash, latest_hash)

    def test_success_atomically_writes_legacy_exact_facts_and_readiness(self) -> None:
        payload = completed_payload()
        payload["picks_bans"].append(
            {"hero_id": 99, "is_pick": False, "team": 0, "order": 24}
        )
        self.discover()
        receipt = self.archive_payload(payload)
        processing = completed_match_processing_result(payload, payload["match_id"])
        attempt = self.store.begin_ingest_attempt(payload["match_id"], NOW)

        self.store.record_ingest_success(
            match_id=payload["match_id"],
            attempted_at=NOW,
            attempt_count=attempt,
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

        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM matches WHERE match_id=8001").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM match_players WHERE match_id=8001").fetchone()[0],
            10,
        )
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM player_map_facts WHERE match_id=8001").fetchone()[0],
            10,
        )
        status = self.storage.connection.execute(
            """SELECT detailed_parse_state, player_readiness, state_readiness,
                      draft_readiness, latest_raw_content_hash, raw_artifact_version,
                      has_valid_result
               FROM match_ingest_status WHERE match_id=8001"""
        ).fetchone()
        self.assertEqual(
            tuple(status),
            ("ready", "ready", "ready", "ready", receipt.content_sha256, 1, 1),
        )
        facts_json = json.loads(
            self.storage.connection.execute(
                "SELECT facts_json FROM player_map_facts WHERE match_id=8001 AND player_slot=0"
            ).fetchone()[0]
        )
        self.assertEqual(facts_json["kills"], 0)
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM heroes").fetchone()[0],
            11,
        )

    def test_normalization_failure_rolls_back_but_raw_evidence_survives(self) -> None:
        payload = completed_payload(hero_start=101)
        self.discover()
        receipt = self.archive_payload(payload)
        facts = extract_completed_match_facts(payload)
        attempt = self.store.begin_ingest_attempt(payload["match_id"], NOW)

        class FailingDatabase(Database):
            def insert_match(self, match: dict, commit: bool = True) -> None:
                super().insert_match(match, commit=False)
                raise sqlite3.IntegrityError("injected child write failure")

        failing_store = SQLiteIngestAdapter(
            self.storage,
            self.registry,
            FailingDatabase(connection=self.storage.connection),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            failing_store.record_ingest_success(
                match_id=payload["match_id"],
                attempted_at=NOW,
                attempt_count=attempt,
                content_sha256=receipt.content_sha256,
                first_usable_at=NOW,
                payload=payload,
                facts=facts,
                artifact_unchanged=False,
                detail_complete=True,
                retryable=False,
                missing_reasons=(),
                next_retry_at=None,
            )

        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM player_map_facts").fetchone()[0], 0)
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM raw_source_artifacts").fetchone()[0], 1)
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM raw_source_observations").fetchone()[0], 1)
        row = self.storage.connection.execute(
            "SELECT ingest_state, latest_raw_content_hash FROM match_ingest_status WHERE match_id=8001"
        ).fetchone()
        self.assertEqual(tuple(row), ("detail_pending", None))

    def test_stale_attempt_cannot_reject_succeed_or_fail_after_newer_attempt(self) -> None:
        payload = completed_payload()
        self.discover()
        receipt = self.archive_payload(payload)
        processing = completed_match_processing_result(payload, payload["match_id"])
        stale_attempt = self.store.begin_ingest_attempt(payload["match_id"], NOW)
        stale_generation = stale_attempt.generation
        current_at = NOW + timedelta(seconds=1)
        current_attempt = self.store.begin_ingest_attempt(payload["match_id"], current_at)
        current_generation = current_attempt.generation

        self.store.record_scope_rejection(
            payload["match_id"],
            dict(payload, leagueid=99999),
            "league_mismatch",
            NOW,
            attempt_count=stale_attempt,
            attempt_generation=stale_generation,
        )
        stale_outcome = self.store.record_ingest_success(
            match_id=payload["match_id"], attempted_at=NOW,
            attempt_count=stale_attempt, content_sha256=receipt.content_sha256,
            first_usable_at=NOW, payload=payload, facts=processing.facts,
            artifact_unchanged=False, detail_complete=True, retryable=False,
            missing_reasons=(), next_retry_at=None,
            attempt_generation=stale_generation,
        )
        self.assertEqual(stale_outcome, "superseded")
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT stage_in_scope FROM match_ingest_status WHERE match_id=8001"
            ).fetchone()[0],
            1,
        )

        current_outcome = self.store.record_ingest_success(
            match_id=payload["match_id"], attempted_at=current_at,
            attempt_count=current_attempt, content_sha256=receipt.content_sha256,
            first_usable_at=current_at, payload=payload, facts=processing.facts,
            artifact_unchanged=False, detail_complete=True, retryable=False,
            missing_reasons=(), next_retry_at=None,
            attempt_generation=current_generation,
        )
        self.assertEqual(current_outcome, "normalized")
        reused_at = NOW
        reused_attempt = self.store.begin_ingest_attempt(payload["match_id"], reused_at)
        reused_generation = reused_attempt.generation
        self.assertEqual(reused_attempt, stale_attempt)
        self.store.record_ingest_failure(
            match_id=payload["match_id"], attempted_at=NOW,
            attempt_count=stale_attempt, error="stale failure",
            next_retry_at=NOW + timedelta(minutes=15),
            attempt_generation=stale_generation,
        )
        status = self.storage.connection.execute(
            """SELECT ingest_state, retry_count, next_retry_at, last_error
               FROM match_ingest_status WHERE match_id=8001"""
        ).fetchone()
        self.assertEqual(tuple(status)[:2], ("detail_pending", reused_attempt))
        self.assertIsNone(status["last_error"])

        reused_outcome = self.store.record_ingest_success(
            match_id=payload["match_id"], attempted_at=reused_at,
            attempt_count=reused_attempt, content_sha256=receipt.content_sha256,
            first_usable_at=reused_at, payload=payload, facts=None,
            artifact_unchanged=True, detail_complete=True, retryable=False,
            missing_reasons=(), next_retry_at=None,
            attempt_generation=reused_generation,
        )
        self.assertEqual(reused_outcome, "unchanged")
        final = self.storage.connection.execute(
            """SELECT ingest_state, retry_count, next_retry_at, last_error
               FROM match_ingest_status WHERE match_id=8001"""
        ).fetchone()
        self.assertEqual(tuple(final), ("detailed", 0, None, None))

    def test_same_raw_hash_is_reprocessed_after_normalizer_upgrade(self) -> None:
        payload = completed_payload()
        self.discover()
        receipt = self.archive_payload(payload)
        processing = completed_match_processing_result(payload, payload["match_id"])
        first_attempt = self.store.begin_ingest_attempt(payload["match_id"], NOW)
        self.assertEqual(
            self.store.record_ingest_success(
                match_id=payload["match_id"], attempted_at=NOW,
                attempt_count=first_attempt, content_sha256=receipt.content_sha256,
                first_usable_at=NOW, payload=payload, facts=processing.facts,
                artifact_unchanged=False, detail_complete=True, retryable=False,
                missing_reasons=(), next_retry_at=None,
                processor_version="opendota-exact-v0",
            ),
            "normalized",
        )

        upgraded_at = NOW + timedelta(days=1)
        upgraded_attempt = self.store.begin_ingest_attempt(payload["match_id"], upgraded_at)
        self.assertEqual(
            self.store.record_ingest_success(
                match_id=payload["match_id"], attempted_at=upgraded_at,
                attempt_count=upgraded_attempt, content_sha256=receipt.content_sha256,
                first_usable_at=upgraded_at, payload=payload, facts=processing.facts,
                artifact_unchanged=False, detail_complete=True, retryable=False,
                missing_reasons=(), next_retry_at=None,
                processor_version="opendota-exact-v2",
            ),
            "normalized",
        )
        row = self.storage.connection.execute(
            """SELECT normalizer_version, raw_artifact_version
               FROM match_ingest_status WHERE match_id=8001"""
        ).fetchone()
        self.assertEqual(tuple(row), ("opendota-exact-v2", 2))
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM player_map_facts WHERE match_id=8001"
            ).fetchone()[0],
            20,
        )
        derived = StrictDerivedPipeline(self.path).run((8_001,))
        self.assertEqual(derived.derived_maps, 1)
        lineage = self.storage.connection.execute(
            "SELECT normalizer_version FROM strict_derived_status WHERE match_id=8001"
        ).fetchone()
        self.assertEqual(lineage["normalizer_version"], "opendota-exact-v2")

    def test_less_complete_changed_payload_never_replaces_complete_version(self) -> None:
        payload = completed_payload()
        self.seed_heroes()
        self.discover()
        original_receipt = self.archive_payload(payload)
        original = completed_match_processing_result(payload, payload["match_id"])
        attempt = self.store.begin_ingest_attempt(payload["match_id"], NOW)
        self.store.record_ingest_success(
            match_id=payload["match_id"], attempted_at=NOW, attempt_count=attempt,
            content_sha256=original_receipt.content_sha256, first_usable_at=NOW,
            payload=payload, facts=original.facts, artifact_unchanged=False,
            detail_complete=original.detail_complete, retryable=original.retryable,
            missing_reasons=original.missing_reasons, next_retry_at=None,
        )

        for day in range(1, 7):
            observed_at = NOW + timedelta(days=day)
            unchanged_receipt = self.archive_payload(payload, observed_at)
            attempt = self.store.begin_ingest_attempt(payload["match_id"], observed_at)
            self.assertEqual(attempt, 1)
            outcome = self.store.record_ingest_success(
                match_id=payload["match_id"], attempted_at=observed_at,
                attempt_count=attempt, content_sha256=unchanged_receipt.content_sha256,
                first_usable_at=observed_at, payload=payload, facts=None,
                artifact_unchanged=True, detail_complete=True, retryable=False,
                missing_reasons=(), next_retry_at=None,
            )
            self.assertEqual(outcome, "unchanged")

        degraded = copy.deepcopy(payload)
        degraded.pop("objectives")
        degraded_at = NOW + timedelta(days=7)
        degraded_receipt = self.archive_payload(degraded, degraded_at)
        degraded_result = completed_match_processing_result(degraded, payload["match_id"])
        attempt = self.store.begin_ingest_attempt(payload["match_id"], degraded_at)
        self.assertEqual(attempt, 1)
        outcome = self.store.record_ingest_success(
            match_id=payload["match_id"], attempted_at=degraded_at,
            attempt_count=attempt, content_sha256=degraded_receipt.content_sha256,
            first_usable_at=degraded_at, payload=degraded,
            facts=degraded_result.facts, artifact_unchanged=False,
            detail_complete=degraded_result.detail_complete,
            retryable=degraded_result.retryable,
            missing_reasons=degraded_result.missing_reasons,
            next_retry_at=degraded_at + timedelta(minutes=15),
        )

        self.assertEqual(outcome, "retained_more_complete")
        status = self.storage.connection.execute(
            """SELECT ingest_state, detailed_parse_state, latest_raw_content_hash,
                      raw_artifact_version, retry_count, next_retry_at
               FROM match_ingest_status WHERE match_id=8001"""
        ).fetchone()
        self.assertEqual(
            tuple(status)[:5],
            ("detailed", "ready", original_receipt.content_sha256, 1, 1),
        )
        self.assertEqual(
            datetime.fromisoformat(status["next_retry_at"]),
            degraded_at + timedelta(minutes=15),
        )
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM objectives WHERE match_id=8001"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM raw_source_artifacts").fetchone()[0],
            2,
        )

    def test_partial_version_is_replaced_only_by_strict_completeness_improvement(self) -> None:
        complete = completed_payload()
        partial = copy.deepcopy(complete)
        partial.pop("objectives")
        self.seed_heroes()
        self.discover()

        partial_receipt = self.archive_payload(partial)
        partial_result = completed_match_processing_result(partial, partial["match_id"])
        attempt = self.store.begin_ingest_attempt(partial["match_id"], NOW)
        self.assertEqual(
            self.store.record_ingest_success(
                match_id=partial["match_id"], attempted_at=NOW, attempt_count=attempt,
                content_sha256=partial_receipt.content_sha256, first_usable_at=NOW,
                payload=partial, facts=partial_result.facts, artifact_unchanged=False,
                detail_complete=partial_result.detail_complete,
                retryable=partial_result.retryable,
                missing_reasons=partial_result.missing_reasons,
                next_retry_at=NOW + timedelta(minutes=15),
            ),
            "normalized",
        )

        complete_at = NOW + timedelta(minutes=15)
        complete_receipt = self.archive_payload(complete, complete_at)
        complete_result = completed_match_processing_result(complete, complete["match_id"])
        attempt = self.store.begin_ingest_attempt(complete["match_id"], complete_at)
        self.assertEqual(
            self.store.record_ingest_success(
                match_id=complete["match_id"], attempted_at=complete_at,
                attempt_count=attempt, content_sha256=complete_receipt.content_sha256,
                first_usable_at=complete_at, payload=complete,
                facts=complete_result.facts, artifact_unchanged=False,
                detail_complete=complete_result.detail_complete,
                retryable=complete_result.retryable,
                missing_reasons=complete_result.missing_reasons, next_retry_at=None,
            ),
            "normalized",
        )
        row = self.storage.connection.execute(
            """SELECT detailed_parse_state, latest_raw_content_hash,
                      raw_artifact_version, retry_count
               FROM match_ingest_status WHERE match_id=8001"""
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("ready", complete_receipt.content_sha256, 2, 0),
        )
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM objectives WHERE match_id=8001"
            ).fetchone()[0],
            1,
        )

    def test_reconciliation_preserves_ewc_public_count_discrepancy(self) -> None:
        pgl = self.pgl_event()
        ewc = self.registry_port.approved_events(event_id="ewc-dota2-2026")[0]
        self.store.reconcile_event(pgl, set(range(1, 120)), NOW)
        self.store.reconcile_event(ewc, set(range(1, 121)), NOW)

        rows = {
            row["event_id"]: (row["observed_map_count"], row["reconciliation_status"])
            for row in self.storage.connection.execute(
                "SELECT event_id, observed_map_count, reconciliation_status FROM event_registry"
            )
        }
        self.assertEqual(rows[pgl.event_id], (119, "reconciled"))
        self.assertEqual(rows[ewc.event_id], (120, "reconciliation_pending"))

    def test_cli_default_factory_builds_concrete_ports(self) -> None:
        cli_path = Path(self.directory.name) / "cli.db"
        args = build_parser().parse_args([
            "--database", str(cli_path),
            "--archive-root", str(Path(self.directory.name) / "cli-raw"),
            "--once",
        ])

        runtime = build_default_runtime(args)
        try:
            self.assertIsInstance(runtime.ingestor._registry, RegistryIngestAdapter)
            self.assertIsInstance(runtime.ingestor._store, SQLiteIngestAdapter)
        finally:
            import asyncio

            asyncio.run(runtime.close())

    def test_cli_default_data_paths_follow_the_selected_database(self) -> None:
        database = Path(self.directory.name) / "candidate" / "strict.db"

        args = resolve_data_paths(build_parser().parse_args([
            "--database",
            str(database),
            "--once",
        ]))

        self.assertEqual(args.database, database.resolve())
        self.assertEqual(args.archive_root, database.resolve().parent / "raw-sources")
        self.assertEqual(
            args.coverage_report,
            database.resolve().parent
            / "reports"
            / "strict_event_coverage_latest.json",
        )

    def test_cli_event_and_reconcile_filters_are_one_shot(self) -> None:
        class FakeIngestor:
            def __init__(self) -> None:
                self.calls = 0

            async def run_once(self, **values: object) -> dict[str, object]:
                self.calls += 1
                return values

        class UnusedScheduler:
            async def run_due(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("one-shot filters must not enter the scheduler")

        for arguments in (
            ["--event", "pgl-wallachia-s8-2026"],
            ["--reconcile"],
        ):
            ingestor = FakeIngestor()
            runtime = Runtime(ingestor, UnusedScheduler())  # type: ignore[arg-type]
            args = build_parser().parse_args(arguments)

            result = asyncio.run(run(args, runtime_factory=lambda _: runtime))

            self.assertEqual(result, 0)
            self.assertEqual(ingestor.calls, 1)

    def test_cli_records_strict_health_and_writes_current_coverage(self) -> None:
        class FakeIngestor:
            async def run_once(self, **values: object) -> IngestReport:
                return IngestReport()

        class UnusedScheduler:
            async def run_due(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("--once must use the ingestor")

        output = Path(self.directory.name) / "coverage.json"
        runtime = Runtime(
            FakeIngestor(),  # type: ignore[arg-type]
            UnusedScheduler(),  # type: ignore[arg-type]
            database=self.path,
            health_connection=self.storage.connection,
            coverage_report=output,
        )
        args = build_parser().parse_args(["--once"])

        self.assertEqual(asyncio.run(run(args, runtime_factory=lambda _: runtime)), 0)

        health = self.storage.connection.execute(
            "SELECT status, details_json FROM service_health "
            "WHERE component='strict_ingest_worker'"
        ).fetchone()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(json.loads(health["details_json"])["source"], "worker")
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["formal_maps"], 0)
        self.assertEqual(report["versions"]["player_score"], SCORE_VERSION)

    def test_cli_marks_isolated_catalog_failure_as_degraded(self) -> None:
        class UnusedIngestor:
            async def run_once(self, **values: object) -> None:
                raise AssertionError("scheduler one-shot must use the scheduler")

        class DegradedScheduler:
            async def run_due(self, *args: object, **kwargs: object) -> ScheduleRun:
                return ScheduleRun(
                    active_polled=True,
                    recent_rescanned=True,
                    changed_match_ids=(8_001,),
                    candidate_error="catalog unavailable",
                    candidate_retry_at=NOW + timedelta(minutes=15),
                    candidate_error_at=NOW,
                )

        runtime = Runtime(
            UnusedIngestor(),  # type: ignore[arg-type]
            DegradedScheduler(),  # type: ignore[arg-type]
            health_connection=self.storage.connection,
        )
        args = build_parser().parse_args(["--scheduler-once"])

        self.assertEqual(asyncio.run(run(args, runtime_factory=lambda _: runtime)), 0)
        health = self.storage.connection.execute(
            """SELECT status, last_success_at, last_error_at, last_error,
                      details_json FROM service_health
                 WHERE component='strict_ingest_worker'"""
        ).fetchone()
        self.assertEqual((health["status"], health["last_error"]), (
            "degraded",
            "catalog unavailable",
        ))
        self.assertIsNotNone(health["last_success_at"])
        self.assertEqual(health["last_error_at"], NOW.isoformat())
        self.assertEqual(
            json.loads(health["details_json"])["run"]["changed_match_ids"],
            [8_001],
        )

    def test_catalog_backoff_heartbeat_preserves_real_result_times(self) -> None:
        runtime = Runtime(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            health_connection=self.storage.connection,
        )
        _record_runtime_health(
            runtime,
            "degraded",
            NOW,
            successful=True,
            error="catalog unavailable",
            error_at=NOW,
        )
        heartbeat = NOW + timedelta(minutes=5)
        _record_runtime_health(
            runtime,
            "degraded",
            heartbeat,
            successful=False,
            error="catalog unavailable",
            error_at=NOW,
        )

        row = self.storage.connection.execute(
            """SELECT last_heartbeat_at, last_success_at, last_error_at
                 FROM service_health WHERE component='strict_ingest_worker'"""
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (heartbeat.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )

    def test_cli_scheduler_once_runs_one_due_cycle_and_closes_runtime(self) -> None:
        class UnusedIngestor:
            async def run_once(self, **values: object) -> None:
                raise AssertionError("scheduler one-shot must use the scheduler")

        class FakeScheduler:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def run_due(self, now: object, **values: object) -> dict[str, object]:
                self.calls.append({"now": now, **values})
                return values

        for arguments, include_recent in (
            (["--scheduler-once"], True),
            (["--scheduler-once", "--active"], False),
        ):
            scheduler = FakeScheduler()
            closed: list[bool] = []
            runtime = Runtime(
                UnusedIngestor(),
                scheduler,  # type: ignore[arg-type]
                close_callbacks=(lambda: closed.append(True),),
            )
            args = build_parser().parse_args(arguments)

            result = asyncio.run(run(args, runtime_factory=lambda _: runtime))

            self.assertEqual(result, 0)
            self.assertEqual(len(scheduler.calls), 1)
            self.assertEqual(scheduler.calls[0]["include_recent"], include_recent)
            self.assertEqual(closed, [True])

    def test_cli_scheduler_once_rejects_direct_one_shot_filters(self) -> None:
        for direct_arguments in (
            ["--once"],
            ["--reconcile"],
            ["--event", "pgl-wallachia-s8-2026"],
            ["--match", "1"],
        ):
            args = build_parser().parse_args(["--scheduler-once", *direct_arguments])
            with self.assertRaisesRegex(ValueError, "direct one-shot filters"):
                asyncio.run(run(args, runtime_factory=lambda _: None))  # type: ignore[arg-type,return-value]

    def test_cli_rejects_empty_event_id(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--event", " "])

if __name__ == "__main__":
    unittest.main()
