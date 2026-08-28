#!/usr/bin/env python3
"""Tests for the cost/skill model.

The formulas decide a strategic pivot, so they are checked against their own
derivation rather than eyeballed: the optimiser must actually find the
maximum, the closed forms must agree with a numerical sweep, and the scaling
laws must hold.
"""
import math
import sys

from edge_requirements import (HOURS_PER_YEAR, ic_for_sharpe, ic_required,
                               max_sharpe, optimal_horizon, sharpe)

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


S1 = 82.6 / 1e4        # measured on the real dataset
C = 7.32 / 1e4

print("[1] Break-even skill, equation (2)")
check("required IC falls as 1/sqrt(h)",
      abs(ic_required(S1, C, 4) - ic_required(S1, C, 1) / 2) < 1e-12)
check("holding 100x longer needs 10x less skill",
      abs(ic_required(S1, C, 100) * 10 - ic_required(S1, C, 1)) < 1e-12)
check("required IC rises with cost",
      ic_required(S1, 2 * C, 1) > ic_required(S1, C, 1))
check("required IC falls with volatility",
      ic_required(2 * S1, C, 1) < ic_required(S1, C, 1))
check("at break-even the Sharpe is exactly zero",
      abs(sharpe(S1, C, ic_required(S1, C, 24), 24)) < 1e-9)

print("\n[2] The closed-form optimum really is the maximum")
for ic in (0.02, 0.05, 0.10):
    h_star = optimal_horizon(S1, C, ic)
    s_star = max_sharpe(S1, C, ic)
    check(f"IC={ic}: closed form agrees with sharpe() at h*",
          abs(sharpe(S1, C, ic, h_star) - s_star) < 1e-9,
          f"{sharpe(S1, C, ic, h_star):.6f} vs {s_star:.6f}")
    # Brute-force sweep must not beat it.
    best = max(sharpe(S1, C, ic, h) for h in
               [h_star * f for f in (0.1, 0.25, 0.5, 0.8, 1.25, 2, 4, 10)])
    check(f"IC={ic}: no nearby horizon beats h*", s_star >= best - 1e-9,
          f"sweep found {best:.6f} vs {s_star:.6f}")

print("\n[3] The scaling laws in equation (5)")
base = max_sharpe(S1, C, 0.03)
check("halving the cost doubles the achievable Sharpe",
      abs(max_sharpe(S1, C / 2, 0.03) - 2 * base) < 1e-9)
check("doubling the IC quadruples it",
      abs(max_sharpe(S1, C, 0.06) - 4 * base) < 1e-9)
check("doubling volatility doubles it",
      abs(max_sharpe(2 * S1, C, 0.03) - 2 * base) < 1e-9)
check("halving the cost quarters the optimal horizon",
      abs(optimal_horizon(S1, C / 2, 0.03) - optimal_horizon(S1, C, 0.03) / 4) < 1e-6)
check("a stronger forecast should be traded FASTER",
      optimal_horizon(S1, C, 0.10) < optimal_horizon(S1, C, 0.02))

print("\n[4] No edge means no horizon works")
check("zero IC gives zero Sharpe", max_sharpe(S1, C, 0.0) == 0.0)
check("zero IC has no optimal horizon", optimal_horizon(S1, C, 0.0) == float("inf"))
check("a negative IC is not rescued by holding longer",
      all(sharpe(S1, C, -0.01, h) < 0 for h in (1, 24, 24 * 7, 24 * 90)))
check("a tiny IC stays untradeable at its own best horizon",
      max_sharpe(S1, C, 0.005) < 0.05, f"{max_sharpe(S1, C, 0.005):.4f}")

print("\n[5] Inverting for a target")
for target in (0.5, 1.0, 2.0):
    need = ic_for_sharpe(S1, C, target)
    check(f"the IC solved for Sharpe {target} reproduces it",
          abs(max_sharpe(S1, C, need) - target) < 1e-9,
          f"{max_sharpe(S1, C, need):.6f}")
check("a cheaper venue needs less skill for the same Sharpe",
      ic_for_sharpe(S1, 4.0 / 1e4, 1.0) < ic_for_sharpe(S1, C, 1.0))

print("\n[6] The numbers this project actually measured")
check("a 15-minute horizon demands an implausible IC",
      ic_required(S1, C, 0.25) > 0.15, f"{ic_required(S1, C, 0.25):.4f}")
check("a 1-week horizon demands a plausible one",
      ic_required(S1, C, 24 * 7) < 0.01, f"{ic_required(S1, C, 24 * 7):.4f}")
check("Sharpe 1.0 at current cost needs better than a 53% hit rate",
      0.05 < ic_for_sharpe(S1, C, 1.0) < 0.08,
      f"{ic_for_sharpe(S1, C, 1.0):.4f}")

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
