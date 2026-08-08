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


def test_web_entrypoint_starts_and_owns_stable_vision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(web_main, "load_environment_file", lambda _path: None)
    monkeypatch.setattr(web_main, "LiveBettingStore", lambda _url: FakeStore())
    monkeypatch.setattr(web_main, "verify_runtime_schema", lambda _connection: None)
    monkeypatch.setattr(queries, "init_db", lambda url: calls.append(("database", url)))
    monkeypatch.setattr(
        control.control_service,
        "ensure_started",
        lambda connection, component: (
            calls.append((component, connection))
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

    web_main.main(
        [
            "--database-url",
            "postgresql+psycopg://user:password@localhost:5432/database",
            "--config",
            str(tmp_path / "missing.yaml"),
        ]
    )

    assert ("vision_supervisor", FakeStore.connection) in calls
    assert calls[-1] == ("closed", True)
