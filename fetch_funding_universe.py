#!/usr/bin/env python3
"""Download funding-rate history for an entire perpetual universe.

    python3 fetch_funding_universe.py --universe-file data/universe.txt \
                                      --months 6 --out data/funding

Writes data/funding/<SYMBOL>.csv with columns funding_time,rate_bps.

Funding is published every eight hours, so six months is about 550 rows per
symbol - one API call each, no key required. Across ~500 symbols that is a
few minutes.

Why this is worth downloading
-----------------------------
Every price-based signal tested on this universe measured an information
coefficient near zero. Funding is not a forecast: it is a rate the exchange
publishes before you commit, and the short side of a positive-funding perp
receives it. Ranking 492 names by that rate gives a cross-sectional book
whose expected return is an observable cash flow rather than a prediction.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Tuple

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

# Binance tickers are uppercase alphanumerics. The universe file is written
# from exchangeInfo, which has been seen to carry a promotional listing with
# CJK characters in its symbol - and a non-ASCII symbol interpolated into a
# URL makes urllib raise UnicodeEncodeError before any request is sent.
TICKER = re.compile(r"[A-Z0-9]{2,20}\Z")


def safe(text: str) -> str:
    """Render text that a Windows cp1252 console can print.

    The symbol that broke the run would also have crashed the progress line
    on the way to reporting itself - two failures from one bad string, and
    the second would have been even more confusing than the first.
    """
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "backslashreplace").decode("ascii")


def fetch_symbol(symbol: str, months: float, pause: float = 0.15
                 ) -> List[Tuple[float, float]]:
    """[(funding_time_seconds, rate_bps)] oldest first, deduplicated.

    Raises ValueError for a symbol this endpoint cannot be asked about, and
    urllib errors for genuine network trouble. The caller distinguishes the
    two: one bad symbol must never discard hundreds of good downloads.
    """
    if not TICKER.match(symbol):
        raise ValueError(f"{safe(symbol)!r} is not a Binance ticker")
    symbol = urllib.parse.quote(symbol, safe="")
    end = int(time.time() * 1000)
    start = end - int(months * 30.44 * 86_400_000)
    out = {}
    cursor = start
    while cursor < end:
        url = f"{FUNDING_URL}?symbol={symbol}&startTime={cursor}&limit=1000"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                rows = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return []          # symbol has no funding history; skip it
            raise
        except urllib.error.URLError:
            raise                      # network trouble - the caller decides
        if not rows:
            break
        for r in rows:
            out[int(r["fundingTime"])] = float(r["fundingRate"]) * 1e4
        nxt = int(rows[-1]["fundingTime"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(pause)
    return [(k / 1000.0, out[k]) for k in sorted(out)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe-file", default="data/universe.txt")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--months", type=float, default=6.0)
    ap.add_argument("--out", default="data/funding")
    ap.add_argument("--min-rows", type=int, default=200,
                    help="skip symbols with less history than this")
    a = ap.parse_args(argv)

    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        with open(a.universe_file, encoding="utf-8") as fh:
            syms = [l.strip().upper() for l in fh
                    if l.strip() and not l.startswith("#")]

    os.makedirs(a.out, exist_ok=True)
    print(f"=== funding history for {len(syms)} symbols, {a.months:g} months ===")
    kept, short, unusable, net_fail = 0, 0, [], 0
    for i, sym in enumerate(syms, 1):
        label = safe(sym)
        try:
            rows = fetch_symbol(sym, a.months)
            net_fail = 0
        except ValueError as e:
            # Not a ticker this endpoint can be asked about. Skip it and say
            # so - never abandon the symbols already downloaded.
            unusable.append(label)
            print(f"\r  [{i}/{len(syms)}] {label:<14s} skipped: {e}      ")
            continue
        except Exception as e:  # noqa: BLE001 - one symbol must not end the run
            net_fail += 1
            unusable.append(label)
            print(f"\r  [{i}/{len(syms)}] {label:<14s} failed: "
                  f"{safe(str(e))[:60]}      ")
            if net_fail >= 10:
                # Ten in a row is not bad symbols, it is a dead connection.
                print("\n  Ten consecutive failures - stopping. If these are "
                      "403s on CONNECT,\n  this machine is blocked from "
                      "Binance; run it locally.")
                print(f"  {kept} symbols were already written to {a.out} and "
                      "are still usable.")
                break
            time.sleep(1.0)
            continue

        if len(rows) < a.min_rows:
            short += 1
            print(f"\r  [{i}/{len(syms)}] {label:<14s} "
                  f"only {len(rows)} rows - skipped        ", end="")
            continue
        with open(os.path.join(a.out, f"{sym}.csv"), "w", newline="",
                  encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["funding_time", "rate_bps"])
            w.writerows(rows)
        kept += 1
        print(f"\r  [{i}/{len(syms)}] {label:<14s} {len(rows)} rows          ",
              end="", flush=True)
    print(f"\r  done. {kept} written to {a.out}; {short} skipped for short "
          f"history; {len(unusable)} unusable.        ")
    if unusable:
        print(f"  unusable: {', '.join(unusable[:8])}"
              f"{' ...' if len(unusable) > 8 else ''}")
    if kept < 30:
        print("\n  WARNING: a cross-sectional funding book needs breadth. Under")
        print("  30 names there is not enough dispersion for the ranking to")
        print("  mean anything.")
    else:
        print(f"\n  next: python3 backtest_xs_funding.py --funding {a.out} "
              f"--csv data/1h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
