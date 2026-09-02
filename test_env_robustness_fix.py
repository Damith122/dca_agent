#!/usr/bin/env python3
"""
Regression suite for the 2026-08-21 robust environment-parsing fix.

Background: every tunable used to be read with a bare
float(os.environ.get(NAME, "default")) / int(...) conversion. An EMPTY or
malformed variable therefore raised ValueError during `import config` -
before main() ran, before the banner printed, and so with no log line
explaining anything. Clearing a variable in Railway is exactly this case:
os.environ.get returns "" rather than None, the default is skipped, and
float("") explodes.

These tests pin the new contract:
  * missing / empty / whitespace / malformed  ->  code default, never raise
  * a fallback is always REPORTED, never silent
  * the fail-closed real-money gate stays strict
"""
import os
import re
import subprocess
import sys

passed = failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def boot(env_overrides, code="import config, trading, dca2"):
    env = dict(os.environ)
    env.update(BINANCE_API_KEY="d", BINANCE_API_SECRET="d",
               SYMBOL="SOLUSDT", ACTIVE_SYMBOLS="SOLUSDT")
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env)


def all_env_names():
    names = set()
    for f in ("config.py", "trading.py", "dca2.py", "exchange.py",
              "websocket.py", "brain.py", "github_sync.py"):
        src = open(f).read()
        names |= set(re.findall(r'_env_(?:float|int|bool|str|raw)\(\s*"([^"]+)"', src))
        names |= set(re.findall(r'_notional_scaled\(\s*"([^"]+)"', src))
        names |= set(re.findall(r'os\.environ\.get\(\s*"([^"]+)"', src))
        names |= set(re.findall(r'os\.getenv\(\s*"([^"]+)"', src))
    # The real-money gate is deliberately excluded: it must fail CLOSED on a
    # blank value, which is the opposite of falling back to a default.
    return sorted(names - {"I_UNDERSTAND_THIS_IS_REAL_MONEY"})


print("\n[1] No bare env conversions survive anywhere")
for f in ("config.py", "trading.py", "dca2.py", "exchange.py",
          "websocket.py", "brain.py", "github_sync.py"):
    src = open(f).read()
    # strip comments so the explanatory header does not count as a hit
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    raw = re.findall(r'(?:float|int)\(\s*\n?\s*os\.environ\.get', body)
    check(f"{f} has no bare float()/int() around os.environ.get", not raw)

body = "\n".join(l for l in open("config.py").read().splitlines()
                 if not l.lstrip().startswith("#"))
lower = re.findall(r'os\.environ\.get\([^)]*\)\s*\.lower\(\)', body)
check("the only surviving .lower() idiom is the real-money gate",
      len(lower) == 1)
check("...and it is I_UNDERSTAND_THIS_IS_REAL_MONEY",
      "I_UNDERSTAND_THIS_IS_REAL_MONEY" in body[:body.find(lower[0])][-400:]
      if lower else False)

names = all_env_names()
print(f"\n[2] All {len(names)} tunables survive every malformed form")
FORMS = [("empty", ""), ("whitespace", "   "), ("garbage", "not-a-number"),
         ("NaN", "nan"), ("inf", "inf"), ("-inf", "-inf"), ("overflow", "1e400"),
         ("negative", "-1"), ("zero", "0"), ("unicode", "２"),
         ("newline", "\n"), ("json", "{}"), ("typo-bool", "flase"),
         ("huge-int", "9" * 40)]
for label, val in FORMS:
    r = boot({n: val for n in names})
    check(f"every variable = {label!r} still imports",
          r.returncode == 0)

print("\n[3] Blank means 'use the default', not 'crash'")
r = boot({"MIN_STOP_LOSS_USD": "", "PROFIT_LOCK_SLIPPAGE_ATR_MULT": "",
          "SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC": "",
          "ORDERBOOK_IMBALANCE_THRESHOLD": "", "COOL_OFF_PERIOD_MINUTES": ""},
         code=("import config; "
               "print(config.PROFIT_LOCK_SLIPPAGE_ATR_MULT, "
               "config.SMART_ORDERFLOW_EXIT_MIN_HOLD_SEC)"))
check("the five previously-fatal blanks now import", r.returncode == 0)
check("...and resolve to their code defaults (0.25 / 50)",
      r.stdout.strip() == "0.25 50.0")

print("\n[4] A blank is silent; a MALFORMED value is reported")
r = boot({"PROFIT_LOCK_SLIPPAGE_ATR_MULT": ""},
         code="import config; print(len(config.env_parse_warnings()))")
check("a blank produces no warning (it is a legitimate 'unset')",
      r.stdout.strip() == "0")

r = boot({"LEVERAGE": "twenty"},
         code="import config; print('|'.join(config.env_parse_warnings()))")
check("a malformed value IS reported", "LEVERAGE" in r.stdout)
check("...naming the offending value", "'twenty'" in r.stdout)
check("...and the safe default it fell back to", "5" in r.stdout)

r = boot({"DYNAMIC_TP_ENABLED": "flase"},
         code="import config; print(config.DYNAMIC_TP_ENABLED, "
              "'|'.join(config.env_parse_warnings()))")
check("a typo'd boolean no longer reads as True silently",
      "DYNAMIC_TP_ENABLED" in r.stdout)
check("...and falls back to the default rather than False",
      r.stdout.startswith("True"))

print("\n[5] Valid values are still honoured exactly")
r = boot({"INITIAL_ENTRY_USDT": "2", "LEVERAGE": "20"},
         code="import config; print(config.INITIAL_ENTRY_USDT, "
              "config.ENTRY_NOTIONAL_USDT, len(config.env_parse_warnings()))")
check("a valid float is used verbatim", r.stdout.split()[0] == "2.0")
check("...and unsafe leverage is capped before derived sizing",
      r.stdout.split()[1] == "20.0")
check("...with no spurious warning", r.stdout.split()[2] == "0")

r = boot({"MAX_DCA_STEPS": "3.0"},
         code="import config; print(config.MAX_DCA_STEPS)")
check("an int knob accepts '3.0' and the DCA safety cap still wins",
      r.stdout.strip() == "1")

for raw, want in (("true", "True"), ("1", "True"), ("yes", "True"),
                  ("on", "True"), ("false", "False"), ("0", "False"),
                  ("no", "False"), ("off", "False")):
    r = boot({"DYNAMIC_TP_ENABLED": raw},
             code="import config; print(config.DYNAMIC_TP_ENABLED)")
    check(f"bool spelling {raw!r} -> {want}", r.stdout.strip() == want)

print("\n[6] The real-money gate still fails CLOSED")
for blank in ("", "   ", "true", "1", "YES!"):
    r = boot({"I_UNDERSTAND_THIS_IS_REAL_MONEY": blank},
             code="import config; print(config.I_UNDERSTAND_THIS_IS_REAL_MONEY)")
    check(f"I_UNDERSTAND_THIS_IS_REAL_MONEY={blank!r} does NOT unlock mainnet",
          r.stdout.strip() == "False")
r = boot({"I_UNDERSTAND_THIS_IS_REAL_MONEY": "yes"},
         code="import config; print(config.I_UNDERSTAND_THIS_IS_REAL_MONEY)")
check("...and the literal 'yes' still does", r.stdout.strip() == "True")

print("\n[7] The boot beacon brackets every pre-banner step")
src = open("main.py").read()
check("main.py emits a beacon before importing dca2",
      src.index("importing dca2") < src.index("from dca2 import"))
check("...and one after imports complete",
      src.index("from dca2 import") < src.index("imports complete"))
check("beacons write to stdout AND stderr",
      "for stream in (sys.stdout, sys.stderr)" in src)
check("...with an explicit flush", "flush=True" in src)
check("...and cannot themselves abort boot", "except Exception" in src)

r = subprocess.run([sys.executable, "-u", "main.py"],
                   capture_output=True, text=True, timeout=60,
                   env={**os.environ, "BINANCE_API_KEY": "", "BINANCE_API_SECRET": "",
                        "DRY_RUN": "false", "SYMBOL": "SOLUSDT", "ACTIVE_SYMBOLS": "SOLUSDT"})
check("beacons appear on stdout", "[boot" in r.stdout)
check("...and independently on stderr", "[boot" in r.stderr)
check("a missing-key exit still reports itself rather than hanging silently",
      "BINANCE_API_KEY" in (r.stdout + r.stderr))

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
