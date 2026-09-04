#!/usr/bin/env python3
"""Sealed validation for a trend-conditioned pullback mean-reversion strategy.

Research only: public Binance data, no order-capable imports.

Frozen primary rule (decided before reading the result):
- Universe: SOL, SUI, BNB, XRP, TRX, DOGE USD-M perpetuals.
- Weekly review after a completed daily close; earliest fill is next daily open.
- Long only, one position maximum, no DCA, max leverage 1x.
- Long-term regime: close above completed-close SMA(100).
- Pullback: trailing 5-day close return <= -8%.
- Select the deepest eligible pullback.
- Exit: 2.5 ATR stop, 3.0 ATR target, or 10 completed days maximum hold.
- Risk 2% of equity at planned stop; 50% annual-vol target; minimum-order
  flooring allowed only when stop risk remains <=3% of equity.
- Exact historical funding; 7 bps per entry/exit side, stressed at 10 bps.
- Development 75%; final 25% remains sealed unless every development gate passes.

Neighbours are predeclared robustness checks, not parameter-search winners.
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

from backtest_tsmom import fetch_inputs
from breakout import atr_series
from paper_tsmom import fetch_execution_filters
from tsmom import Simulation, Trade, align_candles, stats

DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"


@dataclass(frozen=True)
class Params:
    trend_sma: int = 100
    pullback_days: int = 5
    pullback_pct: float = -0.08
    atr_period: int = 20
    stop_atr: float = 2.5
    target_atr: float = 3.0
    max_hold_days: int = 10
    rebalance_days: int = 7
    rebalance_offset: int = 0
    risk_pct: float = 0.02
    annual_vol_target: float = 0.50
    max_leverage: float = 1.0
    cost_bps_per_side: float = 7.0


@dataclass
class Position:
    symbol: str
    entry_i: int
    entry_ts: float
    entry: float
    qty: float
    stop: float
    target: float
    entry_fee: float
    entry_equity: float
    funding_pnl: float = 0.0


def annual_vol(closes, i, lookback=30):
    if i < lookback:
        return float("nan")
    arr = np.asarray(closes[i-lookback:i+1], dtype=float)
    if np.any(arr <= 0):
        return float("nan")
    r = np.diff(np.log(arr))
    if len(r) < 2:
        return float("nan")
    return float(r.std(ddof=1) * math.sqrt(365.0))


def select_signal(aligned, atrs, i, p: Params):
    choices = []
    need = max(p.trend_sma, p.pullback_days, p.atr_period, 30)
    if i < need:
        return None
    for symbol, rows in aligned.items():
        closes = [float(b.close) for b in rows]
        sma = float(np.mean(closes[i-p.trend_sma+1:i+1]))
        if closes[i] <= sma:
            continue
        old = closes[i-p.pullback_days]
        if old <= 0:
            continue
        pull = closes[i] / old - 1.0
        if pull > p.pullback_pct:
            continue
        atr = atrs[symbol][i]
        if atr is None or atr <= 0 or not math.isfinite(float(atr)):
            continue
        vol = annual_vol(closes, i, 30)
        if not math.isfinite(vol) or vol <= 0:
            continue
        choices.append((pull, symbol, float(atr), vol))
    if not choices:
        return None
    # Most negative 5-day return first; symbol is deterministic tie-breaker.
    return min(choices, key=lambda x: (x[0], x[1]))


def close_position(pos, price, ts, reason, wallet, p):
    gross = pos.qty * (price - pos.entry)
    exit_fee = abs(pos.qty * price) * p.cost_bps_per_side / 1e4
    wallet += gross - exit_fee
    trade = Trade(
        symbol=pos.symbol, side=1, entry_ts=pos.entry_ts, exit_ts=ts,
        entry=pos.entry, exit=price, qty=pos.qty, reason=reason,
        gross_pnl=gross, funding_pnl=pos.funding_pnl,
        fees=pos.entry_fee + exit_fee,
        net_pnl=gross + pos.funding_pnl - pos.entry_fee - exit_fee,
        entry_equity=pos.entry_equity,
    )
    return wallet, trade, exit_fee


def run(candles, funding, minimums, steps, p: Params, *, equity, start, end):
    times, aligned = align_candles(candles)
    n = len(times)
    end = min(end, n)
    atrs = {s: atr_series(rows, p.atr_period) for s, rows in aligned.items()}
    wallet = float(equity)
    pos = None
    pending = None
    trades = []
    curve, curve_ts = [], []
    total_fees = total_funding = 0.0
    blocked = floored = 0

    for i in range(end):
        ts = times[i]
        active = i >= start

        # Execute signal decided on the previous completed daily close.
        if active and pos is None and pending is not None:
            pull, symbol, atr, vol = pending
            bar = aligned[symbol][i]
            stop_fraction = p.stop_atr * atr / bar.open if bar.open > 0 else 0.0
            by_stop = p.risk_pct / stop_fraction if stop_fraction > 0 else 0.0
            by_vol = p.annual_vol_target / vol if vol > 0 else 0.0
            lev = max(0.0, min(p.max_leverage, by_stop, by_vol))
            desired = wallet * lev
            step = float(steps.get(symbol, 0.0))
            qty = desired / bar.open if bar.open > 0 else 0.0
            if step > 0 and qty > 0:
                qty = math.floor(qty / step + 1e-12) * step
            notional = qty * bar.open
            minimum = float(minimums.get(symbol, 0.0))
            if desired > 0 and notional + 1e-12 < minimum:
                min_qty = minimum / bar.open if bar.open > 0 else 0.0
                if step > 0 and min_qty > 0:
                    min_qty = math.ceil(min_qty / step - 1e-12) * step
                min_size = min_qty * bar.open
                floor_risk = min_size * stop_fraction / wallet if wallet > 0 else float("inf")
                floor_lev = min_size / wallet if wallet > 0 else float("inf")
                if floor_risk <= 0.03 and floor_lev <= p.max_leverage:
                    qty, notional = min_qty, min_size
                    floored += 1
                else:
                    qty = 0.0
                    blocked += 1
            if qty > 0:
                fee = notional * p.cost_bps_per_side / 1e4
                entry_equity = wallet
                wallet -= fee
                total_fees += fee
                pos = Position(symbol, i, ts, bar.open, qty,
                               bar.open - p.stop_atr * atr,
                               bar.open + p.target_atr * atr,
                               fee, entry_equity)
        pending = None

        if pos is not None:
            bar = aligned[pos.symbol][i]
            # A gap through the stop is filled at the worse opening price.
            if i > pos.entry_i and bar.open <= pos.stop:
                wallet, t, fee = close_position(pos, bar.open, ts, "gap_stop", wallet, p)
                total_fees += fee
                trades.append(t)
                pos = None
            # Maximum hold exits at the day's open before adding another day's risk.
            elif i - pos.entry_i >= p.max_hold_days:
                wallet, t, fee = close_position(pos, bar.open, ts, "time", wallet, p)
                total_fees += fee
                trades.append(t)
                pos = None
            else:
                day = int(ts // 86400 * 86400)
                rate_bps = float(funding.get(pos.symbol, {}).get(day, 0.0))
                fund = -abs(pos.qty * bar.close) * rate_bps / 1e4
                wallet += fund
                pos.funding_pnl += fund
                total_funding += fund
                hit_stop = bar.low <= pos.stop
                hit_target = bar.high >= pos.target
                # Daily OHLC ordering is unknown; stop wins an ambiguous bar.
                if hit_stop:
                    wallet, t, fee = close_position(pos, pos.stop, ts, "stop", wallet, p)
                    total_fees += fee
                    trades.append(t)
                    pos = None
                elif hit_target:
                    wallet, t, fee = close_position(pos, pos.target, ts, "target", wallet, p)
                    total_fees += fee
                    trades.append(t)
                    pos = None

        if active:
            mark = wallet
            if pos is not None:
                mark += pos.qty * (aligned[pos.symbol][i].close - pos.entry)
            curve.append(mark)
            curve_ts.append(ts)

        # Decide only after this daily bar has completed.
        if active and pos is None and i + 1 < end:
            day_number = int(ts // 86400)
            if day_number % p.rebalance_days == p.rebalance_offset % p.rebalance_days:
                pending = select_signal(aligned, atrs, i, p)

    if pos is not None and end > 0:
        i = end - 1
        price = aligned[pos.symbol][i].close
        wallet, t, fee = close_position(pos, price, times[i], "end", wallet, p)
        total_fees += fee
        trades.append(t)
        if curve:
            curve[-1] = wallet

    return Simulation(trades, curve or [equity], curve_ts, wallet,
                      total_fees, total_funding, None, 0.0, blocked, floored)


def enrich(sim, equity):
    out = stats(sim, equity)
    nets = np.asarray([t.net_pnl for t in sim.trades], dtype=float)
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    out.update({
        "net_pnl": float(sim.final_equity - equity),
        "final_equity": float(sim.final_equity),
        "wins": int(np.sum(nets > 0)),
        "losses": int(np.sum(nets <= 0)),
        "estimated_monthly_pnl": ((sim.final_equity - equity) / days * 30.44 if days > 0 else 0.0),
        "exit_reasons": dict(sorted(Counter(t.reason for t in sim.trades).items())),
        "blocked_min_notional": int(sim.blocked_min_notional),
        "floored_min_notional": int(sim.floored_min_notional),
    })
    return out


def sign_flip_p(trades, runs=4000):
    arr = np.asarray([t.net_pnl for t in trades], dtype=float)
    if not len(arr):
        return 1.0
    obs = float(arr.mean())
    rng = np.random.default_rng(20260904)
    exceed = 0
    for _ in range(runs):
        signs = rng.choice((-1.0, 1.0), size=len(arr))
        if float(np.mean(arr * signs)) >= obs:
            exceed += 1
    return float((exceed + 1) / (runs + 1))


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

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    print("=== FROZEN TREND-CONDITIONED PULLBACK REVERSION ===")
    print("  regime close>SMA100; 5d return<=-8%; deepest pullback long")
    print("  weekly review; next-open fill; 2.5ATR stop; 3ATR target; 10d max hold")
    print("  risk=2%; <=1x; DCA=0; 7bps/side; exact funding; 10bps stress")
    print("  development 75%; final 25% sealed")

    candles, funding = fetch_inputs(symbols, a.months)
    now = time.time()
    candles = {s: [b for b in rows if b.ts + 86400 <= now] for s, rows in candles.items()}
    grid, _ = align_candles(candles)
    minimums, steps = fetch_execution_filters(symbols)
    p = Params()
    warm = max(p.trend_sma, p.pullback_days, p.atr_period, 30) + 10
    if len(grid) < warm + 730:
        raise SystemExit("not enough aligned daily history")
    n = len(grid)
    final_start = warm + int((n - warm) * 0.75)

    dev_sim = run(candles, funding, minimums, steps, p, equity=a.starting_equity, start=warm, end=final_start)
    dev = enrich(dev_sim, a.starting_equity)
    print("\nDEVELOPMENT 75%")
    print("  PRIMARY " + fmt(dev))
    print(f"  exits={dev['exit_reasons']} blocks={dev['blocked_min_notional']} floors={dev['floored_min_notional']}")

    boundaries = np.linspace(warm, final_start, 4, dtype=int)
    folds = []
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for k, (lo, hi) in enumerate(zip(boundaries[:-1], boundaries[1:]), 1):
        sim = run(candles, funding, minimums, steps, p, equity=a.starting_equity, start=int(lo), end=int(hi))
        r = enrich(sim, a.starting_equity)
        folds.append(r)
        print(f"  fold {k}/3 " + fmt(r))

    neighbors = {
        "pullback_-6pct": replace(p, pullback_pct=-0.06),
        "pullback_-10pct": replace(p, pullback_pct=-0.10),
        "trend_sma_75": replace(p, trend_sma=75),
        "trend_sma_150": replace(p, trend_sma=150),
    }
    neigh = {}
    print("\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY")
    for name, q in neighbors.items():
        sim = run(candles, funding, minimums, steps, q, equity=a.starting_equity, start=warm, end=final_start)
        r = enrich(sim, a.starting_equity)
        neigh[name] = r
        print(f"  {name:18s} " + fmt(r))

    stress_p = replace(p, cost_bps_per_side=10.0)
    stress_sim = run(candles, funding, minimums, steps, stress_p, equity=a.starting_equity, start=warm, end=final_start)
    stress = enrich(stress_sim, a.starting_equity)
    sign_p = sign_flip_p(dev_sim.trades)
    print("\nCOST STRESS 10 bps/side")
    print("  " + fmt(stress))
    print(f"  trade sign-flip p={sign_p:.3f}")

    pos_folds = sum(r["net_pnl"] > 0 for r in folds)
    pos_rules = sum(r["net_pnl"] > 0 for r in [dev, *neigh.values()])
    checks = {
        "development_fee_net_positive": dev["net_pnl"] > 0,
        "development_pf_at_least_1_20": dev["profit_factor"] >= 1.20,
        "development_at_least_12_trades": dev["trades"] >= 12,
        "at_least_2_of_3_folds_positive": pos_folds >= 2,
        "at_least_3_of_5_rules_positive": pos_rules >= 3,
        "positive_at_stressed_cost": stress["net_pnl"] > 0,
        "development_drawdown_below_20pct": dev["max_drawdown_pct"] < 20.0,
        "development_cagr_at_least_3pct": dev["cagr_pct"] >= 3.0,
        "trade_sign_flip_p_at_most_0_20": sign_p <= 0.20,
    }
    dev_pass = all(checks.values())
    print("\nDEVELOPMENT ADMISSION HURDLES")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s} {name}")

    final = final_stress = None
    final_checks = {}
    if dev_pass:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        fs = run(candles, funding, minimums, steps, p, equity=a.starting_equity, start=final_start, end=n)
        final = enrich(fs, a.starting_equity)
        fss = run(candles, funding, minimums, steps, stress_p, equity=a.starting_equity, start=final_start, end=n)
        final_stress = enrich(fss, a.starting_equity)
        print("  FINAL  " + fmt(final))
        print("  STRESS " + fmt(final_stress))
        final_checks = {
            "final_positive": final["net_pnl"] > 0,
            "final_pf_at_least_1_10": final["profit_factor"] >= 1.10,
            "final_at_least_4_trades": final["trades"] >= 4,
            "final_stress_positive": final_stress["net_pnl"] > 0,
            "final_drawdown_below_20pct": final["max_drawdown_pct"] < 20.0,
        }
        for name, ok in final_checks.items():
            print(f"  {'PASS' if ok else 'FAIL':4s} {name}")
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")

    passed = dev_pass and all(final_checks.values())
    summary = {
        "strategy": "trend_conditioned_pullback_reversion",
        "development_passed": dev_pass,
        "sealed_final_opened": dev_pass,
        "passed": passed,
        "dev_net_pnl": dev["net_pnl"],
        "dev_pf": dev["profit_factor"],
        "dev_cagr_pct": dev["cagr_pct"],
        "dev_dd_pct": dev["max_drawdown_pct"],
        "dev_trades": dev["trades"],
        "stress_net_pnl": stress["net_pnl"],
        "positive_folds": pos_folds,
        "positive_rules": pos_rules,
        "sign_flip_p": sign_p,
        "final_net_pnl": final["net_pnl"] if final else None,
        "final_pf": final["profit_factor"] if final else None,
        "live_orders_sent": 0,
    }
    print("\n[pullback-reversion-summary] " + json.dumps(summary, sort_keys=True))
    print("\n" + ("PASS: eligible for paper-only observation." if passed else "REJECTED: do not paper/live deploy this candidate."))
    if a.json:
        payload = {"summary": summary, "params": asdict(p), "development": dev,
                   "folds": folds, "neighbors": neigh, "stress": stress,
                   "checks": checks, "final": final, "final_stress": final_stress,
                   "final_checks": final_checks}
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        print(f"wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
