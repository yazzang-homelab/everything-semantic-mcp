"""MCP server: Everything search + local semantic search + configuration self-check.

Tools are read-only except ``everything_maintain`` (save DB / rescan folders / rebuild) and
``everything_semantic_reindex`` (rebuilds the local semantic index only).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Literal

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ModuleNotFoundError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import health, ipc, semantic

INSTANCE = os.environ.get("EVERYTHING_INSTANCE", "").strip()
MAX_LIMIT = 500

server = _Server(
    "everything-semantic",
    instructions=(
        "Instant file/folder name search over the user's Windows machine (and any network folders "
        "Everything indexes) via voidtools Everything: millions of entries in ~100 ms. Prefer this over "
        'walking directories. Query syntax: space = AND, | = OR, ! = NOT, "quoted phrase", wildcards * ?, '
        'ext:pdf;docx, path:"C:\\dir", parent:"C:\\dir" (direct children), size:>10mb, dm:today / '
        "dm:last7days, folder: / file:, regex:<pattern>. Only names and paths are indexed, not contents. "
        "When the user describes a topic rather than a name, use everything_semantic_search, then "
        "refine with everything_search inside the returned folders."
    ),
)

Sort = Literal[
    "name",
    "name_desc",
    "path",
    "path_desc",
    "size",
    "size_desc",
    "extension",
    "extension_desc",
    "date_created",
    "date_created_desc",
    "date_modified",
    "date_modified_desc",
]


def _shape(result: ipc.QueryResult, details: bool) -> dict:
    items = (
        result.items
        if details
        else [i["full_path"] + ("\\" if i["type"] == "folder" else "") for i in result.items]
    )
    return {
        "total": result.total,
        "offset": result.offset,
        "returned": len(result.items),
        "truncated": result.offset + len(result.items) < result.total,
        "elapsed_ms": result.elapsed_ms,
        "results": items,
    }


def _scope(path_filter: str) -> str:
    return f'path:"{path_filter.rstrip(chr(92))}\\"' if path_filter else ""


@server.tool()
def everything_search(
    query: str,
    limit: int = 50,
    offset: int = 0,
    sort: Sort = "name",
    details: bool = True,
    match_path: bool = False,
    match_case: bool = False,
    whole_word: bool = False,
    regex: bool = False,
) -> dict:
    """Search file and folder names across every drive and network folder Everything indexes.

    query: Everything syntax, e.g. 'ext:xlsx budget dm:last30days', 'path:"D:\\Projects" *.pptx',
      'folder: invoices', '"exact name.txt"'. An empty query matches everything (use with filters).
    limit: 1-500 rows; `total` in the response tells you whether to narrow the query.
    sort: name (always instant) | date_modified_desc for "latest" | size_desc for "largest".
    details: include size (bytes) and modification date; false returns bare paths (folders end with '\\').
    match_path: match terms against the full path instead of just the name.
    """
    result = ipc.query(
        query,
        max_results=max(1, min(limit, MAX_LIMIT)),
        offset=max(0, offset),
        sort=sort,
        match_case=match_case,
        whole_word=whole_word,
        match_path=match_path,
        regex=regex,
        request_flags=None if details else ipc.REQ_FULL_PATH,
        instance=INSTANCE,
    )
    return _shape(result, details)


@server.tool()
def everything_count(queries: list[str], match_path: bool = False) -> dict:
    """Count matches for up to 20 queries in one call (no rows returned), e.g. compare 'ext:pdf'
    vs 'ext:docx' under a folder, or check whether a file exists anywhere."""
    return {
        "counts": {
            q: ipc.query(
                q, max_results=0, match_path=match_path, request_flags=ipc.REQ_FULL_PATH, instance=INSTANCE
            ).total
            for q in queries[:20]
        }
    }


@server.tool()
def everything_recent(
    path_filter: str = "", extensions: str = "", within: str = "today", limit: int = 50
) -> dict:
    """Most recently modified files, newest first.

    path_filter: restrict to a folder or share. extensions: ';'-separated, e.g. 'xlsx;csv'.
    within: today | yesterday | last7days | last30days | thisweek | thismonth | 2026-10.
    """
    parts = ["file:", f"dm:{within}", _scope(path_filter), f"ext:{extensions}" if extensions else ""]
    result = ipc.query(
        " ".join(p for p in parts if p),
        max_results=max(1, min(limit, MAX_LIMIT)),
        sort="date_modified_desc",
        instance=INSTANCE,
    )
    return _shape(result, True)


@server.tool()
def everything_largest(path_filter: str = "", min_size: str = "100mb", limit: int = 30) -> dict:
    """Largest files (optionally under a folder or share), for disk-usage triage. min_size: '100mb', '1gb'."""
    parts = ["file:", f"size:>{min_size}", _scope(path_filter)]
    result = ipc.query(
        " ".join(p for p in parts if p),
        max_results=max(1, min(limit, MAX_LIMIT)),
        sort="size_desc",
        instance=INSTANCE,
    )
    return _shape(result, True)


@server.tool()
def everything_health() -> dict:
    """Index health and configuration self-check: version, build (x86/x64), database state,
    indexed volumes and folders, database age, and settings that cause slow indexing, RAM bloat
    or freezes (a drive indexed twice, change monitoring on network folders, foreground rescans,
    rescans at every startup, an index never saved, the 32-bit build). Each issue has a fix."""
    return health.report(ipc.status(INSTANCE), INSTANCE)


@server.tool()
def everything_maintain(action: Literal["save_db", "rescan_folders", "rebuild"]) -> dict:
    """Index maintenance. save_db: persist the in-memory index (fast restart). rescan_folders: refresh
    folder indexes (network shares) now. rebuild: full re-index (slow; only if results look stale)."""
    if ipc.send_command(ipc.IS_DB_BUSY, instance=INSTANCE):
        return {"ok": False, "reason": "the database is busy (indexing in progress); retry later"}
    cmd = {
        "save_db": ipc.SAVE_DB,
        "rescan_folders": ipc.UPDATE_ALL_FOLDER_INDEXES,
        "rebuild": ipc.REBUILD_DB,
    }[action]
    ipc.send_command(cmd, timeout_ms=10_000, instance=INSTANCE)
    return {"ok": True, "action": action}


@server.tool()
def everything_semantic_search(
    query: str, limit: int = 20, path_prefix: str = "", kind: Literal["any", "folder", "file"] = "any"
) -> dict:
    """Meaning-based search over folder names and document file names (multilingual, fully local).
    Use when the user describes WHAT they want rather than a literal name: 'last quarter's sales
    deck', '세금 계산서 모음', 'onboarding material'. Scores: >0.6 strong, 0.4-0.6 related.
    path_prefix restricts to a drive or share. Folders end with '\\'; list their contents with
    everything_search('parent:"<folder>"')."""
    return semantic.search(query, max(1, min(limit, 200)), path_prefix, kind)


@server.tool()
def everything_semantic_reindex(wait: bool = False) -> dict:
    """(Re)build the local semantic index from the live Everything database. Runs in the
    background unless wait=true (~3-7 min per million names on a laptop CPU); poll
    everything_semantic_status."""
    return semantic.build(INSTANCE) if wait else semantic.build_async(INSTANCE)


@server.tool()
def everything_semantic_status() -> dict:
    """Semantic index metadata (count, built_at, model), its location, and any running or failed build."""
    return semantic.status()


def _idle_releaser() -> None:
    while True:
        time.sleep(60)
        semantic.release_idle_model()


def serve() -> None:
    threading.Thread(target=_idle_releaser, daemon=True, name="model-idle").start()
    server.run("stdio")
