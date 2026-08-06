"""Strict legacy-to-official R.O.S.H. authority bridging.

Legacy scores are read-only historical evidence.  This module never changes
their eligibility flag and only bridges a row when an already persisted
official run and its archived request/response artifacts can be replayed.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping, Sequence

from database.session import PostgresSession
from live_betting.rosh_parity_storage import (
    RoshRunMatchLink,
    RoshRunRepository,
    StoredRoshRun,
)
from prematch.stratz_official_profile import get_profile

from .backtest import DraftCorpus, DraftTarget, load_draft_corpus
from .draft_features import ROLE_CONFIDENCE_MIN, AvailabilityMode
from .raw_archive import canonical_json_bytes
from .rosh_features import (
    RoshFeatureSnapshot,
    RoshFeatureTarget,
    RoshRequestPlanWitness,
    build_rosh_feature_snapshot_with_authority,
    replay_rosh_feature_snapshot,
)
from .roles import RECONSTRUCTED_ASSIGNMENT_VERSION


ROSH_AUTHORITY_BRIDGE_VERSION = "rosh-authority-bridge-v1"
ROSH_BRIDGE_LINEAGE_SCHEMA = "rosh-authority-bridge-lineage/v1"
ROSH_BRIDGE_TABLE = "rosh_authority_bridge_records"
_UTC = timezone.utc
_HASH_LENGTH = 64
_STAGES = (
    "legacy_rows",
    "formal_map_linked",
    "ten_heroes_complete",
    "expected_positions_complete",
    "scorer_profile_available",
    "input_artifact_available",
    "response_artifact_available",
    "cutoff_legal",
    "exact_replay_passed",
    "final_eligible",
)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _legacy_evidence_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field} must be an RFC 3339 timestamp") from error
    else:
        raise ValueError(f"{field} must be an RFC 3339 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(_UTC)


def _timestamp(value: object, field: str) -> str:
    return _utc(value, field).isoformat().replace("+00:00", "Z")


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _HASH_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _five_ids(value: object, field: str) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(parsed, list)
        or len(parsed) != 5
        or any(type(item) is not int or item <= 0 for item in parsed)
        or len(set(parsed)) != 5
    ):
        return None
    return tuple(parsed)


def _json_object(value: object, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{field} must be a JSON object") from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be a JSON object")
    return value


def _safe_relative_path(value: object, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} path is unavailable")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        "\\" in value
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.as_posix() != value
    ):
        raise ValueError(f"{field} path is invalid")
    return posix


def _artifact_available(
    artifact_root: Path,
    manifest: Mapping[str, Any],
    *,
    field: str,
) -> bool:
    try:
        content_hash = _digest(manifest.get("content_sha256"), f"{field}.content_sha256")
        gzip_hash = _digest(manifest.get("gzip_sha256"), f"{field}.gzip_sha256")
        relative = _safe_relative_path(manifest.get("relative_path"), field)
        expected = PurePosixPath(
            "sha256", content_hash[:2], f"{content_hash}.json.gz"
        )
        if relative != expected:
            return False
        root = artifact_root.resolve(strict=True)
        path = artifact_root.joinpath(*relative.parts).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            return False
        compressed = path.read_bytes()
        if hashlib.sha256(compressed).hexdigest() != gzip_hash:
            return False
        body = gzip.decompress(compressed)
        byte_count = manifest.get("byte_count")
        if byte_count is not None and (type(byte_count) is not int or byte_count != len(body)):
            return False
        return hmac.compare_digest(hashlib.sha256(body).hexdigest(), content_hash)
    except (OSError, ValueError, EOFError, gzip.BadGzipFile):
        return False


def _snapshot_payload(snapshot: RoshFeatureSnapshot) -> dict[str, object]:
    payload = asdict(snapshot)
    payload["prediction_cutoff"] = snapshot.prediction_cutoff.isoformat()
    for name in ("radiant_hero_ids", "dire_hero_ids"):
        payload.pop(name, None)
    return payload


def _draft_payload(target: RoshFeatureTarget) -> dict[str, object]:
    return {
        "radiant": [
            {"hero_id": hero_id, "position_id": position}
            for position, hero_id in enumerate(target.radiant_hero_ids, 1)
        ],
        "dire": [
            {"hero_id": hero_id, "position_id": position}
            for position, hero_id in enumerate(target.dire_hero_ids, 1)
        ],
    }


@dataclass(frozen=True)
class RoshBridgeStage:
    stage: str
    support: int


@dataclass(frozen=True)
class RoshBridgeMissingReason:
    stage: str
    reason: str
    support: int


@dataclass(frozen=True)
class RoshAuthorityBridgeRecord:
    bridge_key: str
    bridge_version: str
    legacy_score_key: str
    run_id: str
    match_id: int
    prediction_cutoff: str
    draft: Mapping[str, Any]
    radiant_player_ids: tuple[int, ...] | None
    dire_player_ids: tuple[int, ...] | None
    player_coverage_count: int | None
    rosh_profile_id: str
    formula_version: str
    scorer_source_hash: str
    canonical_profile_hash: str
    input_artifact_hash: str
    response_artifact_hash: str
    generated_at: str
    available_at: str
    source: str
    source_match_id: str
    map_number: int | None
    authority: Mapping[str, Any]
    snapshot: Mapping[str, Any]
    content_hash: str
    created_at: str

    def to_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload = {
            "bridge_version": self.bridge_version,
            "legacy_score_key": self.legacy_score_key,
            "run_id": self.run_id,
            "match_id": self.match_id,
            "prediction_cutoff": self.prediction_cutoff,
            "draft": self.draft,
            "radiant_player_ids": (
                None
                if self.radiant_player_ids is None
                else list(self.radiant_player_ids)
            ),
            "dire_player_ids": (
                None if self.dire_player_ids is None else list(self.dire_player_ids)
            ),
            "player_coverage_count": self.player_coverage_count,
            "rosh_profile_id": self.rosh_profile_id,
            "formula_version": self.formula_version,
            "scorer_source_hash": self.scorer_source_hash,
            "canonical_profile_hash": self.canonical_profile_hash,
            "input_artifact_hash": self.input_artifact_hash,
            "response_artifact_hash": self.response_artifact_hash,
            "generated_at": self.generated_at,
            "available_at": self.available_at,
            "source": self.source,
            "source_match_id": self.source_match_id,
            "map_number": self.map_number,
            "authority": self.authority,
            "snapshot": self.snapshot,
            "created_at": self.created_at,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload


@dataclass(frozen=True)
class RoshAuthorityBridgeReport:
    version: str
    formal_maps: int
    legacy_rows: int
    official_runs: int
    official_match_links: int
    stages: tuple[RoshBridgeStage, ...]
    missing_reasons: tuple[RoshBridgeMissingReason, ...]
    player_identity_support: int
    player_identity_diagnostics: tuple[RoshBridgeMissingReason, ...]
    snapshot_attempts: int
    snapshot_available: int
    eligible_records: tuple[RoshAuthorityBridgeRecord, ...]
    inserted_records: int = 0
    unchanged_records: int = 0


@dataclass(frozen=True)
class _LegacyRow:
    score_key: str
    match_id: int
    radiant_heroes: tuple[int, ...] | None
    dire_heroes: tuple[int, ...] | None
    radiant_players: tuple[int, ...] | None
    dire_players: tuple[int, ...] | None
    player_coverage_count: int
    formula_version: str
    evidence: Mapping[str, Any]
    evidence_hash_valid: bool


def _load_legacy_rows(connection: PostgresSession) -> tuple[_LegacyRow, ...]:
    rows = connection.execute(
        """SELECT score_key, match_id, radiant_hero_ids_json,
                  dire_hero_ids_json, radiant_player_ids_json,
                  dire_player_ids_json, player_coverage_count,
                  formula_version, evidence_json, evidence_hash
             FROM historical_rosh_lineup_scores
            ORDER BY match_id, score_key"""
    ).fetchall()
    result: list[_LegacyRow] = []
    for row in rows:
        evidence = _json_object(row["evidence_json"], "legacy evidence")
        result.append(
            _LegacyRow(
                score_key=str(row["score_key"]),
                match_id=int(row["match_id"]),
                radiant_heroes=_five_ids(row["radiant_hero_ids_json"], "radiant heroes"),
                dire_heroes=_five_ids(row["dire_hero_ids_json"], "dire heroes"),
                radiant_players=_five_ids(
                    row["radiant_player_ids_json"], "radiant players"
                ),
                dire_players=_five_ids(row["dire_player_ids_json"], "dire players"),
                player_coverage_count=int(row["player_coverage_count"]),
                formula_version=str(row["formula_version"]),
                evidence=evidence,
                evidence_hash_valid=hmac.compare_digest(
                    _legacy_evidence_hash(evidence), str(row["evidence_hash"])
                ),
            )
        )
    return tuple(result)


def _draft_targets(connection: PostgresSession) -> dict[int, DraftTarget]:
    corpus: DraftCorpus = load_draft_corpus(
        connection,
        availability_mode=AvailabilityMode.RECONSTRUCTED,
        assignment_version=RECONSTRUCTED_ASSIGNMENT_VERSION,
    )
    result: dict[int, DraftTarget] = {}
    for loaded in corpus.maps:
        if loaded.target is not None:
            result[loaded.match_id] = loaded.target
    return result


def _positioned_heroes(target: DraftTarget) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    sides: list[tuple[int, ...]] = []
    for team in (target.radiant, target.dire):
        by_position: dict[int, int] = {}
        for player in team.players:
            position = player.expected_position
            if (
                position is None
                or player.expected_position_confidence < ROLE_CONFIDENCE_MIN
                or position in by_position
            ):
                return None
            by_position[position] = player.hero_id
        if set(by_position) != set(range(1, 6)):
            return None
        sides.append(tuple(by_position[index] for index in range(1, 6)))
    return sides[0], sides[1]


def _players_by_side(target: DraftTarget) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(player.player_id for player in target.radiant.players), tuple(
        player.player_id for player in target.dire.players
    )


def _audit_player_identity(
    rows: Sequence[_LegacyRow],
    targets: Mapping[int, DraftTarget],
) -> tuple[int, tuple[RoshBridgeMissingReason, ...]]:
    support = 0
    diagnostics: Counter[str] = Counter()
    for row in rows:
        if row.player_coverage_count != 10:
            diagnostics["player_coverage_incomplete"] += 1
        if row.radiant_players is None or row.dire_players is None:
            diagnostics["player_ids_unavailable"] += 1
            continue
        expected_radiant, expected_dire = _players_by_side(targets[row.match_id])
        if (
            set(row.radiant_players) != set(expected_radiant)
            or set(row.dire_players) != set(expected_dire)
        ):
            diagnostics["player_identity_mismatch"] += 1
            continue
        support += 1
    return support, tuple(
        RoshBridgeMissingReason(
            "optional_player_identity_evidence",
            reason,
            count,
        )
        for reason, count in sorted(diagnostics.items())
    )


def _lineage(evidence: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = evidence.get("authority_bridge")
    if not isinstance(value, Mapping):
        return None
    required = {
        "schema",
        "run_id",
        "source",
        "source_match_id",
        "map_number",
        "request_started_at",
        "generated_at",
        "available_at",
        "input_artifact_hash",
        "response_artifact_hash",
        "content_hash",
    }
    if set(value) != required or value.get("schema") != ROSH_BRIDGE_LINEAGE_SCHEMA:
        return None
    expected = dict(value)
    claimed = expected.pop("content_hash")
    try:
        if not hmac.compare_digest(_digest(claimed, "lineage content_hash"), _hash(expected)):
            return None
        _digest(value["run_id"], "lineage run_id")
        _digest(value["input_artifact_hash"], "lineage input artifact")
        _digest(value["response_artifact_hash"], "lineage response artifact")
        if value["source"] not in {"opendota", "stratz"}:
            return None
        if not isinstance(value["source_match_id"], str) or not value["source_match_id"].strip():
            return None
        if value["map_number"] is not None and (
            type(value["map_number"]) is not int or not 1 <= value["map_number"] <= 5
        ):
            return None
        for field in ("request_started_at", "generated_at", "available_at"):
            _utc(value[field], f"lineage {field}")
    except ValueError:
        return None
    return value


def _run_candidates(
    runs: Mapping[str, StoredRoshRun],
    lineage: Mapping[str, Any],
    row: _LegacyRow,
    target: DraftTarget,
) -> tuple[StoredRoshRun | None, str | None]:
    run = runs.get(str(lineage["run_id"]))
    if run is None:
        return None, "official_run_unavailable"
    if (
        run.run.status != "succeeded"
        or run.run.mode != "historical_match"
        or run.run.match_id != row.match_id
    ):
        return None, "run_identity_mismatch"
    positioned = _positioned_heroes(target)
    if positioned is None:
        return None, "expected_positions_incomplete"
    radiant, dire = positioned
    expected_target = RoshFeatureTarget(
        match_id=row.match_id,
        date_time=run.run.date_time,
        prediction_cutoff=target.prediction_cutoff,
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        radiant_hero_ids=radiant,
        dire_hero_ids=dire,
    )
    expected_draft = _draft_payload(expected_target)
    if (
        run.run.date_time > int(target.prediction_cutoff.timestamp())
        or run.run.draft.get("radiant") != expected_draft["radiant"]
        or run.run.draft.get("dire") != expected_draft["dire"]
    ):
        return None, "run_draft_mismatch"
    return run, None


def _manifest_artifacts(run: StoredRoshRun, lineage: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    request = run.run.request_manifest.get("request_artifact")
    responses = tuple(run.run.response_manifest)
    if not isinstance(request, Mapping) or not responses:
        return None
    response_hashes = {row.get("response_artifact_hash") for row in responses}
    if len(response_hashes) != 1:
        return None
    response = responses[0]
    if (
        request.get("content_sha256") != lineage["input_artifact_hash"]
        or response.get("response_artifact_hash") != lineage["response_artifact_hash"]
    ):
        return None
    return request, response


def _existing_link(
    links: Sequence[RoshRunMatchLink],
    *,
    run_id: str,
    source: str,
    source_match_id: str,
) -> RoshRunMatchLink | None:
    matches = tuple(
        link
        for link in links
        if link.run_id == run_id
        and link.source == source
        and link.source_match_id == source_match_id
    )
    if len(matches) > 1:
        raise ValueError("duplicate R.O.S.H. run-match links")
    return matches[0] if matches else None


def _record(
    row: _LegacyRow,
    target: DraftTarget,
    run: StoredRoshRun,
    authority: Mapping[str, Any],
    snapshot: RoshFeatureSnapshot,
    lineage: Mapping[str, Any],
    *,
    link: RoshRunMatchLink,
    created_at: str,
) -> RoshAuthorityBridgeRecord:
    snapshot_payload = _snapshot_payload(snapshot)
    draft = run.run.draft
    payload = {
        "bridge_version": ROSH_AUTHORITY_BRIDGE_VERSION,
        "legacy_score_key": row.score_key,
        "run_id": run.run.run_id,
        "match_id": row.match_id,
        "prediction_cutoff": _timestamp(target.prediction_cutoff, "prediction_cutoff"),
        "draft": draft,
        "radiant_player_ids": (
            None if row.radiant_players is None else list(row.radiant_players)
        ),
        "dire_player_ids": (
            None if row.dire_players is None else list(row.dire_players)
        ),
        "player_coverage_count": row.player_coverage_count,
        "rosh_profile_id": run.run.rosh_profile_id,
        "formula_version": run.run.formula_version,
        "scorer_source_hash": run.run.scorer_source_hash,
        "canonical_profile_hash": run.run.canonical_profile_hash,
        "input_artifact_hash": lineage["input_artifact_hash"],
        "response_artifact_hash": lineage["response_artifact_hash"],
        "generated_at": _timestamp(lineage["generated_at"], "generated_at"),
        "available_at": _timestamp(lineage["available_at"], "available_at"),
        "source": link.source,
        "source_match_id": link.source_match_id,
        "map_number": link.map_number,
        "authority": authority,
        "snapshot": snapshot_payload,
    }
    content_hash = _hash({"domain": "rosh-authority-bridge-content/v1", **payload})
    bridge_key = _hash(
        {"domain": "rosh-authority-bridge-key/v1", "content_hash": content_hash}
    )
    return RoshAuthorityBridgeRecord(
        bridge_key=bridge_key,
        content_hash=content_hash,
        bridge_version=ROSH_AUTHORITY_BRIDGE_VERSION,
        legacy_score_key=row.score_key,
        run_id=run.run.run_id,
        match_id=row.match_id,
        prediction_cutoff=payload["prediction_cutoff"],
        draft=draft,
        radiant_player_ids=row.radiant_players,
        dire_player_ids=row.dire_players,
        player_coverage_count=row.player_coverage_count,
        rosh_profile_id=run.run.rosh_profile_id,
        formula_version=run.run.formula_version,
        scorer_source_hash=run.run.scorer_source_hash,
        canonical_profile_hash=run.run.canonical_profile_hash,
        input_artifact_hash=str(lineage["input_artifact_hash"]),
        response_artifact_hash=str(lineage["response_artifact_hash"]),
        generated_at=payload["generated_at"],
        available_at=payload["available_at"],
        source=link.source,
        source_match_id=link.source_match_id,
        map_number=link.map_number,
        authority=authority,
        snapshot=snapshot_payload,
        created_at=created_at,
    )


def _record_payload(record: RoshAuthorityBridgeRecord) -> dict[str, object]:
    return record.to_payload(include_hash=True)


def _validate_record_hashes(record: RoshAuthorityBridgeRecord) -> None:
    if record.bridge_version != ROSH_AUTHORITY_BRIDGE_VERSION:
        raise ValueError("unsupported R.O.S.H. authority bridge version")
    payload = record.to_payload(include_hash=False)
    payload.pop("created_at")
    content_hash = _hash({"domain": "rosh-authority-bridge-content/v1", **payload})
    if not hmac.compare_digest(content_hash, record.content_hash):
        raise ValueError("R.O.S.H. bridge content hash does not recompute")
    bridge_key = _hash(
        {"domain": "rosh-authority-bridge-key/v1", "content_hash": content_hash}
    )
    if not hmac.compare_digest(bridge_key, record.bridge_key):
        raise ValueError("R.O.S.H. bridge key does not recompute")


def _audit_rows(
    rows: Sequence[_LegacyRow],
    *,
    formal_ids: set[int],
    targets: Mapping[int, DraftTarget],
    runs: Mapping[str, StoredRoshRun],
    links: Sequence[RoshRunMatchLink],
    artifact_root: Path,
    created_at: str,
) -> tuple[
    tuple[RoshBridgeStage, ...],
    tuple[RoshBridgeMissingReason, ...],
    int,
    tuple[RoshBridgeMissingReason, ...],
    tuple[RoshAuthorityBridgeRecord, ...],
    int,
    int,
]:
    current = list(rows)
    stages: list[RoshBridgeStage] = [RoshBridgeStage("legacy_rows", len(current))]
    reasons: list[RoshBridgeMissingReason] = []

    def advance(stage: str, predicate, reason) -> None:
        nonlocal current
        passed: list[_LegacyRow] = []
        failed: Counter[str] = Counter()
        for row in current:
            result = predicate(row)
            if result is True:
                passed.append(row)
            else:
                failed[str(result if result is not False else reason)] += 1
        for missing_reason, support in sorted(failed.items()):
            reasons.append(RoshBridgeMissingReason(stage, missing_reason, support))
        current = passed
        stages.append(RoshBridgeStage(stage, len(current)))

    advance(
        "formal_map_linked",
        lambda row: True if row.match_id in formal_ids else "formal_map_unlinked",
        "formal_map_unlinked",
    )
    advance(
        "ten_heroes_complete",
        lambda row: (
            True
            if row.radiant_heroes is not None
            and row.dire_heroes is not None
            and len(set((*row.radiant_heroes, *row.dire_heroes))) == 10
            else "ten_heroes_incomplete"
        ),
        "ten_heroes_incomplete",
    )
    def expected_positions(row: _LegacyRow) -> str | bool:
        target = targets.get(row.match_id)
        if target is None:
            return "expected_positions_incomplete"
        positioned = _positioned_heroes(target)
        if positioned is None:
            return "expected_positions_incomplete"
        if (
            set(row.radiant_heroes or ()) != set(positioned[0])
            or set(row.dire_heroes or ()) != set(positioned[1])
        ):
            return "hero_identity_mismatch"
        return True

    advance(
        "expected_positions_complete",
        expected_positions,
        "expected_positions_incomplete",
    )

    player_identity_support, player_identity_diagnostics = _audit_player_identity(
        current,
        targets,
    )

    lineage_by_key: dict[str, Mapping[str, Any]] = {}
    run_by_key: dict[str, StoredRoshRun] = {}
    target_by_key: dict[str, DraftTarget] = {}
    link_by_key: dict[str, RoshRunMatchLink] = {}

    def scorer_profile(row: _LegacyRow) -> str | bool:
        if not row.evidence_hash_valid:
            return "legacy_evidence_hash_mismatch"
        lineage = _lineage(row.evidence)
        if lineage is None:
            return "scorer_profile_lineage_unavailable"
        run = runs.get(str(lineage["run_id"]))
        target = targets.get(row.match_id)
        if run is None or target is None:
            return "official_run_unavailable"
        try:
            profile = get_profile()
        except Exception:
            return "active_profile_unavailable"
        if any(
            getattr(run.run, name) != getattr(profile, name)
            for name in (
                "rosh_profile_id",
                "formula_version",
                "request_profile_hash",
                "upstream_bundle_hash",
                "scorer_source_hash",
                "canonical_profile_hash",
                "serialization_version",
            )
        ):
            return "profile_identity_mismatch"
        if row.formula_version != run.run.formula_version:
            return "legacy_formula_version_mismatch"
        try:
            candidate, error = _run_candidates(runs, lineage, row, target)
        except ValueError as exc:
            return str(exc)
        if error is not None:
            return error
        assert candidate is not None
        lineage_by_key[row.score_key] = lineage
        run_by_key[row.score_key] = candidate
        target_by_key[row.score_key] = target
        return True

    advance("scorer_profile_available", scorer_profile, "scorer_profile_unavailable")

    def input_artifact(row: _LegacyRow) -> str | bool:
        lineage = lineage_by_key[row.score_key]
        manifest = _manifest_artifacts(run_by_key[row.score_key], lineage)
        if manifest is None or not _artifact_available(
            artifact_root, manifest[0], field="request_artifact"
        ):
            return "input_artifact_unavailable"
        return True

    advance("input_artifact_available", input_artifact, "input_artifact_unavailable")

    def response_artifact(row: _LegacyRow) -> str | bool:
        lineage = lineage_by_key[row.score_key]
        manifest = _manifest_artifacts(run_by_key[row.score_key], lineage)
        if manifest is None or not _artifact_available(
            artifact_root,
            {
                **manifest[1],
                "content_sha256": manifest[1].get("response_artifact_hash"),
                "gzip_sha256": manifest[1].get("response_gzip_sha256"),
            },
            field="response_artifact",
        ):
            return "response_artifact_unavailable"
        return True

    advance("response_artifact_available", response_artifact, "response_artifact_unavailable")

    def cutoff_legal(row: _LegacyRow) -> str | bool:
        lineage = lineage_by_key[row.score_key]
        target = target_by_key[row.score_key]
        cutoff = target.prediction_cutoff
        try:
            times = tuple(
                _utc(lineage[field], f"lineage {field}")
                for field in ("request_started_at", "generated_at", "available_at")
            )
            collected = _utc(run_by_key[row.score_key].run.collected_at, "run collected_at")
        except ValueError:
            return "cutoff_timestamp_invalid"
        if any(value > cutoff for value in (*times, collected)):
            return "cutoff_violation"
        if times[0] > times[1] or times[1] > times[2]:
            return "lineage_timestamp_order_invalid"
        if collected != times[2]:
            return "available_at_run_timestamp_mismatch"
        return True

    advance("cutoff_legal", cutoff_legal, "cutoff_violation")

    def exact_replay(row: _LegacyRow) -> str | bool:
        lineage = lineage_by_key[row.score_key]
        target = target_by_key[row.score_key]
        run = run_by_key[row.score_key]
        link = _existing_link(
            links,
            run_id=run.run.run_id,
            source=str(lineage["source"]),
            source_match_id=str(lineage["source_match_id"]),
        ) or RoshRunMatchLink(
            source=str(lineage["source"]),
            source_match_id=str(lineage["source_match_id"]),
            run_id=run.run.run_id,
            map_number=lineage["map_number"],
            linked_at=_timestamp(lineage["available_at"], "available_at"),
        )
        if (
            link.linked_at != _timestamp(lineage["available_at"], "available_at")
            or link.map_number != lineage["map_number"]
        ):
            return "run_match_link_conflict"
        link_by_key[row.score_key] = link
        positioned = _positioned_heroes(target)
        assert positioned is not None
        rosh_target = RoshFeatureTarget(
            match_id=row.match_id,
            date_time=run.run.date_time,
            prediction_cutoff=target.prediction_cutoff,
            availability_mode=AvailabilityMode.RECONSTRUCTED.value,
            radiant_hero_ids=positioned[0],
            dire_hero_ids=positioned[1],
        )
        try:
            snapshot, authority = build_rosh_feature_snapshot_with_authority(
                rosh_target,
                (run,),
                artifact_root=artifact_root,
                match_links=(link,),
                run_id=run.run.run_id,
                request_plan_witness=RoshRequestPlanWitness.from_run(
                    run,
                    request_started_at=_utc(
                        lineage["request_started_at"], "request_started_at"
                    ),
                ),
            )
        except (ValueError, OSError) as error:
            return f"exact_replay_failed:{type(error).__name__}"
        if snapshot.status != "available":
            return f"exact_replay_failed:{snapshot.missing_reason or 'unknown'}"
        record = _record(
            row,
            target,
            run,
            authority,
            snapshot,
            lineage,
            link=link,
            created_at=created_at,
        )
        lineage_by_key[row.score_key] = {
            **lineage,
            "_authority": authority,
            "_snapshot": snapshot,
            "_record": record,
        }
        return True

    advance("exact_replay_passed", exact_replay, "exact_replay_failed")
    records = tuple(
        lineage_by_key[row.score_key]["_record"]
        for row in current
        if isinstance(lineage_by_key[row.score_key].get("_record"), RoshAuthorityBridgeRecord)
    )
    stages.append(RoshBridgeStage("final_eligible", len(records)))
    return (
        tuple(stages),
        tuple(reasons),
        player_identity_support,
        player_identity_diagnostics,
        records,
        len(current),
        len(records),
    )


def audit_rosh_authority_bridge(
    connection: PostgresSession,
    *,
    artifact_root: str | Path,
    max_rows: int | None = None,
    created_at: datetime | str | None = None,
    draft_targets: Mapping[int, DraftTarget] | None = None,
) -> RoshAuthorityBridgeReport:
    """Read the complete bridge funnel without mutating the connection."""

    if not isinstance(connection, PostgresSession):
        raise ValueError("connection must be a PostgresSession")
    root = Path(artifact_root)
    rows = _load_legacy_rows(connection)
    if max_rows is not None:
        if type(max_rows) is not int or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        rows = rows[:max_rows]
    formal_ids = {
        int(row["match_id"])
        for row in connection.execute(
            "SELECT match_id FROM formal_map_eligibility"
        ).fetchall()
    }
    targets = (
        _draft_targets(connection)
        if draft_targets is None
        else dict(draft_targets)
    )
    repository = RoshRunRepository(connection)
    runs = {
        str(row["run_id"]): repository.get(str(row["run_id"]))
        for row in connection.execute(
            "SELECT run_id FROM rosh_analysis_runs ORDER BY run_id"
        ).fetchall()
    }
    run_map = {key: value for key, value in runs.items() if value is not None}
    links: list[RoshRunMatchLink] = []
    for run in run_map.values():
        links.extend(repository.get_match_links(run.run.run_id))
    now = _timestamp(created_at or datetime.now(_UTC), "created_at")
    (
        stages,
        reasons,
        player_identity_support,
        player_identity_diagnostics,
        records,
        _snapshot_attempts,
        snapshot_available,
    ) = _audit_rows(
        rows,
        formal_ids=formal_ids,
        targets=targets,
        runs=run_map,
        links=tuple(links),
        artifact_root=root,
        created_at=now,
    )
    return RoshAuthorityBridgeReport(
        version=ROSH_AUTHORITY_BRIDGE_VERSION,
        formal_maps=len(formal_ids),
        legacy_rows=len(rows),
        official_runs=len(run_map),
        official_match_links=len(links),
        stages=stages,
        missing_reasons=reasons,
        player_identity_support=player_identity_support,
        player_identity_diagnostics=player_identity_diagnostics,
        snapshot_attempts=next(
            (stage.support for stage in stages if stage.stage == "cutoff_legal"),
            0,
        ),
        snapshot_available=snapshot_available,
        eligible_records=records,
    )


def _insert_record(connection: PostgresSession, record: RoshAuthorityBridgeRecord) -> bool:
    _validate_record_hashes(record)
    values = (
        record.bridge_key,
        record.bridge_version,
        record.legacy_score_key,
        record.run_id,
        record.match_id,
        record.prediction_cutoff,
        canonical_json_bytes(record.draft).decode("utf-8"),
        (
            None
            if record.radiant_player_ids is None
            else canonical_json_bytes(list(record.radiant_player_ids)).decode("utf-8")
        ),
        (
            None
            if record.dire_player_ids is None
            else canonical_json_bytes(list(record.dire_player_ids)).decode("utf-8")
        ),
        record.player_coverage_count,
        record.rosh_profile_id,
        record.formula_version,
        record.scorer_source_hash,
        record.canonical_profile_hash,
        record.input_artifact_hash,
        record.response_artifact_hash,
        record.generated_at,
        record.available_at,
        record.source,
        record.source_match_id,
        record.map_number,
        canonical_json_bytes(record.authority).decode("utf-8"),
        canonical_json_bytes(record.snapshot).decode("utf-8"),
        record.content_hash,
        record.created_at,
    )
    existing = connection.execute(
        "SELECT content_hash FROM rosh_authority_bridge_records WHERE bridge_key=?",
        (record.bridge_key,),
    ).fetchone()
    if existing is not None:
        if str(existing["content_hash"]) != record.content_hash:
            raise ValueError("immutable R.O.S.H. bridge key collision")
        return False
    connection.execute(
        """INSERT INTO rosh_authority_bridge_records
           (bridge_key, bridge_version, legacy_score_key, run_id, match_id,
            prediction_cutoff, draft_json, radiant_player_ids_json,
            dire_player_ids_json, player_coverage_count, rosh_profile_id,
            formula_version, scorer_source_hash,
            canonical_profile_hash, input_artifact_hash, response_artifact_hash,
            generated_at, available_at, source, source_match_id, map_number,
            authority_json, snapshot_json, content_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?)""",
        values,
    )
    return True


def persist_rosh_authority_bridge(
    connection: PostgresSession,
    report: RoshAuthorityBridgeReport,
    *,
    checkpoint: Callable[[str, int], None] | None = None,
) -> RoshAuthorityBridgeReport:
    """Atomically insert eligible links and bridge records, never legacy rows."""

    if not isinstance(report, RoshAuthorityBridgeReport):
        raise ValueError("report must be a RoshAuthorityBridgeReport")
    repository = RoshRunRepository(connection)
    inserted = 0
    unchanged = 0
    with connection.transaction():
        for index, record in enumerate(report.eligible_records):
            existing = _existing_link(
                tuple(
                    link
                    for run in (
                        repository.get(record.run_id),
                    )
                    if run is not None
                    for link in repository.get_match_links(record.run_id)
                ),
                run_id=record.run_id,
                source=record.source,
                source_match_id=record.source_match_id,
            )
            if existing is None:
                repository.link_matches(
                    record.run_id,
                    (
                        {
                            "source": record.source,
                            "source_match_id": record.source_match_id,
                            "map_number": record.map_number,
                        },
                    ),
                    linked_at=record.available_at,
                )
            elif existing.linked_at != record.available_at:
                raise ValueError("existing R.O.S.H. match link timestamp disagrees")
            elif existing.map_number != record.map_number:
                raise ValueError("existing R.O.S.H. match link map number disagrees")
            if checkpoint is not None:
                checkpoint("link", index)
            if _insert_record(connection, record):
                inserted += 1
            else:
                unchanged += 1
            if checkpoint is not None:
                checkpoint("record", index)
    return replace(
        report,
        inserted_records=inserted,
        unchanged_records=unchanged,
    )


def replay_rosh_authority_bridge_record(
    connection: PostgresSession,
    record: RoshAuthorityBridgeRecord,
    *,
    artifact_root: str | Path,
) -> RoshFeatureSnapshot:
    """Recompute one persisted bridge record from official run artifacts."""

    _validate_record_hashes(record)
    repository = RoshRunRepository(connection)
    run = repository.get(record.run_id)
    if run is None:
        raise ValueError("R.O.S.H. bridge run is unavailable")
    links = repository.get_match_links(record.run_id)
    snapshot = replay_rosh_feature_snapshot(
        record.authority,
        runs=(run,),
        artifact_root=artifact_root,
        match_links=links,
    )
    if canonical_json_bytes(_snapshot_payload(snapshot)) != canonical_json_bytes(
        record.snapshot
    ):
        raise ValueError("R.O.S.H. bridge snapshot does not replay")
    return snapshot


def load_rosh_bridge_witnesses(
    connection: PostgresSession,
) -> dict[int, RoshRequestPlanWitness]:
    """Load only persisted bridge witnesses for reconstructed Prematch replay."""

    relation = connection.execute("SELECT to_regclass(?)", (ROSH_BRIDGE_TABLE,)).fetchone()
    if relation is None or relation[0] is None:
        return {}
    rows = connection.execute(
        """SELECT match_id, run_id, authority_json
             FROM rosh_authority_bridge_records
            ORDER BY match_id, created_at DESC, bridge_key DESC"""
    ).fetchall()
    result: dict[int, RoshRequestPlanWitness] = {}
    for row in rows:
        match_id = int(row["match_id"])
        if match_id in result:
            continue
        authority = _json_object(row["authority_json"], "bridge authority")
        witness = authority.get("request_plan_witness")
        if not isinstance(witness, Mapping):
            continue
        result[match_id] = RoshRequestPlanWitness(
            run_id=str(witness["run_id"]),
            request_started_at=_utc(
                witness["request_started_at"], "request_started_at"
            ),
            request_hash=str(witness["request_hash"]),
            request_artifact_hash=str(witness["request_artifact_hash"]),
        )
    return result


def report_as_dict(report: RoshAuthorityBridgeReport) -> dict[str, object]:
    return asdict(report)


def report_as_markdown(report: RoshAuthorityBridgeReport) -> str:
    lines = [
        "# R.O.S.H. Authority Bridge",
        "",
        f"- Version: `{report.version}`",
        f"- Formal maps: {report.formal_maps}",
        f"- Legacy rows: {report.legacy_rows}",
        f"- Official runs: {report.official_runs}",
        f"- Official match links: {report.official_match_links}",
        f"- Snapshot attempts: {report.snapshot_attempts}",
        f"- Snapshot available: {report.snapshot_available}",
        "",
        "## Read-only Funnel",
        "",
        "| Stage | Support |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {row.stage} | {row.support} |" for row in report.stages)
    lines.extend(("", "## Missing Reasons", "", "| Stage | Reason | Support |", "| --- | --- | ---: |"))
    lines.extend(
        f"| {row.stage} | `{row.reason}` | {row.support} |"
        for row in report.missing_reasons
    )
    lines.extend(
        (
            "",
            "## Optional Player Identity Evidence",
            "",
            "Player identity is not an input to the R.O.S.H. scorer and does not "
            "affect final eligibility.",
            "",
            f"- Matching player identity support: {report.player_identity_support}",
            "",
            "| Diagnostic | Support |",
            "| --- | ---: |",
        )
    )
    lines.extend(
        f"| `{row.reason}` | {row.support} |"
        for row in report.player_identity_diagnostics
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "ROSH_AUTHORITY_BRIDGE_VERSION",
    "ROSH_BRIDGE_LINEAGE_SCHEMA",
    "RoshAuthorityBridgeRecord",
    "RoshAuthorityBridgeReport",
    "RoshBridgeMissingReason",
    "RoshBridgeStage",
    "audit_rosh_authority_bridge",
    "load_rosh_bridge_witnesses",
    "persist_rosh_authority_bridge",
    "replay_rosh_authority_bridge_record",
    "report_as_dict",
    "report_as_markdown",
]
