"""Offline regression tests for the 2026-08-21 Profit Lock slippage correction.

THE DEFECT
--------------------------------------------------------------------------
P2 (2026-08-19) added a volatility-aware floor so Profit Lock would not close
at a level slippage turns into a realized loss:

    floor = max(MIN_NET_PROFIT_USDT,
                PROFIT_LOCK_SLIPPAGE_ATR_MULT x atr_pct x notional)

But the estimator it guards, estimate_net_pnl_usdt_executable(), ALREADY
prices the exit at the executable side of the book - best_bid to close a
LONG, best_ask to close a SHORT - and already uses the actual accrued
commission. The spread is priced in BEFORE the buffer is applied, so
0.5 x ATR on top charged for it a second time.

Production evidence (NEAR, previous deployment):

    [profit-lock] HOLDING - executable net $+0.0572 is at/below the locked
    level $+0.0706 but under the vol-aware fee-safe floor $0.0643
    (atr%=0.336) ... "closing here would likely realize a loss after slippage"

That trade went on to close at exactly +$0.0572 (session_pnl reconciliation:
-0.1598 + 0.1040 + X = +0.0014). The slippage never materialised. At that
notional a FULL spread was $0.021 against a $0.064 buffer - about 3x.

THE STRUCTURAL CONSEQUENCE
--------------------------------------------------------------------------
Profit Lock can only close when locked_profit >= slippage_floor. Since
locked_profit is peak x PROFIT_LOCK_RATIO, that is a condition on the PEAK:

    peak >= (SLIPPAGE_MULT / LOCK_RATIO) x atr_pct x notional

At 0.5/0.5 the peak had to exceed a FULL ATR-of-notional before the lock
could act. Both sides scale with notional, so this is a RATIO problem -
raising position size does not fix it.

THE FIX
--------------------------------------------------------------------------
  1. PROFIT_LOCK_SLIPPAGE_ATR_MULT 0.5 -> 0.25. On the NEAR numbers that
     widens the trigger window from $0.0063 to $0.0205 - 3.3x. This is a
     CALIBRATION choice, not a clean bug fix: only ~0.43+ still blocks the
     2026-08-19 15:28 close that P2 was built for, so any meaningful
     reduction re-opens it. See config.py for why 0.25 is defensible now
     (P1's executable estimator, the tick throttle, the 50s min-hold).
  2. The HOLDING log now distinguishes "not yet firing" from "CANNOT fire at
     this peak", and reports the peak required. The old message gave no way
     to tell those apart.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_profit_lock_slippage_ratio_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import asyncio
import io
import sys
import time

import config
import trading
import dca2 as bot

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -> ' + detail) if detail and not cond else ''}")


class Capture:
    def __enter__(self):
        self._buf = io.StringIO()
        self._stdout = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *exc):
        sys.stdout = self._stdout

    @property
    def text(self):
        return self._buf.getvalue()


def make_manager():
    f = bot.SymbolFilters(tick_size=0.001, step_size=1.0, min_qty=1.0, min_notional=5.0)
    return bot.MartingaleManager(client=None, symbol="NEARUSDT", filters=f, leverage=20)


def near_position(peak, net, atr_pct=0.00336, qty=21.0, entry=1.822):
    """The live NEAR trade, reconstructed at the profit-lock decision."""
    m = make_manager()
    p = m.position
    p.status = "OPEN"
    p.side = "LONG"
    p.total_qty = qty
    p.original_qty = qty
    p.avg_entry_price = entry
    p.entries = [(entry, qty)]
    p.opened_at = time.time() - 600
    p.profit_lock_active = True
    p.peak_unrealized_pnl = peak
    m.best_bid_price, m.best_ask_price = entry, entry + 0.001
    m.current_price = entry
    m._position_fees_accum = 0.0192
    m._position_fees_reliable = True
    m.position_sync_ready = True
    m.last_regime = trading.RegimeReading(
        regime=trading.REGIME_WEAK_TREND, atr_pct=atr_pct, atr_ratio=1.0,
    )
    m.estimate_net_pnl_usdt_executable = lambda: net
    m.estimate_net_pnl_usdt = lambda *a, **k: net
    m.closed = []

    async def fake_close(reason, **kw):
        m.closed.append({"reason": reason, "tag": kw.get("exit_reason_tag")})
        m.position.status = "CLOSING"

    m.close_position = fake_close
    return m


async def run(m):
    try:
        await m._manage_open_position()
    except Exception:
        pass


# ===========================================================================
print("\n[1] The corrected constant and the geometry it implies")
# ===========================================================================
check("PROFIT_LOCK_SLIPPAGE_ATR_MULT is 0.25",
      config.PROFIT_LOCK_SLIPPAGE_ATR_MULT == 0.25,
      f"got {config.PROFIT_LOCK_SLIPPAGE_ATR_MULT}")
check("PROFIT_LOCK_RATIO is unchanged at 0.50",
      config.PROFIT_LOCK_RATIO == 0.50, f"got {config.PROFIT_LOCK_RATIO}")
ratio = config.PROFIT_LOCK_SLIPPAGE_ATR_MULT / config.PROFIT_LOCK_RATIO
check("the peak requirement drops from 1.00 to 0.50 ATR-of-notional",
      abs(ratio - 0.50) < 1e-9, f"got {ratio:.3f}")
check("it is still a POSITIVE buffer, not disabled",
      config.PROFIT_LOCK_SLIPPAGE_ATR_MULT > 0)
check("MIN_NET_PROFIT_USDT still underpins it as an absolute floor",
      config.MIN_NET_PROFIT_USDT > 0)

# ===========================================================================
print("\n[2] The floor itself, on the exact live numbers")
# ===========================================================================
m = near_position(peak=0.1411, net=0.0572)
floor = m._profit_lock_min_net_floor()
notional = m._position_notional_usdt()
old_floor = max(config.MIN_NET_PROFIT_USDT, 0.5 * 0.00336 * notional)
print(f"       notional=${notional:.2f}  atr=0.336%")
print(f"       old floor (0.5 ATR) = ${old_floor:.4f}   new floor = ${floor:.4f}")
check("the new floor is materially lower than the old one", floor < old_floor,
      f"{floor:.4f} vs {old_floor:.4f}")
check("the new floor is below the $0.0572 the trade actually realized",
      floor <= 0.0572, f"floor=${floor:.4f}")
check("the OLD floor was above it (which is why the exit was blocked)",
      old_floor > 0.0572, f"old=${old_floor:.4f}")

# ===========================================================================
print("\n[3] The exact production scenario now closes")
# ===========================================================================
m = near_position(peak=0.1411, net=0.0572)
asyncio.run(run(m))
locked = [c for c in m.closed if c["tag"] == "profit_lock"]
check("the live NEAR scenario now fires Profit Lock", len(locked) == 1,
      f"closed={m.closed}")
check("the close reason still reports the locked level and the floor",
      locked and "PROFIT LOCK" in locked[0]["reason"] and "floor" in locked[0]["reason"])

# ===========================================================================
print("\n[4] The buffer still BLOCKS a genuinely too-thin exit")
# ===========================================================================
# The fix must not simply switch the guard off. A net barely above zero, in a
# violent tape, must still be refused.
m = near_position(peak=0.30, net=0.004, atr_pct=0.010)
asyncio.run(run(m))
check("a $0.004 net in a 1.0% ATR tape is still refused",
      not [c for c in m.closed if c["tag"] == "profit_lock"], f"{m.closed}")

m = near_position(peak=0.30, net=0.02, atr_pct=0.00336)
asyncio.run(run(m))
check("a net below MIN_NET_PROFIT_USDT is still refused",
      not [c for c in m.closed if c["tag"] == "profit_lock"], f"{m.closed}")

# a NEGATIVE net must never close via profit lock
m = near_position(peak=0.30, net=-0.05)
asyncio.run(run(m))
check("a negative net never closes via Profit Lock",
      not [c for c in m.closed if c["tag"] == "profit_lock"])

# ===========================================================================
print("\n[5] The lock still only fires when price has RETRACED to the level")
# ===========================================================================
m = near_position(peak=0.1411, net=0.1400)     # still near the peak
asyncio.run(run(m))
check("a position still near its peak does NOT close",
      not [c for c in m.closed if c["tag"] == "profit_lock"],
      "profit lock fired while price was still at the peak")

m = near_position(peak=0.1411, net=0.0700)     # retraced below locked 0.0706
asyncio.run(run(m))
check("a position retraced below the locked level DOES close",
      len([c for c in m.closed if c["tag"] == "profit_lock"]) == 1, f"{m.closed}")

# ===========================================================================
print("\n[6] Which term binds, and what that means for position size")
# ===========================================================================
# floor = max(MIN_NET_PROFIT_USDT, SLIPPAGE_MULT x atr x notional), so there
# are two regimes and they behave differently under a size change.
#
#   ATR-dominated (high volatility): both the locked level and the floor
#     scale with notional, so the peak requirement in ATR terms is INVARIANT
#     to size. This is the regime the old 0.5 multiplier sat in at typical
#     volatility, which is why raising position size could not have fixed it.
#
#   Flat-floor-dominated (typical volatility, post-fix): MIN_NET_PROFIT_USDT
#     does NOT scale, so the requirement in ATR terms FALLS as notional rises.
HIGH_ATR = 0.015          # ATR term dominates at both sizes
reqs = {}
for qty, n_label in ((21.0, "$38"), (42.0, "$76")):
    m = near_position(peak=0.10, net=0.06, qty=qty, atr_pct=HIGH_ATR)
    n = m._position_notional_usdt()
    fl = m._profit_lock_min_net_floor()
    reqs[n_label] = (fl / config.PROFIT_LOCK_RATIO) / (HIGH_ATR * n)
    print(f"       {n_label:4} notional @ {HIGH_ATR*100:.1f}% ATR -> floor=${fl:.4f}, "
          f"required peak = {reqs[n_label]:.2f} ATR-of-notional")
check("in the ATR-dominated regime the requirement is INVARIANT to size",
      abs(reqs["$38"] - reqs["$76"]) < 1e-9, f"{reqs}")
check("...and that requirement is now 0.50, not 1.00",
      abs(reqs["$38"] - 0.50) < 1e-9, f"got {reqs['$38']:.4f}")

# At typical volatility the flat floor takes over - the designed fallback.
TYP_ATR = 0.00336
m_small = near_position(peak=0.10, net=0.06, qty=21.0, atr_pct=TYP_ATR)
m_big = near_position(peak=0.10, net=0.06, qty=42.0, atr_pct=TYP_ATR)
# At 0.25 the crossover between the two regimes sits between the two sizes:
# $38 notional at typical ATR is still flat-floor-bound, $76 is already
# ATR-bound. Assert what actually happens rather than a false generalisation.
check("at $38 notional / typical ATR the flat MIN_NET_PROFIT_USDT floor binds",
      m_small._profit_lock_min_net_floor() == config.MIN_NET_PROFIT_USDT,
      f"got {m_small._profit_lock_min_net_floor():.4f}")
check("at $76 notional / typical ATR the ATR term has taken over",
      m_big._profit_lock_min_net_floor() > config.MIN_NET_PROFIT_USDT,
      f"got {m_big._profit_lock_min_net_floor():.4f}")
r_small = (m_small._profit_lock_min_net_floor() / config.PROFIT_LOCK_RATIO) / (
    TYP_ATR * m_small._position_notional_usdt())
r_big = (m_big._profit_lock_min_net_floor() / config.PROFIT_LOCK_RATIO) / (
    TYP_ATR * m_big._position_notional_usdt())
print(f"       at {TYP_ATR*100:.2f}% ATR: $38 -> {r_small:.2f} ATRs, $76 -> {r_big:.2f} ATRs")
check("...so a larger notional lowers the requirement while the flat floor "
      "still has any influence", r_big <= r_small, f"{r_small:.3f} vs {r_big:.3f}")
check("both regimes are strictly better than the old 1.00 requirement",
      r_small < 1.0 and r_big < 1.0 and reqs["$38"] < 1.0)

# ===========================================================================
print("\n[7] The unfirable state is now named in the log")
# ===========================================================================
# Force the structurally-unfirable geometry: a peak so small that half of it
# sits below the floor.
m = near_position(peak=0.02, net=0.001, atr_pct=0.010)
m._should_log_profit_lock_peak_update = lambda *a, **k: True
with Capture() as cap:
    asyncio.run(run(m))
text = cap.text
check("an unfirable lock says so explicitly",
      "UNFIRABLE AT THIS PEAK" in text, text[:400] or "(no output)")
check("...and reports the peak that would be required",
      "until the peak exceeds" in text, text[:400])
check("...and confirms the risk exits are still live",
      "Hard Stop" in text and "RR stop" in text, text[:400])
check("...and it does NOT close the position",
      not [c for c in m.closed if c["tag"] == "profit_lock"])

# an ordinary hold (firable, just not retraced far enough) keeps the old wording
m = near_position(peak=0.40, net=0.06, atr_pct=0.00336)
m._should_log_profit_lock_peak_update = lambda *a, **k: True
with Capture() as cap:
    asyncio.run(run(m))
check("a firable lock does not use the UNFIRABLE wording",
      "UNFIRABLE AT THIS PEAK" not in cap.text)

# ===========================================================================
print("\n[8] Everything else about Profit Lock is untouched")
# ===========================================================================
import inspect
src = inspect.getsource(trading.MartingaleManager._manage_open_position)
check("activation still uses PROFIT_LOCK_ACTIVATION_USDT",
      "PROFIT_LOCK_ACTIVATION_USDT" in src)
check("the locked level is still peak x PROFIT_LOCK_RATIO",
      "p.peak_unrealized_pnl * PROFIT_LOCK_RATIO" in src)
check("peak tracking still uses max()",
      "max(p.peak_unrealized_pnl" in src)
check("the floor helper is still the single source of the buffer",
      src.count("_profit_lock_min_net_floor()") == 1)

fsrc = inspect.getsource(trading.MartingaleManager._profit_lock_min_net_floor)
check("the floor still falls back to the flat floor when ATR is unavailable",
      "MIN_NET_PROFIT_USDT" in fsrc and "atr_pct > 0" in fsrc)
m = near_position(peak=0.1411, net=0.0572, atr_pct=0.0)
check("atr_pct=0 (warm-up) falls back to MIN_NET_PROFIT_USDT exactly",
      m._profit_lock_min_net_floor() == config.MIN_NET_PROFIT_USDT,
      f"got {m._profit_lock_min_net_floor()}")

# ===========================================================================
print("\n" + "=" * 74)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
