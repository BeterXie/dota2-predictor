"""Official R.O.S.H. direction evidence for shadow strategy gates."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from live_betting.rosh_parity_storage import StoredRoshRun
from prematch.stratz_official_profile import canonical_bytes, get_profile


EVIDENCE_SCHEMA = "rosh-direction-evidence/v1"


class RoshEvidenceError(ValueError):
    """Stable fail-closed reason for unusable official R.O.S.H. evidence."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RoshDirectionEvidence:
    schema: str
    analysis_run_id: str
    draft_hash: str
    rosh_profile_id: str
    game_clock_seconds: int
    selected_minute: int
    radiant_score: float
    underdog_side: str
    underdog_direction_score: float
    direction: str
    completeness: str
    evidence_hash: str

    def as_input_ref(self) -> dict[str, Any]:
        return asdict(self)


def resolve_underdog_draft_side(
    underdog_side: str,
    *,
    radiant_team_side: str | None = None,
) -> str:
    """Resolve market team-one/team-two identity to RADIANT or DIRE."""

    normalized = str(underdog_side).upper()
    if normalized in {"RADIANT", "DIRE"}:
        return normalized
    market_side = str(underdog_side).lower()
    radiant_market_side = None if radiant_team_side is None else radiant_team_side.lower()
    if market_side not in {"team_one", "team_two"}:
        raise RoshEvidenceError("team_side_not_confirmed")
    if radiant_market_side not in {"team_one", "team_two"}:
        raise RoshEvidenceError("team_side_not_confirmed")
    return "RADIANT" if market_side == radiant_market_side else "DIRE"


def _profile_matches(run: StoredRoshRun) -> bool:
    profile = get_profile()
    stored = run.run
    return (
        stored.rosh_profile_id == profile.rosh_profile_id
        and stored.formula_version == profile.formula_version
        and stored.request_profile_hash == profile.request_profile_hash
        and stored.upstream_bundle_hash == profile.upstream_bundle_hash
        and stored.scorer_source_hash == profile.scorer_source_hash
        and stored.canonical_profile_hash == profile.canonical_profile_hash
        and stored.serialization_version == profile.serialization_version
    )


def _hash_projection(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def official_rosh_draft_hash(
    radiant_hero_ids: Sequence[int],
    dire_hero_ids: Sequence[int],
) -> str:
    """Build the position-aware draft identity used by official R.O.S.H. runs."""

    radiant = tuple(radiant_hero_ids)
    dire = tuple(dire_hero_ids)
    heroes = (*radiant, *dire)
    if (
        len(radiant) != 5
        or len(dire) != 5
        or any(type(hero_id) is not int or hero_id <= 0 for hero_id in heroes)
        or len(set(heroes)) != 10
    ):
        raise RoshEvidenceError("rosh_lineup_draft_mismatch")
    return _hash_projection(
        {
            "radiant": [
                {"hero_id": hero_id, "position_id": position_id}
                for position_id, hero_id in enumerate(radiant, 1)
            ],
            "dire": [
                {"hero_id": hero_id, "position_id": position_id}
                for position_id, hero_id in enumerate(dire, 1)
            ],
        }
    )


def build_rosh_direction_evidence(
    run: StoredRoshRun | None,
    *,
    observation_draft_hash: str,
    game_clock_seconds: int,
    underdog_side: str,
    radiant_team_side: str | None = None,
) -> RoshDirectionEvidence:
    """Select only the latest minute bucket reached by the observed game clock."""

    if run is None or run.run.status != "succeeded":
        raise RoshEvidenceError("rosh_analysis_unavailable")
    try:
        profile_matches = _profile_matches(run)
    except Exception:
        profile_matches = False
    if not profile_matches:
        raise RoshEvidenceError("rosh_profile_mismatch")
    if run.run.draft_hash != observation_draft_hash:
        raise RoshEvidenceError("rosh_lineup_draft_mismatch")
    if (
        isinstance(game_clock_seconds, bool)
        or not isinstance(game_clock_seconds, int)
        or game_clock_seconds < 0
    ):
        raise RoshEvidenceError("game_clock_unavailable")
    draft_side = resolve_underdog_draft_side(
        underdog_side,
        radiant_team_side=radiant_team_side,
    )
    available = [
        point
        for point in run.minute_points
        if point.minute * 60 <= game_clock_seconds
    ]
    if not available:
        raise RoshEvidenceError("rosh_minute_score_unavailable")
    selected = max(available, key=lambda point: point.minute)
    radiant_score = float(selected.display_score)
    if not math.isfinite(radiant_score):
        raise RoshEvidenceError("rosh_minute_score_unavailable")
    underdog_score = radiant_score if draft_side == "RADIANT" else -radiant_score
    direction = (
        "supports_underdog"
        if underdog_score > 0.0
        else "opposes_underdog"
        if underdog_score < 0.0
        else "neutral"
    )
    projection = {
        "schema": EVIDENCE_SCHEMA,
        "analysis_run_id": run.run.run_id,
        "draft_hash": run.run.draft_hash,
        "rosh_profile_id": run.run.rosh_profile_id,
        "game_clock_seconds": game_clock_seconds,
        "selected_minute": selected.minute,
        "radiant_score": radiant_score,
        "underdog_side": draft_side,
        "underdog_direction_score": underdog_score,
        "direction": direction,
        "completeness": "complete",
    }
    return RoshDirectionEvidence(
        **projection,
        evidence_hash=_hash_projection(projection),
    )


def validate_rosh_direction_evidence(
    evidence: RoshDirectionEvidence,
    run: StoredRoshRun | None,
    *,
    observation_draft_hash: str,
    game_clock_seconds: int,
    underdog_side: str,
    radiant_team_side: str | None = None,
) -> bool:
    try:
        expected = build_rosh_direction_evidence(
            run,
            observation_draft_hash=observation_draft_hash,
            game_clock_seconds=game_clock_seconds,
            underdog_side=underdog_side,
            radiant_team_side=radiant_team_side,
        )
    except RoshEvidenceError:
        return False
    return evidence == expected


__all__ = [
    "EVIDENCE_SCHEMA",
    "RoshDirectionEvidence",
    "RoshEvidenceError",
    "build_rosh_direction_evidence",
    "official_rosh_draft_hash",
    "resolve_underdog_draft_side",
    "validate_rosh_direction_evidence",
]
