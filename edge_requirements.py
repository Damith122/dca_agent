#!/usr/bin/env python3
"""What edge does a strategy need to survive its own trading costs?

    python3 edge_requirements.py --sigma-1h 82.6 --fee-bps 7.32

Every backtest in this project failed the same way, so it is worth writing
down why in closed form rather than trying another parameter set.

The model
---------
Let a strategy hold for h hours, and let sigma_h be the standard deviation of
the h-hour return. Its per-trade gross edge is

    mu = IC * sigma_h                                          (1)

where IC is the correlation between its forecast and the realised return -
the information coefficient. This is the standard decomposition: a forecast
can only extract as much as it correlates with what actually happens, scaled
by how much there is to extract.

Returns diffuse, so sigma_h = sigma_1 * sqrt(h). Break-even after a
round-trip cost c is mu > c, which gives the first result:

    IC_min(h) = c / (sigma_1 * sqrt(h))                        (2)

The required skill falls as 1/sqrt(h). Holding 100x longer cuts the skill
you need by 10x. That is the entire mathematical case for a higher
timeframe, and it is a real effect - but it is a sqrt, not a miracle.

Trading T hours a year at horizon h gives N = T/h trades. Annualised:

    return = N * (IC*sigma_h - c)
    vol    = sigma_h * sqrt(N)
    Sharpe = sqrt(T/h) * (IC - c/(sigma_1*sqrt(h)))            (3)

Maximise (3) over h. With u = 1/sqrt(h) it is a downward parabola in u, so

    h*      = (2c / (sigma_1 * IC))^2                          (4)
    Sharpe* = sqrt(T) * sigma_1 * IC^2 / (4c)                   (5)

Equation (5) is the one that matters. Read it carefully:

  * Sharpe scales with IC SQUARED. Doubling forecast skill quadruples the
    achievable Sharpe. This is where almost all the leverage is.
  * Sharpe scales with 1/c. Halving the fee doubles the achievable Sharpe -
    for free, with no new signal, no new model, no new data.
  * Sharpe scales with sigma_1. A more volatile asset needs less skill for
    the same result, PROVIDED its costs do not rise faster than its
    volatility. That is an empirical question per asset, and --spread-bps
    is there to answer it honestly.

Equation (4) says something less obvious: the optimal horizon is set by your
IC, not by taste. A strong forecast should be traded fast; a weak one must
be held long enough for the move to dwarf the fee. And if IC is
indistinguishable from zero, equation (5) gives Sharpe zero at every h -
no horizon rescues a strategy that cannot forecast. That is the situation
this project has measured four times, and it is why the fix has to be
structural.
"""
from __future__ import annotations

import argparse
import math
import sys

HOURS_PER_YEAR = 24 * 365


def ic_required(sigma_1: float, cost: float, h: float) -> float:
    """Equation (2): minimum IC to break even at horizon h."""
    return cost / (sigma_1 * math.sqrt(h))


def sharpe(sigma_1: float, cost: float, ic: float, h: float,
           hours_per_year: float = HOURS_PER_YEAR) -> float:
    """Equation (3): annualised net Sharpe at horizon h."""
    if h <= 0:
        return 0.0
    return math.sqrt(hours_per_year / h) * (ic - cost / (sigma_1 * math.sqrt(h)))


def optimal_horizon(sigma_1: float, cost: float, ic: float) -> float:
    """Equation (4). Infinite when there is no edge to optimise."""
    if ic <= 0:
        return float("inf")
    return (2.0 * cost / (sigma_1 * ic)) ** 2


def max_sharpe(sigma_1: float, cost: float, ic: float,
               hours_per_year: float = HOURS_PER_YEAR) -> float:
    """Equation (5)."""
    if ic <= 0:
        return 0.0
    return math.sqrt(hours_per_year) * sigma_1 * ic * ic / (4.0 * cost)


def ic_for_sharpe(sigma_1: float, cost: float, target: float,
                  hours_per_year: float = HOURS_PER_YEAR) -> float:
    """Invert (5): the IC needed to reach a target Sharpe at the best horizon."""
    return math.sqrt(4.0 * cost * target / (math.sqrt(hours_per_year) * sigma_1))


def fmt_h(h: float) -> str:
    if h == float("inf"):
        return "never"
    if h < 1:
        return f"{h * 60:.0f} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sigma-1h", type=float, default=82.6,
                    help="std dev of the 1-hour return, in bps (yours: 82.6)")
    ap.add_argument("--fee-bps", type=float, default=7.32,
                    help="round-trip cost including slippage")
    ap.add_argument("--spread-bps", type=float, default=0.0,
                    help="extra round-trip cost for a less liquid asset")
    ap.add_argument("--ic", type=float, default=None,
                    help="your measured information coefficient")
    a = ap.parse_args(argv)

    s1 = a.sigma_1h / 1e4
    c = (a.fee_bps + a.spread_bps) / 1e4
    print(f"=== sigma_1h = {a.sigma_1h:.1f} bps "
          f"({s1 * math.sqrt(HOURS_PER_YEAR) * 100:.0f}% annualised), "
          f"round-trip cost = {(a.fee_bps + a.spread_bps):.2f} bps ===\n")

    print("--- (2) skill required to break even, by holding period ---")
    print(f"{'horizon':>10s} {'sigma_h':>10s} {'IC needed':>11s}  interpretation")
    for h, name in [(0.25, "15 min"), (1, "1 hour"), (4, "4 hours"),
                    (24, "1 day"), (24 * 7, "1 week"), (24 * 30, "1 month")]:
        need = ic_required(s1, c, h)
        note = ("out of reach" if need > 0.15 else
                "very hard" if need > 0.07 else
                "hard but done" if need > 0.03 else "reachable")
        print(f"{name:>10s} {s1 * math.sqrt(h) * 1e4:9.0f}bp {need:11.4f}  {note}")

    print("\n--- (4)+(5) what a given skill level can actually earn ---")
    print(f"{'IC':>6s} {'best horizon':>14s} {'max Sharpe':>11s} "
          f"{'trades/yr':>10s}  verdict")
    for ic in (0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12):
        h = optimal_horizon(s1, c, ic)
        s = max_sharpe(s1, c, ic)
        n = HOURS_PER_YEAR / h if h != float("inf") and h > 0 else 0
        verdict = ("not worth trading" if s < 0.3 else
                   "marginal" if s < 0.8 else
                   "a real business" if s < 2.0 else "exceptional")
        print(f"{ic:6.3f} {fmt_h(h):>14s} {s:11.2f} {n:10.0f}  {verdict}")

    ic = a.ic if a.ic is not None else 0.03
    print(f"\n--- the cost lever, holding IC fixed at {ic:.4f} ---")
    print(f"{'round trip':>12s} {'best horizon':>14s} {'max Sharpe':>11s}  vs now")
    base = max_sharpe(s1, c, ic)
    for label, bps in [("current", a.fee_bps + a.spread_bps),
                       ("BNB -10%", (a.fee_bps + a.spread_bps) * 0.90),
                       ("maker only", 4.00),
                       ("maker + VIP1", 3.20),
                       ("maker + BNB", 3.60)]:
        cc = bps / 1e4
        s = max_sharpe(s1, cc, ic)
        print(f"{label:>12s} {bps:6.2f}bp {fmt_h(optimal_horizon(s1, cc, ic)):>10s} "
              f"{s:11.2f}  {s / base if base else 0:5.2f}x")

    if a.ic is not None:
        print(f"\n--- your measured IC = {a.ic:.4f} ---")
        h = optimal_horizon(s1, c, a.ic)
        print(f"  best horizon {fmt_h(h)}, max Sharpe {max_sharpe(s1, c, a.ic):.3f}")
        if max_sharpe(s1, c, a.ic) < 0.3:
            print("  No horizon and no asset in this universe makes that tradeable.")
            print("  The fix is not a parameter - it is more skill or less cost.")

    print("\n--- what you would need for a Sharpe of 1.0 ---")
    for label, bps in [("current cost", a.fee_bps + a.spread_bps),
                       ("maker only", 4.00)]:
        need = ic_for_sharpe(s1, bps / 1e4, 1.0)
        print(f"  at {label:>12s} ({bps:.2f}bp): IC = {need:.4f}  "
              f"-> a {50 + need * 50:.1f}% directional hit rate")
    print("\n  Retail equity/crypto research typically lands at IC 0.02-0.05.")
    print("  Anything above 0.10 out of sample is a claim to check, not to trust.")

    # --- the option that does not need an IC at all ----------------------
    #
    # Everything above prices SKILL. A funding-rate carry trade prices a
    # CASH FLOW instead: hold spot long against a perp short, stay delta
    # neutral, and collect the funding the perp pays every 8 hours. There
    # is no forecast, so IC does not appear - the only questions are
    # whether the carry exceeds the cost of getting in and out, and how
    # long it must be held to do so.
    print("\n--- the structurally different option: funding carry ---")
    print("  Delta-neutral (long spot / short perp). No forecast required, so")
    print("  none of the IC arithmetic above applies. Cost is paid on four")
    print("  legs, funding accrues three times a day.")
    legs = (a.fee_bps + a.spread_bps) * 2.0        # two instruments, in and out
    print(f"\n  round-trip cost across both legs: {legs:.2f} bps")
    print(f"{'funding/8h':>12s} {'annualised':>11s} {'break-even hold':>16s} "
          f"{'net after 30d':>14s}")
    for f8 in (0.0025, 0.005, 0.01, 0.02, 0.05):
        daily = f8 * 3.0
        ann = daily * 365
        be_days = legs / (daily * 100) if daily > 0 else float("inf")
        net30 = daily * 30 * 100 - legs
        print(f"{f8:11.4f}% {ann:10.1f}% {be_days:13.1f} d {net30:11.1f} bps")
    print("\n  Binance's funding on the majors sits near 0.010% per 8h in normal")
    print("  conditions - about 11% a year - and goes far higher when the market")
    print("  is one-sided. At that level the position pays for its own entry in")
    print(f"  roughly {legs / (0.01 * 3 * 100):.0f} days and earns thereafter.")
    print("\n  What this trade actually risks, since it is not risk-free:")
    print("    - funding flips negative and you pay instead of collect")
    print("    - the basis moves against you between the two unwinds")
    print("    - spot and perp margin are separate; a sharp move can liquidate")
    print("      the short leg while the long leg is fine but unusable as margin")
    print("    - it needs roughly 2x the capital of a directional trade for the")
    print("      same notional, so returns are on a bigger base than they look")
    return 0


if __name__ == "__main__":
    sys.exit(main())
