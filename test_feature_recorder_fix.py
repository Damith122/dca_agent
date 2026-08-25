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
    # 2026-08-25: observe() moved BEFORE the FLAT gate (see section [11]), so
    # it now runs ahead of evaluate() rather than after it. The decision is
    # attached separately by annotate_latest(), which is what must follow
    # evaluate() - that ordering is asserted in [11].
    check("the decision reaches the recorder via annotate_latest",
          body.index("self.last_entry_decision = decision")
          < body.index("self.feature_recorder.annotate_latest("))
    check("it is fed the same decision object the engine produced",
          "decision_fields(\n                    decision," in body
          or "decision_fields(decision," in body)
    check("it receives the orderflow snapshot", "orderflow=orderflow_now," in body)
    check("only completed shards are uploaded",
          "completed_shards()" in body)
    d2 = open("dca2.py").read()
    check("a sync loop is scheduled", "feature_log_loop(m)" in d2)
    check("rows are flushed on shutdown", "feature_recorder.flush()" in d2)
    check("startup states the recorder's configuration",
          "FEATURE RECORDER ON" in d2 and "feature recorder OFF" in d2)

    print("\n[9] DRY_RUN must still reach the entry evaluation")
    # Live regression: the recorder reported taken=0 on all four symbols
    # because initialize_sync() returned early under DRY_RUN without setting
    # position_sync_ready, and the entry path is gated on that flag. DRY_RUN
    # could therefore never evaluate an entry at all - it only managed
    # positions it could never open. The recorder made it visible.
    body_t = "\n".join(l for l in src.splitlines()
                        if not l.lstrip().startswith("#"))
    i_dry = body_t.index("if DRY_RUN:\n        manager.position_sync_ready = True")
    check("initialize_sync sets position_sync_ready before its DRY_RUN return",
          i_dry > 0)
    tail = body_t[i_dry:i_dry + 200]
    check("...and does so BEFORE returning",
          tail.index("position_sync_ready = True") < tail.index("return"))
    check("the entry path is still gated on the flag for live trading",
          "if not self.position_sync_ready:" in body_t)

    print("\n[10] Every name the hot paths reference actually resolves")
    # Live regression, and the worst bug of this change: the recorder hook was
    # written as `observe(time.time(), price, ...)` but on_price_tick() takes no
    # price argument and holds no local of that name. Every tick raised
    #   [market-ws:...] error processing message, skipping: name 'price' is not defined
    # immediately AFTER evaluate(), so the entry-debug line still printed and the
    # failure looked like "the recorder isn't recording". Under DRY_RUN nothing
    # was at risk, but on a live deploy it would have killed every step after
    # that line - order placement, DCA, stops, profit lock.
    #
    # A static scope check catches this class of bug across the whole file,
    # which importing the module cannot: a NameError inside a function only
    # surfaces when that line executes.
    import ast as _ast, builtins as _bi

    def _stores(node, stop_at_nested=True):
        """Names bound anywhere in this node, not descending into nested
        function bodies when asked (those have their own scope)."""
        out = set()
        for child in _ast.iter_child_nodes(node):
            if stop_at_nested and isinstance(
                    child, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                out.add(child.name)
                continue
            if isinstance(child, _ast.Name) and isinstance(child.ctx, _ast.Store):
                out.add(child.id)
            elif isinstance(child, _ast.ExceptHandler) and child.name:
                out.add(child.name)
            elif isinstance(child, (_ast.Import, _ast.ImportFrom)):
                for a in child.names:
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(child, _ast.Lambda):
                continue
            out |= _stores(child, stop_at_nested)
        return out

    def _params(fn):
        a = fn.args
        out = {x.arg for x in list(a.args) + list(a.kwonlyargs) + list(a.posonlyargs)}
        if a.vararg: out.add(a.vararg.arg)
        if a.kwarg:  out.add(a.kwarg.arg)
        return out

    def _loads(node):
        """Loaded names in this scope only, skipping nested function bodies.

        Annotations are skipped deliberately: every module here uses
        `from __future__ import annotations`, so annotations are strings and
        never evaluated. Counting them reported MartingaleManager in
        websocket.py as unresolved when it is only ever a type hint.
        """
        out = []
        for child in _ast.iter_child_nodes(node):
            if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                continue
            if isinstance(child, _ast.arg):
                continue                      # bare annotation on a parameter
            if isinstance(child, _ast.AnnAssign):
                # `x: SomeType = value` - the annotation is never evaluated
                # (local annotations never are, and PEP 563 makes the rest
                # strings), so only the target and value can reference names.
                for part in (child.target, child.value):
                    if part is None:
                        continue
                    if isinstance(part, _ast.Name) and isinstance(part.ctx, _ast.Load):
                        out.append((part.id, part.lineno))
                    out += _loads(part)
                continue
            if isinstance(child, _ast.Name) and isinstance(child.ctx, _ast.Load):
                out.append((child.id, child.lineno))
            out += _loads(child)
        return out

    def _scan_loads(fn):
        """Loads that really execute when the function runs: its body, plus
        decorators and argument defaults (evaluated at def time). Return and
        parameter annotations are excluded for the reason above."""
        out = []
        for stmt in fn.body:
            # A nested def/class is its own scope, checked separately by
            # _scan. Descending into it here would attribute ITS parameters
            # and locals to the enclosing function and report them unresolved.
            if isinstance(stmt, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                 _ast.ClassDef)):
                continue
            if isinstance(stmt, _ast.AnnAssign):
                # Same exclusion as in _loads, applied when the ANNOTATED
                # ASSIGNMENT is the statement itself rather than a child.
                for part in (stmt.target, stmt.value):
                    if part is None:
                        continue
                    if isinstance(part, _ast.Name) and isinstance(part.ctx, _ast.Load):
                        out.append((part.id, part.lineno))
                    out += _loads(part)
                continue
            out += _loads(stmt)
            if isinstance(stmt, _ast.Name) and isinstance(stmt.ctx, _ast.Load):
                out.append((stmt.id, stmt.lineno))
        for d in list(fn.decorator_list) + [x for x in
                (list(fn.args.defaults) + [k for k in fn.args.kw_defaults if k])]:
            out += _loads(d)
            if isinstance(d, _ast.Name) and isinstance(d.ctx, _ast.Load):
                out.append((d.id, d.lineno))
        return out

    def _scan(scope_node, inherited, mod_names, problems):
        """Recursive scope check. A nested function inherits everything bound
        in its enclosing scopes - closures are exactly why the first version of
        this check produced false positives on _side_stats and _cache_locally."""
        for fn in _ast.iter_child_nodes(scope_node):
            if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                continue
            if isinstance(fn, _ast.ClassDef):
                _scan(fn, inherited | {fn.name}, mod_names, problems)
                continue
            bound = inherited | _params(fn) | _stores(fn) | {fn.name}
            for name, line in _scan_loads(fn):
                if (name not in bound and name not in mod_names
                        and not hasattr(_bi, name)):
                    problems.append(f"{fn.name}:{name}@{line}")
            _scan(fn, bound, mod_names, problems)

    def _module_names(tree):
        names = set(_stores(tree, stop_at_nested=True))
        for n in _ast.walk(tree):
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, (_ast.Import, _ast.ImportFrom)):
                for a in n.names:
                    names.add((a.asname or a.name).split(".")[0])
        return names

    for mod in ("trading.py", "dca2.py", "feature_recorder.py", "websocket.py",
                "config.py", "brain.py", "exchange.py", "github_sync.py"):
        tree = _ast.parse(open(mod).read())
        problems = []
        _scan(tree, set(), _module_names(tree), problems)
        check(f"{mod}: every referenced name resolves",
              not problems, "; ".join(problems[:6]))

    check("the recorder is passed self.current_price, not an undefined local",
          "_recorder_tick_ts, self.current_price," in src)

    print("\n[11] Recording must not stop when a position opens")
    # Live regression: observe() sat inside `if self.position.status == "FLAT"`,
    # so sampling stopped the moment a symbol opened a position. Under DRY_RUN
    # a simulated entry never gets a fill, so every symbol parked in ENTERING
    # and never recorded again - 24.7h of runtime yielded 3.2h of data.
    # Scoped to on_price_tick: trading.py contains several FLAT gates and a
    # file-wide search finds the wrong one.
    _t = _ast.parse(src)
    _tick = next(n for n in _ast.walk(_t)
                 if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                 and n.name == "on_price_tick")
    obs_line = ann_line = flat_line = None
    for node in _ast.walk(_tick):
        if isinstance(node, _ast.Attribute) and node.attr == "observe":
            obs_line = node.lineno
        elif isinstance(node, _ast.Attribute) and node.attr == "annotate_latest":
            ann_line = node.lineno
        elif (isinstance(node, _ast.If) and flat_line is None
              and "status == 'FLAT'" in _ast.unparse(node.test).replace('"', "'")):
            flat_line = node.lineno
    check("observe() runs BEFORE the FLAT gate", obs_line < flat_line,
          f"observe@{obs_line} flat@{flat_line}")
    check("the decision is annotated INSIDE the flat branch", ann_line > flat_line,
          f"annotate@{ann_line} flat@{flat_line}")
    check("observe() no longer receives a decision at the pre-gate site",
          "decision=decision," not in src)

    # annotate_latest must bind only to a sample taken on the SAME tick.
    r4 = new_rec(tmp, interval_sec=60.0, horizons_sec=[1])
    r4.observe(T0, 100.0, regime=R(), conf=C())
    r4.annotate_latest(T0, **r4.decision_fields(
        D(enter=True, side="LONG", score=0.9, comp={"threshold": 0.75}), FLOW, 1.5))
    check("a decision annotates the sample from this tick",
          r4._pending[-1]["row"]["score"] == 0.9)
    check("...and marks it as decided", r4._pending[-1]["row"]["decided"] is True)
    r4.annotate_latest(T0 + 30.0, **r4.decision_fields(
        D(enter=False, score=0.1, comp={}), FLOW, 1.5))
    check("a later tick with no fresh sample does NOT overwrite an older row",
          r4._pending[-1]["row"]["score"] == 0.9)

    r5 = new_rec(tmp, interval_sec=60.0, horizons_sec=[1])
    r5.observe(T0, 100.0, regime=R(), conf=C(),
               extra={"pos_status": "OPEN", "dry": True})
    check("a sample taken while a position is OPEN is still recorded",
          r5.stats()["taken"] == 1)
    check("...and is marked undecided so it is separable in analysis",
          r5._pending[-1]["row"]["decided"] is False)
    check("...and carries the position status",
          r5._pending[-1]["row"]["pos_status"] == "OPEN")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
