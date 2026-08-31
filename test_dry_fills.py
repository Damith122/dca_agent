#!/usr/bin/env python3
"""Simulated DRY_RUN fills: does the success head actually get labels, and
is the simulation honest about lookahead and cost?

The bug this covers: brain.learn_success() is reachable from exactly one
place, _on_close_filled(). Under DRY_RUN no order was ever sent, so no fill
event ever arrived, so no position ever opened, so none ever closed, so that
call never ran. success_p sat at its 0.5 fallback with single-digit lifetime
samples against BRAIN_HEAD_MIN_SAMPLES=20 - starved, not broken.

The risk this covers: a paper-trading harness that fills at the price the
decision was made on will manufacture an edge that does not survive contact
with a real venue. The two tests that matter most here are therefore the
lookahead guard (an order must never fill on its submitting tick) and the
cost charge (slippage adverse in BOTH directions, plus commission).
"""
import os

# Must be set BEFORE config is imported - it reads the environment at import.
os.environ["DRY_RUN"] = "true"
os.environ["DRY_FILL_ENABLED"] = "true"
os.environ["USE_TESTNET"] = "true"

import ast          # noqa: E402
import asyncio      # noqa: E402
import inspect      # noqa: E402
import sys          # noqa: E402
import time         # noqa: E402

import config       # noqa: E402
import trading      # noqa: E402
from dry_fills import DryFillSimulator, PendingFill  # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


def section(title):
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


# ==========================================================================
section("[1] the lookahead guard - no fill on the submitting tick")
# ==========================================================================
sim = DryFillSimulator(slippage_bps=1.0, taker_fee_pct=0.0005)
T0 = 1_000_000.0
sim.register(1, "initial", "BUY", 2.0, 100.0, now=T0)

check("an order does not fill on the tick that submitted it",
      sim.ready(100.0, now=T0) == [],
      "same-tick fill is the decision and the execution reading one price")
check("nor on a tick timestamped fractionally earlier (clock skew)",
      sim.ready(100.0, now=T0 - 0.5) == [])
check("it does fill on a strictly later tick",
      len(sim.ready(100.0, now=T0 + 0.25)) == 1)
check("it stays pending until then (not silently dropped)",
      sim.pending_count() == 1)

# The rule that actually holds in production: the bot's decision-tick
# counter, not the wall clock. Submitting and resolving inside one tick are
# microseconds apart, which any time-only comparison waves through.
sim_tick = DryFillSimulator()
sim_tick.register(1, "initial", "BUY", 1.0, 100.0, now=T0, tick=41)
check("same tick counter -> not eligible, however much clock has passed",
      sim_tick.ready(100.0, now=T0 + 3600, tick=41) == [],
      "a wall-clock-only rule would have filled this")
check("an EARLIER tick counter is not eligible either",
      sim_tick.ready(100.0, now=T0 + 3600, tick=40) == [])
check("the next tick counter is eligible",
      len(sim_tick.ready(100.0, now=T0 + 0.001, tick=42)) == 1)

sim_delay = DryFillSimulator(min_delay_sec=5.0)
sim_delay.register(1, "initial", "BUY", 1.0, 100.0, now=T0)
check("min_delay_sec holds the fill back for the configured latency",
      sim_delay.ready(100.0, now=T0 + 4.9) == [])
check("and releases it once the latency has elapsed",
      len(sim_delay.ready(100.0, now=T0 + 5.1)) == 1)

# ==========================================================================
section("[2] the fill is priced on the LATER tick, not the submitted one")
# ==========================================================================
sim = DryFillSimulator(slippage_bps=0.0)
sim.register(1, "initial", "BUY", 1.0, 100.0, now=T0)
ready = sim.ready(107.0, now=T0 + 1)
check("fill price comes from the price passed at resolution time",
      ready[0][1] == 107.0, f"got {ready[0][1]}")
check("the submitted price is retained for the log only",
      ready[0][0].submitted_price == 100.0)

# ==========================================================================
section("[3] cost: slippage is adverse in BOTH directions")
# ==========================================================================
sim = DryFillSimulator(slippage_bps=10.0)   # 10 bps = 0.1%
check("a BUY pays up",  sim.fill_price("BUY", 100.0) == 100.1,
      str(sim.fill_price("BUY", 100.0)))
check("a SELL sells down", sim.fill_price("SELL", 100.0) == 99.9,
      str(sim.fill_price("SELL", 100.0)))
check("slippage never favours the order (BUY)",
      sim.fill_price("BUY", 100.0) > 100.0)
check("slippage never favours the order (SELL)",
      sim.fill_price("SELL", 100.0) < 100.0)
check("zero slippage is honoured exactly",
      DryFillSimulator(slippage_bps=0.0).fill_price("BUY", 100.0) == 100.0)

# ==========================================================================
section("[4] cost: taker commission on notional")
# ==========================================================================
sim = DryFillSimulator(taker_fee_pct=0.0005)
check("0.05% of notional", abs(sim.commission(101.0, 2.0) - 0.101) < 1e-12,
      str(sim.commission(101.0, 2.0)))
check("commission is never negative",
      sim.commission(101.0, 2.0) > 0)
check("the default rate is the Binance USD-M taker rate",
      DryFillSimulator().taker_fee_pct == 0.0005)

# ==========================================================================
section("[5] realized PnL is GROSS of fees (Binance's own rp convention)")
# ==========================================================================
sim = DryFillSimulator(taker_fee_pct=0.0005)
check("LONG 100 -> 101 on 2 units is +2.0",
      abs(sim.realized_pnl("LONG", 100.0, 101.0, 2.0) - 2.0) < 1e-12)
check("SHORT 100 -> 101 on 2 units is -2.0",
      abs(sim.realized_pnl("SHORT", 100.0, 101.0, 2.0) + 2.0) < 1e-12)
check("SHORT 100 -> 99 on 2 units is +2.0",
      abs(sim.realized_pnl("SHORT", 100.0, 99.0, 2.0) - 2.0) < 1e-12)
# The whole point of gross: handle_order_update() accumulates "n" into
# _position_fees_accum and _on_close_filled() subtracts it. Netting fees
# into rp here as well would deduct them twice.
check("rp does NOT already have the commission netted out",
      sim.realized_pnl("LONG", 100.0, 101.0, 2.0)
      != 2.0 - sim.commission(101.0, 2.0))

# ==========================================================================
section("[6] the synthesised event carries what handle_order_update reads")
# ==========================================================================
pf = PendingFill(order_id=-7, role="close", side="SELL", qty=2.0,
                 submitted_ts=T0, submitted_price=100.0)
ev = DryFillSimulator().build_event(pf, 101.0, "SOLUSDT", realized_pnl=2.0)
o = ev["o"]
for key, why in [("i", "order id, looked up in _order_index"),
                 ("X", "status - must be FILLED to dispatch"),
                 ("ap", "average fill price"),
                 ("z", "cumulative filled qty"),
                 ("rp", "realized pnl -> _rp_accum"),
                 ("n", "commission -> _fee_accum"),
                 ("N", "commission asset - must be USDT to be trusted"),
                 ("t", "trade id"), ("T", "event time")]:
    check(f"event carries o.{key} ({why})", key in o)
check("status is FILLED", o["X"] == "FILLED")
check("commission asset is USDT, so _fee_accum trusts it",
      o["N"] == "USDT")
check("float fields parse as floats (handler casts them)",
      float(o["ap"]) == 101.0 and float(o["z"]) == 2.0 and float(o["rp"]) == 2.0)
check("commission is populated, not left at zero",
      float(o["n"]) > 0)
check("event type is the one the websocket consumer dispatches on",
      ev["e"] == "ORDER_TRADE_UPDATE")

# ==========================================================================
section("[7] bookkeeping: resolve, cancel, counters")
# ==========================================================================
sim = DryFillSimulator()
sim.register(1, "initial", "BUY", 1.0, 100.0, now=T0)
check("resolve returns the pending order", sim.resolve(1) is not None)
check("and removes it, so it cannot fill twice", sim.resolve(1) is None)
check("filled counter advanced once", sim.filled == 1)

sim = DryFillSimulator()
sim.register(2, "close", "SELL", 1.0, 100.0, now=T0)
check("cancel removes a pending order", sim.cancel(2) is True)
check("cancelling an unknown order is a no-op, not an error",
      sim.cancel(999) is False)
check("a cancelled order never fills", sim.ready(100.0, now=T0 + 10) == [])

sim = DryFillSimulator()
sim.register(3, "initial", "BUY", 0.0, 100.0, now=T0)
check("a zero-qty order is refused (would be an unfillable phantom)",
      sim.pending_count() == 0)
sim.register(4, "initial", "BUY", 1.0, 0.0, now=T0)
check("a zero-price order is refused", sim.pending_count() == 0)
check("refused orders do not inflate the submitted counter",
      sim.submitted == 0)
check("stats reports submitted/filled/pending",
      set(DryFillSimulator().stats()) == {"submitted", "filled", "pending"})

# ==========================================================================
section("[8] wiring: the DRY_RUN order sites register with the simulator")
# ==========================================================================
src = open("trading.py", encoding="utf-8").read()
tree = ast.parse(src)


def func_src(name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


step_src = func_src("_place_step_order")
close_src = func_src("_place_reduce_only_close_order")
resolve_src = func_src("_resolve_dry_fills")
tick_src = func_src("on_price_tick")

check("_place_step_order registers its simulated entry/DCA order",
      "_dry_fills.register(" in step_src
      and "fake_id, role, order_side, qty, price" in step_src)
check("the entry registration passes the tick counter (the lookahead guard)",
      "tick=self._ticks_run" in step_src)
check("the close registration passes the tick counter too",
      "tick=self._ticks_run" in close_src)
check("_resolve_dry_fills asks for fills eligible on THIS tick counter",
      "sim.ready(price, tick=self._ticks_run)" in resolve_src)
check("_place_reduce_only_close_order registers its simulated close",
      "_dry_fills.register(" in close_src and '"close"' in close_src)
check("_resolve_dry_fills exists on the manager", bool(resolve_src))
check("on_price_tick resolves pending fills",
      "await self._resolve_dry_fills()" in tick_src)

# Ordering matters: resolution must happen BEFORE the decision logic, so the
# state machine sees the position as it now is, and AFTER the throttle gate,
# so a submit and its fill can never land on the same tick.
gate_i = tick_src.find("self._ticks_run += 1")
res_i = tick_src.find("await self._resolve_dry_fills()")
feat_i = tick_src.find("features = self.build_features()")
check("resolution runs after the tick-throttle gate (guarantees a later tick)",
      -1 < gate_i < res_i, f"gate={gate_i} resolve={res_i}")
check("resolution runs before the entry/exit decision for this tick",
      res_i < feat_i, f"resolve={res_i} features={feat_i}")

# The critical design property: go through handle_order_update, not the
# handlers, so fees/PnL/dedup/trade-log/brain all run the production path.
check("_resolve_dry_fills dispatches via handle_order_update",
      "await self.handle_order_update(" in resolve_src)
check("_resolve_dry_fills does NOT call _on_entry_filled directly",
      "self._on_entry_filled(" not in resolve_src)
check("_resolve_dry_fills does NOT call _on_close_filled directly",
      "self._on_close_filled(" not in resolve_src)
check("_resolve_dry_fills skips orders no longer in _order_index "
      "(would otherwise rot in _unmatched_fills)",
      "not in self._order_index" in resolve_src)

# ==========================================================================
section("[9] wiring: the simulator is OFF unless DRY_RUN")
# ==========================================================================
init_src = func_src("__init__")
check("the manager guards construction on DRY_RUN",
      "if (DRY_RUN and DRY_FILL_ENABLED) else None" in src)
for name in ("DRY_FILL_ENABLED", "DRY_FILL_SLIPPAGE_BPS",
             "DRY_FILL_TAKER_FEE_PCT", "DRY_FILL_MIN_DELAY_SEC"):
    check(f"config exposes {name}", hasattr(config, name))
    check(f"{name} is in config.__all__", name in config.__all__)
check("DRY_FILL_ENABLED defaults to on",
      "_env_bool(\"DRY_FILL_ENABLED\", True)" in
      open("config.py", encoding="utf-8").read())


# ==========================================================================
section("[10] end to end: a simulated round trip reaches learn_success")
# ==========================================================================
class _FakeClient:
    async def get_position_risk(self, *a, **k):
        raise AssertionError("DRY_RUN must never hit the exchange")

    def is_cooldown_active(self):
        return False


def build_manager():
    filters = trading.SymbolFilters(
        tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0,
    )
    return trading.MartingaleManager(_FakeClient(), "SOLUSDT", filters, 5)


async def round_trip():
    m = build_manager()
    check("simulator is live on a DRY_RUN manager", m._dry_fills is not None)
    if m._dry_fills is None:
        return

    learned = []
    m.brain.learn_success = lambda f, s: learned.append(("success", s))
    m.brain.learn_quality = lambda f, r: learned.append(("quality", r))
    m.save_dca_state = lambda **k: asyncio.sleep(0)
    m._log_trade = lambda *a, **k: None

    m.current_price = 100.0
    # --- entry -------------------------------------------------------
    await m._place_step_order(step=0, side_signal="LONG", size_mult=1.0)
    check("a DRY_RUN entry leaves the position ENTERING with an order pending",
          m.position.status == "ENTERING" and m.position.pending_order_id is not None)
    check("the entry was queued with the simulator",
          m._dry_fills.pending_count() == 1)

    # Same tick: must not fill.
    await m._resolve_dry_fills()
    check("the entry does not fill on its own submitting tick",
          m.position.status == "ENTERING",
          "lookahead: decision and execution on one price")

    # Later tick, price has moved up.
    m._ticks_run += 1                  # a later decision tick
    m.current_price = 101.0
    await m._resolve_dry_fills()
    check("the entry fills on a later tick and the position opens",
          m.position.status == "OPEN", f"status={m.position.status}")
    entry_px = m.position.avg_entry_price
    check("it filled at the LATER price, not the submitted one",
          entry_px is not None and entry_px > 100.5, str(entry_px))
    check("the BUY paid slippage on top of that price",
          entry_px is not None and entry_px > 101.0, str(entry_px))
    check("entry commission was accumulated from the event's o.n",
          m._position_fees_accum > 0, str(m._position_fees_accum))
    check("fee tracking stayed reliable (commission asset was USDT)",
          m._position_fees_reliable is True)
    check("nothing is left pending after the fill",
          m._dry_fills.pending_count() == 0)

    # --- close -------------------------------------------------------
    import numpy as np
    m.position.entry_features = np.zeros(config.N_FEATURES_V2)
    qty = m.position.total_qty
    await m.close_position(reason="test", exit_reason_tag="take_profit")
    check("the close leaves the position CLOSING with an order pending",
          m.position.status == "CLOSING")
    check("the close was queued with the simulator",
          m._dry_fills.pending_count() == 1)
    await m._resolve_dry_fills()
    check("the close does not fill on its own submitting tick",
          m.position.status == "CLOSING")

    m._ticks_run += 1
    m.current_price = 103.0            # a winner
    await m._resolve_dry_fills()

    check("the round trip finished and the position is FLAT",
          m.position.status == "FLAT", f"status={m.position.status}")
    check("brain.learn_success WAS called - this is the whole point",
          any(k == "success" for k, _ in learned), str(learned))
    check("brain.learn_quality was called alongside it",
          any(k == "quality" for k, _ in learned))
    check("a profitable round trip was labelled a success",
          ("success", True) in learned, str(learned))
    # The commission accumulators are cleared when the trade finalizes, so
    # assert on the number they produced instead: the booked PnL must be the
    # gross move MINUS both legs' commission. This is the end-to-end proof
    # that the simulated fills are being charged for, rather than a paper
    # round trip booking its full gross move as profit.
    # 2026-08-31: these used to hardcode qty=0.2, which was the quantity the
    # sizing path happened to produce at the INITIAL_ENTRY_USDT default of the
    # day. Changing that default silently broke an assertion that has nothing
    # to do with position size - the claim under test is "PnL is booked net of
    # commission", which must hold at ANY quantity. Read the filled qty.
    entry_fee = 101.0101 * qty * config.DRY_FILL_TAKER_FEE_PCT
    exit_fee = 102.9897 * qty * config.DRY_FILL_TAKER_FEE_PCT
    gross = (102.9897 - 101.0101) * qty
    check("realized PnL was booked at all",
          m.realized_pnl_total != 0.0)
    check("booked PnL is NET of both legs' commission, not the gross move",
          abs(m.realized_pnl_total - (gross - entry_fee - exit_fee)) < 1e-4,
          f"booked={m.realized_pnl_total} expected={gross - entry_fee - exit_fee}")
    check("which is strictly less than the gross move",
          m.realized_pnl_total < gross)
    check("no simulated order is left dangling",
          m._dry_fills.pending_count() == 0)
    check("the fill was counted", m._dry_fills.filled == 2)
    return m, qty


async def losing_trip():
    """A losing round trip must be labelled a failure, or the head only ever
    sees one class and learns nothing."""
    m = build_manager()
    learned = []
    m.brain.learn_success = lambda f, s: learned.append(s)
    m.brain.learn_quality = lambda f, r: None
    m.save_dca_state = lambda **k: asyncio.sleep(0)
    m._log_trade = lambda *a, **k: None

    import numpy as np
    m.current_price = 100.0
    await m._place_step_order(step=0, side_signal="LONG", size_mult=1.0)
    m._ticks_run += 1
    await m._resolve_dry_fills()
    m.position.entry_features = np.zeros(config.N_FEATURES_V2)
    await m.close_position(reason="test", exit_reason_tag="hard_stop")
    m._ticks_run += 1
    m.current_price = 95.0             # a clear loser
    await m._resolve_dry_fills()
    check("a losing round trip is labelled a failure",
          learned == [False], str(learned))
    check("the losing trade still closed cleanly to FLAT",
          m.position.status == "FLAT")


async def untracked_order_is_dropped():
    """An order the manager has forgotten must not be dispatched: it would
    land in the untracked branch and sit in _unmatched_fills until it
    expired, which looks like a hang rather than a drop."""
    m = build_manager()
    m.current_price = 100.0
    await m._place_step_order(step=0, side_signal="LONG", size_mult=1.0)
    oid = m.position.pending_order_id
    m._order_index.pop(oid, None)      # simulate it being consumed elsewhere
    m._ticks_run += 1
    await m._resolve_dry_fills()
    check("an untracked simulated order is dropped, not dispatched",
          m._dry_fills.pending_count() == 0)
    check("and nothing was buffered as an unmatched fill",
          not m._unmatched_fills, str(m._unmatched_fills))


async def no_price_no_fill():
    m = build_manager()
    m.current_price = 100.0
    await m._place_step_order(step=0, side_signal="LONG", size_mult=1.0)
    m._ticks_run += 1
    m.current_price = None
    await m._resolve_dry_fills()
    check("with no current price, nothing fills (no guessed price)",
          m._dry_fills.pending_count() == 1 and m.position.status == "ENTERING")


async def main():
    await round_trip()
    section("[11] the other label class, and the failure modes")
    await losing_trip()
    await untracked_order_is_dropped()
    await no_price_no_fill()


asyncio.run(main())

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
