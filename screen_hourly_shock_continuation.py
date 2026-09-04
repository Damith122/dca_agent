#!/usr/bin/env python3
"""Frozen raw screen: abnormal 1h move -> next 4h continuation.

Research-only signal screen. It deliberately stops BEFORE wallet sizing or
exchange execution code. If the raw forward edge cannot clear realistic costs,
there is no reason to build a trading engine around it.

Primary pre-registered rule:
- Six USD-M perpetuals: SOL,SUI,BNB,XRP,TRX,DOGE.
- Completed 1h return must exceed 1.5x the stdev of the PRIOR 48 hourly returns.
- Among simultaneous events choose the largest absolute standardized shock.
- Direction = same as the shock (continuation).
- Earliest entry = next hour open; exit = open four hours later.
- At most one event exposure at a time (no overlapping signals).
- Base cost 7 bps/side = 14 bps round trip; stress 10 bps/side = 20 bps RT.
- First 75% development; final 25% sealed until development gates pass.
"""
from __future__ import annotations
import argparse, json, math, os
from dataclasses import dataclass, asdict, replace
import numpy as np
import backtest_breakout
from tsmom import align_candles

DEFAULT_SYMBOLS="SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"

@dataclass(frozen=True)
class Params:
    vol_lookback: int=48
    shock_sigma: float=1.5
    hold_hours: int=4
    cost_bps_side: float=7.0


def sign_flip_p(values,runs=4000):
    a=np.asarray([x for x in values if np.isfinite(x)],dtype=float)
    if not len(a): return 1.0
    obs=float(a.mean()); rng=np.random.default_rng(20260904); exceed=0
    for _ in range(runs):
        s=rng.choice((-1.0,1.0),size=len(a))
        if float(np.mean(a*s))>=obs: exceed+=1
    return float((exceed+1)/(runs+1))


def t_stat(a):
    a=np.asarray(a,dtype=float)
    if len(a)<2: return 0.0
    sd=float(a.std(ddof=1))
    return float(a.mean()/(sd/math.sqrt(len(a)))) if sd>1e-15 else 0.0


def metrics(values):
    a=np.asarray(values,dtype=float)
    return {
        "n":int(len(a)),
        "mean_bps":float(a.mean()*1e4) if len(a) else 0.0,
        "median_bps":float(np.median(a)*1e4) if len(a) else 0.0,
        "positive_pct":float((a>0).mean()*100) if len(a) else 0.0,
        "t_stat":t_stat(a),
        "sum_return_pct":float(a.sum()*100) if len(a) else 0.0,
    }


def evaluate(aligned,grid,p,start,end):
    closes={s:np.asarray([b.close for b in rows],dtype=float) for s,rows in aligned.items()}
    rets={s:np.concatenate([[np.nan],np.diff(np.log(c))]) for s,c in closes.items()}
    values=[]; shocks=[]; i=max(start,p.vol_lookback+1)
    while i<end-p.hold_hours-1:
        candidates=[]
        for sym,r in rets.items():
            hist=r[i-p.vol_lookback:i]
            hist=hist[np.isfinite(hist)]
            if len(hist)<max(20,p.vol_lookback-2): continue
            sigma=float(hist.std(ddof=1))
            cur=float(r[i])
            if sigma<=1e-12 or not math.isfinite(cur): continue
            z=cur/sigma
            if abs(z)>=p.shock_sigma:
                candidates.append((abs(z),sym,1 if z>0 else -1,z))
        if not candidates:
            i+=1; continue
        _,sym,side,z=max(candidates,key=lambda x:(x[0],x[1]))
        entry_i=i+1; exit_i=entry_i+p.hold_hours
        if exit_i>=end: break
        entry=float(aligned[sym][entry_i].open); exit_px=float(aligned[sym][exit_i].open)
        if entry>0:
            gross=side*(exit_px-entry)/entry
            net=gross-2*p.cost_bps_side/1e4
            values.append(net); shocks.append(float(z))
        i=exit_i  # no overlapping position exposure
    m=metrics(values); m["sign_flip_p"]=sign_flip_p(values); m["mean_abs_shock_sigma"]=float(np.mean(np.abs(shocks))) if shocks else 0.0
    return m,values


def fmt(m):
    return (f"n={m['n']:4d} mean={m['mean_bps']:+7.2f}bps med={m['median_bps']:+7.2f}bps "
            f"pos={m['positive_pct']:5.1f}% t={m['t_stat']:+5.2f} sum={m['sum_return_pct']:+7.2f}% p={m['sign_flip_p']:.3f}")


def main(argv=None):
    for k in ('BINANCE_API_KEY','BINANCE_API_SECRET','API_KEY','API_SECRET'): os.environ.pop(k,None)
    ap=argparse.ArgumentParser(); ap.add_argument('--symbols',default=DEFAULT_SYMBOLS); ap.add_argument('--months',type=float,default=18.0); ap.add_argument('--json',default=None); a=ap.parse_args(argv)
    syms=[x.strip().upper() for x in a.symbols.split(',') if x.strip()]
    print('=== FROZEN HOURLY SHOCK CONTINUATION RAW SCREEN ===')
    print('  |1h return| >=1.5 x PRIOR-48h sigma; same-direction next-open to +4h open')
    print('  no overlap; base=14bps round trip; stress=20bps; dev75/final25 sealed')
    series={}
    for sym in syms:
        print(f'\n=== {sym} 1h public history ==='); series[sym]=backtest_breakout.fetch(sym,'1h',a.months)
    grid,aligned=align_candles(series)
    p=Params(); warm=p.vol_lookback+10; n=len(grid); final_start=warm+int((n-warm)*0.75)
    dev,dev_vals=evaluate(aligned,grid,p,warm,final_start)
    print('\nDEVELOPMENT 75%\n  PRIMARY '+fmt(dev))
    bounds=np.linspace(warm,final_start,4,dtype=int); folds=[]
    print('\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS')
    for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):
        r,_=evaluate(aligned,grid,p,int(lo),int(hi)); folds.append(r); print(f'  fold {j}/3 '+fmt(r))
    variants={
        'sigma_1_25':replace(p,shock_sigma=1.25),
        'sigma_1_75':replace(p,shock_sigma=1.75),
        'vol_36h':replace(p,vol_lookback=36),
        'vol_72h':replace(p,vol_lookback=72),
    }
    neighbors={}; print('\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY')
    for name,q in variants.items():
        r,_=evaluate(aligned,grid,q,warm,final_start); neighbors[name]=r; print(f'  {name:10s} '+fmt(r))
    stress_p=replace(p,cost_bps_side=10.0); stress,_=evaluate(aligned,grid,stress_p,warm,final_start)
    print('\nCOST STRESS 20 bps round trip\n  '+fmt(stress))
    pos_folds=sum(r['mean_bps']>0 for r in folds); pos_rules=sum(r['mean_bps']>0 for r in [dev,*neighbors.values()])
    checks={
        'development_cost_adjusted_mean_positive':dev['mean_bps']>0,
        'development_t_at_least_2':dev['t_stat']>=2.0,
        'development_at_least_100_events':dev['n']>=100,
        'at_least_2_of_3_folds_positive':pos_folds>=2,
        'at_least_4_of_5_rules_positive':pos_rules>=4,
        'stress_mean_positive':stress['mean_bps']>0,
        'sign_flip_p_at_most_0_10':dev['sign_flip_p']<=0.10,
    }
    print('\nDEVELOPMENT SCREEN HURDLES'); [print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in checks.items()]
    dev_pass=all(checks.values()); final=final_stress=None; final_checks={}
    if dev_pass:
        print('\nDEVELOPMENT PASS — opening sealed FINAL 25%')
        final,_=evaluate(aligned,grid,p,final_start,n); final_stress,_=evaluate(aligned,grid,stress_p,final_start,n)
        print('  FINAL  '+fmt(final)); print('  STRESS '+fmt(final_stress))
        final_checks={
            'final_mean_positive':final['mean_bps']>0,
            'final_t_at_least_1':final['t_stat']>=1.0,
            'final_at_least_30_events':final['n']>=30,
            'final_stress_mean_positive':final_stress['mean_bps']>0,
        }
        [print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in final_checks.items()]
    else: print('\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened')
    passed=dev_pass and all(final_checks.values())
    summary={'strategy':'hourly_shock_4h_continuation_screen','development_passed':dev_pass,'sealed_final_opened':dev_pass,'passed':passed,'development':dev,'stress':stress,'positive_folds':pos_folds,'positive_rules':pos_rules,'final':final,'final_stress':final_stress,'live_orders_sent':0,'raw_screen_only':True}
    print('\n[hourly-shock-summary] '+json.dumps(summary,sort_keys=True)); print('\n'+('PASS SCREEN: eligible for a separate execution-aware validator.' if passed else 'REJECTED SCREEN: do not build execution logic for this rule.'))
    if a.json:
        with open(a.json,'w',encoding='utf-8') as fh: json.dump({'summary':summary,'params':asdict(p),'folds':folds,'neighbors':neighbors,'development_checks':checks,'final_checks':final_checks,'research_only':True},fh,indent=2,sort_keys=True)
        print('wrote '+a.json)
    return 0 if passed else 2
if __name__=='__main__': raise SystemExit(main())
