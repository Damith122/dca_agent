"""Low-frequency, fee-aware time-series momentum engine.

The old bot tried to predict a few basis points over minutes.  This engine
deliberately targets moves that last weeks, so one round trip is small next
to the move being traded.  It holds at most one liquid perpetual at a time,
uses only information available at the previous daily close, fills at the
next daily open, accounts for fees/slippage and historical funding, and sizes
the position from both volatility and stop risk.

The module contains no exchange code.  The historical validator and the
paper/live runner must call these same signal and sizing functions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from breakout import Candle, atr_series


@dataclass(frozen=True)
class TSMOMParams:
    lookback: int = 30
    vol_lookback: int = 30
    atr_period: int = 20
    signal_threshold: float = 0.25
    rebalance_bars: int = 7
    # UTC-day modulo ``rebalance_bars``.  Day zero is 1970-01-01, so offset
    # zero with a seven-day schedule is Thursday.  A timestamp anchor is
    # reproducible in live trading; an array-index anchor is not.
    rebalance_offset: int = 0
    risk_pct: float = 0.02
    annual_vol_target: float = 0.50
    max_leverage: float = 1.0
    stop_atr: float = 3.0
    trail_start_atr: float = 3.0
    trail_atr: float = 2.5
    cooldown_bars: int = 3
    cost_bps_per_side: float = 7.0
    allow_short: bool = True


@dataclass(frozen=True)
class Target:
    symbol: str
    side: int
    score: float
    leverage: float
    atr: float


@dataclass
class Position:
    symbol: str
    side: int
    entry_i: int
    entry_ts: float
    entry: float
    qty: float
    leverage: float
    atr: float
    stop: float
    best: float
    entry_fee: float
    entry_equity: float
    funding_pnl: float = 0.0
    exit_fee: float = 0.0


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: int
    entry_ts: float
    exit_ts: float
    entry: float
    exit: float
    qty: float
    reason: str
    gross_pnl: float
    funding_pnl: float
    fees: float
    net_pnl: float
    entry_equity: float

    @property
    def net_return(self) -> float:
        return self.net_pnl / self.entry_equity if self.entry_equity else 0.0


@dataclass
class Simulation:
    trades: List[Trade]
    equity_curve: List[float]
    timestamps: List[float]
    final_equity: float
    total_fees: float
    total_funding: float
    open_position: Optional[Position] = None
    estimated_exit_fee: float = 0.0
    blocked_min_notional: int = 0
    floored_min_notional: int = 0


def align_candles(series: Mapping[str, Sequence[Candle]]) -> Tuple[List[float], Dict[str, List[Candle]]]:
    """Align symbols on exact daily opens; never forward-fill a trade price."""
    common: Optional[set[int]] = None
    indexed: Dict[str, Dict[int, Candle]] = {}
    for symbol, candles in series.items():
        d = {int(round(c.ts)): c for c in candles}
        indexed[symbol] = d
        common = set(d) if common is None else common.intersection(d)
    grid = sorted(common or ())
    return [float(t) for t in grid], {
        symbol: [indexed[symbol][t] for t in grid]
        for symbol in sorted(indexed)
    }


def _annual_vol(closes: np.ndarray, i: int, lookback: int) -> float:
    if i < lookback or lookback < 2:
        return float("nan")
    r = np.diff(np.log(closes[i - lookback:i + 1]))
    if len(r) < 2:
        return float("nan")
    return float(r.std(ddof=1) * math.sqrt(365.0))


def momentum_score(closes: Sequence[float], i: int, p: TSMOMParams) -> float:
    """Volatility-normalised return using closes no later than ``i``."""
    c = np.asarray(closes, dtype=float)
    need = max(p.lookback, p.vol_lookback)
    if i < need or c[i] <= 0 or c[i - p.lookback] <= 0:
        return float("nan")
    move = math.log(c[i] / c[i - p.lookback])
    r = np.diff(np.log(c[i - p.vol_lookback:i + 1]))
    scale = float(r.std(ddof=1) * math.sqrt(p.lookback)) if len(r) > 1 else 0.0
    return move / scale if scale > 1e-12 else float("nan")


def position_leverage(price: float, atr: float, annual_vol: float,
                      p: TSMOMParams) -> float:
    """Respect both the loss-at-stop budget and the annual volatility target."""
    if price <= 0 or atr <= 0 or annual_vol <= 0 or not math.isfinite(annual_vol):
        return 0.0
    stop_fraction = p.stop_atr * atr / price
    if stop_fraction <= 0:
        return 0.0
    by_stop = p.risk_pct / stop_fraction
    by_vol = p.annual_vol_target / annual_vol
    return max(0.0, min(p.max_leverage, by_stop, by_vol))


def choose_target(aligned: Mapping[str, Sequence[Candle]], atrs: Mapping[str, Sequence[Optional[float]]],
                  i: int, p: TSMOMParams) -> Optional[Target]:
    """Pick the strongest absolute trend; at most one position is allowed."""
    choices: List[Target] = []
    for symbol, candles in aligned.items():
        closes = [c.close for c in candles]
        score = momentum_score(closes, i, p)
        atr = atrs[symbol][i] if i < len(atrs[symbol]) else None
        if not math.isfinite(score) or atr is None or atr <= 0:
            continue
        if abs(score) < p.signal_threshold or (score < 0 and not p.allow_short):
            continue
        ann = _annual_vol(np.asarray(closes), i, p.vol_lookback)
        lev = position_leverage(candles[i].close, float(atr), ann, p)
        if lev > 0:
            choices.append(Target(symbol, 1 if score > 0 else -1,
                                  float(score), lev, float(atr)))
    if not choices:
        return None
    return max(choices, key=lambda t: (abs(t.score), t.symbol))


def _close_position(pos: Position, price: float, ts: float, reason: str,
                    wallet: float, p: TSMOMParams) -> Tuple[float, Trade]:
    gross = pos.side * pos.qty * (price - pos.entry)
    fee = abs(pos.qty * price) * p.cost_bps_per_side / 1e4
    wallet += gross - fee
    trade = Trade(
        symbol=pos.symbol, side=pos.side, entry_ts=pos.entry_ts, exit_ts=ts,
        entry=pos.entry, exit=price, qty=pos.qty, reason=reason,
        gross_pnl=gross, funding_pnl=pos.funding_pnl,
        fees=pos.entry_fee + fee,
        net_pnl=gross + pos.funding_pnl - pos.entry_fee - fee,
        entry_equity=pos.entry_equity,
    )
    return wallet, trade


def run(series: Mapping[str, Sequence[Candle]],
        funding_bps_by_day: Optional[Mapping[str, Mapping[int, float]]] = None,
        p: Optional[TSMOMParams] = None, *, starting_equity: float = 1000.0,
        trade_start: int = 0, trade_end: Optional[int] = None,
        liquidate_at_end: bool = True,
        min_notional_by_symbol: Optional[Mapping[str, float]] = None,
        qty_step_by_symbol: Optional[Mapping[str, float]] = None,
        min_notional_max_risk_pct: Optional[float] = None) -> Simulation:
    """Run the strategy with previous-close signals and next-open fills.

    Funding values are the sum of the settled rates, in basis points, for the
    UTC day beginning at each candle timestamp.  A positive rate is paid by a
    long and received by a short.
    """
    p = p or TSMOMParams()
    times, aligned = align_candles(series)
    if not times:
        return Simulation([], [starting_equity], [], starting_equity, 0.0, 0.0)
    n = len(times)
    end = min(n, trade_end if trade_end is not None else n)
    atrs = {s: atr_series(c, p.atr_period) for s, c in aligned.items()}
    funding = funding_bps_by_day or {}
    wallet = float(starting_equity)
    pos: Optional[Position] = None
    pending: Optional[Target] = None
    pending_ready = False
    cooldown = 0
    trades: List[Trade] = []
    curve: List[float] = []
    curve_ts: List[float] = []
    total_fees = 0.0
    total_funding = 0.0
    blocked_min_notional = 0
    floored_min_notional = 0

    for i in range(n):
        ts = times[i]
        if i >= end:
            break
        active = i >= trade_start

        # A stop crossed by the overnight gap fills at the worse open, never
        # at the stale stop price.
        if pos is not None:
            bar = aligned[pos.symbol][i]
            gapped = ((pos.side > 0 and bar.open <= pos.stop) or
                      (pos.side < 0 and bar.open >= pos.stop))
            if gapped:
                wallet, t = _close_position(pos, bar.open, ts, "gap_stop", wallet, p)
                total_fees += t.fees - pos.entry_fee
                trades.append(t)
                pos = None
                cooldown = p.cooldown_bars

        # Execute the target decided at the previous close.  A same-side,
        # same-symbol target causes no resize and therefore no artificial fee.
        if active and pending_ready and pending is not None and cooldown <= 0:
            same = pos is not None and pos.symbol == pending.symbol and pos.side == pending.side
            if not same and pos is not None:
                bar = aligned[pos.symbol][i]
                wallet, t = _close_position(pos, bar.open, ts, "signal", wallet, p)
                total_fees += t.fees - pos.entry_fee
                trades.append(t)
                pos = None
            if pos is None:
                bar = aligned[pending.symbol][i]
                notional = max(0.0, wallet * pending.leverage)
                qty = notional / bar.open if bar.open > 0 else 0.0
                desired_notional = notional
                step = float((qty_step_by_symbol or {}).get(pending.symbol, 0.0))
                if step > 0 and qty > 0:
                    qty = math.floor(qty / step + 1e-12) * step
                    notional = qty * bar.open
                minimum = float((min_notional_by_symbol or {}).get(
                    pending.symbol, 0.0))
                if desired_notional > 0 and notional + 1e-12 < minimum:
                    min_qty = minimum / bar.open if bar.open > 0 else 0.0
                    if step > 0 and min_qty > 0:
                        min_qty = math.ceil(min_qty / step - 1e-12) * step
                    min_size = min_qty * bar.open
                    stop_fraction = p.stop_atr * pending.atr / bar.open
                    floor_risk = (min_size * stop_fraction / wallet
                                  if wallet > 0 else float("inf"))
                    floor_leverage = min_size / wallet if wallet > 0 else float("inf")
                    can_floor = (
                        min_notional_max_risk_pct is not None
                        and min_notional_max_risk_pct > 0
                        and floor_risk <= min_notional_max_risk_pct
                        and floor_leverage <= p.max_leverage
                    )
                    if can_floor:
                        qty, notional = min_qty, min_size
                        floored_min_notional += 1
                    else:
                        qty = 0.0
                        blocked_min_notional += 1
                if qty > 0:
                    entry_fee = notional * p.cost_bps_per_side / 1e4
                    entry_equity = wallet
                    wallet -= entry_fee
                    total_fees += entry_fee
                    stop = (bar.open - pending.side * p.stop_atr * pending.atr)
                    pos = Position(pending.symbol, pending.side, i, ts, bar.open,
                                   qty, notional / entry_equity, pending.atr, stop,
                                   bar.open, entry_fee, entry_equity)
        elif active and pending_ready and pending is None and pos is not None:
            bar = aligned[pos.symbol][i]
            wallet, t = _close_position(pos, bar.open, ts, "flat_signal", wallet, p)
            total_fees += t.fees - pos.entry_fee
            trades.append(t)
            pos = None

        pending = None
        pending_ready = False
        if cooldown > 0:
            cooldown -= 1

        if pos is not None:
            bar = aligned[pos.symbol][i]
            day = int(ts // 86400 * 86400)
            rate_bps = float(funding.get(pos.symbol, {}).get(day, 0.0))
            fund_pnl = -pos.side * abs(pos.qty * bar.close) * rate_bps / 1e4
            wallet += fund_pnl
            pos.funding_pnl += fund_pnl
            total_funding += fund_pnl

            stopped = ((pos.side > 0 and bar.low <= pos.stop) or
                       (pos.side < 0 and bar.high >= pos.stop))
            if stopped:
                wallet, t = _close_position(pos, pos.stop, ts, "stop", wallet, p)
                total_fees += t.fees - pos.entry_fee
                trades.append(t)
                pos = None
                cooldown = p.cooldown_bars
            else:
                if pos.side > 0:
                    pos.best = max(pos.best, bar.high)
                    if pos.best - pos.entry >= p.trail_start_atr * pos.atr:
                        new_stop = max(pos.stop, pos.best - p.trail_atr * pos.atr)
                        # Daily OHLC does not reveal whether the high or low
                        # occurred first.  If both the newly earned trail and
                        # its breach fit inside the bar, take the adverse
                        # ordering and exit; never award the backtest the
                        # better path through an ambiguous candle.
                        if bar.low <= new_stop:
                            pos.stop = new_stop
                            wallet, t = _close_position(pos, new_stop, ts,
                                                        "trail", wallet, p)
                            total_fees += t.fees - pos.entry_fee
                            trades.append(t)
                            pos = None
                            cooldown = p.cooldown_bars
                        else:
                            pos.stop = new_stop
                else:
                    pos.best = min(pos.best, bar.low)
                    if pos.entry - pos.best >= p.trail_start_atr * pos.atr:
                        new_stop = min(pos.stop, pos.best + p.trail_atr * pos.atr)
                        if bar.high >= new_stop:
                            pos.stop = new_stop
                            wallet, t = _close_position(pos, new_stop, ts,
                                                        "trail", wallet, p)
                            total_fees += t.fees - pos.entry_fee
                            trades.append(t)
                            pos = None
                            cooldown = p.cooldown_bars
                        else:
                            pos.stop = new_stop

        mark = wallet
        if pos is not None:
            close = aligned[pos.symbol][i].close
            mark += pos.side * pos.qty * (close - pos.entry)
        if active:
            curve.append(mark)
            curve_ts.append(ts)

        # Decide only after this bar has closed; the earliest fill is the next
        # bar's open.  Anchor the schedule to the global aligned history.  If
        # every validation fold reset its own weekday, the folds would test
        # different rules and could not be compared.
        cadence = max(1, p.rebalance_bars)
        day_number = int(ts // 86400)
        if (i + 1 < end and active and
                day_number % cadence == p.rebalance_offset % cadence):
            pending = choose_target(aligned, atrs, i, p)
            pending_ready = True

    estimated_exit_fee = 0.0
    if pos is not None and curve_ts and liquidate_at_end:
        i = min(end, n) - 1
        price = aligned[pos.symbol][i].close
        wallet, t = _close_position(pos, price, times[i], "end", wallet, p)
        total_fees += t.fees - pos.entry_fee
        trades.append(t)
        if curve:
            curve[-1] = wallet
    elif pos is not None and curve_ts:
        i = min(end, n) - 1
        price = aligned[pos.symbol][i].close
        estimated_exit_fee = abs(pos.qty * price) * p.cost_bps_per_side / 1e4
        wallet = wallet + pos.side * pos.qty * (price - pos.entry) - estimated_exit_fee
        if curve:
            curve[-1] = wallet

    return Simulation(trades, curve or [starting_equity], curve_ts, wallet,
                      total_fees, total_funding, pos, estimated_exit_fee,
                      blocked_min_notional, floored_min_notional)


def stats(sim: Simulation, starting_equity: float = 1000.0) -> Dict[str, float]:
    curve = np.asarray(sim.equity_curve, dtype=float)
    nets = np.asarray([t.net_pnl for t in sim.trades], dtype=float)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    peak = np.maximum.accumulate(curve)
    dd = np.divide(peak - curve, peak, out=np.zeros_like(curve), where=peak > 0)
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    ret = sim.final_equity / starting_equity - 1.0
    cagr = ((sim.final_equity / starting_equity) ** (365.0 / days) - 1.0
            if days > 0 and sim.final_equity > 0 else 0.0)
    return {
        "trades": float(len(nets)),
        "win_rate": float((nets > 0).mean() * 100) if len(nets) else 0.0,
        "total_return_pct": ret * 100,
        "cagr_pct": cagr * 100,
        "profit_factor": (float(wins.sum() / -losses.sum())
                          if len(losses) and losses.sum() < 0 else float("inf")),
        "expectancy_pct": (float(np.mean([t.net_return for t in sim.trades]) * 100)
                           if sim.trades else 0.0),
        "max_drawdown_pct": float(dd.max() * 100) if len(dd) else 0.0,
        "fees": sim.total_fees,
        "funding": sim.total_funding,
    }
