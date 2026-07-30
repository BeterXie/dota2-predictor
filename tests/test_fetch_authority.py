from __future__ import annotations

import pytest

from fetch import main as fetch_main


def test_fetch_passes_explicit_postgres_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+psycopg://dota2:test@localhost/dota2"
    observed: list[str | None] = []

    async def run(
        _config: dict[str, object],
        _force: bool,
        _match_id: int | None,
        selected_url: str | None = None,
    ) -> None:
        observed.append(selected_url)

    monkeypatch.setattr(fetch_main, "load_config", lambda: {})
    monkeypatch.setattr(fetch_main, "run", run)

    fetch_main.main(
        ["--database-url", database_url, "--match-id", "42"]
    )

    assert observed == [database_url]


def test_fetch_uses_database_url_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str | None] = []

    async def run(
        _config: dict[str, object],
        _force: bool,
        _match_id: int | None,
        selected_url: str | None = None,
    ) -> None:
        observed.append(selected_url)

    monkeypatch.setattr(fetch_main, "load_config", lambda: {})
    monkeypatch.setattr(fetch_main, "run", run)

    fetch_main.main([])

    assert observed == [None]


def test_fetch_rejects_duplicate_database_url_before_loading_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load() -> dict[str, object]:
        raise AssertionError("config loaded")

    monkeypatch.setattr(fetch_main, "load_config", load)

    with pytest.raises(SystemExit):
        fetch_main.main(
            [
                "--database-url=postgresql+psycopg://first/db",
                "--database-url",
                "postgresql+psycopg://second/db",
            ]
        )
