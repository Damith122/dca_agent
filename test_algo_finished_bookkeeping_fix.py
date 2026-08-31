#!/usr/bin/env python3
"""A protective stop that fills must be booked, even when Binance never
reports the child order id.

The live event, 2026-08-31 12:42:56 UTC, SUIUSDT:

    12:42:29  SELL 4.8872 + 5.1035 USDT @ 0.7187/0.7188   entry, MARKET fallback
    12:42:56  BUY  9.9997 USDT @ 0.7194  rp -0.00902  fee 0.00500

0.7194 is the EXACT trigger price of the protective stop placed at 12:42:29,
so the algo triggered and its child filled. But both the ALGO_UPDATE and the
REST re-query reported FINISHED with an empty actualOrderId, and the code
read that as proof it had been canceled:

    # No child id anywhere -> it did not trigger, so FINISHED means canceled.

The close was therefore never booked. Not a risk failure - the position was
really gone and reconciliation reached FLAT - but the trade was missing from
trades_log and session_total, the Brain got no label for a real loss, and the
reported total drifted +0.0190 USDT optimistic against Binance.

An absent actualOrderId is UNKNOWN, not "canceled". These tests pin the
three-way resolution against the exchange position, and the recovery of the
fill from user-trade history.
"""
import os
os.environ.setdefault("DRY_RUN", "true")

import ast          # noqa: E402
import asyncio      # noqa: E402
import sys          # noqa: E402

import config       # noqa: E402
import trading      # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


def section(t):
    print(f"\n--- {t} " + "-" * max(0, 66 - len(t)))


TSRC = open("trading.py", encoding="utf-8").read()
TREE = ast.parse(TSRC)


def func_src(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(TSRC, n) or ""
    return ""


# ==========================================================================
section("[1] the old assumption is gone")
# ==========================================================================
check("the 'FINISHED with no child means canceled' assumption is removed",
      "it did not trigger, so FINISHED means" not in TSRC)
check("a recovery path exists", bool(func_src("_finalize_untracked_algo_close")))
check("the ambiguous branch consults the exchange position",
      "_fetch_exchange_position()" in TSRC.split("if status == \"FINISHED\":")[1][:4000])
check("the live incident is documented in the code",
      "0.7194" in TSRC and "actualOrderId" in TSRC)


# ==========================================================================
section("[2] three-way resolution, driven by the exchange")
# ==========================================================================
class _Client:
    def __init__(self, position=None, trades=None, fail_pos=False, fail_trades=False):
        self._pos, self._trades = position, trades or []
        self.fail_pos, self.fail_trades = fail_pos, fail_trades
        self.placed = []

    async def get_position_risk(self, symbol):
        if self.fail_pos:
            raise trading.BinanceApiError("boom", -1)
        return [self._pos] if self._pos else []

    async def get_user_trades(self, symbol, **kw):
        if self.fail_trades:
            raise trading.BinanceApiError("boom", -1)
        return self._trades

    async def get_algo_order(self, **kw):
        return {}

    def is_cooldown_active(self):
        return False


def mgr(client):
    f = trading.SymbolFilters(tick_size=0.0001, step_size=0.1,
                              min_qty=0.1, min_notional=5.0)
    m = trading.MartingaleManager(client, "SUIUSDT", f, 2)
    m.save_dca_state = lambda **k: asyncio.sleep(0)
    m._log_trade = lambda *a, **k: None
    m.brain.learn_success = lambda *a, **k: m._learned.append(True)
    m.brain.learn_quality = lambda *a, **k: None
    m._learned = []
    p = m.position
    p.status = "OPEN"
    p.side = "SHORT"
    p.total_qty = 13.9
    p.original_qty = 13.9
    p.avg_entry_price = 0.71875
    p.entries = [(0.71875, 13.9)]
    p.opened_at = trading.time.time()
    # Required for Brain reinforcement - _on_close_filled() only calls
    # learn_success() when the entry's feature vector was captured.
    import numpy as np
    p.entry_features = np.zeros(config.N_FEATURES_V2)
    p.protective_stop_algo_id = 3000002167009346
    p.protective_stop_price = 0.7194
    m.current_price = 0.7194
    return m


# The real fills from the incident.
LIVE_FILLS = [
    {"id": 9001, "qty": "13.9", "price": "0.7194", "realizedPnl": "-0.00902",
     "commission": "0.00499983", "commissionAsset": "USDT", "buyer": True},
]


async def case_position_gone():
    """The live case: exchange flat -> the stop filled -> book the trade."""
    m = mgr(_Client(position=None, trades=LIVE_FILLS))
    await m._finalize_untracked_algo_close(context="test")
    return m


async def case_still_open():
    """Exchange still holds the position -> a genuine cancel."""
    m = mgr(_Client(position={"symbol": "SUIUSDT", "positionAmt": "-13.9"}))
    await m.handle_algo_update({"algoId": 3000002167009346, "algoStatus": "FINISHED"}) \
        if hasattr(m, "handle_algo_update") else None
    return m


async def run():
    global passed, failed
    m = await case_position_gone()
    check("the recovered close was booked (position reset to FLAT)",
          m.position.status == "FLAT", f"status={m.position.status}")
    check("the Brain received a label for it", len(m._learned) == 1,
          f"{len(m._learned)} labels")
    # The per-role accumulators are cleared when the trade finalizes, so
    # assert on the number they produced: net = gross - commission, exactly
    # as Binance reported it (-0.00902 gross, 0.00499983 commission).
    check("booked net PnL = Binance gross minus Binance commission",
          abs(m.realized_pnl_total - (-0.00902 - 0.00499983)) < 1e-6,
          f"booked {m.realized_pnl_total}, expected {-0.00902 - 0.00499983}")
    check("exit_reason is protective_stop, so it is NOT excluded from learning",
          "protective_stop" not in trading.INFRASTRUCTURE_ONLY_EXIT_REASONS)
    check("the algo id is recorded as finalized, so a retry cannot double-book",
          m._close_order_already_finalized(3000002167009346))

    # idempotency: a second recovery for the same algo must be a no-op
    before = m.realized_pnl_total
    m.position.protective_stop_algo_id = 3000002167009346
    await m._finalize_untracked_algo_close(context="duplicate")
    check("a duplicate recovery does not double-count PnL",
          m.realized_pnl_total == before, f"{before} -> {m.realized_pnl_total}")

    # user-trade lookup failure must not book a guessed trade
    m2 = mgr(_Client(position=None, fail_trades=True))
    await m2._finalize_untracked_algo_close(context="test")
    check("a failed userTrades lookup books nothing (left to reconciliation)",
          m2.position.status == "OPEN" and not m2._learned)

    # no matching unaccounted fill -> book nothing
    m3 = mgr(_Client(position=None, trades=[]))
    await m3._finalize_untracked_algo_close(context="test")
    check("no recoverable fill -> books nothing rather than guessing",
          m3.position.status == "OPEN")

    # fills already accounted for (id <= _last_live_trade_id) are ignored
    m4 = mgr(_Client(position=None, trades=LIVE_FILLS))
    m4._last_live_trade_id = 9001
    await m4._finalize_untracked_algo_close(context="test")
    check("already-seen fills are not re-booked",
          m4.position.status == "OPEN")

    # only fills on the CLOSING side count
    wrong_side = [dict(LIVE_FILLS[0], buyer=False)]   # SELL, same side as a SHORT
    m5 = mgr(_Client(position=None, trades=wrong_side))
    await m5._finalize_untracked_algo_close(context="test")
    check("fills on the opening side are not mistaken for the close",
          m5.position.status == "OPEN")


asyncio.run(run())


# ==========================================================================
section("[3] the unknown case never guesses")
# ==========================================================================
fin = TSRC.split('if status == "FINISHED":')[1][:5000]
check("an unfetchable position is treated as UNKNOWN, not canceled",
      "outcome UNKNOWN" in fin or "UNKNOWN" in fin)
check("...and tracking is NOT cleared in that case",
      fin.find("could not be fetched") < fin.find("still open on the exchange"))
check("PROTECTION_PENDING is entered so the sweep retries",
      "_mark_protection_pending" in fin)

rec = func_src("_finalize_untracked_algo_close")
check("recovery routes through the production _on_close_filled()",
      "self._on_close_filled(" in rec)
check("...and sets the exit reason to protective_stop",
      '_pending_exit_reason = "protective_stop"' in rec)
check("recovery is guarded by the finalized-id dedupe",
      "_close_order_already_finalized" in rec)
check("commission is only trusted in USDT",
      'commissionAsset' in rec and '_position_fees_reliable' in rec)

print("\n" + "=" * 70)
print(f"  {passed} passed, {failed} failed")
print("=" * 70)
sys.exit(1 if failed else 0)
