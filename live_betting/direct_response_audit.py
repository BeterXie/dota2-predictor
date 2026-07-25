"""One immutable, sanitized audit path for direct RayBet HTTP responses."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Generic, TypeVar
from urllib.parse import parse_qsl, urlsplit
import uuid

from event_intelligence.raw_archive import (
    ArtifactReceipt,
    sanitize_request_identity,
)

from .raybet import RayBetHTTPResponse
from .odds_response_authority import ResponseArtifactLimitError
from .sanitize import (
    RayBetPayloadSanitizationError,
    sanitize_raybet_payload,
)

if TYPE_CHECKING:
    from .storage import LiveBettingStore


DIRECT_REQUEST_FAILURE_FORMAT = "raybet-direct-request-failure-v1"
PAYLOAD_LIMIT_FAILURE_TYPE = "payload_limits_exceeded"

T = TypeVar("T")


@dataclass(frozen=True)
class DirectResponseDecision(Generic[T]):
    value: T
    disposition: str
    reason: str
    observed_raybet_match_id: str | None = None


@dataclass(frozen=True)
class DirectResponseContext:
    payload: dict[str, Any]
    sanitized_payload: dict[str, Any]
    observed_at: datetime
    endpoint: str
    request_identity: str
    http_status: int | None
    provider_code: int | None
    receipt: ArtifactReceipt


class DirectResponseRequestIdentityError(ValueError):
    """A transport receipt does not identify the request that was issued."""


class DirectResponsePayloadShapeError(ValueError):
    """A parsed direct response is not the required object envelope."""


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("RayBet response received_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    sanitized = sanitize_raybet_payload(dict(value or {}))
    if not isinstance(sanitized, dict):
        raise ValueError("direct request metadata must be an object")
    return sanitized


def _transport_metadata(source: object) -> dict[str, Any]:
    started_at = getattr(source, "request_started_at", None)
    if started_at is None:
        started_at = getattr(source, "raybet_request_started_at", None)
    duration_ms = getattr(source, "transport_duration_ms", None)
    if duration_ms is None:
        duration_ms = getattr(source, "raybet_transport_duration_ms", None)
    metadata: dict[str, Any] = {}
    if isinstance(started_at, datetime):
        metadata["request_started_at"] = _aware_utc(started_at).isoformat()
    if (
        isinstance(duration_ms, (int, float))
        and not isinstance(duration_ms, bool)
        and duration_ms >= 0
    ):
        metadata["transport_duration_ms"] = float(duration_ms)
    return metadata


def _provider_code(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("code")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _canonical_request_identity(value: str, field: str) -> str:
    try:
        canonical = sanitize_request_identity(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"direct response {field} is invalid") from error
    if not canonical:
        raise ValueError(f"direct response {field} is invalid")
    return canonical


def _receipt_identity_matches(
    *,
    expected_endpoint: str,
    expected_request_identity: str,
    actual_endpoint: str,
    actual_request_identity: str,
) -> bool:
    try:
        return (
            _canonical_request_identity(actual_endpoint, "actual endpoint")
            == expected_endpoint
            and _canonical_request_identity(
                actual_request_identity, "actual request identity"
            )
            == expected_request_identity
        )
    except ValueError:
        return False


def _payload_limit_metadata(
    metadata: Mapping[str, Any],
    *,
    expected_endpoint: str,
    expected_request_identity: str,
    actual_endpoint: str,
    actual_request_identity: str,
) -> dict[str, Any]:
    def identity(value: str) -> dict[str, Any]:
        parsed = urlsplit(value)
        return {
            "scheme": parsed.scheme,
            "authority": parsed.netloc,
            "path": parsed.path,
            "query": [list(item) for item in parse_qsl(parsed.query)],
        }

    actual_endpoint = _canonical_request_identity(
        actual_endpoint, "actual endpoint"
    )
    actual_request_identity = _canonical_request_identity(
        actual_request_identity, "actual request identity"
    )
    return {
        **metadata,
        "expected_endpoint": expected_endpoint,
        "expected_request_identity": identity(expected_request_identity),
        "actual_endpoint": actual_endpoint,
        "actual_request_identity": identity(actual_request_identity),
    }


def record_direct_request_failure(
    store: LiveBettingStore,
    *,
    response_kind: str,
    claimed_raybet_match_id: str | None,
    error: BaseException,
    observed_at: datetime,
    endpoint: str | None = None,
    request_identity: str | None = None,
    http_status: int | None = None,
    provider_code: int | None = None,
    request_metadata: Mapping[str, Any] | None = None,
    reason: str | None = None,
    failure_error_type: str | None = None,
) -> str:
    """Persist a sanitized failure artifact and immutable audit row."""

    observed_at = _aware_utc(observed_at)
    if failure_error_type not in {None, PAYLOAD_LIMIT_FAILURE_TYPE}:
        raise ValueError("direct request failure type is invalid")
    error_type = (
        failure_error_type
        or type(error).__name__[:100]
        or "Exception"
    )
    payload = {
        "artifact_version": DIRECT_REQUEST_FAILURE_FORMAT,
        "response_kind": response_kind,
        "claimed_raybet_match_id": claimed_raybet_match_id,
        "failure": {"error_type": error_type},
    }
    receipt = store.archive_response_payload(
        payload,
        observed_at=observed_at,
        match_id=claimed_raybet_match_id,
        response_kind=response_kind,
        endpoint=endpoint,
        request_identity=request_identity,
        status_code=http_status,
    )
    return store.record_direct_response_audit(
        receipt,
        response_kind=response_kind,
        claimed_raybet_match_id=claimed_raybet_match_id,
        observed_raybet_match_id=None,
        disposition="rejected",
        reason=reason or f"request_failed:{error_type}",
        provider_code=provider_code,
        request_metadata=_metadata(request_metadata),
        payload_kind="request_failure",
        sanitized=True,
    )


def _record_payload_limit_failure(
    store: LiveBettingStore,
    *,
    response_kind: str,
    claimed_raybet_match_id: str | None,
    error: BaseException,
    observed_at: datetime,
    endpoint: str,
    request_identity: str,
    expected_endpoint: str,
    expected_request_identity: str,
    http_status: int | None,
    provider_code: int | None,
    request_metadata: Mapping[str, Any],
) -> str:
    return record_direct_request_failure(
        store,
        response_kind=response_kind,
        claimed_raybet_match_id=claimed_raybet_match_id,
        error=error,
        observed_at=observed_at,
        endpoint=endpoint,
        request_identity=request_identity,
        http_status=http_status,
        provider_code=provider_code,
        request_metadata=_payload_limit_metadata(
            request_metadata,
            expected_endpoint=expected_endpoint,
            expected_request_identity=expected_request_identity,
            actual_endpoint=endpoint,
            actual_request_identity=request_identity,
        ),
        reason=PAYLOAD_LIMIT_FAILURE_TYPE,
        failure_error_type=PAYLOAD_LIMIT_FAILURE_TYPE,
    )


def audited_direct_request(
    store: LiveBettingStore,
    *,
    fetch: Callable[[], RayBetHTTPResponse | dict[str, Any]],
    process: Callable[[DirectResponseContext], DirectResponseDecision[T]],
    response_kind: str,
    claimed_raybet_match_id: str | None,
    endpoint: str,
    request_identity: str,
    request_metadata: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    rejection_reason: Callable[[Exception], str] | None = None,
) -> T:
    """Fetch, sanitize, archive, validate, and audit one direct response."""

    expected_endpoint = _canonical_request_identity(endpoint, "endpoint")
    expected_request_identity = _canonical_request_identity(
        request_identity, "request identity"
    )
    metadata = {
        **_metadata(request_metadata),
        "receipt_id": uuid.uuid4().hex,
    }
    now = clock or (lambda: datetime.now(timezone.utc))
    try:
        fetched = fetch()
    except Exception as error:
        failed_response = getattr(error, "raybet_response", None)
        if isinstance(failed_response, RayBetHTTPResponse):
            metadata.update(_transport_metadata(failed_response))
            observed_at = _aware_utc(failed_response.received_at)
            try:
                failed_payload = sanitize_raybet_payload(failed_response.payload)
                receipt = store.archive_response_payload(
                    failed_payload,
                    observed_at=observed_at,
                    match_id=claimed_raybet_match_id,
                    response_kind=response_kind,
                    endpoint=failed_response.endpoint,
                    request_identity=failed_response.request_identity,
                    status_code=failed_response.http_status,
                )
                result = (
                    failed_payload.get("result")
                    if isinstance(failed_payload, dict)
                    else None
                )
                observed_match_id = (
                    str(result.get("id") or "") or None
                    if isinstance(result, dict)
                    else None
                )
                identity_matches = _receipt_identity_matches(
                    expected_endpoint=expected_endpoint,
                    expected_request_identity=expected_request_identity,
                    actual_endpoint=receipt.endpoint,
                    actual_request_identity=receipt.request_identity,
                )
                store.record_direct_response_audit(
                    receipt,
                    response_kind=response_kind,
                    claimed_raybet_match_id=claimed_raybet_match_id,
                    observed_raybet_match_id=observed_match_id,
                    disposition="rejected",
                    reason=(
                        "request_identity_mismatch"
                        if not identity_matches
                        else (
                            "validation_failed"
                            if not isinstance(failed_payload, dict)
                            else f"request_failed:{type(error).__name__}"
                        )
                    ),
                    provider_code=failed_response.provider_code,
                    request_metadata=metadata,
                    payload_kind="provider_response",
                    sanitized=True,
                )
            except (
                RayBetPayloadSanitizationError,
                ResponseArtifactLimitError,
            ) as audit_error:
                _record_payload_limit_failure(
                    store,
                    response_kind=response_kind,
                    claimed_raybet_match_id=claimed_raybet_match_id,
                    error=audit_error,
                    observed_at=observed_at,
                    endpoint=failed_response.endpoint,
                    request_identity=failed_response.request_identity,
                    expected_endpoint=expected_endpoint,
                    expected_request_identity=expected_request_identity,
                    http_status=failed_response.http_status,
                    provider_code=failed_response.provider_code,
                    request_metadata=metadata,
                )
                raise audit_error from error
            except Exception as audit_error:
                raise audit_error from error
            raise
        metadata.update(_transport_metadata(error))
        received_at = getattr(error, "raybet_received_at", None)
        observed_at = _aware_utc(
            received_at if isinstance(received_at, datetime) else now()
        )
        response = getattr(error, "response", None)
        raw_status = getattr(
            error,
            "raybet_http_status",
            getattr(response, "status_code", None),
        )
        http_status = (
            int(raw_status)
            if isinstance(raw_status, int) and not isinstance(raw_status, bool)
            else None
        )
        actual_endpoint = str(getattr(error, "raybet_endpoint", endpoint))
        actual_identity = str(
            getattr(error, "raybet_request_identity", request_identity)
        )
        identity_matches = _receipt_identity_matches(
            expected_endpoint=expected_endpoint,
            expected_request_identity=expected_request_identity,
            actual_endpoint=actual_endpoint,
            actual_request_identity=actual_identity,
        )
        record_direct_request_failure(
            store,
            response_kind=response_kind,
            claimed_raybet_match_id=claimed_raybet_match_id,
            error=error,
            observed_at=observed_at,
            endpoint=actual_endpoint,
            request_identity=actual_identity,
            http_status=http_status,
            request_metadata=metadata,
            reason=None if identity_matches else "request_identity_mismatch",
        )
        raise

    if isinstance(fetched, RayBetHTTPResponse):
        metadata.update(_transport_metadata(fetched))
        raw_payload = fetched.payload
        observed_at = _aware_utc(fetched.received_at)
        actual_endpoint = fetched.endpoint
        actual_identity = fetched.request_identity
        http_status = fetched.http_status
        provider_code = fetched.provider_code
        payload_kind = "provider_response"
    else:
        raw_payload = fetched
        observed_at = _aware_utc(now())
        actual_endpoint = endpoint
        actual_identity = request_identity
        http_status = None
        provider_code = (
            _provider_code(raw_payload)
            if isinstance(raw_payload, Mapping)
            else None
        )
        payload_kind = "aggregate"
        metadata["transport_receipt"] = "compat"
    try:
        sanitized = sanitize_raybet_payload(raw_payload)
    except RayBetPayloadSanitizationError as error:
        _record_payload_limit_failure(
            store,
            response_kind=response_kind,
            claimed_raybet_match_id=claimed_raybet_match_id,
            error=error,
            observed_at=observed_at,
            endpoint=actual_endpoint,
            request_identity=actual_identity,
            expected_endpoint=expected_endpoint,
            expected_request_identity=expected_request_identity,
            http_status=http_status,
            provider_code=provider_code,
            request_metadata=metadata,
        )
        raise
    except Exception as error:
        record_direct_request_failure(
            store,
            response_kind=response_kind,
            claimed_raybet_match_id=claimed_raybet_match_id,
            error=error,
            observed_at=observed_at,
            endpoint=actual_endpoint,
            request_identity=actual_identity,
            http_status=http_status,
            provider_code=provider_code,
            request_metadata=metadata,
        )
        raise

    try:
        receipt = store.archive_response_payload(
            sanitized,
            observed_at=observed_at,
            match_id=claimed_raybet_match_id,
            response_kind=response_kind,
            endpoint=actual_endpoint,
            request_identity=actual_identity,
            status_code=http_status,
        )
    except ResponseArtifactLimitError as error:
        _record_payload_limit_failure(
            store,
            response_kind=response_kind,
            claimed_raybet_match_id=claimed_raybet_match_id,
            error=error,
            observed_at=observed_at,
            endpoint=actual_endpoint,
            request_identity=actual_identity,
            expected_endpoint=expected_endpoint,
            expected_request_identity=expected_request_identity,
            http_status=http_status,
            provider_code=provider_code,
            request_metadata=metadata,
        )
        raise
    if payload_kind == "provider_response" and not _receipt_identity_matches(
        expected_endpoint=expected_endpoint,
        expected_request_identity=expected_request_identity,
        actual_endpoint=receipt.endpoint,
        actual_request_identity=receipt.request_identity,
    ):
        result = sanitized.get("result")
        observed_match_id = (
            str(result.get("id") or "") or None
            if isinstance(result, dict)
            else None
        )
        store.record_direct_response_audit(
            receipt,
            response_kind=response_kind,
            claimed_raybet_match_id=claimed_raybet_match_id,
            observed_raybet_match_id=observed_match_id,
            disposition="rejected",
            reason="request_identity_mismatch",
            provider_code=provider_code,
            request_metadata=metadata,
            payload_kind=payload_kind,
            sanitized=True,
        )
        raise DirectResponseRequestIdentityError(
            "RayBet response request identity does not match the issued request"
        )
    if not isinstance(sanitized, dict):
        store.record_direct_response_audit(
            receipt,
            response_kind=response_kind,
            claimed_raybet_match_id=claimed_raybet_match_id,
            observed_raybet_match_id=None,
            disposition="rejected",
            reason="validation_failed",
            provider_code=provider_code,
            request_metadata=metadata,
            payload_kind=payload_kind,
            sanitized=True,
        )
        raise DirectResponsePayloadShapeError(
            "RayBet response must be an object"
        )
    context = DirectResponseContext(
        payload=raw_payload,
        sanitized_payload=sanitized,
        observed_at=observed_at,
        endpoint=receipt.endpoint,
        request_identity=receipt.request_identity,
        http_status=http_status,
        provider_code=provider_code,
        receipt=receipt,
    )
    try:
        with store.transaction():
            decision = process(context)
            store.record_direct_response_audit(
                receipt,
                response_kind=response_kind,
                claimed_raybet_match_id=claimed_raybet_match_id,
                observed_raybet_match_id=decision.observed_raybet_match_id,
                disposition=decision.disposition,
                reason=decision.reason,
                provider_code=provider_code,
                request_metadata=metadata,
                payload_kind=payload_kind,
                sanitized=True,
            )
    except Exception as error:
        observed_match_id = None
        result = sanitized.get("result")
        if isinstance(result, dict):
            observed_match_id = str(result.get("id") or "") or None
        reason = (
            rejection_reason(error)
            if rejection_reason is not None
            else f"validation_failed:{type(error).__name__}"
        )
        store.record_direct_response_audit(
            receipt,
            response_kind=response_kind,
            claimed_raybet_match_id=claimed_raybet_match_id,
            observed_raybet_match_id=observed_match_id,
            disposition="rejected",
            reason=reason,
            provider_code=provider_code,
            request_metadata=metadata,
            payload_kind=payload_kind,
            sanitized=True,
        )
        raise
    return decision.value


__all__ = [
    "DIRECT_REQUEST_FAILURE_FORMAT",
    "DirectResponseContext",
    "DirectResponseDecision",
    "DirectResponsePayloadShapeError",
    "DirectResponseRequestIdentityError",
    "audited_direct_request",
    "record_direct_request_failure",
]
