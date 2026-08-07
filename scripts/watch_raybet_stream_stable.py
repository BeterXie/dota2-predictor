"""Run the RayBet watcher with the stabilized vision runtime installed."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.watch_raybet_stream as watcher  # noqa: E402
from vision.stable_runtime import install_stable_runtime  # noqa: E402


def main() -> int:
    install_stable_runtime(watcher)
    return watcher.main()


if __name__ == "__main__":
    raise SystemExit(main())
