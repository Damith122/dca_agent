#!/usr/bin/env python3
"""Frozen broad-Spot 3-week cross-sectional momentum raw screen.

Fixed 15 liquid-major Binance USDT spot names. Weekly review, rank trailing
21-day return, buy the strongest winner only if its 21d return is positive,
enter next daily open and hold 7 days. 24 bps RT base cost; 30 bps stress.
Development 75%; final 25% sealed. Raw screen only; fixed-current-universe
survivorship caveat applies.
"""
from __future__ import annotations
import argparse,json,math,time,urllib.request
import numpy as np
SPOT='https://api.binance.com/api/v3/klines';DAY=86400000

def fetch(s,m):
 end=int(time.time()*1000);start=end-int(m*30.44*DAY);o={};cur=start
 while cur<end:
  with urllib.request.urlopen(f'{SPOT}?symbol={s}&interval=1d&startTime={cur}&limit=1000',timeout=30) as r:rows=json.loads(r.read().decode())
  if not rows:break
  for z in rows:o[int(z[0])]=(int(z[0])//1000,float(z[1]),float(z[4]))
  nxt=int(rows[-1][0])+DAY
  if nxt<=cur:break
  cur=nxt;time.sleep(.05)
 print(f'  {s}: {len(o)} bars');return o

def align(raw):
 c=None
 for m in raw.values():c=set(m) if c is None else c&set(m)
 ks=sorted(c);f=next(iter(raw));g=[raw[f][k][0] for k in ks];a={s:{'open':np.array([m[k][1] for k in ks]),'close':np.array([m[k][2] for k in ks])} for s,m in raw.items()};return g,a

def po(t):return .5*math.erfc(t/math.sqrt(2))
def sm(x):
 a=np.asarray(x,float)
 if len(a)==0:return {'n':0,'mean_bps':0.,'median_bps':0.,'positive_pct':0.,'t_stat':0.,'p':1.,'sum_return_pct':0.}
 sd=float(a.std(ddof=1)) if len(a)>1 else 0.;t=float(a.mean()/(sd/math.sqrt(len(a)))) if sd>0 else 0.;return {'n':int(len(a)),'mean_bps':float(a.mean()*1e4),'median_bps':float(np.median(a)*1e4),'positive_pct':float((a>0).mean()*100),'t_stat':t,'p':po(t),'sum_return_pct':float(a.sum()*100)}
def fmt(s):return f"n={s['n']:3d} mean={s['mean_bps']:+7.2f}bps med={s['median_bps']:+7.2f}bps pos={s['positive_pct']:5.1f}% t={s['t_stat']:+5.2f} sum={s['sum_return_pct']:+7.2f}% p={s['p']:.3f}"
def run(g,a,look=21,hold=7,cost=24.,lo=0,hi=None):
 if hi is None:hi=len(g)
 sy=list(a);out=[];i=max(lo,look+3)
 while i+1+hold<hi:
  rr={s:a[s]['close'][i]/a[s]['close'][i-look]-1 for s in sy};w=max(sy,key=lambda s:rr[s])
  if rr[w]<=0:i+=7;continue
  e=i+1;x=i+1+hold;ent=a[w]['open'][e];ex=a[w]['open'][x]
  if ent>0 and ex>0:out.append(ex/ent-1-cost/1e4)
  i+=7
 return out

def main(argv=None):
 ap=argparse.ArgumentParser();ap.add_argument('--months',type=float,default=60.);ap.add_argument('--json',default=None);args=ap.parse_args(argv)
 sy='BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,SOLUSDT,DOGEUSDT,TRXUSDT,LINKUSDT,LTCUSDT,BCHUSDT,DOTUSDT,AVAXUSDT,ETCUSDT,XLMUSDT'.split(',')
 print('=== FROZEN BROAD SPOT CROSS-SECTIONAL 3-WEEK MOMENTUM ===');print('  weekly strongest positive trailing21d winner; long next open7d; 24bps RT / stress30bps; dev75/final25 sealed')
 raw={s:fetch(s,args.months) for s in sy};g,a=align(raw);n=len(g);print(f'common {len(a)} names, {n} days');warm=60;split=warm+int((n-warm)*.75)
 variants={'PRIMARY_21d':21,'lookback_14d':14,'lookback_28d':28,'lookback_35d':35,'lookback_42d':42};dev={};print('\nDEVELOPMENT 75%')
 for k,l in variants.items():dev[k]=sm(run(g,a,l,7,24.,warm,split));print(f'  {k:14s} {fmt(dev[k])}')
 b=np.linspace(warm,split,4,dtype=int);folds=[];print('\nTHREE DEVELOPMENT FOLDS')
 for j,(lo,hi) in enumerate(zip(b[:-1],b[1:]),1):st=sm(run(g,a,21,7,24.,int(lo),int(hi)));folds.append(st);print(f'  fold {j}/3 {fmt(st)}')
 stress=sm(run(g,a,21,7,30.,warm,split));print('\nSTRESS 30bps RT\n  '+fmt(stress));p=dev['PRIMARY_21d'];pf=sum(x['mean_bps']>0 for x in folds);pr=sum(x['mean_bps']>0 for x in dev.values())
 checks={'development_mean_positive':p['mean_bps']>0,'development_t_at_least_1_8':p['t_stat']>=1.8,'development_at_least_100_trades':p['n']>=100,'at_least_2_of_3_folds_positive':pf>=2,'at_least_4_of_5_rules_positive':pr>=4,'stress_mean_positive':stress['mean_bps']>0,'p_at_most_0_05':p['p']<=.05};print('\nDEVELOPMENT HURDLES');[print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in checks.items()]
 dp=all(checks.values());final=fs=None;passed=False
 if dp:
  print('\nDEVELOPMENT PASS — opening FINAL25');final=sm(run(g,a,21,7,24.,split,n));fs=sm(run(g,a,21,7,30.,split,n));print('  FINAL  '+fmt(final));print('  STRESS '+fmt(fs));passed=final['mean_bps']>0 and final['n']>=20 and fs['mean_bps']>0
 else:print('\nDEVELOPMENT FAIL — FINAL25 sealed')
 q={'strategy':'broad_spot_cross_sectional_momentum_21d','development':p,'variants':dev,'folds':folds,'stress':stress,'checks':checks,'development_passed':dp,'final':final,'final_stress':fs,'sealed_final_opened':dp,'passed':passed,'raw_screen_only':True,'live_orders_sent':0,'survivorship_caveat':True};print('\n[xs-momentum-summary] '+json.dumps(q,sort_keys=True))
 if args.json:
  with open(args.json,'w') as f:json.dump(q,f,indent=2);print(f'wrote {args.json}')
 print('\n'+('PASS RAW SCREEN: next $15 execution replay.' if passed else 'REJECTED SCREEN.'));return 0 if passed else 2
if __name__=='__main__':raise SystemExit(main())
