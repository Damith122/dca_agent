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
# CONFIG (moved to config.py - imported here unchanged)
# ============================================================================

from config import (
    SYMBOL,
    USE_TESTNET,
    DRY_RUN,
    I_UNDERSTAND_THIS_IS_REAL_MONEY,
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
    TAKER_FEE_RATE,
    MIN_NET_PROFIT_USDT,
    LIQUIDATION_SANITY_MIN_RATIO,
    LIQUIDATION_SANITY_MAX_RATIO,
    LIQUIDATION_WARNING_BUFFER_PCT,
    SYNC_PENDING_GRACE_SEC,
    CANDLE_INTERVAL_SEC,
    CANDLE_HISTORY,
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
    BRAIN_AUTO_PUSH_INTERVAL_SEC,
    LISTEN_KEY_KEEPALIVE_SEC,
    BALANCE_REFRESH_SEC,
    POSITION_RISK_POLL_SEC,
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
# REST CLIENT (signed requests, HMAC-SHA256) - moved to exchange.py, imported
# below. SYMBOL FILTERS moved with it (fetch_symbol_filters calls
# client.get_exchange_info(), so it travels with the REST client).
# ============================================================================

from exchange import BinanceApiError, RestClient, SymbolFilters, fetch_symbol_filters


# ============================================================================
# TRADING EXECUTION - moved to trading.py, imported below. Candle,
# CandleAggregator, RegimeReading, MarketRegimeEngine, FeatureBuilderV2,
# RiskEngine, ConfidenceReading, ConfidenceEngine, EntryDecision,
# EntryEngineV2, RewardCalculator, TradeLogger, PerformanceStats,
# PositionState, MartingaleManager, and initialize_sync all moved together -
# see trading.py's module docstring for why they couldn't be split apart.
# ============================================================================

from trading import (
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
    initialize_sync,
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
