import unittest
from paper_validation import paper_environment


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


if __name__ == "__main__":
    unittest.main()
