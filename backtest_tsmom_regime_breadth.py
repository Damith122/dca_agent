#!/usr/bin/env python3
"""Sealed validation for a BTC-regime + six-coin breadth overlay on 30d TSMOM.

Research-only. Uses public market data and no order-capable module.

Pre-registered primary overlay:
- Base strategy is the already-admitted 30-day long/cash TSMOM rule.
- Review weekly at completed daily close; fill no earlier than next daily open.
- BTC must be in two consecutive non-negative 28-day return blocks (UP->UP).
- At least 4 of 6 tradable coins must have positive raw 30-day returns.
- If either regime gate is false, target is cash (existing position exits next open).
- Base stop/trail/volatility sizing, fees, funding and min-order safety are unchanged.

The first 75% after warm-up is development data. The final 25% stays sealed
unless all development gates pass. Nearby thresholds are robustness checks,
not tuning targets.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from typing import Dict

import numpy as np

import backtest_breakout
import tsmom
from backtest_tsmom import fetch_inputs
from breakout import atr_series
from paper_tsmom import fetch_execution_filters
from tsmom import TSMOMParams, align_candles, stats


DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"
BTC_SYMBOL = "BTCUSDT"


@dataclass(frozen=True)
class OverlayParams:
    btc_block_days: int = 28
    btc_positive_blocks: int = 2
    breadth_lookback_days: int = 30
    breadth_min_positive: int = 4


def _t_stat(values):
    arr = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
    if len(arr) < 2 or arr.std(ddof=1) <= 0:
        return 0.0
    return float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr))))


def _sign_flip_p(values, runs=4000):
    arr = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
    if not len(arr):
        return 1.0
    observed = float(arr.mean())
    rng = np.random.default_rng(20260904)
    exceed = 0
    for _ in range(runs):
        signs = rng.choice((-1.0, 1.0), size=len(arr))
        if float(np.mean(arr * signs)) >= observed:
            exceed += 1
    return float((exceed + 1) / (runs + 1))


def build_regime_mask(candles, btc_rows, overlay: OverlayParams):
    grid, aligned = align_candles(candles)
    btc_by_ts = {int(round(bar.ts)): bar for bar in btc_rows}
    btc_close = []
    for ts in grid:
        bar = btc_by_ts.get(int(round(ts)))
        btc_close.append(float(bar.close) if bar is not None else float("nan"))

    mask: Dict[int, bool] = {}
    detail = {}
    need = max(
        overlay.breadth_lookback_days,
        overlay.btc_block_days * overlay.btc_positive_blocks,
    )
    for i in range(len(grid)):
        if i < need:
            mask[i] = False
            continue

        btc_ok = True
        btc_block_returns = []
        for block in range(overlay.btc_positive_blocks):
            hi = i - block * overlay.btc_block_days
            lo = i - (block + 1) * overlay.btc_block_days
            a, b = btc_close[lo], btc_close[hi]
            if not (math.isfinite(a) and math.isfinite(b) and a > 0):
                btc_ok = False
                btc_block_returns.append(float("nan"))
                continue
            r = b / a - 1.0
            btc_block_returns.append(r)
            if r < 0:
                btc_ok = False

        positive = 0
        for rows in aligned.values():
            old = float(rows[i - overlay.breadth_lookback_days].close)
            new = float(rows[i].close)
            r = new / old - 1.0 if old > 0 else float("nan")
            if math.isfinite(r) and r > 0:
                positive += 1

        breadth_ok = positive >= overlay.breadth_min_positive
        mask[i] = bool(btc_ok and breadth_ok)
        detail[i] = {
            "btc_ok": float(btc_ok),
            "breadth_positive": float(positive),
            "breadth_ok": float(breadth_ok),
            "btc_latest_block_return": float(btc_block_returns[0]) if btc_block_returns else float("nan"),
        }
    return grid, aligned, mask, detail


def run_overlay(candles, btc_rows, funding, params, overlay, *,
                starting_equity, trade_start, trade_end, minimums, steps):
    _, _, mask, _ = build_regime_mask(candles, btc_rows, overlay)
    original = tsmom.choose_target

    def filtered_choose_target(aligned, atrs, i, p, *, times=None,
                               funding_bps_by_day=None):
        target = original(
            aligned, atrs, i, p, times=times,
            funding_bps_by_day=funding_bps_by_day,
        )
        if target is None:
            return None
        return target if mask.get(i, False) else None

    tsmom.choose_target = filtered_choose_target
    try:
        return tsmom.run(
            candles, funding, params,
            starting_equity=starting_equity,
            trade_start=trade_start,
            trade_end=trade_end,
            min_notional_by_symbol=minimums,
            qty_step_by_symbol=steps,
            min_notional_max_risk_pct=0.03,
        )
    finally:
        tsmom.choose_target = original


def enrich(sim, equity):
    result = stats(sim, equity)
    nets = np.asarray([t.net_pnl for t in sim.trades], dtype=float)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    result.update({
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "net_pnl": float(sim.final_equity - equity),
        "final_equity": float(sim.final_equity),
        "avg_win_usd": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_usd": float(losses.mean()) if len(losses) else 0.0,
        "estimated_monthly_pnl": ((sim.final_equity - equity) / days * 30.44
                                  if days > 0 else 0.0),
        "trades_per_month": (len(nets) / days * 30.44 if days > 0 else 0.0),
        "exit_reasons": dict(sorted(Counter(t.reason for t in sim.trades).items())),
        "blocked_min_notional": int(sim.blocked_min_notional),
        "floored_min_notional": int(sim.floored_min_notional),
    })
    return result


def evaluate_overlay(candles, btc_rows, funding, minimums, steps,
                     params, overlay, equity, start, end):
    sim = run_overlay(
        candles, btc_rows, funding, params, overlay,
        starting_equity=equity, trade_start=start, trade_end=end,
        minimums=minimums, steps=steps,
    )
    return enrich(sim, equity), sim


def evaluate_base(candles, funding, minimums, steps, params, equity, start, end):
    sim = tsmom.run(
        candles, funding, params, starting_equity=equity,
        trade_start=start, trade_end=end,
        min_notional_by_symbol=minimums,
        qty_step_by_symbol=steps,
        min_notional_max_risk_pct=0.03,
    )
    return enrich(sim, equity), sim


def diagnostics(candles, btc_rows, funding, params, overlay, start, end):
    grid, aligned, mask, detail = build_regime_mask(candles, btc_rows, overlay)
    atrs = {symbol: atr_series(rows, params.atr_period)
            for symbol, rows in aligned.items()}
    reviewed = base_candidates = regime_candidates = regime_blocks = 0
    btc_pass_reviews = breadth_pass_reviews = both_pass_reviews = 0
    forward = []
    cadence = max(1, params.rebalance_bars)
    for i in range(start, end):
        if int(grid[i] // 86400) % cadence != params.rebalance_offset % cadence:
            continue
        reviewed += 1
        d = detail.get(i, {})
        btc_pass_reviews += int(bool(d.get("btc_ok", 0.0)))
        breadth_pass_reviews += int(bool(d.get("breadth_ok", 0.0)))
        both_pass_reviews += int(bool(mask.get(i, False)))
        target = tsmom.choose_target(
            aligned, atrs, i, params, times=grid,
            funding_bps_by_day=funding,
        )
        if target is None:
            continue
        base_candidates += 1
        if not mask.get(i, False):
            regime_blocks += 1
            continue
        regime_candidates += 1
        entry_i, exit_i = i + 1, i + 31
        if exit_i < end:
            entry = float(aligned[target.symbol][entry_i].open)
            exit_price = float(aligned[target.symbol][exit_i].open)
            if entry > 0:
                gross = exit_price / entry - 1.0
                forward.append(gross - 2.0 * params.cost_bps_per_side / 1e4)
    arr = np.asarray(forward, dtype=float)
    return {
        "reviewed_rebalances": reviewed,
        "btc_regime_pass_reviews": btc_pass_reviews,
        "breadth_pass_reviews": breadth_pass_reviews,
        "both_regime_pass_reviews": both_pass_reviews,
        "base_30d_candidates": base_candidates,
        "regime_blocks": regime_blocks,
        "overlay_candidates": regime_candidates,
        "forward_30d_observations": int(len(arr)),
        "forward_30d_fee_net_mean_pct": float(arr.mean() * 100) if len(arr) else 0.0,
        "forward_30d_fee_net_median_pct": float(np.median(arr) * 100) if len(arr) else 0.0,
        "forward_30d_positive_pct": float(np.mean(arr > 0) * 100) if len(arr) else 0.0,
        "forward_30d_t": _t_stat(arr),
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
        raise SystemExit("this frozen study requires exactly six tradable symbols")

    print("=== FROZEN BTC REGIME + SIX-COIN BREADTH OVERLAY ===")
    print("    base: 30d long/cash TSMOM, weekly review, next-open fill")
    print("    BTC state: two consecutive non-negative 28d return blocks (UP->UP)")
    print("    breadth: >=4/6 symbols with positive raw 30d return")
    print("    regime OFF => cash; one position; DCA=0; leverage <=1x")
    print("    development 75%; final 25% sealed until all dev gates pass")
    print(f"    cost={a.cost_bps_side:g} bps/side; stress={a.stress_cost_bps_side:g} bps/side")

    candles, funding = fetch_inputs(symbols, a.months)
    print(f"\n=== {BTC_SYMBOL} public regime history ===")
    btc_rows = backtest_breakout.fetch(BTC_SYMBOL, "1d", a.months)
    now = time.time()
    candles = {
        symbol: [bar for bar in rows if bar.ts + 86400 <= now]
        for symbol, rows in candles.items()
    }
    btc_rows = [bar for bar in btc_rows if bar.ts + 86400 <= now]

    grid, _ = align_candles(candles)
    primary = TSMOMParams(
        lookback=30, vol_lookback=30, signal_threshold=0.25,
        rebalance_bars=7, risk_pct=0.02, annual_vol_target=0.50,
        max_leverage=1.0, stop_atr=3.0, trail_start_atr=3.0,
        trail_atr=2.5, cost_bps_per_side=a.cost_bps_side,
        allow_short=False,
    )
    primary_overlay = OverlayParams()
    warm = max(
        primary.lookback, primary.vol_lookback, primary.atr_period,
        primary_overlay.btc_block_days * primary_overlay.btc_positive_blocks,
        primary_overlay.breadth_lookback_days,
    ) + 10
    if len(grid) < warm + 730:
        raise SystemExit(f"only {len(grid)} aligned completed daily bars")

    minimums, steps = fetch_execution_filters(symbols)
    n = len(grid)
    final_start = warm + int((n - warm) * 0.75)

    baseline_dev, _ = evaluate_base(
        candles, funding, minimums, steps, primary,
        a.starting_equity, warm, final_start,
    )
    development, dev_sim = evaluate_overlay(
        candles, btc_rows, funding, minimums, steps, primary,
        primary_overlay, a.starting_equity, warm, final_start,
    )
    diag = diagnostics(
        candles, btc_rows, funding, primary, primary_overlay,
        warm, final_start,
    )

    print("\nDEVELOPMENT 75%")
    print("  BASE     " + fmt(baseline_dev))
    print("  OVERLAY  " + fmt(development))
    print(f"  overlay exits={development['exit_reasons']} blocks={development['blocked_min_notional']} "
          f"floors={development['floored_min_notional']}")
    print("\nREGIME / FORWARD DIAGNOSTICS — DEVELOPMENT ONLY")
    print(f"  reviews={diag['reviewed_rebalances']} BTC-pass={diag['btc_regime_pass_reviews']} "
          f"breadth-pass={diag['breadth_pass_reviews']} both={diag['both_regime_pass_reviews']}")
    print(f"  base candidates={diag['base_30d_candidates']} regime-blocks={diag['regime_blocks']} "
          f"overlay candidates={diag['overlay_candidates']}")
    print(f"  30d forward n={diag['forward_30d_observations']} "
          f"mean={diag['forward_30d_fee_net_mean_pct']:+.3f}% "
          f"median={diag['forward_30d_fee_net_median_pct']:+.3f}% "
          f"positive={diag['forward_30d_positive_pct']:.1f}% t={diag['forward_30d_t']:+.2f}")

    boundaries = np.linspace(warm, final_start, 4, dtype=int)
    folds = []
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for number, (lo, hi) in enumerate(zip(boundaries[:-1], boundaries[1:]), 1):
        r, _ = evaluate_overlay(
            candles, btc_rows, funding, minimums, steps, primary,
            primary_overlay, a.starting_equity, int(lo), int(hi),
        )
        folds.append(r)
        print(f"  fold {number}/3 " + fmt(r))

    variants = {
        "breadth_3_of_6": replace(primary_overlay, breadth_min_positive=3),
        "breadth_5_of_6": replace(primary_overlay, breadth_min_positive=5),
        "btc_one_28d_block": replace(primary_overlay, btc_positive_blocks=1),
        "btc_two_21d_blocks": replace(primary_overlay, btc_block_days=21),
    }
    neighbors = {}
    print("\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY")
    for name, overlay in variants.items():
        r, _ = evaluate_overlay(
            candles, btc_rows, funding, minimums, steps, primary,
            overlay, a.starting_equity, warm, final_start,
        )
        neighbors[name] = r
        print(f"  {name:22s} " + fmt(r))

    stress_params = replace(primary, cost_bps_per_side=a.stress_cost_bps_side)
    stress, _ = evaluate_overlay(
        candles, btc_rows, funding, minimums, steps, stress_params,
        primary_overlay, a.starting_equity, warm, final_start,
    )
    sign_p = _sign_flip_p([trade.net_pnl for trade in dev_sim.trades])
    print(f"\nCOST STRESS {a.stress_cost_bps_side:g} bps/side")
    print("  " + fmt(stress))
    print(f"  trade sign-flip p={sign_p:.3f}")

    positive_folds = sum(r["net_pnl"] > 0 for r in folds)
    positive_rules = sum(
        r["net_pnl"] > 0 for r in [development, *neighbors.values()]
    )
    checks = {
        "development_fee_net_positive": development["net_pnl"] > 0,
        "development_profit_factor_at_least_1_20": development["profit_factor"] >= 1.20,
        "development_at_least_8_trades": development["trades"] >= 8,
        "at_least_2_of_3_folds_positive": positive_folds >= 2,
        "at_least_3_of_5_neighbor_rules_positive": positive_rules >= 3,
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

    final = final_stress = baseline_final = None
    final_checks = {}
    if development_passed:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        baseline_final, _ = evaluate_base(
            candles, funding, minimums, steps, primary,
            a.starting_equity, final_start, n,
        )
        final, _ = evaluate_overlay(
            candles, btc_rows, funding, minimums, steps, primary,
            primary_overlay, a.starting_equity, final_start, n,
        )
        final_stress, _ = evaluate_overlay(
            candles, btc_rows, funding, minimums, steps, stress_params,
            primary_overlay, a.starting_equity, final_start, n,
        )
        final_checks = {
            "final_fee_net_positive": final["net_pnl"] > 0,
            "final_profit_factor_at_least_1_10": final["profit_factor"] >= 1.10,
            "final_at_least_3_trades": final["trades"] >= 3,
            "final_positive_at_stressed_cost": final_stress["net_pnl"] > 0,
            "final_drawdown_below_20pct": final["max_drawdown_pct"] < 20.0,
        }
        print("  BASE FINAL " + fmt(baseline_final))
        print("  OVERLAY    " + fmt(final))
        print("  STRESS     " + fmt(final_stress))
        for name, passed in final_checks.items():
            print(f"  {'PASS' if passed else 'FAIL':4s} {name}")
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")

    passed = development_passed and all(final_checks.values())
    payload = {
        "schema": 1,
        "strategy": "tsmom_30d_btc_upup_breadth",
        "symbols": symbols,
        "btc_regime_symbol": BTC_SYMBOL,
        "months": a.months,
        "starting_equity": a.starting_equity,
        "base_params": asdict(primary),
        "overlay_params": asdict(primary_overlay),
        "baseline_development": baseline_dev,
        "development": development,
        "diagnostics": diag,
        "development_folds": folds,
        "neighbors": neighbors,
        "stress": stress,
        "sign_flip_p": sign_p,
        "development_checks": checks,
        "development_passed": development_passed,
        "sealed_final_opened": development_passed,
        "baseline_final": baseline_final,
        "final": final,
        "final_stress": final_stress,
        "final_checks": final_checks,
        "passed": passed,
        "live_orders_sent": 0,
        "research_only": True,
    }
    summary = {
        "strategy": payload["strategy"],
        "development_passed": development_passed,
        "sealed_final_opened": development_passed,
        "passed": passed,
        "dev_net_pnl": development["net_pnl"],
        "dev_pf": development["profit_factor"],
        "dev_cagr_pct": development["cagr_pct"],
        "dev_dd_pct": development["max_drawdown_pct"],
        "dev_trades": development["trades"],
        "baseline_dev_net_pnl": baseline_dev["net_pnl"],
        "stress_net_pnl": stress["net_pnl"],
        "sign_flip_p": sign_p,
        "positive_folds": positive_folds,
        "positive_rules": positive_rules,
        "final_net_pnl": final["net_pnl"] if final else None,
        "final_pf": final["profit_factor"] if final else None,
        "live_orders_sent": 0,
    }
    print("\n[btc-regime-breadth-summary] " + json.dumps(summary, sort_keys=True))
    print("\n" + ("PASS: eligible for frozen paper-only observation."
                    if passed else
                    "REJECTED: do not replace the admitted base 30-day TSMOM candidate."))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        print(f"wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
