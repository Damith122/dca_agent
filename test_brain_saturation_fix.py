#!/usr/bin/env python3
"""
Regression tests for the 2026-08-21 Brain V2 classifier saturation fix.

THE BUG
--------------------------------------------------------------------------
The two REGRESSOR heads were given an explicit schedule:

    SGDRegressor(..., learning_rate="invscaling", eta0=0.01, power_t=0.25)

but all three CLASSIFIER heads were left on sklearn's alpha-derived default:

    SGDClassifier(..., alpha=1e-5, learning_rate="optimal")

For log_loss, "optimal" derives its rate from alpha alone:

    typw = sqrt(1/sqrt(alpha)) = 17.78
    eta0 = typw = 17.78 ;  t0 = 1/(eta0*alpha) = 5623
    eta(t) = 1/(alpha*(t+t0))   ->   eta(1) = 17.78,  eta(1e6) = 0.099

So the classifiers began training at 1778x the regressors' rate and were
still 10x higher after a million updates. On normalized features that
diverges: weights grow without bound and predict_proba collapses onto 0/1.
The live tp_hit head was emitting ~1e-200 for every setup on all four
symbols.

The decisive evidence that this was DIVERGENCE and not confidence: measured
on synthetic data with a known signal, the "optimal" schedule scored AUC
0.614 with an identical outcome rate in its top and bottom deciles (0.0050
vs 0.0050). A constant eta0=0.01 scored AUC 0.703 and separated the same
deciles 0.0000 vs 0.0300.

No network call and no real order is made anywhere in this file.
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import sys

import numpy as np
from sklearn.linear_model import SGDClassifier

import brain

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


print("\n[1] Every classifier head is on the corrected schedule")
b = brain.BrainV2()
for name in ("noise", "success", "tp_hit"):
    m = getattr(b, f"{name}_model")
    check(f"{name}: learning_rate is constant, not 'optimal'",
          m.learning_rate == "constant", f"got {m.learning_rate}")
    check(f"{name}: eta0 is set explicitly to 0.01",
          m.eta0 == 0.01, f"got {m.eta0}")

check("all three share one kwargs dict, so they cannot drift apart again",
      brain._CLASSIFIER_KW["learning_rate"] == "constant"
      and brain._CLASSIFIER_KW["eta0"] == 0.01)
check("the classifiers now match the regressors' eta0",
      b.trend_model.eta0 == b.tp_hit_model.eta0 == 0.01)


print("\n[2] The old schedule really does diverge (mechanism, not assertion)")
rng = np.random.default_rng(0)
X = rng.standard_normal((3000, brain.N_FEATURES_V2))
y = (rng.random(3000) < 0.10).astype(int)


def train(**kw):
    params = dict(loss="log_loss", penalty="l2", alpha=1e-5, warm_start=True)
    params.update(kw)
    m = SGDClassifier(**params)
    first = True
    for i in range(len(X)):
        m.partial_fit(X[i:i + 1], y[i:i + 1],
                      classes=np.array([0, 1]) if first else None)
        first = False
    return m, m.predict_proba(X[:500])[:, 1]


old_m, old_p = train(learning_rate="optimal")
new_m, new_p = train(learning_rate="constant", eta0=0.01)
old_peak = float(np.max(np.abs(old_m.coef_)))
new_peak = float(np.max(np.abs(new_m.coef_)))

check("the old schedule drives |coef| into the saturated range",
      old_peak >= brain.SATURATED_COEF_ABS, f"peak={old_peak:.1f}")
check("the corrected schedule stays far below it",
      new_peak < brain.SATURATED_COEF_ABS, f"peak={new_peak:.1f}")
check("the old schedule emits absurd probabilities",
      old_p.min() < 1e-10, f"min={old_p.min():.2e}")
check("the corrected schedule keeps probabilities in a usable range",
      new_p.min() > 1e-6, f"min={new_p.min():.2e}")


print("\n[3] Saturation is detected and the head is rebuilt")
b = brain.BrainV2()
x = rng.standard_normal(brain.N_FEATURES_V2)
for i in range(60):
    b.learn_tp_hit(x, i % 5 == 0)
check("a freshly trained head is not flagged", not b._head_is_saturated("tp_hit"))

b.tp_hit_model.coef_ = b.tp_hit_model.coef_ * 0 + 40.0
check("a diverged head IS flagged", b._head_is_saturated("tp_hit"))

reset = b.reset_saturated_heads()
check("reset_saturated_heads reports what it reset",
      [n for n, _ in reset] == ["tp_hit"], f"got {reset}")
check("...including the peak that condemned it",
      reset and abs(reset[0][1] - 40.0) < 1e-9)
check("the head is unfitted afterwards", b.tp_hit_fitted is False)
check("...its sample counter is cleared", b.tp_hit_samples == 0)
check("...its positive counter is cleared", b.tp_hit_positives == 0)
check("...and it must earn READY again",
      b.head_readiness()["tp_hit"] != "READY")
check("a healthy head is left alone",
      brain.BrainV2().reset_saturated_heads() == [])


print("\n[4] A diverged snapshot is repaired on load, not inherited")
b = brain.BrainV2()
for i in range(60):
    b.learn_tp_hit(x, i % 5 == 0)
    b.learn_noise(x, i % 3 == 0)
b.tp_hit_model.coef_ = b.tp_hit_model.coef_ * 0 + 40.0
b.noise_model.coef_ = b.noise_model.coef_ * 0 + 60.0
blob = b.to_bytes()

restored = brain.BrainV2.from_bytes(blob, brain.N_FEATURES_V2, 50)
check("the diverged tp_hit head does not survive the round trip",
      restored.tp_hit_fitted is False)
check("the diverged noise head does not either",
      restored.noise_fitted is False)
check("...and neither reports READY", 
      restored.head_readiness()["tp_hit"] != "READY"
      and restored.head_readiness()["noise"] != "READY")

# A snapshot whose heads are healthy must round-trip untouched - the reset
# must not become a silent wipe of good learning on every restart.
healthy = brain.BrainV2()
for i in range(60):
    healthy.learn_tp_hit(x, i % 5 == 0)
kept = brain.BrainV2.from_bytes(healthy.to_bytes(), brain.N_FEATURES_V2, 50)
check("a HEALTHY snapshot is preserved across load",
      kept.tp_hit_fitted is True and kept.tp_hit_samples == 60)


print("\n[5] The base rate is tracked and persisted")
b = brain.BrainV2()
for i in range(100):
    b.learn_tp_hit(x, i % 4 == 0)      # exactly 25% positive
check("positives are counted", b.tp_hit_positives == 25)
check("the base rate reflects them", abs(b.tp_hit_base_rate() - 0.25) < 1e-9)
check("an empty head reports a zero base rate",
      brain.BrainV2().tp_hit_base_rate() == 0.0)

rt = brain.BrainV2.from_bytes(b.to_bytes(), brain.N_FEATURES_V2, 50)
check("the positive count survives a save/load round trip",
      rt.tp_hit_positives == 25, f"got {rt.tp_hit_positives}")
check("...so the base rate does too",
      abs(rt.tp_hit_base_rate() - 0.25) < 1e-9)

print("\n[6] A pre-v4 snapshot is rebuilt even when |coef| looks fine")
# Found in live data: ETH's tp_hit head escaped the |coef| screen (it had
# drifted less far than the others), so it kept 27.6M restored samples while
# tp_hit_positives - absent from the older format - defaulted to 0. Base rate
# is positives/samples, so mixing the two windows pinned it at exactly 0.0,
# which silently disabled the veto for that symbol with no way to recover.
b = brain.BrainV2()
for i in range(400):
    b.learn_tp_hit(x, i % 3 == 0)
b.tp_hit_model.coef_ = b.tp_hit_model.coef_ * 0 + 2.0     # under SATURATED_COEF_ABS
check("the fixture sits below the |coef| screen",
      not b._head_is_saturated("tp_hit"))
legacy = b.to_state()
legacy["version"] = 3
legacy.pop("tp_hit_positives", None)
legacy.pop("tp_hit_labeled_samples", None)   # neither field existed at v3
mig = brain.BrainV2.from_bytes(
    __import__("pickle").dumps(legacy), brain.N_FEATURES_V2, 50)
check("a pre-v4 head is rebuilt anyway - it trained under the bad schedule",
      mig.tp_hit_fitted is False)
check("...its sample counter restarts with it", mig.tp_hit_samples == 0)
check("...so samples and positives share one window",
      mig.tp_hit_samples == 0 and mig.tp_hit_positives == 0)
check("...and the base rate is not pinned at a false zero over 400 samples",
      mig.tp_hit_base_rate() == 0.0 and mig.tp_hit_samples == 0)
check("noise and success are rebuilt on the same grounds",
      mig.noise_fitted is False and mig.success_fitted is False)

# A version-4 snapshot is already on the corrected schedule and must survive.
healthy4 = brain.BrainV2()
for i in range(400):
    healthy4.learn_tp_hit(x, i % 3 == 0)
kept4 = brain.BrainV2.from_bytes(healthy4.to_bytes(), brain.N_FEATURES_V2, 50)
check("a version-4 snapshot is NOT rebuilt",
      kept4.tp_hit_fitted is True and kept4.tp_hit_samples == 400)
check("...and keeps a real base rate",
      abs(kept4.tp_hit_base_rate() - 134/400) < 0.01,
      f"got {kept4.tp_hit_base_rate():.4f}")

print("\n[7] The base rate is measured over a consistent window")
# The live regression: a v4 snapshot re-saved by the previous build carried
# 27.6M lifetime samples but positives counted over only a few hours, giving
# 2.134e-06 against ~5e-04 on every other symbol - understated ~250x. Keying
# the migration on the format version missed it, because the snapshot was
# legitimately version 4.
legacy4 = brain.BrainV2()
for i in range(50):
    legacy4.learn_tp_hit(x, i % 5 == 0)
st = legacy4.to_state()
st["tp_hit_samples"] = 27_600_000      # lifetime count from the old format
st["tp_hit_positives"] = 59            # counted over an unknown, shorter span
st.pop("tp_hit_labeled_samples", None) # the field that did not exist yet
check("the fixture is version 4, so a version test would let it through",
      st["version"] == 4)
mig4 = brain.BrainV2.from_bytes(
    __import__("pickle").dumps(st), brain.N_FEATURES_V2, 50)
check("a v4 snapshot WITHOUT the window field is still rebuilt",
      mig4.tp_hit_fitted is False)
check("...the mismatched lifetime count is discarded",
      mig4.tp_hit_samples == 0)
check("...and the orphaned positive count with it",
      mig4.tp_hit_positives == 0 and mig4.tp_hit_labeled_samples == 0)
check("...so no 250x-understated rate can survive",
      mig4.tp_hit_base_rate() == 0.0)

# The counters must stay locked together from then on.
b = brain.BrainV2()
for i in range(1000):
    b.learn_tp_hit(x, i % 8 == 0)
check("labeled_samples tracks every label seen", b.tp_hit_labeled_samples == 1000)
check("positives is a strict subset", b.tp_hit_positives == 125)
check("the base rate is positives over the window",
      abs(b.tp_hit_base_rate() - 0.125) < 1e-9)
rt = brain.BrainV2.from_bytes(b.to_bytes(), brain.N_FEATURES_V2, 50)
check("the window survives a round trip",
      rt.tp_hit_labeled_samples == 1000 and rt.tp_hit_positives == 125)
check("...preserving the rate exactly",
      abs(rt.tp_hit_base_rate() - 0.125) < 1e-9)
b.reset_head("tp_hit")
check("a reset clears the whole window, not just the weights",
      b.tp_hit_labeled_samples == 0 and b.tp_hit_positives == 0
      and b.tp_hit_base_rate() == 0.0)

print("\n[8] Older snapshots still load")
state = b.to_state()
check("the format is versioned 4", state["version"] == 4)
for older in (2, 3):
    legacy = dict(state)
    legacy["version"] = older
    legacy.pop("tp_hit_positives", None)
    got = brain.BrainV2.from_bytes(
        __import__("pickle").dumps(legacy), brain.N_FEATURES_V2, 50)
    check(f"a version-{older} snapshot still loads",
          isinstance(got, brain.BrainV2))
    check(f"...defaulting its missing positive count to 0",
          got.tp_hit_positives == 0)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
