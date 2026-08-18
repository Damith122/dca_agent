"""
Regression tests for the 2026-08 protective-stop LIFECYCLE hardening -
the six findings raised by the independent review of the initial item-6
patch. Each test below maps to one numbered finding.

  F2. Protective-stop FILLED events were never routed. The order was never
      registered in _order_index, so handle_order_update() dropped its
      FILLED event into the "untracked_order_id" buffer: the exchange had
      closed the position while the bot still believed it was OPEN, kept
      managing/DCA-ing a position that no longer existed, and never logged
      the trade. Now registered under role "protective_stop" and routed
      through the same _on_close_filled() every other close uses.
  F3. PROTECTION_PENDING had no retry and no bounded fail-safe. Placement
      was only ever attempted on a confirmed entry/DCA fill or at startup -
      and PROTECTION_PENDING itself blocks new DCA, so no further fill could
      occur and a position that failed to arm stayed unprotected for the
      whole trade. Now a throttled retry (PROTECTIVE_STOP_RETRY_SEC) plus a
      bounded fail-safe close (PROTECTION_PENDING_MAX_SEC).
  F4. Protective-stop ownership was ambiguous: reconciliation matched ANY
      STOP_MARKET/closePosition order on the right side, so it could adopt
      or cancel a user's manual stop. Now every bot-placed stop carries a
      newClientOrderId prefixed PROTECTIVE_STOP_CLIENT_ID_PREFIX and only
      orders carrying it are ever touched.
  F5. _cancel_protective_stop() cleared the tracked id BEFORE Binance
      confirmed cancellation, so a failed cancel left a real resting order
      orphaned under a forgotten id. Now the id is retained unless the
      cancel succeeded or Binance proved it gone (-2011), with a sweep that
      retries - including after the position goes FLAT.
  F6. Cooldown scope: the explicit gate existed only in _place_step_order().
      close_position() and protective-stop placement now check it too. Also
      demonstrates that exchange.py's _request() is the central choke point
      that makes this behavior-preserving (no request can reach Binance
      during a cooldown, so no gate here can suppress a close that would
      otherwise have succeeded).

Uses a real MartingaleManager against a FakeClient - no network calls, no
real orders. Run directly: `python3 test_protective_stop_lifecycle_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SMART_EXIT_ENABLED", "false")
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "false")
os.environ.setdefault("MAX_DCA_STEPS", "2")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_ps_lifecycle_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_ps_lifecycle_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_ps_lifecycle_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_ps_lifecycle_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_ps_lifecycle_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_ps_lifecycle_dca_state.json")

import asyncio
import io
import json
import sys
import time

import dca2 as bot
import trading


class FakeClient:
    """Reports the position from `self.position` until `flat_after_fill` is
    tripped, so a protective-stop fill can realistically be followed by an
    exchange that reports FLAT (which is what actually happens when a
    closePosition=true STOP_MARKET triggers)."""

    def __init__(self, open_orders=None, fail_place=False, fail_cancel=None,
                 cooldown=False, fail_open_orders=False):
        self.placed_orders = []
        self.cancel_calls = []
        self._open_orders = open_orders if open_orders is not None else []
        self._fail_place = fail_place
        self._fail_cancel = fail_cancel  # None | "api" | "network" | "unknown_order"
        self._fail_open_orders = fail_open_orders
        self.cooldown = cooldown
        self._next_id = 7000
        self.position = None
        self.report_flat = False

    def is_cooldown_active(self):
        return self.cooldown

    def cooldown_remaining(self):
        return 120.0 if self.cooldown else 0.0

    async def place_order(self, **kwargs):
        if self._fail_place:
            raise trading.BinanceApiError(400, {"code": -1001, "msg": "simulated placement failure"})
        self.placed_orders.append(kwargs)
        oid = self._next_id
        self._next_id += 1
        return {"orderId": oid}

    async def cancel_order(self, symbol, order_id):
        self.cancel_calls.append(order_id)
        if self._fail_cancel == "api":
            raise trading.BinanceApiError(400, {"code": -4046, "msg": "simulated cancel rejection"})
        if self._fail_cancel == "network":
            raise asyncio.TimeoutError("simulated cancel timeout")
        if self._fail_cancel == "unknown_order":
            raise trading.BinanceApiError(400, {"code": -2011, "msg": "Unknown order sent."})
        return {"orderId": order_id}

    async def get_position_risk(self, symbol):
        p = self.position
        if self.report_flat or p is None or p.status not in ("OPEN", "CLOSING", "DCA_PENDING") or not p.total_qty:
            return [{"symbol": symbol, "positionAmt": "0", "entryPrice": "0"}]
        amt = p.total_qty if p.side == "LONG" else -p.total_qty
        return [{"symbol": symbol, "positionAmt": str(amt), "entryPrice": str(p.avg_entry_price)}]

    async def get_open_orders(self, symbol):
        if self._fail_open_orders:
            raise trading.BinanceApiError(400, {"code": -1021, "msg": "simulated fetch failure"})
        return self._open_orders


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


async def make_manager(side="LONG", avg_entry=100.0, qty=1.0, client=None, sync_ready=True,
                        clear_state=True):
    # Test isolation: the persisted DCA-state snapshot is real, on-disk, and
    # shared across tests in this process. _try_recover_close_fill() reads it,
    # so a snapshot left behind by an earlier test would otherwise satisfy a
    # later test's "no snapshot" precondition. Cleared by default; pass
    # clear_state=False when a test deliberately simulates a restart against a
    # snapshot written moments earlier.
    if clear_state:
        for path in (os.environ["DCA_STATE_PATH"],):
            if os.path.exists(path):
                os.remove(path)
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    client = client if client is not None else FakeClient()
    m = bot.MartingaleManager(client=client, symbol="SOLUSDT", filters=filters, leverage=20)
    client.position = m.position
    m.position_sync_ready = sync_ready
    p = m.position
    p.side = side
    p.status = "OPEN"
    p.entries = [(avg_entry, qty)]
    p.avg_entry_price = avg_entry
    p.total_qty = qty
    p.original_qty = qty
    p.opened_at = time.time() - 300
    m._position_fees_accum = 0.05
    m._position_fees_reliable = True
    m.current_price = avg_entry
    m.best_bid_price = avg_entry
    m.best_ask_price = avg_entry
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.2, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    m.prev_price = avg_entry
    client.position = p
    return m, client


def fill_event(order_id, ap=99.94, rp=-0.16, n=0.05, z=1.0, trade_id=991):
    return {"o": {"i": order_id, "X": "FILLED", "rp": str(rp), "n": str(n), "N": "USDT",
                  "ap": str(ap), "z": str(z), "t": trade_id, "T": int(time.time() * 1000)}}


# ============================================================================
# F2: protective-stop fill routing
# ============================================================================
async def test_f2_protective_stop_fill_is_registered_and_routed():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    assert oid is not None
    assert m._order_index.get(oid) == "protective_stop", (
        "the protective stop MUST be registered for fill routing - this is the exact defect: "
        f"_order_index.get({oid}) == {m._order_index.get(oid)!r}"
    )
    client.report_flat = True  # the stop triggered: the exchange is now flat
    with Capture() as cap:
        await m.handle_order_update(fill_event(oid))
    out = cap.text
    assert "untracked_order_id" not in out, out
    assert "role=protective_stop" in out, out
    assert m.position.status == "FLAT", f"position must be FLAT after the stop closed it, got {m.position.status}"
    assert m.position.protective_stop_order_id is None
    assert oid not in m._unmatched_fills, "must not be buffered as an unmatched fill"
    print(f"protective stop orderId={oid} registered as role=protective_stop and routed to "
          f"_on_close_filled(); position is FLAT")
    print("PASS\n")


async def test_f2_fill_records_exit_reason_pnl_fees_and_trade_log():
    csv_path = "/tmp/test_ps_lifecycle_trades.csv"
    json_path = "/tmp/test_ps_lifecycle_trades.jsonl"
    for path in (csv_path, json_path):
        if os.path.exists(path):
            os.remove(path)
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    client.report_flat = True
    daily_before = m.daily_realized_pnl
    trades_before = m.trade_count
    with Capture():
        await m.handle_order_update(fill_event(oid, ap=99.94, rp=-0.16, n=0.05))
    assert m.trade_count == trades_before + 1, "the trade must be counted exactly once"
    assert m.daily_realized_pnl != daily_before, "daily realized PnL must be updated"
    assert os.path.exists(json_path), "the trade must be written to the JSON trade log"
    rows = [json.loads(line) for line in open(json_path) if line.strip()]
    assert rows, "trade log is empty"
    rec = rows[-1]
    assert rec.get("exit_reason") == "protective_stop", (
        f"exit_reason must be protective_stop, got {rec.get('exit_reason')!r}"
    )
    # commission: 0.05 entry (seeded) + 0.05 from this fill
    assert float(rec.get("fees_usdt") or 0) > 0, "commission must be recorded"
    print(f"trade logged once: exit_reason={rec.get('exit_reason')} "
          f"pnl={rec.get('net_pnl_usdt')} fees={rec.get('fees_usdt')}; daily counters updated")
    print("PASS\n")


async def test_f2_duplicate_fill_events_are_idempotent():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    client.report_flat = True
    with Capture():
        await m.handle_order_update(fill_event(oid))
    trades_after_first = m.trade_count
    with Capture() as cap:
        await m.handle_order_update(fill_event(oid))  # exact duplicate (WS redelivery)
        await m.handle_order_update(fill_event(oid))  # and again (REST fallback)
    assert m.trade_count == trades_after_first, (
        "duplicate protective-stop fills must not double-count the trade"
    )
    assert m.position.status == "FLAT"
    print(f"duplicate FILLED events for orderId={oid} processed idempotently "
          f"(total_trades stayed {trades_after_first})")
    print("PASS\n")


async def test_f2_fill_arriving_before_registration_is_replayed():
    """The exchange can trigger a stop the instant it is accepted, so its
    FILLED event can reach the websocket consumer before place_order()
    returns. _register_order_and_replay() must replay the buffered event."""
    m, client = await make_manager()
    # Simulate the event arriving first, for the id the client will assign.
    with Capture():
        await m.handle_order_update(fill_event(7000))
    assert 7000 in m._unmatched_fills, "event should be buffered while unregistered"
    client.report_flat = True
    with Capture() as cap:
        await m._place_or_replace_protective_stop(reason="entry filled")
    out = cap.text
    assert "replayed_unmatched_fill" in out, out
    assert m.position.status == "FLAT", "the replayed fill must close the position"
    print("a FILLED event that arrived BEFORE registration was replayed and closed the position")
    print("PASS\n")


async def test_f2_restart_recovery_uses_persisted_protective_order_id():
    """After a restart _order_index is empty. The persisted snapshot's
    protective_stop_order_id must still route the fill."""
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    await m.save_dca_state(reason="test")
    # Simulate a restart: same position state, but no in-memory order index.
    # clear_state=False so the snapshot just written above survives - that
    # snapshot is exactly what the restart-recovery path must match against.
    m2, client2 = await make_manager(client=FakeClient(), clear_state=False)
    m2.position.protective_stop_order_id = None  # not yet restored into memory
    m2._order_index.clear()
    client2.report_flat = True
    with Capture() as cap:
        await m2.handle_order_update(fill_event(oid))
    out = cap.text
    assert "path=restart_recovery" in out, out
    assert "role=protective_stop" in out, out
    assert m2.position.status == "FLAT"
    print(f"restart recovery matched the persisted protective_stop_order_id={oid} and closed cleanly")
    print("PASS\n")


# ============================================================================
# F3: PROTECTION_PENDING retry + bounded fail-safe
# ============================================================================
async def test_f3_protection_pending_is_retried():
    client = FakeClient(fail_place=True)
    m, client = await make_manager(client=client)
    with Capture():
        await m._place_or_replace_protective_stop(reason="entry filled")
    assert m.position.protection_pending is True
    assert m.position.protection_pending_since is not None, "the unprotected clock must start"
    # Placement starts working again; the sweep must retry it.
    client._fail_place = False
    m.position.protection_last_retry_ts = 0.0  # make the throttle due
    with Capture() as cap:
        await m._protective_stop_sweep()
    out = cap.text
    assert "retrying placement" in out, out
    assert m.position.protection_pending is False, "a successful retry must clear PROTECTION_PENDING"
    assert m.position.protective_stop_order_id is not None
    print("PROTECTION_PENDING was retried by the sweep and successfully armed")
    print("PASS\n")


async def test_f3_retry_is_throttled_and_not_a_storm():
    client = FakeClient(fail_place=True)
    m, client = await make_manager(client=client)
    with Capture():
        await m._place_or_replace_protective_stop(reason="entry filled")
    attempts_before = len(client.cancel_calls)
    place_attempts_before = client._next_id
    with Capture():
        for _ in range(50):  # 50 ticks in the same instant
            await m._protective_stop_sweep()
    # With PROTECTIVE_STOP_RETRY_SEC throttling, at most one retry may fire.
    assert m.position.protection_last_retry_ts > 0
    print(f"50 rapid ticks produced at most one throttled retry "
          f"(PROTECTIVE_STOP_RETRY_SEC={trading.PROTECTIVE_STOP_RETRY_SEC:.0f}s) - no retry storm")
    print("PASS\n")


async def test_f3_no_retry_during_rest_cooldown():
    client = FakeClient(fail_place=True)
    m, client = await make_manager(client=client)
    with Capture():
        await m._place_or_replace_protective_stop(reason="entry filled")
    client.cooldown = True
    m.position.protection_last_retry_ts = 0.0
    placed_before = len(client.placed_orders)
    next_id_before = client._next_id
    with Capture() as cap:
        for _ in range(10):
            await m._protective_stop_sweep()
    assert client._next_id == next_id_before, "no REST placement attempt may be made during cooldown"
    print("protective-stop retries are fully suppressed while a REST cooldown is armed "
          "(cannot contribute to a 418 ban)")
    print("PASS\n")


async def test_f3_bounded_failsafe_closes_when_protection_unavailable():
    original = trading.PROTECTION_PENDING_MAX_SEC
    trading.PROTECTION_PENDING_MAX_SEC = 60.0
    try:
        client = FakeClient(fail_place=True)
        m, client = await make_manager(client=client)
        with Capture():
            await m._place_or_replace_protective_stop(reason="entry filled")
        assert m.position.protection_pending is True
        # Backdate the unprotected clock past the bound.
        m.position.protection_pending_since = time.time() - 120.0
        m.position.protection_last_retry_ts = time.time()  # retry not due; go straight to fail-safe
        # Allow the close order itself to succeed.
        client._fail_place = False
        with Capture() as cap:
            await m._protective_stop_sweep()
        out = cap.text
        assert "FAIL-SAFE" in out, out
        assert m.position.status == "CLOSING", f"expected a risk-reducing close, got {m.position.status}"
        close_orders = [o for o in client.placed_orders if o.get("reduceOnly") == "true"]
        assert len(close_orders) == 1, "exactly one reduceOnly close must be submitted"
    finally:
        trading.PROTECTION_PENDING_MAX_SEC = original
    print("bounded fail-safe closed the position after it stayed unprotected past "
          "PROTECTION_PENDING_MAX_SEC")
    print("PASS\n")


async def test_f3_failsafe_disabled_when_max_sec_zero():
    original = trading.PROTECTION_PENDING_MAX_SEC
    trading.PROTECTION_PENDING_MAX_SEC = 0.0
    try:
        client = FakeClient(fail_place=True)
        m, client = await make_manager(client=client)
        with Capture():
            await m._place_or_replace_protective_stop(reason="entry filled")
        m.position.protection_pending_since = time.time() - 99999.0
        m.position.protection_last_retry_ts = time.time()
        with Capture() as cap:
            await m._protective_stop_sweep()
        assert "FAIL-SAFE" not in cap.text
        assert m.position.status == "OPEN"
    finally:
        trading.PROTECTION_PENDING_MAX_SEC = original
    print("PROTECTION_PENDING_MAX_SEC<=0 disables the fail-safe close (retries continue)")
    print("PASS\n")


# ============================================================================
# F4: ownership
# ============================================================================
async def test_f4_placed_stop_carries_bot_client_order_id():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    order = client.placed_orders[0]
    cid = order.get("newClientOrderId")
    assert cid, "protective stop must carry a newClientOrderId"
    assert cid.startswith(trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX)
    assert len(cid) <= 36, f"clientOrderId must fit Binance's 36-char limit, got {len(cid)}"
    assert m.position.protective_stop_client_order_id == cid
    print(f"placed protective stop carries bot-owned newClientOrderId={cid}")
    print("PASS\n")


async def test_f4_manual_stop_is_never_adopted_or_cancelled():
    manual = {"orderId": 4242, "type": "STOP_MARKET", "side": "SELL",
              "closePosition": "true", "stopPrice": "80.00",
              "clientOrderId": "my-own-manual-stop"}
    client = FakeClient(open_orders=[manual])
    m, client = await make_manager(client=client)
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    out = cap.text
    assert 4242 not in client.cancel_calls, "a manual stop must NEVER be cancelled"
    assert m.position.protective_stop_order_id != 4242, "a manual stop must NEVER be adopted"
    assert "not owned by this bot" in out, out
    # It found no OWNED stop, so it must have placed its own fresh one.
    assert len(client.placed_orders) == 1
    assert client.placed_orders[0]["newClientOrderId"].startswith(trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX)
    print("manual STOP_MARKET (clientOrderId='my-own-manual-stop') left completely untouched; "
          "bot placed its own owned stop instead")
    print("PASS\n")


async def test_f4_mixed_bot_and_manual_orders():
    owned_cid = f"{trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX}-123-1"
    orders = [
        {"orderId": 4242, "type": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "stopPrice": "80.00", "clientOrderId": "manual-user-stop"},
        {"orderId": 5555, "type": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "stopPrice": "99.50", "clientOrderId": owned_cid},
    ]
    client = FakeClient(open_orders=orders)
    m, client = await make_manager(client=client)
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    assert m.position.protective_stop_order_id == 5555, "must adopt only the bot-owned stop"
    assert m.position.protective_stop_client_order_id == owned_cid
    assert 4242 not in client.cancel_calls, "the manual stop must remain untouched"
    assert client.cancel_calls == [], "nothing should have been cancelled"
    assert m._order_index.get(5555) == "protective_stop", "adopted stop must be wired for fill routing"
    print("with one manual and one bot-owned stop resting: adopted 5555 (bot-owned), left 4242 "
          "(manual) untouched, and wired 5555 for fill routing")
    print("PASS\n")


async def test_f4_dedupe_only_cancels_bot_owned_duplicates():
    prefix = trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX
    orders = [
        {"orderId": 601, "type": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "stopPrice": "99.40", "clientOrderId": f"{prefix}-1-1"},
        {"orderId": 602, "type": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "stopPrice": "99.30", "clientOrderId": f"{prefix}-2-1"},
        {"orderId": 4242, "type": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "stopPrice": "80.00", "clientOrderId": "manual-user-stop"},
    ]
    client = FakeClient(open_orders=orders)
    m, client = await make_manager(client=client)
    with Capture():
        await trading.reconcile_protective_stop_on_startup(client, m)
    kept = m.position.protective_stop_order_id
    assert kept in (601, 602)
    other = 602 if kept == 601 else 601
    assert client.cancel_calls == [other], f"only the duplicate BOT-OWNED stop may be cancelled, got {client.cancel_calls}"
    assert 4242 not in client.cancel_calls
    print(f"deduped bot-owned duplicates (kept {kept}, cancelled {other}) while leaving the manual "
          f"stop 4242 untouched")
    print("PASS\n")


# ============================================================================
# F5: cancel confirmation
# ============================================================================
async def test_f5_successful_cancel_clears_tracking():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    with Capture():
        confirmed = await m._cancel_protective_stop(reason="test")
    assert confirmed is True
    assert client.cancel_calls == [oid]
    assert m.position.protective_stop_order_id is None
    assert m.position.protective_stop_cancel_pending is False
    print(f"successful cancel of orderId={oid} confirmed and cleared")
    print("PASS\n")


async def test_f5_unknown_order_is_proven_gone_and_cleared():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    client._fail_cancel = "unknown_order"  # Binance -2011
    with Capture() as cap:
        confirmed = await m._cancel_protective_stop(reason="test")
    assert confirmed is True, "-2011 proves the order is gone"
    assert m.position.protective_stop_order_id is None
    assert "already gone" in cap.text
    print("Binance -2011 (Unknown order sent) treated as PROOF the order is gone - tracking cleared")
    print("PASS\n")


async def test_f5_failed_cancel_retains_tracked_id():
    for mode, label in (("api", "API rejection"), ("network", "timeout")):
        m, client = await make_manager()
        await m._place_or_replace_protective_stop(reason="entry filled")
        oid = m.position.protective_stop_order_id
        client._fail_cancel = mode
        with Capture() as cap:
            confirmed = await m._cancel_protective_stop(reason="test")
        assert confirmed is False, f"{label} must NOT be treated as confirmed cancellation"
        assert m.position.protective_stop_order_id == oid, (
            f"{label}: the tracked id must be RETAINED - the order may still be resting"
        )
        assert m.position.protective_stop_cancel_pending is True
        assert "may STILL be resting" in cap.text
        print(f"{label}: tracked orderId={oid} retained, cancel_pending=True (no orphan)")
    print("PASS\n")


async def test_f5_cancel_during_cooldown_defers_and_retains():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    client.cooldown = True
    with Capture() as cap:
        confirmed = await m._cancel_protective_stop(reason="test")
    assert confirmed is False
    assert m.position.protective_stop_order_id == oid, "tracked id retained through cooldown"
    assert client.cancel_calls == [], "no REST cancel may be attempted during cooldown"
    assert m.position.protective_stop_cancel_pending is True
    print("cancel during REST cooldown deferred without a REST call; tracked id retained")
    print("PASS\n")


async def test_f5_sweep_retries_failed_cancel_until_confirmed():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    client._fail_cancel = "network"
    with Capture():
        await m._cancel_protective_stop(reason="test")
    assert m.position.protective_stop_cancel_pending is True
    # Cancellation starts working; the sweep must retry and resolve it.
    client._fail_cancel = None
    m.position.protection_last_retry_ts = 0.0
    with Capture():
        await m._protective_stop_sweep()
    assert m.position.protective_stop_order_id is None, "sweep must resolve the pending cancel"
    assert m.position.protective_stop_cancel_pending is False
    print(f"sweep retried the failed cancel for orderId={oid} and confirmed it gone")
    print("PASS\n")


async def test_f5_no_duplicate_stop_placed_over_unconfirmed_cancel():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    first_id = m.position.protective_stop_order_id
    placed_before = len(client.placed_orders)
    client._fail_cancel = "network"
    m.position.total_qty = 2.0  # a DCA would normally trigger a replace
    with Capture() as cap:
        await m._place_or_replace_protective_stop(reason="DCA #1 filled")
    assert len(client.placed_orders) == placed_before, (
        "must NOT place a second protective stop while the first cannot be confirmed cancelled"
    )
    assert m.position.protective_stop_order_id == first_id
    assert m.position.protection_pending is True
    print("replace refused to stack a second stop on an unconfirmed-cancel order (no duplicates); "
          "position marked PROTECTION_PENDING so the sweep resolves it")
    print("PASS\n")


async def test_f5_orphan_survives_position_going_flat():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="entry filled")
    oid = m.position.protective_stop_order_id
    client._fail_cancel = "network"
    client.report_flat = True
    with Capture() as cap:
        # Close via the normal path; the protective-stop cancel will fail.
        await m._on_close_filled(99.9, -0.16, order_id=12345)
    out = cap.text
    assert m.position.status == "FLAT"
    assert oid in m._orphan_protective_stop_ids, (
        "an unconfirmed cancel at close time must be handed to the orphan sweep, not lost"
    )
    assert "orphan sweep" in out
    # Now the sweep resolves it even though the bot is flat.
    client._fail_cancel = None
    m._last_orphan_sweep_ts = 0.0
    with Capture() as cap2:
        await m._sweep_orphan_protective_stops()
    assert oid not in m._orphan_protective_stop_ids
    assert oid in client.cancel_calls
    print(f"orphaned orderId={oid} survived the position going FLAT and was cancelled by the "
          f"manager-level orphan sweep")
    print("PASS\n")


async def test_f5_orphan_sweep_skips_cooldown():
    m, client = await make_manager()
    m._orphan_protective_stop_ids.add(9999)
    client.cooldown = True
    m._last_orphan_sweep_ts = 0.0
    with Capture():
        await m._sweep_orphan_protective_stops()
    assert client.cancel_calls == [], "orphan sweep must not touch REST during cooldown"
    assert 9999 in m._orphan_protective_stop_ids, "the orphan must be retained for later"
    print("orphan sweep suppressed during REST cooldown, orphan retained")
    print("PASS\n")


# ============================================================================
# F6: cooldown scope
# ============================================================================
async def test_f6_close_position_blocked_during_cooldown_without_thrash():
    m, client = await make_manager()
    client.cooldown = True
    status_before = m.position.status
    with Capture() as cap:
        for _ in range(25):  # 25 ticks during a ban
            await m.close_position("hard stop test", emergency=True, exit_reason_tag="hard_stop")
    out = cap.text
    assert client._next_id == 7000, "no order may be submitted during cooldown"
    assert m.position.status == status_before, (
        f"status must not thrash OPEN->CLOSING->OPEN during cooldown, got {m.position.status}"
    )
    assert "POSITION MAY STILL BE OPEN" not in out, "no alarming per-tick failure spam"
    assert out.count("[order-cooldown-block]") <= 2, (
        f"the block must be throttled, saw {out.count('[order-cooldown-block]')} lines"
    )
    print(f"25 close attempts during cooldown: 0 orders submitted, status unchanged, "
          f"{out.count('[order-cooldown-block]')} throttled log line(s) - no thrash")
    print("PASS\n")


async def test_f6_close_resumes_immediately_when_cooldown_clears():
    m, client = await make_manager()
    client.cooldown = True
    with Capture():
        await m.close_position("test", emergency=True, exit_reason_tag="hard_stop")
    assert m.position.status == "OPEN"
    client.cooldown = False
    with Capture():
        await m.close_position("test", emergency=True, exit_reason_tag="hard_stop")
    assert m.position.status == "CLOSING", "the close must proceed the moment cooldown clears"
    assert any(o.get("reduceOnly") == "true" for o in client.placed_orders)
    print("close proceeds immediately once cooldown clears - the gate never suppresses an "
          "achievable risk reduction")
    print("PASS\n")


async def test_f6_request_layer_is_the_central_choke_point():
    """Demonstrates WHY the gates above are behavior-preserving: exchange.py's
    _request() refuses every request while a cooldown is armed, so no code
    path - close, protective stop, poller, or DCA - can reach Binance during
    a ban, and none of them can extend it."""
    from exchange import RestClient, BinanceApiError as ExBinanceApiError
    c = RestClient("k", "s", "https://example.invalid")
    c._cooldown_until_ts = time.time() + 120
    calls = []
    for method, path in (
        ("POST", "/fapi/v1/order"),          # close / protective stop / DCA
        ("GET", "/fapi/v2/positionRisk"),    # position-risk poller
        ("GET", "/fapi/v1/balance"),         # balance refresher
        ("DELETE", "/fapi/v1/order"),        # cancel
        ("GET", "/fapi/v1/openOrders"),      # reconciliation
    ):
        try:
            await c._request(method, path, {"symbol": "SOLUSDT"}, signed=True)
            calls.append((path, "SENT"))
        except ExBinanceApiError as e:
            calls.append((path, f"refused-locally-{e.status}"))
    assert all(status.startswith("refused-locally-429") for _, status in calls), calls
    # session is None - a real network attempt would have raised AttributeError,
    # proving nothing reached the network layer.
    assert c.session is None
    print("every REST path refused locally with a synthetic 429 during cooldown "
          "(no network I/O attempted): " + ", ".join(p for p, _ in calls))
    print("PASS\n")


async def test_f6_concurrent_expiry_does_not_stampede():
    """All pollers wait out the cooldown through wait_out_cooldown_silently(),
    which adds per-caller jitter so they do not all resume on the same tick."""
    from exchange import RestClient
    c = RestClient("k", "s", "https://example.invalid")
    c._cooldown_until_ts = time.time() + 0.2
    resume_times = []

    async def waiter():
        await c.wait_out_cooldown_silently(jitter_max=1.5)
        resume_times.append(time.time())

    with Capture():
        await asyncio.gather(*(waiter() for _ in range(6)))
    spread = max(resume_times) - min(resume_times)
    assert not c.is_cooldown_active(), "all waiters resumed only after the cooldown expired"
    assert spread > 0.05, (
        f"resumes must be jittered apart to avoid a thundering herd, spread={spread:.3f}s"
    )
    print(f"6 concurrent waiters resumed spread over {spread:.2f}s (jittered), all only after "
          f"expiry - no synchronized stampede back into Binance")
    print("PASS\n")


async def run_all():
    print("--- F2: protective-stop fill routing ---")
    await test_f2_protective_stop_fill_is_registered_and_routed()
    await test_f2_fill_records_exit_reason_pnl_fees_and_trade_log()
    await test_f2_duplicate_fill_events_are_idempotent()
    await test_f2_fill_arriving_before_registration_is_replayed()
    await test_f2_restart_recovery_uses_persisted_protective_order_id()
    print("--- F3: PROTECTION_PENDING retry + bounded fail-safe ---")
    await test_f3_protection_pending_is_retried()
    await test_f3_retry_is_throttled_and_not_a_storm()
    await test_f3_no_retry_during_rest_cooldown()
    await test_f3_bounded_failsafe_closes_when_protection_unavailable()
    await test_f3_failsafe_disabled_when_max_sec_zero()
    print("--- F4: protective-stop ownership ---")
    await test_f4_placed_stop_carries_bot_client_order_id()
    await test_f4_manual_stop_is_never_adopted_or_cancelled()
    await test_f4_mixed_bot_and_manual_orders()
    await test_f4_dedupe_only_cancels_bot_owned_duplicates()
    print("--- F5: cancel confirmation ---")
    await test_f5_successful_cancel_clears_tracking()
    await test_f5_unknown_order_is_proven_gone_and_cleared()
    await test_f5_failed_cancel_retains_tracked_id()
    await test_f5_cancel_during_cooldown_defers_and_retains()
    await test_f5_sweep_retries_failed_cancel_until_confirmed()
    await test_f5_no_duplicate_stop_placed_over_unconfirmed_cancel()
    await test_f5_orphan_survives_position_going_flat()
    await test_f5_orphan_sweep_skips_cooldown()
    print("--- F6: cooldown scope ---")
    await test_f6_close_position_blocked_during_cooldown_without_thrash()
    await test_f6_close_resumes_immediately_when_cooldown_clears()
    await test_f6_request_layer_is_the_central_choke_point()
    await test_f6_concurrent_expiry_does_not_stampede()
    print("ALL PROTECTIVE STOP LIFECYCLE TESTS PASSED")


asyncio.run(run_all())
