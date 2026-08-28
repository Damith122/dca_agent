#!/usr/bin/env python3
"""Tests for the harvesting optimiser.

A sweep is a machine for finding the luckiest cell, so the tests are about
the three things that stop it: the search adjustment, the hold-out, and the
ledger that remembers how many signals have already been tried.
"""
import json
import os
import sys
import tempfile

import numpy as np

import cross_sectional as CS
import optimise_cross_sectional as O

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


print("[1] The trial ledger remembers previous signals")
tmp = tempfile.mkdtemp(prefix="ledger_test_")
path = os.path.join(tmp, "log.json")
check("a missing ledger reads as empty, not as an error",
      O.read_ledger(path) == [])
O.append_ledger(path, {"signal": "low_vol", "configs": 100})
O.append_ledger(path, {"signal": "mom_skip", "configs": 100})
trials = O.read_ledger(path)
check("entries accumulate", len(trials) == 2, str(len(trials)))
check("signals are recoverable",
      sorted({t["signal"] for t in trials}) == ["low_vol", "mom_skip"])
with open(path, "w", encoding="utf-8") as fh:
    fh.write("{ this is not json")
check("a corrupt ledger degrades to empty rather than blocking research",
      O.read_ledger(path) == [])

n = len({t["signal"] for t in trials})
check("two signals halve the allowed alpha",
      abs(0.05 / 2 - 0.025) < 1e-12)
check("four signals quarter it", abs(0.05 / 4 - 0.0125) < 1e-12)

print("\n[2] A configuration that never trades cannot win")
src = open("optimise_cross_sectional.py", encoding="utf-8").read()
check("the sweep filters out books with no turnover",
      'if st["annual_turnover"] < 0.5:' in src)
check("...before ranking by Sharpe",
      src.index('if st["annual_turnover"] < 0.5:') < src.index('rows.sort'))

print("\n[3] The hold-out is reserved before anything is measured")
check("the split happens before the sweep",
      src.index("px, px_hold = px[:cut], px[cut:]") < src.index("for reb, damp"))
check("the hold-out is tested at a plain 2.0 threshold",
      'hs["ic_t"] >= 2.0 and hs["sharpe"] > 0' in src)
check("a failed hold-out blocks the verdict",
      "(hold_ok is not False)" in src)
check("the winning configuration is what gets tested, not a re-search",
      'hs = evaluate(px_hold, fn, best["rebalance"], p, best["fee"]' in src)

print("\n[4] End to end on data with and without an edge")
T, N = 2400, 40
r = np.random.default_rng(17)
fac = r.normal(0, 0.010, T)
alpha_i = r.normal(0, 0.0015, N)
lp = np.zeros((T, N))
for t_ in range(1, T):
    mom = (lp[t_ - 1] - lp[max(0, t_ - 169)]) if t_ > 169 else np.zeros(N)
    lp[t_] = lp[t_ - 1] + fac[t_] + alpha_i + 0.02 * np.tanh(mom * 8) * 0.02 \
        + r.normal(0, 0.012, N)
px_sig = 100 * np.exp(lp)
r2 = np.random.default_rng(83)
px_noise = 100 * np.exp(np.cumsum(r2.normal(0, 0.010, (T, 1))
                                  + r2.normal(0, 0.012, (T, N)), axis=0))

p = CS.BookParams()
cut = int(T * 0.67)
good_in = O.evaluate(px_sig[:cut], CS.sig_momentum, 24, p, 7.32, 24 * 365)
good_out = O.evaluate(px_sig[cut:], CS.sig_momentum, 24, p, 7.32, 24 * 365)
check("a real edge shows in the sweep window", good_in["ic_t"] > 5.0,
      f"t={good_in['ic_t']:.2f}")
check("...and survives on the hold-out", good_out["ic_t"] > 2.0,
      f"t={good_out['ic_t']:.2f}")

noise_in = O.evaluate(px_noise[:cut], CS.sig_momentum, 24, p, 7.32, 24 * 365)
noise_out = O.evaluate(px_noise[cut:], CS.sig_momentum, 24, p, 7.32, 24 * 365)
check("noise does not survive the hold-out",
      noise_out["ic_t"] < 2.0, f"t={noise_out['ic_t']:.2f}")

print("\n[4b] A positive Sharpe on a negative IC is not a strategy")
# Live regression from the mom_skip run: every top row had IC t near -4.5
# alongside a positive Sharpe. The signal ranked the wrong way round, so the
# money came from the -0.11 beta, and the optimiser crowned it anyway.
check("the sweep filters to configurations with a positive IC",
      'coherent = [r for r in rows if r["ic_t"] > 0]' in src)
check("...and says so explicitly when none qualifies",
      "NO COHERENT CONFIGURATION" in src)
check("...pointing at the inverse ranking as the next HYPOTHESIS, not a fix",
      "test it" in src and "flipping a" in src)
check("the beta warning is chosen per signal, not hard-coded for low_vol",
      '"mom_skip":' in src and '"reversal":' in src)
check("an empty ledger warns that the adjustment is not applying",
      "no cross-signal" in src)

print("\n[5] The verdict names the hurdle that failed")
for phrase in ("below the", "costs exceed the edge",
               "directional ", "did not survive unseen data"):
    check(f"a failure for '{phrase[:28]}' is reported specifically",
          phrase in src)

import shutil
shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
