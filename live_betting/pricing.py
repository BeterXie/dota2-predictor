"""Market probability helpers used by calibrated pricing models."""

from __future__ import annotations

import math
from collections.abc import Iterable


def devig(prices: Iterable[float]) -> list[float]:
    implied = [1.0 / price for price in prices]
    total = sum(implied)
    if total <= 0:
        raise ValueError("prices must contain positive decimal odds")
    return [value / total for value in implied]


def poisson_cdf(k: int, mean: float) -> float:
    if mean < 0:
        raise ValueError("mean cannot be negative")
    term = math.exp(-mean)
    total = term
    for value in range(1, k + 1):
        term *= mean / value
        total += term
    return min(1.0, total)


def total_over_probability(line: float, expected_total: float) -> float:
    """Return P(total > line) for half-point totals under a Poisson baseline."""
    if line % 1 != 0.5:
        raise ValueError("baseline total model supports half-point lines only")
    return 1.0 - poisson_cdf(math.floor(line), expected_total)


def market_key(market_type: str, period: str, side: str | None, line: float | None) -> str:
    line_text = "" if line is None else f"{line:g}"
    return f"{market_type}|{period}|{side or ''}|{line_text}"
