from pathlib import Path

from shared.environment import load_environment_file
from web.main import resolve_database_url


def test_environment_file_provides_database_url(tmp_path: Path) -> None:
    environment_file = tmp_path / ".env"
    environment_file.write_text(
        "# local runtime\n"
        "DATABASE_URL=postgresql+psycopg://local@localhost/dota2_predictor\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}

    load_environment_file(environment_file, environ=environment)

    assert environment["DATABASE_URL"].endswith("/dota2_predictor")


def test_environment_file_does_not_override_process_environment(tmp_path: Path) -> None:
    environment_file = tmp_path / ".env"
    environment_file.write_text(
        "DATABASE_URL=postgresql+psycopg://file@localhost/from_file\n",
        encoding="utf-8",
    )
    environment = {
        "DATABASE_URL": "postgresql+psycopg://process@localhost/from_process"
    }

    load_environment_file(environment_file, environ=environment)

    assert environment["DATABASE_URL"].endswith("/from_process")


def test_cli_database_url_remains_highest_priority_after_environment_load(
    tmp_path: Path,
) -> None:
    environment_file = tmp_path / ".env"
    environment_file.write_text(
        "DATABASE_URL=postgresql+psycopg://file@localhost/from_file\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}
    load_environment_file(environment_file, environ=environment)

    database_url, source = resolve_database_url(
        "postgresql+psycopg://cli@localhost/from_cli",
        {},
        tmp_path / "config.yaml",
        environment,
    )

    assert database_url.endswith("/from_cli")
    assert source == "cli"
