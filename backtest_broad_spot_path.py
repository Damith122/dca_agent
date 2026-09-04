#!/usr/bin/env python3
"""Frozen broad-Spot smooth-path continuation proxy.

Fixed 15 liquid-major Binance USDT spot names. Weekly review; prior 14 daily
log returns; path efficiency = abs(sum r)/sum(abs r). Among positive-return
coins choose max(sum r * efficiency), requiring efficiency >= cross-sectional
median. Buy next daily open, hold 7 days. 24 bps RT base / 30 bps stress.
Development 75%; final 25% sealed. Raw screen only; survivorship caveat applies.
"""
from __future__ import annotations
import argparse,json,math,time,urllib.request
import numpy as np
SPOT='https://api.binance.com/api/v3/klines';DAY=86400000

def fetch(sym,months):
 end=int(time.time()*1000);start=end-int(months*30.44*DAY);out={};cur=start
 while cur<end:
  with urllib.request.urlopen(f'{SPOT}?symbol={sym}&interval=1d&startTime={cur}&limit=1000',timeout=30) as r: rows=json.loads(r.read().decode())
  if not rows:break
  for z in rows:out[int(z[0])]=(int(z[0])//1000,float(z[1]),float(z[4]))
  nxt=int(rows[-1][0])+DAY
  if nxt<=cur:break
  cur=nxt;time.sleep(.05)
 print(f'  {sym}: {len(out)} bars');return out

def align(raw):
 common=None
 for m in raw.values():common=set(m) if common is None else common&set(m)
 ks=sorted(common);first=next(iter(raw));g=[raw[first][k][0] for k in ks];a={s:{'open':np.array([m[k][1] for k in ks]),'close':np.array([m[k][2] for k in ks])} for s,m in raw.items()};return g,a

def pone(t):return .5*math.erfc(t/math.sqrt(2))
def summ(x):
 a=np.asarray(x,float)
 if len(a)==0:return {'n':0,'mean_bps':0.,'median_bps':0.,'positive_pct':0.,'t_stat':0.,'p':1.,'sum_return_pct':0.}
 sd=float(a.std(ddof=1)) if len(a)>1 else 0.;t=float(a.mean()/(sd/math.sqrt(len(a)))) if sd>0 else 0.
 return {'n':int(len(a)),'mean_bps':float(a.mean()*1e4),'median_bps':float(np.median(a)*1e4),'positive_pct':float((a>0).mean()*100),'t_stat':t,'p':pone(t),'sum_return_pct':float(a.sum()*100)}
def fmt(s):return f"n={s['n']:3d} mean={s['mean_bps']:+7.2f}bps med={s['median_bps']:+7.2f}bps pos={s['positive_pct']:5.1f}% t={s['t_stat']:+5.2f} sum={s['sum_return_pct']:+7.2f}% p={s['p']:.3f}"
def run(g,a,look=14,hold=7,medfilter=True,cost=24.,lo=0,hi=None):
 if hi is None:hi=len(g)
 syms=list(a);lr={s:np.r_[np.nan,np.diff(np.log(a[s]['close']))] for s in syms};out=[];i=max(lo,look+3)
 while i+1+hold<hi:
  vals=[];effall=[]
  for s in syms:
   r=lr[s][i-look+1:i+1]
   if len(r)!=look or not np.all(np.isfinite(r)):continue
   cum=float(r.sum());den=float(np.abs(r).sum());eff=abs(cum)/den if den>0 else 0.;effall.append(eff)
   if cum>0:vals.append((s,cum,eff,cum*eff))
  if not vals:i+=7;continue
  s,cum,eff,score=max(vals,key=lambda z:z[3])
  if medfilter and eff<float(np.median(effall)):i+=7;continue
  e=i+1;x=i+1+hold;ent=a[s]['open'][e];ex=a[s]['open'][x]
  if ent>0 and ex>0:out.append(ex/ent-1-cost/1e4)
  i+=7
 return out

def main(argv=None):
 ap=argparse.ArgumentParser();ap.add_argument('--months',type=float,default=60.);ap.add_argument('--json',default=None);args=ap.parse_args(argv)
 syms='BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,SOLUSDT,DOGEUSDT,TRXUSDT,LINKUSDT,LTCUSDT,BCHUSDT,DOTUSDT,AVAXUSDT,ETCUSDT,XLMUSDT'.split(',')
 print('=== FROZEN BROAD SPOT SMOOTH-PATH CONTINUATION PROXY ===');print('  fixed 15 majors; weekly max positive 14d return*path efficiency; efficiency >= median; long7d');print('  24bps RT base / 30bps stress; dev75/final25 sealed')
 raw={}
 for s in syms:
  try:raw[s]=fetch(s,args.months)
  except Exception as e:print(f'  {s}: fetch failed {e}')
 if len(raw)<12:raise SystemExit('fewer than 12 names')
 g,a=align(raw);n=len(g);print(f'common: {len(a)} symbols, {n} daily bars');warm=60;split=warm+int((n-warm)*.75)
 variants={'PRIMARY_14d':(14,True),'lookback_10d':(10,True),'lookback_21d':(21,True),'lookback_28d':(28,True),'no_eff_filter':(14,False)};dev={};print('\nDEVELOPMENT 75%')
 for name,(lk,mf) in variants.items():dev[name]=summ(run(g,a,lk,7,mf,24.,warm,split));print(f'  {name:14s} {fmt(dev[name])}')
 bounds=np.linspace(warm,split,4,dtype=int);folds=[];print('\nTHREE DEVELOPMENT FOLDS')
 for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):st=summ(run(g,a,14,7,True,24.,int(lo),int(hi)));folds.append(st);print(f'  fold {j}/3 {fmt(st)}')
 stress=summ(run(g,a,14,7,True,30.,warm,split));print('\nSTRESS 30bps RT\n  '+fmt(stress));p=dev['PRIMARY_14d'];pf=sum(x['mean_bps']>0 for x in folds);pr=sum(x['mean_bps']>0 for x in dev.values())
 checks={'development_mean_positive':p['mean_bps']>0,'development_t_at_least_1_8':p['t_stat']>=1.8,'development_at_least_50_trades':p['n']>=50,'at_least_2_of_3_folds_positive':pf>=2,'at_least_4_of_5_rules_positive':pr>=4,'stress_mean_positive':stress['mean_bps']>0,'p_at_most_0_05':p['p']<=.05};print('\nDEVELOPMENT HURDLES');[print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in checks.items()]
 dp=all(checks.values());final=fs=None;passed=False
 if dp:
  print('\nDEVELOPMENT PASS — opening FINAL25');final=summ(run(g,a,14,7,True,24.,split,n));fs=summ(run(g,a,14,7,True,30.,split,n));print('  FINAL  '+fmt(final));print('  STRESS '+fmt(fs));passed=final['mean_bps']>0 and final['n']>=12 and fs['mean_bps']>0
 else:print('\nDEVELOPMENT FAIL — FINAL25 sealed')
 payload={'strategy':'broad_spot_smooth_path_continuation_proxy','symbols':list(a),'aligned_bars':n,'development':p,'variants':dev,'folds':folds,'stress':stress,'checks':checks,'development_passed':dp,'final':final,'final_stress':fs,'sealed_final_opened':dp,'passed':passed,'raw_screen_only':True,'live_orders_sent':0,'survivorship_caveat':True};print('\n[broad-spot-path-summary] '+json.dumps(payload,sort_keys=True))
 if args.json:
  with open(args.json,'w') as q:json.dump(payload,q,indent=2);print(f'wrote {args.json}')
 print('\n'+('PASS RAW SCREEN: next $15 executable replay.' if passed else 'REJECTED SCREEN.'));return 0 if passed else 2
if __name__=='__main__':raise SystemExit(main())
