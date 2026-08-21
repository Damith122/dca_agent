"""Focused regression tests for the final sync/DCA invariant hardening.

No real network or order is used. Run directly from the repository root:
    python3 test_final_sync_dca_invariants_fix.py
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
os.environ.setdefault("MAX_DCA_STEPS", "2")
os.environ.setdefault("SMART_EXIT_ENABLED", "true")
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "true")
# This file exercises sync/DCA-invariant hardening (dca_step never resets,
# no duplicate/late-fill over-DCA) in isolation, at test position sizes
# never tuned against the 2026-08 per-trade fee-net loss budget (item 5,
# trading.py _manage_open_position) - disabled here (0 = off) so that
# unrelated gate cannot place its own close order and confuse the
# zero-new-orders assertions under test.
os.environ.setdefault("MAX_TRADE_NET_LOSS_USDT", "0")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_final_invariants_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_final_invariants_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_final_invariants_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_final_invariants_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_final_invariants_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_final_invariants_dca_state.json")

import asyncio
import json
import time

import dca2 as bot
import trading
from trading import PositionState


DCA_STATE_PATH = os.environ["DCA_STATE_PATH"]


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.placed_orders = []
        self._next_order_id = 8100

    async def get_position_risk(self, symbol):
        return self.rows

    async def get_user_trades(
        self, symbol, from_id=None, start_time_ms=None, limit=1000, order_id=None,
    ):
        return []

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        order_id = self._next_order_id
        self._next_order_id += 1
        return {"orderId": order_id}


def position_rows(side="LONG", qty=2.0, avg_entry=95.0):
    amount = qty if side == "LONG" else -qty
    return [{
        "symbol": "SOLUSDT",
        "positionAmt": str(amount),
        "entryPrice": str(avg_entry),
    }]


def make_manager(client=None):
    filters = bot.SymbolFilters(
        tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0,
    )
    return bot.MartingaleManager(
        client=client, symbol="SOLUSDT", filters=filters, leverage=20,
    )


def remove_state_file():
    try:
        os.remove(DCA_STATE_PATH)
    except FileNotFoundError:
        pass


async def test_avg_entry_mismatch_blocks_until_rebuild_finishes():
    """Equal qty with a different exchange average is not already synced."""
    remove_state_file()
    client = FakeClient(position_rows(qty=2.0, avg_entry=95.0))
    manager = make_manager(client)
    manager.position = PositionState(
        status="OPEN",
        side="LONG",
        avg_entry_price=100.0,
        total_qty=2.0,
        original_qty=2.0,
        entries=[(100.0, 2.0)],
        dca_step=0,
        opened_at=time.time() - 999999,
    )
    manager.position_sync_ready = True

    paused = asyncio.Event()
    release = asyncio.Event()

    async def paused_snapshot_lookup():
        paused.set()
        await release.wait()
        return None

    manager.load_dca_state_snapshot = paused_snapshot_lookup
    task = asyncio.create_task(
        trading.initialize_sync(client, manager, context="avg-mismatch", rows=client.rows)
    )
    await paused.wait()

    assert manager.position_sync_ready is False
    assert manager.position.avg_entry_price == 100.0

    # This old local position is far past Max Hold and at a DCA-triggering
    # adverse price, but remains inside Hard Stop. Provisional-economics
    # actions must stay silent while the authoritative rebuild is paused.
    manager.current_price = 99.4
    manager.prev_price = manager.current_price
    await manager._manage_open_position()
    assert client.placed_orders == []

    release.set()
    await task
    assert manager.position_sync_ready is True
    assert manager.position.status == "OPEN"
    assert manager.position.avg_entry_price == 95.0
    assert manager.position.total_qty == 2.0


async def test_inconsistent_snapshot_history_is_rejected():
    """A history whose quantities do not sum to total_qty is not trusted."""
    remove_state_file()
    with open(DCA_STATE_PATH, "w", encoding="utf-8") as state_file:
        json.dump({
            "status": "OPEN",
            "side": "LONG",
            "qty": 2.0,
            "avg_entry_price": 100.0,
            "dca_step": 1,
            "dca_history": [[100.0, 1.0]],
        }, state_file)

    manager = make_manager()
    await bot.load_dca_state(manager)
    assert manager.position.status == "FLAT"
    assert manager.position.total_qty == 0.0
    remove_state_file()


async def test_out_of_range_recovered_step_is_capped_and_blocked():
    """Both startup ingestion paths enforce dca_step <= MAX_DCA_STEPS."""
    snapshot = {
        "symbol": "SOLUSDT",
        "status": "OPEN",
        "side": "LONG",
        "qty": 2.0,
        "avg_entry_price": 95.0,
        "dca_step": 5,
        "dca_history": [[97.0, 1.0], [93.0, 1.0]],
        "dca_blocked": False,
        "opened_at": time.time() - 60,
    }

    remove_state_file()
    with open(DCA_STATE_PATH, "w", encoding="utf-8") as state_file:
        json.dump(snapshot, state_file)

    restored = make_manager()
    await bot.load_dca_state(restored)
    assert restored.position.status == "OPEN"
    assert restored.position.dca_step == trading.MAX_DCA_STEPS
    assert restored.position.dca_blocked is True

    client = FakeClient(position_rows(qty=2.0, avg_entry=95.0))
    rebuilt = make_manager(client)

    async def snapshot_lookup():
        return dict(snapshot)

    rebuilt.load_dca_state_snapshot = snapshot_lookup
    await trading.initialize_sync(
        client, rebuilt, context="out-of-range-step", rows=client.rows,
    )
    assert rebuilt.position.status == "OPEN"
    assert rebuilt.position.dca_step == trading.MAX_DCA_STEPS
    assert rebuilt.position.dca_blocked is True
    assert rebuilt.position_sync_ready is True
    remove_state_file()


async def test_late_and_duplicate_dca_fill_never_exceeds_cap():
    """A real late fill updates economics once without creating step 3."""
    client = FakeClient()
    manager = make_manager(client)
    manager.position = PositionState(
        status="OPEN",
        side="LONG",
        avg_entry_price=100.0,
        total_qty=1.0,
        original_qty=1.0,
        entries=[(100.0, 1.0)],
        dca_step=trading.MAX_DCA_STEPS,
        opened_at=time.time(),
    )
    manager.position_sync_ready = True
    order_id = 8200
    manager._order_index[order_id] = "dca"
    event = {
        "o": {
            "i": order_id,
            "X": "FILLED",
            "ap": "98.0",
            "z": "0.5",
            "t": 1,
            "T": int(time.time() * 1000),
        },
        "E": int(time.time() * 1000),
    }

    await manager.handle_order_update(event)
    assert manager.position.total_qty == 1.5
    assert manager.position.dca_step == trading.MAX_DCA_STEPS
    assert manager.position.dca_blocked is True
    entry_count = len(manager.position.entries)

    await manager.handle_order_update(event)
    assert manager.position.total_qty == 1.5
    assert len(manager.position.entries) == entry_count
    assert manager.position.dca_step == trading.MAX_DCA_STEPS

    manager.current_price = 97.0
    await manager._place_step_order(
        step=trading.MAX_DCA_STEPS + 1,
        side_signal="LONG",
        expected_position=manager.position,
    )
    assert client.placed_orders == []
    assert manager.position.dca_step <= trading.MAX_DCA_STEPS


async def main():
    await test_avg_entry_mismatch_blocks_until_rebuild_finishes()
    await test_inconsistent_snapshot_history_is_rejected()
    await test_out_of_range_recovered_step_is_capped_and_blocked()
    await test_late_and_duplicate_dca_fill_never_exceeds_cap()
    print("ALL FINAL SYNC / DCA INVARIANT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
