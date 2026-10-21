"""Reproducible benchmark on the local machine (prints the numbers quoted in the README).

  python benchmarks/bench.py            # name search + semantic query latency (index must exist)
  python benchmarks/bench.py --reindex  # also time a full semantic index build

Queries are generic (no machine-specific paths); results depend on what Everything indexes.
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time

from everything_semantic_mcp import ipc, semantic

NAME_QUERIES = ["*.pdf", "ext:xlsx", "report", "dm:today file:", "size:>1gb", "folder: 2026"]
SEMANTIC_QUERIES = ["budget report", "onboarding material", "meeting notes", "회의록", "견적서", "사진 백업"]
RUNS = 5


def p50(xs):
    return round(statistics.median(xs), 1)


def main() -> None:
    st = ipc.status()
    total = ipc.query("", max_results=0, request_flags=ipc.REQ_FULL_PATH).total
    out = {
        "machine": {
            "cpu": platform.processor(),
            "os": platform.platform(),
            "python": platform.python_version(),
        },
        "everything": {"version": st["version"], "build": st["target_machine"], "entries": total},
    }
    name = {}
    for q in NAME_QUERIES:
        lat = []
        for _ in range(RUNS):
            t = time.perf_counter()
            r = ipc.query(q, max_results=100)
            lat.append((time.perf_counter() - t) * 1000)
        name[q] = {"p50_ms": p50(lat), "total": r.total}
    out["name_search_100_rows"] = name

    if "--reindex" in sys.argv:
        out["semantic_build"] = semantic.build()
    sem = {}
    semantic.search("warmup", 1)
    for q in SEMANTIC_QUERIES:
        lat = [semantic.search(q, 20)["elapsed_ms"] for _ in range(RUNS)]
        sem[q] = p50(lat)
    out["semantic_query_ms_p50"] = sem
    out["semantic_index"] = semantic.status()["index"]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
