"""Run a bounded, keyless paper experiment: python paper_validation.py --minutes 60.

This entry point cannot enable live execution, even if inherited Railway
variables request it. Each process gets fresh paper state and starting cash.
"""
import argparse
import ast
import asyncio
import os
from pathlib import Path


def paper_environment(environ):
    root = Path(__file__).resolve().parent
    result = dict(environ)
    # Do not inherit hidden live overrides. Read names without importing
    # config (which freezes configuration and state paths at import time).
    tree = ast.parse((root / "config.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else ""
        is_env_get = (isinstance(node.func, ast.Attribute)
                      and isinstance(node.func.value, ast.Attribute)
                      and ast.unparse(node.func.value) == "os.environ")
        arg = node.args[0]
        if (name.startswith("_env_") or is_env_get) and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            result.pop(arg.value, None)
    for line in (root / "paper_validation.env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    # Hard boundary, independent of profile edits.
    result.update(DRY_RUN="true", USE_TESTNET="false", LIVE_TRADING_CONFIRMATION="false",
                  I_UNDERSTAND_THIS_IS_REAL_MONEY="no", BINANCE_API_KEY="", BINANCE_API_SECRET="",
                  GITHUB_TOKEN="", GITHUB_REPO="")
    return result


async def experiment(minutes):
    import dca2  # imported only after the forced paper environment is set
    try:
        await asyncio.wait_for(dca2.main(), timeout=minutes * 60)
    except asyncio.TimeoutError:
        print("[paper-validation] experiment time limit reached; stopped, no live orders sent")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.minutes <= 1440:
        parser.error("--minutes must be 1..1440")
    environment = paper_environment(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    asyncio.run(experiment(args.minutes))


if __name__ == "__main__":
    main()
