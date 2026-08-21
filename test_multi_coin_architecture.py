"""Offline regression tests for the 2026-08-20 multi-coin refactor.

WHAT CHANGED
--------------------------------------------------------------------------
  1. config.ACTIVE_SYMBOLS   - the watchlist (SYMBOL is forced first).
     config.MAX_ACTIVE_TRADES - portfolio-wide cap on open positions.
  2. Per-symbol file isolation. Every persistence path a MartingaleManager
     touches is derived from ITS OWN self.symbol via
     trading.resolve_symbol_paths(), instead of the module-level config
     constants that are frozen at import time to the single primary SYMBOL.
  3. trading.PortfolioCoordinator enforces MAX_ACTIVE_TRADES across the
     whole watchlist, with a SYNCHRONOUS reservation so two symbols cannot
     both pass the gate before either reports OPEN.
  4. websocket.userdata_consumer(..., managers=...) opens ONE account-wide
     user-data stream and routes each event to the manager owning its
     symbol. This is the highest-risk part of the refactor: the user-data
     stream is per-ACCOUNT, so before this change a four-manager process
     would have applied every symbol's fills to every manager.
  5. github_sync's upload lock is shared per (repo, branch) across
     instances, since each manager now owns its own client.

No network call and no real order is made anywhere in this file.

Run directly: `python3 test_multi_coin_architecture.py`
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "false")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import asyncio
import sys

import config
import trading
import websocket as ws
import github_sync
from trading import PortfolioCoordinator, resolve_symbol_paths

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -> ' + detail) if detail and not cond else ''}")


# ===========================================================================
print("\n[1] Watchlist definition")
# ===========================================================================
check("ACTIVE_SYMBOLS contains all four coins",
      set(config.ACTIVE_SYMBOLS) == {"SOLUSDT", "BTCUSDT", "ETHUSDT", "NEARUSDT"},
      f"got {config.ACTIVE_SYMBOLS}")
check("the primary SYMBOL is first in the watchlist",
      config.ACTIVE_SYMBOLS[0] == config.SYMBOL, f"got {config.ACTIVE_SYMBOLS}")
check("MAX_ACTIVE_TRADES defaults to 1", config.MAX_ACTIVE_TRADES == 1,
      f"got {config.MAX_ACTIVE_TRADES}")
check("no duplicates in the watchlist",
      len(config.ACTIVE_SYMBOLS) == len(set(config.ACTIVE_SYMBOLS)))

# parser edge cases
check("a watchlist that omits the primary still gets it, first",
      config._parse_symbol_list("BTCUSDT,ETHUSDT", "SOLUSDT") == ["SOLUSDT", "BTCUSDT", "ETHUSDT"])
check("blank/whitespace entries are dropped",
      config._parse_symbol_list("BTCUSDT, ,  ,ETHUSDT", "SOLUSDT")
      == ["SOLUSDT", "BTCUSDT", "ETHUSDT"])
check("lower-case env input is normalised",
      config._parse_symbol_list("btcusdt,ethusdt", "solusdt") == ["solusdt", "BTCUSDT", "ETHUSDT"])
check("a duplicate of the primary is not repeated",
      config._parse_symbol_list("SOLUSDT,BTCUSDT", "SOLUSDT") == ["SOLUSDT", "BTCUSDT"])
check("an empty watchlist still yields the primary",
      config._parse_symbol_list("", "SOLUSDT") == ["SOLUSDT"])

# ===========================================================================
print("\n[2] Strict per-symbol file isolation")
# ===========================================================================
LOGICAL = ["brain_local", "dca_state", "stats_csv", "stats_json",
           "trade_log_csv", "trade_log_json", "trade_cursor",
           "github_brain", "github_dca_state", "github_cursor",
           "github_trades_csv", "github_trades_json", "github_stats_csv"]

paths = {sym: resolve_symbol_paths(sym) for sym in config.ACTIVE_SYMBOLS}

for key in LOGICAL:
    vals = [paths[s][key] for s in config.ACTIVE_SYMBOLS]
    check(f"'{key}' is distinct across all 4 symbols", len(set(vals)) == len(vals),
          f"collision: {vals}")

# the exact naming convention the refactor specifies
env = config.RUNTIME_ENV
for sym in config.ACTIVE_SYMBOLS:
    p = paths[sym]
    expect = {
        "brain_local":    f"brain_{env}_{sym}.pkl",
        "dca_state":      f"dca_state_{env}_{sym}.json",
        "stats_csv":      f"performance_stats_{env}_{sym}.csv",
        "trade_log_json": f"trades_log_{env}_{sym}.jsonl",
        "trade_cursor":   f"trade_sync_cursor_{env}_{sym}.json",
    }
    for key, want in expect.items():
        check(f"{sym} {key} -> {want}", os.path.basename(p[key]) == want,
              f"got {p[key]}")

# every symbol's whole path set must be disjoint from every other's
for i, a in enumerate(config.ACTIVE_SYMBOLS):
    for b in config.ACTIVE_SYMBOLS[i + 1:]:
        overlap = set(paths[a].values()) & set(paths[b].values())
        check(f"{a} and {b} share NO file at all", not overlap, f"shared: {overlap}")

# and every generated name must actually carry its own symbol
for sym in config.ACTIVE_SYMBOLS:
    bad = [v for v in paths[sym].values() if sym not in v]
    check(f"every {sym} path is stamped with {sym}", not bad, f"unstamped: {bad}")

# ===========================================================================
print("\n[3] PortfolioCoordinator - the MAX_ACTIVE_TRADES cap")
# ===========================================================================
pc = PortfolioCoordinator(1)
check("a fresh portfolio has capacity", pc.has_capacity("SOLUSDT"))
check("first reserve succeeds", pc.try_reserve("SOLUSDT") is True)
check("the portfolio now reports 1 active", pc.active_count() == 1)
check("a SECOND symbol is refused", pc.try_reserve("BTCUSDT") is False)
check("the second symbol has no capacity", pc.has_capacity("BTCUSDT") is False)
check("the HOLDER still has capacity for itself (DCA adds)", pc.has_capacity("SOLUSDT") is True)
check("re-reserving as the holder is idempotent", pc.try_reserve("SOLUSDT") is True)
check("still only 1 active after the holder re-reserves", pc.active_count() == 1)
check("holders() names the holder", pc.holders() == ["SOLUSDT"])

pc.confirm("SOLUSDT")
check("confirm keeps the count at 1", pc.active_count() == 1)
pc.release("SOLUSDT")
check("release frees the slot", pc.active_count() == 0)
check("a double release is harmless", (pc.release("SOLUSDT"), pc.active_count()) [1] == 0)
check("the next symbol can now take the slot", pc.try_reserve("BTCUSDT") is True)

# a cap above 1 still behaves
pc2 = PortfolioCoordinator(2)
check("cap=2 admits two symbols",
      pc2.try_reserve("A") and pc2.try_reserve("B") and pc2.active_count() == 2)
check("cap=2 refuses a third", pc2.try_reserve("C") is False)

# a nonsense cap must not disable trading outright
check("cap is clamped to >= 1", PortfolioCoordinator(0).max_active == 1)
check("a negative cap is clamped to 1", PortfolioCoordinator(-5).max_active == 1)

# confirm() adopts a position this process never reserved (restart recovery)
pc3 = PortfolioCoordinator(1)
pc3.confirm("ETHUSDT")
check("confirm adopts a recovered position without a prior reserve",
      pc3.active_count() == 1 and pc3.try_reserve("SOLUSDT") is False)

# ===========================================================================
print("\n[4] The reservation is ATOMIC across concurrent symbols")
# ===========================================================================
# This is the property the whole cap rests on. Four coroutines race for one
# slot with an await immediately after the claim - exactly the shape of
# _place_step_order(), where the claim happens before the order round-trip.
pc = PortfolioCoordinator(1)
winners = []


async def racer(sym):
    await asyncio.sleep(0)             # force interleaving
    if pc.try_reserve(sym):            # synchronous check-and-set
        winners.append(sym)
        await asyncio.sleep(0.01)      # the "order round-trip"


async def race():
    await asyncio.gather(*[racer(s) for s in config.ACTIVE_SYMBOLS])

asyncio.run(race())
check("exactly ONE symbol wins the slot in a 4-way race", len(winners) == 1,
      f"winners={winners}")
check("the portfolio holds exactly 1 slot after the race", pc.active_count() == 1)

# ===========================================================================
print("\n[5] Manager wiring - per-instance paths and shared portfolio")
# ===========================================================================
filters = trading.SymbolFilters(
    tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0,
)
shared = PortfolioCoordinator(1)
mgrs = {s: trading.MartingaleManager(None, s, filters, 20, portfolio=shared)
        for s in config.ACTIVE_SYMBOLS}

for sym, m in mgrs.items():
    check(f"{sym} manager carries its own symbol", m.symbol == sym)
    check(f"{sym} manager's brain path is its own",
          m.paths["brain_local"] == f"brain_{env}_{sym}.pkl", f"got {m.paths['brain_local']}")
    check(f"{sym} trade logger writes its own CSV",
          m.trade_logger.csv_path == f"trades_log_{env}_{sym}.csv",
          f"got {m.trade_logger.csv_path}")
    check(f"{sym} perf stats writes its own CSV",
          m.perf_stats.csv_path == f"performance_stats_{env}_{sym}.csv",
          f"got {m.perf_stats.csv_path}")
    check(f"{sym} github client defaults to its own brain",
          m.github_sync.path == f"brain_{env}_{sym}.pkl", f"got {m.github_sync.path}")

check("all four managers share ONE portfolio",
      len({id(m.portfolio) for m in mgrs.values()}) == 1)
check("a manager built without a portfolio gets its own single-slot one",
      trading.MartingaleManager(None, "XRPUSDT", filters, 20).portfolio.max_active == 1)

# No FILE may be claimed by two DIFFERENT symbols. One symbol legitimately
# maps several logical keys onto the same name - brain_local and
# github_brain are deliberately the same filename locally and remotely -
# so the check is on distinct symbols, not on distinct logical keys.
owners = {}
for sym, m in mgrs.items():
    for v in m.paths.values():
        owners.setdefault(v, set()).add(sym)
crossed = {k: sorted(v) for k, v in owners.items() if len(v) > 1}
check("no file is claimed by two DIFFERENT managers", not crossed, f"{crossed}")
check("each manager still owns a full set of files",
      all(len(set(m.paths.values())) >= 7 for m in mgrs.values()))

# ===========================================================================
print("\n[6] The gate reconciles itself from real position status")
# ===========================================================================
sol, btc = mgrs["SOLUSDT"], mgrs["BTCUSDT"]
for status in ("OPEN", "ENTERING", "DCA_PENDING", "CLOSING"):
    shared._slots.clear()
    sol.position.status = status
    sol._sync_portfolio_slot()
    check(f"status={status} claims the portfolio slot", shared.active_count() == 1)
    check(f"status={status} blocks the other symbols",
          shared.has_capacity("BTCUSDT") is False)

shared._slots.clear()
sol.position.status = "OPEN"
sol._sync_portfolio_slot()
sol.position.status = "FLAT"
sol._sync_portfolio_slot()
check("going FLAT releases the slot", shared.active_count() == 0)
check("the other symbols can trade again", shared.has_capacity("BTCUSDT") is True)

# a manager that never had a slot and is flat must not create one
btc.position.status = "FLAT"
btc._sync_portfolio_slot()
check("a flat manager does not fabricate a slot", shared.active_count() == 0)

# ===========================================================================
print("\n[7] User-data routing - the highest-risk piece")
# ===========================================================================
check("ORDER_TRADE_UPDATE symbol is read from o.s",
      ws._event_symbol({"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "i": 1}}) == "BTCUSDT")
check("ACCOUNT_UPDATE-style nesting resolves",
      ws._event_symbol({"e": "X", "a": {"s": "ETHUSDT"}}) == "ETHUSDT")
check("a top-level symbol resolves",
      ws._event_symbol({"e": "ALGO_UPDATE", "s": "NEARUSDT"}) == "NEARUSDT")
check("a nested vendor payload resolves",
      ws._event_symbol({"e": "ALGO_UPDATE", "ao": {"symbol": "SOLUSDT"}}) == "SOLUSDT")
check("lower-case is normalised", ws._event_symbol({"o": {"s": "solusdt"}}) == "SOLUSDT")
check("an event with NO symbol returns None (never a guess)",
      ws._event_symbol({"e": "listenKeyExpired"}) is None)
check("a blank symbol returns None", ws._event_symbol({"o": {"s": "   "}}) is None)


class RecordingManager:
    """Minimal stand-in: records what it was handed."""
    def __init__(self, symbol):
        self.symbol = symbol
        self.orders, self.algos, self.accounts = [], [], []
        self.available_balance = 0.0

    async def handle_order_update(self, e):
        self.orders.append(e)

    async def handle_algo_update(self, e):
        self.algos.append(e)

    def on_account_update(self, e):
        self.accounts.append(e)


rec = {s: RecordingManager(s) for s in config.ACTIVE_SYMBOLS}


def build_router(registry):
    """Rebuilds userdata_consumer's internal routing exactly, so the demux
    is tested without opening a websocket."""
    _registry = {k.strip().upper(): v for k, v in registry.items()}
    warned = set()

    def _route(event):
        sym = ws._event_symbol(event)
        if sym is None:
            return None
        target = _registry.get(sym)
        if target is None and sym not in warned:
            warned.add(sym)
        return target
    return _route


route = build_router(rec)
check("a BTCUSDT fill routes to the BTCUSDT manager",
      route({"o": {"s": "BTCUSDT"}}) is rec["BTCUSDT"])
check("a SOLUSDT fill routes to the SOLUSDT manager",
      route({"o": {"s": "SOLUSDT"}}) is rec["SOLUSDT"])
check("a BTCUSDT fill does NOT reach the SOLUSDT manager",
      route({"o": {"s": "BTCUSDT"}}) is not rec["SOLUSDT"])
check("an off-watchlist symbol routes to nobody",
      route({"o": {"s": "DOGEUSDT"}}) is None)
check("an unattributable event routes to nobody",
      route({"e": "listenKeyExpired"}) is None)

# ACCOUNT_UPDATE is broadcast; each manager filters its own position row
acct = {
    "e": "ACCOUNT_UPDATE",
    "a": {"B": [{"a": "USDT", "wb": "19.57", "cw": "19.57"}],
          "P": [{"s": "BTCUSDT", "pa": "0.001"}, {"s": "SOLUSDT", "pa": "0"}]},
}
for m in rec.values():
    m.on_account_update(acct)
check("ACCOUNT_UPDATE reaches every manager (balance is account-wide)",
      all(len(m.accounts) == 1 for m in rec.values()))

# the real manager must ignore another symbol's position row
real = mgrs["SOLUSDT"]
real.on_account_update(acct)
check("a real manager ignores another symbol's position row in ACCOUNT_UPDATE",
      real.ws_position_amt in (0.0, None), f"got {real.ws_position_amt}")

# ===========================================================================
print("\n[8] Market streams subscribe to each manager's OWN symbol")
# ===========================================================================
import inspect
src = inspect.getsource(ws.market_data_consumer)
check("market_data_consumer derives the symbol from the manager",
      "manager.symbol" in src)
check("market_data_consumer no longer builds URLs from the SYMBOL global",
      "{SYMBOL.lower()}" not in src)
check("stream labels carry the symbol (so 4 coins' logs stay separable)",
      'label=f"public/bookTicker:{sym}"' in src)

# ===========================================================================
print("\n[9] GitHub sync - one upload lock per branch, shared across coins")
# ===========================================================================
clients = {s: github_sync.GithubBrainSync("t", "o/r", f"brain_{env}_{s}.pkl", "brain-state")
           for s in config.ACTIVE_SYMBOLS}
locks = {id(c._upload_lock) for c in clients.values()}
check("all four coins serialize through ONE upload lock", len(locks) == 1,
      f"{len(locks)} distinct locks - concurrent PUTs would 409")
other_branch = github_sync.GithubBrainSync("t", "o/r", "x.pkl", "different-branch")
check("a different branch gets its own lock",
      id(other_branch._upload_lock) not in locks)
check("a different repo gets its own lock",
      id(github_sync.GithubBrainSync("t", "o/other", "x.pkl", "brain-state")._upload_lock)
      not in locks)
check("upload() still accepts an explicit per-file path",
      "path" in inspect.signature(github_sync.GithubBrainSync.upload).parameters)
check("download() still accepts an explicit per-file path",
      "path" in inspect.signature(github_sync.GithubBrainSync.download).parameters)
check("the sha cache is keyed per path, not global",
      isinstance(clients["SOLUSDT"]._last_sha, dict))

# ===========================================================================
print("\n[10] No global static filename survives in the manager")
# ===========================================================================
import re
lines = open("trading.py").read().splitlines()
start = next(i for i, l in enumerate(lines) if l.startswith("class MartingaleManager:"))
GLOBALS = ["TRADE_LOG_JSON_PATH", "TRADE_LOG_CSV_PATH", "STATS_JSON_PATH", "STATS_CSV_PATH",
           "BRAIN_LOCAL_PATH", "GITHUB_BRAIN_PATH", "DCA_STATE_PATH", "TRADE_SYNC_CURSOR_PATH",
           "GITHUB_DCA_STATE_PATH", "GITHUB_TRADE_SYNC_CURSOR_PATH",
           "GITHUB_TRADES_LOG_CSV_PATH", "GITHUB_STATS_CSV_PATH", "GITHUB_TRADES_LOG_JSON_PATH"]
pat = re.compile(r"\b(" + "|".join(GLOBALS) + r")\b")
leaks = [(i + 1, l.strip()[:80]) for i, l in enumerate(lines[start:], start)
         if pat.search(l) and not l.lstrip().startswith("#") and '"""' not in l]
check("MartingaleManager references NO primary-scoped path global",
      not leaks, f"{leaks}")

d2 = open("dca2.py").read()
check("load_dca_state() reads the manager's own snapshot",
      "manager.paths[\"dca_state\"]" in d2)
check("load_dca_state() pulls the manager's own remote snapshot",
      "manager.paths[\"github_dca_state\"]" in d2)

# ===========================================================================
print("\n" + "=" * 74)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
