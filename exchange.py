#!/usr/bin/env python3
"""
================================================================================
 Binance REST API client - moved out of dca2.py

 This file contains ONLY what was relocated out of dca2.py's "REST CLIENT
 (signed requests, HMAC-SHA256)" and "SYMBOL FILTERS" sections: BinanceApiError,
 RestClient, SymbolFilters, and fetch_symbol_filters. Every method, formula,
 and error-handling branch is unchanged from the original dca2.py source -
 nothing was fixed, renamed, or optimized. fetch_symbol_filters travels with
 RestClient since it takes a RestClient instance and calls
 client.get_exchange_info() - it's a REST API operation, not trading logic.

 This module is self-contained: it does not import anything from dca2.py,
 config.py, indicators.py, or brain.py, and none of those modules are needed
 by it. It only depends on stdlib + aiohttp.
================================================================================
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import aiohttp

# ============================================================================
# REST CLIENT (signed requests, HMAC-SHA256)
# ============================================================================


class BinanceApiError(Exception):
    def __init__(self, status: int, data: dict, headers: Optional[dict] = None):
        self.status = status
        self.data = data
        # 2026-08 HTTP 418/429 cooldown fix (this field only - status/data/
        # the exception message are unchanged): response headers are now
        # captured so callers (specifically RestClient._request()'s own
        # cooldown-arming logic below) can read Retry-After without a
        # second request. headers defaults to {} (not None) so any
        # existing `e.headers.get(...)` call written against this
        # exception never needs a None-check - backward compatible with
        # every existing raise site/catch site, which never passed or read
        # this field before.
        self.headers = headers or {}
        super().__init__(f"HTTP {status}: {data}")

    @property
    def code(self) -> Optional[int]:
        return self.data.get("code") if isinstance(self.data, dict) else None


class RestClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.session: Optional[aiohttp.ClientSession] = None
        self._time_offset_ms = 0
        # 2026-08 HTTP 418/429 global cooldown fix (these two fields only):
        # ONE shared cooldown, keyed off wall-clock time.time(), consulted
        # by every single call through _request() below - startup calls
        # (retry_with_backoff(client.start, ...) etc.) and every poller
        # (balance_refresher, position_risk_poller, funding_oi_poller,
        # listen_key_keepalive, and _manage_open_position()'s own order
        # placement) all go through this same RestClient instance's
        # _request(), so arming this once here protects all of them
        # without touching any individual poller's own code.
        # _cooldown_until_ts: wall-clock time.time() the cooldown expires;
        # 0.0 (falsy) means "no cooldown active" - _request() below skips
        # sending the request entirely while time.time() < this value.
        self._cooldown_until_ts: float = 0.0
        # _cooldown_logged_until_ts: which cooldown expiry was last logged,
        # so re-arming (or re-entering _request while still under an
        # already-logged cooldown) doesn't re-print the same message on
        # every poll - logs exactly once per distinct cooldown window.
        self._cooldown_logged_until_ts: float = 0.0
        # _cooldown_resume_logged: whether the one-time "cooldown expired,
        # resuming" line has already been printed for the CURRENT cooldown
        # window - reset to False every time _arm_cooldown() (re)arms a
        # cooldown, set True by note_cooldown_resume_if_needed() the first
        # time any caller notices it has cleared. Prevents every poller
        # from printing its own duplicate resume line.
        self._cooldown_resume_logged: bool = True

    async def start(self) -> None:
        # 2026-08 session-cleanup fix (this method only - _sync_server_time/
        # _request/every other method is unchanged): retry_with_backoff()
        # (dca2.py) calls start() again on any failure, and the old code
        # unconditionally created a brand-new aiohttp.ClientSession every
        # single call - a failed attempt (e.g. _sync_server_time() raising
        # after the session was already created) left that session
        # referenced nowhere once the next retry overwrote self.session,
        # producing "Unclosed client session"/"Unclosed connector"
        # warnings. Now: (1) close out any session already sitting in
        # self.session (from a previous failed attempt) before creating a
        # new one, and (2) if THIS attempt's _sync_server_time() call
        # fails, close the session it just opened before re-raising, so a
        # subsequent retry never has two live sessions to choose between.
        if self.session is not None and not self.session.closed:
            await self.session.close()
        connector = aiohttp.TCPConnector(resolver=aiohttp.resolver.ThreadedResolver())
        self.session = aiohttp.ClientSession(
            connector=connector, headers={"X-MBX-APIKEY": self.api_key}
        )
        try:
            await self._sync_server_time()
        except Exception:
            await self.session.close()
            self.session = None
            raise

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _sync_server_time(self) -> None:
        data = await self._request("GET", "/fapi/v1/time")
        server_ms = data["serverTime"]
        local_ms = int(time.time() * 1000)
        self._time_offset_ms = server_ms - local_ms

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _sign(self, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = self._timestamp()
        params.setdefault("recvWindow", 5000)
        query = urllib.parse.urlencode(params, doseq=True)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _arm_cooldown(self, resp_status: int, headers, data: dict) -> None:
        """2026-08 HTTP 418/429 global cooldown fix. Called only from
        _request() below, only for a 418/429 response - every other status
        (including every other 4xx/5xx) is completely unaffected and still
        just raises BinanceApiError exactly as before. Determines how long
        to suppress ALL further requests through this RestClient (shared
        by every poller/caller - see __init__ above): computes BOTH (1)
        the standard Retry-After response header, in seconds, and (2)
        Binance's own futures rate-limit ban message, which embeds an
        absolute ban-until Unix ms timestamp as free text - e.g. '...
        banned until 1755000000000...' - whenever each is present/parses
        cleanly, and uses the LATER (safer, longer) of the two if both are
        available. Falls back to a conservative fixed default (60s) only
        if NEITHER is present/parseable, so a 418/429 can never fail to
        arm cooldown at all. Never widens/shortens an already-longer-
        still-active cooldown below what's newly computed - always takes
        the max of current vs newly computed expiry. Resets the one-time
        resume-log flag so note_cooldown_resume_if_needed() will log
        again the next time this (possibly extended) cooldown clears."""
        now = time.time()
        candidates = []

        retry_after = headers.get("Retry-After") if headers else None
        if retry_after:
            try:
                candidates.append(now + float(retry_after))
            except (TypeError, ValueError):
                pass

        msg = data.get("msg", "") if isinstance(data, dict) else ""
        match = re.search(r"banned until (\d{10,})", str(msg))
        if match:
            banned_until_ms = int(match.group(1))
            candidates.append(banned_until_ms / 1000.0)

        if candidates:
            new_until = max(candidates)  # later/safer of the two when both are available
        else:
            new_until = now + 60.0  # conservative default - Binance 418 bans start at 2 minutes and escalate

        self._cooldown_until_ts = max(self._cooldown_until_ts, new_until)
        self._cooldown_resume_logged = False

        if self._cooldown_logged_until_ts != self._cooldown_until_ts:
            self._cooldown_logged_until_ts = self._cooldown_until_ts
            expiry_utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self._cooldown_until_ts))
            print(
                f"\033[31m[rest-cooldown] HTTP {resp_status} ({data.get('msg', data) if isinstance(data, dict) else data}) "
                f"- suppressing ALL REST requests on this client until {expiry_utc} "
                f"({self._cooldown_until_ts - now:.0f}s).\033[0m"
            )

    def is_cooldown_active(self) -> bool:
        """True while a shared 418/429 cooldown (see _arm_cooldown above)
        is still in effect. Read-only - callers (retry_with_backoff(),
        pollers) use this to decide whether to skip a REST call entirely
        rather than let _request() raise for them."""
        return bool(self._cooldown_until_ts and time.time() < self._cooldown_until_ts)

    def cooldown_remaining(self) -> float:
        """Seconds until the shared cooldown clears (0.0 if none active)."""
        return max(0.0, self._cooldown_until_ts - time.time())

    def cooldown_expiry_utc_str(self) -> str:
        """Human-readable UTC expiry of the current cooldown window, for
        logging (see retry_with_backoff() in dca2.py)."""
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self._cooldown_until_ts))

    def note_cooldown_resume_if_needed(self) -> None:
        """Logs one 'resuming REST requests' line exactly once per
        cooldown window, the first time ANY caller notices the cooldown
        has cleared after being active - safe to call from multiple
        concurrent pollers/retry_with_backoff without duplicate lines.
        No-op if no cooldown has ever been armed, or the resume for the
        current window was already logged."""
        if self._cooldown_until_ts and not self.is_cooldown_active() and not self._cooldown_resume_logged:
            self._cooldown_resume_logged = True
            print(f"\033[32m[rest-cooldown] cooldown expired - resuming normal REST requests.\033[0m")

    async def wait_out_cooldown_silently(self, jitter_max: float = 3.0) -> None:
        """2026-08 HTTP 418/429 global cooldown fix - thundering-herd
        prevention. If a shared cooldown is currently active, sleeps
        until it clears plus a small random jitter (so every poller
        doesn't all wake up and hit Binance on the exact same tick), then
        returns - logging nothing itself (see _arm_cooldown's one-time
        start log and note_cooldown_resume_if_needed's one-time resume
        log for the only cooldown-related output callers should expect).
        No-op (returns immediately, no sleep) if no cooldown is active."""
        if not self.is_cooldown_active():
            self.note_cooldown_resume_if_needed()
            return
        remaining = self.cooldown_remaining()
        await asyncio.sleep(remaining + random.uniform(0.1, jitter_max))
        self.note_cooldown_resume_if_needed()

    async def _request(
        self, method: str, path: str, params: Optional[dict] = None, signed: bool = False
    ) -> dict:
        # 2026-08 HTTP 418/429 global cooldown fix (this check only - every
        # other line in this method is unchanged): while a cooldown armed
        # by _arm_cooldown() below is still active, refuse to send ANY
        # request (startup or poller, signed or not) rather than adding to
        # an active IP ban. Raises the same BinanceApiError type every
        # existing caller already catches (BinanceApiError /
        # aiohttp.ClientError / asyncio.TimeoutError), so no call site
        # needs to change - this never blindly retries an order whose
        # execution result is ambiguous, it simply never sends the retry
        # at all until cooldown clears.
        if self._cooldown_until_ts and time.time() < self._cooldown_until_ts:
            remaining = self._cooldown_until_ts - time.time()
            raise BinanceApiError(
                429, {"code": -1003, "msg": f"local REST cooldown active for another {remaining:.0f}s"},
            )

        params = params or {}
        if signed:
            params = self._sign(params)
        url = f"{self.base_url}{path}"
        async with self.session.request(
            method, url, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {"raw": text}
            if resp.status in (418, 429):
                self._arm_cooldown(resp.status, resp.headers, data)
            if resp.status != 200:
                raise BinanceApiError(resp.status, data, headers=dict(resp.headers))
            return data

    # --- public endpoints ---------------------------------------------------
    async def get_exchange_info(self) -> dict:
        return await self._request("GET", "/fapi/v1/exchangeInfo")

    async def get_book_ticker(self, symbol: str) -> dict:
        return await self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    async def get_premium_index(self, symbol: str) -> dict:
        """Mark price + current funding rate. Best-effort feature source."""
        return await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    async def get_open_interest(self, symbol: str) -> dict:
        """Current open interest. Best-effort feature source."""
        return await self._request("GET", "/fapi/v1/openInterest", {"symbol": symbol})

    # --- signed account endpoints -------------------------------------------
    async def get_balance(self) -> list:
        return await self._request("GET", "/fapi/v2/balance", signed=True)

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True
        )

    async def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        try:
            return await self._request(
                "POST", "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type}, signed=True,
            )
        except BinanceApiError as e:
            if e.code == -4046:
                return {"msg": "already set"}
            raise

    async def get_position_risk(self, symbol: str) -> list:
        return await self._request(
            "GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True
        )

    async def get_user_trades(
        self, symbol: str, from_id: Optional[int] = None,
        start_time_ms: Optional[int] = None, limit: int = 1000,
        order_id: Optional[int] = None,
    ) -> list:
        """Actual executed fills for `symbol` (Binance's own account trade
        history - the source of truth for what really happened, independent
        of whatever the local process's in-memory state or the user-data
        websocket stream did or didn't see). Read-only; used by the
        trade-log reconciliation safety net, and (new) by the missed-fill
        REST recovery path in trading.py's
        MartingaleManager._resolve_pending_order_via_rest(), which passes
        `order_id` to fetch only this exact order's own fill(s) (accurate
        realizedPnl/commission for that one order, not a wider window).
        `order_id`, `from_id`, and `start_time_ms` are mutually exclusive
        per Binance's API - `order_id` takes priority when given, then
        `from_id` (incremental cursor)."""
        params = {"symbol": symbol, "limit": limit}
        if order_id is not None:
            params["orderId"] = order_id
        elif from_id is not None:
            params["fromId"] = from_id
        elif start_time_ms is not None:
            params["startTime"] = start_time_ms
        return await self._request("GET", "/fapi/v1/userTrades", params, signed=True)

    # --- signed trading endpoints -------------------------------------------
    async def place_order(self, **kwargs) -> dict:
        return await self._request("POST", "/fapi/v1/order", kwargs, signed=True)

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        return await self._request(
            "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True
        )

    async def get_open_orders(self, symbol: str) -> list:
        """Signed GET /fapi/v1/openOrders - all currently OPEN orders for
        `symbol` (e.g. a resting STOP_MARKET protective stop). 2026-08
        exchange-native protective-stop fix: used by trading.py's startup
        reconciliation to discover/adopt an already-placed protective stop
        after a restart, and to detect an OPEN position with no protective
        order resting on the exchange (see PROTECTION_PENDING handling).
        Docs: https://developers.binance.com/en/docs/derivatives/usds-margined-futures/trade/rest-api
        """
        return await self._request(
            "GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True
        )

    # --- Algo (conditional) order endpoints ---------------------------------
    # 2026-08 Binance Algo-Service migration (root cause of the live -4120
    # failures): Binance moved USD-M CONDITIONAL order types (STOP_MARKET,
    # TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET, ...) off /fapi/v1/order onto a
    # dedicated Algo Service. Sending type=STOP_MARKET to /fapi/v1/order now
    # returns:
    #     HTTP 400 {"code": -4120, "msg": "Order type not supported for this
    #               endpoint. Please use the Algo Order API endpoints instead."}
    # These four methods are the Algo equivalents, used ONLY by the
    # exchange-native protective stop in trading.py. Ordinary MARKET entries,
    # DCA additions and reduceOnly closes deliberately keep using
    # place_order()/cancel_order()/get_order()/get_open_orders() above -
    # MARKET is not a conditional type and is unaffected by the migration.
    #
    # Field-name differences that matter (verified against the docs):
    #   triggerPrice   (NOT stopPrice)
    #   clientAlgoId   (NOT newClientOrderId)
    #   algoId         (NOT orderId) - the response identifier
    #   orderType      (NOT type)    - the type field in RESPONSES
    #   actualOrderId  - "" until the algo triggers, then the child order's id
    # Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
    async def place_algo_order(self, **kwargs) -> dict:
        """Signed POST /fapi/v1/algoOrder - places a conditional (algo)
        order. Caller supplies Binance's exact field names, e.g.:
            symbol, side, algoType="CONDITIONAL", type="STOP_MARKET",
            triggerPrice, closePosition="true", workingType, clientAlgoId
        Never send `quantity` or `reduceOnly` together with
        closePosition="true" - Binance rejects that combination."""
        return await self._request("POST", "/fapi/v1/algoOrder", kwargs, signed=True)

    async def get_algo_order(
        self, algo_id: Optional[int] = None, client_algo_id: Optional[str] = None
    ) -> dict:
        """Signed GET /fapi/v1/algoOrder - query ONE algo order's real
        current state. Exactly one of algo_id / client_algo_id must be
        given (Binance requires one; sending neither is an error). This is
        the authoritative fallback whenever an ALGO_UPDATE websocket event
        is missed or ambiguous - notably FINISHED, which per Binance's own
        docs can mean either filled OR canceled, so it must never be
        treated as a fill without checking `algoStatus`/`actualOrderId`
        here first.
        Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Query-Algo-Order
        """
        if algo_id is None and client_algo_id is None:
            raise ValueError("get_algo_order requires algo_id or client_algo_id")
        params: dict = {}
        if algo_id is not None:
            params["algoId"] = algo_id
        else:
            params["clientAlgoId"] = client_algo_id
        return await self._request("GET", "/fapi/v1/algoOrder", params, signed=True)

    async def cancel_algo_order(
        self, algo_id: Optional[int] = None, client_algo_id: Optional[str] = None
    ) -> dict:
        """Signed DELETE /fapi/v1/algoOrder - cancel ONE algo order by
        algoId or clientAlgoId (exactly one required). Note Binance does
        NOT take `symbol` here - the algo id alone identifies the order.
        Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Query-Algo-Order
        """
        if algo_id is None and client_algo_id is None:
            raise ValueError("cancel_algo_order requires algo_id or client_algo_id")
        params: dict = {}
        if algo_id is not None:
            params["algoId"] = algo_id
        else:
            params["clientAlgoId"] = client_algo_id
        return await self._request("DELETE", "/fapi/v1/algoOrder", params, signed=True)

    async def get_open_algo_orders(self, symbol: Optional[str] = None) -> list:
        """Signed GET /fapi/v1/openAlgoOrders - every currently-open algo
        order (optionally filtered to one symbol; omitting symbol returns
        all symbols and costs far more request weight, so trading.py always
        passes one). Used by protective-stop reconciliation to discover,
        adopt, de-duplicate and clean up bot-owned conditional stops.
        Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Current-All-Algo-Open-Orders
        """
        params: dict = {}
        if symbol is not None:
            params["symbol"] = symbol
        return await self._request("GET", "/fapi/v1/openAlgoOrders", params, signed=True)

    async def get_order(self, symbol: str, order_id: int) -> dict:
        """Signed GET /fapi/v1/order - query a single order's current
        status by orderId. REST fallback used when a fill's WebSocket
        ORDER_TRADE_UPDATE event is missed (dropped/decommissioned
        user-data stream, reconnect race, etc.) so pending local state can
        be resolved deterministically from Binance's own order record
        instead of being inferred from a position-snapshot mismatch.
        Docs: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade
        """
        return await self._request(
            "GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True
        )

    # --- user data stream ----------------------------------------------------
    async def create_listen_key(self) -> str:
        data = await self._request("POST", "/fapi/v1/listenKey")
        return data["listenKey"]

    async def keepalive_listen_key(self) -> None:
        await self._request("PUT", "/fapi/v1/listenKey")


# ============================================================================
# SYMBOL FILTERS (tick size / step size / min notional)
# ============================================================================


@dataclass
class SymbolFilters:
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float


async def fetch_symbol_filters(client: RestClient, symbol: str) -> SymbolFilters:
    info = await client.get_exchange_info()
    sym_info = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
    if sym_info is None:
        raise SystemExit(f"Symbol {symbol} not found in exchangeInfo response.")

    tick_size = step_size = min_qty = 0.0
    min_notional = 0.0
    for f in sym_info["filters"]:
        if f["filterType"] == "PRICE_FILTER":
            tick_size = float(f["tickSize"])
        elif f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
            min_qty = float(f["minQty"])
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f.get("notional", 0.0))

    return SymbolFilters(
        tick_size=tick_size, step_size=step_size, min_qty=min_qty, min_notional=min_notional
    )
