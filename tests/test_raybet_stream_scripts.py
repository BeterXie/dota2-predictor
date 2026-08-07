from __future__ import annotations

from pathlib import Path

import pytest

from scripts.supervise_raybet_streams import watcher_command
from scripts.watch_raybet_stream import (
    _sanitized_stream_location,
    _validate_stream_url,
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


class _LiveMatchConnection:
    def __init__(self) -> None:
        self.closed = False

    def execute(self, query: str, params: tuple[str]) -> object:
        assert "SELECT status FROM raybet_matches" in query
        assert params == ("42",)

        class _Result:
            @staticmethod
            def fetchone() -> dict[str, str]:
                return {"status": "2"}

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


def test_live_stream_route_falls_back_to_raybet_page(monkeypatch) -> None:
    connection = _LiveMatchConnection()
    monkeypatch.setattr(monitor_router.queries, "get_db", lambda: connection)

    def unavailable(_: str) -> str:
        raise TimeoutError("upstream unavailable")

    monkeypatch.setattr(monitor_router, "_fresh_live_stream_url", unavailable)

    response = monitor_router.live_stream("42")

    assert connection.closed is True
    assert response.status_code == 307
    assert response.headers["location"] == "https://www.ray086.com/"
