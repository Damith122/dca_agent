"""
Regression tests for the DCA re-fire spacing fix (2026-08).

Root cause: the DCA trigger only ever checked pct_move relative to the
CURRENT, already-recalculated avg_entry_price. Since avg_entry_price
blends toward each DCA fill's price the instant it lands, price could
already be beyond -dca_distance_pct of the NEW avg_entry even though it
barely moved since the PREVIOUS DCA fill itself - letting two DCA steps
consume the same adverse move within a fraction of a second (observed on
Testnet SOLUSDT: DCA #1 and #2 both filling at ~77.08, ~0.09s apart).

Fix: for DCA #2 and later (p.dca_step >= 1), price must move ANOTHER full
dca_distance_pct beyond the anchor set by the PREVIOUS DCA fill
(last_dca_price - the exact existing field, now anchored to the actual
fill price rather than the pre-order mark price). DCA #1 is completely
unaffected - last_dca_price is None at that point, so only the existing
pct_move-vs-avg_entry check applies, unchanged.

Run directly: `python3 test_dca_spacing_fix.py`
"""
import os
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_dca_spacing_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_dca_spacing_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_dca_spacing_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_dca_spacing_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_dca_spacing_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_dca_spacing_dca_state.json")
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
os.environ.setdefault("MAX_HOLD_TIME_SEC", "999999")

import asyncio
import time

import dca2 as bot
import trading


def order_event(order_id, status, rp=0.0, n=0.0, N="USDT", ap=0.0, z=0.0):
    return {
        "o": {
            "i": order_id, "X": status, "rp": str(rp), "n": str(n), "N": N,
            "ap": str(ap), "z": str(z), "t": order_id * 10, "T": int(time.time() * 1000),
        }
    }


class FakeClient:
    def __init__(self):
        self.placed_orders = []
        self._next_id = 8000

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        oid = self._next_id
        self._next_id += 1
        return {"orderId": oid}

    async def get_position_risk(self, symbol):
        return [{"symbol": symbol, "positionAmt": "0", "entryPrice": "0"}]


async def make_manager(side="LONG"):
    filters = bot.SymbolFilters(tick_size=0.0001, step_size=0.01, min_qty=0.01, min_notional=5.0)
    m = bot.MartingaleManager(client=FakeClient(), symbol="SOLUSDT", filters=filters, leverage=20)
    m.position_sync_ready = True  # 2026-08 position_sync_ready gate: this test file exercises DCA/Max Hold management directly against an already-OPEN position, bypassing initialize_sync() - mark it ready so the new startup-readiness gate (unrelated to this file's own DCA-spacing fix) doesn't mask what's under test.
    m.position.side = side
    m.position.status = "OPEN"
    m.position.entries = [(77.08, 1.03)]
    m.position.avg_entry_price = 77.08
    m.position.total_qty = 1.03
    m.position.original_qty = 1.03
    m.position.dca_step = 0
    m.position.opened_at = time.time() - 300
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.2, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    m.prev_price = 77.08
    return m


async def tick(m, price):
    m.current_price = price
    m.prev_price = price
    await m._manage_open_position()


# ============================================================================
# TEST 1: Initial -> DCA #1 still works at the existing trigger (unchanged)
# ============================================================================
async def test1_initial_to_dca1_unchanged():
    m = await make_manager("LONG")
    dca_distance = m.get_dynamic_dca_distance_pct()
    trigger_price = 77.08 * (1 - dca_distance)
    await tick(m, trigger_price - 0.0001)  # just past the trigger
    print(f"TEST 1: dca_distance={dca_distance*100:.4f}%, orders placed={len(m.client.placed_orders)}, "
          f"dca_step (pending)={m.position.pending_role}")
    assert len(m.client.placed_orders) == 1, "DCA #1 must still trigger exactly at the existing pct_move-vs-avg_entry rule"
    print("TEST 1: PASS - initial -> DCA #1 trigger unchanged\n")


# ============================================================================
# TEST 2 & 3 (LONG): DCA #2 does NOT fire at same/near-same price as DCA #1's
# fill, only after another full dca_distance_pct move from that anchor
# ============================================================================
async def test2_3_dca2_requires_full_spacing_long():
    m = await make_manager("LONG")
    # Deliberately DIFFERENT initial-entry vs DCA#1-fill prices, so
    # avg_entry_price genuinely shifts away from last_dca_price after the
    # blend - this is what makes the bug reproducible: without it, a test
    # where both fills share one price can pass even with the OLD
    # (avg_entry-only) gate, since avg_entry and last_dca_price would be
    # numerically identical and prove nothing about which gate is doing
    # the blocking.
    m.position.entries = [(77.20, 1.03)]
    m.position.avg_entry_price = 77.20
    m.position.total_qty = 1.03
    dca_distance = m.get_dynamic_dca_distance_pct()

    m._order_index[7999] = "dca"
    await m.handle_order_update(order_event(7999, "FILLED", n=0.03, N="USDT", ap=77.05, z=1.6))
    print(f"After DCA #1: dca_step={m.position.dca_step}, last_dca_price={m.position.last_dca_price}, "
          f"avg_entry={m.position.avg_entry_price:.5f}")
    assert m.position.dca_step == 1
    assert m.position.last_dca_price == 77.05, "last_dca_price must be anchored to the ACTUAL fill price"
    assert m.position.avg_entry_price != m.position.last_dca_price, (
        "test setup check: avg_entry must genuinely differ from last_dca_price for this test to be meaningful"
    )

    # This price is BELOW the OLD gate's trigger (pct_move vs the newly
    # recalculated avg_entry) - the pre-fix code would have fired DCA #2
    # here - but is NOT yet a full dca_distance_pct below last_dca_price
    # (77.05). Proves the NEW gate, not just the pre-existing outer one,
    # is what's blocking it.
    old_gate_trigger = m.position.avg_entry_price * (1 - dca_distance)
    new_gate_trigger = m.position.last_dca_price * (1 - dca_distance)
    assert new_gate_trigger < old_gate_trigger, "test setup check: the gap window must exist"
    price_in_gap = (old_gate_trigger + new_gate_trigger) / 2
    await tick(m, price_in_gap)
    print(f"TEST 2: price={price_in_gap:.5f} (old avg_entry-based gate WOULD fire here: "
          f"price <= {old_gate_trigger:.5f}; new last_dca_price-based gate requires "
          f"price <= {new_gate_trigger:.5f}) -> orders placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 0, (
        "DCA #2 must NOT fire even though the OLD avg_entry-only gate would have allowed it"
    )
    print("TEST 2: PASS - DCA #2 does not immediately re-fire (the new gate, not just the old one, is blocking it)\n")

    # Move price to satisfy the NEW gate too - DCA #2 must now fire.
    await tick(m, new_gate_trigger - 0.0001)
    print(f"TEST 3: price moved a full dca_distance_pct beyond last_dca_price -> "
          f"orders placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 1, "DCA #2 must fire once spaced a full dca_distance_pct from last_dca_price"
    print("TEST 3: PASS - DCA #2 fires only after full adverse spacing\n")


# ============================================================================
# TEST 4: SHORT side works symmetrically
# ============================================================================
async def test4_short_symmetric():
    m = await make_manager("SHORT")
    dca_distance = m.get_dynamic_dca_distance_pct()
    m._order_index[7998] = "dca"
    await m.handle_order_update(order_event(7998, "FILLED", n=0.03, N="USDT", ap=77.08, z=1.6))
    assert m.position.dca_step == 1
    assert m.position.last_dca_price == 77.08

    # Near-same price -> must NOT trigger
    await tick(m, 77.08 + 0.001)
    print(f"TEST 4a (SHORT near-same price): orders placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 0

    # Full spacing beyond last_dca_price -> must trigger
    required_trigger = 77.08 * (1 + dca_distance)
    await tick(m, required_trigger + 0.0001)
    print(f"TEST 4b (SHORT full spacing): orders placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 1
    print("TEST 4: PASS - SHORT spacing symmetric with LONG\n")


# ============================================================================
# TEST 5: Restart-restored last_dca_price continues to enforce spacing
# ============================================================================
async def test5_restart_restored_spacing():
    filters = bot.SymbolFilters(tick_size=0.0001, step_size=0.01, min_qty=0.01, min_notional=5.0)
    m = bot.MartingaleManager(client=FakeClient(), symbol="SOLUSDT", filters=filters, leverage=20)
    rows = [{"positionAmt": "2.63", "entryPrice": "77.02", "symbol": "SOLUSDT"}]
    m.position.side = "LONG"
    m.position.status = "OPEN"
    m.position.avg_entry_price = 77.02
    m.position.total_qty = 2.63
    m.position.original_qty = 2.63
    m.position.entries = [(77.08, 1.03), (76.97, 1.6)]
    m.position.dca_step = 1
    m.position.last_dca_price = 76.97
    await m.save_dca_state(reason="test setup")

    m2 = bot.MartingaleManager(client=FakeClient(), symbol="SOLUSDT", filters=filters, leverage=20)
    await trading.initialize_sync(client=None, manager=m2, context="startup", rows=rows)
    print(f"TEST 5: after restart restore: dca_step={m2.position.dca_step}, "
          f"last_dca_price={m2.position.last_dca_price} (expected 76.97)")
    assert m2.position.dca_step == 1
    assert m2.position.last_dca_price == 76.97, "last_dca_price must be restored from the persisted snapshot"

    m2.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0)
    m2.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.2, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )

    # Price near the restored last_dca_price -> must NOT trigger
    await tick(m2, 76.96)
    print(f"TEST 5b: price near restored last_dca_price -> orders placed={len(m2.client.placed_orders)}")
    assert len(m2.client.placed_orders) == 0, "restart-restored last_dca_price must still enforce spacing"
    print("TEST 5: PASS - restart-restored last_dca_price enforces spacing correctly\n")


# ============================================================================
# TEST 6: once truly exhausted, the hard boundary closes instead of adding
# ============================================================================
async def test6_max_dca_exhausted_closes_without_new_dca():
    m = await make_manager("LONG")
    m.position.dca_step = trading.MAX_DCA_STEPS  # already fully exhausted
    m.position.last_dca_price = 76.50
    dca_distance = m.get_dynamic_dca_distance_pct()

    trigger_price = m.position.avg_entry_price * (1 - dca_distance)
    await tick(m, trigger_price - 0.0001)
    print(f"TEST 6: dca_step={m.position.dca_step} (exhausted), status={m.position.status}, "
          f"orders placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 0, "no new DCA must ever be placed once exhausted"
    assert m.position.status == "FLAT", "fake exchange is flat, so the hard close reconciles locally"
    print("TEST 6: PASS - max_dca_exhausted hard boundary never adds exposure\n")


async def main():
    await test1_initial_to_dca1_unchanged()
    await test2_3_dca2_requires_full_spacing_long()
    await test4_short_symmetric()
    await test5_restart_restored_spacing()
    await test6_max_dca_exhausted_closes_without_new_dca()
    print("ALL DCA SPACING FIX TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
