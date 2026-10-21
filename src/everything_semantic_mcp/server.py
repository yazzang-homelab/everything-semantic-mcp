"""MCP server: filename search (Everything or built-in index) + local semantic search + self-check.

Engine: voidtools Everything over IPC on Windows when it is running, otherwise the built-in
cross-platform index (see engine.py). Tool names keep the ``everything_`` prefix for
compatibility with existing agent configs.
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

from . import engine as engine_mod
from . import health, ipc, semantic

MAX_LIMIT = 500
_engine = None
_engine_lock = threading.Lock()


def engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = engine_mod.select()
        return _engine


server = _Server(
    "findex",
    instructions=(
        "Instant file/folder name search over the user's machine (Everything on Windows, or findex's "
        "own index on any OS): millions of entries in ~100 ms. Prefer this over walking directories. "
        'Query syntax: space = AND, | = OR, ! = NOT, "quoted phrase", wildcards * ?, ext:pdf;docx, '
        'path:"<dir>", parent:"<dir>" (direct children), size:>10mb, dm:today / dm:last7days, '
        "folder: / file:. Only names and paths are indexed, not contents. For a natural-language "
        "request use everything_find (name match + meaning); for exact filters, sorting and counts "
        "use everything_search."
    ),
)

Sort = Literal[
    "name",
    "name_desc",
    "path",
    "path_desc",
    "size",
    "size_desc",
    "date_modified",
    "date_modified_desc",
]


def _shape(total: int, items: list[dict], offset: int, elapsed: float, details: bool) -> dict:
    sep = "\\" if engine().name == "everything" else os.sep
    rows = items if details else [i["full_path"] + (sep if i["type"] == "folder" else "") for i in items]
    return {
        "engine": engine().name,
        "total": total,
        "offset": offset,
        "returned": len(items),
        "truncated": offset + len(items) < total,
        "elapsed_ms": elapsed,
        "results": rows,
    }


def _scope(path_filter: str) -> str:
    return f'path:"{path_filter.rstrip(chr(92) + "/")}"' if path_filter else ""


@server.tool()
def everything_search(
    query: str,
    limit: int = 50,
    offset: int = 0,
    sort: Sort = "name",
    details: bool = True,
    match_path: bool = False,
) -> dict:
    """Search file and folder names across every indexed drive and folder.

    query: Everything syntax, e.g. 'ext:xlsx budget dm:last30days', 'path:"D:\\Projects" *.pptx',
      'folder: invoices', '"exact name.txt"'. An empty query matches everything (use with filters).
    limit: 1-500 rows; `total` tells you whether to narrow the query.
    sort: name (always fast) | date_modified_desc for "latest" | size_desc for "largest".
    If nothing contains every word, the response has match='relaxed' and lists entries containing
    the most of the words (filters still apply) - check them before relying on them.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    eng = engine()
    total, items, ms = eng.search(
        query, max_results=limit, offset=max(0, offset), sort=sort, match_path=match_path, details=details
    )
    out = _shape(total, items, offset, ms, details)
    out["match"] = "exact"
    if total == 0 and offset == 0:
        relaxed = eng.relaxed(query, limit)
        if relaxed:
            out.update(
                match="relaxed",
                returned=len(relaxed),
                results=relaxed,
                note="No entry contains every word; these contain the most of them.",
            )
    return out


@server.tool()
def everything_find(
    request: str, limit: int = 20, path_prefix: str = "", wait_for_model: bool = True
) -> dict:
    """Best single call for a natural-language request ('quarterly sales deck', '신입 교육 자료',
    'post annealing 작업표준서 최신본'). Fuses name matching (any word, ranked by how many words
    match, then BM25 on the local engine) with meaning-based search (reciprocal rank fusion).
    Measured on 780k real paths with 20 topic queries: the target is in the top 10 for 0.90 of
    queries vs 0.25 for a strict name search. Use everything_search for exact filters and sorting."""
    started = time.perf_counter()
    limit = max(1, min(limit, 100))
    eng = engine()
    scoped = (request + " " + _scope(path_prefix)).strip()
    lexical = eng.relaxed(scoped, 100)
    if lexical is None:
        lexical = eng.search(scoped, max_results=100, match_path=True)[1]
    meaning_note = None
    try:
        # Do not block a person on the ~20 s model load: name results come back immediately
        # and meaning joins once the background warm-up finishes.
        meaning = semantic.search(request, 100, path_prefix, "any", wait=wait_for_model)["results"]
    except RuntimeError as exc:
        meaning, meaning_note = [], str(exc)  # index not built / model loading / extra missing
    fused: dict[str, float] = {}
    by_path: dict[str, dict] = {}
    for rank, item in enumerate(lexical):
        p = item["full_path"]
        fused[p] = fused.get(p, 0.0) + 1.0 / (61 + rank)
        by_path[p] = item
    for rank, item in enumerate(meaning):
        p = item["path"].rstrip("\\/")
        fused[p] = fused.get(p, 0.0) + 1.0 / (61 + rank)
        by_path.setdefault(
            p, {"full_path": p, "type": "folder" if item["path"].endswith(("\\", "/")) else "file"}
        )
        by_path[p]["meaning_score"] = item["score"]
    order = sorted(fused, key=lambda p: -fused[p])[:limit]
    return {
        "engine": eng.name,
        "request": request,
        "candidates": {"name_match": len(lexical), "meaning": len(meaning)},
        **({"meaning_note": meaning_note} if meaning_note else {}),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "results": [by_path[p] for p in order],
    }


@server.tool()
def everything_count(queries: list[str]) -> dict:
    """Count matches for up to 20 queries in one call (no rows), e.g. compare 'ext:pdf' vs 'ext:docx'."""
    return {"counts": {q: engine().count(q) for q in queries[:20]}}


@server.tool()
def everything_recent(
    path_filter: str = "", extensions: str = "", within: str = "today", limit: int = 50
) -> dict:
    """Most recently modified files, newest first. within: today | yesterday | last7days |
    last30days | thisweek | thismonth | 2026-10. extensions: 'xlsx;csv'."""
    q = " ".join(
        p
        for p in ["file:", f"dm:{within}", _scope(path_filter), f"ext:{extensions}" if extensions else ""]
        if p
    )
    total, items, ms = engine().search(
        q, max_results=max(1, min(limit, MAX_LIMIT)), sort="date_modified_desc"
    )
    return _shape(total, items, 0, ms, True)


@server.tool()
def everything_largest(path_filter: str = "", min_size: str = "100mb", limit: int = 30) -> dict:
    """Largest files (optionally under a folder), for disk-usage triage. min_size: '100mb', '1gb'."""
    q = " ".join(p for p in ["file:", f"size:>{min_size}", _scope(path_filter)] if p)
    total, items, ms = engine().search(q, max_results=max(1, min(limit, MAX_LIMIT)), sort="size_desc")
    return _shape(total, items, 0, ms, True)


@server.tool()
def everything_semantic_search(
    query: str, limit: int = 20, path_prefix: str = "", kind: Literal["any", "folder", "file"] = "any"
) -> dict:
    """Meaning-only search over folder and document names (multilingual, fully local). Scores:
    >0.6 strong, 0.4-0.6 related. Usually everything_find is the better single call."""
    return semantic.search(query, max(1, min(limit, 200)), path_prefix, kind)


@server.tool()
def everything_semantic_reindex(wait: bool = False) -> dict:
    """(Re)build the local semantic index from the current name index (background unless wait=true)."""
    return semantic.build(engine()) if wait else semantic.build_async(engine())


@server.tool()
def everything_status() -> dict:
    """Engine (everything | local), entry counts, indexed roots and scan times, semantic index state."""
    return {"engine": engine().status(), "semantic": semantic.status()}


@server.tool()
def everything_rescan() -> dict:
    """Rescan the built-in index's folders now (local engine only; Everything keeps itself current)."""
    eng = engine()
    if eng.name != "local":
        return {"ok": False, "reason": "Everything keeps its own index current; use everything_maintain"}
    return eng.rescan(background=True)


@server.tool()
def everything_health() -> dict:
    """Everything configuration self-check (Windows + Everything engine): build, database state,
    and settings that cause slow indexing, RAM bloat or freezes, each with a fix."""
    eng = engine()
    if eng.name != "everything":
        return {"engine": eng.name, "note": "Only applies when voidtools Everything is the engine."}
    return health.report(ipc.status(eng.instance), eng.instance)


@server.tool()
def everything_maintain(action: Literal["save_db", "rescan_folders", "rebuild"]) -> dict:
    """Everything index maintenance: save_db | rescan_folders | rebuild (Everything engine only)."""
    eng = engine()
    if eng.name != "everything":
        return {"ok": False, "reason": "use everything_rescan with the built-in index"}
    if ipc.send_command(ipc.IS_DB_BUSY, instance=eng.instance):
        return {"ok": False, "reason": "the database is busy (indexing in progress); retry later"}
    cmd = {
        "save_db": ipc.SAVE_DB,
        "rescan_folders": ipc.UPDATE_ALL_FOLDER_INDEXES,
        "rebuild": ipc.REBUILD_DB,
    }[action]
    ipc.send_command(cmd, timeout_ms=10_000, instance=eng.instance)
    return {"ok": True, "action": action}


def _background() -> None:
    """Model warm-up, first-run name scan for the local engine, idle model release."""
    semantic.warm_up()
    eng = engine()
    if eng.name == "local" and eng.index.total() == 0:
        eng.rescan(background=True)
    while True:
        time.sleep(60)
        semantic.release_idle_model()


def serve() -> None:
    os.environ.setdefault("FINDEX_EMBED_PROCESS", "1")  # model load must not stall tool calls
    threading.Thread(target=_background, daemon=True, name="findex-bg").start()
    server.run("stdio")
