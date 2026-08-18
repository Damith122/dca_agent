"""
Regression tests for the 2026-08 exchange-native protective stop (item 6) -
trading.py MartingaleManager._compute_protective_stop_price() /
_place_or_replace_protective_stop() / _cancel_protective_stop(), and the
top-level reconcile_protective_stop_on_startup().

Root cause this addresses: a client-side-only loss check (item 5) cannot
protect the position during the exact scenario the attached Live incident
describes - an HTTP 418 IP ban during which this process could not reach
Binance's REST API at all for ~25 minutes. A STOP_MARKET closePosition=true
order resting directly on the exchange protects the position independently
of whether this process can currently make any REST call.

Covers, per the task's own requirements for item 6:
  - placed immediately after an entry/DCA fill, sized from the same
    MAX_TRADE_NET_LOSS_USDT/MAX_TRADE_EXIT_BUFFER_USDT budget as item 5
  - closePosition=true (cannot reverse/increase the position; always closes
    the entire current position; no qty bookkeeping needed across DCA)
  - correctly replaced (cancel-then-place) after a DCA changes the
    position's economics
  - PROTECTION_PENDING entered (new DCA blocked; other exits unaffected)
    when placement fails, instead of continuing silently unprotected
  - startup reconciliation adopts an already-resting matching order,
    de-duplicates extras, places a fresh one if none is found, and enters
    PROTECTION_PENDING (rather than crashing or assuming protection) if the
    open-orders fetch itself fails
  - disabled cleanly via PROTECTIVE_STOP_ENABLED / MAX_TRADE_NET_LOSS_USDT

2026-08 update (review finding 4): reconciliation now proves ownership via
the bot-assigned clientOrderId prefix, so the open-order fixtures below
carry PROTECTIVE_STOP_CLIENT_ID_PREFIX. An order WITHOUT it is deliberately
no longer adopted or cancelled - that case (a manual/third-party stop being
left untouched) is covered in test_protective_stop_lifecycle_fix.py.

Uses a real MartingaleManager against a FakeClient - no network calls.
Run directly: `python3 test_protective_stop_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "false")
os.environ.setdefault("MAX_DCA_STEPS", "2")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_protective_stop_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_protective_stop_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_protective_stop_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_protective_stop_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_protective_stop_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_protective_stop_dca_state.json")

import asyncio
import io
import sys
import time

import dca2 as bot
import trading


class FakeClient:
    def __init__(self, open_orders=None, fail_open_orders=False, fail_place=False):
        self.placed_orders = []
        self.cancelled_order_ids = []
        self._open_orders = open_orders if open_orders is not None else []
        self._fail_open_orders = fail_open_orders
        self._fail_place = fail_place
        self._next_id = 7000
        self.position = None

    async def place_order(self, **kwargs):
        if self._fail_place:
            raise trading.BinanceApiError(400, {"code": -1001, "msg": "simulated network/API failure"})
        self.placed_orders.append(kwargs)
        oid = self._next_id
        self._next_id += 1
        return {"orderId": oid, "algoId": oid}

    # --- Algo (conditional) endpoints: the protective stop lives here now.
    async def place_algo_order(self, **kwargs):
        return await self.place_order(**kwargs)

    async def cancel_algo_order(self, algo_id=None, client_algo_id=None):
        return await self.cancel_order(None, algo_id if algo_id is not None else client_algo_id)

    async def get_open_algo_orders(self, symbol=None):
        return await self.get_open_orders(symbol)

    async def get_algo_order(self, algo_id=None, client_algo_id=None):
        for o in self._open_orders:
            if (algo_id is not None and o.get("algoId") == algo_id) or (
                client_algo_id is not None and o.get("clientAlgoId") == client_algo_id):
                return o
        return {"algoId": algo_id, "clientAlgoId": client_algo_id, "algoStatus": "NEW",
                "actualOrderId": ""}

    async def cancel_order(self, symbol, order_id):
        self.cancelled_order_ids.append(order_id)
        return {"orderId": order_id}

    async def get_position_risk(self, symbol):
        p = self.position
        if p is None or p.status != "OPEN" or not p.total_qty:
            return [{"symbol": symbol, "positionAmt": "0", "entryPrice": "0"}]
        amt = p.total_qty if p.side == "LONG" else -p.total_qty
        return [{"symbol": symbol, "positionAmt": str(amt), "entryPrice": str(p.avg_entry_price)}]

    async def get_open_orders(self, symbol):
        if self._fail_open_orders:
            raise trading.BinanceApiError(400, {"code": -1021, "msg": "simulated fetch failure"})
        return self._open_orders


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


async def make_manager(side="LONG", avg_entry=100.0, qty=1.0, dca_step=0, client=None):
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    client = client if client is not None else FakeClient()
    m = bot.MartingaleManager(client=client, symbol="SOLUSDT", filters=filters, leverage=20)
    client.position = m.position
    m.position_sync_ready = True
    m.position.side = side
    m.position.status = "OPEN"
    m.position.entries = [(avg_entry, qty)]
    m.position.avg_entry_price = avg_entry
    m.position.total_qty = qty
    m.position.original_qty = qty
    m.position.dca_step = dca_step
    m.position.opened_at = time.time() - 300
    m._position_fees_accum = 0.05
    m._position_fees_reliable = True
    m.current_price = avg_entry
    m.best_bid_price = avg_entry
    m.best_ask_price = avg_entry
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.2, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    m.prev_price = avg_entry
    client.position = m.position
    return m, client


# ============================================================================
# _compute_protective_stop_price(): inverts the same fee model item 5 uses,
# so the exchange-side trigger matches the client-side one.
# ============================================================================
async def test_stop_price_long_below_entry_matches_budget():
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
    stop_price = m._compute_protective_stop_price()
    assert stop_price is not None and stop_price < 100.0, "a LONG protective stop must sit below entry"
    # Confirm the computed price actually reproduces (approximately, modulo
    # one tick_size of ROUND_DOWN) the budget trigger via the SAME estimator
    # item 5 itself uses - proves the two mechanisms agree on where "the
    # budget is breached" is.
    m.best_bid_price = stop_price
    m.best_ask_price = stop_price + 0.01
    net_at_stop = m.estimate_net_pnl_usdt_executable()
    trigger = -(trading.MAX_TRADE_NET_LOSS_USDT - trading.MAX_TRADE_EXIT_BUFFER_USDT)
    assert abs(net_at_stop - trigger) < 0.01, f"net at computed stop price ({net_at_stop:+.4f}) should match trigger ({trigger:+.4f})"
    print(f"LONG protective stop price={stop_price:.4f} (entry=100.0) reproduces the item-5 budget "
          f"trigger ({net_at_stop:+.4f} ~= {trigger:+.4f})")
    print("PASS\n")


async def test_stop_price_short_above_entry_matches_budget():
    m, client = await make_manager(side="SHORT", avg_entry=100.0, qty=1.0)
    stop_price = m._compute_protective_stop_price()
    assert stop_price is not None and stop_price > 100.0, "a SHORT protective stop must sit above entry"
    m.best_ask_price = stop_price
    m.best_bid_price = stop_price - 0.01
    net_at_stop = m.estimate_net_pnl_usdt_executable()
    trigger = -(trading.MAX_TRADE_NET_LOSS_USDT - trading.MAX_TRADE_EXIT_BUFFER_USDT)
    assert abs(net_at_stop - trigger) < 0.01, f"net at computed stop price ({net_at_stop:+.4f}) should match trigger ({trigger:+.4f})"
    print(f"SHORT protective stop price={stop_price:.4f} (entry=100.0) reproduces the item-5 budget "
          f"trigger ({net_at_stop:+.4f} ~= {trigger:+.4f})")
    print("PASS\n")


async def test_stop_price_none_when_budget_disabled():
    original = trading.MAX_TRADE_NET_LOSS_USDT
    trading.MAX_TRADE_NET_LOSS_USDT = 0.0
    try:
        m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
        assert m._compute_protective_stop_price() is None
    finally:
        trading.MAX_TRADE_NET_LOSS_USDT = original
    print("protective stop price computation returns None when the loss budget is disabled")
    print("PASS\n")


# ============================================================================
# _place_or_replace_protective_stop(): placement, replace, closePosition
# semantics, and PROTECTION_PENDING on failure.
# ============================================================================
async def test_place_protective_stop_long():
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
    await m._place_or_replace_protective_stop(reason="initial entry filled")
    assert len(client.placed_orders) == 1
    order = client.placed_orders[0]
    assert order["side"] == "SELL", "closing a LONG must SELL"
    assert order["type"] == "STOP_MARKET"
    assert order["algoType"] == "CONDITIONAL"
    assert order["closePosition"] == "true", "must close the ENTIRE position, never a fixed reduceOnly qty"
    assert "quantity" not in order, "closePosition=true orders must not also specify a quantity"
    assert "reduceOnly" not in order, "closePosition=true must not be combined with reduceOnly"
    assert "stopPrice" not in order, "the Algo API uses triggerPrice, not stopPrice"
    assert m.position.protective_stop_algo_id is not None
    assert m.position.protective_stop_price is not None
    assert m.position.protection_pending is False
    print(f"LONG protective stop placed: {order['side']} {order['type']} closePosition=true "
          f"triggerPrice={order['triggerPrice']} workingType={order['workingType']}")
    print("PASS\n")


async def test_place_protective_stop_short():
    m, client = await make_manager(side="SHORT", avg_entry=100.0, qty=1.0)
    await m._place_or_replace_protective_stop(reason="initial entry filled")
    assert len(client.placed_orders) == 1
    order = client.placed_orders[0]
    assert order["side"] == "BUY", "closing a SHORT must BUY"
    assert order["closePosition"] == "true"
    print(f"SHORT protective stop placed: {order['side']} {order['type']} closePosition=true "
          f"triggerPrice={order['triggerPrice']}")
    print("PASS\n")


async def test_replace_cancels_old_before_placing_new():
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
    await m._place_or_replace_protective_stop(reason="initial entry filled")
    first_id = m.position.protective_stop_algo_id
    assert first_id is not None
    # Simulate a DCA that shifts the position's economics (bigger qty ->
    # different stop price) and trigger a replace.
    m.position.total_qty = 2.0
    m.position.avg_entry_price = 99.5
    await m._place_or_replace_protective_stop(reason="DCA #1 filled")
    assert client.cancelled_order_ids == [first_id], "must cancel exactly the previously-tracked order before placing the new one"
    assert len(client.placed_orders) == 2
    second_id = m.position.protective_stop_algo_id
    assert second_id is not None and second_id != first_id
    print(f"replace: cancelled stale orderId={first_id} before placing new orderId={second_id} "
          f"(cancel-then-replace sequence)")
    print("PASS\n")


async def test_disabled_by_flag_places_nothing():
    original = trading.PROTECTIVE_STOP_ENABLED
    trading.PROTECTIVE_STOP_ENABLED = False
    try:
        m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
        await m._place_or_replace_protective_stop(reason="initial entry filled")
        assert len(client.placed_orders) == 0
        assert m.position.protective_stop_algo_id is None
    finally:
        trading.PROTECTIVE_STOP_ENABLED = original
    print("PROTECTIVE_STOP_ENABLED=false places nothing (feature fully disabled)")
    print("PASS\n")


async def test_placement_failure_enters_protection_pending():
    client = FakeClient(fail_place=True)
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0, client=client)
    with Capture() as cap:
        await m._place_or_replace_protective_stop(reason="initial entry filled")
    out = cap.text
    assert "*** HIGH SEVERITY ***" in out, out
    assert m.position.protection_pending is True
    assert m.position.protection_pending_reason is not None
    assert m.position.protective_stop_algo_id is None
    print("a failed placement enters PROTECTION_PENDING with a high-severity log instead of "
          "crashing or continuing silently unprotected")
    print("PASS\n")


async def test_protection_pending_blocks_new_dca_not_other_exits():
    # Widened budget (env-isolation, same rationale documented in
    # test_trade_loss_budget_and_dca_gates_fix.py's item-7 tests): at
    # production defaults item 5 itself would already close this fixture's
    # position at the DCA trigger price, which would mask what THIS test is
    # actually about (PROTECTION_PENDING withholding the DCA add
    # specifically).
    original_budget = trading.MAX_TRADE_NET_LOSS_USDT
    original_buffer = trading.MAX_TRADE_EXIT_BUFFER_USDT
    trading.MAX_TRADE_NET_LOSS_USDT = 5.0
    trading.MAX_TRADE_EXIT_BUFFER_USDT = 0.5
    try:
        client = FakeClient(fail_place=True)
        m, client = await make_manager(side="LONG", avg_entry=77.08, qty=1.03, dca_step=0, client=client)
        m.position.protection_pending = True
        m.position.protection_pending_reason = "placement failed: simulated"
        dca_distance = m.get_dynamic_dca_distance_pct()
        trigger_price = 77.08 * (1 - dca_distance)
        with Capture() as cap:
            m.current_price = trigger_price - 0.0001
            m.prev_price = m.current_price
            m.best_bid_price = m.current_price
            m.best_ask_price = m.current_price
            await m._manage_open_position()
        out = cap.text
        assert "[trade-loss-budget] TRIGGERED" not in out, "item 5 must not have preempted this scenario"
        assert "PROTECTION_PENDING" in out, out
        assert len(client.placed_orders) == 0, "no new DCA add while PROTECTION_PENDING"
        assert m.position.dca_step == 0
    finally:
        trading.MAX_TRADE_NET_LOSS_USDT = original_budget
        trading.MAX_TRADE_EXIT_BUFFER_USDT = original_buffer
    print("PROTECTION_PENDING withholds a new DCA add (Hard Stop/other risk-reducing exits remain "
          "reachable earlier in the same function)")
    print("PASS\n")


# ============================================================================
# _cancel_protective_stop()
# ============================================================================
async def test_cancel_clears_local_state():
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
    await m._place_or_replace_protective_stop(reason="initial entry filled")
    order_id = m.position.protective_stop_algo_id
    assert order_id is not None
    await m._cancel_protective_stop(reason="position closed")
    assert client.cancelled_order_ids == [order_id]
    assert m.position.protective_stop_algo_id is None
    assert m.position.protective_stop_price is None
    print(f"cancel clears local tracking fields regardless of REST outcome (orderId={order_id})")
    print("PASS\n")


async def test_cancel_noop_when_nothing_tracked():
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0)
    assert m.position.protective_stop_algo_id is None
    await m._cancel_protective_stop(reason="nothing to cancel")
    assert client.cancelled_order_ids == [], "must not call cancel_order when nothing is tracked"
    print("cancel is a no-op (no REST call) when no protective stop is currently tracked")
    print("PASS\n")


# ============================================================================
# reconcile_protective_stop_on_startup()
# ============================================================================
async def test_reconcile_adopts_single_matching_order():
    open_orders = [
        {"algoId": 555, "orderType": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "triggerPrice": "94.50", "clientAlgoId": f"{trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX}-a-1"},
    ]
    client = FakeClient(open_orders=open_orders)
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0, client=client)
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    out = cap.text
    assert "adopted existing algoId=555" in out, out
    assert m.position.protective_stop_algo_id == 555
    assert m.position.protective_stop_price == 94.50
    assert m.position.protection_pending is False
    assert len(client.placed_orders) == 0, "must not place a redundant new stop when one already exists"
    print("startup reconciliation adopted the single already-resting matching protective stop")
    print("PASS\n")


async def test_reconcile_dedupes_multiple_matching_orders():
    open_orders = [
        {"algoId": 601, "orderType": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "triggerPrice": "94.00", "clientAlgoId": f"{trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX}-b-1"},
        {"algoId": 602, "orderType": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "triggerPrice": "94.10", "clientAlgoId": f"{trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX}-b-2"},
    ]
    client = FakeClient(open_orders=open_orders)
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0, client=client)
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    out = cap.text
    assert "found 2 resting protective stop" in out, out
    assert m.position.protective_stop_algo_id in (601, 602)
    kept = m.position.protective_stop_algo_id
    other = 602 if kept == 601 else 601
    assert client.cancelled_order_ids == [other], "must cancel every extra so at most one remains"
    print(f"startup reconciliation adopted orderId={kept} and cancelled the duplicate orderId={other}")
    print("PASS\n")


async def test_reconcile_places_fresh_when_none_found():
    client = FakeClient(open_orders=[])
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0, client=client)
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    out = cap.text
    assert "UNPROTECTED" in out, out
    assert len(client.placed_orders) == 1, "must place a fresh protective stop when none was found"
    assert m.position.protective_stop_algo_id is not None
    print("startup reconciliation placed a fresh protective stop for a genuinely unprotected OPEN position")
    print("PASS\n")


async def test_reconcile_enters_protection_pending_on_fetch_failure():
    client = FakeClient(fail_open_orders=True)
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0, client=client)
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    out = cap.text
    assert "*** HIGH SEVERITY ***" in out, out
    assert m.position.protection_pending is True
    assert m.position.protection_pending_reason is not None
    print("a failed open-orders fetch at startup enters PROTECTION_PENDING (never assumes protected) "
          "with a high-severity log")
    print("PASS\n")


async def test_reconcile_noop_when_not_open():
    client = FakeClient(open_orders=[
        {"algoId": 1, "orderType": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "triggerPrice": "94.0", "clientAlgoId": f"{trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX}-c-1"},
    ])
    m, client = await make_manager(side="LONG", avg_entry=100.0, qty=1.0, client=client)
    m.position.status = "FLAT"
    await trading.reconcile_protective_stop_on_startup(client, m)
    assert m.position.protective_stop_algo_id is None
    assert len(client.placed_orders) == 0
    print("reconciliation is a no-op when the position is not OPEN")
    print("PASS\n")


async def run_all():
    await test_stop_price_long_below_entry_matches_budget()
    await test_stop_price_short_above_entry_matches_budget()
    await test_stop_price_none_when_budget_disabled()
    await test_place_protective_stop_long()
    await test_place_protective_stop_short()
    await test_replace_cancels_old_before_placing_new()
    await test_disabled_by_flag_places_nothing()
    await test_placement_failure_enters_protection_pending()
    await test_protection_pending_blocks_new_dca_not_other_exits()
    await test_cancel_clears_local_state()
    await test_cancel_noop_when_nothing_tracked()
    await test_reconcile_adopts_single_matching_order()
    await test_reconcile_dedupes_multiple_matching_orders()
    await test_reconcile_places_fresh_when_none_found()
    await test_reconcile_enters_protection_pending_on_fetch_failure()
    await test_reconcile_noop_when_not_open()
    print("ALL PROTECTIVE STOP TESTS PASSED")


asyncio.run(run_all())
