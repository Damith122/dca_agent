"""Donchian breakout engine, shared by the backtest and the live bot.

Why one module
--------------
A backtest that runs different code from production tests nothing. Every
entry, exit, stop and size decision lives here, and both `backtest_breakout.py`
and the live trading loop call the SAME functions on the same candle objects.
If you change a rule, both move together or neither does.

The strategy
------------
Classic Donchian channel breakout with volatility-scaled risk:

  ENTRY   close breaks the highest high (long) or lowest low (short) of the
          previous `channel` bars. The breakout bar's close is the signal;
          the fill is the NEXT bar's open, because at signal time the current
          bar has not closed yet in live trading.
  FILTER  ATR as a fraction of price must sit inside [atr_floor, atr_ceiling].
          Below the floor there is no move to catch and the fee eats it;
          above the ceiling stops get taken out by noise.
  STOP    entry -/+ stop_atr * ATR. Fixed at entry, never widened. Widening a
          stop is how a small loss becomes an account.
  TRAIL   once price has moved trail_start_atr * ATR in favour, a trailing
          stop follows at trail_atr * ATR from the best price reached.
  TARGET  hard exit at tp_atr * ATR if it gets there first.
  SIZE    risk_pct of equity divided by the stop distance, so every trade
          risks the same fraction of equity regardless of volatility. Capped
          at max_leverage to keep a tight stop from demanding absurd notional.

Intrabar ordering is deliberately pessimistic: if a bar's range contains both
the stop and the target, the STOP is assumed to have hit first. Without that
assumption a backtest quietly awards itself the better of two outcomes on
every ambiguous bar, which is where most fictitious edge comes from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class BreakoutParams:
    channel: int = 20            # Donchian lookback, in bars
    atr_period: int = 14
    atr_floor: float = 0.0025    # 25 bps - below this the fee dominates
    atr_ceiling: float = 0.040   # 4% - above this stops are noise-driven
    stop_atr: float = 1.5
    tp_atr: float = 6.0
    trail_atr: float = 2.0
    trail_start_atr: float = 2.0
    risk_pct: float = 0.01       # 1% of equity risked per trade
    max_leverage: float = 5.0
    fee_bps_round_trip: float = 7.32
    allow_short: bool = True
    cooldown_bars: int = 1       # bars to wait after an exit before re-entering


@dataclass
class Trade:
    side: str
    entry_ts: float
    entry: float
    exit_ts: float = 0.0
    exit: float = 0.0
    qty: float = 0.0
    reason: str = ""
    mfe: float = 0.0
    mae: float = 0.0
    # Stop distance as a fraction of entry, captured when the trade opened.
    # This is what makes an R-multiple exact: 1R is the planned loss, so a
    # full stop-out is -1R by construction and a trailed exit is whatever
    # fraction of it actually happened. Deriving R from average percentages
    # afterwards gets this wrong whenever volatility varies between trades.
    risk_frac: float = 0.0

    def r_multiple(self, fee_bps: float) -> float:
        """Net result in units of the risk taken. Zero risk_frac means the
        trade predates this field; report 0 rather than divide by zero."""
        if self.risk_frac <= 0:
            return 0.0
        return (self.gross_pct - fee_bps / 1e4) / self.risk_frac

    @property
    def gross_pct(self) -> float:
        if not self.exit or not self.entry:
            return 0.0
        raw = (self.exit - self.entry) / self.entry
        return raw if self.side == "LONG" else -raw


def true_range(prev_close: float, c: Candle) -> float:
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def atr_series(candles: Sequence[Candle], period: int) -> List[Optional[float]]:
    """Wilder's ATR. Index i uses candles[0..i] only - no lookahead."""
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) < period + 1:
        return out
    trs = [true_range(candles[i - 1].close, candles[i]) for i in range(1, len(candles))]
    running = sum(trs[:period]) / period
    out[period] = running
    for i in range(period + 1, len(candles)):
        running = (running * (period - 1) + trs[i - 1]) / period
        out[i] = running
    return out


def donchian(candles: Sequence[Candle], i: int, channel: int):
    """Highest high / lowest low of the `channel` bars BEFORE i.

    The current bar is excluded on purpose: including it makes every bar its
    own breakout and produces a backtest that cannot lose.
    """
    if i < channel:
        return None, None
    window = candles[i - channel:i]
    return max(c.high for c in window), min(c.low for c in window)


def position_size(equity: float, entry: float, stop: float, p: BreakoutParams) -> float:
    """Quantity such that a stop-out costs exactly risk_pct of equity."""
    distance = abs(entry - stop)
    if distance <= 0 or entry <= 0:
        return 0.0
    qty = (equity * p.risk_pct) / distance
    max_qty = (equity * p.max_leverage) / entry
    return min(qty, max_qty)


def entry_signal(candles: Sequence[Candle], i: int, atr: Optional[float],
                 p: BreakoutParams) -> Optional[str]:
    """Signal on bar i's close, to be filled at bar i+1's open. Returns
    "LONG", "SHORT" or None."""
    if atr is None or atr <= 0:
        return None
    c = candles[i]
    atr_pct = atr / c.close if c.close else 0.0
    if not (p.atr_floor <= atr_pct <= p.atr_ceiling):
        return None
    hi, lo = donchian(candles, i, p.channel)
    if hi is None:
        return None
    if c.close > hi:
        return "LONG"
    if p.allow_short and c.close < lo:
        return "SHORT"
    return None


@dataclass
class OpenPosition:
    side: str
    entry: float
    entry_ts: float
    qty: float
    atr: float
    stop: float
    target: float
    best: float
    trailing: bool = False
    mfe: float = 0.0
    mae: float = 0.0


def open_position(side: str, fill: float, ts: float, atr: float, equity: float,
                  p: BreakoutParams) -> Optional[OpenPosition]:
    if side == "LONG":
        stop = fill - p.stop_atr * atr
        target = fill + p.tp_atr * atr
    else:
        stop = fill + p.stop_atr * atr
        target = fill - p.tp_atr * atr
    qty = position_size(equity, fill, stop, p)
    if qty <= 0:
        return None
    return OpenPosition(side=side, entry=fill, entry_ts=ts, qty=qty, atr=atr,
                        stop=stop, target=target, best=fill)


def update_position(pos: OpenPosition, c: Candle, p: BreakoutParams):
    """Walk one bar. Returns (exit_price, reason) or (None, "") if still open.

    Pessimistic ordering: when a bar could have hit both the stop and the
    target, the stop wins.
    """
    if pos.side == "LONG":
        pos.mfe = max(pos.mfe, (c.high - pos.entry) / pos.entry)
        pos.mae = min(pos.mae, (c.low - pos.entry) / pos.entry)
        if c.low <= pos.stop:
            return pos.stop, "trail" if pos.trailing else "stop"
        if c.high >= pos.target:
            return pos.target, "target"
        pos.best = max(pos.best, c.high)
        if not pos.trailing and pos.best - pos.entry >= p.trail_start_atr * pos.atr:
            pos.trailing = True
        if pos.trailing:
            pos.stop = max(pos.stop, pos.best - p.trail_atr * pos.atr)
    else:
        pos.mfe = max(pos.mfe, (pos.entry - c.low) / pos.entry)
        pos.mae = min(pos.mae, (pos.entry - c.high) / pos.entry)
        if c.high >= pos.stop:
            return pos.stop, "trail" if pos.trailing else "stop"
        if c.low <= pos.target:
            return pos.target, "target"
        pos.best = min(pos.best, c.low)
        if not pos.trailing and pos.entry - pos.best >= p.trail_start_atr * pos.atr:
            pos.trailing = True
        if pos.trailing:
            pos.stop = min(pos.stop, pos.best + p.trail_atr * pos.atr)
    return None, ""


def run(candles: Sequence[Candle], p: BreakoutParams, equity: float = 1000.0):
    """Bar-by-bar simulation. Returns (trades, equity_curve)."""
    atrs = atr_series(candles, p.atr_period)
    trades: List[Trade] = []
    curve = [equity]
    pos: Optional[OpenPosition] = None
    pending: Optional[str] = None
    cooldown = 0

    for i in range(len(candles)):
        c = candles[i]

        if pos is not None:
            exit_px, reason = update_position(pos, c, p)
            if exit_px is not None:
                t = Trade(side=pos.side, entry_ts=pos.entry_ts, entry=pos.entry,
                          exit_ts=c.ts, exit=exit_px, qty=pos.qty, reason=reason,
                          mfe=pos.mfe, mae=pos.mae,
                          risk_frac=p.stop_atr * pos.atr / pos.entry)
                notional = pos.qty * pos.entry
                pnl = t.gross_pct * notional - notional * p.fee_bps_round_trip / 1e4
                equity += pnl
                trades.append(t)
                pos = None
                cooldown = p.cooldown_bars

        if pos is None and pending is not None and cooldown <= 0:
            atr = atrs[i - 1] if i > 0 else None
            if atr:
                pos = open_position(pending, c.open, c.ts, atr, equity, p)
            pending = None
        elif pending is not None:
            pending = None

        if cooldown > 0:
            cooldown -= 1

        if pos is None and cooldown <= 0:
            pending = entry_signal(candles, i, atrs[i], p)

        curve.append(equity)

    # A position still open when the data ends is NOT free. Dropping it hides
    # exactly the trade most likely to be a large unrealised loser, and makes
    # a backtest look better the more often it ends mid-trade. Mark it to the
    # last close and charge the fee like any other round trip.
    if pos is not None:
        last = candles[-1]
        t = Trade(side=pos.side, entry_ts=pos.entry_ts, entry=pos.entry,
                  exit_ts=last.ts, exit=last.close, qty=pos.qty,
                  reason="end_of_data", mfe=pos.mfe, mae=pos.mae,
                  risk_frac=p.stop_atr * pos.atr / pos.entry)
        notional = pos.qty * pos.entry
        equity += t.gross_pct * notional - notional * p.fee_bps_round_trip / 1e4
        trades.append(t)
        curve.append(equity)

    return trades, curve


def stats(trades: Sequence[Trade], curve: Sequence[float], p: BreakoutParams) -> dict:
    """Net-of-fee performance. Every figure here is AFTER the round-trip fee."""
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "total_return_pct": 0.0,
                "profit_factor": 0.0, "max_drawdown_pct": 0.0,
                "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "expectancy_pct": 0.0,
                "avg_win_r": 0.0, "avg_loss_r": 0.0}
    fee = p.fee_bps_round_trip / 1e4
    nets = [t.gross_pct - fee for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    peak = curve[0]
    dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100.0,
        "total_return_pct": (curve[-1] / curve[0] - 1.0) * 100.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown_pct": dd * 100.0,
        "avg_win_pct": (sum(wins) / len(wins) * 100.0) if wins else 0.0,
        "avg_loss_pct": (sum(losses) / len(losses) * 100.0) if losses else 0.0,
        "expectancy_pct": sum(nets) / len(nets) * 100.0,
        # R-multiples: what risk_simulator.py needs, so the handoff between
        # the two tools is exact rather than eyeballed off percentages.
        "avg_win_r": (sum(rw) / len(rw)) if (rw := [t.r_multiple(p.fee_bps_round_trip)
                                                   for t in trades
                                                   if t.r_multiple(p.fee_bps_round_trip) > 0]) else 0.0,
        "avg_loss_r": (-sum(rl) / len(rl)) if (rl := [t.r_multiple(p.fee_bps_round_trip)
                                                     for t in trades
                                                     if t.r_multiple(p.fee_bps_round_trip) <= 0]) else 0.0,
    }
