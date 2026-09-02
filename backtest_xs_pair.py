#!/usr/bin/env python3
"""Frozen 36-month validation for the $15 relative-strength pair."""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace

import numpy as np

import backtest_breakout
import cross_sectional as CS
from fetch_funding_universe import fetch_symbol
from paper_tsmom import fetch_execution_filters
from xs_pair import (PairParams, align_candles, choose_pair, run,
                     score_vector, stats)


DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"


def funding_by_day(rows):
    out = {}
    for ts, rate in rows:
        day = int(ts // 86400 * 86400)
        out[day] = out.get(day, 0.0) + float(rate)
    return out


def fetch_inputs(symbols, months):
    candles, funding = {}, {}

    def one(symbol):
        rows = backtest_breakout.fetch(symbol, "1d", months, verbose=False)
        rates = fetch_symbol(symbol, months, pause=0.03)
        return symbol, rows, funding_by_day(rates), len(rates)

    with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
        jobs = {pool.submit(one, symbol): symbol for symbol in symbols}
        for job in as_completed(jobs):
            symbol, rows, rates, count = job.result()
            candles[symbol] = rows
            funding[symbol] = rates
            print(f"  {symbol}: {len(rows)} daily bars, {count} funding prints")
    return candles, funding


def evaluate(candles, funding, minimums, steps, p, equity, start, end):
    sim = run(candles, p, starting_equity=equity, trade_start=start,
              trade_end=end, funding_bps_by_day=funding,
              min_notional_by_symbol=minimums, qty_step_by_symbol=steps)
    st = stats(sim, equity)
    st["exit_reasons"] = dict(sorted(Counter(t.reason for t in sim.trades).items()))
    st["held_pair_blocks"] = sim.held_pair_blocks
    st["daily_loss_blocks"] = sim.daily_loss_blocks
    st["daily_target_blocks"] = sim.daily_target_blocks
    st["daily_loss_lock_hits"] = sim.daily_loss_lock_hits
    st["daily_target_lock_hits"] = sim.daily_target_lock_hits
    st["blocked_min_notional"] = sim.blocked_min_notional
    return st


def _t_stat(values):
    arr = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    if len(arr) < 2 or arr.std(ddof=1) <= 0:
        return 0.0
    return float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr))))


def diagnostics(candles, p, start, end, null_runs=100):
    grid, aligned = align_candles(candles)
    symbols = sorted(aligned)
    ics, signal_rows, forward_rows = [], [], []
    pair_returns, market_returns = [], []
    for i in range(max(start, p.lookback_days + p.skip_days),
                   end - p.rebalance_days):
        day = int(grid[i] // 86400)
        if day % p.rebalance_days != p.rebalance_offset % p.rebalance_days:
            continue
        scores = score_vector(aligned, i, p)
        signal = choose_pair(scores)
        if signal is None:
            continue
        score_row = np.array([scores.get(symbol, np.nan) for symbol in symbols])
        forward = np.array([
            aligned[symbol][i + p.rebalance_days].close / aligned[symbol][i].close - 1.0
            for symbol in symbols
        ])
        ics.append(CS.spearman_ic(score_row, forward))
        signal_rows.append(score_row)
        forward_rows.append(forward)
        long_i, short_i = symbols.index(signal.long_symbol), symbols.index(signal.short_symbol)
        pair_returns.append(0.5 * forward[long_i] - 0.5 * forward[short_i])
        market_returns.append(float(np.nanmean(forward)))

    actual_t = _t_stat(ics)
    rng = np.random.default_rng(20260902)
    null_t = []
    for _ in range(null_runs):
        shuffled = []
        for score_row, forward in zip(signal_rows, forward_rows):
            vals = score_row.copy()
            rng.shuffle(vals)
            shuffled.append(CS.spearman_ic(vals, forward))
        null_t.append(_t_stat(shuffled))
    p_value = ((sum(value >= actual_t for value in null_t) + 1)
               / (len(null_t) + 1)) if null_t else 1.0

    prices = np.array([[aligned[symbol][i].close for symbol in symbols]
                       for i in range(start, end)], dtype=float)
    returns = np.diff(np.log(prices), axis=0) if len(prices) > 1 else np.empty((0, len(symbols)))
    breadth = CS.effective_breadth(returns)
    beta = CS.book_beta(np.asarray(pair_returns), np.asarray(market_returns))
    good = np.asarray([x for x in ics if np.isfinite(x)], dtype=float)
    return {
        "observations": len(good),
        "mean_ic": float(good.mean()) if len(good) else 0.0,
        "ic_t": actual_t,
        "shuffle_p_value": p_value,
        "null_t_median": float(np.median(null_t)) if null_t else 0.0,
        "null_t_95pct": float(np.percentile(null_t, 95)) if null_t else 0.0,
        "effective_breadth": float(breadth) if np.isfinite(breadth) else None,
        "pair_beta_to_equal_weight_market": float(beta) if np.isfinite(beta) else None,
    }


def fmt(st):
    return (f"n={int(st['trades']):3d} win={st['win_rate']:5.1f}% "
            f"net=${st['net_pnl']:+7.3f} final=${st['final_equity']:6.3f} "
            f"PF={st['profit_factor']:4.2f} DD={st['max_drawdown_pct']:5.1f}% "
            f"avgW=${st['avg_win']:+.3f} avgL=${st['avg_loss']:+.3f} "
            f"pairs/d={st['candidate_pairs_per_day']:.3f} "
            f"tr/d={st['trades_per_day']:.3f} "
            f"est/mo=${st['estimated_monthly_pnl']:+.3f}")


def main(argv=None):
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "API_KEY", "API_SECRET"):
        os.environ.pop(key, None)

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--months", type=float, default=36.0)
    ap.add_argument("--starting-equity", type=float, default=15.0)
    ap.add_argument("--cost-bps-side", type=float, default=7.0)
    ap.add_argument("--stress-cost-bps-side", type=float, default=10.0)
    ap.add_argument("--null-runs", type=int, default=100)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    symbols = [symbol.strip().upper() for symbol in a.symbols.split(",") if symbol.strip()]

    print(f"=== frozen relative-strength pair: {a.months:g} months, ${a.starting_equity:g} wallet ===")
    print("    strongest $5 LONG + weakest $5 SHORT; one pair; DCA=0")
    print("    30d volatility-adjusted momentum, skip 3d, weekly next-open rebalance")
    print(f"    cost={a.cost_bps_side:g} bps/side/leg; exact funding included")
    candles, funding = fetch_inputs(symbols, a.months)
    now = time.time()
    candles = {symbol: [bar for bar in rows if bar.ts + 86400.0 <= now]
               for symbol, rows in candles.items()}
    grid, _ = align_candles(candles)
    base = PairParams(cost_bps_per_side=a.cost_bps_side)
    warm = base.lookback_days + base.skip_days + 14
    if len(grid) < warm + 365:
        raise SystemExit(f"only {len(grid)} aligned completed daily bars")
    minimums, steps = fetch_execution_filters(symbols)
    n = len(grid)
    hold_start = warm + int((n - warm) * 0.60)

    full = evaluate(candles, funding, minimums, steps, base,
                    a.starting_equity, warm, n)
    holdout = evaluate(candles, funding, minimums, steps, base,
                       a.starting_equity, hold_start, n)
    diag = diagnostics(candles, base, hold_start, n, a.null_runs)
    print("\nPRIMARY fixed before data")
    print("  all data  " + fmt(full))
    print("  FINAL 40% " + fmt(holdout))
    print(f"  exits      {holdout['exit_reasons']}")
    print("  blocks     held={held_pair_blocks} loss={daily_loss_blocks} "
          "target={daily_target_blocks} min-notional={blocked_min_notional}"
          .format(**holdout))
    print("\nFORECAST DIAGNOSTICS ON FINAL 40%")
    print(f"  observations={diag['observations']} mean IC={diag['mean_ic']:+.4f} "
          f"IC t={diag['ic_t']:+.2f} shuffle p={diag['shuffle_p_value']:.3f}")
    print(f"  null t median={diag['null_t_median']:+.2f} "
          f"95th={diag['null_t_95pct']:+.2f} breadth={diag['effective_breadth']} "
          f"beta={diag['pair_beta_to_equal_weight_market']}")

    boundaries = np.linspace(warm, n, 5, dtype=int)
    periods = []
    print("\nFOUR CHRONOLOGICAL PERIODS")
    for number, (lo, hi) in enumerate(zip(boundaries[:-1], boundaries[1:]), 1):
        st = evaluate(candles, funding, minimums, steps, base,
                      a.starting_equity, int(lo), int(hi))
        periods.append(st)
        print(f"  period {number}/4 " + fmt(st))

    variants = {
        "lookback_20": replace(base, lookback_days=20, skip_days=2),
        "lookback_60": replace(base, lookback_days=60, skip_days=5),
        "rebalance_14": replace(base, rebalance_days=14),
        "rank_buffer_1": replace(base, rank_buffer=1),
    }
    neighbors = {}
    print("\nNEIGHBOUR ROBUSTNESS ON FINAL 40%")
    for name, params in variants.items():
        st = evaluate(candles, funding, minimums, steps, params,
                      a.starting_equity, hold_start, n)
        neighbors[name] = st
        print(f"  {name:14s} " + fmt(st))

    stress = evaluate(candles, funding, minimums, steps,
                      replace(base, cost_bps_per_side=a.stress_cost_bps_side),
                      a.starting_equity, hold_start, n)
    print(f"\nCOST STRESS {a.stress_cost_bps_side:g} bps/side/leg")
    print("  FINAL 40% " + fmt(stress))

    positive_periods = sum(st["net_pnl"] > 0 for st in periods)
    positive_neighbors = sum(st["net_pnl"] > 0
                             for st in [holdout, *neighbors.values()])
    beta = diag["pair_beta_to_equal_weight_market"]
    breadth = diag["effective_breadth"]
    checks = {
        "holdout_fee_net_positive": holdout["net_pnl"] > 0,
        "holdout_profit_factor_at_least_1_15": holdout["profit_factor"] >= 1.15,
        "holdout_at_least_20_pair_trades": holdout["trades"] >= 20,
        "average_winner_at_least_5_cents": holdout["avg_win"] >= 0.05,
        "weekly_candidate_frequency": 0.08 <= holdout["candidate_pairs_per_day"] <= 0.25,
        "at_least_3_of_4_periods_positive": positive_periods >= 3,
        "at_least_3_of_5_neighbor_rules_positive": positive_neighbors >= 3,
        "positive_at_stressed_cost": stress["net_pnl"] > 0,
        "holdout_drawdown_below_20pct": holdout["max_drawdown_pct"] < 20.0,
        "estimated_monthly_pnl_at_least_15_cents": holdout["estimated_monthly_pnl"] >= 0.15,
        "positive_significant_information_coefficient": (
            diag["mean_ic"] > 0 and diag["ic_t"] >= 2.0),
        "shuffle_p_value_at_most_0_05": diag["shuffle_p_value"] <= 0.05,
        "effective_breadth_at_least_2": breadth is not None and breadth >= 2.0,
        "absolute_market_beta_at_most_0_20": beta is not None and abs(beta) <= 0.20,
    }
    passed = all(checks.values())
    print("\nADMISSION HURDLES")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s} {name}")
    print("\n  " + ("PASS: paper-only pair runner may be built."
                     if passed else "REJECTED: do not deploy this candidate."))

    payload = {
        "symbols": symbols, "months": a.months, "params": asdict(base),
        "full": full, "holdout": holdout, "diagnostics": diag,
        "periods": periods, "neighbors": neighbors, "stress": stress,
        "checks": checks, "passed": passed, "simulation_only": True,
        "live_orders_sent": 0,
    }
    print("[xs-pair-summary] " + json.dumps(payload, sort_keys=True))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print(f"  wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
