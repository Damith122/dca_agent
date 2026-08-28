#!/usr/bin/env python3
"""Tests for the cointegration pairs backtest.

Pairs trading is unusually good at producing convincing backtests of nothing:
two unrelated random walks drift apart and back, and a spread built from them
looks like it mean-reverts right up until it does not. So these tests are
mostly about the machinery that refuses a trade.

The two that matter most are at the bottom, and both were failures first:
the pipeline must find a planted cointegration and must NOT find one in
independent random walks.
"""
import csv
import os
import shutil
import sys
import tempfile

import numpy as np

import backtest_stat_arb as B
import stat_arb as SA

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


rng = np.random.default_rng(1)

print("[1] OLS and the ADF statistic")
x = np.arange(200.0)
b, c = SA.ols(3.0 * x + 7.0, x)
check("OLS recovers a known slope and intercept",
      abs(b - 3.0) < 1e-9 and abs(c - 7.0) < 1e-9, f"{b:.6f}, {c:.6f}")
check("a degenerate x does not divide by zero",
      SA.ols(np.ones(50), np.ones(50)) == (0.0, 1.0))

walk = np.cumsum(rng.normal(0, 1, 3000))
ou = np.zeros(3000)
for i in range(1, 3000):
    ou[i] = 0.9 * ou[i - 1] + rng.normal(0, 1)
check("ADF is near zero for a random walk", SA.adf_stat(walk) > -2.5,
      f"{SA.adf_stat(walk):.2f}")
check("ADF is strongly negative for a mean-reverting series",
      SA.adf_stat(ou) < -8.0, f"{SA.adf_stat(ou):.2f}")
check("a too-short series returns 0 rather than nonsense",
      SA.adf_stat(np.arange(5.0)) == 0.0)

print("\n[2] Half-life")
hl = SA.half_life(ou)
check("an AR(1) with rho 0.9 has a half-life near ln2/0.105 = 6.6 bars",
      5.0 < hl < 9.0, f"{hl:.2f}")
check("a random walk has an unusable half-life",
      SA.half_life(walk) > 100 or SA.half_life(walk) == float("inf"),
      f"{SA.half_life(walk):.1f}")
diverging = np.cumsum(np.abs(rng.normal(0, 1, 500))) ** 1.5
check("a diverging series reports infinite half-life",
      SA.half_life(diverging) == float("inf"))

print("\n[3] fit_pair accepts real cointegration and rejects spurious")
f = np.cumsum(rng.normal(0, 0.01, 3000))
wob = np.zeros(3000)
for i in range(1, 3000):
    wob[i] = 0.95 * wob[i - 1] + rng.normal(0, 0.01)
st = SA.fit_pair(1.3 * f + wob, f)
check("a genuinely cointegrated pair is accepted", st.cointegrated, st.reason)
check("...with the hedge ratio recovered", abs(st.beta - 1.3) < 0.1, f"{st.beta:.3f}")
bad = SA.fit_pair(np.cumsum(rng.normal(0, 0.01, 3000)),
                  np.cumsum(rng.normal(0, 0.01, 3000)))
check("two independent walks are rejected", not bad.cointegrated, bad.reason)
check("the rejection says why", "ADF" in bad.reason or "revert" in bad.reason,
      bad.reason)

print("\n[4] The Engle-Granger thresholds are the stricter ones")
check("5% critical value is -3.34, not the plain-ADF -2.86",
      SA.EG_CRITICAL[0.05] == -3.34, str(SA.EG_CRITICAL[0.05]))
check("stricter alpha demands a more negative statistic",
      SA.EG_CRITICAL[0.01] < SA.EG_CRITICAL[0.05] < SA.EG_CRITICAL[0.10])

print("\n[5] Cost scales with the hedge ratio, profit does not")
flat = SA.PairStats(beta=0.0, intercept=0.0, spread_mean=0.0, spread_sd=0.002,
                    adf=-5, half_life=10, cointegrated=True)
levered = SA.PairStats(beta=2.0, intercept=0.0, spread_mean=0.0, spread_sd=0.002,
                       adf=-5, half_life=10, cointegrated=True)
n_flat = SA.expected_net_bps(flat, 2.0, 0.5, 7.32)
n_lev = SA.expected_net_bps(levered, 2.0, 0.5, 7.32)
check("a beta of 2 costs three times a beta of 0",
      abs((n_flat - n_lev) - 2 * 7.32) < 1e-9, f"{n_flat:.2f} vs {n_lev:.2f}")
check("...so the levered pair nets less for the same spread move", n_lev < n_flat)
check("a spread too small to cover both legs nets negative",
      SA.expected_net_bps(
          SA.PairStats(1.0, 0, 0, 0.0002, -5, 10, True), 2.0, 0.5, 7.32) < 0)

print("\n[6] The t-statistic, not the average, is what gets compared")
lucky = B.summarise([{"net_bps": 400.0, "bars": 3, "reason": "target"},
                     {"net_bps": 400.0, "bars": 3, "reason": "target"}])
grind = B.summarise([{"net_bps": 8.0, "bars": 5, "reason": "target"}] * 300
                    + [{"net_bps": -6.0, "bars": 5, "reason": "stop"}] * 200)
check("two lucky trades show a huge per-trade average",
      lucky["expectancy_bps"] > 300, f"{lucky['expectancy_bps']:.0f}")
check("...but a far weaker t-statistic than a long grind",
      grind["t_stat"] > lucky["t_stat"],
      f"grind t={grind['t_stat']:.1f} vs lucky t={lucky['t_stat']:.1f}")
check("an empty result has t = 0", B.summarise([])["t_stat"] == 0.0)
check("a single trade cannot manufacture a t-stat",
      B.summarise([{"net_bps": 999.0, "bars": 1, "reason": "target"}])["t_stat"] == 0.0)

print("\n[7] Walk-forward never fits on the window it trades")
src = open("backtest_stat_arb.py", encoding="utf-8").read()
check("the fit uses only data up to the training boundary",
      "SA.fit_pair(log_a[:tr_hi], log_b[:tr_hi]" in src)
check("the trade uses only data after it",
      "trade_pair(log_a[tr_hi:te_hi], log_b[tr_hi:te_hi], st," in src)
check("a pair failing cointegration in a fold is skipped, not traded anyway",
      "if not st.cointegrated:\n            continue" in src)

print("\n[8] End to end")
tmp = tempfile.mkdtemp(prefix="statarb_test_")


def write(d, sym, logp):
    os.makedirs(d, exist_ok=True)
    p = np.exp(logp) * 100
    with open(os.path.join(d, f"{sym}.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for i, v in enumerate(p):
            w.writerow([1_700_000_000_000 + i * 3_600_000, v, v * 1.003,
                        v * 0.997, v, 100])


try:
    n = 3000
    r2 = np.random.default_rng(21)
    fac = np.cumsum(r2.normal(0, 0.008, n))
    for sym, beta, rho, sd in [("AAAUSDT", 1.0, 0.0, 0.0), ("BBBUSDT", 1.15, 0.97, 0.004),
                               ("CCCUSDT", 0.85, 0.96, 0.005)]:
        o = np.zeros(n)
        for i in range(1, n):
            o[i] = rho * o[i - 1] + r2.normal(0, sd)
        write(os.path.join(tmp, "coint"), sym, beta * fac + o)
    r3 = np.random.default_rng(99)
    for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        write(os.path.join(tmp, "indep"), sym, np.cumsum(r3.normal(0, 0.008, n)))

    def run(d):
        raw = {s: B.load_closes(os.path.join(tmp, d), s)
               for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")}
        _, px = B.align(raw)
        logs = {s: np.log(v) for s, v in px.items()}
        import itertools
        trades = []
        for A, C in itertools.combinations(sorted(logs), 2):
            tr, _ = B.walk_forward_pair(logs[A], logs[C], 5, 2.0, 0.5, 4.0,
                                        7.32, 0.05, 200)
            trades.extend(tr)
        return B.summarise(trades)

    good = run("coint")
    check("planted cointegration is found and traded", good["trades"] > 20,
          str(good["trades"]))
    check("...profitably after both legs' fees", good["expectancy_bps"] > 0,
          f"{good['expectancy_bps']:+.1f} bps")
    check("...with a t-statistic that clears a Bonferroni hurdle",
          good["t_stat"] > 3.0, f"t={good['t_stat']:.2f}")

    noise = run("indep")
    check("independent walks do not clear the same hurdle",
          noise["t_stat"] < 3.0, f"t={noise['t_stat']:.2f}")

    nd = B.null_distribution(1500, 3, 25, 4, 2.0, 0.5, 4.0, 7.32, 0.05, 200,
                             0.008, seed=3)
    check("the false-positive rate is near the nominal alpha",
          2.0 <= nd["flag_pct"] <= 12.0, f"{nd['flag_pct']:.1f}%")
    check("the null control produces a distribution, not one number",
          len(nd["means"]) >= 3, str(len(nd["means"])))
    if len(nd["means"]):
        check("...whose right tail is far above zero, which is the whole warning",
              float(np.percentile(nd["means"], 95)) > 0.5,
              f"95th pct t={np.percentile(nd['means'], 95):.2f}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
