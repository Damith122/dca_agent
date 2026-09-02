"""Fee-aware 15-minute breakout with completed 1-hour trend confirmation.

The rule is intentionally small and deterministic.  It produces candidate
signals on completed 15-minute candles, fills only at the next candle open,
holds at most one symbol, and models the paper profile's taker fee plus
adverse fill slippage on both sides.  There is no exchange/order code here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from breakout import Candle, atr_series


@dataclass(frozen=True)
class IntradayParams:
    signal_mode: str = "breakout"
    allow_long: bool = True
    allow_short: bool = True
    channel: int = 12
    atr_period: int = 14
    atr_floor_pct: float = 0.0015
    atr_ceiling_pct: float = 0.0200
    volume_lookback: int = 20
    volume_ratio: float = 1.20
    momentum_bars: int = 4
    momentum_floor_pct: float = 0.0010
    hourly_ema_fast: int = 20
    hourly_ema_slow: int = 50
    pullback_ema: int = 20
    rsi_period: int = 14
    stop_pct: float = 0.0025
    target_pct: float = 0.0055
    max_hold_bars: int = 16
    cooldown_bars: int = 4
    notional: float = 10.0
    min_notional: float = 5.0
    max_leverage: float = 5.0
    cost_bps_per_side: float = 7.0
    daily_loss_gate: float = -0.30
    daily_target_gate: float = 0.50


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: int
    score: float
    signal_ts: float


@dataclass
class Position:
    symbol: str
    side: int
    entry_ts: float
    entry: float
    notional: float
    stop: float
    target: float
    entry_i: int
    funding_pnl: float = 0.0


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: int
    entry_ts: float
    exit_ts: float
    entry: float
    exit: float
    notional: float
    reason: str
    gross_pnl: float
    costs: float
    funding_pnl: float
    net_pnl: float


@dataclass
class Simulation:
    trades: List[Trade]
    equity_curve: List[float]
    timestamps: List[float]
    final_equity: float
    candidate_signals: int
    exposure_blocks: int
    daily_gate_blocks: int
    min_notional_blocks: int
    open_position: Optional[Position] = None
    open_fee_net_estimate: float = 0.0


def align_candles(series: Mapping[str, Sequence[Candle]]) -> Tuple[List[float], Dict[str, List[Candle]]]:
    common: Optional[set[int]] = None
    indexed: Dict[str, Dict[int, Candle]] = {}
    for symbol, rows in series.items():
        table = {int(round(c.ts)): c for c in rows}
        indexed[symbol] = table
        common = set(table) if common is None else common.intersection(table)
    grid = sorted(common or ())
    return [float(x) for x in grid], {
        symbol: [indexed[symbol][t] for t in grid]
        for symbol in sorted(indexed)
    }


def hourly_trend(candles: Sequence[Candle], p: IntradayParams) -> List[int]:
    """1/-1/0 using only completed UTC hourly closes available at each bar."""
    out = [0] * len(candles)
    fast = slow = previous_fast = None
    count = 0
    alpha_fast = 2.0 / (p.hourly_ema_fast + 1.0)
    alpha_slow = 2.0 / (p.hourly_ema_slow + 1.0)
    state = 0
    for i, bar in enumerate(candles):
        # A 15m bar timestamp is its OPEN.  The :45 bar is the final bar of
        # that UTC hour, so its close is the first safe hourly close.
        if int(bar.ts + 900) % 3600 == 0:
            previous_fast = fast
            fast = bar.close if fast is None else alpha_fast * bar.close + (1 - alpha_fast) * fast
            slow = bar.close if slow is None else alpha_slow * bar.close + (1 - alpha_slow) * slow
            count += 1
            if count >= p.hourly_ema_slow and previous_fast is not None:
                if fast > slow and fast > previous_fast:
                    state = 1
                elif fast < slow and fast < previous_fast:
                    state = -1
                else:
                    state = 0
        out[i] = state
    return out


def ema_series(candles: Sequence[Candle], period: int) -> List[float]:
    if not candles:
        return []
    alpha = 2.0 / (max(1, period) + 1.0)
    out = [float(candles[0].close)]
    for bar in candles[1:]:
        out.append(alpha * bar.close + (1.0 - alpha) * out[-1])
    return out


def rsi_series(candles: Sequence[Candle], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) <= period:
        return out
    gains, losses = [], []
    for a, b in zip(candles, candles[1:]):
        change = b.close - a.close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(candles)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = (100.0 if avg_loss == 0 else
                  100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return out


def candidate_at(symbol: str, candles: Sequence[Candle], atrs: Sequence[Optional[float]],
                 trend: Sequence[int], i: int, p: IntradayParams) -> Optional[Candidate]:
    need = max(p.channel, p.atr_period, p.volume_lookback, p.momentum_bars)
    if i < need or i >= len(candles):
        return None
    bar = candles[i]
    atr = atrs[i]
    if atr is None or atr <= 0 or bar.close <= 0:
        return None
    atr_pct = atr / bar.close
    if not (p.atr_floor_pct <= atr_pct <= p.atr_ceiling_pct):
        return None
    prior = candles[i - p.channel:i]
    high = max(c.high for c in prior)
    low = min(c.low for c in prior)
    base_volume = median(c.volume for c in candles[i - p.volume_lookback:i])
    vol_ratio = bar.volume / base_volume if base_volume > 0 else 0.0
    if vol_ratio < p.volume_ratio:
        return None
    move = bar.close / candles[i - p.momentum_bars].close - 1.0
    side = 0
    breakout = 0.0
    if bar.close > high and move >= p.momentum_floor_pct and trend[i] > 0:
        side, breakout = 1, (bar.close - high) / atr
    elif bar.close < low and move <= -p.momentum_floor_pct and trend[i] < 0:
        side, breakout = -1, (low - bar.close) / atr
    if not side:
        return None
    score = breakout + abs(move) / max(p.momentum_floor_pct, 1e-12) + vol_ratio
    return Candidate(symbol, side, float(score), bar.ts)


def pullback_candidate_at(symbol: str, candles: Sequence[Candle],
                          atrs: Sequence[Optional[float]], trend: Sequence[int],
                          ema: Sequence[float], rsi: Sequence[Optional[float]],
                          i: int, p: IntradayParams) -> Optional[Candidate]:
    need = max(p.atr_period, p.volume_lookback, p.pullback_ema, p.rsi_period) + 1
    if i < need or atrs[i] is None or rsi[i] is None or rsi[i - 1] is None:
        return None
    bar, previous = candles[i], candles[i - 1]
    atr = float(atrs[i])
    atr_pct = atr / bar.close if bar.close > 0 else 0.0
    if not (p.atr_floor_pct <= atr_pct <= p.atr_ceiling_pct):
        return None
    base_volume = median(c.volume for c in candles[i - p.volume_lookback:i])
    vol_ratio = bar.volume / base_volume if base_volume > 0 else 0.0
    if vol_ratio < p.volume_ratio:
        return None
    side = 0
    if (trend[i] > 0 and previous.close <= ema[i - 1] and bar.close > ema[i]
            and bar.close > bar.open and 45 <= rsi[i] <= 65 and rsi[i] > rsi[i - 1]):
        side = 1
    elif (trend[i] < 0 and previous.close >= ema[i - 1] and bar.close < ema[i]
          and bar.close < bar.open and 35 <= rsi[i] <= 55 and rsi[i] < rsi[i - 1]):
        side = -1
    if not side:
        return None
    score = abs(bar.close - ema[i]) / max(atr, 1e-12) + abs(float(rsi[i]) - 50) / 10 + vol_ratio
    return Candidate(symbol, side, float(score), bar.ts)


def impulse_candidate_at(symbol: str, candles: Sequence[Candle],
                         atrs: Sequence[Optional[float]], trend: Sequence[int],
                         i: int, p: IntradayParams) -> Optional[Candidate]:
    need = max(p.atr_period, p.volume_lookback, p.momentum_bars) + 1
    if i < need or atrs[i] is None:
        return None
    bar = candles[i]
    atr = float(atrs[i])
    atr_pct = atr / bar.close if bar.close > 0 else 0.0
    if not (p.atr_floor_pct <= atr_pct <= p.atr_ceiling_pct):
        return None
    base_volume = median(c.volume for c in candles[i - p.volume_lookback:i])
    vol_ratio = bar.volume / base_volume if base_volume > 0 else 0.0
    if vol_ratio < p.volume_ratio:
        return None
    move = bar.close / candles[i - p.momentum_bars].close - 1.0
    previous_move = candles[i - 1].close / candles[i - 1 - p.momentum_bars].close - 1.0
    side = 0
    if trend[i] > 0 and move >= p.momentum_floor_pct > previous_move:
        side = 1
    elif trend[i] < 0 and move <= -p.momentum_floor_pct < previous_move:
        side = -1
    if not side:
        return None
    score = abs(move) / max(p.momentum_floor_pct, 1e-12) + vol_ratio
    return Candidate(symbol, side, float(score), bar.ts)


def reversion_candidate_at(symbol: str, candles: Sequence[Candle],
                           atrs: Sequence[Optional[float]], trend: Sequence[int],
                           rsi: Sequence[Optional[float]], i: int,
                           p: IntradayParams) -> Optional[Candidate]:
    lookback = 20
    need = max(p.atr_period, p.volume_lookback, p.rsi_period, lookback)
    if i < need or atrs[i] is None or rsi[i] is None or trend[i] != 0:
        return None
    bar = candles[i]
    atr = float(atrs[i])
    atr_pct = atr / bar.close if bar.close > 0 else 0.0
    if not (p.atr_floor_pct <= atr_pct <= p.atr_ceiling_pct):
        return None
    window = np.asarray([c.close for c in candles[i - lookback:i]], dtype=float)
    sd = float(window.std(ddof=1))
    if sd <= 0:
        return None
    z = (bar.close - float(window.mean())) / sd
    side = 0
    if z <= -2.0 and rsi[i] <= 30 and bar.close > bar.open:
        side = 1
    elif z >= 2.0 and rsi[i] >= 70 and bar.close < bar.open:
        side = -1
    if not side:
        return None
    return Candidate(symbol, side, abs(float(z)) + abs(float(rsi[i]) - 50) / 10,
                     bar.ts)


def _close(pos: Position, price: float, ts: float, reason: str,
           p: IntradayParams) -> Trade:
    raw_return = pos.side * (price / pos.entry - 1.0)
    gross = pos.notional * raw_return
    costs = pos.notional * 2.0 * p.cost_bps_per_side / 1e4
    net = gross + pos.funding_pnl - costs
    return Trade(pos.symbol, pos.side, pos.entry_ts, ts, pos.entry, price,
                 pos.notional, reason, gross, costs, pos.funding_pnl, net)


def exit_on_bar(pos: Position, bar: Candle) -> Tuple[Optional[float], str]:
    """Pessimistic intrabar exit; stop wins when stop and target both fit."""
    if pos.side > 0:
        if bar.open <= pos.stop:
            return bar.open, "gap_stop"
        if bar.low <= pos.stop:
            return pos.stop, "stop"
        if bar.high >= pos.target:
            return pos.target, "target"
    else:
        if bar.open >= pos.stop:
            return bar.open, "gap_stop"
        if bar.high >= pos.stop:
            return pos.stop, "stop"
        if bar.low <= pos.target:
            return pos.target, "target"
    return None, ""


def run(series: Mapping[str, Sequence[Candle]], p: Optional[IntradayParams] = None,
        *, starting_equity: float = 15.0,
        funding_bps_by_ts: Optional[Mapping[str, Mapping[int, float]]] = None,
        trade_start: int = 0, trade_end: Optional[int] = None,
        liquidate_at_end: bool = True) -> Simulation:
    p = p or IntradayParams()
    times, aligned = align_candles(series)
    if not times:
        return Simulation([], [starting_equity], [], starting_equity, 0, 0, 0, 0)
    n = len(times)
    end = min(n, trade_end if trade_end is not None else n)
    atrs = {s: atr_series(rows, p.atr_period) for s, rows in aligned.items()}
    trends = {s: hourly_trend(rows, p) for s, rows in aligned.items()}
    emas = {s: ema_series(rows, p.pullback_ema) for s, rows in aligned.items()}
    rsis = {s: rsi_series(rows, p.rsi_period) for s, rows in aligned.items()}
    funding = funding_bps_by_ts or {}

    wallet = float(starting_equity)
    pos: Optional[Position] = None
    pending: Optional[Candidate] = None
    cooldown_until = 0
    trades: List[Trade] = []
    curve: List[float] = []
    curve_ts: List[float] = []
    candidates = exposure_blocks = daily_blocks = min_blocks = 0
    day = None
    daily_realized = 0.0

    for i in range(end):
        ts = times[i]
        current_day = int(ts // 86400)
        if current_day != day:
            day, daily_realized = current_day, 0.0

        # Funding settles on exact UTC timestamps. Existing positions receive
        # it before next-open orders are considered; a new 00:00 order cannot
        # claim funding that settled at that same instant.
        if pos is not None:
            rate = float(funding.get(pos.symbol, {}).get(int(ts), 0.0))
            if rate:
                pos.funding_pnl += -pos.side * pos.notional * rate / 1e4

        # A pending signal was formed only at the previous completed close.
        gate_locked = (daily_realized <= p.daily_loss_gate
                       or daily_realized >= p.daily_target_gate)
        if i >= trade_start and pending is not None and pos is None and i >= cooldown_until:
            if gate_locked:
                daily_blocks += 1
            else:
                notional = min(p.notional, max(0.0, wallet * p.max_leverage))
                if notional + 1e-12 < p.min_notional:
                    min_blocks += 1
                else:
                    bar = aligned[pending.symbol][i]
                    stop = bar.open * (1.0 - pending.side * p.stop_pct)
                    target = bar.open * (1.0 + pending.side * p.target_pct)
                    pos = Position(pending.symbol, pending.side, ts, bar.open,
                                   notional, stop, target, i)
        pending = None

        if pos is not None:
            bar = aligned[pos.symbol][i]
            exit_price, reason = exit_on_bar(pos, bar)
            if exit_price is None and i - pos.entry_i + 1 >= p.max_hold_bars:
                exit_price, reason = bar.close, "max_hold"
            if exit_price is not None:
                trade = _close(pos, exit_price, ts, reason, p)
                wallet += trade.net_pnl
                daily_realized += trade.net_pnl
                trades.append(trade)
                pos = None
                cooldown_until = i + p.cooldown_bars + 1

        mark = wallet
        if pos is not None:
            price = aligned[pos.symbol][i].close
            mark += (pos.side * pos.notional * (price / pos.entry - 1.0)
                     + pos.funding_pnl
                     - pos.notional * 2.0 * p.cost_bps_per_side / 1e4)
        if i >= trade_start:
            curve.append(mark)
            curve_ts.append(ts)

        signals = []
        if i >= trade_start:
            for symbol, rows in aligned.items():
                if p.signal_mode == "pullback":
                    signal = pullback_candidate_at(
                        symbol, rows, atrs[symbol], trends[symbol],
                        emas[symbol], rsis[symbol], i, p)
                elif p.signal_mode == "impulse":
                    signal = impulse_candidate_at(
                        symbol, rows, atrs[symbol], trends[symbol], i, p)
                elif p.signal_mode == "reversion":
                    signal = reversion_candidate_at(
                        symbol, rows, atrs[symbol], trends[symbol],
                        rsis[symbol], i, p)
                else:
                    signal = candidate_at(symbol, rows, atrs[symbol], trends[symbol], i, p)
                if signal is not None and ((signal.side > 0 and not p.allow_long)
                                           or (signal.side < 0 and not p.allow_short)):
                    signal = None
                if signal is not None:
                    signals.append(signal)
            candidates += len(signals)
            if signals:
                if pos is not None:
                    exposure_blocks += len(signals)
                elif daily_realized <= p.daily_loss_gate or daily_realized >= p.daily_target_gate:
                    daily_blocks += len(signals)
                elif i >= cooldown_until and i + 1 < end:
                    pending = max(signals, key=lambda x: (x.score, x.symbol))

    open_estimate = 0.0
    if pos is not None and curve_ts and liquidate_at_end:
        i = end - 1
        trade = _close(pos, aligned[pos.symbol][i].close, times[i], "end", p)
        wallet += trade.net_pnl
        trades.append(trade)
        pos = None
        if curve:
            curve[-1] = wallet
    elif pos is not None and curve_ts:
        i = end - 1
        price = aligned[pos.symbol][i].close
        open_estimate = (pos.side * pos.notional * (price / pos.entry - 1.0)
                         + pos.funding_pnl
                         - pos.notional * 2.0 * p.cost_bps_per_side / 1e4)
        wallet += open_estimate
        if curve:
            curve[-1] = wallet

    return Simulation(trades, curve or [starting_equity], curve_ts, wallet,
                      candidates, exposure_blocks, daily_blocks, min_blocks,
                      pos, open_estimate)


def stats(sim: Simulation, starting_equity: float = 15.0) -> Dict[str, float]:
    nets = np.asarray([t.net_pnl for t in sim.trades], dtype=float)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    curve = np.asarray(sim.equity_curve, dtype=float)
    peak = np.maximum.accumulate(curve)
    dd = np.divide(peak - curve, peak, out=np.zeros_like(curve), where=peak > 0)
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    return {
        "trades": float(len(nets)),
        "wins": float(len(wins)),
        "losses": float(len(losses)),
        "win_rate": float((nets > 0).mean() * 100) if len(nets) else 0.0,
        "net_pnl": float(nets.sum()) if len(nets) else 0.0,
        "return_pct": (sim.final_equity / starting_equity - 1.0) * 100.0,
        "profit_factor": (float(wins.sum() / -losses.sum())
                          if len(losses) and losses.sum() < 0 else float("inf")),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown_pct": float(dd.max() * 100) if len(dd) else 0.0,
        "candidate_signals_per_day": sim.candidate_signals / days if days > 0 else 0.0,
        "trades_per_day": len(nets) / days if days > 0 else 0.0,
        "exposure_blocks": float(sim.exposure_blocks),
        "daily_gate_blocks": float(sim.daily_gate_blocks),
    }
