"""Run the existing RayBet supervisor with stabilized vision watchers."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.supervise_raybet_streams as supervisor  # noqa: E402


def stable_watcher_command(
    database_url: str,
    match_id: str,
    output_dir: Path,
    evidence_dir: Path,
) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "scripts" / "watch_raybet_stream_stable.py"),
        "--match-id",
        match_id,
        "--database-url",
        database_url,
        "--output",
        str(output_dir / f"{match_id}.jsonl"),
        "--evidence-dir",
        str(evidence_dir),
        "--interval",
        "1",
        "--evidence-interval",
        "30",
        "--refresh-url",
    ]


def main() -> int:
    os.environ.setdefault(
        "VISION_DEBUG_DIR",
        str(ROOT / "data" / "live_betting" / "vision_debug"),
    )
    supervisor.watcher_command = stable_watcher_command
    return supervisor.main()


if __name__ == "__main__":
    raise SystemExit(main())
