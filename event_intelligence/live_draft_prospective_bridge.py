"""Bridge a locked live draft to immutable prospective Team Rating/R.O.S.H. shadows."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from database.session import PostgresSession
from event_intelligence.prospective_rosh_candidate import (
    ProspectiveRoshCandidate,
    candidate_probability,
    load_frozen_prospective_rosh_candidate,
    verify_prospective_rosh_candidate,
)
from event_intelligence.prospective_rosh_shadow import (
    ProspectiveRoshEvidence,
    ProspectiveRoshShadowRepository,
    archive_exact_artifacts,
    build_prospective_rosh_evidence,
)
from event_intelligence.prospective_team_rating import (
    AuthoritativeResult,
    ProspectiveTeamRatingRepository,
    team_rating_state_hash,
)
from event_intelligence.raw_archive import canonical_json_bytes
from event_intelligence.team_rating import (
    TeamRatingState,
    effective_team_rating,
    team_rating_probability,
    update_team_ratings,
)
from live_betting.rosh_parity import ExactByteArtifactStore
from live_betting.stratz_rosh_client import StratzRoshClient, StratzRoshError


UTC = timezone.utc
BRIDGE_VERSION = "live-draft-prospective-bridge-v1"
FROZEN_CANDIDATE_HASH = (
    "84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d"
)
LOCK_CONFIRMATION = (
    "本次预测只使用已锁定阵容，未使用击杀、经济、经验、防御塔、肉山或其他游戏内状态。"
)
_DRAFT_MARKERS = frozenset({"draft", "draft_complete", "pre_game"})
_GAMEPLAY_MARKERS = frozenset({"gameplay", "in_game", "post_game"})


class LegacyRoshTransport(Protocol):
    def fetch_legacy_lineup_batch(
        self,
        radiant_heroes: Sequence[int],
        dire_heroes: Sequence[int],
        *,
        statistics_cutoff: datetime,
    ) -> Any: ...


def _canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


def _required_text(value: object, field: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _digest(value: object, field: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _probability(value: object, field: str) -> float:
    result = float(value)
    if not math.isfinite(result) or not 0.0 < result < 1.0:
        raise ValueError(f"{field} must be a strict probability")
    return result


def _mapping_payload(mapping: Mapping[str, Any]) -> dict[str, object]:
    slots = tuple(mapping.get("slots") or ())
    if len(slots) != 10:
        raise ValueError("locked draft mapping must contain exactly ten slots")
    normalized: list[dict[str, object]] = []
    heroes: set[int] = set()
    teams: dict[str, set[int]] = {"radiant": set(), "dire": set()}
    positions: dict[str, set[int]] = {"radiant": set(), "dire": set()}
    for raw in slots:
        if not isinstance(raw, Mapping):
            raise ValueError("draft slot is invalid")
        side = str(raw.get("side"))
        position = int(raw.get("position") or 0)
        team_id = int(raw.get("team_id") or 0)
        hero_id = int(raw.get("hero_id") or 0)
        player_id = raw.get("player_id")
        if side not in teams or position not in range(1, 6):
            raise ValueError("draft side and position are invalid")
        if team_id <= 0 or hero_id <= 0:
            raise ValueError("draft team and hero identities must be positive")
        if player_id is not None and int(player_id) <= 0:
            raise ValueError("draft player identity must be positive when present")
        if hero_id in heroes or position in positions[side]:
            raise ValueError("draft heroes and side positions must be unique")
        heroes.add(hero_id)
        teams[side].add(team_id)
        positions[side].add(position)
        normalized.append(
            {
                "side": side,
                "position": position,
                "team_id": team_id,
                "hero_id": hero_id,
                "player_id": None if player_id is None else int(player_id),
            }
        )
    if any(value != set(range(1, 6)) for value in positions.values()):
        raise ValueError("each draft side must contain positions 1 through 5")
    if any(len(value) != 1 for value in teams.values()) or teams["radiant"] == teams["dire"]:
        raise ValueError("draft mapping must contain two distinct canonical teams")
    return {
        "raybet_match_id": _required_text(mapping.get("raybet_match_id"), "raybet_match_id"),
        "map_number": int(mapping.get("map_number") or 0),
        "version": int(mapping.get("version") or 0),
        "is_locked": bool(mapping.get("is_locked")),
        "created_by": _required_text(mapping.get("created_by"), "created_by"),
        "created_at": _parse_utc(mapping.get("created_at"), "mapping created_at").isoformat(),
        "slots": sorted(normalized, key=lambda row: (row["side"] != "radiant", row["position"])),
    }


def canonical_mapping_hash(mapping: Mapping[str, Any]) -> str:
    payload = _mapping_payload(mapping)
    if not payload["is_locked"]:
        raise ValueError("draft mapping must be locked")
    if int(payload["map_number"]) not in range(1, 6) or int(payload["version"]) <= 0:
        raise ValueError("draft mapping identity is invalid")
    return _hash(payload)


@dataclass(frozen=True)
class LiveTeamRatingP0:
    seed_hash: str
    configuration_hash: str
    base_authority_hash: str | None
    base_as_of: datetime
    base_state_hash: str
    applied_results: tuple[AuthoritativeResult, ...]
    applied_result_manifest_hash: str
    state_before_hash: str
    training_input_hash: str
    radiant_rating: float
    dire_rating: float
    rating_difference: float
    support: int
    probability: float
    artifact_hash: str

    def to_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "availability_mode": "prospective",
            "rating_version": "team-rating-elo-v1",
            "seed_hash": self.seed_hash,
            "configuration_hash": self.configuration_hash,
            "base_authority_hash": self.base_authority_hash,
            "base_as_of": self.base_as_of.isoformat(),
            "base_state_hash": self.base_state_hash,
            "applied_results": [row.to_payload() for row in self.applied_results],
            "applied_result_manifest_hash": self.applied_result_manifest_hash,
            "state_before_hash": self.state_before_hash,
            "training_input_hash": self.training_input_hash,
            "radiant_rating": self.radiant_rating,
            "dire_rating": self.dire_rating,
            "rating_difference": self.rating_difference,
            "support": self.support,
            "raw_probability": self.probability,
            "inputs": ["canonical_team_ids", "pre_lock_authoritative_results", "frozen_seed_state"],
        }
        if include_hash:
            payload["artifact_hash"] = self.artifact_hash
        return payload


@dataclass(frozen=True)
class LiveDraftProspectivePrediction:
    prediction_hash: str
    raybet_match_id: str
    map_number: int
    mapping_version: int
    mapping_hash: str
    operator_locked_at: datetime
    operator_identity: str
    confirmation_text: str
    confirmed_at: datetime
    radiant_team_id: int
    dire_team_id: int
    p0: LiveTeamRatingP0
    candidate_hash: str
    record_status: str
    p1_probability: float | None
    pure_rosh_score: float | None
    standardized_rosh_score: float | None
    rosh_logit_contribution: float | None
    rosh_evidence: ProspectiveRoshEvidence | None
    missing_reason: str | None
    game_clock_seconds: int | None
    vision_frame_timestamp: datetime | None
    draft_state_marker: str | None
    live_state_input_used: bool
    causal_status: str
    causal_reason: str | None
    created_at: datetime

    def to_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": BRIDGE_VERSION,
            "identity": {
                "raybet_match_id": self.raybet_match_id,
                "map_number": self.map_number,
                "mapping_version": self.mapping_version,
                "mapping_hash": self.mapping_hash,
            },
            "operator_locked_at": self.operator_locked_at.isoformat(),
            "operator_identity": self.operator_identity,
            "confirmation_text": self.confirmation_text,
            "confirmed_at": self.confirmed_at.isoformat(),
            "radiant_team_id": self.radiant_team_id,
            "dire_team_id": self.dire_team_id,
            "team_rating_p0": self.p0.to_payload(),
            "candidate_hash": self.candidate_hash,
            "official_v2_compatible": False,
            "record_status": self.record_status,
            "p0_probability": self.p0.probability,
            "p1_probability": self.p1_probability,
            "pure_rosh_score": self.pure_rosh_score,
            "standardized_rosh_score": self.standardized_rosh_score,
            "rosh_logit_contribution": self.rosh_logit_contribution,
            "rosh_evidence": None if self.rosh_evidence is None else self.rosh_evidence.to_payload(),
            "missing_reason": self.missing_reason,
            "causal_evidence": {
                "game_clock_seconds": self.game_clock_seconds,
                "vision_frame_timestamp": (
                    None if self.vision_frame_timestamp is None else self.vision_frame_timestamp.isoformat()
                ),
                "draft_state_marker": self.draft_state_marker,
                "live_state_input_used": self.live_state_input_used,
                "causal_status": self.causal_status,
                "causal_reason": self.causal_reason,
            },
            "model_inputs": {
                "allowed": [
                    "canonical_team_ids",
                    "pre_lock_authoritative_results",
                    "frozen_team_rating_seed_state",
                    "locked_heroes_and_expected_positions",
                    "archived_rosh_statistics",
                ],
                "excluded": [
                    "game_clock",
                    "kills",
                    "net_worth",
                    "xp",
                    "towers",
                    "roshan_state",
                    "live_odds",
                    "current_map_gameplay_observations",
                ],
            },
            "created_at": self.created_at.isoformat(),
        }
        if include_hash:
            payload["prediction_hash"] = self.prediction_hash
        return payload


def _causal_status(
    *,
    live_state_input_used: bool,
    game_clock_seconds: int | None,
    draft_state_marker: str | None,
) -> tuple[str, str | None]:
    if live_state_input_used:
        return "ineligible", "live_state_input_used"
    if game_clock_seconds is not None and game_clock_seconds > 0:
        return "ineligible", "positive_game_clock_observed"
    if draft_state_marker in _GAMEPLAY_MARKERS:
        return "ineligible", "gameplay_state_observed"
    if draft_state_marker in _DRAFT_MARKERS and game_clock_seconds in {None, 0}:
        return "eligible", None
    return "unverified", "draft_lock_timing_evidence_insufficient"


def _team_state(states: Sequence[TeamRatingState], team_id: int, initial: float) -> TeamRatingState:
    return next(
        (state for state in states if state.team_id == team_id),
        TeamRatingState(team_id, initial, 0, (), None),
    )


class LiveDraftProspectiveBridgeRepository:
    def __init__(self, connection: PostgresSession) -> None:
        self.connection = connection
        self.team_rating = ProspectiveTeamRatingRepository(connection)

    def load_mapping(
        self,
        raybet_match_id: str,
        map_number: int,
        mapping_version: int,
    ) -> dict[str, object]:
        rows = self.connection.execute(
            """SELECT team_id, side, position, hero_id, player_id, is_locked,
                      created_by, created_at
                 FROM live_draft_mappings
                WHERE raybet_match_id=? AND map_number=? AND version=?
                ORDER BY CASE side WHEN 'radiant' THEN 0 ELSE 1 END, position""",
            (raybet_match_id, map_number, mapping_version),
        ).fetchall()
        if len(rows) != 10:
            raise ValueError("locked_draft_mapping_unavailable")
        mapping = {
            "raybet_match_id": raybet_match_id,
            "map_number": map_number,
            "version": mapping_version,
            "is_locked": all(bool(row[5]) for row in rows),
            "created_by": str(rows[0][6]),
            "created_at": str(rows[0][7]),
            "slots": [
                {
                    "team_id": int(row[0]),
                    "side": str(row[1]),
                    "position": int(row[2]),
                    "hero_id": int(row[3]),
                    "player_id": None if row[4] is None else int(row[4]),
                }
                for row in rows
            ],
        }
        canonical_mapping_hash(mapping)
        return mapping

    def build_p0(
        self,
        mapping: Mapping[str, Any],
        *,
        observed_at: datetime,
    ) -> LiveTeamRatingP0 | None:
        cutoff = _parse_utc(mapping["created_at"], "operator_locked_at")
        observed = _utc(observed_at, "observed_at")
        seed = self.team_rating.load_seed(cutoff)
        if seed is None:
            return None
        base = self.team_rating.load_base_state(seed, cutoff)
        results = self.team_rating.load_results(
            after=base.as_of,
            cutoff=cutoff,
            observed_at=observed,
            target_match_id=None,
            allow_seed_observation=True,
        )
        states = base.states
        for result in results:
            assert result.row.result_usable_at is not None
            states = update_team_ratings(
                states,
                result.row,
                result.row.result_usable_at,
                seed.config,
            )
        payload = _mapping_payload(mapping)
        slots = payload["slots"]
        assert isinstance(slots, list)
        radiant_team_id = int(next(row["team_id"] for row in slots if row["side"] == "radiant"))
        dire_team_id = int(next(row["team_id"] for row in slots if row["side"] == "dire"))
        radiant_state = _team_state(states, radiant_team_id, seed.config.initial_rating)
        dire_state = _team_state(states, dire_team_id, seed.config.initial_rating)
        radiant_rating, _ = effective_team_rating(radiant_state, (), cutoff, seed.config)
        dire_rating, _ = effective_team_rating(dire_state, (), cutoff, seed.config)
        probability = team_rating_probability(radiant_rating, dire_rating, seed.config)
        manifest = [result.to_payload() for result in results]
        state_hash = team_rating_state_hash(states)
        training_input_hash = _hash(
            {
                "seed_hash": seed.seed_hash,
                "base_state_hash": base.state_hash,
                "applied_results": manifest,
                "operator_locked_at": cutoff.isoformat(),
            }
        )
        draft = LiveTeamRatingP0(
            seed_hash=seed.seed_hash,
            configuration_hash=seed.configuration_hash,
            base_authority_hash=base.authority_hash,
            base_as_of=base.as_of,
            base_state_hash=base.state_hash,
            applied_results=results,
            applied_result_manifest_hash=_hash(manifest),
            state_before_hash=state_hash,
            training_input_hash=training_input_hash,
            radiant_rating=radiant_rating,
            dire_rating=dire_rating,
            rating_difference=radiant_rating - dire_rating,
            support=radiant_state.maps_seen + dire_state.maps_seen,
            probability=probability,
            artifact_hash="",
        )
        return LiveTeamRatingP0(
            **{**draft.__dict__, "artifact_hash": _hash(draft.to_payload(include_hash=False))}
        )

    def latest_observed_game_clock(
        self,
        raybet_match_id: str,
        map_number: int,
        confirmed_at: datetime,
    ) -> int | None:
        row = self.connection.execute(
            """SELECT game_time_seconds FROM live_game_snapshots
                WHERE raybet_match_id=? AND map_number=?
                  AND live_text_timestamp_utc(captured_at) <= live_text_timestamp_utc(?)
                ORDER BY live_text_timestamp_utc(captured_at) DESC, snapshot_id DESC LIMIT 1""",
            (raybet_match_id, map_number, confirmed_at.isoformat()),
        ).fetchone()
        return None if row is None else int(row[0])

    def load_prediction(
        self,
        raybet_match_id: str,
        map_number: int,
        mapping_version: int,
    ) -> dict[str, object] | None:
        row = self.connection.execute(
            """SELECT artifact_json FROM live_draft_prospective_predictions
                WHERE raybet_match_id=? AND map_number=? AND mapping_version=?""",
            (raybet_match_id, map_number, mapping_version),
        ).fetchone()
        return None if row is None else json.loads(str(row[0]))

    def persist_prediction(self, prediction: LiveDraftProspectivePrediction) -> bool:
        artifact_json = _canonical_json(prediction.to_payload())
        existing = self.connection.execute(
            """SELECT prediction_hash FROM live_draft_prospective_predictions
                WHERE raybet_match_id=? AND map_number=? AND mapping_version=?""",
            (prediction.raybet_match_id, prediction.map_number, prediction.mapping_version),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != prediction.prediction_hash:
                raise ValueError("immutable live draft prospective prediction conflict")
            return False
        evidence = prediction.rosh_evidence
        with self.connection.transaction():
            inserted = self.connection.execute(
                """INSERT INTO live_draft_prospective_predictions
                   (prediction_hash, bridge_version, raybet_match_id, map_number,
                    mapping_version, mapping_hash, operator_locked_at,
                    operator_identity, confirmation_text, confirmed_at,
                    radiant_team_id, dire_team_id, team_rating_seed_hash,
                    team_rating_configuration_hash, team_rating_base_state_hash,
                    team_rating_applied_manifest_hash, team_rating_state_before_hash,
                    team_rating_training_input_hash, team_rating_artifact_hash,
                    radiant_rating, dire_rating, rating_difference, support,
                    p0_probability, candidate_hash, record_status, p1_probability,
                    pure_rosh_score, standardized_rosh_score, rosh_logit_contribution,
                    rosh_request_manifest_hash, rosh_response_manifest_hash,
                    rosh_evidence_hash, rosh_statistics_cutoff, rosh_available_at,
                    missing_reason, game_clock_seconds, vision_frame_timestamp,
                    draft_state_marker, live_state_input_used, causal_status,
                    causal_reason, artifact_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?)
                   ON CONFLICT DO NOTHING RETURNING prediction_hash""",
                (
                    prediction.prediction_hash,
                    BRIDGE_VERSION,
                    prediction.raybet_match_id,
                    prediction.map_number,
                    prediction.mapping_version,
                    prediction.mapping_hash,
                    prediction.operator_locked_at.isoformat(),
                    prediction.operator_identity,
                    prediction.confirmation_text,
                    prediction.confirmed_at.isoformat(),
                    prediction.radiant_team_id,
                    prediction.dire_team_id,
                    prediction.p0.seed_hash,
                    prediction.p0.configuration_hash,
                    prediction.p0.base_state_hash,
                    prediction.p0.applied_result_manifest_hash,
                    prediction.p0.state_before_hash,
                    prediction.p0.training_input_hash,
                    prediction.p0.artifact_hash,
                    prediction.p0.radiant_rating,
                    prediction.p0.dire_rating,
                    prediction.p0.rating_difference,
                    prediction.p0.support,
                    prediction.p0.probability,
                    prediction.candidate_hash,
                    prediction.record_status,
                    prediction.p1_probability,
                    prediction.pure_rosh_score,
                    prediction.standardized_rosh_score,
                    prediction.rosh_logit_contribution,
                    None if evidence is None else evidence.request_manifest_hash,
                    None if evidence is None else evidence.response_manifest_hash,
                    None if evidence is None else evidence.evidence_hash,
                    None if evidence is None else evidence.statistics_cutoff.isoformat(),
                    None if evidence is None else evidence.available_at.isoformat(),
                    prediction.missing_reason,
                    prediction.game_clock_seconds,
                    None if prediction.vision_frame_timestamp is None else prediction.vision_frame_timestamp.isoformat(),
                    prediction.draft_state_marker,
                    prediction.live_state_input_used,
                    prediction.causal_status,
                    prediction.causal_reason,
                    artifact_json,
                    prediction.created_at.isoformat(),
                ),
            ).fetchone()
        if inserted is None:
            raise ValueError("immutable live draft prospective prediction conflict")
        return True

    def settle_prediction(
        self,
        prediction_hash: str,
        *,
        settled_at: datetime,
    ) -> dict[str, object] | None:
        digest = _digest(prediction_hash, "prediction_hash")
        settled = _utc(settled_at, "settled_at")
        row = self.connection.execute(
            """SELECT prediction.raybet_match_id, prediction.map_number,
                      prediction.radiant_team_id, prediction.dire_team_id,
                      prediction.causal_status, prediction.causal_reason,
                      prediction.created_at, result.strict_mapping_id,
                      result.dota_match_id, result.winner_side,
                      result.first_usable_at, evidence.opendota_content_hash,
                      mapping.canonical_team_one_id, mapping.canonical_team_two_id,
                      target.start_time
                 FROM live_draft_prospective_predictions AS prediction
                 JOIN map_results AS result
                   ON result.raybet_match_id=prediction.raybet_match_id
                  AND result.map_number=prediction.map_number
                 JOIN strict_live_map_mappings AS mapping
                   ON mapping.mapping_id=result.strict_mapping_id
                 JOIN matches AS target ON target.match_id=result.dota_match_id
                 JOIN settlement_result_evidence AS evidence
                   ON evidence.evidence_id=result.opendota_evidence_id
                WHERE prediction.prediction_hash=?""",
            (digest,),
        ).fetchone()
        if row is None:
            return None
        radiant_team_id = int(row[2])
        dire_team_id = int(row[3])
        team_one_id = int(row[12])
        team_two_id = int(row[13])
        if {radiant_team_id, dire_team_id} != {team_one_id, team_two_id}:
            raise ValueError("official settlement team identity disagrees")
        winning_team = team_one_id if str(row[9]) == "team_one" else team_two_id
        winner_side = "radiant" if winning_team == radiant_team_id else "dire"
        actual_start = datetime.fromtimestamp(int(row[14]), UTC)
        prediction_created = _parse_utc(row[6], "prediction created_at")
        initial_status = str(row[4])
        initial_reason = None if row[5] is None else str(row[5])
        if initial_status == "ineligible":
            causal_status, causal_reason = "ineligible", initial_reason
        elif prediction_created >= actual_start:
            causal_status = "ineligible"
            causal_reason = "prediction_not_before_authoritative_actual_start"
        elif initial_status == "eligible":
            causal_status, causal_reason = "eligible", None
        else:
            causal_status, causal_reason = "unverified", initial_reason
        result_usable_at = _parse_utc(row[10], "result_usable_at")
        if result_usable_at > settled:
            raise ValueError("settlement result is not yet authoritative")
        payload: dict[str, object] = {
            "prediction_hash": digest,
            "strict_mapping_id": int(row[7]),
            "dota_match_id": int(row[8]),
            "winner_side": winner_side,
            "result_evidence_hash": _digest(row[11], "result_evidence_hash"),
            "authoritative_actual_start": actual_start.isoformat(),
            "result_usable_at": result_usable_at.isoformat(),
            "post_settlement_causal_status": causal_status,
            "post_settlement_causal_reason": causal_reason,
            "settled_at": settled.isoformat(),
            "created_at": settled.isoformat(),
        }
        settlement_hash = _hash(payload)
        existing = self.connection.execute(
            """SELECT settlement_hash FROM live_draft_prospective_settlements
                WHERE prediction_hash=?""",
            (digest,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != settlement_hash:
                raise ValueError("immutable live draft prospective settlement conflict")
            return {"settlement_hash": settlement_hash, **payload}
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO live_draft_prospective_settlements
                   (settlement_hash, prediction_hash, strict_mapping_id,
                    dota_match_id, winner_side, result_evidence_hash,
                    authoritative_actual_start, result_usable_at,
                    post_settlement_causal_status,
                    post_settlement_causal_reason, settled_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    settlement_hash,
                    digest,
                    payload["strict_mapping_id"],
                    payload["dota_match_id"],
                    winner_side,
                    payload["result_evidence_hash"],
                    payload["authoritative_actual_start"],
                    payload["result_usable_at"],
                    causal_status,
                    causal_reason,
                    payload["settled_at"],
                    payload["created_at"],
                ),
            )
        return {"settlement_hash": settlement_hash, **payload}


def generate_live_draft_prediction(
    repository: LiveDraftProspectiveBridgeRepository,
    transport: LegacyRoshTransport | None = None,
    *,
    artifact_root: str | Path,
    raybet_match_id: str,
    map_number: int,
    mapping_version: int,
    operator_identity: str,
    confirmation_text: str,
    confirmed_at: datetime,
    game_clock_seconds: int | None = None,
    vision_frame_timestamp: datetime | None = None,
    draft_state_marker: str | None = None,
    live_state_input_used: bool = False,
    candidate: ProspectiveRoshCandidate | None = None,
) -> dict[str, object]:
    if confirmation_text != LOCK_CONFIRMATION:
        raise ValueError("lock confirmation text does not match the frozen statement")
    confirmed = _utc(confirmed_at, "confirmed_at")
    mapping = repository.load_mapping(raybet_match_id, map_number, mapping_version)
    mapping_payload = _mapping_payload(mapping)
    if not bool(mapping_payload["is_locked"]):
        raise ValueError("draft_mapping_not_locked")
    locked_at = _parse_utc(mapping_payload["created_at"], "operator_locked_at")
    if confirmed < locked_at:
        raise ValueError("confirmation cannot precede operator lock")
    existing = repository.load_prediction(raybet_match_id, map_number, mapping_version)
    if existing is not None:
        return {"status": "unchanged", "prediction": existing}
    p0 = repository.build_p0(mapping, observed_at=confirmed)
    if p0 is None:
        return {
            "status": "blocked",
            "missing_reason": "prospective_team_rating_seed_unavailable",
            "prediction": None,
        }
    frozen = candidate or load_frozen_prospective_rosh_candidate()
    verify_prospective_rosh_candidate(frozen)
    if frozen.artifact_hash != FROZEN_CANDIDATE_HASH:
        raise ValueError("frozen prospective R.O.S.H. candidate identity drift")
    ProspectiveRoshShadowRepository(repository.connection).store_candidate(
        frozen,
        created_at=confirmed,
    )
    slots = mapping_payload["slots"]
    assert isinstance(slots, list)
    radiant_slots = sorted((row for row in slots if row["side"] == "radiant"), key=lambda row: row["position"])
    dire_slots = sorted((row for row in slots if row["side"] == "dire"), key=lambda row: row["position"])
    radiant_heroes = tuple(int(row["hero_id"]) for row in radiant_slots)
    dire_heroes = tuple(int(row["hero_id"]) for row in dire_slots)
    evidence: ProspectiveRoshEvidence | None = None
    missing_reason: str | None = None
    try:
        effective_transport = transport or StratzRoshClient()
        batch = effective_transport.fetch_legacy_lineup_batch(
            radiant_heroes,
            dire_heroes,
            statistics_cutoff=locked_at,
        )
        store = ExactByteArtifactStore(artifact_root)
        requests = archive_exact_artifacts(store, batch.request_bodies)
        responses = archive_exact_artifacts(store, batch.response_bodies)
        evidence = build_prospective_rosh_evidence(
            frozen,
            artifact_root=store.root,
            radiant_heroes=radiant_heroes,
            dire_heroes=dire_heroes,
            request_artifacts=requests,
            response_artifacts=responses,
            statistics_cutoff=locked_at,
            available_at=_utc(batch.collected_at, "R.O.S.H. available_at"),
        )
    except (OSError, StratzRoshError, ValueError):
        missing_reason = "prospective_rosh_evidence_unavailable"
    p1: float | None = None
    standardized: float | None = None
    contribution: float | None = None
    pure_score: float | None = None
    if evidence is not None:
        p1, standardized, contribution = candidate_probability(
            frozen,
            team_probability=p0.probability,
            pure_rosh_score=evidence.pure_rosh_score,
        )
        pure_score = evidence.pure_rosh_score
    persisted_clock = repository.latest_observed_game_clock(
        raybet_match_id,
        map_number,
        confirmed,
    )
    effective_clock = persisted_clock if persisted_clock is not None else game_clock_seconds
    if effective_clock is not None and effective_clock < 0:
        raise ValueError("game_clock_seconds must be non-negative")
    marker = None if draft_state_marker is None else str(draft_state_marker).strip()
    causal_status, causal_reason = _causal_status(
        live_state_input_used=bool(live_state_input_used),
        game_clock_seconds=effective_clock,
        draft_state_marker=marker,
    )
    radiant_team_id = int(radiant_slots[0]["team_id"])
    dire_team_id = int(dire_slots[0]["team_id"])
    prediction = LiveDraftProspectivePrediction(
        prediction_hash="",
        raybet_match_id=_required_text(raybet_match_id, "raybet_match_id"),
        map_number=int(map_number),
        mapping_version=int(mapping_version),
        mapping_hash=canonical_mapping_hash(mapping),
        operator_locked_at=locked_at,
        operator_identity=_required_text(operator_identity, "operator_identity"),
        confirmation_text=confirmation_text,
        confirmed_at=confirmed,
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        p0=p0,
        candidate_hash=frozen.artifact_hash,
        record_status="paired" if evidence is not None else "p0_only",
        p1_probability=p1,
        pure_rosh_score=pure_score,
        standardized_rosh_score=standardized,
        rosh_logit_contribution=contribution,
        rosh_evidence=evidence,
        missing_reason=missing_reason,
        game_clock_seconds=effective_clock,
        vision_frame_timestamp=(
            None if vision_frame_timestamp is None else _utc(vision_frame_timestamp, "vision_frame_timestamp")
        ),
        draft_state_marker=marker,
        live_state_input_used=bool(live_state_input_used),
        causal_status=causal_status,
        causal_reason=causal_reason,
        created_at=max(datetime.now(UTC), confirmed),
    )
    prediction = LiveDraftProspectivePrediction(
        **{**prediction.__dict__, "prediction_hash": _hash(prediction.to_payload(include_hash=False))}
    )
    inserted = repository.persist_prediction(prediction)
    return {
        "status": "created" if inserted else "unchanged",
        "prediction": prediction.to_payload(),
    }


__all__ = [
    "BRIDGE_VERSION",
    "FROZEN_CANDIDATE_HASH",
    "LOCK_CONFIRMATION",
    "LiveDraftProspectiveBridgeRepository",
    "LiveDraftProspectivePrediction",
    "LiveTeamRatingP0",
    "canonical_mapping_hash",
    "generate_live_draft_prediction",
]
