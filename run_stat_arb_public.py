#!/usr/bin/env python3
"""Fetch public 1h candles then run the existing walk-forward/null-control stat-arb screen.

This wrapper does not change the stat-arb model. It only supplies current public
history so the existing out-of-sample and synthetic-random-walk controls can run
inside Railway. Base test uses 14 bps round trip PER LEG. Funding is deliberately
not added at this raw-screen stage: only a strong fee-adjusted pass is eligible
for a separate exact-funding execution-aware validation.
"""
from __future__ import annotations
import argparse,csv,os,tempfile
import backtest_breakout,backtest_stat_arb

def main(argv=None):
 ap=argparse.ArgumentParser();ap.add_argument('--months',type=float,default=18.0);ap.add_argument('--fee-bps',type=float,default=14.0);ap.add_argument('--null-universes',type=int,default=100);ap.add_argument('--json',default=None);args=ap.parse_args(argv)
 syms=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT']
 d=tempfile.mkdtemp(prefix='stat_arb_')
 print(f'=== PUBLIC STAT-ARB RAW SCREEN: {len(syms)} symbols, {args.months:g} months, {args.fee_bps:g} bps/leg RT ===')
 for s in syms:
  print(f'\n=== {s} 1h ===');cs=backtest_breakout.fetch(s,'1h',args.months)
  with open(os.path.join(d,f'{s}.csv'),'w',newline='',encoding='utf-8') as f:
   w=csv.writer(f);w.writerow(['ts','open','high','low','close','volume'])
   for c in cs:w.writerow([c.ts,c.open,c.high,c.low,c.close,c.volume])
 cmd=['--csv',d,'--interval','1h','--symbols',','.join(syms),'--fee-bps',str(args.fee_bps),'--z-entry','2.0','--z-exit','0.5','--z-stop','4.0','--folds','6','--alpha','0.05','--max-hold','200','--null-universes',str(args.null_universes)]
 if args.json:cmd += ['--json',args.json]
 return backtest_stat_arb.main(cmd)
if __name__=='__main__':raise SystemExit(main())
