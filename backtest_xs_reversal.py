#!/usr/bin/env python3
"""Frozen, fee-aware validation of 24-hour cross-sectional reversal.

Public Binance data only.  Binance credentials are removed at startup, no
order-capable module is imported, and the sealed final 25% is evaluated only
when every development hurdle passes.
"""
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
from xs_reversal import (ReversalParams, align_candles,
                         choose_reversal_pair, return_vector, run, stats)


DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"


def funding_by_timestamp(rows):
    return {int(round(ts)): float(rate) for ts, rate in rows}


def fetch_inputs(symbols, months):
    candles, funding = {}, {}

    def one(symbol):
        bars = backtest_breakout.fetch(symbol, "1h", months, verbose=False)
        rates = fetch_symbol(symbol, months, pause=0.03)
        return symbol, bars, funding_by_timestamp(rates), len(rates)

    with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
        jobs = {pool.submit(one, symbol): symbol for symbol in symbols}
        for job in as_completed(jobs):
            symbol, bars, rates, count = job.result()
            candles[symbol] = bars
            funding[symbol] = rates
            print(f"  {symbol}: {len(bars)} hourly bars, {count} funding prints")
    return candles, funding


def evaluate(candles, funding, minimums, steps, params, equity, start, end):
    sim = run(candles, params, starting_equity=equity, trade_start=start,
              trade_end=end, funding_bps_by_ts=funding,
              min_notional_by_symbol=minimums,
              qty_step_by_symbol=steps)
    result = stats(sim, equity)
    result["exit_reasons"] = dict(sorted(Counter(t.reason for t in sim.trades).items()))
    result["candidate_days"] = sim.candidate_days
    result["dispersion_blocks"] = sim.dispersion_blocks
    result["exposure_blocks"] = sim.exposure_blocks
    result["daily_loss_blocks"] = sim.daily_loss_blocks
    result["daily_target_blocks"] = sim.daily_target_blocks
    result["daily_loss_lock_hits"] = sim.daily_loss_lock_hits
    result["daily_target_lock_hits"] = sim.daily_target_lock_hits
    result["blocked_min_notional"] = sim.blocked_min_notional
    return result, sim


def _t_stat(values):
    arr = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if len(arr) < 2 or arr.std(ddof=1) <= 0:
        return 0.0
    return float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr))))


def _sign_flip_p(values, null_runs=1000):
    arr = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if not len(arr):
        return 1.0
    observed = float(arr.mean())
    rng = np.random.default_rng(20260903)
    exceed = 0
    for _ in range(null_runs):
        signs = rng.choice((-1.0, 1.0), size=len(arr))
        exceed += float(np.mean(arr * signs)) >= observed
    return float((exceed + 1) / (null_runs + 1))


def diagnostics(candles, params, start, end, null_runs=500):
    grid, aligned = align_candles(candles)
    symbols = sorted(aligned)
    gross_spreads, market_returns, rank_rows, forward_rows = [], [], [], []
    dispersion_candidates = 0
    first = max(start, params.lookback_hours)
    for i in range(first, end):
        if int(grid[i] // 3600) % 24 != params.signal_hour_utc:
            continue
        entry_i = i + 1
        exit_i = entry_i + params.max_hold_hours
        if exit_i >= end:
            continue
        values = return_vector(aligned, i, params.lookback_hours)
        signal = choose_reversal_pair(values, params.min_dispersion)
        if signal is None:
            continue
        dispersion_candidates += 1
        ranks = np.asarray([values[symbol] for symbol in symbols], dtype=float)
        forward = np.asarray([
            aligned[symbol][exit_i].open / aligned[symbol][entry_i].open - 1.0
            for symbol in symbols
        ], dtype=float)
        long_i = symbols.index(signal.long_symbol)
        short_i = symbols.index(signal.short_symbol)
        gross_spreads.append(0.5 * (forward[long_i] - forward[short_i]))
        market_returns.append(float(np.mean(forward)))
        rank_rows.append(ranks)
        forward_rows.append(forward)

    gross = np.asarray(gross_spreads, dtype=float)
    cost_return = 2.0 * params.cost_bps_per_side / 1e4
    net_before_funding = gross - cost_return
    actual_mean = float(gross.mean()) if len(gross) else 0.0
    rng = np.random.default_rng(20260903)
    null_means = []
    for _ in range(null_runs):
        shuffled_spreads = []
        for ranks, forward in zip(rank_rows, forward_rows):
            shuffled = ranks.copy()
            rng.shuffle(shuffled)
            long_i, short_i = int(np.argmin(shuffled)), int(np.argmax(shuffled))
            shuffled_spreads.append(0.5 * (forward[long_i] - forward[short_i]))
        null_means.append(float(np.mean(shuffled_spreads)) if shuffled_spreads else 0.0)
    shuffle_p = ((sum(value >= actual_mean for value in null_means) + 1)
                 / (len(null_means) + 1)) if null_means else 1.0
    beta = CS.book_beta(gross, np.asarray(market_returns, dtype=float))
    return {
        "observations": int(len(gross)),
        "gross_reversal_mean_bps": float(gross.mean() * 1e4) if len(gross) else 0.0,
        "gross_reversal_median_bps": float(np.median(gross) * 1e4) if len(gross) else 0.0,
        "gross_reversal_t": _t_stat(gross),
        "net_before_funding_mean_bps": (float(net_before_funding.mean() * 1e4)
                                         if len(net_before_funding) else 0.0),
        "positive_net_before_funding_pct": (float(np.mean(net_before_funding > 0) * 100.0)
                                             if len(net_before_funding) else 0.0),
        "shuffle_p_value": shuffle_p,
        "null_mean_95pct_bps": (float(np.percentile(null_means, 95) * 1e4)
                                 if null_means else 0.0),
        "pair_beta_to_equal_weight_market": (float(beta) if np.isfinite(beta) else None),
        "dispersion_candidates": dispersion_candidates,
    }


def fmt(result):
    return (f"n={int(result['trades']):4d} win={result['win_rate']:5.1f}% "
            f"gross=${result['gross_price_pnl']:+7.3f} fee=${result['fees']:.3f} "
            f"fund=${result['funding']:+.3f} net=${result['net_pnl']:+7.3f} "
            f"final=${result['final_equity']:6.3f} PF={result['profit_factor']:4.2f} "
            f"DD={result['max_drawdown_pct']:5.1f}% avgW=${result['avg_win']:+.3f} "
            f"avgL=${result['avg_loss']:+.3f} tr/d={result['trades_per_day']:.3f} "
            f"est/mo=${result['estimated_monthly_pnl']:+.3f}")


def main(argv=None):
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "API_KEY", "API_SECRET"):
        os.environ.pop(key, None)

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--months", type=float, default=36.0)
    parser.add_argument("--starting-equity", type=float, default=15.0)
    parser.add_argument("--cost-bps-side", type=float, default=7.0)
    parser.add_argument("--stress-cost-bps-side", type=float, default=10.0)
    parser.add_argument("--null-runs", type=int, default=500)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",")
               if symbol.strip()]

    print(f"=== frozen 24h cross-sectional reversal: {args.months:g} months ===")
    print("    previous 24h winner SHORT + loser LONG; $5+$5; one pair; DCA=0")
    print("    signal on completed 23:00 UTC bar; next-hour open; max hold 24h")
    print("    dispersion >=3%; pair stop -$0.30; target +$0.50")
    print(f"    cost={args.cost_bps_side:g} bps/side/leg; published funding included")
    candles, funding = fetch_inputs(symbols, args.months)
    now = time.time()
    candles = {symbol: [bar for bar in bars if bar.ts + 3600.0 <= now]
               for symbol, bars in candles.items()}
    grid, _ = align_candles(candles)
    base = ReversalParams(cost_bps_per_side=args.cost_bps_side)
    warm = base.lookback_hours + 48
    if len(grid) < warm + 365 * 24:
        raise SystemExit(f"only {len(grid)} aligned completed hourly bars")
    minimums, steps = fetch_execution_filters(symbols)
    n = len(grid)
    final_start = warm + int((n - warm) * 0.75)

    development, dev_sim = evaluate(
        candles, funding, minimums, steps, base, args.starting_equity,
        warm, final_start)
    diag = diagnostics(candles, base, warm, final_start, args.null_runs)
    print("\nDEVELOPMENT 75% — PRIMARY RULE FIXED BEFORE DATA")
    print("  " + fmt(development))
    print(f"  exits={development['exit_reasons']}")
    print("  blocks dispersion={dispersion_blocks} exposure={exposure_blocks} "
          "loss={daily_loss_blocks} target={daily_target_blocks} "
          "min-notional={blocked_min_notional}".format(**development))
    print("\nFORWARD-RETURN DIAGNOSTICS — DEVELOPMENT ONLY")
    print(f"  observations={diag['observations']} gross mean="
          f"{diag['gross_reversal_mean_bps']:+.2f}bps median="
          f"{diag['gross_reversal_median_bps']:+.2f}bps "
          f"net-before-funding={diag['net_before_funding_mean_bps']:+.2f}bps")
    print(f"  t={diag['gross_reversal_t']:+.2f} shuffle p={diag['shuffle_p_value']:.3f} "
          f"positive-net={diag['positive_net_before_funding_pct']:.1f}% "
          f"beta={diag['pair_beta_to_equal_weight_market']}")

    boundaries = np.linspace(warm, final_start, 4, dtype=int)
    folds = []
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for number, (lo, hi) in enumerate(zip(boundaries[:-1], boundaries[1:]), 1):
        result, _ = evaluate(candles, funding, minimums, steps, base,
                             args.starting_equity, int(lo), int(hi))
        folds.append(result)
        print(f"  fold {number}/3 " + fmt(result))

    variants = {
        "dispersion_2pct": replace(base, min_dispersion=0.02),
        "dispersion_4pct": replace(base, min_dispersion=0.04),
        "lookback_12h": replace(base, lookback_hours=12),
        "lookback_48h": replace(base, lookback_hours=48),
    }
    neighbors = {}
    print("\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY")
    for name, params in variants.items():
        result, _ = evaluate(candles, funding, minimums, steps, params,
                             args.starting_equity, warm, final_start)
        neighbors[name] = result
        print(f"  {name:17s} " + fmt(result))

    stress_params = replace(base, cost_bps_per_side=args.stress_cost_bps_side)
    stress, _ = evaluate(candles, funding, minimums, steps, stress_params,
                         args.starting_equity, warm, final_start)
    trade_p = _sign_flip_p([trade.net_pnl for trade in dev_sim.trades])
    print(f"\nCOST STRESS {args.stress_cost_bps_side:g} bps/side/leg")
    print("  " + fmt(stress))
    print(f"  trade sign-flip p={trade_p:.3f}")

    positive_folds = sum(result["net_pnl"] > 0 for result in folds)
    positive_rules = sum(result["net_pnl"] > 0
                         for result in [development, *neighbors.values()])
    beta = diag["pair_beta_to_equal_weight_market"]
    dev_checks = {
        "development_fee_net_positive": development["net_pnl"] > 0,
        "development_profit_factor_at_least_1_15": development["profit_factor"] >= 1.15,
        "development_at_least_100_trades": development["trades"] >= 100,
        "average_winner_at_least_3_cents": development["avg_win"] >= 0.03,
        "at_least_2_of_3_folds_positive": positive_folds >= 2,
        "at_least_3_of_5_neighbor_rules_positive": positive_rules >= 3,
        "positive_at_stressed_cost": stress["net_pnl"] > 0,
        "development_drawdown_below_20pct": development["max_drawdown_pct"] < 20.0,
        "estimated_monthly_pnl_at_least_15_cents": development["estimated_monthly_pnl"] >= 0.15,
        "gross_reversal_edge_exceeds_14bps_cost": diag["net_before_funding_mean_bps"] > 0,
        "gross_reversal_t_at_least_2": diag["gross_reversal_t"] >= 2.0,
        "shuffle_p_value_at_most_0_10": diag["shuffle_p_value"] <= 0.10,
        "trade_sign_flip_p_value_at_most_0_10": trade_p <= 0.10,
        "absolute_market_beta_at_most_0_20": beta is not None and abs(beta) <= 0.20,
    }
    development_passed = all(dev_checks.values())
    print("\nDEVELOPMENT ADMISSION HURDLES")
    for name, passed in dev_checks.items():
        print(f"  {'PASS' if passed else 'FAIL':4s} {name}")

    final = None
    final_stress = None
    final_halves = []
    final_checks = {}
    if development_passed:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        final, _ = evaluate(candles, funding, minimums, steps, base,
                            args.starting_equity, final_start, n)
        final_stress, _ = evaluate(candles, funding, minimums, steps,
                                   stress_params, args.starting_equity,
                                   final_start, n)
        midpoint = final_start + (n - final_start) // 2
        for lo, hi in ((final_start, midpoint), (midpoint, n)):
            result, _ = evaluate(candles, funding, minimums, steps, base,
                                 args.starting_equity, lo, hi)
            final_halves.append(result)
        final_checks = {
            "final_fee_net_positive": final["net_pnl"] > 0,
            "final_profit_factor_at_least_1_15": final["profit_factor"] >= 1.15,
            "final_at_least_30_trades": final["trades"] >= 30,
            "both_final_halves_positive": all(x["net_pnl"] > 0 for x in final_halves),
            "final_positive_at_stressed_cost": final_stress["net_pnl"] > 0,
            "final_drawdown_below_20pct": final["max_drawdown_pct"] < 20.0,
            "final_average_winner_at_least_3_cents": final["avg_win"] >= 0.03,
            "final_estimated_monthly_pnl_at_least_15_cents": final["estimated_monthly_pnl"] >= 0.15,
        }
        print("  FINAL 25% " + fmt(final))
        print("  STRESS    " + fmt(final_stress))
        for number, result in enumerate(final_halves, 1):
            print(f"  half {number}    " + fmt(result))
        for name, passed in final_checks.items():
            print(f"  {'PASS' if passed else 'FAIL':4s} {name}")
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")

    passed = development_passed and all(final_checks.values())
    print("\n  " + ("PASS: eligible for a separate paper-only run."
                     if passed else "REJECTED: no paper or live deployment."))
    payload = {
        "schema": 1,
        "strategy": "xs_reversal_24h",
        "symbols": symbols,
        "months": args.months,
        "params": asdict(base),
        "development": development,
        "diagnostics": diag,
        "development_folds": folds,
        "neighbors": neighbors,
        "stress": stress,
        "trade_sign_flip_p_value": trade_p,
        "development_checks": dev_checks,
        "development_passed": development_passed,
        "sealed_final_status": "opened" if development_passed else "unopened",
        "final": final,
        "final_stress": final_stress,
        "final_halves": final_halves,
        "final_checks": final_checks,
        "passed": passed,
        "simulation_only": True,
        "live_orders_sent": 0,
        "funding_note": "published rates with hourly-open notional proxy; entry-timestamp settlement excluded",
    }
    print("[xs-reversal-summary] " + json.dumps(payload, sort_keys=True))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print(f"  wrote {args.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
