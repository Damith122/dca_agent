#!/usr/bin/env python3
"""Sealed $15 validation for a multi-horizon, funding-aware TSMOM rule.

The original 30-day long/cash candidate is the only strategy family in this
repository with robust positive historical evidence.  This study tests a
precommitted robustness extension: require 30-day and 90-day trends to agree
and avoid longs whose last three settled funding days sum above nine basis
points.  It uses public data only and imports no order client.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import Counter
from dataclasses import asdict, replace

import numpy as np

from backtest_tsmom import fetch_inputs
from breakout import atr_series
from paper_tsmom import fetch_execution_filters
from tsmom import TSMOMParams, align_candles, choose_target, run, stats


DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"


def evaluate(candles, funding, minimums, steps, params, equity, start, end):
    sim = run(candles, funding, params, starting_equity=equity,
              trade_start=start, trade_end=end,
              min_notional_by_symbol=minimums,
              qty_step_by_symbol=steps,
              min_notional_max_risk_pct=0.03)
    result = stats(sim, equity)
    nets = np.asarray([trade.net_pnl for trade in sim.trades], dtype=float)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    result.update({
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "net_pnl": sim.final_equity - equity,
        "final_equity": sim.final_equity,
        "avg_win_usd": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_usd": float(losses.mean()) if len(losses) else 0.0,
        "estimated_monthly_pnl": ((sim.final_equity - equity) / days * 30.44
                                  if days > 0 else 0.0),
        "trades_per_month": (len(nets) / days * 30.44 if days > 0 else 0.0),
        "exit_reasons": dict(sorted(Counter(t.reason for t in sim.trades).items())),
        "blocked_min_notional": sim.blocked_min_notional,
        "floored_min_notional": sim.floored_min_notional,
    })
    return result, sim


def _t_stat(values):
    arr = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if len(arr) < 2 or arr.std(ddof=1) <= 0:
        return 0.0
    return float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr))))


def _sign_flip_p(values, runs=2000):
    arr = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if not len(arr):
        return 1.0
    observed = float(arr.mean())
    rng = np.random.default_rng(20260903)
    exceed = 0
    for _ in range(runs):
        signs = rng.choice((-1.0, 1.0), size=len(arr))
        exceed += float(np.mean(arr * signs)) >= observed
    return float((exceed + 1) / (runs + 1))


def signal_diagnostics(candles, funding, primary, start, end):
    grid, aligned = align_candles(candles)
    atrs = {symbol: atr_series(rows, primary.atr_period)
            for symbol, rows in aligned.items()}
    base = replace(primary, confirmation_lookback=0,
                   funding_lookback_days=0,
                   max_trailing_funding_bps=float("inf"))
    consensus = replace(primary, funding_lookback_days=0,
                        max_trailing_funding_bps=float("inf"))
    reviewed = base_candidates = confirmation_blocks = funding_blocks = 0
    final_candidates = 0
    forward = []
    cadence = max(1, primary.rebalance_bars)
    for i in range(start, end):
        if int(grid[i] // 86400) % cadence != primary.rebalance_offset % cadence:
            continue
        reviewed += 1
        raw = choose_target(aligned, atrs, i, base, times=grid,
                            funding_bps_by_day=funding)
        agreed = choose_target(aligned, atrs, i, consensus, times=grid,
                               funding_bps_by_day=funding)
        target = choose_target(aligned, atrs, i, primary, times=grid,
                               funding_bps_by_day=funding)
        if raw is not None:
            base_candidates += 1
        if raw is not None and agreed is None:
            confirmation_blocks += 1
        if agreed is not None and target is None:
            funding_blocks += 1
        if target is None:
            continue
        final_candidates += 1
        entry_i, exit_i = i + 1, i + 31
        if exit_i < end:
            entry = aligned[target.symbol][entry_i].open
            exit_price = aligned[target.symbol][exit_i].open
            gross = target.side * (exit_price / entry - 1.0)
            forward.append(gross - 2.0 * primary.cost_bps_per_side / 1e4)
    arr = np.asarray(forward, dtype=float)
    return {
        "reviewed_rebalances": reviewed,
        "base_30d_candidates": base_candidates,
        "confirmation_day_blocks": confirmation_blocks,
        "funding_crowding_day_blocks": funding_blocks,
        "consensus_candidates": final_candidates,
        "forward_30d_observations": int(len(arr)),
        "forward_30d_fee_net_mean_pct": float(arr.mean() * 100) if len(arr) else 0.0,
        "forward_30d_fee_net_median_pct": float(np.median(arr) * 100) if len(arr) else 0.0,
        "forward_30d_positive_pct": float(np.mean(arr > 0) * 100) if len(arr) else 0.0,
        "forward_30d_t": _t_stat(arr),
    }


def fmt(result):
    return (f"n={int(result['trades']):3d} win={result['win_rate']:5.1f}% "
            f"net=${result['net_pnl']:+6.3f} final=${result['final_equity']:6.3f} "
            f"CAGR={result['cagr_pct']:+5.2f}% PF={result['profit_factor']:4.2f} "
            f"DD={result['max_drawdown_pct']:5.2f}% avgW=${result['avg_win_usd']:+.3f} "
            f"avgL=${result['avg_loss_usd']:+.3f} est/mo=${result['estimated_monthly_pnl']:+.3f}")


def main(argv=None):
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "API_KEY", "API_SECRET"):
        os.environ.pop(key, None)
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--months", type=float, default=36.0)
    parser.add_argument("--starting-equity", type=float, default=15.0)
    parser.add_argument("--cost-bps-side", type=float, default=7.0)
    parser.add_argument("--stress-cost-bps-side", type=float, default=10.0)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",")
               if symbol.strip()]

    print("=== frozen 30d/90d funding-aware TSMOM consensus ===")
    print("    long/cash; weekly next-open decisions; one position; DCA=0")
    print("    30d + 90d trends must agree; trailing 3d funding <=9 bps")
    print("    2% stop risk; 3% min-notional floor cap; leverage <=1x")
    print(f"    cost={args.cost_bps_side:g} bps/side; stress={args.stress_cost_bps_side:g}")
    candles, funding = fetch_inputs(symbols, args.months)
    now = time.time()
    candles = {symbol: [bar for bar in rows if bar.ts + 86400 <= now]
               for symbol, rows in candles.items()}
    grid, _ = align_candles(candles)
    primary = TSMOMParams(
        lookback=30, confirmation_lookback=90, vol_lookback=30,
        signal_threshold=0.25, rebalance_bars=7, risk_pct=0.02,
        annual_vol_target=0.50, max_leverage=1.0, stop_atr=3.0,
        trail_start_atr=3.0, trail_atr=2.5, cost_bps_per_side=args.cost_bps_side,
        allow_short=False, funding_lookback_days=3,
        max_trailing_funding_bps=9.0)
    warm = max(primary.lookback, primary.confirmation_lookback,
               primary.vol_lookback, primary.atr_period) + 10
    if len(grid) < warm + 730:
        raise SystemExit(f"only {len(grid)} aligned completed daily bars")
    minimums, steps = fetch_execution_filters(symbols)
    n = len(grid)
    final_start = warm + int((n - warm) * 0.75)

    development, dev_sim = evaluate(
        candles, funding, minimums, steps, primary, args.starting_equity,
        warm, final_start)
    diagnostics = signal_diagnostics(candles, funding, primary, warm, final_start)
    print("\nDEVELOPMENT 75% — PRIMARY FROZEN BEFORE DATA")
    print("  " + fmt(development))
    print(f"  exits={development['exit_reasons']} blocks="
          f"{development['blocked_min_notional']} floors="
          f"{development['floored_min_notional']}")
    print("\nSIGNAL / FORWARD EVIDENCE — DEVELOPMENT ONLY")
    print(f"  reviews={diagnostics['reviewed_rebalances']} base="
          f"{diagnostics['base_30d_candidates']} confirmation-blocks="
          f"{diagnostics['confirmation_day_blocks']} funding-blocks="
          f"{diagnostics['funding_crowding_day_blocks']} final="
          f"{diagnostics['consensus_candidates']}")
    print(f"  30d fee-net forward n={diagnostics['forward_30d_observations']} "
          f"mean={diagnostics['forward_30d_fee_net_mean_pct']:+.3f}% "
          f"median={diagnostics['forward_30d_fee_net_median_pct']:+.3f}% "
          f"positive={diagnostics['forward_30d_positive_pct']:.1f}% "
          f"t={diagnostics['forward_30d_t']:+.2f}")

    boundaries = np.linspace(warm, final_start, 4, dtype=int)
    folds = []
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for number, (lo, hi) in enumerate(zip(boundaries[:-1], boundaries[1:]), 1):
        result, _ = evaluate(candles, funding, minimums, steps, primary,
                             args.starting_equity, int(lo), int(hi))
        folds.append(result)
        print(f"  fold {number}/3 " + fmt(result))

    variants = {
        "confirm_60d": replace(primary, confirmation_lookback=60),
        "confirm_120d": replace(primary, confirmation_lookback=120),
        "funding_cap_6bps": replace(primary, max_trailing_funding_bps=6.0),
        "no_funding_filter": replace(primary, funding_lookback_days=0,
                                     max_trailing_funding_bps=float("inf")),
    }
    neighbors = {}
    print("\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY")
    for name, params in variants.items():
        result, _ = evaluate(candles, funding, minimums, steps, params,
                             args.starting_equity, warm, final_start)
        neighbors[name] = result
        print(f"  {name:19s} " + fmt(result))

    stress_params = replace(primary, cost_bps_per_side=args.stress_cost_bps_side)
    stress, _ = evaluate(candles, funding, minimums, steps, stress_params,
                         args.starting_equity, warm, final_start)
    sign_p = _sign_flip_p([trade.net_pnl for trade in dev_sim.trades])
    print(f"\nCOST STRESS {args.stress_cost_bps_side:g} bps/side")
    print("  " + fmt(stress))
    print(f"  trade sign-flip p={sign_p:.3f}")

    positive_folds = sum(result["net_pnl"] > 0 for result in folds)
    positive_rules = sum(result["net_pnl"] > 0
                         for result in [development, *neighbors.values()])
    checks = {
        "development_fee_net_positive": development["net_pnl"] > 0,
        "development_profit_factor_at_least_1_20": development["profit_factor"] >= 1.20,
        "development_at_least_8_trades": development["trades"] >= 8,
        "at_least_2_of_3_folds_positive": positive_folds >= 2,
        "at_least_3_of_5_neighbor_rules_positive": positive_rules >= 3,
        "positive_at_stressed_cost": stress["net_pnl"] > 0,
        "development_drawdown_below_20pct": development["max_drawdown_pct"] < 20.0,
        "development_cagr_at_least_3pct": development["cagr_pct"] >= 3.0,
        "positive_30d_forward_mean": diagnostics["forward_30d_fee_net_mean_pct"] > 0,
        "trade_sign_flip_p_at_most_0_20": sign_p <= 0.20,
    }
    development_passed = all(checks.values())
    print("\nDEVELOPMENT ADMISSION HURDLES")
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL':4s} {name}")

    final = final_stress = None
    final_checks = {}
    if development_passed:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        final, _ = evaluate(candles, funding, minimums, steps, primary,
                            args.starting_equity, final_start, n)
        final_stress, _ = evaluate(candles, funding, minimums, steps,
                                   stress_params, args.starting_equity,
                                   final_start, n)
        final_checks = {
            "final_fee_net_positive": final["net_pnl"] > 0,
            "final_profit_factor_at_least_1_10": final["profit_factor"] >= 1.10,
            "final_at_least_3_trades": final["trades"] >= 3,
            "final_positive_at_stressed_cost": final_stress["net_pnl"] > 0,
            "final_drawdown_below_20pct": final["max_drawdown_pct"] < 20.0,
        }
        print("  FINAL 25% " + fmt(final))
        print("  STRESS    " + fmt(final_stress))
        for name, passed in final_checks.items():
            print(f"  {'PASS' if passed else 'FAIL':4s} {name}")
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")

    passed = development_passed and all(final_checks.values())
    print("\n  " + ("PASS: eligible for frozen paper-only observation."
                     if passed else "REJECTED: do not replace the base TSMOM candidate."))
    payload = {
        "schema": 1,
        "strategy": "tsmom_30d_90d_funding_consensus",
        "symbols": symbols,
        "months": args.months,
        "params": asdict(primary),
        "development": development,
        "diagnostics": diagnostics,
        "development_folds": folds,
        "neighbors": neighbors,
        "stress": stress,
        "trade_sign_flip_p_value": sign_p,
        "development_checks": checks,
        "development_passed": development_passed,
        "sealed_final_status": "opened" if development_passed else "unopened",
        "final": final,
        "final_stress": final_stress,
        "final_checks": final_checks,
        "passed": passed,
        "simulation_only": True,
        "live_orders_sent": 0,
    }
    print("[tsmom-consensus-summary] " + json.dumps(payload, sort_keys=True))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
