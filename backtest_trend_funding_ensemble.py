#!/usr/bin/env python3
"""Sealed $15 validation for a trend + funding-crowding ensemble.

Research only: public Binance USD-M daily candles and settled funding, no
order-capable exchange client, no live orders.

Frozen primary hypothesis:
- Universe: SOL, SUI, BNB, XRP, TRX, DOGE.
- Weekly review after a completed UTC daily close; earliest fill next open.
- Candidate set: existing 30-day volatility-normalised momentum score >=0.25.
- For candidates, rank trend strength high-to-low and trailing seven completed
  days of settled funding low-to-high. Composite = 70% trend rank + 30% low
  funding-crowding rank; buy the highest composite score.
- Funding changes cross-sectional selection; it is NOT a tuned hard threshold.
- Long/cash only, one position, DCA=0, 2% planned stop risk, <=1x leverage,
  3 ATR initial stop, 3 ATR trail activation, 2.5 ATR trail.
- 7 bps/side execution cost plus exact settled funding; 10 bps/side stress.
- $15 wallet uses current exchange minimum notional / quantity steps, and may
  floor to the exchange minimum only while planned stop risk remains <=3%.

Neighbours vary one design choice only: trend/funding weights 80/20 and 60/40,
or funding lookback 3 and 14 days. First 75% after warm-up is development;
final 25% remains sealed unless all pre-registered development gates pass.
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
from tsmom import (
    TSMOMParams, Target, align_candles, momentum_score, position_leverage, stats,
)

DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"


@dataclass(frozen=True)
class EnsembleParams:
    funding_days: int = 7
    trend_weight: float = 0.70
    funding_weight: float = 0.30


def _rank(values, *, high_good: bool):
    """Return deterministic 0..1 ranks where 1 is best."""
    if not values:
        return {}
    ordered = sorted(values, key=lambda s: (values[s], s), reverse=high_good)
    if len(ordered) == 1:
        return {ordered[0]: 1.0}
    return {symbol: 1.0 - idx / (len(ordered) - 1) for idx, symbol in enumerate(ordered)}


def _annual_vol(rows, i: int, lookback: int) -> float:
    if i < lookback or lookback < 2:
        return float("nan")
    closes = np.asarray([float(x.close) for x in rows[i - lookback:i + 1]], dtype=float)
    if np.any(closes <= 0):
        return float("nan")
    ret = np.diff(np.log(closes))
    if len(ret) < 2:
        return float("nan")
    return float(ret.std(ddof=1) * math.sqrt(365.0))


def trailing_funding(funding, symbol: str, day: int, days: int) -> float:
    """Funding bps already settled in the completed UTC days ending at day."""
    rows = funding.get(symbol, {})
    return sum(float(rows.get(day - lag * 86400, 0.0)) for lag in range(max(1, days)))


def selection_inputs(aligned, i, p, ensemble, times, funding):
    trend = {}
    crowding = {}
    if times is None or i >= len(times):
        return trend, crowding
    day = int(times[i] // 86400 * 86400)
    for symbol, rows in aligned.items():
        closes = [x.close for x in rows]
        score = momentum_score(closes, i, p)
        if not math.isfinite(score) or score < p.signal_threshold:
            continue
        trend[symbol] = float(score)
        crowding[symbol] = trailing_funding(
            funding or {}, symbol, day, ensemble.funding_days
        )
    return trend, crowding


def select_symbol(aligned, i, p, ensemble, times, funding):
    trend, crowding = selection_inputs(aligned, i, p, ensemble, times, funding)
    if not trend:
        return None, trend, crowding, {}
    tr = _rank(trend, high_good=True)
    fr = _rank(crowding, high_good=False)
    composite = {
        s: ensemble.trend_weight * tr[s] + ensemble.funding_weight * fr[s]
        for s in trend
    }
    symbol = max(composite, key=lambda s: (composite[s], trend[s], s))
    return symbol, trend, crowding, composite


def choose_ensemble_target(aligned, atrs, i, p, ensemble, *, times, funding):
    symbol, trend, _, composite = select_symbol(
        aligned, i, p, ensemble, times, funding
    )
    if symbol is None:
        return None
    atr = atrs[symbol][i] if i < len(atrs[symbol]) else None
    if atr is None or not math.isfinite(float(atr)) or float(atr) <= 0:
        return None
    ann = _annual_vol(aligned[symbol], i, p.vol_lookback)
    lev = position_leverage(float(aligned[symbol][i].close), float(atr), ann, p)
    if lev <= 0:
        return None
    # Keep the target score meaningful for diagnostics while execution sizing
    # still comes from the unchanged TSMOM risk engine.
    return Target(
        symbol=symbol, side=1, score=float(composite[symbol]),
        leverage=float(lev), atr=float(atr),
    )


def run_ensemble(candles, funding, params, ensemble, *, equity, start, end,
                 minimums, steps):
    original = tsmom.choose_target

    def chooser(aligned, atrs, i, p, *, times=None, funding_bps_by_day=None):
        return choose_ensemble_target(
            aligned, atrs, i, p, ensemble,
            times=times, funding=funding_bps_by_day or {},
        )

    tsmom.choose_target = chooser
    try:
        return tsmom.run(
            candles, funding, params, starting_equity=equity,
            trade_start=start, trade_end=end,
            min_notional_by_symbol=minimums,
            qty_step_by_symbol=steps,
            min_notional_max_risk_pct=0.03,
        )
    finally:
        tsmom.choose_target = original


def enrich(sim, equity):
    result = stats(sim, equity)
    nets = np.asarray([x.net_pnl for x in sim.trades], dtype=float)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    result.update({
        "net_pnl": float(sim.final_equity - equity),
        "final_equity": float(sim.final_equity),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "avg_win_usd": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_usd": float(losses.mean()) if len(losses) else 0.0,
        "estimated_monthly_pnl": ((sim.final_equity - equity) / days * 30.44
                                  if days > 0 else 0.0),
        "exit_reasons": dict(sorted(Counter(x.reason for x in sim.trades).items())),
        "blocked_min_notional": int(sim.blocked_min_notional),
        "floored_min_notional": int(sim.floored_min_notional),
    })
    return result


def evaluate(candles, funding, minimums, steps, params, ensemble, equity, start, end):
    sim = run_ensemble(
        candles, funding, params, ensemble, equity=equity,
        start=start, end=end, minimums=minimums, steps=steps,
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


def diagnostics(candles, funding, params, ensemble, start, end):
    grid, aligned = align_candles(candles)
    reviewed = selected = cash = 0
    forward = []
    funding_selected = []
    candidate_counts = []
    cadence = max(1, params.rebalance_bars)
    for i in range(start, end):
        if int(grid[i] // 86400) % cadence != params.rebalance_offset % cadence:
            continue
        reviewed += 1
        symbol, trend, crowding, _ = select_symbol(
            aligned, i, params, ensemble, grid, funding
        )
        candidate_counts.append(len(trend))
        if symbol is None:
            cash += 1
            continue
        selected += 1
        funding_selected.append(crowding[symbol])
        entry_i, exit_i = i + 1, i + 31
        if exit_i < end:
            entry = float(aligned[symbol][entry_i].open)
            exit_price = float(aligned[symbol][exit_i].open)
            if entry > 0:
                forward.append(
                    exit_price / entry - 1.0 - 2.0 * params.cost_bps_per_side / 1e4
                )
    arr = np.asarray(forward, dtype=float)
    t = 0.0
    if len(arr) >= 2 and arr.std(ddof=1) > 0:
        t = float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr))))
    return {
        "reviewed_rebalances": reviewed,
        "selected_reviews": selected,
        "cash_reviews": cash,
        "mean_eligible_candidates": float(np.mean(candidate_counts)) if candidate_counts else 0.0,
        "mean_selected_trailing_funding_bps": float(np.mean(funding_selected)) if funding_selected else 0.0,
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

    symbols = [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
    if len(symbols) != 6:
        raise SystemExit("frozen study requires exactly six symbols")

    print("=== FROZEN TREND + FUNDING-CROWDING ENSEMBLE ===")
    print("  eligible: 30d vol-normalised trend >=0.25")
    print("  selection: 70% trend rank + 30% low trailing-7d funding rank")
    print("  weekly completed-close review; next-open fill; long/cash; DCA=0; <=1x")
    print("  development 75%; final 25% sealed until all dev gates pass")

    candles, funding = fetch_inputs(symbols, a.months)
    now = time.time()
    candles = {
        s: [bar for bar in rows if bar.ts + 86400 <= now]
        for s, rows in candles.items()
    }
    grid, _ = align_candles(candles)
    params = TSMOMParams(
        lookback=30, vol_lookback=30, signal_threshold=0.25,
        rebalance_bars=7, risk_pct=0.02, annual_vol_target=0.50,
        max_leverage=1.0, stop_atr=3.0, trail_start_atr=3.0,
        trail_atr=2.5, cost_bps_per_side=a.cost_bps_side,
        allow_short=False,
    )
    primary = EnsembleParams()
    warm = max(params.lookback, params.vol_lookback, params.atr_period,
               primary.funding_days) + 10
    if len(grid) < warm + 730:
        raise SystemExit(f"only {len(grid)} aligned completed daily bars")
    minimums, steps = fetch_execution_filters(symbols)
    n = len(grid)
    final_start = warm + int((n - warm) * 0.75)

    baseline_dev, _ = evaluate_base(
        candles, funding, minimums, steps, params,
        a.starting_equity, warm, final_start,
    )
    development, dev_sim = evaluate(
        candles, funding, minimums, steps, params, primary,
        a.starting_equity, warm, final_start,
    )
    diag = diagnostics(candles, funding, params, primary, warm, final_start)

    print("\nDEVELOPMENT 75%")
    print("  BASE     " + fmt(baseline_dev))
    print("  ENSEMBLE " + fmt(development))
    print(f"  exits={development['exit_reasons']} blocks={development['blocked_min_notional']} "
          f"floors={development['floored_min_notional']}")
    print(f"  reviews={diag['reviewed_rebalances']} selected={diag['selected_reviews']} "
          f"cash={diag['cash_reviews']} eligible_mean={diag['mean_eligible_candidates']:.2f}")
    print(f"  forward n={diag['forward_30d_observations']} "
          f"mean={diag['forward_30d_fee_net_mean_pct']:+.3f}% "
          f"median={diag['forward_30d_fee_net_median_pct']:+.3f}% "
          f"positive={diag['forward_30d_positive_pct']:.1f}% t={diag['forward_30d_t']:+.2f}")

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
        "trend80_funding20": replace(primary, trend_weight=0.80, funding_weight=0.20),
        "trend60_funding40": replace(primary, trend_weight=0.60, funding_weight=0.40),
        "funding_3d": replace(primary, funding_days=3),
        "funding_14d": replace(primary, funding_days=14),
    }
    neighbors = {}
    print("\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY")
    for name, ensemble in variants.items():
        r, _ = evaluate(
            candles, funding, minimums, steps, params, ensemble,
            a.starting_equity, warm, final_start,
        )
        neighbors[name] = r
        print(f"  {name:22s} " + fmt(r))

    stress_params = replace(params, cost_bps_per_side=a.stress_cost_bps_side)
    stress, _ = evaluate(
        candles, funding, minimums, steps, stress_params, primary,
        a.starting_equity, warm, final_start,
    )
    sign_p = _sign_flip_p([x.net_pnl for x in dev_sim.trades])
    print(f"\nCOST STRESS {a.stress_cost_bps_side:g} bps/side")
    print("  " + fmt(stress))
    print(f"  trade sign-flip p={sign_p:.3f}")

    positive_folds = sum(x["net_pnl"] > 0 for x in folds)
    positive_rules = sum(x["net_pnl"] > 0 for x in [development, *neighbors.values()])
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
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s} {name}")

    final = final_stress = baseline_final = None
    final_checks = {}
    if development_passed:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        baseline_final, _ = evaluate_base(
            candles, funding, minimums, steps, params,
            a.starting_equity, final_start, n,
        )
        final, _ = evaluate(
            candles, funding, minimums, steps, params, primary,
            a.starting_equity, final_start, n,
        )
        final_stress, _ = evaluate(
            candles, funding, minimums, steps, stress_params, primary,
            a.starting_equity, final_start, n,
        )
        print("  BASE FINAL " + fmt(baseline_final))
        print("  ENSEMBLE   " + fmt(final))
        print("  STRESS     " + fmt(final_stress))
        final_checks = {
            "final_fee_net_positive": final["net_pnl"] > 0,
            "final_profit_factor_at_least_1_10": final["profit_factor"] >= 1.10,
            "final_at_least_3_trades": final["trades"] >= 3,
            "final_positive_at_stressed_cost": final_stress["net_pnl"] > 0,
            "final_drawdown_below_20pct": final["max_drawdown_pct"] < 20.0,
        }
        for name, ok in final_checks.items():
            print(f"  {'PASS' if ok else 'FAIL':4s} {name}")
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")

    passed = development_passed and all(final_checks.values())
    summary = {
        "strategy": "trend_funding_crowding_ensemble",
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
        "forward_30d_mean_pct": diag["forward_30d_fee_net_mean_pct"],
        "forward_30d_t": diag["forward_30d_t"],
        "final_net_pnl": final["net_pnl"] if final else None,
        "final_pf": final["profit_factor"] if final else None,
        "live_orders_sent": 0,
    }
    print("\n[trend-funding-summary] " + json.dumps(summary, sort_keys=True))
    print("\n" + ("PASS: eligible for paper-only observation."
                    if passed else
                    "REJECTED: do not replace the admitted base 30-day TSMOM candidate."))

    if a.json:
        payload = {
            "schema": 1,
            "summary": summary,
            "symbols": symbols,
            "months": a.months,
            "starting_equity": a.starting_equity,
            "base_params": asdict(params),
            "ensemble_params": asdict(primary),
            "baseline_development": baseline_dev,
            "development": development,
            "diagnostics": diag,
            "folds": folds,
            "neighbors": neighbors,
            "stress": stress,
            "development_checks": checks,
            "baseline_final": baseline_final,
            "final": final,
            "final_stress": final_stress,
            "final_checks": final_checks,
            "research_only": True,
            "live_orders_sent": 0,
        }
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        print(f"wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
