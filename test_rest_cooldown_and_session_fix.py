"""
Regression tests for the 2026-08 RestClient HTTP 418/429 global cooldown +
session-cleanup fix.

Root cause (confirmed from code): RestClient._request() had no shared
cooldown - every poller (balance_refresher, position_risk_poller,
funding_oi_poller, listen_key_keepalive) and every _manage_open_position()
order-placement call independently kept hitting Binance every 10/60/120s
even after an HTTP 418 (IP ban) or 429 (rate limit) response, digging the
ban deeper. Separately, RestClient.start() unconditionally created a brand
new aiohttp.ClientSession on every call - a failed attempt (e.g.
_sync_server_time() raising after the session was created) left that
session referenced nowhere once retry_with_backoff() called start() again,
producing "Unclosed client session"/"Unclosed connector" warnings.

Fix (this file's target):
  1. BinanceApiError now carries the response headers.
  2. RestClient._request() arms ONE shared cooldown
     (self._cooldown_until_ts) the moment it sees a 418/429 response -
     parsing Retry-After first, then Binance's "banned until <ms>"
     message as a fallback, then a conservative 60s default. While that
     cooldown is active, _request() refuses to send ANY further request
     (raising the same BinanceApiError type every caller already catches)
     instead of hitting the network again.
  3. RestClient.start() now closes any pre-existing session before
     creating a new one, and closes the session it just opened if
     _sync_server_time() fails, before re-raising - so a chain of failed
     retries never leaves more than one open session at a time.

This file is self-contained (exchange.py has no dependency on trading.py/
dca2.py/config.py) - it fakes aiohttp's session/response objects directly,
no real network call is ever made.

Run directly: `python3 test_rest_cooldown_and_session_fix.py`
"""
import asyncio
import time

from exchange import RestClient, BinanceApiError


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    async def text(self):
        import json
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeRequestContext:
    """Wraps a FakeResponse so `session.request(...)` can be used exactly
    like aiohttp's own `async with session.request(...) as resp:` pattern,
    without any real network I/O."""
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Records every call to `.request(...)` and returns responses from a
    pre-configured queue, in order. `.closed` / `.close()` mirror aiohttp's
    own ClientSession enough for RestClient.start()/close() to work
    against it."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.request_calls = []
        self.closed = False

    def request(self, method, url, params=None, timeout=None):
        self.request_calls.append((method, url, params))
        if not self._responses:
            raise AssertionError("FakeSession ran out of configured responses")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return FakeRequestContext(resp)

    async def close(self):
        self.closed = True


def make_client_with_session(responses):
    client = RestClient(api_key="k", api_secret="s", base_url="https://example.invalid")
    client.session = FakeSession(responses)
    return client


# ============================================================================
# TEST 1-2: HTTP 418 arms one shared cooldown; repeated poll attempts
# during cooldown produce no additional network requests.
# ============================================================================
async def test_418_arms_cooldown_and_blocks_further_requests():
    print("\n=== test_418_arms_cooldown_and_blocks_further_requests ===")
    resp_418 = FakeResponse(418, body={"code": -1003, "msg": "banned until 9999999999999"}, headers={})
    client = make_client_with_session([resp_418])

    try:
        await client._request("GET", "/fapi/v2/positionRisk", {"symbol": "SOLUSDT"}, signed=True)
        assert False, "expected BinanceApiError on the 418 response"
    except BinanceApiError as e:
        print(f"TEST 1: first call raised {e.status} as expected, cooldown_until={client._cooldown_until_ts:.0f}")
        assert e.status == 418
        assert client._cooldown_until_ts > time.time(), "cooldown must be armed after a 418"

    # 10 more "poller" attempts during the cooldown window - none may reach
    # the fake session's request() at all.
    calls_before = len(client.session.request_calls)
    for _ in range(10):
        try:
            await client._request("GET", "/fapi/v2/positionRisk", {"symbol": "SOLUSDT"}, signed=True)
            assert False, "expected BinanceApiError while cooldown is active"
        except BinanceApiError as e:
            assert e.code == -1003 or "cooldown" in str(e.data).lower()
    calls_after = len(client.session.request_calls)
    print(f"TEST 2: request_calls before={calls_before} after 10 attempts={calls_after}")
    assert calls_after == calls_before, "no additional network requests may be sent while cooldown is active"
    print("TEST 1-2: PASS - one shared cooldown armed by 418, repeated polls send zero further requests\n")


async def test_429_with_retry_after_header_arms_cooldown():
    print("=== test_429_with_retry_after_header_arms_cooldown ===")
    resp_429 = FakeResponse(429, body={"code": -1003, "msg": "Too many requests"}, headers={"Retry-After": "5"})
    client = make_client_with_session([resp_429])
    before = time.time()
    try:
        await client._request("GET", "/fapi/v1/time")
        assert False
    except BinanceApiError:
        pass
    remaining = client._cooldown_until_ts - before
    print(f"TEST: 429 with Retry-After=5 -> cooldown remaining={remaining:.1f}s")
    assert 4.0 <= remaining <= 6.0, f"Retry-After header must be honored, got {remaining:.1f}s remaining"
    print("PASS - Retry-After header parsed and used for cooldown duration\n")


# ============================================================================
# TEST 3: cooldown expiry permits a request again.
# ============================================================================
async def test_cooldown_expiry_permits_request_again():
    print("=== test_cooldown_expiry_permits_request_again ===")
    resp_ok = FakeResponse(200, body={"serverTime": 123456789})
    client = make_client_with_session([resp_ok])
    client._cooldown_until_ts = time.time() - 1.0  # already expired

    data = await client._request("GET", "/fapi/v1/time")
    print(f"TEST: request after expired cooldown -> data={data}, request_calls={len(client.session.request_calls)}")
    assert data == {"serverTime": 123456789}
    assert len(client.session.request_calls) == 1, "an expired cooldown must permit the request through"
    print("PASS - expired cooldown allows a request through again\n")


# ============================================================================
# TEST 4: failed RestClient.start() retries leave no open sessions.
# ============================================================================
async def test_failed_start_retries_leave_no_open_sessions():
    print("=== test_failed_start_retries_leave_no_open_sessions ===")
    import exchange as exchange_module

    created_sessions = []
    real_client_session_cls = exchange_module.aiohttp.ClientSession

    class _TrackedFakeSession:
        """Stands in for aiohttp.ClientSession itself (not just the
        request layer), so this test exercises the REAL RestClient.start()
        method end-to-end, including its own session-close bookkeeping."""
        def __init__(self, *a, **kw):
            self.closed = False
            created_sessions.append(self)

        async def close(self):
            self.closed = True

    client = RestClient(api_key="k", api_secret="s", base_url="https://example.invalid")

    call_count = {"n": 0}

    async def fake_sync_server_time():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("simulated network failure on attempt 1")
        client._time_offset_ms = 0

    exchange_module.aiohttp.ClientSession = _TrackedFakeSession
    client._sync_server_time = fake_sync_server_time
    try:
        try:
            await client.start()
            assert False, "expected the simulated network failure to propagate out of start()"
        except ConnectionError:
            pass

        print(f"TEST 4a: after failed attempt 1 -> client.session is None: {client.session is None}, "
              f"session 1 closed: {created_sessions[0].closed}")
        assert client.session is None, "a failed start() attempt must clear self.session"
        assert created_sessions[0].closed, "the session opened by the failed attempt must be closed before re-raising"

        # retry_with_backoff() would call start() again exactly like this:
        await client.start()
        print(f"TEST 4b: after successful attempt 2 -> session 1 closed: {created_sessions[0].closed}, "
              f"session 2 closed: {created_sessions[1].closed}, "
              f"current session is session 2: {client.session is created_sessions[1]}")
        assert created_sessions[0].closed, "the first (failed) session must remain closed"
        assert not created_sessions[1].closed, "the second (successful) session must still be open"
        assert client.session is created_sessions[1], "only ONE live session may exist after retries succeed"
        assert len(created_sessions) == 2, "exactly two sessions total must ever have been created across both attempts"
        print("TEST 4: PASS - failed start() retries leave no open/orphaned sessions behind\n")
    finally:
        exchange_module.aiohttp.ClientSession = real_client_session_cls



async def main():
    await test_418_arms_cooldown_and_blocks_further_requests()
    await test_429_with_retry_after_header_arms_cooldown()
    await test_cooldown_expiry_permits_request_again()
    await test_failed_start_retries_leave_no_open_sessions()
    print("ALL REST COOLDOWN + SESSION CLEANUP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
