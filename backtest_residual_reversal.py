#!/usr/bin/env python3
"""Frozen BTC-residual cross-sectional 1d reversal raw screen.

Primary rule fixed before results:
- daily bars, six liquid alt perpetuals + BTC benchmark
- rolling 60d beta of each alt daily log return vs BTC, using only prior data
- current residual = alt 1d return - beta * BTC 1d return
- if largest absolute residual >= 1.5 x that alt's prior-60d residual sigma,
  trade OPPOSITE the residual from next daily open to following daily open
- one position at a time; 14 bps round trip + exact settled funding
- stress at 20 bps round trip; development 75%, final 25% sealed

Raw screen only. No live orders.
"""
from __future__ import annotations
import argparse, json, math
import numpy as np
import backtest_breakout, fetch_funding_universe


def normal_p(t): return 0.5*math.erfc(t/math.sqrt(2.0))

def summarise(xs):
    a=np.asarray(xs,float)
    if len(a)==0: return {"n":0,"mean_bps":0.0,"median_bps":0.0,"positive_pct":0.0,"t_stat":0.0,"p":1.0,"sum_return_pct":0.0}
    sd=float(a.std(ddof=1)) if len(a)>1 else 0.0
    t=float(a.mean()/(sd/math.sqrt(len(a)))) if sd>0 else 0.0
    return {"n":int(len(a)),"mean_bps":float(a.mean()*1e4),"median_bps":float(np.median(a)*1e4),
            "positive_pct":float((a>0).mean()*100),"t_stat":t,"p":normal_p(t),"sum_return_pct":float(a.sum()*100)}

def fmt(s):
    return f"n={s['n']:3d} mean={s['mean_bps']:+7.2f}bps med={s['median_bps']:+7.2f}bps pos={s['positive_pct']:5.1f}% t={s['t_stat']:+5.2f} sum={s['sum_return_pct']:+7.2f}% p={s['p']:.3f}"

def fetch(symbols,months):
    candles={}; fund={}
    for s in symbols:
        print(f"\n=== {s} public daily/funding history ===")
        candles[s]=backtest_breakout.fetch(s,"1d",months)
        fund[s]=fetch_funding_universe.fetch_symbol(s,months,pause=0.03)
        print(f"  funding prints: {len(fund[s])}")
    return candles,fund

def align(candles):
    common=None; maps={}
    for s,cs in candles.items():
        m={int(round(c.ts)):c for c in cs}; maps[s]=m
        ks=set(m); common=ks if common is None else common&ks
    grid=sorted(common)
    return grid,{s:[maps[s][t] for t in grid] for s in candles}

def funding_net(rows,entry_ts,exit_ts,direction):
    # Binance funding: positive rate is paid by longs, received by shorts.
    bps=sum(float(rate) for ts,rate in rows if entry_ts <= ts < exit_ts)
    return -direction*bps/1e4

def run(grid,a,fund,threshold=1.5,beta_days=60,resid_days=60,cost_bps_rt=14.0,lo=0,hi=None):
    if hi is None: hi=len(grid)
    syms=[s for s in a if s!="BTCUSDT"]
    close={s:np.array([c.close for c in a[s]],float) for s in a}
    opn={s:np.array([c.open for c in a[s]],float) for s in a}
    ret={s:np.r_[np.nan,np.diff(np.log(close[s]))] for s in a}
    btc=ret["BTCUSDT"]
    warm=max(beta_days,resid_days)+5
    out=[]; i=max(lo,warm)
    while i+2<hi:
        x=btc[i-beta_days:i]
        vx=float(np.nanvar(x,ddof=1))
        if not np.isfinite(vx) or vx<=0: i+=1; continue
        best=None; best_abs=-1.0; best_res=0.0; best_sigma=0.0
        for s in syms:
            y=ret[s][i-beta_days:i]
            m=np.isfinite(x)&np.isfinite(y)
            if m.sum()<beta_days*0.9: continue
            beta=float(np.cov(x[m],y[m],ddof=1)[0,1]/vx)
            # build prior residuals using frozen current beta; all source returns are prior to i
            k0=max(1,i-resid_days)
            rr=ret[s][k0:i]-beta*btc[k0:i]
            sigma=float(np.nanstd(rr,ddof=1))
            cur=float(ret[s][i]-beta*btc[i]) if np.isfinite(ret[s][i]) and np.isfinite(btc[i]) else np.nan
            if np.isfinite(cur) and np.isfinite(sigma) and sigma>0 and abs(cur)>best_abs:
                best=s; best_abs=abs(cur); best_res=cur; best_sigma=sigma
        if best is None or best_abs < threshold*best_sigma: i+=1; continue
        direction=-1.0 if best_res>0 else 1.0
        entry_i=i+1; exit_i=i+2
        entry=opn[best][entry_i]; exitp=opn[best][exit_i]
        if entry<=0 or exitp<=0: i+=1; continue
        gross=direction*(exitp/entry-1.0)
        fnet=funding_net(fund[best],grid[entry_i],grid[exit_i],direction)
        net=gross+fnet-cost_bps_rt/1e4
        out.append(net)
        i=exit_i
    return out

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument("--months",type=float,default=48.0); ap.add_argument("--json",default=None); args=ap.parse_args(argv)
    symbols=["BTCUSDT","SOLUSDT","SUIUSDT","BNBUSDT","XRPUSDT","TRXUSDT","DOGEUSDT"]
    print("=== FROZEN BTC-RESIDUAL CROSS-SECTIONAL REVERSAL RAW SCREEN ===")
    print("  prior-60d beta vs BTC; extreme 1d residual >=1.5x prior residual sigma; opposite next day")
    print("  exact funding; base=14bps RT; stress=20bps; development75/final25 sealed")
    candles,fund=fetch(symbols,args.months); grid,a=align(candles)
    n=len(grid); warm=100
    if n<warm+500: raise SystemExit("not enough common daily history")
    split=warm+int((n-warm)*0.75)
    variants={"PRIMARY":(1.5,60,60),"threshold_1_25":(1.25,60,60),"threshold_1_75":(1.75,60,60),"beta_30d":(1.5,30,60),"beta_90d":(1.5,90,60)}
    dev={}; print("\nDEVELOPMENT 75%")
    for name,(th,beta,rd) in variants.items():
        dev[name]=summarise(run(grid,a,fund,th,beta,rd,14.0,warm,split)); print(f"  {name:14s} {fmt(dev[name])}")
    print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    bounds=np.linspace(warm,split,4,dtype=int); folds=[]
    for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):
        st=summarise(run(grid,a,fund,1.5,60,60,14.0,int(lo),int(hi))); folds.append(st); print(f"  fold {j}/3 {fmt(st)}")
    stress=summarise(run(grid,a,fund,1.5,60,60,20.0,warm,split)); print("\nCOST STRESS 20 bps RT\n  "+fmt(stress))
    p=dev["PRIMARY"]; pos_folds=sum(x["mean_bps"]>0 for x in folds); pos_rules=sum(x["mean_bps"]>0 for x in dev.values())
    checks={"development_mean_positive":p["mean_bps"]>0,"development_t_at_least_2":p["t_stat"]>=2.0,"development_at_least_30_events":p["n"]>=30,
            "at_least_2_of_3_folds_positive":pos_folds>=2,"at_least_4_of_5_rules_positive":pos_rules>=4,"stress_mean_positive":stress["mean_bps"]>0,"one_sided_p_at_most_0_05":p["p"]<=0.05}
    print("\nDEVELOPMENT SCREEN HURDLES")
    for k,v in checks.items(): print(f"  {'PASS' if v else 'FAIL':4s} {k}")
    dev_pass=all(checks.values()); final=final_stress=None; passed=False
    if dev_pass:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        final=summarise(run(grid,a,fund,1.5,60,60,14.0,split,n)); final_stress=summarise(run(grid,a,fund,1.5,60,60,20.0,split,n))
        print("  FINAL  "+fmt(final)); print("  STRESS "+fmt(final_stress))
        passed=final["mean_bps"]>0 and final["n"]>=10 and final_stress["mean_bps"]>0
    else: print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")
    payload={"strategy":"btc_residual_cross_sectional_reversal_raw_screen","development":p,"variants":dev,"folds":folds,"stress":stress,"checks":checks,
             "development_passed":dev_pass,"final":final,"final_stress":final_stress,"sealed_final_opened":dev_pass,"passed":passed,"live_orders_sent":0,"raw_screen_only":True}
    print("\n[residual-reversal-summary] "+json.dumps(payload,sort_keys=True))
    if args.json:
        with open(args.json,"w",encoding="utf-8") as f: json.dump(payload,f,indent=2); print(f"wrote {args.json}")
    print("\n"+("PASS RAW SCREEN: eligible for execution-aware study." if passed else "REJECTED SCREEN: do not build execution logic for this rule."))
    return 0 if passed else 2

if __name__=="__main__": raise SystemExit(main())
