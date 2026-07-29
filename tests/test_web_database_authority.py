from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_betting.service_coordination import SingleInstanceLock
from web import app as web_app
from web import main as web_main
from web import queries
from web.schemas import PrematchRequest


def test_web_database_sources_have_one_explicit_precedence(tmp_path: Path) -> None:
    config_path = tmp_path / "web" / "config.yaml"
    config_path.parent.mkdir()
    config = {"database": "config.db"}
    environment = {"DATABASE_PATH": str(tmp_path / "environment.db")}

    selected, source = web_main.resolve_database_path(
        tmp_path / "cli.db",
        config,
        config_path,
        environment,
    )
    assert selected == (tmp_path / "cli.db").resolve()
    assert source == "cli"

    selected, source = web_main.resolve_database_path(
        None,
        config,
        config_path,
        environment,
    )
    assert selected == (tmp_path / "environment.db").resolve()
    assert source == "environment"

    selected, source = web_main.resolve_database_path(
        None,
        config,
        config_path,
        {},
    )
    assert selected == (config_path.parent / "config.db").resolve()
    assert source == "config"


def test_web_main_hands_the_resolved_path_to_queries_and_reload_children(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "candidate.db"
    previous = queries.DB_PATH
    invoked: dict[str, object] = {}
    verified: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        web_main,
        "verify_prepared_database",
        lambda path, *, odds_raw_root: verified.append((path, odds_raw_root)),
    )
    monkeypatch.setattr(
        web_main.uvicorn,
        "run",
        lambda application, **kwargs: invoked.update(
            {"application": application, **kwargs}
        ),
    )
    try:
        web_main.main(
            [
                "--database",
                str(database),
                "--config",
                str(tmp_path / "missing.yaml"),
            ]
        )
        assert queries.DB_PATH == str(database.resolve())
        assert web_main.os.environ["DATABASE_PATH"] == str(database.resolve())
        assert invoked["application"] == "web.app:app"
        assert verified == [
            (
                database.resolve(),
                database.resolve().parent / "live_betting" / "raw-v2",
            )
        ]
    finally:
        queries.init_db(previous)


def test_prediction_code_uses_the_queries_database_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "candidate.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE teams (team_id INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO teams VALUES (?)", [(10,), (20,)])
    connection.commit()
    connection.close()

    observed: list[str] = []

    score = SimpleNamespace(
        pure_lineup_score=0.0,
        player_adjusted_lineup_score=None,
        effective_lineup_score=0.0,
        scoring_mode="pure",
        player_coverage_count=0,
        stake_multiplier=0.5,
        formula_version="rosh-test",
        source_name="stratz",
        source_week=1_800_000_000,
        source_as_of=datetime(2026, 7, 28, tzinfo=timezone.utc),
        evidence_hash="a" * 64,
        evidence={"pure_minute_table": []},
    )
    client = SimpleNamespace(fetch_lineup_score=lambda *_a, **_k: score)

    def format_output(*args: object) -> dict[str, object]:
        observed.append(str(args[-1]))
        return {"status": "ok"}

    output = SimpleNamespace(
        format_output=format_output,
        save_prediction=lambda *_: "prediction.json",
        _sanitize=lambda value: value,
    )
    previous = queries.DB_PATH
    queries.init_db(str(database))
    monkeypatch.setattr(
        web_app,
        "_get_prematch_builder",
        lambda: (client, output),
    )
    monkeypatch.setattr(web_app, "_PREDICTIONS_DIR", str(tmp_path / "predictions"))
    request = PrematchRequest(
        radiant_id=10,
        dire_id=20,
        radiant_heroes=[1, 2, 3, 4, 5],
        dire_heroes=[6, 7, 8, 9, 10],
    )
    try:
        result = web_app.create_prematch_prediction(request)
    finally:
        queries.init_db(previous)

    assert result["status"] == "ok"
    assert observed == [str(database.resolve())]


def test_web_lifespan_holds_its_lock_and_reaps_completed_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    sqlite3.connect(database).close()
    previous = queries.DB_PATH

    class CompletedFetch:
        @staticmethod
        def poll() -> int:
            return 0

    monkeypatch.setattr(web_app, "_FETCH_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(web_app.control.control_service, "shutdown", lambda *_a, **_k: None)
    monkeypatch.setattr(web_app.control.control_service, "close", lambda: None)
    monkeypatch.setattr(web_app.control.control_sessions, "clear", lambda: None)
    web_app._fetch_process = CompletedFetch()
    web_app._fetch_process_identity = None

    async def exercise() -> None:
        async with web_app._lifespan(web_app.app):
            with pytest.raises(RuntimeError, match="already held"):
                with SingleInstanceLock(database.with_suffix(".web.lock")):
                    pass
            for _ in range(20):
                if web_app._fetch_process is None:
                    break
                await asyncio.sleep(0.002)
            assert web_app._fetch_process is None
            assert web_app._fetch_poll_task is not None
        assert web_app._fetch_poll_task is None

    queries.init_db(str(database))
    try:
        asyncio.run(exercise())
    finally:
        web_app._fetch_process = None
        web_app._fetch_process_identity = None
        queries.init_db(previous)


def test_fetch_poll_keeps_an_unverifiable_handle() -> None:
    class UnverifiableFetch:
        @staticmethod
        def poll() -> int:
            raise OSError("opaque")

    process = UnverifiableFetch()
    web_app._fetch_process = process
    web_app._fetch_process_identity = None
    try:
        web_app._poll_fetch_process_once()
        assert web_app._fetch_process is process
    finally:
        web_app._fetch_process = None
        web_app._fetch_process_identity = None
