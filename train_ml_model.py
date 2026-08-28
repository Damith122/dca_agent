#!/usr/bin/env python3
"""Walk-forward XGBoost classifier over OHLCV, evaluated net of fees.

    python3 train_ml_model.py --csv data/1h --interval 1h \
                              --symbols SOLUSDT,BTCUSDT,ETHUSDT,NEARUSDT

What it predicts
----------------
Not "will price go up" - that is not tradeable. It predicts which barrier a
trade would hit first: a triple-barrier label. From bar i+1's open, does
price reach +k*ATR before -k*ATR within `horizon` bars? The upper barrier is
forced to exceed the round-trip fee, so a correct prediction is a profitable
trade by construction rather than by hope.

The four things that make ML backtests lie, and what is done about each
---------------------------------------------------------------------
1. LOOKAHEAD IN FEATURES. Everything in ml_features.py uses bars up to i and
   the trade opens at i+1's open.

2. LABEL LEAKAGE ACROSS THE SPLIT. The label for bar i is only known at bar
   i+horizon. Training on bars whose outcome overlaps the test window leaks
   the future backwards. Those bars are PURGED, and an EMBARGO drops a
   further band after the test window before it rejoins training. Skip this
   and a worthless model posts a beautiful equity curve - it is the single
   most common way this goes wrong.

3. THRESHOLD FITTED ON TEST. The probability cutoff is chosen on a
   validation slice held out from the END of each training window, never on
   the test fold.

4. NO NULL BASELINE. --null-runs repeats the entire pipeline with the labels
   shuffled inside each training fold. That measures what this much model
   capacity scores on data with no signal. If the real result is not clearly
   outside that distribution, it is noise wearing a confidence interval.

Every reported figure is after the round-trip fee.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

import ml_features

warnings.filterwarnings("ignore")


def make_classifier(seed: int = 0):
    """XGBoost if present, then LightGBM, then sklearn's own boosted trees -
    which is the same histogram algorithm, so results are comparable."""
    try:
        from xgboost import XGBClassifier
        return XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            min_child_weight=20, reg_lambda=2.0,
            eval_metric="logloss", tree_method="hist",
            random_state=seed, n_jobs=4), "xgboost"
    except ImportError:
        pass
    try:
        from lightgbm import LGBMClassifier
        return LGBMClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, min_child_samples=20,
            reg_lambda=2.0, random_state=seed, n_jobs=4, verbose=-1), "lightgbm"
    except ImportError:
        pass
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(
        max_iter=300, max_depth=4, learning_rate=0.05,
        min_samples_leaf=20, l2_regularization=2.0,
        random_state=seed), "sklearn-hist"


def load_csv(directory: str, symbol: str) -> pd.DataFrame:
    path = os.path.join(directory, f"{symbol}.csv")
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.reader(fh):
            if not r or not r[0].lstrip("-").replace(".", "", 1).isdigit():
                continue
            ts = float(r[0])
            if ts > 1e11:
                ts /= 1000.0
            rows.append((ts, float(r[1]), float(r[2]), float(r[3]),
                         float(r[4]), float(r[5]) if len(r) > 5 else 0.0))
    rows.sort()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.set_index("ts")


def triple_barrier(df: pd.DataFrame, horizon: int, k_atr: float, fee_frac: float):
    """Label each bar by which barrier a trade opened at i+1 would hit first.

    Returns (label, up_ret, dn_ret, atr_frac):
      label 1  upper barrier first  -> a long would have won
      label 0  lower barrier first, or neither inside the horizon
      atr_frac barrier width as a fraction of the entry, so a result can be
               expressed in R-multiples later.

    Bars whose horizon runs past the end of the data get label -1 and are
    dropped: a partially observed outcome is not a label.
    """
    a = ml_features.atr(df, 14).values
    o = df["open"].values
    hi = df["high"].values
    lo = df["low"].values
    n = len(df)
    label = np.full(n, -1, dtype=int)
    atr_frac = np.full(n, np.nan)
    hit_bar = np.full(n, -1, dtype=int)

    for i in range(n - 1):
        if not np.isfinite(a[i]) or a[i] <= 0:
            continue
        entry = o[i + 1]
        if entry <= 0:
            continue
        width = k_atr * a[i]
        # The barrier must clear the fee, or a "win" can still lose money.
        if width / entry <= fee_frac:
            continue
        end = min(i + 1 + horizon, n)
        if i + 1 + horizon > n:
            break                      # outcome not fully observed - stop here
        up, dn = entry + width, entry - width
        lab, when = 0, end - 1
        for j in range(i + 1, end):
            up_hit = hi[j] >= up
            dn_hit = lo[j] <= dn
            if up_hit and dn_hit:
                lab, when = 0, j       # pessimistic: assume the loss first
                break
            if up_hit:
                lab, when = 1, j
                break
            if dn_hit:
                lab, when = 0, j
                break
        label[i] = lab
        atr_frac[i] = width / entry
        hit_bar[i] = when
    return label, atr_frac, hit_bar


def fold_trades(proba, label, atr_frac, thr, fee_frac, allow_short=True):
    """Turn probabilities into net R-multiples.

    A long is taken when p >= thr, a short when p <= 1 - thr. The barrier is
    symmetric, so a short wins exactly when the long label is 0 - but the
    fee is charged either way, which is what stops a 50/50 model from
    looking break-even.
    """
    out = []
    for p, y, af in zip(proba, label, atr_frac):
        if not np.isfinite(af) or y < 0:
            continue
        if p >= thr:
            gross = af if y == 1 else -af
        elif allow_short and p <= 1.0 - thr:
            gross = af if y == 0 else -af
        else:
            continue
        out.append((gross - fee_frac) / af)      # R-multiple, net of fee
    return np.array(out)


def summarise(r: np.ndarray, fee_frac: float) -> dict:
    if len(r) == 0:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy_r": 0.0, "total_r": 0.0, "avg_win_r": 0.0,
                "avg_loss_r": 0.0, "max_dd_r": 0.0}
    w, l = r[r > 0], r[r <= 0]
    curve = np.concatenate([[0.0], np.cumsum(r)])
    peak = np.maximum.accumulate(curve)
    return {
        "trades": int(len(r)),
        "win_rate": float(len(w) / len(r) * 100),
        "profit_factor": float(w.sum() / -l.sum()) if len(l) and l.sum() < 0 else float("inf"),
        "expectancy_r": float(r.mean()),
        "total_r": float(r.sum()),
        "avg_win_r": float(w.mean()) if len(w) else 0.0,
        "avg_loss_r": float(l.mean()) if len(l) else 0.0,
        "max_dd_r": float((peak - curve).max()),
    }


def walk_forward(X, y, af, model_seed, folds, horizon, embargo, thr_grid,
                 fee_frac, shuffle_labels=False, min_train=500):
    """Expanding-window walk forward with purge and embargo.

    Returns (all out-of-sample R-multiples, per-fold diagnostics).
    """
    n = len(X)
    fold_size = n // (folds + 1)
    rng = np.random.default_rng(model_seed)
    all_r, diag = [], []

    for f in range(folds):
        test_lo = fold_size * (f + 1)
        test_hi = min(fold_size * (f + 2), n)
        if test_hi - test_lo < 30:
            continue

        # PURGE: a training bar whose label resolves at or after the test
        # window starts has seen the test period. Drop it.
        train_hi = test_lo - horizon - 1
        if train_hi < min_train:
            continue
        tr = np.arange(0, train_hi)

        # Hold out the tail of training to pick the threshold. Purge that
        # boundary too, for exactly the same reason.
        val_lo = int(len(tr) * 0.80)
        fit_idx = tr[:max(1, val_lo - horizon - 1)]
        val_idx = tr[val_lo:]
        if len(fit_idx) < min_train or len(val_idx) < 50:
            continue

        yy = y.copy()
        if shuffle_labels:
            # Shuffle ONLY within the fitting window, so the test labels stay
            # real and the comparison isolates the model's contribution.
            perm = rng.permutation(len(fit_idx))
            yy[fit_idx] = y[fit_idx][perm]

        model, backend = make_classifier(model_seed + f)
        model.fit(X[fit_idx], yy[fit_idx])

        p_val = model.predict_proba(X[val_idx])[:, 1]
        best_thr, best_score = None, -1e18
        for t in thr_grid:
            r = fold_trades(p_val, y[val_idx], af[val_idx], t, fee_frac)
            if len(r) < 10:
                continue
            score = r.mean() * np.sqrt(len(r))      # edge, penalised for thinness
            if score > best_score:
                best_thr, best_score = t, score
        if best_thr is None:
            continue

        te = np.arange(test_lo, test_hi)
        p_te = model.predict_proba(X[te])[:, 1]
        r_te = fold_trades(p_te, y[te], af[te], best_thr, fee_frac)
        all_r.append(r_te)
        diag.append({"fold": f + 1, "threshold": best_thr,
                     "train": len(fit_idx), "test": len(te),
                     "trades": len(r_te),
                     "expectancy_r": float(r_te.mean()) if len(r_te) else 0.0,
                     "backend": backend})
        # EMBARGO is implicit in the expanding window: the next fold's
        # training set still stops `horizon` bars before its own test start.

    return (np.concatenate(all_r) if all_r else np.array([])), diag


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/1h")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--symbols", default="SOLUSDT,BTCUSDT,ETHUSDT,NEARUSDT")
    ap.add_argument("--horizon", type=int, default=24, help="bars to the vertical barrier")
    ap.add_argument("--k-atr", type=float, default=1.5, help="barrier width in ATR")
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--fee-bps", type=float, default=7.32)
    ap.add_argument("--null-runs", type=int, default=20,
                    help="shuffled-label repeats; 0 to skip (not recommended)")
    ap.add_argument("--json", default=None)
    ap.add_argument("--model-out", default=None, help="fit a final model on ALL data")
    a = ap.parse_args(argv)

    fee = a.fee_bps / 1e4
    thr_grid = [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70]
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    _, backend = make_classifier(0)
    print(f"=== walk-forward ML, {a.interval}, backend={backend}, "
          f"fee {a.fee_bps:.2f} bps ===")
    print(f"    triple barrier: +/-{a.k_atr:g} ATR, {a.horizon} bars, "
          f"purge+embargo {a.horizon} bars\n")

    per_symbol, pooled = {}, []
    null_means = []

    for sym in symbols:
        try:
            df = load_csv(a.csv, sym)
        except FileNotFoundError:
            print(f"  {sym}: no CSV in {a.csv} - run fetch_binance_data.py first")
            continue
        feats = ml_features.build(df)
        label, af, _ = triple_barrier(df, a.horizon, a.k_atr, fee)

        # A single degenerate column - a constant-volume stretch makes vol_z
        # all-NaN, for instance - would otherwise drop every row through the
        # all-columns notna() test, silently and with no clue why. Drop the
        # column instead, and say so.
        nan_frac = feats.isna().mean()
        dead = nan_frac[nan_frac > 0.5].index.tolist()
        if dead:
            print(f"  {sym}: dropping {len(dead)} mostly-empty feature(s): "
                  f"{', '.join(dead[:6])}{' ...' if len(dead) > 6 else ''}")
            feats = feats.drop(columns=dead)

        finite = feats.notna().all(axis=1).values
        labelled = (label >= 0) & np.isfinite(af)
        ok = finite & labelled
        if ok.sum() < 800:
            # Show WHERE the rows went, so an empty run is diagnosable rather
            # than just disappointing.
            print(f"  {sym}: only {int(ok.sum())} usable rows of {len(feats)} "
                  f"(features complete on {int(finite.sum())}, "
                  f"labelled on {int(labelled.sum())}) - skipping, need ~800+")
            continue
        X = feats.values[ok]
        y = label[ok]
        afv = af[ok]

        r, diag = walk_forward(X, y, afv, 0, a.folds, a.horizon, a.horizon,
                               thr_grid, fee)
        st = summarise(r, fee)
        per_symbol[sym] = st
        pooled.append(r)

        print(f"{sym}: {len(X)} labelled bars, base rate "
              f"{y.mean() * 100:.1f}% up-first")
        for d in diag:
            print(f"    fold {d['fold']}  thr {d['threshold']:.2f}  "
                  f"train {d['train']:5d}  trades {d['trades']:4d}  "
                  f"expectancy {d['expectancy_r']:+.4f}R")
        print(f"  OUT-OF-SAMPLE  trades {st['trades']}  win {st['win_rate']:.1f}%  "
              f"PF {st['profit_factor']:.2f}  expectancy {st['expectancy_r']:+.4f}R  "
              f"total {st['total_r']:+.1f}R  maxDD {st['max_dd_r']:.1f}R\n")

        if a.null_runs:
            for s in range(a.null_runs):
                rn, _ = walk_forward(X, y, afv, 1000 + s, a.folds, a.horizon,
                                     a.horizon, thr_grid, fee, shuffle_labels=True)
                if len(rn):
                    null_means.append(rn.mean())

    if not pooled:
        print("no symbols produced results")
        return 1

    allr = np.concatenate(pooled)
    st = summarise(allr, fee)
    print("=== POOLED OUT-OF-SAMPLE (this is the only line that matters) ===")
    print(f"  trades            {st['trades']}")
    print(f"  net win rate      {st['win_rate']:.1f}%")
    print(f"  profit factor     {st['profit_factor']:.2f}")
    print(f"  expectancy        {st['expectancy_r']:+.4f}R per trade")
    print(f"  avg win/avg loss  {st['avg_win_r']:+.2f}R / {st['avg_loss_r']:+.2f}R")
    print(f"  total             {st['total_r']:+.1f}R")
    print(f"  max drawdown      {st['max_dd_r']:.1f}R")

    if null_means:
        nm = np.array(null_means)
        p = float((nm >= st["expectancy_r"]).mean())
        print(f"\n=== NULL BASELINE ({len(nm)} shuffled-label runs) ===")
        print(f"  shuffled expectancy  mean {nm.mean():+.4f}R  "
              f"sd {nm.std():.4f}  95th pct {np.percentile(nm, 95):+.4f}R")
        print(f"  real expectancy      {st['expectancy_r']:+.4f}R")
        print(f"  p-value (fraction of noise runs that beat it): {p:.3f}")
        if p > 0.05:
            print("\n  NOT SIGNIFICANT. This model does not beat label noise.")
            print("  Do not trade it, and do not tune it until it does - tuning")
            print("  against a null result is how you find a lucky seed.")
        else:
            print("\n  Beats the null. Next: feed the numbers above into")
            print(f"  risk_simulator.py --win-rate {st['win_rate']:.1f} "
                  f"--avg-win {st['avg_win_r']:.2f} "
                  f"--avg-loss {abs(st['avg_loss_r']):.2f} "
                  f"--observed-trades {st['trades']}")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"pooled": st, "per_symbol": per_symbol,
                       "null_mean": float(np.mean(null_means)) if null_means else None},
                      fh, indent=2)
        print(f"\n  wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
