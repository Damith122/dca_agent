import inspect
import unittest
from dataclasses import replace

from breakout import Candle
from mtf_continuation import (MTFParams, _completed_four_hour, candle_pattern,
                              run)


def trend_series(both=False):
    n = 830
    rows = []
    for i in range(n):
        px = 100.0 + i * 0.02
        rows.append(Candle(i * 3600.0, px - 0.01, px + 0.08,
                           px - 0.08, px, 100.0))
    # Exact bullish engulfing at n-2; broad zone tolerance is used by tests.
    i = n - 2
    base = rows[i].close
    rows[i - 1] = Candle((i - 1) * 3600.0, base + 0.05, base + 0.08,
                         base - 0.08, base - 0.05, 100.0)
    rows[i] = Candle(i * 3600.0, base - 0.06, base + 0.12,
                     base - 0.10, base + 0.07, 100.0)
    entry = base + 0.08
    rows[-1] = Candle((n - 1) * 3600.0, entry,
                      entry + (3.0 if both else 0.05),
                      entry - (3.0 if both else 0.05), entry, 100.0)
    return rows


class MTFContinuationTests(unittest.TestCase):
    def params(self, **kw):
        values = {"zone_tolerance_atr": 20.0, "long_rsi_low": 0.0,
                  "long_rsi_high": 100.0, "risk_pct": 0.10}
        values.update(kw)
        return replace(MTFParams(), **values)

    def test_four_hour_close_not_visible_early(self):
        rows = [Candle(i * 3600.0, 1, 1, 1, float(i + 1), 1) for i in range(8)]
        close, _ = _completed_four_hour(rows, 1)
        self.assertTrue(all(v != v for v in close[:3]))
        self.assertEqual(close[3], 4.0)
        self.assertEqual(close[6], 4.0)
        self.assertEqual(close[7], 8.0)

    def test_patterns_are_objective(self):
        p = MTFParams()
        prev = Candle(0, 10, 10.2, 8.8, 9, 1)
        curr = Candle(1, 8.9, 10.3, 8.8, 10.1, 1)
        self.assertEqual(candle_pattern(prev, curr, 1, p), "engulfing")
        self.assertIsNone(candle_pattern(prev, curr, -1, p))

    def test_next_open_fill_and_one_position(self):
        p = self.params()
        rows = trend_series()
        sim = run({"SOLUSDT": rows, "SUIUSDT": trend_series()}, p)
        self.assertEqual(len(sim.trades), 1)
        self.assertEqual(sim.trades[0].entry_ts, rows[-1].ts)

    def test_stop_wins_if_stop_and_target_share_bar(self):
        p = self.params()
        sim = run({"SOLUSDT": trend_series(both=True)}, p)
        self.assertEqual(sim.trades[0].reason, "stop")
        self.assertLess(sim.trades[0].net_pnl, 0)

    def test_risk_and_minimum_limits(self):
        rows = trend_series()
        base = self.params(risk_pct=0.00001)
        sim = run({"SOLUSDT": rows}, base,
                  min_notional_by_symbol={"SOLUSDT": 5.0})
        self.assertEqual(sim.trades, [])
        self.assertEqual(sim.minimum_blocks, 1)

    def test_no_order_capability(self):
        import mtf_continuation
        source = inspect.getsource(mtf_continuation)
        for bad in ("import exchange", "import trading", "create_order(", "place_order("):
            self.assertNotIn(bad, source)


if __name__ == "__main__":
    unittest.main()
