"""Capture diagnostic frames from a RayBet HLS stream."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision.stream_capture import HLSStreamCapture, nonblack_ratio  # noqa: E402


DEFAULT_FIXTURE_DIR = ROOT / "data" / "live_betting" / "live_fixtures"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    with HLSStreamCapture(args.url) as capture:
        for frame in capture.sample(interval=args.interval, count=args.count):
            timestamp = datetime.fromtimestamp(frame.captured_at, timezone.utc)
            name = f"{args.match_id}_{timestamp.strftime('%Y%m%dT%H%M%S_%fZ')}.png"
            path = args.output / name
            cv2.imwrite(str(path), frame.image)
            print(f"{path} nonblack={nonblack_ratio(frame.image):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
