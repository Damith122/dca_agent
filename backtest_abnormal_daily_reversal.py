#!/usr/bin/env python3
"""Secondary frozen abnormal one-day move -> next-day REVERSAL study.

Triggered by the literature documenting both intraday momentum and reversal.
Because the paired continuation hypothesis was already inspected and failed,
this study uses stricter development admission (sign-flip p<=0.10, PF>=1.30)
and keeps the same untouched final 25% protocol.
"""
from __future__ import annotations
import argparse, json, math, os
from dataclasses import asdict, replace
import numpy as np
import backtest_abnormal_daily_continuation as B
from backtest_tsmom import fetch_inputs
from paper_tsmom import fetch_execution_filters
from tsmom import align_candles
from breakout import atr_series


def run(candles, funding, minimums, steps, p, equity0, start, end):
    grid, aligned = align_candles(candles)
    atrs={s:atr_series(rows,p.atr_period) for s,rows in aligned.items()}
    closes={s:np.asarray([b.close for b in rows],dtype=float) for s,rows in aligned.items()}
    wallet=float(equity0); pos=None; pending=None
    trades=[]; curve=[]; curve_ts=[]; blocked=floored=0
    def close_pos(price,ts,reason):
        nonlocal wallet,pos
        if pos is None: return
        gross=pos['side']*pos['qty']*(price-pos['entry'])
        fee=abs(pos['qty']*price)*p.cost_bps_side/1e4
        wallet += gross-fee
        trades.append({'symbol':pos['symbol'],'side':pos['side'],'net':gross+pos['funding']-pos['entry_fee']-fee,'reason':reason})
        pos=None
    for i in range(len(grid)):
        if i>=end: break
        ts=grid[i]; active=i>=start
        if pos is not None and i>pos['entry_i']:
            close_pos(aligned[pos['symbol']][i].open,ts,'time')
        if active and pending is not None and pos is None:
            sym,side,atr=pending; bar=aligned[sym][i]
            stop_frac=p.stop_atr*atr/bar.open if bar.open>0 else 0.0
            lev=min(p.max_leverage,p.risk_pct/stop_frac) if stop_frac>0 else 0.0
            desired=wallet*max(0.0,lev); qty=desired/bar.open if bar.open>0 else 0.0
            step=float(steps.get(sym,0.0))
            if step>0 and qty>0: qty=math.floor(qty/step+1e-12)*step
            notional=qty*bar.open; minimum=float(minimums.get(sym,0.0))
            if desired>0 and notional+1e-12<minimum:
                min_qty=minimum/bar.open if bar.open>0 else 0.0
                if step>0 and min_qty>0: min_qty=math.ceil(min_qty/step-1e-12)*step
                min_notional=min_qty*bar.open
                floor_risk=min_notional*stop_frac/wallet if wallet>0 else float('inf')
                floor_lev=min_notional/wallet if wallet>0 else float('inf')
                if floor_risk<=0.03 and floor_lev<=p.max_leverage:
                    qty,notional=min_qty,min_notional; floored+=1
                else:
                    qty=0.0; blocked+=1
            if qty>0:
                entry_fee=notional*p.cost_bps_side/1e4; wallet-=entry_fee
                stop=bar.open-side*p.stop_atr*atr
                pos={'symbol':sym,'side':side,'entry_i':i,'entry':bar.open,'qty':qty,'stop':stop,'entry_fee':entry_fee,'funding':0.0}
        pending=None
        if pos is not None:
            bar=aligned[pos['symbol']][i]; day=int(ts//86400*86400)
            rb=float(funding.get(pos['symbol'],{}).get(day,0.0))
            fp=-pos['side']*abs(pos['qty']*bar.close)*rb/1e4
            wallet+=fp; pos['funding']+=fp
            hit=(pos['side']>0 and bar.low<=pos['stop']) or (pos['side']<0 and bar.high>=pos['stop'])
            if hit: close_pos(pos['stop'],ts,'stop')
        mark=wallet
        if pos is not None:
            bar=aligned[pos['symbol']][i]; mark+=pos['side']*pos['qty']*(bar.close-pos['entry'])
        if active: curve.append(mark); curve_ts.append(ts)
        if active and i+1<end and i>=p.vol_lookback+1:
            candidates=[]
            for sym,arr in closes.items():
                if arr[i-1]<=0 or arr[i]<=0: continue
                r1=math.log(arr[i]/arr[i-1])
                hist=np.diff(np.log(arr[i-p.vol_lookback:i+1]))
                sigma=float(hist[:-1].std(ddof=1)) if len(hist)>2 else 0.0
                atr=atrs[sym][i] if i<len(atrs[sym]) else None
                if sigma<=1e-12 or atr is None or atr<=0: continue
                z=r1/sigma
                if abs(z)>=p.abnormal_sigma:
                    # REVERSAL: trade opposite the abnormal move.
                    candidates.append((abs(z),sym,-1 if z>0 else 1,float(atr)))
            if candidates:
                _,sym,side,atr=max(candidates,key=lambda x:(x[0],x[1])); pending=(sym,side,atr)
    if pos is not None:
        i=min(end,len(grid))-1; close_pos(aligned[pos['symbol']][i].close,grid[i],'end')
        if curve: curve[-1]=wallet
    return B._stats(trades,curve,curve_ts,equity0),trades,blocked,floored


def main(argv=None):
    for k in ('BINANCE_API_KEY','BINANCE_API_SECRET','API_KEY','API_SECRET'): os.environ.pop(k,None)
    ap=argparse.ArgumentParser(); ap.add_argument('--symbols',default=B.DEFAULT_SYMBOLS); ap.add_argument('--months',type=float,default=48.0); ap.add_argument('--starting-equity',type=float,default=15.0); ap.add_argument('--json',default=None); a=ap.parse_args(argv)
    symbols=[x.strip().upper() for x in a.symbols.split(',') if x.strip()]
    print('=== FROZEN ABNORMAL DAILY REVERSAL — SECONDARY HYPOTHESIS ===')
    print('  |1d return| >= 1.5 x trailing-20d sigma; trade OPPOSITE direction next open')
    print('  hold 1 day; 2ATR stop; <=1x; 2% risk; DCA=0; 7bps/side; exact funding; 10bps stress')
    print('  stricter due paired-hypothesis inspection: dev PF>=1.30 and sign-flip p<=0.10')
    candles,funding=fetch_inputs(symbols,a.months); minimums,steps=fetch_execution_filters(symbols)
    grid,_=align_candles(candles); p=B.Params(); warm=max(p.vol_lookback,p.atr_period)+10; n=len(grid); final_start=warm+int((n-warm)*0.75)
    dev,trades,blocked,floored=run(candles,funding,minimums,steps,p,a.starting_equity,warm,final_start)
    print('\nDEVELOPMENT 75%\n  PRIMARY '+B.fmt(dev)+f' blocks={blocked} floors={floored}')
    bounds=np.linspace(warm,final_start,4,dtype=int); folds=[]
    print('\nTHREE CHRONOLOGICAL DEVELOPMENT FOLDS')
    for j,(lo,hi) in enumerate(zip(bounds[:-1],bounds[1:]),1):
        r,_,_,_=run(candles,funding,minimums,steps,p,a.starting_equity,int(lo),int(hi)); folds.append(r); print(f'  fold {j}/3 '+B.fmt(r))
    variants={'sigma_1_25':replace(p,abnormal_sigma=1.25),'sigma_1_75':replace(p,abnormal_sigma=1.75),'vol_15':replace(p,vol_lookback=15),'vol_30':replace(p,vol_lookback=30)}
    neighbors={}; print('\nNEIGHBOUR ROBUSTNESS — DEVELOPMENT ONLY')
    for name,q in variants.items():
        r,_,_,_=run(candles,funding,minimums,steps,q,a.starting_equity,warm,final_start); neighbors[name]=r; print(f'  {name:12s} '+B.fmt(r))
    stress_p=replace(p,cost_bps_side=10.0); stress,_,_,_=run(candles,funding,minimums,steps,stress_p,a.starting_equity,warm,final_start)
    sign_p=B._sign_flip_p([t['net'] for t in trades]); pos_folds=sum(r['net_pnl']>0 for r in folds); pos_rules=sum(r['net_pnl']>0 for r in [dev,*neighbors.values()])
    print('\nCOST STRESS 10 bps/side\n  '+B.fmt(stress)+f'\n  trade sign-flip p={sign_p:.3f}')
    checks={'development_fee_net_positive':dev['net_pnl']>0,'development_pf_at_least_1_30':dev['profit_factor']>=1.30,'development_at_least_20_trades':dev['trades']>=20,'at_least_2_of_3_folds_positive':pos_folds>=2,'at_least_4_of_5_rules_positive':pos_rules>=4,'positive_at_stressed_cost':stress['net_pnl']>0,'development_drawdown_below_20pct':dev['max_drawdown_pct']<20,'development_cagr_at_least_3pct':dev['cagr_pct']>=3,'trade_sign_flip_p_at_most_0_10':sign_p<=0.10}
    print('\nDEVELOPMENT ADMISSION HURDLES'); [print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in checks.items()]
    dev_pass=all(checks.values()); final=final_stress=None; final_checks={}
    if dev_pass:
        print('\nDEVELOPMENT PASS — opening sealed FINAL 25%')
        final,_,_,_=run(candles,funding,minimums,steps,p,a.starting_equity,final_start,n); final_stress,_,_,_=run(candles,funding,minimums,steps,stress_p,a.starting_equity,final_start,n)
        print('  FINAL  '+B.fmt(final)); print('  STRESS '+B.fmt(final_stress))
        final_checks={'final_positive':final['net_pnl']>0,'final_pf_at_least_1_20':final['profit_factor']>=1.20,'final_at_least_8_trades':final['trades']>=8,'final_stress_positive':final_stress['net_pnl']>0,'final_drawdown_below_20pct':final['max_drawdown_pct']<20}
        [print(f"  {'PASS' if v else 'FAIL':4s} {k}") for k,v in final_checks.items()]
    else: print('\nDEVELOPMENT FAIL — sealed FINAL 25% remains unopened')
    passed=dev_pass and all(final_checks.values())
    summary={'strategy':'abnormal_daily_reversal_secondary','development_passed':dev_pass,'sealed_final_opened':dev_pass,'passed':passed,'dev_net_pnl':dev['net_pnl'],'dev_pf':dev['profit_factor'],'dev_cagr_pct':dev['cagr_pct'],'dev_dd_pct':dev['max_drawdown_pct'],'dev_trades':dev['trades'],'stress_net_pnl':stress['net_pnl'],'sign_flip_p':sign_p,'positive_folds':pos_folds,'positive_rules':pos_rules,'final_net_pnl':final['net_pnl'] if final else None,'final_pf':final['profit_factor'] if final else None,'live_orders_sent':0,'secondary_hypothesis':True}
    print('\n[abnormal-reversal-summary] '+json.dumps(summary,sort_keys=True)); print('\n'+('PASS: eligible for paper-only observation.' if passed else 'REJECTED: do not paper/live deploy this candidate.'))
    if a.json:
        with open(a.json,'w',encoding='utf-8') as fh: json.dump({'summary':summary,'params':asdict(p),'development':dev,'neighbors':neighbors,'folds':folds,'stress':stress,'final':final,'final_stress':final_stress,'development_checks':checks,'final_checks':final_checks,'research_only':True},fh,indent=2,sort_keys=True)
        print('wrote '+a.json)
    return 0 if passed else 2
if __name__=='__main__': raise SystemExit(main())
