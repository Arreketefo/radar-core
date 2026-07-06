"""Robust price statistics shared by every radar's deal/valuation engine."""

from __future__ import annotations

import statistics


def robust_baseline(values: list[float]) -> tuple[float, float]:
    """Median + MAD. MAD can legitimately be 0.0 (all values identical):
    consumers must NOT divide by it without checking — a z-score over a
    degenerate window explodes for trivial deviations."""
    median = statistics.median(values)
    mad = statistics.median([abs(x - median) for x in values])
    return median, mad


def drop_display(drop_pct: float) -> str:
    """Signed price-vs-baseline delta: drop 0.30 -> '-30%', -0.17 -> '+17%'.

    A deal can fire ABOVE baseline (absolute-threshold rules); a hardcoded '-'
    would render that as a bogus double-negative discount. Rounding to int
    first avoids the IEEE negative zero ('-0%') float formatting produces.
    """
    return f"{round(-drop_pct * 100):+d}%"
