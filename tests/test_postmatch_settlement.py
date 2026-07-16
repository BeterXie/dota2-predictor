from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from event_intelligence.raw_archive import RawArchive
from live_betting.postmatch_monitor import (
    StoredMapResult,
    _reconcile_and_settle,
    _refresh_raybet_final,
    _vision_drafts,
)
from live_betting.raybet import parse_raybet_map_final
from live_betting.settlement import reconcile_map_winners
from live_betting.storage import LiveBettingStore
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

    def insert_filled_order(
        self,
        *,
        odds_id: str = "winner-one",
        market_key: str = "winner|map_1|team_one|",
    ) -> None:
        self.store.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, odds_id, market_key, signaled_at,
                model_probability, market_probability, signal_price,
                signal_transport_key, signal_transport_at, expires_at,
                signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status, fill_price, filled_at)
               VALUES ('order-1', '1001', ?, ?, ?, 0.6, 0.5, 2.0,
                       'signal', ?, ?, 'winner-group', 'team_one', 1,
                       1.0, 'filled', 2.0, ?)""",
            (
                odds_id,
                market_key,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
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
