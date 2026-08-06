from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_direct_script_help_resolves_repository_imports(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "assign_strict_event_roles.py"),
            "--help",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--availability-mode" in completed.stdout
    assert "--database-url" in completed.stdout
