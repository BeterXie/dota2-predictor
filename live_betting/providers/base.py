"""Live provider protocol."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from ..models import LiveEvent, LiveFrame, ProviderMatch


class LiveDataProvider(Protocol):
    async def list_live_matches(self) -> list[ProviderMatch]: ...

    async def get_match_state(self, provider_match_id: str) -> LiveFrame | None: ...

    async def stream_events(
        self, provider_match_id: str, cursor: str | None = None
    ) -> AsyncIterator[LiveEvent | LiveFrame]: ...

    async def get_final_result(self, provider_match_id: str) -> dict | None: ...
