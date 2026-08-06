"""Frozen retrospective-initialized candidate for prospective R.O.S.H. shadowing."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit
from sklearn.model_selection import GroupKFold

from event_intelligence.raw_archive import canonical_json_bytes
from event_intelligence.legacy_rosh_reconstruction import (
    LEGACY_ROSH_FORMULA_VERSION,
)
from event_intelligence.rosh_retrospective_utility import (
    CohortLoadResult,
    RetrospectiveRow,
)
from prematch.stratz_rosh import (
    HEROES_META_POSITIONS_QUERY,
    HERO_STATS_BY_TIME_QUERY,
    ROSH_BRACKET_BASIC,
    SYNERGY_QUERY,
)


PROSPECTIVE_ROSH_CANDIDATE_VERSION = "prospective-rosh-candidate-v1"
PROSPECTIVE_ROSH_ARTIFACT_VERSION = "prospective-rosh-candidate-artifact-v1"
PROSPECTIVE_ROSH_PROFILE_ID = "legacy-dematus-pure-rosh-prospective-v1"
PROSPECTIVE_ROSH_PROFILE_SCHEMA = "dematus-pure-rosh-profile/v1"
PROSPECTIVE_ROSH_SCORER_FAMILY = "legacy_dematus_pure_lineup"
PROSPECTIVE_ROSH_FORMULA = (
    "logit(P1)=logit(P0)-beta_rosh*standardized_pure_rosh_score"
)
PROSPECTIVE_ROSH_LABELS = (
    "retrospective_initialized",
    "prospective_unvalidated",
    "shadow_only",
    "not_deployment_eligible",
)
PROSPECTIVE_EVALUATION_PLAN = {
    "schema": "prospective-rosh-evaluation-plan/v1",
    "phase_20_paired_maps": "lineage_replay_settlement_idempotency_append_only",
    "phase_100_paired_maps": "coverage_missing_distribution_profile_drift_only",
    "phase_200_paired_maps": "first_preregistered_paired_evaluation",
    "bootstrap_samples": 2_000,
    "bootstrap_confidence": 0.95,
    "ece_bins": 10,
    "major_slice_min_support": 20,
    "max_ece_increase": 0.01,
    "max_major_slice_brier_increase": 0.01,
    "max_major_slice_log_loss_increase": 0.02,
    "required_primary_metrics": ["brier_score", "log_loss"],
    "slices": ["event", "patch", "month"],
    "window": "first_200_paired_by_prediction_cutoff_then_match_id",
}
_UTC = timezone.utc
_EPSILON = 1e-12
_FROZEN_CANDIDATE_PATH = (
    Path(__file__).parent / "resources" / "prospective_rosh_candidate_v1.json"
)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(_UTC)


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _scorer_source_hash() -> str:
    path = Path(__file__).parents[1] / "prematch" / "stratz_rosh.py"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prospective_rosh_profile() -> dict[str, Any]:
    operations = (
        ("heroes_meta_positions", HEROES_META_POSITIONS_QUERY),
        ("hero_stats_by_time_bracket", HERO_STATS_BY_TIME_QUERY),
        ("synergy", SYNERGY_QUERY),
    )
    profile = {
        "schema": PROSPECTIVE_ROSH_PROFILE_SCHEMA,
        "profile_id": PROSPECTIVE_ROSH_PROFILE_ID,
        "formula_version": LEGACY_ROSH_FORMULA_VERSION,
        "scorer_family": PROSPECTIVE_ROSH_SCORER_FAMILY,
        "scorer_source_hash": _scorer_source_hash(),
        "source": "stratz",
        "bracket_basic": ROSH_BRACKET_BASIC,
        "pure_lineup_only": True,
        "player_identity_used": False,
        "official_v2_compatible": False,
        "operations": [
            {
                "name": name,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            }
            for name, query in operations
        ],
    }
    return {**profile, "profile_hash": _hash(profile)}


@dataclass(frozen=True)
class CandidateFold:
    fold: int
    train_support: int
    test_support: int
    train_mean: float
    train_scale: float
    beta_rosh: float

    def to_payload(self) -> dict[str, float | int]:
        return {
            "fold": self.fold,
            "train_support": self.train_support,
            "test_support": self.test_support,
            "train_mean": self.train_mean,
            "train_scale": self.train_scale,
            "beta_rosh": self.beta_rosh,
        }


@dataclass(frozen=True)
class ProspectiveRoshCandidate:
    artifact_version: str
    candidate_version: str
    formula: str
    labels: tuple[str, ...]
    retrospective_formula_version: str
    prospective_profile_id: str
    prospective_profile_hash: str
    scorer_source_hash: str
    training_support: int
    training_cohort_hash: str
    training_cutoff: datetime
    frozen_at: datetime
    prospective_start_at: datetime
    score_mean: float
    score_scale: float
    beta_rosh: float
    fit_log_loss: float
    folds: tuple[CandidateFold, ...]
    evaluation_plan: Mapping[str, Any]
    artifact_hash: str

    def to_payload(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "artifact_version": self.artifact_version,
            "candidate_version": self.candidate_version,
            "formula": self.formula,
            "labels": list(self.labels),
            "retrospective_formula_version": self.retrospective_formula_version,
            "prospective_profile_id": self.prospective_profile_id,
            "prospective_profile_hash": self.prospective_profile_hash,
            "scorer_source_hash": self.scorer_source_hash,
            "training_support": self.training_support,
            "training_cohort_hash": self.training_cohort_hash,
            "training_cutoff": self.training_cutoff.isoformat(),
            "frozen_at": self.frozen_at.isoformat(),
            "prospective_start_at": self.prospective_start_at.isoformat(),
            "score_mean": self.score_mean,
            "score_scale": self.score_scale,
            "beta_rosh": self.beta_rosh,
            "fit_log_loss": self.fit_log_loss,
            "folds": [row.to_payload() for row in self.folds],
            "fold_beta_range": {
                "minimum": min(row.beta_rosh for row in self.folds),
                "maximum": max(row.beta_rosh for row in self.folds),
            },
            "evaluation_plan": dict(self.evaluation_plan),
            "deployment_eligible": False,
        }
        if include_hash:
            payload["artifact_hash"] = self.artifact_hash
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())


def _training_rows(rows: Sequence[RetrospectiveRow]) -> tuple[RetrospectiveRow, ...]:
    values = tuple(
        sorted(
            rows,
            key=lambda row: (
                _utc(row.prediction_cutoff, "prediction_cutoff"),
                row.match_id,
            ),
        )
    )
    if len(values) != 513:
        raise ValueError("prospective candidate requires the frozen 513-map cohort")
    if any(row.team_probability is None for row in values):
        raise ValueError("candidate cohort must be completely paired")
    if {row.formula_version for row in values} != {LEGACY_ROSH_FORMULA_VERSION}:
        raise ValueError("candidate cohort formula identity is not frozen")
    for row in values:
        score = _finite(row.pure_lineup_score, "pure_lineup_score")
        probability = _finite(row.team_probability, "team_probability")
        if not 0.0 < probability < 1.0:
            raise ValueError("candidate Team Rating probabilities must be interior")
        if row.radiant_win not in (0, 1):
            raise ValueError("candidate outcomes must be binary")
        if score != row.pure_lineup_score:
            raise ValueError("candidate score representation is not canonical")
    return values


def _cohort_hash(rows: Sequence[RetrospectiveRow]) -> str:
    return _hash(
        {
            "domain": "prospective-rosh-candidate-training-cohort/v1",
            "rows": [
                {
                    "match_id": row.match_id,
                    "score_key": row.score_key,
                    "formula_version": row.formula_version,
                    "prediction_cutoff": row.prediction_cutoff.isoformat(),
                    "series_key": row.series_key,
                    "pure_lineup_score": row.pure_lineup_score,
                    "team_probability": row.team_probability,
                    "radiant_win": row.radiant_win,
                }
                for row in rows
            ],
        }
    )


def _fit_beta(
    scores: np.ndarray,
    probabilities: np.ndarray,
    outcomes: np.ndarray,
) -> tuple[float, float]:
    offsets = logit(np.clip(probabilities, _EPSILON, 1.0 - _EPSILON))

    def objective(parameters: np.ndarray) -> tuple[float, np.ndarray]:
        beta = float(parameters[0])
        logits = offsets - beta * scores
        loss = float(np.mean(np.logaddexp(0.0, logits) - outcomes * logits))
        gradient = np.asarray([np.mean((expit(logits) - outcomes) * (-scores))])
        return loss, gradient

    result = minimize(
        objective,
        np.zeros(1, dtype=np.float64),
        jac=True,
        method="L-BFGS-B",
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        raise RuntimeError(f"candidate beta fit failed: {result.message}")
    return float(result.x[0]), float(result.fun)


def _folds(rows: Sequence[RetrospectiveRow]) -> tuple[CandidateFold, ...]:
    scores = np.asarray([row.pure_lineup_score for row in rows], dtype=np.float64)
    probabilities = np.asarray([row.team_probability for row in rows], dtype=np.float64)
    outcomes = np.asarray([row.radiant_win for row in rows], dtype=np.float64)
    groups = np.asarray([row.series_key for row in rows], dtype=object)
    result: list[CandidateFold] = []
    for fold, (train, test) in enumerate(
        GroupKFold(n_splits=5).split(scores, outcomes, groups),
        1,
    ):
        train_mean = float(np.mean(scores[train]))
        train_scale = float(np.std(scores[train]))
        if train_scale <= 0.0:
            raise ValueError("candidate fold scale is zero")
        beta, _loss = _fit_beta(
            (scores[train] - train_mean) / train_scale,
            probabilities[train],
            outcomes[train],
        )
        result.append(
            CandidateFold(
                fold=fold,
                train_support=len(train),
                test_support=len(test),
                train_mean=train_mean,
                train_scale=train_scale,
                beta_rosh=beta,
            )
        )
    return tuple(result)


def freeze_prospective_rosh_candidate(
    cohort: CohortLoadResult,
    *,
    frozen_at: datetime,
    prospective_start_at: datetime,
) -> ProspectiveRoshCandidate:
    if not isinstance(cohort, CohortLoadResult):
        raise ValueError("cohort must be a CohortLoadResult")
    values = _training_rows(cohort.paired)
    frozen = _utc(frozen_at, "frozen_at")
    start = _utc(prospective_start_at, "prospective_start_at")
    training_cutoff = max(
        _utc(row.prediction_cutoff, "prediction_cutoff") for row in values
    )
    if frozen <= training_cutoff or start <= frozen:
        raise ValueError("candidate must freeze after training and before collection")
    scores = np.asarray([row.pure_lineup_score for row in values], dtype=np.float64)
    probabilities = np.asarray([row.team_probability for row in values], dtype=np.float64)
    outcomes = np.asarray([row.radiant_win for row in values], dtype=np.float64)
    score_mean = float(np.mean(scores))
    score_scale = float(np.std(scores))
    if score_scale <= 0.0:
        raise ValueError("candidate score scale is zero")
    beta, fit_loss = _fit_beta(
        (scores - score_mean) / score_scale,
        probabilities,
        outcomes,
    )
    profile = prospective_rosh_profile()
    candidate = ProspectiveRoshCandidate(
        artifact_version=PROSPECTIVE_ROSH_ARTIFACT_VERSION,
        candidate_version=PROSPECTIVE_ROSH_CANDIDATE_VERSION,
        formula=PROSPECTIVE_ROSH_FORMULA,
        labels=PROSPECTIVE_ROSH_LABELS,
        retrospective_formula_version=LEGACY_ROSH_FORMULA_VERSION,
        prospective_profile_id=PROSPECTIVE_ROSH_PROFILE_ID,
        prospective_profile_hash=str(profile["profile_hash"]),
        scorer_source_hash=str(profile["scorer_source_hash"]),
        training_support=len(values),
        training_cohort_hash=_cohort_hash(values),
        training_cutoff=training_cutoff,
        frozen_at=frozen,
        prospective_start_at=start,
        score_mean=score_mean,
        score_scale=score_scale,
        beta_rosh=beta,
        fit_log_loss=fit_loss,
        folds=_folds(values),
        evaluation_plan=PROSPECTIVE_EVALUATION_PLAN,
        artifact_hash="",
    )
    return replace(
        candidate,
        artifact_hash=_hash(candidate.to_payload(include_hash=False)),
    )


def verify_prospective_rosh_candidate(
    candidate: ProspectiveRoshCandidate,
    *,
    require_current_profile: bool = True,
) -> None:
    if not isinstance(candidate, ProspectiveRoshCandidate):
        raise ValueError("candidate must be a ProspectiveRoshCandidate")
    if (
        candidate.artifact_version != PROSPECTIVE_ROSH_ARTIFACT_VERSION
        or candidate.candidate_version != PROSPECTIVE_ROSH_CANDIDATE_VERSION
        or candidate.formula != PROSPECTIVE_ROSH_FORMULA
        or candidate.labels != PROSPECTIVE_ROSH_LABELS
        or candidate.retrospective_formula_version != LEGACY_ROSH_FORMULA_VERSION
        or candidate.prospective_profile_id != PROSPECTIVE_ROSH_PROFILE_ID
        or candidate.training_support != 513
        or candidate.score_scale <= 0.0
        or candidate.beta_rosh >= 0.0
        or len(candidate.folds) != 5
        or dict(candidate.evaluation_plan) != PROSPECTIVE_EVALUATION_PLAN
    ):
        raise ValueError("candidate contract is not frozen")
    for field in (
        "training_cohort_hash",
        "prospective_profile_hash",
        "scorer_source_hash",
        "artifact_hash",
    ):
        _digest(getattr(candidate, field), field)
    for field in ("score_mean", "score_scale", "beta_rosh", "fit_log_loss"):
        _finite(getattr(candidate, field), field)
    training_cutoff = _utc(candidate.training_cutoff, "training_cutoff")
    frozen_at = _utc(candidate.frozen_at, "frozen_at")
    prospective_start_at = _utc(
        candidate.prospective_start_at,
        "prospective_start_at",
    )
    if frozen_at <= training_cutoff:
        raise ValueError("candidate frozen_at does not follow training cutoff")
    if prospective_start_at <= frozen_at:
        raise ValueError("candidate prospective start must follow freeze")
    if [row.fold for row in candidate.folds] != [1, 2, 3, 4, 5]:
        raise ValueError("candidate folds are not the frozen five-fold sequence")
    if (
        any(
            row.train_support <= 0
            or row.test_support <= 0
            or row.train_support + row.test_support != candidate.training_support
            or row.train_scale <= 0.0
            or row.beta_rosh >= 0.0
            for row in candidate.folds
        )
        or sum(row.test_support for row in candidate.folds)
        != candidate.training_support
    ):
        raise ValueError("candidate fold contract is not frozen")
    for row in candidate.folds:
        for field in ("train_mean", "train_scale", "beta_rosh"):
            _finite(getattr(row, field), f"fold {field}")
    expected_hash = _hash(candidate.to_payload(include_hash=False))
    if not hmac.compare_digest(expected_hash, candidate.artifact_hash):
        raise ValueError("candidate artifact hash mismatch")
    if require_current_profile:
        profile = prospective_rosh_profile()
        if (
            profile.get("formula_version") != LEGACY_ROSH_FORMULA_VERSION
            or profile.get("scorer_family") != PROSPECTIVE_ROSH_SCORER_FAMILY
            or profile.get("official_v2_compatible") is not False
            or candidate.retrospective_formula_version
            != profile.get("formula_version")
            or
            candidate.prospective_profile_id != profile["profile_id"]
            or candidate.prospective_profile_hash != profile["profile_hash"]
            or candidate.scorer_source_hash != profile["scorer_source_hash"]
        ):
            raise ValueError("candidate prospective profile drift")


def candidate_probability(
    candidate: ProspectiveRoshCandidate,
    *,
    team_probability: float,
    pure_rosh_score: float,
) -> tuple[float, float, float]:
    verify_prospective_rosh_candidate(candidate)
    p0 = _finite(team_probability, "team_probability")
    score = _finite(pure_rosh_score, "pure_rosh_score")
    if not 0.0 < p0 < 1.0:
        raise ValueError("team_probability must be strictly between zero and one")
    standardized = (score - candidate.score_mean) / candidate.score_scale
    contribution = -candidate.beta_rosh * standardized
    probability = float(expit(logit(p0) + contribution))
    return probability, standardized, contribution


_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_version",
        "candidate_version",
        "formula",
        "labels",
        "retrospective_formula_version",
        "prospective_profile_id",
        "prospective_profile_hash",
        "scorer_source_hash",
        "training_support",
        "training_cohort_hash",
        "training_cutoff",
        "frozen_at",
        "prospective_start_at",
        "score_mean",
        "score_scale",
        "beta_rosh",
        "fit_log_loss",
        "folds",
        "fold_beta_range",
        "evaluation_plan",
        "deployment_eligible",
        "artifact_hash",
    }
)


def load_prospective_rosh_candidate_json(
    payload_json: str,
    *,
    require_current_profile: bool = True,
) -> ProspectiveRoshCandidate:
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("candidate artifact JSON is invalid") from error
    if not isinstance(payload, dict) or frozenset(payload) != _ARTIFACT_FIELDS:
        raise ValueError("candidate artifact fields do not match")
    folds_raw = payload["folds"]
    if not isinstance(folds_raw, list):
        raise ValueError("candidate folds must be an array")
    if len(folds_raw) != 5 or any(not isinstance(row, Mapping) for row in folds_raw):
        raise ValueError("candidate folds must contain five objects")
    folds = tuple(
        CandidateFold(
            fold=int(row["fold"]),
            train_support=int(row["train_support"]),
            test_support=int(row["test_support"]),
            train_mean=_finite(row["train_mean"], "fold train_mean"),
            train_scale=_finite(row["train_scale"], "fold train_scale"),
            beta_rosh=_finite(row["beta_rosh"], "fold beta_rosh"),
        )
        for row in folds_raw
    )
    candidate = ProspectiveRoshCandidate(
        artifact_version=str(payload["artifact_version"]),
        candidate_version=str(payload["candidate_version"]),
        formula=str(payload["formula"]),
        labels=tuple(str(value) for value in payload["labels"]),
        retrospective_formula_version=str(payload["retrospective_formula_version"]),
        prospective_profile_id=str(payload["prospective_profile_id"]),
        prospective_profile_hash=str(payload["prospective_profile_hash"]),
        scorer_source_hash=str(payload["scorer_source_hash"]),
        training_support=int(payload["training_support"]),
        training_cohort_hash=str(payload["training_cohort_hash"]),
        training_cutoff=_parse_utc(payload["training_cutoff"], "training_cutoff"),
        frozen_at=_parse_utc(payload["frozen_at"], "frozen_at"),
        prospective_start_at=_parse_utc(
            payload["prospective_start_at"], "prospective_start_at"
        ),
        score_mean=_finite(payload["score_mean"], "score_mean"),
        score_scale=_finite(payload["score_scale"], "score_scale"),
        beta_rosh=_finite(payload["beta_rosh"], "beta_rosh"),
        fit_log_loss=_finite(payload["fit_log_loss"], "fit_log_loss"),
        folds=folds,
        evaluation_plan=(
            dict(payload["evaluation_plan"])
            if isinstance(payload["evaluation_plan"], Mapping)
            else {}
        ),
        artifact_hash=str(payload["artifact_hash"]),
    )
    if payload["deployment_eligible"] is not False:
        raise ValueError("candidate must not be deployment eligible")
    if payload["fold_beta_range"] != {
        "minimum": min(row.beta_rosh for row in folds),
        "maximum": max(row.beta_rosh for row in folds),
    }:
        raise ValueError("candidate fold beta range mismatch")
    verify_prospective_rosh_candidate(
        candidate,
        require_current_profile=require_current_profile,
    )
    if canonical_json_bytes(payload) != candidate.canonical_bytes():
        raise ValueError("candidate artifact JSON is not canonical")
    return candidate


def load_frozen_prospective_rosh_candidate() -> ProspectiveRoshCandidate:
    """Load and verify the committed research-only candidate artifact."""

    try:
        payload = _FROZEN_CANDIDATE_PATH.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("frozen prospective R.O.S.H. candidate is unavailable") from error
    return load_prospective_rosh_candidate_json(payload.rstrip("\n"))


__all__ = [
    "PROSPECTIVE_EVALUATION_PLAN",
    "PROSPECTIVE_ROSH_ARTIFACT_VERSION",
    "PROSPECTIVE_ROSH_CANDIDATE_VERSION",
    "PROSPECTIVE_ROSH_FORMULA",
    "PROSPECTIVE_ROSH_LABELS",
    "PROSPECTIVE_ROSH_PROFILE_ID",
    "PROSPECTIVE_ROSH_SCORER_FAMILY",
    "CandidateFold",
    "ProspectiveRoshCandidate",
    "candidate_probability",
    "freeze_prospective_rosh_candidate",
    "load_frozen_prospective_rosh_candidate",
    "load_prospective_rosh_candidate_json",
    "prospective_rosh_profile",
    "verify_prospective_rosh_candidate",
]
