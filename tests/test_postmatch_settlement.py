from __future__ import annotations

import asyncio
import gzip
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from event_intelligence.ingest_adapters import SQLiteIngestAdapter
from event_intelligence.raw_archive import RawArchive, canonical_json_bytes
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage
from live_betting.markets import (
    normalized_state_hash,
    snapshots_from_payload,
)
from live_betting.models import Market, ModelQuote, OddsSnapshot
from live_betting.notifications import claim
from live_betting.postmatch_monitor import (
    StoredMapResult,
    VisionDraftIdentity,
    _causal_draft_cutoffs,
    _latest_exact_raybet_final,
    _reconcile_and_settle,
    _refresh_raybet_final,
    _vision_drafts,
    label_once,
)
from live_betting.raybet import RayBetMapFinal, parse_raybet_map_final
from live_betting.research import ResearchPrediction
from live_betting.settlement import reconcile_map_winners
from live_betting.storage import LiveBettingStore
from live_betting.strategy import make_order
from live_betting.strict_eligibility import (
    accept_strict_live_map_mapping,
    invalidate_strict_live_map_mapping,
)
from live_betting.vision import VisionObservation as VisionObservationRecord
from tests.draft_authority_fixture import (
    make_test_vision_observation,
    seed_test_draft_authority,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def raybet_final_payload(
    *,
    score_winner: str | None = "team_one",
    market_winner: str | None = "team_one",
) -> dict[str, object]:
    score = {
        "team_one": {"r1": 1 if score_winner == "team_one" else 0},
        "team_two": {"r1": 1 if score_winner == "team_two" else 0},
    }
    market_values = {
        "team_one": 1 if market_winner == "team_one" else 0,
        "team_two": 1 if market_winner == "team_two" else 0,
    }
    if market_winner is None:
        market_values = {"team_one": -1, "team_two": -1}
    return {
        "id": "1001",
        "game_id": 151,
        "tournament_name": "PGL Wallachia Season 8",
        "start_time": "2026-04-20 12:00:00",
        "round": "bo3",
        "stage": "main_event",
        "status": 2,
        "team": [
            {
                "pos": 1,
                "team_id": 101,
                "team_name": "One",
                "score": score["team_one"],
            },
            {
                "pos": 2,
                "team_id": 202,
                "team_name": "Two",
                "score": score["team_two"],
            },
        ],
        "odds": [
            {
                "odds_id": "winner-one",
                "odds_group_id": "winner-group",
                "match_stage": "r1",
                "group_short_name": "Winner",
                "tag": "win",
                "team_id": 101,
                "status": 5 if market_winner is not None else 4,
                "win": market_values["team_one"],
            },
            {
                "odds_id": "winner-two",
                "odds_group_id": "winner-group",
                "match_stage": "r1",
                "group_short_name": "Winner",
                "tag": "win",
                "team_id": 202,
                "status": 5 if market_winner is not None else 4,
                "win": market_values["team_two"],
            },
        ],
    }


def live_odds_payload(rows: list[OddsSnapshot]) -> dict[str, object]:
    return {
        "result": {
            "id": "1001",
            "game_id": 151,
            "team": [
                {"team_id": 101, "pos": 1, "team_name": "One"},
                {"team_id": 202, "pos": 2, "team_name": "Two"},
            ],
            "odds": [
                {
                    "id": row.odds_id,
                    "odds_group_id": row.odds_group_id,
                    "team_id": 101 if row.market.side == "team_one" else 202,
                    "match_stage": f"r{row.market.period.removeprefix('map_')}",
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


def VisionObservation(
    raybet_match_id: str,
    map_number: int,
    captured_at: datetime,
    game_clock_seconds: int,
    is_paused: bool,
    radiant_hero_ids: tuple[int, ...],
    dire_hero_ids: tuple[int, ...],
    clock_confidence: float,
    draft_confidence: float,
    source_frame_ref: str,
    screen_state: str,
    radiant_team_side: str | None,
) -> VisionObservationRecord:
    """Preserve legacy fixture calls while publishing real frame evidence."""
    if is_paused or screen_state != "game":
        raise ValueError("postmatch vision fixtures must be unpaused game frames")
    return make_test_vision_observation(
        raybet_match_id=raybet_match_id,
        map_number=map_number,
        captured_at=captured_at,
        game_clock_seconds=game_clock_seconds,
        radiant_hero_ids=radiant_hero_ids,
        dire_hero_ids=dire_hero_ids,
        radiant_team_side=radiant_team_side,
        clock_confidence=clock_confidence,
        draft_confidence=draft_confidence,
        label=source_frame_ref,
    )


class RayBetFinalResultTests(unittest.TestCase):
    def test_normalizes_consistent_final_map_and_exact_outcomes(self) -> None:
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)

        self.assertEqual(final.status, "confirmed")
        self.assertEqual(final.winner_side, "team_one")
        self.assertEqual(final.score_winner_side, "team_one")
        self.assertEqual(final.market_winner_side, "team_one")
        self.assertTrue(final.selection_won("winner-one"))
        self.assertFalse(final.selection_won("winner-two"))
        self.assertIn("sha256:", final.evidence_ref)

    def test_internal_raybet_conflict_is_not_normalized_to_a_winner(self) -> None:
        final = parse_raybet_map_final(
            raybet_final_payload(score_winner="team_one", market_winner="team_two"),
            1,
            observed_at=NOW,
        )

        self.assertEqual(final.status, "conflict")
        self.assertIsNone(final.winner_side)
        self.assertEqual(final.reason, "raybet_score_market_conflict")

    def test_unsettled_winner_market_remains_pending(self) -> None:
        final = parse_raybet_map_final(
            raybet_final_payload(market_winner=None), 1, observed_at=NOW
        )

        self.assertEqual(final.status, "pending")
        self.assertEqual(final.reason, "raybet_winner_market_not_settled")

    def test_top_level_live_status_cannot_override_settled_map_evidence(self) -> None:
        payload = {**raybet_final_payload(), "status": 1}

        final = parse_raybet_map_final(payload, 1, observed_at=NOW)

        self.assertEqual(final.status, "confirmed")
        self.assertEqual(final.winner_side, "team_one")

    def test_present_but_malformed_map_score_fails_closed(self) -> None:
        payload = raybet_final_payload()
        payload["team"][0]["score"]["r1"] = "1"  # type: ignore[index]

        final = parse_raybet_map_final(payload, 1, observed_at=NOW)

        self.assertEqual(final.status, "conflict")
        self.assertEqual(final.reason, "raybet_map_score_invalid")

    def test_unknown_third_winner_outcome_fails_closed(self) -> None:
        payload = raybet_final_payload()
        payload["odds"].append(  # type: ignore[union-attr]
            {
                "odds_id": "winner-unknown",
                "odds_group_id": "winner-group",
                "match_stage": "r1",
                "group_short_name": "Winner",
                "tag": "win",
                "team_id": 303,
                "status": 5,
                "win": 0,
            }
        )

        final = parse_raybet_map_final(payload, 1, observed_at=NOW)

        self.assertEqual(final.status, "conflict")
        self.assertEqual(final.reason, "raybet_winner_market_invalid")

    def test_reused_odds_id_with_conflicting_result_fails_closed(self) -> None:
        payload = raybet_final_payload()
        payload["odds"].extend(  # type: ignore[union-attr]
            [
                {
                    "odds_id": "winner-extra",
                    "odds_group_id": "winner-group-2",
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "team_id": 101,
                    "status": 5,
                    "win": 1,
                },
                {
                    "odds_id": "winner-one",
                    "odds_group_id": "winner-group-2",
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "team_id": 202,
                    "status": 5,
                    "win": 0,
                },
            ]
        )

        final = parse_raybet_map_final(payload, 1, observed_at=NOW)

        self.assertEqual(final.status, "conflict")
        self.assertEqual(final.reason, "raybet_winner_market_invalid")

    def test_expected_match_game_and_team_identity_are_bound(self) -> None:
        cases = (
            (
                {**raybet_final_payload(), "id": "1002"},
                {"expected_match_id": "1001", "expected_team_ids": (101, 202)},
                "raybet_match_identity_invalid",
            ),
            (
                {**raybet_final_payload(), "game_id": 1},
                {"expected_match_id": "1001", "expected_team_ids": (101, 202)},
                "raybet_game_identity_invalid",
            ),
            (
                raybet_final_payload(),
                {"expected_match_id": "1001", "expected_team_ids": (202, 101)},
                "raybet_team_identity_conflict",
            ),
        )
        for payload, expected, reason in cases:
            with self.subTest(reason=reason):
                final = parse_raybet_map_final(
                    payload, 1, observed_at=NOW, **expected
                )
                self.assertEqual(final.status, "conflict")
                self.assertEqual(final.reason, reason)

    def test_cross_source_winner_conflict_requires_manual_review(self) -> None:
        status, reason = reconcile_map_winners(
            raybet_status="confirmed",
            raybet_winner="team_two",
            opendota_winner="team_one",
        )
        self.assertEqual((status, reason), ("manual_review", "winner_conflict"))


class PostmatchSettlementPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = LiveBettingStore(Path(self.tempdir.name) / "live.db")
        self.store.init_schema()
        intelligence = IntelligenceStorage(
            self.store.path, connection=self.store.connection
        )
        intelligence.init_schema()
        ingest = SQLiteIngestAdapter(intelligence, EventRegistry(intelligence))
        self.opendota_archive = RawArchive(
            Path(self.tempdir.name) / "opendota-raw",
            observation_sink=ingest.record_raw_artifact,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def latest_stored_raybet_final(
        self,
        payload: dict[str, object],
        *,
        observed_at: datetime = NOW + timedelta(seconds=1),
    ) -> RayBetMapFinal:
        match_id = str(payload.get("id") or "")
        self.assertTrue(match_id)
        odds = payload.get("odds")
        self.assertIsInstance(odds, list)
        assert isinstance(odds, list)
        for index, row in enumerate(odds):
            self.assertIsInstance(row, dict)
            assert isinstance(row, dict)
            row.setdefault("odds", 2.0 + index / 100)
        response = {"result": payload}

        artifact = self.store.archive_response_payload(
            response,
            observed_at=observed_at,
            match_id=match_id,
            response_kind="final_odds",
        )
        self.store.upsert_raybet_match(payload, observed_at)
        audit_key = self.store.record_direct_response_audit(
            artifact,
            response_kind="final_odds",
            claimed_raybet_match_id=match_id,
            observed_raybet_match_id=match_id,
            disposition="audit_only",
            reason="final_result_evidence",
        )
        snapshots = snapshots_from_payload(response, received_at=observed_at)
        transport_key: str | None = None
        response_state_hash: str | None = None
        if snapshots:
            transport_key = f"final-transport:{audit_key}"
            self.store.store_odds_observation(
                source="direct",
                observation_key=transport_key,
                source_event_id=None,
                raybet_match_id=match_id,
                observed_at=observed_at,
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
        final = _latest_exact_raybet_final(
            self.store, match_id, 1, team_ids=(101, 202)
        )
        if final is not None:
            return final
        return replace(
            parse_raybet_map_final(
                payload,
                1,
                observed_at=observed_at,
                expected_match_id=match_id,
                expected_team_ids=(101, 202),
            ),
            audit_key=audit_key,
            transport_key=transport_key,
            response_state_hash=response_state_hash,
            response_artifact_hash=artifact.content_sha256,
        )

    def ensure_strict_mapping(self, raybet_match_id: str = "1001") -> int:
        existing = self.store.connection.execute(
            """SELECT mapping_id FROM strict_live_map_mappings
                WHERE raybet_match_id=? AND map_number=1""",
            (raybet_match_id,),
        ).fetchone()
        if existing is not None:
            return int(existing["mapping_id"])
        IntelligenceStorage(
            self.store.path, connection=self.store.connection
        ).init_schema()
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
            "INSERT OR REPLACE INTO teams(team_id, name) VALUES (?, ?)",
            ((101, "One"), (202, "Two")),
        )
        metadata_at = NOW - timedelta(seconds=2)
        recorded_at = NOW - timedelta(seconds=1)
        self.store.upsert_raybet_match(
            {**raybet_final_payload(), "id": raybet_match_id}, metadata_at
        )
        evidence = {
            "kind": "manual_cross_source_review",
            "raybet_url": f"https://example.invalid/raybet/{raybet_match_id}",
            "official_event_url": "https://www.pglesports.com/",
            "tournament": {
                "raybet_name": "PGL Wallachia Season 8",
                "event_name": "PGL Wallachia Season 8",
            },
            "schedule": {
                "raybet_scheduled_at": "2026-04-20 12:00:00",
                "utc_offset_minutes": 480,
                "scheduled_at_utc": "2026-04-20T04:00:00+00:00",
                "timezone_evidence": "audited RayBet UTC+08 display contract",
            },
            "stage": {
                "scope": "main_event",
                "source_url": "https://www.pglesports.com/",
            },
            "team_crosswalk": {
                "team_one": {
                    "raybet_team_id": 101,
                    "raybet_team_name": "One",
                    "canonical_team_id": 101,
                    "canonical_team_name": "One",
                    "source_url": "https://example.invalid/teams/one",
                },
                "team_two": {
                    "raybet_team_id": 202,
                    "raybet_team_name": "Two",
                    "canonical_team_id": 202,
                    "canonical_team_name": "Two",
                    "source_url": "https://example.invalid/teams/two",
                },
            },
        }
        with patch("live_betting.strict_eligibility._utc_now", return_value=recorded_at):
            mapping = accept_strict_live_map_mapping(
                self.store.connection,
                raybet_match_id=raybet_match_id,
                map_number=1,
                event_id="pgl-wallachia-s8-2026",
                team_one_id=101,
                team_two_id=202,
                canonical_team_one_id=101,
                canonical_team_two_id=202,
                source="test_exact_mapping",
                evidence=evidence,
                accepted_by="test",
                accepted_at=recorded_at,
            )
        self.store.connection.commit()
        return mapping.mapping_id

    def insert_filled_order(
        self,
        *,
        odds_id: str = "winner-one",
        market_key: str = "winner|map_1|team_one|",
        filled_at: datetime = NOW,
    ) -> None:
        strict_mapping_id = self.ensure_strict_mapping()
        market_type, period, side, _line = market_key.split("|")
        market = Market(market_type, period, side, None, side, True)
        opposite_side = "team_two" if side == "team_one" else "team_one"
        opposite_market = Market(
            market_type, period, opposite_side, None, opposite_side, True
        )
        signal_at = max(
            NOW - timedelta(microseconds=500_000),
            filled_at - timedelta(seconds=10),
        )
        vision_observation = VisionObservation(
            "1001", 1, signal_at, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "postmatch-frame", "game", "team_one",
        )
        self.store.insert_vision_observation(vision_observation)
        draft_authority = seed_test_draft_authority(
            self.store.connection,
            raybet_match_id="1001",
            map_number=1,
            strict_mapping_id=strict_mapping_id,
            observed_at=signal_at,
            label="postmatch-settlement",
        )
        signal = OddsSnapshot(
            "1001", odds_id, "winner-group", signal_at, 2.0, 1, market
        )
        opposite_odds_id = "winner-two" if odds_id != "winner-two" else "winner-one"
        opposite_signal = OddsSnapshot(
            "1001",
            opposite_odds_id,
            "winner-group",
            signal_at,
            1.5,
            1,
            opposite_market,
        )
        signal_rows = [signal, opposite_signal]
        self.store.store_odds_observation(
            source="direct",
            observation_key="signal",
            source_event_id=None,
            raybet_match_id="1001",
            observed_at=signal_at,
            normalized_state_hash=normalized_state_hash(signal_rows),
            snapshots=signal_rows,
            raw_payload=live_odds_payload(signal_rows),
        )
        input_ref = f"postmatch-input:{odds_id}:{market_key}"
        strategy_version = "postmatch-test-v1"
        market_probability = (1.0 / signal.price) / (
            (1.0 / signal.price) + (1.0 / opposite_signal.price)
        )
        model_probability = 0.6
        edge = model_probability - market_probability
        self.assertTrue(
            self.store.insert_decision(
                SimpleNamespace(
                    decision_key=f"postmatch-decision:{odds_id}:{market_key}",
                    raybet_match_id="1001",
                    map_number=1,
                    decided_at=signal_at,
                    underdog_side=side,
                    market_probability=market_probability,
                    model_probability=model_probability,
                    edge=edge,
                    data_quality=0.8,
                    eligible=True,
                    reason="eligible",
                    contributions={
                            "__inputs__": {
                                "draft_authority": asdict(draft_authority),
                                "strict_live_eligibility": {
                                "mapping_refs": {
                                    "strict_mapping_id": strict_mapping_id
                                }
                            },
                            "vision": {
                                "captured_at": signal_at.isoformat(),
                                "source_frame_ref": (
                                    vision_observation.source_frame_ref
                                ),
                                "game_clock_seconds": 600,
                            },
                            "quality": {"aggregate": 0.8},
                            "draft_landmark": {
                                "model_version": "postmatch-model-v1",
                                "model_kind": "pure_draft",
                                "model_hash": "a" * 64,
                            },
                        }
                    },
                    input_ref=input_ref,
                    strategy_version=strategy_version,
                ),
                draft_authority=draft_authority,
                vision_observation=vision_observation,
                vision_transport_key="signal",
            )
        )
        order = make_order(
            ModelQuote(
                "1001", period, market, model_probability, market_probability, edge,
                signal_at, strategy_version, input_ref,
            ),
            signal,
            min_edge=0.05,
            signal_transport_key="signal",
            signal_transport_at=signal_at,
        )
        assert order is not None
        self.order_key = order.order_key
        self.assertTrue(
            self.store.insert_map_order(
                order,
                1,
                strict_mapping_id=strict_mapping_id,
                draft_authority=draft_authority,
            )
        )
        successor = OddsSnapshot(
            "1001", odds_id, "winner-group", filled_at, 2.0, 1, market
        )
        opposite_successor = OddsSnapshot(
            "1001",
            opposite_odds_id,
            "winner-group",
            filled_at,
            1.5,
            1,
            opposite_market,
        )
        successor_rows = [successor, opposite_successor]
        self.store.store_odds_observation(
            source="direct",
            observation_key="fill",
            source_event_id=None,
            raybet_match_id="1001",
            observed_at=filled_at,
            normalized_state_hash=normalized_state_hash(successor_rows),
            snapshots=successor_rows,
            raw_payload=live_odds_payload(successor_rows),
        )
        resolved = self.store.process_pending_successor(
            order, watermark=filled_at
        )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.status, "filled")

    def test_core_shadow_ledger_rows_are_immutable(self) -> None:
        self.insert_filled_order()
        settled_at = NOW + timedelta(seconds=1)
        self.assertEqual(
            _reconcile_and_settle(
                self.store,
                self.opendota_result(settled_at=settled_at),
                self.latest_stored_raybet_final(
                    raybet_final_payload(), observed_at=settled_at
                ),
            ),
            {"status": "confirmed", "orders_settled": 1},
        )
        mutations = (
            (
                "update strategy decision",
                """UPDATE strategy_decisions SET reason='tampered'
                     WHERE decision_key=(
                         SELECT decision_key FROM shadow_order_decision_lineage
                          WHERE order_key=?
                     )""",
            ),
            (
                "delete strategy decision",
                """DELETE FROM strategy_decisions
                     WHERE decision_key=(
                         SELECT decision_key FROM shadow_order_decision_lineage
                          WHERE order_key=?
                     )""",
            ),
            (
                "update terminal order",
                "UPDATE shadow_orders SET fill_price=99.0 WHERE order_key=?",
            ),
            (
                "delete terminal order",
                "DELETE FROM shadow_orders WHERE order_key=?",
            ),
            (
                "update settlement core",
                "UPDATE settlements SET result='loss' WHERE order_key=?",
            ),
            (
                "delete settlement",
                "DELETE FROM settlements WHERE order_key=?",
            ),
        )

        for label, statement in mutations:
            with self.subTest(mutation=label):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    self.store.connection.execute(statement, (self.order_key,))
                self.store.connection.rollback()

        order = self.store.connection.execute(
            "SELECT status, fill_price FROM shadow_orders WHERE order_key=?",
            (self.order_key,),
        ).fetchone()
        settlement = self.store.connection.execute(
            "SELECT result, return_units FROM settlements WHERE order_key=?",
            (self.order_key,),
        ).fetchone()
        self.assertEqual(tuple(order), ("filled", 2.0))
        self.assertEqual(tuple(settlement), ("win", 2.0))

    def test_settlement_creation_fails_closed_after_decision_deletion(self) -> None:
        self.insert_filled_order()
        self.store.connection.execute(
            "DROP TRIGGER strategy_decisions_immutable_delete"
        )
        self.store.connection.execute(
            """DELETE FROM strategy_decisions
                WHERE decision_key=(
                    SELECT decision_key FROM shadow_order_decision_lineage
                     WHERE order_key=?
                )""",
            (self.order_key,),
        )
        self.store.connection.commit()

        self.assertFalse(
            self.store.insert_settlement(
                self.order_key,
                "win",
                2.0,
                NOW + timedelta(seconds=1),
                "opendota:deleted-decision",
            )
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM settlements WHERE order_key=?",
                (self.order_key,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE order_key=? AND event_type='settled'""",
                (self.order_key,),
            ).fetchone()[0],
            0,
        )

        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM settlements WHERE order_key=?",
                (self.order_key,),
            ).fetchone()
        )
        self.assertIsNone(
            self.store.connection.execute(
                """SELECT 1 FROM notification_outbox
                    WHERE order_key=? AND event_type='settled'""",
                (self.order_key,),
            ).fetchone()
        )

    def test_claim_fails_closed_after_settlement_ledger_tamper(self) -> None:
        self.insert_filled_order()
        settled_at = NOW + timedelta(seconds=1)
        self.assertEqual(
            _reconcile_and_settle(
                self.store,
                self.opendota_result(settled_at=settled_at),
                self.latest_stored_raybet_final(
                    raybet_final_payload(), observed_at=settled_at
                ),
            ),
            {"status": "confirmed", "orders_settled": 1},
        )
        self.store.connection.execute(
            """UPDATE notification_outbox SET status='sent'
                WHERE order_key=? AND event_type='filled'""",
            (self.order_key,),
        )
        self.store.connection.execute("DROP TRIGGER settlements_core_immutable")
        self.store.connection.execute(
            "UPDATE settlements SET return_units=99.0 WHERE order_key=?",
            (self.order_key,),
        )
        self.store.connection.commit()

        self.assertIsNone(claim(self.store.connection, now=settled_at))
        outbox = self.store.connection.execute(
            """SELECT status, last_error FROM notification_outbox
                WHERE order_key=? AND event_type='settled'""",
            (self.order_key,),
        ).fetchone()
        self.assertEqual(
            tuple(outbox),
            ("dead_letter", "settlement_ledger_authority_mismatch"),
        )

    def opendota_result(
        self,
        *,
        raybet_match_id: str = "1001",
        dota_match_id: int = 9001,
        winner: str = "team_one",
        settled_at: datetime = NOW + timedelta(seconds=1),
        observed_at: datetime | None = None,
        first_usable_at: datetime | None = None,
    ) -> StoredMapResult:
        observed_at = observed_at or settled_at
        first_usable_at = first_usable_at or settled_at
        team_one_kills = 30 if winner == "team_one" else 20
        team_two_kills = 20 if winner == "team_one" else 30
        payload = {
            "match_id": dota_match_id,
            "radiant_win": winner == "team_one",
            "radiant_team_id": 101,
            "dire_team_id": 202,
            "radiant_score": team_one_kills,
            "dire_score": team_two_kills,
            "duration": 2400,
        }
        receipt = self.opendota_archive.archive_json(
            source="opendota",
            endpoint=f"/api/matches/{dota_match_id}",
            request_identity=f"/api/matches/{dota_match_id}",
            payload_bytes=canonical_json_bytes(payload),
            observed_at=observed_at,
            match_id=dota_match_id,
            status_code=200,
            first_usable_at=first_usable_at,
        )
        return StoredMapResult(
            raybet_match_id, 1, dota_match_id, winner,
            team_one_kills, team_two_kills, 2400,
            f"opendota:{dota_match_id}:sha256:{receipt.content_sha256}", settled_at,
            f"opendota:{receipt.content_sha256}", receipt.observation_id,
            receipt.content_sha256, receipt.observed_at,
            receipt.first_usable_at,
        )

    def test_latest_exact_final_preserves_score_without_normalized_rows(self) -> None:
        self.insert_filled_order()
        payload = raybet_final_payload()
        odds = payload["odds"]
        assert isinstance(odds, list)
        for row in odds:
            assert isinstance(row, dict)
            row["odds"] = 1.0

        final = self.latest_stored_raybet_final(payload)

        self.assertEqual(final.status, "confirmed")
        self.assertEqual(final.winner_side, "team_one")
        self.assertEqual(final.score_winner_side, "team_one")
        self.assertIsNotNone(final.audit_key)
        self.assertIsNotNone(final.response_artifact_hash)
        self.assertIsNone(final.transport_key)
        self.assertIsNone(final.response_state_hash)
        self.assertEqual(
            _reconcile_and_settle(self.store, self.opendota_result(), final),
            {"status": "confirmed", "orders_settled": 1},
        )

    def test_latest_exact_score_market_conflict_requires_review(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(
            raybet_final_payload(
                score_winner="team_two", market_winner="team_one"
            )
        )

        self.assertEqual(final.status, "conflict")
        self.assertEqual(final.reason, "raybet_score_market_conflict")
        outcome = _reconcile_and_settle(
            self.store, self.opendota_result(), final
        )

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("manual_review", "raybet_score_market_conflict"),
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT result, review_required FROM settlements"
            ).fetchone()),
            ("review", 1),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM map_results"
            ).fetchone()[0],
            0,
        )

    def test_latest_exact_final_aggregates_conflicting_winner_groups(self) -> None:
        payload = raybet_final_payload()
        odds = payload["odds"]
        assert isinstance(odds, list)
        odds.extend(
            [
                {
                    "odds_id": "winner-two-group-one",
                    "odds_group_id": "winner-group-2",
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "team_id": 101,
                    "status": 5,
                    "win": 0,
                },
                {
                    "odds_id": "winner-two-group-two",
                    "odds_group_id": "winner-group-2",
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "team_id": 202,
                    "status": 5,
                    "win": 1,
                },
            ]
        )

        final = self.latest_stored_raybet_final(payload)

        self.assertEqual(final.status, "conflict")
        self.assertEqual(final.reason, "raybet_winner_market_conflict")

    def test_latest_exact_final_includes_unsupported_winner_rows(self) -> None:
        payload = raybet_final_payload()
        odds = payload["odds"]
        assert isinstance(odds, list)
        odds.append(
            {
                "odds_id": "winner-unknown",
                "odds_group_id": "winner-group-2",
                "match_stage": "r1",
                "group_short_name": "Winner",
                "tag": "win",
                "team_id": 303,
                "status": 5,
                "win": 0,
            }
        )

        final = self.latest_stored_raybet_final(payload)

        self.assertEqual(final.status, "conflict")
        self.assertEqual(final.reason, "raybet_winner_market_invalid")

    def test_postmatch_draft_matching_fails_closed_after_anchor_conflict(self) -> None:
        original = VisionObservation(
            "1001", 1, NOW, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "original", "game", "team_one",
        )
        conflicting = VisionObservation(
            "1001", 1, NOW.replace(second=13), 601, False,
            (1, 2, 3, 4, 6), (5, 7, 8, 9, 10),
            0.95, 0.95, "conflict", "game", "team_one",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)

        self.assertEqual(_vision_drafts(self.store.connection, "1001"), {})

    def test_postmatch_draft_matching_fails_closed_without_conflict_audit(self) -> None:
        self.store.insert_vision_observation(
            VisionObservation(
                "1001", 1, NOW, 600, False,
                (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
                0.95, 0.95, "missing-conflict-audit", "game", "team_one",
            )
        )
        self.store.connection.execute("DROP TABLE vision_draft_conflicts")

        self.assertEqual(_vision_drafts(self.store.connection, "1001"), {})

    def test_causal_cutoff_rechecks_orphan_conflict_without_invalidation(self) -> None:
        mapping_id = self.ensure_strict_mapping()
        anchor_at = NOW - timedelta(seconds=2)
        anchor_observation = VisionObservation(
            "1001", 1, anchor_at, 598, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "cutoff-anchor", "game", "team_one",
        )
        self.store.insert_vision_observation(anchor_observation)
        authority = seed_test_draft_authority(
            self.store.connection,
            raybet_match_id="1001",
            map_number=1,
            strict_mapping_id=mapping_id,
            observed_at=NOW,
            label="postmatch-causal-cutoff",
        )
        signal_rows = [
            OddsSnapshot(
                "1001", "cutoff-one", "cutoff-group", NOW, 3.0, 1,
                Market("winner", "map_1", "team_one", None, "team_one", True),
            ),
            OddsSnapshot(
                "1001", "cutoff-two", "cutoff-group", NOW, 2.0, 1,
                Market("winner", "map_1", "team_two", None, "team_two", True),
            ),
        ]
        self.store.store_odds_observation(
            source="direct",
            observation_key="cutoff-transport",
            source_event_id=None,
            raybet_match_id="1001",
            observed_at=NOW,
            normalized_state_hash=normalized_state_hash(signal_rows),
            snapshots=signal_rows,
            raw_payload=live_odds_payload(signal_rows),
        )
        self.assertTrue(
            self.store.insert_decision(
                SimpleNamespace(
                    decision_key="cutoff-decision",
                    raybet_match_id="1001",
                    map_number=1,
                    decided_at=NOW,
                    underdog_side="team_one",
                    market_probability=0.4,
                    model_probability=0.5,
                    edge=0.1,
                    data_quality=0.8,
                    eligible=True,
                    reason="eligible",
                    contributions={
                        "__inputs__": {
                            "draft_authority": asdict(authority),
                            "strict_live_eligibility": {
                                "mapping_refs": {
                                    "strict_mapping_id": mapping_id,
                                }
                            },
                        }
                    },
                    input_ref="input",
                    strategy_version="strategy",
                ),
                draft_authority=authority,
                vision_observation=replace(
                    anchor_observation, game_clock_seconds=600
                ),
                vision_transport_key="cutoff-transport",
            )
        )
        self.assertEqual(
            _causal_draft_cutoffs(self.store, "1001", {1}),
            {1: NOW},
        )

        self.store.connection.execute(
            """INSERT INTO vision_draft_conflicts
               (raybet_match_id, map_number, captured_at, source_frame_ref,
                observed_draft_hash, radiant_hero_ids, dire_hero_ids,
                observed_radiant_team_side, reason, recorded_at)
               VALUES ('1001', 1, ?, 'orphan-conflict', ?, '[1,2,3,4,6]',
                       '[5,7,8,9,10]', 'team_one',
                       'confirmed_draft_conflict', ?)""",
            (anchor_at.isoformat(), "0" * 64, NOW.isoformat()),
        )
        self.store.connection.commit()

        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM vision_derived_invalidations"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(_causal_draft_cutoffs(self.store, "1001", {1}), {})

    def test_postmatch_draft_matching_recovers_with_same_draft_replacement(self) -> None:
        original = VisionObservation(
            "1001", 1, NOW, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "original-invalidated", "game", "team_one",
        )
        replacement_at = NOW + timedelta(seconds=2)
        replacement = VisionObservation(
            "1001", 1, replacement_at, 602, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "same-draft-replacement", "game", "team_one",
        )
        self.store.insert_vision_observation(original)
        self.store.connection.execute(
            """INSERT INTO vision_observation_invalidations
               (raybet_match_id, captured_at, source_frame_ref,
                invalidated_at, reason)
               VALUES ('1001', ?, ?, ?, 'bad frame')""",
            (
                NOW.isoformat(),
                original.source_frame_ref,
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
        self.store.connection.execute(
            """UPDATE vision_observations SET confirmed=0
                WHERE raybet_match_id='1001'
                  AND source_frame_ref=?""",
            (original.source_frame_ref,),
        )
        self.store.connection.commit()
        self.store.insert_vision_observation(replacement)

        self.assertEqual(
            _vision_drafts(self.store.connection, "1001"),
            {
                1: {
                    VisionDraftIdentity(
                        radiant_hero_ids=frozenset(range(1, 6)),
                        dire_hero_ids=frozenset(range(6, 11)),
                        radiant_team_side="team_one",
                    )
                }
            },
        )
        self.assertEqual(
            _vision_drafts(
                self.store.connection,
                "1001",
                causal_cutoffs={1: NOW + timedelta(seconds=1)},
            ),
            {},
        )

    def test_postmatch_team_side_promotion_respects_causal_cutoff(self) -> None:
        unknown_side = VisionObservation(
            "1001", 1, NOW, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "unknown-side", "game", None,
        )
        known_at = NOW + timedelta(seconds=2)
        known_side = VisionObservation(
            "1001", 1, known_at, 602, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "known-side", "game", "team_two",
        )
        self.store.insert_vision_observation(unknown_side)
        self.store.insert_vision_observation(known_side)

        self.assertEqual(
            _vision_drafts(
                self.store.connection,
                "1001",
                causal_cutoffs={1: known_at - timedelta(microseconds=1)},
            ),
            {},
        )
        self.assertEqual(
            _vision_drafts(
                self.store.connection,
                "1001",
                causal_cutoffs={1: known_at},
            ),
            {
                1: {
                    VisionDraftIdentity(
                        radiant_hero_ids=frozenset(range(1, 6)),
                        dire_hero_ids=frozenset(range(6, 11)),
                        radiant_team_side="team_two",
                    )
                }
            },
        )

    def test_label_once_accepts_exact_opendota_map_identity(self) -> None:
        self.ensure_strict_mapping()
        self.latest_stored_raybet_final(raybet_final_payload())
        self.store.insert_vision_observation(
            VisionObservation(
                "1001", 1, NOW, 600, False,
                (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
                0.95, 0.95, "exact-label", "game", "team_one",
            )
        )

        class FakeOpenDotaClient:
            async def get_team_matches(self, team_id: int) -> list[dict[str, int]]:
                return [
                    {
                        "match_id": 9001,
                        "start_time": int(
                            datetime(
                                2026, 4, 20, 4, 0, tzinfo=timezone.utc
                            ).timestamp()
                        ),
                    }
                ]

            async def get_match(self, match_id: int) -> dict[str, object]:
                return {
                    "match_id": match_id,
                    "leagueid": 19543,
                    "radiant_team_id": 101,
                    "dire_team_id": 202,
                    "radiant_win": True,
                    "radiant_score": 30,
                    "dire_score": 20,
                    "duration": 2400,
                    "players": [
                        *(
                            {"player_slot": slot, "hero_id": slot + 1}
                            for slot in range(5)
                        ),
                        *(
                            {"player_slot": slot, "hero_id": slot - 122}
                            for slot in range(128, 133)
                        ),
                    ],
                }

        outcome = asyncio.run(
            label_once(
                self.store,
                FakeOpenDotaClient(),  # type: ignore[arg-type]
                self.opendota_archive,
                "1001",
                101,
                "team_one",
            )
        )

        self.assertEqual(
            outcome,
            {
                "status": "labeled",
                "maps": 1,
                "orders_settled": 0,
                "settlement_pending": 0,
                "settlement_manual_review": 0,
            },
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT map_number, dota_match_id, winner_side
                     FROM map_results WHERE raybet_match_id='1001'"""
            ).fetchone()),
            (1, 9001, "team_one"),
        )

    def test_label_once_uses_research_cutoff_without_a_filled_order(self) -> None:
        mapping_id = self.ensure_strict_mapping()
        original = VisionObservation(
            "1001", 1, NOW, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "research-original", "game", "team_one",
        )
        conflicting = VisionObservation(
            "1001", 1, NOW + timedelta(seconds=10), 600, False,
            (1, 2, 3, 4, 6), (5, 7, 8, 9, 10),
            0.95, 0.95, "research-conflict", "game", "team_one",
        )
        self.store.insert_vision_observation(original)
        transport_hash = normalized_state_hash([])
        self.store.store_odds_observation(
            source="direct",
            observation_key="research-transport",
            source_event_id=None,
            raybet_match_id="1001",
            observed_at=NOW,
            normalized_state_hash=transport_hash,
            snapshots=[],
            raw_payload=live_odds_payload([]),
        )
        authority = seed_test_draft_authority(
            self.store.connection,
            raybet_match_id="1001",
            map_number=1,
            strict_mapping_id=mapping_id,
            observed_at=NOW,
            label="postmatch-research-cutoff",
        )
        self.assertTrue(
            self.store.insert_research_prediction(
                ResearchPrediction(
                    prediction_key="research-only",
                    schema_version="test",
                    raybet_match_id="1001",
                    map_number=1,
                    observed_at=NOW,
                    game_clock_seconds=600,
                    game_minute=10.0,
                    selected_side="team_one",
                    market_probability=0.5,
                    market_price=2.0,
                    raw_model_probability=authority.radiant_probability,
                    feature_hash=authority.feature_hash,
                    model_hash=authority.model_hash,
                    calibration_hash=authority.calibration_hash,
                    transport_key="research-transport",
                    transport_hash=transport_hash,
                    radiant_hero_ids=(1, 2, 3, 4, 5),
                    dire_hero_ids=(6, 7, 8, 9, 10),
                    radiant_team_side="team_one",
                    strict_mapping_id=mapping_id,
                    clock_source="vision",
                    clock_trust="trusted_vision",
                    manual_clock_event_id=None,
                    manual_clock_seconds=None,
                    manual_clock_trust="not_observed",
                    manual_clock_validation="ok",
                    actionability="research_only",
                    gate_status="passed",
                    gate_failures=(),
                    input_context_hash="f" * 64,
                    draft_authority=authority,
                    created_at=NOW,
                )
            )
        )
        self.store.insert_vision_observation(conflicting)
        self.latest_stored_raybet_final(raybet_final_payload())

        class FakeOpenDotaClient:
            async def get_team_matches(self, team_id: int) -> list[dict[str, int]]:
                del team_id
                return [{
                    "match_id": 9001,
                    "start_time": int(
                        datetime(2026, 4, 20, 4, 0, tzinfo=timezone.utc).timestamp()
                    ),
                }]

            async def get_match(self, match_id: int) -> dict[str, object]:
                return {
                    "match_id": match_id,
                    "leagueid": 19543,
                    "radiant_team_id": 101,
                    "dire_team_id": 202,
                    "radiant_win": True,
                    "radiant_score": 30,
                    "dire_score": 20,
                    "duration": 2400,
                    "players": [
                        *({"player_slot": slot, "hero_id": slot + 1}
                          for slot in range(5)),
                        *({"player_slot": slot, "hero_id": slot - 122}
                          for slot in range(128, 133)),
                    ],
                }

        outcome = asyncio.run(
            label_once(
                self.store,
                FakeOpenDotaClient(),  # type: ignore[arg-type]
                self.opendota_archive,
                "1001",
                101,
                "team_one",
            )
        )

        self.assertEqual(outcome["status"], "labeled")
        self.assertEqual(outcome["maps"], 0)
        self.assertEqual(outcome["settlement_manual_review"], 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM map_results WHERE raybet_match_id='1001'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM settlement_result_evidence
                    WHERE raybet_match_id='1001'"""
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_result_labels"
            ).fetchone()[0],
            0,
        )

    def test_label_once_rejects_two_exact_candidates_before_settlement(self) -> None:
        self.insert_filled_order()
        self.store.insert_vision_observation(
            VisionObservation(
                "1001", 1, NOW, 600, False,
                (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
                0.95, 0.95, "ambiguous-label", "game", "team_one",
            )
        )

        class FakeOpenDotaClient:
            async def get_team_matches(self, team_id: int) -> list[dict[str, int]]:
                start_time = int(
                    datetime(
                        2026, 4, 20, 4, 0, tzinfo=timezone.utc
                    ).timestamp()
                )
                return [
                    {"match_id": 9001, "start_time": start_time},
                    {"match_id": 9002, "start_time": start_time + 60},
                ]

            async def get_match(self, match_id: int) -> dict[str, object]:
                return {
                    "match_id": match_id,
                    "leagueid": 19543,
                    "radiant_team_id": 101,
                    "dire_team_id": 202,
                    "radiant_win": True,
                    "radiant_score": 30,
                    "dire_score": 20,
                    "duration": 2400,
                    "players": [
                        *(
                            {"player_slot": slot, "hero_id": slot + 1}
                            for slot in range(5)
                        ),
                        *(
                            {"player_slot": slot, "hero_id": slot - 122}
                            for slot in range(128, 133)
                        ),
                    ],
                }

        with tempfile.TemporaryDirectory() as archive_dir:
            outcome = asyncio.run(
                label_once(
                    self.store,
                    FakeOpenDotaClient(),  # type: ignore[arg-type]
                    RawArchive(Path(archive_dir)),
                    "1001",
                    101,
                    "team_one",
                )
            )

        self.assertEqual(outcome["status"], "opendota_map_identity_ambiguous")
        self.assertEqual(outcome["ambiguous_maps"], [1])
        for table in (
            "map_results",
            "settlement_reconciliations",
            "settlements",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE event_type='settled'"""
            ).fetchone()[0],
            0,
        )

    def test_label_once_rechecks_mapping_after_opendota_awaits(self) -> None:
        mapping_id = self.ensure_strict_mapping()
        self.store.insert_vision_observation(
            VisionObservation(
                "1001", 1, NOW, 600, False,
                (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
                0.95, 0.95, "mapping-race-label", "game", "team_one",
            )
        )

        class FakeOpenDotaClient:
            async def get_team_matches(self, team_id: int) -> list[dict[str, int]]:
                return [
                    {
                        "match_id": 9001,
                        "start_time": int(
                            datetime(
                                2026, 4, 20, 4, 0, tzinfo=timezone.utc
                            ).timestamp()
                        ),
                    }
                ]

            async def get_match(self, match_id: int) -> dict[str, object]:
                invalidate_strict_live_map_mapping(
                    self_connection,
                    mapping_id=mapping_id,
                    reason="identity withdrawn during fetch",
                    invalidated_by="test",
                    invalidated_at=NOW,
                )
                return {
                    "match_id": match_id,
                    "leagueid": 19543,
                    "radiant_team_id": 101,
                    "dire_team_id": 202,
                    "radiant_win": True,
                    "radiant_score": 30,
                    "dire_score": 20,
                    "duration": 2400,
                    "players": [
                        *(
                            {"player_slot": slot, "hero_id": slot + 1}
                            for slot in range(5)
                        ),
                        *(
                            {"player_slot": slot, "hero_id": slot - 122}
                            for slot in range(128, 133)
                        ),
                    ],
                }

        self_connection = self.store.connection
        with tempfile.TemporaryDirectory() as archive_dir:
            outcome = asyncio.run(
                label_once(
                    self.store,
                    FakeOpenDotaClient(),  # type: ignore[arg-type]
                    RawArchive(Path(archive_dir)),
                    "1001",
                    101,
                    "team_one",
                )
            )

        self.assertEqual(
            outcome,
            {
                "status": "strict_mapping_changed_during_postmatch",
                "map_number": 1,
            },
        )
        for table in (
            "map_results",
            "settlement_result_evidence",
            "settlement_reconciliations",
            "settlements",
            "notification_outbox",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                )

    def test_postmatch_draft_recovery_requires_post_invalidation_lineage(self) -> None:
        earlier_same_draft = VisionObservation(
            "1001", 1, NOW - timedelta(seconds=1), 599, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "earlier-same-draft", "game", "team_one",
        )
        original = VisionObservation(
            "1001", 1, NOW, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "original-invalidated", "game", "team_one",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(earlier_same_draft)
        self.store.connection.execute(
            """INSERT INTO vision_observation_invalidations
               (raybet_match_id, captured_at, source_frame_ref,
                invalidated_at, reason)
               VALUES ('1001', ?, ?, ?, 'bad frame')""",
            (
                NOW.isoformat(),
                original.source_frame_ref,
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
        self.store.connection.execute(
            """UPDATE vision_observations SET confirmed=0
                WHERE raybet_match_id='1001'
                  AND source_frame_ref=?""",
            (original.source_frame_ref,),
        )
        self.store.connection.commit()

        self.assertEqual(_vision_drafts(self.store.connection, "1001"), {})

    def test_settlement_before_later_draft_conflict_stays_confirmed(self) -> None:
        self.insert_filled_order()
        original = VisionObservation(
            "1001", 1, NOW + timedelta(seconds=1), 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "original", "game", "team_one",
        )
        conflicting = VisionObservation(
            "1001", 1, NOW + timedelta(seconds=2), 601, False,
            (1, 2, 3, 4, 6), (5, 7, 8, 9, 10),
            0.95, 0.95, "conflict", "game", "team_one",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        final = self.latest_stored_raybet_final(raybet_final_payload())

        outcome = _reconcile_and_settle(self.store, self.opendota_result(), final)

        self.assertEqual(outcome, {"status": "confirmed", "orders_settled": 1})
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("confirmed", "sources_consistent"),
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0],
            1,
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT result, review_required FROM settlements"
            ).fetchone()),
            ("win", 0),
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE event_type='settled'"""
            ).fetchone()[0],
            1,
        )

    def test_later_draft_conflict_preserves_prior_settlement(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        result = self.opendota_result()
        self.assertEqual(
            _reconcile_and_settle(self.store, result, final),
            {"status": "confirmed", "orders_settled": 1},
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT status FROM notification_outbox
                    WHERE event_type='settled'"""
            ).fetchone()[0],
            "pending",
        )

        original = VisionObservation(
            "1001", 1, NOW + timedelta(seconds=1), 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "original-late", "game", "team_one",
        )
        conflicting = VisionObservation(
            "1001", 1, NOW + timedelta(seconds=2), 601, False,
            (1, 2, 3, 4, 6), (5, 7, 8, 9, 10),
            0.95, 0.95, "conflict-late", "game", "team_one",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)

        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("confirmed", "sources_consistent"),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT review_required FROM settlements WHERE order_key=?",
                (self.order_key,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT status, last_error FROM notification_outbox
                    WHERE event_type='settled'"""
            ).fetchone()),
            ("pending", None),
        )

    def test_late_arriving_conflict_reviews_later_settlement(self) -> None:
        self.insert_filled_order()
        settled_at = NOW + timedelta(seconds=20)
        result = self.opendota_result(settled_at=settled_at)
        final = self.latest_stored_raybet_final(
            raybet_final_payload(), observed_at=settled_at
        )
        self.assertEqual(
            _reconcile_and_settle(self.store, result, final),
            {"status": "confirmed", "orders_settled": 1},
        )

        self.store.insert_vision_observation(VisionObservation(
            "1001", 1, NOW + timedelta(seconds=5), 605, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "late-original", "game", "team_one",
        ))
        self.store.insert_vision_observation(VisionObservation(
            "1001", 1, NOW + timedelta(seconds=10), 610, False,
            (1, 2, 3, 4, 6), (5, 7, 8, 9, 10),
            0.95, 0.95, "late-conflict", "game", "team_one",
        ))

        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("manual_review", "vision_draft_conflict"),
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT result, review_required FROM settlements"
            ).fetchone()),
            ("win", 1),
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                """SELECT status, last_error FROM notification_outbox
                    WHERE event_type='settled'"""
            ).fetchone()),
            ("dead_letter", "vision_draft_conflict"),
        )

    def test_draft_conflict_after_signal_forces_manual_review(self) -> None:
        self.insert_filled_order(
            filled_at=NOW + timedelta(seconds=15)
        )
        original = VisionObservation(
            "1001", 1, NOW + timedelta(seconds=6), 606, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "original-future", "game", "team_one",
        )
        conflicting = VisionObservation(
            "1001", 1, NOW + timedelta(seconds=10), 610, False,
            (1, 2, 3, 4, 6), (5, 7, 8, 9, 10),
            0.95, 0.95, "conflict-future", "game", "team_one",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, filled_at FROM shadow_orders WHERE order_key=?",
                (self.order_key,),
            ).fetchone()),
            ("filled", (NOW + timedelta(seconds=15)).isoformat()),
        )
        result = self.opendota_result(
            settled_at=NOW + timedelta(seconds=20)
        )
        final = self.latest_stored_raybet_final(
            raybet_final_payload(), observed_at=result.settled_at
        )

        outcome = _reconcile_and_settle(self.store, result, final)

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        self.assertEqual(
            self.store.connection.execute(
                "SELECT review_required FROM settlements WHERE order_key=?",
                (self.order_key,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("manual_review", "vision_draft_conflict"),
        )

    def test_pending_reconciliation_cannot_confirm_after_draft_conflict(self) -> None:
        self.insert_filled_order()
        pending_at = NOW + timedelta(seconds=1)
        pending_result = self.opendota_result(settled_at=pending_at)
        pending_final = self.latest_stored_raybet_final(
            raybet_final_payload(market_winner=None),
            observed_at=pending_at,
        )
        self.assertEqual(
            _reconcile_and_settle(self.store, pending_result, pending_final),
            {"status": "pending", "orders_settled": 0},
        )

        self.store.insert_vision_observation(VisionObservation(
            "1001", 1, NOW, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "pending-original", "game", "team_one",
        ))
        self.store.insert_vision_observation(VisionObservation(
            "1001", 1, NOW + timedelta(seconds=2), 602, False,
            (1, 2, 3, 4, 6), (5, 7, 8, 9, 10),
            0.95, 0.95, "pending-conflict", "game", "team_one",
        ))
        confirmed_at = NOW + timedelta(seconds=3)
        confirmed_result = replace(pending_result, settled_at=confirmed_at)
        confirmed_final = self.latest_stored_raybet_final(
            raybet_final_payload(), observed_at=confirmed_at
        )

        self.assertEqual(
            _reconcile_and_settle(self.store, confirmed_result, confirmed_final),
            {"status": "manual_review", "orders_settled": 0},
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("manual_review", "vision_draft_conflict"),
        )

    def test_label_once_reviews_settlement_after_future_draft_conflict(self) -> None:
        self.insert_filled_order()
        original = VisionObservation(
            "1001", 1, NOW, 600, False,
            (1, 2, 3, 4, 5), (6, 7, 8, 9, 10),
            0.95, 0.95, "original-label", "game", "team_one",
        )
        conflicting = VisionObservation(
            "1001", 1, NOW + timedelta(seconds=10), 610, False,
            (1, 2, 3, 4, 6), (5, 7, 8, 9, 10),
            0.95, 0.95, "conflict-label", "game", "team_one",
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.store.connection.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                clock_confidence, draft_confidence, source_frame_ref,
                screen_state, confirmed)
               VALUES ('1001', 1, ?, 620, 0, '[11,12,13,14,15]',
                       '[16,17,18,19,20]', 'team_one', 0.99, 0.99,
                       'untrusted-late-confirmed', 'game', 1)""",
            ((NOW + timedelta(seconds=20)).isoformat(),),
        )
        self.store.connection.commit()

        class FakeOpenDotaClient:
            async def get_team_matches(self, team_id: int) -> list[dict[str, int]]:
                self.team_id = team_id
                return [
                    {
                        "match_id": 9001,
                        "start_time": int(
                            datetime(
                                2026, 4, 20, 4, 0, tzinfo=timezone.utc
                            ).timestamp()
                        ),
                    }
                ]

            async def get_match(self, match_id: int) -> dict[str, object]:
                self.match_id = match_id
                return {
                    "match_id": 9001,
                    "leagueid": 19543,
                    "radiant_team_id": 101,
                    "dire_team_id": 202,
                    "radiant_win": True,
                    "radiant_score": 30,
                    "dire_score": 20,
                    "duration": 2400,
                    "players": [
                        *(
                            {"player_slot": slot, "hero_id": slot + 1}
                            for slot in range(5)
                        ),
                        *(
                            {"player_slot": slot, "hero_id": slot - 122}
                            for slot in range(128, 133)
                        ),
                    ],
                }

        with tempfile.TemporaryDirectory() as archive_dir:
            outcome = asyncio.run(
                label_once(
                    self.store,
                    FakeOpenDotaClient(),  # type: ignore[arg-type]
                    RawArchive(Path(archive_dir)),
                    "1001",
                    101,
                    "team_one",
                )
            )

        self.assertEqual(
            outcome,
            {
                "status": "labeled",
                "maps": 0,
                "orders_settled": 0,
                "settlement_pending": 0,
                "settlement_manual_review": 1,
            },
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT result, review_required FROM settlements WHERE order_key=?",
                (self.order_key,),
            ).fetchone()),
            ("review", 1),
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM vision_derived_invalidations
                    WHERE dependent_type='shadow_order'
                      AND dependent_key=?""",
                (self.order_key,),
            ).fetchone()[0],
            0,
        )

    def test_existing_review_settlement_cannot_be_reconciled_as_confirmed(self) -> None:
        self.insert_filled_order()
        self.assertTrue(
            self.store.insert_settlement(
                self.order_key, "review", 0.0, NOW, "legacy-review", True
            )
        )
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)

        outcome = _reconcile_and_settle(self.store, self.opendota_result(), final)

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("manual_review", "existing_settlement_review"),
        )

    def test_raybet_final_refresh_archives_before_normalization(self) -> None:
        payload = raybet_final_payload()
        payload["odds"] = [
            {**row, "odds": 2.0 if row["team_id"] == 101 else 1.8}
            for row in payload["odds"]  # type: ignore[union-attr]
        ]
        response = {"result": payload}

        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                self.match_id = match_id
                return response

        with patch(
            "event_intelligence.raw_archive.gzip.compress",
            wraps=gzip.compress,
        ) as compress:
            refreshed, observed_at = _refresh_raybet_final(
                self.store, Client(), "1001"
            )
        self.assertEqual(compress.call_count, 1)
        self.assertEqual(refreshed["id"], "1001")
        self.assertIsNotNone(observed_at)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM odds_transport_observations "
                "WHERE raybet_match_id='1001'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM odds_response_outcomes_effective "
                "WHERE raybet_match_id='1001'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT response_kind, disposition, reason "
                "FROM direct_response_audit"
            ).fetchone()),
            ("final_odds", "audit_only", "final_result_evidence"),
        )
        files = list(self.store.raw_archive_root.rglob("*.json.gz"))
        self.assertEqual(len(files), 1)

    def test_raybet_identity_conflict_is_archived_but_not_normalized(self) -> None:
        response = {"result": {**raybet_final_payload(), "id": "9999"}}

        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                return response

        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            _refresh_raybet_final(self.store, Client(), "1001")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM odds_transport_observations"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT response_kind, observed_raybet_match_id, disposition, reason "
                "FROM direct_response_audit"
            ).fetchone()),
            ("final_odds", "9999", "rejected", "identity_mismatch"),
        )
        self.assertEqual(
            len(list(self.store.raw_archive_root.rglob("*.json.gz"))),
            1,
        )

    def test_raybet_final_request_failure_is_replayable(self) -> None:
        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                raise TimeoutError("upstream secret detail")

        with self.assertRaises(TimeoutError):
            _refresh_raybet_final(self.store, Client(), "1001")
        audit = self.store.connection.execute(
            """SELECT audit_key, response_kind, claimed_raybet_match_id,
                      disposition, reason FROM direct_response_audit"""
        ).fetchone()
        self.assertEqual(
            tuple(audit[1:]),
            (
                "final_odds",
                "1001",
                "rejected",
                "request_failed:TimeoutError",
            ),
        )
        self.assertEqual(
            self.store.direct_response_payload(str(audit[0])),
            {
                "artifact_version": "raybet-direct-request-failure-v1",
                "claimed_raybet_match_id": "1001",
                "failure": {"error_type": "TimeoutError"},
                "response_kind": "final_odds",
            },
        )
        self.assertEqual(
            len(list(self.store.raw_archive_root.rglob("*.json.gz"))),
            1,
        )

    def test_agreement_persists_evidence_settlement_and_result_mail(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        self.assertIsNotNone(final.transport_key)
        self.assertIsNotNone(final.response_state_hash)
        self.assertIsNotNone(final.response_artifact_hash)

        outcome = _reconcile_and_settle(self.store, self.opendota_result(), final)

        self.assertEqual(outcome, {"status": "confirmed", "orders_settled": 1})
        reconciliation = self.store.connection.execute(
            """SELECT status, raybet_winner_side, opendota_winner_side
                 FROM settlement_reconciliations"""
        ).fetchone()
        self.assertEqual(tuple(reconciliation), ("confirmed", "team_one", "team_one"))
        settlement = self.store.connection.execute(
            "SELECT result, return_units, review_required FROM settlements"
        ).fetchone()
        self.assertEqual(tuple(settlement), ("win", 2.0, 0))
        self.assertEqual(
            self.store.connection.execute(
                """SELECT event_type FROM notification_outbox
                    WHERE event_type='settled'"""
            ).fetchone()[0],
            "settled",
        )

    def test_result_at_fill_timestamp_requires_manual_review(self) -> None:
        self.insert_filled_order(filled_at=NOW)
        final = self.latest_stored_raybet_final(
            raybet_final_payload(), observed_at=NOW
        )

        outcome = _reconcile_and_settle(
            self.store,
            self.opendota_result(settled_at=NOW),
            final,
        )

        self.assertEqual(
            outcome,
            {"status": "manual_review", "orders_settled": 0},
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT result, review_required FROM settlements"
            ).fetchone()),
            ("review", 1),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM settlement_authority"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT reason FROM settlement_authority_audit
                    WHERE order_key=? ORDER BY audit_id LIMIT 1""",
                (self.order_key,),
            ).fetchone()[0],
            "settlement_time_order_invalid",
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE order_key=? AND event_type='settled'""",
                (self.order_key,),
            ).fetchone()[0],
            0,
        )
        trigger_sql = self.store.connection.execute(
            """SELECT sql FROM sqlite_master
                WHERE type='trigger' AND name='settlement_authority_insert_guard'"""
        ).fetchone()[0]
        self.assertIn(
            "julianday(orders.filled_at)<julianday(result.settled_at)",
            "".join(str(trigger_sql).split()),
        )
        self.assertEqual(
            "".join(str(trigger_sql).split()).count(
                "julianday(evidence.first_usable_at)>"
                "julianday(orders.filled_at)"
            ),
            2,
        )

    def test_conflict_persists_both_facts_without_result_mail(self) -> None:
        self.insert_filled_order()
        final = parse_raybet_map_final(
            raybet_final_payload(score_winner="team_two", market_winner="team_two"),
            1,
            observed_at=NOW,
        )

        outcome = _reconcile_and_settle(self.store, self.opendota_result(), final)

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        reconciliation = self.store.connection.execute(
            """SELECT status, raybet_winner_side, opendota_winner_side, reason
                 FROM settlement_reconciliations"""
        ).fetchone()
        self.assertEqual(
            tuple(reconciliation),
            ("manual_review", "team_two", "team_one", "winner_conflict"),
        )
        settlement = self.store.connection.execute(
            "SELECT result, return_units, review_required FROM settlements"
        ).fetchone()
        self.assertEqual(tuple(settlement), ("review", 0.0, 1))
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE event_type='settled'"""
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM map_results").fetchone()[0],
            0,
        )

    def test_missing_exact_raybet_order_outcome_stays_pending(self) -> None:
        self.insert_filled_order(odds_id="not-in-final-payload")
        final = self.latest_stored_raybet_final(raybet_final_payload())

        outcome = _reconcile_and_settle(self.store, self.opendota_result(), final)

        self.assertEqual(outcome, {"status": "pending", "orders_settled": 0})
        row = self.store.connection.execute(
            "SELECT status, reason FROM settlement_reconciliations"
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", "raybet_order_outcome_missing"))
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0],
            0,
        )

    def test_order_market_map_mismatch_requires_manual_review(self) -> None:
        self.insert_filled_order()
        self.store.connection.execute("DROP TRIGGER shadow_orders_terminal_immutable")
        self.store.connection.execute(
            "DROP TRIGGER shadow_orders_signal_identity_immutable"
        )
        self.store.connection.execute(
            "UPDATE shadow_orders SET market_key='winner|map_2|team_one|' "
            "WHERE order_key=?",
            (self.order_key,),
        )
        self.store.connection.commit()
        final = self.latest_stored_raybet_final(raybet_final_payload())

        outcome = _reconcile_and_settle(self.store, self.opendota_result(), final)

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("manual_review", "order_market_identity_invalid"),
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT result, review_required FROM settlements"
            ).fetchone()),
            ("review", 1),
        )

    def test_manual_review_is_not_silently_cleared_by_later_poll(self) -> None:
        self.insert_filled_order()
        conflict = parse_raybet_map_final(
            raybet_final_payload(score_winner="team_two", market_winner="team_two"),
            1,
            observed_at=NOW,
        )
        _reconcile_and_settle(self.store, self.opendota_result(), conflict)
        matching = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)

        outcome = _reconcile_and_settle(self.store, self.opendota_result(), matching)

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        row = self.store.connection.execute(
            "SELECT status, reason FROM settlement_reconciliations"
        ).fetchone()
        self.assertEqual(tuple(row), ("manual_review", "winner_conflict"))

    def test_agreement_replay_is_idempotent(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        result = self.opendota_result()
        self.assertEqual(
            _reconcile_and_settle(self.store, result, final)["orders_settled"], 1
        )

        replay = _reconcile_and_settle(self.store, result, final)

        self.assertEqual(replay, {"status": "confirmed", "orders_settled": 0})
        for table in (
            "settlement_reconciliations",
            "settlements",
            "map_results",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    1,
                )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM notification_outbox"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM settlement_result_evidence"
            ).fetchone()[0],
            2,
        )

    def test_notification_failure_rolls_back_complete_reconciliation(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())

        with patch.object(
            self.store,
            "enqueue_notification",
            side_effect=RuntimeError("outbox failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "outbox failure"):
                _reconcile_and_settle(self.store, self.opendota_result(), final)

        for table in (
            "settlement_result_evidence",
            "settlement_reconciliations",
            "settlements",
            "map_results",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE event_type='filled'"""
            ).fetchone()[0],
            1,
        )

    def test_reconciliation_first_usable_uses_later_opendota_availability(
        self,
    ) -> None:
        self.insert_filled_order()
        opendota_observed = NOW + timedelta(seconds=1)
        first_usable = NOW + timedelta(seconds=2)
        final = self.latest_stored_raybet_final(
            raybet_final_payload(), observed_at=NOW + timedelta(seconds=1)
        )
        result = self.opendota_result(
            settled_at=first_usable,
            observed_at=opendota_observed,
            first_usable_at=first_usable,
        )

        self.assertEqual(
            _reconcile_and_settle(self.store, result, final),
            {"status": "confirmed", "orders_settled": 1},
        )
        row = self.store.connection.execute(
            """SELECT raybet_observed_at, opendota_observed_at,
                      first_usable_at FROM settlement_reconciliations"""
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                (NOW + timedelta(seconds=1)).isoformat(),
                opendota_observed.isoformat(),
                first_usable.isoformat(),
            ),
        )

    def test_reconciliation_first_usable_uses_later_raybet_observation(
        self,
    ) -> None:
        self.insert_filled_order()
        opendota_first_usable = NOW + timedelta(seconds=1)
        first_usable = NOW + timedelta(seconds=2)
        final = self.latest_stored_raybet_final(
            raybet_final_payload(), observed_at=first_usable
        )
        result = self.opendota_result(
            settled_at=first_usable,
            observed_at=NOW,
            first_usable_at=opendota_first_usable,
        )

        self.assertEqual(
            _reconcile_and_settle(self.store, result, final),
            {"status": "confirmed", "orders_settled": 1},
        )
        row = self.store.connection.execute(
            """SELECT raybet_observed_at, opendota_observed_at,
                      first_usable_at FROM settlement_reconciliations"""
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (first_usable.isoformat(), NOW.isoformat(), first_usable.isoformat()),
        )

    def test_fake_opendota_evidence_ref_is_not_formal_authority(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        result = replace(
            self.opendota_result(),
            evidence_ref="opendota:9001:sha256:" + "0" * 64,
        )

        self.assertEqual(
            _reconcile_and_settle(self.store, result, final),
            {"status": "manual_review", "orders_settled": 0},
        )
        row = self.store.connection.execute(
            "SELECT status, reason FROM settlement_reconciliations"
        ).fetchone()
        self.assertEqual(tuple(row), ("manual_review", "source_authority_invalid"))
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM map_results").fetchone()[0],
            0,
        )

    def test_direct_insert_rejects_fake_opendota_evidence_ref(self) -> None:
        result = self.opendota_result()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "settlement source evidence authority is required",
        ):
            self.store.connection.execute(
                """INSERT INTO settlement_result_evidence
                   (raybet_match_id, map_number, dota_match_id, source, status,
                    winner_side, evidence_ref, facts_json, observed_at,
                    first_usable_at, opendota_artifact_id,
                    opendota_observation_id, opendota_content_hash)
                   VALUES ('1001', 1, 9001, 'opendota', 'confirmed',
                           'team_one', ?, '{}', ?, ?, ?, ?, ?)""",
                (
                    "opendota:9001:sha256:" + "0" * 64,
                    result.opendota_observed_at.isoformat(),
                    result.opendota_first_usable_at.isoformat(),
                    result.opendota_artifact_id,
                    result.opendota_observation_id,
                    result.opendota_content_hash,
                ),
            )

    def _assert_source_authority_rejected(
        self,
        *,
        final_changes: dict[str, object] | None = None,
        result_changes: dict[str, object] | None = None,
    ) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        result = self.opendota_result()
        if final_changes:
            final = replace(final, **final_changes)
        if result_changes:
            result = replace(result, **result_changes)
        self.assertEqual(
            _reconcile_and_settle(self.store, result, final),
            {"status": "manual_review", "orders_settled": 0},
        )
        row = self.store.connection.execute(
            "SELECT status, reason FROM settlement_reconciliations"
        ).fetchone()
        self.assertEqual(tuple(row), ("manual_review", "source_authority_invalid"))
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM map_results").fetchone()[0],
            0,
        )

    def test_raybet_final_audit_ref_must_match_raw_artifact(self) -> None:
        self._assert_source_authority_rejected(
            final_changes={"audit_key": "0" * 64}
        )

    def test_raybet_final_transport_refs_are_all_or_none(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        self.assertIsNotNone(final.transport_key)
        partial = replace(final, response_state_hash=None)

        self.assertEqual(
            _reconcile_and_settle(self.store, self.opendota_result(), partial),
            {"status": "manual_review", "orders_settled": 0},
        )
        row = self.store.connection.execute(
            "SELECT status, reason FROM settlement_reconciliations"
        ).fetchone()
        self.assertEqual(tuple(row), ("manual_review", "source_authority_missing"))

    def test_raybet_final_state_ref_must_match_transport(self) -> None:
        self._assert_source_authority_rejected(
            final_changes={"response_state_hash": "0" * 64}
        )

    def test_raybet_final_artifact_ref_must_match_audit(self) -> None:
        self._assert_source_authority_rejected(
            final_changes={"response_artifact_hash": "0" * 64}
        )

    def test_raybet_final_raw_file_is_reverified(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        row = self.store.connection.execute(
            "SELECT storage_path FROM odds_raw_artifacts WHERE artifact_hash=?",
            (final.response_artifact_hash,),
        ).fetchone()
        artifact_path = self.store.raw_archive_root / str(row["storage_path"])
        artifact_path.write_bytes(b"corrupt")

        self.assertEqual(
            _reconcile_and_settle(self.store, self.opendota_result(), final),
            {"status": "manual_review", "orders_settled": 0},
        )
        reason = self.store.connection.execute(
            "SELECT reason FROM settlement_reconciliations"
        ).fetchone()[0]
        self.assertEqual(reason, "source_authority_invalid")

    def test_opendota_artifact_ref_must_match_observation(self) -> None:
        self._assert_source_authority_rejected(
            result_changes={"opendota_artifact_id": "opendota:" + "0" * 64}
        )

    def test_opendota_observation_ref_must_match_artifact(self) -> None:
        self._assert_source_authority_rejected(
            result_changes={"opendota_observation_id": "0" * 64}
        )

    def test_opendota_content_hash_must_match_raw_artifact(self) -> None:
        self._assert_source_authority_rejected(
            result_changes={"opendota_content_hash": "0" * 64}
        )

    def test_opendota_raw_match_identity_is_required(self) -> None:
        self._assert_source_authority_rejected(
            result_changes={"dota_match_id": 9002}
        )

    def test_opendota_availability_must_match_raw_registry(self) -> None:
        later = NOW + timedelta(seconds=2)
        self._assert_source_authority_rejected(
            result_changes={
                "settled_at": later,
                "opendota_first_usable_at": later,
            }
        )

    def test_direct_insert_rejects_partial_raybet_transport_refs(self) -> None:
        final = self.latest_stored_raybet_final(raybet_final_payload())
        self.assertIsNotNone(final.transport_key)
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "settlement source evidence authority is required",
        ):
            self.store.connection.execute(
                """INSERT INTO settlement_result_evidence
                   (raybet_match_id, map_number, dota_match_id, source, status,
                    winner_side, evidence_ref, facts_json, observed_at,
                    first_usable_at, raybet_audit_key,
                    raybet_transport_key, raybet_response_artifact_hash)
                   VALUES ('1001', 1, 9001, 'raybet', 'confirmed',
                           'team_one', ?, '{}', ?, ?, ?, ?, ?)""",
                (
                    final.evidence_ref,
                    final.observed_at.isoformat(),
                    final.observed_at.isoformat(),
                    final.audit_key,
                    final.transport_key,
                    final.response_artifact_hash,
                ),
            )

    def test_pending_reconciliation_cannot_confirm_without_source_refs(self) -> None:
        mapping_id = self.ensure_strict_mapping()
        self.store.connection.execute(
            """INSERT INTO settlement_reconciliations
               (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                raybet_winner_side, opendota_winner_side,
                raybet_evidence_ref, opendota_evidence_ref, status, reason,
                first_observed_at, updated_at)
               VALUES ('1001', 1, ?, 9001, 'team_one', 'team_one',
                       'raybet:pending', 'opendota:pending', 'pending',
                       'waiting_for_source_authority', ?, ?)""",
            (mapping_id, NOW.isoformat(), NOW.isoformat()),
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "settlement reconciliation source authority is required",
        ):
            self.store.connection.execute(
                """UPDATE settlement_reconciliations SET status='confirmed'
                    WHERE raybet_match_id='1001' AND map_number=1"""
            )

    def test_confirmed_reconciliation_source_authority_is_immutable(self) -> None:
        self.ensure_strict_mapping()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        self.assertEqual(
            _reconcile_and_settle(self.store, self.opendota_result(), final),
            {"status": "confirmed", "orders_settled": 0},
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.store.connection.execute(
                """UPDATE settlement_reconciliations
                      SET raybet_evidence_ref='forged'
                    WHERE raybet_match_id='1001' AND map_number=1"""
            )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM notification_outbox
                    WHERE event_type='settled'"""
            ).fetchone()[0],
            0,
        )

    def test_later_source_conflict_flags_existing_settlement(self) -> None:
        self.insert_filled_order()
        matching = self.latest_stored_raybet_final(raybet_final_payload())
        result = self.opendota_result()
        _reconcile_and_settle(self.store, result, matching)
        changed = self.latest_stored_raybet_final(
            raybet_final_payload(score_winner="team_two", market_winner="team_two"),
            observed_at=NOW + timedelta(seconds=1),
        )

        outcome = _reconcile_and_settle(self.store, result, changed)

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("manual_review", "source_result_changed"),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT review_required FROM settlements"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM settlement_result_evidence"
            ).fetchone()[0],
            3,
        )

    def test_duplicate_opendota_link_flags_both_maps_for_review(self) -> None:
        self.insert_filled_order()
        first_final = self.latest_stored_raybet_final(raybet_final_payload())
        _reconcile_and_settle(self.store, self.opendota_result(), first_final)
        second_payload = {**raybet_final_payload(), "id": "1002"}
        self.ensure_strict_mapping("1002")
        second_final = self.latest_stored_raybet_final(second_payload)
        second_result = self.opendota_result(raybet_match_id="1002")

        outcome = _reconcile_and_settle(self.store, second_result, second_final)

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        rows = self.store.connection.execute(
            """SELECT raybet_match_id, status, reason
                 FROM settlement_reconciliations ORDER BY raybet_match_id"""
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("1001", "manual_review", "opendota_match_link_conflict"),
                ("1002", "manual_review", "opendota_match_link_conflict"),
            ],
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT review_required FROM settlements WHERE order_key=?",
                (self.order_key,),
            ).fetchone()[0],
            1,
        )

    def test_orphan_map_result_is_rejected(self) -> None:
        mapping_id = self.ensure_strict_mapping()
        first_result = self.opendota_result()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "map result mapping authority is required"
        ):
            self.store.connection.execute(
                """INSERT INTO map_results
                   (raybet_match_id, map_number, strict_mapping_id,
                    dota_match_id, winner_side, team_one_kills,
                    team_two_kills, duration_seconds, evidence_ref, settled_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    first_result.raybet_match_id,
                    first_result.map_number,
                    mapping_id,
                    first_result.dota_match_id,
                    first_result.winner_side,
                    first_result.team_one_kills,
                    first_result.team_two_kills,
                    first_result.duration_seconds,
                    first_result.evidence_ref,
                    first_result.settled_at.isoformat(),
                ),
            )

    def test_map_result_insert_failure_cannot_continue_settlement(self) -> None:
        self.insert_filled_order()
        final = self.latest_stored_raybet_final(raybet_final_payload())

        with patch.object(self.store, "insert_map_result", return_value=False):
            outcome = _reconcile_and_settle(
                self.store, self.opendota_result(), final
            )

        self.assertEqual(
            outcome, {"status": "manual_review", "orders_settled": 0}
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, reason FROM settlement_reconciliations"
            ).fetchone()),
            ("manual_review", "map_result_persistence_conflict"),
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT result, review_required FROM settlements"
            ).fetchone()),
            ("review", 1),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM map_results"
            ).fetchone()[0],
            0,
        )

    def test_source_evidence_is_append_only(self) -> None:
        self.ensure_strict_mapping()
        final = self.latest_stored_raybet_final(raybet_final_payload())
        _reconcile_and_settle(self.store, self.opendota_result(), final)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.connection.execute(
                "UPDATE settlement_result_evidence SET facts_json='{}'"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.connection.execute("DELETE FROM settlement_result_evidence")


class SettlementMigrationTests(unittest.TestCase):
    def test_additive_schema_preserves_legacy_settlement_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute(
                """CREATE TABLE settlements (
                       order_key TEXT PRIMARY KEY,
                       result TEXT NOT NULL,
                       return_units REAL NOT NULL,
                       settled_at TEXT NOT NULL,
                       evidence_ref TEXT NOT NULL,
                       review_required INTEGER NOT NULL DEFAULT 0
                   )"""
            )
            connection.execute(
                """INSERT INTO settlements VALUES
                   ('legacy', 'win', 2.0, ?, 'legacy-source', 0)""",
                (NOW.isoformat(),),
            )
            connection.commit()
            connection.close()

            with LiveBettingStore(path) as store:
                store.init_schema()
                row = store.connection.execute(
                    "SELECT * FROM settlements WHERE order_key='legacy'"
                ).fetchone()
                tables = {
                    item[0]
                    for item in store.connection.execute(
                        """SELECT name FROM sqlite_master
                            WHERE type='table' AND name IN (
                                'settlement_result_evidence',
                                'settlement_reconciliations'
                            )"""
                    )
                }

            self.assertEqual(
                tuple(row),
                ("legacy", "win", 2.0, NOW.isoformat(), "legacy-source", 0),
            )
            self.assertEqual(
                tables,
                {"settlement_result_evidence", "settlement_reconciliations"},
            )


if __name__ == "__main__":
    unittest.main()
