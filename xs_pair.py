"""Small-wallet cross-sectional relative-strength pair simulator.

One pair is one strategy trade: the strongest risk-adjusted momentum asset is
held long and the weakest is held short with equal dollar notionals.  Signals
use completed daily candles and execute at the next daily open.  The module
contains no exchange client and cannot send orders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from breakout import Candle


@dataclass(frozen=True)
class PairParams:
    lookback_days: int = 30
    skip_days: int = 3
    rebalance_days: int = 7
    rebalance_offset: int = 0
    rank_buffer: int = 2
    max_hold_days: int = 28
    cooldown_days: int = 3
    gross_notional_usd: float = 10.0
    max_rounded_notional_usd: float = 10.50
    max_leverage: float = 5.0
    cost_bps_per_side: float = 7.0
    pair_stop_usd: float = -0.30
    pair_target_usd: float = 0.60
    daily_profit_target: float = 0.50
    daily_loss_gate: float = -0.30


@dataclass(frozen=True)
class PairSignal:
    long_symbol: str
    short_symbol: str
    long_score: float
    short_score: float


@dataclass
class PairPosition:
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
class PairTrade:
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
class PairSimulation:
    trades: List[PairTrade]
    equity_curve: List[float]
    timestamps: List[float]
    final_equity: float
    total_fees: float
    total_funding: float
    candidate_pairs: int
    held_pair_blocks: int
    daily_loss_blocks: int
    daily_target_blocks: int
    daily_loss_lock_hits: int
    daily_target_lock_hits: int
    blocked_min_notional: int


def align_candles(series: Mapping[str, Sequence[Candle]]) -> Tuple[List[float], Dict[str, List[Candle]]]:
    common: Optional[set[int]] = None
    indexed: Dict[str, Dict[int, Candle]] = {}
    for symbol, candles in series.items():
        rows = {int(round(c.ts)): c for c in candles}
        indexed[symbol] = rows
        common = set(rows) if common is None else common.intersection(rows)
    grid = sorted(common or ())
    return [float(ts) for ts in grid], {
        symbol: [indexed[symbol][ts] for ts in grid]
        for symbol in sorted(indexed)
    }


def score_vector(aligned: Mapping[str, Sequence[Candle]], i: int,
                 p: PairParams) -> Dict[str, float]:
    """Volatility-adjusted momentum ending at i-skip, with no lookahead."""
    end = i - p.skip_days
    start = end - p.lookback_days
    if start < 0:
        return {}
    out: Dict[str, float] = {}
    for symbol, rows in aligned.items():
        closes = np.asarray([bar.close for bar in rows[start:end + 1]], dtype=float)
        if len(closes) != p.lookback_days + 1 or np.any(closes <= 0):
            continue
        daily = np.diff(np.log(closes))
        vol = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
        move = math.log(closes[-1] / closes[0])
        scale = vol * math.sqrt(p.lookback_days)
        if scale > 1e-12 and math.isfinite(scale):
            out[symbol] = move / scale
    return out


def choose_pair(scores: Mapping[str, float]) -> Optional[PairSignal]:
    valid = [(symbol, float(score)) for symbol, score in scores.items()
             if math.isfinite(float(score))]
    if len(valid) < 2:
        return None
    valid.sort(key=lambda item: (item[1], item[0]))
    short_symbol, short_score = valid[0]
    long_symbol, long_score = valid[-1]
    if long_symbol == short_symbol:
        return None
    return PairSignal(long_symbol, short_symbol, long_score, short_score)


def pair_inside_buffer(pos: PairPosition, scores: Mapping[str, float],
                       p: PairParams) -> bool:
    ranked = sorted(scores, key=lambda symbol: (scores[symbol], symbol))
    if pos.long_symbol not in ranked or pos.short_symbol not in ranked:
        return False
    width = min(max(1, p.rank_buffer), max(1, len(ranked) // 2))
    return (pos.short_symbol in ranked[:width]
            and pos.long_symbol in ranked[-width:])


def _rounded_leg(price: float, target: float, minimum: float, step: float) -> Tuple[float, float]:
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


def _mark_net(pos: PairPosition, long_price: float, short_price: float,
              p: PairParams) -> float:
    gross = (pos.long_qty * (long_price - pos.long_entry)
             + pos.short_qty * (pos.short_entry - short_price))
    exit_fees = ((pos.long_qty * long_price + pos.short_qty * short_price)
                 * p.cost_bps_per_side / 1e4)
    return gross + pos.funding_pnl - pos.entry_fees - exit_fees


def _close(pos: PairPosition, long_price: float, short_price: float,
           ts: float, reason: str, wallet: float,
           p: PairParams) -> Tuple[float, PairTrade, float]:
    gross = (pos.long_qty * (long_price - pos.long_entry)
             + pos.short_qty * (pos.short_entry - short_price))
    exit_fees = ((pos.long_qty * long_price + pos.short_qty * short_price)
                 * p.cost_bps_per_side / 1e4)
    wallet += gross - exit_fees
    trade = PairTrade(
        pos.long_symbol, pos.short_symbol, pos.entry_ts, ts,
        pos.long_entry, pos.short_entry, long_price, short_price, reason,
        gross, pos.funding_pnl, pos.entry_fees + exit_fees,
        gross + pos.funding_pnl - pos.entry_fees - exit_fees,
        pos.entry_equity,
    )
    return wallet, trade, gross - exit_fees


def run(series: Mapping[str, Sequence[Candle]], p: Optional[PairParams] = None,
        *, starting_equity: float = 15.0, trade_start: int = 0,
        trade_end: Optional[int] = None,
        funding_bps_by_day: Optional[Mapping[str, Mapping[int, float]]] = None,
        min_notional_by_symbol: Optional[Mapping[str, float]] = None,
        qty_step_by_symbol: Optional[Mapping[str, float]] = None) -> PairSimulation:
    p = p or PairParams()
    times, aligned = align_candles(series)
    if not times:
        return PairSimulation([], [starting_equity], [], starting_equity,
                              0.0, 0.0, 0, 0, 0, 0, 0, 0, 0)
    n = len(times)
    end = min(n, trade_end if trade_end is not None else n)
    wallet = float(starting_equity)
    pos: Optional[PairPosition] = None
    pending_signal: Optional[PairSignal] = None
    pending_reason: Optional[str] = None
    cooldown = 0
    trades: List[PairTrade] = []
    curve: List[float] = []
    curve_ts: List[float] = []
    total_fees = total_funding = 0.0
    candidate_pairs = held_pair_blocks = 0
    daily_loss_blocks = daily_target_blocks = 0
    daily_loss_lock_hits = daily_target_lock_hits = 0
    blocked_min_notional = 0
    daily_net = 0.0
    current_day: Optional[int] = None
    lock: Optional[str] = None
    funding = funding_bps_by_day or {}

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
        if cooldown > 0:
            cooldown -= 1

        # Apply the previous completed close's decision at today's open.
        if active and pending_reason is not None and pos is not None:
            long_open = aligned[pos.long_symbol][i].open
            short_open = aligned[pos.short_symbol][i].open
            wallet, trade, today = _close(pos, long_open, short_open, ts,
                                          pending_reason, wallet, p)
            total_fees += trade.fees - pos.entry_fees
            daily_net += today
            trades.append(trade)
            pos = None
            if pending_reason != "rank_rotate":
                cooldown = p.cooldown_days
            update_lock()

        if active and pending_signal is not None and pos is None and cooldown <= 0:
            if lock == "loss":
                daily_loss_blocks += 1
            elif lock == "target":
                daily_target_blocks += 1
            else:
                ls, ss = pending_signal.long_symbol, pending_signal.short_symbol
                lp, sp = aligned[ls][i].open, aligned[ss][i].open
                leg_target = p.gross_notional_usd / 2.0
                lq, ln = _rounded_leg(lp, leg_target,
                                      float((min_notional_by_symbol or {}).get(ls, 0.0)),
                                      float((qty_step_by_symbol or {}).get(ls, 0.0)))
                sq, sn = _rounded_leg(sp, leg_target,
                                      float((min_notional_by_symbol or {}).get(ss, 0.0)),
                                      float((qty_step_by_symbol or {}).get(ss, 0.0)))
                total_notional = ln + sn
                affordable = wallet * p.max_leverage
                if (lq <= 0 or sq <= 0 or total_notional > affordable + 1e-12
                        or total_notional > p.max_rounded_notional_usd + 1e-12):
                    blocked_min_notional += 1
                else:
                    fees = total_notional * p.cost_bps_per_side / 1e4
                    before = wallet
                    wallet -= fees
                    daily_net -= fees
                    total_fees += fees
                    pos = PairPosition(ls, ss, i, ts, lp, sp, lq, sq,
                                       fees, before)
                    update_lock()
        pending_signal = None
        pending_reason = None

        if pos is not None:
            day_ts = int(ts // 86400 * 86400)
            long_close = aligned[pos.long_symbol][i].close
            short_close = aligned[pos.short_symbol][i].close
            long_rate = float(funding.get(pos.long_symbol, {}).get(day_ts, 0.0))
            short_rate = float(funding.get(pos.short_symbol, {}).get(day_ts, 0.0))
            payment = (-pos.long_qty * long_close * long_rate / 1e4
                       + pos.short_qty * short_close * short_rate / 1e4)
            if payment:
                pos.funding_pnl += payment
                wallet += payment
                daily_net += payment
                total_funding += payment
                update_lock()

            net = _mark_net(pos, long_close, short_close, p)
            if net <= p.pair_stop_usd:
                pending_reason = "pair_stop"
            elif net >= p.pair_target_usd:
                pending_reason = "pair_target"
            elif i - pos.entry_i + 1 >= p.max_hold_days:
                pending_reason = "max_hold"

        if active:
            mark = wallet
            if pos is not None:
                mark += (pos.long_qty * (aligned[pos.long_symbol][i].close - pos.long_entry)
                         + pos.short_qty * (pos.short_entry - aligned[pos.short_symbol][i].close))
            curve.append(mark)
            curve_ts.append(ts)

        # Weekly UTC-epoch cadence; compute only after today's close.
        if active and i + 1 < end and day % p.rebalance_days == p.rebalance_offset % p.rebalance_days:
            scores = score_vector(aligned, i, p)
            signal = choose_pair(scores)
            if signal is not None:
                candidate_pairs += 1
                if pos is None:
                    if pending_reason is None and cooldown <= 0:
                        if lock == "loss":
                            daily_loss_blocks += 1
                        elif lock == "target":
                            daily_target_blocks += 1
                        else:
                            pending_signal = signal
                elif pending_reason is None:
                    if pair_inside_buffer(pos, scores, p):
                        held_pair_blocks += 1
                    else:
                        pending_reason = "rank_rotate"
                        pending_signal = signal

    if pos is not None and curve_ts:
        i = end - 1
        wallet, trade, _ = _close(
            pos, aligned[pos.long_symbol][i].close,
            aligned[pos.short_symbol][i].close, times[i], "end", wallet, p)
        total_fees += trade.fees - pos.entry_fees
        trades.append(trade)
        if curve:
            curve[-1] = wallet

    return PairSimulation(trades, curve or [starting_equity], curve_ts, wallet,
                          total_fees, total_funding, candidate_pairs,
                          held_pair_blocks, daily_loss_blocks,
                          daily_target_blocks, daily_loss_lock_hits,
                          daily_target_lock_hits, blocked_min_notional)


def stats(sim: PairSimulation, starting_equity: float = 15.0) -> Dict[str, float]:
    curve = np.asarray(sim.equity_curve, dtype=float)
    nets = np.asarray([trade.net_pnl for trade in sim.trades], dtype=float)
    wins, losses = nets[nets > 0], nets[nets <= 0]
    peak = np.maximum.accumulate(curve)
    dd = np.divide(peak - curve, peak, out=np.zeros_like(curve), where=peak > 0)
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    return {
        "trades": float(len(nets)),
        "wins": float((nets > 0).sum()),
        "losses": float((nets <= 0).sum()),
        "win_rate": float((nets > 0).mean() * 100.0) if len(nets) else 0.0,
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
        "candidate_pairs_per_day": sim.candidate_pairs / days if days > 0 else 0.0,
        "trades_per_day": len(nets) / days if days > 0 else 0.0,
        "estimated_monthly_pnl": ((sim.final_equity - starting_equity) / days * 30.44
                                  if days > 0 else 0.0),
    }
