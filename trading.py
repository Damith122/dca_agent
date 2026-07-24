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

 2026-07 DCA-state persistence fix: adds a dedicated, minimal persistence
 layer for the DCA lifecycle (dca_step, entries, opened_at, last_dca_price)
 - see the "DCA STATE PERSISTENCE" section below and its use inside
 _on_entry_filled / _on_close_filled / initialize_sync. This is
 deliberately NOT built on Binance trade-history replay (that mechanism
 already exists separately for trade-log recovery and is untouched here);
 it is a small, self-contained snapshot of just the DCA lifecycle fields,
 saved after every DCA fill and cleared on a full close, so that a restart
 mid-position can restore dca_step/entries/opened_at/last_dca_price instead
 of always resetting dca_step to 0. It only ever applies on a confirmed
 side + avg_entry_price match against what the exchange itself reports; a
 mismatched or stale snapshot is ignored safely and the existing reset-to-0
 rebuild path is used unchanged. No entry, exit, TP, Smart Exit, Brain, or
 sizing/risk logic was touched by this change.
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
    SIGNAL_LOOKBACK_TICKS,
    SIGNAL_DEADBAND_PCT,
    TRADE_COOLDOWN_SEC,
    MIN_HOLD_SEC_BEFORE_EXIT,
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

        active_threshold = (
            SIDEWAYS_ENTRY_SCORE_THRESHOLD
            if regime.regime == REGIME_SIDEWAYS
            else ENTRY_SCORE_THRESHOLD
        )

        if self._should_log():
            print(color(
                f"{now_str()} [entry-debug] regime={regime.regime} "
                f"regime_blocked={regime_blocked} "
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

    def mark_header_present(self) -> None:
        """Re-checks csv_path on disk and refreshes the cached header-written
        flag. Needed because TradeLogger is constructed (and caches this
        flag) before an async GitHub restore can write a downloaded CSV to
        csv_path - without this, the next log_trade() would re-write a
        header into the middle of the restored file."""
        self._csv_header_written = os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0

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
        self._last_warmup_skip_log_ts: float = 0.0  # throttles the pre-warmup "no entry" log line

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

        # --- Trade-log reconciliation (Binance is the source of truth) ----
        # In-memory high-water-mark of the highest Binance trade id ("t" on
        # ORDER_TRADE_UPDATE) this process has itself seen live, plus the
        # durable cursor loaded from disk/GitHub in load_trade_sync_cursor().
        # Both exist purely to make reconcile_trade_history_from_exchange()
        # idempotent - neither is read by any entry/exit/DCA/risk logic.
        self._last_live_trade_id: int = 0
        self._trade_sync_cursor: int = 0

        # --- DCA lifecycle persistence (dca_step/entries/opened_at/
        # last_dca_price snapshot) ----------------------------------------
        # Populated by the module-level load_dca_state(manager) helper,
        # which main() calls BEFORE initialize_sync() (see config.py /
        # dca2.py). Only ever consumed by initialize_sync()'s resync
        # branch, and only after it independently confirms the snapshot's
        # side + avg_entry_price actually match what the exchange itself
        # reports for the current position - never applied blindly.
        self._persisted_dca_state: Optional[dict] = None

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

    # -- DCA lifecycle persistence (dca_step / entries / opened_at /
    # -- last_dca_price) - NOT built on Binance trade-history replay. Reuses
    # -- self.github_sync (same shared GitHub client as brain.pkl / CSV logs
    # -- / the trade-sync cursor) via its path= parameter. Fail-soft
    # -- throughout, same pattern as persist_brain() / _persist_trade_sync_cursor().

    async def save_dca_state(self, reason: str) -> None:
        """Snapshots the current DCA lifecycle (side, avg_entry_price used
        as the restart match key, dca_step, entries, opened_at,
        last_dca_price) to local disk + GitHub. Called after every DCA fill
        - never touches PositionState itself, and never touches entry/exit/
        TP/Smart-Exit/Brain/sizing logic."""
        p = self.position
        if p.status not in ("OPEN", "DCA_PENDING") or p.avg_entry_price is None:
            return

        state = {
            "side": p.side,
            "avg_entry_price": p.avg_entry_price,
            "dca_step": p.dca_step,
            "entries": [[price, qty] for price, qty in p.entries],
            "opened_at": p.opened_at,
            "last_dca_price": p.last_dca_price,
        }
        try:
            payload = json.dumps(state).encode("utf-8")
        except Exception as e:  # noqa: BLE001 - serialization must never crash the trading loop
            print(color(f"[dca-state] failed to serialize DCA state ({e}), skipping persist.", RED))
            return

        try:
            tmp_path = f"{DCA_STATE_PATH}.tmp"
            with open(tmp_path, "wb") as f:
                f.write(payload)
            os.replace(tmp_path, DCA_STATE_PATH)  # atomic on POSIX
        except Exception as e:  # noqa: BLE001
            print(color(f"[dca-state] failed to write {DCA_STATE_PATH} locally: {e}", RED))

        try:
            pushed = await self.github_sync.upload(
                payload, message=f"dca-state sync: {reason} (dca_step={p.dca_step})",
                path=GITHUB_DCA_STATE_PATH,
            )
            if pushed:
                print(color(
                    f"{now_str()} [dca-state] pushed DCA state to GitHub ({reason}, "
                    f"dca_step={p.dca_step}, side={p.side}, avg_entry={p.avg_entry_price:.2f})",
                    MAGENTA,
                ))
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; upload() already catches internally
            print(color(f"[dca-state] unexpected error during push (bot keeps trading): {e}", RED))

    async def clear_dca_state(self, reason: str) -> None:
        """Clears the persisted DCA snapshot after a full position close, so
        a later restart never restores a stale/finished lifecycle onto an
        unrelated future position. Fail-soft, same pattern as
        save_dca_state()."""
        try:
            if os.path.exists(DCA_STATE_PATH):
                os.remove(DCA_STATE_PATH)
        except Exception as e:  # noqa: BLE001
            print(color(f"[dca-state] failed to remove local {DCA_STATE_PATH}: {e}", YELLOW))

        try:
            await self.github_sync.upload(
                b"{}", message=f"dca-state clear: {reason}", path=GITHUB_DCA_STATE_PATH,
            )
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders; upload() already catches internally
            print(color(f"[dca-state] unexpected error clearing GitHub DCA state: {e}", RED))

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
        state - purely a logging safety net."""
        if DRY_RUN or self.client is None:
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
            print(color(f"[reconcile:{context}] could not fetch Binance trade history "
                        f"(continuing without it): {e}", YELLOW))
            return
        except Exception as e:  # noqa: BLE001 - reconciliation must never take the bot down
            print(color(f"[reconcile:{context}] unexpected error fetching trade history: {e}", RED))
            return

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
                    "binance_order_ids": sorted(order_ids),
                    "recovered": True,
                }
                self.trade_logger.log_trade(record)
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

    def _should_log_warmup_skip(self, interval_sec: float = 30.0) -> bool:
        now = time.time()
        if now - self._last_warmup_skip_log_ts >= interval_sec:
            self._last_warmup_skip_log_ts = now
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
            momentum = float(features[22]) if len(features) > 22 else 0.0  # momentum_short index

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
        smart_exit_loss_gate = pct_move <= -SMART_EXIT_MIN_LOSS_PCT
        smart_exit_near_dca = (
            dca_distance_pct > 0
            and (-pct_move) >= dca_distance_pct * SMART_EXIT_DCA_PROXIMITY_RATIO
        )
        if (
            SMART_EXIT_ENABLED and held_long_enough
            and smart_exit_loss_gate
            and pct_move > -SMART_EXIT_MAX_LOSS_PCT
            and not smart_exit_near_dca
        ):
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
            # routed to _on_close_filled(). Only reconcile_trade_history_from_
            # exchange() will ever pick this fill up, on its next run. Logging
            # only - the `return` below is unchanged.
            if _diag_status == "FILLED":
                print(color(
                    f"{now_str()} [fill-trace] path=live_user_websocket order_id={order_id} "
                    f"trade_id={_diag_trade_id} close_time={_diag_close_time} "
                    f"reason=untracked_order_id (order_id not in local _order_index - not placed "
                    f"by this process instance, e.g. a restart happened between order placement "
                    f"and this fill; _on_close_filled() will NOT run for this fill, only "
                    f"reconciliation will eventually recover it)", YELLOW,
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
            await self._on_entry_filled(role, fill_price, fill_qty)
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

        # Persist the DCA lifecycle snapshot after every DCA fill (not the
        # initial entry - a fresh position with dca_step==0 needs nothing to
        # restore). Fire-and-forget, same pattern as persist_brain()/
        # sync_trade_log_to_github() elsewhere in this class - never blocks
        # or interrupts the trading loop.
        if role == "dca":
            asyncio.create_task(self.save_dca_state(reason=f"dca fill step {self.position.dca_step}"))

    async def _on_close_filled(self, fill_price: float, total_rp: float, order_id: Optional[int] = None) -> None:
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
            "binance_order_ids": [int(order_id)] if order_id is not None else [],
        }
        self.trade_logger.log_trade(record)

        self.position = PositionState(last_close_time=time.time())

        asyncio.create_task(self.persist_brain(reason="trade closed"))
        asyncio.create_task(self.sync_trade_log_to_github())
        # Full close: the DCA lifecycle just ended, so the persisted
        # snapshot must never be restored onto a future, unrelated
        # position. Clearing it here (not just on the next successful
        # save) closes that gap.
        asyncio.create_task(self.clear_dca_state(reason="position closed"))
        if self._last_live_trade_id:
            asyncio.create_task(self._persist_trade_sync_cursor(
                self._last_live_trade_id, reason="live close"
            ))


# ============================================================================
# DCA STATE PERSISTENCE (module-level loader, called from dca2.py's main()
# BEFORE initialize_sync() - see config.py's DCA_STATE_PATH /
# GITHUB_DCA_STATE_PATH and this module's MartingaleManager.save_dca_state()/
# clear_dca_state()). Deliberately NOT built on Binance trade-history
# replay: this is a small, dedicated snapshot of just the DCA lifecycle
# fields (dca_step, entries, opened_at, last_dca_price), keyed by side +
# avg_entry_price so it can only ever be applied to the position it was
# actually taken from.
# ============================================================================


async def load_dca_state(manager: "MartingaleManager") -> None:
    """Startup: restores the persisted DCA lifecycle snapshot onto
    `manager._persisted_dca_state` (local disk first, falling back to
    GitHub via the same shared github_sync session as brain.pkl/CSV logs/
    the trade-sync cursor - same pattern as restore_csv_logs_from_github()/
    load_trade_sync_cursor()). This does NOT touch PositionState - at the
    point main() calls this (before initialize_sync()), the bot doesn't
    yet know what the exchange itself reports as open. initialize_sync()
    is the only place that ever applies this snapshot, and only after
    confirming it matches the exchange's reported side + avg_entry_price;
    a missing, corrupt, or non-matching snapshot is always ignored safely.
    Never raises."""
    manager._persisted_dca_state = None

    try:
        if os.path.exists(DCA_STATE_PATH) and os.path.getsize(DCA_STATE_PATH) > 0:
            with open(DCA_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict) and state.get("side") and state.get("avg_entry_price") is not None:
                manager._persisted_dca_state = state
                print(color(
                    f"[dca-state] loaded local {DCA_STATE_PATH} "
                    f"(side={state.get('side')}, dca_step={state.get('dca_step')}, "
                    f"avg_entry={state.get('avg_entry_price')})", MAGENTA,
                ))
                return
    except Exception as e:  # noqa: BLE001 - corrupt local file must not block startup
        print(color(f"[dca-state] local {DCA_STATE_PATH} unreadable ({e}), trying GitHub ...", YELLOW))

    try:
        data = await manager.github_sync.download(path=GITHUB_DCA_STATE_PATH)
    except Exception as e:  # noqa: BLE001 - restore must never block startup
        print(color(f"[dca-state] could not check GitHub for DCA state: {e}", YELLOW))
        return

    if not data:
        return

    try:
        state = json.loads(data.decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - corrupt/incompatible snapshot must not block startup
        print(color(f"[dca-state] failed to parse GitHub DCA state ({e}), ignoring.", YELLOW))
        return

    if not (isinstance(state, dict) and state.get("side") and state.get("avg_entry_price") is not None):
        return  # e.g. the "{}" written by clear_dca_state() after a full close - nothing to restore

    manager._persisted_dca_state = state
    try:
        tmp_path = f"{DCA_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp_path, DCA_STATE_PATH)
    except Exception as e:  # noqa: BLE001 - disk write failure shouldn't block using the snapshot
        print(color(f"[dca-state] could not cache downloaded DCA state to disk: {e}", YELLOW))

    print(color(
        f"[dca-state] restored from GitHub ({GITHUB_REPO}/{GITHUB_DCA_STATE_PATH}) "
        f"(side={state.get('side')}, dca_step={state.get('dca_step')}, "
        f"avg_entry={state.get('avg_entry_price')})", MAGENTA,
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
    their dataclass defaults automatically via PositionState()).

    2026-07 DCA-state persistence fix: when this function has to rebuild
    PositionState from the exchange's reported position (the *** RESYNCING
    TO MATCH EXCHANGE *** branch below), it now checks
    manager._persisted_dca_state (loaded by load_dca_state() before this
    function ever runs - see dca2.py's main()) and, ONLY if that snapshot's
    side and avg_entry_price actually match what the exchange itself just
    reported (within a small tolerance - exact equality can't be expected
    across float round-tripping / partial fills), restores dca_step,
    entries, opened_at, and last_dca_price from it instead of resetting
    dca_step to 0. A missing or non-matching snapshot changes nothing:
    the exact same reset-to-0 rebuild as before still runs. This never
    touches entry/exit/TP/Smart-Exit/Brain/sizing/risk logic."""
    if DRY_RUN:
        return  # nothing real to sync against

    # Trade-log reliability safety net - see reconcile_trade_history_from_exchange()
    # docstring. Runs on every startup / websocket-reconnect / periodic poll that
    # already calls this function, so no new timer is introduced. Independent of
    # the position-sync logic below: never touches PositionState.
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

    # --- DCA-state match check (2026-07 DCA-state persistence fix) ---------
    # Only ever consumed here. A persisted snapshot is restored ONLY if its
    # side matches AND its avg_entry_price is within a small tolerance of
    # what the exchange itself just reported for THIS position - otherwise
    # it's ignored safely (e.g. it belongs to a different, already-closed
    # lifecycle) and the existing reset-to-0 rebuild runs exactly as before.
    restored_dca: Optional[dict] = None
    persisted = manager._persisted_dca_state
    if persisted and persisted.get("side") == side and persisted.get("avg_entry_price") is not None and entry_price:
        try:
            persisted_avg = float(persisted["avg_entry_price"])
            tolerance = max(manager.filters.tick_size * 5, entry_price * 0.0015)
            if abs(persisted_avg - entry_price) <= tolerance:
                restored_dca = persisted
        except (TypeError, ValueError):
            restored_dca = None

    if restored_dca is not None:
        try:
            dca_step = int(restored_dca.get("dca_step", 0) or 0)
        except (TypeError, ValueError):
            dca_step = 0
        try:
            entries = [(float(pr), float(q)) for pr, q in (restored_dca.get("entries") or [])]
        except (TypeError, ValueError):
            entries = []
        if not entries:
            entries = [(entry_price, qty)]
        opened_at = restored_dca.get("opened_at") or time.time()
        last_dca_price = restored_dca.get("last_dca_price")

        print(color(
            f"{now_str()} [sync:{context}] *** RESYNCING TO MATCH EXCHANGE (DCA STATE RESTORED) *** "
            f"exchange shows side={side} qty={qty} avg_entry={entry_price:.2f}; local state "
            f"was status={p.status} side={p.side} avg_entry={p.avg_entry_price}. Persisted DCA "
            f"snapshot matches (avg_entry={restored_dca.get('avg_entry_price')}) - restoring "
            f"dca_step={dca_step}, entries={len(entries)}, opened_at, last_dca_price instead of "
            f"resetting dca_step to 0.",
            YELLOW,
        ))
        manager.position = PositionState(
            side=side,
            status="OPEN",
            dca_step=dca_step,
            entries=entries,
            avg_entry_price=entry_price,
            total_qty=qty,
            original_qty=qty,
            opened_at=opened_at,
            last_dca_price=last_dca_price,
            max_favorable_price=entry_price,
            max_adverse_price=entry_price,
        )
        return

    print(color(
        f"{now_str()} [sync:{context}] *** RESYNCING TO MATCH EXCHANGE *** "
        f"exchange shows side={side} qty={qty} avg_entry={entry_price:.2f}; local state "
        f"was status={p.status} side={p.side} avg_entry={p.avg_entry_price}. Rebuilding "
        f"local state so take-profit / hard-stop / DCA logic resumes managing this trade "
        f"(dca_step reset to 0 - no matching persisted DCA snapshot was found, or it didn't "
        f"match this position; review manually if that matters for your risk tolerance).",
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
