"""
Regression tests for the 2026-08 per-trade fee-net loss budget (item 5),
the DCA loss-budget gate (item 7), and the prospective post-DCA max-hold
gate (item 8) - trading.py MartingaleManager._manage_open_position().

Root causes these address (see the attached Live incident report):
  - A single trade lost -$1.0070 (~12x the preceding +$0.0845 win) with no
    per-trade dollar ceiling independent of DCA step count - only the
    whole-day MAX_DAILY_LOSS_USDT circuit breaker existed, which does
    nothing to cap a single trade's loss.
  - DCA #1 was submitted and filled at a dca_step=0 hold time (3.10h) that
    passed the THEN-current 4h soft cap, but the moment it filled,
    dca_step became 1 and the soft cap immediately dropped to 2h
    (MAX_HOLD_TIME_DCA_MULTIPLIER), so the very next tick force-closed the
    position at a loss the DCA add could not possibly have helped avoid.

Fixes under test here:
  - item 5: MAX_TRADE_NET_LOSS_USDT / MAX_TRADE_EXIT_BUFFER_USDT gate,
    evaluated every tick (second only to Hard Stop), using the EXECUTABLE
    closing-side price and actual accumulated commission
    (estimate_net_pnl_usdt_executable). Gated behind position_sync_ready
    (provisional-economics-dependent, like Max Hold V2/Smart Exit/DCA -
    NOT a simple deterministic Hard-Stop-style check). Disabled entirely
    when MAX_TRADE_NET_LOSS_USDT<=0.
  - item 7: before submitting a NEW DCA add, projects the fee-net PnL the
    position would have immediately after that add fills
    (extra_qty/extra_entry_price on the same estimator) and withholds the
    add - without touching dca_step - if the projection would already
    breach the same budget/trigger.
  - item 8: computes the max-hold soft timeout that will apply the instant
    a pending DCA fills (dca_step is guaranteed >=1 post-fill, so this is
    MAX_HOLD_TIME_SEC * MAX_HOLD_TIME_DCA_MULTIPLIER regardless of the
    CURRENT step) and withholds the DCA add if the position is already
    past that prospective threshold, even though the current-step
    threshold has not yet been reached.

Uses a real MartingaleManager against a FakeClient - no network calls.
Run directly: `python3 test_trade_loss_budget_and_dca_gates_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
os.environ.setdefault("MAX_HOLD_TIME_SEC", "999999")
os.environ.setdefault("MAX_DCA_STEPS", "2")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_loss_budget_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_loss_budget_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_loss_budget_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_loss_budget_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_loss_budget_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_loss_budget_dca_state.json")

import asyncio
import io
import sys
import time

import dca2 as bot
import trading


class FakeClient:
    """Reports positionAmt/side matching whatever the test has set on
    manager.position, so close_position()'s exchange-verification fetch
    trusts local state and actually submits a reduceOnly close order
    instead of short-circuiting to 'exchange already flat'."""

    def __init__(self):
        self.placed_orders = []
        self._next_id = 9000
        self.position = None  # set by the test after manager creation

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        oid = self._next_id
        self._next_id += 1
        return {"orderId": oid}

    async def get_position_risk(self, symbol):
        p = self.position
        if p is None or p.status != "OPEN" or not p.total_qty:
            return [{"symbol": symbol, "positionAmt": "0", "entryPrice": "0"}]
        amt = p.total_qty if p.side == "LONG" else -p.total_qty
        return [{"symbol": symbol, "positionAmt": str(amt), "entryPrice": str(p.avg_entry_price)}]

    async def cancel_order(self, symbol, order_id):
        return {"orderId": order_id}


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


async def make_manager(side="LONG", avg_entry=77.08, qty=1.03, dca_step=0,
                        last_dca_price=None, opened_secs_ago=300,
                        sync_ready=True):
    filters = bot.SymbolFilters(tick_size=0.0001, step_size=0.01, min_qty=0.01, min_notional=5.0)
    client = FakeClient()
    m = bot.MartingaleManager(client=client, symbol="SOLUSDT", filters=filters, leverage=20)
    client.position = m.position
    m.position_sync_ready = sync_ready
    m.position.side = side
    m.position.status = "OPEN"
    m.position.entries = [(avg_entry, qty)]
    m.position.avg_entry_price = avg_entry
    m.position.total_qty = qty
    m.position.original_qty = qty
    m.position.dca_step = dca_step
    m.position.last_dca_price = last_dca_price
    m.position.opened_at = time.time() - opened_secs_ago
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.2, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    m.prev_price = avg_entry
    client.position = m.position
    return m


async def tick(m, price):
    m.current_price = price
    m.prev_price = price
    m.best_bid_price = price
    m.best_ask_price = price
    await m._manage_open_position()


# ============================================================================
# estimate_net_pnl_usdt_executable(): direct correctness checks (item 5/7/6's
# shared estimator) - LONG uses best_bid as the executable closing price,
# SHORT uses best_ask, both net actual/estimated commission.
# ============================================================================
async def test_executable_pnl_long_uses_bid_and_fees():
    m = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
    m._position_fees_accum = 0.05
    m._position_fees_reliable = True
    m.best_bid_price = 99.80
    m.best_ask_price = 99.82
    net = m.estimate_net_pnl_usdt_executable()
    # gross = (99.80-100.0)*1.0 = -0.20; close_fee = 0.0005*1*99.80=0.0499
    expected = (99.80 - 100.0) * 1.0 - 0.05 - (trading.TAKER_FEE_RATE * 1.0 * 99.80)
    assert abs(net - expected) < 1e-9, f"expected {expected}, got {net}"
    print(f"LONG executable pnl uses best_bid + actual fees: net={net:+.4f} (expected {expected:+.4f})")
    print("PASS\n")


async def test_executable_pnl_short_uses_ask_and_fees():
    m = await make_manager(side="SHORT", avg_entry=100.0, qty=1.0)
    m._position_fees_accum = 0.05
    m._position_fees_reliable = True
    m.best_bid_price = 100.18
    m.best_ask_price = 100.20
    net = m.estimate_net_pnl_usdt_executable()
    # gross = (100.0-100.20)*1.0 = -0.20 (SHORT closes by buying at the ask)
    expected = (100.0 - 100.20) * 1.0 - 0.05 - (trading.TAKER_FEE_RATE * 1.0 * 100.20)
    assert abs(net - expected) < 1e-9, f"expected {expected}, got {net}"
    print(f"SHORT executable pnl uses best_ask + actual fees: net={net:+.4f} (expected {expected:+.4f})")
    print("PASS\n")


async def test_executable_pnl_projects_prospective_dca_add():
    m = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
    m._position_fees_accum = 0.05
    m._position_fees_reliable = True
    m.best_bid_price = 99.00
    m.best_ask_price = 99.02
    without_add = m.estimate_net_pnl_usdt_executable()
    with_add = m.estimate_net_pnl_usdt_executable(extra_qty=1.0, extra_entry_price=99.00)
    assert with_add < without_add, "adding more qty at a worse price must project a worse (more negative) pnl"
    # actual position untouched - preview only
    assert m.position.total_qty == 1.0, "preview must not mutate the real position"
    print(f"projected-with-add pnl ({with_add:+.4f}) < current pnl ({without_add:+.4f}); "
          f"real position untouched (qty={m.position.total_qty})")
    print("PASS\n")


# ============================================================================
# item 5: per-trade fee-net loss budget
# ============================================================================
async def test_loss_budget_triggers_close_long():
    m = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
    m._position_fees_accum = 0.05
    m._position_fees_reliable = True
    with Capture() as cap:
        await tick(m, 99.80)  # net ~= -0.30, well past the -0.15 trigger
    out = cap.text
    assert "[trade-loss-budget] TRIGGERED" in out, out
    assert len(m.client.placed_orders) == 1, "must submit exactly one closing order"
    assert m.client.placed_orders[0]["side"] == "SELL", "closing a LONG must SELL, never reverse"
    assert m.client.placed_orders[0]["reduceOnly"] == "true"
    assert m.position.status == "CLOSING"
    assert m.position.trade_loss_budget_trigger_pnl is not None and m.position.trade_loss_budget_trigger_pnl <= -0.15
    print("LONG: per-trade loss budget triggered a single reduceOnly SELL close, never a reversal")
    print("PASS\n")


async def test_loss_budget_triggers_close_short():
    m = await make_manager(side="SHORT", avg_entry=100.0, qty=1.0)
    m._position_fees_accum = 0.05
    m._position_fees_reliable = True
    with Capture() as cap:
        await tick(m, 100.20)  # SHORT adverse move -> net ~= -0.30
    out = cap.text
    assert "[trade-loss-budget] TRIGGERED" in out, out
    assert len(m.client.placed_orders) == 1
    assert m.client.placed_orders[0]["side"] == "BUY", "closing a SHORT must BUY, never reverse"
    assert m.client.placed_orders[0]["reduceOnly"] == "true"
    print("SHORT: per-trade loss budget triggered a single reduceOnly BUY close, never a reversal")
    print("PASS\n")


async def test_loss_budget_disabled_when_env_zero():
    # trading.py imported the constant by value at module load, so directly
    # patch the module attribute the gate actually reads - equivalent to
    # what a real MAX_TRADE_NET_LOSS_USDT=0 deployment would produce.
    original = trading.MAX_TRADE_NET_LOSS_USDT
    trading.MAX_TRADE_NET_LOSS_USDT = 0.0
    try:
        m = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
        m._position_fees_accum = 0.05
        m._position_fees_reliable = True
        with Capture() as cap:
            await tick(m, 99.80)
        out = cap.text
        assert "[trade-loss-budget]" not in out
        # A -0.2% move can independently cross the (unrelated) ATR-based DCA
        # trigger distance at this price/qty - that is expected and not what
        # this test is about; the point is specifically that no *close*
        # order (reduceOnly) was submitted by the (now-disabled) budget gate.
        assert not any(o.get("reduceOnly") == "true" for o in m.client.placed_orders), (
            "no closing order may come from a disabled per-trade loss budget gate"
        )
        assert m.position.status != "CLOSING"
    finally:
        trading.MAX_TRADE_NET_LOSS_USDT = original
    print("MAX_TRADE_NET_LOSS_USDT<=0 fully disables the gate (previous Hard-Stop-only behavior)")
    print("PASS\n")


async def test_loss_budget_gated_by_position_sync_ready():
    m = await make_manager(side="LONG", avg_entry=100.0, qty=1.0, sync_ready=False)
    m._position_fees_accum = 0.05
    m._position_fees_reliable = True
    with Capture() as cap:
        await tick(m, 99.80)  # same deep-loss move as the triggering test above
    out = cap.text
    assert "[trade-loss-budget] TRIGGERED" not in out, (
        "provisional-economics-dependent gate must not act until position_sync_ready"
    )
    assert len(m.client.placed_orders) == 0, "no discretionary close order while unready"
    assert m.position.status == "OPEN", "position must remain open - no discretionary close while unready"
    print("[trade-loss-budget-skip] withheld while position_sync_ready=False, matching the "
          "existing Max-Hold-V2/DCA/Smart-Exit invariant")
    print("PASS\n")


# ============================================================================
# item 7: DCA loss-budget gate (projects the post-add state before allowing
# a new DCA order). Uses a deliberately generous MAX_TRADE_NET_LOSS_USDT/
# MAX_TRADE_EXIT_BUFFER_USDT for this test only (env-isolated, matching the
# pattern already used elsewhere in this suite) so the CURRENT (pre-add)
# state stays inside budget and the item-5 gate above does not preempt the
# DCA block - this isolates item 7's own projection logic. At the
# production defaults (0.20/0.05) item 5 already fires at or before the
# first DCA trigger for this position's size (see the interaction test
# below), which is expected/documented, not a bug in either gate.
# ============================================================================
async def test_dca_budget_gate_blocks_oversized_add():
    original_budget = trading.MAX_TRADE_NET_LOSS_USDT
    original_buffer = trading.MAX_TRADE_EXIT_BUFFER_USDT
    trading.MAX_TRADE_NET_LOSS_USDT = 0.9
    trading.MAX_TRADE_EXIT_BUFFER_USDT = 0.1  # trigger = -0.80
    try:
        m = await make_manager(
            side="LONG", avg_entry=77.10874524714829, qty=2.63, dca_step=1,
            last_dca_price=77.05,
        )
        dca_step_before = m.position.dca_step
        orders_before = len(m.client.placed_orders)
        with Capture() as cap:
            await tick(m, 76.8958)  # DCA #2 trigger price; current pnl ~-0.76 (> -0.80, item 5 OK)
        out = cap.text
        assert "[dca-budget] blocked" in out, out
        assert "[trade-loss-budget] TRIGGERED" not in out, "item 5 must not have preempted this scenario"
        assert len(m.client.placed_orders) == orders_before, "no DCA order may be placed once the projection breaches budget"
        assert m.position.dca_step == dca_step_before, "a blocked DCA must not count as completed"
        assert m.position.status == "OPEN"
    finally:
        trading.MAX_TRADE_NET_LOSS_USDT = original_budget
        trading.MAX_TRADE_EXIT_BUFFER_USDT = original_buffer
    print("[dca-budget] blocked the DCA #2 add whose projected post-fill fee-net pnl would breach "
          "the budget, without touching dca_step or the current (pre-add) OPEN position")
    print("PASS\n")


async def test_dca_budget_gate_allows_add_within_budget():
    original_budget = trading.MAX_TRADE_NET_LOSS_USDT
    original_buffer = trading.MAX_TRADE_EXIT_BUFFER_USDT
    trading.MAX_TRADE_NET_LOSS_USDT = 5.0
    trading.MAX_TRADE_EXIT_BUFFER_USDT = 0.5  # trigger = -4.5, comfortably beyond either state
    try:
        m = await make_manager(
            side="LONG", avg_entry=77.10874524714829, qty=2.63, dca_step=1,
            last_dca_price=77.05,
        )
        with Capture() as cap:
            await tick(m, 76.8958)
        out = cap.text
        assert "[dca-budget] blocked" not in out, out
        assert len(m.client.placed_orders) == 1, "DCA #2 must still fire normally when comfortably inside budget"
        assert m.client.placed_orders[0]["side"] == "BUY"
    finally:
        trading.MAX_TRADE_NET_LOSS_USDT = original_budget
        trading.MAX_TRADE_EXIT_BUFFER_USDT = original_buffer
    print("DCA #2 still fires normally when the projected post-add state comfortably remains inside budget")
    print("PASS\n")


async def test_loss_budget_preempts_dca_at_production_defaults():
    """Documents the real interaction at PRODUCTION default sizing/budget
    (MAX_TRADE_NET_LOSS_USDT=0.20, MAX_TRADE_EXIT_BUFFER_USDT=0.05,
    INITIAL_ENTRY_USDT/LEVERAGE/dca distance all left at whatever this
    process's env actually configures): item 5 (evaluated BEFORE the DCA
    block, every tick) already breaches its own trigger at or before the
    very first DCA distance is reached, so it closes the position and the
    DCA block below is never reached at all in the same tick. This is
    expected given the configured size (see item 7's own code comment) -
    not a defect in either gate, and asserted explicitly here so a future
    change to sizing/budget that silently breaks this interaction is
    caught."""
    m = await make_manager(side="LONG", avg_entry=77.08, qty=1.03, dca_step=0)
    dca_distance = m.get_dynamic_dca_distance_pct()
    trigger_price = 77.08 * (1 - dca_distance)
    with Capture() as cap:
        await tick(m, trigger_price - 0.0001)
    out = cap.text
    assert "[trade-loss-budget] TRIGGERED" in out, (
        "at production defaults the per-trade budget is expected to fire at/before the first DCA "
        f"trigger for this position size - if this no longer holds, re-check whether sizing or the "
        f"budget changed: {out}"
    )
    assert "DCA STEP 1" not in out, "the DCA block must not have been reached in the same tick"
    print("Documented: at production defaults, item 5 closes the trade at/before the first DCA "
          "distance is reached for the currently-configured size - item 7's own gate is a "
          "defense-in-depth safeguard for scenarios where it is not (larger budget, or fees "
          "accounted for differently); see the final report's sizing recommendation.")
    print("PASS\n")


# ============================================================================
# item 8: prospective post-DCA max-hold gate
# ============================================================================
async def test_prospective_post_dca_timeout_blocks_dca():
    # MAX_HOLD_TIME_SEC left at the real 4h default (this file only
    # overrides it to 999999 as a safety net against unrelated timeouts
    # firing in OTHER tests below - re-enable the real value for this one).
    # Also temporarily widens the per-trade loss budget (same env-isolation
    # rationale as the item-7 tests above): at production defaults item 5
    # already closes this fixture's position at the DCA #1 trigger price
    # (see test_loss_budget_preempts_dca_at_production_defaults), which
    # would prevent the DCA block - and this gate - from ever being reached
    # at all, masking what this test is actually about.
    real_max_hold_sec = 4 * 3600
    real_multiplier = trading.MAX_HOLD_TIME_DCA_MULTIPLIER  # 0.5 default -> 2h prospective post-DCA cap
    original_max_hold_sec = trading.MAX_HOLD_TIME_SEC
    original_enabled = trading.MAX_HOLD_TIME_ENABLED
    original_budget = trading.MAX_TRADE_NET_LOSS_USDT
    original_buffer = trading.MAX_TRADE_EXIT_BUFFER_USDT
    trading.MAX_HOLD_TIME_SEC = real_max_hold_sec
    trading.MAX_HOLD_TIME_ENABLED = True
    trading.MAX_TRADE_NET_LOSS_USDT = 5.0
    trading.MAX_TRADE_EXIT_BUFFER_USDT = 0.5
    try:
        prospective_post_dca_sec = real_max_hold_sec * real_multiplier  # 2h
        held_sec = prospective_post_dca_sec + 600  # 10 min past the POST-dca cap...
        assert held_sec < real_max_hold_sec, "must still be under the CURRENT (step-0) 4h cap"
        m = await make_manager(
            side="LONG", avg_entry=77.08, qty=1.03, dca_step=0,
            opened_secs_ago=held_sec,
        )
        dca_distance = m.get_dynamic_dca_distance_pct()
        trigger_price = 77.08 * (1 - dca_distance)
        with Capture() as cap:
            await tick(m, trigger_price - 0.0001)
        out = cap.text
        assert "[trade-loss-budget] TRIGGERED" not in out, "item 5 must not have preempted this scenario"
        assert "dca_blocked_post_step_timeout" in out, out
        assert len(m.client.placed_orders) == 0, "no DCA order may be placed when it would be immediately overdue on fill"
        assert m.position.dca_step == 0, "blocked DCA must not be counted"
    finally:
        trading.MAX_HOLD_TIME_SEC = original_max_hold_sec
        trading.MAX_HOLD_TIME_ENABLED = original_enabled
        trading.MAX_TRADE_NET_LOSS_USDT = original_budget
        trading.MAX_TRADE_EXIT_BUFFER_USDT = original_buffer
    print("DCA withheld: current hold time already exceeds the 2h prospective post-DCA soft cap "
          "even though the current (step-0) 4h cap has not yet been reached")
    print("PASS\n")


async def test_dca_still_fires_well_before_prospective_timeout():
    original_max_hold_sec = trading.MAX_HOLD_TIME_SEC
    original_enabled = trading.MAX_HOLD_TIME_ENABLED
    original_budget = trading.MAX_TRADE_NET_LOSS_USDT
    original_buffer = trading.MAX_TRADE_EXIT_BUFFER_USDT
    trading.MAX_HOLD_TIME_SEC = 4 * 3600
    trading.MAX_HOLD_TIME_ENABLED = True
    trading.MAX_TRADE_NET_LOSS_USDT = 5.0
    trading.MAX_TRADE_EXIT_BUFFER_USDT = 0.5
    try:
        m = await make_manager(
            side="LONG", avg_entry=77.08, qty=1.03, dca_step=0, opened_secs_ago=300,
        )
        dca_distance = m.get_dynamic_dca_distance_pct()
        trigger_price = 77.08 * (1 - dca_distance)
        with Capture() as cap:
            await tick(m, trigger_price - 0.0001)
        out = cap.text
        assert "dca_blocked_post_step_timeout" not in out, out
        assert len(m.client.placed_orders) == 1
        assert m.position.dca_step == 0  # not-yet-filled; pending
        assert m.position.pending_role == "dca"
    finally:
        trading.MAX_HOLD_TIME_SEC = original_max_hold_sec
        trading.MAX_HOLD_TIME_ENABLED = original_enabled
        trading.MAX_TRADE_NET_LOSS_USDT = original_budget
        trading.MAX_TRADE_EXIT_BUFFER_USDT = original_buffer
    print("DCA still fires normally at a fresh position, far from either the current or the "
          "prospective post-DCA hold-time cap")
    print("PASS\n")


async def run_all():
    await test_executable_pnl_long_uses_bid_and_fees()
    await test_executable_pnl_short_uses_ask_and_fees()
    await test_executable_pnl_projects_prospective_dca_add()
    await test_loss_budget_triggers_close_long()
    await test_loss_budget_triggers_close_short()
    await test_loss_budget_disabled_when_env_zero()
    await test_loss_budget_gated_by_position_sync_ready()
    await test_dca_budget_gate_blocks_oversized_add()
    await test_dca_budget_gate_allows_add_within_budget()
    await test_loss_budget_preempts_dca_at_production_defaults()
    await test_prospective_post_dca_timeout_blocks_dca()
    await test_dca_still_fires_well_before_prospective_timeout()
    print("ALL TRADE LOSS BUDGET / DCA GATE TESTS PASSED")


asyncio.run(run_all())
