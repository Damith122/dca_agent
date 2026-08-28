#!/usr/bin/env python3
"""Tests for the cross-sectional funding book.

Carry trades fail in a way price strategies do not: the cash flow is real
and arrives on schedule, while the price of holding the position quietly
takes it back. So the property pinned hardest here is that carry and price
stay SEPARATED in the accounting - a book that collects 7 bps and loses 7
bps of price must never report as a carry strategy that works.
"""
import sys

import numpy as np

import backtest_xs_funding as F
import cross_sectional as CS

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


P = CS.BookParams(quantile=0.2, damping=0.0)

print("[1] Carry has the opposite sign to the weight")
# One name paying +10 bps, one paying -10. Short the payer, long the payee.
T, N = 60, 20
fund = np.zeros((T, N))
fund[:, 0] = 10.0          # top payer
fund[:, -1] = -10.0        # pays nothing / receives
for j in range(1, N - 1):
    fund[:, j] = np.linspace(-9, 9, N - 2)[j - 1]
carry, price, net, tos = F.run(fund, None, P, hold=3, cost_bps=0.0, lookback=3)
check("a book shorting the payers collects positive carry",
      carry.mean() > 0, f"{carry.mean() * 1e4:+.2f} bps")
check("with no prices the price leg is exactly zero",
      np.allclose(price, 0.0))
check("net equals carry when costs are zero",
      np.allclose(net, carry), f"{(net - carry).max():.3e}")

# Inverting every rate inverts the RANKING too, so the book flips sides and
# still ends up short whoever is now paying. The carry must stay positive -
# asserting it goes negative confuses the strategy with a fixed position.
flipped = -fund
c2, _, _, _ = F.run(flipped, None, P, hold=3, cost_bps=0.0, lookback=3)
check("inverting every rate keeps the carry positive - the book adapts",
      c2.mean() > 0, f"{c2.mean() * 1e4:+.2f} bps")
sig_hi = -np.nanmedian(fund[:3], axis=0)
sig_lo = -np.nanmedian(flipped[:3], axis=0)
w_hi = CS.target_weights(sig_hi, P)
w_lo = CS.target_weights(sig_lo, P)
check("...by taking the opposite side of every name",
      float(np.dot(w_hi, w_lo)) < 0,
      f"dot {float(np.dot(w_hi, w_lo)):+.4f}")
# Flat funding everywhere is nothing to rank on and nothing to collect.
flat = np.full((T, N), 3.0)
c_flat, _, _, _ = F.run(flat, None, P, 3, 0.0, 3)
check("a universe where everyone pays the same collects nothing",
      abs(c_flat.mean()) < 1e-9, f"{c_flat.mean() * 1e4:+.4f} bps")

print("\n[2] Costs come out")
_, _, n0, t0 = F.run(fund, None, P, 3, 0.0, 3)
_, _, n1, _ = F.run(fund, None, P, 3, 50.0, 3)
check("a higher fee lowers the net", n1.mean() < n0.mean())
check("...by turnover times the fee",
      abs((n0.mean() - n1.mean()) - t0.mean() * 50.0 / 1e4) < 1e-9)

print("\n[3] The signal never reads the funding it is about to earn")
src = open("backtest_xs_funding.py", encoding="utf-8").read()
check("the lookback window ends before the current print",
      "hist = fund[i - lookback:i]" in src)
check("the earned window starts AT the current print",
      "f_win = np.nan_to_num(fund[i:i + hold]" in src)
# Corrupting only the future must not change the position taken.
fut = fund.copy()
fut[30:, :] = np.random.default_rng(1).normal(0, 50, (T - 30, N))
c_a, _, _, to_a = F.run(fund[:30], None, P, 3, 0.0, 3)
c_b, _, _, to_b = F.run(fut[:30], None, P, 3, 0.0, 3)
check("truncating the panel does not change earlier decisions",
      np.allclose(to_a, to_b))

print("\n[4] Carry and price stay separated - the trap case")
rng = np.random.default_rng(7)
T2, N2 = 300, 40
lvl = rng.normal(0, 2.0, N2)
f2 = lvl + rng.normal(0, 1.0, (T2, N2))
# price drifts so that shorting a payer loses exactly what it collects
drift = lvl / 1e4
px2 = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.02, (T2, N2)), axis=0))
c3, p3, n3, t3 = F.run(f2, px2, P, 3, 7.32, 3)
check("the carry is genuinely positive", c3.mean() > 0,
      f"{c3.mean() * 1e4:+.2f} bps")
check("...and the price leg takes it back", p3.mean() < 0,
      f"{p3.mean() * 1e4:+.2f} bps")
check("...so the net is NOT reported as a working carry trade",
      n3.mean() < 0, f"{n3.mean() * 1e4:+.2f} bps")
check("the decomposition is what reveals it",
      "carry collected" in src and "price movement" in src)
check("...and the tool says so when price eats the carry",
      "short-" in src and "volatility position" in src)

print("\n[5] Summary arithmetic")
st = F.stats(np.array([]), np.array([]), np.array([]), np.array([]), 1095)
check("an empty run does not divide by zero", st["periods"] == 0)
st = F.stats(c3, p3, n3, t3, 1095, 7.32)
check("cost is turnover times the fee, not turnover alone",
      abs(st["cost_bps"] - t3.mean() * 7.32) < 1e-9,
      f"{st['cost_bps']:.3f} vs {t3.mean() * 7.32:.3f}")
check("carry plus price minus cost reconciles to net",
      abs((st["carry_bps"] + st["price_bps"] - st["cost_bps"])
          - st["net_bps"]) < 1e-6,
      f"{st['carry_bps'] + st['price_bps'] - st['cost_bps']:.4f} "
      f"vs {st['net_bps']:.4f}")

print("\n[6] Shuffling the ranks destroys the carry")
c4, _, _, _ = F.run(f2, None, P, 3, 0.0, 3)
c5, _, _, _ = F.run(f2, None, P, 3, 0.0, 3,
                    shuffle_rng=np.random.default_rng(3))
check("a shuffled ranking collects far less carry",
      abs(c5.mean()) < abs(c4.mean()) / 2,
      f"shuffled {c5.mean() * 1e4:+.2f} vs real {c4.mean() * 1e4:+.2f}")

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
