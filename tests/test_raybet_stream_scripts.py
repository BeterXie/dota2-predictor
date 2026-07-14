from __future__ import annotations

import json
from io import StringIO
import sqlite3
from pathlib import Path

import pytest

from scripts.supervise_raybet_streams import (
    DEFAULT_OBSERVATION_DIR as SUPERVISOR_OBSERVATION_DIR,
    reap_children,
    watcher_command,
)
from scripts.watch_raybet_stream import (
    DEFAULT_OBSERVATION_DIR as WATCHER_OBSERVATION_DIR,
    ROOT,
    completion_check_due,
    match_source,
    resolve_source,
)


def _source_database(tmp_path: Path, raw: dict, rows: list[tuple]) -> Path:
    database = tmp_path / "live.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE raybet_matches (
                raybet_match_id TEXT PRIMARY KEY,
                live_url TEXT,
                raw_json TEXT,
                best_of INTEGER
            );
            CREATE TABLE odds_snapshots (
                id INTEGER PRIMARY KEY,
                odds_id TEXT,
                raybet_match_id TEXT,
                market_type TEXT,
                status TEXT,
                period TEXT,
                received_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO raybet_matches VALUES (?, ?, ?, ?)",
            ("42", "https://stream.test/live.m3u8", json.dumps(raw), 3),
        )
        connection.executemany(
            "INSERT INTO odds_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()
    return database


def test_match_source_prefers_manual_current_index(tmp_path: Path) -> None:
    raw = {
        "team": [
            {"score": {"manualControlData": {"currentIndex": 3}}},
            {"score": {"manualControlData": {"currentIndex": 3}}},
        ]
    }
    database = _source_database(
        tmp_path,
        raw,
        [
            (1, "series-a", "42", "winner", "1", "series", "2026-07-14T01:00:00+00:00"),
            (2, "map-1-a", "42", "winner", "1", "map_1", "2026-07-14T01:00:00+00:00"),
            (3, "map-2-a", "42", "winner", "1", "map_2", "2026-07-14T01:00:00+00:00"),
        ],
    )
    assert match_source(database, "42") == ("https://stream.test/live.m3u8", 3)


def test_match_source_uses_latest_state_per_outcome(tmp_path: Path) -> None:
    database = _source_database(
        tmp_path,
        {},
        [
            (1, "map-2-a", "42", "winner", "1", "map_2", "2026-07-14T01:00:00+00:00"),
            (2, "map-2-b", "42", "winner", "1", "map_2", "2026-07-14T01:00:00+00:00"),
            (3, "map-2-a", "42", "winner", "2", "map_2", "2026-07-14T02:00:00+00:00"),
            (4, "map-2-b", "42", "winner", "2", "map_2", "2026-07-14T02:00:00+00:00"),
            (5, "map-3-a", "42", "winner", "1", "map_3", "2026-07-14T03:00:00+00:00"),
            (6, "map-3-b", "42", "winner", "1", "map_3", "2026-07-14T03:00:00+00:00"),
        ],
    )

    assert match_source(database, "42") == ("https://stream.test/live.m3u8", 3)


def test_match_source_fails_closed_when_current_map_is_ambiguous(
    tmp_path: Path,
) -> None:
    database = _source_database(
        tmp_path,
        {},
        [
            (1, "map-1-a", "42", "winner", "1", "map_1", "2026-07-14T01:00:00+00:00"),
            (2, "map-2-a", "42", "winner", "1", "map_2", "2026-07-14T01:00:00+00:00"),
            (3, "series", "42", "winner", "1", "series", "2026-07-14T01:00:00+00:00"),
        ],
    )

    with pytest.raises(ValueError, match="cannot determine a unique current map"):
        match_source(database, "42")


def test_explicit_map_override_bypasses_ambiguous_market_inference(
    tmp_path: Path,
) -> None:
    database = _source_database(
        tmp_path,
        {},
        [
            (1, "map-1-a", "42", "winner", "1", "map_1", "2026-07-14T01:00:00+00:00"),
            (2, "map-2-a", "42", "winner", "1", "map_2", "2026-07-14T01:00:00+00:00"),
        ],
    )

    assert match_source(database, "42", map_override=2) == (
        "https://stream.test/live.m3u8",
        2,
    )


def test_direct_url_requires_explicit_map_number() -> None:
    with pytest.raises(ValueError, match="--map-number is required"):
        resolve_source(
            url="https://stream.test/live.m3u8",
            database=None,
            match_id="42",
            map_number=None,
        )
    assert resolve_source(
        url="https://stream.test/live.m3u8",
        database=None,
        match_id="42",
        map_number=2,
    ) == ("https://stream.test/live.m3u8", 2)


def test_supervisor_does_not_override_inferred_map_number(tmp_path: Path) -> None:
    command = watcher_command(
        tmp_path / "live.db",
        "42",
        tmp_path / "observations",
        tmp_path / "evidence",
    )
    assert "--map-number" not in command
    assert str(tmp_path / "observations" / "42.jsonl") in command


def test_completion_checks_use_sample_count_not_decoder_sequence() -> None:
    assert not completion_check_due(1)
    assert completion_check_due(15)
    assert completion_check_due(30)


class FakeProcess:
    def __init__(self) -> None:
        self.running = True
        self.terminated = False

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True
        self.running = False

    def wait(self, timeout: float) -> int:
        del timeout
        self.running = False
        return 0


def test_supervisor_terminates_watchers_that_are_no_longer_active() -> None:
    process = FakeProcess()
    stdout = StringIO()
    stderr = StringIO()
    children = {"42": (process, stdout, stderr)}

    reap_children(children, set())

    assert process.terminated
    assert stdout.closed and stderr.closed
    assert children == {}


def test_observation_defaults_are_anchored_in_predictor() -> None:
    expected = ROOT / "data" / "live_betting" / "live_observations"
    assert WATCHER_OBSERVATION_DIR == expected
    assert SUPERVISOR_OBSERVATION_DIR == expected
