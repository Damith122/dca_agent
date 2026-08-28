"""Cross-sectional portfolio construction: relative ranking, not prediction.

The structural change
---------------------
Six strategies asked "will this asset go up?" That question needs an
information coefficient large enough to beat the fee on a single bet, and
four measurements put it near zero.

This asks a different question: "will SOL beat NEAR?" Two things change, and
both are mechanical rather than hopeful.

FIRST, BREADTH. Grinold's Fundamental Law says IR = IC * sqrt(breadth). With
four correlated majors you place roughly 1.2 independent bets a period; with
a hundred names you place perhaps 28. The SAME forecast quality that gives
IR 0.35 on four assets gives IR 1.68 on a hundred. Nothing about the signal
improves - only the number of times you get to use it.

SECOND, THE MARKET FACTOR CANCELS. Crypto majors correlate ~0.76 because one
common factor drives them. A directional bet is mostly a bet on that factor,
which is why single-asset volatility is 77% annualised and swamps any edge.
A dollar-neutral long/short book holds the factor at roughly zero, so the
portfolio's volatility is the RESIDUAL volatility - several times smaller.
The same expected return over a much smaller denominator is a much larger
Sharpe.

What this does NOT fix
----------------------
Turnover. A cross-sectional book rebalances every period and pays the fee on
every weight change, so cost scales with turnover rather than with trade
count:

    net_return = IC * sigma_residual * sqrt(breadth) - turnover * cost

Rebalancing daily across 100 names at 100% turnover and 7.32 bps costs 184%
a year. That is why the engine reports turnover as prominently as return,
why weights are damped toward the previous book, and why a no-trade band
exists. Breadth without turnover control just loses money faster in more
places.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np


@dataclass
class BookParams:
    quantile: float = 0.2        # long the top 20%, short the bottom 20%
    gross_exposure: float = 1.0  # sum of |weights|
    max_weight: float = 0.10     # per-name cap, so one name cannot dominate
    damping: float = 0.5         # 0 = jump to target, 1 = never trade
    no_trade_band: float = 0.005  # ignore weight changes smaller than this
    min_names: int = 10          # below this, breadth is not worth the costs


def rank_normalise(signal: np.ndarray) -> np.ndarray:
    """Cross-sectional ranks mapped to [-1, +1], NaNs excluded.

    Ranks rather than raw values: a single asset with an extreme reading
    would otherwise take most of the book, and the whole point is to spread
    the same modest edge over many names.
    """
    out = np.full(len(signal), np.nan)
    ok = np.isfinite(signal)
    n = int(ok.sum())
    if n < 2:
        return out
    order = np.argsort(np.argsort(signal[ok]))
    out[ok] = 2.0 * order / (n - 1) - 1.0
    return out


def target_weights(signal: np.ndarray, p: BookParams) -> np.ndarray:
    """Dollar-neutral long/short weights from a cross-sectional signal.

    Long the top quantile, short the bottom, nothing in the middle. Both
    sides are scaled to the same gross so the book carries no net market
    exposure - that is what removes the common factor.
    """
    w = np.zeros(len(signal))
    r = rank_normalise(signal)
    ok = np.isfinite(r)
    n = int(ok.sum())
    if n < p.min_names:
        return w
    k = max(1, int(round(n * p.quantile)))
    idx = np.where(ok)[0]
    order = idx[np.argsort(r[idx])]
    shorts, longs = order[:k], order[-k:]
    if len(longs) == 0 or len(shorts) == 0:
        return w
    half = p.gross_exposure / 2.0
    w[longs] = half / len(longs)
    w[shorts] = -half / len(shorts)
    # Cap, then rescale each side so the book stays dollar neutral.
    w = np.clip(w, -p.max_weight, p.max_weight)
    lp, sp = w[w > 0].sum(), -w[w < 0].sum()
    if lp > 0:
        w[w > 0] *= half / lp
    if sp > 0:
        w[w < 0] *= half / sp
    return w


def apply_damping(target: np.ndarray, previous: Optional[np.ndarray],
                  p: BookParams) -> np.ndarray:
    """Move part-way to the target and ignore trivial changes.

    Turnover is the cost driver, and the marginal name that just crossed a
    quantile boundary is the least informative one in the book. Damping and
    a no-trade band drop exactly those trades.
    """
    if previous is None:
        return target
    moved = previous + (1.0 - p.damping) * (target - previous)
    small = np.abs(moved - previous) < p.no_trade_band
    moved[small] = previous[small]
    return moved


def turnover(new: np.ndarray, old: Optional[np.ndarray]) -> float:
    if old is None:
        return float(np.abs(new).sum())
    return float(np.abs(new - old).sum())


def spearman_ic(signal: np.ndarray, fwd: np.ndarray) -> float:
    """Rank correlation between the signal and the realised forward return.

    This is THE diagnostic. Portfolio returns are noisy and can flatter a
    dead signal for months; IC measures the forecast itself, period by
    period, and its mean and t-statistic say whether anything is there.
    """
    ok = np.isfinite(signal) & np.isfinite(fwd)
    if ok.sum() < 5:
        return np.nan
    a = np.argsort(np.argsort(signal[ok])).astype(float)
    b = np.argsort(np.argsort(fwd[ok])).astype(float)
    a -= a.mean()
    b -= b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / denom) if denom > 0 else np.nan


def effective_breadth(returns: np.ndarray, max_missing: float = 0.2
                      ) -> float:
    """How many INDEPENDENT directions of variation a universe really has.

    Participation ratio of the correlation eigenvalues,

        N_eff = (sum lambda_i)^2 / sum lambda_i^2

    which equals N for independent names and collapses toward 1 as a single
    factor takes over.

    NaN handling is the whole difficulty and it has already gone wrong once
    in production. np.corrcoef propagates a single NaN across the ENTIRE
    matrix, so one late-listed symbol with one missing bar turned every
    entry to NaN, the finite-column filter kept nothing, and the guard
    returned 1.0 - which read as "this universe is one asset" when it
    actually meant "this function could not run". A 300-name universe of
    perpetuals has missing bars in almost every column, so it fired every
    time.

    The fix is to build a complete block before correlating: drop columns
    missing more than `max_missing` of their history, then drop any rows
    still holding a NaN, and only then correlate. If that leaves too little
    to measure, return NaN - an explicit "unknown" the caller must handle,
    never a number that looks like an answer.
    """
    r = np.asarray(returns, dtype=float)
    if r.ndim != 2 or r.shape[1] < 2:
        return float(r.shape[1]) if r.ndim == 2 and r.shape[1] >= 1 else np.nan

    keep_col = np.isnan(r).mean(axis=0) <= max_missing
    if keep_col.sum() < 2:
        return np.nan
    r = r[:, keep_col]

    keep_row = ~np.isnan(r).any(axis=1)
    if keep_row.sum() < max(30, r.shape[1] // 2):
        return np.nan
    r = r[keep_row]

    # A column with no variation makes corrcoef divide by zero.
    varying = r.std(axis=0) > 0
    if varying.sum() < 2:
        return np.nan
    r = r[:, varying]

    with np.errstate(invalid="ignore", divide="ignore"):
        c = np.corrcoef(r, rowvar=False)
    if not np.isfinite(c).all():
        return np.nan
    ev = np.clip(np.linalg.eigvalsh(c), 0.0, None)
    denom = float((ev * ev).sum())
    if denom <= 0:
        return np.nan
    return float(ev.sum() ** 2 / denom)


# --- signals -------------------------------------------------------------
# Each takes a (time x asset) price matrix and returns one value per asset
# at row `i`, using ONLY rows up to and including i.

def sig_momentum(px: np.ndarray, i: int, lookback: int = 168) -> np.ndarray:
    if i < lookback:
        return np.full(px.shape[1], np.nan)
    return px[i] / px[i - lookback] - 1.0


def sig_reversal(px: np.ndarray, i: int, lookback: int = 24) -> np.ndarray:
    if i < lookback:
        return np.full(px.shape[1], np.nan)
    return -(px[i] / px[i - lookback] - 1.0)


def sig_low_vol(px: np.ndarray, i: int, lookback: int = 168) -> np.ndarray:
    if i < lookback + 1:
        return np.full(px.shape[1], np.nan)
    r = np.diff(np.log(px[i - lookback:i + 1]), axis=0)
    return -np.nanstd(r, axis=0)


def sig_momentum_skip(px: np.ndarray, i: int, lookback: int = 168,
                      skip: int = 24) -> np.ndarray:
    """Momentum measured to `skip` bars ago, not to now.

    The most recent bars carry short-term reversal, which fights the
    momentum signal. Skipping them is standard in equity momentum and is
    the one variant here with a documented reason to exist.
    """
    if i < lookback + skip:
        return np.full(px.shape[1], np.nan)
    return px[i - skip] / px[i - lookback - skip] - 1.0


SIGNALS = {
    "momentum": sig_momentum,
    "reversal": sig_reversal,
    "low_vol": sig_low_vol,
    "mom_skip": sig_momentum_skip,
}
