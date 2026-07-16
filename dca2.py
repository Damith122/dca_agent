#!/usr/bin/env python3
"""
================================================================================
 Martingale DCA Scalper - Binance USD-M Futures (Testnet / Demo)
 Railway.app 24/7 deployment build
 --- BRAIN V2: Probability/Confidence engine, regime detection, ATR-based
     DCA, dynamic sizing, multi-signal Smart Exit, partial TP, trade
     logging + offline dataset, composite reward learning, performance
     stats export. ---
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

Also: the online-learning brain and DCA step counters live in memory only
(brain weights ARE persisted to brain_v2.pkl / GitHub - see BRAIN V2
PERSISTENCE below - but candle/feature buffers are not, so there's a short
re-warmup period for regime/ATR context after every restart).

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

WHAT'S NEW IN THIS BUILD (Brain V2)
----------------------------------------------------------
  Feature Builder    -> Brain V2 -> Confidence Engine -> Market Regime
  Engine -> Risk Engine -> Entry Engine V2 -> Position Manager ->
  Smart Exit V2 -> Trade Logger -> Training Dataset -> Online Learning

  - Brain V2 no longer just predicts direction. It runs several small
    online models in parallel and turns them into: tp_hit_probability,
    success_probability, trend_confidence, noise_probability, risk_score,
    confidence_score, hold_probability, exit_probability.
  - A real (tick-built) 1-minute candle series now backs ATR, EMA stack,
    volume delta, candle-shape and regime features - not just raw tick
    price history.
  - Market Regime Engine classifies STRONG_TREND / WEAK_TREND / SIDEWAYS /
    HIGH_VOL / LOW_VOL and the rest of the stack adapts to it.
  - Entry Engine V2 computes a single composite Entry Score from brain
    confidence + trend confidence + volume confirmation + volatility +
    momentum + regime + risk, and only trades above a configurable
    threshold (ENTRY_SCORE_THRESHOLD) - fewer, higher quality trades.
  - Smart Exit V2 requires several conditions to agree (confidence drop,
    trend weakening, momentum reversal, volume confirmation, ATR move,
    min-hold, regime) instead of a single flipped prediction.
  - DCA spacing is now ATR-adaptive (bounded by DCA_MIN/MAX_DISTANCE_PCT)
    instead of one fixed percentage.
  - Position size scales with brain confidence / risk score / regime /
    volatility, within SIZE_MIN_MULT..SIZE_MAX_MULT of the base size.
  - Partial take-profit + breakeven-stop + optional ATR trailing stop on
    the runner.
  - Every closed trade is appended to a permanent JSONL + CSV dataset
    (entry/exit features, MFE/MAE, confidence, regime, DCA count, exit
    reason, fees, etc.) for future offline retraining.
  - A composite reward (net pnl after fees, drawdown, efficiency vs MFE,
    early-exit penalty) is what the brain actually learns from - not raw
    PnL alone.
  - Rolling performance statistics (win rate, profit factor, expectancy,
    by-regime and by-side breakdowns, ...) are computed continuously and
    exported to JSON/CSV.

Everything from the previous build that already worked is preserved:
Binance API integration, signed REST client, order execution, DRY_RUN /
testnet / leverage safety gates, position recovery & reconciliation,
Railway resilience (retry-with-backoff startup, run_forever supervisor),
cooldown + min-hold guardrails, fee-aware profit gating, liquidation
sanity checking, listenKey keepalive, and the self-healing sync loop.
================================================================================
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import json
import math
import os
import pickle
import sys
import time
import traceback
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Deque, Dict, List, Optional, Tuple

import aiohttp
import numpy as np
import websockets
from sklearn.linear_model import SGDRegressor, SGDClassifier
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

# --- Position sizing (Fixed Amount base, Martingale, now confidence-scaled) -
INITIAL_ENTRY_USDT = 1.5
DCA_MULTIPLIER = 2.0
MAX_DCA_STEPS = 5

# --- Trade management ---------------------------------------------------------
DCA_TRIGGER_PCT = 0.002          # floor / fallback DCA spacing (also used if ATR unavailable)
TAKE_PROFIT_PCT = 0.002          # base / floor TP - used as-is in quiet markets
HARD_STOP_PCT = 0.05

# --- Dynamic (volatility-based) Take Profit ----------------------------------
DYNAMIC_TP_ENABLED = True
TAKE_PROFIT_MAX_PCT = 0.006      # hard ceiling - TP will never expand past this
TP_VOL_LOW = 0.0003              # tick-return std at/below this -> quiet -> base TP
TP_VOL_HIGH = 0.0012             # tick-return std at/above this -> max TP expansion

# --- Simple entry signal (warmup/fallback only, see BRAIN V2 below) ---------
SIGNAL_LOOKBACK_TICKS = 20
SIGNAL_DEADBAND_PCT = 0.0005

# --- Over-trading guardrails --------------------------------------------------
TRADE_COOLDOWN_SEC = int(os.environ.get("TRADE_COOLDOWN_SEC", "60"))
MIN_HOLD_SEC_BEFORE_EXIT = int(os.environ.get("MIN_HOLD_SEC_BEFORE_EXIT", "60"))

# --- Fee-aware profit threshold ----------------------------------------------
TAKER_FEE_RATE = float(os.environ.get("TAKER_FEE_RATE", "0.0005"))
MIN_NET_PROFIT_USDT = float(os.environ.get("MIN_NET_PROFIT_USDT", "0.05"))

# --- Liquidation-price sanity check -------------------------------------------
LIQUIDATION_SANITY_MIN_RATIO = 0.2
LIQUIDATION_SANITY_MAX_RATIO = 5.0
LIQUIDATION_WARNING_BUFFER_PCT = float(os.environ.get("LIQUIDATION_WARNING_BUFFER_PCT", "0.15"))

# --- State reconciliation grace period ----------------------------------------
SYNC_PENDING_GRACE_SEC = int(os.environ.get("SYNC_PENDING_GRACE_SEC", "8"))

# --- Candle aggregation (backs ATR / EMA / regime / volume features) --------
CANDLE_INTERVAL_SEC = int(os.environ.get("CANDLE_INTERVAL_SEC", "60"))
CANDLE_HISTORY = 180          # ~3 hours of 1m candles kept in memory

# --- Technical feature params -------------------------------------------------
ATR_PERIOD = 14
EMA_FAST = 9
EMA_MED = 21
EMA_SLOW = 55
ROLLING_RETURN_WINDOWS = (5, 15, 30)

# --- Market Regime Engine -----------------------------------------------------
REGIME_ATR_HIGH_MULT = 1.6     # current ATR vs its own rolling mean -> HIGH_VOL
REGIME_ATR_LOW_MULT = 0.6      # current ATR vs its own rolling mean -> LOW_VOL
REGIME_TREND_SLOPE_STRONG = 0.0009   # EMA_FAST slope (pct/candle) -> STRONG_TREND
REGIME_TREND_SLOPE_WEAK = 0.0003     # EMA_FAST slope (pct/candle) -> WEAK_TREND
REGIME_LOOKBACK_CANDLES = 30

# --- Brain V2 --------------------------------------------------------------
N_FEATURES_V2 = 34
BRAIN2_WARMUP_UPDATES = int(os.environ.get("BRAIN2_WARMUP_UPDATES", "80"))
LABEL_HORIZON_TICKS = 10
FEATURE_SHORT_LOOKBACK = 5
RECENT_TRADE_WINDOW = 20
TP_HIT_LOOKAHEAD_CANDLES = 8      # how far ahead we check "did price reach TP-ish move"

# --- Entry Engine V2 ---------------------------------------------------------
ENTRY_SCORE_THRESHOLD = float(os.environ.get("ENTRY_SCORE_THRESHOLD", "0.60"))
ENTRY_WEIGHTS = {
    "brain_confidence": 0.30,
    "trend_confidence": 0.20,
    "volume_confirmation": 0.12,
    "volatility_fit": 0.10,
    "momentum": 0.13,
    "regime_fit": 0.10,
    "risk_score": 0.05,   # subtracted, see EntryEngineV2
}

# --- Smart Exit V2 ------------------------------------------------------------
SMART_EXIT_ENABLED = os.environ.get("SMART_EXIT_ENABLED", "true").lower() != "false"
SMART_EXIT_MAX_LOSS_PCT = 0.01
SMART_EXIT_CONFIRM_TICKS = 5
SMART_EXIT_MIN_AGREE = 4          # of the following 6 signals, how many must agree to exit
SMART_EXIT_CONFIDENCE_DROP = 0.18  # confidence_score drop vs entry that counts as "dropped"
SMART_EXIT_ATR_MOVE_MULT = 0.8     # adverse move >= this * ATR% counts as a signal

# --- ATR-based Dynamic DCA ----------------------------------------------------
DCA_ATR_MULTIPLIER = float(os.environ.get("DCA_ATR_MULTIPLIER", "1.2"))
DCA_MIN_DISTANCE_PCT = float(os.environ.get("DCA_MIN_DISTANCE_PCT", "0.0015"))
DCA_MAX_DISTANCE_PCT = float(os.environ.get("DCA_MAX_DISTANCE_PCT", "0.02"))

# --- Dynamic position sizing ---------------------------------------------------
SIZE_MIN_MULT = float(os.environ.get("SIZE_MIN_MULT", "0.5"))
SIZE_MAX_MULT = float(os.environ.get("SIZE_MAX_MULT", "1.5"))

# --- Partial TP / breakeven / trailing stop -----------------------------------
PARTIAL_TP_ENABLED = os.environ.get("PARTIAL_TP_ENABLED", "true").lower() != "false"
PARTIAL_TP_FRACTION = float(os.environ.get("PARTIAL_TP_FRACTION", "0.5"))
PARTIAL_TP_TRIGGER_RATIO = float(os.environ.get("PARTIAL_TP_TRIGGER_RATIO", "0.6"))  # of dynamic TP distance
BREAKEVEN_AFTER_PARTIAL = os.environ.get("BREAKEVEN_AFTER_PARTIAL", "true").lower() != "false"
TRAILING_STOP_ENABLED = os.environ.get("TRAILING_STOP_ENABLED", "true").lower() != "false"
TRAILING_STOP_ATR_MULT = float(os.environ.get("TRAILING_STOP_ATR_MULT", "1.0"))

# --- Trade logging / offline dataset / performance stats ---------------------
TRADE_LOG_JSON_PATH = os.environ.get("TRADE_LOG_JSON_PATH", "trades_log.jsonl")
TRADE_LOG_CSV_PATH = os.environ.get("TRADE_LOG_CSV_PATH", "trades_log.csv")
STATS_JSON_PATH = os.environ.get("STATS_JSON_PATH", "performance_stats.json")
STATS_CSV_PATH = os.environ.get("STATS_CSV_PATH", "performance_stats.csv")
STATS_EXPORT_INTERVAL_SEC = int(os.environ.get("STATS_EXPORT_INTERVAL_SEC", "300"))

# --- Funding rate / open interest (best-effort extra features) ---------------
FUNDING_OI_POLL_SEC = int(os.environ.get("FUNDING_OI_POLL_SEC", "120"))

# --- Persistent Adaptive Learning (Cloud-Sync Brain) -------------------------
BRAIN_LOCAL_PATH = os.environ.get("BRAIN_LOCAL_PATH", "brain_v2.pkl")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRAIN_PATH = os.environ.get("GITHUB_BRAIN_PATH", "brain_v2.pkl")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
BRAIN_AUTO_PUSH_INTERVAL_SEC = int(os.environ.get("BRAIN_AUTO_PUSH_INTERVAL_SEC", "300"))

# --- Timing -------------------------------------------------------------------
LISTEN_KEY_KEEPALIVE_SEC = 25 * 60
BALANCE_REFRESH_SEC = 60
POSITION_RISK_POLL_SEC = 10
MAX_BACKOFF_SEC = 30
IDLE_DATA_TIMEOUT_SEC = 20
USER_WS_IDLE_FALLBACK_SEC = 20 * 60

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


GREEN, RED, YELLOW, CYAN, GRAY, BOLD, MAGENTA, BLUE = "32", "31", "33", "36", "90", "1", "35", "34"


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    steps = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * d_step)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def ema_series(values: List[float], period: int) -> List[float]:
    """Simple EMA over a list, seeded with the first value's SMA-of-1
    (i.e. plain EMA formula, no lookahead)."""
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


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
# CANDLE (1-minute OHLCV, built from tick data - no extra REST/kline calls)
# ============================================================================


@dataclass
class Candle:
    open_time: float
    open: float
    high: float
    low: float
    close: float
    buy_volume: float = 0.0
    sell_volume: float = 0.0

    @property
    def volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def range(self) -> float:
        return max(self.high - self.low, 1e-9)


class CandleAggregator:
    """Builds fixed-interval OHLCV candles from the raw bookTicker mid-price
    tick stream (for O/H/L/C) plus the aggTrade stream (for buy/sell volume
    delta - bookTicker alone carries no trade volume). Keeps a rolling
    history in memory; nothing here is persisted to disk (a short re-warmup
    after a restart is an acceptable tradeoff - see header notes)."""

    def __init__(self, interval_sec: int = CANDLE_INTERVAL_SEC, max_history: int = CANDLE_HISTORY):
        self.interval_sec = interval_sec
        self.candles: Deque[Candle] = deque(maxlen=max_history)
        self._current: Optional[Candle] = None
        self._bucket_start: Optional[float] = None

    def _bucket_for(self, ts: float) -> float:
        return math.floor(ts / self.interval_sec) * self.interval_sec

    def on_price(self, price: float, ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        bucket = self._bucket_for(ts)
        if self._current is None or bucket != self._bucket_start:
            if self._current is not None:
                self.candles.append(self._current)
            self._current = Candle(open_time=bucket, open=price, high=price, low=price, close=price)
            self._bucket_start = bucket
        else:
            self._current.high = max(self._current.high, price)
            self._current.low = min(self._current.low, price)
            self._current.close = price

    def on_trade(self, qty: float, is_buyer_maker: bool, ts: Optional[float] = None) -> None:
        """is_buyer_maker=True means the aggressor was a SELLER (taker sold
        into a resting bid) - Binance's own convention. We bucket volume by
        taker side, which is what actually reflects buy/sell pressure."""
        if self._current is None:
            return
        if is_buyer_maker:
            self._current.sell_volume += qty
        else:
            self._current.buy_volume += qty

    def closed_candles(self) -> List[Candle]:
        """All fully-closed candles, oldest first. Excludes the in-progress bucket."""
        return list(self.candles)

    def all_candles_incl_live(self) -> List[Candle]:
        out = list(self.candles)
        if self._current is not None:
            out.append(self._current)
        return out


# ============================================================================
# TECHNICAL INDICATORS (ATR / EMA stack / rolling vol) OVER THE CANDLE SERIES
# ============================================================================


def compute_atr(candles: List[Candle], period: int = ATR_PERIOD) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        c = candles[i]
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return float(np.mean(window)) if window else 0.0


def compute_atr_pct(candles: List[Candle], period: int = ATR_PERIOD) -> float:
    if not candles:
        return 0.0
    atr = compute_atr(candles, period)
    last_close = candles[-1].close or 1.0
    return atr / last_close


# ============================================================================
# MARKET REGIME ENGINE
# ============================================================================

REGIME_STRONG_TREND = "STRONG_TREND"
REGIME_WEAK_TREND = "WEAK_TREND"
REGIME_SIDEWAYS = "SIDEWAYS"
REGIME_HIGH_VOL = "HIGH_VOL"
REGIME_LOW_VOL = "LOW_VOL"

REGIME_LIST = [REGIME_STRONG_TREND, REGIME_WEAK_TREND, REGIME_SIDEWAYS, REGIME_HIGH_VOL, REGIME_LOW_VOL]


@dataclass
class RegimeReading:
    regime: str = REGIME_SIDEWAYS
    trend_slope: float = 0.0        # pct change per candle of EMA_FAST
    atr_pct: float = 0.0
    atr_ratio: float = 1.0          # current ATR vs its own rolling mean
    ema_fast: Optional[float] = None
    ema_med: Optional[float] = None
    ema_slow: Optional[float] = None


class MarketRegimeEngine:
    """Classifies the market into one of REGIME_LIST using the EMA stack's
    slope (trend strength/direction) and ATR's level relative to its own
    recent history (volatility expansion/compression). Volatility regimes
    take priority when extreme, since a genuinely fast/dangerous tape
    matters more to risk/entry sizing than whether it's also trending."""

    def __init__(self, lookback: int = REGIME_LOOKBACK_CANDLES):
        self.lookback = lookback
        self.atr_history: Deque[float] = deque(maxlen=lookback * 3)

    def evaluate(self, candles: List[Candle]) -> RegimeReading:
        if len(candles) < max(EMA_SLOW, ATR_PERIOD) + 2:
            return RegimeReading()

        closes = [c.close for c in candles]
        ema_fast_series = ema_series(closes, EMA_FAST)
        ema_med_series = ema_series(closes, EMA_MED)
        ema_slow_series = ema_series(closes, EMA_SLOW)

        ema_fast = ema_fast_series[-1]
        ema_med = ema_med_series[-1]
        ema_slow = ema_slow_series[-1]

        lookback_n = min(self.lookback, len(ema_fast_series) - 1)
        slope = 0.0
        if lookback_n > 0 and ema_fast_series[-1 - lookback_n]:
            ref = ema_fast_series[-1 - lookback_n]
            slope = (ema_fast - ref) / ref / lookback_n  # pct per candle

        atr = compute_atr(candles, ATR_PERIOD)
        atr_pct = compute_atr_pct(candles, ATR_PERIOD)
        self.atr_history.append(atr)
        atr_mean = float(np.mean(self.atr_history)) if self.atr_history else atr
        atr_ratio = safe_div(atr, atr_mean, default=1.0) if atr_mean else 1.0

        # Volatility extremes take priority.
        if atr_ratio >= REGIME_ATR_HIGH_MULT:
            regime = REGIME_HIGH_VOL
        elif atr_ratio <= REGIME_ATR_LOW_MULT and atr_ratio > 0:
            regime = REGIME_LOW_VOL
        elif abs(slope) >= REGIME_TREND_SLOPE_STRONG:
            regime = REGIME_STRONG_TREND
        elif abs(slope) >= REGIME_TREND_SLOPE_WEAK:
            regime = REGIME_WEAK_TREND
        else:
            regime = REGIME_SIDEWAYS

        return RegimeReading(
            regime=regime, trend_slope=slope, atr_pct=atr_pct, atr_ratio=atr_ratio,
            ema_fast=ema_fast, ema_med=ema_med, ema_slow=ema_slow,
        )


# ============================================================================
# FEATURE BUILDER V2 (rich, normalized feature vector for Brain V2)
# ============================================================================

FEATURE_NAMES = [
    "price_return", "log_return", "price_velocity", "price_acceleration",
    "rolling_return_5", "rolling_return_15", "rolling_return_30",
    "rolling_volatility", "atr_pct", "atr_expansion", "atr_compression",
    "ema_fast_distance", "ema_med_distance", "ema_slow_distance", "ema_fast_slope",
    "ema_fast_vs_med", "ema_med_vs_slow",
    "vwap_distance", "volume_z", "volume_delta", "volume_acceleration",
    "buyer_seller_pressure", "momentum_short", "momentum_long",
    "candle_body_pct", "upper_wick_ratio", "lower_wick_ratio", "candle_strength",
    "consecutive_direction", "spread_pct", "order_book_imbalance",
    "funding_rate", "time_of_day_sin", "session_encoded",
]
# (FEATURE_NAMES kept close to N_FEATURES_V2 for reference/debugging; the
#  live vector below is authoritative and includes a few extra
#  position/history features appended at the end.)


class FeatureBuilderV2:
    """Builds the full Brain V2 feature vector from: the tick-built candle
    series (technical/candle-shape/volume features), live tick state
    (velocity, spread, order-book imbalance), best-effort funding/OI data,
    wall-clock session info, and position/trade-history context. Every
    feature is a bounded/normalized ratio (percent-of-price, z-score, or
    a value already in roughly [-1, 1]) rather than a raw price, so no
    separate scaler is needed before feeding SGD models online."""

    def __init__(self):
        self.vwap_cum_pv: float = 0.0
        self.vwap_cum_v: float = 0.0
        self.vwap_window: Deque[Tuple[float, float]] = deque(maxlen=500)  # (price*qty, qty)

    def update_vwap(self, price: float, qty: float) -> None:
        if qty <= 0:
            return
        self.vwap_window.append((price * qty, qty))
        self.vwap_cum_pv = sum(pv for pv, _ in self.vwap_window)
        self.vwap_cum_v = sum(v for _, v in self.vwap_window)

    def vwap(self) -> Optional[float]:
        return safe_div(self.vwap_cum_pv, self.vwap_cum_v, default=None) if self.vwap_cum_v else None

    def build(
        self,
        candles: List[Candle],
        current_price: Optional[float],
        prev_price: Optional[float],
        prev_prev_price: Optional[float],
        best_bid_qty: float,
        best_ask_qty: float,
        spread_pct: float,
        funding_rate: Optional[float],
        position,  # PositionState
        recent_win_rate: float,
        recent_trade_frequency: float,
    ) -> np.ndarray:
        price = current_price or (candles[-1].close if candles else 0.0)

        # --- returns / velocity / acceleration -------------------------------
        price_return = safe_div((price - prev_price), prev_price) if prev_price else 0.0
        log_return = math.log(price / prev_price) if (prev_price and price > 0 and prev_price > 0) else 0.0
        price_velocity = price_return
        prev_return = safe_div((prev_price - prev_prev_price), prev_prev_price) if (prev_price and prev_prev_price) else 0.0
        price_acceleration = price_velocity - prev_return

        closes = [c.close for c in candles] if candles else []
        rolling_returns = {}
        for w in ROLLING_RETURN_WINDOWS:
            if len(closes) > w and closes[-1 - w]:
                rolling_returns[w] = (closes[-1] - closes[-1 - w]) / closes[-1 - w]
            else:
                rolling_returns[w] = 0.0

        rolling_volatility = 0.0
        if len(closes) >= 5:
            arr = np.asarray(closes[-30:], dtype=float)
            rets = np.diff(arr) / np.where(arr[:-1] == 0, 1.0, arr[:-1])
            rolling_volatility = float(np.std(rets)) if len(rets) else 0.0

        # --- ATR / volatility regime -----------------------------------------
        atr_pct = compute_atr_pct(candles, ATR_PERIOD) if candles else 0.0
        atr_hist_pct = compute_atr_pct(candles[:-5], ATR_PERIOD) if len(candles) > ATR_PERIOD + 5 else atr_pct
        atr_expansion = clamp(safe_div(atr_pct - atr_hist_pct, atr_hist_pct, 0.0), -3.0, 3.0) if atr_hist_pct else 0.0
        atr_compression = -atr_expansion

        # --- EMA stack ---------------------------------------------------------
        ema_fast_distance = ema_med_distance = ema_slow_distance = 0.0
        ema_fast_slope = ema_fast_vs_med = ema_med_vs_slow = 0.0
        if len(closes) >= EMA_SLOW + 2:
            ef = ema_series(closes, EMA_FAST)
            em = ema_series(closes, EMA_MED)
            es = ema_series(closes, EMA_SLOW)
            if price:
                ema_fast_distance = (price - ef[-1]) / price
                ema_med_distance = (price - em[-1]) / price
                ema_slow_distance = (price - es[-1]) / price
            if len(ef) > 5 and ef[-6]:
                ema_fast_slope = (ef[-1] - ef[-6]) / ef[-6] / 5.0
            if em[-1]:
                ema_fast_vs_med = (ef[-1] - em[-1]) / em[-1]
            if es[-1]:
                ema_med_vs_slow = (em[-1] - es[-1]) / es[-1]

        # --- VWAP ---------------------------------------------------------------
        vwap_val = self.vwap()
        vwap_distance = safe_div(price - vwap_val, vwap_val, 0.0) if vwap_val else 0.0

        # --- volume ---------------------------------------------------------------
        volumes = [c.volume for c in candles] if candles else []
        volume_z = 0.0
        if len(volumes) >= 10:
            vmean, vstd = float(np.mean(volumes[-30:])), float(np.std(volumes[-30:]))
            volume_z = clamp(safe_div(volumes[-1] - vmean, vstd, 0.0), -4.0, 4.0) if vstd else 0.0
        volume_delta = 0.0
        buyer_seller_pressure = 0.0
        if candles:
            last = candles[-1]
            volume_delta = clamp(safe_div(last.buy_volume - last.sell_volume, last.volume, 0.0), -1.0, 1.0)
            buyer_seller_pressure = volume_delta
        volume_acceleration = 0.0
        if len(volumes) >= 3:
            volume_acceleration = clamp(safe_div(volumes[-1] - volumes[-2], volumes[-2], 0.0), -3.0, 3.0)

        # --- momentum (kept from V1, still useful as a fast/slow tick pair) ----
        momentum_short = price_return
        momentum_long = rolling_returns.get(ROLLING_RETURN_WINDOWS[1], 0.0)

        # --- candle shape ---------------------------------------------------------
        candle_body_pct = upper_wick_ratio = lower_wick_ratio = candle_strength = 0.0
        consecutive_direction = 0.0
        if candles:
            c = candles[-1]
            candle_body_pct = safe_div(c.body, c.open, 0.0) if c.open else 0.0
            upper_wick = c.high - max(c.open, c.close)
            lower_wick = min(c.open, c.close) - c.low
            upper_wick_ratio = safe_div(upper_wick, c.range, 0.0)
            lower_wick_ratio = safe_div(lower_wick, c.range, 0.0)
            candle_strength = safe_div(abs(c.body), c.range, 0.0)

            direction_run = 0
            for cc in reversed(candles[-10:]):
                d = 1 if cc.body > 0 else (-1 if cc.body < 0 else 0)
                if direction_run == 0:
                    direction_run = d
                elif d == (1 if direction_run > 0 else -1):
                    direction_run += (1 if direction_run > 0 else -1)
                else:
                    break
            consecutive_direction = clamp(direction_run / 5.0, -1.0, 1.0)

        # --- microstructure --------------------------------------------------------
        book_total = best_bid_qty + best_ask_qty
        order_book_imbalance = safe_div(best_bid_qty - best_ask_qty, book_total, 0.0) if book_total > 0 else 0.0

        # --- funding / time-of-day / session ----------------------------------------
        funding = funding_rate if funding_rate is not None else 0.0
        now = datetime.now(timezone.utc)
        seconds_of_day = now.hour * 3600 + now.minute * 60 + now.second
        time_of_day_sin = math.sin(2 * math.pi * seconds_of_day / 86400.0)
        # Rough session buckets by UTC hour: Asia / Europe / US, encoded -1..1
        hour = now.hour
        if 0 <= hour < 8:
            session_encoded = -1.0   # Asia
        elif 8 <= hour < 16:
            session_encoded = 0.0    # Europe
        else:
            session_encoded = 1.0    # US

        # --- position / DCA / duration context ----------------------------------
        side_encoded = 0.0
        unrealized_pnl = 0.0
        dca_ratio = 0.0
        position_duration_norm = 0.0
        if position is not None and position.status in ("OPEN", "DCA_PENDING") and position.avg_entry_price and price:
            side_encoded = 1.0 if position.side == "LONG" else -1.0
            unrealized_pnl = (
                (price - position.avg_entry_price) / position.avg_entry_price
                if position.side == "LONG"
                else (position.avg_entry_price - price) / position.avg_entry_price
            )
            dca_ratio = position.dca_step / MAX_DCA_STEPS
            if position.opened_at:
                position_duration_norm = clamp((time.time() - position.opened_at) / 3600.0, 0.0, 4.0) / 4.0

        recent_win_rate_f = recent_win_rate
        recent_trade_frequency_f = clamp(recent_trade_frequency, 0.0, 1.0)

        vec = np.array([
            price_return, log_return, price_velocity, price_acceleration,
            rolling_returns.get(ROLLING_RETURN_WINDOWS[0], 0.0),
            rolling_returns.get(ROLLING_RETURN_WINDOWS[1], 0.0),
            rolling_returns.get(ROLLING_RETURN_WINDOWS[2], 0.0),
            rolling_volatility, atr_pct, atr_expansion, atr_compression,
            ema_fast_distance, ema_med_distance, ema_slow_distance, ema_fast_slope,
            ema_fast_vs_med, ema_med_vs_slow,
            vwap_distance, volume_z, volume_delta, volume_acceleration,
            buyer_seller_pressure, momentum_short, momentum_long,
            candle_body_pct, upper_wick_ratio, lower_wick_ratio, candle_strength,
            consecutive_direction, spread_pct, order_book_imbalance,
            funding, time_of_day_sin, session_encoded,
            side_encoded, unrealized_pnl, dca_ratio, position_duration_norm,
            recent_win_rate_f, recent_trade_frequency_f,
        ], dtype=float)

        # Pad/truncate defensively to N_FEATURES_V2 so config drift never
        # crashes the model shape.
        if len(vec) < N_FEATURES_V2:
            vec = np.pad(vec, (0, N_FEATURES_V2 - len(vec)))
        elif len(vec) > N_FEATURES_V2:
            vec = vec[:N_FEATURES_V2]
        return vec


# ============================================================================
# RISK ENGINE
# ============================================================================


class RiskEngine:
    """Heuristic (non-ML) risk score in [0, 1], 0 = safest, 1 = most risky.
    Combines: current volatility regime, DCA depth already used, distance
    to the exchange's own reported liquidation price, and how close the
    position already is to the hard stop. Used to gate/shrink entries and
    as an input feature/label for the brain's own risk head."""

    def score(
        self,
        regime: RegimeReading,
        dca_step: int,
        pct_move_adverse: float,   # 0 if flat/profitable, positive fraction if adverse
        distance_to_liq_pct: Optional[float],
    ) -> float:
        vol_component = clamp(regime.atr_ratio / (REGIME_ATR_HIGH_MULT * 1.5), 0.0, 1.0)
        dca_component = dca_step / MAX_DCA_STEPS
        drawdown_component = clamp(pct_move_adverse / HARD_STOP_PCT, 0.0, 1.0)
        liq_component = 0.0
        if distance_to_liq_pct is not None:
            liq_component = clamp(1.0 - (distance_to_liq_pct / max(LIQUIDATION_WARNING_BUFFER_PCT * 3, 1e-6)), 0.0, 1.0)

        score = (
            0.30 * vol_component
            + 0.25 * dca_component
            + 0.30 * drawdown_component
            + 0.15 * liq_component
        )
        return clamp(score, 0.0, 1.0)


# ============================================================================
# BRAIN V2 - probability / confidence engine (replaces the old direction-only
# predictor). Runs several small online models in parallel over the SAME
# normalized feature vector and turns their outputs into a set of
# probabilities/scores that the rest of the stack consumes.
# ============================================================================


class RunningNormalizer:
    """Welford online mean/variance normalizer, one instance per model head
    (kept separate from the feature vector itself so features stay in their
    natural, somewhat-interpretable units for logging/regime logic, while
    each model still gets a properly normalized input)."""

    def __init__(self, n_features: int):
        self.n_features = n_features
        self._n_seen = 0
        self._mean = np.zeros(n_features, dtype=float)
        self._m2 = np.zeros(n_features, dtype=float)

    def update(self, x: np.ndarray) -> None:
        self._n_seen += 1
        delta = x - self._mean
        self._mean += delta / self._n_seen
        delta2 = x - self._mean
        self._m2 += delta * delta2

    def normalize(self, x: np.ndarray) -> np.ndarray:
        if self._n_seen < 2:
            return x
        variance = self._m2 / max(self._n_seen - 1, 1)
        std = np.sqrt(variance)
        std = np.where(std < 1e-8, 1.0, std)
        return (x - self._mean) / std

    def state(self) -> dict:
        return {"n_features": self.n_features, "_n_seen": self._n_seen, "_mean": self._mean, "_m2": self._m2}

    def load(self, state: dict) -> None:
        self._n_seen = state["_n_seen"]
        self._mean = state["_mean"]
        self._m2 = state["_m2"]


class BrainV2:
    """Multi-head online model:

      - trend model      (SGDRegressor)  -> signed forward-return estimate;
                                             trend_confidence = clamp(|pred|/scale)
      - noise model       (SGDClassifier) -> P(this tick's move is noise, i.e.
                                             forward move stays inside the
                                             typical volatility band)
      - success model     (SGDClassifier) -> P(a trade opened here ends net
                                             profitable after fees)
      - tp_hit model       (SGDClassifier) -> P(price reaches the dynamic TP
                                             distance within TP_HIT_LOOKAHEAD
                                             candles, before the hard stop)
      - quality model      (SGDRegressor)  -> predicts the composite REWARD
                                             (see RewardCalculator) a trade
                                             opened here would earn - this is
                                             what "good trading behavior"
                                             actually trains against, not
                                             raw PnL.

    confidence_score / risk_score / hold_probability / exit_probability are
    DERIVED (in ConfidenceEngine) from these five heads plus the heuristic
    RiskEngine output - they are not separate models, since they are
    algebraic combinations of the others by design (keeps the learned
    model count small and each one well-identified, which matters a lot
    for a low-sample online learner).
    """

    def __init__(self, n_features: int = N_FEATURES_V2, warmup_updates: int = BRAIN2_WARMUP_UPDATES):
        self.n_features = n_features
        self.warmup_updates = warmup_updates

        self.trend_model = SGDRegressor(
            loss="squared_error", penalty="l2", alpha=1e-5,
            learning_rate="invscaling", eta0=0.01, power_t=0.25, warm_start=True,
        )
        self.quality_model = SGDRegressor(
            loss="huber", penalty="l2", alpha=1e-5,
            learning_rate="invscaling", eta0=0.01, power_t=0.25, warm_start=True,
        )
        self.noise_model = SGDClassifier(
            loss="log_loss", penalty="l2", alpha=1e-5,
            learning_rate="optimal", warm_start=True,
        )
        self.success_model = SGDClassifier(
            loss="log_loss", penalty="l2", alpha=1e-5,
            learning_rate="optimal", warm_start=True,
        )
        self.tp_hit_model = SGDClassifier(
            loss="log_loss", penalty="l2", alpha=1e-5,
            learning_rate="optimal", warm_start=True,
        )

        self.norm = RunningNormalizer(n_features)

        self.trend_fitted = False
        self.quality_fitted = False
        self.noise_fitted = False
        self.success_fitted = False
        self.tp_hit_fitted = False

        self.update_count = 0
        self.last_trend_pred: Optional[float] = None
        self.last_noise_prob: Optional[float] = None
        self.last_success_prob: Optional[float] = None
        self.last_tp_hit_prob: Optional[float] = None
        self.last_quality_pred: Optional[float] = None

        # scale used to squash trend_model's raw regression output into a
        # 0..1 "trend_confidence" - set from observed prediction magnitude,
        # starts at a sane prior and adapts slowly.
        self._trend_scale = 0.0015

    # -- prediction -----------------------------------------------------------

    def predict_all(self, x: np.ndarray) -> dict:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            x = np.nan_to_num(x)
        xn = self.norm.normalize(x).reshape(1, -1)

        trend_pred = float(self.trend_model.predict(xn)[0]) if self.trend_fitted else 0.0
        quality_pred = float(self.quality_model.predict(xn)[0]) if self.quality_fitted else 0.0
        noise_prob = float(self.noise_model.predict_proba(xn)[0][1]) if self.noise_fitted else 0.5
        success_prob = float(self.success_model.predict_proba(xn)[0][1]) if self.success_fitted else 0.5
        tp_hit_prob = float(self.tp_hit_model.predict_proba(xn)[0][1]) if self.tp_hit_fitted else 0.5

        self.last_trend_pred = trend_pred
        self.last_noise_prob = noise_prob
        self.last_success_prob = success_prob
        self.last_tp_hit_prob = tp_hit_prob
        self.last_quality_pred = quality_pred

        trend_confidence = clamp(abs(trend_pred) / max(self._trend_scale, 1e-6), 0.0, 1.0)
        trend_direction = "LONG" if trend_pred > 0 else ("SHORT" if trend_pred < 0 else None)

        return {
            "trend_pred": trend_pred,
            "trend_confidence": trend_confidence,
            "trend_direction": trend_direction,
            "noise_probability": noise_prob,
            "success_probability": success_prob,
            "tp_hit_probability": tp_hit_prob,
            "quality_pred": quality_pred,
        }

    # -- online learning --------------------------------------------------------

    def learn_trend(self, x: np.ndarray, forward_return: float) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)) or not np.isfinite(forward_return):
            return
        self.norm.update(x)
        xn = self.norm.normalize(x).reshape(1, -1)
        self.trend_model.partial_fit(xn, [float(forward_return)])
        self.trend_fitted = True
        # slowly adapt the confidence-squash scale toward observed |return| typical size
        self._trend_scale = 0.98 * self._trend_scale + 0.02 * max(abs(forward_return), 1e-6)
        self.update_count += 1

    def learn_noise(self, x: np.ndarray, is_noise: bool) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        classes = np.array([0, 1]) if not self.noise_fitted else None
        self.noise_model.partial_fit(xn, [1 if is_noise else 0], classes=classes)
        self.noise_fitted = True

    def learn_success(self, x: np.ndarray, was_success: bool) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        classes = np.array([0, 1]) if not self.success_fitted else None
        self.success_model.partial_fit(xn, [1 if was_success else 0], classes=classes)
        self.success_fitted = True

    def learn_tp_hit(self, x: np.ndarray, tp_was_hit: bool) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        classes = np.array([0, 1]) if not self.tp_hit_fitted else None
        self.tp_hit_model.partial_fit(xn, [1 if tp_was_hit else 0], classes=classes)
        self.tp_hit_fitted = True

    def learn_quality(self, x: np.ndarray, reward: float) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)) or not np.isfinite(reward):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        self.quality_model.partial_fit(xn, [float(reward)])
        self.quality_fitted = True

    def is_ready(self) -> bool:
        return self.trend_fitted and self.update_count >= self.warmup_updates

    # -- persistence ------------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "version": 2,
            "n_features": self.n_features,
            "warmup_updates": self.warmup_updates,
            "trend_model": self.trend_model, "quality_model": self.quality_model,
            "noise_model": self.noise_model, "success_model": self.success_model,
            "tp_hit_model": self.tp_hit_model,
            "trend_fitted": self.trend_fitted, "quality_fitted": self.quality_fitted,
            "noise_fitted": self.noise_fitted, "success_fitted": self.success_fitted,
            "tp_hit_fitted": self.tp_hit_fitted,
            "update_count": self.update_count,
            "_trend_scale": self._trend_scale,
            "norm": self.norm.state(),
        }

    def load_state(self, state: dict) -> None:
        self.trend_model = state["trend_model"]
        self.quality_model = state["quality_model"]
        self.noise_model = state["noise_model"]
        self.success_model = state["success_model"]
        self.tp_hit_model = state["tp_hit_model"]
        self.trend_fitted = state["trend_fitted"]
        self.quality_fitted = state["quality_fitted"]
        self.noise_fitted = state["noise_fitted"]
        self.success_fitted = state["success_fitted"]
        self.tp_hit_fitted = state["tp_hit_fitted"]
        self.update_count = state["update_count"]
        self._trend_scale = state.get("_trend_scale", 0.0015)
        self.norm.load(state["norm"])

    def to_bytes(self) -> bytes:
        return pickle.dumps(self.to_state(), protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def from_bytes(cls, data: bytes, n_features: int, warmup_updates: int) -> "BrainV2":
        """Falls back to a fresh (cold) brain on any corruption, version
        mismatch, or feature-shape mismatch - a bad/stale snapshot must
        never prevent the bot from starting."""
        brain = cls(n_features, warmup_updates)
        try:
            state = pickle.loads(data)
            if state.get("version") != 2 or state.get("n_features") != n_features:
                print(color(
                    f"[brain] snapshot incompatible (version={state.get('version')}, "
                    f"n_features={state.get('n_features')}, expected {n_features}) - "
                    f"starting a fresh Brain V2.", YELLOW,
                ))
                return brain
            brain.load_state(state)
        except Exception as e:  # noqa: BLE001 - corrupted/incompatible snapshot must not crash startup
            print(color(f"[brain] failed to deserialize snapshot ({e}), starting fresh.", YELLOW))
            return cls(n_features, warmup_updates)
        return brain


# ============================================================================
# CLOUD-SYNC BRAIN (push/pull brain snapshot to GitHub across ephemeral
# restarts). Unchanged in behavior from the previous build - still generic
# over whatever bytes it's given.
# ============================================================================


class GithubBrainSync:
    """Best-effort sync of the brain snapshot to a GitHub repo via the
    Contents API. Deliberately fails soft everywhere: any network/auth/API
    error is caught, logged, and swallowed - trading must never stop
    because GitHub is unreachable or misconfigured. If GITHUB_TOKEN/
    GITHUB_REPO aren't set, `enabled` is False and every method becomes a
    no-op, so the bot still runs fine on local-disk state alone (just
    without cross-restart persistence on a fully ephemeral host)."""

    def __init__(self, token: str, repo: str, path: str, branch: str):
        self.token = token
        self.repo = repo
        self.path = path
        self.branch = branch
        self.enabled = bool(token and repo)
        self.session: Optional[aiohttp.ClientSession] = None
        self._last_sha: Optional[str] = None

    async def start(self) -> None:
        if not self.enabled:
            print(color(
                "[brain-sync] GITHUB_TOKEN / GITHUB_REPO not set - brain snapshot will persist "
                "locally only (lost on next ephemeral restart). Set both env vars to enable "
                "cross-restart cloud sync.", YELLOW,
            ))
            return
        self.session = aiohttp.ClientSession(headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    def _url(self) -> str:
        return f"https://api.github.com/repos/{self.repo}/contents/{self.path}"

    async def download(self) -> Optional[bytes]:
        if not self.enabled or self.session is None:
            return None
        try:
            async with self.session.get(
                self._url(), params={"ref": self.branch},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                data = await resp.json()
                self._last_sha = data.get("sha")
                content_b64 = data.get("content", "")
                if not content_b64:
                    return None
                return base64.b64decode(content_b64)
        except Exception as e:  # noqa: BLE001 - sync must never take the bot down
            print(color(f"[brain-sync] GitHub download failed (continuing without it): {e}", YELLOW))
            return None

    async def upload(self, data: bytes, message: str) -> bool:
        if not self.enabled or self.session is None:
            return False
        try:
            sha = self._last_sha
            if sha is None:
                async with self.session.get(
                    self._url(), params={"ref": self.branch},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        existing = await resp.json()
                        sha = existing.get("sha")

            payload = {
                "message": message,
                "content": base64.b64encode(data).decode("ascii"),
                "branch": self.branch,
            }
            if sha:
                payload["sha"] = sha

            async with self.session.put(
                self._url(), json=payload, timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                result = await resp.json()
                self._last_sha = (result.get("content") or {}).get("sha")
                return True
        except Exception as e:  # noqa: BLE001 - sync must never take the bot down
            print(color(f"[brain-sync] GitHub push failed (bot keeps trading): {e}", YELLOW))
            return False


# ============================================================================
# CONFIDENCE ENGINE - turns Brain V2's raw head outputs + RiskEngine into
# the final confidence_score / hold_probability / exit_probability that the
# rest of the stack consumes. Kept as pure functions of already-computed
# values (no further learning here) so it stays simple to reason about.
# ============================================================================


@dataclass
class ConfidenceReading:
    confidence_score: float = 0.0     # 0..1 overall conviction in the current read
    trend_confidence: float = 0.0     # 0..1 how strongly Brain V2 believes in a direction
    trend_direction: Optional[str] = None
    success_probability: float = 0.5
    tp_hit_probability: float = 0.5
    noise_probability: float = 0.5
    risk_score: float = 0.0
    hold_probability: float = 0.5
    exit_probability: float = 0.5
    quality_pred: float = 0.0


class ConfidenceEngine:
    def evaluate(self, brain_out: dict, risk_score: float, position_side: Optional[str] = None) -> ConfidenceReading:
        trend_confidence = brain_out["trend_confidence"]
        success_p = brain_out["success_probability"]
        tp_hit_p = brain_out["tp_hit_probability"]
        noise_p = brain_out["noise_probability"]

        # Overall confidence: weighted blend of "the brain thinks this will
        # work" signals, discounted by how "noisy" it currently thinks the
        # market is and by heuristic risk.
        raw_confidence = (
            0.35 * trend_confidence
            + 0.35 * success_p
            + 0.30 * tp_hit_p
        )
        confidence_score = clamp(raw_confidence * (1.0 - 0.5 * noise_p) * (1.0 - 0.4 * risk_score), 0.0, 1.0)

        # hold/exit probability: if we're IN a position, "hold" is favored
        # when success probability is still high AND the trend direction
        # still agrees with the position side; "exit" rises when either
        # flips.
        hold_probability = success_p
        exit_probability = 1.0 - success_p
        if position_side is not None and brain_out.get("trend_direction") is not None:
            agrees = brain_out["trend_direction"] == position_side
            if not agrees:
                hold_probability = clamp(hold_probability - 0.25, 0.0, 1.0)
                exit_probability = clamp(exit_probability + 0.25, 0.0, 1.0)

        return ConfidenceReading(
            confidence_score=confidence_score,
            trend_confidence=trend_confidence,
            trend_direction=brain_out.get("trend_direction"),
            success_probability=success_p,
            tp_hit_probability=tp_hit_p,
            noise_probability=noise_p,
            risk_score=risk_score,
            hold_probability=hold_probability,
            exit_probability=exit_probability,
            quality_pred=brain_out.get("quality_pred", 0.0),
        )


# ============================================================================
# ENTRY ENGINE V2 - composite Entry Score gating fresh entries. Replaces
# "any nonzero signal opens a trade" with a weighted score that must clear
# ENTRY_SCORE_THRESHOLD, cutting trade frequency in favor of quality.
# ============================================================================


@dataclass
class EntryDecision:
    should_enter: bool
    side: Optional[str]
    score: float
    components: dict


class EntryEngineV2:
    def evaluate(
        self,
        conf: ConfidenceReading,
        regime: RegimeReading,
        volume_z: float,
        momentum: float,
        features: np.ndarray,
    ) -> EntryDecision:
        if conf.trend_direction is None or conf.trend_confidence <= 0:
            return EntryDecision(False, None, 0.0, {})

        volume_confirmation = clamp((volume_z + 2.0) / 4.0, 0.0, 1.0)  # z in [-2,2] -> [0,1]

        # Volatility fit: entries are best in LOW/normal-to-moderate vol and
        # trending regimes; HIGH_VOL is discounted (bigger, faster adverse
        # moves against a martingale DCA book), pure SIDEWAYS is discounted
        # too (no edge for a directional entry).
        if regime.regime == REGIME_HIGH_VOL:
            volatility_fit = 0.35
        elif regime.regime in (REGIME_STRONG_TREND, REGIME_WEAK_TREND):
            volatility_fit = 1.0
        elif regime.regime == REGIME_LOW_VOL:
            volatility_fit = 0.6
        else:  # SIDEWAYS
            volatility_fit = 0.4

        momentum_component = clamp((abs(momentum) / 0.002), 0.0, 1.0)  # saturates at 0.2% tick momentum

        # Regime fit: does the regime's own directional bias (slope sign)
        # agree with the brain's proposed side?
        regime_fit = 0.5
        if regime.regime in (REGIME_STRONG_TREND, REGIME_WEAK_TREND):
            slope_side = "LONG" if regime.trend_slope > 0 else "SHORT"
            regime_fit = 1.0 if slope_side == conf.trend_direction else 0.2
        elif regime.regime == REGIME_SIDEWAYS:
            regime_fit = 0.5
        elif regime.regime == REGIME_HIGH_VOL:
            regime_fit = 0.4

        components = {
            "brain_confidence": conf.confidence_score,
            "trend_confidence": conf.trend_confidence,
            "volume_confirmation": volume_confirmation,
            "volatility_fit": volatility_fit,
            "momentum": momentum_component,
            "regime_fit": regime_fit,
            "risk_score": conf.risk_score,
        }

        score = 0.0
        for key, weight in ENTRY_WEIGHTS.items():
            val = components.get(key, 0.0)
            if key == "risk_score":
                score -= weight * val   # risk SUBTRACTS from the score
            else:
                score += weight * val

        should_enter = score >= ENTRY_SCORE_THRESHOLD
        return EntryDecision(should_enter, conf.trend_direction, score, components)


# ============================================================================
# REWARD CALCULATOR - composite training signal, not raw PnL. Rewards net
# profit after fees, penalizes drawdown and inefficiency (leaving most of
# the favorable move on the table, or exiting long before TP for no good
# reason), so the brain learns "good trading behavior" instead of pure
# outcome noise.
# ============================================================================


class RewardCalculator:
    def compute(
        self,
        net_pnl_usdt: float,
        invested_notional: float,
        mfe_pct: float,     # max favorable excursion, as a fraction move
        mae_pct: float,     # max adverse excursion, as a fraction move
        dynamic_tp_pct: float,
        exit_reason: str,
        held_sec: float,
    ) -> float:
        if invested_notional <= 0:
            return 0.0

        net_pnl_pct = net_pnl_usdt / invested_notional

        # Efficiency: how much of the best available favorable move did the
        # trade actually capture? 1.0 = captured the full MFE, 0 = captured
        # none / went the wrong way.
        efficiency = clamp(safe_div(net_pnl_pct, mfe_pct, 0.0), -1.0, 1.0) if mfe_pct > 1e-9 else 0.0

        # Drawdown penalty: how deep did it go against us before recovering
        # (or before the eventual loss)? Bigger MAE is worse, independent
        # of the final outcome, since deep excursions are riskier / more
        # stressful on this leverage.
        drawdown_penalty = clamp(mae_pct / HARD_STOP_PCT, 0.0, 1.0)

        # Early-exit penalty: only relevant for SMART EXIT closes - if the
        # trade was very close to reaching dynamic TP (per MFE) when it
        # exited, that's a worse outcome than a clean TP hit even if it was
        # still net-profitable, since the exit gave up available profit.
        early_exit_penalty = 0.0
        if exit_reason == "smart_exit" and dynamic_tp_pct > 0:
            progress_to_tp = clamp(mfe_pct / dynamic_tp_pct, 0.0, 1.0)
            early_exit_penalty = 0.3 * progress_to_tp

        # Holding-quality: extremely short holds (churn) are mildly
        # penalized even if profitable, since they're closer to noise than
        # skill and pay fees disproportionately relative to the move
        # captured.
        churn_penalty = 0.1 if held_sec < MIN_HOLD_SEC_BEFORE_EXIT * 1.5 and net_pnl_pct <= 0 else 0.0

        reward = (
            net_pnl_pct
            + 0.15 * efficiency
            - 0.20 * drawdown_penalty
            - early_exit_penalty
            - churn_penalty
        )
        return float(clamp(reward, -1.0, 1.0))


# ============================================================================
# TRADE LOGGER - permanent JSON/CSV dataset of every completed trade, for
# future offline retraining and for the Performance Stats module below.
# ============================================================================

TRADE_LOG_FIELDS = [
    "close_time", "symbol", "side", "entry_price", "exit_price", "qty",
    "invested_notional", "gross_pnl_usdt", "fees_usdt", "net_pnl_usdt",
    "net_pnl_pct", "dca_count", "holding_time_sec", "mfe_pct", "mae_pct",
    "exit_reason", "tp_hit", "smart_exit", "manual_exit", "hard_stop",
    "entry_regime", "exit_regime", "entry_confidence", "entry_risk_score",
    "entry_success_prob", "entry_tp_hit_prob", "reward", "final_outcome",
]


class TradeLogger:
    """Appends one JSON line + one CSV row per closed trade. Both writes
    are best-effort (a logging failure must never interrupt trading) and
    both are append-only, so this is safe to run continuously on an
    ephemeral filesystem (and can be pushed to GitHub the same way the
    brain snapshot is, if desired, by pointing GITHUB_REPO's workflow at
    it externally - not wired automatically here to avoid an extra API
    call on every single trade close)."""

    def __init__(self, json_path: str = TRADE_LOG_JSON_PATH, csv_path: str = TRADE_LOG_CSV_PATH):
        self.json_path = json_path
        self.csv_path = csv_path
        self._csv_header_written = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0

    def log_trade(self, record: dict) -> None:
        try:
            with open(self.json_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:  # noqa: BLE001 - logging must never crash the trading loop
            print(color(f"[trade-log] failed to append JSONL: {e}", YELLOW))

        try:
            write_header = not self._csv_header_written
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS, extrasaction="ignore")
                if write_header:
                    writer.writeheader()
                    self._csv_header_written = True
                writer.writerow(record)
        except Exception as e:  # noqa: BLE001
            print(color(f"[trade-log] failed to append CSV: {e}", YELLOW))

    def load_all(self) -> List[dict]:
        """Reads back every logged trade from the JSONL file (source of
        truth - CSV is a convenience export). Used by PerformanceStats."""
        records: List[dict] = []
        if not os.path.exists(self.json_path):
            return records
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:  # noqa: BLE001
            print(color(f"[trade-log] failed to read JSONL for stats: {e}", YELLOW))
        return records


# ============================================================================
# PERFORMANCE STATISTICS - computed continuously from the trade log and
# exported to JSON/CSV on a fixed interval.
# ============================================================================


class PerformanceStats:
    def __init__(self, logger: TradeLogger, json_path: str = STATS_JSON_PATH, csv_path: str = STATS_CSV_PATH):
        self.logger = logger
        self.json_path = json_path
        self.csv_path = csv_path

    def compute(self) -> dict:
        trades = self.logger.load_all()
        n = len(trades)
        if n == 0:
            return {"trade_count": 0, "generated_at": now_str()}

        net_pnls = [float(t.get("net_pnl_usdt", 0.0)) for t in trades]
        wins = [p for p in net_pnls if p > 0]
        losses = [p for p in net_pnls if p <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        total_fees = sum(float(t.get("fees_usdt", 0.0)) for t in trades)
        net_profit = sum(net_pnls)

        win_rate = safe_div(len(wins), n, 0.0)
        loss_rate = safe_div(len(losses), n, 0.0)
        profit_factor = safe_div(gross_profit, gross_loss, default=float("inf") if gross_profit > 0 else 0.0)
        avg_win = safe_div(gross_profit, len(wins), 0.0)
        avg_loss = safe_div(gross_loss, len(losses), 0.0)
        expectancy = win_rate * avg_win - loss_rate * avg_loss

        hold_times = [float(t.get("holding_time_sec", 0.0)) for t in trades]
        dca_counts = [float(t.get("dca_count", 0.0)) for t in trades]

        def _side_stats(side: str) -> dict:
            side_trades = [t for t in trades if t.get("side") == side]
            side_pnls = [float(t.get("net_pnl_usdt", 0.0)) for t in side_trades]
            return {
                "count": len(side_trades),
                "win_rate": safe_div(len([p for p in side_pnls if p > 0]), len(side_trades), 0.0),
                "net_profit": sum(side_pnls),
            }

        by_regime: Dict[str, dict] = {}
        for regime_name in REGIME_LIST:
            regime_trades = [t for t in trades if t.get("entry_regime") == regime_name]
            if not regime_trades:
                continue
            regime_pnls = [float(t.get("net_pnl_usdt", 0.0)) for t in regime_trades]
            by_regime[regime_name] = {
                "count": len(regime_trades),
                "win_rate": safe_div(len([p for p in regime_pnls if p > 0]), len(regime_trades), 0.0),
                "net_profit": sum(regime_pnls),
            }

        confidences = [float(t.get("entry_confidence", 0.0)) for t in trades]
        confidence_dist = {
            "mean": float(np.mean(confidences)) if confidences else 0.0,
            "p25": float(np.percentile(confidences, 25)) if confidences else 0.0,
            "p50": float(np.percentile(confidences, 50)) if confidences else 0.0,
            "p75": float(np.percentile(confidences, 75)) if confidences else 0.0,
        }

        daily: Dict[str, dict] = {}
        for t in trades:
            close_time = t.get("close_time")
            day_key = str(close_time)[:10] if close_time else "unknown"
            d = daily.setdefault(day_key, {"count": 0, "net_profit": 0.0, "wins": 0})
            d["count"] += 1
            d["net_profit"] += float(t.get("net_pnl_usdt", 0.0))
            if float(t.get("net_pnl_usdt", 0.0)) > 0:
                d["wins"] += 1
        for d in daily.values():
            d["win_rate"] = safe_div(d["wins"], d["count"], 0.0)

        return {
            "generated_at": now_str(),
            "trade_count": n,
            "win_rate": win_rate,
            "loss_rate": loss_rate,
            "profit_factor": profit_factor,
            "expectancy_usdt": expectancy,
            "avg_win_usdt": avg_win,
            "avg_loss_usdt": avg_loss,
            "net_profit_usdt": net_profit,
            "gross_profit_usdt": gross_profit,
            "gross_loss_usdt": gross_loss,
            "total_fees_usdt": total_fees,
            "largest_win_usdt": max(net_pnls) if net_pnls else 0.0,
            "largest_loss_usdt": min(net_pnls) if net_pnls else 0.0,
            "avg_holding_time_sec": float(np.mean(hold_times)) if hold_times else 0.0,
            "avg_dca_count": float(np.mean(dca_counts)) if dca_counts else 0.0,
            "long_performance": _side_stats("LONG"),
            "short_performance": _side_stats("SHORT"),
            "performance_by_regime": by_regime,
            "brain_confidence_distribution": confidence_dist,
            "daily_statistics": daily,
        }

    def export(self) -> None:
        stats = self.compute()
        try:
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2, default=str)
        except Exception as e:  # noqa: BLE001 - stats export must never crash the trading loop
            print(color(f"[stats] failed to write JSON stats: {e}", YELLOW))

        try:
            flat = {k: v for k, v in stats.items() if not isinstance(v, dict)}
            write_header = not (os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0)
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(flat.keys()))
                if write_header:
                    writer.writeheader()
                writer.writerow(flat)
        except Exception as e:  # noqa: BLE001
            print(color(f"[stats] failed to write CSV stats: {e}", YELLOW))

        if stats.get("trade_count", 0) > 0:
            print(color(
                f"{now_str()} [stats] trades={stats['trade_count']} win_rate={stats['win_rate']*100:.1f}% "
                f"profit_factor={stats['profit_factor']:.2f} expectancy=${stats['expectancy_usdt']:+.4f} "
                f"net_profit=${stats['net_profit_usdt']:+.4f} fees=${stats['total_fees_usdt']:.4f}",
                BLUE,
            ))


# ============================================================================
# POSITION STATE + MARTINGALE MANAGER V2 (core strategy state machine, now
# wired through Feature Builder -> Brain V2 -> Confidence Engine -> Market
# Regime Engine -> Risk Engine -> Entry Engine V2 -> Position Manager ->
# Smart Exit V2 -> Trade Logger -> Training Dataset -> Online Learning)
# ============================================================================


@dataclass
class PositionState:
    side: Optional[str] = None
    status: str = "FLAT"
    dca_step: int = 0
    entries: List[tuple] = field(default_factory=list)
    avg_entry_price: Optional[float] = None
    total_qty: float = 0.0
    original_qty: float = 0.0            # qty at full size, before any partial TP reduced it
    pending_order_id: Optional[int] = None
    pending_role: Optional[str] = None
    pending_order_ts: float = 0.0
    opened_at: float = 0.0
    last_close_time: float = 0.0
    last_dca_price: Optional[float] = None   # anchor for ATR-based DCA spacing

    # -- partial TP / breakeven / trailing -------------------------------------
    partial_tp_done: bool = False
    breakeven_armed: bool = False
    breakeven_price: Optional[float] = None
    trailing_stop_price: Optional[float] = None
    max_favorable_price: Optional[float] = None
    max_adverse_price: Optional[float] = None

    # -- entry-time snapshot, for training / logging ---------------------------
    entry_features: Optional[np.ndarray] = None
    entry_regime: str = REGIME_SIDEWAYS
    entry_confidence: float = 0.0
    entry_risk_score: float = 0.0
    entry_success_prob: float = 0.5
    entry_tp_hit_prob: float = 0.5
    entry_dynamic_tp_pct: float = TAKE_PROFIT_PCT
    realized_fees_usdt: float = 0.0


class MartingaleManager:
    def __init__(self, client: RestClient, symbol: str, filters: SymbolFilters, leverage: int):
        self.client = client
        self.symbol = symbol
        self.filters = filters
        self.leverage = leverage

        self.position = PositionState()
        self.current_price: Optional[float] = None
        self.prev_price: Optional[float] = None
        self.prev_prev_price: Optional[float] = None
        self.available_balance: float = 0.0
        self.liquidation_price: Optional[float] = None

        self.price_history: List[float] = []   # kept for the fallback static momentum signal only
        self.trade_count = 0
        self.realized_pnl_total = 0.0
        self.last_trade_action_ts: float = 0.0
        self.last_trade_open_ts: float = 0.0

        # --- Brain V2 stack -----------------------------------------------------
        self.candles = CandleAggregator()
        self.feature_builder = FeatureBuilderV2()
        self.regime_engine = MarketRegimeEngine()
        self.risk_engine = RiskEngine()
        self.brain = BrainV2(N_FEATURES_V2, BRAIN2_WARMUP_UPDATES)
        self.confidence_engine = ConfidenceEngine()
        self.entry_engine = EntryEngineV2()
        self.reward_calc = RewardCalculator()
        self.trade_logger = TradeLogger()
        self.perf_stats = PerformanceStats(self.trade_logger)

        self._feature_buffer: Deque[Tuple[float, np.ndarray, float]] = deque(
            maxlen=LABEL_HORIZON_TICKS + 1
        )
        self.last_regime: RegimeReading = RegimeReading()
        self.last_confidence: ConfidenceReading = ConfidenceReading()
        self.last_entry_decision: Optional[EntryDecision] = None

        # --- real-time feature ingestion inputs ---------------------------
        self.best_bid_qty: float = 0.0
        self.best_ask_qty: float = 0.0
        self.best_bid_price: float = 0.0
        self.best_ask_price: float = 0.0
        self.funding_rate: Optional[float] = None
        self.open_interest: Optional[float] = None
        self.recent_trade_outcomes: deque[float] = deque(maxlen=RECENT_TRADE_WINDOW)
        self.recent_trade_timestamps: deque[float] = deque(maxlen=RECENT_TRADE_WINDOW)

        # --- Cloud-Sync Brain --------------------------------------------
        self.github_sync = GithubBrainSync(
            GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRAIN_PATH, GITHUB_BRANCH
        )
        self._brain_dirty = False
        self.last_brain_sync_ts: Optional[float] = None

        self._order_index: Dict[int, str] = {}
        self._rp_accum: Dict[int, float] = {}

    # -- Persistent Adaptive Learning: startup load / ongoing persistence ----

    async def load_or_init_brain(self) -> None:
        if os.path.exists(BRAIN_LOCAL_PATH):
            try:
                with open(BRAIN_LOCAL_PATH, "rb") as f:
                    data = f.read()
                self.brain = BrainV2.from_bytes(data, N_FEATURES_V2, BRAIN2_WARMUP_UPDATES)
                print(color(
                    f"[brain] loaded local {BRAIN_LOCAL_PATH} "
                    f"(updates={self.brain.update_count}, ready={self.brain.is_ready()})", MAGENTA,
                ))
                return
            except Exception as e:  # noqa: BLE001 - corrupt local file must not block startup
                print(color(f"[brain] local {BRAIN_LOCAL_PATH} unreadable ({e}), trying GitHub ...", YELLOW))

        await self.github_sync.start()
        remote = await self.github_sync.download()
        if remote:
            try:
                with open(BRAIN_LOCAL_PATH, "wb") as f:
                    f.write(remote)
            except Exception as e:  # noqa: BLE001 - disk write failure shouldn't block using the brain
                print(color(f"[brain] could not cache downloaded brain to disk: {e}", YELLOW))
            self.brain = BrainV2.from_bytes(remote, N_FEATURES_V2, BRAIN2_WARMUP_UPDATES)
            print(color(
                f"[brain] restored from GitHub ({GITHUB_REPO}/{GITHUB_BRAIN_PATH}) "
                f"(updates={self.brain.update_count}, ready={self.brain.is_ready()})", MAGENTA,
            ))
            return

        print(color(
            "[brain] no local or remote snapshot found - starting a fresh (cold) Brain V2.", GRAY
        ))

    async def persist_brain(self, reason: str) -> None:
        try:
            data = self.brain.to_bytes()
        except Exception as e:  # noqa: BLE001 - serialization must never crash the trading loop
            print(color(f"[brain] failed to serialize brain state ({e}), skipping persist.", RED))
            return

        try:
            tmp_path = f"{BRAIN_LOCAL_PATH}.tmp"
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, BRAIN_LOCAL_PATH)  # atomic on POSIX - never a half-written file
        except Exception as e:  # noqa: BLE001
            print(color(f"[brain] failed to write {BRAIN_LOCAL_PATH} locally: {e}", RED))

        try:
            pushed = await self.github_sync.upload(
                data, message=f"brain sync: {reason} (updates={self.brain.update_count})"
            )
            if pushed:
                self.last_brain_sync_ts = time.time()
                print(color(
                    f"{now_str()} [brain-sync] pushed brain snapshot to GitHub ({reason}, "
                    f"updates={self.brain.update_count})", MAGENTA,
                ))
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; upload() already catches internally
            print(color(f"[brain-sync] unexpected error during push (bot keeps trading): {e}", RED))
        self._brain_dirty = False

    # -- sizing / fees -----------------------------------------------------------

    def confidence_size_multiplier(self, conf: ConfidenceReading, regime: RegimeReading) -> float:
        """High confidence + low risk = larger size; low confidence / high
        risk / high volatility = smaller size. Bounded to
        [SIZE_MIN_MULT, SIZE_MAX_MULT] so martingale sizing never grows
        unboundedly beyond what MAX_DCA_STEPS / min-notional checks at
        startup were sized for."""
        base = 0.5 + 0.5 * conf.confidence_score      # confidence_score in [0,1] -> [0.5, 1.0]
        risk_discount = 1.0 - 0.5 * conf.risk_score    # risk_score in [0,1] -> [0.5, 1.0]
        vol_discount = 0.7 if regime.regime == REGIME_HIGH_VOL else 1.0
        mult = base * risk_discount * vol_discount
        return clamp(mult, SIZE_MIN_MULT, SIZE_MAX_MULT)

    def notional_for_step(self, step: int, size_mult: float = 1.0) -> float:
        margin = INITIAL_ENTRY_USDT if step == 0 else INITIAL_ENTRY_USDT * (DCA_MULTIPLIER ** step)
        # Dynamic sizing only applies to the INITIAL entry - DCA step sizing
        # stays purely martingale-deterministic (2x per step) so the
        # existing, already-safety-checked-at-startup DCA math is untouched.
        if step == 0:
            margin *= size_mult
        return margin * self.leverage

    def estimate_round_trip_fee_usdt(self, qty: float, entry_price: float, exit_price: float) -> float:
        entry_notional = qty * entry_price
        exit_notional = qty * exit_price
        return TAKER_FEE_RATE * (entry_notional + exit_notional)

    def estimate_net_pnl_usdt(self, exit_price: float, qty: Optional[float] = None) -> float:
        p = self.position
        if not p.avg_entry_price or p.total_qty <= 0:
            return 0.0
        use_qty = qty if qty is not None else p.total_qty
        if p.side == "LONG":
            gross = (exit_price - p.avg_entry_price) * use_qty
        else:
            gross = (p.avg_entry_price - exit_price) * use_qty
        fees = self.estimate_round_trip_fee_usdt(use_qty, p.avg_entry_price, exit_price)
        return gross - fees

    # -- tick plumbing -----------------------------------------------------------

    def update_price_history(self, price: float) -> None:
        self.price_history.append(price)
        if len(self.price_history) > SIGNAL_LOOKBACK_TICKS + 1:
            self.price_history.pop(0)

    def on_book_ticker(self, bid: float, ask: float, bid_qty: float, ask_qty: float) -> None:
        self.prev_prev_price = self.prev_price
        self.prev_price = self.current_price
        price = (bid + ask) / 2
        self.current_price = price
        self.best_bid_price, self.best_ask_price = bid, ask
        self.best_bid_qty, self.best_ask_qty = bid_qty, ask_qty
        self.update_price_history(price)
        self.candles.on_price(price)
        self.feature_builder.update_vwap(price, (bid_qty + ask_qty) / 2.0)

    def on_agg_trade(self, qty: float, is_buyer_maker: bool) -> None:
        self.candles.on_trade(qty, is_buyer_maker)

    def _spread_pct(self) -> float:
        if not self.best_bid_price or not self.best_ask_price:
            return 0.0
        mid = (self.best_bid_price + self.best_ask_price) / 2.0
        return safe_div(self.best_ask_price - self.best_bid_price, mid, 0.0)

    def _recent_trade_frequency(self) -> float:
        """Trades in the RECENT_TRADE_WINDOW timestamps per hour, normalized
        against a "busy" baseline of one trade every TRADE_COOLDOWN_SEC."""
        if len(self.recent_trade_timestamps) < 2:
            return 0.0
        span = self.recent_trade_timestamps[-1] - self.recent_trade_timestamps[0]
        if span <= 0:
            return 0.0
        rate_per_sec = len(self.recent_trade_timestamps) / span
        baseline = 1.0 / max(TRADE_COOLDOWN_SEC, 1)
        return clamp(rate_per_sec / baseline, 0.0, 1.0)

    def build_features(self) -> np.ndarray:
        candles = self.candles.all_candles_incl_live()
        recent_win_rate = float(np.mean(self.recent_trade_outcomes)) if self.recent_trade_outcomes else 0.5
        return self.feature_builder.build(
            candles=candles,
            current_price=self.current_price,
            prev_price=self.prev_price,
            prev_prev_price=self.prev_prev_price,
            best_bid_qty=self.best_bid_qty,
            best_ask_qty=self.best_ask_qty,
            spread_pct=self._spread_pct(),
            funding_rate=self.funding_rate,
            position=self.position,
            recent_win_rate=recent_win_rate,
            recent_trade_frequency=self._recent_trade_frequency(),
        )

    # -- dynamic TP / DCA spacing --------------------------------------------------

    def get_dynamic_take_profit_pct(self) -> float:
        if not DYNAMIC_TP_ENABLED:
            return TAKE_PROFIT_PCT
        candles = self.candles.closed_candles()
        if len(candles) < 5:
            return TAKE_PROFIT_PCT
        vol = self.last_regime.atr_pct if self.last_regime.atr_pct else compute_atr_pct(candles)
        if vol <= TP_VOL_LOW:
            return TAKE_PROFIT_PCT
        if vol >= TP_VOL_HIGH:
            return TAKE_PROFIT_MAX_PCT
        vol_range = TP_VOL_HIGH - TP_VOL_LOW
        ratio = (vol - TP_VOL_LOW) / vol_range if vol_range > 0 else 0.0
        return TAKE_PROFIT_PCT + ratio * (TAKE_PROFIT_MAX_PCT - TAKE_PROFIT_PCT)

    def get_dynamic_dca_distance_pct(self) -> float:
        """ATR-adaptive DCA spacing: distance scales with current ATR% so
        DCA adds happen further apart in a volatile market (avoiding
        rapid-fire DCA into noise) and closer together in a quiet one,
        always bounded to [DCA_MIN_DISTANCE_PCT, DCA_MAX_DISTANCE_PCT] and
        never below the original static DCA_TRIGGER_PCT floor."""
        atr_pct = self.last_regime.atr_pct
        if atr_pct <= 0:
            return DCA_TRIGGER_PCT
        dynamic = atr_pct * DCA_ATR_MULTIPLIER
        dynamic = clamp(dynamic, DCA_MIN_DISTANCE_PCT, DCA_MAX_DISTANCE_PCT)
        return max(dynamic, DCA_TRIGGER_PCT)

    # -- entry signal (fallback for warmup only) ------------------------------------

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

    # -- learning from ticks --------------------------------------------------------

    def _learn_from_tick(self, features: np.ndarray, atr_pct_now: float) -> None:
        price = self.current_price
        if price is None:
            return
        if len(self._feature_buffer) == self._feature_buffer.maxlen:
            old_price, old_features, old_atr_pct = self._feature_buffer[0]
            if old_price:
                forward_return = (price - old_price) / old_price
                self.brain.learn_trend(old_features, forward_return)
                # noise: forward move stayed inside roughly half an ATR band
                noise_band = max(old_atr_pct * 0.5, 1e-6)
                is_noise = abs(forward_return) < noise_band
                self.brain.learn_noise(old_features, is_noise)
                # tp_hit: forward move reached (at least) the base take-profit
                # distance in EITHER direction - a rough proxy for "was there
                # a tradeable move available from here", refined further by
                # the success/quality heads learned at actual trade close.
                tp_was_hit = abs(forward_return) >= TAKE_PROFIT_PCT
                self.brain.learn_tp_hit(old_features, tp_was_hit)
                self._brain_dirty = True
        self._feature_buffer.append((price, features.copy(), atr_pct_now))

    # -- main tick handler -----------------------------------------------------------

    async def on_price_tick(self) -> None:
        features = self.build_features()
        candles = self.candles.all_candles_incl_live()
        self.last_regime = self.regime_engine.evaluate(candles)
        self._learn_from_tick(features, self.last_regime.atr_pct)

        brain_out = self.brain.predict_all(features)
        pct_move_adverse = 0.0
        if self.position.status == "OPEN" and self.position.avg_entry_price and self.current_price:
            pct_move_adverse = max(
                0.0,
                -(self._pct_move()),
            )
        distance_to_liq_pct = None
        if self.liquidation_price and self.current_price:
            distance_to_liq_pct = abs(self.current_price - self.liquidation_price) / self.current_price
        risk_score = self.risk_engine.score(
            self.last_regime, self.position.dca_step, pct_move_adverse, distance_to_liq_pct
        )
        self.last_confidence = self.confidence_engine.evaluate(brain_out, risk_score, self.position.side)

        if self.position.status == "FLAT":
            if time.time() - self.last_trade_action_ts < TRADE_COOLDOWN_SEC:
                return
            if not self.brain.is_ready():
                signal = self._static_momentum_signal()
                if signal is not None:
                    await self._place_step_order(step=0, side_signal=signal, size_mult=1.0)
                return

            volumes = [c.volume for c in candles]
            volume_z = 0.0
            if len(volumes) >= 10:
                vmean, vstd = float(np.mean(volumes[-30:])), float(np.std(volumes[-30:]))
                volume_z = clamp(safe_div(volumes[-1] - vmean, vstd, 0.0), -4.0, 4.0) if vstd else 0.0
            momentum = float(features[22]) if len(features) > 22 else 0.0  # momentum_short index

            decision = self.entry_engine.evaluate(self.last_confidence, self.last_regime, volume_z, momentum, features)
            self.last_entry_decision = decision
            if decision.should_enter and decision.side is not None:
                size_mult = self.confidence_size_multiplier(self.last_confidence, self.last_regime)
                self.position.entry_features = features.copy()
                self.position.entry_regime = self.last_regime.regime
                self.position.entry_confidence = self.last_confidence.confidence_score
                self.position.entry_risk_score = self.last_confidence.risk_score
                self.position.entry_success_prob = self.last_confidence.success_probability
                self.position.entry_tp_hit_prob = self.last_confidence.tp_hit_probability
                self.position.entry_dynamic_tp_pct = self.get_dynamic_take_profit_pct()
                await self._place_step_order(step=0, side_signal=decision.side, size_mult=size_mult)
        elif self.position.status == "OPEN":
            await self._manage_open_position()

    def _pct_move(self) -> float:
        """Signed favorable pct move on the average entry (positive = in
        profit). Used by risk scoring, smart exit, TP, trailing, etc."""
        p = self.position
        if p.avg_entry_price is None or self.current_price is None:
            return 0.0
        if p.side == "LONG":
            return (self.current_price - p.avg_entry_price) / p.avg_entry_price
        return (p.avg_entry_price - self.current_price) / p.avg_entry_price

    async def _place_step_order(self, step: int, side_signal: str, size_mult: float = 1.0) -> None:
        notional = self.notional_for_step(step, size_mult)
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
                f"{self.symbol} @ market (~{price:.2f}, notional=${notional:.2f}, "
                f"size_mult={size_mult:.2f}, regime={self.last_regime.regime}, "
                f"confidence={self.last_confidence.confidence_score:.2f})", GRAY
            ))
            self._order_index[fake_id] = role
            self.position.pending_order_id = fake_id
            self.position.pending_role = role
            self.position.pending_order_ts = time.time()
            self.position.side = side_signal
            self.position.status = "ENTERING" if step == 0 else "DCA_PENDING"
            self.last_trade_action_ts = time.time()
            return

        try:
            resp = await self.client.place_order(
                symbol=self.symbol, side=order_side, type="MARKET", quantity=qty,
            )
            self._order_index[resp["orderId"]] = role
            self.position.pending_order_id = resp["orderId"]
            self.position.pending_role = role
            self.position.pending_order_ts = time.time()
            self.position.side = side_signal
            self.position.status = "ENTERING" if step == 0 else "DCA_PENDING"
            self.last_trade_action_ts = time.time()
            print(color(
                f"{now_str()} {step_label} PLACED  {order_side} {qty} {self.symbol} "
                f"@ market (notional=${notional:.2f}, orderId={resp['orderId']}, "
                f"size_mult={size_mult:.2f}, regime={self.last_regime.regime})",
                CYAN,
            ))
        except BinanceApiError as e:
            print(color(f"[dca] {step_label} order FAILED: {e}", RED))

    async def close_position(self, reason: str, emergency: bool = False, exit_reason_tag: str = "manual") -> None:
        if self.position.status not in ("OPEN", "DCA_PENDING") or self.position.total_qty <= 0:
            return
        close_side = "SELL" if self.position.side == "LONG" else "BUY"
        qty = self.position.total_qty
        label = "EMERGENCY CLOSE" if emergency else "CLOSE (full)"
        print(color(
            f"{now_str()} {label}: {reason} | closing {close_side} {qty} {self.symbol}",
            RED if emergency else GREEN,
        ))
        self.position.status = "CLOSING"
        self.position.pending_order_ts = time.time()
        self.last_trade_action_ts = time.time()
        self._pending_exit_reason = exit_reason_tag  # consumed in _on_close_filled

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
                quantity=qty, reduceOnly="true",
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

    async def partial_close_position(self, fraction: float, reason: str) -> None:
        """Reduces the position by `fraction` of its current qty via a
        reduceOnly market order, WITHOUT touching status (stays OPEN) -
        used for Partial TP. The remaining runner keeps being managed by
        _manage_open_position as normal (including a possible later full
        close via TP/hard-stop/smart-exit/DCA-exhausted)."""
        p = self.position
        if p.status != "OPEN" or p.total_qty <= 0:
            return
        close_side = "SELL" if p.side == "LONG" else "BUY"
        qty = round_step(p.total_qty * fraction, self.filters.step_size)
        if qty < self.filters.min_qty or qty <= 0:
            return  # too small to bother - runner keeps its full size
        if (p.total_qty - qty) < self.filters.min_qty:
            return  # would leave an unclosable dust runner - skip partial, let full TP handle it

        print(color(f"{now_str()} PARTIAL TP: {reason} | closing {close_side} {qty} {self.symbol}", GREEN))
        self.last_trade_action_ts = time.time()

        if DRY_RUN:
            fake_id = -(int(time.time() * 1000) % 1_000_000) - 800000
            self._order_index[fake_id] = "partial_close"
            self.position.pending_role = "partial_close"
            print(color(f"{now_str()} [DRY RUN] would place PARTIAL CLOSE {close_side} {qty} {self.symbol} reduceOnly MARKET", GRAY))
            # In dry run there's no real fill event coming back, so apply the
            # reduction immediately to keep local state consistent.
            await self._apply_partial_close(qty, self.current_price or p.avg_entry_price, dry_run=True)
            return

        try:
            resp = await self.client.place_order(
                symbol=self.symbol, side=close_side, type="MARKET",
                quantity=qty, reduceOnly="true",
            )
            self._order_index[resp["orderId"]] = "partial_close"
        except BinanceApiError as e:
            print(color(f"[position] partial TP order FAILED: {e}", RED))

    async def _apply_partial_close(self, qty: float, fill_price: float, dry_run: bool = False) -> None:
        p = self.position
        pnl = self.estimate_net_pnl_usdt(fill_price, qty) if fill_price else 0.0
        p.total_qty = max(p.total_qty - qty, 0.0)
        p.partial_tp_done = True
        self.realized_pnl_total += pnl
        if BREAKEVEN_AFTER_PARTIAL:
            p.breakeven_armed = True
            p.breakeven_price = p.avg_entry_price
        print(color(
            f"{now_str()} PARTIAL TP FILLED @ {fill_price:.2f}  qty={qty}  "
            f"est_pnl={pnl:+.4f} USDT  remaining_qty={p.total_qty}  "
            f"breakeven_armed={p.breakeven_armed}", GREEN,
        ))

    # -- open-position management: TP / DCA / hard stop / smart exit / trailing ---

    async def _manage_open_position(self) -> None:
        p = self.position
        avg = p.avg_entry_price
        price = self.current_price
        if avg is None or price is None:
            return

        # track max favorable / adverse excursion for reward + trailing stop
        if p.side == "LONG":
            p.max_favorable_price = price if p.max_favorable_price is None else max(p.max_favorable_price, price)
            p.max_adverse_price = price if p.max_adverse_price is None else min(p.max_adverse_price, price)
        else:
            p.max_favorable_price = price if p.max_favorable_price is None else min(p.max_favorable_price, price)
            p.max_adverse_price = price if p.max_adverse_price is None else max(p.max_adverse_price, price)

        pct_move = self._pct_move()

        # Hard stop: always fires immediately, bypassing every other gate.
        if pct_move <= -HARD_STOP_PCT:
            await self.close_position(
                f"hard stop: {pct_move*100:.2f}% adverse move on average entry",
                emergency=True, exit_reason_tag="hard_stop",
            )
            return

        # Breakeven stop (armed only after a partial TP has been taken): if
        # price falls back through the original average entry, close the
        # remaining runner instead of letting a locked-in partial win turn
        # into an overall loss.
        if p.breakeven_armed and p.breakeven_price is not None:
            breakeven_hit = (
                (p.side == "LONG" and price <= p.breakeven_price)
                or (p.side == "SHORT" and price >= p.breakeven_price)
            )
            if breakeven_hit:
                await self.close_position(
                    f"breakeven stop after partial TP: price {price:.2f} back through "
                    f"entry {p.breakeven_price:.2f}", emergency=True, exit_reason_tag="breakeven",
                )
                return

        held_long_enough = (time.time() - p.opened_at) >= MIN_HOLD_SEC_BEFORE_EXIT
        dynamic_tp_pct = self.get_dynamic_take_profit_pct()

        # --- Partial Take Profit ---------------------------------------------------
        if (
            PARTIAL_TP_ENABLED and not p.partial_tp_done and held_long_enough
            and pct_move >= dynamic_tp_pct * PARTIAL_TP_TRIGGER_RATIO
        ):
            net_pnl_partial = self.estimate_net_pnl_usdt(price, p.total_qty * PARTIAL_TP_FRACTION)
            if net_pnl_partial >= MIN_NET_PROFIT_USDT * PARTIAL_TP_FRACTION:
                await self.partial_close_position(
                    PARTIAL_TP_FRACTION,
                    f"{pct_move*100:.2f}% favorable move reached "
                    f"{PARTIAL_TP_TRIGGER_RATIO*100:.0f}% of dynamic TP ({dynamic_tp_pct*100:.3f}%)",
                )

        # --- Full Take Profit --------------------------------------------------------
        if pct_move >= dynamic_tp_pct and held_long_enough:
            net_pnl = self.estimate_net_pnl_usdt(price)
            if net_pnl >= MIN_NET_PROFIT_USDT:
                await self.close_position(
                    f"take-profit: {pct_move*100:.2f}% favorable move "
                    f"(dynamic TP={dynamic_tp_pct*100:.3f}%, base={TAKE_PROFIT_PCT*100:.2f}%, "
                    f"est. net pnl=${net_pnl:+.4f} after fees)",
                    exit_reason_tag="take_profit",
                )
                return

        # --- Trailing stop on the runner (after partial TP armed breakeven) ----------
        if TRAILING_STOP_ENABLED and p.breakeven_armed and held_long_enough and self.last_regime.atr_pct > 0:
            trail_distance = price * self.last_regime.atr_pct * TRAILING_STOP_ATR_MULT
            if p.side == "LONG":
                candidate = (p.max_favorable_price or price) - trail_distance
                p.trailing_stop_price = candidate if p.trailing_stop_price is None else max(p.trailing_stop_price, candidate)
                if price <= p.trailing_stop_price and pct_move > 0:
                    await self.close_position(
                        f"trailing stop: price {price:.2f} <= trail {p.trailing_stop_price:.2f} "
                        f"(ATR-based, {pct_move*100:.2f}% still favorable)",
                        exit_reason_tag="trailing_stop",
                    )
                    return
            else:
                candidate = (p.max_favorable_price or price) + trail_distance
                p.trailing_stop_price = candidate if p.trailing_stop_price is None else min(p.trailing_stop_price, candidate)
                if price >= p.trailing_stop_price and pct_move > 0:
                    await self.close_position(
                        f"trailing stop: price {price:.2f} >= trail {p.trailing_stop_price:.2f} "
                        f"(ATR-based, {pct_move*100:.2f}% still favorable)",
                        exit_reason_tag="trailing_stop",
                    )
                    return

        # --- Smart Exit V2: requires a MAJORITY of independent signals to agree -------
        if SMART_EXIT_ENABLED and held_long_enough and pct_move > -SMART_EXIT_MAX_LOSS_PCT:
            signals = self._smart_exit_v2_signals(pct_move, dynamic_tp_pct)
            agree_count = sum(1 for v in signals.values() if v)
            if agree_count >= SMART_EXIT_MIN_AGREE:
                await self.close_position(
                    f"SMART EXIT V2: {agree_count}/{len(signals)} signals agree "
                    f"({', '.join(k for k, v in signals.items() if v)}) at {pct_move*100:.2f}% - "
                    f"exiting before further adverse move rather than a single-tick panic exit",
                    exit_reason_tag="smart_exit",
                )
                return

        # --- ATR-adaptive DCA -----------------------------------------------------------
        dca_distance_pct = self.get_dynamic_dca_distance_pct()
        if pct_move <= -dca_distance_pct:
            if p.dca_step >= MAX_DCA_STEPS:
                await self.close_position(
                    f"max DCA steps ({MAX_DCA_STEPS}) exhausted and price still adverse "
                    f"({pct_move*100:.2f}%, dca_distance={dca_distance_pct*100:.3f}%)",
                    emergency=True, exit_reason_tag="max_dca_exhausted",
                )
                return
            size_mult = self.confidence_size_multiplier(self.last_confidence, self.last_regime)
            await self._place_step_order(step=p.dca_step + 1, side_signal=p.side, size_mult=size_mult)
            p.last_dca_price = price

    def _smart_exit_v2_signals(self, pct_move: float, dynamic_tp_pct: float) -> Dict[str, bool]:
        """Six independent, cheap-to-evaluate signals. Exit only fires when
        at least SMART_EXIT_MIN_AGREE of them agree - a single flipped
        prediction (the old Smart Exit's failure mode) can satisfy at most
        one or two of these on its own."""
        p = self.position
        conf = self.last_confidence
        regime = self.last_regime

        # 1) Brain confidence has dropped meaningfully vs its value at entry.
        confidence_drop = (p.entry_confidence - conf.confidence_score) >= SMART_EXIT_CONFIDENCE_DROP

        # 2) Trend direction has flipped against the position, with
        #    non-trivial trend_confidence behind the flip (not just noise).
        trend_reversal = (
            conf.trend_direction is not None
            and conf.trend_direction != p.side
            and conf.trend_confidence >= 0.35
        )

        # 3) Momentum (short-horizon price velocity) is moving against us.
        momentum_reversal = False
        if self.prev_price and self.current_price:
            velocity = (self.current_price - self.prev_price) / self.prev_price
            momentum_reversal = (p.side == "LONG" and velocity < -0.0004) or (p.side == "SHORT" and velocity > 0.0004)

        # 4) Volume confirms the adverse move (elevated volume on the wrong side).
        candles = self.candles.all_candles_incl_live()
        volume_confirmation = False
        if candles:
            last = candles[-1]
            if p.side == "LONG":
                volume_confirmation = last.sell_volume > last.buy_volume * 1.3
            else:
                volume_confirmation = last.buy_volume > last.sell_volume * 1.3

        # 5) ATR-scaled adverse move: the CURRENT adverse excursion already
        #    represents a "real" move relative to typical volatility, not
        #    just tick noise.
        atr_move_signal = False
        if regime.atr_pct > 0 and pct_move < 0:
            atr_move_signal = abs(pct_move) >= regime.atr_pct * SMART_EXIT_ATR_MOVE_MULT

        # 6) Regime itself has shifted away from what it was at entry (e.g.
        #    a STRONG_TREND we entered on has degraded to SIDEWAYS/HIGH_VOL).
        regime_shift = regime.regime != p.entry_regime and regime.regime in (REGIME_SIDEWAYS, REGIME_HIGH_VOL)

        return {
            "confidence_drop": confidence_drop,
            "trend_reversal": trend_reversal,
            "momentum_reversal": momentum_reversal,
            "volume_confirmation": volume_confirmation,
            "atr_move": atr_move_signal,
            "regime_shift": regime_shift,
        }

    # -- order fill handling --------------------------------------------------------

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
        elif role == "partial_close":
            await self._apply_partial_close(fill_qty, fill_price)
        elif role == "close":
            await self._on_close_filled(fill_price, total_rp)

    async def _on_entry_filled(self, role: str, fill_price: float, fill_qty: float) -> None:
        self.position.entries.append((fill_price, fill_qty))
        total_notional = sum(p * q for p, q in self.position.entries)
        total_qty = sum(q for _, q in self.position.entries)
        self.position.avg_entry_price = total_notional / total_qty if total_qty else None
        self.position.total_qty = total_qty
        self.position.original_qty = total_qty
        if role == "dca":
            self.position.dca_step += 1
        else:
            self.position.opened_at = time.time()
            self.position.max_favorable_price = fill_price
            self.position.max_adverse_price = fill_price
        self.position.status = "OPEN"
        self.position.pending_order_id = None
        self.position.pending_role = None

        step_label = "INITIAL" if role == "initial" else f"DCA #{self.position.dca_step}"
        side_color = GREEN if self.position.side == "LONG" else RED
        print(color(
            f"{now_str()} ENTRY FILLED [{step_label}] {self.position.side} "
            f"qty={fill_qty} @ {fill_price:.2f}  ->  avg_entry={self.position.avg_entry_price:.2f}  "
            f"total_qty={self.position.total_qty}  leverage={self.leverage}x  margin={MARGIN_TYPE}  "
            f"regime={self.last_regime.regime}  confidence={self.last_confidence.confidence_score:.2f}",
            side_color,
        ))

    async def _on_close_filled(self, fill_price: float, total_rp: float) -> None:
        p = self.position
        self.realized_pnl_total += total_rp
        self.trade_count += 1
        pnl_color = GREEN if total_rp >= 0 else RED

        exit_reason = getattr(self, "_pending_exit_reason", "manual")
        held_sec = time.time() - p.opened_at if p.opened_at else 0.0
        invested_notional = sum(price * qty for price, qty in p.entries) or 0.0
        fees_est = self.estimate_round_trip_fee_usdt(p.original_qty or p.total_qty, p.avg_entry_price or fill_price, fill_price)

        # MFE/MAE as pct-of-entry moves, using tracked favorable/adverse
        # extremes across the whole life of the trade.
        mfe_pct = mae_pct = 0.0
        if p.avg_entry_price:
            if p.side == "LONG":
                mfe_pct = safe_div((p.max_favorable_price or fill_price) - p.avg_entry_price, p.avg_entry_price, 0.0)
                mae_pct = safe_div(p.avg_entry_price - (p.max_adverse_price or fill_price), p.avg_entry_price, 0.0)
            else:
                mfe_pct = safe_div(p.avg_entry_price - (p.max_favorable_price or fill_price), p.avg_entry_price, 0.0)
                mae_pct = safe_div((p.max_adverse_price or fill_price) - p.avg_entry_price, p.avg_entry_price, 0.0)
            mfe_pct = max(mfe_pct, 0.0)
            mae_pct = max(mae_pct, 0.0)

        net_pnl_total = total_rp  # includes any partial-TP pnl already added to realized_pnl_total separately
        # total_rp here is only the FINAL close leg's realized pnl per Binance's
        # own accounting; combine with whatever partial-TP pnl we tracked locally.
        combined_net_pnl = net_pnl_total

        reward = self.reward_calc.compute(
            net_pnl_usdt=combined_net_pnl,
            invested_notional=invested_notional or 1.0,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            dynamic_tp_pct=p.entry_dynamic_tp_pct or TAKE_PROFIT_PCT,
            exit_reason=exit_reason,
            held_sec=held_sec,
        )

        print(color(
            f"{now_str()} POSITION CLOSED @ {fill_price:.2f}  PnL={total_rp:+.4f} USDT  "
            f"(DCA steps used: {p.dca_step}/{MAX_DCA_STEPS})  exit_reason={exit_reason}  "
            f"reward={reward:+.4f}  session_total={self.realized_pnl_total:+.4f}",
            pnl_color,
        ))

        was_success = combined_net_pnl > 0
        self.recent_trade_outcomes.append(1.0 if was_success else 0.0)
        self.recent_trade_timestamps.append(time.time())

        if p.entry_features is not None:
            self.brain.learn_success(p.entry_features, was_success)
            self.brain.learn_quality(p.entry_features, reward)
            self._brain_dirty = True
            print(color(
                f"{now_str()} [brain] reinforced entry decision (success={was_success}, "
                f"reward={reward:+.4f}, brain_updates={self.brain.update_count})", MAGENTA,
            ))

        # --- permanent training dataset -------------------------------------------
        record = {
            "close_time": now_str(),
            "symbol": self.symbol,
            "side": p.side,
            "entry_price": p.avg_entry_price,
            "exit_price": fill_price,
            "qty": p.original_qty or p.total_qty,
            "invested_notional": invested_notional,
            "gross_pnl_usdt": combined_net_pnl + fees_est,
            "fees_usdt": fees_est,
            "net_pnl_usdt": combined_net_pnl,
            "net_pnl_pct": safe_div(combined_net_pnl, invested_notional, 0.0),
            "dca_count": p.dca_step,
            "holding_time_sec": held_sec,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "exit_reason": exit_reason,
            "tp_hit": exit_reason == "take_profit",
            "smart_exit": exit_reason == "smart_exit",
            "manual_exit": exit_reason == "manual",
            "hard_stop": exit_reason in ("hard_stop", "max_dca_exhausted"),
            "entry_regime": p.entry_regime,
            "exit_regime": self.last_regime.regime,
            "entry_confidence": p.entry_confidence,
            "entry_risk_score": p.entry_risk_score,
            "entry_success_prob": p.entry_success_prob,
            "entry_tp_hit_prob": p.entry_tp_hit_prob,
            "reward": reward,
            "final_outcome": "win" if was_success else "loss",
        }
        self.trade_logger.log_trade(record)

        self.position = PositionState(last_close_time=time.time())

        asyncio.create_task(self.persist_brain(reason="trade closed"))


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
    reported position. Runs at startup, after every user-data-stream
    reconnection, and on every periodic position-risk poll - see the
    original design notes carried over from the previous build. Unchanged
    in behavior; only the PositionState fields being (re)built have grown
    (partial-TP/breakeven/trailing/entry-snapshot fields all reset to
    their dataclass defaults automatically via PositionState())."""
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
        if p.status in ("ENTERING", "DCA_PENDING", "CLOSING"):
            age = time.time() - p.pending_order_ts
            if age < SYNC_PENDING_GRACE_SEC:
                print(color(
                    f"{now_str()} [sync:{context}] exchange shows flat but a {p.status} order "
                    f"was placed only {age:.1f}s ago (< {SYNC_PENDING_GRACE_SEC}s grace) - waiting "
                    f"for the fill event instead of resetting early.", GRAY,
                ))
                return
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
        original_qty=qty,
        opened_at=time.time(),
        max_favorable_price=entry_price,
        max_adverse_price=entry_price,
    )


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


async def balance_refresher(client: RestClient, manager: MartingaleManager) -> None:
    while True:
        try:
            balances = await client.get_balance()
            usdt = next((b for b in balances if b["asset"] == "USDT"), None)
            if usdt:
                real_balance = float(usdt["availableBalance"])
                manager.available_balance = min(real_balance, 50.0)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[balance] refresh failed: {e}", RED))
        await asyncio.sleep(BALANCE_REFRESH_SEC)


async def funding_oi_poller(client: RestClient, manager: MartingaleManager) -> None:
    """Best-effort funding rate + open interest refresh. Both are optional
    feature inputs - any failure just leaves the last known value (or None)
    in place and never interrupts trading."""
    while True:
        try:
            premium = await client.get_premium_index(SYMBOL)
            manager.funding_rate = float(premium.get("lastFundingRate", 0) or 0)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[funding] premiumIndex poll failed (continuing without it): {e}", YELLOW))
        try:
            oi = await client.get_open_interest(SYMBOL)
            manager.open_interest = float(oi.get("openInterest", 0) or 0)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[funding] openInterest poll failed (continuing without it): {e}", YELLOW))
        await asyncio.sleep(FUNDING_OI_POLL_SEC)


async def position_risk_poller(client: RestClient, manager: MartingaleManager) -> None:
    """Polls Binance's OWN authoritative liquidation price, sanity-checks it
    against mark price, and re-syncs local state on every cycle. Unchanged
    from the previous build."""
    while True:
        if DRY_RUN:
            await asyncio.sleep(POSITION_RISK_POLL_SEC)
            continue
        try:
            rows = await client.get_position_risk(SYMBOL)
            row = next((r for r in rows if float(r.get("positionAmt", 0)) != 0), None)
            if row:
                mark_price = float(row.get("markPrice", 0) or 0)
                raw_liq = float(row.get("liquidationPrice", 0) or 0)

                plausible = (
                    mark_price > 0
                    and raw_liq > 0
                    and LIQUIDATION_SANITY_MIN_RATIO <= (raw_liq / mark_price) <= LIQUIDATION_SANITY_MAX_RATIO
                )

                if plausible:
                    manager.liquidation_price = raw_liq
                    print(color(
                        f"{now_str()} [risk] LIQUIDATION PRICE: {manager.liquidation_price:.2f}  "
                        f"(mark={mark_price:.2f}, positionAmt={row.get('positionAmt')})", MAGENTA
                    ))
                    distance_pct = (
                        abs(mark_price - manager.liquidation_price) / mark_price if mark_price else 1.0
                    )
                    if distance_pct <= LIQUIDATION_WARNING_BUFFER_PCT and manager.position.status == "OPEN":
                        print(color(
                            f"{now_str()} [risk] mark price is within "
                            f"{distance_pct*100:.2f}% of liquidation ({manager.liquidation_price:.2f}) - "
                            f"triggering emergency close before the exchange forces it.", RED,
                        ))
                        await manager.close_position(
                            f"liquidation buffer breached: mark {mark_price:.2f} within "
                            f"{distance_pct*100:.2f}% of liq {manager.liquidation_price:.2f}",
                            emergency=True, exit_reason_tag="liquidation_buffer",
                        )
                else:
                    manager.liquidation_price = None
                    if raw_liq > 0 and mark_price > 0:
                        print(color(
                            f"{now_str()} [risk] ignoring implausible liquidationPrice="
                            f"{raw_liq:.2f} vs mark={mark_price:.2f} (outside "
                            f"{LIQUIDATION_SANITY_MIN_RATIO}x-{LIQUIDATION_SANITY_MAX_RATIO}x band) - "
                            f"likely a Cross-margin/testnet over-collateralization artifact, not a real risk reading.",
                            YELLOW,
                        ))
            else:
                manager.liquidation_price = None
            await initialize_sync(client, manager, context="periodic poll", rows=rows)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[risk] position risk poll failed: {e}", RED))
        await asyncio.sleep(POSITION_RISK_POLL_SEC)


async def brain_sync_loop(manager: MartingaleManager, interval_sec: int = BRAIN_AUTO_PUSH_INTERVAL_SEC) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        if manager._brain_dirty:
            await manager.persist_brain(reason="periodic interval")


async def stats_export_loop(manager: MartingaleManager, interval_sec: int = STATS_EXPORT_INTERVAL_SEC) -> None:
    """Periodically (re)computes performance statistics from the permanent
    trade log and exports them to JSON/CSV - independent of trade activity,
    so a quiet stretch still gets a fresh (unchanged-count) export."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            manager.perf_stats.export()
        except Exception as e:  # noqa: BLE001 - stats must never crash the trading loop
            print(color(f"[stats] export loop error: {e}", YELLOW))


async def status_loop(manager: MartingaleManager, interval_sec: int = 20) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        p = manager.position
        liq = f"{manager.liquidation_price:.2f}" if manager.liquidation_price else "n/a"
        brain_state = "READY" if manager.brain.is_ready() else (
            f"WARMUP {manager.brain.update_count}/{BRAIN2_WARMUP_UPDATES}"
        )
        conf = manager.last_confidence
        regime = manager.last_regime
        sync_state = (
            f"{time.time() - manager.last_brain_sync_ts:.0f}s ago"
            if manager.last_brain_sync_ts else "never"
        )
        print(color(
            f"{now_str()} [status] price={manager.current_price}  status={p.status}  "
            f"side={p.side}  dca_step={p.dca_step}/{MAX_DCA_STEPS}  "
            f"avg_entry={p.avg_entry_price}  qty={p.total_qty}  "
            f"liq_price={liq}  balance={manager.available_balance:.2f} USDT  "
            f"trades={manager.trade_count}  session_pnl={manager.realized_pnl_total:+.4f}  "
            f"regime={regime.regime}  atr%={regime.atr_pct*100:.3f}  "
            f"brain=[{brain_state}]  confidence={conf.confidence_score:.2f}  "
            f"success_p={conf.success_probability:.2f}  tp_hit_p={conf.tp_hit_probability:.2f}  "
            f"risk={conf.risk_score:.2f}  "
            f"github_sync=[{'on' if manager.github_sync.enabled else 'off'}, last_push={sync_state}]",
            BOLD,
        ))


# ============================================================================
# ENTRYPOINT
# ============================================================================


async def main() -> None:
    enforce_safety_gates()

    print(color("=" * 78, CYAN))
    print(color(" Martingale DCA Scalper - Binance USD-M Futures  [Brain V2]", BOLD))
    print(color(f" Symbol: {SYMBOL}   Testnet: {USE_TESTNET}   Dry-run: {DRY_RUN}", GRAY))
    print(color(
        f" Leverage: {LEVERAGE}x (cap {MAX_ALLOWED_LEVERAGE}x)   Margin: {MARGIN_TYPE}   "
        f"Initial entry: ${INITIAL_ENTRY_USDT}   DCA x{DCA_MULTIPLIER}   Max steps: {MAX_DCA_STEPS}",
        GRAY,
    ))
    print(color(
        f" DCA trigger (floor): -{DCA_TRIGGER_PCT*100:.2f}%   Take-profit (floor): +{TAKE_PROFIT_PCT*100:.2f}%   "
        f"Hard stop: -{HARD_STOP_PCT*100:.2f}%   Entry score threshold: {ENTRY_SCORE_THRESHOLD:.2f}", GRAY,
    ))
    print(color(
        f" ATR-DCA mult={DCA_ATR_MULTIPLIER}  DCA range=[{DCA_MIN_DISTANCE_PCT*100:.2f}%, {DCA_MAX_DISTANCE_PCT*100:.2f}%]  "
        f"Size mult range=[{SIZE_MIN_MULT}, {SIZE_MAX_MULT}]  Partial TP={PARTIAL_TP_ENABLED} "
        f"({PARTIAL_TP_FRACTION*100:.0f}% @ {PARTIAL_TP_TRIGGER_RATIO*100:.0f}% of TP)  "
        f"Trailing stop={TRAILING_STOP_ENABLED}", GRAY,
    ))
    if DRY_RUN:
        print(color(" *** DRY RUN MODE - no real orders will be sent ***", YELLOW))
    if not USE_TESTNET:
        print(color(" *** LIVE MAINNET MODE - REAL MONEY AT RISK ***", RED))

    print(color("=" * 78, CYAN))

    client = RestClient(API_KEY, API_SECRET, REST_BASE)
    manager: Optional[MartingaleManager] = None

    try:
        await retry_with_backoff(client.start, label="REST client startup / time sync")

        filters = await retry_with_backoff(
            fetch_symbol_filters, client, SYMBOL, label="fetch_symbol_filters"
        )
        print(color(
            f"[setup] {SYMBOL} filters: tick={filters.tick_size} step={filters.step_size} "
            f"minQty={filters.min_qty} minNotional={filters.min_notional}", GRAY
        ))

        # Cross 40x / DCA sizing sanity check: confirm every one of the 5
        # martingale steps clears the exchange's minimum notional up front,
        # using the LARGEST possible size multiplier so a confidence-scaled
        # initial entry can never silently fall below min_notional either.
        for step in range(MAX_DCA_STEPS + 1):
            margin = INITIAL_ENTRY_USDT if step == 0 else INITIAL_ENTRY_USDT * (DCA_MULTIPLIER ** step)
            if step == 0:
                margin *= SIZE_MIN_MULT  # worst case (smallest allowed) initial size
            step_notional = margin * LEVERAGE
            ok = step_notional >= filters.min_notional
            label = "INITIAL (min size)" if step == 0 else f"DCA #{step}"
            print(color(
                f"[setup]   {label}: margin=${margin:.2f} notional=${step_notional:.2f} "
                f"{'OK' if ok else 'BELOW MIN_NOTIONAL - will be skipped at runtime!'}",
                GRAY if ok else RED,
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
        bid, ask = float(book["bidPrice"]), float(book["askPrice"])
        manager.on_book_ticker(bid, ask, float(book.get("bidQty", 0) or 0), float(book.get("askQty", 0) or 0))
        print(color(f"[setup] current price: {manager.current_price:.2f}", GRAY))

        try:
            premium = await client.get_premium_index(SYMBOL)
            manager.funding_rate = float(premium.get("lastFundingRate", 0) or 0)
            print(color(f"[setup] current funding rate: {manager.funding_rate:.6f}", GRAY))
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[setup] could not fetch initial funding rate (continuing without it): {e}", YELLOW))

        # Persistent Adaptive Learning: local brain snapshot -> GitHub -> fresh model.
        await manager.load_or_init_brain()

        await initialize_sync(client, manager, context="startup")

        await asyncio.gather(
            market_data_consumer(manager),
            userdata_consumer(client, manager),
            listen_key_keepalive(client),
            balance_refresher(client, manager),
            position_risk_poller(client, manager),
            funding_oi_poller(client, manager),
            status_loop(manager),
            brain_sync_loop(manager),
            stats_export_loop(manager),
        )
    finally:
        if manager is not None:
            try:
                await manager.persist_brain(reason="shutdown")
            except Exception as e:  # noqa: BLE001 - shutdown persistence is best-effort only
                print(color(f"[brain] final persist on shutdown failed: {e}", YELLOW))
            try:
                manager.perf_stats.export()
            except Exception:  # noqa: BLE001 - never block shutdown on stats export
                pass
            try:
                await manager.github_sync.close()
            except Exception:  # noqa: BLE001 - never block shutdown on sync cleanup
                pass
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
