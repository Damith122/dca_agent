#!/usr/bin/env python3
"""Every name a module references at module scope must actually resolve.

A missing function is a RUNTIME error, not a syntax error, so `ast.parse`
and `python -c "import x"` both pass a file that will explode the moment a
particular branch runs. That is exactly how fetch_binance_data.py shipped
calling usdt_perp_universe() without defining it: a patch anchored on a
function name that lived in a different file, a replace that silently did
nothing, and an AST check that was happy either way.

This walks each module's syntax tree and resolves every name that is LOADED
against the names that module actually has - its own definitions, its
imports, its builtins - so the gap is caught at test time rather than by a
user running a command-line flag for the first time.
"""
import ast
import builtins
import sys

MODULES = [
    "fetch_binance_data.py", "backtest_breakout.py", "breakout.py",
    "backtest_funding.py", "funding_arb.py", "backtest_stat_arb.py",
    "stat_arb.py", "cross_sectional.py", "backtest_cross_sectional.py",
    "train_ml_model.py", "ml_features.py", "risk_simulator.py",
    "edge_requirements.py", "analyze_feature_log.py", "retention.py",
    "optimise_cross_sectional.py", "fetch_funding_universe.py",
    "backtest_xs_funding.py", "ml_features.py", "diagnose_live.py",
    "exchange.py", "dry_fills.py", "tsmom.py", "backtest_tsmom.py",
    "paper_tsmom.py", "intraday.py", "backtest_intraday.py",
    "backtest_intraday_family.py",
]

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


def module_names(tree):
    """Everything the module defines or imports at any scope."""
    names = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
            for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                names.add(a.arg)
            if node.args.vararg:
                names.add(node.args.vararg.arg)
            if node.args.kwarg:
                names.add(node.args.kwarg.arg)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.Lambda):
            for a in (node.args.args + node.args.kwonlyargs
                      + node.args.posonlyargs):
                names.add(a.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            names.update(node.names)
    return names


print("[1] Every referenced name resolves")
for mod in MODULES:
    try:
        src = open(mod, encoding="utf-8").read()
    except FileNotFoundError:
        check(f"{mod}: present", False, "file missing")
        continue
    tree = ast.parse(src)
    known = module_names(tree)
    missing = sorted({n.id for n in ast.walk(tree)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                      and n.id not in known})
    check(f"{mod}: every name is defined", not missing,
          ", ".join(missing[:6]))

print("\n[2] Every module actually imports")
for mod in MODULES:
    name = mod[:-3]
    try:
        __import__(name)
        check(f"{name}: imports cleanly", True)
    except FileNotFoundError:
        check(f"{name}: imports cleanly", False, "file missing")
    except Exception as e:  # noqa: BLE001
        check(f"{name}: imports cleanly", False, f"{type(e).__name__}: {e}")

print("\n[3] Regression: the flag that shipped broken")
import fetch_binance_data as F
check("usdt_perp_universe exists", callable(F.usdt_perp_universe))
check("...and json is available at module scope, not only inside a function",
      hasattr(F, "json"))
check("--universe and --universe-limit are wired to it",
      "--universe" in open("fetch_binance_data.py", encoding="utf-8").read()
      and "usdt_perp_universe(a.universe_limit)"
      in open("fetch_binance_data.py", encoding="utf-8").read())

print("\n" + "=" * 74)
print(f"  {passed} passed, {failed} failed")
print("=" * 74)
sys.exit(1 if failed else 0)
