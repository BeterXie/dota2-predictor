"""Run strict-scope causal Dota 2 draft walk-forward evaluation."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    database_writer_authority,
)
from event_intelligence.backtest import (  # noqa: E402
    report_as_dict,
    run_strict_draft_backtest,
)
from event_intelligence.draft_features import AvailabilityMode  # noqa: E402
from event_intelligence.draft_model import (  # noqa: E402
    DEFAULT_L2_REGULARIZATION,
    DEFAULT_MIN_SAMPLES,
)


def _minimum_samples(value: str) -> int:
    parsed = int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("must be at least 2")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, default=ROOT / "data" / "dota2.db")
    parser.add_argument(
        "--availability-mode",
        choices=tuple(mode.value for mode in AvailabilityMode),
        default=AvailabilityMode.RECONSTRUCTED.value,
        help="historical evidence availability policy",
    )
    parser.add_argument(
        "--assignment-version",
        help="pinned expected-position assignment version",
    )
    parser.add_argument(
        "--min-samples",
        type=_minimum_samples,
        default=DEFAULT_MIN_SAMPLES,
        help="minimum earlier maps required to train one target model",
    )
    parser.add_argument(
        "--l2-regularization",
        type=_positive_float,
        default=DEFAULT_L2_REGULARIZATION,
    )
    parser.add_argument("--dry-run", action="store_true", help="compute without writes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with database_writer_authority(args.database):
        report = run_strict_draft_backtest(
            args.database,
            availability_mode=AvailabilityMode(args.availability_mode),
            assignment_version=args.assignment_version,
            dry_run=args.dry_run,
            min_samples=args.min_samples,
            l2_regularization=args.l2_regularization,
        )
    print(json.dumps(report_as_dict(report), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
