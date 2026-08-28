#!/usr/bin/env python3
"""Cost optimisation for the cross-sectional book - hurdle 3 only.

    python3 optimise_cross_sectional.py --csv data/1h --interval 1h \
            --universe-file data/universe.txt --signal low_vol

Hurdles 1, 2 and 4 test whether a FORECAST exists. This sweeps only the
parameters that decide what the forecast costs to harvest - rebalance
period, damping, no-trade band, quantile width, signal smoothing and fee -
and leaves the signal itself alone.

Why that separation is the whole discipline
-------------------------------------------
Sweeping parameters until a hurdle clears is how a null result gets
converted into a backtest. It is legitimate here ONLY because:

  * the signal is fixed before the sweep starts. Nothing below can invent
    forecasting power; it can only stop wasting the power already measured.
  * the null control is re-run at the winning configuration, not at the
    default, so the comparison is against noise put through the same search.
  * the t-hurdle is raised by log(configurations tried). Searching 200
    settings is 200 chances to get lucky and the threshold must know that.

If the winner clears an adjusted hurdle, the edge was real and was being
eaten by turnover. If it only clears the unadjusted one, the sweep found
the luckiest cell and nothing more.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys

import numpy as np

import backtest_cross_sectional as B
import cross_sectional as CS


def evaluate(px, signal_fn, rebalance, p, fee_bps, ppy_base):
    r, t, ic = B.run_book(px, signal_fn, p, rebalance, fee_bps)
    return B.summarise(r, t, ic, ppy_base / rebalance, fee_bps)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/1h")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--universe-file", default=None)
    ap.add_argument("--signal", default="low_vol", choices=sorted(CS.SIGNALS))
    ap.add_argument("--fee-bps", type=float, default=7.32)
    ap.add_argument("--maker-bps", type=float, default=4.0,
                    help="cost if entries are posted rather than taken")
    ap.add_argument("--null-runs", type=int, default=60)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    if a.universe_file:
        with open(a.universe_file, encoding="utf-8") as fh:
            syms = [l.strip().upper() for l in fh
                    if l.strip() and not l.startswith("#")]
    elif a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        import os
        syms = [f[:-4] for f in sorted(os.listdir(a.csv)) if f.endswith(".csv")]

    grid, px, kept = B.load_matrix(a.csv, syms)
    if px.size == 0 or len(kept) < 10:
        print(f"need at least 10 aligned symbols; got {len(kept)}")
        return 1
    bars_per_year = {"15m": 4 * 24 * 365, "1h": 24 * 365,
                     "4h": 6 * 365, "1d": 365}.get(a.interval, 24 * 365)
    base_fn = CS.SIGNALS[a.signal]

    print(f"=== cost optimisation for '{a.signal}', {len(kept)} names, "
          f"{len(grid)} bars ===")
    print("    the signal is FIXED; only harvesting parameters move\n")

    # --- diagnostic that low_vol in particular needs ---------------------
    p0 = CS.BookParams()
    book_r, mkt_r = [], []
    w_prev = None
    for i in range(0, px.shape[0] - 24, 24):
        sig = base_fn(px, i)
        if not np.isfinite(sig).any():
            continue
        w = CS.apply_damping(CS.target_weights(sig, p0), w_prev, p0)
        fwd = px[i + 24] / px[i] - 1.0
        book_r.append(float(np.nansum(w * np.nan_to_num(fwd, nan=0.0))))
        mkt_r.append(float(np.nanmean(fwd)))
        w_prev = w
    beta = CS.book_beta(np.array(book_r), np.array(mkt_r))
    betas = np.array([beta]) if np.isfinite(beta) else np.array([])
    if len(betas):
        print(f"--- market-exposure check ---")
        print(f"  book beta to the equal-weighted market: {beta:+.4f}  "
              f"(over {len(book_r)} rebalances)")
        if abs(betas.mean()) > 0.05:
            print("  WARNING: this book is NOT market neutral despite summing to")
            print("  zero. Ranking on volatility puts calm names long and wild")
            print("  ones short, and the wild ones carry more beta. Some of the")
            print("  measured IC may be a directional bet on a trending sample,")
            print("  not a forecast. Treat any win below with suspicion.\n")
        else:
            print("  book is close to market neutral - the IC is not just beta.\n")

    REBAL = [24, 48, 72, 120, 168, 336]
    DAMP = [0.0, 0.5, 0.8]
    BAND = [0.0, 0.005, 0.02]
    QUANT = [0.1, 0.2, 0.3]
    SMOOTH = [1, 3, 6]
    FEES = [a.fee_bps, a.maker_bps]

    combos = list(itertools.product(REBAL, DAMP, BAND, QUANT, SMOOTH, FEES))
    print(f"--- sweeping {len(combos)} harvesting configurations ---")
    rows = []
    for reb, damp, band, quant, sm, fee in combos:
        p = CS.BookParams(quantile=quant, damping=damp, no_trade_band=band)
        fn = base_fn if sm == 1 else CS.smoothed(base_fn, sm)
        st = evaluate(px, fn, reb, p, fee, bars_per_year)
        if st["periods"] < 20:
            continue
        rows.append({"rebalance": reb, "damping": damp, "band": band,
                     "quantile": quant, "smooth": sm, "fee": fee, **st})
    if not rows:
        print("no configuration produced enough rebalances to evaluate")
        return 1

    rows.sort(key=lambda r: -r["sharpe"])
    print(f"\n{'reb':>5s} {'damp':>5s} {'band':>6s} {'quant':>6s} {'sm':>3s} "
          f"{'fee':>5s} {'Sharpe':>8s} {'IC t':>7s} {'turn/yr':>8s} {'cost%':>7s}")
    for r in rows[:12]:
        print(f"{r['rebalance']:5d} {r['damping']:5.1f} {r['band']:6.3f} "
              f"{r['quantile']:6.2f} {r['smooth']:3d} {r['fee']:5.2f} "
              f"{r['sharpe']:+8.2f} {r['ic_t']:+7.2f} "
              f"{r['annual_turnover']:8.1f} {r['cost_drag_pct']:6.1f}%")

    best = rows[0]
    print(f"\n=== best configuration ===")
    print(f"  rebalance every {best['rebalance']} bars, damping "
          f"{best['damping']:g}, band {best['band']:g}, quantile "
          f"{best['quantile']:g}, smoothing {best['smooth']}, "
          f"fee {best['fee']:g} bps")
    print(f"  net Sharpe {best['sharpe']:+.2f}   IC t {best['ic_t']:+.2f}   "
          f"cost {best['cost_drag_pct']:.1f}%/yr   "
          f"turnover {best['annual_turnover']:.1f}x")

    # --- the control, at the WINNING settings ----------------------------
    p = CS.BookParams(quantile=best["quantile"], damping=best["damping"],
                      no_trade_band=best["band"])
    fn = base_fn if best["smooth"] == 1 else CS.smoothed(base_fn, best["smooth"])
    null_sharpe, null_ic = [], []
    for k in range(a.null_runs):
        r, t, ic = B.run_book(px, fn, p, best["rebalance"], best["fee"],
                              shuffle_rng=np.random.default_rng(7000 + k))
        st = B.summarise(r, t, ic, bars_per_year / best["rebalance"], best["fee"])
        null_sharpe.append(st["sharpe"])
        null_ic.append(st["ic_t"])
    nsh, nic = np.array(null_sharpe), np.array(null_ic)
    p_sh = float((nsh >= best["sharpe"]).mean())
    p_ic = float((nic >= best["ic_t"]).mean())

    t_hurdle = 2.0 + math.log(max(1, len(rows)))
    print(f"\n=== NULL CONTROL at the winning settings ({a.null_runs} runs) ===")
    print(f"  Sharpe from shuffled ranks  median {np.median(nsh):+.2f}  "
          f"95th {np.percentile(nsh, 95):+.2f}")
    print(f"  IC t from shuffled ranks    median {np.median(nic):+.2f}  "
          f"95th {np.percentile(nic, 95):+.2f}")
    print(f"  p on Sharpe {p_sh:.3f}   p on IC t {p_ic:.3f}")
    print(f"\n  hurdle 1  IC t >= {t_hurdle:.2f} "
          f"(2.0 + log of {len(rows)} configurations searched): "
          f"{'PASS' if best['ic_t'] >= t_hurdle else 'FAIL'} at {best['ic_t']:+.2f}")
    print(f"  hurdle 2  p <= 0.05 on IC t: "
          f"{'PASS' if p_ic <= 0.05 else 'FAIL'} at {p_ic:.3f}")
    print(f"  hurdle 3  net Sharpe > 0: "
          f"{'PASS' if best['sharpe'] > 0 else 'FAIL'} at {best['sharpe']:+.2f}")
    # A failing beta is not a dead end - it is a hedge. Strip beta*market
    # out of the book's returns and see whether the edge survives. If it
    # does, the fix is to short the index alongside the book; if it does
    # not, the "edge" was the market all along.
    hedged_sharpe = None
    if len(betas) and abs(beta) > 0.05 and len(book_r) > 10:
        br = np.array(book_r) - beta * np.array(mkt_r)
        sd = br.std(ddof=1)
        if sd > 0:
            hedged_sharpe = float(br.mean() / sd
                                  * math.sqrt(bars_per_year / 24))
            print(f"\n  after hedging out beta {beta:+.4f}: "
                  f"Sharpe {hedged_sharpe:+.2f} "
                  f"(unhedged {np.array(book_r).mean() / np.array(book_r).std(ddof=1) * math.sqrt(bars_per_year / 24):+.2f})")
            print("  If that is close to the unhedged figure the signal is real")
            print("  and beta was incidental. If it collapses, the market WAS")
            print("  the signal and hedging removes the whole result.")

    beta_ok = (not len(betas)) or abs(betas.mean()) <= 0.05
    print(f"  hurdle 5  book beta within 0.05: "
          f"{'PASS' if beta_ok else 'FAIL'} at "
          f"{betas.mean() if len(betas) else float('nan'):+.4f}")

    all_ok = (best["ic_t"] >= t_hurdle and p_ic <= 0.05
              and best["sharpe"] > 0 and beta_ok)
    print("\n  " + ("TRADEABLE at these settings. The edge was real and "
                    "turnover was eating it." if all_ok else
                    "NOT TRADEABLE. A sweep that only clears the UNADJUSTED "
                    "hurdle has\n  found its luckiest cell, not an edge."))
    if best["sharpe"] > 0 and best["ic_t"] < t_hurdle:
        print(f"\n  Note: IC t {best['ic_t']:+.2f} would pass a plain 2.0 threshold but not")
        print(f"  the {t_hurdle:.2f} demanded by searching {len(rows)} configurations.")
        print("  Confirm on data none of this sweep has seen before trading it.")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"best": best, "p_ic": p_ic, "p_sharpe": p_sh,
                       "configs": len(rows),
                       "book_beta": float(betas.mean()) if len(betas) else None},
                      fh, indent=2)
        print(f"\n  wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
