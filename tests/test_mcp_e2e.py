"""End to end over real MCP stdio: start `findex mcp` and call the read-only tools.

Runs on every OS against the built-in engine (FINDEX_ENGINE=local, a temp tree), and on
Windows additionally against Everything when it is running.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from everything_semantic_mcp import ipc

EXPECTED = {
    "everything_search",
    "everything_find",
    "everything_count",
    "everything_recent",
    "everything_largest",
    "everything_semantic_search",
    "everything_semantic_reindex",
    "everything_status",
    "everything_rescan",
    "everything_health",
    "everything_maintain",
}


def _payload(res):
    if getattr(res, "structured_content", None):
        return res.structured_content
    return json.loads(res.content[0].text)


async def _session(env: dict, check) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "everything_semantic_mcp", "mcp"], env={**os.environ, **env}
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        assert {t.name for t in (await s.list_tools()).tools} == EXPECTED
        await check(s)


def test_stdio_local_engine(tmp_path):
    data = tmp_path / "tree" / "Reports" / "2026 예산 요약"
    data.mkdir(parents=True)
    (data / "budget_final.xlsx").write_bytes(b"x" * 10)

    from everything_semantic_mcp import localindex

    # Index the temp tree before the server starts: with roots and entries present, the
    # server neither adds the default roots (home dir) nor starts a first-run scan.
    localindex.LocalIndex(tmp_path / "findex" / "names.db", roots=[str(tmp_path / "tree")]).scan_all()

    async def check(s):
        found = _payload(await s.call_tool("everything_search", {"query": "budget ext:xlsx"}))
        assert found["engine"] == "local" and found["total"] == 1
        found = _payload(await s.call_tool("everything_find", {"request": "예산 요약 자료"}))
        assert any("예산" in r["full_path"] for r in found["results"])
        st = _payload(await s.call_tool("everything_status", {}))
        assert st["engine"]["engine"] == "local"

    asyncio.run(_session({"FINDEX_ENGINE": "local", "FINDEX_DATA": str(tmp_path / "findex")}, check))


@pytest.mark.skipif(sys.platform != "win32" or not ipc.available(), reason="Everything is not running")
def test_stdio_everything_engine():
    async def check(s):
        found = _payload(await s.call_tool("everything_search", {"query": "everything.exe", "limit": 3}))
        assert found["engine"] == "everything" and found["total"] >= 1
        health = _payload(await s.call_tool("everything_health", {}))
        assert health["db_loaded"] is True and isinstance(health["issues"], list)

    asyncio.run(_session({"FINDEX_ENGINE": "auto"}, check))
