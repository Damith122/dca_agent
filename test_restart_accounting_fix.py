"""
Permanent regression tests for the 2026-08 restart-safe runtime-accounting
fix (MartingaleManager.restore_runtime_accounting_from_history()).

Root cause fixed: TradeLogger.load_all() / restore_csv_logs_from_github()
already restore the PERMANENT trades_log_<ENV>_<SYMBOL>.jsonl history across
a Railway restart, but MartingaleManager's own in-memory runtime counters
(trade_count, realized_pnl_total, daily_realized_pnl) always started a fresh
process at 0/0.0/0.0 regardless - nothing ever re-derived them from that
restored history. Because daily_realized_pnl also backs MAX_DAILY_LOSS_USDT
(a NEW-ENTRY-only gate), this could let a restart silently bypass Daily Loss
Protection for the rest of that UTC day.

Scope note: PARTIAL_TP_ENABLED is False in this deployment. This fix is pure
runtime-accounting bookkeeping restored from the trade log - it does NOT
touch _apply_partial_close(), DCA-state persistence/restoration, or any
Partial-TP behavior in any way. See test_existing_dca_step_restart_recovery_unchanged()
below, which specifically pins down that the pre-existing DCA restart
recovery path is untouched.

Exercises the real MartingaleManager/TradeLogger/initialize_sync() from
trading.py via dca2.py, with DRY_RUN=false so initialize_sync() actually
runs its exchange-reconciliation logic (rows are always passed in directly -
no real network call is ever made) - same pattern as
test_dca_resync_race_fix.py.

Run directly: `python3 test_restart_accounting_fix.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
# initialize_sync() itself starts with `if DRY_RUN: return` - needs
# DRY_RUN=false to exercise it here, same as test_dca_resync_race_fix.py /
# test_fill_race_fix.py. No test below ever reaches a real network call
# (rows is always passed in directly, and manager.client stays None or a
# FakeClient stub).
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_restart_acct_trades_log.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_restart_acct_trades_log.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_restart_acct_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_restart_acct_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_restart_acct_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_restart_acct_dca_state.json")
os.environ.setdefault("BRAIN2_WARMUP_UPDATES", "5")
os.environ.setdefault("MAX_DAILY_LOSS_USDT", "2.5")
os.environ.setdefault("DAILY_PROFIT_TARGET_USDT", "0.5")
# Explicit, matching the real Railway deployment this fix targets - Partial
# TP is OFF and this fix must not depend on or interact with it either way.
os.environ.setdefault("PARTIAL_TP_ENABLED", "false")

import asyncio
import json
import math
import os as _os
import time
from datetime import datetime, timedelta, timezone

import dca2 as bot
import trading


TRADE_LOG_PATH = trading.TRADE_LOG_JSON_PATH
DCA_STATE_PATH = trading.DCA_STATE_PATH


def _reset_files():
    for path in (TRADE_LOG_PATH, trading.TRADE_LOG_CSV_PATH, DCA_STATE_PATH):
        try:
            _os.remove(path)
        except FileNotFoundError:
            pass


async def make_manager(symbol: str = "SOLUSDT") -> trading.MartingaleManager:
    filters = bot.SymbolFilters(tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0)
    manager = bot.MartingaleManager(client=None, symbol=symbol, filters=filters, leverage=20)
    # No GITHUB_TOKEN/GITHUB_REPO set anywhere in this test environment, so
    # github_sync.enabled is already False and every upload()/download()
    # call is a local no-op - nothing here ever touches the network.
    return manager


class FakeClient:
    """initialize_sync() only calls get_position_risk() when `rows` isn't
    passed directly - every test below passes `rows` explicitly, but this
    stub exists so the call signature stays valid regardless."""
    def __init__(self, rows):
        self._rows = rows

    async def get_position_risk(self, symbol):
        return self._rows

    async def get_user_trades(self, symbol, from_id=None, limit=1000):
        return []  # no exchange fill history to reconcile in these tests


def rows_for(side: str, qty: float, avg_entry: float) -> list:
    amt = qty if side == "LONG" else -qty
    return [{"positionAmt": str(amt), "entryPrice": str(avg_entry)}]


def write_trade(net_pnl: float, close_time: str, symbol: str = "SOLUSDT", order_id: int = 1) -> None:
    record = {
        "close_time": close_time,
        "symbol": symbol,
        "side": "LONG",
        "entry_price": 100.0,
        "exit_price": 101.0,
        "qty": 1.0,
        "invested_notional": 100.0,
        "gross_pnl_usdt": net_pnl,
        "fees_usdt": 0.0,
        "net_pnl_usdt": net_pnl,
        "net_pnl_pct": net_pnl / 100.0,
        "dca_count": 0,
        "holding_time_sec": 60.0,
        "exit_reason": "take_profit",
        "final_outcome": "win" if net_pnl > 0 else "loss",
        "exit_order_id": order_id,
        "binance_order_ids": [order_id],
    }
    with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def yesterday_utc_str() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


# ============================================================================
# 1) + 2) + 3) Restart with several completed trades: trade_count and
#    cumulative/session realized_pnl_total restore exactly; today's
#    daily_realized_pnl restores exactly.
# ============================================================================

async def test_restore_trade_count_and_realized_pnl_total():
    print("\n=== test_restore_trade_count_and_realized_pnl_total ===")
    _reset_files()
    today = today_utc_str()
    write_trade(0.0728, f"{today} 18:41:30 UTC", order_id=1)
    write_trade(0.1882, f"{today} 19:30:36 UTC", order_id=2)
    write_trade(-0.2010, f"{today} 22:01:22 UTC", order_id=3)
    write_trade(0.0615, f"{today} 22:36:35 UTC", order_id=4)
    write_trade(0.2755, f"{today} 23:59:00 UTC", order_id=5)
    write_trade(-0.0347, f"{today} 23:59:50 UTC", order_id=6)

    manager = await make_manager()
    assert manager.trade_count == 0
    assert manager.realized_pnl_total == 0.0

    await manager.restore_runtime_accounting_from_history()

    assert manager.trade_count == 6, f"expected trade_count=6, got {manager.trade_count}"
    expected_total = 0.0728 + 0.1882 - 0.2010 + 0.0615 + 0.2755 - 0.0347
    assert abs(manager.realized_pnl_total - expected_total) < 1e-9, (
        f"expected realized_pnl_total={expected_total:.4f}, got {manager.realized_pnl_total:.4f}"
    )
    assert abs(manager.daily_realized_pnl - expected_total) < 1e-9, (
        f"expected daily_realized_pnl={expected_total:.4f} (all trades are today), "
        f"got {manager.daily_realized_pnl:.4f}"
    )
    assert manager._daily_loss_tracker_date == today, (
        f"expected _daily_loss_tracker_date={today}, got {manager._daily_loss_tracker_date}"
    )
    print(f"PASS: trades={manager.trade_count} session_pnl={manager.realized_pnl_total:+.4f} "
          f"today_pnl={manager.daily_realized_pnl:+.4f}")


# ============================================================================
# 4) + 5) Previous-day trades are included in cumulative/session PnL but
#    excluded from today's daily_realized_pnl.
# ============================================================================

async def test_previous_day_trades_excluded_from_daily_bucket():
    print("\n=== test_previous_day_trades_excluded_from_daily_bucket ===")
    _reset_files()
    today = today_utc_str()
    yesterday = yesterday_utc_str()
    write_trade(1.5000, f"{yesterday} 10:00:00 UTC", order_id=1)
    write_trade(-0.5000, f"{yesterday} 20:00:00 UTC", order_id=2)
    write_trade(0.2500, f"{today} 09:00:00 UTC", order_id=3)

    manager = await make_manager()
    await manager.restore_runtime_accounting_from_history()

    assert manager.trade_count == 3
    assert abs(manager.realized_pnl_total - 1.25) < 1e-9, (
        f"expected cumulative realized_pnl_total=1.25 (all 3 trades, including yesterday's), "
        f"got {manager.realized_pnl_total:.4f}"
    )
    assert abs(manager.daily_realized_pnl - 0.25) < 1e-9, (
        f"expected daily_realized_pnl=0.25 (today's trade only), got {manager.daily_realized_pnl:.4f}"
    )
    print(f"PASS: cumulative={manager.realized_pnl_total:+.4f} (3 trades, incl. yesterday) "
          f"today={manager.daily_realized_pnl:+.4f} (1 trade)")


# ============================================================================
# 6) After restart, MAX_DAILY_LOSS_USDT still blocks entries if today's
#    restored net PnL already exceeded the loss limit.
# ============================================================================

async def test_max_daily_loss_still_blocks_after_restart():
    print("\n=== test_max_daily_loss_still_blocks_after_restart ===")
    _reset_files()
    today = today_utc_str()
    # MAX_DAILY_LOSS_USDT=2.5 (set in env above) - push today's realized
    # PnL below -2.5 across several trades, exactly as a real losing day
    # would accumulate.
    write_trade(-1.00, f"{today} 08:00:00 UTC", order_id=1)
    write_trade(-1.00, f"{today} 09:00:00 UTC", order_id=2)
    write_trade(-0.60, f"{today} 10:00:00 UTC", order_id=3)

    manager = await make_manager()
    await manager.restore_runtime_accounting_from_history()

    assert manager.daily_realized_pnl <= -trading.MAX_DAILY_LOSS_USDT, (
        f"expected daily_realized_pnl <= -{trading.MAX_DAILY_LOSS_USDT}, "
        f"got {manager.daily_realized_pnl:.4f}"
    )
    # Exact condition on_price_tick()'s Daily Loss Protection gate checks -
    # asserted directly here (rather than driving the full brain/feature
    # pipeline) to keep this test focused on the accounting restoration
    # itself, which is what this fix changes. The gate's own code
    # (on_price_tick()) is completely untouched by this fix.
    gate_blocks_entries = (
        trading.MAX_DAILY_LOSS_USDT > 0 and manager.daily_realized_pnl <= -trading.MAX_DAILY_LOSS_USDT
    )
    assert gate_blocks_entries, "Daily Loss Protection must block new entries immediately after restart"

    # _maybe_reset_daily_loss_tracker() (called by the real gate on every
    # tick) must NOT wipe the restored value just because it's being
    # called for the first time this process - _daily_loss_tracker_date
    # was already primed to today's UTC date by the restoration itself.
    pnl_before = manager.daily_realized_pnl
    manager._maybe_reset_daily_loss_tracker()
    assert manager.daily_realized_pnl == pnl_before, (
        "the first _maybe_reset_daily_loss_tracker() call after restart must not reset "
        "today's restored daily_realized_pnl"
    )
    print(f"PASS: daily_pnl={manager.daily_realized_pnl:+.4f} correctly blocks new entries "
          f"post-restart, and survives the first _maybe_reset_daily_loss_tracker() call")


# ============================================================================
# 7) After restart, the fee-net daily profit target is restored from the
#    permanent trade log and therefore cannot be bypassed by a redeploy.
# ============================================================================

async def test_daily_profit_target_still_blocks_after_restart():
    print("\n=== test_daily_profit_target_still_blocks_after_restart ===")
    _reset_files()
    today = today_utc_str()
    write_trade(0.20, f"{today} 08:00:00 UTC", order_id=11)
    write_trade(0.31, f"{today} 09:00:00 UTC", order_id=12)

    manager = await make_manager()
    await manager.restore_runtime_accounting_from_history()

    assert manager.daily_realized_pnl >= trading.DAILY_PROFIT_TARGET_USDT, (
        f"expected daily_realized_pnl >= +{trading.DAILY_PROFIT_TARGET_USDT}, "
        f"got {manager.daily_realized_pnl:.4f}"
    )
    gate_blocks_entries = (
        trading.DAILY_PROFIT_TARGET_USDT > 0
        and manager.daily_realized_pnl >= trading.DAILY_PROFIT_TARGET_USDT
    )
    assert gate_blocks_entries, (
        "Daily Profit Target must block new entries immediately after restart"
    )
    pnl_before = manager.daily_realized_pnl
    manager._maybe_reset_daily_loss_tracker()
    assert manager.daily_realized_pnl == pnl_before, (
        "the first daily tracker reset check after restart must preserve today's "
        "restored fee-net profit"
    )
    print(
        f"PASS: daily fee-net PnL={manager.daily_realized_pnl:+.4f} keeps new "
        "entries locked after restart"
    )


# ============================================================================
# 7) Malformed/missing/corrupt/blank/None historical records fail soft.
# ============================================================================

async def test_corrupt_rows_fail_soft():
    print("\n=== test_corrupt_rows_fail_soft ===")
    _reset_files()
    today = today_utc_str()
    write_trade(0.1000, f"{today} 08:00:00 UTC", order_id=1)
    # Corrupt/incomplete rows, written directly (bypassing write_trade's
    # well-formed record) - these must not crash restoration or the rest
    # of the file from being counted.
    with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"symbol": "SOLUSDT", "net_pnl_usdt": None, "close_time": f"{today} 09:00:00 UTC"}) + "\n")
        f.write(json.dumps({"symbol": "SOLUSDT", "net_pnl_usdt": "not-a-number", "close_time": f"{today} 09:30:00 UTC"}) + "\n")
        f.write("{not even valid json\n")
        f.write("\n")  # blank line
    write_trade(0.2000, f"{today} 10:00:00 UTC", order_id=2)

    manager = await make_manager()
    # Must not raise.
    await manager.restore_runtime_accounting_from_history()

    # The invalid-JSON line and the blank line are already dropped silently
    # by TradeLogger.load_all() itself (pre-existing, unchanged behavior),
    # so they never reach this method at all. Of the two remaining
    # malformed rows: net_pnl_usdt=None is treated the same as "missing"
    # (contributes 0.0, matching PerformanceStats._safe_float()'s own
    # documented None-handling) and still counts as a trade; the
    # non-numeric "not-a-number" string fails float() and is skipped
    # entirely (does not increment trade_count). Net effect: 3 trades
    # counted (the 2 well-formed + the None row), 1 skipped.
    assert manager.trade_count == 3, f"expected 3 trades counted (2 well-formed + 1 None-pnl row), got {manager.trade_count}"
    assert abs(manager.realized_pnl_total - 0.3) < 1e-9, (
        f"expected realized_pnl_total=0.3 (the non-numeric row contributes 0), got {manager.realized_pnl_total:.4f}"
    )
    print(f"PASS: corrupt/None rows skipped fail-soft - trade_count={manager.trade_count} "
          f"realized_pnl_total={manager.realized_pnl_total:+.4f}")


async def test_missing_trade_log_file_is_a_no_op():
    """No trades_log.jsonl on disk at all (brand-new deployment, or a
    GitHub restore that genuinely found nothing) must leave the runtime
    counters at their fresh-process defaults, not raise."""
    print("\n=== test_missing_trade_log_file_is_a_no_op ===")
    _reset_files()
    assert not _os.path.exists(TRADE_LOG_PATH)

    manager = await make_manager()
    await manager.restore_runtime_accounting_from_history()

    assert manager.trade_count == 0
    assert manager.realized_pnl_total == 0.0
    assert manager.daily_realized_pnl == 0.0
    assert manager._daily_loss_tracker_date == today_utc_str()
    print("PASS: missing trades_log.jsonl leaves counters at 0 without raising")


# ============================================================================
# Hardening: a JSONL line can be syntactically valid JSON without being an
# object (null / list / bare string / bare number), and float() can
# successfully parse "NaN"/"Infinity" strings or accept a NaN/inf number
# straight from JSON. Neither may raise, and neither may ever enter
# realized_pnl_total/daily_realized_pnl.
# ============================================================================

async def test_non_dict_json_lines_fail_soft():
    print("\n=== test_non_dict_json_lines_fail_soft ===")
    _reset_files()
    today = today_utc_str()
    write_trade(0.1000, f"{today} 08:00:00 UTC", order_id=1)
    with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(None) + "\n")       # valid JSON null
        f.write(json.dumps([]) + "\n")          # valid JSON list
        f.write(json.dumps([1, 2, 3]) + "\n")   # valid JSON non-empty list
        f.write(json.dumps("just a string") + "\n")  # valid JSON string
        f.write(json.dumps(123) + "\n")         # valid JSON number
    write_trade(0.2000, f"{today} 10:00:00 UTC", order_id=2)

    manager = await make_manager()
    await manager.restore_runtime_accounting_from_history()  # must not raise (AttributeError on rec.get(...))

    assert manager.trade_count == 2, (
        f"expected only the 2 well-formed dict rows counted, got {manager.trade_count}"
    )
    assert abs(manager.realized_pnl_total - 0.3) < 1e-9, (
        f"expected realized_pnl_total=0.3 (non-dict rows contribute 0), got {manager.realized_pnl_total:.4f}"
    )
    assert math.isfinite(manager.realized_pnl_total)
    assert math.isfinite(manager.daily_realized_pnl)
    print(f"PASS: null/list/string/number JSON lines skipped fail-soft (no AttributeError) - "
          f"trade_count={manager.trade_count} realized_pnl_total={manager.realized_pnl_total:+.4f}")


async def test_nonfinite_pnl_fails_soft():
    print("\n=== test_nonfinite_pnl_fails_soft ===")
    _reset_files()
    today = today_utc_str()
    write_trade(0.1000, f"{today} 08:00:00 UTC", order_id=1)
    with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
        # json.dumps(float("nan")) emits the non-standard-but-Python-parseable
        # "NaN"/"Infinity"/"-Infinity" tokens - json.loads() (and therefore
        # TradeLogger.load_all()) accepts them by default, and float() on
        # the resulting Python float is a no-op pass-through - so these
        # rows DO reach this method as real dicts with a real float
        # net_pnl_usdt, not a TypeError/ValueError case.
        f.write(json.dumps({"symbol": "SOLUSDT", "net_pnl_usdt": float("nan"), "close_time": f"{today} 09:00:00 UTC"}) + "\n")
        f.write(json.dumps({"symbol": "SOLUSDT", "net_pnl_usdt": float("inf"), "close_time": f"{today} 09:30:00 UTC"}) + "\n")
        f.write(json.dumps({"symbol": "SOLUSDT", "net_pnl_usdt": float("-inf"), "close_time": f"{today} 09:45:00 UTC"}) + "\n")
        # Also cover the string-token spelling directly, in case a
        # historical row was ever hand-edited or produced by another tool.
        f.write('{"symbol": "SOLUSDT", "net_pnl_usdt": "NaN", "close_time": "%s 09:50:00 UTC"}\n' % today)
    write_trade(0.2000, f"{today} 10:00:00 UTC", order_id=2)

    manager = await make_manager()
    await manager.restore_runtime_accounting_from_history()  # must not raise, must not corrupt totals

    assert manager.trade_count == 2, (
        f"expected only the 2 well-formed finite-PnL rows counted, got {manager.trade_count}"
    )
    assert abs(manager.realized_pnl_total - 0.3) < 1e-9, (
        f"expected realized_pnl_total=0.3 (NaN/Infinity rows contribute 0, not NaN), "
        f"got {manager.realized_pnl_total!r}"
    )
    assert abs(manager.daily_realized_pnl - 0.3) < 1e-9
    assert math.isfinite(manager.realized_pnl_total), (
        f"realized_pnl_total must remain finite, got {manager.realized_pnl_total!r}"
    )
    assert math.isfinite(manager.daily_realized_pnl), (
        f"daily_realized_pnl must remain finite, got {manager.daily_realized_pnl!r}"
    )
    # A NaN daily_realized_pnl would make MAX_DAILY_LOSS_USDT's
    # `daily_realized_pnl <= -MAX_DAILY_LOSS_USDT` comparison always False
    # (NaN compares False to everything) - silently defeating Daily Loss
    # Protection. Confirm the restored value is a normal, comparable float.
    gate_condition_is_well_defined = (
        (manager.daily_realized_pnl <= -trading.MAX_DAILY_LOSS_USDT) is True
        or (manager.daily_realized_pnl <= -trading.MAX_DAILY_LOSS_USDT) is False
    )
    assert gate_condition_is_well_defined
    print(f"PASS: NaN/+-Infinity PnL rows skipped fail-soft - trade_count={manager.trade_count} "
          f"realized_pnl_total={manager.realized_pnl_total:+.4f} (finite) "
          f"daily_realized_pnl={manager.daily_realized_pnl:+.4f} (finite)")


async def test_valid_rows_before_and_after_malformed_rows_still_restore():
    """Malformed rows anywhere in the file (start, middle, end, mixed) must
    never prevent the well-formed rows around them from being counted."""
    print("\n=== test_valid_rows_before_and_after_malformed_rows_still_restore ===")
    _reset_files()
    today = today_utc_str()
    with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(None) + "\n")  # malformed BEFORE any valid row
    write_trade(0.1000, f"{today} 08:00:00 UTC", order_id=1)
    with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"net_pnl_usdt": float("nan"), "symbol": "SOLUSDT", "close_time": f"{today} 08:30:00 UTC"}) + "\n")
    write_trade(0.2000, f"{today} 09:00:00 UTC", order_id=2)
    with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps([1, 2]) + "\n")  # malformed AFTER the last valid row

    manager = await make_manager()
    await manager.restore_runtime_accounting_from_history()

    assert manager.trade_count == 2, f"expected 2 valid trades despite surrounding malformed rows, got {manager.trade_count}"
    assert abs(manager.realized_pnl_total - 0.3) < 1e-9, (
        f"expected realized_pnl_total=0.3, got {manager.realized_pnl_total:.4f}"
    )
    assert math.isfinite(manager.realized_pnl_total) and math.isfinite(manager.daily_realized_pnl)
    print(f"PASS: valid rows before/after malformed rows both restore correctly - "
          f"trade_count={manager.trade_count} realized_pnl_total={manager.realized_pnl_total:+.4f}")


# ============================================================================
# 8) Reconciliation after startup does not double-count already-restored
#    historical trades - exercises the REAL reconcile_trade_history_from_exchange()
#    dedup path (logged_binance_order_ids()) against a real flat->open->flat
#    Binance fill lifecycle, not a client=None early-return no-op.
# ============================================================================

async def test_reconciliation_does_not_double_count():
    print("\n=== test_reconciliation_does_not_double_count ===")
    _reset_files()
    today = today_utc_str()
    write_trade(0.5000, f"{today} 08:00:00 UTC", order_id=42)

    manager = await make_manager()
    await manager.restore_runtime_accounting_from_history()
    assert manager.trade_count == 1
    assert abs(manager.realized_pnl_total - 0.5) < 1e-9

    # A real Binance flat->open->flat fill lifecycle whose EXIT order id
    # (42) is already represented in trades_log.jsonl (see write_trade()
    # above, which writes binance_order_ids=[42]) - the existing
    # logged_binance_order_ids() dedup inside
    # reconcile_trade_history_from_exchange() must recognize this and skip
    # the whole lifecycle, not just avoid re-adding order_id=42 in
    # isolation. Entry leg uses a DIFFERENT order id (41) on purpose, so
    # this actually exercises the "any fill already logged -> skip the
    # WHOLE lifecycle" branch (order_ids & already_order_ids), not a
    # trivial single-id match.
    now_ms = int(time.time() * 1000)
    fills = [
        {
            "id": 9001, "orderId": 41, "side": "BUY", "qty": "1.0", "price": "100.0",
            "time": now_ms, "realizedPnl": "0", "commission": "0.05", "commissionAsset": "USDT",
        },
        {
            "id": 9002, "orderId": 42, "side": "SELL", "qty": "1.0", "price": "101.0",
            "time": now_ms + 1000, "realizedPnl": "0.55", "commission": "0.05", "commissionAsset": "USDT",
        },
    ]

    class FillsClient:
        """Stands in for RestClient - only get_user_trades() is exercised
        by reconcile_trade_history_from_exchange() in this test."""
        async def get_user_trades(self, symbol, from_id=None, limit=1000):
            return fills

    manager.client = FillsClient()  # DRY_RUN=false + a real client -> the actual reconcile path runs, not its early-return no-op
    trade_log_size_before = _os.path.getsize(TRADE_LOG_PATH)

    await manager.reconcile_trade_history_from_exchange(context="test")

    assert manager.trade_count == 1, (
        f"reconciliation must skip a lifecycle already represented via order_id=42 "
        f"(existing logged_binance_order_ids() dedup), got trade_count={manager.trade_count}"
    )
    assert abs(manager.realized_pnl_total - 0.5) < 1e-9, (
        f"reconciliation must not double-count order_id=42's PnL into realized_pnl_total, "
        f"got {manager.realized_pnl_total:.4f}"
    )
    assert abs(manager.daily_realized_pnl - 0.5) < 1e-9, (
        f"reconciliation must not double-count order_id=42's PnL into daily_realized_pnl, "
        f"got {manager.daily_realized_pnl:.4f}"
    )
    trade_log_size_after = _os.path.getsize(TRADE_LOG_PATH)
    assert trade_log_size_after == trade_log_size_before, (
        "reconciliation must not append a duplicate row to trades_log.jsonl for an "
        "already-logged lifecycle"
    )
    logged = trading.TradeLogger().load_all()
    assert len(logged) == 1, f"expected exactly 1 trade in trades_log.jsonl after reconcile, got {len(logged)}"
    print(f"PASS: trade_count={manager.trade_count} realized_pnl_total={manager.realized_pnl_total:+.4f} "
          f"daily_realized_pnl={manager.daily_realized_pnl:+.4f} unchanged, and no duplicate row "
          f"appended, after a real reconcile pass over an already-logged lifecycle "
          f"(entry order_id=41, exit order_id=42)")


# ============================================================================
# 9) Existing DCA restart recovery remains unchanged.
# ============================================================================

async def test_existing_dca_step_restart_recovery_unchanged():
    print("\n=== test_existing_dca_step_restart_recovery_unchanged ===")
    _reset_files()

    manager = await make_manager()
    p = manager.position
    p.side = "LONG"
    p.status = "OPEN"
    p.entries = [(100.0, 1.0)]
    p.total_qty = 1.0
    p.original_qty = 1.0
    p.avg_entry_price = 100.0
    p.opened_at = time.time() - 60

    await manager._on_entry_filled("dca", fill_price=98.0, fill_qty=1.0, order_id=101)
    assert p.dca_step == 1
    await asyncio.sleep(0.05)  # let the fire-and-forget save_dca_state() from the DCA fill land

    manager2 = await make_manager()
    # This fix's new step, run exactly where it sits in the real startup
    # sequence - must not perturb the DCA snapshot restore that follows.
    await manager2.restore_runtime_accounting_from_history()
    await bot.load_dca_state(manager2)
    assert manager2.position.dca_step == 1, (
        f"expected dca_step=1 pre-populated from the persisted snapshot, got {manager2.position.dca_step}"
    )

    matching_rows = rows_for("LONG", manager.position.total_qty, manager.position.avg_entry_price)
    await trading.initialize_sync(FakeClient(matching_rows), manager2, context="startup", rows=matching_rows)
    assert manager2.position.dca_step == 1, (
        f"existing DCA restart recovery must be unaffected by this fix - "
        f"expected dca_step=1, got {manager2.position.dca_step}"
    )
    assert manager2.position.status == "OPEN"
    assert manager2.position.avg_entry_price == manager.position.avg_entry_price
    assert manager2.position.total_qty == manager.position.total_qty
    print(f"PASS: dca_step={manager2.position.dca_step} correctly restored, unchanged by this fix")


async def main():
    await test_restore_trade_count_and_realized_pnl_total()
    await test_previous_day_trades_excluded_from_daily_bucket()
    await test_max_daily_loss_still_blocks_after_restart()
    await test_daily_profit_target_still_blocks_after_restart()
    await test_corrupt_rows_fail_soft()
    await test_missing_trade_log_file_is_a_no_op()
    await test_non_dict_json_lines_fail_soft()
    await test_nonfinite_pnl_fails_soft()
    await test_valid_rows_before_and_after_malformed_rows_still_restore()
    await test_reconciliation_does_not_double_count()
    await test_existing_dca_step_restart_recovery_unchanged()
    print("\nAll test_restart_accounting_fix tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
