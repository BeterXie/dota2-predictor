from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from live_betting.stratz_rosh_client import (
    StratzRoshClient,
    resolve_stratz_api_token,
)


def test_legacy_batch_preserves_exact_request_and_response_bytes() -> None:
    calls: list[bytes] = []

    def post(_url: str, *, data: bytes, **_kwargs):
        calls.append(data)
        return SimpleNamespace(
            status_code=200,
            content=b'{"data":{}}',
            headers={},
        )

    client = StratzRoshClient(
        token="token",
        post=post,
        clock=lambda: datetime(2026, 8, 7, 10, tzinfo=timezone.utc),
    )
    result = client.fetch_legacy_lineup_batch(
        (1, 2, 3, 4, 5),
        (6, 7, 8, 9, 10),
        statistics_cutoff=datetime(2026, 8, 7, 9, tzinfo=timezone.utc),
    )

    assert set(result.request_bodies) == {
        "heroes_meta_positions",
        "hero_stats_by_time_bracket",
        "synergy",
    }
    assert tuple(result.request_bodies.values()) == tuple(calls)
    assert set(result.response_bodies.values()) == {b'{"data":{}}'}


def test_only_primary_stratz_token_is_runtime_authority() -> None:
    assert resolve_stratz_api_token({"STRATZ_API_TOKEN": " primary "}) == "primary"
    assert resolve_stratz_api_token({"STRATZ_TOKEN": "legacy"}) is None
