#!/usr/bin/env python3
"""Position-sizing and risk-of-ruin simulator.

    python3 risk_simulator.py --win-rate 40 --avg-win 2.0 --avg-loss 1.0 \
                              --trades 200 --observed-trades 60

Answers the question that actually decides whether an account survives:
given a strategy's measured statistics, how much can be risked per trade
before ruin becomes likely - and how much of the measured edge is real?

The part most simulators get wrong
----------------------------------
A backtest gives you an ESTIMATE of the edge, not the edge. Simulating with
the point estimate assumes you measured it perfectly, which is exactly the
assumption that empties accounts. `--observed-trades` fixes that: the true
win rate is drawn from the Beta posterior implied by how many trades you
actually observed, so a 60-trade backtest is correctly treated as far less
certain than a 6000-trade one, and the simulator inherits that uncertainty.

Set --observed-trades to the number of trades your BACKTEST produced. If you
leave it out, the simulator assumes the edge is known exactly and its answers
will be too optimistic.
"""
from __future__ import annotations

import argparse
import sys

import numpy as np


def simulate(win_rate, avg_win, avg_loss, risk_pct, n_trades, n_paths=20000,
             observed_trades=None, ruin_at=0.5, seed=0):
    """Monte-Carlo equity paths. Returns a dict of outcome statistics.

    win_rate as a fraction; avg_win / avg_loss as R-multiples (an avg_win of
    2.0 means the average winner returns twice what a loser costs). Sizing is
    fractional: each trade risks `risk_pct` of CURRENT equity, so losses
    shrink the next bet and the account cannot go negative - it just decays.
    """
    rng = np.random.default_rng(seed)
    if observed_trades:
        # Uncertainty in the edge itself. Jeffreys prior, so a small sample
        # stays honestly wide instead of pretending to know the mean.
        wins = win_rate * observed_trades
        p = rng.beta(wins + 0.5, observed_trades - wins + 0.5, size=n_paths)
    else:
        p = np.full(n_paths, win_rate)

    equity = np.ones(n_paths)
    peak = np.ones(n_paths)
    max_dd = np.zeros(n_paths)
    ruined = np.zeros(n_paths, dtype=bool)

    for _ in range(n_trades):
        hit = rng.random(n_paths) < p
        r = np.where(hit, avg_win, -avg_loss)
        equity = equity * (1.0 + risk_pct * r)
        equity = np.maximum(equity, 1e-9)
        peak = np.maximum(peak, equity)
        max_dd = np.maximum(max_dd, (peak - equity) / peak)
        ruined |= equity <= (1.0 - ruin_at)

    return {
        "median_return_pct": (np.median(equity) - 1) * 100,
        "mean_return_pct": (equity.mean() - 1) * 100,
        "p05_return_pct": (np.percentile(equity, 5) - 1) * 100,
        "p95_return_pct": (np.percentile(equity, 95) - 1) * 100,
        "prob_profit": (equity > 1.0).mean() * 100,
        "prob_ruin": ruined.mean() * 100,
        "median_max_dd_pct": np.median(max_dd) * 100,
        "p95_max_dd_pct": np.percentile(max_dd, 95) * 100,
    }


def kelly(win_rate, avg_win, avg_loss):
    """Kelly fraction in R-multiple terms. Negative means no bet size is
    profitable - the edge is negative and leverage only speeds up the loss."""
    if avg_loss <= 0:
        return 0.0
    b = avg_win / avg_loss
    return (win_rate * (b + 1) - 1) / b


def expectancy_r(win_rate, avg_win, avg_loss):
    return win_rate * avg_win - (1 - win_rate) * avg_loss


def trades_needed(win_rate, avg_win, avg_loss, confidence=1.96, power=0.84):
    """How many trades before the edge is distinguishable from zero.

    Uses the per-trade R-multiple distribution: n = ((z_a + z_b) * sd / mean)^2.
    """
    mu = expectancy_r(win_rate, avg_win, avg_loss)
    if mu <= 0:
        return float("inf")
    var = (win_rate * (avg_win - mu) ** 2
           + (1 - win_rate) * (-avg_loss - mu) ** 2)
    return ((confidence + power) * np.sqrt(var) / mu) ** 2


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--win-rate", type=float, default=40.0, help="percent")
    ap.add_argument("--avg-win", type=float, default=2.0, help="R multiple")
    ap.add_argument("--avg-loss", type=float, default=1.0, help="R multiple")
    ap.add_argument("--trades", type=int, default=200, help="trades to simulate")
    ap.add_argument("--observed-trades", type=int, default=None,
                    help="how many trades your backtest actually produced. "
                         "Omitting this assumes the edge is known exactly.")
    ap.add_argument("--risk-pct", type=float, default=None,
                    help="risk per trade as a percent; omit to sweep")
    ap.add_argument("--paths", type=int, default=20000)
    a = ap.parse_args(argv)

    wr = a.win_rate / 100.0
    exp_r = expectancy_r(wr, a.avg_win, a.avg_loss)
    k = kelly(wr, a.avg_win, a.avg_loss)

    print("=== strategy statistics ===")
    print(f"  win rate            {a.win_rate:.1f}%")
    print(f"  avg win / avg loss  {a.avg_win:.2f}R / {a.avg_loss:.2f}R")
    print(f"  expectancy          {exp_r:+.4f}R per trade")
    print(f"  full Kelly          {k * 100:+.1f}% of equity per trade")
    if exp_r <= 0:
        print("\n  EXPECTANCY IS NEGATIVE. No position size makes this profitable;")
        print("  larger size only loses the money faster. Stop here.")
        return 1
    print(f"  quarter Kelly       {k * 25:.1f}%  <- the practical ceiling; full")
    print("                          Kelly assumes the edge is known exactly")

    n = trades_needed(wr, a.avg_win, a.avg_loss)
    print(f"\n  trades needed to prove this edge is real (95%/80%): {n:.0f}")
    if a.observed_trades:
        print(f"  trades you have observed:                          {a.observed_trades}")
        if a.observed_trades < n:
            print(f"  -> you are {n / a.observed_trades:.1f}x short of proving it. "
                  "The sweep below\n     accounts for that uncertainty.")

    if a.risk_pct is not None:
        sweep = [a.risk_pct / 100.0]
    else:
        sweep = [0.0025, 0.005, 0.01, 0.02, 0.03, 0.05, 0.10]

    print(f"\n=== {a.trades} trades, {a.paths} simulated paths"
          + (f", edge uncertainty from {a.observed_trades} observed trades"
             if a.observed_trades else ", edge assumed known exactly") + " ===")
    print(f"{'risk/trade':>11s} {'median':>9s} {'5th pct':>9s} {'95th pct':>9s} "
          f"{'P(profit)':>10s} {'med DD':>8s} {'95% DD':>8s} {'P(-50%)':>9s}")
    for rp in sweep:
        s = simulate(wr, a.avg_win, a.avg_loss, rp, a.trades,
                     n_paths=a.paths, observed_trades=a.observed_trades)
        flag = "  <-- ruin risk" if s["prob_ruin"] > 5 else ""
        print(f"{rp * 100:10.2f}% {s['median_return_pct']:+8.1f}% "
              f"{s['p05_return_pct']:+8.1f}% {s['p95_return_pct']:+8.1f}% "
              f"{s['prob_profit']:9.1f}% {s['median_max_dd_pct']:7.1f}% "
              f"{s['p95_max_dd_pct']:7.1f}% {s['prob_ruin']:8.1f}%{flag}")

    print("\n  P(-50%) is the chance of halving the account at least once.")
    print("  Read the 5th-percentile column, not the median: it is the outcome")
    print("  you must be able to survive, and it is where accounts actually end.")
    print("\n  Leverage note: risking 1% per trade with a 1.5-ATR stop is NOT 1x")
    print("  leverage - it is whatever notional makes that stop cost 1%. The")
    print("  breakout engine caps this with BREAKOUT_MAX_LEVERAGE; keep it low")
    print("  until the edge has the trade count above to back it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
