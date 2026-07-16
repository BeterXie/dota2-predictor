"""Replay immutable browser events and print a durable-state digest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from live_betting.browser_replay import replay_browser_events


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--restart-after", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--mode", choices=("arrival", "capture"), default="arrival")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = replay_browser_events(
        args.source,
        args.target,
        restart_after=args.restart_after,
        overwrite=args.overwrite,
        mode=args.mode,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
