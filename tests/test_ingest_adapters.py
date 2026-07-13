from __future__ import annotations

import copy
import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_intelligence.facts import extract_completed_match_facts
from event_intelligence.ingest import StrictEventIngestor, completed_match_processing_result
from event_intelligence.ingest_adapters import (
    RegistryIngestAdapter,
    SQLiteIngestAdapter,
)
from event_intelligence.raw_archive import RawArchive, canonical_json_bytes
from event_intelligence.opendota import OpenDotaAdapter
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage
from fetch.db import Database
from scripts.run_strict_event_ingest import (
    Runtime,
    build_default_runtime,
    build_parser,
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
        reopened = IntelligenceStorage(self.path)
        try:
            reopened_store = SQLiteIngestAdapter(
                reopened, EventRegistry(reopened), Database(connection=reopened.connection)
            )
            self.assertEqual(reopened_store.get_scheduler_checkpoint("active_poll"), NOW)
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
                processor_version="opendota-exact-v1",
            ),
            "normalized",
        )
        row = self.storage.connection.execute(
            """SELECT normalizer_version, raw_artifact_version
               FROM match_ingest_status WHERE match_id=8001"""
        ).fetchone()
        self.assertEqual(tuple(row), ("opendota-exact-v1", 2))
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM player_map_facts WHERE match_id=8001"
            ).fetchone()[0],
            20,
        )

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


if __name__ == "__main__":
    unittest.main()
