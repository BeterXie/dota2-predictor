"""PandaScore Dota 2 fixtures adapter.

Live WebSocket URLs and schemas are plan-specific. This adapter deliberately
enables REST fixtures first and refuses to fabricate live events until a Live
API endpoint has been provisioned for the configured account.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import AsyncIterator, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import websockets

from ..models import LiveEvent, LiveFrame, ProviderMatch


API_URL = "https://api.pandascore.co"


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PandaScoreProvider:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not token:
            raise ValueError("PANDASCORE_TOKEN is required")
        self._owns_client = client is None
        self.token = token
        self.client = client or httpx.AsyncClient(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=20,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def _match(row: dict[str, Any]) -> ProviderMatch:
        opponents = [item.get("opponent") or {} for item in row.get("opponents") or []]
        league = row.get("league") or {}
        return ProviderMatch(
            provider="pandascore",
            provider_match_id=str(row.get("id")),
            tournament=str(league.get("name") or (row.get("tournament") or {}).get("name") or ""),
            team_one=str(opponents[0].get("name") if opponents else ""),
            team_two=str(opponents[1].get("name") if len(opponents) > 1 else ""),
            scheduled_at=_datetime(row.get("begin_at") or row.get("scheduled_at")),
            best_of=int(row.get("number_of_games") or 0) or None,
            status=str(row.get("status") or ""),
            raw=row,
        )

    async def _matches(self, endpoint: str) -> list[ProviderMatch]:
        response = await self.client.get(endpoint, params={"per_page": 100})
        response.raise_for_status()
        return [self._match(row) for row in response.json()]

    async def list_live_matches(self) -> list[ProviderMatch]:
        return await self._matches("/dota2/matches/running")

    async def list_upcoming_matches(self) -> list[ProviderMatch]:
        return await self._matches("/dota2/matches/upcoming")

    async def list_live_endpoints(self) -> list[dict[str, Any]]:
        response = await self.client.get("/lives")
        response.raise_for_status()
        return list(response.json() or [])

    def _authenticated_url(self, url: str) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query["token"] = self.token
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def _endpoint(self, provider_match_id: str, endpoint_type: str) -> str | None:
        for row in await self.list_live_endpoints():
            match = row.get("match") or {}
            if str(match.get("id") or row.get("match_id")) != str(provider_match_id):
                continue
            for endpoint in row.get("endpoints") or []:
                if endpoint.get("type") == endpoint_type and endpoint.get("open"):
                    return str(endpoint.get("url"))
        return None

    @staticmethod
    def _frame(provider_match_id: str, message: dict[str, Any]) -> LiveFrame:
        payload = message.get("payload") or {}
        game = payload.get("game") or payload
        teams = game.get("teams") or payload.get("teams") or []
        one = teams[0] if teams else {}
        two = teams[1] if len(teams) > 1 else {}
        now = datetime.now(timezone.utc)
        sequence = message.get("sequence") or payload.get("sequence") or payload.get("id")

        def first(mapping: dict[str, Any], *keys: str) -> Any:
            for key in keys:
                if mapping.get(key) is not None:
                    return mapping[key]
            return None

        return LiveFrame(
            provider="pandascore",
            provider_match_id=provider_match_id,
            provider_game_id=str(game.get("id") or payload.get("game_id") or "") or None,
            sequence=str(sequence or "") or None,
            source_at=_datetime(message.get("timestamp") or payload.get("timestamp")),
            received_at=now,
            game_time=first(game, "time", "game_time") if game else payload.get("game_time"),
            team_one_kills=first(one, "kills", "score"),
            team_two_kills=first(two, "kills", "score"),
            team_one_gold=one.get("gold"),
            team_two_gold=two.get("gold"),
            state=str(game.get("status") or payload.get("status") or "") or None,
            raw=message,
        )

    async def get_match_state(self, provider_match_id: str) -> LiveFrame | None:
        endpoint = await self._endpoint(provider_match_id, "frames")
        if not endpoint:
            return None
        async with websockets.connect(self._authenticated_url(endpoint)) as socket:
            async for raw in socket:
                message = json.loads(raw)
                if message.get("type") != "hello":
                    return self._frame(provider_match_id, message)
        return None

    async def stream_frames(self, provider_match_id: str) -> AsyncIterator[LiveFrame]:
        endpoint = await self._endpoint(provider_match_id, "frames")
        if not endpoint:
            raise RuntimeError(f"no open PandaScore frames endpoint for match {provider_match_id}")
        async with websockets.connect(self._authenticated_url(endpoint)) as socket:
            async for raw in socket:
                message = json.loads(raw)
                if message.get("type") != "hello":
                    yield self._frame(provider_match_id, message)

    async def stream_events(
        self, provider_match_id: str, cursor: str | None = None
    ) -> AsyncIterator[LiveEvent | LiveFrame]:
        del cursor  # PandaScore does not document Dota 2 event recovery.
        endpoint = await self._endpoint(provider_match_id, "events")
        if not endpoint:
            raise RuntimeError(f"no open PandaScore events endpoint for match {provider_match_id}")
        async with websockets.connect(self._authenticated_url(endpoint)) as socket:
            async for raw in socket:
                message = json.loads(raw)
                if message.get("type") == "hello":
                    continue
                payload = message.get("payload") or {}
                canonical = json.dumps(message, sort_keys=True, separators=(",", ":"))
                event_id = str(payload.get("id") or message.get("id") or
                               hashlib.sha256(canonical.encode()).hexdigest()[:32])
                yield LiveEvent(
                    provider="pandascore",
                    provider_event_id=event_id,
                    provider_match_id=provider_match_id,
                    provider_game_id=str(payload.get("game_id") or
                                         (payload.get("game") or {}).get("id") or "") or None,
                    event_type=str(message.get("type") or payload.get("type") or "unknown"),
                    source_at=_datetime(message.get("timestamp") or payload.get("timestamp")),
                    received_at=datetime.now(timezone.utc),
                    game_time=payload.get("game_time") or payload.get("time"),
                    team=str(payload.get("team_id") or payload.get("team") or "") or None,
                    player=str(payload.get("player_id") or payload.get("player") or "") or None,
                    raw=message,
                )

    async def get_final_result(self, provider_match_id: str) -> dict | None:
        response = await self.client.get(f"/dota2/matches/{provider_match_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
