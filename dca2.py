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
import base64
import hashlib
import hmac
import json
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
TAKE_PROFIT_PCT = 0.003        # base / floor TP - used as-is in quiet markets
HARD_STOP_PCT = 0.05

# --- Dynamic (volatility-based) Take Profit ----------------------------------
# When the market is choppy/trending hard, a fixed 0.3% TP leaves money on the
# table; when it's dead quiet, a wider TP just means longer exposure for no
# extra edge. This lets TP breathe with realized volatility while never going
# below the original static TAKE_PROFIT_PCT.
DYNAMIC_TP_ENABLED = True
TAKE_PROFIT_MAX_PCT = 0.006     # hard ceiling - TP will never expand past this
TP_VOL_LOW = 0.0003             # tick-return std at/below this -> quiet -> base TP
TP_VOL_HIGH = 0.0012            # tick-return std at/above this -> max TP expansion

# --- Simple entry signal (warmup/fallback only, see BRAIN_* below) ----------
SIGNAL_LOOKBACK_TICKS = 20
SIGNAL_DEADBAND_PCT = 0.0005

# --- Over-trading guardrails --------------------------------------------------
# Root cause of "rapid-fire" churn: nothing previously stopped a fresh entry
# the instant the bot went FLAT again, and nothing stopped a discretionary
# (non-emergency) exit the instant it became even marginally profitable -
# so on a choppy tape the bot could open/close within seconds, paying taker
# fees on both legs each time. These two knobs fix that directly:
#   1. TRADE_COOLDOWN_SEC gates any NEW trade (entry, DCA add, or a
#      discretionary close) for this long after the LAST trade action of
#      any kind - tracked in MartingaleManager.last_trade_action_ts, not
#      just PositionState.last_close_time (which only covered re-entries).
#   2. MIN_HOLD_SEC_BEFORE_EXIT additionally requires a position to have
#      been open at least this long before take-profit/smart-exit are
#      allowed to fire - hard stop and max-DCA-exhausted emergency closes
#      always bypass BOTH of these, on purpose: safety exits must never be
#      delayed by a cooldown meant to stop over-trading.
TRADE_COOLDOWN_SEC = int(os.environ.get("TRADE_COOLDOWN_SEC", "60"))
MIN_HOLD_SEC_BEFORE_EXIT = int(os.environ.get("MIN_HOLD_SEC_BEFORE_EXIT", "60"))

# --- Fee-aware profit threshold ----------------------------------------------
# A take-profit (or smart-exit) that only just covers the round-trip taker
# fee is a losing trade after costs. Require net PnL, after an estimated
# round-trip fee, to clear a minimum before a DISCRETIONARY close fires.
# Emergency closes (hard stop, max-DCA-exhausted) ignore this - safety
# always takes priority over a fee-optimal exit.
TAKER_FEE_RATE = float(os.environ.get("TAKER_FEE_RATE", "0.0005"))   # per fill, on notional; check your actual VIP tier
MIN_NET_PROFIT_USDT = float(os.environ.get("MIN_NET_PROFIT_USDT", "0.05"))

# --- Liquidation-price sanity check -------------------------------------------
# liquidationPrice comes straight from Binance's own /fapi/v2/positionRisk -
# this bot does not compute it. But on Cross margin, a tiny position sitting
# on top of a much larger wallet balance can legitimately produce a
# liquidation price that is mathematically valid yet absurdly far from the
# mark price (or, on testnet, backed by inflated fake balances). Rather than
# reimplement Binance's tiered maintenance-margin formula ourselves (a
# guaranteed source of NEW bugs), we sanity-check the reported value against
# mark price and refuse to trust/display/act on anything outside a plausible
# band.
LIQUIDATION_SANITY_MIN_RATIO = 0.2   # liq price below 20% of mark = implausible, discard
LIQUIDATION_SANITY_MAX_RATIO = 5.0   # liq price above 5x mark = implausible, discard
LIQUIDATION_WARNING_BUFFER_PCT = float(os.environ.get("LIQUIDATION_WARNING_BUFFER_PCT", "0.15"))

# --- State reconciliation grace period ----------------------------------------
# A market order can fill in milliseconds, but Binance's positionRisk
# snapshot (polled every POSITION_RISK_POLL_SEC) can lag the fill by a beat.
# Without a grace window, that lag race let initialize_sync() see "exchange
# still flat" for an order that was, in fact, already filling, force-reset
# local state to FLAT, and let on_price_tick() open a SECOND, duplicate
# entry right on top of the first - a direct cause of rapid-fire duplicate
# trades. Skip forced resets on a still-pending order/close until this many
# seconds have passed since it was placed.
SYNC_PENDING_GRACE_SEC = int(os.environ.get("SYNC_PENDING_GRACE_SEC", "8"))

# --- Online Learning Brain (SGDRegressor, partial_fit only, no stored data) --
FEATURE_SHORT_LOOKBACK = 5
LABEL_HORIZON_TICKS = 10
BRAIN_WARMUP_UPDATES = 50
PREDICTION_DEADBAND = 0.00015
# Features: momentum_short, momentum_long, volatility, side_encoded,
# unrealized_pnl, dca_ratio, price_velocity, order_book_imbalance,
# recent_win_rate - see MartingaleManager.get_features().
N_FEATURES = 9
RECENT_TRADE_WINDOW = 20   # trailing window used for the "historical success pattern" feature

# --- Persistent Adaptive Learning (Cloud-Sync Brain) -------------------------
# Railway's filesystem is ephemeral - anything not pushed off-box is lost on
# every redeploy/restart. brain.pkl is the on-disk snapshot of the online
# model (weights + running feature-normalizer stats); it is written locally
# after every trade / at a fixed interval, then best-effort pushed to a
# GitHub repo so a fresh container can pull it back down instead of starting
# from a cold, unwarmed model every time it restarts.
BRAIN_LOCAL_PATH = os.environ.get("BRAIN_LOCAL_PATH", "brain.pkl")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")           # fine-grained PAT, "Contents: read/write"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")             # "your-username/your-repo"
GITHUB_BRAIN_PATH = os.environ.get("GITHUB_BRAIN_PATH", "brain.pkl")  # path *inside* that repo
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
BRAIN_AUTO_PUSH_INTERVAL_SEC = int(os.environ.get("BRAIN_AUTO_PUSH_INTERVAL_SEC", "300"))

# --- Smart Exit / Damage Control (brain-driven early exit) -------------------
# If the brain's own live prediction flips hard against an open position
# before HARD_STOP_PCT is ever reached, this exits immediately at breakeven
# / minimal loss instead of riding the position down to the stop or, worse,
# toward liquidation.
SMART_EXIT_ENABLED = os.environ.get("SMART_EXIT_ENABLED", "true").lower() != "false"
# Baseline threshold, used AS-IS in high-volatility markets. Previously
# 0.00025 - too close to PREDICTION_DEADBAND (0.00015) to filter out normal
# single-tick noise, which is what made Smart Exit fire on minor wiggles.
SMART_EXIT_REVERSAL_THRESHOLD = 0.0006
SMART_EXIT_MAX_LOSS_PCT = 0.01            # only fires while still in "minor loss" territory

# Dynamic sensitivity: in a quiet market (low realized volatility), the SAME
# raw prediction magnitude is proportionally much more likely to be noise
# than signal, so the effective threshold scales UP (more patient) as
# volatility drops toward TP_VOL_LOW, and relaxes back down to the plain
# SMART_EXIT_REVERSAL_THRESHOLD ("remain as is") at/above TP_VOL_HIGH. Reuses
# the same TP_VOL_LOW/TP_VOL_HIGH regime bands as the dynamic take-profit
# logic so "quiet" and "volatile" mean the same thing everywhere in the bot.
SMART_EXIT_VOL_PATIENCE_MULT = 3.0    # threshold multiplier applied in the quietest markets

# Confidence / persistence filter: a single noisy tick must never be enough
# to close a trade. The reversal condition has to hold for a MAJORITY of the
# last SMART_EXIT_CONFIRM_TICKS predictions, not just the latest one.
SMART_EXIT_CONFIRM_TICKS = 5
SMART_EXIT_CONFIRM_RATIO = 0.7        # fraction of the recent window that must agree

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

    # -- persistence ("brain.pkl") --------------------------------------------
    # Only plain, picklable state is captured here - the sklearn model plus
    # the running mean/variance normalizer stats. This is what lets the bot
    # resume warmed-up instead of cold after a Railway restart.
    def to_state(self) -> dict:
        return {
            "n_features": self.n_features,
            "warmup_updates": self.warmup_updates,
            "model": self.model,
            "fitted": self.fitted,
            "update_count": self.update_count,
            "last_prediction": self.last_prediction,
            "_n_seen": self._n_seen,
            "_mean": self._mean,
            "_m2": self._m2,
        }

    def load_state(self, state: dict) -> None:
        self.model = state["model"]
        self.fitted = state["fitted"]
        self.update_count = state["update_count"]
        self.last_prediction = state.get("last_prediction")
        self._n_seen = state["_n_seen"]
        self._mean = state["_mean"]
        self._m2 = state["_m2"]

    def to_bytes(self) -> bytes:
        return pickle.dumps(self.to_state(), protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def from_bytes(cls, data: bytes, n_features: int, warmup_updates: int) -> "OnlineBrain":
        """Builds a brain from a serialized snapshot. Falls back to a fresh
        (cold) brain on any corruption or feature-shape mismatch - a bad or
        stale brain.pkl must never prevent the bot from starting."""
        brain = cls(n_features, warmup_updates)
        try:
            state = pickle.loads(data)
            if state.get("n_features") != n_features:
                print(color(
                    f"[brain] snapshot has n_features={state.get('n_features')}, code expects "
                    f"{n_features} - discarding snapshot and starting a fresh brain.", YELLOW,
                ))
                return brain
            brain.load_state(state)
        except Exception as e:  # noqa: BLE001 - corrupted/incompatible snapshot must not crash startup
            print(color(f"[brain] failed to deserialize snapshot ({e}), starting fresh.", YELLOW))
            return cls(n_features, warmup_updates)
        return brain


# ============================================================================
# CLOUD-SYNC BRAIN (push/pull brain.pkl to GitHub across ephemeral restarts)
# ============================================================================


class GithubBrainSync:
    """Best-effort sync of brain.pkl to a GitHub repo via the Contents API.

    Deliberately fails soft everywhere: any network/auth/API error is caught,
    logged, and swallowed - trading must never stop because GitHub is
    unreachable or misconfigured. If GITHUB_TOKEN/GITHUB_REPO aren't set,
    `enabled` is False and every method becomes a no-op, so the bot still
    runs fine on brain.pkl local-disk state alone (just without cross-
    restart persistence on a fully ephemeral host).
    """

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
                "[brain-sync] GITHUB_TOKEN / GITHUB_REPO not set - brain.pkl will persist "
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
        """Fetches the current brain.pkl bytes from GitHub, or None if it
        doesn't exist yet / sync is disabled / the call fails."""
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
        """Pushes brain.pkl bytes to GitHub, creating or updating the file as
        needed. Returns False (never raises) on any failure so callers can
        log-and-continue trading regardless of sync health."""
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
    pending_order_ts: float = 0.0    # when the current pending order/close was placed - sync grace period
    opened_at: float = 0.0           # when the position first went OPEN - minimum-hold-time gate
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
        self.last_trade_action_ts: float = 0.0  # last entry/DCA/close placement - drives TRADE_COOLDOWN_SEC

        self.brain = OnlineBrain(N_FEATURES)
        self._feature_buffer: deque[Tuple[float, np.ndarray]] = deque(
            maxlen=LABEL_HORIZON_TICKS + 1
        )
        self._entry_feature_snapshot: Optional[np.ndarray] = None

        # --- real-time feature ingestion inputs ---------------------------
        self.best_bid_qty: float = 0.0
        self.best_ask_qty: float = 0.0
        self.recent_trade_outcomes: deque[float] = deque(maxlen=RECENT_TRADE_WINDOW)
        self._recent_predictions: deque[float] = deque(maxlen=SMART_EXIT_CONFIRM_TICKS)

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
        """Startup logic: local brain.pkl wins if present (fastest, and
        reflects this exact container's own most recent state); otherwise
        pull the latest snapshot down from GitHub; otherwise fall back to
        the fresh OnlineBrain already created in __init__. Any failure at
        any stage just falls through to the next option / a cold brain -
        this must never prevent the bot from starting."""
        if os.path.exists(BRAIN_LOCAL_PATH):
            try:
                with open(BRAIN_LOCAL_PATH, "rb") as f:
                    data = f.read()
                self.brain = OnlineBrain.from_bytes(data, N_FEATURES, BRAIN_WARMUP_UPDATES)
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
            self.brain = OnlineBrain.from_bytes(remote, N_FEATURES, BRAIN_WARMUP_UPDATES)
            print(color(
                f"[brain] restored from GitHub ({GITHUB_REPO}/{GITHUB_BRAIN_PATH}) "
                f"(updates={self.brain.update_count}, ready={self.brain.is_ready()})", MAGENTA,
            ))
            return

        print(color(
            "[brain] no local or remote snapshot found - starting a fresh (cold) model.", GRAY
        ))

    async def persist_brain(self, reason: str) -> None:
        """Writes brain.pkl to local disk, then best-effort pushes it to
        GitHub. Called after every closed trade and on a fixed interval.
        Every failure mode here is caught and logged - a broken sync must
        never stop the bot from trading."""
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
                    f"{now_str()} [brain-sync] pushed brain.pkl to GitHub ({reason}, "
                    f"updates={self.brain.update_count})", MAGENTA,
                ))
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; upload() already catches internally
            print(color(f"[brain-sync] unexpected error during push (bot keeps trading): {e}", RED))
        self._brain_dirty = False

    def notional_for_step(self, step: int) -> float:
        margin = INITIAL_ENTRY_USDT if step == 0 else INITIAL_ENTRY_USDT * (DCA_MULTIPLIER ** step)
        return margin * self.leverage

    def estimate_round_trip_fee_usdt(self, qty: float, entry_price: float, exit_price: float) -> float:
        """Taker fee is charged on notional value on EACH fill (entry and
        exit), independent of leverage - so at 40x this is a much bigger
        bite out of margin than the headline rate suggests."""
        entry_notional = qty * entry_price
        exit_notional = qty * exit_price
        return TAKER_FEE_RATE * (entry_notional + exit_notional)

    def estimate_net_pnl_usdt(self, exit_price: float) -> float:
        """Gross PnL at `exit_price` on the current position, minus the
        estimated round-trip taker fee. Used to gate discretionary
        (non-emergency) closes so the bot doesn't lock in a "profit" that
        fees would immediately eat."""
        p = self.position
        if not p.avg_entry_price or p.total_qty <= 0:
            return 0.0
        if p.side == "LONG":
            gross = (exit_price - p.avg_entry_price) * p.total_qty
        else:
            gross = (p.avg_entry_price - exit_price) * p.total_qty
        fees = self.estimate_round_trip_fee_usdt(p.total_qty, p.avg_entry_price, exit_price)
        return gross - fees

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

        # --- Price Velocity: instantaneous tick-to-tick rate of change, as
        # distinct from the multi-tick momentum_short/momentum_long above.
        price_velocity = 0.0
        if len(hist) >= 2 and hist[-2]:
            price_velocity = (hist[-1] - hist[-2]) / hist[-2]

        # --- Order Book Imbalance: skew between resting bid/ask size at the
        # top of book, from the same bookTicker feed already driving price.
        # +1 => all resting size is on the bid (buy pressure), -1 => all on
        # the ask (sell pressure), 0 => balanced / no data yet.
        bid_qty, ask_qty = self.best_bid_qty, self.best_ask_qty
        book_total = bid_qty + ask_qty
        order_book_imbalance = (bid_qty - ask_qty) / book_total if book_total > 0 else 0.0

        # --- Historical success pattern: rolling win-rate over the last
        # RECENT_TRADE_WINDOW closed trades. 0.5 (neutral prior) until the
        # bot has actually closed any trades.
        recent_win_rate = (
            float(np.mean(self.recent_trade_outcomes)) if self.recent_trade_outcomes else 0.5
        )

        return np.array(
            [
                momentum_short, momentum_long, volatility, side_encoded, unrealized_pnl,
                dca_ratio, price_velocity, order_book_imbalance, recent_win_rate,
            ],
            dtype=float,
        )

    def get_dynamic_smart_exit_threshold(self) -> float:
        """Volatility-based dynamic Smart Exit sensitivity.

        - Quiet/sideways market (recent tick-return std <= TP_VOL_LOW): the
          threshold is scaled UP by SMART_EXIT_VOL_PATIENCE_MULT - the bot is
          more patient, because in a quiet market the same raw prediction
          magnitude is much more likely to just be noise.
        - High volatility (std >= TP_VOL_HIGH): threshold relaxes back down
          to the plain SMART_EXIT_REVERSAL_THRESHOLD - sensitivity "remains
          as is", since real trend changes are more plausible here and a
          large move is already underway.
        - In between: linear interpolation, same shape as
          get_dynamic_take_profit_pct(), and reusing the same TP_VOL_LOW/
          TP_VOL_HIGH bands so "quiet" and "volatile" mean the same thing
          everywhere in the bot.
        """
        vol = self._recent_tick_volatility()

        if vol <= TP_VOL_LOW:
            return SMART_EXIT_REVERSAL_THRESHOLD * SMART_EXIT_VOL_PATIENCE_MULT
        if vol >= TP_VOL_HIGH:
            return SMART_EXIT_REVERSAL_THRESHOLD

        vol_range = TP_VOL_HIGH - TP_VOL_LOW
        ratio = (vol - TP_VOL_LOW) / vol_range if vol_range > 0 else 0.0
        # ratio goes 0 -> 1 as vol goes TP_VOL_LOW -> TP_VOL_HIGH, and the
        # multiplier should go SMART_EXIT_VOL_PATIENCE_MULT -> 1.0 over that
        # same span, so interpolate on (1 - ratio).
        multiplier = 1.0 + (1.0 - ratio) * (SMART_EXIT_VOL_PATIENCE_MULT - 1.0)
        return SMART_EXIT_REVERSAL_THRESHOLD * multiplier

    def smart_exit_confirmed(self, side: str, dynamic_threshold: float) -> bool:
        """Confidence / persistence filter: true only if a MAJORITY of the
        last SMART_EXIT_CONFIRM_TICKS live predictions both exceed the
        dynamic threshold AND agree on direction against `side`. A single
        noisy tick - even a large one - can never satisfy this alone; it
        takes a sustained run of predictions to confirm a real reversal."""
        history = self._recent_predictions
        if len(history) < SMART_EXIT_CONFIRM_TICKS:
            return False  # not enough recent ticks yet to have any confidence at all

        if side == "LONG":
            agreeing = sum(1 for p in history if p <= -dynamic_threshold)
        else:
            agreeing = sum(1 for p in history if p >= dynamic_threshold)

        return (agreeing / len(history)) >= SMART_EXIT_CONFIRM_RATIO

    def _recent_tick_volatility(self) -> float:
        """Std-dev of tick-to-tick returns over the recent price_history window.
        Kept as its own small method (separate from get_features) so the
        entry-signal feature vector is never touched by TP logic - this only
        feeds the dynamic take-profit calculation below."""
        hist = self.price_history
        if len(hist) < 3:
            return 0.0
        arr = np.asarray(hist, dtype=float)
        rets = np.diff(arr) / np.where(arr[:-1] == 0, 1.0, arr[:-1])
        vol = float(np.std(rets))
        return vol if np.isfinite(vol) else 0.0

    def get_dynamic_take_profit_pct(self) -> float:
        """Volatility-based dynamic TP.

        - Quiet/sideways market (recent tick-return std <= TP_VOL_LOW):
          TP stays at the original static TAKE_PROFIT_PCT.
        - High volatility (std >= TP_VOL_HIGH): TP expands up to the
          TAKE_PROFIT_MAX_PCT ceiling, letting winners run further instead of
          getting clipped early during a strong move.
        - In between: linear interpolation, so TP widens smoothly rather than
          jumping between two fixed values.

        Always returns a plain float >= TAKE_PROFIT_PCT and <=
        TAKE_PROFIT_MAX_PCT - never None/bool, so callers can compare it
        directly against pct_move with no extra type checks.
        """
        if not DYNAMIC_TP_ENABLED:
            return TAKE_PROFIT_PCT

        vol = self._recent_tick_volatility()

        if vol <= TP_VOL_LOW:
            return TAKE_PROFIT_PCT
        if vol >= TP_VOL_HIGH:
            return TAKE_PROFIT_MAX_PCT

        vol_range = TP_VOL_HIGH - TP_VOL_LOW
        ratio = (vol - TP_VOL_LOW) / vol_range if vol_range > 0 else 0.0
        dynamic_pct = TAKE_PROFIT_PCT + ratio * (TAKE_PROFIT_MAX_PCT - TAKE_PROFIT_PCT)
        return dynamic_pct

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
                self._brain_dirty = True
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
        self.position.pending_order_ts = time.time()
        self.last_trade_action_ts = time.time()

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

    async def on_price_tick(self) -> None:
        features = self.get_features()
        self._learn_from_tick(features)

        # Refresh last_prediction on every tick (not just while FLAT via
        # generate_entry_signal) so Smart Exit sees a live reversal signal
        # while a trade is OPEN, not a stale prediction from before entry.
        if self.brain.is_ready():
            pred = self.brain.predict(features)
            self._recent_predictions.append(pred)

        if self.position.status == "FLAT":
            if time.time() - self.last_trade_action_ts < TRADE_COOLDOWN_SEC:
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

        # Hard stop is a safety exit - it always fires immediately,
        # regardless of cooldowns, hold-time, or profit thresholds.
        if pct_move <= -HARD_STOP_PCT:
            await self.close_position(
                f"hard stop: {pct_move*100:.2f}% adverse move on average entry", emergency=True
            )
            return

        # Over-trading guardrail: take-profit and smart-exit are
        # DISCRETIONARY closes, so both are held back until the position
        # has been open at least MIN_HOLD_SEC_BEFORE_EXIT. This is what
        # stops a noisy tick from flipping the position open->closed within
        # a couple of seconds. Hard stop above, and the max-DCA-exhausted
        # emergency exit below, are safety exits and deliberately bypass
        # this - a reversal that's actually blowing through the stop must
        # never be delayed by a cooldown meant to curb over-trading.
        held_long_enough = (time.time() - self.position.opened_at) >= MIN_HOLD_SEC_BEFORE_EXIT

        dynamic_tp_pct = self.get_dynamic_take_profit_pct()
        if pct_move >= dynamic_tp_pct and held_long_enough:
            # Fee-aware profit threshold: only take profit if the estimated
            # NET pnl (after the round-trip taker fee) clears a minimum -
            # otherwise a "profitable" 0.3% move that fees eat entirely was
            # exactly what was causing losses on rapid churn.
            net_pnl = self.estimate_net_pnl_usdt(price)
            if net_pnl >= MIN_NET_PROFIT_USDT:
                await self.close_position(
                    f"take-profit: {pct_move*100:.2f}% favorable move "
                    f"(dynamic TP={dynamic_tp_pct*100:.3f}%, base={TAKE_PROFIT_PCT*100:.2f}%, "
                    f"est. net pnl=${net_pnl:+.4f} after fees)"
                )
                return
            # Move clears the raw TP% but not the fee-aware floor yet - hold
            # and let it run rather than closing into a fee-losing "profit".

        # --- Smart Exit / Damage Control ------------------------------------
        # If the brain's live prediction has flipped hard against the
        # position's own side before HARD_STOP_PCT is ever reached, get out
        # now at breakeven/minimal loss rather than riding it down to the
        # hard stop (or, in a fast-moving 40x market, toward liquidation).
        # Still gated by held_long_enough - a reversal prediction seconds
        # after entry is far more likely noise than a real regime change.
        #
        # Two extra filters on top of that, both aimed at the same problem
        # (firing on ordinary noise instead of a real reversal):
        #   - dynamic_threshold widens in quiet markets (more patient) and
        #     relaxes to the baseline in volatile ones (as-is sensitivity).
        #   - smart_exit_confirmed() requires a MAJORITY of the last several
        #     predictions to agree, not just the latest single tick.
        last_pred = self.brain.last_prediction
        if SMART_EXIT_ENABLED and held_long_enough and self.brain.is_ready() and last_pred is not None:
            dynamic_threshold = self.get_dynamic_smart_exit_threshold()
            reversal_against_position = (
                (self.position.side == "LONG" and last_pred <= -dynamic_threshold)
                or (self.position.side == "SHORT" and last_pred >= dynamic_threshold)
            )
            if (
                reversal_against_position
                and pct_move > -SMART_EXIT_MAX_LOSS_PCT
                and self.smart_exit_confirmed(self.position.side, dynamic_threshold)
            ):
                await self.close_position(
                    f"SMART EXIT: brain confirms reversal against {self.position.side} over "
                    f"the last {SMART_EXIT_CONFIRM_TICKS} ticks (last_pred={last_pred:+.5f}, "
                    f"dynamic_threshold={dynamic_threshold:+.5f}) at {pct_move*100:.2f}% - "
                    f"exiting at breakeven/minimal loss instead of risking further adverse move"
                )
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
        else:
            self.position.opened_at = time.time()  # first fill only - marks the start of the hold-time clock
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

        self.recent_trade_outcomes.append(1.0 if total_rp >= 0 else 0.0)

        if self._entry_feature_snapshot is not None:
            invested_notional = sum(p * q for p, q in self.position.entries) or None
            if invested_notional:
                realized_trade_return = total_rp / invested_notional
                self.brain.learn(self._entry_feature_snapshot, realized_trade_return)
                self._brain_dirty = True
                print(color(
                    f"{now_str()} [brain] reinforced entry decision with realized "
                    f"trade outcome (label={realized_trade_return:+.5f}, "
                    f"brain_updates={self.brain.update_count})", MAGENTA,
                ))
            self._entry_feature_snapshot = None

        self.position = PositionState(last_close_time=time.time())

        # Auto-Persistence: push the updated brain after every trade. Fired
        # as a background task so a slow/failed GitHub call never delays the
        # next tick's trading decisions; persist_brain() itself catches and
        # logs every failure mode internally.
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
        #
        # BUT: a market order can fill in milliseconds while Binance's own
        # positionRisk snapshot can lag that fill by a beat. Without this
        # grace window, a poll landing in that gap would see "exchange
        # still flat" for an order that is, in fact, already filling, force
        # local state back to FLAT, and let on_price_tick() immediately
        # open a SECOND entry on top of the first the very next tick - a
        # direct cause of rapid-fire duplicate trades. So a freshly-placed
        # pending order/close gets SYNC_PENDING_GRACE_SEC to actually show
        # up before we trust "flat" enough to reset.
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
                            manager.best_bid_qty = float(data.get("B", 0) or 0)
                            manager.best_ask_qty = float(data.get("A", 0) or 0)
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
    websocket reconnect event.

    Liquidation-price note: `liquidationPrice` is Binance's own computed
    field from /fapi/v2/positionRisk - this bot never calculates it itself.
    On Cross margin, a small position sitting on a much larger wallet
    balance (very common on testnet, which grants large fake balances) can
    legitimately produce a liquidation price that is mathematically valid
    but absurdly far from the mark price (e.g. millions of dollars away on
    BTCUSDT). Rather than re-deriving Binance's tiered maintenance-margin
    formula ourselves - a near-guaranteed source of new bugs - every value
    is sanity-checked against mark price before being trusted, displayed,
    or acted on. An implausible reading is now treated as "unavailable"
    (None) instead of being surfaced as if it were real."""
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

                    # Genuine early-warning safety exit - but ONLY on a
                    # value that passed the plausibility check above, so a
                    # bogus multi-million reading can never trigger this.
                    side = "LONG" if float(row["positionAmt"]) > 0 else "SHORT"
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
                            emergency=True,
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
    """Auto-Persistence on a fixed cadence, independent of trade activity -
    the per-trade push in _on_close_filled covers the "after each trade"
    requirement, this covers "at reasonable intervals" for a quiet market
    where the brain is still learning from ticks but no trade has closed
    in a while."""
    while True:
        await asyncio.sleep(interval_sec)
        if manager._brain_dirty:
            await manager.persist_brain(reason="periodic interval")


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
            f"brain=[{brain_state}, last_pred={last_pred}]  "
            f"github_sync=[{'on' if manager.github_sync.enabled else 'off'}, last_push={sync_state}]",
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
        # rather than discovering a too-small step mid-trade.
        for step in range(MAX_DCA_STEPS + 1):
            margin = INITIAL_ENTRY_USDT if step == 0 else INITIAL_ENTRY_USDT * (DCA_MULTIPLIER ** step)
            step_notional = margin * LEVERAGE
            ok = step_notional >= filters.min_notional
            label = "INITIAL" if step == 0 else f"DCA #{step}"
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
        manager.current_price = (float(book["bidPrice"]) + float(book["askPrice"])) / 2
        manager.best_bid_qty = float(book.get("bidQty", 0) or 0)
        manager.best_ask_qty = float(book.get("askQty", 0) or 0)
        print(color(f"[setup] current price: {manager.current_price:.2f}", GRAY))

        # Persistent Adaptive Learning: local brain.pkl -> GitHub -> fresh model.
        await manager.load_or_init_brain()

        await initialize_sync(client, manager, context="startup")

        await asyncio.gather(
            market_data_consumer(manager),
            userdata_consumer(client, manager),
            listen_key_keepalive(client),
            balance_refresher(client, manager),
            position_risk_poller(client, manager),
            status_loop(manager),
            brain_sync_loop(manager),
        )
    finally:
        if manager is not None:
            try:
                await manager.persist_brain(reason="shutdown")
            except Exception as e:  # noqa: BLE001 - shutdown persistence is best-effort only
                print(color(f"[brain] final persist on shutdown failed: {e}", YELLOW))
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
