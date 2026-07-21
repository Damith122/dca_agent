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
# aggTrade for buy/sell volume delta - combined stream, single connection)
# ============================================================================


async def market_data_consumer(manager: MartingaleManager) -> None:
    host_idx = 0
    backoff = 1.0
    hosts = [WS_MARKET_BASE]
    stream_path = f"{SYMBOL.lower()}@bookTicker/{SYMBOL.lower()}@aggTrade"

    while True:
        host = hosts[host_idx % len(hosts)]
        url = f"{host}/stream?streams={stream_path}"
        try:
            print(color(f"[market-ws] connecting to {host} ...", GRAY))
            async with websockets.connect(
                url, ping_interval=15, ping_timeout=10, max_queue=2048
            ) as ws:
                print(color("[market-ws] connected (bookTicker + aggTrade).", GREEN))
                backoff = 1.0
                last_msg_time = time.time()

                async def watchdog(ws_ref) -> None:
                    while True:
                        await asyncio.sleep(5)
                        if time.time() - last_msg_time > IDLE_DATA_TIMEOUT_SEC:
                            print(color("[market-ws] idle timeout, forcing reconnect ...", RED))
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
                            if stream.endswith("@bookTicker"):
                                bid = float(data.get("b", 0) or 0)
                                ask = float(data.get("a", 0) or 0)
                                bid_qty = float(data.get("B", 0) or 0)
                                ask_qty = float(data.get("A", 0) or 0)
                                if bid and ask:
                                    manager.on_book_ticker(bid, ask, bid_qty, ask_qty)
                                    await manager.on_price_tick()
                            elif stream.endswith("@aggTrade"):
                                qty = float(data.get("q", 0) or 0)
                                is_buyer_maker = bool(data.get("m", False))
                                if qty > 0:
                                    manager.on_agg_trade(qty, is_buyer_maker)
                        except Exception as e:  # noqa: BLE001 - one bad tick must not kill the socket
                            print(color(f"[market-ws] error processing message, skipping: {e}", RED))
                finally:
                    wd_task.cancel()
        except Exception as e:  # noqa: BLE001 - this IS the reconnect boundary; anything
            # that escapes the websocket context should trigger backoff+retry, not a crash.
            print(color(f"[market-ws] disconnected ({e}), retrying in {backoff:.1f}s ...", RED))
        host_idx += 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SEC)


# ============================================================================
# USER DATA WEBSOCKET
# ============================================================================


async def userdata_consumer(client: RestClient, manager: MartingaleManager) -> None:
    backoff = 1.0
    while True:
        try:
            listen_key = await client.create_listen_key()
            url = f"{WS_USERDATA_BASE}/ws/{listen_key}"
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
        try:
            await client.keepalive_listen_key()
            print(color(f"{now_str()} [user-ws] listenKey keepalive sent.", GRAY))
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[user-ws] listenKey keepalive failed: {e}", RED))
