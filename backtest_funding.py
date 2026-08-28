#!/usr/bin/env python3
"""Fetch funding history and backtest the delta-neutral carry.

    python3 backtest_funding.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --months 6
    python3 backtest_funding.py --csv data/funding --months 6      # offline

Unlike every directional backtest in this project, the return here is a
recorded cash flow rather than a forecast, so the backtest is arithmetic on
observed history rather than an estimate of skill. That removes the
overfitting problem but not the regime problem: six months of positive
funding is evidence that funding WAS positive, not a promise that it stays
that way. The output reports the worst stretch as prominently as the total.

Funding history comes from the public endpoint
/fapi/v1/fundingRate (no API key needed, 1000 rows per call, 3 rows a day,
so six months is one or two calls per symbol).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List

import funding_arb as FA

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


def fetch_funding(symbol: str, months: float) -> List[tuple]:
    """[(ts_seconds, rate_bps)] oldest first."""
    end = int(time.time() * 1000)
    start = end - int(months * 30.44 * 86_400_000)
    out: Dict[int, float] = {}
    cursor = start
    while cursor < end:
        url = f"{FUNDING_URL}?symbol={symbol}&startTime={cursor}&limit=1000"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                rows = json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001
            raise SystemExit(
                f"could not reach Binance for {symbol}: {e}\n"
                "If this is a 403 on CONNECT the machine is blocked from "
                "Binance. Run it locally, or use --csv with saved history.")
        if not rows:
            break
        for r in rows:
            out[int(r["fundingTime"])] = float(r["fundingRate"]) * 1e4
        nxt = int(rows[-1]["fundingTime"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.2)
    return [(k / 1000.0, out[k]) for k in sorted(out)]


def load_funding_csv(directory: str, symbol: str) -> List[tuple]:
    path = os.path.join(directory, f"{symbol}.csv")
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].lstrip("-").replace(".", "", 1).isdigit():
                continue
            ts = float(row[0])
            if ts > 1e11:
                ts /= 1000.0
            out.append((ts, float(row[1])))
    out.sort()
    return out


def save_funding_csv(directory: str, symbol: str, rows: List[tuple]) -> None:
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{symbol}.csv"), "w",
              newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["funding_time", "rate_bps"])
        w.writerows(rows)


def run_carry(rows: List[tuple], costs: FA.CarryCosts, p: FA.CarryParams):
    """Walk the funding series, opening and closing carries by the rules.

    Funding at index i is only visible AFTER it settles, so the decision at
    i uses history up to i-1. Using the print you are about to receive to
    decide whether to hold for it is the same lookahead that ruined the
    directional backtests.
    """
    hist: List[float] = []
    state = FA.CarryState()
    cycles = []
    equity_bps = 0.0
    curve = [0.0]

    for i, (ts, rate) in enumerate(rows):
        if state.open:
            state.periods_held += 1
            state.funding_collected_bps += rate       # negative rates subtract
            equity_bps_now = equity_bps + state.funding_collected_bps - costs.entry_bps
        else:
            equity_bps_now = equity_bps
        curve.append(equity_bps_now)

        hist.append(rate)

        if state.open:
            exit_now, reason = FA.should_exit(state, hist, costs, p)
            if exit_now:
                net = FA.cycle_pnl_bps(state.funding_collected_bps, costs)
                equity_bps += net
                cycles.append({
                    "entry_ts": rows[state.entry_period][0], "exit_ts": ts,
                    "periods": state.periods_held,
                    "funding_bps": state.funding_collected_bps,
                    "net_bps": net, "reason": reason,
                })
                state = FA.CarryState()
        else:
            enter, reason, est = FA.should_enter(hist, costs, p)
            if enter:
                state = FA.CarryState(open=True, entry_period=i, notional=1.0)

    if state.open:
        # An open position at the end is marked out at its current accrual
        # minus the full round trip, exactly as if it were closed now.
        net = FA.cycle_pnl_bps(state.funding_collected_bps, costs)
        equity_bps += net
        cycles.append({"entry_ts": rows[state.entry_period][0],
                       "exit_ts": rows[-1][0], "periods": state.periods_held,
                       "funding_bps": state.funding_collected_bps,
                       "net_bps": net, "reason": "end_of_data"})
    return cycles, equity_bps, curve


def summarise(cycles, equity_bps, curve, p: FA.CarryParams, days: float):
    if not cycles:
        return {"cycles": 0}
    nets = [c["net_bps"] for c in cycles]
    wins = [x for x in nets if x > 0]
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    on_notional = equity_bps / 1e4
    capital_mult = 1.0 + 1.0 / p.leverage
    years = days / 365.0 if days > 0 else 1.0
    return {
        "cycles": len(cycles),
        "win_rate": len(wins) / len(cycles) * 100,
        "total_bps": equity_bps,
        "mean_hold_days": sum(c["periods"] for c in cycles) / len(cycles) / 3,
        "best_bps": max(nets), "worst_bps": min(nets),
        "max_dd_bps": dd,
        "return_on_notional_pct": on_notional * 100,
        "return_on_capital_pct": on_notional / capital_mult * 100,
        "annualised_on_capital_pct": (on_notional / capital_mult / years) * 100,
        "exposure_pct": sum(c["periods"] for c in cycles) / max(1, len(curve)) * 100,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT")
    ap.add_argument("--months", type=float, default=6.0)
    ap.add_argument("--csv", default=None, help="load/save funding CSVs here")
    ap.add_argument("--save", default=None, help="write fetched funding to this dir")
    ap.add_argument("--spot-bps", type=float, default=10.0, help="per side")
    ap.add_argument("--perp-bps", type=float, default=5.0, help="per side")
    ap.add_argument("--leverage", type=float, default=2.0)
    ap.add_argument("--min-funding-bps", type=float, default=0.5)
    a = ap.parse_args(argv)

    costs = FA.CarryCosts(a.spot_bps, a.spot_bps, a.perp_bps, a.perp_bps)
    p = FA.CarryParams(leverage=a.leverage, min_funding_bps=a.min_funding_bps)
    print(f"=== delta-neutral funding carry, {a.months:g} months ===")
    print(f"    cost {costs.total_bps:.1f} bps/cycle "
          f"(spot {a.spot_bps:g}/side, perp {a.perp_bps:g}/side), "
          f"leverage {a.leverage:g}x on the perp leg")
    print(f"    break-even needs {costs.total_bps:.0f} bps of funding = "
          f"{costs.total_bps / 3:.0f} days at 1 bps/period\n")

    agg = []
    for sym in [s.strip().upper() for s in a.symbols.split(",") if s.strip()]:
        rows = (load_funding_csv(a.csv, sym) if a.csv
                else fetch_funding(sym, a.months))
        if a.save:
            save_funding_csv(a.save, sym, rows)
        if len(rows) < 60:
            print(f"  {sym}: only {len(rows)} funding prints, skipping")
            continue
        days = (rows[-1][0] - rows[0][0]) / 86400
        rates = [r for _, r in rows]
        pos = sum(1 for r in rates if r > 0) / len(rates) * 100
        med = sorted(rates)[len(rates) // 2]
        print(f"{sym}: {len(rows)} prints over {days:.0f} days")
        print(f"  funding  median {med:+.3f} bps/8h  "
              f"mean {sum(rates) / len(rates):+.3f}  "
              f"positive {pos:.0f}% of periods  "
              f"(median annualised {med * 3 * 365 / 100:+.1f}%)")

        cycles, eq, curve = run_carry(rows, costs, p)
        st = summarise(cycles, eq, curve, p, days)
        if not st["cycles"]:
            print("  no carry met the entry rule\n")
            continue
        print(f"  cycles {st['cycles']}  win {st['win_rate']:.0f}%  "
              f"mean hold {st['mean_hold_days']:.1f}d  "
              f"in-market {st['exposure_pct']:.0f}% of the time")
        print(f"  net {st['total_bps']:+.0f} bps on notional  "
              f"best {st['best_bps']:+.0f}  worst {st['worst_bps']:+.0f}  "
              f"max drawdown {st['max_dd_bps']:.0f} bps")
        print(f"  RETURN ON CAPITAL {st['return_on_capital_pct']:+.2f}% "
              f"over {days:.0f} days = "
              f"{st['annualised_on_capital_pct']:+.1f}% annualised\n")
        agg.append(st)

    if agg:
        print("=== portfolio ===")
        print(f"  mean annualised return on capital: "
              f"{sum(s['annualised_on_capital_pct'] for s in agg) / len(agg):+.1f}%")
        print(f"  symbols profitable: "
              f"{sum(1 for s in agg if s['total_bps'] > 0)}/{len(agg)}")
        print(f"  worst single cycle: "
              f"{min(s['worst_bps'] for s in agg):+.0f} bps")
        print("\n  This is a single-digit-percent trade on majors, not a")
        print("  doubling machine. Judge it against a savings rate, and size")
        print("  it knowing the drawdown above is what a funding flip costs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
