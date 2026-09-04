#!/usr/bin/env python3
"""Frozen abnormal one-day move -> next-day continuation study.

Independent from the admitted 30d TSMOM family: this is a one-day event rule.
Research only; public data; no order-capable imports.

Primary rule (pre-registered):
- Six USD-M perpetuals: SOL,SUI,BNB,XRP,TRX,DOGE.
- At completed daily close, compute close-to-close 1d return.
- Abnormal if |1d return| >= 1.5 * trailing 20d return stdev.
- Pick the largest standardized abnormal move across symbols.
- Trade in the SAME direction at next daily open; hold one day.
- 2 ATR stop using ATR(20); next day's open is the scheduled exit.
- At most one position, <=1x leverage, 2% stop-risk target, DCA=0.
- 7 bps/side base cost, exact settled funding, 10 bps/side stress.
- $15 wallet, current exchange minimums/steps, min-order floor allowed only
  when stop risk stays <=3% and leverage <=1x.
- First 75% development; final 25% sealed until all development gates pass.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass, asdict, replace

import numpy as np

from backtest_tsmom import fetch_inputs
from paper_tsmom import fetch_execution_filters
from tsmom import align_candles
from breakout import atr_series

DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"

@dataclass(frozen=True)
class Params:
    vol_lookback: int = 20
    abnormal_sigma: float = 1.5
    atr_period: int = 20
    stop_atr: float = 2.0
    risk_pct: float = 0.02
    max_leverage: float = 1.0
    cost_bps_side: float = 7.0


def _sign_flip_p(values, runs=4000):
    a = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    if not len(a):
        return 1.0
    obs = float(a.mean())
    rng = np.random.default_rng(20260904)
    exceed = 0
    for _ in range(runs):
        s = rng.choice((-1.0, 1.0), size=len(a))
        if float(np.mean(a * s)) >= obs:
            exceed += 1
    return float((exceed + 1) / (runs + 1))


def _stats(trades, curve, ts, equity0):
    nets = np.asarray([t["net"] for t in trades], dtype=float)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    arr = np.asarray(curve or [equity0], dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = np.divide(peak-arr, peak, out=np.zeros_like(arr), where=peak>0)
    final = float(arr[-1])
    days = (ts[-1]-ts[0])/86400.0 if len(ts)>1 else 0.0
    cagr = ((final/equity0)**(365.0/days)-1.0)*100 if days>0 and final>0 else 0.0
    return {
        "trades": float(len(nets)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": float((nets>0).mean()*100) if len(nets) else 0.0,
        "net_pnl": final-equity0,
        "final_equity": final,
        "profit_factor": float(wins.sum()/-losses.sum()) if len(losses) and losses.sum()<0 else float("inf"),
        "cagr_pct": cagr,
        "max_drawdown_pct": float(dd.max()*100) if len(dd) else 0.0,
        "estimated_monthly_pnl": ((final-equity0)/days*30.44 if days>0 else 0.0),
    }


def run(candles, funding, minimums, steps, p, equity0, start, end):
    grid, aligned = align_candles(candles)
    atrs = {s: atr_series(rows, p.atr_period) for s, rows in aligned.items()}
    closes = {s: np.asarray([b.close for b in rows], dtype=float) for s, rows in aligned.items()}
    wallet = float(equity0)
    pos = None
    pending = None
    trades, curve, curve_ts = [], [], []
    blocked = floored = 0

    def close_pos(price, ts, reason):
        nonlocal wallet, pos
        if pos is None:
            return
        gross = pos["side"] * pos["qty"] * (price-pos["entry"])
        fee = abs(pos["qty"]*price) * p.cost_bps_side/1e4
        wallet += gross - fee
        trades.append({"symbol":pos["symbol"],"side":pos["side"],"net":gross+pos["funding"]-pos["entry_fee"]-fee,"reason":reason})
        pos = None

    for i in range(len(grid)):
        if i >= end:
            break
        ts = grid[i]
        active = i >= start

        # Scheduled one-day exit occurs at today's open before any new entry.
        if pos is not None and i > pos["entry_i"]:
            close_pos(aligned[pos["symbol"]][i].open, ts, "time")

        if active and pending is not None and pos is None:
            sym, side, atr = pending
            bar = aligned[sym][i]
            stop_frac = p.stop_atr * atr / bar.open if bar.open>0 else 0.0
            lev = min(p.max_leverage, p.risk_pct/stop_frac) if stop_frac>0 else 0.0
            desired = wallet * max(0.0, lev)
            qty = desired/bar.open if bar.open>0 else 0.0
            step = float(steps.get(sym,0.0))
            if step>0 and qty>0:
                qty = math.floor(qty/step + 1e-12)*step
            notional = qty*bar.open
            minimum = float(minimums.get(sym,0.0))
            if desired>0 and notional+1e-12 < minimum:
                min_qty = minimum/bar.open if bar.open>0 else 0.0
                if step>0 and min_qty>0:
                    min_qty = math.ceil(min_qty/step - 1e-12)*step
                min_notional = min_qty*bar.open
                floor_risk = min_notional*stop_frac/wallet if wallet>0 else float("inf")
                floor_lev = min_notional/wallet if wallet>0 else float("inf")
                if floor_risk <= 0.03 and floor_lev <= p.max_leverage:
                    qty, notional = min_qty, min_notional
                    floored += 1
                else:
                    qty = 0.0
                    blocked += 1
            if qty>0:
                entry_fee = notional*p.cost_bps_side/1e4
                wallet -= entry_fee
                stop = bar.open - side*p.stop_atr*atr
                pos = {"symbol":sym,"side":side,"entry_i":i,"entry":bar.open,"qty":qty,"stop":stop,"entry_fee":entry_fee,"funding":0.0}

        pending = None

        if pos is not None:
            bar = aligned[pos["symbol"]][i]
            # Exact daily settled funding aggregate supplied by fetch_inputs.
            day = int(ts//86400*86400)
            rate_bps = float(funding.get(pos["symbol"],{}).get(day,0.0))
            fpnl = -pos["side"] * abs(pos["qty"]*bar.close) * rate_bps/1e4
            wallet += fpnl
            pos["funding"] += fpnl
            hit = (pos["side"]>0 and bar.low<=pos["stop"]) or (pos["side"]<0 and bar.high>=pos["stop"])
            if hit:
                close_pos(pos["stop"], ts, "stop")

        mark = wallet
        if pos is not None:
            bar = aligned[pos["symbol"]][i]
            mark += pos["side"]*pos["qty"]*(bar.close-pos["entry"])
        if active:
            curve.append(mark); curve_ts.append(ts)

        # Decide at completed close for next open.
        if active and i+1 < end and i >= p.vol_lookback+1:
            candidates = []
            for sym, arr in closes.items():
                if arr[i-1] <= 0 or arr[i] <= 0:
                    continue
                r1 = math.log(arr[i]/arr[i-1])
                hist = np.diff(np.log(arr[i-p.vol_lookback:i+1]))
                sigma = float(hist[:-1].std(ddof=1)) if len(hist)>2 else 0.0
                atr = atrs[sym][i] if i < len(atrs[sym]) else None
                if sigma<=1e-12 or atr is None or atr<=0:
                    continue
                z = r1/sigma
                if abs(z) >= p.abnormal_sigma:
                    candidates.append((abs(z), sym, 1 if z>0 else -1, float(atr)))
            if candidates:
                _, sym, side, atr = max(candidates, key=lambda x:(x[0],x[1]))
                pending = (sym, side, atr)

    if pos is not None:
        i = min(end,len(grid))-1
        close_pos(aligned[pos["symbol"]][i].close, grid[i], "end")
        if curve:
            curve[-1] = wallet
    return _stats(trades, curve, curve_ts, equity0), trades, blocked, floored


def fmt(r):
    return (f"n={int(r['trades']):3d} win={r['win_rate']:5.1f}% net=${r['net_pnl']:+7.4f} "
            f"final=${r['final_equity']:7.4f} CAGR={r['cagr_pct']:+6.2f}% "
            f"PF={r['profit_factor']:5.2f} DD={r['max_drawdown_pct']:5.2f}% est/mo=${r['estimated_monthly_pnl']:+.4f}")


def main(argv=None):
    for k in ("BINANCE_API_KEY","BINANCE_API_SECRET","API_KEY","API_SECRET"):
        os.environ.pop(k, None)
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbols",default=DEFAULT_SYMBOLS)
    ap.add_argument("--months",type=float,default=48.0)
    ap.add_argument("--starting-equity",type=float,default=15.0)
    ap.add_argument("--json",default=None)
    a=ap.parse_args(argv)
    symbols=[x.strip().upper() for x in a.symbols.split(',') if x.strip()]
    print("=== FROZEN ABNORMAL DAILY CONTINUATION ===")
    print("  signal: |1d return| >= 1.5 x trailing-20d sigma; trade same direction next open")
    print("  hold 1 day; 2ATR stop; <=1x; 2% risk; DCA=0; 7bps/side; exact funding; 10bps stress")
    print("  development 75%; final 25% sealed")
    candles,funding=fetch_inputs(symbols,a.months)
    minimums,steps=fetch_execution_filters(symbols)
    grid,_=align_candles(candles)
    p=Params()
    warm=max(p.vol_lookback,p.atr_period)+10
    n=len(grid)
    final_start=warm+int((n-warm)*0.75)
    dev,trades,blocked,floored=run(candles,funding,minimums,steps,p,a.starting_equity,warm,final_start)
    print("\nDEVELOPMENT 75%\n  PRIMARY "+fmt(dev)+f" blocks={blocked} floors={floored}")
    bounds=np.linspace(warm,final_start,4,dtype=int)
    folds=[]
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):
        r,_,_,_=run(candles,funding,minimums,steps,p,a.starting_equity,int(lo),int(hi)); folds.append(r)
        print(f"  fold {j}/3 "+fmt(r))
    variants={
        "sigma_1_25":replace(p,abnormal_sigma=1.25),
        "sigma_1_75":replace(p,abnormal_sigma=1.75),
        "vol_15":replace(p,vol_lookback=15),
        "vol_30":replace(p,vol_lookback=30),
    }
    neighbors={}
    print("\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY")
    for name,q in variants.items():
        r,_,_,_=run(candles,funding,minimums,steps,q,a.starting_equity,warm,final_start); neighbors[name]=r
        print(f"  {name:12s} "+fmt(r))
    stress_p=replace(p,cost_bps_side=10.0)
    stress,_,_,_=run(candles,funding,minimums,steps,stress_p,a.starting_equity,warm,final_start)
    sign_p=_sign_flip_p([t['net'] for t in trades])
    print("\nCOST STRESS 10 bps/side\n  "+fmt(stress)+f"\n  trade sign-flip p={sign_p:.3f}")
    pos_folds=sum(r['net_pnl']>0 for r in folds)
    pos_rules=sum(r['net_pnl']>0 for r in [dev,*neighbors.values()])
    checks={
        "development_fee_net_positive":dev['net_pnl']>0,
        "development_pf_at_least_1_20":dev['profit_factor']>=1.20,
        "development_at_least_20_trades":dev['trades']>=20,
        "at_least_2_of_3_folds_positive":pos_folds>=2,
        "at_least_3_of_5_rules_positive":pos_rules>=3,
        "positive_at_stressed_cost":stress['net_pnl']>0,
        "development_drawdown_below_20pct":dev['max_drawdown_pct']<20,
        "development_cagr_at_least_3pct":dev['cagr_pct']>=3,
        "trade_sign_flip_p_at_most_0_20":sign_p<=0.20,
    }
    print("\nDEVELOPMENT ADMISSION HURDLES")
    for k,v in checks.items(): print(f"  {'PASS' if v else 'FAIL':4s} {k}")
    dev_pass=all(checks.values())
    final=final_stress=None; final_checks={}
    if dev_pass:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        final,_,_,_=run(candles,funding,minimums,steps,p,a.starting_equity,final_start,n)
        final_stress,_,_,_=run(candles,funding,minimums,steps,stress_p,a.starting_equity,final_start,n)
        print("  FINAL  "+fmt(final)); print("  STRESS "+fmt(final_stress))
        final_checks={
            "final_positive":final['net_pnl']>0,
            "final_pf_at_least_1_10":final['profit_factor']>=1.10,
            "final_at_least_5_trades":final['trades']>=5,
            "final_stress_positive":final_stress['net_pnl']>0,
            "final_drawdown_below_20pct":final['max_drawdown_pct']<20,
        }
        for k,v in final_checks.items(): print(f"  {'PASS' if v else 'FAIL':4s} {k}")
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")
    passed=dev_pass and all(final_checks.values())
    summary={"strategy":"abnormal_daily_continuation","development_passed":dev_pass,"sealed_final_opened":dev_pass,"passed":passed,"dev_net_pnl":dev['net_pnl'],"dev_pf":dev['profit_factor'],"dev_cagr_pct":dev['cagr_pct'],"dev_dd_pct":dev['max_drawdown_pct'],"dev_trades":dev['trades'],"stress_net_pnl":stress['net_pnl'],"sign_flip_p":sign_p,"positive_folds":pos_folds,"positive_rules":pos_rules,"final_net_pnl":final['net_pnl'] if final else None,"final_pf":final['profit_factor'] if final else None,"live_orders_sent":0}
    print("\n[abnormal-daily-summary] "+json.dumps(summary,sort_keys=True))
    print("\n"+("PASS: eligible for paper-only observation." if passed else "REJECTED: do not paper/live deploy this candidate."))
    if a.json:
        with open(a.json,'w',encoding='utf-8') as fh:
            json.dump({"summary":summary,"params":asdict(p),"development":dev,"neighbors":neighbors,"folds":folds,"stress":stress,"final":final,"final_stress":final_stress,"development_checks":checks,"final_checks":final_checks,"research_only":True},fh,indent=2,sort_keys=True)
        print("wrote "+a.json)
    return 0 if passed else 2

if __name__=='__main__':
    raise SystemExit(main())
