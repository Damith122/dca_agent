#!/usr/bin/env python3
"""Train-only strategy-family selection with one untouched final test."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace

import numpy as np

from backtest_intraday import evaluate, fetch_inputs, fmt
from intraday import IntradayParams, align_candles


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT")
    ap.add_argument("--months", type=float, default=12.0)
    ap.add_argument("--starting-equity", type=float, default=15.0)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    candles, funding = fetch_inputs(symbols, a.months, a.cache)
    grid, _ = align_candles(candles)

    base = IntradayParams()
    warm = max(base.channel, base.atr_period, base.volume_lookback,
               base.momentum_bars, base.hourly_ema_slow * 4) + 8
    final_start = warm + int((len(grid) - warm) * .80)
    if final_start - warm < 6000 or len(grid) - final_start < 1500:
        raise SystemExit("not enough aligned bars for 80/20 train/final split")

    family = {
        "pullback_base": replace(base, signal_mode="pullback", volume_ratio=1.0),
        "pullback_selective": replace(base, signal_mode="pullback", volume_ratio=1.3),
        "impulse_10bps": replace(base, signal_mode="impulse", momentum_floor_pct=.0010,
                                  volume_ratio=1.2),
        "impulse_20bps": replace(base, signal_mode="impulse", momentum_floor_pct=.0020,
                                  volume_ratio=1.2),
        "pullback_long_only": replace(base, signal_mode="pullback", volume_ratio=1.0,
                                      allow_short=False),
        "impulse_long_only": replace(base, signal_mode="impulse",
                                     momentum_floor_pct=.0010, volume_ratio=1.2,
                                     allow_short=False),
        "reversion_fast": replace(base, signal_mode="reversion", stop_pct=.0030,
                                   target_pct=.0035, max_hold_bars=8),
        "reversion_wide": replace(base, signal_mode="reversion", stop_pct=.0030,
                                   target_pct=.0045, max_hold_bars=12),
    }
    boundaries = np.linspace(warm, final_start, 4, dtype=int)
    training = {}
    eligible = []
    print(f"=== TRAIN ONLY: {len(family)} precommitted hypotheses, final 20% sealed ===")
    for name, p in family.items():
        folds = [evaluate(candles, funding, p, a.starting_equity, int(lo), int(hi))
                 for lo, hi in zip(boundaries[:-1], boundaries[1:])]
        combined = evaluate(candles, funding, p, a.starting_equity, warm, final_start)
        positive = sum(st["net_pnl"] > 0 for st in folds)
        train_ok = (combined["net_pnl"] > 0 and combined["profit_factor"] >= 1.10
                    and combined["trades"] >= 100 and positive >= 2
                    and 3 <= combined["candidate_signals_per_day"] <= 15
                    and .3 <= combined["trades_per_day"] <= 3)
        training[name] = {"combined": combined, "folds": folds,
                          "positive_folds": positive, "eligible": train_ok}
        print(f"\n{name:20s} {'ELIGIBLE' if train_ok else 'REJECT'}")
        print("  train " + fmt(combined))
        for i, st in enumerate(folds, 1):
            print(f"  fold {i}/3 " + fmt(st))
        if train_ok:
            eligible.append((name, p, combined, folds))

    payload = {"symbols": symbols, "months": a.months, "training": training,
               "final_test_opened": False, "passed": False}
    if not eligible:
        print("\nNO TRAIN-ELIGIBLE STRATEGY. Final 20% remains unopened; do not deploy.")
        if a.json:
            with open(a.json, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        return 2

    # Precommitted selection: highest minimum fold PF, then combined PF.
    eligible.sort(key=lambda item: (
        min(st["profit_factor"] for st in item[3]), item[2]["profit_factor"]),
        reverse=True)
    name, selected, train_st, folds = eligible[0]
    print(f"\nSELECTED ON TRAIN ONLY: {name}")
    print("Opening the final 20% exactly once.")
    final = evaluate(candles, funding, selected, a.starting_equity,
                     final_start, len(grid))
    stress = evaluate(candles, funding, replace(selected, cost_bps_per_side=10.0),
                      a.starting_equity, final_start, len(grid))
    print("  FINAL 20% " + fmt(final))
    print("  STRESS    " + fmt(stress))
    checks = {
        "final_fee_net_positive": final["net_pnl"] > 0,
        "final_profit_factor_at_least_1_15": final["profit_factor"] >= 1.15,
        "final_at_least_50_trades": final["trades"] >= 50,
        "candidate_signals_5_to_15_per_day": 5 <= final["candidate_signals_per_day"] <= 15,
        "executed_trades_0_5_to_3_per_day": .5 <= final["trades_per_day"] <= 3,
        "average_winner_at_least_3_cents": final["avg_win"] >= .03,
        "positive_at_10bps_side": stress["net_pnl"] > 0,
        "final_drawdown_below_20pct": final["max_drawdown_pct"] < 20,
        "final_equity_above_zero": final["final_equity"] > 0,
    }
    passed = all(checks.values())
    print("\nFINAL ADMISSION")
    for check, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4s} {check}")
    print("\n  " + ("PASS: build a paper-only runner."
                     if passed else "REJECTED: do not deploy this strategy family."))
    payload.update({"selected": name, "selected_params": asdict(selected),
                    "final_test_opened": True, "final": final,
                    "stress": stress, "checks": checks, "passed": passed})
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"  wrote {a.json}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
