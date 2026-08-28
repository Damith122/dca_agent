"""
Focused regression test for the 2026-08 fill-tracking race fix
(_unmatched_fills / _register_order_and_replay / handle_order_update /
initialize_sync's ENTERING/DCA_PENDING grace bypass) and the 2026-08
fee/slippage-safe Profit Lock fix.

Exercises the real MartingaleManager and initialize_sync from trading.py
via dca2.py, with DRY_RUN=true so no network calls happen - same
environment-setup pattern as smoke_test.py. Not part of smoke_test.py
itself since this targets these specific fixes in isolation.

Run from the repo root: python3 test_fill_race_fix.py
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
# NOTE: initialize_sync() itself starts with `if DRY_RUN: return` (correct
# production behavior - nothing real to sync against in dry-run), so
# testing it here needs DRY_RUN=false. None of these tests ever call
# _place_step_order()/close_position() with a real client (manager.client
# stays None throughout), so this is safe - nothing here actually reaches
# a live REST call.
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_performance_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_performance_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_brain_v2.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_dca_state.json")
os.environ.setdefault("BRAIN2_WARMUP_UPDATES", "5")

import asyncio
import time

import dca2 as bot
import trading


async def make_manager():
    filters = bot.SymbolFilters(tick_size=0.1, step_size=0.0001, min_qty=0.0001, min_notional=5.0)
    manager = bot.MartingaleManager(client=None, symbol="BTCUSDT", filters=filters, leverage=40)
    return manager


def fake_order_update_event(order_id: int, fill_price: float, fill_qty: float) -> dict:
    """Mimics a Binance ORDER_TRADE_UPDATE user-data-stream payload shape,
    just the fields handle_order_update() actually reads."""
    return {
        "o": {
            "i": order_id,
            "X": "FILLED",
            "ap": fill_price,
            "z": fill_qty,
            "t": 1,
            "T": int(time.time() * 1000),
        },
        "E": int(time.time() * 1000),
    }


async def test_race_condition_fill_is_not_lost():
    """The exact bug: a FILLED websocket event for this process's own DCA
    order arrives BEFORE the order is registered in _order_index (simulated
    directly, since the real race is a timing accident). Must be buffered
    and then replayed the moment registration happens - not dropped."""
    print("\n=== test_race_condition_fill_is_not_lost ===")
    manager = await make_manager()
    manager.position.side = "SHORT"
    manager.position.status = "OPEN"
    manager.position.avg_entry_price = 62825.0
    manager.position.total_qty = 0.0019
    manager.position.dca_step = 0
    manager.position.entries = [(62825.0, 0.0019)]

    order_id = 26726553044
    # Simulate the websocket delivering the FILLED event first.
    event = fake_order_update_event(order_id, fill_price=62944.46, fill_qty=0.0011)
    await manager.handle_order_update(event)

    assert order_id in manager._unmatched_fills, "fill must be buffered, not dropped"
    assert manager.position.dca_step == 0, "must not advance before registration"

    # Now the placing coroutine "catches up" and registers the order -
    # this is what _place_step_order() does internally after this fix.
    already_filled = await manager._register_order_and_replay(order_id, "dca")

    assert already_filled is True
    assert order_id not in manager._unmatched_fills
    assert manager.position.dca_step == 1, f"expected dca_step=1, got {manager.position.dca_step}"
    assert manager.position.status == "OPEN"
    assert manager.position.pending_order_id is None
    print(f"PASS: dca_step={manager.position.dca_step}, status={manager.position.status}")


async def test_normal_fill_order_unaffected():
    """Regression guard: registration-before-fill (the common case) must
    behave exactly as before this fix."""
    print("\n=== test_normal_fill_order_unaffected ===")
    manager = await make_manager()
    manager.position.side = "LONG"
    manager.position.status = "OPEN"
    manager.position.avg_entry_price = 63000.0
    manager.position.total_qty = 0.001
    manager.position.dca_step = 0
    manager.position.entries = [(63000.0, 0.001)]

    order_id = 555111
    already_filled = await manager._register_order_and_replay(order_id, "dca")
    assert already_filled is False

    event = fake_order_update_event(order_id, fill_price=63010.0, fill_qty=0.0016)
    await manager.handle_order_update(event)
    assert manager.position.dca_step == 1
    assert manager.position.status == "OPEN"
    print(f"PASS: dca_step={manager.position.dca_step}, status={manager.position.status}")


async def test_unmatched_fill_ttl_prunes():
    """A fill that's never claimed (genuinely foreign/stale) must not sit in
    the buffer forever."""
    print("\n=== test_unmatched_fill_ttl_prunes ===")
    manager = await make_manager()
    manager._UNMATCHED_FILL_TTL_SEC = 0.05
    order_id = 1
    await manager.handle_order_update(fake_order_update_event(order_id, 1.0, 1.0))
    assert order_id in manager._unmatched_fills
    await asyncio.sleep(0.1)
    manager._prune_unmatched_fills()
    assert order_id not in manager._unmatched_fills
    print("PASS: stale unmatched fill pruned")


async def test_initialize_sync_grace_prevents_false_resync():
    """The other half of the fix: initialize_sync() must not force a full
    RESYNCING TO MATCH EXCHANGE (and dca_step reset) for a DCA_PENDING
    position whose own order was placed moments ago."""
    print("\n=== test_initialize_sync_grace_prevents_false_resync ===")
    manager = await make_manager()
    manager.position.side = "SHORT"
    manager.position.status = "DCA_PENDING"
    manager.position.dca_step = 2
    manager.position.avg_entry_price = 62944.46
    manager.position.total_qty = 0.0093
    manager.position.pending_order_ts = time.time()  # placed "now"

    class FakeClient:
        async def get_position_risk(self, symbol):
            return [{"positionAmt": "-0.0143", "entryPrice": "63065.51"}]

    # Negative positionAmt = SHORT per Binance's sign convention (matches
    # manager.position.side="SHORT" above - same side, just a qty the local
    # DCA_PENDING state hasn't caught up to yet).
    rows = [{"positionAmt": "-0.0143", "entryPrice": "63065.51"}]
    await trading.initialize_sync(FakeClient(), manager, context="test", rows=rows)

    assert manager.position.dca_step == 2, (
        f"dca_step must be preserved during the grace window, got {manager.position.dca_step}"
    )
    print(f"PASS: dca_step preserved at {manager.position.dca_step} during grace window")

    # After the grace window elapses, a genuine same-side mismatch should
    # still resync (safety net must still work).
    manager.position.pending_order_ts = time.time() - 30
    await trading.initialize_sync(FakeClient(), manager, context="test", rows=rows)
    assert manager.position.status == "OPEN"
    print(f"PASS: genuine stale mismatch still resyncs after grace expiry "
          f"(status={manager.position.status}, dca_step={manager.position.dca_step})")


async def test_profit_lock_requires_fee_safe_floor():
    """2026-08 fee/slippage-safe Profit Lock fix: a thin margin (below
    MIN_NET_PROFIT_USDT) at/below the locked level must NOT trigger a
    close - only a margin that also clears the same fee-aware floor
    TP/Partial TP already use should."""
    print("\n=== test_profit_lock_requires_fee_safe_floor ===")
    manager = await make_manager()
    manager.position.side = "LONG"
    manager.position.status = "OPEN"
    manager.position.avg_entry_price = 60000.0
    manager.position.total_qty = 0.01
    manager.position.entries = [(60000.0, 0.01)]
    manager.position.opened_at = time.time() - 1000
    manager.position.profit_lock_active = True
    # peak was only just above activation - locked_profit = 0.5 * peak
    manager.position.peak_unrealized_pnl = 0.11
    close_calls = []

    async def fake_close_position(reason, emergency=False, exit_reason_tag="manual", expected_position=None):
        close_calls.append(exit_reason_tag)

    manager.close_position = fake_close_position
    # Force current unrealized pnl estimate to a thin positive margin
    # (below trading.MIN_NET_PROFIT_USDT) that is still <= locked_profit.
    #
    # 2026-08-19 P1: Profit Lock now reads estimate_net_pnl_usdt_EXECUTABLE()
    # (best_bid/best_ask + actual accumulated commission) rather than
    # estimate_net_pnl_usdt() (mid/mark + both legs guessed at TAKER_FEE_RATE),
    # because the old basis decided on a price the close could not achieve - it
    # closed a live trade at a realized -$0.0170 on a +$0.0936 estimate. Both
    # are stubbed here so this test pins the FLOOR behaviour it was written for,
    # independent of which estimator feeds it.
    manager.estimate_net_pnl_usdt = lambda price, qty=None: 0.03
    manager.estimate_net_pnl_usdt_executable = lambda extra_qty=0.0, extra_entry_price=None: 0.03
    manager.current_price = 60050.0
    manager.candles.on_price(60050.0)

    await manager._manage_open_position()
    assert "profit_lock" not in close_calls, (
        "Profit Lock must not close on a margin below MIN_NET_PROFIT_USDT"
    )
    print(f"PASS: thin margin (0.03 < MIN_NET_PROFIT_USDT) did not trigger profit_lock close")

    # Now a margin that clears BOTH the fee-safe floor (>= 0.05) AND is
    # still at/below locked_profit (peak 0.11 * ratio 0.5 = 0.055) should
    # close exactly as before this fix.
    manager.estimate_net_pnl_usdt = lambda price, qty=None: 0.052
    manager.estimate_net_pnl_usdt_executable = lambda extra_qty=0.0, extra_entry_price=None: 0.052
    await manager._manage_open_position()
    assert "profit_lock" in close_calls, "Profit Lock must still close once above the fee-safe floor"
    print("PASS: margin above MIN_NET_PROFIT_USDT still triggers profit_lock close as before")


async def main():
    await test_race_condition_fill_is_not_lost()
    await test_normal_fill_order_unaffected()
    await test_unmatched_fill_ttl_prunes()
    await test_initialize_sync_grace_prevents_false_resync()
    await test_profit_lock_requires_fee_safe_floor()
    print("\nALL FILL-RACE-FIX + PROFIT-LOCK-FIX TESTS PASSED")


asyncio.run(main())
