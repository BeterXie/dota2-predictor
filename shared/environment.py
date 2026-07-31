"""Project environment-file loading helpers."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


def load_environment_file(
    path: Path,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Load simple KEY=VALUE entries without overriding process environment."""

    target = os.environ if environ is None else environ
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        target.setdefault(key.strip(), value.strip())
