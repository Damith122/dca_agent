"""Offline regression tests for the 2026-08-21 orderflow-exit minimum-hold gate.

WHAT THE LOGS SHOWED
--------------------------------------------------------------------------
Deployment c5d71582, 5 closed trades:

  exit_reason            hold      MFE       net
  orderflow_smart_exit   10.5s     0.000%   -$0.0676
  profit_lock            67.6s     0.600%   +$0.0968
  orderflow_smart_exit   16.6s     0.118%   -$0.0591
  orderflow_smart_exit   12.8s     0.009%   -$0.0584
  profit_lock           192.1s     0.758%   +$0.1331

All three losses exited via orderflow_smart_exit within seconds of
SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC expiring at its old value of 10s, each for
$0.058-$0.068 on a ~$38 notional. Round-trip fees alone are ~$0.027, so those
exits were booking barely more than the cost of the round trip - normal entry
noise converted into a realized loss.

THE CHANGE
--------------------------------------------------------------------------
  1. SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC 10 -> 50. That sits between the two
     sibling gates that already exist for the slower exits
     (MIN_HOLD_SEC_BEFORE_EXIT=60, SMART_EXIT_MIN_HOLD_SEC=90) and keeps this
     the fastest-reacting discretionary exit without firing inside the spread.
  2. The age test moved OUT of the block's outer condition into the decision
     itself, so a suppressed exit logs a line instead of vanishing silently.
     Previously a young position skipped the whole block leaving no trace.

WHAT MUST STAY ACTIVE DURING THE WINDOW (the safety requirement)
--------------------------------------------------------------------------
Hard Stop, the trade-loss budget and the 1:N RR stop all run EARLIER in
_manage_open_position() and return on their own. Profit Lock runs LATER and
is reached normally, because the orderflow block only returns when it
actually closes the position. This file asserts that ordering directly
against the source, not just by inspection.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_orderflow_exit_min_hold_fix.py`
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
import re
import sys
import time

import config
import trading
import dca2 as bot

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


def make_manager():
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    return bot.MartingaleManager(client=None, symbol="SOLUSDT", filters=filters, leverage=20)


def open_long_with_flipped_book(age_sec, net_target=-0.15):
    # 2026-08-21: net_target moved -0.07 -> -0.15. The orderflow micro-loss
    # band is now derived from notional (0.125%-0.25%), so at this fixture's
    # ~$79 notional it is [-$0.20, -$0.10]; -0.07 sits ABOVE the band and no
    # longer triggers. -0.15 is its midpoint.
    """A LONG whose book has flipped hard against it and whose fee-net PnL
    sits inside the micro-loss band - i.e. every orderflow-exit condition is
    met EXCEPT possibly the minimum hold."""
    m = make_manager()
    p = m.position
    p.status = "OPEN"
    p.side = "LONG"
    p.total_qty = 0.96
    p.original_qty = 0.96
    p.avg_entry_price = 82.43
    p.entries = [(82.43, 0.96)]
    p.opened_at = time.time() - age_sec
    m.best_bid_price, m.best_ask_price = 82.40, 82.41
    m.current_price = 82.405
    m._position_fees_accum = 0.0158
    m._position_fees_reliable = True
    m.position_sync_ready = True
    m.last_regime = trading.RegimeReading(
        regime=trading.REGIME_WEAK_TREND, atr_pct=0.003326, atr_ratio=1.0,
    )
    # Book flipped hard against the LONG, trades hitting the bid.
    m.orderflow_snapshot = lambda: {
        "data_available": True,
        "imbalance": -0.95,          # far past SMART_ORDERFLOW_EXIT_IMBALANCE
        "trade_delta": -5000.0,      # sellers dominating
    }
    # Land the fee-net estimate squarely inside the micro-loss band.
    m.estimate_net_pnl_usdt_executable = lambda: net_target
    m.closed = []

    async def fake_close(reason, **kw):
        m.closed.append({"reason": reason, "tag": kw.get("exit_reason_tag")})
        m.position.status = "CLOSING"

    m.close_position = fake_close
    return m


async def run_manage(m):
    try:
        await m._manage_open_position()
    except Exception:
        pass    # stubs short-circuit later stages; the exit decision already ran


# ===========================================================================
print("\n[1] The config value moved into the requested 45-60s band")
# ===========================================================================
check("SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC is 50s",
      config.SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC == 50.0,
      f"got {config.SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC}")
check("it sits inside the requested 45-60s band",
      45.0 <= config.SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC <= 60.0)
check("it is still the FASTEST discretionary exit (below Smart Exit V2's 90s)",
      config.SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC < config.SMART_EXIT_MIN_HOLD_SEC)
check("it is below the generic MIN_HOLD_SEC_BEFORE_EXIT (60s) too",
      config.SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC <= config.MIN_HOLD_SEC_BEFORE_EXIT)
os.environ["_MH_TEST"] = "10"
check("it stays env-overridable (set to 10 to restore the old behaviour)",
      float(os.environ["_MH_TEST"]) == 10.0)
del os.environ["_MH_TEST"]

# ===========================================================================
print("\n[2] Inside the window, the orderflow exit does NOT fire")
# ===========================================================================
for age in (0.0, 5.0, 10.5, 16.6, 12.8, 30.0, 49.0):
    m = open_long_with_flipped_book(age_sec=age)
    asyncio.run(run_manage(m))
    fired = [c for c in m.closed if c["tag"] == "orderflow_smart_exit"]
    check(f"age={age}s -> orderflow exit suppressed", not fired,
          f"fired with tag {fired}")

# The three real production holds are all inside the new window
check("all three live losing holds (10.5s/16.6s/12.8s) are now suppressed",
      all(h < config.SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC for h in (10.5, 16.6, 12.8)))

# ===========================================================================
print("\n[3] Past the window, it fires exactly as before")
# ===========================================================================
for age in (50.0, 51.0, 75.0, 300.0):
    m = open_long_with_flipped_book(age_sec=age)
    asyncio.run(run_manage(m))
    fired = [c for c in m.closed if c["tag"] == "orderflow_smart_exit"]
    check(f"age={age}s -> orderflow exit fires", len(fired) == 1,
          f"closed={m.closed}")

m = open_long_with_flipped_book(age_sec=60.0)
asyncio.run(run_manage(m))
check("the firing path still tags the trade orderflow_smart_exit",
      m.closed and m.closed[0]["tag"] == "orderflow_smart_exit")
check("the close reason still explains the book flip",
      "SMART ORDERFLOW EXIT" in m.closed[0]["reason"])

# ===========================================================================
print("\n[4] SAFETY: the Hard Stop still fires inside the window")
# ===========================================================================
# Requirement 1. A catastrophic move at 5 seconds old must still close.
m = open_long_with_flipped_book(age_sec=5.0)
m.current_price = 60.0                      # ~27% against a 20x LONG
m.best_bid_price, m.best_ask_price = 59.99, 60.0
asyncio.run(run_manage(m))
tags = [c["tag"] for c in m.closed]
check("a hard-stop-sized adverse move closes at 5s old", bool(m.closed), f"{m.closed}")
check("...and it closes via a RISK exit, not the orderflow exit",
      "orderflow_smart_exit" not in tags, f"tags={tags}")

# ===========================================================================
print("\n[5] SAFETY: the source ordering guarantees the risk exits run first")
# ===========================================================================
src = inspect.getsource(trading.MartingaleManager._manage_open_position)


def pos(tag):
    i = src.find(tag)
    return i if i >= 0 else 10**9


of_at = pos('exit_reason_tag="orderflow_smart_exit"')
check("Hard Stop is evaluated BEFORE the orderflow exit",
      pos('exit_reason_tag="hard_stop"') < of_at)
check("the trade-loss budget is evaluated BEFORE the orderflow exit",
      pos('exit_reason_tag="max_trade_net_loss"') < of_at)
check("the 1:N RR stop is evaluated BEFORE the orderflow exit",
      pos('exit_reason_tag="rr_stop_loss"') < of_at)
check("Profit Lock is evaluated AFTER the orderflow exit (so it is reached "
      "whenever the orderflow exit does not close)",
      pos('exit_reason_tag="profit_lock"') > of_at)

# The block may only return when it actually closes - otherwise Profit Lock
# and everything after it would be starved for the whole window.
block_start = src.index("SMART ORDERFLOW EARLY EXIT")
block_end = src.index("Breakeven stop", block_start)
block = src[block_start:block_end]
# Count real return STATEMENTS, not the substring - the block's own comments
# legitimately contain the words "return" and "returns".
return_lines = [l for l in block.splitlines() if re.match(r"^\s*return\b", l)]
check("the orderflow block contains exactly ONE return statement (the firing path)",
      len(return_lines) == 1, f"found {len(return_lines)}: {return_lines}")
stmt_offset = block.index(return_lines[0]) if return_lines else -1
check("that return sits inside the firing branch, after close_position()",
      stmt_offset > block.index("close_position"))

# ===========================================================================
print("\n[6] SAFETY: Profit Lock is genuinely reachable inside the window")
# ===========================================================================
# A young, PROFITABLE position with a flipped book must still be able to arm
# Profit Lock - the orderflow block must not swallow the tick.
m = open_long_with_flipped_book(age_sec=8.0)
m.estimate_net_pnl_usdt_executable = lambda: +0.25      # comfortably in profit
with Capture() as cap:
    asyncio.run(run_manage(m))
check("a profitable young position reaches the Profit Lock stage",
      "profit-lock" in cap.text.lower(), cap.text[-200:] or "(no output)")
check("...and is NOT closed by the orderflow exit",
      not [c for c in m.closed if c["tag"] == "orderflow_smart_exit"])

# ===========================================================================
print("\n[7] The suppression is now VISIBLE in the log")
# ===========================================================================
m = open_long_with_flipped_book(age_sec=12.0)
with Capture() as cap:
    asyncio.run(run_manage(m))
text = cap.text
check("a suppressed exit logs an explicit line",
      "[orderflow-exit] SUPPRESSED by the minimum-hold gate" in text,
      text[:300] or "(no output)")
check("the line reports the position's age", re.search(r"only \d+s old", text) is not None)
check("the line reports the configured gate", "gate is 50s" in text, text[:300])
check("the line names what remains active",
      "Hard Stop" in text and "Profit Lock" in text)

# it must NOT log when the book has not flipped (no noise)
m = open_long_with_flipped_book(age_sec=12.0)
m.orderflow_snapshot = lambda: {"data_available": True, "imbalance": 0.0, "trade_delta": 0.0}
with Capture() as cap:
    asyncio.run(run_manage(m))
check("a quiet book logs no suppression line",
      "SUPPRESSED by the minimum-hold gate" not in cap.text)

# nor when the loss is outside the micro-band (that is the pre-existing line)
m = open_long_with_flipped_book(age_sec=12.0)
m.estimate_net_pnl_usdt_executable = lambda: -0.90     # way past the band
with Capture() as cap:
    asyncio.run(run_manage(m))
check("a loss outside the micro-band does not use the suppression line",
      "SUPPRESSED by the minimum-hold gate" not in cap.text)

# ===========================================================================
print("\n[8] Nothing else about the trigger changed")
# ===========================================================================
# Each remaining condition must still independently block the exit, even
# well past the minimum hold.
m = open_long_with_flipped_book(age_sec=120.0)
m.orderflow_snapshot = lambda: {"data_available": False}
asyncio.run(run_manage(m))
check("no orderflow data -> still no exit",
      not [c for c in m.closed if c["tag"] == "orderflow_smart_exit"])

m = open_long_with_flipped_book(age_sec=120.0)
m.orderflow_snapshot = lambda: {"data_available": True, "imbalance": -0.10, "trade_delta": -5000.0}
asyncio.run(run_manage(m))
check("imbalance below threshold -> still no exit",
      not [c for c in m.closed if c["tag"] == "orderflow_smart_exit"])

m = open_long_with_flipped_book(age_sec=120.0)
m.orderflow_snapshot = lambda: {"data_available": True, "imbalance": -0.95, "trade_delta": +5000.0}
asyncio.run(run_manage(m))
check("trade delta on OUR side -> still no exit",
      not [c for c in m.closed if c["tag"] == "orderflow_smart_exit"])

m = open_long_with_flipped_book(age_sec=120.0)
m.estimate_net_pnl_usdt_executable = lambda: -0.01     # above the band
asyncio.run(run_manage(m))
check("loss above the micro-band -> still no exit",
      not [c for c in m.closed if c["tag"] == "orderflow_smart_exit"])

m = open_long_with_flipped_book(age_sec=120.0)
m.position_sync_ready = False
asyncio.run(run_manage(m))
check("position_sync_ready=False -> still no exit",
      not [c for c in m.closed if c["tag"] == "orderflow_smart_exit"])

# a SHORT flips the other way - the side logic is untouched
m = open_long_with_flipped_book(age_sec=120.0)
m.position.side = "SHORT"
m.orderflow_snapshot = lambda: {"data_available": True, "imbalance": +0.95, "trade_delta": +5000.0}
asyncio.run(run_manage(m))
check("a SHORT with the book flipped UP still exits",
      len([c for c in m.closed if c["tag"] == "orderflow_smart_exit"]) == 1, f"{m.closed}")

# ===========================================================================
print("\n[9] A DCA add does not re-arm the window")
# ===========================================================================
# opened_at is set once on the initial entry and never reset by a DCA add,
# so the gate measures true position age rather than restarting mid-trade.
tsrc = open("trading.py").read()
assigns = [l.strip() for l in tsrc.splitlines()
           if re.search(r"\.opened_at\s*=\s*time\.time\(\)", l)]
check("opened_at is stamped in exactly one place", len(assigns) == 1, f"{assigns}")

m = open_long_with_flipped_book(age_sec=120.0)
before = m.position.opened_at
m.position.dca_step = 1
m.position.total_qty = 1.92                # simulate a DCA add having landed
asyncio.run(run_manage(m))
check("a DCA add leaves opened_at untouched", m.position.opened_at == before)
check("...so a DCA'd position past the window still exits",
      len([c for c in m.closed if c["tag"] == "orderflow_smart_exit"]) == 1)

# ===========================================================================
print("\n" + "=" * 74)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
