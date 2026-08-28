"""
Regression tests for the 2026-08 Live startup safety incident.

Root cause (confirmed from code): _dca_state_snapshot() (trading.py) writes
"qty" for PositionState.total_qty and "dca_history" for PositionState.entries.
load_dca_state() (dca2.py) used to filter the raw snapshot dict directly
against PositionState's own field names - neither "qty" nor "dca_history"
matches a PositionState field name, so both were silently dropped on every
restore. An OPEN position restored with side/avg_entry_price/dca_step/
opened_at all correct, but total_qty=0.0 and entries=[] (the PositionState()
defaults). _manage_open_position() had no gate against this: pct_move still
computed against the (correct) avg_entry_price, but
estimate_net_pnl_usdt() returns exactly 0.0 whenever total_qty<=0 (its own
existing guard), which Max Hold Time V2 read as "no meaningful loss" and
deferred - a real unrealized loss made invisible to every PnL-based
decision. A concurrent periodic sync then restored the real qty, and the
next close (correctly, once state was accurate) used it.

Fix (this file's target):
  1. load_dca_state() now explicitly remaps qty->total_qty (and
     qty->original_qty when no separate original_qty is present),
     dca_history->entries, before filtering against PositionState fields -
     and validates any OPEN/DCA_PENDING/CLOSING snapshot's economics
     (valid side, positive avg_entry, positive qty, consistent entries)
     before trusting it, falling back to flat otherwise.
  2. _manage_open_position() now refuses to manage ANY position (TP,
     Profit Lock, Smart Exit, Hard Stop, Max Hold, DCA, order placement)
     whose total_qty<=0, side isn't LONG/SHORT, or avg_entry<=0 - logging
     a throttled explicit safety message instead of silently treating
     zero quantity as zero PnL.
  3. close_position()/_place_step_order() now accept an optional
     expected_position parameter; every call site inside
     _manage_open_position() passes its own `p`, so if self.position is
     replaced by a concurrent initialize_sync() while a decision is still
     in flight, that decision is skipped instead of executing against
     the replacement.

Exercises the real MartingaleManager/load_dca_state/initialize_sync from
trading.py/dca2.py, with DRY_RUN=false. No real network call is ever made
(rows/snapshots are always supplied directly or via a local temp file).

Run directly: `python3 test_startup_state_safety_fix.py`
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
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "true")
os.environ.setdefault("MAX_DCA_STEPS", "2")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_startup_safety_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_startup_safety_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_startup_safety_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_startup_safety_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_startup_safety_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_startup_safety_dca_state.json")

import asyncio
import io
import json
import sys
import time

import dca2 as bot
import trading
from trading import PositionState

DCA_STATE_PATH = os.environ["DCA_STATE_PATH"]


class FakeClient:
    def __init__(self, side="LONG", qty=3.55, entry_price=95.0, fail_position_risk=False):
        self.placed_orders = []
        self._next_id = 9700
        self.side = side
        self.qty = qty
        self.entry_price = entry_price
        self.fail_position_risk = fail_position_risk

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


def write_snapshot(data):
    with open(DCA_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def real_format_snapshot(**overrides):
    base = {
        "symbol": "SOLUSDT",
        "status": "OPEN",
        "side": "LONG",
        "qty": 3.55,
        "avg_entry_price": 95.0,
        "initial_entry_price": 96.0,
        "dca_step": 2,
        "last_dca_price": 94.5,
        "profit_lock_active": False,
        "peak_unrealized_pnl": 0.0,
        "pending_order_id": None,
        "pending_role": None,
        "opened_at": time.time() - 5000,
        "dca_history": [[96.0, 1.0], [94.5, 2.55]],
        "total_invested_margin": 16.85,
        "current_notional": 337.0,
        "last_entry_order_id": 1001,
        "last_dca_order_id": 1002,
        "accumulated_close_pnl": 0.0,
        "position_fees_accum": 0.34,
        "position_fees_reliable": True,
        "dca_blocked": False,
        "dca_block_reason": None,
    }
    base.update(overrides)
    return base


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


# ============================================================================
# TEST 1: a real current-format DCA snapshot restores total_qty=3.55,
# original_qty, entries/history, average entry, dca_step=2, and opened_at.
# ============================================================================
async def test_real_snapshot_restores_full_state():
    print("\n=== test_real_snapshot_restores_full_state ===")
    write_snapshot(real_format_snapshot())
    m = await make_manager()
    opened_at_expected = real_format_snapshot()["opened_at"]

    await bot.load_dca_state(m)

    p = m.position
    print(f"TEST 1: status={p.status} side={p.side} total_qty={p.total_qty} "
          f"original_qty={p.original_qty} entries={p.entries} avg_entry={p.avg_entry_price} "
          f"dca_step={p.dca_step}")
    assert p.status == "OPEN"
    assert p.side == "LONG"
    assert p.total_qty == 3.55, f"total_qty must be restored from 'qty', got {p.total_qty}"
    assert p.original_qty == 3.55, f"original_qty must fall back to 'qty' when not separately given, got {p.original_qty}"
    assert p.entries == [(96.0, 1.0), (94.5, 2.55)], f"entries must be restored from 'dca_history', got {p.entries}"
    assert p.avg_entry_price == 95.0
    assert p.dca_step == 2
    assert abs(p.opened_at - opened_at_expected) < 1.0, "opened_at must be preserved"
    print("TEST 1: PASS - full state restored correctly from a real snapshot\n")


# ============================================================================
# TEST 2: startup REST sync fails while a valid snapshot exists -
# calculations use the restored real quantity, never zero.
# ============================================================================
async def test_rest_failure_uses_real_restored_qty():
    print("=== test_rest_failure_uses_real_restored_qty ===")
    write_snapshot(real_format_snapshot())
    client = FakeClient(fail_position_risk=True)
    m = await make_manager(client=client)
    await bot.load_dca_state(m)
    assert m.position.total_qty == 3.55, "setup check: snapshot must have restored real qty"

    await trading.initialize_sync(client, m, context="startup")  # REST fails -> leaves state as-is

    net_pnl = m.estimate_net_pnl_usdt(96.0)  # slightly favorable vs avg_entry=95.0
    print(f"TEST 2: after failed REST sync -> total_qty={m.position.total_qty} est_net_pnl={net_pnl:.4f}")
    assert m.position.total_qty == 3.55, "a failed REST sync must never zero out the restored quantity"
    assert net_pnl != 0.0, "PnL must be computed from the REAL restored quantity, never zero"
    print("TEST 2: PASS - PnL uses the real restored quantity despite the REST failure\n")


# ============================================================================
# TEST 3: an invalid OPEN snapshot (qty=0, the pre-fix corruption shape) is
# REJECTED by load_dca_state() and falls back to flat.
# ============================================================================
async def test_invalid_snapshot_rejected_falls_back_flat():
    print("=== test_invalid_snapshot_rejected_falls_back_flat ===")
    # Simulate the OLD bug's output shape directly (as if qty/dca_history
    # had already been dropped) to prove the NEW validation gate - not just
    # the remap - is what's protecting this restore.
    write_snapshot(real_format_snapshot(qty=0.0, dca_history=[]))
    # total_qty will still be set to 0.0 via the qty remap (0.0 is falsy-safe
    # here since we explicitly pass qty=0.0), so this exercises the
    # validation gate specifically, independent of the remap fix.
    m = await make_manager()

    with Capture() as cap:
        await bot.load_dca_state(m)

    print(f"TEST 3: status={m.position.status} total_qty={m.position.total_qty}")
    assert m.position.status == "FLAT", "an OPEN snapshot with qty=0 must be rejected, not restored"
    assert m.position.total_qty == 0.0
    assert "REJECTED" in cap.text, "rejection must be logged explicitly"
    print("TEST 3: PASS - invalid OPEN snapshot rejected, falls back to flat\n")


# ============================================================================
# TEST 4: an OPEN position with total_qty<=0 (however it got that way) -
# 1,000 rapid price ticks place ZERO DCA/close orders and never emit a Max
# Hold "no meaningful loss" close decision.
# ============================================================================
async def test_invalid_open_state_blocks_all_management():
    print("=== test_invalid_open_state_blocks_all_management ===")
    client = FakeClient(qty=0.0)
    m = await make_manager(client=client)
    m.position = PositionState(
        status="OPEN", side="LONG", avg_entry_price=95.0, total_qty=0.0,
        dca_step=2, opened_at=time.time() - 999999,  # long past even the 8h hard cap
    )
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.1, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )

    with Capture() as cap:
        for i in range(1000):
            m.current_price = 90.0 + (i % 5)  # jitter, well below avg_entry (real loss if qty were real)
            m.prev_price = m.current_price
            await m._manage_open_position()

    print(f"TEST 4: status={m.position.status} orders_placed={len(m.client.placed_orders)}")
    assert m.position.status == "OPEN", "an invalid position must be left alone, not force-closed either"
    assert len(m.client.placed_orders) == 0, "no DCA/close order may ever be placed against an invalid position"
    assert "no meaningful loss" not in cap.text, (
        "an invalid (qty<=0) position must never be classified as having 'no meaningful loss'"
    )
    assert "[invalid-open-state]" in cap.text, "the safety gate's own diagnostic must be emitted"
    print("TEST 4: PASS - invalid OPEN state fully blocks management, 1000 ticks, zero orders\n")


# ============================================================================
# TEST 5: concurrent sync replaces the position during an awaited decision -
# no decision based on the stale PositionState places an order against the
# replacement.
# ============================================================================
async def test_stale_decision_guard_blocks_close_and_dca():
    print("=== test_stale_decision_guard_blocks_close_and_dca ===")
    client = FakeClient(side="LONG", qty=3.55, entry_price=95.0)
    m = await make_manager(client=client)
    old_p = PositionState(
        status="OPEN", side="LONG", avg_entry_price=95.0, total_qty=3.55,
        dca_step=1, opened_at=time.time(),
    )
    m.position = old_p

    # Simulate initialize_sync() swapping self.position mid-decision (e.g.
    # while _manage_open_position() was awaiting a diagnostic/exchange
    # call earlier in the same tick).
    new_p = PositionState(
        status="OPEN", side="SHORT", avg_entry_price=200.0, total_qty=1.0,
        dca_step=0, opened_at=time.time(),
    )
    m.position = new_p

    await m.close_position("stale decision test", expected_position=old_p)
    print(f"TEST 5a: after stale close_position() call -> orders={len(m.client.placed_orders)} "
          f"self.position is new_p: {m.position is new_p}")
    assert len(m.client.placed_orders) == 0, "a close decided against the OLD position must not execute"
    assert m.position is new_p, "the replacement position object must be untouched"

    await m._place_step_order(step=2, side_signal="LONG", size_mult=1.0, expected_position=old_p)
    print(f"TEST 5b: after stale _place_step_order() call -> orders={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 0, "a DCA decided against the OLD position must not execute"
    print("TEST 5: PASS - stale-position decisions are skipped, never executed against the replacement\n")


# ============================================================================
# TEST 6: after a successful authoritative sync (valid, real economics),
# normal TP / Profit Lock / Smart Exit / Hard Stop remain fully available -
# specifically, Hard Stop still fires immediately on a valid position.
# ============================================================================
async def test_valid_state_hard_stop_still_works():
    print("=== test_valid_state_hard_stop_still_works ===")
    client = FakeClient(side="LONG", qty=3.55, entry_price=95.0)
    m = await make_manager(client=client)
    m.position = PositionState(
        status="OPEN", side="LONG", avg_entry_price=95.0, total_qty=3.55,
        dca_step=1, opened_at=time.time(),
    )
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.001, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.1, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    m.current_price = 95.0 * (1 - trading.HARD_STOP_PCT - 0.001)  # beyond hard stop
    m.prev_price = m.current_price

    await m._manage_open_position()
    print(f"TEST 6: status={m.position.status} orders={len(m.client.placed_orders)}")
    assert m.position.status in ("CLOSING", "FLAT"), "Hard Stop must still fire on a valid, real position"
    assert len(m.client.placed_orders) == 1
    print("TEST 6: PASS - normal risk management resumes for a valid position\n")


async def main():
    await test_real_snapshot_restores_full_state()
    await test_rest_failure_uses_real_restored_qty()
    await test_invalid_snapshot_rejected_falls_back_flat()
    await test_invalid_open_state_blocks_all_management()
    await test_stale_decision_guard_blocks_close_and_dca()
    await test_valid_state_hard_stop_still_works()
    print("ALL STARTUP-STATE SAFETY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
