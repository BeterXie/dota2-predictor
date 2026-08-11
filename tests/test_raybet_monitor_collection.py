from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_betting import monitor


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def execute(self, query: str) -> _Rows:
        assert "FROM raybet_matches" in query
        return _Rows(self._rows)


def test_live_list_refresh_persists_all_open_match_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2026, 8, 9, 11, 0, tzinfo=timezone.utc)
    head_to_head = {
        "id": "42",
        "status": 1,
        "team": [
            {"pos": 1, "team_id": 101, "team_name": "Alpha"},
            {"pos": 2, "team_id": 202, "team_name": "Beta"},
        ],
    }
    provider_rows = [
        head_to_head,
        {"id": "99", "status": 1, "match_short_name": "Outright"},
    ]
    calls: dict[str, object] = {}

    def fetch(
        store: object,
        client: object,
        *,
        response_kind: str,
        match_types: tuple[int, ...],
    ) -> list[dict[str, object]]:
        calls["response_kind"] = response_kind
        calls["match_types"] = match_types
        return provider_rows

    def sync(
        store: object,
        fetched_rows: list[dict[str, object]],
        *,
        observed_at: datetime,
    ) -> None:
        calls["rows"] = fetched_rows
        calls["observed_at"] = observed_at

    monkeypatch.setattr(monitor, "_fetch_match_list", fetch)
    monkeypatch.setattr(monitor, "_sync_open_match_rows", sync)

    cache, degraded = monitor._refresh_live_list_cache(
        object(),
        object(),
        None,
        monotonic_clock=lambda: 100.0,
        wall_clock=lambda: observed_at,
    )

    assert degraded is False
    assert list(cache.rows) == [head_to_head]
    assert calls == {
        "response_kind": "live_match_list",
        "match_types": monitor.OPEN_MATCH_TYPES,
        "rows": [head_to_head],
        "observed_at": observed_at,
    }


def test_open_match_sync_upserts_current_rows_and_unlists_missing() -> None:
    executed: list[tuple[str, tuple[str, ...]]] = []
    upserted: list[tuple[str, datetime]] = []
    observed_at = datetime(2026, 8, 9, 11, 0, tzinfo=timezone.utc)

    class Connection:
        def execute(
            self,
            query: str,
            params: tuple[str, ...] = (),
        ) -> None:
            executed.append((query, params))

    store = SimpleNamespace(
        connection=Connection(),
        transaction=nullcontext,
        upsert_raybet_match=lambda row, updated_at: upserted.append(
            (str(row["id"]), updated_at)
        ),
    )

    monitor._sync_open_match_rows(
        store,
        [{"id": "43", "status": 1}, {"id": "42", "status": 1}],
        observed_at=observed_at,
    )

    assert len(executed) == 1
    assert "SET status='unlisted'" in executed[0][0]
    assert executed[0][1] == ("42", "43")
    assert upserted == [("43", observed_at), ("42", observed_at)]


def test_due_scheduled_unlisted_match_is_retained_for_live_odds_polling() -> None:
    store = SimpleNamespace(
        connection=_Connection(
            [
                {
                    "raybet_match_id": "42",
                    "scheduled_at": "2026-08-08 17:00:00",
                    "status": "unlisted",
                },
                {
                    "raybet_match_id": "43",
                    "scheduled_at": "2026-08-08 18:00:00",
                    "status": "1",
                },
                {
                    "raybet_match_id": "44",
                    "scheduled_at": "2026-08-07 18:00:00",
                    "status": "1",
                },
            ]
        )
    )

    rows = monitor._scheduled_live_fallback_rows(
        store,
        now=datetime(2026, 8, 8, 9, 5, tzinfo=timezone.utc),
    )

    assert rows == [
        {
            "id": "42",
            "status": "unlisted",
            "start_time": "2026-08-08 17:00:00",
            "_force_live_poll": True,
        }
    ]


def test_future_prematch_collects_when_no_recent_audit_snapshot() -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    class Connection:
        def execute(self, query: str, params: tuple[str, ...]) -> SimpleNamespace:
            assert "FROM direct_response_audit" in query
            assert params == (
                "42",
                (now - timedelta(seconds=115)).isoformat(),
                now.isoformat(),
            )
            return SimpleNamespace(fetchone=lambda: None)

    store = SimpleNamespace(connection=Connection())
    assert monitor._prematch_collection_due(
        store,
        "42",
        {
            "id": "42",
            "status": "1",
            "start_time": "2026-08-09 23:00:00",
        },
        now=now,
    )


def test_future_prematch_waits_for_two_minute_refresh_interval() -> None:
    class Connection:
        def execute(self, query: str, params: tuple[str, ...]) -> SimpleNamespace:
            assert "FROM direct_response_audit" in query
            return SimpleNamespace(fetchone=lambda: {"recent": 1})

    store = SimpleNamespace(connection=Connection())

    assert not monitor._prematch_collection_due(
        store,
        "42",
        {
            "id": "42",
            "status": "1",
            "start_time": "2026-08-09 23:00:00",
        },
        now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
    )


def test_prematch_due_clock_advances_for_each_serial_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_checked_at = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    second_checked_at = first_checked_at + timedelta(seconds=9)
    checked_at: list[datetime] = []
    clock_values = iter((first_checked_at, second_checked_at))

    def collection_due(
        store: object,
        match_id: str,
        list_row: dict[str, object],
        *,
        now: datetime,
    ) -> bool:
        checked_at.append(now)
        return True

    monkeypatch.setattr(monitor, "_prematch_collection_due", collection_due)
    monkeypatch.setattr(
        monitor,
        "_collect_odds_response",
        lambda *args, **kwargs: (0, 2, "fingerprint", False),
    )
    store = SimpleNamespace(
        raw_archive_root=tmp_path.resolve(),
        record_collector=lambda *args, **kwargs: None,
    )
    rows = [
        {
            "id": match_id,
            "status": "1",
            "start_time": "2026-08-09 23:00:00",
        }
        for match_id in ("42", "43")
    ]

    result = monitor.collect_once(
        store,
        object(),
        tmp_path.resolve(),
        list_rows=rows,
        audit_match_list=False,
        wall_clock=lambda: next(clock_values),
    )

    assert result["prematch_collected"] == 2
    assert checked_at == [first_checked_at, second_checked_at]


def test_forced_prematch_refresh_does_not_defer_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_due_check(*args: object, **kwargs: object) -> bool:
        raise AssertionError("full channel must collect immediately")

    monkeypatch.setattr(
        monitor,
        "_prematch_collection_due",
        unexpected_due_check,
    )
    monkeypatch.setattr(
        monitor,
        "_collect_odds_response",
        lambda *args, **kwargs: (0, 2, "fingerprint", False),
    )
    store = SimpleNamespace(
        raw_archive_root=tmp_path.resolve(),
        record_collector=lambda *args, **kwargs: None,
    )

    result = monitor.collect_once(
        store,
        object(),
        tmp_path.resolve(),
        list_rows=[
            {
                "id": "42",
                "status": "1",
                "start_time": "2026-08-09 23:00:00",
            }
        ],
        audit_match_list=False,
        force_prematch_refresh=True,
    )

    assert result["matches"] == 1
    assert result["prematch_collected"] == 1
    assert result["prematch_skipped"] == 0


def test_scheduled_fallback_can_use_priority_polling() -> None:
    fallback = {
        "id": "42",
        "status": "1",
        "start_time": "2026-08-08 17:00:00",
        "_force_live_poll": True,
    }

    priority, full = monitor._partition_live_rows([fallback], {"42"})

    assert priority == [fallback]
    assert full == []


def test_provider_status_one_is_forced_live_after_scheduled_start() -> None:
    row = {
        "id": "42",
        "status": "1",
        "start_time": "2026-08-08 17:00:00",
    }

    assert monitor._scheduled_live_fallback_due(
        row,
        now=datetime(2026, 8, 8, 9, 5, tzinfo=timezone.utc),
    )


def test_forced_scheduled_poll_is_not_treated_as_prematch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def collect_response(
        store: object,
        client: object,
        *,
        match_id: str,
        response_kind: str,
        list_row: dict[str, object],
        audit_only: bool,
    ) -> tuple[int, int, str, bool]:
        calls.append(
            {
                "match_id": match_id,
                "response_kind": response_kind,
                "list_row": list_row,
                "audit_only": audit_only,
            }
        )
        return 1, 2, "fingerprint", False

    monkeypatch.setattr(monitor, "_collect_odds_response", collect_response)
    store = SimpleNamespace(
        raw_archive_root=tmp_path.resolve(),
        record_collector=lambda *args, **kwargs: None,
    )
    row = {
        "id": "42",
        "status": "1",
        "start_time": "2026-08-08 17:00:00",
        "_force_live_poll": True,
    }

    result = monitor.collect_once(
        store,
        object(),
        tmp_path.resolve(),
        list_rows=[row],
        audit_match_list=False,
        wall_clock=lambda: datetime(2026, 8, 8, 9, 5, tzinfo=timezone.utc),
    )

    assert result["matches"] == 1
    assert result["prematch_collected"] == 0
    assert calls == [
        {
            "match_id": "42",
            "response_kind": "live_odds",
            "list_row": row,
            "audit_only": False,
        }
    ]


def test_failed_odds_collection_reports_the_match_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_collection(*args: object, **kwargs: object) -> object:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(monitor, "_collect_odds_response", fail_collection)
    store = SimpleNamespace(
        raw_archive_root=tmp_path.resolve(),
        record_collector=lambda *args, **kwargs: None,
    )

    result = monitor.collect_once(
        store,
        object(),
        tmp_path.resolve(),
        list_rows=[{
            "id": "42",
            "status": "unlisted",
            "start_time": "2026-08-08 17:00:00",
            "_force_live_poll": True,
        }],
        audit_match_list=False,
    )

    assert result["errors"] == 1
    assert result["required_match_ids"] == ["42"]
    assert result["last_successful_match_ids"] == []
    assert result["failed_match_ids"] == ["42"]
