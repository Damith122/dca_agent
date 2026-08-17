"""
Focused regression tests for the 2026-08 improvements:
  1) Max Hold Time Protection
  2) Low Volatility ("dead market") Entry Filter
  3) Percentage Adaptive TP/DCA System
  5) Entry-timing momentum calibration fix

Exercises the real MartingaleManager / EntryEngineV2 from trading.py via
dca2.py, with DRY_RUN=true so close_position()'s DRY_RUN branch is used
(no real client / network calls needed) - same environment-setup pattern
as smoke_test.py / test_fill_race_fix.py.

Run from the repo root: python3 test_new_features.py
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_nf_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_nf_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_nf_performance_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_nf_performance_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_nf_brain_v2.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_nf_dca_state.json")
os.environ.setdefault("BRAIN2_WARMUP_UPDATES", "5")
os.environ.setdefault("MIN_HOLD_SEC_BEFORE_EXIT", "0")
os.environ.setdefault("MIN_NET_PROFIT_USDT", "0.00")  # keep PnL floor checks out of the way of these tests
os.environ.setdefault("MAX_HOLD_TIME_SEC", "100")
os.environ.setdefault("MAX_HOLD_TIME_HARD_CAP_SEC", "200")

import asyncio
import time

import dca2 as bot
import trading


async def make_manager():
    filters = bot.SymbolFilters(tick_size=0.1, step_size=0.0001, min_qty=0.0001, min_notional=5.0)
    manager = bot.MartingaleManager(client=None, symbol="BTCUSDT", filters=filters, leverage=40)
    return manager


def open_flat_position(manager, side="LONG", entry_price=60000.0, qty=0.002):
    manager.position_sync_ready = True  # 2026-08 position_sync_ready gate: this helper builds an already-OPEN position directly, bypassing initialize_sync() - mark it ready so the new startup-readiness gate doesn't mask the Max Hold/DCA/entry-signal behavior under test in this file.
    manager.position.side = side
    manager.position.status = "OPEN"
    manager.position.avg_entry_price = entry_price
    manager.position.total_qty = qty
    manager.position.original_qty = qty
    manager.position.entries = [(entry_price, qty)]
    manager.position.dca_step = 0
    manager.current_price = entry_price
    manager.prev_price = entry_price


def seed_closed_candles(manager, n=10, start_price=60000.0, interval_sec=61):
    """Feeds n candles far enough apart in time that each becomes a fully
    CLOSED candle (CandleAggregator.closed_candles() excludes the current
    in-progress bucket) - needed for DYNAMIC_TP_ENABLED's `len(candles) >= 5`
    gate and for compute_atr_pct() to have real high/low/close spread."""
    base_ts = time.time() - n * interval_sec
    for i in range(n):
        price = start_price + i
        manager.candles.on_price(price - 0.5, ts=base_ts + i * interval_sec)
        manager.candles.on_price(price + 0.5, ts=base_ts + i * interval_sec)
        manager.candles.on_price(price, ts=base_ts + i * interval_sec)
    # force the last in-progress bucket to close too
    manager.candles.on_price(start_price + n, ts=base_ts + n * interval_sec)


# ----------------------------------------------------------------------------
# 1) Max Hold Time Protection
# ----------------------------------------------------------------------------

async def test_max_hold_time_closes_dead_flat_position():
    """A flat/no-progress position held past MAX_HOLD_TIME_SEC in a SIDEWAYS
    regime must be force-closed with exit_reason_tag='max_hold_time'."""
    print("\n=== test_max_hold_time_closes_dead_flat_position ===")
    manager = await make_manager()
    open_flat_position(manager, side="LONG", entry_price=60000.0)
    manager.current_price = 60000.0  # breakeven -> unrealized pnl <= 0
    manager.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0006)
    manager.position.opened_at = time.time() - (trading.MAX_HOLD_TIME_SEC + 10)

    await manager._manage_open_position()

    assert manager.position.status == "CLOSING", f"expected CLOSING, got {manager.position.status}"
    assert manager._pending_exit_reason == "max_hold_time", manager._pending_exit_reason
    print(f"PASS: status={manager.position.status}, exit_reason={manager._pending_exit_reason}")


async def test_max_hold_time_defers_a_trending_winner_then_hard_caps():
    """A profitable position in a genuinely trending regime is NOT force-
    closed at the soft MAX_HOLD_TIME_SEC cap, but IS force-closed once the
    absolute MAX_HOLD_TIME_HARD_CAP_SEC is reached, regardless of PnL/regime."""
    print("\n=== test_max_hold_time_defers_a_trending_winner_then_hard_caps ===")
    manager = await make_manager()
    open_flat_position(manager, side="LONG", entry_price=60000.0, qty=0.01)
    # Favorable move large enough to clear round-trip fees (net profit > 0)
    # but still well below the ~0.35% base dynamic TP / DCA distance, so
    # TP/DCA/Smart-Exit don't fire and only Max Hold Time decides here.
    manager.current_price = 60120.0  # +0.20% - net-positive after fees, < TAKE_PROFIT_PCT
    manager.last_regime = trading.RegimeReading(regime=trading.REGIME_STRONG_TREND, atr_pct=0.0006)
    manager.position.opened_at = time.time() - (trading.MAX_HOLD_TIME_SEC + 10)

    await manager._manage_open_position()
    net_pnl = manager.estimate_net_pnl_usdt(manager.current_price)
    assert net_pnl > 0, f"test setup issue: expected net-positive pnl, got {net_pnl}"
    assert manager.position.status == "OPEN", (
        f"trending + profitable position should be deferred past the soft cap, "
        f"got status={manager.position.status}"
    )
    print(f"PASS (soft cap deferred): status={manager.position.status}")

    # Now push past the absolute hard cap - must close unconditionally.
    manager.position.opened_at = time.time() - (trading.MAX_HOLD_TIME_HARD_CAP_SEC + 10)
    await manager._manage_open_position()
    assert manager.position.status == "CLOSING", f"expected CLOSING at hard cap, got {manager.position.status}"
    assert manager._pending_exit_reason == "max_hold_time"
    print(f"PASS (hard cap enforced): status={manager.position.status}")


async def test_max_hold_time_disabled_flag_is_respected():
    """MAX_HOLD_TIME_ENABLED=false must fully disable the feature (backward
    compatibility / quick rollback switch)."""
    print("\n=== test_max_hold_time_disabled_flag_is_respected ===")
    original = trading.MAX_HOLD_TIME_ENABLED
    trading.MAX_HOLD_TIME_ENABLED = False
    try:
        manager = await make_manager()
        open_flat_position(manager, side="LONG", entry_price=60000.0)
        manager.current_price = 60000.0
        manager.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0006)
        manager.position.opened_at = time.time() - (trading.MAX_HOLD_TIME_HARD_CAP_SEC + 100)
        await manager._manage_open_position()
        assert manager.position.status == "OPEN", "disabled flag must not force-close"
        print("PASS: MAX_HOLD_TIME_ENABLED=false leaves position open")
    finally:
        trading.MAX_HOLD_TIME_ENABLED = original


# ----------------------------------------------------------------------------
# 2) Low Volatility ("dead market") Entry Filter
# ----------------------------------------------------------------------------

async def test_low_volatility_filter_blocks_dead_market_entry():
    """A SIDEWAYS regime with atr_pct below LOW_VOLATILITY_ATR_PCT_THRESHOLD
    must be blocked even though SIDEWAYS is otherwise an allowed regime."""
    print("\n=== test_low_volatility_filter_blocks_dead_market_entry ===")
    engine = trading.EntryEngineV2()
    conf = trading.ConfidenceReading(
        confidence_score=0.9, trend_confidence=0.9, trend_direction="LONG",
        success_probability=0.9, tp_hit_probability=0.9, noise_probability=0.1,
        risk_score=0.05,
    )
    dead_regime = trading.RegimeReading(
        regime=trading.REGIME_SIDEWAYS,
        atr_pct=trading.LOW_VOLATILITY_ATR_PCT_THRESHOLD * 0.5,  # well below the floor
    )
    decision = engine.evaluate(conf, dead_regime, volume_z=1.0, momentum=0.001, features=[0.0] * 34)
    assert decision.should_enter is False, "dead market must be blocked regardless of score"
    print(f"PASS: dead market blocked, score={decision.score:.4f}")

    normal_regime = trading.RegimeReading(
        regime=trading.REGIME_SIDEWAYS,
        atr_pct=trading.LOW_VOLATILITY_ATR_PCT_THRESHOLD * 3.0,  # comfortably above the floor
    )
    decision2 = engine.evaluate(conf, normal_regime, volume_z=1.0, momentum=0.001, features=[0.0] * 34)
    assert decision2.should_enter is True, "normal ranging market must NOT be blocked by the dead-market filter"
    print(f"PASS: normal ranging SIDEWAYS market still allowed, score={decision2.score:.4f}")


# ----------------------------------------------------------------------------
# 3) Percentage Adaptive TP/DCA System
# ----------------------------------------------------------------------------

async def test_adaptive_scale_shrinks_tp_for_large_dca_position():
    """A deep-DCA (large notional) position should get a smaller dynamic TP
    percentage than a fresh, step-0-sized position under identical
    volatility/regime conditions."""
    print("\n=== test_adaptive_scale_shrinks_tp_for_large_dca_position ===")
    manager = await make_manager()
    # Feed enough CLOSED candles (spaced >= CANDLE_INTERVAL_SEC apart) so
    # DYNAMIC_TP_ENABLED's candle-count gate passes and last_regime.atr_pct
    # is set to a mid-range value (so we're on the linear part of the vol
    # curve, not clamped at TAKE_PROFIT_MAX_PCT).
    import trading as T
    seed_closed_candles(manager)
    manager.last_regime = T.RegimeReading(regime=T.REGIME_SIDEWAYS, atr_pct=(T.TP_VOL_LOW + T.TP_VOL_HIGH) / 2)

    # Small/fresh position (step 0 size)
    open_flat_position(manager, side="LONG", entry_price=60000.0, qty=0.001)  # ~ baseline notional
    small_tp = manager.get_dynamic_take_profit_pct()

    # Deep-DCA position: qty scaled up ~6x baseline notional
    manager.position.total_qty = 0.006
    large_tp = manager.get_dynamic_take_profit_pct()

    assert large_tp <= small_tp, f"expected large-position TP <= small-position TP, got {large_tp} > {small_tp}"
    assert T.TAKE_PROFIT_PCT * T.ADAPTIVE_TP_MIN_RATIO <= large_tp <= T.TAKE_PROFIT_MAX_PCT * T.ADAPTIVE_TP_MAX_RATIO
    print(f"PASS: small_position_tp={small_tp:.5f} >= large_position_tp={large_tp:.5f}")


async def test_adaptive_scale_disabled_matches_legacy_behavior():
    """ADAPTIVE_SIZING_ENABLED=false must reproduce the exact pre-existing
    vol-only dynamic TP/DCA calculation (backward compatibility)."""
    print("\n=== test_adaptive_scale_disabled_matches_legacy_behavior ===")
    import trading as T
    original = T.ADAPTIVE_SIZING_ENABLED
    T.ADAPTIVE_SIZING_ENABLED = False
    try:
        manager = await make_manager()
        seed_closed_candles(manager)
        manager.last_regime = T.RegimeReading(regime=T.REGIME_STRONG_TREND, atr_pct=T.TP_VOL_HIGH)
        open_flat_position(manager, side="LONG", entry_price=60000.0, qty=0.01)
        tp = manager.get_dynamic_take_profit_pct()
        assert abs(tp - T.TAKE_PROFIT_MAX_PCT) < 1e-9, f"expected exactly TAKE_PROFIT_MAX_PCT, got {tp}"
        print(f"PASS: adaptive scaling disabled -> tp={tp:.5f} == TAKE_PROFIT_MAX_PCT")
    finally:
        T.ADAPTIVE_SIZING_ENABLED = original


# ----------------------------------------------------------------------------
# 5) Entry-timing momentum calibration fix
# ----------------------------------------------------------------------------

async def test_momentum_component_saturates_at_configured_threshold():
    """An aligned momentum_component must saturate to 1.0 once momentum
    reaches ENTRY_MOMENTUM_SATURATION_PCT (the multi-candle rolling-return
    scale), not the old single-tick 0.002 threshold."""
    print("\n=== test_momentum_component_saturates_at_configured_threshold ===")
    engine = trading.EntryEngineV2()
    conf = trading.ConfidenceReading(
        confidence_score=0.5, trend_confidence=0.5, trend_direction="LONG",
        success_probability=0.5, tp_hit_probability=0.5, noise_probability=0.3,
        risk_score=0.2,
    )
    regime = trading.RegimeReading(regime=trading.REGIME_WEAK_TREND, atr_pct=0.0006)

    d_zero = engine.evaluate(conf, regime, volume_z=0.0, momentum=0.0, features=[0.0] * 34)
    d_saturated = engine.evaluate(
        conf, regime, volume_z=0.0, momentum=trading.ENTRY_MOMENTUM_SATURATION_PCT, features=[0.0] * 34
    )
    assert d_saturated.components["momentum"] > d_zero.components["momentum"], (
        "momentum component must respond to a realistic multi-candle rolling-return-scale input"
    )
    assert abs(d_saturated.components["momentum"] - 1.0) < 1e-9, d_saturated.components["momentum"]
    print(
        f"PASS: momentum_component(0)={d_zero.components['momentum']:.4f} "
        f"momentum_component(threshold)={d_saturated.components['momentum']:.4f}"
    )


async def main():
    await test_max_hold_time_closes_dead_flat_position()
    await test_max_hold_time_defers_a_trending_winner_then_hard_caps()
    await test_max_hold_time_disabled_flag_is_respected()
    await test_low_volatility_filter_blocks_dead_market_entry()
    await test_adaptive_scale_shrinks_tp_for_large_dca_position()
    await test_adaptive_scale_disabled_matches_legacy_behavior()
    await test_momentum_component_saturates_at_configured_threshold()
    print("\nALL NEW-FEATURE TESTS PASSED")


asyncio.run(main())
