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
PERSISTENCE below - but candle/feature buffers are not). As of the 2026-08
instant warm-up fix that no longer costs an hour of downtime: startup issues
ONE REST call to GET /fapi/v1/klines and seeds the candle buffer with the
last KLINE_WARMUP_LIMIT closed 1m candles, so ATR/RSI/EMA/regime are valid
within seconds of boot; the live websocket stream then continues the same
series in real time (see warm_up_candles_from_klines in trading.py).

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
import random
import sys
import time
import traceback
import urllib.parse
from collections import deque
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Deque, Dict, List, Optional, Tuple

import aiohttp
import numpy as np
import websockets
from sklearn.linear_model import SGDRegressor, SGDClassifier
from websockets.exceptions import ConnectionClosed

# ============================================================================
# CONFIG (moved to config.py - imported here unchanged)
# ============================================================================

from config import (
    FEATURE_RECORDER_ENABLED,
    FEATURE_RECORDER_INTERVAL_SEC,
    FEATURE_RECORDER_SHARD_SEC,
    FEATURE_LOG_RETENTION_ENABLED,
    FEATURE_LOG_RETAIN_LOCAL_HOURS,
    FEATURE_LOG_MAX_LOCAL_MB,
    BREAKOUT_ENGINE_ENABLED,
    BREAKOUT_TIMEFRAME,
    BREAKOUT_CHANNEL,
    BREAKOUT_STOP_ATR,
    BREAKOUT_TP_ATR,
    BREAKOUT_TRAIL_ATR,
    BREAKOUT_TRAIL_START_ATR,
    BREAKOUT_RISK_PCT,
    FEATURE_LOG_PATH,
    feature_recorder_horizons,
    env_parse_warnings,
    SYMBOL,
    # 2026-08-20 multi-coin watchlist
    ACTIVE_SYMBOLS,
    MAX_ACTIVE_TRADES,
    # 2026-08-21 notional-relative risk scaling
    ENTRY_NOTIONAL_USDT,
    notional_scaling_report,
    USE_TESTNET,
    DRY_RUN,
    DRY_FILL_ENABLED,
    DRY_FILL_SLIPPAGE_BPS,
    DRY_FILL_TAKER_FEE_PCT,
    I_UNDERSTAND_THIS_IS_REAL_MONEY,
    LIVE_TRADING_CONFIRMATION,
    API_KEY,
    API_SECRET,
    LEVERAGE,
    MAX_ALLOWED_LEVERAGE,
    MARGIN_TYPE,
    INITIAL_ENTRY_USDT,
    DCA_MULTIPLIER,
    MAX_DCA_STEPS,
    DCA_TRIGGER_PCT,
    TAKE_PROFIT_PCT,
    HARD_STOP_PCT,
    DYNAMIC_TP_ENABLED,
    TAKE_PROFIT_MAX_PCT,
    TP_VOL_LOW,
    TP_VOL_HIGH,
    SIGNAL_LOOKBACK_TICKS,
    SIGNAL_DEADBAND_PCT,
    TRADE_COOLDOWN_SEC,
    MIN_HOLD_SEC_BEFORE_EXIT,
    MAX_DAILY_LOSS_USDT,
    DAILY_PROFIT_TARGET_USDT,
    TAKER_FEE_RATE,
    MIN_NET_PROFIT_USDT,
    LIQUIDATION_SANITY_MIN_RATIO,
    LIQUIDATION_SANITY_MAX_RATIO,
    LIQUIDATION_WARNING_BUFFER_PCT,
    SYNC_PENDING_GRACE_SEC,
    CANDLE_INTERVAL_SEC,
    CANDLE_HISTORY,
    KLINE_WARMUP_ENABLED,
    KLINE_WARMUP_LIMIT,
    KLINE_WARMUP_INTERVAL,
    ATR_PERIOD,
    EMA_FAST,
    EMA_MED,
    EMA_SLOW,
    ROLLING_RETURN_WINDOWS,
    REGIME_ATR_HIGH_MULT,
    REGIME_ATR_LOW_MULT,
    REGIME_TREND_SLOPE_STRONG,
    REGIME_TREND_SLOPE_WEAK,
    REGIME_LOOKBACK_CANDLES,
    N_FEATURES_V2,
    BRAIN2_WARMUP_UPDATES,
    LABEL_HORIZON_TICKS,
    FEATURE_SHORT_LOOKBACK,
    RECENT_TRADE_WINDOW,
    TP_HIT_LOOKAHEAD_CANDLES,
    ENTRY_SCORE_THRESHOLD,
    ENTRY_WEIGHTS,
    SMART_EXIT_ENABLED,
    SMART_EXIT_MAX_LOSS_PCT,
    SMART_EXIT_CONFIRM_TICKS,
    SMART_EXIT_MIN_AGREE,
    SMART_EXIT_CONFIDENCE_DROP,
    SMART_EXIT_ATR_MOVE_MULT,
    DCA_ATR_MULTIPLIER,
    DCA_MIN_DISTANCE_PCT,
    DCA_MAX_DISTANCE_PCT,
    SIZE_MIN_MULT,
    SIZE_MAX_MULT,
    PARTIAL_TP_ENABLED,
    PARTIAL_TP_FRACTION,
    PARTIAL_TP_TRIGGER_RATIO,
    BREAKEVEN_AFTER_PARTIAL,
    TRAILING_STOP_ENABLED,
    TRAILING_STOP_ATR_MULT,
    TRADE_LOG_JSON_PATH,
    TRADE_LOG_CSV_PATH,
    STATS_JSON_PATH,
    STATS_CSV_PATH,
    STATS_EXPORT_INTERVAL_SEC,
    FUNDING_OI_POLL_SEC,
    BRAIN_LOCAL_PATH,
    GITHUB_TOKEN,
    GITHUB_REPO,
    GITHUB_BRAIN_PATH,
    GITHUB_BRANCH,
    DCA_STATE_PATH,
    GITHUB_DCA_STATE_PATH,
    BRAIN_AUTO_PUSH_INTERVAL_SEC,
    LISTEN_KEY_KEEPALIVE_SEC,
    BALANCE_REFRESH_SEC,
    BALANCE_WS_FRESH_SEC,
    POSITION_RISK_POLL_SEC,
    POSITION_RISK_POLL_IDLE_SEC,
    REST_POLL_JITTER_PCT,
    jittered_interval,
    MAX_BACKOFF_SEC,
    IDLE_DATA_TIMEOUT_SEC,
    USER_WS_IDLE_FALLBACK_SEC,
    STARTUP_RETRY_ATTEMPTS,
    STARTUP_RETRY_BASE_DELAY_SEC,
    SUPERVISOR_RESTART_DELAY_SEC,
    REST_BASE,
    WS_MARKET_BASE,
    WS_USERDATA_BASE,
)


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


# round_step / clamp / safe_div / ema_series moved to indicators.py - imported below.
from indicators import round_step, clamp, safe_div, ema_series


async def retry_with_backoff(coro_fn, *args, attempts: int = STARTUP_RETRY_ATTEMPTS,
                              base_delay: float = STARTUP_RETRY_BASE_DELAY_SEC, label: str = "operation"):
    """Retries a one-shot async setup call with exponential backoff. Used for
    the REST calls that run ONCE before the self-reconnecting websocket loops
    take over - those calls have no other retry path of their own, so a single
    transient network blip during container startup would otherwise kill the
    whole process before it ever gets going.

    2026-08 HTTP 418/429 cooldown-survival fix (this function only - the
    exponential-backoff retry behavior for every OTHER kind of failure,
    including the final SystemExit after `attempts` genuine failures, is
    completely unchanged): an HTTP 418 (IP ban) or 429 (rate limit) is not
    a normal transient failure - retrying it on the usual exponential
    schedule only digs the ban deeper, and a long ban (Binance 418 bans
    escalate well past the handful of minutes this retry loop's own
    backoff would cover) previously exhausted all `attempts` and raised
    SystemExit, which the outer run_forever() supervisor would then
    restart from scratch - losing the in-memory cooldown and immediately
    contacting Binance again on the new process, worsening the ban. Now:
    a 418/429 (detected via BinanceApiError.status, the same exception
    type/attribute every existing caller already uses) makes this loop
    wait out the SAME shared RestClient cooldown _arm_cooldown() already
    armed (see exchange.py) - however long that takes - and then retries
    WITHOUT consuming one of the `attempts` budget, so the process stays
    alive and simply waits instead of restart-looping."""
    last_exc = None
    attempt = 1
    while attempt <= attempts:
        try:
            return await coro_fn(*args)
        except BinanceApiError as e:
            if e.status == 451:
                raise SystemExit(
                    f"[startup] {label}: Binance denied access from this host (HTTP 451). "
                    "Stopping; verify service eligibility with Binance before trying again."
                ) from e
            if e.status in (418, 429):
                client = getattr(coro_fn, "__self__", None)
                if not isinstance(client, RestClient):
                    client = next((a for a in args if isinstance(a, RestClient)), None)
                if client is not None:
                    remaining = client.cooldown_remaining()
                    if remaining > 0:
                        expiry_utc = client.cooldown_expiry_utc_str()
                        print(color(
                            f"[startup] {label} hit HTTP {e.status} (rate limit/IP ban) - waiting for "
                            f"the shared cooldown to clear ({expiry_utc}, {remaining:.0f}s) before "
                            f"retrying. This wait does NOT count against the {attempts}-attempt budget "
                            f"and will not exit the process.", RED,
                        ))
                        await asyncio.sleep(remaining + random.uniform(0.1, 3.0))
                        print(color(f"[startup] {label} resuming after cooldown.", GREEN))
                        continue  # retry now, same `attempt` count - not consumed
            last_exc = e
            delay = base_delay * (2 ** (attempt - 1))
            print(color(
                f"[startup] {label} failed (attempt {attempt}/{attempts}): {e}. "
                f"Retrying in {delay:.1f}s ...", YELLOW
            ))
            if attempt < attempts:
                await asyncio.sleep(delay)
            attempt += 1
        except Exception as e:  # noqa: BLE001 - deliberately broad, this is a retry wrapper
            last_exc = e
            delay = base_delay * (2 ** (attempt - 1))
            print(color(
                f"[startup] {label} failed (attempt {attempt}/{attempts}): {e}. "
                f"Retrying in {delay:.1f}s ...", YELLOW
            ))
            if attempt < attempts:
                await asyncio.sleep(delay)
            attempt += 1
    raise SystemExit(f"[startup] {label} failed after {attempts} attempts: {last_exc}")


# ============================================================================
# SAFETY GATE CHECKS
# ============================================================================


def enforce_safety_gates() -> None:
    if not DRY_RUN and (not API_KEY or not API_SECRET):
        raise SystemExit(
            "Missing BINANCE_API_KEY / BINANCE_API_SECRET environment variables. "
            "Set them in Railway's Variables tab (never hardcode them in this "
            "file, especially once it's pushed to GitHub). Generate TESTNET "
            "keys at https://testnet.binancefuture.com/ if you don't have them."
        )

    if not DRY_RUN and not USE_TESTNET and not I_UNDERSTAND_THIS_IS_REAL_MONEY:
        raise SystemExit(
            "REFUSING TO START: USE_TESTNET=false (mainnet) but "
            "I_UNDERSTAND_THIS_IS_REAL_MONEY is not set to 'yes'. "
            "This is a deliberate safety gate."
        )

    # 2026-08 Live Trading Safety Guard: a second, independent gate -
    # both this AND I_UNDERSTAND_THIS_IS_REAL_MONEY above must be set
    # before mainnet trading is allowed to start. Deliberately checked
    # separately (not merged into the check above) so it prints its own
    # explicit message and can be reasoned about/audited independently.
    if not DRY_RUN and not USE_TESTNET and not LIVE_TRADING_CONFIRMATION:
        print(color(
            "[LIVE SAFETY]\n"
            "Mainnet trading blocked.\n"
            "Set LIVE_TRADING_CONFIRMATION=true to enable live orders.",
            RED,
        ))
        raise SystemExit(
            "REFUSING TO START: USE_TESTNET=false (mainnet) but "
            "LIVE_TRADING_CONFIRMATION is not set to 'true'. "
            "This is a deliberate safety gate, separate from "
            "I_UNDERSTAND_THIS_IS_REAL_MONEY, to prevent accidental "
            "real-money execution."
        )

    global LEVERAGE
    if LEVERAGE > MAX_ALLOWED_LEVERAGE:
        print(color(
            f"[safety] Requested LEVERAGE={LEVERAGE} exceeds MAX_ALLOWED_LEVERAGE="
            f"{MAX_ALLOWED_LEVERAGE}. Clamping to {MAX_ALLOWED_LEVERAGE}.", YELLOW
        ))
        LEVERAGE = MAX_ALLOWED_LEVERAGE

    if not 0 <= MAX_DCA_STEPS <= 5:
        raise SystemExit("MAX_DCA_STEPS must be between 0 (disabled) and 5.")


# ============================================================================
# REST CLIENT (signed requests, HMAC-SHA256) - moved to exchange.py, imported
# below. SYMBOL FILTERS moved with it (fetch_symbol_filters calls
# client.get_exchange_info(), so it travels with the REST client).
# ============================================================================

from exchange import (BinanceApiError, RestClient, SymbolFilters,
                      SymbolNotListed, fetch_symbol_filters)


# ============================================================================
# TRADING EXECUTION - moved to trading.py, imported below. Candle,
# CandleAggregator, RegimeReading, MarketRegimeEngine, FeatureBuilderV2,
# RiskEngine, ConfidenceReading, ConfidenceEngine, EntryDecision,
# EntryEngineV2, RewardCalculator, TradeLogger, PerformanceStats,
# PositionState, MartingaleManager, and initialize_sync all moved together -
# see trading.py's module docstring for why they couldn't be split apart.
# ============================================================================

from trading import (
    # 2026-08-20 multi-coin
    PortfolioCoordinator,
    resolve_symbol_paths,
    Candle,
    CandleAggregator,
    RegimeReading,
    MarketRegimeEngine,
    FeatureBuilderV2,
    RiskEngine,
    ConfidenceReading,
    ConfidenceEngine,
    EntryDecision,
    EntryEngineV2,
    RewardCalculator,
    TradeLogger,
    PerformanceStats,
    PositionState,
    MartingaleManager,
    sanitize_recovered_dca_step,
    initialize_sync,
    warm_up_candles_from_klines,
    reconcile_protective_stop_on_startup,
    BrainV2,
)




# ============================================================================
# MARKET DATA WEBSOCKET / USER DATA WEBSOCKET - moved to websocket.py,
# imported below. initialize_sync is injected onto the websocket module so
# userdata_consumer's reconnect-time call to it keeps working unchanged -
# see websocket.py's module docstring for why.
# ============================================================================

from websocket import market_data_consumer, userdata_consumer, listen_key_keepalive
import websocket as _websocket_module
_websocket_module.initialize_sync = initialize_sync
from paper_summary import print_paper_summary


# ============================================================================
# PERSISTENT DCA STATE RECOVERY
# ============================================================================
# Startup-only helper: restores a persisted DCA/position state snapshot
# (local disk first, then GitHub via the SAME shared github_sync client
# already used for brain.pkl / the CSV logs / the trade-sync cursor - see
# DCA_STATE_PATH / GITHUB_DCA_STATE_PATH in config.py) so an in-progress
# DCA position isn't silently forgotten across an ephemeral restart before
# initialize_sync() ever runs its own exchange-vs-local reconciliation.
#
# This does NOT change any trading/DCA/entry/exit logic - it only rebuilds
# manager.position from whatever was last persisted (if anything), the same
# way manager.position already starts as a fresh PositionState() by
# default. initialize_sync() (unchanged, in trading.py) still runs
# immediately afterward and is still the sole authority that reconciles
# this rebuilt local state against what Binance actually reports.
# Fails soft everywhere - any missing/corrupt/incompatible snapshot simply
# leaves manager.position as its current (flat) default.


async def load_dca_state(manager: MartingaleManager) -> None:
    if DRY_RUN:
        return  # nothing real to recover DRY_RUN state against

    data: Optional[dict] = None
    try:
        # 2026-08-20 multi-coin: read THIS manager's own snapshot, not the
        # primary symbol's. With four managers the module-level constant
        # would have restored the primary's DCA state onto all four.
        if os.path.exists(manager.paths["dca_state"]):
            with open(manager.paths["dca_state"], "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:  # noqa: BLE001 - corrupt/missing local file must not block startup
        print(color(f"[dca-state] could not read local {manager.paths['dca_state']}: {e}", YELLOW))
        data = None

    if data is None:
        try:
            raw = await manager.github_sync.download(path=manager.paths["github_dca_state"])
            if raw:
                data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - restore must never block startup
            print(color(f"[dca-state] could not check GitHub for DCA state: {e}", YELLOW))
            data = None

    if not data:
        print(color(
            "[dca-state] no local or remote DCA state snapshot found - starting flat.", GRAY
        ))
        return

    try:
        valid_fields = {f.name for f in fields(PositionState)}
        # entry_features is an Optional[np.ndarray] on PositionState - not
        # something this JSON-based snapshot format carries; leave it at
        # its dataclass default (None) rather than guessing a reconstruction.
        #
        # 2026-08 snapshot key-name fix (this remap only - every other
        # field in the snapshot already matches its PositionState name
        # exactly and is untouched): _dca_state_snapshot() (trading.py)
        # writes "qty" for PositionState.total_qty and "dca_history" for
        # PositionState.entries. Neither name matches a PositionState
        # field, so the old `k in valid_fields` filter below silently
        # dropped both - every restored OPEN snapshot got total_qty=0.0
        # and entries=[] (the PositionState() defaults) while side/
        # avg_entry_price/dca_step/opened_at all restored correctly,
        # producing an OPEN position with real economics but zero
        # quantity (see the Live incident this fix addresses: qty=0.000000
        # net=+0.0000, Max Hold read that as "no meaningful loss"). Remap
        # explicitly before filtering, and derive original_qty from the
        # same qty value when the snapshot doesn't carry a separate
        # original_qty (older/current snapshot format never has - only a
        # possible FUTURE snapshot format might, so an existing
        # original_qty key is still honored if present, for forward
        # compatibility).
        remapped = dict(data)
        if "qty" in remapped:
            remapped.setdefault("total_qty", remapped["qty"])
            remapped.setdefault("original_qty", remapped["qty"])
        if "dca_history" in remapped:
            remapped.setdefault("entries", remapped["dca_history"])

        kwargs = {k: v for k, v in remapped.items() if k in valid_fields and k != "entry_features"}
        if isinstance(kwargs.get("entries"), list):
            kwargs["entries"] = [tuple(e) for e in kwargs["entries"]]

        candidate = PositionState(**kwargs)

        candidate.dca_step, recovered_step_safety_reason = sanitize_recovered_dca_step(
            candidate.dca_step
        )
        if recovered_step_safety_reason is not None:
            candidate.dca_blocked = True
            candidate.dca_block_reason = recovered_step_safety_reason

        if candidate.status == "FLAT" and (
            candidate.side is not None
            or candidate.total_qty != 0
            or candidate.avg_entry_price not in (None, 0)
            or candidate.dca_step != 0
            or candidate.entries
        ):
            print(color(
                "[dca-state] REJECTED snapshot claiming FLAT with non-flat economics - "
                "starting from a clean FLAT state.", RED,
            ))
            return

        # 2026-08 invalid-OPEN-snapshot validation (this block only): an
        # OPEN/DCA_PENDING/CLOSING snapshot is only trusted if its core
        # economics are actually self-consistent - valid side, positive
        # average entry, positive quantity, and (when entries/dca_history
        # was present in the raw snapshot at all) a non-empty, qty-
        # consistent fill history. A snapshot that fails this check is
        # exactly the shape of the pre-fix qty=0 bug (or any other
        # corruption) reappearing under a different cause - falling back
        # to a fresh FLAT PositionState() is always safe (initialize_sync()
        # runs immediately after this and is the sole authority that can
        # rebuild a genuinely-open position from the exchange itself),
        # whereas trusting a broken OPEN snapshot is not.
        if candidate.status in ("OPEN", "DCA_PENDING", "CLOSING"):
            entries_sum = sum(qty for _, qty in candidate.entries) if candidate.entries else 0.0
            qty_tolerance = max(manager.filters.step_size, 1e-9) * 2
            entries_consistent = (
                not candidate.entries
                or (
                    all(float(price) > 0 and float(qty) > 0 for price, qty in candidate.entries)
                    and abs(entries_sum - candidate.total_qty) <= qty_tolerance
                )
            )
            valid_open = (
                candidate.side in ("LONG", "SHORT")
                and candidate.avg_entry_price is not None
                and candidate.avg_entry_price > 0
                and candidate.total_qty > 0
                and entries_consistent
            )
            if not valid_open:
                print(color(
                    f"[dca-state] REJECTED snapshot claiming status={candidate.status} with invalid "
                    f"economics (side={candidate.side}, avg_entry={candidate.avg_entry_price}, "
                    f"total_qty={candidate.total_qty}, entries={len(candidate.entries)}) - "
                    f"starting flat instead of restoring an unmanageable position. "
                    f"initialize_sync() will rebuild from the exchange if a position genuinely exists.",
                    RED,
                ))
                return

        manager.position = candidate
        print(color(
            f"[dca-state] restored DCA state snapshot (status={manager.position.status}, "
            f"side={manager.position.side}, dca_step={manager.position.dca_step}, "
            f"total_qty={manager.position.total_qty}).", MAGENTA,
        ))
    except Exception as e:  # noqa: BLE001 - corrupted/incompatible snapshot must not crash startup
        print(color(f"[dca-state] failed to apply DCA state snapshot ({e}), starting flat.", YELLOW))


def _exc_text(e: BaseException) -> str:
    """2026-08-19 F3. Human-readable text for an exception that is NEVER empty.

    asyncio.TimeoutError, aiohttp.ClientError and several aiohttp subclasses
    carry NO message - str(e) is "". Logging a bare {e} therefore produced
    lines like:

        [risk] position risk poll failed:
        [balance] refresh failed:

    which is what the 2026-08-19 21:06 logs showed. Those blanks were REST
    timeouts, and they mattered: the timing-out positionRisk poll is what left
    initialize_sync with a stale "position still open" read, which resurrected
    an already-closed position and produced the duplicate trade record. The
    failure was fully diagnosable the whole time - it just had no text.

    Always prefixes the exception class, so an empty message still identifies
    what went wrong."""
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


async def balance_refresher(client: RestClient, manager: MartingaleManager) -> None:
    while True:
        # 2026-08 HTTP 418/429 cooldown-survival fix (this check only -
        # the actual balance refresh below is unchanged): skip this
        # iteration's REST call entirely and silently while the shared
        # cooldown is active, instead of calling get_balance() (which
        # would just raise locally anyway - see RestClient._request())
        # and logging an error every BALANCE_REFRESH_SEC. wait_out_cooldown_silently()
        # sleeps until the cooldown clears (plus jitter, to avoid every
        # poller resuming on the same tick) and logs the one-time resume
        # line itself - nothing else needs to log here.
        if client.is_cooldown_active():
            await client.wait_out_cooldown_silently()
            continue
        # 2026-08 HTTP 429 REST rate-limit fix - websocket-first state
        # tracking. Binance pushes the USDT wallet balance on every
        # ACCOUNT_UPDATE (see MartingaleManager.on_account_update), so while
        # that stream copy is fresher than BALANCE_WS_FRESH_SEC there is
        # nothing a GET /fapi/v2/balance could add - skip the request
        # entirely rather than spend rate limit re-learning what the socket
        # already told us. getattr-guarded so a manager without the field
        # (older stubs/tests) simply keeps polling exactly as before.
        last_ws_ts = getattr(manager, "last_account_update_ts", 0.0) or 0.0
        if BALANCE_WS_FRESH_SEC > 0 and (time.time() - last_ws_ts) < BALANCE_WS_FRESH_SEC:
            await asyncio.sleep(jittered_interval(BALANCE_REFRESH_SEC))
            continue
        try:
            balances = await client.get_balance()
            usdt = next((b for b in balances if b["asset"] == "USDT"), None)
            if usdt:
                real_balance = float(usdt["availableBalance"])
                manager.available_balance = min(real_balance, 50.0)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[balance] refresh failed: {_exc_text(e)}", RED))
        await asyncio.sleep(jittered_interval(BALANCE_REFRESH_SEC))


async def funding_oi_poller(client: RestClient, manager: MartingaleManager) -> None:
    """Best-effort funding rate + open interest refresh. Both are optional
    feature inputs - any failure just leaves the last known value (or None)
    in place and never interrupts trading."""
    while True:
        # 2026-08 HTTP 418/429 cooldown-survival fix - same pattern as
        # balance_refresher() above.
        if client.is_cooldown_active():
            await client.wait_out_cooldown_silently()
            continue
        try:
            premium = await client.get_premium_index(manager.symbol)
            manager.funding_rate = float(premium.get("lastFundingRate", 0) or 0)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[funding:{manager.symbol}] premiumIndex poll failed (continuing without it): {_exc_text(e)}", YELLOW))
        try:
            oi = await client.get_open_interest(manager.symbol)
            manager.open_interest = float(oi.get("openInterest", 0) or 0)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(f"[funding:{manager.symbol}] openInterest poll failed (continuing without it): {_exc_text(e)}", YELLOW))
        # 2026-08 HTTP 429 fix: jittered so this poller never lines up with
        # the balance/positionRisk pollers on the same wall-clock tick.
        await asyncio.sleep(jittered_interval(FUNDING_OI_POLL_SEC))


def _position_risk_interval(manager: MartingaleManager) -> float:
    """2026-08 HTTP 429 REST rate-limit fix. Chooses this cycle's positionRisk
    poll interval from the rate-limit-safe 15-30s band:

      - POSITION_RISK_POLL_SEC (active, default 20s) whenever something is
        actually at risk - a live position, or anything mid-flight
        (ENTERING / DCA_PENDING / CLOSING / a pending order) where the
        liquidation price and the exchange-vs-local resync genuinely matter.
      - POSITION_RISK_POLL_IDLE_SEC (idle, default 30s) while flat, where the
        poll is only a safety net behind the user-data stream.

    The user-data stream's own ACCOUNT_UPDATE view (see
    MartingaleManager.has_ws_position_hint) is consulted FIRST and can only
    ever escalate to the active cadence, never suppress it: if the websocket
    says a position exists we poll actively even when local state still
    believes it is flat. A stale/absent hint falls through to local state, so
    this can never poll LESS often than the old behavior would have when
    something is open.
    """
    ws_hint = None
    hint_fn = getattr(manager, "has_ws_position_hint", None)
    if callable(hint_fn):
        ws_hint = hint_fn(POSITION_RISK_POLL_IDLE_SEC * 3)
    if ws_hint:
        return POSITION_RISK_POLL_SEC

    position = getattr(manager, "position", None)
    status = getattr(position, "status", "FLAT") if position is not None else "FLAT"
    busy = (
        status != "FLAT"
        or (position is not None and getattr(position, "total_qty", 0.0))
        or (position is not None and getattr(position, "pending_order_id", None) is not None)
        or getattr(manager, "liquidation_price", None) is not None
    )
    return POSITION_RISK_POLL_SEC if busy else POSITION_RISK_POLL_IDLE_SEC


async def position_risk_poller(client: RestClient, manager: MartingaleManager) -> None:
    """Polls Binance's OWN authoritative liquidation price, sanity-checks it
    against mark price, and re-syncs local state on every cycle.

    2026-08 HTTP 429 REST rate-limit fix (cadence only - every risk check,
    sanity band, emergency-close condition and the initialize_sync() call
    below are byte-for-byte unchanged): this used to fire every 10s
    unconditionally, forever, open or flat. Binance answered with
    "-1003 Too many requests ... Please use the websocket for live updates to
    avoid polling the API", which then suppressed ALL REST traffic on the
    shared cooldown for ~30s at a time. The poll now runs on the adaptive
    15-30s cadence chosen by _position_risk_interval() above, with jitter, and
    the liquidation price stays the ONLY thing it is needed for - it is the
    single risk field the user-data websocket never sends."""
    while True:
        if DRY_RUN:
            await asyncio.sleep(jittered_interval(_position_risk_interval(manager)))
            continue
        # 2026-08 HTTP 418/429 cooldown-survival fix - same pattern as
        # balance_refresher() above. Deliberately does NOT call
        # initialize_sync() either while skipping - it would just hit the
        # same cooldown-blocked get_position_risk() path if rows were None,
        # and this poller always supplies its own freshly-fetched `rows`.
        if client.is_cooldown_active():
            await client.wait_out_cooldown_silently()
            continue
        try:
            rows = await client.get_position_risk(manager.symbol)
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
            print(color(f"[risk:{manager.symbol}] position risk poll failed: {_exc_text(e)}", RED))
        await asyncio.sleep(jittered_interval(_position_risk_interval(manager)))


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
            continue
        try:
            await manager.sync_performance_stats_to_github()
        except Exception as e:  # noqa: BLE001 - GitHub sync must never crash the trading loop
            print(color(f"[csv-sync] performance_stats.csv sync error: {e}", YELLOW))


async def feature_log_loop(manager: MartingaleManager, interval_sec: int = 120) -> None:
    """Flushes recorded rows to disk and uploads completed shards.

    Runs regardless of DRY_RUN - recording is the entire point when trading is
    disabled. Skips itself cheaply when the recorder is off.
    """
    if not manager.feature_recorder.enabled:
        return
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await manager.sync_feature_log_to_github()
        except Exception as e:  # noqa: BLE001 - never crash the trading loop
            print(color(f"[feature-log] loop error: {_exc_text(e)}", YELLOW))


async def feature_log_status_loop(manager: MartingaleManager, interval_sec: int = 600) -> None:
    """Periodic visibility on how fast the dataset is accumulating, so a
    silently-stalled recorder is obvious rather than discovered days later."""
    if not manager.feature_recorder.enabled:
        return
    while True:
        await asyncio.sleep(interval_sec)
        try:
            st = manager.feature_recorder.stats()
            print(color(
                f"{now_str()} [feature-log:{manager.symbol}] taken={st['taken']} "
                f"finalised={st['finalised']} written={st['written']} "
                f"pending={st['pending']} dropped={st['dropped']} "
                f"shard={st['shard']}", GRAY,
            ))
        except Exception as e:  # noqa: BLE001
            print(color(f"[feature-log] status error: {_exc_text(e)}", YELLOW))


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


async def setup_symbol(
    client: RestClient, symbol: str, portfolio: "PortfolioCoordinator"
) -> MartingaleManager:
    """2026-08-20 multi-coin: everything that used to happen inline in main()
    for the one SYMBOL, now parameterised so it can run once per watchlist
    entry. The body is the original startup sequence verbatim - same calls,
    same order, same comments - with SYMBOL replaced by the `symbol`
    argument and each log line prefixed with the symbol so four interleaved
    startups stay readable.

    Ordering is load-bearing and unchanged: brain -> CSV restore ->
    accounting rebuild -> cursor -> DCA snapshot -> initialize_sync ->
    protective-stop reconciliation. See the inline comments for why.
    """
    filters = await retry_with_backoff(
        fetch_symbol_filters, client, symbol, label="fetch_symbol_filters"
    )
    print(color(
        f"[setup] {symbol} filters: tick={filters.tick_size} step={filters.step_size} "
        f"minQty={filters.min_qty} minNotional={filters.min_notional}", GRAY
    ))

    # Cross 40x / DCA sizing sanity check: confirm every one of the 5
    # martingale steps clears the exchange's minimum notional up front.
    # Step 0 (the initial entry) is ALWAYS exactly INITIAL_ENTRY_USDT -
    # it is never confidence/risk/regime-scaled - so it's checked as-is.
    # DCA steps 1-5 CAN be scaled down by confidence sizing at runtime
    # (see confidence_size_multiplier / notional_for_step), so those are
    # checked at their worst case (SIZE_MIN_MULT) to make sure a
    # low-confidence DCA add can never silently fall below min_notional.
    for step in range(MAX_DCA_STEPS + 1):
        margin = INITIAL_ENTRY_USDT if step == 0 else INITIAL_ENTRY_USDT * (DCA_MULTIPLIER ** step)
        if step > 0:
            margin *= SIZE_MIN_MULT  # worst case (smallest allowed) DCA add size
        step_notional = margin * LEVERAGE
        ok = step_notional >= filters.min_notional
        label = "INITIAL" if step == 0 else f"DCA #{step} (min size)"
        print(color(
            f"[setup]   [{symbol}] {label}: margin=${margin:.2f} notional=${step_notional:.2f} "
            f"{'OK' if ok else 'BELOW MIN_NOTIONAL - will be skipped at runtime!'}",
            GRAY if ok else RED,
        ))

    if not DRY_RUN:
        await retry_with_backoff(client.set_leverage, symbol, LEVERAGE, label="set_leverage")
        await retry_with_backoff(client.set_margin_type, symbol, MARGIN_TYPE, label="set_margin_type")
        print(color(f"[setup] [{symbol}] leverage set to {LEVERAGE}x, margin type {MARGIN_TYPE}", GRAY))
    else:
        print(color(
            f"[setup] [{symbol}] [DRY RUN] would set leverage={LEVERAGE}x, marginType={MARGIN_TYPE}", GRAY
        ))

    manager = MartingaleManager(client, symbol, filters, LEVERAGE, portfolio=portfolio)

    if not DRY_RUN:
        balances = await retry_with_backoff(client.get_balance, label="get_balance")
        usdt = next((b for b in balances if b["asset"] == "USDT"), None)
        manager.available_balance = float(usdt["availableBalance"]) if usdt else 0.0
    else:
        manager.available_balance = manager.portfolio.paper_wallet
    print(color(f"[setup] [{symbol}] available balance: {manager.available_balance:.2f} USDT", GRAY))

    # 2026-08 instant warm-up fix: ONE REST call to GET /fapi/v1/klines
    # seeds the candle buffer with the last KLINE_WARMUP_LIMIT closed 1m
    # candles, so ATR / RSI / EMA / regime are valid within seconds of
    # boot instead of after ~an hour of the live tick stream building 57
    # candles by itself ("[entry-skip] startup warm-up: insufficient
    # market history (candles=5/57)"). Runs BEFORE the first
    # on_book_ticker() below and before the websocket consumers start, so
    # the live stream simply continues the series from the seeded history
    # - only fully-closed historical candles are seeded, the in-progress
    # bucket is always owned by the live stream (see
    # CandleAggregator.prime_from_klines). Best-effort: a failure here
    # just restores the old stream-only warm-up, it never blocks startup.
    await warm_up_candles_from_klines(client, manager)

    book = await retry_with_backoff(client.get_book_ticker, symbol, label="get_book_ticker")
    bid, ask = float(book["bidPrice"]), float(book["askPrice"])
    manager.on_book_ticker(bid, ask, float(book.get("bidQty", 0) or 0), float(book.get("askQty", 0) or 0))
    print(color(f"[setup] [{symbol}] current price: {manager.current_price:.2f}", GRAY))

    try:
        premium = await client.get_premium_index(symbol)
        manager.funding_rate = float(premium.get("lastFundingRate", 0) or 0)
        print(color(f"[setup] [{symbol}] current funding rate: {manager.funding_rate:.6f}", GRAY))
    except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(color(f"[setup] could not fetch initial funding rate (continuing without it): {_exc_text(e)}", YELLOW))

    # Persistent Adaptive Learning: local brain snapshot -> GitHub -> fresh model.
    await manager.load_or_init_brain()
    # Same GitHub session as the brain - restores trades_log.jsonl /
    # trades_log.csv / performance_stats.csv so trade history and
    # analytics survive an ephemeral restart exactly like brain.pkl does.
    await manager.restore_csv_logs_from_github()
    # 2026-08 restart-safe accounting fix: rebuilds trade_count /
    # realized_pnl_total / daily_realized_pnl (and
    # _daily_loss_tracker_date) from the trades_log JSONL just restored
    # above - MUST run after restore_csv_logs_from_github() (so it
    # reads restored history, not an empty fresh file) and before
    # load_trade_sync_cursor()/reconcile_trade_history_from_exchange()
    # ever gets a chance to run, so anything reconciliation goes on to
    # recover afterward is simply added on top of this base (see that
    # method's own docstring in trading.py for why this can never
    # double-count). Pure bookkeeping restore only - does not touch
    # DCA/position state, Brain V2, or any trading decision.
    await manager.restore_runtime_accounting_from_history()
    # Restores the trade-log reconciliation cursor (see
    # reconcile_trade_history_from_exchange) the same way, so a Railway
    # restart resumes catching up on Binance trade history from where
    # it left off instead of re-scanning (or missing) anything.
    await manager.load_trade_sync_cursor()
    # Restores a persisted DCA/position state snapshot (local disk, then
    # GitHub - see DCA_STATE_PATH / GITHUB_DCA_STATE_PATH) so an
    # in-progress DCA position survives an ephemeral restart instead of
    # starting flat. MUST run before initialize_sync() below, so that
    # initialize_sync()'s own exchange-vs-local reconciliation (unchanged)
    # has this rebuilt local state to compare against.
    await load_dca_state(manager)

    await initialize_sync(client, manager, context="startup")
    # item 6: exchange-native protective stop reconciliation - MUST run
    # after initialize_sync() (needs the authoritative post-reconcile
    # side/qty/avg_entry_price) and before the long-running consumer
    # loops start, so an OPEN position recovered above is never left
    # believing it's protected without this process actually having
    # confirmed that against Binance's own open orders.
    await reconcile_protective_stop_on_startup(client, manager)
    return manager


async def main() -> None:
    enforce_safety_gates()

    print(color("=" * 78, CYAN))
    print(color(" Martingale DCA Scalper - Binance USD-M Futures  [Brain V2]", BOLD))
    print(color(f" Symbol: {SYMBOL}   Testnet: {USE_TESTNET}   Dry-run: {DRY_RUN}", GRAY))
    from config import RUNTIME_ENV, EXPOSURE_GUARD_ENABLED, PAPER_START_BALANCE_USDT
    print(color(f" State namespace: {RUNTIME_ENV} | exposure admission: {EXPOSURE_GUARD_ENABLED}", GRAY))
    if DRY_RUN:
        print(color(f" Paper starting wallet: ${PAPER_START_BALANCE_USDT:.2f}; fresh experiment, no account orders", GRAY))
    # 2026-08-20 multi-coin: make the watchlist and the portfolio cap the
    # first thing visible in a deploy log, so which coins are live and how
    # many can trade at once is never in doubt.
    print(color(
        f" Watchlist: {', '.join(ACTIVE_SYMBOLS)}   "
        f"MAX_ACTIVE_TRADES: {MAX_ACTIVE_TRADES} (across ALL symbols)", BOLD,
    ))
    if USE_TESTNET:
        print(color(
            " *** TESTNET market data is thin/illiquid - bid/ask (and therefore "
            "momentum/ATR/candle formation) can stay flat for extended real-world "
            "stretches. That is expected testnet behavior, not a pipeline bug. "
            "Set USE_TESTNET=false for live mainnet market data. ***", YELLOW,
        ))
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
    print(color(
        f" Daily fee-net locks (UTC): profit=+${DAILY_PROFIT_TARGET_USDT:.2f}  "
        f"loss=-${MAX_DAILY_LOSS_USDT:.2f}  (new entries only)", GRAY,
    ))
    # 2026-08-21 notional-relative risk scaling: print what every dollar
    # threshold resolved to and, critically, which ones an env var is still
    # pinning. A leftover explicit override does not scale with
    # INITIAL_ENTRY_USDT, so it silently changes the strategy's geometry the
    # next time position size moves - it must not be invisible.
    # 2026-08-21 robust env parsing: any variable that was blank or
    # unparseable fell back to its code default instead of crashing at
    # import. That fallback must never be silent - a mistyped threshold
    # would otherwise change the strategy's risk geometry invisibly.
    env_warnings = env_parse_warnings()
    if env_warnings:
        print(color(
            f" [config] {len(env_warnings)} environment variable(s) could not be "
            f"parsed and fell back to code defaults:", YELLOW,
        ))
        for warning in env_warnings:
            print(color(f"     {warning}", YELLOW))

    scaling = notional_scaling_report()
    overridden = [(n, v) for n, v, src in scaling if src == "OVERRIDDEN"]
    print(color(
        f" Risk scaling: every dollar threshold is derived from the entry notional "
        f"(${ENTRY_NOTIONAL_USDT:.2f} = ${INITIAL_ENTRY_USDT} x {LEVERAGE}x). "
        f"{len(scaling) - len(overridden)}/{len(scaling)} derived.", GRAY,
    ))
    for name, value, src in scaling:
        print(color(
            f"     {name:36} ${value:>8.4f}  {src}",
            YELLOW if src == "OVERRIDDEN" else GRAY,
        ))
    if overridden:
        print(color(
            f" *** {len(overridden)} RISK THRESHOLD(S) ARE PINNED BY AN ENV VAR and will NOT "
            f"scale if INITIAL_ENTRY_USDT changes: "
            f"{', '.join(n for n, _ in overridden)}. Remove them to restore automatic "
            f"scaling. ***", RED,
        ))
    # 2026-08 rate-limit + warm-up fixes: surface both cadences up front, so a
    # Live log makes it obvious which REST budget and warm-up path is in play.
    print(color(
        f" REST polling: positionRisk {POSITION_RISK_POLL_SEC:.0f}s active / "
        f"{POSITION_RISK_POLL_IDLE_SEC:.0f}s idle, balance {BALANCE_REFRESH_SEC:.0f}s "
        f"(skipped while the user-stream copy is <{BALANCE_WS_FRESH_SEC:.0f}s old), "
        f"+/-{REST_POLL_JITTER_PCT * 100:.0f}% jitter", GRAY,
    ))
    print(color(
        f" Candle warm-up: "
        + (
            f"{KLINE_WARMUP_LIMIT} historical {KLINE_WARMUP_INTERVAL} klines via one REST call, "
            f"then live websocket updates"
            if KLINE_WARMUP_ENABLED else
            "DISABLED - live stream only (~1h before indicators are valid)"
        ), GRAY,
    ))
    if DRY_RUN:
        print(color(" *** DRY RUN MODE - no real orders will be sent ***", YELLOW))
        # 2026-08-29: the success head learns only from closed trades, and
        # before this a DRY_RUN order never filled - so no position ever
        # opened, none ever closed, and learn_success() never ran. State the
        # simulator's configuration at boot so "why is success_p still 0.5?"
        # is answerable from the first screen of logs.
        if DRY_FILL_ENABLED:
            print(color(
                f"     simulated fills ON - DRY_RUN orders fill against REAL "
                f"{'testnet' if USE_TESTNET else 'mainnet'} prices on a LATER tick "
                f"(never the submitting one), charged {DRY_FILL_SLIPPAGE_BPS:.1f}bps "
                f"adverse slippage + {DRY_FILL_TAKER_FEE_PCT*100:.3f}% taker fee. "
                f"Positions open and close, so the success head gets labels. "
                f"No capital is at risk.", GRAY,
            ))
        else:
            print(color(
                "     simulated fills OFF (DRY_FILL_ENABLED=false) - orders are logged "
                "and never filled, so no trade ever closes and the success head cannot "
                "learn. Recording and every other head are unaffected.", YELLOW,
            ))
    # 2026-08-24: state the recorder's configuration at boot. Without this the
    # only way to confirm it is running is to wait out the 10-minute status
    # loop, which makes "is it on?" needlessly slow to answer - and makes a
    # silently-disabled recorder look identical to a quiet one.
    if FEATURE_RECORDER_ENABLED:
        _horizons = feature_recorder_horizons()
        print(color(
            f" *** FEATURE RECORDER ON - sampling every "
            f"{FEATURE_RECORDER_INTERVAL_SEC:g}s per symbol, horizons "
            f"{'/'.join(f'{h:g}s' for h in _horizons)}, "
            f"{FEATURE_RECORDER_SHARD_SEC/3600:g}h shards ***", MAGENTA,
        ))
        print(color(
            f"     recording EVERY evaluated setup (accepted and rejected) to "
            f"{os.path.basename(FEATURE_LOG_PATH)} -> GitHub", GRAY,
        ))
        if FEATURE_LOG_RETENTION_ENABLED:
            print(color(
                f"     local retention: delete UPLOADED shards older than "
                f"{FEATURE_LOG_RETAIN_LOCAL_HOURS:g}h, cap "
                f"{FEATURE_LOG_MAX_LOCAL_MB:g} MB (GitHub copies are never "
                f"touched)", GRAY,
            ))
        else:
            print(color(
                "     local retention OFF - shards accumulate until the disk "
                "allowance runs out", YELLOW,
            ))
    else:
        print(color(
            " *** feature recorder OFF (set FEATURE_RECORDER_ENABLED=true "
            "to collect entry-rule research data) ***", GRAY,
        ))
    if BREAKOUT_ENGINE_ENABLED:
        print(color(
            f" *** BREAKOUT ENGINE ON - {BREAKOUT_TIMEFRAME} Donchian({BREAKOUT_CHANNEL}), "
            f"stop {BREAKOUT_STOP_ATR:g} ATR, target {BREAKOUT_TP_ATR:g} ATR, "
            f"trail {BREAKOUT_TRAIL_ATR:g} ATR from {BREAKOUT_TRAIL_START_ATR:g} ATR, "
            f"risking {BREAKOUT_RISK_PCT * 100:g}% per trade ***", MAGENTA,
        ))
        print(color(
            "     verify with: python3 backtest_breakout.py --months 6 "
            "- deploy only on profitable WALK-FORWARD results", GRAY,
        ))
    else:
        print(color(
            " *** breakout engine OFF - validate with backtest_breakout.py "
            "before setting BREAKOUT_ENGINE_ENABLED=true ***", GRAY,
        ))
    if DRY_RUN:
        print(color(" *** PAPER MODE - SIMULATED ORDERS, NO REAL MONEY EXECUTION ***", GRAY))
    elif not USE_TESTNET:
        print(color(" *** LIVE MAINNET MODE - REAL MONEY AT RISK ***", RED))

    print(color("=" * 78, CYAN))

    client = RestClient("" if DRY_RUN else API_KEY, "" if DRY_RUN else API_SECRET, REST_BASE)
    # 2026-08-20 multi-coin: `managers` is the watchlist registry, SYMBOL ->
    # manager. `manager` remains bound to the PRIMARY symbol's manager so
    # every pre-existing reference below (and the shutdown block) is
    # unchanged for a single-symbol deployment.
    managers: Dict[str, MartingaleManager] = {}
    manager: Optional[MartingaleManager] = None
    portfolio = PortfolioCoordinator(MAX_ACTIVE_TRADES)

    try:
        await retry_with_backoff(client.start, label="REST client startup / time sync")

        # ------------------------------------------------------------------
        # 2026-08-20 multi-coin: run the original per-symbol startup once per
        # watchlist entry, sequentially. Sequential and not gathered on
        # purpose: each symbol's startup issues a burst of signed REST calls
        # (filters, leverage, margin type, balance, klines, bookTicker,
        # positionRisk, userTrades), and firing four bursts concurrently is
        # exactly the pattern that earned this bot its HTTP 429 ban earlier.
        # Startup cost is a few seconds per symbol, paid once.
        #
        # A symbol that fails to start up is SKIPPED, not fatal - one bad
        # ticker in the watchlist must not stop the other three from
        # trading.
        # ------------------------------------------------------------------
        for sym in ACTIVE_SYMBOLS:
            try:
                managers[sym] = await setup_symbol(client, sym, portfolio)
            except SymbolNotListed as e:
                portfolio.managers.pop(sym, None)
                # Its own branch so the log says "not on this venue" rather
                # than burying it among genuine initialisation failures -
                # the fix is a watchlist change, not a retry.
                print(color(
                    f"[setup] {sym} is NOT LISTED here ({_exc_text(e)}) - excluded "
                    f"from this run; the remaining watchlist symbols continue.", YELLOW,
                ))
            except Exception as e:  # noqa: BLE001 - one symbol must not sink the watchlist
                print(color(
                    f"[setup] {sym} FAILED to initialise ({_exc_text(e)}) - it is excluded "
                    f"from this run; the remaining watchlist symbols continue.", RED,
                ))
        if not managers:
            raise RuntimeError(
                "no watchlist symbol initialised successfully - refusing to run blind"
            )
        # The primary symbol's manager backs every pre-existing single-manager
        # reference below (the shutdown block in particular). If the primary
        # itself failed to start, fall back to any survivor.
        manager = managers.get(SYMBOL) or next(iter(managers.values()))
        print(color(
            f"[setup] watchlist ready: {', '.join(managers)}  |  "
            f"MAX_ACTIVE_TRADES={MAX_ACTIVE_TRADES} (portfolio-wide)", CYAN,
        ))
        # 2026-08-20 multi-coin: a symbol whose exchange minimum notional is
        # above this bot's entry size can never place an order - every
        # attempt would be rejected. The per-step check inside setup_symbol()
        # already prints that per symbol, but with four coins scrolling past
        # it is easy to miss, so restate it once, loudly, as a single list.
        # This is a CONFIGURATION problem, not a runtime error: the symbol
        # stays in the watchlist and keeps learning, it just cannot trade
        # until INITIAL_ENTRY_USDT x LEVERAGE clears its minimum.
        entry_notional = INITIAL_ENTRY_USDT * LEVERAGE
        untradable = [
            (m.symbol, m.filters.min_notional)
            for m in managers.values()
            if entry_notional < m.filters.min_notional
        ]
        if untradable:
            detail = ", ".join(f"{sym} needs ${mn:.2f}" for sym, mn in untradable)
            print(color(
                f"[setup] *** {len(untradable)} WATCHLIST SYMBOL(S) CANNOT TRADE at the "
                f"current size: entry notional is ${entry_notional:.2f} "
                f"(INITIAL_ENTRY_USDT=${INITIAL_ENTRY_USDT} x {LEVERAGE}x) but {detail}. "
                f"They will evaluate and learn but never open a position. Raise "
                f"INITIAL_ENTRY_USDT/LEVERAGE or drop them from ACTIVE_SYMBOLS. ***", RED,
            ))

        # ------------------------------------------------------------------
        # Per-symbol loops are scheduled once per manager. Account-wide loops
        # are scheduled EXACTLY ONCE for the whole process:
        #   - listen_key_keepalive: one listenKey per account, not per symbol.
        #   - userdata_consumer:    one account-wide stream, demuxed by symbol
        #                           (see websocket.py). Four connections would
        #                           each carry all four symbols' events anyway.
        #   - balance_refresher:    USDT balance is account-wide; polling it
        #                           once per symbol would be 4x the REST cost
        #                           for the same number.
        # ------------------------------------------------------------------
        # Paper runs use public market data and their own cash ledger only.
        # A real user stream must never overwrite simulated positions/balance.
        tasks = []
        if not DRY_RUN:
            tasks.extend([
                listen_key_keepalive(client),
                userdata_consumer(client, manager, managers=managers),
                balance_refresher(client, manager),
            ])
        for m in managers.values():
            tasks.extend([
                market_data_consumer(m),
                position_risk_poller(client, m),
                funding_oi_poller(client, m),
                status_loop(m),
                brain_sync_loop(m),
                stats_export_loop(m),
                # 2026-08-24 feature recorder; both return immediately when
                # FEATURE_RECORDER_ENABLED is false, so scheduling them
                # unconditionally costs nothing.
                feature_log_loop(m),
                feature_log_status_loop(m),
            ])
        await asyncio.gather(*tasks)
    finally:
        # A timer-driven paper run may end with a simulated position still
        # open. Emit its fee-net mark-to-market BEFORE any optional shutdown
        # I/O so the experiment always has an economic result even if later
        # persistence/cleanup is interrupted. Never force-close at the
        # arbitrary cutoff: that would train the Brain on a synthetic label.
        if DRY_RUN:
            try:
                print_paper_summary(portfolio, managers, PAPER_START_BALANCE_USDT)
            except Exception as e:  # noqa: BLE001 - diagnostics must not block shutdown
                print(color(f"[paper-summary] failed: {_exc_text(e)}", YELLOW))
        # 2026-08-24: flush recorded rows before shutdown so an in-flight
        # shard is not lost when the container is reclaimed.
        for m in managers.values():
            try:
                m.feature_recorder.flush()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        # 2026-08-20 multi-coin: flush EVERY manager, not just the primary -
        # otherwise three of four coins would lose their final brain
        # snapshot, stats export and trade-log push on every restart. Each
        # step is individually guarded exactly as before, so one symbol
        # failing to flush never stops the others.
        for _m in list(managers.values()):
            if _m is manager:
                continue
            for _step, _label in (
                (lambda: _m.persist_brain(reason="shutdown"), "brain persist"),
                (lambda: _m.sync_trade_log_to_github(), "trade-log sync"),
                (lambda: _m.sync_performance_stats_to_github(), "stats sync"),
                (lambda: _m.github_sync.close(), "sync cleanup"),
            ):
                try:
                    await _step()
                except Exception as e:  # noqa: BLE001 - shutdown is best-effort
                    print(color(f"[shutdown] {_m.symbol} {_label} failed: {_exc_text(e)}", YELLOW))
            try:
                _m.perf_stats.export()
            except Exception:  # noqa: BLE001 - never block shutdown on stats export
                pass
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
                await manager.sync_trade_log_to_github()
            except Exception:  # noqa: BLE001 - never block shutdown on sync
                pass
            try:
                await manager.sync_performance_stats_to_github()
            except Exception:  # noqa: BLE001 - never block shutdown on sync
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
