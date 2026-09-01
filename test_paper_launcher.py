import unittest
from types import SimpleNamespace

from paper_validation import paper_environment
from paper_summary import build_paper_summary


class LauncherTests(unittest.TestCase):
    def test_inherited_live_settings_and_credentials_are_not_used(self):
        original = {"DRY_RUN": "false", "LIVE_TRADING_CONFIRMATION": "true",
                    "BINANCE_API_KEY": "DO_NOT_SEND", "BINANCE_API_SECRET": "DO_NOT_SEND",
                    "GITHUB_TOKEN": "DO_NOT_SEND", "TRADE_LOG_JSON_PATH": "trades_log_LIVE_SOLUSDT.jsonl",
                    "BRAIN_LOCAL_PATH": "brain_LIVE_SOLUSDT.pkl", "MAX_DCA_STEPS": "5",
                    "BREAKOUT_ENGINE_ENABLED": "true", "PATH": "/usr/bin"}
        result = paper_environment(original)
        self.assertEqual(result["DRY_RUN"], "true")
        self.assertEqual(result["LIVE_TRADING_CONFIRMATION"], "false")
        self.assertEqual(result["BINANCE_API_KEY"], "")
        self.assertEqual(result["BINANCE_API_SECRET"], "")
        self.assertEqual(result["GITHUB_TOKEN"], "")
        self.assertNotIn("BRAIN_LOCAL_PATH", result)
        self.assertNotIn("TRADE_LOG_JSON_PATH", result)
        self.assertNotIn("BREAKOUT_ENGINE_ENABLED", result)
        self.assertEqual(result["PATH"], original["PATH"])
        self.assertEqual(result["MAX_DCA_STEPS"], "0")
        self.assertEqual(original["DRY_RUN"], "false")

    def test_final_summary_includes_open_fee_net_mark_to_market(self):
        class FakeStats:
            def __init__(self, win_rate):
                self.win_rate = win_rate

            def compute(self):
                return {"win_rate": self.win_rate}

        flat = SimpleNamespace(status="FLAT", side=None, total_qty=0.0,
                               avg_entry_price=None)
        open_long = SimpleNamespace(status="OPEN", side="LONG", total_qty=13.6,
                                    avg_entry_price=0.73019601)
        closed_manager = SimpleNamespace(
            trade_count=2,
            realized_pnl_total=0.12,
            perf_stats=FakeStats(0.5),
            position=flat,
        )
        open_manager = SimpleNamespace(
            trade_count=0,
            realized_pnl_total=0.0,
            perf_stats=FakeStats(0.0),
            position=open_long,
            best_bid_price=0.72865,
            best_ask_price=0.72875,
            current_price=0.72870,
            estimate_net_pnl_usdt_executable=lambda: -0.0309,
        )
        portfolio = SimpleNamespace(paper_wallet=15.115)

        summary = build_paper_summary(
            portfolio,
            {"SOLUSDT": closed_manager, "SUIUSDT": open_manager},
            starting_balance=15.0,
        )

        self.assertEqual(summary["schema"], "paper_summary_v1")
        self.assertEqual(summary["closed_trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["open_position_count"], 1)
        self.assertAlmostEqual(summary["realized_fee_net_pnl_usdt"], 0.12)
        self.assertAlmostEqual(summary["open_fee_net_pnl_estimate_usdt"], -0.0309)
        self.assertAlmostEqual(summary["estimated_total_fee_net_pnl_usdt"], 0.0891)
        self.assertAlmostEqual(summary["estimated_equity_if_flattened_usdt"], 15.0891)
        self.assertEqual(summary["open_positions"][0]["symbol"], "SUIUSDT")
        self.assertEqual(summary["open_positions"][0]["mark"], 0.72865)

    def test_final_summary_is_zero_when_no_trade_was_opened(self):
        class EmptyStats:
            def compute(self):
                return {"trade_count": 0}

        manager = SimpleNamespace(
            trade_count=0,
            realized_pnl_total=0.0,
            perf_stats=EmptyStats(),
            position=SimpleNamespace(status="FLAT", side=None, total_qty=0.0,
                                     avg_entry_price=None),
        )
        summary = build_paper_summary(
            SimpleNamespace(paper_wallet=15.0),
            {"SOLUSDT": manager},
            starting_balance=15.0,
        )

        self.assertEqual(summary["closed_trades"], 0)
        self.assertEqual(summary["open_position_count"], 0)
        self.assertEqual(summary["estimated_total_fee_net_pnl_usdt"], 0.0)
        self.assertEqual(summary["estimated_equity_if_flattened_usdt"], 15.0)


if __name__ == "__main__":
    unittest.main()
