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

from config import (
    SYMBOL,
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
    TAKER_FEE_RATE,
    MIN_NET_PROFIT_USDT,
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
    RECENT_TRADE_WINDOW,
    ENTRY_SCORE_THRESHOLD,
    SIDEWAYS_ENTRY_SCORE_THRESHOLD,
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
)
from indicators import clamp, safe_div, ema_series, round_step
from exchange import BinanceApiError, RestClient, SymbolFilters

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
    ) -> EntryDecision:
        if conf.trend_direction is None or conf.trend_confidence <= 0:
            return EntryDecision(False, None, 0.0, {})

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
        momentum_component = clamp((abs(momentum) / ENTRY_MOMENTUM_SATURATION_PCT), 0.0, 1.0)

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

        active_threshold = (
            SIDEWAYS_ENTRY_SCORE_THRESHOLD
            if regime.regime == REGIME_SIDEWAYS
            else ENTRY_SCORE_THRESHOLD
        )

        if self._should_log():
            print(color(
                f"{now_str()} [entry-debug] regime={regime.regime} "
                f"regime_blocked={regime_blocked} dead_market_blocked={dead_market_blocked} "
                f"atr_pct={regime.atr_pct:.6f} "
                f"brain_confidence={components['brain_confidence']:.4f} "
                f"trend_confidence={components['trend_confidence']:.4f} "
                f"volume_confirmation={components['volume_confirmation']:.4f} "
                f"volatility_fit={components['volatility_fit']:.4f} "
                f"momentum={components['momentum']:.4f} "
                f"regime_fit={components['regime_fit']:.4f} "
                f"risk_score={components['risk_score']:.4f} "
                f"final_score={score:.4f} threshold={active_threshold:.4f}",
                GRAY,
            ))

        should_enter = (not regime_blocked) and score >= active_threshold
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
    def __init__(self, logger: TradeLogger, json_path: str = STATS_JSON_PATH, csv_path: str = STATS_CSV_PATH):
        self.logger = logger
        self.json_path = json_path
        self.csv_path = csv_path

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
        trades = self.logger.load_all()
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
    # 2026-08 DCA State Recovery V2: which order_id filled the initial
    # entry / most recent DCA add - audit/recovery fields only, never read
    # by any entry/exit/DCA/risk decision (mirrors initial_entry_price's
    # own audit-only role in _dca_state_snapshot()).
    last_entry_order_id: Optional[int] = None
    last_dca_order_id: Optional[int] = None

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
        self._last_max_hold_dca_defer_log_ts: float = 0.0  # throttles the Max Hold Time V2 "DCA opportunity available" diagnostic line
        self._max_hold_dca_defer_pending: bool = False  # set True when Max Hold Time V2 defers for a DCA opportunity this tick; consumed by _on_entry_filled() to log "[dca] executed after max-hold defer"
        self._last_profit_lock_debug_log_ts: float = 0.0  # throttles the [profit-lock-debug] diagnostic line
        self._last_profit_lock_peak_update_log_ts: float = 0.0  # throttles the [profit-lock-peak] UPDATED diagnostic line

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
        self._last_synced_csv_hash: Dict[str, Optional[str]] = {}

        self._order_index: Dict[int, str] = {}
        self._rp_accum: Dict[int, float] = {}

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
        self._UNMATCHED_FILL_TTL_SEC: float = 15.0

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

    # -- Persistent Adaptive Learning: startup load / ongoing persistence ----

    async def load_or_init_brain(self) -> None:
        # Start (or reuse) the single shared GitHub session up front, so it's
        # available for the CSV log/stats restore that runs right after this,
        # regardless of which branch below actually loads the brain from.
        await self.github_sync.start()

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
        }

    async def save_dca_state(self, reason: str) -> None:
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
        """
        step_label = f"{self.position.dca_step}/{MAX_DCA_STEPS}"
        try:
            payload = json.dumps(self._dca_state_snapshot()).encode("utf-8")
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
            (TRADE_LOG_CSV_PATH, GITHUB_TRADES_LOG_CSV_PATH, "trades_log.csv"),
            (STATS_CSV_PATH, GITHUB_STATS_CSV_PATH, "performance_stats.csv"),
            (TRADE_LOG_JSON_PATH, GITHUB_TRADES_LOG_JSON_PATH, "trades_log.jsonl"),
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
        await self._sync_csv_to_github(TRADE_LOG_CSV_PATH, GITHUB_TRADES_LOG_CSV_PATH, "trades_log.csv")
        await self._sync_csv_to_github(TRADE_LOG_JSON_PATH, GITHUB_TRADES_LOG_JSON_PATH, "trades_log.jsonl")

    async def sync_performance_stats_to_github(self) -> None:
        await self._sync_csv_to_github(STATS_CSV_PATH, GITHUB_STATS_CSV_PATH, "performance_stats.csv")

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

    async def reconcile_trade_history_from_exchange(self, context: str = "reconcile") -> None:
        """Fetches executed fills for `self.symbol` from Binance starting
        just after the persisted cursor (or the optional explicit backfill
        id on true first run - see TRADE_RECONCILE_BACKFILL_FROM_ID),
        reconstructs any flat->open->flat position lifecycle Binance
        reports, and logs any such lifecycle that isn't already in
        trades_log.jsonl (deduped by Binance order id via
        TradeLogger.logged_binance_order_ids()). Safe to call frequently -
        it is a no-op (single cheap REST call, empty result) once caught
        up. Never raises; never touches PositionState or any strategy
        state - purely a logging safety net.

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
            from_id = max(self._trade_sync_cursor, self._last_live_trade_id) + 1

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
        max_id_seen = max(int(t["id"]) for t in fills)
        cursor_cap = max_id_seen

        # Reconstruct each flat -> open -> flat position lifecycle from the
        # running signed position size (BUY=+qty, SELL=-qty; this bot only
        # ever runs in one-way mode - see close_position()/_place_step_order(),
        # which always use plain BUY/SELL with no positionSide). A lifecycle
        # still open at the end of the fetched window is the CURRENT live
        # position and is skipped - it hasn't closed yet.
        lifecycles: List[dict] = []
        running = 0.0
        current: Optional[dict] = None
        eps = 1e-9
        for t in fills:
            signed_qty = float(t["qty"]) * (1.0 if t["side"] == "BUY" else -1.0)
            was_flat = abs(running) < eps
            running += signed_qty
            if was_flat and abs(running) > eps:
                current = {"open_side": "LONG" if running > 0 else "SHORT", "fills": [], "open_time": int(t["time"])}
            if current is not None:
                current["fills"].append(t)
            if not was_flat and abs(running) < eps and current is not None:
                current["close_time"] = int(t["time"])
                lifecycles.append(current)
                current = None

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
                net_pnl = sum(float(t.get("realizedPnl", 0.0)) for t in lc["fills"])
                avg_entry = safe_div(entry_notional, entry_qty, 0.0)
                avg_exit = safe_div(sum(float(t["qty"]) * float(t["price"]) for t in exit_fills), exit_qty, 0.0)
                close_dt = datetime.fromtimestamp(lc["close_time"] / 1000, tz=timezone.utc)
                # exit_order_id: the orderId of the fill that actually closed the
                # lifecycle (chronologically last exit fill) - exchange data, when
                # available, same as the live-close path's exit_order_id.
                exit_order_id = int(exit_fills[-1]["orderId"]) if exit_fills[-1].get("orderId") is not None else None

                record = {
                    "close_time": close_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "symbol": self.symbol,
                    "side": lc["open_side"],
                    "entry_price": avg_entry or None,
                    "exit_price": avg_exit or None,
                    "qty": exit_qty or entry_qty,
                    "invested_notional": entry_notional,
                    "gross_pnl_usdt": net_pnl + fees,
                    "fees_usdt": fees,
                    "net_pnl_usdt": net_pnl,
                    "net_pnl_pct": safe_div(net_pnl, entry_notional, 0.0),
                    "dca_count": max(len(entry_fills) - 1, 0),
                    "holding_time_sec": max((lc["close_time"] - lc["open_time"]) / 1000.0, 0.0),
                    "mfe_pct": None,
                    "mae_pct": None,
                    "exit_reason": "reconciled_from_exchange",
                    "tp_hit": None,
                    "smart_exit": None,
                    "manual_exit": None,
                    "hard_stop": None,
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
                    f"close_time={record['close_time']} "
                    f"reason=not_found_in_local_trade_log (never seen by _on_close_filled(); "
                    f"recovered via REST /fapi/v1/userTrades)", MAGENTA,
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

    def _should_log_max_hold_review(self, interval_sec: float = 30.0) -> bool:
        """Throttle for the Max Hold Time V2 'kept alive after emergency
        review' diagnostic line - debugging only, does not affect whether
        the position is actually deferred or closed."""
        now = time.time()
        if now - self._last_max_hold_review_log_ts >= interval_sec:
            self._last_max_hold_review_log_ts = now
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
            self._maybe_reset_daily_loss_tracker()
            if MAX_DAILY_LOSS_USDT > 0 and self.daily_realized_pnl <= -MAX_DAILY_LOSS_USDT:
                if self._should_log_daily_loss_block():
                    print(color(
                        f"{now_str()} [daily-loss] entries halted: today's realized PnL "
                        f"${self.daily_realized_pnl:+.4f} <= -${MAX_DAILY_LOSS_USDT:.2f} limit - "
                        f"no new trades until the next UTC day (existing open positions, if any, "
                        f"are unaffected)", RED,
                    ))
                return

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

            decision = self.entry_engine.evaluate(self.last_confidence, self.last_regime, volume_z, momentum, features)
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
            order_id = resp["orderId"]
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
                self.position.pending_order_ts = time.time()
                self.position.side = side_signal
                self.position.status = "ENTERING" if step == 0 else "DCA_PENDING"
                self.last_trade_action_ts = time.time()
            print(color(
                f"{now_str()} {step_label} PLACED  {order_side} {qty} {self.symbol} "
                f"@ market (notional=${notional:.2f}, orderId={order_id}, "
                f"size_mult={size_mult:.2f}, regime={self.last_regime.regime})",
                CYAN,
            ))
        except BinanceApiError as e:
            print(color(f"[dca] {step_label} order FAILED: {e}", RED))

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
        except (BinanceApiError, aiohttp.ClientError, asyncio.TimeoutError) as e:
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

    async def close_position(self, reason: str, emergency: bool = False, exit_reason_tag: str = "manual") -> None:
        if self.position.status not in ("OPEN", "DCA_PENDING") or self.position.total_qty <= 0:
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
                    self.position = PositionState(last_close_time=time.time())
                    self.last_trade_action_ts = time.time()
                    asyncio.create_task(self.delete_dca_state(reason="exchange already flat at close time"))
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
        if MAX_HOLD_TIME_ENABLED and held_sec_so_far >= effective_max_hold_sec:
            hard_cap_hit = held_sec_so_far >= MAX_HOLD_TIME_HARD_CAP_SEC
            trending_and_profitable = (
                unrealized_pnl_usdt > 0
                and self.last_regime.regime in (REGIME_STRONG_TREND, REGIME_WEAK_TREND)
            )
            meaningful_loss = unrealized_pnl_usdt < 0 and abs(pct_move) > MAX_HOLD_TIME_SMALL_LOSS_PCT

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
            dca_opportunity_available = (
                not hard_cap_hit
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
                    "dca_exhausted": p.dca_step >= MAX_DCA_STEPS,
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
        if (
            SMART_EXIT_ENABLED and smart_exit_held_long_enough
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
                )
                return

        # --- ATR-adaptive DCA -----------------------------------------------------------
        if pct_move <= -dca_distance_pct:
            if p.dca_step >= MAX_DCA_STEPS:
                await self.close_position(
                    f"max DCA steps ({MAX_DCA_STEPS}) exhausted and price still adverse "
                    f"({pct_move*100:.2f}%, dca_distance={dca_distance_pct*100:.3f}%)",
                    emergency=True, exit_reason_tag="max_dca_exhausted",
                )
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
                    await self.close_position(
                        f"final DCA step skipped: low-probability recovery "
                        f"({agree_count}/{len(recovery_risk_signals)} signals agree: "
                        f"{', '.join(k for k, v in recovery_risk_signals.items() if v)}; "
                        f"risk={conf.risk_score:.2f}, trend_direction={conf.trend_direction}, "
                        f"trend_confidence={conf.trend_confidence:.2f}, regime={regime.regime}, "
                        f"atr_ratio={regime.atr_ratio:.2f}) at {pct_move*100:.2f}% - "
                        f"exiting instead of adding the last DCA step",
                        emergency=True, exit_reason_tag="final_dca_skipped_low_probability",
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
        try:
            snapshot = await self.load_dca_state_snapshot()
        except Exception:  # noqa: BLE001 - recovery must never crash the fill handler
            return False
        if not snapshot:
            return False
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

        fill_price = float(o.get("ap") or 0.0)
        rp = float(o.get("rp") or 0.0)
        print(color(
            f"{now_str()} [fill-trace] path=restart_recovery order_id={order_id} "
            f"reason=matched_persisted_dca_state_snapshot (pending_role=close, "
            f"pending_order_id={snap_order_id}) -> routing to _on_close_filled() "
            f"despite empty in-memory _order_index (restart-safe recovery)", CYAN,
        ))
        await self._on_close_filled(fill_price, rp, order_id=order_id)
        return True

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

        # Record-keeping only (not used by any entry/exit/DCA/risk decision):
        # tracks the highest Binance trade id this process has itself
        # observed live, so the reconciliation safety net below never
        # re-fetches/re-logs a fill this process just handled.
        trade_id = o.get("t")
        if trade_id is not None:
            try:
                self._last_live_trade_id = max(self._last_live_trade_id, int(trade_id))
            except (TypeError, ValueError):
                pass

        status = o.get("X")
        if status != "FILLED":
            return

        role = self._order_index.pop(order_id)
        total_rp = self._rp_accum.pop(order_id, 0.0)
        fill_price = float(o.get("ap") or 0.0)
        fill_qty = float(o.get("z") or 0.0)

        if role in ("initial", "dca"):
            await self._on_entry_filled(role, fill_price, fill_qty, order_id=order_id)
        elif role == "partial_close":
            await self._apply_partial_close(fill_qty, fill_price)
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
        self.position.entries.append((fill_price, fill_qty))
        total_notional = sum(p * q for p, q in self.position.entries)
        total_qty = sum(q for _, q in self.position.entries)
        self.position.avg_entry_price = total_notional / total_qty if total_qty else None
        self.position.total_qty = total_qty
        self.position.original_qty = total_qty
        if role == "dca":
            self.position.dca_step += 1
            self.position.last_dca_order_id = order_id
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

        step_label = "INITIAL" if role == "initial" else f"DCA #{self.position.dca_step}"
        side_color = GREEN if self.position.side == "LONG" else RED
        print(color(
            f"{now_str()} ENTRY FILLED [{step_label}] {self.position.side} "
            f"qty={fill_qty} @ {fill_price:.2f}  ->  avg_entry={self.position.avg_entry_price:.2f}  "
            f"total_qty={self.position.total_qty}  leverage={self.leverage}x  margin={MARGIN_TYPE}  "
            f"regime={self.last_regime.regime}  confidence={self.last_confidence.confidence_score:.2f}",
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

    async def _on_close_filled(self, fill_price: float, total_rp: float, order_id: Optional[int] = None) -> None:
        p = self.position
        self.realized_pnl_total += total_rp
        # 2026-08 Daily Loss Protection: same value/accumulation point as
        # realized_pnl_total above - only the DAILY bucket also resets at
        # each UTC day boundary. This only ever feeds the entry-gate check
        # in on_price_tick() - nothing about this close's own handling
        # (trade log, DCA-state cleanup, etc.) is affected.
        self._maybe_reset_daily_loss_tracker()
        self.daily_realized_pnl += total_rp
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
        total_rp_for_record = self._closing_accumulated_rp
        self._closing_accumulated_rp = 0.0
        self._closing_retry_count = 0
        self.trade_count += 1
        pnl_color = GREEN if total_rp_for_record >= 0 else RED

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

        net_pnl_total = total_rp_for_record  # includes any partial-TP pnl already added to realized_pnl_total separately
        # total_rp_for_record is the SUM of every close leg's realized pnl
        # for this trade (usually just one leg; more if close-verification
        # above needed a retry) per Binance's own accounting; combine with
        # whatever partial-TP pnl we tracked locally.
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
            f"{now_str()} POSITION CLOSED @ {fill_price:.2f}  PnL={total_rp_for_record:+.4f} USDT  "
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
            "exit_order_id": int(order_id) if order_id is not None else None,
            "binance_order_ids": [int(order_id)] if order_id is not None else [],
            "smart_exit_agree_count": smart_exit_diag["agree_count"] if smart_exit_diag else "",
            "smart_exit_required_agree": smart_exit_diag["required_agree"] if smart_exit_diag else "",
            "smart_exit_signals_fired": smart_exit_diag["signals_fired"] if smart_exit_diag else "",
            "smart_exit_dca_distance_pct": smart_exit_diag["dca_distance_pct"] if smart_exit_diag else "",
        }
        self._log_completed_trade(record)

        self.position = PositionState(last_close_time=time.time())
        # New (flat) position - reset the peak-save throttle so the next
        # trade's Profit Lock starts measuring peak growth from zero
        # (2026-07 DCA-state-recovery fix).
        self._last_dca_state_peak_saved = 0.0

        asyncio.create_task(self.persist_brain(reason="trade closed"))
        asyncio.create_task(self.sync_trade_log_to_github())
        asyncio.create_task(self.delete_dca_state(reason="trade closed"))
        if self._last_live_trade_id:
            asyncio.create_task(self._persist_trade_sync_cursor(
                self._last_live_trade_id, reason="live close"
            ))


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
        await manager.reconcile_trade_history_from_exchange(context=context)

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
        print(color(
            f"{now_str()} [sync:{context}] exchange still shows {side} qty={qty} but a close "
            f"order (id={p.pending_order_id}) is already pending for this exact position "
            f"(side/qty/avg_entry all match) - its FILLED event just hasn't arrived yet. "
            f"Leaving local state (including opened_at) untouched instead of rebuilding.", GRAY,
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
    snapshot_restored = False
    snapshot = await manager.load_dca_state_snapshot()
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
            restored_dca_step = int(snapshot.get("dca_step", 0) or 0)
            restored_last_dca_price = snapshot.get("last_dca_price")
            preserved_profit_lock_active = bool(snapshot.get("profit_lock_active", False))
            preserved_peak_unrealized_pnl = float(snapshot.get("peak_unrealized_pnl", 0.0) or 0.0)
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
            print(color(
                f"{now_str()} [sync:{context}] [dca-state] snapshot found but does not match "
                f"exchange position (side/qty/avg_entry/symbol) - ignoring.", YELLOW,
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
    )
    # Keep the peak-save throttle consistent with whatever peak was just
    # restored (or reset to 0.0 on a fresh/mismatched snapshot), so the
    # next Profit Lock peak update in _manage_open_position() is measured
    # relative to the correct baseline (2026-07 DCA-state-recovery fix).
    manager._last_dca_state_peak_saved = preserved_peak_unrealized_pnl
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
