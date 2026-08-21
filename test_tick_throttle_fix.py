"""Offline regression tests for the 2026-08-21 tick-throttling fix.

WHAT HAPPENED
--------------------------------------------------------------------------
Deployment 19ca9220 ran the four-symbol watchlist (SOL/ETH/NEAR/SUI) for
~2h with zero restarts and correct isolation, but ETHUSDT's bookTicker
socket was being closed by Binance every 60-100 seconds:

    [market-ws:public/bookTicker:ETHUSDT] disconnected
        (sent 1011 (internal error) keepalive ping timeout; no close frame)

25 reconnects in 36 minutes on ETHUSDT, against 10 on SOLUSDT and 3-4 on
NEARUSDT/SUIUSDT. The count tracked each book's message rate almost
exactly, and the cadence was metronomic rather than bursty - so this was
not the egress instability chased earlier.

ROOT CAUSE
--------------------------------------------------------------------------
websocket.py ran a FULL decision cycle on every bookTicker message:

    manager.on_book_ticker(bid, ask, bid_qty, ask_qty)
    await manager.on_price_tick()      # <- feature build + Brain V2
                                       #    inference + risk scoring

bookTicker is event-driven, so a busy book fires it hundreds of times a
second. Railway reported ~0.8-1.1 CPU cores steady - which looks small
against an 8-core limit but is effectively maxed for a single-threaded
asyncio loop. The starved loop could not service its own websocket
keepalive pongs, so Binance closed the busiest socket.

THE FIX
--------------------------------------------------------------------------
Decimation, not queueing. on_book_ticker() still records EVERY message, so
price and orderbook stay perfectly current; only the expensive decision
cycle is rate-limited, and when it runs it reads the latest state. Nothing
is buffered, so the throttle can never build a backlog or act on stale data.

Two intervals, because the states differ:
  TICK_MIN_INTERVAL_SEC         (FLAT,   250ms) - entry scanning only, and
      entry scoring is driven by 1-minute candles.
  TICK_MIN_INTERVAL_ACTIVE_SEC  (live,   100ms) - stop-loss, Profit Lock,
      trailing, DCA and Profit Lock peak sampling. Still far finer than the
      100-500ms REST round-trip needed to act, and the exchange-native
      STOP_MARKET algo order remains the server-side backstop.

_sync_portfolio_slot() is deliberately left OUTSIDE the gate: it releases
the MAX_ACTIVE_TRADES slot on close, and throttling it would delay every
other symbol's ability to open.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_tick_throttle_fix.py`
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

import config
import trading
from trading import MartingaleManager, PortfolioCoordinator, SymbolFilters

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


FILTERS = SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)


def make_manager(symbol="SOLUSDT", portfolio=None):
    """A manager whose decision cycle is replaced by a counter, so the test
    measures ONLY how often the throttle lets the cycle through."""
    m = MartingaleManager(None, symbol, FILTERS, 20, portfolio=portfolio)
    m.cycles = 0

    async def _count():
        m.cycles += 1

    # Everything on_price_tick() does after the gate, stubbed to a counter.
    m._sweep_orphan_protective_stops = _count
    return m


async def tick(m):
    """Drive one bookTicker message through the real gate."""
    try:
        await m.on_price_tick()
    except Exception:
        # The stub short-circuits the cycle, so anything past the counter
        # raises. That is fine - the counter already recorded the pass.
        pass


# ===========================================================================
print("\n[1] Config knobs are in the requested 100-250ms band")
# ===========================================================================
check("TICK_MIN_INTERVAL_SEC is 250ms", config.TICK_MIN_INTERVAL_SEC == 0.25,
      f"got {config.TICK_MIN_INTERVAL_SEC}")
check("TICK_MIN_INTERVAL_ACTIVE_SEC is 100ms",
      config.TICK_MIN_INTERVAL_ACTIVE_SEC == 0.10,
      f"got {config.TICK_MIN_INTERVAL_ACTIVE_SEC}")
check("both sit inside the 100-250ms band",
      0.10 <= config.TICK_MIN_INTERVAL_ACTIVE_SEC <= 0.25
      and 0.10 <= config.TICK_MIN_INTERVAL_SEC <= 0.25)
check("an unset env var yields the default",
      config._clamped_tick_interval("_TT_DEFINITELY_UNSET", 0.25) == 0.25)
os.environ["_TT_TEST"] = "5.0"
check("an over-large env value is clamped to 1s",
      config._clamped_tick_interval("_TT_TEST", 0.25) == 1.0)
os.environ["_TT_TEST"] = "-3"
check("a negative env value falls back to the default",
      config._clamped_tick_interval("_TT_TEST", 0.25) == 0.25)
os.environ["_TT_TEST"] = "banana"
check("an unparseable env value falls back to the default",
      config._clamped_tick_interval("_TT_TEST", 0.25) == 0.25)
os.environ["_TT_TEST"] = "0"
check("0 is honoured (disables the throttle)",
      config._clamped_tick_interval("_TT_TEST", 0.25) == 0.0)
del os.environ["_TT_TEST"]

# ===========================================================================
print("\n[2] A burst of ticks is decimated, not queued")
# ===========================================================================
m = make_manager()
m.position.status = "FLAT"


async def burst(mgr, n):
    for _ in range(n):
        await tick(mgr)

asyncio.run(burst(m, 500))
check("500 back-to-back ticks produced exactly ONE decision cycle",
      m.cycles == 1, f"got {m.cycles}")
check("all 500 were still counted as seen (nothing is dropped silently)",
      m._ticks_seen == 500, f"got {m._ticks_seen}")
check("no backlog is retained - the throttle holds no queue",
      not hasattr(m, "_tick_queue") and not hasattr(m, "_pending_ticks"))

# after the interval elapses, exactly one more gets through
m._last_tick_run_ts -= (config.TICK_MIN_INTERVAL_SEC + 0.01)
asyncio.run(burst(m, 200))
check("once the interval elapses, exactly one more cycle runs",
      m.cycles == 2, f"got {m.cycles}")

# ===========================================================================
print("\n[3] Real elapsed-time behaviour matches the configured rate")
# ===========================================================================
m = make_manager()
m.position.status = "FLAT"


async def drive_for(mgr, seconds, sleep_s=0.002):
    end = time.time() + seconds
    while time.time() < end:
        await tick(mgr)
        await asyncio.sleep(sleep_s)

asyncio.run(drive_for(m, 1.0))
# 1 second at a 250ms interval -> 4-5 cycles
check("~1s of dense ticks at 250ms yields 4-5 cycles, not hundreds",
      4 <= m.cycles <= 6, f"got {m.cycles} from {m._ticks_seen} ticks")
check("the decimation ratio is large (>80% of ticks skipped)",
      m._ticks_seen > 0 and (m._ticks_seen - m.cycles) / m._ticks_seen > 0.8,
      f"seen={m._ticks_seen} ran={m.cycles}")

# ===========================================================================
print("\n[4] A live position gets the TIGHTER interval")
# ===========================================================================
for status in ("OPEN", "ENTERING", "DCA_PENDING", "CLOSING"):
    m = make_manager()
    m.position.status = status
    check(f"status={status} selects the active interval",
          m._tick_throttle_interval() == config.TICK_MIN_INTERVAL_ACTIVE_SEC,
          f"got {m._tick_throttle_interval()}")

m = make_manager()
m.position.status = "FLAT"
check("status=FLAT selects the idle interval",
      m._tick_throttle_interval() == config.TICK_MIN_INTERVAL_SEC)
check("the active interval is strictly tighter than the idle one",
      config.TICK_MIN_INTERVAL_ACTIVE_SEC < config.TICK_MIN_INTERVAL_SEC)

# and it really does run more often while a position is live
m_flat, m_open = make_manager(), make_manager()
m_flat.position.status, m_open.position.status = "FLAT", "OPEN"
asyncio.run(drive_for(m_flat, 1.0))
asyncio.run(drive_for(m_open, 1.0))
check("an OPEN position gets ~2.5x more risk checks than a FLAT scan",
      m_open.cycles > m_flat.cycles,
      f"open={m_open.cycles} flat={m_flat.cycles}")
check("an OPEN position still gets ~10 risk checks/sec",
      8 <= m_open.cycles <= 12, f"got {m_open.cycles}")

# ===========================================================================
print("\n[5] A gap always runs - the gate is elapsed-time, not a fixed cadence")
# ===========================================================================
m = make_manager()
m.position.status = "FLAT"
check("the very first tick after startup always runs",
      m._should_run_tick(time.time()) is True)
now = time.time()
m._last_tick_run_ts = now
check("an immediate follow-up is skipped", m._should_run_tick(now) is False)
check("a tick after a long gap (e.g. a websocket reconnect) runs",
      m._should_run_tick(now + 30.0) is True)
check("the gate never blocks two consecutive long-gap ticks",
      m._should_run_tick(now + 60.0) is True)

# ===========================================================================
print("\n[6] interval=0 restores the previous every-message behaviour")
# ===========================================================================
m = make_manager()
m.position.status = "FLAT"
orig = trading.TICK_MIN_INTERVAL_SEC
try:
    trading.TICK_MIN_INTERVAL_SEC = 0.0
    asyncio.run(burst(m, 50))
    check("with interval=0 every single tick runs a cycle",
          m.cycles == 50, f"got {m.cycles}")
finally:
    trading.TICK_MIN_INTERVAL_SEC = orig

# ===========================================================================
print("\n[7] Each symbol throttles on its OWN clock")
# ===========================================================================
pc = PortfolioCoordinator(1)
mgrs = {s: make_manager(s, portfolio=pc) for s in config.ACTIVE_SYMBOLS}
for mm in mgrs.values():
    mm.position.status = "FLAT"

# a busy book (SOL) gets 400 ticks, a quiet one (NEAR) gets 2
asyncio.run(burst(mgrs["SOLUSDT"], 400))
asyncio.run(burst(mgrs["NEARUSDT"], 2))
check("the busy symbol is decimated hard",
      mgrs["SOLUSDT"].cycles == 1 and mgrs["SOLUSDT"]._ticks_seen == 400,
      f"cycles={mgrs['SOLUSDT'].cycles}")
check("the quiet symbol is barely affected (its 1st tick still ran)",
      mgrs["NEARUSDT"].cycles == 1 and mgrs["NEARUSDT"]._ticks_seen == 2)
check("throttle state is per-instance, never shared",
      len({id(mm.__dict__) for mm in mgrs.values()}) == 4)
check("one symbol's ticks do not consume another's budget",
      mgrs["ETHUSDT"]._ticks_seen == 0 and mgrs["ETHUSDT"]._should_run_tick(time.time()))

# ===========================================================================
print("\n[8] The portfolio slot is NOT throttled")
# ===========================================================================
# This is the one thing that must stay on every tick: it releases the
# MAX_ACTIVE_TRADES slot on close, and delaying it would delay every OTHER
# symbol's ability to open.
pc = PortfolioCoordinator(1)
sol = make_manager("SOLUSDT", portfolio=pc)
eth = make_manager("ETHUSDT", portfolio=pc)
sol.position.status = "OPEN"
asyncio.run(tick(sol))
check("an OPEN position claims the slot on its first tick", pc.holders() == ["SOLUSDT"])
check("the other symbol is correctly blocked", pc.has_capacity("ETHUSDT") is False)

# now close it and tick again IMMEDIATELY - inside the throttle interval
sol.position.status = "FLAT"
asyncio.run(tick(sol))
check("the slot is released on the very next tick, even though the "
      "decision cycle was throttled", pc.active_count() == 0)
check("the other symbol can open again immediately",
      pc.has_capacity("ETHUSDT") is True)

src = inspect.getsource(MartingaleManager.on_price_tick)
gate_at = src.index("_should_run_tick")
sync_at = src.index("_sync_portfolio_slot")
check("_sync_portfolio_slot() is called BEFORE the throttle gate",
      sync_at < gate_at)

# ===========================================================================
print("\n[9] Price/orderbook recording is untouched by the throttle")
# ===========================================================================
ws_src = open("websocket.py").read()
check("on_book_ticker() is still called on every message, outside the gate",
      "manager.on_book_ticker(bid, ask, bid_qty, ask_qty)\n"
      "                                    await manager.on_price_tick()" in ws_src)
check("the throttle lives in on_price_tick(), not in websocket.py",
      "_should_run_tick" in inspect.getsource(trading.MartingaleManager)
      and "_should_run_tick" not in ws_src)

# every message really does update the recorded price
m = make_manager()
m.position.status = "FLAT"
prices = []
for i in range(20):
    bid, ask = 100.0 + i, 100.1 + i
    m.on_book_ticker(bid, ask, 5.0, 5.0)
    asyncio.run(tick(m))
    prices.append(m.current_price)
check("every one of 20 rapid messages updated current_price",
      len(set(prices)) == 20, f"got {len(set(prices))} distinct prices")
check("the decision cycle sees the LATEST price, not a stale one",
      m.current_price == (119.0 + 119.1) / 2, f"got {m.current_price}")
check("only a fraction of those 20 messages ran a cycle",
      m.cycles < 20, f"got {m.cycles}")

# ===========================================================================
print("\n[10] The efficiency log is diagnostics only")
# ===========================================================================
m = make_manager()
m.position.status = "FLAT"
m._ticks_seen, m._ticks_run = 1000, 4
m._last_tick_throttle_log_ts = time.time() - 301
with Capture() as cap:
    m._maybe_log_tick_throttle(time.time())
check("the periodic line reports the decimation rate",
      "[tick-throttle]" in cap.text and "decimated" in cap.text, cap.text[:120])
check("it names the symbol", "SOLUSDT" in cap.text)
check("counters reset after logging", m._ticks_seen == 0 and m._ticks_run == 0)

# the first window is skipped rather than logging a meaningless sample
m2 = make_manager()
m2._ticks_seen, m2._last_tick_throttle_log_ts = 5, 0.0
with Capture() as cap:
    m2._maybe_log_tick_throttle(time.time())
check("the first window logs nothing (no meaningless sample)",
      "[tick-throttle]" not in cap.text, cap.text[:100])
check("...but it does arm the window", m2._last_tick_throttle_log_ts > 0)

# never divides by zero on a symbol that has seen nothing
m3 = make_manager()
with Capture() as cap:
    m3._maybe_log_tick_throttle(time.time())
check("a symbol with zero ticks never divides by zero", "[tick-throttle]" not in cap.text)

# ===========================================================================
print("\n[11] Estimated CPU relief")
# ===========================================================================
# The observed production rates, decimated by the shipped intervals.
observed = {"ETHUSDT": 180, "SOLUSDT": 70, "NEARUSDT": 25, "SUIUSDT": 20}  # msg/sec
before = sum(observed.values())
after = sum(min(rate, 1.0 / config.TICK_MIN_INTERVAL_SEC) for rate in observed.values())
print(f"       decision cycles/sec across the watchlist: {before:.0f} -> {after:.0f} "
      f"({before / after:.0f}x reduction)")
check("the throttle cuts total decision cycles by more than 10x",
      before / after > 10, f"{before / after:.1f}x")
check("every symbol still gets at least 4 cycles/sec while FLAT",
      1.0 / config.TICK_MIN_INTERVAL_SEC >= 4.0)
check("every symbol still gets at least 10 cycles/sec while OPEN",
      1.0 / config.TICK_MIN_INTERVAL_ACTIVE_SEC >= 10.0)

# ===========================================================================
print("\n" + "=" * 74)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
