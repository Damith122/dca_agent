#!/usr/bin/env python3
"""Frozen BTC shock -> alt lead-lag raw screen.

Primary hypothesis is fixed before reading results:
- 1h BTC shock >= 1.75 x prior 72h sigma
- choose, at decision time, the alt with the highest positive rolling 30d beta to BTC
- trade same direction from next 1h open to the open 4 hours later
- one position at a time, no overlap
- 14 bps round trip base cost; 20 bps stress
- development 75%; sealed final 25% opened only if all dev gates pass

This is a RAW screen. It sends no orders and does not build live execution logic.
"""
from __future__ import annotations

import argparse, json, math
import numpy as np
import backtest_breakout


def fetch(symbols, months):
    out = {}
    for s in symbols:
        print(f"\n=== {s} 1h public history ===")
        out[s] = backtest_breakout.fetch(s, "1h", months)
    return out


def align(data):
    common = None
    maps = {}
    for s, cs in data.items():
        m = {int(round(c.ts)): c for c in cs}
        maps[s] = m
        ks = set(m)
        common = ks if common is None else common & ks
    grid = sorted(common)
    return grid, {s: [maps[s][t] for t in grid] for s in data}


def normal_one_sided_p(t):
    return 0.5 * math.erfc(t / math.sqrt(2.0))


def summarize(xs):
    a = np.asarray(xs, dtype=float)
    if len(a) == 0:
        return {"n":0,"mean_bps":0.0,"median_bps":0.0,"positive_pct":0.0,"t_stat":0.0,"p":1.0,"sum_return_pct":0.0}
    sd = float(a.std(ddof=1)) if len(a)>1 else 0.0
    t = float(a.mean()/(sd/math.sqrt(len(a)))) if sd>0 else 0.0
    return {"n":int(len(a)),"mean_bps":float(a.mean()*1e4),"median_bps":float(np.median(a)*1e4),
            "positive_pct":float((a>0).mean()*100),"t_stat":t,"p":normal_one_sided_p(t),
            "sum_return_pct":float(a.sum()*100)}


def run(grid, a, shock_mult=1.75, sigma_h=72, beta_h=720, hold_h=4, cost_bps_rt=14.0,
        lo=0, hi=None):
    if hi is None: hi = len(grid)
    syms = [s for s in a if s != "BTCUSDT"]
    close = {s: np.array([c.close for c in a[s]], float) for s in a}
    opn = {s: np.array([c.open for c in a[s]], float) for s in a}
    rets = {s: np.r_[np.nan, np.diff(np.log(close[s]))] for s in a}
    btc = rets["BTCUSDT"]
    out=[]; picks=[]
    i=max(lo, beta_h+2, sigma_h+2)
    while i + hold_h + 1 < hi:
        prior = btc[i-sigma_h:i]
        sig = float(np.nanstd(prior, ddof=1))
        if not np.isfinite(sig) or sig<=0 or not np.isfinite(btc[i]) or abs(btc[i]) < shock_mult*sig:
            i += 1; continue
        x = btc[i-beta_h:i]
        vx = float(np.nanvar(x, ddof=1))
        if not np.isfinite(vx) or vx<=0:
            i += 1; continue
        best=None; best_beta=-1e9
        for s in syms:
            y = rets[s][i-beta_h:i]
            mask=np.isfinite(x)&np.isfinite(y)
            if mask.sum()<beta_h*0.9: continue
            cov=float(np.cov(x[mask], y[mask], ddof=1)[0,1])
            beta=cov/vx
            if beta>best_beta:
                best_beta=beta; best=s
        if best is None or best_beta<=0:
            i += 1; continue
        entry=opn[best][i+1]; exitp=opn[best][i+1+hold_h]
        if entry<=0 or exitp<=0:
            i += 1; continue
        direction=1.0 if btc[i]>0 else -1.0
        gross=direction*(exitp/entry-1.0)
        net=gross-cost_bps_rt/1e4
        out.append(net); picks.append((grid[i],best,best_beta,direction,gross,net))
        i += hold_h+1
    return out,picks


def fmt(st):
    return f"n={st['n']:4d} mean={st['mean_bps']:+7.2f}bps med={st['median_bps']:+7.2f}bps pos={st['positive_pct']:5.1f}% t={st['t_stat']:+5.2f} sum={st['sum_return_pct']:+7.2f}% p={st['p']:.3f}"


def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--months",type=float,default=18.0)
    ap.add_argument("--json",default=None)
    args=ap.parse_args(argv)
    symbols=["BTCUSDT","SOLUSDT","SUIUSDT","BNBUSDT","XRPUSDT","TRXUSDT","DOGEUSDT"]
    print("=== FROZEN BTC LEAD-LAG RAW SCREEN ===")
    print("  BTC 1h shock >=1.75x prior-72h sigma; highest prior-30d-beta alt; same-direction next-open to +4h")
    print("  one position/no overlap; base=14bps RT; stress=20bps; dev75/final25 sealed")
    data=fetch(symbols,args.months)
    grid,a=align(data)
    n=len(grid); warm=800
    if n < warm+1000: raise SystemExit("not enough common hourly history")
    split=warm+int((n-warm)*0.75)
    variants={
        "PRIMARY":(1.75,72,720),
        "shock_1_50":(1.50,72,720),
        "shock_2_00":(2.00,72,720),
        "beta_14d":(1.75,72,336),
        "beta_45d":(1.75,72,1080),
    }
    dev={}
    print("\nDEVELOPMENT 75%")
    for name,(sm,sh,bh) in variants.items():
        xs,_=run(grid,a,sm,sh,bh,4,14.0,warm,split)
        dev[name]=summarize(xs)
        print(f"  {name:12s} {fmt(dev[name])}")
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    bounds=np.linspace(warm,split,4,dtype=int)
    folds=[]
    for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):
        xs,_=run(grid,a,1.75,72,720,4,14.0,int(lo),int(hi))
        st=summarize(xs); folds.append(st); print(f"  fold {j}/3 {fmt(st)}")
    stress=summarize(run(grid,a,1.75,72,720,4,20.0,warm,split)[0])
    print("\nCOST STRESS 20 bps RT")
    print("  "+fmt(stress))
    pos_folds=sum(x["mean_bps"]>0 for x in folds)
    pos_rules=sum(x["mean_bps"]>0 for x in dev.values())
    p=dev["PRIMARY"]
    checks={
        "development_cost_adjusted_mean_positive":p["mean_bps"]>0,
        "development_t_at_least_2":p["t_stat"]>=2.0,
        "development_at_least_100_events":p["n"]>=100,
        "at_least_2_of_3_folds_positive":pos_folds>=2,
        "at_least_4_of_5_rules_positive":pos_rules>=4,
        "stress_mean_positive":stress["mean_bps"]>0,
        "one_sided_p_at_most_0_05":p["p"]<=0.05,
    }
    dev_pass=all(checks.values())
    print("\nDEVELOPMENT SCREEN HURDLES")
    for k,v in checks.items(): print(f"  {'PASS' if v else 'FAIL':4s} {k}")
    final=final_stress=None; passed=False
    if dev_pass:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        final=summarize(run(grid,a,1.75,72,720,4,14.0,split,n)[0])
        final_stress=summarize(run(grid,a,1.75,72,720,4,20.0,split,n)[0])
        print("  FINAL  "+fmt(final)); print("  STRESS "+fmt(final_stress))
        passed=final["mean_bps"]>0 and final["p"]<=0.20 and final_stress["mean_bps"]>0 and final["n"]>=30
    else:
        print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")
    payload={"strategy":"btc_lead_lag_high_beta_alt_raw_screen","development":dev["PRIMARY"],"variants":dev,
             "folds":folds,"stress":stress,"checks":checks,"development_passed":dev_pass,
             "final":final,"final_stress":final_stress,"sealed_final_opened":dev_pass,"passed":passed,"live_orders_sent":0,"raw_screen_only":True}
    print("\n[btc-lead-lag-summary] "+json.dumps(payload,sort_keys=True))
    if args.json:
        with open(args.json,"w",encoding="utf-8") as f: json.dump(payload,f,indent=2)
        print(f"wrote {args.json}")
    print("\n"+("PASS RAW SCREEN: eligible for full execution-aware study." if passed else "REJECTED SCREEN: do not build execution logic for this rule."))
    return 0 if passed else 2

if __name__=="__main__": raise SystemExit(main())
