"""Run formal nested chronological Team Rating evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from event_intelligence.draft_features import AvailabilityMode  # noqa: E402
from event_intelligence.storage import IntelligenceStorage  # noqa: E402
from event_intelligence.team_rating_backtest import (  # noqa: E402
    report_as_dict,
    report_as_markdown,
    run_team_rating_backtest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--availability-mode",
        choices=(AvailabilityMode.RECONSTRUCTED.value,),
        default=AvailabilityMode.RECONSTRUCTED.value,
        help="formal post-match evaluation is reconstructed walk-forward only",
    )
    parser.add_argument("--dry-run", action="store_true", help="compute without writes")
    parser.add_argument(
        "--checkpoint-latest",
        action="store_true",
        help="persist only the final target's pre-match rating state",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with IntelligenceStorage(require_database_url(args.database_url)) as storage:
        storage.init_schema()
        report = run_team_rating_backtest(
            storage,
            availability_mode=AvailabilityMode(args.availability_mode),
            dry_run=args.dry_run,
            checkpoint_latest=args.checkpoint_latest,
        )
    payload = json.dumps(report_as_dict(report), ensure_ascii=True, sort_keys=True)
    if args.json_output is not None:
        _write(args.json_output, payload + "\n")
    if args.markdown_output is not None:
        _write(args.markdown_output, report_as_markdown(report))
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
