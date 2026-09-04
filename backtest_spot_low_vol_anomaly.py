#!/usr/bin/env python3
"""Frozen broad-spot low-volatility anomaly screen.

Primary is fixed before evaluation:
- 15 fixed liquid Binance spot USDT majors
- trailing 60 completed daily log-return volatility
- every 28 days choose the LOWEST-volatility asset
- decision on completed close, enter next daily open, exit 28 days later at open
- long-only, one position, no overlap
- 24 bps round-trip base spot cost; 30 bps stress
- first 75% development; sealed final 25% opens only if dev hurdles pass

Neighbours are robustness checks only, never used to retune the primary:
30d/90d volatility formation and 14d/42d holding periods.
"""
from __future__ import annotations
import json, math, sys, time, urllib.parse, urllib.request
from statistics import NormalDist
import numpy as np

SYMS = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","ADAUSDT","SOLUSDT","DOGEUSDT","TRXUSDT","LINKUSDT","LTCUSDT","BCHUSDT","DOTUSDT","AVAXUSDT","ETCUSDT","XLMUSDT"]
BASE_RT_BPS = 24.0
STRESS_RT_BPS = 30.0
MONTHS = 60.0


def fetch_spot(symbol, months=MONTHS):
    end = int(time.time()*1000)
    start = end - int(months*30.44*86400000)
    rows = {}
    cursor = start
    while cursor < end:
        q = urllib.parse.urlencode({"symbol":symbol,"interval":"1d","startTime":cursor,"limit":1000})
        with urllib.request.urlopen("https://api.binance.com/api/v3/klines?"+q, timeout=30) as r:
            data = json.loads(r.read().decode())
        if not data: break
        for x in data:
            rows[int(x[0])] = (float(x[1]), float(x[4]))
        nxt = int(data[-1][0]) + 1
        if nxt <= cursor: break
        cursor = nxt
        time.sleep(0.03)
    ts = np.array(sorted(rows), dtype=np.int64)
    op = np.array([rows[t][0] for t in ts], float)
    cl = np.array([rows[t][1] for t in ts], float)
    return ts, op, cl


def align(raw):
    common = None
    for ts,_,_ in raw.values():
        s = set(ts.tolist())
        common = s if common is None else common & s
    grid = np.array(sorted(common), dtype=np.int64)
    out = {}
    for s,(ts,op,cl) in raw.items():
        idx = {int(t):i for i,t in enumerate(ts)}
        out[s] = (np.array([op[idx[int(t)]] for t in grid]), np.array([cl[idx[int(t)]] for t in grid]))
    return grid, out


def stats(xs):
    a = np.asarray(xs,float)
    if len(a)==0:
        return {"n":0,"mean_bps":0.0,"median_bps":0.0,"positive_pct":0.0,"t_stat":0.0,"p":1.0,"sum_return_pct":0.0,"pf":0.0}
    mean = float(a.mean()); med=float(np.median(a)); sd=float(a.std(ddof=1)) if len(a)>1 else 0.0
    t = mean/(sd/math.sqrt(len(a))) if sd>0 else 0.0
    p = 1.0-NormalDist().cdf(t)
    wins=a[a>0]; losses=a[a<=0]
    pf=float(wins.sum()/-losses.sum()) if len(losses) and losses.sum()<0 else float('inf')
    return {"n":int(len(a)),"mean_bps":mean,"median_bps":med,"positive_pct":float((a>0).mean()*100),"t_stat":float(t),"p":float(p),"sum_return_pct":float(a.sum()/100),"pf":pf}


def run(grid, data, formation=60, hold=28, rt_bps=BASE_RT_BPS, lo=0, hi=None):
    n=len(grid); hi=n if hi is None else min(hi,n)
    start=max(lo, formation+2)
    out=[]; high_out=[]; picked=[]
    i=start
    while i+1+hold < hi:
        vols=[]
        for s,(op,cl) in data.items():
            lr=np.diff(np.log(cl[i-formation:i+1]))
            vols.append((float(lr.std(ddof=1)),s))
        vols.sort()
        low=vols[0][1]; high=vols[-1][1]
        for sym,bucket in ((low,out),(high,high_out)):
            op,_=data[sym]
            gross=(op[i+1+hold]/op[i+1]-1.0)*1e4
            bucket.append(gross-rt_bps)
        picked.append(low)
        i += hold
    return out, high_out, picked


def fmt(st):
    return f"n={st['n']:3d} mean={st['mean_bps']:+8.2f}bps med={st['median_bps']:+8.2f}bps pos={st['positive_pct']:5.1f}% t={st['t_stat']:+5.2f} p={st['p']:.3f} PF={st['pf']:.2f} sum={st['sum_return_pct']:+7.2f}%"


def main():
    print("=== FROZEN BROAD SPOT LOW-VOLATILITY ANOMALY ===")
    print("  lowest trailing-60d realized vol; long next-open 28d; 24bps RT base / 30bps stress")
    print("  15 fixed majors; dev75/final25 sealed; neighbours pre-registered")
    raw={}
    for s in SYMS:
        ts,op,cl=fetch_spot(s)
        raw[s]=(ts,op,cl)
        print(f"  {s}: {len(ts)} daily bars")
    grid,data=align(raw)
    n=len(grid); warm=100; dev_end=warm+int((n-warm)*0.75)
    print(f"common: {len(data)} symbols, {n} aligned daily bars; dev_end index={dev_end}")

    rules={
      "PRIMARY_60d_hold28":(60,28),
      "formation_30d":(30,28),
      "formation_90d":(90,28),
      "hold_14d":(60,14),
      "hold_42d":(60,42),
    }
    dev_stats={}
    print("\nDEVELOPMENT 75%")
    for name,(f,h) in rules.items():
        xs,hi,_=run(grid,data,f,h,BASE_RT_BPS,warm,dev_end)
        st=stats(xs); dev_stats[name]=st
        print(f"  {name:<20} {fmt(st)}")
        if name=="PRIMARY_60d_hold28":
            hs=stats(hi)
            spread=np.asarray(xs)-np.asarray(hi)
            print(f"  HIGH-VOL comparator   {fmt(hs)}")
            print(f"  LOW-minus-HIGH spread {fmt(stats(spread))}")

    primary=dev_stats["PRIMARY_60d_hold28"]
    folds=[]
    b=np.linspace(warm,dev_end,4,dtype=int)
    print("\nTHREE DEVELOPMENT FOLDS")
    for j,(lo,hi) in enumerate(zip(b[:-1],b[1:]),1):
        xs,_,_=run(grid,data,60,28,BASE_RT_BPS,int(lo),int(hi))
        st=stats(xs); folds.append(st); print(f"  fold {j}/3 {fmt(st)}")
    sx,_,_=run(grid,data,60,28,STRESS_RT_BPS,warm,dev_end)
    stress=stats(sx)
    print("\nSTRESS 30bps RT")
    print("  "+fmt(stress))
    checks={
      "development_mean_positive": primary["mean_bps"]>0,
      "development_t_at_least_1_8": primary["t_stat"]>=1.8,
      "development_at_least_30_trades": primary["n"]>=30,
      "at_least_2_of_3_folds_positive": sum(x["mean_bps"]>0 for x in folds)>=2,
      "at_least_4_of_5_rules_positive": sum(x["mean_bps"]>0 for x in dev_stats.values())>=4,
      "stress_mean_positive": stress["mean_bps"]>0,
      "p_at_most_0_05": primary["p"]<=0.05,
    }
    print("\nDEVELOPMENT HURDLES")
    for k,v in checks.items(): print(f"  {'PASS' if v else 'FAIL'} {k}")
    dev_pass=all(checks.values())
    final=final_stress=None
    if dev_pass:
        print("\nDEVELOPMENT PASS — opening sealed FINAL 25%")
        fx,_,_=run(grid,data,60,28,BASE_RT_BPS,dev_end,n)
        fsx,_,_=run(grid,data,60,28,STRESS_RT_BPS,dev_end,n)
        final=stats(fx); final_stress=stats(fsx)
        print("  FINAL  "+fmt(final)); print("  STRESS "+fmt(final_stress))
    else:
        print("\nDEVELOPMENT FAIL — FINAL25 remains sealed")
    passed=bool(dev_pass and final and final["mean_bps"]>0 and final_stress["mean_bps"]>0 and final["pf"]>=1.10)
    payload={"strategy":"broad_spot_low_vol_60d_hold28","symbols":SYMS,"aligned_bars":n,"development":primary,"variants":dev_stats,"folds":folds,"stress":stress,"checks":checks,"development_passed":dev_pass,"sealed_final_opened":dev_pass,"final":final,"final_stress":final_stress,"passed":passed,"live_orders_sent":0,"survivorship_caveat":True}
    print("\n[low-vol-summary] "+json.dumps(payload,sort_keys=True))
    with open('/tmp/LOW_VOL_20260904_validation_summary.json','w') as f: json.dump(payload,f,indent=2)
    print("wrote /tmp/LOW_VOL_20260904_validation_summary.json")
    print("PASS — paper admission only" if passed else "REJECTED / NOT ADMITTED")
    return 0 if passed else 2

if __name__=='__main__': sys.exit(main())
