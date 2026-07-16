"""RayBet collection loop."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .health import record_health
from .markets import normalized_state_hash, snapshots_from_payload
from .models import utc_now
from .raybet import RayBetClient
from .storage import LiveBettingStore


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _write_raw(raw_dir: Path, match_id: str, payload: dict[str, Any], now: datetime) -> Path:
    path = raw_dir / now.strftime("%Y-%m-%d") / match_id
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"{now.strftime('%H%M%S_%f')}.json.gz"
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    return target


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _direct_observation_key(
    match_id: str, observed_at: datetime, payload_fingerprint: str
) -> str:
    value = f"direct\n{match_id}\n{observed_at.isoformat()}\n{payload_fingerprint}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def collect_once(
    store: LiveBettingStore,
    client: RayBetClient,
    raw_dir: Path,
    list_rows: list[dict[str, Any]] | None = None,
    raw_fingerprints: dict[str, str] | None = None,
) -> dict[str, int]:
    list_rows = list_rows if list_rows is not None else client.live_matches()
    odds_count = 0
    changed_count = 0
    raw_fingerprints = raw_fingerprints if raw_fingerprints is not None else {}
    for list_row in list_rows:
        payload = client.match_odds(str(list_row.get("id")))
        observed_at = utc_now()
        result = payload.get("result") or {}
        match_id = str(result.get("id"))
        fingerprint = _fingerprint(payload)
        if raw_fingerprints.get(match_id) != fingerprint:
            _write_raw(raw_dir, match_id, payload, observed_at)
            raw_fingerprints[match_id] = fingerprint
        snapshots = snapshots_from_payload(payload, received_at=observed_at)
        with store.transaction():
            store.upsert_raybet_match(result, observed_at)
            _, changes = store.store_odds_observation(
                source="direct",
                observation_key=_direct_observation_key(
                    match_id, observed_at, fingerprint
                ),
                source_event_id=None,
                raybet_match_id=match_id,
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash(snapshots),
                snapshots=snapshots,
            )
            changed_count += changes
        odds_count += len(snapshots)
    store.record_collector("raybet", success_at=utc_now())
    return {"matches": len(list_rows), "odds": odds_count, "changed": changed_count}


def run(args: argparse.Namespace) -> int:
    load_dotenv()
    db_path = Path(args.database)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(args.raw_dir)

    with LiveBettingStore(db_path) as store, RayBetClient() as client:
        if not getattr(args, "schema_prepared", False):
            store.init_schema()
        started_at = utc_now()
        record_health(
            store.connection,
            "raybet_worker",
            "starting",
            heartbeat_at=started_at,
            details={"source": "worker"},
        )
        failures = 0
        list_rows: list[dict[str, Any]] | None = None
        raw_fingerprints: dict[str, str] = {}
        next_list_refresh = 0.0
        while True:
            try:
                monotonic_now = time.monotonic()
                if list_rows is None or monotonic_now >= next_list_refresh:
                    list_rows = client.live_matches()
                    next_list_refresh = monotonic_now + args.list_interval
                summary = collect_once(
                    store, client, raw_dir, list_rows=list_rows,
                    raw_fingerprints=raw_fingerprints,
                )
                failures = 0
                succeeded_at = utc_now()
                record_health(
                    store.connection,
                    "raybet_worker",
                    "healthy",
                    heartbeat_at=succeeded_at,
                    success_at=succeeded_at,
                    details={"source": "worker", **summary},
                )
                logger.info(
                    "collected matches=%d odds=%d changed=%d",
                    summary["matches"], summary["odds"], summary["changed"],
                )
            except Exception as exc:
                failures += 1
                now = utc_now()
                store.record_collector("raybet", error_at=now, error=f"{type(exc).__name__}: {exc}")
                record_health(
                    store.connection,
                    "raybet_worker",
                    "degraded",
                    heartbeat_at=now,
                    error_at=now,
                    error=type(exc).__name__,
                    details={"source": "worker", "consecutive_failures": failures},
                )
                logger.error("RayBet collection failed: %s", exc)
                if args.once:
                    return 1
            if args.once:
                return 0
            delay = min(args.max_backoff, args.interval * (2 ** min(failures, 5)))
            time.sleep(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "data" / "dota2.db"))
    parser.add_argument("--raw-dir", default=str(ROOT / "data" / "live_betting" / "raw"))
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--list-interval", type=float, default=15.0)
    parser.add_argument("--max-backoff", type=float, default=300.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(run(parse_args()))
