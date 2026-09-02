#!/usr/bin/env python3
"""Sealed 4H breakout / completed-1D regime validation for a $15 wallet.

Six coherent rules are compared on the first 75% only.  The final 25% is
evaluated exactly once only when a rule clears every precommitted training
hurdle.  Public market data only; Binance credentials are deleted and no
order-capable module is imported.
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
from fetch_funding_universe import fetch_symbol
from htf_breakout import (HTFBreakoutParams, align_candles, prepare_features,
                          run, signals_at, stats)
from paper_tsmom import fetch_execution_filters


DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"

# Frozen before downloading data.  These are neighbouring expressions of one
# hypothesis, not an open-ended optimiser.
FAMILY = {
    "BRK20_EXIT10": HTFBreakoutParams(channel=20, exit_channel=10,
                                       stop_atr=1.5, trail_start_atr=2.5,
                                       trail_atr=2.0, max_hold_bars=90),
    "BRK30_EXIT15": HTFBreakoutParams(channel=30, exit_channel=15,
                                       stop_atr=1.75, trail_start_atr=3.0,
                                       trail_atr=2.25, max_hold_bars=108),
    "BRK40_EXIT20": HTFBreakoutParams(),
    "BRK60_EXIT30": HTFBreakoutParams(channel=60, exit_channel=30,
                                       stop_atr=2.25, trail_start_atr=3.5,
                                       trail_atr=3.0, max_hold_bars=168),
    "BRK40_VOL12": HTFBreakoutParams(volume_ratio=1.2),
    "BRK40_LONG": HTFBreakoutParams(allow_short=False),
}


def fetch_inputs(symbols, months):
    candles, funding = {}, {}

    def one(symbol):
        rows = backtest_breakout.fetch(symbol, "4h", months, verbose=False)
        rates = fetch_symbol(symbol, months, pause=0.03)
        return symbol, rows, {int(ts): float(rate) for ts, rate in rates}

    with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
        jobs = {pool.submit(one, symbol): symbol for symbol in symbols}
        for job in as_completed(jobs):
            symbol, rows, rates = job.result()
            candles[symbol], funding[symbol] = rows, rates
            print(f"  {symbol}: {len(rows)} four-hour bars, {len(rates)} funding prints")
    return candles, funding


def evaluate(candles, funding, minimums, steps, p, equity, start, end):
    sim = run(candles, p, starting_equity=equity, trade_start=start,
              trade_end=end, funding_bps_by_ts=funding,
              min_notional_by_symbol=minimums, qty_step_by_symbol=steps)
    st = stats(sim, equity)
    st.update({
        "exit_reasons": dict(sorted(Counter(t.reason for t in sim.trades).items())),
        "symbols": dict(sorted(Counter(t.symbol for t in sim.trades).items())),
        "exposure_blocks": sim.exposure_blocks,
        "risk_blocks": sim.risk_blocks,
        "min_notional_blocks": sim.min_notional_blocks,
        "daily_loss_blocks": sim.daily_loss_blocks,
        "daily_target_blocks": sim.daily_target_blocks,
        "daily_loss_lock_hits": sim.daily_loss_lock_hits,
        "daily_target_lock_hits": sim.daily_target_lock_hits,
    })
    st["estimated_monthly_pnl"] = (st["net_pnl"] / st["days"] * 30.44
                                    if st["days"] > 0 else 0.0)
    return st, sim


def sign_flip_pvalue(values, seed=20260902, samples=20000):
    """One-sided null that trade signs are exchangeable; deterministic seed."""
    a = np.asarray(values, dtype=float)
    if not len(a) or a.mean() <= 0:
        return 1.0
    rng = np.random.default_rng(seed)
    observed = float(a.mean())
    hits = 1
    for _ in range(samples):
        hits += float((a * rng.choice((-1.0, 1.0), len(a))).mean()) >= observed
    return hits / (samples + 1.0)


def forward_evidence(candles, p, start, end, horizons=(6, 18)):
    """Non-overlapping directional returns after top-ranked completed signals."""
    times, aligned = align_candles(candles)
    features = prepare_features(aligned, p)
    out = {}
    for horizon in horizons:
        rows, next_allowed = [], start
        i = start
        while i + horizon < end:
            choices = signals_at(aligned, features, i, p)
            if choices and i >= next_allowed:
                sig = choices[0]
                entry = aligned[sig.symbol][i + 1].open
                exit_px = aligned[sig.symbol][i + horizon].close
                gross_bps = sig.side * (exit_px / entry - 1.0) * 1e4
                rows.append(gross_bps - 2.0 * p.cost_bps_per_side)
                next_allowed = i + horizon
            i += 1
        a = np.asarray(rows, dtype=float)
        stderr = float(a.std(ddof=1) / math.sqrt(len(a))) if len(a) > 1 else 0.0
        out[f"{horizon * 4}h"] = {
            "non_overlapping_signals": len(rows),
            "mean_fee_net_bps": float(a.mean()) if len(a) else 0.0,
            "median_fee_net_bps": float(np.median(a)) if len(a) else 0.0,
            "positive_rate_pct": float((a > 0).mean() * 100) if len(a) else 0.0,
            "t_stat": float(a.mean() / stderr) if stderr > 0 else 0.0,
        }
    return out


def fmt(st):
    return (f"n={int(st['trades']):3d} win={st['win_rate']:5.1f}% "
            f"net=${st['net_pnl']:+7.3f} final=${st['final_equity']:6.3f} "
            f"PF={st['profit_factor']:4.2f} DD={st['max_drawdown_pct']:5.1f}% "
            f"avgW=${st['avg_win']:+.3f} avgL=${st['avg_loss']:+.3f} "
            f"tr/d={st['trades_per_day']:.3f} est/mo=${st['estimated_monthly_pnl']:+.3f}")


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

    print(f"=== SEALED 4H BREAKOUT + COMPLETED 1D REGIME: {a.months:g} months ===")
    print("    first 75%=training only; final 25%=opened once only after train pass")
    print("    $15 wallet, $10 notional, max 5x, one position, DCA=0")
    print(f"    {a.cost_bps_side:g} bps/side base, {a.stress_cost_bps_side:g} bps/side stress, exact funding")
    candles, funding = fetch_inputs(symbols, a.months)
    now = time.time()
    candles = {s: [c for c in rows if c.ts + 14400.0 <= now] for s, rows in candles.items()}
    grid, _ = align_candles(candles)
    warm = max(max(p.daily_slow * 6, p.channel, p.exit_channel,
                   p.atr_period, p.volume_lookback) for p in FAMILY.values()) + 12
    if len(grid) < warm + 6 * 365:
        raise SystemExit(f"only {len(grid)} aligned 4H bars; insufficient history")
    n = len(grid)
    train_end = warm + int((n - warm) * 0.75)
    minimums, steps = fetch_execution_filters(symbols)
    fold_edges = np.linspace(warm, train_end, 4, dtype=int)
    print(f"    aligned={n} bars; warm={warm}; train={train_end-warm}; sealed-final={n-train_end}")

    train_results, eligible = {}, []
    print("\nTRAIN-ONLY FAMILY")
    for name, raw in FAMILY.items():
        p = replace(raw, cost_bps_per_side=a.cost_bps_side)
        combined, sim = evaluate(candles, funding, minimums, steps, p,
                                 a.starting_equity, warm, train_end)
        folds = []
        for lo, hi in zip(fold_edges[:-1], fold_edges[1:]):
            st, _ = evaluate(candles, funding, minimums, steps, p,
                             a.starting_equity, int(lo), int(hi))
            folds.append(st)
        stress_p = replace(p, cost_bps_per_side=a.stress_cost_bps_side)
        stress, _ = evaluate(candles, funding, minimums, steps, stress_p,
                             a.starting_equity, warm, train_end)
        pvalue = sign_flip_pvalue([t.net_pnl for t in sim.trades])
        positive_folds = sum(st["net_pnl"] > 0 for st in folds)
        checks = {
            "fee_net_positive": combined["net_pnl"] > 0,
            "profit_factor_at_least_1_15": combined["profit_factor"] >= 1.15,
            "at_least_30_trades": combined["trades"] >= 30,
            "at_least_2_of_3_folds_positive": positive_folds >= 2,
            "positive_at_10bps_side": stress["net_pnl"] > 0,
            "drawdown_below_20pct": combined["max_drawdown_pct"] < 20,
            "average_winner_at_least_5c": combined["avg_win"] >= 0.05,
        }
        ok = all(checks.values())
        print(f"  {name:14s} {fmt(combined)} folds+={positive_folds}/3 "
              f"stress=${stress['net_pnl']:+.3f} p={pvalue:.4f} {'ELIGIBLE' if ok else 'REJECT'}")
        train_results[name] = {"params": asdict(p), "combined": combined,
                               "folds": folds, "stress": stress,
                               "sign_flip_pvalue_uncorrected": pvalue,
                               "bonferroni_alpha_6_rules": 0.05 / len(FAMILY),
                               "checks": checks, "eligible": ok}
        if ok:
            # Prefer the most consistent fold floor, then total training edge.
            eligible.append((min(st["net_pnl"] for st in folds),
                             combined["net_pnl"], name, p))

    selected = max(eligible)[2] if eligible else None
    holdout = stress = final_forward = final_checks = None
    passed = False
    if selected is None:
        print("\nSEALED FINAL 25% NOT OPENED: no family member cleared training hurdles.")
    else:
        p = max(eligible)[3]
        print(f"\nFROZEN SELECTION: {selected}; opening final 25% exactly once")
        holdout, _ = evaluate(candles, funding, minimums, steps, p,
                              a.starting_equity, train_end, n)
        stress, _ = evaluate(candles, funding, minimums, steps,
                             replace(p, cost_bps_per_side=a.stress_cost_bps_side),
                             a.starting_equity, train_end, n)
        final_forward = forward_evidence(candles, p, train_end, n)
        final_checks = {
            "final_fee_net_positive": holdout["net_pnl"] > 0,
            "final_profit_factor_at_least_1_15": holdout["profit_factor"] >= 1.15,
            "final_at_least_10_trades": holdout["trades"] >= 10,
            "final_average_winner_at_least_5c": holdout["avg_win"] >= 0.05,
            "final_positive_at_10bps_side": stress["net_pnl"] > 0,
            "final_drawdown_below_20pct": holdout["max_drawdown_pct"] < 20,
            "estimated_monthly_pnl_at_least_15c": holdout["estimated_monthly_pnl"] >= 0.15,
        }
        passed = all(final_checks.values())
        print("  FINAL 25% " + fmt(holdout))
        print(f"  STRESS    {fmt(stress)}")
        print(f"  exits={holdout['exit_reasons']} symbols={holdout['symbols']}")
        print(f"  forward={json.dumps(final_forward, sort_keys=True)}")
        for key, ok in final_checks.items():
            print(f"  {'PASS' if ok else 'FAIL':4s} {key}")

    train_forward = None
    if selected:
        train_forward = forward_evidence(candles, max(eligible)[3], warm, train_end)
    payload = {
        "schema": 1, "strategy": "4h_donchian_completed_1d_regime",
        "simulation_only": True, "live_orders_sent": 0,
        "symbols": symbols, "months": a.months, "starting_equity": a.starting_equity,
        "cost_bps_per_side": a.cost_bps_side,
        "stress_cost_bps_per_side": a.stress_cost_bps_side,
        "split": {"warm": warm, "train_end": train_end, "bars": n,
                  "train_fraction": 0.75, "final_opened": selected is not None},
        "train_results": train_results, "selected": selected,
        "train_forward_returns": train_forward, "holdout": holdout,
        "holdout_stress": stress, "holdout_forward_returns": final_forward,
        "final_checks": final_checks, "passed": passed,
    }
    print("\n[htf-breakout-summary] " + json.dumps(payload, sort_keys=True))
    print("\n" + ("PASS: paper-only runner may be built; live remains forbidden."
                   if passed else "REJECTED: do not paper/live deploy this strategy."))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
