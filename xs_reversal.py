"""One-day cross-sectional reversal pair simulator for a $15 paper wallet.

At 23:00 UTC, after the hourly candle has completed, rank each asset's
trailing 24-hour return.  Buy the weakest and short the strongest at the next
hour's open, using equal dollar legs.  This module has no exchange client and
cannot place live orders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from breakout import Candle


@dataclass(frozen=True)
class ReversalParams:
    lookback_hours: int = 24
    signal_hour_utc: int = 23
    max_hold_hours: int = 24
    min_dispersion: float = 0.03
    gross_notional_usd: float = 10.0
    max_rounded_notional_usd: float = 10.50
    max_leverage: float = 5.0
    cost_bps_per_side: float = 7.0
    pair_stop_usd: float = -0.30
    pair_target_usd: float = 0.50
    daily_profit_target: float = 0.50
    daily_loss_gate: float = -0.30


@dataclass(frozen=True)
class ReversalSignal:
    long_symbol: str
    short_symbol: str
    long_return: float
    short_return: float
    dispersion: float


@dataclass
class ReversalPosition:
    long_symbol: str
    short_symbol: str
    entry_i: int
    entry_ts: float
    long_entry: float
    short_entry: float
    long_qty: float
    short_qty: float
    entry_fees: float
    entry_equity: float
    funding_pnl: float = 0.0


@dataclass(frozen=True)
class ReversalTrade:
    long_symbol: str
    short_symbol: str
    entry_ts: float
    exit_ts: float
    long_entry: float
    short_entry: float
    long_exit: float
    short_exit: float
    reason: str
    gross_pnl: float
    funding_pnl: float
    fees: float
    net_pnl: float
    entry_equity: float


@dataclass
class ReversalSimulation:
    trades: List[ReversalTrade]
    equity_curve: List[float]
    timestamps: List[float]
    final_equity: float
    total_fees: float
    total_funding: float
    candidate_days: int
    dispersion_blocks: int
    exposure_blocks: int
    daily_loss_blocks: int
    daily_target_blocks: int
    daily_loss_lock_hits: int
    daily_target_lock_hits: int
    blocked_min_notional: int


def align_candles(series: Mapping[str, Sequence[Candle]]) -> Tuple[List[float], Dict[str, List[Candle]]]:
    common: Optional[set[int]] = None
    indexed: Dict[str, Dict[int, Candle]] = {}
    for symbol, candles in series.items():
        rows = {int(round(candle.ts)): candle for candle in candles}
        indexed[symbol] = rows
        common = set(rows) if common is None else common.intersection(rows)
    grid = sorted(common or ())
    return [float(ts) for ts in grid], {
        symbol: [indexed[symbol][ts] for ts in grid]
        for symbol in sorted(indexed)
    }


def return_vector(aligned: Mapping[str, Sequence[Candle]], i: int,
                  lookback_hours: int) -> Dict[str, float]:
    """Trailing close-to-close returns ending at completed bar i."""
    start = i - lookback_hours
    if start < 0:
        return {}
    out: Dict[str, float] = {}
    for symbol, rows in aligned.items():
        old, new = float(rows[start].close), float(rows[i].close)
        if old > 0 and new > 0:
            value = new / old - 1.0
            if math.isfinite(value):
                out[symbol] = value
    return out


def choose_reversal_pair(returns: Mapping[str, float],
                         min_dispersion: float) -> Optional[ReversalSignal]:
    valid = [(symbol, float(value)) for symbol, value in returns.items()
             if math.isfinite(float(value))]
    if len(valid) < 2:
        return None
    valid.sort(key=lambda item: (item[1], item[0]))
    long_symbol, long_return = valid[0]
    short_symbol, short_return = valid[-1]
    dispersion = short_return - long_return
    if long_symbol == short_symbol or dispersion + 1e-12 < min_dispersion:
        return None
    return ReversalSignal(long_symbol, short_symbol, long_return,
                          short_return, dispersion)


def _rounded_leg(price: float, target: float, minimum: float,
                 step: float) -> Tuple[float, float]:
    if price <= 0:
        return 0.0, 0.0
    qty = target / price
    if step > 0:
        qty = math.floor(qty / step + 1e-12) * step
    notional = qty * price
    if notional + 1e-12 < minimum:
        qty = minimum / price
        if step > 0:
            qty = math.ceil(qty / step - 1e-12) * step
        notional = qty * price
    return qty, notional


def _gross(pos: ReversalPosition, long_price: float,
           short_price: float) -> float:
    return (pos.long_qty * (long_price - pos.long_entry)
            + pos.short_qty * (pos.short_entry - short_price))


def _mark_net(pos: ReversalPosition, long_price: float, short_price: float,
              p: ReversalParams) -> float:
    exit_fees = ((pos.long_qty * long_price + pos.short_qty * short_price)
                 * p.cost_bps_per_side / 1e4)
    return (_gross(pos, long_price, short_price) + pos.funding_pnl
            - pos.entry_fees - exit_fees)


def _close(pos: ReversalPosition, long_price: float, short_price: float,
           ts: float, reason: str, wallet: float,
           p: ReversalParams) -> Tuple[float, ReversalTrade, float]:
    gross = _gross(pos, long_price, short_price)
    exit_fees = ((pos.long_qty * long_price + pos.short_qty * short_price)
                 * p.cost_bps_per_side / 1e4)
    wallet += gross - exit_fees
    net = gross + pos.funding_pnl - pos.entry_fees - exit_fees
    trade = ReversalTrade(
        pos.long_symbol, pos.short_symbol, pos.entry_ts, ts,
        pos.long_entry, pos.short_entry, long_price, short_price, reason,
        gross, pos.funding_pnl, pos.entry_fees + exit_fees, net,
        pos.entry_equity,
    )
    return wallet, trade, gross - exit_fees


def run(series: Mapping[str, Sequence[Candle]],
        p: Optional[ReversalParams] = None, *, starting_equity: float = 15.0,
        trade_start: int = 0, trade_end: Optional[int] = None,
        funding_bps_by_ts: Optional[Mapping[str, Mapping[int, float]]] = None,
        min_notional_by_symbol: Optional[Mapping[str, float]] = None,
        qty_step_by_symbol: Optional[Mapping[str, float]] = None,
        close_at_end: bool = True) -> ReversalSimulation:
    p = p or ReversalParams()
    times, aligned = align_candles(series)
    empty = ReversalSimulation([], [starting_equity], [], starting_equity,
                               0.0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0)
    if not times:
        return empty
    end = min(len(times), trade_end if trade_end is not None else len(times))
    wallet = float(starting_equity)
    pos: Optional[ReversalPosition] = None
    pending_signal: Optional[ReversalSignal] = None
    pending_reason: Optional[str] = None
    trades: List[ReversalTrade] = []
    curve: List[float] = []
    curve_ts: List[float] = []
    total_fees = total_funding = 0.0
    candidate_days = dispersion_blocks = exposure_blocks = 0
    daily_loss_blocks = daily_target_blocks = 0
    daily_loss_lock_hits = daily_target_lock_hits = 0
    blocked_min_notional = 0
    current_day: Optional[int] = None
    daily_net = 0.0
    lock: Optional[str] = None
    funding = funding_bps_by_ts or {}

    def update_lock() -> None:
        nonlocal lock, daily_loss_lock_hits, daily_target_lock_hits
        if lock is None and daily_net <= p.daily_loss_gate:
            lock = "loss"
            daily_loss_lock_hits += 1
        elif lock is None and daily_net >= p.daily_profit_target:
            lock = "target"
            daily_target_lock_hits += 1

    for i in range(end):
        ts = times[i]
        active = i >= trade_start
        day = int(ts // 86400)
        if day != current_day:
            current_day, daily_net, lock = day, 0.0, None

        # A completed candle can only trigger an action at the next open.
        if active and pending_reason is not None and pos is not None:
            wallet, trade, today = _close(
                pos, aligned[pos.long_symbol][i].open,
                aligned[pos.short_symbol][i].open, ts, pending_reason,
                wallet, p)
            total_fees += trade.fees - pos.entry_fees
            daily_net += today
            trades.append(trade)
            pos = None
            update_lock()

        if active and pending_signal is not None and pos is None:
            if lock == "loss":
                daily_loss_blocks += 1
            elif lock == "target":
                daily_target_blocks += 1
            else:
                ls, ss = pending_signal.long_symbol, pending_signal.short_symbol
                lp, sp = aligned[ls][i].open, aligned[ss][i].open
                target = p.gross_notional_usd / 2.0
                lq, ln = _rounded_leg(
                    lp, target, float((min_notional_by_symbol or {}).get(ls, 0.0)),
                    float((qty_step_by_symbol or {}).get(ls, 0.0)))
                sq, sn = _rounded_leg(
                    sp, target, float((min_notional_by_symbol or {}).get(ss, 0.0)),
                    float((qty_step_by_symbol or {}).get(ss, 0.0)))
                gross_notional = ln + sn
                if (lq <= 0 or sq <= 0
                        or gross_notional > wallet * p.max_leverage + 1e-12
                        or gross_notional > p.max_rounded_notional_usd + 1e-12):
                    blocked_min_notional += 1
                else:
                    fees = gross_notional * p.cost_bps_per_side / 1e4
                    before = wallet
                    wallet -= fees
                    daily_net -= fees
                    total_fees += fees
                    pos = ReversalPosition(ls, ss, i, ts, lp, sp, lq, sq,
                                           fees, before)
                    update_lock()
        pending_signal = None
        pending_reason = None

        if pos is not None:
            # Use exact published funding timestamps/rates.  A pair entered at
            # the same timestamp is conservatively not treated as having held
            # through that settlement event.
            fts = int(round(ts))
            if pos.entry_ts < ts:
                lp = aligned[pos.long_symbol][i].open
                sp = aligned[pos.short_symbol][i].open
                lr = float(funding.get(pos.long_symbol, {}).get(fts, 0.0))
                sr = float(funding.get(pos.short_symbol, {}).get(fts, 0.0))
                payment = (-pos.long_qty * lp * lr / 1e4
                           + pos.short_qty * sp * sr / 1e4)
                if payment:
                    pos.funding_pnl += payment
                    wallet += payment
                    daily_net += payment
                    total_funding += payment
                    update_lock()

            lc = aligned[pos.long_symbol][i].close
            sc = aligned[pos.short_symbol][i].close
            net = _mark_net(pos, lc, sc, p)
            if net <= p.pair_stop_usd:
                pending_reason = "pair_stop"
            elif net >= p.pair_target_usd:
                pending_reason = "pair_target"
            elif i - pos.entry_i + 1 >= p.max_hold_hours:
                pending_reason = "max_hold"

        if active:
            mark = wallet
            if pos is not None:
                lc = aligned[pos.long_symbol][i].close
                sc = aligned[pos.short_symbol][i].close
                exit_fees = ((pos.long_qty * lc + pos.short_qty * sc)
                             * p.cost_bps_per_side / 1e4)
                mark += _gross(pos, lc, sc) - exit_fees
            curve.append(mark)
            curve_ts.append(ts)

        # 23:00 candle closes at midnight; next bar open is executable.
        hour = int(ts // 3600) % 24
        if active and i + 1 < end and hour == p.signal_hour_utc:
            candidate_days += 1
            values = return_vector(aligned, i, p.lookback_hours)
            signal = choose_reversal_pair(values, p.min_dispersion)
            if signal is None:
                dispersion_blocks += 1
            elif pos is not None and pending_reason is None:
                exposure_blocks += 1
            elif lock == "loss":
                daily_loss_blocks += 1
            elif lock == "target":
                daily_target_blocks += 1
            else:
                pending_signal = signal

    if close_at_end and pos is not None and curve_ts:
        i = end - 1
        wallet, trade, _ = _close(
            pos, aligned[pos.long_symbol][i].close,
            aligned[pos.short_symbol][i].close, times[i], "end", wallet, p)
        total_fees += trade.fees - pos.entry_fees
        trades.append(trade)
        if curve:
            curve[-1] = wallet

    return ReversalSimulation(
        trades, curve or [starting_equity], curve_ts, wallet, total_fees,
        total_funding, candidate_days, dispersion_blocks, exposure_blocks,
        daily_loss_blocks, daily_target_blocks, daily_loss_lock_hits,
        daily_target_lock_hits, blocked_min_notional,
    )


def stats(sim: ReversalSimulation,
          starting_equity: float = 15.0) -> Dict[str, float]:
    curve = np.asarray(sim.equity_curve, dtype=float)
    nets = np.asarray([trade.net_pnl for trade in sim.trades], dtype=float)
    wins, losses = nets[nets > 0], nets[nets <= 0]
    peak = np.maximum.accumulate(curve)
    dd = np.divide(peak - curve, peak, out=np.zeros_like(curve), where=peak > 0)
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    gross = sum(trade.gross_pnl for trade in sim.trades)
    return {
        "trades": float(len(nets)),
        "wins": float((nets > 0).sum()),
        "losses": float((nets <= 0).sum()),
        "win_rate": float((nets > 0).mean() * 100.0) if len(nets) else 0.0,
        "gross_price_pnl": gross,
        "net_pnl": sim.final_equity - starting_equity,
        "final_equity": sim.final_equity,
        "profit_factor": (float(wins.sum() / -losses.sum())
                          if len(losses) and losses.sum() < 0 else float("inf")),
        "expectancy_usd": float(nets.mean()) if len(nets) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown_pct": float(dd.max() * 100.0) if len(dd) else 0.0,
        "fees": sim.total_fees,
        "funding": sim.total_funding,
        "days": days,
        "candidate_days_per_day": sim.candidate_days / days if days > 0 else 0.0,
        "trades_per_day": len(nets) / days if days > 0 else 0.0,
        "estimated_monthly_pnl": ((sim.final_equity - starting_equity) / days * 30.44
                                  if days > 0 else 0.0),
    }
