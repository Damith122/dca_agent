"""
Regression tests for the DCA time gate plus the DCA loss-deferral rollback.

Root cause (observed on a clean Live SOLUSDT LONG trade): Max Hold Time V2's
emergency review already reaches a genuine soft max-hold threshold
(effective_max_hold_sec = MAX_HOLD_TIME_SEC * MAX_HOLD_TIME_DCA_MULTIPLIER
once dca_step >= 1) and reports action=DEFER, reason="DCA opportunity
available" - but the DCA placement block further down in the same function
had NO time-window check of its own, so a new DCA add could still be
submitted after the position was already flagged for emergency review. That
new DCA increases notional/exposure at exactly the point the bot's own
review logic had already flagged the trade as questionable.

The original Option-B fix introduced a single shared
`dca_time_eligible` boolean, computed once per tick from the exact same
held_sec_so_far / effective_max_hold_sec already used for the threshold
check, now gates BOTH:
  (a) the dca_opportunity_available signal inside the emergency review, and
  (b) the actual DCA placement gate (a new early return placed after the
      pre-existing, untouched step-exhaustion branch, before the spacing
      gate / final-DCA gate / _place_step_order() call).
The two can never disagree, by construction, because they read the same
variable.

The loss-deferral rollback keeps the deterministic DCA-aware soft Max Hold
boundary. A later fee-aware correction changed the 2/2 adverse branch from
an immediate close into an exposure cap: no DCA #3 is possible, while normal
TP/Profit-Lock/Smart-Exit/Hard-Stop and the 2h DCA Max Hold remain active.

Explicitly NOT changed (and asserted here where practical):
  - MAX_HOLD_TIME_SEC, MAX_HOLD_TIME_HARD_CAP_SEC, MAX_HOLD_TIME_DCA_MULTIPLIER,
    MAX_HOLD_TIME_RECOVERY_MIN_AGREE, MAX_HOLD_TIME_SMALL_LOSS_PCT - no ENV
    defaults touched.
  - DCA sizing, spacing, distance formula, TP, Profit Lock, Smart Exit, Hard
    Stop, and Brain are untouched.
  - This fix does not add a per-trade dollar-loss cap and does not
    guarantee any particular loss ceiling.

Run directly: `python3 test_dca_time_gate_fix.py`
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
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_dca_time_gate_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_dca_time_gate_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_dca_time_gate_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_dca_time_gate_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_dca_time_gate_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_dca_time_gate_dca_state.json")
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
# This file exercises the Max Hold V2 / DCA time-gate interaction in
# isolation, at test position sizes never tuned against the 2026-08
# per-trade fee-net loss budget (item 5, trading.py _manage_open_position) -
# disabled here (0 = off) so that unrelated gate cannot preempt/relabel the
# specific soft-timeout/defer log lines under test, exactly like
# SMART_EXIT_ENABLED=false above.
os.environ.setdefault("MAX_TRADE_NET_LOSS_USDT", "0")
# Deliberately NOT overriding any MAX_HOLD_TIME_* value - tests run against
# the real production defaults (4h soft / 8h hard / 0.5 DCA multiplier / 2
# min-agree) so they validate actual deployed behavior, not a test-only
# config. Hold-time boundaries are reached by backdating position.opened_at,
# not by waiting or by shrinking the thresholds.

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
    def __init__(self, rows=None):
        self.placed_orders = []
        self._next_id = 9000
        self.rows = rows or []

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        oid = self._next_id
        self._next_id += 1
        return {"orderId": oid}

    async def get_position_risk(self, symbol):
        return self.rows


async def make_manager(side="LONG", dca_step=1, entries=None, avg_entry=77.20,
                        total_qty=2.63, held_sec=None,
                        confidence_kwargs=None, regime=None):
    """Builds a manager already past DCA #1 (dca_step defaults to 1, same
    as the evidenced Live trade), so effective_max_hold_sec uses the
    DCA-aware 0.5x multiplier exactly as in the real incident."""
    filters = bot.SymbolFilters(tick_size=0.0001, step_size=0.01, min_qty=0.01, min_notional=5.0)
    m = bot.MartingaleManager(client=FakeClient(), symbol="SOLUSDT", filters=filters, leverage=20)
    m.position_sync_ready = True  # 2026-08 position_sync_ready gate: this test file exercises DCA/Max Hold management directly against an already-OPEN position, bypassing initialize_sync() - mark it ready so the new startup-readiness gate (unrelated to this file's own DCA time-gate fix) doesn't mask what's under test.
    m.position.side = side
    m.position.status = "OPEN"
    m.position.entries = entries if entries is not None else [(77.20, 1.03), (77.05, 1.60)]
    m.position.avg_entry_price = avg_entry
    m.position.total_qty = total_qty
    m.position.original_qty = total_qty
    m.position.dca_step = dca_step
    m.position.last_dca_price = 77.05 if dca_step >= 1 else None
    m.client.rows = [{
        "symbol": "SOLUSDT",
        "positionAmt": str(total_qty if side == "LONG" else -total_qty),
        "entryPrice": str(avg_entry),
    }]
    m.last_regime = regime or trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0)
    ck = confidence_kwargs or {}
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=ck.get("confidence_score", 0.5),
        risk_score=ck.get("risk_score", 0.2),
        trend_direction=ck.get("trend_direction", None),
        trend_confidence=ck.get("trend_confidence", 0.0),
        success_probability=0.5, tp_hit_probability=0.5,
    )
    m.prev_price = avg_entry
    m.current_price = avg_entry

    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER if dca_step >= 1 else trading.MAX_HOLD_TIME_SEC
    if held_sec is None:
        held_sec = effective  # default: exactly at boundary
    m.position.opened_at = time.time() - held_sec
    return m


async def tick(m, price):
    m.current_price = price
    m.prev_price = price
    await m._manage_open_position()


def small_loss_price(m, extra_pct=0.003):
    """A price that produces a meaningful (fee-net) loss, comfortably above
    MAX_HOLD_TIME_SMALL_LOSS_PCT (0.15%) but well inside HARD_STOP_PCT (2%)
    so Hard Stop never fires and masks the test."""
    avg = m.position.avg_entry_price
    if m.position.side == "LONG":
        return avg * (1 - extra_pct)
    return avg * (1 + extra_pct)


def dca_trigger_price(m):
    dca_distance = m.get_dynamic_dca_distance_pct()
    anchor = m.position.last_dca_price if m.position.last_dca_price else m.position.avg_entry_price
    if m.position.side == "LONG":
        return anchor * (1 - dca_distance) - 0.0001
    return anchor * (1 + dca_distance) + 0.0001


# ============================================================================
# TEST 1: well BEFORE the soft threshold - DCA still fires normally
# ============================================================================
async def test1_before_soft_threshold_dca_fires():
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    m = await make_manager(dca_step=1, held_sec=effective - 5)  # 5s under threshold
    price = dca_trigger_price(m)
    await tick(m, price)
    print(f"TEST 1: held={(time.time()-m.position.opened_at)/3600:.4f}h < "
          f"soft={effective/3600:.4f}h -> orders placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 1, "DCA must still fire normally before the soft threshold"
    print("TEST 1: PASS - DCA unaffected before the soft threshold\n")


# ============================================================================
# TEST 2: AT / just past the soft threshold - DCA must NOT fire
# ============================================================================
async def test2_at_soft_threshold_dca_blocked():
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    m = await make_manager(dca_step=1, held_sec=effective)  # >= threshold by construction (elapsed wall time only adds margin)
    price = dca_trigger_price(m)
    await tick(m, price)
    print(f"TEST 2: held={(time.time()-m.position.opened_at)/3600:.4f}h >= "
          f"soft={effective/3600:.4f}h -> orders placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 1, "the DCA-aware timeout must place exactly one close"
    assert m.client.placed_orders[0].get("reduceOnly") == "true"
    assert m.position.pending_role == "close"
    print("TEST 2: PASS - DCA blocked and position closes exactly at the soft threshold\n")


# ============================================================================
# TEST 3: well AFTER the soft threshold (but under hard cap) - close without
# any recovery-vote or "DCA opportunity available" deferral
# ============================================================================
async def test3_after_soft_threshold_never_defers_for_dca(capsys):
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    m = await make_manager(dca_step=1, held_sec=effective + 900)  # 15 min past soft threshold
    price = dca_trigger_price(m)
    await tick(m, price)
    out = capsys.readouterr().out
    print(f"TEST 3: orders placed={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 1
    assert m.client.placed_orders[0].get("reduceOnly") == "true"
    assert "DCA opportunity available" not in out, (
        "max-hold review must never defer with reason='DCA opportunity available' once DCA is time-blocked"
    )
    assert "reason=DCA soft timeout reached" in out
    assert "action=DEFER" not in out
    print("TEST 3: PASS - no recovery-vote defer once a DCA position reaches soft timeout\n")


# ============================================================================
# TEST 4: dca_opportunity_available and the placement gate must never
# disagree - direct internal check via the shared dca_time_eligible flag
# ============================================================================
async def test4_shared_flag_never_diverges():
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    for held in (effective - 10, effective, effective + 3600):
        m = await make_manager(dca_step=1, held_sec=held)
        price = dca_trigger_price(m)
        m.current_price = price
        m.prev_price = price
        held_sec_so_far = time.time() - m.position.opened_at
        dca_time_eligible_expected = held_sec_so_far < effective
        await m._manage_open_position()
        assert len(m.client.placed_orders) == 1
        role = "close" if m.client.placed_orders[0].get("reduceOnly") == "true" else "dca"
        expected_role = "dca" if dca_time_eligible_expected else "close"
        print(f"TEST 4: held={held_sec_so_far/3600:.4f}h eligible_expected={dca_time_eligible_expected} "
              f"role={role}")
        assert role == expected_role
    print("TEST 4: PASS - before boundary DCA is eligible; at/after boundary only close is allowed\n")


# ============================================================================
# TEST 5: insufficient recovery signals may not keep a DCA position open
# ============================================================================
async def test5_time_blocked_insufficient_signals_still_closes():
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    m = await make_manager(
        dca_step=1, held_sec=effective + 300,
        confidence_kwargs={"risk_score": 0.2, "trend_direction": None, "trend_confidence": 0.0},
    )
    price = small_loss_price(m)  # meaningful loss, but no signals should agree
    m.prev_price = m.position.avg_entry_price  # velocity ~0 -> momentum_against False
    await tick(m, price)
    print(f"TEST 5: status={m.position.status} orders={len(m.client.placed_orders)}")
    assert m.position.status == "CLOSING"
    assert len(m.client.placed_orders) == 1
    assert m.client.placed_orders[0].get("reduceOnly") == "true"
    print("TEST 5: PASS - 0/2 recovery signals cannot defer the DCA soft-timeout close\n")


# ============================================================================
# TEST 6: sufficient signals also close via the same deterministic boundary
# ============================================================================
async def test6_time_blocked_sufficient_signals_closes():
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    m = await make_manager(
        dca_step=1, held_sec=effective + 300,
        confidence_kwargs={"risk_score": 0.2, "trend_direction": "SHORT", "trend_confidence": 0.60},
    )
    price = small_loss_price(m)
    # NOTE: deliberately not using the shared tick() helper here, since it
    # sets prev_price == current_price (zero velocity) - momentum_against
    # needs a genuine prev_price -> current_price delta.
    m.prev_price = m.position.avg_entry_price * 1.002  # velocity strongly against a LONG -> momentum_against True
    m.current_price = price
    await m._manage_open_position()
    print(f"TEST 6: status={m.position.status} orders={len(m.client.placed_orders)}")
    assert m.position.status in ("CLOSING", "FLAT"), (
        "2/4 recovery-risk signals must force-close via max_hold_time "
        "(status may already resolve to FLAT if the FakeClient reports no residual exchange position)"
    )
    assert len(m.client.placed_orders) == 1
    assert m.client.placed_orders[0].get("reduceOnly") == "true"
    print("TEST 6: PASS - signal values do not alter the deterministic DCA timeout close\n")


# ============================================================================
# TEST 7: hard cap always wins, regardless of dca_step/price - unaffected
# by this fix
# ============================================================================
async def test7_hard_cap_always_wins():
    m = await make_manager(dca_step=0, held_sec=trading.MAX_HOLD_TIME_HARD_CAP_SEC + 60,
                            entries=[(77.20, 1.03)], avg_entry=77.20, total_qty=1.03)
    price = m.position.avg_entry_price * 1.001  # slightly profitable, still must close on hard cap
    await tick(m, price)
    print(f"TEST 7: status={m.position.status}")
    assert m.position.status in ("CLOSING", "FLAT"), "MAX_HOLD_TIME_HARD_CAP_SEC must force-close unconditionally"
    print("TEST 7: PASS - 8h hard cap unaffected by the DCA time gate\n")


# ============================================================================
# TEST 8: step exhaustion caps exposure without crystallizing an immediate
# fee-heavy loss before the DCA-aware time boundary
# ============================================================================
async def test8_step_exhaustion_caps_exposure_before_time_boundary():
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    m = await make_manager(dca_step=trading.MAX_DCA_STEPS, held_sec=effective - 3600)  # well BEFORE soft threshold
    price = dca_trigger_price(m)
    await tick(m, price)
    print(f"TEST 8: dca_step={m.position.dca_step} (exhausted), orders={len(m.client.placed_orders)}, "
          f"status={m.position.status}")
    assert len(m.client.placed_orders) == 0, "2/2 must never submit DCA #3 or an immediate fee-heavy close"
    assert m.position.status == "OPEN"
    assert m.position.dca_step == trading.MAX_DCA_STEPS
    print("TEST 8: PASS - max_dca_exhausted caps exposure and leaves normal exits active\n")


# ============================================================================
# TEST 9: Hard Stop still fires immediately even when time-blocked (and even
# before it, proving earlier blocks are untouched)
# ============================================================================
async def test9_hard_stop_unaffected():
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    m = await make_manager(dca_step=1, held_sec=effective + 300)
    price = m.position.avg_entry_price * (1 - trading.HARD_STOP_PCT - 0.001)  # beyond hard stop
    await tick(m, price)
    print(f"TEST 9: status={m.position.status}")
    assert m.position.status in ("CLOSING", "FLAT"), "Hard Stop must still fire immediately regardless of DCA time-block state"
    print("TEST 9: PASS - Hard Stop unaffected\n")


# ============================================================================
# TEST 10: a DCA order placed one tick BEFORE the threshold is not
# mishandled/duplicated once ticks continue past the threshold while it's
# still pending (status=DCA_PENDING blocks _manage_open_position entirely)
# ============================================================================
async def test10_pending_dca_not_duplicated_across_boundary():
    effective = trading.MAX_HOLD_TIME_SEC * trading.MAX_HOLD_TIME_DCA_MULTIPLIER
    m = await make_manager(dca_step=1, held_sec=effective - 2)  # just before threshold
    price = dca_trigger_price(m)
    m.current_price = price
    m.prev_price = price
    await m._manage_open_position()
    assert len(m.client.placed_orders) == 1, "setup: DCA must have fired just before the threshold"
    assert m.position.status == "DCA_PENDING", "position must be DCA_PENDING while the order is unfilled"

    # Advance well past the threshold WITHOUT the fill landing yet.
    m.position.opened_at -= 3600  # simulate more time elapsing while still pending
    m.current_price = price
    m.prev_price = price
    await m._manage_open_position()  # should be a no-op: status != "OPEN"
    print(f"TEST 10a: status={m.position.status} orders_still={len(m.client.placed_orders)}")
    assert len(m.client.placed_orders) == 1, "_manage_open_position must not run (and must not re-place) while a DCA is DCA_PENDING"

    # Now the fill lands.
    order_id = m.client.placed_orders[-1]["newClientOrderId"] if "newClientOrderId" in m.client.placed_orders[-1] else None
    # Use the real pending_order_id the manager tracked, mirroring how the
    # exchange callback is routed in production.
    pending_id = m.position.pending_order_id
    await m.handle_order_update(order_event(pending_id, "FILLED", n=0.03, N="USDT", ap=price, z=1.6))
    print(f"TEST 10b: status={m.position.status} dca_step={m.position.dca_step} orders={len(m.client.placed_orders)}")
    assert m.position.status == "OPEN"
    assert m.position.dca_step == 2, "exactly one DCA fill applied, no duplication"
    assert len(m.client.placed_orders) == 1, "still exactly one order ever placed across the whole sequence"
    print("TEST 10: PASS - pending DCA across the threshold boundary is not mishandled or duplicated\n")


# ============================================================================
# TEST 11: restart/recovery preserves the original opened_at, so the time
# gate cannot be reset by a resync
# ============================================================================
async def test11_restart_preserves_opened_at():
    filters = bot.SymbolFilters(tick_size=0.0001, step_size=0.01, min_qty=0.01, min_notional=5.0)
    m = bot.MartingaleManager(client=FakeClient(), symbol="SOLUSDT", filters=filters, leverage=20)
    real_opened_at = time.time() - (trading.MAX_HOLD_TIME_HARD_CAP_SEC + 120)  # already past hard cap
    m.position.side = "LONG"
    m.position.status = "OPEN"
    m.position.avg_entry_price = 77.02
    m.position.total_qty = 2.63
    m.position.original_qty = 2.63
    m.position.entries = [(77.08, 1.03), (76.97, 1.6)]
    m.position.dca_step = 1
    m.position.last_dca_price = 76.97
    m.position.opened_at = real_opened_at
    await m.save_dca_state(reason="test setup")

    rows = [{"positionAmt": "2.63", "entryPrice": "77.02", "symbol": "SOLUSDT"}]
    m2 = bot.MartingaleManager(client=FakeClient(), symbol="SOLUSDT", filters=filters, leverage=20)
    await trading.initialize_sync(client=None, manager=m2, context="startup", rows=rows)

    print(f"TEST 11: restored opened_at={m2.position.opened_at:.0f} (expected {real_opened_at:.0f})")
    assert abs(m2.position.opened_at - real_opened_at) < 1.0, (
        "restart must restore the ORIGINAL opened_at, not reset the hold-time clock"
    )

    m2.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0)
    m2.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.2, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    m2.current_price = 77.02 * 1.001
    m2.prev_price = 77.02 * 1.001
    await m2._manage_open_position()
    print(f"TEST 11b: status={m2.position.status}")
    assert m2.position.status in ("CLOSING", "FLAT"), "restored (already-past-hard-cap) hold time must still force a close"
    print("TEST 11: PASS - restart cannot reset the time gate\n")


class _Capsys:
    """Tiny stand-in for pytest's capsys fixture so this file can run with
    plain `python3` (matching the existing test_*.py convention in this
    repo) while still letting TEST 3 assert on printed log text."""
    def __init__(self):
        import io
        import sys
        self._io = io.StringIO()
        self._real_stdout = sys.stdout

    def start(self):
        import sys
        sys.stdout = self._io

    def readouterr(self):
        import sys
        sys.stdout = self._real_stdout
        out = self._io.getvalue()
        self._io.seek(0)
        self._io.truncate(0)
        sys.stdout = self._io
        return type("R", (), {"out": out})()

    def stop(self):
        import sys
        sys.stdout = self._real_stdout


async def run_all():
    await test1_before_soft_threshold_dca_fires()
    await test2_at_soft_threshold_dca_blocked()

    cap = _Capsys()
    cap.start()
    try:
        await test3_after_soft_threshold_never_defers_for_dca(cap)
    finally:
        cap.stop()

    await test4_shared_flag_never_diverges()
    await test5_time_blocked_insufficient_signals_still_closes()
    await test6_time_blocked_sufficient_signals_closes()
    await test7_hard_cap_always_wins()
    await test8_step_exhaustion_caps_exposure_before_time_boundary()
    await test9_hard_stop_unaffected()
    await test10_pending_dca_not_duplicated_across_boundary()
    await test11_restart_preserves_opened_at()
    print("ALL DCA TIME-GATE FIX TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run_all())
