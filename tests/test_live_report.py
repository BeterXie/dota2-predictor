from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from event_intelligence.ingest_adapters import SQLiteIngestAdapter
from event_intelligence.raw_archive import RawArchive, canonical_json_bytes
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.models import Market, OddsSnapshot
from live_betting.postmatch_monitor import StoredMapResult
from live_betting.raybet import parse_raybet_map_final
from live_betting.report import build_report
from live_betting.settlement import (
    persist_authoritative_settlement_snapshot,
    resolve_authoritative_settlement,
)
from live_betting.storage import (
    _DRAFT_AUTHORITY_COLUMNS,
    _VISION_AUTHORITY_COLUMNS,
    LiveBettingStore,
)
from live_betting.strict_eligibility import accept_strict_live_map_mapping
from tests.draft_authority_fixture import (
    make_test_vision_observation,
    seed_test_draft_authority,
)


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)


def live_odds_payload(
    match_id: str,
    map_number: int,
    rows: list[OddsSnapshot],
) -> dict[str, object]:
    return {
        "result": {
            "id": match_id,
            "game_id": 151,
            "team": [
                {"team_id": 101, "team_name": "One", "pos": 1},
                {"team_id": 202, "team_name": "Two", "pos": 2},
            ],
            "odds": [
                {
                    "id": row.odds_id,
                    "odds_group_id": row.odds_group_id,
                    "team_id": 101 if row.market.side == "team_one" else 202,
                    "match_stage": f"r{map_number}",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "odds": row.price,
                    "status": row.status,
                    "last_update": row.last_update,
                }
                for row in rows
            ],
        }
    }


def raybet_final_payload(
    match_id: str,
    map_number: int,
    winner_side: str,
) -> dict[str, object]:
    period = f"r{map_number}"
    return {
        "id": match_id,
        "game_id": 151,
        "tournament_name": "Test Event",
        "start_time": "2026-07-15 16:00:00",
        "round": "bo5",
        "stage": "main_event",
        "status": 2,
        "team": [
            {
                "pos": 1,
                "team_id": 101,
                "team_name": "One",
                "score": {period: 1 if winner_side == "team_one" else 0},
            },
            {
                "pos": 2,
                "team_id": 202,
                "team_name": "Two",
                "score": {period: 1 if winner_side == "team_two" else 0},
            },
        ],
        "odds": [
            {
                "odds_id": f"final-{match_id}-{map_number}-one",
                "odds_group_id": f"final-{match_id}-{map_number}",
                "match_stage": period,
                "group_short_name": "Winner",
                "tag": "win",
                "team_id": 101,
                "odds": 2.0,
                "status": 5,
                "win": 1 if winner_side == "team_one" else 0,
            },
            {
                "odds_id": f"final-{match_id}-{map_number}-two",
                "odds_group_id": f"final-{match_id}-{map_number}",
                "match_stage": period,
                "group_short_name": "Winner",
                "tag": "win",
                "team_id": 202,
                "odds": 2.0,
                "status": 5,
                "win": 1 if winner_side == "team_two" else 0,
            },
        ],
    }


class LiveReportCohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LiveBettingStore(":memory:")
        self.store.init_schema()
        intelligence = IntelligenceStorage(
            self.store.path, connection=self.store.connection
        )
        intelligence.init_schema()
        ingest = SQLiteIngestAdapter(intelligence, EventRegistry(intelligence))
        self.opendota_archive = RawArchive(
            Path(self.store.raw_archive_root) / "opendota",
            observation_sink=ingest.record_raw_artifact,
        )
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS event_registry (event_id TEXT PRIMARY KEY)"
        )

    def tearDown(self) -> None:
        self.store.close()

    def insert_strict_mapping(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        event_id: str,
        available_at: str | None = None,
    ) -> int:
        official_url = f"https://example.invalid/events/{event_id}"
        self.store.connection.execute(
            """INSERT OR IGNORE INTO event_registry
               (event_id, canonical_name, tier, prize_pool_usd,
                main_event_start_at, main_event_end_at, opendota_league_id,
                secondary_provider_ids_json, official_evidence_urls_json,
                evidence_status, scope_policy_version, scope, approval_status,
                approved_by, approved_at, reconciliation_status,
                included_stages_json, excluded_categories_json,
                created_at, updated_at)
               VALUES (?, ?, 'tier_1', 1000000, ?, ?, ?, '{}', ?,
                       'manually_audited', 'test-v1', 'formal_main_event',
                       'approved', 'test', ?, 'not_required', '["main_event"]',
                       '["qualifier"]', ?, ?)""",
            (
                event_id,
                f"Test {event_id}",
                (NOW - timedelta(days=2)).isoformat(),
                (NOW + timedelta(days=2)).isoformat(),
                int(hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:12], 16),
                json.dumps([official_url], separators=(",", ":")),
                (NOW - timedelta(days=2)).isoformat(),
                (NOW - timedelta(days=2)).isoformat(),
                (NOW - timedelta(days=2)).isoformat(),
            ),
        )
        self.store.connection.execute(
            """CREATE TABLE IF NOT EXISTS teams (
                   team_id INTEGER PRIMARY KEY,
                   name TEXT,
                   tag TEXT,
                   logo_url TEXT,
                   updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        self.store.connection.executemany(
            "INSERT OR IGNORE INTO teams(team_id, name) VALUES (?, ?)",
            ((10, "Canonical One"), (20, "Canonical Two")),
        )
        self.store.connection.commit()
        raw_available_at = available_at or (
            NOW - timedelta(days=1)
        ).isoformat()
        parsed_available_at = datetime.fromisoformat(raw_available_at)
        naive_available_at = (
            parsed_available_at.tzinfo is None
            or parsed_available_at.utcoffset() is None
        )
        if naive_available_at:
            parsed_available_at = parsed_available_at.replace(tzinfo=timezone.utc)
        available_utc = parsed_available_at.astimezone(timezone.utc)
        schedule_raw = "2026-07-15 16:00:00"
        raybet_payload = {
            "id": raybet_match_id,
            "game_id": 151,
            "tournament_name": "Test Event",
            "start_time": schedule_raw,
            "round": "bo5",
            "stage": "main_event",
            "team": [
                {"pos": 1, "team_id": 101, "team_name": "One"},
                {"pos": 2, "team_id": 202, "team_name": "Two"},
            ],
        }
        self.store.upsert_raybet_match(raybet_payload, available_utc)
        self.store.connection.commit()
        existing = self.store.connection.execute(
            """SELECT mapping_id FROM strict_live_map_mappings
                WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        ).fetchone()
        if existing is not None:
            cursor = self.store.connection.execute(
                """INSERT INTO strict_live_map_mappings
                   (raybet_match_id, map_number, event_id, team_one_id,
                    team_two_id, canonical_team_one_id,
                    canonical_team_one_name, canonical_team_two_id,
                    canonical_team_two_name, canonical_identity_json,
                    canonical_identity_hash, crosswalk_evidence_json,
                    crosswalk_evidence_hash, stage_scope, scheduled_at_utc,
                    raybet_best_of, raybet_identity_json, raybet_identity_hash,
                    raybet_metadata_updated_at, source, evidence_json,
                    evidence_hash, mapping_version, acceptance_mode,
                    automatic_approval_id, accepted_by, accepted_at,
                    recorded_at, created_at)
                   SELECT raybet_match_id, map_number, ?, team_one_id,
                          team_two_id, canonical_team_one_id,
                          canonical_team_one_name, canonical_team_two_id,
                          canonical_team_two_name, canonical_identity_json,
                          canonical_identity_hash, crosswalk_evidence_json,
                          crosswalk_evidence_hash, stage_scope, scheduled_at_utc,
                          raybet_best_of, raybet_identity_json,
                          raybet_identity_hash, raybet_metadata_updated_at,
                          source, evidence_json, evidence_hash, mapping_version,
                          acceptance_mode, automatic_approval_id, accepted_by,
                          accepted_at, recorded_at, created_at
                     FROM strict_live_map_mappings WHERE mapping_id=?""",
                (event_id, int(existing[0])),
            )
            self.store.connection.commit()
            return int(cursor.lastrowid)
        evidence = {
            "kind": "manual_cross_source_review",
            "raybet_url": f"https://example.invalid/raybet/{raybet_match_id}",
            "official_event_url": official_url,
            "tournament": {
                "raybet_name": "Test Event",
                "event_name": f"Test {event_id}",
            },
            "schedule": {
                "raybet_scheduled_at": schedule_raw,
                "utc_offset_minutes": 480,
                "scheduled_at_utc": NOW.isoformat(),
                "timezone_evidence": "fixture RayBet UTC+08 contract",
            },
            "stage": {
                "scope": "main_event",
                "source_url": official_url,
            },
            "team_crosswalk": {
                "team_one": {
                    "raybet_team_id": 101,
                    "raybet_team_name": "One",
                    "canonical_team_id": 10,
                    "canonical_team_name": "Canonical One",
                    "source_url": "https://example.invalid/teams/one",
                },
                "team_two": {
                    "raybet_team_id": 202,
                    "raybet_team_name": "Two",
                    "canonical_team_id": 20,
                    "canonical_team_name": "Canonical Two",
                    "source_url": "https://example.invalid/teams/two",
                },
            },
        }
        with patch(
            "live_betting.strict_eligibility._utc_now",
            return_value=available_utc,
        ):
            mapping = accept_strict_live_map_mapping(
                self.store.connection,
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                event_id=event_id,
                team_one_id=101,
                team_two_id=202,
                canonical_team_one_id=10,
                canonical_team_two_id=20,
                source="test_exact_mapping",
                evidence=evidence,
                accepted_by="test",
                accepted_at=available_utc,
                mapping_version="test-v1",
            )
        if naive_available_at:
            self.store.connection.execute(
                "DROP TRIGGER strict_live_map_mappings_no_update"
            )
            self.store.connection.execute(
                """UPDATE strict_live_map_mappings
                      SET raybet_metadata_updated_at=?, accepted_at=?,
                          recorded_at=?, created_at=?
                    WHERE mapping_id=?""",
                (
                    raw_available_at,
                    raw_available_at,
                    raw_available_at,
                    raw_available_at,
                    mapping.mapping_id,
                ),
            )
            self.store.connection.commit()
        return mapping.mapping_id

    def test_empty_report_has_no_synthetic_evaluation_evidence(self) -> None:
        report = build_report(self.store.connection)

        self.assertEqual(report["evaluation_cohorts"], [])
        self.assertIsNone(report["confidence_intervals_90"])
        self.assertIsNone(report["event_sensitivity"])
        self.assertEqual(report["stability_status"], "descriptive_only")

    def record_source_reconciliation(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        strict_mapping_id: int,
        dota_match_id: int,
        winner_side: str,
        settled_at: datetime,
        status: str,
        reason: str,
    ) -> tuple[object, StoredMapResult]:
        final_payload = raybet_final_payload(
            raybet_match_id, map_number, winner_side
        )
        response = {"result": final_payload}
        artifact = self.store.archive_response_payload(
            response,
            observed_at=settled_at,
            match_id=raybet_match_id,
            response_kind="final_odds",
        )
        audit_key = self.store.record_direct_response_audit(
            artifact,
            response_kind="final_odds",
            claimed_raybet_match_id=raybet_match_id,
            observed_raybet_match_id=raybet_match_id,
            disposition="audit_only",
            reason="live_report_fixture",
        )
        snapshots = snapshots_from_payload(response, received_at=settled_at)
        transport_key = f"final-transport:{dota_match_id}"
        self.store.store_odds_observation(
            source="direct",
            observation_key=transport_key,
            source_event_id=None,
            raybet_match_id=raybet_match_id,
            observed_at=settled_at,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
            raw_payload=response,
            raw_artifact=artifact,
        )
        response_state_hash = str(
            self.store.connection.execute(
                """SELECT response_state_hash
                     FROM odds_transport_observations
                    WHERE observation_key=?""",
                (transport_key,),
            ).fetchone()[0]
        )
        final = parse_raybet_map_final(
            final_payload,
            map_number,
            observed_at=settled_at,
            expected_match_id=raybet_match_id,
            expected_team_ids=(101, 202),
        )
        team_one_kills = 30 if winner_side == "team_one" else 20
        team_two_kills = 30 if winner_side == "team_two" else 20
        opendota_payload = {
            "match_id": dota_match_id,
            "radiant_win": winner_side == "team_one",
            "radiant_team_id": 10,
            "dire_team_id": 20,
            "radiant_score": team_one_kills,
            "dire_score": team_two_kills,
            "duration": 2400,
        }
        receipt = self.opendota_archive.archive_json(
            source="opendota",
            endpoint=f"/api/matches/{dota_match_id}",
            request_identity=f"/api/matches/{dota_match_id}",
            payload_bytes=canonical_json_bytes(opendota_payload),
            observed_at=settled_at,
            match_id=dota_match_id,
            status_code=200,
            first_usable_at=settled_at,
        )
        opendota_evidence_ref = (
            f"opendota:{dota_match_id}:sha256:{receipt.content_sha256}"
        )
        reconciliation = self.store.record_settlement_reconciliation(
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            strict_mapping_id=strict_mapping_id,
            dota_match_id=dota_match_id,
            raybet_status=final.status,
            raybet_winner_side=final.winner_side,
            opendota_winner_side=winner_side,
            raybet_evidence_ref=final.evidence_ref,
            opendota_evidence_ref=opendota_evidence_ref,
            raybet_facts={},
            opendota_facts={
                "team_one_kills": team_one_kills,
                "team_two_kills": team_two_kills,
                "duration_seconds": 2400,
            },
            status=status,
            reason=reason,
            raybet_observed_at=settled_at,
            opendota_observed_at=settled_at,
            opendota_first_usable_at=settled_at,
            raybet_audit_key=audit_key,
            raybet_transport_key=transport_key,
            raybet_response_state_hash=response_state_hash,
            raybet_response_artifact_hash=artifact.content_sha256,
            opendota_artifact_id=f"opendota:{receipt.content_sha256}",
            opendota_observation_id=receipt.observation_id,
            opendota_content_hash=receipt.content_sha256,
        )
        result = StoredMapResult(
            raybet_match_id,
            map_number,
            dota_match_id,
            winner_side,
            team_one_kills,
            team_two_kills,
            2400,
            f"settlement-reconciliation:{raybet_match_id}:map:{map_number}",
            settled_at,
        )
        return reconciliation, result

    def insert_settled_order(
        self,
        index: int,
        *,
        strategy_version: str = "strategy-v1",
        model_hash: str = "2" * 64,
        event_id: str = "event-one",
        include_complete_identity: bool = True,
        series_id: str | None = None,
        map_number: int = 1,
        game_clock_seconds: int = 30 * 60,
        vision_quality: float = 0.96,
        latency_seconds: float = 3.0,
        coverage: float = 0.85,
        signal_price: float = 3.0,
        fill_price: float | None = 3.0,
        order_status: str = "filled",
        rejection_reason: str | None = None,
        strict_available_at: str | None = None,
        settlement_result: str | None = None,
        settlement_review_required: bool = False,
        settlement_mapping_matches_order: bool = True,
        include_map_result: bool = True,
    ) -> None:
        match_id = series_id or str(1_000_000 + index)
        order_key = f"order-{index}"
        decided_at = NOW + (
            timedelta(hours=index * 2)
            if series_id is not None
            else timedelta(seconds=index * 10)
        )
        strict_mapping_id = self.insert_strict_mapping(
            raybet_match_id=match_id,
            map_number=map_number,
            event_id=event_id,
            available_at=strict_available_at,
        )
        captured_at = decided_at - timedelta(seconds=latency_seconds)
        captured_observation = make_test_vision_observation(
            raybet_match_id=match_id,
            map_number=map_number,
            captured_at=captured_at,
            game_clock_seconds=game_clock_seconds,
            clock_confidence=vision_quality,
            draft_confidence=vision_quality,
            label=f"frame-{index}",
        )
        self.assertTrue(self.store.insert_vision_observation(captured_observation))
        authority = seed_test_draft_authority(
            self.store.connection,
            raybet_match_id=match_id,
            map_number=map_number,
            strict_mapping_id=strict_mapping_id,
            observed_at=captured_at,
            horizon_minutes=30,
            label=f"live-report:{model_hash}",
        )
        signal_market = Market(
            "winner", f"map_{map_number}", "team_two", None, "team_two", True
        )
        opposite_market = Market(
            "winner", f"map_{map_number}", "team_one", None, "team_one", True
        )
        signal = OddsSnapshot(
            match_id,
            f"odds-{index}",
            f"winner-group-{index}",
            decided_at,
            signal_price,
            1,
            signal_market,
        )
        opposite = OddsSnapshot(
            match_id,
            f"odds-{index}-opposite",
            f"winner-group-{index}",
            decided_at,
            signal_price / 1.5,
            1,
            opposite_market,
        )
        signal_rows = [signal, opposite]
        transport_key = f"transport-{index}"
        self.store.store_odds_observation(
            source="direct",
            observation_key=transport_key,
            source_event_id=None,
            raybet_match_id=match_id,
            observed_at=decided_at,
            normalized_state_hash=normalized_state_hash(signal_rows),
            snapshots=signal_rows,
            raw_payload=live_odds_payload(match_id, map_number, signal_rows),
        )
        market_probability = (1.0 / signal.price) / (
            (1.0 / signal.price) + (1.0 / opposite.price)
        )
        probability = 0.7
        outcome = index % 2 == 0
        result = "win" if outcome else "loss"
        return_units = 2.0 if outcome else 0.0
        if settlement_result is not None:
            result = settlement_result
            if result == "review":
                return_units = 0.0
        winner_side = "team_two" if outcome else "team_one"
        reconciliation_status = (
            "manual_review"
            if result == "review" or settlement_review_required
            else "confirmed"
        )
        mapping_timestamp_unverifiable = False
        if strict_available_at is not None:
            parsed_strict_available_at = datetime.fromisoformat(
                strict_available_at
            )
            mapping_timestamp_unverifiable = (
                parsed_strict_available_at.tzinfo is None
                or parsed_strict_available_at.utcoffset() is None
            )
        expected_reconciliation_status = (
            "manual_review"
            if not settlement_mapping_matches_order
            or mapping_timestamp_unverifiable
            else reconciliation_status
        )
        landmark = {
            "model_version": "draft-logistic-l2-v1",
            "model_kind": "pure_draft",
            "availability_mode": "prospective",
            "feature_hash": "1" * 64,
            "model_hash": model_hash,
            "calibration_hash": "3" * 64,
            "global_gate_ref": "global-gate:passed",
        }
        if not include_complete_identity:
            landmark.pop("model_version")
        contributions = json.dumps({
            "draft_curve": 0.1,
            "__inputs__": {
                "draft_landmark": landmark,
                "strict_live_eligibility": {
                    "mapping_refs": {
                        "strict_mapping_id": strict_mapping_id,
                        "strict_event_id": event_id,
                        "strict_canonical_team_one_id": 10,
                        "strict_canonical_team_one_name": "Canonical One",
                        "strict_canonical_team_two_id": 20,
                        "strict_canonical_team_two_name": "Canonical Two",
                    }
                },
                "vision": {
                    "captured_at": captured_at.isoformat(),
                    "source_frame_ref": captured_observation.source_frame_ref,
                    "game_clock_seconds": game_clock_seconds,
                },
            },
        })
        self.assertTrue(
            self.store.insert_decision(
                SimpleNamespace(
                    decision_key=f"decision-{index}",
                    raybet_match_id=match_id,
                    map_number=map_number,
                    decided_at=decided_at,
                    underdog_side="team_two",
                    market_probability=market_probability,
                    model_probability=probability,
                    edge=probability - market_probability,
                    data_quality=coverage,
                    eligible=True,
                    reason="eligible",
                    contributions=json.loads(contributions),
                    input_ref=f"input-{index}",
                    strategy_version=strategy_version,
                ),
                draft_authority=authority,
                vision_observation=replace(
                    captured_observation,
                    game_clock_seconds=int(
                        game_clock_seconds + latency_seconds
                    ),
                ),
                vision_transport_key=transport_key,
            )
        )
        authority_columns = (*_DRAFT_AUTHORITY_COLUMNS, *_VISION_AUTHORITY_COLUMNS)
        decision_authority = self.store.connection.execute(
            f"SELECT {', '.join(authority_columns)} FROM strategy_decisions "
            "WHERE decision_key=?",
            (f"decision-{index}",),
        ).fetchone()
        self.assertIsNotNone(decision_authority)
        assert decision_authority is not None
        self.store.connection.execute(
            """INSERT INTO shadow_map_attempts
               (raybet_match_id, map_number, order_key, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (match_id, map_number, order_key, order_status, decided_at.isoformat()),
        )
        self.store.connection.execute(
            """INSERT INTO shadow_order_decision_lineage
               (order_key, decision_key, recorded_at) VALUES (?, ?, ?)""",
            (order_key, f"decision-{index}", decided_at.isoformat()),
        )
        self.store.connection.execute(
            f"""INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status, fill_price, filled_at,
                rejection_reason, {', '.join(authority_columns)})
               VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, 'team_two', 1, 1.0,
                       ?, ?, ?, ?,
                       {', '.join('?' for _ in authority_columns)})""",
            (
                order_key,
                match_id,
                strict_mapping_id,
                f"odds-{index}",
                f"winner|map_{map_number}|team_two|",
                decided_at.isoformat(),
                probability,
                market_probability,
                signal_price,
                transport_key,
                decided_at.isoformat(),
                (decided_at + timedelta(seconds=15)).isoformat(),
                f"winner-group-{index}",
                order_status,
                fill_price,
                (
                    (decided_at + timedelta(seconds=3)).isoformat()
                    if order_status == "filled"
                    else None
                ),
                rejection_reason,
                *tuple(decision_authority),
            ),
        )
        self.store.connection.commit()
        if order_status != "filled":
            return
        settled_at = decided_at + timedelta(hours=1)
        dota_match_id = 100_000 + index
        settlement_mapping_id = strict_mapping_id
        if not settlement_mapping_matches_order:
            settlement_mapping_id = self.insert_strict_mapping(
                raybet_match_id=match_id,
                map_number=map_number,
                event_id=f"{event_id}-settlement",
                available_at=strict_available_at,
            )
        reconciliation, map_result = self.record_source_reconciliation(
            raybet_match_id=match_id,
            map_number=map_number,
            strict_mapping_id=settlement_mapping_id,
            dota_match_id=dota_match_id,
            winner_side=winner_side,
            settled_at=settled_at,
            status=reconciliation_status,
            reason=(
                "settlement_review_required"
                if reconciliation_status == "manual_review"
                else "sources_consistent"
            ),
        )
        self.assertEqual(
            reconciliation["status"],
            expected_reconciliation_status,
            str(reconciliation["reason"]),
        )
        if (
            expected_reconciliation_status != "confirmed"
            or not include_map_result
            or not settlement_mapping_matches_order
        ):
            self.store.connection.execute(
                """INSERT INTO settlements
                   (order_key, result, return_units, settled_at, evidence_ref,
                    review_required) VALUES (?, ?, ?, ?, ?, 1)""",
                (
                    order_key,
                    result,
                    return_units,
                    settled_at.isoformat(),
                    f"review:{index}",
                ),
            )
            return
        self.assertTrue(
            self.store.insert_map_result(
                map_result, strict_mapping_id=settlement_mapping_id
            )
        )
        self.store.connection.commit()
        settlement_authority = resolve_authoritative_settlement(
            self.store.connection, order_key
        )
        self.assertTrue(persist_authoritative_settlement_snapshot(
            self.store.connection, settlement_authority
        ))
        self.store.connection.execute(
            """INSERT INTO settlements
               (order_key, result, return_units, settled_at, evidence_ref,
                review_required) VALUES (?, ?, ?, ?, ?, 0)""",
            (
                order_key,
                settlement_authority.result,
                settlement_authority.return_units,
                settlement_authority.settled_at.isoformat(),
                settlement_authority.map_result_evidence_ref,
            ),
        )

    def test_incompatible_model_cohorts_are_never_pooled(self) -> None:
        self.insert_settled_order(1, model_hash="2" * 64)
        self.insert_settled_order(2, model_hash="4" * 64)
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(len(report["evaluation_cohorts"]), 2)
        self.assertTrue(all(
            cohort["confidence_intervals_90"]["status"]
            == "insufficient_series"
            for cohort in report["evaluation_cohorts"]
        ))
        self.assertTrue(all(
            cohort["confidence_intervals_90"]["series_count"] == 1
            for cohort in report["evaluation_cohorts"]
        ))
        self.assertIsNone(report["brier_score"])
        self.assertIsNone(report["log_loss"])
        self.assertIsNone(report["maximum_drawdown_units"])
        self.assertEqual(
            report["stability_status"], "incompatible_cohorts_not_pooled"
        )

    def test_confirmed_reconciliation_without_map_result_is_unscored(self) -> None:
        self.insert_settled_order(2, include_map_result=False)
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["settled_orders"], 0)
        self.assertEqual(report["evaluation_cohorts"][0]["settled_orders"], 0)
        self.assertEqual(report["order_audit"]["scored_orders"], 0)

    def test_reconciliation_mapping_must_match_order_mapping(self) -> None:
        self.insert_settled_order(2, settlement_mapping_matches_order=False)
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["settled_orders"], 0)
        self.assertEqual(report["evaluation_cohorts"][0]["settled_orders"], 0)
        self.assertEqual(report["order_audit"]["scored_orders"], 0)

    def test_incomplete_contribution_identity_cannot_hide_persisted_authority(
        self,
    ) -> None:
        self.insert_settled_order(1, include_complete_identity=False)
        self.store.connection.commit()

        report = build_report(self.store.connection)
        json.dumps(report, allow_nan=False)
        cohort = report["evaluation_cohorts"][0]

        self.assertTrue(cohort["identity_complete"])
        self.assertIsNotNone(cohort["brier_score"])
        self.assertIsNotNone(report["brier_score"])
        self.assertEqual(report["stability_status"], "descriptive_only")

    def test_series_bootstrap_and_leave_one_event_out_are_cohort_local(self) -> None:
        self.insert_settled_order(
            1, series_id="200001", map_number=1, event_id="event-a"
        )
        self.insert_settled_order(
            2, series_id="200001", map_number=2, event_id="event-a"
        )
        self.insert_settled_order(
            3, series_id="200002", map_number=1, event_id="event-b"
        )
        self.insert_settled_order(
            4, series_id="200002", map_number=2, event_id="event-b"
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)
        cohort = report["evaluation_cohorts"][0]
        interval = cohort["confidence_intervals_90"]
        sensitivity = cohort["event_sensitivity"]

        self.assertEqual(interval["status"], "computed")
        self.assertEqual(interval["series_count"], 2)
        self.assertEqual(interval["iterations"], 1000)
        self.assertIn("brier_score", interval["metrics"])
        self.assertIn("roi", interval["metrics"])
        self.assertEqual(
            interval,
            build_report(self.store.connection)["evaluation_cohorts"][0][
                "confidence_intervals_90"
            ],
        )
        self.assertEqual(sensitivity["status"], "computed")
        self.assertEqual(
            {row["held_out_event"] for row in sensitivity["slices"]},
            {"event-a", "event-b"},
        )
        self.assertNotIn(
            "series_cluster_bootstrap_90_ci_missing",
            cohort["promotion_gate_failures"],
        )
        self.assertNotIn(
            "leave_one_event_out_sensitivity_missing",
            cohort["promotion_gate_failures"],
        )

    def test_operational_strata_use_persisted_evidence(self) -> None:
        self.insert_settled_order(
            1,
            series_id="200001",
            map_number=1,
            game_clock_seconds=25 * 60,
            vision_quality=0.96,
            latency_seconds=2.0,
            coverage=0.85,
            signal_price=3.0,
            fill_price=2.94,
        )
        self.insert_settled_order(
            2,
            series_id="200001",
            map_number=2,
            game_clock_seconds=35 * 60,
            vision_quality=0.99,
            latency_seconds=6.0,
            coverage=0.65,
            signal_price=4.5,
            fill_price=None,
            order_status="rejected",
            rejection_reason="slippage",
        )
        self.store.connection.commit()

        strata = build_report(self.store.connection)["evaluation_cohorts"][0][
            "stratified"
        ]
        buckets = {
            name: {row["bucket"] for row in rows}
            for name, rows in strata.items()
        }

        self.assertEqual(buckets["team"], {"20:Canonical Two"})
        self.assertEqual(buckets["odds_bucket"], {"2.50-3.99", "4.00-5.99"})
        self.assertEqual(
            buckets["game_minute_bucket"], {"20-29", "30-39"}
        )
        self.assertEqual(
            buckets["vision_quality_bucket"], {"0.95-0.979", "0.98-1.00"}
        )
        self.assertEqual(buckets["signal_reason"], {"eligible"})
        self.assertEqual(buckets["latency_bucket"], {"1-3s", "5-10s"})
        self.assertEqual(
            buckets["coverage_bucket"], {"0.60-0.79", "0.80-1.00"}
        )
        self.assertEqual(
            buckets["rejection"], {"filled", "rejected:slippage"}
        )
        self.assertEqual(
            buckets["slippage_bucket"],
            {"adverse_1-3pct", "rejected_slippage"},
        )

    def test_five_hundred_orders_from_one_event_cannot_claim_stability(self) -> None:
        for index in range(500):
            self.insert_settled_order(index, event_id="only-event")
        self.store.connection.commit()

        report = build_report(self.store.connection)
        cohort = report["evaluation_cohorts"][0]

        self.assertEqual(cohort["settled_orders"], 500)
        self.assertEqual(cohort["event_count"], 1)
        self.assertEqual(
            cohort["stability_status"], "stability_blocked_single_event"
        )
        self.assertEqual(cohort["promotion_gate_status"], "not_passed")
        self.assertIn(
            "cross_event_evidence_missing", cohort["promotion_gate_failures"]
        )
        self.assertNotEqual(report["stability_status"], "stable")

    def test_review_results_are_unscored_and_reconciliation_is_visible(self) -> None:
        self.insert_settled_order(
            1,
            settlement_result="review",
            settlement_review_required=True,
        )
        self.store.connection.execute(
            """UPDATE settlement_reconciliations
                  SET status='manual_review', reason='test_conflict'
                WHERE raybet_match_id='1000001' AND map_number=1"""
        )
        pending_mapping_id = self.insert_strict_mapping(
            raybet_match_id="300001",
            map_number=1,
            event_id="event-pending",
        )
        self.store.connection.execute(
            """INSERT INTO settlement_reconciliations
               (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                raybet_winner_side, opendota_winner_side,
                raybet_evidence_ref, opendota_evidence_ref, status, reason,
                first_observed_at, updated_at)
               VALUES ('300001', 1, ?, 10001, NULL, 'team_one',
                       'raybet:pending', 'opendota:pending', 'pending', 'test',
                       ?, ?)""",
            (pending_mapping_id, NOW.isoformat(), NOW.isoformat()),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["settled_orders"], 0)
        self.assertEqual(report["evaluation_cohorts"][0]["settled_orders"], 0)
        self.assertEqual(report["orders"]["settled"], 0)
        self.assertEqual(
            report["settlement_reconciliation"],
            {"pending": 1, "confirmed": 0, "manual_review": 1},
        )

    def test_order_audit_counts_invalidated_orders_separately_from_evaluation(self) -> None:
        self.insert_settled_order(1)
        self.insert_settled_order(
            2,
            settlement_result="review",
            settlement_review_required=True,
        )
        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, recorded_at)
               VALUES ('shadow_order', 'order-1', '1000001', 1,
                       'vision_draft_conflict', ?)""",
            (NOW.isoformat(),),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "available")
        self.assertEqual(audit["unknown_reasons"], [])
        self.assertEqual(audit["total_orders"], 2)
        self.assertEqual(audit["included_orders"], 1)
        self.assertEqual(audit["scored_orders"], 0)
        self.assertEqual(audit["excluded_orders"], 1)
        self.assertEqual(audit["invalidated_orders"], 1)
        self.assertEqual(audit["draft_conflict_orders"], 0)
        self.assertEqual(audit["review_required_orders"], 1)
        self.assertEqual(report["invalidated_order_count"], 1)
        self.assertEqual(report["review_required_order_count"], 1)
        self.assertEqual(
            audit["exclusion_reasons"],
            {
                "vision_derived_invalidation": 1,
                "vision_draft_conflict": 0,
                "strict_mapping_invalidated": 0,
                "strict_mapping_unverifiable": 0,
            },
        )
        self.assertEqual(report["orders"]["signals"], 1)
        self.assertEqual(report["settled_orders"], 0)

    def test_decision_audit_counts_vision_invalidation(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute(
            """INSERT INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, recorded_at)
               VALUES ('strategy_decision', 'decision-1', '1000001', 1,
                       'vision_observation_invalidated', ?)""",
            (NOW.isoformat(),),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["decision_audit"]

        self.assertEqual(audit["status"], "available")
        self.assertEqual(audit["raw_decisions"], 1)
        self.assertEqual(audit["included_decisions"], 0)
        self.assertEqual(audit["excluded_decisions"], 1)
        self.assertEqual(audit["invalidated_decisions"], 1)
        self.assertEqual(audit["draft_conflict_decisions"], 0)
        self.assertEqual(report["raw_decision_count"], 1)
        self.assertEqual(report["included_decision_count"], 0)
        self.assertEqual(report["invalidated_decision_count"], 1)

    def test_order_audit_preserves_signal_before_future_conflict(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute(
            """UPDATE vision_draft_anchors
                  SET status='conflict', conflict_at=?
                WHERE raybet_match_id='1000001' AND map_number=1""",
            ((NOW + timedelta(minutes=1)).isoformat(),),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "available")
        self.assertEqual(audit["total_orders"], 1)
        self.assertEqual(audit["included_orders"], 1)
        self.assertEqual(audit["scored_orders"], 1)
        self.assertEqual(audit["excluded_orders"], 0)
        self.assertEqual(audit["invalidated_orders"], 0)
        self.assertEqual(audit["draft_conflict_orders"], 0)
        self.assertEqual(report["orders"]["signals"], 1)

        decision_audit = report["decision_audit"]
        self.assertEqual(decision_audit["status"], "available")
        self.assertEqual(decision_audit["raw_decisions"], 1)
        self.assertEqual(decision_audit["included_decisions"], 1)
        self.assertEqual(decision_audit["excluded_decisions"], 0)
        self.assertEqual(decision_audit["invalidated_decisions"], 0)
        self.assertEqual(decision_audit["draft_conflict_decisions"], 0)
        self.assertEqual(report["draft_conflict_decision_count"], 0)

    def test_pre_migration_missing_reconciliation_table_fails_closed(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute("DROP TABLE settlement_reconciliations")
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["settled_orders"], 0)
        self.assertEqual(report["orders"]["settled"], 0)
        self.assertEqual(
            report["settlement_reconciliation"],
            {"pending": 0, "confirmed": 0, "manual_review": 0},
        )

    def test_order_audit_marks_missing_conflict_table_unknown(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute("DROP TABLE vision_draft_conflicts")
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "unavailable")
        self.assertIn(
            "vision_draft_conflict_tables_missing", audit["unknown_reasons"]
        )
        self.assertEqual(audit["total_orders"], 1)
        self.assertEqual(audit["included_orders"], 0)
        self.assertEqual(audit["scored_orders"], 0)
        self.assertIsNone(audit["draft_conflict_orders"])
        self.assertIsNone(audit["excluded_orders"])
        self.assertEqual(report["settled_orders"], 0)

    def test_order_audit_marks_missing_invalidation_table_unknown(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute("DROP TABLE vision_derived_invalidations")
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "unavailable")
        self.assertIn(
            "vision_derived_invalidations_table_missing", audit["unknown_reasons"]
        )
        self.assertEqual(audit["total_orders"], 1)
        self.assertEqual(audit["included_orders"], 0)
        self.assertEqual(audit["scored_orders"], 0)
        self.assertIsNone(audit["invalidated_orders"])
        self.assertIsNone(audit["excluded_orders"])
        self.assertIsNone(report["invalidated_decision_count"])
        decision_audit = report["decision_audit"]
        self.assertEqual(decision_audit["status"], "unavailable")
        self.assertIn(
            "vision_derived_invalidations_table_missing",
            decision_audit["unknown_reasons"],
        )
        self.assertEqual(decision_audit["raw_decisions"], 1)
        self.assertEqual(decision_audit["included_decisions"], 0)
        self.assertIsNone(decision_audit["invalidated_decisions"])
        self.assertIsNone(decision_audit["excluded_decisions"])

    def test_order_audit_marks_missing_anchor_table_fail_closed(self) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute("DROP TABLE vision_draft_anchors")
        self.store.connection.commit()

        report = build_report(self.store.connection)
        audit = report["order_audit"]

        self.assertEqual(audit["status"], "unavailable")
        self.assertIn(
            "vision_draft_conflict_tables_missing", audit["unknown_reasons"]
        )
        self.assertEqual(audit["total_orders"], 1)
        self.assertEqual(audit["included_orders"], 0)
        self.assertEqual(audit["scored_orders"], 0)
        self.assertEqual(report["settled_orders"], 0)
        self.assertEqual(report["invalidated_order_count"], 0)

    def test_strict_schema_loss_excludes_mapped_rows_without_impact_fallback(
        self,
    ) -> None:
        self.insert_settled_order(1)
        self.store.connection.execute(
            "DROP TABLE strict_live_map_mapping_invalidations"
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["decision_count"], 0)
        self.assertEqual(report["orders"]["signals"], 0)
        for audit, raw_key, unverifiable_key in (
            (
                report["decision_audit"],
                "raw_decisions",
                "strict_mapping_unverifiable_decisions",
            ),
            (
                report["order_audit"],
                "total_orders",
                "strict_mapping_unverifiable_orders",
            ),
        ):
            self.assertEqual(audit["status"], "unavailable")
            self.assertIn(
                "strict_live_map_mapping_invalidations_table_missing",
                audit["unknown_reasons"],
            )
            self.assertEqual(audit[raw_key], 1)
            self.assertEqual(audit["excluded_decisions" if raw_key == "raw_decisions" else "excluded_orders"], 1)
            self.assertIsNone(
                audit[
                    "strict_mapping_invalidated_decisions"
                    if raw_key == "raw_decisions"
                    else "strict_mapping_invalidated_orders"
                ]
            )
            self.assertEqual(audit[unverifiable_key], 1)

    def test_naive_strict_mapping_timestamp_is_unverifiable(self) -> None:
        self.insert_settled_order(
            1,
            strict_available_at=(NOW - timedelta(days=1))
            .replace(tzinfo=None)
            .isoformat(),
        )
        self.store.connection.commit()

        report = build_report(self.store.connection)

        self.assertEqual(report["decision_count"], 0)
        self.assertEqual(report["orders"]["signals"], 0)
        self.assertEqual(
            report["decision_audit"]["strict_mapping_unverifiable_decisions"],
            1,
        )
        self.assertEqual(
            report["order_audit"]["strict_mapping_unverifiable_orders"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
