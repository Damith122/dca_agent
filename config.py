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
import uuid

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

# ============================================================================
# ROBUST ENVIRONMENT PARSING (2026-08-21)
#
# Every tunable below is read from the environment. Previously each one did
# its own bare float(os.environ.get(NAME, default)) / int(...) conversion,
# which meant an EMPTY or malformed variable raised ValueError at import
# time - before main() runs, before the banner prints, and therefore with no
# log line explaining what went wrong. Clearing a variable in Railway (the
# natural way to "unset" it, since the dashboard has no delete) is exactly
# the case that hit this: os.environ.get returns "" rather than None, the
# default is skipped, and float("") explodes.
#
# These helpers make that impossible. A variable that is missing, empty,
# whitespace-only, or unparseable falls back to the code default and records
# a warning instead of raising. Nothing here can abort startup.
#
# Warnings are collected rather than printed, because config.py is imported
# before the banner exists; dca2.py prints them via env_parse_warnings()
# once there is somewhere sensible to put them.
# ============================================================================

_ENV_WARNINGS: list = []


def _env_raw(name: str):
    """Return a usable raw string for `name`, or None if it is absent or
    blank. Empty and whitespace-only are treated as ABSENT, not as a value -
    that is the whole point: clearing a variable must mean "use the code
    default", never "crash"."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _env_float(name: str, default: float) -> float:
    # The default is coerced too, so a float-typed knob is ALWAYS a float
    # regardless of whether the environment supplied it. Returning the bare
    # literal would make e.g. an unset 50 an int while a set "50" is a
    # float - a type that varies with configuration is a latent bug.
    default = float(default)
    raw = _env_raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _ENV_WARNINGS.append(
            f"{name}={raw!r} is not a number - using default {default}"
        )
        return default
    # NaN/inf would poison every downstream comparison silently, so they are
    # rejected the same way a malformed string is.
    if value != value or value in (float("inf"), float("-inf")):
        _ENV_WARNINGS.append(
            f"{name}={raw!r} is not finite - using default {default}"
        )
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = _env_raw(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    # Accept "5.0" for an int-typed knob rather than rejecting it outright;
    # a value that is genuinely fractional still falls back to the default.
    # OverflowError is caught alongside ValueError because int(float("inf"))
    # raises it - "inf" must degrade to the default like any other bad value.
    try:
        as_float = float(raw)
        if as_float == as_float and as_float not in (float("inf"), float("-inf")):
            if as_float == int(as_float):
                return int(as_float)
    except (TypeError, ValueError, OverflowError):
        pass
    _ENV_WARNINGS.append(
        f"{name}={raw!r} is not an integer - using default {default}"
    )
    return default


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean tunable. Recognises the usual spellings in both
    directions; anything unrecognised warns and falls back rather than
    silently reading as False (the old `.lower() != "false"` idiom turned a
    typo like "flase" into True without a word)."""
    raw = _env_raw(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("true", "1", "yes", "y", "on"):
        return True
    if lowered in ("false", "0", "no", "n", "off"):
        return False
    _ENV_WARNINGS.append(
        f"{name}={raw!r} is not a boolean - using default {default}"
    )
    return default


def _env_str(name: str, default: str) -> str:
    raw = _env_raw(name)
    return default if raw is None else raw


def env_parse_warnings() -> list:
    """Every environment variable that could not be parsed, as human-readable
    strings. Empty when the environment is clean."""
    return list(_ENV_WARNINGS)


# 2026-08 USE_TESTNET moved up from the "Safety gates" section below
# (verbatim - same env var, same parsing, same default) so RUNTIME_ENV and
# _symbol_scoped_default() can be computed from it. USE_TESTNET's own
# meaning/behavior as a safety gate is completely unchanged - it is simply
# read a few lines earlier than before.
USE_TESTNET = _env_bool("USE_TESTNET", True)
DRY_RUN = _env_bool("DRY_RUN", True)

# 2026-08 environment + symbol state isolation: runtime persistence must
# be isolated by BOTH which Binance environment this process is trading
# on (Testnet vs Live) AND which symbol - switching either one, via
# Railway ENV + redeploy, must never let one environment/symbol
# combination's Brain/DCA-state/trade-sync-cursor/trade-log/stats be
# silently loaded by another. No new env var is introduced - USE_TESTNET
# (already the single source of truth for Testnet vs Live) is reused
# directly.
RUNTIME_ENV = "TESTNET" if USE_TESTNET else "LIVE"
if DRY_RUN:
    # Each paper process is a separate experiment: no cash reset disguised
    # as continuation of an older run, and no simulated brain in live state.
    RUNTIME_ENV = "PAPER_" + RUNTIME_ENV + "_" + uuid.uuid4().hex[:12]


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
    MAX_ACTIVE_TRADES = 1
except (TypeError, ValueError):
    MAX_ACTIVE_TRADES = 1


# ---------------------------------------------------------------------------
# 2026-08-21 tick throttling (CPU saturation / websocket keepalive fix).
#
# Every bookTicker message used to drive a full on_price_tick() decision
# cycle - feature build, regime evaluation, Brain V2 inference, risk scoring.
# bookTicker is event-driven, so a busy book (ETHUSDT) fires it hundreds of
# times a second, and with a four-symbol watchlist that saturated the single
# asyncio event loop: Railway showed ~0.8-1.1 CPU cores steady, which for a
# single-threaded loop is effectively maxed. The starved loop then failed to
# service its own websocket keepalive pongs, and Binance closed the busiest
# socket with 1011 every 60-100s (25 reconnects in 36 minutes on ETHUSDT
# bookTicker, vs 3-4 on the quiet ones - the reconnect count tracked message
# rate almost exactly).
#
# The fix is decimation, not queueing: on_book_ticker() still records EVERY
# message (price and orderbook stay perfectly current - that part is cheap
# arithmetic), while the expensive decision cycle runs at most once per
# interval and simply reads the latest state when it does run. No tick is
# buffered and no price update is lost.
#
# Two intervals, because the two states have genuinely different needs:
#
#   TICK_MIN_INTERVAL_SEC (FLAT, default 250ms) - the only work here is entry
#     scanning, and entry scoring is driven by 1-minute candles. Evaluating a
#     1m-candle-based signal 200x/second buys nothing. This is also where the
#     load actually is: with MAX_ACTIVE_TRADES=1, at least three of four
#     symbols are FLAT at any moment.
#
#   TICK_MIN_INTERVAL_ACTIVE_SEC (position live, default 100ms) - stop-loss,
#     Profit Lock, trailing and DCA all run here, plus Profit Lock's peak
#     sampling. 10Hz is still far finer than the 100-500ms REST round-trip
#     needed to act on any of them, and the exchange-native STOP_MARKET algo
#     order remains the server-side backstop regardless of this loop.
#
# Set either to 0 to disable throttling for that state and restore the
# previous every-message behaviour exactly.
# ---------------------------------------------------------------------------
def _clamped_tick_interval(env_var: str, default: float) -> float:
    """Parse a tick interval, clamped to [0, 1] seconds. 0 disables the
    throttle; anything above 1s is refused because it would start to delay
    risk exits meaningfully rather than just decimating idle scans."""
    value = _env_float(env_var, default)
    if value < 0:
        return default
    return min(value, 1.0)


TICK_MIN_INTERVAL_SEC = _clamped_tick_interval("TICK_MIN_INTERVAL_SEC", 0.25)
TICK_MIN_INTERVAL_ACTIVE_SEC = _clamped_tick_interval("TICK_MIN_INTERVAL_ACTIVE_SEC", 0.10)
# How often each symbol reports its own tick-throttle efficiency, so the
# saturation fix is verifiable from the deploy log rather than inferred.
TICK_THROTTLE_LOG_INTERVAL_SEC = 300.0


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
# DRY_RUN is parsed above so simulated records never use LIVE state paths.
I_UNDERSTAND_THIS_IS_REAL_MONEY = os.environ.get(
    "I_UNDERSTAND_THIS_IS_REAL_MONEY", ""
).lower() == "yes"
LIVE_TRADING_CONFIRMATION = _env_bool("LIVE_TRADING_CONFIRMATION", False)
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
MAX_ALLOWED_LEVERAGE = min(10, max(1, _env_int("MAX_ALLOWED_LEVERAGE", 10)))
LEVERAGE = min(MAX_ALLOWED_LEVERAGE, max(1, _env_int("LEVERAGE", 5)))
MARGIN_TYPE = "CROSSED"

# --- Position sizing (Fixed Amount base, Martingale, now confidence-scaled) -
INITIAL_ENTRY_USDT = _env_float("INITIAL_ENTRY_USDT", 3)
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

# ============================================================================
# 2026-08-31 AGGRESSIVE SIZING (operator-directed, negative expectancy known)
#
# The operator asked for a configuration targeting >= $0.50 net profit per day
# on a ~$16.92 account, having been shown - and having explicitly accepted -
# the measured evidence against it. That evidence, unchanged by this block:
#
#   71 live/simulated round trips: gross +1.3 bps, net -8.7 bps per trade,
#   t = -3.23, p < 0.01. The strategy's GROSS edge is statistically
#   indistinguishable from zero; the net result is the fee line.
#
# Nothing below creates edge. Sizing multiplies whatever per-trade expectancy
# already exists, and that expectancy is negative, so every number here
# enlarges the expected loss and shortens the time to ruin. Monte Carlo over
# this configuration returns ~100% probability of account ruin with a median
# time to ruin of 36-61 days. This block exists so that the reasoning behind
# each number is recoverable later, not because the numbers are defensible on
# expectancy grounds.
#
# WHAT CHANGED, AND THE ARITHMETIC BEHIND EACH
#
#   INITIAL_ENTRY_USDT   5  -> 3     margin per entry
#   LEVERAGE             2  -> 20
#   MAX_ACTIVE_TRADES    1  -> 2
#   => ENTRY_NOTIONAL_USDT $10 -> $60 (6x), portfolio exposure up to $120
#
# $3 is not a round number chosen for taste - it is the largest initial margin
# that still lets BOTH portfolio slots carry their DCA rung on this account:
#
#   initial            margin $3.00   notional  $60.00
#   + DCA #1 (x1.6)    margin $4.80   notional  $96.00
#   full position      margin $7.80   notional $156.00
#   x 2 slots          margin $15.60  notional $312.00
#   balance $16.92  -> free margin $1.32 (8% buffer)
#
# $4 initial (the previous default) needs $20.80 at the same fill state and is
# not fundable. $8 needs $41.60 and is not fundable at any leverage.
#
# THE BUFFER IS THIN AND THE FAILURE MODE MATTERS. Confidence-based sizing
# (confidence_size_multiplier(), bounded by SIZE_MIN_MULT/SIZE_MAX_MULT=1.5)
# can scale an entry to $4.50 margin and its DCA to $7.20, i.e. $11.70 per
# slot, $23.40 for two. That does not fit. Under CROSSED margin the
# consequence is a REJECTED order (Binance -2019), not a liquidation: the
# second slot's DCA simply fails to place and the existing position keeps its
# protective stop. That is survivable and self-limiting, which is why
# SIZE_MAX_MULT is left alone rather than clamped to 1.0 - clamping it would
# trade a recoverable rejection for the permanent loss of confidence scaling.
#
# LIQUIDATION DISTANCE. At the full $312 notional state, maintenance margin is
# roughly $1.56, so liquidation needs equity below that: about a 4.9% adverse
# move. The ATR-scaled stop, capped by MAX_STOP_LOSS_USD (derived: $0.675 =
# 112 bps of notional), fires around 1.1%. The stop is the binding constraint
# by a factor of four - PROVIDED it executes. The protective stop is the only
# thing standing between this configuration and the liquidation price, so a
# stop that fails to place is now an account-level event, not a trade-level
# one.
#
# TAKE PROFIT, AND THE CEILING THE TESTS FOUND. Widening the target is the one
# change here that improves the trade's economics rather than merely scaling
# it: round-trip cost is ~7 bps and is FIXED per round trip, so it is 20% of a
# 35 bps target but only 10% of a 70 bps one. Expected move size grows as the
# square root of holding time while cost does not grow at all, so wider is the
# right direction - but only while the target stays reachable.
#
# The first attempt at this block set TAKE_PROFIT_PCT to 100 bps.
# test_algo_update_and_trade_log_recovery_fix.py rejected it, and correctly:
# it asserts the take-profit must sit within about 5 ATR of the ATR FLOOR
# (0.08%), because beyond that the position cannot travel the distance before
# the loss budget or the max-hold timer ends the trade. 100 bps is 12.5 ATR at
# the floor. In the quiet SIDEWAYS conditions the live logs actually show,
# that target would essentially never be hit and every trade would resolve at
# the stop or on max-hold - which is not a more aggressive strategy, it is a
# worse one wearing an aggressive number.
#
# Probing the invariant directly: 0.004 passes, 0.005 fails. So
# TAKE_PROFIT_PCT = 0.004 (40 bps = 5.0 ATR at the floor) is a HARD CEILING
# imposed by the design, not a preference, and that is where it sits.
#
# The expansion the operator asked for therefore lives in TAKE_PROFIT_MAX_PCT
# (100 -> 200 bps), which is the right place for it: DYNAMIC_TP_ENABLED
# interpolates between the two as tick-return volatility runs from TP_VOL_LOW
# to TP_VOL_HIGH, so the wide target is applied only when the market is
# actually moving far enough to reach it, and the 40 bps base governs the
# quiet conditions where it must stay reachable. Note that the base is the
# value used in quiet markets, which is exactly why it could not simply be
# raised to the number that sounded most aggressive.
#
# WHAT WAS DELIBERATELY NOT CHANGED
#
#   ENTRY_SCORE_THRESHOLD stays at its configured value. Size and frequency
#   multiply: raising both would compound the expected loss rather than add to
#   it, and the entry gate is the last filter still rejecting trades. Lowering
#   it is the single fastest available route to ruin.
#
#   MAX_DCA_STEPS stays at 1 (see the re-bind further down). 2 steps do not
#   fit the margin arithmetic above at two slots.
#
#   The Brain needs no head reset for the take-profit change.
#   TAKE_PROFIT_PCT only SEEDS _label_move_scale in trading.py, which is an
#   EWMA of realised |forward return| decaying at 0.999/tick; within about a
#   thousand ticks the labels ride the market's own move scale and not this
#   constant. The tp_hit head's accumulated updates stay valid.
#
# THE TWO INTENTIONAL ENV OVERRIDES. Every notional-scaled threshold below is
# left to derive itself EXCEPT the two daily circuit breakers, which are set
# explicitly in the deployment environment:
#
#   MAX_DAILY_LOSS_USDT       $2.50   (14.8% of a $16.92 account)
#   DAILY_PROFIT_TARGET_USDT  $1.00   (2x the $0.50/day objective)
#
# These two are overridden on principle, not convenience. Notional is the
# right reference for per-TRADE geometry - a stop governs one position and
# that position's notional is fixed for its lifetime. It is the wrong
# reference for a per-DAY budget, which is properly a fraction of ACCOUNT
# EQUITY and has nothing to do with how large any single entry happens to be.
# Left derived, the daily loss cap lands at $0.75, which is barely more than a
# single full stop-out ($0.675) - entries would halt after one losing trade on
# most days. That directly defeats the operator's standing first priority that
# data collection must never stop. Note the gate is NEW-ENTRY-only: the tick
# feature recorder and the tp_hit/noise heads keep learning through a halt
# either way; it is the success head, which can only be labelled by a closed
# trade, that goes hungry.
#
# TO REVERT: set INITIAL_ENTRY_USDT=5, LEVERAGE=2, MAX_ACTIVE_TRADES=1,
# TAKE_PROFIT_PCT=0.0035, TAKE_PROFIT_MAX_PCT=0.010, DCA_TRIGGER_PCT=0.002.
# Every notional-scaled threshold follows automatically; none of them needs a
# matching edit (see the next block).
# ============================================================================


# ============================================================================
# 2026-08-21 NOTIONAL-RELATIVE RISK SCALING
#
# Every per-trade dollar threshold below (stop ceiling, loss budget, take
# profit, Profit Lock arming, the orderflow micro-loss band, ...) is now
# DERIVED from the entry notional instead of being a hardcoded dollar value.
#
# WHY. PnL scales with notional: the same 0.2% price move is worth $0.08 at a
# $40 entry and $0.16 at $80. A fixed dollar threshold therefore silently
# HALVES in percentage terms every time position size doubles - a stop that
# was 1.1% of notional becomes 0.55%, i.e. twice as tight, without anyone
# deciding that. Scaling INITIAL_ENTRY_USDT from 2 to 4 on 2026-08-21 required
# ten separate manual env-var edits to keep the geometry intact, and getting
# any one of them wrong would have silently changed the strategy. One of them
# (the orderflow micro-loss band) would have broken outright: at $80 notional
# a fresh position's fee-net PnL starts at about -$0.056, which fell straight
# inside the old [-$0.10, -$0.05] exit band, so every position would have been
# born eligible for an immediate exit.
#
# HOW. Each threshold is expressed as a FRACTION OF ENTRY NOTIONAL, chosen so
# that at the current INITIAL_ENTRY_USDT=4 / LEVERAGE=20 ($80 notional) every
# value reproduces exactly what was running before this refactor. Change
# INITIAL_ENTRY_USDT alone and the whole risk geometry follows.
#
# ESCAPE HATCH. Setting the corresponding env var explicitly still wins, and
# now prints a startup warning - so a stale Railway variable left over from
# the manual era is visible rather than silently pinning a threshold while the
# rest of the geometry moves around it.
#
# Note this scales off NOTIONAL, not wallet balance. Balance is the wrong
# reference: it moves with every closed trade and with deposits, so a
# balance-relative stop would drift mid-session and change meaning after every
# win or loss. Notional is fixed for the life of a position, which is exactly
# the horizon these thresholds govern.
# ============================================================================
ENTRY_NOTIONAL_USDT = INITIAL_ENTRY_USDT * LEVERAGE

# Populated by _notional_scaled() below; reported at startup by dca2.py and
# used to warn about leftover explicit overrides.
_NOTIONAL_SCALED_RESOLVED: dict = {}
_NOTIONAL_SCALED_OVERRIDDEN: dict = {}


def _notional_scaled(env_var: str, fraction_of_notional: float, *, floor: float = 0.0) -> float:
    """A per-trade dollar threshold as a fraction of the entry notional.

    An explicitly-set env var always wins (and is recorded so startup can warn
    about it). `floor` is an absolute dollar minimum for thresholds that stop
    being meaningful below a certain size regardless of notional - it is
    deliberately NOT scaled.
    """
    explicit = _env_raw(env_var)
    if explicit is not None:
        try:
            value = float(explicit)
            _NOTIONAL_SCALED_OVERRIDDEN[env_var] = value
            _NOTIONAL_SCALED_RESOLVED[env_var] = value
            return value
        except (TypeError, ValueError):
            pass          # unparseable -> fall through to the derived value
    value = max(floor, fraction_of_notional * ENTRY_NOTIONAL_USDT)
    _NOTIONAL_SCALED_RESOLVED[env_var] = value
    return value


def notional_scaling_report() -> list:
    """(name, value, 'derived'|'OVERRIDDEN') for every scaled threshold, for
    the startup banner. Pure diagnostics."""
    return [
        (name, value, "OVERRIDDEN" if name in _NOTIONAL_SCALED_OVERRIDDEN else "derived")
        for name, value in sorted(_NOTIONAL_SCALED_RESOLVED.items())
    ]


DCA_MULTIPLIER = _env_float("DCA_MULTIPLIER", 1.6)  # reduced from 2.0 (2026-07 profitability fix) - less notional/fee blowup per DCA rung
MAX_DCA_STEPS = min(1, max(0, _env_int("MAX_DCA_STEPS", 1)))        # capped at one rescue step; paper momentum runner uses none

# --- Trade management ---------------------------------------------------------
DCA_TRIGGER_PCT = _env_float("DCA_TRIGGER_PCT", 0.002)    # floor / fallback DCA spacing (also used if ATR unavailable). Unchanged: at a 40 bps base take-profit the rung still fires once inside the TP distance, which is the intended geometry. It was briefly widened to 0.005 alongside a 100 bps target and reverted with it.
TAKE_PROFIT_PCT = _env_float("TAKE_PROFIT_PCT", 0.004)    # 2026-08-31 aggressive config: raised from 0.0035, and NO FURTHER. See the AGGRESSIVE SIZING block above - 0.004 is a hard ceiling imposed by reachability at the ATR floor, not a preference. A wider target improves the fee RATIO rather than just scaling both sides of the ledger, but only while it stays reachable; the expansion the operator asked for lives in TAKE_PROFIT_MAX_PCT, which is volatility-gated and so only applies when the move size actually supports it.
HARD_STOP_PCT = _env_float("HARD_STOP_PCT", 0.02)         # tightened from 0.05 (2026-07 profitability fix) - fixes stop:TP risk/reward skew

# --- Dynamic (volatility-based) Take Profit ----------------------------------
DYNAMIC_TP_ENABLED = _env_bool("DYNAMIC_TP_ENABLED", True)
TAKE_PROFIT_MAX_PCT = _env_float("TAKE_PROFIT_MAX_PCT", 0.020)  # 2026-08-31 aggressive config: raised from 0.010, holding the same 2x expansion band over the base target.
TP_VOL_LOW = _env_float("TP_VOL_LOW", 0.0003)    # tick-return std at/below this -> quiet -> base TP
TP_VOL_HIGH = _env_float("TP_VOL_HIGH", 0.0012)  # tick-return std at/above this -> max TP expansion

# --- Max Hold Time Protection (NEW - scalping bot safety net) ----------------
# This is a scalping bot; positions are meant to resolve in minutes, not
# hours. If the market goes dead/ranging and none of TP/Smart-Exit/DCA/
# Profit-Lock naturally close the trade, this is a backstop so the bot
# never silently holds a position for many hours. See trading.py's
# _manage_open_position() for how PnL/regime/Profit-Lock state are
# consulted before this is allowed to force-close a trade (it does NOT
# blindly close profitable/trending positions - see MAX_HOLD_TIME_HARD_CAP_SEC
# for the unconditional absolute ceiling).
MAX_HOLD_TIME_ENABLED = _env_bool("MAX_HOLD_TIME_ENABLED", True)
MAX_HOLD_TIME_SEC = _env_int("MAX_HOLD_TIME_SEC", 4 * 3600)       # 4h soft cap
MAX_HOLD_TIME_HARD_CAP_SEC = _env_int("MAX_HOLD_TIME_HARD_CAP_SEC", 8 * 3600)  # 8h absolute cap, always closes regardless of PnL/regime/profit-lock
MAX_HOLD_TIME_SMALL_LOSS_PCT = _env_float("MAX_HOLD_TIME_SMALL_LOSS_PCT", 0.0015)
# 2026-08 Max Hold Time V2: a loss smaller (in magnitude) than this at timeout
# is treated as "very small" and closes normally (not worth an emergency
# recovery review). 0.15% mirrors DCA_MIN_DISTANCE_PCT's floor - roughly the
# smallest adverse move the bot's own DCA/exit machinery treats as meaningful.
MAX_HOLD_TIME_RECOVERY_MIN_AGREE = _env_int("MAX_HOLD_TIME_RECOVERY_MIN_AGREE", 2)
# Of the 5 recovery-risk signals evaluated at timeout for a position with a
# genuinely significant loss (trend_against, high_risk, momentum_against,
# extreme_volatility, dca_exhausted - see trading.py's
# _manage_open_position()), how many must agree before the position is
# force-closed as "low probability of recovery" rather than kept open past
# MAX_HOLD_TIME_SEC (still bounded by MAX_HOLD_TIME_HARD_CAP_SEC either way).
MAX_HOLD_TIME_DCA_MULTIPLIER = _env_float("MAX_HOLD_TIME_DCA_MULTIPLIER", 0.5)
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
CLOSE_VERIFY_MAX_RETRIES = _env_int("CLOSE_VERIFY_MAX_RETRIES", 3)
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
LOW_VOLATILITY_FILTER_ENABLED = _env_bool("LOW_VOLATILITY_FILTER_ENABLED", True)
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
LOW_VOLATILITY_ATR_PCT_THRESHOLD = _env_float("LOW_VOLATILITY_ATR_PCT_THRESHOLD", 0.0008)

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
ADAPTIVE_SIZING_ENABLED = _env_bool("ADAPTIVE_SIZING_ENABLED", True)
ADAPTIVE_SIZE_SENSITIVITY = _env_float("ADAPTIVE_SIZE_SENSITIVITY", 0.25)
ADAPTIVE_SCALE_MIN = _env_float("ADAPTIVE_SCALE_MIN", 0.45)
ADAPTIVE_SCALE_MAX = _env_float("ADAPTIVE_SCALE_MAX", 1.15)
ADAPTIVE_TP_MIN_RATIO = _env_float("ADAPTIVE_TP_MIN_RATIO", 0.45)
ADAPTIVE_TP_MAX_RATIO = _env_float("ADAPTIVE_TP_MAX_RATIO", 1.0)

# --- Entry Timing: momentum feature calibration (2026-08 entry-timing fix) ---
# See trading.py EntryEngineV2/on_price_tick module notes for root cause:
# the momentum component previously read the single-tick price_return
# (features[22], typically ~1e-5 for BTC bookTicker jitter) but scored it
# against a 0.002 (0.2%) saturation threshold sized for a multi-candle move
# - the momentum term was effectively always ~0 regardless of real market
# momentum. Entry now reads the short rolling return (features[4],
# ROLLING_RETURN_WINDOWS[0]=5 candles) against this threshold instead.
ENTRY_MOMENTUM_SATURATION_PCT = _env_float("ENTRY_MOMENTUM_SATURATION_PCT", 0.0015)

# --- Profit Lock -------------------------------------------------------------
#
# 2026-08-22: the activation threshold is now derived from the take-profit
# target instead of being an independent fixed fraction of notional.
#
# WHY. The old value (0.00125 of notional, $0.05 at a $40 entry) armed the
# lock at 45% of the net TP target. With PROFIT_LOCK_RATIO = 0.50 the lock
# then closes on a retrace to half the peak, so a position that armed at $0.05
# net was exiting near $0.025 - and that is exactly what the live trades did:
# both winners in the 2026-08-22 06:00-08:30 window closed via profit_lock at
# net +$0.0262 and +$0.0270, roughly 39% of the take-profit target.
#
# That capping is not a small inefficiency, it is decisive. Measured over
# those six trades:
#
#     avg win (gross) +0.0541    avg loss (gross) -0.0417    win rate 33%
#     break-even win rate with winners capped at 0.0541 :  74%
#     break-even win rate with winners reaching  0.1400 :  39%
#
# A 74% win rate is unreachable, so while the lock arms this early the system
# cannot be profitable no matter how good entries get.
#
# HOW. The threshold is expressed as a fraction of the NET take-profit target
# - net because unrealized_pnl_usdt, the value it is compared against, is
# already fee-adjusted. Deriving it from TAKE_PROFIT_PCT keeps the two in step:
# change the TP and the lock follows, instead of silently reverting to capping
# winners at some unrelated dollar figure.
#
# Still routed through _notional_scaled so it stays in the startup risk-scaling
# report and remains overridable by an explicit env var.

# Measured round-trip cost over 13 live trades: 0.0732% of notional (a maker
# entry at 0.02% plus a taker exit at 0.05% lands in the same place). A literal
# rather than a reference to TAKER_FEE_RATE, which config.py does not define
# until further down - a forward reference here would be a NameError at import.
ROUND_TRIP_FEE_PCT_EST = 0.000732

# What the position must actually net to have "reached target".
NET_TAKE_PROFIT_PCT = max(0.0, TAKE_PROFIT_PCT - ROUND_TRIP_FEE_PCT_EST)

# 0.65 -> the lock arms once a position has banked ~two thirds of the net TP
# target, so a winner has room to run to target instead of being closed at the
# first retrace off a barely-positive peak.
PROFIT_LOCK_ACTIVATION_TP_FRACTION = _env_float(
    "PROFIT_LOCK_ACTIVATION_TP_FRACTION", 0.65
)

PROFIT_LOCK_ACTIVATION_USDT = _notional_scaled(
    "PROFIT_LOCK_ACTIVATION_USDT",
    PROFIT_LOCK_ACTIVATION_TP_FRACTION * NET_TAKE_PROFIT_PCT,
)
PROFIT_LOCK_RATIO = _env_float("PROFIT_LOCK_RATIO", 0.50)

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
PROFIT_LOCK_MIN_AGE_SEC = _env_float("PROFIT_LOCK_MIN_AGE_SEC", 4.0)
# P2 - PROFIT_LOCK_SLIPPAGE_ATR_MULT: the fee-safe floor Profit Lock requires
#   before closing was a FLAT MIN_NET_PROFIT_USDT ($0.05). In a 0.33%-ATR tape
#   one second of movement was worth $0.11 - more than twice the whole buffer.
#   The floor is now max(MIN_NET_PROFIT_USDT, mult * atr_pct * notional), so it
#   grows with volatility. At 0.5 x 0.333% x $79 that is ~$0.13.
# 2026-08-21 P2 RECALIBRATION: 0.5 -> 0.25.
#
# THE MECHANIC. Profit Lock closes only when
#     slippage_floor <= net <= peak x PROFIT_LOCK_RATIO
# so the trigger is a WINDOW, and its width is
#     peak x PROFIT_LOCK_RATIO - slippage_floor
# When the floor is large relative to half the peak that window collapses,
# and once the floor exceeds the locked level it closes entirely.
#
# WHAT PRODUCTION SHOWED (NEAR, 2026-08-20). Peak $0.1411 -> locked $0.0706,
# floor $0.0643 at atr 0.336%. That is a trigger window just $0.0063 wide -
# price had to land inside six-tenths of a cent. It did not; then ATR rose to
# 0.372%, the floor became $0.0712 > the $0.0706 locked level, and the window
# shut completely:
#
#   [profit-lock] HOLDING - executable net $+0.0572 is at/below the locked
#   level $+0.0706 but under the vol-aware fee-safe floor $0.0643 ...
#   "closing here would likely realize a loss after slippage"
#
# The trade later closed at EXACTLY +$0.0572. The slippage never materialised;
# the buffer blocked a valid exit and a $0.1411 peak decayed to $0.0572.
#
# THE HONEST TRADE-OFF. P2 chose 0.5 because of the 2026-08-19 15:28 incident,
# where a close estimated at +$0.1125 executable REALIZED -$0.0170 - $0.13 lost
# between decision and fill (0.16% of notional, about 0.5 ATR, which is exactly
# where 0.5 came from). Only a multiplier of ~0.43 or above still blocks that
# close, so ANY meaningful reduction re-opens that scenario. This is a
# calibration choice between two documented failure modes, not a clean bug fix:
#
#   too high -> the trigger window collapses and good exits are refused (NEAR)
#   too low  -> a fast tape can turn an estimated profit into a realized loss (15:28)
#
# WHY 0.25 IS DEFENSIBLE NOW. Three things changed since 15:28 that were not
# true when 0.5 was chosen:
#   - P1 replaced the mid/taker-taker estimator with
#     estimate_net_pnl_usdt_executable(), which prices the exit at the
#     executable side of the book and uses actual accrued commission. The
#     spread is now priced in BEFORE this buffer is applied, so part of what
#     0.5 was covering is already accounted for.
#   - The 2026-08-21 tick throttle took the event loop from ~0.8-1.1 CPU cores
#     (saturated, single-threaded) to ~0.31, and eliminated the websocket
#     starvation that came with it. Decision-to-fill latency in the 15:28
#     window was inflated by that saturation.
#   - The 50s orderflow minimum-hold means positions are no longer being
#     churned in the first seconds, when the tape is most hostile.
#
# At 0.25 the NEAR window widens from $0.0063 to $0.021 - 3.3x - while the
# buffer still scales with volatility and MIN_NET_PROFIT_USDT remains the
# absolute floor underneath. Set back to 0.5 to restore the previous
# behaviour exactly; raise toward 0.43+ to re-block the 15:28 scenario at the
# cost of the narrow window returning.
PROFIT_LOCK_SLIPPAGE_ATR_MULT = _env_float("PROFIT_LOCK_SLIPPAGE_ATR_MULT", 0.25)

# P4 - ATR-scaled risk geometry. Fixed-dollar thresholds on a fixed notional are
#   INVERSELY proportional to volatility in ATR terms, which is why the same
#   $0.15 loss trigger was ~3.5 ATR on 2026-08-18 (target unreachable at 12 ATR)
#   and ~0.36 ATR on 2026-08-19 (stopped out by ordinary tick noise in 3.5s).
#   Scaling both legs by ATR keeps the risk:reward ratio stable across regimes.
#   The pre-existing dollar/percent values are retained as CAPS, per the agreed
#   design - see the deployment notes about the cap binding at high ATR.
ATR_RISK_SCALING_ENABLED = _env_bool("ATR_RISK_SCALING_ENABLED", True)
# 2026-08-31: raised 1.2 -> 2.5 after the first two live trades under the
# aggressive sizing config (see manual 15) were both stopped out by ordinary
# tick noise in 5s and 63s respectively, for -$0.1458 and -$0.1382.
#
# This is the failure documented three lines above, recurring for a NEW
# reason. The stop did not change - the notional did, and that moved which
# clamp was binding. SL_MIN_USD carries an ABSOLUTE floor of $0.05, and at
# the previous $10 notional that floor was what actually set the stop,
# forcing it out to 2.55 ATR. At $60 notional the floor stops binding and the
# stop falls back to SL_ATR_MULT x ATR = 1.2 ATR - less than half as wide in
# volatility terms, without anyone choosing that. A 6x size change silently
# halved the stop distance.
#
# Setting SL_ATR_MULT to 2.5 restores the effective width the account was
# actually trading at ($0.289 vs $0.1388 at atr 0.196%, notional $59), and
# matches TP_ATR_MULT so both legs move together at 2.5 ATR - which is what
# "keeps the risk:reward ratio stable across regimes" above was always meant
# to deliver. It does NOT create edge: under random-walk pricing the 1.2 ATR
# and 2.5 ATR geometries are within ~1.3 bps of each other, both negative.
# What it buys is holding time - trades that resolve in minutes rather than
# seconds - which cuts trade frequency, and frequency is what multiplies a
# negative expectancy into a burn rate.
#
# ORDERING WITH THE EXCHANGE-SIDE STOP is preserved and was checked. The
# client-side rr-stop here is not the same thing as the protective
# STOP_MARKET resting on Binance: _compute_protective_stop_price() derives
# that one from MAX_TRADE_NET_LOSS_USDT - MAX_TRADE_EXIT_BUFFER_USDT
# (-$0.60 fee-net), independently of this multiplier. At the current ATR the
# client stop ($0.289) still fires well ahead of the exchange backstop
# ($0.60), which is the intended ordering. Above roughly 0.40% ATR the client
# stop would exceed the backstop and the exchange would trigger first - still
# protective, simply a tighter close than intended, and it is capped by
# MAX_STOP_LOSS_USD ($0.675) either way.
#
# COST: the per-trade loss ceiling roughly doubles, so MAX_DAILY_LOSS_USDT
# ($2.50) now binds after about 9 stopped-out trades instead of about 18.
SL_ATR_MULT = _env_float("SL_ATR_MULT", 2.5)
# FLOOR on the ATR-scaled stop. Caught by test_new_features.py during
# implementation: with the dollar value used purely as a CAP, a dead tape
# (atr 0.02%) produced 1.2 x 0.0002 x $80 = a $0.02 stop - smaller than the
# ~$0.055 round-trip fee, so the position could be stopped out before it could
# ever cover its own costs, and the RR stop fired ahead of every other exit.
# The effective stop is therefore clamped to
# [max(SL_MIN_USD, 1.5 x round-trip fee), MAX_STOP_LOSS_USD].
# 0.15% of notional -> $0.12 at $80, floored at $0.05 so a very small
# account still keeps a meaningful minimum stop.
SL_MIN_USD = _notional_scaled("SL_MIN_USD", 0.0015, floor=0.05)
TP_ATR_MULT = _env_float("TP_ATR_MULT", 2.5)

# P5 - Momentum-exhaustion guard. Both 2026-08-19 losses entered with
#   momentum_magnitude saturated at exactly 1.0000 AND |flow_delta| ~1e5
#   (+105,041 and +96,810) - i.e. the bot bought the top of a vertical move and
#   immediately mean-reverted. Saturated momentum plus extreme one-sided flow is
#   treated as LATE-ENTRY risk, not confirmation.
# ============================================================================
# 2026-08-21 TP-HIT PROBABILITY VETO
#
# Brain V2's tp_hit head predicts whether a trade will reach take-profit. Its
# output turned out to be the single sharpest divider in the live record.
# Across 18 closed trades on 2026-08-21 (ETH + NEAR):
#
#   tp_hit_prob ~0 (<1e-20)   10 trades   1/10 wins (10%)   net -$1.5461
#   tp_hit_prob 0.5            7 trades   2/7  wins (29%)   net -$0.1467
#   tp_hit_prob 1.0            1 trade    1/1  wins (100%)  net +$0.3596
#
# EVERY dollar of the drawdown sits in the near-zero bucket. All six of ETH's
# post-scale-up losses were in it (1.9e-52, 1.1e-46, 6.9e-23, 7.5e-48,
# 1.0e-21, 2.9e-46) and all six lost. The head carries only 30% of
# ConfidenceEngine's blend, which was not enough to stop entries clearing the
# composite threshold by as little as 0.004.
#
# WHY A FLOOR OF 0.10 IS SAFE. brain.py returns EXACTLY 0.5 whenever the head
# is not fitted or not reliable:
#
#     tp_hit_prob = (float(self.tp_hit_model.predict_proba(xn)[0][1])
#                    if (self.tp_hit_fitted and tp_hit_reliable) else 0.5)
#
# so an untrained or unreliable head can never be vetoed by a floor below 0.5.
# The veto ALSO checks head_readiness()["tp_hit"] == "READY" explicitly rather
# than relying on that coincidence. The observed values are strongly bimodal -
# either 0.5/1.0 or below 1e-20 - so anything in (0, 0.5) separates them
# identically; 0.10 is chosen as an order of magnitude above the observed
# near-zero cluster while leaving room for a genuinely uncertain 0.15-0.49
# reading to still trade.
#
# SCOPE. Entry gating ONLY, on the same footing as the regime / dead-market /
# counter-momentum / momentum-exhaustion guards: it can only ever REJECT a
# trade the old code would have taken. Exits, DCA, Profit Lock, Hard Stop and
# every open-position path are untouched.
#
# CAVEAT worth keeping in view: 18 trades is a small sample, and one WIN did
# sit in the near-zero bucket (NEAR at 7.8e-44, +$0.1331) - so this filter
# would have cost that trade. Set TP_HIT_VETO_ENABLED=false to disable.
# ============================================================================
# 2026-08-21: default flipped to OFF. The head this veto reads had diverged
# under the old learning rate (see brain.py) and was emitting ~1e-200 for
# every setup, so leaving the veto on by default would halt trading outright
# on any deployment that has not yet set the variable. It should be turned
# back on deliberately, once the rebuilt head has reached READY and its
# probabilities have been observed to be sane.
# ============================================================================
# FEATURE RECORDER (2026-08-24)
#
# Records every evaluated setup - accepted AND rejected - with its full
# feature vector, score components, orderflow snapshot and realised forward
# return, so entry rules can be tested offline instead of with live capital.
#
# Defaults OFF. Turn it on with FEATURE_RECORDER_ENABLED=true, ideally
# alongside DRY_RUN=true so the pipeline runs and records without trading.
# ============================================================================
FEATURE_RECORDER_ENABLED = _env_bool("FEATURE_RECORDER_ENABLED", False)

# Seconds between samples, per symbol. The decision loop runs ~3.5x/s, so
# recording every cycle would produce ~30 MB/hour and swamp the whole-file
# GitHub sync. 10s is also the honest choice statistically: consecutive ticks
# are so autocorrelated that denser sampling inflates the row count without
# adding independent information.
FEATURE_RECORDER_INTERVAL_SEC = _env_float("FEATURE_RECORDER_INTERVAL_SEC", 10.0)

# Forward-return horizons in seconds. A sample is held until the LONGEST one
# elapses, with MFE/MAE tracked throughout.
#
# 2026-08-26: extended to 3600s (1h). The 25.6h dataset showed mean reversion
# is real but too small to clear fees - fading moves beyond 2 ATR earned
# +2.5 to +5.1 bps gross against a 7.3 bps round trip. Reversion was also
# strongest at the LONGEST pair recorded (bwd300 -> r300, r = -0.063), which
# is exactly what you would see if the effect keeps growing past the edge of
# the measurement window. 300s was the limit, so that was untestable.
#
# 900/1800/3600 test it directly: if the reversion move scales with horizon
# while the fee stays fixed at 7.3 bps, a 15-60 minute hold could clear costs
# where a 5-minute one cannot.
#
# COST OF THE LONGER WINDOW - a sample is only written once its longest
# horizon matures, so rows now lag reality by an hour rather than 5 minutes,
# and a restart discards up to an hour of in-flight samples per symbol
# instead of 5 minutes. The short horizons are all retained, so nothing that
# worked before is lost.
FEATURE_RECORDER_HORIZONS_SEC = _env_str(
    "FEATURE_RECORDER_HORIZONS_SEC", "5,15,30,60,300,900,1800,3600"
)

# Files rotate on this interval and each completed shard uploads exactly once,
# so no sync ever re-sends the accumulated history.
FEATURE_RECORDER_SHARD_SEC = _env_float("FEATURE_RECORDER_SHARD_SEC", 3600.0)

# Safety valve: a stalled feed must not grow the pending buffer without bound.
FEATURE_RECORDER_MAX_PENDING = _env_int("FEATURE_RECORDER_MAX_PENDING", 2000)

# --- Local shard retention -------------------------------------------------
# The recorder produces ~400 KB per symbol per hour and never cleaned up after
# itself, so a long run filled the container's disk allowance while the bot was
# holding real positions. Retention only ever touches shards this process has
# CONFIRMED uploaded to GitHub - losing recorded data to a tidying routine
# would be worse than running out of disk, because a full disk is loud and a
# silently deleted dataset is not.
# Age rule: delete confirmed-uploaded shards older than this. 0 disables it.
FEATURE_LOG_RETAIN_LOCAL_HOURS = _env_float("FEATURE_LOG_RETAIN_LOCAL_HOURS", 6.0)
# Budget rule (safety net): if the shard directory still exceeds this after the
# age sweep, delete confirmed-uploaded shards oldest-first until it fits.
# 0 disables it. 256 MB is ~7 days of four-symbol recording.
FEATURE_LOG_MAX_LOCAL_MB = _env_float("FEATURE_LOG_MAX_LOCAL_MB", 256.0)
# Master switch. Off means shards accumulate exactly as they did before.
FEATURE_LOG_RETENTION_ENABLED = _env_bool("FEATURE_LOG_RETENTION_ENABLED", True)

# --- Donchian breakout engine (higher timeframe pivot) ---------------------
# The micro mean-reversion work concluded that no 10s-3600s signal clears the
# 7.32 bps round-trip fee: the best rule scored t=+0.27 against the fee over
# 42h, and 1 of 25 rules was net-positive out of sample. This engine trades a
# 15m/1h Donchian breakout instead, where a winner is 1-2% rather than 10 bps
# and the fee is ~0.5% of the move rather than ~100% of it.
#
# DEFAULT OFF, deliberately. It has NOT been validated on historical data -
# backtest_breakout.py could not reach Binance from the machine it was written
# on. Run it yourself over 3-6 months and turn this on only if the
# WALK-FORWARD (not the in-sample) lines are net profitable. Enabling this
# without that evidence repeats the mistake that produced 0W/7L.
BREAKOUT_ENGINE_ENABLED = _env_bool("BREAKOUT_ENGINE_ENABLED", False)
BREAKOUT_TIMEFRAME = _env_str("BREAKOUT_TIMEFRAME", "1h")
BREAKOUT_CHANNEL = _env_int("BREAKOUT_CHANNEL", 20)
BREAKOUT_ATR_PERIOD = _env_int("BREAKOUT_ATR_PERIOD", 14)
BREAKOUT_ATR_FLOOR = _env_float("BREAKOUT_ATR_FLOOR", 0.0025)
BREAKOUT_ATR_CEILING = _env_float("BREAKOUT_ATR_CEILING", 0.040)
BREAKOUT_STOP_ATR = _env_float("BREAKOUT_STOP_ATR", 1.5)
BREAKOUT_TP_ATR = _env_float("BREAKOUT_TP_ATR", 6.0)
BREAKOUT_TRAIL_ATR = _env_float("BREAKOUT_TRAIL_ATR", 2.0)
BREAKOUT_TRAIL_START_ATR = _env_float("BREAKOUT_TRAIL_START_ATR", 2.0)
BREAKOUT_RISK_PCT = _env_float("BREAKOUT_RISK_PCT", 0.01)
BREAKOUT_MAX_LEVERAGE = _env_float("BREAKOUT_MAX_LEVERAGE", 5.0)
BREAKOUT_ALLOW_SHORT = _env_bool("BREAKOUT_ALLOW_SHORT", True)


def breakout_params():
    """Live parameters as a BreakoutParams, so the bot and the backtest are
    driven by one set of numbers rather than two that can drift apart."""
    from breakout import BreakoutParams
    return BreakoutParams(
        channel=BREAKOUT_CHANNEL,
        atr_period=BREAKOUT_ATR_PERIOD,
        atr_floor=BREAKOUT_ATR_FLOOR,
        atr_ceiling=BREAKOUT_ATR_CEILING,
        stop_atr=BREAKOUT_STOP_ATR,
        tp_atr=BREAKOUT_TP_ATR,
        trail_atr=BREAKOUT_TRAIL_ATR,
        trail_start_atr=BREAKOUT_TRAIL_START_ATR,
        risk_pct=BREAKOUT_RISK_PCT,
        max_leverage=BREAKOUT_MAX_LEVERAGE,
        fee_bps_round_trip=ROUND_TRIP_FEE_PCT_EST * 1e4,
        allow_short=BREAKOUT_ALLOW_SHORT,
    )


def feature_recorder_horizons() -> list:
    """Parsed horizon list; malformed entries are skipped rather than fatal."""
    out = []
    for part in str(FEATURE_RECORDER_HORIZONS_SEC).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out.append(value)
    return sorted(set(out)) or [5.0, 15.0, 30.0, 60.0, 300.0]


TP_HIT_VETO_ENABLED = (
    _env_bool("TP_HIT_VETO_ENABLED", False)
)
# The tp_hit label asks whether price moved at least TAKE_PROFIT_PCT within
# LABEL_HORIZON_TICKS - a rare event, so a CORRECTLY calibrated head produces
# small numbers by construction. Measured on synthetic data at a 0.5% base
# rate, a healthy head's output topped out at 0.091 across every sample. A
# fixed 0.10 floor would therefore have vetoed 100% of entries even with a
# perfectly good model: the old default was unreachable, not merely strict.
#
# The threshold is now a FRACTION OF THE OBSERVED BASE RATE, so it means
# "materially worse than a randomly chosen moment" and stays meaningful
# whatever the label's natural frequency turns out to be per symbol.
TP_HIT_VETO_BASE_RATE_RATIO = _env_float("TP_HIT_VETO_BASE_RATE_RATIO", 0.5)

# Absolute floor, kept only as an escape hatch. Left at 0.0 the veto is
# purely base-rate-relative; set it above 0 to reinstate a hard cutoff.
TP_HIT_VETO_MIN_PROB = _env_float("TP_HIT_VETO_MIN_PROB", 0.0)

# The base rate is only trustworthy once enough labels have accumulated.
# Below this the veto stands down entirely rather than acting on noise.
TP_HIT_VETO_MIN_SAMPLES = _env_int("TP_HIT_VETO_MIN_SAMPLES", 5000)

MOMENTUM_EXHAUSTION_GUARD_ENABLED = _env_bool("MOMENTUM_EXHAUSTION_GUARD_ENABLED", True)
MOMENTUM_EXHAUSTION_MAGNITUDE = _env_float("MOMENTUM_EXHAUSTION_MAGNITUDE", 1.0)
MOMENTUM_EXHAUSTION_FLOW_DELTA = _env_float("MOMENTUM_EXHAUSTION_FLOW_DELTA", 50000)

# P6 - Market-websocket reconnect cooldown. Both losing entries fired 1-3s after
#   a bookTicker "1011 keepalive ping timeout" reconnect, and the incident tick
#   showed a 0.32% incoherence between the bot's own price and its own fill.
#   New entries are suppressed briefly after any market-stream reconnect so the
#   price/orderbook series is coherent before a decision is made. Open-position
#   management (TP/SL/Profit-Lock/Smart-Exit/DCA) is deliberately NOT gated.
MARKET_WS_RECONNECT_COOLDOWN_SEC = _env_float("MARKET_WS_RECONNECT_COOLDOWN_SEC", 3.0)

# --- Simple entry signal (warmup/fallback only, see BRAIN V2 below) ---------
SIGNAL_LOOKBACK_TICKS = 20
SIGNAL_DEADBAND_PCT = 0.0005

# --- Over-trading guardrails --------------------------------------------------
TRADE_COOLDOWN_SEC = _env_int("TRADE_COOLDOWN_SEC", 60)
MIN_HOLD_SEC_BEFORE_EXIT = _env_int("MIN_HOLD_SEC_BEFORE_EXIT", 60)
# 1.25% of notional -> $1.00 at $80. Deliberately just above the
# per-trade budget (1.125%), preserving the operator's existing choice
# that roughly one full-budget loss ends the UTC day. Raise the fraction
# to allow a longer losing sequence.
MAX_DAILY_LOSS_USDT = _notional_scaled("MAX_DAILY_LOSS_USDT", 0.0125)
# 1.25% of notional -> $1.00 at $80. Symmetric with the loss lock.
DAILY_PROFIT_TARGET_USDT = _notional_scaled("DAILY_PROFIT_TARGET_USDT", 0.0125)
# 2026-08 fee-net daily session locks: once today's (UTC calendar day)
# cumulative REALIZED NET PnL reaches either boundary, no NEW entries are
# opened for the rest of that UTC day. The tracker uses Binance's actual
# commissions when available, so +$0.50 means profit AFTER fees rather than
# a raw-price/gross-PnL target. An already-OPEN position keeps being managed
# normally (TP/Hard-Stop/Profit-Lock/DCA/Max-Hold-Time all remain active) -
# these values only gate the FLAT-state entry decision. Both locks reset at
# the next UTC day boundary. Set either value <=0 to disable that side.

# --- Fee-aware profit threshold ----------------------------------------------
TAKER_FEE_RATE = _env_float("TAKER_FEE_RATE", 0.0005)
# 0.0625% of notional -> $0.05 at $80. Absolute floor under the Profit
# Lock slippage buffer and the TP gates. This is a NET figure - fees are
# already deducted by estimate_net_pnl_usdt_executable() - so it does not
# need to track the fee rate.
MIN_NET_PROFIT_USDT = _notional_scaled("MIN_NET_PROFIT_USDT", 0.000625)

# --- Liquidation-price sanity check -------------------------------------------
LIQUIDATION_SANITY_MIN_RATIO = 0.2
LIQUIDATION_SANITY_MAX_RATIO = 5.0
LIQUIDATION_WARNING_BUFFER_PCT = _env_float("LIQUIDATION_WARNING_BUFFER_PCT", 0.15)

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
# 1.125% of notional -> $0.90 at $80. Per-trade fee-net loss budget.
MAX_TRADE_NET_LOSS_USDT = _notional_scaled("MAX_TRADE_NET_LOSS_USDT", 0.01125)
# 0.125% of notional -> $0.10 at $80. Subtracted from the budget above.
MAX_TRADE_EXIT_BUFFER_USDT = _notional_scaled("MAX_TRADE_EXIT_BUFFER_USDT", 0.00125)

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
PROTECTIVE_STOP_ENABLED = _env_bool("PROTECTIVE_STOP_ENABLED", True)
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
PROTECTIVE_STOP_RETRY_SEC = _env_float("PROTECTIVE_STOP_RETRY_SEC", 30)
PROTECTION_PENDING_MAX_SEC = _env_float("PROTECTION_PENDING_MAX_SEC", 300)

# --- State reconciliation grace period ----------------------------------------
SYNC_PENDING_GRACE_SEC = _env_int("SYNC_PENDING_GRACE_SEC", 8)

# --- Candle aggregation (backs ATR / EMA / regime / volume features) --------
CANDLE_INTERVAL_SEC = _env_int("CANDLE_INTERVAL_SEC", 60)
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
KLINE_WARMUP_ENABLED = _env_bool("KLINE_WARMUP_ENABLED", True)
KLINE_WARMUP_LIMIT = max(1, min(_env_int("KLINE_WARMUP_LIMIT", 100), CANDLE_HISTORY))

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
BRAIN2_WARMUP_UPDATES = _env_int("BRAIN2_WARMUP_UPDATES", 80)
LABEL_HORIZON_TICKS = 10

# --- tp_hit label calibration (2026-08-28) --------------------------------
# The tp_hit head labelled a sample positive when |forward return| over
# LABEL_HORIZON_TICKS reached TAKE_PROFIT_PCT. Those two numbers describe
# different timescales: 10 ticks is roughly 2.5 seconds, over which the
# median move measured on live data is 0.8 bps, against a 35 bps threshold.
# The label was therefore True 0.003% of the time, the head correctly learned
# to answer "no" always, and its output correlated -0.0007 with the outcome.
#
# The threshold is now a multiple of the move actually typical at this
# horizon, tracked as an EWMA. That asks a learnable question - "is this an
# unusually large move from here" - with a balanced label, instead of an
# impossible one. 1.2x the mean absolute move puts the base rate near 30%
# for a roughly Laplace return distribution.
TP_HIT_LABEL_ADAPTIVE = _env_bool("TP_HIT_LABEL_ADAPTIVE", True)
TP_HIT_LABEL_MULT = _env_float("TP_HIT_LABEL_MULT", 1.2)
# Below this many observations the EWMA is still moving; fall back to the
# fixed threshold rather than labelling against a half-formed scale.
TP_HIT_LABEL_MIN_SAMPLES = _env_int("TP_HIT_LABEL_MIN_SAMPLES", 500)

# --- noise label calibration (2026-08-29) ---------------------------------
# The same defect as tp_hit above, in the fourth head, found in live logs.
#
#     noise_band = max(old_atr_pct * 0.5, 1e-6)
#     is_noise   = abs(forward_return) < noise_band
#
# forward_return spans LABEL_HORIZON_TICKS - 10 ticks, about 2.5 seconds -
# while atr_pct is a 14-period ATR on ONE-MINUTE candles. At a live
# atr_pct of 0.0012 that band is 6.0 bps, against a median 2.5s move of
# 0.8 bps. The label was therefore True almost always, the head learned to
# answer "yes, noise" and was right, and its output carried no information.
#
# That is not harmless. noise_p enters the confidence blend as
#
#     confidence_score = raw_confidence * (1 - 0.5*noise_p) * (1 - 0.4*risk)
#
# so a head pinned near 1.0 HALVES brain_confidence on every tick - and
# brain_confidence is the heaviest term in the composite entry score
# (ENTRY_WEIGHTS 0.30). Solved back from 201 live entry-debug lines the
# implied noise_p had a median of 0.988, with 96.5% of ticks above 0.90.
# The entry score was structurally capped near 0.55 against a 0.75 bar, so
# no entry could fire even in WEAK_TREND, where the score topped out at
# 0.5089. This is why the bot had not opened a position.
#
# The band is now a multiple of the move actually typical at this horizon,
# tracked by the same EWMA the tp_hit label uses - one scale, so the two
# labels cannot drift onto different timescales.
NOISE_LABEL_ADAPTIVE = _env_bool("NOISE_LABEL_ADAPTIVE", True)
# Targets a roughly balanced label. Calibrated from our own production
# measurement rather than an assumed distribution: at TP_HIT_LABEL_MULT=1.2
# the live tp_hit base rate settled at 0.19-0.21, i.e. P(|r| < 1.2*scale)
# is about 0.80. Fitting 1 - exp(-k*m) to that point gives k = 1.34, and
# P(|r| < m*scale) = 0.5 then needs m = ln(2)/1.34 = 0.52. Raise it toward
# 1.0 to call more moves noise, lower it to call fewer.
NOISE_LABEL_MULT = _env_float("NOISE_LABEL_MULT", 0.5)
# Below this many observations the EWMA is still moving; fall back to the
# old ATR band rather than labelling against a half-formed scale.
NOISE_LABEL_MIN_SAMPLES = _env_int("NOISE_LABEL_MIN_SAMPLES", 500)

# --- simulated fills under DRY_RUN (2026-08-29) ---------------------------
# The success head learns only from CLOSED trades. Under DRY_RUN no order is
# ever sent, so no fill event ever arrives, so no position ever opens or
# closes, and the head has stayed at its 0.5 fallback with a handful of
# lifetime samples against BRAIN_HEAD_MIN_SAMPLES=20. It is starved, not
# broken.
#
# With this enabled, DRY_RUN orders are filled against the REAL mainnet
# prices the bot is already streaming: labels describe the market actually
# being traded, with nothing at risk and no venue mismatch. Two rules keep
# it honest - an order never fills on the tick that submitted it (deciding
# and executing on the same observation is lookahead, and it is the usual
# way a paper harness invents profit), and every fill is charged adverse
# slippage plus the taker commission.
#
# Has no effect whatsoever when DRY_RUN is false: real fills come from the
# exchange and this module is never consulted.
DRY_FILL_ENABLED = _env_bool("DRY_FILL_ENABLED", True)
# Adverse slippage per fill, in basis points of the fill price. Applied
# against the order in both directions (a BUY pays up, a SELL sells down).
DRY_FILL_SLIPPAGE_BPS = _env_float("DRY_FILL_SLIPPAGE_BPS", 1.0)
# Commission per fill as a fraction of notional. 0.05% is the Binance USD-M
# taker rate, which is what a MARKET order pays.
DRY_FILL_TAKER_FEE_PCT = _env_float("DRY_FILL_TAKER_FEE_PCT", 0.0005)
# Extra latency beyond "a later tick" before a simulated order may fill.
# The tick throttle (TICK_MIN_INTERVAL_SEC) already guarantees a gap, so 0
# means "next decision tick"; raise it to model a slower venue.
DRY_FILL_MIN_DELAY_SEC = _env_float("DRY_FILL_MIN_DELAY_SEC", 0.0)

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
BRAIN_HEAD_MIN_SAMPLES = _env_int("BRAIN_HEAD_MIN_SAMPLES", 20)
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
ENTRY_SCORE_THRESHOLD = min(0.95, max(0.70, _env_float("ENTRY_SCORE_THRESHOLD", 0.75)))
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
# 2026-08-23: SIDEWAYS entries are now DISABLED, not filtered.
#
# The 0.63 -> 0.68 raise on 2026-08-22 did not work, and the MFE data from the
# 18h18m run that followed shows why. Raising the bar only moved the marginal
# cluster: the five ETH losses scored 0.6819, 0.6822, 0.6834, 0.6849 and
# 0.6982 - barely clearing 0.68, exactly as the previous cluster barely
# cleared 0.63. Entries will always cluster just above whatever bar is set,
# because the bar selects RANK, not quality.
#
# What settles it is maximum favourable excursion - how far each trade ever
# ran in our favour before exiting:
#
#     SIDEWAYS      6 trades  0W/6L  net -0.4682  avg MFE 0.020%
#     WEAK_TREND    3 trades  2W/1L  net +0.0609  avg MFE 0.307%
#     STRONG_TREND  1 trade   1W/0L  net +0.0993  avg MFE 0.647%
#
# Five of the six SIDEWAYS losers never moved more than 0.02% our way; three
# had an MFE of exactly 0.000%, meaning price never ticked in our favour at
# all, and three were closed inside five seconds. These entries were not
# stopped out early or capped by an exit rule - they were wrong at the instant
# they were placed. No threshold, stop distance or take-profit setting can
# rescue a trade whose best moment is its entry.
#
# The mechanism: all five ETH losses were SHORTs into a 2413-2420 range. In a
# mean-reverting tape "momentum aligned" means selling the low, which is
# precisely backwards.
#
# Disabling SIDEWAYS over that window turns -0.3080 into +0.1602.
#
# 0.85 is above the regime's structural ceiling of 0.84 (with SIDEWAYS's caps
# of volatility_fit 0.40 and regime_fit 0.50, and every other term maxed), so
# no SIDEWAYS entry can qualify. This is deliberately an off-switch. It is a
# single value to revert if trending-only proves too sparse.
SIDEWAYS_ENTRY_SCORE_THRESHOLD = _env_float("SIDEWAYS_ENTRY_SCORE_THRESHOLD", 0.85)  # OFF-SWITCH: above the 0.84 structural max, so SIDEWAYS never enters. All other regimes unchanged at 0.75.
# Clean Live entry evidence exposed a directional scoring hole: the entry
# engine rewarded abs(momentum), so a strong upward move boosted a proposed
# SHORT exactly like a LONG. In SIDEWAYS, block a meaningful counter move
# (at least half the existing momentum-saturation scale); tiny sign jitter
# remains governed by the normal composite score instead of a hard gate.
SIDEWAYS_ENTRY_MOMENTUM_ALIGNMENT_ENABLED = (
    _env_bool("SIDEWAYS_ENTRY_MOMENTUM_ALIGNMENT_ENABLED", True)
)
SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO = _env_float("SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO", 0.50)
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
SMART_EXIT_ENABLED = _env_bool("SMART_EXIT_ENABLED", True)
SMART_EXIT_MAX_LOSS_PCT = 0.01
SMART_EXIT_MIN_LOSS_PCT = 0.0010   # Smart Exit gate: position must already be at least -0.10% adverse before Smart Exit is evaluated at all (2026-07 Smart Exit fix)
SMART_EXIT_CONFIRM_TICKS = 5
SMART_EXIT_MIN_AGREE = 5          # of the following 6 signals, how many must agree to exit (raised from 4 - 2026-07 Smart Exit fix)
SMART_EXIT_CONFIDENCE_DROP = 0.18  # confidence_score drop vs entry that counts as "dropped"
SMART_EXIT_ATR_MOVE_MULT = 0.8     # adverse move >= this * ATR% counts as a signal
SMART_EXIT_DCA_PROXIMITY_RATIO = 0.9  # if the adverse move is already >= this fraction of the current DCA trigger distance, Smart Exit is blocked so DCA can activate instead (2026-07 Smart Exit fix)
SMART_EXIT_MIN_HOLD_SEC = _env_float("SMART_EXIT_MIN_HOLD_SEC", 90)
# Smart-Exit-only minimum hold, separate from and stricter than MIN_HOLD_SEC_BEFORE_EXIT
# (60s) above. Only gates Smart Exit itself, so TP/partial-TP/trailing-stop timing is
# unchanged - a fresh entry just gets extra room before Smart Exit can close it
# (2026-08 Smart Exit V2 retune).
SMART_EXIT_MIN_AGREE_RANGING = _env_int("SMART_EXIT_MIN_AGREE_RANGING", 6)
# In SIDEWAYS/WEAK_TREND, require ALL 6 signals (unanimous) instead of the normal
# SMART_EXIT_MIN_AGREE (5), since ordinary chop/pullbacks in these regimes trip 1-2
# signals often without a real reversal happening. STRONG_TREND/HIGH_VOL keep the
# stricter 5-of-6 bar unchanged, so genuine reversals are still caught quickly
# (2026-08 Smart Exit V2 retune).

# --- ATR-based Dynamic DCA ----------------------------------------------------
DCA_ATR_MULTIPLIER = _env_float("DCA_ATR_MULTIPLIER", 1.2)
DCA_MIN_DISTANCE_PCT = _env_float("DCA_MIN_DISTANCE_PCT", 0.0015)
DCA_MAX_DISTANCE_PCT = _env_float("DCA_MAX_DISTANCE_PCT", 0.02)

# --- Dynamic position sizing ---------------------------------------------------
SIZE_MIN_MULT = _env_float("SIZE_MIN_MULT", 0.5)
SIZE_MAX_MULT = _env_float("SIZE_MAX_MULT", 1.5)

# --- Partial TP / breakeven / trailing stop -----------------------------------
PARTIAL_TP_ENABLED = _env_bool("PARTIAL_TP_ENABLED", False)  # disabled by default (2026-07 profitability fix) - was truncating winners; trailing stop remains active
PARTIAL_TP_FRACTION = _env_float("PARTIAL_TP_FRACTION", 0.5)
PARTIAL_TP_TRIGGER_RATIO = _env_float("PARTIAL_TP_TRIGGER_RATIO", 0.6)  # of dynamic TP distance
BREAKEVEN_AFTER_PARTIAL = _env_bool("BREAKEVEN_AFTER_PARTIAL", True)
TRAILING_STOP_ENABLED = _env_bool("TRAILING_STOP_ENABLED", True)
TRAILING_STOP_ATR_MULT = _env_float("TRAILING_STOP_ATR_MULT", 1.0)

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
STATS_EXPORT_INTERVAL_SEC = _env_int("STATS_EXPORT_INTERVAL_SEC", 300)

# --- Funding rate / open interest (best-effort extra features) ---------------
FUNDING_OI_POLL_SEC = _env_int("FUNDING_OI_POLL_SEC", 120)

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
BRAIN_AUTO_PUSH_INTERVAL_SEC = _env_int("BRAIN_AUTO_PUSH_INTERVAL_SEC", 300)

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
# Feature-recorder dataset. Sharded, so these are the BASE names - the
# recorder appends a UTC timestamp per shard.
FEATURE_LOG_PATH = os.environ.get(
    "FEATURE_LOG_PATH", _symbol_scoped_default("feature_log.jsonl")
)
GITHUB_FEATURE_LOG_PATH = os.environ.get(
    "GITHUB_FEATURE_LOG_PATH",
    "/".join(p for p in (_GITHUB_BRAIN_DIR, _symbol_scoped_default("feature_log.jsonl")) if p),
)
_warn_if_explicit_path_bypasses_isolation("FEATURE_LOG_PATH")
_warn_if_explicit_path_bypasses_isolation("GITHUB_FEATURE_LOG_PATH")

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
    value = _env_float(name, default)
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
    BALANCE_REFRESH_SEC = max(_env_float("BALANCE_REFRESH_SEC", 60), REST_POLL_MIN_SEC)
except (TypeError, ValueError):
    BALANCE_REFRESH_SEC = 60.0
try:
    BALANCE_WS_FRESH_SEC = max(_env_float("BALANCE_WS_FRESH_SEC", 90), 0.0)
except (TypeError, ValueError):
    BALANCE_WS_FRESH_SEC = 90.0
try:
    REST_POLL_JITTER_PCT = min(max(_env_float("REST_POLL_JITTER_PCT", 0.15), 0.0), 0.5)
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
    # 2026-08-21 robust env parsing helpers (exported so trading.py and any
    # future module can read the environment through the same safe path).
    "_env_raw", "_env_float", "_env_int", "_env_bool", "_env_str",
    "env_parse_warnings",
    "SYMBOL",
    # 2026-08-20 multi-coin watchlist
    "ACTIVE_SYMBOLS",
    "MAX_ACTIVE_TRADES",
    "symbol_scoped_name",
    # 2026-08-21 notional-relative risk scaling
    "ENTRY_NOTIONAL_USDT",
    "notional_scaling_report",
    # 2026-08-21 tp_hit probability veto
    "FEATURE_LOG_PATH",
    "GITHUB_FEATURE_LOG_PATH",
    "FEATURE_RECORDER_ENABLED",
    "FEATURE_RECORDER_INTERVAL_SEC",
    "FEATURE_RECORDER_HORIZONS_SEC",
    "FEATURE_RECORDER_SHARD_SEC",
    "FEATURE_RECORDER_MAX_PENDING",
    "feature_recorder_horizons",
    # 2026-08-27 local shard retention
    "FEATURE_LOG_RETENTION_ENABLED",
    "FEATURE_LOG_RETAIN_LOCAL_HOURS",
    "FEATURE_LOG_MAX_LOCAL_MB",
    # 2026-08-28 Donchian breakout engine
    "BREAKOUT_ENGINE_ENABLED",
    "BREAKOUT_TIMEFRAME",
    "BREAKOUT_CHANNEL",
    "BREAKOUT_ATR_PERIOD",
    "BREAKOUT_ATR_FLOOR",
    "BREAKOUT_ATR_CEILING",
    "BREAKOUT_STOP_ATR",
    "BREAKOUT_TP_ATR",
    "BREAKOUT_TRAIL_ATR",
    "BREAKOUT_TRAIL_START_ATR",
    "BREAKOUT_RISK_PCT",
    "BREAKOUT_MAX_LEVERAGE",
    "BREAKOUT_ALLOW_SHORT",
    "breakout_params",
    "TP_HIT_VETO_ENABLED",
    "TP_HIT_VETO_MIN_PROB",
    "TP_HIT_VETO_BASE_RATE_RATIO",
    "TP_HIT_VETO_MIN_SAMPLES",
    # 2026-08-21 tick throttling
    "TICK_MIN_INTERVAL_SEC",
    "TICK_MIN_INTERVAL_ACTIVE_SEC",
    "TICK_THROTTLE_LOG_INTERVAL_SEC",
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
    "PROFIT_LOCK_ACTIVATION_TP_FRACTION",
    "NET_TAKE_PROFIT_PCT",
    "ROUND_TRIP_FEE_PCT_EST",
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
    "TP_HIT_LABEL_ADAPTIVE",
    "TP_HIT_LABEL_MULT",
    "TP_HIT_LABEL_MIN_SAMPLES",
    "NOISE_LABEL_ADAPTIVE",
    "NOISE_LABEL_MULT",
    "NOISE_LABEL_MIN_SAMPLES",
    "DRY_FILL_ENABLED",
    "DRY_FILL_SLIPPAGE_BPS",
    "DRY_FILL_TAKER_FEE_PCT",
    "DRY_FILL_MIN_DELAY_SEC",
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
ENABLE_ORDERBOOK_GUARD = _env_bool("ENABLE_ORDERBOOK_GUARD", True)
ORDERBOOK_IMBALANCE_THRESHOLD = _env_float("ORDERBOOK_IMBALANCE_THRESHOLD", 0.20)
AGG_TRADE_DELTA_WINDOW_SEC = _env_int("AGG_TRADE_DELTA_WINDOW_SEC", 10)
# 1.125% of notional -> $0.90 at $80. Cap on the ATR-scaled stop.
MAX_STOP_LOSS_USD = _notional_scaled("MAX_STOP_LOSS_USD", 0.01125)
# 1.125% of notional -> $0.90 at $80. Reward leg of the 1:N RR envelope.
TARGET_PROFIT_USD = _notional_scaled("TARGET_PROFIT_USD", 0.01125)
COOL_OFF_PERIOD_MINUTES = _env_float("COOL_OFF_PERIOD_MINUTES", 15)
USE_POST_ONLY_LIMIT = _env_bool("USE_POST_ONLY_LIMIT", True)

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
MAX_DCA_STEPS = min(1, max(0, _env_int("MAX_DCA_STEPS", 1)))

# --- 2. High-frequency market-data layer (websocket.py) ---------------------
# Binance publishes partial book depth in fixed sizes (5/10/20) and at
# 250ms/500ms/100ms speeds. We subscribe to the 20-level/100ms stream and
# compute the imbalance over the top ORDERBOOK_DEPTH_LEVELS (10) of each
# side, per the spec:
#     (top10_bid_vol - top10_ask_vol) / (top10_bid_vol + top10_ask_vol)
ORDERBOOK_STREAM_DEPTH = _env_int("ORDERBOOK_STREAM_DEPTH", 20)
ORDERBOOK_STREAM_SPEED_MS = _env_int("ORDERBOOK_STREAM_SPEED_MS", 100)
ORDERBOOK_DEPTH_LEVELS = _env_int("ORDERBOOK_DEPTH_LEVELS", 10)
# RAM safety on Railway: both rolling buffers are collections.deque with a
# hard maxlen, so the orderflow layer's memory footprint is bounded and
# constant no matter how long the process runs. 600 depth samples at 100ms
# is a 60s rolling view; 4000 aggTrades comfortably covers the 10s delta
# window even in a violent tape.
ORDERBOOK_BUFFER_MAXLEN = _env_int("ORDERBOOK_BUFFER_MAXLEN", 600)
AGG_TRADE_BUFFER_MAXLEN = _env_int("AGG_TRADE_BUFFER_MAXLEN", 4000)
# A depth/trade reading older than this is treated as "no data" rather than
# as a live signal - a silently dead stream must never look like a neutral
# (0.0) orderbook.
ORDERFLOW_STALE_SEC = _env_float("ORDERFLOW_STALE_SEC", 5.0)
# Dynamic auto-reconnect tuning for the market websockets (Railway network
# drops): full jitter is applied to the existing exponential backoff so a
# multi-stream reconnect storm never re-hits Binance in lockstep.
WS_RECONNECT_JITTER_RATIO = _env_float("WS_RECONNECT_JITTER_RATIO", 0.5)

# --- 3. Entry Liquidity & Flow Guard (brain.py) ------------------------------
# Multi-factor confirmation: a trade may only open when the TECHNICAL signal
# (existing Brain/EMA/RSI/ATR/regime score), the ORDERBOOK support, and the
# 10s trade-flow DELTA all agree. ORDERBOOK_SUPPORT_MIN is the minimum
# same-side imbalance that counts as genuine book support (0.0 = book merely
# not against us); AGG_TRADE_DELTA_MIN is the equivalent floor on the signed
# 10s volume delta, in base-asset units.
ORDERBOOK_SUPPORT_MIN = _env_float("ORDERBOOK_SUPPORT_MIN", 0.0)
AGG_TRADE_DELTA_MIN = _env_float("AGG_TRADE_DELTA_MIN", 0.0)
# When True (default), an unavailable/stale orderflow feed BLOCKS entries
# instead of silently degrading to "no guard". Fail-safe, not fail-open.
ORDERBOOK_GUARD_REQUIRES_DATA = (
    _env_bool("ORDERBOOK_GUARD_REQUIRES_DATA", True)
)

# --- 4. Strict 1:2 risk-to-reward envelope (trading.py) ---------------------
# Sized for the documented account shape: $4 initial margin @ 20x leverage
# on a ~$20 wallet (= $80 notional). SL is capped at $0.15-$0.20 fee-net and
# TP targeted at $0.35-$0.40 fee-net, i.e. ~1:2. MAX_STOP_LOSS_USD /
# TARGET_PROFIT_USD above are the outer (worst/best) bounds of those bands;
# the MIN_* values below are the inner bounds, used so a winner can be
# banked at $0.35 the moment orderflow turns instead of insisting on the
# full $0.40.
# DEAD VALUE. Imported by trading.py but never read for any decision - the
# working stop floor is SL_MIN_USD above. Retained only so the existing import
# does not break; setting it has no effect. Safe to delete from Railway.
MIN_STOP_LOSS_USD = _env_float("MIN_STOP_LOSS_USD", 0.15)
# 0.4375% of notional -> $0.35 at $80. The orderflow-assisted TP path,
# which produced the single best trade of 2026-08-21 (+$0.3596 on NEAR).
MIN_TARGET_PROFIT_USD = _notional_scaled("MIN_TARGET_PROFIT_USD", 0.004375)
ENFORCE_RISK_REWARD_USD = (
    _env_bool("ENFORCE_RISK_REWARD_USD", True)
)
# Post-only (maker) entry execution. A GTX order that would cross the book
# is rejected by Binance rather than paying taker fees; we re-price once,
# and only then fall back to the original MARKET behavior (so the bot can
# never be left unable to trade at all by a fast tape).
POST_ONLY_LIMIT_OFFSET_TICKS = _env_int("POST_ONLY_LIMIT_OFFSET_TICKS", 0)
POST_ONLY_LIMIT_TIMEOUT_SEC = _env_float("POST_ONLY_LIMIT_TIMEOUT_SEC", 20)
POST_ONLY_MARKET_FALLBACK = (
    _env_bool("POST_ONLY_MARKET_FALLBACK", True)
)

# --- 5. Continuous 24/7 execution -------------------------------------------
# The existing MAX_DAILY_LOSS_USDT / DAILY_PROFIT_TARGET_USDT variables and
# their entry gates are PRESERVED exactly as they are. This toggle decides
# whether those two gates still halt new entries for the remainder of a UTC
# day. The default now preserves the profit lock. Daily loss is always a
# hard admission limit, even when continuous operation is explicitly enabled.
CONTINUOUS_24_7_TRADING = (
    _env_bool("CONTINUOUS_24_7_TRADING", False)
)

# --- 6. Dynamic post-loss cool-off window -----------------------------------
# On ANY losing trade: no new entry for COOL_OFF_PERIOD_MINUTES, and for an
# equally long window AFTER that, the entry orderbook guard is tightened -
# the adverse-imbalance veto trips earlier (threshold reduced by
# COOL_OFF_IMBALANCE_TIGHTEN) and genuine same-side book support of at least
# COOL_OFF_SUPPORT_MIN is required. This is the anti-revenge-trading rule.
COOL_OFF_IMBALANCE_TIGHTEN = _env_float("COOL_OFF_IMBALANCE_TIGHTEN", 0.10)
COOL_OFF_SUPPORT_MIN = _env_float("COOL_OFF_SUPPORT_MIN", 0.20)

# --- 7. Smart Orderflow Early Exit ------------------------------------------
# If the orderbook imbalance flips violently AGAINST an open position before
# the stop is reached, exit at a micro-loss instead of riding it to the full
# SL. Fires only inside the $0.05-$0.10 fee-net micro-loss band.
SMART_ORDERFLOW_EXIT_ENABLED = (
    _env_bool("SMART_ORDERFLOW_EXIT_ENABLED", True)
)
SMART_ORDERFLOW_EXIT_IMBALANCE = _env_float("SMART_ORDERFLOW_EXIT_IMBALANCE", 0.35)
# 0.125% of notional -> $0.10 at $80. THE BAND FLOOR THAT MUST SCALE.
# A fresh position's fee-net PnL starts at exactly minus the round-trip fee
# (~0.07% of notional, measured from the live trade log). If this floor ever
# drops below that, every position is born INSIDE the exit band with zero
# price movement and becomes eligible for an immediate orderflow exit. At the
# old fixed $0.05 that is precisely what the $2 -> $4 scale-up would have
# caused ($80 notional -> $0.056 of fees > $0.05). Scaling it keeps the floor
# a constant ~1.8x the round-trip fee at any size.
SMART_ORDERFLOW_EXIT_MIN_LOSS_USD = _notional_scaled(
    "SMART_ORDERFLOW_EXIT_MIN_LOSS_USD", 0.00125
)
# 0.25% of notional -> $0.20 at $80. Keeps the band exactly 2x as wide as
# its floor at every position size.
SMART_ORDERFLOW_EXIT_MAX_LOSS_USD = _notional_scaled(
    "SMART_ORDERFLOW_EXIT_MAX_LOSS_USD", 0.0025
)
# 2026-08-21 minimum-hold widening (10s -> 50s).
#
# Live evidence from deployment c5d71582: three of five closed trades exited
# via orderflow_smart_exit at holds of 10.5s, 16.6s and 12.8s - i.e. within
# seconds of this gate expiring at its old 10s value - each for a fee-net
# loss of $0.058-$0.068 on a ~$38 notional. Round-trip fees alone are
# ~$0.027, so those exits were realizing barely more than the cost of the
# round trip: normal entry noise being converted into a booked loss.
#
# Ten seconds of orderbook imbalance is microstructure noise, not a thesis
# being invalidated. 50s sits between the two sibling gates that already
# exist for the slower exits - MIN_HOLD_SEC_BEFORE_EXIT (60s) and
# SMART_EXIT_MIN_HOLD_SEC (90s) - and keeps this the fastest-reacting
# discretionary exit while no longer firing inside the spread.
#
# This changes ONLY when the orderflow exit becomes eligible. Throughout the
# window the Hard Stop, the trade-loss budget, the 1:N RR stop and Profit
# Lock all remain fully active (they are evaluated earlier in
# _manage_open_position, or fall through unaffected), so downside is still
# capped at the RR ceiling - roughly 2x the micro-loss this gate used to
# book, against the full winning distribution on the upside.
#
# Set back to 10 to restore the previous behaviour exactly.
SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC = _env_float("SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC", 50)

# --- 8. Safe DCA: single-step rescue order, break-even target ---------------
# The one permitted DCA add is only submitted when the book itself supports
# the reversal (bids for a LONG rescue, asks for a SHORT rescue). Once that
# rescue has filled, the position stops chasing the full TP and targets a
# fast break-even exit instead.
DCA_REQUIRE_ORDERBOOK_SUPPORT = (
    _env_bool("DCA_REQUIRE_ORDERBOOK_SUPPORT", True)
)
DCA_RESCUE_SUPPORT_MIN = _env_float("DCA_RESCUE_SUPPORT_MIN", 0.10)
DCA_RESCUE_BREAKEVEN_ENABLED = (
    _env_bool("DCA_RESCUE_BREAKEVEN_ENABLED", True)
)
# 0.05% of notional -> $0.04 at $80.
DCA_RESCUE_BREAKEVEN_MIN_NET_USD = _notional_scaled(
    "DCA_RESCUE_BREAKEVEN_MIN_NET_USD", 0.0005
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

# Account-wide admission policy. These are stress reserves, not an exact
# exchange liquidation formula. Keep exchange-native stops enabled.
EXPOSURE_GUARD_ENABLED = _env_bool("EXPOSURE_GUARD_ENABLED", True)
EXPOSURE_HEADROOM_PCT = max(0.0, _env_float("EXPOSURE_HEADROOM_PCT", 0.03))
EXPOSURE_MAINTENANCE_FLOOR_PCT = max(0.001, _env_float("EXPOSURE_MAINTENANCE_FLOOR_PCT", 0.01))
EXPOSURE_COST_RESERVE_PCT = max(0.0, _env_float("EXPOSURE_COST_RESERVE_PCT", 0.002))
EXPOSURE_RECHECK_SEC = max(1.0, _env_float("EXPOSURE_RECHECK_SEC", 10.0))
PAPER_START_BALANCE_USDT = max(0.0, _env_float("PAPER_START_BALANCE_USDT", 15.0))
__all__ += ["EXPOSURE_GUARD_ENABLED", "EXPOSURE_HEADROOM_PCT",
            "EXPOSURE_MAINTENANCE_FLOOR_PCT", "EXPOSURE_COST_RESERVE_PCT",
            "EXPOSURE_RECHECK_SEC", "PAPER_START_BALANCE_USDT"]
