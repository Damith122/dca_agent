#!/usr/bin/env python3
"""Offline analysis of feature-recorder shards.

Usage:  python3 analyze_feature_log.py <directory-of-shards> [--fee-bps 7.32]

Answers one question: does fading a prior move produce a net edge after fees?
It is written to be hard to fool, because the earlier versions of this analysis
fooled us twice - once with a 4-trade sample, once with overlapping windows.

Three corrections do all the work, and any analysis of this dataset that skips
them will report an edge that is not there:

  1. OVERLAP. Samples are 10s apart but a 3600s forward return spans 360 of
     them, so 56,411 rows carry ~42 independent observations, not 56,411.
     Everything significant is computed on NON-OVERLAPPING windows.
  2. CROSS-SYMBOL CORRELATION. The four symbols correlate ~0.80 at the hour
     scale, so they are ~1.2 independent series, not 4. They are combined into
     one equal-weight portfolio before any statistics are computed.
  3. SELECTION. Scanning a grid and reporting its best cell reports the
     luckiest cell. Rules are ranked on the first half of the data and scored
     on the second half, which they never saw.
"""
from __future__ import annotations

import glob
import json
import os
import sys

import numpy as np

HORIZONS = [5, 15, 30, 60, 300, 900, 1800, 3600]
LOOKBACKS = [60, 300, 900, 1800, 3600]


def load(directory):
    per = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                per.setdefault(row["sym"], []).append(row)
    out = {}
    for sym, rows in per.items():
        rows.sort(key=lambda r: r["ts"])
        d = {"ts": np.array([r["ts"] for r in rows]),
             "px": np.array([r["px"] for r in rows]),
             "regime": np.array([r.get("regime") for r in rows])}
        for h in HORIZONS:
            if all(f"r{h}" in r for r in rows):
                d[f"r{h}"] = np.array([r[f"r{h}"] for r in rows], dtype=float)
        out[sym] = d
    return out


def backward(d, lookback, tol=20.0):
    """Return over the previous `lookback` seconds; NaN where history is short."""
    ts, px = d["ts"], d["px"]
    target = ts - lookback
    j = np.clip(np.searchsorted(ts, target), 0, len(ts) - 1)
    jm = np.clip(j - 1, 0, len(ts) - 1)
    pick = np.where(np.abs(ts[jm] - target) < np.abs(ts[j] - target), jm, j)
    ok = (np.abs(ts[pick] - target) <= tol) & (ts >= ts[0] + lookback - tol)
    return np.where(ok, (px - px[pick]) / px[pick], np.nan)


def portfolio(D, grid, lookback, horizon):
    """Equal-weight fade P&L across symbols, on a common 10s time grid.

    Correction 2: symbols are combined BEFORE any statistic is computed, so
    their correlation cannot masquerade as extra sample size.
    """
    acc = []
    for d in D.values():
        if f"r{horizon}" not in d:
            return None
        x = -np.sign(backward(d, lookback)) * d[f"r{horizon}"]
        acc.append(np.interp(grid, d["ts"], np.nan_to_num(x, nan=0.0)))
    return np.vstack(acc).mean(axis=0)


def nonoverlap_stats(X, horizon):
    """Correction 1: one observation per forward window, averaged over every
    phase offset so the answer does not depend on where the sampling starts."""
    step = max(1, int(horizon / 10))
    means, sds, ns, wins = [], [], [], []
    for off in range(0, step, max(1, step // 12)):
        v = X[off::step]
        if len(v) < 3:
            continue
        means.append(v.mean()); sds.append(v.std(ddof=1)); ns.append(len(v))
    if not means:
        return None
    mean = float(np.mean(means)) * 1e4
    sd = float(np.mean(sds)) * 1e4
    n = int(np.mean(ns))
    return {"gross": mean, "sd": sd, "n": n, "se": sd / np.sqrt(n)}


def main(argv):
    directory = argv[1] if len(argv) > 1 else "."
    fee = 7.32
    if "--fee-bps" in argv:
        fee = float(argv[argv.index("--fee-bps") + 1])

    D = load(directory)
    if not D:
        print(f"no shards found in {directory}")
        return 1
    print(f"=== dataset: {directory} ===")
    total = 0
    for sym in sorted(D):
        ts = D[sym]["ts"]
        total += len(ts)
        print(f"  {sym:9s} n={len(ts):6d}  span={(ts[-1] - ts[0]) / 3600:6.2f}h")
    print(f"  {total} rows, round-trip fee assumed {fee:.2f} bps\n")

    syms = sorted(D)
    t0 = max(D[s]["ts"][0] for s in syms)
    t1 = min(D[s]["ts"][-1] for s in syms)
    grid = np.arange(t0, t1, 10.0)
    half = len(grid) // 2

    # Correction 2, made visible: how independent are the symbols really?
    h = 3600 if all(f"r3600" in d for d in D.values()) else max(
        hh for hh in HORIZONS if all(f"r{hh}" in d for d in D.values()))
    M = np.vstack([np.interp(grid, D[s]["ts"], D[s][f"r{h}"]) for s in syms])
    C = np.corrcoef(M)
    rho = (C.sum() - len(syms)) / (len(syms) * (len(syms) - 1))
    eff = len(syms) / (1 + (len(syms) - 1) * rho)
    print(f"cross-symbol correlation at r{h}: {rho:+.2f}  ->  {len(syms)} symbols "
          f"behave like {eff:.2f} independent series\n")

    print("=== fade grid, gross bps per round trip (selection-biased, do not trade) ===")
    horizons = [hh for hh in [60, 300, 900, 1800, 3600]
                if all(f"r{hh}" in d for d in D.values())]
    print(f"{'':9s}" + "".join(f"  H={hh:<5d}" for hh in horizons))
    for L in LOOKBACKS:
        line = f"bwd{L:<6d}"
        for hh in horizons:
            X = portfolio(D, grid, L, hh)
            line += f"  {X.mean() * 1e4:+7.2f}"
        print(line)

    print("\n=== the honest test: non-overlapping windows ===")
    print(f"{'signal':>17s} {'n':>4s} {'gross':>8s} {'net':>8s} {'SE':>7s} {'t vs fee':>9s}")
    verdicts = []
    for L in LOOKBACKS:
        for hh in horizons:
            X = portfolio(D, grid, L, hh)
            st = nonoverlap_stats(X, hh)
            if st is None:
                continue
            t = (st["gross"] - fee) / st["se"]
            verdicts.append((t, L, hh, st))
    for t, L, hh, st in sorted(verdicts, reverse=True)[:6]:
        print(f"  bwd{L}->r{hh:<5d} {st['n']:4d} {st['gross']:+8.2f} "
              f"{st['gross'] - fee:+8.2f} {st['se']:7.2f} {t:+9.2f}")
    best_t = verdicts and max(v[0] for v in verdicts) or 0.0
    print(f"\n  best t-statistic against the fee: {best_t:+.2f}   "
          f"(needs > +2.0 to be worth trading, and > +3.0 after correcting for "
          f"the {len(verdicts)} cells searched)")

    print("\n=== selection test: rank on the first half, score on the second ===")
    scored = []
    for L in LOOKBACKS:
        for hh in horizons:
            X = portfolio(D, grid, L, hh)
            scored.append(((L, hh), X[:half].mean() * 1e4 - fee,
                           X[half:].mean() * 1e4 - fee))
    scored.sort(key=lambda e: -e[1])
    for (L, hh), ins, oos in scored[:5]:
        print(f"  bwd{L}->r{hh:<5d}  in-sample net {ins:+7.2f}   out-of-sample net {oos:+7.2f}")
    pos = sum(1 for _, _, oos in scored if oos > 0)
    print(f"\n  {pos}/{len(scored)} rules are net-positive out of sample "
          f"({pos / len(scored) * 100:.0f}%). A coin flip gives ~50%; "
          f"well under 50% means the fee beats the whole rule space.")

    print("\n=== concentration: how much rides on the best few hours? ===")
    X = portfolio(D, grid, 1800, horizons[-1])
    v = X[::max(1, int(horizons[-1] / 10))]
    srt = np.sort(v)[::-1]
    if v.sum() != 0:
        for k in (1, 3):
            if len(srt) > k:
                print(f"  best {k} of {len(v)} windows = {srt[:k].sum() / v.sum() * 100:5.0f}% "
                      f"of the total; without them the mean is "
                      f"{srt[k:].mean() * 1e4:+6.2f} gross / "
                      f"{srt[k:].mean() * 1e4 - fee:+6.2f} net")

    print("\n=== how much gross edge would clear the fee with 95% confidence? ===")
    st = nonoverlap_stats(portfolio(D, grid, 1800, horizons[-1]), horizons[-1])
    for days in (2, 7, 30, 90):
        hours = days * 24
        need = fee + 1.96 * st["sd"] / np.sqrt(hours)
        print(f"  with {days:3d} days ({hours:5d} hours): gross must exceed {need:6.2f} bps"
              f"   (observed {st['gross']:+.2f})")
    print("\n  If the observed gross is below the 90-day figure, more recording will")
    print("  NOT rescue the strategy - the effect itself is too small for the fee,")
    print("  and only a lower fee or a genuinely different signal changes that.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
