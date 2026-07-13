"""Conservative RayBet-to-live-provider fixture matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from .models import ProviderMatch


@dataclass(frozen=True)
class LinkCandidate:
    provider_match_id: str
    confidence: float
    reasons: tuple[str, ...]


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _name_score(left: str, right: str) -> float:
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return 0.0


def score_candidate(
    *,
    ray_team_one: str,
    ray_team_two: str,
    ray_tournament: str,
    ray_scheduled_at: datetime | None,
    ray_best_of: int | None,
    candidate: ProviderMatch,
) -> LinkCandidate:
    direct = (_name_score(ray_team_one, candidate.team_one) +
              _name_score(ray_team_two, candidate.team_two)) / 2
    swapped = (_name_score(ray_team_one, candidate.team_two) +
               _name_score(ray_team_two, candidate.team_one)) / 2
    team_score = max(direct, swapped)
    reasons = [f"teams={team_score:.2f}"]
    score = team_score * 0.7

    tournament_score = _name_score(ray_tournament, candidate.tournament)
    score += tournament_score * 0.1
    reasons.append(f"tournament={tournament_score:.2f}")

    if ray_scheduled_at and candidate.scheduled_at:
        minutes = abs((ray_scheduled_at - candidate.scheduled_at).total_seconds()) / 60
        time_score = 1.0 if minutes <= 15 else 0.5 if minutes <= 90 else 0.0
        score += time_score * 0.15
        reasons.append(f"time_delta_min={minutes:.1f}")
    if ray_best_of and candidate.best_of:
        best_of_score = 1.0 if ray_best_of == candidate.best_of else 0.0
        score += best_of_score * 0.05
        reasons.append(f"best_of={best_of_score:.2f}")
    return LinkCandidate(candidate.provider_match_id, round(score, 4), tuple(reasons))


def choose_unique(candidates: list[LinkCandidate], threshold: float = 0.85) -> LinkCandidate | None:
    ranked = sorted(candidates, key=lambda item: item.confidence, reverse=True)
    if not ranked or ranked[0].confidence < threshold:
        return None
    if len(ranked) > 1 and ranked[0].confidence - ranked[1].confidence < 0.08:
        return None
    return ranked[0]
