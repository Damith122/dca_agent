"""Final accounting snapshot for bounded dry-run experiments.

This module deliberately has no bot/runtime dependencies so its arithmetic
can be tested offline. It reads manager state but never mutates it.
"""

import json
from typing import Mapping


def build_paper_summary(portfolio, managers: Mapping[str, object], starting_balance: float) -> dict:
    """Build one deterministic, machine-readable result for a paper run.

    A bounded paper experiment can end while a simulated position is still
    open. Previously its final log then said only that the timer expired;
    ``trades=0`` / ``session_pnl=0`` looked like no economic result even
    though entry commission and unrealised PnL existed. This snapshot keeps
    realised outcomes separate from open-position mark-to-market. It is
    diagnostic only: it never submits a close, mutates a position, or feeds
    an artificial timeout label into the Brain.
    """
    closed_trades = 0
    wins = 0
    losses = 0
    realized_fee_net = 0.0
    open_fee_net_estimate = 0.0
    open_positions = []

    for symbol, manager in sorted(managers.items()):
        manager_closed = max(0, int(getattr(manager, "trade_count", 0)))
        closed_trades += manager_closed
        realized_fee_net += float(getattr(manager, "realized_pnl_total", 0.0))

        # PerformanceStats is the same fee-net source used by the normal
        # runtime stats line. The paper launcher starts in its own fresh
        # namespace, so this is the run's win/loss split, not live history.
        try:
            stats = manager.perf_stats.compute()
            manager_wins = int(round(float(stats.get("win_rate", 0.0)) * manager_closed))
        except Exception:  # a summary must survive optional stats I/O failure
            manager_wins = 0
        manager_wins = min(max(manager_wins, 0), manager_closed)
        wins += manager_wins
        losses += manager_closed - manager_wins

        position = manager.position
        if (
            getattr(position, "status", "FLAT") == "FLAT"
            or not getattr(position, "avg_entry_price", None)
            or float(getattr(position, "total_qty", 0.0)) <= 0
        ):
            continue

        side = getattr(position, "side", None)
        if side == "LONG":
            mark = getattr(manager, "best_bid_price", None)
        else:
            mark = getattr(manager, "best_ask_price", None)
        mark = mark or getattr(manager, "current_price", None) or position.avg_entry_price

        # Use the bot's canonical conservative, fee-aware executable-price
        # estimator. This includes accumulated entry commission when it is
        # reliable plus estimated close commission. It does NOT force a fill
        # at the arbitrary experiment cutoff.
        try:
            fee_net_estimate = float(manager.estimate_net_pnl_usdt_executable())
        except Exception:  # retain a useful fallback result
            fee_net_estimate = float(manager.estimate_net_pnl_usdt(float(mark)))
        open_fee_net_estimate += fee_net_estimate
        open_positions.append({
            "symbol": symbol,
            "status": position.status,
            "side": side,
            "qty": round(float(position.total_qty), 12),
            "avg_entry": round(float(position.avg_entry_price), 12),
            "mark": round(float(mark), 12),
            "fee_net_pnl_estimate_usdt": round(fee_net_estimate, 8),
        })

    estimated_total = realized_fee_net + open_fee_net_estimate
    return {
        "schema": "paper_summary_v1",
        "starting_balance_usdt": round(float(starting_balance), 8),
        "paper_cash_wallet_usdt": round(float(portfolio.paper_wallet), 8),
        "closed_trades": closed_trades,
        "wins": wins,
        "losses": losses,
        "realized_fee_net_pnl_usdt": round(realized_fee_net, 8),
        "open_position_count": len(open_positions),
        "open_positions": open_positions,
        "open_fee_net_pnl_estimate_usdt": round(open_fee_net_estimate, 8),
        "estimated_total_fee_net_pnl_usdt": round(estimated_total, 8),
        "estimated_equity_if_flattened_usdt": round(
            float(starting_balance) + estimated_total, 8,
        ),
    }


def print_paper_summary(portfolio, managers: Mapping[str, object], starting_balance: float) -> dict:
    """Print and return the final paper snapshot for Railway logs."""
    summary = build_paper_summary(portfolio, managers, starting_balance)
    print("[paper-summary] " + json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return summary
