"""Fail-closed feature projection for the frozen official R.O.S.H. scorer."""

from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence

from live_betting.rosh_evidence import official_rosh_draft_hash
from live_betting.rosh_parity_storage import (
    RoshRunMatchLink,
    StoredRoshRun,
)
from prematch.stratz_official_profile import (
    RoshRequestPlan,
    build_official_request_plan,
    canonical_bytes,
    get_profile,
    validate_canonical_request_plan,
)
from prematch.stratz_official_score import (
    ALL_RANK_FALLBACK,
    NormalizedRoshInputs,
    OfficialRoshResult,
    normalize_official_responses,
    result_projection,
    score_official_rosh,
)

from .draft_features import AvailabilityMode


UTC = timezone.utc
ROSH_FEATURE_VERSION = "official-rosh-features-v1"
ROSH_AUTHORITY_SCHEMA = "official-rosh-feature-authority/v1"
ROSH_REQUEST_PLAN_WITNESS_SCHEMA = "official-rosh-request-plan-witness/v1"
ROSH_PROSPECTIVE_TIMING_SCHEMA = "official-rosh-prospective-timing/v1"

ROSH_FEATURE_SCHEMA = (
    "relative_advantage",
    "score_20",
    "score_30",
    "score_40",
    "score_50",
    "slope_20_40",
    "slope_30_50",
    "curve_min",
    "curve_max",
    "curve_range",
    "direction_flip_count",
    "position_min_support",
    "synergy_min_support",
    "rank_fallback_ratio",
    "coverage",
)
ROSH_MODEL_SCHEMA = tuple(
    projected
    for name in ROSH_FEATURE_SCHEMA
    for projected in (name, f"{name}__missing")
)
ROSH_MODEL_SCHEMA_HASH = hashlib.sha256(
    canonical_bytes(list(ROSH_MODEL_SCHEMA))
).hexdigest()

_AVAILABLE = "available"
_UNAVAILABLE = "unavailable"
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MATCH_LINK_SOURCES = frozenset({"raybet", "opendota", "stratz"})
_AUTHORITY_FIELDS = (
    "schema",
    "feature_version",
    "target",
    "requested_run_id",
    "candidate_run_ids",
    "match_links",
    "request_plan_witness",
    "prospective_timing",
)


class _UnavailableEvidence(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _unavailable(reason: str) -> None:
    raise _UnavailableEvidence(reason)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


def _timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat()


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def _exact_object(
    value: object,
    fields: Sequence[str],
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise ValueError(f"{field} fields do not match the schema")
    return value


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _hero_ids(value: Iterable[int], field: str) -> tuple[int, ...]:
    result = tuple(value)
    if len(result) != 5:
        raise ValueError(f"{field} must contain exactly five heroes")
    for hero_id in result:
        _positive_int(hero_id, field)
    return result


@dataclass(frozen=True)
class RoshFeatureTarget:
    match_id: int
    date_time: int
    prediction_cutoff: datetime
    availability_mode: str
    radiant_hero_ids: tuple[int, ...]
    dire_hero_ids: tuple[int, ...]
    match_source: str | None = None
    source_match_id: str | None = None

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "R.O.S.H. target match_id")
        _positive_int(self.date_time, "R.O.S.H. target date_time")
        object.__setattr__(
            self,
            "prediction_cutoff",
            _utc(self.prediction_cutoff, "R.O.S.H. prediction_cutoff"),
        )
        mode = AvailabilityMode(self.availability_mode)
        object.__setattr__(self, "availability_mode", mode.value)
        radiant = _hero_ids(self.radiant_hero_ids, "radiant_hero_ids")
        dire = _hero_ids(self.dire_hero_ids, "dire_hero_ids")
        if len(set((*radiant, *dire))) != 10:
            raise ValueError("R.O.S.H. target heroes must be unique")
        object.__setattr__(self, "radiant_hero_ids", radiant)
        object.__setattr__(self, "dire_hero_ids", dire)
        if mode is AvailabilityMode.RECONSTRUCTED:
            if self.match_source is not None or self.source_match_id is not None:
                raise ValueError(
                    "reconstructed R.O.S.H. target cannot use a live match link"
                )
        else:
            source = _nonempty(self.match_source, "match_source")
            if source not in _MATCH_LINK_SOURCES:
                raise ValueError("unsupported R.O.S.H. match link source")
            object.__setattr__(self, "match_source", source)
            object.__setattr__(
                self,
                "source_match_id",
                _nonempty(self.source_match_id, "source_match_id"),
            )

    @property
    def draft_hash(self) -> str:
        return official_rosh_draft_hash(
            self.radiant_hero_ids,
            self.dire_hero_ids,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "match_id": self.match_id,
            "date_time": self.date_time,
            "prediction_cutoff": self.prediction_cutoff.isoformat(),
            "availability_mode": self.availability_mode,
            "radiant_hero_ids": list(self.radiant_hero_ids),
            "dire_hero_ids": list(self.dire_hero_ids),
            "match_source": self.match_source,
            "source_match_id": self.source_match_id,
        }


@dataclass(frozen=True)
class RoshRequestPlanWitness:
    run_id: str
    request_started_at: datetime
    request_hash: str
    request_artifact_hash: str

    def __post_init__(self) -> None:
        for name in ("run_id", "request_hash", "request_artifact_hash"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(
            self,
            "request_started_at",
            _utc(self.request_started_at, "request_started_at"),
        )

    @classmethod
    def from_run(
        cls,
        run: StoredRoshRun,
        *,
        request_started_at: datetime,
    ) -> RoshRequestPlanWitness:
        artifact = run.run.request_manifest.get("request_artifact")
        if not isinstance(artifact, Mapping):
            raise ValueError("run has no archived request artifact")
        return cls(
            run_id=run.run.run_id,
            request_started_at=request_started_at,
            request_hash=run.run.request_hash,
            request_artifact_hash=_sha256(
                artifact.get("content_sha256"),
                "request_artifact.content_sha256",
            ),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": ROSH_REQUEST_PLAN_WITNESS_SCHEMA,
            "run_id": self.run_id,
            "request_started_at": self.request_started_at.isoformat(),
            "request_hash": self.request_hash,
            "request_artifact_hash": self.request_artifact_hash,
        }


@dataclass(frozen=True, order=True)
class RoshResponseTiming:
    operation_index: int
    operation_name: str
    response_artifact_hash: str
    first_usable_at: datetime

    def __post_init__(self) -> None:
        _nonnegative_int(self.operation_index, "operation_index")
        object.__setattr__(
            self,
            "operation_name",
            _nonempty(self.operation_name, "operation_name"),
        )
        object.__setattr__(
            self,
            "response_artifact_hash",
            _sha256(self.response_artifact_hash, "response_artifact_hash"),
        )
        object.__setattr__(
            self,
            "first_usable_at",
            _utc(self.first_usable_at, "response first_usable_at"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "operation_index": self.operation_index,
            "operation_name": self.operation_name,
            "response_artifact_hash": self.response_artifact_hash,
            "first_usable_at": self.first_usable_at.isoformat(),
        }


@dataclass(frozen=True)
class RoshProspectiveTimingAuthority:
    run_id: str
    request_started_at: datetime
    request_first_usable_at: datetime
    request_hash: str
    request_artifact_hash: str
    responses: tuple[RoshResponseTiming, ...]

    def __post_init__(self) -> None:
        for name in ("run_id", "request_hash", "request_artifact_hash"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        started = _utc(self.request_started_at, "request_started_at")
        usable = _utc(
            self.request_first_usable_at,
            "request first_usable_at",
        )
        if usable < started:
            raise ValueError("request first_usable_at cannot precede request start")
        responses = tuple(sorted(self.responses))
        if not responses or tuple(row.operation_index for row in responses) != tuple(
            range(len(responses))
        ):
            raise ValueError("response timing operations must be complete and ordered")
        if any(row.first_usable_at < started for row in responses):
            raise ValueError("response first_usable_at cannot precede request start")
        object.__setattr__(self, "request_started_at", started)
        object.__setattr__(self, "request_first_usable_at", usable)
        object.__setattr__(self, "responses", responses)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": ROSH_PROSPECTIVE_TIMING_SCHEMA,
            "run_id": self.run_id,
            "request_started_at": self.request_started_at.isoformat(),
            "request_first_usable_at": self.request_first_usable_at.isoformat(),
            "request_hash": self.request_hash,
            "request_artifact_hash": self.request_artifact_hash,
            "responses": [row.to_payload() for row in self.responses],
        }


@dataclass(frozen=True)
class RoshFeatureSnapshot:
    match_id: int
    prediction_cutoff: datetime
    availability_mode: str
    status: str
    missing_reason: str | None
    feature_version: str
    formula_version: str | None
    profile_hash: str | None
    result_hash: str | None
    run_id: str | None
    evidence_hash: str | None
    relative_advantage: float | None
    score_20: float | None
    score_30: float | None
    score_40: float | None
    score_50: float | None
    slope_20_40: float | None
    slope_30_50: float | None
    curve_min: float | None
    curve_max: float | None
    curve_range: float | None
    direction_flip_count: int | None
    position_min_support: int | None
    synergy_min_support: int | None
    rank_fallback_ratio: float | None
    coverage: float
    input_hash: str

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "R.O.S.H. snapshot match_id")
        object.__setattr__(
            self,
            "prediction_cutoff",
            _utc(self.prediction_cutoff, "R.O.S.H. snapshot prediction_cutoff"),
        )
        AvailabilityMode(self.availability_mode)
        if self.status not in {_AVAILABLE, _UNAVAILABLE}:
            raise ValueError("unsupported R.O.S.H. snapshot status")
        if self.feature_version != ROSH_FEATURE_VERSION:
            raise ValueError("unsupported R.O.S.H. feature version")
        for name in ("profile_hash", "result_hash", "run_id", "evidence_hash"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _sha256(value, name))
        if self.formula_version is not None:
            object.__setattr__(
                self,
                "formula_version",
                _nonempty(self.formula_version, "formula_version"),
            )
        for name in ROSH_FEATURE_SCHEMA:
            value = getattr(self, name)
            if name in {
                "direction_flip_count",
                "position_min_support",
                "synergy_min_support",
            }:
                if value is not None:
                    _nonnegative_int(value, name)
            elif value is not None:
                object.__setattr__(self, name, _finite(value, name))
        if not 0.0 <= self.coverage <= 1.0:
            raise ValueError("R.O.S.H. coverage must be between zero and one")
        object.__setattr__(self, "input_hash", _sha256(self.input_hash, "input_hash"))
        identities = (
            self.formula_version,
            self.profile_hash,
            self.result_hash,
            self.run_id,
            self.evidence_hash,
        )
        unavailable_signals = tuple(
            getattr(self, name) for name in ROSH_FEATURE_SCHEMA if name != "coverage"
        )
        if self.status == _AVAILABLE:
            if self.missing_reason is not None or any(
                value is None for value in identities
            ):
                raise ValueError("available R.O.S.H. snapshot has incomplete identity")
            if (
                self.relative_advantage is None
                or self.direction_flip_count is None
                or self.position_min_support is None
                or self.synergy_min_support is None
            ):
                raise ValueError(
                    "available R.O.S.H. snapshot has incomplete core signals"
                )
        else:
            _nonempty(self.missing_reason, "missing_reason")
            if any(value is not None for value in (*identities, *unavailable_signals)):
                raise ValueError(
                    "unavailable R.O.S.H. snapshot must not claim evidence"
                )
            if self.coverage != 0.0:
                raise ValueError("unavailable R.O.S.H. snapshot coverage must be zero")


def project_rosh_features(
    snapshot: RoshFeatureSnapshot,
) -> dict[str, float | None]:
    """Project the fixed M4 signal schema for the downstream M5 model."""

    if not isinstance(snapshot, RoshFeatureSnapshot):
        raise ValueError("snapshot must be a RoshFeatureSnapshot")
    projected: dict[str, float | None] = {}
    for name in ROSH_FEATURE_SCHEMA:
        raw = getattr(snapshot, name)
        projected[name] = None if raw is None else float(raw)
        projected[f"{name}__missing"] = 1.0 if raw is None else 0.0
    if tuple(projected) != ROSH_MODEL_SCHEMA:
        raise AssertionError("R.O.S.H. projection does not match model schema")
    return projected


def _target_from_payload(value: object) -> RoshFeatureTarget:
    row = _exact_object(
        value,
        (
            "match_id",
            "date_time",
            "prediction_cutoff",
            "availability_mode",
            "radiant_hero_ids",
            "dire_hero_ids",
            "match_source",
            "source_match_id",
        ),
        "R.O.S.H. target",
    )
    radiant = row["radiant_hero_ids"]
    dire = row["dire_hero_ids"]
    if not isinstance(radiant, list) or not isinstance(dire, list):
        raise ValueError("R.O.S.H. target heroes must be arrays")
    return RoshFeatureTarget(
        match_id=row["match_id"],
        date_time=row["date_time"],
        prediction_cutoff=_parse_utc(
            row["prediction_cutoff"],
            "prediction_cutoff",
        ),
        availability_mode=row["availability_mode"],
        radiant_hero_ids=tuple(radiant),
        dire_hero_ids=tuple(dire),
        match_source=row["match_source"],
        source_match_id=row["source_match_id"],
    )


def _witness_from_payload(value: object) -> RoshRequestPlanWitness:
    row = _exact_object(
        value,
        (
            "schema",
            "run_id",
            "request_started_at",
            "request_hash",
            "request_artifact_hash",
        ),
        "R.O.S.H. request-plan witness",
    )
    if row["schema"] != ROSH_REQUEST_PLAN_WITNESS_SCHEMA:
        raise ValueError("unsupported R.O.S.H. request-plan witness")
    return RoshRequestPlanWitness(
        run_id=row["run_id"],
        request_started_at=_parse_utc(
            row["request_started_at"],
            "request_started_at",
        ),
        request_hash=row["request_hash"],
        request_artifact_hash=row["request_artifact_hash"],
    )


def _response_timing_from_payload(value: object) -> RoshResponseTiming:
    row = _exact_object(
        value,
        (
            "operation_index",
            "operation_name",
            "response_artifact_hash",
            "first_usable_at",
        ),
        "R.O.S.H. response timing",
    )
    return RoshResponseTiming(
        operation_index=row["operation_index"],
        operation_name=row["operation_name"],
        response_artifact_hash=row["response_artifact_hash"],
        first_usable_at=_parse_utc(
            row["first_usable_at"],
            "response first_usable_at",
        ),
    )


def _timing_from_payload(value: object) -> RoshProspectiveTimingAuthority:
    row = _exact_object(
        value,
        (
            "schema",
            "run_id",
            "request_started_at",
            "request_first_usable_at",
            "request_hash",
            "request_artifact_hash",
            "responses",
        ),
        "R.O.S.H. prospective timing authority",
    )
    if row["schema"] != ROSH_PROSPECTIVE_TIMING_SCHEMA:
        raise ValueError("unsupported R.O.S.H. prospective timing authority")
    responses = row["responses"]
    if not isinstance(responses, list):
        raise ValueError("R.O.S.H. response timing must be an array")
    return RoshProspectiveTimingAuthority(
        run_id=row["run_id"],
        request_started_at=_parse_utc(
            row["request_started_at"],
            "request_started_at",
        ),
        request_first_usable_at=_parse_utc(
            row["request_first_usable_at"],
            "request first_usable_at",
        ),
        request_hash=row["request_hash"],
        request_artifact_hash=row["request_artifact_hash"],
        responses=tuple(_response_timing_from_payload(item) for item in responses),
    )


def _link_payload(link: RoshRunMatchLink) -> dict[str, object]:
    if not isinstance(link, RoshRunMatchLink):
        raise ValueError("R.O.S.H. match links must be RoshRunMatchLink values")
    _nonempty(link.source, "match link source")
    _nonempty(link.source_match_id, "source_match_id")
    _sha256(link.run_id, "match link run_id")
    if link.map_number is not None:
        _positive_int(link.map_number, "map_number")
    linked_at = _parse_utc(link.linked_at, "linked_at")
    return {
        "source": link.source,
        "source_match_id": link.source_match_id,
        "run_id": link.run_id,
        "map_number": link.map_number,
        "linked_at": linked_at.isoformat(),
    }


def _link_payloads(links: Iterable[RoshRunMatchLink]) -> list[dict[str, object]]:
    payloads = [_link_payload(link) for link in links]
    payloads.sort(
        key=lambda row: (
            str(row["run_id"]),
            str(row["source"]),
            str(row["source_match_id"]),
            -1 if row["map_number"] is None else int(row["map_number"]),
            str(row["linked_at"]),
        )
    )
    if len({_hash(row) for row in payloads}) != len(payloads):
        raise ValueError("duplicate R.O.S.H. match link")
    return payloads


def _parse_json_bytes(body: bytes, field: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{field} contains duplicate object keys")
            result[key] = value
        return result

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _UnavailableEvidence(f"{field}_invalid") from error
    _validate_finite_json(value, field)
    return value


def _validate_finite_json(value: object, field: str) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_finite_json(item, field)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json(item, field)
    elif isinstance(value, float) and not math.isfinite(value):
        _unavailable(f"{field}_invalid")


def _safe_artifact_path(root: Path, relative_path: object) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        _unavailable("artifact_manifest_incomplete")
    if "\\" in relative_path:
        _unavailable("artifact_path_invalid")
    posix = PurePosixPath(relative_path)
    windows = PureWindowsPath(relative_path)
    if (
        posix == PurePosixPath(".")
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or posix.as_posix() != relative_path
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        _unavailable("artifact_path_invalid")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = root.joinpath(*posix.parts).resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        _unavailable("artifact_missing")
    if not resolved_path.is_file():
        _unavailable("artifact_missing")
    return resolved_path


def _load_exact_artifact(
    root: Path,
    *,
    relative_path: object,
    content_sha256: object,
    gzip_sha256: object,
    byte_count: object | None = None,
) -> bytes:
    try:
        expected_content = _sha256(content_sha256, "content_sha256")
        expected_gzip = _sha256(gzip_sha256, "gzip_sha256")
    except ValueError:
        _unavailable("artifact_manifest_incomplete")
    expected_path = PurePosixPath(
        "sha256",
        expected_content[:2],
        f"{expected_content}.json.gz",
    ).as_posix()
    if relative_path != expected_path:
        _unavailable("artifact_path_invalid")
    path = _safe_artifact_path(root, relative_path)
    try:
        compressed = path.read_bytes()
    except OSError:
        _unavailable("artifact_missing")
    if len(compressed) > _MAX_ARTIFACT_BYTES:
        _unavailable("artifact_too_large")
    if not hmac.compare_digest(hashlib.sha256(compressed).hexdigest(), expected_gzip):
        _unavailable("artifact_gzip_hash_mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            body = stream.read(_MAX_ARTIFACT_BYTES + 1)
    except (OSError, EOFError, gzip.BadGzipFile):
        _unavailable("artifact_gzip_invalid")
    if len(body) > _MAX_ARTIFACT_BYTES:
        _unavailable("artifact_too_large")
    if byte_count is not None:
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            _unavailable("artifact_manifest_incomplete")
        if byte_count != len(body):
            _unavailable("artifact_byte_count_mismatch")
    if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected_content):
        _unavailable("artifact_content_hash_mismatch")
    return body


def _run_map(runs: Iterable[StoredRoshRun]) -> dict[str, StoredRoshRun]:
    result: dict[str, StoredRoshRun] = {}
    for stored in runs:
        if not isinstance(stored, StoredRoshRun):
            raise ValueError("R.O.S.H. candidates must be StoredRoshRun values")
        run_id = _sha256(stored.run.run_id, "candidate run_id")
        previous = result.get(run_id)
        if previous is not None and previous != stored:
            raise ValueError("conflicting R.O.S.H. candidate run")
        result[run_id] = stored
    return result


def _authority_payload(
    target: RoshFeatureTarget,
    runs: Mapping[str, StoredRoshRun],
    links: Sequence[RoshRunMatchLink],
    *,
    run_id: str | None,
    request_plan_witness: RoshRequestPlanWitness | None,
    prospective_timing: RoshProspectiveTimingAuthority | None,
) -> dict[str, object]:
    if not isinstance(target, RoshFeatureTarget):
        raise ValueError("target must be a RoshFeatureTarget")
    requested = None if run_id is None else _sha256(run_id, "requested run_id")
    unknown_links = {link.run_id for link in links}.difference(runs)
    if unknown_links:
        raise ValueError("R.O.S.H. match link references an unknown candidate")
    if request_plan_witness is not None and not isinstance(
        request_plan_witness,
        RoshRequestPlanWitness,
    ):
        raise ValueError("request_plan_witness has the wrong type")
    if prospective_timing is not None and not isinstance(
        prospective_timing,
        RoshProspectiveTimingAuthority,
    ):
        raise ValueError("prospective_timing has the wrong type")
    return {
        "schema": ROSH_AUTHORITY_SCHEMA,
        "feature_version": ROSH_FEATURE_VERSION,
        "target": target.to_payload(),
        "requested_run_id": requested,
        "candidate_run_ids": sorted(runs),
        "match_links": _link_payloads(links),
        "request_plan_witness": (
            None if request_plan_witness is None else request_plan_witness.to_payload()
        ),
        "prospective_timing": (
            None if prospective_timing is None else prospective_timing.to_payload()
        ),
    }


def _validated_authority(
    authority_payload: Mapping[str, Any],
) -> tuple[
    RoshFeatureTarget,
    str | None,
    tuple[str, ...],
    RoshRequestPlanWitness | None,
    RoshProspectiveTimingAuthority | None,
]:
    authority = _exact_object(
        authority_payload,
        _AUTHORITY_FIELDS,
        "R.O.S.H. feature authority",
    )
    if authority["schema"] != ROSH_AUTHORITY_SCHEMA:
        raise ValueError("unsupported R.O.S.H. authority schema")
    if authority["feature_version"] != ROSH_FEATURE_VERSION:
        raise ValueError("unsupported R.O.S.H. feature version")
    target = _target_from_payload(authority["target"])
    requested_raw = authority["requested_run_id"]
    requested = (
        None if requested_raw is None else _sha256(requested_raw, "requested_run_id")
    )
    candidate_raw = authority["candidate_run_ids"]
    if not isinstance(candidate_raw, list):
        raise ValueError("R.O.S.H. candidate run IDs must be an array")
    candidate_ids = tuple(_sha256(item, "candidate_run_id") for item in candidate_raw)
    if candidate_ids != tuple(sorted(set(candidate_ids))):
        raise ValueError("R.O.S.H. candidate run IDs are not canonical")
    links = authority["match_links"]
    if not isinstance(links, list):
        raise ValueError("R.O.S.H. match links must be an array")
    for raw in links:
        row = _exact_object(
            raw,
            ("source", "source_match_id", "run_id", "map_number", "linked_at"),
            "R.O.S.H. match link",
        )
        source = _nonempty(row["source"], "match link source")
        if source not in _MATCH_LINK_SOURCES:
            raise ValueError("unsupported R.O.S.H. match link source")
        _nonempty(row["source_match_id"], "source_match_id")
        _sha256(row["run_id"], "match link run_id")
        if row["map_number"] is not None:
            _positive_int(row["map_number"], "map_number")
        _parse_utc(row["linked_at"], "linked_at")
    if links != sorted(
        links,
        key=lambda row: (
            str(row["run_id"]),
            str(row["source"]),
            str(row["source_match_id"]),
            -1 if row["map_number"] is None else int(row["map_number"]),
            str(row["linked_at"]),
        ),
    ):
        raise ValueError("R.O.S.H. match links are not canonical")
    if len({_hash(row) for row in links}) != len(links):
        raise ValueError("duplicate R.O.S.H. match link")
    witness_raw = authority["request_plan_witness"]
    timing_raw = authority["prospective_timing"]
    witness = None if witness_raw is None else _witness_from_payload(witness_raw)
    timing = None if timing_raw is None else _timing_from_payload(timing_raw)
    mode = AvailabilityMode(target.availability_mode)
    if mode is AvailabilityMode.RECONSTRUCTED:
        if timing is not None:
            raise ValueError(
                "reconstructed authority cannot contain prospective timing"
            )
    elif witness is not None:
        raise ValueError("prospective authority cannot contain a reconstructed witness")
    if timing is not None and requested != timing.run_id:
        raise ValueError("prospective timing must select its bound run")
    return target, requested, candidate_ids, witness, timing


def _matching_links(
    target: RoshFeatureTarget,
    run_id: str,
    links: Sequence[RoshRunMatchLink],
) -> tuple[RoshRunMatchLink, ...]:
    if AvailabilityMode(target.availability_mode) is AvailabilityMode.RECONSTRUCTED:
        return ()
    return tuple(
        link
        for link in links
        if link.run_id == run_id
        and link.source == target.match_source
        and link.source_match_id == target.source_match_id
    )


def _matches_target_identity(
    stored: StoredRoshRun,
    target: RoshFeatureTarget,
) -> bool:
    run = stored.run
    mode = AvailabilityMode(target.availability_mode)
    if (
        run.status != "succeeded"
        or run.mode
        != (
            "historical_match"
            if mode is AvailabilityMode.RECONSTRUCTED
            else "explicit_draft"
        )
        or run.date_time != target.date_time
        or run.draft_hash != target.draft_hash
    ):
        return False
    if mode is AvailabilityMode.RECONSTRUCTED:
        return run.match_id == target.match_id
    return run.match_id is None


def _cutoff_legal_time(value: object, cutoff: datetime, field: str) -> bool:
    try:
        return _parse_utc(value, field) <= cutoff
    except ValueError:
        return False


def _bounded_authority_inputs(
    target: RoshFeatureTarget,
    runs: Mapping[str, StoredRoshRun],
    links: Sequence[RoshRunMatchLink],
    selected_run_id: str | None,
) -> tuple[dict[str, StoredRoshRun], tuple[RoshRunMatchLink, ...]]:
    mode = AvailabilityMode(target.availability_mode)
    bounded_runs: dict[str, StoredRoshRun] = {}
    for run_id, stored in runs.items():
        if selected_run_id is not None and run_id != selected_run_id:
            continue
        if not _matches_target_identity(stored, target):
            continue
        if mode is AvailabilityMode.PROSPECTIVE:
            if not _cutoff_legal_time(
                stored.run.collected_at,
                target.prediction_cutoff,
                "run collected_at",
            ):
                continue
            legal_links = tuple(
                link
                for link in _matching_links(target, run_id, links)
                if _cutoff_legal_time(
                    link.linked_at,
                    target.prediction_cutoff,
                    "linked_at",
                )
            )
            if not legal_links:
                continue
        bounded_runs[run_id] = stored
    if mode is AvailabilityMode.RECONSTRUCTED:
        return bounded_runs, ()
    bounded_links = tuple(
        link
        for link in links
        if link.run_id in bounded_runs
        and link.source == target.match_source
        and link.source_match_id == target.source_match_id
        and _cutoff_legal_time(
            link.linked_at,
            target.prediction_cutoff,
            "linked_at",
        )
    )
    return bounded_runs, bounded_links


def _select_run(
    target: RoshFeatureTarget,
    requested_run_id: str | None,
    runs: Mapping[str, StoredRoshRun],
) -> StoredRoshRun:
    if requested_run_id is not None:
        selected = runs.get(requested_run_id)
        if selected is None:
            _unavailable("requested_run_not_found")
        if not _matches_target_identity(selected, target):
            _unavailable("run_identity_mismatch")
        return selected
    matches = tuple(
        stored for stored in runs.values() if _matches_target_identity(stored, target)
    )
    if not matches:
        _unavailable("run_unavailable")
    if len(matches) != 1:
        _unavailable("ambiguous_runs")
    return matches[0]


def _active_profile_for_run(stored: StoredRoshRun) -> Any:
    try:
        profile = get_profile()
    except Exception:
        _unavailable("profile_unavailable")
    run = stored.run
    identities = {
        "rosh_profile_id": profile.rosh_profile_id,
        "formula_version": profile.formula_version,
        "request_profile_hash": profile.request_profile_hash,
        "upstream_bundle_hash": profile.upstream_bundle_hash,
        "scorer_source_hash": profile.scorer_source_hash,
        "canonical_profile_hash": profile.canonical_profile_hash,
        "serialization_version": profile.serialization_version,
    }
    if any(getattr(run, name) != expected for name, expected in identities.items()):
        _unavailable("profile_mismatch")
    return profile


def _analysis_input(target: RoshFeatureTarget) -> dict[str, object]:
    mode = AvailabilityMode(target.availability_mode)
    value: dict[str, object] = {
        "mode": (
            "historical_match"
            if mode is AvailabilityMode.RECONSTRUCTED
            else "explicit_draft"
        ),
        "date_time": target.date_time,
        "bracket_ids": ["IMMORTAL"],
    }
    if mode is AvailabilityMode.RECONSTRUCTED:
        value["match_id"] = target.match_id
    else:
        value["radiant"] = [
            {"hero_id": hero_id, "position_id": position_id}
            for position_id, hero_id in enumerate(target.radiant_hero_ids, 1)
        ]
        value["dire"] = [
            {"hero_id": hero_id, "position_id": position_id}
            for position_id, hero_id in enumerate(target.dire_hero_ids, 1)
        ]
    return value


def _request_started_at(
    stored: StoredRoshRun,
    target: RoshFeatureTarget,
    witness: RoshRequestPlanWitness | None,
    timing: RoshProspectiveTimingAuthority | None,
) -> datetime:
    mode = AvailabilityMode(target.availability_mode)
    if mode is AvailabilityMode.RECONSTRUCTED:
        if witness is None:
            _unavailable("request_plan_witness_unavailable")
        if (
            witness.run_id != stored.run.run_id
            or witness.request_hash != stored.run.request_hash
        ):
            _unavailable("request_plan_witness_mismatch")
        return witness.request_started_at
    if timing is None:
        _unavailable("prospective_timing_unavailable")
    if (
        timing.run_id != stored.run.run_id
        or timing.request_hash != stored.run.request_hash
    ):
        _unavailable("prospective_timing_mismatch")
    return timing.request_started_at


def _build_plan(
    stored: StoredRoshRun,
    target: RoshFeatureTarget,
    profile: Any,
    witness: RoshRequestPlanWitness | None,
    timing: RoshProspectiveTimingAuthority | None,
) -> RoshRequestPlan:
    started_at = _request_started_at(stored, target, witness, timing)
    try:
        plan = build_official_request_plan(
            _analysis_input(target),
            profile=profile,
            request_started_at=started_at,
        )
        validate_canonical_request_plan(plan)
    except ValueError:
        _unavailable("request_plan_mismatch")
    if plan.request_hash != stored.run.request_hash:
        _unavailable("request_hash_mismatch")
    return plan


def _operation_request_payload(plan: RoshRequestPlan) -> list[dict[str, object]]:
    return [
        {
            "operationName": operation.operation_name,
            "variables": _plain_json(operation.variables),
            "query": operation.query,
        }
        for operation in plan.operations
    ]


def _operation_manifest_payload(plan: RoshRequestPlan) -> list[dict[str, object]]:
    return [
        {
            "index": operation.index,
            "operation_name": operation.operation_name,
            "query_sha256": operation.query_sha256,
            "variables": _plain_json(operation.variables),
        }
        for operation in plan.operations
    ]


def _request_archive(
    root: Path,
    stored: StoredRoshRun,
    plan: RoshRequestPlan,
    witness: RoshRequestPlanWitness | None,
    timing: RoshProspectiveTimingAuthority | None,
) -> tuple[bytes, Mapping[str, Any]]:
    manifest = stored.run.request_manifest
    if not isinstance(manifest, Mapping):
        _unavailable("request_manifest_incomplete")
    artifact = manifest.get("request_artifact")
    operations = manifest.get("operations")
    if not isinstance(artifact, Mapping) or not isinstance(operations, list):
        _unavailable("request_manifest_incomplete")
    if (
        manifest.get("schema") != "rosh-request-manifest/v1"
        or manifest.get("request_hash") != plan.request_hash
        or canonical_bytes(operations)
        != canonical_bytes(_operation_manifest_payload(plan))
    ):
        _unavailable("request_manifest_mismatch")
    expected_artifact_hash = (
        witness.request_artifact_hash
        if witness is not None
        else timing.request_artifact_hash
    )
    if artifact.get("content_sha256") != expected_artifact_hash:
        _unavailable("request_artifact_identity_mismatch")
    body = _load_exact_artifact(
        root,
        relative_path=artifact.get("relative_path"),
        content_sha256=artifact.get("content_sha256"),
        gzip_sha256=artifact.get("gzip_sha256"),
        byte_count=artifact.get("byte_count"),
    )
    if manifest.get("request_body_sha256") != hashlib.sha256(body).hexdigest():
        _unavailable("request_body_hash_mismatch")
    parsed = _parse_json_bytes(body, "request_archive")
    if canonical_bytes(parsed) != canonical_bytes(_operation_request_payload(plan)):
        _unavailable("request_semantics_mismatch")
    return body, artifact


def _response_archive(
    root: Path,
    stored: StoredRoshRun,
    plan: RoshRequestPlan,
    request_artifact: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    manifest = tuple(stored.run.response_manifest)
    if len(manifest) != len(plan.operations):
        _unavailable("response_manifest_incomplete")
    try:
        run_collected_at = _parse_utc(stored.run.collected_at, "run collected_at")
    except ValueError:
        _unavailable("run_timestamp_invalid")
    response_identity: tuple[object, object, object] | None = None
    for operation, raw in zip(plan.operations, manifest, strict=True):
        if not isinstance(raw, Mapping):
            _unavailable("response_manifest_incomplete")
        if (
            raw.get("operation_index") != operation.index
            or raw.get("operation_name") != operation.operation_name
            or raw.get("request_artifact_hash")
            != request_artifact.get("content_sha256")
            or raw.get("request_relative_path") != request_artifact.get("relative_path")
        ):
            _unavailable("response_manifest_mismatch")
        try:
            response_collected_at = _parse_utc(
                raw.get("collected_at"),
                "response collected_at",
            )
        except ValueError:
            _unavailable("response_manifest_incomplete")
        if response_collected_at != run_collected_at:
            _unavailable("response_timestamp_mismatch")
        identity = (
            raw.get("response_artifact_hash"),
            raw.get("relative_path"),
            raw.get("response_gzip_sha256"),
        )
        if response_identity is None:
            response_identity = identity
        elif identity != response_identity:
            _unavailable("response_artifact_identity_mismatch")
    if response_identity is None:
        _unavailable("response_manifest_incomplete")
    body = _load_exact_artifact(
        root,
        relative_path=response_identity[1],
        content_sha256=response_identity[0],
        gzip_sha256=response_identity[2],
    )
    parsed = _parse_json_bytes(body, "response_archive")
    if not isinstance(parsed, list) or not all(
        isinstance(row, Mapping) for row in parsed
    ):
        _unavailable("response_archive_invalid")
    return tuple(parsed), manifest


def _validate_prospective_cutoff(
    target: RoshFeatureTarget,
    stored: StoredRoshRun,
    links: Sequence[RoshRunMatchLink],
    manifest: Sequence[Mapping[str, Any]],
    timing: RoshProspectiveTimingAuthority | None,
) -> None:
    if AvailabilityMode(target.availability_mode) is AvailabilityMode.RECONSTRUCTED:
        return
    if timing is None:
        _unavailable("prospective_timing_unavailable")
    cutoff = target.prediction_cutoff
    try:
        run_collected_at = _parse_utc(stored.run.collected_at, "run collected_at")
    except ValueError:
        _unavailable("run_timestamp_invalid")
    if (
        timing.request_started_at > cutoff
        or timing.request_first_usable_at > cutoff
        or run_collected_at > cutoff
    ):
        _unavailable("prospective_cutoff_violation")
    if len(timing.responses) != len(manifest):
        _unavailable("prospective_timing_incomplete")
    for response_timing, response_manifest in zip(
        timing.responses,
        manifest,
        strict=True,
    ):
        try:
            collected_at = _parse_utc(
                response_manifest.get("collected_at"),
                "response collected_at",
            )
        except ValueError:
            _unavailable("response_manifest_incomplete")
        if (
            response_timing.operation_index != response_manifest.get("operation_index")
            or response_timing.operation_name != response_manifest.get("operation_name")
            or response_timing.response_artifact_hash
            != response_manifest.get("response_artifact_hash")
        ):
            _unavailable("prospective_timing_mismatch")
        if (
            response_timing.first_usable_at < collected_at
            or response_timing.first_usable_at > cutoff
            or collected_at > cutoff
        ):
            _unavailable("prospective_cutoff_violation")
    matching_links = _matching_links(target, stored.run.run_id, links)
    if not matching_links:
        _unavailable("prospective_match_link_unavailable")
    try:
        linked_times = tuple(
            _parse_utc(link.linked_at, "linked_at") for link in matching_links
        )
    except ValueError:
        _unavailable("prospective_match_link_invalid")
    if min(linked_times) > cutoff:
        _unavailable("prospective_match_link_after_cutoff")


def _normalized_draft_projection(
    normalized: NormalizedRoshInputs,
) -> dict[str, list[dict[str, int]]]:
    return {
        side.lower(): [
            {"hero_id": slot.hero_id, "position_id": slot.position_id}
            for slot in normalized.draft
            if slot.team_side == side
        ]
        for side in ("RADIANT", "DIRE")
    }


def _validate_draft_identity(
    target: RoshFeatureTarget,
    stored: StoredRoshRun,
    normalized: NormalizedRoshInputs,
) -> None:
    projection = _normalized_draft_projection(normalized)
    radiant = tuple(row["hero_id"] for row in projection["radiant"])
    dire = tuple(row["hero_id"] for row in projection["dire"])
    if radiant != target.radiant_hero_ids or dire != target.dire_hero_ids:
        _unavailable("draft_identity_mismatch")
    try:
        replayed_hash = official_rosh_draft_hash(radiant, dire)
    except ValueError:
        _unavailable("draft_identity_mismatch")
    if (
        replayed_hash != target.draft_hash
        or replayed_hash != stored.run.draft_hash
        or canonical_bytes(projection) != canonical_bytes(stored.run.draft)
    ):
        _unavailable("draft_hash_mismatch")


def _stored_hero_projection(stored: StoredRoshRun) -> list[dict[str, object]]:
    return [
        {
            "team_side": row.team_side,
            "hero_id": row.hero_id,
            "position_id": row.position_id,
            "position_base_diff": row.components.get("position_base_diff"),
            "same_team_synergy": row.components.get("same_team_synergy"),
            "opponent_matchup_synergy": row.components.get("opponent_matchup_synergy"),
            "raw_score": row.raw_score,
            "display_score": row.display_score,
        }
        for row in stored.hero_scores
    ]


def _stored_minute_projection(stored: StoredRoshRun) -> list[dict[str, object]]:
    return [
        {
            "minute": row.minute,
            "radiant_time_delta": row.radiant_time_delta,
            "dire_time_delta": row.dire_time_delta,
            "synergy_delta": row.synergy_delta,
            "raw_score": row.raw_score,
            "display_score": row.display_score,
            "rank_source_counts": row.source_audit.get("rank_source_counts"),
            "slots": row.source_audit.get("slots"),
        }
        for row in stored.minute_points
    ]


def _validate_stored_result(
    stored: StoredRoshRun,
    result: OfficialRoshResult,
) -> None:
    scalar_projection = {
        "radiant_team_score": result.radiant_team_score,
        "dire_team_score": result.dire_team_score,
        "relative_advantage": result.relative_advantage,
    }
    stored_scalars = {
        "radiant_team_score": stored.run.radiant_team_score,
        "dire_team_score": stored.run.dire_team_score,
        "relative_advantage": stored.run.relative_advantage,
    }
    expected_heroes = [row.projection() for row in result.hero_scores]
    expected_minutes = [row.projection() for row in result.minute_points]
    stored_projection = {
        **scalar_projection,
        "hero_scores": expected_heroes,
        "minute_points": expected_minutes,
    }
    if stored.result is None or canonical_bytes(stored.result) != canonical_bytes(
        stored_projection
    ):
        _unavailable("stored_result_mismatch")
    if canonical_bytes(stored_scalars) != canonical_bytes(scalar_projection):
        _unavailable("stored_result_mismatch")
    if canonical_bytes(_stored_hero_projection(stored)) != canonical_bytes(
        expected_heroes
    ):
        _unavailable("stored_hero_rows_mismatch")
    if canonical_bytes(_stored_minute_projection(stored)) != canonical_bytes(
        expected_minutes
    ):
        _unavailable("stored_minute_rows_mismatch")


def _analysis_identity(
    plan: RoshRequestPlan,
    draft_hash: str,
) -> dict[str, object]:
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


def _validate_evidence_identity(
    stored: StoredRoshRun,
    plan: RoshRequestPlan,
    request_artifact: Mapping[str, Any],
    response_manifest: Sequence[Mapping[str, Any]],
    result: OfficialRoshResult,
) -> None:
    replayed_result_hash = _hash(result_projection(result))
    if not hmac.compare_digest(replayed_result_hash, result.result_hash):
        _unavailable("result_hash_mismatch")
    response_hashes = {row.get("response_artifact_hash") for row in response_manifest}
    if len(response_hashes) != 1:
        _unavailable("response_artifact_identity_mismatch")
    evidence_hash = _hash(
        {
            "schema": "rosh-analysis-evidence/v1",
            "analysis_identity": _analysis_identity(
                plan,
                stored.run.draft_hash,
            ),
            "request_artifact_hash": request_artifact.get("content_sha256"),
            "response_artifact_hash": next(iter(response_hashes)),
            "result_hash": result.result_hash,
            "status": "succeeded",
        }
    )
    if not hmac.compare_digest(evidence_hash, stored.run.evidence_hash):
        _unavailable("evidence_hash_mismatch")
    run_id = _hash(
        {
            "schema": "rosh-analysis-run-id/v1",
            "evidence_hash": evidence_hash,
            "status": "succeeded",
        }
    )
    if not hmac.compare_digest(run_id, stored.run.run_id):
        _unavailable("run_id_mismatch")


def _synergy_min_support(normalized: NormalizedRoshInputs) -> int:
    slots_by_side = {
        side: tuple(slot for slot in normalized.draft if slot.team_side == side)
        for side in ("RADIANT", "DIRE")
    }
    support_by_pair: dict[tuple[str, int, int], int] = {}
    for row in normalized.synergy_samples:
        key = (row.relation, row.hero_id, row.other_hero_id)
        support_by_pair[key] = support_by_pair.get(key, 0) + row.match_count
    required: list[int] = []
    for side in ("RADIANT", "DIRE"):
        opponent = "DIRE" if side == "RADIANT" else "RADIANT"
        for slot in slots_by_side[side]:
            required.extend(
                support_by_pair.get(("with", slot.hero_id, teammate.hero_id), 0)
                for teammate in slots_by_side[side]
                if teammate.hero_id != slot.hero_id
            )
            required.extend(
                support_by_pair.get(("vs", slot.hero_id, enemy.hero_id), 0)
                for enemy in slots_by_side[opponent]
            )
    return min(required) if required else 0


def _feature_signals(
    normalized: NormalizedRoshInputs,
    result: OfficialRoshResult,
) -> dict[str, float | int | None]:
    points = tuple(sorted(result.minute_points, key=lambda row: row.minute))
    by_minute = {row.minute: row.display_score for row in points}
    scores = {minute: by_minute.get(minute) for minute in (20, 30, 40, 50)}

    def slope(first: int, second: int) -> float | None:
        first_score = scores[first]
        second_score = scores[second]
        if first_score is None or second_score is None:
            return None
        return (second_score - first_score) / (second - first)

    curve = tuple(row.display_score for row in points)
    nonzero_signs = tuple(1 if value > 0.0 else -1 for value in curve if value != 0.0)
    flips = sum(
        previous != current
        for previous, current in zip(nonzero_signs, nonzero_signs[1:])
    )
    total_slots = sum(len(point.slots) for point in points)
    fallback_slots = sum(
        slot.source == ALL_RANK_FALLBACK for point in points for slot in point.slots
    )
    exact_count = sum(value is not None for value in scores.values())
    position_by_slot = {
        (row.hero_id, row.position_id): row.match_count
        for row in normalized.position_stats
    }
    required_position_support = tuple(
        position_by_slot.get((slot.hero_id, slot.position_id), 0)
        for slot in normalized.draft
    )
    return {
        "relative_advantage": result.relative_advantage,
        "score_20": scores[20],
        "score_30": scores[30],
        "score_40": scores[40],
        "score_50": scores[50],
        "slope_20_40": slope(20, 40),
        "slope_30_50": slope(30, 50),
        "curve_min": min(curve) if curve else None,
        "curve_max": max(curve) if curve else None,
        "curve_range": max(curve) - min(curve) if curve else None,
        "direction_flip_count": flips,
        "position_min_support": (
            min(required_position_support) if required_position_support else 0
        ),
        "synergy_min_support": _synergy_min_support(normalized),
        "rank_fallback_ratio": (fallback_slots / total_slots if total_slots else None),
        "coverage": exact_count / 4.0,
    }


def _available_snapshot(
    authority: Mapping[str, Any],
    target: RoshFeatureTarget,
    stored: StoredRoshRun,
    links: Sequence[RoshRunMatchLink],
    root: Path,
    witness: RoshRequestPlanWitness | None,
    timing: RoshProspectiveTimingAuthority | None,
) -> RoshFeatureSnapshot:
    profile = _active_profile_for_run(stored)
    plan = _build_plan(stored, target, profile, witness, timing)
    _request_body, request_artifact = _request_archive(
        root,
        stored,
        plan,
        witness,
        timing,
    )
    responses, response_manifest = _response_archive(
        root,
        stored,
        plan,
        request_artifact,
    )
    _validate_prospective_cutoff(
        target,
        stored,
        links,
        response_manifest,
        timing,
    )
    try:
        normalized = normalize_official_responses(plan, responses)
        result = score_official_rosh(normalized, profile)
    except ValueError:
        _unavailable("scorer_replay_failed")
    _validate_draft_identity(target, stored, normalized)
    _validate_stored_result(stored, result)
    _validate_evidence_identity(
        stored,
        plan,
        request_artifact,
        response_manifest,
        result,
    )
    signals = _feature_signals(normalized, result)
    input_hash = _hash(
        {
            "domain": "official-rosh-feature-input/v1",
            "authority": authority,
            "run_id": stored.run.run_id,
            "evidence_hash": stored.run.evidence_hash,
            "result_hash": result.result_hash,
            "signals": signals,
        }
    )
    return RoshFeatureSnapshot(
        match_id=target.match_id,
        prediction_cutoff=target.prediction_cutoff,
        availability_mode=target.availability_mode,
        status=_AVAILABLE,
        missing_reason=None,
        feature_version=ROSH_FEATURE_VERSION,
        formula_version=result.formula_version,
        profile_hash=profile.canonical_profile_hash,
        result_hash=result.result_hash,
        run_id=stored.run.run_id,
        evidence_hash=stored.run.evidence_hash,
        input_hash=input_hash,
        **signals,
    )


def _unavailable_snapshot(
    authority: Mapping[str, Any],
    target: RoshFeatureTarget,
    reason: str,
) -> RoshFeatureSnapshot:
    input_hash = _hash(
        {
            "domain": "official-rosh-feature-input/v1",
            "authority": authority,
            "status": _UNAVAILABLE,
            "missing_reason": reason,
        }
    )
    return RoshFeatureSnapshot(
        match_id=target.match_id,
        prediction_cutoff=target.prediction_cutoff,
        availability_mode=target.availability_mode,
        status=_UNAVAILABLE,
        missing_reason=reason,
        feature_version=ROSH_FEATURE_VERSION,
        formula_version=None,
        profile_hash=None,
        result_hash=None,
        run_id=None,
        evidence_hash=None,
        relative_advantage=None,
        score_20=None,
        score_30=None,
        score_40=None,
        score_50=None,
        slope_20_40=None,
        slope_30_50=None,
        curve_min=None,
        curve_max=None,
        curve_range=None,
        direction_flip_count=None,
        position_min_support=None,
        synergy_min_support=None,
        rank_fallback_ratio=None,
        coverage=0.0,
        input_hash=input_hash,
    )


def _snapshot_from_authority(
    authority: Mapping[str, Any],
    runs: Mapping[str, StoredRoshRun],
    links: Sequence[RoshRunMatchLink],
    artifact_root: Path,
) -> RoshFeatureSnapshot:
    target, requested, candidate_ids, witness, timing = _validated_authority(authority)
    bounded_runs, bounded_links = _bounded_authority_inputs(
        target,
        runs,
        links,
        requested,
    )
    if tuple(sorted(bounded_runs)) != candidate_ids:
        raise ValueError("bounded R.O.S.H. candidate authority does not match")
    if canonical_bytes(_link_payloads(bounded_links)) != canonical_bytes(
        authority["match_links"]
    ):
        raise ValueError("bounded R.O.S.H. match-link authority does not match")
    try:
        stored = _select_run(target, requested, bounded_runs)
        return _available_snapshot(
            authority,
            target,
            stored,
            bounded_links,
            artifact_root,
            witness,
            timing,
        )
    except _UnavailableEvidence as error:
        return _unavailable_snapshot(authority, target, error.reason)


def _effective_selected_run_id(
    run_id: str | None,
    prospective_timing: RoshProspectiveTimingAuthority | None,
) -> str | None:
    selected = None if run_id is None else _sha256(run_id, "requested run_id")
    if prospective_timing is None:
        return selected
    if not isinstance(prospective_timing, RoshProspectiveTimingAuthority):
        raise ValueError("prospective_timing has the wrong type")
    if selected is not None and selected != prospective_timing.run_id:
        raise ValueError("requested run_id conflicts with prospective timing")
    return prospective_timing.run_id


def build_rosh_feature_snapshot_with_authority(
    target: RoshFeatureTarget,
    runs: Iterable[StoredRoshRun],
    *,
    artifact_root: str | Path,
    match_links: Iterable[RoshRunMatchLink] = (),
    run_id: str | None = None,
    request_plan_witness: RoshRequestPlanWitness | None = None,
    prospective_timing: RoshProspectiveTimingAuthority | None = None,
) -> tuple[RoshFeatureSnapshot, dict[str, object]]:
    """Build a snapshot and the bounded external authority needed to replay it."""

    if not isinstance(target, RoshFeatureTarget):
        raise ValueError("target must be a RoshFeatureTarget")
    all_candidates = _run_map(runs)
    all_links = tuple(match_links)
    if any(not isinstance(link, RoshRunMatchLink) for link in all_links):
        raise ValueError("R.O.S.H. match links must be RoshRunMatchLink values")
    selected_run_id = _effective_selected_run_id(run_id, prospective_timing)
    candidates, links = _bounded_authority_inputs(
        target,
        all_candidates,
        all_links,
        selected_run_id,
    )
    authority = _authority_payload(
        target,
        candidates,
        links,
        run_id=selected_run_id,
        request_plan_witness=request_plan_witness,
        prospective_timing=prospective_timing,
    )
    return (
        _snapshot_from_authority(authority, candidates, links, Path(artifact_root)),
        authority,
    )


def build_rosh_feature_snapshot(
    target: RoshFeatureTarget,
    runs: Iterable[StoredRoshRun],
    *,
    artifact_root: str | Path,
    match_links: Iterable[RoshRunMatchLink] = (),
    run_id: str | None = None,
    request_plan_witness: RoshRequestPlanWitness | None = None,
    prospective_timing: RoshProspectiveTimingAuthority | None = None,
) -> RoshFeatureSnapshot:
    snapshot, _authority = build_rosh_feature_snapshot_with_authority(
        target,
        runs,
        artifact_root=artifact_root,
        match_links=match_links,
        run_id=run_id,
        request_plan_witness=request_plan_witness,
        prospective_timing=prospective_timing,
    )
    return snapshot


def replay_rosh_feature_snapshot(
    authority_payload: Mapping[str, Any],
    *,
    runs: Iterable[StoredRoshRun],
    artifact_root: str | Path,
    match_links: Iterable[RoshRunMatchLink] = (),
) -> RoshFeatureSnapshot:
    """Replay M4 from external run/link/archive authority and reject claim drift."""

    target, requested, candidate_ids, _witness, _timing = _validated_authority(
        authority_payload
    )
    all_candidates = _run_map(runs)
    all_links = tuple(match_links)
    if any(not isinstance(link, RoshRunMatchLink) for link in all_links):
        raise ValueError("R.O.S.H. match links must be RoshRunMatchLink values")
    candidates, links = _bounded_authority_inputs(
        target,
        all_candidates,
        all_links,
        requested,
    )
    if tuple(sorted(candidates)) != candidate_ids:
        raise ValueError("external R.O.S.H. candidate authority does not match")
    actual_links = _link_payloads(links)
    if canonical_bytes(actual_links) != canonical_bytes(
        authority_payload["match_links"]
    ):
        raise ValueError("external R.O.S.H. match-link authority does not match")
    return _snapshot_from_authority(
        authority_payload,
        candidates,
        links,
        Path(artifact_root),
    )


__all__ = [
    "ROSH_AUTHORITY_SCHEMA",
    "ROSH_FEATURE_SCHEMA",
    "ROSH_FEATURE_VERSION",
    "ROSH_MODEL_SCHEMA",
    "ROSH_MODEL_SCHEMA_HASH",
    "ROSH_PROSPECTIVE_TIMING_SCHEMA",
    "ROSH_REQUEST_PLAN_WITNESS_SCHEMA",
    "RoshFeatureSnapshot",
    "RoshFeatureTarget",
    "RoshProspectiveTimingAuthority",
    "RoshRequestPlanWitness",
    "RoshResponseTiming",
    "build_rosh_feature_snapshot",
    "build_rosh_feature_snapshot_with_authority",
    "project_rosh_features",
    "replay_rosh_feature_snapshot",
]
