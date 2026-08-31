"""Offline, standard-library tests; no credentials or network required."""
import copy
import unittest

from exposure_guard import assess_add


def account(equity=16.0, gross=0.0, symbol="SOLUSDT", short=False):
    # The V2 wire format deliberately does NOT include a notional field.
    return {"multiAssetsMargin": False, "canTrade": True,
            "totalOpenOrderInitialMargin": "0", "totalCrossWalletBalance": str(equity),
            "totalCrossUnPnl": "0", "availableBalance": str(equity),
            "totalMaintMargin": "0", "positions": ([] if not gross else [{
                "symbol": symbol, "positionAmt": "-1" if short else "1",
                "entryPrice": str(gross), "unrealizedProfit": "0",
                "positionSide": "BOTH", "isolated": False}])}


def check(data, add=60, **kwargs):
    return assess_add(data, symbol="SOLUSDT", add_notional=add, leverage=20, **kwargs)


class ExposureTests(unittest.TestCase):
    def test_observed_dca_scale_is_refused_for_both_directions(self):
        for short in (False, True):
            with self.subTest(short=short):
                verdict = check(account(gross=60, short=short), 55)
                self.assertFalse(verdict.allowed)
                self.assertAlmostEqual(verdict.gross_notional, 115)
                self.assertGreater(verdict.required_equity, 16)

    def test_second_coin_spends_same_collateral(self):
        self.assertTrue(check(account(), 60).allowed)
        self.assertFalse(check(account(gross=60, symbol="SUIUSDT"), 60).allowed)

    def test_small_position_fits_without_raising_leverage(self):
        self.assertTrue(assess_add(account(15), symbol="SOLUSDT", add_notional=10,
                                   leverage=5).allowed)

    def test_unknown_and_nonfinite_collateral_fail_closed(self):
        for field in ("totalCrossWalletBalance", "totalCrossUnPnl", "availableBalance",
                      "totalMaintMargin", "totalOpenOrderInitialMargin", "positions"):
            d = account(); del d[field]
            self.assertFalse(check(d).allowed, field)
        for value in ("nan", "inf", "-inf", None):
            d = account(); d["totalCrossWalletBalance"] = value
            self.assertFalse(check(d).allowed)

    def test_positive_pnl_is_not_spendable_edge(self):
        d = account(gross=60); d["totalCrossUnPnl"] = "100"
        self.assertFalse(check(d).allowed)
        d = account(); d["totalCrossUnPnl"] = "-10"
        self.assertFalse(check(d).allowed)

    def test_external_orders_or_unsupported_modes_block(self):
        for field, value in (("totalOpenOrderInitialMargin", "1"),
                             ("multiAssetsMargin", True), ("canTrade", False)):
            d = account(); d[field] = value
            self.assertFalse(check(d).allowed)
        for field, value in (("isolated", True), ("positionSide", "LONG"),
                             ("symbol", "BTCUSDC")):
            d = account(gross=10); d["positions"][0][field] = value
            self.assertFalse(check(d, 5).allowed)

    def test_local_fill_cannot_hide_external_exposure(self):
        d = account(gross=30, symbol="SUIUSDT")
        result = check(d, 10, local_notionals={"SOLUSDT": 60, "SUIUSDT": 20})
        self.assertEqual(result.gross_notional, 100)
        self.assertFalse(result.allowed)

    def test_maintenance_and_free_margin_are_separate_constraints(self):
        d = account(16); d["totalMaintMargin"] = "15"
        self.assertFalse(check(d, 10).allowed)
        d = account(16); d["availableBalance"] = "0.1"
        self.assertEqual(check(d, 10).reason, "insufficient free initial margin")

    def test_v2_mark_valuation_long_and_short(self):
        for short, pnl in ((False, 10), (True, -10)):
            d = account(100, 60, short=short)
            d["positions"][0]["unrealizedProfit"] = str(pnl)
            self.assertEqual(check(d, 10).gross_notional, 80)

    def test_no_mutation_or_offsetting_credit(self):
        d = account(100, 30); original = copy.deepcopy(d)
        check(d, 10)
        self.assertEqual(d, original)
        d["positions"].append({**d["positions"][0], "symbol": "SUIUSDT", "positionAmt": "-1"})
        self.assertEqual(check(d, 10).gross_notional, 70)


if __name__ == "__main__":
    unittest.main()
