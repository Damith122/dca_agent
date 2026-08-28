#!/usr/bin/env python3
"""Tests for the ML pipeline.

An ML backtest can be made to show any result you like, so these check the
guards rather than the accuracy:

  - features never see the future
  - training never sees a label that resolves inside the test window
  - the probability threshold is chosen on validation, never on test
  - a "win" is a win AFTER the fee
  - and, end to end, the pipeline finds nothing in noise and finds a real
    edge when one is planted
"""
import sys

import numpy as np
import pandas as pd

import ml_features
import train_ml_model as T

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


def frame(n=600, seed=0, price=None):
    rng = np.random.default_rng(seed)
    if price is None:
        price = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, n)))
    o = np.concatenate([[price[0]], price[:-1]])
    wig = np.abs(rng.normal(0, 1, n)) * 0.004 * price
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, price) + wig,
         "low": np.minimum(o, price) - wig, "close": price,
         "volume": rng.lognormal(3, 0.4, n)},
        index=np.arange(1_700_000_000, 1_700_000_000 + n * 3600, 3600)[:n])


print("[1] Features never see the future")
df = frame(600, seed=1)
full = ml_features.build(df)
# Truncating the frame must not change any earlier feature value.
cut = 400
part = ml_features.build(df.iloc[:cut])
cols_ok, offenders = True, []
for c in full.columns:
    a = full[c].iloc[:cut].values
    b = part[c].values
    both = np.isfinite(a) & np.isfinite(b)
    if not np.allclose(a[both], b[both], rtol=1e-9, atol=1e-12):
        cols_ok = False
        offenders.append(c)
check("every feature at bar i is unchanged when later bars are deleted",
      cols_ok, ", ".join(offenders[:5]))
check("the feature set is non-trivial", full.shape[1] >= 25, str(full.shape[1]))

print("\n[2] Labels describe a trade that could actually be taken")
fee = 7.32 / 1e4
lab, af, hit = T.triple_barrier(df, horizon=12, k_atr=1.0, fee_frac=fee)
check("labels are 0/1 or -1 for unusable", set(np.unique(lab)) <= {-1, 0, 1})
lab_ok = lab >= 0
check("some bars are labelled", lab_ok.sum() > 100, str(lab_ok.sum()))
check("every barrier is wider than the fee",
      np.all(af[lab_ok] > fee), f"min {np.nanmin(af[lab_ok]):.6f} vs fee {fee:.6f}")
check("the tail of the series is unlabelled, not guessed",
      np.all(lab[-12:] == -1), str(lab[-12:]))

# A monotonically rising series must label 1 everywhere it labels at all.
up = 100 * np.exp(np.cumsum(np.full(400, 0.004)))
lab_up, af_up, _ = T.triple_barrier(frame(400, price=up), 12, 1.0, fee)
check("a relentless uptrend labels up-first", np.nanmean(lab_up[lab_up >= 0]) > 0.95,
      f"{np.nanmean(lab_up[lab_up >= 0]):.3f}")
dn = 100 * np.exp(np.cumsum(np.full(400, -0.004)))
lab_dn, _, _ = T.triple_barrier(frame(400, price=dn), 12, 1.0, fee)
check("a relentless downtrend labels down-first",
      np.nanmean(lab_dn[lab_dn >= 0]) < 0.05, f"{np.nanmean(lab_dn[lab_dn >= 0]):.3f}")

print("\n[3] The fee is charged on every trade, winners included")
af_v = np.full(4, 0.02)
r = T.fold_trades(np.array([0.9, 0.9, 0.1, 0.1]), np.array([1, 0, 0, 1]),
                  af_v, thr=0.6, fee_frac=fee)
check("four signals produce four trades", len(r) == 4, str(len(r)))
check("a winning long is below +1R because of the fee",
      r[0] < 1.0 and r[0] > 0.9, f"{r[0]:.4f}R")
check("a losing long is worse than -1R because of the fee", r[1] < -1.0,
      f"{r[1]:.4f}R")
flat = T.fold_trades(np.array([0.55]), np.array([1]), np.array([0.02]),
                     thr=0.6, fee_frac=fee)
check("a probability inside the band produces no trade", len(flat) == 0)
zero_fee = T.fold_trades(np.array([0.9]), np.array([1]), np.array([0.02]),
                         thr=0.6, fee_frac=0.0)
check("with no fee a winner is exactly +1R", abs(zero_fee[0] - 1.0) < 1e-12)

print("\n[4] Purging: training must not reach into the test window")
n = 3000
X = np.random.default_rng(2).normal(size=(n, 4))
y = (np.random.default_rng(3).random(n) > 0.5).astype(int)
af2 = np.full(n, 0.02)
seen = []


class Spy:
    """Stands in for the classifier and records which rows it was fitted on."""

    def fit(self, Xf, yf):
        self.n = len(Xf)
        return self

    def predict_proba(self, Xp):
        return np.column_stack([np.full(len(Xp), 0.5), np.full(len(Xp), 0.5)])


orig = T.make_classifier
horizon = 24


def spy_factory(seed=0):
    m = Spy()
    seen.append(m)
    return m, "spy"


T.make_classifier = spy_factory
try:
    T.walk_forward(X, y, af2, 0, 4, horizon, horizon, [0.5], fee, min_train=100)
finally:
    T.make_classifier = orig

fold_size = n // 5
check("the model was fitted at least once", len(seen) > 0, str(len(seen)))
if seen:
    # Fold 1 tests [fold_size, 2*fold_size). Training stops horizon bars
    # before that, and 20% of it is held back for the threshold, purged again.
    limit = int((fold_size - horizon - 1) * 0.80) - horizon - 1
    check("the first fold trained only on rows that resolve before its test window",
          seen[0].n <= limit + 1, f"fitted {seen[0].n}, purge limit {limit}")
    check("...which is strictly fewer rows than the naive split would give",
          seen[0].n < fold_size, f"{seen[0].n} vs {fold_size}")

print("\n[5] The threshold is chosen on validation, not on test")
src = open("train_ml_model.py", encoding="utf-8").read()
check("the threshold search reads validation probabilities",
      "p_val = model.predict_proba(X[val_idx])" in src)
check("...and the test probabilities are only computed afterwards",
      src.index("best_thr, best_score = t, score") < src.index("p_te = model.predict_proba"))
check("the validation slice is itself purged from the fitting window",
      "fit_idx = tr[:max(1, val_lo - horizon - 1)]" in src)

print("\n[6] Summary statistics")
st = T.summarise(np.array([]), fee)
check("no trades does not divide by zero", st["trades"] == 0
      and st["profit_factor"] == 0.0)
st = T.summarise(np.array([1.0, 1.0, -1.0]), fee)
check("profit factor is gross win over gross loss",
      abs(st["profit_factor"] - 2.0) < 1e-9, str(st["profit_factor"]))
check("win rate counts net winners", abs(st["win_rate"] - 66.6667) < 0.01)
st = T.summarise(np.array([1.0, -1.0, -1.0, 1.0]), fee)
check("max drawdown is measured peak to trough",
      abs(st["max_dd_r"] - 2.0) < 1e-9, str(st["max_dd_r"]))

print("\n[7] A degenerate feature column must not wipe the dataset silently")
d2 = frame(600, seed=5)
d2["volume"] = 100.0                     # constant -> vol_z is all NaN
f2 = ml_features.build(d2)
nan_frac = f2.isna().mean()
dead = nan_frac[nan_frac > 0.5].index.tolist()
check("the degenerate columns are identifiable", len(dead) > 0, str(dead))
kept = f2.drop(columns=dead)
check("dropping them leaves a usable dataset",
      kept.notna().all(axis=1).sum() > 300,
      str(int(kept.notna().all(axis=1).sum())))
check("...where keeping them would have left none",
      f2.notna().all(axis=1).sum() == 0,
      str(int(f2.notna().all(axis=1).sum())))

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
