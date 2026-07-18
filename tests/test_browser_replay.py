from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from live_betting.browser_contract import (
    BrowserEvent,
    EventType,
    Transport,
    canonical_json,
    payload_sha256,
)
from live_betting.browser_replay import replay_browser_events
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


def _event(event_id: str, captured_at: datetime, payload: dict) -> BrowserEvent:
    return BrowserEvent(
        schema_version=1,
        event_id=event_id,
        capture_session_id="a" * 32,
        captured_at_utc=captured_at,
        page_origin="https://www.ray086.com",
        page_path="/sports/esports",
        source_path="/v2/odds",
        transport=Transport.FETCH,
        event_type=EventType.ODDS,
        raybet_match_id="410001",
        game_id=151,
        payload=payload,
        payload_hash=payload_sha256(payload),
        payload_bytes=len(canonical_json(payload)),
        extension_version="0.1.0",
    )


def _payload(price: float) -> dict:
    return {
        "result": {
            "id": "410001",
            "game_id": 151,
            "team": [
                {"team_id": 1, "pos": 1, "team_name": "One"},
                {"team_id": 2, "pos": 2, "team_name": "Two"},
            ],
            "odds": [
                {"id": "one", "team_id": 1, "match_stage": "r1",
                 "group_short_name": "Winner", "tag": "win", "odds": price},
                {"id": "two", "team_id": 2, "match_stage": "r1",
                 "group_short_name": "Winner", "tag": "win", "odds": 1.8},
            ],
        }
    }


def _write_source(
    path: Path,
    *,
    reverse: bool = False,
    captured_at_by_index: dict[int, datetime] | None = None,
    received_at_by_index: dict[int, datetime] | None = None,
) -> None:
    with LiveBettingStore(path) as store:
        store.init_schema()
        values = ((1, 2.0), (2, 2.1))
        if reverse:
            values = tuple(reversed(values))
        for index, price in values:
            event = _event(
                f"{index:064x}",
                (captured_at_by_index or {}).get(index, NOW.replace(second=index)),
                _payload(price),
            )
            store.insert_browser_event(
                event,
                received_at=(received_at_by_index or {}).get(index, event.captured_at_utc),
                recognized=True,
            )
            store.update_browser_event_status(event.event_id, "audit_only", "fixture")


def test_restart_replay_matches_uninterrupted_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.db"
        uninterrupted = root / "uninterrupted.db"
        restarted = root / "restarted.db"
        _write_source(source)

        first = replay_browser_events(source, uninterrupted)
        second = replay_browser_events(source, restarted, restart_after=1)

        assert first == second
        assert first["events"] == 2
        assert first["outcomes"] == {"accepted": 2}
        assert first["tables"]["odds_transport_observations"]["rows"] == 2


def test_replay_reads_immutable_payload_and_orders_by_capture_time() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.db"
        target = root / "target.db"
        _write_source(source, reverse=True)
        with closing(sqlite3.connect(source)) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE browser_events SET payload_json='[]' WHERE event_id=?",
                    (f"{1:064x}",),
                )

        summary = replay_browser_events(source, target)
        assert summary["events"] == 2
        with closing(sqlite3.connect(target)) as connection:
            rows = connection.execute(
                "SELECT event_id, captured_at, received_at FROM browser_events ORDER BY captured_at"
            ).fetchall()
        assert [row[0] for row in rows] == [f"{1:064x}", f"{2:064x}"]
        assert rows[0][1] == rows[0][2]


def test_capture_reconstruction_is_explicit_and_arrival_preserves_future_gate() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.db"
        arrival_target = root / "arrival.db"
        capture_target = root / "capture.db"
        _write_source(
            source,
            captured_at_by_index={1: NOW.replace(second=30), 2: NOW.replace(second=31)},
            received_at_by_index={1: NOW, 2: NOW},
        )

        arrival = replay_browser_events(source, arrival_target, mode="arrival")
        capture = replay_browser_events(source, capture_target, mode="capture")

        assert arrival["mode"] == "arrival"
        assert capture["mode"] == "capture"
        assert arrival["downstream"] == "ingest_only"
        assert arrival["tables"]["odds_snapshots"]["rows"] == 0
        assert capture["tables"]["odds_snapshots"]["rows"] == 3
