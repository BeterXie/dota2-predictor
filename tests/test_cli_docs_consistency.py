from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_documented_collector_commands_use_the_current_raw_root() -> None:
    documents = (
        ROOT / "README.md",
        ROOT / "live_betting" / "README.md",
        ROOT / "docs" / "monitoring-console-operations-manual.md",
    )
    stale = re.compile(r"data[/\\]live_betting[/\\]raw(?!-v2)(?:[/\\]|\b)")
    for document in documents:
        text = document.read_text(encoding="utf-8")
        assert "--raw-dir data/live_betting/raw-v2" in text
        assert stale.search(text) is None


def test_database_operations_runbook_covers_the_published_clis() -> None:
    runbook = (ROOT / "live_betting" / "README.md").read_text(encoding="utf-8")
    required = (
        "scripts/database_cutover.py checkpoint",
        "scripts/database_cutover.py verify-prepared",
        "scripts/compact_legacy_odds.py",
        "scripts/database_bundle.py create",
        "scripts/database_bundle.py verify",
        "scripts/database_bundle.py restore",
        '--odds-raw-root (Join-Path $compactionDir "live_betting/raw-v2")',
    )
    for command in required:
        assert command in runbook
    assert "`M_c = 512 MiB`" in runbook
    assert "`M_b = 512 MiB`" in runbook
    assert "`5L + R + M_c`" in runbook
    assert "`2L` for the work database" in runbook
    assert "`2L` for the `VACUUM INTO` output" in runbook
    assert "`L` for generated\n   raw artifacts" in runbook
    assert "`C + A + M_b`" in runbook
    assert "it is not a read-only verification command" in runbook
    assert "--database`, then\n`DATABASE_PATH`, then `web/config.yaml`" in runbook


def test_cutover_runbook_uses_one_candidate_database_for_every_process() -> None:
    runbook = (ROOT / "live_betting" / "README.md").read_text(encoding="utf-8")
    required = (
        '$candidateDb = Join-Path $restoreDir "dota2.db"',
        "python -m web.main --database $candidateDb",
        "python scripts/run_dota_shadow_service.py",
        "--database $candidateDb",
        "--start-collector",
        "--start-companion",
        "--start-shadow",
        "--start-vision",
        "--start-mail",
        "--start-strict-ingest",
        "--start-postmatch",
        "--start-draft-publisher",
    )
    for value in required:
        assert value in runbook
    assert "Web process and all workers must use `$candidateDb`" in runbook
    assert "never run the old\nproduction database and the candidate database" in runbook


def test_compactor_help_tracks_the_current_schema_instead_of_a_number() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "compact_legacy_odds.py"), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "current live schema" in completed.stdout
    assert re.search(r"prepared\s+v\d+", completed.stdout, re.IGNORECASE) is None


def test_database_cutover_help_publishes_the_documented_commands() -> None:
    command = ROOT / "scripts" / "database_cutover.py"
    completed = subprocess.run(
        [sys.executable, str(command), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "checkpoint" in completed.stdout
    assert "verify-prepared" in completed.stdout
    for subcommand in ("checkpoint", "verify-prepared"):
        help_result = subprocess.run(
            [sys.executable, str(command), subcommand, "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "--database" in help_result.stdout


def test_database_bundle_create_help_exposes_explicit_revision_adoption() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "database_bundle.py"),
            "create",
            "--help",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--resume" in completed.stdout
    assert "--adopt-resume-from-git-commit" in completed.stdout
