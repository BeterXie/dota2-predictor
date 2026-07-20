from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from live_betting.direct_response_audit import (
    PAYLOAD_LIMIT_FAILURE_TYPE,
    DirectResponseContext,
    DirectResponseDecision,
    DirectResponsePayloadShapeError,
    DirectResponseRequestIdentityError,
    audited_direct_request,
)
from live_betting.monitor import _collect_odds_response, _fetch_match_list
from live_betting.odds_response_authority import (
    MAX_RESPONSE_ARTIFACT_BYTES,
    ResponseArtifactLimitError,
)
from live_betting.postmatch_monitor import _refresh_raybet_final
from live_betting.raybet import BASE_URL, RayBetClient, RayBetHTTPResponse
from live_betting.sanitize import RayBetPayloadSanitizationError
from live_betting.storage import LiveBettingStore
from scripts.watch_raybet_stream import match_source


NOW = datetime(2026, 7, 18, 1, 2, 3, tzinfo=timezone.utc)
STREAM_URL = "https://play.ehome.gg/live.m3u8"


class EmptySession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class HTTPResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class InvalidJSONHTTPResponse(HTTPResponse):
    def json(self) -> object:
        raise ValueError("invalid provider JSON with SECRET_DETAIL")


class Session(EmptySession):
    def __init__(self, responses: list[HTTPResponse]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def get(
        self,
        endpoint: str,
        *,
        params: dict[str, object] | None,
        timeout: float,
    ) -> HTTPResponse:
        del timeout
        self.calls.append((endpoint, params))
        return self.responses.pop(0)


def http_response(
    payload: dict[str, object],
    *,
    received_at: datetime,
    request_identity: str,
    status_code: int = 200,
) -> RayBetHTTPResponse:
    code = payload.get("code")
    return RayBetHTTPResponse(
        payload=payload,
        endpoint=f"{BASE_URL}/match",
        request_identity=request_identity,
        received_at=received_at,
        http_status=status_code,
        provider_code=code if type(code) is int else None,
    )


def audit_rows(store: LiveBettingStore) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in store.connection.execute(
            """SELECT audit_key, response_kind, observed_at, endpoint,
                      request_identity, http_status, provider_code,
                      request_metadata_json, payload_kind, sanitized,
                      disposition, reason, artifact_hash
                 FROM direct_response_audit ORDER BY observed_at, audit_key"""
        )
    ]


def request_metadata(row: dict[str, object]) -> dict[str, object]:
    metadata = json.loads(str(row["request_metadata_json"]))
    receipt_id = metadata.pop("receipt_id")
    assert isinstance(receipt_id, str) and len(receipt_id) == 32
    return metadata


def test_match_lists_archive_each_actual_http_page_with_receipt_metadata(
    tmp_path: Path,
) -> None:
    client = RayBetClient(client=EmptySession())
    responses = {
        (1, 1): http_response(
            {
                "code": 200,
                "result": [
                    {"id": "1001", "game_id": 151, "start_time": 2},
                    {"id": "ignored", "game_id": 1},
                ],
            },
            received_at=NOW,
            request_identity=f"{BASE_URL}/match?match_type=1&page=1",
        ),
        (1, 2): http_response(
            {"code": 200, "result": []},
            received_at=NOW + timedelta(seconds=1),
            request_identity=f"{BASE_URL}/match?match_type=1&page=2",
        ),
        (2, 1): http_response(
            {"code": 200, "result": []},
            received_at=NOW + timedelta(seconds=2),
            request_identity=f"{BASE_URL}/match?match_type=2&page=1",
        ),
    }
    with (
        LiveBettingStore(tmp_path / "live.db") as store,
        patch.object(
            client,
            "match_page_response",
            side_effect=lambda match_type, page: responses[(match_type, page)],
        ),
    ):
        store.init_schema()
        assert _fetch_match_list(
            store, client, response_kind="live_match_list"
        ) == [{"id": "1001", "game_id": 151, "start_time": 2}]
        rows = audit_rows(store)
        assert [row["observed_at"] for row in rows] == [
            (NOW + timedelta(seconds=offset)).isoformat() for offset in range(3)
        ]
        assert [request_metadata(row) for row in rows] == [
            {"match_type": 1, "page": 1},
            {"match_type": 1, "page": 2},
            {"match_type": 2, "page": 1},
        ]
        assert all(row["endpoint"] == f"{BASE_URL}/match" for row in rows)
        assert all(row["http_status"] == 200 for row in rows)
        assert all(row["provider_code"] == 200 for row in rows)
        assert all(row["payload_kind"] == "provider_response" for row in rows)
        assert all(row["sanitized"] == 1 for row in rows)
        assert all(row["reason"] == "match_page_observed" for row in rows)


def test_second_page_failure_preserves_first_page_and_has_no_success_payload(
    tmp_path: Path,
) -> None:
    client = RayBetClient(client=EmptySession())
    first = http_response(
        {"code": 200, "result": [{"id": "1001", "game_id": 151}]},
        received_at=NOW,
        request_identity=f"{BASE_URL}/match?match_type=1&page=1",
    )

    def fetch(match_type: int, page: int) -> RayBetHTTPResponse:
        assert match_type == 1
        if page == 1:
            return first
        raise TimeoutError("upstream SECRET_QUERY_TOKEN")

    with (
        LiveBettingStore(tmp_path / "live.db") as store,
        patch.object(client, "match_page_response", side_effect=fetch),
    ):
        store.init_schema()
        with pytest.raises(TimeoutError):
            _fetch_match_list(store, client, response_kind="live_match_list")
        rows = audit_rows(store)
        assert len(rows) == 2
        by_kind = {str(row["payload_kind"]): row for row in rows}
        first_row = by_kind["provider_response"]
        failure_row = by_kind["request_failure"]
        assert first_row["observed_at"] == NOW.isoformat()
        assert request_metadata(first_row) == {
            "match_type": 1,
            "page": 1,
        }
        assert request_metadata(failure_row) == {
            "match_type": 1,
            "page": 2,
        }
        failure = store.direct_response_payload(str(failure_row["audit_key"]))
        assert "result" not in failure
        assert failure["failure"] == {"error_type": "TimeoutError"}
        assert "SECRET_QUERY_TOKEN" not in json.dumps(failure)


def test_match_page_receipt_identity_mismatch_is_archived_but_rejected(
    tmp_path: Path,
) -> None:
    client = RayBetClient(client=EmptySession())
    response = http_response(
        {"code": 200, "result": [{"id": "1001", "game_id": 151}]},
        received_at=NOW,
        request_identity=f"{BASE_URL}/match?match_type=1&page=999",
    )
    with (
        LiveBettingStore(tmp_path / "live.db") as store,
        patch.object(client, "match_page_response", return_value=response),
    ):
        store.init_schema()
        with pytest.raises(DirectResponseRequestIdentityError):
            _fetch_match_list(store, client, response_kind="live_match_list")

        row = audit_rows(store)[0]
        assert row["request_identity"].endswith("match_type=1&page=999")
        assert row["disposition"] == "rejected"
        assert row["reason"] == "request_identity_mismatch"
        assert store.direct_response_payload(str(row["audit_key"])) == response.payload


def test_provider_code_failure_archives_the_actual_rejected_response(
    tmp_path: Path,
) -> None:
    client = RayBetClient(
        client=Session(
            [
                HTTPResponse(
                    {
                        "code": 503,
                        "message": "retry?token=SECRET_PROVIDER_TOKEN",
                        "result": [],
                    }
                )
            ]
        )
    )
    with LiveBettingStore(tmp_path / "live.db") as store:
        store.init_schema()
        with pytest.raises(RuntimeError, match="code=503"):
            _fetch_match_list(store, client, response_kind="live_match_list")
        row = audit_rows(store)[0]
        assert row["payload_kind"] == "provider_response"
        assert row["disposition"] == "rejected"
        assert row["provider_code"] == 503
        assert row["http_status"] == 200
        payload = store.direct_response_payload(str(row["audit_key"]))
        assert payload["code"] == 503
        assert "SECRET_PROVIDER_TOKEN" not in json.dumps(payload)


def test_invalid_json_failure_keeps_exact_http_receipt_metadata(
    tmp_path: Path,
) -> None:
    client = RayBetClient(client=Session([InvalidJSONHTTPResponse(None)]))
    with LiveBettingStore(tmp_path / "live.db") as store:
        store.init_schema()
        with pytest.raises(ValueError, match="invalid provider JSON") as raised:
            _fetch_match_list(store, client, response_kind="live_match_list")
        row = audit_rows(store)[0]
        assert row["observed_at"] == raised.value.raybet_received_at.isoformat()
        assert row["request_identity"] == (
            f"{BASE_URL}/match?match_type=1&page=1"
        )
        assert row["http_status"] == 200
        assert row["provider_code"] is None
        assert row["payload_kind"] == "request_failure"
        assert request_metadata(row) == {"match_type": 1, "page": 1}
        payload = store.direct_response_payload(str(row["audit_key"]))
        assert payload["failure"] == {"error_type": "ValueError"}
        assert "SECRET_DETAIL" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("provider_payload", "sanitized_payload", "secret"),
    (
        (
            [
                {
                    "token": "LIST_SECRET",
                    "live_url": (
                        "https://qplay.ehome.gg/live.m3u8?auth_key=LIST_SECRET"
                    ),
                }
            ],
            [{"live_url": "https://qplay.ehome.gg/live.m3u8"}],
            "LIST_SECRET",
        ),
        (
            "https://qplay.ehome.gg/live.m3u8?auth_key=SCALAR_SECRET",
            "https://qplay.ehome.gg/live.m3u8",
            "SCALAR_SECRET",
        ),
    ),
)
def test_non_object_http_json_is_replayable_sanitized_provider_evidence(
    tmp_path: Path,
    provider_payload: object,
    sanitized_payload: object,
    secret: str,
) -> None:
    client = RayBetClient(client=Session([HTTPResponse(provider_payload)]))
    with LiveBettingStore(tmp_path / "live.db") as store:
        store.init_schema()
        with pytest.raises(RuntimeError, match="non-object response"):
            _fetch_match_list(store, client, response_kind="live_match_list")

        row = audit_rows(store)[0]
        assert row["payload_kind"] == "provider_response"
        assert row["disposition"] == "rejected"
        assert row["reason"] == "validation_failed"
        assert store.direct_response_payload(str(row["audit_key"])) == sanitized_payload
        artifact = store.connection.execute(
            "SELECT storage_path FROM odds_raw_artifacts WHERE artifact_hash=?",
            (str(row["artifact_hash"]),),
        ).fetchone()
        assert artifact is not None
        compressed_path = store.raw_archive_root / str(artifact["storage_path"])
        canonical = gzip.decompress(compressed_path.read_bytes())
        assert canonical == json.dumps(
            sanitized_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert hashlib.sha256(canonical).hexdigest() == row["artifact_hash"]
        assert secret.encode() not in canonical
    assert secret.encode() not in (tmp_path / "live.db").read_bytes()


def test_completed_list_archives_type_four_page_receipt(tmp_path: Path) -> None:
    client = RayBetClient(client=EmptySession())
    response = http_response(
        {"code": 200, "result": []},
        received_at=NOW,
        request_identity=f"{BASE_URL}/match?match_type=4&page=1",
    )
    with (
        LiveBettingStore(tmp_path / "live.db") as store,
        patch.object(client, "match_page_response", return_value=response) as fetch,
    ):
        store.init_schema()
        assert _fetch_match_list(
            store, client, response_kind="completed_match_list"
        ) == []
        fetch.assert_called_once_with(4, 1)
        row = audit_rows(store)[0]
        assert row["response_kind"] == "completed_match_list"
        assert request_metadata(row) == {"match_type": 4, "page": 1}


def test_live_list_preserves_type_two_override_and_per_type_dedup(
    tmp_path: Path,
) -> None:
    client = RayBetClient(client=EmptySession())
    payloads = {
        (1, 1): [{"id": "1001", "game_id": 151, "status": 1}],
        (1, 2): [],
        (2, 1): [{"id": "1001", "game_id": 151, "status": 2}],
        (2, 2): [],
    }

    def fetch(match_type: int, page: int) -> RayBetHTTPResponse:
        return http_response(
            {"code": 200, "result": payloads[(match_type, page)]},
            received_at=NOW + timedelta(seconds=len(audit_calls)),
            request_identity=(
                f"{BASE_URL}/match?match_type={match_type}&page={page}"
            ),
        )

    audit_calls: list[tuple[int, int]] = []

    def tracked_fetch(match_type: int, page: int) -> RayBetHTTPResponse:
        audit_calls.append((match_type, page))
        return fetch(match_type, page)

    with (
        LiveBettingStore(tmp_path / "live.db") as store,
        patch.object(client, "match_page_response", side_effect=tracked_fetch),
    ):
        store.init_schema()
        assert _fetch_match_list(
            store, client, response_kind="live_match_list"
        ) == [{"id": "1001", "game_id": 151, "status": 2}]
        assert audit_calls == [(1, 1), (1, 2), (2, 1), (2, 2)]


def test_repeated_payload_reuses_one_gzip_but_retains_each_receipt(
    tmp_path: Path,
) -> None:
    payload = {"code": 200, "result": []}
    endpoint = f"{BASE_URL}/match"

    def process(
        context: DirectResponseContext,
    ) -> DirectResponseDecision[None]:
        assert context.sanitized_payload == payload
        return DirectResponseDecision(
            None, disposition="audit_only", reason="match_page_observed"
        )

    with LiveBettingStore(tmp_path / "live.db") as store:
        store.init_schema()
        for _ in range(2):
            audited_direct_request(
                store,
                fetch=lambda: RayBetHTTPResponse(
                    payload,
                    endpoint,
                    f"{endpoint}?match_type=1&page=1",
                    NOW,
                    200,
                    200,
                ),
                process=process,
                response_kind="live_match_list",
                claimed_raybet_match_id=None,
                endpoint=endpoint,
                request_identity=f"{endpoint}?match_type=1&page=1",
                request_metadata={"match_type": 1, "page": 1},
            )
        rows = audit_rows(store)
        assert len(rows) == 2
        assert rows[0]["artifact_hash"] == rows[1]["artifact_hash"]
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM odds_raw_artifacts"
            ).fetchone()[0]
            == 1
        )
        assert len(list(store.raw_archive_root.rglob("*.json.gz"))) == 1


@pytest.mark.parametrize(
    ("actual_endpoint", "actual_identity"),
    (
        (
            f"{BASE_URL}/odds",
            f"{BASE_URL}/match?match_type=1&page=1",
        ),
        (
            f"{BASE_URL}/match",
            f"{BASE_URL}/match?match_type=1&page=2",
        ),
    ),
)
def test_exact_receipt_endpoint_and_request_identity_are_both_required(
    tmp_path: Path,
    actual_endpoint: str,
    actual_identity: str,
) -> None:
    endpoint = f"{BASE_URL}/match"
    expected_identity = f"{endpoint}?match_type=1&page=1"
    process_called = False

    def process(
        _context: DirectResponseContext,
    ) -> DirectResponseDecision[None]:
        nonlocal process_called
        process_called = True
        return DirectResponseDecision(
            None, disposition="audit_only", reason="unexpected"
        )

    with LiveBettingStore(tmp_path / "live.db") as store:
        store.init_schema()
        with pytest.raises(DirectResponseRequestIdentityError):
            audited_direct_request(
                store,
                fetch=lambda: RayBetHTTPResponse(
                    {"code": 200, "result": []},
                    actual_endpoint,
                    actual_identity,
                    NOW,
                    200,
                    200,
                ),
                process=process,
                response_kind="live_match_list",
                claimed_raybet_match_id=None,
                endpoint=endpoint,
                request_identity=expected_identity,
            )
        assert not process_called
        assert audit_rows(store)[0]["reason"] == "request_identity_mismatch"


def test_equivalent_query_order_is_canonicalized_before_identity_comparison(
    tmp_path: Path,
) -> None:
    endpoint = f"{BASE_URL}/match"
    with LiveBettingStore(tmp_path / "live.db") as store:
        store.init_schema()
        result = audited_direct_request(
            store,
            fetch=lambda: RayBetHTTPResponse(
                {"code": 200, "result": []},
                endpoint,
                f"{endpoint}?page=1&match_type=1",
                NOW,
                200,
                200,
            ),
            process=lambda _context: DirectResponseDecision(
                "ok", disposition="audit_only", reason="matched"
            ),
            response_kind="live_match_list",
            claimed_raybet_match_id=None,
            endpoint=endpoint,
            request_identity=f"{endpoint}?match_type=1&page=1",
        )
        assert result == "ok"
        assert audit_rows(store)[0]["reason"] == "matched"


def test_transport_failure_with_wrong_request_identity_is_marked_as_mismatch(
    tmp_path: Path,
) -> None:
    endpoint = f"{BASE_URL}/match"
    error = TimeoutError("upstream timeout")
    error.raybet_endpoint = endpoint
    error.raybet_request_identity = f"{endpoint}?match_type=1&page=2"
    error.raybet_received_at = NOW
    error.raybet_http_status = None

    def fail() -> dict[str, object]:
        raise error

    with LiveBettingStore(tmp_path / "live.db") as store:
        store.init_schema()
        with pytest.raises(TimeoutError):
            audited_direct_request(
                store,
                fetch=fail,
                process=lambda _context: DirectResponseDecision(
                    None, disposition="audit_only", reason="unexpected"
                ),
                response_kind="live_match_list",
                claimed_raybet_match_id=None,
                endpoint=endpoint,
                request_identity=f"{endpoint}?match_type=1&page=1",
            )
        row = audit_rows(store)[0]
        assert row["payload_kind"] == "request_failure"
        assert row["reason"] == "request_identity_mismatch"


def _payload_limit_case(kind: str, *, outside: bool) -> dict[str, object]:
    if kind == "nodes":
        # root object + list + members: 99,998 members is exactly 100,000 nodes.
        member_count = 99_999 if outside else 99_998
        payload: dict[str, object] = {"items": [None] * member_count}
        if outside:
            payload["token"] = "NODE_LIMIT_SECRET"
        return payload
    if kind == "depth":
        value: object = "DEPTH_LIMIT_SECRET" if outside else "ok"
        for _ in range(65 if outside else 64):
            value = {"child": value}
        assert isinstance(value, dict)
        return value
    if kind == "bytes":
        empty_size = len(b'{"blob":""}')
        target_size = MAX_RESPONSE_ARTIFACT_BYTES + int(outside)
        secret = "BYTE_LIMIT_SECRET" if outside else ""
        padding = target_size - empty_size - len(secret)
        return {"blob": ("x" * padding) + secret}
    raise AssertionError(f"unknown payload limit case: {kind}")


def _limit_response(payload: object, *, identity_secret: bool = False) -> RayBetHTTPResponse:
    request_identity = f"{BASE_URL}/odds?match_id=1001"
    if identity_secret:
        request_identity += "&token=REQUEST_ID_SECRET"
    return RayBetHTTPResponse(
        payload,
        f"{BASE_URL}/odds",
        request_identity,
        NOW,
        200,
        200,
    )


@pytest.mark.parametrize("kind", ("nodes", "depth", "bytes"))
def test_payload_limit_exact_boundary_is_accepted(
    tmp_path: Path,
    kind: str,
) -> None:
    payload = _payload_limit_case(kind, outside=False)
    process_called = False

    def process(_context: DirectResponseContext) -> DirectResponseDecision[str]:
        nonlocal process_called
        process_called = True
        return DirectResponseDecision(
            "processed", disposition="audit_only", reason="boundary_accepted"
        )

    with LiveBettingStore(tmp_path / f"inside-{kind}.db") as store:
        store.init_schema()
        result = audited_direct_request(
            store,
            fetch=lambda: _limit_response(payload),
            process=process,
            response_kind="live_odds",
            claimed_raybet_match_id="1001",
            endpoint=f"{BASE_URL}/odds",
            request_identity=f"{BASE_URL}/odds?match_id=1001",
        )
        assert result == "processed"
        assert process_called
        row = audit_rows(store)[0]
        assert row["payload_kind"] == "provider_response"
        assert row["reason"] == "boundary_accepted"
        if kind == "bytes":
            artifact = store.connection.execute(
                "SELECT uncompressed_bytes FROM odds_raw_artifacts WHERE artifact_hash=?",
                (str(row["artifact_hash"]),),
            ).fetchone()
            assert artifact is not None
            assert artifact["uncompressed_bytes"] == MAX_RESPONSE_ARTIFACT_BYTES


@pytest.mark.parametrize(
    ("kind", "error_type", "payload_secret"),
    (
        ("nodes", RayBetPayloadSanitizationError, "NODE_LIMIT_SECRET"),
        ("depth", RayBetPayloadSanitizationError, "DEPTH_LIMIT_SECRET"),
        ("bytes", ResponseArtifactLimitError, "BYTE_LIMIT_SECRET"),
    ),
)
def test_payload_limit_excess_writes_only_bounded_failure_receipt(
    tmp_path: Path,
    kind: str,
    error_type: type[Exception],
    payload_secret: str,
) -> None:
    payload = _payload_limit_case(kind, outside=True)
    process_called = False

    def process(_context: DirectResponseContext) -> DirectResponseDecision[None]:
        nonlocal process_called
        process_called = True
        return DirectResponseDecision(
            None, disposition="audit_only", reason="unexpected"
        )

    database = tmp_path / f"outside-{kind}.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        with pytest.raises(error_type):
            audited_direct_request(
                store,
                fetch=lambda: _limit_response(payload, identity_secret=True),
                process=process,
                response_kind="live_odds",
                claimed_raybet_match_id="1001",
                endpoint=f"{BASE_URL}/odds",
                request_identity=f"{BASE_URL}/odds?match_id=1001",
                request_metadata={"operation": "limit_test"},
            )
        assert not process_called
        row = audit_rows(store)[0]
        assert row["payload_kind"] == "request_failure"
        assert row["disposition"] == "rejected"
        assert row["reason"] == PAYLOAD_LIMIT_FAILURE_TYPE
        assert row["http_status"] == 200
        assert row["provider_code"] == 200
        assert row["endpoint"] == f"{BASE_URL}/odds"
        assert row["request_identity"] == f"{BASE_URL}/odds?match_id=1001"
        metadata = request_metadata(row)
        structured_identity = {
            "scheme": "https",
            "authority": "cfinfo.365raylinks.com",
            "path": "/v2/odds",
            "query": [["match_id", "1001"]],
        }
        assert metadata == {
            "operation": "limit_test",
            "expected_endpoint": f"{BASE_URL}/odds",
            "expected_request_identity": structured_identity,
            "actual_endpoint": f"{BASE_URL}/odds",
            "actual_request_identity": structured_identity,
        }
        assert store.direct_response_payload(str(row["audit_key"])) == {
            "artifact_version": "raybet-direct-request-failure-v1",
            "response_kind": "live_odds",
            "claimed_raybet_match_id": "1001",
            "failure": {"error_type": PAYLOAD_LIMIT_FAILURE_TYPE},
        }
        persisted = b"".join(
            gzip.decompress(path.read_bytes())
            for path in store.raw_archive_root.rglob("*.json.gz")
        )
        assert payload_secret.encode() not in persisted
        assert b"REQUEST_ID_SECRET" not in persisted
        assert len(persisted) < 1024
    database_bytes = database.read_bytes()
    assert payload_secret.encode() not in database_bytes
    assert b"REQUEST_ID_SECRET" not in database_bytes


def test_non_object_client_limit_failure_still_writes_bounded_audit(
    tmp_path: Path,
) -> None:
    secret = "CLIENT_LIMIT_SECRET"
    payload: list[object] = [None] * 100_000
    payload.append({"token": secret})
    client = RayBetClient(client=Session([HTTPResponse(payload)]))

    database = tmp_path / "client-limit.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        with pytest.raises(RayBetPayloadSanitizationError):
            _fetch_match_list(store, client, response_kind="live_match_list")
        row = audit_rows(store)[0]
        assert row["response_kind"] == "live_match_list"
        assert row["payload_kind"] == "request_failure"
        assert row["reason"] == PAYLOAD_LIMIT_FAILURE_TYPE
        assert request_metadata(row)["match_type"] == 1
        assert store.direct_response_payload(str(row["audit_key"]))["failure"] == {
            "error_type": PAYLOAD_LIMIT_FAILURE_TYPE
        }
        persisted = b"".join(
            gzip.decompress(path.read_bytes())
            for path in store.raw_archive_root.rglob("*.json.gz")
        )
        assert secret.encode() not in persisted
    assert secret.encode() not in database.read_bytes()


def test_compatibility_aggregate_is_explicitly_marked(tmp_path: Path) -> None:
    class Client:
        def live_matches(self) -> list[dict[str, object]]:
            return [{"id": "1001", "game_id": 151}]

    with LiveBettingStore(tmp_path / "live.db") as store:
        store.init_schema()
        assert _fetch_match_list(
            store, Client(), response_kind="live_match_list"
        ) == [{"id": "1001", "game_id": 151}]
        row = audit_rows(store)[0]
        assert row["payload_kind"] == "aggregate"
        assert json.loads(str(row["request_metadata_json"])) == {"aggregate": True}


@pytest.mark.parametrize("response_kind", ("live_odds", "completed_odds"))
def test_live_and_completed_odds_share_exact_success_receipts(
    tmp_path: Path, response_kind: str
) -> None:
    response = RayBetHTTPResponse(
        {
            "code": 200,
            "result": {
                "id": "1001",
                "game_id": 151,
                "team": [
                    {"pos": 1, "team_name": "One"},
                    {"pos": 2, "team_name": "Two"},
                ],
                "odds": [],
            },
        },
        f"{BASE_URL}/odds",
        f"{BASE_URL}/odds?match_id=1001",
        NOW,
        200,
        200,
    )

    class Client:
        def match_odds_response(self, match_id: str) -> RayBetHTTPResponse:
            assert match_id == "1001"
            return response

    with LiveBettingStore(tmp_path / f"{response_kind}.db") as store:
        store.init_schema()
        assert _collect_odds_response(
            store,
            Client(),
            match_id="1001",
            response_kind=response_kind,
            list_row={"id": "1001", "status": 3},
        )[:2] == (0, 0)
        row = audit_rows(store)[0]
        assert row["response_kind"] == response_kind
        assert row["observed_at"] == NOW.isoformat()
        assert row["http_status"] == 200
        assert row["provider_code"] == 200
        assert row["payload_kind"] == "provider_response"
        assert request_metadata(row) == {"operation": response_kind}


@pytest.mark.parametrize("response_kind", ("live_odds", "completed_odds"))
def test_odds_receipt_identity_mismatch_never_enters_normalized_state(
    tmp_path: Path, response_kind: str
) -> None:
    response = RayBetHTTPResponse(
        {
            "code": 200,
            "result": {
                "id": "1001",
                "game_id": 151,
                "team": [],
                "odds": [],
            },
        },
        f"{BASE_URL}/odds",
        f"{BASE_URL}/odds?match_id=999",
        NOW,
        200,
        200,
    )

    class Client:
        def match_odds_response(self, _match_id: str) -> RayBetHTTPResponse:
            return response

    with LiveBettingStore(tmp_path / f"{response_kind}.db") as store:
        store.init_schema()
        with pytest.raises(DirectResponseRequestIdentityError):
            _collect_odds_response(
                store,
                Client(),
                match_id="1001",
                response_kind=response_kind,
                list_row={"id": "1001", "status": 3},
            )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM raybet_matches"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM odds_transport_observations"
        ).fetchone()[0] == 0
        assert audit_rows(store)[0]["reason"] == "request_identity_mismatch"


def test_bare_dict_compatibility_response_is_not_labeled_as_http_receipt(
    tmp_path: Path,
) -> None:
    class Client:
        def match_odds(self, _match_id: str) -> dict[str, object]:
            return {
                "code": 200,
                "result": {
                    "id": "1001",
                    "game_id": 151,
                    "team": [
                        {"pos": 1, "team_name": "One"},
                        {"pos": 2, "team_name": "Two"},
                    ],
                    "odds": [],
                },
            }

    with LiveBettingStore(tmp_path / "compat.db") as store:
        store.init_schema()
        _collect_odds_response(
            store,
            Client(),
            match_id="1001",
            response_kind="live_odds",
            list_row={"id": "1001"},
        )
        row = audit_rows(store)[0]
        assert row["payload_kind"] == "aggregate"
        assert request_metadata(row) == {
            "operation": "live_odds",
            "transport_receipt": "compat",
        }


@pytest.mark.parametrize("response_kind", ("live_odds", "completed_odds"))
@pytest.mark.parametrize("provider_payload", ([{"result": []}], "not-an-object"))
def test_non_object_odds_receipt_never_enters_normalized_state(
    tmp_path: Path,
    response_kind: str,
    provider_payload: object,
) -> None:
    response = RayBetHTTPResponse(
        provider_payload,
        f"{BASE_URL}/odds",
        f"{BASE_URL}/odds?match_id=1001",
        NOW,
        200,
        None,
    )

    class Client:
        def match_odds_response(self, _match_id: str) -> RayBetHTTPResponse:
            return response

    with LiveBettingStore(tmp_path / f"malformed-{response_kind}.db") as store:
        store.init_schema()
        with pytest.raises(DirectResponsePayloadShapeError):
            _collect_odds_response(
                store,
                Client(),
                match_id="1001",
                response_kind=response_kind,
                list_row={"id": "1001", "status": 3},
            )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM raybet_matches"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM odds_transport_observations"
        ).fetchone()[0] == 0
        row = audit_rows(store)[0]
        assert row["payload_kind"] == "provider_response"
        assert row["reason"] == "validation_failed"
        assert store.direct_response_payload(str(row["audit_key"])) == provider_payload


@pytest.mark.parametrize("response_kind", ("live_odds", "completed_odds"))
def test_live_and_completed_odds_share_sanitized_failure_receipts(
    tmp_path: Path, response_kind: str
) -> None:
    class Client:
        def match_odds_response(self, match_id: str) -> RayBetHTTPResponse:
            raise TimeoutError("request failed?token=ODDS_FAILURE_SECRET")

    with LiveBettingStore(tmp_path / f"{response_kind}.db") as store:
        store.init_schema()
        with pytest.raises(TimeoutError):
            _collect_odds_response(
                store,
                Client(),
                match_id="1001",
                response_kind=response_kind,
                list_row={"id": "1001", "status": 3},
            )
        row = audit_rows(store)[0]
        assert row["response_kind"] == response_kind
        assert row["payload_kind"] == "request_failure"
        assert row["disposition"] == "rejected"
        assert request_metadata(row) == {"operation": response_kind}
        payload = store.direct_response_payload(str(row["audit_key"]))
        assert payload["failure"] == {"error_type": "TimeoutError"}
        assert "ODDS_FAILURE_SECRET" not in json.dumps(payload)


def test_final_receipt_identity_mismatch_never_enters_final_authority(
    tmp_path: Path,
) -> None:
    response = RayBetHTTPResponse(
        {
            "code": 200,
            "result": {
                "id": "1001",
                "game_id": 151,
                "team": [],
                "odds": [],
            },
        },
        f"{BASE_URL}/odds",
        f"{BASE_URL}/odds?match_id=999",
        NOW,
        200,
        200,
    )

    class Client:
        def match_odds_response(self, _match_id: str) -> RayBetHTTPResponse:
            return response

    with LiveBettingStore(tmp_path / "final.db") as store:
        store.init_schema()
        with pytest.raises(DirectResponseRequestIdentityError):
            _refresh_raybet_final(store, Client(), "1001")
        assert store.connection.execute(
            "SELECT COUNT(*) FROM raybet_matches"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM odds_transport_observations"
        ).fetchone()[0] == 0
        row = audit_rows(store)[0]
        assert row["response_kind"] == "final_odds"
        assert row["reason"] == "request_identity_mismatch"


@pytest.mark.parametrize("provider_payload", ([{"result": []}], "not-an-object"))
def test_non_object_final_receipt_never_enters_final_authority(
    tmp_path: Path,
    provider_payload: object,
) -> None:
    response = RayBetHTTPResponse(
        provider_payload,
        f"{BASE_URL}/odds",
        f"{BASE_URL}/odds?match_id=1001",
        NOW,
        200,
        None,
    )

    class Client:
        def match_odds_response(self, _match_id: str) -> RayBetHTTPResponse:
            return response

    with LiveBettingStore(tmp_path / "malformed-final.db") as store:
        store.init_schema()
        with pytest.raises(DirectResponsePayloadShapeError):
            _refresh_raybet_final(store, Client(), "1001")
        assert store.connection.execute(
            "SELECT COUNT(*) FROM raybet_matches"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM odds_transport_observations"
        ).fetchone()[0] == 0
        row = audit_rows(store)[0]
        assert row["response_kind"] == "final_odds"
        assert row["reason"] == "validation_failed"
        assert store.direct_response_payload(str(row["audit_key"])) == provider_payload


def watcher_database(path: Path) -> None:
    raw = {
        "team": [
            {"score": {"manualControlData": {"currentIndex": 1}}},
            {"score": {"manualControlData": {"currentIndex": 1}}},
        ]
    }
    with LiveBettingStore(path) as store:
        store.init_schema()
        store.connection.execute(
            """INSERT INTO raybet_matches
               (raybet_match_id, status, live_url, raw_json, best_of, updated_at)
               VALUES ('42', '2', ?, ?, 3, ?)""",
            (STREAM_URL, json.dumps(raw), NOW.isoformat()),
        )
        store.connection.commit()


def test_watcher_refresh_audits_success_and_never_persists_signed_url(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    watcher_database(database)
    secret = "EPHEMERAL_WATCH_TOKEN"
    signed = f"https://qplay.ehome.gg/live.m3u8?auth_key={secret}"
    response = RayBetHTTPResponse(
        {
            "code": 200,
            "result": {
                "id": 42,
                "game_id": 151,
                "live_url": signed,
                "team": [
                    {"score": {"manualControlData": {"currentIndex": 1}}},
                    {"score": {"manualControlData": {"currentIndex": 1}}},
                ],
            },
        },
        f"{BASE_URL}/odds",
        f"{BASE_URL}/odds?match_id=42",
        NOW + timedelta(seconds=1),
        200,
        200,
    )

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def match_odds_response(self, match_id: str) -> RayBetHTTPResponse:
            assert match_id == "42"
            return response

    with patch("scripts.watch_raybet_stream.RayBetClient", return_value=Client()):
        assert match_source(database, "42", refresh_url=True) == (signed, 1)

    with LiveBettingStore(database) as store:
        row = audit_rows(store)[0]
        assert row["reason"] == "stream_url_refresh"
        assert row["payload_kind"] == "provider_response"
        persisted = store.direct_response_payload(str(row["audit_key"]))
        assert persisted["result"]["live_url"] == (
            "https://qplay.ehome.gg/live.m3u8"
        )
        artifacts = list(store.raw_archive_root.rglob("*.json.gz"))
        assert artifacts
        assert all(secret.encode() not in gzip.decompress(path.read_bytes()) for path in artifacts)
    assert secret.encode() not in database.read_bytes()
    wal = database.with_name(f"{database.name}-wal")
    if wal.exists():
        assert secret.encode() not in wal.read_bytes()


def test_watcher_receipt_identity_mismatch_never_returns_stream_url(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    watcher_database(database)
    response = RayBetHTTPResponse(
        {
            "code": 200,
            "result": {
                "id": 42,
                "game_id": 151,
                "live_url": STREAM_URL,
                "team": [
                    {"score": {"manualControlData": {"currentIndex": 1}}},
                    {"score": {"manualControlData": {"currentIndex": 1}}},
                ],
            },
        },
        f"{BASE_URL}/odds",
        f"{BASE_URL}/odds?match_id=999",
        NOW + timedelta(seconds=1),
        200,
        200,
    )

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def match_odds_response(self, _match_id: str) -> RayBetHTTPResponse:
            return response

    with patch("scripts.watch_raybet_stream.RayBetClient", return_value=Client()):
        with pytest.raises(DirectResponseRequestIdentityError):
            match_source(database, "42", refresh_url=True)

    with LiveBettingStore(database) as store:
        row = audit_rows(store)[0]
        assert row["response_kind"] == "live_odds"
        assert row["disposition"] == "rejected"
        assert row["reason"] == "request_identity_mismatch"


@pytest.mark.parametrize("provider_payload", ([{"result": []}], "not-an-object"))
def test_non_object_watcher_receipt_never_returns_stream_url(
    tmp_path: Path,
    provider_payload: object,
) -> None:
    database = tmp_path / "live.db"
    watcher_database(database)
    response = RayBetHTTPResponse(
        provider_payload,
        f"{BASE_URL}/odds",
        f"{BASE_URL}/odds?match_id=42",
        NOW + timedelta(seconds=1),
        200,
        None,
    )

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def match_odds_response(self, _match_id: str) -> RayBetHTTPResponse:
            return response

    with patch("scripts.watch_raybet_stream.RayBetClient", return_value=Client()):
        with pytest.raises(DirectResponsePayloadShapeError):
            match_source(database, "42", refresh_url=True)

    with LiveBettingStore(database) as store:
        row = audit_rows(store)[0]
        assert row["response_kind"] == "live_odds"
        assert row["disposition"] == "rejected"
        assert row["reason"] == "validation_failed"
        assert store.direct_response_payload(str(row["audit_key"])) == provider_payload


def test_watcher_refresh_failure_is_sanitized_and_replayable(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    watcher_database(database)
    secret = "FAILURE_SECRET_TOKEN"

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def match_odds_response(self, match_id: str) -> RayBetHTTPResponse:
            raise TimeoutError(f"upstream?token={secret}")

    with patch("scripts.watch_raybet_stream.RayBetClient", return_value=Client()):
        with pytest.raises(TimeoutError):
            match_source(database, "42", refresh_url=True)

    with LiveBettingStore(database) as store:
        row = audit_rows(store)[0]
        assert row["payload_kind"] == "request_failure"
        assert row["disposition"] == "rejected"
        payload = store.direct_response_payload(str(row["audit_key"]))
        assert payload["failure"] == {"error_type": "TimeoutError"}
        assert secret not in json.dumps(payload)
        artifacts = list(store.raw_archive_root.rglob("*.json.gz"))
        assert all(secret.encode() not in gzip.decompress(path.read_bytes()) for path in artifacts)
    assert secret.encode() not in database.read_bytes()


def test_public_client_return_values_remain_unchanged() -> None:
    odds = {"code": 200, "result": {"id": "42", "game_id": 151}}
    matches = {
        "code": 200,
        "result": [{"id": "42", "game_id": 151, "status": 2}],
    }
    session = Session([HTTPResponse(odds), HTTPResponse(matches)])
    client = RayBetClient(client=session)

    assert client.match_odds("42") == odds
    assert client.match_page(1, 1) == matches["result"]
    assert session.calls == [
        (f"{BASE_URL}/odds", {"match_id": "42"}),
        (f"{BASE_URL}/match", {"match_type": 1, "page": 1}),
    ]
