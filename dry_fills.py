"""Simulated fills for DRY_RUN, driven by real mainnet prices.

Why this exists
---------------
success_p learns only from CLOSED trades. Under DRY_RUN no order is ever
sent, so no fill event ever arrives, so no position ever opens or closes,
so the head has stayed at its 0.5 fallback with 1-12 lifetime samples
against a threshold of 20. It is starved, not broken.

The alternatives were both worse. Trading real money to generate labels
risks capital on a model we have just proved was not working. Trading
Binance testnet generates labels about a synthetic order book whose prices
diverge from mainnet - confident-looking data about the wrong market.

This fills simulated orders against the REAL mainnet prices the bot is
already streaming, so the labels describe the market actually being traded,
with nothing at risk.

How it stays honest
-------------------
1. NEVER FILL ON THE SUBMITTING TICK. An order submitted while the price is
   P must not fill at P - that is the decision and the execution reading the
   same number, which is lookahead. Fills are resolved on a LATER tick, at
   whatever price arrived by then.
2. CHARGE THE COST. A simulated fill applies slippage against the order and
   the taker commission, so a break-even trade shows as a small loss exactly
   as it would live.
3. SYNTHESISE THE EVENT, NOT THE OUTCOME. The simulator emits a Binance
   ORDER_TRADE_UPDATE payload and hands it to handle_order_update(), the
   same entry point the live websocket uses. Every downstream step - fee
   accumulation, realized-PnL bookkeeping, the close-dedup gate, trade
   logging, learn_success/learn_quality - runs the production path. Nothing
   about the fill logic is duplicated here, so the two cannot drift apart.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PendingFill:
    order_id: int
    role: str            # "initial" | "dca" | "close" | "partial_close"
    side: str            # BUY / SELL of the order itself
    qty: float
    submitted_ts: float
    submitted_price: float
    # The bot's decision-tick counter at submission. The later-tick rule is
    # enforced against THIS, not against the wall clock: two calls made
    # microseconds apart within one tick would otherwise satisfy a
    # time-based comparison and let an order fill on the observation that
    # produced it. None only when no tick counter was supplied (the unit
    # tests that exercise the timing rule directly).
    submitted_tick: Optional[int] = None


@dataclass
class DryFillSimulator:
    """Holds simulated orders until a later tick can fill them."""

    taker_fee_pct: float = 0.0005      # 0.05% per side, Binance USD-M taker
    slippage_bps: float = 1.0          # adverse, applied against the order
    min_delay_sec: float = 0.0         # extra latency beyond "a later tick"
    _pending: Dict[int, PendingFill] = field(default_factory=dict)
    filled: int = 0
    submitted: int = 0

    def register(self, order_id: int, role: str, side: str, qty: float,
                 price: float, now: Optional[float] = None,
                 tick: Optional[int] = None) -> None:
        if qty <= 0 or price <= 0:
            return
        self._pending[order_id] = PendingFill(
            order_id=order_id, role=role, side=side, qty=float(qty),
            submitted_ts=time.time() if now is None else now,
            submitted_price=float(price), submitted_tick=tick)
        self.submitted += 1

    def cancel(self, order_id: int) -> bool:
        return self._pending.pop(order_id, None) is not None

    def pending_count(self) -> int:
        return len(self._pending)

    def fill_price(self, side: str, price: float) -> float:
        """Slippage always works against the order, never for it."""
        slip = price * self.slippage_bps / 1e4
        return price + slip if side == "BUY" else price - slip

    def ready(self, price: float, now: Optional[float] = None,
              tick: Optional[int] = None) -> List[Tuple[PendingFill, float]]:
        """Orders whose fill may now be resolved, with their fill prices.

        An order is only eligible on a tick STRICTLY LATER than the one that
        submitted it. Without that rule the bot would decide and execute on
        the same observation, which is the most common way a paper-trading
        harness invents profit that does not exist.

        The rule is enforced on the caller's tick COUNTER where one is
        supplied, deliberately not on elapsed time. An order submitted and a
        resolution attempted within the same tick are separated by
        microseconds of wall clock, which any time-based comparison would
        wave through; the counter cannot be fooled that way.
        """
        now = time.time() if now is None else now
        out = []
        for pf in list(self._pending.values()):
            if (tick is not None and pf.submitted_tick is not None
                    and tick <= pf.submitted_tick):
                continue
            if now <= pf.submitted_ts + self.min_delay_sec:
                continue
            out.append((pf, self.fill_price(pf.side, price)))
        return out

    def resolve(self, order_id: int) -> Optional[PendingFill]:
        pf = self._pending.pop(order_id, None)
        if pf is not None:
            self.filled += 1
        return pf

    def commission(self, fill_price: float, qty: float) -> float:
        return abs(fill_price * qty) * self.taker_fee_pct

    def realized_pnl(self, side_of_position: str, avg_entry: float,
                     exit_price: float, qty: float) -> float:
        """Gross realized PnL of a close, before commission.

        Binance reports realizedPnl gross of fees and the commission
        separately, so the simulated event must do the same or the
        downstream accounting - which subtracts fees itself - would deduct
        them twice.
        """
        if side_of_position == "LONG":
            return (exit_price - avg_entry) * qty
        return (avg_entry - exit_price) * qty

    def build_event(self, pf: PendingFill, fill_price: float, symbol: str,
                    realized_pnl: float = 0.0,
                    trade_id: Optional[int] = None) -> dict:
        """A Binance ORDER_TRADE_UPDATE payload for handle_order_update().

        Only the fields that handler actually reads are populated: i, X, ap,
        z, rp, n, N, t, T. Inventing a fuller payload would suggest a
        fidelity this does not have.
        """
        ms = int(time.time() * 1000)
        return {
            "e": "ORDER_TRADE_UPDATE",
            "E": ms,
            "o": {
                "s": symbol,
                "i": pf.order_id,
                "S": pf.side,
                "X": "FILLED",
                "ap": f"{fill_price:.10f}",
                "z": f"{pf.qty:.10f}",
                "rp": f"{realized_pnl:.10f}",
                "n": f"{self.commission(fill_price, pf.qty):.10f}",
                "N": "USDT",
                "t": trade_id if trade_id is not None else -(ms % 1_000_000_000),
                "T": ms,
            },
        }

    def stats(self) -> dict:
        return {"submitted": self.submitted, "filled": self.filled,
                "pending": len(self._pending)}
