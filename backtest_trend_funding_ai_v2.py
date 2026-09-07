#!/usr/bin/env python3
"""Research-only AI-assisted optimizer for trend + funding-crowding.

No live orders. Uses the existing frozen backtest engine, expands the candidate
space, and uses a RandomForest meta-model only to rank parameter configurations
on chronological development folds. The final 25% remains sealed until the
chosen configuration passes development robustness gates.
"""
from __future__ import annotations

import json
from dataclasses import replace
from itertools import product

import numpy as np
from sklearn.ensemble import RandomForestRegressor

import tsmom
from backtest_tsmom import fetch_inputs
from paper_tsmom import fetch_execution_filters
from backtest_trend_funding_ensemble import (
    DEFAULT_SYMBOLS, EnsembleParams, evaluate, evaluate_base, fmt,
)


def main():
    symbols = DEFAULT_SYMBOLS.split(",")
    equity = 15.0
    months = 48.0
    candles, funding = fetch_inputs(symbols, months)
    minimums, steps = fetch_execution_filters(symbols)

    # Completed daily bars only; identical frozen execution assumptions.
    import time
    now = time.time()
    candles = {s: [b for b in rows if b.ts + 86400 <= now] for s, rows in candles.items()}
    grid, _ = tsmom.align_candles(candles)
    p0 = tsmom.TSMOMParams(
        lookback=30, vol_lookback=30, signal_threshold=0.25,
        rebalance_bars=7, risk_pct=0.02, annual_vol_target=0.50,
        max_leverage=1.0, stop_atr=3.0, trail_start_atr=3.0,
        trail_atr=2.5, cost_bps_per_side=7.0, allow_short=False,
    )
    warm = max(p0.lookback, p0.vol_lookback, p0.atr_period, 3) + 10
    n = len(grid)
    final_start = warm + int((n - warm) * 0.75)
    if n < warm + 730:
        raise SystemExit(f"only {n} aligned completed daily bars")

    # Broader but still economically sensible search. Threshold controls how
    # many candidates enter the ranking; weights/lookback control crowding use.
    thresholds = [0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    weights = [(1.00, 0.00), (0.90, 0.10), (0.80, 0.20), (0.70, 0.30),
               (0.60, 0.40), (0.50, 0.50)]
    funding_days = [3, 5, 7, 10, 14]

    bounds = np.linspace(warm, final_start, 4, dtype=int)
    rows = []
    print("=== AI-ASSISTED TREND + FUNDING V2 ===")
    print("Development only: 75% / chronological 3-fold CV; final 25% sealed")

    for threshold, (tw, fw), fd in product(thresholds, weights, funding_days):
        p = replace(p0, signal_threshold=threshold)
        ep = EnsembleParams(funding_days=fd, trend_weight=tw, funding_weight=fw)
        fold_results = []
        for lo, hi in zip(bounds[:-1], bounds[1:]):
            r, _ = evaluate(candles, funding, minimums, steps, p, ep, equity, int(lo), int(hi))
            fold_results.append(r)
        pf = np.array([r["profit_factor"] for r in fold_results], float)
        net = np.array([r["net_pnl"] for r in fold_results], float)
        trades = np.array([r["trades"] for r in fold_results], float)
        dd = np.array([r["max_drawdown_pct"] for r in fold_results], float)
        # Penalize instability, sparse trading, and drawdown. This is a ranking
        # objective only; admission still uses explicit gates below.
        score = float(net.mean() + 0.15 * np.median(net) + 0.02 * np.log1p(trades.sum())
                      - 0.015 * dd.max() - 0.25 * pf.std())
        rows.append({"threshold": threshold, "trend_weight": tw, "funding_weight": fw,
                     "funding_days": fd, "score": score, "net_mean": float(net.mean()),
                     "pf_mean": float(pf.mean()), "pf_min": float(pf.min()),
                     "trades": int(trades.sum()), "dd_max": float(dd.max()),
                     "positive_folds": int((net > 0).sum())})

    # Meta-model learns the relationship between parameter choices and CV score,
    # then ranks the same pre-registered grid. It is deliberately not trained on
    # the sealed final segment.
    X = np.array([[r["threshold"], r["trend_weight"], r["funding_days"]] for r in rows])
    y = np.array([r["score"] for r in rows])
    model = RandomForestRegressor(n_estimators=300, random_state=20260907,
                                  min_samples_leaf=3, max_features=0.8)
    model.fit(X, y)
    for r in rows:
        r["ai_predicted_score"] = float(model.predict([[r["threshold"], r["trend_weight"], r["funding_days"]]])[0])

    rows.sort(key=lambda r: (r["ai_predicted_score"], r["score"]), reverse=True)
    print("\nTOP 10 DEVELOPMENT CONFIGURATIONS")
    for r in rows[:10]:
        print(f"  th={r['threshold']:.2f} w={r['trend_weight']:.1f}/{r['funding_weight']:.1f} "
              f"fd={r['funding_days']:2d} trades={r['trades']:2d} PFmean={r['pf_mean']:.2f} "
              f"netmean=${r['net_mean']:+.4f} DDmax={r['dd_max']:.2f}% "
              f"folds={r['positive_folds']}/3 AI={r['ai_predicted_score']:+.4f}")

    # Select only from configs with basic robustness. Prefer more trades when
    # scores are close so the optimizer does not exploit tiny samples.
    eligible = [r for r in rows if r["positive_folds"] >= 2 and r["trades"] >= 12
                and r["dd_max"] < 20 and r["net_mean"] > 0]
    if not eligible:
        print("\nNO DEVELOPMENT CONFIG PASSED PRE-FILTERS; final remains sealed")
        return 2
    best = eligible[0]
    p = replace(p0, signal_threshold=best["threshold"])
    ep = EnsembleParams(funding_days=best["funding_days"],
                        trend_weight=best["trend_weight"],
                        funding_weight=best["funding_weight"])
    dev, _ = evaluate(candles, funding, minimums, steps, p, ep, equity, warm, final_start)
    stress_p = replace(p, cost_bps_per_side=10.0)
    stress, _ = evaluate(candles, funding, minimums, steps, stress_p, ep, equity, warm, final_start)
    baseline, _ = evaluate_base(candles, funding, minimums, steps, p0, equity, warm, final_start)

    gates = {
        "dev_net_positive": dev["net_pnl"] > 0,
        "dev_pf_ge_1_20": dev["profit_factor"] >= 1.20,
        "dev_trades_ge_8": dev["trades"] >= 8,
        "dev_dd_lt_20": dev["max_drawdown_pct"] < 20,
        "stress_net_positive": stress["net_pnl"] > 0,
        "beats_baseline_net": dev["net_pnl"] > baseline["net_pnl"],
    }
    print("\nSELECTED CONFIG")
    print(best)
    print("  BASE     " + fmt(baseline))
    print("  AI-V2    " + fmt(dev))
    print("  STRESS   " + fmt(stress))
    for k, v in gates.items():
        print(f"  {'PASS' if v else 'FAIL':4s} {k}")
    passed = all(gates.values())

    if passed:
        final, _ = evaluate(candles, funding, minimums, steps, p, ep, equity, final_start, n)
        final_stress, _ = evaluate(candles, funding, minimums, steps, stress_p, ep, equity, final_start, n)
        print("\nSEALED FINAL 25% OPENED")
        print("  FINAL       " + fmt(final))
        print("  FINAL STRESS" + fmt(final_stress))
    else:
        final = final_stress = None
        print("\nDEVELOPMENT FAILED — sealed final 25% remains unopened")

    out = {
        "strategy": "trend_funding_crowding_ai_v2",
        "research_only": True,
        "live_orders_sent": 0,
        "selected": best,
        "baseline": baseline,
        "development": dev,
        "stress": stress,
        "gates": gates,
        "development_passed": passed,
        "final": final,
        "final_stress": final_stress,
        "top10": rows[:10],
    }
    with open("TREND_FUNDING_AI_V2_SUMMARY.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
