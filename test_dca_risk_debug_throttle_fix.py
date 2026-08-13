"""
Regression tests for the 2026-08 [dca-risk-debug] Railway rate-limit fix.

Root cause: in MartingaleManager._manage_open_position(), the
"[dca-risk-debug] exit_candidate=max_dca_exhausted ..." diagnostic line was
printed unconditionally on every tick that entered the max_dca_exhausted
branch (dca_step >= MAX_DCA_STEPS), with no throttle at all - unlike the
"[max-dca-exhausted-review]" line right after it, which already had a 30s
throttle (_should_log_max_dca_exhausted_review). On a Live SOLUSDT position
stuck at dca_step=2/2, this produced ~1,982 identical [dca-risk-debug] lines
in ~14 seconds of aggTrade ticks, which caused Railway to start dropping log
lines ("Railway rate limit reached", "Messages dropped: 54",
"Messages dropped: 184"), obscuring fill/close/Profit Lock output. The
underlying trading state was correct the whole time (dca_step=2/2, qty and
avg_entry matched the exchange, no resync loop, no over-DCA) - this was a
pure logging/observability problem, not a decision-logic bug.

Fix (logging only - this file's target): the [dca-risk-debug]
exit_candidate=max_dca_exhausted print now uses the exact same
"throttle DEFER, always print CLOSE immediately" pattern the adjacent
[max-dca-exhausted-review] line already used, via a new
_should_log_dca_risk_debug_exhausted() 30s throttle. The recovery-risk
signal computation (previously below the print) was moved above it, byte-
for-byte unchanged, purely so the DEFER/CLOSE decision is known before
deciding whether to print. Label, field names, field order, and values of
the line itself are all unchanged. No trading decision, ENV variable,
threshold, DCA trigger/spacing/sizing/step-limit, TP, Profit Lock, Smart
Exit, Hard Stop, Max Hold Time, or recovery-review logic is touched.

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
    """Captures everything printed to stdout during a `with` block, while
    still letting the caller see it afterwards (unlike pytest's capsys,
    this file has no pytest dependency, matching the repo's existing
    plain-`python3` test convention)."""
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


# ============================================================================
# TEST 1-4: >=1,000 rapid DEFER ticks while dca_step is exhausted - the
# [dca-risk-debug] line must be throttled to at most one per 30s interval,
# only the already-throttled [max-dca-exhausted-review] output otherwise
# appears, the position stays open, and no order is placed.
# ============================================================================
async def test_rapid_defer_ticks_throttle_dca_risk_debug():
    print("\n=== test_rapid_defer_ticks_throttle_dca_risk_debug ===")
    m = await make_manager()
    # ~0.5% adverse - well under HARD_STOP_PCT (2%) - and no signals will
    # agree (risk_score=0.1, trend_direction=None, near-zero velocity),
    # so exhausted_agree_count stays at 0/2 -> decision=DEFER every tick.
    price = m.position.avg_entry_price * 0.995

    with Capture() as cap:
        for _ in range(1200):
            m.current_price = price
            m.prev_price = price
            await m._manage_open_position()

    debug_lines = [l for l in cap.text.splitlines() if "[dca-risk-debug] exit_candidate=max_dca_exhausted" in l]
    review_lines = [l for l in cap.text.splitlines() if "[max-dca-exhausted-review]" in l]

    print(f"TEST 1: 1200 rapid DEFER ticks -> [dca-risk-debug] lines={len(debug_lines)}, "
          f"[max-dca-exhausted-review] lines={len(review_lines)}")
    assert len(debug_lines) == 1, (
        f"expected exactly one throttled [dca-risk-debug] line within the 30s interval, got {len(debug_lines)}"
    )
    print("TEST 1: PASS - [dca-risk-debug] throttled to a single line across 1200 rapid DEFER ticks\n")

    print(f"TEST 2: [max-dca-exhausted-review] lines={len(review_lines)} (must also stay throttled, unchanged)")
    assert len(review_lines) == 1, (
        f"[max-dca-exhausted-review]'s existing 30s throttle must be unaffected by this fix, got {len(review_lines)}"
    )
    print("TEST 2: PASS - existing [max-dca-exhausted-review] throttle unaffected\n")

    print(f"TEST 3: total captured lines={len(cap.text.splitlines())} (must not be ~1200+ repeated debug lines)")
    assert len(cap.text.splitlines()) < 50, (
        "total log volume across 1200 rapid DEFER ticks must stay small (no unthrottled per-tick spam)"
    )
    print("TEST 3: PASS - total log volume stays small, not one line per tick\n")

    print(f"TEST 4: status={m.position.status} orders_placed={len(m.client.placed_orders)}")
    assert m.position.status == "OPEN", "position must remain open across DEFER ticks"
    assert len(m.client.placed_orders) == 0, "no DCA/close order may be placed while decision stays DEFER"
    print("TEST 4: PASS - position stays open, no order placed during DEFER ticks\n")


# ============================================================================
# TEST 5-7: recovery signals reach the existing close threshold immediately
# after the throttle window opened (i.e. well within 30s of the last
# [dca-risk-debug] print) - [dca-risk-debug] and decision=CLOSE must still
# log immediately, bypassing the throttle, and exactly one close order must
# be placed.
# ============================================================================
async def test_close_decision_bypasses_throttle_immediately():
    print("=== test_close_decision_bypasses_throttle_immediately ===")
    m = await make_manager()
    price = m.position.avg_entry_price * 0.995

    with Capture() as cap:
        # Prime the throttle exactly as TEST 1 does - one DEFER tick.
        m.current_price = price
        m.prev_price = price
        await m._manage_open_position()

        # Immediately (same test, well under 30s wall-clock later) push
        # recovery-risk signals to/above MAX_HOLD_TIME_RECOVERY_MIN_AGREE
        # (2 of trend_against/high_risk/momentum_against/extreme_volatility)
        # - reusing the exact same signal formulas the code already uses.
        m.last_confidence = trading.ConfidenceReading(
            confidence_score=0.5, risk_score=0.90, trend_direction=("SHORT" if m.position.side == "LONG" else "LONG"),
            trend_confidence=0.80, success_probability=0.5, tp_hit_probability=0.5,
        )
        m.prev_price = price
        m.current_price = price * (0.999 if m.position.side == "LONG" else 1.001)  # strong adverse velocity
        await m._manage_open_position()

    debug_lines = [l for l in cap.text.splitlines() if "[dca-risk-debug] exit_candidate=max_dca_exhausted" in l]
    review_lines = [l for l in cap.text.splitlines() if "[max-dca-exhausted-review]" in l]
    close_review_lines = [l for l in review_lines if "decision=CLOSE" in l]

    print(f"TEST 5: [dca-risk-debug] lines={len(debug_lines)}")
    assert len(debug_lines) == 2, (
        "expected exactly 2 [dca-risk-debug] lines: the priming DEFER tick, plus the CLOSE tick logging "
        f"immediately despite being well within the 30s throttle window - got {len(debug_lines)}"
    )
    print("TEST 5: PASS - [dca-risk-debug] logged immediately on CLOSE, bypassing the throttle\n")

    print(f"TEST 6: [max-dca-exhausted-review] decision=CLOSE lines={len(close_review_lines)}")
    assert len(close_review_lines) == 1, "the CLOSE decision itself must be logged immediately"
    print("TEST 6: PASS - decision=CLOSE logged immediately\n")

    print(f"TEST 7: status={m.position.status} orders_placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 1, "exactly one close order must be placed once the recovery review closes"
    assert m.position.status in ("CLOSING", "FLAT"), f"position must be closing, got status={m.position.status}"
    print("TEST 7: PASS - exactly one close order placed\n")


async def main():
    await test_rapid_defer_ticks_throttle_dca_risk_debug()
    await test_close_decision_bypasses_throttle_immediately()
    print("ALL DCA-RISK-DEBUG THROTTLE FIX TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
