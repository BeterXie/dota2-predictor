"""RayBet collection loop and PandaScore fixture linking."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .markets import normalized_state_hash, snapshots_from_payload
from .match_linker import choose_unique, score_candidate
from .models import ProviderMatch, utc_now
from .providers.pandascore import PandaScoreProvider
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


def _raybet_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc)


def _split_match_name(value: str) -> tuple[str, str]:
    parts = value.split(" - VS - ", 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (value.strip(), "")


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


async def fetch_pandascore_matches(token: str) -> list[ProviderMatch]:
    provider = PandaScoreProvider(token)
    try:
        live, upcoming = await asyncio.gather(
            provider.list_live_matches(), provider.list_upcoming_matches()
        )
        return live + upcoming
    finally:
        await provider.close()


def link_matches(
    store: LiveBettingStore,
    raybet_rows: list[dict[str, Any]],
    provider_matches: list[ProviderMatch],
    now: datetime,
) -> None:
    for provider_match in provider_matches:
        store.upsert_provider_match(provider_match, now)
    for row in raybet_rows:
        team_one, team_two = _split_match_name(str(row.get("match_name") or ""))
        round_name = str(row.get("round") or "").lower()
        best_of = int(round_name[2:]) if round_name.startswith("bo") and round_name[2:].isdigit() else None
        candidates = [
            score_candidate(
                ray_team_one=team_one,
                ray_team_two=team_two,
                ray_tournament=str(row.get("tournament_name") or ""),
                ray_scheduled_at=_raybet_time(row.get("start_time")),
                ray_best_of=best_of,
                candidate=match,
            )
            for match in provider_matches
        ]
        selected = choose_unique(candidates)
        if selected:
            store.upsert_match_link(
                str(row.get("id")), "pandascore", selected.provider_match_id,
                selected.confidence, "accepted", ";".join(selected.reasons), now,
            )


def collect_once(
    store: LiveBettingStore,
    client: RayBetClient,
    raw_dir: Path,
    provider_matches: list[ProviderMatch] | None = None,
    list_rows: list[dict[str, Any]] | None = None,
    raw_fingerprints: dict[str, str] | None = None,
) -> dict[str, int]:
    now = utc_now()
    list_rows = list_rows if list_rows is not None else client.live_matches()
    details: list[dict[str, Any]] = []
    odds_count = 0
    changed_count = 0
    raw_fingerprints = raw_fingerprints if raw_fingerprints is not None else {}
    for list_row in list_rows:
        payload = client.match_odds(str(list_row.get("id")))
        observed_at = utc_now()
        result = payload.get("result") or {}
        details.append(result)
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
    if provider_matches:
        link_matches(store, details, provider_matches, now)
    store.record_collector("raybet", success_at=now)
    return {"matches": len(details), "odds": odds_count, "changed": changed_count}


def load_pandascore_matches(enabled: bool) -> list[ProviderMatch]:
    """Load optional commercial fixture data only after explicit CLI opt-in."""
    if not enabled:
        logger.info("PandaScore fixture linking is disabled")
        return []
    token = os.environ.get("PANDASCORE_TOKEN", "")
    if not token:
        logger.warning("PANDASCORE_TOKEN is absent; PandaScore linking is disabled")
        return []
    return asyncio.run(fetch_pandascore_matches(token))


def run(args: argparse.Namespace) -> int:
    load_dotenv()
    db_path = Path(args.database)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(args.raw_dir)
    provider_matches = load_pandascore_matches(
        getattr(args, "enable_pandascore", False)
    )

    with LiveBettingStore(db_path) as store, RayBetClient() as client:
        store.init_schema()
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
                    store, client, raw_dir, provider_matches, list_rows=list_rows,
                    raw_fingerprints=raw_fingerprints,
                )
                failures = 0
                logger.info(
                    "collected matches=%d odds=%d changed=%d",
                    summary["matches"], summary["odds"], summary["changed"],
                )
            except Exception as exc:
                failures += 1
                now = utc_now()
                store.record_collector("raybet", error_at=now, error=f"{type(exc).__name__}: {exc}")
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
    parser.add_argument(
        "--enable-pandascore",
        action="store_true",
        help="explicitly enable optional PandaScore fixture linking",
    )
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(run(parse_args()))
