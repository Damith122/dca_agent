import inspect
import unittest
from dataclasses import replace

from breakout import Candle
from htf import (HTFParams, Position, _close, _completed_timeframe,
                 align_candles, prepare_features, run, signals_at)


def trending_series(symbol_shift=0.0, both_levels=False, target_only=False):
    n = 60 * 24 + 12
    rows = []
    for i in range(n):
        close = 100.0 + symbol_shift + i * 0.01
        rows.append(Candle(i * 3600.0, close - 0.01, close + 0.05,
                           close - 0.05, close, 100.0))
    # A completed-hour pullback followed by bullish resumption at i=n-2.
    i = n - 2
    prior_base = 100.0 + symbol_shift + (i - 1) * 0.01
    rows[i - 1] = Candle((i - 1) * 3600.0, prior_base,
                         prior_base + 0.05, prior_base - 1.55,
                         prior_base - 1.50, 100.0)
    resume = 100.0 + symbol_shift + i * 0.01 + 0.25
    rows[i] = Candle(i * 3600.0, resume - 0.50, resume + 0.05,
                     resume - 0.60, resume, 100.0)
    entry = resume + 0.02
    last = Candle((n - 1) * 3600.0, entry, entry + 0.05,
                  entry - 0.05, entry + 0.01, 100.0)
    rows[-1] = last
    # ATR is roughly 0.1, so these deliberately span both/one exit level.
    if both_levels:
        rows[-1] = Candle(last.ts, entry, entry + 2.0, entry - 2.0,
                          entry + 0.5, 100.0)
    elif target_only:
        rows[-1] = Candle(last.ts, entry, entry + 2.0, entry - 0.02,
                          entry + 0.5, 100.0)
    return rows


class HigherTimeframeTests(unittest.TestCase):
    def test_higher_timeframe_close_is_not_visible_early(self):
        rows = [Candle(i * 3600.0, 1, 1, 1, float(i + 1), 1)
                for i in range(8)]
        close, _, _ = _completed_timeframe(rows, 4, 1, 2)
        self.assertTrue(all(value != value for value in close[:3]))
        self.assertEqual(close[3], 4.0)
        self.assertEqual(close[6], 4.0)
        self.assertEqual(close[7], 8.0)

    def test_pullback_signal_uses_completed_4h_and_daily_trend(self):
        rows = trending_series()
        _, aligned = align_candles({"SOLUSDT": rows})
        p = HTFParams()
        features = prepare_features(aligned, p)
        sigs = signals_at(aligned, features, len(rows) - 2, p)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].side, 1)

    def test_next_open_fill_and_pessimistic_stop_before_target(self):
        rows = trending_series(both_levels=True)
        sim = run({"SOLUSDT": rows}, starting_equity=15.0)
        self.assertEqual(len(sim.trades), 1)
        self.assertEqual(sim.trades[0].reason, "stop")
        self.assertEqual(sim.trades[0].entry_ts, rows[-1].ts)
        self.assertLess(sim.trades[0].net_pnl, 0.0)

    def test_target_is_fee_net_and_larger_than_five_cents(self):
        rows = trending_series(target_only=True)
        sim = run({"SOLUSDT": rows}, starting_equity=15.0)
        self.assertEqual(len(sim.trades), 1)
        self.assertEqual(sim.trades[0].reason, "target")
        self.assertGreater(sim.trades[0].net_pnl, 0.05)
        self.assertGreater(sim.total_fees, 0.0)

    def test_one_active_position_across_symbols(self):
        series = {"SOLUSDT": trending_series(),
                  "SUIUSDT": trending_series(symbol_shift=10.0)}
        sim = run(series, starting_equity=15.0)
        self.assertEqual(len(sim.trades), 1)

    def test_exchange_minimum_can_block_entry(self):
        rows = trending_series()
        sim = run({"SOLUSDT": rows}, starting_equity=15.0,
                  min_notional_by_symbol={"SOLUSDT": 20.0},
                  qty_step_by_symbol={"SOLUSDT": 0.001})
        self.assertEqual(sim.trades, [])
        self.assertEqual(sim.blocked_min_notional, 1)

    def test_daily_loss_gate_fails_closed(self):
        rows = trending_series()
        p = replace(HTFParams(), daily_loss_gate=-0.001)
        sim = run({"SOLUSDT": rows}, p, starting_equity=15.0)
        self.assertEqual(sim.daily_loss_lock_hits, 1)

    def test_engine_has_no_order_client(self):
        import htf
        source = inspect.getsource(htf)
        for forbidden in ("import exchange", "import trading", "create_order(",
                          "new_order(", "place_order("):
            self.assertNotIn(forbidden, source)

    def test_close_math_includes_both_fees(self):
        p = HTFParams(cost_bps_per_side=7.0)
        pos = Position("SOLUSDT", 1, 0, 0.0, 100.0, 0.1, 1.0,
                       98.5, 103.0, 0.007, 15.0)
        wallet, trade, _ = _close(pos, 103.0, 1.0, "target", 14.993, p)
        self.assertAlmostEqual(trade.fees, 0.01421, places=6)
        self.assertAlmostEqual(trade.net_pnl, 0.28579, places=6)
        self.assertAlmostEqual(wallet, 15.28579, places=6)


if __name__ == "__main__":
    unittest.main()
