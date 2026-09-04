#!/usr/bin/env python3
"""Frozen smooth-price-path continuation proxy screen.

Inspired by recent price-path-continuity evidence, but this is explicitly a
simple pre-registered proxy, not an exact paper replication.

Primary:
- six liquid USD-M alt perpetuals, daily bars
- weekly review
- prior 14 daily log returns: cumulative return and path efficiency
  efficiency = abs(sum r) / sum(abs(r))
- among positive 14d-return coins, choose max(cumulative * efficiency)
- require chosen efficiency >= cross-sectional median
- long next daily open, hold 7 days
- exact funding + 14 bps RT; stress 20 bps
- development 75%; final 25% sealed
"""
from __future__ import annotations
import argparse,json,math
import numpy as np
import backtest_breakout,fetch_funding_universe

def pone(t):return 0.5*math.erfc(t/math.sqrt(2.0))
def summ(x):
 a=np.asarray(x,float)
 if len(a)==0:return {"n":0,"mean_bps":0.0,"median_bps":0.0,"positive_pct":0.0,"t_stat":0.0,"p":1.0,"sum_return_pct":0.0}
 sd=float(a.std(ddof=1)) if len(a)>1 else 0.0;t=float(a.mean()/(sd/math.sqrt(len(a)))) if sd>0 else 0.0
 return {"n":int(len(a)),"mean_bps":float(a.mean()*1e4),"median_bps":float(np.median(a)*1e4),"positive_pct":float((a>0).mean()*100),"t_stat":t,"p":pone(t),"sum_return_pct":float(a.sum()*100)}
def fmt(s):return f"n={s['n']:3d} mean={s['mean_bps']:+7.2f}bps med={s['median_bps']:+7.2f}bps pos={s['positive_pct']:5.1f}% t={s['t_stat']:+5.2f} sum={s['sum_return_pct']:+7.2f}% p={s['p']:.3f}"
def fetch(syms,months):
 c={};f={}
 for s in syms:
  print(f"\n=== {s} daily/funding ===");c[s]=backtest_breakout.fetch(s,"1d",months);f[s]=fetch_funding_universe.fetch_symbol(s,months,pause=0.03);print(f"  funding prints: {len(f[s])}")
 return c,f
def align(c):
 common=None;maps={}
 for s,cs in c.items():
  m={int(round(x.ts)):x for x in cs};maps[s]=m;ks=set(m);common=ks if common is None else common&ks
 g=sorted(common);return g,{s:[maps[s][t] for t in g] for s in c}
def fnet(rows,a,b):return -sum(float(r) for ts,r in rows if a<=ts<b)/1e4
def run(g,a,f,look=14,hold=7,median_filter=True,cost=14.0,lo=0,hi=None):
 if hi is None:hi=len(g)
 syms=list(a);close={s:np.array([z.close for z in a[s]],float) for s in syms};opn={s:np.array([z.open for z in a[s]],float) for s in syms};lr={s:np.r_[np.nan,np.diff(np.log(close[s]))] for s in syms}
 out=[];i=max(lo,look+3)
 while i+1+hold<hi:
  vals=[]
  for s in syms:
   r=lr[s][i-look+1:i+1]
   if len(r)!=look or not np.all(np.isfinite(r)):continue
   cum=float(r.sum());den=float(np.abs(r).sum());eff=abs(cum)/den if den>0 else 0.0
   if cum>0:vals.append((s,cum,eff,cum*eff))
  if not vals:i+=7;continue
  chosen=max(vals,key=lambda z:z[3]);s,cum,eff,score=chosen
  if median_filter:
   all_eff=[]
   for q in syms:
    r=lr[q][i-look+1:i+1];den=float(np.nansum(np.abs(r)));all_eff.append(abs(float(np.nansum(r)))/den if den>0 else 0.0)
   if eff<float(np.nanmedian(all_eff)):i+=7;continue
  e=i+1;x=i+1+hold;entry=opn[s][e];exitp=opn[s][x]
  if entry<=0 or exitp<=0:i+=7;continue
  out.append(exitp/entry-1.0+fnet(f[s],g[e],g[x])-cost/1e4);i+=7
 return out
def main(argv=None):
 ap=argparse.ArgumentParser();ap.add_argument("--months",type=float,default=48.0);ap.add_argument("--json",default=None);args=ap.parse_args(argv)
 syms=["SOLUSDT","SUIUSDT","BNBUSDT","XRPUSDT","TRXUSDT","DOGEUSDT"]
 print("=== FROZEN SMOOTH PRICE-PATH CONTINUATION PROXY ===");print("  weekly: max positive cumulative-return*path-efficiency over prior14d; efficiency >= cross-sectional median; long 7d");print("  exact funding; base14bps RT; stress20bps; dev75/final25 sealed")
 c,f=fetch(syms,args.months);g,a=align(c);n=len(g);warm=50;split=warm+int((n-warm)*0.75)
 variants={"PRIMARY_14d":(14,True),"lookback_10d":(10,True),"lookback_21d":(21,True),"lookback_28d":(28,True),"no_eff_filter":(14,False)};dev={}
 print("\nDEVELOPMENT 75%")
 for name,(lk,mf) in variants.items():dev[name]=summ(run(g,a,f,lk,7,mf,14.0,warm,split));print(f"  {name:14s} {fmt(dev[name])}")
 print("\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS");bounds=np.linspace(warm,split,4,dtype=int);folds=[]
 for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):
  st=summ(run(g,a,f,14,7,True,14.0,int(lo),int(hi)));folds.append(st);print(f"  fold {j}/3 {fmt(st)}")
 stress=summ(run(g,a,f,14,7,True,20.0,warm,split));print("\nCOST STRESS 20bps RT\n  "+fmt(stress))
 p=dev['PRIMARY_14d'];pf=sum(x['mean_bps']>0 for x in folds);pr=sum(x['mean_bps']>0 for x in dev.values())
 checks={"development_mean_positive":p['mean_bps']>0,"development_t_at_least_1_8":p['t_stat']>=1.8,"development_at_least_30_trades":p['n']>=30,"at_least_2_of_3_folds_positive":pf>=2,"at_least_4_of_5_rules_positive":pr>=4,"stress_mean_positive":stress['mean_bps']>0,"one_sided_p_at_most_0_05":p['p']<=0.05}
 print("\nDEVELOPMENT HURDLES");[print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in checks.items()]
 dp=all(checks.values());final=fs=None;passed=False
 if dp:
  print("\nDEVELOPMENT PASS — opening sealed FINAL 25%");final=summ(run(g,a,f,14,7,True,14.0,split,n));fs=summ(run(g,a,f,14,7,True,20.0,split,n));print("  FINAL  "+fmt(final));print("  STRESS "+fmt(fs));passed=final['mean_bps']>0 and final['n']>=8 and fs['mean_bps']>0
 else:print("\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened")
 payload={"strategy":"smooth_path_continuation_proxy_14d","development":p,"variants":dev,"folds":folds,"stress":stress,"checks":checks,"development_passed":dp,"final":final,"final_stress":fs,"sealed_final_opened":dp,"passed":passed,"live_orders_sent":0,"raw_screen_only":True}
 print("\n[path-continuity-summary] "+json.dumps(payload,sort_keys=True))
 if args.json:
  with open(args.json,'w',encoding='utf-8') as q:json.dump(payload,q,indent=2);print(f"wrote {args.json}")
 print("\n"+("PASS RAW SCREEN: eligible for execution-aware validation." if passed else "REJECTED SCREEN: do not deploy this rule."));return 0 if passed else 2
if __name__=='__main__':raise SystemExit(main())
