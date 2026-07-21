
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
        # _last_sha). Multiple fire-and-forget sync tasks can legitimately
        # be in flight at once (e.g. a trade close and the periodic
        # reconciliation pass both calling upload() around the same time)
        # - without this, two concurrent uploads to the SAME path both read
        # the same cached sha before either PUT lands, so the second PUT is
        # rejected by GitHub with 409 (stale sha) even though nothing was
        # actually wrong with the data. The lock serializes read-sha ->
        # PUT -> cache-update per path, so the second call always sees the
        # sha the first call just wrote. Uploads to DIFFERENT paths (e.g.
        # brain.pkl vs trades_log.csv) are unaffected and still run
        # concurrently.
        self._path_locks: dict = {}

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
        # Serialize the whole read-sha -> PUT -> cache-update sequence per
        # path. Callers (e.g. a trade close and the periodic reconciliation
        # pass) can both fire an upload() for the same path around the same
        # time via asyncio.create_task(); without this lock both would read
        # the same cached sha and the second PUT would be rejected by
        # GitHub with 409 once the first PUT lands. The lock guarantees the
        # second call only proceeds after the first has finished and
        # refreshed self._last_sha[p], so it always PUTs with the current
        # sha. Different paths use different locks and are never blocked by
        # each other.
        async with self._lock_for(p):
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
                    if resp.status not in (200, 201):
                        text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                    result = await resp.json()
                    self._last_sha[p] = (result.get("content") or {}).get("sha")
                    return True
            except Exception as e:  # noqa: BLE001 - sync must never take the bot down
                print(color(f"[brain-sync] GitHub push failed for {p} (bot keeps trading): {e}", YELLOW))
                return False
