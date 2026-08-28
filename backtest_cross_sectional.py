#!/usr/bin/env python3
"""Walk-forward backtest of a cross-sectional long/short book.

    python3 backtest_cross_sectional.py --csv data/1h --interval 1h \
            --universe-file universe.txt --rebalance 24

Reports, in order of how much you should trust them:

  IC          the rank correlation between signal and forward return, with
              a t-statistic. This is the forecast itself, measured directly.
              A book can look profitable for months on a dead signal; the IC
              t-stat cannot.
  NET SHARPE  after turnover costs, dollar-neutral.
  NULL        the same pipeline with the cross-sectional ranks SHUFFLED each
              period, which destroys the signal while preserving every other
              property of the data. Whatever that earns is what the machinery
              extracts from nothing.

The headline warning this design exists to enforce: breadth multiplies
whatever edge you have, INCLUDING an edge of zero, and it multiplies costs
without any qualification at all. A cross-sectional book on a dead signal
loses money faster than a directional one, in more places at once.
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


def load_matrix(directory: str, symbols: List[str]):
    """(timestamps, price matrix, kept symbols) on a common clock."""
    series = {}
    for s in symbols:
        path = os.path.join(directory, f"{s}.csv")
        if not os.path.exists(path):
            continue
        ts, px = [], []
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.reader(fh):
                if not r or not r[0].lstrip("-").replace(".", "", 1).isdigit():
                    continue
                t = float(r[0])
                if t > 1e11:
                    t /= 1000.0
                ts.append(int(round(t)))
                px.append(float(r[4]))
        if len(ts) > 100:
            series[s] = dict(zip(ts, px))
    if not series:
        return np.array([]), np.zeros((0, 0)), []
    # Keep timestamps present for MOST names rather than all: demanding a
    # perfect intersection across a hundred listings throws the panel away
    # because of one late-listed symbol.
    counts: Dict[int, int] = {}
    for d in series.values():
        for t in d:
            counts[t] = counts.get(t, 0) + 1
    need = max(2, int(len(series) * 0.9))
    grid = sorted(t for t, c in counts.items() if c >= need)
    if len(grid) < 200:
        return np.array([]), np.zeros((0, 0)), []
    keep = [s for s, d in series.items()
            if sum(1 for t in grid if t in d) >= 0.95 * len(grid)]
    mat = np.full((len(grid), len(keep)), np.nan)
    for j, s in enumerate(keep):
        d = series[s]
        for i, t in enumerate(grid):
            if t in d:
                mat[i, j] = d[t]
    # Forward-fill small holes only; a long hole means the name was not
    # trading and must stay NaN so it is excluded from the book.
    for j in range(mat.shape[1]):
        col = mat[:, j]
        last, gap = np.nan, 0
        for i in range(len(col)):
            if np.isfinite(col[i]):
                last, gap = col[i], 0
            elif np.isfinite(last) and gap < 3:
                col[i] = last
                gap += 1
    return np.array(grid, dtype=float), mat, keep


def run_book(px: np.ndarray, signal_fn, p: CS.BookParams, rebalance: int,
             cost_bps: float, shuffle_rng: Optional[np.random.Generator] = None):
    """Walk the panel, rebalancing every `rebalance` bars.

    The signal at bar i is formed from prices up to i and the book is held
    over (i, i+rebalance], so the return it earns is strictly in the future.
    """
    n_t = px.shape[0]
    w_prev: Optional[np.ndarray] = None
    rets, tos, ics = [], [], []
    for i in range(0, n_t - rebalance, rebalance):
        sig = signal_fn(px, i)
        if shuffle_rng is not None:
            # Destroy the cross-sectional ordering, keep everything else -
            # the same names, the same period, the same forward returns.
            ok = np.isfinite(sig)
            vals = sig[ok].copy()
            shuffle_rng.shuffle(vals)
            sig = sig.copy()
            sig[ok] = vals
        fwd = px[i + rebalance] / px[i] - 1.0
        ics.append(CS.spearman_ic(sig, fwd))
        w_t = CS.target_weights(sig, p)
        w = CS.apply_damping(w_t, w_prev, p)
        to = CS.turnover(w, w_prev)
        gross = float(np.nansum(w * np.nan_to_num(fwd, nan=0.0)))
        rets.append(gross - to * cost_bps / 1e4)
        tos.append(to)
        w_prev = w
    return np.array(rets), np.array(tos), np.array(ics, dtype=float)


def summarise(rets, tos, ics, periods_per_year, cost_bps=0.0):
    if len(rets) == 0:
        return {"periods": 0}
    sd = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    sharpe = (float(rets.mean()) / sd * math.sqrt(periods_per_year)) if sd > 0 else 0.0
    curve = np.concatenate([[0.0], np.cumsum(rets)])
    peak = np.maximum.accumulate(curve)
    good = ics[np.isfinite(ics)]
    ic_t = (float(good.mean()) / (good.std(ddof=1) / math.sqrt(len(good)))
            if len(good) > 1 and good.std(ddof=1) > 0 else 0.0)
    return {
        "periods": len(rets),
        "total_pct": float(curve[-1] * 100),
        "sharpe": sharpe,
        "mean_ic": float(good.mean()) if len(good) else 0.0,
        "ic_t": ic_t,
        "hit_rate": float((rets > 0).mean() * 100),
        "mean_turnover": float(tos.mean()),
        # Turnover is a FRACTION OF GROSS traded, not a cost. Multiplying it
        # by periods per year gave "6010% a year" from a 0.16 turnover book,
        # which is annual turnover expressed as a percentage - a real number
        # wearing the wrong label. The fee has to be in the formula.
        "annual_turnover": float(tos.mean() * periods_per_year),
        "cost_drag_pct": float(tos.mean() * (cost_bps / 1e4)
                               * periods_per_year * 100),
        "max_dd_pct": float((peak - curve).max() * 100),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/1h")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--universe-file", default=None,
                    help="text file, one symbol per line")
    ap.add_argument("--rebalance", type=int, default=24, help="bars between rebalances")
    ap.add_argument("--fee-bps", type=float, default=7.32,
                    help="round trip; use 4.0 for maker-only")
    ap.add_argument("--quantile", type=float, default=0.2)
    ap.add_argument("--damping", type=float, default=0.5)
    ap.add_argument("--null-runs", type=int, default=50)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    if a.universe_file:
        with open(a.universe_file, encoding="utf-8") as fh:
            syms = [l.strip().upper() for l in fh if l.strip()
                    and not l.startswith("#")]
    elif a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        syms = [f.replace(".csv", "") for f in sorted(os.listdir(a.csv))
                if f.endswith(".csv")]

    grid, px, kept = load_matrix(a.csv, syms)
    if px.size == 0 or len(kept) < 5:
        print(f"need at least 5 symbols with aligned history; got {len(kept)}")
        print("Fetch a wider universe: fetch_binance_data.py --universe all")
        return 1

    bars_per_year = {"15m": 4 * 24 * 365, "1h": 24 * 365,
                     "4h": 6 * 365, "1d": 365}.get(a.interval, 24 * 365)
    ppy = bars_per_year / a.rebalance
    logret = np.diff(np.log(px), axis=0)
    breadth = CS.effective_breadth(logret)

    print(f"=== cross-sectional long/short, {a.interval}, "
          f"rebalance every {a.rebalance} bars ===")
    print(f"    {len(kept)} symbols, {len(grid)} aligned bars, "
          f"fee {a.fee_bps:g} bps round trip")
    print(f"    effective breadth {breadth:.1f} of {len(kept)} names "
          f"(correlation discount)")
    if breadth < 5:
        print(f"\n    WARNING: {breadth:.1f} independent names is not a")
        print("    cross-sectional universe. The whole mechanism is breadth,")
        print("    and you do not have any. Widen the universe before reading")
        print("    anything below as a result.\n")

    p = CS.BookParams(quantile=a.quantile, damping=a.damping)
    print(f"\n{'signal':>10s} {'IC':>8s} {'IC t':>7s} {'Sharpe':>8s} "
          f"{'total':>9s} {'turnover':>9s} {'cost/yr':>9s} {'maxDD':>8s}")
    results = {}
    for name, fn in CS.SIGNALS.items():
        r, t, ic = run_book(px, fn, p, a.rebalance, a.fee_bps)
        st = summarise(r, t, ic, ppy, a.fee_bps)
        results[name] = st
        print(f"{name:>10s} {st['mean_ic']:+8.4f} {st['ic_t']:+7.2f} "
              f"{st['sharpe']:+8.2f} {st['total_pct']:+8.1f}% "
              f"{st['mean_turnover']:9.2f} {st['cost_drag_pct']:8.1f}% "
              f"{st['max_dd_pct']:7.1f}%")

    best = max(results, key=lambda k: results[k]["ic_t"])
    bs = results[best]
    print(f"\n=== strongest signal by IC t-statistic: {best} ===")
    print(f"  mean IC        {bs['mean_ic']:+.4f}  (t = {bs['ic_t']:+.2f})")
    print(f"  net Sharpe     {bs['sharpe']:+.2f}")
    print(f"  turnover       {bs['mean_turnover']:.2f} per rebalance "
          f"= {bs['annual_turnover']:.0f}x a year -> "
          f"{bs['cost_drag_pct']:.1f}% a year in fees")

    rng = np.random.default_rng(0)
    null_ic_t, null_sharpe = [], []
    for k in range(a.null_runs):
        r, t, ic = run_book(px, CS.SIGNALS[best], p, a.rebalance, a.fee_bps,
                            shuffle_rng=np.random.default_rng(1000 + k))
        st = summarise(r, t, ic, ppy, a.fee_bps)
        null_ic_t.append(st["ic_t"])
        null_sharpe.append(st["sharpe"])
    nt = np.array(null_ic_t)
    ns = np.array(null_sharpe)
    p_ic = float((nt >= bs["ic_t"]).mean())
    p_sh = float((ns >= bs["sharpe"]).mean())
    print(f"\n=== NULL CONTROL ({a.null_runs} runs with ranks shuffled) ===")
    print(f"  IC t from nothing      median {np.median(nt):+.2f}  "
          f"95th {np.percentile(nt, 95):+.2f}")
    print(f"  Sharpe from nothing    median {np.median(ns):+.2f}  "
          f"95th {np.percentile(ns, 95):+.2f}")
    print(f"  p-value on IC t        {p_ic:.3f}")
    print(f"  p-value on Sharpe      {p_sh:.3f}")

    t_hurdle = 2.0 + math.log(max(1, len(CS.SIGNALS)))
    ok = (bs["ic_t"] >= t_hurdle and p_ic <= 0.05 and bs["sharpe"] > 0
          and breadth >= 5)
    print(f"\n  hurdle 1  IC t >= {t_hurdle:.2f} ({len(CS.SIGNALS)} signals "
          f"searched): {'PASS' if bs['ic_t'] >= t_hurdle else 'FAIL'}")
    print(f"  hurdle 2  p <= 0.05 vs shuffled ranks: "
          f"{'PASS' if p_ic <= 0.05 else 'FAIL'}")
    print(f"  hurdle 3  net Sharpe > 0 after costs: "
          f"{'PASS' if bs['sharpe'] > 0 else 'FAIL'}")
    print(f"  hurdle 4  effective breadth >= 5: "
          f"{'PASS' if breadth >= 5 else 'FAIL'} at {breadth:.1f}")
    print("\n  " + ("TRADEABLE - all four hurdles clear." if ok else
                    "NOT TRADEABLE. All four hurdles must clear."))
    if not ok and bs["cost_drag_pct"] > 30:
        print(f"  Note: costs alone are {bs['cost_drag_pct']:.0f}% a year. Raise "
              f"--rebalance or --damping\n  before concluding the signal is dead - "
              "turnover may be hiding it.")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"results": results, "best": best, "p_ic": p_ic,
                       "breadth": breadth}, fh, indent=2)
        print(f"\n  wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
