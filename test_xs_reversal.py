import inspect
import math
import unittest
from dataclasses import replace

from breakout import Candle
from xs_reversal import (ReversalParams, ReversalPosition, _close,
                         _mark_net, align_candles, choose_reversal_pair,
                         return_vector, run)


def hourly_panel(days=12):
    slopes = {
        "SOLUSDT": 0.0020,
        "SUIUSDT": -0.0020,
        "BNBUSDT": 0.0008,
        "XRPUSDT": -0.0007,
        "TRXUSDT": 0.0003,
        "DOGEUSDT": -0.0002,
    }
    panel = {}
    for symbol, slope in slopes.items():
        rows = []
        previous = 100.0
        for i in range(days * 24):
            close = 100.0 * math.exp(slope * i)
            rows.append(Candle(i * 3600.0, previous, max(previous, close),
                               min(previous, close), close, 1000.0))
            previous = close
        panel[symbol] = rows
    return panel


class CrossSectionalReversalTests(unittest.TestCase):
    def test_weakest_is_long_and_strongest_is_short(self):
        signal = choose_reversal_pair(
            {"SOLUSDT": 0.05, "SUIUSDT": -0.04, "BNBUSDT": 0.01}, 0.03)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.long_symbol, "SUIUSDT")
        self.assertEqual(signal.short_symbol, "SOLUSDT")
        self.assertAlmostEqual(signal.dispersion, 0.09)

    def test_dispersion_gate_rejects_routine_noise(self):
        signal = choose_reversal_pair(
            {"SOLUSDT": 0.01, "SUIUSDT": -0.01, "BNBUSDT": 0.0}, 0.03)
        self.assertIsNone(signal)

    def test_return_vector_uses_only_completed_history(self):
        series = hourly_panel()
        _, aligned = align_candles(series)
        before = return_vector(aligned, 48, 24)
        for symbol in aligned:
            future = aligned[symbol][49]
            aligned[symbol][49] = Candle(future.ts, future.open, future.high,
                                         future.low, future.close * 100.0,
                                         future.volume)
        self.assertEqual(before, return_vector(aligned, 48, 24))

    def test_signal_fills_at_next_midnight_open(self):
        params = replace(ReversalParams(), pair_stop_usd=-100.0,
                         pair_target_usd=100.0)
        sim = run(hourly_panel(), params)
        self.assertGreater(len(sim.trades), 0)
        first = sim.trades[0]
        self.assertEqual(int(first.entry_ts // 3600) % 24, 0)
        self.assertEqual(first.entry_ts - 3600.0,
                         int(first.entry_ts - 3600.0))

    def test_only_one_pair_is_active(self):
        params = replace(ReversalParams(), pair_stop_usd=-100.0,
                         pair_target_usd=100.0)
        trades = sorted(run(hourly_panel(), params).trades,
                        key=lambda trade: trade.entry_ts)
        self.assertGreater(len(trades), 1)
        for left, right in zip(trades[:-1], trades[1:]):
            self.assertLessEqual(left.exit_ts, right.entry_ts)

    def test_four_fill_fee_math(self):
        params = ReversalParams(cost_bps_per_side=7.0)
        pos = ReversalPosition("SUIUSDT", "SOLUSDT", 0, 0.0,
                               100.0, 100.0, 0.05, 0.05,
                               0.007, 15.0)
        wallet, trade, _ = _close(pos, 103.0, 97.0, 86400.0,
                                  "pair_target", 14.993, params)
        self.assertAlmostEqual(trade.gross_pnl, 0.30, places=6)
        self.assertAlmostEqual(trade.fees, 0.014, places=4)
        self.assertAlmostEqual(wallet, 15.286, places=3)
        self.assertGreater(trade.net_pnl, 0.28)

    def test_fee_net_mark_drives_stop(self):
        params = ReversalParams()
        pos = ReversalPosition("SUIUSDT", "SOLUSDT", 0, 0.0,
                               100.0, 100.0, 0.05, 0.05,
                               0.007, 15.0)
        self.assertLess(_mark_net(pos, 97.0, 103.0, params), -0.30)

    def test_minimum_order_rounding_is_bounded(self):
        minimums = {symbol: 6.0 for symbol in hourly_panel()}
        steps = {symbol: 0.001 for symbol in hourly_panel()}
        sim = run(hourly_panel(), ReversalParams(),
                  min_notional_by_symbol=minimums,
                  qty_step_by_symbol=steps)
        self.assertEqual(sim.trades, [])
        self.assertGreater(sim.blocked_min_notional, 0)

    def test_engine_has_no_order_client(self):
        import xs_reversal
        source = inspect.getsource(xs_reversal)
        for forbidden in ("import exchange", "import trading", "create_order(",
                          "new_order(", "place_order("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
