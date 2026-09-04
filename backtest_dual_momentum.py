#!/usr/bin/env python3
"""Sealed validation for volatility-managed dual momentum with a cash state.

Research-only: public Binance data, no order-capable module, zero live orders.

Frozen primary rule:
- Universe: SOL, SUI, BNB, XRP, TRX, DOGE perpetuals.
- Weekly review after a completed UTC daily candle; earliest fill next daily open.
- Relative momentum: rank raw trailing 90-day returns.
- Absolute momentum: selected asset must have both positive 90-day and positive
  30-day returns; otherwise hold cash.
- Exposure is volatility managed with a 35% annual-vol target, capped at 1x,
  while the existing 2% stop-risk cap, 3 ATR stop, 3 ATR trail activation and
  2.5 ATR trail remain in force.
- One position, long/cash only, DCA=0, 7 bps per side plus exact historical
  funding; 10 bps/side stress.
- $15 replay uses current minimum notionals/quantity steps; minimum-order floor
  is allowed only while stop risk remains <=3%.

Neighbour checks: relative lookback 60/120 days and absolute lookback 20/45 days.
The first 75% after warm-up is development. The final 25% remains sealed unless
all development admission gates pass.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace

import numpy as np

import tsmom
from backtest_tsmom import fetch_inputs
from paper_tsmom import fetch_execution_filters
from tsmom import TSMOMParams, Target, align_candles, position_leverage, stats

DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"


@dataclass(frozen=True)
class DualParams:
    relative_lookback: int = 90
    absolute_lookback: int = 30


def _raw_return(rows, i: int, lookback: int) -> float:
    if i < lookback:
        return float("nan")
    old = float(rows[i - lookback].close)
    new = float(rows[i].close)
    return new / old - 1.0 if old > 0 else float("nan")


def _annual_vol(rows, i: int, lookback: int) -> float:
    if i < lookback or lookback < 2:
        return float("nan")
    closes = np.asarray([float(x.close) for x in rows[i - lookback:i + 1]], dtype=float)
    if np.any(closes <= 0):
        return float("nan")
    rets = np.diff(np.log(closes))
    if len(rets) < 2:
        return float("nan")
    return float(rets.std(ddof=1) * math.sqrt(365.0))


def choose_dual_target(aligned, atrs, i, p, dual: DualParams):
    ranked = []
    for symbol, rows in aligned.items():
        rel = _raw_return(rows, i, dual.relative_lookback)
        if math.isfinite(rel):
            ranked.append((rel, symbol))
    if not ranked:
        return None
    rel, symbol = max(ranked, key=lambda x: (x[0], x[1]))
    rows = aligned[symbol]
    abs_ret = _raw_return(rows, i, dual.absolute_lookback)
    if not (math.isfinite(rel) and math.isfinite(abs_ret) and rel > 0 and abs_ret > 0):
        return None
    atr = atrs[symbol][i] if i < len(atrs[symbol]) else None
    if atr is None or atr <= 0:
        return None
    ann = _annual_vol(rows, i, p.vol_lookback)
    lev = position_leverage(float(rows[i].close), float(atr), ann, p)
    if lev <= 0:
        return None
    return Target(symbol=symbol, side=1, score=float(rel), leverage=float(lev), atr=float(atr))


def run_dual(candles, funding, params, dual, *, starting_equity, start, end,
             minimums, steps):
    original = tsmom.choose_target

    def chooser(aligned, atrs, i, p, *, times=None, funding_bps_by_day=None):
        return choose_dual_target(aligned, atrs, i, p, dual)

    tsmom.choose_target = chooser
    try:
        return tsmom.run(
            candles, funding, params, starting_equity=starting_equity,
            trade_start=start, trade_end=end,
            min_notional_by_symbol=minimums,
            qty_step_by_symbol=steps,
            min_notional_max_risk_pct=0.03,
        )
    finally:
        tsmom.choose_target = original


def enrich(sim, equity):
    r = stats(sim, equity)
    nets = np.asarray([t.net_pnl for t in sim.trades], dtype=float)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    r.update({
        "net_pnl": float(sim.final_equity - equity),
        "final_equity": float(sim.final_equity),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "avg_win_usd": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_usd": float(losses.mean()) if len(losses) else 0.0,
        "estimated_monthly_pnl": ((sim.final_equity - equity) / days * 30.44
                                  if days > 0 else 0.0),
        "exit_reasons": dict(sorted(Counter(t.reason for t in sim.trades).items())),
        "blocked_min_notional": int(sim.blocked_min_notional),
        "floored_min_notional": int(sim.floored_min_notional),
    })
    return r


def evaluate(candles, funding, minimums, steps, params, dual, equity, start, end):
    sim = run_dual(
        candles, funding, params, dual, starting_equity=equity,
        start=start, end=end, minimums=minimums, steps=steps,
    )
    return enrich(sim, equity), sim


def _sign_flip_p(values, runs=4000):
    arr = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    if not len(arr):
        return 1.0
    observed = float(arr.mean())
    rng = np.random.default_rng(20260904)
    exceed = 0
    for _ in range(runs):
        signs = rng.choice((-1.0, 1.0), size=len(arr))
        exceed += float(np.mean(arr * signs)) >= observed
    return float((exceed + 1) / (runs + 1))


def diagnostics(candles, params, dual, start, end):
    grid, aligned = align_candles(candles)
    reviewed = selected = cash = 0
    forward_returns = []
    cadence = max(1, params.rebalance_bars)
    for i in range(start, end):
        if int(grid[i] // 86400) % cadence != params.rebalance_offset % cadence:
            continue
        reviewed += 1
        ranked = []
        for symbol, rows in aligned.items():
            rel = _raw_return(rows, i, dual.relative_lookback)
            if math.isfinite(rel):
                ranked.append((rel, symbol))
        if not ranked:
            cash += 1
            continue
        rel, symbol = max(ranked, key=lambda x: (x[0], x[1]))
        abs_ret = _raw_return(aligned[symbol], i, dual.absolute_lookback)
        if not (rel > 0 and math.isfinite(abs_ret) and abs_ret > 0):
            cash += 1
            continue
        selected += 1
        entry_i, exit_i = i + 1, i + 31
        if exit_i < end:
            entry = float(aligned[symbol][entry_i].open)
            exit_price = float(aligned[symbol][exit_i].open)
            if entry > 0:
                forward_returns.append(
                    exit_price / entry - 1.0 - 2.0 * params.cost_bps_per_side / 1e4
                )
    arr = np.asarray(forward_returns, dtype=float)
    t = 0.0
    if len(arr) >= 2 and arr.std(ddof=1) > 0:
        t = float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr))))
    return {
        "reviewed_rebalances": reviewed,
        "selected_risk_on": selected,
        "cash_reviews": cash,
        "forward_30d_observations": int(len(arr)),
        "forward_30d_fee_net_mean_pct": float(arr.mean() * 100) if len(arr) else 0.0,
        "forward_30d_fee_net_median_pct": float(np.median(arr) * 100) if len(arr) else 0.0,
        "forward_30d_positive_pct": float(np.mean(arr > 0) * 100) if len(arr) else 0.0,
        "forward_30d_t": t,
    }


def fmt(r):
    return (f"n={int(r['trades']):3d} win={r['win_rate']:5.1f}% "
            f"net=${r['net_pnl']:+7.4f} final=${r['final_equity']:7.4f} "
            f"CAGR={r['cagr_pct']:+6.2f}% PF={r['profit_factor']:5.2f} "
            f"DD={r['max_drawdown_pct']:5.2f}% est/mo=${r['estimated_monthly_pnl']:+.4f}")


def main(argv=None):
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "API_KEY", "API_SECRET"):
        os.environ.pop(key, None)

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--months", type=float, default=48.0)
    ap.add_argument("--starting-equity", type=float, default=15.0)
    ap.add_argument("--cost-bps-side", type=float, default=7.0)
    ap.add_argument("--stress-cost-bps-side", type=float, default=10.0)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if len(symbols) != 6:
        raise SystemExit("frozen study requires exactly six symbols")

    print("=== FROZEN VOLATILITY-MANAGED DUAL MOMENTUM ===")
    print("    relative: strongest raw 90d return; absolute: selected 90d>0 and 30d>0")
    print("    vol target: 35% annual; <=1x; weekly review; next-open fill")
    print("    cash when absolute momentum fails; one position; no shorts; DCA=0")
    print("    development 75%; final 25% sealed until all dev gates pass")

    candles, funding = fetch_inputs(symbols, a.months)
    now = time.time()
    candles = {
        s: [bar for bar in rows if bar.ts + 86400 <= now]
        for s, rows in candles.items()
    }
    grid, _ = align_candles(candles)
    params = TSMOMParams(
        lookback=30, vol_lookback=30, signal_threshold=0.0,
        rebalance_bars=7, risk_pct=0.02, annual_vol_target=0.35,
        max_leverage=1.0, stop_atr=3.0, trail_start_atr=3.0,
        trail_atr=2.5, cost_bps_per_side=a.cost_bps_side, allow_short=False,
    )
    primary = DualParams()
    warm = max(primary.relative_lookback, primary.absolute_lookback,
               params.vol_lookback, params.atr_period) + 10
    if len(grid) < warm + 730:
        raise SystemExit(f"only {len(grid)} aligned completed daily bars")

    minimums, steps = fetch_execution_filters(symbols)
    n = len(grid)
    final_start = warm + int((n - warm) * 0.75)

    development, dev_sim = evaluate(
        candles, funding, minimums, steps, params, primary,
        a.starting_equity, warm, final_start,
    )
    diag = diagnostics(candles, params, primary, warm, final_start)
    print("\nDEVELOPMENT 75%")
    print("  PRIMARY " + fmt(development))
    print(f"  exits={development['exit_reasons']} blocks={development['blocked_min_notional']} "
          f"floors={development['floored_min_notional']}")
    print(f"  reviews={diag['reviewed_rebalances']} risk-on={diag['selected_risk_on']} "
          f"cash={diag['cash_reviews']} forward_n={diag['forward_30d_observations']} "
          f"forward_mean={diag['forward_30d_fee_net_mean_pct']:+.3f}% "
          f"t={diag['forward_30d_t']:+.2f}")

    bounds = np.linspace(warm, final_start, 4, dtype=int)
    folds = []
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for number, (lo, hi) in enumerate(zip(bounds[:-1], bounds[1:]), 1):
        r, _ = evaluate(
            candles, funding, minimums, steps, params, primary,
            a.starting_equity, int(lo), int(hi),
        )
        folds.append(r)
        print(f"  fold {number}/3 " + fmt(r))

    variants = {
        "relative_60_abs30": replace(primary, relative_lookback=60),
        "relative_120_abs30": replace(primary, relative_lookback=120),
        "relative_90_abs20": replace(primary, absolute_lookback=20),
        "relative_90_abs45": replace(primary, absolute_lookback=45),
    }
    neighbors = {}
    print("\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY")
    for name, dual in variants.items():
        r, _ = evaluate(
            candles, funding, minimums, steps, params, dual,
            a.starting_equity, warm, final_start,
        )
        neighbors[name] = r
        print(f"  {name:22s} " + fmt(r))

    stress_params = replace(params, cost_bps_per_side=a.stress_cost_bps_side)
    stress, _ = evaluate(
        candles, funding, minimums, steps, stress_params, primary,
        a.starting_equity, warm, final_start,
    )
    sign_p = _sign_flip_p([t.net_pnl for t in dev_sim.trades])
    print(f"\nCOST STRESS {a.stress_cost_bps_side:g} bps/side")
    print("  " + fmt(stress))
    print(f"  trade sign-flip p={sign_p:.3f}")

    positive_folds = sum(r["net_pnl"] > 0 for r in folds)
    positive_rules = sum(r["net_pnl"] > 0 for r in [development, *neighbors.values()])
    checks = {
        "development_fee_net_positive": development["net_pnl"] > 0,
        "development_profit_factor_at_least_1_20": development["profit_factor"] >= 1.20,
        "development_at_least_8_trades": development["trades"] >= 8,
        "at_least_2_of_3_folds_positive": positive_folds >= 2,
        "at_least_3_of_5_rules_positive": positive_rules >= 3,
        "positive_at_stressed_cost": stress["net_pnl"] > 0,
        "development_drawdown_below_20pct": development["max_drawdown_pct"] < 20.0,
        "development_cagr_at_least_3pct": development["cagr_pct"] >= 3.0,
        "positive_30d_forward_mean": diag["forward_30d_fee_net_mean_pct"] > 0,
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
        final, _ = evaluate(
            candles, funding, minimums, steps, params, primary,
            a.starting_equity, final_start, n,
        )
        final_stress, _ = evaluate(
            candles, funding, minimums, steps, stress_params, primary,
            a.starting_equity, final_start, n,
        )
        final_checks = {
            "final_fee_net_positive": final["net_pnl"] > 0,
            "final_profit_factor_at_least_1_10": final["profit_factor"] >= 1.10,
            "final_at_least_3_trades": final["trades"] >= 3,
            "final_positive_at_stressed_cost": final_stress["net_pnl"] > 0,
            "final_drawdown_below_20pct": final["max_drawdown_pct"] < 20.0,
        }
        print("  FINAL  " + fmt(final))
        print("  STRESS " + fmt(final_stress))
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")

    passed = development_passed and all(final_checks.values())
    summary = {
        "strategy": "volatility_managed_dual_momentum",
        "development_passed": development_passed,
        "sealed_final_opened": development_passed,
        "passed": passed,
        "dev_net_pnl": development["net_pnl"],
        "dev_pf": development["profit_factor"],
        "dev_cagr_pct": development["cagr_pct"],
        "dev_dd_pct": development["max_drawdown_pct"],
        "dev_trades": development["trades"],
        "stress_net_pnl": stress["net_pnl"],
        "sign_flip_p": sign_p,
        "positive_folds": positive_folds,
        "positive_rules": positive_rules,
        "forward_30d_mean_pct": diag["forward_30d_fee_net_mean_pct"],
        "forward_30d_t": diag["forward_30d_t"],
        "final_net_pnl": final["net_pnl"] if final else None,
        "final_pf": final["profit_factor"] if final else None,
        "live_orders_sent": 0,
    }
    print("\n[dual-momentum-summary] " + json.dumps(summary, sort_keys=True))
    print("\n" + ("PASS: eligible for paper-only observation."
                    if passed else
                    "REJECTED: do not replace the admitted base 30-day TSMOM candidate."))
    if a.json:
        payload = {
            "schema": 1,
            "strategy": summary["strategy"],
            "symbols": symbols,
            "months": a.months,
            "starting_equity": a.starting_equity,
            "base_params": asdict(params),
            "dual_params": asdict(primary),
            "development": development,
            "diagnostics": diag,
            "folds": folds,
            "neighbors": neighbors,
            "stress": stress,
            "sign_flip_p": sign_p,
            "checks": checks,
            "development_passed": development_passed,
            "sealed_final_opened": development_passed,
            "final": final,
            "final_stress": final_stress,
            "final_checks": final_checks,
            "passed": passed,
            "live_orders_sent": 0,
            "research_only": True
        }
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        print(f"wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
