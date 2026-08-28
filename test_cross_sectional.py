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
