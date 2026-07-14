import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)


class OpenDotaClient:
    """Async HTTP client for the OpenDota API with rate limiting and retry."""

    BASE_URL = "https://api.opendota.com"

    def __init__(self, rate_limit: int = 50):
        self._min_interval = 60.0 / rate_limit
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        return self._client

    async def _request(self, endpoint: str) -> dict | list:
        client = await self._get_client()
        url = f"{self.BASE_URL}{endpoint}"

        async with self._lock:
            now = time.monotonic()
            wait = self._last_request + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

        last_status = 0
        for attempt in range(5):
            try:
                response = await client.get(url)
                last_status = response.status_code

                if response.status_code == 429:
                    backoff = 2 ** (attempt + 1)
                    logger.warning("Rate limited (429) on %s, retrying in %ds (attempt %d)",
                                   endpoint, backoff, attempt + 1)
                    await asyncio.sleep(backoff)
                    continue

                if response.status_code >= 500:
                    backoff = 2 ** (attempt + 1)
                    logger.warning("Server error %d on %s, retrying in %ds (attempt %d)",
                                   response.status_code, endpoint, backoff, attempt + 1)
                    await asyncio.sleep(backoff)
                    continue

                response.raise_for_status()
                return response.json()

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                backoff = 2 ** (attempt + 1)
                logger.warning("Connection error on %s: %s, retrying in %ds (attempt %d)",
                               endpoint, e, backoff, attempt + 1)
                await asyncio.sleep(backoff)

        raise RuntimeError(
            f"Failed to fetch {endpoint} after 5 attempts (last status: {last_status})"
        )

    async def get_match(self, match_id: int) -> dict:
        return await self._request(f"/api/matches/{match_id}")

    async def get_leagues(self) -> list[dict]:
        return await self._request("/api/leagues")

    async def get_league_matches(self, league_id: int) -> list[dict]:
        return await self._request(f"/api/leagues/{league_id}/matches")

    async def get_team_matches(self, team_id: int) -> list[dict]:
        return await self._request(f"/api/teams/{team_id}/matches")

    async def get_heroes(self) -> list[dict]:
        return await self._request("/api/heroes")

    async def get_hero_stats(self) -> list[dict]:
        return await self._request("/api/heroStats")

    async def get_hero_matchups(self, hero_id: int) -> list[dict]:
        return await self._request(f"/api/heroes/{hero_id}/matchups")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
