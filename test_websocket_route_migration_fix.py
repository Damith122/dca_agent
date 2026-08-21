"""Offline regression tests for Binance Futures WebSocket route migration.

No socket or REST request leaves the process.
Run: python3 test_websocket_route_migration_fix.py
"""

import asyncio
import contextlib
import io

import websocket as ws_module


class _StubManager:
    """2026-08-20 multi-coin: market_data_consumer() now builds its stream
    URLs from the manager's OWN symbol rather than the module-level SYMBOL
    global, so the stub has to carry one."""
    symbol = "SOLUSDT"


async def test_live_market_routes_are_split():
    captured = []
    original_use_testnet = ws_module.USE_TESTNET
    original_runner = ws_module._run_single_market_stream
    try:
        ws_module.USE_TESTNET = False

        async def fake_runner(manager, url, label, stream_suffix):
            captured.append((url, label, stream_suffix))

        ws_module._run_single_market_stream = fake_runner
        await ws_module.market_data_consumer(_StubManager())
    finally:
        ws_module.USE_TESTNET = original_use_testnet
        ws_module._run_single_market_stream = original_runner

    assert len(captured) == 3   # bookTicker + depth + aggTrade
    by_suffix = {suffix: url for url, _label, suffix in captured}
    assert "/public/stream?streams=" in by_suffix["bookTicker"]
    assert by_suffix["bookTicker"].endswith("@bookTicker")
    assert "/market/stream?streams=" in by_suffix["aggTrade"]
    assert by_suffix["aggTrade"].endswith("@aggTrade")


async def test_testnet_market_route_stays_legacy_combined():
    captured = []
    original_use_testnet = ws_module.USE_TESTNET
    original_runner = ws_module._run_single_market_stream
    try:
        ws_module.USE_TESTNET = True

        async def fake_runner(manager, url, label, stream_suffix):
            captured.append((url, label, stream_suffix))

        ws_module._run_single_market_stream = fake_runner
        await ws_module.market_data_consumer(_StubManager())
    finally:
        ws_module.USE_TESTNET = original_use_testnet
        ws_module._run_single_market_stream = original_runner

    assert len(captured) == 1
    url, label, suffix = captured[0]
    assert "/stream?streams=" in url
    assert "@bookTicker/" in url and url.endswith("@aggTrade")
    assert label == "testnet-combined:SOLUSDT"
    assert suffix == "both"


class _FailingWebSocketContext:
    async def __aenter__(self):
        raise RuntimeError("intentional offline stop")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _OneListenKeyClient:
    def __init__(self, listen_key):
        self.listen_key = listen_key
        self.calls = 0

    def is_cooldown_active(self):
        return False

    async def create_listen_key(self):
        self.calls += 1
        if self.calls == 1:
            return self.listen_key
        raise asyncio.CancelledError


async def _capture_userdata_url(use_testnet):
    secret_listen_key = "TEST_LISTEN_KEY_MUST_NOT_APPEAR_IN_LOGS"
    client = _OneListenKeyClient(secret_listen_key)
    captured_urls = []
    original_use_testnet = ws_module.USE_TESTNET
    original_connect = ws_module.websockets.connect
    original_sleep = ws_module.asyncio.sleep
    try:
        ws_module.USE_TESTNET = use_testnet

        def fake_connect(url, **kwargs):
            captured_urls.append(url)
            return _FailingWebSocketContext()

        async def no_sleep(_seconds):
            return None

        ws_module.websockets.connect = fake_connect
        ws_module.asyncio.sleep = no_sleep
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                await ws_module.userdata_consumer(client, object())
            except asyncio.CancelledError:
                pass
    finally:
        ws_module.USE_TESTNET = original_use_testnet
        ws_module.websockets.connect = original_connect
        ws_module.asyncio.sleep = original_sleep

    assert len(captured_urls) == 1
    assert secret_listen_key not in output.getvalue()
    return captured_urls[0], secret_listen_key


async def test_live_userdata_route_is_private_and_explicit():
    url, listen_key = await _capture_userdata_url(use_testnet=False)
    assert "/private/ws?" in url
    assert f"listenKey={listen_key}" in url
    assert "events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE" in url


async def test_testnet_userdata_route_stays_legacy():
    url, listen_key = await _capture_userdata_url(use_testnet=True)
    assert url.endswith(f"/ws/{listen_key}")
    assert "/private/" not in url


async def main():
    await test_live_market_routes_are_split()
    await test_testnet_market_route_stays_legacy_combined()
    await test_live_userdata_route_is_private_and_explicit()
    await test_testnet_userdata_route_stays_legacy()
    print("ALL WEBSOCKET ROUTE MIGRATION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
