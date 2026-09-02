import inspect
import unittest
from dataclasses import replace

from breakout import Candle
from xs_pair import (PairParams, PairPosition, _close, _mark_net,
                     align_candles, choose_pair, run, score_vector)


def panel(days=140):
    slopes = {
        "SOLUSDT": 0.30,
        "SUIUSDT": -0.20,
        "BNBUSDT": 0.12,
        "XRPUSDT": -0.05,
        "TRXUSDT": 0.04,
        "DOGEUSDT": -0.10,
    }
    out = {}
    for symbol, slope in slopes.items():
        rows = []
        for i in range(days):
            # Deterministic small oscillation prevents zero volatility.
            close = 100.0 + slope * i + (0.15 if i % 2 else -0.15)
            rows.append(Candle(i * 86400.0, close - slope * 0.2,
                               close + 0.4, close - 0.4, close, 1000.0))
        out[symbol] = rows
    return out


class RelativeStrengthPairTests(unittest.TestCase):
    def test_strongest_long_weakest_short(self):
        series = panel()
        _, aligned = align_candles(series)
        scores = score_vector(aligned, 80, PairParams())
        signal = choose_pair(scores)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.long_symbol, "SOLUSDT")
        self.assertEqual(signal.short_symbol, "SUIUSDT")

    def test_skip_days_prevents_recent_move_lookahead(self):
        series = panel()
        _, aligned = align_candles(series)
        p = PairParams(skip_days=3)
        before = score_vector(aligned, 80, p)
        for symbol in aligned:
            for i in (78, 79, 80):
                bar = aligned[symbol][i]
                aligned[symbol][i] = Candle(bar.ts, bar.open, bar.high,
                                            bar.low, bar.close * 10.0, bar.volume)
        after = score_vector(aligned, 80, p)
        self.assertEqual(before, after)

    def test_signal_fills_only_at_next_daily_open(self):
        series = panel()
        p = replace(PairParams(), pair_stop_usd=-100.0,
                    pair_target_usd=100.0, max_hold_days=28)
        sim = run(series, p, starting_equity=15.0)
        self.assertGreater(len(sim.trades), 0)
        first = sim.trades[0]
        signal_day = int(first.entry_ts // 86400) - 1
        self.assertEqual(signal_day % p.rebalance_days, p.rebalance_offset)

    def test_only_one_pair_is_active(self):
        series = panel()
        p = replace(PairParams(), pair_stop_usd=-100.0,
                    pair_target_usd=100.0, max_hold_days=28)
        sim = run(series, p, starting_equity=15.0)
        ordered = sorted(sim.trades, key=lambda trade: trade.entry_ts)
        for left, right in zip(ordered[:-1], ordered[1:]):
            self.assertLessEqual(left.exit_ts, right.entry_ts)

    def test_round_trip_math_has_four_leg_fees(self):
        p = PairParams(cost_bps_per_side=7.0)
        pos = PairPosition("SOLUSDT", "SUIUSDT", 0, 0.0,
                           100.0, 100.0, 0.05, 0.05, 0.007, 15.0)
        wallet, trade, _ = _close(pos, 103.0, 97.0, 86400.0,
                                  "pair_target", 14.993, p)
        self.assertAlmostEqual(trade.gross_pnl, 0.30, places=6)
        self.assertAlmostEqual(trade.fees, 0.014, places=4)
        self.assertAlmostEqual(wallet, 15.286, places=3)
        self.assertGreater(trade.net_pnl, 0.28)

    def test_fee_net_mark_drives_pair_stop(self):
        p = PairParams()
        pos = PairPosition("SOLUSDT", "SUIUSDT", 0, 0.0,
                           100.0, 100.0, 0.05, 0.05, 0.007, 15.0)
        self.assertLess(_mark_net(pos, 97.0, 103.0, p), -0.30)

    def test_minimum_order_rounding_is_bounded(self):
        series = panel()
        p = replace(PairParams(), max_rounded_notional_usd=10.5,
                    pair_stop_usd=-100.0, pair_target_usd=100.0)
        minimums = {symbol: 6.0 for symbol in series}
        steps = {symbol: 0.001 for symbol in series}
        sim = run(series, p, starting_equity=15.0,
                  min_notional_by_symbol=minimums,
                  qty_step_by_symbol=steps)
        self.assertEqual(sim.trades, [])
        self.assertGreater(sim.blocked_min_notional, 0)

    def test_daily_loss_gate_fails_closed(self):
        series = panel()
        p = replace(PairParams(), daily_loss_gate=-0.001,
                    pair_stop_usd=-100.0, pair_target_usd=100.0)
        sim = run(series, p, starting_equity=15.0)
        self.assertGreater(sim.daily_loss_lock_hits, 0)

    def test_engine_has_no_order_client(self):
        import xs_pair
        source = inspect.getsource(xs_pair)
        for forbidden in ("import exchange", "import trading", "create_order(",
                          "new_order(", "place_order("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
