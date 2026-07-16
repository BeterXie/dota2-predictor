from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from event_intelligence.raw_archive import RawArchive
from event_intelligence.storage import IntelligenceStorage
from live_betting.postmatch_monitor import (
    StoredMapResult,
    VisionDraftIdentity,
    _latest_exact_raybet_final,
    _reconcile_and_settle,
    _refresh_raybet_final,
    _vision_drafts,
    label_once,
)
from live_betting.raybet import RayBetMapFinal, parse_raybet_map_final
from live_betting.settlement import reconcile_map_winners
from live_betting.storage import LiveBettingStore
from live_betting.strict_eligibility import (
    accept_strict_live_map_mapping,
    invalidate_strict_live_map_mapping,
)
from live_betting.vision import VisionObservation


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

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def latest_stored_raybet_final(
        self, payload: dict[str, object]
    ) -> RayBetMapFinal:
        odds = payload.get("odds")
        self.assertIsInstance(odds, list)
        assert isinstance(odds, list)
        for index, row in enumerate(odds):
            self.assertIsInstance(row, dict)
            assert isinstance(row, dict)
            row.setdefault("odds", 2.0 + index / 100)
        response = {"result": payload}

        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                return response

        _refresh_raybet_final(
            self.store,
            RawArchive(Path(self.tempdir.name) / "raybet-raw"),
            Client(),  # type: ignore[arg-type]
            "1001",
        )
        final = _latest_exact_raybet_final(
            self.store, "1001", 1, team_ids=(101, 202)
        )
        self.assertIsNotNone(final)
        assert final is not None
        return final

    def ensure_strict_mapping(self) -> int:
        existing = self.store.connection.execute(
            "SELECT mapping_id FROM strict_live_map_mappings WHERE mapping_id=1"
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
        self.store.upsert_raybet_match(raybet_final_payload(), metadata_at)
        evidence = {
            "kind": "manual_cross_source_review",
            "raybet_url": "https://example.invalid/raybet/1001",
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
                raybet_match_id="1001",
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
    ) -> None:
        strict_mapping_id = self.ensure_strict_mapping()
        self.store.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, odds_id, market_key, signaled_at,
                model_probability, market_probability, signal_price,
                signal_transport_key, signal_transport_at, expires_at,
                signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, strict_mapping_id, stake, status,
                fill_price, filled_at)
               VALUES ('order-1', '1001', ?, ?, ?, 0.6, 0.5, 2.0,
                       'signal', ?, ?, 'winner-group', 'team_one', 1,
                       ?, 1.0, 'filled', 2.0, ?)""",
            (
                odds_id,
                market_key,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                strict_mapping_id,
                NOW.isoformat(),
            ),
        )
        self.store.connection.execute(
            """INSERT INTO shadow_map_attempts
               (raybet_match_id, map_number, order_key, status, created_at)
               VALUES ('1001', 1, 'order-1', 'filled', ?)""",
            (NOW.isoformat(),),
        )
        self.store.connection.commit()

    @staticmethod
    def opendota_result(*, winner: str = "team_one") -> StoredMapResult:
        return StoredMapResult(
            "1001", 1, 9001, winner, 30, 20, 2400,
            "opendota:9001", NOW,
        )

    def test_latest_exact_final_preserves_score_without_normalized_rows(self) -> None:
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
               VALUES ('1001', ?, 'original-invalidated', ?, 'bad frame')""",
            (NOW.isoformat(), (NOW + timedelta(seconds=1)).isoformat()),
        )
        self.store.connection.execute(
            """UPDATE vision_observations SET confirmed=0
                WHERE raybet_match_id='1001'
                  AND source_frame_ref='original-invalidated'"""
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
            "notification_outbox",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
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
               VALUES ('1001', ?, 'original-invalidated', ?, 'bad frame')""",
            (NOW.isoformat(), (NOW + timedelta(seconds=1)).isoformat()),
        )
        self.store.connection.execute(
            """UPDATE vision_observations SET confirmed=0
                WHERE raybet_match_id='1001'
                  AND source_frame_ref='original-invalidated'"""
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
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)

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
                "SELECT COUNT(*) FROM notification_outbox"
            ).fetchone()[0],
            1,
        )

    def test_later_draft_conflict_preserves_prior_settlement(self) -> None:
        self.insert_filled_order()
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)
        result = self.opendota_result()
        self.assertEqual(
            _reconcile_and_settle(self.store, result, final),
            {"status": "confirmed", "orders_settled": 1},
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM notification_outbox"
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
                "SELECT review_required FROM settlements WHERE order_key='order-1'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            tuple(self.store.connection.execute(
                "SELECT status, last_error FROM notification_outbox"
            ).fetchone()),
            ("pending", None),
        )

    def test_late_arriving_conflict_reviews_later_settlement(self) -> None:
        self.insert_filled_order()
        settled_at = NOW + timedelta(seconds=20)
        result = StoredMapResult(
            "1001", 1, 9001, "team_one", 30, 20, 2400,
            "opendota:9001", settled_at,
        )
        final = parse_raybet_map_final(
            raybet_final_payload(), 1, observed_at=settled_at
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
                "SELECT status, last_error FROM notification_outbox"
            ).fetchone()),
            ("dead_letter", "vision_draft_conflict"),
        )

    def test_draft_conflict_after_signal_forces_manual_review(self) -> None:
        self.insert_filled_order()
        self.store.connection.execute(
            "UPDATE shadow_orders SET filled_at=? WHERE order_key='order-1'",
            ((NOW + timedelta(seconds=15)).isoformat(),),
        )
        self.store.connection.commit()
        original = VisionObservation(
            "1001", 1, NOW, 600, False,
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
                "SELECT status, filled_at FROM shadow_orders WHERE order_key='order-1'"
            ).fetchone()),
            ("filled", (NOW + timedelta(seconds=15)).isoformat()),
        )
        result = StoredMapResult(
            "1001", 1, 9001, "team_one", 30, 20, 2400,
            "opendota:9001", NOW + timedelta(seconds=20),
        )
        final = parse_raybet_map_final(
            raybet_final_payload(), 1, observed_at=result.settled_at
        )

        outcome = _reconcile_and_settle(self.store, result, final)

        self.assertEqual(outcome, {"status": "manual_review", "orders_settled": 0})
        self.assertEqual(
            self.store.connection.execute(
                "SELECT review_required FROM settlements WHERE order_key='order-1'"
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
        pending_result = StoredMapResult(
            "1001", 1, 9001, "team_one", 30, 20, 2400,
            "opendota:9001", pending_at,
        )
        pending_final = parse_raybet_map_final(
            raybet_final_payload(market_winner=None),
            1,
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
        confirmed_result = StoredMapResult(
            "1001", 1, 9001, "team_one", 30, 20, 2400,
            "opendota:9001", confirmed_at,
        )
        confirmed_final = parse_raybet_map_final(
            raybet_final_payload(), 1, observed_at=confirmed_at
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
                "SELECT result, review_required FROM settlements WHERE order_key='order-1'"
            ).fetchone()),
            ("review", 1),
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM vision_derived_invalidations
                    WHERE dependent_type='shadow_order'
                      AND dependent_key='order-1'"""
            ).fetchone()[0],
            0,
        )

    def test_existing_review_settlement_cannot_be_reconciled_as_confirmed(self) -> None:
        self.insert_filled_order()
        self.assertTrue(
            self.store.insert_settlement(
                "order-1", "review", 0.0, NOW, "legacy-review", True
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

        with tempfile.TemporaryDirectory() as directory:
            archive = RawArchive(Path(directory) / "raw")
            refreshed, observed_at = _refresh_raybet_final(
                self.store, archive, Client(), "1001"
            )
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
                    "SELECT COUNT(*) FROM odds_response_outcomes "
                    "WHERE raybet_match_id='1001'"
                ).fetchone()[0],
                2,
            )
            files = list((Path(directory) / "raw" / "raybet").rglob("*.json.gz"))
            self.assertEqual(len(files), 1)

    def test_raybet_identity_conflict_is_archived_but_not_normalized(self) -> None:
        response = {"result": {**raybet_final_payload(), "id": "9999"}}

        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                return response

        with tempfile.TemporaryDirectory() as directory:
            archive = RawArchive(Path(directory) / "raw")
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                _refresh_raybet_final(self.store, archive, Client(), "1001")
            self.assertEqual(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM odds_transport_observations"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                len(list((Path(directory) / "raw" / "raybet").rglob("*.json.gz"))),
                1,
            )

    def test_agreement_persists_evidence_settlement_and_result_mail(self) -> None:
        self.insert_filled_order()
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)

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
                "SELECT event_type FROM notification_outbox"
            ).fetchone()[0],
            "settled",
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
                "SELECT COUNT(*) FROM notification_outbox"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM map_results").fetchone()[0],
            0,
        )

    def test_missing_exact_raybet_order_outcome_stays_pending(self) -> None:
        self.insert_filled_order(odds_id="not-in-final-payload")
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)

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
        self.insert_filled_order(market_key="winner|map_2|team_one|")
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)

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
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)
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
            "notification_outbox",
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
                "SELECT COUNT(*) FROM settlement_result_evidence"
            ).fetchone()[0],
            2,
        )

    def test_notification_failure_rolls_back_complete_reconciliation(self) -> None:
        self.insert_filled_order()
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)

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
            "notification_outbox",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                )

    def test_later_source_conflict_flags_existing_settlement(self) -> None:
        self.insert_filled_order()
        matching = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)
        result = self.opendota_result()
        _reconcile_and_settle(self.store, result, matching)
        changed = parse_raybet_map_final(
            raybet_final_payload(score_winner="team_two", market_winner="team_two"),
            1,
            observed_at=NOW,
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
        first_final = parse_raybet_map_final(
            raybet_final_payload(), 1, observed_at=NOW
        )
        _reconcile_and_settle(self.store, self.opendota_result(), first_final)
        second_payload = {**raybet_final_payload(), "id": "1002"}
        second_final = parse_raybet_map_final(
            second_payload,
            1,
            observed_at=NOW,
            expected_match_id="1002",
        )
        second_result = StoredMapResult(
            "1002", 1, 9001, "team_one", 30, 20, 2400,
            "opendota:9001", NOW,
        )

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
                "SELECT review_required FROM settlements WHERE order_key='order-1'"
            ).fetchone()[0],
            1,
        )

    def test_orphan_map_result_blocks_duplicate_opendota_link(self) -> None:
        self.insert_filled_order()
        first_result = self.opendota_result()
        self.assertTrue(self.store.insert_map_result(first_result))
        self.assertTrue(
            self.store.insert_settlement(
                "order-1", "win", 2.0, NOW, "orphan-result:9001"
            )
        )
        second_payload = {**raybet_final_payload(), "id": "1002"}
        second_final = parse_raybet_map_final(
            second_payload,
            1,
            observed_at=NOW,
            expected_match_id="1002",
        )
        second_result = replace(first_result, raybet_match_id="1002")

        outcome = _reconcile_and_settle(
            self.store, second_result, second_final
        )

        self.assertEqual(
            outcome, {"status": "manual_review", "orders_settled": 0}
        )
        reconciliation = self.store.connection.execute(
            """SELECT raybet_match_id, status, reason
                 FROM settlement_reconciliations"""
        ).fetchone()
        self.assertEqual(
            tuple(reconciliation),
            ("1002", "manual_review", "opendota_match_link_conflict"),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT review_required FROM settlements WHERE order_key='order-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            [tuple(row) for row in self.store.connection.execute(
                """SELECT raybet_match_id, map_number, dota_match_id
                     FROM map_results"""
            )],
            [("1001", 1, 9001)],
        )

    def test_map_result_insert_failure_cannot_continue_settlement(self) -> None:
        self.insert_filled_order()
        final = parse_raybet_map_final(
            raybet_final_payload(), 1, observed_at=NOW
        )

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
        final = parse_raybet_map_final(raybet_final_payload(), 1, observed_at=NOW)
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
