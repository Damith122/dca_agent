#!/usr/bin/env python3
"""Frozen broad liquid Spot 8-week loser-reversal screen.

A closer venue/horizon test of recent cross-sectional reversal evidence:
- fixed 15-name liquid-major USDT spot universe (chosen before results)
- weekly review
- rank trailing 56d return; buy the worst recent LOSER
- require selected loser trailing-56d realised volatility >= cross-sectional median
- enter next daily open, hold 7 days
- long-only, one position
- spot base cost 12 bps/side (24 bps RT); stress 15 bps/side (30 bps RT)
- development 75%; final 25% sealed

Raw historical screen only. Current-liquid fixed universe implies survivorship bias;
a pass would require a separate $15 executable-minimum replay before paper admission.
"""
from __future__ import annotations
import argparse,json,math,time,urllib.request
import numpy as np

SPOT='https://api.binance.com/api/v3/klines'
DAY=86400000

def fetch(sym,months):
 end=int(time.time()*1000); start=end-int(months*30.44*DAY); out={};cur=start
 while cur<end:
  url=f'{SPOT}?symbol={sym}&interval=1d&startTime={cur}&limit=1000'
  with urllib.request.urlopen(url,timeout=30) as r: rows=json.loads(r.read().decode())
  if not rows:break
  for z in rows: out[int(z[0])]=(int(z[0])//1000,float(z[1]),float(z[4]))
  nxt=int(rows[-1][0])+DAY
  if nxt<=cur:break
  cur=nxt;time.sleep(0.05)
 print(f'  {sym}: {len(out)} daily bars')
 return out

def align(raw):
 common=None
 for m in raw.values(): common=set(m) if common is None else common&set(m)
 ks=sorted(common); g=[raw[next(iter(raw))][k][0] for k in ks]
 a={s:{'open':np.array([m[k][1] for k in ks]),'close':np.array([m[k][2] for k in ks])} for s,m in raw.items()}
 return g,a

def pone(t):return 0.5*math.erfc(t/math.sqrt(2))
def summ(x):
 a=np.asarray(x,float)
 if len(a)==0:return {'n':0,'mean_bps':0.0,'median_bps':0.0,'positive_pct':0.0,'t_stat':0.0,'p':1.0,'sum_return_pct':0.0}
 sd=float(a.std(ddof=1)) if len(a)>1 else 0.;t=float(a.mean()/(sd/math.sqrt(len(a)))) if sd>0 else 0.
 return {'n':int(len(a)),'mean_bps':float(a.mean()*1e4),'median_bps':float(np.median(a)*1e4),'positive_pct':float((a>0).mean()*100),'t_stat':t,'p':pone(t),'sum_return_pct':float(a.sum()*100)}
def fmt(s):return f"n={s['n']:3d} mean={s['mean_bps']:+7.2f}bps med={s['median_bps']:+7.2f}bps pos={s['positive_pct']:5.1f}% t={s['t_stat']:+5.2f} sum={s['sum_return_pct']:+7.2f}% p={s['p']:.3f}"
def run(g,a,formation=56,hold=7,vol_filter=True,cost_rt=24.,lo=0,hi=None):
 if hi is None:hi=len(g)
 syms=list(a);lr={s:np.r_[np.nan,np.diff(np.log(a[s]['close']))] for s in syms};out=[];i=max(lo,formation+3)
 while i+1+hold<hi:
  rets={s:a[s]['close'][i]/a[s]['close'][i-formation]-1 for s in syms}
  vols={s:float(np.nanstd(lr[s][i-formation+1:i+1],ddof=1)) for s in syms}
  loser=min(syms,key=lambda s:rets[s])
  if rets[loser]>=0:i+=7;continue
  if vol_filter and vols[loser]<float(np.nanmedian(list(vols.values()))):i+=7;continue
  e=i+1;x=i+1+hold;ent=a[loser]['open'][e];ex=a[loser]['open'][x]
  if ent>0 and ex>0:out.append(ex/ent-1-cost_rt/1e4)
  i+=7
 return out

def main(argv=None):
 ap=argparse.ArgumentParser();ap.add_argument('--months',type=float,default=60.0);ap.add_argument('--json',default=None);args=ap.parse_args(argv)
 syms='BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,SOLUSDT,DOGEUSDT,TRXUSDT,LINKUSDT,LTCUSDT,BCHUSDT,DOTUSDT,AVAXUSDT,ETCUSDT,XLMUSDT'.split(',')
 print('=== FROZEN BROAD LIQUID SPOT 8-WEEK LOSER REVERSAL ===');print('  15 fixed majors; worst negative 56d return + vol>=cross-sectional median; long 7d');print('  spot cost 24bps RT; stress30bps; dev75/final25 sealed; raw screen')
 raw={}
 for s in syms:
  try:raw[s]=fetch(s,args.months)
  except Exception as e:print(f'  {s}: fetch failed {e}')
 if len(raw)<12:raise SystemExit('fewer than 12 fixed spot names available')
 g,a=align(raw);n=len(g);print(f'\ncommon universe: {len(a)} symbols, {n} aligned daily bars')
 warm=90
 if n<warm+500:raise SystemExit('not enough common history')
 split=warm+int((n-warm)*0.75)
 variants={'PRIMARY_56d':(56,True),'formation_49d':(49,True),'formation_63d':(63,True),'formation_70d':(70,True),'no_vol_filter':(56,False)};dev={}
 print('\nDEVELOPMENT 75%')
 for name,(fm,vf) in variants.items():dev[name]=summ(run(g,a,fm,7,vf,24.,warm,split));print(f'  {name:15s} {fmt(dev[name])}')
 bounds=np.linspace(warm,split,4,dtype=int);folds=[];print('\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS')
 for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):
  st=summ(run(g,a,56,7,True,24.,int(lo),int(hi)));folds.append(st);print(f'  fold {j}/3 {fmt(st)}')
 stress=summ(run(g,a,56,7,True,30.,warm,split));print('\nCOST STRESS 30bps RT\n  '+fmt(stress))
 p=dev['PRIMARY_56d'];pf=sum(x['mean_bps']>0 for x in folds);pr=sum(x['mean_bps']>0 for x in dev.values())
 checks={'development_mean_positive':p['mean_bps']>0,'development_t_at_least_1_8':p['t_stat']>=1.8,'development_at_least_40_trades':p['n']>=40,'at_least_2_of_3_folds_positive':pf>=2,'at_least_4_of_5_rules_positive':pr>=4,'stress_mean_positive':stress['mean_bps']>0,'one_sided_p_at_most_0_05':p['p']<=0.05}
 print('\nDEVELOPMENT HURDLES');[print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in checks.items()]
 dp=all(checks.values());final=fs=None;passed=False
 if dp:
  print('\nDEVELOPMENT PASS — opening sealed FINAL 25%');final=summ(run(g,a,56,7,True,24.,split,n));fs=summ(run(g,a,56,7,True,30.,split,n));print('  FINAL  '+fmt(final));print('  STRESS '+fmt(fs));passed=final['mean_bps']>0 and final['n']>=10 and fs['mean_bps']>0
 else:print('\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened')
 payload={'strategy':'broad_spot_weekly_high_vol_loser_reversal_56d','symbols':list(a),'aligned_bars':n,'development':p,'variants':dev,'folds':folds,'stress':stress,'checks':checks,'development_passed':dp,'final':final,'final_stress':fs,'sealed_final_opened':dp,'passed':passed,'live_orders_sent':0,'raw_screen_only':True,'survivorship_caveat':True}
 print('\n[broad-spot-reversal-summary] '+json.dumps(payload,sort_keys=True))
 if args.json:
  with open(args.json,'w',encoding='utf-8') as q:json.dump(payload,q,indent=2);print(f'wrote {args.json}')
 print('\n'+('PASS RAW SCREEN: next step is $15 minimum/order replay.' if passed else 'REJECTED SCREEN: do not paper/live deploy.'));return 0 if passed else 2
if __name__=='__main__':raise SystemExit(main())
