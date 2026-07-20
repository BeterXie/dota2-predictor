from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_betting.engine import price_groups
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.monitor import (
    collect_completed_once,
    collect_once,
    completed_refresh_due,
    run,
)
from live_betting.raybet import RayBetClient
from live_betting.shadow_monitor import latest_market_state
from live_betting.storage import LiveBettingStore
from live_betting.strategy import attempt_fill
from tests.draft_authority_fixture import (
    make_test_vision_observation,
    seed_test_draft_authority,
)


NOW = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)


def snapshot(
    observed_at: datetime,
    price: float,
    *,
    odds_id: str = "winner-one",
    side: str = "team_one",
    match_id: str = "match-1",
) -> OddsSnapshot:
    return OddsSnapshot(
        match_id,
        odds_id,
        "winner-group",
        observed_at,
        price,
        1,
        Market("winner", "map_1", side, None, side, True),
        last_update=f"{price:.2f}",
    )


def raw_odds_payload(rows: list[OddsSnapshot]) -> dict[str, object]:
    match_ids = {row.raybet_match_id for row in rows}
    if len(match_ids) > 1:
        raise ValueError("raw fixture requires one RayBet match")
    match_id = next(iter(match_ids)) if match_ids else "match-1"
    return {
        "result": {
            "id": match_id,
            "game_id": 151,
            "team": [
                {"team_id": 1, "pos": 1, "team_name": "One"},
                {"team_id": 2, "pos": 2, "team_name": "Two"},
            ],
            "odds": [
                {
                    "id": row.odds_id,
                    "odds_group_id": row.odds_group_id,
                    "team_id": 1 if row.market.side == "team_one" else 2,
                    "match_stage": "r1",
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
    def test_alignment_and_decision_duplicate_keys_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                store.connection.execute(
                    "CREATE TABLE IF NOT EXISTS event_registry (event_id TEXT PRIMARY KEY)"
                )
                store.connection.execute(
                    "INSERT INTO event_registry (event_id) VALUES ('browser-test')"
                )
                identity_json = "{}"
                identity_hash = hashlib.sha256(
                    identity_json.encode("utf-8")
                ).hexdigest()
                mapping = store.connection.execute(
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
                       VALUES ('1001', 1, 'browser-test', 101, 202, 101,
                               'Alpha', 202, 'Beta', ?, ?, ?, ?, 'main_event',
                               ?, 3, ?, ?, ?, 'test', ?, ?, 'test-v1',
                               'manual_exact', NULL, 'test', ?, ?, ?)""",
                    (
                        identity_json,
                        identity_hash,
                        identity_json,
                        identity_hash,
                        (NOW - timedelta(days=1)).isoformat(),
                        identity_json,
                        identity_hash,
                        (NOW - timedelta(days=1)).isoformat(),
                        identity_json,
                        identity_hash,
                        (NOW - timedelta(days=1)).isoformat(),
                        (NOW - timedelta(days=1)).isoformat(),
                        (NOW - timedelta(days=1)).isoformat(),
                    ),
                )
                store.connection.commit()
                vision = make_test_vision_observation(
                    raybet_match_id="1001",
                    map_number=1,
                    captured_at=NOW,
                    label="browser-storage-decision-frame",
                )
                store.insert_vision_observation(vision)
                draft_authority = seed_test_draft_authority(
                    store.connection,
                    raybet_match_id="1001",
                    map_number=1,
                    strict_mapping_id=int(mapping.lastrowid),
                    observed_at=NOW,
                    label="browser-storage-decision",
                )
                winner_rows = [
                    snapshot(NOW, 2.5, match_id="1001"),
                    snapshot(
                        NOW,
                        1.5,
                        odds_id="winner-two",
                        side="team_two",
                        match_id="1001",
                    ),
                ]
                store.store_odds_observation(
                    source="direct",
                    observation_key="browser-storage-decision-transport",
                    source_event_id=None,
                    raybet_match_id="1001",
                    observed_at=NOW,
                    normalized_state_hash=normalized_state_hash(winner_rows),
                    snapshots=winner_rows,
                    raw_payload=raw_odds_payload(winner_rows),
                )
                alignment = SimpleNamespace(
                    odds_snapshot_id=1,
                    raybet_match_id="1001",
                    map_number=1,
                    game_clock_seconds=600,
                    observation_captured_at=NOW,
                    method="nearest_prior",
                    lag_seconds=1.0,
                    usable=True,
                    reason=None,
                )
                self.assertTrue(store.insert_alignment(alignment))
                self.assertFalse(store.insert_alignment(alignment))
                with self.assertRaisesRegex(ValueError, "alignment identity"):
                    store.insert_alignment(
                        SimpleNamespace(**{**vars(alignment), "usable": False})
                    )

                decision = SimpleNamespace(
                    decision_key="decision-1",
                    raybet_match_id="1001",
                    map_number=1,
                    decided_at=NOW,
                    underdog_side="team_one",
                    market_probability=price_groups(winner_rows)["winner-one"],
                    model_probability=0.5,
                    edge=0.1,
                    data_quality=0.8,
                    eligible=True,
                    reason="eligible",
                    contributions={
                        "draft": 0.1,
                        "__inputs__": {
                            "draft_authority": asdict(draft_authority),
                            "strict_live_eligibility": {
                                "mapping_refs": {
                                    "strict_mapping_id": int(mapping.lastrowid)
                                }
                            },
                        },
                    },
                    input_ref="input-1",
                    strategy_version="strategy-1",
                )
                self.assertTrue(
                    store.insert_decision(
                        decision,
                        draft_authority=draft_authority,
                        vision_observation=vision,
                        vision_transport_key=(
                            "browser-storage-decision-transport"
                        ),
                    )
                )
                self.assertFalse(
                    store.insert_decision(
                        decision,
                        draft_authority=draft_authority,
                        vision_observation=vision,
                        vision_transport_key=(
                            "browser-storage-decision-transport"
                        ),
                    )
                )
                with self.assertRaisesRegex(ValueError, "decision identity"):
                    store.insert_decision(
                        SimpleNamespace(**{**vars(decision), "edge": 0.2}),
                        draft_authority=draft_authority,
                        vision_observation=vision,
                        vision_transport_key=(
                            "browser-storage-decision-transport"
                        ),
                    )

    def test_completed_refresh_is_not_due_between_low_frequency_ticks(self) -> None:
        self.assertTrue(completed_refresh_due(None, 0.0, 300.0))
        self.assertFalse(completed_refresh_due([{"id": "match-1"}], 3.0, 300.0))
        self.assertTrue(completed_refresh_due([{"id": "match-1"}], 300.0, 300.0))

    def test_completed_match_list_uses_raybet_type_four(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}

        client = RayBetClient(client=Session())
        with patch.object(
            client, "matches", return_value=[{"id": "1001", "status": 3}],
        ) as matches:
            self.assertEqual(
                client.completed_matches(max_pages=4),
                [{"id": "1001", "status": 3}],
            )
            matches.assert_called_once_with(match_type=4, max_pages=4)

    def test_raybet_match_list_skips_malformed_rows(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}

        client = RayBetClient(client=Session())
        with patch.object(
            client,
            "match_page",
            return_value=[
                None,
                {"game_id": "not-a-number", "id": 1},
                {"game_id": 151, "id": "not-a-match-id"},
                {"game_id": 151, "id": 1001, "status": 2},
            ],
        ):
            self.assertEqual(
                client.matches(match_type=1, max_pages=1),
                [{"game_id": 151, "id": 1001, "status": 2}],
            )

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
                                {
                                    "id": "match-1",
                                    "tournament_name": "Test",
                                    "team": [
                                        {"pos": 1, "team_name": "One"},
                                        {"pos": 2, "team_name": "Two"},
                                    ],
                                },
                                NOW,
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
                "id": "1001",
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
                        raw_payload=raw_odds_payload(first),
                    ),
                    ("on_time", 1),
                )
                self.assertEqual(
                    store.store_odds_observation(
                        source="direct", observation_key="direct-t3", source_event_id=None,
                        raybet_match_id="match-1", observed_at=t3,
                        normalized_state_hash=normalized_state_hash(unchanged), snapshots=unchanged,
                        raw_payload=raw_odds_payload(unchanged),
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
                        raw_payload=raw_odds_payload(delayed),
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

    def test_transport_retry_rejects_new_membership_after_empty_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                empty_hash = normalized_state_hash([])
                store.store_odds_observation(
                    source="direct", observation_key="empty", source_event_id=None,
                    raybet_match_id="match-1", observed_at=NOW,
                    normalized_state_hash=empty_hash, snapshots=[],
                    raw_payload=raw_odds_payload([]),
                )
                changed = [snapshot(NOW, 2.0)]
                with self.assertRaisesRegex(ValueError, "already belongs"):
                    store.store_odds_observation(
                        source="direct", observation_key="empty", source_event_id=None,
                        raybet_match_id="match-1", observed_at=NOW,
                        normalized_state_hash=normalized_state_hash(changed),
                        snapshots=changed,
                        raw_payload=raw_odds_payload(changed),
                    )

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
                    raw_payload=raw_odds_payload(rows),
                )
                unchanged = [snapshot(t3, 2.0)]
                store.store_odds_observation(
                    source="direct", observation_key="direct-t3", source_event_id=None,
                    raybet_match_id="match-1", observed_at=t3,
                    normalized_state_hash=normalized_state_hash(unchanged), snapshots=unchanged,
                    raw_payload=raw_odds_payload(unchanged),
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
    def test_completed_collector_archives_type4_final_odds_and_status_three(self) -> None:
        payload = {
            "result": {
                "id": "1001",
                "game_id": 151,
                "status": 3,
                "tournament_name": "Completed Cup",
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
            def completed_matches(self) -> list[dict[str, object]]:
                return [{"id": "1001", "game_id": 151, "status": 3}]

            def match_odds(self, match_id: str) -> dict[str, object]:
                self.last_match_id = match_id
                return payload

        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
                store.init_schema()
                with patch(
                    "live_betting.monitor.utc_now",
                    side_effect=(NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)),
                ):
                    summary = collect_completed_once(store, Client(), raw_dir)
                self.assertEqual(summary["matches"], 1)
                row = store.connection.execute(
                    "SELECT status, raw_json FROM raybet_matches WHERE raybet_match_id='1001'"
                ).fetchone()
                self.assertEqual(row["status"], "3")
                self.assertIn("Completed Cup", row["raw_json"])
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM odds_transport_observations "
                        "WHERE raybet_match_id='1001' AND processing_status='processed'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM odds_response_outcomes_effective "
                        "WHERE raybet_match_id='1001'"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    [tuple(row) for row in store.connection.execute(
                        "SELECT response_kind, disposition FROM direct_response_audit "
                        "ORDER BY observed_at"
                    )],
                    [
                        ("completed_match_list", "audit_only"),
                        ("completed_odds", "accepted"),
                    ],
                )
            files = list(raw_dir.rglob("*.json.gz"))
            self.assertEqual(len(files), 2)
            self.assertTrue(all(len(path.name) == 72 for path in files))

    def test_completed_out_right_is_audited_skip_not_collection_error(self) -> None:
        payload = {
            "result": {
                "id": "1001",
                "game_id": 151,
                "status": 3,
                "match_short_name": "Outright",
                "team": [
                    {
                        "team_id": position,
                        "pos": position,
                        "team_name": f"Team {position}",
                    }
                    for position in range(1, 25)
                ],
                "odds": [],
            }
        }

        class Client:
            def match_odds(self, _match_id: str) -> dict[str, object]:
                return payload

        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
                store.init_schema()

                summary = collect_completed_once(
                    store,
                    Client(),
                    raw_dir,
                    completed_rows=[{"id": "1001", "status": 3}],
                )

                self.assertEqual(
                    (summary["matches"], summary["skipped"], summary["errors"]),
                    (0, 1, 0),
                )
                self.assertIsNone(
                    store.connection.execute(
                        "SELECT 1 FROM raybet_matches WHERE raybet_match_id='1001'"
                    ).fetchone()
                )
                audit = store.connection.execute(
                    """SELECT disposition, reason FROM direct_response_audit
                        WHERE response_kind='completed_odds'"""
                ).fetchone()
                self.assertEqual(
                    tuple(audit),
                    ("audit_only", "non_head_to_head_match"),
                )
                collector = store.connection.execute(
                    """SELECT last_success_at, last_error
                         FROM collector_runs WHERE collector='raybet_completed'"""
                ).fetchone()
                self.assertIsNotNone(collector["last_success_at"])
                self.assertIsNone(collector["last_error"])

    def test_worker_health_stays_healthy_for_completed_out_right_skip(self) -> None:
        payload = {
            "result": {
                "id": "1001",
                "game_id": 151,
                "status": 3,
                "match_short_name": "Outright",
                "team": [
                    {"pos": position, "team_name": f"Team {position}"}
                    for position in range(1, 25)
                ],
                "odds": [],
            }
        }

        class Client:
            def __enter__(self) -> "Client":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def live_matches(self) -> list[dict[str, object]]:
                return []

            def completed_matches(self) -> list[dict[str, object]]:
                return [{"id": "1001", "game_id": 151, "status": 3}]

            def match_odds(self, _match_id: str) -> dict[str, object]:
                return payload

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            args = SimpleNamespace(
                database=database,
                raw_dir=Path(directory) / "raw",
                interval=0.0,
                list_interval=15.0,
                completed_interval=300.0,
                max_backoff=300.0,
                once=True,
                schema_prepared=False,
            )
            with patch("live_betting.monitor.RayBetClient", return_value=Client()):
                self.assertEqual(run(args), 0)
            with LiveBettingStore(database) as store:
                health = store.connection.execute(
                    """SELECT status, last_error, details_json
                         FROM service_health WHERE component='raybet_worker'"""
                ).fetchone()
                self.assertEqual((health["status"], health["last_error"]), ("healthy", None))
                details = json.loads(health["details_json"])
                self.assertEqual(details["completed"]["skipped"], 1)
                self.assertEqual(details["completed"]["errors"], 0)
                self.assertIsNone(
                    store.connection.execute(
                        "SELECT 1 FROM raybet_matches WHERE raybet_match_id='1001'"
                    ).fetchone()
                )

    def test_completed_collector_isolates_one_match_failure(self) -> None:
        payload = {
            "result": {
                "id": "1002",
                "game_id": 151,
                "status": 3,
                "team": [
                    {"team_id": 1, "pos": 1, "team_name": "One"},
                    {"team_id": 2, "pos": 2, "team_name": "Two"},
                ],
                "odds": [],
            }
        }

        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                if match_id == "1001":
                    raise TimeoutError("fixture timeout")
                return payload

        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
                store.init_schema()
                with patch(
                    "live_betting.monitor.utc_now",
                    side_effect=(
                        NOW,
                        NOW + timedelta(seconds=1),
                        NOW + timedelta(seconds=2),
                        NOW + timedelta(seconds=3),
                    ),
                ):
                    summary = collect_completed_once(
                        store,
                        Client(),
                        raw_dir,
                        completed_rows=[
                            {"id": "1001", "status": 3},
                            {"id": "1002", "status": 3},
                        ],
                    )
                self.assertEqual(summary["matches"], 1)
                self.assertEqual(summary["errors"], 1)
                self.assertIsNotNone(
                    store.connection.execute(
                        "SELECT 1 FROM raybet_matches WHERE raybet_match_id='1002'"
                    ).fetchone()
                )
                audits = store.connection.execute(
                    """SELECT audit_key, response_kind, claimed_raybet_match_id,
                              disposition, reason
                         FROM direct_response_audit ORDER BY observed_at"""
                ).fetchall()
                self.assertEqual(
                    [tuple(row[1:]) for row in audits],
                    [
                        ("completed_match_list", None, "audit_only", "match_list_observed"),
                        ("completed_odds", "1001", "rejected", "request_failed:TimeoutError"),
                        ("completed_odds", "1002", "accepted", "normalized"),
                    ],
                )
                failure_payload = store.direct_response_payload(str(audits[1][0]))
                self.assertEqual(
                    failure_payload,
                    {
                        "artifact_version": "raybet-direct-request-failure-v1",
                        "claimed_raybet_match_id": "1001",
                        "failure": {"error_type": "TimeoutError"},
                        "response_kind": "completed_odds",
                    },
                )
            self.assertEqual(len(list(raw_dir.rglob("*.json.gz"))), 3)

    def test_direct_collector_content_addresses_repeated_raw_responses(self) -> None:
        payload = {
            "result": {
                "id": "1001",
                "game_id": 151,
                "team": [
                    {"team_id": 1, "pos": 1, "team_name": "One"},
                    {"team_id": 2, "pos": 2, "team_name": "Two"},
                ],
                "odds": [
                    {"id": "one", "team_id": 1, "match_stage": "r1",
                     "group_short_name": "Winner", "tag": "win", "odds": "2.0", "status": 1},
                    {"id": "two", "team_id": 2, "match_stage": "r1",
                     "group_short_name": "Winner", "tag": "win", "odds": "1.8", "status": 1},
                ],
            }
        }

        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                return payload

        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
                store.init_schema()
                with patch(
                    "live_betting.monitor.utc_now",
                    side_effect=tuple(
                        NOW + timedelta(seconds=offset) for offset in range(6)
                    ),
                ):
                    collect_once(store, Client(), raw_dir, list_rows=[{"id": "1001"}], raw_fingerprints={})
                    collect_once(
                        store,
                        Client(),
                        raw_dir,
                        list_rows=[{"id": "1001"}],
                        raw_fingerprints={},
                        audit_match_list=False,
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM odds_transport_observations"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM odds_raw_artifacts"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM direct_response_audit"
                    ).fetchone()[0],
                    3,
                )

            self.assertEqual(len(list(raw_dir.rglob("*.json.gz"))), 2)

    def test_direct_collector_rejects_mismatched_archive_configuration(self) -> None:
        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                raise AssertionError(f"unexpected fetch for {match_id}")

        with tempfile.TemporaryDirectory() as directory:
            owned_raw_dir = Path(directory) / "owned-raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=owned_raw_dir
            ) as store:
                store.init_schema()
                with self.assertRaisesRegex(ValueError, "does not match"):
                    collect_once(
                        store,
                        Client(),
                        Path(directory) / "other-raw",
                        list_rows=[],
                        raw_fingerprints={},
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM direct_response_audit"
                    ).fetchone()[0],
                    0,
                )
            self.assertEqual(list(owned_raw_dir.rglob("*.json.gz")), [])

    def test_match_list_request_failures_are_replayable(self) -> None:
        class Client:
            def live_matches(self) -> list[dict[str, object]]:
                raise TimeoutError("secret upstream detail")

            def completed_matches(self) -> list[dict[str, object]]:
                raise ConnectionError("private endpoint detail")

        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
                store.init_schema()
                with self.assertRaises(TimeoutError):
                    collect_once(store, Client(), raw_dir)
                with self.assertRaises(ConnectionError):
                    collect_completed_once(store, Client(), raw_dir)
                audits = store.connection.execute(
                    """SELECT audit_key, response_kind, disposition, reason
                         FROM direct_response_audit ORDER BY response_kind"""
                ).fetchall()
                self.assertEqual(
                    [tuple(row[1:]) for row in audits],
                    [
                        (
                            "completed_match_list",
                            "rejected",
                            "request_failed:ConnectionError",
                        ),
                        ("live_match_list", "rejected", "request_failed:TimeoutError"),
                    ],
                )
                for row in audits:
                    payload = store.direct_response_payload(str(row[0]))
                    self.assertNotIn("detail", json.dumps(payload))

    def test_direct_collector_rejects_mismatched_and_non_dota_responses(self) -> None:
        responses = {
            "1001": {"result": {"id": "1002", "game_id": 151, "odds": []}},
            "1002": {"result": {"id": "1002", "game_id": "151", "odds": []}},
        }

        class Client:
            def match_odds(self, match_id: str) -> dict[str, object]:
                return responses[match_id]

        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
                store.init_schema()
                summary = collect_once(
                    store,
                    Client(),
                    raw_dir,
                    list_rows=[{"id": "1001"}, {"id": "1002"}],
                    raw_fingerprints={},
                )
                self.assertEqual(summary["listed"], 2)
                self.assertEqual(summary["matches"], 0)
                self.assertEqual(summary["errors"], 2)
                self.assertEqual(
                    store.connection.execute("SELECT COUNT(*) FROM raybet_matches").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store.connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    [tuple(row) for row in store.connection.execute(
                        "SELECT response_kind, disposition, reason "
                        "FROM direct_response_audit ORDER BY observed_at"
                    )],
                    [
                        ("live_match_list", "audit_only", "match_list_observed"),
                        ("live_odds", "rejected", "identity_mismatch"),
                        ("live_odds", "rejected", "identity_mismatch"),
                    ],
                )

    def test_direct_collector_continues_after_one_match_failure(self) -> None:
        payload = {
            "result": {
                "id": "1002",
                "game_id": 151,
                "team": [
                    {"pos": 1, "team_name": "One"},
                    {"pos": 2, "team_name": "Two"},
                ],
                "odds": [],
            }
        }

        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def match_odds(self, match_id: str) -> dict[str, object]:
                self.calls.append(match_id)
                if match_id == "1001":
                    raise TimeoutError("fixture timeout")
                return payload

        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
                store.init_schema()
                summary = collect_once(
                    store,
                    client,
                    raw_dir,
                    list_rows=[{"id": "1001"}, {"id": "1002"}],
                    raw_fingerprints={},
                )
                self.assertEqual(client.calls, ["1001", "1002"])
                self.assertEqual(summary["matches"], 1)
                self.assertEqual(summary["errors"], 1)
                self.assertIsNotNone(
                    store.connection.execute(
                        "SELECT 1 FROM raybet_matches WHERE raybet_match_id='1002'"
                    ).fetchone()
                )
                collector = store.connection.execute(
                    "SELECT last_success_at, last_error_at, last_error "
                    "FROM collector_runs WHERE collector='raybet'"
                ).fetchone()
                self.assertIsNotNone(collector["last_success_at"])
                self.assertIsNotNone(collector["last_error_at"])
                self.assertIn("1 live match", collector["last_error"])
                failed = store.connection.execute(
                    """SELECT audit_key, disposition, reason
                         FROM direct_response_audit
                        WHERE response_kind='live_odds'
                          AND claimed_raybet_match_id='1001'"""
                ).fetchone()
                self.assertEqual(
                    tuple(failed[1:]),
                    ("rejected", "request_failed:TimeoutError"),
                )
                self.assertEqual(
                    store.direct_response_payload(str(failed[0]))["failure"],
                    {"error_type": "TimeoutError"},
                )

    def test_worker_marks_partial_collection_as_degraded(self) -> None:
        payload = {
            "result": {
                "id": "1002",
                "game_id": 151,
                "team": [
                    {"pos": 1, "team_name": "One"},
                    {"pos": 2, "team_name": "Two"},
                ],
                "odds": [],
            }
        }

        class Client:
            def __enter__(self) -> "Client":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def live_matches(self) -> list[dict[str, object]]:
                return [{"id": "1001"}, {"id": "1002"}]

            def completed_matches(self) -> list[dict[str, object]]:
                return []

            def match_odds(self, match_id: str) -> dict[str, object]:
                if match_id == "1001":
                    raise TimeoutError("fixture timeout")
                return payload

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            args = SimpleNamespace(
                database=database,
                raw_dir=Path(directory) / "raw",
                interval=0.0,
                list_interval=15.0,
                completed_interval=300.0,
                max_backoff=300.0,
                once=True,
                schema_prepared=False,
            )
            with patch("live_betting.monitor.RayBetClient", return_value=Client()):
                self.assertEqual(run(args), 0)
            with LiveBettingStore(database) as store:
                health = store.connection.execute(
                    "SELECT status, details_json FROM service_health "
                    "WHERE component='raybet_worker'"
                ).fetchone()
                self.assertEqual(health["status"], "degraded")
                self.assertEqual(json.loads(health["details_json"])["errors"], 1)

    def test_browser_match_is_insert_only(self) -> None:
        direct = {
            "id": "match-1", "tournament_name": "Direct Cup", "live_url": "https://video",
            "team": [{"pos": 1, "team_name": "One"}, {"pos": 2, "team_name": "Two"}],
        }
        browser = {
            "id": "match-1", "tournament_name": "Browser Cup",
            "team": [
                {"pos": 1, "team_name": "Wrong One"},
                {"pos": 2, "team_name": "Wrong Two"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
                store.init_schema()
                store.upsert_raybet_match(direct, NOW)
                self.assertFalse(store.insert_browser_raybet_match(browser, NOW))
                row = store.connection.execute(
                    "SELECT tournament, team_one, live_url, raw_json FROM raybet_matches"
                ).fetchone()
                self.assertEqual(
                    (row["tournament"], row["team_one"], row["live_url"]),
                    ("Direct Cup", "One", None),
                )
                self.assertIn("Direct Cup", row["raw_json"])

    def test_teamless_update_reuses_existing_head_to_head_identity(self) -> None:
        initial = {
            "id": "match-1",
            "game_id": 151,
            "tournament_name": "Direct Cup",
            "start_time": "2026-07-13 20:00:00",
            "round": "bo3",
            "team": [
                {"team_id": 11, "pos": 1, "team_name": "One"},
                {"team_id": 22, "pos": 2, "team_name": "Two"},
            ],
            "status": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                store.upsert_raybet_match(initial, NOW)
                store.upsert_raybet_match(
                    {"id": "match-1", "status": 3},
                    NOW + timedelta(seconds=1),
                )

                row = store.connection.execute(
                    """SELECT tournament, team_one, team_two, scheduled_at,
                              best_of, status, raw_json
                         FROM raybet_matches WHERE raybet_match_id='match-1'"""
                ).fetchone()

                self.assertEqual(
                    tuple(row[:6]),
                    (
                        "Direct Cup",
                        "One",
                        "Two",
                        "2026-07-13 20:00:00",
                        3,
                        "3",
                    ),
                )
                stored = json.loads(row["raw_json"])
                self.assertEqual(
                    [team["team_id"] for team in stored["team"]],
                    [11, 22],
                )
                self.assertEqual(stored["game_id"], 151)
                self.assertEqual(stored["tournament_name"], "Direct Cup")
                self.assertEqual(stored["start_time"], "2026-07-13 20:00:00")
                self.assertEqual(stored["round"], "bo3")

    def test_explicit_invalid_teams_never_reuse_existing_identity(self) -> None:
        initial = {
            "id": "match-1",
            "tournament_name": "Direct Cup",
            "team": [
                {"pos": 1, "team_name": "One"},
                {"pos": 2, "team_name": "Two"},
            ],
            "status": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                store.upsert_raybet_match(initial, NOW)

                with self.assertRaisesRegex(
                    ValueError, "raybet_exact_team_metadata_missing"
                ):
                    store.upsert_raybet_match(
                        {
                            "id": "match-1",
                            "team": [
                                {
                                    "pos": position,
                                    "team_name": f"Team {position}",
                                }
                                for position in range(1, 25)
                            ],
                            "status": 3,
                        },
                        NOW + timedelta(seconds=1),
                    )

                row = store.connection.execute(
                    """SELECT team_one, team_two, status
                         FROM raybet_matches WHERE raybet_match_id='match-1'"""
                ).fetchone()
                self.assertEqual(tuple(row), ("One", "Two", "2"))

    def test_explicit_outright_marker_rejects_even_with_two_teams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()

                with self.assertRaisesRegex(
                    ValueError, "raybet_non_head_to_head_match"
                ):
                    store.upsert_raybet_match(
                        {
                            "id": "outright-without-teams",
                            "match_short_name": "Outright",
                        },
                        NOW,
                    )

                with self.assertRaisesRegex(
                    ValueError, "raybet_non_head_to_head_match"
                ):
                    store.upsert_raybet_match(
                        {
                            "id": "outright-2",
                            "match_short_name": "Outright",
                            "team": [
                                {"pos": 1, "team_name": "One"},
                                {"pos": 2, "team_name": "Two"},
                            ],
                        },
                        NOW,
                    )

                self.assertIsNone(
                    store.connection.execute(
                        "SELECT 1 FROM raybet_matches WHERE raybet_match_id='outright-2'"
                    ).fetchone()
                )

    def test_teamless_update_rejects_legacy_outright_marker_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                store.connection.execute(
                    """INSERT INTO raybet_matches VALUES
                       ('legacy-outright', 'Cup', 'One', 'Two',
                        '2026-07-13 20:00:00', 3, '2', NULL, ?, ?)""",
                    (
                        json.dumps({"match_short_name": "Outright"}),
                        NOW,
                    ),
                )
                store.connection.commit()
                before = tuple(
                    store.connection.execute(
                        """SELECT tournament, team_one, team_two, status,
                                  raw_json, updated_at
                             FROM raybet_matches
                            WHERE raybet_match_id='legacy-outright'"""
                    ).fetchone()
                )

                with self.assertRaisesRegex(
                    ValueError, "raybet_non_head_to_head_match"
                ):
                    store.upsert_raybet_match(
                        {"id": "legacy-outright", "status": 3},
                        NOW + timedelta(seconds=1),
                    )

                after = tuple(
                    store.connection.execute(
                        """SELECT tournament, team_one, team_two, status,
                                  raw_json, updated_at
                             FROM raybet_matches
                            WHERE raybet_match_id='legacy-outright'"""
                    ).fetchone()
                )
                self.assertEqual(after, before)

    def test_direct_complete_response_rolls_back_on_market_failure(self) -> None:
        payload = {
            "result": {
                "id": "1001",
                "game_id": 151,
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
            raw_dir = Path(directory) / "raw"
            with LiveBettingStore(
                Path(directory) / "test.db", raw_archive_root=raw_dir
            ) as store:
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
                    summary = collect_once(
                        store, Client(), raw_dir,
                        list_rows=[{"id": "1001"}], raw_fingerprints={},
                    )
                self.assertEqual(summary["matches"], 0)
                self.assertEqual(summary["errors"], 1)
                for table in (
                    "raybet_matches", "odds_snapshots", "odds_transport_observations"
                ):
                    self.assertEqual(
                        store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )
                self.assertEqual(
                    [tuple(row) for row in store.connection.execute(
                        "SELECT response_kind, disposition, reason "
                        "FROM direct_response_audit ORDER BY observed_at"
                    )],
                    [
                        ("live_match_list", "audit_only", "match_list_observed"),
                        ("live_odds", "rejected", "processing_failed:RuntimeError"),
                    ],
                )

    def test_raybet_metadata_does_not_regress_on_late_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveBettingStore(Path(directory) / "test.db") as store:
                store.init_schema()
                store.upsert_raybet_match(
                    {
                        "id": "1001",
                        "game_id": 151,
                        "tournament_name": "New Cup",
                        "team": [
                            {"pos": 1, "team_name": "New One"},
                            {"pos": 2, "team_name": "New Two"},
                        ],
                        "status": 2,
                    },
                    NOW + timedelta(seconds=10),
                )
                store.upsert_raybet_match(
                    {
                        "id": "1001",
                        "game_id": 151,
                        "tournament_name": "Old Cup",
                        "team": [
                            {"pos": 1, "team_name": "Old One"},
                            {"pos": 2, "team_name": "Old Two"},
                        ],
                        "status": 1,
                    },
                    NOW,
                )
                row = store.connection.execute(
                    "SELECT tournament, team_one, status, updated_at "
                    "FROM raybet_matches WHERE raybet_match_id='1001'"
                ).fetchone()
                self.assertEqual(row["tournament"], "New Cup")
                self.assertEqual(row["team_one"], "New One")
                self.assertEqual(row["status"], "2")
                self.assertEqual(row["updated_at"], (NOW + timedelta(seconds=10)).isoformat())


if __name__ == "__main__":
    unittest.main()
