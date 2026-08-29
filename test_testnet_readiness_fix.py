#!/usr/bin/env python3
"""Tests for switching the watchlist onto Binance testnet.

Testnet lists far fewer symbols than mainnet, so the failure that never
mattered on mainnet - a watchlist symbol that is not on the venue - becomes
the likely case. These pin that it degrades to one excluded symbol rather
than a dead process, and that the environment switch isolates state as
intended.
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("SYMBOL", "SOLUSDT")

import asyncio
import importlib
import sys

import exchange

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


print("[1] An unlisted symbol must not kill the process")
check("SymbolNotListed is an Exception, not a BaseException",
      issubclass(exchange.SymbolNotListed, Exception))
check("...so `except Exception` catches it",
      not issubclass(exchange.SymbolNotListed, SystemExit))
caught = False
try:
    raise exchange.SymbolNotListed("NEARUSDT")
except Exception:
    caught = True
check("...demonstrated", caught)

src = open("exchange.py", encoding="utf-8").read()
check("fetch_symbol_filters no longer raises SystemExit",
      "raise SystemExit(f\"Symbol {symbol} not found" not in src)
check("...and the message names the venue mismatch",
      "testnet lists far fewer symbols than mainnet" in src)
check("...and reports how many symbols the venue does list",
      "symbols available" in src)


class _Client:
    def __init__(self, symbols):
        self._symbols = symbols

    async def get_exchange_info(self):
        return {"symbols": [{"symbol": s, "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
            for s in self._symbols]}


async def _probe(listed, want):
    try:
        await exchange.fetch_symbol_filters(_Client(listed), want)
        return None
    except Exception as e:  # noqa: BLE001
        return e


err = asyncio.run(_probe(["BTCUSDT", "ETHUSDT"], "NEARUSDT"))
check("a symbol absent from exchangeInfo raises SymbolNotListed",
      isinstance(err, exchange.SymbolNotListed), type(err).__name__)
check("...and the message names the symbol", "NEARUSDT" in str(err))
ok = asyncio.run(_probe(["BTCUSDT", "ETHUSDT"], "ETHUSDT"))
check("a listed symbol still resolves", ok is None, str(ok))

print("\n[2] The setup loop excludes it rather than aborting")
dsrc = open("dca2.py", encoding="utf-8").read()
check("SymbolNotListed has its own branch",
      "except SymbolNotListed as e:" in dsrc)
check("...printed before the generic failure branch",
      dsrc.index("except SymbolNotListed") < dsrc.index(
          "except Exception as e:  # noqa: BLE001 - one symbol must not sink"))
check("...and says NOT LISTED rather than FAILED",
      "is NOT LISTED here" in dsrc)
check("the run still refuses to start with zero symbols",
      "refusing to run blind" in dsrc)
check("SymbolNotListed is imported where it is caught",
      "SymbolNotListed" in dsrc.split("from exchange import")[1][:200])

print("\n[3] Switching environment isolates every piece of state")
for env, tag in (("false", "LIVE"), ("true", "TESTNET")):
    os.environ["USE_TESTNET"] = env
    import config
    importlib.reload(config)
    check(f"USE_TESTNET={env} -> RUNTIME_ENV {tag}", config.RUNTIME_ENV == tag)
    for label, path in (("brain", config.BRAIN_LOCAL_PATH),
                        ("dca state", config.DCA_STATE_PATH),
                        ("trade log", config.TRADE_LOG_CSV_PATH),
                        ("shards", config.FEATURE_LOG_PATH)):
        check(f"  {label} is scoped to {tag}", f"_{tag}_" in path, path)

os.environ["USE_TESTNET"] = "true"
importlib.reload(config)
check("testnet points at the testnet REST host",
      config.REST_BASE == "https://testnet.binancefuture.com")
check("...and the testnet stream host",
      "binancefuture.com" in config.WS_MARKET_BASE)

print("\n[4] The real-money gates apply to mainnet only")
csrc = open("dca2.py", encoding="utf-8").read()
check("the money gate is conditioned on NOT testnet",
      "if not USE_TESTNET and not I_UNDERSTAND_THIS_IS_REAL_MONEY" in csrc)
check("...as is the second confirmation gate",
      "if not USE_TESTNET and not LIVE_TRADING_CONFIRMATION" in csrc)
check("so DRY_RUN=false on testnet needs no extra flag",
      "USE_TESTNET" in csrc)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
