from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_DATABASE_DOCS = (
    ROOT / "README.md",
    ROOT / "live_betting" / "README.md",
    ROOT / "docs" / "monitoring-console-operations-manual.md",
)
RETIRED_SQLITE_COMMANDS = (
    "scripts/database_cutover.py",
    "scripts/database_bundle.py",
    "scripts/backup_database.py",
    "scripts/restore_database.py",
    "scripts/compact_legacy_odds.py",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_active_runbooks_use_postgres_runtime_authority() -> None:
    for document in ACTIVE_DATABASE_DOCS:
        content = _text(document)
        assert "DATABASE_URL" in content
        assert "alembic upgrade head" in content
        assert "--database data/dota2.db" not in content
        assert "--database data\\dota2.db" not in content


def test_active_runbooks_do_not_publish_retired_sqlite_operations() -> None:
    for document in ACTIVE_DATABASE_DOCS:
        content = _text(document)
        for command in RETIRED_SQLITE_COMMANDS:
            assert command not in content


def test_runbooks_document_the_one_time_read_only_import() -> None:
    for document in ACTIVE_DATABASE_DOCS:
        content = _text(document)
        assert "scripts/migrate_sqlite_to_postgres.py" in content.replace("\\", "/")
        assert "--sqlite" in content
        assert "--postgres" in content
        assert "--dry-run" in content


def test_runtime_entrypoints_publish_database_url_option() -> None:
    commands = (
        [sys.executable, "-m", "fetch.main", "--help"],
        [sys.executable, "-m", "web.main", "--help"],
        [sys.executable, "-m", "live_betting.monitor", "--help"],
        [sys.executable, str(ROOT / "scripts" / "run_dota_shadow_service.py"), "--help"],
        [
            sys.executable,
            str(ROOT / "scripts" / "migrate_sqlite_to_postgres.py"),
            "--help",
        ],
    )
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        if command[-2:] == [
            str(ROOT / "scripts" / "migrate_sqlite_to_postgres.py"),
            "--help",
        ]:
            assert "--sqlite" in completed.stdout
            assert "--postgres" in completed.stdout
        else:
            assert "--database-url" in completed.stdout
            assert "--database " not in completed.stdout


def test_migration_readme_tracks_current_alembic_head() -> None:
    content = _text(ROOT / "database" / "migrations" / "README.md")
    assert "20260802_0021" in content
    assert "PostgreSQL-only" in content
    assert "does not create a SQLite backup" in content


def test_supervisor_runbook_keeps_only_active_start_flags() -> None:
    content = _text(ROOT / "README.md")
    assert "--start-collector" in content
    assert "--start-strict-ingest" in content
    for retired_flag in (
        "--start-vision",
        "--start-shadow",
        "--start-postmatch",
        "--draft-deployment-key",
    ):
        assert retired_flag not in content
