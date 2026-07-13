"""Persist PandaScore Dota 2 frames and events from open Live API sockets."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from .models import utc_now
from .monitor import ROOT, load_dotenv
from .providers.pandascore import PandaScoreProvider
from .storage import LiveBettingStore


logger = logging.getLogger(__name__)


def _is_dota(row: dict[str, Any]) -> bool:
    match = row.get("match") or {}
    videogame = match.get("videogame") or {}
    text = " ".join(
        str(value or "") for value in (
            videogame.get("slug"), videogame.get("name"), match.get("videogame_slug")
        )
    ).lower()
    return "dota" in text


async def consume_frames(
    store: LiveBettingStore, provider: PandaScoreProvider, match_id: str
) -> None:
    collector = f"pandascore:frames:{match_id}"
    try:
        async for frame in provider.stream_frames(match_id):
            store.insert_frame(frame)
            store.record_collector(collector, success_at=utc_now(), cursor=frame.sequence)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        store.record_collector(
            collector, error_at=utc_now(), error=f"{type(exc).__name__}: {exc}", gap=True
        )
        logger.error("frames stream %s stopped: %s", match_id, exc)


async def consume_events(
    store: LiveBettingStore, provider: PandaScoreProvider, match_id: str
) -> None:
    collector = f"pandascore:events:{match_id}"
    try:
        async for event in provider.stream_events(match_id):
            store.insert_event(event)
            store.record_collector(collector, success_at=utc_now())
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        store.record_collector(
            collector, error_at=utc_now(), error=f"{type(exc).__name__}: {exc}", gap=True
        )
        logger.error("events stream %s stopped: %s", match_id, exc)


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    token = os.environ.get("PANDASCORE_TOKEN", "")
    if not token:
        logger.error("PANDASCORE_TOKEN is required for live collection")
        return 2
    provider = PandaScoreProvider(token)
    store = LiveBettingStore(args.database)
    store.init_schema()
    tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
    try:
        while True:
            endpoints = await provider.list_live_endpoints()
            available: dict[str, set[str]] = {}
            for row in endpoints:
                if not _is_dota(row):
                    continue
                match_id = str((row.get("match") or {}).get("id") or row.get("match_id") or "")
                if not match_id:
                    continue
                available[match_id] = {
                    str(endpoint.get("type"))
                    for endpoint in row.get("endpoints") or [] if endpoint.get("open")
                }
            for match_id, endpoint_types in available.items():
                for stream_type, consumer in (
                    ("frames", consume_frames), ("events", consume_events)
                ):
                    if stream_type not in endpoint_types:
                        continue
                    key = (match_id, stream_type)
                    if key not in tasks or tasks[key].done():
                        tasks[key] = asyncio.create_task(consumer(store, provider, match_id))
            for key, task in list(tasks.items()):
                if key[0] not in available or key[1] not in available[key[0]]:
                    task.cancel()
                    del tasks[key]
            if args.once:
                logger.info("discovered %d live Dota 2 matches", len(available))
                return 0
            await asyncio.sleep(args.discovery_interval)
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        store.close()
        await provider.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "data" / "dota2.db"))
    parser.add_argument("--discovery-interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
