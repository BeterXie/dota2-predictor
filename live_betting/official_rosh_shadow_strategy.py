"""Shadow-only v6 strategy gate for official R.O.S.H. direction evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from live_betting.rosh_evidence import (
    RoshDirectionEvidence,
    RoshEvidenceError,
    build_rosh_direction_evidence,
    validate_rosh_direction_evidence,
)
from live_betting.rosh_parity_storage import StoredRoshRun
from live_betting.strategy_contract import (
    OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION,
    build_official_rosh_strategy_contract,
    canonical_bytes,
)


CANDIDATE_SCHEMA = "official-rosh-shadow-candidate/v1"


@dataclass(frozen=True)
class OfficialRoshShadowEvaluation:
    candidate_hash: str
    status: str
    reason: str
    strategy_version: str
    rosh_direction_evidence: RoshDirectionEvidence | None
    calibration_artifact_ref: None
    calibrated_probability: None
    edge: None
    stake_multiplier: None
    paper_order: None
    strategy_contract: Mapping[str, Any]

    def as_record(self) -> dict[str, Any]:
        return {
            "schema": CANDIDATE_SCHEMA,
            "candidate_hash": self.candidate_hash,
            "status": self.status,
            "reason": self.reason,
            "strategy_version": self.strategy_version,
            "rosh_direction_evidence": (
                None
                if self.rosh_direction_evidence is None
                else self.rosh_direction_evidence.as_input_ref()
            ),
            "calibration_artifact_ref": None,
            "calibrated_probability": None,
            "edge": None,
            "stake_multiplier": None,
            "paper_order": None,
            "strategy_contract": dict(self.strategy_contract),
            "cohort": {
                "m3_c": "shadow_candidate_or_rejection",
                "m3_e": None,
            },
        }


class OfficialRoshDirectionShadowStrategy:
    """Evaluate direction gates without manufacturing probability or stake."""

    strategy_version = OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION

    def evaluate(
        self,
        *,
        run: StoredRoshRun | None,
        observation_draft_hash: str,
        game_clock_seconds: int,
        underdog_side: str,
        radiant_team_side: str | None = None,
        direction_evidence: RoshDirectionEvidence | None = None,
        calibration_artifact: Mapping[str, Any] | None = None,
    ) -> OfficialRoshShadowEvaluation:
        contract = build_official_rosh_strategy_contract()
        evidence: RoshDirectionEvidence | None = None
        try:
            evidence = build_rosh_direction_evidence(
                run,
                observation_draft_hash=observation_draft_hash,
                game_clock_seconds=game_clock_seconds,
                underdog_side=underdog_side,
                radiant_team_side=radiant_team_side,
            )
        except RoshEvidenceError as error:
            return self._result(
                status="rejected",
                reason=error.reason,
                evidence=None,
                contract=contract.as_input_ref(),
            )
        if direction_evidence is not None and not validate_rosh_direction_evidence(
            direction_evidence,
            run,
            observation_draft_hash=observation_draft_hash,
            game_clock_seconds=game_clock_seconds,
            underdog_side=underdog_side,
            radiant_team_side=radiant_team_side,
        ):
            return self._result(
                status="rejected",
                reason="rosh_evidence_hash_mismatch",
                evidence=evidence,
                contract=contract.as_input_ref(),
            )
        if evidence.direction == "neutral":
            return self._result(
                status="rejected",
                reason="rosh_direction_neutral",
                evidence=evidence,
                contract=contract.as_input_ref(),
            )
        if evidence.direction == "opposes_underdog":
            return self._result(
                status="rejected",
                reason="rosh_direction_opposes_underdog",
                evidence=evidence,
                contract=contract.as_input_ref(),
            )
        if calibration_artifact is not None:
            return self._result(
                status="rejected",
                reason="calibration_artifact_unregistered",
                evidence=evidence,
                contract=contract.as_input_ref(),
            )
        return self._result(
            status="shadow_candidate",
            reason="calibrated_probability_unavailable",
            evidence=evidence,
            contract=contract.as_input_ref(),
        )

    def _result(
        self,
        *,
        status: str,
        reason: str,
        evidence: RoshDirectionEvidence | None,
        contract: Mapping[str, Any],
    ) -> OfficialRoshShadowEvaluation:
        projection = {
            "schema": CANDIDATE_SCHEMA,
            "status": status,
            "reason": reason,
            "strategy_version": self.strategy_version,
            "rosh_direction_evidence": (
                None if evidence is None else evidence.as_input_ref()
            ),
            "calibration_artifact_ref": None,
            "calibrated_probability": None,
            "edge": None,
            "stake_multiplier": None,
            "paper_order": None,
            "strategy_contract": dict(contract),
            "cohort": {
                "m3_c": "shadow_candidate_or_rejection",
                "m3_e": None,
            },
        }
        candidate_hash = hashlib.sha256(canonical_bytes(projection)).hexdigest()
        return OfficialRoshShadowEvaluation(
            candidate_hash=candidate_hash,
            status=status,
            reason=reason,
            strategy_version=self.strategy_version,
            rosh_direction_evidence=evidence,
            calibration_artifact_ref=None,
            calibrated_probability=None,
            edge=None,
            stake_multiplier=None,
            paper_order=None,
            strategy_contract=dict(contract),
        )


__all__ = [
    "CANDIDATE_SCHEMA",
    "OfficialRoshDirectionShadowStrategy",
    "OfficialRoshShadowEvaluation",
]
