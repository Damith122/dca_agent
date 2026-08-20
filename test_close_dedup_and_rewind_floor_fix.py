"""Offline regression tests for the 2026-08-19 21:06 UTC duplicate-trade
incident (F1, F2, F3).

WHAT HAPPENED
--------------------------------------------------------------------------
  21:06:02  [risk] position risk poll failed:            <- EMPTY message (F3)
  21:06:26  [entry-accepted] LONG score=0.8620 threshold=0.7500 STRONG_TREND
  21:06:26  [post-only] rejected -5022 x2 -> MARKET fallback (taker entry)
  21:06:27  ENTRY FILLED LONG 0.93 @ 85.89
  21:06:33  [reconcile] "rewinding userTrades window from id>3462286436
            back to id>=1"                                <- FULL SCAN (F2)
  21:06:35  [trade-loss-budget] TRIGGERED est -0.1542
  21:06:36  close order 233740047626 FILLED (websocket)
  21:06:37  close-verify FLAT -> finalizing -> TRADE LOGGED        (row 1, correct)
  21:06:39  [dca-state] restored ... pending_close_order_id=233740047626
  21:06:39  *** RESYNCING TO MATCH EXCHANGE *** rebuilds the closed position
  21:07:41  [rest-recovery] order 233740047626 status=FILLED
  21:07:42  -> _on_close_filled() AGAIN -> TRADE LOGGED AGAIN      (row 2, phantom)

Both CSV rows carried the SAME exit_order_id (233740047626) and identical
entry/exit/qty/notional/gross/fees/net. It was one trade written twice:
double-counted session and daily PnL, an inflated trade count, and a second
(phantom) Brain reinforcement.

THE THREE FIXES
--------------------------------------------------------------------------
  F1  _on_close_filled() is now idempotent per close order id. The id is
      recorded BEFORE any bookkeeping and persisted in the DCA snapshot, and
      pending_order_id/pending_role are cleared in the same synchronous step
      so a snapshot-driven rebuild cannot restore a resolved close.
  F2  _open_position_reconcile_floor() returned max(1, floor), so a stored 0
      collapsed to 1 and rewound the window to id>=1 - a full account-history
      scan. A floor is now required to be > 1 AND within
      MAX_RECONCILE_REWIND_IDS of the cursor, else no rewind happens.
      (The cursor's own monotonic guard already existed in
      _persist_trade_sync_cursor and is unchanged.)
  F3  asyncio.TimeoutError / aiohttp.ClientError have an EMPTY str(), so
      "failed: {e}" logged nothing. _exc_text() always prefixes the class.
      Those blanks were the REST timeouts that made positionRisk stale -
      the direct trigger for F1's duplicate.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_close_dedup_and_rewind_floor_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SYMBOL", "SOLUSDT")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_dedup_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_dedup_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_dedup_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_dedup_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_dedup_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_dedup_dca_state.json")

import asyncio
import io
import sys
import time

import aiohttp

import config
import dca2 as bot
import trading

CLOSE_ORDER_ID = 233740047626        # the real close order from the incident


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


def open_the_2106_long(m):
    """The 21:06 LONG exactly as it stood when the close filled."""
    p = m.position
    p.status = "CLOSING"
    p.side = "LONG"
    p.total_qty = 0.93
    p.original_qty = 0.93
    p.avg_entry_price = 85.89
    p.entries = [(85.89, 0.93)]
    p.opened_at = time.time() - 9.5
    p.pending_order_id = CLOSE_ORDER_ID
    p.pending_role = "close"
    p.entry_regime = "STRONG_TREND"
    p.entry_confidence = 0.78375
    m.current_price = 85.78
    m.best_bid_price, m.best_ask_price = 85.78, 85.79
    m._position_fees_accum = 0.0798
    m._position_fees_reliable = True
    m._pending_exit_reason = "max_trade_net_loss"
    m.last_regime = trading.RegimeReading(
        regime=trading.REGIME_STRONG_TREND, atr_pct=0.003914, atr_ratio=1.0,
    )
    return m


# ============================================================================
# F1 - CLOSE-ORDER IDEMPOTENCY
# ============================================================================

async def test_duplicate_close_delivery_logs_one_trade():
    print("=== test_duplicate_close_delivery_logs_one_trade ===")

    m = open_the_2106_long(make_manager())
    logged = []
    m.trade_logger.log_trade = lambda rec: logged.append(rec)
    m.perf_stats.export = lambda: None
    reinforced = []
    m.reinforce_entry_decision = lambda *a, **k: reinforced.append(1)

    async def flat(*a, **k):
        return None, 0.0

    m._fetch_exchange_position = flat

    # Delivery 1 - the live websocket fill at 21:06:36.
    with Capture() as c1:
        await m._on_close_filled(85.78, -0.1023, order_id=CLOSE_ORDER_ID)
    # Delivery 2 - the REST-recovery fallback at 21:07:41, same order id.
    with Capture() as c2:
        await m._on_close_filled(85.78, -0.1023, order_id=CLOSE_ORDER_ID)

    print(f"F1: trades logged={len(logged)} trade_count={m.trade_count}")
    print(f"F1: realized_pnl_total={m.realized_pnl_total:+.4f}")
    print(f"F1: duplicate suppressed={'[close-dedup]' in c2.text}")

    assert len(logged) == 1, (
        f"one close order must produce exactly ONE trade record (got {len(logged)}) - "
        f"live, the second delivery wrote a duplicate row with zeroed entry metadata"
    )
    assert m.trade_count == 1, "trade_count must not be inflated by a re-delivery"
    assert "[close-dedup]" in c2.text, "the suppression must be visible in the log"
    assert "[close-dedup]" not in c1.text, "the FIRST delivery must be processed normally"
    # The live incident double-counted -0.1821 into session PnL.
    assert abs(m.realized_pnl_total - (-0.18212654)) < 0.01, (
        f"PnL must be counted once, not twice (got {m.realized_pnl_total:+.4f})"
    )
    print("F1: PASS - one close order, one trade, one PnL entry\n")


async def test_pending_order_cleared_atomically_with_finalize():
    print("=== test_pending_order_cleared_atomically_with_finalize ===")

    m = open_the_2106_long(make_manager())
    m.trade_logger.log_trade = lambda rec: None
    m.perf_stats.export = lambda: None

    async def flat(*a, **k):
        return None, 0.0

    m._fetch_exchange_position = flat
    assert m.position.pending_order_id == CLOSE_ORDER_ID

    with Capture():
        await m._on_close_filled(85.78, -0.1023, order_id=CLOSE_ORDER_ID)

    snap = m._dca_state_snapshot()
    print(f"F1: snapshot pending_order_id={snap.get('pending_order_id')}")
    print(f"F1: snapshot finalized_close_order_ids={snap.get('finalized_close_order_ids')}")

    assert snap.get("pending_order_id") is None, (
        "a snapshot written after finalize must NOT still advertise the resolved "
        "close order - that field is what initialize_sync restored to resurrect "
        "the closed 21:06 position"
    )
    assert CLOSE_ORDER_ID in (snap.get("finalized_close_order_ids") or []), (
        "the finalized id must be persisted so a RESTART cannot re-finalize it"
    )
    print("F1: PASS - pending cleared and the finalized id persisted\n")


async def test_finalized_memory_survives_restart():
    print("=== test_finalized_memory_survives_restart ===")

    # A fresh process (new manager) restoring a snapshot that already records
    # this close as finalized must refuse to finalize it again.
    m2 = open_the_2106_long(make_manager())
    m2._mark_close_order_finalized(CLOSE_ORDER_ID)   # as restored from snapshot
    logged = []
    m2.trade_logger.log_trade = lambda rec: logged.append(rec)
    m2.perf_stats.export = lambda: None

    async def flat(*a, **k):
        return None, 0.0

    m2._fetch_exchange_position = flat

    with Capture() as cap:
        await m2._on_close_filled(85.78, -0.1023, order_id=CLOSE_ORDER_ID)

    print(f"F1: after restart -> trades logged={len(logged)}")
    assert not logged, "a restart must not re-finalize an already-logged close"
    assert "[close-dedup]" in cap.text
    # And an unrelated close is unaffected.
    assert m2._close_order_already_finalized(999999) is False
    print("F1: PASS - the guard survives a restart and is order-specific\n")


# ============================================================================
# F2 - REWIND FLOOR PLAUSIBILITY
# ============================================================================

def test_rewind_floor_rejects_implausible_values():
    print("=== test_rewind_floor_rejects_implausible_values ===")

    def floor_for(stored, cursor=3462286436):
        m = make_manager()
        p = m.position
        p.status, p.side, p.total_qty = "OPEN", "LONG", 0.93
        p.avg_entry_price = 85.89
        m._trade_sync_cursor = cursor
        m._open_position_first_trade_id = stored
        return m._open_position_reconcile_floor()

    # The live bug: a stored 0 became max(1, 0) = 1 -> full history scan.
    print(f"F2: stored=0    -> {floor_for(0)}")
    print(f"F2: stored=1    -> {floor_for(1)}")
    assert floor_for(0) is None, (
        "a stored 0 must NOT collapse to 1 - that is what produced "
        "'rewinding userTrades window from id>3462286436 back to id>=1'"
    )
    assert floor_for(1) is None

    # Absurdly stale but positive - still refused.
    with Capture() as cap:
        far = floor_for(12345)
    print(f"F2: stored=12345 (2.9e9 behind cursor) -> {far}")
    assert far is None, "a floor billions of ids behind the cursor must be refused"
    assert "implausible entry-leg floor" in cap.text

    # A genuine, recent entry leg IS honoured.
    good = floor_for(3462286400)
    print(f"F2: stored=3462286400 (36 ids behind cursor) -> {good}")
    assert good == 3462286400, "a real entry leg near the cursor must still rewind"

    # Flat -> never rewinds, regardless of stored value.
    m = make_manager()
    m._open_position_first_trade_id = 3462286400
    assert m._open_position_reconcile_floor() is None
    print("F2: PASS - only a plausible, recent entry leg triggers a rewind\n")


def test_cursor_never_moves_backward():
    print("=== test_cursor_never_moves_backward ===")

    # This guard PRE-DATES the F-series fixes; asserted here so a future edit
    # cannot remove it, since F2's failure mode would otherwise corrupt the
    # cursor from an ancient max_id_seen.
    src = open("trading.py", encoding="utf-8").read()
    idx = src.index("async def _persist_trade_sync_cursor")
    body = src[idx:idx + 700]
    print("F2: guard present =", "if trade_id <= self._trade_sync_cursor:" in body)
    assert "if trade_id <= self._trade_sync_cursor:" in body, (
        "the monotonic cursor guard must remain - it is the backstop that kept "
        "the id>=1 scan from also rewinding the persisted cursor"
    )
    print("F2: PASS - the cursor is monotonic\n")


# ============================================================================
# F3 - EXCEPTION TEXT
# ============================================================================

def test_empty_exceptions_still_identify_themselves():
    print("=== test_empty_exceptions_still_identify_themselves ===")

    cases = [
        (asyncio.TimeoutError(), "TimeoutError"),
        (aiohttp.ClientError(), "ClientError"),
        (aiohttp.ServerTimeoutError(), "ServerTimeoutError"),
        (aiohttp.ClientError("connection reset"), "ClientError: connection reset"),
    ]
    for exc, expected in cases:
        got = bot._exc_text(exc)
        print(f"F3: {type(exc).__name__:20} str(e)={str(exc)!r:20} -> {got!r}")
        assert got == expected, f"expected {expected!r}, got {got!r}"
        assert got.strip(), "the text must NEVER be empty - that is the whole defect"

    # And the call sites actually use it.
    src = open("dca2.py", encoding="utf-8").read()
    for site in (
        "[risk] position risk poll failed: {_exc_text(e)}",
        "[balance] refresh failed: {_exc_text(e)}",
        "[funding] premiumIndex poll failed (continuing without it): {_exc_text(e)}",
        "[funding] openInterest poll failed (continuing without it): {_exc_text(e)}",
    ):
        assert site in src, f"call site not routed through _exc_text: {site}"
    print("F3: PASS - a timeout can no longer log a blank line\n")


async def main():
    await test_duplicate_close_delivery_logs_one_trade()
    await test_pending_order_cleared_atomically_with_finalize()
    await test_finalized_memory_survives_restart()
    test_rewind_floor_rejects_implausible_values()
    test_cursor_never_moves_backward()
    test_empty_exceptions_still_identify_themselves()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
