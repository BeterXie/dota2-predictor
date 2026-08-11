from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from web import main as web_main
from web import queries
from web.routers import control


class FakeStore:
    connection = object()

    def __enter__(self) -> FakeStore:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def init_schema(self) -> None:
        return None


def test_postmatch_worker_uses_existing_schema_and_batch_mode() -> None:
    captured: dict[str, object] = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=2468)

    process = web_main._start_postmatch_worker(popen)

    assert process.pid == 2468
    assert captured["command"][1:] == [
        "-u",
        "-m",
        "live_betting.postmatch_monitor",
        "--all",
        "--interval",
        "60",
        "--schema-prepared",
    ]
    assert "stdout" not in captured["kwargs"]
    assert "stderr" not in captured["kwargs"]


def test_strict_ingest_worker_uses_existing_schema_and_continuous_mode() -> None:
    captured: dict[str, object] = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=1357)

    process = web_main._start_strict_ingest_worker(popen)

    assert process.pid == 1357
    assert captured["command"][1:] == [
        "-u",
        str(web_main.ROOT / "scripts" / "run_strict_event_ingest.py"),
        "--interval",
        "30",
        "--schema-prepared",
    ]
    assert "stdout" not in captured["kwargs"]
    assert "stderr" not in captured["kwargs"]


def test_map_decision_worker_uses_existing_schema_and_one_second_polling() -> None:
    captured: dict[str, object] = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=9753)

    process = web_main._start_map_decision_worker(popen)

    assert process.pid == 9753
    assert captured["command"][1:] == [
        "-u",
        "-m",
        "live_betting.map_decision_checkpoints",
        "--interval",
        "1",
        "--schema-prepared",
    ]
    assert "stdout" not in captured["kwargs"]
    assert "stderr" not in captured["kwargs"]


def test_web_entrypoint_starts_and_owns_runtime_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://before:before@localhost/before")
    monkeypatch.setattr(web_main, "load_environment_file", lambda _path: None)
    monkeypatch.setattr(web_main, "LiveBettingStore", lambda _url: FakeStore())
    monkeypatch.setattr(web_main, "verify_runtime_schema", lambda _connection: None)
    monkeypatch.setattr(queries, "init_db", lambda url: calls.append(("database", url)))
    monkeypatch.setattr(
        control.control_service,
        "ensure_started",
        lambda connection, component, **options: (
            calls.append((component, connection))
            or calls.append((f"{component}_options", options))
            or {"status": "running", "detail": "started"}
        ),
    )
    monkeypatch.setattr(
        control.control_service,
        "close",
        lambda: calls.append(("closed", True)),
    )
    monkeypatch.setattr(
        web_main.uvicorn,
        "run",
        lambda *args, **kwargs: calls.append(("uvicorn", SimpleNamespace(args=args, kwargs=kwargs))),
    )
    postmatch = SimpleNamespace(pid=2468)
    strict_ingest = SimpleNamespace(pid=1357)
    map_decision = SimpleNamespace(pid=9753)
    monkeypatch.setattr(web_main, "_start_postmatch_worker", lambda: postmatch)
    monkeypatch.setattr(
        web_main,
        "_start_strict_ingest_worker",
        lambda: strict_ingest,
    )
    monkeypatch.setattr(
        web_main,
        "_start_map_decision_worker",
        lambda: map_decision,
    )
    monkeypatch.setattr(
        web_main,
        "terminate_subprocess_tree",
        lambda process: calls.append(("worker_stopped", process.pid)),
    )

    web_main.main(
        [
            "--database-url",
            "postgresql+psycopg://user:password@localhost:5432/database",
            "--config",
            str(tmp_path / "missing.yaml"),
        ]
    )

    assert ("vision_supervisor", FakeStore.connection) in calls
    assert ("raybet_collector", FakeStore.connection) in calls
    assert ("vision_supervisor_options", {"ignore_supervisor_heartbeat": True}) in calls
    assert ("raybet_collector_options", {"ignore_supervisor_heartbeat": True}) in calls
    assert ("worker_stopped", 2468) in calls
    assert ("worker_stopped", 1357) in calls
    assert ("worker_stopped", 9753) in calls
    assert calls[-1] == ("closed", True)
    assert web_main.os.environ["DATABASE_URL"] == (
        "postgresql+psycopg://before:before@localhost/before"
    )
