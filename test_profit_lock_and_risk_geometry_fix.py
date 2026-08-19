"""Offline regression tests for the 2026-08-19 WEAK_TREND loss pair (P1-P6).

THE TWO LIVE TRADES
--------------------------------------------------------------------------
  14:54:44.11  [market-ws:public/bookTicker] disconnected (1011 ping timeout)
  14:54:44.99  reconnected
  14:54:45.07  [entry-accepted] LONG score=0.7583/0.7500 atr_pct=0.002274
               raw_momentum=+0.016018 momentum_magnitude=1.0000
               flow_delta=+105041.79
  14:54:45.68  ENTRY FILLED LONG 0.99 @ 80.55
  14:54:49.19  [trade-loss-budget] TRIGGERED est $-0.1548 <= -$0.1500
  14:54:54     CLOSED @ 80.42  net -$0.1845          (3.5s fill -> trigger)

  15:28:10.69  [market-ws:public/bookTicker] disconnected (1011 ping timeout)
  15:28:11.86  reconnected
  15:28:13.19  [entry-accepted] LONG score=0.7613/0.7500 atr_pct=0.003326
               raw_momentum=+0.019883 momentum_magnitude=1.0000
               flow_delta=+96810.03
  15:28:19.21  ENTRY FILLED LONG 0.96 @ 82.43   <- bot's own price read 82.695
  15:28:19.33  [profit-lock] ACTIVATED net=$+0.1751 peak=$+0.1751 floor=$+0.0876
  15:28:20.35  CLOSE: profit lock, est $+0.0936 <= locked $+0.0972
  15:28:34     CLOSED @ 82.47  net -$0.0170          (1.3s fill -> trigger)

THE SIX DEFECTS
--------------------------------------------------------------------------
  P1  Profit Lock decided on estimate_net_pnl_usdt(price) - the MID/MARK
      price with both legs estimated at TAKER_FEE_RATE - then executed a
      market close at the BID. estimate_net_pnl_usdt_executable() (bid/ask +
      actual commission) already existed and was deliberately withheld from
      Profit Lock. Measured: loss budget slipped -$0.030 on the executable
      estimator, Profit Lock slipped -$0.111 on the optimistic one.
  P2  The fee-safe floor was a flat MIN_NET_PROFIT_USDT ($0.05). One second
      of a 0.33%-ATR tape was worth $0.11 - twice the whole buffer.
  P3  The lock ARMED 0.02s after the entry fill, on a mark 0.32% away from
      its own fill price. That peak was never realizable.
  P4  Fixed-dollar thresholds on a fixed notional are INVERSELY proportional
      to volatility in ATR terms: the same $0.15 trigger was ~3.5 ATR on
      2026-08-18 and ~0.36 ATR on 2026-08-19.
  P5  Both entries had momentum_magnitude saturated at 1.0000 with |flow|
      ~1e5 - buying the top of a vertical move.
  P6  Both entries fired 1-3s after a market-websocket reconnect.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_profit_lock_and_risk_geometry_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SYMBOL", "SOLUSDT")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_plock_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_plock_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_plock_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_plock_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_plock_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_plock_dca_state.json")

import asyncio
import io
import sys
import time

import config
import dca2 as bot
import trading


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


def make_manager():
    filters = bot.SymbolFilters(
        tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0,
    )
    return bot.MartingaleManager(
        client=None, symbol="SOLUSDT", filters=filters, leverage=20,
    )


def open_the_1528_long(m, age_sec=10.0, bid=82.47, ask=82.48, atr_pct=0.003326):
    """Reconstructs the 15:28 LONG exactly as it stood at the profit-lock
    decision: entry 82.43, qty 0.96, actual entry commission 0.0158."""
    p = m.position
    p.status = "OPEN"
    p.side = "LONG"
    p.total_qty = 0.96
    p.original_qty = 0.96
    p.avg_entry_price = 82.43
    p.entries = [(82.43, 0.96)]
    p.opened_at = time.time() - age_sec
    m.best_bid_price, m.best_ask_price = bid, ask
    m.current_price = (bid + ask) / 2.0
    m._position_fees_accum = 0.0158          # real maker entry commission
    m._position_fees_reliable = True
    m.last_regime = trading.RegimeReading(
        regime=trading.REGIME_WEAK_TREND, atr_pct=atr_pct, atr_ratio=1.0,
    )
    return m


# ============================================================================
# P1 - EXECUTABLE PRICE, NOT MID
# ============================================================================

def test_profit_lock_uses_executable_price_not_mid():
    print("=== test_profit_lock_uses_executable_price_not_mid ===")

    m = open_the_1528_long(make_manager())

    mid_based = m.estimate_net_pnl_usdt(m.current_price)
    executable = m.estimate_net_pnl_usdt_executable()
    REALIZED = -0.0170          # what the live trade actually netted

    print(f"P1: mid-based estimate  = {mid_based:+.4f}  (old Profit Lock basis)")
    print(f"P1: executable estimate = {executable:+.4f}  (new Profit Lock basis)")
    print(f"P1: live REALIZED       = {REALIZED:+.4f}")

    # The point of P1 is ACCURACY, not conservatism. The old estimator was wrong
    # in two offsetting directions: it over-charged fees (both legs at
    # TAKER_FEE_RATE = $0.079 vs the actual $0.055 maker-entry/taker-exit split)
    # while valuing the position at an unachievable mid price. The executable
    # estimator uses the real exit side AND the real accumulated commission, and
    # reproduces the realized outcome to the cent.
    assert abs(executable - REALIZED) < 0.0001, (
        f"the executable estimator must predict the realized net ({REALIZED:+.4f}); "
        f"got {executable:+.4f}"
    )
    assert abs(mid_based - REALIZED) > 0.015, (
        "the old mid/taker-taker estimator should be visibly off from the realized "
        "outcome - that error is the defect"
    )
    expected = (82.47 - 82.43) * 0.96 - 0.0158 - config.TAKER_FEE_RATE * (0.96 * 82.47)
    assert abs(executable - expected) < 1e-6
    print("P1: executable uses the real fee split (0.0158 maker + taker exit)")
    print("P1: PASS - Profit Lock now values the position at what it would really net\n")


# ============================================================================
# P2 - VOLATILITY-AWARE FEE-SAFE FLOOR
# ============================================================================

def test_fee_safe_floor_scales_with_volatility():
    print("=== test_fee_safe_floor_scales_with_volatility ===")

    m = open_the_1528_long(make_manager(), atr_pct=0.003326)
    live_floor = m._profit_lock_min_net_floor()
    expected = config.PROFIT_LOCK_SLIPPAGE_ATR_MULT * 0.003326 * (82.43 * 0.96)

    print(f"P2: flat MIN_NET_PROFIT_USDT = {config.MIN_NET_PROFIT_USDT:.4f}")
    print(f"P2: vol-aware floor @ atr 0.333% = {live_floor:.4f}")

    assert abs(live_floor - expected) < 1e-9
    assert live_floor > config.MIN_NET_PROFIT_USDT, (
        "at 0.33% ATR the floor must exceed the old flat $0.05 buffer"
    )
    # At the incident's decision moment the position was really worth +0.1125
    # on the executable basis (the old estimator reported +0.0936). Both are
    # below the new floor, so the close is blocked either way.
    assert 0.1125 < live_floor, (
        f"the incident's decision-moment value (+0.1125 executable / +0.0936 as "
        f"logged) must be BELOW the new floor ({live_floor:.4f}) - otherwise the "
        f"same close happens again"
    )
    print(f"P2: incident decision (+0.1125 exec / +0.0936 logged) < floor "
          f"{live_floor:.4f} -> would NOT close")

    # A quiet tape falls back to the flat floor rather than guessing.
    quiet = open_the_1528_long(make_manager(), atr_pct=0.0)
    assert quiet._profit_lock_min_net_floor() == config.MIN_NET_PROFIT_USDT, (
        "with no ATR reading the floor must fall back to the pre-fix flat value"
    )
    print("P2: PASS - the buffer now tracks how far price can actually travel\n")


# ============================================================================
# P3 - MINIMUM AGE BEFORE ARMING
# ============================================================================

async def test_profit_lock_will_not_arm_on_the_entry_tick():
    print("=== test_profit_lock_will_not_arm_on_the_entry_tick ===")

    # 0.02s old, deep in profit on the incoherent mark - exactly 15:28:19.33.
    m = open_the_1528_long(make_manager(), age_sec=0.02, bid=82.69, ask=82.70)
    assert m.estimate_net_pnl_usdt_executable() >= config.PROFIT_LOCK_ACTIVATION_USDT, (
        "fixture must be profitable enough to arm, or the test proves nothing"
    )

    with Capture() as cap:
        await m._manage_open_position()

    print(f"P3: age=0.02s -> profit_lock_active={m.position.profit_lock_active}")
    assert m.position.profit_lock_active is False, (
        "Profit Lock must NOT arm 0.02s after the fill - that is the incident"
    )
    assert "arming DEFERRED" in cap.text

    # Same position, past the minimum age -> arms normally.
    m2 = open_the_1528_long(
        make_manager(), age_sec=config.PROFIT_LOCK_MIN_AGE_SEC + 1.0, bid=82.69, ask=82.70,
    )
    with Capture():
        await m2._manage_open_position()
    print(f"P3: age={config.PROFIT_LOCK_MIN_AGE_SEC + 1.0}s -> "
          f"profit_lock_active={m2.position.profit_lock_active}")
    assert m2.position.profit_lock_active is True, (
        "once the position is old enough the lock must arm exactly as before"
    )
    print("P3: PASS - arming waits for the price series to settle\n")


# ============================================================================
# P1+P2+P3 TOGETHER - THE INCIDENT MUST NOT REPRODUCE
# ============================================================================

async def test_the_1528_close_no_longer_happens():
    """The incident state, checked at all three prices it passed through.
    None of them may close the position."""
    print("=== test_the_1528_close_no_longer_happens ===")

    async def run_at(bid, ask, label):
        m = open_the_1528_long(make_manager(), age_sec=10.0, bid=bid, ask=ask)
        m.position.profit_lock_active = True
        m.position.peak_unrealized_pnl = 0.1943      # the live peak
        closed = []

        async def spy_close(reason, **kw):
            closed.append((reason, kw.get("exit_reason_tag")))

        m.close_position = spy_close
        m._last_profit_lock_peak_update_log_ts = 0.0   # clear the log throttle
        with Capture() as cap:
            await m._manage_open_position()
        return m, closed, cap.text

    locked = 0.1943 * config.PROFIT_LOCK_RATIO

    # (a) The DECISION price. Live, the old estimator reported +0.0936 which was
    #     <= the locked level, so it closed. On the executable basis the
    #     position is really worth +0.1125 - ABOVE the locked level, so the
    #     trigger condition is not even met.
    m_a, closed_a, _ = await run_at(82.605, 82.615, "decision")
    exec_a = m_a.estimate_net_pnl_usdt_executable()
    print(f"(a) decision price : executable={exec_a:+.4f} vs locked={locked:+.4f} "
          f"-> closes={len(closed_a)}")
    assert exec_a > locked, "P1 alone lifts the value back above the locked level"
    assert not closed_a

    # (b) Slightly lower - now genuinely AT/below the locked level, but still
    #     under the vol-aware floor. This is the branch P2 exists for.
    m_b, closed_b, log_b = await run_at(82.57, 82.58, "at locked level")
    exec_b = m_b.estimate_net_pnl_usdt_executable()
    floor_b = m_b._profit_lock_min_net_floor()
    print(f"(b) at locked level: executable={exec_b:+.4f} <= locked={locked:+.4f}, "
          f"floor={floor_b:.4f} -> closes={len(closed_b)}")
    assert 0 < exec_b <= locked, "fixture must sit in the trigger band"
    assert exec_b < floor_b, "and below the vol-aware floor"
    assert not closed_b, (
        "this is the state that closed live for a realized loss - P2 must hold it"
    )
    assert "HOLDING" in log_b, "and it must say why it held"

    # (c) The actual FILL price. On the executable basis the position is already
    #     net-NEGATIVE here, so Profit Lock is out of the picture entirely.
    m_c, closed_c, _ = await run_at(82.47, 82.48, "fill")
    exec_c = m_c.estimate_net_pnl_usdt_executable()
    print(f"(c) fill price     : executable={exec_c:+.4f} (net-negative) "
          f"-> closes={len(closed_c)}")
    assert exec_c < 0
    assert not closed_c, "a net-negative position must never be closed by Profit Lock"

    print("P1+2+3: PASS - none of the incident's three prices closes the trade\n")


# ============================================================================
# P4 - ATR-SCALED RISK GEOMETRY
# ============================================================================

def test_risk_geometry_scales_with_atr():
    print("=== test_risk_geometry_scales_with_atr ===")

    # 2026-08-19: high vol. Stop was 0.36 ATR - noise.
    hot = open_the_1528_long(make_manager(), atr_pct=0.003326)
    hot_sl = hot.rr_stop_loss_usd()
    hot_notional = hot._position_notional_usdt()
    hot_sl_in_atr = (hot_sl / hot_notional) / 0.003326

    # 2026-08-18: dead tape. Stop was ~3.5 ATR while TP was ~12 ATR.
    cold = open_the_1528_long(make_manager(), atr_pct=0.0008)
    cold_sl = cold.rr_stop_loss_usd()
    cold_sl_in_atr = (cold_sl / cold._position_notional_usdt()) / 0.0008

    print(f"P4: atr 0.333% -> stop ${hot_sl:.4f} = {hot_sl_in_atr:.2f} ATR "
          f"(was 0.36 ATR pre-fix)")
    print(f"P4: atr 0.080% -> stop ${cold_sl:.4f} = {cold_sl_in_atr:.2f} ATR "
          f"(was ~3.5 ATR pre-fix)")

    assert hot_sl <= config.MAX_STOP_LOSS_USD, "the dollar cap must never be exceeded"
    assert cold_sl < config.MAX_STOP_LOSS_USD, (
        "in a quiet tape the ATR term must bind and TIGHTEN the stop below the cap"
    )
    assert cold_sl >= config.SL_MIN_USD, (
        "but never below the SL_MIN_USD floor - a stop tighter than round-trip "
        "fees is unwinnable by construction"
    )

    # DEAD tape: the ATR term alone would give an absurd stop, so the floor must
    # take over. This case was caught by test_new_features.py during
    # implementation - 1.2 x 0.02% x $79 = $0.019, well under the ~$0.055
    # round-trip fee, and the RR stop began firing ahead of every other exit.
    dead = open_the_1528_long(make_manager(), atr_pct=0.0002)
    dead_sl = dead.rr_stop_loss_usd()
    raw_atr_term = config.SL_ATR_MULT * 0.0002 * dead._position_notional_usdt()
    fee_est = dead.estimate_round_trip_fee_usdt(0.96, 82.43, 82.43)
    print(f"P4: atr 0.020% -> raw ATR term would be ${raw_atr_term:.4f} "
          f"(round-trip fee ${fee_est:.4f}); floored to ${dead_sl:.4f}")
    assert raw_atr_term < fee_est, "fixture must be in the pathological band"
    assert dead_sl >= config.SL_MIN_USD
    assert dead_sl > fee_est, (
        "the stop must always sit beyond the round-trip fee, or the trade cannot "
        "win under any price path"
    )

    # Take-profit widens with ATR instead of sitting 12 ATR away.
    hot_tp = hot.atr_scaled_take_profit_pct(config.TAKE_PROFIT_PCT)
    hot_tp_in_atr = hot_tp / 0.003326
    print(f"P4: atr 0.333% -> TP {hot_tp*100:.3f}% = {hot_tp_in_atr:.2f} ATR "
          f"(base {config.TAKE_PROFIT_PCT*100:.2f}% would be "
          f"{config.TAKE_PROFIT_PCT/0.003326:.2f} ATR)")
    assert hot_tp > config.TAKE_PROFIT_PCT, "a volatile tape must widen the target"
    assert hot_tp <= config.TAKE_PROFIT_MAX_PCT, "never past the configured maximum"
    assert hot_tp_in_atr <= config.TP_ATR_MULT + 1e-9

    # Never shrinks the target below what was configured.
    assert cold.atr_scaled_take_profit_pct(config.TAKE_PROFIT_PCT) >= config.TAKE_PROFIT_PCT

    # Disabled / no-ATR paths keep the pre-fix values exactly.
    flat = open_the_1528_long(make_manager(), atr_pct=0.0)
    assert flat.rr_stop_loss_usd() == config.MAX_STOP_LOSS_USD
    assert flat.atr_scaled_take_profit_pct(config.TAKE_PROFIT_PCT) == config.TAKE_PROFIT_PCT
    print("P4: PASS - risk geometry tracks volatility, dollar values still cap it\n")


# ============================================================================
# P5 - MOMENTUM-EXHAUSTION GUARD
# ============================================================================

def test_momentum_exhaustion_blocks_the_live_entries():
    print("=== test_momentum_exhaustion_blocks_the_live_entries ===")

    engine = trading.EntryEngineV2()
    conf = trading.ConfidenceReading(
        confidence_score=0.39, trend_confidence=1.0, trend_direction="LONG",
        success_probability=0.5, tp_hit_probability=1.0, noise_probability=0.5,
        risk_score=0.125,
    )
    regime = trading.RegimeReading(
        regime=trading.REGIME_WEAK_TREND, atr_pct=0.003326, atr_ratio=1.0,
    )

    # Saturated momentum + extreme aligned flow - the 15:28 signature.
    hot_flow = {
        "imbalance": 0.0204, "trade_delta": 96810.03,
        "book_support": True, "flow_aligned": True, "data_available": True,
    }

    engine._last_log_ts = 9e18
    decision = engine.evaluate(
        conf, regime, volume_z=2.0,
        momentum=trading.ENTRY_MOMENTUM_SATURATION_PCT * 2,   # saturates to 1.0
        features=[0.0] * 34, orderflow=hot_flow,
    )

    print(f"P5: momentum_magnitude={decision.components['momentum_magnitude']:.4f} "
          f"flow_delta={decision.components['trade_delta']:+.2f}")
    print(f"P5: momentum_exhausted={decision.components['momentum_exhausted']} "
          f"should_enter={decision.should_enter}")

    assert decision.components["momentum_magnitude"] >= 1.0, "fixture must saturate"
    assert decision.components["momentum_exhausted"] is True
    assert decision.should_enter is False, (
        "a saturated move with ~1e5 one-sided flow is a late entry, not confirmation"
    )
    assert "momentum_exhausted" in decision.components.get("rejection_reason", "")

    # Saturated momentum with NORMAL flow must still be tradable - the guard
    # deliberately requires BOTH conditions.
    calm_flow = {
        "imbalance": 0.0204, "trade_delta": 250.0,
        "book_support": True, "flow_aligned": True, "data_available": True,
    }

    engine._last_log_ts = 9e18
    calm = engine.evaluate(
        conf, regime, volume_z=2.0,
        momentum=trading.ENTRY_MOMENTUM_SATURATION_PCT * 2,
        features=[0.0] * 34, orderflow=calm_flow,
    )
    print(f"P5: same momentum, flow +250 -> exhausted={calm.components['momentum_exhausted']}")
    assert calm.components["momentum_exhausted"] is False, (
        "strong momentum alone is normal and healthy in a real trend - the guard "
        "must require extreme flow too, or it would block every trend entry"
    )
    print("P5: PASS - only saturated-momentum-plus-extreme-flow is blocked\n")


# ============================================================================
# P6 - POST-RECONNECT ENTRY COOLDOWN
# ============================================================================

def test_market_stream_reconnect_cooldown():
    print("=== test_market_stream_reconnect_cooldown ===")

    m = make_manager()
    assert m.market_stream_settling() == 0.0, "no reconnect seen yet -> no cooldown"

    m.note_market_stream_reconnect("public/bookTicker")
    remaining = m.market_stream_settling()
    print(f"P6: immediately after reconnect -> {remaining:.2f}s remaining")
    assert 0 < remaining <= config.MARKET_WS_RECONNECT_COOLDOWN_SEC
    assert m._last_market_stream_reconnect_label == "public/bookTicker"

    # Expired.
    m._last_market_stream_reconnect_ts = time.time() - (
        config.MARKET_WS_RECONNECT_COOLDOWN_SEC + 1.0
    )
    print(f"P6: after the window -> {m.market_stream_settling():.2f}s remaining")
    assert m.market_stream_settling() == 0.0
    print("P6: PASS - a brief settling window is tracked per reconnect\n")


def test_websocket_module_calls_the_reconnect_hook():
    print("=== test_websocket_module_calls_the_reconnect_hook ===")

    source = open("websocket.py", encoding="utf-8").read()
    connected_at = source.index('[market-ws:{label}] connected.')
    hook_at = source.index("note_market_stream_reconnect")
    print(f"P6: 'connected.' at {connected_at}, hook at {hook_at}")
    assert hook_at > connected_at, (
        "the hook must fire on the connected path - a cooldown nothing triggers "
        "is not a cooldown"
    )
    assert "getattr(manager," in source[connected_at:hook_at + 200], (
        "guarded so a manager stub without the method is unaffected"
    )
    print("P6: PASS - websocket.py notifies the manager on every reconnect\n")


async def main():
    test_profit_lock_uses_executable_price_not_mid()
    test_fee_safe_floor_scales_with_volatility()
    await test_profit_lock_will_not_arm_on_the_entry_tick()
    await test_the_1528_close_no_longer_happens()
    test_risk_geometry_scales_with_atr()
    test_momentum_exhaustion_blocks_the_live_entries()
    test_market_stream_reconnect_cooldown()
    test_websocket_module_calls_the_reconnect_hook()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
