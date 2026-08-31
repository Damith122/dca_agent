"""Conservative order admission; not a Binance liquidation-price calculator.

Use a fresh /fapi/v2/account snapshot in USDT single-asset cross-margin mode.
Reserve a correlated adverse move across GROSS exposure, maintenance and costs.
Unknown account data fail closed. Existing exit protection remains independent.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str
    gross_notional: float = 0.0
    required_equity: float = 0.0
    equity: float = 0.0


def _number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite account value")
    return result


def assess_add(account, *, symbol, add_notional, leverage,
               local_notionals=None, liquidation_buffer=0.15,
               headroom=0.03, maintenance_floor=0.01, cost_rate=0.002):
    """Reject additions that cannot fund the configured stress reserve.

    No credit for positive unrealized profit, offsetting directions, or a
    different symbol's assumed diversification. Local exposure only raises
    exchange exposure; it can never overwrite or hide an unmanaged position.
    Existing opening orders, isolated/hedge/multi-asset positions are refused
    because this small-account guard does not model those configurations.
    """
    try:
        add = _number(add_notional)
        lev = _number(leverage)
        buffer, extra, mmr, costs = map(_number, (
            liquidation_buffer, headroom, maintenance_floor, cost_rate))
        if add <= 0 or lev < 1 or buffer <= 0 or extra < 0 or mmr <= 0 or costs < 0:
            raise ValueError("invalid risk parameters")
        if not symbol.endswith("USDT") or account.get("multiAssetsMargin") is not False:
            raise ValueError("USDT single-asset mode required")
        if account.get("canTrade") is not True:
            raise ValueError("account trading status unknown or disabled")
        if _number(account["totalOpenOrderInitialMargin"]) != 0:
            raise ValueError("unresolved opening orders")
        wallet = _number(account["totalCrossWalletBalance"])
        pnl = _number(account["totalCrossUnPnl"])
        equity = wallet + min(pnl, 0.0)
        available = _number(account["availableBalance"])
        maintenance = _number(account["totalMaintMargin"])
        if wallet < 0 or maintenance < 0:
            raise ValueError("invalid collateral or maintenance")
        notionals = {}
        positions = account["positions"]
        if not isinstance(positions, list):
            raise ValueError("missing position list")
        for row in positions:
            amount = _number(row["positionAmt"])
            if amount == 0:
                continue
            name = row["symbol"]
            if (not name.endswith("USDT") or row.get("positionSide") != "BOTH"
                    or row.get("isolated") is not False):
                raise ValueError("unsupported active position mode")
            if "notional" in row:
                value = abs(_number(row["notional"]))
            else:
                # V2 has entryPrice/unrealizedProfit, not a notional field.
                # For a linear contract: abs(q)*mark = abs(q)*entry +
                # sign(q)*unrealizedProfit. Keep entry value as a floor.
                entry_value = abs(amount) * _number(row["entryPrice"])
                marked_value = entry_value + (1 if amount > 0 else -1) * _number(row["unrealizedProfit"])
                if entry_value <= 0 or marked_value <= 0:
                    raise ValueError("invalid position valuation")
                value = max(entry_value, marked_value)
            if value == 0 or name in notionals:
                raise ValueError("invalid or duplicate active position")
            notionals[name] = value
        for name, value in (local_notionals or {}).items():
            value = _number(value)
            if value < 0:
                raise ValueError("invalid local exposure")
            notionals[name] = max(notionals.get(name, 0.0), value)
        gross = sum(notionals.values()) + add
        # Reserve costs on all exposure, not only the proposed add. This is
        # intentionally more conservative than a point liquidation estimate.
        required = gross * (buffer + extra + costs) + max(maintenance, gross * mmr)
        if equity <= required:
            return Admission(False, "collateral stress reserve exceeded", gross, required, equity)
        if available < add * (1.0 / lev + costs):
            return Admission(False, "insufficient free initial margin", gross, required, equity)
        return Admission(True, "within collateral stress reserve", gross, required, equity)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        return Admission(False, f"invalid account/risk data: {exc}")
