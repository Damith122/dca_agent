"""Offline behavioral integration tests, with all exchange methods faked."""
import os
os.environ["DRY_RUN"] = "true"
os.environ["USE_TESTNET"] = "false"
os.environ["MAX_DCA_STEPS"] = "0"
os.environ["EXPOSURE_GUARD_ENABLED"] = "true"
os.environ["SYMBOL"] = "SOLUSDT"

import asyncio
import copy
import time
import math
import unittest
from unittest.mock import AsyncMock, patch

import config
import trading
import dca2
from test_exposure_guard import account


class AdmissionIntegration(unittest.IsolatedAsyncioTestCase):
    def manager(self, portfolio=None, symbol="SOLUSDT", snapshot=None):
        client = type("Client", (), {})()
        client.get_account = AsyncMock(return_value=copy.deepcopy(snapshot or account(15)))
        filters = trading.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5)
        m = trading.MartingaleManager(client, symbol, filters, 5, portfolio)
        m.current_price = 100.0
        m.position_sync_ready = True
        m.notional_for_step = lambda step, mult: 10.0
        m._place_step_order_admitted = AsyncMock()
        return m

    async def test_account_failure_leaves_open_position_and_exits_untouched(self):
        m = self.manager()
        m.position = trading.PositionState(status="OPEN", side="LONG", total_qty=0.6,
                                           avg_entry_price=100, dca_step=0)
        m.client.get_account.side_effect = TimeoutError()
        original = m.position
        with patch.object(trading, "DRY_RUN", False), patch.object(trading, "MAX_DCA_STEPS", 1):
            await m._place_step_order(1, "LONG", expected_position=original)
        m._place_step_order_admitted.assert_not_called()
        self.assertIs(m.position, original)
        self.assertEqual(m.position.status, "OPEN")
        self.assertEqual(m.position.dca_step, 0)

    async def test_no_dca_mode_and_risk_score_are_valid(self):
        m = self.manager()
        m.position = trading.PositionState(status="OPEN", side="LONG", total_qty=0.1, avg_entry_price=100)
        await m._place_step_order(1, "LONG", expected_position=m.position)
        m._place_step_order_admitted.assert_not_called()
        score = m.risk_engine.score(m.last_regime, 0, 0.0, None)
        self.assertTrue(0 <= score <= 1)
        self.assertTrue(all(math.isfinite(x) for x in m.build_features()))

    async def test_shared_daily_loss_cannot_be_bypassed_by_continuous_mode(self):
        portfolio = trading.PortfolioCoordinator(2)
        a = self.manager(portfolio); b = self.manager(portfolio, "SUIUSDT")
        for m in (a, b):
            m._maybe_reset_daily_loss_tracker(); m.daily_realized_pnl = -0.16
        with patch.object(trading, "MAX_DAILY_LOSS_USDT", 0.30), patch.object(trading, "CONTINUOUS_24_7_TRADING", True):
            await a._place_step_order(0, "LONG")
        a._place_step_order_admitted.assert_not_called()
        self.assertAlmostEqual(portfolio.daily_net_pnl(), -0.32)

    async def test_read_race_does_not_resurrect_closed_position(self):
        m = self.manager()
        async def changed():
            m.position = trading.PositionState(status="CLOSING", side="LONG")
            return account()
        m.client.get_account.side_effect = changed
        with patch.object(trading, "DRY_RUN", False):
            await m._place_step_order(0, "LONG")
        m._place_step_order_admitted.assert_not_called()
        self.assertEqual(m.position.status, "CLOSING")

    async def test_same_object_state_race_is_also_blocked(self):
        m = self.manager()
        async def changed():
            m.position.status = "CLOSING"
            return account()
        m.client.get_account.side_effect = changed
        with patch.object(trading, "DRY_RUN", False):
            await m._place_step_order(0, "LONG")
        m._place_step_order_admitted.assert_not_called()

    async def test_concurrent_candidates_do_not_queue_stale_orders(self):
        portfolio = trading.PortfolioCoordinator(2)
        a = self.manager(portfolio); b = self.manager(portfolio, "SUIUSDT")
        started = asyncio.Event(); release = asyncio.Event()
        async def slow_read():
            started.set(); await release.wait(); return account()
        a.client.get_account.side_effect = slow_read
        with patch.object(trading, "DRY_RUN", False):
            task = asyncio.create_task(a._place_step_order(0, "LONG"))
            await started.wait()
            await b._place_step_order(0, "SHORT")
            release.set(); await task
        a._place_step_order_admitted.assert_awaited_once()
        b.client.get_account.assert_not_called()
        b._place_step_order_admitted.assert_not_called()

    async def test_pending_maker_add_blocks_other_symbols(self):
        portfolio = trading.PortfolioCoordinator(2)
        a = self.manager(portfolio); b = self.manager(portfolio, "SUIUSDT")
        a.position.status = "ENTERING"; a.position.pending_order_id = 123
        await b._place_step_order(0, "SHORT")
        b._place_step_order_admitted.assert_not_called()

    async def test_external_position_blocks_a_duplicate_initial_entry(self):
        m = self.manager(snapshot=account(100, 10))
        with patch.object(trading, "DRY_RUN", False):
            await m._place_step_order(0, "LONG")
        m._place_step_order_admitted.assert_not_called()

    async def test_account_retry_is_throttled_and_unknown_data_not_cached(self):
        m = self.manager()
        m.client.get_account.side_effect = TimeoutError()
        with patch.object(trading, "DRY_RUN", False):
            await m._place_step_order(0, "LONG")
            await m._place_step_order(0, "LONG")
        m.client.get_account.assert_awaited_once()

    async def test_paper_fill_debits_fees_from_shared_cash(self):
        m = self.manager()
        m._place_step_order_admitted = trading.MartingaleManager._place_step_order_admitted.__get__(m)
        start_cash = m.portfolio.paper_wallet
        await m._place_step_order(0, "LONG")
        m._ticks_run += 1
        # Keep the production fill-accounting dispatch network-free here.
        m.handle_order_update = AsyncMock()
        await m._resolve_dry_fills()
        event = m.handle_order_update.call_args.args[0]
        self.assertAlmostEqual(m.portfolio.paper_wallet, start_cash - float(event["o"]["n"]))

    async def test_paper_uses_own_wallet_without_account_read(self):
        m = self.manager()
        await m._place_step_order(0, "LONG")
        m.client.get_account.assert_not_called()
        m._place_step_order_admitted.assert_awaited_once()
        self.assertTrue(config.RUNTIME_ENV.startswith("PAPER_LIVE"))
        self.assertNotIn("/trades_log_LIVE_", "/" + m.paths["trade_log_json"])

    async def test_paper_slot_reserved_at_real_submission_boundary(self):
        portfolio = trading.PortfolioCoordinator(1)
        a = self.manager(portfolio); b = self.manager(portfolio, "SUIUSDT")
        for m in (a, b):
            m._place_step_order_admitted = trading.MartingaleManager._place_step_order_admitted.__get__(m)
        await a._place_step_order(0, "LONG")
        portfolio.next_exposure_check = 0
        await b._place_step_order(0, "SHORT")
        self.assertEqual(a.position.status, "ENTERING")
        self.assertEqual(b.position.status, "FLAT")
        self.assertEqual(portfolio.active_count(), 1)

    async def test_public_paper_requires_no_real_money_confirmation_or_keys(self):
        with patch.multiple(dca2, DRY_RUN=True, USE_TESTNET=False, API_KEY="", API_SECRET="",
                            LIVE_TRADING_CONFIRMATION=False, I_UNDERSTAND_THIS_IS_REAL_MONEY=False):
            dca2.enforce_safety_gates()
        with patch.multiple(dca2, DRY_RUN=False, USE_TESTNET=False, API_KEY="", API_SECRET=""):
            with self.assertRaises(SystemExit):
                dca2.enforce_safety_gates()

    async def test_restricted_location_is_not_retried(self):
        request = AsyncMock(side_effect=trading.BinanceApiError(451, {"msg": "restricted location"}))
        with patch.object(asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(SystemExit):
                await dca2.retry_with_backoff(request, label="public data")
        request.assert_awaited_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
