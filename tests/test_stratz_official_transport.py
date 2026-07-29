from __future__ import annotations

import hashlib
import json
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from live_betting.stratz_rosh_client import (
    OfficialRoshBatch,
    STRATZ_GRAPHQL_ENDPOINT,
    StratzRoshClient,
    StratzRoshError,
)
from prematch.stratz_official_profile import V2_PROFILE_ID, build_official_request_plan


STARTED_AT = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
COLLECTED_AT = datetime(2026, 7, 28, 12, 0, 5, tzinfo=timezone.utc)
GOLDEN_REQUEST_SHA256 = (
    "280f11b38a29c87751c4f36c74d95d4b89bf087f00b766331fbbe379f551971f"
)


class Response:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


def plan() -> Any:
    return build_official_request_plan(
        {
            "mode": "historical_match",
            "match_id": 8904419709,
            "date_time": 1784485548,
            "bracket_ids": ["IMMORTAL"],
        },
        request_started_at=STARTED_AT,
    )


def response_bytes(count: int = 6) -> bytes:
    return json.dumps(
        [{"data": {"slot": index}} for index in range(count)],
        separators=(",", ":"),
    ).encode("utf-8")


def golden_request_body() -> bytes:
    return (
        Path(__file__).parent
        / "fixtures"
        / "stratz_official_rosh"
        / "8904419709"
        / "requests.json"
    ).read_bytes()


def test_exact_batch_body_order_and_one_post() -> None:
    request_plan = plan()
    raw_response = (
        '[ {"data":{"slot":0,"label":"六项"}},'
        '{"data":{"slot":1}},{"data":{"slot":2}},'
        '{"data":{"slot":3}},{"data":{"slot":4}},'
        '{"data":{"slot":5}} ]\n'
    ).encode("utf-8")
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(url: str, **kwargs: Any) -> Response:
        calls.append((url, kwargs))
        return Response(raw_response)

    result = StratzRoshClient(
        "private-token",
        post=post,
        clock=lambda: COLLECTED_AT,
    ).fetch_official_batch(request_plan)

    assert len(calls) == 1
    url, call = calls[0]
    assert url == STRATZ_GRAPHQL_ENDPOINT
    assert call["timeout"] == 30.0
    assert call["impersonate"] == "chrome120"
    expected_body = golden_request_body()
    assert call["data"] == expected_body == result.request_body
    assert [list(item) for item in json.loads(call["data"])] == [
        ["operationName", "variables", "query"]
    ] * 6
    assert [item["operationName"] for item in json.loads(call["data"])] == [
        operation.operation_name for operation in request_plan.operations
    ]
    assert [item["query"] for item in json.loads(call["data"])] == [
        operation.query for operation in request_plan.operations
    ]
    assert result.response_body == raw_response
    assert len(result.responses) == 6
    assert result.responses[0]["data"]["label"] == "六项"
    assert result.collected_at == COLLECTED_AT
    assert result.diagnostics["attempt_count"] == 1
    assert result.diagnostics["retry_delays_seconds"] == ()


def test_active_v2_request_body_matches_frozen_capture() -> None:
    expected_body = golden_request_body()
    posted_bodies: list[bytes] = []
    request_plan = plan()

    def post(_url: str, **kwargs: Any) -> Response:
        posted_bodies.append(kwargs["data"])
        return Response(response_bytes())

    assert request_plan.profile.rosh_profile_id == V2_PROFILE_ID
    result = StratzRoshClient(
        "private-token",
        post=post,
        clock=lambda: COLLECTED_AT,
    ).fetch_official_batch(request_plan)

    assert hashlib.sha256(expected_body).hexdigest() == GOLDEN_REQUEST_SHA256
    assert hashlib.sha256(result.request_body).hexdigest() == GOLDEN_REQUEST_SHA256
    assert result.request_body == expected_body
    assert posted_bodies == [expected_body]


@pytest.mark.parametrize("drift", ["canonical", "profile"])
def test_request_plan_drift_is_rejected_before_post(drift: str) -> None:
    request_plan = plan()
    if drift == "canonical":
        first = replace(request_plan.operations[0], query="query drift")
        request_plan = replace(
            request_plan,
            operations=(first, *request_plan.operations[1:]),
        )
    else:
        request_plan = replace(
            request_plan,
            profile=replace(request_plan.profile, formula_version="profile-drift"),
        )
    calls = 0

    def post(_url: str, **_kwargs: Any) -> Response:
        nonlocal calls
        calls += 1
        return Response(response_bytes())

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient("private-token", post=post).fetch_official_batch(
            request_plan
        )

    assert calls == 0
    assert caught.value.category == "profile_drift"


def test_http_200_graphql_errors_fail_the_whole_batch_without_body_leak() -> None:
    secret = "sensitive-upstream-body"
    payload = [{"data": {"slot": index}} for index in range(6)]
    payload[2] = {
        "errors": [
            {
                "message": secret,
                "extensions": {"code": "RATE_LIMITED"},
            }
        ]
    }

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient(
            "private-token",
            post=lambda *_args, **_kwargs: Response(
                json.dumps(payload).encode("utf-8")
            ),
        ).fetch_official_batch(plan())

    assert caught.value.category == "graphql_rate_limited"
    assert caught.value.retryable is True
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        (b"not-json", "invalid_json"),
        (b'{"data":{}}', "invalid_response"),
        (response_bytes(5), "invalid_response"),
        (response_bytes(7), "invalid_response"),
        (
            json.dumps(
                [{"data": {}} for _ in range(5)] + [{}]
            ).encode("utf-8"),
            "invalid_response",
        ),
    ],
    ids=["invalid-json", "non-array", "missing-item", "extra-item", "missing-data"],
)
def test_malformed_or_incomplete_batch_fails_closed(
    raw: bytes,
    category: str,
) -> None:
    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient(
            "private-token",
            post=lambda *_args, **_kwargs: Response(raw),
        ).fetch_official_batch(plan())

    assert caught.value.category == category


@pytest.mark.parametrize(
    ("constant", "operation_index"),
    [("NaN", 0), ("Infinity", 2), ("-Infinity", 5)],
)
def test_non_finite_json_constant_in_unconsumed_data_is_invalid_json(
    constant: str,
    operation_index: int,
) -> None:
    items = [f'{{"data":{{"slot":{index}}}}}' for index in range(6)]
    items[operation_index] = (
        f'{{"data":{{"slot":{operation_index},'
        f'"transportOnly":{constant},"secret":"must-not-escape"}}}}'
    )
    raw = f'[{",".join(items)}]'.encode("utf-8")

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient(
            "private-token",
            post=lambda *_args, **_kwargs: Response(raw),
        ).fetch_official_batch(plan())

    assert caught.value.category == "invalid_json"
    assert constant not in str(caught.value)
    assert "must-not-escape" not in str(caught.value)


def test_http_429_honors_retry_after_then_succeeds() -> None:
    responses = [
        Response(b"ignored", status_code=429, headers={"Retry-After": "7"}),
        Response(response_bytes()),
    ]
    sleeps: list[float] = []
    calls = 0

    def post(_url: str, **_kwargs: Any) -> Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    result = StratzRoshClient(
        "private-token",
        post=post,
        sleeper=sleeps.append,
        clock=lambda: COLLECTED_AT,
    ).fetch_official_batch(plan())

    assert calls == 2
    assert sleeps == [7.0]
    assert result.diagnostics["attempt_count"] == 2
    assert result.diagnostics["retry_delays_seconds"] == (7.0,)


def test_http_5xx_uses_capped_exponential_backoff() -> None:
    responses = [
        Response(b"ignored", status_code=500),
        Response(b"ignored", status_code=502),
        Response(b"ignored", status_code=503),
        Response(response_bytes()),
    ]
    sleeps: list[float] = []
    calls = 0

    def post(_url: str, **_kwargs: Any) -> Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    result = StratzRoshClient(
        "private-token",
        post=post,
        sleeper=sleeps.append,
        clock=lambda: COLLECTED_AT,
        official_max_attempts=4,
        official_backoff_base_seconds=2,
        official_backoff_cap_seconds=3,
    ).fetch_official_batch(plan())

    assert calls == 4
    assert sleeps == [2.0, 3.0, 3.0]
    assert result.diagnostics["retry_delays_seconds"] == (2.0, 3.0, 3.0)


def test_retry_termination_is_bounded() -> None:
    calls = 0
    sleeps: list[float] = []

    def post(_url: str, **_kwargs: Any) -> Response:
        nonlocal calls
        calls += 1
        return Response(b"upstream-body-must-not-escape", status_code=503)

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient(
            "private-token",
            post=post,
            sleeper=sleeps.append,
            official_max_attempts=3,
        ).fetch_official_batch(plan())

    assert calls == 3
    assert sleeps == [1.0, 2.0]
    assert caught.value.category == "http_5xx"
    assert "upstream-body-must-not-escape" not in str(caught.value)


@pytest.mark.parametrize(
    "endpoint",
    ["https://example.invalid/graphql", "http://api.stratz.com/graphql"],
    ids=["endpoint-drift", "http-scheme"],
)
def test_endpoint_drift_is_rejected_before_post(endpoint: str) -> None:
    calls = 0

    def post(_url: str, **_kwargs: Any) -> Response:
        nonlocal calls
        calls += 1
        return Response(response_bytes())

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient(
            "private-token",
            endpoint=endpoint,
            post=post,
        ).fetch_official_batch(plan())

    assert calls == 0
    assert caught.value.category == "profile_drift"


def test_secret_is_absent_from_result_repr_and_sanitized_errors() -> None:
    secret = "do-not-expose-private-token"
    result = StratzRoshClient(
        secret,
        post=lambda *_args, **_kwargs: Response(response_bytes()),
        clock=lambda: COLLECTED_AT,
    ).fetch_official_batch(plan())

    assert secret not in repr(result)
    assert {field.name for field in fields(OfficialRoshBatch)} == {
        "request_body",
        "response_body",
        "responses",
        "collected_at",
        "diagnostics",
    }
    assert "headers" not in result.diagnostics
    assert "Authorization" not in repr(result)

    def failed_post(_url: str, **_kwargs: Any) -> Response:
        raise RuntimeError(secret)

    with pytest.raises(StratzRoshError) as caught:
        StratzRoshClient(secret, post=failed_post).fetch_official_batch(plan())

    assert secret not in str(caught.value)
