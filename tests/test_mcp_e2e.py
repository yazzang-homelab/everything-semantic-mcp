"""End to end over real MCP stdio: start the installed CLI and call every read-only tool."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from everything_semantic_mcp import ipc

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not ipc.available(), reason="Everything is not running"
)

EXPECTED = {
    "everything_search",
    "everything_count",
    "everything_recent",
    "everything_largest",
    "everything_health",
    "everything_maintain",
    "everything_semantic_search",
    "everything_semantic_reindex",
    "everything_semantic_status",
}


def _payload(res):
    if getattr(res, "structured_content", None):
        return res.structured_content
    return json.loads(res.content[0].text)


def test_stdio_session_lists_and_calls_tools():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "everything_semantic_mcp"])
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            assert {t.name for t in (await s.list_tools()).tools} == EXPECTED
            found = _payload(await s.call_tool("everything_search", {"query": "everything.exe", "limit": 3}))
            assert found["total"] >= 1
            counts = _payload(await s.call_tool("everything_count", {"queries": ["ext:exe", "ext:dll"]}))
            assert all(v >= 0 for v in counts["counts"].values())
            health = _payload(await s.call_tool("everything_health", {}))
            assert health["db_loaded"] is True and isinstance(health["issues"], list)
            status = _payload(await s.call_tool("everything_semantic_status", {}))
            assert "index_dir" in status

    asyncio.run(run())
