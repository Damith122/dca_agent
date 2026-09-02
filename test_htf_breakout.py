import inspect
import unittest
from dataclasses import replace

from breakout import Candle
from htf_breakout import (HTFBreakoutParams, _completed_daily, align_candles,
                          prepare_features, run, signals_at)


def series(shift=0.0, both=False):
    n = 55 * 6 + 3
    rows = []
    for i in range(n):
        px = 100 + shift + i * 0.03
        rows.append(Candle(i * 14400.0, px - 0.02, px + 0.08,
                           px - 0.08, px, 100.0))
    i = n - 2
    prior_hi = max(c.high for c in rows[i - 40:i])
    close = prior_hi + 0.20
    rows[i] = Candle(i * 14400.0, close - 0.1, close + 0.05,
                     close - 0.15, close, 200.0)
    entry = close + 0.02
    rows[-1] = Candle((n - 1) * 14400.0, entry,
                      entry + (3.0 if both else 0.05),
                      entry - (3.0 if both else 0.05), entry, 100.0)
    return rows


class HTFBreakoutTests(unittest.TestCase):
    def test_daily_close_not_visible_until_day_complete(self):
        rows = [Candle(i * 14400.0, 1, 1, 1, float(i + 1), 1) for i in range(12)]
        close, _, _ = _completed_daily(rows, 1, 2)
        self.assertTrue(all(v != v for v in close[:5]))
        self.assertEqual(close[5], 6.0)
        self.assertEqual(close[10], 6.0)
        self.assertEqual(close[11], 12.0)

    def test_breakout_signal_and_next_open(self):
        rows = series()
        _, aligned = align_candles({"SOLUSDT": rows})
        p = replace(HTFBreakoutParams(), min_atr_pct=0.0,
                    max_stop_risk_pct=0.10)
        f = prepare_features(aligned, p)
        self.assertEqual(signals_at(aligned, f, len(rows) - 2, p)[0].side, 1)
        sim = run({"SOLUSDT": rows}, p)
        self.assertEqual(len(sim.trades), 1)
        self.assertEqual(sim.trades[0].entry_ts, rows[-1].ts)

    def test_pessimistic_stop_and_fees(self):
        p = replace(HTFBreakoutParams(), min_atr_pct=0.0,
                    max_stop_risk_pct=0.10)
        sim = run({"SOLUSDT": series(both=True)}, p)
        self.assertEqual(sim.trades[0].reason, "stop")
        self.assertLess(sim.trades[0].net_pnl, 0)
        self.assertGreater(sim.total_fees, 0)

    def test_one_position_across_symbols(self):
        p = replace(HTFBreakoutParams(), min_atr_pct=0.0,
                    max_stop_risk_pct=0.10)
        sim = run({"SOLUSDT": series(), "SUIUSDT": series(shift=10)}, p)
        self.assertEqual(len(sim.trades), 1)

    def test_minimum_and_risk_can_block(self):
        rows = series()
        base = replace(HTFBreakoutParams(), min_atr_pct=0.0,
                       max_stop_risk_pct=0.10)
        sim = run({"SOLUSDT": rows}, base,
                  min_notional_by_symbol={"SOLUSDT": 20})
        self.assertEqual(sim.min_notional_blocks, 1)
        tight = replace(HTFBreakoutParams(), min_atr_pct=0.0,
                        max_stop_risk_pct=0.0001)
        sim = run({"SOLUSDT": rows}, tight)
        self.assertEqual(sim.risk_blocks, 1)

    def test_no_order_client(self):
        import htf_breakout
        src = inspect.getsource(htf_breakout)
        for bad in ("import exchange", "import trading", "create_order(", "place_order("):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
