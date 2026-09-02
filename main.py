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
import os
import sys
import time

# ============================================================================
# BOOT BEACON (2026-08-21)
#
# A deploy once reached "Starting Container" and then produced no output at
# all - no banner, no traceback - which made it impossible to tell apart
# three very different failures: a hang during import, a hang before the
# first print, or a healthy process whose stdout was never collected.
#
# These beacons remove that ambiguity. They bracket every step that runs
# before the banner, and each one is written to BOTH stdout and stderr with
# an explicit flush. Duplicating the stream matters: if only one of the two
# is being captured by the host, the other still gets through, so silence
# now means "the process is not running" rather than "we cannot tell".
#
# Deliberately dependency-free - this must work before any project module
# is imported, which is exactly when the hard-to-diagnose failures happen.
# ============================================================================

_BOOT_T0 = time.monotonic()


def _boot(message: str) -> None:
    line = f"[boot +{time.monotonic() - _BOOT_T0:6.2f}s] {message}"
    for stream in (sys.stdout, sys.stderr):
        try:
            print(line, file=stream, flush=True)
        except Exception:  # noqa: BLE001 - a broken stream must never stop boot
            pass


_boot(
    f"main.py entered - python {sys.version.split()[0]}, pid {os.getpid()}, "
    f"cwd {os.getcwd()}"
)
_boot("importing dca2 (loads config, trading, brain, sklearn) ...")

from dca2 import run_forever, color, YELLOW

_boot("imports complete - handing control to run_forever()")

if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except (KeyboardInterrupt, SystemExit):
        print(color("\n[shutdown] stopped.", YELLOW))
