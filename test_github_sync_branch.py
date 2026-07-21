"""
Focused test for the Railway-deploy-loop fix: GithubBrainSync must push
runtime state to a dedicated branch, auto-creating it off the repo's
default branch if it doesn't exist yet, using ONLY the single shared
session (no second GitHub client). Not part of smoke_test.py (which has
no network/GitHub involvement) - this exercises github_sync.py in
isolation with a fake aiohttp session, so it needs no real token/repo.

Run: python3 test_github_sync_branch.py
"""
from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import github_sync


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class FakeSession:
    """Records every call and returns scripted responses keyed by
    (method, url) so we can assert exactly what github_sync.py does
    against the GitHub API - without any real network access."""

    def __init__(self):
        self.calls = []
        self.branch_exists = False  # starts absent -> forces auto-create path
        self.files: dict[str, dict] = {}

    def _resp_for(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        repo_base = "https://api.github.com/repos/acct/repo"

        if method == "GET" and url == f"{repo_base}/git/ref/heads/brain-state":
            if self.branch_exists:
                return FakeResponse(200, {"object": {"sha": "branchsha"}})
            return FakeResponse(404, {"message": "Not Found"})

        if method == "GET" and url == repo_base:
            return FakeResponse(200, {"default_branch": "main"})

        if method == "GET" and url == f"{repo_base}/git/ref/heads/main":
            return FakeResponse(200, {"object": {"sha": "mainsha123"}})

        if method == "GET" and url.startswith(f"{repo_base}/contents/"):
            # sha lookup before first PUT of a given file - none exist yet
            return FakeResponse(404, {"message": "Not Found"})

        if method == "POST" and url == f"{repo_base}/git/refs":
            body = kwargs.get("json", {})
            assert body["ref"] == "refs/heads/brain-state"
            assert body["sha"] == "mainsha123"
            self.branch_exists = True
            return FakeResponse(201, {"ref": body["ref"]})

        if method == "PUT" and url.startswith(f"{repo_base}/contents/"):
            body = kwargs.get("json", {})
            assert body["branch"] == "brain-state", (
                "upload() must push to the dedicated runtime branch, "
                "never to the code/deploy branch"
            )
            path = url.split("/contents/", 1)[1]
            self.files[path] = body
            return FakeResponse(201, {"content": {"sha": "newfilesha"}})

        raise AssertionError(f"Unexpected call: {method} {url} {kwargs}")

    @asynccontextmanager
    async def get(self, url, **kwargs):
        yield self._resp_for("GET", url, **kwargs)

    @asynccontextmanager
    async def put(self, url, **kwargs):
        yield self._resp_for("PUT", url, **kwargs)

    @asynccontextmanager
    async def post(self, url, **kwargs):
        yield self._resp_for("POST", url, **kwargs)

    async def close(self):
        pass


async def main():
    sync = github_sync.GithubBrainSync(
        token="fake-token", repo="acct/repo", path="brain_v2.pkl", branch="brain-state",
    )
    fake = FakeSession()
    sync.session = fake  # bypass start(): no real aiohttp.ClientSession/network needed

    # 1) First upload: branch doesn't exist yet -> must be auto-created off
    #    the repo's default branch ("main"), then the file pushed to it.
    ok = await sync.upload(b"brain-bytes-1", message="brain sync: test", path="brain_v2.pkl")
    assert ok is True
    assert fake.branch_exists is True
    assert sync._branch_ready is True
    assert "brain_v2.pkl" in fake.files
    assert fake.files["brain_v2.pkl"]["branch"] == "brain-state"
    ref_check_calls = [c for c in fake.calls if c[1].endswith("/git/ref/heads/brain-state")]
    assert len(ref_check_calls) == 1, "branch existence must be checked, not skipped"
    create_calls = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/git/refs")]
    assert len(create_calls) == 1, "branch must be created exactly once"
    print("OK: first upload auto-creates dedicated branch and pushes to it")

    # 2) Second upload (different file, e.g. trades_log.csv): branch check
    #    must be cached (_branch_ready), not repeated - one shared client,
    #    minimal API calls.
    calls_before = len(fake.calls)
    ok2 = await sync.upload(b"csv-bytes", message="trades_log.csv sync", path="trades_log.csv")
    assert ok2 is True
    assert fake.files["trades_log.csv"]["branch"] == "brain-state"
    ref_check_calls_after = [c for c in fake.calls if c[1].endswith("/git/ref/heads/brain-state")]
    assert len(ref_check_calls_after) == 1, "branch existence must NOT be re-checked on every upload"
    create_calls_after = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/git/refs")]
    assert len(create_calls_after) == 1, "branch must never be created twice"
    print("OK: second upload reuses cached branch-ready state, no duplicate branch creation")

    # 3) Every payload pushed anywhere in this test targeted the dedicated
    #    branch, never "main" (the presumed Railway deploy branch) - this is
    #    the actual deploy-loop fix.
    assert all(v["branch"] == "brain-state" for v in fake.files.values())
    print("OK: no runtime commit ever targeted the code/deploy branch")

    print("ALL GITHUB_SYNC BRANCH TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
