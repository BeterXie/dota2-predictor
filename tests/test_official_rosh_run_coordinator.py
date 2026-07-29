from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from live_betting.official_rosh_run import (
    OfficialRoshRunCoordinator,
    OfficialRoshRunKey,
)
from live_betting.rosh_evidence import official_rosh_draft_hash
from live_betting.rosh_parity import RoshAnalysisError
from live_betting.storage import LiveBettingStore
from prematch.stratz_official_profile import get_profile


RADIANT = (1, 2, 3, 4, 5)
DIRE = (6, 7, 8, 9, 10)
DATE_TIME = 1_785_283_200


def _key(
    radiant: tuple[int, ...] = RADIANT,
    dire: tuple[int, ...] = DIRE,
) -> OfficialRoshRunKey:
    profile = get_profile()
    return OfficialRoshRunKey(
        draft_hash=official_rosh_draft_hash(radiant, dire),
        rosh_profile_id=profile.rosh_profile_id,
        canonical_profile_hash=profile.canonical_profile_hash,
        date_time=DATE_TIME,
    )


def _result(key: OfficialRoshRunKey):
    return SimpleNamespace(
        run=SimpleNamespace(
            draft_hash=key.draft_hash,
            rosh_profile_id=key.rosh_profile_id,
            canonical_profile_hash=key.canonical_profile_hash,
            date_time=key.date_time,
            status="succeeded",
        )
    )


def _poll(coordinator: OfficialRoshRunCoordinator, key: OfficialRoshRunKey):
    return coordinator.poll_or_submit(
        key,
        radiant_hero_ids=RADIANT if key == _key() else (11, 12, 13, 14, 15),
        dire_hero_ids=DIRE if key == _key() else (16, 17, 18, 19, 20),
        request_started_at=datetime.fromtimestamp(DATE_TIME + 10, timezone.utc),
        profile=get_profile(),
    )


def test_first_submit_is_nonblocking_and_repeated_identity_is_singleflight() -> None:
    future: Future[object] = Future()
    executor = MagicMock()
    executor.submit.return_value = future
    coordinator = OfficialRoshRunCoordinator(
        database_path="unused.db",
        executor=executor,
        monotonic=lambda: 0.0,
    )
    key = _key()

    first = _poll(coordinator, key)
    repeated = _poll(coordinator, key)
    future.set_result(_result(key))
    completed = _poll(coordinator, key)
    after_completion = _poll(coordinator, key)

    assert first.status == "pending"
    assert repeated.status == "pending"
    assert completed.status == "succeeded"
    assert after_completion.status == "succeeded"
    assert executor.submit.call_count == 1


def test_retryable_failure_uses_bounded_backoff() -> None:
    first: Future[object] = Future()
    second: Future[object] = Future()
    executor = MagicMock()
    executor.submit.side_effect = (first, second)
    now = [0.0]
    coordinator = OfficialRoshRunCoordinator(
        database_path="unused.db",
        executor=executor,
        monotonic=lambda: now[0],
        max_attempts=2,
        backoff_base_seconds=5.0,
    )
    key = _key()

    assert _poll(coordinator, key).status == "pending"
    first.set_exception(RoshAnalysisError("upstream_unavailable"))
    failed = _poll(coordinator, key)
    assert failed.status == "failed"
    assert failed.attempts == 1
    now[0] = 4.9
    assert _poll(coordinator, key).status == "failed"
    assert executor.submit.call_count == 1
    now[0] = 5.0
    assert _poll(coordinator, key).status == "pending"
    second.set_exception(RoshAnalysisError("upstream_unavailable"))
    assert _poll(coordinator, key).status == "failed"
    now[0] = 100.0
    assert _poll(coordinator, key).status == "failed"
    assert executor.submit.call_count == 2


def test_changed_draft_never_receives_completed_old_result() -> None:
    old_future: Future[object] = Future()
    new_future: Future[object] = Future()
    executor = MagicMock()
    executor.submit.side_effect = (old_future, new_future)
    coordinator = OfficialRoshRunCoordinator(
        database_path="unused.db", executor=executor, monotonic=lambda: 0.0
    )
    old_key = _key()
    new_key = _key((11, 12, 13, 14, 15), (16, 17, 18, 19, 20))

    assert _poll(coordinator, old_key).status == "pending"
    assert _poll(coordinator, new_key).status == "unavailable"
    old_future.set_result(_result(old_key))
    assert _poll(coordinator, new_key).status == "pending"
    assert executor.submit.call_count == 2


def test_completed_result_after_timeout_is_discarded_and_retried() -> None:
    expired: Future[object] = Future()
    replacement: Future[object] = Future()
    executor = MagicMock()
    executor.submit.side_effect = (expired, replacement)
    now = [0.0]
    coordinator = OfficialRoshRunCoordinator(
        database_path="unused.db",
        executor=executor,
        monotonic=lambda: now[0],
        timeout_seconds=10.0,
        backoff_base_seconds=5.0,
    )
    key = _key()

    assert _poll(coordinator, key).status == "pending"
    expired.set_result(_result(key))
    now[0] = 11.0
    timed_out = _poll(coordinator, key)
    assert timed_out.status == "failed"
    assert timed_out.error_code == "background_timeout"
    now[0] = 15.9
    assert _poll(coordinator, key).status == "failed"
    now[0] = 16.0
    assert _poll(coordinator, key).status == "pending"
    assert executor.submit.call_count == 2


def test_background_runner_opens_an_independent_sqlite_connection(tmp_path) -> None:
    database = tmp_path / "live.db"
    store = LiveBettingStore(database)
    store.init_schema()
    seen_connections = []
    finished = threading.Event()
    key = _key()

    class Runner:
        def execute(self, *_args, **_kwargs):
            finished.set()
            return _result(key)

    def runner_factory(repository, _artifacts):
        seen_connections.append(repository.connection)
        return Runner()

    coordinator = OfficialRoshRunCoordinator(
        database_path=database,
        artifact_root=tmp_path / "artifacts",
        runner_factory=runner_factory,
    )
    try:
        assert _poll(coordinator, key).status == "pending"
        assert finished.wait(timeout=2)
        for _ in range(100):
            status = _poll(coordinator, key)
            if status.status == "succeeded":
                break
            time.sleep(0.01)
        assert status.status == "succeeded"
        assert seen_connections[0] is not store.connection
    finally:
        coordinator.close()
        store.close()
