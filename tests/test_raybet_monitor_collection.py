from __future__ import annotations

from datetime import datetime, timezone
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


def test_due_scheduled_match_is_retained_for_live_odds_polling() -> None:
    store = SimpleNamespace(
        connection=_Connection(
            [
                {
                    "raybet_match_id": "42",
                    "scheduled_at": "2026-08-08 17:00:00",
                    "status": "1",
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
            "status": "1",
            "start_time": "2026-08-08 17:00:00",
            "_force_live_poll": True,
        }
    ]


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
