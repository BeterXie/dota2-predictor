"""Prospective-only, non-deployable R.O.S.H. shadow ledger and evaluation."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from database.session import PostgresSession
from event_intelligence.prospective_rosh_candidate import (
    ProspectiveRoshCandidate,
    prospective_rosh_profile,
    verify_prospective_rosh_candidate,
)
from event_intelligence.raw_archive import canonical_json_bytes
from live_betting.rosh_parity import (
    ExactArtifactReceipt,
    ExactByteArtifactStore,
)
from prematch.stratz_rosh import normalize_rosh_analysis, score_rosh_lineups


PROSPECTIVE_ROSH_SHADOW_VERSION = "prospective-rosh-shadow-v1"
REQUIRED_ROSH_OPERATIONS = (
    "heroes_meta_positions",
    "hero_stats_by_time_bracket",
    "synergy",
)
UTC = timezone.utc
_EPSILON = 1e-12
_REPLAY_TOLERANCE = 1e-9


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _probability(value: object, field: str) -> float:
    result = _finite(value, field)
    if not 0.0 < result < 1.0:
        raise ValueError(f"{field} must be strictly between zero and one")
    return result


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def _lineup(value: Sequence[int], field: str) -> tuple[int, ...]:
    result = tuple(value)
    if (
        len(result) != 5
        or any(type(hero_id) is not int or hero_id <= 0 for hero_id in result)
        or len(set(result)) != 5
    ):
        raise ValueError(f"{field} must contain five unique positive hero IDs")
    return result


@dataclass(frozen=True)
class ArtifactIdentity:
    operation: str
    content_sha256: str
    gzip_sha256: str
    relative_path: str
    byte_count: int

    def __post_init__(self) -> None:
        if self.operation not in REQUIRED_ROSH_OPERATIONS:
            raise ValueError("artifact operation is not part of the frozen profile")
        _digest(self.content_sha256, "content_sha256")
        _digest(self.gzip_sha256, "gzip_sha256")
        _positive_integer(self.byte_count, "byte_count")
        path = PurePosixPath(self.relative_path)
        if path.is_absolute() or ".." in path.parts or path.suffixes != [".json", ".gz"]:
            raise ValueError("artifact relative path is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "content_sha256": self.content_sha256,
            "gzip_sha256": self.gzip_sha256,
            "relative_path": self.relative_path,
            "byte_count": self.byte_count,
        }


def archive_exact_artifacts(
    store: ExactByteArtifactStore,
    bodies: Mapping[str, bytes],
) -> tuple[ArtifactIdentity, ...]:
    """Archive exactly one raw request or response for each frozen operation."""

    if not isinstance(store, ExactByteArtifactStore):
        raise ValueError("store must be an ExactByteArtifactStore")
    if set(bodies) != set(REQUIRED_ROSH_OPERATIONS):
        raise ValueError("artifact bundle must contain the three frozen operations")
    result: list[ArtifactIdentity] = []
    for operation in REQUIRED_ROSH_OPERATIONS:
        receipt: ExactArtifactReceipt = store.persist(bodies[operation])
        result.append(
            ArtifactIdentity(
                operation=operation,
                content_sha256=receipt.content_sha256,
                gzip_sha256=receipt.gzip_sha256,
                relative_path=receipt.relative_path,
                byte_count=receipt.byte_count,
            )
        )
    return tuple(result)


def _validate_manifest(
    values: Sequence[ArtifactIdentity],
    field: str,
) -> tuple[ArtifactIdentity, ...]:
    result = tuple(values)
    if tuple(value.operation for value in result) != REQUIRED_ROSH_OPERATIONS:
        raise ValueError(f"{field} does not match the frozen operation sequence")
    return result


def artifact_manifest_hash(values: Sequence[ArtifactIdentity]) -> str:
    manifest = _validate_manifest(values, "artifact manifest")
    return _hash([value.to_payload() for value in manifest])


def _read_artifact(root: Path, artifact: ArtifactIdentity) -> bytes:
    relative = PurePosixPath(artifact.relative_path)
    path = root.joinpath(*relative.parts)
    try:
        compressed = path.read_bytes()
        body = gzip.decompress(compressed)
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ValueError("archived R.O.S.H. artifact is unavailable") from error
    if hashlib.sha256(compressed).hexdigest() != artifact.gzip_sha256:
        raise ValueError("archived R.O.S.H. gzip hash mismatch")
    if (
        len(body) != artifact.byte_count
        or hashlib.sha256(body).hexdigest() != artifact.content_sha256
    ):
        raise ValueError("archived R.O.S.H. content hash mismatch")
    return body


def _verify_archived_requests(
    artifact_root: str | Path,
    request_artifacts: Sequence[ArtifactIdentity],
    *,
    radiant_heroes: Sequence[int],
    dire_heroes: Sequence[int],
    statistics_cutoff: datetime,
) -> None:
    heroes = (*_lineup(radiant_heroes, "radiant_heroes"), *_lineup(dire_heroes, "dire_heroes"))
    if len(set(heroes)) != 10:
        raise ValueError("R.O.S.H. request lineups must contain ten unique heroes")
    profile_operations = {
        str(row["name"]): str(row["query_sha256"])
        for row in prospective_rosh_profile()["operations"]
    }
    cutoff_epoch = int(_utc(statistics_cutoff, "statistics_cutoff").timestamp())
    for artifact in _validate_manifest(request_artifacts, "request artifacts"):
        try:
            payload = json.loads(_read_artifact(Path(artifact_root), artifact))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("archived R.O.S.H. request is invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise ValueError("archived R.O.S.H. request must be an object")
        query = payload.get("query")
        variables = payload.get("variables")
        if (
            not isinstance(query, str)
            or hashlib.sha256(query.encode("utf-8")).hexdigest()
            != profile_operations[artifact.operation]
            or not isinstance(variables, Mapping)
        ):
            raise ValueError("archived R.O.S.H. request profile drift")
        requested_heroes = variables.get("heroIds")
        if (
            not isinstance(requested_heroes, list)
            or len(requested_heroes) != 10
            or set(requested_heroes) != set(heroes)
        ):
            raise ValueError("archived R.O.S.H. request lineup mismatch")
        request_week = variables.get(
            "currentWeek" if artifact.operation == "synergy" else "week"
        )
        if (
            isinstance(request_week, bool)
            or not isinstance(request_week, int)
            or request_week > cutoff_epoch
        ):
            raise ValueError("archived R.O.S.H. request week follows statistics cutoff")


def replay_archived_pure_rosh(
    artifact_root: str | Path,
    response_artifacts: Sequence[ArtifactIdentity],
    *,
    radiant_heroes: Sequence[int],
    dire_heroes: Sequence[int],
) -> tuple[float, str]:
    """Recompute only the pure lineup score from exact archived responses."""

    radiant = _lineup(radiant_heroes, "radiant_heroes")
    dire = _lineup(dire_heroes, "dire_heroes")
    if set(radiant) & set(dire):
        raise ValueError("radiant and dire hero lineups must not overlap")
    responses: dict[str, Mapping[str, Any]] = {}
    for artifact in _validate_manifest(response_artifacts, "response artifacts"):
        try:
            payload = json.loads(_read_artifact(Path(artifact_root), artifact))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("archived R.O.S.H. response is invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise ValueError("archived R.O.S.H. response must be an object")
        responses[artifact.operation] = payload
    normalized = normalize_rosh_analysis(responses)
    result = score_rosh_lineups(radiant, dire, normalized)
    score = _finite(result.get("pure_lineup_score"), "pure_lineup_score")
    return score, _hash(normalized)


@dataclass(frozen=True)
class TeamRatingAuthority:
    prediction_id: int
    run_id: str
    prediction_cutoff: datetime
    probability: float
    rating_version: str
    artifact_version: str
    artifact_hash: str
    input_hash: str
    training_input_hash: str

    def __post_init__(self) -> None:
        _positive_integer(self.prediction_id, "prediction_id")
        _digest(self.run_id, "run_id")
        object.__setattr__(
            self,
            "prediction_cutoff",
            _utc(self.prediction_cutoff, "prediction_cutoff"),
        )
        object.__setattr__(self, "probability", _probability(self.probability, "P0"))
        _nonempty(self.rating_version, "rating_version")
        _nonempty(self.artifact_version, "artifact_version")
        _digest(self.artifact_hash, "artifact_hash")
        _digest(self.input_hash, "input_hash")
        _digest(self.training_input_hash, "training_input_hash")


@dataclass(frozen=True)
class ProspectiveRoshEvidence:
    profile_id: str
    profile_hash: str
    formula_version: str
    scorer_source_hash: str
    radiant_heroes: tuple[int, ...]
    dire_heroes: tuple[int, ...]
    pure_rosh_score: float
    normalized_statistics_hash: str
    request_artifacts: tuple[ArtifactIdentity, ...]
    response_artifacts: tuple[ArtifactIdentity, ...]
    request_manifest_hash: str
    response_manifest_hash: str
    statistics_cutoff: datetime
    available_at: datetime
    evidence_hash: str

    def to_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "profile_id": self.profile_id,
            "profile_hash": self.profile_hash,
            "formula_version": self.formula_version,
            "scorer_source_hash": self.scorer_source_hash,
            "radiant_heroes": list(self.radiant_heroes),
            "dire_heroes": list(self.dire_heroes),
            "expected_positions": [1, 2, 3, 4, 5],
            "pure_rosh_score": self.pure_rosh_score,
            "normalized_statistics_hash": self.normalized_statistics_hash,
            "request_artifacts": [
                value.to_payload() for value in self.request_artifacts
            ],
            "response_artifacts": [
                value.to_payload() for value in self.response_artifacts
            ],
            "request_manifest_hash": self.request_manifest_hash,
            "response_manifest_hash": self.response_manifest_hash,
            "statistics_cutoff": self.statistics_cutoff.isoformat(),
            "available_at": self.available_at.isoformat(),
        }
        if include_hash:
            payload["evidence_hash"] = self.evidence_hash
        return payload


def build_prospective_rosh_evidence(
    candidate: ProspectiveRoshCandidate,
    *,
    artifact_root: str | Path,
    radiant_heroes: Sequence[int],
    dire_heroes: Sequence[int],
    request_artifacts: Sequence[ArtifactIdentity],
    response_artifacts: Sequence[ArtifactIdentity],
    statistics_cutoff: datetime,
    available_at: datetime,
) -> ProspectiveRoshEvidence:
    verify_prospective_rosh_candidate(candidate)
    requests = _validate_manifest(request_artifacts, "request artifacts")
    responses = _validate_manifest(response_artifacts, "response artifacts")
    statistics = _utc(statistics_cutoff, "statistics_cutoff")
    available = _utc(available_at, "available_at")
    if statistics > available:
        raise ValueError("statistics_cutoff must not follow available_at")
    _verify_archived_requests(
        artifact_root,
        requests,
        radiant_heroes=radiant_heroes,
        dire_heroes=dire_heroes,
        statistics_cutoff=statistics,
    )
    score, normalized_hash = replay_archived_pure_rosh(
        artifact_root,
        responses,
        radiant_heroes=radiant_heroes,
        dire_heroes=dire_heroes,
    )
    profile = prospective_rosh_profile()
    evidence = ProspectiveRoshEvidence(
        profile_id=str(profile["profile_id"]),
        profile_hash=str(profile["profile_hash"]),
        formula_version=str(profile["formula_version"]),
        scorer_source_hash=str(profile["scorer_source_hash"]),
        radiant_heroes=_lineup(radiant_heroes, "radiant_heroes"),
        dire_heroes=_lineup(dire_heroes, "dire_heroes"),
        pure_rosh_score=score,
        normalized_statistics_hash=normalized_hash,
        request_artifacts=requests,
        response_artifacts=responses,
        request_manifest_hash=artifact_manifest_hash(requests),
        response_manifest_hash=artifact_manifest_hash(responses),
        statistics_cutoff=statistics,
        available_at=available,
        evidence_hash="",
    )
    return ProspectiveRoshEvidence(
        **{
            **evidence.__dict__,
            "evidence_hash": _hash(evidence.to_payload(include_hash=False)),
        }
    )


def verify_prospective_rosh_evidence(
    candidate: ProspectiveRoshCandidate,
    evidence: ProspectiveRoshEvidence,
) -> None:
    verify_prospective_rosh_candidate(candidate)
    profile = prospective_rosh_profile()
    if (
        evidence.profile_id != candidate.prospective_profile_id
        or evidence.profile_id != profile["profile_id"]
        or evidence.profile_hash != candidate.prospective_profile_hash
        or evidence.profile_hash != profile["profile_hash"]
        or evidence.formula_version != candidate.retrospective_formula_version
        or evidence.formula_version != profile["formula_version"]
        or evidence.scorer_source_hash != candidate.scorer_source_hash
        or evidence.scorer_source_hash != profile["scorer_source_hash"]
    ):
        raise ValueError("R.O.S.H. profile, formula, or scorer drift")
    _lineup(evidence.radiant_heroes, "radiant_heroes")
    _lineup(evidence.dire_heroes, "dire_heroes")
    _finite(evidence.pure_rosh_score, "pure_rosh_score")
    _digest(evidence.normalized_statistics_hash, "normalized_statistics_hash")
    if evidence.request_manifest_hash != artifact_manifest_hash(
        evidence.request_artifacts
    ):
        raise ValueError("R.O.S.H. request manifest hash mismatch")
    if evidence.response_manifest_hash != artifact_manifest_hash(
        evidence.response_artifacts
    ):
        raise ValueError("R.O.S.H. response manifest hash mismatch")
    if evidence.statistics_cutoff > evidence.available_at:
        raise ValueError("R.O.S.H. statistics cutoff follows availability")
    if evidence.evidence_hash != _hash(evidence.to_payload(include_hash=False)):
        raise ValueError("R.O.S.H. evidence content hash mismatch")


class ProspectiveRoshShadowRepository:
    """Idempotent writes into the independent append-only research ledger."""

    def __init__(self, connection: PostgresSession) -> None:
        if not isinstance(connection, PostgresSession):
            raise ValueError("connection must be a PostgresSession")
        self.connection = connection

    def _insert_or_match(
        self,
        *,
        table: str,
        key_column: str,
        key: object,
        payload: Mapping[str, object],
        ignore_on_retry: frozenset[str] = frozenset(),
    ) -> bool:
        columns = tuple(payload)
        selected = ", ".join(columns)
        existing = self.connection.execute(
            f"SELECT {selected} FROM {table} WHERE {key_column}=?",
            (key,),
        ).fetchone()
        expected = tuple(payload[column] for column in columns)
        if existing is not None:
            compared = tuple(
                column for column in columns if column not in ignore_on_retry
            )
            if tuple(existing[column] for column in compared) != tuple(
                payload[column] for column in compared
            ):
                raise ValueError(f"immutable {table} conflict")
            return False
        placeholders = ", ".join("?" for _column in columns)
        self.connection.execute(
            f"INSERT INTO {table} ({selected}) VALUES ({placeholders})",
            expected,
        )
        return True

    def store_candidate(
        self,
        candidate: ProspectiveRoshCandidate,
        *,
        created_at: datetime,
    ) -> bool:
        verify_prospective_rosh_candidate(candidate)
        created = _utc(created_at, "created_at")
        if created < candidate.frozen_at:
            raise ValueError("candidate cannot be stored before frozen_at")
        return self._insert_or_match(
            table="prospective_rosh_candidates",
            key_column="candidate_hash",
            key=candidate.artifact_hash,
            payload={
                "candidate_hash": candidate.artifact_hash,
                "artifact_version": candidate.artifact_version,
                "candidate_version": candidate.candidate_version,
                "artifact_json": candidate.canonical_bytes().decode("utf-8"),
                "formula": candidate.formula,
                "retrospective_formula_version": (
                    candidate.retrospective_formula_version
                ),
                "prospective_profile_id": candidate.prospective_profile_id,
                "prospective_profile_hash": candidate.prospective_profile_hash,
                "scorer_source_hash": candidate.scorer_source_hash,
                "training_support": candidate.training_support,
                "training_cohort_hash": candidate.training_cohort_hash,
                "training_cutoff": candidate.training_cutoff.isoformat(),
                "frozen_at": candidate.frozen_at.isoformat(),
                "prospective_start_at": candidate.prospective_start_at.isoformat(),
                "score_mean": candidate.score_mean,
                "score_scale": candidate.score_scale,
                "beta_rosh": candidate.beta_rosh,
                "fit_log_loss": candidate.fit_log_loss,
                "retrospective_initialized": True,
                "prospective_unvalidated": True,
                "shadow_only": True,
                "not_deployment_eligible": True,
                "deployment_eligible": False,
                "created_at": created.isoformat(),
            },
            ignore_on_retry=frozenset({"created_at"}),
        )

__all__ = [
    "PROSPECTIVE_ROSH_SHADOW_VERSION",
    "ArtifactIdentity",
    "ProspectiveRoshEvidence",
    "ProspectiveRoshShadowRepository",
    "TeamRatingAuthority",
    "archive_exact_artifacts",
    "artifact_manifest_hash",
    "build_prospective_rosh_evidence",
    "replay_archived_pure_rosh",
    "verify_prospective_rosh_evidence",
]
