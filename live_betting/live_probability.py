"""Validated, interpretable live win-probability update for shadow decisions.

The model treats the locked R.O.S.H. draft probability as the prior and applies
one chronological-holdout-validated log-odds adjustment for the current
Radiant net-worth lead.  It deliberately does not infer missing Vision fields
or reuse a coefficient beyond its validated checkpoint minute.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


MODEL_VERSION = "vision-gold-lead-logit-v1"
TRAINING_COHORT = {
    "source": "postgresql:matches+gold_advantage",
    "match_count": 3980,
    "observed_start_utc": "2025-01-24T09:00:52+00:00",
    "observed_end_utc": "2026-08-10T20:18:42+00:00",
    "split": "chronological_80_20",
    "feature": "radiant_networth_lead_per_1000_gold",
    "fit_intercept": False,
    "l2_regularization": 1.0,
}

# Log-odds change per 1,000 gold of Radiant advantage.  Each value was fitted
# independently for its checkpoint minute using only the chronological first
# 80% of the cohort above.
COEFFICIENT_BY_MINUTE = {
    5: 0.576768492,
    10: 0.429245964,
    15: 0.318585020,
    20: 0.286861765,
    25: 0.254491510,
    30: 0.222826643,
    35: 0.197695467,
    40: 0.165683500,
    45: 0.131758138,
    50: 0.116216140,
    55: 0.094128891,
    60: 0.077583691,
}

# Frozen chronological holdout evidence.  Brier score and log loss must both
# beat the per-minute training-base-rate baseline before a minute is admitted
# to COEFFICIENT_BY_MINUTE.
VALIDATION_BY_MINUTE = {
    5: (3184, 796, 0.234769, 0.249759, 0.661511, 0.692665, 0.029124),
    10: (3184, 796, 0.213620, 0.249759, 0.613749, 0.692665, 0.039549),
    15: (3177, 795, 0.196826, 0.249778, 0.572347, 0.692703, 0.042616),
    20: (3160, 791, 0.167593, 0.249786, 0.496446, 0.692719, 0.029679),
    25: (3047, 762, 0.143225, 0.249903, 0.432054, 0.692954, 0.031422),
    30: (2728, 683, 0.124498, 0.250154, 0.381094, 0.693455, 0.023802),
    35: (2145, 537, 0.126167, 0.250338, 0.391662, 0.693824, 0.023489),
    40: (1516, 379, 0.130226, 0.248853, 0.401364, 0.690853, 0.035406),
    45: (928, 233, 0.149328, 0.248370, 0.452639, 0.689886, 0.062858),
    50: (563, 141, 0.181760, 0.248760, 0.539929, 0.690667, 0.108829),
    55: (339, 85, 0.184697, 0.249135, 0.543317, 0.691416, 0.168243),
    60: (212, 54, 0.176892, 0.257090, 0.532553, 0.707344, 0.104545),
}


@dataclass(frozen=True)
class LiveProbabilityEstimate:
    checkpoint_minute: int
    prior_radiant_probability: float
    radiant_networth_lead: int
    coefficient_per_1000_gold: float
    probability_radiant: float

    def context(self) -> dict[str, object]:
        validation = VALIDATION_BY_MINUTE[self.checkpoint_minute]
        return {
            "available": True,
            "reason": None,
            "model_version": MODEL_VERSION,
            "checkpoint_minute": self.checkpoint_minute,
            "prior_radiant_probability": self.prior_radiant_probability,
            "radiant_networth_lead": self.radiant_networth_lead,
            "coefficient_per_1000_gold": self.coefficient_per_1000_gold,
            "probability_radiant": self.probability_radiant,
            "training_cohort": TRAINING_COHORT,
            "validation": {
                "train_samples": validation[0],
                "holdout_samples": validation[1],
                "holdout_brier": validation[2],
                "baseline_brier": validation[3],
                "holdout_log_loss": validation[4],
                "baseline_log_loss": validation[5],
                "holdout_ece": validation[6],
            },
        }


def estimate_radiant_win_probability(
    *,
    prior_radiant_probability: float,
    radiant_networth_lead: int,
    checkpoint_minute: int,
) -> LiveProbabilityEstimate:
    if type(checkpoint_minute) is not int or checkpoint_minute not in COEFFICIENT_BY_MINUTE:
        raise ValueError("live_probability_checkpoint_minute_not_validated")
    if type(radiant_networth_lead) is not int:
        raise ValueError("live_probability_networth_lead_invalid")
    prior = float(prior_radiant_probability)
    if not math.isfinite(prior) or not 0.0 <= prior <= 1.0:
        raise ValueError("live_probability_prior_invalid")

    bounded_prior = min(1.0 - 1e-6, max(1e-6, prior))
    bounded_lead_per_1000 = min(
        50.0,
        max(-50.0, radiant_networth_lead / 1000.0),
    )
    coefficient = COEFFICIENT_BY_MINUTE[checkpoint_minute]
    prior_log_odds = math.log(bounded_prior / (1.0 - bounded_prior))
    live_log_odds = prior_log_odds + coefficient * bounded_lead_per_1000
    if live_log_odds >= 0.0:
        probability = 1.0 / (1.0 + math.exp(-live_log_odds))
    else:
        exponent = math.exp(live_log_odds)
        probability = exponent / (1.0 + exponent)
    return LiveProbabilityEstimate(
        checkpoint_minute=checkpoint_minute,
        prior_radiant_probability=prior,
        radiant_networth_lead=radiant_networth_lead,
        coefficient_per_1000_gold=coefficient,
        probability_radiant=probability,
    )


__all__ = [
    "COEFFICIENT_BY_MINUTE",
    "MODEL_VERSION",
    "TRAINING_COHORT",
    "VALIDATION_BY_MINUTE",
    "LiveProbabilityEstimate",
    "estimate_radiant_win_probability",
]
