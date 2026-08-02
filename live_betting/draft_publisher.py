"""Publish immutable prospective draft curves from strict live draft anchors."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

import psutil
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from database.engine import build_engine, require_database_url
from database.session import DatabaseRow, PostgresSession

from event_intelligence.backtest import (
    HORIZONS,
    DraftDependencyLimitError,
    draft_dependency_fingerprint,
)
from event_intelligence.deployment import (
    DEPLOYMENT_VERSION,
    FrozenDraftDeployment,
    assert_draft_models_match_database,
    build_frozen_draft_deployment,
    load_bounded_prospective_history,
    split_calibration_samples,
)
from event_intelligence.draft_artifacts import (
    CalibrationSample,
    DraftCalibrationArtifact,
    assert_model_calibration_compatible,
    build_calibration_artifact,
    canonical_hash,
    canonical_json_bytes,
    load_calibration_artifact_json,
    load_model_artifact_json,
)
from event_intelligence.draft_features import (
    PURE_FEATURE_SCHEMA,
    AvailabilityMode,
    DerivedFactProvenance,
    DraftMapEvidence,
    DraftPlayer,
    DraftTarget,
    DraftTeam,
    ExpectedRoleAssignment,
    build_draft_feature_artifact,
)
from event_intelligence.draft_model import (
    DraftModelArtifact,
    FeatureSchema,
    PredictionStatus,
    predict_draft,
)
from event_intelligence.models import RolePurpose
from event_intelligence.roles import RoleSource
from .draft_evidence import (
    draft_dependency_snapshot_reason,
    prospective_outcome_authority,
)
from .health import record_health
from .storage import LiveBettingStore
from .strict_eligibility import StrictLiveMapMapping, query_strict_live_eligibility
from .vision import VisionObservation
from .vision_frame_registry import verify_registered_vision_frame


PUBLISHER_VERSION = "prospective-draft-publisher-v2"
PUBLISHER_COMPONENT = "draft_publisher_worker"
LINEUP_ROLE_VERSION = "live-unknown-role-v1"
PURE_MODEL_FEATURE_SCHEMA_HASH = FeatureSchema.from_names(
    PURE_FEATURE_SCHEMA
).schema_hash
HISTORY_LOAD_TIMEOUT_SECONDS = 120.0
MAX_RUNTIME_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_ARTIFACT_ROWS = 10_000
MAX_RUNTIME_LINEAGE_CHANGES = 10_000
MAX_RUNTIME_HISTORY_ROWS = 100_000
MAX_RUNTIME_HISTORY_BYTES = 128 * 1024 * 1024
MAX_RUNTIME_HISTORY_VALUE_BYTES = 8 * 1024 * 1024
SAFE_FAILURE_MESSAGE_MAX_LENGTH = 240
_SQL_DIAGNOSTIC_RE = re.compile(
    r"(?:\bselect\b.+?\bfrom\b|\binsert\s+into\b|\bupdate\s+\S+\s+set\b|"
    r"\bdelete\s+from\b|\bcreate\s+table\b|\balter\s+table\b|"
    r"\bdrop\s+table\b|\bpragma\b|\bwith\s+recursive\b)",
    re.IGNORECASE | re.DOTALL,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_QUOTED_PATH_RE = re.compile(
    r'"(?:[A-Za-z]:[\\/]|\\\\|/)[^"\r\n]*"'
    r"|'(?:[A-Za-z]:[\\/]|\\\\|/)[^'\r\n]*'"
)
_WINDOWS_OR_UNC_SPACED_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)"
    r"[^<>|?*:\"'\r\n,;]*?"
    r"(?:\.[A-Za-z0-9][A-Za-z0-9_-]{0,15})+"
    r"(?=$|[\s,;:)\]}])"
)
_POSIX_SPACED_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.])/"
    r"(?:[^/\"'\r\n,;]+/)+"
    r"[^/\"'\r\n,;]*?"
    r"(?:\.[A-Za-z0-9][A-Za-z0-9_-]{0,15})+"
    r"(?=$|[\s,;:)\]}])"
)
_PATH_ERROR_TRAILER_PATTERN = (
    r"(?:was\s+not\s+found|not\s+found|does\s+not\s+exist|"
    r"permission\s+denied|access\s+denied|failed|missing|denied|"
    r"errno(?:\s+\d+)?|because|at\s+line)"
)
_WINDOWS_OR_UNC_BOUNDED_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)"
    r"(?=[^<>|?*:\"'\r\n,;]*\s)"
    r"[^<>|?*:\"'\r\n,;]*?"
    r"(?=$|[,;)\]}]|\s+" + _PATH_ERROR_TRAILER_PATTERN + r"\b)",
    re.IGNORECASE,
)
_POSIX_BOUNDED_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.])/"
    r"(?=[^\s/\"'\r\n,;]+/)"
    r"(?=[^\"'\r\n,;]*\s)"
    r"[^\"'\r\n,;]*?"
    r"(?=$|[,;)\]}]|\s+" + _PATH_ERROR_TRAILER_PATTERN + r"\b)",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'<>|,;)\]}]+"
)
_UNC_PATH_RE = re.compile(r"\\\\[^\s\"'<>|,;)\]}]+")
_POSIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.])/(?:[^\s\"'<>|,;)\]}]+)"
)
_AUTHORIZATION_KEY_PATTERN = (
    r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9]+[_-])*authorization"
    r"(?![A-Za-z0-9_-])"
)
_OPAQUE_CREDENTIAL_PATTERN = (
    r"(?:\"[^\"]*\"|'[^']*'|"
    r"(?=\S*(?:[0-9]|[^A-Za-z\s]))\S+|[A-Za-z]{16,})"
)
_AUTHORIZATION_EXPLICIT_RE = re.compile(
    _AUTHORIZATION_KEY_PATTERN
    + r"\s*[:=/]\s*(?:\"[^\"]+\"|'[^']+'|"
    + r"[A-Za-z][A-Za-z0-9._~+-]*\s+\S+).*$",
    re.IGNORECASE,
)
_AUTHORIZATION_SPACED_RE = re.compile(
    _AUTHORIZATION_KEY_PATTERN
    + r"\s+[A-Za-z][A-Za-z0-9._~+-]*\s+"
    + _OPAQUE_CREDENTIAL_PATTERN
    + r".*$",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_CREDENTIAL_KEY_PATTERN = (
    r"(?:(?:[A-Za-z0-9]+[_-])*(?:password|passwd|secret|token|api[_-]?key)"
    r"|smtp[_-]?code)"
)
_CREDENTIAL_EXPLICIT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    + _CREDENTIAL_KEY_PATTERN
    + r"(?![A-Za-z0-9_-])\s*[:=/]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_PREFIXED_CREDENTIAL_SPACED_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*[_-])"
    + _CREDENTIAL_KEY_PATTERN
    + r"(?![A-Za-z0-9_-])\s+(?:\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_OPAQUE_CREDENTIAL_SPACED_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    + _CREDENTIAL_KEY_PATTERN
    + r"(?![A-Za-z0-9_-])\s+"
    + _OPAQUE_CREDENTIAL_PATTERN,
    re.IGNORECASE,
)


class _FrozenDeploymentLineageError(ValueError):
    pass


class _HistoryLoadTimeoutError(TimeoutError):
    pass


class _HistoryLoadLimitError(RuntimeError):
    pass


def _safe_offline_rebuild_failure(error: Exception) -> dict[str, object]:
    exception_type = type(error).__name__
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", exception_type) is None:
        exception_type = "Exception"
    message = " ".join(str(error).split())
    if _SQL_DIAGNOSTIC_RE.search(message) is not None:
        message = "sensitive SQL diagnostic removed"
    else:
        message = _URL_RE.sub("[url]", message)
        message = _QUOTED_PATH_RE.sub("[path]", message)
        message = _WINDOWS_OR_UNC_SPACED_PATH_RE.sub("[path]", message)
        message = _POSIX_SPACED_PATH_RE.sub("[path]", message)
        message = _WINDOWS_OR_UNC_BOUNDED_PATH_RE.sub("[path]", message)
        message = _POSIX_BOUNDED_PATH_RE.sub("[path]", message)
        message = _WINDOWS_PATH_RE.sub("[path]", message)
        message = _UNC_PATH_RE.sub("[path]", message)
        message = _POSIX_PATH_RE.sub("[path]", message)
        message = _AUTHORIZATION_EXPLICIT_RE.sub("[credential]", message)
        message = _AUTHORIZATION_SPACED_RE.sub("[credential]", message)
        message = _BEARER_RE.sub("[credential]", message)
        message = _CREDENTIAL_EXPLICIT_RE.sub("[credential]", message)
        message = _PREFIXED_CREDENTIAL_SPACED_RE.sub("[credential]", message)
        message = _OPAQUE_CREDENTIAL_SPACED_RE.sub("[credential]", message)
    if not message:
        message = "offline artifact rebuild failed"
    truncated = len(message) > SAFE_FAILURE_MESSAGE_MAX_LENGTH
    if truncated:
        message = message[: SAFE_FAILURE_MESSAGE_MAX_LENGTH - 3] + "..."
    return {
        "exception_type": exception_type,
        "message": message,
        "message_truncated": truncated,
    }


def _publisher_process_generation(pid: int, created_at: float) -> str:
    return f"{pid}:{created_at:.9f}"


def _publisher_process_details() -> dict[str, object]:
    process = psutil.Process(os.getpid())
    pid = int(process.pid)
    created_at = float(process.create_time())
    return {
        "process_pid": pid,
        "process_created_at": created_at,
        "process_generation": _publisher_process_generation(pid, created_at),
    }


@contextmanager
def publisher_singleton_lock(database_url: str) -> Iterator[None]:
    """Hold one PostgreSQL advisory lock for the publisher process lifetime."""

    resolved_url = require_database_url(database_url)
    digest = hashlib.sha256(
        f"draft-publisher:{resolved_url}".encode("utf-8")
    ).hexdigest()
    lock_key = int(digest[:16], 16)
    if lock_key >= 2**63:
        lock_key -= 2**64
    engine = build_engine(resolved_url)
    connection = engine.connect()
    try:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            ).scalar_one()
        )
        if not acquired:
            raise RuntimeError("draft publisher is already running for this database")
        yield
    finally:
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": lock_key},
            )
        finally:
            connection.close()
            engine.dispose()


@dataclass(frozen=True)
class DraftAnchor:
    raybet_match_id: str
    map_number: int
    draft_hash: str
    radiant_heroes: tuple[int, ...]
    dire_heroes: tuple[int, ...]
    radiant_team_side: str
    anchored_at: datetime
    source_frame_ref: str
    team_side_anchored_at: datetime
    team_side_source_frame_ref: str


@dataclass(frozen=True)
class PublicationResult:
    status: str
    raybet_match_id: str
    map_number: int
    curve_key: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PublisherCycleReport:
    deployment_key: str
    candidates: int
    inserted: int
    unchanged: int
    skipped: int
    outcomes_inserted: int
    results: tuple[PublicationResult, ...]


@dataclass(frozen=True)
class ProspectiveHistorySnapshot:
    dependency_revision: int
    dependency_fingerprint: str
    maps: tuple[DraftMapEvidence, ...]

    def __post_init__(self) -> None:
        if self.dependency_revision < 1:
            raise ValueError("history dependency revision must be positive")
        if (
            len(self.dependency_fingerprint) != 64
            or any(
                value not in "0123456789abcdef"
                for value in self.dependency_fingerprint
            )
        ):
            raise ValueError("history dependency fingerprint must be SHA-256")


_BOUND_RUNTIME_HISTORIES: weakref.WeakValueDictionary[
    int, ProspectiveHistorySnapshot
] = weakref.WeakValueDictionary()


def _bind_runtime_history(
    history: ProspectiveHistorySnapshot,
) -> ProspectiveHistorySnapshot:
    _BOUND_RUNTIME_HISTORIES[id(history)] = history
    return history


def _runtime_history_is_bound(history: ProspectiveHistorySnapshot) -> bool:
    return _BOUND_RUNTIME_HISTORIES.get(id(history)) is history


def _parse_utc(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _json_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _deployment_identity(
    *,
    training_cutoff: datetime,
    dependency_fingerprint: str,
    dependency_revision: int,
    models: Sequence[DraftModelArtifact],
    calibrations: Sequence[DraftCalibrationArtifact],
    evidence_mode: str,
) -> dict[str, object]:
    return {
        "deployment_version": DEPLOYMENT_VERSION,
        "training_cutoff": training_cutoff.isoformat(),
        "dependency_fingerprint": dependency_fingerprint,
        "dependency_revision": dependency_revision,
        "model_hashes": {
            str(row.horizon_minutes): row.model_hash for row in models
        },
        "calibration_hashes": {
            str(row.horizon_minutes): row.calibration_hash
            for row in calibrations
        },
        "evidence_mode": evidence_mode,
    }


def _deployment_training_reason(
    connection: PostgresSession,
    deployment: FrozenDraftDeployment,
) -> str | None:
    return draft_dependency_snapshot_reason(
        connection,
        expected_revision=deployment.dependency_revision,
        expected_fingerprint=deployment.dependency_fingerprint,
        cutoff=deployment.training_cutoff,
    )


def _expected_artifact_row(
    artifact: DraftModelArtifact,
) -> tuple[object, ...]:
    return (
        artifact.model_version,
        artifact.model_kind,
        artifact.horizon_minutes,
        artifact.training_cutoff.isoformat(),
        artifact.feature_schema_hash,
        artifact.training_input_hash,
        _json_text(artifact.to_payload()),
    )


def _expected_calibration_row(
    artifact: DraftCalibrationArtifact,
) -> tuple[object, ...]:
    return (
        artifact.model_hash,
        artifact.calibration_version,
        artifact.horizon_minutes,
        artifact.evidence_mode,
        artifact.support,
        _json_text(artifact.to_payload()),
    )


def persist_frozen_deployment(
    connection: PostgresSession,
    deployment: FrozenDraftDeployment,
    *,
    created_at: datetime,
) -> bool:
    """Publish a complete five-horizon bundle after rechecking source lineage."""

    created = _parse_utc(created_at, "deployment created_at")
    if created < deployment.training_cutoff:
        raise ValueError("deployment cannot be published before its training cutoff")
    if {row.horizon_minutes for row in deployment.models} != set(HORIZONS):
        raise ValueError("deployment must contain five model horizons")
    if {row.horizon_minutes for row in deployment.calibrations} != set(HORIZONS):
        raise ValueError("deployment must contain five calibration horizons")
    for model in deployment.models:
        assert_model_calibration_compatible(
            model, deployment.calibration(model.horizon_minutes)
        )
        if model.model_kind != "pure_draft":
            raise ValueError("live deployment only accepts pure_draft models")
        if (
            model.feature_names != tuple(sorted(PURE_FEATURE_SCHEMA))
            or model.feature_schema_hash != PURE_MODEL_FEATURE_SCHEMA_HASH
        ):
            raise ValueError("live deployment requires the exact pure feature schema")
    identity = _deployment_identity(
        training_cutoff=deployment.training_cutoff,
        dependency_fingerprint=deployment.dependency_fingerprint,
        dependency_revision=deployment.dependency_revision,
        models=deployment.models,
        calibrations=deployment.calibrations,
        evidence_mode=deployment.evidence_mode,
    )
    if canonical_hash(identity) != deployment.deployment_key:
        raise ValueError("deployment key does not match its artifacts")

    connection.begin()
    try:
        dependency_reason = _deployment_training_reason(connection, deployment)
        if dependency_reason is not None:
            raise RuntimeError(
                f"draft deployment training lineage is stale: {dependency_reason}"
            )
        assert_draft_models_match_database(
            connection,
            deployment.models,
            training_cutoff=deployment.training_cutoff,
        )

        model_created_at: dict[str, datetime] = {}
        for model in deployment.models:
            if model.training_cutoff != deployment.training_cutoff:
                raise ValueError("deployment model training cutoffs disagree")
            expected = _expected_artifact_row(model)
            existing = connection.execute(
                """SELECT model_version, model_kind, horizon_minutes,
                          training_cutoff, feature_schema_hash,
                          training_input_hash, artifact_json, created_at
                     FROM draft_model_artifacts WHERE model_hash=?""",
                (model.model_hash,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO draft_model_artifacts
                       (model_hash, model_version, model_kind, horizon_minutes,
                        training_cutoff, feature_schema_hash, training_input_hash,
                        artifact_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (model.model_hash, *expected, created.isoformat()),
                )
                model_created_at[model.model_hash] = created
            elif tuple(existing[:7]) != expected:
                raise ValueError(f"immutable model artifact conflict: {model.model_hash}")
            else:
                model_created_at[model.model_hash] = _parse_utc(
                    existing[7],
                    "model artifact created_at",
                )
            if model_created_at[model.model_hash] > created:
                raise ValueError("model artifact postdates deployment publication")

        calibration_created_at: dict[str, datetime] = {}
        for calibration in deployment.calibrations:
            samples = (*calibration.fit_samples, *calibration.evaluation_samples)
            if calibration.evidence_mode == "prospective" and any(
                row.observed_at < model_created_at[calibration.model_hash]
                for row in samples
            ):
                raise ValueError("prospective calibration predates its frozen model")
            expected = _expected_calibration_row(calibration)
            existing = connection.execute(
                """SELECT model_hash, calibration_version, horizon_minutes,
                          evidence_mode, support, artifact_json, created_at
                     FROM draft_calibration_artifacts WHERE calibration_hash=?""",
                (calibration.calibration_hash,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO draft_calibration_artifacts
                       (calibration_hash, model_hash, calibration_version,
                        horizon_minutes, evidence_mode, support, artifact_json,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        calibration.calibration_hash,
                        *expected,
                        created.isoformat(),
                    ),
                )
                calibration_created_at[calibration.calibration_hash] = created
            elif tuple(existing[:6]) != expected:
                raise ValueError(
                    "immutable calibration artifact conflict: "
                    f"{calibration.calibration_hash}"
                )
            else:
                calibration_created_at[calibration.calibration_hash] = _parse_utc(
                    existing[6],
                    "calibration artifact created_at",
                )
            artifact_created = calibration_created_at[calibration.calibration_hash]
            if artifact_created > created:
                raise ValueError("calibration artifact postdates deployment publication")
            if samples and max(row.settled_at for row in samples) > artifact_created:
                raise ValueError("calibration samples settle after artifact publication")

        model_hashes = identity["model_hashes"]
        calibration_hashes = identity["calibration_hashes"]
        expected_bundle = (
            _json_text(model_hashes),
            _json_text(calibration_hashes),
            deployment.training_cutoff.isoformat(),
            deployment.dependency_fingerprint,
            deployment.dependency_revision,
            deployment.evidence_mode,
        )
        existing_bundle = connection.execute(
            """SELECT model_hashes_json, calibration_hashes_json,
                      training_cutoff, dependency_fingerprint,
                      dependency_revision, evidence_mode
                 FROM draft_deployment_bundles WHERE deployment_key=?""",
            (deployment.deployment_key,),
        ).fetchone()
        inserted = existing_bundle is None
        if inserted:
            connection.execute(
                """INSERT INTO draft_deployment_bundles
                   (deployment_key, model_hashes_json, calibration_hashes_json,
                    training_cutoff, dependency_fingerprint,
                    dependency_revision, evidence_mode, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    deployment.deployment_key,
                    *expected_bundle,
                    created.isoformat(),
                ),
            )
        elif tuple(existing_bundle) != expected_bundle:
            raise ValueError(
                f"immutable deployment conflict: {deployment.deployment_key}"
            )
        connection.commit()
        return inserted
    except BaseException:
        connection.rollback()
        raise


def _hash_map(value: object, field: str) -> dict[int, str]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be JSON text")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def reject_constant(constant: str) -> None:
        raise ValueError(f"invalid JSON constant: {constant}")

    try:
        payload = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"{field} is invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be an object")
    result: dict[int, str] = {}
    for horizon in HORIZONS:
        digest = payload.get(str(horizon))
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"{field} lacks horizon {horizon}")
        result[horizon] = digest
    if set(payload) != {str(value) for value in HORIZONS}:
        raise ValueError(f"{field} contains unsupported horizons")
    return result


def _load_frozen_deployment_snapshot(
    connection: PostgresSession,
    deployment_key: str | None,
    *,
    verify_authoritative_corpus: bool = True,
    verify_dependency_lineage: bool = True,
    enforce_runtime_limits: bool = False,
) -> FrozenDraftDeployment | None:
    if deployment_key is None:
        row = connection.execute(
            """SELECT deployment_key, model_hashes_json,
                      calibration_hashes_json, training_cutoff,
                      dependency_fingerprint, dependency_revision,
                      evidence_mode, created_at
                 FROM draft_deployment_bundles
                ORDER BY live_text_timestamp_utc(created_at) DESC,
                         deployment_key DESC
                LIMIT 1"""
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT deployment_key, model_hashes_json,
                      calibration_hashes_json, training_cutoff,
                      dependency_fingerprint, dependency_revision,
                      evidence_mode, created_at
                 FROM draft_deployment_bundles
                WHERE deployment_key=?""",
            (deployment_key,),
        ).fetchone()
    if row is None:
        return None
    model_hashes = _hash_map(row[1], "model_hashes_json")
    calibration_hashes = _hash_map(row[2], "calibration_hashes_json")
    if enforce_runtime_limits:
        _assert_runtime_artifact_limits(
            connection,
            model_hashes=model_hashes,
            calibration_hashes=calibration_hashes,
        )
    models: list[DraftModelArtifact] = []
    calibrations: list[DraftCalibrationArtifact] = []
    model_created_at: dict[int, datetime] = {}
    calibration_created_at: dict[int, datetime] = {}
    for horizon in HORIZONS:
        model_row = connection.execute(
            """SELECT model_version, model_kind, horizon_minutes,
                      training_cutoff, feature_schema_hash,
                      training_input_hash, artifact_json, created_at
                   FROM draft_model_artifacts
                WHERE model_hash=? AND horizon_minutes=?""",
            (model_hashes[horizon], horizon),
        ).fetchone()
        calibration_row = connection.execute(
            """SELECT model_hash, calibration_version, horizon_minutes,
                      evidence_mode, support, artifact_json, created_at
                   FROM draft_calibration_artifacts
                WHERE calibration_hash=? AND model_hash=?
                  AND horizon_minutes=?""",
            (calibration_hashes[horizon], model_hashes[horizon], horizon),
        ).fetchone()
        if model_row is None or calibration_row is None:
            raise ValueError("deployment references a missing artifact")
        model = load_model_artifact_json(str(model_row[6]))
        calibration = load_calibration_artifact_json(str(calibration_row[5]))
        assert_model_calibration_compatible(model, calibration)
        if tuple(model_row[:7]) != _expected_artifact_row(model):
            raise ValueError("model artifact columns disagree with canonical JSON")
        if tuple(calibration_row[:6]) != _expected_calibration_row(calibration):
            raise ValueError(
                "calibration artifact columns disagree with canonical JSON"
            )
        models.append(model)
        calibrations.append(calibration)
        model_created_at[horizon] = _parse_utc(
            model_row[7], "model artifact created_at"
        )
        calibration_created_at[horizon] = _parse_utc(
            calibration_row[6], "calibration artifact created_at"
        )
    cutoff = _parse_utc(row[3], "deployment training_cutoff")
    dependency_fingerprint = str(row[4])
    revision = int(row[5])
    evidence_mode = str(row[6])
    identity = _deployment_identity(
        training_cutoff=cutoff,
        dependency_fingerprint=dependency_fingerprint,
        dependency_revision=revision,
        models=models,
        calibrations=calibrations,
        evidence_mode=evidence_mode,
    )
    deployment_key = str(row[0])
    if canonical_hash(identity) != deployment_key:
        raise ValueError("deployment bundle hash does not recompute")
    deployment = FrozenDraftDeployment(
        deployment_key=deployment_key,
        training_cutoff=cutoff,
        dependency_fingerprint=dependency_fingerprint,
        dependency_revision=revision,
        models=tuple(models),
        calibrations=tuple(calibrations),
    )
    if deployment.evidence_mode != evidence_mode:
        raise ValueError("deployment evidence mode disagrees with calibrations")
    bundle_created_at = _parse_utc(row[7], "deployment created_at")
    if bundle_created_at < cutoff:
        raise ValueError("deployment was created before its training cutoff")
    for horizon in HORIZONS:
        calibration = deployment.calibration(horizon)
        samples = (*calibration.fit_samples, *calibration.evaluation_samples)
        if not (
            cutoff
            <= model_created_at[horizon]
            <= calibration_created_at[horizon]
            <= bundle_created_at
        ):
            raise ValueError("deployment artifact chronology is invalid")
        if samples and max(sample.settled_at for sample in samples) > (
            calibration_created_at[horizon]
        ):
            raise ValueError("calibration evidence postdates its artifact")
        if calibration.evidence_mode == "prospective" and any(
            sample.observed_at < model_created_at[horizon] for sample in samples
        ):
            raise ValueError("prospective calibration predates its frozen model")
    if verify_dependency_lineage:
        dependency_reason = _deployment_training_reason(connection, deployment)
        if dependency_reason is not None:
            raise _FrozenDeploymentLineageError(
                f"draft deployment lineage is stale: {dependency_reason}"
            )
    if verify_authoritative_corpus:
        assert_draft_models_match_database(
            connection,
            deployment.models,
            training_cutoff=deployment.training_cutoff,
        )
    return deployment


def _validate_deployment_key(deployment_key: str) -> str:
    if (
        not isinstance(deployment_key, str)
        or len(deployment_key) != 64
        or any(character not in "0123456789abcdef" for character in deployment_key)
    ):
        raise ValueError("deployment_key must be a lowercase SHA-256 digest")
    return deployment_key


def _assert_runtime_artifact_limits(
    connection: PostgresSession,
    *,
    model_hashes: Mapping[int, str],
    calibration_hashes: Mapping[int, str],
) -> None:
    for horizon in HORIZONS:
        model = connection.execute(
            """SELECT octet_length(artifact_json)
                 FROM draft_model_artifacts
                WHERE model_hash=? AND horizon_minutes=?""",
            (model_hashes[horizon], horizon),
        ).fetchone()
        calibration = connection.execute(
            """SELECT octet_length(artifact_json), support
                 FROM draft_calibration_artifacts
                WHERE calibration_hash=? AND model_hash=?
                  AND horizon_minutes=?""",
            (calibration_hashes[horizon], model_hashes[horizon], horizon),
        ).fetchone()
        if model is None or calibration is None:
            raise ValueError("deployment references a missing artifact")
        if (
            model[0] is None
            or int(model[0]) > MAX_RUNTIME_ARTIFACT_BYTES
            or calibration[0] is None
            or int(calibration[0]) > MAX_RUNTIME_ARTIFACT_BYTES
        ):
            raise ValueError("deployment artifact exceeds runtime size limit")
        if int(calibration[1]) > MAX_RUNTIME_ARTIFACT_ROWS:
            raise ValueError("deployment calibration exceeds runtime row limit")


def _bounded_runtime_lineage_reason(
    connection: PostgresSession,
    deployment: FrozenDraftDeployment,
) -> tuple[int, str | None]:
    row = connection.execute(
        """SELECT dependency_revision FROM draft_lineage_revisions
            WHERE singleton=1"""
    ).fetchone()
    if row is None:
        return 0, "draft_dependency_revision_unavailable"
    current_revision = int(row[0])
    if current_revision < deployment.dependency_revision:
        return current_revision, "draft_dependency_revision_moved_backwards"
    delta = current_revision - deployment.dependency_revision
    if delta == 0:
        return current_revision, None
    if delta > MAX_RUNTIME_LINEAGE_CHANGES:
        return current_revision, "draft_dependency_change_limit_exceeded"
    changes = connection.execute(
        """SELECT COUNT(*), MIN(dependency_revision), MAX(dependency_revision),
                  MAX(CASE
                        WHEN affected_from_unix IS NULL
                          OR affected_from_unix<=? THEN 1 ELSE 0 END)
             FROM draft_lineage_changes
            WHERE dependency_revision>? AND dependency_revision<=?""",
        (
            int(deployment.training_cutoff.timestamp()),
            deployment.dependency_revision,
            current_revision,
        ),
    ).fetchone()
    if (
        changes is None
        or int(changes[0]) != delta
        or int(changes[1]) != deployment.dependency_revision + 1
        or int(changes[2]) != current_revision
    ):
        return current_revision, "draft_dependency_change_log_incomplete"
    if int(changes[3] or 0) == 1:
        return current_revision, "draft_dependencies_changed_before_cutoff"
    return current_revision, None


def _runtime_input_reasons(
    connection: PostgresSession,
    deployment: FrozenDraftDeployment,
    history: ProspectiveHistorySnapshot,
) -> tuple[str | None, str | None]:
    current_revision, deployment_reason = _bounded_runtime_lineage_reason(
        connection,
        deployment,
    )
    history_reason = (
        None
        if history.dependency_revision == current_revision
        else "draft_dependency_revision_changed"
    )
    return deployment_reason, history_reason


def _runtime_deployment_generation(
    connection: PostgresSession,
) -> tuple[int, int]:
    artifact = connection.execute(
        """SELECT artifact_revision FROM draft_deployment_revisions
            WHERE singleton=1"""
    ).fetchone()
    dependency = connection.execute(
        """SELECT dependency_revision FROM draft_lineage_revisions
            WHERE singleton=1"""
    ).fetchone()
    if artifact is None or dependency is None:
        raise _FrozenDeploymentLineageError(
            "draft deployment runtime generation is unavailable"
        )
    return int(artifact[0]), int(dependency[0])


def _runtime_pinned_deployment_generation(
    connection: PostgresSession,
) -> tuple[int, int]:
    artifact = connection.execute(
        """SELECT artifact_revision FROM draft_deployment_revisions
            WHERE singleton=1"""
    ).fetchone()
    dependency = connection.execute(
        """SELECT dependency_revision FROM draft_lineage_revisions
            WHERE singleton=1"""
    ).fetchone()
    if artifact is None or dependency is None:
        raise _FrozenDeploymentLineageError(
            "draft deployment runtime generation is unavailable"
        )
    return int(artifact[0]), int(dependency[0])


def load_frozen_deployment(
    connection: PostgresSession,
    *,
    deployment_key: str,
) -> FrozenDraftDeployment | None:
    """Load and replay one deployment from a consistent database snapshot."""

    deployment_key = _validate_deployment_key(deployment_key)
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.begin()
    try:
        deployment = _load_frozen_deployment_snapshot(connection, deployment_key)
        if owns_snapshot:
            connection.commit()
        return deployment
    except BaseException:
        if owns_snapshot and connection.in_transaction:
            connection.rollback()
        raise


def load_latest_frozen_deployment(
    connection: PostgresSession,
) -> FrozenDraftDeployment | None:
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.begin()
    try:
        deployment = _load_frozen_deployment_snapshot(connection, None)
        if owns_snapshot:
            connection.commit()
        return deployment
    except BaseException:
        if owns_snapshot and connection.in_transaction:
            connection.rollback()
        raise


def load_pinned_frozen_deployment(
    connection: PostgresSession,
    *,
    deployment_key: str,
) -> FrozenDraftDeployment | None:
    """Load one externally pinned runtime deployment with bounded DB work."""

    deployment_key = _validate_deployment_key(deployment_key)
    if connection.in_transaction:
        raise RuntimeError("pinned deployment load requires a fresh connection")
    connection.begin()
    try:
        loaded_generation = _runtime_pinned_deployment_generation(connection)
        deployment = _load_frozen_deployment_snapshot(
            connection,
            deployment_key,
            verify_authoritative_corpus=False,
            verify_dependency_lineage=False,
            enforce_runtime_limits=True,
        )
        if deployment is None:
            connection.commit()
            return None
        for model in deployment.models:
            if len(model.training_corpus) > MAX_RUNTIME_ARTIFACT_ROWS:
                raise ValueError("deployment model exceeds runtime row limit")
        loaded_revision, reason = _bounded_runtime_lineage_reason(
            connection,
            deployment,
        )
        if reason is not None:
            raise _FrozenDeploymentLineageError(
                f"draft deployment lineage is stale: {reason}"
            )
        if loaded_revision != loaded_generation[1]:
            raise _FrozenDeploymentLineageError(
                "draft deployment lineage changed during runtime load"
            )
        connection.commit()
        if _runtime_pinned_deployment_generation(connection) != loaded_generation:
            raise _FrozenDeploymentLineageError(
                "draft deployment generation changed during runtime load"
            )
        return deployment
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def _heroes(value: object, field: str) -> tuple[int, ...]:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{field} is invalid JSON") from error
    if (
        not isinstance(payload, list)
        or len(payload) != 5
        or any(type(hero) is not int or hero <= 0 for hero in payload)
        or len(set(payload)) != 5
    ):
        raise ValueError(f"{field} must contain five unique hero IDs")
    return tuple(payload)


def _anchor_from_row(row: DatabaseRow | Sequence[object]) -> DraftAnchor:
    radiant = _heroes(row[3], "radiant heroes")
    dire = _heroes(row[4], "dire heroes")
    if set(radiant) & set(dire):
        raise ValueError("draft anchor contains duplicate heroes")
    team_side = str(row[5])
    if team_side not in {"team_one", "team_two"}:
        raise ValueError("draft anchor lacks a trusted radiant team side")
    anchor = DraftAnchor(
        raybet_match_id=str(row[0]),
        map_number=int(row[1]),
        draft_hash=str(row[2]),
        radiant_heroes=radiant,
        dire_heroes=dire,
        radiant_team_side=team_side,
        anchored_at=_parse_utc(row[6], "anchor anchored_at"),
        source_frame_ref=str(row[7]),
        team_side_anchored_at=_parse_utc(row[8], "team_side_anchored_at"),
        team_side_source_frame_ref=str(row[9]),
    )
    expected_hash = hashlib.sha256(
        json.dumps(
            {"radiant": list(radiant), "dire": list(dire)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if anchor.draft_hash != expected_hash:
        raise ValueError("draft anchor hash does not match its heroes")
    if not anchor.source_frame_ref or not anchor.team_side_source_frame_ref:
        raise ValueError("draft anchor source frames are required")
    return anchor


def ready_draft_anchors(connection: PostgresSession) -> tuple[DraftAnchor, ...]:
    rows = connection.execute(
        """SELECT anchor.raybet_match_id, anchor.map_number,
                  anchor.draft_hash, anchor.radiant_hero_ids,
                  anchor.dire_hero_ids, anchor.radiant_team_side,
                  anchor.anchored_at, anchor.source_frame_ref,
                  anchor.team_side_anchored_at,
                  anchor.team_side_source_frame_ref
             FROM vision_draft_anchors AS anchor
            WHERE anchor.status='anchored'
              AND anchor.radiant_team_side IN ('team_one', 'team_two')
              AND anchor.team_side_anchored_at IS NOT NULL
              AND anchor.team_side_source_frame_ref IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM vision_draft_conflicts AS conflict
                   WHERE conflict.raybet_match_id=anchor.raybet_match_id
                     AND conflict.map_number=anchor.map_number
              )
            ORDER BY anchor.anchored_at, anchor.raybet_match_id,
                     anchor.map_number"""
    ).fetchall()
    anchors = []
    for row in rows:
        try:
            anchors.append(_anchor_from_row(row))
        except (TypeError, ValueError):
            continue
    return tuple(anchors)


def _synthetic_id(payload: Mapping[str, object], *, negative: bool = False) -> int:
    value = (1 << 61) + int(canonical_hash(payload)[:15], 16) + 1
    return -value if negative else value


def _unknown_player(
    *,
    raybet_match_id: str,
    map_number: int,
    team_id: int,
    side: str,
    index: int,
    hero_id: int,
    cutoff: datetime,
) -> DraftPlayer:
    role_hash = canonical_hash(
        {
            "version": LINEUP_ROLE_VERSION,
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "team_id": team_id,
            "side": side,
            "index": index,
            "hero_id": hero_id,
            "status": "player_identity_not_observed",
        }
    )
    return DraftPlayer(
        player_id=_synthetic_id(
            {
                "raybet_match_id": raybet_match_id,
                "map_number": map_number,
                "team_id": team_id,
                "side": side,
                "index": index,
            },
            negative=True,
        ),
        hero_id=hero_id,
        expected_role=ExpectedRoleAssignment(
            purpose=RolePurpose.EXPECTED_POSITION,
            source=RoleSource.UNKNOWN,
            position=None,
            confidence=0.0,
            provenance=DerivedFactProvenance(
                cutoff=cutoff,
                first_usable_at=cutoff,
                input_hash=role_hash,
                version=LINEUP_ROLE_VERSION,
            ),
        ),
    )


def _latest_patch(connection: PostgresSession, cutoff: datetime) -> int | None:
    try:
        row = connection.execute(
            """SELECT match.patch
                 FROM matches AS match
                 JOIN match_ingest_status AS status
                   ON status.match_id=match.match_id
                 JOIN raw_source_artifacts AS artifact
                   ON artifact.artifact_id=status.latest_raw_artifact_id
                  AND artifact.match_id=match.match_id
                  AND artifact.content_hash=status.latest_raw_content_hash
                WHERE match.patch IS NOT NULL
                  AND match.start_time IS NOT NULL
                  AND match.start_time<=?
                  AND status.first_usable_at IS NOT NULL
                  AND live_text_timestamp_utc(status.first_usable_at)<=
                      CAST(? AS timestamptz)
                  AND artifact.first_usable_at IS NOT NULL
                  AND live_text_timestamp_utc(artifact.first_usable_at)<=
                      CAST(? AS timestamptz)
                ORDER BY match.start_time DESC, match.match_id DESC LIMIT 1""",
            (
                int(cutoff.timestamp()),
                cutoff.isoformat(),
                cutoff.isoformat(),
            ),
        ).fetchone()
    except SQLAlchemyError:
        return None
    return None if row is None else int(row[0])


def build_live_draft_target(
    connection: PostgresSession,
    anchor: DraftAnchor,
    mapping: StrictLiveMapMapping,
    cutoff: datetime,
) -> DraftTarget:
    radiant_team_id = (
        mapping.canonical_team_one_id
        if anchor.radiant_team_side == "team_one"
        else mapping.canonical_team_two_id
    )
    dire_team_id = (
        mapping.canonical_team_two_id
        if anchor.radiant_team_side == "team_one"
        else mapping.canonical_team_one_id
    )

    def team(team_id: int, side: str, heroes: tuple[int, ...]) -> DraftTeam:
        return DraftTeam(
            team_id=team_id,
            players=tuple(
                _unknown_player(
                    raybet_match_id=anchor.raybet_match_id,
                    map_number=anchor.map_number,
                    team_id=team_id,
                    side=side,
                    index=index,
                    hero_id=hero_id,
                    cutoff=cutoff,
                )
                for index, hero_id in enumerate(heroes)
            ),
        )

    return DraftTarget(
        match_id=_synthetic_id(
            {
                "source": "raybet-live",
                "raybet_match_id": anchor.raybet_match_id,
                "map_number": anchor.map_number,
                "strict_mapping_id": mapping.mapping_id,
            }
        ),
        prediction_cutoff=cutoff,
        event_id=mapping.event_id,
        patch=_latest_patch(connection, cutoff),
        radiant=team(radiant_team_id, "radiant", anchor.radiant_heroes),
        dire=team(dire_team_id, "dire", anchor.dire_heroes),
        availability_mode=AvailabilityMode.PROSPECTIVE,
        map_number=anchor.map_number,
    )


def _curve_key(
    *,
    anchor: DraftAnchor,
    mapping_id: int,
    deployment_key: str,
    input_snapshot_hash: str,
    feature_dependency_revision: int,
    feature_dependency_fingerprint: str,
) -> str:
    return canonical_hash(
        {
            "publisher_version": PUBLISHER_VERSION,
            "raybet_match_id": anchor.raybet_match_id,
            "map_number": anchor.map_number,
            "strict_mapping_id": mapping_id,
            "anchor_draft_hash": anchor.draft_hash,
            "radiant_team_side": anchor.radiant_team_side,
            "anchor_source_frame_ref": anchor.source_frame_ref,
            "anchor_anchored_at": anchor.anchored_at.isoformat(),
            "anchor_team_side_source_frame_ref": (
                anchor.team_side_source_frame_ref
            ),
            "anchor_team_side_anchored_at": (
                anchor.team_side_anchored_at.isoformat()
            ),
            "deployment_key": deployment_key,
            "input_snapshot_hash": input_snapshot_hash,
            "feature_dependency_revision": feature_dependency_revision,
            "feature_dependency_fingerprint": feature_dependency_fingerprint,
        }
    )


def _existing_curve(
    connection: PostgresSession,
    anchor: DraftAnchor,
    mapping_id: int,
    deployment_key: str,
    feature_dependency_revision: int,
    feature_dependency_fingerprint: str,
) -> str | None:
    row = connection.execute(
        """SELECT curve_key FROM prospective_draft_curves
            WHERE raybet_match_id=? AND map_number=? AND strict_mapping_id=?
              AND anchor_draft_hash=? AND radiant_team_side=?
              AND anchor_source_frame_ref=? AND anchor_anchored_at=?
              AND anchor_team_side_source_frame_ref=?
              AND anchor_team_side_anchored_at=?
              AND deployment_key=?
              AND feature_dependency_revision=?
              AND feature_dependency_fingerprint=?
            ORDER BY first_usable_at, curve_key LIMIT 1""",
        (
            anchor.raybet_match_id,
            anchor.map_number,
            mapping_id,
            anchor.draft_hash,
            anchor.radiant_team_side,
            anchor.source_frame_ref,
            anchor.anchored_at.isoformat(),
            anchor.team_side_source_frame_ref,
            anchor.team_side_anchored_at.isoformat(),
            deployment_key,
            feature_dependency_revision,
            feature_dependency_fingerprint,
        ),
    ).fetchone()
    return None if row is None else str(row[0])


def draft_anchor_frames_are_authoritative(
    connection: PostgresSession,
    anchor: DraftAnchor,
) -> bool:
    refs = {
        (anchor.anchored_at.isoformat(), anchor.source_frame_ref): False,
        (
            anchor.team_side_anchored_at.isoformat(),
            anchor.team_side_source_frame_ref,
        ): True,
    }
    for (captured_at, frame_ref), requires_team_side in refs.items():
        row = connection.execute(
            """SELECT observation.raybet_match_id, observation.map_number,
                      observation.captured_at,
                      observation.game_clock_seconds, observation.is_paused,
                      observation.radiant_hero_ids,
                      observation.dire_hero_ids,
                      observation.clock_confidence,
                      observation.draft_confidence,
                      observation.source_frame_ref,
                      observation.source_frame_sha256,
                      observation.source_frame_bytes,
                      observation.screen_state,
                      observation.radiant_team_side, observation.confirmed
                 FROM vision_observations AS observation
                WHERE observation.raybet_match_id=?
                  AND observation.map_number=?
                  AND observation.captured_at=?
                  AND observation.source_frame_ref=?
                  AND NOT EXISTS (
                      SELECT 1 FROM vision_observation_invalidations AS invalidation
                       WHERE invalidation.raybet_match_id=
                                 observation.raybet_match_id
                         AND invalidation.captured_at=observation.captured_at
                         AND invalidation.source_frame_ref=
                                 observation.source_frame_ref
                  )""",
            (
                anchor.raybet_match_id,
                anchor.map_number,
                captured_at,
                frame_ref,
            ),
        ).fetchone()
        if row is None or row[14] != 1:
            return False
        try:
            receipt = verify_registered_vision_frame(
                connection,
                str(row[9]),
                expected_sha256=str(row[10]),
                expected_bytes=int(row[11]),
            )
        except (RuntimeError, TypeError, ValueError):
            return False
        try:
            observation = VisionObservation(
                raybet_match_id=str(row[0]),
                map_number=row[1],
                captured_at=_parse_utc(row[2], "frame captured_at"),
                game_clock_seconds=row[3],
                is_paused=None if row[4] is None else bool(row[4]),
                radiant_hero_ids=_heroes(row[5], "frame radiant heroes"),
                dire_hero_ids=_heroes(row[6], "frame dire heroes"),
                clock_confidence=float(row[7]),
                draft_confidence=float(row[8]),
                source_frame_ref=str(row[9]),
                screen_state=str(row[12]),
                radiant_team_side=(
                    None if row[13] is None else str(row[13])
                ),
                source_frame_sha256=receipt.content_sha256,
                source_frame_bytes=receipt.byte_length,
            )
        except (TypeError, ValueError):
            return False
        if not observation.is_confirmed:
            return False
        if (
            observation.raybet_match_id != anchor.raybet_match_id
            or observation.map_number != anchor.map_number
            or observation.captured_at.isoformat() != captured_at
            or observation.source_frame_ref != frame_ref
            or observation.radiant_hero_ids != anchor.radiant_heroes
            or observation.dire_hero_ids != anchor.dire_heroes
            or observation.radiant_team_side
            not in {None, anchor.radiant_team_side}
        ):
            return False
        if (
            requires_team_side
            and observation.radiant_team_side != anchor.radiant_team_side
        ):
            return False
    return True


def publish_anchor_curve(
    connection: PostgresSession,
    *,
    anchor: DraftAnchor,
    deployment: FrozenDraftDeployment,
    history: ProspectiveHistorySnapshot,
    published_at: datetime,
) -> PublicationResult:
    if not _runtime_history_is_bound(history):
        return PublicationResult(
            "skipped",
            anchor.raybet_match_id,
            anchor.map_number,
            reason="draft_history:runtime_snapshot_untrusted",
        )
    now = _parse_utc(published_at, "published_at")
    prediction_cutoff = max(
        _parse_utc(anchor.anchored_at, "anchor anchored_at"),
        _parse_utc(
            anchor.team_side_anchored_at,
            "anchor team_side_anchored_at",
        ),
    )
    if now < prediction_cutoff:
        return PublicationResult(
            "skipped",
            anchor.raybet_match_id,
            anchor.map_number,
            reason="vision_anchor_from_future",
        )
    deployment_reason, history_reason = _runtime_input_reasons(
        connection,
        deployment,
        history,
    )
    deployment_row = connection.execute(
        """SELECT created_at FROM draft_deployment_bundles
            WHERE deployment_key=?""",
        (deployment.deployment_key,),
    ).fetchone()
    if deployment_row is None:
        deployment_reason = deployment_reason or "deployment_not_persisted"
    else:
        try:
            deployment_created_at = _parse_utc(
                deployment_row[0], "deployment created_at"
            )
        except ValueError:
            deployment_reason = deployment_reason or "deployment_created_at_invalid"
        else:
            if deployment_created_at > prediction_cutoff:
                deployment_reason = (
                    deployment_reason or "deployment_not_available_at_cutoff"
                )
    if deployment_reason is not None:
        return PublicationResult(
            "skipped",
            anchor.raybet_match_id,
            anchor.map_number,
            reason=f"draft_deployment:{deployment_reason}",
        )
    if history_reason is not None:
        return PublicationResult(
            "skipped",
            anchor.raybet_match_id,
            anchor.map_number,
            reason=f"draft_history:{history_reason}",
        )
    eligibility = query_strict_live_eligibility(
        connection,
        raybet_match_id=anchor.raybet_match_id,
        map_number=anchor.map_number,
        transport_observed_at=prediction_cutoff,
    )
    if not eligibility.eligible or eligibility.mapping is None:
        return PublicationResult(
            "skipped",
            anchor.raybet_match_id,
            anchor.map_number,
            reason=f"strict_mapping:{eligibility.reason}",
        )
    mapping = eligibility.mapping
    if not draft_anchor_frames_are_authoritative(connection, anchor):
        return PublicationResult(
            "skipped",
            anchor.raybet_match_id,
            anchor.map_number,
            reason="vision_anchor_evidence_invalid",
        )
    target = build_live_draft_target(
        connection, anchor, mapping, prediction_cutoff
    )
    snapshot, feature_artifact = build_draft_feature_artifact(
        target, history.maps
    )
    existing = _existing_curve(
        connection,
        anchor,
        mapping.mapping_id,
        deployment.deployment_key,
        history.dependency_revision,
        history.dependency_fingerprint,
    )
    if existing is not None:
        return PublicationResult(
            "unchanged", anchor.raybet_match_id, anchor.map_number, existing
        )
    predictions: list[dict[str, object]] = []
    for horizon in HORIZONS:
        model = deployment.model(horizon)
        calibration = deployment.calibration(horizon)
        prediction = predict_draft(model, snapshot.pure_values())
        if prediction.status is not PredictionStatus.PREDICTED or prediction.probability is None:
            raise ValueError(f"deployment horizon {horizon} could not predict")
        raw_probability = prediction.probability
        probability = calibration.apply(raw_probability)
        uncertainty = prediction.uncertainty
        if uncertainty is not None:
            uncertainty = min(
                0.5,
                uncertainty * max(1.0, abs(calibration.slope)),
            )
        passed = calibration.passes_live_gate and calibration.support >= 100
        predictions.append(
            {
                "horizon": horizon,
                "model": model,
                "calibration": calibration,
                "raw_probability": raw_probability,
                "probability": probability,
                "uncertainty": uncertainty,
                "raw_uncertainty": prediction.uncertainty,
                "model_input_hash": prediction.input_snapshot_hash,
                "passed": passed,
                "validation_reason": (
                    None
                    if passed
                    else (
                        "prospective_calibration_gate_not_passed:"
                        + (
                            ",".join(calibration.gate.reasons)
                            if calibration.evidence_mode == "prospective"
                            else "prospective_evidence_required"
                        )
                    )
                ),
            }
        )
    curve_key = _curve_key(
        anchor=anchor,
        mapping_id=mapping.mapping_id,
        deployment_key=deployment.deployment_key,
        input_snapshot_hash=snapshot.input_hash,
        feature_dependency_revision=history.dependency_revision,
        feature_dependency_fingerprint=history.dependency_fingerprint,
    )
    lineup_hash = canonical_hash(
        {
            "dire": list(anchor.dire_heroes),
            "radiant": list(anchor.radiant_heroes),
        }
    )
    scaling = snapshot.feature("scaling_40m_win_rate_diff").value or 0.0
    synergy = snapshot.feature("synergy_win_rate_diff").value or 0.0
    input_refs = [
        f"strict-mapping:{mapping.mapping_id}",
        f"vision-draft:{anchor.draft_hash}",
        f"vision-frame:{anchor.source_frame_ref}",
        f"draft-snapshot:{snapshot.input_hash}",
        f"draft-deployment:{deployment.deployment_key}",
        f"draft-history-revision:{history.dependency_revision}",
        f"draft-history-fingerprint:{history.dependency_fingerprint}",
    ]

    connection.begin()
    try:
        deployment_reason, history_reason = _runtime_input_reasons(
            connection,
            deployment,
            history,
        )
        if deployment_reason is not None or history_reason is not None:
            raise RuntimeError("draft dependencies changed before publication")
        deployment_row = connection.execute(
            """SELECT created_at FROM draft_deployment_bundles
                WHERE deployment_key=?""",
            (deployment.deployment_key,),
        ).fetchone()
        if (
            deployment_row is None
            or _parse_utc(deployment_row[0], "deployment created_at")
            > prediction_cutoff
        ):
            raise RuntimeError("draft deployment was unavailable at prediction cutoff")
        current = connection.execute(
            """SELECT draft_hash, radiant_team_side, status,
                      team_side_anchored_at, team_side_source_frame_ref,
                      anchored_at, source_frame_ref
                 FROM vision_draft_anchors
                WHERE raybet_match_id=? AND map_number=?""",
            (anchor.raybet_match_id, anchor.map_number),
        ).fetchone()
        if current is None or tuple(current) != (
            anchor.draft_hash,
            anchor.radiant_team_side,
            "anchored",
            anchor.team_side_anchored_at.isoformat(),
            anchor.team_side_source_frame_ref,
            anchor.anchored_at.isoformat(),
            anchor.source_frame_ref,
        ):
            raise RuntimeError("vision anchor changed before publication")
        if connection.execute(
            """SELECT 1 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=? LIMIT 1""",
            (anchor.raybet_match_id, anchor.map_number),
        ).fetchone() is not None:
            raise RuntimeError("vision draft conflict appeared before publication")
        if not draft_anchor_frames_are_authoritative(connection, anchor):
            raise RuntimeError("vision anchor evidence changed before publication")
        strict = query_strict_live_eligibility(
            connection,
            raybet_match_id=anchor.raybet_match_id,
            map_number=anchor.map_number,
            transport_observed_at=prediction_cutoff,
        )
        if (
            not strict.eligible
            or strict.mapping is None
            or strict.mapping.mapping_id != mapping.mapping_id
        ):
            raise RuntimeError("strict mapping changed before publication")
        duplicate = _existing_curve(
            connection,
            anchor,
            mapping.mapping_id,
            deployment.deployment_key,
            history.dependency_revision,
            history.dependency_fingerprint,
        )
        if duplicate is not None:
            connection.rollback()
            return PublicationResult(
                "unchanged", anchor.raybet_match_id, anchor.map_number, duplicate
            )
        connection.execute(
            """INSERT INTO prospective_draft_curves
               (curve_key, raybet_match_id, map_number, strict_mapping_id,
                lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                prediction_cutoff, first_usable_at, availability_mode,
                created_at, radiant_team_side, anchor_draft_hash,
                anchor_source_frame_ref, anchor_anchored_at, deployment_key,
                 anchor_team_side_source_frame_ref,
                 anchor_team_side_anchored_at,
                 target_snapshot_hash, feature_snapshot_json,
                 feature_dependency_fingerprint, feature_dependency_revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prospective', ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                curve_key,
                anchor.raybet_match_id,
                anchor.map_number,
                mapping.mapping_id,
                lineup_hash,
                _json_text(list(anchor.radiant_heroes)),
                _json_text(list(anchor.dire_heroes)),
                prediction_cutoff.isoformat(),
                now.isoformat(),
                now.isoformat(),
                anchor.radiant_team_side,
                anchor.draft_hash,
                anchor.source_frame_ref,
                anchor.anchored_at.isoformat(),
                deployment.deployment_key,
                anchor.team_side_source_frame_ref,
                anchor.team_side_anchored_at.isoformat(),
                snapshot.input_hash,
                _json_text(feature_artifact),
                history.dependency_fingerprint,
                history.dependency_revision,
            ),
        )
        for row in predictions:
            horizon = int(row["horizon"])
            model = row["model"]
            calibration = row["calibration"]
            assert isinstance(model, DraftModelArtifact)
            assert isinstance(calibration, DraftCalibrationArtifact)
            passed = bool(row["passed"])
            landmark_key = canonical_hash(
                {"curve_key": curve_key, "horizon_minutes": horizon}
            )
            refs = [
                *input_refs,
                f"draft-model:{model.model_hash}",
                f"draft-calibration:{calibration.calibration_hash}",
            ]
            connection.execute(
                """INSERT INTO prospective_draft_landmarks
                   (landmark_key, curve_key, horizon_minutes,
                    radiant_probability, scaling_edge, synergy_edge, quality,
                    validation_status, support, calibration_ref,
                    input_refs_json, uncertainty, validation_reason,
                    feature_hash, model_hash, calibration_hash,
                    global_calibration_passed, global_gate_ref, model_version,
                    model_kind, availability_mode, input_snapshot_hash,
                    created_at, raw_radiant_probability, deployment_key,
                    model_input_hash, raw_uncertainty)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, 'pure_draft', 'prospective', ?, ?, ?, ?, ?, ?)""",
                (
                    landmark_key,
                    curve_key,
                    horizon,
                    float(row["probability"]),
                    float(scaling),
                    float(synergy),
                    float(snapshot.pure_coverage),
                    "passed" if passed else "failed",
                    calibration.support,
                    f"draft-calibration:{calibration.calibration_hash}",
                    _json_text(refs),
                    row["uncertainty"],
                    row["validation_reason"],
                    model.feature_schema_hash,
                    model.model_hash,
                    calibration.calibration_hash,
                    int(passed),
                    f"draft-calibration:{calibration.calibration_hash}",
                    model.model_version,
                    snapshot.input_hash,
                    now.isoformat(),
                    float(row["raw_probability"]),
                    deployment.deployment_key,
                    row["model_input_hash"],
                    row["raw_uncertainty"],
                ),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return PublicationResult(
        "inserted", anchor.raybet_match_id, anchor.map_number, curve_key
    )


def append_prospective_outcomes(
    connection: PostgresSession,
    *,
    created_at: datetime,
) -> int:
    """Append exact two-source outcomes without modifying original predictions."""

    now = _parse_utc(created_at, "outcome created_at")
    rows = connection.execute(
        """SELECT curve.curve_key, curve.raybet_match_id, curve.map_number,
                  curve.strict_mapping_id, curve.radiant_team_side,
                  curve.first_usable_at, result.dota_match_id,
                  result.winner_side, result.evidence_ref, result.settled_at,
                  reconciliation.raybet_evidence_ref,
                  reconciliation.opendota_evidence_ref,
                  reconciliation.first_observed_at
             FROM prospective_draft_curves AS curve
              JOIN map_results AS result
                ON result.raybet_match_id=curve.raybet_match_id
               AND result.map_number=curve.map_number
               AND result.strict_mapping_id=curve.strict_mapping_id
              JOIN settlement_reconciliations AS reconciliation
                ON reconciliation.raybet_match_id=curve.raybet_match_id
               AND reconciliation.map_number=curve.map_number
               AND reconciliation.strict_mapping_id=curve.strict_mapping_id
              AND reconciliation.dota_match_id=result.dota_match_id
              AND reconciliation.status='confirmed'
              AND reconciliation.raybet_winner_side=result.winner_side
              AND reconciliation.opendota_winner_side=result.winner_side
             LEFT JOIN prospective_draft_outcomes AS outcome
               ON outcome.curve_key=curve.curve_key
            WHERE outcome.curve_key IS NULL
              AND live_text_timestamp_utc(curve.first_usable_at)<
                  live_text_timestamp_utc(result.settled_at)
            ORDER BY result.settled_at, curve.curve_key"""
    ).fetchall()
    inserted = 0
    for row in rows:
        first_usable = _parse_utc(row[5], "curve first_usable_at")
        eligibility = query_strict_live_eligibility(
            connection,
            raybet_match_id=str(row[1]),
            map_number=int(row[2]),
            transport_observed_at=first_usable,
        )
        if (
            not eligibility.eligible
            or eligibility.mapping is None
            or eligibility.mapping.mapping_id != int(row[3])
        ):
            continue
        if connection.execute(
            """SELECT 1 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=? LIMIT 1""",
            (str(row[1]), int(row[2])),
        ).fetchone() is not None:
            continue
        evidence_rows = connection.execute(
            """SELECT source, status, winner_side, evidence_ref, facts_json,
                      observed_at
                 FROM settlement_result_evidence
                WHERE raybet_match_id=? AND map_number=?
                  AND dota_match_id=?
                  AND evidence_ref IN (?, ?)
                ORDER BY source""",
            (
                str(row[1]),
                int(row[2]),
                int(row[6]),
                str(row[10]),
                str(row[11]),
            ),
        ).fetchall()
        if (
            len(evidence_rows) != 2
            or {str(value[0]) for value in evidence_rows} != {"raybet", "opendota"}
            or any(str(value[1]) != "confirmed" for value in evidence_rows)
            or any(str(value[2]) != str(row[7]) for value in evidence_rows)
        ):
            continue
        try:
            radiant_win, evidence_hash = prospective_outcome_authority(
                curve_key=str(row[0]),
                dota_match_id=int(row[6]),
                winner_side=str(row[7]),
                radiant_team_side=str(row[4]),
                map_result_ref=str(row[8]),
                reconciliation_observed_at=str(row[12]),
                evidence_rows=tuple(
                    (
                        str(value[0]),
                        str(value[1]),
                        str(value[2]),
                        str(value[3]),
                        str(value[4]),
                        str(value[5]),
                    )
                    for value in evidence_rows
                ),
            )
        except (TypeError, ValueError):
            continue
        try:
            availability_times = (
                _parse_utc(row[9], "map result settled_at"),
                _parse_utc(row[12], "reconciliation first_observed_at"),
                *(
                    _parse_utc(value[5], "settlement evidence observed_at")
                    for value in evidence_rows
                ),
            )
        except ValueError:
            continue
        if any(value > now for value in availability_times):
            continue
        first_usable_at = max(now, *availability_times)
        cursor = connection.execute(
            """INSERT INTO prospective_draft_outcomes
               (curve_key, strict_mapping_id, dota_match_id, radiant_win,
                winner_side, evidence_ref, evidence_hash, settled_at,
                first_usable_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                str(row[0]),
                int(row[3]),
                int(row[6]),
                radiant_win,
                str(row[7]),
                str(row[8]),
                evidence_hash,
                str(row[9]),
                first_usable_at.isoformat(),
                now.isoformat(),
            ),
        )
        inserted += int(cursor.rowcount == 1)
    connection.commit()
    return inserted


def _prospective_calibration_samples(
    connection: PostgresSession,
    *,
    model_hash: str,
    horizon_minutes: int,
) -> tuple[CalibrationSample, ...]:
    rows = connection.execute(
        """SELECT curve.curve_key, landmark.raw_radiant_probability,
                  outcome.radiant_win, curve.first_usable_at,
                  outcome.first_usable_at, outcome.settled_at,
                  curve.raybet_match_id, mapping.event_id,
                  curve.radiant_team_side,
                  outcome.winner_side, outcome.evidence_ref,
                  outcome.evidence_hash, outcome.dota_match_id,
                  result.evidence_ref, result.settled_at,
                  reconciliation.first_observed_at,
                  outcome.created_at,
                  raybet_evidence.source, raybet_evidence.status,
                  raybet_evidence.winner_side, raybet_evidence.evidence_ref,
                  raybet_evidence.facts_json, raybet_evidence.observed_at,
                  opendota_evidence.source, opendota_evidence.status,
                  opendota_evidence.winner_side,
                  opendota_evidence.evidence_ref,
                  opendota_evidence.facts_json,
                  opendota_evidence.observed_at
             FROM prospective_draft_curves AS curve
             JOIN prospective_draft_landmarks AS landmark
               ON landmark.curve_key=curve.curve_key
              AND landmark.horizon_minutes=?
              AND landmark.model_hash=?
             JOIN prospective_draft_outcomes AS outcome
               ON outcome.curve_key=curve.curve_key
              AND outcome.strict_mapping_id=curve.strict_mapping_id
              JOIN map_results AS result
                ON result.raybet_match_id=curve.raybet_match_id
               AND result.map_number=curve.map_number
               AND result.strict_mapping_id=curve.strict_mapping_id
               AND result.dota_match_id=outcome.dota_match_id
              AND result.winner_side=outcome.winner_side
              JOIN settlement_reconciliations AS reconciliation
                ON reconciliation.raybet_match_id=curve.raybet_match_id
               AND reconciliation.map_number=curve.map_number
               AND reconciliation.strict_mapping_id=curve.strict_mapping_id
               AND reconciliation.dota_match_id=outcome.dota_match_id
              AND reconciliation.status='confirmed'
              AND reconciliation.raybet_winner_side=outcome.winner_side
              AND reconciliation.opendota_winner_side=outcome.winner_side
             JOIN settlement_result_evidence AS raybet_evidence
               ON raybet_evidence.raybet_match_id=curve.raybet_match_id
              AND raybet_evidence.map_number=curve.map_number
              AND raybet_evidence.dota_match_id=outcome.dota_match_id
              AND raybet_evidence.source='raybet'
              AND raybet_evidence.status='confirmed'
              AND raybet_evidence.winner_side=outcome.winner_side
              AND raybet_evidence.evidence_ref=
                  reconciliation.raybet_evidence_ref
             JOIN settlement_result_evidence AS opendota_evidence
               ON opendota_evidence.raybet_match_id=curve.raybet_match_id
              AND opendota_evidence.map_number=curve.map_number
              AND opendota_evidence.dota_match_id=outcome.dota_match_id
              AND opendota_evidence.source='opendota'
              AND opendota_evidence.status='confirmed'
              AND opendota_evidence.winner_side=outcome.winner_side
              AND opendota_evidence.evidence_ref=
                  reconciliation.opendota_evidence_ref
             JOIN strict_live_map_mappings AS mapping
               ON mapping.mapping_id=curve.strict_mapping_id
              AND mapping.raybet_match_id=curve.raybet_match_id
              AND mapping.map_number=curve.map_number
             LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
               ON invalidation.mapping_id=mapping.mapping_id
            WHERE invalidation.mapping_id IS NULL
              AND landmark.raw_radiant_probability IS NOT NULL
              AND live_text_timestamp_utc(curve.first_usable_at)<
                  live_text_timestamp_utc(outcome.settled_at)
              AND NOT EXISTS (
                  SELECT 1 FROM vision_draft_conflicts AS conflict
                   WHERE conflict.raybet_match_id=curve.raybet_match_id
                     AND conflict.map_number=curve.map_number
              )
              AND NOT EXISTS (
                  SELECT 1
                    FROM prospective_draft_curves AS earlier
                    JOIN prospective_draft_landmarks AS earlier_landmark
                      ON earlier_landmark.curve_key=earlier.curve_key
                     AND earlier_landmark.horizon_minutes=?
                     AND earlier_landmark.model_hash=?
                    JOIN strict_live_map_mappings AS earlier_mapping
                      ON earlier_mapping.mapping_id=earlier.strict_mapping_id
                     AND earlier_mapping.raybet_match_id=earlier.raybet_match_id
                     AND earlier_mapping.map_number=earlier.map_number
                    LEFT JOIN strict_live_map_mapping_invalidations
                              AS earlier_invalidation
                      ON earlier_invalidation.mapping_id=earlier_mapping.mapping_id
                   WHERE earlier.raybet_match_id=curve.raybet_match_id
                     AND earlier.map_number=curve.map_number
                     AND earlier_invalidation.mapping_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM vision_draft_conflicts AS conflict
                          WHERE conflict.raybet_match_id=earlier.raybet_match_id
                            AND conflict.map_number=earlier.map_number
                     )
                     AND (
                          live_text_timestamp_utc(earlier.first_usable_at)<
                              live_text_timestamp_utc(curve.first_usable_at)
                         OR (
                              live_text_timestamp_utc(earlier.first_usable_at)=
                                  live_text_timestamp_utc(curve.first_usable_at)
                             AND earlier.curve_key<curve.curve_key
                         )
                     )
              )
            ORDER BY curve.first_usable_at, curve.curve_key""",
        (horizon_minutes, model_hash, horizon_minutes, model_hash),
    ).fetchall()
    from .profiles.draft_curve import prospective_curve_authority_matches

    samples = []
    for row in rows:
        if not prospective_curve_authority_matches(connection, str(row[0])):
            continue
        try:
            radiant_win, evidence_hash = prospective_outcome_authority(
                curve_key=str(row[0]),
                dota_match_id=int(row[12]),
                winner_side=str(row[9]),
                radiant_team_side=str(row[8]),
                map_result_ref=str(row[13]),
                reconciliation_observed_at=str(row[15]),
                evidence_rows=(
                    tuple(str(row[index]) for index in range(17, 23)),
                    tuple(str(row[index]) for index in range(23, 29)),
                ),
            )
            outcome_first_usable = _parse_utc(
                row[4], "prospective outcome first_usable_at"
            )
            outcome_settled = _parse_utc(row[5], "prospective outcome settled_at")
            result_settled = _parse_utc(row[14], "map result settled_at")
            expected_first_usable = max(
                result_settled,
                _parse_utc(row[15], "reconciliation first_observed_at"),
                _parse_utc(row[16], "prospective outcome created_at"),
                _parse_utc(row[22], "raybet evidence observed_at"),
                _parse_utc(row[28], "opendota evidence observed_at"),
            )
        except (TypeError, ValueError):
            continue
        if (
            int(row[2]) != radiant_win
            or str(row[10]) != str(row[13])
            or str(row[11]) != evidence_hash
            or outcome_settled != result_settled
            or outcome_first_usable != expected_first_usable
        ):
            continue
        samples.append(
            CalibrationSample(
                sample_id=f"{row[0]}:{horizon_minutes}",
                probability=float(row[1]),
                outcome=radiant_win,
                observed_at=_parse_utc(row[3], "curve first_usable_at"),
                settled_at=outcome_first_usable,
                cluster_id=f"raybet:{row[6]}",
                event_id=str(row[7]),
            )
        )
    return tuple(samples)


def build_prospective_calibration_deployment(
    connection: PostgresSession,
    current: FrozenDraftDeployment,
) -> FrozenDraftDeployment | None:
    """Recalibrate one frozen model family from exact prospective outcomes."""

    if _deployment_training_reason(connection, current) is not None:
        return None
    samples_by_horizon = {
        horizon: _prospective_calibration_samples(
            connection,
            model_hash=current.model(horizon).model_hash,
            horizon_minutes=horizon,
        )
        for horizon in HORIZONS
    }
    if not any(samples_by_horizon.values()):
        return None
    calibrations = []
    for horizon in HORIZONS:
        fit, evaluation = split_calibration_samples(samples_by_horizon[horizon])
        calibrations.append(
            build_calibration_artifact(
                current.model(horizon),
                evidence_mode="prospective",
                source_ref="prospective-draft-outcomes-v1",
                fit_samples=fit,
                evaluation_samples=evaluation,
            )
        )
    if _deployment_training_reason(connection, current) is not None:
        return None
    identity = _deployment_identity(
        training_cutoff=current.training_cutoff,
        dependency_fingerprint=current.dependency_fingerprint,
        dependency_revision=current.dependency_revision,
        models=current.models,
        calibrations=calibrations,
        evidence_mode="prospective",
    )
    candidate = FrozenDraftDeployment(
        deployment_key=canonical_hash(identity),
        training_cutoff=current.training_cutoff,
        dependency_fingerprint=current.dependency_fingerprint,
        dependency_revision=current.dependency_revision,
        models=current.models,
        calibrations=tuple(calibrations),
    )
    return None if candidate.deployment_key == current.deployment_key else candidate


def publish_cycle(
    connection: PostgresSession,
    *,
    deployment: FrozenDraftDeployment,
    history: ProspectiveHistorySnapshot,
    now: datetime,
) -> PublisherCycleReport:
    anchors = ready_draft_anchors(connection)
    results = tuple(
        publish_anchor_curve(
            connection,
            anchor=anchor,
            deployment=deployment,
            history=history,
            published_at=now,
        )
        for anchor in anchors
    )
    outcomes = append_prospective_outcomes(connection, created_at=now)
    return PublisherCycleReport(
        deployment_key=deployment.deployment_key,
        candidates=len(anchors),
        inserted=sum(row.status == "inserted" for row in results),
        unchanged=sum(row.status == "unchanged" for row in results),
        skipped=sum(row.status == "skipped" for row in results),
        outcomes_inserted=outcomes,
        results=results,
    )


def _build_and_persist(database_url: str, now: datetime) -> FrozenDraftDeployment:
    with LiveBettingStore(database_url) as store:
        store.connection.begin()
        try:
            deployment = build_frozen_draft_deployment(
                store.connection,
                training_cutoff=now,
            )
        finally:
            store.connection.rollback()
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=datetime.now(timezone.utc),
        )
    return deployment


def audited_rebase_frozen_deployment(
    connection: PostgresSession,
    *,
    old_deployment_key: str,
    created_at: datetime,
) -> tuple[FrozenDraftDeployment, bool]:
    """Rebind an authoritatively replayed deployment to current lineage."""

    old_deployment_key = _validate_deployment_key(old_deployment_key)
    if connection.in_transaction:
        raise RuntimeError("audited deployment rebase requires a fresh connection")
    connection.begin()
    try:
        old = _load_frozen_deployment_snapshot(
            connection,
            old_deployment_key,
            verify_authoritative_corpus=True,
            verify_dependency_lineage=False,
        )
        if old is None:
            raise ValueError("old deployment key is not persisted")
        revision_row = connection.execute(
            """SELECT dependency_revision FROM draft_lineage_revisions
                WHERE singleton=1"""
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("draft dependency revision is unavailable")
        current_fingerprint = draft_dependency_fingerprint(connection)
        current_revision = int(revision_row[0])
        identity = _deployment_identity(
            training_cutoff=old.training_cutoff,
            dependency_fingerprint=current_fingerprint,
            dependency_revision=current_revision,
            models=old.models,
            calibrations=old.calibrations,
            evidence_mode=old.evidence_mode,
        )
        rebased = FrozenDraftDeployment(
            deployment_key=canonical_hash(identity),
            training_cutoff=old.training_cutoff,
            dependency_fingerprint=current_fingerprint,
            dependency_revision=current_revision,
            models=old.models,
            calibrations=old.calibrations,
        )
        connection.rollback()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    inserted = persist_frozen_deployment(
        connection,
        rebased,
        created_at=created_at,
    )
    return rebased, inserted


def _load_history(
    database_url: str,
    *,
    timeout_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProspectiveHistorySnapshot:
    store = LiveBettingStore(database_url)
    reader = store.connection
    deadline = None
    timed_out = False
    if timeout_seconds is not None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            store.close()
            raise ValueError("history load timeout must be positive")
        deadline = monotonic() + timeout_seconds

    def require_time_remaining() -> None:
        nonlocal timed_out
        if deadline is not None and monotonic() >= deadline:
            timed_out = True
            raise _HistoryLoadTimeoutError("history_load_timeout")

    try:
        reader.begin()
        if timeout_seconds is not None:
            reader.execute(
                "SELECT set_config('statement_timeout', ?, true)",
                (f"{max(1, int(timeout_seconds * 1000))}ms",),
            )
        loaded_generation = _runtime_deployment_generation(reader)
        revision_row = reader.execute(
            """SELECT dependency_revision FROM draft_lineage_revisions
                WHERE singleton=1"""
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("draft dependency revision is unavailable")
        require_time_remaining()
        fingerprint, history = load_bounded_prospective_history(
            reader,
            max_rows=MAX_RUNTIME_HISTORY_ROWS,
            max_bytes=MAX_RUNTIME_HISTORY_BYTES,
            max_value_bytes=MAX_RUNTIME_HISTORY_VALUE_BYTES,
        )
        require_time_remaining()
        history_snapshot = _bind_runtime_history(
            ProspectiveHistorySnapshot(
                dependency_revision=int(revision_row[0]),
                dependency_fingerprint=fingerprint,
                maps=history,
            )
        )
        if history_snapshot.dependency_revision != loaded_generation[1]:
            raise _FrozenDeploymentLineageError(
                "draft history revision changed during runtime load"
            )
        reader.rollback()
        if _runtime_deployment_generation(reader) != loaded_generation:
            raise _FrozenDeploymentLineageError(
                "draft history generation changed during runtime load"
            )
        return history_snapshot
    except DraftDependencyLimitError as error:
        raise _HistoryLoadLimitError("history_load_limit_exceeded") from error
    except DBAPIError as error:
        cause = getattr(error, "orig", error)
        sqlstate = getattr(cause, "sqlstate", None) or getattr(
            cause, "pgcode", None
        )
        if timed_out or sqlstate == "57014":
            raise _HistoryLoadTimeoutError("history_load_timeout") from error
        raise
    finally:
        try:
            if reader.in_transaction:
                reader.rollback()
        finally:
            store.close()


def _load_history_with_timeout(
    database_url: str,
    *,
    timeout_seconds: float,
) -> ProspectiveHistorySnapshot:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise ValueError("history load timeout must be positive")
    return _load_history(database_url, timeout_seconds=timeout_seconds)


def _refresh_dependency_inputs(
    database_url: str,
    deployment: FrozenDraftDeployment,
    history: ProspectiveHistorySnapshot,
    *,
    now: datetime,
) -> tuple[FrozenDraftDeployment, ProspectiveHistorySnapshot]:
    del now
    store = LiveBettingStore(database_url)
    reader = store.connection
    try:
        reader.begin()
        loaded_generation = _runtime_deployment_generation(reader)
        deployment_reason, history_reason = _runtime_input_reasons(
            reader,
            deployment,
            history,
        )
        reader.rollback()
        if deployment_reason is not None:
            raise _FrozenDeploymentLineageError(
                f"draft deployment lineage is stale: {deployment_reason}"
            )
        if history_reason is None:
            return deployment, history
        if history_reason != "draft_dependency_revision_changed":
            raise _FrozenDeploymentLineageError(
                f"draft history lineage is stale: {history_reason}"
            )
        refreshed = _load_history_with_timeout(
            database_url,
            timeout_seconds=HISTORY_LOAD_TIMEOUT_SECONDS,
        )
        if _runtime_deployment_generation(reader) != loaded_generation:
            raise _FrozenDeploymentLineageError(
                "draft history generation changed during runtime refresh"
            )
        reader.begin()
        deployment_reason, history_reason = _runtime_input_reasons(
            reader,
            deployment,
            refreshed,
        )
        reader.rollback()
    finally:
        if reader.in_transaction:
            reader.rollback()
        store.close()
    if deployment_reason is not None:
        raise _FrozenDeploymentLineageError(
            f"draft deployment lineage is stale: {deployment_reason}"
        )
    if history_reason is not None:
        raise _FrozenDeploymentLineageError(
            f"draft history lineage is stale: {history_reason}"
        )
    return deployment, refreshed


def _record_cycle_health(
    connection: PostgresSession,
    *,
    status: str,
    now: datetime,
    report: PublisherCycleReport | None = None,
    error: str | None = None,
    phase: str | None = None,
    failure: Mapping[str, object] | None = None,
    history_dependency_revision: int | None = None,
    history_refreshed: bool | None = None,
) -> None:
    details: dict[str, Any] = {
        "publisher_version": PUBLISHER_VERSION,
        **_publisher_process_details(),
    }
    if report is not None:
        details.update(
            {
                "deployment_key": report.deployment_key,
                "candidates": report.candidates,
                "inserted": report.inserted,
                "unchanged": report.unchanged,
                "skipped": report.skipped,
                "outcomes_inserted": report.outcomes_inserted,
            }
        )
    if phase is not None:
        details["phase"] = phase
    if failure is not None:
        details["failure"] = dict(failure)
    if history_dependency_revision is not None:
        details["history_dependency_revision"] = history_dependency_revision
    if history_refreshed is not None:
        details["history_refreshed"] = history_refreshed
    record_health(
        connection,
        PUBLISHER_COMPONENT,
        status,
        heartbeat_at=now,
        success_at=now if status == "healthy" else None,
        error_at=now if error else None,
        error=error,
        details=details,
    )


def _record_database_health(
    database_url: str,
    *,
    status: str,
    phase: str,
    error: str | None = None,
    failure: Mapping[str, object] | None = None,
) -> None:
    with LiveBettingStore(database_url) as store:
        _record_cycle_health(
            store.connection,
            status=status,
            now=datetime.now(timezone.utc),
            error=error,
            phase=phase,
            failure=failure,
        )


def _run_publisher_locked(
    database_url: str,
    *,
    once: bool,
    interval_seconds: float,
    rebuild_artifacts: bool,
    deployment_key: str | None = None,
    rebase_deployment_key: str | None = None,
    history_timeout_seconds: float = HISTORY_LOAD_TIMEOUT_SECONDS,
) -> int:
    if rebase_deployment_key is not None:
        if not once:
            return 2
        try:
            old_key = _validate_deployment_key(rebase_deployment_key)
            with LiveBettingStore(database_url) as store:
                deployment, inserted = audited_rebase_frozen_deployment(
                    store.connection,
                    old_deployment_key=old_key,
                    created_at=datetime.now(timezone.utc),
                )
        except Exception as error:
            failure = _safe_offline_rebuild_failure(error)
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": "audited_rebase_failed",
                        "failure": failure,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "ok",
                    "phase": "audited_rebase_complete",
                    "old_deployment_key": old_key,
                    "new_deployment_key": deployment.deployment_key,
                    "inserted": inserted,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    if rebuild_artifacts:
        if not once:
            _record_database_health(
                database_url,
                status="unhealthy",
                phase="offline_rebuild",
                error="offline_rebuild_requires_once",
            )
            return 2
        _record_database_health(
            database_url,
            status="starting",
            phase="offline_rebuild",
        )
        try:
            deployment = _build_and_persist(
                database_url, datetime.now(timezone.utc)
            )
        except Exception as error:
            failure = _safe_offline_rebuild_failure(error)
            _record_database_health(
                database_url,
                status="unhealthy",
                phase="offline_rebuild",
                error="offline_rebuild_failed",
                failure=failure,
            )
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": "offline_rebuild_failed",
                        "failure": failure,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
            return 2
        _record_database_health(
            database_url,
            status="healthy",
            phase="offline_rebuild_complete",
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "phase": "offline_rebuild_complete",
                    "deployment_key": deployment.deployment_key,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    if deployment_key is None:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_deployment",
            error="deployment_pin_missing",
        )
        return 2
    try:
        deployment_key = _validate_deployment_key(deployment_key)
    except ValueError:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_deployment",
            error="deployment_pin_invalid",
        )
        return 2
    _record_database_health(
        database_url,
        status="starting",
        phase="loading_deployment",
    )
    try:
        with LiveBettingStore(database_url) as store:
            deployment = load_pinned_frozen_deployment(
                store.connection,
                deployment_key=deployment_key,
            )
    except _FrozenDeploymentLineageError:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_deployment",
            error="frozen_deployment_lineage_invalid",
        )
        return 2
    except ValueError:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_deployment",
            error="frozen_deployment_invalid",
        )
        return 2
    except Exception:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_deployment",
            error="frozen_deployment_load_failed",
        )
        return 2
    if deployment is None:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_deployment",
            error="frozen_deployment_missing",
        )
        return 2
    _record_database_health(
        database_url,
        status="starting",
        phase="loading_history",
    )
    try:
        history = _load_history_with_timeout(
            database_url,
            timeout_seconds=history_timeout_seconds,
        )
    except _HistoryLoadLimitError:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_history",
            error="history_load_limit_exceeded",
        )
        return 2
    except _HistoryLoadTimeoutError:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_history",
            error="history_load_timeout",
        )
        return 2
    except Exception:
        _record_database_health(
            database_url,
            status="unhealthy",
            phase="loading_history",
            error="history_load_failed",
        )
        return 2
    while True:
        cycle_at = datetime.now(timezone.utc)
        try:
            previous_history = history
            deployment, history = _refresh_dependency_inputs(
                database_url,
                deployment,
                history,
                now=cycle_at,
            )
            history_refreshed = history is not previous_history
            with LiveBettingStore(database_url) as store:
                report = publish_cycle(
                    store.connection,
                    deployment=deployment,
                    history=history,
                    now=cycle_at,
                )
                _record_cycle_health(
                    store.connection,
                    status="healthy",
                    now=cycle_at,
                    report=report,
                    phase="healthy",
                    history_dependency_revision=history.dependency_revision,
                    history_refreshed=history_refreshed,
                )
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "deployment_key": report.deployment_key,
                        "candidates": report.candidates,
                        "inserted": report.inserted,
                        "unchanged": report.unchanged,
                        "skipped": report.skipped,
                        "outcomes_inserted": report.outcomes_inserted,
                        "history_dependency_revision": history.dependency_revision,
                        "history_refreshed": history_refreshed,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        except _FrozenDeploymentLineageError:
            _record_database_health(
                database_url,
                status="unhealthy",
                phase="loading_deployment",
                error="frozen_deployment_lineage_invalid",
            )
            return 2
        except Exception as error:
            message = f"{type(error).__name__}: {' '.join(str(error).split())[:500]}"
            with LiveBettingStore(database_url) as store:
                _record_cycle_health(
                    store.connection,
                    status="degraded",
                    now=cycle_at,
                    error=message,
                )
            if once:
                raise
        if once:
            return 0
        time.sleep(interval_seconds)


def run_publisher(
    database_url: str,
    *,
    once: bool,
    interval_seconds: float,
    rebuild_artifacts: bool,
    deployment_key: str | None = None,
    rebase_deployment_key: str | None = None,
) -> int:
    with publisher_singleton_lock(database_url):
        return _run_publisher_locked(
            database_url,
            once=once,
            interval_seconds=interval_seconds,
            rebuild_artifacts=rebuild_artifacts,
            deployment_key=deployment_key,
            rebase_deployment_key=rebase_deployment_key,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--rebuild-artifacts", action="store_true")
    parser.add_argument("--rebase-deployment-key")
    parser.add_argument("--deployment-key")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0.0:
        parser.error("--interval must be positive")
    if args.rebuild_artifacts and not args.once:
        parser.error("--rebuild-artifacts requires --once")
    if args.rebase_deployment_key is not None and not args.once:
        parser.error("--rebase-deployment-key requires --once")
    if args.rebuild_artifacts and args.rebase_deployment_key is not None:
        parser.error(
            "--rebuild-artifacts cannot be used with --rebase-deployment-key"
        )
    if (args.rebuild_artifacts or args.rebase_deployment_key is not None) and (
        args.deployment_key is not None
    ):
        parser.error("--deployment-key cannot be used with an offline mode")
    if args.rebase_deployment_key is not None:
        try:
            args.rebase_deployment_key = _validate_deployment_key(
                args.rebase_deployment_key
            )
        except ValueError:
            parser.error(
                "--rebase-deployment-key must be a lowercase SHA-256 digest"
            )
    if not args.rebuild_artifacts and args.rebase_deployment_key is None:
        try:
            args.deployment_key = _validate_deployment_key(args.deployment_key)
        except ValueError:
            parser.error(
                "--deployment-key is required and must be a lowercase SHA-256 digest"
            )
    database_url = require_database_url(args.database_url)
    with LiveBettingStore(database_url) as store:
        store.init_schema()
    return run_publisher(
        database_url,
        once=args.once,
        interval_seconds=float(args.interval),
        rebuild_artifacts=bool(args.rebuild_artifacts),
        deployment_key=args.deployment_key,
        rebase_deployment_key=args.rebase_deployment_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DraftAnchor",
    "PublicationResult",
    "PublisherCycleReport",
    "append_prospective_outcomes",
    "audited_rebase_frozen_deployment",
    "build_prospective_calibration_deployment",
    "load_frozen_deployment",
    "load_latest_frozen_deployment",
    "load_pinned_frozen_deployment",
    "persist_frozen_deployment",
    "publish_anchor_curve",
    "publish_cycle",
    "ready_draft_anchors",
]
