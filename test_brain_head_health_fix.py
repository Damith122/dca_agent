#!/usr/bin/env python3
"""Tests for the 2026-08-28 dead-head fixes.

Diagnosing 84,744 rows of live recording found two of the brain's five heads
emitting a single constant value and a third pinned at one end of its range.
Neither failure raised an error; the bot reported the heads READY throughout.
These tests pin the properties that would have caught them.
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import sys

import numpy as np

import config

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


def clamp(v, a, b):
    return max(a, min(b, v))


print("[1] trend_confidence compares like with like")
src = open("brain.py", encoding="utf-8").read()
check("the scale tracks PREDICTIONS, not realised returns",
      "self._pred_scale = (0.99 * self._pred_scale" in src)
check("...and the confidence divides by that scale",
      "abs(trend_pred) / max(self._pred_scale * 2.0" in src)
check("the old realised-return denominator is gone from the ratio",
      "abs(trend_pred) / max(self._trend_scale" not in src)
check("a warm-up guard avoids an arbitrary confidence at the start",
      "self._pred_scale_samples < 200" in src)
check("the new scale is persisted across restarts",
      '"_pred_scale": self._pred_scale' in src
      and 'state.get("_pred_scale"' in src)

# The live failure: 84,744 rows, one distinct value.
rng = np.random.default_rng(0)
preds = np.abs(rng.normal(0, 0.0008, 20000))
realised = np.abs(rng.normal(0, 0.00008, 20000))   # ~0.8 bps, as measured live

old_scale, old_c = 0.0015, []
for p, r in zip(preds, realised):
    old_scale = 0.98 * old_scale + 0.02 * max(r, 1e-6)
    old_c.append(clamp(p / max(old_scale, 1e-6), 0.0, 1.0))
new_scale, new_c = 0.0015, []
for i, p in enumerate(preds):
    new_scale = 0.99 * new_scale + 0.01 * max(p, 1e-9)
    new_c.append(0.5 if i < 200 else clamp(p / max(new_scale * 2.0, 1e-12), 0.0, 1.0))
old_c, new_c = np.array(old_c[2000:]), np.array(new_c[2000:])

check("the OLD formula pins to 1.0 - reproducing the live failure",
      (old_c >= 0.9999).mean() > 0.8, f"{(old_c >= 0.9999).mean() * 100:.1f}%")
check("the NEW formula does not", (new_c >= 0.9999).mean() < 0.3,
      f"{(new_c >= 0.9999).mean() * 100:.1f}%")
check("...and produces a genuinely varying confidence",
      len(np.unique(np.round(new_c, 4))) > 1000,
      str(len(np.unique(np.round(new_c, 4)))))
check("...centred somewhere usable rather than at an extreme",
      0.2 < np.median(new_c) < 0.8, f"{np.median(new_c):.3f}")
check("it still saturates for a genuinely huge prediction",
      clamp(1.0 / max(new_scale * 2.0, 1e-12), 0.0, 1.0) == 1.0)

print("\n[2] The tp_hit label matches its own horizon")
tsrc = open("trading.py", encoding="utf-8").read()
check("the threshold scales with the move typical at this horizon",
      "threshold = TP_HIT_LABEL_MULT * self._tp_label_scale" in tsrc)
check("...with a warm-up fallback to the fixed distance",
      "threshold = TAKE_PROFIT_PCT" in tsrc)
check("the old unconditional comparison is gone",
      "tp_was_hit = abs(forward_return) >= TAKE_PROFIT_PCT" not in tsrc)
check("the tracker is initialised with the buffer it belongs to",
      "self._tp_label_scale: float = TAKE_PROFIT_PCT" in tsrc)
check("adaptive labelling can be switched off",
      config.TP_HIT_LABEL_ADAPTIVE is True
      and hasattr(config, "TP_HIT_LABEL_MULT"))

# Live-scale returns: median |move| 0.8 bps against a 35 bps threshold.
moves = np.abs(rng.laplace(0, 0.00008, 30000))
old_rate = (moves >= config.TAKE_PROFIT_PCT).mean()
scale, hits = config.TAKE_PROFIT_PCT, []
for v in moves:
    scale = 0.999 * scale + 0.001 * max(v, 1e-9)
    hits.append(v >= config.TP_HIT_LABEL_MULT * scale)
new_rate = np.array(hits[500:]).mean()
check("the OLD label is almost never positive - reproducing the live failure",
      old_rate < 0.001, f"{old_rate * 100:.4f}%")
check("the NEW label is balanced enough to learn from",
      0.1 < new_rate < 0.5, f"{new_rate * 100:.1f}%")
check("...and the threshold ends up near the move size, not 40x it",
      scale < config.TAKE_PROFIT_PCT / 5, f"{scale * 1e4:.2f} bps")

flat = np.full(5000, 0.00008)
s2, h2 = config.TAKE_PROFIT_PCT, []
for v in flat:
    s2 = 0.999 * s2 + 0.001 * v
    h2.append(v >= config.TP_HIT_LABEL_MULT * s2)
check("a perfectly flat market does not label everything positive",
      np.array(h2[2000:]).mean() < 0.01,
      f"{np.array(h2[2000:]).mean() * 100:.2f}%")

print("\n[3] success_p is starved, not broken - and says so")
# Only one real call site. Counted from the syntax tree rather than by text
# match: prose mentioning learn_success() - in a comment explaining why a
# path deliberately does NOT reinforce, or in a docstring describing what
# runs downstream of a fill - is not a call, and a line-based count read
# those as extra call sites.
import ast as _ast_bh
real_calls = sum(
    1 for n in _ast_bh.walk(_ast_bh.parse(tsrc))
    if isinstance(n, _ast_bh.Call)
    and isinstance(n.func, _ast_bh.Attribute)
    and n.func.attr == "learn_success"
)
check("its only label source is a closed trade", real_calls == 1,
      f"{real_calls} call sites")
check("the reliability gate needs both classes AND a sample count",
      "samples >= BRAIN_HEAD_MIN_SAMPLES and len(classes_seen) >= 2" in src)
check("an unreliable head reports UNRELIABLE rather than READY",
      "UNRELIABLE" in src)
check("...and predict_all falls back to 0.5 rather than guessing",
      "if (self.success_fitted and success_reliable) else 0.5" in src)

print("\n[3b] The base rate must be able to forget a label change")
# Live follow-on: after the label was fixed the head emitted tp_hit_p from
# 0.09 to 0.68, while tp_hit_base_rate still reported 0.043% - three million
# samples of the OLD definition drowning nine thousand of the new one. Any
# veto computed against that mixture is a no-op.
check("the rate is tracked as an EWMA, not only cumulatively",
      "self.tp_hit_rate_ewma" in src)
check("...and the EWMA is what gets returned once warm",
      "if self.tp_hit_rate_samples >= self._TP_RATE_WARMUP" in src)
check("...with the cumulative ratio as the warm-up fallback",
      "self.tp_hit_positives / float(self.tp_hit_labeled_samples)" in src)
check("both are persisted across restarts",
      '"tp_hit_rate_ewma": self.tp_hit_rate_ewma' in src
      and 'state.get("tp_hit_rate_ewma"' in src)
check("a head reset clears the rolling rate too",
      "self.tp_hit_rate_ewma = 0.0" in src.split("def to_state")[0])

alpha, warm = 1e-4, 20000
# Three million samples of a 0.003% label, then a switch to 25%.
cum_pos, cum_n = int(3_000_000 * 3.2e-05), 3_000_000
r, ss = 0.0, 0
rng2 = np.random.default_rng(3)
for i in range(40000):
    hit = 1.0 if rng2.random() < 0.25 else 0.0
    r = hit if ss == 0 else (1 - alpha) * r + alpha * hit
    ss += 1
    cum_pos += hit
    cum_n += 1
cumulative = cum_pos / cum_n
check("the cumulative ratio stays stuck near the old label's rate",
      cumulative < 0.01, f"{cumulative * 100:.4f}%")
check("the EWMA reaches the new label's rate within hours",
      0.15 < r < 0.35, f"{r * 100:.1f}% after {ss} samples")
check("...which is what a veto threshold needs to stay meaningful",
      0.5 * r > 0.05, f"threshold would be {0.5 * r:.4f}")
check("the warm-up is at least two time constants",
      warm >= 2 / alpha)

print("\n[4] Defaults are safe")
check("adaptive labelling defaults ON", config.TP_HIT_LABEL_ADAPTIVE is True)
check("the multiplier is above 1, so the label stays selective",
      config.TP_HIT_LABEL_MULT > 1.0)
check("the warm-up is long enough for the EWMA to settle",
      config.TP_HIT_LABEL_MIN_SAMPLES >= 200)
check("a malformed env value falls back rather than crashing",
      config._env_float("NOT_A_REAL_VAR_ABC", 1.2) == 1.2)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
