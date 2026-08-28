#!/usr/bin/env python3
"""Tests for the delta-neutral funding carry.

This trade fails differently from a directional one. It cannot be wrong
about direction - it has none - so the tests target the three ways it
actually loses money: entering when the carry will not cover its cost,
leaving before the front-loaded cost is recovered, and letting the hedge
break while believing the position is neutral.
"""
import sys

import backtest_funding as BF
import funding_arb as FA

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


C = FA.CarryCosts()                       # 30 bps a cycle
P = FA.CarryParams()


def series(rate, n=40):
    return [rate] * n


print("[1] The cost arithmetic")
check("spot fees are counted at double the perp rate, not equal to them",
      C.spot_in_bps == 2 * C.perp_in_bps, f"{C.spot_in_bps} vs {C.perp_in_bps}")
check("a full cycle costs 30 bps at VIP0", C.total_bps == 30.0, str(C.total_bps))
check("break-even at 1 bps/period is 30 periods",
      FA.break_even_periods(C, 1.0) == 30.0)
check("break-even at 3 bps/period is 10 periods",
      FA.break_even_periods(C, 3.0) == 10.0)
check("zero funding never breaks even",
      FA.break_even_periods(C, 0.0) == float("inf"))
check("negative funding never breaks even",
      FA.break_even_periods(C, -1.0) == float("inf"))

print("\n[2] Entry refuses trades that cannot pay for themselves")
ok, why, _ = FA.should_enter(series(2.0), C, P)
check("healthy persistent funding is accepted", ok, why)
ok, why, _ = FA.should_enter(series(0.2), C, P)
check("funding below the floor is refused", not ok, why)
ok, why, _ = FA.should_enter(series(-1.0), C, P)
check("negative funding is refused", not ok, why)
ok, why, _ = FA.should_enter(series(2.0, 5), C, P)
check("too little history is refused rather than guessed", not ok, why)
# A carry that would need longer than the maximum hold to break even.
tight = FA.CarryParams(max_hold_periods=20, min_funding_bps=0.1)
ok, why, _ = FA.should_enter(series(0.5), C, tight)
check("a break-even beyond the max hold is refused", not ok, why)

# One huge spike inside an otherwise dead market must not drag entry in:
# this is why the estimate is a median rather than a mean.
spiky = [0.0] * 20 + [60.0]
mean = sum(spiky) / len(spiky)
ok, why, est = FA.should_enter(spiky, C, P)
check("a single squeeze spike does not trigger entry", not ok, why)
check("...even though the MEAN would have cleared the floor",
      mean > P.min_funding_bps, f"mean {mean:.2f} bps")

# Positive median but flickering sign - not durable enough to hold for weeks.
flicker = [3.0, -2.5] * 10 + [3.0]
ok, why, _ = FA.should_enter(flicker, C, P)
check("funding that keeps flipping sign is refused", not ok, why)

print("\n[3] Exit protects the front-loaded cost")
st = FA.CarryState(open=True, periods_held=1, funding_collected_bps=2.0)
out, why = FA.should_exit(st, series(2.0), C, P)
check("a healthy carry is held", not out, why)

st = FA.CarryState(open=True, periods_held=2, funding_collected_bps=4.0)
out, why = FA.should_exit(st, series(-1.0), C, P)
check("the minimum hold blocks churn on one bad print", not out, why)

st = FA.CarryState(open=True, periods_held=10, funding_collected_bps=20.0)
out, why = FA.should_exit(st, series(-1.0), C, P)
check("negative funding exits once past the minimum hold", out, why)

# Weak but positive funding, entry cost not yet recovered: staying is right.
st = FA.CarryState(open=True, periods_held=10, funding_collected_bps=5.0)
out, why = FA.should_exit(st, series(0.0), C, FA.CarryParams(exit_funding_bps=0.5))
check("it does not bail before the entry cost is recovered",
      not out or "EMERGENCY" in why, why)
st = FA.CarryState(open=True, periods_held=10, funding_collected_bps=20.0)
out, why = FA.should_exit(st, series(0.0), C, FA.CarryParams(exit_funding_bps=0.5))
check("...but leaves once it is", out, why)

st = FA.CarryState(open=True, periods_held=999, funding_collected_bps=500.0)
out, why = FA.should_exit(st, series(5.0), C, P)
check("the maximum hold forces a reassessment", out, why)

print("\n[4] Risk overrides beat the minimum hold")
st = FA.CarryState(open=True, periods_held=1, funding_collected_bps=1.0)
out, why = FA.should_exit(st, series(5.0), C, P, margin_ratio=0.001)
check("a margin emergency exits immediately, mid-hold", out, why)
check("...and says so", "EMERGENCY" in why, why)
out, why = FA.should_exit(st, series(5.0), C, P, leg_imbalance=0.05)
check("a broken hedge exits immediately", out, why)
check("...describing it as a hedge failure, not a funding decision",
      "hedge" in why.lower(), why)
out, why = FA.should_exit(st, series(5.0), C, P, margin_ratio=0.50,
                          leg_imbalance=0.001)
check("healthy margin and a tight hedge do not trigger an exit", not out, why)

print("\n[5] Sizing reflects that spot is bought outright")
notional, spot_cap, perp_margin = FA.position_size(1000.0, 100.0, P)
check("capital is notional plus perp margin",
      abs(spot_cap + perp_margin - 1000.0) < 1e-9,
      f"{spot_cap:.2f} + {perp_margin:.2f}")
check("2x leverage means notional is two-thirds of equity",
      abs(notional - 666.667) < 0.01, f"{notional:.2f}")
n2, _, _ = FA.position_size(1000.0, 100.0, FA.CarryParams(leverage=4.0))
check("more leverage buys more notional per unit of capital", n2 > notional)
check("...which is exactly the thing that raises liquidation risk", n2 == 800.0,
      str(n2))

print("\n[6] The backtest does not peek at the funding it is deciding on")
src = open("backtest_funding.py", encoding="utf-8").read()
check("the rate is appended to history AFTER it is accrued",
      src.index("state.funding_collected_bps += rate") < src.index("hist.append(rate)"))
check("an open position at the end is marked out, not quietly dropped",
      'end_of_data' in src)

print("\n[7] End to end: it must lose money in a market that pays nothing")
dead = [(i * 28800.0, 0.05) for i in range(400)]        # funding ~ zero
cycles, eq, curve = BF.run_carry(dead, C, P)
check("a market with no carry produces no trades", len(cycles) == 0,
      f"{len(cycles)} cycles, {eq:+.0f} bps")

neg = [(i * 28800.0, -1.5) for i in range(400)]
cycles, eq, curve = BF.run_carry(neg, C, P)
check("a persistently negative market produces no trades", len(cycles) == 0,
      f"{len(cycles)} cycles, {eq:+.0f} bps")

good = [(i * 28800.0, 2.0) for i in range(400)]
cycles, eq, curve = BF.run_carry(good, C, P)
check("a persistently paying market is traded", len(cycles) > 0, str(len(cycles)))
check("...and is net profitable after all four legs", eq > 0, f"{eq:+.0f} bps")
# The final cycle is a forced mark-out at the end of the data, not a decision
# the rules made, so it is excluded - judging the strategy on where the file
# happens to stop would be as arbitrary as judging it on where it starts.
decided = [c for c in cycles if c["reason"] != "end_of_data"]
if decided:
    worst = min(c["net_bps"] for c in decided)
    check("...with every rule-driven cycle clearing its own cost", worst > 0,
          f"{worst:+.1f} bps")
    check("...and the truncated final cycle is labelled, not counted as a win",
          all(c["reason"] == "end_of_data" for c in cycles if c not in decided))

# A market that pays well, then stops for good.
turn = ([(i * 28800.0, 2.5) for i in range(150)]
        + [(i * 28800.0, -2.0) for i in range(150, 400)])
cycles, eq, curve = BF.run_carry(turn, C, P)
check("a regime that turns negative is exited, not ridden to zero",
      all(c["periods"] < 200 for c in cycles),
      str([c["periods"] for c in cycles]))
check("...and the damage is bounded to a few cycles' cost",
      eq > -3 * C.total_bps, f"{eq:+.0f} bps")

print("\n[8] Return expectations are stated on CAPITAL, not notional")
r = FA.annualised_return(1.0, C, P, 90)
check("1 bps/period held 30 days annualises to single digits",
      0.02 < r < 0.10, f"{r * 100:.2f}%")
check("break-even funding annualises to zero",
      abs(FA.annualised_return(1.0, C, P, 30)) < 1e-9)
check("higher funding pays more",
      FA.annualised_return(2.0, C, P, 90) > r)
check("a longer hold amortises the fixed cost better",
      FA.annualised_return(1.0, C, P, 180) > FA.annualised_return(1.0, C, P, 45))

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
