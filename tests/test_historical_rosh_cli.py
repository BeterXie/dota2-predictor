from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from event_intelligence.historical_rosh_backfill import (
    HistoricalRoshBackfillReport,
)
from scripts import backfill_historical_rosh as cli


def test_cli_requires_explicit_database() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args([])

    assert caught.value.code == 2


def test_cli_reports_resolved_explicit_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requested = tmp_path / "nested" / ".." / "active.db"
    resolved = requested.resolve()
    resolved.touch()
    observed: dict[str, Any] = {}
    lock_depth = 0

    class Storage:
        def __init__(self, database: Path) -> None:
            observed["storage_database"] = database
            self.connection = object()

        def __enter__(self) -> "Storage":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def init_schema(self) -> None:
            observed["schema_initialized"] = True

    @contextmanager
    def writer(database: Path) -> Iterator[None]:
        nonlocal lock_depth
        observed["writer_database"] = database
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    def backfill(storage: object, client: object, **kwargs: Any) -> object:
        assert lock_depth == 0
        observed["storage"] = storage
        observed["client"] = client
        assert kwargs["persist_score"](
            storage,
            1,
            {
                "radiant_hero_ids": (1, 2, 3, 4, 5),
                "dire_hero_ids": (6, 7, 8, 9, 10),
                "radiant_player_ids": (100, 101, 102, 103, 104),
                "dire_player_ids": (105, 106, 107, 108, 109),
            },
            SimpleNamespace(formula_version="formula-v1"),
            datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
        return HistoricalRoshBackfillReport(0, 0, 0, 0, ())

    client = object()
    monkeypatch.setattr(cli, "database_writer_authority", writer)
    monkeypatch.setattr(cli, "IntelligenceStorage", Storage)
    monkeypatch.setattr(cli, "StratzRoshClient", lambda: client)
    monkeypatch.setattr(cli, "backfill_historical_rosh_scores", backfill)
    monkeypatch.setattr(
        cli,
        "existing_historical_rosh_score_for_identity",
        lambda *_args, **_kwargs: None,
    )

    def persist(*_args: object, **_kwargs: object) -> bool:
        assert lock_depth == 1
        observed["persisted"] = True
        return True

    monkeypatch.setattr(cli, "persist_historical_rosh_score", persist)

    assert cli.main(["--database", str(requested)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["database"] == str(resolved)
    assert observed["writer_database"] == resolved
    assert observed["storage_database"] == resolved
    assert observed["schema_initialized"] is True
    assert observed["client"] is client
    assert observed["persisted"] is True
    assert lock_depth == 0
