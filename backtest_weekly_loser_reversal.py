#!/usr/bin/env python3
"""Frozen 8-week high-volatility loser reversal raw screen.

Literature-motivated but implementation is pre-registered for this wallet:
- six liquid USD-M alt perpetuals
- weekly review
- rank trailing 56d simple returns; choose the WORST recent loser
- selected coin must have negative 56d return and trailing-56d realized vol
  at or above the cross-sectional median (high-volatility loser)
- enter next daily open, hold 7 days to a daily open
- long only, one position, no DCA
- 14 bps round trip + exact funding; stress 20 bps
- development 75%; final 25% sealed until all gates pass

Raw screen only. No orders.
"""
from __future__ import annotations
import argparse, json, math
import numpy as np
import backtest_breakout, fetch_funding_universe


def p_one(t): return 0.5*math.erfc(t/math.sqrt(2.0))
def summ(xs):
    a=np.asarray(xs,float)
    if len(a)==0:return {"n":0,"mean_bps":0.0,"median_bps":0.0,"positive_pct":0.0,"t_stat":0.0,"p":1.0,"sum_return_pct":0.0}
    sd=float(a.std(ddof=1)) if len(a)>1 else 0.0; t=float(a.mean()/(sd/math.sqrt(len(a)))) if sd>0 else 0.0
    return {"n":int(len(a)),"mean_bps":float(a.mean()*1e4),"median_bps":float(np.median(a)*1e4),"positive_pct":float((a>0).mean()*100),"t_stat":t,"p":p_one(t),"sum_return_pct":float(a.sum()*100)}
def fmt(s):return f"n={s['n']:3d} mean={s['mean_bps']:+7.2f}bps med={s['median_bps']:+7.2f}bps pos={s['positive_pct']:5.1f}% t={s['t_stat']:+5.2f} sum={s['sum_return_pct']:+7.2f}% p={s['p']:.3f}"

def fetch(symbols,months):
    c={};f={}
    for s in symbols:
        print(f"\n=== {s} daily/funding ==="); c[s]=backtest_breakout.fetch(s,"1d",months); f[s]=fetch_funding_universe.fetch_symbol(s,months,pause=0.03); print(f"  funding prints: {len(f[s])}")
    return c,f

def align(c):
    common=None;maps={}
    for s,cs in c.items():
        m={int(round(x.ts)):x for x in cs};maps[s]=m;ks=set(m);common=ks if common is None else common&ks
    g=sorted(common);return g,{s:[maps[s][t] for t in g] for s in c}
def fnet(rows,a,b):
    # long pays positive funding / receives negative funding
    return -sum(float(r) for ts,r in rows if a<=ts<b)/1e4

def run(g,a,f,formation=56,hold=7,vol_filter=True,cost_bps_rt=14.0,lo=0,hi=None):
    if hi is None: hi=len(g)
    syms=list(a); close={s:np.array([x.close for x in a[s]],float) for s in syms}; opn={s:np.array([x.open for x in a[s]],float) for s in syms}
    lr={s:np.r_[np.nan,np.diff(np.log(close[s]))] for s in syms}
    out=[]; i=max(lo,formation+3)
    # fixed seven-day cadence from the start of the requested segment
    while i+1+hold<hi:
        rets={s:close[s][i]/close[s][i-formation]-1.0 for s in syms}
        vols={s:float(np.nanstd(lr[s][i-formation+1:i+1],ddof=1)) for s in syms}
        loser=min(syms,key=lambda s:rets[s])
        if rets[loser]>=0: i+=7; continue
        if vol_filter:
            med=float(np.nanmedian(list(vols.values())))
            if not np.isfinite(vols[loser]) or vols[loser]<med: i+=7; continue
        e=i+1; x=i+1+hold; entry=opn[loser][e]; exitp=opn[loser][x]
        if entry<=0 or exitp<=0: i+=7; continue
        gross=exitp/entry-1.0; net=gross+fnet(f[loser],g[e],g[x])-cost_bps_rt/1e4
        out.append(net); i+=7
    return out

def main(argv=None):
    ap=argparse.ArgumentParser();ap.add_argument("--months",type=float,default=48.0);ap.add_argument("--json",default=None);args=ap.parse_args(argv)
    syms=["SOLUSDT","SUIUSDT","BNBUSDT","XRPUSDT","TRXUSDT","DOGEUSDT"]
    print("=== FROZEN 8-WEEK HIGH-VOLATILITY LOSER REVERSAL RAW SCREEN ===")
    print("  weekly: worst trailing-56d loser; require negative return + vol >= cross-sectional median; long next open 7d")
    print("  exact funding; base=14bps RT; stress=20bps; dev75/final25 sealed")
    c,f=fetch(syms,args.months);g,a=align(c);n=len(g);warm=90
    if n<warm+500:raise SystemExit("not enough common daily history")
    split=warm+int((n-warm)*0.75)
    variants={"PRIMARY_56d":(56,True),"formation_49d":(49,True),"formation_63d":(63,True),"formation_70d":(70,True),"no_vol_filter":(56,False)}
    dev={};print("\nDEVELOPMENT 75%")
    for name,(fm,vf) in variants.items():dev[name]=summ(run(g,a,f,fm,7,vf,14.0,warm,split));print(f"  {name:15s} {fmt(dev[name])}")
    bounds=np.linspace(warm,split,4,dtype=int);folds=[];print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS")
    for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):
        st=summ(run(g,a,f,56,7,True,14.0,int(lo),int(hi)));folds.append(st);print(f"  fold {j}/3 {fmt(st)}")
    stress=summ(run(g,a,f,56,7,True,20.0,warm,split));print("\nCOST STRESS 20bps RT\n  "+fmt(stress))
    p=dev["PRIMARY_56d"];pf=sum(x['mean_bps']>0 for x in folds);pr=sum(x['mean_bps']>0 for x in dev.values())
    checks={"development_mean_positive":p['mean_bps']>0,"development_t_at_least_1_8":p['t_stat']>=1.8,"development_at_least_30_trades":p['n']>=30,
            "at_least_2_of_3_folds_positive":pf>=2,"at_least_4_of_5_rules_positive":pr>=4,"stress_mean_positive":stress['mean_bps']>0,"one_sided_p_at_most_0_05":p['p']<=0.05}
    print("\nDEVELOPMENT HURDLES");[print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in checks.items()]
    dp=all(checks.values());final=fs=None;passed=False
    if dp:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        final=summ(run(g,a,f,56,7,True,14.0,split,n));fs=summ(run(g,a,f,56,7,True,20.0,split,n));print("  FINAL  "+fmt(final));print("  STRESS "+fmt(fs))
        passed=final['mean_bps']>0 and final['n']>=8 and fs['mean_bps']>0
    else:print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")
    payload={"strategy":"weekly_high_vol_loser_reversal_56d_long_only","development":p,"variants":dev,"folds":folds,"stress":stress,"checks":checks,"development_passed":dp,"final":final,"final_stress":fs,"sealed_final_opened":dp,"passed":passed,"live_orders_sent":0,"raw_screen_only":True}
    print("\n[weekly-loser-reversal-summary] "+json.dumps(payload,sort_keys=True))
    if args.json:
        with open(args.json,"w",encoding="utf-8") as q:json.dump(payload,q,indent=2);print(f"wrote {args.json}")
    print("\n"+("PASS RAW SCREEN: eligible for $15 execution-aware validation." if passed else "REJECTED SCREEN: do not deploy this rule."))
    return 0 if passed else 2
if __name__=="__main__":raise SystemExit(main())
