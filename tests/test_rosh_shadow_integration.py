from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from live_betting.live_player_identity import LivePlayerIdentity
from live_betting.shadow_monitor import (
    RoshFetchCoordinator,
    RoshFetchKey,
    _rosh_score_for_trusted_draft,
    run_once,
)
from live_betting.vision import VisionObservation


def observation() -> VisionObservation:
    captured = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    return VisionObservation(
        raybet_match_id="1001",
        map_number=1,
        captured_at=captured,
        game_clock_seconds=600,
        is_paused=False,
        radiant_hero_ids=(1, 2, 3, 4, 5),
        dire_hero_ids=(6, 7, 8, 9, 10),
        clock_confidence=0.95,
        draft_confidence=0.95,
        source_frame_ref="vision-frame:test",
        screen_state="game",
        radiant_team_side="team_one",
    )


def test_fresh_fetch_waits_for_next_transport_and_same_transport_does_not_refetch() -> None:
    current_transport = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    first_run = current_transport + timedelta(seconds=10)
    completed = first_run + timedelta(seconds=1)
    fetched = SimpleNamespace(source_as_of=completed)
    already_fetched = SimpleNamespace(source_as_of=completed)
    cached = SimpleNamespace(source_as_of=completed)
    bound = object()
    store = MagicMock()
    store.rosh_draft_hash.return_value = "a" * 64
    store.find_rosh_lineup_score.side_effect = [None, None, None, None, cached]
    store.insert_rosh_lineup_score.side_effect = [bound]
    coordinator = MagicMock()
    coordinator.poll_or_submit.return_value = None

    first = _rosh_score_for_trusted_draft(
        store,
        observation(),
        strict_mapping_id=7,
        as_of=current_transport,
        fetch_started_at=first_run,
        fetch_coordinator=coordinator,
    )
    repeated = _rosh_score_for_trusted_draft(
        store,
        observation(),
        strict_mapping_id=7,
        as_of=current_transport,
        fetch_started_at=first_run + timedelta(seconds=2),
        fetch_coordinator=coordinator,
    )
    next_transport = _rosh_score_for_trusted_draft(
        store,
        observation(),
        strict_mapping_id=7,
        as_of=completed + timedelta(seconds=5),
        fetch_started_at=completed + timedelta(seconds=6),
        fetch_coordinator=coordinator,
    )

    assert first is None
    assert repeated is None
    assert next_transport is bound
    assert coordinator.poll_or_submit.call_count == 2


def test_exact_player_identity_reaches_player_adjusted_fetch_and_later_cache() -> None:
    current_transport = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    first_run = current_transport + timedelta(seconds=10)
    completed = first_run + timedelta(seconds=1)
    fetched = SimpleNamespace(source_as_of=completed, scoring_mode="player_adjusted")
    cached = SimpleNamespace(source_as_of=completed, scoring_mode="player_adjusted")
    bound = object()
    store = MagicMock()
    store.rosh_draft_hash.return_value = "a" * 64
    store.find_rosh_lineup_score.side_effect = [None, None, cached]
    store.insert_rosh_lineup_score.side_effect = [bound]
    coordinator = MagicMock()
    coordinator.poll_or_submit.return_value = None
    identity = LivePlayerIdentity(
        radiant_team_id=10,
        dire_team_id=20,
        radiant_hero_ids=(1, 2, 3, 4, 5),
        dire_hero_ids=(6, 7, 8, 9, 10),
        radiant_player_ids=(101, 102, 103, 104, 105),
        dire_player_ids=(106, 107, 108, 109, 110),
        source_match_id=7001,
        source_name="opendota_live",
        fetched_at=current_transport - timedelta(seconds=1),
        evidence_hash="b" * 64,
    )

    fresh = _rosh_score_for_trusted_draft(
        store,
        observation(),
        strict_mapping_id=7,
        as_of=current_transport,
        fetch_started_at=first_run,
        player_identity=identity,
        fetch_coordinator=coordinator,
    )
    later = _rosh_score_for_trusted_draft(
        store,
        observation(),
        strict_mapping_id=7,
        as_of=completed + timedelta(seconds=5),
        fetch_started_at=completed + timedelta(seconds=6),
        player_identity=identity,
        fetch_coordinator=coordinator,
    )

    assert fresh is None
    assert later is bound
    coordinator.poll_or_submit.assert_called_once_with(
        coordinator.poll_or_submit.call_args.args[0],
        radiant_heroes=(1, 2, 3, 4, 5),
        dire_heroes=(6, 7, 8, 9, 10),
        query_started_at=first_run,
        radiant_players=(101, 102, 103, 104, 105),
        dire_players=(106, 107, 108, 109, 110),
        player_identity_evidence={
            "radiant_team_id": 10,
            "dire_team_id": 20,
            "source_name": "opendota_live",
            "source_match_id": 7001,
            "fetched_at": current_transport - timedelta(seconds=1),
            "evidence_hash": "b" * 64,
        },
    )
    assert store.find_rosh_lineup_score.call_args_list[0].kwargs[
        "radiant_player_ids"
    ] == (101, 102, 103, 104, 105)


def _fetch_key() -> RoshFetchKey:
    return RoshFetchKey(
        radiant_heroes=(1, 2, 3, 4, 5),
        dire_heroes=(6, 7, 8, 9, 10),
        radiant_players=(None,) * 5,
        dire_players=(None,) * 5,
        player_identity_evidence_hash=None,
        cache_week_start=1_774_137_600,
    )


def _poll(coordinator: RoshFetchCoordinator, key: RoshFetchKey):
    return coordinator.poll_or_submit(
        key,
        radiant_heroes=key.radiant_heroes,
        dire_heroes=key.dire_heroes,
        query_started_at=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
        radiant_players=key.radiant_players,
        dire_players=key.dire_players,
        player_identity_evidence=None,
    )


def test_coordinator_cache_miss_is_nonblocking_and_same_key_is_deduplicated() -> None:
    release = threading.Event()
    started = threading.Event()
    fetched = SimpleNamespace(source_as_of=datetime.now(timezone.utc))
    client = MagicMock()

    def blocking_fetch(*_args, **_kwargs):
        started.set()
        release.wait(timeout=2)
        return fetched

    client.fetch_lineup_score.side_effect = blocking_fetch
    coordinator = RoshFetchCoordinator(client_factory=lambda: client)
    key = _fetch_key()
    before = time.monotonic()
    try:
        assert _poll(coordinator, key) is None
        assert time.monotonic() - before < 0.2
        assert started.wait(timeout=1)
        assert _poll(coordinator, key) is None
        assert client.fetch_lineup_score.call_count == 1
        release.set()
        result = None
        for _ in range(100):
            result = _poll(coordinator, key)
            if result is not None:
                break
            time.sleep(0.01)
        assert result is fetched
        assert client.fetch_lineup_score.call_count == 1
    finally:
        release.set()
        coordinator.close()


def test_uncancellable_timeout_does_not_enqueue_duplicate_work() -> None:
    future = MagicMock()
    future.done.return_value = False
    future.cancel.return_value = False
    executor = MagicMock()
    executor.submit.return_value = future
    ticks = iter((0.0, 100.0, 200.0))
    coordinator = RoshFetchCoordinator(
        executor=executor,
        monotonic=lambda: next(ticks),
        timeout_seconds=10.0,
    )
    key = _fetch_key()

    assert _poll(coordinator, key) is None
    assert _poll(coordinator, key) is None
    assert _poll(coordinator, key) is None
    assert executor.submit.call_count == 1
    assert future.cancel.call_count == 0


def test_different_keys_cannot_create_executor_queue_while_active() -> None:
    future = MagicMock()
    future.done.return_value = False
    executor = MagicMock()
    executor.submit.return_value = future
    coordinator = RoshFetchCoordinator(executor=executor, monotonic=lambda: 0.0)
    first = _fetch_key()
    second = RoshFetchKey(
        **{**first.__dict__, "radiant_heroes": (11, 12, 13, 14, 15)}
    )

    assert _poll(coordinator, first) is None
    for _ in range(100):
        assert _poll(coordinator, second) is None

    assert executor.submit.call_count == 1


def test_completed_old_key_is_swept_before_current_key_is_submitted() -> None:
    old_future = MagicMock()
    old_future.done.return_value = False
    old_future.result.return_value = object()
    current_future = MagicMock()
    current_future.done.return_value = False
    executor = MagicMock()
    executor.submit.side_effect = (old_future, current_future)
    coordinator = RoshFetchCoordinator(executor=executor, monotonic=lambda: 0.0)
    first = _fetch_key()
    second = RoshFetchKey(
        **{**first.__dict__, "dire_heroes": (16, 17, 18, 19, 20)}
    )

    assert _poll(coordinator, first) is None
    old_future.done.return_value = True
    assert _poll(coordinator, second) is None

    assert old_future.result.call_count == 1
    assert executor.submit.call_count == 2
    assert coordinator._active is not None
    assert coordinator._active[0] == second


def test_expired_completed_result_is_discarded_and_refetched() -> None:
    expired_result = object()
    expired_future = MagicMock()
    expired_future.done.return_value = True
    expired_future.result.return_value = expired_result
    replacement_future = MagicMock()
    replacement_future.done.return_value = False
    executor = MagicMock()
    executor.submit.side_effect = (expired_future, replacement_future)
    ticks = iter((0.0, 100.0))
    coordinator = RoshFetchCoordinator(
        executor=executor,
        monotonic=lambda: next(ticks),
        timeout_seconds=10.0,
    )
    key = _fetch_key()

    assert _poll(coordinator, key) is None
    assert _poll(coordinator, key) is None
    assert expired_future.result.call_count == 1
    assert executor.submit.call_count == 2


def test_failed_background_fetch_is_cleared_and_can_retry() -> None:
    fetched = SimpleNamespace(source_as_of=datetime.now(timezone.utc))
    calls = 0
    client = MagicMock()

    def fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret must not escape")
        return fetched

    client.fetch_lineup_score.side_effect = fetch
    coordinator = RoshFetchCoordinator(client_factory=lambda: client)
    key = _fetch_key()
    try:
        assert _poll(coordinator, key) is None
        result = None
        for _ in range(100):
            result = _poll(coordinator, key)
            if result is not None and calls >= 2:
                break
            time.sleep(0.01)
        assert result is fetched
        assert calls == 2
    finally:
        coordinator.close()


def test_completed_background_result_is_inserted_on_main_thread_and_not_used() -> None:
    current = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    fetched = SimpleNamespace(source_as_of=current + timedelta(seconds=2))
    coordinator = MagicMock()
    coordinator.poll_or_submit.return_value = fetched
    store = MagicMock()
    store.rosh_draft_hash.return_value = "a" * 64
    store.find_rosh_lineup_score.side_effect = [None, None]
    insert_threads: list[int] = []
    store.insert_rosh_lineup_score.side_effect = (
        lambda *_args, **_kwargs: insert_threads.append(threading.get_ident())
    )
    main_thread = threading.get_ident()

    result = _rosh_score_for_trusted_draft(
        store,
        observation(),
        strict_mapping_id=7,
        as_of=current,
        fetch_started_at=current + timedelta(seconds=3),
        fetch_coordinator=coordinator,
    )

    assert result is None
    assert insert_threads == [main_thread]


def test_pending_fill_path_never_polls_rosh_network() -> None:
    coordinator = MagicMock()
    pending = SimpleNamespace(status="filled", order_key="order-1")
    with (
        patch("live_betting.shadow_monitor.ingest_vision", return_value=0),
        patch("live_betting.shadow_monitor._process_pending_order", return_value=pending),
    ):
        result = run_once(
            MagicMock(),
            MagicMock(),
            Path("unused.jsonl"),
            now=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
            rosh_fetch_coordinator=coordinator,
        )

    assert result["status"] == "shadow_filled"
    coordinator.poll_or_submit.assert_not_called()


def test_production_coordinator_uses_five_second_request_timeout() -> None:
    with patch("live_betting.shadow_monitor.StratzRoshClient") as client_type:
        coordinator = RoshFetchCoordinator()
        try:
            coordinator._client_factory()
        finally:
            coordinator.close()
    client_type.assert_called_once_with(timeout_seconds=5.0)
