"""Disk retention for feature-recorder shards.

Why this exists
---------------
The recorder writes one JSONL shard per symbol per hour and never deletes
it. At the measured rate - roughly 400 KB per shard, four symbols - that is
~38 MB/day of local disk and the same again in the GitHub branch. Over a
24-hour research run that is fine. Left running for a fortnight it fills an
ephemeral container's disk allowance, at which point writes start failing
while the bot is holding real positions.

Design rules, in priority order:

  1. NEVER delete a shard that has not been confirmed uploaded. Losing
     recorded data to a disk-tidying routine would be far worse than
     running out of disk, because the disk problem is visible and this one
     is silent. `uploaded` is the caller's set of confirmed-pushed paths.
  2. NEVER delete the active shard. The caller passes only completed
     shards (FeatureRecorder.completed_shards() already excludes it).
  3. Age first, budget second. Age is predictable; the budget sweep is the
     safety net for a run that produces more than expected.
  4. If the budget cannot be met using eligible shards alone, report it
     rather than reaching for ineligible ones. An operator seeing
     "over budget, N shards not yet uploaded" can act; a routine that
     quietly deleted them cannot be undone.

Everything here is pure filesystem work with injectable clock and unlink,
so it is testable without touching real files, and every call is wrapped by
the caller so a retention failure can never interrupt trading.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Dict, Iterable, List, Optional, Set


def prune_shards(
    shards: Iterable[str],
    *,
    uploaded: Set[str],
    retain_hours: float,
    max_bytes: float,
    require_upload: bool = True,
    now: Optional[float] = None,
    unlink: Callable[[str], None] = os.unlink,
    getsize: Callable[[str], int] = os.path.getsize,
    getmtime: Callable[[str], float] = os.path.getmtime,
) -> Dict[str, object]:
    """Delete old, already-uploaded shards. Returns a report, never raises
    for an individual file - one unreadable shard must not stop the sweep.

    `retain_hours <= 0` disables the age rule; `max_bytes <= 0` disables the
    budget rule. With both disabled nothing is ever deleted.
    """
    now = time.time() if now is None else now
    info = []
    for path in shards:
        try:
            info.append({"path": path, "size": getsize(path), "mtime": getmtime(path)})
        except OSError:
            continue                      # vanished or unreadable - skip silently
    info.sort(key=lambda e: e["mtime"])    # oldest first

    def eligible(entry) -> bool:
        return (not require_upload) or entry["path"] in uploaded

    deleted: List[str] = []
    freed = 0

    def drop(entry) -> bool:
        nonlocal freed
        try:
            unlink(entry["path"])
        except OSError:
            return False
        deleted.append(entry["path"])
        freed += entry["size"]
        entry["gone"] = True
        return True

    # Rule 3a - age.
    if retain_hours > 0:
        cutoff = now - retain_hours * 3600.0
        for entry in info:
            if entry["mtime"] < cutoff and eligible(entry):
                drop(entry)

    # Rule 3b - budget, oldest first, eligible only.
    live = [e for e in info if not e.get("gone")]
    total = sum(e["size"] for e in live)
    blocked = 0
    if max_bytes > 0 and total > max_bytes:
        for entry in live:
            if total <= max_bytes:
                break
            if not eligible(entry):
                blocked += 1
                continue
            size = entry["size"]
            if drop(entry):
                total -= size

    return {
        "deleted": deleted,
        "freed_bytes": freed,
        "retained_bytes": total,
        "retained_files": sum(1 for e in info if not e.get("gone")),
        "over_budget": bool(max_bytes > 0 and total > max_bytes),
        "blocked_unuploaded": blocked,
    }


def describe(report: Dict[str, object]) -> str:
    """One-line human summary for the deploy log. Returns "" when the sweep
    did nothing and nothing is wrong, so a healthy bot stays quiet."""
    deleted = report["deleted"]                       # type: ignore[index]
    if not deleted and not report["over_budget"]:     # type: ignore[index]
        return ""
    parts = []
    if deleted:
        parts.append(f"pruned {len(deleted)} shard(s), "
                     f"freed {report['freed_bytes'] / 1048576:.1f} MB")
    parts.append(f"{report['retained_files']} kept "
                 f"({report['retained_bytes'] / 1048576:.1f} MB)")
    if report["over_budget"]:                         # type: ignore[index]
        parts.append(f"STILL OVER BUDGET - {report['blocked_unuploaded']} shard(s) "
                     f"not yet uploaded and will not be deleted")
    return "; ".join(parts)
