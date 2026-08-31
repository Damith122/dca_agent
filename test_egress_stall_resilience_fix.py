"""Offline regression tests for the 2026-08-20 egress-stall fixes (N1, N2).

WHAT HAPPENED
--------------------------------------------------------------------------
Deployment b9f4b6de ran 7h31m with zero restarts and traded nothing (the
SIDEWAYS gates held correctly on a flat tape), but the deploy log carried a
recurring background failure:

  21 x  [risk] position risk poll failed: BinanceApiError: HTTP 400:
        {'code': -1021, 'msg': 'Timestamp for this request is outside of
        the recvWindow.'}
 ~35 x  [risk|balance|funding] ... failed: TimeoutError
 ~30 x  [market-ws:*] disconnected (sent 1011 keepalive ping timeout)

The -1021s and the TimeoutErrors clustered in the SAME minutes, and during
08:07-08:22 all four websockets (bookTicker, depth, aggTrade, user-ws) went
down together. Binance does not drop four independent sockets in lockstep -
the container's own egress was stalling. 02:13-06:20 was near-spotless,
which also rules out a monotonically drifting clock as the whole story.

MECHANISM
--------------------------------------------------------------------------
_sign() stamps `timestamp` at signing time; the request then has to reach
Binance's gateway. Spend longer than recvWindow in flight -> -1021. Spend
longer than the 10s client timeout -> TimeoutError. One event, two
severities. recvWindow was a hardcoded 5000ms, the clock offset was computed
exactly once in start() and then trusted for the whole process lifetime, and
_request() had no retry at all - so the bot failed closed on every stall.

THE FIXES
--------------------------------------------------------------------------
  N1a recvWindow 5000 -> RECV_WINDOW_MS (10000), giving real headroom for a
      stall. setdefault() semantics unchanged - an explicit per-call value
      still wins.
  N1b _maybe_resync_server_time() refreshes the offset every
      TIME_RESYNC_INTERVAL_SEC (1800s), called from _request() before
      signing. Gated on `signed` so the unsigned /fapi/v1/time call inside
      _sync_server_time() cannot recurse. Never raises; a failed resync
      leaves the old offset and the caller's request proceeds. Concurrent
      pollers are collapsed to one call by a lock + re-check.
  N1c ONE bounded retry for -1021, opt-in per call site. Only the seven
      read-only signed GETs opt in. Every mutating endpoint (place_order,
      cancel_order, place_algo_order, cancel_algo_order, set_leverage,
      set_margin_type) is untouched: replaying a request whose execution
      result is ambiguous could double-fill.
  N2  github_sync.py's three bare `{e}` log sites now route through
      _exc_text(), the same helper the 2026-08-19 F3 pass applied to
      dca2.py/trading.py. Production showed the blank-tailed line twice:
        [brain-sync] GitHub push failed for brain_LIVE_SOLUSDT.pkl (bot keeps trading):

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_egress_stall_resilience_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import asyncio
import inspect
import io
import sys
import time

import aiohttp

import exchange
import github_sync
from exchange import BinanceApiError, RestClient


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


# ---------------------------------------------------------------------------
# A fake RestClient whose _send_once is scripted, so no socket is ever opened.
# ---------------------------------------------------------------------------
def make_client():
    c = RestClient("k", "s", "https://example.invalid")
    c.session = object()          # never touched: _send_once is stubbed out
    return c


def stale_ts_error():
    return BinanceApiError(
        400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}
    )


def script(client, outcomes):
    """Replace _send_once with a scripted sequence; record every call.

    The stub signs exactly where the real _send_once does - inside itself,
    per attempt - so a retry genuinely re-signs rather than replaying the
    params the first attempt already stamped. That is the property test [6]
    asserts on, so the stub has to reproduce it faithfully."""
    calls = []

    async def _send_once(method, path, params, signed):
        sent = client._sign(params) if signed else dict(params)
        calls.append({"method": method, "path": path, "params": sent, "signed": signed})
        out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(out, BaseException):
            raise out
        return out

    client._send_once = _send_once
    return calls


def freeze_time_sync(client, offset_ms=0, synced_ts=None):
    client._time_offset_ms = offset_ms
    client._time_synced_ts = time.time() if synced_ts is None else synced_ts


# ===========================================================================
print("\n[1] N1a - recvWindow is 10000ms, and an explicit override still wins")
# ===========================================================================
c = make_client()
signed = c._sign({"symbol": "SOLUSDT"})
check("RECV_WINDOW_MS constant is 10000", exchange.RECV_WINDOW_MS == 10000,
      f"got {exchange.RECV_WINDOW_MS}")
check("_sign() applies recvWindow=10000", signed.get("recvWindow") == 10000,
      f"got {signed.get('recvWindow')}")
check("_sign() still honours an explicit recvWindow",
      c._sign({"symbol": "SOLUSDT", "recvWindow": 2000}).get("recvWindow") == 2000)
check("_sign() still signs and does not mutate the caller's dict",
      "signature" in signed and "timestamp" in signed)
_orig = {"symbol": "SOLUSDT"}
c._sign(_orig)
check("_sign() leaves the original params untouched (retry can re-sign)",
      _orig == {"symbol": "SOLUSDT"}, f"got {_orig}")

# ===========================================================================
print("\n[2] N1b - periodic resync fires when stale, stays quiet when fresh")
# ===========================================================================
c = make_client()
freeze_time_sync(c, offset_ms=5, synced_ts=time.time())          # just synced
hits = []

async def fake_sync():
    hits.append(time.time())
    c._time_synced_ts = time.time()

c._sync_server_time = fake_sync
asyncio.run(c._maybe_resync_server_time())
check("fresh offset -> no resync", len(hits) == 0, f"{len(hits)} call(s)")

# age the offset past the interval
c._time_synced_ts = time.time() - (exchange.TIME_RESYNC_INTERVAL_SEC + 1)
asyncio.run(c._maybe_resync_server_time())
check("stale offset -> resync fires", len(hits) == 1, f"{len(hits)} call(s)")

# immediately afterwards it is fresh again
asyncio.run(c._maybe_resync_server_time())
check("resync makes the offset fresh again (no second call)", len(hits) == 1,
      f"{len(hits)} call(s)")
check("resync interval is 30 minutes", exchange.TIME_RESYNC_INTERVAL_SEC == 1800.0,
      f"got {exchange.TIME_RESYNC_INTERVAL_SEC}")

# A never-synced client must NOT be treated as overdue: doing so would inject
# an unsigned /fapi/v1/time request ahead of the caller's first signed request.
# start() establishes the baseline unconditionally, and the -1021 force path
# still corrects an offset that is actually wrong.
c = make_client()
never = []

async def never_sync():
    never.append(1)

c._sync_server_time = never_sync
c._time_synced_ts = 0.0
asyncio.run(c._maybe_resync_server_time())
check("a never-synced client does not self-trigger a periodic resync",
      len(never) == 0, f"{len(never)} call(s)")
asyncio.run(c._maybe_resync_server_time(force=True))
check("...but force=True still syncs it", len(never) == 1, f"{len(never)} call(s)")

# and end-to-end: a first signed call on a fresh client sends ONE request
c = make_client()
c._time_synced_ts = 0.0
calls = script(c, [[{"asset": "USDT"}]])
asyncio.run(c.get_balance())
check("a fresh client's first signed call sends exactly one request",
      len(calls) == 1 and calls[0]["path"] == "/fapi/v2/balance", f"{calls}")

# ===========================================================================
print("\n[3] N1b - a FAILING resync never raises and never storms")
# ===========================================================================
c = make_client()
attempts = []

async def failing_sync():
    attempts.append(1)
    raise asyncio.TimeoutError()

c._sync_server_time = failing_sync
c._time_offset_ms = 1234
c._time_synced_ts = time.time() - (exchange.TIME_RESYNC_INTERVAL_SEC + 1)

try:
    asyncio.run(c._maybe_resync_server_time())
    raised = False
except Exception:
    raised = True
check("a failing resync does not propagate", not raised)
check("the previous offset survives a failed resync", c._time_offset_ms == 1234,
      f"got {c._time_offset_ms}")
check("one failed attempt was made", len(attempts) == 1, f"{len(attempts)}")

# hammer it: the min-retry floor must suppress the follow-ups
for _ in range(10):
    asyncio.run(c._maybe_resync_server_time())
check("repeat calls inside the retry floor do NOT storm /fapi/v1/time",
      len(attempts) == 1, f"{len(attempts)} attempt(s) - expected 1")

# ===========================================================================
print("\n[4] N1b - concurrent pollers collapse into ONE resync call")
# ===========================================================================
c = make_client()
slow_hits = []

async def slow_sync():
    slow_hits.append(1)
    await asyncio.sleep(0.05)         # hold the lock while others pile up
    c._time_synced_ts = time.time()

c._sync_server_time = slow_sync
c._time_synced_ts = time.time() - (exchange.TIME_RESYNC_INTERVAL_SEC + 1)

async def stampede():
    await asyncio.gather(*[c._maybe_resync_server_time() for _ in range(8)])

asyncio.run(stampede())
check("8 concurrent pollers produced exactly 1 /fapi/v1/time call",
      len(slow_hits) == 1, f"{len(slow_hits)} call(s)")

# ===========================================================================
print("\n[5] N1b - the resync is gated on `signed` (no recursion)")
# ===========================================================================
c = make_client()
c._time_synced_ts = time.time() - (exchange.TIME_RESYNC_INTERVAL_SEC + 1)
resyncs = []

async def counting_resync(force=False):
    resyncs.append(force)

c._maybe_resync_server_time = counting_resync
script(c, [{"serverTime": 1}])
asyncio.run(c._request("GET", "/fapi/v1/time"))
check("an UNSIGNED request never triggers a resync (breaks the recursion)",
      len(resyncs) == 0, f"{len(resyncs)} resync(s)")

script(c, [{"ok": 1}])
asyncio.run(c._request("GET", "/fapi/v2/balance", signed=True))
check("a SIGNED request does consult the resync", len(resyncs) == 1,
      f"{len(resyncs)} resync(s)")

# the real _sync_server_time must stamp _time_synced_ts
c2 = make_client()
script(c2, [{"serverTime": int(time.time() * 1000) + 250}])
c2._time_synced_ts = 0.0
asyncio.run(c2._sync_server_time())
check("_sync_server_time() stamps _time_synced_ts", c2._time_synced_ts > 0)
check("_sync_server_time() still computes the offset",
      200 <= c2._time_offset_ms <= 300, f"got {c2._time_offset_ms}")

# ===========================================================================
print("\n[6] N1c - a read-only GET retries ONCE on -1021 and succeeds")
# ===========================================================================
c = make_client()
freeze_time_sync(c)
forced = []

async def note_force(force=False):
    forced.append(force)

async def resync_shifts_offset(force=False):
    forced.append(force)
    c._time_offset_ms += 7000       # the sync discovers we were 7s behind
    c._time_synced_ts = time.time()

c._maybe_resync_server_time = resync_shifts_offset
calls = script(c, [stale_ts_error(), [{"asset": "USDT", "availableBalance": "19.57"}]])
out = asyncio.run(c.get_balance())
check("get_balance() survives one -1021", out and out[0]["asset"] == "USDT")
check("it sent exactly 2 requests (1 retry, not a loop)", len(calls) == 2,
      f"{len(calls)}")
check("the retry forced a fresh time sync", forced[-1] is True, f"{forced}")
# The whole point of the retry: attempt 2 must carry a timestamp derived from
# the CORRECTED offset, not a replay of the one Binance just rejected.
t0 = calls[0]["params"]["timestamp"]
t1 = calls[1]["params"]["timestamp"]
check("the retry re-signed with a timestamp off the corrected offset",
      t1 - t0 >= 7000, f"delta={t1 - t0}ms")
check("the retry carries its own fresh signature",
      calls[0]["params"]["signature"] != calls[1]["params"]["signature"])

# ===========================================================================
print("\n[7] N1c - the retry is BOUNDED: a second -1021 propagates")
# ===========================================================================
c = make_client()
freeze_time_sync(c)
c._maybe_resync_server_time = note_force
calls = script(c, [stale_ts_error(), stale_ts_error(), [{"asset": "USDT"}]])
try:
    asyncio.run(c.get_balance())
    raised = None
except BinanceApiError as e:
    raised = e
check("a second consecutive -1021 propagates", raised is not None and raised.code == -1021)
check("it stopped at 2 attempts - there is no third", len(calls) == 2, f"{len(calls)}")

# ===========================================================================
print("\n[8] N1c - MUTATING endpoints are never replayed")
# ===========================================================================
async def never_resync(force=False):
    pass

for label, coro_factory in [
    ("place_order", lambda cl: cl.place_order(symbol="SOLUSDT", side="BUY", type="MARKET", quantity=1)),
    ("cancel_order", lambda cl: cl.cancel_order("SOLUSDT", 1)),
    ("place_algo_order", lambda cl: cl.place_algo_order(symbol="SOLUSDT", side="SELL")),
    ("set_leverage", lambda cl: cl.set_leverage("SOLUSDT", 20)),
]:
    c = make_client()
    freeze_time_sync(c)
    c._maybe_resync_server_time = never_resync
    calls = script(c, [stale_ts_error(), {"orderId": 1}])
    try:
        asyncio.run(coro_factory(c))
        raised = None
    except BinanceApiError as e:
        raised = e
    check(f"{label}() raises on -1021 instead of replaying",
          raised is not None and raised.code == -1021)
    check(f"{label}() sent exactly ONE request", len(calls) == 1, f"{len(calls)}")

# ===========================================================================
print("\n[9] N1c - every read-only signed GET opts in; nothing else does")
# ===========================================================================
src = inspect.getsource(exchange)
READ_ONLY = ["/fapi/v2/balance", "/fapi/v2/positionRisk", "/fapi/v1/userTrades",
             "/fapi/v1/openOrders", "/fapi/v1/order\"", "/fapi/v1/algoOrder",
             "/fapi/v1/openAlgoOrders"]
call_sites = [l for l in src.splitlines()
              if "retry_stale_timestamp=True" in l and not l.lstrip().startswith("#")]
check("all 8 read-only signed GETs opt into the retry", len(call_sites) == 8,
      f"found {len(call_sites)}")
check("retry_stale_timestamp defaults to False (opt-in, not opt-out)",
      "retry_stale_timestamp: bool = False" in src)

# and prove the default really is off for an unmarked signed call
c = make_client()
freeze_time_sync(c)
c._maybe_resync_server_time = never_resync
calls = script(c, [stale_ts_error(), {"ok": 1}])
try:
    asyncio.run(c._request("POST", "/fapi/v1/whatever", {"a": 1}, signed=True))
except BinanceApiError:
    pass
check("an unmarked signed call is not retried by default", len(calls) == 1, f"{len(calls)}")

# ===========================================================================
print("\n[10] N1c - only -1021 triggers the retry; other errors pass straight through")
# ===========================================================================
for code, status in [(-1003, 429), (-2019, 400), (-1013, 400), (None, 500)]:
    c = make_client()
    freeze_time_sync(c)
    c._maybe_resync_server_time = never_resync
    err = BinanceApiError(status, {"code": code, "msg": "x"} if code else {"msg": "boom"})
    calls = script(c, [err, [{"asset": "USDT"}]])
    try:
        asyncio.run(c.get_balance())
        raised = None
    except BinanceApiError as e:
        raised = e
    check(f"code={code} is not retried", raised is not None and len(calls) == 1,
          f"{len(calls)} call(s)")

# ===========================================================================
print("\n[11] N1 - the 418/429 cooldown still short-circuits before anything else")
# ===========================================================================
c = make_client()
freeze_time_sync(c)
c._cooldown_until_ts = time.time() + 30
resyncs = []
c._maybe_resync_server_time = lambda force=False: resyncs.append(force)
calls = script(c, [{"ok": 1}])
try:
    asyncio.run(c.get_balance())
    raised = None
except BinanceApiError as e:
    raised = e
check("an active cooldown still blocks the request", raised is not None and raised.status == 429)
check("a cooled-down request never reaches the wire", len(calls) == 0, f"{len(calls)}")
check("a cooled-down request never triggers a resync", len(resyncs) == 0, f"{len(resyncs)}")

# ===========================================================================
print("\n[12] N2 - github_sync's error lines can no longer be blank")
# ===========================================================================
check("github_sync exposes _exc_text", hasattr(github_sync, "_exc_text"))
for exc, expect in [
    (asyncio.TimeoutError(), "TimeoutError"),
    (aiohttp.ClientError(), "ClientError"),
    (aiohttp.ServerDisconnectedError(), "ServerDisconnectedError"),
    (RuntimeError("HTTP 409: conflict"), "RuntimeError: HTTP 409: conflict"),
]:
    got = github_sync._exc_text(exc)
    check(f"_exc_text({type(exc).__name__}) -> non-empty", got.strip() != "" and expect in got,
          f"got {got!r}")

gh_src = inspect.getsource(github_sync)
bare = [l.strip() for l in gh_src.splitlines()
        if "{e}" in l and "{_exc_text(e)}" not in l and "[brain-sync]" in l]
check("no logging line in github_sync.py interpolates a bare '{e}'",
      not bare, f"still bare: {bare}")
check("all three log sites route through _exc_text",
      gh_src.count("{_exc_text(e)}") == 3, f"found {gh_src.count('{_exc_text(e)}')}")

# behavioural: drive the real push path into a timeout and read the line
class _BoomSession:
    """Every verb raises the message-less timeout that produced the blank
    production line, so upload() takes its `except Exception` path."""
    def put(self, *a, **k):
        raise asyncio.TimeoutError()
    def get(self, *a, **k):
        raise asyncio.TimeoutError()

sync = github_sync.GithubBrainSync(
    token="t", repo="o/r", path="brain_LIVE_SOLUSDT.pkl", branch="b"
)
sync.session = _BoomSession()
sync._branch_ready = True          # skip _ensure_branch, isolate the push path
with Capture() as cap:
    asyncio.run(sync.upload(b"x", "msg"))
line = [l for l in cap.text.splitlines() if "brain-sync" in l]
check("the live push-failure line now names the exception",
      any("TimeoutError" in l for l in line),
      f"got {line!r}")
check("the line no longer ends in a bare colon",
      all(not l.rstrip().endswith(":") for l in line), f"got {line!r}")

# ===========================================================================
print("\n[13] N1 - _exc_text stays in lockstep between the two modules")
# ===========================================================================
import dca2
for exc in [asyncio.TimeoutError(), aiohttp.ClientError(), ValueError("v"), RuntimeError("")]:
    check(f"github_sync._exc_text matches dca2._exc_text for {type(exc).__name__}",
          github_sync._exc_text(exc) == dca2._exc_text(exc),
          f"{github_sync._exc_text(exc)!r} vs {dca2._exc_text(exc)!r}")

# ===========================================================================
print("\n" + "=" * 74)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
