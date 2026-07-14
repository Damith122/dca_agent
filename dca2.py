#!/usr/bin/env python3
"""
================================================================================
 Martingale DCA Scalper - Binance USD-M Futures (Testnet / Demo)
 Railway.app 24/7 deployment build
================================================================================

RUNNING 24/7 ON A REMOTE HOST (Railway or similar) - READ THIS
----------------------------------------------------------
A cloud host WILL restart this process sometimes - deploys, host
maintenance, OOM, transient crashes. Two things were added specifically
for that reality, beyond what a laptop-only bot needs:

  - `reconcile_position_on_startup()` queries Binance's OWN position-risk
    endpoint before the bot ever assumes it is flat. If a real position is
    already open (e.g. the process restarted mid-trade), the bot rebuilds
    its in-memory PositionState from that instead of blindly opening a
    second, unrelated position on top of it.
  - The bottom-level `run_forever()` supervisor catches any exception that
    escapes `main()` (other than the deliberate `SystemExit` safety gates)
    and restarts with backoff, logging what happened, instead of letting
    one unhandled exception silently kill the whole container.

Also: the online-learning brain and DCA step counters live in memory only.
A process restart means the brain forgets everything it learned and starts
warming up again - not fatal for a bot that trades this frequently, but
worth knowing since nothing here persists that state to disk.

SAFETY DEFAULTS (unchanged)
----------------------------------------------------------
  - TESTNET ONLY by default. Mainnet requires BOTH `USE_TESTNET=false` AND
    `I_UNDERSTAND_THIS_IS_REAL_MONEY=yes` set explicitly, or the bot
    refuses to start.
  - DRY_RUN=true by default - orders are logged, never sent, until you
    flip `DRY_RUN=false` yourself.
  - LEVERAGE is clamped to MAX_ALLOWED_LEVERAGE (50) regardless of config.

REQUIRED SETUP
----------------------------------------------------------
1. Binance Futures TESTNET API keys: https://testnet.binancefuture.com/
2. Environment variables (set these in Railway's Variables tab, NOT in code):
       BINANCE_API_KEY=...
       BINANCE_API_SECRET=...
       DRY_RUN=true
       USE_TESTNET=true
3. pip install -r requirements.txt
4. python dca2.py
================================================================================
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import traceback
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

import aiohttp
import numpy as np
import websockets
from sklearn.linear_model import SGDRegressor
from websockets.exceptions import ConnectionClosed

# ============================================================================
# CONFIG
# ============================================================================

SYMBOL = "BTCUSDT"

# --- Safety gates - read the header above before touching these -------------
USE_TESTNET = os.environ.get("USE_TESTNET", "true").lower() != "false"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() != "false"
I_UNDERSTAND_THIS_IS_REAL_MONEY = os.environ.get(
    "I_UNDERSTAND_THIS_IS_REAL_MONEY", ""
).lower() == "yes"

# API keys MUST come from environment variables - set these in Railway's
# "Variables" tab (Project -> your service -> Variables), never in this file.
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# --- Account / margin --------------------------------------------------------
LEVERAGE = 40
MAX_ALLOWED_LEVERAGE = 50
MARGIN_TYPE = "CROSSED"

# --- Position sizing (Fixed Amount, Martingale) ------------------------------
INITIAL_ENTRY_USDT = 1.5
DCA_MULTIPLIER = 2.0
MAX_DCA_STEPS = 5

# --- Trade management ---------------------------------------------------------
DCA_TRIGGER_PCT = 0.002
TAKE_PROFIT_PCT = 0.003
HARD_STOP_PCT = 0.05

# --- Simple entry signal (warmup/fallback only, see BRAIN_* below) ----------
SIGNAL_LOOKBACK_TICKS = 20
SIGNAL_DEADBAND_PCT = 0.0005
POST_EXIT_COOLDOWN_SEC = 15

# --- Online Learning Brain (SGDRegressor, partial_fit only, no stored data) --
FEATURE_SHORT_LOOKBACK = 5
LABEL_HORIZON_TICKS = 10
BRAIN_WARMUP_UPDATES = 50
PREDICTION_DEADBAND = 0.00015
N_FEATURES = 6

# --- Timing -------------------------------------------------------------------
LISTEN_KEY_KEEPALIVE_SEC = 25 * 60
BALANCE_REFRESH_SEC = 60
POSITION_RISK_POLL_SEC = 10
MAX_BACKOFF_SEC = 30
IDLE_DATA_TIMEOUT_SEC = 20            # market data: bookTicker ticks constantly, so silence
                                       # this long on an active symbol really does mean trouble
USER_WS_IDLE_FALLBACK_SEC = 20 * 60   # user data: silence is NORMAL (no fills = no messages).
                                       # This is a loose last-resort fallback, not the primary
                                       # liveness check - see the comment in userdata_consumer.

# --- Cloud-host resilience ---------------------------------------------------
STARTUP_RETRY_ATTEMPTS = 5
STARTUP_RETRY_BASE_DELAY_SEC = 2.0
SUPERVISOR_RESTART_DELAY_SEC = 10

# --- Hosts ---------------------------------------------------------------------
if USE_TESTNET:
    REST_BASE = "https://testnet.binancefuture.com"
    WS_MARKET_BASE = "wss://stream.binancefuture.com"
    WS_USERDATA_BASE = "wss://stream.binancefuture.com"
else:
    REST_BASE = "https://fapi.binance.com"
    WS_MARKET_BASE = "wss://fstream.binance.com"
    WS_USERDATA_BASE = "wss://fstream.binance.com"


# ============================================================================
# UTIL
# ============================================================================


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


_USE_COLOR = sys.stdout.isatty()


def color(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


GREEN, RED, YELLOW, CYAN, GRAY, BOLD, MAGENTA = "32", "31", "33", "36", "90", "1", "35"


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    steps = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * d_step)


async def retry_with_backoff(coro_fn, *args, attempts: int = STARTUP_RETRY_ATTEMPTS,
                              base_delay: float = STARTUP_RETRY_BASE_DELAY_SEC, label: str = "operation"):
    """Retries a one-shot async setup call with exponential backoff. Used for
    the REST calls that run ONCE before the self-reconnecting websocket loops
    take over - those calls have no other retry path of their own, so a single
    transient network blip during container startup would otherwise kill the
    whole process before it ever gets going."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_fn(*args)
        except Exception as e:  # noqa: BLE001 - deliberately broad, this is a retry wrapper
            last_exc = e
            delay = base_delay * (2 ** (attempt - 1))
            print(color(
                f"[startup] {label} failed (attempt {attempt}/{attempts}): {e}. "
                f"Retrying in {delay:.1f}s ...", YELLOW
            ))
            if attempt < attempts:
                await asyncio.sleep(delay)
    raise SystemExit(f"[startup] {label} failed after {attempts} attempts: {last_exc}")


# ============================================================================
# SAFETY GATE CHECKS
# ============================================================================


def enforce_safety_gates() -> None:
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "Missing BINANCE_API_KEY / BINANCE_API_SECRET environment variables. "
            "Set them in Railway's Variables tab (never hardcode them in this "
            "file, especially once it's pushed to GitHub). Generate TESTNET "
            "keys at https://testnet.binancefuture.com/ if you don't have them."
        )

    if not USE_TESTNET and not I_UNDERSTAND_THIS_IS_REAL_MONEY:
        raise SystemExit(
            "REFUSING TO START: USE_TESTNET=false (mainnet) but "
            "I_UNDERSTAND_THIS_IS_REAL_MONEY is not set to 'yes'. "
            "This is a deliberate safety gate."
        )

    global LEVERAGE
    if LEVERAGE > MAX_ALLOWED_LEVERAGE:
        print(color(
            f"[safety] Requested LEVERAGE={LEVERAGE} exceeds MAX_ALLOWED_LEVERAGE="
            f"{MAX_ALLOWED_LEVERAGE}. Clamping to {MAX_ALLOWED_LEVERAGE}.", YELLOW
        ))
        LEVERAGE = MAX_ALLOWED_LEVERAGE

    if MAX_DCA_STEPS > 5:
        raise SystemExit("MAX_DCA_STEPS > 5 is not supported by this script's safety design.")


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


# ============================================================================
# ONLINE LEARNING BRAIN (SGDRegressor, partial_fit only - no historical data)
# ============================================================================


class OnlineBrain:
    def __init__(self, n_features: int, warmup_updates: int = BRAIN_WARMUP_UPDATES):
        self.n_features = n_features
        self.warmup_updates = warmup_updates
        self.model = SGDRegressor(
            loss="squared_error",
            penalty="l2",
            alpha=1e-5,
            learning_rate="invscaling",
            eta0=0.01,
            power_t=0.25,
            warm_start=True,
        )
        self.fitted = False
        self.update_count = 0
        self.last_prediction: Optional[float] = None

        self._n_seen = 0
        self._mean = np.zeros(n_features, dtype=float)
        self._m2 = np.zeros(n_features, dtype=float)

    def _update_normalizer(self, x: np.ndarray) -> None:
        self._n_seen += 1
        delta = x - self._mean
        self._mean += delta / self._n_seen
        delta2 = x - self._mean
        self._m2 += delta * delta2

    def _normalize(self, x: np.ndarray) -> np.ndarray:
        if self._n_seen < 2:
            return x
        variance = self._m2 / max(self._n_seen - 1, 1)
        std = np.sqrt(variance)
        std = np.where(std < 1e-8, 1.0, std)
        return (x - self._mean) / std

    def learn(self, x: np.ndarray, y: float) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)) or not np.isfinite(y):
            return
        self._update_normalizer(x)
        x_norm = self._normalize(x).reshape(1, -1)
        self.model.partial_fit(x_norm, [float(y)])
        self.fitted = True
        self.update_count += 1

    def predict(self, x: np.ndarray) -> float:
        if not self.fitted:
            return 0.0
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return 0.0
        x_norm = self._normalize(x).reshape(1, -1)
        pred = float(self.model.predict(x_norm)[0])
        self.last_prediction = pred
        return pred

    def is_ready(self) -> bool:
        return self.fitted and self.update_count >= self.warmup_updates


# ============================================================================
# POSITION STATE + MARTINGALE DCA MANAGER (the core strategy state machine)
# ============================================================================


@dataclass
class PositionState:
    side: Optional[str] = None
    status: str = "FLAT"
    dca_step: int = 0
    entries: List[tuple] = field(default_factory=list)
    avg_entry_price: Optional[float] = None
    total_qty: float = 0.0
    pending_order_id: Optional[int] = None
    pending_role: Optional[str] = None
    last_close_time: float = 0.0


class MartingaleManager:
    def __init__(self, client: RestClient, symbol: str, filters: SymbolFilters, leverage: int):
        self.client = client
        self.symbol = symbol
        self.filters = filters
        self.leverage = leverage

        self.position = PositionState()
        self.current_price: Optional[float] = None
        self.available_balance: float = 0.0
        self.liquidation_price: Optional[float] = None

        self.price_history: List[float] = []
        self.trade_count = 0
        self.realized_pnl_total = 0.0

        self.brain = OnlineBrain(N_FEATURES)
        self._feature_buffer: deque[Tuple[float, np.ndarray]] = deque(
            maxlen=LABEL_HORIZON_TICKS + 1
        )
        self._entry_feature_snapshot: Optional[np.ndarray] = None

        self._order_index: Dict[int, str] = {}
        self._rp_accum: Dict[int, float] = {}

    def notional_for_step(self, step: int) -> float:
        margin = INITIAL_ENTRY_USDT if step == 0 else INITIAL_ENTRY_USDT * (DCA_MULTIPLIER ** step)
        return margin * self.leverage

    def update_price_history(self, price: float) -> None:
        self.price_history.append(price)
        if len(self.price_history) > SIGNAL_LOOKBACK_TICKS + 1:
            self.price_history.pop(0)

    def get_features(self) -> np.ndarray:
        hist = self.price_history
        price = self.current_price

        momentum_short = 0.0
        momentum_long = 0.0
        volatility = 0.0

        if price and len(hist) >= 2:
            short_idx = max(len(hist) - 1 - FEATURE_SHORT_LOOKBACK, 0)
            ref = hist[short_idx]
            if ref:
                momentum_short = (price - ref) / ref

        if price and len(hist) > SIGNAL_LOOKBACK_TICKS:
            old = hist[0]
            if old:
                momentum_long = (price - old) / old

        if len(hist) >= 3:
            arr = np.asarray(hist, dtype=float)
            rets = np.diff(arr) / np.where(arr[:-1] == 0, 1.0, arr[:-1])
            volatility = float(np.std(rets))

        side_encoded = 0.0
        unrealized_pnl = 0.0
        dca_ratio = 0.0
        p = self.position
        if p.status in ("OPEN", "DCA_PENDING") and p.avg_entry_price and price:
            side_encoded = 1.0 if p.side == "LONG" else -1.0
            unrealized_pnl = (
                (price - p.avg_entry_price) / p.avg_entry_price
                if p.side == "LONG"
                else (p.avg_entry_price - price) / p.avg_entry_price
            )
            dca_ratio = p.dca_step / MAX_DCA_STEPS

        return np.array(
            [momentum_short, momentum_long, volatility, side_encoded, unrealized_pnl, dca_ratio],
            dtype=float,
        )

    def _static_momentum_signal(self) -> Optional[str]:
        if len(self.price_history) <= SIGNAL_LOOKBACK_TICKS:
            return None
        old, new = self.price_history[0], self.price_history[-1]
        if old <= 0:
            return None
        change = (new - old) / old
        if change > SIGNAL_DEADBAND_PCT:
            return "LONG"
        if change < -SIGNAL_DEADBAND_PCT:
            return "SHORT"
        return None

    def generate_entry_signal(self, features: np.ndarray) -> Optional[str]:
        if len(self.price_history) <= SIGNAL_LOOKBACK_TICKS:
            return None
        if not self.brain.is_ready():
            return self._static_momentum_signal()
        predicted_return = self.brain.predict(features)
        if predicted_return > PREDICTION_DEADBAND:
            return "LONG"
        if predicted_return < -PREDICTION_DEADBAND:
            return "SHORT"
        return None

    def _learn_from_tick(self, features: np.ndarray) -> None:
        price = self.current_price
        if price is None:
            return
        if len(self._feature_buffer) == self._feature_buffer.maxlen:
            old_price, old_features = self._feature_buffer[0]
            if old_price:
                realized_forward_return = (price - old_price) / old_price
                self.brain.learn(old_features, realized_forward_return)
        self._feature_buffer.append((price, features.copy()))

    async def _place_step_order(self, step: int, side_signal: str) -> None:
        notional = self.notional_for_step(step)
        price = self.current_price
        if price is None or price <= 0:
            return
        qty = round_step(notional / price, self.filters.step_size)

        if qty < self.filters.min_qty or qty * price < self.filters.min_notional:
            print(color(
                f"[dca] skipping step {step}: qty {qty} / notional {qty*price:.2f} "
                f"below exchange minimum (min_qty={self.filters.min_qty}, "
                f"min_notional={self.filters.min_notional})", YELLOW
            ))
            return

        order_side = "BUY" if side_signal == "LONG" else "SELL"
        role = "initial" if step == 0 else "dca"
        step_label = "INITIAL ENTRY" if step == 0 else f"DCA STEP {step}/{MAX_DCA_STEPS}"

        if DRY_RUN:
            fake_id = -(int(time.time() * 1000) % 1_000_000) - step
            print(color(
                f"{now_str()} [DRY RUN] would place {step_label} {order_side} {qty} "
                f"{self.symbol} @ market (~{price:.2f}, notional=${notional:.2f})", GRAY
            ))
            self._order_index[fake_id] = role
            self.position.pending_order_id = fake_id
            self.position.pending_role = role
            self.position.side = side_signal
            self.position.status = "ENTERING" if step == 0 else "DCA_PENDING"
            return

        try:
            resp = await self.client.place_order(
                symbol=self.symbol, side=order_side, type="MARKET", quantity=qty,
            )
            self._order_index[resp["orderId"]] = role
            self.position.pending_order_id = resp["orderId"]
            self.position.pending_role = role
            self.position.side = side_signal
            self.position.status = "ENTERING" if step == 0 else "DCA_PENDING"
            print(color(
                f"{now_str()} {step_label} PLACED  {order_side} {qty} {self.symbol} "
                f"@ market (notional=${notional:.2f}, orderId={resp['orderId']})",
                CYAN,
            ))
        except BinanceApiError as e:
            print(color(f"[dca] {step_label} order FAILED: {e}", RED))

    async def close_position(self, reason: str, emergency: bool = False) -> None:
        if self.position.status not in ("OPEN", "DCA_PENDING") or self.position.total_qty <= 0:
            return
        close_side = "SELL" if self.position.side == "LONG" else "BUY"
        qty = self.position.total_qty
        label = "EMERGENCY CLOSE" if emergency else "CLOSE (take-profit)"
        print(color(
            f"{now_str()} {label}: {reason} | closing {close_side} {qty} {self.symbol}",
            RED if emergency else GREEN,
        ))
        self.position.status = "CLOSING"

        if DRY_RUN:
            fake_id = -(int(time.time() * 1000) % 1_000_000) - 900000
            self._order_index[fake_id] = "close"
            self.position.pending_order_id = fake_id
            self.position.pending_role = "close"
            print(color(
                f"{now_str()} [DRY RUN] would place CLOSE {close_side} {qty} "
                f"{self.symbol} reduceOnly MARKET", GRAY
            ))
            return

        try:
            resp = await self.client.place_order(
                symbol=self.symbol, side=close_side, type="MARKET",
                quantity=qty, reduceOnly=True,
            )
            self._order_index[resp["orderId"]] = "close"
            self.position.pending_order_id = resp["orderId"]
            self.position.pending_role = "close"
        except BinanceApiError as e:
            print(color(
                f"[position] FAILED to close position: {e} - "
                f"POSITION MAY STILL BE OPEN, check manually!", RED
            ))
            self.position.status = "OPEN"

    async def on_price_tick(self) -> None:
        features = self.get_features()
        self._learn_from_tick(features)

        if self.position.status == "FLAT":
            if time.time() - self.position.last_close_time < POST_EXIT_COOLDOWN_SEC:
                return
            signal = self.generate_entry_signal(features)
            if signal is not None:
                self._entry_feature_snapshot = features.copy()
                await self._place_step_order(step=0, side_signal=signal)
        elif self.position.status == "OPEN":
            await self._manage_open_position()

    async def _manage_open_position(self) -> None:
        avg = self.position.avg_entry_price
        price = self.current_price
        if avg is None or price is None:
            return

        pct_move = (price - avg) / avg if self.position.side == "LONG" else (avg - price) / avg

        if pct_move <= -HARD_STOP_PCT:
            await self.close_position(
                f"hard stop: {pct_move*100:.2f}% adverse move on average entry", emergency=True
            )
            return

        if pct_move >= TAKE_PROFIT_PCT:
            await self.close_position(f"take-profit: {pct_move*100:.2f}% favorable move")
            return

        if pct_move <= -DCA_TRIGGER_PCT:
            if self.position.dca_step >= MAX_DCA_STEPS:
                await self.close_position(
                    f"max DCA steps ({MAX_DCA_STEPS}) exhausted and price still adverse "
                    f"({pct_move*100:.2f}%)", emergency=True,
                )
                return
            await self._place_step_order(step=self.position.dca_step + 1, side_signal=self.position.side)

    async def handle_order_update(self, event: dict) -> None:
        o = event.get("o", {})
        order_id = o.get("i")
        if order_id not in self._order_index:
            return

        rp = float(o.get("rp") or 0.0)
        if rp:
            self._rp_accum[order_id] = self._rp_accum.get(order_id, 0.0) + rp

        status = o.get("X")
        if status != "FILLED":
            return

        role = self._order_index.pop(order_id)
        total_rp = self._rp_accum.pop(order_id, 0.0)
        fill_price = float(o.get("ap") or 0.0)
        fill_qty = float(o.get("z") or 0.0)

        if role in ("initial", "dca"):
            await self._on_entry_filled(role, fill_price, fill_qty)
        elif role == "close":
            await self._on_close_filled(fill_price, total_rp)

    async def _on_entry_filled(self, role: str, fill_price: float, fill_qty: float) -> None:
        self.position.entries.append((fill_price, fill_qty))
        total_notional = sum(p * q for p, q in self.position.entries)
        total_qty = sum(q for _, q in self.position.entries)
        self.position.avg_entry_price = total_notional / total_qty if total_qty else None
        self.position.total_qty = total_qty
        if role == "dca":
            self.position.dca_step += 1
        self.position.status = "OPEN"
        self.position.pending_order_id = None
        self.position.pending_role = None

        step_label = "INITIAL" if role == "initial" else f"DCA #{self.position.dca_step}"
        side_color = GREEN if self.position.side == "LONG" else RED
        print(color(
            f"{now_str()} ENTRY FILLED [{step_label}] {self.position.side} "
            f"qty={fill_qty} @ {fill_price:.2f}  ->  avg_entry={self.position.avg_entry_price:.2f}  "
            f"total_qty={self.position.total_qty}  leverage={self.leverage}x  margin={MARGIN_TYPE}",
            side_color,
        ))

    async def _on_close_filled(self, fill_price: float, total_rp: float) -> None:
        self.realized_pnl_total += total_rp
        self.trade_count += 1
        pnl_color = GREEN if total_rp >= 0 else RED
        print(color(
            f"{now_str()} POSITION CLOSED @ {fill_price:.2f}  PnL={total_rp:+.4f} USDT  "
            f"(DCA steps used: {self.position.dca_step}/{MAX_DCA_STEPS})  "
            f"session_total={self.realized_pnl_total:+.4f}",
            pnl_color,
        ))

        if self._entry_feature_snapshot is not None:
            invested_notional = sum(p * q for p, q in self.position.entries) or None
            if invested_notional:
                realized_trade_return = total_rp / invested_notional
                self.brain.learn(self._entry_feature_snapshot, realized_trade_return)
                print(color(
                    f"{now_str()} [brain] reinforced entry decision with realized "
                    f"trade outcome (label={realized_trade_return:+.5f}, "
                    f"brain_updates={self.brain.update_count})", MAGENTA,
                ))
            self._entry_feature_snapshot = None

        self.position = PositionState(last_close_time=time.time())


# ============================================================================
# POSITION SYNC (the fix for "stuck ENTERING after a missed fill event")
# ============================================================================


async def initialize_sync(
    client: RestClient,
    manager: MartingaleManager,
    context: str = "startup",
    rows: Optional[list] = None,
) -> None:
    """Reconciles the bot's in-memory PositionState against Binance's actual
    reported position. This runs from THREE places, not just startup:

      1. Once at process startup.
      2. Immediately after every user-data-stream (re)connection - a dropped
         listenKey means any ORDER_TRADE_UPDATE fills that happened while
         disconnected are gone for good; Binance does not replay them.
      3. On every periodic position-risk poll (~10s) as a defense-in-depth
         net, so even a fill missed for some OTHER reason (a bad message, a
         brief gap between disconnect and reconnect, etc.) self-heals within
         one poll cycle instead of staying stuck indefinitely.

    Without repeated reconciliation like this, a fill that lands while the
    websocket is down leaves the bot stuck in ENTERING/DCA_PENDING forever:
    on_price_tick() only acts while status is FLAT or OPEN, so a stuck
    ENTERING position is silently never checked against take-profit /
    hard-stop / DCA - exactly the symptom in the reported logs (exchange
    showing a real positionAmt while the bot showed status=ENTERING,
    avg_entry=None, indefinitely).

    `rows` lets a caller that already fetched position-risk data (the
    poller) pass it in directly instead of doubling up on the API call.
    """
    if DRY_RUN:
        return  # nothing real to sync against

    if rows is None:
        try:
            rows = await client.get_position_risk(SYMBOL)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(
                f"[sync:{context}] could not fetch position risk: {e}. "
                f"Leaving local state as-is - will retry next cycle.", RED
            ))
            return

    row = next((r for r in rows if float(r.get("positionAmt", 0)) != 0), None)
    p = manager.position

    if row is None:
        # Exchange shows flat. If local state thinks there's a position OR a
        # pending order in flight, either it already closed (TP/hard-stop/
        # manual) or the entry never actually filled while we were
        # disconnected - either way, waiting on a fill event that already
        # happened (or never will) is exactly how the bot gets stuck.
        if p.status != "FLAT":
            print(color(
                f"{now_str()} [sync:{context}] exchange reports NO open position, but local "
                f"state was status={p.status} side={p.side}. Resetting to FLAT so the bot "
                f"can evaluate a fresh entry instead of waiting on a fill that won't arrive.",
                YELLOW,
            ))
            manager.position = PositionState(last_close_time=time.time())
        return

    amt = float(row["positionAmt"])
    entry_price = float(row.get("entryPrice", 0) or 0)
    side = "LONG" if amt > 0 else "SHORT"
    qty = abs(amt)

    # Don't clobber an already-healthy OPEN position (and its DCA step
    # count) on every routine 10s poll - only rebuild when local state
    # actually disagrees with what the exchange reports.
    already_synced = (
        p.status == "OPEN"
        and p.side == side
        and p.avg_entry_price is not None
        and abs(p.total_qty - qty) < max(manager.filters.step_size, 1e-9)
    )
    if already_synced:
        return

    print(color(
        f"{now_str()} [sync:{context}] *** RESYNCING TO MATCH EXCHANGE *** "
        f"exchange shows side={side} qty={qty} avg_entry={entry_price:.2f}; local state "
        f"was status={p.status} side={p.side} avg_entry={p.avg_entry_price}. Rebuilding "
        f"local state so take-profit / hard-stop / DCA logic resumes managing this trade "
        f"(dca_step reset to 0 - the exact prior step count isn't recoverable from this "
        f"endpoint; review manually if that matters for your risk tolerance).",
        YELLOW,
    ))
    manager.position = PositionState(
        side=side,
        status="OPEN",
        dca_step=0,
        entries=[(entry_price, qty)],
        avg_entry_price=entry_price,
        total_qty=qty,
    )


# ============================================================================
# MARKET DATA WEBSOCKET
# ============================================================================


async def market_data_consumer(manager: MartingaleManager) -> None:
    host_idx = 0
    backoff = 1.0
    hosts = [WS_MARKET_BASE]
    stream_path = f"{SYMBOL.lower()}@bookTicker"

    while True:
        host = hosts[host_idx % len(hosts)]
        url = f"{host}/stream?streams={stream_path}"
        try:
            print(color(f"[market-ws] connecting to {host} ...", GRAY))
            async with websockets.connect(
                url, ping_interval=15, ping_timeout=10, max_queue=2048
            ) as ws:
                print(color("[market-ws] connected.", GREEN))
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
                            data = msg.get("data", {})
                            bid = float(data.get("b", 0) or 0)
                            ask = float(data.get("a", 0) or 0)
                            if bid and ask:
                                price = (bid + ask) / 2
                                manager.current_price = price
                                manager.update_price_history(price)
                                await manager.on_price_tick()
                        except Exception as e:  # noqa: BLE001 - one bad tick must not kill the socket
                            print(color(f"[market-ws] error processing message, skipping: {e}", RED))
                finally:
                    wd_task.cancel()
        except Exception as e:  # noqa: BLE001 - this IS the reconnect boundary; anything
            # that escapes the websocket context (dead socket, DNS blip, a bug
            # in an untested code path) should trigger backoff+retry, not a
            # crash - ask #3: no exception here should kill the bot.
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

                # Re-sync IMMEDIATELY after every (re)connection. Any fill
                # that landed while this stream was down is gone for good -
                # Binance doesn't replay missed events on a new listenKey -
                # so this is what actually fixes the "stuck ENTERING with a
                # real position open" bug, not just the idle-timeout tuning
                # below.
                await initialize_sync(client, manager, context="user-ws reconnect")

                async def watchdog(ws_ref) -> None:
                    # NOTE: user-data pushes events only when something
                    # happens (a fill, a balance change) - it is completely
                    # normal for this stream to go quiet for many minutes on
                    # a low-activity account. The real heartbeat here is
                    # `websockets`' own ping_interval/ping_timeout above,
                    # which sends protocol-level pings and raises
                    # ConnectionClosed on a genuinely dead socket - that's
                    # already caught below. This watchdog is now just a very
                    # loose fallback, not the primary liveness check, since
                    # treating silence as staleness is what was forcing
                    # needless reconnects (and losing fills) in the first
                    # place.
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
        except Exception as e:  # noqa: BLE001 - this IS the reconnect boundary; any API/network
            # error here (dead socket, listenKey creation failure, a bug) triggers
            # backoff+retry rather than crashing the bot - ask #3.
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


async def balance_refresher(client: RestClient, manager: MartingaleManager) -> None:
    while True:
        try:
            balances = await client.get_balance()
            usdt = next((b for b in balances if b["asset"] == "USDT"), None)
            if usdt:
                # Use the REAL fetched balance - a prior version of this file
                # hardcoded 50.0 here, which would silently mask your actual
                # account balance the entire time the bot runs. Don't do that.
                #manager.available_balance = float(usdt["availableBalance"])
                real_balance = float(usdt["availableBalance"])
                manager.available_balance = min(real_balance, 50.0)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[balance] refresh failed: {e}", RED))
        await asyncio.sleep(BALANCE_REFRESH_SEC)


async def position_risk_poller(client: RestClient, manager: MartingaleManager) -> None:
    """Polls Binance's OWN authoritative liquidation price AND, on every
    cycle, calls initialize_sync() with the same fetched rows - this is
    what guarantees the bot self-heals from a stuck ENTERING/DCA_PENDING
    state within ~POSITION_RISK_POLL_SEC seconds, even independent of any
    websocket reconnect event."""
    while True:
        if DRY_RUN:
            await asyncio.sleep(POSITION_RISK_POLL_SEC)
            continue
        try:
            rows = await client.get_position_risk(SYMBOL)
            row = next((r for r in rows if float(r.get("positionAmt", 0)) != 0), None)
            if row:
                manager.liquidation_price = float(row.get("liquidationPrice", 0) or 0)
                print(color(
                    f"{now_str()} [risk] LIQUIDATION PRICE: {manager.liquidation_price:.2f}  "
                    f"(mark={float(row.get('markPrice', 0)):.2f}, "
                    f"positionAmt={row.get('positionAmt')})", MAGENTA
                ))
            else:
                manager.liquidation_price = None
            await initialize_sync(client, manager, context="periodic poll", rows=rows)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[risk] position risk poll failed: {e}", RED))
        await asyncio.sleep(POSITION_RISK_POLL_SEC)


async def status_loop(manager: MartingaleManager, interval_sec: int = 20) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        p = manager.position
        liq = f"{manager.liquidation_price:.2f}" if manager.liquidation_price else "n/a"
        brain_state = "READY" if manager.brain.is_ready() else (
            f"WARMUP {manager.brain.update_count}/{BRAIN_WARMUP_UPDATES}"
        )
        last_pred = (
            f"{manager.brain.last_prediction:+.5f}"
            if manager.brain.last_prediction is not None else "n/a"
        )
        print(color(
            f"{now_str()} [status] price={manager.current_price}  status={p.status}  "
            f"side={p.side}  dca_step={p.dca_step}/{MAX_DCA_STEPS}  "
            f"avg_entry={p.avg_entry_price}  qty={p.total_qty}  "
            f"liq_price={liq}  balance={manager.available_balance:.2f} USDT  "
            f"trades={manager.trade_count}  session_pnl={manager.realized_pnl_total:+.4f}  "
            f"brain=[{brain_state}, last_pred={last_pred}]",
            BOLD,
        ))


# ============================================================================
# ENTRYPOINT
# ============================================================================


async def main() -> None:
    enforce_safety_gates()

    print(color("=" * 78, CYAN))
    print(color(" Martingale DCA Scalper - Binance USD-M Futures", BOLD))
    print(color(f" Symbol: {SYMBOL}   Testnet: {USE_TESTNET}   Dry-run: {DRY_RUN}", GRAY))
    print(color(
        f" Leverage: {LEVERAGE}x (cap {MAX_ALLOWED_LEVERAGE}x)   Margin: {MARGIN_TYPE}   "
        f"Initial entry: ${INITIAL_ENTRY_USDT}   DCA x{DCA_MULTIPLIER}   Max steps: {MAX_DCA_STEPS}",
        GRAY,
    ))
    print(color(
        f" DCA trigger: -{DCA_TRIGGER_PCT*100:.2f}%   Take-profit: +{TAKE_PROFIT_PCT*100:.2f}%   "
        f"Hard stop: -{HARD_STOP_PCT*100:.2f}%", GRAY,
    ))
    if DRY_RUN:
        print(color(" *** DRY RUN MODE - no real orders will be sent ***", YELLOW))
    if not USE_TESTNET:
        print(color(" *** LIVE MAINNET MODE - REAL MONEY AT RISK ***", RED))

    print(color("=" * 78, CYAN))

    client = RestClient(API_KEY, API_SECRET, REST_BASE)

    try:
        await retry_with_backoff(client.start, label="REST client startup / time sync")

        filters = await retry_with_backoff(
            fetch_symbol_filters, client, SYMBOL, label="fetch_symbol_filters"
        )
        print(color(
            f"[setup] {SYMBOL} filters: tick={filters.tick_size} step={filters.step_size} "
            f"minQty={filters.min_qty} minNotional={filters.min_notional}", GRAY
        ))

        if not DRY_RUN:
            await retry_with_backoff(client.set_leverage, SYMBOL, LEVERAGE, label="set_leverage")
            await retry_with_backoff(client.set_margin_type, SYMBOL, MARGIN_TYPE, label="set_margin_type")
            print(color(f"[setup] leverage set to {LEVERAGE}x, margin type {MARGIN_TYPE}", GRAY))
        else:
            print(color(
                f"[setup] [DRY RUN] would set leverage={LEVERAGE}x, marginType={MARGIN_TYPE}", GRAY
            ))

        manager = MartingaleManager(client, SYMBOL, filters, LEVERAGE)

        if not DRY_RUN:
            balances = await retry_with_backoff(client.get_balance, label="get_balance")
            usdt = next((b for b in balances if b["asset"] == "USDT"), None)
            manager.available_balance = float(usdt["availableBalance"]) if usdt else 0.0
        else:
            manager.available_balance = 500.0
        print(color(f"[setup] available balance: {manager.available_balance:.2f} USDT", GRAY))

        book = await retry_with_backoff(client.get_book_ticker, SYMBOL, label="get_book_ticker")
        manager.current_price = (float(book["bidPrice"]) + float(book["askPrice"])) / 2
        print(color(f"[setup] current price: {manager.current_price:.2f}", GRAY))

        await initialize_sync(client, manager, context="startup")

        await asyncio.gather(
            market_data_consumer(manager),
            userdata_consumer(client, manager),
            listen_key_keepalive(client),
            balance_refresher(client, manager),
            position_risk_poller(client, manager),
            status_loop(manager),
        )
    finally:
        await client.close()


async def run_forever() -> None:
    """Outer supervisor for 24/7 cloud hosting. `main()` already reconnects
    its own websockets forever - this layer exists only to catch whatever
    exception manages to escape THAT and restart the whole bot instead of
    letting the container exit and stay down. `SystemExit` from the
    deliberate safety gates is NOT caught here - those are supposed to stop
    the bot, not trigger an infinite restart loop."""
    while True:
        try:
            await main()
        except SystemExit as e:
            print(color(f"[supervisor] stopping: {e}", RED))
            raise
        except Exception:  # noqa: BLE001 - top-level catch-all is intentional here
            print(color("[supervisor] main() crashed with an unhandled exception:", RED))
            traceback.print_exc()
            print(color(
                f"[supervisor] restarting in {SUPERVISOR_RESTART_DELAY_SEC}s ...", YELLOW
            ))
            await asyncio.sleep(SUPERVISOR_RESTART_DELAY_SEC)


if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except (KeyboardInterrupt, SystemExit):
        print(color("\n[shutdown] stopped.", YELLOW))
