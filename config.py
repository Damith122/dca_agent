#!/usr/bin/env python3
"""
================================================================================
 Configuration for the Martingale DCA Scalper (dca2.py)

 This file contains ONLY what was moved out of dca2.py's original
 "# ==== CONFIG ====" block. Nothing here was changed, renamed, recalculated,
 or "improved" - it is a verbatim relocation. Behavior, defaults, and every
 environment variable name are identical to before this refactor.

 Railway / any host: no environment variable changes are required. This file
 reads the exact same env vars, with the exact same defaults, as before.
================================================================================
"""

import os

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
DCA_MULTIPLIER = 1.6            # reduced from 2.0 (2026-07 profitability fix) - less notional/fee blowup per DCA rung
MAX_DCA_STEPS = 3               # reduced from 5 (2026-07 profitability fix) - caps worst-case martingale depth

# --- Trade management ---------------------------------------------------------
DCA_TRIGGER_PCT = 0.002          # floor / fallback DCA spacing (also used if ATR unavailable)
TAKE_PROFIT_PCT = 0.0035         # raised from 0.002 (2026-07 profitability fix) - clears round-trip fee floor with real margin
HARD_STOP_PCT = 0.02            # tightened from 0.05 (2026-07 profitability fix) - fixes stop:TP risk/reward skew

# --- Dynamic (volatility-based) Take Profit ----------------------------------
DYNAMIC_TP_ENABLED = True
TAKE_PROFIT_MAX_PCT = 0.010      # raised from 0.006 (2026-07 profitability fix) - lets winners run further in trend/high-vol
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
REGIME_TREND_SLOPE_STRONG = 0.00060   # EMA_FAST slope (pct/candle) -> STRONG_TREND
REGIME_TREND_SLOPE_WEAK   = 0.00020     # EMA_FAST slope (pct/candle) -> WEAK_TREND
REGIME_LOOKBACK_CANDLES = 30

# --- Brain V2 --------------------------------------------------------------
N_FEATURES_V2 = 34
BRAIN2_WARMUP_UPDATES = int(os.environ.get("BRAIN2_WARMUP_UPDATES", "80"))
LABEL_HORIZON_TICKS = 10
FEATURE_SHORT_LOOKBACK = 5
RECENT_TRADE_WINDOW = 20
TP_HIT_LOOKAHEAD_CANDLES = 8      # how far ahead we check "did price reach TP-ish move"

# --- Entry Engine V2 ---------------------------------------------------------
ENTRY_SCORE_THRESHOLD = float(os.environ.get("ENTRY_SCORE_THRESHOLD", "0.75"))  # raised from 0.60 (2026-07 profitability fix)
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
PARTIAL_TP_ENABLED = os.environ.get("PARTIAL_TP_ENABLED", "false").lower() != "false"  # disabled by default (2026-07 profitability fix) - was truncating winners; trailing stop remains active
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
# IMPORTANT (Railway deploy-loop fix): runtime state (brain.pkl, trade logs,
# performance stats, sync cursor) is committed by the bot itself while it is
# running. Railway's GitHub integration redeploys on every push to the branch
# it is connected to (normally "main"). If GITHUB_BRANCH == that branch, each
# runtime commit triggers a redeploy -> restart -> another commit -> infinite
# deploy loop. To break that loop, runtime commits go to a DEDICATED branch
# (default "brain-state") that Railway is never connected to, while Railway
# keeps deploying only from "main" on real code pushes. GithubBrainSync will
# auto-create this branch on first use if it doesn't exist yet - no manual
# GitHub setup required. Do not set this to the same branch Railway deploys
# from.
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "brain-state")
BRAIN_AUTO_PUSH_INTERVAL_SEC = int(os.environ.get("BRAIN_AUTO_PUSH_INTERVAL_SEC", "300"))

# CSV analytics sync (same repo/session as brain.pkl - see GithubBrainSync).
# Default: same directory as GITHUB_BRAIN_PATH, so they live beside brain.pkl.
_GITHUB_BRAIN_DIR = os.path.dirname(GITHUB_BRAIN_PATH)
GITHUB_TRADES_LOG_CSV_PATH = os.environ.get(
    "GITHUB_TRADES_LOG_CSV_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, "trades_log.csv") if p),
)
GITHUB_STATS_CSV_PATH = os.environ.get(
    "GITHUB_STATS_CSV_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, "performance_stats.csv") if p),
)
GITHUB_TRADES_LOG_JSON_PATH = os.environ.get(
    "GITHUB_TRADES_LOG_JSON_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, "trades_log.jsonl") if p),
)

# --- Trade-log reconciliation (Binance trade history is the source of
# truth; recovers any closed trade the live websocket stream missed) -------
TRADE_SYNC_CURSOR_PATH = os.environ.get("TRADE_SYNC_CURSOR_PATH", "trade_sync_cursor.json")
GITHUB_TRADE_SYNC_CURSOR_PATH = os.environ.get(
    "GITHUB_TRADE_SYNC_CURSOR_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, "trade_sync_cursor.json") if p),
)
# Only used the very FIRST time the bot ever runs with no cursor file found
# locally or on GitHub. Left unset (None), the bot seeds the cursor at the
# current latest Binance trade id and only auto-recovers gaps from that
# point forward - it will NOT retroactively rewrite already-logged history.
# Set this to a specific Binance trade id (an integer, as a string) to
# explicitly opt in to a one-time historical backfill starting at that id;
# recovered rows are tagged "recovered": true / exit_reason
# "reconciled_from_exchange" so they're easy to identify and audit.
TRADE_RECONCILE_BACKFILL_FROM_ID = os.environ.get("TRADE_RECONCILE_BACKFILL_FROM_ID", "") or None

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


__all__ = [
    "SYMBOL",
    "USE_TESTNET",
    "DRY_RUN",
    "I_UNDERSTAND_THIS_IS_REAL_MONEY",
    "API_KEY",
    "API_SECRET",
    "LEVERAGE",
    "MAX_ALLOWED_LEVERAGE",
    "MARGIN_TYPE",
    "INITIAL_ENTRY_USDT",
    "DCA_MULTIPLIER",
    "MAX_DCA_STEPS",
    "DCA_TRIGGER_PCT",
    "TAKE_PROFIT_PCT",
    "HARD_STOP_PCT",
    "DYNAMIC_TP_ENABLED",
    "TAKE_PROFIT_MAX_PCT",
    "TP_VOL_LOW",
    "TP_VOL_HIGH",
    "SIGNAL_LOOKBACK_TICKS",
    "SIGNAL_DEADBAND_PCT",
    "TRADE_COOLDOWN_SEC",
    "MIN_HOLD_SEC_BEFORE_EXIT",
    "TAKER_FEE_RATE",
    "MIN_NET_PROFIT_USDT",
    "LIQUIDATION_SANITY_MIN_RATIO",
    "LIQUIDATION_SANITY_MAX_RATIO",
    "LIQUIDATION_WARNING_BUFFER_PCT",
    "SYNC_PENDING_GRACE_SEC",
    "CANDLE_INTERVAL_SEC",
    "CANDLE_HISTORY",
    "ATR_PERIOD",
    "EMA_FAST",
    "EMA_MED",
    "EMA_SLOW",
    "ROLLING_RETURN_WINDOWS",
    "REGIME_ATR_HIGH_MULT",
    "REGIME_ATR_LOW_MULT",
    "REGIME_TREND_SLOPE_STRONG",
    "REGIME_TREND_SLOPE_WEAK",
    "REGIME_LOOKBACK_CANDLES",
    "N_FEATURES_V2",
    "BRAIN2_WARMUP_UPDATES",
    "LABEL_HORIZON_TICKS",
    "FEATURE_SHORT_LOOKBACK",
    "RECENT_TRADE_WINDOW",
    "TP_HIT_LOOKAHEAD_CANDLES",
    "ENTRY_SCORE_THRESHOLD",
    "ENTRY_WEIGHTS",
    "SMART_EXIT_ENABLED",
    "SMART_EXIT_MAX_LOSS_PCT",
    "SMART_EXIT_CONFIRM_TICKS",
    "SMART_EXIT_MIN_AGREE",
    "SMART_EXIT_CONFIDENCE_DROP",
    "SMART_EXIT_ATR_MOVE_MULT",
    "DCA_ATR_MULTIPLIER",
    "DCA_MIN_DISTANCE_PCT",
    "DCA_MAX_DISTANCE_PCT",
    "SIZE_MIN_MULT",
    "SIZE_MAX_MULT",
    "PARTIAL_TP_ENABLED",
    "PARTIAL_TP_FRACTION",
    "PARTIAL_TP_TRIGGER_RATIO",
    "BREAKEVEN_AFTER_PARTIAL",
    "TRAILING_STOP_ENABLED",
    "TRAILING_STOP_ATR_MULT",
    "TRADE_LOG_JSON_PATH",
    "TRADE_LOG_CSV_PATH",
    "STATS_JSON_PATH",
    "STATS_CSV_PATH",
    "STATS_EXPORT_INTERVAL_SEC",
    "FUNDING_OI_POLL_SEC",
    "BRAIN_LOCAL_PATH",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "GITHUB_BRAIN_PATH",
    "GITHUB_BRANCH",
    "GITHUB_TRADES_LOG_CSV_PATH",
    "GITHUB_STATS_CSV_PATH",
    "GITHUB_TRADES_LOG_JSON_PATH",
    "TRADE_SYNC_CURSOR_PATH",
    "GITHUB_TRADE_SYNC_CURSOR_PATH",
    "TRADE_RECONCILE_BACKFILL_FROM_ID",
    "BRAIN_AUTO_PUSH_INTERVAL_SEC",
    "LISTEN_KEY_KEEPALIVE_SEC",
    "BALANCE_REFRESH_SEC",
    "POSITION_RISK_POLL_SEC",
    "MAX_BACKOFF_SEC",
    "IDLE_DATA_TIMEOUT_SEC",
    "USER_WS_IDLE_FALLBACK_SEC",
    "STARTUP_RETRY_ATTEMPTS",
    "STARTUP_RETRY_BASE_DELAY_SEC",
    "SUPERVISOR_RESTART_DELAY_SEC",
    "REST_BASE",
    "WS_MARKET_BASE",
    "WS_USERDATA_BASE",
]
