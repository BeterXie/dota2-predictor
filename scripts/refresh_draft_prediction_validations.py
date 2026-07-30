"""Rebuild persisted draft input-lineage proofs outside request handling."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.incremental import refresh_draft_prediction_validations  # noqa: E402
from event_intelligence.storage import IntelligenceStorage  # noqa: E402
from database.engine import require_database_url  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    args = parser.parse_args()

    storage = IntelligenceStorage(require_database_url(args.database_url))
    started = time.perf_counter()
    try:
        storage.init_schema()
        keys = refresh_draft_prediction_validations(storage.connection)
    finally:
        storage.close()
    print(
        json.dumps(
            {
                "validated_predictions": len(keys),
                "validated_matches": len({match_id for _, match_id in keys}),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
