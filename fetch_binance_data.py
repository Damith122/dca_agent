#!/usr/bin/env python3
"""Download historical klines from Binance and write them in the exact CSV
format backtest_breakout.py expects.

    python3 fetch_binance_data.py --symbols SOLUSDT,BTCUSDT,ETHUSDT,NEARUSDT \
                                  --intervals 1h,15m --months 6 --out data

Produces  data/1h/SOLUSDT.csv,  data/15m/SOLUSDT.csv,  ... with columns
ts,open,high,low,close,volume - then:

    python3 backtest_breakout.py --csv data/1h  --interval 1h  --walk-forward 4
    python3 backtest_breakout.py --csv data/15m --interval 15m --walk-forward 4

Where the data comes from
-------------------------
Binance Vision (data.binance.vision) publishes the same klines as the API as
static monthly and daily ZIPs. For six months of 15m bars that is ~6 requests
per symbol instead of ~12 paged API calls, it is not rate-limited, and it
needs no API key. Monthly archives only exist for COMPLETED months, so the
current month is assembled from daily files, and anything still missing (the
last day or two) is topped up from the REST API.

Integrity, because a backtest on bad candles is worse than no backtest:
  - each archive's published SHA256 checksum is verified when available
  - rows are keyed by open_time, so overlapping sources cannot double-count
  - the result is sorted, and any gap larger than one bar is reported
  - rows failing high >= max(open,close) or low <= min(open,close) are
    dropped and counted, since a corrupt bar can invent a breakout
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

VISION = "https://data.binance.vision/data"
FAPI = "https://fapi.binance.com/fapi/v1/klines"
EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# exchangeInfo has been seen to carry a promotional listing whose symbol
# contains CJK characters. Interpolated into a URL that makes urllib raise
# UnicodeEncodeError before any request goes out, so it is filtered at the
# source rather than handled at every call site.
TICKER = re.compile(r"[A-Z0-9]{2,20}\Z")


def safe(text: str) -> str:
    """Render text a Windows cp1252 console can print without raising."""
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "backslashreplace").decode("ascii")
MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
      "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}

# open_time, open, high, low, close, volume, close_time, ...
Row = Tuple[int, float, float, float, float, float]


def _get(url: str, timeout: int = 60) -> Optional[bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None                     # archive not published yet
        raise
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"\ncould not reach {url.split('/')[2]}: {e}\n"
            "If this is a 403 on CONNECT, the machine you are on blocks Binance "
            "(many cloud sandboxes and CI runners do). Run this from your own\n"
            "machine or the Railway container.")


def _parse_zip(blob: bytes, market: str) -> List[Row]:
    out: List[Row] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8"):
                parts = line.strip().split(",")
                if len(parts) < 6:
                    continue
                # Archives from 2025 onward carry a header row; older ones do not.
                if not parts[0].lstrip("-").isdigit():
                    continue
                ts = int(parts[0])
                # Some 2025+ archives publish open_time in MICROseconds.
                if ts > 1e15:
                    ts //= 1000
                out.append((ts, float(parts[1]), float(parts[2]), float(parts[3]),
                            float(parts[4]), float(parts[5])))
    return out


def _verify(blob: bytes, checksum: Optional[bytes], label: str) -> bool:
    if not checksum:
        return True                          # no checksum published; accept
    want = checksum.decode().split()[0].strip().lower()
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        print(f"\n  CHECKSUM MISMATCH on {label} - discarding this archive")
        return False
    return True


def _archive(market: str, freq: str, symbol: str, interval: str, stamp: str):
    q = urllib.parse.quote(symbol, safe="")
    base = (f"{VISION}/{market}/{freq}/klines/{q}/{interval}/"
            f"{q}-{interval}-{stamp}.zip")
    blob = _get(base)
    if blob is None:
        return None
    try:
        chk = _get(base + ".CHECKSUM")
    except Exception:  # noqa: BLE001 - a missing checksum must not stop the download
        chk = None
    if not _verify(blob, chk, f"{symbol} {interval} {stamp}"):
        return None
    return _parse_zip(blob, market)


def _api_topup(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[Row]:
    step = MS[interval]
    out: List[Row] = []
    cursor = start_ms
    while cursor < end_ms:
        url = f"{FAPI}?symbol={symbol}&interval={interval}&startTime={cursor}&limit=1500"
        raw = _get(url, timeout=30)
        if not raw:
            break
        rows = json.loads(raw.decode())
        if not rows:
            break
        for r in rows:
            out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                        float(r[4]), float(r[5])))
        cursor = int(rows[-1][0]) + step
        time.sleep(0.25)
    return out


def month_stamps(months: float) -> Tuple[List[str], List[str]]:
    """(complete months as YYYY-MM, days of the current month as YYYY-MM-DD)."""
    now = datetime.now(timezone.utc)
    first_of_now = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stamps = []
    cursor = first_of_now
    for _ in range(max(1, int(months))):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        stamps.append(cursor.strftime("%Y-%m"))
    stamps.reverse()
    days = []
    d = first_of_now
    while d < now.replace(hour=0, minute=0, second=0, microsecond=0):
        days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return stamps, days


def usdt_perp_universe(limit: int = 0) -> List[str]:
    """Every actively trading USDT perpetual on Binance USD-M.

    Cross-sectional strategies live or die on breadth: four majors that
    correlate 0.76 are worth about 1.5 independent names, and no amount of
    signal work fixes that. This is how the universe gets wide enough for
    the Fundamental Law to have something to multiply.

    Only PERPETUAL contracts with status TRADING and a USDT quote are
    returned - dated futures roll and delist, which would put survivorship
    holes in the middle of a panel.
    """
    raw = _get(EXCHANGE_INFO, timeout=60)
    if not raw:
        raise SystemExit(
            "could not read exchangeInfo from Binance. If this is a 403 on "
            "CONNECT, the machine is blocked from Binance - run it locally.")
    try:
        info = json.loads(raw.decode())
    except ValueError as e:
        raise SystemExit(f"exchangeInfo did not parse as JSON: {e}")
    raw = [s["symbol"] for s in info.get("symbols", [])
           if s.get("status") == "TRADING"
           and s.get("quoteAsset") == "USDT"
           and s.get("contractType") == "PERPETUAL"]
    syms = sorted(s for s in raw if TICKER.match(s))
    dropped = [safe(s) for s in raw if not TICKER.match(s)]
    if dropped:
        print(f"  dropped {len(dropped)} listing(s) whose symbol is not a "
              f"plain ticker: {', '.join(dropped[:5])}")
    if not syms:
        raise SystemExit("exchangeInfo returned no trading USDT perpetuals")
    return syms[:limit] if limit else syms


def sane(r: Row) -> bool:
    _, o, h, l, c, v = r
    if min(o, h, l, c) <= 0 or v < 0:
        return False
    return h >= max(o, c) and l <= min(o, c) and h >= l


def download(symbol: str, interval: str, months: float, market: str = "futures/um"
             ) -> Tuple[List[Row], Dict[str, int]]:
    stamps, days = month_stamps(months)
    rows: Dict[int, Row] = {}
    for stamp in stamps:
        got = _archive(market, "monthly", symbol, interval, stamp)
        if got:
            for r in got:
                rows[r[0]] = r
        print(f"\r  {symbol} {interval}: {len(rows)} bars (monthly {stamp})   ",
              end="", flush=True)
    for day in days:
        got = _archive(market, "daily", symbol, interval, day)
        if got:
            for r in got:
                rows[r[0]] = r
        print(f"\r  {symbol} {interval}: {len(rows)} bars (daily {day})   ",
              end="", flush=True)

    step = MS[interval]
    now_ms = int(time.time() * 1000)
    if rows:
        last = max(rows)
        if now_ms - last > 2 * step:
            for r in _api_topup(symbol, interval, last + step, now_ms):
                rows[r[0]] = r
    else:
        start = now_ms - int(months * 30.44 * 86_400_000)
        for r in _api_topup(symbol, interval, start, now_ms):
            rows[r[0]] = r
    print(f"\r  {symbol} {interval}: {len(rows)} bars (complete)            ")

    ordered = [rows[k] for k in sorted(rows)]
    bad = [r for r in ordered if not sane(r)]
    ordered = [r for r in ordered if sane(r)]
    gaps = 0
    for a, b in zip(ordered, ordered[1:]):
        if b[0] - a[0] > step:
            gaps += (b[0] - a[0]) // step - 1
    return ordered, {"bars": len(ordered), "dropped": len(bad), "missing": int(gaps)}


def write_csv(path: str, rows: List[Row]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for ts, o, h, l, c, v in rows:
            w.writerow([ts, o, h, l, c, v])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SOLUSDT,BTCUSDT,ETHUSDT,NEARUSDT")
    ap.add_argument("--universe", default=None,
                    help="'all' fetches every trading USDT perpetual - the "
                         "breadth a cross-sectional book needs")
    ap.add_argument("--universe-limit", type=int, default=0,
                    help="cap the universe size (0 = no cap)")
    ap.add_argument("--intervals", default="1h,15m")
    ap.add_argument("--months", type=float, default=6.0)
    ap.add_argument("--out", default="data")
    ap.add_argument("--market", default="futures/um", choices=["futures/um", "spot"],
                    help="futures/um matches what the bot trades; use spot only "
                         "if you know why you want it")
    a = ap.parse_args(argv)

    if a.universe == "all":
        symbols = usdt_perp_universe(a.universe_limit)
        print(f"universe: {len(symbols)} trading USDT perpetuals")
        with open(os.path.join(a.out, "universe.txt") if a.out else "universe.txt",
                  "w", encoding="utf-8") as fh:
            os.makedirs(a.out, exist_ok=True)
            fh.write("\n".join(symbols) + "\n")
    else:
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    intervals = [s.strip() for s in a.intervals.split(",") if s.strip()]
    for iv in intervals:
        if iv not in MS:
            raise SystemExit(f"unsupported interval {iv!r}; use one of {sorted(MS)}")

    print(f"=== downloading {a.months:g} months of {a.market} klines ===")
    report = []
    failed = []
    for iv in intervals:
        for sym in symbols:
            if not TICKER.match(sym):
                failed.append(f"{safe(sym)} ({iv})")
                print(f"  {safe(sym)} {iv}: not a plain ticker - skipped")
                continue
            try:
                rows, st = download(sym, iv, a.months, a.market)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 - one symbol, not the run
                failed.append(f"{safe(sym)} ({iv})")
                print(f"  {safe(sym)} {iv}: failed - {safe(str(e))[:60]}")
                continue
            if not rows:
                print(f"  {sym} {iv}: NO DATA - check the symbol exists on this market")
                continue
            path = os.path.join(a.out, iv, f"{sym}.csv")
            write_csv(path, rows)
            span = (rows[-1][0] - rows[0][0]) / 86_400_000
            first = datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc)
            last = datetime.fromtimestamp(rows[-1][0] / 1000, timezone.utc)
            report.append((sym, iv, st, span, first, last, path))

    if failed:
        print(f"\n  {len(failed)} symbol(s) skipped or failed: "
              f"{', '.join(failed[:8])}{' ...' if len(failed) > 8 else ''}")
    print("\n=== summary ===")
    print(f"{'symbol':>9s} {'iv':>4s} {'bars':>7s} {'days':>6s} "
          f"{'missing':>8s} {'dropped':>8s}  range")
    for sym, iv, st, span, first, last, path in report:
        flag = ""
        if st["missing"] > st["bars"] * 0.01:
            flag = "  <-- >1% of bars missing"
        print(f"{sym:>9s} {iv:>4s} {st['bars']:7d} {span:6.0f} "
              f"{st['missing']:8d} {st['dropped']:8d}  "
              f"{first:%Y-%m-%d} to {last:%Y-%m-%d}{flag}")

    if report:
        print("\n=== next steps ===")
        for iv in intervals:
            print(f"  python3 backtest_breakout.py --csv {a.out}/{iv} "
                  f"--interval {iv} --walk-forward 4 --json wf_{iv}.json")
        print("\n  Read the WALK-FORWARD block, not the in-sample one. Then feed")
        print("  its win rate, avg win and avg loss into risk_simulator.py with")
        print("  --observed-trades set to the walk-forward trade count.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
