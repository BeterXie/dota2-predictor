"""Application orchestration for immutable STRATZ official R.O.S.H. runs.

The module deliberately owns no scoring rules.  It joins the frozen request
planner, official transport, exact-byte evidence store, pure normalizer/scorer,
and append-only repository while enforcing the canonical-draft run boundary.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol

from live_betting.rosh_parity_storage import (
    RoshHeroScoreRecord,
    RoshMinutePointRecord,
    RoshRunRecord,
    RoshRunRepository,
    StoredRoshRun,
)
from live_betting.stratz_rosh_client import OfficialRoshBatch, StratzRoshError
from prematch.stratz_official_profile import (
    RoshAnalysisInput,
    RoshParityProfile,
    RoshRequestPlan,
    build_official_request_plan,
    canonical_bytes,
    get_profile,
    validate_active_profile,
    validate_draft,
)
from prematch.stratz_official_score import (
    OfficialRoshResult,
    ScoreError,
    normalize_official_responses,
    score_official_rosh,
)


PUBLIC_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "profile_drift",
        "source_data_incomplete",
        "source_draft_mismatch",
        "source_match_not_found",
        "upstream_rate_limited",
        "upstream_unavailable",
    }
)
_PUBLIC_MESSAGES = MappingProxyType(
    {
        "invalid_request": "Rosh analysis request is invalid",
        "profile_drift": "Rosh analysis profile validation failed",
        "source_data_incomplete": "Rosh source data is incomplete",
        "source_draft_mismatch": "Rosh source draft identity does not match",
        "source_match_not_found": "Rosh source match was not found",
        "upstream_rate_limited": "Rosh upstream is rate limited",
        "upstream_unavailable": "Rosh upstream is unavailable",
    }
)
_SECRET_PATTERN = re.compile(
    rb"authorization|bearer\s+|set-cookie|cookie|password|"
    rb"(?:api[-_ ]?key)|secret|session[-_ ]?(?:id|token)|access[-_ ]?token",
    re.IGNORECASE,
)


class OfficialBatchTransport(Protocol):
    def fetch_official_batch(self, plan: RoshRequestPlan) -> OfficialRoshBatch: ...


class RoshAnalysisError(RuntimeError):
    """A public, allowlisted analysis failure with no upstream text."""

    def __init__(
        self,
        error_code: str,
        *,
        run_id: str | None = None,
    ) -> None:
        if error_code not in PUBLIC_ERROR_CODES:
            raise ValueError("error_code is not public")
        super().__init__(_PUBLIC_MESSAGES[error_code])
        self.error_code = error_code
        self.run_id = run_id


class ArtifactError(RuntimeError):
    """Raised when exact transport bytes cannot be safely retained."""


@dataclass(frozen=True)
class ExactArtifactReceipt:
    content_sha256: str
    gzip_sha256: str
    relative_path: str
    byte_count: int


class ExactByteArtifactStore:
    """Content-addressed gzip storage that never parses or rewrites JSON."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def persist(self, body: bytes) -> ExactArtifactReceipt:
        if not isinstance(body, bytes):
            raise ArtifactError("artifact body must be exact bytes")
        if _SECRET_PATTERN.search(body):
            raise ArtifactError("artifact failed credential scan")
        content_hash = hashlib.sha256(body).hexdigest()
        relative = PurePosixPath("sha256", content_hash[:2], f"{content_hash}.json.gz")
        path = self.root.joinpath(*relative.parts)
        compressed = gzip.compress(body, compresslevel=9, mtime=0)
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    self._verify(path, compressed)
                else:
                    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                    try:
                        temporary.write_bytes(compressed)
                        os.replace(temporary, path)
                    finally:
                        if temporary.exists():
                            temporary.unlink()
                    self._verify(path, compressed)
                gzip_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise ArtifactError("exact artifact persistence failed") from exc
        return ExactArtifactReceipt(
            content_hash,
            gzip_hash,
            relative.as_posix(),
            len(body),
        )

    @staticmethod
    def _verify(path: Path, expected: bytes) -> None:
        stored = path.read_bytes()
        if stored != expected:
            raise ArtifactError("content-addressed artifact is not canonical")


@dataclass
class _Flight:
    ready: threading.Event
    result: StoredRoshRun | None = None
    error: RoshAnalysisError | None = None


class _DraftBoundaryError(ValueError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class RoshParityOrchestrator:
    """Reusable dependency-injected runner with request-scoped singleflight."""

    def __init__(
        self,
        *,
        transport: OfficialBatchTransport,
        artifacts: ExactByteArtifactStore,
        repository: RoshRunRepository,
        event_hook: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport
        self.artifacts = artifacts
        self.repository = repository
        self.event_hook = event_hook
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._flight_lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}

    def execute(
        self,
        analysis_input: RoshAnalysisInput | Mapping[str, Any],
        profile: RoshParityProfile | None = None,
        *,
        request_started_at: datetime | None = None,
    ) -> StoredRoshRun:
        try:
            started = _utc(request_started_at or self.clock())
        except ValueError:
            self._emit("pre_draft", "invalid_request", None, None)
            raise RoshAnalysisError("invalid_request") from None
        try:
            active_profile = profile or get_profile()
            validate_active_profile(active_profile)
        except Exception:
            self._emit("pre_draft", "profile_drift", None, None)
            raise RoshAnalysisError("profile_drift") from None
        try:
            plan = build_official_request_plan(
                analysis_input,
                active_profile,
                request_started_at=started,
            )
        except Exception:
            self._emit("pre_draft", "invalid_request", None, None)
            raise RoshAnalysisError("invalid_request") from None

        with self._flight_lock:
            flight = self._flights.get(plan.request_hash)
            leader = flight is None
            if leader:
                flight = _Flight(threading.Event())
                self._flights[plan.request_hash] = flight
        assert flight is not None
        if not leader:
            flight.ready.wait()
            if flight.error is not None:
                raise RoshAnalysisError(
                    flight.error.error_code,
                    run_id=flight.error.run_id,
                )
            assert flight.result is not None
            return flight.result

        try:
            flight.result = self._execute_plan(plan)
            return flight.result
        except RoshAnalysisError as exc:
            flight.error = exc
            raise
        finally:
            flight.ready.set()
            with self._flight_lock:
                self._flights.pop(plan.request_hash, None)

    def _execute_plan(self, plan: RoshRequestPlan) -> StoredRoshRun:
        mode = plan.analysis_input.mode
        expected_request = _request_body(plan)
        try:
            batch = self.transport.fetch_official_batch(plan)
        except StratzRoshError as exc:
            code = _transport_error_code(exc)
            if mode == "historical_match":
                self._emit("pre_draft", code, plan, None)
                raise RoshAnalysisError(code) from None
            draft = _explicit_draft(plan)
            request_manifest = self._persist_request_manifest(plan, expected_request)
            return self._raise_recorded_failure(
                plan,
                draft,
                code,
                "transport",
                request_manifest,
                (),
                hashlib.sha256(expected_request).hexdigest(),
                None,
                self.clock(),
            )
        except Exception:
            code = "upstream_unavailable"
            if mode == "historical_match":
                self._emit("pre_draft", code, plan, None)
                raise RoshAnalysisError(code) from None
            draft = _explicit_draft(plan)
            return self._raise_recorded_failure(
                plan,
                draft,
                code,
                "transport",
                self._persist_request_manifest(plan, expected_request),
                (),
                hashlib.sha256(expected_request).hexdigest(),
                None,
                self.clock(),
            )

        try:
            parsed = _validate_batch_evidence(plan, batch, expected_request)
        except ValueError:
            code = "profile_drift" if batch.request_body != expected_request else "upstream_unavailable"
            if mode == "historical_match":
                self._emit("pre_draft", code, plan, None)
                raise RoshAnalysisError(code) from None
            draft = _explicit_draft(plan)
            return self._raise_recorded_failure(
                plan,
                draft,
                code,
                "transport_evidence",
                self._request_manifest(plan, expected_request, None),
                (),
                hashlib.sha256(expected_request).hexdigest(),
                None,
                batch.collected_at,
            )

        if mode == "historical_match":
            try:
                draft = _historical_draft(plan, parsed)
            except _DraftBoundaryError as exc:
                self._emit("pre_draft", exc.error_code, plan, None)
                raise RoshAnalysisError(exc.error_code) from None
        else:
            draft = _explicit_draft(plan)

        request_hash = hashlib.sha256(batch.request_body).hexdigest()
        response_hash = hashlib.sha256(batch.response_body).hexdigest()
        request_manifest = self._request_manifest(plan, batch.request_body, None)
        response_manifest: tuple[Mapping[str, Any], ...] = ()
        try:
            request_receipt = self.artifacts.persist(batch.request_body)
            request_manifest = self._request_manifest(
                plan,
                batch.request_body,
                request_receipt,
            )
            response_receipt = self.artifacts.persist(batch.response_body)
            response_manifest = self._response_manifest(
                plan,
                batch.collected_at,
                request_receipt,
                response_receipt,
            )
        except Exception:
            return self._raise_recorded_failure(
                plan,
                draft,
                "source_data_incomplete",
                "artifact",
                request_manifest,
                response_manifest,
                request_hash,
                response_hash,
                batch.collected_at,
            )

        try:
            normalized = normalize_official_responses(plan, batch.responses)
            result = score_official_rosh(normalized, plan.profile)
        except ScoreError:
            return self._raise_recorded_failure(
                plan,
                draft,
                "source_data_incomplete",
                "normalizer_or_scorer",
                request_manifest,
                response_manifest,
                request_hash,
                response_hash,
                batch.collected_at,
            )
        except ValueError:
            return self._raise_recorded_failure(
                plan,
                draft,
                "profile_drift",
                "normalizer_or_scorer",
                request_manifest,
                response_manifest,
                request_hash,
                response_hash,
                batch.collected_at,
            )
        except Exception:
            return self._raise_recorded_failure(
                plan,
                draft,
                "source_data_incomplete",
                "normalizer_or_scorer",
                request_manifest,
                response_manifest,
                request_hash,
                response_hash,
                batch.collected_at,
            )
        return self._write_success(
            plan,
            draft,
            result,
            request_manifest,
            response_manifest,
            request_hash,
            response_hash,
            batch.collected_at,
        )

    def _write_success(
        self,
        plan: RoshRequestPlan,
        draft: Mapping[str, Any],
        result: OfficialRoshResult,
        request_manifest: Mapping[str, Any],
        response_manifest: Sequence[Mapping[str, Any]],
        request_artifact_hash: str,
        response_artifact_hash: str,
        collected_at: datetime,
    ) -> StoredRoshRun:
        draft_hash = _hash_json(draft)
        identity = _analysis_identity(plan, draft_hash)
        evidence_hash = _hash_json(
            {
                "schema": "rosh-analysis-evidence/v1",
                "analysis_identity": identity,
                "request_artifact_hash": request_artifact_hash,
                "response_artifact_hash": response_artifact_hash,
                "result_hash": result.result_hash,
                "status": "succeeded",
            }
        )
        try:
            existing = self.repository.get_by_evidence_hash(evidence_hash)
        except Exception:
            self._emit("repository", "source_data_incomplete", plan, None)
            raise RoshAnalysisError("source_data_incomplete") from None
        if existing is not None:
            return existing
        run = _run_record(
            plan,
            draft,
            draft_hash,
            evidence_hash,
            "succeeded",
            request_manifest,
            response_manifest,
            collected_at,
            result=result,
        )
        heroes = tuple(
            RoshHeroScoreRecord(
                row.team_side,
                row.position_id,
                row.hero_id,
                row.raw_score,
                row.display_score,
                {
                    "position_base_diff": row.position_base_diff,
                    "same_team_synergy": row.same_team_synergy,
                    "opponent_matchup_synergy": row.opponent_matchup_synergy,
                },
            )
            for row in result.hero_scores
        )
        minutes = tuple(
            RoshMinutePointRecord(
                row.minute,
                row.raw_score,
                row.display_score,
                row.radiant_time_delta,
                row.dire_time_delta,
                row.synergy_delta,
                {
                    "rank_source_counts": dict(row.rank_source_counts),
                    "slots": [slot.projection() for slot in row.slots],
                },
            )
            for row in result.minute_points
        )
        try:
            stored = self.repository.write_succeeded(run, heroes, minutes)
        except Exception:
            self._emit("repository", "source_data_incomplete", plan, None)
            raise RoshAnalysisError("source_data_incomplete") from None
        self._emit("succeeded", None, plan, stored.run.run_id)
        return stored

    def _raise_recorded_failure(
        self,
        plan: RoshRequestPlan,
        draft: Mapping[str, Any],
        error_code: str,
        stage: str,
        request_manifest: Mapping[str, Any],
        response_manifest: Sequence[Mapping[str, Any]],
        request_artifact_hash: str,
        response_artifact_hash: str | None,
        collected_at: datetime,
    ) -> StoredRoshRun:
        draft_hash = _hash_json(draft)
        evidence_hash = _hash_json(
            {
                "schema": "rosh-analysis-evidence/v1",
                "analysis_identity": _analysis_identity(plan, draft_hash),
                "request_artifact_hash": request_artifact_hash,
                "response_artifact_hash": response_artifact_hash,
                "status": "failed",
                "error_code": error_code,
                "failure_stage": stage,
            }
        )
        try:
            existing = self.repository.get_by_evidence_hash(evidence_hash)
        except Exception:
            self._emit("repository", "source_data_incomplete", plan, None)
            raise RoshAnalysisError("source_data_incomplete") from None
        if existing is None:
            run = _run_record(
                plan,
                draft,
                draft_hash,
                evidence_hash,
                "failed",
                request_manifest,
                response_manifest,
                collected_at,
                error_code=error_code,
            )
            try:
                existing = self.repository.write_failed(run)
            except Exception:
                self._emit("repository", "source_data_incomplete", plan, None)
                raise RoshAnalysisError("source_data_incomplete") from None
        self._emit(stage, error_code, plan, existing.run.run_id)
        raise RoshAnalysisError(error_code, run_id=existing.run.run_id) from None

    def _persist_request_manifest(
        self,
        plan: RoshRequestPlan,
        request_body: bytes,
    ) -> Mapping[str, Any]:
        manifest = self._request_manifest(plan, request_body, None)
        try:
            receipt = self.artifacts.persist(request_body)
        except Exception:
            return manifest
        return self._request_manifest(plan, request_body, receipt)

    @staticmethod
    def _request_manifest(
        plan: RoshRequestPlan,
        request_body: bytes,
        receipt: ExactArtifactReceipt | None,
    ) -> Mapping[str, Any]:
        manifest: dict[str, Any] = {
            "schema": "rosh-request-manifest/v1",
            "request_hash": plan.request_hash,
            "request_body_sha256": hashlib.sha256(request_body).hexdigest(),
            "operations": [
                {
                    "index": operation.index,
                    "operation_name": operation.operation_name,
                    "query_sha256": operation.query_sha256,
                    "variables": _json_value(operation.variables),
                }
                for operation in plan.operations
            ],
        }
        if receipt is not None:
            manifest["request_artifact"] = _receipt_projection(receipt)
        return manifest

    @staticmethod
    def _response_manifest(
        plan: RoshRequestPlan,
        collected_at: datetime,
        request: ExactArtifactReceipt,
        response: ExactArtifactReceipt,
    ) -> tuple[Mapping[str, Any], ...]:
        timestamp = _timestamp(collected_at)
        return tuple(
            {
                "operation_name": operation.operation_name,
                "operation_index": operation.index,
                "request_artifact_hash": request.content_sha256,
                "response_artifact_hash": response.content_sha256,
                "collected_at": timestamp,
                "relative_path": response.relative_path,
                "request_relative_path": request.relative_path,
                "response_gzip_sha256": response.gzip_sha256,
            }
            for operation in plan.operations
        )

    def _emit(
        self,
        stage: str,
        error_code: str | None,
        plan: RoshRequestPlan | None,
        run_id: str | None,
    ) -> None:
        if self.event_hook is None:
            return
        event: dict[str, Any] = {
            "event": "rosh_analysis_succeeded" if error_code is None else "rosh_analysis_failed",
            "stage": stage,
            "error_code": error_code,
            "mode": None if plan is None else plan.analysis_input.mode,
            "profile_id": None if plan is None else plan.profile.rosh_profile_id,
            "request_hash_prefix": None if plan is None else plan.request_hash[:12],
            "run_id_prefix": None if run_id is None else run_id[:12],
        }
        try:
            self.event_hook(event)
        except Exception:
            return


def execute_rosh_analysis(
    analysis_input: RoshAnalysisInput | Mapping[str, Any],
    profile: RoshParityProfile | None = None,
    *,
    transport: OfficialBatchTransport,
    artifacts: ExactByteArtifactStore,
    repository: RoshRunRepository,
    request_started_at: datetime,
    event_hook: Callable[[Mapping[str, Any]], None] | None = None,
) -> StoredRoshRun:
    """One-shot convenience wrapper; reuse ``RoshParityOrchestrator`` for concurrency."""

    return RoshParityOrchestrator(
        transport=transport,
        artifacts=artifacts,
        repository=repository,
        event_hook=event_hook,
    ).execute(
        analysis_input,
        profile,
        request_started_at=request_started_at,
    )


def _validate_batch_evidence(
    plan: RoshRequestPlan,
    batch: OfficialRoshBatch,
    expected_request: bytes,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(batch, OfficialRoshBatch):
        raise ValueError("transport result is not an official batch")
    if batch.request_body != expected_request:
        raise ValueError("transport request evidence drift")
    try:
        parsed = json.loads(
            batch.response_body.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError):
        raise ValueError("transport response evidence is invalid") from None
    if not isinstance(parsed, list) or len(parsed) != len(plan.operations):
        raise ValueError("transport response evidence count drift")
    for item in parsed:
        if not isinstance(item, Mapping):
            raise ValueError("transport response item is invalid")
        errors = item.get("errors")
        if errors is not None and (
            not isinstance(errors, list) or bool(errors)
        ):
            raise ValueError("transport response contains GraphQL errors")
        if not isinstance(item.get("data"), Mapping):
            raise ValueError("transport response item has no data")
    if canonical_bytes(parsed) != canonical_bytes(batch.responses):
        raise ValueError("parsed response does not match exact bytes")
    return tuple(batch.responses)


def _historical_draft(
    plan: RoshRequestPlan,
    responses: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    try:
        data = responses[0]["data"]
        if not isinstance(data, Mapping):
            raise TypeError
        match = data.get("match")
        if match is None:
            raise _DraftBoundaryError("source_match_not_found")
        if not isinstance(match, Mapping):
            raise TypeError
        if match.get("id") != plan.analysis_input.match_id:
            raise _DraftBoundaryError("source_draft_mismatch")
        if match.get("endDateTime") != plan.analysis_input.date_time:
            raise _DraftBoundaryError("source_draft_mismatch")
        pick_bans = match.get("pickBans")
        players = match.get("players")
        if not _array(pick_bans) or not _array(players):
            raise TypeError
        picked: dict[int, str] = {}
        for row in pick_bans:
            if not isinstance(row, Mapping):
                raise TypeError
            if not isinstance(row.get("isPick"), bool) or not isinstance(row.get("isRadiant"), bool):
                raise TypeError
            if not row["isPick"]:
                continue
            hero_id = _positive_int(row.get("heroId"))
            if hero_id in picked:
                raise TypeError
            picked[hero_id] = "radiant" if row["isRadiant"] else "dire"
        sides: dict[str, list[dict[str, int]]] = {"radiant": [], "dire": []}
        seen: set[int] = set()
        for row in players:
            if not isinstance(row, Mapping):
                raise TypeError
            hero_id = _positive_int(row.get("heroId"))
            position = row.get("position")
            if hero_id in seen or hero_id not in picked or not isinstance(position, str):
                raise TypeError
            prefix = "POSITION_"
            if not position.startswith(prefix):
                raise TypeError
            position_id = _positive_int(int(position.removeprefix(prefix)))
            seen.add(hero_id)
            sides[picked[hero_id]].append(
                {"hero_id": hero_id, "position_id": position_id}
            )
        if seen != set(picked):
            raise TypeError
        for side in sides:
            sides[side].sort(key=lambda row: row["position_id"])
        validate_draft(sides["radiant"], sides["dire"])
        return sides
    except _DraftBoundaryError:
        raise
    except (KeyError, TypeError, ValueError, IndexError):
        raise _DraftBoundaryError("source_data_incomplete") from None


def _explicit_draft(plan: RoshRequestPlan) -> Mapping[str, Any]:
    draft = {
        "radiant": [
            {
                "hero_id": int(row.get("hero_id", row.get("heroId"))),
                "position_id": int(row.get("position_id", row.get("positionId"))),
            }
            for row in plan.analysis_input.radiant
        ],
        "dire": [
            {
                "hero_id": int(row.get("hero_id", row.get("heroId"))),
                "position_id": int(row.get("position_id", row.get("positionId"))),
            }
            for row in plan.analysis_input.dire
        ],
    }
    validate_draft(draft["radiant"], draft["dire"])
    return draft


def _request_body(plan: RoshRequestPlan) -> bytes:
    value = [
        {
            "operationName": operation.operation_name,
            "variables": _json_value(operation.variables),
            "query": operation.query,
        }
        for operation in plan.operations
    ]
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _analysis_identity(plan: RoshRequestPlan, draft_hash: str) -> Mapping[str, Any]:
    profile = plan.profile
    return {
        "schema": "rosh-analysis-identity/v1",
        "mode": plan.analysis_input.mode,
        "match_id": plan.analysis_input.match_id,
        "date_time": plan.analysis_input.date_time,
        "draft_hash": draft_hash,
        "request_hash": plan.request_hash,
        "profile": {
            "rosh_profile_id": profile.rosh_profile_id,
            "formula_version": profile.formula_version,
            "request_profile_hash": profile.request_profile_hash,
            "upstream_bundle_hash": profile.upstream_bundle_hash,
            "scorer_source_hash": profile.scorer_source_hash,
            "canonical_profile_hash": profile.canonical_profile_hash,
            "serialization_version": profile.serialization_version,
        },
    }


def _run_record(
    plan: RoshRequestPlan,
    draft: Mapping[str, Any],
    draft_hash: str,
    evidence_hash: str,
    status: str,
    request_manifest: Mapping[str, Any],
    response_manifest: Sequence[Mapping[str, Any]],
    collected_at: datetime,
    *,
    result: OfficialRoshResult | None = None,
    error_code: str | None = None,
) -> RoshRunRecord:
    profile = plan.profile
    run_id = _hash_json(
        {
            "schema": "rosh-analysis-run-id/v1",
            "evidence_hash": evidence_hash,
            "status": status,
        }
    )
    return RoshRunRecord(
        run_id=run_id,
        status=status,
        mode=plan.analysis_input.mode,
        match_id=plan.analysis_input.match_id,
        date_time=plan.analysis_input.date_time,
        draft_hash=draft_hash,
        draft=draft,
        rosh_profile_id=profile.rosh_profile_id,
        formula_version=profile.formula_version,
        request_profile_hash=profile.request_profile_hash,
        upstream_bundle_hash=profile.upstream_bundle_hash,
        scorer_source_hash=profile.scorer_source_hash,
        canonical_profile_hash=profile.canonical_profile_hash,
        serialization_version=profile.serialization_version,
        request_hash=plan.request_hash,
        request_manifest=request_manifest,
        response_manifest=tuple(response_manifest),
        evidence_hash=evidence_hash,
        collected_at=_timestamp(collected_at),
        radiant_team_score=None if result is None else result.radiant_team_score,
        dire_team_score=None if result is None else result.dire_team_score,
        relative_advantage=None if result is None else result.relative_advantage,
        error_code=error_code,
    )


def _transport_error_code(error: StratzRoshError) -> str:
    if error.category in {"http_429", "graphql_rate_limited"}:
        return "upstream_rate_limited"
    if error.category == "profile_drift":
        return "profile_drift"
    return "upstream_unavailable"


def _receipt_projection(receipt: ExactArtifactReceipt) -> Mapping[str, Any]:
    return {
        "content_sha256": receipt.content_sha256,
        "gzip_sha256": receipt.gzip_sha256,
        "relative_path": receipt.relative_path,
        "byte_count": receipt.byte_count,
    }


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON value")
    return value


def _array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError
    return value


__all__ = [
    "ArtifactError",
    "ExactArtifactReceipt",
    "ExactByteArtifactStore",
    "OfficialBatchTransport",
    "PUBLIC_ERROR_CODES",
    "RoshAnalysisError",
    "RoshParityOrchestrator",
    "execute_rosh_analysis",
]
