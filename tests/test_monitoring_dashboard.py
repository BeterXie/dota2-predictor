from __future__ import annotations

import base64
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from live_betting.engine import price_groups
from live_betting.browser_contract import canonical_json, payload_sha256
from live_betting.comeback import (
    STRATEGY_VERSION,
    _identity,
    no_signal_decision,
    score_comeback,
)
from live_betting.health import record_health
from live_betting.markets import normalized_state_hash
from live_betting.market_state import MarketSurface
from live_betting.models import Market, OddsSnapshot, RoshLineupScore
from live_betting.profiles import DraftCurve, PlayerForm, TeamStyleProfile
from live_betting.profiles.draft_curve import DraftPoint
from live_betting.shadow_monitor import _persist_decision
from live_betting.storage import LiveBettingStore
from live_betting.stratz_rosh_client import (
    ROSH_FORMULA_VERSION,
    canonical_evidence_hash,
    rosh_cache_week_start,
)
from live_betting.strict_eligibility import query_strict_mapping_snapshot
from live_betting.vision import VisionObservation
from live_betting.vision_frame_registry import (
    publish_vision_frame_bytes,
    retire_vision_frame_artifact,
)
from shared.sqlite import classify_sqlite_error
from tests.draft_authority_fixture import (
    make_test_vision_observation,
    seed_test_draft_authority,
)
from web import monitoring, queries
from web.app import app
from web.monitoring import (
    _current_winner,
    build_monitor_snapshot,
    current_markets,
    derive_health,
    monitor_match_detail,
    monitor_history_page,
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

    def test_storage_rejects_non_head_to_head_out_right(self) -> None:
        payload = {
            "id": "out-right",
            "match_name": "Champion",
            "match_short_name": "Outright",
            "tournament_name": "World Cup",
            "start_time": "2026-07-13 22:00:00",
            "round": "bo1",
            "status": 3,
            "team": [
                {"pos": position, "team_name": f"Team {position}"}
                for position in range(1, 25)
            ],
        }

        with self.assertRaisesRegex(ValueError, "raybet_non_head_to_head_match"):
            self.store.upsert_raybet_match(payload, NOW)

        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM raybet_matches WHERE raybet_match_id='out-right'"
            ).fetchone()
        )

    def test_legacy_out_right_is_excluded_from_history_and_replay(self) -> None:
        payload = {
            "id": "out-right",
            "match_short_name": "Outright",
            "team": [
                {"pos": position, "team_name": f"Team {position}"}
                for position in range(1, 25)
            ],
        }
        self.store.connection.execute(
            """INSERT INTO raybet_matches VALUES
               ('out-right', 'World Cup', 'Team 1', 'Team 2',
                '2026-07-12 22:00:00', 1, '3', NULL, ?, ?)""",
            (json.dumps(payload), (NOW - timedelta(days=1)).isoformat()),
        )
        self.store.connection.execute(
            """INSERT INTO raybet_matches VALUES
               ('marked-out-right', 'World Cup', 'Team 1', 'Team 2',
                '2026-07-12 21:00:00', 1, '3', NULL, ?, ?)""",
            (
                json.dumps({"match_short_name": "Outright"}),
                (NOW - timedelta(days=1, minutes=1)).isoformat(),
            ),
        )
        self.store.connection.commit()

        history = monitor_history_page(
            self.store.connection,
            now=NOW + timedelta(days=1),
        )

        self.assertEqual(history["items"], [])
        self.assertIsNone(
            monitor_match_detail(
                self.store.connection,
                "out-right",
                now=NOW + timedelta(days=1),
            )
        )
        self.assertIsNone(
            monitor_match_detail(
                self.store.connection,
                "marked-out-right",
                now=NOW + timedelta(days=1),
            )
        )

    def test_valid_two_team_match_remains_visible(self) -> None:
        self.add_match(status=3, scheduled_at="2026-07-12 22:00:00")

        history = monitor_history_page(
            self.store.connection,
            now=NOW + timedelta(days=1),
        )

        self.assertEqual(
            [item["raybet_match_id"] for item in history["items"]],
            ["match-1"],
        )

    def test_out_right_rows_do_not_starve_later_head_to_head_history(self) -> None:
        self.add_match(
            match_id="old-head-to-head",
            status=5,
            scheduled_at=None,
            updated_at=NOW - timedelta(days=5),
        )
        payload = json.dumps(
            {
                "match_short_name": "Outright",
                "team": [
                    {"pos": position, "team_name": f"Team {position}"}
                    for position in range(1, 25)
                ],
            }
        )
        self.store.connection.executemany(
            """INSERT INTO raybet_matches
               (raybet_match_id, tournament, team_one, team_two,
                scheduled_at, best_of, status, live_url, raw_json, updated_at)
               VALUES (?, 'World Cup', 'Team 1', 'Team 2', ?, 1, '3',
                       NULL, ?, ?)""",
            [
                (
                    f"out-right-{index:03d}",
                    (NOW - timedelta(minutes=index + 1)).isoformat(),
                    payload,
                    NOW.isoformat(),
                )
                for index in range(205)
            ],
        )
        self.store.connection.commit()

        page = monitor_history_page(self.store.connection, limit=5, now=NOW)

        self.assertEqual(
            [item["raybet_match_id"] for item in page["items"]],
            ["old-head-to-head"],
        )
        self.assertFalse(page["has_more"])

    def test_out_right_history_scan_is_bounded_and_resumable(self) -> None:
        self.add_match(
            match_id="old-head-to-head",
            status=5,
            scheduled_at=None,
            updated_at=NOW - timedelta(days=5),
        )
        payload = json.dumps(
            {
                "match_short_name": "Outright",
                "team": [
                    {"pos": position, "team_name": f"Team {position}"}
                    for position in range(1, 25)
                ],
            }
        )
        self.store.connection.executemany(
            """INSERT INTO raybet_matches
               (raybet_match_id, tournament, team_one, team_two,
                scheduled_at, best_of, status, live_url, raw_json, updated_at)
               VALUES (?, 'World Cup', 'Team 1', 'Team 2', ?, 1, '3',
                       NULL, ?, ?)""",
            [
                (
                    f"out-right-{index:04d}",
                    (NOW - timedelta(minutes=index + 1)).isoformat(),
                    payload,
                    NOW.isoformat(),
                )
                for index in range(monitoring._HISTORY_RAW_SCAN_LIMIT + 5)
            ],
        )
        self.store.connection.commit()

        first = monitor_history_page(self.store.connection, limit=5, now=NOW)
        self.assertEqual(first["items"], [])
        self.assertTrue(first["has_more"])
        self.assertIsNotNone(first["next_cursor"])

        second = monitor_history_page(
            self.store.connection,
            cursor=first["next_cursor"],
            limit=5,
            now=NOW + timedelta(days=30),
        )
        self.assertEqual(
            [item["raybet_match_id"] for item in second["items"]],
            ["old-head-to-head"],
        )
        self.assertFalse(second["has_more"])

    def add_frame_observation(
        self,
        *,
        match_id: str = "match-1",
        captured_at: datetime,
        label: str,
        game_clock_seconds: int = 120,
    ):
        encoded = b"\xff\xd8\xff\xe0" + label.encode("ascii") + b"\xff\xd9"
        receipt = publish_vision_frame_bytes(
            Path(self.directory.name) / "vision-frames",
            encoded,
        )
        observation = VisionObservation(
            raybet_match_id=match_id,
            map_number=1,
            captured_at=captured_at,
            game_clock_seconds=game_clock_seconds,
            is_paused=False,
            radiant_hero_ids=(1, 2, 3, 4, 5),
            dire_hero_ids=(6, 7, 8, 9, 10),
            clock_confidence=0.99,
            draft_confidence=0.99,
            source_frame_ref=receipt.frame_ref,
            screen_state="game",
            radiant_team_side="team_one",
            source_frame_sha256=receipt.content_sha256,
            source_frame_bytes=receipt.byte_length,
            source_frame_path=str(receipt.storage_path),
        )
        self.store.insert_vision_observation(observation)
        self.store.connection.commit()
        return observation, receipt, encoded

    def get_frame(self, match_id: str, digest: str):
        previous_path = queries.DB_PATH
        queries.init_db(str(self.database))
        try:
            with TestClient(app) as client:
                return client.get(
                    f"/api/monitor/matches/{match_id}/vision-frames/{digest}.jpg"
                )
        finally:
            queries.init_db(previous_path)

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
        label: str,
        decided_at: datetime,
        team_style: float,
        *,
        map_number: int = 1,
    ) -> str:
        mapping_id = self.ensure_strict_mapping()
        mapping_snapshot = query_strict_mapping_snapshot(
            self.store.connection,
            mapping_id=mapping_id,
            observed_at=decided_at,
        )
        self.assertTrue(mapping_snapshot.eligible)
        assert mapping_snapshot.mapping is not None
        draft_authority = seed_test_draft_authority(
            self.store.connection,
            raybet_match_id="match-1",
            map_number=map_number,
            strict_mapping_id=mapping_id,
            observed_at=decided_at,
            label=f"monitor:{label}",
        )
        vision = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=map_number,
            captured_at=decided_at,
            label=f"monitor-frame:{label}",
        )
        self.store.insert_vision_observation(vision)
        rows = self.add_winner_response(
            decided_at,
            2.5,
            5.0 / 3.0,
            observation_key=f"monitor-transport:{label}",
            period=f"map_{map_number}",
        )
        market_probability = price_groups(rows)[rows[0].odds_id]
        bounded_market = min(1.0 - 1e-6, max(1e-6, market_probability))
        market_logit = math.log(bounded_market / (1.0 - bounded_market))
        raw_contributions = {
            "team_style": team_style,
            "player_form": -0.02,
            "draft_curve": 0.08,
            "late_game_style": 0.0,
            "market_movement": 0.01,
        }
        model_probability = 1.0 / (
            1.0
            + math.exp(
                -(market_logit + math.fsum(raw_contributions.values()))
            )
        )
        conservative_contributions = {
            "team_style": (
                raw_contributions["team_style"] * 0.8
                if raw_contributions["team_style"] > 0.0
                else raw_contributions["team_style"]
            ),
            "player_form": -0.02,
            "draft_curve": 0.04,
            "late_game_style": 0.0,
            "market_movement": 0.01,
        }
        conservative_probability = 1.0 / (
            1.0
            + math.exp(
                -(
                    market_logit
                    + math.fsum(conservative_contributions.values())
                )
            )
        )
        independent_positive = (
            raw_contributions["team_style"]
            + raw_contributions["late_game_style"]
            > 0.0
            or raw_contributions["player_form"] > 0.0
            or raw_contributions["draft_curve"] > 0.0
        )
        inputs = {
            "draft_authority": asdict(draft_authority),
            "strict_live_eligibility": {
                "mapping_refs": mapping_snapshot.mapping.input_refs()
            },
            "transport": {
                "current_key": f"monitor-transport:{label}",
                "current_at": decided_at.isoformat(),
            },
            "vision": {
                "captured_at": vision.captured_at.isoformat(),
                "source_frame_ref": vision.source_frame_ref,
                "game_clock_seconds": vision.game_clock_seconds,
                "radiant_team_side": vision.radiant_team_side,
            },
            "conservative_contributions": conservative_contributions,
            "conservative_probability": conservative_probability,
            "independent_positive": independent_positive,
        }
        decision_key, input_ref = _identity(
            observation=vision,
            decided_at=decided_at,
            underdog_side="team_one",
            model_probability=model_probability,
            reason="eligible",
            inputs=inputs,
        )
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
                **raw_contributions,
                "__conservative__": conservative_contributions,
                "__inputs__": inputs,
            },
            input_ref=input_ref,
            strategy_version=STRATEGY_VERSION,
        )
        self.assertTrue(
            self.store.insert_decision(
                decision,
                draft_authority=draft_authority,
                vision_observation=vision,
                vision_transport_key=f"monitor-transport:{label}",
            )
        )
        self.assertEqual(len(decision_key), 32)
        self.assertEqual(len(input_ref), 24)
        return decision_key

    def add_no_signal_decision(
        self,
        label: str,
        decided_at: datetime,
        *,
        reason: str = "strict_live_ineligible:mapping_missing",
    ) -> str:
        vision = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=decided_at,
            game_clock_seconds=590,
            label=f"monitor-no-signal:{label}",
        )
        self.store.insert_vision_observation(vision)
        surface = MarketSurface(
            underdog_side="team_one",
            underdog_price=2.5,
            underdog_probability=0.4,
            probability_move=0.0,
            kill_handicap=None,
            total_kills=None,
            duration_minutes=None,
            quality=0.5,
            missing_markets=("kill_handicap",),
        )
        decision = no_signal_decision(
            observation=vision,
            surface=surface,
            decided_at=decided_at,
            reason=reason,
        )
        self.assertTrue(_persist_decision(self.store, decision))
        self.assertRegex(decision.decision_key, r"^[0-9a-f]{32}$")
        self.assertRegex(decision.input_ref, r"^[0-9a-f]{24}$")
        return decision.decision_key

    def build_scored_blocked_decision(
        self,
        label: str,
        decided_at: datetime,
    ):
        mapping_id = self.ensure_strict_mapping()
        mapping_snapshot = query_strict_mapping_snapshot(
            self.store.connection,
            mapping_id=mapping_id,
            observed_at=decided_at,
        )
        self.assertTrue(mapping_snapshot.eligible)
        assert mapping_snapshot.mapping is not None
        draft_authority = seed_test_draft_authority(
            self.store.connection,
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=mapping_id,
            observed_at=decided_at,
            label=f"monitor-scored-blocked:{label}",
        )
        vision = make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=decided_at,
            game_clock_seconds=600,
            label=f"monitor-scored-blocked-frame:{label}",
        )
        self.store.insert_vision_observation(vision)
        transport_key = f"monitor-scored-blocked-transport:{label}"
        self.add_winner_response(
            decided_at,
            2.5,
            5.0 / 3.0,
            observation_key=transport_key,
        )
        surface = MarketSurface(
            underdog_side="team_one",
            underdog_price=2.5,
            underdog_probability=0.4,
            probability_move=0.0,
            kill_handicap=-3.5,
            total_kills=48.5,
            duration_minutes=36.5,
            quality=1.0,
        )
        point = DraftPoint(
            minute=draft_authority.horizon_minutes,
            radiant_probability=draft_authority.radiant_probability,
            scaling_edge=0.0,
            synergy_edge=0.0,
            quality=draft_authority.quality,
            validated=True,
            support=draft_authority.support,
            calibration_ref=draft_authority.global_gate_ref,
            input_refs=("test",),
            uncertainty=draft_authority.uncertainty,
            feature_hash=draft_authority.feature_hash,
            model_hash=draft_authority.model_hash,
            calibration_hash=draft_authority.calibration_hash,
            global_calibration_passed=True,
            global_gate_ref=draft_authority.global_gate_ref,
            model_version=draft_authority.model_version,
            model_kind="pure_draft",
            availability_mode="prospective",
            input_snapshot_hash=draft_authority.input_snapshot_hash,
            landmark_key=draft_authority.landmark_key,
            curve_key=draft_authority.curve_key,
            deployment_key=draft_authority.deployment_key,
            target_snapshot_hash=draft_authority.target_snapshot_hash,
        )
        draft_curve = DraftCurve(
            points=(point,),
            source_ref=draft_authority.source_ref,
            authority_revision=draft_authority.authority_revision,
            dependency_revision=draft_authority.dependency_revision,
            curve_key=draft_authority.curve_key,
            deployment_key=draft_authority.deployment_key,
            target_snapshot_hash=draft_authority.target_snapshot_hash,
            strict_mapping_id=draft_authority.strict_mapping_id,
        )
        draft_hash = hashlib.sha256(
            json.dumps(
                {
                    "radiant": list(vision.radiant_hero_ids),
                    "dire": list(vision.dire_hero_ids),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        decision = score_comeback(
            observation=vision,
            surface=surface,
            underdog_style=TeamStyleProfile(
                11, 100, 0.28, 0.16, 0.84, 0.4, 38.0, 0.8
            ),
            favorite_style=TeamStyleProfile(
                22, 100, 0.18, 0.2, 0.8, 0.35, 36.0, 0.8
            ),
            underdog_form=PlayerForm((1, 2, 3, 4, 5), 0.2, {}, 50, 0.8),
            favorite_form=PlayerForm((6, 7, 8, 9, 10), 0.0, {}, 50, 0.8),
            draft_curve=draft_curve,
            decided_at=decided_at,
            stable=True,
            min_edge=0.99,
            rosh_lineup_score=RoshLineupScore(
                score_key="a" * 64,
                draft_hash=draft_hash,
                player_identity_hash="c" * 64,
                pure_lineup_score=(draft_authority.radiant_probability * 100) - 50,
                player_adjusted_lineup_score=None,
                effective_lineup_score=(
                    draft_authority.radiant_probability * 100
                ) - 50,
                scoring_mode="pure",
                player_coverage_count=0,
                stake_multiplier=0.5,
                formula_version=ROSH_FORMULA_VERSION,
                source_name="stratz",
                source_week=1_773_619_200,
                cache_week_start=1_773_619_200,
                source_as_of=decided_at,
                evidence_hash="b" * 64,
                evidence={
                    "pure_minute_table": [
                        {
                            "minute": 20,
                            "win_rate_graph": (
                                draft_authority.radiant_probability * 100
                            ) - 50,
                            "match_percentage": 100.0,
                        }
                    ],
                    "minute_table": [],
                },
            ),
            input_refs={
                "draft_authority": asdict(draft_authority),
                "strict_live_eligibility": {
                    "mapping_refs": mapping_snapshot.mapping.input_refs()
                },
                "transport": {
                    "current_key": transport_key,
                    "current_at": decided_at.isoformat(),
                },
            },
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "edge_below_threshold")
        return decision, draft_authority, vision, transport_key

    def add_scored_blocked_decision(
        self,
        label: str,
        decided_at: datetime,
    ) -> str:
        decision, draft_authority, vision, transport_key = (
            self.build_scored_blocked_decision(label, decided_at)
        )
        self.assertTrue(
            _persist_decision(
                self.store,
                decision,
                draft_authority=draft_authority,
                vision_observation=vision,
                vision_transport_key=transport_key,
            )
        )
        return decision.decision_key

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
                "id": "42", "game_id": 151, "status": 2,
                "team": [
                    {"pos": 1, "team_name": "One"},
                    {"pos": 2, "team_name": "Two"},
                ],
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
            {
                "id": "42", "game_id": 151, "status": 2,
                "team": [
                    {"pos": 1, "team_name": "One"},
                    {"pos": 2, "team_name": "Two"},
                ],
            },
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
        older_key = self.add_decision(
            "older-valid", NOW - timedelta(seconds=20), 0.55
        )
        newer_key = self.add_decision(
            "newer-impacted", NOW - timedelta(seconds=5), 0.75
        )
        older_probability = self.store.connection.execute(
            "SELECT model_probability FROM strategy_decisions WHERE decision_key=?",
            (older_key,),
        ).fetchone()[0]
        before = monitor_cursor(self.store.connection)
        self.store.connection.execute("PRAGMA foreign_keys=OFF")
        self.store.connection.execute(
            """INSERT INTO strict_live_mapping_impacts
               (mapping_id, invalidation_id, dependent_type, dependent_key,
                reason, recorded_at)
               VALUES (1, 1, 'strategy_decision', ?,
                       'mapping_invalidated', ?)""",
            (newer_key, NOW.isoformat()),
        )
        self.store.connection.commit()
        self.store.connection.execute("PRAGMA foreign_keys=ON")

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        self.assertNotEqual(before, monitor_cursor(self.store.connection))
        self.assertEqual(
            snapshot["matches"][0]["latest_decision"]["model_probability"],
            older_probability,
        )
        assert detail is not None
        self.assertEqual(
            [decision["decision_key"] for decision in detail["decisions"]],
            [older_key],
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
        older_key = self.add_decision(
            "older-valid", NOW - timedelta(seconds=30), 0.55
        )
        invalidated_key = self.add_decision(
            "vision-invalidated", NOW - timedelta(seconds=20), 0.65
        )
        self.add_decision("draft-conflicted", NOW - timedelta(seconds=5), 0.75)
        older_probability = self.store.connection.execute(
            "SELECT model_probability FROM strategy_decisions WHERE decision_key=?",
            (older_key,),
        ).fetchone()[0]
        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES ('strategy_decision', ?, 'match-1', 1,
                       'bad_frame', 'vision_observation_invalidated', ?)""",
            (invalidated_key, NOW.isoformat()),
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
            older_probability,
        )
        assert detail is not None
        self.assertEqual(
            [decision["decision_key"] for decision in detail["decisions"]],
            [older_key],
        )

    def test_vision_views_exclude_only_the_invalidated_observation(self) -> None:
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

        latest = build_monitor_snapshot(
            self.store.connection,
            now=NOW,
        )["matches"][0]["latest_vision"]
        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        self.assertEqual(latest["observed_at"], older.captured_at.isoformat())
        assert detail is not None
        self.assertEqual(
            [point["source_frame_ref"] for point in detail["vision"]],
            [older.source_frame_ref],
        )
        invalidated_response = self.get_frame(
            "match-1",
            str(invalidated.source_frame_sha256),
        )
        self.assertEqual(invalidated_response.status_code, 404)

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

    def test_vision_views_exclude_observations_at_or_after_draft_conflict(
        self,
    ) -> None:
        self.add_match(status=2)
        before, before_receipt, _ = self.add_frame_observation(
            captured_at=NOW - timedelta(seconds=20),
            label="before-conflict",
            game_clock_seconds=100,
        )
        after, after_receipt, _ = self.add_frame_observation(
            captured_at=NOW - timedelta(seconds=5),
            label="after-conflict",
            game_clock_seconds=115,
        )
        conflict_at = NOW - timedelta(seconds=10)
        self.store.connection.execute(
            """UPDATE vision_draft_anchors
                  SET status='conflict', conflict_at=?
                WHERE raybet_match_id='match-1' AND map_number=1""",
            (conflict_at.isoformat(),),
        )
        self.store.connection.execute(
            """INSERT INTO vision_draft_conflicts
               (raybet_match_id, map_number, captured_at, source_frame_ref,
                observed_draft_hash, radiant_hero_ids, dire_hero_ids,
                observed_radiant_team_side, reason, recorded_at)
               VALUES ('match-1', 1, ?, ?, ?, '[1,2,3,4,5]',
                       '[6,7,8,9,10]', 'team_two', 'confirmed_draft_conflict', ?)""",
            (conflict_at.isoformat(), after.source_frame_ref, "f" * 64, NOW.isoformat()),
        )
        self.store.connection.commit()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        self.assertEqual(
            snapshot["matches"][0]["latest_vision"]["source_frame_ref"],
            before.source_frame_ref,
        )
        assert detail is not None
        self.assertEqual(
            [point["source_frame_ref"] for point in detail["vision"]],
            [before.source_frame_ref],
        )
        before_response = self.get_frame("match-1", before_receipt.content_sha256)
        after_response = self.get_frame("match-1", after_receipt.content_sha256)
        self.assertEqual(before_response.status_code, 200)
        self.assertEqual(after_response.status_code, 404)

    def test_vision_timeline_keeps_latest_bounded_points_in_ascending_order(
        self,
    ) -> None:
        self.add_match(status=2)
        observations = [
            self.add_frame_observation(
                captured_at=NOW - timedelta(seconds=seconds),
                label=f"bounded-{seconds}",
                game_clock_seconds=120 - seconds,
            )[0]
            for seconds in (40, 30, 20, 10)
        ]

        detail = monitor_match_detail(
            self.store.connection,
            "match-1",
            now=NOW,
            max_points=2,
        )

        assert detail is not None
        self.assertEqual(
            [point["source_frame_ref"] for point in detail["vision"]],
            [observations[2].source_frame_ref, observations[3].source_frame_ref],
        )

    def test_vision_frame_endpoint_returns_exact_registered_jpeg(self) -> None:
        self.add_match(status=2)
        observation, receipt, encoded = self.add_frame_observation(
            captured_at=NOW - timedelta(seconds=5),
            label="served-frame",
        )
        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        point = detail["vision"][-1]
        self.assertEqual(point["frame_digest"], receipt.content_sha256)
        self.assertEqual(
            point["frame_url"],
            f"/api/monitor/matches/match-1/vision-frames/{receipt.content_sha256}.jpg",
        )
        self.assertEqual(point["source_frame_ref"], observation.source_frame_ref)

        response = self.get_frame("match-1", receipt.content_sha256)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, encoded)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["etag"], f'"{receipt.content_sha256}"')
        self.assertIn("immutable", response.headers["cache-control"])

    def test_vision_frame_endpoint_rejects_cross_match_reference(self) -> None:
        self.add_match(status=2)
        self.add_match(match_id="match-2", status=2)
        _, receipt, _ = self.add_frame_observation(
            captured_at=NOW - timedelta(seconds=5),
            label="match-one-only",
        )

        response = self.get_frame("match-2", receipt.content_sha256)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(receipt.storage_path), response.text)

    def test_vision_frame_endpoint_rejects_retired_and_missing_files(self) -> None:
        self.add_match(status=2)
        _, retired, _ = self.add_frame_observation(
            captured_at=NOW - timedelta(seconds=10),
            label="retired-frame",
        )
        _, missing, _ = self.add_frame_observation(
            captured_at=NOW - timedelta(seconds=5),
            label="missing-frame",
        )
        retire_vision_frame_artifact(
            self.store.connection,
            retired.frame_ref,
            reason="test retirement",
            actor="test_monitoring_dashboard",
            retired_at=NOW,
        )
        missing.storage_path.unlink()

        retired_response = self.get_frame("match-1", retired.content_sha256)
        missing_response = self.get_frame("match-1", missing.content_sha256)

        self.assertIn(retired_response.status_code, {404, 409})
        self.assertIn(missing_response.status_code, {404, 409})
        self.assertNotIn(str(retired.storage_path), retired_response.text)
        self.assertNotIn(str(missing.storage_path), missing_response.text)

    def test_vision_frame_endpoint_rejects_tamper_and_hardlink(self) -> None:
        self.add_match(status=2)
        _, tampered, _ = self.add_frame_observation(
            captured_at=NOW - timedelta(seconds=10),
            label="tampered-frame",
        )
        _, linked, _ = self.add_frame_observation(
            captured_at=NOW - timedelta(seconds=5),
            label="hardlinked-frame",
        )
        tampered.storage_path.write_bytes(b"\xff\xd8tampered\xff\xd9")
        os.link(linked.storage_path, linked.storage_path.with_suffix(".link.jpg"))

        tampered_response = self.get_frame("match-1", tampered.content_sha256)
        linked_response = self.get_frame("match-1", linked.content_sha256)

        self.assertEqual(tampered_response.status_code, 409)
        self.assertEqual(linked_response.status_code, 409)
        self.assertNotIn(str(tampered.storage_path), tampered_response.text)
        self.assertNotIn(str(linked.storage_path), linked_response.text)

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
        self.assertEqual(
            monitor_history_page(
                self.store.connection,
                limit=10,
                now=NOW,
            )["items"],
            [],
        )

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

    def test_realtime_snapshot_bounds_history_projection_and_keeps_live_priority(self) -> None:
        history_rows = [
            (
                f"history-{index:04d}",
                "Archive Cup",
                "One",
                "Two",
                (NOW - timedelta(days=index + 2)).isoformat(),
                3,
                "5",
                None,
                "{}",
                (NOW - timedelta(days=index + 1)).isoformat(),
            )
            for index in range(500)
        ]
        self.store.connection.executemany(
            """INSERT INTO raybet_matches
               (raybet_match_id, tournament, team_one, team_two,
                scheduled_at, best_of, status, live_url, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            history_rows,
        )
        self.add_match(
            match_id="live-priority",
            status=2,
            scheduled_at="2026-07-14 21:30:00",
            updated_at=NOW,
        )

        with patch(
            "web.monitoring._monitor_match",
            wraps=monitoring._monitor_match,
        ) as projected:
            snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertLessEqual(projected.call_count, 128)
        self.assertLessEqual(len(snapshot["matches"]), 64)
        self.assertIn(
            "live-priority",
            [match["raybet_match_id"] for match in snapshot["matches"]],
        )

    def test_history_keyset_pages_are_stable_unique_and_include_null_schedule(self) -> None:
        rows = []
        for index in range(37):
            rows.append(
                (
                    f"ended-{index:02d}",
                    "History Cup",
                    "One",
                    "Two",
                    None if index == 0 else (NOW - timedelta(days=index)).isoformat(),
                    3,
                    "5",
                    None,
                    "{}",
                    (NOW - timedelta(days=index)).isoformat(),
                )
            )
        self.store.connection.executemany(
            """INSERT INTO raybet_matches
               (raybet_match_id, tournament, team_one, team_two,
                scheduled_at, best_of, status, live_url, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.store.connection.commit()

        seen: list[str] = []
        cursor = None
        while True:
            page = monitor_history_page(
                self.store.connection,
                cursor=cursor,
                limit=7,
                now=NOW,
            )
            seen.extend(item["raybet_match_id"] for item in page["items"])
            if not page["has_more"]:
                break
            self.assertIsNotNone(page["next_cursor"])
            self.assertNotEqual(cursor, page["next_cursor"])
            cursor = page["next_cursor"]

        self.assertEqual(len(seen), 37)
        self.assertEqual(len(set(seen)), 37)
        self.assertIn("ended-00", seen)

    def test_history_cursor_fixes_cutoff_and_rejects_tampering(self) -> None:
        self.add_match(
            match_id="older-ended",
            status=5,
            scheduled_at=None,
            updated_at=NOW - timedelta(days=2),
        )
        self.add_match(
            match_id="clock-boundary",
            status=2,
            scheduled_at="2026-07-13 20:00:00",
            updated_at=NOW - timedelta(minutes=10),
        )
        self.add_match(
            match_id="newer-ended",
            status=5,
            scheduled_at=None,
            updated_at=NOW - timedelta(days=1),
        )

        first = monitor_history_page(self.store.connection, limit=1, now=NOW)
        cursor = first["next_cursor"]
        self.assertTrue(first["has_more"])
        self.assertIsNotNone(cursor)
        second = monitor_history_page(
            self.store.connection,
            cursor=cursor,
            limit=1,
            now=NOW + timedelta(hours=1),
        )
        self.assertEqual(
            [item["raybet_match_id"] for item in second["items"]],
            ["older-ended"],
        )
        tampered = f"{cursor[:-1]}{'0' if cursor[-1] != '0' else '1'}"
        with self.assertRaisesRegex(ValueError, "invalid history cursor"):
            monitor_history_page(
                self.store.connection,
                cursor=tampered,
                limit=1,
            )
        encoded = cursor.split(".", 1)[0]
        padding = "=" * (-len(encoded) % 4)
        body = base64.urlsafe_b64decode(encoded + padding)
        public_checksum = hashlib.sha256(
            monitoring._HISTORY_CURSOR_DOMAIN + body
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "invalid history cursor"):
            monitor_history_page(
                self.store.connection,
                cursor=f"{encoded}.{public_checksum}",
                limit=1,
            )
        self.store.connection.execute(
            """UPDATE raybet_matches SET updated_at=?
                WHERE raybet_match_id='newer-ended'""",
            (NOW.isoformat(),),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, "history cursor anchor changed"):
            monitor_history_page(
                self.store.connection,
                cursor=cursor,
                limit=1,
            )

    def test_empty_history_window_advances_cursor_without_unbounded_scan(self) -> None:
        self.add_match(
            match_id="old-ended",
            status=5,
            scheduled_at=None,
            updated_at=NOW - timedelta(days=5),
        )
        self.store.connection.executemany(
            """INSERT INTO raybet_matches
               (raybet_match_id, tournament, team_one, team_two,
                scheduled_at, best_of, status, live_url, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    f"screened-live-{index:03d}",
                    "Live Cup",
                    "One",
                    "Two",
                    (NOW - timedelta(minutes=index + 1)).isoformat(),
                    3,
                    "2",
                    None,
                    "{}",
                    NOW.isoformat(),
                )
                for index in range(205)
            ],
        )
        self.store.connection.commit()

        with patch(
            "web.monitoring._monitor_match",
            wraps=monitoring._monitor_match,
        ) as projected:
            first = monitor_history_page(self.store.connection, limit=5, now=NOW)
            self.assertEqual(first["items"], [])
            self.assertTrue(first["has_more"])
            self.assertIsNotNone(first["next_cursor"])
            self.assertEqual(projected.call_count, 200)
            first_projection_count = projected.call_count
            second = monitor_history_page(
                self.store.connection,
                cursor=first["next_cursor"],
                limit=5,
                now=NOW + timedelta(days=30),
            )
            self.assertEqual(projected.call_count - first_projection_count, 6)
        self.assertEqual(
            [item["raybet_match_id"] for item in second["items"]],
            ["old-ended"],
        )

    def test_recent_timeline_ended_match_is_not_buried_by_future_rows(self) -> None:
        self.add_match(
            match_id="old-row-recent-event",
            status=1,
            scheduled_at=(NOW - timedelta(days=2)).isoformat(),
            updated_at=NOW - timedelta(days=2),
        )
        future_rows = [
            (
                f"future-event-{index:04d}",
                "Future Cup",
                "One",
                "Two",
                (NOW + timedelta(days=1, minutes=index)).isoformat(),
                3,
                "1",
                None,
                "{}",
                NOW.isoformat(),
            )
            for index in range(500)
        ]
        self.store.connection.executemany(
            """INSERT INTO raybet_matches
               (raybet_match_id, tournament, team_one, team_two,
                scheduled_at, best_of, status, live_url, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            future_rows,
        )
        self.add_match(
            match_id="old-row-recent-event",
            status=5,
            scheduled_at=(NOW - timedelta(hours=1)).isoformat(),
            updated_at=NOW,
        )

        page = monitor_history_page(
            self.store.connection,
            limit=20,
            now=NOW,
        )

        self.assertEqual(
            [item["raybet_match_id"] for item in page["items"]],
            ["old-row-recent-event"],
        )
        self.assertFalse(page["has_more"])

    def test_activity_projection_prevents_noisy_match_starvation_and_duplicate_rows(self) -> None:
        for match_id in ("quiet", "noisy"):
            self.add_match(
                match_id=match_id,
                status=2,
                scheduled_at="2026-07-13 20:00:00",
                updated_at=NOW - timedelta(days=1),
            )
        market = Market("winner", "map_1", "team_one", None, "team_one", True)
        quiet_at = NOW - timedelta(minutes=10)
        self.add_winner_response(
            quiet_at,
            2.0,
            2.0,
            match_id="quiet",
            observation_key="quiet-response",
        )
        for index in range(300):
            self.store.insert_odds(
                OddsSnapshot(
                    "noisy",
                    f"noisy-{index}",
                    "noisy-group",
                    NOW - timedelta(minutes=5) + timedelta(microseconds=index),
                    2.0,
                    1,
                    market,
                )
            )
        self.store.connection.commit()

        activity = self.store.connection.execute(
            """SELECT raybet_match_id, latest_odds_activity_at
                 FROM raybet_match_odds_activity ORDER BY raybet_match_id"""
        ).fetchall()
        self.assertEqual([row["raybet_match_id"] for row in activity], ["noisy", "quiet"])
        self.assertEqual(activity[1]["latest_odds_activity_at"], quiet_at.isoformat())
        direct_latest = max(
            self.store.connection.execute(
                """SELECT MAX(observed_at) FROM odds_transport_observations
                    WHERE raybet_match_id='quiet'"""
            ).fetchone()[0],
            self.store.connection.execute(
                """SELECT MAX(received_at) FROM odds_snapshots
                    WHERE raybet_match_id='quiet'"""
            ).fetchone()[0],
        )
        self.assertEqual(activity[1]["latest_odds_activity_at"], direct_latest)
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM raybet_match_odds_activity
                    WHERE raybet_match_id='quiet'"""
            ).fetchone()[0],
            1,
        )
        candidates = monitoring._realtime_match_candidates(
            self.store.connection,
            NOW,
        )
        self.assertTrue(
            {"quiet", "noisy"}.issubset(
                {str(row["raybet_match_id"]) for row in candidates}
            )
        )

    def test_realtime_candidate_queries_use_v9_indexes_without_temp_sort(self) -> None:
        checks = (
            (
                """SELECT raybet_match_id FROM raybet_matches
                    WHERE status='2' AND updated_at>=? AND updated_at<=?
                    ORDER BY updated_at DESC, raybet_match_id DESC LIMIT 64""",
                (
                    (NOW - timedelta(seconds=90)).isoformat(),
                    NOW.isoformat(),
                ),
                "idx_raybet_matches_status_updated",
            ),
            (
                """SELECT raybet_match_id FROM raybet_matches
                    WHERE updated_at>=? AND updated_at<=?
                    ORDER BY updated_at DESC, raybet_match_id DESC LIMIT 64""",
                (
                    (NOW - timedelta(hours=24)).isoformat(),
                    NOW.isoformat(),
                ),
                "idx_raybet_matches_updated",
            ),
            (
                f"""SELECT raybet_match_id FROM raybet_matches
                    WHERE {monitoring._SCHEDULE_UTC_JULIANDAY}>julianday(?)
                    ORDER BY {monitoring._SCHEDULE_UTC_JULIANDAY},
                             raybet_match_id LIMIT 64""",
                ((NOW + timedelta(minutes=15)).isoformat(),),
                "idx_raybet_matches_schedule_utc",
            ),
            (
                f"""SELECT raybet_match_id FROM raybet_matches
                    INDEXED BY idx_raybet_matches_ended_schedule_review
                    WHERE {monitoring._ENDED_STATUS_SQL}
                      AND scheduled_at IS NOT NULL
                      AND ({monitoring._SCHEDULE_UTC_JULIANDAY})>julianday(?)
                    ORDER BY ({monitoring._SCHEDULE_UTC_JULIANDAY}),
                             updated_at DESC, raybet_match_id DESC LIMIT 64""",
                (NOW.isoformat(),),
                "idx_raybet_matches_ended_schedule_review",
            ),
            (
                f"""SELECT raybet_match_id FROM raybet_matches
                    INDEXED BY idx_raybet_matches_ended_schedule_review
                    WHERE {monitoring._ENDED_STATUS_SQL}
                      AND scheduled_at IS NOT NULL
                      AND ({monitoring._SCHEDULE_UTC_JULIANDAY}) IS NULL
                      AND updated_at<=?
                    ORDER BY updated_at DESC, raybet_match_id DESC LIMIT 64""",
                (NOW.isoformat(),),
                "idx_raybet_matches_ended_schedule_review",
            ),
            (
                f"""SELECT raybet_match_id FROM raybet_matches
                    WHERE {monitoring._TIMELINE_KEY_SQL}<=
                          CAST(julianday(?) * 86400000 AS INTEGER)
                    ORDER BY {monitoring._TIMELINE_KEY_SQL} DESC,
                             raybet_match_id DESC LIMIT 201""",
                (NOW.isoformat(),),
                "idx_raybet_matches_timeline",
            ),
            (
                """SELECT raybet_match_id FROM vision_observations
                    WHERE confirmed=1 AND screen_state='game'
                      AND captured_at>=? AND captured_at<=?
                    ORDER BY captured_at DESC, raybet_match_id DESC LIMIT 256""",
                (
                    (NOW - timedelta(seconds=120)).isoformat(),
                    NOW.isoformat(),
                ),
                "idx_vision_confirmed_game_captured",
            ),
            (
                """SELECT raybet_match_id FROM raybet_match_odds_activity
                    WHERE latest_odds_activity_at>=?
                      AND latest_odds_activity_at<=?
                    ORDER BY latest_odds_activity_at DESC,
                             raybet_match_id DESC LIMIT 64""",
                (
                    (NOW - timedelta(minutes=15)).isoformat(),
                    NOW.isoformat(),
                ),
                "idx_raybet_match_odds_activity_time",
            ),
        )
        for sql, params, expected_index in checks:
            with self.subTest(index=expected_index):
                details = [
                    str(row[3])
                    for row in self.store.connection.execute(
                        f"EXPLAIN QUERY PLAN {sql}",
                        params,
                    )
                ]
                self.assertTrue(
                    any(expected_index in detail for detail in details),
                    details,
                )
                self.assertFalse(
                    any("USE TEMP B-TREE" in detail for detail in details),
                    details,
                )

    def test_realtime_bucket_sql_limits_raw_index_windows_before_projection(self) -> None:
        rows = []
        for index in range(80):
            rows.extend(
                (
                    (
                        f"future-poison-{index:03d}",
                        "Poison Cup",
                        "One",
                        "Two",
                        (NOW + timedelta(hours=1, minutes=index)).isoformat(),
                        3,
                        "5",
                        None,
                        "{}",
                        (NOW - timedelta(minutes=index)).isoformat(),
                    ),
                    (
                        f"past-poison-{index:03d}",
                        "Poison Cup",
                        "One",
                        "Two",
                        (NOW - timedelta(minutes=index + 1)).isoformat(),
                        3,
                        "2",
                        None,
                        "{}",
                        (NOW - timedelta(minutes=index)).isoformat(),
                    ),
                )
            )
        self.store.connection.executemany(
            """INSERT INTO raybet_matches
               (raybet_match_id, tournament, team_one, team_two,
                scheduled_at, best_of, status, live_url, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.store.connection.commit()

        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            candidates = monitoring._realtime_match_candidates(
                self.store.connection,
                NOW,
            )
        finally:
            self.store.connection.set_trace_callback(None)

        self.assertLessEqual(len(candidates), 128)
        normalized = [" ".join(statement.lower().split()) for statement in statements]
        upcoming = [
            statement
            for statement in normalized
            if "from raybet_matches" in statement
            and "then julianday(scheduled_at, '-8 hours')" in statement
            and ">julianday(" in statement
            and "indexed by idx_raybet_matches_ended_schedule_review" not in statement
        ]
        metadata = [
            statement
            for statement in normalized
            if "from raybet_matches where updated_at>=" in statement
        ]
        history = [
            statement
            for statement in normalized
            if "from raybet_matches where cast(coalesce(" in statement
            and "<= cast(julianday(" in statement
        ]
        ended_review = [
            statement
            for statement in normalized
            if "indexed by idx_raybet_matches_ended_schedule_review" in statement
        ]
        self.assertEqual(len(upcoming), 1, normalized)
        self.assertEqual(len(metadata), 1, normalized)
        self.assertEqual(len(history), 1, normalized)
        self.assertEqual(
            len(ended_review),
            2,
            normalized,
        )
        self.assertIn("limit 64", upcoming[0])
        self.assertIn("limit 64", metadata[0])
        self.assertIn("limit 16", history[0])
        for statement in (*upcoming, *metadata, *history, *ended_review):
            if statement in ended_review:
                self.assertIn("limit 64", statement)
            self.assertNotIn("coalesce(status", statement)
            self.assertNotIn(" not in ", statement)

    def test_future_timestamp_poison_cannot_starve_realtime_candidate_buckets(self) -> None:
        old = (NOW - timedelta(days=1)).isoformat()
        future = (NOW + timedelta(days=1)).isoformat()
        match_rows = []
        for bucket in ("provider", "metadata", "activity", "vision"):
            for index in range(70):
                match_rows.append(
                    (
                        f"{bucket}-future-{index:02d}",
                        "Poison Cup",
                        "One",
                        "Two",
                        None,
                        3,
                        "2" if bucket == "provider" else "0",
                        None,
                        "{}",
                        future if bucket in {"provider", "metadata"} else old,
                    )
                )
        for match_id, status, updated_at in (
            ("provider-real", "2", NOW.isoformat()),
            ("metadata-real", "0", NOW.isoformat()),
            ("activity-real", "0", old),
            ("vision-real", "0", old),
        ):
            match_rows.append(
                (
                    match_id,
                    "Real Cup",
                    "One",
                    "Two",
                    None,
                    3,
                    status,
                    None,
                    "{}",
                    updated_at,
                )
            )
        self.store.connection.executemany(
            """INSERT INTO raybet_matches
               (raybet_match_id, tournament, team_one, team_two,
                scheduled_at, best_of, status, live_url, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            match_rows,
        )
        self.store.connection.executemany(
            """INSERT INTO raybet_match_odds_activity
               (raybet_match_id, latest_odds_activity_at) VALUES (?, ?)""",
            [
                (f"activity-future-{index:02d}", future)
                for index in range(70)
            ] + [("activity-real", (NOW - timedelta(minutes=5)).isoformat())],
        )
        vision_rows = [
            (
                f"vision-future-{index:02d}",
                future,
                f"future-frame-{index:02d}",
            )
            for index in range(70)
        ] + [
            (
                "vision-real",
                (NOW - timedelta(seconds=10)).isoformat(),
                "real-frame",
            )
        ]
        self.store.connection.executemany(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                clock_confidence, draft_confidence, source_frame_ref,
                source_frame_sha256, source_frame_bytes, screen_state, confirmed)
               VALUES (?, 1, ?, 120, 0, '[1,2,3,4,5]', '[6,7,8,9,10]',
                       'team_one', 0.99, 0.99, ?, NULL, NULL, 'game', 1)""",
            vision_rows,
        )
        self.store.connection.commit()

        candidates = monitoring._realtime_match_candidates(
            self.store.connection,
            NOW,
        )
        candidate_ids = {str(row["raybet_match_id"]) for row in candidates}
        self.assertTrue(
            {
                "provider-real",
                "metadata-real",
                "activity-real",
                "vision-real",
            }.issubset(candidate_ids),
            candidate_ids,
        )

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

    def test_provider_status_five_with_real_raybet_schedule_is_ended(self) -> None:
        self.add_match(status=5, scheduled_at="2026-07-11 20:00:00")

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        history = monitor_history_page(self.store.connection, limit=10, now=NOW)
        schedule_key = self.store.connection.execute(
            f"""SELECT {monitoring._SCHEDULE_UTC_JULIANDAY}
                   FROM raybet_matches WHERE raybet_match_id='match-1'"""
        ).fetchone()[0]

        self.assertIsNotNone(schedule_key)
        self.assertEqual(snapshot["matches"][0]["lifecycle"], "ended")
        self.assertTrue(snapshot["matches"][0]["history_eligible"])
        self.assertEqual(snapshot["summary"]["live_view"]["total"], 0)
        self.assertEqual(snapshot["summary"]["history_view"]["total"], 1)
        self.assertEqual(
            [item["raybet_match_id"] for item in history["items"]],
            ["match-1"],
        )

    def test_future_scheduled_ended_match_stays_visible_but_out_of_history(
        self,
    ) -> None:
        self.add_match(
            status=5,
            scheduled_at=(NOW + timedelta(minutes=10)).isoformat(),
            updated_at=NOW - timedelta(days=2),
        )
        for index in range(64):
            self.add_match(
                match_id=f"normal-ended-{index:02d}",
                status=5,
                scheduled_at=(NOW - timedelta(minutes=index + 1)).isoformat(),
                updated_at=NOW - timedelta(days=1),
            )

        candidate_ids = {
            str(row["raybet_match_id"])
            for row in monitoring._realtime_match_candidates(
                self.store.connection,
                NOW,
            )
        }
        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        history = monitor_history_page(self.store.connection, limit=10, now=NOW)

        self.assertIn("match-1", candidate_ids)
        match = next(
            item
            for item in snapshot["matches"]
            if item["raybet_match_id"] == "match-1"
        )
        self.assertEqual(match["lifecycle"], "degraded")
        self.assertFalse(match["history_eligible"])
        self.assertEqual(snapshot["summary"]["live_view"]["degraded"], 1)
        self.assertNotIn(
            "match-1",
            {item["raybet_match_id"] for item in history["items"]},
        )

    def test_submillisecond_schedule_boundary_is_decided_by_python(self) -> None:
        self.add_match(
            status=5,
            scheduled_at="2026-07-14 22:00:00",
            updated_at=NOW - timedelta(days=2),
        )
        future_clock = NOW - timedelta(microseconds=500)
        past_clock = NOW + timedelta(microseconds=500)

        future_review_ids = {
            str(row["raybet_match_id"])
            for row in monitoring._ended_review_match_candidates(
                self.store.connection,
                future_clock,
            )
        }
        past_review_ids = {
            str(row["raybet_match_id"])
            for row in monitoring._ended_review_match_candidates(
                self.store.connection,
                past_clock,
            )
        }
        future = build_monitor_snapshot(
            self.store.connection,
            now=future_clock,
        )["matches"][0]
        past = build_monitor_snapshot(
            self.store.connection,
            now=past_clock,
        )["matches"][0]

        self.assertIn("match-1", future_review_ids)
        self.assertEqual(future["lifecycle"], "degraded")
        self.assertFalse(future["history_eligible"])
        self.assertNotIn("match-1", past_review_ids)
        self.assertEqual(past["lifecycle"], "ended")
        self.assertTrue(past["history_eligible"])

    def test_sqlite_permissive_noncanonical_ended_schedules_require_review(
        self,
    ) -> None:
        cases = {
            "whitespace-separator": "2026-07-13    22:00:00",
            "hour-24": "2026-07-13 24:00:00",
            "february-30": "2026-02-30 22:00:00",
            "trailing-space": "2026-07-13 22:00:00 ",
        }
        for match_id, scheduled_at in cases.items():
            self.add_match(
                match_id=match_id,
                status=5,
                scheduled_at=scheduled_at,
                updated_at=NOW - timedelta(days=2),
            )

        candidate_ids = {
            str(row["raybet_match_id"])
            for row in monitoring._realtime_match_candidates(
                self.store.connection,
                NOW,
            )
        }
        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        matches = {item["raybet_match_id"]: item for item in snapshot["matches"]}
        history_ids = {
            item["raybet_match_id"]
            for item in monitor_history_page(
                self.store.connection,
                limit=50,
                now=NOW,
            )["items"]
        }

        for match_id in cases:
            with self.subTest(match_id=match_id):
                schedule_key = self.store.connection.execute(
                    f"""SELECT {monitoring._SCHEDULE_UTC_JULIANDAY}
                           FROM raybet_matches WHERE raybet_match_id=?""",
                    (match_id,),
                ).fetchone()[0]
                self.assertIsNone(schedule_key)
                self.assertIsNone(monitoring._parse_schedule(cases[match_id]))
                self.assertIn(match_id, candidate_ids)
                self.assertEqual(matches[match_id]["lifecycle"], "degraded")
                self.assertFalse(matches[match_id]["history_eligible"])
                self.assertNotIn(match_id, history_ids)

    def test_malformed_scheduled_ended_match_stays_visible_but_out_of_history(
        self,
    ) -> None:
        self.add_match(
            status=5,
            scheduled_at="not-a-timestamp",
            updated_at=NOW - timedelta(days=2),
        )
        for index in range(64):
            self.add_match(
                match_id=f"normal-ended-{index:02d}",
                status=5,
                scheduled_at=(NOW - timedelta(minutes=index + 1)).isoformat(),
                updated_at=NOW - timedelta(days=1),
            )

        candidate_ids = {
            str(row["raybet_match_id"])
            for row in monitoring._realtime_match_candidates(
                self.store.connection,
                NOW,
            )
        }
        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)
        history = monitor_history_page(self.store.connection, limit=10, now=NOW)

        self.assertIn("match-1", candidate_ids)
        match = next(
            item
            for item in snapshot["matches"]
            if item["raybet_match_id"] == "match-1"
        )
        self.assertEqual(match["lifecycle"], "degraded")
        self.assertFalse(match["history_eligible"])
        self.assertEqual(snapshot["summary"]["live_view"]["degraded"], 1)
        self.assertNotIn(
            "match-1",
            {item["raybet_match_id"] for item in history["items"]},
        )

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
        before = monitor_cursor(self.store.connection, now=NOW)
        self.assertEqual(before, monitor_cursor(self.store.connection, now=NOW))

        self.add_winner_pair(NOW, 2.0, 2.0)

        self.assertNotEqual(before, monitor_cursor(self.store.connection, now=NOW))

    def test_snapshot_cursor_ignores_clock_only_age_changes(self) -> None:
        self.add_match(status=2)

        first = build_monitor_snapshot(self.store.connection, now=NOW)
        second = build_monitor_snapshot(
            self.store.connection,
            now=NOW + timedelta(seconds=1),
        )

        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["cursor"], second["cursor"])

    def test_snapshot_cursor_ignores_large_invisible_audit_history_without_full_scan(
        self,
    ) -> None:
        self.add_match(status=2)
        before = build_monitor_snapshot(self.store.connection, now=NOW)
        artifact_hash = "f" * 64
        self.store.connection.execute(
            """INSERT INTO odds_raw_artifacts
               (artifact_hash, source, storage_path, uncompressed_bytes,
                compressed_bytes, schema_fingerprint)
               VALUES (?, 'raybet', 'unused-test-artifact', 2, 2, ?)""",
            (artifact_hash, "e" * 64),
        )
        hidden_rows = 600
        self.store.connection.executemany(
            """INSERT INTO browser_events
               (event_id, schema_version, capture_session_id, captured_at,
                received_at, transport, event_type, raybet_match_id, game_id,
                page_origin, page_path, source_path, payload_hash,
                payload_bytes, payload_json, payload_artifact_hash,
                payload_storage, capture_reason, extension_version,
                recognized, processing_status, processing_reason)
               VALUES (?, 1, ?, ?, ?, 'xhr', 'odds', 'hidden-match', 151,
                       'https://www.ray086.com', '/sports/esports', '/v2/odds',
                       ?, 2, '{}', ?, 'external', NULL, 'test', 1,
                       'processed', NULL)""",
            [
                (
                    hashlib.sha256(f"hidden-browser-{index}".encode()).hexdigest(),
                    "b" * 32,
                    (NOW - timedelta(days=30, seconds=index)).isoformat(),
                    (NOW - timedelta(days=30, seconds=index)).isoformat(),
                    artifact_hash,
                    artifact_hash,
                )
                for index in range(hidden_rows)
            ],
        )
        self.store.connection.executemany(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids,
                radiant_team_side, clock_confidence, draft_confidence,
                source_frame_ref, source_frame_sha256, source_frame_bytes,
                screen_state, confirmed)
               VALUES ('hidden-match', 1, ?, 120, 0, '[]', '[]',
                       'team_one', 0.9, 0.9, ?, NULL, NULL, 'game', 1)""",
            [
                (
                    (NOW - timedelta(days=30, seconds=index)).isoformat(),
                    f"frame-sha256:{hashlib.sha256(f'hidden-vision-{index}'.encode()).hexdigest()}",
                )
                for index in range(hidden_rows)
            ],
        )
        self.store.connection.commit()

        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            after = build_monitor_snapshot(self.store.connection, now=NOW)
        finally:
            self.store.connection.set_trace_callback(None)

        self.assertEqual(before["cursor"], after["cursor"])
        normalized = [" ".join(statement.lower().split()) for statement in statements]
        revision_tables = (
            "vision_observations",
            "browser_events",
            "vision_observation_invalidations",
            "vision_draft_conflicts",
            "vision_derived_invalidations",
        )
        self.assertFalse(
            any(
                ("count(" in statement or "max(" in statement)
                and any(f"from {table}" in statement for table in revision_tables)
                for statement in normalized
            ),
            normalized,
        )
        bounded_reads = [
            statement
            for statement in normalized
            if any(f"from {table}" in statement for table in ("vision_observations", "browser_events"))
        ]
        self.assertTrue(bounded_reads)
        self.assertTrue(
            all("limit" in statement for statement in bounded_reads),
            bounded_reads,
        )

    def test_cursor_changes_for_an_unchanged_complete_transport(self) -> None:
        self.add_match()
        first = NOW - timedelta(seconds=6)
        self.add_winner_response(first, 2.0, 2.0, observation_key="response-1")
        before = monitor_cursor(self.store.connection, now=NOW)
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
        self.assertNotEqual(before, monitor_cursor(self.store.connection, now=NOW))

    def test_cursor_changes_for_vision_invalidation_and_confirmed_downgrade(self) -> None:
        self.add_match(status=2)
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
        before = monitor_cursor(self.store.connection, now=NOW)

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

        after = monitor_cursor(self.store.connection, now=NOW)
        self.assertNotEqual(before, after)
        self.assertEqual(after, monitor_cursor(self.store.connection, now=NOW))

    def test_cursor_ignores_conflict_and_derived_writes_without_visible_change(
        self,
    ) -> None:
        self.add_match(status=2)
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
        before = monitor_cursor(self.store.connection, now=NOW)
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

        conflict_cursor = monitor_cursor(self.store.connection, now=NOW)
        self.assertEqual(before, conflict_cursor)

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

        derived_cursor = monitor_cursor(self.store.connection, now=NOW)
        self.assertEqual(conflict_cursor, derived_cursor)
        self.assertEqual(
            derived_cursor,
            monitor_cursor(self.store.connection, now=NOW),
        )

    def test_monitor_api_exposes_bootstrap_and_match_detail(self) -> None:
        self.add_match(status=5)
        self.add_winner_response(
            NOW, 2.0, 2.0, observation_key="monitor-api-response"
        )
        previous_path = queries.DB_PATH
        queries.init_db(str(self.database))
        try:
            with TestClient(app) as client:
                bootstrap = client.get("/api/monitor/bootstrap")
                detail = client.get("/api/monitor/matches/match-1")
                history = client.get("/api/monitor/history?limit=1")
                invalid_cursor = client.get("/api/monitor/history?cursor=broken")
                invalid_limit = client.get("/api/monitor/history?limit=51")
        finally:
            queries.init_db(previous_path)

        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(bootstrap.json()["matches"][0]["raybet_match_id"], "match-1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["winner_timeline"]), 1)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["items"][0]["raybet_match_id"], "match-1")
        self.assertEqual(invalid_cursor.status_code, 400)
        self.assertEqual(invalid_limit.status_code, 422)

    def test_match_detail_analysis_exposes_only_persisted_live_evidence(self) -> None:
        self.add_match(status=2)
        self.add_decision("analysis-decision", NOW - timedelta(seconds=5), 0.65)
        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            with patch(
                "live_betting.profiles.build_draft_curve",
                side_effect=AssertionError("monitor detail must not recompute curves"),
            ):
                before = self.store.connection.total_changes
                detail = monitor_match_detail(
                    self.store.connection, "match-1", now=NOW
                )
                after = self.store.connection.total_changes
        finally:
            self.store.connection.set_trace_callback(None)

        assert detail is not None
        self.assertEqual(before, after)
        self.assertEqual(detail["analysis"]["odds"]["reason"], "odds_available")
        self.assertEqual(
            detail["analysis"]["vision"]["reason"],
            "trusted_vision_available",
        )
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "available")
        self.assertEqual(strategy["reason"], "strategy_available")
        decision = strategy["data"]["decisions"][0]
        self.assertNotIn("contributions_json", decision)
        self.assertIsInstance(decision["inputs"], dict)
        self.assertEqual(decision["draft_authority"]["strict_mapping_id"], 1)
        self.assertEqual(decision["vision_authority"]["confirmed"], 1)
        lineup = detail["analysis"]["lineup"]
        self.assertEqual(lineup["reason"], "lineup_available")
        self.assertEqual(lineup["data"]["radiant"]["hero_ids"], [1, 2, 3, 4, 5])
        self.assertEqual(lineup["data"]["dire"]["hero_ids"], [6, 7, 8, 9, 10])
        self.assertEqual(
            lineup["data"]["active_curve"]["reason"],
            "active_curve_available",
        )
        self.assertEqual(
            lineup["data"]["scores"],
            {
                "status": "waiting",
                "reason": "rosh_lineup_score_pending",
                "data": None,
            },
        )
        self.assertEqual(
            lineup["data"]["players"],
            {
                "status": "unavailable",
                "reason": "live_player_identity_unavailable",
                "data": None,
            },
        )
        normalized = "\n".join(statements).casefold()
        self.assertNotRegex(
            normalized,
            r"\b(insert|update|delete|replace|create|drop|alter)\b",
        )
        self.assertNotIn("map_results", normalized)
        self.assertNotIn("settlement_reconciliations", normalized)

    def test_match_detail_exposes_latest_causal_rosh_lineup_score(self) -> None:
        self.add_match(status=2)
        self.add_decision("rosh-score", NOW - timedelta(seconds=5), 0.65)
        mapping_id = self.strict_mapping_id
        self.assertIsNotNone(mapping_id)
        anchor = self.store.connection.execute(
            """SELECT draft_hash FROM vision_draft_anchors
                WHERE raybet_match_id='match-1' AND map_number=1"""
        ).fetchone()
        self.assertIsNotNone(anchor)
        draft_hash = str(anchor["draft_hash"])
        player_slots = [
            {
                "slot": slot,
                "side": "radiant" if slot < 5 else "dire",
                "position": (slot % 5) + 1,
                "hero_id": slot + 1,
                "steam_account_id": (
                    101 + slot if slot < 5 else 196 + slot
                ),
                "selected": True,
                "resolved": True,
                "fallback_reason": None,
            }
            for slot in range(10)
        ]
        source_as_of = NOW - timedelta(seconds=3)
        source_week = int(source_as_of.timestamp())
        cache_week_start = rosh_cache_week_start(source_as_of)

        def make_score(
            formula_version: str,
            pure_score: float,
            adjusted_score: float,
        ) -> SimpleNamespace:
            pure_bucket = {
                "minute": 60,
                "time_start": 59,
                "time_end": 60,
                "advantage_side": "radiant",
                "advantage_percent": pure_score,
                "radiant_advantage": pure_score,
                "dire_advantage": 0.0,
                "match_percentage": 50.0,
                "win_rate_graph": pure_score,
                "hero_adjustment": pure_score,
                "hero_base_adjustment": pure_score,
                "hero_tempo_adjustment": 0.0,
                "synergy_adjustment": 0.0,
                "player_adjustment": 0.0,
            }
            evidence = {
                "fixture": "monitor-rosh",
                "source": "stratz",
                "formula_version": formula_version,
                "source_week": source_week,
                "cache_week_start": cache_week_start,
                "source_as_of": source_as_of.isoformat(),
                "player_slots": player_slots,
                "pure_minute_table": [pure_bucket],
                "minute_table": [
                    {
                        **pure_bucket,
                        "advantage_percent": adjusted_score,
                        "radiant_advantage": adjusted_score,
                        "win_rate_graph": adjusted_score,
                        "player_adjustment": adjusted_score - pure_score,
                    }
                ],
                "score": {
                    "pure_lineup_score": pure_score,
                    "player_adjusted_lineup_score": adjusted_score,
                    "effective_lineup_score": adjusted_score,
                    "scoring_mode": "player_adjusted",
                    "player_coverage_count": 10,
                },
            }
            return SimpleNamespace(
                pure_lineup_score=pure_score,
                player_adjusted_lineup_score=adjusted_score,
                effective_lineup_score=adjusted_score,
                scoring_mode="player_adjusted",
                player_coverage_count=10,
                stake_multiplier=1.0,
                stake_cap=1.0,
                formula_version=formula_version,
                source_name="stratz",
                source_week=source_week,
                cache_week_start=cache_week_start,
                source_as_of=source_as_of,
                evidence=evidence,
                evidence_hash=canonical_evidence_hash(evidence),
            )

        score = make_score(ROSH_FORMULA_VERSION, 3.2, 4.1)
        old_score = make_score("dematus-rosh-obsolete", 30.0, 40.0)
        self.assertIsNotNone(
            self.store.insert_rosh_lineup_score(
                old_score,
                raybet_match_id="match-1",
                map_number=1,
                strict_mapping_id=int(mapping_id),
                draft_hash=draft_hash,
                radiant_hero_ids=(1, 2, 3, 4, 5),
                dire_hero_ids=(6, 7, 8, 9, 10),
                radiant_player_ids=(101, 102, 103, 104, 105),
                dire_player_ids=(201, 202, 203, 204, 205),
                created_at=source_as_of,
            )
        )
        persisted = self.store.insert_rosh_lineup_score(
            score,
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=int(mapping_id),
            draft_hash=draft_hash,
            radiant_hero_ids=(1, 2, 3, 4, 5),
            dire_hero_ids=(6, 7, 8, 9, 10),
            radiant_player_ids=(101, 102, 103, 104, 105),
            dire_player_ids=(201, 202, 203, 204, 205),
            created_at=source_as_of,
        )
        self.assertIsNotNone(persisted)
        self.store.connection.commit()

        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        scores = detail["analysis"]["lineup"]["data"]["scores"]
        self.assertEqual(scores["status"], "available")
        self.assertEqual(scores["reason"], "rosh_lineup_score_available")
        self.assertEqual(
            scores["data"],
            {
                "pure_lineup_score": 3.2,
                "player_adjusted_lineup_score": 4.1,
                "effective_lineup_score": 4.1,
                "mode": "player_adjusted",
                "player_coverage": 1.0,
                "player_coverage_count": 10,
                "stake_multiplier": 1.0,
                "formula_version": ROSH_FORMULA_VERSION,
                "source_as_of": source_as_of.isoformat(),
                "score_key": persisted.score_key,
                "player_identity_hash": persisted.player_identity_hash,
                "evidence_hash": score.evidence_hash,
                "stake_cap": 1.0,
            },
        )
        self.assertNotIn("evidence", scores["data"])
        players = detail["analysis"]["lineup"]["data"]["players"]
        self.assertEqual(players["status"], "available")
        self.assertEqual(players["reason"], "rosh_player_identity_available")
        self.assertEqual(len(players["data"]["players"]), 10)
        self.assertEqual(
            players["data"]["players"][0],
            {
                "steam_account_id": 101,
                "side": "radiant",
                "position": 1,
                "hero_id": 1,
                "status": "resolved",
            },
        )
        self.assertNotIn("fallback_reason", players["data"]["players"][0])

    def test_available_analysis_matches_cross_layer_golden_fixture(self) -> None:
        self.add_match(status=2)
        self.add_scored_blocked_decision(
            "blocked-golden-v1",
            NOW - timedelta(seconds=10),
        )
        decision_key = self.add_decision(
            "decision-golden-v1",
            NOW - timedelta(seconds=5),
            0.65,
        )
        expected = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "monitor-analysis-available.json"
            ).read_text(encoding="utf-8")
        )

        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        self.assertEqual(detail["analysis"], expected)
        strategy = expected["strategy"]["data"]
        self.assertEqual(strategy["displayed_count"], 1)
        self.assertEqual(strategy["excluded_decision_count"], 1)
        self.assertEqual(strategy["excluded"]["invalid_payload"], 1)
        [golden_decision] = strategy["decisions"]
        self.assertEqual(golden_decision["decision_key"], decision_key)
        self.assertRegex(golden_decision["decision_key"], r"^[0-9a-f]{32}$")
        self.assertRegex(golden_decision["input_ref"], r"^[0-9a-f]{24}$")
        self.assertAlmostEqual(
            golden_decision["model_probability"],
            monitoring._strategy_probability(
                golden_decision["market_probability"],
                golden_decision["contributions"],
            ),
            places=12,
        )
        self.assertAlmostEqual(
            golden_decision["inputs"]["conservative_probability"],
            monitoring._strategy_probability(
                golden_decision["market_probability"],
                golden_decision["conservative_contributions"],
            ),
            places=12,
        )

    def test_no_signal_decision_is_available_without_strategy_authorities(
        self,
    ) -> None:
        self.add_match(status=2)
        decision_key = self.add_no_signal_decision(
            "no-signal-golden-v1",
            NOW - timedelta(seconds=5),
        )
        expected = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "monitor-analysis-no-signal.json"
            ).read_text(encoding="utf-8")
        )
        persisted = self.store.connection.execute(
            """SELECT draft_curve_key, draft_landmark_key,
                      vision_source_frame_ref, vision_transport_key
                 FROM strategy_decisions WHERE decision_key=?""",
            (decision_key,),
        ).fetchone()
        assert persisted is not None
        self.assertTrue(all(value is None for value in persisted))

        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        self.assertEqual(detail["analysis"], expected)
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "available")
        self.assertEqual(strategy["reason"], "strategy_available")
        self.assertEqual(strategy["data"]["displayed_count"], 1)
        self.assertEqual(strategy["data"]["excluded_decision_count"], 0)
        decision = strategy["data"]["decisions"][0]
        self.assertEqual(decision["decision_key"], decision_key)
        self.assertEqual(decision["eligible"], 0)
        self.assertEqual(decision["reason"], "strict_live_ineligible:mapping_missing")
        self.assertEqual(decision["model_probability"], 0.4)
        self.assertEqual(decision["market_probability"], 0.4)
        self.assertEqual(decision["edge"], 0.0)
        self.assertEqual(decision["data_quality"], 0.0)
        self.assertEqual(decision["contributions"], {})
        self.assertEqual(decision["conservative_contributions"], {})
        self.assertEqual(decision["draft_authority"], {})
        self.assertEqual(decision["vision_authority"], {})

    def test_scored_blocked_decision_is_available_with_complete_authority(
        self,
    ) -> None:
        self.add_match(status=2)
        blocked_key = self.add_scored_blocked_decision(
            "scored-blocked",
            NOW - timedelta(seconds=5),
        )
        connection = self.store.connection
        persisted = connection.execute(
            """SELECT draft_curve_key, draft_landmark_key,
                      vision_source_frame_ref, vision_transport_key
                 FROM strategy_decisions WHERE decision_key=?""",
            (blocked_key,),
        ).fetchone()
        assert persisted is not None
        self.assertIsNotNone(persisted["draft_curve_key"])
        self.assertIsNotNone(persisted["draft_landmark_key"])
        self.assertIsNotNone(persisted["vision_source_frame_ref"])
        self.assertIsNotNone(persisted["vision_transport_key"])

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "available")
        self.assertEqual(strategy["reason"], "strategy_available")
        self.assertEqual(strategy["data"]["excluded_decision_count"], 0)
        self.assertEqual(len(strategy["data"]["decisions"]), 1)
        decision = strategy["data"]["decisions"][0]
        self.assertEqual(decision["decision_key"], blocked_key)
        self.assertEqual(decision["eligible"], 0)
        self.assertEqual(decision["reason"], "edge_below_threshold")
        self.assertEqual(decision["draft_authority"]["strict_mapping_id"], 1)
        self.assertEqual(decision["vision_authority"]["confirmed"], 1)

    def test_scored_blocked_decision_fails_closed_without_exact_authority(
        self,
    ) -> None:
        self.add_match(status=2)
        decision, draft_authority, vision, transport_key = (
            self.build_scored_blocked_decision(
                "scored-blocked-fail-closed",
                NOW - timedelta(seconds=5),
            )
        )
        self.assertFalse(_persist_decision(self.store, decision))
        self.assertFalse(
            _persist_decision(
                self.store,
                decision,
                draft_authority=replace(
                    draft_authority,
                    landmark_key="0" * 64,
                ),
                vision_observation=vision,
                vision_transport_key=transport_key,
            )
        )
        self.assertFalse(
            _persist_decision(
                self.store,
                decision,
                draft_authority=draft_authority,
                vision_observation=vision,
                vision_transport_key="wrong-transport-key",
            )
        )
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM strategy_decisions WHERE decision_key=?",
                (decision.decision_key,),
            ).fetchone()
        )

    def test_malformed_no_signal_decisions_are_reviewed(self) -> None:
        self.add_match(status=2)
        base_key = self.add_no_signal_decision(
            "malformed-no-signal-source",
            NOW - timedelta(seconds=5),
        )
        connection = self.store.connection
        columns = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(strategy_decisions)")
        ]
        source = dict(
            connection.execute(
                "SELECT * FROM strategy_decisions WHERE decision_key=?",
                (base_key,),
            ).fetchone()
        )
        missing_market = json.loads(source["contributions_json"])
        del missing_market["__inputs__"]["market"]
        cases = (
            ("eligible-reason", {"reason": "eligible"}),
            (
                "nonzero-baseline",
                {"model_probability": 0.41, "edge": 0.01},
            ),
            (
                "missing-market",
                {
                    "contributions_json": json.dumps(
                        missing_market, separators=(",", ":")
                    )
                },
            ),
        )
        for label, overrides in cases:
            clone = {
                **source,
                "decision_key": hashlib.sha256(label.encode("ascii")).hexdigest()[
                    :32
                ],
                "input_ref": hashlib.sha256(
                    f"input:{label}".encode("ascii")
                ).hexdigest()[:24],
                **overrides,
            }
            connection.execute(
                f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(clone[column] for column in columns),
            )
        connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES ('strategy_decision', ?, 'match-1', 1,
                       'test', 'test', ?)""",
            (base_key, NOW.isoformat()),
        )
        connection.commit()

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "review")
        self.assertEqual(strategy["reason"], "strategy_evidence_invalid")
        self.assertEqual(strategy["data"]["decisions"], [])
        self.assertEqual(strategy["data"]["excluded"]["invalid_payload"], 3)
        self.assertEqual(strategy["data"]["excluded_decision_count"], 4)

    def test_match_detail_analysis_has_stable_waiting_reasons(self) -> None:
        self.add_match(status=1)

        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        self.assertEqual(
            {
                name: (section["status"], section["reason"])
                for name, section in detail["analysis"].items()
            },
            {
                "odds": ("waiting", "winner_odds_pending"),
                "vision": ("waiting", "trusted_vision_pending"),
                "strategy": ("waiting", "strategy_decision_pending"),
                "lineup": ("waiting", "trusted_vision_pending"),
            },
        )

    def test_match_detail_analysis_excludes_future_live_records(self) -> None:
        self.add_match(status=2)
        past_key = self.add_decision(
            "past-decision", NOW - timedelta(seconds=5), 0.6
        )
        self.add_decision("future-decision", NOW + timedelta(minutes=5), 0.8)
        future_curve_key = str(
            self.store.connection.execute(
                """SELECT curve_key FROM prospective_draft_curves
                    ORDER BY first_usable_at DESC LIMIT 1"""
            ).fetchone()[0]
        )

        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        self.assertEqual(
            [item["decision_key"] for item in detail["decisions"]],
            [past_key],
        )
        self.assertTrue(
            all(_parse <= NOW for _parse in (
                datetime.fromisoformat(point["observed_at"])
                for point in detail["winner_timeline"]
            ))
        )
        self.assertLessEqual(
            datetime.fromisoformat(detail["analysis"]["vision"]["data"]["captured_at"]),
            NOW,
        )
        self.assertNotEqual(
            detail["analysis"]["lineup"]["data"]["active_curve"]["data"]["curve_key"],
            future_curve_key,
        )
        self.assertTrue(detail["markets"])
        self.assertTrue(
            all(
                datetime.fromisoformat(market["received_at"]) <= NOW
                for market in detail["markets"]
            )
        )

        self.add_match(match_id="legacy-market", status=2)
        for observed_at, price_one, price_two in (
            (NOW - timedelta(seconds=5), 2.0, 2.0),
            (NOW + timedelta(minutes=5), 1.5, 3.0),
        ):
            for odds_id, side, price in (
                ("legacy-one", "team_one", price_one),
                ("legacy-two", "team_two", price_two),
            ):
                self.store.insert_odds(
                    OddsSnapshot(
                        "legacy-market",
                        odds_id,
                        "legacy-winner",
                        observed_at,
                        price,
                        1,
                        Market("winner", "map_1", side, None, side, True),
                    )
                )
        self.store.connection.commit()

        legacy = monitor_match_detail(
            self.store.connection, "legacy-market", now=NOW
        )

        assert legacy is not None
        self.assertEqual(
            {market["price"] for market in legacy["markets"]},
            {2.0},
        )
        self.assertTrue(
            all(
                datetime.fromisoformat(market["received_at"]) <= NOW
                for market in legacy["markets"]
            )
        )

    def test_lineup_curve_rejects_stale_dependency_revision(self) -> None:
        self.add_match(status=2)
        self.add_decision("stale-curve", NOW - timedelta(seconds=5), 0.65)
        self.store.connection.execute(
            """UPDATE draft_lineage_revisions
                  SET dependency_revision=dependency_revision+1,
                      updated_at=?
                WHERE singleton=1""",
            (NOW.isoformat(),),
        )
        self.store.connection.commit()

        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        curve = detail["analysis"]["lineup"]["data"]["active_curve"]
        self.assertEqual(curve["status"], "unavailable")
        self.assertEqual(curve["reason"], "lineup_curve_dependency_revision_stale")
        self.assertIsNone(curve["data"])

    def test_lineup_curve_exposes_only_passed_landmarks(self) -> None:
        self.add_match(status=2)
        self.add_decision("mixed-curve", NOW - timedelta(seconds=5), 0.65)
        connection = self.store.connection
        landmark_columns = [
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(prospective_draft_landmarks)"
            )
        ]
        passed = dict(
            connection.execute(
                "SELECT * FROM prospective_draft_landmarks LIMIT 1"
            ).fetchone()
        )
        model_20 = connection.execute(
            """SELECT model_hash, model_version, feature_schema_hash
                 FROM draft_model_artifacts WHERE horizon_minutes=20 LIMIT 1"""
        ).fetchone()
        calibration_20 = connection.execute(
            """SELECT calibration_hash, support
                 FROM draft_calibration_artifacts
                WHERE model_hash=? AND horizon_minutes=20 LIMIT 1""",
            (model_20["model_hash"],),
        ).fetchone()
        failed = {
            **passed,
            "landmark_key": hashlib.sha256(b"monitor-failed-landmark").hexdigest(),
            "horizon_minutes": 20,
            "validation_status": "failed",
            "support": calibration_20["support"],
            "global_calibration_passed": 0,
            "model_hash": model_20["model_hash"],
            "model_version": model_20["model_version"],
            "feature_hash": model_20["feature_schema_hash"],
            "calibration_hash": calibration_20["calibration_hash"],
            "calibration_ref": f"draft-calibration:{calibration_20['calibration_hash']}",
            "global_gate_ref": f"draft-calibration:{calibration_20['calibration_hash']}",
        }
        connection.execute(
            f"INSERT INTO prospective_draft_landmarks "
            f"({', '.join(landmark_columns)}) "
            f"VALUES ({', '.join('?' for _ in landmark_columns)})",
            tuple(failed[column] for column in landmark_columns),
        )
        connection.commit()

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        curve = detail["analysis"]["lineup"]["data"]["active_curve"]
        self.assertEqual(curve["status"], "available")
        self.assertEqual(
            [point["validation_status"] for point in curve["data"]["points"]],
            ["passed"],
        )

        curve_columns = [
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(prospective_draft_curves)"
            )
        ]
        source_curve = dict(
            connection.execute(
                "SELECT * FROM prospective_draft_curves LIMIT 1"
            ).fetchone()
        )
        failed_curve_key = "f" * 64
        failed_curve = {**source_curve, "curve_key": failed_curve_key}
        connection.execute(
            f"INSERT INTO prospective_draft_curves ({', '.join(curve_columns)}) "
            f"VALUES ({', '.join('?' for _ in curve_columns)})",
            tuple(failed_curve[column] for column in curve_columns),
        )
        only_failed = {
            **passed,
            "landmark_key": "e" * 64,
            "curve_key": failed_curve_key,
            "validation_status": "failed",
            "global_calibration_passed": 0,
        }
        connection.execute(
            f"INSERT INTO prospective_draft_landmarks "
            f"({', '.join(landmark_columns)}) "
            f"VALUES ({', '.join('?' for _ in landmark_columns)})",
            tuple(only_failed[column] for column in landmark_columns),
        )
        connection.commit()

        unavailable = monitor_match_detail(connection, "match-1", now=NOW)

        assert unavailable is not None
        failed_section = unavailable["analysis"]["lineup"]["data"]["active_curve"]
        self.assertEqual(failed_section["status"], "unavailable")
        self.assertEqual(
            failed_section["reason"], "validated_live_draft_landmark_missing"
        )
        self.assertIsNone(failed_section["data"])

    def test_malformed_strategy_evidence_is_review_not_json_500(self) -> None:
        self.add_match(status=2)
        deep: object = "leaf"
        for _ in range(20):
            deep = {"nested": deep}
        rows = (
            (
                "bad-ref",
                1,
                0.4,
                0.5,
                0.1,
                0.8,
                0,
                "{}",
                "../input",
            ),
            (
                "bad-numeric",
                0,
                1.2,
                0.5,
                -0.7,
                -0.1,
                2,
                "{}",
                "input-safe",
            ),
            (
                "bad-nan",
                1,
                0.4,
                0.5,
                0.1,
                0.8,
                0,
                '{"__inputs__":{"value":NaN}}',
                "input-safe",
            ),
            (
                "bad-depth",
                1,
                0.4,
                0.5,
                0.1,
                0.8,
                0,
                json.dumps({"__inputs__": deep}),
                "input-safe",
            ),
        )
        for index, row in enumerate(rows):
            self.store.connection.execute(
                """INSERT INTO strategy_decisions
                   (decision_key, raybet_match_id, map_number, decided_at,
                    underdog_side, market_probability, model_probability,
                    edge, data_quality, eligible, reason, contributions_json,
                    input_ref, strategy_version)
                   VALUES (?, 'match-1', ?, ?, 'team_one', ?, ?, ?, ?, ?,
                           'blocked', ?, ?, 'strategy-v1')""",
                (
                    row[0],
                    row[1],
                    (NOW - timedelta(seconds=10 - index)).isoformat(),
                    *row[2:],
                ),
            )
        self.store.connection.commit()
        previous_path = queries.DB_PATH
        queries.init_db(str(self.database))
        try:
            with TestClient(app) as client:
                response = client.get("/api/monitor/matches/match-1")
        finally:
            queries.init_db(previous_path)

        self.assertEqual(response.status_code, 200)
        strategy = response.json()["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "review")
        self.assertEqual(strategy["reason"], "strategy_evidence_invalid")
        self.assertEqual(strategy["data"]["decisions"], [])
        self.assertEqual(strategy["data"]["excluded"]["invalid_payload"], 4)

    def test_trusted_vision_candidate_scan_is_bounded(self) -> None:
        self.add_match(status=2)
        self.add_frame_observation(
            captured_at=NOW - timedelta(minutes=10),
            label="bounded-vision-anchor",
        )
        for index in range(257):
            captured_at = NOW - timedelta(seconds=300 - index)
            digest = hashlib.sha256(f"wrong-vision-{index}".encode()).hexdigest()
            self.store.connection.execute(
                """INSERT INTO vision_observations
                   (raybet_match_id, map_number, captured_at, game_clock_seconds,
                    is_paused, radiant_hero_ids, dire_hero_ids,
                    radiant_team_side, clock_confidence, draft_confidence,
                    source_frame_ref, source_frame_sha256, source_frame_bytes,
                    screen_state, confirmed)
                   VALUES ('match-1', 1, ?, 120, 0, '[11,12,13,14,15]',
                           '[16,17,18,19,20]', 'team_one', 0.99, 0.99,
                           ?, ?, 1, 'game', 1)""",
                (
                    captured_at.isoformat(),
                    f"vision-frame:sha256:{digest}",
                    digest,
                ),
            )
        self.store.connection.commit()

        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        self.assertEqual(
            detail["analysis"]["vision"],
            {
                "status": "review",
                "reason": "vision_candidate_limit_exceeded",
                "data": None,
            },
        )
        self.assertEqual(
            detail["analysis"]["lineup"]["reason"],
            "vision_candidate_limit_exceeded",
        )

    def test_strict_mapping_candidate_scan_is_bounded(self) -> None:
        self.add_match(status=2)
        self.add_decision("mapping-limit", NOW - timedelta(seconds=5), 0.65)
        connection = self.store.connection
        columns = [
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(strict_live_map_mappings)"
            )
            if str(row["name"]) != "mapping_id"
        ]
        source = dict(
            connection.execute(
                "SELECT * FROM strict_live_map_mappings LIMIT 1"
            ).fetchone()
        )
        for _ in range(64):
            connection.execute(
                f"INSERT INTO strict_live_map_mappings ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(source[column] for column in columns),
            )
        connection.commit()

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        self.assertEqual(
            detail["analysis"]["lineup"],
            {
                "status": "review",
                "reason": "strict_mapping_candidate_limit_exceeded",
                "data": None,
            },
        )

    def test_strategy_scan_finds_valid_after_two_hundred_invalid_rows(self) -> None:
        self.add_match(status=2)
        valid_key = self.add_decision(
            "valid-after-invalid", NOW - timedelta(minutes=5), 0.65
        )
        for index in range(200):
            self.store.connection.execute(
                """INSERT INTO strategy_decisions
                   (decision_key, raybet_match_id, map_number, decided_at,
                    underdog_side, market_probability, model_probability,
                    edge, data_quality, eligible, reason, contributions_json,
                    input_ref, strategy_version)
                   VALUES (?, 'match-1', 1, ?, 'team_one', 0.4, 0.5, 0.1,
                           0.8, 0, 'blocked', '[]', ?, 'strategy-v1')""",
                (
                    f"invalid-scan-{index:03d}",
                    (NOW - timedelta(seconds=index / 1000)).isoformat(),
                    f"invalid-input-{index:03d}",
                ),
            )
        self.store.connection.commit()

        detail = monitor_match_detail(self.store.connection, "match-1", now=NOW)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "available")
        self.assertEqual(
            [decision["decision_key"] for decision in strategy["data"]["decisions"]],
            [valid_key],
        )
        self.assertEqual(strategy["data"]["displayed_count"], 1)
        self.assertEqual(strategy["data"]["scanned_count"], 201)
        self.assertFalse(strategy["data"]["has_more"])
        self.assertFalse(strategy["data"]["truncated"])
        self.assertEqual(
            strategy["data"]["count_scope"],
            "recent_scanned_window",
        )
        self.assertEqual(strategy["data"]["excluded"]["invalid_payload"], 200)
        self.assertEqual(strategy["data"]["excluded_decision_count"], 200)

    def test_strategy_scan_reports_has_more_for_201_valid_rows(self) -> None:
        self.add_match(status=2)
        base_key = self.add_decision(
            "valid-scan-base", NOW - timedelta(seconds=5), 0.65
        )
        connection = self.store.connection
        columns = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(strategy_decisions)")
        ]
        source = dict(
            connection.execute(
                "SELECT * FROM strategy_decisions WHERE decision_key=?",
                (base_key,),
            ).fetchone()
        )
        for index in range(200):
            clone = {
                **source,
                "decision_key": hashlib.sha256(
                    f"valid-scan-{index:03d}".encode("ascii")
                ).hexdigest()[:32],
                "input_ref": hashlib.sha256(
                    f"valid-input-{index:03d}".encode("ascii")
                ).hexdigest()[:24],
            }
            connection.execute(
                f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(clone[column] for column in columns),
            )
        connection.commit()

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "available")
        self.assertEqual(len(strategy["data"]["decisions"]), 200)
        self.assertEqual(strategy["data"]["displayed_count"], 200)
        self.assertTrue(strategy["data"]["has_more"])
        self.assertTrue(strategy["data"]["truncated"])
        self.assertEqual(
            strategy["data"]["count_scope"],
            "recent_scanned_window",
        )

    def test_strategy_scan_is_bounded_and_uses_descending_keyset(self) -> None:
        self.add_match(status=2)
        for index in range(1001):
            self.store.connection.execute(
                """INSERT INTO strategy_decisions
                   (decision_key, raybet_match_id, map_number, decided_at,
                    underdog_side, market_probability, model_probability,
                    edge, data_quality, eligible, reason, contributions_json,
                    input_ref, strategy_version)
                   VALUES (?, 'match-1', 1, ?, 'team_one', 0.4, 0.5, 0.1,
                           0.8, 0, 'blocked', '[]', ?, 'strategy-v1')""",
                (
                    f"bounded-invalid-{index:04d}",
                    (NOW - timedelta(seconds=1)).isoformat(),
                    f"bounded-input-{index:04d}",
                ),
            )
        self.store.connection.commit()
        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            detail = monitor_match_detail(
                self.store.connection,
                "match-1",
                now=NOW,
            )
        finally:
            self.store.connection.set_trace_callback(None)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "review")
        self.assertEqual(strategy["reason"], "strategy_scan_limit_exceeded")
        self.assertEqual(strategy["data"]["scanned_count"], 1000)
        self.assertEqual(strategy["data"]["displayed_count"], 0)
        self.assertEqual(strategy["data"]["excluded_decision_count"], 1000)
        self.assertTrue(strategy["data"]["has_more"])
        self.assertTrue(strategy["data"]["truncated"])
        self.assertEqual(
            strategy["data"]["count_scope"],
            "recent_scanned_window",
        )
        candidate_queries = [
            statement.casefold()
            for statement in statements
            if "from strategy_decisions as decision" in statement.casefold()
            and "contributions_json" in statement.casefold()
        ]
        self.assertGreaterEqual(len(candidate_queries), 4)
        self.assertTrue(
            all(" offset " not in statement for statement in candidate_queries)
        )
        self.assertTrue(
            all("decision.decided_at <" in statement for statement in candidate_queries)
        )

    def test_strategy_authority_reason_counts_overlap_but_unique_rows_do_not(self) -> None:
        self.add_match(status=2)
        decision_key = self.add_decision(
            "overlapping-authority", NOW - timedelta(seconds=5), 0.65
        )
        connection = self.store.connection
        connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES ('strategy_decision', ?, 'match-1', 1,
                       'test', 'test', ?)""",
            (decision_key, NOW.isoformat()),
        )
        invalidation = connection.execute(
            """INSERT INTO strict_live_map_mapping_invalidations
               (mapping_id, reason, invalidated_by, invalidated_at, recorded_at)
               VALUES (?, 'test', 'test', ?, ?)""",
            (self.strict_mapping_id, NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """INSERT INTO strict_live_mapping_impacts
               (mapping_id, invalidation_id, dependent_type, dependent_key,
                reason, recorded_at)
               VALUES (?, ?, 'strategy_decision', ?,
                       'mapping_invalidated', ?)""",
            (
                self.strict_mapping_id,
                int(invalidation.lastrowid),
                decision_key,
                NOW.isoformat(),
            ),
        )
        connection.commit()

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "review")
        self.assertEqual(strategy["data"]["excluded_decision_count"], 1)
        self.assertEqual(strategy["data"]["excluded"]["vision_invalidated"], 1)
        self.assertEqual(strategy["data"]["excluded"]["mapping_impacted"], 1)
        self.assertEqual(strategy["data"]["count_scope"], "recent_scanned_window")

    def test_eligible_strategy_requires_independent_positive_explanation(self) -> None:
        self.add_match(status=2)
        base_key = self.add_decision(
            "explanation-base", NOW - timedelta(seconds=5), 0.65
        )
        connection = self.store.connection
        columns = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(strategy_decisions)")
        ]
        source = dict(
            connection.execute(
                "SELECT * FROM strategy_decisions WHERE decision_key=?",
                (base_key,),
            ).fetchone()
        )
        market_only = {
            **source,
            "decision_key": hashlib.sha256(b"market-only-explanation").hexdigest()[
                :32
            ],
            "input_ref": hashlib.sha256(b"market-only-input").hexdigest()[:24],
            "contributions_json": json.dumps(
                {
                    "market_movement": 0.2,
                    "__conservative__": {"market_movement": 0.1},
                    "__inputs__": {"market": {"quality": 0.8}},
                },
                separators=(",", ":"),
            ),
        }
        connection.execute(
            f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(market_only[column] for column in columns),
        )
        partial = {
            **source,
            "decision_key": hashlib.sha256(
                b"partial-independent-explanation"
            ).hexdigest()[:32],
            "input_ref": hashlib.sha256(b"partial-independent-input").hexdigest()[
                :24
            ],
            "contributions_json": json.dumps(
                {
                    "team_style": 0.2,
                    "draft_curve": 0.1,
                    "__conservative__": {
                        "team_style": 0.1,
                        "draft_curve": 0.05,
                    },
                    "__inputs__": {
                        "draft_authority": json.loads(
                            source["contributions_json"]
                        )["__inputs__"]["draft_authority"],
                        "strict_live_eligibility": {
                            "mapping_refs": {"strict_mapping_id": 1}
                        },
                    },
                },
                separators=(",", ":"),
            ),
        }
        connection.execute(
            f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(partial[column] for column in columns),
        )
        connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES ('strategy_decision', ?, 'match-1', 1,
                       'test', 'test', ?)""",
            (base_key, NOW.isoformat()),
        )
        connection.commit()

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "review")
        self.assertEqual(strategy["reason"], "strategy_evidence_invalid")
        self.assertEqual(strategy["data"]["decisions"], [])
        self.assertEqual(strategy["data"]["excluded"]["invalid_payload"], 2)
        self.assertEqual(strategy["data"]["excluded_decision_count"], 3)

    def test_eligible_strategy_requires_matching_input_authority_lineage(self) -> None:
        self.add_match(status=2)
        base_key = self.add_decision(
            "lineage-base", NOW - timedelta(seconds=5), 0.65
        )
        connection = self.store.connection
        columns = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(strategy_decisions)")
        ]
        source = dict(
            connection.execute(
                "SELECT * FROM strategy_decisions WHERE decision_key=?",
                (base_key,),
            ).fetchone()
        )
        payload = json.loads(source["contributions_json"])
        payload["__inputs__"]["draft_authority"]["quality"] = 0.7
        mismatch = {
            **source,
            "decision_key": hashlib.sha256(b"lineage-mismatch").hexdigest()[:32],
            "input_ref": hashlib.sha256(b"lineage-mismatch-input").hexdigest()[:24],
            "contributions_json": json.dumps(payload, separators=(",", ":")),
        }
        connection.execute(
            f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(mismatch[column] for column in columns),
        )
        connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES ('strategy_decision', ?, 'match-1', 1,
                       'test', 'test', ?)""",
            (base_key, NOW.isoformat()),
        )
        connection.commit()

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "review")
        self.assertEqual(strategy["reason"], "strategy_evidence_invalid")
        self.assertEqual(strategy["data"]["decisions"], [])
        self.assertEqual(strategy["data"]["excluded"]["invalid_payload"], 1)
        self.assertEqual(strategy["data"]["excluded_decision_count"], 2)

    def test_eligible_strategy_recomputes_probabilities_and_validates_identity(
        self,
    ) -> None:
        self.add_match(status=2)
        base_key = self.add_decision(
            "math-identity-base", NOW - timedelta(seconds=5), 0.65
        )
        connection = self.store.connection
        columns = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(strategy_decisions)")
        ]
        source = dict(
            connection.execute(
                "SELECT * FROM strategy_decisions WHERE decision_key=?",
                (base_key,),
            ).fetchone()
        )
        base_payload = json.loads(source["contributions_json"])

        raw_tamper = json.loads(json.dumps(base_payload))
        raw_tamper["team_style"] += 0.01
        conservative_probability_tamper = json.loads(json.dumps(base_payload))
        conservative_probability_tamper["__inputs__"][
            "conservative_probability"
        ] += 0.01
        independent_tamper = json.loads(json.dumps(base_payload))
        independent_tamper["__inputs__"]["independent_positive"] = False

        def conservative_tamper(key: str, value: float) -> dict[str, object]:
            payload = json.loads(json.dumps(base_payload))
            payload["__conservative__"][key] = value
            payload["__inputs__"]["conservative_contributions"][key] = value
            probability = monitoring._strategy_probability(
                float(source["market_probability"]),
                payload["__conservative__"],
            )
            assert probability is not None
            payload["__inputs__"]["conservative_probability"] = probability
            return payload

        conservative_amplified = conservative_tamper(
            "team_style", base_payload["team_style"] + 0.01
        )
        conservative_flipped = conservative_tamper("player_form", 0.01)
        conservative_market_changed = conservative_tamper(
            "market_movement", base_payload["market_movement"] + 0.01
        )

        cancellation = json.loads(json.dumps(base_payload))
        cancellation_raw = {
            "team_style": -0.01,
            "player_form": 0.0,
            "draft_curve": 0.0,
            "late_game_style": 0.005,
            "market_movement": 0.08,
        }
        cancellation_conservative = {
            "team_style": -0.01,
            "player_form": 0.0,
            "draft_curve": 0.0,
            "late_game_style": 0.0025,
            "market_movement": 0.08,
        }
        cancellation.update(cancellation_raw)
        cancellation["__conservative__"] = cancellation_conservative
        cancellation["__inputs__"][
            "conservative_contributions"
        ] = cancellation_conservative
        cancellation_model = monitoring._strategy_probability(
            float(source["market_probability"]), cancellation_raw
        )
        cancellation_conservative_probability = monitoring._strategy_probability(
            float(source["market_probability"]), cancellation_conservative
        )
        assert cancellation_model is not None
        assert cancellation_conservative_probability is not None
        cancellation["__inputs__"][
            "conservative_probability"
        ] = cancellation_conservative_probability
        cancellation["__inputs__"]["independent_positive"] = True
        cases = (
            ("raw", raw_tamper, {}),
            (
                "model",
                base_payload,
                {
                    "model_probability": float(source["model_probability"]) + 0.01,
                    "edge": float(source["edge"]) + 0.01,
                },
            ),
            ("conservative-probability", conservative_probability_tamper, {}),
            ("independent", independent_tamper, {}),
            ("conservative-amplified", conservative_amplified, {}),
            ("conservative-flipped", conservative_flipped, {}),
            ("conservative-market", conservative_market_changed, {}),
            (
                "independent-cancellation",
                cancellation,
                {
                    "model_probability": cancellation_model,
                    "edge": cancellation_model - float(source["market_probability"]),
                },
            ),
            ("eligible-reason", base_payload, {"reason": "edge_below_threshold"}),
            ("decision-format", base_payload, {"decision_key": "not-32-hex"}),
            ("input-format", base_payload, {"input_ref": "not-24-hex"}),
        )
        for label, payload, overrides in cases:
            clone = {
                **source,
                "decision_key": hashlib.sha256(label.encode("ascii")).hexdigest()[
                    :32
                ],
                "input_ref": hashlib.sha256(
                    f"input:{label}".encode("ascii")
                ).hexdigest()[:24],
                "contributions_json": json.dumps(payload, separators=(",", ":")),
                **overrides,
            }
            connection.execute(
                f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(clone[column] for column in columns),
            )
        connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
               VALUES ('strategy_decision', ?, 'match-1', 1,
                       'test', 'test', ?)""",
            (base_key, NOW.isoformat()),
        )
        connection.commit()

        detail = monitor_match_detail(connection, "match-1", now=NOW)

        assert detail is not None
        strategy = detail["analysis"]["strategy"]
        self.assertEqual(strategy["status"], "review")
        self.assertEqual(strategy["reason"], "strategy_evidence_invalid")
        self.assertEqual(strategy["data"]["decisions"], [])
        self.assertEqual(strategy["data"]["excluded"]["invalid_payload"], 11)
        self.assertEqual(strategy["data"]["excluded"]["vision_invalidated"], 1)
        self.assertEqual(strategy["data"]["excluded_decision_count"], 12)

    def test_sqlite_errors_are_classified_and_busy_detail_returns_503(self) -> None:
        readonly = sqlite3.OperationalError("database is locked")
        readonly.sqlite_errorcode = sqlite3.SQLITE_READONLY
        memory = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(sqlite3.OperationalError) as missing_table:
                memory.execute("SELECT * FROM missing_monitor_table").fetchall()
            memory.execute("CREATE TABLE monitor_sample (present INTEGER)")
            with self.assertRaises(sqlite3.OperationalError) as missing_column:
                memory.execute("SELECT absent FROM monitor_sample").fetchall()
        finally:
            memory.close()
        self.assertEqual(
            classify_sqlite_error(sqlite3.OperationalError("database is locked")),
            "busy",
        )
        self.assertEqual(classify_sqlite_error(readonly), "other")
        self.assertEqual(
            missing_table.exception.sqlite_errorcode,
            sqlite3.SQLITE_ERROR,
        )
        self.assertEqual(
            classify_sqlite_error(missing_table.exception),
            "schema_missing",
        )
        self.assertEqual(
            classify_sqlite_error(missing_column.exception),
            "schema_missing",
        )
        self.assertEqual(
            classify_sqlite_error(
                sqlite3.OperationalError("database is locked during readonly write")
            ),
            "other",
        )
        self.assertEqual(
            classify_sqlite_error(sqlite3.OperationalError("no such column: x")),
            "schema_missing",
        )
        self.assertEqual(
            classify_sqlite_error(sqlite3.DatabaseError("database disk image is malformed")),
            "other",
        )
        previous_path = queries.DB_PATH
        queries.init_db(str(self.database))
        try:
            with (
                patch(
                    "web.routers.monitor.monitoring.monitor_match_detail",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ),
                TestClient(app) as client,
            ):
                response = client.get("/api/monitor/matches/match-1")
        finally:
            queries.init_db(previous_path)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "1")


if __name__ == "__main__":
    unittest.main()
