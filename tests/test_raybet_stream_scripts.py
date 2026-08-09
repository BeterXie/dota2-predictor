from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from scripts import supervise_raybet_streams, watch_raybet_stream
from scripts.supervise_raybet_streams import watcher_command
from scripts.watch_raybet_stream import (
    _sanitized_stream_location,
    _should_persist_frame,
    _validate_stream_url,
    match_is_complete,
)
from web.routers import monitor as monitor_router


def test_signed_hls_url_is_validated_without_leaking_query() -> None:
    url = "https://play.ehome.gg/live/match.m3u8?token=secret&expires=123"

    assert _validate_stream_url(url) == url
    assert _sanitized_stream_location(url) == "play.ehome.gg/live/match.m3u8"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/live.m3u8",
        "https://user:password@play.ehome.gg/live.m3u8",
        "https://example.com/live.m3u8",
        "https://play.ehome.gg/live.m3u8#fragment",
    ],
)
def test_stream_url_rejects_non_public_or_credentialed_locations(url: str) -> None:
    with pytest.raises(ValueError, match="invalid stream URL"):
        _validate_stream_url(url)


def test_supervisor_starts_the_retained_hls_watcher(tmp_path: Path) -> None:
    command = watcher_command(
        "postgresql+psycopg://user@localhost/dota2",
        "raybet-1",
        tmp_path / "output",
        tmp_path / "evidence",
    )

    assert command[1].endswith("watch_raybet_stream.py")
    assert command[command.index("--match-id") + 1] == "raybet-1"
    assert "--refresh-url" in command
    assert "--evidence-dir" in command


def test_prematch_exact_dota_payload_is_eligible_only_inside_watch_window() -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    payload = json.dumps({"id": "42", "game_id": 151, "status": "1"})

    assert supervise_raybet_streams._exact_dota_live_payload(payload, "42")
    assert supervise_raybet_streams._prematch_window_open(
        (now + timedelta(minutes=20)).isoformat(), now=now
    )
    assert not supervise_raybet_streams._prematch_window_open(
        (now + timedelta(minutes=31)).isoformat(), now=now
    )
    assert supervise_raybet_streams._prematch_window_open(
        "2026-08-09 20:20:00",
        now=now,
    )


def test_supervisor_starts_near_term_prematch_with_verified_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    url = "https://play.ehome.gg/live/42.m3u8"
    raw = json.dumps(
        {
            "id": "42",
            "game_id": 151,
            "status": "1",
            "_dota2_predictor_public_stream_v1": {
                "source": "direct_unsigned_v1",
                "url": url,
            },
        }
    )

    class Result:
        @staticmethod
        def fetchall() -> list[tuple[object, ...]]:
            return [
                (
                    "42",
                    "1",
                    now.isoformat(),
                    url,
                    raw,
                    (now + timedelta(minutes=20)).isoformat(),
                    None,
                )
            ]

    class Connection:
        @staticmethod
        def execute(_query: str, _params: tuple[object, ...]) -> Result:
            return Result()

    class Store:
        connection = Connection()

        def __enter__(self) -> "Store":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        supervise_raybet_streams,
        "LiveBettingStore",
        lambda _database_url: Store(),
    )

    assert supervise_raybet_streams.active_match_evidence(
        "postgresql://example", now=now
    ) == {"42": "prematch_verified_stream"}


@pytest.mark.parametrize(
    ("database_status", "scheduled_delta"),
    [
        ("1", timedelta(minutes=20)),
        ("unlisted", timedelta(minutes=-5)),
    ],
)
def test_supervisor_probes_ephemeral_stream_inside_prematch_window(
    monkeypatch: pytest.MonkeyPatch,
    database_status: str,
    scheduled_delta: timedelta,
) -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    raw = json.dumps({"id": "42", "game_id": 151, "status": "1"})

    class Result:
        @staticmethod
        def fetchall() -> list[tuple[object, ...]]:
            return [
                (
                    "42",
                    database_status,
                    now.isoformat(),
                    None,
                    raw,
                    (now + scheduled_delta).isoformat(),
                    None,
                )
            ]

    class Connection:
        @staticmethod
        def execute(_query: str, _params: tuple[object, ...]) -> Result:
            return Result()

    class Store:
        connection = Connection()

        def __enter__(self) -> "Store":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        supervise_raybet_streams,
        "LiveBettingStore",
        lambda _database_url: Store(),
    )

    assert supervise_raybet_streams.active_match_evidence(
        "postgresql://example", now=now
    ) == {"42": "prematch_ephemeral_stream_probe"}


def test_prematch_ephemeral_probe_does_not_exhaust_before_stream_appears() -> None:
    failed_at = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    first = supervise_raybet_streams.watcher_retry_after_failure(
        None,
        exit_code=2,
        produced_output=False,
        failed_at=failed_at,
        persistent_probe=True,
    )
    repeated = supervise_raybet_streams.watcher_retry_after_failure(
        first,
        exit_code=2,
        produced_output=False,
        failed_at=failed_at + timedelta(minutes=1),
        persistent_probe=True,
    )

    assert first.attempts == repeated.attempts == 1
    assert not first.exhausted and not repeated.exhausted
    assert repeated.retry_at == failed_at + timedelta(minutes=2)

    live_retry = supervise_raybet_streams.watcher_retry_after_failure(
        repeated,
        exit_code=2,
        produced_output=False,
        failed_at=failed_at + timedelta(minutes=2),
    )
    assert live_retry.attempts == 2
    assert not live_retry.exhausted


def test_confirmed_hud_does_not_bypass_lifecycle_evidence_scheduler() -> None:
    assert not _should_persist_frame(
        None,
        None,  # type: ignore[arg-type]
        captured_at=60.0,
        last_evidence_at=0.0,
        evidence_interval=30.0,
    )
    assert _should_persist_frame(
        None,
        None,  # type: ignore[arg-type]
        captured_at=60.0,
        last_evidence_at=0.0,
        evidence_interval=30.0,
        evidence_due=True,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [("1", False), ("2", False), ("3", True), ("finished", True)],
)
def test_watcher_closes_only_on_terminal_match_evidence(
    status: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        @staticmethod
        def fetchone() -> tuple[str, str, str, int]:
            return status, "2026-08-09T10:00:00+00:00", "{}", 3

    class Connection:
        @staticmethod
        def execute(_query: str, _params: tuple[str]) -> Result:
            return Result()

    class Store:
        connection = Connection()

        def __enter__(self) -> "Store":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        watch_raybet_stream,
        "LiveBettingStore",
        lambda _database_url: Store(),
    )

    assert match_is_complete("postgresql://example", "42") is expected


def test_observation_directory_is_shared_by_supervisor_and_direct_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation_root = tmp_path / "shared-observations"
    monkeypatch.setenv("VISION_OBSERVATION_DIR", str(observation_root))

    supervisor_args = supervise_raybet_streams.resolve_data_paths(
        argparse.Namespace(output_dir=None, evidence_dir=None, log_dir=None)
    )
    watcher_args = watch_raybet_stream.resolve_data_paths(
        argparse.Namespace(match_id="38417147", output=None, evidence_dir=None)
    )

    assert supervisor_args.output_dir == observation_root.resolve()
    assert watcher_args.output == observation_root.resolve() / "38417147.jsonl"


class _LiveMatchConnection:
    def __init__(self) -> None:
        self.closed = False

    def execute(self, query: str, params: tuple[str]) -> object:
        assert "FROM raybet_matches" in query
        assert params == ("42",)

        class _Result:
            @staticmethod
            def fetchone() -> dict[str, object]:
                return {
                    "raybet_match_id": "42",
                    "tournament": "Event",
                    "team_one": "Team A",
                    "team_two": "Team B",
                    "scheduled_at": "2026-08-09T12:00:00+00:00",
                    "best_of": 3,
                    "status": "2",
                    "live_url": None,
                    "raw_json": json.dumps(
                        {
                            "id": "42",
                            "game_id": 151,
                            "status": "2",
                            "team": [
                                {"pos": 1, "team_id": 1},
                                {"pos": 2, "team_id": 2},
                            ],
                        }
                    ),
                    "updated_at": "2026-08-09T12:00:00+00:00",
                }

        return _Result()

    def close(self) -> None:
        self.closed = True


def test_live_stream_route_redirects_to_fresh_hls(monkeypatch) -> None:
    connection = _LiveMatchConnection()
    stream_url = "https://play.ehome.gg/live/42.m3u8?expires=1&sig=test"
    monkeypatch.setattr(monitor_router.queries, "get_db", lambda: connection)
    monkeypatch.setattr(
        monitor_router,
        "_fresh_live_stream_url",
        lambda match_id: stream_url if match_id == "42" else "",
    )

    response = monitor_router.live_stream("42")

    assert connection.closed is True
    assert response.status_code == 307
    assert response.headers["location"] == stream_url
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_live_stream_route_reports_resolver_outage(monkeypatch) -> None:
    connection = _LiveMatchConnection()
    monkeypatch.setattr(monitor_router.queries, "get_db", lambda: connection)

    def unavailable(_: str) -> str:
        raise TimeoutError("upstream unavailable")

    monkeypatch.setattr(monitor_router, "_fresh_live_stream_url", unavailable)

    with pytest.raises(HTTPException) as captured:
        monitor_router.live_stream("42")

    assert connection.closed is True
    assert captured.value.status_code == 503
