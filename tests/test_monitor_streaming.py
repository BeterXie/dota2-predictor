from __future__ import annotations

import json
from contextlib import contextmanager

from web import monitoring
from web.routers import monitor


class _FakeConnection:
    def __init__(self) -> None:
        self.events: list[str] = []

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def close(self) -> None:
        self.events.append("close")


def test_monitor_snapshot_uses_one_bounded_transaction(monkeypatch) -> None:
    connection = _FakeConnection()
    monkeypatch.setattr(monitor.queries, "get_db", lambda: connection)
    monkeypatch.setattr(
        monitor.monitoring,
        "build_monitor_snapshot",
        lambda session: {"cursor": "cursor-1", "session": session},
    )

    snapshot = monitor._build_snapshot()

    assert snapshot["cursor"] == "cursor-1"
    assert connection.events == ["begin", "commit", "close"]


def test_stream_resolver_is_only_exposed_for_live_dota_matches() -> None:
    row = {
        "raybet_match_id": "38422415",
        "status": "2",
        "raw_json": json.dumps({"game_id": 151}),
    }

    assert monitoring._stream_resolver_url(row) == (
        "/api/monitor/matches/38422415/live-stream"
    )
    assert monitoring._stream_resolver_url({**row, "status": "1"}) is None
    assert monitoring._stream_resolver_url({**row, "raw_json": '{"game_id": 152}'}) is None
    assert monitoring._stream_resolver_url({**row, "raybet_match_id": "not-numeric"}) is None


def test_series_game_details_exposes_explicit_non_hash_map_id(monkeypatch) -> None:
    monkeypatch.setattr(monitoring, "_locked_draft_map_numbers", lambda *_args: set())
    monkeypatch.setattr(monitoring, "latest_live_draft_mapping", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(monitoring, "latest_map_checkpoints", lambda *_args: [])

    games, market_evidence = monitoring._series_game_details(
        object(),
        summary={
            "raybet_match_id": "38422865",
            "lifecycle": "live",
            "current_map_number": None,
        },
        prematch_timeline=[],
        collection_timeline=[],
        vision=[{"map_number": 1, "observed_at": "2026-08-11T03:10:00+08:00"}],
        latest_capture=None,
        game_snapshots=[],
        latest_huds={},
        vision_runtime=None,
        markets=[],
        postmatch={
            "status": "waiting",
            "reason": "not_ingested",
            "games": [],
            "unresolved_maps": [],
        },
        raybet_final_map_numbers=set(),
        max_points=100,
    )

    assert market_evidence == []
    assert len(games) == 1
    assert games[0]["game_id"] == "38422865:map_1"
    assert games[0]["map_id"] == "38422865:map_1"


def test_monitor_sse_snapshot_cache_reuses_recent_build(monkeypatch) -> None:
    clock = iter((100.0, 100.0, 102.0, 105.0, 105.0))
    builds: list[int] = []
    monkeypatch.setattr(monitor.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(monitor, "_snapshot_cache", None)

    def build() -> dict[str, object]:
        builds.append(1)
        return {"cursor": f"cursor-{len(builds)}"}

    monkeypatch.setattr(monitor, "_build_snapshot", build)

    first = monitor._cached_snapshot()
    second = monitor._cached_snapshot()
    third = monitor._cached_snapshot()

    assert first is second
    assert third is not first
    assert builds == [1, 1]
