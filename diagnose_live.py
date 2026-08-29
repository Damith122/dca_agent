#!/usr/bin/env python3
"""Health check on a live feature-recorder dataset.

    python3 diagnose_live.py <shard-directory>

This is not a strategy search. It asks whether the machine producing the
data is working, which has to be answered first: a dead feature or a
saturated model head makes every downstream result meaningless, and neither
shows up as an error in the logs.

Four questions, in the order they matter:

  1. FEATURES. Is any input constant, near-constant, or stuck? A feature the
     model can never learn from is worse than a missing one, because it
     occupies a slot and dilutes the rest.
  2. HEADS. Do the brain's own probabilities vary at all, and do they land
     where they claim? A head that reports READY while emitting one value is
     a broken component reporting healthy.
  3. CALIBRATION. When a head says 20%, does it happen 20% of the time? This
     is the only test that distinguishes a model from a random number with a
     confident interface.
  4. DRIFT. Are the features the bot sees now the same distribution it saw
     at the start? If not, anything fitted on the early window is already
     stale.
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict

import numpy as np

HORIZONS = [5, 15, 30, 60, 300, 900, 1800, 3600]
# Probability heads are bounded [0,1] and a median at either end means
# saturation. quality is a REGRESSION over reward and risk/conf are scores -
# a zero-centred regression sitting near 0 is healthy, not saturated, so
# applying a probability test to them produces a false alarm.
PROB_HEADS = {"success_p", "tp_hit_p", "noise_p", "trend_conf"}
HEADS = ["success_p", "tp_hit_p", "noise_p", "quality", "conf", "risk",
         "trend_conf"]


def load(directory):
    rows = []
    for path in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    rows.sort(key=lambda r: r["ts"])
    return rows


def rule(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def feature_health(rows):
    rule("1. FEATURE HEALTH  -  is every input actually varying?")
    F = np.array([r["f"] for r in rows], dtype=float)
    n, k = F.shape
    print(f"  {n} rows x {k} features\n")
    print(f"  {'#':>3s} {'distinct':>9s} {'sd':>12s} {'min':>12s} "
          f"{'max':>12s}  verdict")
    dead, weak = [], []
    for j in range(k):
        col = F[:, j]
        good = col[np.isfinite(col)]
        if len(good) == 0:
            dead.append(j)
            print(f"  {j:3d} {'0':>9s} {'-':>12s} {'-':>12s} {'-':>12s}  "
                  f"ALL NaN")
            continue
        d = len(np.unique(np.round(good, 10)))
        sd = float(good.std())
        # A feature with two distinct values is a flag, which is fine. One is
        # a constant, which cannot carry information at all.
        if d <= 1:
            v, dead[len(dead):] = "CONSTANT - carries no information", [j]
        elif d <= 5 and sd == 0:
            v, weak[len(weak):] = "degenerate", [j]
        elif sd < 1e-9:
            v, weak[len(weak):] = "near-constant", [j]
        else:
            v = ""
        if v:
            print(f"  {j:3d} {d:9d} {sd:12.3e} {good.min():12.4f} "
                  f"{good.max():12.4f}  {v}")
    live = k - len(dead) - len(weak)
    print(f"\n  {live}/{k} features carry usable variation; "
          f"{len(dead)} constant, {len(weak)} degenerate")
    if dead:
        print(f"  dead feature indices: {dead}")
        print("  Each of these occupies a model input and can only dilute the")
        print("  others. Remove them or fix whatever should be filling them.")
    return dead, weak


def head_health(rows):
    rule("2. MODEL HEADS  -  do the brain's own outputs vary?")
    print(f"  {'head':>11s} {'distinct':>9s} {'min':>12s} {'median':>12s} "
          f"{'max':>12s}  verdict")
    broken = []
    for h in HEADS:
        v = np.array([r[h] for r in rows if r.get(h) is not None], dtype=float)
        v = v[np.isfinite(v)]
        if len(v) == 0:
            print(f"  {h:>11s} {'0':>9s}  never recorded")
            continue
        d = len(np.unique(np.round(v, 8)))
        if d == 1:
            verdict = "CONSTANT - this head is not learning"
            broken.append(h)
        elif h in PROB_HEADS and (np.median(v) > 0.98 or np.median(v) < 0.02):
            verdict = "SATURATED - pinned at one end of its range"
            broken.append(h)
        elif h not in PROB_HEADS:
            verdict = "(regression/score - range check only)"
        else:
            verdict = ""
        print(f"  {h:>11s} {d:9d} {v.min():12.6f} {np.median(v):12.6f} "
              f"{v.max():12.6f}  {verdict}")

    # A head reporting READY while emitting a constant is the worst case:
    # a broken component that the bot believes it can trust.
    ready = Counter()
    for r in rows:
        for k, s in (r.get("rdy") or {}).items():
            if isinstance(s, str):
                ready[(k, s)] += 1
    print("\n  readiness the bot reports to itself:")
    for (k, s), c in sorted(ready.items()):
        flag = ""
        if s == "READY" and f"{k}_p" in broken:
            flag = "  <-- claims READY but emits a constant or saturated value"
        if s == "READY" and k == "trend" and "trend_conf" in broken:
            flag = "  <-- claims READY but trend_conf never varies"
        print(f"    {k:9s} {s:12s} {c:7d}{flag}")
    return broken


def calibration(rows):
    rule("3. CALIBRATION  -  when a head says X%, does X% happen?")
    # tp_hit_p predicts whether |move| exceeds an adaptive threshold at the
    # LABEL horizon - about 2.5 seconds. An earlier version of this function
    # compared it against "did MFE beat MAE over the whole recorder window",
    # which is a different event over a horizon 1400x longer, and produced a
    # damning gap column that criticised the head for something it never
    # claimed. r5 is the closest recorded horizon to the label's own, so the
    # realised outcome is reconstructed from that.
    p = np.array([r.get("tp_hit_p", np.nan) for r in rows], dtype=float)
    r5 = np.array([r.get("r5", np.nan) for r in rows], dtype=float)
    ok = np.isfinite(p) & np.isfinite(r5)
    if ok.sum() < 500:
        print("  not enough rows carrying both a prediction and an r5 outcome")
        return
    p, moves = p[ok], np.abs(r5[ok])

    # Reconstruct the adaptive threshold the bot itself would have used: an
    # EWMA of |move| at this horizon, times the configured multiplier.
    scale, hits = float(np.median(moves)), []
    for v in moves:
        scale = 0.999 * scale + 0.001 * max(v, 1e-9)
        hits.append(v >= 1.2 * scale)
    hit = np.array(hits, dtype=float)
    warm = 500
    p, hit = p[warm:], hit[warm:]
    print(f"  {len(p)} rows, outcome = |r5| above the adaptive threshold")
    print(f"  realised base rate: {hit.mean() * 100:.1f}%")
    print(f"  mean prediction:    {p.mean() * 100:.1f}%\n")
    print(f"  {'predicted tp_hit_p':>22s} {'n':>7s} {'predicted':>10s} "
          f"{'actual':>9s}  gap")
    edges = [0, 0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 1.01]
    for a, b in zip(edges, edges[1:]):
        m = (p >= a) & (p < b)
        if m.sum() < 30:
            continue
        print(f"  [{a:8.2f},{b:8.2f}) {m.sum():7d} {p[m].mean() * 100:9.1f}% "
              f"{hit[m].mean() * 100:8.1f}%  {(hit[m].mean() - p[m].mean()) * 100:+7.1f} pts")
    corr = float(np.corrcoef(p, hit)[0, 1]) if p.std() > 0 else float("nan")
    n = len(p)
    # A 5s forward window at 10s sampling does NOT overlap the next row's, so
    # unlike the longer horizons these observations are already independent
    # and the naive t is honest here. That is only true because r5 is shorter
    # than the sampling interval - the r300 and r3600 sections below have to
    # subsample, and do.
    t = corr * math.sqrt(max(1, n - 2)) / math.sqrt(max(1e-12, 1 - corr * corr))
    print(f"\n  correlation between prediction and outcome: {corr:+.4f}  "
          f"(t {t:+.2f} on {len(idx)} samples)")
    if abs(corr) < 0.03:
        print("  The head now VARIES but its variation does not track the")
        print("  outcome. Fixing the label made it learnable; it did not make")
        print("  it predictive. Those are different problems and only the")
        print("  first one has been solved.")
    elif corr > 0:
        print("  The prediction carries real information about its own label.")


def feature_ic(rows):
    rule("4. WHICH FEATURES PREDICT ANYTHING?")
    F = np.array([r["f"] for r in rows], dtype=float)
    for h in (300, 3600):
        y = np.array([r.get(f"r{h}", np.nan) for r in rows], dtype=float)
        ok = np.isfinite(y)
        if ok.sum() < 500:
            continue
        # Non-overlapping sample: with 10s spacing, r3600 spans 360 rows, so
        # adjacent rows share almost all of their outcome. Correlating the
        # full series would overstate significance by more than an order of
        # magnitude.
        step = max(1, h // 10)
        idx = np.where(ok)[0][::step]
        n = len(idx)
        if n < 30:
            continue
        yy = y[idx]
        print(f"\n  horizon r{h}  ({n} non-overlapping observations, "
              f"|t| > {1.96:.2f} to matter)")
        scored = []
        for j in range(F.shape[1]):
            x = F[idx, j]
            m = np.isfinite(x) & np.isfinite(yy)
            if m.sum() < 30 or x[m].std() == 0:
                continue
            c = float(np.corrcoef(x[m], yy[m])[0, 1])
            if not math.isfinite(c):
                continue
            t = c * math.sqrt(max(1, m.sum() - 2)) / math.sqrt(max(1e-12, 1 - c * c))
            scored.append((abs(t), j, c, t))
        scored.sort(reverse=True)
        if not scored:
            print("    no feature had enough variation to test")
            continue
        for _, j, c, t in scored[:5]:
            mark = "  <-- survives" if abs(t) > 1.96 else ""
            print(f"    feature {j:2d}  corr {c:+.4f}  t {t:+6.2f}{mark}")
        surv = sum(1 for a, _, _, _ in scored if a > 1.96)
        exp = 0.05 * len(scored)
        print(f"    {surv} of {len(scored)} features exceed |t|=1.96; "
              f"chance alone would give about {exp:.1f}")


def drift(rows):
    rule("5. DRIFT  -  is the bot still seeing what it saw at the start?")
    F = np.array([r["f"] for r in rows], dtype=float)
    half = len(F) // 2
    print(f"  comparing the first {half} rows against the last {len(F) - half}\n")
    moved = []
    for j in range(F.shape[1]):
        a, b = F[:half, j], F[half:, j]
        a, b = a[np.isfinite(a)], b[np.isfinite(b)]
        if len(a) < 100 or len(b) < 100 or a.std() == 0:
            continue
        # Standardised mean shift - how far the centre moved in units of the
        # early window's own spread.
        d = abs(b.mean() - a.mean()) / a.std()
        if d > 0.5:
            moved.append((d, j, a.mean(), b.mean()))
    moved.sort(reverse=True)
    if not moved:
        print("  no feature centre moved by more than half a standard deviation.")
        print("  The input distribution is stable over this window.")
    else:
        print(f"  {'#':>3s} {'shift (sd)':>11s} {'early mean':>13s} "
              f"{'late mean':>13s}")
        for d, j, am, bm in moved[:8]:
            print(f"  {j:3d} {d:11.2f} {am:13.5f} {bm:13.5f}")
        print(f"\n  {len(moved)} feature(s) drifted more than 0.5 sd. Anything")
        print("  fitted on the early window is describing a different market.")


def main(argv=None):
    directory = argv[1] if argv and len(argv) > 1 else "."
    rows = load(directory)
    if len(rows) < 1000:
        print(f"only {len(rows)} rows found in {directory}")
        return 1
    syms = Counter(r["sym"] for r in rows)
    span = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    print(f"=== live diagnostic: {len(rows)} rows, {len(syms)} symbols, "
          f"{span:.1f}h ===")
    print("    " + ", ".join(f"{s} {c}" for s, c in sorted(syms.items())))

    feature_health(rows)
    head_health(rows)
    calibration(rows)
    # Per-symbol, because pooling four correlated symbols would inflate every
    # count without adding independent information.
    one = max(syms, key=lambda s: syms[s])
    sub = [r for r in rows if r["sym"] == one]
    print(f"\n  (features and drift assessed on {one} alone - pooling four "
          f"correlated\n   symbols inflates every count without adding "
          f"independent information)")
    feature_ic(sub)
    drift(sub)

    rule("WHAT TO DO WITH THIS")
    print("  A constant feature or a saturated head invalidates every result")
    print("  computed downstream of it, including all seven strategy families")
    print("  tested so far. Fix those before running another backtest - a")
    print("  search over broken inputs cannot find anything but noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
