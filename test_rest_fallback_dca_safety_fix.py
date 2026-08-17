"""
Regression tests for the 2026-08 WS-migration / REST order-status fallback /
hard DCA safety invariant fix.

Root cause this patch addresses (see the Live incident report it was
written against): Binance permanently decommissioned the legacy Live
private user-data WebSocket URL on 2026-04-23. The bot kept a live TCP/WS
connection (looked "connected" in logs) but stopped receiving
ORDER_TRADE_UPDATE/ACCOUNT_UPDATE events. Once a DCA/close order's FILLED
event never arrived, initialize_sync()'s SYNC_PENDING_GRACE_SEC (8s) grace
window expired with no way to confirm the order's real outcome, so it fell
straight to "snapshot doesn't match exchange" -> full rebuild -> dca_step
reset to 0 -> the bot re-fired DCA steps it had already taken (observed:
dca_count=5 against a configured MAX_DCA_STEPS=2).

Fix (this patch):
  1. websocket.py: Live now connects through the migrated /public, /market,
     /private routes (see that file); Testnet is unchanged.
  2. exchange.py: RestClient.get_order() (signed GET /fapi/v1/order) and
     get_user_trades(order_id=...) let a specific pending order's real
     outcome be queried directly instead of guessed.
  3. trading.py: MartingaleManager._resolve_pending_order_via_rest() is the
     single place a pending order is resolved via REST once grace has
     elapsed - FILLED routes through the same _on_entry_filled()/
     _on_close_filled() functions the live WebSocket path uses (so
     dca_step/avg_entry/total_qty/fees advance exactly once, idempotently),
     NEW/PARTIALLY_FILLED keeps waiting, CANCELED/EXPIRED/REJECTED clears
     pending bookkeeping safely, and REST-error/unknown/ambiguous sets
     PositionState.dca_blocked=True (persisted, restart-safe) instead of
     ever resetting dca_step to 0 and allowing further DCA.

Exercises the real MartingaleManager/initialize_sync from trading.py via
dca2.py, with DRY_RUN=false (so initialize_sync/_on_close_filled's
exchange-verification path actually runs) and manager.client replaced by
FakeClient below - no real network calls are made anywhere in this file.

Run from the repo root: python3 test_rest_fallback_dca_safety_fix.py
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("MAX_DCA_STEPS", "2")
# Isolate the DCA-gate tests below from unrelated strategy features (Smart
# Exit / Max Hold Time) that could otherwise close the position for reasons
# having nothing to do with what's under test here - same test-isolation
# pattern already used by test_new_features.py. Does not change strategy
# behavior in production; this only affects this test process's env.
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "false")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_rest_fallback_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_rest_fallback_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_rest_fallback_performance_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_rest_fallback_performance_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_rest_fallback_brain_v2.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_rest_fallback_dca_state.json")
os.environ.setdefault("BRAIN2_WARMUP_UPDATES", "5")

import asyncio
import time

import dca2 as bot
import trading
from exchange import BinanceApiError


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------

async def make_manager():
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    manager = bot.MartingaleManager(client=None, symbol="SOLUSDT", filters=filters, leverage=20)
    return manager


class FakeClient:
    """Configurable stub standing in for RestClient. `order_status` /
    `order_fills` / `raise_on_get_order` control what get_order() /
    get_user_trades() report for the *specific* pending order under test;
    `position_rows` controls get_position_risk() (used both by
    initialize_sync when `rows` isn't passed directly, and by
    _on_close_filled()'s post-fill verification)."""

    def __init__(
        self, position_rows=None, order_status="FILLED",
        order_fills=None, raise_on_get_order=False, order_resp_extra=None,
    ):
        self.position_rows = position_rows if position_rows is not None else []
        self.order_status = order_status
        self.order_fills = order_fills if order_fills is not None else []
        self.raise_on_get_order = raise_on_get_order
        self.order_resp_extra = order_resp_extra or {}
        self.get_order_calls = []
        self.get_user_trades_calls = []
        self.placed_orders = []
        self._next_order_id = 9900

    async def get_position_risk(self, symbol):
        return self.position_rows

    async def get_order(self, symbol, order_id):
        self.get_order_calls.append(order_id)
        if self.raise_on_get_order:
            raise BinanceApiError(500, {"msg": "server error"})
        resp = {"status": self.order_status}
        resp.update(self.order_resp_extra)
        return resp

    async def get_user_trades(self, symbol, from_id=None, start_time_ms=None, limit=1000, order_id=None):
        self.get_user_trades_calls.append(order_id)
        if order_id is not None:
            return self.order_fills
        # Reconciliation safety-net call (initialize_sync always makes this
        # first, unconditionally) - always a no-op empty result here, since
        # no test in this file depends on trade-log reconciliation.
        return []

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        order_id = self._next_order_id
        self._next_order_id += 1
        return {"orderId": order_id}


def rows_for(side: str, qty: float, avg_entry: float, symbol="SOLUSDT") -> list:
    amt = qty if side == "LONG" else -qty
    return [{"symbol": symbol, "positionAmt": str(amt), "entryPrice": str(avg_entry)}]


def open_position(manager, side, qty, avg_entry, dca_step, pending_order_id=None, pending_role=None):
    p = manager.position
    p.side = side
    p.status = "DCA_PENDING" if pending_role in ("dca",) else ("ENTERING" if pending_role == "initial" else "OPEN")
    p.avg_entry_price = avg_entry
    p.total_qty = qty
    p.original_qty = qty
    p.dca_step = dca_step
    p.entries = [(avg_entry, qty)]
    p.pending_order_id = pending_order_id
    p.pending_role = pending_role
    p.pending_order_ts = time.time() - (trading.SYNC_PENDING_GRACE_SEC + 5)  # grace already elapsed
    if pending_order_id is not None:
        manager._order_index[pending_order_id] = pending_role


# ---------------------------------------------------------------------------
# 1) DCA FILLED event arrives normally (baseline - unaffected by this patch)
# ---------------------------------------------------------------------------

async def test_normal_dca_fill_via_websocket():
    print("\n=== test_normal_dca_fill_via_websocket ===")
    manager = await make_manager()
    open_position(manager, "LONG", qty=1.0, avg_entry=100.0, dca_step=0)
    order_id = 9001
    manager._order_index[order_id] = "dca"
    event = {
        "o": {"i": order_id, "X": "FILLED", "ap": 98.0, "z": 1.0, "t": 1, "T": int(time.time() * 1000)},
        "E": int(time.time() * 1000),
    }
    await manager.handle_order_update(event)
    assert manager.position.dca_step == 1
    assert manager.position.dca_blocked is False
    assert manager.position.status == "OPEN"
    print(f"PASS: dca_step={manager.position.dca_step} dca_blocked={manager.position.dca_blocked}")


# ---------------------------------------------------------------------------
# 2) DCA FILLED WebSocket event dropped, REST confirms FILLED
# ---------------------------------------------------------------------------

async def test_missed_dca_fill_recovered_via_rest():
    print("\n=== test_missed_dca_fill_recovered_via_rest ===")
    manager = await make_manager()
    order_id = 9002
    # DCA #1 already happened; this is DCA #2 in flight whose WS event was
    # dropped. Exchange qty/avg_entry already reflect the fill (2.01 -> 2.93,
    # mirroring the LONG example in the incident report).
    open_position(manager, "LONG", qty=2.01, avg_entry=97.5, dca_step=1,
                   pending_order_id=order_id, pending_role="dca")
    client = FakeClient(
        position_rows=rows_for("LONG", 2.93, 97.9),
        order_status="FILLED",
        order_fills=[{"qty": "0.92", "price": "98.20", "realizedPnl": "0.0", "commission": "0.02", "commissionAsset": "USDT"}],
    )
    manager.client = client  # mirrors production, where manager.client IS the client passed to initialize_sync
    await trading.initialize_sync(client, manager, context="test", rows=None)

    assert manager.position.dca_step == 2, f"expected dca_step=2, got {manager.position.dca_step}"
    assert manager.position.dca_blocked is False
    assert manager.position.status == "OPEN"
    assert abs(manager.position.total_qty - 2.93) < 1e-6
    assert order_id not in manager._order_index, "order must be consumed exactly once"
    print(f"PASS: dca_step={manager.position.dca_step} total_qty={manager.position.total_qty} "
          f"dca_blocked={manager.position.dca_blocked}")


# ---------------------------------------------------------------------------
# 3) Duplicate WebSocket/REST processing of the same order never double-counts
# ---------------------------------------------------------------------------

async def test_duplicate_ws_then_rest_does_not_double_count():
    print("\n=== test_duplicate_ws_then_rest_does_not_double_count ===")
    manager = await make_manager()
    order_id = 9003
    open_position(manager, "SHORT", qty=1.0, avg_entry=100.0, dca_step=0,
                   pending_order_id=order_id, pending_role="dca")
    # The live WebSocket path processes the fill first (as it normally
    # would - REST recovery only ever runs after grace expiry, so in
    # practice this ordering is the common case).
    event = {
        "o": {"i": order_id, "X": "FILLED", "ap": 100.0, "z": 0.5, "t": 1, "T": int(time.time() * 1000)},
        "E": int(time.time() * 1000),
    }
    await manager.handle_order_update(event)
    assert manager.position.status == "OPEN"
    assert manager.position.dca_step == 1
    assert abs(manager.position.total_qty - 1.5) < 1e-9

    # Now a stale periodic-poll resync lands afterward and tries to REST-
    # resolve the SAME order_id (simulating a race where the poll had
    # already started before the WS fill applied). Must be a no-op.
    client = FakeClient(position_rows=rows_for("SHORT", 1.5, 100.0))
    resolution = await manager._resolve_pending_order_via_rest(client, order_id, "dca", "test")
    assert resolution == "filled"
    assert manager.position.dca_step == 1, "must not double-increment dca_step for the same order_id"
    assert abs(manager.position.total_qty - 1.5) < 1e-9, "must not double-apply the same order_id"
    print(f"PASS: dca_step stays {manager.position.dca_step}, total_qty stays "
          f"{manager.position.total_qty} after duplicate REST resolution")


# ---------------------------------------------------------------------------
# 4) Pending order remains NEW / PARTIALLY_FILLED
# ---------------------------------------------------------------------------

async def test_pending_order_still_new_blocks_nothing_but_waits():
    print("\n=== test_pending_order_still_new_blocks_nothing_but_waits ===")
    manager = await make_manager()
    order_id = 9004
    open_position(manager, "LONG", qty=1.0, avg_entry=100.0, dca_step=0,
                   pending_order_id=order_id, pending_role="dca")
    client = FakeClient(position_rows=rows_for("LONG", 1.0, 100.0), order_status="NEW")
    manager.client = client
    await trading.initialize_sync(client, manager, context="test", rows=None)

    assert manager.position.dca_step == 0, "must not advance while genuinely still pending"
    assert manager.position.dca_blocked is False, "NEW/PARTIALLY_FILLED is not an unknown/ambiguous result"
    assert manager.position.pending_order_id == order_id, "must keep waiting on the same order"
    print("PASS: still-NEW order leaves state untouched, no resync/DCA")


# ---------------------------------------------------------------------------
# 5) REST verification fails (network/API error) -> conservative block, never dca_step=0-and-continue
# ---------------------------------------------------------------------------

async def test_rest_verification_failure_blocks_dca_not_resets_step():
    print("\n=== test_rest_verification_failure_blocks_dca_not_resets_step ===")
    manager = await make_manager()
    order_id = 9005
    # Exchange qty already reflects a fill the process never confirmed
    # (dca_step really is 2 on Binance's side) - but with no saved
    # DCA-state snapshot to confirm it (simulates snapshot loss/never
    # written) AND a REST order-status lookup that itself fails.
    open_position(manager, "SHORT", qty=2.91, avg_entry=76.4, dca_step=1,
                   pending_order_id=order_id, pending_role="dca")
    client = FakeClient(position_rows=rows_for("SHORT", 2.91, 76.4), raise_on_get_order=True)
    manager.client = client
    await trading.initialize_sync(client, manager, context="test", rows=None)

    assert manager.position.dca_blocked is True, "ambiguous REST result must block further DCA"
    assert manager.position.dca_step != 5, "must never fabricate an inflated step count either"
    assert manager.position.status == "OPEN", "risk management (TP/Hard Stop) must keep working"
    assert abs(manager.position.total_qty - 2.91) < 1e-6, "qty must still come from the exchange, for correct risk sizing"
    print(f"PASS: dca_blocked={manager.position.dca_blocked} dca_step={manager.position.dca_step} "
          f"total_qty={manager.position.total_qty} status={manager.position.status}")

    # And the DCA trigger gate itself must actually honor the block, even
    # though dca_step (1) is still below MAX_DCA_STEPS (2). SHORT @ avg=76.4:
    # adverse move means price ABOVE avg_entry - a mild ~0.5% move, well
    # under HARD_STOP_PCT (2%), so this exercises the DCA gate specifically,
    # not the hard stop.
    manager.opened_at_guard = None
    manager.position.opened_at = time.time()
    manager.current_price = 76.78
    manager.prev_price = 76.5
    manager.prev_prev_price = 76.4
    manager.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.1, trend_direction=None, trend_confidence=0.0,
    )
    manager.last_regime = trading.RegimeReading(
        regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0,
    )
    order_index_before = dict(manager._order_index)
    await manager._manage_open_position()
    assert manager._order_index == order_index_before, "no new DCA order may be placed while dca_blocked"
    print("PASS: DCA trigger gate refuses to place a new order while dca_blocked=True")


# ---------------------------------------------------------------------------
# 6) Missed close FILLED event recovered via REST
# ---------------------------------------------------------------------------

async def test_missed_close_fill_recovered_via_rest():
    print("\n=== test_missed_close_fill_recovered_via_rest ===")
    manager = await make_manager()
    order_id = 9006
    open_position(manager, "LONG", qty=1.0, avg_entry=100.0, dca_step=1,
                   pending_order_id=order_id, pending_role="close")
    manager.position.status = "CLOSING"
    client = FakeClient(
        position_rows=[],  # exchange now flat - close_position()'s own post-fill verification will see this
        order_status="FILLED",
        order_fills=[{"qty": "1.0", "price": "101.5", "realizedPnl": "1.5", "commission": "0.05", "commissionAsset": "USDT"}],
    )
    manager.client = client
    await trading.initialize_sync(client, manager, context="test", rows=None)

    assert manager.position.status == "FLAT", f"expected FLAT after recovered close fill, got {manager.position.status}"
    assert manager.position.total_qty == 0.0
    print(f"PASS: status={manager.position.status} after REST-recovered close fill "
          f"(normal close bookkeeping ran instead of only later reconciliation)")


# ---------------------------------------------------------------------------
# 7) Snapshot mismatch immediately after a DCA fill whose REST resolution is
#    also ambiguous - the exact original incident shape, end to end
# ---------------------------------------------------------------------------

async def test_snapshot_mismatch_after_dca_fill_still_blocks_not_resets():
    print("\n=== test_snapshot_mismatch_after_dca_fill_still_blocks_not_resets ===")
    manager = await make_manager()
    order_id = 9007
    # No matching DCA-state snapshot exists at all for this exact
    # side/qty/avg_entry (simulates the snapshot being stale/never written
    # for this fill) AND the REST order-status lookup for the missed fill's
    # order_id also fails - the worst case, both safety nets miss at once.
    open_position(manager, "SHORT", qty=2.0, avg_entry=75.0, dca_step=0,
                   pending_order_id=order_id, pending_role="dca")
    client = FakeClient(position_rows=rows_for("SHORT", 2.91, 76.0), raise_on_get_order=True)
    manager.client = client
    await trading.initialize_sync(client, manager, context="test", rows=None)

    assert manager.position.dca_blocked is True
    assert manager.position.status == "OPEN"
    assert abs(manager.position.total_qty - 2.91) < 1e-6, "must still resync qty/avg_entry from exchange"
    print(f"PASS: dca_blocked={manager.position.dca_blocked} total_qty={manager.position.total_qty} "
          f"(never silently reset to dca_step=0-and-continue)")


# ---------------------------------------------------------------------------
# 8) Restart/resync with previous DCA activity - snapshot restores correctly
# ---------------------------------------------------------------------------

async def test_restart_resync_restores_dca_step_and_clears_block():
    print("\n=== test_restart_resync_restores_dca_step_and_clears_block ===")
    manager = await make_manager()
    manager.position.side = "LONG"
    manager.position.status = "OPEN"
    manager.position.avg_entry_price = 99.0
    manager.position.total_qty = 2.0
    manager.position.dca_step = 1
    manager.position.entries = [(100.0, 1.0), (98.0, 1.0)]
    manager.position.last_dca_order_id = 555
    manager.position.opened_at = time.time() - 300
    await manager.save_dca_state(reason="test setup")

    # Simulate a fresh process: brand-new PositionState, nothing in memory.
    manager.position = trading.PositionState()
    client = FakeClient(position_rows=rows_for("LONG", 2.0, 99.0))
    manager.client = client
    await trading.initialize_sync(client, manager, context="startup", rows=None)

    assert manager.position.dca_step == 1, f"expected dca_step=1 restored, got {manager.position.dca_step}"
    assert manager.position.dca_blocked is False, "a genuinely matching snapshot must clear the block"
    assert manager.position.status == "OPEN"
    print(f"PASS: dca_step={manager.position.dca_step} dca_blocked={manager.position.dca_blocked} "
          f"restored from snapshot across a simulated restart")


# ---------------------------------------------------------------------------
# 9) No further DCA once two completed steps are reached (the invariant itself)
# ---------------------------------------------------------------------------

async def test_max_dca_steps_hard_limit_never_exceeded():
    print("\n=== test_max_dca_steps_hard_limit_never_exceeded ===")
    manager = await make_manager()
    open_position(manager, "LONG", qty=3.0, avg_entry=95.0, dca_step=2)  # already at MAX_DCA_STEPS
    client = FakeClient(position_rows=rows_for("LONG", 3.0, 95.0))
    manager.client = client
    manager.position.opened_at = time.time()
    manager.current_price = 94.5  # ~0.5% adverse - well under HARD_STOP_PCT (2%)
    manager.prev_price = 94.7
    manager.prev_prev_price = 94.8
    manager.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.1, trend_direction=None, trend_confidence=0.0,
    )
    manager.last_regime = trading.RegimeReading(
        regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0,
    )
    await manager._manage_open_position()
    assert manager.position.dca_step == 2, "dca_step itself must never exceed MAX_DCA_STEPS"
    assert len(client.placed_orders) == 1
    assert client.placed_orders[0].get("reduceOnly") == "true", "only a risk-reducing close is allowed"
    assert manager.position.pending_role == "close"
    print(f"PASS: dca_step capped at {manager.position.dca_step}/{trading.MAX_DCA_STEPS}; "
          "hard boundary placed one reduceOnly close and no further DCA")


async def main():
    await test_normal_dca_fill_via_websocket()
    await test_missed_dca_fill_recovered_via_rest()
    await test_duplicate_ws_then_rest_does_not_double_count()
    await test_pending_order_still_new_blocks_nothing_but_waits()
    await test_rest_verification_failure_blocks_dca_not_resets_step()
    await test_missed_close_fill_recovered_via_rest()
    await test_snapshot_mismatch_after_dca_fill_still_blocks_not_resets()
    await test_restart_resync_restores_dca_step_and_clears_block()
    await test_max_dca_steps_hard_limit_never_exceeded()
    print("\nAll test_rest_fallback_dca_safety_fix tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
