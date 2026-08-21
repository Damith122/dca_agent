================================================================================
 DEPLOYMENT PACKAGE - dynamic risk scaling + the two pending code fixes
 Built 2026-08-21
================================================================================

*** DEPLOY THIS BEFORE CLEARING ANY RAILWAY VARIABLES. ***
See "RAILWAY CLEANUP" below - the order matters and getting it wrong crashes
the bot at import.

FILES
--------------------------------------------------------------------------
  config.py     CHANGED  notional-relative scaling for 13 dollar thresholds
  trading.py    CHANGED  Profit Lock recalibration + min-hold diagnostics
                         + rr_ratio() now reports the effective ratio
  dca2.py       CHANGED  startup report of every derived threshold
  test_*.py     the new suite plus the three existing ones this touched

This bundle INCLUDES the two fixes that were missing from your last deploy:
  - Profit Lock slippage recalibration (0.5 -> 0.25) and the
    "UNFIRABLE AT THIS PEAK" diagnostic
  - orderflow minimum-hold at 50s and its SUPPRESSED log line
The numeric halves of both are already live via Railway variables; this
bundle adds the code and the diagnostics.

WHAT CHANGED
--------------------------------------------------------------------------
Every per-trade dollar threshold is now derived from

    ENTRY_NOTIONAL_USDT = INITIAL_ENTRY_USDT x LEVERAGE

instead of being a hardcoded dollar value. Change INITIAL_ENTRY_USDT alone
and the whole risk geometry follows - no more ten-variable manual edits.

Thirteen thresholds are scaled:
    MAX_STOP_LOSS_USD                  1.125%  of notional
    MAX_TRADE_NET_LOSS_USDT            1.125%
    TARGET_PROFIT_USD                  1.125%
    MAX_TRADE_EXIT_BUFFER_USDT         0.125%
    PROFIT_LOCK_ACTIVATION_USDT        0.125%
    SMART_ORDERFLOW_EXIT_MIN_LOSS_USD  0.125%
    SMART_ORDERFLOW_EXIT_MAX_LOSS_USD  0.25%
    MIN_TARGET_PROFIT_USD              0.4375%
    SL_MIN_USD                         0.15%   (absolute floor $0.05)
    MAX_DAILY_LOSS_USDT                1.25%
    DAILY_PROFIT_TARGET_USDT           1.25%
    MIN_NET_PROFIT_USDT                0.0625%
    DCA_RESCUE_BREAKEVEN_MIN_NET_USD   0.05%

BEHAVIOUR-NEUTRAL TODAY. Every fraction was chosen so that at your live
INITIAL_ENTRY_USDT=4 / LEVERAGE=20 ($80 notional) the derived value equals
exactly what is running now. Nothing about the strategy changes on deploy.
The test suite asserts this against the live figures.

Scaling is off NOTIONAL, not wallet balance. Balance moves with every closed
trade and every deposit, so a balance-relative stop would drift mid-session
and change meaning after each win or loss. Notional is fixed for the life of
a position - the horizon these thresholds actually govern.

RAILWAY CLEANUP - READ THE ORDER
--------------------------------------------------------------------------
I could NOT safely clear the variables for you yet, and I did not try.

The Railway MCP has no delete-variable primitive - only "set". Setting a
variable to an empty string is the closest available action, and against the
code you are running RIGHT NOW that is fatal:

    float(os.environ.get("MAX_STOP_LOSS_USD", "0.20"))
    -> ValueError: could not convert string to float: ''
    -> crash at import, on every restart

The new config.py treats empty AND missing as "unset" and falls back to the
derived value, so clearing is only safe once this bundle is live.

  STEP 1  Deploy this ZIP. Your existing variables stay set; because they
          hold exactly the derived values, behaviour is identical. Startup
          will print each threshold marked OVERRIDDEN plus a summary line
          naming them - that is expected at this stage.

  STEP 2  Then remove these 13 from the Railway dashboard (or ask me and I
          will empty them, which the new code reads as unset):

              MAX_STOP_LOSS_USD
              MAX_TRADE_NET_LOSS_USDT
              MAX_TRADE_EXIT_BUFFER_USDT
              TARGET_PROFIT_USD
              MIN_TARGET_PROFIT_USD
              PROFIT_LOCK_ACTIVATION_USDT
              MIN_NET_PROFIT_USDT
              SL_MIN_USD
              MAX_DAILY_LOSS_USDT
              DAILY_PROFIT_TARGET_USDT
              SMART_ORDERFLOW_EXIT_MIN_LOSS_USD
              SMART_ORDERFLOW_EXIT_MAX_LOSS_USD
              DCA_RESCUE_BREAKEVEN_MIN_NET_USD

          Plus MIN_STOP_LOSS_USD, which is dead code - imported but never
          read for any decision.

  STEP 3  Startup should then report 13/13 derived and no warning line.

KEEP these - they are real inputs, not derived values:
    INITIAL_ENTRY_USDT (4)          the single knob everything scales from
    SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC (50)
    PROFIT_LOCK_SLIPPAGE_ATR_MULT (0.25)
    ACTIVE_SYMBOLS, MAX_ACTIVE_TRADES, SYMBOL, API keys, GitHub vars, etc.

TO RESIZE IN FUTURE
--------------------------------------------------------------------------
Change INITIAL_ENTRY_USDT and nothing else. At $8 you would get a $160
notional with MAX_STOP_LOSS_USD $1.80, TARGET_PROFIT_USD $1.80, daily lock
$2.00, orderflow band [-$0.40, -$0.20] - all automatically, all in the same
proportions.

ONE THING I CORRECTED
--------------------------------------------------------------------------
rr_ratio() was computing TARGET_PROFIT_USD over the stop CAP rather than the
stop actually in force. The cap almost never binds - at $80 notional across
the observed 0.09-0.42% ATR band the working stop is $0.12-$0.40 against a
$0.90 cap - so the logs printed "1:1 envelope" when the real reward:risk was
between 2:1 and 7:1. It now reports against the effective stop. This is a
logging fix only; no decision ever read that number.

VERIFICATION
--------------------------------------------------------------------------
  python3 test_notional_scaled_risk_fix.py   ->  62 passed, 0 failed

  Full suite: 26 of 31 pass. The 5 failures are pre-existing stale
  assertions from the P1-P6 recalibration and are unrelated to this change.
  That is one FEWER than before - test_new_features.py now passes.
================================================================================
