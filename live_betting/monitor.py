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
from .sanitize import sanitize_raybet_payload
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


def _write_raw(raw_dir: Path, match_id: str, payload: Any, now: datetime) -> Path:
    payload = sanitize_raybet_payload(payload)
    path = raw_dir / now.strftime("%Y-%m-%d") / match_id
    path.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    target = path / f"{now.strftime('%H%M%S_%f')}_{digest}.json.gz"
    if not target.exists():
        with gzip.open(target, "wb") as handle:
            handle.write(canonical)
    return target


def _fingerprint(payload: dict[str, Any]) -> str:
    payload = sanitize_raybet_payload(payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _direct_observation_key(
    match_id: str, observed_at: datetime, payload_fingerprint: str
) -> str:
    value = f"direct\n{match_id}\n{observed_at.isoformat()}\n{payload_fingerprint}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def completed_refresh_due(
    completed_rows: list[dict[str, Any]] | None,
    monotonic_now: float,
    next_refresh: float,
) -> bool:
    """Return whether the low-frequency completed feed should be fetched."""
    return completed_rows is None or monotonic_now >= next_refresh


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
    error_count = 0
    raw_fingerprints = raw_fingerprints if raw_fingerprints is not None else {}
    _write_raw(raw_dir, "_match_list", {"result": list_rows}, utc_now())
    for list_row in list_rows:
        match_id = ""
        try:
            if not isinstance(list_row, dict):
                raise ValueError("live match list row is not an object")
            match_id = str(list_row.get("id") or "")
            if not match_id.isdigit():
                raise ValueError("live match list id is invalid")
            payload = sanitize_raybet_payload(client.match_odds(match_id))
            result = payload.get("result") if isinstance(payload, dict) else None
            if (
                not isinstance(result, dict)
                or str(result.get("id") or "") != match_id
                or type(result.get("game_id")) is not int
                or int(result["game_id"]) != 151
            ):
                raise ValueError("live odds response identity mismatch")
            observed_at = utc_now()
            fingerprint = _fingerprint(payload)
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
        except Exception as error:
            error_count += 1
            logger.warning(
                "RayBet fetch failed for live match_id=%s (%s)",
                match_id or "<invalid>",
                type(error).__name__,
            )
    completed_at = utc_now()
    if error_count:
        store.record_collector(
            "raybet",
            success_at=completed_at if len(list_rows) > error_count else None,
            error_at=completed_at,
            error=f"{error_count} live match(s) failed",
        )
    else:
        store.record_collector("raybet", success_at=completed_at)
    return {
        "matches": len(list_rows) - error_count,
        "listed": len(list_rows),
        "odds": odds_count,
        "changed": changed_count,
        "errors": error_count,
    }


def collect_completed_once(
    store: LiveBettingStore,
    client: RayBetClient,
    raw_dir: Path,
    completed_rows: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Archive final odds for the low-frequency RayBet completed feed.

    RayBet exposes completed matches through ``match_type=4``.  They are
    deliberately collected outside ``collect_once`` so the live 3-second loop
    does not repeatedly enumerate and fetch the whole historical list.  A
    completed response is still stored as an ordinary immutable transport
    observation; its provider row carries status ``3`` and is therefore
    available to post-match reconciliation and the historical UI.
    """
    rows = completed_rows if completed_rows is not None else client.completed_matches()
    odds_count = 0
    changed_count = 0
    error_count = 0
    list_observed_at = utc_now()
    _write_raw(
        raw_dir,
        "_completed_match_list",
        {"result": rows},
        list_observed_at,
    )
    fetched_matches = 0
    for list_row in rows:
        match_id = str(list_row.get("id") or "")
        if not match_id.isdigit():
            continue
        try:
            payload = sanitize_raybet_payload(client.match_odds(match_id))
            result = payload.get("result")
            if (
                not isinstance(result, dict)
                or str(result.get("id") or "") != match_id
                or type(result.get("game_id")) is not int
                or int(result["game_id"]) != 151
            ):
                # Keep the list archive for audit, but never let an identity-
                # mismatched response enter normalized odds or overwrite metadata.
                error_count += 1
                continue
            observed_at = utc_now()
            fingerprint = _fingerprint(payload)
            _write_raw(raw_dir, match_id, payload, observed_at)
            snapshots = snapshots_from_payload(payload, received_at=observed_at)
            # The completed list contract uses status=3.  Some odds responses omit
            # the parent match status, so carry the completed-list status through to
            # the normalized metadata without changing the archived response.
            stored_result = dict(result)
            if stored_result.get("status") in (None, ""):
                stored_result["status"] = list_row.get("status", 3)
            with store.transaction():
                store.upsert_raybet_match(stored_result, observed_at)
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
            fetched_matches += 1
        except Exception as error:
            error_count += 1
            logger.warning(
                "completed RayBet fetch failed for match_id=%s (%s)",
                match_id,
                type(error).__name__,
            )
    completed_at = utc_now()
    if error_count:
        store.record_collector(
            "raybet_completed",
            success_at=completed_at if fetched_matches else None,
            error_at=completed_at,
            error=f"{error_count} completed match(s) failed",
        )
    else:
        store.record_collector("raybet_completed", success_at=completed_at)
    return {
        "matches": fetched_matches,
        "listed": len(rows),
        "odds": odds_count,
        "changed": changed_count,
        "errors": error_count,
    }


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
        completed_rows: list[dict[str, Any]] | None = None
        next_completed_refresh = 0.0
        while True:
            try:
                monotonic_now = time.monotonic()
                if list_rows is None or monotonic_now >= next_list_refresh:
                    list_rows = client.live_matches()
                    next_list_refresh = monotonic_now + args.list_interval
                completed_refresh_needed = False
                completed_summary = {
                    "matches": 0,
                    "listed": len(completed_rows or []),
                    "odds": 0,
                    "changed": 0,
                    "errors": 0,
                }
                if completed_refresh_due(
                    completed_rows, monotonic_now, next_completed_refresh
                ):
                    next_completed_refresh = (
                        monotonic_now + args.completed_interval
                    )
                    try:
                        completed_rows = client.completed_matches()
                    except Exception as error:
                        completed_rows = completed_rows or []
                        completed_summary["errors"] = 1
                        completed_at = utc_now()
                        store.record_collector(
                            "raybet_completed",
                            error_at=completed_at,
                            error=f"completed list failed: {type(error).__name__}",
                        )
                        logger.warning(
                            "completed RayBet list refresh failed (%s)",
                            type(error).__name__,
                        )
                    else:
                        completed_refresh_needed = True
                summary = collect_once(
                    store, client, raw_dir, list_rows=list_rows,
                    raw_fingerprints=raw_fingerprints,
                )
                if completed_refresh_needed:
                    try:
                        completed_summary = collect_completed_once(
                            store, client, raw_dir, completed_rows=completed_rows
                        )
                    except Exception as error:
                        completed_summary["errors"] = 1
                        completed_at = utc_now()
                        store.record_collector(
                            "raybet_completed",
                            error_at=completed_at,
                            error=f"completed collection failed: {type(error).__name__}",
                        )
                        logger.warning(
                            "completed RayBet collection failed (%s)",
                            type(error).__name__,
                        )
                failures = 0
                succeeded_at = utc_now()
                partial_errors = summary["errors"] + completed_summary["errors"]
                worker_status = "degraded" if partial_errors else "healthy"
                collection_succeeded = any(
                    (
                        item["matches"] > 0
                        or (item["listed"] == 0 and item["errors"] == 0)
                    )
                    for item in (summary, completed_summary)
                )
                record_health(
                    store.connection,
                    "raybet_worker",
                    worker_status,
                    heartbeat_at=succeeded_at,
                    success_at=succeeded_at if collection_succeeded else None,
                    error_at=succeeded_at if partial_errors else None,
                    error=(
                        f"{partial_errors} match collection error(s)"
                        if partial_errors
                        else None
                    ),
                    details={
                        "source": "worker",
                        **summary,
                        "completed": completed_summary,
                    },
                )
                logger.info(
                    "collected matches=%d odds=%d changed=%d completed=%d",
                    summary["matches"], summary["odds"], summary["changed"],
                    completed_summary["matches"],
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
    parser.add_argument("--completed-interval", type=float, default=300.0)
    parser.add_argument("--max-backoff", type=float, default=300.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(run(parse_args()))
