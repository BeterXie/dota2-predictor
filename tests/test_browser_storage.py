from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.monitor import collect_once
from live_betting.shadow_monitor import latest_market_state
from live_betting.storage import LiveBettingStore
from live_betting.strategy import attempt_fill


NOW = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)


def snapshot(
    observed_at: datetime,
    price: float,
    *,
    odds_id: str = "winner-one",
    side: str = "team_one",
) -> OddsSnapshot:
    return OddsSnapshot(
        "match-1",
        odds_id,
        "winner-group",
        observed_at,
        price,
        1,
        Market("winner", "map_1", side, None, side, True),
        last_update=f"{price:.2f}",
    )


def browser_event(event_id: str, captured_at: datetime) -> dict[str, object]:
    return {
        "event_id": event_id,
        "schema_version": 1,
        "capture_session_id": "1" * 32,
        "captured_at_utc": captured_at,
        "transport": "fetch",
        "event_type": "odds",
        "raybet_match_id": "match-1",
        "game_id": 151,
        "page_origin": "https://www.ray086.com",
        "page_path": "/sports/esports",
        "source_path": "/v2/odds",
        "payload_hash": "2" * 64,
        "payload_bytes": 2,
        "payload": {},
        "capture_reason": None,
        "extension_version": "0.1.0",
    }


class BrowserSchemaTests(unittest.TestCase):
    def test_transaction_rollback_and_legacy_autocommit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with LiveBettingStore(path) as store:
                store.init_schema()
                store.execute(
                    "INSERT INTO collector_runs (collector, gap_detected) VALUES (?, 0)",
                    ("committed",),
                )
                other = sqlite3.connect(path)
                try:
                    self.assertEqual(
                        other.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0], 1
                    )
                finally:
                    other.close()

                with self.assertRaisesRegex(RuntimeError, "rollback"):
                    with store.transaction():
                        store.execute(
                            "INSERT INTO collector_runs (collector, gap_detected) VALUES (?, 0)",
                            ("rolled-back",),
                        )
                        raise RuntimeError("rollback")
                self.assertIsNone(
                    store.connection.execute(
                        "SELECT 1 FROM collector_runs WHERE collector='rolled-back'"
                    ).fetchone()
                )

    def test_savepoint_rolls_back_only_normalization_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                event = browser_event("a" * 64, NOW)
                with store.transaction():
                    self.assertTrue(
                        store.insert_browser_event(
                            event, received_at=NOW, recognized=True
                        )
                    )
                    with self.assertRaisesRegex(ValueError, "parser"):
                        with store.savepoint("normalization"):
                            store.insert_browser_raybet_match(
                                {"id": "match-1", "tournament_name": "Test"}, NOW
                            )
                            raise ValueError("parser")
                    store.update_browser_event_status(
                        "a" * 64, "error", "normalization_failed"
                    )
                self.assertIsNone(
                    store.connection.execute("SELECT 1 FROM raybet_matches").fetchone()
                )
                row = store.connection.execute(
                    "SELECT processing_status FROM browser_events"
                ).fetchone()
                self.assertEqual(row[0], "error")

    def test_browser_event_is_immutable_and_duplicate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                event = browser_event("b" * 64, NOW)
                self.assertTrue(
                    store.insert_browser_event(event, received_at=NOW, recognized=True)
                )
                self.assertFalse(
                    store.insert_browser_event(event, received_at=NOW, recognized=True)
                )
                self.assertTrue(
                    store.update_browser_event_status("b" * 64, "processed")
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    store.execute(
                        "UPDATE browser_events SET payload_json='[]' WHERE event_id=?",
                        ("b" * 64,),
                    )
                self.assertEqual(
                    store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1
                )


class EventTimeTests(unittest.TestCase):
    def test_parser_uses_explicit_time_and_hashes_normalized_state(self) -> None:
        payload = {
            "result": {
                "id": "match-1",
                "team": [{"team_id": 10, "pos": 1}],
                "odds": [{
                    "id": "winner-one",
                    "team_id": 10,
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "odds": "2.10",
                    "status": 5,
                }],
            }
        }
        rows = snapshots_from_payload(payload, received_at=NOW)
        self.assertEqual(rows[0].received_at, NOW)
        self.assertEqual(normalized_state_hash(rows), normalized_state_hash(list(reversed(rows))))

    def test_late_browser_response_is_audit_and_transport_only(self) -> None:
        t1, t2, t3 = NOW, NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                first = [snapshot(t1, 2.0)]
                unchanged = [snapshot(t3, 2.0)]
                delayed = [snapshot(t2, 2.5)]
                self.assertEqual(
                    store.store_odds_observation(
                        source="direct", observation_key="direct-t1", source_event_id=None,
                        raybet_match_id="match-1", observed_at=t1,
                        normalized_state_hash=normalized_state_hash(first), snapshots=first,
                    ),
                    ("on_time", 1),
                )
                self.assertEqual(
                    store.store_odds_observation(
                        source="direct", observation_key="direct-t3", source_event_id=None,
                        raybet_match_id="match-1", observed_at=t3,
                        normalized_state_hash=normalized_state_hash(unchanged), snapshots=unchanged,
                    ),
                    ("on_time", 0),
                )
                event_id = "c" * 64
                store.insert_browser_event(
                    browser_event(event_id, t2), received_at=t3, recognized=True
                )
                self.assertEqual(
                    store.store_odds_observation(
                        source="browser", observation_key=event_id,
                        source_event_id=event_id, raybet_match_id="match-1",
                        observed_at=t2, normalized_state_hash=normalized_state_hash(delayed),
                        snapshots=delayed,
                    ),
                    ("late", 0),
                )
                self.assertEqual(
                    store.connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0],
                    1,
                )
                transport = store.connection.execute(
                    """SELECT timing_status, processing_status
                       FROM odds_transport_observations WHERE observation_key=?""",
                    (event_id,),
                ).fetchone()
                self.assertEqual(tuple(transport), ("late", "audit_only"))

    def test_latest_state_uses_event_time_not_insertion_id(self) -> None:
        t1, t2, t3 = NOW, NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                store.insert_odds(snapshot(t1, 2.0))
                store.insert_odds(snapshot(t1, 1.8, odds_id="winner-two", side="team_two"))
                store.insert_odds(snapshot(t3, 3.0))
                self.assertTrue(store.insert_odds(snapshot(t2, 2.5)))
                latest = latest_market_state(store.connection, "match-1", 1, as_of=t3)
                prices = {row.odds_id: row.price for row in latest}
                self.assertEqual(prices, {"winner-one": 3.0, "winner-two": 1.8})

    def test_latest_state_excludes_rows_after_as_of(self) -> None:
        t1, t2 = NOW, NOW + timedelta(seconds=2)
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                store.insert_odds(snapshot(t1, 2.0))
                store.insert_odds(snapshot(t1, 1.8, odds_id="winner-two", side="team_two"))
                store.insert_odds(snapshot(t2, 9.0))

                latest = latest_market_state(store.connection, "match-1", 1, as_of=t1)
                prices = {row.odds_id: row.price for row in latest}
                self.assertEqual(prices, {"winner-one": 2.0, "winner-two": 1.8})

    def test_unchanged_later_transport_can_fill(self) -> None:
        t1, t3 = NOW, NOW + timedelta(seconds=3)
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                rows = [snapshot(t1, 2.0)]
                store.store_odds_observation(
                    source="direct", observation_key="direct-t1", source_event_id=None,
                    raybet_match_id="match-1", observed_at=t1,
                    normalized_state_hash=normalized_state_hash(rows), snapshots=rows,
                )
                unchanged = [snapshot(t3, 2.0)]
                store.store_odds_observation(
                    source="direct", observation_key="direct-t3", source_event_id=None,
                    raybet_match_id="match-1", observed_at=t3,
                    normalized_state_hash=normalized_state_hash(unchanged), snapshots=unchanged,
                )
                order = ShadowOrder(
                    "order", "match-1", "winner-one", rows[0].market, t1,
                    0.60, 0.50, 2.0, "direct-t1", t1,
                    t1 + timedelta(seconds=15), rows[0].odds_group_id,
                    rows[0].market.outcome_key, True,
                )
                candidate = store.next_fill_candidate(order)
                self.assertIsNotNone(candidate)
                filled = attempt_fill(
                    order,
                    snapshot(datetime.fromisoformat(candidate["received_at"]), candidate["price"]),
                    observed_at=datetime.fromisoformat(candidate["transport_observed_at"]),
                )
                self.assertEqual((filled.status, filled.filled_at), ("filled", t3))


class OwnershipAndAtomicityTests(unittest.TestCase):
    def test_browser_match_is_insert_only(self) -> None:
        direct = {
            "id": "match-1", "tournament_name": "Direct Cup", "live_url": "https://video",
            "team": [{"pos": 1, "team_name": "One"}, {"pos": 2, "team_name": "Two"}],
        }
        browser = {
            "id": "match-1", "tournament_name": "Browser Cup",
            "team": [{"pos": 1, "team_name": "Wrong"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                store.upsert_raybet_match(direct, NOW)
                self.assertFalse(store.insert_browser_raybet_match(browser, NOW))
                row = store.connection.execute(
                    "SELECT tournament, team_one, live_url, raw_json FROM raybet_matches"
                ).fetchone()
                self.assertEqual(
                    (row["tournament"], row["team_one"], row["live_url"]),
                    ("Direct Cup", "One", "https://video"),
                )
                self.assertIn("Direct Cup", row["raw_json"])

    def test_direct_complete_response_rolls_back_on_market_failure(self) -> None:
        payload = {
            "result": {
                "id": "match-1",
                "tournament_name": "Atomic Cup",
                "team": [
                    {"team_id": 1, "pos": 1, "team_name": "One"},
                    {"team_id": 2, "pos": 2, "team_name": "Two"},
                ],
                "odds": [
                    {"id": "one", "team_id": 1, "match_stage": "r1",
                     "group_short_name": "Winner", "tag": "win", "odds": "2.0", "status": 5},
                    {"id": "two", "team_id": 2, "match_stage": "r1",
                     "group_short_name": "Winner", "tag": "win", "odds": "1.8", "status": 5},
                ],
            }
        }

        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                return payload

        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                original = store.insert_odds
                calls = 0

                def fail_second(row: OddsSnapshot) -> bool:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("injected market failure")
                    return original(row)

                with patch.object(store, "insert_odds", side_effect=fail_second):
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        collect_once(
                            store, Client(), Path(directory) / "raw",
                            list_rows=[{"id": "match-1"}], raw_fingerprints={},
                        )
                for table in (
                    "raybet_matches", "odds_snapshots", "odds_transport_observations"
                ):
                    self.assertEqual(
                        store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )


if __name__ == "__main__":
    unittest.main()
