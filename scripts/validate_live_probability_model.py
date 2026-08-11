"""Reproduce the frozen live gold-lead model's chronological holdout audit."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Sequence

from live_betting.live_probability import (
    COEFFICIENT_BY_MINUTE,
    MODEL_VERSION,
    TRAINING_COHORT,
    VALIDATION_BY_MINUTE,
)
from live_betting.storage import LiveBettingStore
from shared.environment import load_environment_file


ROOT = Path(__file__).resolve().parents[1]
COHORT_END_EPOCH = 1_786_393_122


def _probability(coefficient: float, radiant_lead: int) -> float:
    value = coefficient * min(50.0, max(-50.0, radiant_lead / 1000.0))
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def _metrics(
    rows: list[tuple[int, int]],
    *,
    coefficient: float,
) -> tuple[float, float, float]:
    probabilities = [_probability(coefficient, lead) for lead, _ in rows]
    labels = [winner for _, winner in rows]
    brier = sum(
        (probability - winner) ** 2
        for probability, winner in zip(probabilities, labels, strict=True)
    ) / len(labels)
    log_loss = -sum(
        winner * math.log(probability)
        + (1 - winner) * math.log(1.0 - probability)
        for probability, winner in zip(probabilities, labels, strict=True)
    ) / len(labels)
    bins: list[list[tuple[float, int]]] = [[] for _ in range(10)]
    for probability, winner in zip(probabilities, labels, strict=True):
        bins[min(9, int(probability * 10))].append((probability, winner))
    calibration_error = sum(
        len(bucket)
        / len(labels)
        * abs(
            sum(probability for probability, _ in bucket) / len(bucket)
            - sum(winner for _, winner in bucket) / len(bucket)
        )
        for bucket in bins
        if bucket
    )
    return brier, log_loss, calibration_error


def validate(database_url: str | None = None) -> dict[str, object]:
    minutes: list[dict[str, object]] = []
    with LiveBettingStore(database_url) as store:
        connection = store.connection
        for minute, coefficient in COEFFICIENT_BY_MINUTE.items():
            raw = connection.execute(
                """SELECT gold.value,
                          CASE WHEN match.radiant_win THEN 1 ELSE 0 END
                     FROM gold_advantage AS gold
                     JOIN matches AS match ON match.match_id=gold.match_id
                    WHERE gold.time_min=? AND match.radiant_win IS NOT NULL
                      AND match.start_time<=?
                    ORDER BY match.start_time, match.match_id""",
                (minute, COHORT_END_EPOCH),
            ).fetchall()
            split = int(len(raw) * 0.8)
            train = [(int(row[0]), int(row[1])) for row in raw[:split]]
            holdout = [(int(row[0]), int(row[1])) for row in raw[split:]]
            base_rate = sum(winner for _, winner in train) / len(train)
            brier, log_loss, calibration_error = _metrics(
                holdout,
                coefficient=coefficient,
            )
            baseline_brier = sum(
                (base_rate - winner) ** 2 for _, winner in holdout
            ) / len(holdout)
            baseline_log_loss = -sum(
                winner * math.log(base_rate)
                + (1 - winner) * math.log(1.0 - base_rate)
                for _, winner in holdout
            ) / len(holdout)
            frozen = VALIDATION_BY_MINUTE[minute]
            reproduced = (
                len(train) == frozen[0]
                and len(holdout) == frozen[1]
                and abs(brier - frozen[2]) <= 1e-5
                and abs(baseline_brier - frozen[3]) <= 1e-5
                and abs(log_loss - frozen[4]) <= 1e-5
                and abs(baseline_log_loss - frozen[5]) <= 1e-5
                and abs(calibration_error - frozen[6]) <= 1e-5
            )
            passed = brier < baseline_brier and log_loss < baseline_log_loss
            minutes.append(
                {
                    "minute": minute,
                    "coefficient_per_1000_gold": coefficient,
                    "train_samples": len(train),
                    "holdout_samples": len(holdout),
                    "holdout_brier": round(brier, 6),
                    "baseline_brier": round(baseline_brier, 6),
                    "holdout_log_loss": round(log_loss, 6),
                    "baseline_log_loss": round(baseline_log_loss, 6),
                    "holdout_ece": round(calibration_error, 6),
                    "passed": passed,
                    "frozen_evidence_reproduced": reproduced,
                }
            )
    return {
        "model_version": MODEL_VERSION,
        "training_cohort": TRAINING_COHORT,
        "status": (
            "passed"
            if all(
                row["passed"] and row["frozen_evidence_reproduced"]
                for row in minutes
            )
            else "failed"
        ),
        "minutes": minutes,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_environment_file(ROOT / ".env")
    report = validate(args.database_url)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
