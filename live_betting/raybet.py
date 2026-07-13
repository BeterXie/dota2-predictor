"""Read-only RayBet Dota 2 match and odds client."""

from __future__ import annotations

from typing import Any

from curl_cffi import requests


BASE_URL = "https://cfinfo.365raylinks.com/v2"
SITE_URL = "https://www.ray086.com/"
DOTA2_GAME_ID = 151


class RayBetClient:
    def __init__(
        self, *, timeout: float = 20.0, client: requests.Session | None = None
    ) -> None:
        self.timeout = timeout
        self._owns_client = client is None
        self.client = client or requests.Session(impersonate="chrome120")
        self.client.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.ray086.com",
                "Referer": SITE_URL,
            }
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "RayBetClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.get(f"{BASE_URL}/{path.lstrip('/')}", params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise RuntimeError(f"RayBet returned code={payload.get('code')} for {path}")
        return payload

    def games(self) -> list[dict[str, Any]]:
        return list(self._get("/game").get("result") or [])

    def match_page(self, match_type: int, page: int = 1) -> list[dict[str, Any]]:
        result = self._get("/match", {"match_type": match_type, "page": page}).get("result")
        return list(result or [])

    def matches(self, *, match_type: int, max_pages: int = 10) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            page_rows = self.match_page(match_type, page)
            if not page_rows:
                break
            for row in page_rows:
                if int(row.get("game_id") or 0) != DOTA2_GAME_ID:
                    continue
                match_id = str(row.get("id"))
                if match_id not in seen:
                    rows.append(row)
                    seen.add(match_id)
        return rows

    def live_matches(self, *, max_pages: int = 10) -> list[dict[str, Any]]:
        combined: dict[str, dict[str, Any]] = {}
        for match_type in (1, 2):
            for row in self.matches(match_type=match_type, max_pages=max_pages):
                combined[str(row.get("id"))] = row
        return sorted(combined.values(), key=lambda row: str(row.get("start_time") or ""))

    def match_odds(self, match_id: int | str) -> dict[str, Any]:
        payload = self._get("/odds", {"match_id": str(match_id)})
        if not isinstance(payload.get("result"), dict):
            raise RuntimeError(f"RayBet odds missing result for match_id={match_id}")
        return payload
