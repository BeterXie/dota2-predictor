from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from event_intelligence.historical_rosh_backfill import (
    HistoricalRoshBackfillFailure,
    HistoricalRoshBackfillReport,
)
from scripts import run_historical_rosh_worker as worker


NOW = datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc)


class FakeStop:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float | None] = []
        self.on_wait: Any = None

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if self.on_wait is not None:
            self.on_wait(timeout)
        return self.stopped


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def complete() -> object:
    return SimpleNamespace(
        scoring_mode="current_player_adjusted",
        player_coverage_count=10,
        created_at=NOW,
    )


def partial(*, coverage: int, created_at: datetime) -> object:
    return SimpleNamespace(
        scoring_mode="pure",
        player_coverage_count=coverage,
        created_at=created_at,
    )


def report(
    *,
    inserted: int = 0,
    skipped: int = 0,
    failure: str | None = None,
) -> HistoricalRoshBackfillReport:
    failures = (
        ()
        if failure is None
        else (HistoricalRoshBackfillFailure(match_id=1, error=failure),)
    )
    return HistoricalRoshBackfillReport(
        selected=1,
        inserted=inserted,
        skipped=skipped,
        failed=len(failures),
        failures=failures,
    )


def test_parser_defaults_are_safe_for_supervisor_command(tmp_path: Path) -> None:
    args = worker.build_parser().parse_args(
        ["--database", str(tmp_path / "active.db"), "--schema-prepared"]
    )

    assert args.batch_size == 5
    assert args.idle_seconds == 60
    assert args.error_backoff_seconds == 300
    assert args.partial_retry_seconds == 604800
    assert args.schema_prepared is True


def test_fair_cursor_finishes_existing_maps_then_discovers_a_new_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match_ids = [1, 2]
    scores: dict[int, object] = {}
    calls: list[int] = []
    connections: list[FakeConnection] = []
    health: list[tuple[str, dict[str, Any]]] = []
    stop = FakeStop()

    def on_wait(timeout: float | None) -> None:
        if timeout == 60 and 3 not in match_ids:
            match_ids.append(3)

    stop.on_wait = on_wait
    monkeypatch.setattr(worker, "load_formal_match_ids", lambda _connection: match_ids)
    monkeypatch.setattr(
        worker,
        "load_existing_historical_rosh_score",
        lambda _connection, match_id: scores.get(match_id),
    )

    def backfill(_storage: object, _client: object, **kwargs: Any) -> object:
        match_id = kwargs["match_id"]
        calls.append(match_id)
        scores[match_id] = complete()
        if match_id == 3:
            stop.set()
        return report(inserted=1)

    def open_connection(_database: Path) -> FakeConnection:
        connection = FakeConnection()
        connections.append(connection)
        return connection

    result = worker.run_worker(
        tmp_path / "active.db",
        batch_size=2,
        throttle_seconds=0,
        stop_event=stop,
        clock=lambda: NOW,
        token_resolver=lambda: "token",
        client_factory=lambda _token: object(),  # type: ignore[arg-type]
        health_recorder=lambda status, _at, **kwargs: health.append(
            (status, kwargs)
        ),
        read_connection_factory=open_connection,  # type: ignore[arg-type]
        backfill=backfill,  # type: ignore[arg-type]
        emit=lambda _payload: None,
    )

    assert result == 0
    assert calls == [1, 2, 3]
    assert len(connections) == 3
    assert all(connection.closed for connection in connections)
    assert 60 in stop.waits
    assert health[0][0] == "starting"
    assert health[-1][0] == "stopped"


def test_recent_partial_uses_persisted_created_at_cooldown_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = FakeStop()
    stop.on_wait = lambda timeout: stop.set() if timeout == 60 else None
    existing = partial(coverage=7, created_at=NOW - timedelta(days=1))
    calls: list[int] = []
    monkeypatch.setattr(worker, "load_formal_match_ids", lambda _connection: (1,))
    monkeypatch.setattr(
        worker,
        "load_existing_historical_rosh_score",
        lambda _connection, _match_id: existing,
    )

    worker.run_worker(
        tmp_path / "active.db",
        stop_event=stop,
        clock=lambda: NOW,
        token_resolver=lambda: "token",
        client_factory=lambda _token: object(),  # type: ignore[arg-type]
        health_recorder=lambda *_args, **_kwargs: None,
        read_connection_factory=lambda _database: FakeConnection(),  # type: ignore[arg-type]
        backfill=lambda *_args, **_kwargs: calls.append(1),  # type: ignore[arg-type]
        emit=lambda _payload: None,
    )

    assert calls == []
    assert stop.waits == [60]


def test_missing_token_records_degraded_and_waits_without_opening_database(
    tmp_path: Path,
) -> None:
    stop = FakeStop()
    stop.on_wait = lambda _timeout: stop.set()
    health: list[tuple[str, dict[str, Any]]] = []
    opened: list[Path] = []

    worker.run_worker(
        tmp_path / "active.db",
        stop_event=stop,
        clock=lambda: NOW,
        token_resolver=lambda: None,
        client_factory=lambda _token: pytest.fail("client must not be created"),
        health_recorder=lambda status, _at, **kwargs: health.append(
            (status, kwargs)
        ),
        read_connection_factory=lambda database: opened.append(database),  # type: ignore[arg-type]
        emit=lambda _payload: None,
    )

    assert opened == []
    assert stop.waits == [60]
    assert [status for status, _details in health] == [
        "starting",
        "degraded",
        "stopped",
    ]
    assert health[1][1]["error"] == "configuration_missing"


def test_rate_limit_stops_batch_and_uses_global_error_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = FakeStop()
    stop.on_wait = lambda timeout: stop.set() if timeout == 300 else None
    calls: list[int] = []
    monkeypatch.setattr(worker, "load_formal_match_ids", lambda _connection: (1, 2))
    monkeypatch.setattr(
        worker,
        "load_existing_historical_rosh_score",
        lambda _connection, _match_id: None,
    )

    def backfill(_storage: object, _client: object, **kwargs: Any) -> object:
        calls.append(kwargs["match_id"])
        return report(failure="StratzRoshError: http_429")

    worker.run_worker(
        tmp_path / "active.db",
        batch_size=2,
        throttle_seconds=0,
        stop_event=stop,
        clock=lambda: NOW,
        token_resolver=lambda: "token",
        client_factory=lambda _token: object(),  # type: ignore[arg-type]
        health_recorder=lambda *_args, **_kwargs: None,
        read_connection_factory=lambda _database: FakeConnection(),  # type: ignore[arg-type]
        backfill=backfill,  # type: ignore[arg-type]
        emit=lambda _payload: None,
    )

    assert calls == [1]
    assert stop.waits == [300]


@pytest.mark.parametrize(
    "failure",
    [
        "StratzRoshError: http_auth_failure",
        "StratzRoshError: graphql_auth_failure",
    ],
)
def test_auth_failure_stops_batch_and_refreshes_token_after_global_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    stop = FakeStop()
    scores: dict[int, object] = {}
    tokens = iter(("expired-token", "fresh-token"))
    client_tokens: list[str] = []
    calls: list[tuple[int, str]] = []
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(worker, "load_formal_match_ids", lambda _connection: (1, 2))
    monkeypatch.setattr(
        worker,
        "load_existing_historical_rosh_score",
        lambda _connection, match_id: scores.get(match_id),
    )

    def resolve_token() -> str:
        token = next(tokens)
        events.append(("token", token))
        return token

    def client_factory(token: str) -> object:
        client_tokens.append(token)
        return token

    def backfill(_storage: object, client: str, **kwargs: Any) -> object:
        match_id = kwargs["match_id"]
        calls.append((match_id, client))
        events.append(("request", match_id))
        if client == "expired-token":
            return report(failure=failure)
        scores[match_id] = complete()
        stop.set()
        return report(inserted=1)

    def on_wait(timeout: float | None) -> None:
        events.append(("wait", timeout))

    stop.on_wait = on_wait
    worker.run_worker(
        tmp_path / "active.db",
        batch_size=2,
        throttle_seconds=0,
        stop_event=stop,
        clock=lambda: NOW,
        token_resolver=resolve_token,
        client_factory=client_factory,  # type: ignore[arg-type]
        health_recorder=lambda *_args, **_kwargs: None,
        read_connection_factory=lambda _database: FakeConnection(),  # type: ignore[arg-type]
        backfill=backfill,  # type: ignore[arg-type]
        emit=lambda _payload: None,
    )

    assert client_tokens == ["expired-token", "fresh-token"]
    assert calls == [(1, "expired-token"), (2, "fresh-token")]
    assert events.index(("wait", 300)) < events.index(("request", 2))


def test_stop_aware_retry_wait_raises_sanitized_cancellation() -> None:
    stop = FakeStop()
    stop.set()

    with pytest.raises(worker.StratzRoshError) as caught:
        worker._stop_aware_wait(stop, 2)

    assert stop.waits == [2]
    assert caught.value.category == "request_cancelled"


def test_ordinary_failure_advances_cursor_and_does_not_starve_later_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = FakeStop()
    calls: list[int] = []
    scores: dict[int, object] = {}
    monkeypatch.setattr(worker, "load_formal_match_ids", lambda _connection: (1, 2))
    monkeypatch.setattr(
        worker,
        "load_existing_historical_rosh_score",
        lambda _connection, match_id: scores.get(match_id),
    )

    def backfill(_storage: object, _client: object, **kwargs: Any) -> object:
        match_id = kwargs["match_id"]
        calls.append(match_id)
        if match_id == 1:
            return report(failure="HistoricalRoshIdentityError: mismatch")
        scores[match_id] = complete()
        stop.set()
        return report(inserted=1)

    worker.run_worker(
        tmp_path / "active.db",
        batch_size=2,
        throttle_seconds=0,
        stop_event=stop,
        clock=lambda: NOW,
        token_resolver=lambda: "token",
        client_factory=lambda _token: object(),  # type: ignore[arg-type]
        health_recorder=lambda *_args, **_kwargs: None,
        read_connection_factory=lambda _database: FakeConnection(),  # type: ignore[arg-type]
        backfill=backfill,  # type: ignore[arg-type]
        emit=lambda _payload: None,
    )

    assert calls == [1, 2]


def test_main_sigterm_handler_requests_boundary_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(
        worker.signal,
        "signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )

    def run_worker(_database: Path, **kwargs: Any) -> int:
        handlers[worker.signal.SIGTERM](worker.signal.SIGTERM, None)
        assert kwargs["stop_event"].is_set()
        return 0

    monkeypatch.setattr(worker, "run_worker", run_worker)

    assert worker.main(
        [
            "--database",
            str(tmp_path / "active.db"),
            "--schema-prepared",
        ]
    ) == 0
    assert worker.signal.SIGINT in handlers
    assert worker.signal.SIGTERM in handlers


def test_network_runs_without_writer_authority_and_persist_uses_short_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = FakeStop()
    lock_depth = 0
    inserted: list[dict[str, Any]] = []
    storage_connection = sqlite3.connect(":memory:")
    persisted = False

    @contextmanager
    def authority(_database: Path) -> Iterator[None]:
        nonlocal lock_depth
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    class Storage:
        def __init__(self, _database: Path) -> None:
            self.connection = storage_connection

        def __enter__(self) -> "Storage":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def insert_historical_rosh_lineup_score(self, **values: Any) -> object:
            assert lock_depth == 1
            inserted.append(values)
            return object()

    monkeypatch.setattr(worker, "database_writer_authority", authority)
    monkeypatch.setattr(worker, "IntelligenceStorage", Storage)
    monkeypatch.setattr(worker, "load_formal_match_ids", lambda _connection: (1,))

    def existing(_connection: object, _match_id: int) -> object | None:
        return complete() if persisted else None

    monkeypatch.setattr(worker, "load_existing_historical_rosh_score", existing)

    score = SimpleNamespace(
        formula_version="formula-v1",
        pure_lineup_score=1.0,
        current_player_adjusted_lineup_score=2.0,
        effective_lineup_score=2.0,
        scoring_mode="current_player_adjusted",
        player_coverage_count=10,
        source_week=1,
        source_as_of=NOW,
        player_stats_as_of=NOW,
        evidence={},
        evidence_hash="a" * 64,
        source_name="stratz",
    )
    identity = {
        "radiant_hero_ids": (1, 2, 3, 4, 5),
        "dire_hero_ids": (6, 7, 8, 9, 10),
        "radiant_player_ids": (100, 101, 102, 103, 104),
        "dire_player_ids": (105, 106, 107, 108, 109),
    }

    def backfill(_storage: object, _client: object, **kwargs: Any) -> object:
        nonlocal persisted
        assert lock_depth == 0
        assert kwargs["persist_score"](
            _storage,
            1,
            identity,
            score,
            NOW,
        )
        assert lock_depth == 0
        persisted = True
        stop.set()
        return report(inserted=1)

    try:
        worker.run_worker(
            tmp_path / "active.db",
            stop_event=stop,
            clock=lambda: NOW,
            token_resolver=lambda: "token",
            client_factory=lambda _token: object(),  # type: ignore[arg-type]
            health_recorder=lambda *_args, **_kwargs: None,
            read_connection_factory=lambda _database: FakeConnection(),  # type: ignore[arg-type]
            backfill=backfill,  # type: ignore[arg-type]
            emit=lambda _payload: None,
        )
    finally:
        storage_connection.close()

    assert lock_depth == 0
    assert len(inserted) == 1
