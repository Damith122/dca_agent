"""
Regression tests for exhausted-DCA logging after the loss-deferral rollback.

Root cause: in MartingaleManager._manage_open_position(), the
"[dca-risk-debug] exit_candidate=max_dca_exhausted ..." diagnostic line was
printed unconditionally on every tick that entered the max_dca_exhausted
branch (dca_step >= MAX_DCA_STEPS). A first attempt throttled it to once
per 30s (mirroring the existing [max-dca-exhausted-review] throttle), but
Live logs still showed ~2,942 of these lines in ~25 seconds - the aggTrade
tick rate on a stuck deep-DCA position is high enough that even a 30s
per-instance throttle produces enough volume, combined with everything
else being logged, to trip Railway's rate limiter and drop other log lines
(fill/close/Profit Lock output). The underlying trading state was correct
throughout (dca_step=2/2, qty/avg_entry matched the exchange, no resync
loop, no over-DCA) - this was purely a logging/observability problem.

The obsolete per-tick debug line remains removed. At the existing adverse
boundary, a confirmed 2/2 position now emits one
`[max-dca-exhausted] ... decision=CLOSE` line and submits one reduceOnly
close. Repeated ticks cannot spam logs or duplicate orders because the
position immediately becomes CLOSING.

Run directly: `python3 test_dca_risk_debug_throttle_fix.py`
"""
import os
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("MAX_DCA_STEPS", "2")
# Isolate this test from unrelated strategy features that could otherwise
# close/modify the position for reasons unrelated to the max_dca_exhausted
# review under test - same test-isolation pattern already used by
# test_new_features.py / test_rest_fallback_dca_safety_fix.py. Does not
# change production behavior; only affects this test process's env.
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "false")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_dca_risk_debug_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_dca_risk_debug_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_dca_risk_debug_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_dca_risk_debug_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_dca_risk_debug_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_dca_risk_debug_dca_state.json")

import asyncio
import io
import sys
import time

import dca2 as bot
import trading


class FakeClient:
    """Minimal stub: reports a matching open position (so close_position()
    actually places a reduceOnly order when it decides to close) and
    records every order placed. No real network calls."""

    def __init__(self, side="LONG", qty=3.55, entry_price=95.0):
        self.placed_orders = []
        self._next_id = 9500
        self.side = side
        self.qty = qty
        self.entry_price = entry_price

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        oid = self._next_id
        self._next_id += 1
        return {"orderId": oid}

    async def get_position_risk(self, symbol):
        amt = self.qty if self.side == "LONG" else -self.qty
        return [{"symbol": symbol, "positionAmt": str(amt), "entryPrice": str(self.entry_price)}]


async def make_manager(qty=3.55, avg_entry=95.0, side="LONG"):
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    client = FakeClient(side=side, qty=qty, entry_price=avg_entry)
    m = bot.MartingaleManager(client=client, symbol="SOLUSDT", filters=filters, leverage=20)
    m.position.side = side
    m.position.status = "OPEN"
    m.position.entries = [(avg_entry, qty)]
    m.position.avg_entry_price = avg_entry
    m.position.total_qty = qty
    m.position.original_qty = qty
    m.position.dca_step = trading.MAX_DCA_STEPS  # already exhausted, matches the Live incident
    m.position.opened_at = time.time()
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.1, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    m.prev_price = avg_entry * 0.995
    m.current_price = avg_entry * 0.995
    return m


class Capture:
    """Captures everything printed to stdout during a `with` block (no
    pytest dependency, matching the repo's existing plain-`python3` test
    convention)."""
    def __enter__(self):
        self._buf = io.StringIO()
        self._real_stdout = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *exc):
        sys.stdout = self._real_stdout

    @property
    def text(self):
        return self._buf.getvalue()


def debug_lines_in(text):
    return [l for l in text.splitlines() if "exit_candidate=max_dca_exhausted" in l]


def review_lines_in(text):
    return [l for l in text.splitlines() if "[max-dca-exhausted]" in l]


# ============================================================================
# TEST 1-4: >=1,000 rapid ticks at the hard boundary produce exactly one
# close decision and one reduceOnly order, with no obsolete debug spam.
# ============================================================================
async def test_rapid_ticks_close_once_without_dca_risk_debug():
    print("\n=== test_rapid_ticks_close_once_without_dca_risk_debug ===")
    m = await make_manager()
    # ~0.5% adverse - well under HARD_STOP_PCT (2%) - and no signals agree.
    # The exhausted boundary must still close immediately.
    price = m.position.avg_entry_price * 0.995

    with Capture() as cap:
        for _ in range(1200):
            m.current_price = price
            m.prev_price = price
            await m._manage_open_position()

    debug_lines = debug_lines_in(cap.text)
    review_lines = review_lines_in(cap.text)

    print(f"TEST 1: 1200 rapid ticks -> exit_candidate=max_dca_exhausted lines={len(debug_lines)}")
    assert len(debug_lines) == 0, (
        f"the removed [dca-risk-debug] line must NEVER print, got {len(debug_lines)}"
    )
    print("TEST 1: PASS - zero obsolete exit_candidate=max_dca_exhausted lines\n")

    print(f"TEST 2: [max-dca-exhausted] lines={len(review_lines)}")
    assert len(review_lines) == 1, (
        f"the hard boundary must log exactly once, got {len(review_lines)}"
    )
    assert "decision=CLOSE" in review_lines[0]
    print("TEST 2: PASS - one immediate hard-boundary close decision logged\n")

    print(f"TEST 3: total captured lines={len(cap.text.splitlines())} (must not be ~1200+ repeated debug lines)")
    assert len(cap.text.splitlines()) < 50, (
        "total log volume across 1200 rapid ticks must stay small"
    )
    print("TEST 3: PASS - total log volume stays small, not one line per tick\n")

    print(f"TEST 4: status={m.position.status} orders_placed={len(m.client.placed_orders)}")
    assert m.position.status == "CLOSING"
    assert len(m.client.placed_orders) == 1
    assert m.client.placed_orders[0].get("reduceOnly") == "true"
    print("TEST 4: PASS - exactly one reduceOnly close, no duplicate order\n")


# ============================================================================
# TEST 5-7: the same immediate close happens even with 0 recovery signals;
# signal values no longer control the exhausted boundary.
# ============================================================================
async def test_close_tick_also_never_prints_dca_risk_debug():
    print("=== test_close_tick_also_never_prints_dca_risk_debug ===")
    m = await make_manager()
    price = m.position.avg_entry_price * 0.995

    with Capture() as cap:
        m.prev_price = price
        m.current_price = price
        await m._manage_open_position()

    debug_lines = debug_lines_in(cap.text)
    review_lines = review_lines_in(cap.text)
    close_review_lines = [l for l in review_lines if "decision=CLOSE" in l]

    print(f"TEST 5: exit_candidate=max_dca_exhausted lines={len(debug_lines)}")
    assert len(debug_lines) == 0, (
        f"the removed [dca-risk-debug] line must never print, even on the CLOSE tick, got {len(debug_lines)}"
    )
    print("TEST 5: PASS - zero obsolete debug lines on the close tick\n")

    print(f"TEST 6: [max-dca-exhausted] decision=CLOSE lines={len(close_review_lines)}")
    assert len(close_review_lines) == 1, "the CLOSE decision itself must still be logged immediately"
    print("TEST 6: PASS - decision=CLOSE logged immediately with 0 recovery signals\n")

    print(f"TEST 7: status={m.position.status} orders_placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 1
    assert m.client.placed_orders[0].get("reduceOnly") == "true"
    assert m.position.status in ("CLOSING", "FLAT"), f"position must be closing, got status={m.position.status}"
    print("TEST 7: PASS - exactly one close order placed\n")


# ============================================================================
# TEST 8: Profit Lock logging format is completely unaffected by this
# removal - [profit-lock-debug] / [PROFITLOCK VERIFY] and their gross PnL /
# fee estimate / net PnL / peak / floor / action fields still appear
# exactly as before.
# ============================================================================
async def test_profit_lock_logging_unchanged():
    print("=== test_profit_lock_logging_unchanged ===")
    m = await make_manager()
    price = m.position.avg_entry_price * 0.995

    with Capture() as cap:
        m.current_price = price
        m.prev_price = price
        await m._manage_open_position()

    text = cap.text
    print(f"TEST 8: [profit-lock-debug] present={'[profit-lock-debug]' in text} "
          f"[PROFITLOCK VERIFY] present={'[PROFITLOCK VERIFY]' in text}")
    assert "[profit-lock-debug]" in text, "[profit-lock-debug] line must still be emitted"
    assert "[PROFITLOCK VERIFY]" in text, "[PROFITLOCK VERIFY] block must still be emitted"
    for field in ("gross=", "fees=", "net=", "peak=", "floor=", "action="):
        assert field in text, f"expected Profit Lock field '{field}' missing from output"
    for field in ("gross_local=", "fee_est=", "net_local="):
        assert field in text, f"expected [PROFITLOCK VERIFY] field '{field}' missing from output"
    print("TEST 8: PASS - Profit Lock logging (gross/fee/net/peak/floor/action, VERIFY fields) all unchanged\n")


async def main():
    await test_rapid_ticks_close_once_without_dca_risk_debug()
    await test_close_tick_also_never_prints_dca_risk_debug()
    await test_profit_lock_logging_unchanged()
    print("ALL DCA-RISK-DEBUG REMOVAL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
