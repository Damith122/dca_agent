#!/usr/bin/env python3
"""
================================================================================
 Main entrypoint - simply starts the bot.

 This file contains no bot logic of its own. It imports run_forever() (the
 24/7 supervisor loop) from dca2.py and runs it with the exact same
 asyncio.run(...) + KeyboardInterrupt/SystemExit handling as dca2.py's own
 `if __name__ == "__main__":` guard.

 dca2.py's own entrypoint guard is untouched, so `python dca2.py` still
 works exactly as before - Railway's existing start command does not need
 to change. This file is an additional, equivalent entrypoint: if you point
 Railway's start command at `python main.py` instead, behavior is identical,
 since both ultimately just call the same run_forever().
================================================================================
"""

import asyncio

from dca2 import run_forever, color, YELLOW

if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except (KeyboardInterrupt, SystemExit):
        print(color("\n[shutdown] stopped.", YELLOW))
