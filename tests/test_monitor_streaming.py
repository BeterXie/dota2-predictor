from __future__ import annotations

from contextlib import contextmanager

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
