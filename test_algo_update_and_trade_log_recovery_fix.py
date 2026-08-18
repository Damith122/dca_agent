"""Offline regression tests for the 2026-08 LIVE incident in which a
protective stop closed a real position and the trade was NEVER recorded.

WHAT HAPPENED (Railway deployment 905935aa, 2026-08-18)
--------------------------------------------------------------------------
  17:56:41  ENTRY FILLED [INITIAL] SHORT qty=1.04 @ 76.90
  17:56:41  [protective-stop] PLACED ALGO ... triggerPrice=76.9900 algoId=3000002144104022
  18:07:48  [user-ws] ALGO_UPDATE received          <- x3, and NO [algo-update] line
  18:07:48  [user-ws] ORDER_TRADE_UPDATE received order_id=233511993724 status=FILLED
  18:07:48  [fill-trace] ... reason=untracked_order_id
  18:07:54  [sync:periodic poll] exchange reports NO open position, but local
            state was status=OPEN side=SHORT. Resetting to FLAT ...
  18:07:58  [status] status=FLAT ... trades=0  session_pnl=+0.0000

The position closed on the exchange for a real -$0.0936, and the bot recorded
nothing: no trades_log_LIVE_SOLUSDT.csv, no .jsonl, trades=0. The trade-sync
cursor on the brain-state branch was left at {"last_trade_id": 3460311789} -
exactly one below the close fill 3460311790 - permanently wedged.

THE FOUR FAILURES, AND THE FIX EACH TEST PINS DOWN
--------------------------------------------------------------------------
  A  handle_algo_update() parsed the payload from exactly three wrapper keys
     and three literal field names. The live envelope matched none of them,
     so ownership could not be established and the method returned BEFORE its
     own [algo-update] diagnostic - three "ALGO_UPDATE received" lines with
     zero "[algo-update]" lines. The stop's child order id was never wired,
     so its fill could not be routed.

  B  reconcile_trade_history_from_exchange() started its window at
     max(cursor, _last_live_trade_id) + 1. For an OPEN position that is the
     ENTRY fill, so the next pass fetched only the CLOSE fill. With no
     matching entry the reconstruction read that lone BUY as a new LONG
     *opening*, found it unclosed, and skipped it.

  C  initialize_sync() then replaced PositionState with a blank one - the only
     record of what the fill belonged to - so nothing could ever attribute it.

  D  cursor_cap pinned the cursor one below the orphan, so every later pass
     repeated the same misreading forever. The safety net was jammed.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_algo_update_and_trade_log_recovery_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SYMBOL", "SOLUSDT")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_algofix_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_algofix_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_algofix_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_algofix_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_algofix_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_algofix_dca_state.json")

import asyncio
import io
import sys
import time

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


def make_manager(client=None):
    filters = bot.SymbolFilters(
        tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0,
    )
    return bot.MartingaleManager(
        client=client, symbol="SOLUSDT", filters=filters, leverage=20,
    )


def open_short_at_76_90(m, first_trade_id=3460311000):
    """Puts the manager in exactly the state the live bot was in at 18:07:47."""
    p = m.position
    p.status = "OPEN"
    p.side = "SHORT"
    p.total_qty = 1.04
    p.original_qty = 1.04
    p.avg_entry_price = 76.90
    p.entries = [(76.90, 1.04)]
    p.opened_at = time.time() - 667  # 17:56:41 -> 18:07:48
    p.protective_stop_algo_id = 3000002144104022
    p.protective_stop_client_algo_id = "bv2ps-7075801113-1"
    m._open_position_first_trade_id = first_trade_id
    m._last_live_trade_id = first_trade_id
    return m


# ============================================================================
# FIX A - ALGO_UPDATE ENVELOPE PARSING
# ============================================================================

# ----------------------------------------------------------------------------
# TEST 1: the exact failure - an envelope the old parser could not read must
# no longer vanish silently.
# ----------------------------------------------------------------------------
async def test_unparsed_envelope_is_never_silently_dropped():
    print("=== test_unparsed_envelope_is_never_silently_dropped ===")

    class RestStub:
        def __init__(self):
            self.algo_queries = 0

        def is_cooldown_active(self):
            return False

        async def get_algo_order(self, algo_id=None, client_algo_id=None):
            self.algo_queries += 1
            return {
                "algoId": 3000002144104022,
                "clientAlgoId": "bv2ps-7075801113-1",
                "algoStatus": "TRIGGERED",
                "actualOrderId": 233511993724,
            }

    client = RestStub()
    m = open_short_at_76_90(make_manager(client))

    # An envelope shape the old ("ao"/"a"/"o" + literal names) parser could
    # not read at all - the whole point is that we do NOT know the real live
    # shape, so an unknown one must degrade safely, not silently.
    with Capture() as cap:
        await m.handle_algo_update({"e": "ALGO_UPDATE", "T": 1, "mystery": {"zzz": 1}})

    print(f"TEST 1: algo_queries={client.algo_queries}")
    print(f"TEST 1: logged={'UNATTRIBUTED' in cap.text}")
    assert "UNATTRIBUTED" in cap.text, (
        "an ALGO_UPDATE that cannot be attributed MUST be logged - the live "
        "incident was invisible precisely because this returned in silence"
    )
    assert client.algo_queries == 1, (
        "while tracking a stop on an open position, an unparsable envelope must "
        "escalate to an authoritative REST lookup rather than be assumed irrelevant"
    )
    assert m.position.protective_stop_actual_order_id == 233511993724, (
        "the REST lookup's actualOrderId must be wired up so the close fill can route"
    )
    print("TEST 1: PASS - unparsable envelope is logged AND resolved over REST\n")


# ----------------------------------------------------------------------------
# TEST 2: the tolerant extractor reads every plausible envelope/field spelling.
# ----------------------------------------------------------------------------
def test_extractor_handles_every_envelope_shape():
    print("=== test_extractor_handles_every_envelope_shape ===")

    shapes = {
        "documented 'ao' wrapper": {"e": "ALGO_UPDATE", "ao": {
            "algoId": 7, "clientAlgoId": "bv2ps-1-1", "algoStatus": "TRIGGERED",
            "actualOrderId": 42}},
        "'a' wrapper": {"e": "ALGO_UPDATE", "a": {
            "algoId": 7, "clientAlgoId": "bv2ps-1-1", "algoStatus": "TRIGGERED",
            "actualOrderId": 42}},
        "'o' wrapper": {"e": "ALGO_UPDATE", "o": {
            "algoId": 7, "clientAlgoId": "bv2ps-1-1", "algoStatus": "TRIGGERED",
            "actualOrderId": 42}},
        "flat / no wrapper": {
            "e": "ALGO_UPDATE", "algoId": 7, "clientAlgoId": "bv2ps-1-1",
            "algoStatus": "TRIGGERED", "actualOrderId": 42},
        "short wire names": {"e": "ALGO_UPDATE", "ao": {
            "ai": 7, "cai": "bv2ps-1-1", "as": "TRIGGERED", "aoi": 42}},
        "nested one level deeper": {"e": "ALGO_UPDATE", "data": {"algo": {
            "algoId": 7, "clientAlgoId": "bv2ps-1-1", "algoStatus": "TRIGGERED",
            "actualOrderId": 42}}},
        "unknown wrapper key": {"e": "ALGO_UPDATE", "somethingNew": {
            "algoId": 7, "clientAlgoId": "bv2ps-1-1", "algoStatus": "TRIGGERED",
            "actualOrderId": 42}},
    }

    for label, event in shapes.items():
        _, algo_id, client_algo_id, status, actual = trading.MartingaleManager._extract_algo_fields(event)
        print(f"TEST 2: {label:28s} -> algoId={algo_id} status={status} actualOrderId={actual}")
        assert str(algo_id) == "7", f"{label}: algoId not recovered"
        assert client_algo_id == "bv2ps-1-1", f"{label}: clientAlgoId not recovered"
        assert status == "TRIGGERED", f"{label}: status not recovered"
        assert str(actual) == "42", f"{label}: actualOrderId not recovered"

    # A genuinely empty event must stay empty rather than inventing values.
    _, algo_id, client_algo_id, status, actual = trading.MartingaleManager._extract_algo_fields({})
    assert (algo_id, client_algo_id, status, actual) == (None, "", "", None)
    trading.MartingaleManager._extract_algo_fields(None)  # must not raise
    print("TEST 2: PASS - every envelope shape parses; an empty one stays empty\n")


# ----------------------------------------------------------------------------
# TEST 3: a parsed TRIGGERED event wires the child order and replays the fill
# that arrived before we knew its id - the ordering seen live.
# ----------------------------------------------------------------------------
async def test_triggered_event_wires_child_and_replays_buffered_fill():
    print("=== test_triggered_event_wires_child_and_replays_buffered_fill ===")

    class RestStub:
        def is_cooldown_active(self):
            return False

        async def get_position_risk(self, symbol):
            return []                      # exchange confirms flat after the stop filled

        async def get_open_algo_orders(self, symbol):
            return []

        async def cancel_algo_order(self, algo_id=None):
            return {}

    m = open_short_at_76_90(make_manager(RestStub()))
    logged = []
    m.trade_logger.log_trade = lambda rec: logged.append(rec)
    m.perf_stats.export = lambda: None
    child_id = 233511993724

    # The child MARKET order filled before any ALGO_UPDATE told us its id -
    # exactly the live ordering. It lands in the unmatched-fill buffer.
    m._unmatched_fills[child_id] = (
        {"e": "ORDER_TRADE_UPDATE", "o": {
            "i": child_id, "X": "FILLED", "S": "BUY", "q": "1.04", "ap": "76.99",
            "rp": "-0.0936", "n": "0.04", "N": "USDT", "t": 3460311790,
        }}, time.time(),
    )
    assert child_id not in m._order_index

    with Capture() as cap:
        await m.handle_algo_update({"e": "ALGO_UPDATE", "ao": {
            "algoId": 3000002144104022,
            "clientAlgoId": "bv2ps-7075801113-1",
            "algoStatus": "TRIGGERED",
            "actualOrderId": child_id,
        }})

    print(f"TEST 3: diagnostic_logged={'[algo-update]' in cap.text}")
    print(f"TEST 3: buffer_drained={child_id not in m._unmatched_fills}")
    print(f"TEST 3: routed_to_close={'exit_reason=protective_stop' in cap.text}")
    print(f"TEST 3: trades_logged={len(logged)} status={m.position.status} count={m.trade_count}")

    assert "[algo-update]" in cap.text, "the per-event diagnostic must always print for our own stop"
    assert "registered for close bookkeeping" in cap.text, "the child order must be wired up"
    assert child_id not in m._unmatched_fills, (
        "the buffered fill must be CLAIMED and replayed once the child id is known - "
        "leaving it to expire is what lost the live trade"
    )
    assert "path=replayed_unmatched_fill" in cap.text, "the buffered fill must be replayed"
    assert "exit_reason=protective_stop" in cap.text, (
        "the replayed fill must route through _on_close_filled() as a protective-stop close"
    )
    # And the trade is actually finalized: logged, counted, position flat.
    assert len(logged) == 1, f"the closed trade must be logged exactly once (got {len(logged)})"
    assert logged[0].get("side") == "SHORT"
    assert m.trade_count == 1, "trade_count must reflect the recovered close"
    assert m.position.status == "FLAT", "the position must finalize to FLAT"
    print("TEST 3: PASS - TRIGGERED wires the child, replays the fill, and logs the trade\n")


# ============================================================================
# FIX B - RECONCILIATION MUST NOT SKIP THE ENTRY LEG
# ============================================================================

# ----------------------------------------------------------------------------
# TEST 4: while a position is open, the userTrades window is rewound to
# include its entry fill, so entry+close are always reconstructed together.
# ----------------------------------------------------------------------------
async def test_reconcile_window_includes_the_open_entry_leg():
    print("=== test_reconcile_window_includes_the_open_entry_leg ===")

    requested = {}

    class RestStub:
        def is_cooldown_active(self):
            return False

        async def get_user_trades(self, symbol, from_id=None, start_time_ms=None,
                                  limit=1000, order_id=None):
            requested["from_id"] = from_id
            return []

    m = open_short_at_76_90(make_manager(RestStub()), first_trade_id=3460311000)
    m._trade_sync_cursor = 3460070820
    m._last_live_trade_id = 3460311000  # the ENTRY fill, processed live

    with Capture() as cap:
        await m.reconcile_trade_history_from_exchange(context="test")

    print(f"TEST 4: requested from_id={requested['from_id']} (entry fill id=3460311000)")
    assert requested["from_id"] == 3460311000, (
        "the window must START AT the open position's entry fill, not after it - "
        "starting after it is what produced an orphan close with no matching entry"
    )
    assert "rewinding userTrades window" in cap.text
    print("TEST 4: PASS - the open position's entry leg is always in the window\n")


# ----------------------------------------------------------------------------
# TEST 5: when flat, the rewind does not apply - normal cursor behavior.
# ----------------------------------------------------------------------------
async def test_reconcile_window_unchanged_when_flat():
    print("=== test_reconcile_window_unchanged_when_flat ===")

    requested = {}

    class RestStub:
        def is_cooldown_active(self):
            return False

        async def get_user_trades(self, symbol, from_id=None, start_time_ms=None,
                                  limit=1000, order_id=None):
            requested["from_id"] = from_id
            return []

    m = make_manager(RestStub())          # FLAT
    m._trade_sync_cursor = 3460070820
    m._last_live_trade_id = 3460070900

    await m.reconcile_trade_history_from_exchange(context="test")

    print(f"TEST 5: requested from_id={requested['from_id']}")
    assert requested["from_id"] == 3460070901, (
        "with nothing open there is no entry leg to keep together - the cursor "
        "behaves exactly as before this fix"
    )
    print("TEST 5: PASS - flat reconciliation is unchanged\n")


# ============================================================================
# FIX D - ORPHAN CLOSE SELF-HEAL (UNWEDGES THE STUCK CURSOR)
# ============================================================================

# ----------------------------------------------------------------------------
# TEST 6: the live wedge itself. Cursor at 3460311789, the only fill after it
# is the close 3460311790, bot is FLAT. Before the fix this was skipped
# forever. Now it must backfill by time, rebuild the trade, and log it.
# ----------------------------------------------------------------------------
async def test_orphan_close_is_recovered_and_cursor_unwedges():
    print("=== test_orphan_close_is_recovered_and_cursor_unwedges ===")

    entry_ms = 1787077001000   # 2026-08-18 17:56:41 UTC
    close_ms = 1787077668000   # 2026-08-18 18:07:48 UTC

    entry_fill = {
        "id": 3460311000, "orderId": 233510421723, "symbol": "SOLUSDT", "side": "SELL",
        "qty": "1.04", "price": "76.90", "realizedPnl": "0", "commission": "0.016",
        "commissionAsset": "USDT", "time": entry_ms,
    }
    close_fill = {
        "id": 3460311790, "orderId": 233511993724, "symbol": "SOLUSDT", "side": "BUY",
        "qty": "1.04", "price": "76.99", "realizedPnl": "-0.0936", "commission": "0.032",
        "commissionAsset": "USDT", "time": close_ms,
    }

    calls = []

    class RestStub:
        def is_cooldown_active(self):
            return False

        async def get_user_trades(self, symbol, from_id=None, start_time_ms=None,
                                  limit=1000, order_id=None):
            calls.append({"from_id": from_id, "start_time_ms": start_time_ms})
            if from_id is not None:
                # The wedged state: only the orphan close is after the cursor.
                return [close_fill] if from_id <= 3460311790 else []
            if start_time_ms is not None:
                # Time-based backfill finally reveals the entry leg.
                return [f for f in (entry_fill, close_fill) if f["time"] >= start_time_ms]
            return []

    m = make_manager(RestStub())          # FLAT, exactly like the live bot
    m._trade_sync_cursor = 3460311789     # the real wedged value from brain-state
    m._last_live_trade_id = 0

    logged = []
    m.trade_logger.log_trade = lambda rec: logged.append(rec)
    m.trade_logger.logged_binance_order_ids = lambda: set()
    m.perf_stats.export = lambda: None
    m.sync_trade_log_to_github = lambda *a, **k: asyncio.sleep(0)
    persisted = []
    m._persist_trade_sync_cursor = lambda tid, reason="": persisted.append(tid) or asyncio.sleep(0)

    with Capture() as cap:
        await m.reconcile_trade_history_from_exchange(context="test")

    print(f"TEST 6: rest_calls={calls}")
    print(f"TEST 6: trades_logged={len(logged)} cursor_persisted={persisted}")
    assert "ORPHAN CLOSE DETECTED" in cap.text, "the wedge must be detected and named"
    assert any(c["start_time_ms"] is not None for c in calls), (
        "recovery must re-fetch by time to reach back past the window's start"
    )
    assert len(logged) == 1, (
        f"the completed SHORT lifecycle must finally be logged (got {len(logged)})"
    )
    rec = logged[0]
    print(f"TEST 6: logged side={rec.get('side')} net_pnl={rec.get('net_pnl_usdt')}")
    assert rec.get("side") == "SHORT"
    assert abs(float(rec["net_pnl_usdt"]) - (-0.0936 - 0.048)) < 1e-6, (
        "net PnL must be Binance realizedPnl minus real commissions across both legs"
    )
    assert persisted and persisted[-1] >= 3460311790, (
        f"the cursor MUST advance past the orphan (was wedged at 3460311789, "
        f"got {persisted[-1] if persisted else None}) - otherwise every future "
        f"trade stays jammed behind it"
    )
    print("TEST 6: PASS - the orphan trade is recovered and the cursor unwedges\n")


# ----------------------------------------------------------------------------
# TEST 7: the backfill is attempted at most once per orphan, so a genuinely
# unmatchable fill cannot turn into a Binance history re-fetch on every poll.
# ----------------------------------------------------------------------------
async def test_orphan_backfill_is_attempted_once():
    print("=== test_orphan_backfill_is_attempted_once ===")

    close_fill = {
        "id": 999000, "orderId": 111, "symbol": "SOLUSDT", "side": "BUY",
        "qty": "1.0", "price": "77.00", "realizedPnl": "0", "commission": "0.01",
        "commissionAsset": "USDT", "time": 1787077668000,
    }
    backfills = []

    class RestStub:
        def is_cooldown_active(self):
            return False

        async def get_user_trades(self, symbol, from_id=None, start_time_ms=None,
                                  limit=1000, order_id=None):
            if start_time_ms is not None:
                backfills.append(start_time_ms)
                return [close_fill]        # entry leg genuinely unavailable
            return [close_fill]

    m = make_manager(RestStub())
    m._trade_sync_cursor = 998999
    m.trade_logger.logged_binance_order_ids = lambda: set()
    m._persist_trade_sync_cursor = lambda tid, reason="": asyncio.sleep(0)

    with Capture():
        for _ in range(4):
            await m.reconcile_trade_history_from_exchange(context="test")

    print(f"TEST 7: backfill_attempts={len(backfills)} over 4 reconciliation passes")
    assert len(backfills) == 1, (
        "an unmatchable orphan must be retried once, then left alone - not "
        "re-fetched on every poll (that would be a new rate-limit problem)"
    )
    print("TEST 7: PASS - orphan backfill is bounded to one attempt\n")


# ============================================================================
# FIX C - FINALIZE BEFORE DISCARDING THE POSITION
# ============================================================================

# ----------------------------------------------------------------------------
# TEST 8: the full live sequence end to end. Exchange goes flat under an OPEN
# local SHORT; the trade must be reconciled BEFORE the state is reset.
# ----------------------------------------------------------------------------
async def test_reset_to_flat_reconciles_before_discarding_state():
    print("=== test_reset_to_flat_reconciles_before_discarding_state ===")

    order_of_events = []

    class RestStub:
        def is_cooldown_active(self):
            return False

        async def get_position_risk(self, symbol):
            return []                      # exchange reports flat

        async def get_user_trades(self, symbol, from_id=None, start_time_ms=None,
                                  limit=1000, order_id=None):
            return []

        async def get_open_algo_orders(self, symbol):
            return []

    m = open_short_at_76_90(make_manager(RestStub()))

    async def spy_reconcile(context="reconcile"):
        # Capture what the position still looked like at reconcile time - the
        # whole point of fix C is that this runs while the state is intact.
        order_of_events.append(("reconcile", m.position.status, m.position.side,
                                m.position.total_qty))

    m.reconcile_trade_history_from_exchange = spy_reconcile

    with Capture() as cap:
        await trading.initialize_sync(m.client, m, context="periodic poll")

    order_of_events.append(("after_sync", m.position.status, m.position.side,
                            m.position.total_qty))

    for label, status, side, qty in order_of_events:
        print(f"TEST 8: {label:12s} status={status} side={side} qty={qty}")

    assert order_of_events[0][0] == "reconcile", "reconciliation must run FIRST"
    assert order_of_events[0][1] == "OPEN" and order_of_events[0][2] == "SHORT", (
        "reconciliation must see the position still intact - resetting first is "
        "what destroyed the only record the close could be attributed to"
    )
    assert abs(order_of_events[0][3] - 1.04) < 1e-9
    assert m.position.status == "FLAT", "and only THEN is the state reset"
    assert m._open_position_first_trade_id is None, (
        "the finished position's entry-leg floor must not leak into the next trade"
    )
    assert "Reconciling against Binance trade history BEFORE resetting" in cap.text
    print("TEST 8: PASS - the trade is reconciled before local state is discarded\n")


# ----------------------------------------------------------------------------
# TEST 9: a failure inside that pre-reset reconciliation must not wedge the
# bot in a phantom OPEN position - the reset still happens.
# ----------------------------------------------------------------------------
async def test_reset_still_happens_if_reconcile_fails():
    print("=== test_reset_still_happens_if_reconcile_fails ===")

    class RestStub:
        def is_cooldown_active(self):
            return False

        async def get_position_risk(self, symbol):
            return []

        async def get_open_algo_orders(self, symbol):
            return []

    m = open_short_at_76_90(make_manager(RestStub()))

    async def boom(context="reconcile"):
        raise RuntimeError("Binance 502")

    m.reconcile_trade_history_from_exchange = boom

    with Capture() as cap:
        await trading.initialize_sync(m.client, m, context="periodic poll")

    print(f"TEST 9: status_after={m.position.status} sync_ready={m.position_sync_ready}")
    assert m.position.status == "FLAT", (
        "bookkeeping must never be able to strand the bot in a position the "
        "exchange has already closed"
    )
    assert "pre-reset reconciliation failed" in cap.text
    print("TEST 9: PASS - a reconciliation failure never blocks the reset\n")


async def main():
    await test_unparsed_envelope_is_never_silently_dropped()
    test_extractor_handles_every_envelope_shape()
    await test_triggered_event_wires_child_and_replays_buffered_fill()
    await test_reconcile_window_includes_the_open_entry_leg()
    await test_reconcile_window_unchanged_when_flat()
    await test_orphan_close_is_recovered_and_cursor_unwedges()
    await test_orphan_backfill_is_attempted_once()
    await test_reset_to_flat_reconciles_before_discarding_state()
    await test_reset_still_happens_if_reconcile_fails()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
