import unittest
from types import SimpleNamespace

from breakout import Candle
from paper_tsmom import completed_daily_candles, make_summary, parse_utc


class PaperTSMOMTests(unittest.TestCase):
    def test_parse_utc_accepts_z(self):
        self.assertEqual(parse_utc("1970-01-02T00:00:00Z"), 86400.0)

    def test_incomplete_daily_bar_is_never_a_signal(self):
        series = {"SUIUSDT": [Candle(0, 1, 1, 1, 1),
                              Candle(86400, 1, 1, 1, 1)]}
        got = completed_daily_candles(series, 86400 + 100)
        self.assertEqual(len(got["SUIUSDT"]), 1)

    def test_summary_can_never_claim_a_live_order(self):
        sim = SimpleNamespace(
            trades=[], open_position=None, estimated_exit_fee=0.0,
            final_equity=15.0, total_fees=0.0, total_funding=0.0,
            blocked_min_notional=0, equity_curve=[15.0], timestamps=[],
            floored_min_notional=0,
        )
        summary = make_summary(
            sim, symbols=["SUIUSDT"], namespace="N", start_ts=0,
            last_ts=86400, starting_equity=15.0,
            latest_closes={"SUIUSDT": 1.0},
        )
        self.assertTrue(summary["simulation_only"])
        self.assertEqual(summary["live_orders_sent"], 0)


if __name__ == "__main__":
    unittest.main()
