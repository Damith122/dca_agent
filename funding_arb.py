"""Delta-neutral funding-rate carry: engine shared by the backtest and the bot.

Why this survives the fee trap
------------------------------
Every directional strategy in this project failed the same way. Its edge was
IC * sigma_h, an unknown quantity estimated from noisy history, and the fee
was subtracted from it. Four measurements put that IC at roughly zero.

This trade has no IC term. Hold spot long against an equal perp short and
the price exposure cancels: whatever the market does, one leg gains what the
other loses. What remains is the funding the perp pays every eight hours -
an observable cash flow, published in advance, not a forecast.

That changes what "edge" means. You are not predicting; you are being paid a
rate you can read on screen before committing. The two remaining questions
are arithmetic:

    revenue = notional * funding_rate * (periods held)
    cost    = notional * (spot_in + spot_out + perp_in + perp_out)

so the position must be held long enough that revenue clears cost:

    break_even_periods = total_cost_bps / funding_bps_per_period

At Binance VIP0 the four legs cost about 30 bps (spot fees are 0.10% a side -
DOUBLE the futures rate, which is the detail that makes naive carry maths
too optimistic). Against typical major-pair funding of 0.010% per 8h - 3 bps
a day - that is ten days to break even. With a BNB discount and maker fills
on the perp leg it is roughly six.

What actually goes wrong, and what this module does about it
-----------------------------------------------------------
1. FUNDING FLIPS NEGATIVE. Then you pay instead of collect. The entry rule
   requires a persistent positive history, not one attractive print, and the
   exit rule leaves when the trailing estimate decays.
2. YOU EXIT TOO EARLY. Cost is front-loaded and revenue accrues slowly, so
   leaving before break-even locks in a loss. min_hold_periods enforces the
   arithmetic unless a risk rule overrides it.
3. THE SHORT LEG LIQUIDATES. Spot and perp margin are separate pools. A
   sharp rally cannot hurt the hedged POSITION but can absolutely liquidate
   the perp leg, leaving an unhedged spot bag. Leverage is capped low and
   margin headroom is checked every cycle.
4. THE LEGS DRIFT APART. Filling one leg and not the other leaves naked
   directional risk - the exact thing this trade exists to avoid. The engine
   reports the imbalance so the caller can flatten rather than hope.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Sequence
from collections import deque

PERIODS_PER_DAY = 3          # Binance funds at 00:00, 08:00, 16:00 UTC
PERIODS_PER_YEAR = 3 * 365


@dataclass
class CarryCosts:
    """All-in round-trip cost, in basis points of notional, per leg."""
    spot_in_bps: float = 10.0
    spot_out_bps: float = 10.0
    perp_in_bps: float = 5.0
    perp_out_bps: float = 5.0

    @property
    def total_bps(self) -> float:
        return (self.spot_in_bps + self.spot_out_bps
                + self.perp_in_bps + self.perp_out_bps)

    @property
    def entry_bps(self) -> float:
        return self.spot_in_bps + self.perp_in_bps


@dataclass
class CarryParams:
    lookback_periods: int = 21          # 7 days of funding prints
    min_funding_bps: float = 0.5        # per 8h; 0.5 bps = 0.005%
    entry_margin: float = 1.5           # require this multiple of break-even
    exit_funding_bps: float = 0.0       # leave when the estimate drops here
    min_hold_periods: int = 3           # never churn inside one day
    max_hold_periods: int = 90          # 30 days, then reassess
    leverage: float = 2.0               # on the PERP leg only
    maintenance_margin: float = 0.005   # Binance tier-1 is ~0.4%
    margin_buffer: float = 3.0          # keep this multiple of maintenance
    max_leg_imbalance: float = 0.02     # 2% notional drift before flattening


@dataclass
class CarryState:
    open: bool = False
    entry_period: int = 0
    notional: float = 0.0
    spot_qty: float = 0.0
    perp_qty: float = 0.0
    entry_spot: float = 0.0
    entry_perp: float = 0.0
    funding_collected_bps: float = 0.0
    periods_held: int = 0


def funding_estimate(history: Sequence[float], lookback: int) -> Optional[float]:
    """Expected funding per period, in bps, from recent prints.

    The MEDIAN, not the mean: funding spikes during squeezes, and a mean
    lets one 0.3% print argue for entering a market that pays nothing the
    rest of the month. The median asks whether the carry is durable.
    """
    if len(history) < lookback:
        return None
    window = list(history)[-lookback:]
    return statistics.median(window)


def break_even_periods(costs: CarryCosts, funding_bps: float) -> float:
    """How many funding periods until the position has paid for itself."""
    if funding_bps <= 0:
        return float("inf")
    return costs.total_bps / funding_bps


def should_enter(history: Sequence[float], costs: CarryCosts,
                 p: CarryParams) -> tuple:
    """(enter, reason, expected_funding_bps).

    Requires the trade to clear its cost with room to spare INSIDE the
    maximum hold, so entering is a decision about the whole trade rather
    than about the next eight hours.
    """
    est = funding_estimate(history, p.lookback_periods)
    if est is None:
        return False, f"only {len(history)} funding prints, need {p.lookback_periods}", 0.0
    if est < p.min_funding_bps:
        return False, f"funding {est:.3f} bps below floor {p.min_funding_bps:.3f}", est
    # Persistence matters more than level: a median that is positive only
    # because of a few spikes is not something to hold for weeks.
    window = list(history)[-p.lookback_periods:]
    positive = sum(1 for x in window if x > 0) / len(window)
    if positive < 0.70:
        return False, f"only {positive * 100:.0f}% of recent periods positive", est
    be = break_even_periods(costs, est)
    if be * p.entry_margin > p.max_hold_periods:
        return False, (f"break-even {be:.0f} periods x{p.entry_margin:g} margin "
                       f"exceeds max hold {p.max_hold_periods}"), est
    return True, f"funding {est:.3f} bps, break-even {be:.1f} periods", est


def should_exit(state: CarryState, history: Sequence[float], costs: CarryCosts,
                p: CarryParams, margin_ratio: Optional[float] = None,
                leg_imbalance: float = 0.0) -> tuple:
    """(exit, reason). Risk rules override the minimum hold; economics do not.

    The ordering matters. A margin problem or a broken hedge is an emergency
    and leaves immediately even at a loss. Weak funding is not an emergency,
    and bailing out before break-even guarantees the loss that patience might
    have avoided.
    """
    if margin_ratio is not None and margin_ratio < p.maintenance_margin * p.margin_buffer:
        return True, (f"margin ratio {margin_ratio:.4f} within "
                      f"{p.margin_buffer:g}x of maintenance - EMERGENCY")
    if abs(leg_imbalance) > p.max_leg_imbalance:
        return True, (f"legs drifted {leg_imbalance * 100:+.2f}% apart - "
                      "the hedge is broken, flatten")
    if state.periods_held >= p.max_hold_periods:
        return True, f"held {state.periods_held} periods, reassess"
    if state.periods_held < p.min_hold_periods:
        return False, ""
    est = funding_estimate(history, p.lookback_periods)
    if est is None:
        return False, ""
    if est <= p.exit_funding_bps:
        # Only leave on weak funding once the entry cost is already recovered,
        # or once the carry has turned STRICTLY negative and is bleeding.
        #
        # The distinction matters: at exactly zero funding the position earns
        # nothing but costs nothing to hold, while exiting crystallises the
        # sunk entry cost and pays the exit fee on top. Treating "flat" as
        # "losing" made it bail out of a merely dull market and book a
        # guaranteed loss it could have waited out.
        if state.funding_collected_bps >= costs.entry_bps or est < 0:
            return True, f"funding decayed to {est:.3f} bps"
    return False, ""


def position_size(equity: float, price: float, p: CarryParams) -> tuple:
    """(notional, spot_capital, perp_margin).

    Spot is bought outright, so it consumes its full notional. The perp short
    posts margin at `leverage`. Total capital is notional * (1 + 1/leverage),
    which is why a carry book needs more capital than a directional one for
    the same exposure.
    """
    per_unit = 1.0 + 1.0 / p.leverage
    notional = equity / per_unit
    return notional, notional, notional / p.leverage


def cycle_pnl_bps(funding_collected_bps: float, costs: CarryCosts,
                  basis_slippage_bps: float = 0.0) -> float:
    """Net result of one complete carry cycle, in bps of notional."""
    return funding_collected_bps - costs.total_bps - basis_slippage_bps


def annualised_return(funding_bps: float, costs: CarryCosts, p: CarryParams,
                      hold_periods: int) -> float:
    """Return on CAPITAL (not notional) if this carry repeated all year."""
    if hold_periods <= 0:
        return 0.0
    net_bps = funding_bps * hold_periods - costs.total_bps
    cycles = PERIODS_PER_YEAR / hold_periods
    on_notional = net_bps * cycles / 1e4
    capital_per_notional = 1.0 + 1.0 / p.leverage
    return on_notional / capital_per_notional
