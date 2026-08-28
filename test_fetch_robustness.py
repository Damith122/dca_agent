#!/usr/bin/env python3
"""Tests for the fetchers' resilience to one bad symbol.

A universe download makes hundreds of independent requests. The failure that
matters is not that one of them breaks - it is that breaking one discards
the other five hundred. That happened live: 521 symbols downloaded, then a
promotional listing with CJK characters in its symbol raised
UnicodeEncodeError inside urllib and took the whole run with it.
"""
import sys
import urllib.error

import fetch_binance_data as P
import fetch_funding_universe as F

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


BAD = "币安人生USDT"          # the symbol that broke the run

print("[1] Ticker validation")
for good in ("BTCUSDT", "1000PEPEUSDT", "ETHUSDT", "1MBABYDOGEUSDT"):
    check(f"{good} is accepted", bool(F.TICKER.match(good)))
for bad in (BAD, "btcusdt", "BTC-USDT", "BTC USDT", "", "A"):
    check(f"{bad!r} is rejected", not F.TICKER.match(bad))
check("both fetchers use the same rule", F.TICKER.pattern == P.TICKER.pattern)

print("\n[2] Non-ASCII never reaches a URL")
err = None
try:
    F.fetch_symbol(BAD, 1)
except ValueError as e:
    err = e
except UnicodeEncodeError as e:  # the original failure
    err = e
check("a non-ticker raises ValueError, not UnicodeEncodeError",
      isinstance(err, ValueError), type(err).__name__)
check("...and not SystemExit either", not isinstance(err, SystemExit))
# safe() downgrades only when the console cannot take the characters, so
# the property to assert is "encodable in THIS console's encoding", not
# "always ASCII" - on a UTF-8 terminal the original text passes through.
enc = sys.stdout.encoding or "ascii"


def printable(text):
    try:
        F.safe(text).encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


check("the message is printable on this console", printable(str(err)))
check("...and so is the raw symbol once passed through safe()", printable(BAD))

import urllib.parse
q = urllib.parse.quote(BAD, safe="")
check("percent-encoding makes the symbol URL-safe",
      q.isascii() and "%" in q, q[:20])

print("\n[3] Printing to a cp1252 console does not crash")
check("safe() output always fits the console encoding", printable(BAD),
      f"stdout={enc}")
# Simulate a cp1252 console, which is the Windows default and where the
# original run would have crashed a second time on the progress line.
class _Cp1252:
    encoding = "cp1252"


_real = sys.stdout
try:
    sys.stdout = _Cp1252()
    downgraded = F.safe(BAD)
finally:
    sys.stdout = _real
check("on a cp1252 console it downgrades to ASCII escapes",
      downgraded.isascii(), downgraded[:24])
check("...without losing the information entirely",
      "\\u" in downgraded or "\\x" in downgraded, downgraded[:24])
check("safe() leaves ordinary text alone", F.safe("BTCUSDT") == "BTCUSDT")
check("safe() never raises on any input",
      all(isinstance(F.safe(x), str)
          for x in (BAD, "", "ok", "—dash", "\U0001F600")))
check("both fetchers expose it", callable(P.safe) and callable(F.safe))

print("\n[4] One failure does not end the run")
src = open("fetch_funding_universe.py", encoding="utf-8").read()
check("a per-symbol error is caught inside the loop",
      "except Exception as e:  # noqa: BLE001 - one symbol must not end the run"
      in src)
check("fetch_symbol no longer raises SystemExit for a bad symbol",
      "raise SystemExit(" not in src.split("def main")[0])
check("a run of consecutive failures DOES stop it",
      "if net_fail >= 10:" in src)
check("...and says the earlier downloads are still usable",
      "are still usable" in src)
check("the counter resets after a success", "net_fail = 0" in src)

psrc = open("fetch_binance_data.py", encoding="utf-8").read()
check("the price fetcher also survives one bad symbol",
      "except Exception as e:  # noqa: BLE001 - one symbol, not the run" in psrc)
check("...and reports what it skipped", "skipped or failed" in psrc)
check("bad symbols are filtered out of universe.txt at the source",
      "syms = sorted(s for s in raw if TICKER.match(s))" in psrc)
check("...and the drop is reported, not silent",
      "not a plain ticker" in psrc)

print("\n[5] Every URL built is ASCII-safe")
for s in (psrc, src):
    check("symbols are percent-encoded before interpolation",
          "urllib.parse.quote(symbol" in s or "quote(symbol, safe=" in s)

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
