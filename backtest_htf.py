#!/usr/bin/env python3
"""Chronological validation for the frozen 1H/4H/1D pullback candidate."""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace

import numpy as np

import backtest_breakout
from fetch_funding_universe import fetch_symbol
from htf import HTFParams, align_candles, run, stats
from paper_tsmom import fetch_execution_filters


DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"


def fetch_inputs(symbols, months):
    candles, funding = {}, {}

    def one(symbol):
        rows = backtest_breakout.fetch(symbol, "1h", months, verbose=False)
        rates = fetch_symbol(symbol, months, pause=0.03)
        return symbol, rows, {int(ts): float(rate) for ts, rate in rates}

    with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
        jobs = {pool.submit(one, symbol): symbol for symbol in symbols}
        for job in as_completed(jobs):
            symbol, rows, rates = job.result()
            candles[symbol] = rows
            funding[symbol] = rates
            print(f"  {symbol}: {len(rows)} hourly bars, {len(rates)} funding prints")
    return candles, funding


def evaluate(candles, funding, minimums, steps, p, equity, start, end):
    sim = run(candles, p, starting_equity=equity, trade_start=start,
              trade_end=end, funding_bps_by_ts=funding,
              min_notional_by_symbol=minimums, qty_step_by_symbol=steps)
    st = stats(sim, equity)
    st["exit_reasons"] = dict(sorted(Counter(t.reason for t in sim.trades).items()))
    st["exposure_blocks"] = sim.exposure_blocks
    st["daily_loss_blocks"] = sim.daily_loss_blocks
    st["daily_target_blocks"] = sim.daily_target_blocks
    st["daily_loss_lock_hits"] = sim.daily_loss_lock_hits
    st["daily_target_lock_hits"] = sim.daily_target_lock_hits
    st["blocked_min_notional"] = sim.blocked_min_notional
    st["estimated_monthly_pnl"] = (st["net_pnl"] / st["days"] * 30.44
                                   if st["days"] > 0 else 0.0)
    return st


def fmt(st):
    return (f"n={int(st['trades']):4d} win={st['win_rate']:5.1f}% "
            f"net=${st['net_pnl']:+7.3f} final=${st['final_equity']:6.3f} "
            f"PF={st['profit_factor']:4.2f} DD={st['max_drawdown_pct']:5.1f}% "
            f"avgW=${st['avg_win']:+.3f} avgL=${st['avg_loss']:+.3f} "
            f"sig/d={st['candidate_signals_per_day']:4.2f} "
            f"tr/d={st['trades_per_day']:4.2f} "
            f"est/mo=${st['estimated_monthly_pnl']:+.3f}")


def main(argv=None):
    # This validator needs public market data only.  Provisioned trading keys
    # are removed before any network call as defence in depth.
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "API_KEY", "API_SECRET"):
        os.environ.pop(key, None)

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--months", type=float, default=36.0)
    ap.add_argument("--starting-equity", type=float, default=15.0)
    ap.add_argument("--cost-bps-side", type=float, default=7.0)
    ap.add_argument("--stress-cost-bps-side", type=float, default=10.0)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    print(f"=== frozen 1H/4H/1D validation: {a.months:g} months, "
          f"${a.starting_equity:g} wallet ===")
    print("    one position, DCA=0, $10 notional, next-hour-open fills")
    print(f"    cost={a.cost_bps_side:g} bps/side; funding included exactly")
    candles, funding = fetch_inputs(symbols, a.months)
    now = time.time()
    candles = {symbol: [c for c in rows if c.ts + 3600.0 <= now]
               for symbol, rows in candles.items()}
    grid, _ = align_candles(candles)
    base = HTFParams(cost_bps_per_side=a.cost_bps_side)
    warm = max(base.daily_slow * 24, base.four_hour_slow * 4,
               base.hourly_ema, base.atr_period, base.volume_lookback) + 48
    if len(grid) < warm + 24 * 365:
        raise SystemExit(f"only {len(grid)} aligned hourly bars; insufficient history")
    minimums, steps = fetch_execution_filters(symbols)
    n = len(grid)
    hold_start = warm + int((n - warm) * 0.60)

    primary = evaluate(candles, funding, minimums, steps, base,
                       a.starting_equity, warm, n)
    holdout = evaluate(candles, funding, minimums, steps, base,
                       a.starting_equity, hold_start, n)
    print("\nPRIMARY fixed before data")
    print("  all data  " + fmt(primary))
    print("  FINAL 40% " + fmt(holdout))
    print(f"  exits      {holdout['exit_reasons']}")
    print("  blocks     exposure={exposure_blocks} daily-loss={daily_loss_blocks} "
          "daily-target={daily_target_blocks} min-notional={blocked_min_notional}"
          .format(**holdout))
    print("  lock hits  loss={daily_loss_lock_hits} target={daily_target_lock_hits}"
          .format(**holdout))

    boundaries = np.linspace(warm, n, 5, dtype=int)
    periods = []
    print("\nFOUR CHRONOLOGICAL PERIODS")
    for i, (lo, hi) in enumerate(zip(boundaries[:-1], boundaries[1:]), 1):
        st = evaluate(candles, funding, minimums, steps, base,
                      a.starting_equity, int(lo), int(hi))
        periods.append(st)
        print(f"  period {i}/4 " + fmt(st))

    variants = {
        "ema_12_48": replace(base, four_hour_fast=12, four_hour_slow=48),
        "ema_24_72": replace(base, four_hour_fast=24, four_hour_slow=72),
        "rr_tighter": replace(base, stop_atr=1.25, target_atr=2.50),
        "rr_wider": replace(base, stop_atr=1.75, target_atr=3.50),
    }
    neighbors = {}
    print("\nNEIGHBOUR ROBUSTNESS ON THE SAME FINAL 40%")
    for name, params in variants.items():
        st = evaluate(candles, funding, minimums, steps, params,
                      a.starting_equity, hold_start, n)
        neighbors[name] = st
        print(f"  {name:12s} " + fmt(st))

    stress = evaluate(candles, funding, minimums, steps,
                      replace(base, cost_bps_per_side=a.stress_cost_bps_side),
                      a.starting_equity, hold_start, n)
    print(f"\nCOST STRESS {a.stress_cost_bps_side:g} bps/side")
    print("  FINAL 40% " + fmt(stress))

    positive_periods = sum(st["net_pnl"] > 0 for st in periods)
    positive_neighbors = sum(st["net_pnl"] > 0
                             for st in [holdout, *neighbors.values()])
    checks = {
        "holdout_fee_net_positive": holdout["net_pnl"] > 0,
        "holdout_profit_factor_at_least_1_15": holdout["profit_factor"] >= 1.15,
        "holdout_at_least_30_trades": holdout["trades"] >= 30,
        "holdout_positive_expectancy": holdout["expectancy_usd"] > 0,
        "average_winner_at_least_5_cents": holdout["avg_win"] >= 0.05,
        "candidate_frequency_0_1_to_2_per_day": (
            0.1 <= holdout["candidate_signals_per_day"] <= 2.0),
        "at_least_3_of_4_periods_positive": positive_periods >= 3,
        "at_least_3_of_5_neighbor_rules_positive": positive_neighbors >= 3,
        "positive_at_stressed_cost": stress["net_pnl"] > 0,
        "holdout_drawdown_below_20pct": holdout["max_drawdown_pct"] < 20.0,
        "estimated_monthly_pnl_at_least_15_cents": (
            holdout["estimated_monthly_pnl"] >= 0.15),
    }
    passed = all(checks.values())
    print("\nADMISSION HURDLES")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s} {name}")
    print("\n  " + ("PASS: frozen paper-only runner may be built."
                     if passed else "REJECTED: do not deploy this candidate."))

    payload = {
        "symbols": symbols, "months": a.months, "params": asdict(base),
        "primary": primary, "holdout": holdout, "periods": periods,
        "neighbors": neighbors, "stress": stress, "checks": checks,
        "passed": passed, "live_orders_sent": 0,
    }
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        print(f"  wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
