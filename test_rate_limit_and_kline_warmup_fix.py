"""Offline tests for the 2026-08 Live incident fix pair:

  (1) HTTP 429 REST rate-limit spamming
      -----------------------------------------------------------------
      Live log:
        [risk] position risk poll failed: HTTP 429: {'code': -1003, 'msg':
        'Too many requests; current limit of IP(...) is 2400 requests per
        minute. Please use the websocket for live updates to avoid polling
        the API.'}
        [rest-cooldown] ... suppressing ALL REST requests on this client
        until ... (28s).

      position_risk_poller fired a signed GET /fapi/v2/positionRisk every
      10s unconditionally, open or flat, and every poller slept on a fixed
      interval so they resynchronized into bursts. Once Binance answered
      429, the shared cooldown suppressed ALL REST traffic for ~30s at a
      time, repeatedly.

      The fix: every poll interval is now read from the environment and
      CLAMPED into a rate-limit-safe 15-30s band; positionRisk runs at the
      active cadence only when something is actually at risk and idles at
      the slow end while flat; the balance refresh is skipped outright
      while the user-data websocket's ACCOUNT_UPDATE copy is fresh; and
      every poller sleep is jittered so they never line up.

  (2) Instant warm-up via historical klines
      -----------------------------------------------------------------
      Live log:
        [entry-skip] startup warm-up: insufficient market history
        (candles=5/57, atr_pct=0.000000) - indicators not yet valid, no
        entries opened

      The candle series backing ATR / EMA / regime was built exclusively
      from the live tick stream, so a fresh container needed
      max(EMA_SLOW, ATR_PERIOD) + 2 = 57 one-minute candles - nearly an
      hour - before it could consider a single entry.

      The fix: ONE REST call to GET /fapi/v1/klines at initialization seeds
      the buffer with the last 100 closed 1m candles, then the live
      websocket stream continues the series with no loss of real-time
      precision (only fully-CLOSED candles are seeded; the in-progress
      bucket always belongs to the live stream).

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_rate_limit_and_kline_warmup_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_ratelimit_warmup_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_ratelimit_warmup_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_ratelimit_warmup_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_ratelimit_warmup_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_ratelimit_warmup_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_ratelimit_warmup_dca_state.json")

import asyncio
import io
import math
import subprocess
import sys
import time

import config
import dca2 as bot
import trading
from exchange import RestClient


class Capture:
    def __enter__(self):
        self._buf = io.StringIO()
        self._stdout = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *exc):
        sys.stdout = self._stdout

    @property
    def text(self):
        return self._buf.getvalue()


def make_manager():
    filters = bot.SymbolFilters(
        tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0,
    )
    return bot.MartingaleManager(client=None, symbol="SOLUSDT", filters=filters, leverage=20)


def synthetic_klines(count=100, interval_sec=60, end_ts=None, base=77.0):
    """Binance-shaped kline rows, oldest first, ending with the candle that
    is CURRENTLY forming (exactly like a real GET /fapi/v1/klines reply)."""
    end_ts = end_ts if end_ts is not None else time.time()
    current_bucket = math.floor(end_ts / interval_sec) * interval_sec
    rows = []
    for i in range(count - 1, -1, -1):
        open_time = current_bucket - i * interval_sec
        # A gentle wave so ATR/EMA get real, non-degenerate values.
        drift = math.sin(open_time / (interval_sec * 7.0)) * 0.6
        o = base + drift
        c = o + 0.05
        rows.append([
            int(open_time * 1000),          # [0]  openTime ms
            f"{o:.4f}",                     # [1]  open
            f"{max(o, c) + 0.10:.4f}",      # [2]  high
            f"{min(o, c) - 0.10:.4f}",      # [3]  low
            f"{c:.4f}",                     # [4]  close
            "1000.0",                       # [5]  volume
            int((open_time + interval_sec) * 1000) - 1,  # [6] closeTime ms
            "77000.0",                      # [7]  quote volume
            500,                            # [8]  trades
            "600.0",                        # [9]  taker buy base volume
            "46200.0",                      # [10] taker buy quote volume
            "0",                            # [11] ignore
        ])
    return rows


# ============================================================================
# PART 1 - HTTP 429 REST RATE LIMIT
# ============================================================================

# ----------------------------------------------------------------------------
# TEST 1: every poll interval sits inside the 15-30s rate-limit-safe band.
# ----------------------------------------------------------------------------
def test_poll_intervals_are_in_the_safe_band():
    print("=== test_poll_intervals_are_in_the_safe_band ===")

    print(
        f"TEST 1: POSITION_RISK_POLL_SEC={config.POSITION_RISK_POLL_SEC} "
        f"POSITION_RISK_POLL_IDLE_SEC={config.POSITION_RISK_POLL_IDLE_SEC} "
        f"BALANCE_REFRESH_SEC={config.BALANCE_REFRESH_SEC}"
    )
    assert config.REST_POLL_MIN_SEC == 15.0 and config.REST_POLL_MAX_SEC == 30.0
    assert 15.0 <= config.POSITION_RISK_POLL_SEC <= 30.0, (
        "the active positionRisk poll must live in the requested 15-30s band "
        "(it was a fixed 10s when Binance handed out the 429)"
    )
    assert 15.0 <= config.POSITION_RISK_POLL_IDLE_SEC <= 30.0
    assert config.POSITION_RISK_POLL_IDLE_SEC >= config.POSITION_RISK_POLL_SEC, (
        "the flat/idle cadence must never poll MORE often than the active one"
    )
    assert config.BALANCE_REFRESH_SEC >= config.REST_POLL_MIN_SEC
    print("TEST 1: PASS - default poll cadences are all inside the safe band\n")


# ----------------------------------------------------------------------------
# TEST 2: an out-of-band environment override is clamped, not obeyed - no
# deployment can configure the bot back into a rate-limit ban.
# ----------------------------------------------------------------------------
def test_out_of_band_env_override_is_clamped():
    print("=== test_out_of_band_env_override_is_clamped ===")

    probe = (
        "import os, config;"
        "print(config.POSITION_RISK_POLL_SEC, config.POSITION_RISK_POLL_IDLE_SEC)"
    )
    env = dict(os.environ)
    env["POSITION_RISK_POLL_SEC"] = "2"       # the old, ban-inducing kind of value
    env["POSITION_RISK_POLL_IDLE_SEC"] = "600"
    out = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1]
    active, idle = (float(x) for x in out.split())

    print(f"TEST 2: POSITION_RISK_POLL_SEC=2 -> {active};  IDLE=600 -> {idle}")
    assert active == 15.0, "a 2s override must be clamped up to the 15s floor"
    assert idle == 30.0, "a 600s override must be clamped down to the 30s ceiling"
    print("TEST 2: PASS - out-of-band overrides are clamped into [15s, 30s]\n")


# ----------------------------------------------------------------------------
# TEST 3: poller sleeps are jittered, so independent pollers never
# resynchronize into one burst against the shared IP rate limit.
# ----------------------------------------------------------------------------
def test_poll_sleeps_are_jittered():
    print("=== test_poll_sleeps_are_jittered ===")

    samples = [config.jittered_interval(20.0) for _ in range(200)]
    lo, hi = min(samples), max(samples)
    print(f"TEST 3: 200 samples of jittered_interval(20.0) -> [{lo:.3f}, {hi:.3f}]")
    assert len(set(samples)) > 100, "the interval must actually vary between calls"
    assert lo >= 20.0 * (1 - config.REST_POLL_JITTER_PCT) - 1e-9
    assert hi <= 20.0 * (1 + config.REST_POLL_JITTER_PCT) + 1e-9
    assert config.jittered_interval(0.1) >= 1.0, "never sleeps below the 1s floor"
    print("TEST 3: PASS - every poller sleep is jittered within +/- the configured band\n")


# ----------------------------------------------------------------------------
# TEST 4: positionRisk idles while flat and goes active the moment anything
# is at risk - the actual reduction in REST pressure.
# ----------------------------------------------------------------------------
def test_position_risk_cadence_is_adaptive():
    print("=== test_position_risk_cadence_is_adaptive ===")

    m = make_manager()
    flat_interval = bot._position_risk_interval(m)

    m.position.status = "OPEN"
    m.position.side = "LONG"
    m.position.total_qty = 1.0
    open_interval = bot._position_risk_interval(m)

    print(f"TEST 4: flat={flat_interval}s open={open_interval}s")
    assert flat_interval == config.POSITION_RISK_POLL_IDLE_SEC
    assert open_interval == config.POSITION_RISK_POLL_SEC
    assert open_interval <= flat_interval

    # An hour of flat idling at the new cadence versus the old fixed 10s.
    old_calls_per_hour = 3600 / 10
    new_calls_per_hour = 3600 / flat_interval
    print(
        f"TEST 4: positionRisk calls/hour while flat: {old_calls_per_hour:.0f} (old) "
        f"-> {new_calls_per_hour:.0f} (new)"
    )
    assert new_calls_per_hour <= old_calls_per_hour / 3, (
        "the flat cadence must cut REST pressure by at least 3x - this is the "
        "state the bot was in for the entire 429-throttled Live log"
    )
    print("TEST 4: PASS - cadence adapts to real risk and cuts idle REST load\n")


# ----------------------------------------------------------------------------
# TEST 5: a websocket position hint can only ever ESCALATE the cadence, never
# suppress it - it must be impossible for the WS-first optimization to make
# the bot poll less often while a real position is open.
# ----------------------------------------------------------------------------
def test_ws_hint_can_only_escalate_the_cadence():
    print("=== test_ws_hint_can_only_escalate_the_cadence ===")

    m = make_manager()
    assert m.has_ws_position_hint(90.0) is None, "no stream data yet == unknown"

    # Stream says a position exists while local state still believes it is
    # flat -> must poll at the ACTIVE cadence.
    m.on_account_update({"a": {"P": [{"s": "SOLUSDT", "pa": "1.5"}]}})
    assert m.has_ws_position_hint(90.0) is True
    assert bot._position_risk_interval(m) == config.POSITION_RISK_POLL_SEC

    # Stream says flat AND local state is flat -> idle cadence.
    m.on_account_update({"a": {"P": [{"s": "SOLUSDT", "pa": "0"}]}})
    assert m.has_ws_position_hint(90.0) is False
    assert bot._position_risk_interval(m) == config.POSITION_RISK_POLL_IDLE_SEC

    # Stream says flat but local state says OPEN -> local state still wins
    # and keeps the ACTIVE cadence (the hint never suppresses).
    m.position.status = "OPEN"
    m.position.total_qty = 1.0
    assert bot._position_risk_interval(m) == config.POSITION_RISK_POLL_SEC

    # A stale hint is discarded entirely rather than trusted.
    m.ws_position_ts = time.time() - 10_000
    assert m.has_ws_position_hint(90.0) is None
    print("TEST 5: PASS - the websocket hint escalates but never suppresses polling\n")


# ----------------------------------------------------------------------------
# TEST 6: ACCOUNT_UPDATE feeds balance + position state off the websocket,
# which is exactly what Binance's own 429 message asks callers to do.
# ----------------------------------------------------------------------------
def test_account_update_feeds_state_from_the_websocket():
    print("=== test_account_update_feeds_state_from_the_websocket ===")

    m = make_manager()
    before = m.last_account_update_ts
    m.on_account_update({
        "e": "ACCOUNT_UPDATE",
        "a": {
            "B": [{"a": "BNB", "wb": "1.0", "cw": "1.0"}, {"a": "USDT", "wb": "25.5", "cw": "20.37"}],
            "P": [{"s": "ETHUSDT", "pa": "9.0"}, {"s": "SOLUSDT", "pa": "-2.63"}],
        },
    })

    print(
        f"TEST 6: available_balance={m.available_balance} ws_position_amt={m.ws_position_amt} "
        f"ts_advanced={m.last_account_update_ts > before}"
    )
    assert m.available_balance == 20.37, "cross-wallet balance ('cw') wins, as before"
    assert m.ws_position_amt == -2.63, "only THIS symbol's position amount is recorded"
    assert m.last_account_update_ts > before

    # A malformed frame must never raise - one bad message cannot be allowed
    # to take down the user-data socket.
    m.on_account_update({})
    m.on_account_update({"a": {"B": [{"a": "USDT", "cw": "not-a-number"}], "P": "garbage"}})
    assert m.available_balance == 20.37, "a malformed frame leaves the last good value alone"
    print("TEST 6: PASS - ACCOUNT_UPDATE records balance/position state with a timestamp\n")


# ----------------------------------------------------------------------------
# TEST 7: balance_refresher spends ZERO rate limit while the websocket copy
# of the balance is fresh, and resumes polling once it goes stale.
# ----------------------------------------------------------------------------
async def test_balance_refresh_defers_to_the_websocket():
    print("=== test_balance_refresh_defers_to_the_websocket ===")

    class CountingClient:
        def __init__(self):
            self.get_balance_calls = 0

        def is_cooldown_active(self):
            return False

        def cooldown_remaining(self):
            return 0.0

        async def wait_out_cooldown_silently(self, jitter_max=3.0):
            return

        async def get_balance(self):
            self.get_balance_calls += 1
            return [{"asset": "USDT", "availableBalance": "100.0"}]

    class FreshWs:
        available_balance = 20.37
        last_account_update_ts = time.time()   # the socket just told us

    client = CountingClient()
    try:
        await asyncio.wait_for(bot.balance_refresher(client, FreshWs()), timeout=0.25)
    except asyncio.TimeoutError:
        pass
    print(f"TEST 7: fresh websocket balance -> get_balance_calls={client.get_balance_calls}")
    assert client.get_balance_calls == 0, (
        "no REST call may be spent re-learning a balance the websocket already delivered"
    )

    class StaleWs:
        available_balance = 0.0
        last_account_update_ts = time.time() - (config.BALANCE_WS_FRESH_SEC + 60)

    client2 = CountingClient()
    stale = StaleWs()
    try:
        await asyncio.wait_for(bot.balance_refresher(client2, stale), timeout=0.25)
    except asyncio.TimeoutError:
        pass
    print(f"TEST 7: stale websocket balance -> get_balance_calls={client2.get_balance_calls}")
    assert client2.get_balance_calls >= 1, "REST must resume once the websocket copy goes stale"
    assert stale.available_balance == 50.0, "the existing 50.0 cap is untouched by this fix"
    print("TEST 7: PASS - the balance poll is websocket-first and REST is the fallback\n")


# ============================================================================
# PART 2 - INSTANT WARM-UP VIA HISTORICAL KLINES
# ============================================================================

# ----------------------------------------------------------------------------
# TEST 8: get_klines issues exactly ONE unsigned GET to /fapi/v1/klines and
# clamps limit to Binance's documented 1500 maximum.
# ----------------------------------------------------------------------------
async def test_get_klines_issues_one_unsigned_request():
    print("=== test_get_klines_issues_one_unsigned_request ===")

    calls = []

    client = RestClient("k", "s", "https://example.invalid")

    async def fake_request(method, path, params=None, signed=False):
        calls.append((method, path, dict(params or {}), signed))
        return []

    client._request = fake_request

    await client.get_klines("SOLUSDT", interval="1m", limit=100)
    await client.get_klines("SOLUSDT", interval="1m", limit=99999)

    print(f"TEST 8: calls={calls}")
    assert len(calls) == 2, "one REST call per invocation - this is not a poller"
    method, path, params, signed = calls[0]
    assert (method, path, signed) == ("GET", "/fapi/v1/klines", False)
    assert params == {"symbol": "SOLUSDT", "interval": "1m", "limit": 100}
    assert calls[1][2]["limit"] == 1500, "limit is clamped to Binance's maximum"
    print("TEST 8: PASS - a single unsigned klines request with the right params\n")


# ----------------------------------------------------------------------------
# TEST 9: prime_from_klines seeds closed candles, drops the in-progress one,
# and reconstructs buy/sell volume from taker-buy base volume.
# ----------------------------------------------------------------------------
def test_prime_from_klines_seeds_only_closed_candles():
    print("=== test_prime_from_klines_seeds_only_closed_candles ===")

    now = time.time()
    agg = trading.CandleAggregator(interval_sec=60, max_history=180)
    rows = synthetic_klines(count=100, interval_sec=60, end_ts=now)
    seeded = agg.prime_from_klines(rows, now_ts=now)

    closed = agg.closed_candles()
    current_bucket = math.floor(now / 60) * 60
    print(f"TEST 9: rows=100 seeded={seeded} closed_candles={len(closed)}")
    assert seeded == 99, "the still-forming final kline row is dropped, the other 99 are seeded"
    assert len(closed) == 99
    assert all(c.open_time < current_bucket for c in closed), (
        "no seeded candle may occupy the in-progress bucket - that one belongs "
        "to the live websocket stream"
    )
    assert closed == sorted(closed, key=lambda c: c.open_time), "oldest first"
    assert agg._current is None, "priming never fabricates a live bucket"

    last = closed[-1]
    assert abs(last.buy_volume - 600.0) < 1e-6
    assert abs(last.sell_volume - 400.0) < 1e-6, "sell = volume - takerBuyBase"
    assert abs(last.volume - 1000.0) < 1e-6

    # Malformed rows are skipped individually, never aborting the warm-up.
    agg2 = trading.CandleAggregator(interval_sec=60, max_history=180)
    dirty = synthetic_klines(count=10, interval_sec=60, end_ts=now)
    dirty.insert(3, ["nonsense"])
    dirty.insert(5, None)
    assert agg2.prime_from_klines(dirty, now_ts=now) == 9
    assert agg2.prime_from_klines([], now_ts=now) == 0
    print("TEST 9: PASS - only fully-closed candles are seeded, bad rows are skipped\n")


# ----------------------------------------------------------------------------
# TEST 10: live-observed candles always beat a seeded historical one, so
# priming can never overwrite real streamed data - real-time precision is
# preserved, which is the whole point of handing over to the websocket.
# ----------------------------------------------------------------------------
def test_live_candles_win_over_seeded_history():
    print("=== test_live_candles_win_over_seeded_history ===")

    now = time.time()
    agg = trading.CandleAggregator(interval_sec=60, max_history=180)

    # The live stream has already produced two candles and is mid-way through
    # a third when the warm-up seed lands.
    agg.on_price(100.0, ts=now - 150)
    agg.on_price(101.0, ts=now - 90)
    agg.on_price(102.0, ts=now - 30)
    live_buckets = {c.open_time for c in agg.closed_candles()}
    live_current = agg._current
    assert len(live_buckets) == 2 and live_current is not None

    agg.prime_from_klines(synthetic_klines(count=100, interval_sec=60, end_ts=now), now_ts=now)

    by_bucket = {c.open_time: c for c in agg.closed_candles()}
    print(f"TEST 10: buffer={len(by_bucket)} live_buckets_preserved={live_buckets <= set(by_bucket)}")
    assert live_buckets <= set(by_bucket)
    for bucket in live_buckets:
        assert by_bucket[bucket].close in (100.0, 101.0), (
            "a live-observed candle must never be replaced by the REST snapshot"
        )
    assert agg._current is live_current, "the in-progress live bucket is left completely alone"
    assert len(by_bucket) >= 57, "history still reaches back past the indicator minimum"
    print("TEST 10: PASS - streamed candles survive the seed, the live bucket is untouched\n")


# ----------------------------------------------------------------------------
# TEST 11: the end-to-end warm-up - one REST call clears the exact gate that
# printed "insufficient market history (candles=5/57, atr_pct=0.000000)".
# ----------------------------------------------------------------------------
async def test_warm_up_clears_the_startup_gate_in_one_call():
    print("=== test_warm_up_clears_the_startup_gate_in_one_call ===")

    needed = max(config.EMA_SLOW, config.ATR_PERIOD) + 2
    m = make_manager()

    # Before: exactly the Live failure state.
    assert len(m.candles.all_candles_incl_live()) < needed
    assert m.last_regime.atr_pct == 0.0

    class KlineClient:
        def __init__(self):
            self.kline_calls = 0

        def is_cooldown_active(self):
            return False

        async def get_klines(self, symbol, interval="1m", limit=100):
            self.kline_calls += 1
            assert symbol == "SOLUSDT" and interval == "1m"
            return synthetic_klines(count=limit, interval_sec=60)

    client = KlineClient()
    with Capture() as cap:
        seeded = await trading.warm_up_candles_from_klines(client, m)

    candles = m.candles.all_candles_incl_live()
    print(
        f"TEST 11: kline_calls={client.kline_calls} seeded={seeded} "
        f"candles={len(candles)}/{needed} atr%={m.last_regime.atr_pct * 100:.4f}"
    )
    assert client.kline_calls == 1, "the whole warm-up costs exactly one REST call"
    assert seeded >= needed
    assert len(candles) >= needed, "the 57-candle indicator minimum is met immediately"
    assert m.last_regime.atr_pct > 0.0, (
        "ATR must be a real reading, not the default 0.0 that also blocked entries"
    )
    assert "[warmup]" in cap.text and "Live websocket updates take over" in cap.text

    # And the gate the Live log kept tripping is now satisfied.
    assert not (len(candles) < needed or m.last_regime.atr_pct <= 0.0), (
        "the startup warm-up entry gate must be clear straight after initialization"
    )
    print("TEST 11: PASS - indicators are warm within one REST call instead of ~an hour\n")


# ----------------------------------------------------------------------------
# TEST 12: the warm-up is strictly best-effort - a failure, an active REST
# cooldown, or a disabled flag degrades to the old stream-only behavior and
# never blocks startup.
# ----------------------------------------------------------------------------
async def test_warm_up_never_blocks_startup():
    print("=== test_warm_up_never_blocks_startup ===")

    class BoomClient:
        def is_cooldown_active(self):
            return False

        async def get_klines(self, symbol, interval="1m", limit=100):
            raise RuntimeError("HTTP 429: {'code': -1003, 'msg': 'Too many requests'}")

    m = make_manager()
    with Capture() as cap:
        assert await trading.warm_up_candles_from_klines(BoomClient(), m) == 0
    assert "falling back to" in cap.text
    print("TEST 12: fetch failure -> warm-up returns 0 and startup continues")

    class CooldownClient:
        def __init__(self):
            self.kline_calls = 0

        def is_cooldown_active(self):
            return True

        async def get_klines(self, symbol, interval="1m", limit=100):
            self.kline_calls += 1
            return synthetic_klines(count=limit)

    cd = CooldownClient()
    with Capture() as cap:
        assert await trading.warm_up_candles_from_klines(cd, make_manager()) == 0
    assert cd.kline_calls == 0, (
        "an active 418/429 cooldown must never be deepened by the warm-up call"
    )
    assert "REST cooldown active" in cap.text

    class EmptyClient:
        def is_cooldown_active(self):
            return False

        async def get_klines(self, symbol, interval="1m", limit=100):
            return []

    with Capture() as cap:
        assert await trading.warm_up_candles_from_klines(EmptyClient(), make_manager()) == 0
    assert "no usable candles" in cap.text
    print("TEST 12: PASS - every warm-up failure mode degrades to stream-only warm-up\n")


# ----------------------------------------------------------------------------
# TEST 13: main() actually calls the warm-up, before the websocket consumers
# start - a fix nothing invokes is not a fix.
# ----------------------------------------------------------------------------
def test_main_wires_the_warm_up_before_the_stream():
    print("=== test_main_wires_the_warm_up_before_the_stream ===")

    source = open("dca2.py", encoding="utf-8").read()
    warmup_at = source.index("await warm_up_candles_from_klines(client, manager)")
    consumers_at = source.index("market_data_consumer(manager),")
    sync_at = source.index('await initialize_sync(client, manager, context="startup")')

    print(f"TEST 13: warm_up@{warmup_at} initialize_sync@{sync_at} consumers@{consumers_at}")
    assert warmup_at < sync_at < consumers_at, (
        "the kline seed must run at initialization, before the websocket "
        "consumers take over the series"
    )
    print("TEST 13: PASS - main() seeds candles at startup, then hands over to the stream\n")


async def main():
    test_poll_intervals_are_in_the_safe_band()
    test_out_of_band_env_override_is_clamped()
    test_poll_sleeps_are_jittered()
    test_position_risk_cadence_is_adaptive()
    test_ws_hint_can_only_escalate_the_cadence()
    test_account_update_feeds_state_from_the_websocket()
    await test_balance_refresh_defers_to_the_websocket()
    await test_get_klines_issues_one_unsigned_request()
    test_prime_from_klines_seeds_only_closed_candles()
    test_live_candles_win_over_seeded_history()
    await test_warm_up_clears_the_startup_gate_in_one_call()
    await test_warm_up_never_blocks_startup()
    test_main_wires_the_warm_up_before_the_stream()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
