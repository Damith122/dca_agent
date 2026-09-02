"""Fee-aware 1H/4H/1D trend-pullback research engine.

Signals use only completed candles.  A decision made after an hourly close is
filled no earlier than the next hourly open.  The module contains no exchange
client and cannot place orders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from breakout import Candle, atr_series


@dataclass(frozen=True)
class HTFParams:
    hourly_ema: int = 20
    four_hour_fast: int = 20
    four_hour_slow: int = 50
    daily_fast: int = 20
    daily_slow: int = 50
    atr_period: int = 14
    volume_lookback: int = 20
    volume_ratio: float = 0.80
    min_atr_pct: float = 0.0025
    max_atr_pct: float = 0.0300
    stop_atr: float = 1.50
    target_atr: float = 3.00
    max_hold_bars: int = 48
    cooldown_bars: int = 4
    notional_usd: float = 10.0
    max_leverage: float = 5.0
    cost_bps_per_side: float = 7.0
    daily_profit_target: float = 0.50
    daily_loss_gate: float = -0.30
    allow_short: bool = True


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: int
    score: float
    atr: float


@dataclass
class Position:
    symbol: str
    side: int
    entry_i: int
    entry_ts: float
    entry: float
    qty: float
    atr: float
    stop: float
    target: float
    entry_fee: float
    entry_equity: float
    funding_pnl: float = 0.0


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
    candidate_signals: int
    exposure_blocks: int
    daily_loss_blocks: int
    daily_target_blocks: int
    daily_loss_lock_hits: int
    daily_target_lock_hits: int
    blocked_min_notional: int


@dataclass(frozen=True)
class FeatureSet:
    hourly_ema: np.ndarray
    four_close: np.ndarray
    four_fast: np.ndarray
    four_slow: np.ndarray
    daily_close: np.ndarray
    daily_fast: np.ndarray
    daily_slow: np.ndarray
    atr: Tuple[Optional[float], ...]
    prior_volume_mean: np.ndarray


def align_candles(series: Mapping[str, Sequence[Candle]]) -> Tuple[List[float], Dict[str, List[Candle]]]:
    """Align symbols on exact hourly opens without forward-filled prices."""
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


def _ema(values: Sequence[float], period: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=float)
    if period <= 0 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = float(np.mean(values[:period]))
    out[period - 1] = seed
    for i in range(period, len(values)):
        out[i] = alpha * float(values[i]) + (1.0 - alpha) * out[i - 1]
    return out


def _completed_timeframe(candles: Sequence[Candle], hours: int,
                         fast: int, slow: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Map completed higher-timeframe closes/EMAs onto hourly closes.

    A 4H bucket beginning at 00:00 becomes visible only after the 03:00 hourly
    candle closes.  A daily bucket becomes visible only after 23:00 closes.
    """
    n = len(candles)
    mapped_close = np.full(n, np.nan, dtype=float)
    mapped_fast = np.full(n, np.nan, dtype=float)
    mapped_slow = np.full(n, np.nan, dtype=float)
    completed: List[float] = []
    fast_values: List[float] = []
    slow_values: List[float] = []
    last_close = last_fast = last_slow = float("nan")
    alpha_fast = 2.0 / (fast + 1.0)
    alpha_slow = 2.0 / (slow + 1.0)
    for i, bar in enumerate(candles):
        hour_number = int(round(bar.ts)) // 3600
        if (hour_number + 1) % hours == 0:
            completed.append(float(bar.close))
            if len(completed) == fast:
                fast_values.append(float(np.mean(completed[-fast:])))
            elif len(completed) > fast:
                fast_values.append(alpha_fast * completed[-1]
                                   + (1.0 - alpha_fast) * fast_values[-1])
            if len(completed) == slow:
                slow_values.append(float(np.mean(completed[-slow:])))
            elif len(completed) > slow:
                slow_values.append(alpha_slow * completed[-1]
                                   + (1.0 - alpha_slow) * slow_values[-1])
            last_close = completed[-1]
            if len(completed) >= fast:
                last_fast = fast_values[-1]
            if len(completed) >= slow:
                last_slow = slow_values[-1]
        mapped_close[i] = last_close
        mapped_fast[i] = last_fast
        mapped_slow[i] = last_slow
    return mapped_close, mapped_fast, mapped_slow


def _prior_rolling_mean(values: Sequence[float], lookback: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=float)
    if lookback <= 0:
        return out
    csum = np.concatenate(([0.0], np.cumsum(np.asarray(values, dtype=float))))
    for i in range(lookback, len(values)):
        out[i] = (csum[i] - csum[i - lookback]) / lookback
    return out


def prepare_features(aligned: Mapping[str, Sequence[Candle]],
                     p: HTFParams) -> Dict[str, FeatureSet]:
    out: Dict[str, FeatureSet] = {}
    for symbol, candles in aligned.items():
        closes = [c.close for c in candles]
        four = _completed_timeframe(candles, 4, p.four_hour_fast, p.four_hour_slow)
        daily = _completed_timeframe(candles, 24, p.daily_fast, p.daily_slow)
        out[symbol] = FeatureSet(
            hourly_ema=_ema(closes, p.hourly_ema),
            four_close=four[0], four_fast=four[1], four_slow=four[2],
            daily_close=daily[0], daily_fast=daily[1], daily_slow=daily[2],
            atr=tuple(atr_series(candles, p.atr_period)),
            prior_volume_mean=_prior_rolling_mean([c.volume for c in candles],
                                                   p.volume_lookback),
        )
    return out


def signals_at(aligned: Mapping[str, Sequence[Candle]],
               features: Mapping[str, FeatureSet], i: int,
               p: HTFParams) -> List[Signal]:
    if i <= 0:
        return []
    choices: List[Signal] = []
    for symbol, candles in aligned.items():
        f = features[symbol]
        required = (f.hourly_ema[i], f.hourly_ema[i - 1], f.four_close[i],
                    f.four_fast[i], f.four_slow[i], f.daily_close[i],
                    f.daily_fast[i], f.daily_slow[i], f.prior_volume_mean[i])
        if not all(math.isfinite(float(v)) for v in required):
            continue
        atr = f.atr[i]
        bar, prev = candles[i], candles[i - 1]
        if atr is None or atr <= 0 or bar.close <= 0:
            continue
        atr_pct = float(atr) / bar.close
        if not p.min_atr_pct <= atr_pct <= p.max_atr_pct:
            continue
        volume_mean = f.prior_volume_mean[i]
        if volume_mean <= 0 or bar.volume < p.volume_ratio * volume_mean:
            continue

        long_trend = (f.four_fast[i] > f.four_slow[i]
                      and f.four_close[i] > f.four_fast[i]
                      and f.daily_fast[i] > f.daily_slow[i]
                      and f.daily_close[i] > f.daily_fast[i])
        short_trend = (f.four_fast[i] < f.four_slow[i]
                       and f.four_close[i] < f.four_fast[i]
                       and f.daily_fast[i] < f.daily_slow[i]
                       and f.daily_close[i] < f.daily_fast[i])
        long_resume = (prev.close <= f.hourly_ema[i - 1]
                       and bar.close > f.hourly_ema[i] and bar.close > bar.open)
        short_resume = (prev.close >= f.hourly_ema[i - 1]
                        and bar.close < f.hourly_ema[i] and bar.close < bar.open)
        side = 1 if long_trend and long_resume else 0
        if p.allow_short and short_trend and short_resume:
            side = -1
        if not side:
            continue
        trend_strength = (abs(f.four_fast[i] - f.four_slow[i]) / float(atr)
                          + abs(f.daily_fast[i] - f.daily_slow[i]) / float(atr))
        choices.append(Signal(symbol, side, float(trend_strength), float(atr)))
    return sorted(choices, key=lambda s: (s.score, s.symbol), reverse=True)


def _close(pos: Position, price: float, ts: float, reason: str,
           wallet: float, p: HTFParams) -> Tuple[float, Trade, float]:
    gross = pos.side * pos.qty * (price - pos.entry)
    exit_fee = abs(pos.qty * price) * p.cost_bps_per_side / 1e4
    wallet += gross - exit_fee
    trade = Trade(
        symbol=pos.symbol, side=pos.side, entry_ts=pos.entry_ts, exit_ts=ts,
        entry=pos.entry, exit=price, qty=pos.qty, reason=reason,
        gross_pnl=gross, funding_pnl=pos.funding_pnl,
        fees=pos.entry_fee + exit_fee,
        net_pnl=gross + pos.funding_pnl - pos.entry_fee - exit_fee,
        entry_equity=pos.entry_equity,
    )
    return wallet, trade, gross - exit_fee


def run(series: Mapping[str, Sequence[Candle]], p: Optional[HTFParams] = None,
        *, starting_equity: float = 15.0, trade_start: int = 0,
        trade_end: Optional[int] = None,
        funding_bps_by_ts: Optional[Mapping[str, Mapping[int, float]]] = None,
        min_notional_by_symbol: Optional[Mapping[str, float]] = None,
        qty_step_by_symbol: Optional[Mapping[str, float]] = None) -> Simulation:
    p = p or HTFParams()
    times, aligned = align_candles(series)
    if not times:
        return Simulation([], [starting_equity], [], starting_equity, 0.0, 0.0,
                          0, 0, 0, 0, 0, 0, 0)
    features = prepare_features(aligned, p)
    n = len(times)
    end = min(n, trade_end if trade_end is not None else n)
    wallet = float(starting_equity)
    pos: Optional[Position] = None
    pending: Optional[Signal] = None
    cooldown = 0
    trades: List[Trade] = []
    curve: List[float] = []
    curve_ts: List[float] = []
    total_fees = total_funding = 0.0
    candidate_signals = exposure_blocks = 0
    daily_loss_blocks = daily_target_blocks = 0
    daily_loss_lock_hits = daily_target_lock_hits = 0
    blocked_min_notional = 0
    current_day: Optional[int] = None
    daily_net = 0.0
    lock: Optional[str] = None

    def update_lock() -> None:
        nonlocal lock, daily_loss_lock_hits, daily_target_lock_hits
        if lock is None and daily_net <= p.daily_loss_gate:
            lock = "loss"
            daily_loss_lock_hits += 1
        elif lock is None and daily_net >= p.daily_profit_target:
            lock = "target"
            daily_target_lock_hits += 1

    funding = funding_bps_by_ts or {}
    for i in range(end):
        ts = times[i]
        active = i >= trade_start
        day = int(ts // 86400)
        if day != current_day:
            current_day, daily_net, lock = day, 0.0, None
        if cooldown > 0:
            cooldown -= 1

        # Execute only the previous close's decision.
        if active and pending is not None and pos is None and cooldown <= 0:
            if lock == "loss":
                daily_loss_blocks += 1
            elif lock == "target":
                daily_target_blocks += 1
            else:
                bar = aligned[pending.symbol][i]
                affordable = max(0.0, wallet * p.max_leverage)
                notional = min(p.notional_usd, affordable)
                step = float((qty_step_by_symbol or {}).get(pending.symbol, 0.0))
                qty = notional / bar.open if bar.open > 0 else 0.0
                if step > 0 and qty > 0:
                    qty = math.floor(qty / step + 1e-12) * step
                notional = qty * bar.open
                minimum = float((min_notional_by_symbol or {}).get(pending.symbol, 0.0))
                if qty <= 0 or notional + 1e-12 < minimum:
                    blocked_min_notional += 1
                else:
                    entry_fee = notional * p.cost_bps_per_side / 1e4
                    wallet -= entry_fee
                    daily_net -= entry_fee
                    total_fees += entry_fee
                    pos = Position(
                        pending.symbol, pending.side, i, ts, bar.open, qty,
                        pending.atr,
                        bar.open - pending.side * p.stop_atr * pending.atr,
                        bar.open + pending.side * p.target_atr * pending.atr,
                        entry_fee, wallet + entry_fee,
                    )
                    update_lock()
        pending = None

        if pos is not None:
            bar = aligned[pos.symbol][i]
            rate = float(funding.get(pos.symbol, {}).get(int(ts), 0.0))
            if rate:
                notional = abs(pos.qty * bar.open)
                payment = -pos.side * notional * rate / 1e4
                pos.funding_pnl += payment
                wallet += payment
                daily_net += payment
                total_funding += payment
                update_lock()

            stop_hit = bar.low <= pos.stop if pos.side > 0 else bar.high >= pos.stop
            target_hit = bar.high >= pos.target if pos.side > 0 else bar.low <= pos.target
            if stop_hit:
                # Pessimistic ordering if both levels fit inside one candle.
                price, reason = pos.stop, "stop"
                if ((pos.side > 0 and bar.open <= pos.stop)
                        or (pos.side < 0 and bar.open >= pos.stop)):
                    price, reason = bar.open, "gap_stop"
                wallet, trade, today = _close(pos, price, ts, reason, wallet, p)
                total_fees += trade.fees - pos.entry_fee
                daily_net += today
                trades.append(trade)
                pos = None
                cooldown = p.cooldown_bars
                update_lock()
            elif target_hit:
                wallet, trade, today = _close(pos, pos.target, ts, "target", wallet, p)
                total_fees += trade.fees - pos.entry_fee
                daily_net += today
                trades.append(trade)
                pos = None
                cooldown = p.cooldown_bars
                update_lock()
            elif i - pos.entry_i + 1 >= p.max_hold_bars:
                wallet, trade, today = _close(pos, bar.close, ts, "max_hold", wallet, p)
                total_fees += trade.fees - pos.entry_fee
                daily_net += today
                trades.append(trade)
                pos = None
                cooldown = p.cooldown_bars
                update_lock()

        if active:
            mark = wallet
            if pos is not None:
                close = aligned[pos.symbol][i].close
                mark += pos.side * pos.qty * (close - pos.entry)
            curve.append(mark)
            curve_ts.append(ts)

        if active and i + 1 < end:
            candidates = signals_at(aligned, features, i, p)
            candidate_signals += len(candidates)
            if candidates:
                if pos is not None:
                    exposure_blocks += 1
                elif lock == "loss":
                    daily_loss_blocks += 1
                elif lock == "target":
                    daily_target_blocks += 1
                elif cooldown <= 0:
                    pending = candidates[0]

    if pos is not None and curve_ts:
        i = end - 1
        price = aligned[pos.symbol][i].close
        wallet, trade, _ = _close(pos, price, times[i], "end", wallet, p)
        total_fees += trade.fees - pos.entry_fee
        trades.append(trade)
        if curve:
            curve[-1] = wallet

    return Simulation(trades, curve or [starting_equity], curve_ts, wallet,
                      total_fees, total_funding, candidate_signals,
                      exposure_blocks, daily_loss_blocks, daily_target_blocks,
                      daily_loss_lock_hits, daily_target_lock_hits,
                      blocked_min_notional)


def stats(sim: Simulation, starting_equity: float = 15.0) -> Dict[str, float]:
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
        "wins": float((nets > 0).sum()),
        "losses": float((nets <= 0).sum()),
        "win_rate": float((nets > 0).mean() * 100) if len(nets) else 0.0,
        "net_pnl": sim.final_equity - starting_equity,
        "final_equity": sim.final_equity,
        "total_return_pct": ret * 100.0,
        "cagr_pct": cagr * 100.0,
        "profit_factor": (float(wins.sum() / -losses.sum())
                          if len(losses) and losses.sum() < 0 else float("inf")),
        "expectancy_usd": float(nets.mean()) if len(nets) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown_pct": float(dd.max() * 100.0) if len(dd) else 0.0,
        "fees": sim.total_fees,
        "funding": sim.total_funding,
        "days": days,
        "candidate_signals_per_day": sim.candidate_signals / days if days > 0 else 0.0,
        "trades_per_day": len(nets) / days if days > 0 else 0.0,
    }
