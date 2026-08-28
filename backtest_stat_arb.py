#!/usr/bin/env python3
"""Walk-forward backtest of cointegrated pairs trading.

    python3 backtest_stat_arb.py --csv data/1h --interval 1h \
            --symbols BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT --fee-bps 7.32

For every pair it refits the hedge ratio, spread mean/sd, ADF statistic and
half-life on a training window, then trades the NEXT window with those frozen
numbers, then rolls forward. Nothing that decides a trade is ever estimated
on the data that trade is scored against.

Reading the output
------------------
Two lines matter and they are both out of sample:

  WALK-FORWARD   what the rules earned on data they had not seen.
  FALSE-POSITIVE the same machinery run on independent random walks, which
                 by construction contain no cointegration. Whatever it
                 "earns" there is what the method extracts from nothing. A
                 real result must be clearly outside that.

The second is the one that separates this from every promising pairs
backtest that ever lost money. Two independent random walks drift apart and
back for reasons that have nothing to do with an economic link, and a spread
built from them looks convincingly mean-reverting until it does not revert.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
import sys
from typing import Dict, List, Optional

import numpy as np

import stat_arb as SA


def load_closes(directory: str, symbol: str):
    ts, close = [], []
    with open(os.path.join(directory, f"{symbol}.csv"), newline="",
              encoding="utf-8") as fh:
        for r in csv.reader(fh):
            if not r or not r[0].lstrip("-").replace(".", "", 1).isdigit():
                continue
            t = float(r[0])
            if t > 1e11:
                t /= 1000.0
            ts.append(t)
            close.append(float(r[4]))
    order = np.argsort(ts)
    return np.array(ts)[order], np.array(close)[order]


def align(series: Dict[str, tuple]) -> tuple:
    """Common timestamps across every symbol - a pair trade needs both legs
    quoted at the same instant, and forward-filling one leg onto the other's
    clock invents prices that never traded."""
    common = None
    for ts, _ in series.values():
        s = set(np.round(ts).astype(np.int64))
        common = s if common is None else (common & s)
    grid = np.array(sorted(common), dtype=float)
    out = {}
    for sym, (ts, px) in series.items():
        idx = {int(round(t)): i for i, t in enumerate(ts)}
        out[sym] = np.array([px[idx[int(g)]] for g in grid])
    return grid, out


def trade_pair(log_y: np.ndarray, log_x: np.ndarray, st: SA.PairStats,
               z_entry: float, z_exit: float, z_stop: float,
               cost_bps: float, max_hold: int):
    """Trade one test window with FROZEN training statistics.

    Position is +1 (long the spread: long y, short beta*x) or -1. P&L for one
    bar is the spread change times the position; the cost of entering and
    leaving is charged on (1 + |beta|) units of notional because that is how
    much is actually traded.
    """
    spread = log_y - st.beta * log_x - st.intercept
    z = (spread - st.spread_mean) / (st.spread_sd if st.spread_sd > 0 else 1e-9)
    leg_cost = (1.0 + abs(st.beta)) * cost_bps / 1e4

    trades, pos, entry_i = [], 0, 0
    for i in range(1, len(z)):
        if pos != 0:
            held = i - entry_i
            hit_target = abs(z[i]) <= z_exit
            hit_stop = abs(z[i]) >= z_stop
            expired = held >= max_hold
            if hit_target or hit_stop or expired or i == len(z) - 1:
                gross = pos * (spread[i] - spread[entry_i])
                trades.append({
                    "gross_bps": gross * 1e4,
                    "net_bps": gross * 1e4 - leg_cost * 1e4,
                    "bars": held,
                    "reason": ("target" if hit_target else "stop" if hit_stop
                               else "expiry" if expired else "end_of_window"),
                })
                pos = 0
        if pos == 0 and i < len(z) - 1:
            # The entry band is a BAND, not a floor. Entering at |z| beyond
            # the stop means stopping out on the very next bar and re-entering
            # the bar after - a churn loop that pays the fee every bar and
            # captures nothing. It is also economically wrong: |z| past the
            # stop is the evidence that this spread has stopped reverting, so
            # it is the last moment to add exposure to it.
            if z_entry <= z[i] < z_stop:
                pos, entry_i = -1, i        # spread rich: short it
            elif -z_stop < z[i] <= -z_entry:
                pos, entry_i = 1, i         # spread cheap: buy it
    return trades


def walk_forward_pair(log_a: np.ndarray, log_b: np.ndarray, folds: int,
                      z_entry: float, z_exit: float, z_stop: float,
                      cost_bps: float, alpha: float, max_hold: int):
    n = len(log_a)
    size = n // (folds + 1)
    out, fits = [], []
    for f in range(folds):
        tr_hi = size * (f + 1)
        te_hi = min(size * (f + 2), n)
        if te_hi - tr_hi < 30 or tr_hi < 200:
            continue
        st = SA.fit_pair(log_a[:tr_hi], log_b[:tr_hi], alpha=alpha)
        fits.append(st)
        if not st.cointegrated:
            continue
        out.extend(trade_pair(log_a[tr_hi:te_hi], log_b[tr_hi:te_hi], st,
                              z_entry, z_exit, z_stop, cost_bps, max_hold))
    return out, fits


def summarise(trades):
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "total_bps": 0.0,
                "expectancy_bps": 0.0, "t_stat": 0.0, "profit_factor": 0.0,
                "max_dd_bps": 0.0, "mean_bars": 0.0}
    nets = np.array([t["net_bps"] for t in trades])
    wins, losses = nets[nets > 0], nets[nets <= 0]
    curve = np.concatenate([[0.0], np.cumsum(nets)])
    peak = np.maximum.accumulate(curve)
    # The t-statistic, not the per-trade average, is what the null control
    # compares. A control universe that took two lucky trades shows an
    # expectancy of +800 bps; one that took six hundred cannot. Dividing by
    # the standard error makes runs of different sizes comparable, which is
    # the whole point of asking whether a mean is distinguishable from zero.
    sd = float(nets.std(ddof=1)) if len(nets) > 1 else 0.0
    tstat = float(nets.mean() / (sd / np.sqrt(len(nets)))) if sd > 0 else 0.0
    return {
        "trades": len(nets),
        "win_rate": float((nets > 0).mean() * 100),
        "total_bps": float(nets.sum()),
        "expectancy_bps": float(nets.mean()),
        "t_stat": tstat,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("inf"),
        "max_dd_bps": float((peak - curve).max()),
        "mean_bars": float(np.mean([t["bars"] for t in trades])),
    }


def null_distribution(n_bars: int, n_symbols: int, universes: int, folds: int,
                      z_entry: float, z_exit: float, z_stop: float,
                      cost_bps: float, alpha: float, max_hold: int,
                      sigma: float, seed: int = 0):
    """Distribution of the pooled result over synthetic universes with NO
    cointegration in them.

    The first version of this pooled every control pair into one number and
    compared point estimates. That was wrong, and testing it on independent
    random walks proved it: the real run scored +39 bps against a control
    mean of -19 and was declared a winner, when both were noise. The control
    pooled 120 fits and the real run pooled 36, so the control's mean was
    simply less noisy - a difference in sample size read as a difference in
    skill.

    The fix is to make each control draw the SAME SHAPE as the real study:
    n_symbols independent walks, the same pair count, the same folds. That
    yields a distribution of outcomes achievable from nothing, and the real
    result is scored as a percentile against it rather than against its mean.
    """
    rng = np.random.default_rng(seed)
    means, flagged, fits_total, trade_counts = [], 0, 0, []
    for _ in range(universes):
        logs = [np.cumsum(rng.normal(0, sigma, n_bars)) for _ in range(n_symbols)]
        trades = []
        for i, j in itertools.combinations(range(n_symbols), 2):
            tr, fits = walk_forward_pair(logs[i], logs[j], folds, z_entry,
                                         z_exit, z_stop, cost_bps, alpha,
                                         max_hold)
            trades.extend(tr)
            flagged += sum(1 for s in fits if s.cointegrated)
            fits_total += len(fits)
        st = summarise(trades)
        trade_counts.append(st["trades"])
        if st["trades"] > 1:
            means.append(st["t_stat"])
    return {
        "flag_pct": flagged / max(1, fits_total) * 100,
        "means": np.array(means),
        "median_trades": float(np.median(trade_counts)) if trade_counts else 0.0,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/1h")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT")
    ap.add_argument("--fee-bps", type=float, default=7.32, help="per leg, round trip")
    ap.add_argument("--z-entry", type=float, default=2.0)
    ap.add_argument("--z-exit", type=float, default=0.5)
    ap.add_argument("--z-stop", type=float, default=4.0)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--max-hold", type=int, default=200, help="bars")
    ap.add_argument("--null-universes", type=int, default=200,
                    help="synthetic no-cointegration universes for the control")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    raw = {}
    for s in syms:
        try:
            raw[s] = load_closes(a.csv, s)
        except FileNotFoundError:
            print(f"  {s}: no CSV in {a.csv} - run fetch_binance_data.py first")
    if len(raw) < 2:
        print("need at least two symbols")
        return 1
    grid, px = align(raw)
    logs = {s: np.log(v) for s, v in px.items()}
    print(f"=== cointegration pairs, {a.interval}, {len(grid)} aligned bars ===")
    print(f"    entry |z|>{a.z_entry:g}, exit |z|<{a.z_exit:g}, "
          f"stop |z|>{a.z_stop:g}, fee {a.fee_bps:g} bps/leg round trip\n")

    print("--- pre-screen on the FULL sample (indicative only, in-sample) ---")
    print(f"{'pair':>14s} {'beta':>7s} {'ADF':>7s} {'half-life':>10s} "
          f"{'spread sd':>10s} {'net/trade':>10s}  tradeable")
    pairs = list(itertools.combinations(sorted(logs), 2))
    for A, B in pairs:
        st = SA.fit_pair(logs[A], logs[B], alpha=a.alpha)
        net = SA.expected_net_bps(st, a.z_entry, a.z_exit, a.fee_bps)
        hl = f"{st.half_life:.0f}" if np.isfinite(st.half_life) else "inf"
        print(f"{A[:-4] + '/' + B[:-4]:>14s} {st.beta:7.3f} {st.adf:7.2f} "
              f"{hl:>10s} {st.spread_sd * 1e4:7.0f}bp {net:8.1f}bp  "
              f"{'yes' if st.cointegrated and net > 0 else 'no'}"
              f"{'' if st.cointegrated else '  (' + st.reason + ')'}")

    print("\n--- WALK-FORWARD (out of sample) ---")
    pooled = []
    for A, B in pairs:
        tr, fits = walk_forward_pair(logs[A], logs[B], a.folds, a.z_entry,
                                     a.z_exit, a.z_stop, a.fee_bps, a.alpha,
                                     a.max_hold)
        coint = sum(1 for s in fits if s.cointegrated)
        st = summarise(tr)
        pooled.extend(tr)
        if not st["trades"]:
            print(f"  {A[:-4] + '/' + B[:-4]:>14s}  cointegrated in "
                  f"{coint}/{len(fits)} folds, no trades")
            continue
        print(f"  {A[:-4] + '/' + B[:-4]:>14s}  coint {coint}/{len(fits)} folds  "
              f"trades {st['trades']:3d}  win {st['win_rate']:4.0f}%  "
              f"PF {st['profit_factor']:4.2f}  "
              f"exp {st['expectancy_bps']:+6.1f}bp  "
              f"total {st['total_bps']:+7.0f}bp")

    ps = summarise(pooled)
    print("\n=== POOLED OUT-OF-SAMPLE ===")
    if not ps["trades"]:
        print("  no trades - no pair passed cointegration out of sample")
        return 0
    print(f"  trades          {ps['trades']}")
    print(f"  win rate        {ps['win_rate']:.1f}%")
    print(f"  profit factor   {ps['profit_factor']:.2f}")
    print(f"  expectancy      {ps['expectancy_bps']:+.2f} bps per trade "
          f"(t = {ps['t_stat']:+.2f})")
    print(f"  total           {ps['total_bps']:+.0f} bps")
    print(f"  max drawdown    {ps['max_dd_bps']:.0f} bps")
    print(f"  mean hold       {ps['mean_bars']:.0f} bars")

    sigma = float(np.mean([np.diff(v).std() for v in logs.values()]))
    null = null_distribution(len(grid), len(logs), a.null_universes, a.folds,
                             a.z_entry, a.z_exit, a.z_stop, a.fee_bps,
                             a.alpha, a.max_hold, sigma)
    m = null["means"]
    print(f"\n=== NULL CONTROL ({a.null_universes} synthetic universes, "
          f"{len(logs)} independent walks each) ===")
    print(f"  declared cointegrated  {null['flag_pct']:.1f}% of fits "
          f"(alpha says {a.alpha * 100:.0f}%)")
    if len(m) < 5:
        print(f"  only {len(m)} control universes produced any trades - the")
        print("  filter rejects noise almost entirely, which is a good sign,")
        print("  but the comparison below is weak. Raise --null-universes.")
    if len(m):
        pval = float((m >= ps["t_stat"]).mean())
        print(f"  t-stat from NOTHING:  median {np.median(m):+.2f}  "
              f"95th pct {np.percentile(m, 95):+.2f}  max {m.max():+.2f}")
        print(f"  your t-stat           {ps['t_stat']:+.2f}  "
              f"({ps['expectancy_bps']:+.2f} bps over {ps['trades']} trades)")
        print(f"  p-value (control universes that matched or beat it): {pval:.3f}")
        # TWO independent hurdles, both required.
        #
        # The null percentile alone is not enough: with a heavy right tail it
        # takes hundreds of universes to resolve a threshold, and on 60 a
        # t-stat of 1.44 from pure noise scored p=0.027. The raw t-stat alone
        # is not enough either, because it does not know how many pairs and
        # folds were searched to find it. Testing k pairs is k chances to get
        # lucky, so the raw hurdle is Bonferroni-adjusted for the pair count.
        n_pairs_tested = len(pairs)
        t_hurdle = 2.0 + math.log(max(1, n_pairs_tested))
        t_ok = ps["t_stat"] >= t_hurdle
        p_ok = pval <= 0.05
        print(f"\n  hurdle 1  raw t >= {t_hurdle:.2f} "
              f"(2.0 adjusted for {n_pairs_tested} pairs searched): "
              f"{'PASS' if t_ok else 'FAIL'} at t={ps['t_stat']:+.2f}")
        print(f"  hurdle 2  p <= 0.05 against the null universes: "
              f"{'PASS' if p_ok else 'FAIL'} at p={pval:.3f}")
        if ps["expectancy_bps"] <= 0 or not (t_ok and p_ok):
            print("\n  NOT TRADEABLE. Both hurdles must clear. The null control")
            print("  catches a method that mines noise; the t-stat catches a")
            print("  result with too little evidence behind it, however pretty.")
        else:
            print("\n  Clears both hurdles. Size it with:")
            print(f"    python3 risk_simulator.py --win-rate {ps['win_rate']:.1f} "
                  f"--avg-win 1.0 --avg-loss 1.0 --observed-trades {ps['trades']}")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"pooled": ps, "false_positive_pct": fp_rate,
                       "control": fp}, fh, indent=2)
        print(f"\n  wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
