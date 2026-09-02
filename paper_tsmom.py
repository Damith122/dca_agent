#!/usr/bin/env python3
"""Reconstruct one fee-aware TSMOM paper account from public Binance data.

This runner is intentionally incapable of placing an order.  It imports no
exchange trading client, deletes Binance credentials from its own process,
uses completed daily candles plus public funding history, and writes only a
structured paper summary to stdout and (when configured) GitHub brain-state.

Railway should run it once after each UTC daily candle closes.  Rebuilding the
account from a fixed start timestamp makes restarts deterministic and avoids
trusting ephemeral local state.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Mapping, Sequence, Tuple

from backtest_tsmom import fetch_inputs
from tsmom import TSMOMParams, align_candles, run, stats


EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
DEFAULT_SYMBOLS = "SOLUSDT,SUIUSDT,BNBUSDT,XRPUSDT,TRXUSDT,DOGEUSDT"
SAFE_NAMESPACE = re.compile(r"[^A-Za-z0-9_.-]+")


def parse_utc(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def completed_daily_candles(series: Mapping[str, Sequence], now: float):
    """Drop today's still-forming UTC candle; it is not a signal yet."""
    return {
        symbol: [c for c in candles if c.ts + 86400.0 <= now]
        for symbol, candles in series.items()
    }


def fetch_execution_filters(symbols: Sequence[str]) -> Tuple[Dict[str, float], Dict[str, float]]:
    with urllib.request.urlopen(EXCHANGE_INFO, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    wanted = set(symbols)
    minimums: Dict[str, float] = {}
    steps: Dict[str, float] = {}
    for row in payload.get("symbols", []):
        symbol = row.get("symbol")
        if symbol not in wanted:
            continue
        filters = {f.get("filterType"): f for f in row.get("filters", [])}
        minimums[symbol] = float(filters.get("MIN_NOTIONAL", {}).get("notional", 0.0))
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
        steps[symbol] = float(lot.get("stepSize", 0.0))
    missing = wanted.difference(minimums)
    if missing:
        raise SystemExit(f"exchangeInfo missing execution filters for {sorted(missing)}")
    return minimums, steps


def make_summary(sim, *, symbols, namespace, start_ts, last_ts, starting_equity,
                 latest_closes):
    closed = sim.trades
    wins = sum(t.net_pnl > 0 for t in closed)
    realised = sum(t.net_pnl for t in closed)
    open_info = None
    open_net = 0.0
    if sim.open_position is not None:
        p = sim.open_position
        mark = float(latest_closes[p.symbol])
        open_net = (p.side * p.qty * (mark - p.entry) + p.funding_pnl
                    - p.entry_fee - sim.estimated_exit_fee)
        open_info = {
            "symbol": p.symbol,
            "side": "LONG" if p.side > 0 else "SHORT",
            "entry": p.entry,
            "mark": mark,
            "qty": p.qty,
            "stop": p.stop,
            "funding_pnl": p.funding_pnl,
            "estimated_exit_fee": sim.estimated_exit_fee,
            "fee_net_pnl_estimate": open_net,
        }
    st = stats(sim, starting_equity)
    return {
        "schema": 1,
        "strategy": "tsmom_30d_long_cash",
        "namespace": namespace,
        "simulation_only": True,
        "live_orders_sent": 0,
        "symbols": list(symbols),
        "start_utc": datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
        "through_completed_candle_utc": datetime.fromtimestamp(last_ts, timezone.utc).isoformat(),
        "starting_equity": starting_equity,
        "closed_trades": len(closed),
        "wins": wins,
        "losses": len(closed) - wins,
        "realized_fee_net_pnl": realised,
        "open_position": open_info,
        "open_position_fee_net_estimate": open_net,
        "estimated_total_fee_net_pnl": sim.final_equity - starting_equity,
        "estimated_final_equity": sim.final_equity,
        "exit_reasons": dict(sorted(Counter(t.reason for t in closed).items())),
        "realized_fees": sim.total_fees,
        "realized_funding": sim.total_funding,
        "blocked_min_notional": sim.blocked_min_notional,
        "floored_min_notional": sim.floored_min_notional,
        "max_drawdown_pct": st["max_drawdown_pct"],
    }


async def archive_summary(summary: dict, namespace: str) -> bool:
    # Keep the pure research/test path usable without aiohttp installed.
    # Railway installs the runtime requirements before this optional archive
    # path is reached.
    from github_sync import GithubBrainSync

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    remote = f"brain/{namespace}_tsmom_paper_summary.json"
    sync = GithubBrainSync(token, repo, remote, "brain-state")
    await sync.start()
    try:
        body = json.dumps(summary, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        return await sync.upload(body, "Update TSMOM paper evidence")
    finally:
        await sync.close()


def main(argv=None) -> int:
    # Defence in depth: even an accidentally provisioned key cannot be used
    # by this public-data-only process.
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "API_KEY", "API_SECRET"):
        os.environ.pop(key, None)

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=os.environ.get("TSMOM_PAPER_SYMBOLS", DEFAULT_SYMBOLS))
    ap.add_argument("--start", default=os.environ.get("TSMOM_PAPER_START_UTC"))
    ap.add_argument("--starting-equity", type=float,
                    default=float(os.environ.get("TSMOM_PAPER_START_EQUITY", "15")))
    ap.add_argument("--months", type=float, default=12.0)
    ap.add_argument("--namespace", default=os.environ.get("TSMOM_PAPER_NAMESPACE", "TSMOM_PAPER_V1"))
    ap.add_argument("--no-archive", action="store_true")
    a = ap.parse_args(argv)
    if not a.start:
        raise SystemExit("TSMOM_PAPER_START_UTC/--start is required for restart-stable evidence")
    if a.starting_equity <= 0:
        raise SystemExit("starting equity must be positive")

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    start_ts = parse_utc(a.start)
    now = time.time()
    if start_ts >= now:
        raise SystemExit("paper start must be earlier than now")
    months = max(a.months, (now - start_ts) / (30.44 * 86400.0) + 5.0)
    namespace = SAFE_NAMESPACE.sub("_", a.namespace).strip("._") or "TSMOM_PAPER_V1"

    candles, funding = fetch_inputs(symbols, months)
    candles = completed_daily_candles(candles, now)
    grid, aligned = align_candles(candles)
    p = TSMOMParams(cost_bps_per_side=7.0, allow_short=False,
                    risk_pct=0.02, max_leverage=1.0)
    warm = max(p.lookback, p.vol_lookback, p.atr_period) + 10
    trade_start = next((i for i, ts in enumerate(grid) if ts >= start_ts), len(grid))
    if trade_start < warm:
        trade_start = warm
    if trade_start >= len(grid):
        raise SystemExit("not enough completed candles after the configured paper start")

    minimums, steps = fetch_execution_filters(symbols)
    sim = run(candles, funding, p, starting_equity=a.starting_equity,
              trade_start=trade_start, liquidate_at_end=False,
              min_notional_by_symbol=minimums, qty_step_by_symbol=steps,
              min_notional_max_risk_pct=0.03)
    latest = {symbol: rows[-1].close for symbol, rows in aligned.items()}
    summary = make_summary(
        sim, symbols=symbols, namespace=namespace, start_ts=grid[trade_start],
        last_ts=grid[-1], starting_equity=a.starting_equity,
        latest_closes=latest,
    )
    print("[tsmom-paper-summary] " + json.dumps(summary, sort_keys=True))
    if not a.no_archive:
        archived = asyncio.run(archive_summary(summary, namespace))
        print(f"[tsmom-paper-archive] {'ok' if archived else 'disabled_or_failed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
