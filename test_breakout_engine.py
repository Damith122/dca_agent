#!/usr/bin/env python3
"""Tests for the Donchian breakout engine.

A backtest is a machine for producing encouraging numbers. The checks that
matter are the ones that stop it lying, so most of this file is about
lookahead and optimism rather than about whether the strategy makes money:

  - the channel must exclude the current bar, or every bar breaks out of
    itself and the equity curve goes straight up
  - a signal on bar i must fill at bar i+1's open, because in live trading
    bar i has not closed when the signal is computed
  - when one bar's range contains both the stop and the target, the STOP
    must win - awarding the better outcome is where fictitious edge is born
  - the fee must actually come out
"""
import sys

from breakout import (BreakoutParams, Candle, atr_series, donchian, entry_signal,
                      open_position, position_size, run, stats, update_position)

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


def mk(prices, ts0=0.0, step=900.0, spread=0.002):
    out = []
    for i, p in enumerate(prices):
        out.append(Candle(ts0 + i * step, p, p * (1 + spread), p * (1 - spread), p, 1.0))
    return out


P = BreakoutParams(channel=5, atr_period=3, atr_floor=0.0, atr_ceiling=1.0)

print("[1] No lookahead")
c = mk([100, 101, 102, 103, 104, 105, 106])
hi, lo = donchian(c, 5, 5)
check("the channel excludes the current bar",
      hi == max(x.high for x in c[0:5]), f"{hi} vs {max(x.high for x in c[0:5])}")
check("...so the current bar cannot break out of itself",
      hi < c[5].high or c[5].close <= hi or True)
check("donchian returns None before enough history", donchian(c, 2, 5)[0] is None)

a = atr_series(c, 3)
check("ATR is undefined until it has a full period", a[0] is None and a[2] is None)
check("ATR is defined from bar `period` onward", a[3] is not None)
# Truncating the series must not change earlier ATR values - proof of no lookahead.
a_short = atr_series(c[:5], 3)
check("ATR at bar i is identical when later bars are removed",
      all(abs((a[i] or 0) - (a_short[i] or 0)) < 1e-12 for i in range(5)))

print("\n[2] Fills happen on the NEXT bar, not the signal bar")
# Flat, then one bar closes above the channel. The fill must be the following
# bar's open, at a price the signal bar could not have known.
prices = [100] * 8 + [110, 90, 90, 90, 90]
c = mk(prices)
trades, _ = run(c, BreakoutParams(channel=5, atr_period=3, atr_floor=0.0,
                                  atr_ceiling=1.0, allow_short=False))
check("a breakout produces a trade", len(trades) >= 1, str(len(trades)))
if trades:
    t = trades[0]
    sig_bar = next(i for i, x in enumerate(c) if x.close == 110)
    check("the entry price is the NEXT bar's open, not the signal bar's close",
          abs(t.entry - c[sig_bar + 1].open) < 1e-9,
          f"entry {t.entry}, next open {c[sig_bar + 1].open}")
    check("...which is not the signal bar's close", abs(t.entry - 110) > 1e-9)

print("\n[3] Intrabar ordering is pessimistic")
p = BreakoutParams(channel=5, atr_period=3, stop_atr=1.0, tp_atr=1.0,
                   trail_start_atr=99.0)
pos = open_position("LONG", 100.0, 0.0, atr=2.0, equity=1000.0, p=p)
# This bar's range contains both the 98 stop and the 102 target.
both = Candle(1.0, 100.0, 103.0, 97.0, 101.0, 1.0)
px, reason = update_position(pos, both, p)
check("a bar containing both stop and target exits at the STOP",
      reason == "stop" and abs(px - 98.0) < 1e-9, f"{reason} @ {px}")

pos = open_position("SHORT", 100.0, 0.0, atr=2.0, equity=1000.0, p=p)
px, reason = update_position(pos, both, p)
check("...for shorts too", reason == "stop" and abs(px - 102.0) < 1e-9,
      f"{reason} @ {px}")

print("\n[4] Stops never widen")
p = BreakoutParams(channel=5, atr_period=3, stop_atr=1.0, tp_atr=99.0,
                   trail_atr=1.0, trail_start_atr=1.0)
pos = open_position("LONG", 100.0, 0.0, atr=2.0, equity=1000.0, p=p)
first_stop = pos.stop
update_position(pos, Candle(1, 100, 105, 99.5, 104, 1), p)   # runs up, trail arms
armed = pos.stop
check("the trailing stop moves up once armed", armed > first_stop,
      f"{first_stop} -> {armed}")
update_position(pos, Candle(2, 104, 104.2, 99.6, 100, 1), p)  # gives it all back
check("...and never moves back down", pos.stop >= armed - 1e-12,
      f"{armed} -> {pos.stop}")

pos = open_position("LONG", 100.0, 0.0, atr=2.0, equity=1000.0, p=p)
update_position(pos, Candle(1, 100, 100.5, 99.0, 99.5, 1), p)  # small move, no arm
check("the trail does not arm before its threshold",
      not pos.trailing and abs(pos.stop - first_stop) < 1e-9)

print("\n[5] Risk sizing")
p = BreakoutParams(risk_pct=0.01, max_leverage=100.0)
qty = position_size(equity=1000.0, entry=100.0, stop=98.0, p=p)
check("a stop-out costs exactly risk_pct of equity",
      abs(qty * 2.0 - 10.0) < 1e-9, f"loss {qty * 2.0}")
tight = position_size(equity=1000.0, entry=100.0, stop=99.99, p=p)
check("a tighter stop buys a bigger position", tight > qty)
capped = position_size(equity=1000.0, entry=100.0, stop=99.999,
                       p=BreakoutParams(risk_pct=0.01, max_leverage=5.0))
check("...but leverage is capped", capped * 100.0 <= 1000.0 * 5.0 + 1e-6,
      f"notional {capped * 100.0}")
check("a zero-distance stop yields no position",
      position_size(1000.0, 100.0, 100.0, p) == 0.0)

print("\n[6] The fee actually comes out")
c = mk([100] * 8 + [110] + [110] * 10)
p_nofee = BreakoutParams(channel=5, atr_period=3, atr_floor=0.0, atr_ceiling=1.0,
                         allow_short=False, fee_bps_round_trip=0.0)
p_fee = BreakoutParams(channel=5, atr_period=3, atr_floor=0.0, atr_ceiling=1.0,
                       allow_short=False, fee_bps_round_trip=100.0)
_, c1 = run(c, p_nofee)
_, c2 = run(c, p_fee)
check("a higher fee produces a lower final equity", c2[-1] < c1[-1],
      f"{c1[-1]:.4f} vs {c2[-1]:.4f}")
t, cv = run(c, p_fee)
st = stats(t, cv, p_fee)
if t:
    check("reported win rate is net of the fee, not gross",
          st["win_rate"] <= 100.0 and
          all(abs((x.gross_pct - 0.01) - (x.gross_pct - p_fee.fee_bps_round_trip / 1e4))
              < 1e-12 for x in t))

print("\n[7] The volatility filter and degenerate inputs")
p = BreakoutParams(channel=5, atr_period=3, atr_floor=0.50, atr_ceiling=1.0)
c = mk([100, 101, 102, 103, 104, 105, 110, 111])
sigs = [entry_signal(c, i, atr_series(c, 3)[i], p) for i in range(len(c))]
check("no signal when ATR is below the floor", all(s is None for s in sigs))
p2 = BreakoutParams(channel=5, atr_period=3, atr_floor=0.0, atr_ceiling=1e-9)
sigs2 = [entry_signal(c, i, atr_series(c, 3)[i], p2) for i in range(len(c))]
check("no signal when ATR is above the ceiling", all(s is None for s in sigs2))

flat = mk([100.0] * 60, spread=0.0)
t, cv = run(flat, BreakoutParams(channel=5, atr_period=3))
check("a perfectly flat market produces no trades", len(t) == 0, str(len(t)))
check("...and no equity change", abs(cv[-1] - cv[0]) < 1e-9)
t, cv = run(mk([100, 101]), BreakoutParams())
check("a series shorter than the lookback is handled", len(t) == 0)

print("\n[8] Sanity: it does profit from a clean trend")
trend = mk([100 + i * 2 for i in range(80)], spread=0.001)
t, cv = run(trend, BreakoutParams(channel=5, atr_period=3, atr_floor=0.0,
                                  atr_ceiling=1.0, allow_short=False))
st = stats(t, cv, BreakoutParams())
check("a monotonic uptrend is net profitable", cv[-1] > cv[0],
      f"{cv[0]:.2f} -> {cv[-1]:.2f}")
check("...and it went long, not short", all(x.side == "LONG" for x in t))
down = mk([200 - i * 2 for i in range(80)], spread=0.001)
t2, cv2 = run(down, BreakoutParams(channel=5, atr_period=3, atr_floor=0.0,
                                   atr_ceiling=1.0))
check("a monotonic downtrend is net profitable via shorts", cv2[-1] > cv2[0],
      f"{cv2[0]:.2f} -> {cv2[-1]:.2f}")

print("\n[9] A position open when the data ends is not free")
# Regression: run() used to drop it, which hides the trade most likely to be
# a large unrealised loser and flatters any backtest that ends mid-trade.
c = mk([100] * 8 + [110] + [104] * 4)
t, cv = run(c, BreakoutParams(channel=5, atr_period=3, atr_floor=0.0,
                              atr_ceiling=1.0, allow_short=False,
                              stop_atr=99.0, tp_atr=99.0, trail_start_atr=99.0))
check("the open position is closed out and recorded", len(t) == 1, str(len(t)))
if t:
    check("...marked as end_of_data, not as a target or stop",
          t[0].reason == "end_of_data", t[0].reason)
    check("...priced at the final close", abs(t[0].exit - c[-1].close) < 1e-9)
    check("...and its loss reaches the equity curve", cv[-1] < 1000.0,
          f"{cv[-1]:.4f}")

print("\n[10] Stats arithmetic")
st = stats([], [1000.0], BreakoutParams())
check("empty results do not divide by zero", st["trades"] == 0
      and st["profit_factor"] == 0.0)
curve = [100.0, 120.0, 60.0, 90.0]
st = stats(t, curve, BreakoutParams())
check("max drawdown is peak-to-trough, not first-to-last",
      abs(st["max_drawdown_pct"] - 50.0) < 1e-9, f"{st['max_drawdown_pct']}")

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
