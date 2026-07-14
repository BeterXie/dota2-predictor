"""Download Dota hero portraits used to rebuild live HUD features."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import cv2
import httpx
import numpy as np


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HEROES_URL = "https://api.opendota.com/api/constants/heroes"
PORTRAIT_BASE = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes"
)
DEFAULT_OUTPUT = ROOT / "vision" / "templates" / "heroes"


def valid_portrait_bytes(content: bytes) -> bool:
    if not content:
        return False
    try:
        image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        return False
    return bool(image is not None and image.size)


async def fetch_hero_portraits(destination: Path) -> tuple[int, int]:
    destination.mkdir(parents=True, exist_ok=True)
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=12)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=15.0),
        limits=limits,
        headers={"User-Agent": "dota2-predictor/1.0"},
    ) as client:
        response = await client.get(HEROES_URL)
        response.raise_for_status()
        heroes = response.json()
        metadata: dict[str, dict[str, str | int]] = {}
        semaphore = asyncio.Semaphore(8)

        async def download(hero_id: int, internal_name: str) -> bool:
            target = destination / f"{hero_id}.png"
            if target.exists():
                try:
                    if valid_portrait_bytes(target.read_bytes()):
                        return True
                except OSError:
                    pass
            async with semaphore:
                for attempt in range(3):
                    try:
                        portrait = await client.get(
                            f"{PORTRAIT_BASE}/{internal_name}.png"
                        )
                        if portrait.status_code == 200 and valid_portrait_bytes(
                            portrait.content
                        ):
                            temporary = target.with_suffix(".png.part")
                            temporary.write_bytes(portrait.content)
                            temporary.replace(target)
                            return True
                        if portrait.status_code == 404:
                            return False
                    except httpx.RequestError:
                        if attempt == 2:
                            return False
                    await asyncio.sleep(2**attempt)
            return False

        jobs = []
        for key, hero in heroes.items():
            hero_id = int(hero.get("id") or key)
            internal_name = str(hero["name"]).removeprefix("npc_dota_hero_")
            metadata[str(hero_id)] = {
                "id": hero_id,
                "name": internal_name,
                "localized_name": str(hero.get("localized_name") or internal_name),
            }
            jobs.append(download(hero_id, internal_name))
        results = await asyncio.gather(*jobs)

    metadata_path = destination / "heroes.json"
    temporary_metadata = metadata_path.with_suffix(".json.part")
    temporary_metadata.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_metadata.replace(metadata_path)
    downloaded = sum(results)
    return downloaded, len(results) - downloaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    downloaded, failed = asyncio.run(fetch_hero_portraits(args.output))
    print(f"hero portraits downloaded={downloaded} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
