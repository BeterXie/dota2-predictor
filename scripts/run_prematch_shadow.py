"""Run one prospective prematch shadow collection and settlement cycle."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from event_intelligence.prematch_deployment import (  # noqa: E402
    load_frozen_prematch_deployment_json,
)
from event_intelligence.prematch_shadow import (  # noqa: E402
    collect_prematch_shadow,
    evaluate_prematch_prospective_gate,
    load_prematch_shadow_metrics,
    settle_ready_prematch_shadows,
)
from event_intelligence.storage import IntelligenceStorage  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--deployment",
        type=Path,
        required=True,
        help="canonical prospective FrozenPrematchDeployment JSON",
    )
    parser.add_argument(
        "--cutoff-source",
        default="prospective_draft_complete",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observed_at = datetime.now(timezone.utc)
    deployment = load_frozen_prematch_deployment_json(
        args.deployment.read_text(encoding="utf-8")
    )
    with IntelligenceStorage(require_database_url(args.database_url)) as storage:
        storage.init_schema(seed_events=False)
        collection = collect_prematch_shadow(
            storage.connection,
            deployment,
            observed_at=observed_at,
            cutoff_source=args.cutoff_source,
        )
        settlement = settle_ready_prematch_shadows(
            storage.connection,
            observed_at=observed_at,
        )
        metrics = load_prematch_shadow_metrics(storage.connection)
        decision = evaluate_prematch_prospective_gate(
            metrics,
            calibration_gate_passed=deployment.calibration_artifact.gate_passed,
            incremental_gate_passed=True,
        )
    print(
        json.dumps(
            {
                "match_id": collection.prediction.match_id,
                "persistence": asdict(collection.persistence),
                "settlement": asdict(settlement),
                "metrics": asdict(metrics),
                "prospective_decision": asdict(decision),
            },
            allow_nan=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
