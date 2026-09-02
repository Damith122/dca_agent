#!/usr/bin/env python3
"""Chronological validation for the fixed 15m/1h intraday candidate."""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace

import numpy as np

import backtest_breakout
from fetch_funding_universe import fetch_symbol
from intraday import IntradayParams, align_candles, run, stats


def _save_candles(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in rows:
            writer.writerow([c.ts, c.open, c.high, c.low, c.close, c.volume])


def _load_funding(path):
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].replace(".", "", 1).isdigit():
                continue
            out[int(float(row[0]))] = float(row[1])
    return out


def _save_funding(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["funding_time", "rate_bps"])
        writer.writerows(sorted(rows.items()))


def fetch_inputs(symbols, months, cache=None):
    candles, funding = {}, {}

    def one(symbol):
        candle_path = os.path.join(cache, "15m", f"{symbol}.csv") if cache else ""
        funding_path = os.path.join(cache, "funding", f"{symbol}.csv") if cache else ""
        if cache and os.path.exists(candle_path) and os.path.exists(funding_path):
            return (symbol, backtest_breakout.load_csv(os.path.dirname(candle_path), symbol),
                    _load_funding(funding_path))
        rows = backtest_breakout.fetch(symbol, "15m", months, verbose=False)
        rates = fetch_symbol(symbol, months, pause=0.03)
        mapped = {int(ts): float(rate) for ts, rate in rates}
        if cache:
            _save_candles(candle_path, rows)
            _save_funding(funding_path, mapped)
        return symbol, rows, mapped

    with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
        jobs = {pool.submit(one, symbol): symbol for symbol in symbols}
        for job in as_completed(jobs):
            symbol, rows, rates = job.result()
            candles[symbol] = rows
            funding[symbol] = rates
            print(f"  {symbol}: {len(rows)} 15m bars, {len(rates)} funding prints")
    return candles, funding


def fmt(st):
    return (f"n={int(st['trades']):4d} win={st['win_rate']:5.1f}% "
            f"net=${st['net_pnl']:+7.3f} PF={st['profit_factor']:4.2f} "
            f"DD={st['max_drawdown_pct']:5.1f}% "
            f"avgW=${st['avg_win']:+.3f} avgL=${st['avg_loss']:+.3f} "
            f"signals/day={st['candidate_signals_per_day']:4.1f} "
            f"trades/day={st['trades_per_day']:4.2f}")


def evaluate(candles, funding, p, starting_equity, start, end):
    sim = run(candles, p, starting_equity=starting_equity,
              funding_bps_by_ts=funding, trade_start=start, trade_end=end)
    st = stats(sim, starting_equity)
    st["exit_reasons"] = dict(sorted({r: sum(t.reason == r for t in sim.trades)
                                      for r in {t.reason for t in sim.trades}}.items()))
    st["final_equity"] = sim.final_equity
    return st


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT")
    ap.add_argument("--months", type=float, default=12.0)
    ap.add_argument("--starting-equity", type=float, default=15.0)
    ap.add_argument("--cost-bps-side", type=float, default=7.0)
    ap.add_argument("--stress-cost-bps-side", type=float, default=10.0)
    ap.add_argument("--cache", default=None,
                    help="reuse/write public candles and funding under this directory")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    print(f"=== fixed intraday validation: {a.months:g} months, {len(symbols)} symbols ===")
    candles, funding = fetch_inputs(symbols, a.months, a.cache)
    # The public endpoint includes today's still-forming candle. It is never
    # allowed into a historical signal or fill.
    now = time.time()
    candles = {s: [c for c in rows if c.ts + 900 <= now] for s, rows in candles.items()}
    grid, _ = align_candles(candles)
    base = IntradayParams(cost_bps_per_side=a.cost_bps_side)
    warm = max(base.channel, base.atr_period, base.volume_lookback,
               base.momentum_bars, base.hourly_ema_slow * 4) + 8
    if len(grid) < warm + 3000:
        raise SystemExit(f"only {len(grid)} aligned bars; need at least {warm + 3000}")
    hold_start = warm + int((len(grid) - warm) * 0.60)

    results = {}
    results["full"] = evaluate(candles, funding, base, a.starting_equity, warm, len(grid))
    results["holdout"] = evaluate(candles, funding, base, a.starting_equity,
                                  hold_start, len(grid))
    print("\nPRIMARY fixed before evaluation")
    print("  all data  " + fmt(results["full"]))
    print("  FINAL 40% " + fmt(results["holdout"]))
    print(f"  exits      {results['holdout']['exit_reasons']}")

    boundaries = np.linspace(warm, len(grid), 5, dtype=int)
    periods = []
    for i, (lo, hi) in enumerate(zip(boundaries[:-1], boundaries[1:]), 1):
        st = evaluate(candles, funding, base, a.starting_equity, int(lo), int(hi))
        periods.append(st)
        print(f"  period {i}/4 " + fmt(st))
    results["periods"] = periods

    variants = {
        "channel_8": replace(base, channel=8),
        "channel_20": replace(base, channel=20),
        "volume_1_0": replace(base, volume_ratio=1.0),
        "volume_1_4": replace(base, volume_ratio=1.4),
    }
    neighbors = {}
    print("\nROBUSTNESS on the same untouched 40% (not used to retune primary)")
    for name, p in variants.items():
        neighbors[name] = evaluate(candles, funding, p, a.starting_equity,
                                   hold_start, len(grid))
        print(f"  {name:12s} " + fmt(neighbors[name]))
    results["neighbors"] = neighbors

    stress = replace(base, cost_bps_per_side=a.stress_cost_bps_side)
    stress_st = evaluate(candles, funding, stress, a.starting_equity,
                         hold_start, len(grid))
    results["stress"] = stress_st
    print(f"\nCOST STRESS {a.stress_cost_bps_side:g} bps/side")
    print("  FINAL 40% " + fmt(stress_st))

    h = results["holdout"]
    positive_periods = sum(x["net_pnl"] > 0 for x in periods)
    positive_neighbors = sum(x["net_pnl"] > 0 for x in neighbors.values())
    checks = {
        "holdout_fee_net_positive": h["net_pnl"] > 0,
        "holdout_profit_factor_at_least_1_15": h["profit_factor"] >= 1.15,
        "holdout_at_least_100_trades": h["trades"] >= 100,
        "candidate_signals_5_to_15_per_day": 5 <= h["candidate_signals_per_day"] <= 15,
        "executed_trades_0_5_to_3_per_day": .5 <= h["trades_per_day"] <= 3,
        "average_winner_at_least_3_cents": h["avg_win"] >= .03,
        "at_least_3_of_4_periods_positive": positive_periods >= 3,
        "at_least_3_of_4_neighbors_positive": positive_neighbors >= 3,
        "positive_at_stressed_cost": stress_st["net_pnl"] > 0,
        "holdout_drawdown_below_20pct": h["max_drawdown_pct"] < 20,
        "holdout_equity_above_zero": h["final_equity"] > 0,
    }
    passed = all(checks.values())
    print("\nADMISSION HURDLES")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s} {name}")
    print("\n  " + ("PASS: paper-only runner may be built."
                     if passed else "REJECTED: do not deploy this candidate."))

    payload = {"symbols": symbols, "months": a.months, "params": asdict(base),
               "results": results, "checks": checks, "passed": passed}
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"  wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
