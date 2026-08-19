#!/usr/bin/env python3
"""
================================================================================
 Trading execution code - moved out of dca2.py

 This file contains ONLY what was relocated out of dca2.py: the complete
 "CANDLE" through "POSITION SYNC" sections - Candle, CandleAggregator,
 RegimeReading, MarketRegimeEngine, FeatureBuilderV2, RiskEngine,
 ConfidenceReading, ConfidenceEngine, EntryDecision, EntryEngineV2,
 RewardCalculator, TradeLogger, PerformanceStats, PositionState,
 MartingaleManager, and initialize_sync. Every formula, threshold, DCA/TP/SL
 rule, and risk calculation below is byte-for-byte identical to the original
 dca2.py source - nothing was fixed, renamed, tuned, or optimized. Only the
 location changed.

 Why all of these moved together as one unit, not just MartingaleManager:
 every one of these classes is constructed or used ONLY inside
 MartingaleManager (or by another class in this same list) - none of them
 are referenced anywhere else in dca2.py. Moving MartingaleManager alone
 would have meant importing all of these back from dca2.py, which would
 create a circular import (dca2.py imports MartingaleManager from here).
 So the whole tightly-coupled cluster moved together, exactly the same
 reasoning already used for RunningNormalizer+BrainV2 and
 listen_key_keepalive+the websocket consumers in earlier moves.

 initialize_sync moved along with PositionState/MartingaleManager for the
 same reason: it directly reads and rebuilds `manager.position`
 (a PositionState) by reconciling against the exchange's reported position -
 it's position-management/trading-execution logic, not the websocket or
 REST-polling code it happens to be called from. Its other two call sites
 (dca2.py's position_risk_poller and main()) now reach it via the import
 below, and websocket.py's late-bound injection of it 
 (`_websocket_module.initialize_sync = initialize_sync`) is unchanged in
 dca2.py except for where the name is imported from.

 Dependencies: config.py (constants), indicators.py (clamp/safe_div/
 ema_series/compute_atr/compute_atr_pct), brain.py (BrainV2), github_sync.py
 (GithubBrainSync), exchange.py (RestClient/SymbolFilters/BinanceApiError),
 plus stdlib + numpy + aiohttp. This module carries its own private copies
 of dca2.py's now_str()/color()/color-constants (same reasoning as in
 brain.py/websocket.py/github_sync.py: avoids a circular import back to
 dca2.py for two tiny formatting helpers).

 2026-07 Smart Exit fix: three gating changes to Smart Exit V2 in
 _manage_open_position(), scoped to that section only - no other logic
 (Entry, Brain, TP, DCA sizing, Hard Stop) was touched:
   1. Smart Exit is no longer evaluated at all until the position is at
      least SMART_EXIT_MIN_LOSS_PCT (-0.10%) adverse - it previously could
      fire even while still in profit as long as pct_move was above
      -SMART_EXIT_MAX_LOSS_PCT.
   2. If the current adverse move is already within
      SMART_EXIT_DCA_PROXIMITY_RATIO (90%) of the ATR-adaptive DCA trigger
      distance, Smart Exit is blocked outright so the DCA branch below can
      activate instead of racing it to close the trade first.
   3. SMART_EXIT_MIN_AGREE raised from 4 to 5 (see config.py) - now needs a
      stronger majority of the six independent signals to agree.

 NEW: Position-level Profit Lock (added, isolated to _manage_open_position()
 and PositionState only - does not touch Entry, Brain learning, DCA sizing/
 triggers, Smart Exit, TP, or Risk logic):
   - Uses the WHOLE POSITION's net unrealized PnL in USDT (via
     estimate_net_pnl_usdt(), which is already DCA-aware through
     avg_entry_price/total_qty - the same method the existing TP/partial-TP
     checks use, for consistency).
   - Once unrealized PnL first reaches +PROFIT_LOCK_ACTIVATION_USDT, the
     lock activates and the position's peak unrealized PnL is tracked from
     then on.
   - The protected/locked profit is always PROFIT_LOCK_RATIO (70%) of that
     peak.
   - If unrealized PnL falls back to or below the locked level, the entire
     position is closed immediately with exit_reason_tag="profit_lock".
   - Evaluated independently of held_long_enough/TP/Smart-Exit/DCA gates,
     right after the existing hard-stop/breakeven checks (which already
     return early on their own trigger conditions), so none of those
     branches are touched.

 2026-07 Profit Lock resync fix (isolated to initialize_sync() only - no
 other logic touched): profit_lock_active / peak_unrealized_pnl live on
 PositionState, which initialize_sync() rebuilds from scratch on a
 websocket reconnect/resync whenever local state doesn't already match
 what the exchange reports. That rebuild was unconditionally dropping any
 already-armed Profit Lock state for a trade that was simply continuing
 across the reconnect. initialize_sync() now captures the OLD position's
 profit_lock_active/peak_unrealized_pnl immediately before the rebuild and
 carries them into the new PositionState - but ONLY when the old local
 state was itself OPEN on the SAME side the exchange now reports (i.e. the
 same trade continuing, not a stale/closed trade being replaced by an
 unrelated new position). Any other prior status/side is treated as "no
 prior lock state to carry forward", exactly as before this fix. Profit
 Lock's own activation/ratio/close logic in _manage_open_position() is
 completely unchanged.

 2026-07 reconcile-throttle fix (isolated to initialize_sync() only - no
 other logic touched, see that function for details): initialize_sync()
 previously called reconcile_trade_history_from_exchange() (a GET
 /fapi/v1/userTrades REST call) unconditionally on every invocation -
 startup, every user-data-websocket reconnect, AND every
 position_risk_poller tick (every POSITION_RISK_POLL_SEC, i.e. every 10s),
 forever. While a close order is already pending for the current position
 (status == "CLOSING" with a pending_order_id in flight - including the
 restart-safe pending-close recovery path), that lifecycle hasn't closed
 on Binance's side yet, so re-fetching userTrades on every single poll
 tick finds nothing new and simply piles up redundant REST calls against
 an already-loaded endpoint - this was the source of the repeated
 502/504s observed after the pending-close recovery fix. Reconciliation
 is now skipped for the duration of that pending close only, and resumes
 automatically as soon as status leaves CLOSING (the very next startup /
 reconnect / poll call) - no closed trade is ever permanently skipped,
 and every other branch of initialize_sync() (already_synced check,
 OPEN/CLOSING position rebuild, DCA-state snapshot restore, Profit Lock
 carry-forward) is unchanged.

 2026-07 reconcile-backoff fix (isolated to
 reconcile_trade_history_from_exchange() only - no other logic touched):
 the pending-close throttle above stops redundant calls while OUR OWN
 close order is in flight, but it does nothing when Binance's userTrades
 endpoint itself is unhealthy (returning 502/504) - in that case every
 caller (startup, every user-ws reconnect, and position_risk_poller every
 POSITION_RISK_POLL_SEC ~10s) kept re-hitting the same failing endpoint
 immediately again, forever, which is exactly the repeated
 "[reconcile:*] could not fetch Binance trade history: HTTP 502" pattern
 reported in the field. MartingaleManager now tracks a cooldown
 (`_reconcile_cooldown_until`) and an exponential backoff duration
 (`_reconcile_backoff_sec`, seeded at RECONCILE_BACKOFF_BASE_SEC and
 capped at RECONCILE_BACKOFF_MAX_SEC) for this call only:
   - On entry, if we're still within a previously-set cooldown window,
     the call is skipped (logged once) and returns immediately - startup/
     reconnect/poll all continue normally without blocking on it.
   - On a network/API failure fetching userTrades (BinanceApiError /
     aiohttp.ClientError / asyncio.TimeoutError - i.e. exactly a 502/504
     or timeout), the cooldown is (re)armed and the backoff duration is
     doubled (capped), so repeated instability backs off further with
     each consecutive failure instead of retrying every ~10s indefinitely.
   - On any successful fetch (including an empty result), the backoff
     resets to its base value and the cooldown clears, so recovery is
     immediate once Binance is healthy again.
   - This does not change what gets reconciled/recovered, the dedup
     logic, the trade-sync cursor, or any entry/exit/DCA/risk decision -
     it only paces how often this one REST call is attempted while it is
     failing.

 2026-07 DCA-state-recovery fix (this update - isolated to
 _dca_state_snapshot(), save_dca_state() call sites inside
 _manage_open_position(), and MartingaleManager.__init__ only; the
 startup-side compare-and-restore logic in initialize_sync() was already
 correct and is UNCHANGED):
   - Root cause: the persisted DCA-state snapshot (dca_step, last_dca_price,
     profit_lock_active, peak_unrealized_pnl, ...) was only ever re-saved
     on an entry fill (_on_entry_filled) or a full close. Position-level
     Profit Lock's own state (profit_lock_active flipping on, and
     peak_unrealized_pnl growing tick-by-tick while a trade runs) changes
     independently of any entry/DCA fill, so a restart/redeploy between a
     Profit Lock activation (or a new, higher peak) and the next entry
     fill silently lost that state - initialize_sync() would then restore
     a STALE snapshot (profit_lock_active possibly still False, or a
     lower peak than what actually happened), even though dca_step/
     avg_entry/side/qty still matched the exchange perfectly.
   - Fix: the position snapshot is now also persisted (fire-and-forget,
     same asyncio.create_task(self.save_dca_state(...)) pattern used
     everywhere else in this file) the moment Profit Lock first activates,
     and again whenever its tracked peak meaningfully increases afterward
     (throttled via _last_dca_state_peak_saved so a profitable trade
     running for a while doesn't trigger a disk/GitHub write on every
     single tick). dca_step/last_dca_price were already saved correctly on
     every entry/DCA fill - unchanged.
   - Also added `initial_entry_price` (the price of the very first entry
     fill) to the persisted snapshot purely as an additional audit/
     debugging field - it is not required by and does not change the
     side/qty/avg_entry_price matching logic in initialize_sync(), which
     remains the sole authority for deciding whether a snapshot is trusted
     and applied on restart.
   - No entry/exit/DCA/TP/Smart-Exit/Brain/Risk decision, and no field or
     branch of initialize_sync() itself, was touched by this fix.

 2026-07 final-DCA gate fix (this update - isolated to the
 "Final-DCA low-probability-recovery gate" block inside
 _manage_open_position() only; no other logic - Entry, general DCA
 sizing/triggers, TP, Smart Exit, Brain/Risk scoring, or
 initialize_sync() - was touched):
   - Root cause: the gate treated a single weak signal - most commonly
     conf.confidence_score < 0.35 on its own - as sufficient to abandon
     the final DCA step and force-close the position. Brain V2's
     confidence_score legitimately runs low during ordinary sideways
     chop/consolidation even when nothing is actually wrong, so this was
     closing out normal sideways recoveries the last DCA add would
     otherwise have handled fine, before the trade ever got a chance to
     recover.
   - Fix: confidence_score is no longer part of this gate at all. The
     gate now looks only at four independent "genuinely low probability
     of recovery" signals - strong trend against the position (trend
     confidence bar raised from 0.35 to 0.55), high heuristic risk score
     (bar raised from 0.65 to 0.75), abnormal (not just mildly adverse)
     momentum against the position (velocity threshold raised from
     0.0004 to 0.0008), and extreme volatility (HIGH_VOL regime AND
     atr_ratio meaningfully beyond the regime engine's own HIGH_VOL
     cutoff) - and only skips the final DCA / closes the position when at
     least two of these four agree. A single borderline reading (e.g. one
     adverse tick, or a momentarily elevated risk score) can no longer
     trigger this on its own. Every other DCA step, DCA sizing, and every
     other branch of _manage_open_position() is unchanged.

 2026-08 Smart Exit V2 retune (isolated to the Smart Exit V2 block inside
 _manage_open_position() and its two new config constants only - signal
 calculation in _smart_exit_v2_signals(), Profit Lock, DCA, TP, Hard Stop,
 Brain scoring, and initialize_sync() are all untouched):
   - Root cause: testnet observation showed most losing trades closing via
     smart_exit rather than hard_stop/max_dca_exhausted, with Smart Exit
     firing on ordinary pullbacks in choppy/ranging conditions before mean
     reversion had a chance to play out.
   - Fix 1: Smart Exit now gates on its own, stricter minimum hold
     (SMART_EXIT_MIN_HOLD_SEC, 90s) instead of the general held_long_enough
     (60s, still used unchanged by TP/partial-TP/trailing-stop), so a
     freshly opened position gets extra room before Smart Exit can act.
   - Fix 2: in SIDEWAYS/WEAK_TREND regimes, the required signal agreement
     bar rises from SMART_EXIT_MIN_AGREE (5/6) to
     SMART_EXIT_MIN_AGREE_RANGING (6/6, unanimous), since ordinary chop in
     these regimes routinely trips 1-2 of the 6 signals without a genuine
     reversal. STRONG_TREND/HIGH_VOL keep the original 5/6 bar unchanged,
     so real reversals/breakdowns are still closed just as quickly as
     before.
   - Nothing about the six underlying signals, the loss/DCA-proximity
     gates, or any other exit path (TP, trailing stop, hard stop, profit
     lock, max DCA exhausted, final-DCA gate) was changed.
================================================================================
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import aiohttp
import numpy as np

# 2026-08 post-only (maker) entry execution: the SELL-side maker price must
# round UP to the tick grid, which round_step() (ROUND_DOWN only) cannot do.
# Decimal is used for the same precision reason round_step() itself uses it.
from decimal import Decimal, ROUND_UP

from config import (
    SYMBOL,
    RUNTIME_ENV,
    DRY_RUN,
    LEVERAGE,
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
    MAX_HOLD_TIME_ENABLED,
    MAX_HOLD_TIME_SEC,
    MAX_HOLD_TIME_HARD_CAP_SEC,
    MAX_HOLD_TIME_SMALL_LOSS_PCT,
    MAX_HOLD_TIME_RECOVERY_MIN_AGREE,
    MAX_HOLD_TIME_DCA_MULTIPLIER,
    CLOSE_VERIFY_MAX_RETRIES,
    LOW_VOLATILITY_FILTER_ENABLED,
    LOW_VOLATILITY_ATR_PCT_THRESHOLD,
    ADAPTIVE_SIZING_ENABLED,
    ADAPTIVE_SIZE_SENSITIVITY,
    ADAPTIVE_SCALE_MIN,
    ADAPTIVE_SCALE_MAX,
    ADAPTIVE_TP_MIN_RATIO,
    ADAPTIVE_TP_MAX_RATIO,
    ENTRY_MOMENTUM_SATURATION_PCT,
    PROFIT_LOCK_ACTIVATION_USDT,
    PROFIT_LOCK_RATIO,
    SIGNAL_LOOKBACK_TICKS,
    SIGNAL_DEADBAND_PCT,
    TRADE_COOLDOWN_SEC,
    MIN_HOLD_SEC_BEFORE_EXIT,
    MAX_DAILY_LOSS_USDT,
    DAILY_PROFIT_TARGET_USDT,
    TAKER_FEE_RATE,
    MIN_NET_PROFIT_USDT,
    LIQUIDATION_WARNING_BUFFER_PCT,
    MAX_TRADE_NET_LOSS_USDT,
    MAX_TRADE_EXIT_BUFFER_USDT,
    PROTECTIVE_STOP_ENABLED,
    PROTECTIVE_STOP_WORKING_TYPE,
    PROTECTIVE_STOP_CLIENT_ID_PREFIX,
    PROTECTIVE_STOP_RETRY_SEC,
    PROTECTION_PENDING_MAX_SEC,
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
    RECENT_TRADE_WINDOW,
    ENTRY_SCORE_THRESHOLD,
    SIDEWAYS_ENTRY_SCORE_THRESHOLD,
    SIDEWAYS_ENTRY_MOMENTUM_ALIGNMENT_ENABLED,
    SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO,
    ENTRY_WEIGHTS,
    SMART_EXIT_ENABLED,
    SMART_EXIT_MAX_LOSS_PCT,
    SMART_EXIT_MIN_LOSS_PCT,
    SMART_EXIT_MIN_AGREE,
    SMART_EXIT_CONFIDENCE_DROP,
    SMART_EXIT_ATR_MOVE_MULT,
    SMART_EXIT_DCA_PROXIMITY_RATIO,
    SMART_EXIT_MIN_HOLD_SEC,
    SMART_EXIT_MIN_AGREE_RANGING,
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
    BRAIN_LOCAL_PATH,
    GITHUB_TOKEN,
    GITHUB_REPO,
    GITHUB_BRAIN_PATH,
    GITHUB_BRANCH,
    GITHUB_TRADES_LOG_CSV_PATH,
    GITHUB_STATS_CSV_PATH,
    GITHUB_TRADES_LOG_JSON_PATH,
    TRADE_SYNC_CURSOR_PATH,
    GITHUB_TRADE_SYNC_CURSOR_PATH,
    TRADE_RECONCILE_BACKFILL_FROM_ID,
    SESSION_START_DATE,
    DCA_STATE_PATH,
    GITHUB_DCA_STATE_PATH,
    # ------------------------------------------------------------------
    # 2026-08 high-frequency orderflow upgrade (appended config block -
    # see the banner at the bottom of config.py). Every name below is
    # NEW; not one pre-existing import above was removed or renamed.
    # ------------------------------------------------------------------
    ENABLE_ORDERBOOK_GUARD,
    ORDERBOOK_IMBALANCE_THRESHOLD,
    AGG_TRADE_DELTA_WINDOW_SEC,
    MAX_STOP_LOSS_USD,
    TARGET_PROFIT_USD,
    COOL_OFF_PERIOD_MINUTES,
    USE_POST_ONLY_LIMIT,
    ORDERBOOK_SUPPORT_MIN,
    AGG_TRADE_DELTA_MIN,
    ORDERBOOK_GUARD_REQUIRES_DATA,
    MIN_STOP_LOSS_USD,
    MIN_TARGET_PROFIT_USD,
    ENFORCE_RISK_REWARD_USD,
    POST_ONLY_LIMIT_OFFSET_TICKS,
    POST_ONLY_LIMIT_TIMEOUT_SEC,
    POST_ONLY_MARKET_FALLBACK,
    CONTINUOUS_24_7_TRADING,
    COOL_OFF_IMBALANCE_TIGHTEN,
    COOL_OFF_SUPPORT_MIN,
    SMART_ORDERFLOW_EXIT_ENABLED,
    SMART_ORDERFLOW_EXIT_IMBALANCE,
    SMART_ORDERFLOW_EXIT_MIN_LOSS_USD,
    SMART_ORDERFLOW_EXIT_MAX_LOSS_USD,
    SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC,
    DCA_REQUIRE_ORDERBOOK_SUPPORT,
    DCA_RESCUE_SUPPORT_MIN,
    DCA_RESCUE_BREAKEVEN_ENABLED,
    DCA_RESCUE_BREAKEVEN_MIN_NET_USD,
)
from indicators import clamp, safe_div, ema_series, round_step
from exchange import BinanceApiError, RestClient, SymbolFilters
# 2026-08 high-frequency orderflow upgrade: OrderFlowTracker is the bounded
# in-memory ring-buffer data layer defined in websocket.py (the module that
# owns the market feed it is fed from). websocket.py imports only config +
# exchange + stdlib and never imports this module, so this is a plain
# one-directional dependency - no circular import is created, and dca2.py's
# existing `from websocket import ...` / initialize_sync injection is
# completely unaffected.
from websocket import OrderFlowTracker

# ----------------------------------------------------------------------------
# Private helpers (identical copies of dca2.py's now_str()/color()/color
# constants - duplicated only to avoid a circular import; see module
# docstring above).
# ----------------------------------------------------------------------------

import sys
from datetime import datetime, timezone


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


_USE_COLOR = sys.stdout.isatty()


def color(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


GREEN, RED, YELLOW, CYAN, GRAY, BOLD, MAGENTA, BLUE = "32", "31", "33", "36", "90", "1", "35", "34"


def sanitize_recovered_dca_step(raw_step) -> Tuple[int, Optional[str]]:
    """Return a recovered DCA step inside the configured hard limit.

    A non-None reason means the input was invalid/out of range. Snapshot
    callers use that reason as a conservative DCA safety block instead of
    treating the clamped number as proof that more exposure is safe.
    """
    try:
        numeric = float(raw_step or 0)
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError
        parsed = int(numeric)
    except (TypeError, ValueError, OverflowError):
        return 0, f"recovered dca_step={raw_step!r} is not a valid integer"

    clamped = min(max(parsed, 0), MAX_DCA_STEPS)
    if clamped != parsed:
        return clamped, (
            f"recovered dca_step={parsed} is outside the configured range "
            f"0..{MAX_DCA_STEPS}; clamped to {clamped}"
        )
    return clamped, None


# ============================================================================
# PROFIT LOCK (new, position-level, DCA-aware - see module docstring) -------
# PROFIT_LOCK_ACTIVATION_USDT / PROFIT_LOCK_RATIO now live in config.py
# (2026-08 Railway-tuning fix, see config.py) so they can be tuned via
# environment variables without a code edit - same default values as
# before, imported above.
# ============================================================================

# DCA-state peak-save throttle (2026-07 DCA-state-recovery fix): minimum
# growth in peak_unrealized_pnl (USDT) since the last persisted snapshot
# before Profit Lock's growing peak triggers another save_dca_state() call.
# Keeps a long-running profitable trade from writing to disk/GitHub on
# every single tick while still keeping the persisted peak reasonably
# fresh for restart/redeploy recovery.
DCA_STATE_PEAK_SAVE_MIN_DELTA_USDT = 0.02


# ============================================================================
# RECONCILE BACKOFF (new - see 2026-07 reconcile-backoff fix in module
# docstring). Local constants only, scoped to trading.py, governing how
# reconcile_trade_history_from_exchange() paces retries against Binance's
# userTrades endpoint while it is returning 502/504 or timing out.
# ============================================================================

RECONCILE_BACKOFF_BASE_SEC = 30.0     # initial cooldown armed after the first failure
RECONCILE_BACKOFF_MAX_SEC = 300.0     # cap on the cooldown even after repeated failures

# 2026-08 orphan-close self-heal (fix D). How far back to re-fetch Binance
# trade history, by time, when the reconciliation window turns out to start
# AFTER a closed position's entry leg (leaving a close fill with no matching
# entry - see reconcile_trade_history_from_exchange). Must comfortably cover
# the longest a position can be held: MAX_HOLD_TIME_HARD_CAP_SEC is 8h by
# default, so 24h leaves ample margin while staying well inside Binance's
# 7-day userTrades startTime/endTime limit.
ORPHAN_REWIND_LOOKBACK_MS = 24 * 60 * 60 * 1000


# ============================================================================
# SESSION START FILTER (new - 2026-07 session-start filter, see config.py's
# SESSION_START_DATE docstring). Parsed once at import time; used only by
# reconcile_trade_history_from_exchange() to gate which recovered Binance
# trades are allowed into trades_log.csv / trades_log.jsonl /
# performance_stats.csv. Falls back to "no cutoff" (epoch 0) if
# SESSION_START_DATE is unset or malformed, so a bad value never blocks
# startup or reconciliation.
# ============================================================================

try:
    SESSION_START_MS = int(
        datetime.fromisoformat(SESSION_START_DATE.replace("Z", "+00:00")).timestamp() * 1000
    )
except Exception:  # noqa: BLE001 - a malformed/missing cutoff must never block startup
    SESSION_START_MS = 0


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
    history in memory; nothing here is persisted to disk. As of the 2026-08
    instant warm-up fix, a restart no longer has to rebuild that history one
    streamed candle at a time: prime_from_klines() below seeds it from a
    single REST klines call at startup, and the live streams take over from
    there (see warm_up_candles_from_klines)."""

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

    def prime_from_klines(self, klines: List[list], now_ts: Optional[float] = None) -> int:
        """2026-08 instant warm-up fix. Seeds the rolling history with
        already-CLOSED historical candles fetched in one REST call
        (GET /fapi/v1/klines - see RestClient.get_klines), so ATR / EMA /
        regime are valid within seconds of boot instead of after ~an hour of
        the live tick stream building 57 one-minute candles by itself.

        Real-time precision is deliberately NOT traded away for this:

          - Only candles whose bucket is strictly OLDER than the current
            bucket are seeded. Binance's last kline row is the still-forming
            candle; it is dropped, because the in-progress bucket belongs to
            the live websocket stream alone (self._current, fed by on_price).
          - Any bucket the live stream has already produced WINS over the
            seeded one, so priming after ticks have started flowing can never
            overwrite real observed data with a REST snapshot.
          - self._current and self._bucket_start are never touched, so an
            already-open live bucket keeps accumulating exactly as it was.

        Buy/sell volume is reconstructed from the kline's own taker-buy base
        volume (field [9]), matching on_trade()'s taker-side convention:
        buy_volume = takerBuyBase, sell_volume = volume - takerBuyBase.

        Malformed/short rows are skipped individually rather than aborting
        the warm-up - a partial seed is still far better than none. Returns
        the number of candles actually seeded.
        """
        if not klines:
            return 0

        now_ts = now_ts if now_ts is not None else time.time()
        live_bucket = self._bucket_start if self._bucket_start is not None else self._bucket_for(now_ts)

        seeded: Dict[float, Candle] = {}
        for row in klines:
            try:
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    continue
                open_time = float(row[0]) / 1000.0
                bucket = self._bucket_for(open_time)
                if bucket >= live_bucket:
                    continue  # in-progress bucket - owned by the live stream
                volume = float(row[5] or 0)
                taker_buy = float(row[9] or 0) if len(row) > 9 else volume / 2.0
                buy_volume = max(0.0, min(taker_buy, volume))
                seeded[bucket] = Candle(
                    open_time=bucket,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    buy_volume=buy_volume,
                    sell_volume=max(0.0, volume - buy_volume),
                )
            except (TypeError, ValueError, IndexError):
                continue  # one malformed row must never abort the warm-up

        if not seeded:
            return 0

        merged: Dict[float, Candle] = dict(seeded)
        # Live-observed candles always win over a seeded historical one.
        for candle in self.candles:
            merged[candle.open_time] = candle

        ordered = [merged[k] for k in sorted(merged)]
        maxlen = self.candles.maxlen or len(ordered)
        self.candles = deque(ordered[-maxlen:], maxlen=maxlen)
        return sum(1 for c in self.candles if c.open_time in seeded)

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
# - moved to indicators.py, imported below.
# ============================================================================

from indicators import compute_atr, compute_atr_pct


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
        self._last_log_ts: float = 0.0
        self._log_interval_sec: float = 15.0

    def _should_log(self) -> bool:
        now = time.time()
        if now - self._last_log_ts >= self._log_interval_sec:
            self._last_log_ts = now
            return True
        return False

    def evaluate(self, candles: List[Candle]) -> RegimeReading:
        if len(candles) < max(EMA_SLOW, ATR_PERIOD) + 2:
            if self._should_log():
                print(color(
                    f"{now_str()} [regime-debug] insufficient candles "
                    f"({len(candles)} < {max(EMA_SLOW, ATR_PERIOD) + 2}) - "
                    f"returning default RegimeReading (regime=SIDEWAYS)",
                    GRAY,
                ))
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

        if self._should_log():
            print(color(
                f"{now_str()} [regime-debug] atr={atr:.6f} atr_ratio={atr_ratio:.4f} "
                f"slope={slope:.6f} ema_fast={ema_fast:.4f} ema_slow={ema_slow:.4f} "
                f"regime={regime} "
                f"REGIME_ATR_HIGH_MULT={REGIME_ATR_HIGH_MULT:.4f} "
                f"REGIME_ATR_LOW_MULT={REGIME_ATR_LOW_MULT:.4f} "
                f"REGIME_TREND_SLOPE_STRONG={REGIME_TREND_SLOPE_STRONG:.6f} "
                f"REGIME_TREND_SLOPE_WEAK={REGIME_TREND_SLOPE_WEAK:.6f}",
                GRAY,
            ))

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
# BRAIN V2 - probability / confidence engine (moved to brain.py - imported
# here unchanged). RunningNormalizer moved with it since it's only used
# internally by BrainV2.
# ============================================================================

from brain import RunningNormalizer, BrainV2
# 2026-08 strategy upgrade: the Liquidity & Flow Guard lives alongside the
# Brain (see brain.py's own banner for why) and is applied by EntryEngineV2
# below, after every pre-existing technical check has already run.
from brain import LiquidityFlowGuard


# ============================================================================
# CLOUD-SYNC BRAIN (push/pull brain snapshot to GitHub across ephemeral
# restarts) - moved to github_sync.py, imported below.
# ============================================================================

from github_sync import GithubBrainSync


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
    def __init__(self):
        self._last_log_ts: float = 0.0
        self._log_interval_sec: float = 15.0
        # 2026-08 Liquidity & Flow Guard (brain.py). Stateless and pure -
        # constructed once here purely to avoid re-allocating it on every
        # tick. All per-decision thresholds are passed in at call time by
        # evaluate() below, so the post-loss cool-off can tighten them
        # without mutating any shared state.
        self.liquidity_guard = LiquidityFlowGuard()

    def _should_log(self) -> bool:
        now = time.time()
        if now - self._last_log_ts >= self._log_interval_sec:
            self._last_log_ts = now
            return True
        return False

    def evaluate(
        self,
        conf: ConfidenceReading,
        regime: RegimeReading,
        volume_z: float,
        momentum: float,
        features: np.ndarray,
        brain_readiness: Optional[dict] = None,
        orderflow: Optional[dict] = None,
        imbalance_threshold: Optional[float] = None,
        support_min: Optional[float] = None,
    ) -> EntryDecision:
        # 2026-08 entry-quality audit fix (item 10): brain_readiness is
        # purely a logging input (READY/WARMING_UP/UNRELIABLE per model
        # head + meaningful sample counts - see BrainV2.head_readiness()) -
        # it does not affect should_enter/score/components below in any
        # way. The actual reliability gating already happened upstream, in
        # BrainV2.predict_all() (confidence/success/tp_hit inputs to
        # `conf` are already neutral-if-unreliable by the time they reach
        # here) - this is only surfaced so the acceptance/rejection log
        # line is auditable without cross-referencing brain internals.
        if conf.trend_direction is None or conf.trend_confidence <= 0:
            return EntryDecision(False, None, 0.0, {"rejection_reason": "no directional trend signal"})

        # --- Regime gate (2026-07 profitability fix) ---------------------------
        # SIDEWAYS/HIGH_VOL/LOW_VOL entries had no measurable directional edge
        # and were a major source of losing trades. Only trending regimes -
        # where there is an actual directional bias to align with - are
        # allowed to open fresh entries. This is a hard block, not a score
        # discount: a blocked regime can never reach should_enter=True
        # regardless of how high the rest of the score comes in.
        allowed_regimes = (
            REGIME_STRONG_TREND,
            REGIME_WEAK_TREND,
            REGIME_SIDEWAYS,
        )
        regime_blocked = regime.regime not in allowed_regimes

        # --- Low-volatility ("dead market") hard gate (NEW) ---------------------
        # MarketRegimeEngine's SIDEWAYS/LOW_VOL split is RELATIVE (current ATR
        # vs its own recent rolling mean - see MarketRegimeEngine.evaluate), so
        # a genuinely dead/flat tape can still be classified SIDEWAYS (allowed,
        # at a lower score threshold) instead of LOW_VOL (already blocked) if
        # the rolling mean itself has also been low for a while. This is an
        # ABSOLUTE floor on atr_pct, independent of that ratio, so it only
        # blocks truly dead conditions - normal ranging/SIDEWAYS markets above
        # the floor are completely unaffected. Like the regime gate above, this
        # is a hard block, not a score discount, and only applies when we
        # actually have a valid (nonzero) ATR reading, so warmup / insufficient-
        # candle ticks (atr_pct == 0.0 default) never get blocked by this.
        dead_market_blocked = (
            LOW_VOLATILITY_FILTER_ENABLED
            and regime.atr_pct > 0
            and regime.atr_pct < LOW_VOLATILITY_ATR_PCT_THRESHOLD
        )
        regime_blocked = regime_blocked or dead_market_blocked

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

        # (2026-08 entry-timing fix) `momentum` is now the short rolling
        # return (features[4], ~5 candles - see on_price_tick) rather than a
        # single bookTicker-tick return. Root cause of the prior bug: a
        # single-tick return is typically ~1e-5 for BTC, so scoring it
        # against a 0.2% (0.002) saturation threshold meant this component
        # was effectively always ~0 regardless of real market momentum -
        # the 0.13 weight assigned to it in ENTRY_WEIGHTS was silently dead.
        # ENTRY_MOMENTUM_SATURATION_PCT (config.py) is sized for the
        # multi-candle rolling return actually being passed in now.
        momentum_magnitude = clamp((abs(momentum) / ENTRY_MOMENTUM_SATURATION_PCT), 0.0, 1.0)
        momentum_aligned = (
            (conf.trend_direction == "LONG" and momentum > 0)
            or (conf.trend_direction == "SHORT" and momentum < 0)
        )
        # 2026-08 clean-Live entry-quality fix: abs(momentum) used to
        # reward a strong move even when it ran AGAINST Brain's proposed
        # side. In SIDEWAYS, regime_fit is deliberately neutral (0.5), so
        # nothing else checked this direction. Preserve magnitude scoring
        # for aligned entries and give counter-momentum no positive score.
        # A meaningful counter move is also a hard SIDEWAYS entry block;
        # tiny sign jitter below the configured ratio remains score-only.
        sideways_counter_momentum_blocked = (
            SIDEWAYS_ENTRY_MOMENTUM_ALIGNMENT_ENABLED
            and regime.regime == REGIME_SIDEWAYS
            and momentum_magnitude >= SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO
            and not momentum_aligned
        )
        regime_blocked = regime_blocked or sideways_counter_momentum_blocked
        momentum_component = momentum_magnitude if momentum_aligned else 0.0

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
            "momentum_raw": momentum,
            "momentum_magnitude": momentum_magnitude,
            "momentum_aligned": momentum_aligned,
            "sideways_counter_momentum_blocked": sideways_counter_momentum_blocked,
            "regime_fit": regime_fit,
            "risk_score": conf.risk_score,
            # 2026-08 entry-quality audit fix (item 10 - issue #7): quality_pred
            # is recorded here for visibility/auditability only - it is
            # deliberately NOT added to ENTRY_WEIGHTS/the score sum below.
            # Confirmed: BrainV2 computes it (the quality/reward-prediction
            # head) but it was never wired into the composite Entry Score.
            # Left unwired in this pass rather than assigning it an
            # untested weight from only two live trades - see the final
            # report's entry-quality-audit section for the full rationale.
            "quality_pred": conf.quality_pred,
        }

        score = 0.0
        for key, weight in ENTRY_WEIGHTS.items():
            val = components.get(key, 0.0)
            if key == "risk_score":
                score -= weight * val   # risk SUBTRACTS from the score
            else:
                score += weight * val

        active_threshold = (
            SIDEWAYS_ENTRY_SCORE_THRESHOLD
            if regime.regime == REGIME_SIDEWAYS
            else ENTRY_SCORE_THRESHOLD
        )
        components["threshold"] = active_threshold

        # --- Liquidity & Flow Guard (2026-08 upgrade) ------------------------
        # Applied here, AFTER every pre-existing technical component has
        # already been computed and scored - the EMA/RSI/ATR stack, the
        # regime gate, the dead-market floor, the counter-momentum block and
        # the composite ENTRY_WEIGHTS score above are all completely
        # untouched by this upgrade. This adds two things:
        #
        #   (a) A HARD VETO on the proposed side, on the same footing as the
        #       existing regime/dead-market/counter-momentum hard blocks: a
        #       vetoed side can never reach should_enter=True regardless of
        #       how strong the technical score is.
        #           BLOCK LONG  if imbalance < -threshold OR 10s delta < 0
        #           BLOCK SHORT if imbalance > +threshold OR 10s delta > 0
        #
        #   (b) MULTI-FACTOR CONFIRMATION: technical_signal AND
        #       orderbook_support AND flow_delta_alignment must ALL hold.
        #       `technical_ok` below is exactly the pre-upgrade acceptance
        #       decision (regime allowed AND score >= threshold), so this is
        #       a strict tightening - it can only ever reject a trade the
        #       old code would have taken, never accept one it would have
        #       refused.
        #
        # `orderflow=None` (no orderflow wiring at all - e.g. a focused unit
        # test constructing EntryEngineV2 directly) makes the guard inert
        # and preserves the exact pre-upgrade behavior. See
        # LiquidityFlowGuard in brain.py.
        liquidity = self.liquidity_guard.evaluate(
            conf.trend_direction,
            orderflow,
            imbalance_threshold=imbalance_threshold,
            support_min=support_min,
        )
        liquidity_blocked = bool(liquidity["blocked"])
        components["liquidity_blocked"] = liquidity_blocked
        components["liquidity_reason"] = liquidity["reason"]
        components["orderbook_imbalance"] = liquidity["imbalance"]
        components["trade_delta"] = liquidity["trade_delta"]
        components["orderbook_support"] = liquidity["book_support"]
        components["flow_aligned"] = liquidity["flow_aligned"]
        components["orderflow_data_available"] = liquidity["data_available"]
        components["liquidity_guard_active"] = liquidity["active"]
        components["imbalance_threshold_used"] = liquidity["threshold"]

        technical_ok = (not regime_blocked) and score >= active_threshold
        components["technical_signal"] = technical_ok
        # The three confirmation factors, all required.
        multi_factor_confirmed = (
            technical_ok and liquidity["book_support"] and liquidity["flow_aligned"]
        )
        components["multi_factor_confirmed"] = multi_factor_confirmed

        should_enter = technical_ok and (not liquidity_blocked) and multi_factor_confirmed
        if regime_blocked:
            rejection_reason = (
                "dead_market_blocked" if dead_market_blocked
                else "sideways_counter_momentum_blocked" if sideways_counter_momentum_blocked
                else "regime_not_allowed"
            )
        elif not technical_ok:
            rejection_reason = f"score {score:.4f} below threshold {active_threshold:.4f}"
        elif liquidity_blocked:
            rejection_reason = f"liquidity_guard: {liquidity['reason']}"
        elif not multi_factor_confirmed:
            rejection_reason = "multi_factor_confirmation_incomplete"
        else:
            rejection_reason = "accepted"
        components["rejection_reason"] = rejection_reason

        readiness = brain_readiness or {}

        if self._should_log():
            print(color(
                f"{now_str()} [entry-debug] regime={regime.regime} "
                f"regime_blocked={regime_blocked} dead_market_blocked={dead_market_blocked} "
                f"counter_momentum_blocked={sideways_counter_momentum_blocked} "
                f"atr_pct={regime.atr_pct:.6f} "
                f"brain_confidence={components['brain_confidence']:.4f} "
                f"trend_confidence={components['trend_confidence']:.4f} "
                f"volume_confirmation={components['volume_confirmation']:.4f} "
                f"volatility_fit={components['volatility_fit']:.4f} "
                f"momentum={components['momentum']:.4f} raw_momentum={momentum:+.6f} "
                f"momentum_aligned={momentum_aligned} "
                f"regime_fit={components['regime_fit']:.4f} "
                f"risk_score={components['risk_score']:.4f} "
                f"quality_pred={conf.quality_pred:+.4f} "
                f"success_p={conf.success_probability:.4f} tp_hit_p={conf.tp_hit_probability:.4f} "
                f"final_score={score:.4f} threshold={active_threshold:.4f} "
                f"decision={rejection_reason} "
                f"ob_imbalance={liquidity['imbalance']:+.4f} "
                f"flow_delta={liquidity['trade_delta']:+.4f} "
                f"ob_support={liquidity['book_support']} flow_aligned={liquidity['flow_aligned']} "
                f"orderflow_data={liquidity['data_available']} "
                f"imbalance_thr={liquidity['threshold']:.3f} "
                f"brain_ready=[trend={readiness.get('trend','?')} "
                f"success={readiness.get('success','?')}({readiness.get('success_samples','?')}) "
                f"tp_hit={readiness.get('tp_hit','?')}({readiness.get('tp_hit_samples','?')}) "
                f"noise={readiness.get('noise','?')}({readiness.get('noise_samples','?')}) "
                f"updates={readiness.get('update_count','?')}]",
                GRAY,
            ))

        # Never throttle the exact accepted tick. Previously the periodic
        # [entry-debug] line could show a sub-threshold tick, then a real
        # order appeared before the next 15-second log, leaving no evidence
        # of which inputs actually authorized it. One line per real entry is
        # low volume and freezes the decision before async fill timing can
        # change self.last_confidence.
        if should_enter:
            print(color(
                f"{now_str()} [entry-accepted] side={conf.trend_direction} "
                f"score={score:.4f} threshold={active_threshold:.4f} "
                f"regime={regime.regime} atr_pct={regime.atr_pct:.6f} "
                f"brain_confidence={conf.confidence_score:.4f} "
                f"trend_confidence={conf.trend_confidence:.4f} "
                f"success_p={conf.success_probability:.6f} "
                f"tp_hit_p={conf.tp_hit_probability:.6f} risk={conf.risk_score:.4f} "
                f"quality_pred={conf.quality_pred:+.4f} "
                f"volume_confirmation={volume_confirmation:.4f} "
                f"raw_momentum={momentum:+.6f} "
                f"momentum_magnitude={momentum_magnitude:.4f} "
                f"momentum_aligned={momentum_aligned} "
                f"ob_imbalance={liquidity['imbalance']:+.4f} "
                f"flow_delta={liquidity['trade_delta']:+.4f} "
                f"imbalance_thr={liquidity['threshold']:.3f} "
                f"multi_factor_confirmed={multi_factor_confirmed} "
                f"brain_ready=[trend={readiness.get('trend','?')} "
                f"success={readiness.get('success','?')}({readiness.get('success_samples','?')}) "
                f"tp_hit={readiness.get('tp_hit','?')}({readiness.get('tp_hit_samples','?')}) "
                f"noise={readiness.get('noise','?')}({readiness.get('noise_samples','?')}) "
                f"updates={readiness.get('update_count','?')}]",
                GREEN,
            ))
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


def trade_log_close_time_str(ts: Optional[float] = None) -> str:
    """Full UTC timestamp for the trades_log.csv / trades_log.jsonl
    'close_time' field: "YYYY-MM-DD HH:MM:SS UTC". This is the one and
    only place both the live-close path and
    reconcile_trade_history_from_exchange() should build that field from,
    so they always agree on format. Deliberately kept separate from
    now_str() (short "HH:MM:SS", used only for console/log print prefixes
    elsewhere in this file) - this keeps the fix scoped to trade-log
    serialization and doesn't touch anything now_str() is used for.
    `ts` is an optional Unix timestamp (as reconciliation already has, from
    the exchange's trade history); omitted for the live-close path, which
    means "now"."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


# 2026-08 Brain-contamination fix: exit reasons that are caused by local
# infrastructure/API failure rather than by anything the entry signal got
# right or wrong. Trades closed for these reasons are still recorded in full
# (PnL, fees, CSV/JSONL, daily counters, trade_count) - only Brain TRAINING
# and the recent-win-rate feature skip them, so an outage cannot teach the
# entry model that a normal setup fails. See _on_close_filled().
INFRASTRUCTURE_ONLY_EXIT_REASONS = frozenset({
    "protection_unavailable",   # protective stop could not be armed (e.g. the
                                # Binance Algo-Service migration -4120 outage)
})

TRADE_LOG_FIELDS = [
    "close_time", "symbol", "side", "entry_price", "exit_price", "qty",
    "invested_notional", "gross_pnl_usdt", "fees_usdt", "net_pnl_usdt",
    "net_pnl_pct", "dca_count", "holding_time_sec", "mfe_pct", "mae_pct",
    "exit_reason", "tp_hit", "smart_exit", "manual_exit", "hard_stop",
    "entry_regime", "exit_regime", "entry_confidence", "entry_risk_score",
    "entry_success_prob", "entry_tp_hit_prob", "reward", "final_outcome",
    "exit_order_id",
    "smart_exit_agree_count", "smart_exit_required_agree",
    "smart_exit_signals_fired", "smart_exit_dca_distance_pct",
    # 2026-08 entry-quality audit fix (item 10): the composite Entry Score
    # actually compared against the acceptance threshold, and the threshold
    # itself - NOT the same value as entry_confidence above (which is only
    # the brain_confidence SUB-component of that composite score; see
    # EntryEngineV2.evaluate). Recorded so a trade's real accept/reject
    # basis is auditable from the trade log alone.
    "entry_composite_score", "entry_score_threshold",
    # 2026-08 per-trade net-loss budget fix (item 5): the estimated
    # fee-net PnL (executable bid/ask side, actual fees + estimated closing
    # fee) at the moment the MAX_TRADE_NET_LOSS_USDT gate triggered a close
    # - blank/0 for any trade that did not exit via that gate. Compare
    # against this row's own net_pnl_usdt (the FINAL realized fee-net PnL)
    # to see how much slippage/delay occurred between trigger and fill.
    "loss_budget_trigger_est_net_pnl",
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
        self._migrate_csv_schema_if_needed()
        self._csv_header_written = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0

    def mark_header_present(self) -> None:
        """Re-checks csv_path on disk and refreshes the cached header-written
        flag. Needed because TradeLogger is constructed (and caches this
        flag) before an async GitHub restore can write a downloaded CSV to
        csv_path - without this, the next log_trade() would re-write a
        header into the middle of the restored file."""
        self._migrate_csv_schema_if_needed()
        self._csv_header_written = os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0

    def _migrate_csv_schema_if_needed(self) -> None:
        """Fail-soft schema guard: if csv_path already has a header written
        under an OLDER version of TRADE_LOG_FIELDS (e.g. before
        `exit_order_id` was added), DictWriter would keep appending
        current-schema rows under that stale header - producing a column-
        count mismatch (28-column header, 29-column rows) that breaks
        Excel/CSV parsing. If the on-disk header doesn't exactly match the
        current TRADE_LOG_FIELDS, the whole file is rewritten in place:
        every existing row is reflowed into the current field set (any
        newly-added column, e.g. exit_order_id, is filled blank for old
        rows) under a fresh matching header, so every row - past and
        future - has exactly the same column count. No-op if the file
        doesn't exist yet or its header already matches."""
        if not (os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0):
            return
        try:
            with open(self.csv_path, "r", newline="", encoding="utf-8") as f:
                existing_header = next(csv.reader(f), None)
        except Exception as e:  # noqa: BLE001 - a corrupt/unreadable file must not block startup
            print(color(f"[trade-log] could not inspect CSV header for schema check: {e}", YELLOW))
            return

        if existing_header == TRADE_LOG_FIELDS:
            return  # already the current schema - nothing to migrate

        try:
            with open(self.csv_path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:  # noqa: BLE001
            print(color(f"[trade-log] could not read CSV rows for schema migration: {e}", YELLOW))
            return

        tmp_path = f"{self.csv_path}.tmp"
        try:
            with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS, extrasaction="ignore", restval="")
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
            os.replace(tmp_path, self.csv_path)  # atomic on POSIX
            print(color(
                f"[trade-log] migrated {self.csv_path} to current CSV schema "
                f"({len(TRADE_LOG_FIELDS)} columns, {len(rows)} row(s) reflowed; "
                f"old header had {len(existing_header) if existing_header else 0} columns).", YELLOW,
            ))
        except Exception as e:  # noqa: BLE001 - migration must never crash the trading loop
            print(color(f"[trade-log] CSV schema migration failed: {e}", YELLOW))

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

    def logged_binance_order_ids(self) -> set:
        """Every Binance order id already represented in trades_log.jsonl
        (populated via the `binance_order_ids` field written by both the
        live fill path and the reconciliation safety net - see
        MartingaleManager._on_close_filled / reconcile_trade_history_from_exchange).
        Used purely for duplicate-prevention: if any fill belonging to a
        candidate Binance trade lifecycle is already in this set, that
        lifecycle is treated as already logged and skipped. Records logged
        before this field existed simply contribute nothing here, which is
        fine - they are not re-processed by reconciliation because it only
        ever looks forward from a persisted trade-id cursor, never back
        over old history it hasn't already been told to touch."""
        ids: set = set()
        for record in self.load_all():
            for oid in record.get("binance_order_ids") or []:
                try:
                    ids.add(int(oid))
                except (TypeError, ValueError):
                    continue
        return ids


# ============================================================================
# PERFORMANCE STATISTICS - computed continuously from the trade log and
# exported to JSON/CSV on a fixed interval.
# ============================================================================


class PerformanceStats:
    def __init__(
        self, logger: TradeLogger, json_path: str = STATS_JSON_PATH, csv_path: str = STATS_CSV_PATH,
        symbol: str = SYMBOL,
    ):
        self.logger = logger
        self.json_path = json_path
        self.csv_path = csv_path
        # 2026-08 multi-symbol state isolation: trade logs are now
        # symbol-scoped by filename (config.py's TRADE_LOG_*_PATH), so
        # self.logger should already only ever contain this symbol's
        # trades. This filter remains as a defensive extra guard (e.g.
        # against an explicit env-var override accidentally pointing two
        # symbols at the same log file) rather than the primary
        # correctness mechanism - harmless no-op in the normal case.
        self.symbol = symbol

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        """float(t.get(key, default)) only falls back to `default` when
        `key` is absent - if the key is PRESENT but explicitly None (as
        several analytics-only fields are on reconciled/recovered trades;
        see reconcile_trade_history_from_exchange()'s record dict, which
        can't know entry_confidence/entry_regime/mfe_pct/mae_pct etc. since
        those were never observed live), float(None) still raises
        TypeError. This treats None the same as "missing" - falls back to
        `default` - and is otherwise a no-op passthrough to float(), so
        every existing calculation on well-formed (non-None) fields is
        unchanged."""
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def compute(self) -> dict:
        # 2026-08 multi-symbol state isolation: only this symbol's trades
        # participate in the rollup below - everything from this point on
        # in compute() is completely unchanged (same formulas, same
        # fields), just fed a pre-filtered list instead of every trade
        # ever logged across every symbol.
        trades = [t for t in self.logger.load_all() if t.get("symbol") == self.symbol]
        n = len(trades)
        if n == 0:
            return {"trade_count": 0, "generated_at": now_str()}

        sf = self._safe_float
        net_pnls = [sf(t.get("net_pnl_usdt"), 0.0) for t in trades]
        wins = [p for p in net_pnls if p > 0]
        losses = [p for p in net_pnls if p <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        total_fees = sum(sf(t.get("fees_usdt"), 0.0) for t in trades)
        net_profit = sum(net_pnls)

        win_rate = safe_div(len(wins), n, 0.0)
        loss_rate = safe_div(len(losses), n, 0.0)
        profit_factor = safe_div(gross_profit, gross_loss, default=float("inf") if gross_profit > 0 else 0.0)
        avg_win = safe_div(gross_profit, len(wins), 0.0)
        avg_loss = safe_div(gross_loss, len(losses), 0.0)
        expectancy = win_rate * avg_win - loss_rate * avg_loss

        hold_times = [sf(t.get("holding_time_sec"), 0.0) for t in trades]
        dca_counts = [sf(t.get("dca_count"), 0.0) for t in trades]

        def _side_stats(side: str) -> dict:
            side_trades = [t for t in trades if t.get("side") == side]
            side_pnls = [sf(t.get("net_pnl_usdt"), 0.0) for t in side_trades]
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
            regime_pnls = [sf(t.get("net_pnl_usdt"), 0.0) for t in regime_trades]
            by_regime[regime_name] = {
                "count": len(regime_trades),
                "win_rate": safe_div(len([p for p in regime_pnls if p > 0]), len(regime_trades), 0.0),
                "net_profit": sum(regime_pnls),
            }

        # entry_confidence is None on recovered trades (never observed live)
        # - excluded from the distribution entirely rather than counted as
        # a fabricated 0.0, so recovered trades don't silently skew the
        # mean/percentiles of a metric they never actually had.
        confidences = [
            sf(t.get("entry_confidence"), 0.0) for t in trades if t.get("entry_confidence") is not None
        ]
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
            pnl = sf(t.get("net_pnl_usdt"), 0.0)
            d["net_profit"] += pnl
            if pnl > 0:
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
    # 2026-08 DCA resync-race fix: wall-clock time.time() of the most
    # recent locally-confirmed entry/DCA fill (_on_entry_filled). Lets
    # initialize_sync() tell "exchange REST position data just hasn't
    # caught up to a fill we already confirmed" apart from a genuine
    # mismatch - see the OPEN-status grace block in initialize_sync().
    # Not persisted/restored from the DCA-state snapshot: a fresh process
    # (restart/redeploy) has no in-flight fill to protect, so it correctly
    # starts at 0.0 and this grace simply does not apply on that path.
    last_fill_ts: float = 0.0
    # 2026-08 DCA State Recovery V2: which order_id filled the initial
    # entry / most recent DCA add - audit/recovery fields only, never read
    # by any entry/exit/DCA/risk decision (mirrors initial_entry_price's
    # own audit-only role in _dca_state_snapshot()).
    last_entry_order_id: Optional[int] = None
    last_dca_order_id: Optional[int] = None

    # 2026-08 hard DCA safety invariant: set instead of guessing dca_step=0
    # whenever initialize_sync() cannot confidently recover the true
    # dca_step for an already-open position (REST order-status lookup
    # failed/ambiguous, or no matching DCA-state snapshot was found at
    # all). While True, _manage_open_position()'s DCA trigger treats this
    # position exactly like dca_step >= MAX_DCA_STEPS (no further DCA add
    # is ever placed) regardless of the numeric dca_step value - but does
    # NOT touch TP / Hard Stop / Smart Exit / Profit Lock / Max Hold Time,
    # which all keep managing the position normally. Persisted in the
    # DCA-state snapshot so it survives a restart instead of silently
    # clearing. Only ever cleared by a fresh FLAT position (a new trade
    # starts with dca_blocked=False, the dataclass default).
    dca_blocked: bool = False
    dca_block_reason: Optional[str] = None

    # -- partial TP / breakeven / trailing -------------------------------------
    partial_tp_done: bool = False
    breakeven_armed: bool = False
    breakeven_price: Optional[float] = None
    trailing_stop_price: Optional[float] = None
    max_favorable_price: Optional[float] = None
    max_adverse_price: Optional[float] = None

    # -- Profit Lock (new - see module docstring) ------------------------------
    profit_lock_active: bool = False
    peak_unrealized_pnl: float = 0.0

    # -- entry-time snapshot, for training / logging ---------------------------
    entry_features: Optional[np.ndarray] = None
    entry_regime: str = REGIME_SIDEWAYS
    entry_confidence: float = 0.0
    entry_risk_score: float = 0.0
    entry_success_prob: float = 0.5
    entry_tp_hit_prob: float = 0.5
    entry_dynamic_tp_pct: float = TAKE_PROFIT_PCT
    realized_fees_usdt: float = 0.0
    # 2026-08 entry-quality audit fix (item 10): the actual composite Entry
    # Score and threshold this trade was accepted under - see
    # TRADE_LOG_FIELDS's own comment for why this differs from
    # entry_confidence above.
    entry_composite_score: float = 0.0
    entry_score_threshold: float = 0.0
    # 2026-08 per-trade net-loss budget fix (item 5): estimated fee-net PnL
    # at the instant the MAX_TRADE_NET_LOSS_USDT gate triggered this
    # position's close - None if this position never triggered that gate.
    trade_loss_budget_trigger_pnl: Optional[float] = None

    # -- exchange-native protective stop (item 6) ------------------------------
    # A resting CONDITIONAL STOP_MARKET closePosition=true ALGO order on
    # Binance itself, placed immediately after each confirmed entry/DCA fill
    # so the position is protected even if this process cannot reach the REST
    # API (see the HTTP 418 IP-ban incident this addresses). None whenever no
    # such order is currently believed to be resting on the exchange.
    #
    # 2026-08 Algo-Service migration: this is an `algoId` from
    # POST /fapi/v1/algoOrder, NOT an `orderId` from /fapi/v1/order. Binance
    # rejects conditional types on the plain order endpoint with -4120, which
    # is exactly what broke the three live validation trades.
    protective_stop_algo_id: Optional[int] = None
    protective_stop_price: Optional[float] = None
    # 2026-08 protective-stop ownership fix (review finding 4): the
    # clientAlgoId this bot assigned to the currently-tracked protective stop
    # (always prefixed PROTECTIVE_STOP_CLIENT_ID_PREFIX). Reconciliation uses
    # the PREFIX to decide ownership (never adopting/cancelling a manual or
    # third-party algo order) and this exact value to recognise THIS
    # position's own order. Persisted so ownership survives a restart.
    protective_stop_client_algo_id: Optional[str] = None
    # 2026-08 Algo-Service migration: an algo order does NOT itself produce
    # ORDER_TRADE_UPDATE. When it triggers, Binance creates a CHILD MARKET
    # order and reports its id as `actualOrderId`; THAT child is what fills
    # and emits ORDER_TRADE_UPDATE. Recorded here (and persisted) as soon as
    # it is learned - from an ALGO_UPDATE TRIGGERING/TRIGGERED/FINISHED event
    # or a REST query - and registered in _order_index under role
    # "protective_stop" so the child fill routes through the SAME
    # _on_close_filled() bookkeeping every other close uses, exactly once.
    protective_stop_actual_order_id: Optional[int] = None
    # 2026-08 cancel-confirmation fix (review finding 5): set when a cancel
    # attempt for protective_stop_algo_id failed for an indeterminate reason
    # (network/timeout/REST cooldown/unexpected API error). The order MAY
    # still be resting on Binance, so its id is deliberately NOT cleared -
    # the periodic sweep in _manage_open_position() retries the cancel until
    # it either succeeds or Binance proves the order is gone (-2011).
    protective_stop_cancel_pending: bool = False
    # True whenever this process cannot confirm a protective stop is
    # correctly resting on the exchange for the current position (initial
    # placement failed, a DCA-triggered replace failed, or startup
    # reconciliation found none). While True: no new DCA is submitted (see
    # _manage_open_position), and placement/replacement is retried on a
    # throttled schedule. Never blocks TP/Hard-Stop/Profit-Lock/Smart-Exit/
    # Max-Hold/the per-trade net-loss budget - those remain fully active
    # and are, if anything, MORE relied upon while this is True.
    protection_pending: bool = False
    protection_pending_reason: Optional[str] = None
    # 2026-08 PROTECTION_PENDING fail-safe (review finding 3): wall-clock
    # time.time() when this position FIRST became unprotected, and the last
    # retry attempt's timestamp. Together these drive the throttled retry
    # (PROTECTIVE_STOP_RETRY_SEC) and the bounded fail-safe close
    # (PROTECTION_PENDING_MAX_SEC) in _manage_open_position(). Persisted so
    # a restart cannot silently reset the unprotected clock to zero.
    protection_pending_since: Optional[float] = None
    protection_last_retry_ts: float = 0.0


class MartingaleManager:
    def __init__(self, client: RestClient, symbol: str, filters: SymbolFilters, leverage: int):
        self.client = client
        self.symbol = symbol
        self.filters = filters
        self.leverage = leverage

        self.position = PositionState()
        # 2026-08 position_sync_ready startup gate: starts False on every
        # fresh MartingaleManager - a local/GitHub DCA-state snapshot
        # restore (load_dca_state() in dca2.py) NEVER sets this True, no
        # matter how structurally valid the snapshot looks, because a
        # snapshot only proves what THIS process last wrote to disk, not
        # what Binance's position actually is right now (it could have
        # changed via manual intervention, a missed fill, liquidation,
        # etc. while this process was down). Only initialize_sync() sets
        # this True, and only after it has actually obtained authoritative
        # position-risk rows from Binance and reconciled local state
        # against them - see that function. Consulted by on_price_tick()
        # (blocks new entries), _manage_open_position() (blocks DCA and
        # the Max Hold V2 / Smart Exit discretionary decision blocks), and
        # nothing else - Hard Stop / Profit Lock / TP / Trailing / the
        # invalid-open-state safety gate above all remain fully active
        # regardless of this flag, since they are simple, deterministic,
        # already qty-safe (close_position() re-fetches the exchange's own
        # positionAmt right before submitting) risk-REDUCING exits, never
        # exposure-adding or provisional-economics-dependent decisions.
        self.position_sync_ready: bool = False
        self.current_price: Optional[float] = None
        self.prev_price: Optional[float] = None
        self.prev_prev_price: Optional[float] = None
        self.available_balance: float = 0.0
        self.liquidation_price: Optional[float] = None
        # 2026-08 HTTP 429 REST rate-limit fix - websocket-first state
        # tracking. Binance's own 429 body asks callers to "use the
        # websocket for live updates to avoid polling the API", and the
        # user-data stream's ACCOUNT_UPDATE event already carries both the
        # USDT wallet balance and this symbol's position amount. These three
        # fields record what the stream last told us and when, so the REST
        # pollers in dca2.py can (a) skip the balance refresh entirely while
        # the websocket copy is fresh, and (b) drop the positionRisk poll to
        # its slow idle cadence while the stream says nothing is open.
        # Written ONLY by on_account_update() below; read-only everywhere
        # else. Never used to open, size, close or DCA a position - the
        # exchange-authoritative REST/reconcile paths remain the sole
        # authority for every trading decision, exactly as before.
        self.last_account_update_ts: float = 0.0
        self.ws_position_amt: Optional[float] = None
        self.ws_position_ts: float = 0.0

        self.price_history: List[float] = []   # kept for the fallback static momentum signal only
        self.trade_count = 0
        self.realized_pnl_total = 0.0
        # 2026-08 Daily Loss Protection: separate from realized_pnl_total
        # (whole-process-lifetime "session_total") - this resets at every
        # UTC calendar day boundary via _maybe_reset_daily_loss_tracker(),
        # called from both the entry gate (on_price_tick) and the two
        # realized-PnL accumulation points (_apply_partial_close,
        # _on_close_filled) below. Purely a new-entry gate - never read by
        # any exit/DCA/risk-management code path, so an already-OPEN
        # position is completely unaffected regardless of this value.
        self.daily_realized_pnl: float = 0.0
        self._daily_loss_tracker_date: Optional[str] = None  # UTC "YYYY-MM-DD" the current daily_realized_pnl covers
        self._last_daily_loss_block_log_ts: float = 0.0  # throttles the "entries halted" diagnostic line
        self._last_daily_profit_block_log_ts: float = 0.0  # throttles the fee-net daily-profit lock diagnostic line
        # 2026-08 close-verification fix: a single logical "close this
        # position" action can now take more than one reduceOnly order (a
        # genuine partial fill, or a fill landing on the position in the
        # gap between the pre-close qty fetch and the order executing).
        # _closing_accumulated_rp sums realized PnL across every leg of the
        # SAME close sequence so the eventual trade-log record reflects the
        # whole trade, not just the last leg. Reset to 0.0 at the start of
        # every FRESH close_position() call; NOT reset between retry legs
        # of the same sequence (see _on_close_filled()).
        self._closing_accumulated_rp: float = 0.0
        self._closing_retry_count: int = 0
        self.last_trade_action_ts: float = 0.0
        self.last_trade_open_ts: float = 0.0
        self._last_warmup_skip_log_ts: float = 0.0  # throttles the pre-warmup "no entry" log line
        self._last_max_hold_review_log_ts: float = 0.0  # throttles the Max Hold Time V2 "kept alive" diagnostic line
        self._last_invalid_open_state_log_ts: float = 0.0  # throttles the [invalid-open-state] safety-gate diagnostic line
        self._last_sync_not_ready_log_ts: float = 0.0  # throttles the [entry-skip]/[dca-skip]/[max-hold-skip]/[smart-exit-skip] position_sync_ready diagnostic lines
        self._last_max_hold_fee_net_review_log_ts: float = 0.0  # throttles the [max-hold-review] fee-net meaningful_loss diagnostic line
        self._last_max_dca_exhausted_review_log_ts: float = 0.0  # throttles the [max-dca-exhausted-review] diagnostic line
        self._last_dca_spacing_log_ts: float = 0.0  # throttles the [dca-spacing] diagnostic line
        self._last_max_hold_dca_defer_log_ts: float = 0.0  # throttles the Max Hold Time V2 "DCA opportunity available" diagnostic line
        self._max_hold_dca_defer_pending: bool = False  # set True when Max Hold Time V2 defers for a DCA opportunity this tick; consumed by _on_entry_filled() to log "[dca] executed after max-hold defer"
        self._last_dca_time_blocked_log_ts: float = 0.0  # throttles the [dca-time-blocked] diagnostic line (2026-08 Option B DCA time gate)
        self._last_profit_lock_debug_log_ts: float = 0.0  # throttles the [profit-lock-debug] diagnostic line
        self._last_profit_lock_peak_update_log_ts: float = 0.0  # throttles the [profit-lock-peak] UPDATED diagnostic line
        self._last_dca_post_step_timeout_log_ts: float = 0.0  # throttles the [dca-blocked-post-step-timeout] diagnostic line (item 8)
        self._last_dca_loss_budget_log_ts: float = 0.0  # throttles the [dca-budget] blocked diagnostic line (item 7)
        self._last_order_cooldown_block_log_ts: float = 0.0  # throttles the [order-cooldown-block] diagnostic line (item 4 - REST cooldown retry-storm fix)
        self._last_protection_pending_log_ts: float = 0.0  # throttles the [protective-stop] high-severity PROTECTION_PENDING diagnostic line (item 6)
        self._last_algo_envelope_warn_ts: float = 0.0  # throttles the [algo-update] UNATTRIBUTED diagnostic (2026-08 fix A)
        # 2026-08 reconciliation entry-leg fix (fix B): the SMALLEST Binance
        # trade id belonging to the position that is currently open. Set on
        # the first fill observed after being flat, cleared whenever the
        # position returns to FLAT, and persisted in the DCA-state snapshot
        # so it survives a restart. reconcile_trade_history_from_exchange()
        # uses it as a floor so the userTrades window can never start AFTER
        # this position's own entry leg - which is what turned the LIVE
        # protective-stop close into an unrecoverable orphan fill.
        self._open_position_first_trade_id: Optional[int] = None
        # 2026-08 orphan-close self-heal (fix D): the orphan trade id this
        # process has already attempted a time-based backfill for, so a
        # genuinely unmatchable fill is retried once and then left alone
        # instead of re-fetching Binance history on every poll.
        self._orphan_rewind_attempted_id: Optional[int] = None

        # --- Brain V2 stack -----------------------------------------------------
        self.candles = CandleAggregator()
        self.feature_builder = FeatureBuilderV2()
        self.regime_engine = MarketRegimeEngine()
        self.risk_engine = RiskEngine()
        self.brain = BrainV2(N_FEATURES_V2, BRAIN2_WARMUP_UPDATES)
        # 2026-08 Brain rollback-safety fix: the highest Brain
        # update_count this process has ever confirmed to exist (set once
        # load_or_init_brain() finishes selecting the winning snapshot, and
        # advanced again after every successful GitHub push in
        # persist_brain()). Used only as a cheap, local, no-extra-API-call
        # guard so a persist can never silently push a lower-update Brain
        # over a known-higher remote one - see persist_brain() below.
        self._brain_max_known_update_count: int = 0
        self.confidence_engine = ConfidenceEngine()
        self.entry_engine = EntryEngineV2()
        self.reward_calc = RewardCalculator()
        self.trade_logger = TradeLogger()
        self.perf_stats = PerformanceStats(self.trade_logger, symbol=self.symbol)

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
        self._last_synced_csv_hash: Dict[str, Optional[str]] = {}

        self._order_index: Dict[int, str] = {}
        # 2026-08 protective-stop ownership fix (review finding 4):
        # monotonic per-process counter feeding _new_protective_stop_client_algo_id().
        self._protective_stop_seq: int = 0
        # 2026-08 cancel-confirmation fix (review finding 5): protective-stop
        # order ids whose cancellation could NOT be confirmed at the moment
        # the position went FLAT. PositionState is discarded on close, so
        # these would otherwise be orphaned on the exchange with no local
        # record. Swept (retried) from on_price_tick() - which runs every
        # tick regardless of whether a position is open - until Binance
        # either accepts the cancel or proves the order is gone.
        self._orphan_protective_algo_ids: set = set()
        self._last_orphan_sweep_ts: float = 0.0
        # 2026-08 startup-reconciliation safety: True whenever this process
        # has NOT successfully enumerated Binance's open orders for this
        # symbol. While True, _place_or_replace_protective_stop() refuses to
        # place a new stop - placing one blind could stack a second
        # closePosition=true STOP_MARKET on top of one this process simply
        # could not see. Cleared the moment a get_open_orders() call
        # succeeds; the per-tick sweep retries reconciliation until then.
        self._protective_stop_reconcile_blocked: bool = False
        # 2026-08 stale-leftover safety: True whenever a bot-owned protective
        # stop from a PREVIOUS position may still be resting on Binance and
        # has not been confirmed cancelled - i.e. a FLAT cleanup could not be
        # completed (open orders could not be enumerated, or a cancel
        # failed). While True: no NEW entry is opened (a stale
        # closePosition=true stop would otherwise trigger against the new
        # position at a price computed for a completely different trade), and
        # reconciliation refuses to ADOPT any discovered owned stop - it can
        # only be a leftover, so it is cancelled instead. Cleared only once a
        # successful enumeration proves every stale owned stop is gone.
        self._stale_protective_stops_possible: bool = False
        # 2026-08 hard DCA safety invariant: transient scratch field set by
        # initialize_sync() (via _resolve_pending_order_via_rest()) when a
        # pending order's REST resolution comes back ambiguous/failed
        # ("unknown") - read and cleared by initialize_sync() itself in the
        # same call before it finishes rebuilding PositionState, so the
        # resulting position is marked dca_blocked=True instead of
        # silently defaulting to dca_step=0 with DCA still allowed. Not
        # persisted directly (PositionState.dca_blocked/dca_block_reason
        # are the persisted fields); this is only the hand-off between the
        # grace-block REST check and the rebuild further down in the same
        # function call.
        self._pending_dca_block_reason: Optional[str] = None
        self._rp_accum: Dict[int, float] = {}
        # 2026-08 realized-PnL/fee-accounting fix: actual Binance commission
        # ("n" field on each fill), accumulated per order_id exactly like
        # _rp_accum tracks "rp" - popped and rolled into
        # _position_fees_accum (below) once that order's FILLED event
        # arrives.
        self._fee_accum: Dict[int, float] = {}
        # Running total of ACTUAL commission paid across THIS position's
        # entire lifecycle (initial entry + every DCA add + any partial
        # closes + the close leg(s), including close-verification retries)
        # - reset to 0.0 the moment a fresh "initial" entry fills, and
        # consumed (read + reset) in _on_close_filled()'s finalize block.
        # This is the field that makes commission accounting correct for a
        # DCA'd trade instead of only accounting for the final close's fee.
        self._position_fees_accum: float = 0.0
        # False the moment any fill in this trade reports a commission
        # asset other than USDT (e.g. BNB fee discount enabled on the
        # account) - in that case _position_fees_accum cannot be trusted as
        # a USDT figure, and _on_close_filled() falls back to the existing
        # estimate_round_trip_fee_usdt() estimate for the whole trade
        # instead of silently mixing a foreign-currency number into USDT.
        self._position_fees_reliable: bool = True
        # 2026-08 entry-context/commission race fix: per-role commission
        # breakdown, purely for the final-close diagnostic (does not affect
        # any accounting decision - _position_fees_accum above remains the
        # single authoritative total used for combined_net_pnl). Same
        # reset/consume lifecycle as _position_fees_accum: zeroed on a
        # fresh "initial" entry, read (not reset individually) at finalize.
        self._entry_commission_accum: float = 0.0
        self._dca_commission_accum: float = 0.0
        self._exit_commission_accum: float = 0.0

        # --- Unmatched-fill buffer (2026-08 fill-tracking race fix) --------
        # Bridges the gap between placing an order (REST call returns an
        # orderId) and this same coroutine registering that orderId in
        # _order_index immediately afterward. Under asyncio, the user-data
        # websocket's FILLED event for that exact order can be scheduled and
        # processed by handle_order_update() WHILE this coroutine is still
        # awaiting the REST response - at that instant _order_index doesn't
        # have the id yet, so the fill was previously dropped permanently
        # (logged as "untracked_order_id") for "initial"/"dca" roles, which
        # left dca_step stuck and triggered spurious RESYNCING TO MATCH
        # EXCHANGE on the next periodic poll. Buffering the raw event here
        # (keyed by order_id, with an arrival timestamp for TTL pruning) lets
        # _place_step_order()/close_position() replay it the instant they
        # register the id, closing the race regardless of which side wins.
        # Does not change entry/exit/DCA/risk decisions - purely a delivery
        # guarantee for fill events this process's own orders generate.
        self._unmatched_fills: Dict[int, Tuple[dict, float]] = {}
        # 2026-08 close-fill reliability fix: widened from 15s to 30s.
        # Root cause of a real missed replay: the websocket connection
        # typically delivers a FILLED event faster than a REST place_order()
        # round trip completes, so this buffer's original purpose (closing
        # that ordering race) is common, not rare - 15s left less margin
        # than warranted for occasional REST latency. Purely a wider window
        # on the same existing buffer-and-replay mechanism; no new logic.
        self._UNMATCHED_FILL_TTL_SEC: float = 30.0

        # 2026-08 Smart Exit diagnostics (logging-only, added alongside the
        # Smart Exit V2 retune): captures the signal agree_count/
        # required_agree/which-signals-fired/dca_distance_pct at the exact
        # moment a smart_exit close is decided in _manage_open_position(),
        # so _on_close_filled() can attach it to the permanent trade-log
        # record below. Purely additive - does not affect any entry/exit/
        # DCA/risk decision. Cleared (consumed) on every close so it can
        # never leak into an unrelated later exit's record.
        self._last_smart_exit_diagnostics: Optional[dict] = None

        # --- Trade-log reconciliation (Binance is the source of truth) ----
        # In-memory high-water-mark of the highest Binance trade id ("t" on
        # ORDER_TRADE_UPDATE) this process has itself seen live, plus the
        # durable cursor loaded from disk/GitHub in load_trade_sync_cursor().
        # Both exist purely to make reconcile_trade_history_from_exchange()
        # idempotent - neither is read by any entry/exit/DCA/risk logic.
        self._last_live_trade_id: int = 0
        self._trade_sync_cursor: int = 0

        # --- Reconcile backoff/cooldown (2026-07 reconcile-backoff fix) ----
        # Governs ONLY how often reconcile_trade_history_from_exchange()'s
        # GET /fapi/v1/userTrades call is retried while Binance is returning
        # 502/504 or timing out - see that method and the module docstring.
        # Not read by anything else; does not affect entry/exit/DCA/risk
        # decisions or the trade-sync cursor itself.
        self._reconcile_backoff_sec: float = RECONCILE_BACKOFF_BASE_SEC
        self._reconcile_cooldown_until: float = 0.0

        # --- DCA-state peak-save throttle (2026-07 DCA-state-recovery fix) --
        # Tracks the peak_unrealized_pnl value that was last persisted via
        # save_dca_state(), so _manage_open_position()'s Profit Lock branch
        # only re-saves the snapshot when the peak has grown meaningfully
        # since then (see DCA_STATE_PEAK_SAVE_MIN_DELTA_USDT above) instead
        # of on every single tick of a long-running profitable trade. Purely
        # a persistence-throttling detail - does not affect Profit Lock's
        # own activation/ratio/close decisions in any way.
        self._last_dca_state_peak_saved: float = 0.0

        # ==================================================================
        # 2026-08 HIGH-FREQUENCY ORDERFLOW UPGRADE - manager-level state
        # ==================================================================
        # All ADDITIVE. Nothing above this block was removed or changed.

        # The bounded in-memory rolling data layer (websocket.py). Fed by
        # on_depth_update() / on_agg_trade() below, read by the entry guard,
        # the Smart Orderflow Early Exit and the Safe DCA rescue rule. Its
        # deques carry a hard maxlen, so this costs a fixed, known amount of
        # RAM for the life of the process - the Railway constraint.
        self.orderflow = OrderFlowTracker()
        # Same pure guard object EntryEngineV2 holds; used here for the
        # position-management side (DCA rescue book support) so entry and
        # management can never disagree about what "book support" means.
        self.liquidity_guard = LiquidityFlowGuard()

        # --- Dynamic post-loss cool-off window ----------------------------
        # _cool_off_until: no NEW entry may be opened before this wall-clock
        #   timestamp. Armed by _arm_cool_off() from _on_close_filled()
        #   whenever a trade closes at a fee-net loss.
        # _cool_off_guard_until: for an equally long window AFTER the hard
        #   block expires, the entry orderbook guard stays TIGHTENED (the
        #   adverse-imbalance veto trips earlier and real same-side book
        #   support is required) - this is the anti-revenge-trading half of
        #   the rule, and it is what makes the first trade after a loss
        #   materially harder to open than an ordinary one.
        # Deliberately in-memory only: a cool-off is a short-lived reaction
        # to a just-observed loss, and a process restart already re-derives
        # every risk-relevant fact from the exchange. Nothing is written to
        # the DCA-state snapshot schema, so no persisted file format changes.
        self._cool_off_until: float = 0.0
        self._cool_off_guard_until: float = 0.0
        self._cool_off_reason: str = ""
        self._last_cool_off_log_ts: float = 0.0
        self._last_continuous_trading_log_ts: float = 0.0
        self._last_orderflow_exit_log_ts: float = 0.0
        self._last_dca_orderbook_block_log_ts: float = 0.0

        # --- Post-only (maker) entry execution ----------------------------
        # Set to the orderId of an in-flight POST-ONLY (GTX) entry/DCA limit
        # order, alongside the timestamp it was submitted. A maker order can
        # legitimately rest unfilled forever if price walks away, and the
        # existing sync machinery correctly treats a resting NEW order as
        # "keep waiting" - so this upgrade owns its own timeout, cancels the
        # stale order, and lets the next tick re-decide from scratch. See
        # _post_only_entry_watchdog().
        self._post_only_order_id: Optional[int] = None
        self._post_only_submitted_ts: float = 0.0

    # ---------------------------------------------------------------------
    # 2026-08 orderflow helpers (all additive)
    # ---------------------------------------------------------------------

    def orderflow_snapshot(self) -> dict:
        """One consistent orderflow reading for this decision. Every caller
        in this file takes the snapshot ONCE per tick and passes the same
        dict around, so the entry guard, the Smart Orderflow Early Exit and
        the DCA rescue rule can never see three different books within a
        single tick."""
        return self.orderflow.snapshot()

    def in_cool_off(self, now: Optional[float] = None) -> bool:
        """True while the post-loss hard entry block is still active."""
        ref = time.time() if now is None else now
        return ref < self._cool_off_until

    def cool_off_remaining_sec(self, now: Optional[float] = None) -> float:
        ref = time.time() if now is None else now
        return max(0.0, self._cool_off_until - ref)

    def cool_off_guard_active(self, now: Optional[float] = None) -> bool:
        """True while the TIGHTENED orderbook thresholds apply. Spans both
        the hard-block window and the equally long window after it."""
        ref = time.time() if now is None else now
        return ref < self._cool_off_guard_until

    def entry_imbalance_threshold(self, now: Optional[float] = None) -> float:
        """Imbalance veto threshold for THIS decision. Tightened (lowered,
        so an adverse book trips the veto sooner) while the post-loss guard
        window is active. Floored at 0.05 so it can never collapse to a
        degenerate zero-tolerance value through configuration alone."""
        if not self.cool_off_guard_active(now):
            return ORDERBOOK_IMBALANCE_THRESHOLD
        return max(0.05, ORDERBOOK_IMBALANCE_THRESHOLD - COOL_OFF_IMBALANCE_TIGHTEN)

    def entry_support_min(self, now: Optional[float] = None) -> float:
        """Minimum same-side imbalance that counts as book support for THIS
        decision. Raised to COOL_OFF_SUPPORT_MIN while the post-loss guard
        window is active - i.e. after a loss the book must genuinely back
        the new trade, not merely fail to oppose it."""
        if not self.cool_off_guard_active(now):
            return ORDERBOOK_SUPPORT_MIN
        return max(ORDERBOOK_SUPPORT_MIN, COOL_OFF_SUPPORT_MIN)

    def _arm_cool_off(self, net_pnl: float, exit_reason: str) -> None:
        """Start the dynamic cool-off window after a losing trade.

        Called from _on_close_filled()'s finalize block only, and only for a
        genuinely negative fee-net result. Does not touch, cancel or modify
        any open position - by construction there is none at that point, the
        position has just been confirmed flat."""
        if COOL_OFF_PERIOD_MINUTES <= 0:
            return
        window = COOL_OFF_PERIOD_MINUTES * 60.0
        now = time.time()
        self._cool_off_until = now + window
        # Tightened thresholds outlast the hard block by the same duration.
        self._cool_off_guard_until = self._cool_off_until + window
        self._cool_off_reason = (
            f"loss ${net_pnl:+.4f} on exit_reason={exit_reason}"
        )
        print(color(
            f"{now_str()} [cool-off] ARMED after a losing trade ({self._cool_off_reason}): "
            f"no new entry for {COOL_OFF_PERIOD_MINUTES:.0f} min, then a further "
            f"{COOL_OFF_PERIOD_MINUTES:.0f} min with the entry orderbook guard TIGHTENED "
            f"(imbalance veto {ORDERBOOK_IMBALANCE_THRESHOLD:.2f} -> "
            f"{max(0.05, ORDERBOOK_IMBALANCE_THRESHOLD - COOL_OFF_IMBALANCE_TIGHTEN):.2f}, "
            f"required book support >= {max(ORDERBOOK_SUPPORT_MIN, COOL_OFF_SUPPORT_MIN):.2f}). "
            f"Open-position management (TP/SL/Profit-Lock/Smart-Exit/DCA) is unaffected.",
            YELLOW,
        ))

    def _should_log_cool_off(self, interval_sec: float = 60.0) -> bool:
        now = time.time()
        if now - self._last_cool_off_log_ts >= interval_sec:
            self._last_cool_off_log_ts = now
            return True
        return False

    def _should_log_continuous_trading(self, interval_sec: float = 3600.0) -> bool:
        now = time.time()
        if now - self._last_continuous_trading_log_ts >= interval_sec:
            self._last_continuous_trading_log_ts = now
            return True
        return False

    def _should_log_orderflow_exit(self, interval_sec: float = 30.0) -> bool:
        now = time.time()
        if now - self._last_orderflow_exit_log_ts >= interval_sec:
            self._last_orderflow_exit_log_ts = now
            return True
        return False

    def _should_log_dca_orderbook_block(self, interval_sec: float = 30.0) -> bool:
        now = time.time()
        if now - self._last_dca_orderbook_block_log_ts >= interval_sec:
            self._last_dca_orderbook_block_log_ts = now
            return True
        return False

    # -- Persistent Adaptive Learning: startup load / ongoing persistence ----

    async def load_or_init_brain(self) -> None:
        # 2026-08 environment + symbol state isolation: explicit,
        # unmissable log of which environment (Testnet/Live) AND symbol
        # are active and which persistence files they map to - printed
        # once at startup, before any local/remote Brain candidate is even
        # read, so an environment or symbol switch (Railway ENV
        # USE_TESTNET=.../SYMBOL=... + redeploy) is immediately verifiable
        # from the logs rather than inferred later.
        print(color(
            f"{now_str()} [symbol] environment={RUNTIME_ENV} SYMBOL={self.symbol} | "
            f"brain={BRAIN_LOCAL_PATH} | "
            f"dca={DCA_STATE_PATH} | "
            f"cursor={TRADE_SYNC_CURSOR_PATH} | "
            f"trades={TRADE_LOG_CSV_PATH}/{TRADE_LOG_JSON_PATH} | "
            f"stats={STATS_CSV_PATH}/{STATS_JSON_PATH}", MAGENTA,
        ))
        # Start (or reuse) the single shared GitHub session up front, so it's
        # available for the CSV log/stats restore that runs right after this,
        # regardless of which candidate below actually wins the selection.
        await self.github_sync.start()

        # 2026-08 Brain rollback-safety fix (this function only - Brain
        # training/scoring formulas, N_FEATURES_V2, BRAIN2_WARMUP_UPDATES,
        # and BrainV2.from_bytes()'s own compatibility validation are all
        # unchanged): previously, any readable local brain.pkl was loaded
        # unconditionally and this function RETURNED before ever checking
        # GitHub - a stale local snapshot (e.g. surviving on a persistent
        # volume across a redeploy) could silently roll the bot's learned
        # state backward by tens of thousands of updates, with no
        # comparison ever made and no log evidence of why. Now BOTH
        # candidates are read (when available) and compared by
        # BrainV2.update_count - the ONLY authoritative freshness signal
        # used (never file mtime or Git commit timestamp, per the
        # requirement that a stale snapshot's commit can postdate a
        # genuinely newer one) - and the higher one wins.
        #
        # BrainV2.from_bytes() itself is untouched and does the validation:
        # it never raises - an incompatible/corrupt snapshot silently
        # becomes a fresh BrainV2 (update_count=0), which then automatically
        # loses this comparison to any real snapshot on either side. So
        # "only successfully validated snapshots participate" falls out of
        # the existing from_bytes() behavior for free, without duplicating
        # its compatibility checks here.
        local_brain: Optional[BrainV2] = None
        local_updates: Optional[int] = None
        if os.path.exists(BRAIN_LOCAL_PATH):
            local_data: Optional[bytes] = None
            try:
                with open(BRAIN_LOCAL_PATH, "rb") as f:
                    local_data = f.read()
            except Exception as e:  # noqa: BLE001 - a local read failure must not block startup
                print(color(f"[brain] could not read local {BRAIN_LOCAL_PATH} ({e}) - treating as unavailable.", YELLOW))
            if local_data:
                local_brain = BrainV2.from_bytes(local_data, N_FEATURES_V2, BRAIN2_WARMUP_UPDATES)
                local_updates = local_brain.update_count
                print(color(f"[brain] local snapshot updates={local_updates}", GRAY))

        remote_brain: Optional[BrainV2] = None
        remote_updates: Optional[int] = None
        remote_data: Optional[bytes] = None
        try:
            remote_data = await self.github_sync.download()
        except Exception as e:  # noqa: BLE001 - a remote fetch failure must not block startup
            print(color(f"[brain] could not fetch remote GitHub snapshot ({e}) - continuing without it.", YELLOW))
            remote_data = None
        if remote_data:
            remote_brain = BrainV2.from_bytes(remote_data, N_FEATURES_V2, BRAIN2_WARMUP_UPDATES)
            remote_updates = remote_brain.update_count
            print(color(f"[brain] remote snapshot updates={remote_updates}", GRAY))

        def _cache_locally(data: bytes) -> None:
            try:
                with open(BRAIN_LOCAL_PATH, "wb") as f:
                    f.write(data)
            except Exception as e:  # noqa: BLE001 - disk write failure shouldn't block using the brain
                print(color(f"[brain] could not cache selected brain snapshot to disk: {e}", YELLOW))

        if local_brain is not None and remote_brain is not None:
            if remote_updates > local_updates:
                self.brain = remote_brain
                _cache_locally(remote_data)
                print(color(
                    f"[brain] selected REMOTE snapshot ({remote_updates} > {local_updates}); "
                    f"cached locally (ready={self.brain.is_ready()})", MAGENTA,
                ))
            else:
                self.brain = local_brain
                print(color(
                    f"[brain] selected LOCAL snapshot ({local_updates} >= {remote_updates}) "
                    f"(ready={self.brain.is_ready()})", MAGENTA,
                ))
        elif local_brain is not None:
            self.brain = local_brain
            print(color(
                f"[brain] selected LOCAL snapshot (updates={local_updates}) - no valid remote "
                f"available (ready={self.brain.is_ready()})", MAGENTA,
            ))
        elif remote_brain is not None:
            self.brain = remote_brain
            _cache_locally(remote_data)
            print(color(
                f"[brain] selected REMOTE snapshot (updates={remote_updates}) - no valid local "
                f"available; cached locally (ready={self.brain.is_ready()})", MAGENTA,
            ))
        else:
            # self.brain is already the fresh BrainV2() constructed in
            # __init__ - identical outcome to the previous code's final
            # branch, which also relied on that same pre-existing instance.
            print(color(
                "[brain] no valid local or remote snapshot found - starting a fresh (cold) Brain V2.", GRAY
            ))

        # 2026-08 Brain rollback-safety fix: baseline for persist_brain()'s
        # anti-rollback guard - the highest update_count now confirmed to
        # exist anywhere (local, remote, or 0 for a fresh brain).
        self._brain_max_known_update_count = self.brain.update_count

    async def persist_brain(self, reason: str) -> None:
        # 2026-08 Brain rollback-safety fix: cheap, local, no-extra-API-call
        # guard - if this process's own Brain has somehow fallen below the
        # highest update_count this process has itself already confirmed
        # exists (selected at startup, or from its own prior successful
        # push), refuse to push to GitHub. update_count only ever increases
        # during normal learning (learn_success()/learn_quality()), so this
        # should never fire in ordinary operation; it exists purely as a
        # defensive backstop against something unexpected reassigning
        # self.brain mid-process to a lower-update object, which would
        # otherwise silently overwrite a known-higher remote snapshot. The
        # local disk write below is NOT gated by this - it always reflects
        # this process's own honest current state, which is harmless to
        # keep locally cached regardless.
        if self.brain.update_count < self._brain_max_known_update_count:
            print(color(
                f"[brain-sync] REFUSING to push to GitHub: this process's Brain "
                f"(updates={self.brain.update_count}) is behind a snapshot already known to exist "
                f"(updates={self._brain_max_known_update_count}) - this would roll back the shared "
                f"snapshot. Saving locally only.", RED,
            ))
            try:
                data = self.brain.to_bytes()
                tmp_path = f"{BRAIN_LOCAL_PATH}.tmp"
                with open(tmp_path, "wb") as f:
                    f.write(data)
                os.replace(tmp_path, BRAIN_LOCAL_PATH)
            except Exception as e:  # noqa: BLE001
                print(color(f"[brain] failed to write {BRAIN_LOCAL_PATH} locally: {e}", RED))
            return

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
                # Advance the known baseline - we just confirmed this
                # update_count is now what GitHub holds.
                self._brain_max_known_update_count = max(
                    self._brain_max_known_update_count, self.brain.update_count
                )
                print(color(
                    f"{now_str()} [brain-sync] pushed brain snapshot to GitHub ({reason}, "
                    f"updates={self.brain.update_count})", MAGENTA,
                ))
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; upload() already catches internally
            print(color(f"[brain-sync] unexpected error during push (bot keeps trading): {e}", RED))
        self._brain_dirty = False

    # -- DCA state persistence (side/qty/avg_entry_price/dca_step/last_dca_price/
    # profit_lock_active/peak_unrealized_pnl) - local-disk snapshot only,
    # written after every entry fill (initial + each DCA add), after every
    # meaningful Profit Lock state change (activation, and subsequent
    # meaningful peak growth - see 2026-07 DCA-state-recovery fix), and
    # deleted once a position fully closes. Consumed by initialize_sync() to
    # restore dca_step/last_dca_price/profit_lock_active/peak_unrealized_pnl
    # across a reconnect/restart, but ONLY when the snapshot's own
    # side/qty/avg_entry_price still match what the exchange itself is
    # reporting - see initialize_sync() below (unchanged by this fix).
    # Deliberately does not touch any Entry/TP/DCA/Smart-Exit/Brain/Risk
    # decision logic; this is purely a state-recovery snapshot.

    def _dca_state_snapshot(self) -> dict:
        p = self.position
        invested_notional = sum(price * qty for price, qty in p.entries)
        return {
            "symbol": self.symbol,
            # 2026-08 DCA state recovery hardening: explicit status so a
            # restore can strictly validate that a snapshot claiming FLAT
            # actually has FLAT-consistent economics (qty/avg_entry/
            # dca_step/dca_history all zeroed) before trusting it, instead
            # of inferring "closed" only implicitly from a side/qty
            # mismatch against whatever the exchange currently shows.
            "status": p.status,
            "side": p.side,
            "qty": p.total_qty,
            "avg_entry_price": p.avg_entry_price,
            # Audit/debugging field only (2026-07 DCA-state-recovery fix) -
            # the price of the very first entry fill for this position, as
            # opposed to avg_entry_price which blends in every DCA add.
            # NOT used by initialize_sync()'s match/restore decision, which
            # continues to compare side/qty/avg_entry_price only.
            "initial_entry_price": p.entries[0][0] if p.entries else p.avg_entry_price,
            "dca_step": p.dca_step,
            "last_dca_price": p.last_dca_price,
            "profit_lock_active": p.profit_lock_active,
            "peak_unrealized_pnl": p.peak_unrealized_pnl,
            # Persisted so a CLOSE order's fill can still be routed to
            # _on_close_filled() after a restart/reconnect wipes the
            # in-memory _order_index - see handle_order_update()'s
            # untracked_order_id recovery path below.
            "pending_order_id": p.pending_order_id,
            "pending_role": p.pending_role,
            # 2026-08 fix B: persisted so the reconciliation entry-leg floor
            # survives a restart - without it, a process that restarts while
            # a position is open loses the only marker that keeps the entry
            # and close fills in the same userTrades window.
            "open_position_first_trade_id": self._open_position_first_trade_id,
            # 2026-08 CLOSING-resync opened_at fix: the real entry
            # timestamp, so a full process restart (where the in-memory
            # PositionState is gone entirely, unlike an in-process resync
            # which has a separate, non-persistence-dependent fix in
            # initialize_sync()) can still recover it instead of
            # initialize_sync() falling back to time.time() when
            # rebuilding - which previously corrupted holding_time_sec in
            # the trade log. Only ever read back if side/qty/avg_entry_price
            # all still match what the exchange reports, exactly like every
            # other field restored from this snapshot.
            "opened_at": p.opened_at,
            # 2026-08 DCA State Recovery V2 - all fields below are
            # audit/recovery data restored alongside the fields above (same
            # side/qty/avg_entry_price/symbol match gate in
            # initialize_sync() - unchanged), never consulted by any entry/
            # exit/DCA/risk decision:
            #   - dca_history: the full list of (price, qty) fills that
            #     built this position, so a restart doesn't just know the
            #     blended avg_entry_price but the actual fill-by-fill
            #     history that produced it.
            #   - total_invested_margin: cumulative margin committed
            #     (invested notional / leverage) across every entry/DCA fill.
            #   - current_notional: invested notional at entry prices (qty *
            #     each fill's price, summed) - an audit snapshot of size,
            #     not a live mark-to-market figure.
            #   - last_entry_order_id / last_dca_order_id: which Binance
            #     order actually filled the initial entry / most recent DCA
            #     add (see PositionState's own fields).
            #   - accumulated_close_pnl: only non-zero while a close-
            #     verification sequence (see close_position()/
            #     _on_close_filled()) is actively in progress - lets a
            #     restart mid-close-retry recover how much of this trade's
            #     PnL has already been realized across earlier legs instead
            #     of losing track of it if the process restarts between
            #     retry attempts.
            "dca_history": list(p.entries),
            "total_invested_margin": safe_div(invested_notional, self.leverage, 0.0),
            "current_notional": invested_notional,
            "last_entry_order_id": p.last_entry_order_id,
            "last_dca_order_id": p.last_dca_order_id,
            "accumulated_close_pnl": self._closing_accumulated_rp,
            # 2026-08 realized-PnL/fee-accounting fix: actual commission
            # accumulated across this trade's lifecycle so far (entry +
            # every DCA + any partial closes + any close-retry legs
            # already filled). Restored alongside dca_history/order-ids
            # under the same side/qty/avg_entry_price/symbol match gate, so
            # a restart mid-trade doesn't lose track of commission already
            # paid on earlier legs and understate this trade's true fees
            # when it eventually closes.
            "position_fees_accum": self._position_fees_accum,
            "position_fees_reliable": self._position_fees_reliable,
            # 2026-08 hard DCA safety invariant: persisted alongside every
            # other DCA-state field, restored under the exact same
            # side/qty/avg_entry_price/symbol match gate in
            # initialize_sync() - so a conservative DCA block survives a
            # restart instead of silently clearing back to "DCA allowed".
            "dca_blocked": p.dca_blocked,
            "dca_block_reason": p.dca_block_reason,
            # item 6 - exchange-native protective stop: persisted so a
            # restart can recognize an already-placed protective stop
            # (avoiding a spurious PROTECTION_PENDING/replace on a healthy
            # position) and so a genuinely PROTECTION_PENDING position
            # survives a restart still correctly blocking new DCA until
            # reconcile_protective_stop_on_startup() (dca2.py) or the next
            # entry/DCA fill resolves it. Restored under the same
            # side/qty/avg_entry_price/symbol match gate as every other
            # field here - a mismatched snapshot never carries this over.
            "protective_stop_algo_id": p.protective_stop_algo_id,
            "protective_stop_price": p.protective_stop_price,
            "protective_stop_client_algo_id": p.protective_stop_client_algo_id,
            "protective_stop_actual_order_id": p.protective_stop_actual_order_id,
            "protective_stop_cancel_pending": p.protective_stop_cancel_pending,
            "protection_pending": p.protection_pending,
            "protection_pending_reason": p.protection_pending_reason,
            "protection_pending_since": p.protection_pending_since,
        }

    def _flat_dca_state_snapshot(self) -> dict:
        """2026-08 DCA state recovery hardening: the canonical, explicit
        "no position" snapshot written when a trade fully closes (see
        save_flat_dca_state() below). Deliberately a fixed, hand-built
        structure rather than reusing _dca_state_snapshot() against a
        freshly-reset PositionState(), so every numeric field is always a
        literal 0 (not a JSON null) and status is unambiguously "FLAT" -
        matching exactly what initialize_sync()'s new strict FLAT
        validation checks for. This does not change what gets restored or
        how - it only defines what gets WRITTEN at close time."""
        return {
            "symbol": self.symbol,
            "status": "FLAT",
            "side": None,
            "qty": 0,
            "avg_entry_price": 0,
            "initial_entry_price": 0,
            "dca_step": 0,
            "last_dca_price": None,
            "profit_lock_active": False,
            "peak_unrealized_pnl": 0,
            "pending_order_id": None,
            "pending_role": None,
            "opened_at": 0,
            "dca_history": [],
            "total_invested_margin": 0,
            "current_notional": 0,
            "last_entry_order_id": None,
            "last_dca_order_id": None,
            "accumulated_close_pnl": 0,
            "position_fees_accum": 0,
            "position_fees_reliable": True,
            "dca_blocked": False,
            "dca_block_reason": None,
            "protective_stop_algo_id": None,
            "protective_stop_price": None,
            "protective_stop_client_algo_id": None,
            "protective_stop_actual_order_id": None,
            "protective_stop_cancel_pending": False,
            "protection_pending": False,
            "protection_pending_reason": None,
            "protection_pending_since": None,
        }

    async def save_dca_state(self, reason: str, payload_override: Optional[dict] = None) -> None:
        """Persists the DCA/position snapshot both locally and to GitHub.

        2026-08 DCA-state GitHub persistence fix: previously this only wrote
        DCA_STATE_PATH locally. On a Railway redeploy (or any host that
        doesn't guarantee filesystem persistence across restarts), that
        local file - and with it dca_step, last_dca_price,
        profit_lock_active, peak_unrealized_pnl, opened_at, and any pending
        close-order info - could be silently lost even though
        initialize_sync() would still correctly restore side/qty/avg_entry
        from Binance itself. The dangerous consequence: an already-DCA'd
        position could come back up reporting dca_step=0, letting the bot
        take MORE DCA steps than the position's real risk envelope
        intended. Reuses the SAME github_sync client/session/token/repo/
        branch already used for brain.pkl and the CSV/JSONL trade logs -
        no second GitHub client, no new credentials. Fail-soft: a GitHub
        push failure never blocks the (already-succeeded) local write or
        crashes the trading loop - the position keeps trading normally
        either way, exactly as every other github_sync.upload() call site
        in this file already behaves.

        2026-08 DCA state recovery hardening: payload_override lets a
        caller (see save_flat_dca_state() below) write a specific, fixed
        payload instead of the live self._dca_state_snapshot() - every
        existing call site omits this argument and is completely
        unaffected (defaults to None, same behavior as before this
        parameter existed).
        """
        payload_dict = payload_override if payload_override is not None else self._dca_state_snapshot()
        step_label = f"{self.position.dca_step}/{MAX_DCA_STEPS}" if payload_override is None else "FLAT"
        try:
            payload = json.dumps(payload_dict).encode("utf-8")
            tmp_path = f"{DCA_STATE_PATH}.tmp"
            with open(tmp_path, "wb") as f:
                f.write(payload)
            os.replace(tmp_path, DCA_STATE_PATH)  # atomic on POSIX - never a half-written file
            print(color(f"{now_str()} [dca-state] saved local snapshot step={step_label}", GRAY))
        except Exception as e:  # noqa: BLE001 - snapshot persistence must never crash the trading loop
            print(color(f"[dca-state] failed to save snapshot ({reason}): {e}", YELLOW))
            return  # don't attempt a GitHub push of a snapshot we couldn't even build/write locally

        try:
            pushed = await self.github_sync.upload(
                payload, message=f"dca-state sync: {reason} (step={step_label})",
                path=GITHUB_DCA_STATE_PATH,
            )
            if pushed:
                print(color(f"{now_str()} [dca-state] pushed snapshot to GitHub step={step_label}", MAGENTA))
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; upload() already catches internally
            print(color(f"[dca-state] unexpected error pushing snapshot to GitHub (bot keeps trading): {e}", YELLOW))

    async def save_flat_dca_state(self, reason: str) -> None:
        """2026-08 DCA state recovery hardening: writes the canonical FLAT
        snapshot to BOTH local disk and GitHub backup - called whenever a
        trade fully closes, replacing the previous delete_dca_state()-only
        call at those sites.

        Root cause this fixes: delete_dca_state() only ever removed the
        LOCAL file. It never touched the GitHub backup, so if a later
        restart's local disk was wiped (e.g. a Railway redeploy) AND the
        GitHub backup still held an older, stale snapshot from a PREVIOUS
        (already-closed) trade, load_dca_state_snapshot()'s local-then-
        GitHub fallback could resurrect that stale, closed-trade data.
        Explicitly overwriting both copies with an unambiguous "no
        position" record - rather than just deleting one of the two
        copies - closes that gap directly.
        """
        await self.save_dca_state(reason=reason, payload_override=self._flat_dca_state_snapshot())

    async def load_dca_state_snapshot(self) -> Optional[dict]:
        """Local-first, GitHub-fallback DCA-state snapshot load.

        2026-08 DCA-state GitHub persistence fix: previously local-disk-only,
        which meant initialize_sync()'s own dca_step/last_dca_price/
        profit_lock_active/peak_unrealized_pnl/opened_at restoration (the
        block below this method, in initialize_sync()) had no way to
        recover if the local file was wiped by a redeploy - even though
        save_dca_state() (above) now also pushes to GitHub. Mirrors the
        exact local-then-GitHub pattern dca2.py's own load_dca_state()
        already uses for its separate startup pre-population step. Nothing
        about WHAT is restored, or the side/qty/avg_entry_price validation
        that gates whether any of it is trusted, changed - only WHERE the
        bytes can come from.
        """
        try:
            if os.path.exists(DCA_STATE_PATH):
                with open(DCA_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data["_snapshot_source"] = "local"
                    return data
        except Exception as e:  # noqa: BLE001 - corrupt local snapshot must not block sync/GitHub fallback
            print(color(f"[dca-state] failed to read local snapshot: {e}", YELLOW))

        try:
            raw = await self.github_sync.download(path=GITHUB_DCA_STATE_PATH)
            if raw:
                print(color(f"{now_str()} [dca-state] local snapshot missing - using GitHub backup", MAGENTA))
                data = json.loads(raw.decode("utf-8"))
                data["_snapshot_source"] = "github"
                return data
        except Exception as e:  # noqa: BLE001 - restore must never block sync/startup
            print(color(f"[dca-state] failed to read GitHub snapshot: {e}", YELLOW))

        # Neither local nor GitHub had anything - do NOT silently pretend
        # this is a brand-new position. Whoever calls this (initialize_sync())
        # is responsible for the actual "not recoverable, dca_step reset to
        # 0" warning once it knows an exchange-reported position exists but
        # no snapshot could back it - see that function's own WARNING log.
        return None

    async def delete_dca_state(self, reason: str) -> None:
        try:
            if os.path.exists(DCA_STATE_PATH):
                os.remove(DCA_STATE_PATH)
                print(color(f"[dca-state] snapshot deleted ({reason}).", GRAY))
        except Exception as e:  # noqa: BLE001 - deletion failure must never crash the trading loop
            print(color(f"[dca-state] failed to delete snapshot: {e}", YELLOW))

    # -- Trade record logging (single pipeline for both the live fill path and
    # exchange-history reconciliation) - the ONLY place either path should
    # ever call trade_logger.log_trade(), so recovered trades (reconciled
    # from Binance's own trade history) are appended in exactly the same
    # JSON/CSV shape as a live-closed trade rather than through a second,
    # divergent code path. Does not compute or infer anything itself -
    # callers still build their own record dict (live close vs. reconciled
    # trades legitimately have different data available - e.g. mfe_pct/
    # entry_confidence are only ever known live); this just centralizes the
    # actual persistence call.
    def _log_completed_trade(self, record: dict) -> None:
        self.trade_logger.log_trade(record)

    # -- Trade log / analytics persistence (trades_log.jsonl, trades_log.csv, --
    # -- performance_stats.csv) --------------------------------------------
    # Reuses self.github_sync (same GitHub client/session/token/repo/branch
    # as brain.pkl) via its path= parameter - no second client is created.
    # Fail-soft throughout: any GitHub error just leaves local state as
    # the working copy and trading continues normally.

    async def restore_csv_logs_from_github(self) -> None:
        """Startup: downloads trades_log.jsonl / trades_log.csv /
        performance_stats.csv from GitHub if present, so they survive an
        ephemeral restart the same way brain.pkl does. If a local copy
        already exists (e.g. a persistent volume) it is left alone - GitHub
        is only used to rehydrate an empty/missing local file. If neither a
        local nor a remote copy exists, nothing is created here: TradeLogger /
        PerformanceStats already create the file with proper headers
        automatically on their first natural write (unchanged behavior)."""
        for local_path, remote_path, label in (
            (TRADE_LOG_CSV_PATH, GITHUB_TRADES_LOG_CSV_PATH, os.path.basename(TRADE_LOG_CSV_PATH)),
            (STATS_CSV_PATH, GITHUB_STATS_CSV_PATH, os.path.basename(STATS_CSV_PATH)),
            (TRADE_LOG_JSON_PATH, GITHUB_TRADES_LOG_JSON_PATH, os.path.basename(TRADE_LOG_JSON_PATH)),
        ):
            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                continue  # local copy already present - don't clobber it
            try:
                data = await self.github_sync.download(path=remote_path)
            except Exception as e:  # noqa: BLE001 - restore must never block startup
                print(color(f"[csv-sync] failed to check GitHub for {label}: {e}", YELLOW))
                continue
            if not data:
                continue  # nothing on GitHub yet - created fresh on first write, as before
            try:
                with open(local_path, "wb") as f:
                    f.write(data)
                print(color(f"[csv-sync] restored {label} from GitHub ({len(data)} bytes).", MAGENTA))
            except Exception as e:  # noqa: BLE001 - disk write failure shouldn't block startup
                print(color(f"[csv-sync] could not write restored {label} to disk: {e}", YELLOW))

        # TradeLogger cached its header-written flag at construction time,
        # before this restore could have written a file to disk - refresh it
        # so the next trade close appends instead of duplicating a header.
        self.trade_logger.mark_header_present()
        # Seed the dedup hashes with whatever is on disk now, so a restored-
        # but-unchanged file isn't immediately re-uploaded for no reason.
        self._last_synced_csv_hash[GITHUB_TRADES_LOG_CSV_PATH] = self._file_sha256(TRADE_LOG_CSV_PATH)
        self._last_synced_csv_hash[GITHUB_STATS_CSV_PATH] = self._file_sha256(STATS_CSV_PATH)
        self._last_synced_csv_hash[GITHUB_TRADES_LOG_JSON_PATH] = self._file_sha256(TRADE_LOG_JSON_PATH)

    # -- Restart-safe runtime accounting restoration --------------------------
    # Root cause fixed here: TradeLogger.load_all() / restore_csv_logs_from_github()
    # above already restore the PERMANENT trades_log_<ENV>_<SYMBOL>.jsonl
    # history across a restart, but MartingaleManager's own in-memory
    # RUNTIME counters (trade_count, realized_pnl_total, daily_realized_pnl)
    # always started a fresh process at 0/0.0/0.0 regardless - nothing ever
    # re-derived them from that restored history. Beyond the cosmetic
    # status-line impact (trades=0 / session_pnl=+0.0000 after a restart
    # with real history behind it), daily_realized_pnl backs
    # MAX_DAILY_LOSS_USDT (a NEW-ENTRY-only gate - see on_price_tick()), so
    # this could silently let a Railway restart/crash/redeploy bypass
    # Daily Loss Protection for the rest of that UTC day. This method
    # closes that gap. Pure bookkeeping restoration only - does not read
    # or write DCA/position state, and does not touch Partial TP in any
    # way (PARTIAL_TP_ENABLED is off in this deployment and its existing
    # code path - _apply_partial_close() - is completely untouched by
    # this fix).

    async def restore_runtime_accounting_from_history(self) -> None:
        """Rebuilds trade_count / realized_pnl_total / daily_realized_pnl
        from the already-restored trades_log JSONL (TradeLogger.load_all()
        - the existing source of truth), and sets _daily_loss_tracker_date
        to today's UTC date so the very next _maybe_reset_daily_loss_tracker()
        call does not immediately wipe the value just restored here.

        MUST run after restore_csv_logs_from_github() (so the JSONL file on
        disk actually reflects restored history, not an empty fresh file)
        and BEFORE reconcile_trade_history_from_exchange() ever gets a
        chance to run (both are satisfied by calling this directly from
        dca2.py's startup sequence, right after restore_csv_logs_from_github()
        and before load_trade_sync_cursor()/initialize_sync()). Any trade
        reconciliation goes on to recover afterward is simply ADDED on top
        of the base this method establishes (see
        reconcile_trade_history_from_exchange()) - its existing dedup
        against trades_log.jsonl via logged_binance_order_ids() already
        guarantees a trade counted here is never re-added there, so the two
        can never double-count against each other.

        Only ever reads trades_log.jsonl and mutates the three runtime
        counters above (plus _daily_loss_tracker_date) - never touches
        self.position/PositionState, Brain V2, DCA state, or any
        entry/exit/DCA/risk decision.

        Fail-soft per-row: a single corrupt/missing/None field on one
        historical JSONL line only zeroes out THAT row's contribution -
        never raises, never blocks the rest of history from being counted,
        and never blocks bot startup. This includes a JSON line that
        parsed successfully but isn't an object (null/list/bare string or
        number) and a PnL value that parses to NaN/+-Infinity - both are
        skipped exactly like any other corrupt row, never allowed to enter
        realized_pnl_total/daily_realized_pnl.
        """
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trade_count = 0
        realized_total = 0.0
        daily_total = 0.0
        skipped = 0
        try:
            records = self.trade_logger.load_all()
        except Exception as e:  # noqa: BLE001 - must never block startup
            print(color(f"[startup-accounting] failed to read trade history: {e}", YELLOW))
            records = []

        for rec in records:
            # A JSONL line can be syntactically valid JSON without being an
            # object (null, a list, a bare string/number, ...) - TradeLogger
            # .load_all() only guarantees each line parsed as JSON, not that
            # it's a dict. Guard this explicitly so rec.get(...) below can
            # never raise AttributeError and break startup.
            if not isinstance(rec, dict):
                skipped += 1
                continue
            try:
                # 2026-08 multi-symbol state isolation: same per-symbol
                # filter PerformanceStats.compute() and
                # reconcile_trade_history_from_exchange() already apply -
                # trades_log files are symbol-scoped by filename, so this
                # is a defensive extra guard, not the primary mechanism.
                if rec.get("symbol") != self.symbol:
                    continue
                raw_pnl = rec.get("net_pnl_usdt")
                pnl = float(raw_pnl) if raw_pnl is not None else 0.0
            except (TypeError, ValueError):
                # Corrupt/non-numeric historical row - fail soft: skip only
                # this row's contribution, never crash startup.
                skipped += 1
                continue
            if not math.isfinite(pnl):
                # NaN/+-Infinity parses successfully via float() but must
                # never enter realized_pnl_total/daily_realized_pnl - it
                # would corrupt every downstream comparison (including the
                # MAX_DAILY_LOSS_USDT check) for the rest of the process.
                # Fail soft exactly like any other corrupt row.
                skipped += 1
                continue
            trade_count += 1
            realized_total += pnl
            close_time = rec.get("close_time")
            if close_time and str(close_time)[:10] == today_utc:
                daily_total += pnl

        self.trade_count = trade_count
        self.realized_pnl_total = realized_total
        self._daily_loss_tracker_date = today_utc
        self.daily_realized_pnl = daily_total

        print(color(
            f"{now_str()} [startup-accounting] rebuilt runtime counters from history: "
            f"trades={trade_count} session_pnl={realized_total:+.4f} "
            f"today_pnl={daily_total:+.4f} (UTC day {today_utc}, {len(records)} row(s) read"
            f"{f', {skipped} skipped as corrupt' if skipped else ''}).", MAGENTA,
        ))
        if MAX_DAILY_LOSS_USDT > 0 and daily_total <= -MAX_DAILY_LOSS_USDT:
            print(color(
                f"{now_str()} [startup-accounting] WARNING: today's restored realized PnL "
                f"(${daily_total:+.4f}) already breaches MAX_DAILY_LOSS_USDT "
                f"(-${MAX_DAILY_LOSS_USDT:.2f}) - new entries stay blocked until the next "
                f"UTC day (existing on_price_tick() gate - unchanged).", YELLOW,
            ))
        if DAILY_PROFIT_TARGET_USDT > 0 and daily_total >= DAILY_PROFIT_TARGET_USDT:
            print(color(
                f"{now_str()} [startup-accounting] today's restored realized NET PnL "
                f"(${daily_total:+.4f}) already reached DAILY_PROFIT_TARGET_USDT "
                f"(+${DAILY_PROFIT_TARGET_USDT:.2f}) - profit is locked and new entries "
                f"stay blocked until the next UTC day.", GREEN,
            ))

    @staticmethod
    def _file_sha256(path: str) -> Optional[str]:
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:  # noqa: BLE001 - missing/unreadable file just means "nothing to sync"
            return None

    async def _sync_csv_to_github(self, local_path: str, remote_path: str, label: str) -> None:
        """Pushes local_path to remote_path (same shared GitHub client as
        brain.pkl) ONLY if its content changed since the last successful
        push - avoids uploading on every tick / every stats export when
        nothing new actually happened. Never raises."""
        if not self.github_sync.enabled:
            return
        new_hash = self._file_sha256(local_path)
        if new_hash is None:
            return  # file doesn't exist yet / unreadable - nothing to sync
        if self._last_synced_csv_hash.get(remote_path) == new_hash:
            return  # unchanged since the last successful push - skip the API call
        try:
            with open(local_path, "rb") as f:
                data = f.read()
        except Exception as e:  # noqa: BLE001
            print(color(f"[csv-sync] could not read {label} for sync: {e}", YELLOW))
            return
        try:
            pushed = await self.github_sync.upload(data, message=f"{label} sync", path=remote_path)
            if pushed:
                self._last_synced_csv_hash[remote_path] = new_hash
                print(color(f"{now_str()} [csv-sync] pushed {label} to GitHub.", MAGENTA))
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; upload() already catches internally
            print(color(f"[csv-sync] unexpected error pushing {label} (bot keeps trading): {e}", RED))

    async def sync_trade_log_to_github(self) -> None:
        await self._sync_csv_to_github(TRADE_LOG_CSV_PATH, GITHUB_TRADES_LOG_CSV_PATH, os.path.basename(TRADE_LOG_CSV_PATH))
        await self._sync_csv_to_github(TRADE_LOG_JSON_PATH, GITHUB_TRADES_LOG_JSON_PATH, os.path.basename(TRADE_LOG_JSON_PATH))

    async def sync_performance_stats_to_github(self) -> None:
        await self._sync_csv_to_github(STATS_CSV_PATH, GITHUB_STATS_CSV_PATH, os.path.basename(STATS_CSV_PATH))

    # -- Trade-log reconciliation (Binance is the source of truth) -----------
    # Root-cause fix for trades that go missing from trades_log.jsonl/csv:
    # the ONLY path that ever wrote a trade record was a live fill event on
    # the user-data websocket. Any close that happened while that stream
    # was disconnected, or while the process wasn't running at all, was
    # never seen and never logged - and initialize_sync()'s existing
    # exchange-flat-but-local-still-open branch just reset local state
    # without recording anything. This section closes that gap by treating
    # Binance's own executed-trade history as the source of truth and
    # reconciling it into the log, using a persisted per-account trade-id
    # cursor so every fill is processed exactly once. It never touches
    # PositionState, entry/exit/DCA/TP/SL decisions, Brain V2, the
    # confidence engine, or the risk engine - it only appends rows to
    # trades_log.jsonl/csv that would otherwise be missing.

    async def load_trade_sync_cursor(self) -> None:
        """Startup: restores the persisted 'last confirmed Binance trade id'
        cursor from local disk, falling back to GitHub (same shared
        github_sync session as brain.pkl / the CSV logs) - same
        local-then-GitHub pattern as restore_csv_logs_from_github(). Leaves
        the cursor at 0 ('unknown / first run') if neither is found;
        reconcile_trade_history_from_exchange() treats that as a signal to
        seed forward from *now* rather than guess at history."""
        try:
            if os.path.exists(TRADE_SYNC_CURSOR_PATH):
                with open(TRADE_SYNC_CURSOR_PATH, "r", encoding="utf-8") as f:
                    self._trade_sync_cursor = int(json.load(f).get("last_trade_id", 0) or 0)
                    return
        except Exception as e:  # noqa: BLE001 - corrupt/missing local file must not block startup
            print(color(f"[reconcile] could not read local trade-sync cursor: {e}", YELLOW))
        try:
            data = await self.github_sync.download(path=GITHUB_TRADE_SYNC_CURSOR_PATH)
            if data:
                self._trade_sync_cursor = int(json.loads(data.decode("utf-8")).get("last_trade_id", 0) or 0)
                print(color(
                    f"[reconcile] restored trade-sync cursor from GitHub "
                    f"(last_trade_id={self._trade_sync_cursor}).", MAGENTA,
                ))
        except Exception as e:  # noqa: BLE001 - restore must never block startup
            print(color(f"[reconcile] could not check GitHub for trade-sync cursor: {e}", YELLOW))

    async def _persist_trade_sync_cursor(self, trade_id: int, reason: str) -> None:
        """Writes the cursor locally (atomic replace) and pushes it via the
        same shared github_sync client used for brain.pkl / the CSV+JSONL
        logs - no second GitHub client. Fail-soft: any error here just
        means the next reconciliation pass re-checks a slightly wider
        range next time, never a crash or a blocked trading loop."""
        if trade_id <= self._trade_sync_cursor:
            return
        self._trade_sync_cursor = trade_id
        payload = json.dumps({"last_trade_id": trade_id}).encode("utf-8")
        try:
            tmp_path = f"{TRADE_SYNC_CURSOR_PATH}.tmp"
            with open(tmp_path, "wb") as f:
                f.write(payload)
            os.replace(tmp_path, TRADE_SYNC_CURSOR_PATH)
        except Exception as e:  # noqa: BLE001
            print(color(f"[reconcile] failed to write trade-sync cursor locally: {e}", YELLOW))
        try:
            await self.github_sync.upload(
                payload, message=f"trade-sync cursor: {reason} (id={trade_id})",
                path=GITHUB_TRADE_SYNC_CURSOR_PATH,
            )
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; upload() already catches internally
            print(color(f"[reconcile] unexpected error pushing trade-sync cursor: {e}", RED))

    def _position_is_live(self) -> bool:
        """True while a real position is open locally. Used by the
        orphan-close self-heal (fix D) to tell 'this lifecycle has not closed
        yet' apart from 'this lifecycle's entry leg is missing from the
        window'."""
        p = self.position
        return p.status in ("OPEN", "DCA_PENDING", "CLOSING") and p.total_qty > 0

    @staticmethod
    def _reconstruct_lifecycles(fills: List[dict]):
        """Rebuilds flat -> open -> flat position lifecycles from a
        chronologically sorted list of Binance userTrades fills.

        Extracted verbatim (2026-08 fix D) from
        reconcile_trade_history_from_exchange() so the same reconstruction
        can be run a second time over a widened window when an orphan close
        is detected. Behavior is unchanged - BUY=+qty, SELL=-qty on a running
        signed size, one-way mode only. Returns (completed_lifecycles,
        still_open_lifecycle_or_None).
        """
        lifecycles: List[dict] = []
        running = 0.0
        current: Optional[dict] = None
        eps = 1e-9
        for t in fills:
            signed_qty = float(t["qty"]) * (1.0 if t["side"] == "BUY" else -1.0)
            was_flat = abs(running) < eps
            running += signed_qty
            if was_flat and abs(running) > eps:
                current = {
                    "open_side": "LONG" if running > 0 else "SHORT",
                    "fills": [],
                    "open_time": int(t["time"]),
                }
            if current is not None:
                current["fills"].append(t)
            if not was_flat and abs(running) < eps and current is not None:
                current["close_time"] = int(t["time"])
                lifecycles.append(current)
                current = None
        return lifecycles, current

    def _open_position_reconcile_floor(self) -> Optional[int]:
        """2026-08 fix B. Lowest Binance trade id that reconciliation must
        include so the CURRENTLY-OPEN position's entry leg is always fetched
        alongside its close.

        Returns None when flat (nothing needs keeping together, so the
        normal cursor applies unchanged) or when this position's first
        trade id is genuinely unknown - in which case the caller keeps its
        previous behavior rather than guessing at a window."""
        p = self.position
        if p.status not in ("OPEN", "DCA_PENDING", "CLOSING") or p.total_qty <= 0:
            return None
        if self._open_position_first_trade_id is None:
            return None
        return max(1, int(self._open_position_first_trade_id))

    async def reconcile_trade_history_from_exchange(self, context: str = "reconcile") -> None:
        """Fetches executed fills for `self.symbol` from Binance starting
        just after the persisted cursor (or the optional explicit backfill
        id on true first run - see TRADE_RECONCILE_BACKFILL_FROM_ID),
        reconstructs any flat->open->flat position lifecycle Binance
        reports, and logs any such lifecycle that isn't already in
        trades_log.jsonl (deduped by Binance order id via
        TradeLogger.logged_binance_order_ids()). Safe to call frequently -
        it is a no-op (single cheap REST call, empty result) once caught
        up. Never raises; never touches self.position/PositionState or
        Brain training - still purely a logging/bookkeeping safety net, not
        a trading decision. 2026-08 close-fill reliability fix: now also
        updates trade_count/realized_pnl_total/daily_realized_pnl (using
        the same fee-net PnL logged to the trade record) for exactly the
        lifecycles this pass actually recovers, gated by the same dedup
        check that already prevents re-logging a trade the live path
        already processed - so this can never double-count against a
        normal close.

        2026-07 reconcile-backoff fix (this method only - see module
        docstring): guarded by self._reconcile_cooldown_until /
        self._reconcile_backoff_sec so that a Binance-side outage (502/504
        or timeout) on this specific REST call doesn't get retried on every
        single caller (startup / every user-ws reconnect / every
        position_risk_poller tick, ~10s apart) indefinitely. On entry, if
        still within a previously armed cooldown, this is a fast no-op
        (logged once). On failure, the cooldown is (re)armed with an
        exponentially increasing duration (capped). On success, the
        backoff resets to base. Nothing else about this method's
        reconciliation/dedup/logging behavior is changed."""
        if DRY_RUN or self.client is None:
            return

        now_ts = time.time()
        if now_ts < self._reconcile_cooldown_until:
            remaining = self._reconcile_cooldown_until - now_ts
            print(color(
                f"[reconcile:{context}] skipping userTrades fetch - backing off "
                f"{remaining:.0f}s more after a recent failure (avoids hammering "
                f"an unstable endpoint).", GRAY,
            ))
            return

        first_run = self._trade_sync_cursor <= 0 and self._last_live_trade_id <= 0
        from_id: Optional[int] = None
        if first_run and TRADE_RECONCILE_BACKFILL_FROM_ID:
            try:
                from_id = int(TRADE_RECONCILE_BACKFILL_FROM_ID)
            except ValueError:
                print(color(
                    f"[reconcile:{context}] TRADE_RECONCILE_BACKFILL_FROM_ID="
                    f"{TRADE_RECONCILE_BACKFILL_FROM_ID!r} is not a valid trade id - ignoring.", YELLOW,
                ))
        elif not first_run:
            # 2026-08 reconciliation entry-leg fix (fix B - the reason the
            # LIVE 18:07:48 close could never be recovered by the safety
            # net, and why the cursor wedged at close_trade_id - 1).
            #
            # `_last_live_trade_id` tracks the newest fill this process
            # handled on the websocket. For an OPEN position that is the
            # ENTRY fill - so starting the fetch just after it meant the
            # next pass saw only the eventual CLOSE fill, with no matching
            # entry. The lifecycle reconstruction below then read that lone
            # BUY as a brand-new LONG *opening*, found it never closed
            # inside the window, skipped it, and pinned cursor_cap one
            # below it. Every subsequent pass repeated that forever: the
            # trade was invisible and the cursor never advanced again.
            #
            # Fix: while a position is open locally, never start the window
            # after its own entry fills. Rewind to the oldest trade id that
            # belongs to the currently-open position so the entry and the
            # close are always fetched together and the lifecycle closes
            # properly. Falls back to the previous behavior whenever we are
            # flat (nothing to keep together) or no entry id is known.
            from_id = max(self._trade_sync_cursor, self._last_live_trade_id) + 1
            open_leg_from_id = self._open_position_reconcile_floor()
            if open_leg_from_id is not None and open_leg_from_id < from_id:
                print(color(
                    f"[reconcile:{context}] position is open locally - rewinding userTrades "
                    f"window from id>{from_id - 1} back to id>={open_leg_from_id} so this "
                    f"position's entry leg is fetched together with its close "
                    f"(prevents an orphan close fill being misread as a new opening).", GRAY,
                ))
                from_id = open_leg_from_id

        try:
            fills = await self.client.get_user_trades(self.symbol, from_id=from_id, limit=1000)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            # Arm/extend the cooldown so this REST call backs off instead of
            # being retried again in ~10s by the next poller tick / reconnect
            # / startup - the actual root cause of the repeated 502/504 spam.
            self._reconcile_cooldown_until = time.time() + self._reconcile_backoff_sec
            print(color(f"[reconcile:{context}] could not fetch Binance trade history "
                        f"(continuing without it): {e} - backing off for "
                        f"{self._reconcile_backoff_sec:.0f}s.", YELLOW))
            self._reconcile_backoff_sec = min(
                self._reconcile_backoff_sec * 2, RECONCILE_BACKOFF_MAX_SEC
            )
            return
        except Exception as e:  # noqa: BLE001 - reconciliation must never take the bot down
            print(color(f"[reconcile:{context}] unexpected error fetching trade history: {e}", RED))
            return

        # Successful fetch (even if empty) - the endpoint is healthy again,
        # so reset the backoff back to its base value for next time.
        self._reconcile_backoff_sec = RECONCILE_BACKOFF_BASE_SEC
        self._reconcile_cooldown_until = 0.0

        if not fills:
            if first_run:
                # No cursor anywhere and no explicit backfill id: seed the
                # cursor at the current latest trade so future gaps (from
                # now on) are caught, without guessing at old history.
                try:
                    latest = await self.client.get_user_trades(self.symbol, limit=1)
                    if latest:
                        await self._persist_trade_sync_cursor(
                            int(latest[-1]["id"]), reason="seed cursor (no prior state found)"
                        )
                except Exception as e:  # noqa: BLE001
                    print(color(f"[reconcile:{context}] could not seed initial cursor: {e}", YELLOW))
            return

        fills = sorted(fills, key=lambda t: int(t.get("id", 0)))

        # Reconstruct each flat -> open -> flat position lifecycle from the
        # running signed position size (BUY=+qty, SELL=-qty; this bot only
        # ever runs in one-way mode - see close_position()/_place_step_order(),
        # which always use plain BUY/SELL with no positionSide). A lifecycle
        # still open at the end of the fetched window is the CURRENT live
        # position and is skipped - it hasn't closed yet.
        lifecycles, current = self._reconstruct_lifecycles(fills)

        # 2026-08 orphan-close self-heal (fix D - unwedges a cursor that is
        # already stuck, and stops any future wedge from becoming permanent).
        #
        # An unclosed lifecycle at the end of the window normally means "this
        # is the live position". But if the bot is FLAT locally, that reading
        # is impossible: what we are actually looking at is a CLOSE fill whose
        # matching ENTRY sits BEFORE the window, so the reconstruction misread
        # a closing BUY as an opening LONG (or a closing SELL as an opening
        # SHORT). That is precisely the LIVE 18:07:48 state - and because
        # cursor_cap then pins the cursor one below that orphan fill, every
        # later pass re-fetched the same fill, made the same misreading, and
        # never advanced again. The trade was unrecoverable and the whole
        # trade-log safety net was jammed behind it.
        #
        # Recovery: re-fetch by TIME (userTrades accepts startTime; a bounded
        # look-back well inside Binance's 7-day limit) so the missing entry
        # leg is pulled in, then reconstruct again over the merged set. Tried
        # at most once per distinct orphan id, so a genuinely unmatched fill
        # degrades to the old skip-and-hold behavior instead of re-fetching
        # forever.
        if current is not None and not self._position_is_live():
            orphan_first_id = min(int(t["id"]) for t in current["fills"])
            orphan_first_time = min(int(t["time"]) for t in current["fills"])
            if self._orphan_rewind_attempted_id != orphan_first_id:
                self._orphan_rewind_attempted_id = orphan_first_id
                start_ms = max(0, orphan_first_time - ORPHAN_REWIND_LOOKBACK_MS)
                print(color(
                    f"[reconcile:{context}] ORPHAN CLOSE DETECTED - trade id {orphan_first_id} "
                    f"opens a lifecycle that never closes, but the bot is FLAT. Its entry leg "
                    f"must predate this window; re-fetching userTrades from "
                    f"{datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc):%Y-%m-%d %H:%M:%S} UTC "
                    f"to recover the complete trade.", YELLOW,
                ))
                try:
                    backfill = await self.client.get_user_trades(
                        self.symbol, start_time_ms=start_ms, limit=1000
                    )
                except Exception as e:  # noqa: BLE001 - recovery is best-effort
                    print(color(
                        f"[reconcile:{context}] orphan-close backfill fetch failed ({e}) - "
                        f"leaving the cursor where it is and retrying on a later pass.", YELLOW,
                    ))
                    backfill = None
                if backfill:
                    merged = {int(t["id"]): t for t in backfill}
                    merged.update({int(t["id"]): t for t in fills})
                    fills = [merged[k] for k in sorted(merged)]
                    lifecycles, current = self._reconstruct_lifecycles(fills)
                    print(color(
                        f"[reconcile:{context}] orphan-close backfill merged "
                        f"{len(backfill)} historical fill(s) - reconstruction now yields "
                        f"{len(lifecycles)} complete lifecycle(s)"
                        f"{' (orphan resolved)' if current is None else ' (still unmatched)'}.",
                        GREEN if lifecycles else YELLOW,
                    ))

        max_id_seen = max(int(t["id"]) for t in fills)
        cursor_cap = max_id_seen

        if current is not None:
            # Position still open at the end of this fetch window: do not
            # advance the cursor past its entry fill(s). Advancing here would
            # mean the next reconciliation pass fetches only the eventual
            # close fill with no matching entry, reconstructs it as an
            # unclosed lifecycle, and silently drops it forever.
            cursor_cap = min(int(t["id"]) for t in current["fills"]) - 1

        recorded = 0
        if lifecycles:
            try:
                already_order_ids = self.trade_logger.logged_binance_order_ids()
            except Exception as e:  # noqa: BLE001
                print(color(f"[reconcile:{context}] failed to read existing trade log for dedup: {e}", YELLOW))
                already_order_ids = set()

            for lc in lifecycles:
                # 2026-07 session-start filter: a lifecycle that closed
                # before the operator-configured SESSION_START_DATE is old
                # Binance history from before this session and must never
                # be written into trades_log.csv / trades_log.jsonl /
                # performance_stats.csv. The trade-sync cursor still
                # advances past it further below (cursor_cap is computed
                # from max_id_seen over ALL fetched fills, unconditionally)
                # so this same old trade is not re-evaluated on every
                # future reconciliation pass - it is simply never logged.
                if lc["close_time"] < SESSION_START_MS:
                    continue
                order_ids = {int(t["orderId"]) for t in lc["fills"]}
                if order_ids & already_order_ids:
                    continue  # at least one fill already logged by the live path - skip, avoid a duplicate
                entry_fills = [t for t in lc["fills"] if (t["side"] == "BUY") == (lc["open_side"] == "LONG")]
                exit_fills = [t for t in lc["fills"] if t not in entry_fills]
                if not exit_fills:
                    continue  # defensive: running==0 implies a close happened, but be safe
                entry_notional = sum(float(t["qty"]) * float(t["price"]) for t in entry_fills)
                entry_qty = sum(float(t["qty"]) for t in entry_fills)
                exit_qty = sum(float(t["qty"]) for t in exit_fills)
                fees = sum(float(t.get("commission", 0.0)) for t in lc["fills"])
                # 2026-08 realized-PnL/fee-accounting fix: this was
                # previously treated as the final "net" figure and used
                # directly as net_pnl_usdt below. Binance's realizedPnl
                # excludes commission (same semantics as "rp" on the
                # websocket live path - see _on_close_filled()) - this is
                # the RAW (pre-fee) realized PnL, already correctly summed
                # across every fill in the lifecycle (entries + exits), so
                # no data-sourcing change was needed here, only the
                # downstream field semantics below.
                raw_realized_pnl = sum(float(t.get("realizedPnl", 0.0)) for t in lc["fills"])
                net_pnl = raw_realized_pnl - fees  # THE FIX: genuinely fee-net now
                avg_entry = safe_div(entry_notional, entry_qty, 0.0)
                avg_exit = safe_div(sum(float(t["qty"]) * float(t["price"]) for t in exit_fills), exit_qty, 0.0)
                close_dt = datetime.fromtimestamp(lc["close_time"] / 1000, tz=timezone.utc)
                # exit_order_id: the orderId of the fill that actually closed the
                # lifecycle (chronologically last exit fill) - exchange data, when
                # available, same as the live-close path's exit_order_id.
                exit_order_id = int(exit_fills[-1]["orderId"]) if exit_fills[-1].get("orderId") is not None else None

                # 2026-08 close-fill reliability fix: if this bot is
                # CURRENTLY still tracking one of this lifecycle's own
                # order_ids as its own pending close (self.position.
                # pending_order_id), the real exit reason this process
                # itself decided on (self._pending_exit_reason, e.g.
                # "max_hold_time") can be safely recovered instead of the
                # generic "reconciled_from_exchange" tag. This is narrowly
                # scoped and safe: it only ever matches the single specific
                # order THIS process itself just placed and is still
                # waiting on - never a guess applied to unrelated/older
                # history. If it doesn't match, exit_reason stays exactly
                # "reconciled_from_exchange", unchanged from before.
                recovered_exit_reason = "reconciled_from_exchange"
                try:
                    if self.position.pending_order_id is not None and int(self.position.pending_order_id) in order_ids:
                        recovered_exit_reason = getattr(self, "_pending_exit_reason", None) or "reconciled_from_exchange"
                except (TypeError, ValueError):
                    pass

                record = {
                    "close_time": close_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "symbol": self.symbol,
                    "side": lc["open_side"],
                    "entry_price": avg_entry or None,
                    "exit_price": avg_exit or None,
                    "qty": exit_qty or entry_qty,
                    "invested_notional": entry_notional,
                    "gross_pnl_usdt": raw_realized_pnl,
                    "fees_usdt": fees,
                    "net_pnl_usdt": net_pnl,
                    "net_pnl_pct": safe_div(net_pnl, entry_notional, 0.0),
                    "dca_count": max(len(entry_fills) - 1, 0),
                    "holding_time_sec": max((lc["close_time"] - lc["open_time"]) / 1000.0, 0.0),
                    "mfe_pct": None,
                    "mae_pct": None,
                    "exit_reason": recovered_exit_reason,
                    "tp_hit": recovered_exit_reason == "take_profit",
                    "smart_exit": recovered_exit_reason == "smart_exit",
                    "manual_exit": recovered_exit_reason == "manual",
                    "hard_stop": recovered_exit_reason in ("hard_stop", "max_dca_exhausted"),
                    "entry_regime": None,
                    "exit_regime": None,
                    "entry_confidence": None,
                    "entry_risk_score": None,
                    "entry_success_prob": None,
                    "entry_tp_hit_prob": None,
                    "reward": None,
                    "final_outcome": "win" if net_pnl > 0 else "loss",
                    "exit_order_id": exit_order_id,
                    "binance_order_ids": sorted(order_ids),
                    "recovered": True,
                }
                # Same logging pipeline as a live-closed trade (see
                # _log_completed_trade docstring) - keeps recovered trades in
                # the exact same JSON/CSV shape as normal closes.
                self._log_completed_trade(record)
                # 2026-08 close-fill reliability fix: previously this
                # function only wrote to the trade log/stats files -
                # self.trade_count/realized_pnl_total/daily_realized_pnl
                # (the in-memory counters behind runtime status output and
                # the MAX_DAILY_LOSS_USDT protection) were never touched,
                # so a reconciled trade could be correctly logged to CSV
                # while runtime status still showed trades=0/session_pnl=
                # +0.0000 - exactly the gap this closes. Uses the same
                # fee-net net_pnl already computed for this lifecycle
                # above. Gated by the SAME dedup check already guarding
                # this loop iteration (order_ids & already_order_ids,
                # checked before this point) - a trade the live path has
                # already processed never reaches here, so this can never
                # double-count against a normal _on_close_filled() finalize.
                # Deliberately does NOT call brain.learn_success()/
                # learn_quality() - no entry_features exist for a
                # reconciled trade to train on, and none are fabricated;
                # this remains purely a logging/bookkeeping safety net.
                self.trade_count += 1
                self._maybe_reset_daily_loss_tracker()
                self.realized_pnl_total += net_pnl
                self.daily_realized_pnl += net_pnl
                # DIAGNOSTIC (no effect on `record` or on what gets logged -
                # this runs after log_trade() and only prints): for
                # correlating against [fill-trace] lines from
                # handle_order_update() above, to see whether a given
                # order_id ever appeared there as untracked_order_id
                # (never reached _on_close_filled()) versus never appearing
                # there at all (missed entirely while the user-data
                # websocket was disconnected/reconnecting - Binance does
                # not replay missed stream messages, so that gap is
                # expected and this reconciliation pass is the only way to
                # ever recover it).
                exit_trade_ids = sorted(int(t["id"]) for t in exit_fills if "id" in t)
                print(color(
                    f"{now_str()} [reconcile-trace] path=reconciliation({context}) "
                    f"order_id={sorted(order_ids)} trade_id={exit_trade_ids} "
                    f"close_time={record['close_time']} exit_reason={recovered_exit_reason} "
                    f"reason=not_found_in_local_trade_log (never seen by _on_close_filled(); "
                    f"recovered via REST /fapi/v1/userTrades) "
                    f"session_total={self.realized_pnl_total:+.4f}", MAGENTA,
                ))
                recorded += 1

        await self._persist_trade_sync_cursor(cursor_cap, reason=f"{context} (+{recorded} recovered)")

        if recorded:
            print(color(
                f"{now_str()} [reconcile:{context}] recovered {recorded} trade(s) that Binance shows "
                f"closed but were missing from local logs.", MAGENTA,
            ))
            asyncio.create_task(self.sync_trade_log_to_github())
            try:
                self.perf_stats.export()
            except Exception as e:  # noqa: BLE001
                print(color(f"[reconcile:{context}] failed to refresh performance stats after recovery: {e}", YELLOW))

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
        # The initial entry (step 0) ALWAYS uses INITIAL_ENTRY_USDT exactly
        # as configured - never scaled by confidence/risk/regime. This is a
        # deliberate guarantee: notional_for_step(0, ...) * leverage must
        # always equal INITIAL_ENTRY_USDT * LEVERAGE (e.g. $1.5 * 40 = $60),
        # regardless of what size_mult a caller passes in. Callers on the
        # entry path enforce this by always passing size_mult=1.0 for step 0
        # (see on_price_tick); this check is a second, structural guarantee
        # against that ever regressing.
        #
        # Confidence/risk/regime-based dynamic sizing only ever applies to
        # DCA additions placed AFTER the position is already open (step > 0)
        # - the martingale 2x-per-step base is still purely deterministic,
        # just scaled up/down within [SIZE_MIN_MULT, SIZE_MAX_MULT] by how
        # the Brain currently reads the trade it's already in.
        if step > 0:
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

    def estimate_net_pnl_usdt_executable(
        self, extra_qty: float = 0.0, extra_entry_price: Optional[float] = None,
    ) -> float:
        """Conservative fee-net PnL estimate used ONLY by the per-trade
        net-loss budget (item 5), the DCA loss-budget gate (item 7), and
        the protective-stop price calculation (item 6) - deliberately
        separate from estimate_net_pnl_usdt() above (used by Profit Lock /
        max-hold diagnostics / the max-dca-exhausted review) so this
        change cannot alter any of THEIR existing behavior.

        Differs from estimate_net_pnl_usdt() in two ways the loss-budget
        gate specifically requires: (1) uses the EXECUTABLE closing-side
        price - best_bid for closing a LONG, best_ask for closing a SHORT -
        instead of the mid/mark price, since that is the price a real
        reduceOnly MARKET close would actually fill near; (2) uses the
        ACTUAL accumulated commission (_position_fees_accum) when reliable,
        falling back to a rate-based estimate only when it isn't, instead
        of always estimating both legs from TAKER_FEE_RATE.

        `extra_qty`/`extra_entry_price` let the DCA budget gate project the
        PnL of the position AS IT WOULD BE immediately after a prospective
        DCA add fills, without first submitting or mutating anything -
        pass the candidate DCA's qty/price to preview that state; leave at
        the defaults (0.0/None) to evaluate the position exactly as it is
        now.
        """
        p = self.position
        if not p.avg_entry_price or p.total_qty <= 0:
            return 0.0

        total_qty = p.total_qty + max(extra_qty, 0.0)
        if extra_qty > 0 and extra_entry_price:
            total_notional = p.avg_entry_price * p.total_qty + extra_entry_price * extra_qty
            avg_entry = total_notional / total_qty
        else:
            avg_entry = p.avg_entry_price

        close_price = self.best_bid_price if p.side == "LONG" else self.best_ask_price
        if not close_price:
            close_price = self.current_price or avg_entry

        if p.side == "LONG":
            gross = (close_price - avg_entry) * total_qty
        else:
            gross = (avg_entry - close_price) * total_qty

        if extra_qty <= 0 and self._position_fees_reliable and self._position_fees_accum > 0:
            entry_fees = self._position_fees_accum
        else:
            # Either projecting a not-yet-placed DCA add (no actual
            # commission for it exists yet) or actual fees aren't reliable
            # for this position - estimate the WHOLE entry-side leg
            # (existing fills + the prospective add, if any) from the
            # configured taker rate, consistent with
            # estimate_round_trip_fee_usdt()'s own formula.
            base_fees = (
                self._position_fees_accum
                if (extra_qty <= 0 and self._position_fees_reliable and self._position_fees_accum > 0)
                else TAKER_FEE_RATE * (p.total_qty * p.avg_entry_price)
            )
            extra_fee = TAKER_FEE_RATE * (extra_qty * extra_entry_price) if (extra_qty > 0 and extra_entry_price) else 0.0
            entry_fees = base_fees + extra_fee

        est_close_fee = TAKER_FEE_RATE * (total_qty * close_price)
        return gross - entry_fees - est_close_fee

    # -- tick plumbing -----------------------------------------------------------

    # ---------------------------------------------------------------------
    # 2026-08 strict 1:2 risk-to-reward envelope
    # ---------------------------------------------------------------------
    # Sized for the documented account shape - $4 initial margin @ 20x
    # leverage on a ~$20 wallet, i.e. ~$80 notional per entry - where a
    # $0.20 fee-net stop and a $0.40 fee-net target are exactly 1:2.
    #
    # These are USD (fee-net) envelopes evaluated on the WHOLE position, so
    # they stay correct after a DCA rescue changes the quantity - they are
    # not percentage rules that silently mean different dollar amounts at
    # different sizes.
    #
    # IMPORTANT - what was NOT changed: the pre-existing per-trade budget
    # (MAX_TRADE_NET_LOSS_USDT, default $0.20) and the exchange-native
    # protective STOP_MARKET derived from it (_compute_protective_stop_price)
    # are untouched and still armed exactly as before. At their default
    # values the two agree to the cent; these helpers add an independent
    # client-side envelope on top rather than replacing that machinery, so
    # nothing that already protected a position was weakened or removed.

    def rr_stop_loss_usd(self) -> float:
        """Hard fee-net loss ceiling for one trade ($0.20 by default)."""
        return MAX_STOP_LOSS_USD

    def rr_target_profit_usd(self) -> float:
        """Fee-net profit target for one trade ($0.40 by default)."""
        return TARGET_PROFIT_USD

    def rr_enforcement_active(self) -> bool:
        """The RR envelope is inert unless explicitly enabled AND the
        pre-existing per-trade loss budget is itself enabled. Deployments
        (and focused tests) that disable the budget with
        MAX_TRADE_NET_LOSS_USDT=0 therefore keep exactly the behavior they
        had before this upgrade - one switch still disables both."""
        return (
            ENFORCE_RISK_REWARD_USD
            and MAX_TRADE_NET_LOSS_USDT > 0
            and MAX_STOP_LOSS_USD > 0
            and TARGET_PROFIT_USD > 0
        )

    def rr_ratio(self) -> float:
        """Realized reward:risk of the configured envelope, for logging."""
        return safe_div(TARGET_PROFIT_USD, MAX_STOP_LOSS_USD, 0.0)

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
    
    def on_account_update(self, event: dict) -> None:
        """2026-08 HTTP 429 REST rate-limit fix - websocket-first state
        tracking. Handles the user-data stream's ACCOUNT_UPDATE event.

        Binance pushes ACCOUNT_UPDATE on every balance/position change, which
        makes it a strictly fresher (and free) source for the two things the
        REST pollers were hammering the API for:

          - the USDT wallet balance, which used to require a
            GET /fapi/v2/balance every BALANCE_REFRESH_SEC;
          - whether this symbol has an open position at all, which decides
            whether the positionRisk poller needs its active cadence or can
            sit at the slow idle one.

        The balance assignment itself is byte-for-byte the behavior the
        websocket handler already had (cross-wallet balance "cw", falling
        back to wallet balance "wb"); everything else here only RECORDS what
        the stream said plus a timestamp. Deliberately does NOT rebuild
        manager.position, place/cancel anything, or feed any entry/exit/DCA
        decision - initialize_sync() and the REST reconciliation paths stay
        the sole authority on real position state, unchanged.

        Never raises: a malformed event is ignored, because one bad frame
        must not take down the user-data socket.
        """
        try:
            payload = event.get("a", {}) or {}
        except AttributeError:
            return

        now = time.time()
        self.last_account_update_ts = now

        for b in payload.get("B", []) or []:
            try:
                if b.get("a") == "USDT":
                    self.available_balance = float(b.get("cw") or b.get("wb") or 0)
            except (TypeError, ValueError, AttributeError):
                continue

        for pos in payload.get("P", []) or []:
            try:
                if pos.get("s") != self.symbol:
                    continue
                self.ws_position_amt = float(pos.get("pa") or 0)
                self.ws_position_ts = now
            except (TypeError, ValueError, AttributeError):
                continue

    def has_ws_position_hint(self, max_age_sec: float) -> Optional[bool]:
        """True/False if the user-data stream told us within the last
        `max_age_sec` whether this symbol has a non-zero position; None when
        the stream has said nothing recent enough to rely on. Callers MUST
        treat None as 'unknown' and fall back to their own state - this is a
        polling-cadence hint only, never a trading signal."""
        if self.ws_position_amt is None or not self.ws_position_ts:
            return None
        if time.time() - self.ws_position_ts > max_age_sec:
            return None
        return self.ws_position_amt != 0.0

    def on_agg_trade(self, qty: float, is_buyer_maker: bool) -> None:
        self.candles.on_trade(qty, is_buyer_maker)
        # 2026-08 high-frequency orderflow upgrade (this line only - the
        # pre-existing CandleAggregator ingestion above is untouched and
        # still feeds the candle/volume-delta features exactly as before):
        # the same aggregated trade also feeds the rolling 10s trade-volume
        # delta used by the entry Liquidity & Flow Guard. Both consumers use
        # the identical isBuyerMaker convention, so they can never disagree
        # about which side was the aggressor.
        self.orderflow.on_agg_trade(qty, is_buyer_maker)

    def on_depth_update(self, bids, asks) -> None:
        """Ingest one @depth<N>@100ms partial-book frame from the market
        websocket (websocket.py). Deliberately NOT async and deliberately
        does NOT drive on_price_tick(): depth arrives ~10x/second and must
        never become the trading decision clock - bookTicker remains the
        sole tick driver, exactly as before this upgrade. Wrapped so a
        malformed frame can never propagate an exception back into the
        socket read loop."""
        try:
            self.orderflow.on_depth(bids, asks)
        except Exception as e:  # noqa: BLE001 - a bad depth frame must never kill the feed
            print(color(f"{now_str()} [orderflow] depth frame skipped: {e}", YELLOW))

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

    def _adaptive_scale_factor(self) -> float:
        """Percentage Adaptive TP/DCA System (NEW): a bounded multiplier
        applied on top of the existing ATR-based dynamic TP/DCA numbers
        below, based on:
          - position size/notional/margin already committed (bigger
            accumulated notional - i.e. deeper DCA - shrinks the multiplier
            toward ADAPTIVE_SCALE_MIN, so a large book targets a smaller
            percentage move to secure profit faster / tighten DCA spacing;
            a small/fresh position stays close to 1.0)
          - current market regime (trending regimes nudge the multiplier up
            toward ADAPTIVE_SCALE_MAX so winners/DCA can run a bit wider;
            SIDEWAYS nudges it down toward tighter targets)
        Always clamped to [ADAPTIVE_SCALE_MIN, ADAPTIVE_SCALE_MAX] - the
        callers below additionally clamp the final TP/DCA pct to their own
        existing absolute safety bounds, so this can only move the result
        within those established ceilings/floors, never past them."""
        if not ADAPTIVE_SIZING_ENABLED:
            return 1.0

        scale = 1.0
        p = self.position
        if p.total_qty and p.avg_entry_price:
            current_notional = p.total_qty * p.avg_entry_price
            baseline_notional = INITIAL_ENTRY_USDT * self.leverage
            if baseline_notional > 0:
                depth_ratio = max(0.0, (current_notional / baseline_notional) - 1.0)
                scale *= 1.0 / (1.0 + ADAPTIVE_SIZE_SENSITIVITY * depth_ratio)

        if self.last_regime.regime in (REGIME_STRONG_TREND, REGIME_WEAK_TREND):
            scale *= 1.05
        elif self.last_regime.regime == REGIME_SIDEWAYS:
            scale *= 0.90

        return clamp(scale, ADAPTIVE_SCALE_MIN, ADAPTIVE_SCALE_MAX)

    def get_dynamic_take_profit_pct(self) -> float:
        if not DYNAMIC_TP_ENABLED:
            return TAKE_PROFIT_PCT
        candles = self.candles.closed_candles()
        if len(candles) < 5:
            return TAKE_PROFIT_PCT
        vol = self.last_regime.atr_pct if self.last_regime.atr_pct else compute_atr_pct(candles)
        if vol <= TP_VOL_LOW:
            base_tp = TAKE_PROFIT_PCT
        elif vol >= TP_VOL_HIGH:
            base_tp = TAKE_PROFIT_MAX_PCT
        else:
            vol_range = TP_VOL_HIGH - TP_VOL_LOW
            ratio = (vol - TP_VOL_LOW) / vol_range if vol_range > 0 else 0.0
            base_tp = TAKE_PROFIT_PCT + ratio * (TAKE_PROFIT_MAX_PCT - TAKE_PROFIT_PCT)

        # Percentage Adaptive TP System: scale the vol-based base_tp by
        # position size/margin + regime, then re-clamp to
        # [TAKE_PROFIT_PCT * ADAPTIVE_TP_MIN_RATIO, TAKE_PROFIT_MAX_PCT *
        # ADAPTIVE_TP_MAX_RATIO] - both ratios default to 1.0, so with no
        # env overrides this clamp is identical to the existing
        # [TAKE_PROFIT_PCT, TAKE_PROFIT_MAX_PCT] band and default behavior
        # for a fresh (step-0) position in a non-trending/non-sideways
        # regime is unchanged (scale == 1.0).
        adaptive_tp = base_tp * self._adaptive_scale_factor()
        return clamp(
            adaptive_tp,
            TAKE_PROFIT_PCT * ADAPTIVE_TP_MIN_RATIO,
            TAKE_PROFIT_MAX_PCT * ADAPTIVE_TP_MAX_RATIO,
        )

    def get_dynamic_dca_distance_pct(self) -> float:
        """ATR-adaptive DCA spacing: distance scales with current ATR% so
        DCA adds happen further apart in a volatile market (avoiding
        rapid-fire DCA into noise) and closer together in a quiet one,
        always bounded to [DCA_MIN_DISTANCE_PCT, DCA_MAX_DISTANCE_PCT] and
        never below the original static DCA_TRIGGER_PCT floor. Also scaled
        by the same Percentage Adaptive System factor as dynamic TP (size/
        margin + regime), still within the same existing absolute bounds."""
        atr_pct = self.last_regime.atr_pct
        if atr_pct <= 0:
            dynamic = DCA_TRIGGER_PCT
        else:
            dynamic = clamp(atr_pct * DCA_ATR_MULTIPLIER, DCA_MIN_DISTANCE_PCT, DCA_MAX_DISTANCE_PCT)

        adaptive = dynamic * self._adaptive_scale_factor()
        adaptive = clamp(adaptive, DCA_MIN_DISTANCE_PCT, DCA_MAX_DISTANCE_PCT)
        return max(adaptive, DCA_TRIGGER_PCT)

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

    def _should_log_warmup_skip(self, interval_sec: float = 30.0) -> bool:
        now = time.time()
        if now - self._last_warmup_skip_log_ts >= interval_sec:
            self._last_warmup_skip_log_ts = now
            return True
        return False

    def _maybe_reset_daily_loss_tracker(self) -> None:
        """2026-08 Daily Loss Protection: resets daily_realized_pnl to 0.0
        whenever the UTC calendar day has changed since the last time this
        ran. Called from the entry-gate check below and from both realized-
        PnL accumulation points (_apply_partial_close, _on_close_filled) so
        the tracker is always current whichever fires first. Pure
        bookkeeping - never touches position/order state."""
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_loss_tracker_date != today_utc:
            if self._daily_loss_tracker_date is not None:
                print(color(
                    f"{now_str()} [daily-loss] new UTC day ({today_utc}) - resetting daily "
                    f"realized PnL tracker (was ${self.daily_realized_pnl:+.4f})", GRAY,
                ))
            self._daily_loss_tracker_date = today_utc
            self.daily_realized_pnl = 0.0

    def _should_log_daily_loss_block(self, interval_sec: float = 60.0) -> bool:
        """Throttle for the 'entries halted - daily loss limit reached'
        diagnostic line - purely for visibility, does not affect whether
        entries are actually blocked."""
        now = time.time()
        if now - self._last_daily_loss_block_log_ts >= interval_sec:
            self._last_daily_loss_block_log_ts = now
            return True
        return False

    def _should_log_daily_profit_block(self, interval_sec: float = 60.0) -> bool:
        """Throttle for the fee-net daily-profit entry lock diagnostic.
        Logging only; the boundary check itself runs on every FLAT tick."""
        now = time.time()
        if now - self._last_daily_profit_block_log_ts >= interval_sec:
            self._last_daily_profit_block_log_ts = now
            return True
        return False

    def _should_log_max_hold_review(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the Max Hold Time V2 'kept alive after emergency
        review' diagnostic line - debugging only, does not affect whether
        the position is actually deferred or closed."""
        now = time.time()
        if now - self._last_max_hold_review_log_ts >= interval_sec:
            self._last_max_hold_review_log_ts = now
            return True
        return False

    def _should_log_max_hold_fee_net_review(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the [max-hold-review] fee-net meaningful_loss
        diagnostic line (2026-08 fee-net meaningful_loss fix) - debugging
        only, does not affect meaningful_loss/still_deferring/any decision.
        Only throttles repeated DEFER ticks; the CLOSE decision (a one-time
        terminal event per position) always logs regardless."""
        now = time.time()
        if now - self._last_max_hold_fee_net_review_log_ts >= interval_sec:
            self._last_max_hold_fee_net_review_log_ts = now
            return True
        return False

    def _should_log_max_dca_exhausted_review(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the [max-dca-exhausted-review] diagnostic line
        (2026-08 max_dca_exhausted exposure-cap review fix) - debugging
        only, does not affect the exposure-cap decision itself. It keeps
        repeated HOLD ticks readable while normal exit logic remains active."""
        now = time.time()
        if now - self._last_max_dca_exhausted_review_log_ts >= interval_sec:
            self._last_max_dca_exhausted_review_log_ts = now
            return True
        return False

    def _should_log_invalid_open_state(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the [invalid-open-state] safety-gate diagnostic
        line (2026-08 invalid-OPEN-state safety gate) - debugging only,
        does not affect the gate's own decision to block management (it
        always blocks regardless of whether this tick happens to log)."""
        now = time.time()
        if now - self._last_invalid_open_state_log_ts >= interval_sec:
            self._last_invalid_open_state_log_ts = now
            return True
        return False

    def _should_log_sync_not_ready(self, interval_sec: float = 30.0) -> bool:
        """Throttle shared by every position_sync_ready-gated diagnostic
        line (entry-skip / dca-skip / max-hold-skip / smart-exit-skip) -
        debugging only, does not affect any gate's own decision to block."""
        now = time.time()
        if now - self._last_sync_not_ready_log_ts >= interval_sec:
            self._last_sync_not_ready_log_ts = now
            return True
        return False

    def _should_log_dca_spacing(self, interval_sec: float = 15.0) -> bool:
        """Throttle for the [dca-spacing] diagnostic line (2026-08 DCA
        re-fire spacing fix) - debugging only, does not affect the spacing
        decision itself. Only throttles repeated WAIT ticks; a TRIGGER
        decision always logs regardless (see the caller's own condition)."""
        now = time.time()
        if now - self._last_dca_spacing_log_ts >= interval_sec:
            self._last_dca_spacing_log_ts = now
            return True
        return False

    def _should_log_max_hold_dca_defer(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the Max Hold Time V2 'deferred: DCA opportunity
        available' diagnostic line (2026-08 DCA-awareness fix) - debugging
        only, does not affect whether the position is actually deferred."""
        now = time.time()
        if now - self._last_max_hold_dca_defer_log_ts >= interval_sec:
            self._last_max_hold_dca_defer_log_ts = now
            return True
        return False

    def _should_log_dca_time_blocked(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the [dca-time-blocked] diagnostic line (2026-08
        Option B DCA time gate) - debugging only, does not affect whether a
        new DCA add is actually withheld."""
        now = time.time()
        if now - self._last_dca_time_blocked_log_ts >= interval_sec:
            self._last_dca_time_blocked_log_ts = now
            return True
        return False

    def _should_log_dca_post_step_timeout(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the [dca-blocked-post-step-timeout] diagnostic line
        (item 8 - prospective post-DCA max-hold gate) - debugging only,
        does not affect whether the DCA add is actually withheld."""
        now = time.time()
        if now - self._last_dca_post_step_timeout_log_ts >= interval_sec:
            self._last_dca_post_step_timeout_log_ts = now
            return True
        return False

    def _should_log_dca_loss_budget_blocked(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the [dca-budget] blocked diagnostic line (item 7 -
        DCA loss-budget gate) - debugging only, does not affect whether the
        DCA add is actually withheld."""
        now = time.time()
        if now - self._last_dca_loss_budget_log_ts >= interval_sec:
            self._last_dca_loss_budget_log_ts = now
            return True
        return False

    def _should_log_order_cooldown_block(self, interval_sec: float = 20.0) -> bool:
        """Throttle for the [order-cooldown-block] diagnostic line (item 4 -
        REST cooldown retry-storm fix). Root cause of the live incident this
        addresses: _place_step_order() had no cooldown awareness at all, so
        while the shared RestClient cooldown (see exchange.py's
        _arm_cooldown/is_cooldown_active) was active, EVERY price tick that
        satisfied the DCA trigger distance re-entered this function, which
        called client.place_order() -> _request(), which raised its
        synthetic local BinanceApiError(429) immediately (no network call,
        so no additional real rate-limit damage) - but logged a full
        "[dca] DCA STEP order FAILED" line every single time (434 lines in
        ~22s in the attached log). Gating here stops the redundant order
        attempts (and their per-attempt logging) at the source, for both
        the DCA path and the initial-entry path, without needing to await/
        sleep inside the tick handler (which stays responsive to price
        moves and to the cooldown clearing)."""
        now = time.time()
        if now - self._last_order_cooldown_block_log_ts >= interval_sec:
            self._last_order_cooldown_block_log_ts = now
            return True
        return False

    def _should_log_protection_pending(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the high-severity [protective-stop] PROTECTION_PENDING
        diagnostic line (item 6) - debugging only, does not affect the
        conservative PROTECTION_PENDING state itself or the DCA block it
        drives."""
        now = time.time()
        if now - self._last_protection_pending_log_ts >= interval_sec:
            self._last_protection_pending_log_ts = now
            return True
        return False

    def _should_log_profit_lock_debug(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the [profit-lock-debug] diagnostic line (2026-08
        Profit Lock diagnostics, Issue 2) - debugging only, does not affect
        Profit Lock activation/close behavior."""
        now = time.time()
        if now - self._last_profit_lock_debug_log_ts >= interval_sec:
            self._last_profit_lock_debug_log_ts = now
            return True
        return False

    def _should_log_profit_lock_peak_update(self, interval_sec: float = 10.0) -> bool:
        """Throttle for the [profit-lock-peak] UPDATED diagnostic line
        (2026-08 Profit Lock peak-tracking visibility fix) - debugging
        only, independent of the existing DCA_STATE_PEAK_SAVE_MIN_DELTA_USDT-
        gated [profit-lock] PEAK UPDATED log/persistence trigger, which is
        unchanged. A shorter default interval than the other profit-lock
        debug logs since a fast-trailing peak during a strong move would
        otherwise still print quite often even throttled at 30s."""
        now = time.time()
        if now - self._last_profit_lock_peak_update_log_ts >= interval_sec:
            self._last_profit_lock_peak_update_log_ts = now
            return True
        return False

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

    async def _sweep_orphan_protective_stops(self) -> None:
        """2026-08 cancel-confirmation fix (review finding 5): retries
        cancellation of protective-stop orders whose cancel could not be
        confirmed at close time (see _on_close_filled). Runs regardless of
        whether a position is currently open - an orphaned conditional order
        resting on the exchange is a real hazard precisely when the bot
        believes it is flat, since it could later trigger against a NEW
        position. Throttled to PROTECTIVE_STOP_RETRY_SEC and skipped entirely
        while a REST cooldown is armed, so it can never become a retry storm.
        An id is dropped only when the cancel succeeds or Binance answers
        -2011 (proving it no longer exists)."""
        if DRY_RUN:
            return
        # 2026-08 stale-leftover safety: this sweep now also drives
        # RECONCILIATION retries, not just orphan cancels, and it is the only
        # thing that runs while FLAT (_protective_stop_sweep() returns early
        # unless a position is open). Without this, a FLAT-startup
        # enumeration failure could never be retried, and the stale leftover
        # it failed to find would still be resting when the next position
        # opened.
        needs_reconcile_retry = (
            self._protective_stop_reconcile_blocked or self._stale_protective_stops_possible
        )
        if not self._orphan_protective_algo_ids and not needs_reconcile_retry:
            return
        now = time.time()
        if now - self._last_orphan_sweep_ts < PROTECTIVE_STOP_RETRY_SEC:
            return
        self._last_orphan_sweep_ts = now
        is_cooldown_active = getattr(self.client, "is_cooldown_active", None)
        if is_cooldown_active is not None and is_cooldown_active():
            return
        for order_id in list(self._orphan_protective_algo_ids):
            try:
                await self.client.cancel_algo_order(algo_id=order_id)
                self._orphan_protective_algo_ids.discard(order_id)
                print(color(
                    f"{now_str()} [protective-stop] orphan sweep cancelled stale orderId={order_id}.",
                    GREEN,
                ))
            except BinanceApiError as e:
                if e.code == -2011:  # proven gone
                    self._orphan_protective_algo_ids.discard(order_id)
                    print(color(
                        f"{now_str()} [protective-stop] orphan sweep: orderId={order_id} confirmed "
                        f"already gone.", GRAY,
                    ))
                else:
                    print(color(
                        f"{now_str()} [protective-stop] orphan sweep: cancel orderId={order_id} "
                        f"failed ({e}) - will retry.", YELLOW,
                    ))
            except Exception as e:  # noqa: BLE001 - a sweep must never crash the trading loop
                print(color(
                    f"{now_str()} [protective-stop] orphan sweep: cancel orderId={order_id} error "
                    f"({e}) - will retry.", YELLOW,
                ))

        # Retry reconciliation itself (this is what runs while FLAT). A
        # successful enumeration is the only thing that can clear
        # _protective_stop_reconcile_blocked / _stale_protective_stops_possible
        # and therefore re-open the entry gate.
        if needs_reconcile_retry:
            print(color(
                f"{now_str()} [protective-stop] retrying open-order reconciliation to resolve "
                f"stale protective stops (reconcile_blocked="
                f"{self._protective_stop_reconcile_blocked}, stale_possible="
                f"{self._stale_protective_stops_possible}).", YELLOW,
            ))
            await reconcile_protective_stop_on_startup(self.client, self)

    async def on_price_tick(self) -> None:
        await self._sweep_orphan_protective_stops()
        # 2026-08 post-only (maker) entry execution: a GTX limit order can
        # legitimately rest unfilled forever if price walks away from it.
        # Runs here, before any entry/exit reasoning, so a stale resting
        # entry is cleaned up promptly rather than pinning the state machine
        # in ENTERING/DCA_PENDING. No-op unless a post-only order is
        # actually in flight. See _post_only_entry_watchdog().
        await self._post_only_entry_watchdog()
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

            # 2026-08 dynamic post-loss cool-off (entry gating ONLY -
            # identical in structure and scope to the position_sync_ready /
            # Daily Loss gates below: exit/DCA/risk management for an
            # already-open position, Brain learning and every other function
            # are untouched). Armed by _arm_cool_off() whenever a trade
            # closes at a fee-net loss; blocks new entries outright for
            # COOL_OFF_PERIOD_MINUTES, after which the TIGHTENED orderbook
            # thresholds (entry_imbalance_threshold()/entry_support_min())
            # keep applying for an equally long window. This is the
            # anti-revenge-trading rule.
            if self.in_cool_off():
                if self._should_log_cool_off():
                    print(color(
                        f"{now_str()} [cool-off] entries paused for another "
                        f"{self.cool_off_remaining_sec():.0f}s after a losing trade "
                        f"({self._cool_off_reason or 'recent loss'}) - the orderbook guard "
                        f"stays tightened for a further {COOL_OFF_PERIOD_MINUTES:.0f} min "
                        f"after that. Open positions, if any, are unaffected.", YELLOW,
                    ))
                return

            # 2026-08 position_sync_ready startup gate (isolated to entry
            # gating only - DCA/exit management for an already-open
            # position, Brain learning, and every other function are
            # untouched, identical in structure to the Daily Loss/Warm-up
            # gates right below): position_sync_ready starts False and is
            # only ever set True by initialize_sync() once it has actually
            # obtained authoritative position-risk rows from Binance and
            # reconciled local state against them (see that function).
            # Until then, self.position.status=="FLAT" locally does NOT
            # prove Binance itself is flat - it could be an untouched
            # fresh PositionState(), a rejected/invalid restored snapshot
            # that fell back to FLAT, or a genuinely-stale local view - so
            # no NEW entry may be opened on top of a real, unknown exchange
            # position. Once initialize_sync() confirms readiness this
            # gate never fires again for the rest of the process (it is
            # never reset back to False by a later transient REST
            # failure - see that function's own comment).
            if not self.position_sync_ready:
                if self._should_log_sync_not_ready():
                    print(color(
                        f"{now_str()} [entry-skip] position_sync_ready=False - local state has not yet "
                        f"been reconciled against an authoritative Binance position-risk fetch. "
                        f"No new entry will be opened until initialize_sync() confirms readiness.", YELLOW,
                    ))
                return

            # 2026-08 stale-leftover entry gate (isolated to entry gating
            # only - exit/DCA/risk management for an already-open position is
            # untouched, identical in structure to the position_sync_ready
            # gate directly above). A bot-owned closePosition=true
            # STOP_MARKET left resting by a previous position would trigger
            # against a BRAND-NEW position at a stop price computed for a
            # completely different trade - closing it immediately and at the
            # wrong level. So no new entry is opened while either
            #   (a) open orders could not be enumerated at all, or
            #   (b) a known stale owned stop has not been confirmed cancelled.
            # Both are self-healing: the protective-stop sweep retries
            # enumeration and cancellation every PROTECTIVE_STOP_RETRY_SEC
            # (including while FLAT), and this gate clears the moment every
            # stale owned stop is proven gone.
            if (
                self._protective_stop_reconcile_blocked
                or self._stale_protective_stops_possible
                or self._orphan_protective_algo_ids
            ):
                if self._should_log_protection_pending():
                    print(color(
                        f"{now_str()} [entry-skip] stale protective-stop cleanup unresolved "
                        f"(reconcile_blocked={self._protective_stop_reconcile_blocked}, "
                        f"stale_possible={self._stale_protective_stops_possible}, "
                        f"orphans={sorted(self._orphan_protective_algo_ids)}) - a leftover "
                        f"closePosition=true stop could trigger against a new position at the wrong "
                        f"price. No new entry until every stale owned stop is confirmed gone.", YELLOW,
                    ))
                return

            # 2026-08 Daily Loss Protection (isolated to entry gating only -
            # exit/DCA/risk management for an already-open position, Brain
            # learning, and every other function are untouched, identical in
            # structure to the Startup Warm-up Gate right below): once
            # today's (UTC calendar day) cumulative realized PnL drops to/
            # below -MAX_DAILY_LOSS_USDT, no new entries are opened for the
            # rest of that UTC day. Does not close, modify, or otherwise
            # touch any currently-open position - Hard Stop/TP/Profit Lock/
            # DCA/Max Hold Time all continue exactly as before for a trade
            # that's already running when the limit is hit.
            # 2026-08 CONTINUOUS 24/7 EXECUTION (this `if
            # CONTINUOUS_24_7_TRADING` wrapper only - the daily accounting
            # itself, _maybe_reset_daily_loss_tracker(), daily_realized_pnl,
            # MAX_DAILY_LOSS_USDT / DAILY_PROFIT_TARGET_USDT and both
            # diagnostic log lines are all PRESERVED verbatim below): the
            # bot must keep trading around the clock and must not shut
            # itself down for the rest of a UTC day on a daily profit or
            # loss figure. With CONTINUOUS_24_7_TRADING=true (the default)
            # the two daily halts become periodic informational notices
            # instead of hard `return`s; setting it to false restores the
            # previous daily-halt behavior exactly, with no code change.
            #
            # Risk is not being discarded here, it is being relocated to
            # per-trade controls that this same upgrade tightens: the strict
            # $0.20 fee-net stop (per-trade budget + exchange-native
            # protective STOP_MARKET), the 1:2 RR target, the Smart
            # Orderflow Early Exit, and the post-loss cool-off window - all
            # of which act per trade rather than by freezing the bot for
            # hours at a time.
            self._maybe_reset_daily_loss_tracker()
            if MAX_DAILY_LOSS_USDT > 0 and self.daily_realized_pnl <= -MAX_DAILY_LOSS_USDT:
                if not CONTINUOUS_24_7_TRADING:
                    if self._should_log_daily_loss_block():
                        print(color(
                            f"{now_str()} [daily-loss] entries halted: today's realized PnL "
                            f"${self.daily_realized_pnl:+.4f} <= -${MAX_DAILY_LOSS_USDT:.2f} limit - "
                            f"no new trades until the next UTC day (existing open positions, if any, "
                            f"are unaffected)", RED,
                        ))
                    return
                if self._should_log_continuous_trading():
                    print(color(
                        f"{now_str()} [continuous-24-7] today's realized PnL "
                        f"${self.daily_realized_pnl:+.4f} is past the -${MAX_DAILY_LOSS_USDT:.2f} "
                        f"daily-loss reference, but CONTINUOUS_24_7_TRADING is enabled - the bot "
                        f"keeps trading. Per-trade risk (${MAX_STOP_LOSS_USD:.2f} fee-net stop, "
                        f"1:{self.rr_ratio():.0f} RR target, orderflow early exit, "
                        f"{COOL_OFF_PERIOD_MINUTES:.0f}-min post-loss cool-off) governs from here.",
                        YELLOW,
                    ))

            # Fee-net Daily Profit Lock: once the configured UTC-day target
            # has been realized AFTER commissions, preserve it by refusing
            # new exposure for the remainder of the day. This is symmetric
            # with Daily Loss Protection above and is deliberately an
            # entry-only gate: an already-open position is never abandoned
            # or deprived of TP/Profit-Lock/Smart-Exit/Hard-Stop management.
            if (
                DAILY_PROFIT_TARGET_USDT > 0
                and self.daily_realized_pnl >= DAILY_PROFIT_TARGET_USDT
            ):
                # 2026-08 CONTINUOUS 24/7 EXECUTION - same treatment as the
                # daily-loss gate above: the target and its accounting are
                # preserved exactly, but hitting it no longer stops the bot
                # for the remainder of the UTC day.
                if not CONTINUOUS_24_7_TRADING:
                    if self._should_log_daily_profit_block():
                        print(color(
                            f"{now_str()} [daily-profit] entries halted: today's realized NET PnL "
                            f"${self.daily_realized_pnl:+.4f} >= +${DAILY_PROFIT_TARGET_USDT:.2f} "
                            f"target - profit locked, no new trades until the next UTC day "
                            f"(existing open positions, if any, are unaffected)", GREEN,
                        ))
                    return
                if self._should_log_continuous_trading():
                    print(color(
                        f"{now_str()} [continuous-24-7] today's realized NET PnL "
                        f"${self.daily_realized_pnl:+.4f} has passed the "
                        f"+${DAILY_PROFIT_TARGET_USDT:.2f} daily target, but "
                        f"CONTINUOUS_24_7_TRADING is enabled - the bot keeps trading instead of "
                        f"standing down for the rest of the UTC day.", GREEN,
                    ))

            # 2026-08 Startup Warm-up Gate (isolated to entry gating only -
            # DCA/exit management for an already-open position, Brain
            # learning, and every other function are untouched). Brain's
            # own update_count can in principle reach BRAIN2_WARMUP_UPDATES
            # before the candle/indicator pipeline itself has enough
            # history for a REAL (non-default) regime reading - ticks
            # arrive far more often than candles close. MarketRegimeEngine
            # already falls back to a default RegimeReading(atr_pct=0.0,
            # regime=SIDEWAYS) when len(candles) is below the same
            # max(EMA_SLOW, ATR_PERIOD) + 2 threshold used here, so
            # `self.last_regime.atr_pct <= 0.0` after that many candles is
            # itself a reliable "ATR/EMA not valid yet" signal - no
            # duplicate calculation needed. No entries of any kind (Brain-
            # scored or fallback) are allowed until both conditions clear.
            if len(candles) < max(EMA_SLOW, ATR_PERIOD) + 2 or self.last_regime.atr_pct <= 0.0:
                if self._should_log_warmup_skip():
                    print(color(
                        f"{now_str()} [entry-skip] startup warm-up: insufficient market history "
                        f"(candles={len(candles)}/{max(EMA_SLOW, ATR_PERIOD) + 2}, "
                        f"atr_pct={self.last_regime.atr_pct:.6f}) - indicators not yet valid, "
                        f"no entries opened", GRAY,
                    ))
                return

            if not self.brain.is_ready():
                # (2026-07 profitability fix) The static tick-momentum
                # fallback traded a deadband smaller than normal BTC
                # bid/ask jitter and completely bypassed EntryEngineV2's
                # score threshold and regime gate - it was a major source
                # of low-quality, no-edge entries. No fresh trades are
                # opened until Brain V2 has enough updates to be ready;
                # DCA/exit management for any already-open position is
                # untouched and continues to run normally below.
                if self._should_log_warmup_skip():
                    print(color(
                        f"{now_str()} [entry-skip] brain not ready "
                        f"(updates={self.brain.update_count}/{self.brain.warmup_updates}) - "
                        f"fallback momentum entries disabled, no new trades opened",
                        GRAY,
                    ))
                return

            volumes = [c.volume for c in candles]
            volume_z = 0.0
            if len(volumes) >= 10:
                vmean, vstd = float(np.mean(volumes[-30:])), float(np.std(volumes[-30:]))
                volume_z = clamp(safe_div(volumes[-1] - vmean, vstd, 0.0), -4.0, 4.0) if vstd else 0.0
            # (2026-08 entry-timing fix) features[4] is rolling_return_5 (the
            # short multi-candle rolling return) rather than features[22]'s
            # single-tick momentum_short - see EntryEngineV2.evaluate's
            # momentum_component comment for why the previous index/threshold
            # pairing left this scoring component effectively inert.
            momentum = float(features[4]) if len(features) > 4 else 0.0  # rolling_return_5

            # 2026-08 Liquidity & Flow Guard: ONE orderflow snapshot per
            # decision, taken here and threaded through, so the entry guard
            # can never see a different book than the rest of this tick.
            # The imbalance/support thresholds are the cool-off-aware
            # values (tightened for the window following a losing trade) -
            # see entry_imbalance_threshold()/entry_support_min().
            orderflow_now = self.orderflow_snapshot()
            decision = self.entry_engine.evaluate(
                self.last_confidence, self.last_regime, volume_z, momentum, features,
                brain_readiness=self.brain.head_readiness(),
                orderflow=orderflow_now,
                imbalance_threshold=self.entry_imbalance_threshold(),
                support_min=self.entry_support_min(),
            )
            self.last_entry_decision = decision
            if decision.should_enter and decision.side is not None:
                # Initial entry ALWAYS uses the configured INITIAL_ENTRY_USDT
                # unscaled - confidence/regime/risk-based sizing only ever
                # applies to DCA additions placed after the position is
                # already open (see _manage_open_position). This guarantees
                # notional_for_step(0, ...) == INITIAL_ENTRY_USDT * LEVERAGE
                # regardless of how confident the Brain is at entry time.
                self.position.entry_features = features.copy()
                self.position.entry_regime = self.last_regime.regime
                self.position.entry_confidence = self.last_confidence.confidence_score
                self.position.entry_risk_score = self.last_confidence.risk_score
                self.position.entry_success_prob = self.last_confidence.success_probability
                self.position.entry_tp_hit_prob = self.last_confidence.tp_hit_probability
                self.position.entry_dynamic_tp_pct = self.get_dynamic_take_profit_pct()
                # 2026-08 entry-quality audit fix (item 10 - issue #10):
                # entry_confidence above is ONLY the brain_confidence
                # sub-component (conf.confidence_score) - NOT the same
                # value as the composite Entry Score actually compared
                # against the acceptance threshold. Recorded separately so
                # the trade log's real accept/reject basis is auditable
                # (see TRADE_LOG_FIELDS).
                self.position.entry_composite_score = decision.score
                self.position.entry_score_threshold = decision.components.get("threshold", 0.0)
                await self._place_step_order(step=0, side_signal=decision.side, size_mult=1.0)
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

    # ---------------------------------------------------------------------
    # 2026-08 POST-ONLY (MAKER) ENTRY EXECUTION
    # ---------------------------------------------------------------------
    # Entries and the single DCA rescue add are submitted as post-only
    # (timeInForce=GTX) LIMIT orders resting at the near touch, so they pay
    # the MAKER fee instead of the taker fee and take zero spread slippage.
    # On a $80 notional round trip at Binance USD-M's standard rates that is
    # roughly a 0.02% saving on the entry leg plus the half-spread - money
    # that matters a great deal against a $0.40 profit target.
    #
    # Three things make this safe rather than merely cheaper:
    #   1. Binance REJECTS (rather than fills) a GTX order that would cross,
    #      so a post-only entry can never silently become a taker fill. We
    #      re-price one tick further from the touch and retry exactly once.
    #   2. If it still cannot rest, POST_ONLY_MARKET_FALLBACK (default on)
    #      falls back to the ORIGINAL MARKET order, so a fast tape can never
    #      leave the bot unable to trade at all. Set it to false to require
    #      maker-only execution.
    #   3. A resting maker order that price walks away from is cancelled by
    #      _post_only_entry_watchdog() after POST_ONLY_LIMIT_TIMEOUT_SEC and
    #      the decision is simply re-made on a later tick.
    # EXITS ARE DELIBERATELY UNCHANGED: every close path (hard stop, TP,
    # profit lock, smart exit, max hold, the reduceOnly close-verify
    # retries, the exchange-native protective stop) still uses immediate
    # MARKET/STOP_MARKET execution. Risk-reducing orders must fill, and a
    # resting maker exit is exactly the order that does not fill when it is
    # needed most.

    @staticmethod
    def _is_post_only_reject(e: BinanceApiError) -> bool:
        """True when Binance refused a GTX order specifically because it
        would have executed as a taker (-5022), or the equivalent
        'would immediately trigger/match' rejection (-2010/-2021)."""
        if e.code in (-5022, -2010, -2021):
            return True
        msg = ""
        if isinstance(e.data, dict):
            msg = str(e.data.get("msg", "")).lower()
        return "post only" in msg or "post-only" in msg or "immediately match" in msg

    def _round_price_to_tick(self, price: float, side: str) -> float:
        """Snap a maker price to the symbol's tick grid, always AWAY from
        the crossing direction: a BUY rounds DOWN (stays at/below the bid),
        a SELL rounds UP (stays at/above the ask). round_step() only ever
        rounds down, so the SELL case is computed explicitly here.

        Both branches go through Decimal for the same reason round_step()
        does: a plain `math.ceil(price / tick) * tick` reintroduces binary
        float artifacts (e.g. 80.01000000000001), and Binance rejects a
        price whose precision does not match the symbol's tick size."""
        tick = self.filters.tick_size
        if not tick or tick <= 0:
            return price
        if side == "BUY":
            return round_step(price, tick)
        d_price = Decimal(str(price))
        d_tick = Decimal(str(tick))
        steps = (d_price / d_tick).to_integral_value(rounding=ROUND_UP)
        return float(steps * d_tick)

    def _post_only_entry_price(self, order_side: str, offset_ticks: int = 0) -> Optional[float]:
        """Resting maker price for an entry on `order_side`.

        A BUY rests at the best bid, a SELL at the best ask - the near
        touch, i.e. the most aggressive price that is still passive.
        `offset_ticks` steps further away from the touch (used by the single
        re-price retry after a post-only rejection). Returns None when no
        live book is available, in which case the caller falls back to the
        original MARKET behavior rather than guessing a price."""
        tick = self.filters.tick_size or 0.0
        if order_side == "BUY":
            base = self.best_bid_price or self.current_price
            if not base:
                return None
            price = base - offset_ticks * tick
        else:
            base = self.best_ask_price or self.current_price
            if not base:
                return None
            price = base + offset_ticks * tick
        if price <= 0:
            return None
        return self._round_price_to_tick(price, order_side)

    async def _submit_entry_order(
        self, order_side: str, qty: float, step_label: str,
    ) -> Tuple[dict, str, bool]:
        """Submit one entry/DCA order and return (response, label, was_post_only).

        Raises BinanceApiError exactly like the original inline
        place_order() call did, so _place_step_order()'s existing
        error-handling / pending-state-revert path is unchanged."""
        if not USE_POST_ONLY_LIMIT:
            resp = await self.client.place_order(
                symbol=self.symbol, side=order_side, type="MARKET", quantity=qty,
            )
            return resp, "MARKET", False

        base_offset = max(POST_ONLY_LIMIT_OFFSET_TICKS, 0)
        last_error: Optional[BinanceApiError] = None
        # Attempt 0 rests at the touch; attempt 1 steps one further tick
        # back after a post-only rejection (the book moved between our
        # snapshot and the order reaching the matching engine).
        for attempt in range(2):
            price = self._post_only_entry_price(order_side, base_offset + attempt)
            if price is None:
                break
            try:
                resp = await self.client.place_order(
                    symbol=self.symbol, side=order_side, type="LIMIT",
                    timeInForce="GTX", price=price, quantity=qty,
                )
                print(color(
                    f"{now_str()} [post-only] {step_label} resting as MAKER: {order_side} {qty} "
                    f"@ {price} (attempt {attempt + 1}/2, GTX - Binance rejects rather than "
                    f"crossing, so this can never become a taker fill)", CYAN,
                ))
                return resp, "LIMIT/GTX (post-only)", True
            except BinanceApiError as e:
                if not self._is_post_only_reject(e):
                    raise
                last_error = e
                print(color(
                    f"{now_str()} [post-only] {step_label} rejected as it would have crossed "
                    f"(attempt {attempt + 1}/2, {e}) - re-pricing one tick further from the "
                    f"touch.", YELLOW,
                ))

        if not POST_ONLY_MARKET_FALLBACK:
            if last_error is not None:
                raise last_error
            raise BinanceApiError(
                400,
                {"code": -5022, "msg": "post-only entry could not be priced (no live book)"},
            )

        print(color(
            f"{now_str()} [post-only] {step_label} could not rest as maker - falling back to the "
            f"original MARKET execution so the signal is not lost (set "
            f"POST_ONLY_MARKET_FALLBACK=false to require maker-only entries).", YELLOW,
        ))
        resp = await self.client.place_order(
            symbol=self.symbol, side=order_side, type="MARKET", quantity=qty,
        )
        return resp, "MARKET (post-only fallback)", False

    async def _post_only_entry_watchdog(self) -> None:
        """Cancel a post-only entry/DCA order that has rested unfilled for
        longer than POST_ONLY_LIMIT_TIMEOUT_SEC.

        Why this is needed: the existing sync machinery correctly treats a
        REST-confirmed NEW order as "keep waiting" (see initialize_sync's
        resolution == "pending" branches), which is exactly right for a
        MARKET order that simply has not reported yet - but a maker order
        price has walked away from can rest forever, pinning the state
        machine in ENTERING/DCA_PENDING.

        On a confirmed cancel this only clears the pending bookkeeping and
        returns the position to its prior state (FLAT for an entry, OPEN for
        a DCA add); it deliberately does NOT re-derive quantities or place a
        replacement order. If the order had partially filled, the
        authoritative exchange resync (initialize_sync, running every
        POSITION_RISK_POLL_SEC independently of this) rebuilds real state -
        the same self-healing path every other ambiguous-order case in this
        file already relies on."""
        order_id = self._post_only_order_id
        if order_id is None:
            return
        p = self.position
        if p.pending_order_id != order_id or p.status not in ("ENTERING", "DCA_PENDING"):
            # Filled, replaced, or resolved by some other path - stop
            # tracking it; nothing to cancel.
            self._post_only_order_id = None
            self._post_only_submitted_ts = 0.0
            return
        if time.time() - self._post_only_submitted_ts < POST_ONLY_LIMIT_TIMEOUT_SEC:
            return
        if DRY_RUN:
            self._post_only_order_id = None
            self._post_only_submitted_ts = 0.0
            return
        is_cooldown_active = getattr(self.client, "is_cooldown_active", None)
        if is_cooldown_active is not None and is_cooldown_active():
            return  # retry on a later tick; never add REST load during a 418/429 cooldown

        print(color(
            f"{now_str()} [post-only] entry order_id={order_id} has rested unfilled for "
            f"{time.time() - self._post_only_submitted_ts:.0f}s (>= "
            f"{POST_ONLY_LIMIT_TIMEOUT_SEC:.0f}s) - cancelling; the decision will be re-made "
            f"from scratch on a later tick.", YELLOW,
        ))
        try:
            await self.client.cancel_order(symbol=self.symbol, order_id=order_id)
        except BinanceApiError as e:
            if e.code == -2011:  # "Unknown order sent" - already filled or already gone
                print(color(
                    f"{now_str()} [post-only] order_id={order_id} no longer exists on Binance "
                    f"(filled or already cancelled) - leaving state for the normal fill/resync "
                    f"path to resolve.", GRAY,
                ))
                self._post_only_order_id = None
                self._post_only_submitted_ts = 0.0
                return
            print(color(
                f"{now_str()} [post-only] cancel of order_id={order_id} FAILED ({e}) - leaving "
                f"state untouched; will retry on a later tick.", RED,
            ))
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(
                f"{now_str()} [post-only] cancel of order_id={order_id} failed to reach Binance "
                f"({e}) - leaving state untouched; will retry on a later tick.", RED,
            ))
            return

        self._post_only_order_id = None
        self._post_only_submitted_ts = 0.0
        # Re-read self.position: a concurrent resync may have replaced it
        # while the cancel was in flight, in which case that authoritative
        # rebuild owns the state and must not be overwritten here.
        p = self.position
        if p.pending_order_id != order_id:
            return
        self._order_index.pop(order_id, None)
        p.pending_order_id = None
        p.pending_role = None
        p.status = "OPEN" if p.total_qty > 0 else "FLAT"
        if p.status == "FLAT":
            p.side = None
        print(color(
            f"{now_str()} [post-only] cancel confirmed - position returned to {p.status}; "
            f"entry/DCA will be re-evaluated on the next qualifying tick.", GRAY,
        ))

    async def _place_step_order(
        self, step: int, side_signal: str, size_mult: float = 1.0,
        expected_position: Optional["PositionState"] = None,
    ) -> None:
        # item 4 - REST cooldown retry-storm fix (this check only - every
        # other line of this function, including the DRY_RUN branch, the
        # BinanceApiError handler, and every safety gate below, is
        # unchanged). Root cause of the attached live incident: this
        # function had no cooldown awareness at all, so while the shared
        # RestClient cooldown (exchange.py's is_cooldown_active(), armed by
        # a real Binance 418/429) was active, EVERY price tick that
        # satisfied the DCA trigger re-entered here, reached
        # client.place_order(), and got a synthetic local
        # BinanceApiError(429) raised by _request() BEFORE any network call
        # was made - safe (no real request sent, no state left dangling;
        # the existing BinanceApiError handler below already reverts the
        # pending-state transition correctly), but logged a full "order
        # FAILED" line on every single tick (434 lines in ~22s in the
        # attached log) and did needless work on every one. Checking here,
        # before ANY state mutation or notional/qty calculation, protects
        # both this DCA-add call site and the initial-entry call site
        # (on_price_tick) uniformly. No sleep/await is used (would make the
        # tick handler unresponsive) - the very next met-trigger tick after
        # the cooldown clears re-evaluates and proceeds normally; the
        # position's local state (status/dca_step) is left completely
        # untouched while blocked, so this is never treated as an ambiguous
        # fill.
        # getattr(..., None) defensively - some test doubles for `client`
        # (e.g. a minimal FakeClient used by unrelated regression tests)
        # don't implement is_cooldown_active(); treated as "not in
        # cooldown" rather than raising, matching how those tests already
        # exercise every OTHER cooldown-unaware code path in this file.
        # The real RestClient (exchange.py) always has this method.
        is_cooldown_active = getattr(self.client, "is_cooldown_active", None)
        if is_cooldown_active is not None and is_cooldown_active():
            if step == 0 or self._should_log_order_cooldown_block():
                remaining = self.client.cooldown_remaining()
                step_desc = "initial entry" if step == 0 else f"DCA step {step}/{MAX_DCA_STEPS}"
                print(color(
                    f"{now_str()} [order-cooldown-block] {step_desc} order withheld - local REST "
                    f"cooldown active for another {remaining:.0f}s (no network request attempted, "
                    f"no state changed).", YELLOW,
                ))
            return
        # 2026-08 stale-decision safety gate - identical guard/rationale to
        # close_position()'s own (see its docstring comment): the DCA-add
        # call site inside _manage_open_position() passes its own `p` as
        # expected_position; if self.position has since been swapped by a
        # concurrent sync, this DCA decision is stale and must not be
        # placed against whatever replaced it. The initial-entry call site
        # (try_enter_position, a FLAT->ENTERING transition with no `p` to
        # protect) doesn't pass expected_position and is unaffected.
        if expected_position is not None and expected_position is not self.position:
            print(color(
                f"{now_str()} [stale-decision-guard] _place_step_order(step={step}) skipped - "
                f"self.position was replaced by a concurrent sync while this decision was in "
                f"flight; re-evaluating against current state next tick instead.", YELLOW,
            ))
            return
        # Final order-boundary enforcement for the hard DCA limit. Normal
        # management already stops before calling this function at the
        # limit; this also protects a delayed/stale direct call.
        current_step, current_step_safety_reason = sanitize_recovered_dca_step(
            self.position.dca_step
        )
        self.position.dca_step = current_step
        if current_step_safety_reason is not None:
            self.position.dca_blocked = True
            self.position.dca_block_reason = current_step_safety_reason
        if step > 0 and (
            step > MAX_DCA_STEPS
            or current_step >= MAX_DCA_STEPS
            or self.position.dca_blocked
        ):
            print(color(
                f"{now_str()} [dca-safety-block] refusing DCA step={step}: "
                f"current_step={self.position.dca_step}/{MAX_DCA_STEPS} "
                f"blocked={self.position.dca_blocked}; no order submitted.", RED,
            ))
            return
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

        # 2026-08 stale-decision safety gate, SECOND check (this check
        # only - everything else, including the DRY_RUN branch right
        # below, is unchanged): the guard at function entry above only
        # protects against self.position being swapped BEFORE this call
        # started. Although nothing currently awaits between that entry
        # check and here, re-verifying identity immediately before
        # mutating pending state / submitting the order (whichever comes
        # first - DRY_RUN or the real order path below) means this stays
        # correct even if a future change inserts an await in between -
        # the actual order boundary is what must be protected, not just
        # the function's first line. Aborts without touching
        # status/side/pending_order_ts and without ever placing an order
        # (real or simulated).
        if expected_position is not None and expected_position is not self.position:
            print(color(
                f"{now_str()} [stale-decision-guard] _place_step_order(step={step}) skipped "
                f"immediately before submission - self.position was replaced by a concurrent "
                f"sync; no order submitted, no pending state mutated.", YELLOW,
            ))
            return

        if DRY_RUN:
            fake_id = -(int(time.time() * 1000) % 1_000_000) - step
            print(color(
                f"{now_str()} [DRY RUN] would place {step_label} {order_side} {qty} "
                f"{self.symbol} @ "
                f"{'post-only LIMIT/GTX' if USE_POST_ONLY_LIMIT else 'market'} "
                f"(~{price:.2f}, notional=${notional:.2f}, "
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

        # 2026-08 entry-context/commission race fix: transition status
        # (and side/pending_order_ts) to the pending state BEFORE placing
        # the order, not after the REST round-trip returns. Root cause:
        # initialize_sync() already has a grace-period guard
        # (SYNC_PENDING_GRACE_SEC) specifically to defer its position
        # rebuild while an order is known to be in flight - but it only
        # recognizes p.status in ("ENTERING", "DCA_PENDING", "CLOSING").
        # With status left at "FLAT"/"OPEN" for the entire place_order()
        # REST call (as it was before this fix), a concurrent
        # initialize_sync() poll landing in that window saw no reason to
        # defer and fell through to a full position rebuild - silently
        # replacing self.position (losing entry_confidence/entry_features/
        # entry_regime/entry_dynamic_tp_pct, none of which are part of the
        # DCA-state snapshot schema) and unconditionally resetting
        # _position_fees_accum/_position_fees_reliable, discarding any
        # entry-side commission already accumulated. Setting these fields
        # here reuses that EXISTING grace-period protection immediately -
        # initialize_sync() itself needed no changes at all.
        prior_status = self.position.status
        prior_side = self.position.side
        prior_pending_ts = self.position.pending_order_ts
        self.position.status = "ENTERING" if step == 0 else "DCA_PENDING"
        self.position.side = side_signal
        self.position.pending_order_ts = time.time()

        try:
            # 2026-08 post-only (maker) entry execution: replaces the bare
            # `place_order(type="MARKET", ...)` that used to be inline here.
            # Every line after this call - _register_order_and_replay(), the
            # already_filled race handling, the pending bookkeeping, the
            # BinanceApiError revert path below - is unchanged, and with
            # USE_POST_ONLY_LIMIT=false _submit_entry_order() issues the
            # exact same MARKET order this used to.
            resp, exec_label, was_post_only = await self._submit_entry_order(
                order_side, qty, step_label,
            )
            order_id = resp["orderId"]
            if was_post_only:
                # Track it so _post_only_entry_watchdog() can cancel it if
                # price walks away and it never fills.
                self._post_only_order_id = order_id
                self._post_only_submitted_ts = time.time()
            # 2026-08 fill-tracking race fix: was a bare
            # `self._order_index[resp["orderId"]] = role` - now goes through
            # _register_order_and_replay() so a FILLED event that arrived
            # before this line (asyncio can schedule the websocket consumer
            # while this coroutine was still awaiting place_order()'s REST
            # response) gets replayed instead of silently dropped. If that
            # happened, the position has ALREADY been advanced by that
            # replay (dca_step incremented, status back to OPEN) - do not
            # then overwrite it with "still pending" bookkeeping below.
            already_filled = await self._register_order_and_replay(order_id, role)
            if not already_filled:
                self.position.pending_order_id = order_id
                self.position.pending_role = role
                self.last_trade_action_ts = time.time()
            print(color(
                f"{now_str()} {step_label} PLACED  {order_side} {qty} {self.symbol} "
                f"@ {exec_label} (notional=${notional:.2f}, orderId={order_id}, "
                f"size_mult={size_mult:.2f}, regime={self.last_regime.regime})",
                CYAN,
            ))
        except BinanceApiError as e:
            print(color(f"[dca] {step_label} order FAILED: {e}", RED))
            # The order was never placed (or Binance definitively rejected
            # it) - revert the early pending-state transition above so the
            # position doesn't get stuck thinking an order is in flight
            # when none exists. Restores exactly what was there before this
            # call, for both the fresh-entry (step 0, FLAT->ENTERING) and
            # DCA-add (OPEN->DCA_PENDING) cases.
            self.position.status = prior_status
            self.position.side = prior_side
            self.position.pending_order_ts = prior_pending_ts
            # 2026-08 post-only: nothing is resting on the exchange after a
            # rejected/failed submission, so clear the watchdog tracking too.
            self._post_only_order_id = None
            self._post_only_submitted_ts = 0.0

    async def _fetch_exchange_position(self) -> Optional[Tuple[Optional[str], float]]:
        """Fetches THIS symbol's authoritative position from Binance.

        2026-08 execution-reliability hardening: extracted from
        close_position()'s original inline fetch so the same authoritative
        source is used both before submitting a close order AND after
        (verifying the fill actually zeroed the position - see
        _on_close_filled()) - one implementation, not two that could drift
        apart.

        Returns (side, abs_qty) where side is "LONG"/"SHORT"/None (None
        means the exchange reports flat). Returns None if the fetch itself
        failed (network/API/anything unexpected) - callers MUST treat None
        as "unknown, don't trust this," never as "flat". Broad except
        (matches the persistence-layer's own "must never crash the trading
        loop" convention elsewhere in this file) since a naive `self.client
        is None` in some future caller must degrade to this same safe
        fallback, not raise.
        """
        try:
            rows = await self.client.get_position_risk(self.symbol)
            row = next((r for r in rows if r.get("symbol") == self.symbol), None)
            amt = float(row.get("positionAmt", 0) or 0) if row is not None else 0.0
            side = "LONG" if amt > 0 else ("SHORT" if amt < 0 else None)
            return side, abs(amt)
        except Exception as e:  # noqa: BLE001 - a position-risk fetch must never crash the trading loop
            print(color(f"{now_str()} [exchange-verify] could not fetch position for {self.symbol}: {e}", YELLOW))
            return None

    async def _place_reduce_only_close_order(self, close_side: str, qty: float) -> Optional[int]:
        """Places a single reduceOnly MARKET order and registers it for
        fill-tracking. Returns the order_id (or a fake negative id in
        DRY_RUN), or None if placement failed (already logged - caller
        decides how to react, e.g. leaving the position tracked as OPEN
        with the correct remaining qty rather than assuming success).

        2026-08 execution-reliability hardening: extracted from
        close_position() so the SAME order-placement/tracking mechanics are
        used both for the initial close attempt and for any automatic
        close-verification retry in _on_close_filled() - one
        implementation, not a second copy that could drift apart. Does NOT
        touch self.position.status or finalize anything; callers own that.
        """
        if DRY_RUN:
            fake_id = -(int(time.time() * 1000) % 1_000_000) - 900000 - self._closing_retry_count
            self._order_index[fake_id] = "close"
            self.position.pending_order_id = fake_id
            self.position.pending_role = "close"
            print(color(
                f"{now_str()} [DRY RUN] would place CLOSE {close_side} {qty} "
                f"{self.symbol} reduceOnly MARKET", GRAY
            ))
            return fake_id

        try:
            resp = await self.client.place_order(
                symbol=self.symbol, side=close_side, type="MARKET",
                quantity=qty, reduceOnly="true",
            )
            order_id = resp["orderId"]
            # 2026-08 fill-tracking race fix (same as _place_step_order()) -
            # replaces a bare `self._order_index[resp["orderId"]] = "close"`
            # so a FILLED event that arrived before this line is replayed
            # instead of dropped (it would otherwise only ever be recovered
            # later via _try_recover_close_fill()'s persisted-snapshot
            # path, which needs save_dca_state() below to have run first -
            # a real gap this closes for the common in-session case).
            already_filled = await self._register_order_and_replay(order_id, "close")
            if not already_filled:
                self.position.pending_order_id = order_id
                self.position.pending_role = "close"
                # Restart-safe close-fill recovery: persist the pending close
                # order's id/role now, before its FILLED event arrives - see
                # handle_order_update()'s untracked_order_id recovery path.
                asyncio.create_task(self.save_dca_state(reason="close order placed"))
            # NOTE: if already_filled is True, _on_close_filled() (run via the
            # replay above) has already run its own verification/finalize
            # logic - saving a "close order placed" snapshot afterward would
            # be wrong, hence the guard above instead of an unconditional call.
            return order_id
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # 2026-08 close-fill reliability fix: this branch means the
            # REQUEST may have reached Binance even though the RESPONSE
            # never did (network drop, client-side timeout) - unlike
            # BinanceApiError below, which means Binance itself
            # definitively rejected the order (no ambiguity there, no
            # follow-up needed). If the order actually executed on
            # Binance's side despite the lost response, this process never
            # learns its order_id and can never register/replay its FILLED
            # event through the normal _register_order_and_replay() path -
            # the buffered event in _unmatched_fills would sit unclaimed
            # until TTL expiry regardless of how long that TTL is, and the
            # position would incorrectly stay tracked as OPEN/CLOSING. One
            # immediate check closes this gap: if the exchange now confirms
            # flat, trigger the existing reconciliation safety net right
            # away instead of waiting for the next periodic pass to
            # discover the mismatch.
            print(color(
                f"[position] FAILED to place reduceOnly close order (network/timeout): {e} - "
                f"checking exchange directly to see if it executed anyway ...", YELLOW,
            ))
            try:
                exchange_state = await self._fetch_exchange_position()
                if exchange_state is not None and exchange_state[0] is None:
                    print(color(
                        f"{now_str()} [position] exchange confirms FLAT despite the lost order "
                        f"response - the close likely executed; triggering immediate reconciliation "
                        f"instead of waiting for the next periodic pass.", MAGENTA,
                    ))
                    await self.reconcile_trade_history_from_exchange(context="lost_close_response")
                else:
                    print(color(
                        "[position] exchange still shows an open position - the close order "
                        "genuinely did not execute. POSITION MAY STILL BE OPEN, check manually!", RED,
                    ))
            except Exception as verify_err:  # noqa: BLE001 - this follow-up check must never crash the trading loop
                print(color(
                    f"[position] could not verify exchange state after lost close response: "
                    f"{verify_err} - POSITION MAY STILL BE OPEN, check manually!", RED,
                ))
            return None
        except BinanceApiError as e:
            print(color(
                f"[position] FAILED to place reduceOnly close order: {e} - "
                f"POSITION MAY STILL BE OPEN, check manually!", RED
            ))
            return None
        except Exception as e:  # noqa: BLE001 - order placement must never crash the trading loop;
            # any unexpected failure here is exactly as dangerous as a
            # recognized BinanceApiError (a close order that may or may not
            # have gone through) and must be handled the same way, not
            # propagate and kill the event loop mid-close.
            print(color(
                f"[position] UNEXPECTED error placing reduceOnly close order: {e} - "
                f"POSITION MAY STILL BE OPEN, check manually!", RED
            ))
            return None

    async def close_position(
        self, reason: str, emergency: bool = False, exit_reason_tag: str = "manual",
        expected_position: Optional["PositionState"] = None,
    ) -> None:
        # 2026-08 stale-decision safety gate (this check only - everything
        # below is unchanged): _manage_open_position() binds p = self.position
        # once at the top and reasons about IT for the rest of that call,
        # but a concurrent initialize_sync() (periodic poll / reconnect /
        # startup) can reassign self.position to a brand-new PositionState
        # object while _manage_open_position() is still awaiting something
        # earlier in the same tick. Callers inside _manage_open_position()
        # pass their own `p` as expected_position; if self.position has
        # since been swapped out from under them, the decision was reasoned
        # about a PositionState that this manager no longer considers
        # current, so it must not be executed against whatever replaced it -
        # skip this call entirely and let the NEXT tick (which will
        # re-evaluate against the current self.position) decide fresh.
        # Callers outside that race (e.g. position_risk_poller's own direct
        # close) don't pass expected_position and are completely unaffected.
        if expected_position is not None and expected_position is not self.position:
            print(color(
                f"{now_str()} [stale-decision-guard] close_position(reason={reason}) skipped - "
                f"self.position was replaced by a concurrent sync while this decision was in "
                f"flight; re-evaluating against current state next tick instead.", YELLOW,
            ))
            return
        if self.position.status not in ("OPEN", "DCA_PENDING") or self.position.total_qty <= 0:
            return

        # 2026-08 cooldown-scope fix (review finding 6). BEHAVIOR-PRESERVING:
        # while a 418/429 cooldown is armed, exchange.py's _request() already
        # refuses to send ANY request (it raises a synthetic 429 locally and
        # never touches the network), so neither the pre-close positionAmt
        # fetch nor the reduceOnly order below can possibly reach Binance -
        # this gate does not suppress a close that would otherwise succeed,
        # and it cannot delay risk reduction that was ever achievable.
        #
        # What it DOES prevent is the per-tick churn the old code produced
        # during the 25-minute ban: every tick ran _fetch_exchange_position()
        # (one synthetic-429 error log), flipped status OPEN -> CLOSING,
        # failed to place the order (a second, alarming "POSITION MAY STILL
        # BE OPEN, check manually!" log), then flipped status back to OPEN -
        # hundreds of times. The exchange-native protective stop (item 6) is
        # the mechanism that actually protects the position in this window;
        # this gate just stops the local thrash and logs once per interval.
        is_cooldown_active = getattr(self.client, "is_cooldown_active", None)
        if not DRY_RUN and is_cooldown_active is not None and is_cooldown_active():
            if self._should_log_order_cooldown_block():
                remaining = getattr(self.client, "cooldown_remaining", lambda: 0.0)()
                print(color(
                    f"{now_str()} [order-cooldown-block] close_position(reason={reason}) cannot be "
                    f"submitted - REST cooldown active for another {remaining:.0f}s (every request "
                    f"is refused locally; no order can reach Binance). Position state left "
                    f"unchanged; the close will be re-evaluated each tick and submitted as soon as "
                    f"cooldown clears. The exchange-native protective stop remains in force.", YELLOW,
                ))
            return

        close_side = "SELL" if self.position.side == "LONG" else "BUY"

        # 2026-08 close-verification fix: reset the close-sequence
        # accumulators for this FRESH close attempt (a retry continuing an
        # already-in-progress sequence goes through _on_close_filled()
        # directly, not back through here, so this reset never clobbers an
        # in-flight retry's accumulated PnL/count).
        self._closing_accumulated_rp = 0.0
        self._closing_retry_count = 0

        # 2026-08 dust-position fix: previously this always derived the
        # close quantity from the LOCALLY-tracked self.position.total_qty
        # (accumulated via ordinary float addition across every entry/DCA
        # fill), rounded DOWN to step_size. Local float accumulation can
        # drift a hair below the true value - e.g. 0.0040999999999999995
        # instead of exactly 0.0041 - and round_step()'s ROUND_DOWN then
        # floors an ALREADY-slightly-under value to the next step below
        # that, under-closing by a full step. The bot believes the trade
        # is fully closed while Binance is left holding a real dust
        # position. Now fetches the exchange's own authoritative
        # positionAmt (via _fetch_exchange_position() above) immediately
        # before submitting the close order and closes exactly that (still
        # rounded down to step_size for Binance's own precision rules -
        # safe now, since it starts from the true value instead of a
        # possibly-drifted local copy). Skipped in DRY_RUN (nothing real to
        # query - local qty is already authoritative there). Falls back to
        # the previous local-total_qty behavior if the fetch fails, returns
        # nothing for this symbol, or reports a side that doesn't match
        # local state - a transient API hiccup must never be able to block
        # an emergency close (Hard Stop, Max Hold Time, etc. all depend on
        # this function actually running).
        qty = round_step(self.position.total_qty, self.filters.step_size)
        qty_source = "local total_qty"
        if not DRY_RUN:
            exchange_state = await self._fetch_exchange_position()
            if exchange_state is not None:
                exchange_side, amt_abs = exchange_state
                if exchange_side == self.position.side:
                    fresh_qty = round_step(amt_abs, self.filters.step_size)
                    if fresh_qty > 0:
                        if abs(fresh_qty - qty) > max(self.filters.step_size, 1e-9):
                            print(color(
                                f"{now_str()} [close-qty] exchange positionAmt ({amt_abs}) differs "
                                f"from local total_qty ({self.position.total_qty}) by more than one "
                                f"step - using the exchange value to avoid leaving dust.", YELLOW,
                            ))
                        qty = fresh_qty
                        qty_source = "exchange positionAmt"
                    else:
                        # Same side, but rounds to a genuinely un-closeable
                        # dust amount below step_size - treat identically to
                        # the confidently-FLAT case below rather than
                        # attempting a reduceOnly order for a quantity
                        # Binance can't even represent.
                        exchange_side = "FLAT"
                if exchange_side is None:
                    exchange_side = "FLAT"
                if exchange_side == "FLAT":
                    # Exchange confidently reports NO open position (amt
                    # rounds to 0) - there is nothing to close. Falling
                    # back to local total_qty here would submit a
                    # reduceOnly order Binance will very likely reject
                    # (nothing to reduce), which the existing
                    # BinanceApiError handler below would then treat as a
                    # FAILED close and reset status back to "OPEN" - wrong,
                    # since the position is actually already flat. Instead,
                    # reconcile local state to FLAT directly, exactly like
                    # initialize_sync()'s own "exchange reports NO open
                    # position" branch does, and skip order placement
                    # entirely.
                    print(color(
                        f"{now_str()} [close-qty] exchange already reports FLAT for {self.symbol} "
                        f"(no order needed) - reconciling local state to FLAT instead of submitting "
                        f"a reduceOnly order for a position that no longer exists.", YELLOW,
                    ))
                    # item 6: never leave a stale protective stop resting
                    # once the exchange itself already reports flat.
                    if self.position.protective_stop_algo_id is not None:
                        confirmed_gone = await self._cancel_protective_stop(
                            reason="exchange already flat at close time"
                        )
                        # review finding 5: same orphan handoff as
                        # _on_close_filled() - the PositionState is replaced
                        # immediately below, so an unconfirmed cancel must be
                        # remembered at manager level or it is lost.
                        if not confirmed_gone and self.position.protective_stop_algo_id is not None:
                            self._orphan_protective_algo_ids.add(self.position.protective_stop_algo_id)
                    self.position = PositionState(last_close_time=time.time())
                    self.last_trade_action_ts = time.time()
                    # 2026-08 fix B: position gone - clear its entry-leg floor.
                    self._open_position_first_trade_id = None
                    asyncio.create_task(self.save_flat_dca_state(reason="exchange already flat at close time"))
                    return
                elif exchange_side != self.position.side:
                    print(color(
                        f"{now_str()} [close-qty] WARNING: exchange side ({exchange_side}) does not "
                        f"match local side ({self.position.side}) for {self.symbol} - falling back "
                        f"to local total_qty for this close order (not trusting a side mismatch).",
                        YELLOW,
                    ))
            else:
                print(color(
                    f"{now_str()} [close-qty] could not fetch fresh positionAmt before closing - "
                    f"falling back to local total_qty for this close order.", YELLOW,
                ))

        # 2026-08 stale-decision safety gate, SECOND check (this check
        # only - everything else is unchanged): the guard at function
        # entry above only protects against self.position being swapped
        # BEFORE this call started. self.position could still have been
        # swapped by a concurrent initialize_sync() DURING the
        # _fetch_exchange_position() await just above (the only await
        # between entry and here) - re-verify identity immediately before
        # mutating pending state / submitting the actual order, not just
        # at entry, so that window can never let a stale decision through
        # either. Aborts without touching status/pending_order_id/order
        # index and without ever calling _place_reduce_only_close_order().
        if expected_position is not None and expected_position is not self.position:
            print(color(
                f"{now_str()} [stale-decision-guard] close_position(reason={reason}) skipped "
                f"immediately before submission - self.position was replaced by a concurrent "
                f"sync during the fresh-position fetch; no order submitted, no pending state "
                f"mutated.", YELLOW,
            ))
            return

        label = "EMERGENCY CLOSE" if emergency else "CLOSE (full)"
        print(color(
            f"{now_str()} {label}: {reason} | closing {close_side} {qty} {self.symbol} "
            f"(qty_source={qty_source})",
            RED if emergency else GREEN,
        ))
        self.position.status = "CLOSING"
        self.position.pending_order_ts = time.time()
        self.last_trade_action_ts = time.time()
        self._pending_exit_reason = exit_reason_tag  # consumed in _on_close_filled

        await self._place_reduce_only_close_order(close_side, qty)
        if not DRY_RUN and self.position.pending_order_id is None and self.position.status == "CLOSING":
            # _place_reduce_only_close_order() failed to place the order
            # (already logged) and there's no already-filled replay to have
            # handled it - leave status as OPEN so risk management resumes
            # evaluating this position on the next tick instead of it being
            # stuck in CLOSING with nothing actually pending.
            self.position.status = "OPEN"

    # -- exchange-native protective stop (item 6) ------------------------------

    def _compute_protective_stop_price(
        self, extra_qty: float = 0.0, extra_entry_price: Optional[float] = None,
    ) -> Optional[float]:
        """Inverts estimate_net_pnl_usdt_executable()'s fee model to find
        the STOP_MARKET trigger price at which estimated fee-net PnL first
        reaches the per-trade loss-budget trigger
        (-(MAX_TRADE_NET_LOSS_USDT - MAX_TRADE_EXIT_BUFFER_USDT)) - i.e.
        the exchange-side mirror of the client-side gate in
        _manage_open_position(). Returns None if the budget is disabled or
        the position has no real economics yet to protect. `extra_qty`/
        `extra_entry_price` preview the price a prospective DCA add would
        require, exactly like estimate_net_pnl_usdt_executable()."""
        p = self.position
        if MAX_TRADE_NET_LOSS_USDT <= 0 or not p.avg_entry_price or p.total_qty <= 0:
            return None
        total_qty = p.total_qty + max(extra_qty, 0.0)
        if total_qty <= 0:
            return None
        if extra_qty > 0 and extra_entry_price:
            total_notional = p.avg_entry_price * p.total_qty + extra_entry_price * extra_qty
            avg_entry = total_notional / total_qty
        else:
            avg_entry = p.avg_entry_price

        trigger_net_loss = -(MAX_TRADE_NET_LOSS_USDT - MAX_TRADE_EXIT_BUFFER_USDT)
        if extra_qty <= 0 and self._position_fees_reliable and self._position_fees_accum > 0:
            entry_fees = self._position_fees_accum
        else:
            entry_fees = TAKER_FEE_RATE * (p.total_qty * p.avg_entry_price) + (
                TAKER_FEE_RATE * (extra_qty * extra_entry_price)
                if (extra_qty > 0 and extra_entry_price) else 0.0
            )

        # Solve gross - entry_fees - TAKER_FEE_RATE*total_qty*close == trigger
        # for `close`, where gross is the signed move on `avg_entry`.
        if p.side == "LONG":
            denom = total_qty * (1 - TAKER_FEE_RATE)
            if denom <= 0:
                return None
            stop_price = (trigger_net_loss + entry_fees + avg_entry * total_qty) / denom
        else:
            denom = total_qty * (1 + TAKER_FEE_RATE)
            if denom <= 0:
                return None
            stop_price = (avg_entry * total_qty - trigger_net_loss - entry_fees) / denom

        if stop_price <= 0:
            return None
        # 2026-08 note: round_step() only rounds DOWN (toward zero), which
        # is the tighter/earlier-triggering direction for a SHORT stop
        # (above entry) but the looser/later-triggering direction for a
        # LONG stop (below entry) by at most one tick_size. MAX_TRADE_EXIT_BUFFER_USDT
        # already reserves headroom for slippage/exit-buffer purposes that
        # comfortably absorbs a single tick - not special-cased further.
        return round_step(stop_price, self.filters.tick_size) if self.filters.tick_size else stop_price

    def _new_protective_stop_client_algo_id(self) -> str:
        """2026-08 protective-stop ownership fix (review finding 4):
        generates a unique, bot-owned clientOrderId for a protective stop.
        Always begins with PROTECTIVE_STOP_CLIENT_ID_PREFIX, which is the
        ONLY thing reconciliation uses to decide whether an order resting on
        Binance belongs to this bot - a manual or third-party STOP_MARKET
        never carries it and is therefore never adopted or cancelled.
        Stays inside Binance's ^[\\.A-Za-z0-9_:/-]{1,36}$ charset/length."""
        self._protective_stop_seq += 1
        # Millisecond clock (mod 10 digits ~ 115 days of uniqueness) plus a
        # per-process sequence: unique across replaces within a process and
        # across restarts, without needing any persisted counter.
        return f"{PROTECTIVE_STOP_CLIENT_ID_PREFIX}-{int(time.time() * 1000) % 10_000_000_000}-{self._protective_stop_seq}"[:36]

    def _is_own_protective_stop(self, order: dict) -> bool:
        """True only if `order` (a raw Binance openOrders entry) is a
        protective stop THIS bot placed, proven by its clientOrderId prefix.
        Everything else - including a user's own manually-placed STOP_MARKET
        on the same symbol/side - returns False and must never be adopted,
        replaced, or cancelled by this bot.

        2026-08 strict-ownership hardening: matches on
        PROTECTIVE_STOP_CLIENT_ID_PREFIX + "-" (the exact separator
        _new_protective_stop_client_algo_id() always emits), not the bare
        prefix. A bare-prefix match would also claim an unrelated order whose
        id merely STARTS with the same letters (e.g. prefix "bv2ps" matching
        a third-party "bv2psXYZ" or "bv2pshedge-1"), which this bot must
        never cancel."""
        # 2026-08 Algo-Service migration: algo orders carry `clientAlgoId`.
        # `clientOrderId` is still accepted as a fallback so a legacy
        # pre-migration STOP_MARKET left resting on the plain order endpoint
        # is still recognised as ours and cleaned up rather than orphaned.
        client_id = order.get("clientAlgoId") or order.get("clientOrderId") or ""
        return isinstance(client_id, str) and client_id.startswith(
            f"{PROTECTIVE_STOP_CLIENT_ID_PREFIX}-"
        )

    async def _register_protective_child_order(
        self, actual_order_id, context: str = "",
    ) -> None:
        """2026-08 Algo-Service migration. An algo order never emits
        ORDER_TRADE_UPDATE itself. When it triggers, Binance creates a CHILD
        MARKET order and reports its id as `actualOrderId` (empty string
        until then); THAT child fills and emits ORDER_TRADE_UPDATE.

        This registers the child in _order_index under role
        "protective_stop", so its fill routes through the SAME
        _on_close_filled() bookkeeping every other close uses. Registration
        goes through _register_order_and_replay(), so a FILLED event that
        arrived BEFORE we learned the child's id is replayed from the
        unmatched-fill buffer rather than lost - the child can fill in
        milliseconds, well before any ALGO_UPDATE or REST query tells us its
        id, so that ordering is the common case, not an edge case.

        Idempotent: re-registering an id already in _order_index is a no-op,
        and once the fill is consumed _order_index.pop() prevents any
        duplicate ALGO_UPDATE/REST recovery from double-processing it.
        Accepts "" / None / unparseable values (an untriggered algo) and
        does nothing.
        """
        if actual_order_id in (None, "", 0, "0"):
            return
        try:
            child_id = int(actual_order_id)
        except (TypeError, ValueError):
            return
        p = self.position
        # 2026-08 idempotency guard: only ever wire up a child order while a
        # position is actually open. Once the trade has been finalized the
        # position is reset to FLAT, but a duplicate/late ALGO_UPDATE (e.g. a
        # trailing FINISHED after the fill was already processed) would
        # otherwise re-register the same child id - and
        # _register_order_and_replay() would then replay a buffered duplicate
        # FILLED event through _on_close_filled(), double-counting realized
        # PnL, the trade log and the daily counters. The in-memory status is
        # the authoritative, synchronous signal that this trade is done.
        if p.status not in ("OPEN", "DCA_PENDING", "CLOSING") or p.total_qty <= 0:
            return
        if p.protective_stop_actual_order_id == child_id and child_id in self._order_index:
            return  # already wired up
        p.protective_stop_actual_order_id = child_id
        if child_id in self._order_index:
            return
        print(color(
            f"{now_str()} [protective-stop] algo TRIGGERED -> child order actualOrderId={child_id} "
            f"registered for close bookkeeping ({context}).", CYAN,
        ))
        await self._register_order_and_replay(child_id, "protective_stop")

    async def _resolve_protective_algo_via_rest(self, context: str) -> Optional[dict]:
        """Authoritative REST lookup of the tracked protective algo order.
        Used whenever an ALGO_UPDATE is missed, or arrives in a status that
        cannot be trusted on its own - specifically FINISHED, which per
        Binance's docs may mean either filled OR canceled. Returns the raw
        algo order dict, or None if it could not be determined (caller must
        treat None as 'unknown', never as 'gone')."""
        p = self.position
        if p.protective_stop_algo_id is None and not p.protective_stop_client_algo_id:
            return None
        is_cooldown_active = getattr(self.client, "is_cooldown_active", None)
        if is_cooldown_active is not None and is_cooldown_active():
            return None
        try:
            return await self.client.get_algo_order(
                algo_id=p.protective_stop_algo_id,
                client_algo_id=(
                    p.protective_stop_client_algo_id if p.protective_stop_algo_id is None else None
                ),
            )
        except Exception as e:  # noqa: BLE001 - a status query must never crash the trading loop
            print(color(
                f"{now_str()} [protective-stop] algo status query failed ({context}): {e} - "
                f"treating protection status as UNKNOWN (conservative).", YELLOW,
            ))
            return None

    # 2026-08 fix A: every documented/observed spelling of each ALGO_UPDATE
    # field. Binance's USD-M streams use verbose names in the REST/docs
    # payloads and short names on the wire, and the Algo Service migration
    # is recent enough that both appear in the wild. Ordered most-specific
    # first; the first non-empty match wins.
    # CORRECTED 2026-08-18 against the real LIVE envelope, which this handler's
    # own UNATTRIBUTED diagnostic finally captured once fix A made the failure
    # visible:
    #
    #   envelope_keys=['E','T','e','o']
    #   payload_keys =['R','S','V','X','ai','aid','at','caid','cp','f','gtd',
    #                  'ia','o','p','pP','pm','ps','q','s','tp','tt','wt']
    #
    # Two corrections the live data forced, both of which the first pass got
    # wrong by guessing:
    #
    #   1. The ids are `aid` / `caid` - NOT the `ai` / `cai` originally
    #      guessed. `ai` IS present in the payload but is something else
    #      entirely, so it is deliberately NOT listed: reading an unknown
    #      field as the algoId is exactly the class of guess that caused the
    #      original incident.
    #   2. Status is `X`. The first pass listed `S` ahead of `X`, and `S` is
    #      the SIDE field - so every live event parsed as status="SELL",
    #      matched no status branch, and did nothing. `S` is now removed
    #      entirely; it is never a status under any Binance spelling.
    #
    # Verbose/documented spellings are kept first so a REST payload (which
    # uses algoId/clientAlgoId/algoStatus/actualOrderId) still parses through
    # the same table - _resolve_protective_algo_via_rest() feeds its result
    # straight into _register_protective_child_order().
    _ALGO_FIELD_ALIASES = {
        "algo_id": ("algoId", "algoID", "algo_id", "aid", "strategyId", "si"),
        "client_algo_id": (
            "clientAlgoId", "clientAlgoID", "client_algo_id", "caid", "clientOrderId", "c",
        ),
        "status": (
            "algoStatus", "algo_status", "strategyStatus", "status", "X", "as", "st",
        ),
        "actual_order_id": (
            "actualOrderId", "actualOrderID", "actual_order_id", "aoi", "orderId", "i",
        ),
    }

    def _should_log_algo_envelope_warning(self, min_interval_sec: float = 30.0) -> bool:
        """Throttles the UNATTRIBUTED ALGO_UPDATE diagnostic so a stream of
        third-party algo events can never flood the log, while still making
        sure the condition is never completely invisible again."""
        now = time.time()
        if now - self._last_algo_envelope_warn_ts < min_interval_sec:
            return False
        self._last_algo_envelope_warn_ts = now
        return True

    @classmethod
    def _extract_algo_fields(cls, event: dict):
        """2026-08 fix A. Pulls (payload, algo_id, client_algo_id, status,
        actual_order_id) out of an ALGO_UPDATE event without depending on
        the exact wrapper key or field spelling.

        Strategy: collect every dict in the event (the event itself plus any
        nested dicts, bounded depth so a pathological payload cannot spin),
        score each by how many algo-ish fields it carries, and read the
        fields from the best-scoring one - falling back to a merged view
        across all of them for anything still missing. This is deliberately
        permissive about WHERE the data lives and strict about WHAT is done
        with it: the caller still proves ownership before acting, so a
        loosely-matched third-party payload can never cause an action.

        Returns the payload dict actually used (for diagnostics) plus the
        four fields; every field is None/"" when genuinely absent.
        """
        if not isinstance(event, dict):
            return {}, None, "", "", None

        candidates: List[dict] = []

        def walk(node, depth: int) -> None:
            if depth > 3 or len(candidates) > 24:
                return
            if isinstance(node, dict):
                candidates.append(node)
                for value in node.values():
                    walk(value, depth + 1)
            elif isinstance(node, (list, tuple)):
                for value in node[:8]:
                    walk(value, depth + 1)

        walk(event, 0)

        def read(source: dict, logical: str):
            for name in cls._ALGO_FIELD_ALIASES[logical]:
                if name in source:
                    value = source[name]
                    if value not in (None, ""):
                        return value
            return None

        def score(source: dict) -> int:
            return sum(1 for logical in cls._ALGO_FIELD_ALIASES if read(source, logical) is not None)

        best = max(candidates, key=score, default={})
        if score(best) == 0:
            best = event

        def resolve(logical: str):
            value = read(best, logical)
            if value is not None:
                return value
            # Fall back to any other dict in the event that carries it.
            for source in candidates:
                value = read(source, logical)
                if value is not None:
                    return value
            return None

        algo_id = resolve("algo_id")
        client_algo_id = resolve("client_algo_id") or ""
        raw_status = resolve("status")
        status = str(raw_status).upper() if raw_status is not None else ""
        actual_order_id = resolve("actual_order_id")
        return best, algo_id, client_algo_id, status, actual_order_id

    async def handle_algo_update(self, event: dict) -> None:
        """2026-08 Algo-Service migration: ALGO_UPDATE user-stream handler
        for the exchange-native protective stop.

        Envelope tolerance: the algo payload is read from whichever of the
        documented/observed wrapper keys is present ("ao"/"a"/"o") falling
        back to the top level, because this event's exact envelope key is the
        one part of the migration that could not be verified against live
        traffic while offline. Every field lookup below is by Binance's
        documented NAME (algoId/clientAlgoId/algoStatus/actualOrderId), so an
        envelope difference degrades to 'ignored, resolved later by REST'
        rather than to a wrong action.

        Every branch is conservative and idempotent:
          NEW                     -> track (this is our stop, armed)
          TRIGGERING / TRIGGERED  -> register actualOrderId child if present
          CANCELED/EXPIRED/REJECTED -> proof it is gone; if the position is
                                     still open, re-enter PROTECTION_PENDING
                                     so the sweep re-arms it
          FINISHED                -> NEVER treated as a fill on its own; a
                                     REST query decides filled vs canceled
        """
        # 2026-08 ALGO_UPDATE parse fix (fix A - root cause of the LIVE
        # 18:07:48 incident, where a protective stop filled, closed the
        # position on the exchange, and was never recorded as a trade).
        # The previous parser looked for the payload under exactly three
        # wrapper keys ("ao"/"a"/"o") and only recognised a dict carrying
        # the literal names algoId/algoStatus/clientAlgoId. When the live
        # envelope did not match, `a` silently fell back to the top-level
        # event, every field came back None/"", ownership could not be
        # established, and this method returned BEFORE its own
        # [algo-update] diagnostic - so the failure was completely
        # invisible in the logs (three "ALGO_UPDATE received" lines with
        # zero "[algo-update]" lines is exactly what the incident showed).
        #
        # _extract_algo_fields() below walks the whole event structure and
        # accepts every documented/observed spelling of each field, so an
        # envelope difference can no longer blind this handler. Anything it
        # still cannot parse is now LOGGED (throttled) instead of dropped
        # in silence, and - when a protective stop is actually tracked and
        # a position is open - escalated to an authoritative REST lookup
        # rather than assumed irrelevant.
        a, algo_id, client_algo_id, status, actual_order_id = self._extract_algo_fields(event)
        p = self.position

        # Ownership: only ever act on OUR protective stop. A manual or
        # third-party algo order on the same account must be ignored entirely.
        owned_by_prefix = isinstance(client_algo_id, str) and client_algo_id.startswith(
            f"{PROTECTIVE_STOP_CLIENT_ID_PREFIX}-"
        )
        matches_tracked = (
            algo_id is not None and p.protective_stop_algo_id is not None
            and str(algo_id) == str(p.protective_stop_algo_id)
        ) or (
            bool(client_algo_id) and client_algo_id == p.protective_stop_client_algo_id
        )
        if not owned_by_prefix and not matches_tracked:
            # 2026-08 fix A: NEVER return silently here again. Two distinct
            # cases, and the old code collapsed both into a silent drop:
            #
            #  (1) genuinely someone else's algo order - correct to ignore,
            #      but say so at least occasionally so it is visible;
            #  (2) OUR stop, whose identifying fields this build could not
            #      parse out of the envelope. That is the incident case. If
            #      we are tracking a protective stop on an open position,
            #      an unattributable ALGO_UPDATE is treated as a possible
            #      state change on OUR order and resolved authoritatively
            #      over REST - which returns the real algoStatus and
            #      actualOrderId regardless of the stream envelope's shape.
            tracking_a_stop = (
                p.protective_stop_algo_id is not None or bool(p.protective_stop_client_algo_id)
            )
            position_live = p.status in ("OPEN", "DCA_PENDING", "CLOSING") and p.total_qty > 0
            unparsed = algo_id is None and not client_algo_id and not status
            if self._should_log_algo_envelope_warning():
                print(color(
                    f"{now_str()} [algo-update] UNATTRIBUTED ALGO_UPDATE "
                    f"(algoId={algo_id or '-'} clientAlgoId={client_algo_id or '-'} "
                    f"status={status or 'UNKNOWN'} actualOrderId={actual_order_id or '-'} "
                    f"envelope_keys={sorted(event.keys()) if isinstance(event, dict) else type(event).__name__} "
                    f"parsed_payload_keys={sorted(a.keys()) if isinstance(a, dict) else '-'}) - "
                    f"{'fields could not be parsed from this envelope' if unparsed else 'does not match our tracked stop'}; "
                    f"tracking_a_stop={tracking_a_stop} position_live={position_live}.",
                    YELLOW,
                ))
            if unparsed and tracking_a_stop and position_live:
                info = await self._resolve_protective_algo_via_rest(
                    context="unparsed ALGO_UPDATE envelope"
                )
                if info:
                    rest_status = str(
                        info.get("algoStatus") or info.get("status") or ""
                    ).upper()
                    print(color(
                        f"{now_str()} [algo-update] resolved unparsed envelope over REST: "
                        f"algoStatus={rest_status or 'UNKNOWN'} "
                        f"actualOrderId={info.get('actualOrderId') or '-'}.", CYAN,
                    ))
                    await self._register_protective_child_order(
                        info.get("actualOrderId"), context="REST after unparsed ALGO_UPDATE",
                    )
            return

        print(color(
            f"{now_str()} [algo-update] algoId={algo_id} clientAlgoId={client_algo_id} "
            f"status={status or 'UNKNOWN'} actualOrderId={actual_order_id or '-'}", GRAY,
        ))

        if status == "NEW":
            # Our stop is armed on the exchange. Adopt its ids if this
            # process did not already have them (e.g. event raced the REST
            # response), and clear PROTECTION_PENDING only for OUR order.
            if matches_tracked or p.protective_stop_algo_id is None:
                if algo_id is not None:
                    try:
                        p.protective_stop_algo_id = int(algo_id)
                    except (TypeError, ValueError):
                        pass
                if client_algo_id:
                    p.protective_stop_client_algo_id = client_algo_id
                p.protective_stop_cancel_pending = False
                if p.status in ("OPEN", "DCA_PENDING"):
                    self._clear_protection_pending()
            return

        if status in ("TRIGGERING", "TRIGGERED"):
            # The stop fired. The child MARKET order is what actually closes
            # the position - wire it up so its fill is processed exactly once.
            await self._register_protective_child_order(
                actual_order_id, context=f"ALGO_UPDATE {status}",
            )
            if not actual_order_id:
                # Triggered but the child id was not in this event - ask REST
                # rather than waiting and risking an unrouted fill.
                info = await self._resolve_protective_algo_via_rest(context=f"ALGO_UPDATE {status}")
                if info:
                    await self._register_protective_child_order(
                        info.get("actualOrderId"), context="REST after TRIGGERED",
                    )
            return

        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            # Proof the algo order is gone. Clear tracking, and if the
            # position is still open it is now UNPROTECTED - re-enter
            # PROTECTION_PENDING so the existing throttled sweep re-arms it
            # (and the bounded fail-safe still applies).
            if matches_tracked:
                self._clear_protective_stop_tracking()
                if p.status in ("OPEN", "DCA_PENDING") and p.total_qty > 0:
                    self._mark_protection_pending(
                        f"protective algo stop {status} on the exchange - re-arming"
                    )
                    print(color(
                        f"{now_str()} [protective-stop] *** HIGH SEVERITY *** algo stop {status} "
                        f"while the position is still OPEN - PROTECTION_PENDING re-entered; the "
                        f"sweep will re-arm it (new DCA blocked meanwhile).", RED,
                    ))
            return

        if status == "FINISHED":
            # Binance documents FINISHED as ambiguous: it can mean the algo
            # filled OR that it was canceled. Never finalize a trade on this
            # alone - resolve it against the algo record and the real
            # exchange position.
            info = await self._resolve_protective_algo_via_rest(context="ALGO_UPDATE FINISHED")
            child = (info or {}).get("actualOrderId") or actual_order_id
            if child:
                await self._register_protective_child_order(
                    child, context="ALGO_UPDATE FINISHED (verified)",
                )
                return
            # No child id anywhere -> it did not trigger, so FINISHED means
            # canceled. Treat exactly like CANCELED.
            print(color(
                f"{now_str()} [protective-stop] algo FINISHED with no actualOrderId - this is a "
                f"CANCELED outcome, not a fill; not finalizing any trade.", YELLOW,
            ))
            if matches_tracked:
                self._clear_protective_stop_tracking()
                if p.status in ("OPEN", "DCA_PENDING") and p.total_qty > 0:
                    self._mark_protection_pending(
                        "protective algo stop FINISHED/canceled without triggering - re-arming"
                    )
            return

    def _clear_protective_stop_tracking(self) -> None:
        """Clears every locally-tracked protective-stop identifier. Used only
        where the order is PROVEN gone from the exchange."""
        p = self.position
        p.protective_stop_algo_id = None
        p.protective_stop_price = None
        p.protective_stop_client_algo_id = None
        p.protective_stop_actual_order_id = None
        p.protective_stop_cancel_pending = False

    async def _cancel_protective_stop(self, reason: str) -> bool:
        """Cancels the currently-tracked protective stop order and reports
        whether the order is now PROVABLY gone from Binance.

        2026-08 cancel-confirmation fix (review finding 5): local tracking
        (protective_stop_algo_id/price/client_order_id) is now cleared ONLY
        when cancellation is confirmed - either the cancel call succeeded, or
        Binance answered -2011 "Unknown order sent" which proves the order no
        longer exists (already triggered/expired/manually cancelled). On any
        indeterminate failure (network error, timeout, REST cooldown,
        unexpected API error) the order may STILL be resting on the exchange,
        so its id is deliberately retained and protective_stop_cancel_pending
        is set; the throttled sweep in _manage_open_position() retries until
        it is resolved. Previously this cleared the id up-front, which could
        leave a real resting order orphaned with the bot unable to name it.

        Returns True if the order is confirmed gone, False if it may still be
        resting (caller must not assume the exchange side is clean).
        """
        p = self.position
        algo_id = p.protective_stop_algo_id
        if algo_id is None:
            return True
        if DRY_RUN:
            p.protective_stop_algo_id = None
            p.protective_stop_price = None
            p.protective_stop_client_algo_id = None
            p.protective_stop_actual_order_id = None
            p.protective_stop_cancel_pending = False
            print(color(
                f"{now_str()} [DRY RUN] would cancel protective algo stop algoId={algo_id} ({reason})", GRAY,
            ))
            return True

        # A cancel is a REST request like any other: while a 418/429 cooldown
        # is armed, exchange.py's _request() would refuse to send it anyway
        # (raising a synthetic 429). Detect that up-front so the order id is
        # retained and retried rather than burning a confusing error log.
        is_cooldown_active = getattr(self.client, "is_cooldown_active", None)
        if is_cooldown_active is not None and is_cooldown_active():
            p.protective_stop_cancel_pending = True
            if self._should_log_protection_pending():
                print(color(
                    f"{now_str()} [protective-stop] cancel algoId={algo_id} deferred - REST "
                    f"cooldown active ({reason}); keeping the tracked id so it can be cancelled "
                    f"once cooldown clears (no orphan).", YELLOW,
                ))
            return False

        def _confirmed_gone() -> None:
            p.protective_stop_algo_id = None
            p.protective_stop_price = None
            p.protective_stop_client_algo_id = None
            p.protective_stop_actual_order_id = None
            p.protective_stop_cancel_pending = False

        try:
            # 2026-08 Algo-Service migration: DELETE /fapi/v1/algoOrder, keyed
            # by algoId (no symbol param - the algo id alone identifies it).
            await self.client.cancel_algo_order(algo_id=algo_id)
            _confirmed_gone()
            print(color(f"{now_str()} [protective-stop] cancelled algoId={algo_id} ({reason})", GRAY))
            return True
        except BinanceApiError as e:
            if e.code == -2011:  # "Unknown order sent" - Binance PROVES it is gone
                _confirmed_gone()
                print(color(
                    f"{now_str()} [protective-stop] cancel algoId={algo_id} - already gone "
                    f"(likely triggered or expired) ({reason})", GRAY,
                ))
                return True
            p.protective_stop_cancel_pending = True
            print(color(
                f"{now_str()} [protective-stop] cancel algoId={algo_id} FAILED: {e} ({reason}) - "
                f"order may STILL be resting; keeping the tracked id and retrying on the "
                f"protective-stop sweep.", YELLOW,
            ))
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            p.protective_stop_cancel_pending = True
            print(color(
                f"{now_str()} [protective-stop] cancel algoId={algo_id} network error: {e} ({reason}) - "
                f"order may STILL be resting; keeping the tracked id and retrying on the "
                f"protective-stop sweep.", YELLOW,
            ))
            return False
        except Exception as e:  # noqa: BLE001 - a cancel attempt must never crash the trading loop
            p.protective_stop_cancel_pending = True
            print(color(
                f"{now_str()} [protective-stop] cancel algoId={algo_id} unexpected error: {e} "
                f"({reason}) - order may STILL be resting; keeping the tracked id and retrying "
                f"on the protective-stop sweep.", YELLOW,
            ))
            return False

    async def _place_or_replace_protective_stop(self, reason: str) -> None:
        """Places (or cancels-and-replaces) a server-side STOP_MARKET
        closePosition=true protective stop for the current position, sized
        from the per-trade loss budget (MAX_TRADE_NET_LOSS_USDT /
        MAX_TRADE_EXIT_BUFFER_USDT - same inputs as the client-side gate in
        _manage_open_position). closePosition=true is used deliberately
        instead of a fixed reduceOnly quantity: per Binance USD-M Futures
        docs this order type always closes the ENTIRE current position,
        cannot reverse/increase it, and needs no quantity bookkeeping
        across DCA adds (a fixed-quantity reduceOnly stop would go stale -
        and either under- or over-close - the instant a DCA changes
        total_qty). No-op if PROTECTIVE_STOP_ENABLED is False,
        MAX_TRADE_NET_LOSS_USDT<=0 (budget disabled), or the position has
        no real economics yet.

        Cancel-then-replace race window (accepted risk - see the task's
        "explain the selected replace sequence and remaining risk"
        requirement): Binance has no atomic "amend stopPrice in place" for
        this order type - the only way to move it is cancel + place-new,
        two separate requests, so there is a real (normally sub-second,
        REST-latency-bound) window with NO protective stop resting on the
        exchange between them. The alternative (place-new-before
        cancelling-old) was rejected: two closePosition=true STOP_MARKET
        orders on the same side/symbol resting simultaneously in One-Way
        Mode risk Binance rejecting the second as a duplicate/conflicting
        conditional order, which would leave the STALE stop as the only
        one resting - worse than a short gap with none. This gap is
        mitigated, not eliminated, by the fact that MAX_TRADE_NET_LOSS_USDT
        is independently evaluated client-side on every price tick and is
        NOT dependent on this order existing - this protective stop exists
        specifically for when the client-side check cannot run at all
        (REST cooldown/ban, process restart/outage). If placement fails,
        the position enters PROTECTION_PENDING (new DCA blocked, existing
        risk-reducing exits unaffected) rather than continuing silently
        unprotected.
        """
        p = self.position
        if not PROTECTIVE_STOP_ENABLED or MAX_TRADE_NET_LOSS_USDT <= 0:
            return
        if p.status != "OPEN" or not p.avg_entry_price or p.total_qty <= 0 or p.side not in ("LONG", "SHORT"):
            return

        stop_price = self._compute_protective_stop_price()
        if stop_price is None:
            return

        # 2026-08 startup-reconciliation safety: never place a protective
        # stop while this process has not been able to enumerate Binance's
        # open orders. A bot-owned stop could already be resting from a
        # previous process (that is exactly what startup reconciliation
        # exists to find), and placing blind would leave TWO
        # closePosition=true STOP_MARKETs on the same position. Stay
        # PROTECTION_PENDING (DCA blocked, client-side exits fully active)
        # and let the sweep retry reconciliation first.
        if self._protective_stop_reconcile_blocked:
            self._mark_protection_pending(
                f"open-order reconciliation has not succeeded yet - not placing a protective stop "
                f"blind ({reason})"
            )
            if self._should_log_protection_pending():
                print(color(
                    f"{now_str()} [protective-stop] placement withheld - open orders could not be "
                    f"enumerated, so a previously-placed bot-owned stop may still be resting; "
                    f"retrying reconciliation before placing anything ({reason}).", YELLOW,
                ))
            return

        # 2026-08 cooldown-scope fix (review finding 6): a placement attempt
        # during an armed 418/429 cooldown cannot succeed - exchange.py's
        # _request() refuses to send it and raises a synthetic 429 - so
        # attempting one only produces a HIGH SEVERITY failure log on every
        # caller tick. Mark the position PROTECTION_PENDING (so DCA stays
        # blocked and the retry/fail-safe clock in _manage_open_position()
        # runs) and return without touching REST at all.
        is_cooldown_active = getattr(self.client, "is_cooldown_active", None)
        if is_cooldown_active is not None and is_cooldown_active():
            self._mark_protection_pending(f"REST cooldown active - placement deferred ({reason})")
            if self._should_log_protection_pending():
                print(color(
                    f"{now_str()} [protective-stop] placement deferred - REST cooldown active "
                    f"({reason}); position is PROTECTION_PENDING (new DCA blocked) and placement "
                    f"will be retried once cooldown clears.", YELLOW,
                ))
            return

        # 2026-08 cancel-confirmation fix (review finding 5): if the old
        # order could NOT be confirmed cancelled, do not place a second
        # protective stop on top of it - two resting closePosition=true
        # STOP_MARKETs is exactly the duplicate-order state this sequence
        # exists to avoid. Leave the tracked id in place and let the sweep
        # retry the cancel first.
        if p.protective_stop_algo_id is not None:
            confirmed_gone = await self._cancel_protective_stop(reason=f"replacing before: {reason}")
            if not confirmed_gone:
                self._mark_protection_pending(
                    f"stale protective stop algoId={p.protective_stop_algo_id} could not be "
                    f"confirmed cancelled - not placing a replacement on top of it ({reason})"
                )
                return

        close_side = "SELL" if p.side == "LONG" else "BUY"
        client_algo_id = self._new_protective_stop_client_algo_id()

        if DRY_RUN:
            fake_id = -(int(time.time() * 1000) % 1_000_000) - 800000
            p.protective_stop_algo_id = fake_id
            p.protective_stop_price = stop_price
            p.protective_stop_client_algo_id = client_algo_id
            p.protective_stop_actual_order_id = None
            p.protective_stop_cancel_pending = False
            self._clear_protection_pending()
            print(color(
                f"{now_str()} [DRY RUN] would place PROTECTIVE ALGO STOP {close_side} "
                f"algoType=CONDITIONAL type=STOP_MARKET closePosition=true "
                f"triggerPrice={stop_price:.4f} {self.symbol} "
                f"clientAlgoId={client_algo_id} ({reason})", GRAY,
            ))
            return

        try:
            # 2026-08 Algo-Service migration (root cause of live -4120):
            # conditional types MUST go to POST /fapi/v1/algoOrder, with
            # Binance's algo field names - triggerPrice (not stopPrice) and
            # clientAlgoId (not newClientOrderId). `quantity`/`reduceOnly` are
            # deliberately NOT sent: Binance rejects them alongside
            # closePosition="true", which is itself reduce-only by
            # construction and always closes the ENTIRE current position.
            resp = await self.client.place_algo_order(
                symbol=self.symbol, side=close_side,
                algoType="CONDITIONAL", type="STOP_MARKET",
                triggerPrice=f"{stop_price:.8f}", closePosition="true",
                workingType=PROTECTIVE_STOP_WORKING_TYPE,
                clientAlgoId=client_algo_id,
            )
            algo_id = resp.get("algoId")
            if algo_id is None:
                raise BinanceApiError(
                    400, {"code": -1, "msg": f"algoOrder response missing algoId: {resp}"}
                )
            p.protective_stop_algo_id = algo_id
            p.protective_stop_price = stop_price
            p.protective_stop_client_algo_id = client_algo_id
            p.protective_stop_actual_order_id = None
            p.protective_stop_cancel_pending = False
            self._clear_protection_pending()
            print(color(
                f"{now_str()} [protective-stop] PLACED ALGO {close_side} algoType=CONDITIONAL "
                f"type=STOP_MARKET closePosition=true triggerPrice={stop_price:.4f} "
                f"{self.symbol} algoId={algo_id} clientAlgoId={client_algo_id} "
                f"workingType={PROTECTIVE_STOP_WORKING_TYPE} ({reason})", CYAN,
            ))
            # 2026-08 Algo-Service migration: an ALGO order id is NOT an order
            # id and never appears in ORDER_TRADE_UPDATE, so it is deliberately
            # NOT registered in _order_index here. Registration happens for the
            # CHILD order (actualOrderId) the moment Binance reports it - see
            # _register_protective_child_order(), driven by ALGO_UPDATE or a
            # REST query. If the child's FILLED event beats that registration,
            # the existing unmatched-fill buffer replays it, unchanged.
            asyncio.create_task(self.save_dca_state(reason=f"protective stop placed: {reason}"))
        except Exception as e:  # noqa: BLE001 - order placement must never crash the trading loop (matches _place_reduce_only_close_order's own convention; covers BinanceApiError/aiohttp/timeout and any unexpected error e.g. a missing/None client in a test harness)
            p.protective_stop_algo_id = None
            p.protective_stop_price = None
            p.protective_stop_client_algo_id = None
            p.protective_stop_actual_order_id = None
            p.protective_stop_cancel_pending = False
            self._mark_protection_pending(f"placement failed: {e}")
            print(color(
                f"{now_str()} [protective-stop] *** HIGH SEVERITY *** failed to place protective "
                f"stop for {self.symbol} ({reason}): {e} - position is UNPROTECTED against a REST "
                f"outage/ban; entering PROTECTION_PENDING (new DCA blocked; client-side risk exits "
                f"remain active; placement will be retried).", RED,
            ))
            asyncio.create_task(self.save_dca_state(reason="protective stop placement failed"))

    async def _protective_stop_sweep(self, expected_position: Optional["PositionState"] = None) -> None:
        """2026-08 review findings 3 + 5: the single periodic maintenance
        pass for the exchange-native protective stop, called once per tick
        from _manage_open_position().

        Three responsibilities, in priority order:

        1. **Stale-cancel retry (finding 5).** If a previous cancel could not
           be confirmed (network error/timeout/cooldown), the order id was
           deliberately retained rather than cleared. Retry the cancel here
           until Binance either accepts it or proves the order is gone, so a
           real resting order can never be orphaned under a forgotten id.

        2. **Arm retry (finding 3).** While PROTECTION_PENDING, retry
           placement every PROTECTIVE_STOP_RETRY_SEC. Previously placement was
           only ever attempted on a confirmed entry/DCA fill or at startup -
           and since PROTECTION_PENDING itself blocks new DCA, no further fill
           could occur, so a position that failed to arm stayed unprotected
           for the entire life of the trade with no retry at all.

        3. **Bounded fail-safe (finding 3).** If protection still cannot be
           armed after PROTECTION_PENDING_MAX_SEC of continuous trying, close
           the position (risk-reducing, exit_reason=protection_unavailable)
           rather than leaving a live position indefinitely unprotected
           against the exact REST outage this feature guards. Disabled by
           setting PROTECTION_PENDING_MAX_SEC<=0.

        Retry-storm safety: every branch is time-throttled, and all of them
        are skipped outright while exchange.py's shared 418/429 cooldown is
        armed (a request would be refused locally anyway) - so this adds zero
        REST traffic during a ban and resumes only once it clears.
        """
        p = self.position
        if expected_position is not None and expected_position is not self.position:
            return
        if not PROTECTIVE_STOP_ENABLED or MAX_TRADE_NET_LOSS_USDT <= 0:
            return
        if p.status not in ("OPEN", "DCA_PENDING") or p.total_qty <= 0:
            return

        # Never touch REST while a cooldown/ban is armed (finding 6).
        is_cooldown_active = getattr(self.client, "is_cooldown_active", None)
        if is_cooldown_active is not None and is_cooldown_active():
            return

        now = time.time()

        # (1) stale-cancel retry
        if p.protective_stop_cancel_pending and p.protective_stop_algo_id is not None:
            if now - p.protection_last_retry_ts >= PROTECTIVE_STOP_RETRY_SEC:
                p.protection_last_retry_ts = now
                await self._cancel_protective_stop(reason="stale-cancel sweep retry")
            return

        if not p.protection_pending:
            return

        # (1b) reconciliation retry: while open orders could not be
        # enumerated, placement is deliberately blocked (a bot-owned stop may
        # be resting unseen). Retry the enumeration first - it is what
        # unblocks everything else.
        if self._protective_stop_reconcile_blocked:
            if now - p.protection_last_retry_ts >= PROTECTIVE_STOP_RETRY_SEC:
                p.protection_last_retry_ts = now
                print(color(
                    f"{now_str()} [protective-stop] retrying open-order reconciliation before any "
                    f"placement (blocked since enumeration failed).", YELLOW,
                ))
                await reconcile_protective_stop_on_startup(self.client, self)
            return

        # (2) arm retry
        if now - p.protection_last_retry_ts >= PROTECTIVE_STOP_RETRY_SEC:
            p.protection_last_retry_ts = now
            unprotected_for = now - (p.protection_pending_since or now)
            print(color(
                f"{now_str()} [protective-stop] retrying placement - position has been "
                f"PROTECTION_PENDING for {unprotected_for:.0f}s "
                f"(reason={p.protection_pending_reason or 'unknown'}).", YELLOW,
            ))
            await self._place_or_replace_protective_stop(reason="protection-pending retry")
            if self.position is not p or not p.protection_pending:
                return  # successfully armed (or position swapped) - nothing further

        # (3) bounded fail-safe close
        if PROTECTION_PENDING_MAX_SEC > 0 and p.protection_pending_since is not None:
            unprotected_for = now - p.protection_pending_since
            if unprotected_for >= PROTECTION_PENDING_MAX_SEC:
                print(color(
                    f"{now_str()} [protective-stop] *** HIGH SEVERITY *** FAIL-SAFE: position has "
                    f"been unprotected for {unprotected_for:.0f}s >= "
                    f"{PROTECTION_PENDING_MAX_SEC:.0f}s and a protective stop still cannot be armed "
                    f"(reason={p.protection_pending_reason or 'unknown'}) - closing now rather than "
                    f"leaving a live position exposed to a REST outage indefinitely.", RED,
                ))
                await self.close_position(
                    f"protective stop could not be armed for {unprotected_for:.0f}s "
                    f"({p.protection_pending_reason or 'unknown'}) - bounded fail-safe close",
                    emergency=True, exit_reason_tag="protection_unavailable",
                    expected_position=p,
                )

    def _mark_protection_pending(self, reason: str) -> None:
        """2026-08 PROTECTION_PENDING fail-safe (review finding 3): enters
        (or stays in) the unprotected state, starting the unprotected clock
        exactly once so repeated failures cannot keep resetting it and
        thereby postpone the bounded fail-safe close forever."""
        p = self.position
        p.protection_pending = True
        p.protection_pending_reason = reason
        if p.protection_pending_since is None:
            p.protection_pending_since = time.time()

    def _clear_protection_pending(self) -> None:
        """Clears the unprotected state and its clock - called only when a
        protective stop is confirmed armed on the exchange."""
        p = self.position
        p.protection_pending = False
        p.protection_pending_reason = None
        p.protection_pending_since = None
        p.protection_last_retry_ts = 0.0

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

    async def _apply_partial_close(
        self, qty: float, fill_price: float, dry_run: bool = False,
        actual_rp: Optional[float] = None, actual_fee: Optional[float] = None,
    ) -> None:
        p = self.position
        # 2026-08 realized-PnL/fee-accounting fix: a real (live) partial
        # close fill carries Binance's own realized PnL ("rp") and actual
        # commission ("n"), passed in by handle_order_update() - use those
        # directly (pnl = actual_rp - actual_fee) instead of the estimate,
        # exactly like the full-close path now does. Falls back to the
        # pre-existing estimate_net_pnl_usdt() estimate when actual data
        # isn't available - DRY_RUN (no real fill exists) or the rare case
        # actual_rp is None because this was called from somewhere that
        # genuinely has no fill data.
        pnl_source = "estimated"
        if actual_rp is not None and self._position_fees_reliable:
            pnl = actual_rp - (actual_fee or 0.0)
            pnl_source = "actual"
        else:
            pnl = self.estimate_net_pnl_usdt(fill_price, qty) if fill_price else 0.0
        p.total_qty = max(p.total_qty - qty, 0.0)
        p.partial_tp_done = True
        self.realized_pnl_total += pnl
        # 2026-08 Daily Loss Protection: same value, same accumulation
        # point as realized_pnl_total above - only the DAILY bucket also
        # resets at each UTC day boundary. Logging-only impact elsewhere;
        # this does not change partial-close behavior itself.
        self._maybe_reset_daily_loss_tracker()
        self.daily_realized_pnl += pnl
        if BREAKEVEN_AFTER_PARTIAL:
            p.breakeven_armed = True
            p.breakeven_price = p.avg_entry_price
        print(color(
            f"{now_str()} PARTIAL TP FILLED @ {fill_price:.2f}  qty={qty}  "
            f"pnl={pnl:+.4f} USDT ({pnl_source})  remaining_qty={p.total_qty}  "
            f"breakeven_armed={p.breakeven_armed}", GREEN,
        ))

    # -- open-position management: TP / DCA / hard stop / smart exit / trailing ---

    async def _manage_open_position(self) -> None:
        p = self.position
        # Defense in depth: on_price_tick() only calls this method for an
        # OPEN position, but keep the invariant local too. In particular,
        # never evaluate a timeout close while an entry/DCA/close order is
        # still pending; that order must first be resolved by its exact
        # orderId through WebSocket/REST recovery.
        if p.status != "OPEN":
            return
        avg = p.avg_entry_price
        price = self.current_price
        if avg is None or price is None:
            return

        # 2026-08 invalid-OPEN-state safety gate (this check only - every
        # other line in this function is unchanged): status=="OPEN" is
        # supposed to guarantee a real, manageable position (this is the
        # only call site - see on_price_tick's `elif self.position.status
        # == "OPEN":` gate above), but a corrupted/incomplete restore (the
        # Live incident this addresses: a DCA-state snapshot that restored
        # side/avg_entry_price/dca_step correctly but total_qty=0.0 due to
        # the separate snapshot key-mapping bug - see load_dca_state() in
        # dca2.py) could previously still reach here with economics that
        # make every dollar/percent figure below meaningless: pct_move
        # still computes against avg_entry_price, but
        # estimate_net_pnl_usdt() returns exactly 0.0 for total_qty<=0 (see
        # its own guard), which Max Hold Time V2 then read as "no
        # meaningful loss" and deferred on - a real, and possibly large,
        # unrealized loss silently invisible to every PnL-based decision
        # in this function (TP, Profit Lock, Smart Exit, Max Hold, hard
        # stop distance is unaffected since it's pct-based, but nothing
        # here should be trusted to manage size-dependent risk against a
        # quantity that isn't real). Rather than let TP / Profit Lock /
        # Smart Exit / Hard Stop / Max Hold / DCA reason about a position
        # with no real size, or place any order against it, refuse to
        # manage it at all until an authoritative exchange sync
        # (initialize_sync(), which runs independently of this function on
        # every reconnect/periodic poll/startup) replaces self.position
        # with real economics - normal management resumes automatically
        # the very next tick after that happens, with no special-casing
        # needed here.
        if p.total_qty <= 0 or p.side not in ("LONG", "SHORT") or avg <= 0:
            if self._should_log_invalid_open_state():
                print(color(
                    f"{now_str()} [invalid-open-state] *** MANUAL MANAGEMENT REQUIRED *** "
                    f"status={p.status} side={p.side} avg_entry={avg} total_qty={p.total_qty} - "
                    f"economics are not manageable (missing/zero side, avg_entry, or quantity). "
                    f"No automatic TP/Profit-Lock/Smart-Exit/Hard-Stop/Max-Hold/DCA order will be "
                    f"placed for this position until an authoritative exchange sync restores real "
                    f"position data - if a real position exists on the exchange, it is currently "
                    f"UNPROTECTED by any automated risk management and should be checked manually.",
                    RED,
                ))
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
                expected_position=p,
            )
            return

        # --- Protective-stop sweep: cancel retry + arm retry + fail-safe ----------
        # 2026-08 review findings 3 and 5. Runs every tick, immediately after
        # Hard Stop (which must never be gated by anything) and before every
        # other exit/DCA decision, because an unprotected position is the one
        # state this whole feature exists to prevent. All three branches are
        # throttled and skip entirely while a REST cooldown is armed, so this
        # can never become a retry storm or contribute to a 418 ban.
        await self._protective_stop_sweep(expected_position=p)
        if self.position is not p or p.status not in ("OPEN", "DCA_PENDING"):
            # The fail-safe close (or a concurrent resync) acted - stop
            # reasoning about a position this tick no longer owns.
            return

        # --- Per-trade fee-net loss budget (item 5 - primary behavioral fix) ------
        # Independent of MAX_DAILY_LOSS_USDT (a secondary, whole-day circuit
        # breaker evaluated only at the entry gate) and evaluated BEFORE
        # Profit Lock/TP/Smart-Exit/DCA below, second in priority only to
        # Hard Stop above - risk-reducing, can close the position regardless
        # of Brain/regime/DCA-availability. 2026-08 correction: this gate IS
        # gated behind position_sync_ready, like Max-Hold-V2/DCA/Smart-Exit
        # below - it is provisional-economics-dependent (it reads
        # p.total_qty and accumulated commission via
        # estimate_net_pnl_usdt_executable(), exactly the kind of
        # local-state-derived quantity the Live incident showed can be
        # corrupted/stale across a restart), NOT a simple deterministic
        # pct-of-entry-price check the way Hard Stop above is. Uses the
        # EXECUTABLE bid/ask closing-side price and actual accumulated
        # commission (see estimate_net_pnl_usdt_executable's own docstring
        # for why this is a separate method from estimate_net_pnl_usdt,
        # used by Profit-Lock/diagnostics elsewhere in this function and
        # deliberately left untouched). MAX_TRADE_NET_LOSS_USDT<=0 disables
        # this gate entirely (falls back to Hard Stop / Max Hold Time /
        # Smart Exit only - the previous behavior).
        if MAX_TRADE_NET_LOSS_USDT > 0 and not self.position_sync_ready:
            if self._should_log_sync_not_ready():
                print(color(
                    f"{now_str()} [trade-loss-budget-skip] position_sync_ready=False - the "
                    f"per-trade net-loss budget check is withheld until an authoritative "
                    f"exchange sync confirms this position's real qty/entry/fees; Hard Stop "
                    f"remains active.", YELLOW,
                ))
        elif MAX_TRADE_NET_LOSS_USDT > 0:
            loss_budget_trigger = -(MAX_TRADE_NET_LOSS_USDT - MAX_TRADE_EXIT_BUFFER_USDT)
            est_net_pnl_exec = self.estimate_net_pnl_usdt_executable()
            if est_net_pnl_exec <= loss_budget_trigger:
                p.trade_loss_budget_trigger_pnl = est_net_pnl_exec
                print(color(
                    f"{now_str()} [trade-loss-budget] TRIGGERED: estimated fee-net pnl "
                    f"${est_net_pnl_exec:+.4f} <= trigger ${loss_budget_trigger:+.4f} "
                    f"(budget=${MAX_TRADE_NET_LOSS_USDT:.2f}, buffer=${MAX_TRADE_EXIT_BUFFER_USDT:.2f}) - "
                    f"closing to contain the loss before it grows toward the full budget.", RED,
                ))
                await self.close_position(
                    f"per-trade net-loss budget: estimated fee-net pnl ${est_net_pnl_exec:+.4f} <= "
                    f"-${loss_budget_trigger*-1:.2f} trigger (budget=${MAX_TRADE_NET_LOSS_USDT:.2f}, "
                    f"buffer=${MAX_TRADE_EXIT_BUFFER_USDT:.2f})",
                    emergency=True, exit_reason_tag="max_trade_net_loss",
                    expected_position=p,
                )
                return

        # --- 2026-08 STRICT 1:2 RR - absolute fee-net stop ceiling ---------------
        # An independent ceiling layered ON TOP of the per-trade loss budget
        # directly above (which is unchanged and still fires first at its own
        # buffered trigger whenever the two are configured to their matching
        # defaults). Its job is different: the budget gate is relative to
        # MAX_TRADE_NET_LOSS_USDT, whichever value that is configured to,
        # whereas this enforces the RR envelope's own absolute risk leg -
        # one trade may never lose more than MAX_STOP_LOSS_USD ($0.20)
        # fee-net, which is exactly half of the $0.40 TARGET_PROFIT_USD
        # reward leg enforced further below. Together they are the 1:2.
        #
        # Gated behind position_sync_ready and rr_enforcement_active() for
        # the same reason as the budget gate: it reads provisional
        # local economics (qty + accumulated commission), so it must not act
        # on state an authoritative exchange sync has not confirmed. Hard
        # Stop above is unaffected and remains active regardless.
        if self.rr_enforcement_active() and self.position_sync_ready:
            rr_est_net = self.estimate_net_pnl_usdt_executable()
            if rr_est_net <= -self.rr_stop_loss_usd():
                print(color(
                    f"{now_str()} [rr-stop] TRIGGERED: estimated fee-net pnl "
                    f"${rr_est_net:+.4f} <= -${self.rr_stop_loss_usd():.2f} absolute stop "
                    f"ceiling (1:{self.rr_ratio():.0f} envelope, reward leg "
                    f"${self.rr_target_profit_usd():.2f}) - closing now.", RED,
                ))
                await self.close_position(
                    f"1:{self.rr_ratio():.0f} RR stop: estimated fee-net pnl "
                    f"${rr_est_net:+.4f} reached the ${self.rr_stop_loss_usd():.2f} "
                    f"per-trade risk ceiling",
                    emergency=True, exit_reason_tag="rr_stop_loss",
                    expected_position=p,
                )
                return

        # --- 2026-08 SMART ORDERFLOW EARLY EXIT ----------------------------------
        # If the resting book flips violently AGAINST an open position before
        # the stop is reached, exiting at a micro-loss beats riding it down to
        # the full $0.20 stop. Requires BOTH microstructure signals to have
        # turned (imbalance past SMART_ORDERFLOW_EXIT_IMBALANCE against us AND
        # the 10s aggregated trade delta on the wrong side), and only fires
        # inside the $0.05-$0.10 fee-net micro-loss band - above that band the
        # position is still effectively flat and ordinary chop should not close
        # it; below it, the RR stop ceiling above has already taken over.
        #
        # Independent of, and strictly earlier than, the existing multi-signal
        # Smart Exit V2 further down (which is untouched): this reacts to live
        # orderflow within seconds, where Smart Exit V2 reasons about
        # candle/regime/confidence evidence over a much longer horizon.
        if (
            SMART_ORDERFLOW_EXIT_ENABLED
            and ENABLE_ORDERBOOK_GUARD
            and self.position_sync_ready
            and (time.time() - p.opened_at) >= SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC
        ):
            flow = self.orderflow_snapshot()
            if flow.get("data_available"):
                flow_imbalance = float(flow.get("imbalance", 0.0) or 0.0)
                flow_delta = float(flow.get("trade_delta", 0.0) or 0.0)
                book_flipped = (
                    (p.side == "LONG" and flow_imbalance <= -SMART_ORDERFLOW_EXIT_IMBALANCE)
                    or (p.side == "SHORT" and flow_imbalance >= SMART_ORDERFLOW_EXIT_IMBALANCE)
                )
                flow_against = (
                    (p.side == "LONG" and flow_delta < 0)
                    or (p.side == "SHORT" and flow_delta > 0)
                )
                of_est_net = self.estimate_net_pnl_usdt_executable()
                in_micro_loss_band = (
                    -SMART_ORDERFLOW_EXIT_MAX_LOSS_USD
                    <= of_est_net
                    <= -SMART_ORDERFLOW_EXIT_MIN_LOSS_USD
                )
                if book_flipped and flow_against and in_micro_loss_band:
                    print(color(
                        f"{now_str()} [orderflow-exit] TRIGGERED: book flipped against the "
                        f"{p.side} (imbalance={flow_imbalance:+.4f}, threshold "
                        f"{SMART_ORDERFLOW_EXIT_IMBALANCE:.2f}) with 10s flow delta "
                        f"{flow_delta:+.4f} - exiting at a micro-loss of "
                        f"${of_est_net:+.4f} instead of riding it to the "
                        f"${self.rr_stop_loss_usd():.2f} stop.", YELLOW,
                    ))
                    await self.close_position(
                        f"SMART ORDERFLOW EXIT: orderbook imbalance flipped to "
                        f"{flow_imbalance:+.4f} against the {p.side} with 10s trade delta "
                        f"{flow_delta:+.4f} - closing at a ${of_est_net:+.4f} micro-loss "
                        f"before the stop",
                        exit_reason_tag="orderflow_smart_exit",
                        expected_position=p,
                    )
                    return
                elif book_flipped and flow_against and self._should_log_orderflow_exit():
                    print(color(
                        f"{now_str()} [orderflow-exit] book flipped against the {p.side} "
                        f"(imbalance={flow_imbalance:+.4f}, delta={flow_delta:+.4f}) but "
                        f"fee-net pnl ${of_est_net:+.4f} is outside the micro-loss band "
                        f"[-${SMART_ORDERFLOW_EXIT_MAX_LOSS_USD:.2f}, "
                        f"-${SMART_ORDERFLOW_EXIT_MIN_LOSS_USD:.2f}] - holding; the RR stop "
                        f"ceiling and every other exit remain active.", GRAY,
                    ))

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
                    expected_position=p,
                )
                return

        # --- Profit Lock (new, position-level, DCA-aware) --------------------------
        # Uses the WHOLE POSITION's net unrealized PnL (avg_entry_price /
        # total_qty via estimate_net_pnl_usdt - already DCA-aware, same
        # method the TP checks below use). Independent of held_long_enough
        # and every other gate below: once armed, it protects profit on its
        # own terms and can close the position before Partial TP / TP /
        # Smart Exit / DCA are ever evaluated this tick.
        unrealized_pnl_usdt = self.estimate_net_pnl_usdt(price)

        # 2026-08 Profit Lock debug diagnostics (Issue 2 - logging only, no
        # behavior change: everything below this block, starting with the
        # `if unrealized_pnl_usdt <= 0:` guard, is untouched). Makes the
        # existing fee-adjusted-net-vs-gross-PnL behavior visible, since a
        # position can look profitable on gross price movement alone (e.g.
        # what an exchange UI shows) while actually being flat or negative
        # once round-trip fees are subtracted - which is exactly why
        # Profit Lock may correctly stay inactive even when unrealized PnL
        # "looks" positive. Throttled like every other diagnostic-only log
        # in this file, so an open position doesn't spam this every tick.
        if self._should_log_profit_lock_debug():
            fees_est = self.estimate_round_trip_fee_usdt(p.total_qty, p.avg_entry_price, price)
            gross_pnl_usdt = unrealized_pnl_usdt + fees_est
            if unrealized_pnl_usdt <= 0:
                debug_action = "not activated (net pnl <=0)"
            elif p.profit_lock_active:
                debug_action = "active (monitoring floor)"
            elif unrealized_pnl_usdt >= PROFIT_LOCK_ACTIVATION_USDT:
                debug_action = "activating this tick"
            else:
                debug_action = "not activated (net pnl below threshold)"
            # 2026-08 Profit Lock peak-tracking visibility fix: peak/floor
            # appended to the existing debug line (all prior fields
            # unchanged) so peak-tracking state is visible in the same
            # place as the gross/fee/net breakdown, without needing to
            # cross-reference a separate log line.
            print(color(
                f"{now_str()} [profit-lock-debug] side={p.side} avg_entry={p.avg_entry_price:.2f} "
                f"mark_price={price:.2f} qty={p.total_qty:.6f} "
                f"gross={gross_pnl_usdt:+.4f} fees={fees_est:.4f} net={unrealized_pnl_usdt:+.4f} "
                f"threshold={PROFIT_LOCK_ACTIVATION_USDT:.2f} "
                f"peak={p.peak_unrealized_pnl:+.4f} floor={p.peak_unrealized_pnl * PROFIT_LOCK_RATIO:+.4f} "
                f"profit_lock_active={p.profit_lock_active} action={debug_action}", GRAY,
            ))

            # ================================================================
            # TEMPORARY DIAGNOSTIC - [PROFITLOCK VERIFY] (2026-08 avg_entry_price
            # drift investigation). To be REMOVED once the investigation is
            # concluded. Read-only: fetches Binance's own entryPrice/
            # positionAmt for this symbol and computes the SAME gross/fee/net
            # formulas already used above, once against the local
            # PositionState values and once against the exchange's own
            # values, purely for comparison. Does NOT feed into, gate, or
            # alter the Profit Lock activation decision below, DCA, TP,
            # Smart Exit, Brain, or Risk Engine in any way - this block only
            # ever prints. Skipped in DRY_RUN (nothing real to compare
            # against) and fails silently (logged, not raised) on any fetch
            # error so it can never affect the trading loop.
            # ================================================================
            if not DRY_RUN:
                try:
                    verify_rows = await self.client.get_position_risk(self.symbol)
                    verify_row = next((r for r in verify_rows if r.get("symbol") == self.symbol), None)
                    if verify_row is not None:
                        exchange_entry = float(verify_row.get("entryPrice", 0) or 0)
                        exchange_amt = float(verify_row.get("positionAmt", 0) or 0)
                        exchange_qty = abs(exchange_amt)
                        entry_diff = (
                            (p.avg_entry_price - exchange_entry)
                            if (p.avg_entry_price and exchange_entry) else None
                        )
                        if p.side == "LONG":
                            gross_local = (price - p.avg_entry_price) * p.total_qty if p.avg_entry_price else 0.0
                            gross_exchange = (price - exchange_entry) * exchange_qty if exchange_entry else 0.0
                        else:
                            gross_local = (p.avg_entry_price - price) * p.total_qty if p.avg_entry_price else 0.0
                            gross_exchange = (exchange_entry - price) * exchange_qty if exchange_entry else 0.0
                        fee_est_exchange = (
                            self.estimate_round_trip_fee_usdt(exchange_qty, exchange_entry, price)
                            if exchange_entry else 0.0
                        )
                        net_local = gross_local - fees_est
                        net_exchange = gross_exchange - fee_est_exchange
                        print(color(
                            "[PROFITLOCK VERIFY]\n"
                            f"local_avg_entry={p.avg_entry_price}\n"
                            f"exchange_entry={exchange_entry}\n"
                            f"entry_diff={entry_diff}\n"
                            f"local_qty={p.total_qty}\n"
                            f"exchange_qty={exchange_qty}\n"
                            f"dca_step={p.dca_step}\n"
                            f"gross_local={gross_local:.4f}\n"
                            f"gross_exchange={gross_exchange:.4f}\n"
                            f"fee_est={fees_est:.4f}\n"
                            f"net_local={net_local:.4f}\n"
                            f"net_exchange={net_exchange:.4f}",
                            CYAN,
                        ))
                    else:
                        print(color(
                            f"[PROFITLOCK VERIFY] no positionRisk row found for {self.symbol} "
                            f"(exchange reports flat?) - skipping comparison this tick.", YELLOW,
                        ))
                except Exception as e:  # noqa: BLE001 - a diagnostic fetch must never crash the trading loop
                    print(color(f"[PROFITLOCK VERIFY] fetch failed: {e}", YELLOW))

        # Hard safety guard (root cause of the previous bug: once armed, the
        # lock kept comparing against a stale/positive peak even after PnL
        # dropped to/below zero, which could in theory close a trade that
        # is no longer profitable at all). A losing or breakeven position
        # must NEVER be touched by Profit Lock - skip entirely, every tick,
        # regardless of activation state or any previously tracked peak.
        if unrealized_pnl_usdt <= 0:
            pass
        else:
            if not p.profit_lock_active and unrealized_pnl_usdt >= PROFIT_LOCK_ACTIVATION_USDT:
                p.profit_lock_active = True
                p.peak_unrealized_pnl = unrealized_pnl_usdt
                # 2026-08 Profit Lock diagnostics (logging only - activation/
                # ratio/close behavior below is unchanged): explicitly states
                # peak, protected floor, locked percentage, and current
                # unrealized PnL for debugging, instead of leaving floor/
                # locked-pct implicit in the ratio text.
                print(color(
                    f"{now_str()} [profit-lock] ACTIVATED | unrealized_pnl=${unrealized_pnl_usdt:+.4f} "
                    f"peak=${p.peak_unrealized_pnl:+.4f} "
                    f"floor=${p.peak_unrealized_pnl * PROFIT_LOCK_RATIO:+.4f} "
                    f"locked_pct={PROFIT_LOCK_RATIO*100:.0f}%", GREEN,
                ))
                # 2026-07 DCA-state-recovery fix: persist immediately so a
                # restart/redeploy right after activation doesn't restore a
                # stale snapshot with profit_lock_active still False - see
                # module docstring.
                self._last_dca_state_peak_saved = p.peak_unrealized_pnl
                asyncio.create_task(self.save_dca_state(reason="profit lock activated"))
            if p.profit_lock_active:
                # 2026-08 Profit Lock peak-tracking visibility fix (logging
                # only - the max() assignment itself, the persistence
                # trigger/threshold below, and the existing [profit-lock]
                # PEAK UPDATED log are all untouched): captures old_peak
                # BEFORE it's overwritten, so this new, separately-throttled
                # log can report every genuine peak increase (net_pnl >
                # stored peak) rather than only the ones large enough to
                # cross DCA_STATE_PEAK_SAVE_MIN_DELTA_USDT below.
                old_peak = p.peak_unrealized_pnl
                p.peak_unrealized_pnl = max(p.peak_unrealized_pnl, unrealized_pnl_usdt)
                if p.peak_unrealized_pnl > old_peak and self._should_log_profit_lock_peak_update():
                    print(color(
                        f"{now_str()} [profit-lock-peak] UPDATED | "
                        f"old_peak=${old_peak:+.4f} | "
                        f"new_peak=${p.peak_unrealized_pnl:+.4f} | "
                        f"new_floor=${p.peak_unrealized_pnl * PROFIT_LOCK_RATIO:+.4f} | "
                        f"locked_pct={PROFIT_LOCK_RATIO*100:.0f}%", GRAY,
                    ))
                # 2026-07 DCA-state-recovery fix: re-persist whenever the
                # tracked peak has grown meaningfully since the last save,
                # so a restart/redeploy mid-trade recovers a peak close to
                # the real one instead of whatever was last saved on an
                # entry/DCA fill. Throttled (DCA_STATE_PEAK_SAVE_MIN_DELTA_USDT)
                # so a long-running profitable trade doesn't write to disk/
                # GitHub on every single tick.
                if (p.peak_unrealized_pnl - self._last_dca_state_peak_saved) >= DCA_STATE_PEAK_SAVE_MIN_DELTA_USDT:
                    self._last_dca_state_peak_saved = p.peak_unrealized_pnl
                    asyncio.create_task(self.save_dca_state(reason="profit lock peak updated"))
                    # 2026-08 Profit Lock diagnostics (logging only): same
                    # throttle as the persistence trigger above, so this
                    # doesn't add per-tick log spam - purely for debugging,
                    # does not affect activation/ratio/close behavior.
                    print(color(
                        f"{now_str()} [profit-lock] PEAK UPDATED | unrealized_pnl=${unrealized_pnl_usdt:+.4f} "
                        f"peak=${p.peak_unrealized_pnl:+.4f} "
                        f"floor=${p.peak_unrealized_pnl * PROFIT_LOCK_RATIO:+.4f} "
                        f"locked_pct={PROFIT_LOCK_RATIO*100:.0f}%", GRAY,
                    ))
                locked_profit = p.peak_unrealized_pnl * PROFIT_LOCK_RATIO
                # 2026-08 fee/slippage-safe Profit Lock fix: previously
                # triggered the moment unrealized_pnl_usdt dropped to/below
                # locked_profit, even when that margin was only a few cents
                # above zero (e.g. peak just barely cleared
                # PROFIT_LOCK_ACTIVATION_USDT=0.10, so locked_profit could be
                # as low as ~0.05). estimate_net_pnl_usdt() is already
                # fee-aware, but it's evaluated against the current mark
                # price at decision time - by the time close_position()
                # actually places and fills the market order, ordinary
                # execution slippage can erase a margin that thin, closing
                # at a REALIZED net loss despite the decision being
                # estimated net-positive (confirmed in trades_log.csv:
                # exit_reason=profit_lock rows with negative net_pnl_usdt).
                # Requiring the same fee-aware floor already used by
                # TP/Partial TP below (MIN_NET_PROFIT_USDT) leaves a buffer
                # for exactly that slippage. Activation, peak tracking, the
                # 50% ratio, and every other exit's logic are unchanged; if
                # this additional floor isn't met, Profit Lock simply holds
                # for this tick instead of closing - Hard Stop/Smart Exit
                # remain fully active as the risk backstop either way.
                if unrealized_pnl_usdt <= locked_profit and unrealized_pnl_usdt >= MIN_NET_PROFIT_USDT:
                    await self.close_position(
                        f"PROFIT LOCK: unrealized pnl ${unrealized_pnl_usdt:+.4f} fell to/below "
                        f"locked level ${locked_profit:+.4f} (peak=${p.peak_unrealized_pnl:+.4f}, "
                        f"ratio={PROFIT_LOCK_RATIO*100:.0f}%, fee-safe floor=${MIN_NET_PROFIT_USDT:.4f})",
                        exit_reason_tag="profit_lock",
                        expected_position=p,
                    )
                    return

        # --- Max Hold Time Protection (scalping-bot safety net) --------------------
        # This is a scalping bot; positions are meant to resolve in minutes,
        # not hours. If TP/Smart-Exit/DCA/Profit-Lock all keep passing on a
        # position because the market is dead/ranging, this is a backstop
        # so it never silently holds for many hours. Deliberately does NOT
        # blindly close a profitable trade:
        #   - if Profit Lock is already active, it is already protecting
        #     this trade's profit on its own terms - deferred to, below the
        #     hard cap.
        #   - if the position is currently net-profitable AND the market is
        #     still genuinely trending (STRONG_TREND/WEAK_TREND), it's given
        #     more room to keep working rather than being cut off mid-trend.
        #   - otherwise (flat/losing, or a dead/ranging/choppy regime), it
        #     is closed once MAX_HOLD_TIME_SEC is reached.
        # MAX_HOLD_TIME_HARD_CAP_SEC is an unconditional absolute ceiling
        # that always closes the position regardless of PnL/regime/Profit-
        # Lock state, so total holding time is always bounded even for a
        # genuinely-still-trending winner.
        #
        # 2026-08 Max Hold Time V2 (isolated to this block only - Smart
        # Exit, DCA, TP, Profit Lock, Brain, Risk Engine, and every other
        # exit path are untouched): timeout on a genuinely significant loss
        # is no longer an unconditional force-close. It's now an "emergency
        # review" - the same style of cheap, independent recovery-risk
        # signals already used by the final-DCA gate (trend_against,
        # high_risk, momentum_against, extreme_volatility), plus whether a
        # DCA step is still available, are evaluated. The position is only
        # force-closed if at least MAX_HOLD_TIME_RECOVERY_MIN_AGREE of
        # those 5 agree (genuine low probability of recovery) - otherwise
        # it's kept open for another check cycle. A loss smaller than
        # MAX_HOLD_TIME_SMALL_LOSS_PCT skips this review entirely and
        # closes normally, same as a profitable/flat position. The
        # unconditional MAX_HOLD_TIME_HARD_CAP_SEC ceiling below is
        # completely unchanged - a deferred loser can never be held past it.
        held_sec_so_far = time.time() - p.opened_at
        # 2026-08 DCA-aware Max Hold Time tuning (this line only - every
        # other part of this block, including the hard cap below, is
        # unchanged): a position that has DCA'd at least once already ties
        # up more capital than a fresh dca_step==0 position, so it gets a
        # shorter soft timeout before this emergency-review logic starts
        # evaluating it. dca_step==0 positions are completely unaffected -
        # effective_max_hold_sec equals MAX_HOLD_TIME_SEC exactly, same as
        # before this change.
        effective_max_hold_sec = (
            MAX_HOLD_TIME_SEC * MAX_HOLD_TIME_DCA_MULTIPLIER if p.dca_step >= 1 else MAX_HOLD_TIME_SEC
        )
        # 2026-08 Option B - DCA time-eligibility gate (this variable only -
        # MAX_HOLD_TIME_SEC/MAX_HOLD_TIME_HARD_CAP_SEC/MAX_HOLD_TIME_DCA_MULTIPLIER/
        # MAX_HOLD_TIME_RECOVERY_MIN_AGREE, the recovery-risk signal set, DCA
        # sizing/spacing/distance, TP, Profit Lock, Smart Exit, and Hard Stop
        # are all untouched). Single source of truth for "can a NEW DCA still
        # be placed on this tick", shared by BOTH (a) the dca_opportunity_available
        # signal in the emergency review just below and (b) the actual DCA
        # placement gate further down in this same function - so the two can
        # never disagree about DCA eligibility. Computed unconditionally,
        # every tick, from the exact same held_sec_so_far/effective_max_hold_sec
        # already computed above for the threshold check itself.
        dca_time_eligible = not (MAX_HOLD_TIME_ENABLED and held_sec_so_far >= effective_max_hold_sec)
        # 2026-08 position_sync_ready gate (this outer branch only - every
        # line of the existing Max Hold V2 review logic inside the
        # unchanged `elif` below is untouched): the review's own
        # "no meaningful loss" / recovery-vote decisions are provisional-
        # economics-dependent (this is exactly the mechanism the Live
        # incident exploited via a corrupted qty=0 restore), so they must
        # not run at all until initialize_sync() has authoritatively
        # reconciled local state against Binance. Hard Stop / Profit Lock
        # / TP / Trailing / Breakeven above and below this block are
        # unaffected - they remain simple, deterministic, already
        # qty-safe reduceOnly exits regardless of this flag.
        if not self.position_sync_ready:
            if (
                MAX_HOLD_TIME_ENABLED and held_sec_so_far >= effective_max_hold_sec
                and self._should_log_sync_not_ready()
            ):
                print(color(
                    f"{now_str()} [max-hold-skip] position_sync_ready=False - Max Hold Time V2's "
                    f"review (including its own hard cap) is withheld until an authoritative "
                    f"exchange sync confirms this position's real economics; simple reduceOnly "
                    f"exits (Hard Stop/Profit Lock/TP/Trailing) remain active.", YELLOW,
                ))
        elif MAX_HOLD_TIME_ENABLED and held_sec_so_far >= effective_max_hold_sec:
            hard_cap_hit = held_sec_so_far >= MAX_HOLD_TIME_HARD_CAP_SEC
            trending_and_profitable = (
                unrealized_pnl_usdt > 0
                and self.last_regime.regime in (REGIME_STRONG_TREND, REGIME_WEAK_TREND)
            )
            # 2026-08 DCA loss-deferral rollback: once a position has used
            # any DCA step, the existing DCA-aware soft timeout is a hard
            # exposure boundary. The previous recovery vote could keep a
            # fully-sized DCA position open until the 8h hard cap when a
            # slow adverse drift did not trip two fast market-risk signals.
            # That exact failure was observed live (2/2 DCA, meaningful
            # fee-net loss, 0/2 signals, repeated DEFER). Close at the
            # already-configured effective timeout instead. No timeout,
            # DCA distance/size, TP, Profit Lock, Smart Exit, Hard Stop, or
            # Brain threshold is changed here.
            if p.dca_step >= 1:
                close_decision_reason = (
                    "hard cap reached" if hard_cap_hit else "DCA soft timeout reached"
                )
                print(color(
                    f"{now_str()} [max-hold-v2] close decision: "
                    f"reason={close_decision_reason} "
                    f"dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                    f"held={held_sec_so_far/3600:.2f}h "
                    f"effective_limit={effective_max_hold_sec/3600:.2f}h "
                    f"fee_net_pnl_usdt={unrealized_pnl_usdt:+.4f}",
                    YELLOW,
                ))
                await self.close_position(
                    f"max hold time reached for DCA position: "
                    f"{held_sec_so_far/3600:.2f}h >= {effective_max_hold_sec/3600:.2f}h "
                    f"(dca_step={p.dca_step}, hard_cap={hard_cap_hit}, "
                    f"regime={self.last_regime.regime}, "
                    f"unrealized_pnl=${unrealized_pnl_usdt:+.4f}) - "
                    f"closing at the DCA-aware timeout instead of deferring on recovery votes",
                    emergency=True,
                    exit_reason_tag="max_hold_time",
                    expected_position=p,
                )
                return
            # 2026-08 Max Hold Time V2 fee-net meaningful_loss fix (this
            # line only - MAX_HOLD_TIME_SMALL_LOSS_PCT itself, the
            # recovery-risk signals below, DCA sizing/spacing, TP, Profit
            # Lock, Smart Exit, Hard Stop, and Brain are all untouched):
            # previously the sign check (unrealized_pnl_usdt < 0) was
            # fee-net but the magnitude check (abs(pct_move)) was raw
            # price-only - an inconsistent basis. Confirmed via forensic
            # trace against real trades: a position whose RAW price move
            # was under the 0.15% "too small to bother reviewing" bar but
            # whose FEE-NET loss was already meaningfully above it (e.g.
            # 0.106% raw vs 0.186% fee-net, 0.140% raw vs 0.220% fee-net)
            # was misclassified as trivial and closed with ZERO recovery
            # review - not a failed review, no review at all. Now both the
            # sign and magnitude checks use the same fee-net basis, reusing
            # the exact invested_notional/safe_div pattern already used
            # elsewhere in this file (e.g. _on_close_filled()) rather than
            # inventing a new formula.
            invested_notional_for_loss_check = sum(price * qty for price, qty in p.entries)
            fee_net_pnl_pct = safe_div(unrealized_pnl_usdt, invested_notional_for_loss_check, 0.0)
            meaningful_loss = unrealized_pnl_usdt < 0 and fee_net_pnl_pct < -MAX_HOLD_TIME_SMALL_LOSS_PCT

            # 2026-08 Max Hold Time V2 DCA-awareness fix (isolated to this
            # still_deferring expression and this diagnostic log only - the
            # DCA block itself, Smart Exit, TP, Profit Lock, Brain, Entry
            # scoring, and Risk Engine are all untouched): previously, Max
            # Hold Time V2 was evaluated strictly before the DCA block
            # further down in this same function, so a position could be
            # force-closed by timeout on the very tick price first reached
            # the DCA trigger distance - closing it before DCA ever got a
            # chance to average down and improve recovery odds. Reuses the
            # SAME dca_distance_pct formula the DCA block itself uses
            # (self.get_dynamic_dca_distance_pct() - no second/parallel
            # distance calculation) to check whether a DCA add is currently
            # available and actionable. If so, this tick defers regardless
            # of the recovery-risk review below, letting execution continue
            # to the DCA block after this function returns from this
            # check - the DCA block's own dca_step >= MAX_DCA_STEPS /
            # max_dca_exhausted close path is untouched, so exit-reason
            # attribution (max_hold_time vs max_dca_exhausted) is
            # unaffected. This can never defer forever: once DCA fires,
            # dca_step increments and avg_entry_price shifts, so this
            # exact condition is re-evaluated fresh (and eventually
            # dca_exhausted) on the next cycle; if DCA is unavailable or
            # price hasn't reached the trigger, this is simply False and
            # the existing recovery-risk review below decides as before.
            # Hard cap always wins regardless (`not hard_cap_hit` gate).
            current_loss_distance = max(0.0, -pct_move)
            dca_distance_pct_now = self.get_dynamic_dca_distance_pct()
            # 2026-08 Option B fix (this `dca_time_eligible and` clause only -
            # every other term here, and the DCA block's own trigger/spacing/
            # sizing/distance logic, are unchanged): dca_time_eligible is the
            # SAME shared flag the actual DCA placement gate uses below, so
            # this signal can never claim a DCA opportunity is available when
            # the placement gate would actually refuse to place it. In
            # practice this expression only runs after the soft threshold has
            # already been crossed (see the enclosing `if` above), so
            # dca_time_eligible is always False here - the "DCA opportunity
            # available" defer reason can no longer fire once time-blocked.
            dca_opportunity_available = (
                dca_time_eligible
                and not hard_cap_hit
                and p.dca_step < MAX_DCA_STEPS
                and current_loss_distance >= dca_distance_pct_now
            )
            # 2026-08 Max Hold Time <-> DCA debug logging: records this
            # tick's defer-for-DCA decision so _on_entry_filled() (role=
            # "dca") can log "[dca] executed after max-hold defer" the
            # moment that DCA add actually fills, correlating the two
            # decisions in the logs. Consumed (reset to False) there every
            # time regardless of outcome, and also reset on every fresh
            # "initial" entry, so it can never leak into an unrelated DCA
            # fill from a different max-hold-time cycle or a later trade.
            # Logging-only - does not affect dca_opportunity_available or
            # still_deferring below in any way.
            self._max_hold_dca_defer_pending = dca_opportunity_available

            recovery_risk_signals: Dict[str, bool] = {}
            recovery_agree_count = 0
            low_probability_recovery = False
            if meaningful_loss and not hard_cap_hit:
                conf = self.last_confidence
                regime = self.last_regime
                velocity = 0.0
                if self.prev_price and self.current_price:
                    velocity = (self.current_price - self.prev_price) / self.prev_price
                # 2026-08 Max Hold recovery-vote fix (this dict only - the
                # separate max_dca_exhausted review below already correctly
                # excludes dca_exhausted from its own vote, unchanged):
                # dca_exhausted is position state (p.dca_step >=
                # MAX_DCA_STEPS is a deterministic fact about the trade),
                # not independent market evidence, so it no longer counts
                # toward recovery_agree_count. MAX_HOLD_TIME_RECOVERY_MIN_AGREE
                # (2) is unchanged - it's now measured against exactly the 4
                # genuine market-risk signals below. dca_exhausted is still
                # computed and logged as context (see [max-hold-review]
                # diagnostic) but is deliberately kept OUT of
                # recovery_risk_signals so it can never be summed into the
                # vote.
                dca_exhausted_context = p.dca_step >= MAX_DCA_STEPS
                recovery_risk_signals = {
                    "trend_against": (
                        conf.trend_direction is not None
                        and conf.trend_direction != p.side
                        and conf.trend_confidence >= 0.55
                    ),
                    "high_risk": conf.risk_score >= 0.75,
                    "momentum_against": (
                        (p.side == "LONG" and velocity < -0.0008)
                        or (p.side == "SHORT" and velocity > 0.0008)
                    ),
                    "extreme_volatility": (
                        regime.regime == REGIME_HIGH_VOL
                        and regime.atr_ratio >= REGIME_ATR_HIGH_MULT * 1.25
                    ),
                }
                recovery_agree_count = sum(1 for v in recovery_risk_signals.values() if v)
                low_probability_recovery = recovery_agree_count >= MAX_HOLD_TIME_RECOVERY_MIN_AGREE

            # 2026-08 Max Hold Time V2 stale-profit-lock-flag fix (isolated
            # to this still_deferring expression only - Profit Lock's own
            # activation threshold, ratio, and close condition above are
            # completely untouched): p.profit_lock_active is a sticky flag
            # that, once set True, is never reset back to False while the
            # SAME trade stays open (only a fresh PositionState() on full
            # close changes it) - see Profit Lock's fee-safe-floor comment
            # above for why a fast reversal can blow through the locked
            # level without Profit Lock's own close condition catching it.
            # Previously, a stuck profit_lock_active=True unconditionally
            # bypassed the recovery review below even after the position
            # had reversed into a real loss, silently disabling this
            # safety net for exactly the trades it's meant to protect.
            # profit_lock_active now only defers when the position is
            # STILL actually in profit right now (unrealized_pnl_usdt > 0)
            # - once it's flat/negative, the flag no longer bypasses the
            # meaningful-loss recovery-risk check below.
            still_deferring = (not hard_cap_hit) and (
                (p.profit_lock_active and unrealized_pnl_usdt > 0)
                or trending_and_profitable
                or dca_opportunity_available
                or (meaningful_loss and not low_probability_recovery)
            )

            # 2026-08 fee-net meaningful_loss fix - validation diagnostic
            # (logging only, does not affect still_deferring/meaningful_loss/
            # any decision above, which are already fully computed by this
            # point): lets a Testnet run confirm the fix directly - a
            # position whose raw pct_move is under MAX_HOLD_TIME_SMALL_LOSS_PCT
            # but whose fee_net_pnl_pct is beyond it should now show
            # meaningful_loss=True here (it would previously never have
            # reached this line at all, since the old raw-pct_move check
            # would have made meaningful_loss=False and skipped the
            # recovery-risk block entirely). Always logs on the CLOSE
            # decision (a one-time terminal event); throttled on repeated
            # DEFER ticks so it doesn't spam every tick while a position
            # sits in review.
            if meaningful_loss and (not still_deferring or self._should_log_max_hold_fee_net_review()):
                print(color(
                    f"{now_str()} [max-hold-review] dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                    f"pct_move={pct_move*100:.4f}% fee_net_pnl_usdt={unrealized_pnl_usdt:+.4f} "
                    f"fee_net_pnl_pct={fee_net_pnl_pct*100:.4f}% meaningful_loss={meaningful_loss} "
                    f"signals={','.join(k for k, v in recovery_risk_signals.items() if v) or 'none'} "
                    f"agree_count={recovery_agree_count}/{MAX_HOLD_TIME_RECOVERY_MIN_AGREE} "
                    f"action={'CLOSE' if not still_deferring else 'DEFER'}", CYAN,
                ))
            if not still_deferring:
                review_suffix = ""
                if meaningful_loss:
                    review_suffix = (
                        f", recovery_review={recovery_agree_count}/{len(recovery_risk_signals)} "
                        f"signals agree (required {MAX_HOLD_TIME_RECOVERY_MIN_AGREE}: "
                        f"{', '.join(k for k, v in recovery_risk_signals.items() if v)})"
                    )
                # 2026-08 Max Hold Time <-> DCA debug logging: structured
                # close-decision line requested for auditing future
                # decisions. Purely additive - printed once, right before
                # the close_position() call below, which is unchanged.
                if hard_cap_hit:
                    close_decision_reason = "hard cap reached"
                elif meaningful_loss and low_probability_recovery:
                    close_decision_reason = "recovery review failed"
                else:
                    close_decision_reason = "no meaningful loss / not protected at timeout"
                print(color(
                    f"{now_str()} [max-hold-v2] close decision: "
                    f"reason={close_decision_reason} "
                    f"dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                    f"dca_available={p.dca_step < MAX_DCA_STEPS} "
                    f"loss_pct={pct_move*100:.2f}% "
                    f"signals_agree={recovery_agree_count}/{len(recovery_risk_signals)}",
                    YELLOW,
                ))
                await self.close_position(
                    f"max hold time reached: {held_sec_so_far/3600:.2f}h >= "
                    f"{effective_max_hold_sec/3600:.2f}h (dca_step={p.dca_step}, hard_cap={hard_cap_hit}, "
                    f"regime={self.last_regime.regime}, unrealized_pnl=${unrealized_pnl_usdt:+.4f}, "
                    f"profit_lock_active={p.profit_lock_active}{review_suffix}) - closing dead/ranging "
                    f"hold instead of tying up capital indefinitely",
                    emergency=True, exit_reason_tag="max_hold_time",
                    expected_position=p,
                )
                return
            elif dca_opportunity_available and self._should_log_max_hold_dca_defer():
                print(color(
                    f"{now_str()} [max-hold-v2] deferred: "
                    f"reason=DCA opportunity available "
                    f"dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                    f"loss_pct={pct_move*100:.2f}% "
                    f"dca_distance={dca_distance_pct_now*100:.2f}% "
                    f"hold_time={held_sec_so_far/3600:.2f}h", YELLOW,
                ))
            elif meaningful_loss and self._should_log_max_hold_review():
                print(color(
                    f"{now_str()} [max-hold-review] EMERGENCY REVIEW: timeout reached with "
                    f"significant loss (unrealized_pnl=${unrealized_pnl_usdt:+.4f}, "
                    f"pct_move={pct_move*100:.2f}%) but only {recovery_agree_count}/"
                    f"{len(recovery_risk_signals)} recovery-risk signals agree (required "
                    f"{MAX_HOLD_TIME_RECOVERY_MIN_AGREE}) - keeping position open instead of "
                    f"force-closing (still bounded by hard cap at "
                    f"{MAX_HOLD_TIME_HARD_CAP_SEC/3600:.2f}h)", YELLOW,
                ))

        held_long_enough = (time.time() - p.opened_at) >= MIN_HOLD_SEC_BEFORE_EXIT
        dynamic_tp_pct = self.get_dynamic_take_profit_pct()

        # --- 2026-08 STRICT 1:2 RR - reward leg -----------------------------------
        # The mirror of the RR stop ceiling above, and the reason the envelope
        # is 1:2 rather than merely "a stop plus a percentage target": the
        # existing percentage-based TP below is left completely untouched and
        # still runs, but a winner can no longer drift past the configured
        # dollar target while the ATR-derived percentage TP waits for a move
        # that may never come.
        #
        # Two rungs, both fee-net and both on the WHOLE position (so they stay
        # correct after a DCA rescue changes the quantity):
        #   - TARGET_PROFIT_USD ($0.40): the full target - bank it.
        #   - MIN_TARGET_PROFIT_USD ($0.35): the band's lower edge - bank it
        #     early if live orderflow has already turned against the position,
        #     rather than giving back a nearly-complete win waiting for the
        #     last five cents.
        # Both rungs still respect the pre-existing MIN_NET_PROFIT_USDT
        # fee-safe floor, and both reuse exit_reason_tag="take_profit" so
        # trade-log classification, tp_hit labelling, the Brain's success
        # label and every performance statistic keep their existing meaning.
        if self.rr_enforcement_active() and held_long_enough:
            rr_net_pnl = self.estimate_net_pnl_usdt(price)
            if rr_net_pnl >= self.rr_target_profit_usd() and rr_net_pnl >= MIN_NET_PROFIT_USDT:
                await self.close_position(
                    f"1:{self.rr_ratio():.0f} RR target reached: est. fee-net pnl "
                    f"${rr_net_pnl:+.4f} >= ${self.rr_target_profit_usd():.2f} target "
                    f"(risk leg ${self.rr_stop_loss_usd():.2f}, {pct_move*100:.2f}% move)",
                    exit_reason_tag="take_profit",
                    expected_position=p,
                )
                return
            if rr_net_pnl >= MIN_TARGET_PROFIT_USD and rr_net_pnl >= MIN_NET_PROFIT_USDT:
                rr_flow = self.orderflow_snapshot()
                if ENABLE_ORDERBOOK_GUARD and rr_flow.get("data_available"):
                    rr_imbalance = float(rr_flow.get("imbalance", 0.0) or 0.0)
                    rr_delta = float(rr_flow.get("trade_delta", 0.0) or 0.0)
                    turning = (
                        (p.side == "LONG" and rr_imbalance < -ORDERBOOK_IMBALANCE_THRESHOLD and rr_delta < 0)
                        or (p.side == "SHORT" and rr_imbalance > ORDERBOOK_IMBALANCE_THRESHOLD and rr_delta > 0)
                    )
                    if turning:
                        await self.close_position(
                            f"1:{self.rr_ratio():.0f} RR target banked early: est. fee-net pnl "
                            f"${rr_net_pnl:+.4f} >= ${MIN_TARGET_PROFIT_USD:.2f} and orderflow "
                            f"turned against the {p.side} (imbalance={rr_imbalance:+.4f}, "
                            f"10s delta={rr_delta:+.4f})",
                            exit_reason_tag="take_profit",
                            expected_position=p,
                        )
                        return

        # --- 2026-08 SAFE DCA: fast break-even exit after the 1-step rescue -------
        # Once the single permitted rescue add has filled, the position's goal
        # changes: it is no longer hunting the full RR target on a book that
        # has already moved against it - it wants OUT at break-even (a small
        # but genuinely positive fee-net result) as soon as the rescue works.
        # This is the "target a fast Break-Even exit" half of the Safe DCA
        # rule; the "only add once, only with book support" half is enforced
        # at the DCA placement gate further below.
        #
        # DCA_RESCUE_BREAKEVEN_MIN_NET_USD is fee-NET, so this can never
        # realize a loss dressed up as break-even. Every other exit (TP, RR
        # target, Profit Lock, Smart Exit, Hard Stop, Max Hold, the RR stop
        # ceiling) remains fully active for a rescued position too.
        if (
            DCA_RESCUE_BREAKEVEN_ENABLED
            and p.dca_step >= 1
            and held_long_enough
            and self.position_sync_ready
        ):
            be_net_pnl = self.estimate_net_pnl_usdt(price)
            if be_net_pnl >= DCA_RESCUE_BREAKEVEN_MIN_NET_USD:
                print(color(
                    f"{now_str()} [dca-breakeven] rescued position recovered to a fee-net "
                    f"${be_net_pnl:+.4f} (>= ${DCA_RESCUE_BREAKEVEN_MIN_NET_USD:.2f}) - taking "
                    f"the fast break-even exit the Safe DCA rescue targets rather than "
                    f"re-risking the recovery on the full RR target.", GREEN,
                ))
                await self.close_position(
                    f"Safe DCA break-even exit: rescued {p.side} position "
                    f"(dca_step={p.dca_step}/{MAX_DCA_STEPS}) recovered to est. fee-net pnl "
                    f"${be_net_pnl:+.4f} at {pct_move*100:.2f}%",
                    exit_reason_tag="dca_breakeven",
                    expected_position=p,
                )
                return

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
                    expected_position=p,
                )
                return

        # --- Trailing stop on the runner (after partial TP armed breakeven) ----------
        # 2026-08 fee-aware exit fix (this block only - trail-level/ATR
        # calculation, activation via breakeven_armed, and every other
        # exit path are unchanged): previously this closed on `pct_move >
        # 0` alone - ANY still-technically-favorable price, even one too
        # small to clear round-trip fees. TP, Partial TP, and Profit Lock
        # all already require net_pnl >= MIN_NET_PROFIT_USDT before
        # closing (see those blocks) - trailing stop was the one exit path
        # that could still intentionally realize a net loss after fees on
        # a technically-"favorable" move. Now requires the same fee-safe
        # floor. If the trail level is hit but net PnL doesn't clear it,
        # this simply doesn't force-close via trailing stop this tick -
        # Hard Stop, Max Hold Time, and Smart Exit remain fully active as
        # independent backstops, so this can never create an unbounded
        # hold; it only removes trailing stop's ability to lock in a
        # fee-losing "win".
        if TRAILING_STOP_ENABLED and p.breakeven_armed and held_long_enough and self.last_regime.atr_pct > 0:
            trail_distance = price * self.last_regime.atr_pct * TRAILING_STOP_ATR_MULT
            if p.side == "LONG":
                candidate = (p.max_favorable_price or price) - trail_distance
                p.trailing_stop_price = candidate if p.trailing_stop_price is None else max(p.trailing_stop_price, candidate)
                if price <= p.trailing_stop_price and pct_move > 0:
                    net_pnl_trail = self.estimate_net_pnl_usdt(price)
                    if net_pnl_trail >= MIN_NET_PROFIT_USDT:
                        await self.close_position(
                            f"trailing stop: price {price:.2f} <= trail {p.trailing_stop_price:.2f} "
                            f"(ATR-based, {pct_move*100:.2f}% still favorable, "
                            f"est. net pnl=${net_pnl_trail:+.4f} after fees)",
                            exit_reason_tag="trailing_stop",
                            expected_position=p,
                        )
                        return
            else:
                candidate = (p.max_favorable_price or price) + trail_distance
                p.trailing_stop_price = candidate if p.trailing_stop_price is None else min(p.trailing_stop_price, candidate)
                if price >= p.trailing_stop_price and pct_move > 0:
                    net_pnl_trail = self.estimate_net_pnl_usdt(price)
                    if net_pnl_trail >= MIN_NET_PROFIT_USDT:
                        await self.close_position(
                            f"trailing stop: price {price:.2f} >= trail {p.trailing_stop_price:.2f} "
                            f"(ATR-based, {pct_move*100:.2f}% still favorable, "
                            f"est. net pnl=${net_pnl_trail:+.4f} after fees)",
                            exit_reason_tag="trailing_stop",
                            expected_position=p,
                        )
                        return

        # --- ATR-adaptive DCA distance (computed here so Smart Exit's
        # proximity gate below can reference the same value the DCA branch
        # further down uses) ---------------------------------------------------
        dca_distance_pct = self.get_dynamic_dca_distance_pct()

        # --- Smart Exit V2: requires a MAJORITY of independent signals to agree -------
        # 2026-07 Smart Exit fix (three gates, in order):
        #   1. Never evaluated until the position is at least
        #      SMART_EXIT_MIN_LOSS_PCT (-0.10%) adverse.
        #   2. Blocked outright once the adverse move is already within
        #      SMART_EXIT_DCA_PROXIMITY_RATIO (90%) of the DCA trigger
        #      distance, so DCA gets to activate instead of racing Smart
        #      Exit to close the trade first.
        #   3. SMART_EXIT_MIN_AGREE raised to 5/6 (see config.py).
        #
        # 2026-08 Smart Exit V2 retune (two additional gates, isolated to
        # this block only - signal calculation in _smart_exit_v2_signals(),
        # DCA/TP/Hard-Stop/Brain logic, and everything else untouched):
        #   4. Smart Exit now waits for its OWN minimum hold
        #      (SMART_EXIT_MIN_HOLD_SEC, 90s) rather than reusing the
        #      general held_long_enough (60s, shared with TP/partial-TP/
        #      trailing-stop). A fresh entry gets extra room before Smart
        #      Exit can act, without delaying take-profit for winners.
        #   5. In ranging regimes (SIDEWAYS/WEAK_TREND) - where ordinary
        #      chop routinely trips 1-2 of the 6 signals without a real
        #      reversal - the required agreement bar rises to
        #      SMART_EXIT_MIN_AGREE_RANGING (6/6, unanimous) instead of the
        #      normal SMART_EXIT_MIN_AGREE (5/6). STRONG_TREND/HIGH_VOL are
        #      unchanged at 5/6 so genuine reversals are still caught fast.
        smart_exit_held_long_enough = (time.time() - p.opened_at) >= SMART_EXIT_MIN_HOLD_SEC
        smart_exit_loss_gate = pct_move <= -SMART_EXIT_MIN_LOSS_PCT
        smart_exit_near_dca = (
            dca_distance_pct > 0
            and (-pct_move) >= dca_distance_pct * SMART_EXIT_DCA_PROXIMITY_RATIO
        )
        # 2026-08 position_sync_ready gate (the `self.position_sync_ready
        # and` clause only - every other condition/signal/threshold below
        # is unchanged): Smart Exit's multi-signal analysis is exactly the
        # kind of discretionary, provisional-economics-dependent decision
        # that must not run until initialize_sync() has authoritatively
        # reconciled local state against Binance - see the identical gate
        # on the Max Hold V2 review above.
        if (
            self.position_sync_ready
            and SMART_EXIT_ENABLED and smart_exit_held_long_enough
            and smart_exit_loss_gate
            and pct_move > -SMART_EXIT_MAX_LOSS_PCT
            and not smart_exit_near_dca
        ):
            signals = self._smart_exit_v2_signals(pct_move, dynamic_tp_pct)
            agree_count = sum(1 for v in signals.values() if v)
            required_agree = (
                SMART_EXIT_MIN_AGREE_RANGING
                if self.last_regime.regime in (REGIME_SIDEWAYS, REGIME_WEAK_TREND)
                else SMART_EXIT_MIN_AGREE
            )
            if agree_count >= required_agree:
                self._last_smart_exit_diagnostics = {
                    "agree_count": agree_count,
                    "required_agree": required_agree,
                    "signals_fired": ",".join(k for k, v in signals.items() if v),
                    "dca_distance_pct": dca_distance_pct,
                }
                await self.close_position(
                    f"SMART EXIT V2: {agree_count}/{len(signals)} signals agree "
                    f"(required {required_agree}, regime={self.last_regime.regime}) "
                    f"({', '.join(k for k, v in signals.items() if v)}) at {pct_move*100:.2f}% - "
                    f"exiting before further adverse move rather than a single-tick panic exit",
                    exit_reason_tag="smart_exit",
                    expected_position=p,
                )
                return

        # --- ATR-adaptive DCA -----------------------------------------------------------
        if pct_move <= -dca_distance_pct:
            # 2026-08 hard DCA safety invariant (this condition only - the
            # MAX_DCA_STEPS comparison itself, DCA trigger distance, and
            # DCA sizing are all untouched): a position recovered with
            # p.dca_blocked=True (see PositionState) - e.g. a REST
            # order-status lookup during resync came back ambiguous/failed,
            # or no matching DCA-state snapshot could be found for an
            # already-open position - is blocked from any further DCA add.
            # A confirmed dca_step >= MAX_DCA_STEPS takes the exposure cap
            # below; an uncertain dca_blocked position only blocks
            # exposure because uncertainty is not proof that 2/2 fills
            # completed. TP / Hard Stop / Smart Exit / Profit Lock / Max
            # Hold Time continue to manage either position. This is
            # the single point that turns "we don't know the true DCA step
            # count" into "never add on top of an uncertain count", instead
            # of silently defaulting to 0 and allowing more adds.
            if p.dca_step >= MAX_DCA_STEPS:
                # 2026-08 fee-aware exhausted-DCA correction: MAX_DCA_STEPS
                # is a HARD ADDITIONAL-EXPOSURE boundary, not an automatic
                # stop at the same tiny percentage used to trigger DCA.
                #
                # Live evidence showed why the old immediate close was
                # economically self-defeating: after DCA #2 moved the
                # weighted average toward the latest fill, the position was
                # already almost 0.20% adverse to that NEW average. A few
                # ticks later this branch closed a three-fill MARKET book,
                # realizing $0.2085 fees and a $0.5886 net loss only 53s
                # after DCA #2. That converts the final DCA from a recovery
                # tool into a near-immediate fee crystallizer.
                #
                # At 2/2 we now only cap exposure and return. No DCA #3 can
                # be submitted. Normal fee-net TP/Profit Lock, Smart Exit,
                # Hard Stop, liquidation protection, and the deterministic
                # DCA-aware 2h Max Hold boundary all ran/remain active. The
                # existing final-DCA risk gate also already rejected DCA #2
                # before placement whenever >=2 independent risk signals
                # indicated low-probability recovery.
                if self._should_log_max_dca_exhausted_review():
                    est_net_pnl = self.estimate_net_pnl_usdt(price)
                    print(color(
                        f"{now_str()} [max-dca-exhausted] "
                        f"dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                        f"pct_move={pct_move*100:.4f}% "
                        f"dca_distance={dca_distance_pct*100:.4f}% "
                        f"fee_net_pnl_usdt={est_net_pnl:+.4f} "
                        f"decision=HOLD reason=exposure_capped_normal_exits_active",
                        CYAN,
                    ))
                return

            if p.dca_blocked:
                # Unknown recovery state is exposure-blocking, not proof of
                # how many fills actually completed. Never submit another
                # DCA, but do not pretend it is a confirmed 2/2 position.
                # TP / Profit Lock / Smart Exit / Hard Stop / Max Hold ran
                # earlier and remain available exactly as before.
                print(color(
                    f"{now_str()} [dca-safety-block] side={p.side} "
                    f"dca_step={p.dca_step}/{MAX_DCA_STEPS} reason="
                    f"{p.dca_block_reason or 'unresolved recovery state'} - further DCA blocked.",
                    RED,
                ))
                return

            # item 6: while this process cannot confirm a protective stop
            # is correctly resting on the exchange for this position
            # (initial placement failed, a DCA-triggered replace failed, or
            # startup reconciliation found none), do not add MORE exposure
            # on top of an already-unprotected position. TP / Hard Stop /
            # Profit Lock / Smart Exit / Max Hold / the per-trade net-loss
            # budget all remain fully active and are, if anything, more
            # relied upon while this is True - only a NEW DCA add is
            # withheld here.
            if p.protection_pending:
                if self._should_log_protection_pending():
                    print(color(
                        f"{now_str()} [protective-stop] *** HIGH SEVERITY *** PROTECTION_PENDING "
                        f"side={p.side} dca_step={p.dca_step}/{MAX_DCA_STEPS} reason="
                        f"{p.protection_pending_reason or 'unknown'} - withholding new DCA add; "
                        f"client-side risk exits (Hard Stop/Max Hold/net-loss budget) remain active.",
                        RED,
                    ))
                return

            # 2026-08 position_sync_ready gate (this block only - the
            # step-exhaustion branch above only caps exposure and returns;
            # only a NEW DCA add - genuine additional exposure - is
            # withheld here): mirrors the identical gate on Max Hold V2 /
            # Smart Exit above, using the same shared throttle helper.
            if not self.position_sync_ready:
                if self._should_log_sync_not_ready():
                    print(color(
                        f"{now_str()} [dca-skip] position_sync_ready=False - withholding new DCA "
                        f"add (additional exposure) until an authoritative exchange sync confirms "
                        f"this position's real economics.", YELLOW,
                    ))
                return

            # 2026-08 Option B - DCA time gate (this block only - the
            # step-exhaustion branch above, which always returns without
            # ever placing an order, is untouched and stays fully
            # independent of hold time; the spacing gate, final-DCA gate,
            # and order placement just below are all still reached exactly
            # as before whenever dca_time_eligible is True). Uses the SAME
            # dca_time_eligible flag computed once near the top of this
            # function and already used by dca_opportunity_available above -
            # not a second/parallel time check - so this can never disagree
            # with what the emergency review believes is available. Once the
            # soft max-hold threshold is reached, no NEW DCA add is placed
            # regardless of price distance; Max Hold Time V2's own
            # recovery-risk review (evaluated earlier this same tick,
            # unconditionally) is the sole mechanism deciding hold-vs-close
            # from here. Hard Stop, Profit Lock, TP/Trailing, and Smart Exit
            # all ran earlier in this function and are completely
            # unaffected - this only withholds a NEW order, the same way the
            # spacing-gate and final-DCA-gate `return`s below already do.
            if not dca_time_eligible:
                if self._should_log_dca_time_blocked():
                    print(color(
                        f"{now_str()} [dca-time-blocked] dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                        f"held={held_sec_so_far/3600:.2f}h >= "
                        f"soft_threshold={effective_max_hold_sec/3600:.2f}h - withholding new DCA add "
                        f"(Max Hold Time V2 review governs from here)", YELLOW,
                    ))
                return

            # item 8 - prospective post-DCA max-hold gate (this block only -
            # every other gate/formula in this function, including
            # dca_time_eligible/effective_max_hold_sec above, is untouched).
            #
            # Root cause (confirmed from the attached live incident):
            # dca_time_eligible above is computed from effective_max_hold_sec,
            # which uses the CURRENT p.dca_step - correct for deciding
            # whether THIS position, as it stands right now, has timed out,
            # but the WRONG threshold for deciding whether to SUBMIT a new
            # DCA add. The instant this order fills, dca_step becomes >=1,
            # which immediately multiplies the soft timeout by
            # MAX_HOLD_TIME_DCA_MULTIPLIER (e.g. 4h -> 2h). Live evidence: a
            # dca_step=0 position already held 3.10h passed the check above
            # (4h soft cap, not yet reached) and DCA #1 was submitted and
            # filled; on the very next tick, the SAME 3.10h now exceeded the
            # post-DCA 2h soft cap, forcing an immediate max_hold_time close
            # (net -$1.007) fractions of a second after paying DCA #1's
            # entry commission - the DCA add could not possibly help and
            # only added cost before a close that was already coming.
            #
            # Fix: compute the timeout that WILL apply the instant this DCA
            # fills (post-fill dca_step is guaranteed >=1 regardless of
            # which step this is, so this is NOT step-dependent like
            # effective_max_hold_sec above) and withhold the DCA if the
            # position is ALREADY past that prospective threshold. Max Hold
            # Time V2's own recovery-risk review (evaluated earlier this
            # same tick, unconditionally) remains the sole mechanism
            # deciding hold-vs-close from here - identical in spirit to how
            # dca_time_eligible already hands off to it above. TP / Hard
            # Stop / Profit Lock / Smart Exit / the per-trade net-loss
            # budget are all unaffected - this only withholds a NEW DCA
            # order that would otherwise be immediately, unavoidably overdue.
            prospective_post_dca_max_hold_sec = MAX_HOLD_TIME_SEC * MAX_HOLD_TIME_DCA_MULTIPLIER
            if MAX_HOLD_TIME_ENABLED and held_sec_so_far >= prospective_post_dca_max_hold_sec:
                if self._should_log_dca_post_step_timeout():
                    print(color(
                        f"{now_str()} [dca-blocked-post-step-timeout] dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                        f"held={held_sec_so_far/3600:.2f}h >= prospective_post_dca_limit="
                        f"{prospective_post_dca_max_hold_sec/3600:.2f}h - this DCA would be "
                        f"immediately overdue the instant it fills; withholding the add instead of "
                        f"paying its commission right before a forced timeout close "
                        f"(dca_blocked_post_step_timeout).", YELLOW,
                    ))
                return

            # --- 2026-08 DCA re-fire spacing fix (this block only - sizing,
            # DCA_MULTIPLIER, MAX_DCA_STEPS, the dca_distance_pct formula
            # itself, TP, Hard Stop, Smart Exit, Max Hold, Profit Lock, and
            # everything else in this function are all untouched) --------
            # Scoped to DCA #2 and later (p.dca_step >= 1) only - DCA #1
            # (the first add after the initial entry) still fires purely
            # off the pct_move-vs-avg_entry_price check above, exactly as
            # before this fix.
            #
            # Root cause: avg_entry_price recalculates the instant a DCA
            # fill lands, blending toward that fill's price. That can
            # leave price ALREADY beyond -dca_distance_pct of the NEW
            # avg_entry even though price has barely moved since the
            # PREVIOUS DCA fill itself - letting two DCA steps consume the
            # same adverse move within a fraction of a second (observed:
            # DCA #1 and #2 both filling ~77.08, ~0.09s apart). This adds
            # a second, independent requirement for step 2+: price must
            # move another FULL dca_distance_pct beyond the anchor set by
            # the previous DCA fill (last_dca_price) - the exact existing
            # field already documented and persisted for this purpose,
            # not a parallel system. Uses the SAME dca_distance_pct value
            # computed above (ATR-adaptive formula itself untouched).
            if p.dca_step >= 1 and p.last_dca_price is not None:
                if p.side == "LONG":
                    next_dca_trigger_price = p.last_dca_price * (1 - dca_distance_pct)
                    spacing_satisfied = price <= next_dca_trigger_price
                else:
                    next_dca_trigger_price = p.last_dca_price * (1 + dca_distance_pct)
                    spacing_satisfied = price >= next_dca_trigger_price

                if spacing_satisfied or self._should_log_dca_spacing():
                    print(color(
                        f"{now_str()} [dca-spacing] dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                        f"side={p.side} price={price:.4f} last_dca_price={p.last_dca_price:.4f} "
                        f"required_next_trigger={next_dca_trigger_price:.4f} "
                        f"dca_distance_pct={dca_distance_pct*100:.4f}% "
                        f"decision={'TRIGGER' if spacing_satisfied else 'WAIT'}", CYAN,
                    ))

                if not spacing_satisfied:
                    # Not yet another full dca_distance_pct beyond the
                    # previous DCA fill - wait for a later tick. No order
                    # placed, dca_step unchanged, position stays exactly as
                    # it is. Hard Stop/Smart Exit/TP/Profit Lock/Max Hold
                    # all remain fully active in the meantime.
                    return

            # --- 2026-08 SAFE DCA: orderbook support requirement ----------------
            # The DCA add is now a single-step RESCUE order (MAX_DCA_STEPS
            # defaults to 1), and a rescue is only worth paying for when the
            # resting book actually backs the reversal it is betting on:
            # bids stacked under a losing LONG, asks stacked over a losing
            # SHORT. Without that, averaging down is just buying more of a
            # move that has not finished.
            #
            # Placed here - after every pre-existing DCA gate (step cap,
            # dca_blocked, protection_pending, sync-ready, both time gates,
            # spacing) and before the final-DCA risk gate and the loss-budget
            # projection below - so it can only ever WITHHOLD an add those
            # gates already approved. A withheld add does not increment
            # dca_step and does not count as a used DCA; TP / RR target /
            # Hard Stop / RR stop / Profit Lock / Smart Exit / orderflow exit
            # / Max Hold all remain fully active meanwhile, and the add is
            # re-evaluated on any later qualifying tick once the book turns.
            if DCA_REQUIRE_ORDERBOOK_SUPPORT and ENABLE_ORDERBOOK_GUARD:
                dca_flow = self.orderflow_snapshot()
                if not self.liquidity_guard.supports_reversal(
                    p.side, dca_flow, support_min=DCA_RESCUE_SUPPORT_MIN,
                ):
                    if self._should_log_dca_orderbook_block():
                        print(color(
                            f"{now_str()} [dca-orderbook-block] withholding the "
                            f"{'rescue ' if MAX_DCA_STEPS <= 1 else ''}DCA add "
                            f"step={p.dca_step + 1}/{MAX_DCA_STEPS} for the {p.side}: the book "
                            f"does not support the reversal (imbalance="
                            f"{dca_flow.get('imbalance', 0.0):+.4f}, required "
                            f"{'>' if p.side == 'LONG' else '<'} "
                            f"{DCA_RESCUE_SUPPORT_MIN if p.side == 'LONG' else -DCA_RESCUE_SUPPORT_MIN:+.2f}, "
                            f"orderflow_data={dca_flow.get('data_available')}). dca_step "
                            f"unchanged; every exit path remains active.", YELLOW,
                        ))
                    return

            # --- Final-DCA low-probability-recovery gate (2026-07 final-DCA
            # gate fix - see module docstring) ----------------------------------
            # Scoped ONLY to the last allowed DCA step. Before committing the
            # LAST DCA add, sanity-check whether this genuinely looks like a
            # low-probability recovery rather than an ordinary sideways
            # chop the DCA add would otherwise handle fine. Does not touch
            # any earlier DCA step, sizing, or any other decision in this
            # function.
            #
            # confidence_score is deliberately NOT part of this gate - Brain
            # V2's confidence legitimately runs low during normal sideways
            # consolidation, and using it here was closing out perfectly
            # recoverable trades before the last DCA ever got a chance to
            # work. Instead, four independent "genuinely bad" signals are
            # each evaluated with a stricter bar than their general-purpose
            # (Smart Exit) equivalents, and at least TWO of them must agree
            # before the final DCA is skipped and the position closed - a
            # single borderline reading is no longer enough on its own.
            is_final_dca_step = (p.dca_step + 1) >= MAX_DCA_STEPS
            if is_final_dca_step:
                conf = self.last_confidence
                regime = self.last_regime
                velocity = 0.0
                if self.prev_price and self.current_price:
                    velocity = (self.current_price - self.prev_price) / self.prev_price

                # 1) Abnormal momentum against the position - raised from
                #    0.0004 to 0.0008 so a single mildly adverse tick isn't
                #    "abnormal" on its own.
                momentum_against = (
                    (p.side == "LONG" and velocity < -0.0008)
                    or (p.side == "SHORT" and velocity > 0.0008)
                )
                # 2) Strong trend against the position - trend_confidence
                #    bar raised from 0.35 to 0.55.
                trend_against = (
                    conf.trend_direction is not None
                    and conf.trend_direction != p.side
                    and conf.trend_confidence >= 0.55
                )
                # 3) Genuinely high (not just elevated) heuristic risk score
                #    - bar raised from 0.65 to 0.75.
                high_risk = conf.risk_score >= 0.75
                # 4) Extreme volatility - HIGH_VOL regime AND atr_ratio well
                #    beyond the regime engine's own HIGH_VOL cutoff, not
                #    merely at the threshold.
                extreme_volatility = (
                    regime.regime == REGIME_HIGH_VOL
                    and regime.atr_ratio >= REGIME_ATR_HIGH_MULT * 1.25
                )

                recovery_risk_signals = {
                    "trend_against": trend_against,
                    "high_risk": high_risk,
                    "momentum_against": momentum_against,
                    "extreme_volatility": extreme_volatility,
                }
                agree_count = sum(1 for v in recovery_risk_signals.values() if v)
                low_probability_recovery = agree_count >= 2

                if low_probability_recovery:
                    # ================================================================
                    # [dca-risk-debug] TEMPORARY diagnostic only (2026-08 deep-DCA
                    # decision evidence gathering). Printed immediately before this
                    # exact close_position() call, reusing conf/regime/agree_count/
                    # recovery_risk_signals already computed above for the real
                    # decision, plus pct_move/dynamic_tp_pct/dca_distance_pct already
                    # computed earlier in this function and the existing
                    # estimate_net_pnl_usdt() helper - no new formula, no threshold,
                    # no control flow. Does not gate, delay, or otherwise affect the
                    # close_position() call immediately below it in any way.
                    # ================================================================
                    _dbg_notional = (p.total_qty * p.avg_entry_price) if p.avg_entry_price else 0.0
                    _dbg_adverse_to_tp = (abs(pct_move) / dynamic_tp_pct) if dynamic_tp_pct > 0 else float("nan")
                    _dbg_dca_to_tp = (dca_distance_pct / dynamic_tp_pct) if dynamic_tp_pct > 0 else float("nan")
                    _dbg_est_net_pnl = self.estimate_net_pnl_usdt(price)
                    print(color(
                        f"[dca-risk-debug] exit_candidate=final_dca_skipped_low_probability "
                        f"side={p.side} dca_step={p.dca_step}/{MAX_DCA_STEPS} "
                        f"avg_entry={p.avg_entry_price:.2f} price={price:.2f} "
                        f"total_qty={p.total_qty:.6f} notional={_dbg_notional:.2f} "
                        f"pct_move={pct_move*100:.4f}% dynamic_tp={dynamic_tp_pct*100:.4f}% "
                        f"dca_distance={dca_distance_pct*100:.4f}% "
                        f"adverse_to_tp_ratio={_dbg_adverse_to_tp:.3f} dca_to_tp_ratio={_dbg_dca_to_tp:.3f} "
                        f"regime={regime.regime} atr_pct={regime.atr_pct:.5f} atr_ratio={regime.atr_ratio:.2f} "
                        f"risk={conf.risk_score:.2f} trend_direction={conf.trend_direction} "
                        f"trend_confidence={conf.trend_confidence:.2f} confidence={conf.confidence_score:.2f} "
                        f"profit_lock_active={p.profit_lock_active} est_net_pnl={_dbg_est_net_pnl:.4f} "
                        f"trend_against={trend_against} momentum_against={momentum_against} "
                        f"high_risk={high_risk} extreme_volatility={extreme_volatility} "
                        f"agree_count={agree_count}/4", CYAN,
                    ))
                    await self.close_position(
                        f"final DCA step skipped: low-probability recovery "
                        f"({agree_count}/{len(recovery_risk_signals)} signals agree: "
                        f"{', '.join(k for k, v in recovery_risk_signals.items() if v)}; "
                        f"risk={conf.risk_score:.2f}, trend_direction={conf.trend_direction}, "
                        f"trend_confidence={conf.trend_confidence:.2f}, regime={regime.regime}, "
                        f"atr_ratio={regime.atr_ratio:.2f}) at {pct_move*100:.2f}% - "
                        f"exiting instead of adding the last DCA step",
                        emergency=True, exit_reason_tag="final_dca_skipped_low_probability",
                        expected_position=p,
                    )
                    return

            size_mult = self.confidence_size_multiplier(self.last_confidence, self.last_regime)

            # item 7 - DCA loss-budget gate (this block only - sizing,
            # DCA_MULTIPLIER, MAX_DCA_STEPS, dca_distance_pct, and every
            # other DCA formula above are untouched; does NOT widen
            # MAX_TRADE_NET_LOSS_USDT to "make room" for a DCA - the budget
            # itself is fixed, this only decides whether the add fits
            # inside it). Before submitting this DCA, projects the
            # position's fee-net PnL exactly as it would be immediately
            # after this add fills (new blended avg_entry_price, this
            # step's own entry commission, and an updated estimated closing
            # commission on the LARGER post-DCA quantity - see
            # estimate_net_pnl_usdt_executable's extra_qty/extra_entry_price
            # parameters) and blocks the add if that projection cannot stay
            # meaningfully inside the budget. A blocked DCA does not
            # increment dca_step (this simply returns without calling
            # _place_step_order) and does not count as a completed DCA -
            # TP / Hard Stop / Profit Lock / Smart Exit / Max Hold Time /
            # the per-trade net-loss budget itself all remain fully active
            # afterward. See section 7 of the task for the accompanying
            # economics note: at current $80 initial notional and ~0.05%
            # taker fees, this gate is EXPECTED to block most or all DCA
            # activity at the currently-configured size - that is the
            # intended, accepted trade-off of keeping a strict $0.20
            # fee-net budget with the existing $4 margin/DCA sizing.
            add_notional = self.notional_for_step(p.dca_step + 1, size_mult)
            add_qty = round_step(add_notional / price, self.filters.step_size) if price else 0.0
            if add_qty > 0:
                projected_net_pnl = self.estimate_net_pnl_usdt_executable(
                    extra_qty=add_qty, extra_entry_price=price,
                )
                if MAX_TRADE_NET_LOSS_USDT > 0:
                    loss_budget_trigger = -(MAX_TRADE_NET_LOSS_USDT - MAX_TRADE_EXIT_BUFFER_USDT)
                    if projected_net_pnl <= loss_budget_trigger:
                        remaining = projected_net_pnl - loss_budget_trigger
                        if self._should_log_dca_loss_budget_blocked():
                            print(color(
                                f"{now_str()} [dca-budget] blocked step={p.dca_step + 1} "
                                f"projected_net_loss={projected_net_pnl:+.4f} "
                                f"budget=-{MAX_TRADE_NET_LOSS_USDT:.2f} "
                                f"buffer={MAX_TRADE_EXIT_BUFFER_USDT:.2f} "
                                f"trigger={loss_budget_trigger:+.4f} remaining={remaining:+.4f} - "
                                f"adding this DCA would not remain meaningfully inside the "
                                f"per-trade loss budget; withholding (dca_step unchanged).", YELLOW,
                            ))
                        return

            await self._place_step_order(
                step=p.dca_step + 1, side_signal=p.side, size_mult=size_mult, expected_position=p,
            )
            # 2026-08 DCA re-fire spacing fix: last_dca_price is now set
            # from the ACTUAL fill price in _on_entry_filled()'s "dca"
            # branch below (once the order actually fills), not here from
            # the pre-order mark price - see that function for why.

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

    async def _try_recover_close_fill(self, order_id: int, o: dict) -> bool:
        """Restart-safe fallback for handle_order_update(): if `order_id`
        isn't in this process's in-memory _order_index (e.g. a restart
        happened between close_position() placing the order and its
        FILLED event arriving), checks the persisted DCA-state snapshot
        for a matching pending CLOSE order before giving up. Only fires
        when the snapshot's own pending_order_id (for pending_role="close")
        exactly matches this order_id, so an unrelated/stale snapshot can
        never be mistaken for this fill. Never raises - any failure here
        just falls through to the existing untracked_order_id diagnostic
        and reconciliation safety net, unchanged."""
        # 2026-08 idempotency guard (review finding 2 follow-up): this
        # snapshot-based recovery must only ever fire for a trade that is
        # still open locally. Once _on_close_filled() has finalized a trade
        # the position is reset to FLAT, but the on-disk snapshot is
        # rewritten asynchronously (save_flat_dca_state via create_task), so
        # a DUPLICATE fill event arriving in that window would otherwise
        # match the still-stale snapshot and finalize the same trade twice -
        # double-counting realized PnL, the trade log and daily counters.
        # The in-memory status is the authoritative, synchronous signal that
        # this trade is already done.
        if self.position.status not in ("OPEN", "DCA_PENDING", "CLOSING"):
            return False
        try:
            snapshot = await self.load_dca_state_snapshot()
        except Exception:  # noqa: BLE001 - recovery must never crash the fill handler
            return False
        if not snapshot:
            return False
        # 2026-08 protective-stop fill-routing fix (review finding 2):
        # a protective stop rests on the exchange for the whole life of the
        # trade, so a restart between placing it and its FILLED event
        # arriving is far more likely than for an ordinary close order. The
        # persisted snapshot records its order id separately (it is never
        # this process's pending_order_id, which is reserved for in-flight
        # orders), so match on that too - otherwise a protective stop that
        # triggered while the process was down would be dropped here and the
        # bot would resume managing an already-closed position.
        matched_role = None
        # 2026-08 Algo-Service migration: ORDER_TRADE_UPDATE carries the
        # CHILD order id (actualOrderId), never the algoId - so restart
        # recovery must match the persisted child id.
        snap_protective_id = snapshot.get("protective_stop_actual_order_id")
        if snap_protective_id is not None:
            try:
                if int(snap_protective_id) == int(order_id):
                    matched_role = "protective_stop"
            except (TypeError, ValueError):
                pass
        if matched_role is None:
            if snapshot.get("pending_role") != "close":
                return False
            snap_order_id = snapshot.get("pending_order_id")
            if snap_order_id is None:
                return False
            try:
                if int(snap_order_id) != int(order_id):
                    return False
            except (TypeError, ValueError):
                return False
            matched_role = "close"

        fill_price = float(o.get("ap") or 0.0)
        rp = float(o.get("rp") or 0.0)
        # 2026-08 realized-PnL/fee-accounting fix: this order never passed
        # through handle_order_update()'s normal "n"/"N" accumulation
        # (that's exactly why this restart-recovery path exists), so read
        # and roll in this fill's own commission here too, before
        # finalizing - otherwise a restart landing on the close fill itself
        # would silently lose that leg's commission.
        comm = float(o.get("n") or 0.0)
        comm_asset = o.get("N")
        if comm:
            if comm_asset is None or comm_asset == "USDT":
                self._position_fees_accum += comm
            else:
                self._position_fees_reliable = False
        if matched_role == "protective_stop":
            # The stop triggered; it is gone from the exchange. Clear local
            # tracking and tag the exit reason so the trade log records how
            # this trade actually ended.
            self._clear_protective_stop_tracking()
            self._pending_exit_reason = "protective_stop"
        print(color(
            f"{now_str()} [fill-trace] path=restart_recovery order_id={order_id} "
            f"reason=matched_persisted_dca_state_snapshot (role={matched_role}) -> routing to "
            f"_on_close_filled() despite empty in-memory _order_index (restart-safe recovery)", CYAN,
        ))
        await self._on_close_filled(fill_price, rp, order_id=order_id)
        return True

    async def _resolve_pending_order_via_rest(
        self, client: RestClient, order_id: int, role: str, context: str,
    ) -> str:
        """2026-08 REST order-status fallback (the primary fix for the
        over-DCA/repeated-resync bug this patch addresses): called from
        initialize_sync() ONLY after SYNC_PENDING_GRACE_SEC has already
        elapsed with no WebSocket fill event for a pending entry/DCA/close
        order. Queries this EXACT order_id via signed GET /fapi/v1/order
        (never guesses from a position-snapshot comparison) and, on a
        genuine FILLED result, routes the fill through the SAME
        _on_entry_filled() / _on_close_filled() / _apply_partial_close()
        functions the live WebSocket path uses - so dca_step/avg_entry/
        total_qty advance exactly once, through exactly one code path,
        regardless of whether the fill was learned about via WebSocket or
        this REST fallback.

        Idempotency: if `order_id` is no longer in self._order_index, it
        was already consumed by handle_order_update() (which only pops an
        order_id once its fill has actually been processed) - possibly a
        moment ago, possibly by an earlier call to this same method.
        Returns "filled" immediately without processing anything a second
        time. This is the same idempotency guarantee handle_order_update()
        already relies on for a duplicate/late WebSocket event.

        Returns exactly one of:
          "filled"           - genuinely FILLED, and (unless already
                                processed) has now been routed through the
                                normal fill path exactly once.
          "pending"           - still NEW/PARTIALLY_FILLED on Binance's
                                side; caller must keep waiting, not
                                resync/rebuild, not place another DCA.
          "resolved_no_fill"  - CANCELED/EXPIRED/REJECTED; this order
                                never added anything to the position -
                                caller may clear pending bookkeeping and
                                resync normally (no DCA-step impact).
          "unknown"           - REST error, unknown order, missing/zero
                                fill data, or any other ambiguous result.
                                Caller MUST NOT reset dca_step to 0 or
                                allow further DCA - see
                                PositionState.dca_blocked, which the
                                caller sets on this outcome.
        """
        if order_id not in self._order_index:
            return "filled"
        try:
            resp = await client.get_order(self.symbol, order_id)
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(color(
                f"{now_str()} [sync:{context}] [rest-recovery] GET /fapi/v1/order failed for "
                f"order_id={order_id} role={role}: {e}. Cannot confirm this order's outcome - "
                f"treating as unresolved rather than assuming it's safe to reset/continue.", RED,
            ))
            return "unknown"

        status = resp.get("status")
        print(color(
            f"{now_str()} [sync:{context}] [rest-recovery] order_id={order_id} role={role} "
            f"status={status} (REST fallback after {SYNC_PENDING_GRACE_SEC}s grace with no "
            f"WebSocket fill event).", CYAN,
        ))

        if status == "FILLED":
            try:
                fills = await client.get_user_trades(self.symbol, order_id=order_id, limit=100)
            except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                fills = []
                print(color(
                    f"{now_str()} [sync:{context}] [rest-recovery] order_id={order_id} FILLED but "
                    f"userTrades lookup failed ({e}) - falling back to the order's own avgPrice/"
                    f"executedQty; this fill's commission will use the TAKER_FEE_RATE estimate "
                    f"instead of the exact figure.", YELLOW,
                ))

            fill_qty = float(resp.get("executedQty") or 0.0)
            fill_price = float(resp.get("avgPrice") or 0.0)
            total_rp = 0.0
            total_fee = 0.0
            fee_reliable_this_fill = True
            if fills:
                summed_qty = sum(float(t.get("qty", 0.0) or 0.0) for t in fills)
                if summed_qty > 0:
                    fill_qty = summed_qty
                    notional = sum(
                        float(t.get("qty", 0.0) or 0.0) * float(t.get("price", 0.0) or 0.0) for t in fills
                    )
                    fill_price = safe_div(notional, fill_qty, fill_price)
                total_rp = sum(float(t.get("realizedPnl", 0.0) or 0.0) for t in fills)
                for t in fills:
                    c = float(t.get("commission", 0.0) or 0.0)
                    if t.get("commissionAsset") in (None, "USDT"):
                        total_fee += c
                    else:
                        fee_reliable_this_fill = False
            else:
                fee_reliable_this_fill = False

            if fill_qty <= 0 or fill_price <= 0:
                print(color(
                    f"{now_str()} [sync:{context}] [rest-recovery] order_id={order_id} reported "
                    f"FILLED but returned no usable qty/price - treating as unresolved, blocking "
                    f"further DCA rather than guessing at a fill that can't be sized.", RED,
                ))
                return "unknown"

            role_actual = self._order_index.pop(order_id, role)
            # Mirrors handle_order_update()'s existing per-role fee-accumulator
            # reset/breakdown sequence exactly (see that function), so a
            # REST-recovered fill and a live-WebSocket fill leave
            # self._position_fees_accum/_entry_commission_accum/etc. in
            # identical states either way.
            if role_actual == "initial":
                self._position_fees_accum = 0.0
                self._position_fees_reliable = True
                self._entry_commission_accum = 0.0
                self._dca_commission_accum = 0.0
                self._exit_commission_accum = 0.0
            if not fee_reliable_this_fill:
                self._position_fees_reliable = False
            self._position_fees_accum += total_fee
            if role_actual == "initial":
                self._entry_commission_accum += total_fee
            elif role_actual == "dca":
                self._dca_commission_accum += total_fee
            elif role_actual in ("close", "partial_close"):
                self._exit_commission_accum += total_fee

            if role_actual in ("initial", "dca"):
                print(color(
                    f"{now_str()} [fill-trace] path=rest_recovery order_id={order_id} "
                    f"reason=grace_expired_no_ws_event -> routing to _on_entry_filled() "
                    f"(role={role_actual})", CYAN,
                ))
                await self._on_entry_filled(role_actual, fill_price, fill_qty, order_id=order_id)
            elif role_actual == "close":
                print(color(
                    f"{now_str()} [fill-trace] path=rest_recovery order_id={order_id} "
                    f"reason=grace_expired_no_ws_event -> routing to _on_close_filled()", CYAN,
                ))
                await self._on_close_filled(fill_price, total_rp, order_id=order_id)
            elif role_actual == "partial_close":
                print(color(
                    f"{now_str()} [fill-trace] path=rest_recovery order_id={order_id} "
                    f"reason=grace_expired_no_ws_event -> routing to _apply_partial_close()", CYAN,
                ))
                await self._apply_partial_close(
                    fill_qty, fill_price, actual_rp=total_rp, actual_fee=total_fee,
                )
            return "filled"

        if status in ("NEW", "PARTIALLY_FILLED"):
            return "pending"

        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            self._order_index.pop(order_id, None)
            return "resolved_no_fill"

        # Any other/unexpected status string - never observed before, so
        # treated exactly like a REST failure: assume nothing about this
        # order rather than guessing.
        return "unknown"

    def _prune_unmatched_fills(self) -> None:
        """2026-08 fill-tracking race fix: drops any buffered unmatched-fill
        event older than _UNMATCHED_FILL_TTL_SEC. Pure memory hygiene - an
        event that old was never claimed by this process's own order
        registration (_register_order_and_replay()) and is left to
        reconciliation, exactly as before this fix."""
        if not self._unmatched_fills:
            return
        cutoff = time.time() - self._UNMATCHED_FILL_TTL_SEC
        stale = [oid for oid, (_, ts) in self._unmatched_fills.items() if ts < cutoff]
        for oid in stale:
            self._unmatched_fills.pop(oid, None)

    async def _register_order_and_replay(self, order_id: int, role: str) -> bool:
        """2026-08 fill-tracking race fix: the single place that registers a
        just-placed order's id/role into _order_index - replaces the
        previous bare `self._order_index[resp["orderId"]] = role` at each
        call site (_place_step_order(), close_position()). Immediately after
        registering, checks _unmatched_fills for a FILLED event that arrived
        over the user-data websocket before this registration could happen
        (see handle_order_update()'s untracked_order_id branch) and, if
        found, replays it through handle_order_update() right now - so
        _on_entry_filled()/_on_close_filled()/_apply_partial_close() run
        exactly as they would for a normally-ordered live fill (dca_step
        increments, status flips to OPEN/whatever the fill implies, etc.).
        No entry/exit/DCA/risk decision logic is duplicated here - this only
        closes the delivery-ordering race. Returns True if a buffered fill
        was replayed (caller must NOT then overwrite position.status/
        pending_order_id/pending_role with "still pending" values - the
        position has already moved past that)."""
        self._order_index[order_id] = role
        buffered = self._unmatched_fills.pop(order_id, None)
        if buffered is None:
            return False
        event, _ts = buffered
        print(color(
            f"{now_str()} [fill-trace] path=replayed_unmatched_fill order_id={order_id} "
            f"role={role} -> registration arrived after this order's FILLED event; replaying "
            f"it now through handle_order_update() instead of leaving it lost.", CYAN,
        ))
        await self.handle_order_update(event)
        return True

    async def handle_order_update(self, event: dict) -> None:
        o = event.get("o", {})
        order_id = o.get("i")
        # --- diagnostics only (read-only peek, no state mutated here) ------
        # Used only to log which path a FILLED event took; does not affect
        # the original control flow below, which is unchanged.
        _diag_status = o.get("X")
        _diag_trade_id = o.get("t")
        _diag_event_ms = o.get("T") or event.get("E")
        _diag_close_time = (
            trade_log_close_time_str(_diag_event_ms / 1000.0) if _diag_event_ms else trade_log_close_time_str()
        )
        # ---------------------------------------------------------------------
        if order_id not in self._order_index:
            # DIAGNOSTIC (see class docstring note on fill-path tracing below):
            # this is the exact point where a live fill can be silently lost -
            # order_id isn't in this process's in-memory _order_index (never
            # persisted across restarts), so a FILLED event that genuinely
            # arrived over the connected user-data websocket still can't be
            # routed to _on_close_filled(). Restart-safe recovery: if this was
            # this process's own pending CLOSE order before a restart/reconnect
            # wiped _order_index, the persisted DCA-state snapshot (written by
            # close_position() when the order was placed) still has its
            # order_id/role - use it to route the fill correctly instead of
            # silently dropping it until the next reconciliation pass.
            if _diag_status == "FILLED":
                recovered = await self._try_recover_close_fill(order_id, o)
                if recovered:
                    return
                # 2026-08 fill-tracking race fix: before this fix, an
                # untracked FILLED event for "initial"/"dca" (i.e. not
                # "close") was simply dropped here forever - there was no
                # recovery path for those roles, unlike close orders above.
                # Since _place_step_order() only registers order_id in
                # _order_index AFTER its `await self.client.place_order(...)`
                # REST call returns, this process's own websocket consumer
                # can process this exact order's FILLED event first (pure
                # asyncio scheduling, not necessarily a restart) - dropping
                # it left dca_step stuck and position.status stuck at
                # ENTERING/DCA_PENDING, which then made every subsequent
                # periodic-poll initialize_sync() treat the position as
                # "not synced" and rebuild it, resetting dca_step to 0 and
                # (per the RESYNCING TO MATCH EXCHANGE / EMERGENCY CLOSE
                # pattern this fixes) letting the bot re-open DCA steps it
                # had already taken. Buffer the raw event instead of
                # dropping it - _register_order_and_replay() (called from
                # _place_step_order()/close_position() the moment they learn
                # this same order_id) will replay it through this same
                # function within moments. If nothing ever claims it,
                # _prune_unmatched_fills() drops it after
                # _UNMATCHED_FILL_TTL_SEC and reconciliation remains the
                # fallback, exactly as before this fix.
                self._prune_unmatched_fills()
                if order_id is not None:
                    self._unmatched_fills[order_id] = (event, time.time())
                print(color(
                    f"{now_str()} [fill-trace] path=live_user_websocket order_id={order_id} "
                    f"trade_id={_diag_trade_id} close_time={_diag_close_time} "
                    f"reason=untracked_order_id (order_id not in local _order_index yet - buffered "
                    f"for up to {self._UNMATCHED_FILL_TTL_SEC:.0f}s in case this process's own order "
                    f"registration is still in flight; falls back to reconciliation if never claimed)",
                    YELLOW,
                ))
            return

        rp = float(o.get("rp") or 0.0)
        if rp:
            self._rp_accum[order_id] = self._rp_accum.get(order_id, 0.0) + rp

        # 2026-08 realized-PnL/fee-accounting fix: actual commission for
        # this fill. Binance's "rp" (realized profit) does NOT include
        # commission - it is a separate deduction, reported here via "n"
        # (amount) / "N" (asset). Only trusted when the commission asset is
        # USDT (the standard case for USD-M futures); a non-USDT asset
        # (BNB fee discount) marks the whole position's fee tracking
        # unreliable so _on_close_filled() falls back to the existing
        # TAKER_FEE_RATE estimate for the entire trade instead of silently
        # treating a BNB amount as USDT.
        comm = float(o.get("n") or 0.0)
        comm_asset = o.get("N")
        if comm:
            if comm_asset is None or comm_asset == "USDT":
                self._fee_accum[order_id] = self._fee_accum.get(order_id, 0.0) + comm
            else:
                self._position_fees_reliable = False
                print(color(
                    f"{now_str()} [fee-accounting] order_id={order_id} reported commission in "
                    f"{comm_asset}, not USDT - cannot trust as a USDT amount; this trade's fees "
                    f"will fall back to the TAKER_FEE_RATE estimate.", YELLOW,
                ))

        # Record-keeping only (not used by any entry/exit/DCA/risk decision):
        # tracks the highest Binance trade id this process has itself
        # observed live, so the reconciliation safety net below never
        # re-fetches/re-logs a fill this process just handled.
        trade_id = o.get("t")
        if trade_id is not None:
            try:
                _tid = int(trade_id)
                self._last_live_trade_id = max(self._last_live_trade_id, _tid)
                # 2026-08 fix B: remember where THIS position's fills start,
                # so reconciliation always fetches its entry leg together
                # with its eventual close (see
                # _open_position_reconcile_floor / from_id rewind).
                if self._open_position_first_trade_id is None:
                    self._open_position_first_trade_id = _tid
                else:
                    self._open_position_first_trade_id = min(
                        self._open_position_first_trade_id, _tid
                    )
            except (TypeError, ValueError):
                pass

        status = o.get("X")
        if status != "FILLED":
            return

        role = self._order_index.pop(order_id)
        total_rp = self._rp_accum.pop(order_id, 0.0)
        total_fee_this_order = self._fee_accum.pop(order_id, 0.0)
        fill_price = float(o.get("ap") or 0.0)
        fill_qty = float(o.get("z") or 0.0)

        # 2026-08 realized-PnL/fee-accounting fix: roll this order's actual
        # commission into the position-lifecycle accumulator. Reset first
        # on a fresh "initial" entry (defensive against any prior
        # position's accumulator not having been cleanly consumed, e.g. an
        # edge case in the finalize path) so it always starts this trade's
        # count at exactly this fill's commission, then keeps growing
        # through every subsequent DCA/partial/close fill for the same
        # trade.
        if role == "initial":
            self._position_fees_accum = 0.0
            self._position_fees_reliable = True
            self._entry_commission_accum = 0.0
            self._dca_commission_accum = 0.0
            self._exit_commission_accum = 0.0
        self._position_fees_accum += total_fee_this_order
        # 2026-08 entry-context/commission race fix: per-role breakdown,
        # diagnostic only - does not change combined_net_pnl/fees_final,
        # which still come entirely from _position_fees_accum above.
        if role == "initial":
            self._entry_commission_accum += total_fee_this_order
        elif role == "dca":
            self._dca_commission_accum += total_fee_this_order
        elif role in ("close", "partial_close", "protective_stop"):
            self._exit_commission_accum += total_fee_this_order

        if role in ("initial", "dca"):
            await self._on_entry_filled(role, fill_price, fill_qty, order_id=order_id)
        elif role == "partial_close":
            await self._apply_partial_close(
                fill_qty, fill_price, actual_rp=total_rp, actual_fee=total_fee_this_order,
            )
        elif role == "protective_stop":
            # 2026-08 protective-stop fill-routing fix (review finding 2):
            # the EXCHANGE closed this position for us (STOP_MARKET
            # closePosition=true triggered). Previously this order was never
            # registered in _order_index at all, so this event fell into the
            # "untracked_order_id" branch above and was merely buffered: the
            # bot kept believing the position was OPEN, kept managing/DCA-ing
            # a position that no longer existed, and never logged the trade.
            #
            # It is routed through the SAME _on_close_filled() every other
            # close uses, so realized PnL, commission, trade CSV/JSON
            # logging, daily counters, Brain learning and the reset to FLAT
            # all happen exactly once, through exactly one code path.
            # Exactly-once is guaranteed by the _order_index.pop() above -
            # a duplicate WebSocket/REST event for the same order_id no
            # longer finds it registered.
            #
            # The order itself is GONE from the exchange (it triggered), so
            # local tracking is cleared here WITHOUT a cancel call - there is
            # nothing left to cancel, and issuing one would just produce a
            # spurious -2011.
            self._clear_protective_stop_tracking()
            self._pending_exit_reason = "protective_stop"
            print(color(
                f"{now_str()} [fill-trace] path=live_user_websocket order_id={order_id} "
                f"trade_id={trade_id} close_time={_diag_close_time} "
                f"reason=matched_local_order_index (role=protective_stop) -> exchange-native "
                f"protective stop TRIGGERED and closed the position; routing to "
                f"_on_close_filled() with exit_reason=protective_stop", CYAN,
            ))
            await self._on_close_filled(fill_price, total_rp, order_id=order_id)
        elif role == "close":
            # DIAGNOSTIC: confirms this close IS being routed through the
            # live path, for direct comparison against [fill-trace] lines
            # from the untracked-order_id branch above and against
            # [reconcile-trace] lines below.
            print(color(
                f"{now_str()} [fill-trace] path=live_user_websocket order_id={order_id} "
                f"trade_id={trade_id} close_time={_diag_close_time} "
                f"reason=matched_local_order_index (role=close) -> routing to _on_close_filled()", CYAN,
            ))
            await self._on_close_filled(fill_price, total_rp, order_id=order_id)


    async def _on_entry_filled(self, role: str, fill_price: float, fill_qty: float, order_id: Optional[int] = None) -> None:
        # 2026-08 post-only (maker) entry execution: this order has filled,
        # so there is nothing left resting for the timeout watchdog to
        # cancel. Clearing here (rather than only in the watchdog) means a
        # maker fill can never be followed by a spurious cancel attempt
        # against an order Binance has already closed.
        if order_id is not None and self._post_only_order_id == order_id:
            self._post_only_order_id = None
            self._post_only_submitted_ts = 0.0
        self.position.entries.append((fill_price, fill_qty))
        total_notional = sum(p * q for p, q in self.position.entries)
        total_qty = sum(q for _, q in self.position.entries)
        self.position.avg_entry_price = total_notional / total_qty if total_qty else None
        self.position.total_qty = total_qty
        self.position.original_qty = total_qty
        if role == "dca":
            prior_step, prior_step_safety_reason = sanitize_recovered_dca_step(
                self.position.dca_step
            )
            if prior_step >= MAX_DCA_STEPS:
                # Apply the real fill economics once, but never let a late
                # or unexpected fill turn 2/2 into 3/2 or reopen capacity.
                self.position.dca_step = MAX_DCA_STEPS
                self.position.dca_blocked = True
                self.position.dca_block_reason = (
                    prior_step_safety_reason
                    or f"DCA fill order_id={order_id} arrived while already at "
                    f"MAX_DCA_STEPS={MAX_DCA_STEPS}"
                )
            else:
                self.position.dca_step = min(prior_step + 1, MAX_DCA_STEPS)
                if prior_step_safety_reason is not None:
                    self.position.dca_blocked = True
                    self.position.dca_block_reason = prior_step_safety_reason
            self.position.last_dca_order_id = order_id
            # 2026-08 DCA re-fire spacing fix: anchor last_dca_price to the
            # ACTUAL fill price (already available here, from Binance's
            # own reported average fill price - "ap" on the order event),
            # rather than the pre-order mark price previously used. This
            # is the value the spacing gate in _manage_open_position()
            # compares the next tick's price against before allowing
            # another DCA add.
            self.position.last_dca_price = fill_price
            # 2026-08 Max Hold Time <-> DCA debug logging: if Max Hold Time
            # V2 deferred specifically because this DCA opportunity was
            # available (set/refreshed every tick in _manage_open_position(),
            # see _max_hold_dca_defer_pending's own comment in __init__),
            # log the correlation now that the add has actually filled.
            # Always consumed (reset to False) here regardless of whether it
            # was True, so a stale value can never attach to an unrelated
            # later DCA fill. Logging-only - does not affect dca_step,
            # sizing, or any other part of this fill's handling.
            if self._max_hold_dca_defer_pending:
                print(color(
                    f"{now_str()} [dca] executed after max-hold defer "
                    f"dca_step={self.position.dca_step}/{MAX_DCA_STEPS} "
                    f"entry_price={fill_price:.2f}", CYAN,
                ))
            self._max_hold_dca_defer_pending = False
        else:
            self.position.opened_at = time.time()
            self.position.last_entry_order_id = order_id
            self.position.max_favorable_price = fill_price
            self.position.max_adverse_price = fill_price
            # Guards against a stale defer-flag from an earlier, already-
            # closed trade ever being misattributed to this brand new
            # position's own (unrelated) future DCA fills.
            self._max_hold_dca_defer_pending = False
        self.position.status = "OPEN"
        self.position.pending_order_id = None
        self.position.pending_role = None
        # 2026-08 DCA resync-race fix: stamp the fill so the periodic
        # initialize_sync() poll can recognize a short window where
        # Binance's REST position endpoint still reports the pre-fill
        # qty/avg_entry even though this fill is already confirmed locally
        # (via the user-data-stream WebSocket) - see that function's
        # OPEN-status grace block, a few lines below the existing
        # ENTERING/DCA_PENDING grace it mirrors.
        self.position.last_fill_ts = time.time()

        step_label = "INITIAL" if role == "initial" else f"DCA #{self.position.dca_step}"
        side_color = GREEN if self.position.side == "LONG" else RED
        fill_confidence = (
            self.position.entry_confidence
            if role == "initial"
            else self.last_confidence.confidence_score
        )
        print(color(
            f"{now_str()} ENTRY FILLED [{step_label}] {self.position.side} "
            f"qty={fill_qty} @ {fill_price:.2f}  ->  avg_entry={self.position.avg_entry_price:.2f}  "
            f"total_qty={self.position.total_qty}  leverage={self.leverage}x  margin={MARGIN_TYPE}  "
            f"regime={self.last_regime.regime}  entry_confidence={fill_confidence:.2f}",
            side_color,
        ))

        # Reset the peak-save throttle whenever the position's shape
        # changes via an entry/DCA fill (2026-07 DCA-state-recovery fix) -
        # a fresh fill already triggers save_dca_state() below (capturing
        # dca_step/last_dca_price/avg_entry/qty accurately), so the next
        # Profit Lock peak save should be measured relative to that fresh
        # baseline, not whatever peak happened to be saved before this fill.
        self._last_dca_state_peak_saved = self.position.peak_unrealized_pnl

        asyncio.create_task(self.save_dca_state(reason=f"{step_label} filled"))

        # item 6: (re)place the exchange-native protective stop immediately
        # after every confirmed entry/DCA fill - awaited (not fired-and-
        # forgotten) so a placement failure reliably sets
        # protection_pending=True (blocking further DCA - see
        # _manage_open_position) before this fill is considered fully
        # handled, rather than racing the next price tick.
        await self._place_or_replace_protective_stop(reason=f"{step_label} filled")

    async def _on_close_filled(self, fill_price: float, total_rp: float, order_id: Optional[int] = None) -> None:
        p = self.position
        # 2026-08 realized-PnL/fee-accounting fix: realized_pnl_total and
        # daily_realized_pnl are NO LONGER updated here per-leg with the
        # raw Binance "rp" (which excludes commission - see the finalize
        # block below for the corrected, fee-net update, which happens
        # exactly once per trade regardless of how many close legs it took).
        # 2026-08 close-verification fix: this fill's realized PnL is real,
        # permanent money regardless of whether the position turns out to
        # be fully closed yet - accumulate it now so a trade needing 2-3
        # close legs (a genuine partial fill, or a fill landing on the
        # position between the pre-close qty fetch and the order executing)
        # still produces ONE correct trade-log record for the whole trade,
        # not one truncated record per leg.
        self._closing_accumulated_rp += total_rp

        # 2026-08 close-verification fix (this block only - the finalize
        # logic below is otherwise unchanged in content, just now gated
        # behind a confirmed-flat exchange position instead of running
        # unconditionally after every fill): never assume a single FILLED
        # event means the position is fully gone from Binance. Re-fetches
        # the exchange's own positionAmt (skipped in DRY_RUN - nothing real
        # to verify, the simulated fill is authoritative there by
        # definition) and only proceeds to finalize the trade (log it,
        # delete the DCA snapshot, reset to FLAT) once it's confirmed. If a
        # meaningful remainder is still open, submits another reduceOnly
        # close for exactly that remainder and returns - waiting for THAT
        # order's own FILLED event to re-enter this same function and
        # verify again, up to CLOSE_VERIFY_MAX_RETRIES attempts. If the
        # verification fetch itself fails, this does NOT finalize either -
        # it leaves state as CLOSING/DCA-snapshot-intact and relies on the
        # independent periodic initialize_sync() resync (runs every
        # POSITION_RISK_POLL_SEC regardless) to reconcile it, rather than
        # ever guessing.
        if not DRY_RUN:
            exchange_state = await self._fetch_exchange_position()
            if exchange_state is None:
                print(color(
                    f"{now_str()} [close-verify] could not verify exchange position after this "
                    f"fill - NOT finalizing yet (state stays CLOSING, DCA snapshot NOT deleted); "
                    f"the periodic exchange resync will reconcile this.", RED,
                ))
                return
            exchange_side, remaining_qty = exchange_state
            remaining_qty_rounded = round_step(remaining_qty, self.filters.step_size)
            print(color(
                f"{now_str()} [close-verify] exchange position after fill: side={exchange_side} "
                f"qty={remaining_qty} (rounded={remaining_qty_rounded}, "
                f"step_size={self.filters.step_size})", GRAY,
            ))
            if exchange_side is not None and remaining_qty_rounded > 0 and exchange_side == p.side:
                self._closing_retry_count += 1
                if self._closing_retry_count <= CLOSE_VERIFY_MAX_RETRIES:
                    close_side = "SELL" if p.side == "LONG" else "BUY"
                    print(color(
                        f"{now_str()} [close-verify] position NOT fully closed - "
                        f"{remaining_qty_rounded} {self.symbol} remains after this fill (retry "
                        f"{self._closing_retry_count}/{CLOSE_VERIFY_MAX_RETRIES}) - submitting "
                        f"another reduceOnly close for the remainder.", YELLOW,
                    ))
                    p.total_qty = remaining_qty_rounded
                    new_order_id = await self._place_reduce_only_close_order(close_side, remaining_qty_rounded)
                    if new_order_id is None:
                        print(color(
                            f"{now_str()} [close-verify] CRITICAL: retry order placement FAILED - "
                            f"{remaining_qty_rounded} {self.symbol} remains open. Leaving position "
                            f"tracked as OPEN with the correct remaining quantity - MANUAL REVIEW "
                            f"RECOMMENDED. DCA snapshot NOT deleted.", RED,
                        ))
                        p.status = "OPEN"
                        p.pending_order_id = None
                        p.pending_role = None
                        asyncio.create_task(self.save_dca_state(reason="close retry placement failed"))
                    return  # wait for the retry order's own FILLED event (or the failure path above)
                else:
                    print(color(
                        f"{now_str()} [close-verify] CRITICAL: {remaining_qty_rounded} {self.symbol} "
                        f"still open after {CLOSE_VERIFY_MAX_RETRIES} automatic retry attempts - "
                        f"giving up automatic retries. MANUAL INTERVENTION REQUIRED. Position left "
                        f"tracked as OPEN with the remaining quantity so it is never silently lost. "
                        f"DCA snapshot NOT deleted.", RED,
                    ))
                    p.total_qty = remaining_qty_rounded
                    p.status = "OPEN"
                    p.pending_order_id = None
                    p.pending_role = None
                    asyncio.create_task(self.save_dca_state(reason="close-verify retries exhausted"))
                    return
            # exchange_side is None (confirmed flat), or reports a
            # different/opposite side (can't be attributed to closing this
            # position further - treated as flat-for-this-purpose, same as
            # initialize_sync()'s own handling), or remaining_qty_rounded
            # is a genuinely un-closeable dust amount below step_size -
            # every one of these means there is nothing more THIS close
            # sequence can or should do. Proceed to finalize below.
            print(color(f"{now_str()} [close-verify] confirmed FLAT on exchange - finalizing trade.", GRAY))

        # --- finalize: exchange confirmed flat (or DRY_RUN, where the
        # simulated fill is authoritative by definition) ------------------
        total_rp_for_record = self._closing_accumulated_rp  # raw Binance realized PnL, excludes commission
        self._closing_accumulated_rp = 0.0
        self._closing_retry_count = 0
        self.trade_count += 1

        exit_reason = getattr(self, "_pending_exit_reason", "manual")
        held_sec = time.time() - p.opened_at if p.opened_at else 0.0
        invested_notional = sum(price * qty for price, qty in p.entries) or 0.0

        # 2026-08 realized-PnL/fee-accounting fix (this block only - every
        # other part of finalize is unchanged in content, just now fed the
        # corrected combined_net_pnl below): Binance's "rp" is realized
        # PRICE PnL only and excludes commission (confirmed against
        # Binance USD-M Futures ORDER_TRADE_UPDATE/userTrades semantics -
        # "rp"/"realizedPnl" and "n"/"commission" are reported as separate
        # fields). Prefer the ACTUAL commission accumulated across this
        # trade's full lifecycle (initial entry + every DCA add + any
        # partial closes + the close leg(s)) over an estimate; fall back to
        # the existing TAKER_FEE_RATE-based estimate only if that actual
        # figure is unavailable or was flagged unreliable (a non-USDT
        # commission asset was seen - see handle_order_update()).
        if self._position_fees_reliable and self._position_fees_accum > 0:
            fees_final = self._position_fees_accum
            fee_source = "actual"
        else:
            fees_final = self.estimate_round_trip_fee_usdt(p.original_qty or p.total_qty, p.avg_entry_price or fill_price, fill_price)
            fee_source = "estimated_fallback"
        # 2026-08 entry-context/commission race fix: capture the per-role
        # breakdown for the diagnostic below before resetting it alongside
        # _position_fees_accum - diagnostic only, does not affect fees_final.
        entry_commission_for_record = self._entry_commission_accum
        dca_commission_for_record = self._dca_commission_accum
        exit_commission_for_record = self._exit_commission_accum
        self._position_fees_accum = 0.0
        self._position_fees_reliable = True
        self._entry_commission_accum = 0.0
        self._dca_commission_accum = 0.0
        self._exit_commission_accum = 0.0

        # THE FIX: true economic result = raw realized PnL minus actual
        # full-lifecycle commission - previously this was left as the raw
        # value (fees were computed but never actually subtracted here).
        combined_net_pnl = total_rp_for_record - fees_final
        pnl_color = GREEN if combined_net_pnl >= 0 else RED

        # realized_pnl_total / daily_realized_pnl now update exactly once
        # per trade, right here, with the fee-net result - not per-leg with
        # the raw value as before. MAX_DAILY_LOSS_USDT can no longer be
        # fooled by fees not being subtracted.
        self._maybe_reset_daily_loss_tracker()
        self.realized_pnl_total += combined_net_pnl
        self.daily_realized_pnl += combined_net_pnl

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
            f"{now_str()} POSITION CLOSED @ {fill_price:.2f}  PnL={combined_net_pnl:+.4f} USDT "
            f"(raw={total_rp_for_record:+.4f}, fees={fees_final:.4f} [{fee_source}])  "
            f"(DCA steps used: {p.dca_step}/{MAX_DCA_STEPS})  exit_reason={exit_reason}  "
            f"reward={reward:+.4f}  session_total={self.realized_pnl_total:+.4f}",
            pnl_color,
        ))
        # 2026-08 entry-context/commission race fix: requested final-close
        # diagnostic - breaks the actual commission total down by role, so
        # a missing/incomplete entry-side or DCA-side commission is
        # immediately visible from the logs instead of only showing up as
        # an unexplained gap in the total. Printed once per close (not
        # per-tick) alongside the existing POSITION CLOSED line. Only
        # meaningful when fee_source=="actual" - the estimated fallback is
        # a single round-trip figure with no real per-fill breakdown to show.
        if fee_source == "actual":
            print(color(
                f"{now_str()} [commission-breakdown] entry={entry_commission_for_record:.4f} "
                f"dca={dca_commission_for_record:.4f} exit={exit_commission_for_record:.4f} "
                f"total={fees_final:.4f}", GRAY,
            ))

        was_success = combined_net_pnl > 0

        # 2026-08 DYNAMIC POST-LOSS COOL-OFF: arm the anti-revenge-trading
        # window whenever this trade closed at a fee-net loss. Entry-gating
        # only (see _arm_cool_off / on_price_tick's cool-off gate) - the
        # position has just been confirmed flat here, so nothing open is or
        # can be affected. Deliberately keyed off the same combined_net_pnl
        # that drives every other outcome decision in this block, so a trade
        # the logs call a loss and a trade that triggers a cool-off are
        # always the same set. infra_only_exit is computed just below and
        # is excluded on purpose: an exit forced by a local REST/API failure
        # says nothing about market conditions, so it must not suppress
        # trading for the next 15 minutes.
        if combined_net_pnl < 0 and exit_reason not in INFRASTRUCTURE_ONLY_EXIT_REASONS:
            self._arm_cool_off(combined_net_pnl, exit_reason)

        # 2026-08 Brain-contamination fix: some exits say nothing whatsoever
        # about whether the ENTRY was a good decision - they are forced by
        # local infrastructure/API failures. The live -4120 incident is the
        # exact case: three entries were closed after exactly 300s by the
        # protective-stop fail-safe purely because Binance had migrated
        # conditional orders to the Algo Service, and all three were fed to
        # Brain as success=False. That teaches the entry model that perfectly
        # ordinary setups fail, on evidence that is really about a REST
        # endpoint.
        #
        # Such exits are excluded from BRAIN TRAINING ONLY. Everything
        # financial is deliberately untouched and still exact: realized PnL,
        # commission, the trade CSV/JSONL record, daily loss/profit counters,
        # trade_count, session totals and the exit_reason itself all record
        # the real outcome, because the money was really lost.
        # recent_trade_outcomes (a live feature input, not a label) is also
        # skipped so an infrastructure failure cannot distort the
        # recent-win-rate feature fed into future entry decisions.
        infra_only_exit = exit_reason in INFRASTRUCTURE_ONLY_EXIT_REASONS

        if not infra_only_exit:
            self.recent_trade_outcomes.append(1.0 if was_success else 0.0)
            self.recent_trade_timestamps.append(time.time())

        if p.entry_features is not None and not infra_only_exit:
            self.brain.learn_success(p.entry_features, was_success)
            self.brain.learn_quality(p.entry_features, reward)
            self._brain_dirty = True
            print(color(
                f"{now_str()} [brain] reinforced entry decision (success={was_success}, "
                f"reward={reward:+.4f}, brain_updates={self.brain.update_count})", MAGENTA,
            ))
        elif infra_only_exit:
            print(color(
                f"{now_str()} [brain] SKIPPED learning for exit_reason={exit_reason} - this is an "
                f"infrastructure/API failure, not a strategy outcome; the trade's PnL, fees, CSV "
                f"record and daily counters are unaffected and remain exact.", YELLOW,
            ))

        # 2026-08 Smart Exit diagnostics (logging-only - see attribute's own
        # comment in __init__). Only ever populated immediately before a
        # smart_exit close, and always consumed (popped) here so a later,
        # unrelated exit can never accidentally inherit stale values.
        smart_exit_diag = self._last_smart_exit_diagnostics if exit_reason == "smart_exit" else None
        self._last_smart_exit_diagnostics = None

        # --- permanent training dataset -------------------------------------------
        record = {
            "close_time": trade_log_close_time_str(),
            "symbol": self.symbol,
            "side": p.side,
            "entry_price": p.avg_entry_price,
            "exit_price": fill_price,
            "qty": p.original_qty or p.total_qty,
            "invested_notional": invested_notional,
            "gross_pnl_usdt": total_rp_for_record,
            "fees_usdt": fees_final,
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
            "exit_order_id": int(order_id) if order_id is not None else None,
            "binance_order_ids": [int(order_id)] if order_id is not None else [],
            "smart_exit_agree_count": smart_exit_diag["agree_count"] if smart_exit_diag else "",
            "smart_exit_required_agree": smart_exit_diag["required_agree"] if smart_exit_diag else "",
            "smart_exit_signals_fired": smart_exit_diag["signals_fired"] if smart_exit_diag else "",
            "smart_exit_dca_distance_pct": smart_exit_diag["dca_distance_pct"] if smart_exit_diag else "",
            # 2026-08 entry-quality audit fix (item 10): see TRADE_LOG_FIELDS's
            # own comment - this is the actual composite score/threshold
            # this trade was accepted under, distinct from entry_confidence
            # above.
            "entry_composite_score": p.entry_composite_score,
            "entry_score_threshold": p.entry_score_threshold,
            # 2026-08 per-trade net-loss budget fix (item 5): estimated
            # fee-net PnL at the instant the MAX_TRADE_NET_LOSS_USDT gate
            # triggered this close - "" for any trade that exited via a
            # different path.
            "loss_budget_trigger_est_net_pnl": (
                p.trade_loss_budget_trigger_pnl if p.trade_loss_budget_trigger_pnl is not None else ""
            ),
        }
        self._log_completed_trade(record)

        # item 6: the position is confirmed flat (we only reach here once
        # _fetch_exchange_position() above confirmed it, or in DRY_RUN) -
        # cancel any resting protective stop now so TP/Hard-Stop/Profit-
        # Lock/Smart-Exit/max-hold/manual closes can never leave one
        # orphaned on the exchange.
        if p.protective_stop_algo_id is not None:
            confirmed_gone = await self._cancel_protective_stop(
                reason=f"position closed ({exit_reason})"
            )
            # 2026-08 cancel-confirmation fix (review finding 5): the
            # PositionState below is discarded, so an unconfirmed cancel
            # would lose the only record of a possibly-still-resting order.
            # Hand it to the manager-level orphan sweep instead.
            if not confirmed_gone and p.protective_stop_algo_id is not None:
                self._orphan_protective_algo_ids.add(p.protective_stop_algo_id)
                print(color(
                    f"{now_str()} [protective-stop] orderId={p.protective_stop_algo_id} could not "
                    f"be confirmed cancelled while closing - handing to the orphan sweep so it is "
                    f"retried until Binance accepts the cancel or proves it is gone.", YELLOW,
                ))

        self.position = PositionState(last_close_time=time.time())
        # New (flat) position - reset the peak-save throttle so the next
        # trade's Profit Lock starts measuring peak growth from zero
        # (2026-07 DCA-state-recovery fix).
        self._last_dca_state_peak_saved = 0.0
        # 2026-08 fix B: this trade is finalized and logged - its entry-leg
        # reconciliation floor must not carry into the next position.
        self._open_position_first_trade_id = None

        asyncio.create_task(self.persist_brain(reason="trade closed"))
        asyncio.create_task(self.sync_trade_log_to_github())
        asyncio.create_task(self.save_flat_dca_state(reason="trade closed"))
        if self._last_live_trade_id:
            asyncio.create_task(self._persist_trade_sync_cursor(
                self._last_live_trade_id, reason="live close"
            ))


# ============================================================================
# INSTANT WARM-UP (historical klines -> candle buffer -> live websocket)
# ============================================================================


async def warm_up_candles_from_klines(
    client: RestClient,
    manager: MartingaleManager,
    limit: int = KLINE_WARMUP_LIMIT,
    interval: str = KLINE_WARMUP_INTERVAL,
) -> int:
    """2026-08 instant warm-up fix - the startup-delay half of the live
    incident this build addresses.

    Before this, the candle series backing ATR / EMA / regime was built
    exclusively from the live tick stream, so a fresh container logged
    "[entry-skip] startup warm-up: insufficient market history
    (candles=5/57)" for the better part of an hour before it could consider
    a single entry. This runs ONE REST call
    (GET /fapi/v1/klines, weight 1 at limit<=100) at initialization, seeds
    the buffer with the last `limit` CLOSED candles, and then leaves the
    series to the websocket stream for every subsequent real-time update -
    instant indicators, unchanged live precision.

    Called once from dca2.main(), before the websocket consumers start.
    Best-effort by design: any failure (network, API error, an active REST
    cooldown, a malformed payload) is logged and swallowed, leaving the bot
    in exactly the old stream-only warm-up behavior rather than blocking
    startup. Returns the number of candles seeded (0 if it did nothing).
    """
    if not KLINE_WARMUP_ENABLED:
        print(color(
            "[warmup] KLINE_WARMUP_ENABLED=false - skipping historical kline seed, "
            "the candle buffer will warm up from the live stream only.", GRAY,
        ))
        return 0

    is_cooldown_active = getattr(client, "is_cooldown_active", None)
    if is_cooldown_active is not None and is_cooldown_active():
        print(color(
            "[warmup] REST cooldown active - skipping the historical kline seed "
            "(the live stream still warms the buffer up as before).", YELLOW,
        ))
        return 0

    needed = max(EMA_SLOW, ATR_PERIOD) + 2
    try:
        klines = await client.get_klines(manager.symbol, interval=interval, limit=limit)
    except Exception as e:  # noqa: BLE001 - warm-up is best-effort; never block startup
        print(color(
            f"[warmup] historical kline fetch failed ({e}) - falling back to "
            f"live-stream-only warm-up (~{needed} minutes before entries unlock).", YELLOW,
        ))
        return 0

    seeded = manager.candles.prime_from_klines(klines)
    if seeded <= 0:
        print(color(
            f"[warmup] historical kline fetch returned no usable candles "
            f"(rows={len(klines) if klines else 0}) - falling back to "
            f"live-stream-only warm-up.", YELLOW,
        ))
        return 0

    # Evaluate the regime immediately off the freshly-seeded series so
    # last_regime carries a real ATR reading (not the default atr_pct=0.0)
    # from the very first tick - the entry warm-up gate checks BOTH the
    # candle count and atr_pct > 0.
    candles = manager.candles.all_candles_incl_live()
    try:
        manager.last_regime = manager.regime_engine.evaluate(candles)
    except Exception as e:  # noqa: BLE001 - a regime hiccup must not block startup
        print(color(f"[warmup] initial regime evaluation failed ({e}) - continuing.", YELLOW))

    ready = len(candles) >= needed
    print(color(
        f"[warmup] seeded {seeded} historical {interval} candle(s) from a single REST call "
        f"(buffer={len(candles)}/{needed} needed, regime={manager.last_regime.regime}, "
        f"atr%={manager.last_regime.atr_pct * 100:.3f}) - "
        f"{'indicators are warm, entries unlock as soon as Brain V2 is ready' if ready else 'still short of the indicator minimum, the live stream will finish the warm-up'}. "
        f"Live websocket updates take over from here.",
        GREEN if ready else YELLOW,
    ))
    return seeded


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
    their dataclass defaults automatically via PositionState()) - EXCEPT
    profit_lock_active/peak_unrealized_pnl, which are now explicitly
    carried forward on a matching same-side OPEN resync (see 2026-07
    Profit Lock resync fix in the module docstring).

    2026-07 reconcile-throttle fix (this function only - see module
    docstring): reconcile_trade_history_from_exchange() (a GET
    /fapi/v1/userTrades REST call) is now SKIPPED for as long as the
    current position has a close order already pending (status ==
    "CLOSING" with a pending_order_id set) - covers both a normal close
    in flight and the restart-safe pending-close recovery path (see
    initialize_sync()'s own pending-CLOSE restore below, and
    _try_recover_close_fill()). While that close hasn't resolved on
    Binance's side yet, reconciliation cannot find anything new, so
    calling it on every position_risk_poller tick (every
    POSITION_RISK_POLL_SEC) was purely redundant load against an already
    rate-limited/slow endpoint - the source of the repeated 502/504s seen
    after the pending-close recovery fix. As soon as status leaves
    CLOSING (fill processed, or the next startup/reconnect call), the
    very next invocation of this function resumes reconciliation exactly
    as before - no closed trade is ever permanently skipped, and nothing
    else in this function (already_synced check, OPEN/CLOSING position
    rebuild, DCA-state snapshot restore, Profit Lock carry-forward) is
    touched.

    Note (2026-07 reconcile-backoff fix): reconcile_trade_history_from_exchange()
    itself now also self-throttles on Binance-side 502/504/timeout failures
    (see that method) - this function's own pending-close throttle above is
    unchanged and still applies first; the two throttles are independent
    and complementary, not a replacement for one another.

    DCA-STATE RECOVERY (side/qty/avg_entry-gated restore of dca_step /
    last_dca_price / profit_lock_active / peak_unrealized_pnl - see the
    "snapshot" block below): unchanged by the 2026-07 DCA-state-recovery
    fix. That fix only changed WHEN save_dca_state() is called (now also on
    Profit Lock activation/peak growth, not just on entry fills/close) and
    what _dca_state_snapshot() additionally records (initial_entry_price,
    audit-only). The compare-and-restore logic here still requires side,
    qty, and avg_entry_price to all match the exchange's reported position
    (within a small tolerance) before ANY snapshot field is trusted; on any
    mismatch the snapshot is discarded entirely and dca_step/last_dca_price/
    profit_lock_active/peak_unrealized_pnl all start fresh at their
    PositionState() defaults, exactly as before.

    Not touched by the 2026-07 final-DCA gate fix either - that fix is
    scoped entirely to the final-DCA decision inside
    _manage_open_position() and has no effect on position sync/recovery."""
    if DRY_RUN:
        return  # nothing real to sync against

    # Trade-log reliability safety net - see reconcile_trade_history_from_exchange()
    # docstring. Runs on every startup / websocket-reconnect / periodic poll that
    # already calls this function, so no new timer is introduced. Independent of
    # the position-sync logic below: never touches PositionState.
    #
    # EXCEPTION (2026-07 reconcile-throttle fix, see function docstring above):
    # while a close order is already pending for the current position, skip
    # this REST call entirely - the lifecycle can't have closed on Binance's
    # side yet, so re-fetching userTrades every ~10s here only piles up
    # redundant calls against an endpoint that was already returning
    # 502/504s. Reconciliation resumes automatically the next time this
    # function runs after status leaves CLOSING.
    pending_close_in_flight = (
        manager.position.status == "CLOSING"
        and manager.position.pending_order_id is not None
    )
    if pending_close_in_flight:
        print(color(
            f"{now_str()} [sync:{context}] [reconcile] skipping userTrades reconciliation - "
            f"close order (id={manager.position.pending_order_id}) already pending for the "
            f"current position; will resume once it resolves.", GRAY,
        ))
    else:
        # 2026-08 hardening (fix C follow-up): this safety-net call is
        # bookkeeping only - it must never be able to abort initialize_sync()
        # and propagate out of the position-risk poller, whose own except
        # clause only covers BinanceApiError/ClientError/TimeoutError. An
        # unexpected error here would otherwise escape to the supervisor and
        # restart the whole bot over a trade-log refresh. The method already
        # handles its own API failures internally; this guards the rest.
        try:
            await manager.reconcile_trade_history_from_exchange(context=context)
        except Exception as e:  # noqa: BLE001 - bookkeeping must never abort a state sync
            print(color(
                f"{now_str()} [sync:{context}] trade-history reconciliation raised "
                f"({e}) - continuing with position sync; the trade log retries next pass.",
                YELLOW,
            ))

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
    # 2026-08 position_sync_ready timing fix (comment only - this is where
    # `rows` becomes authoritative, but readiness is deliberately NOT set
    # here anymore): setting it this early let on_price_tick()/
    # _manage_open_position() treat local state as trustworthy the instant
    # rows arrived, even though reconciliation - pending-order recovery,
    # snapshot matching, and installing the final PositionState - hasn't
    # happened yet. position_sync_ready is now set True at each of the
    # THREE points below where this function has genuinely finished
    # reconciling and a final, authoritative PositionState is already in
    # place, with zero `await` between installing that state and setting
    # the flag: (1) the already_synced short-circuit just below, (2) the
    # exchange-confirmed-flat branch inside `if row is None:` above, and
    # (3) the very end of this function, after the full rebuild. Every
    # OTHER return in this function (grace-wait / pending / unknown REST
    # resolution / ambiguous DCA-state) represents reconciliation that is
    # NOT yet finished, and deliberately leaves position_sync_ready
    # untouched - it stays False on a first sync, and stays whatever it
    # already was (True) on a later poll, exactly per the "never reset an
    # already-True flag over a transient/ambiguous condition" requirement.

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
            # 2026-08 REST order-status fallback (the primary fix for the
            # missed-fill/over-DCA bug): grace has elapsed with no
            # WebSocket fill event AND the exchange now shows flat, which
            # is exactly the shape of a missed CLOSE fill (or, for a fresh
            # ENTERING order, a rejected/never-placed entry). Resolve the
            # exact pending order_id via REST before assuming anything -
            # never fall straight to "reset to FLAT" (which would silently
            # discard dca_step for an ENTERING/DCA_PENDING order that
            # actually filled but whose event was lost) or straight to
            # "genuine mismatch" without checking.
            if p.pending_order_id is not None:
                resolution = await manager._resolve_pending_order_via_rest(
                    client, p.pending_order_id, p.pending_role or "unknown", context,
                )
                if resolution == "filled":
                    # Already routed through _on_entry_filled()/_on_close_filled()
                    # inside the resolver - position state has already advanced;
                    # nothing further to do this cycle.
                    return
                if resolution == "pending":
                    print(color(
                        f"{now_str()} [sync:{context}] order_id={p.pending_order_id} still "
                        f"NEW/PARTIALLY_FILLED per REST - continuing to wait, no resync/DCA yet.",
                        GRAY,
                    ))
                    return
                if resolution == "unknown":
                    # Exchange currently shows FLAT here - an ambiguous
                    # REST result gives no basis to decide whether this
                    # order filled, was rejected, or is still in flight.
                    # Do NOT reset to FLAT (that would let a brand new
                    # entry be evaluated immediately while this order's
                    # true outcome is still unknown - risking two
                    # concurrent, conflicting positions) and do NOT clear
                    # pending_order_id/pending_role - leave state exactly
                    # as-is and retry this same REST check on the next
                    # periodic poll, exactly like the "pending" case.
                    print(color(
                        f"{now_str()} [sync:{context}] order_id={p.pending_order_id} could not be "
                        f"confirmed via REST while exchange shows flat - leaving state as-is, "
                        f"will retry next poll rather than guessing.", RED,
                    ))
                    return
                # resolution == "resolved_no_fill" (CANCELED/EXPIRED/REJECTED):
                # this order never added anything - clear pending bookkeeping
                # and fall through to the normal flat/reset handling below.
                p.pending_order_id = None
                p.pending_role = None
        # 2026-08 DCA resync-race fix (exchange-flat variant): an initial
        # fill can move status straight to "OPEN" (see the OPEN-status
        # grace block further below, which this mirrors) BEFORE Binance's
        # REST positionRisk call reflects it at all - i.e. `row is None`
        # (no open position visible yet), not just a stale/smaller qty.
        # Without this check, a periodic poll landing in that window would
        # wipe the just-confirmed OPEN position back to FLAT, discarding
        # entries/avg_entry_price/dca_step for a position that genuinely
        # exists on the exchange. Reuses the same last_fill_ts stamp and
        # SYNC_PENDING_GRACE_SEC window as that later block - once the
        # grace elapses, a still-flat exchange falls through to the normal
        # reset-to-FLAT handling immediately below, unchanged.
        if p.status == "OPEN" and p.last_fill_ts > 0.0:
            age = time.time() - p.last_fill_ts
            if age < SYNC_PENDING_GRACE_SEC:
                print(color(
                    f"{now_str()} [sync:{context}] exchange reports NO open position yet, but a "
                    f"fill was confirmed locally only {age:.1f}s ago (< {SYNC_PENDING_GRACE_SEC}s "
                    f"grace) - local qty={p.total_qty} dca_step={p.dca_step}. REST position data "
                    f"hasn't caught up yet - waiting for a later sync instead of resetting to FLAT.",
                    GRAY,
                ))
                return
        if p.status != "FLAT":
            # 2026-08 close-accounting fix (fix C - the step that turned the
            # LIVE 18:07:48 incident from "a fill we failed to route" into
            # "a trade that never existed").
            #
            # Resetting straight to a blank PositionState() throws away the
            # ONLY record of the position the fill belonged to: side, entry
            # price, qty, opened_at, dca_step. Once that is gone nothing can
            # attribute the close, so no trade is logged, no CSV row is
            # written, and no GitHub push is triggered - which is exactly
            # what happened live (status went OPEN -> FLAT with trades=0 and
            # session_pnl=+0.0000 six seconds after the stop filled).
            #
            # The exchange going flat under an OPEN local position IS a
            # close - by definition. So before discarding the state, give
            # reconciliation one authoritative pass against Binance's own
            # userTrades while the position (and therefore the entry-leg
            # floor from fix B) is still intact. If it finds the lifecycle,
            # the trade is logged/counted/pushed exactly as a normal close
            # would be. Reconciliation is idempotent and deduped by order
            # id, so a close the live path already handled is never
            # double-counted here.
            print(color(
                f"{now_str()} [sync:{context}] exchange reports NO open position, but local "
                f"state was status={p.status} side={p.side} qty={p.total_qty} - the position "
                f"closed on the exchange. Reconciling against Binance trade history BEFORE "
                f"resetting local state, so the closed trade is recorded rather than lost.",
                YELLOW,
            ))
            try:
                await manager.reconcile_trade_history_from_exchange(
                    context=f"{context}:position-closed-on-exchange"
                )
            except Exception as e:  # noqa: BLE001 - a reset must never be blocked by bookkeeping
                print(color(
                    f"{now_str()} [sync:{context}] pre-reset reconciliation failed ({e}) - "
                    f"resetting to FLAT anyway; the trade-log safety net will retry on a later "
                    f"pass now that the cursor was not advanced past it.", RED,
                ))
            print(color(
                f"{now_str()} [sync:{context}] resetting to FLAT so the bot can evaluate a "
                f"fresh entry instead of waiting on a fill that won't arrive.", YELLOW,
            ))
            manager.position = PositionState(last_close_time=time.time())
            # 2026-08 fix B: this position is finished - its entry-leg floor
            # must not leak into the NEXT position's reconciliation window.
            manager._open_position_first_trade_id = None
        # 2026-08 position_sync_ready timing fix (this line only): the
        # exchange has now been confirmed genuinely flat (either local
        # state already agreed, or it was just reset to FLAT immediately
        # above with no await since) - this IS a final, authoritative
        # state. No await between the assignment above and this line.
        manager.position_sync_ready = True
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
        and entry_price > 0
        and abs(p.avg_entry_price - entry_price) / entry_price < 0.001
    )
    if already_synced:
        # 2026-08 position_sync_ready timing fix (this line only): local
        # state already exactly matches the exchange's authoritative
        # report - a final, confirmed state with nothing to rebuild. No
        # await between this check and setting the flag.
        manager.position_sync_ready = True
        return

    # 2026-08 DCA_PENDING-resync fix: mirrors the SYNC_PENDING_GRACE_SEC
    # allowance already used a few lines up for the exchange-flat case.
    # A position that is ENTERING/DCA_PENDING with its own order still
    # inside the grace window is EXPECTED to look "not yet synced" for a
    # few seconds while that order's FILLED event is still in flight (see
    # handle_order_update()'s untracked_order_id buffering and
    # _register_order_and_replay() above, which is the primary fix for
    # this) - it is not evidence of a stale/corrupted position, and
    # rebuilding it here would reset dca_step to 0 for no reason (the
    # exact "RESYNCING TO MATCH EXCHANGE ... dca_step reset to 0" pattern
    # this fixes). Only falls through to the full rebuild below once that
    # grace window has actually elapsed - same as the flat-case check -
    # and every other branch of this function (genuine mismatch,
    # reconnect/restart resync, DCA-state snapshot restore, Profit Lock
    # carry-forward) is untouched.
    if p.status in ("ENTERING", "DCA_PENDING") and p.side == side:
        age = time.time() - p.pending_order_ts
        if age < SYNC_PENDING_GRACE_SEC:
            print(color(
                f"{now_str()} [sync:{context}] exchange shows {side} qty={qty} but local "
                f"order ({p.status}) was placed only {age:.1f}s ago (< {SYNC_PENDING_GRACE_SEC}s "
                f"grace) - waiting for its FILLED event instead of resyncing early.", GRAY,
            ))
            return
        # 2026-08 REST order-status fallback (this exact case is the
        # observed live failure this patch fixes): grace has elapsed,
        # exchange qty/avg_entry has ALREADY moved (a DCA/entry fill did
        # happen on Binance's side) but this process never saw the
        # ORDER_TRADE_UPDATE event for it. Resolve the exact pending
        # order_id via REST BEFORE ever treating this as a "snapshot
        # doesn't match exchange" mismatch - a confirmed FILLED here
        # advances dca_step through the normal idempotent fill path
        # instead of falling through to the full rebuild below (which
        # would otherwise only trust dca_step from a DCA-state snapshot
        # saved BEFORE this exact fill, i.e. one step stale, or reset it
        # to 0 entirely if no snapshot matches).
        if p.pending_order_id is not None:
            resolution = await manager._resolve_pending_order_via_rest(
                client, p.pending_order_id, p.pending_role or "unknown", context,
            )
            if resolution == "filled":
                return  # already advanced via _on_entry_filled() inside the resolver
            if resolution == "pending":
                print(color(
                    f"{now_str()} [sync:{context}] order_id={p.pending_order_id} still "
                    f"NEW/PARTIALLY_FILLED per REST - continuing to wait, no resync/DCA yet.",
                    GRAY,
                ))
                return
            if resolution == "unknown":
                print(color(
                    f"{now_str()} [sync:{context}] [dca-safety-block] order_id="
                    f"{p.pending_order_id} could not be confirmed via REST - this position will "
                    f"be resynced from the exchange but further DCA is blocked rather than "
                    f"resetting dca_step to 0.", RED,
                ))
                manager._pending_dca_block_reason = (
                    f"REST resolution of order_id={p.pending_order_id} was ambiguous/failed"
                )
            # resolution == "resolved_no_fill": this order never filled -
            # clear pending bookkeeping and fall through to the normal
            # rebuild below using whatever the exchange actually reports
            # (unchanged position if it never filled at all).
            p.pending_order_id = None
            p.pending_role = None

    # 2026-08 CLOSING-resync opened_at fix (this branch only - every other
    # branch of this function is untouched): a position that is CLOSING
    # with its own close order still pending, whose side/qty/avg_entry
    # still match what the exchange reports RIGHT NOW, is the SAME active
    # position with a close order genuinely still in flight - Binance
    # simply hasn't processed the FILLED event yet by the time this
    # periodic-poll/reconnect resync ran. This is not evidence of a
    # stale/corrupted position (unlike a genuine side/qty/avg_entry
    # mismatch, which still falls through to the full rebuild below).
    # Before this fix, CLOSING was not treated as an in-flight state the
    # way ENTERING/DCA_PENDING are above, so this exact situation fell
    # through to the full PositionState rebuild further down, which
    # unconditionally sets opened_at=time.time() - overwriting the real
    # entry timestamp with "now". When the close fill event then arrived
    # moments later, _on_close_filled()'s held_sec = time.time() -
    # p.opened_at computed against that just-reset timestamp instead of
    # the real one, producing a near-zero (e.g. ~0.05s) holding_time_sec
    # in the trade log for a position that may have been open for hours.
    # Skipping the rebuild entirely here (rather than rebuilding and only
    # patching opened_at) preserves opened_at AND every other in-memory
    # field untouched, and does not change how the eventual close fill is
    # routed - pending_order_id/pending_role/status all stay exactly as
    # they already were, so _try_recover_close_fill() /
    # handle_order_update()'s normal tracked-order-id path both continue
    # to work exactly as before.
    if (
        p.status == "CLOSING"
        and p.pending_order_id is not None
        and p.side == side
        and p.avg_entry_price is not None
        and abs(p.total_qty - qty) < max(manager.filters.step_size, 1e-9)
        and entry_price > 0
        and abs(p.avg_entry_price - entry_price) / entry_price < 0.001
    ):
        # 2026-08 REST order-status fallback: unchanged quiet-wait behavior
        # while inside grace (still the common, expected case - a close
        # fill is normally only milliseconds to a couple of seconds away).
        # Only once SYNC_PENDING_GRACE_SEC has elapsed with no
        # ORDER_TRADE_UPDATE for this exact close order is a REST lookup
        # attempted - this is what lets a missed close FILLED event be
        # processed through the normal _on_close_filled() bookkeeping
        # (trade logged, DCA snapshot deleted, position reset to FLAT)
        # instead of only ever being caught later by
        # reconcile_trade_history_from_exchange()'s exit_reason=
        # reconciled_from_exchange fallback.
        age = time.time() - p.pending_order_ts
        if age < SYNC_PENDING_GRACE_SEC:
            print(color(
                f"{now_str()} [sync:{context}] exchange still shows {side} qty={qty} but a close "
                f"order (id={p.pending_order_id}) is already pending for this exact position "
                f"(side/qty/avg_entry all match) - its FILLED event just hasn't arrived yet. "
                f"Leaving local state (including opened_at) untouched instead of rebuilding.", GRAY,
            ))
            return
        resolution = await manager._resolve_pending_order_via_rest(
            client, p.pending_order_id, p.pending_role or "close", context,
        )
        if resolution == "filled":
            return  # already routed through _on_close_filled() inside the resolver
        if resolution == "pending":
            print(color(
                f"{now_str()} [sync:{context}] close order_id={p.pending_order_id} still "
                f"NEW/PARTIALLY_FILLED per REST - continuing to wait.", GRAY,
            ))
            return
        if resolution == "unknown":
            # Ambiguous REST result for a close order: never force-close
            # dca_step/state here - just leave status=CLOSING untouched
            # (no DCA can fire while CLOSING regardless) and let the next
            # periodic poll retry this same REST check. Risk-reducing exits
            # (Hard Stop/emergency close) are unaffected since they only
            # ever act on OPEN positions, and this position was already in
            # the middle of exiting.
            print(color(
                f"{now_str()} [sync:{context}] close order_id={p.pending_order_id} could not be "
                f"confirmed via REST - leaving CLOSING state as-is, will retry next poll.", YELLOW,
            ))
            return
        # resolution == "resolved_no_fill": the close order itself was
        # CANCELED/EXPIRED/REJECTED (never filled) - clear pending
        # bookkeeping and fall through to the normal rebuild below so
        # _manage_open_position() can evaluate a fresh close attempt on
        # this still-open position instead of being stuck in CLOSING
        # forever waiting for an order that will never fill.
        p.pending_order_id = None
        p.pending_role = None

    # 2026-08 DCA resync-race fix (this block only - every other branch of
    # this function, including the ENTERING/DCA_PENDING grace above and
    # the genuine-mismatch rebuild below, is untouched): _on_entry_filled()
    # moves a position straight to status="OPEN" the instant a fill is
    # confirmed locally (via the user-data-stream WebSocket), which is
    # BEFORE the ENTERING/DCA_PENDING grace above can apply. Binance's own
    # REST GET /fapi/v1/positionRisk endpoint (what this function polls)
    # can still echo the PRE-fill qty/avg_entry for a second or two after
    # that - observed on Testnet: DCA #2 filled locally (total_qty=3.28,
    # dca_step=2), then ~1.5s later a periodic poll landed mid-lag, saw
    # the exchange still reporting the pre-DCA qty=1.94, treated it as a
    # genuine mismatch, and rebuilt the position with dca_step reset to 0
    # - which then let the bot re-fire a DCA it had already placed.
    #
    # Mirrors the ENTERING/DCA_PENDING grace above using the SAME
    # SYNC_PENDING_GRACE_SEC window (reusing p.last_fill_ts, stamped by
    # _on_entry_filled() at the moment of the fill, instead of a new ENV
    # var): while inside that window, a same-side exchange report whose
    # qty has NOT yet reached what we already hold locally is exactly the
    # shape of REST lag, not a genuine mismatch - Binance never reports
    # LESS than reality, only stale/old reality. Skip the rebuild and wait
    # for a later poll instead; the very next call resumes normal
    # already_synced / genuine-mismatch handling automatically once the
    # exchange catches up OR once the grace window itself elapses. A
    # same-side report with a qty that already MEETS or EXCEEDS local
    # (or a different side) is never treated as lag and still falls
    # through to the full rebuild/genuine-mismatch path below, unchanged.
    if (
        p.status == "OPEN"
        and p.side == side
        and p.last_fill_ts > 0.0
        and (time.time() - p.last_fill_ts) < SYNC_PENDING_GRACE_SEC
        and qty < p.total_qty
    ):
        fill_age = time.time() - p.last_fill_ts
        print(color(
            f"{now_str()} [sync:{context}] exchange still shows {side} qty={qty} (avg={entry_price:.4f}) "
            f"but a fill was confirmed locally only {fill_age:.1f}s ago (< {SYNC_PENDING_GRACE_SEC}s "
            f"grace) - local qty={p.total_qty} dca_step={p.dca_step}. REST position data hasn't "
            f"caught up yet - waiting for a later sync instead of rebuilding.", GRAY,
        ))
        return

    # Preserve Profit Lock state across this resync - ONLY if the local
    # state being replaced was itself an OPEN position on the SAME side as
    # what the exchange now reports (i.e. this is the same trade continuing
    # through a reconnect/resync, not a stale/closed trade being replaced by
    # an unrelated new position). Any other case (FLAT, ENTERING, opposite
    # side, etc.) is treated as "no prior lock state to carry forward" and
    # the rebuilt position starts with Profit Lock inactive, exactly as
    # PositionState()'s own defaults already specify. This covers a resync
    # WITHIN the same running process (in-memory p is still populated).
    preserved_profit_lock_active = p.status == "OPEN" and p.side == side and p.profit_lock_active
    preserved_peak_unrealized_pnl = p.peak_unrealized_pnl if preserved_profit_lock_active else 0.0

    # DCA state snapshot restore - covers the case the in-memory preservation
    # above cannot (a full process restart, where `p` above is already a
    # fresh/default PositionState with nothing to preserve). Validated
    # strictly against what the exchange is reporting RIGHT NOW: side, qty,
    # and avg_entry_price must all match (small tolerance) or the snapshot is
    # treated as stale/unrelated and ignored entirely - only dca_step,
    # last_dca_price, profit_lock_active, peak_unrealized_pnl, opened_at, and
    # (for a pending CLOSE only - see below) pending_order_id/pending_role
    # are ever taken from it; nothing else about position sizing/entries is
    # touched.
    restored_dca_step = 0
    restored_last_dca_price: Optional[float] = None
    restored_pending_order_id: Optional[int] = None
    restored_pending_role: Optional[str] = None
    restored_opened_at: Optional[float] = None
    restored_dca_history: Optional[list] = None
    restored_last_entry_order_id: Optional[int] = None
    restored_last_dca_order_id: Optional[int] = None
    restored_position_fees_accum: float = 0.0
    restored_position_fees_reliable: bool = True
    # 2026-08 hard DCA safety invariant: conservative default is BLOCKED,
    # not "DCA allowed" - only cleared below once a snapshot's own
    # side/qty/avg_entry_price genuinely match what the exchange reports
    # right now (the same trusted-snapshot gate every other restored field
    # already uses). A brand-new position (no snapshot needed at all -
    # entry_price/qty came straight from `row`, no prior DCA activity
    # possible) also clears this immediately below, since there is nothing
    # to lose track of yet.
    restored_dca_blocked = True
    restored_dca_block_reason: Optional[str] = "no matching DCA-state snapshot restored for this position"
    # item 6 - exchange-native protective stop: conservative defaults are
    # "no confirmed protective stop, PROTECTION_PENDING" - identical
    # reasoning to restored_dca_blocked above. Only cleared/populated below
    # once a snapshot's own side/qty/avg_entry_price genuinely match what
    # the exchange reports right now. reconcile_protective_stop_on_startup()
    # (dca2.py, called once after initialize_sync() completes) is the
    # actual authority that confirms/repairs this against Binance's own
    # open orders - this restore is only a best-effort head start so that
    # call has less work to do, never a substitute for it.
    restored_protective_stop_algo_id: Optional[int] = None
    restored_protective_stop_price: Optional[float] = None
    restored_protective_stop_client_algo_id: Optional[str] = None
    restored_protective_stop_actual_order_id: Optional[int] = None
    restored_protective_stop_cancel_pending: bool = False
    restored_protective_stop_legacy_seen: bool = False
    restored_protection_pending = True
    restored_protection_pending_reason: Optional[str] = "not yet reconciled against exchange open orders"
    restored_protection_pending_since: Optional[float] = None
    snapshot_restored = False
    # A confirmed OPEN mismatch is now entering the authoritative rebuild
    # path, which contains awaits. Earlier failure/grace/pending/ambiguous
    # paths have already returned above; only this actual rebuild window
    # temporarily withholds provisional-economics-dependent actions.
    manager.position_sync_ready = False
    snapshot = await manager.load_dca_state_snapshot()

    # 2026-08 DCA state recovery hardening: strict FLAT-snapshot
    # validation. A snapshot that explicitly claims status="FLAT" must have
    # FLAT-consistent economics (side=None, qty=0, avg_entry_price=0,
    # dca_step=0, dca_history=[]). If it claims FLAT but still carries old
    # position data - e.g. a GitHub backup written before save_flat_dca_state()
    # existed, or any other way a stale snapshot could survive a close -
    # it is corrupted and must not be trusted for ANY field, not just the
    # ones that happen to fail a side/qty/price comparison against
    # whatever the exchange currently shows. Treated identically to "no
    # snapshot at all" below, which already safely rebuilds from the
    # exchange. Snapshots without a "status" key (written before this fix)
    # or with any status other than "FLAT" are completely unaffected and
    # continue through the existing match logic further down, unchanged.
    if snapshot is not None and snapshot.get("status") == "FLAT":
        flat_qty = snapshot.get("qty")
        flat_avg_entry = snapshot.get("avg_entry_price")
        flat_dca_step = snapshot.get("dca_step")
        flat_history = snapshot.get("dca_history")
        flat_side = snapshot.get("side")
        try:
            is_consistent_flat = (
                flat_side is None
                and (flat_qty is None or float(flat_qty) == 0.0)
                and (flat_avg_entry is None or float(flat_avg_entry) == 0.0)
                and (flat_dca_step is None or int(flat_dca_step) == 0)
                and not flat_history
            )
        except (TypeError, ValueError):
            is_consistent_flat = False
        if not is_consistent_flat:
            print(color(
                "[dca-state] invalid FLAT snapshot detected - clearing stale position fields",
                RED,
            ))
            snapshot = None

    if snapshot is not None:
        snap_source = snapshot.get("_snapshot_source", "local")
        # 2026-08 DCA-state GitHub persistence fix: symbol is an additional
        # (optional, backward-compatible) validation dimension alongside
        # side/qty/avg_entry_price below - a snapshot predating this fix
        # simply won't have the key, so it's treated as "ok" rather than
        # rejected, exactly like opened_at's own backward-compatible
        # fallback above.
        snap_symbol = snapshot.get("symbol")
        symbol_ok = snap_symbol is None or snap_symbol == manager.symbol
        snap_side = snapshot.get("side")
        snap_qty = snapshot.get("qty")
        snap_avg_entry = snapshot.get("avg_entry_price")
        qty_tol = max(manager.filters.step_size, 1e-9) * 2
        qty_ok = snap_qty is not None and abs(float(snap_qty) - qty) <= qty_tol
        price_ok = (
            snap_avg_entry is not None and entry_price > 0
            and abs(float(snap_avg_entry) - entry_price) / entry_price < 0.001
        )
        if snap_side == side and qty_ok and price_ok and symbol_ok:
            restored_dca_step, recovered_step_safety_reason = sanitize_recovered_dca_step(
                snapshot.get("dca_step", 0)
            )
            restored_last_dca_price = snapshot.get("last_dca_price")
            preserved_profit_lock_active = bool(snapshot.get("profit_lock_active", False))
            preserved_peak_unrealized_pnl = float(snapshot.get("peak_unrealized_pnl", 0.0) or 0.0)
            # 2026-08 realized-PnL/fee-accounting fix: restore actual
            # commission paid so far this trade's lifecycle - older
            # snapshots predating this field simply won't have the key, so
            # this safely defaults to 0.0/reliable=True, identical to a
            # genuinely fresh trade (falls back to the TAKER_FEE_RATE
            # estimate at close time in that case, exactly as it always did
            # before this fix).
            try:
                restored_position_fees_accum = float(snapshot.get("position_fees_accum", 0.0) or 0.0)
            except (TypeError, ValueError):
                restored_position_fees_accum = 0.0
            restored_position_fees_reliable = bool(snapshot.get("position_fees_reliable", True))
            # 2026-08 hard DCA safety invariant: a snapshot that passes the
            # exact same side/qty/avg_entry_price/symbol trust gate every
            # other field above already relies on is trusted for
            # dca_blocked/dca_block_reason too - restores whatever this
            # trade's own prior state was (normally False/None) instead of
            # the conservative True default this rebuild started with.
            restored_dca_blocked = bool(snapshot.get("dca_blocked", False))
            restored_dca_block_reason = snapshot.get("dca_block_reason")
            if recovered_step_safety_reason is not None:
                restored_dca_blocked = True
                restored_dca_block_reason = recovered_step_safety_reason
            # item 6 - exchange-native protective stop: restored under the
            # exact same trust gate as dca_blocked above. Still subject to
            # reconcile_protective_stop_on_startup()'s own authoritative
            # check against Binance's open orders immediately after this
            # function returns - a stale orderId here (e.g. it triggered
            # while this process was down) is corrected there, not here.
            try:
                restored_protective_stop_algo_id = snapshot.get("protective_stop_algo_id")
                restored_protective_stop_algo_id = (
                    int(restored_protective_stop_algo_id)
                    if restored_protective_stop_algo_id is not None else None
                )
            except (TypeError, ValueError):
                restored_protective_stop_algo_id = None
            try:
                restored_protective_stop_price = snapshot.get("protective_stop_price")
                restored_protective_stop_price = (
                    float(restored_protective_stop_price)
                    if restored_protective_stop_price is not None else None
                )
            except (TypeError, ValueError):
                restored_protective_stop_price = None
            restored_protection_pending = bool(snapshot.get("protection_pending", False))
            restored_protection_pending_reason = snapshot.get("protection_pending_reason")
            restored_protective_stop_client_algo_id = snapshot.get("protective_stop_client_algo_id")
            restored_protective_stop_cancel_pending = bool(
                snapshot.get("protective_stop_cancel_pending", False)
            )
            try:
                restored_protective_stop_actual_order_id = snapshot.get(
                    "protective_stop_actual_order_id"
                )
                restored_protective_stop_actual_order_id = (
                    int(restored_protective_stop_actual_order_id)
                    if restored_protective_stop_actual_order_id not in (None, "", 0, "0") else None
                )
            except (TypeError, ValueError):
                restored_protective_stop_actual_order_id = None
            # 2026-08 Algo-Service migration: a snapshot written BEFORE this
            # migration carries the legacy plain-order fields
            # (protective_stop_order_id / protective_stop_client_order_id).
            # Those ids belong to the /fapi/v1/order namespace and are
            # meaningless as algoIds - adopting one would let this process
            # believe it is protected when it is not, and cancelling by that
            # id via the algo endpoint could hit an unrelated algo order.
            #
            # Handled conservatively, exactly as required: dca_step is NOT
            # reset and no blind replacement is placed. Instead the position
            # is marked PROTECTION_PENDING and stale-cleanup is forced, so
            # reconciliation must enumerate the real open ALGO orders before
            # anything is placed or cancelled.
            legacy_stop_id = snapshot.get("protective_stop_order_id")
            legacy_stop_client_id = snapshot.get("protective_stop_client_order_id")
            if (
                restored_protective_stop_algo_id is None
                and (legacy_stop_id is not None or legacy_stop_client_id)
            ):
                restored_protective_stop_legacy_seen = True
                restored_protection_pending = True
                restored_protection_pending_reason = (
                    f"legacy pre-Algo protective-stop snapshot (orderId={legacy_stop_id}, "
                    f"clientOrderId={legacy_stop_client_id}) - re-arm via the Algo API after "
                    f"reconciliation confirms what is actually resting"
                )
                print(color(
                    f"{now_str()} [sync:{context}] [protective-stop] legacy pre-Algo snapshot "
                    f"detected (orderId={legacy_stop_id}) - NOT adopting it as an algoId and NOT "
                    f"placing a blind replacement; entering PROTECTION_PENDING and forcing stale "
                    f"cleanup. dca_step is untouched.", YELLOW,
                ))
            # 2026-08 PROTECTION_PENDING fail-safe (review finding 3): the
            # unprotected clock must survive a restart, otherwise a process
            # that restarts more often than PROTECTION_PENDING_MAX_SEC could
            # never reach the bounded fail-safe. A snapshot predating this
            # field leaves it None, and _mark_protection_pending() starts the
            # clock fresh at the next failure - conservative, never earlier
            # than reality.
            try:
                restored_protection_pending_since = snapshot.get("protection_pending_since")
                restored_protection_pending_since = (
                    float(restored_protection_pending_since)
                    if restored_protection_pending_since is not None else None
                )
            except (TypeError, ValueError):
                restored_protection_pending_since = None
            # 2026-08 CLOSING-resync opened_at fix: restore the real entry
            # timestamp from the snapshot (present on any snapshot saved by
            # the fixed _dca_state_snapshot() - older snapshots predating
            # this fix simply won't have the key, so this safely falls back
            # to None -> time.time() below, exactly like before this fix).
            try:
                snap_opened_at = float(snapshot.get("opened_at", 0) or 0)
                restored_opened_at = snap_opened_at if snap_opened_at > 0 else None
            except (TypeError, ValueError):
                restored_opened_at = None
            # 2026-08 fix B: restore this position's reconciliation entry-leg
            # floor, so a process that restarts mid-position still fetches the
            # entry and the eventual close in ONE userTrades window. Older
            # snapshots predating this field simply leave it None, which keeps
            # the pre-fix behavior rather than guessing at a window.
            try:
                snap_first_trade_id = snapshot.get("open_position_first_trade_id")
                if snap_first_trade_id is not None:
                    manager._open_position_first_trade_id = int(snap_first_trade_id)
            except (TypeError, ValueError):
                pass
            # 2026-08 DCA State Recovery V2: restore the full fill-by-fill
            # history (dca_history) if present and internally consistent
            # (a basic sanity check - its qty must sum to roughly the same
            # total_qty already validated above via qty_ok). An older
            # snapshot predating this field, or one that fails the sanity
            # check, safely falls back to a single synthetic entry built
            # from the exchange's own avg_entry_price/qty below - exactly
            # the previous behavior - rather than trusting a
            # malformed/stale history. Never used by any entry/exit/DCA/
            # risk decision either way - side/qty/avg_entry_price (already
            # validated above) remain the only fields that gate whether
            # ANY of this snapshot is trusted at all.
            raw_history = snapshot.get("dca_history")
            if isinstance(raw_history, list) and raw_history:
                try:
                    parsed_history = [(float(p_), float(q_)) for p_, q_ in raw_history]
                    history_qty_sum = sum(q_ for _, q_ in parsed_history)
                    if abs(history_qty_sum - qty) <= qty_tol:
                        restored_dca_history = parsed_history
                except (TypeError, ValueError):
                    restored_dca_history = None
            try:
                raw_entry_oid = snapshot.get("last_entry_order_id")
                restored_last_entry_order_id = int(raw_entry_oid) if raw_entry_oid is not None else None
            except (TypeError, ValueError):
                restored_last_entry_order_id = None
            try:
                raw_dca_oid = snapshot.get("last_dca_order_id")
                restored_last_dca_order_id = int(raw_dca_oid) if raw_dca_oid is not None else None
            except (TypeError, ValueError):
                restored_last_dca_order_id = None
            snapshot_restored = True
            # Only a pending CLOSE is restored here (not "initial"/"dca") -
            # those are Entry/DCA order-placement concerns and are left
            # untouched, exactly as before this fix. A pending close means a
            # reduceOnly order is already in flight on the exchange for this
            # exact position: restoring it lets _try_recover_close_fill()
            # route that order's eventual FILLED event to _on_close_filled()
            # instead of it falling through to untracked_order_id / exchange
            # reconciliation, and (via status="CLOSING" below) stops
            # _manage_open_position() from evaluating this position again and
            # submitting a second, duplicate close order that would overwrite
            # this same pending_order_id in the snapshot before the first
            # order's fill event ever arrives.
            if snapshot.get("pending_role") == "close" and snapshot.get("pending_order_id") is not None:
                try:
                    restored_pending_order_id = int(snapshot["pending_order_id"])
                    restored_pending_role = "close"
                except (TypeError, ValueError):
                    restored_pending_order_id = None
                    restored_pending_role = None
            # 2026-08 DCA-state GitHub persistence fix: explicit "restored
            # from GitHub" vs "restored from local" wording so it's obvious
            # from the logs alone which source actually backed this
            # recovery (requested diagnostic - purely additive, the
            # restoration itself is identical either way).
            print(color(
                f"{now_str()} [sync:{context}] [dca-state] restored from "
                f"{'GitHub' if snap_source == 'github' else 'local'} step="
                f"{restored_dca_step}/{MAX_DCA_STEPS} side={side} avg_entry={entry_price:.2f} "
                f"last_dca_price={restored_last_dca_price}, "
                f"profit_lock_active={preserved_profit_lock_active}, "
                f"pending_close_order_id={restored_pending_order_id}, "
                f"opened_at={'restored' if restored_opened_at else 'not available - using now()'}, "
                f"dca_history={'restored (' + str(len(restored_dca_history)) + ' fills)' if restored_dca_history else 'not available - using single synthetic entry'}, "
                f"last_entry_order_id={restored_last_entry_order_id}, "
                f"last_dca_order_id={restored_last_dca_order_id}.", MAGENTA,
            ))
            # 2026-08 DCA state recovery hardening: exact requested log
            # format, alongside the more detailed line above.
            print(color(
                f"[dca-state] restored valid snapshot:\n"
                f"side={side} qty={qty} avg_entry={entry_price:.2f} dca_step={restored_dca_step}",
                MAGENTA,
            ))
            if restored_dca_history is None:
                # 2026-08 DCA State Recovery V2: exact requested log format -
                # the position/dca_step itself WAS recovered above (this
                # branch only means the fill-by-fill history specifically
                # wasn't available or didn't pass its sanity check), so a
                # single synthetic entry is used as the conservative
                # fallback for `entries` below instead of leaving it empty.
                print(color(
                    "[DCA RECOVERY WARNING]\n"
                    "history unavailable, using conservative fallback",
                    YELLOW,
                ))
        else:
            restored_dca_block_reason = (
                "DCA-state snapshot found but does not match exchange position "
                "(side/qty/avg_entry/symbol)"
            )
            print(color(
                f"{now_str()} [sync:{context}] [dca-state] snapshot found but does not match "
                f"exchange position (side/qty/avg_entry/symbol) - ignoring.", YELLOW,
            ))
            # 2026-08 DCA state recovery hardening: exact requested log
            # format, alongside the more detailed line above.
            print(color(
                "[dca-state] snapshot rejected:\nreason=exchange mismatch",
                YELLOW,
            ))
            # 2026-08 DCA State Recovery V2: exact requested log format - a
            # mismatched snapshot means NONE of dca_step/history/etc. can be
            # trusted for this exchange-reported position (see the
            # unconditional REBUILD below, dca_step resets to 0 exactly as
            # before this fix).
            print(color(
                "[DCA RECOVERY WARNING]\n"
                "history unavailable, using conservative fallback",
                YELLOW,
            ))
    else:
        # 2026-08 DCA-state GitHub persistence fix: explicit, unmissable
        # WARNING (not silent) when neither local disk nor GitHub had
        # anything to restore from, so a redeploy that reset dca_step to 0
        # on a position that had genuinely already DCA'd is visible in the
        # logs rather than happening quietly. Does not change what happens
        # next (still falls through to the same rebuild below, dca_step=0)
        # - this is a diagnostic addition only.
        restored_dca_block_reason = (
            "no local or GitHub DCA-state snapshot found for this open position"
        )
        print(color(
            f"{now_str()} [sync:{context}] [dca-state] WARNING snapshot missing - dca_step reset risk "
            f"(exchange shows an open {side} qty={qty} position but no local or GitHub DCA-state "
            f"snapshot could back it - dca_step, last_dca_price, profit_lock_active, and "
            f"peak_unrealized_pnl cannot be recovered and will reset to their defaults below).",
            RED,
        ))
        # 2026-08 DCA State Recovery V2: exact requested log format.
        print(color(
            "[DCA RECOVERY WARNING]\n"
            "history unavailable, using conservative fallback",
            YELLOW,
        ))

    # 2026-08 hard DCA safety invariant: a REST resolution earlier in THIS
    # SAME call that came back "unknown" (ambiguous/failed order-status
    # lookup) always wins over whatever the snapshot match above decided -
    # even a snapshot that matched side/qty/avg_entry can't vouch for a
    # DCA step whose most recent add this process still can't confirm.
    # Consumed (reset to None) here so it can never leak into an unrelated
    # later call.
    if manager._pending_dca_block_reason is not None:
        restored_dca_blocked = True
        restored_dca_block_reason = manager._pending_dca_block_reason
        manager._pending_dca_block_reason = None
    if restored_dca_blocked:
        print(color(
            f"{now_str()} [sync:{context}] [dca-safety-block] DCA will remain blocked for this "
            f"position after resync: {restored_dca_block_reason}. TP / Hard Stop / Smart Exit / "
            f"Profit Lock / Max Hold Time are unaffected.", RED,
        ))

    print(color(
        f"{now_str()} [sync:{context}] *** RESYNCING TO MATCH EXCHANGE *** "
        f"exchange shows side={side} qty={qty} avg_entry={entry_price:.2f}; local state "
        f"was status={p.status} side={p.side} avg_entry={p.avg_entry_price}. Rebuilding "
        f"local state so take-profit / hard-stop / DCA logic resumes managing this trade "
        f"(dca_step {'restored from snapshot' if snapshot_restored else 'reset to 0 - not recoverable'}; "
        f"review manually if that matters for your risk tolerance).",
        YELLOW,
    ))
    rebuilt_status = "CLOSING" if restored_pending_order_id is not None else "OPEN"
    manager.position = PositionState(
        side=side,
        status=rebuilt_status,
        dca_step=restored_dca_step,
        # 2026-08 DCA State Recovery V2: prefer the restored fill-by-fill
        # history when available and sanity-checked above; otherwise fall
        # back to the single synthetic entry built from the exchange's own
        # avg_entry_price/qty - exactly the previous (pre-this-fix)
        # behavior. Either way, total_qty/avg_entry_price below are always
        # the exchange's own authoritative values, never derived from
        # entries - entries is informational/audit only (used for
        # invested_notional/reward-calc inputs at close time), so this
        # fallback can never desync position sizing from what Binance
        # actually reports.
        entries=(restored_dca_history if restored_dca_history else [(entry_price, qty)]),
        avg_entry_price=entry_price,
        total_qty=qty,
        original_qty=qty,
        # 2026-08 CLOSING-resync opened_at fix: prefer the real entry
        # timestamp restored from the snapshot above (covers a genuine
        # process restart, where there's no live PositionState left to
        # preserve in-memory); only falls back to time.time() when no
        # matching snapshot was found - i.e. a genuinely new/unrecoverable
        # position, exactly as before this fix.
        opened_at=(restored_opened_at if restored_opened_at else time.time()),
        max_favorable_price=entry_price,
        max_adverse_price=entry_price,
        last_dca_price=restored_last_dca_price,
        last_entry_order_id=restored_last_entry_order_id,
        last_dca_order_id=restored_last_dca_order_id,
        profit_lock_active=preserved_profit_lock_active,
        peak_unrealized_pnl=preserved_peak_unrealized_pnl,
        pending_order_id=restored_pending_order_id,
        pending_role=restored_pending_role,
        pending_order_ts=time.time() if restored_pending_order_id is not None else 0.0,
        # 2026-08 hard DCA safety invariant: this is the actual enforcement
        # point - all the restored_dca_blocked/restored_dca_block_reason
        # computation above only matters if it lands on the rebuilt
        # PositionState itself. Without this, the entire safety-block
        # decision above would be computed and logged but silently
        # discarded, leaving the rebuilt position free to DCA again on an
        # unrecoverable/blocked dca_step - exactly the bug this patch
        # fixes.
        dca_blocked=restored_dca_blocked,
        dca_block_reason=restored_dca_block_reason,
        # item 6 - exchange-native protective stop: best-effort restore
        # only - reconcile_protective_stop_on_startup() (dca2.py, called
        # once right after initialize_sync()) is the actual authority that
        # confirms this against Binance's own open orders and corrects it
        # (including clearing a stale orderId, or placing a fresh stop if
        # none is found resting).
        protective_stop_algo_id=restored_protective_stop_algo_id,
        protective_stop_price=restored_protective_stop_price,
        protective_stop_client_algo_id=restored_protective_stop_client_algo_id,
        protective_stop_actual_order_id=restored_protective_stop_actual_order_id,
        protective_stop_cancel_pending=restored_protective_stop_cancel_pending,
        protection_pending=restored_protection_pending,
        protection_pending_reason=restored_protection_pending_reason,
        protection_pending_since=restored_protection_pending_since,
    )
    # 2026-08 protective-stop fill-routing fix (review finding 2): a
    # protective stop restored from the snapshot must be wired into the
    # in-memory fill-routing map too, so its FILLED event (which can arrive
    # at any moment - it rests on the exchange for the whole trade) routes
    # through _on_close_filled() rather than being dropped as
    # "untracked_order_id". _try_recover_close_fill()'s snapshot lookup
    # remains the fallback for any window this doesn't cover.
    # 2026-08 Algo-Service migration: register the CHILD order id
    # (actualOrderId) - an algoId never appears in ORDER_TRADE_UPDATE, so
    # putting it here would both fail to route the real fill and risk
    # colliding with an unrelated orderId.
    if restored_protective_stop_actual_order_id is not None:
        manager._order_index[restored_protective_stop_actual_order_id] = "protective_stop"
    # A legacy pre-Algo snapshot must force reconciliation before anything is
    # placed or cancelled (see the migration block above).
    if restored_protective_stop_legacy_seen:
        manager._stale_protective_stops_possible = True
    # Keep the peak-save throttle consistent with whatever peak was just
    # restored (or reset to 0.0 on a fresh/mismatched snapshot), so the
    # next Profit Lock peak update in _manage_open_position() is measured
    # relative to the correct baseline (2026-07 DCA-state-recovery fix).
    manager._last_dca_state_peak_saved = preserved_peak_unrealized_pnl
    # 2026-08 realized-PnL/fee-accounting fix: restore the lifecycle fee
    # accumulator whenever the snapshot matched - independent of whether a
    # close order happens to be pending, since commission accrues from the
    # very first entry fill, well before any close is ever placed. A
    # fresh/mismatched snapshot leaves this at the PositionState default
    # (0.0/reliable=True), identical to a genuinely new trade.
    manager._position_fees_accum = restored_position_fees_accum
    manager._position_fees_reliable = restored_position_fees_reliable
    if restored_pending_order_id is not None:
        # Re-populate the in-memory order index too, so the fill routes
        # through the normal handle_order_update() path directly - the
        # persisted-snapshot lookup in _try_recover_close_fill() remains as
        # a fallback for any case this doesn't cover.
        manager._order_index[restored_pending_order_id] = "close"
        # 2026-08 DCA State Recovery V2: if this restored pending order is
        # a CLOSE, also restore how much of this trade's PnL was already
        # realized across earlier legs of the same close-verification
        # sequence (see close_position()/_on_close_filled()'s
        # _closing_accumulated_rp) - otherwise a restart landing mid-retry
        # would lose track of it and the eventual trade-log record would
        # under-report this trade's true PnL. The retry counter itself
        # resets to 0 rather than trying to restore how many attempts had
        # already happened - conservative (worst case, one extra retry
        # attempt is available) rather than trusting a potentially stale
        # count across a restart.
        try:
            manager._closing_accumulated_rp = float(snapshot.get("accumulated_close_pnl", 0.0) or 0.0) if snapshot else 0.0
        except (TypeError, ValueError):
            manager._closing_accumulated_rp = 0.0
        manager._closing_retry_count = 0
        print(color(
            f"{now_str()} [sync:{context}] [dca-state] pending CLOSE order "
            f"{restored_pending_order_id} restored - position set to CLOSING so "
            f"management logic will not submit a second close order while its "
            f"fill is still outstanding.", MAGENTA,
        ))
    if preserved_profit_lock_active:
        print(color(
            f"{now_str()} [sync:{context}] [profit-lock] preserved across resync "
            f"(peak_unrealized_pnl=${preserved_peak_unrealized_pnl:+.4f})", MAGENTA,
        ))
    # 2026-08 position_sync_ready timing fix (this line only): the full
    # rebuild above (manager.position = PositionState(...), fee/peak
    # bookkeeping, order-index registration) has now completely finished -
    # this is the ONLY way execution reaches this exact point (every
    # earlier branch in this function either returns before getting here
    # or falls through to this same rebuild), and there is no `await`
    # between the last state mutation above and this line. The final,
    # authoritative PositionState is fully installed before readiness is
    # ever set.
    manager.position_sync_ready = True


# ============================================================================
# EXCHANGE-NATIVE PROTECTIVE STOP - STARTUP RECONCILIATION (item 6)
# ============================================================================


async def reconcile_protective_stop_on_startup(client: RestClient, manager: MartingaleManager) -> None:
    """Best-effort startup reconciliation for the exchange-native
    protective stop. Called exactly once, from dca2.py's main(), AFTER
    initialize_sync() has already reconciled side/qty/avg_entry_price/
    dca_step against the exchange - deliberately a separate top-level
    function rather than folded into initialize_sync() itself (which is
    already the single most complex/delicate function in this file and is
    called from three different contexts - startup, every user-data-stream
    reconnect, and every position_risk_poller tick - none of which need to
    re-run this REST-heavy open-orders reconciliation). No-op in DRY_RUN
    (nothing real to reconcile), when PROTECTIVE_STOP_ENABLED/
    MAX_TRADE_NET_LOSS_USDT disable the feature, or when the position is
    not OPEN with real economics (nothing to protect yet).

    Queries Binance's own open orders for this symbol and looks for a
    resting STOP_MARKET closePosition=true order on the correct
    close-side:
      - If found (exactly one): adopts its orderId/stopPrice, clears
        PROTECTION_PENDING.
      - If found (more than one - should not normally happen given
        _place_or_replace_protective_stop()'s cancel-then-replace
        discipline, but startup reconciliation must not assume that
        invariant always held): adopts one, best-effort-cancels the rest
        so at most one protective stop is ever resting.
      - If NONE found for a genuinely OPEN position: this position is
        REALLY unprotected right now (e.g. the process crashed between
        entry fill and protective-stop placement, or Binance's own
        housekeeping expired a stale conditional order) - attempts to
        place a fresh one immediately via the same path a normal entry/DCA
        fill uses. If that also fails, the position is left in
        PROTECTION_PENDING (blocking new DCA; every client-side risk exit
        remains fully active) with a high-severity log, rather than
        silently continuing as if protected.
    """
    if DRY_RUN or not PROTECTIVE_STOP_ENABLED or MAX_TRADE_NET_LOSS_USDT <= 0:
        return
    p = manager.position
    position_is_open = (
        p.status == "OPEN" and p.avg_entry_price and p.total_qty > 0 and p.side in ("LONG", "SHORT")
    )

    try:
        open_orders = await client.get_open_algo_orders(manager.symbol)
    except Exception as e:  # noqa: BLE001 - a reconciliation fetch must never crash startup
        # 2026-08 startup-reconciliation safety: block placement until a
        # fetch actually succeeds, so nothing is ever placed on top of a
        # stop this process could not see.
        manager._protective_stop_reconcile_blocked = True
        # 2026-08 stale-leftover safety: an enumeration failure means we
        # cannot rule out a bot-owned stop left resting by a previous
        # position. Assume the worst until proven otherwise - this blocks new
        # entries so a stale stop can never be inherited by a new trade.
        manager._stale_protective_stops_possible = True
        print(color(
            f"{now_str()} [protective-stop] *** HIGH SEVERITY *** startup reconciliation could not "
            f"fetch open orders for {manager.symbol}: {e} - cannot confirm whether a bot-owned "
            f"protective stop is resting; NOT placing a new one blind and BLOCKING new entries "
            f"until this is resolved. "
            f"{'Position left PROTECTION_PENDING (new DCA blocked; client-side risk exits remain active). ' if position_is_open else ''}"
            f"Reconciliation is retried by the protective-stop sweep.", RED,
        ))
        if position_is_open:
            manager._mark_protection_pending(f"startup open-orders fetch failed: {e}")
        return

    # A successful enumeration is the only thing that unblocks placement.
    manager._protective_stop_reconcile_blocked = False

    # 2026-08 protective-stop ownership fix (review finding 4): matching on
    # type/side/closePosition ALONE would also match a STOP_MARKET the user
    # placed manually, or one belonging to another system trading the same
    # account/symbol - and this function both ADOPTS and CANCELS what it
    # matches. Ownership is proven by the bot-assigned clientOrderId prefix
    # (PROTECTIVE_STOP_CLIENT_ID_PREFIX + "-"), which nothing else can
    # accidentally carry. Foreign orders are counted for the log line and
    # then left completely untouched.
    #
    # 2026-08 FLAT-startup fix: this whole block used to be unreachable
    # unless the position was OPEN, so a bot-owned protective stop left
    # resting on Binance (process killed between the exchange closing the
    # position and this bot cancelling the stop) SURVIVED the restart. Being
    # closePosition=true it would then sit there and, once a brand-new
    # position was opened, trigger against THAT position at a stop price
    # computed for a completely different trade. When flat, every bot-owned
    # protective stop is therefore cancelled outright (no side filter -
    # side is only meaningful relative to an open position, and a leftover
    # stop on either side is equally dangerous). Foreign orders are still
    # never touched.
    if not position_is_open:
        owned_flat = [
            o for o in open_orders
            if (o.get("orderType") or o.get("type")) == "STOP_MARKET"
            and str(o.get("closePosition")).lower() == "true"
            and manager._is_own_protective_stop(o)
        ]
        if not owned_flat:
            # Enumeration succeeded and there is nothing bot-owned resting:
            # this is the ONLY proof that no stale leftover exists. Combined
            # with an empty orphan set, it re-opens the entry gate.
            if not manager._orphan_protective_algo_ids:
                if manager._stale_protective_stops_possible:
                    print(color(
                        f"{now_str()} [protective-stop] reconciliation confirms no bot-owned "
                        f"protective stop is resting on {manager.symbol} while FLAT - stale-leftover "
                        f"entry block cleared.", GREEN,
                    ))
                manager._stale_protective_stops_possible = False
            return
        print(color(
            f"{now_str()} [protective-stop] startup reconciliation found {len(owned_flat)} "
            f"bot-owned protective stop(s) resting on {manager.symbol} while FLAT - these are "
            f"leftovers from a previous position and would trigger against a future position; "
            f"cancelling all of them. New entries are blocked until they are confirmed gone.",
            YELLOW,
        ))
        # Any known leftover keeps the entry gate shut until proven gone.
        manager._stale_protective_stops_possible = True
        unresolved = False
        for o in owned_flat:
            order_id = o.get("algoId")
            if order_id is None:
                continue
            try:
                await client.cancel_algo_order(algo_id=order_id)
                manager._orphan_protective_algo_ids.discard(order_id)
                print(color(
                    f"{now_str()} [protective-stop] cancelled leftover orderId={order_id} "
                    f"clientOrderId={o.get('clientAlgoId')} (flat at startup).", GRAY,
                ))
            except BinanceApiError as e:
                if e.code == -2011:  # Binance proves it is already gone
                    manager._orphan_protective_algo_ids.discard(order_id)
                    print(color(
                        f"{now_str()} [protective-stop] leftover orderId={order_id} already gone.",
                        GRAY,
                    ))
                else:
                    # 2026-08: retain + retry instead of silently passing -
                    # an uncancelled leftover is exactly the hazard here.
                    manager._orphan_protective_algo_ids.add(order_id)
                    unresolved = True
                    print(color(
                        f"{now_str()} [protective-stop] cancel of leftover orderId={order_id} "
                        f"FAILED ({e}) - handed to the orphan sweep for retry; entries stay "
                        f"blocked.", YELLOW,
                    ))
            except Exception as e:  # noqa: BLE001 - must never crash startup
                manager._orphan_protective_algo_ids.add(order_id)
                unresolved = True
                print(color(
                    f"{now_str()} [protective-stop] cancel of leftover orderId={order_id} errored "
                    f"({e}) - handed to the orphan sweep for retry; entries stay blocked.", YELLOW,
                ))
        # Only unblock once EVERY stale owned stop is confirmed gone.
        if not unresolved and not manager._orphan_protective_algo_ids:
            manager._stale_protective_stops_possible = False
            print(color(
                f"{now_str()} [protective-stop] all leftover protective stops confirmed cancelled - "
                f"stale-leftover entry block cleared.", GREEN,
            ))
        return

    close_side = "SELL" if p.side == "LONG" else "BUY"
    candidates = [
        o for o in open_orders
        if (o.get("orderType") or o.get("type")) == "STOP_MARKET"
        and o.get("side") == close_side
        and str(o.get("closePosition")).lower() == "true"
    ]
    matching = [o for o in candidates if manager._is_own_protective_stop(o)]
    foreign = [o for o in candidates if not manager._is_own_protective_stop(o)]
    if foreign:
        print(color(
            f"{now_str()} [protective-stop] startup reconciliation ignored {len(foreign)} "
            f"STOP_MARKET order(s) on {manager.symbol} not owned by this bot (clientOrderId does "
            f"not start with '{PROTECTIVE_STOP_CLIENT_ID_PREFIX}-') - they are left untouched.",
            GRAY,
        ))

    # 2026-08 stale-leftover safety: while a FLAT cleanup is unresolved, a
    # bot-owned stop found here CANNOT be assumed to belong to this position -
    # it is far more likely the leftover we already know we failed to cancel,
    # carrying a stop price computed for a completely different trade.
    # Adopting it would silently hand this position the wrong protection. Only
    # an order this process itself placed and is already tracking (same
    # orderId) is trustworthy; everything else owned is cancelled as stale.
    if manager._stale_protective_stops_possible and matching:
        trusted = [o for o in matching if o.get("algoId") == p.protective_stop_algo_id
                   and p.protective_stop_algo_id is not None]
        stale = [o for o in matching if o not in trusted]
        if stale:
            print(color(
                f"{now_str()} [protective-stop] refusing to ADOPT {len(stale)} bot-owned stop(s) "
                f"discovered while stale-leftover cleanup is unresolved - they cannot be proven to "
                f"belong to this position (their stop price was computed for a different trade); "
                f"cancelling them instead.", YELLOW,
            ))
        unresolved = False
        for o in stale:
            stale_id = o.get("algoId")
            if stale_id is None:
                continue
            try:
                await client.cancel_algo_order(algo_id=stale_id)
                manager._orphan_protective_algo_ids.discard(stale_id)
                print(color(
                    f"{now_str()} [protective-stop] cancelled stale orderId={stale_id} "
                    f"clientOrderId={o.get('clientAlgoId')}.", GRAY,
                ))
            except BinanceApiError as e:
                if e.code == -2011:
                    manager._orphan_protective_algo_ids.discard(stale_id)
                else:
                    manager._orphan_protective_algo_ids.add(stale_id)
                    unresolved = True
                    print(color(
                        f"{now_str()} [protective-stop] cancel of stale orderId={stale_id} FAILED "
                        f"({e}) - handed to the orphan sweep for retry.", YELLOW,
                    ))
            except Exception as e:  # noqa: BLE001 - must never crash reconciliation
                manager._orphan_protective_algo_ids.add(stale_id)
                unresolved = True
                print(color(
                    f"{now_str()} [protective-stop] cancel of stale orderId={stale_id} errored "
                    f"({e}) - handed to the orphan sweep for retry.", YELLOW,
                ))
        if not unresolved and not manager._orphan_protective_algo_ids:
            manager._stale_protective_stops_possible = False
        # Fall through with ONLY the trusted (already-tracked) order, if any.
        # If none remains, the "no stop found" path below places a fresh one
        # sized for THIS position - which is the correct protection.
        matching = trusted

    if matching:
        chosen = next(
            (o for o in matching if o.get("algoId") == p.protective_stop_algo_id), matching[0]
        )
        p.protective_stop_algo_id = chosen.get("algoId")
        try:
            p.protective_stop_price = float(chosen.get("triggerPrice", 0) or 0) or None
        except (TypeError, ValueError):
            p.protective_stop_price = None
        p.protective_stop_client_algo_id = chosen.get("clientAlgoId")
        p.protective_stop_cancel_pending = False
        manager._clear_protection_pending()
        # 2026-08 Algo-Service migration: an algoId is NOT an orderId and
        # never appears in ORDER_TRADE_UPDATE, so it must NOT be put into
        # _order_index. Only the CHILD order Binance creates when the algo
        # triggers (actualOrderId) fills and emits ORDER_TRADE_UPDATE - if
        # this adopted algo has already triggered, wire that child up now so
        # its fill routes through _on_close_filled() exactly once.
        await manager._register_protective_child_order(
            chosen.get("actualOrderId"), context="startup reconciliation adopt",
        )
        if len(matching) > 1:
            print(color(
                f"{now_str()} [protective-stop] startup reconciliation found {len(matching)} resting "
                f"protective stop(s) for {manager.symbol} - adopted algoId="
                f"{p.protective_stop_algo_id}; cancelling the rest so at most one remains.", YELLOW,
            ))
            for o in matching:
                if o.get("algoId") != p.protective_stop_algo_id:
                    dup_id = o.get("algoId")
                    if dup_id is None:
                        continue
                    try:
                        await client.cancel_algo_order(algo_id=dup_id)
                    except BinanceApiError as e:
                        if e.code != -2011:  # -2011 proves it is already gone
                            # 2026-08: retain + retry instead of the previous
                            # silent `pass`. A duplicate closePosition=true
                            # stop left resting can close the position at the
                            # wrong price, so it must not be forgotten.
                            manager._orphan_protective_algo_ids.add(dup_id)
                            print(color(
                                f"{now_str()} [protective-stop] cancel of duplicate orderId="
                                f"{dup_id} FAILED ({e}) - handed to the orphan sweep for retry.",
                                YELLOW,
                            ))
                    except Exception as e:  # noqa: BLE001 - must never crash startup
                        manager._orphan_protective_algo_ids.add(dup_id)
                        print(color(
                            f"{now_str()} [protective-stop] cancel of duplicate orderId={dup_id} "
                            f"errored ({e}) - handed to the orphan sweep for retry.", YELLOW,
                        ))
        else:
            print(color(
                f"{now_str()} [protective-stop] startup reconciliation adopted existing "
                f"algoId={p.protective_stop_algo_id} clientAlgoId={p.protective_stop_client_algo_id} "
                f"triggerPrice={p.protective_stop_price} for {manager.symbol}.", GREEN,
            ))
        asyncio.create_task(manager.save_dca_state(reason="protective stop reconciled on startup"))
        return

    print(color(
        f"{now_str()} [protective-stop] startup reconciliation found NO resting protective stop "
        f"for an OPEN {manager.symbol} position - this position is currently UNPROTECTED against a "
        f"REST outage/ban; placing one now.", YELLOW,
    ))
    await manager._place_or_replace_protective_stop(reason="startup reconciliation - none found")
