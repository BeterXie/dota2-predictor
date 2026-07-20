from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live_betting.browser_contract import BrowserEvent, payload_sha256
from live_betting.browser_ingest import BrowserEventIngestor, MAX_FUTURE_CAPTURE_SKEW
from live_betting.markets import normalized_state_hash, snapshots_from_payload
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


def odds_payload(tournament: str = "Browser Cup", price: str = "2.10") -> dict:
    return {
        "result": {
            "id": "38407985",
            "game_id": 151,
            "tournament_name": tournament,
            "round": "bo3",
            "team": [
                {"team_id": 1, "pos": 1, "team_name": "One"},
                {"team_id": 2, "pos": 2, "team_name": "Two"},
            ],
            "odds": [{
                "id": "winner-one", "team_id": 1, "match_stage": "r1",
                "group_short_name": "Winner", "tag": "win", "odds": price,
                "status": 5,
            }],
        }
    }


def event(
    event_id: str, captured_at: datetime = NOW, *, event_type: str = "odds",
    payload: dict | None = None, capture_reason: str | None = None,
    transport: str = "fetch", source_path: str = "/v2/odds",
) -> BrowserEvent:
    payload = odds_payload() if payload is None else payload
    value = {
        "schema_version": 1,
        "event_id": event_id,
        "capture_session_id": "b" * 32,
        "captured_at_utc": captured_at.isoformat().replace("+00:00", "Z"),
        "page_origin": "https://www.ray086.com",
        "page_path": "/sports/esports",
        "source_path": source_path,
        "transport": transport,
        "event_type": event_type,
        "raybet_match_id": "38407985" if event_type != "match_list" else None,
        "game_id": 151,
        "payload": payload,
        "payload_hash": payload_sha256(payload),
        "payload_bytes": len(json.dumps(payload, separators=(",", ":")).encode()),
        "capture_reason": capture_reason,
        "extension_version": "0.1.0",
    }
    return BrowserEvent.model_validate_json(json.dumps(value))


class BrowserIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = LiveBettingStore(Path(self.directory.name) / "test.db")
        self.store.init_schema()
        self.ingestor = BrowserEventIngestor(clock=lambda: NOW + timedelta(seconds=1))

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def scalar(self, sql: str) -> object:
        return self.store.connection.execute(sql).fetchone()[0]

    def test_recognized_odds_writes_audit_transport_match_and_semantics(self) -> None:
        result = self.ingestor.ingest(self.store, event("a" * 64))
        self.assertEqual(
            (result.outcome, result.processing_status, result.timing_status,
             result.normalized_change_count),
            ("accepted", "processed", "on_time", 1),
        )
        self.assertEqual(self.scalar("SELECT processing_status FROM browser_events"), "processed")
        self.assertEqual(self.scalar("SELECT source FROM odds_transport_observations"), "browser")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_snapshots"), 1)
        self.assertEqual(self.scalar("SELECT tournament FROM raybet_matches"), "Browser Cup")

    def test_duplicate_does_not_normalize_twice(self) -> None:
        item = event("b" * 64)
        self.ingestor.ingest(self.store, item)
        duplicate = self.ingestor.ingest(self.store, item)
        self.assertEqual((duplicate.outcome, duplicate.processing_status), ("duplicate", "duplicate"))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM browser_events"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_transport_observations"), 1)

    def test_reused_event_id_with_different_immutable_content_is_rejected(self) -> None:
        event_id = "b" * 64
        self.ingestor.ingest(self.store, event(event_id))
        conflict = self.ingestor.ingest(
            self.store, event(event_id, payload=odds_payload(price="3.10"))
        )
        self.assertEqual(
            (conflict.outcome, conflict.processing_status, conflict.reason),
            ("rejected", "error", "event_id_conflict"),
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM browser_events"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_snapshots"), 1)

    def test_non_complete_event_types_are_audit_only(self) -> None:
        match_payload = {"matches": [{"id": "38407985", "game_id": 151}]}
        item = event("c" * 64, event_type="match_list", payload=match_payload)
        result = self.ingestor.ingest(self.store, item)
        self.assertEqual(
            (result.processing_status, result.reason),
            ("audit_only", "match_list_audit_only"),
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_transport_observations"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM raybet_matches"), 0)

    def test_late_odds_is_transport_and_audit_only(self) -> None:
        newer_at = NOW + timedelta(seconds=10)
        newer_payload = odds_payload("Direct Cup", "2.00")
        newer = snapshots_from_payload(newer_payload, newer_at)
        newer_artifact = self.store.archive_response_payload(
            newer_payload,
            observed_at=newer_at,
            match_id="38407985",
        )
        self.store.store_odds_observation(
            source="direct", observation_key="direct-newer", source_event_id=None,
            raybet_match_id="38407985", observed_at=newer_at,
            normalized_state_hash=normalized_state_hash(newer), snapshots=newer,
            raw_payload=newer_payload, raw_artifact=newer_artifact,
        )
        result = self.ingestor.ingest(self.store, event("d" * 64, NOW))
        self.assertEqual(
            (result.processing_status, result.reason, result.timing_status,
             result.normalized_change_count),
            ("audit_only", "late_observation", "late", 0),
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_snapshots"), 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT timing_status FROM odds_transport_observations WHERE source='browser'"
            ).fetchone()[0],
            "late",
        )

    def test_forged_future_odds_is_audit_only(self) -> None:
        received_at = NOW + timedelta(seconds=1)
        captured_at = received_at + MAX_FUTURE_CAPTURE_SKEW + timedelta(milliseconds=1)
        result = self.ingestor.ingest(self.store, event("9" * 64, captured_at))

        self.assertEqual(
            (result.outcome, result.processing_status, result.reason),
            ("accepted", "audit_only", "future_observation"),
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM browser_events"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_transport_observations"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_snapshots"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM raybet_matches"), 0)

    def test_parser_error_rolls_back_savepoint_but_keeps_error_audit(self) -> None:
        def fail(_payload: dict, _received_at: datetime | None):
            raise ValueError("injected parser failure")

        ingestor = BrowserEventIngestor(parser=fail, clock=lambda: NOW)
        result = ingestor.ingest(self.store, event("e" * 64))
        self.assertEqual(
            (result.outcome, result.processing_status, result.reason),
            ("accepted", "error", "normalization_failed"),
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM raybet_matches"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_transport_observations"), 0)
        row = self.store.connection.execute(
            "SELECT processing_status, processing_reason FROM browser_events"
        ).fetchone()
        self.assertEqual(tuple(row), ("error", "normalization_failed"))

    def test_out_right_rolls_back_authority_but_keeps_error_audit(self) -> None:
        payload = odds_payload()
        payload["result"]["team"] = [
            {"pos": position, "team_name": f"Team {position}"}
            for position in range(1, 25)
        ]

        result = self.ingestor.ingest(
            self.store,
            event("8" * 64, payload=payload),
        )

        self.assertEqual(
            (result.outcome, result.processing_status, result.reason),
            ("accepted", "error", "normalization_failed"),
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM browser_events"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM raybet_matches"), 0)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM odds_transport_observations"),
            0,
        )

    def test_page_state_odds_cannot_enter_normalization(self) -> None:
        item = event(
            "e" * 64,
            transport="page_state",
            source_path="/manualControlData",
        )
        result = self.ingestor.ingest(self.store, item)
        self.assertEqual(
            (result.outcome, result.processing_status, result.reason),
            ("accepted", "error", "normalization_failed"),
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM browser_events"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_snapshots"), 0)

    def test_odds_membership_error_rolls_back_savepoint_but_keeps_error_audit(self) -> None:
        payload = odds_payload()
        payload["result"]["odds"].append(dict(payload["result"]["odds"][0]))
        result = self.ingestor.ingest(self.store, event("e" * 64, payload=payload))
        self.assertEqual(
            (result.outcome, result.processing_status, result.reason),
            ("accepted", "error", "normalization_failed"),
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM raybet_matches"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM odds_transport_observations"), 0)
        row = self.store.connection.execute(
            "SELECT processing_status, processing_reason FROM browser_events"
        ).fetchone()
        self.assertEqual(tuple(row), ("error", "normalization_failed"))

    def test_browser_match_metadata_never_overwrites_direct_row(self) -> None:
        direct = odds_payload("Direct Cup")["result"] | {"live_url": "https://video.example/live"}
        self.store.upsert_raybet_match(direct, NOW - timedelta(seconds=1))
        self.ingestor.ingest(self.store, event("f" * 64, payload=odds_payload("Browser Cup")))
        row = self.store.connection.execute(
            "SELECT tournament, live_url, raw_json FROM raybet_matches"
        ).fetchone()
        self.assertEqual((row["tournament"], row["live_url"]),
                         ("Direct Cup", None))
        self.assertIn("Direct Cup", row["raw_json"])


if __name__ == "__main__":
    unittest.main()
