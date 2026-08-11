from __future__ import annotations

import asyncio

import pytest

from fetch.stratz_detail import (
    STRATZ_GRAPHQL_ENDPOINT,
    StratzDetailError,
    StratzMatchDetailClient,
    stratz_player_positions,
)


class _Response:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def json(self) -> object:
        return self.payload


class _Client:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def _payload(match_id: int = 9001) -> dict[str, object]:
    return {
        "data": {
            "match": {
                "id": match_id,
                "players": [
                    {
                        "steamAccountId": 101,
                        "heroId": 23,
                        "position": "POSITION_1",
                    },
                    {
                        "steamAccountId": 202,
                        "heroId": 45,
                        "position": 5,
                    },
                ],
            }
        }
    }


def test_stratz_detail_fetches_only_optional_match_enrichment() -> None:
    transport = _Client(_payload())
    client = StratzMatchDetailClient("secret", client=transport)

    result = asyncio.run(client.get_match(9001))

    assert result == _payload()
    assert transport.calls[0][0] == STRATZ_GRAPHQL_ENDPOINT
    request = transport.calls[0][1]["json"]
    assert request["operationName"] == "MatchDetailEnrichment"
    assert request["variables"] == {"matchId": 9001}
    assert stratz_player_positions(result, expected_match_id=9001) == {
        (101, 23): 1,
        (202, 45): 5,
    }


def test_stratz_detail_rejects_a_different_match_identity() -> None:
    client = StratzMatchDetailClient("secret", client=_Client(_payload(9002)))

    with pytest.raises(StratzDetailError, match="identity"):
        asyncio.run(client.get_match(9001))
