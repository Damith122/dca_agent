import os
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/performance_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/performance_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/brain_v2.pkl")
os.environ.setdefault("TRADE_COOLDOWN_SEC", "0")
os.environ.setdefault("MIN_HOLD_SEC_BEFORE_EXIT", "0")
os.environ.setdefault("BRAIN2_WARMUP_UPDATES", "20")
os.environ.setdefault("ENTRY_SCORE_THRESHOLD", "-1.0")  # force entries for the smoke test
os.environ.setdefault("SIZE_MIN_MULT", "1.0")
os.environ.setdefault("SIZE_MAX_MULT", "1.0")

import asyncio
import random
import sys

sys.path.insert(0, "/home/claude")
import dca2 as bot


async def run():
    bot.INITIAL_ENTRY_USDT = 20.0
    filters = bot.SymbolFilters(tick_size=0.1, step_size=0.0001, min_qty=0.0001, min_notional=5.0)
    client = None  # DRY_RUN=True, so RestClient methods for orders are never called
    manager = bot.MartingaleManager(client, "BTCUSDT", filters, 40)

    price = 60000.0
    rng = random.Random(42)

    print("Feeding synthetic ticks + aggTrades through the full pipeline ...")
    for i in range(4000):
        drift = 0.00003 * math.sin(i / 50.0) if False else 0.0
        price *= (1 + rng.gauss(0, 0.00025))
        bid = price - 0.5
        ask = price + 0.5
        manager.on_book_ticker(bid, ask, rng.uniform(1, 5), rng.uniform(1, 5))
        manager.on_agg_trade(rng.uniform(0.001, 0.05), rng.random() > 0.5)
        await manager.on_price_tick()

        # simulate fills for any pending order immediately (as DRY_RUN would
        # via a real fill event) so the state machine actually progresses
        if manager.position.pending_order_id is not None:
            role = manager._order_index.get(manager.position.pending_order_id)
            fill_price = manager.current_price
            if role in ("initial", "dca"):
                step = manager.position.dca_step if role == "dca" else 0
                notional = manager.notional_for_step(step, 1.0)
                fill_qty = bot.round_step(notional / fill_price, filters.step_size)
                await manager._on_entry_filled(role, fill_price, fill_qty)
                manager._order_index.pop(manager.position.pending_order_id, None)
            elif role == "close":
                pnl = manager.estimate_net_pnl_usdt(fill_price)
                await manager._on_close_filled(fill_price, pnl)
                manager._order_index.pop(manager.position.pending_order_id, None)
            elif role == "partial_close":
                pass  # DRY_RUN partial closes already self-apply synchronously

    print(f"Ticks processed OK. trade_count={manager.trade_count} "
          f"brain_updates={manager.brain.update_count} regime={manager.last_regime.regime} "
          f"confidence={manager.last_confidence.confidence_score:.3f}")

    stats = manager.perf_stats.compute()
    print("Stats sample:", {k: stats[k] for k in list(stats)[:6]})

    # persistence roundtrip
    data = manager.brain.to_bytes()
    restored = bot.BrainV2.from_bytes(data, bot.N_FEATURES_V2, bot.BRAIN2_WARMUP_UPDATES)
    assert restored.update_count == manager.brain.update_count
    print("Brain persistence roundtrip OK.")

    manager.perf_stats.export()
    print("Stats export OK.")


import math
asyncio.run(run())
print("SMOKE TEST PASSED")
