#!/usr/bin/env python3
"""Sealed validation for the fixed 4H/1H EMA-RSI-ATR continuation rule."""
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
from fetch_funding_universe import fetch_symbol
from mtf_continuation import (MTFParams, align_candles, prepare_features, run,
                              signals_at, stats)
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
            candles[symbol], funding[symbol] = rows, rates
            print(f"  {symbol}: {len(rows)} hourly bars, {len(rates)} funding prints")
    return candles, funding


def evaluate(candles, funding, minimums, steps, p, equity, start, end):
    sim = run(candles, p, starting_equity=equity, trade_start=start,
              trade_end=end, funding_bps_by_ts=funding,
              min_notional_by_symbol=minimums, qty_step_by_symbol=steps)
    st = stats(sim, equity)
    st.update({
        "exit_reasons": dict(sorted(Counter(t.reason for t in sim.trades).items())),
        "symbols": dict(sorted(Counter(t.symbol for t in sim.trades).items())),
        "patterns": dict(sorted(Counter(t.pattern for t in sim.trades).items())),
        "exposure_blocks": sim.exposure_blocks,
        "risk_blocks": sim.risk_blocks,
        "minimum_blocks": sim.minimum_blocks,
        "minimum_floors": sim.minimum_floors,
        "daily_loss_blocks": sim.daily_loss_blocks,
        "daily_target_blocks": sim.daily_target_blocks,
        "daily_loss_lock_hits": sim.daily_loss_lock_hits,
        "daily_target_lock_hits": sim.daily_target_lock_hits,
    })
    st["estimated_monthly_pnl"] = (st["net_pnl"] / st["days"] * 30.44
                                    if st["days"] > 0 else 0.0)
    return st, sim


def sign_flip_pvalue(values, seed=20260902, samples=20000):
    a = np.asarray(values, dtype=float)
    if not len(a) or a.mean() <= 0:
        return 1.0
    rng = np.random.default_rng(seed)
    observed, hits = float(a.mean()), 1
    for _ in range(samples):
        hits += int(float((a * rng.choice((-1.0, 1.0), len(a))).mean()) >= observed)
    return hits / (samples + 1.0)


def forward_evidence(candles, p, start, end, horizons=(6, 24, 72)):
    times, aligned = align_candles(candles)
    features = prepare_features(aligned, p)
    out = {}
    for horizon in horizons:
        rows, next_allowed = [], start
        for i in range(start, max(start, end - horizon)):
            choices = signals_at(aligned, features, i, p)
            if choices and i >= next_allowed and i + horizon < end:
                sig = choices[0]
                entry = aligned[sig.symbol][i + 1].open
                finish = aligned[sig.symbol][i + horizon].close
                gross = sig.side * (finish / entry - 1.0) * 1e4
                rows.append(gross - 2.0 * p.cost_bps_per_side)
                next_allowed = i + horizon
        a = np.asarray(rows, dtype=float)
        se = float(a.std(ddof=1) / math.sqrt(len(a))) if len(a) > 1 else 0.0
        out[f"{horizon}h"] = {
            "non_overlapping_signals": len(rows),
            "mean_fee_net_bps": float(a.mean()) if len(a) else 0.0,
            "median_fee_net_bps": float(np.median(a)) if len(a) else 0.0,
            "positive_rate_pct": float((a > 0).mean() * 100.0) if len(a) else 0.0,
            "t_stat": float(a.mean() / se) if se > 0 else 0.0,
        }
    return out


def fmt(st):
    return (f"n={int(st['trades']):3d} win={st['win_rate']:5.1f}% "
            f"net=${st['net_pnl']:+7.3f} final=${st['final_equity']:6.3f} "
            f"PF={st['profit_factor']:4.2f} DD={st['max_drawdown_pct']:5.1f}% "
            f"avgW=${st['avg_win']:+.3f} avgL=${st['avg_loss']:+.3f} "
            f"W/L-R={st['avg_win_r']:.2f}/{st['avg_loss_r']:.2f} "
            f"sig/d={st['candidate_signals_per_day']:.2f} "
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
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    primary = MTFParams(cost_bps_per_side=a.cost_bps_side)
    neighbours = {
        "PRIMARY": primary,
        "RSI_RELAXED": replace(primary, long_rsi_low=40, long_rsi_high=70,
                               short_rsi_low=30, short_rsi_high=60),
        "ZONE_025ATR": replace(primary, zone_tolerance_atr=0.25),
        "RR_2_5": replace(primary, reward_risk=2.5),
    }

    print(f"=== FIXED 4H/1H EMA-RSI-ATR CONTINUATION: {a.months:g} months ===")
    print("    4H EMA200 + 1H EMA200/20/50 + objective engulfing/pinbar + RSI14")
    print("    next-hour-open fills; ATR1.5 stop; 2R target; max 120h")
    print("    $15 wallet; up to $10 notional; max 5x; risk<=2%; one position; DCA=0")
    print(f"    base cost={a.cost_bps_side:g} bps/side; stress={a.stress_cost_bps_side:g}; exact funding")
    candles, funding = fetch_inputs(symbols, a.months)
    now = time.time()
    candles = {s: [c for c in rows if c.ts + 3600.0 <= now] for s, rows in candles.items()}
    grid, _ = align_candles(candles)
    warm = max(primary.four_hour_ema * 4, primary.hourly_macro,
               primary.hourly_slow, primary.atr_period, primary.rsi_period) + 120
    if len(grid) < warm + 24 * 365:
        raise SystemExit(f"only {len(grid)} aligned hourly bars; insufficient history")
    n = len(grid)
    train_end = warm + int((n - warm) * 0.75)
    folds = np.linspace(warm, train_end, 4, dtype=int)
    minimums, steps = fetch_execution_filters(symbols)
    print(f"    aligned={n}; warm={warm}; train={train_end-warm}; sealed-final={n-train_end}")

    train_results = {}
    print("\nTRAIN ONLY — PRIMARY AND PREDECLARED NEIGHBOURS")
    for name, p in neighbours.items():
        combined, sim = evaluate(candles, funding, minimums, steps, p,
                                 a.starting_equity, warm, train_end)
        train_results[name] = {"params": asdict(p), "combined": combined}
        print(f"  {name:12s} {fmt(combined)}")
        if name == "PRIMARY":
            primary_sim = sim
    primary_train = train_results["PRIMARY"]["combined"]
    fold_stats = []
    print("\nPRIMARY TRAIN FOLDS")
    for j, (lo, hi) in enumerate(zip(folds[:-1], folds[1:]), 1):
        st, _ = evaluate(candles, funding, minimums, steps, primary,
                         a.starting_equity, int(lo), int(hi))
        fold_stats.append(st)
        print(f"  fold {j}/3 {fmt(st)}")
    stress_p = replace(primary, cost_bps_per_side=a.stress_cost_bps_side)
    train_stress, _ = evaluate(candles, funding, minimums, steps, stress_p,
                               a.starting_equity, warm, train_end)
    train_forward = forward_evidence(candles, primary, warm, train_end)
    pvalue = sign_flip_pvalue([t.net_pnl for t in primary_sim.trades])
    positive_folds = sum(st["net_pnl"] > 0 for st in fold_stats)
    positive_neighbours = sum(v["combined"]["net_pnl"] > 0
                              for v in train_results.values())
    train_checks = {
        "primary_fee_net_positive": primary_train["net_pnl"] > 0,
        "primary_profit_factor_at_least_1_15": primary_train["profit_factor"] >= 1.15,
        "primary_at_least_50_trades": primary_train["trades"] >= 50,
        "at_least_2_of_3_folds_positive": positive_folds >= 2,
        "at_least_2_of_4_neighbours_positive": positive_neighbours >= 2,
        "positive_at_10bps_side": train_stress["net_pnl"] > 0,
        "drawdown_below_20pct": primary_train["max_drawdown_pct"] < 20,
        "average_winner_at_least_5c": primary_train["avg_win"] >= 0.05,
        "average_win_r_at_least_1_50": primary_train["avg_win_r"] >= 1.50,
        "sign_flip_pvalue_at_most_0_10": pvalue <= 0.10,
    }
    train_pass = all(train_checks.values())
    print(f"\nTRAIN STRESS {fmt(train_stress)}")
    print(f"TRAIN FORWARD {json.dumps(train_forward, sort_keys=True)}")
    print(f"TRAIN p={pvalue:.5f}; positive folds={positive_folds}/3; neighbours={positive_neighbours}/4")
    for key, ok in train_checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s} {key}")

    holdout = holdout_stress = holdout_forward = final_checks = None
    final_halves = []
    passed = False
    if not train_pass:
        print("\nSEALED FINAL 25% NOT OPENED: primary failed training admission.")
    else:
        print("\nTRAIN PASSED: opening the untouched final 25% exactly once")
        holdout, _ = evaluate(candles, funding, minimums, steps, primary,
                              a.starting_equity, train_end, n)
        holdout_stress, _ = evaluate(candles, funding, minimums, steps, stress_p,
                                     a.starting_equity, train_end, n)
        holdout_forward = forward_evidence(candles, primary, train_end, n)
        half = train_end + (n - train_end) // 2
        for lo, hi in ((train_end, half), (half, n)):
            st, _ = evaluate(candles, funding, minimums, steps, primary,
                             a.starting_equity, lo, hi)
            final_halves.append(st)
        final_checks = {
            "final_fee_net_positive": holdout["net_pnl"] > 0,
            "final_profit_factor_at_least_1_15": holdout["profit_factor"] >= 1.15,
            "final_at_least_15_trades": holdout["trades"] >= 15,
            "both_final_halves_positive": all(st["net_pnl"] > 0 for st in final_halves),
            "final_positive_at_10bps_side": holdout_stress["net_pnl"] > 0,
            "final_drawdown_below_20pct": holdout["max_drawdown_pct"] < 20,
            "final_average_winner_at_least_5c": holdout["avg_win"] >= 0.05,
            "final_average_win_r_at_least_1_50": holdout["avg_win_r"] >= 1.50,
            "estimated_monthly_pnl_at_least_15c": holdout["estimated_monthly_pnl"] >= 0.15,
        }
        passed = all(final_checks.values())
        print("  FINAL 25% " + fmt(holdout))
        print("  STRESS    " + fmt(holdout_stress))
        print(f"  exits={holdout['exit_reasons']} patterns={holdout['patterns']}")
        for j, st in enumerate(final_halves, 1):
            print(f"  final half {j}/2 {fmt(st)}")
        for key, ok in final_checks.items():
            print(f"  {'PASS' if ok else 'FAIL':4s} {key}")

    payload = {
        "schema": 1, "strategy": "4h_ema200_1h_ema_rsi_atr_continuation",
        "simulation_only": True, "live_orders_sent": 0, "symbols": symbols,
        "months": a.months, "starting_equity": a.starting_equity,
        "split": {"bars": n, "warm": warm, "train_end": train_end,
                  "train_fraction": 0.75, "final_opened": train_pass},
        "primary_params": asdict(primary), "train_results": train_results,
        "train_folds": fold_stats, "train_stress": train_stress,
        "train_forward_returns": train_forward,
        "train_sign_flip_pvalue": pvalue, "train_checks": train_checks,
        "train_passed": train_pass, "holdout": holdout,
        "holdout_stress": holdout_stress, "holdout_halves": final_halves,
        "holdout_forward_returns": holdout_forward,
        "monthly_30pct_target_usd": a.starting_equity * 0.30,
        "monthly_30pct_target_met": bool(holdout and holdout["estimated_monthly_pnl"] >= a.starting_equity * 0.30),
        "final_checks": final_checks, "passed": passed,
    }
    print("\n[mtf-continuation-summary] " + json.dumps(payload, sort_keys=True))
    print("\n" + ("PASS: paper-only runner may be built; live remains forbidden."
                   if passed else "REJECTED: do not paper/live deploy this strategy."))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
