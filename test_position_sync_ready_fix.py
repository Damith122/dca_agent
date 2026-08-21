"""
Regression tests for the 2026-08 position_sync_ready startup-readiness gate
(items 1 and 3 of the follow-up safety review).

Root cause this addresses: a structurally VALID restored local snapshot
still only proves what THIS process last wrote to disk - it does not prove
Binance's current position is unchanged (manual intervention, a missed
fill, liquidation, etc. could have happened while the process was down).
Separately, rejecting an invalid snapshot to FLAT while initialize_sync()
is failing could let the entry engine open a brand-new position on top of
an unknown real exchange position.

Fix (this file's target):
  - MartingaleManager.position_sync_ready starts False on every fresh
    manager. A local/GitHub snapshot restore (load_dca_state()) never
    sets it True. Only initialize_sync() sets it True, and ONLY at one of
    three precise points where a final, authoritative PositionState has
    just been installed with ZERO await in between: (1) the
    already-synced short-circuit, (2) the exchange-confirmed-flat branch,
    or (3) the very end of the full rebuild path (2026-08 timing
    correction - an earlier version of this fix set the flag as soon as
    `rows` was obtained, before pending-order recovery/snapshot
    matching/the final rebuild had actually happened, which is too early;
    see test_readiness_timing_barrier below).
  - While False: on_price_tick() blocks all new entries; the Max Hold V2
    review block and the Smart Exit block are both skipped entirely
    (their own "no meaningful loss"/discretionary decisions never run);
    new DCA adds are withheld. Hard Stop / Profit Lock / TP / Trailing /
    Breakeven remain fully active regardless - they are simple,
    deterministic, already qty-safe (close_position() re-fetches the
    exchange's own positionAmt immediately before submitting) reduceOnly
    exits, never exposure-adding or provisional-economics-dependent.
  - close_position()/_place_step_order() now re-verify
    `self.position is expected_position` a SECOND time, immediately
    before mutating pending state / submitting the actual order - not
    just once at function entry - so a concurrent position swap landing
    during an intervening await (e.g. close_position()'s own fresh-
    position REST fetch) can never let a stale decision through either.

Correction: the Live incident's zero-quantity Max Hold decision did NOT
"defer" - with total_qty corrupted to 0, meaningful_loss evaluated False,
dca_opportunity_available was also False (dca_step already 2/2), and
nothing else kept still_deferring True, so Max Hold V2 classified the
position as "no meaningful loss / not protected at timeout" and CLOSED it
immediately via close_position() - it did not sit deferring indefinitely.

Run directly: `python3 test_position_sync_ready_fix.py`
"""
import os
# 2026-08-20 multi-coin: declare the symbol this suite actually exercises.
# Persistence paths are now derived per-manager from its own symbol, so a
# suite that builds SOLUSDT managers while config.SYMBOL sat at the
# BTCUSDT default would resolve its explicit *_PATH overrides against the
# wrong symbol. The mismatch was always latent; symbol-scoped paths
# surface it.
os.environ.setdefault("SYMBOL", "SOLUSDT")
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SMART_EXIT_ENABLED", "true")
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "true")
os.environ.setdefault("MAX_DCA_STEPS", "2")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_sync_ready_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_sync_ready_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_sync_ready_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_sync_ready_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_sync_ready_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_sync_ready_dca_state.json")
os.environ.setdefault("BRAIN2_WARMUP_UPDATES", "1")

import asyncio
import io
import sys
import time

import dca2 as bot
import trading
from trading import PositionState

DCA_STATE_PATH = os.environ["DCA_STATE_PATH"]


class FakeClient:
    def __init__(self, fail_position_risk=True, qty=3.55, side="LONG", entry_price=95.0):
        self.placed_orders = []
        self._next_id = 9900
        self.fail_position_risk = fail_position_risk
        self.qty = qty
        self.side = side
        self.entry_price = entry_price

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        oid = self._next_id
        self._next_id += 1
        return {"orderId": oid}

    async def get_position_risk(self, symbol):
        if self.fail_position_risk:
            raise trading.BinanceApiError(418, {"code": -1003, "msg": "IP banned"})
        amt = self.qty if self.side == "LONG" else -self.qty
        return [{"symbol": symbol, "positionAmt": str(amt), "entryPrice": str(self.entry_price)}]


async def make_manager(client=None):
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    m = bot.MartingaleManager(client=client, symbol="SOLUSDT", filters=filters, leverage=20)
    return m


class Capture:
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


async def make_warmed_up_manager_for_entry(client):
    """Builds a manager whose entry pipeline is fully warmed (enough
    candle history + a ready Brain) so an entry WOULD fire if not for
    position_sync_ready - proves the gate, not warm-up, is what's
    blocking it."""
    m = await make_manager(client)
    price = 100.0
    for i in range(max(trading.EMA_SLOW, trading.ATR_PERIOD) + 10):
        price += 0.05
        m.candles.on_price(price)
        m.update_price_history(price)
    m.current_price = price
    m.prev_price = price
    m.prev_prev_price = price
    # Force Brain "ready" without a real training loop.
    m.brain.update_count = m.brain.warmup_updates + 1
    return m


# ============================================================================
# TEST 1: invalid snapshot -> local FLAT + failed authoritative sync +
# fully warmed entry signal -> zero entry orders (position_sync_ready
# blocks entries even though local state looks like a clean, entry-eligible
# FLAT).
# ============================================================================
async def test_invalid_snapshot_flat_plus_failed_sync_blocks_entries():
    print("\n=== test_invalid_snapshot_flat_plus_failed_sync_blocks_entries ===")
    client = FakeClient(fail_position_risk=True)
    m = await make_warmed_up_manager_for_entry(client)
    assert m.position_sync_ready is False, "setup check: a fresh manager must start not-ready"
    assert m.position.status == "FLAT"

    # Startup sync attempted and failed (simulates the 418-ban incident).
    await trading.initialize_sync(client, m, context="startup")
    assert m.position_sync_ready is False, "a failed REST fetch must never set position_sync_ready"

    with Capture() as cap:
        for _ in range(20):
            await m.on_price_tick()

    print(f"TEST 1: position_sync_ready={m.position_sync_ready} orders_placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 0, "no entry may be opened while position_sync_ready is False"
    assert "[entry-skip] position_sync_ready=False" in cap.text
    print("TEST 1: PASS - entries fully blocked until authoritative sync confirms Binance is flat\n")


# ============================================================================
# TEST 2: valid restored OPEN snapshot + failed sync -> zero DCA and zero
# Max Hold/Smart Exit discretionary close. Hard Stop remains active.
# ============================================================================
async def test_valid_snapshot_failed_sync_blocks_discretionary_not_hard_stop():
    print("=== test_valid_snapshot_failed_sync_blocks_discretionary_not_hard_stop ===")
    client = FakeClient(fail_position_risk=True, qty=3.55, side="LONG", entry_price=95.0)
    m = await make_manager(client)
    m.position = PositionState(
        status="OPEN", side="LONG", avg_entry_price=95.0, total_qty=3.55,
        dca_step=1, opened_at=time.time() - 999999,  # long past every Max Hold threshold
        entries=[(95.5, 1.55), (94.5, 2.0)],
    )
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.9, trend_direction="SHORT",
        trend_confidence=0.8, success_probability=0.5, tp_hit_probability=0.5,
    )

    # Sync fails - position_sync_ready stays False, but the snapshot's
    # economics are otherwise perfectly valid (real qty/side/avg_entry).
    await trading.initialize_sync(client, m, context="startup")
    assert m.position_sync_ready is False

    with Capture() as cap:
        for i in range(50):
            # Deep adverse move (would be a genuine Max Hold/Smart Exit
            # CLOSE candidate) but stays inside HARD_STOP_PCT so only the
            # discretionary paths would normally fire.
            m.current_price = 95.0 * (1 - 0.005 - i * 0.0001)
            m.prev_price = m.current_price
            await m._manage_open_position()

    print(f"TEST 2: status={m.position.status} orders={len(m.client.placed_orders)}")
    assert m.position.status == "OPEN", "position must remain open - no discretionary close while unready"
    assert len(m.client.placed_orders) == 0, "no DCA and no discretionary close order while unready"
    assert "no meaningful loss" not in cap.text
    assert "[max-hold-skip]" in cap.text or "position_sync_ready=False" in cap.text
    print("TEST 2: PASS - Max Hold/Smart Exit/DCA all withheld; genuine economics still not closed on\n")

    # Now confirm Hard Stop is NOT blocked - a real breach still closes
    # immediately even while position_sync_ready is False.
    m2 = await make_manager(FakeClient(fail_position_risk=True, qty=3.55, side="LONG", entry_price=95.0))
    m2.position = PositionState(
        status="OPEN", side="LONG", avg_entry_price=95.0, total_qty=3.55,
        dca_step=1, opened_at=time.time(),
    )
    m2.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0)
    m2.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.1, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    assert m2.position_sync_ready is False
    m2.current_price = 95.0 * (1 - trading.HARD_STOP_PCT - 0.001)
    m2.prev_price = m2.current_price
    await m2._manage_open_position()
    print(f"TEST 2b: (Hard Stop while unready) status={m2.position.status} orders={len(m2.client.placed_orders)}")
    assert m2.position.status in ("CLOSING", "FLAT"), "Hard Stop must remain active regardless of readiness"
    assert len(m2.client.placed_orders) == 1
    print("TEST 2b: PASS - Hard Stop (deterministic reduceOnly exit) unaffected by readiness gate\n")


# ============================================================================
# TEST 3: successful sync flips readiness and restores normal management.
# ============================================================================
async def test_successful_sync_flips_readiness_and_resumes_management():
    print("=== test_successful_sync_flips_readiness_and_resumes_management ===")
    # Test isolation: remove any DCA-state snapshot left on disk by an
    # earlier test in this file (e.g. test 2's close-pending snapshot) so
    # this test's initialize_sync() call starts from a clean slate rather
    # than accidentally matching a leftover snapshot with the same
    # side/qty/avg_entry and restoring its (unrelated) pending-close state.
    if os.path.exists(DCA_STATE_PATH):
        os.remove(DCA_STATE_PATH)
    client = FakeClient(fail_position_risk=False, qty=3.55, side="LONG", entry_price=95.0)
    m = await make_manager(client)
    assert m.position_sync_ready is False

    await trading.initialize_sync(client, m, context="startup")
    print(f"TEST 3: position_sync_ready after successful sync={m.position_sync_ready} "
          f"status={m.position.status} total_qty={m.position.total_qty}")
    assert m.position_sync_ready is True, "a successful authoritative sync must flip readiness"
    assert m.position.status == "OPEN"
    assert m.position.total_qty == 3.55

    # Max Hold now resumes normally: push well past the hard cap with no
    # recovery signals and confirm it actually closes (whereas before sync
    # it would have been skipped entirely).
    m.position.opened_at = time.time() - (trading.MAX_HOLD_TIME_HARD_CAP_SEC + 60)
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.1, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    m.current_price = 95.2
    m.prev_price = 95.2
    await m._manage_open_position()
    print(f"TEST 3b: status={m.position.status}")
    assert m.position.status in ("CLOSING", "FLAT"), "Max Hold Time V2 must resume normal operation post-sync"
    print("TEST 3: PASS - readiness flips True after successful sync, normal management resumes\n")


# ============================================================================
# TEST 4 (item 3): stale state revalidated immediately before the actual
# order boundary, AFTER any intervening await - not just at function entry.
# Uses an async barrier so self.position is swapped DURING
# close_position()'s own fresh-position REST fetch, after the function has
# already begun and passed its entry-point check.
# ============================================================================
async def test_stale_state_revalidated_after_intervening_await():
    print("=== test_stale_state_revalidated_after_intervening_await ===")

    class BarrierClient:
        """Reports a matching exchange position (so close_position()
        proceeds past its qty fetch) but only resolves that REST call
        after the test has swapped manager.position out from under the
        in-flight close_position() call - simulating exactly the race
        window between the fetch starting and returning."""
        def __init__(self, manager_ref, old_p, new_p):
            self.placed_orders = []
            self.manager_ref = manager_ref
            self.old_p = old_p
            self.new_p = new_p
            self.swap_done = False

        async def get_position_risk(self, symbol):
            # This await point IS the race window: swap self.position here,
            # after close_position() has already passed its entry-point
            # expected_position check, before it reaches the actual
            # order-submission boundary.
            await asyncio.sleep(0)
            if not self.swap_done:
                self.swap_done = True
                self.manager_ref.position = self.new_p
            return [{"symbol": symbol, "positionAmt": "3.55", "entryPrice": "95.0"}]

        async def place_order(self, **kwargs):
            self.placed_orders.append(kwargs)
            return {"orderId": 12345}

    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    m = bot.MartingaleManager(client=None, symbol="SOLUSDT", filters=filters, leverage=20)
    old_p = PositionState(status="OPEN", side="LONG", avg_entry_price=95.0, total_qty=3.55, dca_step=1)
    new_p = PositionState(status="OPEN", side="SHORT", avg_entry_price=200.0, total_qty=1.0, dca_step=0)
    m.position = old_p
    client = BarrierClient(m, old_p, new_p)
    m.client = client

    pending_order_id_before = new_p.pending_order_id
    status_before = new_p.status

    with Capture() as cap:
        await m.close_position("stale-state async-barrier test", expected_position=old_p)

    print(f"TEST 4: orders_placed={len(client.placed_orders)} "
          f"new_p.status unchanged={new_p.status == status_before} "
          f"new_p.pending_order_id unchanged={new_p.pending_order_id == pending_order_id_before} "
          f"self.position is new_p={m.position is new_p}")
    assert len(client.placed_orders) == 0, (
        "zero orders may be placed when the position changed during an intervening await"
    )
    assert new_p.status == status_before, "the replacement position's status must not be mutated"
    assert new_p.pending_order_id == pending_order_id_before, "the replacement position's pending state must not be mutated"
    assert m.position is new_p, "the replacement object itself must be left untouched"
    assert "skipped immediately before submission" in cap.text, (
        "the SECOND (post-await) stale-decision-guard check must be what caught this, not the first"
    )
    print("TEST 4: PASS - stale decision caught after the intervening REST fetch, zero orders, zero mutation\n")


# ============================================================================
# TEST 5: same async-barrier race, but for _place_step_order() (DCA add).
# ============================================================================
async def test_stale_state_revalidated_for_dca_add():
    print("=== test_stale_state_revalidated_for_dca_add ===")
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    m = bot.MartingaleManager(client=None, symbol="SOLUSDT", filters=filters, leverage=20)
    old_p = PositionState(status="OPEN", side="LONG", avg_entry_price=95.0, total_qty=3.55, dca_step=1)
    new_p = PositionState(status="OPEN", side="SHORT", avg_entry_price=200.0, total_qty=1.0, dca_step=0)
    m.position = old_p
    m.current_price = 94.0

    class NoOpClient:
        def __init__(self):
            self.placed_orders = []

        async def place_order(self, **kwargs):
            self.placed_orders.append(kwargs)
            return {"orderId": 54321}

    m.client = NoOpClient()

    # Swap position BEFORE calling (there are no awaits between the entry
    # check and the DRY_RUN/order-submission boundary in
    # _place_step_order(), so this proves the check catches an already-
    # stale expected_position deterministically at the actual boundary).
    m.position = new_p

    with Capture() as cap:
        await m._place_step_order(step=2, side_signal="LONG", size_mult=1.0, expected_position=old_p)

    print(f"TEST 5: orders_placed={len(m.client.placed_orders)} new_p.dca_step unchanged={new_p.dca_step == 0}")
    assert len(m.client.placed_orders) == 0
    assert new_p.status == "OPEN" and new_p.pending_order_id is None
    print("TEST 5: PASS - stale DCA decision never reaches order submission\n")


# ============================================================================
# TEST 6 (timing correction): position_sync_ready must NOT flip True until
# initialize_sync() has fully reconciled AND installed the final
# authoritative PositionState - not merely once exchange rows have been
# obtained. Uses an async barrier to pause initialize_sync() mid-
# reconciliation (at its own await for a DCA-state snapshot lookup, well
# BEFORE the final PositionState is installed) and proves nothing can act
# on provisional state during that window.
# ============================================================================
async def test_readiness_timing_barrier_blocks_until_final_state_installed():
    print("=== test_readiness_timing_barrier_blocks_until_final_state_installed ===")
    # Test isolation: remove any DCA-state snapshot left on disk by an
    # earlier test in this file (see test 3's identical cleanup) so this
    # test's initialize_sync() call starts from a clean slate.
    if os.path.exists(DCA_STATE_PATH):
        os.remove(DCA_STATE_PATH)
    client = FakeClient(fail_position_risk=False, qty=3.55, side="LONG", entry_price=95.0)
    m = await make_warmed_up_manager_for_entry(client)
    assert m.position_sync_ready is False
    assert m.position.status == "FLAT"

    pause_event = asyncio.Event()    # set once initialize_sync() has reached the barrier
    release_event = asyncio.Event()  # set by the test to let it continue

    real_snapshot_lookup = m.load_dca_state_snapshot

    async def paused_snapshot_lookup():
        # This await sits INSIDE initialize_sync()'s full-rebuild path,
        # AFTER rows were already obtained and the already-synced/
        # exchange-flat short-circuits were both already ruled out (row is
        # a genuine new OPEN position, not previously known locally), but
        # BEFORE the final `manager.position = PositionState(...)`
        # assignment. Exactly the "exchange rows obtained but final state
        # not yet installed" window this test targets.
        pause_event.set()
        await release_event.wait()
        return await real_snapshot_lookup()

    m.load_dca_state_snapshot = paused_snapshot_lookup

    task = asyncio.create_task(trading.initialize_sync(client, m, context="startup"))
    await pause_event.wait()

    print(f"TEST 6a: mid-reconciliation -> position_sync_ready={m.position_sync_ready} "
          f"local status={m.position.status}")
    assert m.position_sync_ready is False, "readiness must still be False mid-reconciliation"
    assert m.position.status == "FLAT", "the final authoritative state must not be installed yet"

    # A fully warmed entry signal, sent WHILE reconciliation is paused.
    with Capture() as cap_entry:
        for _ in range(10):
            await m.on_price_tick()
    print(f"TEST 6b: entry orders during pause={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 0, "zero entry orders may be placed while reconciliation is unfinished"

    # Directly exercise DCA/Max Hold/Smart Exit against a temporary
    # provisional OPEN position, still during the SAME pause - proves
    # those decisions are blocked too, not just fresh entries. The
    # background initialize_sync() task already captured its own
    # reference to the position object it will eventually replace, so
    # this substitution does not interfere with it.
    m.position = PositionState(
        status="OPEN", side="LONG", avg_entry_price=95.0, total_qty=3.55,
        dca_step=1, opened_at=time.time() - 999999,
    )
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.9, trend_direction="SHORT",
        trend_confidence=0.8, success_probability=0.5, tp_hit_probability=0.5,
    )
    # Adverse enough to normally trigger a NEW DCA add and (given
    # opened_at far in the past) Max Hold's hard cap - but safely inside
    # HARD_STOP_PCT (2%) so Hard Stop itself (a deterministic reduceOnly
    # exit, intentionally still active regardless of readiness) does not
    # fire and mask what's actually under test here.
    m.current_price = 95.0 * (1 - 0.0053)
    m.prev_price = m.current_price
    await m._manage_open_position()
    print(f"TEST 6c: DCA/MaxHold/SmartExit orders during pause={len(m.client.placed_orders)} "
          f"position_sync_ready={m.position_sync_ready}")
    assert len(m.client.placed_orders) == 0, "zero DCA/Max Hold/Smart Exit orders while reconciliation is unfinished"
    assert m.position_sync_ready is False, "readiness must remain False throughout the entire pause"

    # Release the barrier and let reconciliation finish.
    release_event.set()
    await task

    print(f"TEST 6d: after reconciliation completes -> position_sync_ready={m.position_sync_ready} "
          f"status={m.position.status} side={m.position.side} total_qty={m.position.total_qty}")
    assert m.position.status == "OPEN", "the authoritative exchange position must now be installed"
    assert m.position.side == "LONG" and m.position.total_qty == 3.55
    assert m.position_sync_ready is True, "readiness must flip True only AFTER the final state is installed"
    print("TEST 6: PASS - readiness stays False for the entire reconciliation window, "
          "flips True only once the authoritative position is installed\n")


async def main():
    await test_invalid_snapshot_flat_plus_failed_sync_blocks_entries()
    await test_valid_snapshot_failed_sync_blocks_discretionary_not_hard_stop()
    await test_successful_sync_flips_readiness_and_resumes_management()
    await test_stale_state_revalidated_after_intervening_await()
    await test_stale_state_revalidated_for_dca_add()
    await test_readiness_timing_barrier_blocks_until_final_state_installed()
    print("ALL POSITION_SYNC_READY / STALE-BOUNDARY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
