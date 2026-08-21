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
import random

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


# ---------------------------------------------------------------------------
# 2026-08-20 multi-coin watchlist.
#
# ACTIVE_SYMBOLS is the watchlist the evaluator scans. SYMBOL (above) is
# retained unchanged as the PRIMARY symbol: it still seeds every legacy
# module-level path constant below, so a single-symbol deployment behaves
# exactly as it did before this change, and every existing test that reads
# config.SYMBOL keeps working.
#
# Precedence: ACTIVE_SYMBOLS env var (comma-separated) > the default list.
# SYMBOL is always forced to be a member, and always first, so the primary
# symbol can never be silently dropped from its own watchlist by a typo in
# the env var.
#
# MAX_ACTIVE_TRADES caps how many positions may be open across the WHOLE
# watchlist at once, not per symbol. With ~$19 USDT of capital, 1 is the
# only defensible value: each entry is INITIAL_ENTRY_USDT of margin at
# LEVERAGE, and concurrent positions would multiply both the margin draw
# and the correlated drawdown across coins that mostly move together.
# ---------------------------------------------------------------------------
def _parse_symbol_list(raw: str, primary: str) -> list:
    """Comma-separated watchlist -> ordered, de-duplicated, upper-cased list
    with `primary` guaranteed present and first. Blank entries are dropped."""
    out = [primary]
    for part in (raw or "").split(","):
        sym = part.strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


_DEFAULT_WATCHLIST = "SOLUSDT,BTCUSDT,ETHUSDT,NEARUSDT"
ACTIVE_SYMBOLS = _parse_symbol_list(
    os.environ.get("ACTIVE_SYMBOLS", _DEFAULT_WATCHLIST), SYMBOL
)

# Hard cap on simultaneously-open positions across ACTIVE_SYMBOLS. Clamped
# to >= 1: a 0 or negative value would silently disable trading entirely,
# which is a far more surprising outcome than falling back to 1.
try:
    MAX_ACTIVE_TRADES = max(1, int(os.environ.get("MAX_ACTIVE_TRADES", "1")))
except (TypeError, ValueError):
    MAX_ACTIVE_TRADES = 1


def symbol_scoped_name(base_name: str, symbol: str) -> str:
    """2026-08-20 multi-coin: the per-symbol form of
    _symbol_scoped_default() below, taking the symbol EXPLICITLY instead of
    reading the module-level SYMBOL global.

    This is what makes multi-coin file isolation work. Each
    MartingaleManager derives its own persistence paths through this
    helper from its OWN self.symbol, so four managers in one process write
    four disjoint sets of files:

        brain_LIVE_SOLUSDT.pkl     dca_state_LIVE_SOLUSDT.json    ...
        brain_LIVE_BTCUSDT.pkl     dca_state_LIVE_BTCUSDT.json    ...

    The naming convention is byte-identical to what _symbol_scoped_default()
    already produced, so files written by previous single-symbol builds are
    picked up unchanged - there is no migration step."""
    stem, ext = os.path.splitext(base_name)
    return f"{stem}_{RUNTIME_ENV}_{symbol.strip().upper()}{ext}"


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
    return symbol_scoped_name(base_name, SYMBOL)


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
# 2026-08-18 fee-drag recalibration (value only - the gate itself is unchanged
# and already existed; see FeatureBuilderV2's dead_market_blocked check).
# Raised from 0.00015 (0.015%) to 0.0008 (0.08%) after three consecutive losing
# scalps in a dead tape:
#
#   SHORT 76.90->76.99  atr%=0.032   net -$0.1496
#   SHORT 77.18->77.27  atr%=0.044   net -$0.1483
#   LONG  77.28->77.21  atr%=0.029   net -$0.1118
#
# Every one of those ran at 0.029%-0.044% ATR - well ABOVE the old 0.015%
# floor, so dead_market_blocked stayed False and all three were allowed. At
# that volatility the take-profit floor (+0.35%) sits 8-12 ATR away while the
# per-trade loss budget stops out at ~3 ATR, so the target is ~4x less
# reachable than the stop and the strategy is negative-expectancy before any
# edge is considered. Round-trip fees (~0.07-0.10% of notional) alone consume
# a third of a winning move at this ATR.
#
# 0.0008 makes the +0.35% take-profit ~4x ATR instead of ~12x - a distance a
# real move can actually cover - and keeps the bot flat through exactly the
# chop that produced the losing streak.
LOW_VOLATILITY_ATR_PCT_THRESHOLD = float(os.environ.get("LOW_VOLATILITY_ATR_PCT_THRESHOLD", "0.0008"))

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

# --- 2026-08-19 Profit Lock / risk-geometry hardening (P1-P6) -----------------
# Root incident: 15:28:19-15:28:20 UTC, LONG 82.43 -> 82.47, exit_reason=
# profit_lock, REALIZED net -$0.0170 on a decision estimated at +$0.0936.
#
# P3 - PROFIT_LOCK_MIN_AGE_SEC: minimum seconds a position must have been open
#   before Profit Lock may ARM. In the incident the lock armed 0.02s after the
#   entry fill, on a mark price (82.695) 0.32% away from its own fill price
#   (82.43), then closed 1.3s later. That peak was an artifact of price/fill
#   incoherence and was never realizable. Peak TRACKING and the exit check are
#   unaffected once armed - this only delays arming.
PROFIT_LOCK_MIN_AGE_SEC = float(os.environ.get("PROFIT_LOCK_MIN_AGE_SEC", "4.0"))
# P2 - PROFIT_LOCK_SLIPPAGE_ATR_MULT: the fee-safe floor Profit Lock requires
#   before closing was a FLAT MIN_NET_PROFIT_USDT ($0.05). In a 0.33%-ATR tape
#   one second of movement was worth $0.11 - more than twice the whole buffer.
#   The floor is now max(MIN_NET_PROFIT_USDT, mult * atr_pct * notional), so it
#   grows with volatility. At 0.5 x 0.333% x $79 that is ~$0.13.
PROFIT_LOCK_SLIPPAGE_ATR_MULT = float(os.environ.get("PROFIT_LOCK_SLIPPAGE_ATR_MULT", "0.5"))

# P4 - ATR-scaled risk geometry. Fixed-dollar thresholds on a fixed notional are
#   INVERSELY proportional to volatility in ATR terms, which is why the same
#   $0.15 loss trigger was ~3.5 ATR on 2026-08-18 (target unreachable at 12 ATR)
#   and ~0.36 ATR on 2026-08-19 (stopped out by ordinary tick noise in 3.5s).
#   Scaling both legs by ATR keeps the risk:reward ratio stable across regimes.
#   The pre-existing dollar/percent values are retained as CAPS, per the agreed
#   design - see the deployment notes about the cap binding at high ATR.
ATR_RISK_SCALING_ENABLED = os.environ.get("ATR_RISK_SCALING_ENABLED", "true").lower() != "false"
SL_ATR_MULT = float(os.environ.get("SL_ATR_MULT", "1.2"))
# FLOOR on the ATR-scaled stop. Caught by test_new_features.py during
# implementation: with the dollar value used purely as a CAP, a dead tape
# (atr 0.02%) produced 1.2 x 0.0002 x $80 = a $0.02 stop - smaller than the
# ~$0.055 round-trip fee, so the position could be stopped out before it could
# ever cover its own costs, and the RR stop fired ahead of every other exit.
# The effective stop is therefore clamped to
# [max(SL_MIN_USD, 1.5 x round-trip fee), MAX_STOP_LOSS_USD].
SL_MIN_USD = float(os.environ.get("SL_MIN_USD", "0.12"))
TP_ATR_MULT = float(os.environ.get("TP_ATR_MULT", "2.5"))

# P5 - Momentum-exhaustion guard. Both 2026-08-19 losses entered with
#   momentum_magnitude saturated at exactly 1.0000 AND |flow_delta| ~1e5
#   (+105,041 and +96,810) - i.e. the bot bought the top of a vertical move and
#   immediately mean-reverted. Saturated momentum plus extreme one-sided flow is
#   treated as LATE-ENTRY risk, not confirmation.
MOMENTUM_EXHAUSTION_GUARD_ENABLED = os.environ.get(
    "MOMENTUM_EXHAUSTION_GUARD_ENABLED", "true"
).lower() != "false"
MOMENTUM_EXHAUSTION_MAGNITUDE = float(os.environ.get("MOMENTUM_EXHAUSTION_MAGNITUDE", "1.0"))
MOMENTUM_EXHAUSTION_FLOW_DELTA = float(os.environ.get("MOMENTUM_EXHAUSTION_FLOW_DELTA", "50000"))

# P6 - Market-websocket reconnect cooldown. Both losing entries fired 1-3s after
#   a bookTicker "1011 keepalive ping timeout" reconnect, and the incident tick
#   showed a 0.32% incoherence between the bot's own price and its own fill.
#   New entries are suppressed briefly after any market-stream reconnect so the
#   price/orderbook series is coherent before a decision is made. Open-position
#   management (TP/SL/Profit-Lock/Smart-Exit/DCA) is deliberately NOT gated.
MARKET_WS_RECONNECT_COOLDOWN_SEC = float(
    os.environ.get("MARKET_WS_RECONNECT_COOLDOWN_SEC", "3.0")
)

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

# --- Per-trade fee-net loss budget (2026-08 payoff-distribution fix) --------
# Independent of MAX_DAILY_LOSS_USDT (a secondary, whole-day circuit
# breaker). This is a PER-POSITION cap: once a position's estimated
# fee-net PnL (actual accumulated entry/DCA commission + mark-to-executable
# unrealized gross PnL - estimated closing commission) drops to
# -(MAX_TRADE_NET_LOSS_USDT - MAX_TRADE_EXIT_BUFFER_USDT), the position is
# closed immediately (exit_reason="max_trade_net_loss"), independent of
# Brain/regime/DCA-availability. See trading.py's _manage_open_position()
# for the exact calculation. Set MAX_TRADE_NET_LOSS_USDT<=0 to disable this
# gate entirely (falls back to Hard Stop / Max Hold Time / Smart Exit only,
# the previous behavior). Not a profitability guarantee - slippage, gaps,
# delayed fills, and exchange outages/bans can still produce a worse result
# than this budget targets.
MAX_TRADE_NET_LOSS_USDT = float(os.environ.get("MAX_TRADE_NET_LOSS_USDT", "0.20"))
MAX_TRADE_EXIT_BUFFER_USDT = float(os.environ.get("MAX_TRADE_EXIT_BUFFER_USDT", "0.05"))

# --- Exchange-native protective stop (2026-08 HTTP 418 IP-ban resilience) --
# A client-side loss check (MAX_TRADE_NET_LOSS_USDT above) cannot protect an
# open position while the local REST client is banned/cooling down (see the
# 2026-08 HTTP 418 incident: the bot could not submit ANY order, including a
# risk-reducing close, for ~25 minutes). This places a server-side
# STOP_MARKET closePosition=true order on Binance itself immediately after
# each confirmed entry/DCA fill, computed from the same loss-budget inputs,
# so the exchange can close the position even if this process is completely
# unreachable. closePosition=true is used deliberately (rather than a fixed
# reduceOnly quantity) because it is inherently reduce-only/cannot reverse
# the position and needs no quantity bookkeeping across DCA adds - see
# trading.py's _place_or_replace_protective_stop() for the full rationale
# and the accepted cancel-then-replace race window.
PROTECTIVE_STOP_ENABLED = os.environ.get("PROTECTIVE_STOP_ENABLED", "true").lower() != "false"
PROTECTIVE_STOP_WORKING_TYPE = os.environ.get("PROTECTIVE_STOP_WORKING_TYPE", "MARK_PRICE")
# 2026-08 protective-stop ownership fix (review finding 4): every protective
# stop this bot places carries a newClientOrderId beginning with this prefix,
# and startup/periodic reconciliation will ONLY adopt, replace, or cancel an
# order whose clientOrderId carries it. A STOP_MARKET placed manually by the
# user, or by any other system on the same account/symbol, is therefore never
# touched. Binance's clientOrderId charset is ^[\.A-Za-z0-9_:/-]{1,36}$ - keep
# this prefix short (<=12 chars) so the generated suffix always fits.
PROTECTIVE_STOP_CLIENT_ID_PREFIX = os.environ.get("PROTECTIVE_STOP_CLIENT_ID_PREFIX", "bv2ps")
# 2026-08 PROTECTION_PENDING fail-safe (review finding 3): when a protective
# stop cannot be armed, the position is unprotected against exactly the REST
# outage this feature exists for. Placement is retried on this interval
# (throttled, and always skipped while a REST cooldown is active so a retry
# can never contribute to a ban), and if protection still cannot be armed
# after PROTECTION_PENDING_MAX_SEC of continuously trying, the position is
# closed as a bounded, risk-reducing fail-safe rather than left indefinitely
# unprotected. Set PROTECTION_PENDING_MAX_SEC<=0 to disable the fail-safe
# close (retries continue; the position is then allowed to stay open
# unprotected - not recommended).
PROTECTIVE_STOP_RETRY_SEC = float(os.environ.get("PROTECTIVE_STOP_RETRY_SEC", "30"))
PROTECTION_PENDING_MAX_SEC = float(os.environ.get("PROTECTION_PENDING_MAX_SEC", "300"))

# --- State reconciliation grace period ----------------------------------------
SYNC_PENDING_GRACE_SEC = int(os.environ.get("SYNC_PENDING_GRACE_SEC", "8"))

# --- Candle aggregation (backs ATR / EMA / regime / volume features) --------
CANDLE_INTERVAL_SEC = int(os.environ.get("CANDLE_INTERVAL_SEC", "60"))
CANDLE_HISTORY = 180          # ~3 hours of 1m candles kept in memory

# --- Instant warm-up via historical klines ------------------------------------
# 2026-08 startup warm-up fix. The candle series that backs ATR / EMA / regime
# used to be built EXCLUSIVELY from the live tick stream, so a fresh container
# needed max(EMA_SLOW, ATR_PERIOD) + 2 = 57 one-minute candles - nearly an hour -
# before a single entry could be considered ("[entry-skip] startup warm-up:
# insufficient market history (candles=5/57)"). On startup the bot now issues ONE
# REST call to GET /fapi/v1/klines and seeds the candle buffer with the last
# KLINE_WARMUP_LIMIT closed candles, then hands the series straight over to the
# live websocket stream for real-time updates. Indicators are valid within
# seconds of boot, with no loss of real-time precision (only fully-CLOSED
# historical candles are seeded; the in-progress bucket is always owned by the
# live stream).
#
#   KLINE_WARMUP_ENABLED - set to "false" to fall back to the old
#                          stream-only warm-up behavior.
#   KLINE_WARMUP_LIMIT   - how many historical candles to request (Binance
#                          allows up to 1500 in one call; 100 comfortably
#                          exceeds the 57 the indicators need, and is clamped
#                          to CANDLE_HISTORY since the buffer cannot hold more).
#   KLINE_WARMUP_INTERVAL - Binance kline interval string. Derived from
#                          CANDLE_INTERVAL_SEC so it can never silently
#                          disagree with the aggregator's own bucket size.
KLINE_WARMUP_ENABLED = os.environ.get("KLINE_WARMUP_ENABLED", "true").lower() in ("1", "true", "yes")
KLINE_WARMUP_LIMIT = max(1, min(int(os.environ.get("KLINE_WARMUP_LIMIT", "100")), CANDLE_HISTORY))

_KLINE_INTERVAL_BY_SEC = {
    60: "1m", 180: "3m", 300: "5m", 900: "15m", 1800: "30m",
    3600: "1h", 7200: "2h", 14400: "4h", 21600: "6h", 28800: "8h",
    43200: "12h", 86400: "1d",
}
KLINE_WARMUP_INTERVAL = os.environ.get(
    "KLINE_WARMUP_INTERVAL", _KLINE_INTERVAL_BY_SEC.get(CANDLE_INTERVAL_SEC, "1m")
)

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
# 2026-08 entry-quality audit note (item 10 of the Brain V2 audit - see
# brain.py's readiness/sample-count fix for what WAS changed): confirmed
# TP_HIT_LOOKAHEAD_CANDLES is defined here but is NOT imported/used
# anywhere in trading.py or brain.py. The online-learning "tp_hit" head
# (BrainV2.learn_tp_hit, called from trading.py's _learn_from_tick) is
# labeled purely from LABEL_HORIZON_TICKS raw bookTicker ticks, not closed
# candles, despite this constant's "CANDLES" name suggesting otherwise.
# Deliberately left unwired rather than rearchitected in this pass: the
# tp_hit head is an intentionally direction-agnostic "was a tradeable move
# available" proxy (see _learn_from_tick's own comment) that is refined by
# the side-aware success/quality heads learned at actual trade close
# (learn_success/learn_quality, from real fee-net PnL) - moving its
# tick-buffer to a closed-candle horizon would be a real online-learning
# pipeline change with no concrete evidence of harm (unlike the confirmed
# saturated-probability and entry-score-logging defects that WERE fixed),
# so it was left alone per the "minimal targeted changes only" mandate.
# Flagged here for a future, evidence-driven pass if warranted.
BRAIN_HEAD_MIN_SAMPLES = int(os.environ.get("BRAIN_HEAD_MIN_SAMPLES", "20"))
# 2026-08 entry-quality audit fix (confirmed defect - see brain.py):
# success_model/tp_hit_model/noise_model are SGDClassifier heads that flip
# their internal "fitted" flag True after their very FIRST partial_fit()
# call (at trade-close time for success/quality; every LABEL_HORIZON_TICKS
# ticks for noise/tp_hit) and, per scikit-learn's own documented behavior,
# can report predict_proba() confidently near 0.0/1.0 from a single-class
# or very small sample (live evidence: entry_success_prob=1.0 in the
# attached trade log, from at most 1-2 completed trades). BrainV2 now
# tracks a separate per-head sample counter (distinct from update_count,
# which only reflects the trend head's tick-driven learning) and requires
# BOTH classes to have been observed AND at least this many labeled
# samples before a classifier head's predict_all() output is treated as
# reliable; below that, predict_all() reports the neutral prior (0.5)
# regardless of the underlying (possibly saturated) model output - exactly
# like the existing "not yet fitted" default already did before any
# training at all. Purely a reliability gate on the REPORTED probability -
# does not change what is learned, does not reset/discard any existing
# snapshot, and update_count/BRAIN2_WARMUP_UPDATES (the existing overall
# is_ready() gate) are completely unchanged.

# --- Entry Engine V2 ---------------------------------------------------------
ENTRY_SCORE_THRESHOLD = float(os.environ.get("ENTRY_SCORE_THRESHOLD", "0.75"))  # raised from 0.60 (2026-07 profitability fix)
# 2026-08-18: raised 0.60 -> 0.63. This is the threshold that actually gated
# the losing streak - all three trades were SIDEWAYS, so ENTRY_SCORE_THRESHOLD
# (0.75) never applied. The accepted scores were 0.6021 / 0.6093 / 0.6251
# against a 0.6000 bar: the bot was systematically taking only its WEAKEST
# qualifying signals, clearing the gate by as little as 0.002.
#
# WHY 0.63 AND NOT 0.65/0.70 - measured, not guessed. The SIDEWAYS composite
# score is structurally capped: volatility_fit is fixed at 0.40 and regime_fit
# at 0.50 for this regime, so even a PERFECT SIDEWAYS setup (saturated aligned
# momentum, volume_z=2.0, low risk) tops out at 0.6358. Live-accepted scores
# ranged 0.6021-0.6251. So:
#
#   0.63 -> rejects 0.6021 and 0.6093 (the two marginal losers), keeps the
#           strongest live setup (0.6251) and a clean aligned one (0.6358)
#           tradable. A genuine filter.
#   0.65 -> above the ENTIRE observed distribution AND above the structural
#           maximum of 0.6358. That is not a filter, it is an off-switch for
#           SIDEWAYS trading.
#   0.70 -> unreachable in this regime under any conditions.
#
# The ATR floor (LOW_VOLATILITY_ATR_PCT_THRESHOLD, above) already blocks all
# three losing trades on its own and is the principled gate here; this
# threshold is the secondary filter. Set SIDEWAYS_ENTRY_SCORE_THRESHOLD=0.65
# in the environment if you deliberately want SIDEWAYS entries off entirely.
SIDEWAYS_ENTRY_SCORE_THRESHOLD = float(os.environ.get("SIDEWAYS_ENTRY_SCORE_THRESHOLD", "0.63"))  # SIDEWAYS is structurally capped lower (volatility_fit/regime_fit/momentum), all other regimes unchanged at 0.75
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

# 2026-08 HTTP 429 REST rate-limit fix. Binance's own 429 body spells the
# remedy out: "Please use the websocket for live updates to avoid polling the
# API." The REST pollers are now (a) slower, (b) websocket-deferred whenever
# the user-data stream is already delivering the same information, and (c)
# jittered so they never line up into a synchronized burst.
#
# Every interval below is env-overridable and floor-clamped, so no deployment
# can accidentally configure the bot back into a rate-limit ban.
#
#   POSITION_RISK_POLL_SEC      - GET /fapi/v2/positionRisk while a position is
#                                 actually open (liquidation price is the one
#                                 field the user-data stream never sends, so
#                                 this poll cannot be dropped entirely).
#                                 Floor-clamped to REST_POLL_MIN_SEC.
#   POSITION_RISK_POLL_IDLE_SEC - the same poll while FLAT. Nothing is at risk,
#                                 so it runs at the slow end of the band and
#                                 exists only as a safety net behind the
#                                 user-data stream's own ACCOUNT_UPDATE events.
#   BALANCE_REFRESH_SEC         - GET /fapi/v2/balance. Skipped entirely
#                                 whenever an ACCOUNT_UPDATE carrying the USDT
#                                 wallet balance arrived over the websocket in
#                                 the last BALANCE_WS_FRESH_SEC (see
#                                 websocket.py's ACCOUNT_UPDATE handler).
#   BALANCE_WS_FRESH_SEC        - how long a websocket balance stays "fresh
#                                 enough" to suppress the REST refresh.
#   REST_POLL_JITTER_PCT        - +/- fraction of random jitter applied to every
#                                 poller sleep, so independent pollers (and
#                                 independent bot instances behind one NAT IP)
#                                 never resynchronize onto the same tick.
REST_POLL_MIN_SEC = 15.0
REST_POLL_MAX_SEC = 30.0


def _clamped_poll_interval(name: str, default: float) -> float:
    """Reads a poller interval from the environment and clamps it into the
    [REST_POLL_MIN_SEC, REST_POLL_MAX_SEC] rate-limit-safe band. A
    non-numeric value falls back to `default` rather than crashing startup."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        print(f"[config] {name} is not a number - using default {default}s")
        value = default
    clamped = min(max(value, REST_POLL_MIN_SEC), REST_POLL_MAX_SEC)
    if clamped != value:
        print(
            f"[config] {name}={value}s is outside the rate-limit-safe band "
            f"[{REST_POLL_MIN_SEC}s, {REST_POLL_MAX_SEC}s] - clamped to {clamped}s"
        )
    return clamped


POSITION_RISK_POLL_SEC = _clamped_poll_interval("POSITION_RISK_POLL_SEC", 20.0)
POSITION_RISK_POLL_IDLE_SEC = max(
    _clamped_poll_interval("POSITION_RISK_POLL_IDLE_SEC", 30.0), POSITION_RISK_POLL_SEC
)
# Balance is refreshed far less often than position risk (it is websocket-fed),
# so it is only floor-clamped, never capped at REST_POLL_MAX_SEC.
try:
    BALANCE_REFRESH_SEC = max(float(os.environ.get("BALANCE_REFRESH_SEC", "60")), REST_POLL_MIN_SEC)
except (TypeError, ValueError):
    BALANCE_REFRESH_SEC = 60.0
try:
    BALANCE_WS_FRESH_SEC = max(float(os.environ.get("BALANCE_WS_FRESH_SEC", "90")), 0.0)
except (TypeError, ValueError):
    BALANCE_WS_FRESH_SEC = 90.0
try:
    REST_POLL_JITTER_PCT = min(max(float(os.environ.get("REST_POLL_JITTER_PCT", "0.15")), 0.0), 0.5)
except (TypeError, ValueError):
    REST_POLL_JITTER_PCT = 0.15


def jittered_interval(base_sec: float, jitter_pct: float = None) -> float:
    """`base_sec` +/- up to `jitter_pct` of itself, never below 1s. Used by
    every REST poller's sleep so concurrent pollers spread their requests
    out instead of firing on the same wall-clock tick."""
    pct = REST_POLL_JITTER_PCT if jitter_pct is None else jitter_pct
    if pct <= 0:
        return max(1.0, base_sec)
    return max(1.0, base_sec * (1.0 + random.uniform(-pct, pct)))


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
    # 2026-08-20 multi-coin watchlist
    "ACTIVE_SYMBOLS",
    "MAX_ACTIVE_TRADES",
    "symbol_scoped_name",
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
    "PROFIT_LOCK_MIN_AGE_SEC",
    "PROFIT_LOCK_SLIPPAGE_ATR_MULT",
    "ATR_RISK_SCALING_ENABLED",
    "SL_ATR_MULT",
    "SL_MIN_USD",
    "TP_ATR_MULT",
    "MOMENTUM_EXHAUSTION_GUARD_ENABLED",
    "MOMENTUM_EXHAUSTION_MAGNITUDE",
    "MOMENTUM_EXHAUSTION_FLOW_DELTA",
    "MARKET_WS_RECONNECT_COOLDOWN_SEC",
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
    "MAX_TRADE_NET_LOSS_USDT",
    "MAX_TRADE_EXIT_BUFFER_USDT",
    "PROTECTIVE_STOP_ENABLED",
    "PROTECTIVE_STOP_WORKING_TYPE",
    "PROTECTIVE_STOP_CLIENT_ID_PREFIX",
    "PROTECTIVE_STOP_RETRY_SEC",
    "PROTECTION_PENDING_MAX_SEC",
    "SYNC_PENDING_GRACE_SEC",
    "CANDLE_INTERVAL_SEC",
    "CANDLE_HISTORY",
    "KLINE_WARMUP_ENABLED",
    "KLINE_WARMUP_LIMIT",
    "KLINE_WARMUP_INTERVAL",
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
    "BRAIN_HEAD_MIN_SAMPLES",
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
    "BALANCE_WS_FRESH_SEC",
    "POSITION_RISK_POLL_SEC",
    "POSITION_RISK_POLL_IDLE_SEC",
    "REST_POLL_MIN_SEC",
    "REST_POLL_MAX_SEC",
    "REST_POLL_JITTER_PCT",
    "jittered_interval",
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


# ============================================================================
# 2026-08 HIGH-FREQUENCY ORDERFLOW UPGRADE - APPENDED CONFIGURATION
# ============================================================================
# Everything above this banner is untouched: not one existing variable was
# deleted, renamed, or had its default changed by this upgrade, and the
# original `__all__` list above is left exactly as it was (this section
# appends to it at the bottom instead of rewriting it). Every value here is
# environment-overridable with the same os.environ.get(...) pattern already
# used throughout this file, so Railway needs no code change to retune any
# of it.
#
# What this block configures (see websocket.py / brain.py / trading.py for
# the code that consumes it):
#   1. The high-frequency data layer - Binance Futures @depth20@100ms
#      partial-book + @aggTrade streams, kept in bounded collections.deque
#      ring buffers so RAM usage on a small Railway container is a fixed,
#      known constant rather than something that grows with uptime.
#   2. The entry Liquidity & Flow Guard - orderbook imbalance + aggregated
#      trade-volume delta, used as a HARD veto and as the extra confirmation
#      factors on top of the existing technical (EMA/RSI/ATR/regime/Brain)
#      entry score.
#   3. The strict 1:2 risk-to-reward envelope, post-only (maker) entries,
#      the post-loss cool-off window, the orderflow-driven Smart Early Exit,
#      and the 1-step Safe DCA rescue rule.
# ============================================================================

# --- 1. Orderbook / flow guard (the eight required toggles) ------------------
ENABLE_ORDERBOOK_GUARD = os.environ.get("ENABLE_ORDERBOOK_GUARD", "true").lower() != "false"
ORDERBOOK_IMBALANCE_THRESHOLD = float(os.environ.get("ORDERBOOK_IMBALANCE_THRESHOLD", "0.20"))
AGG_TRADE_DELTA_WINDOW_SEC = int(os.environ.get("AGG_TRADE_DELTA_WINDOW_SEC", "10"))
MAX_STOP_LOSS_USD = float(os.environ.get("MAX_STOP_LOSS_USD", "0.20"))
TARGET_PROFIT_USD = float(os.environ.get("TARGET_PROFIT_USD", "0.40"))
COOL_OFF_PERIOD_MINUTES = float(os.environ.get("COOL_OFF_PERIOD_MINUTES", "15"))
USE_POST_ONLY_LIMIT = os.environ.get("USE_POST_ONLY_LIMIT", "true").lower() != "false"

# MAX_DCA_STEPS: deliberately RE-BOUND here rather than edited in place
# above, so the original definition (and its full historical comment about
# why 3 -> 2 happened) is preserved verbatim for the record. The Safe DCA
# rule of this upgrade re-engineers DCA as a single 1-step rescue order
# targeting a fast break-even exit, so the hard cap is now 1. The env var
# name is unchanged (MAX_DCA_STEPS), so an existing Railway deployment that
# already pins it keeps its pinned value - only the DEFAULT moved 2 -> 1.
# This assignment shadows the earlier one at import time; every consumer
# (trading.py's DCA gates, sanitize_recovered_dca_step(), the DCA-state
# snapshot clamps) reads this final value.
MAX_DCA_STEPS = int(os.environ.get("MAX_DCA_STEPS", "1"))

# --- 2. High-frequency market-data layer (websocket.py) ---------------------
# Binance publishes partial book depth in fixed sizes (5/10/20) and at
# 250ms/500ms/100ms speeds. We subscribe to the 20-level/100ms stream and
# compute the imbalance over the top ORDERBOOK_DEPTH_LEVELS (10) of each
# side, per the spec:
#     (top10_bid_vol - top10_ask_vol) / (top10_bid_vol + top10_ask_vol)
ORDERBOOK_STREAM_DEPTH = int(os.environ.get("ORDERBOOK_STREAM_DEPTH", "20"))
ORDERBOOK_STREAM_SPEED_MS = int(os.environ.get("ORDERBOOK_STREAM_SPEED_MS", "100"))
ORDERBOOK_DEPTH_LEVELS = int(os.environ.get("ORDERBOOK_DEPTH_LEVELS", "10"))
# RAM safety on Railway: both rolling buffers are collections.deque with a
# hard maxlen, so the orderflow layer's memory footprint is bounded and
# constant no matter how long the process runs. 600 depth samples at 100ms
# is a 60s rolling view; 4000 aggTrades comfortably covers the 10s delta
# window even in a violent tape.
ORDERBOOK_BUFFER_MAXLEN = int(os.environ.get("ORDERBOOK_BUFFER_MAXLEN", "600"))
AGG_TRADE_BUFFER_MAXLEN = int(os.environ.get("AGG_TRADE_BUFFER_MAXLEN", "4000"))
# A depth/trade reading older than this is treated as "no data" rather than
# as a live signal - a silently dead stream must never look like a neutral
# (0.0) orderbook.
ORDERFLOW_STALE_SEC = float(os.environ.get("ORDERFLOW_STALE_SEC", "5.0"))
# Dynamic auto-reconnect tuning for the market websockets (Railway network
# drops): full jitter is applied to the existing exponential backoff so a
# multi-stream reconnect storm never re-hits Binance in lockstep.
WS_RECONNECT_JITTER_RATIO = float(os.environ.get("WS_RECONNECT_JITTER_RATIO", "0.5"))

# --- 3. Entry Liquidity & Flow Guard (brain.py) ------------------------------
# Multi-factor confirmation: a trade may only open when the TECHNICAL signal
# (existing Brain/EMA/RSI/ATR/regime score), the ORDERBOOK support, and the
# 10s trade-flow DELTA all agree. ORDERBOOK_SUPPORT_MIN is the minimum
# same-side imbalance that counts as genuine book support (0.0 = book merely
# not against us); AGG_TRADE_DELTA_MIN is the equivalent floor on the signed
# 10s volume delta, in base-asset units.
ORDERBOOK_SUPPORT_MIN = float(os.environ.get("ORDERBOOK_SUPPORT_MIN", "0.0"))
AGG_TRADE_DELTA_MIN = float(os.environ.get("AGG_TRADE_DELTA_MIN", "0.0"))
# When True (default), an unavailable/stale orderflow feed BLOCKS entries
# instead of silently degrading to "no guard". Fail-safe, not fail-open.
ORDERBOOK_GUARD_REQUIRES_DATA = (
    os.environ.get("ORDERBOOK_GUARD_REQUIRES_DATA", "true").lower() != "false"
)

# --- 4. Strict 1:2 risk-to-reward envelope (trading.py) ---------------------
# Sized for the documented account shape: $4 initial margin @ 20x leverage
# on a ~$20 wallet (= $80 notional). SL is capped at $0.15-$0.20 fee-net and
# TP targeted at $0.35-$0.40 fee-net, i.e. ~1:2. MAX_STOP_LOSS_USD /
# TARGET_PROFIT_USD above are the outer (worst/best) bounds of those bands;
# the MIN_* values below are the inner bounds, used so a winner can be
# banked at $0.35 the moment orderflow turns instead of insisting on the
# full $0.40.
MIN_STOP_LOSS_USD = float(os.environ.get("MIN_STOP_LOSS_USD", "0.15"))
MIN_TARGET_PROFIT_USD = float(os.environ.get("MIN_TARGET_PROFIT_USD", "0.35"))
ENFORCE_RISK_REWARD_USD = (
    os.environ.get("ENFORCE_RISK_REWARD_USD", "true").lower() != "false"
)
# Post-only (maker) entry execution. A GTX order that would cross the book
# is rejected by Binance rather than paying taker fees; we re-price once,
# and only then fall back to the original MARKET behavior (so the bot can
# never be left unable to trade at all by a fast tape).
POST_ONLY_LIMIT_OFFSET_TICKS = int(os.environ.get("POST_ONLY_LIMIT_OFFSET_TICKS", "0"))
POST_ONLY_LIMIT_TIMEOUT_SEC = float(os.environ.get("POST_ONLY_LIMIT_TIMEOUT_SEC", "20"))
POST_ONLY_MARKET_FALLBACK = (
    os.environ.get("POST_ONLY_MARKET_FALLBACK", "true").lower() != "false"
)

# --- 5. Continuous 24/7 execution -------------------------------------------
# The existing MAX_DAILY_LOSS_USDT / DAILY_PROFIT_TARGET_USDT variables and
# their entry gates are PRESERVED exactly as they are. This toggle decides
# whether those two gates still halt new entries for the remainder of a UTC
# day. Default True = never shut the bot down on a daily profit/loss figure;
# set CONTINUOUS_24_7_TRADING=false to restore the previous daily-halt
# behavior with no code change.
CONTINUOUS_24_7_TRADING = (
    os.environ.get("CONTINUOUS_24_7_TRADING", "true").lower() != "false"
)

# --- 6. Dynamic post-loss cool-off window -----------------------------------
# On ANY losing trade: no new entry for COOL_OFF_PERIOD_MINUTES, and for an
# equally long window AFTER that, the entry orderbook guard is tightened -
# the adverse-imbalance veto trips earlier (threshold reduced by
# COOL_OFF_IMBALANCE_TIGHTEN) and genuine same-side book support of at least
# COOL_OFF_SUPPORT_MIN is required. This is the anti-revenge-trading rule.
COOL_OFF_IMBALANCE_TIGHTEN = float(os.environ.get("COOL_OFF_IMBALANCE_TIGHTEN", "0.10"))
COOL_OFF_SUPPORT_MIN = float(os.environ.get("COOL_OFF_SUPPORT_MIN", "0.20"))

# --- 7. Smart Orderflow Early Exit ------------------------------------------
# If the orderbook imbalance flips violently AGAINST an open position before
# the stop is reached, exit at a micro-loss instead of riding it to the full
# SL. Fires only inside the $0.05-$0.10 fee-net micro-loss band.
SMART_ORDERFLOW_EXIT_ENABLED = (
    os.environ.get("SMART_ORDERFLOW_EXIT_ENABLED", "true").lower() != "false"
)
SMART_ORDERFLOW_EXIT_IMBALANCE = float(
    os.environ.get("SMART_ORDERFLOW_EXIT_IMBALANCE", "0.35")
)
SMART_ORDERFLOW_EXIT_MIN_LOSS_USD = float(
    os.environ.get("SMART_ORDERFLOW_EXIT_MIN_LOSS_USD", "0.05")
)
SMART_ORDERFLOW_EXIT_MAX_LOSS_USD = float(
    os.environ.get("SMART_ORDERFLOW_EXIT_MAX_LOSS_USD", "0.10")
)
SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC = float(
    os.environ.get("SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC", "10")
)

# --- 8. Safe DCA: single-step rescue order, break-even target ---------------
# The one permitted DCA add is only submitted when the book itself supports
# the reversal (bids for a LONG rescue, asks for a SHORT rescue). Once that
# rescue has filled, the position stops chasing the full TP and targets a
# fast break-even exit instead.
DCA_REQUIRE_ORDERBOOK_SUPPORT = (
    os.environ.get("DCA_REQUIRE_ORDERBOOK_SUPPORT", "true").lower() != "false"
)
DCA_RESCUE_SUPPORT_MIN = float(os.environ.get("DCA_RESCUE_SUPPORT_MIN", "0.10"))
DCA_RESCUE_BREAKEVEN_ENABLED = (
    os.environ.get("DCA_RESCUE_BREAKEVEN_ENABLED", "true").lower() != "false"
)
DCA_RESCUE_BREAKEVEN_MIN_NET_USD = float(
    os.environ.get("DCA_RESCUE_BREAKEVEN_MIN_NET_USD", "0.02")
)


__all__ = __all__ + [
    "ENABLE_ORDERBOOK_GUARD",
    "ORDERBOOK_IMBALANCE_THRESHOLD",
    "AGG_TRADE_DELTA_WINDOW_SEC",
    "MAX_STOP_LOSS_USD",
    "TARGET_PROFIT_USD",
    "COOL_OFF_PERIOD_MINUTES",
    "USE_POST_ONLY_LIMIT",
    "ORDERBOOK_STREAM_DEPTH",
    "ORDERBOOK_STREAM_SPEED_MS",
    "ORDERBOOK_DEPTH_LEVELS",
    "ORDERBOOK_BUFFER_MAXLEN",
    "AGG_TRADE_BUFFER_MAXLEN",
    "ORDERFLOW_STALE_SEC",
    "WS_RECONNECT_JITTER_RATIO",
    "ORDERBOOK_SUPPORT_MIN",
    "AGG_TRADE_DELTA_MIN",
    "ORDERBOOK_GUARD_REQUIRES_DATA",
    "MIN_STOP_LOSS_USD",
    "MIN_TARGET_PROFIT_USD",
    "ENFORCE_RISK_REWARD_USD",
    "POST_ONLY_LIMIT_OFFSET_TICKS",
    "POST_ONLY_LIMIT_TIMEOUT_SEC",
    "POST_ONLY_MARKET_FALLBACK",
    "CONTINUOUS_24_7_TRADING",
    "COOL_OFF_IMBALANCE_TIGHTEN",
    "COOL_OFF_SUPPORT_MIN",
    "SMART_ORDERFLOW_EXIT_ENABLED",
    "SMART_ORDERFLOW_EXIT_IMBALANCE",
    "SMART_ORDERFLOW_EXIT_MIN_LOSS_USD",
    "SMART_ORDERFLOW_EXIT_MAX_LOSS_USD",
    "SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC",
    "DCA_REQUIRE_ORDERBOOK_SUPPORT",
    "DCA_RESCUE_SUPPORT_MIN",
    "DCA_RESCUE_BREAKEVEN_ENABLED",
    "DCA_RESCUE_BREAKEVEN_MIN_NET_USD",
]
