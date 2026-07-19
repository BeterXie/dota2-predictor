from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fetch import main as fetch_main
from live_betting.service_coordination import SingleInstanceLock


def test_fetch_uses_explicit_database_and_holds_standard_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    sqlite3.connect(database).close()
    observed: list[Path] = []

    async def run(
        _config: dict[str, object],
        _force: bool,
        _match_id: int | None,
        database_path: str | Path | None = None,
    ) -> None:
        assert database_path is not None
        selected = Path(database_path).resolve()
        observed.append(selected)
        with pytest.raises(RuntimeError, match="already held"):
            with SingleInstanceLock(selected.with_suffix(".service.lock")):
                pass

    monkeypatch.setattr(fetch_main, "load_config", lambda: {})
    monkeypatch.setattr(fetch_main, "run", run)

    fetch_main.main(["--database", str(database), "--match-id", "42"])

    assert observed == [database.resolve()]
    with SingleInstanceLock(database.with_suffix(".service.lock")):
        pass


def test_fetch_rejects_supervisor_lock_before_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "locked.db"
    sqlite3.connect(database).close()
    called = False

    async def run(*_: object, **__: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(fetch_main, "load_config", lambda: {})
    monkeypatch.setattr(fetch_main, "run", run)

    with SingleInstanceLock(database.with_suffix(".service.lock")):
        with pytest.raises(RuntimeError, match="already held"):
            fetch_main.main(["--database", str(database)])

    assert not called


def test_fetch_rejects_duplicate_database_before_loading_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load() -> dict[str, object]:
        raise AssertionError("config loaded")

    monkeypatch.setattr(fetch_main, "load_config", load)

    with pytest.raises(SystemExit):
        fetch_main.main(
            ["--database=first.db", "--database", "second.db"]
        )
