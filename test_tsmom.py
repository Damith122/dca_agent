import math
import unittest

import backtest_breakout
from breakout import Candle
from tsmom import TSMOMParams, momentum_score, position_leverage, run, stats


def bars(closes, *, opens=None, lows=None, highs=None):
    opens = opens or closes
    lows = lows or [min(o, c) * 0.999 for o, c in zip(opens, closes)]
    highs = highs or [max(o, c) * 1.001 for o, c in zip(opens, closes)]
    return [Candle(i * 86400.0, float(o), float(h), float(l), float(c), 1.0)
            for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes))]


class TSMOMTests(unittest.TestCase):
    def test_shared_public_fetcher_supports_daily_bars(self):
        self.assertEqual(backtest_breakout.MS["1d"], 86_400_000)

    def test_score_has_no_future_lookahead(self):
        p = TSMOMParams(lookback=5, vol_lookback=5)
        a = [100, 101, 102, 103, 104, 106, 107]
        before = momentum_score(a, 5, p)
        a[6] = 10000
        self.assertEqual(before, momentum_score(a, 5, p))

    def test_position_size_respects_both_caps(self):
        p = TSMOMParams(risk_pct=0.02, stop_atr=2.0,
                        annual_vol_target=0.50, max_leverage=3.0)
        # stop cap=.02/(2*5/100)=.20, volatility cap=.50/.80=.625
        self.assertAlmostEqual(position_leverage(100, 5, .80, p), .20)

    def test_signal_fills_at_next_open_and_charges_both_sides(self):
        closes = [100 + i for i in range(45)]
        opens = closes.copy()
        opens[32] = 140.0
        p = TSMOMParams(lookback=5, vol_lookback=5, atr_period=3,
                        signal_threshold=0.0, rebalance_bars=1,
                        risk_pct=1.0, annual_vol_target=10.0,
                        max_leverage=1.0, stop_atr=20.0,
                        trail_start_atr=100.0, cost_bps_per_side=10.0,
                        allow_short=False)
        sim = run({"BTCUSDT": bars(closes, opens=opens)}, p=p,
                  starting_equity=1000.0, trade_start=30, trade_end=33)
        self.assertTrue(sim.trades)
        # Signal is made at close[30], so entry is open[31], never close[30].
        self.assertEqual(sim.trades[0].entry, opens[31])
        self.assertGreater(sim.trades[0].fees, 0.0)

    def test_long_only_never_shorts_falling_market(self):
        closes = [200 - i for i in range(80)]
        p = TSMOMParams(lookback=5, vol_lookback=5, atr_period=3,
                        signal_threshold=0.0, rebalance_bars=1,
                        allow_short=False)
        sim = run({"BTCUSDT": bars(closes)}, p=p, trade_start=20)
        self.assertEqual(sim.trades, [])

    def test_gap_stop_uses_worse_open(self):
        closes = [100 + i * .5 for i in range(40)]
        opens = closes.copy()
        lows = [c * .999 for c in closes]
        highs = [c * 1.001 for c in closes]
        opens[23] = 70.0
        lows[23] = 69.0
        highs[23] = 71.0
        closes[23] = 70.0
        p = TSMOMParams(lookback=5, vol_lookback=5, atr_period=3,
                        signal_threshold=0.0, rebalance_bars=1,
                        risk_pct=1.0, annual_vol_target=10.0,
                        max_leverage=1.0, stop_atr=1.0,
                        trail_start_atr=100.0, allow_short=False)
        sim = run({"BTCUSDT": bars(closes, opens=opens, lows=lows, highs=highs)},
                  p=p, trade_start=20, trade_end=25)
        stopped = [t for t in sim.trades if t.reason == "gap_stop"]
        self.assertTrue(stopped)
        self.assertEqual(stopped[0].exit, 70.0)

    def test_positive_funding_hurts_long(self):
        closes = [100 + i * .3 for i in range(50)]
        p = TSMOMParams(lookback=5, vol_lookback=5, atr_period=3,
                        signal_threshold=0.0, rebalance_bars=1,
                        risk_pct=1.0, annual_vol_target=10.0,
                        max_leverage=1.0, stop_atr=20.0,
                        trail_start_atr=100.0, allow_short=False)
        no_fund = run({"BTCUSDT": bars(closes)}, p=p, trade_start=20)
        f = {"BTCUSDT": {i * 86400: 5.0 for i in range(50)}}
        with_fund = run({"BTCUSDT": bars(closes)}, funding_bps_by_day=f,
                        p=p, trade_start=20)
        self.assertLess(with_fund.final_equity, no_fund.final_equity)

    def test_non_rebalance_day_does_not_force_exit(self):
        closes = [100 + i * .4 for i in range(60)]
        p = TSMOMParams(lookback=5, vol_lookback=5, atr_period=3,
                        signal_threshold=0.0, rebalance_bars=7,
                        risk_pct=1.0, annual_vol_target=10.0,
                        max_leverage=1.0, stop_atr=20.0,
                        trail_start_atr=100.0, allow_short=False)
        sim = run({"BTCUSDT": bars(closes)}, p=p,
                  trade_start=20, trade_end=50)
        self.assertEqual(len(sim.trades), 1)
        self.assertEqual(sim.trades[0].reason, "end")

    def test_rebalance_calendar_does_not_reset_at_fold_start(self):
        closes = [100 + i * .4 for i in range(60)]
        p = TSMOMParams(lookback=5, vol_lookback=5, atr_period=3,
                        signal_threshold=0.0, rebalance_bars=7,
                        risk_pct=1.0, annual_vol_target=10.0,
                        max_leverage=1.0, stop_atr=20.0,
                        trail_start_atr=100.0, allow_short=False)
        sim = run({"BTCUSDT": bars(closes)}, p=p,
                  trade_start=20, trade_end=30)
        self.assertTrue(sim.trades)
        # Global signal day is index 21; the next-open fill is index 22.
        self.assertEqual(sim.trades[0].entry_ts, 22 * 86400.0)

    def test_ambiguous_trailing_bar_takes_adverse_path(self):
        closes = [100 + i for i in range(30)]
        opens = closes.copy()
        highs = [c * 1.001 for c in closes]
        lows = [c * .999 for c in closes]
        # Entry follows the day-20 signal.  The next bar both establishes a
        # much higher trailing level and trades below it.
        highs[22] = 130.0
        lows[22] = 102.0
        closes[22] = 120.0
        p = TSMOMParams(lookback=5, vol_lookback=5, atr_period=3,
                        signal_threshold=0.0, rebalance_bars=1,
                        risk_pct=1.0, annual_vol_target=10.0,
                        max_leverage=1.0, stop_atr=20.0,
                        trail_start_atr=1.0, trail_atr=1.0,
                        allow_short=False)
        sim = run({"BTCUSDT": bars(closes, opens=opens, lows=lows, highs=highs)},
                  p=p, trade_start=20, trade_end=24)
        self.assertTrue(any(t.reason == "trail" for t in sim.trades))


if __name__ == "__main__":
    unittest.main()
