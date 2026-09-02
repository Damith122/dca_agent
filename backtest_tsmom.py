#!/usr/bin/env python3
"""Historical validation for the low-frequency TSMOM candidate.

Example (public Binance endpoints, no keys):

    python backtest_tsmom.py --months 60 \
        --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT

The primary rule is fixed before the data is read: 30-day volatility-scaled
momentum, long/cash, weekly review, next-open fills, ATR risk sizing.  Nearby
lookbacks and a long/short variant are reported as robustness checks, not used
to retune the primary result.  Every result includes per-side fees/slippage
and the exact historical perpetual funding prints.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, replace
from typing import Dict, List, Mapping, Sequence

import numpy as np

import backtest_breakout
import fetch_funding_universe
from breakout import Candle
from tsmom import TSMOMParams, align_candles, run, stats


def funding_by_day(rows):
    out: Dict[int, float] = {}
    for ts, rate_bps in rows:
        day = int(ts // 86400 * 86400)
        out[day] = out.get(day, 0.0) + float(rate_bps)
    return out


def fetch_inputs(symbols: Sequence[str], months: float):
    candles: Dict[str, List[Candle]] = {}
    funding: Dict[str, Dict[int, float]] = {}
    for symbol in symbols:
        print(f"\n=== {symbol} public history ===")
        candles[symbol] = backtest_breakout.fetch(symbol, "1d", months)
        rows = fetch_funding_universe.fetch_symbol(symbol, months, pause=0.05)
        funding[symbol] = funding_by_day(rows)
        print(f"  funding prints: {len(rows)}")
    return candles, funding


def fmt(st: Mapping[str, float]) -> str:
    return (f"n={int(st['trades']):3d}  win={st['win_rate']:5.1f}%  "
            f"ret={st['total_return_pct']:+7.2f}%  "
            f"CAGR={st['cagr_pct']:+6.2f}%  PF={st['profit_factor']:4.2f}  "
            f"DD={st['max_drawdown_pct']:5.2f}%  "
            f"exp={st['expectancy_pct']:+6.3f}%")


def period_result(candles, funding, p, start, end, equity=1000.0):
    sim = run(candles, funding, p, starting_equity=equity,
              trade_start=start, trade_end=end)
    return stats(sim, equity), sim


def evaluate(candles, funding, p: TSMOMParams, folds: int):
    grid, _ = align_candles(candles)
    n = len(grid)
    warm = max(p.lookback, p.vol_lookback, p.atr_period) + 10
    if n < warm + 180:
        raise SystemExit(f"only {n} aligned daily bars; need at least {warm + 180}")

    full, full_sim = period_result(candles, funding, p, warm, n)
    hold_start = warm + int((n - warm) * 0.60)
    hold, hold_sim = period_result(candles, funding, p, hold_start, n)

    boundaries = np.linspace(warm, n, folds + 1, dtype=int)
    fold_stats = []
    for lo, hi in zip(boundaries[:-1], boundaries[1:]):
        st, _ = period_result(candles, funding, p, int(lo), int(hi))
        fold_stats.append(st)
    return {
        "bars": n,
        "start_ts": grid[0],
        "end_ts": grid[-1],
        "full": full,
        "holdout": hold,
        "folds": fold_stats,
        "params": asdict(p),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
    ap.add_argument("--months", type=float, default=60.0)
    ap.add_argument("--cost-bps-side", type=float, default=7.0,
                    help="fee + spread + slippage for each entry or exit")
    ap.add_argument("--stress-cost-bps-side", type=float, default=10.0)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    candles, funding = fetch_inputs(symbols, a.months)
    grid, aligned = align_candles(candles)
    if not grid:
        print("no common daily history")
        return 1
    days = (grid[-1] - grid[0]) / 86400.0
    print(f"\n=== aligned study: {len(symbols)} liquid survivors, "
          f"{len(grid)} days ({days / 365.25:.2f} years) ===")
    print("    Note: a current-liquid universe has survivorship bias. The strict")
    print("    final holdout and cost stress reduce overfitting, not survivorship.\n")

    base = TSMOMParams(cost_bps_per_side=a.cost_bps_side,
                       allow_short=False)
    variants = {
        "PRIMARY_30D_LONG_CASH": base,
        "ROBUST_60D_LONG_CASH": replace(base, lookback=60, vol_lookback=60),
        "ROBUST_90D_LONG_CASH": replace(base, lookback=90, vol_lookback=60),
        "RISK_30D_LONG_SHORT": replace(base, allow_short=True),
    }
    results = {}
    for name, p in variants.items():
        print(f"\n--- {name} ---")
        r = evaluate(candles, funding, p, a.folds)
        results[name] = r
        print("  all data       " + fmt(r["full"]))
        print("  FINAL 40%      " + fmt(r["holdout"]))
        for i, st in enumerate(r["folds"], 1):
            print(f"  period {i}/{len(r['folds'])}     " + fmt(st))

    stress = replace(base, cost_bps_per_side=a.stress_cost_bps_side)
    stress_result = evaluate(candles, funding, stress, a.folds)
    results["STRESS_PRIMARY"] = stress_result
    print(f"\n--- PRIMARY COST STRESS ({a.stress_cost_bps_side:g} bps/side) ---")
    print("  FINAL 40%      " + fmt(stress_result["holdout"]))

    primary = results["PRIMARY_30D_LONG_CASH"]
    h = primary["holdout"]
    fold_positive = sum(s["total_return_pct"] > 0 for s in primary["folds"])
    neighbor_positive = sum(results[n]["holdout"]["total_return_pct"] > 0
                            for n in ("PRIMARY_30D_LONG_CASH",
                                      "ROBUST_60D_LONG_CASH",
                                      "ROBUST_90D_LONG_CASH"))
    checks = {
        "final_holdout_positive": h["total_return_pct"] > 0,
        "final_holdout_pf_at_least_1_10": h["profit_factor"] >= 1.10,
        "final_holdout_at_least_10_trades": h["trades"] >= 10,
        "at_least_two_of_three_periods_positive": fold_positive >= 2,
        "at_least_two_neighbor_lookbacks_positive": neighbor_positive >= 2,
        "positive_at_stressed_cost": stress_result["holdout"]["total_return_pct"] > 0,
        "holdout_drawdown_below_25pct": h["max_drawdown_pct"] < 25.0,
    }
    passed = all(checks.values())
    print("\n=== DEPLOYMENT HURDLES ===")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s}  {name}")
    print("\n  " + ("CANDIDATE PASSES HISTORICAL ADMISSION. Paper only is the next step."
                     if passed else
                     "REJECTED. Do not paper/live deploy this candidate."))

    payload = {"symbols": symbols, "months": a.months, "results": results,
               "checks": checks, "passed": passed}
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"  wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
