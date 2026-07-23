#!/usr/bin/env python3
"""
================================================================================
 GitHub sync code - moved out of dca2.py

 This file contains ONLY what was relocated out of dca2.py's "CLOUD-SYNC
 BRAIN (push/pull brain snapshot to GitHub across ephemeral restarts)"
 section: the GithubBrainSync class. Every method, formula, and fail-soft
 error-handling branch is unchanged from the original dca2.py source -
 nothing was fixed, renamed, or optimized.

 This module is self-contained: GithubBrainSync doesn't touch any trading
 state (no PositionState, no MartingaleManager) - it only depends on
 stdlib (base64, typing) + aiohttp. It does not import anything from
 dca2.py, config.py, indicators.py, brain.py, exchange.py, or websocket.py.

 One structural note on the move (not a logic change): GithubBrainSync's
 methods call color()/YELLOW for logging, which live in dca2.py's UTIL
 section. Importing them back from dca2.py would create a circular import
 (dca2.py imports GithubBrainSync from here). To keep this module
 self-contained, it carries its own private copies of those two tiny,
 generic helpers - defined identically to dca2.py's versions - the same
 approach used in brain.py and websocket.py.
================================================================================
"""

from __future__ import annotations

import asyncio
import base64
import sys
from typing import Optional

import aiohttp

# ----------------------------------------------------------------------------
# Private helpers (identical copies of dca2.py's color()/YELLOW - duplicated
# only to avoid a circular import; see module docstring above).
# ----------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()


def color(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


GREEN, RED, YELLOW, CYAN, GRAY, BOLD, MAGENTA, BLUE = "32", "31", "33", "36", "90", "1", "35", "34"


# ============================================================================
# CLOUD-SYNC BRAIN (push/pull brain snapshot to GitHub across ephemeral
# restarts). Unchanged in behavior from the previous build - still generic
# over whatever bytes it's given.
# ============================================================================


class GithubBrainSync:
    """Best-effort sync of one or more files to a GitHub repo via the
    Contents API, all through a single shared session/client. Deliberately
    fails soft everywhere: any network/auth/API error is caught, logged,
    and swallowed - trading must never stop because GitHub is unreachable
    or misconfigured. If GITHUB_TOKEN/GITHUB_REPO aren't set, `enabled` is
    False and every method becomes a no-op, so the bot still runs fine on
    local-disk state alone (just without cross-restart persistence on a
    fully ephemeral host).

    `path` is the primary/default file (brain.pkl) - kept so existing
    callers of download()/upload() without an explicit path keep working
    unchanged. Additional files (e.g. trades_log.csv, performance_stats.csv)
    reuse this exact same session/token/repo/branch by passing their own
    `path=` to download()/upload() - no second GitHub client is ever
    created. Each path's GitHub content-API sha is tracked independently
    in `_last_sha` (keyed by path), since the Contents API requires the
    current sha of THAT file to update it."""

    def __init__(self, token: str, repo: str, path: str, branch: str):
        self.token = token
        self.repo = repo
        self.path = path
        self.branch = branch
        self.enabled = bool(token and repo)
        self.session: Optional[aiohttp.ClientSession] = None
        self._last_sha: dict = {}
        # Tri-state cache for _ensure_branch(): None = not checked yet,
        # True = confirmed to exist (or created), False = check/create
        # failed this run (upload() will still attempt and fail soft).
        # Checked/created at most once per process, not once per file.
        self._branch_ready: Optional[bool] = None
        # One asyncio.Lock per remote path (keyed the same way as
        # _last_sha). Kept for backward compatibility; no longer used to
        # gate upload() itself (see _upload_lock below), since a per-path
        # lock alone doesn't prevent two DIFFERENT files racing for the
        # same branch HEAD.
        self._path_locks: dict = {}
        # Single global lock: ALL uploads to this branch must be fully
        # serialized, not just per-path - the Contents API commits each
        # PUT against the branch's current HEAD, so two concurrent PUTs
        # to DIFFERENT files (e.g. brain.pkl + trades_log.csv) on the same
        # branch can still race for that HEAD and 409, even with correct
        # per-file shas. A per-path lock alone can't prevent that.
        self._upload_lock = asyncio.Lock()

    async def start(self) -> None:
        if not self.enabled:
            print(color(
                "[brain-sync] GITHUB_TOKEN / GITHUB_REPO not set - brain snapshot will persist "
                "locally only (lost on next ephemeral restart). Set both env vars to enable "
                "cross-restart cloud sync.", YELLOW,
            ))
            return
        if self.session is not None:
            return  # already started - reuse the existing session (no second client)
        self.session = aiohttp.ClientSession(headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    def _url(self, path: Optional[str] = None) -> str:
        p = path if path is not None else self.path
        return f"https://api.github.com/repos/{self.repo}/contents/{p}"

    def _lock_for(self, path: str) -> asyncio.Lock:
        lock = self._path_locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._path_locks[path] = lock
        return lock

    async def download(self, path: Optional[str] = None) -> Optional[bytes]:
        p = path if path is not None else self.path
        if not self.enabled or self.session is None:
            return None
        try:
            async with self.session.get(
                self._url(p), params={"ref": self.branch},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                data = await resp.json()
                self._last_sha[p] = data.get("sha")
                content_b64 = data.get("content", "")
                if not content_b64:
                    return None
                return base64.b64decode(content_b64)
        except Exception as e:  # noqa: BLE001 - sync must never take the bot down
            print(color(f"[brain-sync] GitHub download failed for {p} (continuing without it): {e}", YELLOW))
            return None

    async def _ensure_branch(self) -> None:
        """Makes sure self.branch exists on the remote before the first
        commit is pushed to it, creating it off the repo's default branch
        if needed. This is what lets GITHUB_BRANCH point at a brand-new
        dedicated runtime-state branch (e.g. "brain-state") with zero
        manual GitHub setup - the branch is created automatically on first
        use. Checked once per process (cached in self._branch_ready);
        fails soft like every other method here - if this can't confirm
        or create the branch, upload() is still attempted and will simply
        fail (and log) the same way a network error would."""
        if self._branch_ready is not None or self.session is None:
            return
        base = f"https://api.github.com/repos/{self.repo}"
        try:
            async with self.session.get(
                f"{base}/git/ref/heads/{self.branch}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    self._branch_ready = True
                    return
                if resp.status != 404:
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            # Branch doesn't exist yet - create it off the repo's default
            # branch so runtime state has somewhere to live that Railway
            # (deploying from the default/code branch) never watches.
            async with self.session.get(
                base, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                default_branch = (await resp.json()).get("default_branch", "main")
            async with self.session.get(
                f"{base}/git/ref/heads/{default_branch}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                base_sha = (await resp.json())["object"]["sha"]
            async with self.session.post(
                f"{base}/git/refs",
                json={"ref": f"refs/heads/{self.branch}", "sha": base_sha},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            print(color(
                f"[brain-sync] created dedicated runtime-state branch "
                f"'{self.branch}' off '{default_branch}' (one-time setup).", YELLOW,
            ))
            self._branch_ready = True
        except Exception as e:  # noqa: BLE001 - sync must never take the bot down
            print(color(
                f"[brain-sync] could not confirm/create branch '{self.branch}' "
                f"(will still attempt uploads): {e}", YELLOW,
            ))
            self._branch_ready = False

    async def upload(self, data: bytes, message: str, path: Optional[str] = None) -> bool:
        p = path if path is not None else self.path
        if not self.enabled or self.session is None:
            return False
        await self._ensure_branch()
        # Serialize the whole read-sha -> PUT -> cache-update sequence
        # across ALL paths on this branch (not just the same path).
        # Callers (e.g. a trade close, periodic reconciliation, and the
        # brain-sync loop) can all fire upload() around the same time via
        # asyncio.create_task(); without a single global lock, concurrent
        # PUTs to DIFFERENT files still race for the branch's HEAD commit
        # and GitHub rejects the loser with 409, even with a correct
        # per-file sha. Serializing every upload here trades a little
        # throughput for correctness - fine given upload() is already
        # best-effort/fire-and-forget everywhere it's called.
        async with self._upload_lock:
            try:
                sha = self._last_sha.get(p)
                if sha is None:
                    async with self.session.get(
                        self._url(p), params={"ref": self.branch},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            existing = await resp.json()
                            sha = existing.get("sha")

                payload = {
                    "message": message,
                    "content": base64.b64encode(data).decode("ascii"),
                    "branch": self.branch,
                }
                if sha:
                    payload["sha"] = sha

                async with self.session.put(
                    self._url(p), json=payload, timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 409:
                        # Stale sha - someone else (or a prior process) updated
                        # this file since we last cached its sha. Refetch the
                        # current sha and retry the PUT exactly once with it,
                        # rather than failing the whole upload outright.
                        print(color(
                            f"[brain-sync] GitHub push for {p} got 409 (stale sha) - "
                            f"refetching latest sha and retrying once.", YELLOW,
                        ))
                        async with self.session.get(
                            self._url(p), params={"ref": self.branch},
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as sha_resp:
                            if sha_resp.status == 200:
                                fresh = await sha_resp.json()
                                payload["sha"] = fresh.get("sha")
                            else:
                                payload.pop("sha", None)

                        async with self.session.put(
                            self._url(p), json=payload, timeout=aiohttp.ClientTimeout(total=20),
                        ) as retry_resp:
                            if retry_resp.status not in (200, 201):
                                text = await retry_resp.text()
                                raise RuntimeError(
                                    f"HTTP {retry_resp.status} (after 409 retry): {text[:200]}"
                                )
                            result = await retry_resp.json()
                            self._last_sha[p] = (result.get("content") or {}).get("sha")
                            return True

                    if resp.status not in (200, 201):
                        text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                    result = await resp.json()
                    self._last_sha[p] = (result.get("content") or {}).get("sha")
                    return True
            except Exception as e:  # noqa: BLE001 - sync must never take the bot down
                print(color(f"[brain-sync] GitHub push failed for {p} (bot keeps trading): {e}", YELLOW))
                return False
