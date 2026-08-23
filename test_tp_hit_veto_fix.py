"""Offline regression tests for the 2026-08-21 tp_hit probability veto.

THE EVIDENCE
--------------------------------------------------------------------------
Brain V2's tp_hit head predicts whether a trade will reach take-profit. Across
18 closed trades on 2026-08-21 (ETHUSDT + NEARUSDT) it was the sharpest single
divider in the record:

    tp_hit_prob ~0 (<1e-20)   10 trades   1/10 wins (10%)   net -$1.5461
    tp_hit_prob 0.5            7 trades   2/7  wins (29%)   net -$0.1467
    tp_hit_prob 1.0            1 trade    1/1  wins (100%)  net +$0.3596

EVERY dollar of the drawdown sits in the near-zero bucket. All six of ETH's
post-scale-up losses were in it - 1.9e-52, 1.1e-46, 6.9e-23, 7.5e-48, 1.0e-21,
2.9e-46 - and all six lost.

The head already fed ConfidenceEngine at 30% weight, but that only moved
entry_confidence from ~0.64 to ~0.50 - not enough to stop entries clearing the
composite threshold by as little as 0.004 (ETH 14:03, score 0.6337 vs 0.63).

THE FIX
--------------------------------------------------------------------------
UPDATE 2026-08-21 (later): the head that produces these probabilities was
found to have DIVERGED, not learned. All three SGDClassifier heads used
learning_rate="optimal" with no eta0, which at alpha=1e-5 starts at eta=17.78
- 1778x the 0.01 the two regressors were explicitly given. Weights grew
without bound and predict_proba collapsed onto 0/1, which is where the 1e-200
readings come from. Measured on synthetic data with a known signal, that
schedule scored AUC 0.614 with an IDENTICAL outcome rate in its top and
bottom deciles: its extreme probabilities carried no information whatsoever.
See test_brain_saturation_fix.py.

Two consequences for this suite:
  * TP_HIT_VETO_ENABLED now defaults to FALSE. Left on, it would halt
    trading outright against a head whose output means nothing.
  * The threshold is no longer an absolute probability. A correctly
    calibrated head tops out near 0.09 on a label this rare, so the old 0.10
    floor was unreachable - it would have vetoed 100% of entries even with a
    perfect model. It is now TP_HIT_VETO_BASE_RATE_RATIO x the observed base
    rate. The cases below pass base_rate=0.20 so the effective cutoff is
    still 0.10 and the historical expectations remain meaningful.

tp_hit_prob below the veto threshold is a HARD VETO, on the same
footing as the regime / dead-market / counter-momentum / momentum-exhaustion
guards: it can only ever REJECT a trade the old code would have taken, and it
touches nothing outside entry selection.

Gated on head_readiness()["tp_hit"] == "READY". brain.py already returns
exactly 0.5 for an unfitted or unreliable head:

    tp_hit_prob = (float(self.tp_hit_model.predict_proba(xn)[0][1])
                   if (self.tp_hit_fitted and tp_hit_reliable) else 0.5)

so a floor below 0.5 could not fire on one anyway - but the readiness check is
explicit so the behaviour does not depend on that coincidence, and a symbol
whose head has never seen both outcome classes is never blocked by a head that
cannot yet have an opinion.

HONEST CAVEAT, asserted below: one WIN sat in the near-zero bucket (NEAR at
7.8e-44, +$0.1331). This filter would have cost that trade. 18 samples is a
small sample.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_tp_hit_veto_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import io
import sys

import config
import trading

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -> ' + detail) if detail and not cond else ''}")


class Capture:
    def __enter__(self):
        self._buf = io.StringIO()
        self._stdout = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *exc):
        sys.stdout = self._stdout

    @property
    def text(self):
        return self._buf.getvalue()


def conf_with(tp_hit_prob, side="LONG"):
    """A confidence reading strong enough to pass the composite threshold, so
    the veto is provably the thing doing the rejecting."""
    return trading.ConfidenceReading(
        # 0.50 -> 0.75 alongside the STRONG_TREND switch above: brain_confidence
        # carries weight 0.30, so this lifts the composite clear of the 0.75
        # trending threshold. A PRECONDITION of these tests, not their subject.
        confidence_score=0.75,
        trend_confidence=1.0,
        trend_direction=side,
        success_probability=0.5,
        tp_hit_probability=tp_hit_prob,
        noise_probability=0.0,
        risk_score=0.125,
    )


def tradeable_regime():
    # 2026-08-23: was SIDEWAYS, chosen because it carried the lowest entry
    # threshold (0.63) and so let the fixture clear the bar. SIDEWAYS is now an
    # off-switch (threshold 0.85, above the 0.84 structural ceiling), so it can
    # never accept and the control cases would all fail for the wrong reason.
    #
    # STRONG_TREND at 0.75 is the tradable regime now; conf_with() was
    # strengthened to clear it. The requirement is unchanged: the control cases
    # must genuinely be ACCEPTED, so that when a case is rejected it is
    # provably the veto doing it and not the score threshold.
    return trading.RegimeReading(
        regime=trading.REGIME_STRONG_TREND, atr_pct=0.0030, atr_ratio=1.4,
    )


def good_flow(side="LONG"):
    sign = 1.0 if side == "LONG" else -1.0
    return {
        "data_available": True,
        "imbalance": 0.45 * sign,
        "trade_delta": 900.0 * sign,
        "book_support": True,
        "flow_aligned": True,
        "blocked": False,
        "reason": "",
    }


READY = {"tp_hit": "READY", "success": "READY", "trend": "READY", "noise": "READY"}
NOT_READY = {"tp_hit": "UNRELIABLE", "success": "READY", "trend": "READY", "noise": "READY"}
WARMING = {"tp_hit": "WARMING_UP", "success": "READY", "trend": "READY", "noise": "READY"}


# The veto threshold is TP_HIT_VETO_BASE_RATE_RATIO x base_rate. With the
# default ratio of 0.5, a base rate of 0.20 reproduces the 0.10 cutoff these
# cases were originally written against, so the historical expectations below
# stay meaningful under the new base-rate-relative model.
BASE_RATE_FOR_010 = 0.20
ENOUGH_LABELS = 10 * config.TP_HIT_VETO_MIN_SAMPLES


def decide(tp_hit_prob, readiness=READY, side="LONG", momentum=0.004,
           base_rate=BASE_RATE_FOR_010, samples=ENOUGH_LABELS):
    engine = trading.EntryEngineV2()
    engine._last_log_ts = 9e18          # suppress the throttled debug line
    return engine.evaluate(
        conf_with(tp_hit_prob, side), tradeable_regime(), volume_z=2.0,
        momentum=momentum, features=[0.0] * 34,
        brain_readiness=readiness, orderflow=good_flow(side),
        tp_hit_base_rate=base_rate, tp_hit_samples=samples,
    )


# ===========================================================================
print("\n[1] Config surface")
# ===========================================================================
# 2026-08-21: the default is now OFF. The head this veto reads had diverged
# and was emitting ~1e-200 for every setup, so defaulting it on would halt
# trading outright on any deployment that has not set the variable.
check("TP_HIT_VETO_ENABLED defaults OFF after the saturation finding",
      config.TP_HIT_VETO_ENABLED is False)
check("the threshold is base-rate-relative by default",
      config.TP_HIT_VETO_BASE_RATE_RATIO == 0.5,
      f"got {config.TP_HIT_VETO_BASE_RATE_RATIO}")
check("the absolute floor is disabled by default",
      config.TP_HIT_VETO_MIN_PROB == 0.0,
      f"got {config.TP_HIT_VETO_MIN_PROB}")
check("a ratio of 0.5 means 'half as likely as an arbitrary moment'",
      0.0 < config.TP_HIT_VETO_BASE_RATE_RATIO < 1.0)
check("the base rate must be established before the veto acts",
      config.TP_HIT_VETO_MIN_SAMPLES >= 1000)

# Every behavioural case below exercises the veto's LOGIC, so it is forced on
# explicitly rather than relying on the shipped default.
trading.TP_HIT_VETO_ENABLED = True

# ===========================================================================
print("\n[2] A control trade with a healthy tp_hit_prob is NOT vetoed")
# ===========================================================================
d = decide(1.0)
check("tp_hit_prob=1.0 is not vetoed", d.components.get("tp_hit_veto") is False)
check("...and the trade is accepted", d.should_enter is True,
      f"reason={d.components.get('rejection_reason')}")
d = decide(0.5)
check("tp_hit_prob=0.5 (the neutral default) is not vetoed",
      d.components.get("tp_hit_veto") is False)
check("...and the trade is accepted", d.should_enter is True,
      f"reason={d.components.get('rejection_reason')}")
d = decide(0.10)
check("tp_hit_prob exactly at the floor is NOT vetoed (strict <)",
      d.components.get("tp_hit_veto") is False)
d = decide(0.15)
check("tp_hit_prob=0.15 (genuinely uncertain) still trades",
      d.components.get("tp_hit_veto") is False and d.should_enter is True)

# ===========================================================================
print("\n[3] REPLAY: the six ETH post-scale-up losses are all blocked")
# ===========================================================================
ETH_LOSSES = [
    ("11:20", 1.8660119750629455e-52, -0.16313697),
    ("11:48", 1.1308414383708064e-46, -0.20281991),
    ("12:24", 6.903535847740285e-23, -0.12509732),
    ("12:42", 7.521246138184822e-48, -0.07313559),
    ("13:30", 9.973089476602642e-22, -0.17444367),
    ("14:03", 2.861939996992007e-46, -0.10192295),
]
saved = 0.0
for label, prob, pnl in ETH_LOSSES:
    d = decide(prob)
    blocked = d.components.get("tp_hit_veto") is True and d.should_enter is False
    saved += -pnl if blocked else 0.0
    check(f"ETH {label} (tp_hit_prob={prob:.1e}, netted ${pnl:+.4f}) is vetoed", blocked,
          f"reason={d.components.get('rejection_reason')}")
print(f"       -> would have avoided ${saved:.4f} of realized loss on ETH alone")
check("all six ETH post-scale losses are blocked", abs(saved - 0.84055641) < 1e-6,
      f"saved {saved:.6f}")

# ===========================================================================
print("\n[4] REPLAY: the NEAR losses blocked, and the ONE win it would cost")
# ===========================================================================
NEAR = [
    ("10:22", 2.223923886714714e-86, -0.30760379, "loss"),
    ("11:14", 1.0273852137440822e-29, -0.3075744, "loss"),
    ("12:17", 3.424208139507998e-67, -0.2235702, "loss"),
    ("07:51", 7.796813770729679e-44, +0.133138, "WIN"),
]
for label, prob, pnl, kind in NEAR:
    d = decide(prob)
    blocked = d.components.get("tp_hit_veto") is True
    check(f"NEAR {label} ({kind}, ${pnl:+.4f}) is vetoed", blocked)

# The honest accounting for the whole near-zero bucket.
bucket = [p for _l, p, _n in ETH_LOSSES] + [p for _l, p, _n, _k in NEAR]
pnls = [n for _l, _p, n in ETH_LOSSES] + [n for _l, _p, n, _k in NEAR]
check("the veto blocks the ENTIRE near-zero bucket", all(
    decide(p).components.get("tp_hit_veto") is True for p in bucket))
print(f"       bucket net was ${sum(pnls):+.4f} across {len(pnls)} trades "
      f"({sum(1 for n in pnls if n > 0)} win, {sum(1 for n in pnls if n <= 0)} losses)")
check("blocking the whole bucket is net POSITIVE despite costing one win",
      sum(pnls) < 0, f"bucket net {sum(pnls):+.4f}")

# ===========================================================================
print("\n[5] The winners with a healthy prob are NOT touched")
# ===========================================================================
# NEAR 09:07 took profit with tp_hit_prob = 1.0; the two ETH profit-lock wins
# ran at the neutral 0.5. None may be blocked.
for label, prob, pnl in [("NEAR 09:07 TP", 1.0, +0.3595935),
                         ("ETH 07:15 lock", 0.5, +0.09680012),
                         ("ETH 08:36 lock", 0.5, +0.05124295)]:
    d = decide(prob)
    check(f"{label} (${pnl:+.4f}) survives the veto",
          d.components.get("tp_hit_veto") is False and d.should_enter is True)

# ===========================================================================
print("\n[6] An unready head can NEVER veto")
# ===========================================================================
for readiness, label in ((NOT_READY, "UNRELIABLE"), (WARMING, "WARMING_UP"), ({}, "absent")):
    d = decide(1e-50, readiness=readiness)
    check(f"tp_hit head {label} -> no veto even at 1e-50",
          d.components.get("tp_hit_veto") is False,
          f"a head that cannot have an opinion must not block trading")
    check(f"...and {label} still reports head_ready=False",
          d.components.get("tp_hit_head_ready") is False)
d = decide(1e-50, readiness=READY)
check("a READY head reports head_ready=True", d.components.get("tp_hit_head_ready") is True)

# ===========================================================================
print("\n[7] The kill switch works")
# ===========================================================================
orig = trading.TP_HIT_VETO_ENABLED
try:
    trading.TP_HIT_VETO_ENABLED = False
    d = decide(1e-50)
    check("TP_HIT_VETO_ENABLED=False restores the previous behaviour exactly",
          d.components.get("tp_hit_veto") is False and d.should_enter is True)
finally:
    trading.TP_HIT_VETO_ENABLED = orig

orig_floor = trading.TP_HIT_VETO_BASE_RATE_RATIO
try:
    # The ratio scales the threshold: threshold = ratio x base_rate. At the
    # 0.20 base rate these cases use, the default 0.5 gives 0.10, so a
    # tp_hit_prob of 0.15 passes. Raising the ratio to 0.90 lifts the
    # threshold to 0.18 and the same setup is now blocked.
    trading.TP_HIT_VETO_BASE_RATE_RATIO = 0.90
    check("raising the ratio raises the threshold and vetoes more",
          decide(0.15).components["tp_hit_veto"] is True)
    check("...and the threshold is exactly ratio x base rate",
          abs(decide(0.15).components["tp_hit_threshold"] - 0.18) < 1e-9)
    trading.TP_HIT_VETO_BASE_RATE_RATIO = 0.10
    check("lowering the ratio lowers the threshold and vetoes less",
          decide(0.15).components["tp_hit_veto"] is False)
finally:
    trading.TP_HIT_VETO_BASE_RATE_RATIO = orig_floor

# The base rate itself moves the threshold, which is the whole point: the
# same probability can be fine in one market and poor in another.
check("a rarer label lowers the bar",
      decide(0.02, base_rate=0.01).components["tp_hit_veto"] is False)
check("...while the same probability fails against a common label",
      decide(0.02, base_rate=0.50).components["tp_hit_veto"] is True)
check("the veto stands down until enough labels exist",
      decide(1e-300, samples=config.TP_HIT_VETO_MIN_SAMPLES - 1)
      .components["tp_hit_veto"] is False)
check("...and engages once they do",
      decide(1e-300, samples=config.TP_HIT_VETO_MIN_SAMPLES)
      .components["tp_hit_veto"] is True)
check("a zero base rate can never veto",
      decide(0.0, base_rate=0.0).components["tp_hit_veto"] is False)

# ===========================================================================
print("\n[8] Scope: it can only ever REJECT, never accept")
# ===========================================================================
import inspect
src = inspect.getsource(trading.EntryEngineV2.evaluate)
check("the veto is ANDed into technical_ok, never ORed",
      "and not tp_hit_veto" in src)
check("technical_ok still requires the score threshold",
      "score >= active_threshold" in src)
check("the veto has its own rejection_reason branch", "tp_hit_veto:" in src)
check("it is surfaced in the entry-debug line", "tp_hit_veto={tp_hit_veto}" in src)

# a vetoed decision must carry no side and no size
d = decide(1e-50)
check("a vetoed decision does not enter", d.should_enter is False)
check("...and explains itself",
      "tp_hit_veto" in str(d.components.get("rejection_reason", "")),
      f"got {d.components.get('rejection_reason')}")
check("...and names the actual probability",
      "e-" in str(d.components.get("rejection_reason", "")).lower())

# the veto must not rescue a trade that other gates already reject
d_low_score = decide(1.0, momentum=0.0)
check("a healthy tp_hit_prob cannot rescue an otherwise-rejected entry",
      d_low_score.should_enter is False or d_low_score.components["tp_hit_veto"] is False)

# ===========================================================================
print("\n[9] Exits and open-position management are untouched")
# ===========================================================================
tsrc = open("trading.py").read()
mgmt = tsrc[tsrc.index("async def _manage_open_position"):]
mgmt = mgmt[:mgmt.index("\n    async def ", 10)] if "\n    async def " in mgmt[10:] else mgmt
check("_manage_open_position never reads the veto", "tp_hit_veto" not in mgmt)
check("the veto lives only in the entry engine",
      tsrc.count("tp_hit_veto") == tsrc[:tsrc.index("class MartingaleManager:")].count("tp_hit_veto"),
      "tp_hit_veto appears after the entry engine - check the scope")

# ===========================================================================
print("\n" + "=" * 74)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
