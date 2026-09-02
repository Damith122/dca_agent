import unittest

from breakout import Candle, atr_series
from intraday import (IntradayParams, Position, _close, candidate_at,
                      exit_on_bar, hourly_trend, run)


def bars(n=260, step=.03):
    out = []
    price = 100.0
    for i in range(n):
        price += step
        out.append(Candle(i * 900.0, price - .02, price + .08,
                          price - .08, price, 100.0))
    return out


class IntradayTests(unittest.TestCase):
    def test_hourly_confirmation_does_not_read_future(self):
        p = IntradayParams(hourly_ema_fast=2, hourly_ema_slow=3)
        rows = bars(40)
        before = hourly_trend(rows, p)[25]
        rows[30] = Candle(rows[30].ts, 1, 10000, 1, 10000, 100)
        self.assertEqual(before, hourly_trend(rows, p)[25])

    def test_channel_excludes_current_breakout_bar(self):
        p = IntradayParams(channel=3, atr_period=2, volume_lookback=3,
                           volume_ratio=1.0, momentum_bars=2,
                           momentum_floor_pct=.0001,
                           atr_floor_pct=0, atr_ceiling_pct=1)
        rows = bars(20)
        i = 10
        rows[i] = Candle(rows[i].ts, rows[i].open, rows[i].high + 2,
                         rows[i].low, rows[i].close + 1, 200)
        atrs = atr_series(rows, p.atr_period)
        signal = candidate_at("SOLUSDT", rows, atrs, [1] * len(rows), i, p)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, 1)

    def test_target_and_stop_are_fee_net(self):
        p = IntradayParams(notional=10, cost_bps_per_side=7,
                           stop_pct=.0025, target_pct=.0055)
        pos = Position("SOLUSDT", 1, 0, 100, 10, 99.75, 100.55, 0)
        win = _close(pos, 100.55, 900, "target", p)
        loss = _close(pos, 99.75, 900, "stop", p)
        self.assertAlmostEqual(win.net_pnl, .041, places=6)
        self.assertAlmostEqual(loss.net_pnl, -.039, places=6)

    def test_same_bar_stop_wins_over_target(self):
        pos = Position("SOLUSDT", 1, 0, 100, 10, 99.75, 100.55, 0)
        bar = Candle(900, 100, 101, 99, 100, 1)
        price, reason = exit_on_bar(pos, bar)
        self.assertEqual(price, 99.75)
        self.assertEqual(reason, "stop")

    def test_empty_input_is_safe(self):
        sim = run({}, IntradayParams())
        self.assertEqual(sim.final_equity, 15.0)
        self.assertEqual(sim.trades, [])


if __name__ == "__main__":
    unittest.main()
