#!/usr/bin/env python3
"""
================================================================================
 Websocket code - moved out of dca2.py

 This file contains ONLY what was relocated out of dca2.py: the "MARKET DATA
 WEBSOCKET" section (market_data_consumer), the "USER DATA WEBSOCKET" section
 (userdata_consumer), and listen_key_keepalive (the REST keepalive ping that
 keeps userdata_consumer's listenKey alive - it has no websocket connection
 of its own, but it exists solely to service the user-data websocket, so it
 travels with it rather than staying behind as an orphaned one-off in
 dca2.py). All reconnect/backoff logic, watchdog timers, and error handling
 are byte-for-byte identical to the original - nothing was fixed or tuned.

 One structural note on the move (not a logic change): userdata_consumer
 calls initialize_sync(...) on every reconnect. initialize_sync stays in
 dca2.py (it's position-reconciliation/trading logic - PositionState,
 MartingaleManager.position, filters - not websocket code), so importing it
 here would create a circular import (dca2.py imports userdata_consumer from
 this file). Instead, this module declares `initialize_sync = None` as a
 placeholder; dca2.py injects the real function onto this module
 (`websocket.initialize_sync = initialize_sync`) immediately after import,
 before any of these coroutines are ever scheduled. Python resolves a
 function's free variables against its OWN module's globals at call time
 (not at definition time), so `await initialize_sync(...)` inside
 userdata_consumer below is unchanged and correctly reaches dca2.py's real
 function - no signature changes were needed anywhere.

 Also self-contained otherwise: only stdlib + aiohttp + websockets, plus
 config.py (constants) and exchange.py (RestClient/BinanceApiError).
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone

import aiohttp
import websockets

from config import (
    WS_MARKET_BASE,
    WS_USERDATA_BASE,
    SYMBOL,
    USE_TESTNET,
    IDLE_DATA_TIMEOUT_SEC,
    USER_WS_IDLE_FALLBACK_SEC,
    MAX_BACKOFF_SEC,
    LISTEN_KEY_KEEPALIVE_SEC,
)
from exchange import RestClient, BinanceApiError

# ----------------------------------------------------------------------------
# Private helpers (identical copies of dca2.py's now_str()/color()/color
# constants - duplicated only to avoid a circular import; see module
# docstring above).
# ----------------------------------------------------------------------------


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


_USE_COLOR = sys.stdout.isatty()


def color(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


GREEN, RED, YELLOW, CYAN, GRAY, BOLD, MAGENTA, BLUE = "32", "31", "33", "36", "90", "1", "35", "34"


# initialize_sync is injected by dca2.py right after it imports from this
# module - see module docstring above. Left as None until then.
initialize_sync = None


# ============================================================================
# MARKET DATA WEBSOCKET (bookTicker for price/spread/book-imbalance,
# aggTrade for buy/sell volume delta)
#
# 2026-08 WS route-migration fix: Binance permanently decommissioned the
# legacy combined-stream URL (wss://fstream.binance.com/stream?streams=...)
# for LIVE on 2026-04-23, splitting traffic into three dedicated base
# paths - /public (high-frequency public data, incl. bookTicker),
# /market (regular market data, incl. aggTrade), and /private (user-data/
# listenKey streams). An unrouted connection (the old bare host) now only
# ever receives /public data - so on Live, the pre-fix code above silently
# kept receiving bookTicker (still /public) while aggTrade (now /market)
# went dark. That alone would not explain the DCA/resync bug (aggTrade only
# feeds an informational buy/sell-volume signal, not order-fill state), but
# it is a real, independently confirmed data-loss bug fixed here as part of
# the same migration. See module docstring / Binance's own migration notice:
# https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice
#
# Testnet (stream.binancefuture.com) is NOT covered by that notice, so its
# combined-stream behavior is left completely untouched below - only the
# Live host now splits into two independent connections, one per route
# category, each with its own identical reconnect/backoff/watchdog logic
# (byte-for-byte the same loop shape as the original single connection,
# just parameterized per stream instead of duplicated by hand).
# ============================================================================


async def _run_single_market_stream(
    manager: MartingaleManager, url: str, label: str, stream_suffix: str,
) -> None:
    """One reconnecting websocket connection for exactly one market stream
    category (either the legacy combined connection, or - post migration -
    one of the two Live /public / /market connections). Reconnect/backoff/
    watchdog shape is unchanged from the original single-connection
    version; only parameterized by URL/label so it can be reused for each
    routed connection without duplicating the loop by hand."""
    backoff = 1.0
    while True:
        try:
            print(color(f"[market-ws:{label}] connecting to {url} ...", GRAY))
            async with websockets.connect(
                url, ping_interval=15, ping_timeout=10, max_queue=2048
            ) as ws:
                print(color(f"[market-ws:{label}] connected.", GREEN))
                backoff = 1.0
                last_msg_time = time.time()

                async def watchdog(ws_ref) -> None:
                    while True:
                        await asyncio.sleep(5)
                        if time.time() - last_msg_time > IDLE_DATA_TIMEOUT_SEC:
                            print(color(f"[market-ws:{label}] idle timeout, forcing reconnect ...", RED))
                            await ws_ref.close()
                            return

                wd_task = asyncio.create_task(watchdog(ws))
                try:
                    async for raw in ws:
                        last_msg_time = time.time()
                        try:
                            msg = json.loads(raw)
                            stream = msg.get("stream", "")
                            data = msg.get("data", {})
                            if stream.endswith("@bookTicker") and stream_suffix in ("bookTicker", "both"):
                                bid = float(data.get("b", 0) or 0)
                                ask = float(data.get("a", 0) or 0)
                                bid_qty = float(data.get("B", 0) or 0)
                                ask_qty = float(data.get("A", 0) or 0)
                                if bid and ask:
                                    manager.on_book_ticker(bid, ask, bid_qty, ask_qty)
                                    await manager.on_price_tick()
                            elif stream.endswith("@aggTrade") and stream_suffix in ("aggTrade", "both"):
                                qty = float(data.get("q", 0) or 0)
                                is_buyer_maker = bool(data.get("m", False))
                                if qty > 0:
                                    manager.on_agg_trade(qty, is_buyer_maker)
                        except Exception as e:  # noqa: BLE001 - one bad tick must not kill the socket
                            print(color(f"[market-ws:{label}] error processing message, skipping: {e}", RED))
                finally:
                    wd_task.cancel()
        except Exception as e:  # noqa: BLE001 - this IS the reconnect boundary; anything
            # that escapes the websocket context should trigger backoff+retry, not a crash.
            print(color(f"[market-ws:{label}] disconnected ({e}), retrying in {backoff:.1f}s ...", RED))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SEC)


async def market_data_consumer(manager: MartingaleManager) -> None:
    """Entry point unchanged (still a single coroutine scheduled once from
    dca2.py's asyncio.gather()) - internally routes to either the original
    legacy combined connection (Testnet, unaffected by the migration) or
    two independent routed connections (Live: /public for bookTicker,
    /market for aggTrade), each reconnecting/backing off independently so
    one stream category dropping never silently starves the other."""
    if USE_TESTNET:
        book_stream = f"{SYMBOL.lower()}@bookTicker/{SYMBOL.lower()}@aggTrade"
        url = f"{WS_MARKET_BASE}/stream?streams={book_stream}"
        await _run_single_market_stream(manager, url, label="testnet-combined", stream_suffix="both")
        return

    public_url = f"{WS_MARKET_BASE}/public/stream?streams={SYMBOL.lower()}@bookTicker"
    market_url = f"{WS_MARKET_BASE}/market/stream?streams={SYMBOL.lower()}@aggTrade"
    await asyncio.gather(
        _run_single_market_stream(manager, public_url, label="public/bookTicker", stream_suffix="bookTicker"),
        _run_single_market_stream(manager, market_url, label="market/aggTrade", stream_suffix="aggTrade"),
    )


# ============================================================================
# USER DATA WEBSOCKET
# ============================================================================


async def userdata_consumer(client: RestClient, manager: MartingaleManager) -> None:
    backoff = 1.0
    while True:
        # 2026-08 HTTP 418/429 cooldown-survival fix (this check only -
        # everything else in this reconnect loop is unchanged): skip this
        # reconnect attempt entirely and silently while the shared REST
        # cooldown (armed by RestClient._request() on a 418/429 - see
        # exchange.py) is active, instead of calling create_listen_key()
        # (which would just raise locally anyway) and logging a fresh
        # "[user-ws] disconnected (...), retrying in Xs" error every
        # reconnect attempt. wait_out_cooldown_silently() sleeps out the
        # cooldown (plus jitter, so this loop doesn't resume on the exact
        # same tick as every other poller) and logs the one-time resume
        # line itself - nothing else needs to log here. `continue` re-enters
        # the loop, which re-checks the cooldown before ever calling
        # create_listen_key() again.
        if client.is_cooldown_active():
            await client.wait_out_cooldown_silently()
            continue
        try:
            listen_key = await client.create_listen_key()
            # 2026-08 WS route-migration fix: the legacy raw path
            # (wss://fstream.binance.com/ws/<listenKey>) was permanently
            # decommissioned for LIVE user-data streams on 2026-04-23 -
            # private channels stop pushing data on an unrouted connection
            # even though the TCP/WebSocket handshake itself still
            # succeeds (see module docstring). That is the confirmed
            # primary root cause of the missed-fill/over-DCA bug this
            # patch fixes: the process looked "connected" while
            # ORDER_TRADE_UPDATE/ACCOUNT_UPDATE simply never arrived.
            # Routed through /private with an explicit events= subscription
            # on Live; Testnet (not covered by that migration notice) keeps
            # the exact original URL/behavior, unchanged.
            if USE_TESTNET:
                url = f"{WS_USERDATA_BASE}/ws/{listen_key}"
            else:
                url = (
                    f"{WS_USERDATA_BASE}/private/ws?listenKey={listen_key}"
                    f"&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE"
                )
            # Never log the full URL (it embeds the listenKey) - keep the
            # existing generic "connecting ..." message exactly as before.
            print(color("[user-ws] connecting ...", GRAY))
            async with websockets.connect(url, ping_interval=15, ping_timeout=10) as ws:
                print(color("[user-ws] connected - listening for order fills.", GREEN))
                backoff = 1.0
                last_msg_time = time.time()

                await initialize_sync(client, manager, context="user-ws reconnect")

                async def watchdog(ws_ref) -> None:
                    while True:
                        await asyncio.sleep(30)
                        if time.time() - last_msg_time > USER_WS_IDLE_FALLBACK_SEC:
                            print(color(
                                "[user-ws] no messages AND no pong for an extended "
                                "period, forcing reconnect as a last resort ...", RED
                            ))
                            await ws_ref.close()
                            return

                wd_task = asyncio.create_task(watchdog(ws))
                try:
                    async for raw in ws:
                        last_msg_time = time.time()
                        try:
                            event = json.loads(raw)
                            etype = event.get("e")
                            if etype == "ORDER_TRADE_UPDATE":
                                # A successful websocket handshake is not proof
                                # that the private stream is delivering fills.
                                # Emit one credential-free receipt marker for
                                # completed orders so a controlled Live check can
                                # distinguish the real user-stream path from a
                                # later REST reconciliation.  Never include the
                                # listenKey or the API key in this diagnostic.
                                order = event.get("o", {})
                                if str(order.get("X", "")).upper() == "FILLED":
                                    print(color(
                                        f"{now_str()} [user-ws] ORDER_TRADE_UPDATE received "
                                        f"order_id={order.get('i')} status=FILLED",
                                        CYAN,
                                    ))
                                await manager.handle_order_update(event)
                            elif etype == "ACCOUNT_UPDATE":
                                for b in event.get("a", {}).get("B", []):
                                    if b.get("a") == "USDT":
                                        manager.available_balance = float(b.get("cw") or b.get("wb") or 0)
                        except Exception as e:  # noqa: BLE001 - one bad message must not kill the socket
                            print(color(f"[user-ws] error processing message, skipping: {e}", RED))
                finally:
                    wd_task.cancel()
        except Exception as e:  # noqa: BLE001 - reconnect boundary.
            print(color(f"[user-ws] disconnected ({e}), retrying in {backoff:.1f}s ...", RED))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SEC)


async def listen_key_keepalive(client: RestClient) -> None:
    while True:
        await asyncio.sleep(LISTEN_KEY_KEEPALIVE_SEC)
        # 2026-08 HTTP 418/429 cooldown-survival fix - same pattern as the
        # REST pollers in dca2.py: skip silently and wait out the shared
        # cooldown instead of calling through (which would just raise
        # locally anyway) and logging an error every LISTEN_KEY_KEEPALIVE_SEC.
        if client.is_cooldown_active():
            await client.wait_out_cooldown_silently()
            continue
        try:
            await client.keepalive_listen_key()
            print(color(f"{now_str()} [user-ws] listenKey keepalive sent.", GRAY))
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[user-ws] listenKey keepalive failed: {e}", RED))
