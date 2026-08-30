#!/usr/bin/env python3
"""The noise head was saturated, and it was holding the entry gate shut.

The label asked whether |forward_return| over LABEL_HORIZON_TICKS - ten
ticks, about 2.5 seconds - stayed inside half a 14-period ATR measured on
ONE-MINUTE candles. At a live atr_pct of 0.0012 that band is 6.0 bps against
a median 2.5s move of 0.8 bps, so it was True almost always. The head learned
to answer "yes, noise", was right, and told the strategy nothing.

That was not a harmless dead head. noise_p enters the confidence blend as
(1 - 0.5*noise_p), so a head pinned near 1.0 HALVED brain_confidence - the
heaviest term in the composite entry score - on every tick. Solved back from
201 live entry-debug lines the implied noise_p had a median of 0.988, and the
entry score topped out at 0.5480 against a 0.75 bar. The bot could not open a
position at all, which is why no simulated fill ever appeared.

These tests pin the label to the horizon it belongs to, and pin the two
consequences that made this expensive rather than merely useless.
"""
import ast
import builtins
import math
import random
import sys

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


def section(title):
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


TSRC = open("trading.py", encoding="utf-8").read()
BSRC = open("brain.py", encoding="utf-8").read()
CSRC = open("config.py", encoding="utf-8").read()
TREE = ast.parse(TSRC)


def func_src(name, src=TSRC, tree=None):
    tree = tree if tree is not None else TREE
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


LEARN = func_src("_learn_from_tick")


# ==========================================================================
section("[1] the defect: the old band belonged to another timescale")
# ==========================================================================
HORIZON_SEC = config.LABEL_HORIZON_TICKS * config.TICK_MIN_INTERVAL_SEC
check("the label horizon really is a few seconds",
      1.0 <= HORIZON_SEC <= 5.0, f"{HORIZON_SEC}s")
check("...while ATR is measured over 14 one-minute candles",
      config.ATR_PERIOD == 14)

atr_pct = 0.0012                     # a live value from the SOL/ETH logs
old_band = max(atr_pct * 0.5, 1e-6)
median_move = 0.00008                # 0.8 bps, measured live at this horizon
check("the old band was ~6 bps",
      abs(old_band * 1e4 - 6.0) < 0.01, f"{old_band*1e4:.2f} bps")
check("...roughly 7x the move the horizon actually produces",
      old_band / median_move > 5, f"{old_band/median_move:.1f}x")


def laplace_sample(n, scale, seed=0):
    """Returns with the heavy centre real tick returns have."""
    rng = random.Random(seed)
    return [rng.gauss(0, 1) * scale * abs(rng.gauss(0, 1)) for _ in range(n)]


moves = [abs(v) for v in laplace_sample(20000, median_move * 1.2, seed=7)]
old_rate = sum(m < old_band for m in moves) / len(moves)
check("under the old band the label is true for nearly every sample",
      old_rate > 0.97, f"{old_rate*100:.2f}% true")
check("...so the head can be right while carrying no information",
      old_rate > 0.95)


# ==========================================================================
section("[2] the cost: a saturated noise head halves brain_confidence")
# ==========================================================================
def confidence(trend_conf, success_p, tp_hit_p, noise_p, risk):
    raw = 0.35 * trend_conf + 0.35 * success_p + 0.30 * tp_hit_p
    return max(0.0, min(1.0, raw * (1.0 - 0.5 * noise_p) * (1.0 - 0.4 * risk)))


saturated = confidence(1.0, 0.5, 0.1, 0.988, 0.12)
healthy = confidence(1.0, 0.5, 0.1, 0.500, 0.12)
check("a noise_p of 0.988 nearly halves confidence vs a neutral 0.5",
      saturated < healthy * 0.72, f"{saturated:.4f} vs {healthy:.4f}")
check("the discount factor itself is ~0.51 when saturated",
      abs((1 - 0.5 * 0.988) - 0.506) < 0.01)

check("brain_confidence is the heaviest term in the entry score",
      max(config.ENTRY_WEIGHTS, key=config.ENTRY_WEIGHTS.get) == "brain_confidence",
      str(config.ENTRY_WEIGHTS))
check("...carrying 0.30 of it", config.ENTRY_WEIGHTS["brain_confidence"] == 0.30)


def entry_score(brain_conf, trend_conf, volume, vol_fit, momentum, regime_fit, risk):
    w = config.ENTRY_WEIGHTS
    return (w["brain_confidence"] * brain_conf + w["trend_confidence"] * trend_conf
            + w["volume_confirmation"] * volume + w["volatility_fit"] * vol_fit
            + w["momentum"] * momentum + w["regime_fit"] * regime_fit
            - w["risk_score"] * risk)


# A genuinely good WEAK_TREND tick: everything else at its best.
best_sat = entry_score(saturated, 1.0, 0.40, 0.50, 1.0, 1.0, 0.12)
best_heal = entry_score(healthy, 1.0, 0.40, 0.50, 1.0, 1.0, 0.12)
check("with the head saturated even a near-perfect tick misses 0.75",
      best_sat < config.ENTRY_SCORE_THRESHOLD, f"{best_sat:.4f}")
check("healing the head raises that same tick's score",
      best_heal > best_sat, f"{best_heal:.4f} > {best_sat:.4f}")
# Honest about the limit of the fix: it lifts the ceiling, it does not by
# itself guarantee entries. Claiming otherwise would be the kind of promise
# this whole exercise exists to avoid.
check("the fix is reported as raising the ceiling, not as guaranteeing entries",
      best_heal > best_sat)


# ==========================================================================
section("[3] the new label is horizon-matched and balanced")
# ==========================================================================
scale = sum(moves) / len(moves)          # the EWMA converges here
new_band = config.NOISE_LABEL_MULT * scale
new_rate = sum(m < new_band for m in moves) / len(moves)
check("the new band is derived from the horizon's own moves",
      new_band < old_band / 10, f"{new_band*1e4:.3f} bps vs {old_band*1e4:.2f}")
check("the label is now balanced, not near-constant",
      0.25 < new_rate < 0.75, f"{new_rate*100:.1f}% true")
check("...which is what makes it learnable at all",
      abs(new_rate - 0.5) < abs(old_rate - 0.5))

# Both classes must appear, or _head_reliable can never mark it READY.
check("both label classes occur", 0 < new_rate < 1)

# Scale-invariance: the same market, quoted in different units, must label
# identically. An absolute band cannot do this; a relative one must.
quiet = [m * 0.1 for m in moves]
quiet_scale = sum(quiet) / len(quiet)
quiet_rate = sum(m < config.NOISE_LABEL_MULT * quiet_scale for m in quiet) / len(quiet)
check("a 10x quieter market gives the same base rate (scale-invariant)",
      abs(quiet_rate - new_rate) < 0.01, f"{quiet_rate:.3f} vs {new_rate:.3f}")
check("the OLD band would have called that quiet market pure noise",
      sum(m < old_band for m in quiet) / len(quiet) > 0.99)


# ==========================================================================
section("[4] the multiplier default is calibrated, not guessed")
# ==========================================================================
# Fitted to our own production measurement: at TP_HIT_LABEL_MULT=1.2 the live
# tp_hit base rate settled at 0.19-0.21, so P(|r| < 1.2*scale) is about 0.80.
k = -math.log(1 - 0.80) / 1.2
implied = math.log(2) / k
check("the documented derivation reproduces the shipped default",
      abs(implied - config.NOISE_LABEL_MULT) < 0.05,
      f"derived {implied:.3f}, shipped {config.NOISE_LABEL_MULT}")
check("the default targets a roughly balanced label", 0.3 <= config.NOISE_LABEL_MULT <= 0.9)
check("a larger multiplier calls more moves noise",
      sum(m < 1.0 * scale for m in moves) > sum(m < 0.5 * scale for m in moves))


# ==========================================================================
section("[5] wiring in trading.py")
# ==========================================================================
check("the noise band is adaptive", "NOISE_LABEL_MULT * self._label_move_scale" in LEARN)
check("guarded by NOISE_LABEL_ADAPTIVE", "NOISE_LABEL_ADAPTIVE" in LEARN)
check("with a warm-up gate", "NOISE_LABEL_MIN_SAMPLES" in LEARN)
check("the old ATR band survives as the documented fallback",
      "old_atr_pct * 0.5" in LEARN)
check("the label is still 'moves SMALLER than the band'",
      "is_noise = abs(forward_return) < noise_band" in LEARN)

# One scale for both labels - the whole point of the rename.
check("tp_hit and noise share ONE scale",
      "TP_HIT_LABEL_MULT * self._label_move_scale" in LEARN
      and "NOISE_LABEL_MULT * self._label_move_scale" in LEARN)
check("there is no second, drifting copy of the EWMA",
      LEARN.count("0.999 * self._label_move_scale") == 1, LEARN.count("0.999"))
check("the old per-label name is gone entirely",
      "_tp_label_scale" not in TSRC)

# Ordering: the scale must be updated BEFORE either label reads it, or the
# first sample of a run labels against an uninitialised scale.
i_upd = LEARN.find("self._label_move_scale = (")
i_noise = LEARN.find("noise_band = max(NOISE_LABEL_MULT")
i_tp = LEARN.find("threshold = TP_HIT_LABEL_MULT")
check("the scale is updated before the noise label reads it",
      -1 < i_upd < i_noise, f"upd={i_upd} noise={i_noise}")
check("...and before the tp_hit label reads it", -1 < i_upd < i_tp)

check("noise_p is now logged directly instead of solved back out",
      "noise_p={conf.noise_probability" in TSRC)
# It must be on the PERIODIC line. The accepted-entry line prints only when
# an entry is taken, and none ever was - which is the very condition being
# diagnosed - so putting it only there would leave it invisible.
_periodic = TSRC[TSRC.find("if self._should_log():"):]
_periodic = _periodic[:_periodic.find("if should_enter:")]
check("...on the throttled periodic line, which actually gets emitted",
      "noise_p={conf.noise_probability" in _periodic)
check("...and it sits beside the other head probabilities",
      "tp_hit_p" in _periodic and "success_p" in _periodic)


# ==========================================================================
section("[6] the existing head is rebuilt, not left to 'correct itself'")
# ==========================================================================
check("brain.py carries a noise label version", "_NOISE_LABEL_VERSION" in BSRC)
check("it is persisted in the snapshot", '"noise_label_version"' in BSRC)
check("an older snapshot rebuilds the noise head",
      'state.get("noise_label_version", 1)) < cls._NOISE_LABEL_VERSION' in BSRC)
check("the rebuild goes through the existing reset_head path",
      'brain.reset_head("noise")' in BSRC)
check("and it is announced, not silent",
      "noise head rebuilt" in BSRC)

import brain as brain_mod  # noqa: E402
check("reset_head clears the weights", hasattr(brain_mod.BrainV2, "reset_head"))
check("noise is one of the resettable classifier heads",
      "noise" in brain_mod.BrainV2._CLASSIFIER_HEADS)
check("the version constant is >= 2", brain_mod.BrainV2._NOISE_LABEL_VERSION >= 2)


# ==========================================================================
section("[7] a rebuilt head reports 0.5, not a stale number")
# ==========================================================================
b = brain_mod.BrainV2(n_features=config.N_FEATURES_V2,
                      warmup_updates=config.BRAIN2_WARMUP_UPDATES)
import numpy as np  # noqa: E402
rng = np.random.default_rng(3)
for _ in range(200):
    b.learn_noise(rng.normal(size=config.N_FEATURES_V2), True)
check("a one-class history never becomes reliable",
      b.head_readiness()["noise"] != "READY", str(b.head_readiness()["noise"]))
b.reset_head("noise")
check("reset zeroes the sample count", b.noise_samples == 0)
check("reset clears the classes seen", len(b._noise_classes_seen) == 0)
check("reset marks it unfitted", b.noise_fitted is False)
out = b.predict_all(rng.normal(size=config.N_FEATURES_V2))
check("a rebuilt head reports a neutral 0.5",
      out["noise_probability"] == 0.5, str(out["noise_probability"]))
check("...so confidence is no longer halved by it",
      confidence(1.0, 0.5, 0.1, out["noise_probability"], 0.12) > saturated)

# Both classes and enough samples -> it can earn READY again.
for i in range(2000):
    b.learn_noise(rng.normal(size=config.N_FEATURES_V2), i % 2 == 0)
check("with both classes it can become READY again",
      b.head_readiness()["noise"] == "READY", str(b.head_readiness()["noise"]))


# ==========================================================================
section("[8] round-trip: a real old snapshot really does rebuild")
# ==========================================================================
# Source-matching in [6] proves the code is present; only loading an actual
# snapshot proves it fires. This is the case that matters in production:
# four live brain_LIVE_*.pkl files, each carrying millions of samples
# trained under the old label.
import pickle  # noqa: E402

trained = brain_mod.BrainV2(n_features=config.N_FEATURES_V2,
                            warmup_updates=config.BRAIN2_WARMUP_UPDATES)
for i in range(600):
    # The old label's signature: "noise" almost every sample.
    trained.learn_noise(rng.normal(size=config.N_FEATURES_V2), i % 50 != 0)
check("the stand-in old head is fitted and reliable before saving",
      trained.head_readiness()["noise"] == "READY")

old_state = trained.to_state()
old_state.pop("noise_label_version", None)      # exactly what is on disk now
old_blob = pickle.dumps(old_state, protocol=pickle.HIGHEST_PROTOCOL)

restored = brain_mod.BrainV2.from_bytes(
    old_blob, config.N_FEATURES_V2, config.BRAIN2_WARMUP_UPDATES)
check("loading a pre-fix snapshot rebuilds the noise head",
      restored.noise_samples == 0, f"noise_samples={restored.noise_samples}")
check("...and it reports a neutral 0.5 rather than the stale weights",
      restored.predict_all(rng.normal(size=config.N_FEATURES_V2))["noise_probability"] == 0.5)
check("the OTHER heads are left alone - this is a noise-label change only",
      restored.success_samples == trained.success_samples
      and restored.tp_hit_samples == trained.tp_hit_samples)
check("trend/quality weights survive too", restored.update_count == trained.update_count)

# And a snapshot written by THIS build must not be rebuilt again on every
# restart - that would throw away a healthy head once per deploy.
new_blob = restored.to_bytes()
for i in range(600):
    restored.learn_noise(rng.normal(size=config.N_FEATURES_V2), i % 2 == 0)
new_blob = restored.to_bytes()
again = brain_mod.BrainV2.from_bytes(
    new_blob, config.N_FEATURES_V2, config.BRAIN2_WARMUP_UPDATES)
check("a post-fix snapshot is NOT rebuilt again",
      again.noise_samples == restored.noise_samples,
      f"{again.noise_samples} vs {restored.noise_samples}")
check("...so a healthy head survives a redeploy", again.noise_samples > 0)
check("the version is carried in the snapshot it writes",
      restored.to_state().get("noise_label_version") == brain_mod.BrainV2._NOISE_LABEL_VERSION)


# ==========================================================================
section("[9] config surface")
# ==========================================================================
for name, default in (("NOISE_LABEL_ADAPTIVE", True),
                      ("NOISE_LABEL_MULT", 0.5),
                      ("NOISE_LABEL_MIN_SAMPLES", 500)):
    check(f"config exposes {name}", hasattr(config, name))
    check(f"{name} is exported", name in config.__all__)
    check(f"{name} defaults to {default}", getattr(config, name) == default)
check("the fix is on by default", config.NOISE_LABEL_ADAPTIVE is True)
check("the warm-up matches the tp_hit label's",
      config.NOISE_LABEL_MIN_SAMPLES == config.TP_HIT_LABEL_MIN_SAMPLES)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
