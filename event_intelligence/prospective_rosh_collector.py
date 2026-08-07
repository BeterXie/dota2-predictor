"""Operational, prospective-only R.O.S.H. evidence and shadow orchestration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from database.session import PostgresSession
from event_intelligence.draft_features import ROLE_CONFIDENCE_MIN
from event_intelligence.prospective_rosh_candidate import (
    ProspectiveRoshCandidate,
    load_frozen_prospective_rosh_candidate,
    prospective_rosh_profile,
    verify_prospective_rosh_candidate,
)
from event_intelligence.prospective_rosh_shadow import (
    ArtifactIdentity,
    ProspectiveRoshShadowRepository,
    ShadowPrediction,
    ShadowSettlement,
    archive_exact_artifacts,
    artifact_manifest_hash,
    build_prospective_rosh_evidence,
    build_shadow_prediction,
)
from event_intelligence.prospective_team_rating import (
    ProspectiveTeamRatingRepository,
)
from event_intelligence.raw_archive import canonical_json_bytes
from event_intelligence.roles import PROSPECTIVE_ASSIGNMENT_VERSION
from live_betting.rosh_parity import ExactByteArtifactStore
from live_betting.stratz_rosh_client import (
    FetchedLegacyRoshBatch,
    StratzRoshError,
)


UTC = timezone.utc
PROSPECTIVE_ROSH_COLLECTOR_VERSION = "prospective-rosh-operational-collector-v1"
FROZEN_CANDIDATE_HASH = (
    "84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d"
)
FROZEN_PROFILE_ID = "legacy-dematus-pure-rosh-prospective-v1"
FROZEN_FORMULA_VERSION = "dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793"
NETWORK_RETRY_DELAYS = (
    timedelta(seconds=15),
    timedelta(seconds=60),
    timedelta(seconds=180),
)
PREREQUISITE_RETRY_DELAY = timedelta(seconds=30)
DEFAULT_FINALIZATION_MARGIN = timedelta(minutes=2)
MIN_ACCEPTANCE_MAPS = 5
MAX_ACCEPTANCE_MAPS = 10


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() is None:
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


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _actual_time_not_before(value: datetime) -> datetime:
    """Prevent an iteration timestamp from backdating a later operational write."""

    return max(_utc(value, "operational time"), datetime.now(UTC))


def load_operational_candidate() -> ProspectiveRoshCandidate:
    """Load the one allowed shadow candidate and fail closed on identity drift."""

    candidate = load_frozen_prospective_rosh_candidate()
    verify_prospective_rosh_candidate(candidate)
    profile = prospective_rosh_profile()
    if (
        candidate.artifact_hash != FROZEN_CANDIDATE_HASH
        or candidate.prospective_profile_id != FROZEN_PROFILE_ID
        or candidate.retrospective_formula_version != FROZEN_FORMULA_VERSION
        or profile.get("profile_id") != FROZEN_PROFILE_ID
        or profile.get("formula_version") != FROZEN_FORMULA_VERSION
        or profile.get("official_v2_compatible") is not False
        or profile.get("pure_lineup_only") is not True
        or profile.get("player_identity_used") is not False
    ):
        raise ValueError("frozen prospective R.O.S.H. operational identity drift")
    return candidate


class LegacyRoshTransport(Protocol):
    def fetch_legacy_lineup_batch(
        self,
        radiant_heroes: Sequence[int],
        dire_heroes: Sequence[int],
        *,
        statistics_cutoff: datetime,
    ) -> FetchedLegacyRoshBatch: ...


@dataclass(frozen=True)
class ProspectiveRoshLineup:
    match_id: int
    series_id: int
    radiant_heroes: tuple[int, ...]
    dire_heroes: tuple[int, ...]

    def __post_init__(self) -> None:
        _positive_int(self.match_id, "match_id")
        _positive_int(self.series_id, "series_id")
        heroes = (*self.radiant_heroes, *self.dire_heroes)
        if (
            len(self.radiant_heroes) != 5
            or len(self.dire_heroes) != 5
            or any(type(hero_id) is not int or hero_id <= 0 for hero_id in heroes)
            or len(set(heroes)) != 10
        ):
            raise ValueError("prospective R.O.S.H. lineup must contain ten heroes")


@dataclass(frozen=True)
class CollectionResult:
    match_id: int
    status: str
    missing_reason: str | None = None
    prediction_hash: str | None = None
    record_status: str | None = None
    request_manifest_hash: str | None = None
    response_manifest_hash: str | None = None
    exact_replay: bool | None = None
    idempotency: str | None = None


@dataclass(frozen=True)
class CausalAudit:
    audit_hash: str
    prediction_hash: str
    settlement_hash: str
    match_id: int
    authoritative_actual_start: datetime
    prediction_created_at: datetime
    causal_eligible: bool
    exclusion_reason: str | None
    audited_at: datetime
    created_at: datetime

    def to_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": PROSPECTIVE_ROSH_COLLECTOR_VERSION,
            "prediction_hash": self.prediction_hash,
            "settlement_hash": self.settlement_hash,
            "match_id": self.match_id,
            "authoritative_actual_start": self.authoritative_actual_start.isoformat(),
            "prediction_created_at": self.prediction_created_at.isoformat(),
            "causal_eligible": self.causal_eligible,
            "exclusion_reason": self.exclusion_reason,
            "audited_at": self.audited_at.isoformat(),
        }
        if include_hash:
            payload["audit_hash"] = self.audit_hash
        return payload


@dataclass(frozen=True)
class CollectionReport:
    scanned: int
    paired: int
    p0_only: int
    retry_scheduled: int
    terminal_failure: int
    unchanged: int
    settlements_stored: int
    causal_audits_stored: int
    acceptance_limit: int
    acceptance_collected: int
    acceptance_stopped: bool
    results: tuple[CollectionResult, ...]
    acceptance: tuple[Mapping[str, object], ...]


class ProspectiveRoshCollectorRepository:
    """Database boundary for collection attempts, settlement, and causal audit."""

    def __init__(self, connection: PostgresSession) -> None:
        if not isinstance(connection, PostgresSession):
            raise ValueError("connection must be a PostgresSession")
        self.connection = connection
        self.team_rating = ProspectiveTeamRatingRepository(connection)
        self.shadow = ProspectiveRoshShadowRepository(connection)

    def ensure_candidate(
        self,
        candidate: ProspectiveRoshCandidate,
        *,
        created_at: datetime,
    ) -> bool:
        created = _utc(created_at, "created_at")
        with self.connection.transaction():
            return self.shadow.store_candidate(candidate, created_at=created)

    def acceptance_count(self, candidate_hash: str) -> int:
        return int(
            self.connection.execute(
                """SELECT COUNT(*) FROM prospective_rosh_shadow_predictions
                    WHERE candidate_hash=?""",
                (_digest(candidate_hash, "candidate_hash"),),
            ).scalar_one()
        )

    def scan_target_ids(
        self,
        candidate_hash: str,
        *,
        start_at: datetime,
        end_at: datetime,
        observed_at: datetime,
        limit: int,
    ) -> tuple[int, ...]:
        start = _utc(start_at, "scan_start")
        end = _utc(end_at, "scan_end")
        observed = _utc(observed_at, "observed_at")
        bounded = _positive_int(limit, "limit")
        if end < start:
            raise ValueError("scan end precedes start")
        rows = self.connection.execute(
            """SELECT target.match_id
                 FROM matches AS target
                 JOIN match_ingest_status AS status
                   ON status.match_id=target.match_id
                 JOIN formal_events AS event ON event.event_id=status.event_id
                 LEFT JOIN prospective_rosh_shadow_predictions AS prediction
                   ON prediction.candidate_hash=?
                  AND prediction.match_id=target.match_id
                 LEFT JOIN LATERAL (
                     SELECT terminal, retry_at
                       FROM prospective_rosh_collection_attempts AS attempt
                      WHERE attempt.candidate_hash=?
                        AND attempt.match_id=target.match_id
                      ORDER BY attempt_number DESC LIMIT 1
                 ) AS latest_attempt ON true
                WHERE target.start_time >= ?
                  AND target.start_time <= ?
                  AND target.radiant_team_id IS NOT NULL
                  AND target.dire_team_id IS NOT NULL
                  AND status.stage_in_scope=1
                  AND status.is_exhibition=0
                  AND status.is_forfeit=0
                  AND status.is_void_remake=0
                  AND (status.stage_scope='main_event' OR
                       (status.stage_scope='internal_lcq' AND
                        event.include_internal_lcq=1))
                  AND prediction.prediction_hash IS NULL
                  AND (latest_attempt.terminal IS NULL OR
                       (latest_attempt.terminal IS FALSE AND
                        live_text_timestamp_utc(latest_attempt.retry_at) <=
                            live_text_timestamp_utc(?)))
                ORDER BY target.start_time, target.match_id
                LIMIT ?""",
            (
                candidate_hash,
                candidate_hash,
                int(start.timestamp()),
                int(end.timestamp()),
                observed.isoformat(),
                bounded,
            ),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def load_lineup(
        self,
        match_id: int,
        *,
        series_id: int,
        prediction_cutoff: datetime,
        observed_at: datetime,
    ) -> tuple[ProspectiveRoshLineup | None, str | None]:
        match = _positive_int(match_id, "match_id")
        cutoff = _utc(prediction_cutoff, "prediction_cutoff")
        observed = _utc(observed_at, "observed_at")
        rows = self.connection.execute(
            """SELECT player.player_slot, player.hero_id, player.is_radiant,
                      role.position, role.confidence, role.input_cutoff,
                      role.created_at
                 FROM match_players AS player
                 LEFT JOIN player_role_assignments AS role
                   ON role.match_id=player.match_id
                  AND role.player_slot=player.player_slot
                  AND role.purpose='expected_position'
                  AND role.assignment_version=?
                WHERE player.match_id=?
                ORDER BY player.is_radiant DESC, player.player_slot""",
            (PROSPECTIVE_ASSIGNMENT_VERSION, match),
        ).fetchall()
        slots = [row["player_slot"] for row in rows]
        heroes = [row["hero_id"] for row in rows]
        sides = [row["is_radiant"] for row in rows]
        if (
            len(rows) != 10
            or len(set(slots)) != 10
            or any(type(hero_id) is not int or hero_id <= 0 for hero_id in heroes)
            or any(side is None for side in sides)
            or sum(bool(side) for side in sides) != 5
            or len(set(heroes)) != 10
        ):
            return None, "ten_heroes_incomplete"
        positioned: dict[bool, dict[int, int]] = {True: {}, False: {}}
        for row in rows:
            position = row["position"]
            confidence = row["confidence"]
            try:
                input_cutoff = _parse_utc(row["input_cutoff"], "role input_cutoff")
                role_created = _parse_utc(row["created_at"], "role created_at")
            except ValueError:
                return None, "expected_positions_incomplete"
            if (
                type(position) is not int
                or position not in {1, 2, 3, 4, 5}
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or float(confidence) < ROLE_CONFIDENCE_MIN
                or input_cutoff > cutoff
                or input_cutoff > observed
                or role_created > cutoff
                or role_created > observed
            ):
                return None, "expected_positions_incomplete"
            side = bool(row["is_radiant"])
            if position in positioned[side]:
                return None, "expected_positions_incomplete"
            positioned[side][position] = int(row["hero_id"])
        if any(set(values) != {1, 2, 3, 4, 5} for values in positioned.values()):
            return None, "expected_positions_incomplete"
        return (
            ProspectiveRoshLineup(
                match_id=match,
                series_id=_positive_int(series_id, "series_id"),
                radiant_heroes=tuple(positioned[True][value] for value in range(1, 6)),
                dire_heroes=tuple(positioned[False][value] for value in range(1, 6)),
            ),
            None,
        )

    def existing_prediction_hash(
        self,
        candidate_hash: str,
        match_id: int,
    ) -> str | None:
        row = self.connection.execute(
            """SELECT prediction_hash
                 FROM prospective_rosh_shadow_predictions
                WHERE candidate_hash=? AND match_id=?""",
            (candidate_hash, _positive_int(match_id, "match_id")),
        ).fetchone()
        return None if row is None else str(row[0])

    def count_network_attempts(self, candidate_hash: str, match_id: int) -> int:
        return int(
            self.connection.execute(
                """SELECT COUNT(*) FROM prospective_rosh_collection_attempts
                    WHERE candidate_hash=? AND match_id=?
                      AND missing_reason LIKE 'stratz_%'""",
                (candidate_hash, _positive_int(match_id, "match_id")),
            ).scalar_one()
        )

    def record_attempt(
        self,
        candidate: ProspectiveRoshCandidate,
        *,
        match_id: int,
        prediction_cutoff: datetime,
        attempted_at: datetime,
        status: str,
        missing_reason: str | None,
        retry_at: datetime | None = None,
        request_artifacts: Sequence[ArtifactIdentity] | None = None,
        response_artifacts: Sequence[ArtifactIdentity] | None = None,
        created_at: datetime | None = None,
    ) -> str:
        cutoff = _utc(prediction_cutoff, "prediction_cutoff")
        attempted = _utc(attempted_at, "attempted_at")
        created = attempted if created_at is None else _utc(created_at, "created_at")
        retry = None if retry_at is None else _utc(retry_at, "retry_at")
        terminal = status != "retry_scheduled"
        if (status == "retry_scheduled") != (retry is not None):
            raise ValueError("retry_scheduled requires retry_at")
        requests = None if request_artifacts is None else tuple(request_artifacts)
        responses = None if response_artifacts is None else tuple(response_artifacts)
        if (requests is None) != (responses is None):
            raise ValueError("request and response artifact bundles must be paired")
        prior = int(
            self.connection.execute(
                """SELECT COUNT(*) FROM prospective_rosh_collection_attempts
                    WHERE candidate_hash=? AND match_id=?""",
                (candidate.artifact_hash, _positive_int(match_id, "match_id")),
            ).scalar_one()
        )
        number = prior + 1
        request_payload = (
            None if requests is None else [value.to_payload() for value in requests]
        )
        response_payload = (
            None if responses is None else [value.to_payload() for value in responses]
        )
        payload = {
            "version": PROSPECTIVE_ROSH_COLLECTOR_VERSION,
            "candidate_hash": candidate.artifact_hash,
            "match_id": match_id,
            "prediction_cutoff": cutoff.isoformat(),
            "attempt_number": number,
            "attempted_at": attempted.isoformat(),
            "status": status,
            "missing_reason": missing_reason,
            "retry_at": None if retry is None else retry.isoformat(),
            "terminal": terminal,
            "request_artifacts": request_payload,
            "response_artifacts": response_payload,
        }
        attempt_hash = _hash(payload)
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO prospective_rosh_collection_attempts
                   (attempt_hash, candidate_hash, match_id, prediction_cutoff,
                    attempt_number, attempted_at, status, missing_reason,
                    retry_at, terminal, request_artifacts_json,
                    request_manifest_hash, response_artifacts_json,
                    response_manifest_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt_hash,
                    candidate.artifact_hash,
                    match_id,
                    cutoff.isoformat(),
                    number,
                    attempted.isoformat(),
                    status,
                    missing_reason,
                    None if retry is None else retry.isoformat(),
                    terminal,
                    None if request_payload is None else json.dumps(
                        request_payload, sort_keys=True, separators=(",", ":")
                    ),
                    None if requests is None else artifact_manifest_hash(requests),
                    None if response_payload is None else json.dumps(
                        response_payload, sort_keys=True, separators=(",", ":")
                    ),
                    None if responses is None else artifact_manifest_hash(responses),
                    created.isoformat(),
                ),
            )
        return attempt_hash

    def store_prediction_verified(self, record: ShadowPrediction) -> tuple[bool, bool]:
        with self.connection.transaction():
            inserted = self.shadow.store_prediction(record)
            unchanged = not self.shadow.store_prediction(record)
        if not unchanged:
            raise ValueError("shadow prediction idempotency verification failed")
        return inserted, unchanged

    def settle_and_audit_ready(
        self,
        candidate_hash: str,
        *,
        observed_at: datetime,
        limit: int,
    ) -> tuple[int, int]:
        observed = _utc(observed_at, "observed_at")
        rows = self.connection.execute(
            """SELECT prediction.prediction_hash, prediction.match_id,
                      prediction.prediction_cutoff, prediction.created_at,
                      target.start_time, target.duration, target.radiant_win,
                      status.has_valid_result, status.basic_result_state,
                      status.first_usable_at, status.latest_raw_content_hash,
                      settlement.settlement_hash,
                      settlement.eventual_radiant_win,
                      settlement.result_artifact_hash,
                      settlement.result_usable_at,
                      settlement.settled_at, settlement.created_at AS settlement_created_at,
                      audit.audit_hash
                 FROM prospective_rosh_shadow_predictions AS prediction
                 JOIN matches AS target ON target.match_id=prediction.match_id
                 JOIN match_ingest_status AS status ON status.match_id=prediction.match_id
                 LEFT JOIN prospective_rosh_shadow_settlements AS settlement
                   ON settlement.prediction_hash=prediction.prediction_hash
                 LEFT JOIN prospective_rosh_causal_audits AS audit
                   ON audit.prediction_hash=prediction.prediction_hash
                WHERE prediction.candidate_hash=?
                  AND target.radiant_win IS NOT NULL
                  AND status.has_valid_result=1
                  AND status.basic_result_state='ready'
                  AND status.first_usable_at IS NOT NULL
                  AND status.latest_raw_content_hash IS NOT NULL
                  AND audit.audit_hash IS NULL
                ORDER BY live_text_timestamp_utc(prediction.prediction_cutoff),
                         prediction.match_id
                LIMIT ?""",
            (candidate_hash, _positive_int(limit, "limit")),
        ).fetchall()
        settlements = 0
        audits = 0
        for row in rows:
            cutoff = _parse_utc(row["prediction_cutoff"], "prediction_cutoff")
            result_usable = _parse_utc(row["first_usable_at"], "result_usable_at")
            if result_usable > observed or int(row["start_time"] or 0) <= 0:
                continue
            if row["settlement_hash"] is None:
                draft = ShadowSettlement(
                    settlement_hash="",
                    prediction_hash=str(row["prediction_hash"]),
                    eventual_radiant_win=1 if bool(row["radiant_win"]) else 0,
                    result_artifact_hash=_digest(
                        str(row["latest_raw_content_hash"]),
                        "result_artifact_hash",
                    ),
                    result_usable_at=result_usable,
                    settled_at=observed,
                    created_at=observed,
                )
                settlement = ShadowSettlement(
                    **{
                        **draft.__dict__,
                        "settlement_hash": _hash(
                            draft.to_payload(include_hash=False)
                        ),
                    }
                )
                if not cutoff < result_usable <= observed:
                    continue
                with self.connection.transaction():
                    if self.shadow.store_settlement(settlement):
                        settlements += 1
            else:
                settlement = ShadowSettlement(
                    settlement_hash=str(row["settlement_hash"]),
                    prediction_hash=str(row["prediction_hash"]),
                    eventual_radiant_win=int(row["eventual_radiant_win"]),
                    result_artifact_hash=str(row["result_artifact_hash"]),
                    result_usable_at=_parse_utc(
                        row["result_usable_at"], "settlement result_usable_at"
                    ),
                    settled_at=_parse_utc(row["settled_at"], "settled_at"),
                    created_at=_parse_utc(
                        row["settlement_created_at"], "settlement created_at"
                    ),
                )
            actual_start = datetime.fromtimestamp(int(row["start_time"]), UTC)
            prediction_created = _parse_utc(
                row["created_at"], "prediction created_at"
            )
            eligible = prediction_created < actual_start
            draft_audit = CausalAudit(
                audit_hash="",
                prediction_hash=str(row["prediction_hash"]),
                settlement_hash=settlement.settlement_hash,
                match_id=int(row["match_id"]),
                authoritative_actual_start=actual_start,
                prediction_created_at=prediction_created,
                causal_eligible=eligible,
                exclusion_reason=(
                    None if eligible else "prediction_not_before_actual_start"
                ),
                audited_at=observed,
                created_at=observed,
            )
            audit = CausalAudit(
                **{
                    **draft_audit.__dict__,
                    "audit_hash": _hash(draft_audit.to_payload(include_hash=False)),
                }
            )
            if self.store_causal_audit(audit):
                audits += 1
        return settlements, audits

    def store_causal_audit(self, audit: CausalAudit) -> bool:
        if audit.audit_hash != _hash(audit.to_payload(include_hash=False)):
            raise ValueError("causal audit content hash mismatch")
        existing = self.connection.execute(
            """SELECT audit_hash FROM prospective_rosh_causal_audits
                WHERE prediction_hash=?""",
            (audit.prediction_hash,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != audit.audit_hash:
                raise ValueError("immutable prospective R.O.S.H. causal audit conflict")
            return False
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO prospective_rosh_causal_audits
                   (audit_hash, prediction_hash, settlement_hash, match_id,
                    authoritative_actual_start, prediction_created_at,
                    causal_eligible, exclusion_reason, audited_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    audit.audit_hash,
                    audit.prediction_hash,
                    audit.settlement_hash,
                    audit.match_id,
                    audit.authoritative_actual_start.isoformat(),
                    audit.prediction_created_at.isoformat(),
                    audit.causal_eligible,
                    audit.exclusion_reason,
                    audit.audited_at.isoformat(),
                    audit.created_at.isoformat(),
                ),
            )
        return True

    def acceptance_rows(
        self,
        candidate: ProspectiveRoshCandidate,
        *,
        artifact_root: Path,
        limit: int,
    ) -> tuple[Mapping[str, object], ...]:
        rows = self.connection.execute(
            """SELECT prediction.*, settlement.settlement_hash,
                      audit.causal_eligible, audit.exclusion_reason,
                      EXISTS (
                          SELECT 1 FROM prospective_rosh_collection_attempts AS attempt
                           WHERE attempt.candidate_hash=prediction.candidate_hash
                             AND attempt.match_id=prediction.match_id
                             AND attempt.status='idempotency_unchanged'
                      ) AS idempotency_verified
                 FROM prospective_rosh_shadow_predictions AS prediction
                 LEFT JOIN prospective_rosh_shadow_settlements AS settlement
                   ON settlement.prediction_hash=prediction.prediction_hash
                 LEFT JOIN prospective_rosh_causal_audits AS audit
                   ON audit.prediction_hash=prediction.prediction_hash
                WHERE prediction.candidate_hash=?
                ORDER BY live_text_timestamp_utc(prediction.prediction_cutoff),
                         prediction.match_id
                LIMIT ?""",
            (candidate.artifact_hash, _positive_int(limit, "limit")),
        ).fetchall()
        result: list[Mapping[str, object]] = []
        for row in rows:
            cutoff = _parse_utc(row["prediction_cutoff"], "prediction_cutoff")
            created = _parse_utc(row["created_at"], "created_at")
            paired = str(row["record_status"]) == "paired"
            exact_replay: bool | None = None
            artifacts_complete: bool | None = None
            if paired:
                try:
                    requests = _artifact_identities(row["rosh_request_artifacts_json"])
                    responses = _artifact_identities(row["rosh_response_artifacts_json"])
                    evidence = build_prospective_rosh_evidence(
                        candidate,
                        artifact_root=artifact_root,
                        radiant_heroes=_hero_ids(row["rosh_radiant_heroes_json"]),
                        dire_heroes=_hero_ids(row["rosh_dire_heroes_json"]),
                        request_artifacts=requests,
                        response_artifacts=responses,
                        statistics_cutoff=_parse_utc(
                            row["rosh_statistics_cutoff"], "statistics_cutoff"
                        ),
                        available_at=_parse_utc(
                            row["rosh_available_at"], "available_at"
                        ),
                    )
                    artifacts_complete = True
                    exact_replay = (
                        evidence.evidence_hash == row["rosh_evidence_hash"]
                        and abs(evidence.pure_rosh_score - float(row["pure_rosh_score"]))
                        <= 1e-9
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    artifacts_complete = False
                    exact_replay = False
            causal = (
                "pending"
                if row["causal_eligible"] is None
                else "passed"
                if bool(row["causal_eligible"])
                else str(row["exclusion_reason"])
            )
            result.append(
                {
                    "match_id": int(row["match_id"]),
                    "p0_before_cutoff": created <= cutoff,
                    "rosh_before_cutoff": (
                        None
                        if not paired
                        else _parse_utc(row["rosh_available_at"], "rosh_available_at")
                        <= cutoff
                    ),
                    "shadow_status": str(row["record_status"]),
                    "missing_reason": row["missing_reason"],
                    "request_response_artifact_complete": artifacts_complete,
                    "exact_replay": exact_replay,
                    "actual_start_causal_audit": causal,
                    "settlement": row["settlement_hash"] is not None,
                    "idempotency_retry": (
                        "unchanged" if bool(row["idempotency_verified"]) else "pending"
                    ),
                }
            )
        return tuple(result)


def _artifact_identities(value: object) -> tuple[ArtifactIdentity, ...]:
    if not isinstance(value, str):
        raise ValueError("artifact identity JSON is unavailable")
    payload = json.loads(value)
    if not isinstance(payload, list):
        raise ValueError("artifact identity JSON must be an array")
    return tuple(
        ArtifactIdentity(
            operation=str(row["operation"]),
            content_sha256=str(row["content_sha256"]),
            gzip_sha256=str(row["gzip_sha256"]),
            relative_path=str(row["relative_path"]),
            byte_count=int(row["byte_count"]),
        )
        for row in payload
        if isinstance(row, Mapping)
    )


def _hero_ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, str):
        raise ValueError("hero identity JSON is unavailable")
    payload = json.loads(value)
    if not isinstance(payload, list):
        raise ValueError("hero identity JSON must be an array")
    return tuple(int(hero_id) for hero_id in payload)


def _retry_result(
    repository: ProspectiveRoshCollectorRepository,
    candidate: ProspectiveRoshCandidate,
    *,
    match_id: int,
    cutoff: datetime,
    observed_at: datetime,
    missing_reason: str,
    retry_at: datetime,
    dry_run: bool,
) -> CollectionResult:
    if not dry_run:
        repository.record_attempt(
            candidate,
            match_id=match_id,
            prediction_cutoff=cutoff,
            attempted_at=observed_at,
            status="retry_scheduled",
            missing_reason=missing_reason,
            retry_at=retry_at,
        )
    return CollectionResult(match_id, "retry_scheduled", missing_reason)


def _terminal_result(
    repository: ProspectiveRoshCollectorRepository,
    candidate: ProspectiveRoshCandidate,
    *,
    match_id: int,
    cutoff: datetime,
    observed_at: datetime,
    missing_reason: str,
    dry_run: bool,
    request_artifacts: Sequence[ArtifactIdentity] | None = None,
    response_artifacts: Sequence[ArtifactIdentity] | None = None,
    created_at: datetime | None = None,
) -> CollectionResult:
    if not dry_run:
        repository.record_attempt(
            candidate,
            match_id=match_id,
            prediction_cutoff=cutoff,
            attempted_at=observed_at,
            status="terminal_failure",
            missing_reason=missing_reason,
            request_artifacts=request_artifacts,
            response_artifacts=response_artifacts,
            created_at=created_at,
        )
    return CollectionResult(match_id, "terminal_failure", missing_reason)


def _store_prediction(
    repository: ProspectiveRoshCollectorRepository,
    candidate: ProspectiveRoshCandidate,
    prediction: ShadowPrediction,
    *,
    attempted_at: datetime,
    request_artifacts: Sequence[ArtifactIdentity] | None = None,
    response_artifacts: Sequence[ArtifactIdentity] | None = None,
) -> CollectionResult:
    inserted, unchanged = repository.store_prediction_verified(prediction)
    status = "paired_stored" if prediction.record_status == "paired" else "p0_only_stored"
    repository.record_attempt(
        candidate,
        match_id=prediction.match_id,
        prediction_cutoff=prediction.prediction_cutoff,
        attempted_at=attempted_at,
        status=status,
        missing_reason=prediction.missing_reason,
        request_artifacts=request_artifacts,
        response_artifacts=response_artifacts,
        created_at=prediction.created_at,
    )
    repository.record_attempt(
        candidate,
        match_id=prediction.match_id,
        prediction_cutoff=prediction.prediction_cutoff,
        attempted_at=attempted_at,
        status="idempotency_unchanged",
        missing_reason=prediction.missing_reason,
        request_artifacts=request_artifacts,
        response_artifacts=response_artifacts,
        created_at=prediction.created_at,
    )
    evidence = prediction.rosh_evidence
    return CollectionResult(
        match_id=prediction.match_id,
        status="stored" if inserted else "unchanged",
        missing_reason=prediction.missing_reason,
        prediction_hash=prediction.prediction_hash,
        record_status=prediction.record_status,
        request_manifest_hash=(
            None if evidence is None else evidence.request_manifest_hash
        ),
        response_manifest_hash=(
            None if evidence is None else evidence.response_manifest_hash
        ),
        exact_replay=True if evidence is not None else None,
        idempotency="unchanged" if unchanged else None,
    )


def collect_match(
    repository: ProspectiveRoshCollectorRepository,
    transport: LegacyRoshTransport,
    artifact_store: ExactByteArtifactStore,
    candidate: ProspectiveRoshCandidate,
    match_id: int,
    *,
    now: datetime,
    dry_run: bool = False,
    finalization_margin: timedelta = DEFAULT_FINALIZATION_MARGIN,
) -> CollectionResult:
    """Collect one target without ever writing a shadow prediction after cutoff."""

    observed = _utc(now, "now")
    target, has_result = repository.team_rating.load_target(match_id)
    cutoff = target.prediction_cutoff
    existing = repository.existing_prediction_hash(candidate.artifact_hash, match_id)
    if existing is not None:
        return CollectionResult(
            match_id,
            "unchanged",
            prediction_hash=existing,
            idempotency="unchanged",
        )
    if has_result or observed >= cutoff:
        return _terminal_result(
            repository,
            candidate,
            match_id=match_id,
            cutoff=cutoff,
            observed_at=observed,
            missing_reason=(
                "target_result_already_available" if has_result else "cutoff_elapsed"
            ),
            dry_run=dry_run,
        )
    team_rating = (
        repository.team_rating.load_rosh_team_rating_authority(match_id)
        if dry_run
        else repository.team_rating.resolve_rosh_team_rating_authority(
            match_id,
            observed_at=observed,
        )
    )
    if team_rating is None:
        retry = min(observed + PREREQUISITE_RETRY_DELAY, cutoff)
        if retry >= cutoff:
            return _terminal_result(
                repository,
                candidate,
                match_id=match_id,
                cutoff=cutoff,
                observed_at=observed,
                missing_reason="prospective_team_rating_unavailable",
                dry_run=dry_run,
            )
        return _retry_result(
            repository,
            candidate,
            match_id=match_id,
            cutoff=cutoff,
            observed_at=observed,
            missing_reason="prospective_team_rating_unavailable",
            retry_at=retry,
            dry_run=dry_run,
        )
    if target.target.series_id is None:
        return _terminal_result(
            repository,
            candidate,
            match_id=match_id,
            cutoff=cutoff,
            observed_at=observed,
            missing_reason="formal_series_unavailable",
            dry_run=dry_run,
        )
    lineup, lineup_reason = repository.load_lineup(
        match_id,
        series_id=target.target.series_id,
        prediction_cutoff=cutoff,
        observed_at=observed,
    )
    if lineup is None:
        reason = lineup_reason or "expected_positions_incomplete"
        if observed < cutoff - finalization_margin:
            return _retry_result(
                repository,
                candidate,
                match_id=match_id,
                cutoff=cutoff,
                observed_at=observed,
                missing_reason=reason,
                retry_at=min(observed + PREREQUISITE_RETRY_DELAY, cutoff),
                dry_run=dry_run,
            )
        created_at = _actual_time_not_before(observed)
        if created_at >= cutoff:
            return _terminal_result(
                repository,
                candidate,
                match_id=match_id,
                cutoff=cutoff,
                observed_at=observed,
                missing_reason="cutoff_elapsed",
                dry_run=dry_run,
                created_at=created_at,
            )
        prediction = build_shadow_prediction(
            candidate,
            match_id=match_id,
            series_id=target.target.series_id,
            team_rating=team_rating,
            rosh_evidence=None,
            missing_reason=reason,
            created_at=created_at,
        )
        if dry_run:
            return CollectionResult(match_id, "dry_run", reason, record_status="p0_only")
        return _store_prediction(
            repository,
            candidate,
            prediction,
            attempted_at=observed,
        )
    if dry_run:
        return CollectionResult(match_id, "dry_run", record_status="paired")
    statistics_cutoff = observed
    try:
        batch = transport.fetch_legacy_lineup_batch(
            lineup.radiant_heroes,
            lineup.dire_heroes,
            statistics_cutoff=statistics_cutoff,
        )
    except StratzRoshError as error:
        reason = f"stratz_{error.category}"
        failure_at = _actual_time_not_before(observed)
        if failure_at >= cutoff:
            return _terminal_result(
                repository,
                candidate,
                match_id=match_id,
                cutoff=cutoff,
                observed_at=observed,
                missing_reason="request_failed_after_cutoff",
                dry_run=False,
                created_at=failure_at,
            )
        attempts = repository.count_network_attempts(candidate.artifact_hash, match_id)
        if error.retryable and attempts < len(NETWORK_RETRY_DELAYS):
            delay = NETWORK_RETRY_DELAYS[attempts]
            if error.retry_after_seconds is not None:
                delay = max(delay, timedelta(seconds=error.retry_after_seconds))
            retry = failure_at + delay
            if retry < cutoff - finalization_margin:
                return _retry_result(
                    repository,
                    candidate,
                    match_id=match_id,
                    cutoff=cutoff,
                    observed_at=observed,
                    missing_reason=reason,
                    retry_at=retry,
                    dry_run=False,
                )
        prediction = build_shadow_prediction(
            candidate,
            match_id=match_id,
            series_id=lineup.series_id,
            team_rating=team_rating,
            rosh_evidence=None,
            missing_reason=reason,
            created_at=failure_at,
        )
        return _store_prediction(
            repository,
            candidate,
            prediction,
            attempted_at=observed,
        )
    available = _utc(batch.collected_at, "available_at")
    requests = archive_exact_artifacts(artifact_store, batch.request_bodies)
    responses = archive_exact_artifacts(artifact_store, batch.response_bodies)
    if statistics_cutoff > available:
        return _terminal_result(
            repository,
            candidate,
            match_id=match_id,
            cutoff=cutoff,
            observed_at=observed,
            missing_reason="statistics_cutoff_follows_availability",
            dry_run=False,
            request_artifacts=requests,
            response_artifacts=responses,
            created_at=available,
        )
    if available > cutoff:
        return _terminal_result(
            repository,
            candidate,
            match_id=match_id,
            cutoff=cutoff,
            observed_at=observed,
            missing_reason="request_completed_after_cutoff",
            dry_run=False,
            request_artifacts=requests,
            response_artifacts=responses,
            created_at=available,
        )
    try:
        evidence = build_prospective_rosh_evidence(
            candidate,
            artifact_root=artifact_store.root,
            radiant_heroes=lineup.radiant_heroes,
            dire_heroes=lineup.dire_heroes,
            request_artifacts=requests,
            response_artifacts=responses,
            statistics_cutoff=statistics_cutoff,
            available_at=available,
        )
        prediction_created_at = _actual_time_not_before(available)
        if prediction_created_at > cutoff:
            return _terminal_result(
                repository,
                candidate,
                match_id=match_id,
                cutoff=cutoff,
                observed_at=observed,
                missing_reason="artifact_replay_completed_after_cutoff",
                dry_run=False,
                request_artifacts=requests,
                response_artifacts=responses,
                created_at=prediction_created_at,
            )
        prediction = build_shadow_prediction(
            candidate,
            match_id=match_id,
            series_id=lineup.series_id,
            team_rating=team_rating,
            rosh_evidence=evidence,
            created_at=prediction_created_at,
        )
    except ValueError:
        prediction_created_at = _actual_time_not_before(available)
        if prediction_created_at > cutoff:
            return _terminal_result(
                repository,
                candidate,
                match_id=match_id,
                cutoff=cutoff,
                observed_at=observed,
                missing_reason="artifact_replay_completed_after_cutoff",
                dry_run=False,
                request_artifacts=requests,
                response_artifacts=responses,
                created_at=prediction_created_at,
            )
        prediction = build_shadow_prediction(
            candidate,
            match_id=match_id,
            series_id=lineup.series_id,
            team_rating=team_rating,
            rosh_evidence=None,
            missing_reason="rosh_evidence_invalid",
            created_at=prediction_created_at,
        )
    return _store_prediction(
        repository,
        candidate,
        prediction,
        attempted_at=observed,
        request_artifacts=requests,
        response_artifacts=responses,
    )


def run_collector_once(
    repository: ProspectiveRoshCollectorRepository,
    transport: LegacyRoshTransport,
    *,
    artifact_root: Path,
    now: datetime,
    match_id: int | None = None,
    scan_start: datetime | None = None,
    scan_end: datetime | None = None,
    limit: int = 5,
    acceptance_limit: int = MAX_ACCEPTANCE_MAPS,
    dry_run: bool = False,
) -> CollectionReport:
    """Run one bounded collection and settlement pass; never starts a 20-map gate."""

    if not MIN_ACCEPTANCE_MAPS <= acceptance_limit <= MAX_ACCEPTANCE_MAPS:
        raise ValueError("acceptance_limit must be between 5 and 10")
    observed = _utc(now, "now")
    candidate = load_operational_candidate()
    if not dry_run:
        repository.ensure_candidate(candidate, created_at=observed)
    collected = repository.acceptance_count(candidate.artifact_hash)
    remaining = max(0, acceptance_limit - collected)
    if match_id is not None:
        match_ids = () if remaining == 0 else (_positive_int(match_id, "match_id"),)
    elif remaining == 0:
        match_ids = ()
    else:
        start = observed if scan_start is None else _utc(scan_start, "scan_start")
        end = (
            observed + timedelta(hours=24)
            if scan_end is None
            else _utc(scan_end, "scan_end")
        )
        match_ids = repository.scan_target_ids(
            candidate.artifact_hash,
            start_at=start,
            end_at=end,
            observed_at=observed,
            limit=min(_positive_int(limit, "limit"), remaining),
        )
    store = ExactByteArtifactStore(artifact_root)
    results: list[CollectionResult] = []
    for target_id in match_ids:
        try:
            results.append(
                collect_match(
                    repository,
                    transport,
                    store,
                    candidate,
                    target_id,
                    now=observed,
                    dry_run=dry_run,
                )
            )
        except Exception as error:
            reason = f"collector_error_{type(error).__name__}"[:200]
            try:
                target, _has_result = repository.team_rating.load_target(target_id)
                results.append(
                    _terminal_result(
                        repository,
                        candidate,
                        match_id=target_id,
                        cutoff=target.prediction_cutoff,
                        observed_at=observed,
                        missing_reason=reason,
                        dry_run=dry_run,
                    )
                )
            except Exception:
                results.append(CollectionResult(target_id, "terminal_failure", reason))
    settlements = audits = 0
    if not dry_run:
        settlements, audits = repository.settle_and_audit_ready(
            candidate.artifact_hash,
            observed_at=observed,
            limit=acceptance_limit,
        )
    acceptance = repository.acceptance_rows(
        candidate,
        artifact_root=artifact_root,
        limit=acceptance_limit,
    )
    accepted = repository.acceptance_count(candidate.artifact_hash)
    acceptance_complete = (
        accepted >= acceptance_limit
        and len(acceptance) == acceptance_limit
        and all(
            row["settlement"] is True
            and row["actual_start_causal_audit"] != "pending"
            and row["idempotency_retry"] == "unchanged"
            for row in acceptance
        )
    )
    frozen_results = tuple(results)
    return CollectionReport(
        scanned=len(frozen_results),
        paired=sum(row.record_status == "paired" for row in frozen_results),
        p0_only=sum(row.record_status == "p0_only" for row in frozen_results),
        retry_scheduled=sum(row.status == "retry_scheduled" for row in frozen_results),
        terminal_failure=sum(row.status == "terminal_failure" for row in frozen_results),
        unchanged=sum(row.status == "unchanged" for row in frozen_results),
        settlements_stored=settlements,
        causal_audits_stored=audits,
        acceptance_limit=acceptance_limit,
        acceptance_collected=accepted,
        acceptance_stopped=acceptance_complete,
        results=frozen_results,
        acceptance=acceptance,
    )


__all__ = [
    "FROZEN_CANDIDATE_HASH",
    "FROZEN_FORMULA_VERSION",
    "FROZEN_PROFILE_ID",
    "MAX_ACCEPTANCE_MAPS",
    "MIN_ACCEPTANCE_MAPS",
    "CausalAudit",
    "CollectionReport",
    "CollectionResult",
    "ProspectiveRoshCollectorRepository",
    "ProspectiveRoshLineup",
    "collect_match",
    "load_operational_candidate",
    "run_collector_once",
]
