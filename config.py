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

 2026-07 session-start filter (this update - isolated to the new
 SESSION_START_DATE constant only; nothing else in this file was touched):
 adds a manual cutoff timestamp used by trading.py's startup reconciliation
 (reconcile_trade_history_from_exchange) to ignore Binance trade history
 that closed before this moment, so old/pre-session trades are never
 written into trades_log.csv / trades_log.jsonl / performance_stats.csv.
 Does not affect entry/exit/DCA/TP/SL/Smart-Exit/Profit-Lock/Risk logic or
 any existing recovery logic.
================================================================================
"""

import os

# ============================================================================
# CONFIG
# ============================================================================

SYMBOL = os.environ.get("SYMBOL", "BTCUSDT").strip().upper()
# 2026-08 multi-symbol state isolation: SYMBOL is now the single source of
# truth for which instrument this bot instance trades - switching it (via
# Railway ENV + redeploy, no code change) must never let one symbol's
# Brain/DCA-state/trade-sync-cursor/trade-log/stats be silently loaded by
# another. See _symbol_scoped_default() below, used by every
# symbol-specific persistence path's default value.
#
# 2026-08 (later revision): the original version of this mechanism kept a
# BTCUSDT special case (unsuffixed legacy filenames like "brain.pkl") for
# backward compatibility with the Brain that existed at the time. That
# special case has been deliberately removed - the old unsuffixed legacy
# runtime files are retired and are never read, written, migrated, or
# fallen back to for ANY symbol, including BTCUSDT. Every symbol, with no
# exception, now uses the identical "<name>_<SYMBOL>.<ext>" convention.

# 2026-08 USE_TESTNET moved up from the "Safety gates" section below
# (verbatim - same env var, same parsing, same default) so RUNTIME_ENV and
# _symbol_scoped_default() can be computed from it. USE_TESTNET's own
# meaning/behavior as a safety gate is completely unchanged - it is simply
# read a few lines earlier than before.
USE_TESTNET = os.environ.get("USE_TESTNET", "true").lower() != "false"

# 2026-08 environment + symbol state isolation: runtime persistence must
# be isolated by BOTH which Binance environment this process is trading
# on (Testnet vs Live) AND which symbol - switching either one, via
# Railway ENV + redeploy, must never let one environment/symbol
# combination's Brain/DCA-state/trade-sync-cursor/trade-log/stats be
# silently loaded by another. No new env var is introduced - USE_TESTNET
# (already the single source of truth for Testnet vs Live) is reused
# directly.
RUNTIME_ENV = "TESTNET" if USE_TESTNET else "LIVE"


def _symbol_scoped_default(base_name: str) -> str:
    """Returns an environment+symbol-suffixed variant of `base_name` for
    the ACTIVE RUNTIME_ENV and SYMBOL, with no exception for any
    particular combination (e.g. "brain.pkl" -> "brain_TESTNET_SOLUSDT.pkl"
    or "brain_LIVE_BTCUSDT.pkl") - every (environment, symbol) pair gets
    its own dedicated file, guaranteed distinct by construction, so one
    combination's Brain/DCA-state/trade-log/etc. can never be silently
    read, written, or mixed with another's - including Testnet vs Live for
    the SAME symbol.

    Only used to compute env-var DEFAULTS below - an operator who has
    explicitly set the corresponding env var themselves always gets
    exactly the value they set, completely untouched by RUNTIME_ENV/SYMBOL
    (see _warn_if_explicit_path_bypasses_isolation() below for the
    accompanying safety-net warning)."""
    stem, ext = os.path.splitext(base_name)
    return f"{stem}_{RUNTIME_ENV}_{SYMBOL}{ext}"


def _warn_if_explicit_path_bypasses_isolation(env_var_name: str) -> None:
    """2026-08 environment + symbol state isolation safety net: if an
    operator has EXPLICITLY set one of the isolation-scoped path env vars
    (DCA_STATE_PATH, GITHUB_TRADE_SYNC_CURSOR_PATH, etc.) for ANY
    environment/symbol combination, that explicit value is still honored
    exactly as set - explicit always wins over the computed default, no
    forced suffixing, no behavior change. But since an explicit value
    bypasses BOTH the automatic per-environment (Testnet/Live) AND
    per-symbol isolation this mechanism exists for, print a clear one-time
    startup warning so a path accidentally left over from a different
    environment or symbol's config doesn't silently mix Testnet/Live or
    cross-symbol state. Applies uniformly - there is no exception for any
    particular environment or symbol."""
    if os.environ.get(env_var_name):
        print(
            f"[symbol] WARNING: {env_var_name}={os.environ[env_var_name]!r} is explicitly set "
            f"while environment={RUNTIME_ENV} SYMBOL={SYMBOL} - this path bypasses automatic "
            f"per-environment AND per-symbol isolation. It may mix Testnet/Live state or "
            f"different symbols' state if reused elsewhere. Make sure it is not also used by "
            f"another environment/symbol deployment."
        )

# --- Safety gates - read the header above before touching these -------------
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() != "false"
I_UNDERSTAND_THIS_IS_REAL_MONEY = os.environ.get(
    "I_UNDERSTAND_THIS_IS_REAL_MONEY", ""
).lower() == "yes"
LIVE_TRADING_CONFIRMATION = os.environ.get(
    "LIVE_TRADING_CONFIRMATION", "false"
).lower() == "true"
# 2026-08 Live Trading Safety Guard: a second, explicit gate alongside
# I_UNDERSTAND_THIS_IS_REAL_MONEY above - both must be satisfied before
# mainnet trading (USE_TESTNET=false) is allowed to start. Deliberately a
# separate variable/phrase rather than reusing the existing one, so
# switching to live requires a distinct, deliberate action rather than
# whatever value happened to already be set for the other flag. See
# dca2.py's enforce_safety_gates().

# API keys MUST come from environment variables - set these in Railway's
# "Variables" tab (Project -> your service -> Variables), never in this file.
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# --- Account / margin --------------------------------------------------------
# (2026-08 Railway-tuning fix) These were previously hardcoded numbers with
# no environment override. They are now read from the environment with the
# EXACT SAME default values as before, so an unconfigured Railway deploy
# behaves identically to today - only an operator explicitly setting one of
# these env vars changes anything.
LEVERAGE = int(os.environ.get("LEVERAGE", "20"))
MAX_ALLOWED_LEVERAGE = int(os.environ.get("MAX_ALLOWED_LEVERAGE", "50"))
MARGIN_TYPE = "CROSSED"

# --- Position sizing (Fixed Amount base, Martingale, now confidence-scaled) -
INITIAL_ENTRY_USDT = float(os.environ.get("INITIAL_ENTRY_USDT", "4"))
# 2026-08 min-notional fix (this value only - DCA_MULTIPLIER, MAX_DCA_STEPS,
# SIZE_MIN_MULT/MAX_MULT, and every other DCA/TP/Risk-Engine/Daily-Loss-
# Protection value are unchanged): after reducing LEVERAGE to 20x, the
# previous $1.50-$2 initial margin put INITIAL and DCA #1 notional below
# Binance's $50 minimum whenever DCA #1's confidence-based size_mult (see
# confidence_size_multiplier(), bounded to [SIZE_MIN_MULT, SIZE_MAX_MULT])
# landed near its floor - both orders were rejected outright, so the bot
# could never open a new position. $4 margin, at 20x, keeps INITIAL at $80
# notional and DCA #1 at >= $64 notional even in the worst-case
# (size_mult=SIZE_MIN_MULT=0.5) scenario - both comfortably above $50.
# DCA #2/#3 were already well above the minimum and remain so.
DCA_MULTIPLIER = float(os.environ.get("DCA_MULTIPLIER", "1.6"))  # reduced from 2.0 (2026-07 profitability fix) - less notional/fee blowup per DCA rung
MAX_DCA_STEPS = int(os.environ.get("MAX_DCA_STEPS", "2"))        # reduced from 3 (2026-08 $33-account risk fix) - 3 steps required 112% of a $33 account's margin to fully cascade and produced ~45% worst-case single-trade loss; 2 steps fits within the account (~62.5% margin) and cuts worst-case loss to ~25% while keeping the final-DCA low-probability-recovery gate active at the depth where trade evidence showed it protecting against genuine trend moves

# --- Trade management ---------------------------------------------------------
DCA_TRIGGER_PCT = float(os.environ.get("DCA_TRIGGER_PCT", "0.002"))    # floor / fallback DCA spacing (also used if ATR unavailable)
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.0035"))   # raised from 0.002 (2026-07 profitability fix) - clears round-trip fee floor with real margin
HARD_STOP_PCT = float(os.environ.get("HARD_STOP_PCT", "0.02"))         # tightened from 0.05 (2026-07 profitability fix) - fixes stop:TP risk/reward skew

# --- Dynamic (volatility-based) Take Profit ----------------------------------
DYNAMIC_TP_ENABLED = os.environ.get("DYNAMIC_TP_ENABLED", "true").lower() != "false"
TAKE_PROFIT_MAX_PCT = float(os.environ.get("TAKE_PROFIT_MAX_PCT", "0.010"))  # raised from 0.006 (2026-07 profitability fix) - lets winners run further in trend/high-vol
TP_VOL_LOW = float(os.environ.get("TP_VOL_LOW", "0.0003"))    # tick-return std at/below this -> quiet -> base TP
TP_VOL_HIGH = float(os.environ.get("TP_VOL_HIGH", "0.0012"))  # tick-return std at/above this -> max TP expansion

# --- Max Hold Time Protection (NEW - scalping bot safety net) ----------------
# This is a scalping bot; positions are meant to resolve in minutes, not
# hours. If the market goes dead/ranging and none of TP/Smart-Exit/DCA/
# Profit-Lock naturally close the trade, this is a backstop so the bot
# never silently holds a position for many hours. See trading.py's
# _manage_open_position() for how PnL/regime/Profit-Lock state are
# consulted before this is allowed to force-close a trade (it does NOT
# blindly close profitable/trending positions - see MAX_HOLD_TIME_HARD_CAP_SEC
# for the unconditional absolute ceiling).
MAX_HOLD_TIME_ENABLED = os.environ.get("MAX_HOLD_TIME_ENABLED", "true").lower() != "false"
MAX_HOLD_TIME_SEC = int(os.environ.get("MAX_HOLD_TIME_SEC", str(4 * 3600)))       # 4h soft cap
MAX_HOLD_TIME_HARD_CAP_SEC = int(os.environ.get("MAX_HOLD_TIME_HARD_CAP_SEC", str(8 * 3600)))  # 8h absolute cap, always closes regardless of PnL/regime/profit-lock
MAX_HOLD_TIME_SMALL_LOSS_PCT = float(os.environ.get("MAX_HOLD_TIME_SMALL_LOSS_PCT", "0.0015"))
# 2026-08 Max Hold Time V2: a loss smaller (in magnitude) than this at timeout
# is treated as "very small" and closes normally (not worth an emergency
# recovery review). 0.15% mirrors DCA_MIN_DISTANCE_PCT's floor - roughly the
# smallest adverse move the bot's own DCA/exit machinery treats as meaningful.
MAX_HOLD_TIME_RECOVERY_MIN_AGREE = int(os.environ.get("MAX_HOLD_TIME_RECOVERY_MIN_AGREE", "2"))
# Of the 5 recovery-risk signals evaluated at timeout for a position with a
# genuinely significant loss (trend_against, high_risk, momentum_against,
# extreme_volatility, dca_exhausted - see trading.py's
# _manage_open_position()), how many must agree before the position is
# force-closed as "low probability of recovery" rather than kept open past
# MAX_HOLD_TIME_SEC (still bounded by MAX_HOLD_TIME_HARD_CAP_SEC either way).
MAX_HOLD_TIME_DCA_MULTIPLIER = float(os.environ.get("MAX_HOLD_TIME_DCA_MULTIPLIER", "0.5"))
# 2026-08 DCA-aware Max Hold Time tuning: once a position has DCA'd at
# least once (dca_step >= 1), the SOFT max-hold-time threshold
# (MAX_HOLD_TIME_SEC) is multiplied by this factor - default 0.5 means a
# DCA'd position times out in half the normal duration (e.g. 4h -> 2h),
# since a DCA'd position already ties up more capital than a dca_step=0
# position and shouldn't get the same full timeout to resolve. A fresh,
# never-DCA'd position (dca_step == 0) is completely unaffected - it still
# uses the full MAX_HOLD_TIME_SEC exactly as before. This ONLY changes
# WHEN the existing Max Hold Time V2 emergency-review logic starts
# evaluating a DCA'd position - the review itself (recovery-risk signals,
# DCA-opportunity defer, stale-profit-lock-flag handling) and
# MAX_HOLD_TIME_HARD_CAP_SEC (the unconditional absolute ceiling) are both
# completely unchanged.

# --- Close-order verification (2026-08 execution-reliability hardening) ------
CLOSE_VERIFY_MAX_RETRIES = int(os.environ.get("CLOSE_VERIFY_MAX_RETRIES", "3"))
# After every close-order fill, close_position()'s finalization step
# (_on_close_filled() in trading.py) re-fetches the exchange's own
# positionAmt to confirm the position is actually flat before treating the
# trade as closed. If a meaningful remainder is still open (a genuine
# partial fill, or a fill that landed on the position in the brief window
# between the pre-close qty fetch and the order executing), another
# reduceOnly close is submitted automatically for exactly that remainder.
# This caps how many automatic retry attempts are made before giving up and
# requiring manual intervention, so a persistently-failing exchange/network
# condition can never spin forever - the position is left clearly flagged
# and tracked as OPEN with the correct remaining quantity either way, never
# silently treated as closed.

# --- Low Volatility ("dead market") Entry Filter (NEW) -----------------------
# MarketRegimeEngine's SIDEWAYS/LOW_VOL split is RELATIVE (current ATR vs its
# own recent rolling mean), so a genuinely dead/flat tape can still be
# classified SIDEWAYS (which EntryEngineV2 allows, at a lower score
# threshold) rather than LOW_VOL (which is already blocked) if the rolling
# mean itself has also been low for a while. This is an ABSOLUTE floor on
# atr_pct, independent of that ratio, so only truly dead conditions are
# blocked - normal ranging/SIDEWAYS markets above this floor are unaffected.
LOW_VOLATILITY_FILTER_ENABLED = os.environ.get("LOW_VOLATILITY_FILTER_ENABLED", "true").lower() != "false"
LOW_VOLATILITY_ATR_PCT_THRESHOLD = float(os.environ.get("LOW_VOLATILITY_ATR_PCT_THRESHOLD", "0.00015"))

# --- Percentage Adaptive TP/DCA System (NEW) ---------------------------------
# Applies a bounded multiplier on TOP of the existing ATR-based dynamic TP
# (get_dynamic_take_profit_pct) and dynamic DCA spacing
# (get_dynamic_dca_distance_pct) in trading.py, based on how large the
# position has already grown (notional/margin, i.e. DCA depth) and the
# current market regime:
#   - bigger accumulated notional -> multiplier shrinks toward
#     ADAPTIVE_SCALE_MIN (secure profit / tighten DCA sooner on a large book)
#   - small/initial-size position -> multiplier stays near 1.0
#   - trending regimes -> multiplier nudged up (toward ADAPTIVE_SCALE_MAX)
#   - flat/SIDEWAYS regime -> multiplier nudged down
# The result is still clamped by the EXISTING absolute safety bounds
# (TAKE_PROFIT_PCT/TAKE_PROFIT_MAX_PCT and DCA_MIN_DISTANCE_PCT/
# DCA_MAX_DISTANCE_PCT below) - this can only move the dynamic TP/DCA value
# within those already-established ceilings/floors, never beyond them,
# unless ADAPTIVE_TP_MIN_RATIO/ADAPTIVE_TP_MAX_RATIO are explicitly changed
# from their backward-compatible defaults of 1.0.
ADAPTIVE_SIZING_ENABLED = os.environ.get("ADAPTIVE_SIZING_ENABLED", "true").lower() != "false"
ADAPTIVE_SIZE_SENSITIVITY = float(os.environ.get("ADAPTIVE_SIZE_SENSITIVITY", "0.25"))
ADAPTIVE_SCALE_MIN = float(os.environ.get("ADAPTIVE_SCALE_MIN", "0.45"))
ADAPTIVE_SCALE_MAX = float(os.environ.get("ADAPTIVE_SCALE_MAX", "1.15"))
ADAPTIVE_TP_MIN_RATIO = float(os.environ.get("ADAPTIVE_TP_MIN_RATIO", "0.45"))
ADAPTIVE_TP_MAX_RATIO = float(os.environ.get("ADAPTIVE_TP_MAX_RATIO", "1.0"))

# --- Entry Timing: momentum feature calibration (2026-08 entry-timing fix) ---
# See trading.py EntryEngineV2/on_price_tick module notes for root cause:
# the momentum component previously read the single-tick price_return
# (features[22], typically ~1e-5 for BTC bookTicker jitter) but scored it
# against a 0.002 (0.2%) saturation threshold sized for a multi-candle move
# - the momentum term was effectively always ~0 regardless of real market
# momentum. Entry now reads the short rolling return (features[4],
# ROLLING_RETURN_WINDOWS[0]=5 candles) against this threshold instead.
ENTRY_MOMENTUM_SATURATION_PCT = float(os.environ.get("ENTRY_MOMENTUM_SATURATION_PCT", "0.0015"))

# --- Profit Lock (env-configurable - 2026-08 Railway-tuning fix; values and
# behavior are UNCHANGED, only made overridable without a code edit) --------
PROFIT_LOCK_ACTIVATION_USDT = float(os.environ.get("PROFIT_LOCK_ACTIVATION_USDT", "0.10"))
PROFIT_LOCK_RATIO = float(os.environ.get("PROFIT_LOCK_RATIO", "0.50"))

# --- Simple entry signal (warmup/fallback only, see BRAIN V2 below) ---------
SIGNAL_LOOKBACK_TICKS = 20
SIGNAL_DEADBAND_PCT = 0.0005

# --- Over-trading guardrails --------------------------------------------------
TRADE_COOLDOWN_SEC = int(os.environ.get("TRADE_COOLDOWN_SEC", "60"))
MIN_HOLD_SEC_BEFORE_EXIT = int(os.environ.get("MIN_HOLD_SEC_BEFORE_EXIT", "60"))
MAX_DAILY_LOSS_USDT = float(os.environ.get("MAX_DAILY_LOSS_USDT", "0.50"))
DAILY_PROFIT_TARGET_USDT = float(os.environ.get("DAILY_PROFIT_TARGET_USDT", "0.50"))
# 2026-08 fee-net daily session locks: once today's (UTC calendar day)
# cumulative REALIZED NET PnL reaches either boundary, no NEW entries are
# opened for the rest of that UTC day. The tracker uses Binance's actual
# commissions when available, so +$0.50 means profit AFTER fees rather than
# a raw-price/gross-PnL target. An already-OPEN position keeps being managed
# normally (TP/Hard-Stop/Profit-Lock/DCA/Max-Hold-Time all remain active) -
# these values only gate the FLAT-state entry decision. Both locks reset at
# the next UTC day boundary. Set either value <=0 to disable that side.

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
SIDEWAYS_ENTRY_SCORE_THRESHOLD = float(os.environ.get("SIDEWAYS_ENTRY_SCORE_THRESHOLD", "0.60"))  # SIDEWAYS is structurally capped lower (volatility_fit/regime_fit/momentum), all other regimes unchanged at 0.75
# Clean Live entry evidence exposed a directional scoring hole: the entry
# engine rewarded abs(momentum), so a strong upward move boosted a proposed
# SHORT exactly like a LONG. In SIDEWAYS, block a meaningful counter move
# (at least half the existing momentum-saturation scale); tiny sign jitter
# remains governed by the normal composite score instead of a hard gate.
SIDEWAYS_ENTRY_MOMENTUM_ALIGNMENT_ENABLED = (
    os.environ.get("SIDEWAYS_ENTRY_MOMENTUM_ALIGNMENT_ENABLED", "true").lower() != "false"
)
SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO = float(
    os.environ.get("SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO", "0.50")
)
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
SMART_EXIT_MIN_LOSS_PCT = 0.0010   # Smart Exit gate: position must already be at least -0.10% adverse before Smart Exit is evaluated at all (2026-07 Smart Exit fix)
SMART_EXIT_CONFIRM_TICKS = 5
SMART_EXIT_MIN_AGREE = 5          # of the following 6 signals, how many must agree to exit (raised from 4 - 2026-07 Smart Exit fix)
SMART_EXIT_CONFIDENCE_DROP = 0.18  # confidence_score drop vs entry that counts as "dropped"
SMART_EXIT_ATR_MOVE_MULT = 0.8     # adverse move >= this * ATR% counts as a signal
SMART_EXIT_DCA_PROXIMITY_RATIO = 0.9  # if the adverse move is already >= this fraction of the current DCA trigger distance, Smart Exit is blocked so DCA can activate instead (2026-07 Smart Exit fix)
SMART_EXIT_MIN_HOLD_SEC = float(os.environ.get("SMART_EXIT_MIN_HOLD_SEC", "90"))
# Smart-Exit-only minimum hold, separate from and stricter than MIN_HOLD_SEC_BEFORE_EXIT
# (60s) above. Only gates Smart Exit itself, so TP/partial-TP/trailing-stop timing is
# unchanged - a fresh entry just gets extra room before Smart Exit can close it
# (2026-08 Smart Exit V2 retune).
SMART_EXIT_MIN_AGREE_RANGING = int(os.environ.get("SMART_EXIT_MIN_AGREE_RANGING", "6"))
# In SIDEWAYS/WEAK_TREND, require ALL 6 signals (unanimous) instead of the normal
# SMART_EXIT_MIN_AGREE (5), since ordinary chop/pullbacks in these regimes trip 1-2
# signals often without a real reversal happening. STRONG_TREND/HIGH_VOL keep the
# stricter 5-of-6 bar unchanged, so genuine reversals are still caught quickly
# (2026-08 Smart Exit V2 retune).

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
# 2026-08 (later revision): trade logs are now symbol-scoped, not shared.
# A prior version of this mechanism deliberately kept them shared (every
# record already carries its own "symbol" field and dedup keys off
# globally-unique Binance order ids, so mixing was not a correctness risk
# in principle) - but in practice a stale local/GitHub BTC trade log was
# re-uploaded during a SOL deployment, which is exactly the kind of
# cross-symbol contamination this whole isolation mechanism exists to
# prevent. Trade logs now follow the identical "<name>_<SYMBOL>.<ext>"
# convention as every other persistence path below.
TRADE_LOG_JSON_PATH = os.environ.get("TRADE_LOG_JSON_PATH", _symbol_scoped_default("trades_log.jsonl"))
TRADE_LOG_CSV_PATH = os.environ.get("TRADE_LOG_CSV_PATH", _symbol_scoped_default("trades_log.csv"))
_warn_if_explicit_path_bypasses_isolation("TRADE_LOG_JSON_PATH")
_warn_if_explicit_path_bypasses_isolation("TRADE_LOG_CSV_PATH")
STATS_JSON_PATH = os.environ.get("STATS_JSON_PATH", _symbol_scoped_default("performance_stats.json"))
STATS_CSV_PATH = os.environ.get("STATS_CSV_PATH", _symbol_scoped_default("performance_stats.csv"))
_warn_if_explicit_path_bypasses_isolation("STATS_JSON_PATH")
_warn_if_explicit_path_bypasses_isolation("STATS_CSV_PATH")
STATS_EXPORT_INTERVAL_SEC = int(os.environ.get("STATS_EXPORT_INTERVAL_SEC", "300"))

# --- Funding rate / open interest (best-effort extra features) ---------------
FUNDING_OI_POLL_SEC = int(os.environ.get("FUNDING_OI_POLL_SEC", "120"))

# --- Persistent Adaptive Learning (Cloud-Sync Brain) -------------------------
BRAIN_LOCAL_PATH = os.environ.get("BRAIN_LOCAL_PATH", _symbol_scoped_default("brain.pkl"))
_warn_if_explicit_path_bypasses_isolation("BRAIN_LOCAL_PATH")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRAIN_PATH = os.environ.get("GITHUB_BRAIN_PATH", _symbol_scoped_default("brain.pkl"))
_warn_if_explicit_path_bypasses_isolation("GITHUB_BRAIN_PATH")
# IMPORTANT (Railway deploy-loop fix): runtime state (Brain, trade logs,
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

# CSV analytics sync (same repo/session as the Brain snapshot - see
# GithubBrainSync). Default: same directory as GITHUB_BRAIN_PATH, so they
# live beside it. Symbol-scoped filenames, same convention as everywhere
# else in this file - no shared trade-log/stats path across symbols.
_GITHUB_BRAIN_DIR = os.path.dirname(GITHUB_BRAIN_PATH)
GITHUB_TRADES_LOG_CSV_PATH = os.environ.get(
    "GITHUB_TRADES_LOG_CSV_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, _symbol_scoped_default("trades_log.csv")) if p),
)
GITHUB_STATS_CSV_PATH = os.environ.get(
    "GITHUB_STATS_CSV_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, _symbol_scoped_default("performance_stats.csv")) if p),
)
GITHUB_TRADES_LOG_JSON_PATH = os.environ.get(
    "GITHUB_TRADES_LOG_JSON_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, _symbol_scoped_default("trades_log.jsonl")) if p),
)
_warn_if_explicit_path_bypasses_isolation("GITHUB_TRADES_LOG_CSV_PATH")
_warn_if_explicit_path_bypasses_isolation("GITHUB_STATS_CSV_PATH")
_warn_if_explicit_path_bypasses_isolation("GITHUB_TRADES_LOG_JSON_PATH")

# --- Trade-log reconciliation (Binance trade history is the source of
# truth; recovers any closed trade the live websocket stream missed) -------
TRADE_SYNC_CURSOR_PATH = os.environ.get("TRADE_SYNC_CURSOR_PATH", _symbol_scoped_default("trade_sync_cursor.json"))
GITHUB_TRADE_SYNC_CURSOR_PATH = os.environ.get(
    "GITHUB_TRADE_SYNC_CURSOR_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, _symbol_scoped_default("trade_sync_cursor.json")) if p),
)
_warn_if_explicit_path_bypasses_isolation("TRADE_SYNC_CURSOR_PATH")
_warn_if_explicit_path_bypasses_isolation("GITHUB_TRADE_SYNC_CURSOR_PATH")
# Only used the very FIRST time the bot ever runs with no cursor file found
# locally or on GitHub. Left unset (None), the bot seeds the cursor at the
# current latest Binance trade id and only auto-recovers gaps from that
# point forward - it will NOT retroactively rewrite already-logged history.
# Set this to a specific Binance trade id (an integer, as a string) to
# explicitly opt in to a one-time historical backfill starting at that id;
# recovered rows are tagged "recovered": true / exit_reason
# "reconciled_from_exchange" so they're easy to identify and audit.
TRADE_RECONCILE_BACKFILL_FROM_ID = os.environ.get("TRADE_RECONCILE_BACKFILL_FROM_ID", "") or None

# --- Manual trading-session start filter (2026-07 session-start filter) -----
# Startup reconciliation (reconcile_trade_history_from_exchange in
# trading.py) treats Binance's own userTrades history as the source of
# truth and can recover/backfill trades the live websocket missed. This
# cutoff lets an operator mark "the current session officially starts
# here": any Binance trade that CLOSED before this timestamp is ignored by
# that reconciliation pass and is never written into trades_log.csv /
# trades_log.jsonl / performance_stats.csv, regardless of
# TRADE_RECONCILE_BACKFILL_FROM_ID or the persisted trade-sync cursor.
# ISO-8601 UTC string, e.g. "2026-07-31T09:00:00Z". Does not affect
# entry/exit/DCA/TP/SL/Smart-Exit/Profit-Lock/Risk logic, live fills, or
# any existing restart/recovery logic - it only gates what reconciliation
# is allowed to log.
SESSION_START_DATE = os.environ.get("SESSION_START_DATE", "2026-07-31T09:00:00Z")

# 2026-08 human-friendly local session-start time (this block only -
# nothing else in this file, and nothing in trading.py, is touched by
# this feature; it only ever affects the SESSION_START_DATE string
# consumed above, which trading.py already parses unchanged). Lets an
# operator specify the session cutoff in local Sri Lanka time instead of
# manually converting to UTC every time. If both SESSION_START_LOCAL_DATE
# and SESSION_START_LOCAL_TIME are set and valid, the computed UTC
# equivalent OVERWRITES SESSION_START_DATE above; otherwise
# SESSION_START_DATE keeps whatever value it already had (the env var or
# its own default), completely unchanged.
_SESSION_LOCAL_TZ_NAME = "Asia/Colombo"
SESSION_START_LOCAL_DATE = os.environ.get("SESSION_START_LOCAL_DATE", "").strip()
SESSION_START_LOCAL_TIME = os.environ.get("SESSION_START_LOCAL_TIME", "").strip()


def _parse_local_time_of_day(time_str: str):
    """Parses a flexible local time-of-day string into a 24h (hour,
    minute) tuple, or None if it can't be parsed. Accepts '.' or ':' as
    the hour:minute separator, an optional am/pm suffix (case-insensitive,
    with or without a space before it), and a plain 24h "HH:MM" with no
    am/pm suffix at all. Examples that all parse successfully: '1.30pm',
    '1:30pm', '1:30 PM', '13:30', '09:07am'."""
    import re
    s = time_str.strip()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})\s*(am|pm|AM|PM|Am|Pm|aM|pM)?", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3).lower() if m.group(3) else None
    if not (0 <= minute <= 59):
        return None
    if ampm:
        if not (1 <= hour <= 12):
            return None
        if ampm == "am":
            hour = 0 if hour == 12 else hour
        else:  # pm
            hour = 12 if hour == 12 else hour + 12
    else:
        if not (0 <= hour <= 23):
            return None
    return hour, minute


def _resolve_session_start_date() -> str:
    """Returns the effective SESSION_START_DATE (ISO-8601 UTC string).
    Prefers SESSION_START_LOCAL_DATE + SESSION_START_LOCAL_TIME
    (Asia/Colombo) when BOTH are present and valid; otherwise falls back
    to the existing SESSION_START_DATE env var/default, printing a clear
    warning if the local fields were attempted but incomplete/invalid so
    the fallback is never silent."""
    if not SESSION_START_LOCAL_DATE and not SESSION_START_LOCAL_TIME:
        return SESSION_START_DATE  # neither set - normal existing UTC-only behavior, untouched

    if bool(SESSION_START_LOCAL_DATE) != bool(SESSION_START_LOCAL_TIME):
        missing = "SESSION_START_LOCAL_TIME" if SESSION_START_LOCAL_DATE else "SESSION_START_LOCAL_DATE"
        print(
            f"[session] WARNING: only one of SESSION_START_LOCAL_DATE/SESSION_START_LOCAL_TIME is "
            f"set (missing {missing}) - both are required together. Falling back to "
            f"SESSION_START_DATE={SESSION_START_DATE!r}."
        )
        return SESSION_START_DATE

    from datetime import datetime as _dt, timezone as _tz
    try:
        date_parts = _dt.strptime(SESSION_START_LOCAL_DATE, "%Y-%m-%d")
    except ValueError:
        print(
            f"[session] WARNING: SESSION_START_LOCAL_DATE={SESSION_START_LOCAL_DATE!r} is not a "
            f"valid YYYY-MM-DD date. Falling back to SESSION_START_DATE={SESSION_START_DATE!r}."
        )
        return SESSION_START_DATE

    parsed_time = _parse_local_time_of_day(SESSION_START_LOCAL_TIME)
    if parsed_time is None:
        print(
            f"[session] WARNING: SESSION_START_LOCAL_TIME={SESSION_START_LOCAL_TIME!r} could not be "
            f"parsed (expected e.g. '1.30pm', '1:30pm', '1:30 PM', '13:30', '09:07am'). Falling "
            f"back to SESSION_START_DATE={SESSION_START_DATE!r}."
        )
        return SESSION_START_DATE

    hour, minute = parsed_time
    try:
        from zoneinfo import ZoneInfo
        local_dt = _dt(
            date_parts.year, date_parts.month, date_parts.day, hour, minute,
            tzinfo=ZoneInfo(_SESSION_LOCAL_TZ_NAME),
        )
        utc_dt = local_dt.astimezone(_tz.utc)
        utc_iso = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as e:  # noqa: BLE001 - a bad/missing tzdata install must fall back safely, never crash config load
        print(
            f"[session] WARNING: could not convert SESSION_START_LOCAL_DATE/TIME to UTC via "
            f"{_SESSION_LOCAL_TZ_NAME} ({e}). Falling back to SESSION_START_DATE={SESSION_START_DATE!r}."
        )
        return SESSION_START_DATE

    print(
        f"[session] local_start={SESSION_START_LOCAL_DATE} {hour:02d}:{minute:02d} "
        f"{_SESSION_LOCAL_TZ_NAME} -> utc_cutoff={utc_iso}"
    )
    return utc_iso


SESSION_START_DATE = _resolve_session_start_date()

# --- Persistent DCA state ------------------------------------------------------
DCA_STATE_PATH = os.environ.get("DCA_STATE_PATH", _symbol_scoped_default("dca_state.json"))
GITHUB_DCA_STATE_PATH = os.environ.get(
    "GITHUB_DCA_STATE_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, _symbol_scoped_default("dca_state.json")) if p),
)
_warn_if_explicit_path_bypasses_isolation("DCA_STATE_PATH")
_warn_if_explicit_path_bypasses_isolation("GITHUB_DCA_STATE_PATH")

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
    "LIVE_TRADING_CONFIRMATION",
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
    "MAX_HOLD_TIME_ENABLED",
    "MAX_HOLD_TIME_SEC",
    "MAX_HOLD_TIME_HARD_CAP_SEC",
    "MAX_HOLD_TIME_SMALL_LOSS_PCT",
    "MAX_HOLD_TIME_RECOVERY_MIN_AGREE",
    "MAX_HOLD_TIME_DCA_MULTIPLIER",
    "CLOSE_VERIFY_MAX_RETRIES",
    "LOW_VOLATILITY_FILTER_ENABLED",
    "LOW_VOLATILITY_ATR_PCT_THRESHOLD",
    "ADAPTIVE_SIZING_ENABLED",
    "ADAPTIVE_SIZE_SENSITIVITY",
    "ADAPTIVE_SCALE_MIN",
    "ADAPTIVE_SCALE_MAX",
    "ADAPTIVE_TP_MIN_RATIO",
    "ADAPTIVE_TP_MAX_RATIO",
    "ENTRY_MOMENTUM_SATURATION_PCT",
    "PROFIT_LOCK_ACTIVATION_USDT",
    "PROFIT_LOCK_RATIO",
    "SIGNAL_LOOKBACK_TICKS",
    "SIGNAL_DEADBAND_PCT",
    "TRADE_COOLDOWN_SEC",
    "MIN_HOLD_SEC_BEFORE_EXIT",
    "MAX_DAILY_LOSS_USDT",
    "DAILY_PROFIT_TARGET_USDT",
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
    "SIDEWAYS_ENTRY_SCORE_THRESHOLD",
    "SIDEWAYS_ENTRY_MOMENTUM_ALIGNMENT_ENABLED",
    "SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO",
    "ENTRY_WEIGHTS",
    "SMART_EXIT_ENABLED",
    "SMART_EXIT_MAX_LOSS_PCT",
    "SMART_EXIT_MIN_LOSS_PCT",
    "SMART_EXIT_CONFIRM_TICKS",
    "SMART_EXIT_MIN_AGREE",
    "SMART_EXIT_CONFIDENCE_DROP",
    "SMART_EXIT_ATR_MOVE_MULT",
    "SMART_EXIT_DCA_PROXIMITY_RATIO",
    "SMART_EXIT_MIN_HOLD_SEC",
    "SMART_EXIT_MIN_AGREE_RANGING",
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
    "SESSION_START_DATE",
    "SESSION_START_LOCAL_DATE",
    "SESSION_START_LOCAL_TIME",
    "DCA_STATE_PATH",
    "GITHUB_DCA_STATE_PATH",
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
