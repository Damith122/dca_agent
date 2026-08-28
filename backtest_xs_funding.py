#!/usr/bin/env python3
"""Cross-sectional funding carry: rank perps by their own funding rate.

    python3 backtest_xs_funding.py --funding data/funding --csv data/1h \
                                   --hold 3 --null-runs 60

What this trades
----------------
Nothing in this book is a price forecast. A perpetual's funding rate is
published by the exchange before you commit, and the SHORT side receives it
when the rate is positive. So the book shorts the names paying the most and
longs the names paying the least, holds, and collects the difference.

Per period the carry is

    funding_pnl = -sum(w_i * f_i)

with w negative on a short. That is an observable cash flow, not an
estimate. It is the one term in this entire project that does not require
an information coefficient - which is why it is worth testing after seven
price-forecasting families measured an IC of roughly zero.

The book is dollar-neutral, so the price component is residual rather than
market beta, and it runs across the whole perpetual universe rather than
four correlated majors - combining the only two things that worked.

What can still kill it
----------------------
1. FUNDING MEAN-REVERTS AGAINST YOU. High funding usually means crowded
   longs, and crowded longs unwind. Shorting the payer collects the rate and
   eats the unwind. If the price loss exceeds the carry, the trade is a
   short-volatility position wearing a carry costume - which is why the
   output separates carry P&L from price P&L rather than reporting a total.
2. THE HIGH-FUNDING NAMES ARE THE ILLIQUID ONES. Extreme funding
   concentrates in small, wide-spread listings where the real cost is far
   above the headline fee. --extra-cost-bps exists to price that honestly.
3. TURNOVER. Funding ranks churn, and every reshuffle pays the fee twice.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from typing import Dict, List, Optional

import numpy as np

import cross_sectional as CS


def load_funding(directory: str) -> Dict[str, Dict[int, float]]:
    out = {}
    for f in sorted(os.listdir(directory)):
        if not f.endswith(".csv"):
            continue
        sym = f[:-4]
        d = {}
        with open(os.path.join(directory, f), newline="", encoding="utf-8") as fh:
            for r in csv.reader(fh):
                if not r or not r[0].lstrip("-").replace(".", "", 1).isdigit():
                    continue
                t = float(r[0])
                if t > 1e11:
                    t /= 1000.0
                d[int(round(t))] = float(r[1])
        if len(d) > 50:
            out[sym] = d
    return out


def build_panel(funding: Dict[str, Dict[int, float]], min_cover: float = 0.9):
    """(times, funding matrix in bps, symbols) on the common 8h grid."""
    counts: Dict[int, int] = {}
    for d in funding.values():
        for t in d:
            counts[t] = counts.get(t, 0) + 1
    need = max(2, int(len(funding) * min_cover))
    grid = sorted(t for t, c in counts.items() if c >= need)
    if len(grid) < 60:
        return np.array([]), np.zeros((0, 0)), []
    syms = [s for s, d in funding.items()
            if sum(1 for t in grid if t in d) >= 0.95 * len(grid)]
    mat = np.full((len(grid), len(syms)), np.nan)
    for j, s in enumerate(syms):
        d = funding[s]
        for i, t in enumerate(grid):
            if t in d:
                mat[i, j] = d[t]
    return np.array(grid, dtype=float), mat, syms


def load_prices(directory: str, symbols: List[str], times: np.ndarray):
    """Price matrix aligned to the funding grid; NaN where unavailable."""
    mat = np.full((len(times), len(symbols)), np.nan)
    want = np.round(times).astype(np.int64)
    for j, s in enumerate(symbols):
        path = os.path.join(directory, f"{s}.csv")
        if not os.path.exists(path):
            continue
        rows = {}
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.reader(fh):
                if not r or not r[0].lstrip("-").replace(".", "", 1).isdigit():
                    continue
                t = float(r[0])
                if t > 1e11:
                    t /= 1000.0
                rows[int(round(t))] = float(r[4])
        if not rows:
            continue
        keys = np.array(sorted(rows))
        vals = np.array([rows[k] for k in keys])
        idx = np.searchsorted(keys, want, side="right") - 1
        ok = (idx >= 0) & (np.abs(keys[np.clip(idx, 0, len(keys) - 1)] - want)
                           <= 3600 * 2)
        mat[ok, j] = vals[np.clip(idx, 0, len(keys) - 1)][ok]
    return mat


def run(fund: np.ndarray, px: Optional[np.ndarray], p: CS.BookParams,
        hold: int, cost_bps: float, lookback: int = 3,
        shuffle_rng: Optional[np.random.Generator] = None):
    """Walk the funding grid. Returns (carry, price, net, turnover) per period.

    The signal at period i uses funding prints up to and INCLUDING i-1. The
    print at i settles at i and is what the position earns, so using it to
    decide the position would be reading the answer.
    """
    n_t = fund.shape[0]
    w_prev = None
    carry, price, net, tos = [], [], [], []
    for i in range(lookback, n_t - hold, hold):
        hist = fund[i - lookback:i]
        sig = -np.nanmedian(hist, axis=0)      # long the LOW payers
        if shuffle_rng is not None:
            ok = np.isfinite(sig)
            v = sig[ok].copy()
            shuffle_rng.shuffle(v)
            sig = sig.copy()
            sig[ok] = v
        w = CS.apply_damping(CS.target_weights(sig, p), w_prev, p)
        # Carry: a short (w<0) RECEIVES a positive rate, hence the minus.
        f_win = np.nan_to_num(fund[i:i + hold], nan=0.0)
        c = float(-np.nansum(w * f_win.sum(axis=0)) / 1e4)
        if px is not None:
            fwd = px[i + hold] / px[i] - 1.0
            pr = float(np.nansum(w * np.nan_to_num(fwd, nan=0.0)))
        else:
            pr = 0.0
        to = CS.turnover(w, w_prev)
        carry.append(c)
        price.append(pr)
        net.append(c + pr - to * cost_bps / 1e4)
        tos.append(to)
        w_prev = w
    return (np.array(carry), np.array(price), np.array(net), np.array(tos))


def stats(carry, price, net, tos, periods_per_year, cost_bps=0.0):
    if len(net) == 0:
        return {"periods": 0}
    sd = float(net.std(ddof=1)) if len(net) > 1 else 0.0
    curve = np.concatenate([[0.0], np.cumsum(net)])
    peak = np.maximum.accumulate(curve)
    t = (float(net.mean()) / (net.std(ddof=1) / math.sqrt(len(net)))
         if len(net) > 1 and net.std(ddof=1) > 0 else 0.0)
    return {
        "periods": len(net),
        "carry_bps": float(carry.mean() * 1e4),
        "price_bps": float(price.mean() * 1e4),
        "net_bps": float(net.mean() * 1e4),
        "t_stat": t,
        "sharpe": (float(net.mean()) / sd * math.sqrt(periods_per_year)
                   if sd > 0 else 0.0),
        "total_pct": float(curve[-1] * 100),
        "max_dd_pct": float((peak - curve).max() * 100),
        "hit_rate": float((net > 0).mean() * 100),
        "turnover": float(tos.mean()),
        "cost_bps": float(tos.mean() * cost_bps),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--funding", default="data/funding")
    ap.add_argument("--csv", default="data/1h", help="price CSVs; omit with --no-price")
    ap.add_argument("--no-price", action="store_true",
                    help="carry only - shows the cash flow with no price risk")
    ap.add_argument("--hold", type=int, default=3, help="funding periods (8h each)")
    ap.add_argument("--lookback", type=int, default=3)
    ap.add_argument("--quantile", type=float, default=0.2)
    ap.add_argument("--damping", type=float, default=0.5)
    ap.add_argument("--fee-bps", type=float, default=7.32)
    ap.add_argument("--extra-cost-bps", type=float, default=0.0,
                    help="spread/slippage on top of the fee - the high-funding "
                         "names are usually the illiquid ones")
    ap.add_argument("--null-runs", type=int, default=60)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    funding = load_funding(a.funding)
    if len(funding) < 10:
        print(f"only {len(funding)} symbols with funding history in {a.funding}")
        print("run fetch_funding_universe.py first")
        return 1
    times, fund, syms = build_panel(funding)
    if fund.size == 0:
        print("could not build a common funding grid - too little overlap")
        return 1

    px = None if a.no_price else load_prices(a.csv, syms, times)
    if px is not None and not np.isfinite(px).any():
        print(f"no usable prices in {a.csv}; falling back to carry only")
        px = None

    cost = a.fee_bps + a.extra_cost_bps
    ppy = 3 * 365 / a.hold
    days = (times[-1] - times[0]) / 86400
    print(f"=== cross-sectional funding carry ===")
    print(f"    {len(syms)} symbols, {len(times)} funding prints, "
          f"{days:.0f} days")
    print(f"    hold {a.hold} periods ({a.hold * 8}h), quantile "
          f"{a.quantile:g}, cost {cost:.2f} bps round trip")
    if px is None:
        print("    CARRY ONLY - price risk excluded, so this is an upper bound")

    disp = np.nanpercentile(fund, [10, 50, 90], axis=1)
    print(f"\n--- funding dispersion across the universe ---")
    print(f"  10th pct {np.nanmean(disp[0]):+.3f} bps/8h   "
          f"median {np.nanmean(disp[1]):+.3f}   "
          f"90th pct {np.nanmean(disp[2]):+.3f}")
    spread = float(np.nanmean(disp[2] - disp[0]))
    print(f"  decile spread {spread:.3f} bps/8h "
          f"= {spread * 3 * 365 / 100:.1f}% a year of gross carry")
    print(f"  a round trip costs {cost:.2f} bps, so a full rotation needs "
          f"{cost / max(spread, 1e-9):.1f} periods of spread to pay for itself")

    p = CS.BookParams(quantile=a.quantile, damping=a.damping)
    carry, price, net, tos = run(fund, px, p, a.hold, cost, a.lookback)
    st = stats(carry, price, net, tos, ppy, cost)
    if not st["periods"]:
        print("\nno periods evaluated")
        return 1

    print(f"\n=== decomposition (per rebalance, bps of gross) ===")
    print(f"  carry collected   {st['carry_bps']:+8.2f}")
    print(f"  price movement    {st['price_bps']:+8.2f}"
          + ("   <- the risk you take to collect it" if px is not None else ""))
    print(f"  cost              {-st['cost_bps']:+8.2f}")
    print(f"  {'-' * 30}")
    print(f"  net               {st['net_bps']:+8.2f}")
    if px is not None and st["price_bps"] < -abs(st["carry_bps"]) * 0.5:
        print("\n  NOTE: price is eating most of the carry. High funding means")
        print("  crowded longs, and crowded longs unwind - shorting the payer")
        print("  collects the rate and takes the unwind. That is a short-")
        print("  volatility position, not a carry trade.")

    print(f"\n=== performance ===")
    print(f"  periods {st['periods']}   hit rate {st['hit_rate']:.0f}%   "
          f"turnover {st['turnover']:.2f}/rebalance")
    print(f"  net Sharpe {st['sharpe']:+.2f}   t-stat {st['t_stat']:+.2f}   "
          f"total {st['total_pct']:+.1f}%   maxDD {st['max_dd_pct']:.1f}%")

    nt, ns = [], []
    for k in range(a.null_runs):
        c2, p2, n2, t2 = run(fund, px, p, a.hold, cost, a.lookback,
                             shuffle_rng=np.random.default_rng(9000 + k))
        s2 = stats(c2, p2, n2, t2, ppy, cost)
        nt.append(s2["t_stat"])
        ns.append(s2["sharpe"])
    nt, ns = np.array(nt), np.array(ns)
    p_t = float((nt >= st["t_stat"]).mean())
    print(f"\n=== NULL CONTROL ({a.null_runs} runs, funding ranks shuffled) ===")
    print(f"  t from shuffled ranks   median {np.median(nt):+.2f}   "
          f"95th {np.percentile(nt, 95):+.2f}")
    print(f"  Sharpe from shuffled    median {np.median(ns):+.2f}   "
          f"95th {np.percentile(ns, 95):+.2f}")
    print(f"  p-value {p_t:.3f}")

    ok = st["t_stat"] >= 2.0 and p_t <= 0.05 and st["net_bps"] > 0
    print(f"\n  hurdle 1  t >= 2.00: "
          f"{'PASS' if st['t_stat'] >= 2.0 else 'FAIL'} at {st['t_stat']:+.2f}")
    print(f"  hurdle 2  p <= 0.05: {'PASS' if p_t <= 0.05 else 'FAIL'} at {p_t:.3f}")
    print(f"  hurdle 3  net > 0:   "
          f"{'PASS' if st['net_bps'] > 0 else 'FAIL'} at {st['net_bps']:+.2f} bps")
    print("\n  " + ("PROMISING - now re-run with --extra-cost-bps 10 to price the "
                    "spread\n  on the illiquid names, and with --no-price off if "
                    "you used it."
                    if ok else "NOT TRADEABLE at these settings."))
    if ok and px is None:
        print("\n  This was CARRY ONLY. It ignores the price risk of holding the")
        print("  book, which is the entire question. Re-run without --no-price.")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"stats": st, "p_value": p_t, "symbols": len(syms),
                       "decile_spread_bps": spread}, fh, indent=2)
        print(f"\n  wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
