"""
Regression tests for the 2026-08 post-fill DCA resync-race fix
(PositionState.last_fill_ts / initialize_sync()'s OPEN-status grace block).

Root cause: _on_entry_filled() moves a position straight to status="OPEN"
the instant a fill is confirmed locally (via the user-data-stream
WebSocket) - this happens BEFORE the existing ENTERING/DCA_PENDING grace
in initialize_sync() can apply, since that grace only covers ENTERING/
DCA_PENDING statuses. Binance's own REST GET /fapi/v1/positionRisk
endpoint (what initialize_sync() polls) can still echo the PRE-fill
qty/avg_entry for a second or two after that.

Observed on Testnet SOLUSDT: DCA #2 filled locally (total_qty=3.28,
dca_step=2), then ~1.5s later a periodic poll landed mid-lag, saw the
exchange still reporting the pre-DCA qty=1.94, treated it as a genuine
mismatch, and rebuilt the position with dca_step reset to 0 - which then
let the bot re-fire a DCA it had already placed (exchange qty eventually
reached 4.17, exceeding the intended initial + 2 DCA sequence).

Fix: PositionState.last_fill_ts is stamped by _on_entry_filled() at the
moment of every confirmed fill (initial or DCA). initialize_sync() reuses
the existing SYNC_PENDING_GRACE_SEC window (no new ENV var): while a
same-side OPEN position is within that window of its last confirmed fill
and the exchange-reported qty has not yet reached local qty, the rebuild
is skipped and the poll waits for a later sync instead.

Run directly: `python3 test_dca_resync_race_fix.py`
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
# initialize_sync() itself starts with `if DRY_RUN: return` - needs
# DRY_RUN=false to exercise it here, same as test_fill_race_fix.py. No
# test below ever reaches a real network call (rows is always passed in
# directly, and manager.client stays None).
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_dca_resync_race_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_dca_resync_race_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_dca_resync_race_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_dca_resync_race_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_dca_resync_race_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_dca_resync_race_dca_state.json")
os.environ.setdefault("BRAIN2_WARMUP_UPDATES", "5")

import asyncio
import time

import dca2 as bot
import trading


async def make_manager():
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    manager = bot.MartingaleManager(client=None, symbol="SOLUSDT", filters=filters, leverage=20)
    return manager


class FakeClient:
    """initialize_sync() only calls get_position_risk() when `rows` isn't
    passed directly - every test below passes `rows` explicitly, but this
    stub exists so the call signature stays valid if that ever changes."""
    def __init__(self, rows):
        self._rows = rows

    async def get_position_risk(self, symbol):
        return self._rows


def rows_for(side: str, qty: float, avg_entry: float) -> list:
    amt = qty if side == "LONG" else -qty
    return [{"positionAmt": str(amt), "entryPrice": str(avg_entry)}]


async def test_dca2_fill_then_stale_sync_does_not_reset_step():
    """The exact Testnet sequence: DCA #2 fills locally (dca_step -> 2,
    total_qty -> 3.28), then a periodic sync ~1.5s later still reports the
    OLDER pre-fill exchange qty (1.94). Must NOT rebuild / reset dca_step,
    and must not allow a duplicate DCA to fire off a corrupted state."""
    print("\n=== test_dca2_fill_then_stale_sync_does_not_reset_step ===")
    manager = await make_manager()
    p = manager.position
    p.side = "LONG"
    p.status = "OPEN"
    p.dca_step = 1
    p.entries = [(77.50, 1.00), (76.60, 0.94)]  # pre-DCA#2 shape: qty=1.94
    p.total_qty = 1.94
    p.original_qty = 1.94
    p.avg_entry_price = 77.02
    p.last_dca_price = 76.60
    p.pending_order_id = 999
    p.pending_role = "dca"
    p.opened_at = time.time() - 120

    # DCA #2 fills locally.
    await manager._on_entry_filled("dca", fill_price=75.10, fill_qty=1.34, order_id=1001)
    assert p.dca_step == 2, f"expected dca_step=2 after DCA #2 fill, got {p.dca_step}"
    assert abs(p.total_qty - 3.28) < 1e-6, f"expected total_qty=3.28, got {p.total_qty}"
    assert p.status == "OPEN"
    print(f"PASS: local fill applied - dca_step={p.dca_step} total_qty={p.total_qty}")

    # ~1.5s later, periodic sync polls Binance REST, which still echoes
    # the PRE-fill position (qty=1.94, avg=76.13) - lagging behind the
    # WebSocket fill the bot already processed above.
    await asyncio.sleep(0.05)  # keep the test fast; well inside the grace window
    stale_rows = rows_for("LONG", 1.94, 76.13)
    await trading.initialize_sync(FakeClient(stale_rows), manager, context="periodic poll", rows=stale_rows)

    assert p.dca_step == 2, f"dca_step must survive the stale sync, got {p.dca_step}"
    assert abs(p.total_qty - 3.28) < 1e-6, f"total_qty must survive the stale sync, got {p.total_qty}"
    assert p.status == "OPEN"
    print(f"PASS: stale pre-fill sync ignored - dca_step={p.dca_step} total_qty={p.total_qty} "
          f"status={p.status}")

    # With dca_step correctly still at 2 (== MAX_DCA_STEPS-gated logic
    # elsewhere), nothing here re-fires a duplicate DCA #1 - the bug's
    # visible symptom (exchange qty overshooting to 4.17) can only happen
    # if dca_step was wrongly reset to 0 first, which the assertions above
    # already rule out.


async def test_exchange_catch_up_syncs_normally():
    """Once the exchange REST position actually catches up to the fill,
    the very next sync must resolve to already_synced with no changes."""
    print("\n=== test_exchange_catch_up_syncs_normally ===")
    manager = await make_manager()
    p = manager.position
    p.side = "LONG"
    p.status = "OPEN"
    p.dca_step = 1
    p.entries = [(77.50, 1.00), (76.60, 0.94)]
    p.total_qty = 1.94
    p.avg_entry_price = 77.02

    await manager._on_entry_filled("dca", fill_price=75.10, fill_qty=1.34, order_id=1002)
    assert p.dca_step == 2

    caught_up_rows = rows_for("LONG", 3.28, p.avg_entry_price)
    await trading.initialize_sync(FakeClient(caught_up_rows), manager, context="periodic poll", rows=caught_up_rows)

    assert p.dca_step == 2, f"caught-up sync must not touch dca_step, got {p.dca_step}"
    assert p.status == "OPEN"
    print(f"PASS: caught-up exchange state synced normally - dca_step={p.dca_step}")


async def test_genuine_mismatch_after_grace_still_reconciles():
    """A REAL mismatch (not REST lag) still triggers full reconciliation
    once the post-fill grace window has actually elapsed - the safety net
    for a truly stale/corrupted local position must keep working."""
    print("\n=== test_genuine_mismatch_after_grace_still_reconciles ===")
    manager = await make_manager()
    p = manager.position
    p.side = "LONG"
    p.status = "OPEN"
    p.dca_step = 1
    p.entries = [(77.50, 1.00), (76.60, 0.94)]
    p.total_qty = 1.94
    p.avg_entry_price = 77.02

    await manager._on_entry_filled("dca", fill_price=75.10, fill_qty=1.34, order_id=1003)
    assert p.dca_step == 2
    # Force the fill timestamp into the past so the grace window has
    # elapsed, exactly like the equivalent check in test_fill_race_fix.py.
    p.last_fill_ts = time.time() - (trading.SYNC_PENDING_GRACE_SEC + 5)

    # Exchange genuinely reports something else entirely (e.g. a manual
    # intervention or a real desync) - not just an older/smaller echo of
    # our own last fill.
    mismatched_rows = rows_for("LONG", 5.00, 74.00)
    await trading.initialize_sync(FakeClient(mismatched_rows), manager, context="periodic poll", rows=mismatched_rows)

    # initialize_sync() rebuilds by assigning a brand-new PositionState to
    # manager.position - re-fetch it rather than reusing the stale `p`
    # reference from before the rebuild.
    rebuilt = manager.position
    assert abs(rebuilt.total_qty - 5.00) < 1e-6, (
        f"genuine mismatch after grace expiry must still rebuild qty, got {rebuilt.total_qty}"
    )
    assert rebuilt.dca_step == 0, (
        f"genuine mismatch after grace expiry must still reset dca_step (no matching snapshot "
        f"was saved for this qty/avg), got {rebuilt.dca_step}"
    )
    print(f"PASS: genuine post-grace mismatch still reconciled - "
          f"total_qty={rebuilt.total_qty} dca_step={rebuilt.dca_step}")


async def test_initial_entry_fill_race_long_and_short():
    """The same race on the INITIAL entry fill (not just a DCA add), both
    LONG and SHORT - stale pre-fill exchange data must not wipe out a
    freshly-confirmed initial position either."""
    print("\n=== test_initial_entry_fill_race_long_and_short ===")
    for side, sign in (("LONG", 1), ("SHORT", -1)):
        manager = await make_manager()
        p = manager.position
        p.side = side
        p.status = "ENTERING"
        p.pending_order_id = 2001
        p.pending_role = "initial"
        p.pending_order_ts = time.time() - 100  # outside the ENTERING grace on purpose

        await manager._on_entry_filled("initial", fill_price=75.00, fill_qty=1.00, order_id=2001)
        assert p.dca_step == 0
        assert p.status == "OPEN"
        assert abs(p.total_qty - 1.00) < 1e-6

        # Periodic sync lands moments later with Binance REST still flat
        # (position not visible yet at all) - the pre-existing
        # ENTERING/DCA_PENDING-vs-flat grace only applies while status is
        # still ENTERING/DCA_PENDING, so this also exercises a flat-vs-OPEN
        # stale read for a brand new position.
        stale_rows = rows_for(side, 0.0, 0.0)
        # positionAmt == 0 means Binance reports no open position at all -
        # simulate the more common partial-lag case instead: exchange
        # shows the position but at a smaller qty than what just filled
        # (e.g. only part of the fill has propagated through REST yet).
        stale_rows = rows_for(side, 0.60, 75.00)
        await trading.initialize_sync(FakeClient(stale_rows), manager, context="periodic poll", rows=stale_rows)

        assert p.status == "OPEN", f"[{side}] status must remain OPEN, got {p.status}"
        assert abs(p.total_qty - 1.00) < 1e-6, (
            f"[{side}] total_qty must survive the stale post-initial-fill sync, got {p.total_qty}"
        )
        assert p.dca_step == 0, f"[{side}] dca_step must remain 0, got {p.dca_step}"
        print(f"PASS [{side}]: stale post-initial-fill sync ignored - total_qty={p.total_qty}")


async def test_last_dca_price_spacing_anchor_unchanged():
    """Preserve-unchanged check for the 2026-08 DCA re-fire spacing fix:
    last_dca_price must still be anchored to the actual DCA fill price by
    _on_entry_filled(), and must not be touched by the new grace block."""
    print("\n=== test_last_dca_price_spacing_anchor_unchanged ===")
    manager = await make_manager()
    p = manager.position
    p.side = "LONG"
    p.status = "OPEN"
    p.dca_step = 1
    p.entries = [(77.50, 1.00), (76.60, 0.94)]
    p.total_qty = 1.94
    p.avg_entry_price = 77.02
    p.last_dca_price = 76.60

    await manager._on_entry_filled("dca", fill_price=75.10, fill_qty=1.34, order_id=1004)
    assert p.last_dca_price == 75.10, (
        f"last_dca_price must anchor to the actual DCA #2 fill price, got {p.last_dca_price}"
    )

    stale_rows = rows_for("LONG", 1.94, 76.13)
    await trading.initialize_sync(FakeClient(stale_rows), manager, context="periodic poll", rows=stale_rows)
    assert p.last_dca_price == 75.10, (
        f"last_dca_price must be untouched by the stale-sync grace, got {p.last_dca_price}"
    )
    print(f"PASS: last_dca_price anchor preserved at {p.last_dca_price}")


async def test_initial_entry_fill_flat_rest_race_long_and_short():
    """The exchange-flat variant of the same race: WebSocket has already
    confirmed the initial fill (status=OPEN, last_fill_ts stamped), but a
    periodic REST sync within the grace window still reports NO open
    position at all (row is None - not just a stale/smaller qty). Local
    OPEN state must be preserved, not reset to FLAT. After the grace
    window elapses, a genuinely flat exchange must still reset normally."""
    print("\n=== test_initial_entry_fill_flat_rest_race_long_and_short ===")
    for side in ("LONG", "SHORT"):
        manager = await make_manager()
        p = manager.position
        p.side = side
        p.status = "ENTERING"
        p.pending_order_id = 3001
        p.pending_role = "initial"
        p.pending_order_ts = time.time() - 100  # outside the ENTERING/flat grace on purpose

        await manager._on_entry_filled("initial", fill_price=75.00, fill_qty=1.00, order_id=3001)
        assert p.status == "OPEN"
        assert abs(p.total_qty - 1.00) < 1e-6

        # REST hasn't picked up the new position at all yet - positionRisk
        # returns no row with a non-zero positionAmt, so initialize_sync()
        # takes the `row is None` branch.
        await trading.initialize_sync(FakeClient([]), manager, context="periodic poll", rows=[])

        assert manager.position.status == "OPEN", (
            f"[{side}] status must remain OPEN during the grace window, got {manager.position.status}"
        )
        assert abs(manager.position.total_qty - 1.00) < 1e-6, (
            f"[{side}] total_qty must survive the flat-REST-lag sync, got {manager.position.total_qty}"
        )
        assert manager.position.side == side
        print(f"PASS [{side}]: flat-REST-lag sync ignored - status={manager.position.status} "
              f"total_qty={manager.position.total_qty}")

        # Once the grace window has actually elapsed, a genuinely flat
        # exchange must still reset the position to FLAT as before.
        manager.position.last_fill_ts = time.time() - (trading.SYNC_PENDING_GRACE_SEC + 5)
        await trading.initialize_sync(FakeClient([]), manager, context="periodic poll", rows=[])
        assert manager.position.status == "FLAT", (
            f"[{side}] genuine flat exchange after grace expiry must still reset to FLAT, "
            f"got {manager.position.status}"
        )
        print(f"PASS [{side}]: genuine post-grace flat exchange still resets to FLAT")


async def main():
    await test_dca2_fill_then_stale_sync_does_not_reset_step()
    await test_exchange_catch_up_syncs_normally()
    await test_genuine_mismatch_after_grace_still_reconciles()
    await test_initial_entry_fill_race_long_and_short()
    await test_initial_entry_fill_flat_rest_race_long_and_short()
    await test_last_dca_price_spacing_anchor_unchanged()
    print("\nAll test_dca_resync_race_fix tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
