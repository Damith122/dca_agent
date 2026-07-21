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

import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import aiohttp

# ============================================================================
# REST CLIENT (signed requests, HMAC-SHA256)
# ============================================================================


class BinanceApiError(Exception):
    def __init__(self, status: int, data: dict):
        self.status = status
        self.data = data
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

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(resolver=aiohttp.resolver.ThreadedResolver())
        self.session = aiohttp.ClientSession(
            connector=connector, headers={"X-MBX-APIKEY": self.api_key}
        )
        await self._sync_server_time()

    async def close(self) -> None:
        if self.session:
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

    async def _request(
        self, method: str, path: str, params: Optional[dict] = None, signed: bool = False
    ) -> dict:
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
            if resp.status != 200:
                raise BinanceApiError(resp.status, data)
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
    ) -> list:
        """Actual executed fills for `symbol` (Binance's own account trade
        history - the source of truth for what really happened, independent
        of whatever the local process's in-memory state or the user-data
        websocket stream did or didn't see). Read-only; used only by the
        trade-log reconciliation safety net, never by the live strategy.
        `from_id` and `start_time_ms` are mutually exclusive per Binance's
        API - `from_id` (incremental cursor) takes priority when both are
        given."""
        params = {"symbol": symbol, "limit": limit}
        if from_id is not None:
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
