#!/usr/bin/env python3
"""Compare the admitted 30d long/cash TSMOM on Binance spot vs USD-M perpetuals.

Research-only. Public endpoints only; no order-capable client and no live orders.

Frozen comparison:
- Same six-symbol universe: SOL, SUI, BNB, XRP, TRX, DOGE.
- Same completed-daily 30d volatility-normalised TSMOM, weekly review,
  next-day-open fills, one long/cash position, DCA=0, <=1x.
- Same 2% stop-risk sizing and ATR stop/trail rules.
- Perpetual execution cost: 7 bps/side (5 bps taker + 2 bps adverse fill)
  plus exact settled historical funding.
- Spot execution cost: 12 bps/side (published standard 10 bps trading fee
  + 2 bps adverse fill), no funding. A 15 bps/side spot stress is reported.
- Current exchange minimum-notional and quantity-step filters are fetched
  separately for each venue. Both venues are evaluated on the exact same
  intersected daily timestamps.
- $15 starting equity; a minimum-order floor is allowed only when planned
  stop risk stays <=3% of equity.

This is a venue/execution robustness study, not a new signal search. The first
75% after warm-up is development; final 25% is opened only if spot clears the
pre-registered development gates.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from collections import Counter
from dataclasses import replace
from typing import Dict, List

import numpy as np

from backtest_tsmom import fetch_inputs
from breakout import Candle
from paper_tsmom import fetch_execution_filters as fetch_perp_filters
from tsmom import TSMOMParams, run, stats

SPOT_KLINES = "https://api.binance.com/api/v3/klines"
SPOT_INFO = "https://api.binance.com/api/v3/exchangeInfo"
DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"
DAY_MS = 86_400_000


def fetch_spot(symbol: str, months: float) -> List[Candle]:
    end = int(time.time() * 1000)
    start = end - int(months * 30.44 * DAY_MS)
    out: Dict[int, Candle] = {}
    cursor = start
    while cursor < end:
        url = (f"{SPOT_KLINES}?symbol={symbol}&interval=1d"
               f"&startTime={cursor}&limit=1000")
        with urllib.request.urlopen(url, timeout=30) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = Candle(
                ts=int(r[0]) / 1000.0,
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]),
            )
        cursor = int(rows[-1][0]) + DAY_MS
        print(f"\r  SPOT {symbol}: {len(out)} bars", end="", flush=True)
        time.sleep(0.12)
    print()
    return [out[k] for k in sorted(out)]


def fetch_spot_filters(symbols):
    with urllib.request.urlopen(SPOT_INFO, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    wanted = set(symbols)
    minimums, steps = {}, {}
    for row in payload.get("symbols", []):
        symbol = row.get("symbol")
        if symbol not in wanted:
            continue
        filters = {f.get("filterType"): f for f in row.get("filters", [])}
        min_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        minimums[symbol] = float(min_filter.get("minNotional", 0.0))
        lot = filters.get("LOT_SIZE") or {}
        steps[symbol] = float(lot.get("stepSize", 0.0))
    missing = wanted.difference(minimums)
    if missing:
        raise SystemExit(f"spot exchangeInfo missing {sorted(missing)}")
    return minimums, steps


def completed(series, now):
    return {
        s: [bar for bar in rows if bar.ts + 86400 <= now]
        for s, rows in series.items()
    }


def intersect_series(spot, perp):
    common = None
    for rows in list(spot.values()) + list(perp.values()):
        ts = {int(round(x.ts)) for x in rows}
        common = ts if common is None else common.intersection(ts)
    common = sorted(common or ())
    if not common:
        raise SystemExit("no common spot/perpetual timestamps")
    allowed = set(common)
    def trim(series):
        return {
            s: [bar for bar in rows if int(round(bar.ts)) in allowed]
            for s, rows in series.items()
        }
    return trim(spot), trim(perp), [float(x) for x in common]


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


def evaluate(series, funding, filters, params, equity, start, end):
    minimums, steps = filters
    sim = run(
        series, funding, params, starting_equity=equity,
        trade_start=start, trade_end=end,
        min_notional_by_symbol=minimums,
        qty_step_by_symbol=steps,
        min_notional_max_risk_pct=0.03,
    )
    return enrich(sim, equity), sim


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
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    symbols = [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
    if len(symbols) != 6:
        raise SystemExit("frozen comparison requires exactly six symbols")

    print("=== FROZEN SPOT VS PERPETUAL 30D TSMOM COMPARISON ===")
    print("  perp cost=7 bps/side + exact funding")
    print("  spot cost=12 bps/side, no funding; stress=15 bps/side")
    print("  both use same timestamps, current venue minimums and $15 wallet")

    perp, funding = fetch_inputs(symbols, a.months)
    spot = {}
    for symbol in symbols:
        print(f"\n=== {symbol} spot history ===")
        spot[symbol] = fetch_spot(symbol, a.months)

    now = time.time()
    spot = completed(spot, now)
    perp = completed(perp, now)
    spot, perp, grid = intersect_series(spot, perp)

    perp_filters = fetch_perp_filters(symbols)
    spot_filters = fetch_spot_filters(symbols)

    perp_p = TSMOMParams(
        cost_bps_per_side=7.0, allow_short=False,
        risk_pct=0.02, max_leverage=1.0,
    )
    spot_p = replace(perp_p, cost_bps_per_side=12.0)
    spot_stress_p = replace(perp_p, cost_bps_per_side=15.0)

    warm = max(perp_p.lookback, perp_p.vol_lookback, perp_p.atr_period) + 10
    if len(grid) < warm + 730:
        raise SystemExit(f"only {len(grid)} common completed daily bars")
    n = len(grid)
    final_start = warm + int((n - warm) * 0.75)

    perp_dev, _ = evaluate(
        perp, funding, perp_filters, perp_p,
        a.starting_equity, warm, final_start,
    )
    spot_dev, _ = evaluate(
        spot, {}, spot_filters, spot_p,
        a.starting_equity, warm, final_start,
    )
    spot_stress_dev, _ = evaluate(
        spot, {}, spot_filters, spot_stress_p,
        a.starting_equity, warm, final_start,
    )
    print("\nDEVELOPMENT 75% — IDENTICAL DATES")
    print("  PERP " + fmt(perp_dev))
    print("  SPOT " + fmt(spot_dev))
    print("  SPOT STRESS " + fmt(spot_stress_dev))
    print(f"  spot minimums={spot_filters[0]}")
    print(f"  perp minimums={perp_filters[0]}")

    bounds = np.linspace(warm, final_start, 4, dtype=int)
    spot_folds, perp_folds = [], []
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for number, (lo, hi) in enumerate(zip(bounds[:-1], bounds[1:]), 1):
        sp, _ = evaluate(
            spot, {}, spot_filters, spot_p,
            a.starting_equity, int(lo), int(hi),
        )
        pp, _ = evaluate(
            perp, funding, perp_filters, perp_p,
            a.starting_equity, int(lo), int(hi),
        )
        spot_folds.append(sp)
        perp_folds.append(pp)
        print(f"  fold {number}/3 SPOT " + fmt(sp))
        print(f"             PERP " + fmt(pp))

    spot_positive_folds = sum(x["net_pnl"] > 0 for x in spot_folds)
    checks = {
        "spot_development_positive": spot_dev["net_pnl"] > 0,
        "spot_development_pf_at_least_1_15": spot_dev["profit_factor"] >= 1.15,
        "spot_development_at_least_8_trades": spot_dev["trades"] >= 8,
        "spot_at_least_2_of_3_folds_positive": spot_positive_folds >= 2,
        "spot_positive_at_15bps_stress": spot_stress_dev["net_pnl"] > 0,
        "spot_development_drawdown_below_20pct": spot_dev["max_drawdown_pct"] < 20.0,
        "spot_not_less_than_half_perp_net": (
            spot_dev["net_pnl"] >= 0.5 * max(perp_dev["net_pnl"], 0.0)
        ),
    }
    development_passed = all(checks.values())
    print("\nSPOT DEVELOPMENT ADMISSION HURDLES")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s} {name}")

    spot_final = perp_final = spot_final_stress = None
    final_checks = {}
    if development_passed:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        spot_final, _ = evaluate(
            spot, {}, spot_filters, spot_p,
            a.starting_equity, final_start, n,
        )
        perp_final, _ = evaluate(
            perp, funding, perp_filters, perp_p,
            a.starting_equity, final_start, n,
        )
        spot_final_stress, _ = evaluate(
            spot, {}, spot_filters, spot_stress_p,
            a.starting_equity, final_start, n,
        )
        print("  SPOT " + fmt(spot_final))
        print("  PERP " + fmt(perp_final))
        print("  SPOT STRESS " + fmt(spot_final_stress))
        final_checks = {
            "spot_final_positive": spot_final["net_pnl"] > 0,
            "spot_final_pf_at_least_1_10": spot_final["profit_factor"] >= 1.10,
            "spot_final_at_least_3_trades": spot_final["trades"] >= 3,
            "spot_final_positive_at_15bps_stress": spot_final_stress["net_pnl"] > 0,
            "spot_final_drawdown_below_20pct": spot_final["max_drawdown_pct"] < 20.0,
        }
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")

    passed = development_passed and all(final_checks.values())
    summary = {
        "strategy": "spot_vs_perp_30d_long_cash_tsmom",
        "common_bars": len(grid),
        "development_passed": development_passed,
        "sealed_final_opened": development_passed,
        "passed_for_spot_paper_candidate": passed,
        "spot_dev_net_pnl": spot_dev["net_pnl"],
        "spot_dev_pf": spot_dev["profit_factor"],
        "spot_dev_cagr_pct": spot_dev["cagr_pct"],
        "spot_dev_dd_pct": spot_dev["max_drawdown_pct"],
        "spot_dev_trades": spot_dev["trades"],
        "spot_stress_dev_net_pnl": spot_stress_dev["net_pnl"],
        "perp_dev_net_pnl": perp_dev["net_pnl"],
        "perp_dev_pf": perp_dev["profit_factor"],
        "spot_positive_folds": spot_positive_folds,
        "spot_final_net_pnl": spot_final["net_pnl"] if spot_final else None,
        "perp_final_net_pnl": perp_final["net_pnl"] if perp_final else None,
        "live_orders_sent": 0,
    }
    print("\n[spot-perp-summary] " + json.dumps(summary, sort_keys=True))
    print("\n" + ("PASS: spot venue is eligible for paper-only observation."
                    if passed else
                    "NO SPOT REPLACEMENT: keep the admitted perpetual paper candidate."))
    if a.json:
        payload = {
            "schema": 1,
            "summary": summary,
            "symbols": symbols,
            "months": a.months,
            "spot_cost_bps_per_side": 12.0,
            "perp_cost_bps_per_side": 7.0,
            "spot_stress_bps_per_side": 15.0,
            "spot_filters": {
                "minimums": spot_filters[0],
                "steps": spot_filters[1],
            },
            "perp_filters": {
                "minimums": perp_filters[0],
                "steps": perp_filters[1],
            },
            "spot_development": spot_dev,
            "perp_development": perp_dev,
            "spot_stress_development": spot_stress_dev,
            "spot_folds": spot_folds,
            "perp_folds": perp_folds,
            "development_checks": checks,
            "spot_final": spot_final,
            "perp_final": perp_final,
            "spot_final_stress": spot_final_stress,
            "final_checks": final_checks,
            "research_only": True,
            "live_orders_sent": 0
        }
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        print(f"wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
