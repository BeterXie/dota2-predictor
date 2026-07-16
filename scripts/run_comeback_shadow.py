"""Run the read-only Dota comeback shadow monitor."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.shadow_monitor import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
