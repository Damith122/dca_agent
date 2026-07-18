#!/usr/bin/env python3
"""
================================================================================
 Indicator / helper calculation functions - moved out of dca2.py

 This file contains ONLY the standalone calculation functions relocated out
 of dca2.py: round_step, clamp, safe_div, ema_series, compute_atr, and
 compute_atr_pct. Every formula, name, and behavior below is unchanged from
 the original dca2.py source - nothing was fixed, renamed, or optimized.

 Left behind in dca2.py (not calculation functions, so out of scope for this
 move): now_str()/color()/YELLOW etc. (logging/formatting), enforce_safety_gates
 (safety gate logic), retry_with_backoff (async infra), and the Candle /
 CandleAggregator classes (data structures, not standalone calculations).

 Note on type hints: compute_atr / compute_atr_pct take `candles: List[Candle]`
 in their signatures, exactly as in the original. This file keeps the same
 `from __future__ import annotations` that dca2.py uses, so annotations are
 never evaluated at runtime - `Candle` does not need to be imported here
 (the functions only ever access attributes like .high/.low/.close, they
 never construct a Candle), which avoids a circular import with dca2.py.
================================================================================
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import List

import numpy as np

from config import ATR_PERIOD

# ============================================================================
# GENERIC MATH HELPERS
# ============================================================================


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    steps = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * d_step)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def ema_series(values: List[float], period: int) -> List[float]:
    """Simple EMA over a list, seeded with the first value's SMA-of-1
    (i.e. plain EMA formula, no lookahead)."""
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


# ============================================================================
# TECHNICAL INDICATORS (ATR / EMA stack / rolling vol) OVER THE CANDLE SERIES
# ============================================================================


def compute_atr(candles: List[Candle], period: int = ATR_PERIOD) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        c = candles[i]
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return float(np.mean(window)) if window else 0.0


def compute_atr_pct(candles: List[Candle], period: int = ATR_PERIOD) -> float:
    if not candles:
        return 0.0
    atr = compute_atr(candles, period)
    last_close = candles[-1].close or 1.0
    return atr / last_close
