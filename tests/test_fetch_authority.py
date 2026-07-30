from __future__ import annotations

import pytest

from fetch import main as fetch_main


POSTGRES_URL = (
    "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
)


def test_fetch_passes_explicit_postgres_url_to_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[dict[str, object], bool, int | None, str | None]] = []

    async def run(
        config: dict[str, object],
        force: bool,
        match_id: int | None,
        database_url: str | None = None,
    ) -> None:
        observed.append((config, force, match_id, database_url))

    monkeypatch.setattr(fetch_main, "load_config", lambda: {"leagues": []})
    monkeypatch.setattr(fetch_main, "run", run)

    fetch_main.main(
        ["--database-url", POSTGRES_URL, "--match-id", "42", "--force"]
    )

    assert observed == [({"leagues": []}, True, 42, POSTGRES_URL)]


def test_fetch_database_url_precedence_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_url = POSTGRES_URL.replace("dota2_predictor", "environment")
    configured_url = POSTGRES_URL.replace("dota2_predictor", "configured")
    explicit_url = POSTGRES_URL.replace("dota2_predictor", "explicit")
    monkeypatch.setenv("DATABASE_URL", environment_url)

    assert fetch_main.resolve_database_url(
        {"database_url": configured_url}, explicit_url
    ) == explicit_url
    assert fetch_main.resolve_database_url(
        {"database_url": configured_url}
    ) == configured_url
    assert fetch_main.resolve_database_url({}) == environment_url


def test_fetch_rejects_sqlite_runtime_url() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        fetch_main.resolve_database_url(
            {"database_url": "sqlite:///data/dota2.db"}
        )
