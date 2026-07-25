from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from live_betting.direct_response_audit import (
    DirectResponseDecision,
    audited_direct_request,
)
from live_betting.monitor import (
    LIVE_LIST_CACHE_TTL_SECONDS,
    PerRequestBackoff,
    _refresh_live_list_cache,
    collect_once,
)
from live_betting.raybet import BASE_URL, RayBetClient, RayBetHTTPResponse
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc)
ODDS_ENDPOINT = f"{BASE_URL}/odds"


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
