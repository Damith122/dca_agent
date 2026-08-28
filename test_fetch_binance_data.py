#!/usr/bin/env python3
"""Tests for the Binance Vision downloader.

No network is touched: _get is replaced with a stub that serves synthetic
archives, so the parsing, checksum, dedup and validation logic is exercised
end to end - including the round trip into backtest_breakout's CSV reader,
because a downloader that produces files the backtest silently mis-parses is
worse than one that fails loudly.
"""
import csv
import hashlib
import io
import os
import shutil
import sys
import tempfile
import zipfile

import fetch_binance_data as F

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


def make_zip(rows, header=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        lines = []
        if header:
            lines.append("open_time,open,high,low,close,volume,close_time,x,y,z,w,v")
        for ts, o, h, l, c, v in rows:
            lines.append(f"{ts},{o},{h},{l},{c},{v},{ts + 1},0,0,0,0,0")
        z.writestr("k.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


BAR = 3_600_000
BASE = 1_700_000_000_000 // BAR * BAR


def bars(n, start=BASE, price=100.0):
    return [(start + i * BAR, price, price * 1.01, price * 0.99, price, 10.0)
            for i in range(n)]


print("[1] Archive parsing")
blob = make_zip(bars(5))
rows = F._parse_zip(blob, "futures/um")
check("a headerless archive parses", len(rows) == 5, str(len(rows)))
rows_h = F._parse_zip(make_zip(bars(5), header=True), "futures/um")
check("an archive WITH a header row parses to the same bars", rows_h == rows,
      f"{len(rows_h)} vs {len(rows)}")
micro = [(t * 1000, o, h, l, c, v) for t, o, h, l, c, v in bars(3)]
check("microsecond open_time is normalised to milliseconds",
      all(r[0] < 1e15 for r in F._parse_zip(make_zip(micro), "futures/um")))

print("\n[2] Checksums")
good = hashlib.sha256(blob).hexdigest().encode() + b"  file.zip"
check("a matching checksum passes", F._verify(blob, good, "x") is True)
check("a mismatched checksum is rejected",
      F._verify(blob, b"deadbeef  file.zip", "x") is False)
check("a missing checksum is accepted rather than blocking the download",
      F._verify(blob, None, "x") is True)

print("\n[3] Corrupt bars are dropped, not traded")
check("a bar with high below its close is rejected",
      not F.sane((BASE, 100.0, 99.0, 98.0, 100.5, 1.0)))
check("a bar with low above its open is rejected",
      not F.sane((BASE, 100.0, 101.0, 100.5, 100.2, 1.0)))
check("a zero price is rejected", not F.sane((BASE, 0.0, 1.0, 0.0, 0.5, 1.0)))
check("negative volume is rejected", not F.sane((BASE, 100, 101, 99, 100, -1.0)))
check("a normal bar is kept", F.sane((BASE, 100.0, 101.0, 99.0, 100.5, 1.0)))

print("\n[4] Assembly: overlap, gaps, ordering")
served = {}


def fake_get(url, timeout=60):
    if url.endswith(".CHECKSUM"):
        body = served.get(url[:-9])
        return (hashlib.sha256(body).hexdigest().encode() + b"  f.zip") if body else None
    return served.get(url)


F._get = fake_get


def vision_url(sym, iv, freq, stamp):
    return (f"{F.VISION}/futures/um/{freq}/klines/{sym}/{iv}/"
            f"{sym}-{iv}-{stamp}.zip")


months, days = F.month_stamps(2)
# Two archives that OVERLAP by 3 bars, plus one corrupt bar in the second.
first = bars(10)
second = bars(10, start=BASE + 7 * BAR)
second[4] = (second[4][0], 100.0, 99.0, 98.0, 100.5, 1.0)     # impossible bar
served[vision_url("TESTUSDT", "1h", "monthly", months[0])] = make_zip(first)
served[vision_url("TESTUSDT", "1h", "monthly", months[1])] = make_zip(second)
F._api_topup = lambda *a, **k: []
rows, st = F.download("TESTUSDT", "1h", 2)
check("overlapping archives are de-duplicated by open_time",
      st["bars"] == 16, f"{st['bars']} bars (expected 17 minus 1 corrupt)")
check("the corrupt bar is dropped and counted", st["dropped"] == 1, str(st["dropped"]))
check("output is sorted by time", all(a[0] < b[0] for a, b in zip(rows, rows[1:])))
check("no gaps are reported for contiguous data", st["missing"] <= 1,
      str(st["missing"]))

served.clear()
gapped = bars(5) + bars(5, start=BASE + 20 * BAR)
served[vision_url("GAPUSDT", "1h", "monthly", months[0])] = make_zip(gapped)
_, stg = F.download("GAPUSDT", "1h", 2)
check("a real gap is counted", stg["missing"] == 15, str(stg["missing"]))

print("\n[5] A missing archive is not fatal")
served.clear()
served[vision_url("PARTUSDT", "1h", "monthly", months[1])] = make_zip(bars(6))
rows_p, st_p = F.download("PARTUSDT", "1h", 2)
check("one 404 month still yields the other month's bars", st_p["bars"] == 6,
      str(st_p["bars"]))

print("\n[6] The CSV is exactly what backtest_breakout reads")
tmp = tempfile.mkdtemp(prefix="fetch_test_")
try:
    path = os.path.join(tmp, "1h", "TESTUSDT.csv")
    F.write_csv(path, bars(40))
    with open(path, newline="", encoding="utf-8") as fh:
        head = next(csv.reader(fh))
    check("the header is the documented column order",
          head == ["ts", "open", "high", "low", "close", "volume"], str(head))

    from backtest_breakout import load_csv
    candles = load_csv(os.path.join(tmp, "1h"), "TESTUSDT")
    check("backtest_breakout reads it back", len(candles) == 40, str(len(candles)))
    check("...skipping the header rather than parsing it as a bar",
          all(c.open > 0 for c in candles))
    check("...and converting ms timestamps to seconds",
          candles[0].ts < 1e11, str(candles[0].ts))
    check("...preserving OHLC",
          abs(candles[0].high - 101.0) < 1e-9 and abs(candles[0].low - 99.0) < 1e-9)

    from breakout import BreakoutParams, run
    trades, curve = run(candles, BreakoutParams(channel=5, atr_period=3))
    check("the engine runs on downloaded candles without raising",
          isinstance(trades, list) and len(curve) >= len(candles))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n[7] Month arithmetic")
m6, d6 = F.month_stamps(6)
check("six months requests six complete monthly archives", len(m6) == 6, str(len(m6)))
check("months are ordered oldest first", m6 == sorted(m6), str(m6))
check("the current month is covered by daily files instead",
      all(len(x) == 10 for x in d6) if d6 else True)
check("months never include the current, incomplete month",
      all(not x.startswith(F.datetime.now(F.timezone.utc).strftime("%Y-%m"))
          for x in m6), str(m6))

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
