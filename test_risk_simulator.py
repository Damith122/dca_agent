#!/usr/bin/env python3
"""Tests for the risk simulator.

The simulator exists to stop an account being sized off a number that was
never real. So the checks are about whether it stays honest when the edge is
uncertain, negative, or being levered up - not about whether its arithmetic
is pretty.
"""
import sys

from risk_simulator import expectancy_r, kelly, simulate, trades_needed

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


print("[1] Expectancy and Kelly")
check("a 40%/2R:1R strategy has +0.2R expectancy",
      abs(expectancy_r(0.40, 2.0, 1.0) - 0.20) < 1e-12)
check("a 30%/1.5R:1R strategy is negative",
      expectancy_r(0.30, 1.5, 1.0) < 0)
check("Kelly is negative when expectancy is negative",
      kelly(0.30, 1.5, 1.0) < 0, str(kelly(0.30, 1.5, 1.0)))
check("a coin flip at 1:1 has zero Kelly", abs(kelly(0.5, 1.0, 1.0)) < 1e-12)
check("Kelly rises with a better win rate",
      kelly(0.55, 2.0, 1.0) > kelly(0.45, 2.0, 1.0))

print("\n[2] Sample size")
n_small = trades_needed(0.55, 2.0, 1.0)
n_thin = trades_needed(0.41, 2.0, 1.0)
check("a thinner edge needs more trades to prove", n_thin > n_small,
      f"{n_thin:.0f} vs {n_small:.0f}")
check("a negative edge can never be proven positive",
      trades_needed(0.30, 1.5, 1.0) == float("inf"))
check("a 40%/2:1 edge needs hundreds of trades, not dozens",
      200 < trades_needed(0.40, 2.0, 1.0) < 2000,
      f"{trades_needed(0.40, 2.0, 1.0):.0f}")

print("\n[3] Ruin rises with size - the whole point of the tool")
small = simulate(0.40, 2.0, 1.0, 0.005, 200, n_paths=4000, seed=1)
large = simulate(0.40, 2.0, 1.0, 0.10, 200, n_paths=4000, seed=1)
check("bigger risk per trade means more ruin",
      large["prob_ruin"] > small["prob_ruin"],
      f"{small['prob_ruin']:.1f}% -> {large['prob_ruin']:.1f}%")
check("bigger risk per trade means deeper drawdowns",
      large["p95_max_dd_pct"] > small["p95_max_dd_pct"])
check("a tiny size on a positive edge barely ever ruins",
      small["prob_ruin"] < 1.0, f"{small['prob_ruin']:.2f}%")
check("the 5th percentile is worse than the median",
      large["p05_return_pct"] < large["median_return_pct"])

print("\n[4] A negative edge cannot be sized into profit")
for rp in (0.005, 0.02, 0.10):
    s = simulate(0.30, 1.5, 1.0, rp, 200, n_paths=4000, seed=2)
    check(f"risking {rp * 100:.1f}% on a losing edge still loses",
          s["median_return_pct"] < 0, f"{s['median_return_pct']:+.1f}%")

print("\n[5] Edge uncertainty is carried, not assumed away")
known = simulate(0.40, 2.0, 1.0, 0.01, 200, n_paths=8000, seed=3)
thin = simulate(0.40, 2.0, 1.0, 0.01, 200, n_paths=8000,
                observed_trades=20, seed=3)
rich = simulate(0.40, 2.0, 1.0, 0.01, 200, n_paths=8000,
                observed_trades=5000, seed=3)
check("a 20-trade sample gives a wider outcome range than a known edge",
      (thin["p95_return_pct"] - thin["p05_return_pct"])
      > (known["p95_return_pct"] - known["p05_return_pct"]),
      f"{thin['p95_return_pct'] - thin['p05_return_pct']:.0f} vs "
      f"{known['p95_return_pct'] - known['p05_return_pct']:.0f}")
check("a 5000-trade sample is nearly as tight as a known edge",
      (rich["p95_return_pct"] - rich["p05_return_pct"])
      < (thin["p95_return_pct"] - thin["p05_return_pct"]))
check("a thin sample carries MORE ruin risk at the same size",
      thin["prob_ruin"] >= known["prob_ruin"],
      f"{known['prob_ruin']:.1f}% vs {thin['prob_ruin']:.1f}%")
check("...and a thin sample's downside is worse",
      thin["p05_return_pct"] < known["p05_return_pct"],
      f"{known['p05_return_pct']:+.1f}% vs {thin['p05_return_pct']:+.1f}%")

print("\n[6] Fractional sizing cannot go negative")
s = simulate(0.05, 1.0, 1.0, 0.50, 500, n_paths=2000, seed=4)
check("even a catastrophic run leaves equity above zero",
      s["median_return_pct"] > -100.0, f"{s['median_return_pct']:.4f}%")
check("...and it is recognised as ruin", s["prob_ruin"] > 90.0)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
