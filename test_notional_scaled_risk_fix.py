"""Offline regression tests for the 2026-08-21 notional-relative risk scaling.

THE PROBLEM
--------------------------------------------------------------------------
PnL scales with notional: the same 0.2% move is worth $0.08 at a $40 entry
and $0.16 at $80. A hardcoded dollar threshold therefore silently HALVES in
percentage terms every time position size doubles - a stop that was 1.1% of
notional becomes 0.55%, twice as tight, without anyone deciding that.

Scaling INITIAL_ENTRY_USDT from 2 to 4 required TEN separate manual env-var
edits to keep the geometry intact. One of them, if missed, broke outright:
at $80 notional a fresh position's fee-net PnL starts at about -$0.056, which
falls inside the old [-$0.10, -$0.05] orderflow exit band - so every position
would have been born eligible for an immediate exit.

THE FIX
--------------------------------------------------------------------------
Every per-trade dollar threshold is now _notional_scaled(env_var, fraction),
derived from ENTRY_NOTIONAL_USDT = INITIAL_ENTRY_USDT x LEVERAGE. The
fractions were chosen so that at the live INITIAL_ENTRY_USDT=4 / LEVERAGE=20
($80 notional) every value reproduces EXACTLY what was running before the
refactor - so this is behaviour-neutral on the day it ships.

An explicit env var still wins, and is now recorded and reported at startup,
because a leftover override does not scale and would silently pin one
threshold while the rest of the geometry moves around it.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_notional_scaled_risk_fix.py`
"""
import os
import sys

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SYMBOL", "SOLUSDT")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -> ' + detail) if detail and not cond else ''}")


SCALED = [
    "MAX_STOP_LOSS_USD", "MAX_TRADE_NET_LOSS_USDT", "MAX_TRADE_EXIT_BUFFER_USDT",
    "TARGET_PROFIT_USD", "MIN_TARGET_PROFIT_USD", "PROFIT_LOCK_ACTIVATION_USDT",
    "MIN_NET_PROFIT_USDT", "SL_MIN_USD", "MAX_DAILY_LOSS_USDT",
    "DAILY_PROFIT_TARGET_USDT", "SMART_ORDERFLOW_EXIT_MIN_LOSS_USD",
    "SMART_ORDERFLOW_EXIT_MAX_LOSS_USD", "DCA_RESCUE_BREAKEVEN_MIN_NET_USD",
]

# Exactly what was running on Railway before this refactor.
LIVE_AT_4 = {
    "MAX_STOP_LOSS_USD": 0.90, "MAX_TRADE_NET_LOSS_USDT": 0.90,
    "MAX_TRADE_EXIT_BUFFER_USDT": 0.10, "TARGET_PROFIT_USD": 0.90,
    "MIN_TARGET_PROFIT_USD": 0.35, "PROFIT_LOCK_ACTIVATION_USDT": 0.10,
    "MIN_NET_PROFIT_USDT": 0.05, "SL_MIN_USD": 0.12,
    "MAX_DAILY_LOSS_USDT": 1.00, "DAILY_PROFIT_TARGET_USDT": 1.00,
    "SMART_ORDERFLOW_EXIT_MIN_LOSS_USD": 0.10,
    "SMART_ORDERFLOW_EXIT_MAX_LOSS_USD": 0.20,
    "DCA_RESCUE_BREAKEVEN_MIN_NET_USD": 0.04,
}


def load_config(initial_entry, overrides=None):
    """Re-import config with a given INITIAL_ENTRY_USDT and no stale overrides."""
    for v in SCALED:
        os.environ.pop(v, None)
    os.environ["INITIAL_ENTRY_USDT"] = str(initial_entry)
    for k, v in (overrides or {}).items():
        os.environ[k] = str(v)
    sys.modules.pop("config", None)
    import config
    return config


# ===========================================================================
print("\n[1] Behaviour-neutral at the live size")
# ===========================================================================
c = load_config(4)
check("ENTRY_NOTIONAL_USDT is INITIAL_ENTRY_USDT x LEVERAGE",
      c.ENTRY_NOTIONAL_USDT == 4 * c.LEVERAGE, f"got {c.ENTRY_NOTIONAL_USDT}")
for name, want in sorted(LIVE_AT_4.items()):
    got = getattr(c, name)
    check(f"{name} derives to its live value ${want:.4f}", abs(got - want) < 1e-9,
          f"got {got:.4f}")

# ===========================================================================
print("\n[2] Everything scales linearly with INITIAL_ENTRY_USDT")
# ===========================================================================
c2, c4, c8 = load_config(2), load_config(4), load_config(8)
for name in SCALED:
    v2, v4, v8 = getattr(c2, name), getattr(c4, name), getattr(c8, name)
    # SL_MIN_USD has an absolute floor, so it is checked separately below.
    if name == "SL_MIN_USD":
        continue
    linear = abs(v4 - 2 * v2) < 1e-9 and abs(v8 - 2 * v4) < 1e-9
    check(f"{name} doubles when INITIAL_ENTRY_USDT doubles", linear,
          f"$2->{v2:.4f} $4->{v4:.4f} $8->{v8:.4f}")

check("halving INITIAL_ENTRY_USDT halves the stop ceiling",
      abs(c2.MAX_STOP_LOSS_USD - 0.45) < 1e-9, f"got {c2.MAX_STOP_LOSS_USD}")
check("doubling it doubles the stop ceiling",
      abs(c8.MAX_STOP_LOSS_USD - 1.80) < 1e-9, f"got {c8.MAX_STOP_LOSS_USD}")

# ===========================================================================
print("\n[3] The orderflow band can never be born inside itself")
# ===========================================================================
# THE BUG THIS PREVENTS. A fresh position's fee-net PnL is exactly minus the
# round-trip fee. If the band FLOOR ever falls below that, every position is
# instantly inside the exit band with no price movement at all.
FEE_RATE = 0.000701          # measured from the live trade log
for entry in (1, 2, 4, 8, 16):
    cc = load_config(entry)
    fees = FEE_RATE * cc.ENTRY_NOTIONAL_USDT
    floor = cc.SMART_ORDERFLOW_EXIT_MIN_LOSS_USD
    check(f"at ${entry} entry (${cc.ENTRY_NOTIONAL_USDT:.0f} notional) the band floor "
          f"${floor:.4f} > round-trip fees ${fees:.4f}", floor > fees,
          f"floor {floor:.4f} <= fees {fees:.4f} - positions born inside the band")

c = load_config(4)
check("the band stays exactly 2x as wide as its floor",
      abs(c.SMART_ORDERFLOW_EXIT_MAX_LOSS_USD - 2 * c.SMART_ORDERFLOW_EXIT_MIN_LOSS_USD) < 1e-9)

# ===========================================================================
print("\n[4] Risk RATIOS are invariant to position size")
# ===========================================================================
# This is the whole point: the strategy's shape must not change with size.
def ratios(cc):
    n = cc.ENTRY_NOTIONAL_USDT
    return {
        "stop / notional": cc.MAX_STOP_LOSS_USD / n,
        "target / notional": cc.TARGET_PROFIT_USD / n,
        "reward:risk": cc.TARGET_PROFIT_USD / cc.MAX_STOP_LOSS_USD,
        "daily lock / per-trade budget": cc.MAX_DAILY_LOSS_USDT / cc.MAX_TRADE_NET_LOSS_USDT,
        "band width / notional": (cc.SMART_ORDERFLOW_EXIT_MAX_LOSS_USD
                                  - cc.SMART_ORDERFLOW_EXIT_MIN_LOSS_USD) / n,
    }

r2, r4, r8 = ratios(load_config(2)), ratios(load_config(4)), ratios(load_config(8))
for key in r4:
    same = abs(r2[key] - r4[key]) < 1e-9 and abs(r8[key] - r4[key]) < 1e-9
    check(f"'{key}' is identical at $2 / $4 / $8", same,
          f"{r2[key]:.6f} / {r4[key]:.6f} / {r8[key]:.6f}")
print(f"       reward:risk = {r4['reward:risk']:.2f} : 1 at every size")

# ===========================================================================
print("\n[5] An explicit env var still wins, and is REPORTED")
# ===========================================================================
c = load_config(4, overrides={"MAX_STOP_LOSS_USD": "0.33"})
check("an explicit override wins over the derived value",
      abs(c.MAX_STOP_LOSS_USD - 0.33) < 1e-9, f"got {c.MAX_STOP_LOSS_USD}")
report = dict((n, src) for n, _v, src in c.notional_scaling_report())
check("the override is flagged OVERRIDDEN in the report",
      report.get("MAX_STOP_LOSS_USD") == "OVERRIDDEN", f"got {report.get('MAX_STOP_LOSS_USD')}")
check("untouched thresholds are still flagged derived",
      report.get("TARGET_PROFIT_USD") == "derived")
check("the report covers every scaled threshold",
      set(report) == set(SCALED), f"missing {set(SCALED) - set(report)}")

# an overridden value does NOT scale - which is exactly why it is reported
c8 = load_config(8, overrides={"MAX_STOP_LOSS_USD": "0.33"})
check("an overridden threshold does NOT scale with INITIAL_ENTRY_USDT",
      abs(c8.MAX_STOP_LOSS_USD - 0.33) < 1e-9,
      "this is the hazard the startup warning exists for")

# unparseable override falls back to the derived value rather than crashing
c = load_config(4, overrides={"MAX_STOP_LOSS_USD": "not-a-number"})
check("an unparseable override falls back to the derived value",
      abs(c.MAX_STOP_LOSS_USD - 0.90) < 1e-9, f"got {c.MAX_STOP_LOSS_USD}")
c = load_config(4, overrides={"MAX_STOP_LOSS_USD": ""})
check("an empty override is treated as unset",
      abs(c.MAX_STOP_LOSS_USD - 0.90) < 1e-9, f"got {c.MAX_STOP_LOSS_USD}")

# ===========================================================================
print("\n[6] The absolute floor protects a very small account")
# ===========================================================================
tiny = load_config(0.5)          # $10 notional
check("SL_MIN_USD is floored at $0.05 on a tiny account",
      tiny.SL_MIN_USD == 0.05, f"got {tiny.SL_MIN_USD}")
big = load_config(4)
check("...but scales normally once notional is meaningful",
      abs(big.SL_MIN_USD - 0.12) < 1e-9, f"got {big.SL_MIN_USD}")

# ===========================================================================
print("\n[7] No hardcoded dollar default is left behind")
# ===========================================================================
src = open("config.py").read()
import re
for name in SCALED:
    pat = re.compile(rf'^{name} = float\(os\.environ\.get\(', re.M)
    check(f"{name} no longer reads a raw os.environ default",
          not pat.search(src), "still a hardcoded dollar default")

c = load_config(4)
check("MIN_STOP_LOSS_USD is documented as dead",
      "DEAD VALUE" in src and "MIN_STOP_LOSS_USD" in src)

# ===========================================================================
print("\n" + "=" * 74)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
