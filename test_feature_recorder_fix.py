#!/usr/bin/env python3
"""
Tests for the 2026-08-24 feature recorder.

The recorder exists because 58 live trades were not enough to find an entry
edge, and because those 58 are only the setups the current rule ACCEPTED -
nothing was known about the rejected ones. It captures every evaluation with
its realised forward return so entry rules can be tested offline.

Two properties matter most and are pinned hardest here:
  1. It must never disturb trading. Every method swallows its own exceptions.
  2. It must record rejected setups, not just accepted ones - that is the
     whole reason it exists.

No network call and no real order is made anywhere in this file.
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import json
import shutil
import sys
import tempfile

import config
from feature_recorder import FeatureRecorder

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


class D:      # stand-in for EntryDecision
    def __init__(self, enter=False, side=None, score=0.5, comp=None):
        self.should_enter = enter; self.side = side; self.score = score
        self.components = comp or {}


class R:
    regime = "WEAK_TREND"; atr_pct = 0.003; atr_ratio = 1.2; trend_slope = 0.0004


class C:
    confidence_score = 0.42; trend_confidence = 1.0; trend_direction = "LONG"
    success_probability = 0.5; tp_hit_probability = 0.0012; noise_probability = 0.3
    risk_score = 0.13; quality_pred = 0.02


FLOW = {"data_available": True, "imbalance": -0.35, "trade_delta": -1200.0,
        "book_support": False, "flow_aligned": True, "blocked": False}
READY = {"tp_hit": "READY", "trend": "READY"}


_seq = [0]


def new_rec(tmp, **kw):
    """Each recorder gets its own base filename. Sharing one would put every
    recorder's rows in the same hourly shard, and a test reading rows[0] would
    silently pick up an earlier recorder's row."""
    kw.setdefault("enabled", True)
    kw.setdefault("interval_sec", 1.0)
    kw.setdefault("horizons_sec", [1, 2])
    _seq[0] += 1
    path = os.path.join(tmp, f"feature_log_{_seq[0]}.jsonl")
    return FeatureRecorder("SOLUSDT", path, **kw)


tmp = tempfile.mkdtemp()
try:
    print("\n[1] Disabled by default and inert when off")
    check("config default is OFF", config.FEATURE_RECORDER_ENABLED is False)
    off = new_rec(tmp, enabled=False)
    for i in range(50):
        off.observe(1000.0 + i, 100.0 + i, decision=D(), regime=R(), conf=C())
    check("a disabled recorder takes no samples", off.stats()["taken"] == 0)
    check("...and writes nothing", off.flush() == 0)

    print("\n[2] Records REJECTED setups, not just accepted ones")
    r = new_rec(tmp)
    t = 1000.0
    r.observe(t, 100.0, decision=D(enter=False, score=0.41,
              comp={"rejection_reason": "score 0.41 below threshold 0.75",
                    "threshold": 0.75}),
              regime=R(), conf=C(), orderflow=FLOW, brain_readiness=READY)
    check("a rejected evaluation is sampled", r.stats()["taken"] == 1)
    r.observe(t + 1.0, 101.0, decision=D(enter=True, side="LONG", score=0.81,
              comp={"threshold": 0.75}), regime=R(), conf=C(), orderflow=FLOW)
    check("an accepted evaluation is sampled too", r.stats()["taken"] == 2)

    print("\n[3] Forward returns and excursions are realised, not predicted")
    # Epoch-scale timestamps, as in production. Starting at 0.0 would mean the
    # first observe cannot sample, since _last_sample_ts also starts at 0.0.
    T0 = 1_700_000_000.0
    r2 = new_rec(tmp, interval_sec=60.0, horizons_sec=[1, 2])
    r2.observe(T0 + 0.0, 100.0, decision=D(), regime=R(), conf=C())  # sampled
    r2.observe(T0 + 0.5, 102.0, decision=D(), regime=R(), conf=C())  # +2% peak
    r2.observe(T0 + 1.2, 99.0, decision=D(), regime=R(), conf=C())   # -1% trough
    r2.observe(T0 + 2.5, 101.0, decision=D(), regime=R(), conf=C())  # matures it
    n = r2.flush(T0 + 2.5)
    check("a matured sample is written", n >= 1, f"wrote {n}")
    rows = [json.loads(l) for l in
            open(r2.current_shard_path(T0 + 2.5), encoding="utf-8")]
    row = rows[0]
    check("r1 horizon captured", row.get("r1") is not None)
    check("r2 horizon captured", row.get("r2") is not None)
    check("MFE reflects the +2% peak", abs(row["mfe_long"] - 0.02) < 1e-6,
          f"got {row['mfe_long']}")
    check("MAE reflects the -1% trough", abs(row["mae_long"] + 0.01) < 1e-6,
          f"got {row['mae_long']}")
    check("up_won is derived from MFE vs MAE", row["up_won"] is True)

    print("\n[4] The row carries what offline analysis needs")
    r3 = new_rec(tmp, interval_sec=60.0, horizons_sec=[1])
    r3.observe(T0 + 0.0, 100.0, features=[0.1] * 34, regime=R(), conf=C(),
               decision=D(enter=False, score=0.41,
                          comp={"threshold": 0.75, "volume_confirmation": 0.8,
                                "momentum": 1.0, "regime_fit": 0.5,
                                "momentum_aligned": True,
                                "rejection_reason": "score too low"}),
               orderflow=FLOW, brain_readiness=READY,
               extra={"vol_z": 1.5, "dry": True})
    r3.observe(T0 + 3.0, 100.0, decision=D(), regime=R(), conf=C())
    r3.flush(T0 + 3.0)
    row = [json.loads(l) for l in
           open(r3.current_shard_path(T0 + 3.0), encoding="utf-8")][0]
    for key, why in [("f", "the raw feature vector"),
                     ("regime", "regime"), ("atr_pct", "volatility"),
                     ("conf", "brain confidence"), ("tp_hit_p", "tp_hit head"),
                     ("risk", "risk score"), ("score", "composite score"),
                     ("thr", "the threshold it was judged against"),
                     ("reason", "why it was rejected"),
                     ("of_imb", "orderflow imbalance"),
                     ("of_delta", "trade delta"),
                     ("c_mom", "momentum component"),
                     ("rdy", "head readiness"),
                     ("vol_z", "volume z"), ("dry", "dry-run flag")]:
        check(f"row carries {why}", key in row)
    check("the feature vector is intact", len(row["f"]) == 34)
    check("orderflow is captured, which klines could never reconstruct",
          row["of_imb"] is not None and row["of_delta"] is not None)

    print("\n[5] It cannot disturb trading")
    bad = new_rec(tmp)
    class Exploding:
        @property
        def components(self): raise RuntimeError("boom")
        should_enter = False
    bad.observe(T0, 100.0, decision=Exploding(), regime=R(), conf=C())
    check("an exploding decision object does not propagate", True)
    bad.observe(T0, None, decision=D(), regime=R(), conf=C())
    bad.observe(T0, -5.0, decision=D(), regime=R(), conf=C())
    check("a missing or negative price is ignored, not raised", True)
    # A plain missing directory is not enough here - the process runs as root
    # and makedirs would simply create it. Putting a FILE where the directory
    # must be makes the write genuinely impossible.
    blocker = os.path.join(tmp, "blocker")
    with open(blocker, "w") as fh:
        fh.write("not a directory")
    broken = FeatureRecorder("SOLUSDT", os.path.join(blocker, "f.jsonl"),
                             enabled=True, interval_sec=0.5, horizons_sec=[0.5])
    broken.observe(T0, 100.0, decision=D(), regime=R(), conf=C())
    broken.observe(T0 + 1.0, 101.0, decision=D(), regime=R(), conf=C())
    check("an unwritable path returns 0 rather than raising",
          broken.flush(T0 + 1.0) == 0)

    print("\n[6] Volume control: sampling interval and shard rotation")
    slow = new_rec(tmp, interval_sec=10.0, horizons_sec=[1])
    for i in range(100):                      # 100 ticks across 9.9s
        slow.observe(T0 + i * 0.1, 100.0, decision=D(), regime=R(), conf=C())
    check("interval throttles a 3.5/s decision loop to one sample",
          slow.stats()["taken"] == 1, f"took {slow.stats()['taken']}")
    sh = new_rec(tmp, shard_sec=3600.0)
    a = sh.current_shard_path(T0)
    b = sh.current_shard_path(T0 + 3700.0)
    check("shards rotate on the configured interval", a != b)
    check("shard names sort chronologically",
          os.path.basename(a) < os.path.basename(b))
    check("the active shard is excluded from the upload set",
          os.path.basename(sh.current_shard_path())
          not in [os.path.basename(x) for x in sh.completed_shards()])

    print("\n[7] The pending buffer is bounded")
    # interval_sec is floored at 0.5s (see below), so 200 ticks over 2s could
    # only ever produce 4 samples. Span enough time to actually overflow 20.
    cap = new_rec(tmp, interval_sec=0.5, horizons_sec=[9999], max_pending=20)
    for i in range(2000):
        cap.observe(T0 + i * 0.05, 100.0, decision=D(), regime=R(), conf=C())
    check("pending never exceeds max_pending", len(cap._pending) <= 20,
          f"{len(cap._pending)}")
    check("...and the overflow is counted, not silent",
          cap.stats()["dropped"] > 0, f"dropped {cap.stats()['dropped']}")

    # A misconfigured interval must not be able to create a firehose: the
    # decision loop runs ~3.5x/s per symbol, and recording every cycle would
    # be ~30 MB/hour, which is what the sampling design exists to avoid.
    fast = new_rec(tmp, interval_sec=0.0)
    check("interval_sec is floored at 0.5s so it cannot become a firehose",
          fast.interval_sec == 0.5, f"got {fast.interval_sec}")
    check("shard_sec is floored too", new_rec(tmp, shard_sec=1.0).shard_sec == 60.0)

    print("\n[8] Wiring into the engine")
    src = open("trading.py").read()
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    check("the recorder is observed right after evaluate()",
          body.index("self.last_entry_decision = decision")
          < body.index("self.feature_recorder.observe("))
    check("it is fed the same decision object the engine produced",
          "decision=decision," in body)
    check("it receives the orderflow snapshot", "orderflow=orderflow_now," in body)
    check("only completed shards are uploaded",
          "completed_shards()" in body)
    d2 = open("dca2.py").read()
    check("a sync loop is scheduled", "feature_log_loop(m)" in d2)
    check("rows are flushed on shutdown", "feature_recorder.flush()" in d2)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
