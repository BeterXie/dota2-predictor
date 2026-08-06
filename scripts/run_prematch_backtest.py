"""Run formal chronological Prematch M6 evaluation and persistence."""

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
from event_intelligence.prematch_backtest import (  # noqa: E402
    persist_prematch_backtest_result,
    run_prematch_backtest,
)
from event_intelligence.prematch_report import (  # noqa: E402
    build_prematch_report,
    report_as_dict,
    report_as_markdown,
)
from event_intelligence.storage import IntelligenceStorage  # noqa: E402


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        required=True,
        help="root containing authoritative R.O.S.H. artifacts",
    )
    parser.add_argument(
        "--availability-mode",
        choices=(AvailabilityMode.RECONSTRUCTED.value,),
        default=AvailabilityMode.RECONSTRUCTED.value,
        help="formal post-match evaluation is reconstructed walk-forward only",
    )
    parser.add_argument("--dry-run", action="store_true", help="compute without writes")
    parser.add_argument(
        "--max-maps",
        type=_positive_int,
        help="bound formal maps for staged acceptance runs",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_maps is not None and not args.dry_run:
        parser.error("--max-maps requires --dry-run")
    with IntelligenceStorage(require_database_url(args.database_url)) as storage:
        storage.init_schema()
        result = run_prematch_backtest(
            storage,
            artifact_root=args.artifact_root,
            availability_mode=AvailabilityMode(args.availability_mode),
            max_maps=args.max_maps,
        )
        report = build_prematch_report(result)
        payload = json.dumps(
            report_as_dict(report),
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
        )
        markdown = None if args.markdown_output is None else report_as_markdown(report)
        persist_prematch_backtest_result(
            result,
            storage,
            report=report,
            dry_run=args.dry_run,
        )
    if args.json_output is not None:
        _write(args.json_output, payload + "\n")
    if args.markdown_output is not None and markdown is not None:
        _write(args.markdown_output, markdown)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
