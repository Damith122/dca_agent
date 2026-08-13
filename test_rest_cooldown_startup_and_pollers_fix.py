"""
Regression tests for the 2026-08 HTTP 418/429 cooldown-survival fix (item 2
of the follow-up safety review): the shared RestClient cooldown must
survive `retry_with_backoff()`'s own attempt budget at startup, and every
poller must skip silently (no network call, no per-interval error log)
while it's active.

Root cause this addresses: RestClient._request() locally raising
BinanceApiError during an active cooldown (see
test_rest_cooldown_and_session_fix.py) is necessary but not sufficient -
retry_with_backoff() (dca2.py) previously treated that raised error exactly
like any other failure, consuming one of its 5 attempts and exponential-
backoff sleeping only a few seconds between them. A real Binance ban can
easily outlast 5 short backoff sleeps, so retry_with_backoff() would
exhaust its budget and raise SystemExit - which the outer run_forever()
supervisor catches and restarts the WHOLE PROCESS from, discarding the
in-memory cooldown and immediately contacting Binance again on the fresh
process, worsening the ban. Separately, every poller (balance_refresher,
funding_oi_poller, position_risk_poller, listen_key_keepalive) kept calling
through and catching+logging a fresh BinanceApiError every single interval
while cooldown was active.

Fix (this file's target):
  - retry_with_backoff() now detects a BinanceApiError with status 418/429,
    finds the RestClient instance involved, and waits out ITS shared
    cooldown (however long that takes) before retrying - WITHOUT consuming
    one of the `attempts` budget - so a long ban can never trigger
    SystemExit/restart-looping.
  - Each poller now checks client.is_cooldown_active() at the top of its
    loop and, if true, calls client.wait_out_cooldown_silently() (sleeps
    out the cooldown + a small random jitter, to avoid every poller
    resuming on the same tick) and `continue`s - skipping the REST call
    and any per-iteration log entirely for that cycle.

Run directly: `python3 test_rest_cooldown_startup_and_pollers_fix.py`
"""
import os
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_cooldown_poller_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_cooldown_poller_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_cooldown_poller_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_cooldown_poller_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_cooldown_poller_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_cooldown_poller_dca_state.json")

import asyncio
import io
import sys
import time

import dca2 as bot
from exchange import RestClient, BinanceApiError


class Capture:
    def __enter__(self):
        self._buf = io.StringIO()
        self._real_stdout = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *exc):
        sys.stdout = self._real_stdout

    @property
    def text(self):
        return self._buf.getvalue()


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
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.request_calls = []
        self.closed = False

    def request(self, method, url, params=None, timeout=None):
        self.request_calls.append((method, url, params))
        resp = self._responses.pop(0)
        return FakeRequestContext(resp)

    async def close(self):
        self.closed = True


def make_client_with_session(responses):
    client = RestClient(api_key="k", api_secret="s", base_url="https://example.invalid")
    client.session = FakeSession(responses)
    return client


# ============================================================================
# TEST 1: a startup 418 with a long ban does not exhaust `attempts` or exit.
# ============================================================================
async def test_startup_418_does_not_exhaust_attempts_or_exit():
    print("\n=== test_startup_418_does_not_exhaust_attempts_or_exit ===")
    # First call: 418 with a SHORT Retry-After (so the test doesn't wait
    # long) but a "long ban" in spirit - the point under test is that the
    # wait doesn't consume the attempt budget, not the absolute duration.
    resp_418 = FakeResponse(418, body={"code": -1003, "msg": "banned"}, headers={"Retry-After": "0.05"})
    resp_ok = FakeResponse(200, body={"serverTime": 123456789})
    client = make_client_with_session([resp_418, resp_ok])

    with Capture() as cap:
        # attempts=1: if the 418/cooldown wait consumed the attempt budget,
        # this would raise SystemExit immediately after the first failure -
        # succeeding here PROVES the cooldown wait does not count against it.
        result = await bot.retry_with_backoff(
            client._request, "GET", "/fapi/v1/time", attempts=1, base_delay=0.01, label="test time sync",
        )
    print(f"TEST 1: result={result}, request_calls={len(client.session.request_calls)}")
    assert result == {"serverTime": 123456789}, "must eventually succeed after waiting out the cooldown"
    assert len(client.session.request_calls) == 2, "exactly the 418 call + the post-cooldown retry, nothing more"
    assert "does NOT count against" in cap.text, "must log that the cooldown wait doesn't consume the attempt budget"
    assert "SystemExit" not in cap.text
    print("TEST 1: PASS - a 418 with attempts=1 still succeeds after cooldown, no SystemExit\n")


async def test_startup_non_cooldown_failure_still_exhausts_and_exits():
    print("=== test_startup_non_cooldown_failure_still_exhausts_and_exits ===")
    # Sanity check: a GENUINE non-418/429 failure must still behave exactly
    # as before - exhausting `attempts` and raising SystemExit. Proves the
    # cooldown-survival fix is scoped to 418/429 only.
    resp_500 = FakeResponse(500, body={"code": -1, "msg": "server error"})
    client = make_client_with_session([resp_500, resp_500])
    try:
        await bot.retry_with_backoff(
            client._request, "GET", "/fapi/v1/time", attempts=2, base_delay=0.01, label="test time sync",
        )
        assert False, "expected SystemExit after exhausting attempts on a genuine failure"
    except SystemExit as e:
        print(f"TEST: genuine failures still exhaust attempts and exit: {e}")
    print("PASS - non-418/429 failures are unaffected by the cooldown-survival fix\n")


# ============================================================================
# TEST 2: repeated poll cycles during cooldown produce zero network calls
# and no repeated error-log flood.
# ============================================================================
async def test_pollers_skip_silently_during_cooldown():
    print("=== test_pollers_skip_silently_during_cooldown ===")

    class CountingClient:
        def __init__(self):
            self.get_balance_calls = 0
            self._cooldown_until_ts = time.time() + 10.0  # well beyond this test's own timeout
            self._cooldown_resume_logged = True

        def is_cooldown_active(self):
            return time.time() < self._cooldown_until_ts

        def cooldown_remaining(self):
            return max(0.0, self._cooldown_until_ts - time.time())

        async def wait_out_cooldown_silently(self, jitter_max=3.0):
            # Mirrors the real RestClient method's behavior/signature -
            # sleeps out the (long) cooldown, which the test's own
            # wait_for() timeout below will interrupt well before it
            # completes, proving no call is ever reached in that window.
            await asyncio.sleep(self.cooldown_remaining())

        async def get_balance(self):
            self.get_balance_calls += 1
            return [{"asset": "USDT", "availableBalance": "100.0"}]

    client = CountingClient()
    manager = object()  # balance_refresher only reads manager.available_balance, never else

    class M:
        available_balance = 0.0

    with Capture() as cap:
        try:
            await asyncio.wait_for(bot.balance_refresher(client, M()), timeout=0.3)
        except asyncio.TimeoutError:
            pass

    print(f"TEST 2: get_balance_calls={client.get_balance_calls} log_output_len={len(cap.text)}")
    assert client.get_balance_calls == 0, "zero network calls may occur while cooldown is active"
    assert "[balance]" not in cap.text, "no per-iteration error/log line while silently skipping"
    print("TEST 2: PASS - balance_refresher makes zero calls and logs nothing while cooldown is active\n")


# ============================================================================
# TEST 3: cooldown expiry permits requests (and pollers) to resume.
# ============================================================================
async def test_pollers_resume_after_cooldown_expires():
    print("=== test_pollers_resume_after_cooldown_expires ===")

    class CountingClient:
        def __init__(self):
            self.get_balance_calls = 0
            self._cooldown_until_ts = 0.0  # no cooldown active
            self._cooldown_resume_logged = True

        def is_cooldown_active(self):
            return time.time() < self._cooldown_until_ts

        def cooldown_remaining(self):
            return max(0.0, self._cooldown_until_ts - time.time())

        async def wait_out_cooldown_silently(self, jitter_max=3.0):
            await asyncio.sleep(self.cooldown_remaining())

        async def get_balance(self):
            self.get_balance_calls += 1
            return [{"asset": "USDT", "availableBalance": "100.0"}]

    client = CountingClient()

    class M:
        available_balance = 0.0

    m = M()
    try:
        await asyncio.wait_for(bot.balance_refresher(client, m), timeout=0.2)
    except asyncio.TimeoutError:
        pass

    print(f"TEST 3: get_balance_calls={client.get_balance_calls} available_balance={m.available_balance}")
    assert client.get_balance_calls >= 1, "a request must go through once no cooldown is active"
    assert m.available_balance == 50.0, "balance_refresher caps available_balance at 50.0 - unaffected by this fix"
    print("TEST 3: PASS - normal polling resumes once cooldown is inactive\n")


# ============================================================================
# TEST 4: thundering-herd prevention - wait_out_cooldown_silently applies
# jitter, so simultaneous callers don't all resume on the exact same tick.
# ============================================================================
async def test_wait_out_cooldown_applies_jitter():
    print("=== test_wait_out_cooldown_applies_jitter ===")
    client = make_client_with_session([])
    client._cooldown_until_ts = time.time() + 0.05
    client._cooldown_resume_logged = True

    async def timed_wait():
        start = time.time()
        await client.wait_out_cooldown_silently(jitter_max=0.2)
        return time.time() - start

    elapsed = await asyncio.gather(*[timed_wait() for _ in range(5)])
    print(f"TEST 4: elapsed times={[f'{e:.3f}' for e in elapsed]}")
    # All must wait at least the base cooldown remaining, and NOT all be
    # identical (jitter must vary) - a fixed >=0.001s spread across 5
    # concurrent callers confirms randomization is actually applied.
    assert all(e >= 0.05 for e in elapsed), "every caller must wait at least the cooldown remaining"
    assert len(set(round(e, 3) for e in elapsed)) > 1, "jitter must vary resume timing across concurrent callers"
    print("TEST 4: PASS - jitter varies resume timing, preventing a thundering herd\n")


# ============================================================================
# TEST 5-6 (userdata_consumer coverage): the user-data-stream reconnect
# loop must also respect the shared cooldown before requesting/retrying a
# listen key - zero network calls and no per-cycle error-log flood while
# active, exactly one request resumes after expiry.
# ============================================================================
class CountingListenKeyClient:
    def __init__(self, cooldown_active: bool):
        self.create_listen_key_calls = 0
        self._cooldown_until_ts = (time.time() + 10.0) if cooldown_active else 0.0
        self._cooldown_resume_logged = True
        self.called_event = asyncio.Event()

    def is_cooldown_active(self) -> bool:
        return time.time() < self._cooldown_until_ts

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until_ts - time.time())

    async def wait_out_cooldown_silently(self, jitter_max: float = 3.0) -> None:
        await asyncio.sleep(self.cooldown_remaining())

    async def create_listen_key(self):
        self.create_listen_key_calls += 1
        self.called_event.set()
        # Stops the loop right here, before it would ever reach a real
        # websockets.connect() call - proving create_listen_key() was (or
        # wasn't) reached is exactly what this test needs; the actual
        # websocket connection is unrelated to the cooldown gate under test.
        raise RuntimeError("simulated failure - stop before a real websocket connect")


async def test_userdata_consumer_skips_listen_key_during_cooldown():
    print("=== test_userdata_consumer_skips_listen_key_during_cooldown ===")
    import websocket as ws_module

    client = CountingListenKeyClient(cooldown_active=True)
    manager = object()  # never reached - create_listen_key() raises first

    with Capture() as cap:
        try:
            await asyncio.wait_for(ws_module.userdata_consumer(client, manager), timeout=0.3)
        except asyncio.TimeoutError:
            pass

    print(f"TEST 5: create_listen_key_calls={client.create_listen_key_calls} "
          f"disconnected_log_present={'[user-ws] disconnected' in cap.text}")
    assert client.create_listen_key_calls == 0, "zero listen-key requests may occur while cooldown is active"
    assert "[user-ws] disconnected" not in cap.text, "no per-cycle error-log flood while silently skipping"
    print("TEST 5: PASS - userdata_consumer makes zero listen-key requests and logs nothing during cooldown\n")


async def test_userdata_consumer_resumes_exactly_once_after_expiry():
    print("=== test_userdata_consumer_resumes_exactly_once_after_expiry ===")
    import websocket as ws_module

    client = CountingListenKeyClient(cooldown_active=False)
    manager = object()
    task = asyncio.create_task(ws_module.userdata_consumer(client, manager))
    try:
        await asyncio.wait_for(client.called_event.wait(), timeout=1.0)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, RuntimeError):
            pass

    print(f"TEST 6: create_listen_key_calls={client.create_listen_key_calls}")
    assert client.create_listen_key_calls == 1, "exactly one normal request must resume after cooldown expiry"
    print("TEST 6: PASS - userdata_consumer resumes normally, exactly once, once cooldown is inactive\n")


async def main():
    await test_startup_418_does_not_exhaust_attempts_or_exit()
    await test_startup_non_cooldown_failure_still_exhausts_and_exits()
    await test_pollers_skip_silently_during_cooldown()
    await test_pollers_resume_after_cooldown_expires()
    await test_wait_out_cooldown_applies_jitter()
    await test_userdata_consumer_skips_listen_key_during_cooldown()
    await test_userdata_consumer_resumes_exactly_once_after_expiry()
    print("ALL COOLDOWN-SURVIVAL + POLLER-SKIP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
