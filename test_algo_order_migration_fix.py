"""
Regression tests for the 2026-08 Binance Algo-Service migration fix.

Root cause (confirmed from the live Railway log + trades CSV): Binance moved
USD-M CONDITIONAL order types onto a dedicated Algo Service. The protective
stop was still being placed as `type=STOP_MARKET` on POST /fapi/v1/order, so
every placement failed immediately with:

    HTTP 400 {"code": -4120, "msg": "Order type not supported for this
              endpoint. Please use the Algo Order API endpoints instead."}

Each of the three live trades then entered PROTECTION_PENDING, retried every
30s, and was closed by the 300s bounded fail-safe with
exit_reason=protection_unavailable - and all three were fed to Brain as
success=False, contaminating entry-quality learning with an API outage.

This file covers all 20 required checks. Mocks only - no network, no real
orders. Run directly: `python3 test_algo_order_migration_fix.py`
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
os.environ.setdefault("MAX_HOLD_TIME_ENABLED", "false")
os.environ.setdefault("MAX_DCA_STEPS", "1")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_algo_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_algo_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_algo_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_algo_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_algo_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_algo_dca_state.json")

import asyncio
import io
import json
import sys
import time

import numpy as np

import dca2 as bot
import trading
import exchange as exch

PREFIX = trading.PROTECTIVE_STOP_CLIENT_ID_PREFIX


class RecordingClient:
    """Records every REST call by (method, path) so tests can prove which
    endpoint each order type went to. Mirrors the real RestClient surface
    used by trading.py - nothing here touches the network."""

    def __init__(self, open_algo_orders=None, fail_algo_place=False,
                 fail_algo_query=False, fail_open_algo=False, fail_cancel=None,
                 legacy_4120=False):
        self.calls = []                 # (METHOD, path, params)
        self.placed_orders = []         # /fapi/v1/order bodies
        self.placed_algo_orders = []    # /fapi/v1/algoOrder bodies
        self.cancelled_algo_ids = []
        self.cancelled_order_ids = []
        self._open_algo = open_algo_orders if open_algo_orders is not None else []
        self._fail_algo_place = fail_algo_place
        self._fail_algo_query = fail_algo_query
        self._fail_open_algo = fail_open_algo
        self._fail_cancel = fail_cancel
        self._legacy_4120 = legacy_4120
        self._next_order_id = 5000
        self._next_algo_id = 9000
        self.position = None
        self.report_flat = False
        self.algo_query_result = None

    # --- plain order endpoint (MARKET entries / DCA / closes) --------------
    async def place_order(self, **kw):
        self.calls.append(("POST", "/fapi/v1/order", kw))
        if self._legacy_4120 and kw.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            raise trading.BinanceApiError(400, {
                "code": -4120,
                "msg": "Order type not supported for this endpoint. "
                       "Please use the Algo Order API endpoints instead.",
            })
        self.placed_orders.append(kw)
        oid = self._next_order_id
        self._next_order_id += 1
        return {"orderId": oid}

    async def cancel_order(self, symbol, order_id):
        self.calls.append(("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}))
        self.cancelled_order_ids.append(order_id)
        return {"orderId": order_id}

    async def get_open_orders(self, symbol):
        self.calls.append(("GET", "/fapi/v1/openOrders", {"symbol": symbol}))
        return []

    async def get_order(self, symbol, order_id):
        self.calls.append(("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}))
        return {"orderId": order_id, "status": "FILLED"}

    async def get_user_trades(self, symbol, limit=100, order_id=None, from_id=None,
                              start_time_ms=None):
        self.calls.append(("GET", "/fapi/v1/userTrades", {"symbol": symbol}))
        return getattr(self, "user_trades", [])

    # --- algo endpoints (conditional / protective stop) -------------------
    async def place_algo_order(self, **kw):
        self.calls.append(("POST", "/fapi/v1/algoOrder", kw))
        if self._fail_algo_place:
            raise trading.BinanceApiError(400, {"code": -1001, "msg": "simulated algo failure"})
        self.placed_algo_orders.append(kw)
        aid = self._next_algo_id
        self._next_algo_id += 1
        return {"algoId": aid, "clientAlgoId": kw.get("clientAlgoId"), "algoStatus": "NEW"}

    async def cancel_algo_order(self, algo_id=None, client_algo_id=None):
        self.calls.append(("DELETE", "/fapi/v1/algoOrder",
                           {"algoId": algo_id, "clientAlgoId": client_algo_id}))
        self.cancelled_algo_ids.append(algo_id if algo_id is not None else client_algo_id)
        if self._fail_cancel == "network":
            raise asyncio.TimeoutError("simulated cancel timeout")
        if self._fail_cancel == "unknown_order":
            raise trading.BinanceApiError(400, {"code": -2011, "msg": "Unknown order sent."})
        return {"algoId": algo_id}

    async def get_algo_order(self, algo_id=None, client_algo_id=None):
        self.calls.append(("GET", "/fapi/v1/algoOrder",
                           {"algoId": algo_id, "clientAlgoId": client_algo_id}))
        if self._fail_algo_query:
            raise trading.BinanceApiError(400, {"code": -1021, "msg": "simulated query failure"})
        if self.algo_query_result is not None:
            return self.algo_query_result
        for o in self._open_algo:
            if (algo_id is not None and o.get("algoId") == algo_id) or (
                    client_algo_id is not None and o.get("clientAlgoId") == client_algo_id):
                return o
        return {"algoId": algo_id, "algoStatus": "NEW", "actualOrderId": ""}

    async def get_open_algo_orders(self, symbol=None):
        self.calls.append(("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}))
        if self._fail_open_algo:
            raise trading.BinanceApiError(400, {"code": -1021, "msg": "simulated enumeration failure"})
        return self._open_algo

    # --- misc -------------------------------------------------------------
    async def get_position_risk(self, symbol):
        p = self.position
        if self.report_flat or p is None or p.status not in ("OPEN", "CLOSING", "DCA_PENDING") \
                or not p.total_qty:
            return [{"symbol": symbol, "positionAmt": "0", "entryPrice": "0"}]
        amt = p.total_qty if p.side == "LONG" else -p.total_qty
        return [{"symbol": symbol, "positionAmt": str(amt), "entryPrice": str(p.avg_entry_price)}]

    def paths(self, method=None):
        return [c[1] for c in self.calls if method is None or c[0] == method]


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


async def make_manager(side="LONG", avg_entry=100.0, qty=1.0, client=None, clear_state=True):
    if clear_state and os.path.exists(os.environ["DCA_STATE_PATH"]):
        os.remove(os.environ["DCA_STATE_PATH"])
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    client = client if client is not None else RecordingClient()
    m = bot.MartingaleManager(client=client, symbol="SOLUSDT", filters=filters, leverage=20)
    m.position_sync_ready = True
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
    m.current_price = m.best_bid_price = m.best_ask_price = avg_entry
    m.prev_price = avg_entry
    m.last_regime = trading.RegimeReading(regime=trading.REGIME_SIDEWAYS, atr_pct=0.0005, atr_ratio=1.0)
    m.last_confidence = trading.ConfidenceReading(
        confidence_score=0.5, risk_score=0.2, trend_direction=None,
        trend_confidence=0.0, success_probability=0.5, tp_hit_probability=0.5,
    )
    client.position = p
    return m, client


def algo_event(algo_id, status, client_algo_id=None, actual_order_id="", envelope="ao"):
    body = {
        "algoId": algo_id,
        "clientAlgoId": client_algo_id if client_algo_id is not None else f"{PREFIX}-t-1",
        "algoStatus": status,
        "actualOrderId": actual_order_id,
        "symbol": "SOLUSDT",
    }
    return {"e": "ALGO_UPDATE", envelope: body}


def fill_event(order_id, ap=99.94, rp=-0.16, n=0.05, z=1.0, trade_id=4242):
    return {"o": {"i": order_id, "X": "FILLED", "rp": str(rp), "n": str(n), "N": "USDT",
                  "ap": str(ap), "z": str(z), "t": trade_id, "T": int(time.time() * 1000)}}


# ============================================================================
# 1-4: placement goes to the Algo endpoint with the exact documented fields
# ============================================================================
async def test_01_protective_stop_uses_algo_endpoint():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    assert ("POST", "/fapi/v1/algoOrder") in [(c[0], c[1]) for c in client.calls], client.calls
    assert len(client.placed_algo_orders) == 1
    print("protective stop placed via POST /fapi/v1/algoOrder")
    print("PASS\n")


async def test_02_exact_algo_parameters():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    o = client.placed_algo_orders[0]
    assert o["algoType"] == "CONDITIONAL", o
    assert o["type"] == "STOP_MARKET", o
    assert "triggerPrice" in o and float(o["triggerPrice"]) > 0, o
    assert o["clientAlgoId"].startswith(f"{PREFIX}-"), o
    assert o["closePosition"] == "true", o
    assert o["workingType"] == trading.PROTECTIVE_STOP_WORKING_TYPE, o
    assert o["symbol"] == "SOLUSDT" and o["side"] == "SELL", o
    # forbidden combinations / legacy names
    assert "quantity" not in o, "must not send quantity with closePosition=true"
    assert "reduceOnly" not in o, "must not send reduceOnly with closePosition=true"
    assert "stopPrice" not in o, "Algo API uses triggerPrice"
    assert "newClientOrderId" not in o, "Algo API uses clientAlgoId"
    print(f"exact params: algoType={o['algoType']} type={o['type']} "
          f"triggerPrice={o['triggerPrice']} clientAlgoId={o['clientAlgoId']} "
          f"closePosition={o['closePosition']} workingType={o['workingType']}")
    print("PASS\n")


async def test_03_no_protective_stop_reaches_plain_order_endpoint():
    """The exact live regression: a STOP_MARKET on /fapi/v1/order returns
    -4120. The client below raises that error if it ever happens."""
    client = RecordingClient(legacy_4120=True)
    m, client = await make_manager(client=client)
    with Capture() as cap:
        await m._place_or_replace_protective_stop(reason="INITIAL filled")
    assert "-4120" not in cap.text and "4120" not in cap.text, cap.text
    conditional_on_plain = [
        c for c in client.calls
        if c[1] == "/fapi/v1/order" and c[2].get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
    ]
    assert conditional_on_plain == [], conditional_on_plain
    assert m.position.protection_pending is False
    print("no conditional order reached /fapi/v1/order - the -4120 path is unreachable")
    print("PASS\n")


async def test_04_success_stores_ids_and_clears_protection_pending():
    m, client = await make_manager()
    m._mark_protection_pending("seeded")
    assert m.position.protection_pending is True
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    assert m.position.protective_stop_algo_id == 9000
    assert m.position.protective_stop_client_algo_id.startswith(f"{PREFIX}-")
    assert m.position.protection_pending is False
    assert m.position.protection_pending_since is None
    print(f"stored algoId={m.position.protective_stop_algo_id} "
          f"clientAlgoId={m.position.protective_stop_client_algo_id}; PROTECTION_PENDING cleared")
    print("PASS\n")


# ============================================================================
# 5: query / cancel / open-orders endpoints
# ============================================================================
async def test_05_algo_query_cancel_and_open_endpoints():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    await m._resolve_protective_algo_via_rest(context="test")
    await m._cancel_protective_stop(reason="test")
    await trading.reconcile_protective_stop_on_startup(client, m)
    paths = set((c[0], c[1]) for c in client.calls)
    for expected in (("POST", "/fapi/v1/algoOrder"), ("GET", "/fapi/v1/algoOrder"),
                     ("DELETE", "/fapi/v1/algoOrder"), ("GET", "/fapi/v1/openAlgoOrders")):
        assert expected in paths, f"{expected} not called; got {sorted(paths)}"
    # cancel must be keyed by algoId and must NOT send symbol
    cancel_params = [c[2] for c in client.calls if c[1] == "/fapi/v1/algoOrder" and c[0] == "DELETE"][0]
    assert cancel_params.get("algoId") is not None
    assert "symbol" not in cancel_params
    print("all four algo endpoints used with correct methods; DELETE keyed by algoId")
    print("PASS\n")


# ============================================================================
# 6: ALGO_UPDATE in the live private subscription
# ============================================================================
async def test_06_algo_update_in_live_subscription():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "websocket.py")).read()
    assert "events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE" in src, (
        "ALGO_UPDATE must be in the Live /private subscription"
    )
    assert 'etype == "ALGO_UPDATE"' in src, "ALGO_UPDATE must be routed"
    assert "handle_algo_update" in src, "ALGO_UPDATE must reach the manager handler"
    # credential hygiene: the receipt marker must not print the listenKey
    assert "listen_key}" not in src.split("ALGO_UPDATE received")[1][:400]
    print("Live subscription = ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE, routed to "
          "handle_algo_update() with no credential in the log line")
    print("PASS\n")


# ============================================================================
# 7: every algo status handled
# ============================================================================
async def test_07_algo_status_handling():
    # NEW -> tracked, protection cleared
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
    m._mark_protection_pending("seeded")
    with Capture():
        await m.handle_algo_update(algo_event(aid, "NEW", cid))
    assert m.position.protection_pending is False
    assert m.position.protective_stop_algo_id == aid
    print("  NEW      -> tracked, PROTECTION_PENDING cleared")

    # TRIGGERING / TRIGGERED -> child registered
    for status in ("TRIGGERING", "TRIGGERED"):
        m, client = await make_manager()
        await m._place_or_replace_protective_stop(reason="INITIAL filled")
        aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
        with Capture():
            await m.handle_algo_update(algo_event(aid, status, cid, actual_order_id=7777))
        assert m.position.protective_stop_actual_order_id == 7777, status
        assert m._order_index.get(7777) == "protective_stop", status
        print(f"  {status:<9}-> actualOrderId 7777 registered for close bookkeeping")

    # CANCELED / EXPIRED / REJECTED -> cleared + re-arm pending
    for status in ("CANCELED", "EXPIRED", "REJECTED"):
        m, client = await make_manager()
        await m._place_or_replace_protective_stop(reason="INITIAL filled")
        aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
        with Capture() as cap:
            await m.handle_algo_update(algo_event(aid, status, cid))
        assert m.position.protective_stop_algo_id is None, status
        assert m.position.protection_pending is True, status
        assert "HIGH SEVERITY" in cap.text, status
        print(f"  {status:<9}-> tracking cleared, PROTECTION_PENDING re-entered for re-arm")
    print("PASS\n")


# ============================================================================
# 8: FINISHED must not finalize without proof
# ============================================================================
async def test_08_finished_does_not_finalize_without_proof():
    # FINISHED with no actualOrderId, POSITION STILL OPEN on the exchange
    # == a genuine cancel, NOT a fill. (An absent actualOrderId on its own is
    # NOT proof of a cancel - see the second case below, which is the live
    # 2026-08-31 incident.)
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
    client.algo_query_result = {"algoId": aid, "algoStatus": "FINISHED", "actualOrderId": ""}
    trades_before = m.trade_count
    with Capture() as cap:
        await m.handle_algo_update(algo_event(aid, "FINISHED", cid))
    assert m.trade_count == trades_before, "FINISHED alone must never finalize a trade"
    assert m.position.status == "OPEN", "position must not be closed on an ambiguous FINISHED"
    assert "genuine CANCEL, not a fill" in cap.text, cap.text
    assert "still open on the exchange" in cap.text, (
        "the cancel verdict must be justified by the exchange position, not assumed"
    )
    assert ("GET", "/fapi/v1/algoOrder") in [(c[0], c[1]) for c in client.calls], (
        "FINISHED must be verified against the algo record via REST"
    )
    print("FINISHED with no actualOrderId -> verified by REST, treated as CANCELED, no trade "
          "finalized")

    # FINISHED with no actualOrderId but the POSITION IS GONE -> the stop did
    # trigger and Binance simply never reported the child id. This is the
    # 2026-08-31 SUIUSDT incident: treating it as a cancel dropped a real
    # closed trade from trades_log/session_total and denied the Brain a
    # label, leaving the reported total +0.0190 USDT optimistic.
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
    client.algo_query_result = {"algoId": aid, "algoStatus": "FINISHED", "actualOrderId": ""}
    client.report_flat = True                      # exchange says the position is gone
    closing_side_is_buyer = (m.position.side == "SHORT")
    client.user_trades = [{
        "id": 999001, "qty": str(m.position.total_qty),
        "price": str(m.position.protective_stop_price or m.position.avg_entry_price),
        "realizedPnl": "-0.00902", "commission": "0.00499983",
        "commissionAsset": "USDT", "buyer": closing_side_is_buyer,
    }]
    trades_before = m.trade_count
    await m.handle_algo_update(algo_event(aid, "FINISHED", cid))
    assert m.trade_count == trades_before + 1, (
        "a triggered stop whose child id was never reported MUST still be booked"
    )
    assert m.position.status == "FLAT", "the position must be finalized, not left open"
    print("FINISHED with no actualOrderId + exchange FLAT -> fill recovered from userTrades "
          "and booked (2026-08-31 regression)")

    # FINISHED that really did trigger -> child registered, then its fill closes.
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
    client.algo_query_result = {"algoId": aid, "algoStatus": "FINISHED", "actualOrderId": 8181}
    with Capture():
        await m.handle_algo_update(algo_event(aid, "FINISHED", cid))
    assert m._order_index.get(8181) == "protective_stop"
    assert m.position.status == "OPEN", "still open until the CHILD fill actually arrives"
    print("FINISHED that really triggered -> child 8181 registered; still not finalized until the "
          "child fill arrives")
    print("PASS\n")


# ============================================================================
# 9-12: child fill routing, replay, idempotency, dropped-event recovery
# ============================================================================
async def test_09_triggered_child_fill_closes_through_on_close_filled():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
    with Capture():
        await m.handle_algo_update(algo_event(aid, "TRIGGERED", cid, actual_order_id=8200))
    client.report_flat = True
    trades_before = m.trade_count
    with Capture() as cap:
        await m.handle_order_update(fill_event(8200))
    assert "role=protective_stop" in cap.text, cap.text
    assert m.position.status == "FLAT"
    assert m.trade_count == trades_before + 1
    rows = [json.loads(l) for l in open(os.environ["TRADE_LOG_JSON_PATH"]) if l.strip()]
    assert rows[-1]["exit_reason"] == "protective_stop", rows[-1]
    print("triggered child fill routed through _on_close_filled(); exit_reason=protective_stop, "
          "position FLAT, trade counted once")
    print("PASS\n")


async def test_10_child_fill_before_registration_is_replayed():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
    # child fills before ALGO_UPDATE tells us its id
    with Capture():
        await m.handle_order_update(fill_event(8300))
    assert 8300 in m._unmatched_fills
    client.report_flat = True
    with Capture() as cap:
        await m.handle_algo_update(algo_event(aid, "TRIGGERED", cid, actual_order_id=8300))
    assert "replayed_unmatched_fill" in cap.text, cap.text
    assert m.position.status == "FLAT"
    print("child fill that arrived BEFORE actualOrderId registration was replayed and closed the "
          "position")
    print("PASS\n")


async def test_11_duplicate_algo_and_child_events_are_idempotent():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
    with Capture():
        # duplicate TRIGGERED events
        await m.handle_algo_update(algo_event(aid, "TRIGGERED", cid, actual_order_id=8400))
        await m.handle_algo_update(algo_event(aid, "TRIGGERED", cid, actual_order_id=8400))
        await m.handle_algo_update(algo_event(aid, "TRIGGERING", cid, actual_order_id=8400))
    client.report_flat = True
    with Capture():
        await m.handle_order_update(fill_event(8400))
    n = m.trade_count
    with Capture():
        # duplicate child fills (WS redelivery + REST recovery)
        await m.handle_order_update(fill_event(8400))
        await m.handle_order_update(fill_event(8400))
        await m.handle_algo_update(algo_event(aid, "FINISHED", cid, actual_order_id=8400))
    assert m.trade_count == n, "duplicates must not double-count the trade"
    assert m.position.status == "FLAT"
    print(f"duplicate ALGO_UPDATE + duplicate child fills processed idempotently "
          f"(trade_count stayed {n})")
    print("PASS\n")


async def test_12_dropped_algo_event_recovered_by_rest_query():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid, cid = m.position.protective_stop_algo_id, m.position.protective_stop_client_algo_id
    # The TRIGGERED event is dropped entirely; only a TRIGGERED-with-no-child
    # arrives, so the handler must fall back to the exact REST query.
    client.algo_query_result = {"algoId": aid, "algoStatus": "TRIGGERED", "actualOrderId": 8500}
    with Capture():
        await m.handle_algo_update(algo_event(aid, "TRIGGERED", cid, actual_order_id=""))
    assert m._order_index.get(8500) == "protective_stop", "must recover child id via REST"
    q = [c for c in client.calls if c[0] == "GET" and c[1] == "/fapi/v1/algoOrder"]
    assert q, "an exact algo query must have been made"
    assert q[-1][2].get("algoId") == aid, "query must be for the EXACT tracked algo order"
    print(f"dropped child id recovered via exact GET /fapi/v1/algoOrder(algoId={aid}) -> 8500")
    print("PASS\n")


# ============================================================================
# 13-18: lifecycle, ownership, failure handling
# ============================================================================
async def test_13_dca_replace_cancels_previous_algo_stop_first():
    m, client = await make_manager()
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    first = m.position.protective_stop_algo_id
    m.position.total_qty = 2.0            # a DCA changed the economics
    m.position.avg_entry_price = 99.5
    await m._place_or_replace_protective_stop(reason="DCA #1 filled")
    assert client.cancelled_algo_ids == [first], client.cancelled_algo_ids
    assert len(client.placed_algo_orders) == 2
    second = m.position.protective_stop_algo_id
    assert second is not None and second != first
    # cancel must precede the second placement
    order_of_calls = [c[0] + " " + c[1] for c in client.calls
                      if c[1] in ("/fapi/v1/algoOrder",)]
    assert order_of_calls == ["POST /fapi/v1/algoOrder", "DELETE /fapi/v1/algoOrder",
                             "POST /fapi/v1/algoOrder"], order_of_calls
    print(f"DCA replace: cancelled algoId={first} BEFORE placing algoId={second}")
    print("PASS\n")


async def test_14_failed_cancel_retains_ownership_for_retry():
    client = RecordingClient(fail_cancel="network")
    m, client = await make_manager(client=client)
    await m._place_or_replace_protective_stop(reason="INITIAL filled")
    aid = m.position.protective_stop_algo_id
    with Capture() as cap:
        confirmed = await m._cancel_protective_stop(reason="test")
    assert confirmed is False
    assert m.position.protective_stop_algo_id == aid, "id must be retained for retry"
    assert m.position.protective_stop_cancel_pending is True
    assert "may STILL be resting" in cap.text
    # and no duplicate is stacked on top
    placed_before = len(client.placed_algo_orders)
    with Capture():
        await m._place_or_replace_protective_stop(reason="DCA #1 filled")
    assert len(client.placed_algo_orders) == placed_before, "must not stack a duplicate algo stop"
    print(f"failed cancel retained algoId={aid} (cancel_pending=True); no duplicate placed")
    print("PASS\n")


async def test_15_restart_open_adopts_only_bot_owned_algo_stop():
    orders = [
        {"algoId": 611, "orderType": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "triggerPrice": "99.40", "clientAlgoId": f"{PREFIX}-own-1", "algoStatus": "NEW",
         "actualOrderId": ""},
        {"algoId": 999, "orderType": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "triggerPrice": "50.00", "clientAlgoId": "manual-user-algo", "algoStatus": "NEW"},
    ]
    client = RecordingClient(open_algo_orders=orders)
    m, client = await make_manager(client=client)
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    assert m.position.protective_stop_algo_id == 611, "must adopt only the bot-owned algo stop"
    assert m.position.protective_stop_client_algo_id == f"{PREFIX}-own-1"
    assert 999 not in client.cancelled_algo_ids, "manual algo order must be untouched"
    assert 611 not in m._order_index, "an algoId must never enter _order_index"
    assert "not owned by this bot" in cap.text
    print("restart while OPEN adopted bot-owned algoId=611 and left manual algoId=999 untouched")
    print("PASS\n")


async def test_16_restart_flat_cancels_stale_bot_owned_algo_stops():
    orders = [
        {"algoId": 621, "orderType": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "triggerPrice": "99.40", "clientAlgoId": f"{PREFIX}-old-1", "algoStatus": "NEW"},
        {"algoId": 622, "orderType": "STOP_MARKET", "side": "BUY", "closePosition": "true",
         "triggerPrice": "101.00", "clientAlgoId": f"{PREFIX}-old-2", "algoStatus": "NEW"},
        {"algoId": 999, "orderType": "STOP_MARKET", "side": "SELL", "closePosition": "true",
         "triggerPrice": "50.00", "clientAlgoId": "manual-user-algo", "algoStatus": "NEW"},
    ]
    client = RecordingClient(open_algo_orders=orders)
    m, client = await make_manager(client=client)
    m.position = trading.PositionState(last_close_time=time.time())   # FLAT
    client.position = m.position
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    assert sorted(client.cancelled_algo_ids) == [621, 622], client.cancelled_algo_ids
    assert 999 not in client.cancelled_algo_ids, "manual algo order must be untouched"
    assert client.placed_algo_orders == [], "nothing may be placed while flat"
    assert "while FLAT" in cap.text
    print("restart while FLAT cancelled stale bot-owned algoIds 621+622 (both sides); manual 999 "
          "untouched")
    print("PASS\n")


async def test_17_manual_algo_orders_never_touched():
    m, _ = await make_manager()
    assert m._is_own_protective_stop({"clientAlgoId": f"{PREFIX}-1-1"}) is True
    assert m._is_own_protective_stop({"clientAlgoId": "manual-user-algo"}) is False
    assert m._is_own_protective_stop({"clientAlgoId": f"{PREFIX}XYZ-1"}) is False
    assert m._is_own_protective_stop({"clientAlgoId": PREFIX}) is False
    assert m._is_own_protective_stop({}) is False
    # an ALGO_UPDATE for a foreign order must be ignored entirely
    m2, client2 = await make_manager()
    await m2._place_or_replace_protective_stop(reason="INITIAL filled")
    own = m2.position.protective_stop_algo_id
    with Capture():
        await m2.handle_algo_update(algo_event(4242, "CANCELED", "manual-user-algo"))
    assert m2.position.protective_stop_algo_id == own, "a foreign ALGO_UPDATE must change nothing"
    assert m2.position.protection_pending is False
    print("strict clientAlgoId ownership; foreign ALGO_UPDATE ignored entirely")
    print("PASS\n")


async def test_18_enumeration_and_query_failures_block_blind_placement_and_dca():
    client = RecordingClient(fail_open_algo=True)
    m, client = await make_manager(client=client)
    with Capture() as cap:
        await trading.reconcile_protective_stop_on_startup(client, m)
    assert m._protective_stop_reconcile_blocked is True
    assert m._stale_protective_stops_possible is True
    assert client.placed_algo_orders == [], "must not place blind"
    assert "NOT placing a new one blind" in cap.text
    # a direct placement request is refused too
    with Capture():
        await m._place_or_replace_protective_stop(reason="INITIAL filled")
    assert client.placed_algo_orders == [], "placement must stay blocked"
    assert m.position.protection_pending is True
    # ... and PROTECTION_PENDING blocks new DCA
    m.position.dca_step = 0
    with Capture() as cap2:
        m.current_price = m.best_bid_price = m.best_ask_price = 99.0
        m.prev_price = 99.0
        await m._manage_open_position()
    assert "PROTECTION_PENDING" in cap2.text
    # A risk-REDUCING close may legitimately fire here (the per-trade loss
    # budget). What must never happen is a new DCA add - i.e. an exposure-
    # INCREASING order on the same side as the position.
    dca_adds = [
        o for o in client.placed_orders
        if o.get("reduceOnly") != "true" and o.get("side") == "BUY"
    ]
    assert dca_adds == [], f"no DCA MARKET add while protection is unknown, got {dca_adds}"
    print("enumeration failure -> no blind placement, PROTECTION_PENDING, DCA blocked")
    print("PASS\n")


# ============================================================================
# 19: normal MARKET lifecycle untouched
# ============================================================================
async def test_19_standard_market_lifecycle_unchanged():
    m, client = await make_manager()
    # a normal reduceOnly MARKET close still goes to /fapi/v1/order
    client.report_flat = False
    with Capture():
        await m.close_position("test close", exit_reason_tag="manual", expected_position=m.position)
    market_calls = [c for c in client.calls if c[1] == "/fapi/v1/order" and c[0] == "POST"]
    assert market_calls, "close must still use POST /fapi/v1/order"
    body = market_calls[-1][2]
    assert body["type"] == "MARKET" and body["reduceOnly"] == "true", body
    assert "algoType" not in body, "a normal close must never carry algo fields"
    assert client.placed_algo_orders == [], "a normal close must not hit the algo endpoint"
    # and its fill still routes through _on_close_filled()
    oid = m.position.pending_order_id
    client.report_flat = True
    n = m.trade_count
    with Capture() as cap:
        await m.handle_order_update(fill_event(oid))
    assert m.position.status == "FLAT"
    assert m.trade_count == n + 1
    assert "role=close" in cap.text
    print("MARKET close still uses /fapi/v1/order, still routes via _on_close_filled(), fill "
          "replay path untouched")
    print("PASS\n")


# ============================================================================
# 20: infrastructure exits stay in accounting but do not train Brain
# ============================================================================
async def test_20_protection_unavailable_does_not_train_brain():
    assert "protection_unavailable" in trading.INFRASTRUCTURE_ONLY_EXIT_REASONS
    m, client = await make_manager()
    m.position.entry_features = np.zeros(trading.N_FEATURES_V2, dtype=float)
    m._pending_exit_reason = "protection_unavailable"
    client.report_flat = True

    calls = {"success": 0, "quality": 0}
    m.brain.learn_success = lambda *a, **k: calls.__setitem__("success", calls["success"] + 1)
    m.brain.learn_quality = lambda *a, **k: calls.__setitem__("quality", calls["quality"] + 1)

    trades_before, daily_before = m.trade_count, m.daily_realized_pnl
    outcomes_before = len(m.recent_trade_outcomes)
    with Capture() as cap:
        await m._on_close_filled(99.9, -0.02, order_id=4321)

    assert calls == {"success": 0, "quality": 0}, f"Brain must NOT be trained, got {calls}"
    assert len(m.recent_trade_outcomes) == outcomes_before, (
        "the recent-win-rate feature must not be distorted by an infrastructure failure"
    )
    assert "SKIPPED learning" in cap.text, cap.text
    # ... but the money is still fully accounted for
    assert m.trade_count == trades_before + 1, "the trade must still be counted"
    assert m.daily_realized_pnl != daily_before, "daily loss counter must still move"
    rows = [json.loads(l) for l in open(os.environ["TRADE_LOG_JSON_PATH"]) if l.strip()]
    rec = rows[-1]
    assert rec["exit_reason"] == "protection_unavailable"
    assert float(rec["fees_usdt"]) > 0 and rec["net_pnl_usdt"] is not None
    print(f"protection_unavailable: Brain NOT trained; PnL={rec['net_pnl_usdt']} "
          f"fees={rec['fees_usdt']} still recorded, trade counted, daily counter updated")

    # control: a NORMAL exit still trains Brain exactly as before
    m2, client2 = await make_manager()
    m2.position.entry_features = np.zeros(trading.N_FEATURES_V2, dtype=float)
    m2._pending_exit_reason = "take_profit"
    client2.report_flat = True
    calls2 = {"success": 0, "quality": 0}
    m2.brain.learn_success = lambda *a, **k: calls2.__setitem__("success", calls2["success"] + 1)
    m2.brain.learn_quality = lambda *a, **k: calls2.__setitem__("quality", calls2["quality"] + 1)
    with Capture():
        await m2._on_close_filled(100.5, 0.05, order_id=4322)
    assert calls2 == {"success": 1, "quality": 1}, f"normal exits must still train, got {calls2}"
    print("control: a normal take_profit exit still trains Brain (1 success + 1 quality)")
    print("PASS\n")


# ============================================================================
# extra: legacy pre-Algo snapshot migration
# ============================================================================
async def test_21_legacy_snapshot_is_migrated_conservatively():
    """A snapshot written before this migration holds a plain-order
    protective_stop_order_id. It must NOT be adopted as an algoId, must NOT
    trigger a blind replacement, and must NOT reset dca_step."""
    path = os.environ["DCA_STATE_PATH"]
    legacy = {
        "symbol": "SOLUSDT", "status": "OPEN", "side": "LONG", "qty": 1.0,
        "avg_entry_price": 100.0, "initial_entry_price": 100.0, "dca_step": 1,
        "last_dca_price": None, "profit_lock_active": False, "peak_unrealized_pnl": 0,
        "pending_order_id": None, "pending_role": None, "opened_at": time.time() - 600,
        "dca_history": [], "total_invested_margin": 0, "current_notional": 0,
        "last_entry_order_id": None, "last_dca_order_id": None, "accumulated_close_pnl": 0,
        "position_fees_accum": 0.05, "position_fees_reliable": True,
        "dca_blocked": False, "dca_block_reason": None,
        # legacy pre-Algo fields:
        "protective_stop_order_id": 424242,
        "protective_stop_client_order_id": f"{PREFIX}-legacy-1",
        "protection_pending": False, "protection_pending_reason": None,
    }
    with open(path, "w") as f:
        json.dump(legacy, f)

    client = RecordingClient()
    m, client = await make_manager(client=client, clear_state=False)
    # Fresh process: local state is empty, but Binance still reports the real
    # open position the legacy snapshot describes, so initialize_sync() takes
    # the snapshot-restore path (which is where the migration must happen).
    m.position = trading.PositionState()
    client.position = trading.PositionState(
        side="LONG", status="OPEN", avg_entry_price=100.0, total_qty=1.0,
    )

    with Capture() as cap:
        await trading.initialize_sync(client, m, context="startup")
    out = cap.text
    assert "legacy pre-Algo snapshot detected" in out, out
    assert m.position.protective_stop_algo_id is None, "a legacy orderId must NOT become an algoId"
    assert m.position.protection_pending is True
    assert m._stale_protective_stops_possible is True, "must force reconciliation first"
    assert 424242 not in m._order_index
    assert m.position.dca_step == 1, "dca_step must NOT be reset by the migration"
    assert client.placed_algo_orders == [], "must not place a blind duplicate"
    print("legacy snapshot: orderId=424242 not adopted, PROTECTION_PENDING + stale cleanup forced, "
          "dca_step preserved at 1, no blind placement")
    print("PASS\n")


async def run_all():
    print("=== Algo REST placement / parameters ===")
    await test_01_protective_stop_uses_algo_endpoint()
    await test_02_exact_algo_parameters()
    await test_03_no_protective_stop_reaches_plain_order_endpoint()
    await test_04_success_stores_ids_and_clears_protection_pending()
    await test_05_algo_query_cancel_and_open_endpoints()
    print("=== ALGO_UPDATE stream ===")
    await test_06_algo_update_in_live_subscription()
    await test_07_algo_status_handling()
    await test_08_finished_does_not_finalize_without_proof()
    print("=== triggered child-fill processing ===")
    await test_09_triggered_child_fill_closes_through_on_close_filled()
    await test_10_child_fill_before_registration_is_replayed()
    await test_11_duplicate_algo_and_child_events_are_idempotent()
    await test_12_dropped_algo_event_recovered_by_rest_query()
    print("=== lifecycle / ownership / failure handling ===")
    await test_13_dca_replace_cancels_previous_algo_stop_first()
    await test_14_failed_cancel_retains_ownership_for_retry()
    await test_15_restart_open_adopts_only_bot_owned_algo_stop()
    await test_16_restart_flat_cancels_stale_bot_owned_algo_stops()
    await test_17_manual_algo_orders_never_touched()
    await test_18_enumeration_and_query_failures_block_blind_placement_and_dca()
    print("=== unchanged paths + Brain hygiene ===")
    await test_19_standard_market_lifecycle_unchanged()
    await test_20_protection_unavailable_does_not_train_brain()
    await test_21_legacy_snapshot_is_migrated_conservatively()
    print("ALL ALGO ORDER MIGRATION TESTS PASSED")


asyncio.run(run_all())
