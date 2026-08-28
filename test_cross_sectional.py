#!/usr/bin/env python3
"""Tests for the cross-sectional long/short book.

The mechanism here is breadth, so the tests concentrate on the two ways
breadth lies: a measure that flatters a correlated universe, and a cost
model that forgets turnover is paid on every name at once.
"""
import sys

import numpy as np

import backtest_cross_sectional as B
import cross_sectional as CS

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


rng = np.random.default_rng(0)
P = CS.BookParams()


def universe(n, rho, T=3000, seed=0):
    r = np.random.default_rng(seed)
    f = r.normal(0, 1, (T, 1))
    return np.sqrt(rho) * f + np.sqrt(1 - rho) * r.normal(0, 1, (T, n))


print("[1] The book is market neutral by construction")
w = CS.target_weights(rng.normal(size=50), P)
check("weights sum to zero", abs(w.sum()) < 1e-12, f"{w.sum():+.2e}")
check("gross exposure is as configured", abs(np.abs(w).sum() - 1.0) < 1e-9)
check("both sides are populated", (w > 0).sum() > 0 and (w < 0).sum() > 0)
check("only the tails are held, not the middle",
      (w != 0).sum() <= 2 * round(50 * P.quantile) + 2, str((w != 0).sum()))
check("no name exceeds the per-name cap",
      np.abs(w).max() <= P.max_weight + 1e-12, f"{np.abs(w).max():.4f}")
check("a universe below min_names produces no book",
      np.all(CS.target_weights(rng.normal(size=4), P) == 0))
sig = rng.normal(size=50)
sig[::7] = np.nan
w2 = CS.target_weights(sig, P)
check("missing names are excluded, not guessed", abs(w2.sum()) < 1e-12)

print("\n[2] Breadth: the number the whole design rests on")
check("four highly correlated names are worth ~1.5, not 4",
      1.0 <= CS.effective_breadth(universe(4, 0.76, seed=1)) <= 2.0,
      f"{CS.effective_breadth(universe(4, 0.76, seed=1)):.2f}")
check("four INDEPENDENT names are worth 4",
      abs(CS.effective_breadth(universe(4, 0.0, seed=2)) - 4.0) < 0.3)
check("100 independent names are worth ~100",
      CS.effective_breadth(universe(100, 0.0, seed=3)) > 85)
check("adding correlated names saturates rather than compounds",
      CS.effective_breadth(universe(300, 0.30, seed=4))
      < 3 * CS.effective_breadth(universe(100, 0.30, seed=5)))
# The regression that produced 100,492 for sixty names.
demeaned = universe(60, 0.4, seed=6)
demeaned = demeaned - demeaned.mean(axis=1, keepdims=True)
b = CS.effective_breadth(demeaned)
check("cross-sectionally demeaned returns do not explode the measure",
      np.isfinite(b) and b <= 60.0, f"{b:.1f}")
check("a single name reports breadth 1",
      CS.effective_breadth(universe(1, 0.0, seed=7)) == 1.0)

# Live regression: the real 300-symbol run reported breadth 1.0 and failed
# hurdle 4. That was np.corrcoef propagating one NaN across the whole matrix,
# the finite-column filter keeping nothing, and a guard returning 1.0 - a
# data failure wearing the costume of a market verdict.
clean = universe(50, 0.30, seed=8)
base = CS.effective_breadth(clean)
one_nan = clean.copy()
one_nan[5, 7] = np.nan
check("one stray NaN does not collapse the measurement",
      abs(CS.effective_breadth(one_nan) - base) < 0.5,
      f"{CS.effective_breadth(one_nan):.2f} vs {base:.2f}")
late = clean.copy()
late[:1500, 3] = np.nan          # a symbol listed part-way through
check("a late-listed name does not collapse it either",
      abs(CS.effective_breadth(late) - base) < 1.0,
      f"{CS.effective_breadth(late):.2f} vs {base:.2f}")
scattered = clean.copy()
scattered[np.random.default_rng(9).random(clean.shape) < 0.02] = np.nan
check("2% scattered missing data is tolerated",
      np.isfinite(CS.effective_breadth(scattered)))
check("an unmeasurable panel returns NaN, never a plausible-looking 1.0",
      np.isnan(CS.effective_breadth(np.full((100, 20), np.nan))))
check("...and a mostly-empty panel does too",
      np.isnan(CS.effective_breadth(
          np.where(np.random.default_rng(10).random((200, 20)) < 0.9,
                   np.nan, 1.0))))

print("\n[3] Turnover control")
t = CS.target_weights(rng.normal(size=50), P)
prev = CS.target_weights(rng.normal(size=50), P)
damped = CS.apply_damping(t, prev, CS.BookParams(damping=0.9, no_trade_band=0.0))
check("heavy damping moves only slightly toward the target",
      CS.turnover(damped, prev) < 0.25 * CS.turnover(t, prev),
      f"{CS.turnover(damped, prev):.4f} vs {CS.turnover(t, prev):.4f}")
check("zero damping goes straight to the target",
      np.allclose(CS.apply_damping(t, prev, CS.BookParams(damping=0.0,
                                                          no_trade_band=0.0)), t))
banded = CS.apply_damping(t, prev, CS.BookParams(damping=0.0, no_trade_band=1.0))
check("a wide no-trade band suppresses every trade",
      np.allclose(banded, prev))
check("the first book's turnover is its full gross",
      abs(CS.turnover(t, None) - np.abs(t).sum()) < 1e-12)

print("\n[4] Cost accounting - turnover is not a cost until it meets a fee")
rets = np.full(100, 0.001)
tos = np.full(100, 0.16)
st = B.summarise(rets, tos, np.full(100, 0.05), 365, cost_bps=7.32)
check("annual turnover is reported separately from cost",
      abs(st["annual_turnover"] - 0.16 * 365) < 1e-6, f"{st['annual_turnover']:.1f}")
check("cost drag includes the fee and is a plausible percentage",
      3.0 < st["cost_drag_pct"] < 6.0, f"{st['cost_drag_pct']:.2f}%")
check("a zero fee means zero cost drag",
      B.summarise(rets, tos, np.full(100, 0.05), 365, 0.0)["cost_drag_pct"] == 0.0)
check("doubling the fee doubles the drag",
      abs(B.summarise(rets, tos, np.full(100, 0.05), 365, 14.64)["cost_drag_pct"]
          - 2 * st["cost_drag_pct"]) < 1e-9)

print("\n[5] IC is measured, not inferred from P&L")
x = rng.normal(size=200)
check("a perfect forecast has IC 1", abs(CS.spearman_ic(x, x) - 1.0) < 1e-9)
check("an inverted forecast has IC -1", abs(CS.spearman_ic(x, -x) + 1.0) < 1e-9)
check("an unrelated forecast has IC near 0",
      abs(CS.spearman_ic(x, rng.normal(size=200))) < 0.2)
check("too few names give NaN rather than a fake number",
      not np.isfinite(CS.spearman_ic(np.arange(3.0), np.arange(3.0))))

print("\n[6] Signals never read the future")
px = np.cumprod(1 + rng.normal(0, 0.01, (500, 20)), axis=0) * 100
for name, fn in CS.SIGNALS.items():
    a = fn(px, 300)
    b_ = fn(px[:301], 300)
    ok = np.isfinite(a) & np.isfinite(b_)
    check(f"{name}: bar 300 is unchanged when later bars are deleted",
          np.allclose(a[ok], b_[ok]) if ok.any() else True)
check("a signal with insufficient history returns NaN, not zero",
      np.all(np.isnan(CS.sig_momentum(px, 5))))

print("\n[6b] Beta is a time-series regression, not a cross-section")
# First version computed the covariance of WEIGHTS with one cross-section of
# returns - a rank correlation wearing beta's name. Beta is the regression of
# the book's realised return series on the market's.
r6 = np.random.default_rng(11)
mkt = r6.normal(0, 0.02, 500)
check("a book that IS the market has beta 1",
      abs(CS.book_beta(mkt, mkt) - 1.0) < 1e-9)
check("a book short the market has beta -1",
      abs(CS.book_beta(-mkt, mkt) + 1.0) < 1e-9)
check("partial exposure is recovered",
      abs(CS.book_beta(0.3 * mkt + r6.normal(0, 0.01, 500), mkt) - 0.3) < 0.05)
check("an independent book has beta near zero",
      abs(CS.book_beta(r6.normal(0, 0.02, 500), mkt)) < 0.1)
check("too few observations give NaN, not a number",
      np.isnan(CS.book_beta(mkt[:5], mkt[:5])))
check("a flat market does not divide by zero",
      CS.book_beta(mkt, np.zeros(500)) == 0.0)

print("\n[6c] Signal smoothing cuts turnover without inventing information")
px_s = np.cumprod(1 + r6.normal(0, 0.01, (400, 30)), axis=0) * 100
raw = CS.sig_momentum(px_s, 300)
sm = CS.smoothed(CS.sig_momentum, 5)(px_s, 300)
check("a smoothed signal is still defined", np.isfinite(sm).sum() > 20)
check("...and still ranks broadly like the raw signal",
      CS.spearman_ic(raw, sm) > 0.7, f"{CS.spearman_ic(raw, sm):.2f}")
check("smoothing does not read the future",
      np.allclose(CS.smoothed(CS.sig_momentum, 5)(px_s[:301], 300)[np.isfinite(sm)],
                  sm[np.isfinite(sm)]))
w_raw, w_sm = [], []
prev_r = prev_s = None
tr_r = tr_s = 0.0
for i in range(200, 380, 6):
    a_ = CS.target_weights(CS.sig_momentum(px_s, i), P)
    b_ = CS.target_weights(CS.smoothed(CS.sig_momentum, 5)(px_s, i), P)
    tr_r += CS.turnover(a_, prev_r)
    tr_s += CS.turnover(b_, prev_s)
    prev_r, prev_s = a_, b_
check("smoothing reduces cumulative turnover", tr_s < tr_r,
      f"smoothed {tr_s:.2f} vs raw {tr_r:.2f}")

print("\n[7] End to end: it finds a planted edge and rejects noise")
T, N = 2500, 40
r2 = np.random.default_rng(17)
fac = r2.normal(0, 0.010, T)
alpha = r2.normal(0, 0.0015, N)
lp = np.zeros((T, N))
for t_ in range(1, T):
    mom = (lp[t_ - 1] - lp[max(0, t_ - 169)]) if t_ > 169 else np.zeros(N)
    lp[t_] = lp[t_ - 1] + fac[t_] + alpha + 0.02 * np.tanh(mom * 8) * 0.02 \
        + r2.normal(0, 0.012, N)
px_sig = 100 * np.exp(lp)
r3 = np.random.default_rng(83)
px_noise = 100 * np.exp(np.cumsum(r3.normal(0, 0.010, (T, 1))
                                  + r3.normal(0, 0.012, (T, N)), axis=0))

good = B.summarise(*B.run_book(px_sig, CS.sig_momentum, P, 24, 7.32), 365, 7.32)
noise = B.summarise(*B.run_book(px_noise, CS.sig_momentum, P, 24, 7.32), 365, 7.32)
check("a planted cross-sectional edge shows a large IC t",
      good["ic_t"] > 5.0, f"t={good['ic_t']:.2f}")
check("...and a positive net Sharpe after costs", good["sharpe"] > 0,
      f"{good['sharpe']:.2f}")
check("a structureless universe does not", abs(noise["ic_t"]) < 5.0,
      f"t={noise['ic_t']:.2f}")

shuf = B.summarise(*B.run_book(px_sig, CS.sig_momentum, P, 24, 7.32,
                               shuffle_rng=np.random.default_rng(5)), 365, 7.32)
check("shuffling the ranks destroys the edge it just found",
      abs(shuf["ic_t"]) < good["ic_t"] / 3,
      f"shuffled t={shuf['ic_t']:.2f} vs real t={good['ic_t']:.2f}")

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
