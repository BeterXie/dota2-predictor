"""Run exact-draft post-match labeling for one RayBet series."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.postmatch_monitor import main


if __name__ == "__main__":
    raise SystemExit(main())
