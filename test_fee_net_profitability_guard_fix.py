"""Focused offline regression tests for the fee-net profitability guard.

No network request or real Binance order is sent. Run directly:
    python3 test_fee_net_profitability_guard_fix.py

The tests prove:
  1. A confirmed 2/2 position never submits DCA #3 and is not force-closed
     merely because the small DCA trigger distance is crossed.
  2. Normal risk-reducing Hard Stop management remains available at 2/2.
  3. The +$0.50 daily target blocks new entries using realized NET PnL.
  4. The -$0.50 daily loss boundary remains active and symmetric.
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
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "false")
os.environ.setdefault("DAILY_PROFIT_TARGET_USDT", "0.50")
os.environ.setdefault("MAX_DAILY_LOSS_USDT", "0.50")
# This file exercises the pre-existing 2/2-DCA-exhaustion exposure cap and
# the daily profit/loss target gates in isolation, at test position sizes
# never tuned against the 2026-08 per-trade fee-net loss budget (item 5,
# trading.py _manage_open_position) - disabled here (0 = off) so that
# unrelated gate cannot force-close the position before the exhaustion/
# daily-target behavior under test is exercised.
os.environ.setdefault("MAX_TRADE_NET_LOSS_USDT", "0")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_profitability_guard_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_profitability_guard_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_profitability_guard_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_profitability_guard_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_profitability_guard_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_profitability_guard_dca_state.json")

import asyncio
import io
import sys
import time
from datetime import datetime, timezone

import dca2 as bot
import trading


class FakeClient:
    def __init__(self, side="LONG", qty=3.31, avg_entry=75.60):
        self.side = side
        self.qty = qty
        self.avg_entry = avg_entry
        self.placed_orders = []
        self._next_id = 9900

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        order_id = self._next_id
        self._next_id += 1
        return {"orderId": order_id}

    async def get_position_risk(self, symbol):
        amount = self.qty if self.side == "LONG" else -self.qty
        return [{
            "symbol": symbol,
            "positionAmt": str(amount),
            "entryPrice": str(self.avg_entry),
        }]


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


def make_manager(side="LONG", qty=3.31, avg_entry=75.60):
    filters = bot.SymbolFilters(
        tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0,
    )
    client = FakeClient(side=side, qty=qty, avg_entry=avg_entry)
    manager = bot.MartingaleManager(
        client=client, symbol="SOLUSDT", filters=filters, leverage=20,
    )
    manager.position_sync_ready = True
    manager.position = trading.PositionState(
        side=side,
        status="OPEN",
        dca_step=trading.MAX_DCA_STEPS,
        entries=[(avg_entry, qty)],
        avg_entry_price=avg_entry,
        total_qty=qty,
        original_qty=qty,
        opened_at=time.time(),
        last_dca_price=avg_entry,
    )
    manager.current_price = avg_entry
    manager.prev_price = avg_entry
    manager.last_regime = trading.RegimeReading(
        regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0,
    )
    manager.last_confidence = trading.ConfidenceReading(
        confidence_score=0.2,
        risk_score=0.2,
        trend_direction=None,
        trend_confidence=0.0,
        success_probability=0.5,
        tp_hit_probability=0.5,
    )
    return manager


async def test_exhausted_dca_caps_exposure_without_immediate_close():
    manager = make_manager()
    manager.current_price = manager.position.avg_entry_price * 0.995
    manager.prev_price = manager.current_price

    with Capture() as cap:
        await manager._manage_open_position()

    assert manager.position.status == "OPEN"
    assert manager.position.dca_step == trading.MAX_DCA_STEPS
    assert manager.client.placed_orders == []
    assert "[max-dca-exhausted]" in cap.text
    assert "decision=HOLD" in cap.text
    assert "exposure_capped_normal_exits_active" in cap.text
    print("PASS: 2/2 caps exposure without DCA #3 or an immediate fee-heavy close")


async def test_hard_stop_still_reduces_risk_at_two_of_two():
    manager = make_manager()
    manager.current_price = manager.position.avg_entry_price * (
        1 - trading.HARD_STOP_PCT - 0.001
    )
    manager.prev_price = manager.current_price
    await manager._manage_open_position()

    assert manager.position.status == "CLOSING"
    assert len(manager.client.placed_orders) == 1
    assert manager.client.placed_orders[0].get("reduceOnly") == "true"
    print("PASS: normal Hard Stop remains available at 2/2")


async def test_daily_profit_target_blocks_new_entries_fee_net():
    manager = make_manager()
    manager.position = trading.PositionState(status="FLAT")
    manager.position_sync_ready = True
    manager.current_price = 75.0
    manager.prev_price = 75.0
    manager._daily_loss_tracker_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manager.daily_realized_pnl = trading.DAILY_PROFIT_TARGET_USDT

    with Capture() as cap:
        await manager.on_price_tick()

    assert manager.client.placed_orders == []
    assert "[daily-profit] entries halted" in cap.text
    assert "realized NET PnL" in cap.text
    print("PASS: +$0.50 realized fee-net target locks the day against new entries")


async def test_daily_loss_limit_remains_symmetric():
    manager = make_manager()
    manager.position = trading.PositionState(status="FLAT")
    manager.position_sync_ready = True
    manager.current_price = 75.0
    manager.prev_price = 75.0
    manager._daily_loss_tracker_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manager.daily_realized_pnl = -trading.MAX_DAILY_LOSS_USDT

    with Capture() as cap:
        await manager.on_price_tick()

    assert manager.client.placed_orders == []
    assert "[daily-loss] entries halted" in cap.text
    print("PASS: -$0.50 realized fee-net boundary blocks further entries")


async def main():
    await test_exhausted_dca_caps_exposure_without_immediate_close()
    await test_hard_stop_still_reduces_risk_at_two_of_two()
    await test_daily_profit_target_blocks_new_entries_fee_net()
    await test_daily_loss_limit_remains_symmetric()
    print("ALL FEE-NET PROFITABILITY GUARD TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
