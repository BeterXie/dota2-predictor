"""Append-only persistence adapter for official STRATZ R.O.S.H. runs."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from database.session import DatabaseRow, PostgresSession


_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*")
_MATCH_LINK_SOURCES = frozenset({"raybet", "opendota", "stratz"})
_PLACEHOLDERS = frozenset(
    {"", "n/a", "none", "null", "placeholder", "tbd", "todo", "unknown"}
)
_HERO_COMPONENTS = (
    "position_base_diff",
    "same_team_synergy",
    "opponent_matchup_synergy",
)
_MINUTE_CORE_FIELDS = frozenset(
    {
        "dire_time_delta",
        "display_score",
        "minute",
        "radiant_time_delta",
        "raw_score",
        "synergy_delta",
    }
)
_CREDENTIAL_KEY_FAMILIES = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
)
_NONCREDENTIAL_TOKEN_SUFFIXES = frozenset(
    {"budget", "count", "index", "length", "size", "usage"}
)
_GOVERNANCE_MARKERS = (
    "candidate",
    "pending",
    "placeholder",
    "superseded",
    "unactivated",
)


class RoshEvidenceCollisionError(ValueError):
    """An immutable run identity already exists with contradictory content."""


@dataclass(frozen=True)
class RoshHeroScoreRecord:
    team_side: str
    position_id: int
    hero_id: int
    raw_score: float
    display_score: float
    components: Mapping[str, Any]


@dataclass(frozen=True)
class RoshMinutePointRecord:
    minute: int
    raw_score: float
    display_score: float
    radiant_time_delta: float
    dire_time_delta: float
    synergy_delta: float
    source_audit: Mapping[str, Any]


@dataclass(frozen=True)
class RoshRunRecord:
    run_id: str
    status: str
    mode: str
    match_id: int | None
    date_time: int
    draft_hash: str
    draft: Mapping[str, Any]
    rosh_profile_id: str
    formula_version: str
    request_profile_hash: str
    upstream_bundle_hash: str
    scorer_source_hash: str
    canonical_profile_hash: str
    serialization_version: str
    request_hash: str
    request_manifest: Mapping[str, Any]
    response_manifest: Sequence[Mapping[str, Any]]
    evidence_hash: str
    collected_at: str
    radiant_team_score: float | None = None
    dire_team_score: float | None = None
    relative_advantage: float | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class StoredRoshRun:
    run: RoshRunRecord
    hero_scores: tuple[RoshHeroScoreRecord, ...]
    minute_points: tuple[RoshMinutePointRecord, ...]
    result: Mapping[str, Any] | None


@dataclass(frozen=True)
class RoshRunMatchLink:
    source: str
    source_match_id: str
    run_id: str
    map_number: int | None
    linked_at: str


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _plain_json(value: object, label: str) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} JSON object keys must be strings")
            result[key] = _plain_json(item, f"{label}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_json(item, f"{label}[]") for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        number = _finite_number(value, label)
        return 0 if number == 0 else number
    raise ValueError(f"{label} contains a non-JSON value")


def _json(value: object, label: str) -> str:
    return json.dumps(
        _plain_json(value, label),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _reject_secret_fields(value: object, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
            words = tuple(re.findall(r"[a-z0-9]+", expanded.casefold()))
            compact = "".join(words)
            token_diagnostic = (
                "token" in words
                and words[-1] in _NONCREDENTIAL_TOKEN_SUFFIXES
            )
            family_key = any(
                family in words
                or compact.startswith(family)
                or compact.endswith(family)
                for family in _CREDENTIAL_KEY_FAMILIES
            )
            header_container = (
                "header" in words
                or "headers" in words
                or compact.startswith(("header", "headers"))
                or compact.endswith(("header", "headers"))
            )
            api_key = "apikey" in compact or any(
                words[index : index + 2] == ("api", "key")
                for index in range(len(words) - 1)
            )
            token_key = not token_diagnostic and (
                "token" in words
                or compact == "token"
                or compact.startswith("token")
                or compact.endswith("token")
            )
            if family_key or header_container or api_key or token_key:
                raise ValueError(f"{label} contains a forbidden secret field")
            _reject_secret_fields(item, label)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            _reject_secret_fields(item, label)


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    if value == "0" * 64:
        raise ValueError(f"{label} must not be a placeholder hash")
    return value


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a non-placeholder string")
    result = value.strip()
    if result.casefold() in _PLACEHOLDERS or "..." in result:
        raise ValueError(f"{label} must be a non-placeholder string")
    return result


def _profile_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{label} must be a canonical non-placeholder identity")
    try:
        result = _identity(value, label)
    except ValueError as error:
        raise ValueError(
            f"{label} must be a canonical non-placeholder identity"
        ) from error
    if any(marker in result.casefold() for marker in _GOVERNANCE_MARKERS):
        raise ValueError(f"{label} must be a canonical non-placeholder identity")
    return result


def _timestamp(value: object, label: str) -> str:
    text = _identity(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return text


def _draft(value: Mapping[str, Any]) -> dict[str, Any]:
    draft = _plain_json(value, "draft")
    if not isinstance(draft, dict) or set(draft) != {"radiant", "dire"}:
        raise ValueError("draft must contain exactly radiant and dire")
    heroes: set[int] = set()
    for side in ("radiant", "dire"):
        slots = draft[side]
        if not isinstance(slots, list) or len(slots) != 5:
            raise ValueError(f"draft.{side} must contain five slots")
        positions: list[int] = []
        for slot in slots:
            if not isinstance(slot, dict) or set(slot) != {"hero_id", "position_id"}:
                raise ValueError("draft slots require hero_id and position_id")
            hero_id = _positive_integer(slot["hero_id"], "draft hero_id")
            position_id = _positive_integer(
                slot["position_id"], "draft position_id"
            )
            if position_id > 5:
                raise ValueError("draft position_id must be between 1 and 5")
            if hero_id in heroes:
                raise ValueError("draft hero_id values must be globally unique")
            heroes.add(hero_id)
            positions.append(position_id)
        if positions != [1, 2, 3, 4, 5]:
            raise ValueError("draft slots must be canonically ordered by position_id")
    return draft


def _response_manifest(
    value: Sequence[Mapping[str, Any]], *, required: bool
) -> list[dict[str, Any]]:
    manifest = _plain_json(value, "response_manifest")
    if not isinstance(manifest, list) or (required and not manifest):
        raise ValueError("succeeded runs require a non-empty response_manifest")
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ValueError("response_manifest entries must be objects")
        required_fields = {
            "operation_name",
            "request_artifact_hash",
            "response_artifact_hash",
            "collected_at",
            "relative_path",
        }
        if not required_fields.issubset(entry):
            raise ValueError("response_manifest entry is incomplete")
        _identity(entry["operation_name"], "response operation_name")
        _hash(entry["request_artifact_hash"], "request_artifact_hash")
        _hash(entry["response_artifact_hash"], "response_artifact_hash")
        _timestamp(entry["collected_at"], "response collected_at")
        relative_path = _identity(entry["relative_path"], "response relative_path")
        posix_path = PurePosixPath(relative_path)
        windows_path = PureWindowsPath(relative_path)
        normalized = PurePosixPath(relative_path.replace("\\", "/"))
        if (
            normalized == PurePosixPath(".")
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", relative_path) is not None
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or bool(windows_path.root)
            or normalized.is_absolute()
            or ".." in normalized.parts
        ):
            raise ValueError("response relative_path must stay relative")
    return manifest


def _validate_run(run: RoshRunRecord) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    _hash(run.run_id, "run_id")
    if run.status not in {"succeeded", "failed"}:
        raise ValueError("status must be succeeded or failed")
    if run.mode not in {"historical_match", "explicit_draft"}:
        raise ValueError("mode must be historical_match or explicit_draft")
    if run.mode == "historical_match":
        _positive_integer(run.match_id, "match_id")
    elif run.match_id is not None:
        raise ValueError("explicit_draft runs must not have match_id")
    if isinstance(run.date_time, bool) or not isinstance(run.date_time, int) or run.date_time < 0:
        raise ValueError("date_time must be a non-negative integer")
    _hash(run.draft_hash, "draft_hash")
    draft = _draft(run.draft)
    _profile_identity(run.rosh_profile_id, "rosh_profile_id")
    _profile_identity(run.formula_version, "formula_version")
    _hash(run.request_profile_hash, "request_profile_hash")
    _hash(run.upstream_bundle_hash, "upstream_bundle_hash")
    _hash(run.scorer_source_hash, "scorer_source_hash")
    _hash(run.canonical_profile_hash, "canonical_profile_hash")
    _profile_identity(run.serialization_version, "serialization_version")
    _hash(run.request_hash, "request_hash")
    request_manifest = _plain_json(run.request_manifest, "request_manifest")
    if not isinstance(request_manifest, dict):
        raise ValueError("request_manifest must be an object")
    if run.status == "succeeded" and not request_manifest:
        raise ValueError("succeeded runs require a non-empty request_manifest")
    _reject_secret_fields(request_manifest, "request_manifest")
    response_manifest = _response_manifest(
        run.response_manifest, required=run.status == "succeeded"
    )
    _reject_secret_fields(response_manifest, "response_manifest")
    _hash(run.evidence_hash, "evidence_hash")
    _timestamp(run.collected_at, "collected_at")
    scores = (
        run.radiant_team_score,
        run.dire_team_score,
        run.relative_advantage,
    )
    if run.status == "succeeded":
        if run.error_code is not None:
            raise ValueError("succeeded run must not have error_code")
        for label, score in zip(
            ("radiant_team_score", "dire_team_score", "relative_advantage"),
            scores,
            strict=True,
        ):
            _finite_number(score, label)
    else:
        if any(score is not None for score in scores):
            raise ValueError("failed run must not have result scores")
        if not isinstance(run.error_code, str) or _ERROR_CODE_RE.fullmatch(run.error_code) is None:
            raise ValueError("failed run requires a structured error_code")
    return draft, request_manifest, response_manifest


def _validate_heroes(
    heroes: Sequence[RoshHeroScoreRecord], draft: Mapping[str, Any]
) -> tuple[RoshHeroScoreRecord, ...]:
    records = tuple(heroes)
    if len(records) != 10:
        raise ValueError("succeeded run requires exactly ten hero scores")
    expected = {
        (side.upper(), int(slot["position_id"])): int(slot["hero_id"])
        for side in ("radiant", "dire")
        for slot in draft[side]
    }
    observed: dict[tuple[str, int], int] = {}
    hero_ids: set[int] = set()
    for hero in records:
        if hero.team_side not in {"RADIANT", "DIRE"}:
            raise ValueError("hero team_side must be RADIANT or DIRE")
        position_id = _positive_integer(hero.position_id, "hero position_id")
        if position_id > 5:
            raise ValueError("hero position_id must be between 1 and 5")
        hero_id = _positive_integer(hero.hero_id, "hero_id")
        key = (hero.team_side, position_id)
        if key in observed or hero_id in hero_ids:
            raise ValueError("hero side/position and hero_id must be unique")
        observed[key] = hero_id
        hero_ids.add(hero_id)
        _finite_number(hero.raw_score, "hero raw_score")
        _finite_number(hero.display_score, "hero display_score")
        components = _plain_json(hero.components, "hero components")
        if not isinstance(components, dict) or not set(_HERO_COMPONENTS).issubset(components):
            raise ValueError("hero components are incomplete")
        if set(components) & {"hero_id", "team_side", "position_id", "raw_score", "display_score"}:
            raise ValueError("hero components must not replace core fields")
        for name in _HERO_COMPONENTS:
            _finite_number(components[name], f"hero components.{name}")
        _reject_secret_fields(components, "hero components")
    if observed != expected:
        raise ValueError("hero scores do not match the canonical draft")
    return tuple(
        sorted(
            records,
            key=lambda item: (item.team_side != "RADIANT", item.position_id),
        )
    )


def _validate_minutes(
    minutes: Sequence[RoshMinutePointRecord],
) -> tuple[RoshMinutePointRecord, ...]:
    records = tuple(minutes)
    if not records:
        raise ValueError("succeeded run requires minute points")
    observed: set[int] = set()
    for point in records:
        if isinstance(point.minute, bool) or not isinstance(point.minute, int) or point.minute < 0:
            raise ValueError("minute must be a non-negative integer")
        if point.minute in observed:
            raise ValueError("minute values must be unique")
        observed.add(point.minute)
        for label, value in (
            ("raw_score", point.raw_score),
            ("display_score", point.display_score),
            ("radiant_time_delta", point.radiant_time_delta),
            ("dire_time_delta", point.dire_time_delta),
            ("synergy_delta", point.synergy_delta),
        ):
            _finite_number(value, f"minute {label}")
        audit = _plain_json(point.source_audit, "minute source_audit")
        if not isinstance(audit, dict) or not {"rank_source_counts", "slots"}.issubset(audit):
            raise ValueError("minute source_audit is incomplete")
        if not isinstance(audit["rank_source_counts"], dict) or not isinstance(
            audit["slots"], list
        ):
            raise ValueError("minute source_audit has invalid rank counts or slots")
        if _MINUTE_CORE_FIELDS & audit.keys():
            raise ValueError("minute source_audit contains a reserved core field")
        _reject_secret_fields(audit, "minute source_audit")
    return tuple(sorted(records, key=lambda item: item.minute))


def _hero_projection(hero: RoshHeroScoreRecord) -> dict[str, Any]:
    return {
        "hero_id": hero.hero_id,
        "team_side": hero.team_side,
        "position_id": hero.position_id,
        **_plain_json(hero.components, "hero components"),
        "raw_score": hero.raw_score,
        "display_score": hero.display_score,
    }


def _minute_projection(point: RoshMinutePointRecord) -> dict[str, Any]:
    return {
        **_plain_json(point.source_audit, "minute source_audit"),
        "minute": point.minute,
        "radiant_time_delta": point.radiant_time_delta,
        "dire_time_delta": point.dire_time_delta,
        "synergy_delta": point.synergy_delta,
        "raw_score": point.raw_score,
        "display_score": point.display_score,
    }


def _result_projection(
    run: RoshRunRecord,
    heroes: Sequence[RoshHeroScoreRecord],
    minutes: Sequence[RoshMinutePointRecord],
) -> dict[str, Any] | None:
    if run.status == "failed":
        return None
    return {
        "radiant_team_score": run.radiant_team_score,
        "dire_team_score": run.dire_team_score,
        "relative_advantage": run.relative_advantage,
        "hero_scores": [_hero_projection(hero) for hero in heroes],
        "minute_points": [_minute_projection(point) for point in minutes],
    }


def _content_projection(stored: StoredRoshRun) -> dict[str, Any]:
    run = stored.run
    return {
        "run_id": run.run_id,
        "status": run.status,
        "mode": run.mode,
        "match_id": run.match_id,
        "date_time": run.date_time,
        "draft_hash": run.draft_hash,
        "draft": run.draft,
        "rosh_profile_id": run.rosh_profile_id,
        "formula_version": run.formula_version,
        "request_profile_hash": run.request_profile_hash,
        "upstream_bundle_hash": run.upstream_bundle_hash,
        "scorer_source_hash": run.scorer_source_hash,
        "canonical_profile_hash": run.canonical_profile_hash,
        "serialization_version": run.serialization_version,
        "request_hash": run.request_hash,
        "request_manifest": run.request_manifest,
        "response_manifest": run.response_manifest,
        "evidence_hash": run.evidence_hash,
        "collected_at": run.collected_at,
        "radiant_team_score": run.radiant_team_score,
        "dire_team_score": run.dire_team_score,
        "relative_advantage": run.relative_advantage,
        "error_code": run.error_code,
        "result": stored.result,
    }


class RoshRunRepository:
    """Validate and atomically retain immutable terminal R.O.S.H. runs."""

    def __init__(self, connection: PostgresSession) -> None:
        self.connection = connection

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self.connection.transaction():
            yield

    def _checkpoint(self, stage: str, index: int | None = None) -> None:
        """Fault-injection seam used by atomicity tests."""

    @staticmethod
    def _row(row: DatabaseRow) -> dict[str, Any]:
        return dict(row)

    def get(self, run_id: str) -> StoredRoshRun | None:
        _hash(run_id, "run_id")
        cursor = self.connection.execute(
            "SELECT * FROM rosh_analysis_runs WHERE run_id=?", (run_id,)
        )
        raw = cursor.fetchone()
        if raw is None:
            return None
        row = self._row(raw)
        hero_cursor = self.connection.execute(
            """SELECT * FROM rosh_hero_scores WHERE run_id=?
               ORDER BY CASE team_side WHEN 'RADIANT' THEN 0 ELSE 1 END,
                        position_id""",
            (run_id,),
        )
        heroes = tuple(
            RoshHeroScoreRecord(
                team_side=str(item["team_side"]),
                position_id=int(item["position_id"]),
                hero_id=int(item["hero_id"]),
                raw_score=float(item["raw_score"]),
                display_score=float(item["display_score"]),
                components=json.loads(str(item["components_json"])),
            )
            for item in (self._row(raw_hero) for raw_hero in hero_cursor)
        )
        minute_cursor = self.connection.execute(
            "SELECT * FROM rosh_minute_points WHERE run_id=? ORDER BY minute",
            (run_id,),
        )
        minutes = tuple(
            RoshMinutePointRecord(
                minute=int(item["minute"]),
                raw_score=float(item["raw_score"]),
                display_score=float(item["display_score"]),
                radiant_time_delta=float(item["radiant_time_delta"]),
                dire_time_delta=float(item["dire_time_delta"]),
                synergy_delta=float(item["synergy_delta"]),
                source_audit=json.loads(str(item["source_audit_json"])),
            )
            for item in (self._row(raw_minute) for raw_minute in minute_cursor)
        )
        run = RoshRunRecord(
            run_id=str(row["run_id"]),
            status=str(row["status"]),
            mode=str(row["mode"]),
            match_id=None if row["match_id"] is None else int(row["match_id"]),
            date_time=int(row["date_time"]),
            draft_hash=str(row["draft_hash"]),
            draft=json.loads(str(row["draft_json"])),
            rosh_profile_id=str(row["rosh_profile_id"]),
            formula_version=str(row["formula_version"]),
            request_profile_hash=str(row["request_profile_hash"]),
            upstream_bundle_hash=str(row["upstream_bundle_hash"]),
            scorer_source_hash=str(row["scorer_source_hash"]),
            canonical_profile_hash=str(row["canonical_profile_hash"]),
            serialization_version=str(row["serialization_version"]),
            request_hash=str(row["request_hash"]),
            request_manifest=json.loads(str(row["request_manifest_json"])),
            response_manifest=tuple(
                json.loads(str(row["response_manifest_json"]))
            ),
            evidence_hash=str(row["evidence_hash"]),
            collected_at=str(row["collected_at"]),
            radiant_team_score=(
                None if row["radiant_team_score"] is None else float(row["radiant_team_score"])
            ),
            dire_team_score=(
                None if row["dire_team_score"] is None else float(row["dire_team_score"])
            ),
            relative_advantage=(
                None if row["relative_advantage"] is None else float(row["relative_advantage"])
            ),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
        )
        result = None if row["result_json"] is None else json.loads(str(row["result_json"]))
        return StoredRoshRun(run, heroes, minutes, result)

    def link_matches(
        self,
        run_id: str,
        links: Sequence[Mapping[str, Any]],
        *,
        linked_at: str,
    ) -> tuple[RoshRunMatchLink, ...]:
        _hash(run_id, "run_id")
        timestamp = _timestamp(linked_at, "linked_at")
        stored = self.get(run_id)
        if stored is None or stored.run.status != "succeeded":
            raise ValueError("match links require a succeeded Rosh run")
        normalized: list[tuple[str, str, int | None]] = []
        seen: set[tuple[str, str]] = set()
        for link in links:
            source = _identity(link.get("source"), "match link source")
            if source not in _MATCH_LINK_SOURCES:
                raise ValueError("match link source is unsupported")
            source_match_id = _identity(
                link.get("source_match_id"), "source_match_id"
            )
            if len(source_match_id) > 128:
                raise ValueError("source_match_id is too long")
            map_number = link.get("map_number")
            if map_number is not None:
                map_number = _positive_integer(map_number, "map_number")
                if map_number > 5:
                    raise ValueError("map_number must be between 1 and 5")
            identity = (source, source_match_id)
            if identity in seen:
                raise ValueError("duplicate match link identity")
            seen.add(identity)
            normalized.append((source, source_match_id, map_number))

        with self._transaction():
            for source, source_match_id, map_number in normalized:
                self.connection.execute(
                    """INSERT INTO rosh_run_match_links
                       (source, source_match_id, run_id, map_number, linked_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (source, source_match_id, run_id) DO NOTHING""",
                    (source, source_match_id, run_id, map_number, timestamp),
                )
        return self.get_match_links(run_id)

    def get_match_links(self, run_id: str) -> tuple[RoshRunMatchLink, ...]:
        _hash(run_id, "run_id")
        rows = self.connection.execute(
            """SELECT source, source_match_id, run_id, map_number, linked_at
                 FROM rosh_run_match_links
                WHERE run_id=?
                ORDER BY source, source_match_id""",
            (run_id,),
        ).fetchall()
        return tuple(
            RoshRunMatchLink(
                source=str(row["source"]),
                source_match_id=str(row["source_match_id"]),
                run_id=str(row["run_id"]),
                map_number=(
                    None if row["map_number"] is None else int(row["map_number"])
                ),
                linked_at=str(row["linked_at"]),
            )
            for row in rows
        )

    def get_match_records(
        self,
        source: str,
        source_match_id: str,
    ) -> tuple[tuple[StoredRoshRun, tuple[RoshRunMatchLink, ...]], ...]:
        normalized_source = _identity(source, "match link source")
        if normalized_source not in _MATCH_LINK_SOURCES:
            raise ValueError("match link source is unsupported")
        normalized_match_id = _identity(source_match_id, "source_match_id")
        rows = self.connection.execute(
            """SELECT link.run_id
                 FROM rosh_run_match_links AS link
                 JOIN rosh_analysis_runs AS run ON run.run_id=link.run_id
                WHERE link.source=? AND link.source_match_id=?
                ORDER BY live_text_timestamp_utc(run.collected_at) DESC,
                         run.run_id DESC""",
            (normalized_source, normalized_match_id),
        ).fetchall()
        records: list[tuple[StoredRoshRun, tuple[RoshRunMatchLink, ...]]] = []
        for row in rows:
            run_id = str(row["run_id"])
            stored = self.get(run_id)
            if stored is not None:
                records.append((stored, self.get_match_links(run_id)))
        return tuple(records)

    def get_by_evidence_hash(self, evidence_hash: str) -> StoredRoshRun | None:
        _hash(evidence_hash, "evidence_hash")
        row = self.connection.execute(
            "SELECT run_id FROM rosh_analysis_runs WHERE evidence_hash=?",
            (evidence_hash,),
        ).fetchone()
        return None if row is None else self.get(str(row[0]))

    def get_latest_succeeded_for_draft(
        self,
        draft_hash: str,
        *,
        rosh_profile_id: str,
        collected_at_lte: datetime,
    ) -> StoredRoshRun | None:
        """Return only causal succeeded evidence for one active draft/profile."""

        _hash(draft_hash, "draft_hash")
        if not isinstance(rosh_profile_id, str) or not rosh_profile_id.strip():
            raise ValueError("rosh_profile_id must be non-empty")
        if (
            not isinstance(collected_at_lte, datetime)
            or collected_at_lte.tzinfo is None
            or collected_at_lte.utcoffset() is None
        ):
            raise ValueError("collected_at_lte must be timezone-aware")
        cutoff = collected_at_lte.astimezone(timezone.utc).isoformat()
        row = self.connection.execute(
            """SELECT run_id
                 FROM rosh_analysis_runs
                WHERE status='succeeded'
                  AND draft_hash=?
                  AND rosh_profile_id=?
                  AND live_text_timestamp_utc(collected_at)<=CAST(? AS timestamptz)
                ORDER BY live_text_timestamp_utc(collected_at) DESC,
                         date_time DESC, run_id DESC
                LIMIT 1""",
            (draft_hash, rosh_profile_id, cutoff),
        ).fetchone()
        return None if row is None else self.get(str(row[0]))

    def get_succeeded_for_explicit_identity(
        self,
        draft_hash: str,
        *,
        rosh_profile_id: str,
        canonical_profile_hash: str,
        date_time: int,
    ) -> StoredRoshRun | None:
        """Return an already completed live request regardless of causal cutoff."""

        _hash(draft_hash, "draft_hash")
        _hash(canonical_profile_hash, "canonical_profile_hash")
        if not isinstance(rosh_profile_id, str) or not rosh_profile_id.strip():
            raise ValueError("rosh_profile_id must be non-empty")
        if type(date_time) is not int or date_time <= 0:
            raise ValueError("date_time must be a positive integer")
        row = self.connection.execute(
            """SELECT run_id
                 FROM rosh_analysis_runs
                WHERE status='succeeded'
                  AND mode='explicit_draft'
                  AND draft_hash=?
                  AND rosh_profile_id=?
                  AND canonical_profile_hash=?
                  AND date_time=?
                ORDER BY live_text_timestamp_utc(collected_at) DESC, run_id DESC
                LIMIT 1""",
            (
                draft_hash,
                rosh_profile_id,
                canonical_profile_hash,
                date_time,
            ),
        ).fetchone()
        return None if row is None else self.get(str(row[0]))

    def _existing(
        self,
        intended: StoredRoshRun,
    ) -> StoredRoshRun | None:
        rows = self.connection.execute(
            """SELECT run_id FROM rosh_analysis_runs
                WHERE run_id=? OR evidence_hash=? ORDER BY run_id""",
            (intended.run.run_id, intended.run.evidence_hash),
        ).fetchall()
        if not rows:
            return None
        existing = tuple(self.get(str(row[0])) for row in rows)
        if len(existing) != 1 or existing[0] is None:
            raise RoshEvidenceCollisionError("run_id and evidence_hash identify different runs")
        if _json(_content_projection(existing[0]), "stored run") != _json(
            _content_projection(intended), "intended run"
        ):
            raise RoshEvidenceCollisionError(
                "immutable run identity conflicts with stored content"
            )
        return existing[0]

    def write_succeeded(
        self,
        run: RoshRunRecord,
        hero_scores: Sequence[RoshHeroScoreRecord],
        minute_points: Sequence[RoshMinutePointRecord],
    ) -> StoredRoshRun:
        if run.status != "succeeded":
            raise ValueError("write_succeeded requires status='succeeded'")
        draft, request_manifest, response_manifest = _validate_run(run)
        heroes = _validate_heroes(hero_scores, draft)
        minutes = _validate_minutes(minute_points)
        result = _result_projection(run, heroes, minutes)
        intended = StoredRoshRun(run, heroes, minutes, result)
        with self._transaction():
            existing = self._existing(intended)
            if existing is not None:
                return existing
            self._insert_run(run, draft, request_manifest, response_manifest, result)
            self._checkpoint("run")
            for index, hero in enumerate(heroes):
                self.connection.execute(
                    """INSERT INTO rosh_hero_scores
                       (run_id, team_side, position_id, hero_id, raw_score,
                        display_score, components_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run.run_id,
                        hero.team_side,
                        hero.position_id,
                        hero.hero_id,
                        hero.raw_score,
                        hero.display_score,
                        _json(hero.components, "hero components"),
                    ),
                )
                self._checkpoint("hero", index)
            for index, point in enumerate(minutes):
                self.connection.execute(
                    """INSERT INTO rosh_minute_points
                       (run_id, minute, raw_score, display_score,
                        radiant_time_delta, dire_time_delta, synergy_delta,
                        source_audit_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run.run_id,
                        point.minute,
                        point.raw_score,
                        point.display_score,
                        point.radiant_time_delta,
                        point.dire_time_delta,
                        point.synergy_delta,
                        _json(point.source_audit, "minute source_audit"),
                    ),
                )
                self._checkpoint("minute", index)
        stored = self.get(run.run_id)
        assert stored is not None
        return stored

    def write_failed(self, run: RoshRunRecord) -> StoredRoshRun:
        if run.status != "failed":
            raise ValueError("write_failed requires status='failed'")
        draft, request_manifest, response_manifest = _validate_run(run)
        intended = StoredRoshRun(run, (), (), None)
        with self._transaction():
            existing = self._existing(intended)
            if existing is not None:
                return existing
            self._insert_run(
                run, draft, request_manifest, response_manifest, result=None
            )
            self._checkpoint("run")
        stored = self.get(run.run_id)
        assert stored is not None
        return stored

    def _insert_run(
        self,
        run: RoshRunRecord,
        draft: Mapping[str, Any],
        request_manifest: Mapping[str, Any],
        response_manifest: Sequence[Mapping[str, Any]],
        result: Mapping[str, Any] | None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO rosh_analysis_runs
               (run_id, status, mode, match_id, date_time, draft_hash,
                draft_json, rosh_profile_id, formula_version,
                request_profile_hash, upstream_bundle_hash, scorer_source_hash,
                canonical_profile_hash, serialization_version, request_hash,
                request_manifest_json, response_manifest_json,
                radiant_team_score, dire_team_score, relative_advantage,
                result_json, evidence_hash, error_code, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?)""",
            (
                run.run_id,
                run.status,
                run.mode,
                run.match_id,
                run.date_time,
                run.draft_hash,
                _json(draft, "draft"),
                run.rosh_profile_id,
                run.formula_version,
                run.request_profile_hash,
                run.upstream_bundle_hash,
                run.scorer_source_hash,
                run.canonical_profile_hash,
                run.serialization_version,
                run.request_hash,
                _json(request_manifest, "request_manifest"),
                _json(response_manifest, "response_manifest"),
                run.radiant_team_score,
                run.dire_team_score,
                run.relative_advantage,
                None if result is None else _json(result, "result"),
                run.evidence_hash,
                run.error_code,
                run.collected_at,
            ),
        )


__all__ = [
    "RoshEvidenceCollisionError",
    "RoshHeroScoreRecord",
    "RoshMinutePointRecord",
    "RoshRunMatchLink",
    "RoshRunRecord",
    "RoshRunRepository",
    "StoredRoshRun",
]
