from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from live_betting.direct_response_audit import (
    DirectResponseDecision,
    audited_direct_request,
)
from live_betting.monitor import (
    DEFAULT_FULL_ODDS_INTERVAL_SECONDS,
    DEFAULT_PRIORITY_ODDS_INTERVAL_SECONDS,
    LIVE_LIST_CACHE_TTL_SECONDS,
    MAX_PRIORITY_ODDS_WORKERS,
    OddsChannelRuntime,
    PerRequestBackoff,
    _active_priority_match_ids,
    _claim_odds_request,
    _collect_priority_rows,
    _odds_channel_poll_interval,
    _odds_channel_rows,
    _partition_live_rows,
    _publish_live_rows,
    _refresh_live_list_cache,
    _validate_odds_intervals,
    collect_once,
)
from live_betting.raybet import BASE_URL, RayBetClient, RayBetHTTPResponse
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc)
ODDS_ENDPOINT = f"{BASE_URL}/odds"


class PriorityQueryResult:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


class PriorityConnection:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self.rows = rows
        self.statement = ""

    def execute(self, statement: str) -> PriorityQueryResult:
        self.statement = statement
        return PriorityQueryResult(self.rows)


def test_priority_odds_defaults_preserve_transport_freshness_budget() -> None:
    assert DEFAULT_PRIORITY_ODDS_INTERVAL_SECONDS == 8.0
    assert DEFAULT_FULL_ODDS_INTERVAL_SECONDS == 120.0
    assert MAX_PRIORITY_ODDS_WORKERS == 4


def test_daemon_requires_at_least_one_odds_channel() -> None:
    with pytest.raises(ValueError, match="at least one RayBet odds channel"):
        _validate_odds_intervals(0.0, 0.0, once=False)

    _validate_odds_intervals(0.0, 0.0, once=True)


def test_full_channel_polls_at_priority_cadence_without_changing_full_interval() -> None:
    assert _odds_channel_poll_interval(120.0, "full", 8.0) == 8.0
    assert _odds_channel_poll_interval(120.0, "full", 0.0) == 8.0
    assert _odds_channel_poll_interval(8.0, "priority", 8.0) == 8.0


def test_priority_selection_requires_active_non_invalidated_strict_mapping() -> None:
    connection = PriorityConnection([("1001",), ("1003",)])
    store = type("Store", (), {"connection": connection})()

    priority_ids = _active_priority_match_ids(store)
    priority, full = _partition_live_rows(
        [
            {"id": "1001", "status": 2},
            {"id": "1002", "status": 2},
            {"id": "1003", "status": 1},
        ],
        priority_ids,
    )

    assert priority_ids == {"1001", "1003"}
    assert [row["id"] for row in priority] == ["1001"]
    assert [row["id"] for row in full] == ["1002", "1003"]
    _publish_live_rows(
        [
            {"id": "1001", "status": 2},
            {"id": "1002", "status": 2},
            {"id": "1003", "status": 1},
        ]
    )
    try:
        priority_rows, source_count, excluded = _odds_channel_rows(
            store,
            "priority",
        )
        full_rows, full_source_count, full_excluded = _odds_channel_rows(
            store,
            "full",
        )
    finally:
        _publish_live_rows([])

    assert [row["id"] for row in priority_rows] == ["1001"]
    assert [row["id"] for row in full_rows] == ["1002", "1003"]
    assert (source_count, excluded) == (3, 0)
    assert (full_source_count, full_excluded) == (3, 1)
    assert "COALESCE(CAST(match.status AS TEXT), '')!='3'" in connection.statement
    assert "strict_live_map_mapping_invalidations" in connection.statement
    assert "strict_live_map_mapping_supersessions" in connection.statement


def test_full_channel_takes_over_when_priority_is_unavailable() -> None:
    connection = PriorityConnection([("1001",)])
    store = type("Store", (), {"connection": connection})()
    _publish_live_rows(
        [
            {"id": "1001", "status": 2},
            {"id": "1002", "status": 2},
        ]
    )
    try:
        rows, source_count, excluded = _odds_channel_rows(
            store,
            "full",
            priority_available=False,
        )
    finally:
        _publish_live_rows([])

    assert [row["id"] for row in rows] == ["1001", "1002"]
    assert (source_count, excluded) == (2, 0)


def test_priority_runtime_reports_dead_and_stale_workers() -> None:
    thread = type("ThreadState", (), {"is_alive": lambda self: True})()
    runtime = OddsChannelRuntime("priority", 8.0, True)
    runtime.attach_thread(thread)
    runtime.record_cycle(
        active_match_ids=["1001", "1002"],
        successful_match_ids=["1001"],
        cycle_duration_seconds=4.0,
        maximum_request_duration_seconds=3.0,
        healthy=True,
        completed_monotonic=100.0,
    )

    assert runtime.available(monotonic_now=115.0) is True
    assert runtime.available(monotonic_now=117.0) is False
    details = runtime.details(monotonic_now=117.0)
    assert details["state"] == "stale"
    assert details["thread_alive"] is True
    assert details["oldest_match_refresh_age_seconds"] == 17.0
    assert details["unrefreshed_match_count"] == 1

    dead = type("ThreadState", (), {"is_alive": lambda self: False})()
    runtime.attach_thread(dead)
    assert runtime.needs_restart() is True
    assert runtime.details(monotonic_now=117.0)["state"] == "dead"

    runtime.attach_thread(thread)
    runtime.record_cycle(
        active_match_ids=["1001"],
        successful_match_ids=[],
        cycle_duration_seconds=4.0,
        maximum_request_duration_seconds=3.0,
        healthy=False,
        completed_monotonic=120.0,
    )
    assert runtime.available(monotonic_now=121.0) is False
    assert runtime.details(monotonic_now=121.0)["state"] == "degraded"


def test_priority_collection_is_bounded_and_one_failure_does_not_cancel_others(
    tmp_path: Path,
) -> None:
    barrier = threading.Barrier(2, timeout=2.0)
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def collect_row(
        _database_url: str,
        _raw_dir: Path,
        row: dict[str, object],
        _backoff: PerRequestBackoff,
    ) -> dict[str, object]:
        nonlocal active, maximum_active
        match_id = str(row["id"])
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            barrier.wait()
            if match_id == "1002":
                raise RuntimeError("isolated request failure")
            return {
                "matches": 1,
                "listed": 1,
                "odds": 2,
                "changed": 1,
                "errors": 0,
                "backoff_skipped": 0,
                "in_flight_skipped": 0,
                "prematch_collected": 0,
                "prematch_skipped": 0,
                "last_successful_match_ids": [match_id],
                "maximum_request_duration_seconds": 0.5,
            }
        finally:
            with lock:
                active -= 1

    rows = [{"id": str(1000 + index), "status": 2} for index in range(1, 5)]
    with patch("live_betting.monitor._collect_priority_row", side_effect=collect_row):
        summary = _collect_priority_rows(
            "postgresql+psycopg://unused",
            tmp_path,
            rows,
            PerRequestBackoff(),
            max_workers=2,
        )

    assert maximum_active == 2
    assert summary["listed"] == 4
    assert summary["matches"] == 3
    assert summary["errors"] == 1
    assert sorted(summary["last_successful_match_ids"]) == ["1001", "1003", "1004"]


def test_priority_and_full_channels_cannot_claim_same_match() -> None:
    with _claim_odds_request("1001") as priority_claimed:
        with _claim_odds_request("1001") as full_claimed:
            assert priority_claimed is True
            assert full_claimed is False

    with _claim_odds_request("1001") as later_claimed:
        assert later_claimed is True


class Session:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.response = response
        self.error = error

    def get(self, *_args: object, **_kwargs: object) -> object:
        if self.error is not None:
            raise self.error
        return self.response


class Response:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {"code": 200, "result": []}

    def raise_for_status(self) -> None:
        return None


def metadata(store: LiveBettingStore) -> dict[str, object]:
    row = store.connection.execute(
        "SELECT request_metadata_json FROM direct_response_audit"
    ).fetchone()
    assert row is not None
    value = json.loads(str(row["request_metadata_json"]))
    value.pop("receipt_id")
    return value


def test_raybet_transport_timing_is_injected_and_audited_on_success(
    tmp_path: Path,
) -> None:
    wall_times = iter((NOW, NOW + timedelta(milliseconds=250)))
    monotonic_times = iter((10.0, 10.125))
    client = RayBetClient(
        client=Session(Response()),
        wall_clock=lambda: next(wall_times),
        monotonic_clock=lambda: next(monotonic_times),
    )
    endpoint = f"{BASE_URL}/match"
    identity = f"{endpoint}?match_type=1&page=1"

    with LiveBettingStore(tmp_path / "success.db") as store:
        store.init_schema()
        result = audited_direct_request(
            store,
            fetch=lambda: client.match_page_response(1, 1),
            process=lambda _context: DirectResponseDecision(
                "ok", disposition="audit_only", reason="matched"
            ),
            response_kind="live_match_list",
            claimed_raybet_match_id=None,
            endpoint=endpoint,
            request_identity=identity,
        )
        assert result == "ok"
        assert metadata(store) == {
            "request_started_at": NOW.isoformat(),
            "transport_duration_ms": 125.0,
        }


def test_raybet_transport_timing_is_injected_and_audited_on_network_error(
    tmp_path: Path,
) -> None:
    wall_times = iter((NOW, NOW + timedelta(milliseconds=400)))
    monotonic_times = iter((20.0, 20.4))
    error = TimeoutError("network timeout")
    client = RayBetClient(
        client=Session(error=error),
        wall_clock=lambda: next(wall_times),
        monotonic_clock=lambda: next(monotonic_times),
    )
    endpoint = f"{BASE_URL}/match"
    identity = f"{endpoint}?match_type=1&page=1"

    with LiveBettingStore(tmp_path / "failure.db") as store:
        store.init_schema()
        with pytest.raises(TimeoutError):
            audited_direct_request(
                store,
                fetch=lambda: client.match_page_response(1, 1),
                process=lambda _context: DirectResponseDecision(
                    None, disposition="audit_only", reason="unexpected"
                ),
                response_kind="live_match_list",
                claimed_raybet_match_id=None,
                endpoint=endpoint,
                request_identity=identity,
            )
        assert metadata(store) == {
            "request_started_at": NOW.isoformat(),
            "transport_duration_ms": pytest.approx(400.0),
        }
        assert error.raybet_received_at == NOW + timedelta(milliseconds=400)
        assert error.raybet_transport_error is True


def test_live_list_cache_uses_monotonic_ttl_and_fails_closed_after_expiry() -> None:
    rows = [{"id": "1001", "game_id": 151}]
    with patch(
        "live_betting.monitor._fetch_match_list",
        side_effect=(rows, TimeoutError("temporary"), TimeoutError("expired")),
    ):
        cache, degraded = _refresh_live_list_cache(
            object(), object(), None,
            monotonic_clock=lambda: 10.0,
            wall_clock=lambda: NOW,
        )
        assert degraded is False
        assert cache.fetched_at_utc == NOW
        assert cache.expires_at_monotonic == 10.0 + LIVE_LIST_CACHE_TTL_SECONDS

        cached, degraded = _refresh_live_list_cache(
            object(), object(), cache,
            monotonic_clock=lambda: 69.999,
            wall_clock=lambda: NOW - timedelta(days=1),
        )
        assert degraded is True
        assert cached.current_rows(69.999) == rows

        with pytest.raises(TimeoutError, match="expired"):
            _refresh_live_list_cache(
                object(), object(), cache,
                monotonic_clock=lambda: 70.0,
                wall_clock=lambda: NOW - timedelta(days=2),
            )


def test_live_list_refresh_without_process_cache_fails_closed() -> None:
    with patch(
        "live_betting.monitor._fetch_match_list",
        side_effect=TimeoutError("restart has no cache"),
    ):
        with pytest.raises(TimeoutError, match="no cache"):
            _refresh_live_list_cache(
                object(), object(), None,
                monotonic_clock=lambda: 1.0,
                wall_clock=lambda: NOW,
            )


def test_per_request_backoff_is_exponential_bounded_and_success_clears() -> None:
    clock = [0.0]
    backoff = PerRequestBackoff(clock=lambda: clock[0])
    identity = f"{ODDS_ENDPOINT}?match_id=1001"
    error = TimeoutError("network")

    assert backoff.record_failure(ODDS_ENDPOINT, identity, error)
    assert backoff.blocked(ODDS_ENDPOINT, identity)
    assert backoff.details()["entries"][0]["retry_in_seconds"] == 3.0

    for failure_number in range(2, 9):
        clock[0] = backoff.details()["entries"][0]["retry_in_seconds"] + clock[0]
        assert backoff.record_failure(ODDS_ENDPOINT, identity, error)
        expected = min(300.0, 3.0 * (2 ** (failure_number - 1)))
        assert backoff.details()["entries"][0]["retry_in_seconds"] == expected
    assert backoff.details()["entries"][0]["retry_in_seconds"] == 300.0

    backoff.record_success(ODDS_ENDPOINT, identity)
    assert backoff.details() == {"active": 0, "entries": []}


def test_collect_once_backoff_does_not_block_other_matches(tmp_path: Path) -> None:
    calls: list[str] = []

    def collect(*_args: object, match_id: str, **_kwargs: object) -> tuple[int, int, str, bool]:
        calls.append(match_id)
        if match_id == "1001":
            raise TimeoutError("network")
        return (0, 0, match_id, False)

    raw_dir = tmp_path / "raw"
    backoff = PerRequestBackoff(clock=lambda: 0.0)
    with LiveBettingStore(tmp_path / "live.db", raw_archive_root=raw_dir) as store:
        store.init_schema()
        with patch("live_betting.monitor._collect_odds_response", side_effect=collect):
            first = collect_once(
                store,
                object(),
                raw_dir,
                list_rows=[{"id": "1001"}, {"id": "1002"}],
                audit_match_list=False,
                backoff=backoff,
                monotonic_now=0.0,
            )
            second = collect_once(
                store,
                object(),
                raw_dir,
                list_rows=[{"id": "1001"}, {"id": "1002"}],
                audit_match_list=False,
                backoff=backoff,
                monotonic_now=1.0,
            )

    assert calls == ["1001", "1002", "1002"]
    assert first["matches"] == 1 and first["errors"] == 1
    assert second["matches"] == 1 and second["backoff_skipped"] == 1


def test_prematch_collection_runs_once_in_two_hour_window_then_live_starts(
    tmp_path: Path,
) -> None:
    received_at = (
        NOW,
        NOW + timedelta(hours=2, seconds=1),
    )
    calls: list[str] = []
    start_time = "2026-07-23 11:02:03"

    class Client:
        def match_odds_response(self, match_id: str) -> RayBetHTTPResponse:
            observed_at = received_at[len(calls)]
            calls.append(match_id)
            status = 2 if len(calls) == 2 else 1
            return RayBetHTTPResponse(
                {
                    "code": 200,
                    "result": {
                        "id": int(match_id),
                        "game_id": 151,
                        "status": status,
                        "round": "bo3",
                        "team": [
                            {"pos": 1, "team_id": 11, "team_name": "One"},
                            {"pos": 2, "team_id": 22, "team_name": "Two"},
                        ],
                        "odds": [],
                    },
                },
                ODDS_ENDPOINT,
                f"{ODDS_ENDPOINT}?match_id={match_id}",
                observed_at,
                200,
                200,
            )

    raw_dir = tmp_path / "raw"
    with LiveBettingStore(
        tmp_path / "live.db", raw_archive_root=raw_dir
    ) as store:
        store.init_schema()
        before_window = collect_once(
            store,
            Client(),
            raw_dir,
            list_rows=[
                {"id": "1001", "status": 1, "start_time": start_time}
            ],
            audit_match_list=False,
            wall_clock=lambda: NOW - timedelta(seconds=1),
        )
        on_window = collect_once(
            store,
            Client(),
            raw_dir,
            list_rows=[
                {"id": "1001", "status": 1, "start_time": start_time}
            ],
            audit_match_list=False,
            wall_clock=lambda: NOW,
        )
        repeated = collect_once(
            store,
            Client(),
            raw_dir,
            list_rows=[
                {"id": "1001", "status": 1, "start_time": start_time}
            ],
            audit_match_list=False,
            wall_clock=lambda: NOW + timedelta(hours=1),
        )
        stale_prematch = collect_once(
            store,
            Client(),
            raw_dir,
            list_rows=[
                {"id": "1001", "status": 1, "start_time": start_time}
            ],
            audit_match_list=False,
            wall_clock=lambda: NOW + timedelta(hours=2, seconds=1),
        )
        live = collect_once(
            store,
            Client(),
            raw_dir,
            list_rows=[
                {"id": "1001", "status": 2, "start_time": start_time}
            ],
            audit_match_list=False,
            wall_clock=lambda: NOW + timedelta(hours=2, seconds=1),
        )
        audit_reasons = [
            tuple(row)
            for row in store.connection.execute(
                """SELECT disposition, reason FROM direct_response_audit
                     WHERE claimed_raybet_match_id='1001'
                     ORDER BY observed_at"""
            )
        ]
        transport_statuses = [
            str(row[0])
            for row in store.connection.execute(
                """SELECT processing_status FROM odds_transport_observations
                     WHERE raybet_match_id='1001' ORDER BY observed_at"""
            )
        ]

    assert calls == ["1001", "1001"]
    assert before_window["prematch_skipped"] == 1
    assert on_window["prematch_collected"] == 1
    assert repeated["prematch_skipped"] == 1
    assert stale_prematch["prematch_skipped"] == 1
    assert live["prematch_collected"] == 0
    assert audit_reasons == [
        ("audit_only", "prematch_observed"),
        ("accepted", "normalized"),
    ]
    assert transport_statuses == ["audit_only", "processed"]


def test_prematch_failure_does_not_delay_first_live_request(tmp_path: Path) -> None:
    calls: list[str] = []

    def collect(
        *_args: object, match_id: str, **_kwargs: object
    ) -> tuple[int, int, str, bool]:
        calls.append(match_id)
        if len(calls) == 1:
            raise TimeoutError("prematch network failure")
        return (0, 0, match_id, False)

    raw_dir = tmp_path / "raw"
    backoff = PerRequestBackoff(clock=lambda: 0.0)
    with LiveBettingStore(
        tmp_path / "live.db", raw_archive_root=raw_dir
    ) as store:
        store.init_schema()
        with patch("live_betting.monitor._collect_odds_response", side_effect=collect):
            prematch = collect_once(
                store,
                object(),
                raw_dir,
                list_rows=[
                    {
                        "id": "1001",
                        "status": 1,
                        "start_time": "2026-07-23 11:02:03",
                    }
                ],
                audit_match_list=False,
                backoff=backoff,
                monotonic_now=0.0,
                wall_clock=lambda: NOW,
            )
            live = collect_once(
                store,
                object(),
                raw_dir,
                list_rows=[{"id": "1001", "status": 2}],
                audit_match_list=False,
                backoff=backoff,
                monotonic_now=1.0,
                wall_clock=lambda: NOW + timedelta(seconds=1),
            )

    assert calls == ["1001", "1001"]
    assert prematch["errors"] == 1
    assert live["matches"] == 1


def test_non_retryable_rejection_does_not_create_backoff() -> None:
    backoff = PerRequestBackoff(clock=lambda: 0.0)
    identity = f"{ODDS_ENDPOINT}?match_id=1001"
    assert not backoff.record_failure(
        ODDS_ENDPOINT, identity, ValueError("identity mismatch")
    )
    assert backoff.details() == {"active": 0, "entries": []}


def test_backoff_state_is_removed_when_match_leaves_live_list() -> None:
    backoff = PerRequestBackoff(clock=lambda: 0.0)
    identity = f"{ODDS_ENDPOINT}?match_id=1001"
    assert backoff.record_failure(
        ODDS_ENDPOINT, identity, TimeoutError("network")
    )

    backoff.retain(set())

    assert backoff.details() == {"active": 0, "entries": []}


def test_provider_5xx_backoff_keeps_status_in_health_details() -> None:
    backoff = PerRequestBackoff(clock=lambda: 5.0)
    identity = f"{ODDS_ENDPOINT}?match_id=1001"
    error = RuntimeError("provider unavailable")
    error.raybet_response = RayBetHTTPResponse(
        {"code": 503}, ODDS_ENDPOINT, identity, NOW, 200, 503
    )

    assert backoff.record_failure(ODDS_ENDPOINT, identity, error)
    entry = backoff.details()["entries"][0]
    assert entry["last_http_status"] == 200
    assert entry["last_provider_code"] == 503
    assert entry["last_failure_reason"] == "RuntimeError"
