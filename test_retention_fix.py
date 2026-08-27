#!/usr/bin/env python3
"""
Tests for the 2026-08-27 feature-log retention sweep.

The recorder writes ~400 KB per symbol per hour and never deleted anything,
so a long run fills the container's disk allowance while the bot is holding
real positions. Retention fixes that, but a tidying routine that deletes
recorded data is a worse failure than the problem it solves - a full disk is
loud, a silently deleted dataset is not.

So the property pinned hardest here is the negative one: a shard that has
not been CONFIRMED uploaded is never deleted, not by the age rule, not by
the budget sweep, not when the budget cannot otherwise be met.
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import shutil
import sys
import tempfile

import retention

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


NOW = 1_800_000_000.0
KB = 1024


def mkshards(tmp, spec):
    """spec: [(name, size_bytes, age_hours)] -> list of paths, oldest first."""
    out = []
    for name, size, age in spec:
        p = os.path.join(tmp, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        os.utime(p, (NOW - age * 3600, NOW - age * 3600))
        out.append(p)
    return out


tmp = tempfile.mkdtemp(prefix="retention_test_")
try:
    print("[1] The age rule only touches confirmed-uploaded shards")
    a, b, c = mkshards(tmp, [("s_a.jsonl", 10 * KB, 10.0),
                             ("s_b.jsonl", 10 * KB, 9.0),
                             ("s_c.jsonl", 10 * KB, 1.0)])
    rep = retention.prune_shards([a, b, c], uploaded={a, c}, retain_hours=6.0,
                                 max_bytes=0, now=NOW)
    check("an uploaded shard past the age limit is deleted", a in rep["deleted"])
    check("an UNUPLOADED shard past the age limit is kept", b not in rep["deleted"])
    check("...and still exists on disk", os.path.exists(b))
    check("an uploaded shard inside the age limit is kept", c not in rep["deleted"])
    check("freed bytes are counted", rep["freed_bytes"] == 10 * KB, str(rep["freed_bytes"]))

    print("\n[2] The budget sweep never reaches for unuploaded shards")
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    a, b, c = mkshards(tmp, [("s_a.jsonl", 50 * KB, 5.0),
                             ("s_b.jsonl", 50 * KB, 4.0),
                             ("s_c.jsonl", 50 * KB, 3.0)])
    # Budget is 60 KB against 150 KB on disk, but only `a` is uploaded, so the
    # sweep can free 50 KB and must then stop rather than take b or c.
    rep = retention.prune_shards([a, b, c], uploaded={a}, retain_hours=0,
                                 max_bytes=60 * KB, now=NOW)
    check("the sweep deletes what it may", rep["deleted"] == [a], str(rep["deleted"]))
    check("unuploaded shards survive an unmet budget",
          os.path.exists(b) and os.path.exists(c))
    check("...and the shortfall is reported, not hidden", rep["over_budget"] is True)
    check("...naming how many shards blocked it", rep["blocked_unuploaded"] == 2,
          str(rep["blocked_unuploaded"]))
    check("describe() shouts when still over budget",
          "STILL OVER BUDGET" in retention.describe(rep), retention.describe(rep))

    print("\n[3] The budget sweep takes the OLDEST first")
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    a, b, c = mkshards(tmp, [("s_a.jsonl", 50 * KB, 9.0),
                             ("s_b.jsonl", 50 * KB, 5.0),
                             ("s_c.jsonl", 50 * KB, 1.0)])
    rep = retention.prune_shards([c, a, b], uploaded={a, b, c}, retain_hours=0,
                                 max_bytes=60 * KB, now=NOW)
    check("oldest two are dropped, newest kept", rep["deleted"] == [a, b],
          str(rep["deleted"]))
    check("it stops as soon as the budget is met", os.path.exists(c))
    check("no shortfall is reported", rep["over_budget"] is False)

    print("\n[4] Both rules off means nothing is ever deleted")
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    a, b = mkshards(tmp, [("s_a.jsonl", 90 * KB, 99.0), ("s_b.jsonl", 90 * KB, 98.0)])
    rep = retention.prune_shards([a, b], uploaded={a, b}, retain_hours=0,
                                 max_bytes=0, now=NOW)
    check("retain_hours=0 and max_bytes=0 delete nothing", rep["deleted"] == [])
    check("...and describe() stays quiet", retention.describe(rep) == "",
          retention.describe(rep))

    print("\n[5] Filesystem trouble degrades quietly")
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    a, = mkshards(tmp, [("s_a.jsonl", 10 * KB, 10.0)])
    ghost = os.path.join(tmp, "s_ghost.jsonl")
    rep = retention.prune_shards([a, ghost], uploaded={a, ghost}, retain_hours=6.0,
                                 max_bytes=0, now=NOW)
    check("a shard that vanished mid-sweep is skipped, not fatal",
          rep["deleted"] == [a], str(rep["deleted"]))

    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    a, = mkshards(tmp, [("s_a.jsonl", 10 * KB, 10.0)])

    def _boom(path):
        raise OSError("read-only filesystem")

    rep = retention.prune_shards([a], uploaded={a}, retain_hours=6.0, max_bytes=0,
                                 now=NOW, unlink=_boom)
    check("an unlink that fails is not counted as deleted", rep["deleted"] == [])
    check("...and the file is still there", os.path.exists(a))
    check("...and its bytes still count toward the budget",
          rep["retained_bytes"] == 10 * KB, str(rep["retained_bytes"]))

    print("\n[6] Wired into the sync path")
    import trading as _trading

    class _Rec:
        enabled = True

        def __init__(self, shards):
            self._shards = shards

        def completed_shards(self):
            return list(self._shards)

    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    # prune_feature_log_disk() reads the real clock, so these mtimes must be
    # relative to it rather than the frozen NOW used above.
    import time as _time
    old, new = (os.path.join(tmp, "s_old.jsonl"), os.path.join(tmp, "s_new.jsonl"))
    for _p, _age in ((old, 10.0), (new, 1.0)):
        with open(_p, "wb") as _f:
            _f.write(b"x" * (10 * KB))
        os.utime(_p, (_time.time() - _age * 3600, _time.time() - _age * 3600))

    class _Mgr:
        pass

    mgr = _Mgr()
    mgr.feature_recorder = _Rec([old, new])
    mgr._last_synced_csv_hash = {old: "done", new: "done"}
    _trading.MartingaleManager.prune_feature_log_disk(mgr)
    check("the uploaded, aged shard is pruned through the manager",
          not os.path.exists(old))
    check("the recent shard survives", os.path.exists(new))
    check("the deleted shard's marker is dropped so the dict cannot grow forever",
          old not in mgr._last_synced_csv_hash and new in mgr._last_synced_csv_hash,
          str(mgr._last_synced_csv_hash))

    # The ACTIVE shard is never offered to retention: completed_shards() excludes
    # it. Pin that here too, because retention's safety depends on it.
    from feature_recorder import FeatureRecorder
    rec = FeatureRecorder("SOLUSDT", os.path.join(tmp, "feat.jsonl"),
                          enabled=True, shard_sec=3600.0)
    active = rec.current_shard_path()
    open(active, "w").close()
    check("the active shard is not listed as completed",
          active not in rec.completed_shards(), active)

    print("\n[7] Retention never interrupts trading")
    class _Angry:
        enabled = True

        def completed_shards(self):
            raise RuntimeError("disk on fire")

    mgr2 = _Mgr()
    mgr2.feature_recorder = _Angry()
    mgr2._last_synced_csv_hash = {}
    err = None
    try:
        _trading.MartingaleManager.prune_feature_log_disk(mgr2)
    except Exception as exc:  # noqa: BLE001
        err = exc
    check("an exploding sweep is swallowed", err is None, repr(err))

    class _Off:
        enabled = False

        def completed_shards(self):
            raise AssertionError("must not be consulted when the recorder is off")

    mgr3 = _Mgr()
    mgr3.feature_recorder = _Off()
    mgr3._last_synced_csv_hash = {}
    err = None
    try:
        _trading.MartingaleManager.prune_feature_log_disk(mgr3)
    except Exception as exc:  # noqa: BLE001
        err = exc
    check("a disabled recorder short-circuits before any filesystem work",
          err is None, repr(err))

    print("\n[8] Defaults are safe")
    import config
    check("retention defaults ON", config.FEATURE_LOG_RETENTION_ENABLED is True)
    check("the age limit is longer than one shard, so nothing is deleted "
          "in the hour it was written",
          config.FEATURE_LOG_RETAIN_LOCAL_HOURS * 3600
          > config.FEATURE_RECORDER_SHARD_SEC,
          f"{config.FEATURE_LOG_RETAIN_LOCAL_HOURS}h vs "
          f"{config.FEATURE_RECORDER_SHARD_SEC}s")
    check("the age limit also outlasts the longest forward horizon",
          config.FEATURE_LOG_RETAIN_LOCAL_HOURS * 3600
          > max(config.feature_recorder_horizons()))
    check("the budget leaves room for a multi-day run",
          config.FEATURE_LOG_MAX_LOCAL_MB >= 128)
    check("malformed env values fall back to the defaults, not a crash",
          config._env_float("NOT_A_REAL_VAR_XYZ", 6.0) == 6.0)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
