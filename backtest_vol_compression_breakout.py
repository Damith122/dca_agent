#!/usr/bin/env python3
"""Sealed $15 validation of a volatility-compression breakout family.

Frozen primary rule:
- Daily completed-close review, next-open entry; long only; one position; DCA=0.
- Prior day's ATR(20)/close must be at or below the 25th percentile of its
  trailing 120 completed-day history (volatility compression known in advance).
- Current completed close must break above the prior 20-day high.
- Choose the largest breakout distance measured in ATR units.
- 1.5 ATR stop, 4 ATR target, 20-day max hold.
- Risk 2%, 50% annual-vol target, <=1x; min-order floor only if stop risk <=3%.
- Exact funding; 7 bps per side; 10 bps stress.
- Development 75%; final 25% sealed until all development gates pass.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import asdict, replace

import numpy as np

import backtest_trend_pullback_reversion as PR
from backtest_tsmom import fetch_inputs
from paper_tsmom import fetch_execution_filters
from tsmom import align_candles

DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"


def make_selector(*, channel=20, compression_window=120, compression_q=0.25):
    def selector(aligned, atrs, i, p):
        need = max(channel, compression_window + 1, p.atr_period + 2, 30)
        if i < need:
            return None
        choices = []
        for symbol, rows in aligned.items():
            closes = [float(b.close) for b in rows]
            prior_atr = atrs[symbol][i-1]
            if prior_atr is None or closes[i-1] <= 0:
                continue
            prior_atrp = float(prior_atr) / closes[i-1]
            hist = []
            lo = i - compression_window
            for j in range(lo, i):
                a = atrs[symbol][j]
                if a is not None and rows[j].close > 0:
                    hist.append(float(a) / float(rows[j].close))
            if len(hist) < compression_window // 2:
                continue
            threshold = float(np.quantile(np.asarray(hist), compression_q))
            if prior_atrp > threshold:
                continue
            prior_high = max(float(rows[j].high) for j in range(i-channel, i))
            close = closes[i]
            if close <= prior_high:
                continue
            atr = atrs[symbol][i]
            if atr is None or float(atr) <= 0:
                continue
            vol = PR.annual_vol(closes, i, 30)
            if not math.isfinite(vol) or vol <= 0:
                continue
            score = (close - prior_high) / float(atr)
            choices.append((-score, symbol, float(atr), vol))
        return min(choices, key=lambda x: (x[0], x[1])) if choices else None
    return selector


def run_variant(candles, funding, minimums, steps, p, *, equity, start, end,
                channel=20, compression_window=120, compression_q=0.25):
    old = PR.select_signal
    PR.select_signal = make_selector(channel=channel,
                                     compression_window=compression_window,
                                     compression_q=compression_q)
    try:
        sim = PR.run(candles, funding, minimums, steps, p,
                     equity=equity, start=start, end=end)
    finally:
        PR.select_signal = old
    return PR.enrich(sim, equity), sim


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

    print("=== FROZEN VOLATILITY-COMPRESSION BREAKOUT ===")
    print("  prior ATR% <= trailing-120d 25th pct; current close > prior 20d high")
    print("  daily review; next-open fill; 1.5ATR stop; 4ATR target; 20d max hold")
    print("  2% risk; <=1x; DCA=0; exact funding; 7bps/side; 10bps stress")
    print("  development 75%; final 25% sealed")

    candles, funding = fetch_inputs(symbols, a.months)
    now = time.time()
    candles = {s: [b for b in rows if b.ts + 86400 <= now] for s, rows in candles.items()}
    grid, _ = align_candles(candles)
    minimums, steps = fetch_execution_filters(symbols)
    p = PR.Params(trend_sma=30, pullback_days=5, pullback_pct=-0.05,
                  stop_atr=1.5, target_atr=4.0, max_hold_days=20,
                  rebalance_days=1, risk_pct=0.02,
                  annual_vol_target=0.50, max_leverage=1.0,
                  cost_bps_per_side=7.0)
    warm = 150
    n = len(grid)
    final_start = warm + int((n-warm)*0.75)

    dev, dev_sim = run_variant(candles, funding, minimums, steps, p,
                               equity=a.starting_equity, start=warm, end=final_start)
    print("\nDEVELOPMENT 75%")
    print("  PRIMARY " + fmt(dev))
    print(f"  exits={dev['exit_reasons']} blocks={dev['blocked_min_notional']} floors={dev['floored_min_notional']}")

    boundaries = np.linspace(warm, final_start, 4, dtype=int)
    folds=[]
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for k,(lo,hi) in enumerate(zip(boundaries[:-1], boundaries[1:]),1):
        r,_=run_variant(candles,funding,minimums,steps,p,equity=a.starting_equity,start=int(lo),end=int(hi))
        folds.append(r)
        print(f"  fold {k}/3 " + fmt(r))

    specs={
        "channel_15": dict(channel=15),
        "channel_30": dict(channel=30),
        "compression_q20": dict(compression_q=0.20),
        "compression_q35": dict(compression_q=0.35),
    }
    neighbors={}
    print("\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY")
    for name,kw in specs.items():
        r,_=run_variant(candles,funding,minimums,steps,p,equity=a.starting_equity,start=warm,end=final_start,**kw)
        neighbors[name]=r
        print(f"  {name:18s} " + fmt(r))

    stress_p=replace(p,cost_bps_per_side=10.0)
    stress,_=run_variant(candles,funding,minimums,steps,stress_p,equity=a.starting_equity,start=warm,end=final_start)
    sign_p=PR.sign_flip_p(dev_sim.trades)
    print("\nCOST STRESS 10 bps/side")
    print("  "+fmt(stress))
    print(f"  trade sign-flip p={sign_p:.3f}")

    pos_folds=sum(r['net_pnl']>0 for r in folds)
    pos_rules=sum(r['net_pnl']>0 for r in [dev,*neighbors.values()])
    checks={
        "development_fee_net_positive": dev['net_pnl']>0,
        "development_pf_at_least_1_20": dev['profit_factor']>=1.20,
        "development_at_least_12_trades": dev['trades']>=12,
        "at_least_2_of_3_folds_positive": pos_folds>=2,
        "at_least_3_of_5_rules_positive": pos_rules>=3,
        "positive_at_stressed_cost": stress['net_pnl']>0,
        "development_drawdown_below_20pct": dev['max_drawdown_pct']<20.0,
        "development_cagr_at_least_3pct": dev['cagr_pct']>=3.0,
        "trade_sign_flip_p_at_most_0_20": sign_p<=0.20,
    }
    dev_pass=all(checks.values())
    print("\nDEVELOPMENT ADMISSION HURDLES")
    for name,ok in checks.items(): print(f"  {'PASS' if ok else 'FAIL':4s} {name}")

    final=final_stress=None; final_checks={}
    if dev_pass:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        final,_=run_variant(candles,funding,minimums,steps,p,equity=a.starting_equity,start=final_start,end=n)
        final_stress,_=run_variant(candles,funding,minimums,steps,stress_p,equity=a.starting_equity,start=final_start,end=n)
        print("  FINAL  "+fmt(final)); print("  STRESS "+fmt(final_stress))
        final_checks={
            "final_positive": final['net_pnl']>0,
            "final_pf_at_least_1_10": final['profit_factor']>=1.10,
            "final_at_least_4_trades": final['trades']>=4,
            "final_stress_positive": final_stress['net_pnl']>0,
            "final_drawdown_below_20pct": final['max_drawdown_pct']<20.0,
        }
        for name,ok in final_checks.items(): print(f"  {'PASS' if ok else 'FAIL':4s} {name}")
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")

    passed=dev_pass and all(final_checks.values())
    summary={
        "strategy":"volatility_compression_breakout",
        "development_passed":dev_pass,"sealed_final_opened":dev_pass,"passed":passed,
        "dev_net_pnl":dev['net_pnl'],"dev_pf":dev['profit_factor'],"dev_cagr_pct":dev['cagr_pct'],
        "dev_dd_pct":dev['max_drawdown_pct'],"dev_trades":dev['trades'],
        "stress_net_pnl":stress['net_pnl'],"positive_folds":pos_folds,"positive_rules":pos_rules,
        "sign_flip_p":sign_p,"final_net_pnl":final['net_pnl'] if final else None,
        "final_pf":final['profit_factor'] if final else None,"live_orders_sent":0,
    }
    print("\n[vol-compression-summary] "+json.dumps(summary,sort_keys=True))
    print("\n"+("PASS: eligible for paper-only observation." if passed else "REJECTED: do not paper/live deploy this candidate."))
    if a.json:
        with open(a.json,'w',encoding='utf-8') as fh:
            json.dump({"summary":summary,"params":asdict(p),"development":dev,"folds":folds,
                       "neighbors":neighbors,"stress":stress,"checks":checks,"final":final,
                       "final_stress":final_stress,"final_checks":final_checks},fh,indent=2,sort_keys=True)
        print(f"wrote {a.json}")
    return 0 if passed else 2

if __name__=='__main__':
    raise SystemExit(main())
