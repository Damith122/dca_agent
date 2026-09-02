#!/usr/bin/env python3
"""Historical backtest of the Donchian breakout engine.

    python3 backtest_breakout.py --symbols SOLUSDT,ETHUSDT,NEARUSDT,SUIUSDT \
                                 --interval 1h --months 6

Fetches OHLCV straight from Binance USD-M futures (public endpoint, no API
key needed), runs `breakout.run` - the SAME code the live bot executes - and
reports net-of-fee performance.

Every figure printed is AFTER subtracting the round-trip fee (default 7.32
bps, the measured blended rate). There is no separate "gross" line, because
gross numbers are what talk people into unprofitable strategies.

Other useful flags:
    --csv DIR         backtest local CSV candles instead of fetching
                      (ts,open,high,low,close,volume per row)
    --fee-bps 4.0     model maker-only fills
    --walk-forward 4  split into N folds, tune on each and trade the next,
                      reporting only out-of-sample results
    --json out.json   dump full results for later comparison

If the fetch fails with a 403 or a connection error, the machine you are on
cannot reach Binance (some clouds and CI networks block it). Run it from the
Railway container or your own machine.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List

from breakout import BreakoutParams, Candle, run, stats

FAPI = "https://fapi.binance.com/fapi/v1/klines"
MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
      "4h": 14_400_000, "1d": 86_400_000}


def fetch(symbol: str, interval: str, months: float, verbose: bool = True) -> List[Candle]:
    """Page backwards through Binance klines until `months` of history is in
    hand. 1500 bars per request is the documented maximum."""
    step = MS.get(interval)
    if step is None:
        raise SystemExit(f"unsupported interval {interval!r}; use one of {sorted(MS)}")
    end = int(time.time() * 1000)
    start = end - int(months * 30.44 * 86_400_000)
    out: Dict[int, Candle] = {}
    cursor = start
    while cursor < end:
        url = (f"{FAPI}?symbol={symbol}&interval={interval}"
               f"&startTime={cursor}&limit=1500")
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                rows = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Binance returned HTTP {e.code} for {symbol}: {e.reason}")
        except Exception as e:  # noqa: BLE001
            raise SystemExit(
                f"could not reach Binance for {symbol}: {e}\n"
                "This machine may be blocked from api/fapi.binance.com. Run the "
                "backtest from the Railway container or a local machine, or pass "
                "--csv with downloaded candles.")
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = Candle(ts=int(r[0]) / 1000.0, open=float(r[1]),
                                    high=float(r[2]), low=float(r[3]),
                                    close=float(r[4]), volume=float(r[5]))
        cursor = int(rows[-1][0]) + step
        if verbose:
            print(f"\r  {symbol} {interval}: {len(out)} bars", end="", flush=True)
        time.sleep(0.25)          # stay well inside the public rate limit
    if verbose:
        print()
    return [out[k] for k in sorted(out)]


def load_csv(directory: str, symbol: str) -> List[Candle]:
    path = os.path.join(directory, f"{symbol}.csv")
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or row[0].lower().startswith(("ts", "time", "open_time")):
                continue
            ts = float(row[0])
            if ts > 1e11:         # milliseconds
                ts /= 1000.0
            out.append(Candle(ts, float(row[1]), float(row[2]), float(row[3]),
                              float(row[4]), float(row[5]) if len(row) > 5 else 0.0))
    out.sort(key=lambda c: c.ts)
    return out


# The volatility filter is tuned here rather than guessed. Measured on 42h of
# recorded ticks, 1h ATR(14)/price ran 0.74%-1.57% and 15m ran 0.27%-0.90%, so
# a single hard-coded floor cannot serve both timeframes - and 42h is far too
# little to fix one from anyway. Let the walk-forward pick it out of sample.
GRID = {
    "channel": [20, 40, 60],
    "stop_atr": [1.5, 2.5],
    "tp_atr": [4.0, 8.0],
    "trail_atr": [2.0, 3.0],
    "atr_floor": [0.0, 0.004, 0.010],
}


def grid_params(base: BreakoutParams):
    keys = sorted(GRID)
    for combo in itertools.product(*(GRID[k] for k in keys)):
        p = BreakoutParams(**vars(base))
        for k, v in zip(keys, combo):
            setattr(p, k, v)
        yield p


def walk_forward(candles: List[Candle], base: BreakoutParams, folds: int):
    """Tune on everything seen so far, trade the next fold, never look ahead.

    Only the concatenated out-of-sample trades are reported. In-sample results
    from a parameter search are a description of the past, not a forecast.
    """
    size = len(candles) // (folds + 1)
    oos_trades, equity, curve = [], 1000.0, [1000.0]
    for f in range(folds):
        train = candles[:size * (f + 1)]
        test = candles[size * (f + 1):size * (f + 2)]
        if len(train) < base.channel + base.atr_period + 10 or len(test) < 10:
            continue
        best, best_ret = None, -1e9
        for p in grid_params(base):
            t, c = run(train, p)
            if len(t) >= 5 and c[-1] > best_ret:
                best, best_ret = p, c[-1]
        if best is None:
            continue
        t, c = run(test, best, equity=equity)
        scale = c[-1] / c[0] if c[0] else 1.0
        equity *= scale
        curve.extend([equity * (v / c[0]) / scale for v in c[1:]])
        oos_trades.extend(t)
    return oos_trades, curve


def show(title: str, st: dict, p: BreakoutParams):
    print(f"\n--- {title} ---")
    if not st["trades"]:
        print("  no trades taken")
        return
    print(f"  trades              {st['trades']}")
    print(f"  net win rate        {st['win_rate']:.1f}%")
    print(f"  net total return    {st['total_return_pct']:+.2f}%")
    print(f"  profit factor       {st['profit_factor']:.2f}")
    print(f"  max drawdown        {st['max_drawdown_pct']:.2f}%")
    print(f"  avg win / avg loss  {st['avg_win_pct']:+.2f}% / {st['avg_loss_pct']:+.2f}%")
    print(f"  expectancy/trade    {st['expectancy_pct']:+.3f}%  "
          f"(fee {p.fee_bps_round_trip:.2f} bps already deducted)")
    print(f"  avg win / avg loss  {st['avg_win_r']:.2f}R / {st['avg_loss_r']:.2f}R  "
          f"(1R = the planned stop loss)")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SOLUSDT,ETHUSDT,NEARUSDT,SUIUSDT")
    ap.add_argument("--interval", default="1h", choices=sorted(MS))
    ap.add_argument("--months", type=float, default=6.0)
    ap.add_argument("--fee-bps", type=float, default=7.32)
    ap.add_argument("--csv", default=None, help="directory of <SYMBOL>.csv candles")
    ap.add_argument("--walk-forward", type=int, default=4)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    base = BreakoutParams(fee_bps_round_trip=a.fee_bps)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    results = {}

    print(f"=== Donchian breakout backtest, {a.interval}, "
          f"fee {a.fee_bps:.2f} bps round trip ===")
    for sym in symbols:
        candles = (load_csv(a.csv, sym) if a.csv
                   else fetch(sym, a.interval, a.months))
        if len(candles) < base.channel + base.atr_period + 50:
            print(f"  {sym}: only {len(candles)} bars, skipping")
            continue
        span_days = (candles[-1].ts - candles[0].ts) / 86400
        print(f"\n{sym}: {len(candles)} bars, {span_days:.0f} days")

        t, c = run(candles, base)
        show(f"{sym} default params (in-sample, for reference only)",
             stats(t, c, base), base)

        if a.walk_forward:
            t2, c2 = walk_forward(candles, base, a.walk_forward)
            st = stats(t2, c2, base)
            show(f"{sym} WALK-FORWARD out-of-sample ({a.walk_forward} folds)", st, base)
            results[sym] = st

    if results:
        print("\n=== portfolio summary, out-of-sample only ===")
        tot = sum(r["trades"] for r in results.values())
        avg_ret = sum(r["total_return_pct"] for r in results.values()) / len(results)
        pos = sum(1 for r in results.values() if r["total_return_pct"] > 0)
        print(f"  {tot} trades across {len(results)} symbols")
        print(f"  mean net return per symbol   {avg_ret:+.2f}%")
        print(f"  symbols net-profitable       {pos}/{len(results)}")
        print(f"  mean profit factor           "
              f"{sum(r['profit_factor'] for r in results.values()) / len(results):.2f}")
        print(f"  worst max drawdown           "
              f"{max(r['max_drawdown_pct'] for r in results.values()):.2f}%")
        print("\n  Deploy only if the OUT-OF-SAMPLE lines are profitable. "
              "In-sample results\n  from a parameter grid always look good and mean nothing.")

        # Hand the exact numbers to the sizing tool rather than making the
        # reader transcribe them. --observed-trades is the walk-forward count,
        # not the in-sample one: sizing off a number the strategy has not
        # earned out of sample is how an account gets emptied.
        tw = sum(r["trades"] for r in results.values())
        if tw:
            wr = sum(r["win_rate"] * r["trades"] for r in results.values()) / tw
            aw = sum(r["avg_win_r"] * r["trades"] for r in results.values()) / tw
            al = sum(r["avg_loss_r"] * r["trades"] for r in results.values()) / tw
            print("\n=== next step: size it ===")
            print(f"  python3 risk_simulator.py --win-rate {wr:.1f} "
                  f"--avg-win {aw:.2f} --avg-loss {al:.2f} \\\n"
                  f"                            --trades 200 --observed-trades {tw}")

    if a.json and results:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"\n  wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
