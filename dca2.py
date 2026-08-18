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

 2026-08 HIGH-FREQUENCY ORDERFLOW UPGRADE (additive - nothing existing was
 removed or retuned):
   - The market feed now also subscribes to Binance Futures'
     @depth<N>@100ms partial-book stream alongside the pre-existing
     @bookTicker and @aggTrade streams (see market_data_consumer for the
     per-route placement and why depth gets its own Live connection).
   - OrderFlowTracker (below) maintains bounded collections.deque ring
     buffers of that data and derives two live signals: the Orderbook
     Imbalance Index over the top ORDERBOOK_DEPTH_LEVELS levels, and the
     rolling AGG_TRADE_DELTA_WINDOW_SEC aggregated trade-volume delta.
     Both buffers have a hard maxlen, so this layer's RAM footprint is a
     fixed constant - the key requirement for a small Railway container.
   - Every reconnect loop in this file now waits a FULL-JITTERED share of
     its existing exponential backoff (_reconnect_delay), so the now
     several independent sockets recover from a Railway network drop
     without retrying in lockstep. The backoff growth curve and its
     MAX_BACKOFF_SEC ceiling are unchanged.
   - trading.py's MartingaleManager owns one OrderFlowTracker instance and
     receives depth frames through its on_depth_update() method, mirroring
     how it already receives bookTicker/aggTrade.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, List, Optional, Sequence, Tuple

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
    # --- 2026-08 high-frequency orderflow upgrade (appended config) -----
    ORDERBOOK_STREAM_DEPTH,
    ORDERBOOK_STREAM_SPEED_MS,
    ORDERBOOK_DEPTH_LEVELS,
    ORDERBOOK_BUFFER_MAXLEN,
    AGG_TRADE_BUFFER_MAXLEN,
    AGG_TRADE_DELTA_WINDOW_SEC,
    ORDERFLOW_STALE_SEC,
    WS_RECONNECT_JITTER_RATIO,
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
# HIGH-FREQUENCY ORDERFLOW DATA LAYER (2026-08 upgrade)
# ============================================================================
# Everything below this banner is ADDITIVE. The pre-existing bookTicker /
# aggTrade handling, the reconnect/backoff/watchdog loop shape, and the
# user-data stream are all preserved - this only adds a second, richer
# source of microstructure state on top of them.
#
# Two derived signals are maintained, exactly as specified:
#
#   1. Orderbook Imbalance Index, from the @depth<N>@100ms partial-book
#      stream:
#          (top10_bid_vol - top10_ask_vol) / (top10_bid_vol + top10_ask_vol)
#      Range [-1, +1]. Positive = bids dominate (buy-side pressure),
#      negative = asks dominate (sell-side pressure).
#
#   2. Aggregated Trade Volume Delta over a rolling
#      AGG_TRADE_DELTA_WINDOW_SEC (10s) window, from @aggTrade:
#          market_buy_volume - market_sell_volume
#      Binance marks each aggTrade with "m" (isBuyerMaker). m == True means
#      the BUYER was the maker, i.e. the aggressor SOLD into the bid -> that
#      quantity counts as market-SELL volume. m == False is the mirror case
#      and counts as market-BUY volume. (This is the same convention the
#      pre-existing CandleAggregator.on_trade() already uses in trading.py,
#      kept identical here so the two never disagree.)
#
# RAM SAFETY (the Railway constraint): both buffers are collections.deque
# instances with a hard `maxlen`. A deque at maxlen discards from the
# opposite end on every append, so the memory this layer can ever occupy is
# a fixed constant - ORDERBOOK_BUFFER_MAXLEN tuples + AGG_TRADE_BUFFER_MAXLEN
# tuples - regardless of process uptime or market activity. Nothing here
# grows without bound, and nothing is written to disk.
# ============================================================================


class OrderFlowTracker:
    """In-memory rolling microstructure state for ONE symbol.

    Deliberately synchronous, allocation-light and exception-free: it is
    called from the websocket read loop on every single depth/trade message
    (up to ~10 depth updates/second plus every aggregated trade), so a slow
    or throwing implementation here would stall the socket itself. Every
    public reader returns a safe neutral value rather than raising when no
    data has arrived yet or the feed has gone stale.
    """

    def __init__(
        self,
        depth_levels: int = ORDERBOOK_DEPTH_LEVELS,
        window_sec: float = AGG_TRADE_DELTA_WINDOW_SEC,
        depth_maxlen: int = ORDERBOOK_BUFFER_MAXLEN,
        trade_maxlen: int = AGG_TRADE_BUFFER_MAXLEN,
        stale_sec: float = ORDERFLOW_STALE_SEC,
    ) -> None:
        self.depth_levels = max(int(depth_levels), 1)
        self.window_sec = max(float(window_sec), 1.0)
        self.stale_sec = max(float(stale_sec), 0.5)

        # Bounded ring buffers - see the RAM SAFETY note above.
        # _depth_buf entries: (ts, imbalance, bid_vol, ask_vol)
        self._depth_buf: Deque[Tuple[float, float, float, float]] = deque(maxlen=int(depth_maxlen))
        # _trade_buf entries: (ts, signed_qty)  [+ = market buy, - = market sell]
        self._trade_buf: Deque[Tuple[float, float]] = deque(maxlen=int(trade_maxlen))

        self.last_imbalance: float = 0.0
        self.last_bid_volume: float = 0.0
        self.last_ask_volume: float = 0.0
        self.last_depth_ts: float = 0.0
        self.last_trade_ts: float = 0.0
        self.depth_updates: int = 0
        self.trade_updates: int = 0

    # -- ingestion (called from the websocket read loop) ---------------------

    @staticmethod
    def _side_volume(levels: Optional[Sequence], depth_levels: int) -> float:
        """Sum the quantity column of the top `depth_levels` price levels.

        Binance sends each level as a two-element ["price", "qty"] array of
        STRINGS. A malformed/short level is skipped rather than raising -
        one bad frame must never kill the market socket."""
        if not levels:
            return 0.0
        total = 0.0
        for level in list(levels)[:depth_levels]:
            try:
                total += float(level[1])
            except (TypeError, ValueError, IndexError):
                continue
        return total

    def on_depth(
        self, bids: Optional[Sequence], asks: Optional[Sequence], ts: Optional[float] = None,
    ) -> float:
        """Ingest one partial-book snapshot and return the fresh imbalance.

        Returns the previous imbalance unchanged when the frame carries no
        usable volume on either side (an empty book side is not evidence of
        a real -1/+1 imbalance)."""
        now = time.time() if ts is None else ts
        bid_vol = self._side_volume(bids, self.depth_levels)
        ask_vol = self._side_volume(asks, self.depth_levels)
        total = bid_vol + ask_vol
        if total <= 0:
            return self.last_imbalance
        imbalance = (bid_vol - ask_vol) / total
        # Numerically this is already in [-1, 1]; clamped anyway so a freak
        # float artifact can never leak out of the data layer.
        if imbalance > 1.0:
            imbalance = 1.0
        elif imbalance < -1.0:
            imbalance = -1.0

        self.last_imbalance = imbalance
        self.last_bid_volume = bid_vol
        self.last_ask_volume = ask_vol
        self.last_depth_ts = now
        self.depth_updates += 1
        self._depth_buf.append((now, imbalance, bid_vol, ask_vol))
        return imbalance

    def on_agg_trade(self, qty: float, is_buyer_maker: bool, ts: Optional[float] = None) -> None:
        """Ingest one aggregated trade. See the buyer-maker convention note
        in this module's ORDERFLOW banner above."""
        try:
            quantity = float(qty)
        except (TypeError, ValueError):
            return
        if quantity <= 0:
            return
        now = time.time() if ts is None else ts
        self.last_trade_ts = now
        self.trade_updates += 1
        # is_buyer_maker True  -> aggressor was the SELLER -> market sell
        # is_buyer_maker False -> aggressor was the BUYER  -> market buy
        self._trade_buf.append((now, -quantity if is_buyer_maker else quantity))

    # -- readers -------------------------------------------------------------

    def depth_is_fresh(self, now: Optional[float] = None) -> bool:
        if self.last_depth_ts <= 0:
            return False
        ref = time.time() if now is None else now
        return (ref - self.last_depth_ts) <= self.stale_sec

    def trades_are_fresh(self, now: Optional[float] = None) -> bool:
        if self.last_trade_ts <= 0:
            return False
        ref = time.time() if now is None else now
        return (ref - self.last_trade_ts) <= max(self.stale_sec, self.window_sec)

    def imbalance(self, now: Optional[float] = None) -> float:
        """Latest Orderbook Imbalance Index, or 0.0 (neutral) if the depth
        feed has gone stale. Callers that must distinguish "genuinely
        balanced book" from "no data" use depth_is_fresh()/snapshot()."""
        return self.last_imbalance if self.depth_is_fresh(now) else 0.0

    def average_imbalance(self, seconds: float = 1.0, now: Optional[float] = None) -> float:
        """Mean imbalance across the last `seconds` of depth samples - a
        smoothed reading for decisions that should not hinge on a single
        100ms frame. Falls back to the latest value when the window holds
        no samples."""
        ref = time.time() if now is None else now
        cutoff = ref - max(seconds, 0.0)
        values = [imb for (ts, imb, _b, _a) in self._depth_buf if ts >= cutoff]
        if not values:
            return self.imbalance(ref)
        return sum(values) / len(values)

    def _prune_trades(self, now: float) -> None:
        """Drop trades that have fallen out of the rolling delta window.

        Memory hygiene only, NOT the correctness mechanism: deque.popleft()
        is O(1) and the live feed appends in timestamp order, so this keeps
        the working set well under the deque's own maxlen ceiling on a fast
        tape. It deliberately stops at the first in-window entry rather than
        scanning the whole buffer - every reader below applies its own
        explicit cutoff filter, so an out-of-order timestamp (a replayed or
        back-dated trade) can never be counted just because it happened to
        land behind a newer one and escape this loop."""
        cutoff = now - self.window_sec
        buf = self._trade_buf
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def trade_delta(self, window_sec: Optional[float] = None, now: Optional[float] = None) -> float:
        """Signed aggregated trade volume delta (market buys - market sells)
        over the rolling window. 0.0 when no trade has printed in it.

        The cutoff is applied explicitly here (never inferred from the
        buffer's order) so the window is exact regardless of arrival order -
        see _prune_trades' note."""
        ref = time.time() if now is None else now
        self._prune_trades(ref)
        span = self.window_sec if window_sec is None else max(float(window_sec), 0.0)
        cutoff = ref - span
        return sum(q for (ts, q) in self._trade_buf if ts >= cutoff)

    def trade_volumes(self, now: Optional[float] = None) -> Tuple[float, float]:
        """(market_buy_volume, market_sell_volume) over the rolling window."""
        ref = time.time() if now is None else now
        self._prune_trades(ref)
        cutoff = ref - self.window_sec
        buy = sum(q for (ts, q) in self._trade_buf if ts >= cutoff and q > 0)
        sell = -sum(q for (ts, q) in self._trade_buf if ts >= cutoff and q < 0)
        return buy, sell

    def snapshot(self, now: Optional[float] = None) -> dict:
        """One flat dict of everything the strategy/risk layers consume.

        `data_available` is the single field callers should gate on: it is
        True only when BOTH the depth feed and the trade feed have produced
        a reading recently enough to be trusted. A dead stream therefore
        reads as "no data" (which the entry guard treats as a block), never
        as a neutral, permissive 0.0."""
        ref = time.time() if now is None else now
        depth_fresh = self.depth_is_fresh(ref)
        trades_fresh = self.trades_are_fresh(ref)
        buy_vol, sell_vol = self.trade_volumes(ref)
        return {
            "imbalance": self.last_imbalance if depth_fresh else 0.0,
            "imbalance_avg_1s": self.average_imbalance(1.0, ref) if depth_fresh else 0.0,
            "bid_volume": self.last_bid_volume,
            "ask_volume": self.last_ask_volume,
            "trade_delta": self.trade_delta(now=ref),
            "buy_volume": buy_vol,
            "sell_volume": sell_vol,
            "depth_fresh": depth_fresh,
            "trades_fresh": trades_fresh,
            "data_available": depth_fresh and trades_fresh,
            "depth_age_sec": (ref - self.last_depth_ts) if self.last_depth_ts else None,
            "trade_age_sec": (ref - self.last_trade_ts) if self.last_trade_ts else None,
            "depth_updates": self.depth_updates,
            "trade_updates": self.trade_updates,
            "window_sec": self.window_sec,
            "depth_levels": self.depth_levels,
        }


def depth_stream_name(symbol: str = SYMBOL) -> str:
    """`btcusdt@depth20@100ms` - Binance's partial book depth stream at the
    configured level count and update speed."""
    return f"{symbol.lower()}@depth{ORDERBOOK_STREAM_DEPTH}@{ORDERBOOK_STREAM_SPEED_MS}ms"


def _reconnect_delay(backoff: float) -> float:
    """Full-jitter backoff for the market/user reconnect loops.

    The exponential ceiling (MAX_BACKOFF_SEC) is unchanged - this only
    randomizes WITHIN the current backoff so the now-three independent
    market connections, which typically drop together when a Railway
    container loses its network, do not all retry on the same tick and
    hammer Binance in lockstep after every blip."""
    ratio = min(max(WS_RECONNECT_JITTER_RATIO, 0.0), 1.0)
    if ratio <= 0:
        return backoff
    return backoff * (1.0 - ratio) + random.random() * backoff * ratio


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


def _handles(stream_suffix: str, kind: str) -> bool:
    """Whether a connection opened for `stream_suffix` should process a
    message of type `kind`.

    Preserves the original three-value contract exactly - "bookTicker",
    "aggTrade" and "both" behave as they always did - and only adds "depth"
    as a fourth routed category. "both" (the Testnet combined connection)
    means every category, as it always has."""
    return stream_suffix == "both" or stream_suffix == kind


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
                            if stream.endswith("@bookTicker") and _handles(stream_suffix, "bookTicker"):
                                bid = float(data.get("b", 0) or 0)
                                ask = float(data.get("a", 0) or 0)
                                bid_qty = float(data.get("B", 0) or 0)
                                ask_qty = float(data.get("A", 0) or 0)
                                if bid and ask:
                                    manager.on_book_ticker(bid, ask, bid_qty, ask_qty)
                                    await manager.on_price_tick()
                            elif stream.endswith("@aggTrade") and _handles(stream_suffix, "aggTrade"):
                                qty = float(data.get("q", 0) or 0)
                                is_buyer_maker = bool(data.get("m", False))
                                if qty > 0:
                                    manager.on_agg_trade(qty, is_buyer_maker)
                            elif "@depth" in stream and _handles(stream_suffix, "depth"):
                                # 2026-08 high-frequency orderflow upgrade.
                                # Binance USD-M partial book depth frames use
                                # the short keys "b"/"a" (each a list of
                                # ["price", "qty"] string pairs). The long
                                # "bids"/"asks" spelling is accepted too so a
                                # payload-shape difference between the Live and
                                # Testnet hosts can never silently blind this
                                # feed. Never awaits: depth arrives every 100ms
                                # and must not drive the (much heavier)
                                # on_price_tick() decision cycle - bookTicker
                                # remains the sole tick driver, exactly as
                                # before this upgrade.
                                bids = data.get("b")
                                if bids is None:
                                    bids = data.get("bids")
                                asks = data.get("a")
                                if asks is None:
                                    asks = data.get("asks")
                                if bids is not None or asks is not None:
                                    manager.on_depth_update(bids, asks)
                        except Exception as e:  # noqa: BLE001 - one bad tick must not kill the socket
                            print(color(f"[market-ws:{label}] error processing message, skipping: {e}", RED))
                finally:
                    wd_task.cancel()
        except Exception as e:  # noqa: BLE001 - this IS the reconnect boundary; anything
            # that escapes the websocket context should trigger backoff+retry, not a crash.
            print(color(f"[market-ws:{label}] disconnected ({e}), retrying in {backoff:.1f}s ...", RED))
        # 2026-08 dynamic auto-reconnection: the exponential ceiling below is
        # unchanged; only the wait is now full-jittered (see _reconnect_delay)
        # so the independent market connections do not resynchronize into a
        # lockstep retry storm after a Railway network drop takes them all
        # down at the same instant.
        await asyncio.sleep(_reconnect_delay(backoff))
        backoff = min(backoff * 2, MAX_BACKOFF_SEC)


async def market_data_consumer(manager: MartingaleManager) -> None:
    """Entry point unchanged (still a single coroutine scheduled once from
    dca2.py's asyncio.gather()) - internally routes to either the original
    legacy combined connection (Testnet, unaffected by the migration) or
    independent routed connections (Live: /public for the high-frequency
    bookTicker and partial-book depth feeds, /market for aggTrade), each
    reconnecting/backing off independently so one stream category dropping
    never silently starves the others.

    2026-08 high-frequency orderflow upgrade: the @depth<N>@100ms partial
    book stream is added alongside the pre-existing bookTicker/aggTrade
    streams. It is given its OWN Live connection rather than being folded
    into the existing bookTicker one, for two reasons:
      - it is by far the highest-message-rate stream here (10 frames/sec,
        each carrying 2x20 price levels), so isolating it means a depth
        backlog can never delay a bookTicker tick - and bookTicker is what
        drives the actual trading decision cycle (on_price_tick);
      - it keeps each existing connection's URL, label and reconnect
        lifecycle byte-for-byte what it was, so the migration-era routing
        behavior this file already fixed is not disturbed at all.
    Depth is a /public (high-frequency) category stream, same as
    bookTicker. Testnet keeps its single legacy combined connection and
    simply carries the extra stream in the same subscription list - with
    aggTrade kept last so the combined URL's existing shape is unchanged
    apart from the added stream."""
    depth_stream = depth_stream_name(SYMBOL)
    if USE_TESTNET:
        book_stream = (
            f"{SYMBOL.lower()}@bookTicker/{depth_stream}/{SYMBOL.lower()}@aggTrade"
        )
        url = f"{WS_MARKET_BASE}/stream?streams={book_stream}"
        await _run_single_market_stream(manager, url, label="testnet-combined", stream_suffix="both")
        return

    public_url = f"{WS_MARKET_BASE}/public/stream?streams={SYMBOL.lower()}@bookTicker"
    depth_url = f"{WS_MARKET_BASE}/public/stream?streams={depth_stream}"
    market_url = f"{WS_MARKET_BASE}/market/stream?streams={SYMBOL.lower()}@aggTrade"
    await asyncio.gather(
        _run_single_market_stream(manager, public_url, label="public/bookTicker", stream_suffix="bookTicker"),
        _run_single_market_stream(manager, depth_url, label="public/depth", stream_suffix="depth"),
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
                # 2026-08 Algo-Service migration: ALGO_UPDATE carries the
                # lifecycle of conditional (algo) orders - the exchange-native
                # protective stop now lives there, so it MUST be subscribed
                # alongside the existing two events or the stop's
                # NEW/TRIGGERED/FINISHED transitions would never arrive.
                url = (
                    f"{WS_USERDATA_BASE}/private/ws?listenKey={listen_key}"
                    f"&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE"
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
                            elif etype == "ALGO_UPDATE":
                                # 2026-08 Algo-Service migration: lifecycle of
                                # the exchange-native protective stop. One
                                # credential-free receipt marker (no listenKey,
                                # no API key), then the same conservative
                                # handler the REST recovery path uses.
                                print(color(
                                    f"{now_str()} [user-ws] ALGO_UPDATE received", CYAN,
                                ))
                                await manager.handle_algo_update(event)
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
        # 2026-08 dynamic auto-reconnection: same full-jitter treatment as
        # the market streams above (ceiling and growth curve unchanged).
        await asyncio.sleep(_reconnect_delay(backoff))
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
