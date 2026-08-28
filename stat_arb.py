"""Cointegration pairs trading: the statistics, separated from the backtest.

Why this family is worth testing when the others were not
---------------------------------------------------------
A directional trade on one asset needs edge = IC * sigma_h, and four
measurements put that IC near zero. A pairs trade is different in kind: it
does not forecast where the market goes. It bets that a SPREAD which has
historically reverted keeps reverting.

The economics are legible in advance. If the spread's standard deviation is
sigma_s and you enter at z_entry and exit at z_exit, the gross move captured
is (z_entry - z_exit) * sigma_s, and it must beat the cost of trading BOTH
legs, in and out.

The correction that kills most naive pair backtests: a hedge ratio of beta
means you trade 1 unit of one leg and beta units of the other, so the cost
scales with (1 + |beta|) while the profit does not. A pair with beta = 2 pays
three times the fee of a beta = 0 pair for the same spread move. Any
screening table that ignores this ranks the worst pairs highest.

    net_bps = (z_entry - z_exit) * sigma_s * 1e4 - (1 + |beta|) * cost_bps

Cointegration is a statistical claim that has to be tested, not assumed. Two
independent random walks produce a spread that looks mean-reverting to the
eye and is not, which is why this module implements an Augmented
Dickey-Fuller test and why the backtest measures its own false-positive rate
on synthetic independent walks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

# Engle-Granger residual-based critical values (two variables, constant, no
# trend). These are MORE negative than plain ADF values because the residual
# has already been fitted - using plain ADF thresholds on a fitted residual
# is a standard way to declare cointegration that is not there.
EG_CRITICAL = {0.01: -3.90, 0.05: -3.34, 0.10: -3.04}


@dataclass
class PairStats:
    beta: float
    intercept: float
    spread_mean: float
    spread_sd: float
    adf: float
    half_life: float
    cointegrated: bool
    reason: str = ""


def ols(y: np.ndarray, x: np.ndarray) -> Tuple[float, float]:
    """Slope and intercept of y on x. No library dependency, so the exact
    arithmetic is visible."""
    n = len(x)
    sx, sy = x.sum(), y.sum()
    sxx = (x * x).sum()
    sxy = (x * y).sum()
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-18:
        return 0.0, float(y.mean())
    beta = (n * sxy - sx * sy) / denom
    return float(beta), float((sy - beta * sx) / n)


def adf_stat(series: np.ndarray, lags: int = 1) -> float:
    """Augmented Dickey-Fuller t-statistic on the lagged-level coefficient.

    Regress d[t] = a + g*s[t-1] + sum_i c_i*d[t-i] + e[t] and return
    g / se(g). More negative means stronger evidence against a unit root,
    i.e. stronger evidence the series reverts.
    """
    s = np.asarray(series, dtype=float)
    n = len(s)
    if n < lags + 20:
        return 0.0
    d = np.diff(s)
    rows = n - 1 - lags
    if rows < 10:
        return 0.0
    Y = d[lags:]
    cols = [np.ones(rows), s[lags:n - 1]]
    for i in range(1, lags + 1):
        cols.append(d[lags - i:n - 1 - i])
    X = np.column_stack(cols)
    try:
        coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
        resid = Y - X @ coef
        dof = rows - X.shape[1]
        if dof <= 0:
            return 0.0
        s2 = float(resid @ resid) / dof
        xtx_inv = np.linalg.pinv(X.T @ X)
        se = math.sqrt(max(s2 * xtx_inv[1, 1], 1e-300))
        return float(coef[1] / se) if se > 0 else 0.0
    except np.linalg.LinAlgError:
        return 0.0


def half_life(spread: np.ndarray) -> float:
    """Ornstein-Uhlenbeck half-life in bars.

    Fit ds[t] = a + b*s[t-1]. With b < 0 the series pulls back toward its
    mean at rate -b, and half of any deviation decays in ln(2)/(-b) bars.
    This sets the holding period, and therefore how often the cost is paid.
    """
    s = np.asarray(spread, dtype=float)
    if len(s) < 20:
        return float("inf")
    b, _ = ols(np.diff(s), s[:-1])
    if b >= 0:
        return float("inf")            # diverging, not reverting
    return float(math.log(2.0) / -b)


def fit_pair(log_y: np.ndarray, log_x: np.ndarray, *, alpha: float = 0.05,
             max_half_life: float = 240.0,
             min_half_life: float = 1.0) -> PairStats:
    """Estimate the hedge ratio and decide whether the pair is tradeable.

    Every number here comes from the TRAINING window only. Refitting beta on
    data that includes the test period is the pairs-trading equivalent of
    looking at the answers.
    """
    beta, intercept = ols(log_y, log_x)
    spread = log_y - beta * log_x - intercept
    stat = adf_stat(spread)
    hl = half_life(spread)
    crit = EG_CRITICAL.get(alpha, -3.34)

    ok, why = True, ""
    if stat > crit:
        ok, why = False, f"ADF {stat:.2f} above {crit:.2f} - no cointegration"
    elif not math.isfinite(hl):
        ok, why = False, "spread diverges rather than reverts"
    elif hl > max_half_life:
        ok, why = False, f"half-life {hl:.0f} bars too slow to trade"
    elif hl < min_half_life:
        ok, why = False, f"half-life {hl:.2f} bars - noise, not reversion"
    return PairStats(beta=beta, intercept=intercept,
                     spread_mean=float(spread.mean()),
                     spread_sd=float(spread.std()),
                     adf=stat, half_life=hl, cointegrated=ok, reason=why)


def expected_net_bps(st: PairStats, z_entry: float, z_exit: float,
                     cost_bps: float) -> float:
    """Gross capture minus the cost of BOTH legs, in bps of one leg's notional.

    This is the screen that should run before any backtest: if it is not
    comfortably positive, no amount of parameter tuning will save the pair.
    """
    gross = (z_entry - z_exit) * st.spread_sd * 1e4
    return gross - (1.0 + abs(st.beta)) * cost_bps


def zscore(spread_value: float, st: PairStats) -> float:
    if st.spread_sd <= 0:
        return 0.0
    return (spread_value - st.spread_mean) / st.spread_sd
