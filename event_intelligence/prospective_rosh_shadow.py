"""Prospective-only, non-deployable R.O.S.H. shadow ledger and evaluation."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from statistics import mean
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.metrics import roc_auc_score

from database.session import PostgresSession
from event_intelligence.prospective_rosh_candidate import (
    PROSPECTIVE_EVALUATION_PLAN,
    ProspectiveRoshCandidate,
    candidate_probability,
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


@dataclass(frozen=True)
class ShadowPrediction:
    prediction_hash: str
    candidate_hash: str
    match_id: int
    series_id: int
    prediction_cutoff: datetime
    record_status: str
    p0_probability: float
    p1_probability: float | None
    pure_rosh_score: float | None
    standardized_rosh_score: float | None
    rosh_logit_contribution: float | None
    beta_rosh: float
    score_mean: float
    score_scale: float
    team_rating: TeamRatingAuthority
    rosh_evidence: ProspectiveRoshEvidence | None
    missing_reason: str | None
    created_at: datetime

    def to_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": PROSPECTIVE_ROSH_SHADOW_VERSION,
            "candidate_hash": self.candidate_hash,
            "match_id": self.match_id,
            "series_id": self.series_id,
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "record_status": self.record_status,
            "p0_probability": self.p0_probability,
            "p1_probability": self.p1_probability,
            "pure_rosh_score": self.pure_rosh_score,
            "standardized_rosh_score": self.standardized_rosh_score,
            "rosh_logit_contribution": self.rosh_logit_contribution,
            "beta_rosh": self.beta_rosh,
            "score_mean": self.score_mean,
            "score_scale": self.score_scale,
            "team_rating": {
                "prediction_id": self.team_rating.prediction_id,
                "run_id": self.team_rating.run_id,
                "rating_version": self.team_rating.rating_version,
                "artifact_version": self.team_rating.artifact_version,
                "artifact_hash": self.team_rating.artifact_hash,
                "input_hash": self.team_rating.input_hash,
                "training_input_hash": self.team_rating.training_input_hash,
            },
            "rosh_evidence": (
                None
                if self.rosh_evidence is None
                else self.rosh_evidence.to_payload()
            ),
            "missing_reason": self.missing_reason,
            "created_at": self.created_at.isoformat(),
        }
        if include_hash:
            payload["prediction_hash"] = self.prediction_hash
        return payload


def _prediction_record(
    candidate: ProspectiveRoshCandidate,
    *,
    match_id: int,
    series_id: int,
    team_rating: TeamRatingAuthority,
    evidence: ProspectiveRoshEvidence | None,
    missing_reason: str | None,
    created_at: datetime,
) -> ShadowPrediction:
    cutoff = _utc(team_rating.prediction_cutoff, "prediction_cutoff")
    created = _utc(created_at, "created_at")
    if created > cutoff:
        raise ValueError("shadow prediction must be written by prediction_cutoff")
    if cutoff < candidate.prospective_start_at:
        raise ValueError("shadow prediction precedes prospective collection start")
    p1: float | None = None
    standardized: float | None = None
    contribution: float | None = None
    score: float | None = None
    status = "p0_only"
    if evidence is not None:
        verify_prospective_rosh_evidence(candidate, evidence)
        if evidence.available_at > cutoff or evidence.statistics_cutoff > cutoff:
            raise ValueError("R.O.S.H. evidence is unavailable at prediction_cutoff")
        p1, standardized, contribution = candidate_probability(
            candidate,
            team_probability=team_rating.probability,
            pure_rosh_score=evidence.pure_rosh_score,
        )
        score = evidence.pure_rosh_score
        status = "paired"
        if missing_reason is not None:
            raise ValueError("paired shadow prediction cannot have missing_reason")
    else:
        _nonempty(missing_reason, "missing_reason")
    prediction = ShadowPrediction(
        prediction_hash="",
        candidate_hash=candidate.artifact_hash,
        match_id=_positive_integer(match_id, "match_id"),
        series_id=_positive_integer(series_id, "series_id"),
        prediction_cutoff=cutoff,
        record_status=status,
        p0_probability=team_rating.probability,
        p1_probability=p1,
        pure_rosh_score=score,
        standardized_rosh_score=standardized,
        rosh_logit_contribution=contribution,
        beta_rosh=candidate.beta_rosh,
        score_mean=candidate.score_mean,
        score_scale=candidate.score_scale,
        team_rating=team_rating,
        rosh_evidence=evidence,
        missing_reason=missing_reason,
        created_at=created,
    )
    return ShadowPrediction(
        **{
            **prediction.__dict__,
            "prediction_hash": _hash(prediction.to_payload(include_hash=False)),
        }
    )


def build_shadow_prediction(
    candidate: ProspectiveRoshCandidate,
    *,
    match_id: int,
    series_id: int,
    team_rating: TeamRatingAuthority,
    rosh_evidence: ProspectiveRoshEvidence | None,
    missing_reason: str | None = None,
    created_at: datetime,
) -> ShadowPrediction:
    """Build P0/P1, or fail closed to P0-only for invalid R.O.S.H. evidence."""

    verify_prospective_rosh_candidate(candidate)
    if rosh_evidence is not None:
        try:
            return _prediction_record(
                candidate,
                match_id=match_id,
                series_id=series_id,
                team_rating=team_rating,
                evidence=rosh_evidence,
                missing_reason=None,
                created_at=created_at,
            )
        except ValueError:
            missing_reason = missing_reason or "rosh_evidence_invalid"
    return _prediction_record(
        candidate,
        match_id=match_id,
        series_id=series_id,
        team_rating=team_rating,
        evidence=None,
        missing_reason=missing_reason,
        created_at=created_at,
    )


def verify_shadow_prediction(record: ShadowPrediction) -> None:
    if not isinstance(record, ShadowPrediction):
        raise ValueError("record must be a ShadowPrediction")
    if record.prediction_hash != _hash(record.to_payload(include_hash=False)):
        raise ValueError("shadow prediction content hash mismatch")
    if record.team_rating.prediction_cutoff != record.prediction_cutoff:
        raise ValueError("Team Rating and shadow cutoffs disagree")
    if record.created_at > record.prediction_cutoff:
        raise ValueError("shadow prediction follows prediction_cutoff")
    if record.record_status == "paired":
        if (
            record.rosh_evidence is None
            or record.p1_probability is None
            or record.pure_rosh_score is None
            or record.standardized_rosh_score is None
            or record.rosh_logit_contribution is None
            or record.missing_reason is not None
        ):
            raise ValueError("paired shadow prediction is incomplete")
        if record.rosh_evidence.evidence_hash != _hash(
            record.rosh_evidence.to_payload(include_hash=False)
        ):
            raise ValueError("shadow R.O.S.H. evidence hash mismatch")
    elif record.record_status == "p0_only":
        if (
            record.rosh_evidence is not None
            or record.p1_probability is not None
            or record.pure_rosh_score is not None
            or record.standardized_rosh_score is not None
            or record.rosh_logit_contribution is not None
        ):
            raise ValueError("P0-only shadow prediction contains R.O.S.H. output")
        _nonempty(record.missing_reason, "missing_reason")
    else:
        raise ValueError("shadow prediction status is invalid")


@dataclass(frozen=True)
class ShadowSettlement:
    settlement_hash: str
    prediction_hash: str
    eventual_radiant_win: int
    result_artifact_hash: str
    result_usable_at: datetime
    settled_at: datetime
    created_at: datetime

    def to_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": PROSPECTIVE_ROSH_SHADOW_VERSION,
            "prediction_hash": self.prediction_hash,
            "eventual_radiant_win": self.eventual_radiant_win,
            "result_artifact_hash": self.result_artifact_hash,
            "result_usable_at": self.result_usable_at.isoformat(),
            "settled_at": self.settled_at.isoformat(),
            "created_at": self.created_at.isoformat(),
        }
        if include_hash:
            payload["settlement_hash"] = self.settlement_hash
        return payload


def build_shadow_settlement(
    prediction: ShadowPrediction,
    *,
    eventual_radiant_win: int,
    result_artifact_hash: str,
    result_usable_at: datetime,
    settled_at: datetime,
    created_at: datetime,
) -> ShadowSettlement:
    outcome = eventual_radiant_win
    if isinstance(outcome, bool) or outcome not in (0, 1):
        raise ValueError("eventual_radiant_win must be 0 or 1")
    usable = _utc(result_usable_at, "result_usable_at")
    settled = _utc(settled_at, "settled_at")
    created = _utc(created_at, "created_at")
    if not prediction.prediction_cutoff < usable <= settled <= created:
        raise ValueError("settlement timing is invalid")
    record = ShadowSettlement(
        settlement_hash="",
        prediction_hash=_digest(prediction.prediction_hash, "prediction_hash"),
        eventual_radiant_win=outcome,
        result_artifact_hash=_digest(
            result_artifact_hash,
            "result_artifact_hash",
        ),
        result_usable_at=usable,
        settled_at=settled,
        created_at=created,
    )
    return ShadowSettlement(
        **{
            **record.__dict__,
            "settlement_hash": _hash(record.to_payload(include_hash=False)),
        }
    )


def verify_shadow_settlement(record: ShadowSettlement) -> None:
    if not isinstance(record, ShadowSettlement):
        raise ValueError("record must be a ShadowSettlement")
    if record.settlement_hash != _hash(record.to_payload(include_hash=False)):
        raise ValueError("shadow settlement content hash mismatch")
    if not record.result_usable_at <= record.settled_at <= record.created_at:
        raise ValueError("shadow settlement timing is invalid")


@dataclass(frozen=True)
class SettledShadowRow:
    prediction_hash: str
    candidate_hash: str
    match_id: int
    series_id: int
    prediction_cutoff: datetime
    record_status: str
    p0_probability: float
    p1_probability: float | None
    pure_rosh_score: float | None
    standardized_rosh_score: float | None
    rosh_logit_contribution: float | None
    missing_reason: str | None
    rosh_profile_hash: str | None
    rosh_formula_version: str | None
    rosh_scorer_source_hash: str | None
    outcome: int | None
    event_id: str
    patch: int | None
    causal_eligible: bool = True
    causal_exclusion_reason: str | None = None

    @property
    def month(self) -> str:
        return self.prediction_cutoff.strftime("%Y-%m")


def _metrics(
    outcomes: Sequence[int],
    probabilities: Sequence[float],
) -> dict[str, float | int | None]:
    observed = np.asarray(outcomes, dtype=np.int64)
    predicted = np.clip(
        np.asarray(probabilities, dtype=np.float64),
        _EPSILON,
        1.0 - _EPSILON,
    )
    if observed.shape != predicted.shape or observed.ndim != 1 or not len(observed):
        raise ValueError("metric vectors must be non-empty and aligned")
    auc = (
        None
        if len(set(observed.tolist())) < 2
        else float(roc_auc_score(observed, predicted))
    )
    return {
        "support": int(len(observed)),
        "brier_score": float(np.mean((predicted - observed) ** 2)),
        "log_loss": float(
            -np.mean(
                observed * np.log(predicted)
                + (1 - observed) * np.log1p(-predicted)
            )
        ),
        "auc": auc,
        "accuracy": float(np.mean((predicted >= 0.5) == observed)),
        "ece": _ece(observed, predicted),
    }


def _ece(outcomes: np.ndarray, probabilities: np.ndarray) -> float:
    bins = int(PROSPECTIVE_EVALUATION_PLAN["ece_bins"])
    indices = np.minimum((probabilities * bins).astype(int), bins - 1)
    result = 0.0
    for index in range(bins):
        selected = indices == index
        support = int(np.sum(selected))
        if support:
            result += support / len(outcomes) * abs(
                float(np.mean(probabilities[selected]))
                - float(np.mean(outcomes[selected]))
            )
    return result


def _metric_delta(
    m0: Mapping[str, float | int | None],
    m1: Mapping[str, float | int | None],
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for metric in ("brier_score", "log_loss", "auc", "accuracy", "ece"):
        left = m0[metric]
        right = m1[metric]
        result[metric] = (
            None if left is None or right is None else float(right) - float(left)
        )
    return result


def _paired_metrics(rows: Sequence[SettledShadowRow]) -> dict[str, object]:
    outcomes = [int(row.outcome) for row in rows if row.outcome is not None]
    p0 = [row.p0_probability for row in rows]
    p1 = [_finite(row.p1_probability, "P1") for row in rows]
    m0 = _metrics(outcomes, p0)
    m1 = _metrics(outcomes, p1)
    return {"m0": m0, "m1": m1, "delta_m1_minus_m0": _metric_delta(m0, m1)}


def _interval(values: Sequence[float]) -> dict[str, float | int | None]:
    finite = np.asarray([value for value in values if math.isfinite(value)])
    if not len(finite):
        return {"lower": None, "upper": None, "valid_samples": 0}
    lower, upper = np.quantile(finite, (0.025, 0.975))
    return {
        "lower": float(lower),
        "upper": float(upper),
        "valid_samples": int(len(finite)),
    }


def _clustered_bootstrap(
    rows: Sequence[SettledShadowRow],
    *,
    candidate_hash: str,
    samples: int,
) -> dict[str, dict[str, float | int | None]]:
    clusters: dict[int, list[SettledShadowRow]] = {}
    for row in rows:
        clusters.setdefault(row.series_id, []).append(row)
    keys = sorted(clusters)
    seed = int(hashlib.sha256(candidate_hash.encode()).hexdigest()[:16], 16)
    generator = np.random.default_rng(seed)
    estimates: dict[str, list[float]] = {
        metric: []
        for metric in ("brier_score", "log_loss", "auc", "accuracy", "ece")
    }
    for _sample in range(samples):
        selected = generator.choice(len(keys), size=len(keys), replace=True)
        sample = tuple(
            row for index in selected for row in clusters[keys[int(index)]]
        )
        deltas = _paired_metrics(sample)["delta_m1_minus_m0"]
        for metric, value in deltas.items():
            if value is not None:
                estimates[metric].append(float(value))
    return {metric: _interval(values) for metric, values in estimates.items()}


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"support": 0, "minimum": None, "maximum": None, "mean": None}
    return {
        "support": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "mean": mean(values),
        "standard_deviation": float(np.std(values)),
    }


def _slices(
    rows: Sequence[SettledShadowRow],
    field: str,
) -> tuple[dict[str, object], ...]:
    groups: dict[str, list[SettledShadowRow]] = {}
    for row in rows:
        raw = row.month if field == "month" else getattr(row, field)
        groups.setdefault("unknown" if raw is None else str(raw), []).append(row)
    return tuple(
        {
            "value": value,
            "support": len(group),
            **_paired_metrics(group),
        }
        for value, group in sorted(groups.items())
    )


@dataclass(frozen=True)
class ShadowEvaluation:
    evaluation_hash: str
    candidate_hash: str
    stage: int
    paired_support: int
    window_manifest: Mapping[str, object]
    window_manifest_hash: str
    report: Mapping[str, object]
    report_hash: str
    created_at: datetime


def build_shadow_evaluation(
    candidate: ProspectiveRoshCandidate,
    rows: Sequence[SettledShadowRow],
    *,
    stage: int,
    created_at: datetime,
    bootstrap_samples: int | None = None,
) -> ShadowEvaluation:
    verify_prospective_rosh_candidate(candidate)
    if stage not in (20, 100, 200):
        raise ValueError("stage must be 20, 100, or 200")
    ordered = tuple(sorted(rows, key=lambda row: (row.prediction_cutoff, row.match_id)))
    if any(row.candidate_hash != candidate.artifact_hash for row in ordered):
        raise ValueError("evaluation rows mix candidate identities")
    paired = tuple(
        row
        for row in ordered
        if row.record_status == "paired"
        and row.p1_probability is not None
        and row.outcome is not None
        and row.causal_eligible
    )
    if len(paired) < stage:
        raise ValueError(f"stage {stage} requires {stage} settled paired maps")
    window = paired[:stage]
    boundary = (window[-1].prediction_cutoff, window[-1].match_id)
    through_boundary = tuple(
        row
        for row in ordered
        if (row.prediction_cutoff, row.match_id) <= boundary
    )
    if any(row.outcome is None for row in through_boundary):
        raise ValueError("all shadow rows through the stage boundary must be settled")
    manifest = {
        "schema": "prospective-rosh-shadow-window/v1",
        "candidate_hash": candidate.artifact_hash,
        "stage": stage,
        "selection": "first_paired_by_prediction_cutoff_then_match_id",
        "paired_prediction_hashes": [row.prediction_hash for row in window],
        "boundary_prediction_cutoff": window[-1].prediction_cutoff.isoformat(),
        "boundary_match_id": window[-1].match_id,
    }
    manifest_hash = _hash(manifest)
    missing = Counter(
        row.missing_reason
        for row in through_boundary
        if row.record_status == "p0_only" and row.missing_reason is not None
    )
    report: dict[str, object] = {
        "schema": "prospective-rosh-shadow-evaluation/v1",
        "candidate_hash": candidate.artifact_hash,
        "stage": stage,
        "paired_support": stage,
        "total_predictions_through_boundary": len(through_boundary),
        "coverage": stage / len(through_boundary),
        "missing_reasons": [
            {"reason": reason, "support": support}
            for reason, support in sorted(missing.items())
        ],
        "candidate_labels": list(candidate.labels),
        "deployment_evidence": False,
        "candidate_mutation_allowed": False,
        "causal_exclusions": [
            {
                "reason": reason,
                "support": support,
            }
            for reason, support in sorted(
                Counter(
                    row.causal_exclusion_reason or "causal_audit_unavailable"
                    for row in through_boundary
                    if not row.causal_eligible
                ).items()
            )
        ],
    }
    if stage == 20:
        report["acceptance_scope"] = [
            "collection",
            "linkage",
            "offline_exact_replay",
            "settlement",
            "idempotency",
            "append_only",
        ]
        report["effectiveness_conclusion_allowed"] = False
    if stage >= 100:
        report.update(
            {
                "pure_rosh_score_distribution": _distribution(
                    [_finite(row.pure_rosh_score, "score") for row in window]
                ),
                "beta_contribution_distribution": _distribution(
                    [
                        _finite(row.rosh_logit_contribution, "contribution")
                        for row in window
                    ]
                ),
                "profile_drift": {
                    "profile_hashes": sorted(
                        {str(row.rosh_profile_hash) for row in window}
                    ),
                    "formula_versions": sorted(
                        {str(row.rosh_formula_version) for row in window}
                    ),
                    "scorer_source_hashes": sorted(
                        {str(row.rosh_scorer_source_hash) for row in window}
                    ),
                    "detected": any(
                        row.rosh_profile_hash != candidate.prospective_profile_hash
                        or row.rosh_formula_version
                        != candidate.retrospective_formula_version
                        or row.rosh_scorer_source_hash != candidate.scorer_source_hash
                        for row in window
                    ),
                },
            }
        )
    if stage == 100:
        report["effectiveness_conclusion_allowed"] = False
    if stage == 200:
        samples = (
            int(PROSPECTIVE_EVALUATION_PLAN["bootstrap_samples"])
            if bootstrap_samples is None
            else bootstrap_samples
        )
        if samples < 1:
            raise ValueError("bootstrap_samples must be positive")
        comparison = _paired_metrics(window)
        intervals = _clustered_bootstrap(
            window,
            candidate_hash=candidate.artifact_hash,
            samples=samples,
        )
        slices = {
            "event": _slices(window, "event_id"),
            "patch": _slices(window, "patch"),
            "month": _slices(window, "month"),
        }
        min_support = int(PROSPECTIVE_EVALUATION_PLAN["major_slice_min_support"])
        major_slices = [
            row
            for values in slices.values()
            for row in values
            if int(row["support"]) >= min_support
        ]
        stable_primary = all(
            intervals[metric]["upper"] is not None
            and float(intervals[metric]["upper"]) < 0.0
            for metric in ("brier_score", "log_loss")
        )
        ece_delta = comparison["delta_m1_minus_m0"]["ece"]
        calibration_ok = (
            ece_delta is not None
            and float(ece_delta)
            <= float(PROSPECTIVE_EVALUATION_PLAN["max_ece_increase"])
        )
        slices_ok = all(
            float(row["delta_m1_minus_m0"]["brier_score"])
            <= float(
                PROSPECTIVE_EVALUATION_PLAN["max_major_slice_brier_increase"]
            )
            and float(row["delta_m1_minus_m0"]["log_loss"])
            <= float(
                PROSPECTIVE_EVALUATION_PLAN[
                    "max_major_slice_log_loss_increase"
                ]
            )
            for row in major_slices
        )
        report.update(
            {
                "comparison": comparison,
                "series_clustered_bootstrap_delta_95": intervals,
                "bootstrap_samples": samples,
                "slices": slices,
                "preregistered_gate": {
                    "stable_brier_and_log_loss": stable_primary,
                    "calibration_not_materially_worse": calibration_ok,
                    "major_slices_not_materially_worse": slices_ok,
                    "eligible_to_propose_followup_pr": (
                        stable_primary and calibration_ok and slices_ok
                    ),
                },
                "deployment_eligible": False,
            }
        )
    report_hash = _hash(report)
    created = _utc(created_at, "created_at")
    identity = {
        "candidate_hash": candidate.artifact_hash,
        "stage": stage,
        "window_manifest_hash": manifest_hash,
        "report_hash": report_hash,
        "created_at": created.isoformat(),
    }
    return ShadowEvaluation(
        evaluation_hash=_hash(identity),
        candidate_hash=candidate.artifact_hash,
        stage=stage,
        paired_support=stage,
        window_manifest=manifest,
        window_manifest_hash=manifest_hash,
        report=report,
        report_hash=report_hash,
        created_at=created,
    )


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

    def store_prediction(self, record: ShadowPrediction) -> bool:
        verify_shadow_prediction(record)
        evidence = record.rosh_evidence
        existing_identity = self.connection.execute(
            """SELECT prediction_hash
                 FROM prospective_rosh_shadow_predictions
                WHERE candidate_hash=? AND match_id=?""",
            (record.candidate_hash, record.match_id),
        ).fetchone()
        if existing_identity is not None and str(existing_identity[0]) != record.prediction_hash:
            raise ValueError("immutable prospective R.O.S.H. match prediction conflict")
        return self._insert_or_match(
            table="prospective_rosh_shadow_predictions",
            key_column="prediction_hash",
            key=record.prediction_hash,
            payload={
                "prediction_hash": record.prediction_hash,
                "candidate_hash": record.candidate_hash,
                "match_id": record.match_id,
                "series_id": record.series_id,
                "prediction_cutoff": record.prediction_cutoff.isoformat(),
                "record_status": record.record_status,
                "p0_probability": record.p0_probability,
                "p1_probability": record.p1_probability,
                "pure_rosh_score": record.pure_rosh_score,
                "standardized_rosh_score": record.standardized_rosh_score,
                "rosh_logit_contribution": record.rosh_logit_contribution,
                "beta_rosh": record.beta_rosh,
                "score_mean": record.score_mean,
                "score_scale": record.score_scale,
                "team_rating_prediction_id": record.team_rating.prediction_id,
                "team_rating_run_id": record.team_rating.run_id,
                "team_rating_version": record.team_rating.rating_version,
                "team_rating_artifact_version": (
                    record.team_rating.artifact_version
                ),
                "team_rating_artifact_hash": record.team_rating.artifact_hash,
                "team_rating_input_hash": record.team_rating.input_hash,
                "team_rating_training_input_hash": (
                    record.team_rating.training_input_hash
                ),
                "rosh_profile_id": None if evidence is None else evidence.profile_id,
                "rosh_profile_hash": (
                    None if evidence is None else evidence.profile_hash
                ),
                "rosh_formula_version": (
                    None if evidence is None else evidence.formula_version
                ),
                "rosh_scorer_source_hash": (
                    None if evidence is None else evidence.scorer_source_hash
                ),
                "rosh_evidence_hash": (
                    None if evidence is None else evidence.evidence_hash
                ),
                "rosh_radiant_heroes_json": (
                    None
                    if evidence is None
                    else _canonical_json(list(evidence.radiant_heroes))
                ),
                "rosh_dire_heroes_json": (
                    None
                    if evidence is None
                    else _canonical_json(list(evidence.dire_heroes))
                ),
                "rosh_request_artifacts_json": (
                    None
                    if evidence is None
                    else _canonical_json(
                        [value.to_payload() for value in evidence.request_artifacts]
                    )
                ),
                "rosh_request_manifest_hash": (
                    None if evidence is None else evidence.request_manifest_hash
                ),
                "rosh_response_artifacts_json": (
                    None
                    if evidence is None
                    else _canonical_json(
                        [value.to_payload() for value in evidence.response_artifacts]
                    )
                ),
                "rosh_response_manifest_hash": (
                    None if evidence is None else evidence.response_manifest_hash
                ),
                "rosh_statistics_cutoff": (
                    None if evidence is None else evidence.statistics_cutoff.isoformat()
                ),
                "rosh_available_at": (
                    None if evidence is None else evidence.available_at.isoformat()
                ),
                "missing_reason": record.missing_reason,
                "created_at": record.created_at.isoformat(),
            },
        )

    def store_settlement(self, record: ShadowSettlement) -> bool:
        verify_shadow_settlement(record)
        return self._insert_or_match(
            table="prospective_rosh_shadow_settlements",
            key_column="prediction_hash",
            key=record.prediction_hash,
            payload={
                "settlement_hash": record.settlement_hash,
                "prediction_hash": record.prediction_hash,
                "eventual_radiant_win": record.eventual_radiant_win,
                "result_artifact_hash": record.result_artifact_hash,
                "result_usable_at": record.result_usable_at.isoformat(),
                "settled_at": record.settled_at.isoformat(),
                "created_at": record.created_at.isoformat(),
            },
        )

    def store_evaluation(self, record: ShadowEvaluation) -> bool:
        if record.evaluation_hash != _hash(
            {
                "candidate_hash": record.candidate_hash,
                "stage": record.stage,
                "window_manifest_hash": record.window_manifest_hash,
                "report_hash": record.report_hash,
                "created_at": record.created_at.isoformat(),
            }
        ):
            raise ValueError("shadow evaluation content hash mismatch")
        if record.window_manifest_hash != _hash(record.window_manifest):
            raise ValueError("shadow evaluation window hash mismatch")
        if record.report_hash != _hash(record.report):
            raise ValueError("shadow evaluation report hash mismatch")
        return self._insert_or_match(
            table="prospective_rosh_shadow_evaluations",
            key_column="evaluation_hash",
            key=record.evaluation_hash,
            payload={
                "evaluation_hash": record.evaluation_hash,
                "candidate_hash": record.candidate_hash,
                "stage": record.stage,
                "paired_support": record.paired_support,
                "window_manifest_json": _canonical_json(record.window_manifest),
                "window_manifest_hash": record.window_manifest_hash,
                "report_json": _canonical_json(record.report),
                "report_hash": record.report_hash,
                "created_at": record.created_at.isoformat(),
            },
        )

    def load_settled_rows(self, candidate_hash: str) -> tuple[SettledShadowRow, ...]:
        _digest(candidate_hash, "candidate_hash")
        rows = self.connection.execute(
            """SELECT prediction.prediction_hash,
                      prediction.candidate_hash, prediction.match_id,
                      prediction.series_id, prediction.prediction_cutoff,
                      prediction.record_status, prediction.p0_probability,
                      prediction.p1_probability, prediction.pure_rosh_score,
                      prediction.standardized_rosh_score,
                      prediction.rosh_logit_contribution,
                      prediction.missing_reason, prediction.rosh_profile_hash,
                      prediction.rosh_formula_version,
                      prediction.rosh_scorer_source_hash,
                      settlement.eventual_radiant_win AS outcome,
                      causal.causal_eligible,
                      causal.exclusion_reason AS causal_exclusion_reason,
                      ingest.event_id, game.patch
                 FROM prospective_rosh_shadow_predictions AS prediction
                 JOIN match_ingest_status AS ingest
                   ON ingest.match_id=prediction.match_id
                 JOIN matches AS game ON game.match_id=prediction.match_id
                  LEFT JOIN prospective_rosh_shadow_settlements AS settlement
                    ON settlement.prediction_hash=prediction.prediction_hash
                  LEFT JOIN prospective_rosh_causal_audits AS causal
                    ON causal.prediction_hash=prediction.prediction_hash
                WHERE prediction.candidate_hash=?
                ORDER BY live_text_timestamp_utc(prediction.prediction_cutoff),
                         prediction.match_id""",
            (candidate_hash,),
        ).fetchall()
        result: list[SettledShadowRow] = []
        for row in rows:
            cutoff = datetime.fromisoformat(
                str(row["prediction_cutoff"]).replace("Z", "+00:00")
            )
            result.append(
                SettledShadowRow(
                    prediction_hash=str(row["prediction_hash"]),
                    candidate_hash=str(row["candidate_hash"]),
                    match_id=int(row["match_id"]),
                    series_id=int(row["series_id"]),
                    prediction_cutoff=_utc(cutoff, "prediction_cutoff"),
                    record_status=str(row["record_status"]),
                    p0_probability=float(row["p0_probability"]),
                    p1_probability=(
                        None
                        if row["p1_probability"] is None
                        else float(row["p1_probability"])
                    ),
                    pure_rosh_score=(
                        None
                        if row["pure_rosh_score"] is None
                        else float(row["pure_rosh_score"])
                    ),
                    standardized_rosh_score=(
                        None
                        if row["standardized_rosh_score"] is None
                        else float(row["standardized_rosh_score"])
                    ),
                    rosh_logit_contribution=(
                        None
                        if row["rosh_logit_contribution"] is None
                        else float(row["rosh_logit_contribution"])
                    ),
                    missing_reason=(
                        None
                        if row["missing_reason"] is None
                        else str(row["missing_reason"])
                    ),
                    rosh_profile_hash=(
                        None
                        if row["rosh_profile_hash"] is None
                        else str(row["rosh_profile_hash"])
                    ),
                    rosh_formula_version=(
                        None
                        if row["rosh_formula_version"] is None
                        else str(row["rosh_formula_version"])
                    ),
                    rosh_scorer_source_hash=(
                        None
                        if row["rosh_scorer_source_hash"] is None
                        else str(row["rosh_scorer_source_hash"])
                    ),
                    outcome=(
                        None if row["outcome"] is None else int(row["outcome"])
                    ),
                    event_id=str(row["event_id"]),
                    patch=None if row["patch"] is None else int(row["patch"]),
                    causal_eligible=(
                        row["causal_eligible"] is not None
                        and bool(row["causal_eligible"])
                    ),
                    causal_exclusion_reason=(
                        "causal_audit_unavailable"
                        if row["causal_eligible"] is None
                        else None
                        if row["causal_exclusion_reason"] is None
                        else str(row["causal_exclusion_reason"])
                    ),
                )
            )
        return tuple(result)


__all__ = [
    "PROSPECTIVE_ROSH_SHADOW_VERSION",
    "ArtifactIdentity",
    "ProspectiveRoshEvidence",
    "ProspectiveRoshShadowRepository",
    "SettledShadowRow",
    "ShadowEvaluation",
    "ShadowPrediction",
    "ShadowSettlement",
    "TeamRatingAuthority",
    "archive_exact_artifacts",
    "artifact_manifest_hash",
    "build_prospective_rosh_evidence",
    "build_shadow_evaluation",
    "build_shadow_prediction",
    "build_shadow_settlement",
    "replay_archived_pure_rosh",
    "verify_prospective_rosh_evidence",
    "verify_shadow_prediction",
    "verify_shadow_settlement",
]
