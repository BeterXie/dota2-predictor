from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from live_betting.engine import price_groups
from live_betting.browser_contract import canonical_json, payload_sha256
from live_betting.health import record_health
from live_betting.markets import normalized_state_hash
from live_betting.models import Market, OddsSnapshot
from live_betting.storage import LiveBettingStore
from tests.draft_authority_fixture import (
    make_test_vision_observation,
    seed_test_draft_authority,
)
from web import queries
from web.app import app
from web.monitoring import (
    _current_winner,
    build_monitor_snapshot,
    current_markets,
    derive_health,
    monitor_match_detail,
    monitor_cursor,
    winner_timeline,
)


NOW = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)


def raw_odds_payload(rows: list[OddsSnapshot]) -> dict[str, object]:
    match_id = rows[0].raybet_match_id
    outcomes = []
    for row in rows:
        outcomes.append(
            {
                "id": row.odds_id,
                "odds_group_id": row.odds_group_id,
                "team_id": 11 if row.market.side == "team_one" else 22,
                "match_stage": f"r{row.market.period.removeprefix('map_')}",
                "group_short_name": "Winner",
                "tag": "win",
                "odds": str(row.price),
                "status": row.status,
            }
        )
    return {
        "result": {
            "id": match_id,
            "team": [
                {"team_id": 11, "team_name": "Radiant Five", "pos": 1},
                {"team_id": 22, "team_name": "Dire Five", "pos": 2},
            ],
            "odds": outcomes,
        }
    }


class MonitoringDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "monitor.db"
        self.store = LiveBettingStore(self.database)
        self.store.init_schema()
        self.strict_mapping_id: int | None = None

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def add_match(
        self,
        match_id: str = "match-1",
        *,
        status: int = 1,
        scheduled_at: str = "2026-07-14 22:00:00",
        updated_at: datetime = NOW,
    ) -> None:
        self.store.upsert_raybet_match(
            {
                "id": match_id,
                "tournament_name": "World Cup",
                "start_time": scheduled_at,
                "round": "bo3",
                "status": status,
                "team": [
                    {"id": 11, "pos": 1, "team_name": "Radiant Five"},
                    {"id": 22, "pos": 2, "team_name": "Dire Five"},
                ],
            },
            updated_at,
        )
        self.store.connection.commit()

    def add_browser_page_event(
        self,
        *,
        match_id: str = "42",
        page_origin: str = "https://www.ray086.com",
        page_path: str = "/sports/esports",
        event_id: str = "a" * 64,
    ) -> None:
        payload = {"result": {"id": match_id, "game_id": 151, "odds": []}}
        self.store.insert_browser_event(
            {
                "schema_version": 1,
                "event_id": event_id,
                "capture_session_id": "b" * 32,
                "captured_at_utc": NOW,
                "page_origin": page_origin,
                "page_path": page_path,
                "source_path": "/v2/odds",
                "transport": "xhr",
                "event_type": "odds",
                "raybet_match_id": match_id,
                "game_id": 151,
                "payload": payload,
                "payload_hash": payload_sha256(payload),
                "payload_bytes": len(canonical_json(payload)),
                "capture_reason": None,
                "extension_version": "0.1.0",
            },
            received_at=NOW,
            recognized=True,
            processing_status="processed",
        )
        self.store.connection.commit()

    def add_winner_pair(
        self,
        observed_at: datetime,
        one: float,
        two: float,
        *,
        period: str = "map_1",
        status: int = 5,
    ) -> None:
        for odds_id, side, price in (
            (f"winner-{period}-one", "team_one", one),
            (f"winner-{period}-two", "team_two", two),
        ):
            self.store.insert_odds(
                OddsSnapshot(
                    "match-1",
                    odds_id,
                    f"winner-{period}",
                    observed_at,
                    price,
                    status,
                    Market("winner", period, side, None, side, True),
                )
            )
        self.store.connection.commit()

    def add_winner_response(
        self,
        observed_at: datetime,
        one: float,
        two: float | None,
        *,
        match_id: str = "match-1",
        observation_key: str,
        period: str = "map_1",
        status: int = 1,
    ) -> list[OddsSnapshot]:
        snapshots = [
            OddsSnapshot(
                match_id,
                f"winner-{period}-one",
                f"winner-{period}",
                observed_at,
                one,
                status,
                Market("winner", period, "team_one", None, "team_one", True),
            )
        ]
        if two is not None:
            snapshots.append(
                OddsSnapshot(
                    match_id,
                    f"winner-{period}-two",
                    f"winner-{period}",
                    observed_at,
                    two,
                    status,
                    Market("winner", period, "team_two", None, "team_two", True),
                )
            )
        self.store.store_odds_observation(
            source="direct",
            observation_key=observation_key,
            source_event_id=None,
            raybet_match_id=match_id,
            observed_at=observed_at,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
            raw_payload=raw_odds_payload(snapshots),
        )
        return snapshots

    def ensure_strict_mapping(self) -> int:
        if self.strict_mapping_id is not None:
            return self.strict_mapping_id
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS event_registry (event_id TEXT PRIMARY KEY)"
        )
        self.store.connection.execute(
            "INSERT OR IGNORE INTO event_registry VALUES ('monitor-test')"
        )
        identity_json = "{}"
        identity_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        recorded_at = (NOW - timedelta(days=1)).isoformat()
        cursor = self.store.connection.execute(
            """INSERT INTO strict_live_map_mappings
               (raybet_match_id, map_number, event_id, team_one_id,
                team_two_id, canonical_team_one_id,
                canonical_team_one_name, canonical_team_two_id,
                canonical_team_two_name, canonical_identity_json,
                canonical_identity_hash, crosswalk_evidence_json,
                crosswalk_evidence_hash, stage_scope, scheduled_at_utc,
                raybet_best_of, raybet_identity_json,
                raybet_identity_hash, raybet_metadata_updated_at, source,
                evidence_json, evidence_hash, mapping_version,
                acceptance_mode, automatic_approval_id, accepted_by,
                accepted_at, recorded_at, created_at)
               VALUES ('match-1', 1, 'monitor-test', 11, 22, 11,
                       'Radiant Five', 22, 'Dire Five', ?, ?, ?, ?,
                       'main_event', ?, 3, ?, ?, ?, 'test', ?, ?, 'test-v1',
                       'manual_exact', NULL, 'test', ?, ?, ?)""",
            (
                identity_json,
                identity_hash,
                identity_json,
                identity_hash,
                recorded_at,
                identity_json,
                identity_hash,
                recorded_at,
                identity_json,
                identity_hash,
                recorded_at,
                recorded_at,
                recorded_at,
            ),
        )
        self.store.connection.commit()
        self.strict_mapping_id = int(cursor.lastrowid)
        return self.strict_mapping_id

    def add_decision(
        self,
        decision_key: str,
        decided_at: datetime,
        model_probability: float,
        *,
        map_number: int = 1,
    ) -> None:
        mapping_id = self.ensure_strict_mapping()
        draft_authority = seed_test_draft_authority(
            self.store.connection,
            raybet_match_id="match-1",
            map_number=map_number,
            strict_mapping_id=mapping_id,
            observed_at=decided_at,
            label=f"monitor:{decision_key}",
        )
        vision = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=map_number,
            captured_at=decided_at,
            label=f"monitor-frame:{decision_key}",
        )
        self.store.insert_vision_observation(vision)
        rows = self.add_winner_response(
            decided_at,
            2.5,
            5.0 / 3.0,
            observation_key=f"monitor-transport:{decision_key}",
            period=f"map_{map_number}",
        )
        market_probability = price_groups(rows)[rows[0].odds_id]
        decision = SimpleNamespace(
            decision_key=decision_key,
            raybet_match_id="match-1",
            map_number=map_number,
            decided_at=decided_at,
            underdog_side="team_one",
            market_probability=market_probability,
            model_probability=model_probability,
            edge=model_probability - market_probability,
            data_quality=0.9,
            eligible=True,
            reason="eligible",
            contributions={
                "__inputs__": {
                    "draft_authority": asdict(draft_authority),
                    "strict_live_eligibility": {
                        "mapping_refs": {"strict_mapping_id": mapping_id}
                    },
                }
            },
            input_ref=f"input-{decision_key}",
            strategy_version="strategy-v1",
        )
        self.assertTrue(
            self.store.insert_decision(
                decision,
                draft_authority=draft_authority,
                vision_observation=vision,
                vision_transport_key=f"monitor-transport:{decision_key}",
            )
        )

    def test_stale_healthy_heartbeat_is_derived_as_unhealthy(self) -> None:
        heartbeat = NOW - timedelta(minutes=5)
        record_health(
            self.store.connection,
            "raybet_worker",
            "healthy",
            heartbeat_at=heartbeat,
            success_at=heartbeat,
        )

        health = derive_health(self.store.connection, now=NOW)

        row = next(item for item in health if item["component"] == "raybet_worker")
        self.assertEqual(row["reported_status"], "healthy")
        self.assertEqual(row["status"], "unhealthy")
        self.assertEqual(row["freshness"], "stale")
        self.assertEqual(row["age_seconds"], 300.0)

    def test_optional_unconfigured_mail_is_not_counted_as_an_abnormal_process(self) -> None:
        for component in ("raybet_worker", "shadow_worker"):
            record_health(
                self.store.connection,
                component,
                "healthy",
                heartbeat_at=NOW,
                success_at=NOW,
            )
        record_health(
            self.store.connection,
            "mail",
            "degraded",
            heartbeat_at=NOW,
            error_at=NOW,
            error="configuration_missing",
        )
        record_health(
            self.store.connection,
            "mail_worker",
            "degraded",
            heartbeat_at=NOW - timedelta(days=1),
            error_at=NOW - timedelta(days=1),
            error="configuration_missing",
        )

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["summary"]["unhealthy_components"], 0)
        mail = next(item for item in snapshot["health"] if item["component"] == "mail")
        self.assertEqual(mail["status"], "degraded")
        self.assertEqual(mail["last_error"], "configuration_missing")

    def test_non_optional_worker_health_is_counted(self) -> None:
        for component in ("raybet_worker", "shadow_worker"):
            record_health(
                self.store.connection,
                component,
                "healthy",
                heartbeat_at=NOW,
                success_at=NOW,
            )
        record_health(
            self.store.connection,
            "vision_worker",
            "unhealthy",
            heartbeat_at=NOW,
            error_at=NOW,
            error="capture_failed",
        )

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["summary"]["unhealthy_components"], 1)

    def test_snapshot_keeps_unconfirmed_matches_and_marks_missing_readiness(self) -> None:
        self.add_match()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(len(snapshot["matches"]), 1)
        match = snapshot["matches"][0]
        self.assertEqual(match["raybet_match_id"], "match-1")
        self.assertEqual(match["lifecycle"], "degraded")
        self.assertEqual(match["readiness"]["odds"]["status"], "missing")
        self.assertEqual(match["readiness"]["mapping"]["status"], "missing")

    def test_signed_or_legacy_live_url_is_not_exposed_as_playable(self) -> None:
        self.add_match(match_id="42", status=2)
        self.store.connection.execute(
            """UPDATE raybet_matches
                  SET live_url=?, raw_json=?
                WHERE raybet_match_id='42'""",
            (
                "https://qplay.ehome.gg/live/42.m3u8",
                '{"live_url":"https://qplay.ehome.gg/live/42.m3u8"}',
            ),
        )
        self.store.connection.commit()

        match = build_monitor_snapshot(self.store.connection, now=NOW)["matches"][0]

        self.assertIsNone(match["live_url"])
        self.assertEqual(
            match["watch_link"],
            {
                "kind": "none",
                "availability": "unavailable",
                "url": None,
                "reason": "no_safe_entry",
            },
        )

    def test_verified_unsigned_public_stream_is_available_without_page_evidence(
        self,
    ) -> None:
        public_url = "https://qplay.ehome.gg/live/42.m3u8"
        self.store.upsert_raybet_match(
            {
                "id": "42",
                "game_id": 151,
                "status": 2,
                "team": [],
            },
            NOW,
            public_live_url=public_url,
        )
        self.store.connection.commit()

        match = build_monitor_snapshot(self.store.connection, now=NOW)["matches"][0]

        self.assertEqual(
            match["watch_link"],
            {
                "kind": "public_stream",
                "availability": "available",
                "url": public_url,
                "reason": "verified_unsigned_stream",
            },
        )

    def test_captured_allowlisted_match_page_takes_priority_over_public_stream(
        self,
    ) -> None:
        public_url = "https://qplay.ehome.gg/live/42.m3u8"
        self.store.upsert_raybet_match(
            {"id": "42", "game_id": 151, "status": 2, "team": []},
            NOW,
            public_live_url=public_url,
        )
        self.add_browser_page_event()

        match = build_monitor_snapshot(self.store.connection, now=NOW)["matches"][0]

        self.assertEqual(
            match["watch_link"],
            {
                "kind": "match_page",
                "availability": "available",
                "url": "https://www.ray086.com/sports/esports",
                "reason": "captured_raybet_match_page",
            },
        )

    def test_match_page_rejects_foreign_origin_and_unsafe_path(self) -> None:
        for index, (origin, path) in enumerate(
            (
                ("javascript://ray086.com", "/sports/esports"),
                ("https://evil.example", "/sports/esports"),
                ("https://www.ray086.com", "//evil.example/redirect"),
                ("https://www.ray086.com", "/sports/esports/../redirect"),
            )
        ):
            with self.subTest(origin=origin, path=path):
                match_id = str(50 + index)
                self.add_match(match_id=match_id, status=2)
                self.add_browser_page_event(
                    match_id=match_id,
                    page_origin=origin,
                    page_path=path,
                    event_id=f"{index + 1:064x}",
                )
                match = monitor_match_detail(
                    self.store.connection, match_id, now=NOW
                )
                assert match is not None
                self.assertEqual(match["watch_link"]["availability"], "unavailable")

    def test_old_database_without_browser_events_fails_closed(self) -> None:
        self.add_match(match_id="42", status=2)
        self.store.connection.execute("DROP TABLE browser_events")
        self.store.connection.execute(
            """UPDATE raybet_matches
                  SET live_url='https://qplay.ehome.gg/live/42.m3u8'
                WHERE raybet_match_id='42'"""
        )
        self.store.connection.commit()

        match = monitor_match_detail(self.store.connection, "42", now=NOW)

        assert match is not None
        self.assertEqual(match["watch_link"]["availability"], "unavailable")

    def test_monitor_cursor_changes_when_match_page_evidence_arrives(self) -> None:
        self.add_match(match_id="42", status=2)
        before = monitor_cursor(self.store.connection)

        self.add_browser_page_event(match_id="42")

        self.assertNotEqual(monitor_cursor(self.store.connection), before)

    def test_strict_mapping_impact_is_removed_from_summary_and_detail(self) -> None:
        self.add_match(status=2)
        self.add_decision("older-valid", NOW - timedelta(seconds=20), 0.55)
        self.add_decision("newer-impacted", NOW - timedelta(seconds=5), 0.75)
        before = monitor_cursor(self.store.connection)
        self.store.connection.execute("PRAGMA foreign_keys=OFF")
        self.store.connection.execute(
            """INSERT INTO strict_live_mapping_impacts
               (mapping_id, invalidation_id, dependent_type, dependent_key,
                reason, recorded_at)
               VALUES (1, 1, 'strategy_decision', 'newer-impacted',
                       'mapping_invalidated', ?)""",
            (NOW.isoformat(),),
        )
        self.store.connection.commit()
        self.store.connection.execute("PRAGMA foreign_keys=ON")

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        self.assertNotEqual(before, monitor_cursor(self.store.connection))
        self.assertEqual(
            snapshot["matches"][0]["latest_decision"]["model_probability"],
            0.55,
        )
        assert detail is not None
        self.assertEqual(
            [decision["decision_key"] for decision in detail["decisions"]],
            ["older-valid"],
        )

    def test_decision_views_fail_closed_without_strict_impact_relation(self) -> None:
        self.add_match(status=2)
        self.add_decision("decision-1", NOW - timedelta(seconds=5), 0.65)
        self.store.connection.execute("DROP TABLE strict_live_mapping_impacts")
        self.store.connection.commit()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        match = snapshot["matches"][0]
        self.assertIsNone(match["latest_decision"])
        self.assertEqual(match["readiness"]["model"]["status"], "missing")
        assert detail is not None
        self.assertEqual(detail["decisions"], [])

    def test_decision_views_fail_closed_with_malformed_strict_impact_relation(
        self,
    ) -> None:
        self.add_match(status=2)
        self.add_decision("decision-1", NOW - timedelta(seconds=5), 0.65)
        self.store.connection.execute("DROP TABLE strict_live_mapping_impacts")
        self.store.connection.execute(
            "CREATE TABLE strict_live_mapping_impacts (dependent_type TEXT)"
        )
        self.store.connection.commit()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        match = snapshot["matches"][0]
        self.assertIsNone(match["latest_decision"])
        self.assertEqual(match["readiness"]["model"]["status"], "missing")
        assert detail is not None
        self.assertEqual(detail["decisions"], [])

    def test_decision_detail_excludes_vision_invalidated_and_conflicted_rows(
        self,
    ) -> None:
        self.add_match(status=2)
        self.add_decision("older-valid", NOW - timedelta(seconds=30), 0.55)
        self.add_decision("vision-invalidated", NOW - timedelta(seconds=20), 0.65)
        self.add_decision("draft-conflicted", NOW - timedelta(seconds=5), 0.75)
        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES ('strategy_decision', 'vision-invalidated', 'match-1', 1,
                       'bad_frame', 'vision_observation_invalidated', ?)""",
            (NOW.isoformat(),),
        )
        self.store.connection.execute(
            """UPDATE vision_draft_anchors
                  SET status='conflict', conflict_at=?
                WHERE raybet_match_id='match-1' AND map_number=1""",
            ((NOW - timedelta(seconds=10)).isoformat(),),
        )
        self.store.connection.commit()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        self.assertEqual(
            snapshot["matches"][0]["latest_decision"]["model_probability"],
            0.55,
        )
        assert detail is not None
        self.assertEqual(
            [decision["decision_key"] for decision in detail["decisions"]],
            ["older-valid"],
        )

    def test_latest_vision_requires_a_confirmed_frame_after_invalidation(self) -> None:
        self.add_match(status=2)
        older = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=NOW - timedelta(seconds=10),
            game_clock_seconds=110,
            radiant_team_side=None,
            clock_confidence=0.99,
            draft_confidence=0.99,
            label="frame-valid",
        )
        invalidated = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=NOW - timedelta(seconds=5),
            game_clock_seconds=115,
            radiant_team_side=None,
            clock_confidence=0.99,
            draft_confidence=0.99,
            label="frame-invalidated",
        )
        self.store.insert_vision_observation(older)
        self.store.insert_vision_observation(invalidated)
        self.store.connection.execute(
            """INSERT INTO vision_observation_invalidations
               (raybet_match_id, captured_at, source_frame_ref,
                invalidated_at, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "match-1",
                invalidated.captured_at.isoformat(),
                invalidated.source_frame_ref,
                NOW.isoformat(),
                "manual_bad_frame",
            ),
        )
        self.store.connection.execute(
            """UPDATE vision_observations SET confirmed=0
                WHERE raybet_match_id=? AND captured_at=? AND source_frame_ref=?""",
            (
                "match-1",
                invalidated.captured_at.isoformat(),
                invalidated.source_frame_ref,
            ),
        )
        self.store.connection.commit()

        blocked = build_monitor_snapshot(
            self.store.connection,
            now=NOW,
        )["matches"][0]["latest_vision"]

        self.assertIsNone(blocked)

        restored = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=NOW - timedelta(seconds=2),
            game_clock_seconds=118,
            radiant_team_side=None,
            clock_confidence=0.99,
            draft_confidence=0.99,
            label="frame-restored",
        )
        self.store.insert_vision_observation(restored)
        self.store.connection.commit()

        latest = build_monitor_snapshot(
            self.store.connection,
            now=NOW,
        )["matches"][0]["latest_vision"]

        self.assertEqual(latest["observed_at"], restored.captured_at.isoformat())
        self.assertEqual(latest["game_clock_seconds"], 118)
        self.assertEqual(latest["confirmed"], 1)

    def test_provider_status_two_is_live_only_while_fresh(self) -> None:
        self.add_match(status=2)

        fresh = build_monitor_snapshot(self.store.connection, now=NOW)
        stale = build_monitor_snapshot(
            self.store.connection, now=NOW + timedelta(seconds=91)
        )

        self.assertEqual(fresh["matches"][0]["lifecycle"], "live")
        self.assertEqual(stale["matches"][0]["lifecycle"], "degraded")
        self.assertFalse(stale["matches"][0]["history_eligible"])

    def test_long_stale_match_is_replayable_without_claiming_it_ended(self) -> None:
        self.add_match(
            status=2,
            scheduled_at="2026-07-13 22:00:00",
            updated_at=NOW - timedelta(days=1),
        )

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        match = snapshot["matches"][0]
        self.assertEqual(match["lifecycle"], "degraded")
        self.assertTrue(match["history_eligible"])

    def test_recent_transport_activity_blocks_history_archive_even_with_stale_metadata(self) -> None:
        self.add_match(
            status=2,
            scheduled_at="2026-07-13 22:00:00",
            updated_at=NOW - timedelta(days=1),
        )
        recent = NOW - timedelta(minutes=5)
        self.add_winner_response(
            recent,
            2.0,
            2.0,
            observation_key="recent-transport",
        )

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        match = snapshot["matches"][0]
        self.assertEqual(match["lifecycle"], "degraded")
        self.assertFalse(match["history_eligible"])
        self.assertEqual(match["latest_odds_activity_at"], recent.isoformat())

    def test_audit_only_transport_activity_also_blocks_history_archive(self) -> None:
        self.add_match(
            status=2,
            scheduled_at="2026-07-13 22:00:00",
            updated_at=NOW - timedelta(days=1),
        )
        recent = NOW - timedelta(minutes=2)
        authority_at = recent - timedelta(seconds=1)
        self.add_winner_response(
            authority_at,
            2.0,
            2.0,
            observation_key="audit-authority",
        )
        authority = self.store.connection.execute(
            """SELECT normalized_state_hash, response_state_hash,
                      response_artifact_hash
                 FROM odds_transport_observations
                WHERE observation_key='audit-authority'"""
        ).fetchone()
        self.store.insert_transport_observation(
            observation_key="recent-audit-only",
            source="direct",
            source_event_id=None,
            raybet_match_id="match-1",
            observed_at=recent,
            normalized_state_hash=str(authority[0]),
            response_state_hash=str(authority[1]),
            response_artifact_hash=str(authority[2]),
            timing_status="late",
            processing_status="audit_only",
            normalized_change_count=0,
        )
        self.store.connection.commit()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        match = snapshot["matches"][0]
        self.assertFalse(match["history_eligible"])
        self.assertEqual(match["latest_odds_activity_at"], recent.isoformat())

    def test_invalid_match_activity_timestamp_fails_closed_for_history(self) -> None:
        self.add_match(
            status=2,
            scheduled_at="2026-07-13 22:00:00",
            updated_at=NOW - timedelta(days=1),
        )
        self.store.connection.execute(
            "UPDATE raybet_matches SET updated_at='not-a-timestamp' WHERE raybet_match_id='match-1'"
        )
        self.store.connection.commit()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertFalse(snapshot["matches"][0]["history_eligible"])

    def test_history_and_live_summary_counts_use_their_view_filters(self) -> None:
        self.add_match(
            match_id="live",
            status=2,
            scheduled_at="2026-07-14 13:00:00",
            updated_at=NOW,
        )
        self.add_match(
            match_id="archived",
            status=2,
            scheduled_at="2026-07-13 22:00:00",
            updated_at=NOW - timedelta(days=1),
        )
        self.add_match(
            match_id="upcoming",
            status=1,
            scheduled_at="2026-07-15 22:00:00",
            updated_at=NOW,
        )

        summary = build_monitor_snapshot(self.store.connection, now=NOW)["summary"]

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["degraded"], 1)
        self.assertEqual(summary["live_view"]["total"], 2)
        self.assertEqual(summary["live_view"]["degraded"], 0)
        self.assertEqual(summary["live_view"]["live"], 1)
        self.assertEqual(summary["live_view"]["upcoming"], 1)
        self.assertEqual(summary["history_view"]["total"], 1)
        self.assertEqual(summary["history_view"]["degraded"], 1)

    def test_history_matches_are_sorted_newest_first(self) -> None:
        self.add_match(
            match_id="older",
            status=5,
            scheduled_at="2026-07-12 22:00:00",
            updated_at=NOW - timedelta(days=2),
        )
        self.add_match(
            match_id="newer",
            status=5,
            scheduled_at="2026-07-13 22:00:00",
            updated_at=NOW - timedelta(days=1),
        )

        matches = build_monitor_snapshot(self.store.connection, now=NOW)["matches"]

        self.assertEqual([row["raybet_match_id"] for row in matches], ["newer", "older"])

    def test_provider_status_five_is_ended(self) -> None:
        self.add_match(status=5)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["lifecycle"], "ended")
        self.assertTrue(snapshot["matches"][0]["history_eligible"])

    def test_provider_completed_list_status_three_is_ended(self) -> None:
        self.add_match(status=3)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["lifecycle"], "ended")

    def test_upcoming_match_defaults_to_first_unsettled_map(self) -> None:
        self.add_match(scheduled_at="2026-07-15 22:00:00")
        for period, status in (("map_1", 1), ("map_2", 1), ("map_3", 4)):
            self.add_winner_pair(NOW, 2.0, 2.0, period=period, status=status)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["winner"]["period"], "map_1")

    def test_upcoming_match_uses_map_one_when_periods_arrive_separately(self) -> None:
        self.add_match(scheduled_at="2026-07-15 22:00:00")
        self.add_winner_pair(
            NOW - timedelta(seconds=8), 2.0, 2.0, period="map_1", status=1
        )
        self.add_winner_pair(NOW, 2.0, 2.0, period="map_2", status=1)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        winner = snapshot["matches"][0]["winner"]
        self.assertEqual(winner["period"], "map_1")
        self.assertEqual(
            winner["observed_at"], (NOW - timedelta(seconds=8)).isoformat()
        )

    def test_winner_uses_latest_complete_response_even_when_quotes_are_unchanged(self) -> None:
        self.add_match(scheduled_at="2026-07-15 22:00:00")
        first = NOW - timedelta(seconds=12)
        latest_complete = NOW - timedelta(seconds=6)
        self.add_winner_response(first, 2.0, 2.0, observation_key="response-1")
        self.add_winner_response(
            latest_complete,
            2.0,
            2.0,
            observation_key="response-2",
        )
        self.add_winner_response(
            NOW,
            2.2,
            None,
            observation_key="response-3-incomplete",
        )

        latest_snapshot = self.store.connection.execute(
            "SELECT MAX(received_at) FROM odds_snapshots WHERE raybet_match_id='match-1'"
        ).fetchone()[0]
        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(latest_snapshot, NOW.isoformat())
        winner = snapshot["matches"][0]["winner"]
        self.assertEqual(winner["observed_at"], latest_complete.isoformat())
        self.assertEqual(winner["prices"], {"team_one": 2.0, "team_two": 2.0})
        self.assertEqual(
            snapshot["matches"][0]["readiness"]["odds"],
            {
                "status": "ready",
                "observed_at": NOW.isoformat(),
                "age_seconds": 0.0,
            },
        )
        latest_markets = current_markets(self.store.connection, "match-1")
        self.assertEqual(len(latest_markets), 1)
        self.assertEqual(latest_markets[0]["side"], "team_one")
        self.assertEqual(latest_markets[0]["received_at"], NOW.isoformat())

    def test_transport_schema_error_does_not_fall_back_to_legacy_winner(self) -> None:
        self.add_match(status=2)
        self.add_winner_pair(NOW - timedelta(minutes=1), 2.0, 2.0, status=1)
        self.add_winner_response(
            NOW,
            2.0,
            2.0,
            observation_key="malformed-response-schema",
        )
        self.store.connection.execute("DROP TABLE odds_response_state_outcomes")
        self.store.connection.execute(
            "CREATE TABLE odds_response_state_outcomes (response_state_hash TEXT)"
        )
        self.store.connection.commit()

        winner = _current_winner(
            self.store.connection,
            "match-1",
            provider_status="2",
        )

        self.assertIsNone(winner)

    def test_malformed_transport_relation_does_not_fall_back_to_legacy_winner(self) -> None:
        self.add_match(status=2)
        self.add_winner_pair(NOW - timedelta(minutes=1), 2.0, 2.0, status=1)
        self.store.connection.execute("DROP TABLE odds_transport_observations")
        self.store.connection.execute(
            """CREATE TABLE odds_transport_observations (
                observation_key TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL
            )"""
        )
        self.store.connection.execute(
            """INSERT INTO odds_transport_observations
               (observation_key, observed_at) VALUES (?, ?)""",
            ("malformed-transport", NOW.isoformat()),
        )
        self.store.connection.commit()

        winner = _current_winner(
            self.store.connection,
            "match-1",
            provider_status="2",
        )

        self.assertIsNone(winner)

    def test_live_match_skips_explicitly_settled_maps(self) -> None:
        self.add_match(status=2)
        for period, status in (("map_1", 5), ("map_2", 1), ("map_3", 1)):
            self.add_winner_pair(NOW, 2.0, 2.0, period=period, status=status)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["winner"]["period"], "map_2")

    def test_closed_winner_pair_is_not_reported_as_complete(self) -> None:
        self.add_match(status=2)
        self.add_winner_pair(NOW, 2.0, 2.0, status=4)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertFalse(snapshot["matches"][0]["winner"]["complete"])

    def test_ended_match_uses_last_settled_map_not_future_market(self) -> None:
        self.add_match(status=5)
        for period, status in (("map_1", 5), ("map_2", 5), ("map_3", 4)):
            self.add_winner_pair(NOW, 2.0, 2.0, period=period, status=status)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["winner"]["period"], "map_2")

    def test_winner_timeline_uses_only_observed_points_and_devigged_probability(self) -> None:
        self.add_match()
        first = NOW - timedelta(seconds=12)
        second = NOW - timedelta(seconds=6)
        self.add_winner_response(
            first, 2.0, 2.0, observation_key="timeline-first"
        )
        self.add_winner_response(
            second, 4.0, 4 / 3, observation_key="timeline-second"
        )

        timeline = winner_timeline(self.store.connection, "match-1")

        self.assertEqual([point["observed_at"] for point in timeline], [first.isoformat(), second.isoformat()])
        self.assertEqual(timeline[0]["probabilities"], {"team_one": 0.5, "team_two": 0.5})
        self.assertAlmostEqual(timeline[1]["probabilities"]["team_one"], 0.25)
        self.assertAlmostEqual(timeline[1]["probabilities"]["team_two"], 0.75)

    def test_winner_timeline_does_not_pair_different_market_groups(self) -> None:
        self.add_match()
        for odds_id, group_id, side in (
            ("group-a-one", "group-a", "team_one"),
            ("group-b-two", "group-b", "team_two"),
        ):
            self.store.insert_odds(
                OddsSnapshot(
                    "match-1",
                    odds_id,
                    group_id,
                    NOW,
                    2.0,
                    1,
                    Market("winner", "map_1", side, None, side, True),
                )
            )
        self.store.connection.commit()

        self.assertEqual(winner_timeline(self.store.connection, "match-1"), [])

    def test_cursor_changes_only_after_monitor_data_changes(self) -> None:
        self.add_match()
        before = monitor_cursor(self.store.connection)
        self.assertEqual(before, monitor_cursor(self.store.connection))

        self.add_winner_pair(NOW, 2.0, 2.0)

        self.assertNotEqual(before, monitor_cursor(self.store.connection))

    def test_cursor_changes_for_an_unchanged_complete_transport(self) -> None:
        self.add_match()
        first = NOW - timedelta(seconds=6)
        self.add_winner_response(first, 2.0, 2.0, observation_key="response-1")
        before = monitor_cursor(self.store.connection)
        snapshot_count = self.store.connection.execute(
            "SELECT COUNT(*) FROM odds_snapshots WHERE raybet_match_id='match-1'"
        ).fetchone()[0]

        self.add_winner_response(NOW, 2.0, 2.0, observation_key="response-2")

        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM odds_snapshots WHERE raybet_match_id='match-1'"
            ).fetchone()[0],
            snapshot_count,
        )
        self.assertNotEqual(before, monitor_cursor(self.store.connection))

    def test_cursor_changes_for_vision_invalidation_and_confirmed_downgrade(self) -> None:
        observation = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=NOW - timedelta(seconds=5),
            game_clock_seconds=120,
            radiant_team_side=None,
            clock_confidence=0.99,
            draft_confidence=0.99,
            label="cursor-invalidated-frame",
        )
        self.store.insert_vision_observation(observation)
        self.store.connection.commit()
        before = monitor_cursor(self.store.connection)

        self.store.connection.execute(
            """INSERT INTO vision_observation_invalidations
               (raybet_match_id, captured_at, source_frame_ref,
                invalidated_at, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (
                observation.raybet_match_id,
                observation.captured_at.isoformat(),
                observation.source_frame_ref,
                NOW.isoformat(),
                "cursor_regression",
            ),
        )
        self.store.connection.execute(
            """UPDATE vision_observations SET confirmed=0
                WHERE raybet_match_id=? AND captured_at=? AND source_frame_ref=?""",
            (
                observation.raybet_match_id,
                observation.captured_at.isoformat(),
                observation.source_frame_ref,
            ),
        )
        self.store.connection.commit()

        after = monitor_cursor(self.store.connection)
        self.assertNotEqual(before, after)
        self.assertEqual(after, monitor_cursor(self.store.connection))

    def test_cursor_changes_for_draft_and_derived_invalidations(self) -> None:
        observation = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=NOW - timedelta(seconds=5),
            game_clock_seconds=120,
            radiant_team_side=None,
            clock_confidence=0.99,
            draft_confidence=0.99,
            label="cursor-conflict-frame",
        )
        self.store.insert_vision_observation(observation)
        self.store.connection.commit()
        before = monitor_cursor(self.store.connection)
        anchor = self.store.connection.execute(
            """SELECT draft_hash, radiant_hero_ids, dire_hero_ids
                 FROM vision_draft_anchors
                WHERE raybet_match_id=? AND map_number=?""",
            (observation.raybet_match_id, observation.map_number),
        ).fetchone()

        self.store.connection.execute(
            """INSERT INTO vision_draft_conflicts
               (raybet_match_id, map_number, captured_at, source_frame_ref,
                observed_draft_hash, radiant_hero_ids, dire_hero_ids,
                reason, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                observation.raybet_match_id,
                observation.map_number,
                NOW.isoformat(),
                "cursor-conflict",
                anchor["draft_hash"],
                anchor["radiant_hero_ids"],
                anchor["dire_hero_ids"],
                "cursor_regression",
                NOW.isoformat(),
            ),
        )
        self.store.connection.execute(
            """UPDATE vision_draft_anchors
                  SET status='conflict', conflict_at=?
                WHERE raybet_match_id=? AND map_number=?""",
            (
                NOW.isoformat(),
                observation.raybet_match_id,
                observation.map_number,
            ),
        )
        self.store.connection.commit()

        conflict_cursor = monitor_cursor(self.store.connection)
        self.assertNotEqual(before, conflict_cursor)

        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "strategy_decision",
                "cursor-decision",
                observation.raybet_match_id,
                observation.map_number,
                "cursor_regression",
                "vision_draft_conflict",
                NOW.isoformat(),
            ),
        )
        self.store.connection.commit()

        derived_cursor = monitor_cursor(self.store.connection)
        self.assertNotEqual(conflict_cursor, derived_cursor)
        self.assertEqual(derived_cursor, monitor_cursor(self.store.connection))

    def test_monitor_api_exposes_bootstrap_and_match_detail(self) -> None:
        self.add_match()
        self.add_winner_response(
            NOW, 2.0, 2.0, observation_key="monitor-api-response"
        )
        previous_path = queries.DB_PATH
        queries.init_db(str(self.database))
        try:
            with TestClient(app) as client:
                bootstrap = client.get("/api/monitor/bootstrap")
                detail = client.get("/api/monitor/matches/match-1")
        finally:
            queries.init_db(previous_path)

        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(bootstrap.json()["matches"][0]["raybet_match_id"], "match-1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["winner_timeline"]), 1)


if __name__ == "__main__":
    unittest.main()
