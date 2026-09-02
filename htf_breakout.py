"""Paper-only 4H breakout engine with a completed-1D regime filter.

The signal is evaluated only after a four-hour candle closes and the earliest
fill is the next four-hour open.  Daily EMAs contain completed UTC days only.
This module intentionally has no exchange/order client.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from breakout import Candle, atr_series


@dataclass(frozen=True)
class HTFBreakoutParams:
    channel: int = 40
    exit_channel: int = 20
    daily_fast: int = 20
    daily_slow: int = 50
    atr_period: int = 14
    volume_lookback: int = 20
    volume_ratio: float = 1.0
    min_atr_pct: float = 0.004
    max_atr_pct: float = 0.060
    stop_atr: float = 2.0
    trail_start_atr: float = 3.0
    trail_atr: float = 2.5
    max_hold_bars: int = 126
    cooldown_bars: int = 3
    notional_usd: float = 10.0
    max_leverage: float = 5.0
    max_stop_risk_pct: float = 0.03
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
    best: float
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
    min_notional_blocks: int
    daily_loss_blocks: int
    daily_target_blocks: int
    daily_loss_lock_hits: int
    daily_target_lock_hits: int


@dataclass(frozen=True)
class Features:
    atr: Tuple[Optional[float], ...]
    prior_volume: np.ndarray
    daily_close: np.ndarray
    daily_fast: np.ndarray
    daily_slow: np.ndarray


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


def _completed_daily(candles: Sequence[Candle], fast: int, slow: int):
    """Map completed UTC-day closes and EMAs onto 4H closes without lookahead."""
    n = len(candles)
    mapped_close = np.full(n, np.nan)
    mapped_fast = np.full(n, np.nan)
    mapped_slow = np.full(n, np.nan)
    completed: List[float] = []
    ema_fast = ema_slow = float("nan")
    af, ass = 2.0 / (fast + 1.0), 2.0 / (slow + 1.0)
    last_close = float("nan")
    for i, bar in enumerate(candles):
        # Binance 4H bars open 00,04,...,20 UTC.  The 20:00 bar completes day.
        hour = (int(round(bar.ts)) // 3600) % 24
        if hour == 20:
            completed.append(float(bar.close))
            last_close = completed[-1]
            if len(completed) == fast:
                ema_fast = float(np.mean(completed[-fast:]))
            elif len(completed) > fast:
                ema_fast = af * completed[-1] + (1.0 - af) * ema_fast
            if len(completed) == slow:
                ema_slow = float(np.mean(completed[-slow:]))
            elif len(completed) > slow:
                ema_slow = ass * completed[-1] + (1.0 - ass) * ema_slow
        mapped_close[i] = last_close
        mapped_fast[i] = ema_fast
        mapped_slow[i] = ema_slow
    return mapped_close, mapped_fast, mapped_slow


def _prior_mean(values: Sequence[float], lookback: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    a = np.asarray(values, dtype=float)
    csum = np.concatenate(([0.0], np.cumsum(a)))
    for i in range(lookback, len(values)):
        out[i] = (csum[i] - csum[i - lookback]) / lookback
    return out


def prepare_features(aligned: Mapping[str, Sequence[Candle]], p: HTFBreakoutParams):
    out: Dict[str, Features] = {}
    for symbol, candles in aligned.items():
        daily = _completed_daily(candles, p.daily_fast, p.daily_slow)
        out[symbol] = Features(
            tuple(atr_series(candles, p.atr_period)),
            _prior_mean([c.volume for c in candles], p.volume_lookback),
            daily[0], daily[1], daily[2],
        )
    return out


def _channel(candles: Sequence[Candle], i: int, lookback: int):
    if i < lookback:
        return None, None
    window = candles[i - lookback:i]
    return max(c.high for c in window), min(c.low for c in window)


def signals_at(aligned: Mapping[str, Sequence[Candle]], features: Mapping[str, Features],
               i: int, p: HTFBreakoutParams) -> List[Signal]:
    choices: List[Signal] = []
    for symbol, candles in aligned.items():
        f, bar = features[symbol], candles[i]
        atr = f.atr[i]
        required = (f.daily_close[i], f.daily_fast[i], f.daily_slow[i],
                    f.prior_volume[i])
        if atr is None or atr <= 0 or not all(math.isfinite(float(v)) for v in required):
            continue
        atr_pct = float(atr) / bar.close if bar.close > 0 else 0.0
        if not p.min_atr_pct <= atr_pct <= p.max_atr_pct:
            continue
        if f.prior_volume[i] <= 0 or bar.volume < p.volume_ratio * f.prior_volume[i]:
            continue
        hi, lo = _channel(candles, i, p.channel)
        if hi is None:
            continue
        long_regime = f.daily_fast[i] > f.daily_slow[i] and f.daily_close[i] > f.daily_fast[i]
        short_regime = f.daily_fast[i] < f.daily_slow[i] and f.daily_close[i] < f.daily_fast[i]
        side = 1 if long_regime and bar.close > hi else 0
        if p.allow_short and short_regime and bar.close < lo:
            side = -1
        if side:
            boundary = hi if side > 0 else lo
            expansion = side * (bar.close - boundary) / float(atr)
            regime = abs(f.daily_fast[i] - f.daily_slow[i]) / float(atr)
            choices.append(Signal(symbol, side, expansion + 0.25 * regime, float(atr)))
    return sorted(choices, key=lambda s: (s.score, s.symbol), reverse=True)


def _close(pos: Position, price: float, ts: float, reason: str, wallet: float,
           p: HTFBreakoutParams):
    gross = pos.side * pos.qty * (price - pos.entry)
    exit_fee = abs(pos.qty * price) * p.cost_bps_per_side / 1e4
    wallet += gross - exit_fee
    trade = Trade(pos.symbol, pos.side, pos.entry_ts, ts, pos.entry, price,
                  pos.qty, reason, gross, pos.funding_pnl,
                  pos.entry_fee + exit_fee,
                  gross + pos.funding_pnl - pos.entry_fee - exit_fee,
                  pos.entry_equity)
    return wallet, trade, gross - exit_fee


def run(series: Mapping[str, Sequence[Candle]], p: Optional[HTFBreakoutParams] = None,
        *, starting_equity: float = 15.0, trade_start: int = 0,
        trade_end: Optional[int] = None,
        funding_bps_by_ts: Optional[Mapping[str, Mapping[int, float]]] = None,
        min_notional_by_symbol: Optional[Mapping[str, float]] = None,
        qty_step_by_symbol: Optional[Mapping[str, float]] = None) -> Simulation:
    p = p or HTFBreakoutParams()
    times, aligned = align_candles(series)
    if not times:
        return Simulation([], [starting_equity], [], starting_equity, 0, 0,
                          0, 0, 0, 0, 0, 0, 0, 0)
    features = prepare_features(aligned, p)
    end = min(len(times), trade_end if trade_end is not None else len(times))
    wallet, pos, pending_entry = float(starting_equity), None, None
    pending_exit: Optional[str] = None
    cooldown = 0
    trades: List[Trade] = []
    curve: List[float] = []
    curve_ts: List[float] = []
    fees = funding_total = 0.0
    signals = exposure = risk_blocks = min_blocks = 0
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
        ts = times[i]
        active = i >= trade_start
        day = int(ts // 86400)
        if day != current_day:
            current_day, daily_net, lock = day, 0.0, None
        if cooldown > 0:
            cooldown -= 1

        # Decisions made at the previous 4H close execute at this 4H open.
        if active and pos is not None and pending_exit is not None:
            bar = aligned[pos.symbol][i]
            wallet, trade, today = _close(pos, bar.open, ts, pending_exit, wallet, p)
            fees += trade.fees - pos.entry_fee
            daily_net += today
            trades.append(trade)
            pos, pending_exit = None, None
            cooldown = p.cooldown_bars
            update_lock()
        if active and pos is None and pending_entry is not None and cooldown <= 0:
            if lock == "loss":
                loss_blocks += 1
            elif lock == "target":
                target_blocks += 1
            else:
                bar = aligned[pending_entry.symbol][i]
                notional = min(p.notional_usd, max(0.0, wallet * p.max_leverage))
                qty = notional / bar.open if bar.open > 0 else 0.0
                step = float((qty_step_by_symbol or {}).get(pending_entry.symbol, 0.0))
                if step > 0 and qty > 0:
                    qty = math.floor(qty / step + 1e-12) * step
                notional = qty * bar.open
                minimum = float((min_notional_by_symbol or {}).get(pending_entry.symbol, 0.0))
                planned_loss = qty * p.stop_atr * pending_entry.atr
                planned_fees = notional * 2.0 * p.cost_bps_per_side / 1e4
                if qty <= 0 or notional + 1e-12 < minimum:
                    min_blocks += 1
                elif planned_loss + planned_fees > wallet * p.max_stop_risk_pct:
                    risk_blocks += 1
                else:
                    entry_fee = notional * p.cost_bps_per_side / 1e4
                    wallet -= entry_fee
                    fees += entry_fee
                    daily_net -= entry_fee
                    pos = Position(pending_entry.symbol, pending_entry.side, i, ts,
                                   bar.open, qty, pending_entry.atr,
                                   bar.open - pending_entry.side * p.stop_atr * pending_entry.atr,
                                   bar.open, entry_fee, wallet + entry_fee)
                    update_lock()
        pending_entry = None

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
            else:
                if pos.side > 0:
                    pos.best = max(pos.best, bar.high)
                    if pos.best - pos.entry >= p.trail_start_atr * pos.atr:
                        new_stop = max(pos.stop, pos.best - p.trail_atr * pos.atr)
                        if bar.low <= new_stop:  # pessimistic intrabar ordering
                            pos.stop = new_stop
                            wallet, trade, today = _close(pos, new_stop, ts, "trail", wallet, p)
                            fees += trade.fees - pos.entry_fee
                            daily_net += today
                            trades.append(trade)
                            pos = None
                            cooldown = p.cooldown_bars
                            update_lock()
                        else:
                            pos.stop = new_stop
                else:
                    pos.best = min(pos.best, bar.low)
                    if pos.entry - pos.best >= p.trail_start_atr * pos.atr:
                        new_stop = min(pos.stop, pos.best + p.trail_atr * pos.atr)
                        if bar.high >= new_stop:
                            pos.stop = new_stop
                            wallet, trade, today = _close(pos, new_stop, ts, "trail", wallet, p)
                            fees += trade.fees - pos.entry_fee
                            daily_net += today
                            trades.append(trade)
                            pos = None
                            cooldown = p.cooldown_bars
                            update_lock()
                        else:
                            pos.stop = new_stop

        mark = wallet
        if pos is not None:
            mark += pos.side * pos.qty * (aligned[pos.symbol][i].close - pos.entry)
        if active:
            curve.append(mark)
            curve_ts.append(ts)

        if active and i + 1 < end:
            choices = signals_at(aligned, features, i, p)
            signals += len(choices)
            if pos is not None:
                if choices:
                    exposure += 1
                _, exit_lo = _channel(aligned[pos.symbol], i, p.exit_channel)
                exit_hi, _ = _channel(aligned[pos.symbol], i, p.exit_channel)
                close = aligned[pos.symbol][i].close
                channel_exit = ((pos.side > 0 and exit_lo is not None and close < exit_lo) or
                                (pos.side < 0 and exit_hi is not None and close > exit_hi))
                if channel_exit:
                    pending_exit = "channel_exit"
                elif i - pos.entry_i + 1 >= p.max_hold_bars:
                    pending_exit = "max_hold"
            elif choices:
                if lock == "loss":
                    loss_blocks += 1
                elif lock == "target":
                    target_blocks += 1
                elif cooldown <= 0:
                    pending_entry = choices[0]

    if pos is not None and curve_ts:
        i = end - 1
        wallet, trade, _ = _close(pos, aligned[pos.symbol][i].close,
                                  times[i], "end", wallet, p)
        fees += trade.fees - pos.entry_fee
        trades.append(trade)
        curve[-1] = wallet
    return Simulation(trades, curve or [starting_equity], curve_ts, wallet,
                      fees, funding_total, signals, exposure, risk_blocks,
                      min_blocks, loss_blocks, target_blocks, loss_hits, target_hits)


def stats(sim: Simulation, starting_equity: float = 15.0) -> Dict[str, float]:
    curve = np.asarray(sim.equity_curve, dtype=float)
    nets = np.asarray([t.net_pnl for t in sim.trades], dtype=float)
    wins, losses = nets[nets > 0], nets[nets <= 0]
    peak = np.maximum.accumulate(curve)
    dd = np.divide(peak - curve, peak, out=np.zeros_like(curve), where=peak > 0)
    days = ((sim.timestamps[-1] - sim.timestamps[0]) / 86400.0
            if len(sim.timestamps) > 1 else 0.0)
    return {
        "trades": float(len(nets)), "wins": float(len(wins)), "losses": float(len(losses)),
        "win_rate": float(len(wins) / len(nets) * 100.0) if len(nets) else 0.0,
        "net_pnl": sim.final_equity - starting_equity,
        "final_equity": sim.final_equity,
        "profit_factor": (float(wins.sum() / -losses.sum())
                          if len(losses) and losses.sum() < 0 else float("inf")),
        "expectancy_usd": float(nets.mean()) if len(nets) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown_pct": float(dd.max() * 100.0) if len(dd) else 0.0,
        "fees": sim.total_fees, "funding": sim.total_funding, "days": days,
        "candidate_signals_per_day": sim.candidate_signals / days if days else 0.0,
        "trades_per_day": len(nets) / days if days else 0.0,
    }
