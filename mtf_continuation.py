"""Paper-only 4H trend / 1H pullback-continuation research engine.

All decisions use completed candles.  A signal made after a 1H close fills no
earlier than the next 1H open.  The module imports no exchange/order client.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from breakout import Candle, atr_series


@dataclass(frozen=True)
class MTFParams:
    four_hour_ema: int = 200
    hourly_fast: int = 20
    hourly_slow: int = 50
    hourly_macro: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    long_rsi_low: float = 45.0
    long_rsi_high: float = 65.0
    short_rsi_low: float = 35.0
    short_rsi_high: float = 55.0
    zone_tolerance_atr: float = 0.10
    min_pattern_body_fraction: float = 0.30
    pinbar_wick_to_body: float = 2.0
    stop_atr: float = 1.50
    reward_risk: float = 2.0
    max_hold_bars: int = 120
    cooldown_bars: int = 4
    desired_notional_usd: float = 10.0
    max_leverage: float = 5.0
    risk_pct: float = 0.02
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
    pattern: str


@dataclass
class Position:
    symbol: str
    side: int
    entry_i: int
    entry_ts: float
    entry: float
    qty: float
    stop: float
    target: float
    entry_fee: float
    entry_equity: float
    pattern: str
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
    pattern: str
    gross_pnl: float
    funding_pnl: float
    fees: float
    net_pnl: float
    planned_risk_usd: float


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
    risk_blocks: int
    minimum_blocks: int
    minimum_floors: int
    daily_loss_blocks: int
    daily_target_blocks: int
    daily_loss_lock_hits: int
    daily_target_lock_hits: int


@dataclass(frozen=True)
class Features:
    ema20: np.ndarray
    ema50: np.ndarray
    ema200: np.ndarray
    four_close: np.ndarray
    four_ema200: np.ndarray
    rsi: np.ndarray
    atr: Tuple[Optional[float], ...]


def align_candles(series: Mapping[str, Sequence[Candle]]) -> Tuple[List[float], Dict[str, List[Candle]]]:
    common: Optional[set[int]] = None
    indexed: Dict[str, Dict[int, Candle]] = {}
    for symbol, candles in series.items():
        rows = {int(round(c.ts)): c for c in candles}
        indexed[symbol] = rows
        common = set(rows) if common is None else common.intersection(rows)
    grid = sorted(common or ())
    return [float(ts) for ts in grid], {
        symbol: [indexed[symbol][ts] for ts in grid] for symbol in sorted(indexed)
    }


def _ema(values: Sequence[float], period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if period <= 0 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, len(values)):
        out[i] = alpha * float(values[i]) + (1.0 - alpha) * out[i - 1]
    return out


def _rsi(values: Sequence[float], period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) <= period:
        return out
    changes = np.diff(np.asarray(values, dtype=float))
    gains = np.maximum(changes, 0.0)
    losses = np.maximum(-changes, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())

    def value(gain, loss):
        if loss <= 1e-15:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = value(avg_gain, avg_loss)
    return out


def _completed_four_hour(candles: Sequence[Candle], ema_period: int):
    """A 4H bucket is visible only after its fourth hourly candle closes."""
    close = np.full(len(candles), np.nan)
    ema = np.full(len(candles), np.nan)
    completed: List[float] = []
    last_close = last_ema = float("nan")
    alpha = 2.0 / (ema_period + 1.0)
    for i, bar in enumerate(candles):
        hour = (int(round(bar.ts)) // 3600) % 24
        if (hour + 1) % 4 == 0:
            completed.append(float(bar.close))
            last_close = completed[-1]
            if len(completed) == ema_period:
                last_ema = float(np.mean(completed[-ema_period:]))
            elif len(completed) > ema_period:
                last_ema = alpha * completed[-1] + (1.0 - alpha) * last_ema
        close[i], ema[i] = last_close, last_ema
    return close, ema


def prepare_features(aligned: Mapping[str, Sequence[Candle]], p: MTFParams):
    result: Dict[str, Features] = {}
    for symbol, candles in aligned.items():
        closes = [c.close for c in candles]
        four = _completed_four_hour(candles, p.four_hour_ema)
        result[symbol] = Features(
            _ema(closes, p.hourly_fast), _ema(closes, p.hourly_slow),
            _ema(closes, p.hourly_macro), four[0], four[1],
            _rsi(closes, p.rsi_period), tuple(atr_series(candles, p.atr_period)),
        )
    return result


def candle_pattern(previous: Candle, current: Candle, side: int, p: MTFParams) -> Optional[str]:
    rng = current.high - current.low
    body = abs(current.close - current.open)
    if rng <= 0:
        return None
    body_floor = p.min_pattern_body_fraction * rng
    if side > 0:
        engulf = (previous.close < previous.open and current.close > current.open
                  and current.open <= previous.close and current.close >= previous.open
                  and body >= body_floor)
        lower, upper = min(current.open, current.close) - current.low, current.high - max(current.open, current.close)
        pin = (current.close > current.open and lower >= p.pinbar_wick_to_body * max(body, 1e-12)
               and upper <= max(body, 1e-12) and current.close >= current.low + 0.65 * rng)
    else:
        engulf = (previous.close > previous.open and current.close < current.open
                  and current.open >= previous.close and current.close <= previous.open
                  and body >= body_floor)
        upper, lower = current.high - max(current.open, current.close), min(current.open, current.close) - current.low
        pin = (current.close < current.open and upper >= p.pinbar_wick_to_body * max(body, 1e-12)
               and lower <= max(body, 1e-12) and current.close <= current.low + 0.35 * rng)
    return "engulfing" if engulf else ("pinbar" if pin else None)


def signals_at(aligned: Mapping[str, Sequence[Candle]], features: Mapping[str, Features],
               i: int, p: MTFParams) -> List[Signal]:
    if i < 1:
        return []
    choices: List[Signal] = []
    for symbol, candles in aligned.items():
        f, bar, previous = features[symbol], candles[i], candles[i - 1]
        atr = f.atr[i]
        required = (f.ema20[i], f.ema50[i], f.ema200[i], f.four_close[i],
                    f.four_ema200[i], f.rsi[i])
        if atr is None or atr <= 0 or not all(math.isfinite(float(v)) for v in required):
            continue
        lower = min(f.ema20[i], f.ema50[i]) - p.zone_tolerance_atr * atr
        upper = max(f.ema20[i], f.ema50[i]) + p.zone_tolerance_atr * atr
        touched = bar.low <= upper and bar.high >= lower
        if not touched:
            continue
        long_ok = (f.four_close[i] > f.four_ema200[i]
                   and bar.close > f.ema200[i]
                   and f.ema20[i] > f.ema50[i]
                   and bar.close > f.ema20[i]
                   and p.long_rsi_low <= f.rsi[i] <= p.long_rsi_high)
        short_ok = (p.allow_short and f.four_close[i] < f.four_ema200[i]
                    and bar.close < f.ema200[i]
                    and f.ema20[i] < f.ema50[i]
                    and bar.close < f.ema20[i]
                    and p.short_rsi_low <= f.rsi[i] <= p.short_rsi_high)
        side = 1 if long_ok else (-1 if short_ok else 0)
        if not side:
            continue
        pattern = candle_pattern(previous, bar, side, p)
        if pattern is None:
            continue
        trend = (abs(f.four_close[i] - f.four_ema200[i])
                 + abs(f.ema20[i] - f.ema50[i])) / float(atr)
        choices.append(Signal(symbol, side, float(trend), float(atr), pattern))
    return sorted(choices, key=lambda s: (s.score, s.symbol), reverse=True)


def _close(pos: Position, price: float, ts: float, reason: str,
           wallet: float, p: MTFParams):
    gross = pos.side * pos.qty * (price - pos.entry)
    exit_fee = abs(pos.qty * price) * p.cost_bps_per_side / 1e4
    wallet += gross - exit_fee
    planned = abs(pos.qty * (pos.entry - pos.stop)) + pos.entry_fee + exit_fee
    trade = Trade(pos.symbol, pos.side, pos.entry_ts, ts, pos.entry, price,
                  pos.qty, reason, pos.pattern, gross, pos.funding_pnl,
                  pos.entry_fee + exit_fee,
                  gross + pos.funding_pnl - pos.entry_fee - exit_fee, planned)
    return wallet, trade, gross - exit_fee


def run(series: Mapping[str, Sequence[Candle]], p: Optional[MTFParams] = None,
        *, starting_equity: float = 15.0, trade_start: int = 0,
        trade_end: Optional[int] = None,
        funding_bps_by_ts: Optional[Mapping[str, Mapping[int, float]]] = None,
        min_notional_by_symbol: Optional[Mapping[str, float]] = None,
        qty_step_by_symbol: Optional[Mapping[str, float]] = None) -> Simulation:
    p = p or MTFParams()
    times, aligned = align_candles(series)
    if not times:
        return Simulation([], [starting_equity], [], starting_equity, 0, 0,
                          0, 0, 0, 0, 0, 0, 0, 0, 0)
    features = prepare_features(aligned, p)
    end = min(len(times), trade_end if trade_end is not None else len(times))
    wallet = float(starting_equity)
    pos: Optional[Position] = None
    pending: Optional[Signal] = None
    pending_exit = False
    cooldown = 0
    trades: List[Trade] = []
    curve: List[float] = []
    curve_ts: List[float] = []
    fees = funding_total = 0.0
    candidates = exposure = risk_blocks = minimum_blocks = minimum_floors = 0
    loss_blocks = target_blocks = loss_hits = target_hits = 0
    current_day: Optional[int] = None
    daily_net = 0.0
    lock: Optional[str] = None
    funding = funding_bps_by_ts or {}

    def update_lock():
        nonlocal lock, loss_hits, target_hits
        if lock is None and daily_net <= p.daily_loss_gate:
            lock, loss_hits = "loss", loss_hits + 1
        elif lock is None and daily_net >= p.daily_profit_target:
            lock, target_hits = "target", target_hits + 1

    for i in range(end):
        ts, active = times[i], i >= trade_start
        day = int(ts // 86400)
        if day != current_day:
            current_day, daily_net, lock = day, 0.0, None
        if cooldown > 0:
            cooldown -= 1

        if active and pos is not None and pending_exit:
            bar = aligned[pos.symbol][i]
            wallet, trade, today = _close(pos, bar.open, ts, "max_hold", wallet, p)
            fees += trade.fees - pos.entry_fee
            daily_net += today
            trades.append(trade)
            pos, pending_exit = None, False
            cooldown = p.cooldown_bars
            update_lock()

        if active and pos is None and pending is not None and cooldown <= 0:
            if lock == "loss":
                loss_blocks += 1
            elif lock == "target":
                target_blocks += 1
            else:
                bar = aligned[pending.symbol][i]
                stop_distance = p.stop_atr * pending.atr
                stop_fraction = stop_distance / bar.open if bar.open > 0 else float("inf")
                fee_fraction = 2.0 * p.cost_bps_per_side / 1e4
                risk_budget = max(0.0, wallet * p.risk_pct)
                by_risk = risk_budget / (stop_fraction + fee_fraction) if stop_fraction > 0 else 0.0
                notional = min(p.desired_notional_usd, wallet * p.max_leverage, by_risk)
                step = float((qty_step_by_symbol or {}).get(pending.symbol, 0.0))
                qty = notional / bar.open if bar.open > 0 else 0.0
                if step > 0 and qty > 0:
                    qty = math.floor(qty / step + 1e-12) * step
                notional = qty * bar.open
                minimum = float((min_notional_by_symbol or {}).get(pending.symbol, 0.0))
                if qty > 0 and notional + 1e-12 < minimum and bar.open > 0:
                    floor_qty = minimum / bar.open
                    if step > 0:
                        floor_qty = math.ceil(floor_qty / step - 1e-12) * step
                    floor_notional = floor_qty * bar.open
                    floor_risk = floor_notional * (stop_fraction + fee_fraction)
                    if (floor_risk <= risk_budget + 1e-12
                            and floor_notional <= wallet * p.max_leverage + 1e-12):
                        qty, notional = floor_qty, floor_notional
                        minimum_floors += 1
                    else:
                        qty = 0.0
                        minimum_blocks += 1
                planned = notional * (stop_fraction + fee_fraction)
                if qty <= 0:
                    pass
                elif planned > risk_budget + 1e-9:
                    risk_blocks += 1
                else:
                    entry_fee = notional * p.cost_bps_per_side / 1e4
                    wallet -= entry_fee
                    fees += entry_fee
                    daily_net -= entry_fee
                    stop = bar.open - pending.side * stop_distance
                    target = bar.open + pending.side * p.reward_risk * stop_distance
                    pos = Position(pending.symbol, pending.side, i, ts, bar.open,
                                   qty, stop, target, entry_fee,
                                   wallet + entry_fee, pending.pattern)
                    update_lock()
        pending = None

        if pos is not None:
            bar = aligned[pos.symbol][i]
            rate = float(funding.get(pos.symbol, {}).get(int(ts), 0.0))
            if rate:
                payment = -pos.side * abs(pos.qty * bar.open) * rate / 1e4
                pos.funding_pnl += payment
                wallet += payment
                daily_net += payment
                funding_total += payment
                update_lock()
            gapped = ((pos.side > 0 and bar.open <= pos.stop) or
                      (pos.side < 0 and bar.open >= pos.stop))
            stop_hit = ((pos.side > 0 and bar.low <= pos.stop) or
                        (pos.side < 0 and bar.high >= pos.stop))
            target_hit = ((pos.side > 0 and bar.high >= pos.target) or
                          (pos.side < 0 and bar.low <= pos.target))
            if gapped or stop_hit:
                price = bar.open if gapped else pos.stop
                reason = "gap_stop" if gapped else "stop"
                wallet, trade, today = _close(pos, price, ts, reason, wallet, p)
                fees += trade.fees - pos.entry_fee
                daily_net += today
                trades.append(trade)
                pos = None
                cooldown = p.cooldown_bars
                update_lock()
            elif target_hit:
                wallet, trade, today = _close(pos, pos.target, ts, "target", wallet, p)
                fees += trade.fees - pos.entry_fee
                daily_net += today
                trades.append(trade)
                pos = None
                cooldown = p.cooldown_bars
                update_lock()

        mark = wallet
        if pos is not None:
            mark += pos.side * pos.qty * (aligned[pos.symbol][i].close - pos.entry)
        if active:
            curve.append(mark)
            curve_ts.append(ts)

        if active and i + 1 < end:
            choices = signals_at(aligned, features, i, p)
            candidates += len(choices)
            if pos is not None:
                if choices:
                    exposure += 1
                if i - pos.entry_i + 1 >= p.max_hold_bars:
                    pending_exit = True
            elif choices:
                if lock == "loss":
                    loss_blocks += 1
                elif lock == "target":
                    target_blocks += 1
                elif cooldown <= 0:
                    pending = choices[0]

    if pos is not None and curve_ts:
        i = end - 1
        wallet, trade, _ = _close(pos, aligned[pos.symbol][i].close,
                                  times[i], "end", wallet, p)
        fees += trade.fees - pos.entry_fee
        trades.append(trade)
        curve[-1] = wallet
    return Simulation(trades, curve or [starting_equity], curve_ts, wallet,
                      fees, funding_total, candidates, exposure, risk_blocks,
                      minimum_blocks, minimum_floors, loss_blocks, target_blocks,
                      loss_hits, target_hits)


def stats(sim: Simulation, starting_equity: float = 15.0) -> Dict[str, float]:
    curve = np.asarray(sim.equity_curve, dtype=float)
    nets = np.asarray([t.net_pnl for t in sim.trades], dtype=float)
    wins, losses = nets[nets > 0], nets[nets <= 0]
    peak = np.maximum.accumulate(curve)
    dd = np.divide(peak - curve, peak, out=np.zeros_like(curve), where=peak > 0)
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    planned = np.asarray([t.planned_risk_usd for t in sim.trades], dtype=float)
    realised_win_r = [t.net_pnl / t.planned_risk_usd for t in sim.trades
                      if t.net_pnl > 0 and t.planned_risk_usd > 0]
    realised_loss_r = [-t.net_pnl / t.planned_risk_usd for t in sim.trades
                       if t.net_pnl <= 0 and t.planned_risk_usd > 0]
    return {
        "trades": float(len(nets)), "wins": float(len(wins)), "losses": float(len(losses)),
        "win_rate": float(len(wins) / len(nets) * 100.0) if len(nets) else 0.0,
        "net_pnl": sim.final_equity - starting_equity, "final_equity": sim.final_equity,
        "profit_factor": (float(wins.sum() / -losses.sum())
                          if len(losses) and losses.sum() < 0 else float("inf")),
        "expectancy_usd": float(nets.mean()) if len(nets) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg_planned_risk": float(planned.mean()) if len(planned) else 0.0,
        "avg_win_r": float(np.mean(realised_win_r)) if realised_win_r else 0.0,
        "avg_loss_r": float(np.mean(realised_loss_r)) if realised_loss_r else 0.0,
        "max_drawdown_pct": float(dd.max() * 100.0) if len(dd) else 0.0,
        "fees": sim.total_fees, "funding": sim.total_funding, "days": days,
        "candidate_signals_per_day": sim.candidate_signals / days if days else 0.0,
        "trades_per_day": len(nets) / days if days else 0.0,
    }
