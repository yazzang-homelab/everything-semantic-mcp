"""findex — filename + meaning search for people and AI agents.

findex                 open the search page in your browser (double-click)
findex mcp             run as an MCP server over stdio (what agents start)
findex scan            build/refresh the built-in name index now
findex reindex         build the semantic (meaning) index now
findex status          print engine + index status as JSON
findex health          Everything configuration self-check (Windows + Everything)
findex roots add DIR | roots remove DIR | roots   manage folders the built-in index covers
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "ui"
    if cmd in ("mcp", "serve"):
        from .server import serve

        serve()
        return 0
    if cmd == "ui":
        from .webui import serve

        port = int(args[1]) if len(args) > 1 else 0
        serve(port=port)
        return 0

    from . import health, ipc, semantic
    from .server import engine

    eng = engine()
    if cmd == "scan":
        if eng.name != "local":
            print("Everything is the engine; it keeps its own index current.", file=sys.stderr)
            return 0
        print(json.dumps(eng.rescan(background=False), ensure_ascii=False, indent=2))
        return 0
    if cmd == "reindex":
        print(json.dumps(semantic.build(eng), ensure_ascii=False))
        return 0
    if cmd == "status":
        print(
            json.dumps({"engine": eng.status(), "semantic": semantic.status()}, ensure_ascii=False, indent=2)
        )
        return 0
    if cmd == "health":
        if eng.name != "everything":
            print("health applies to voidtools Everything (Windows).", file=sys.stderr)
            return 0
        print(json.dumps(health.report(ipc.status(eng.instance), eng.instance), ensure_ascii=False, indent=2))
        return 0
    if cmd == "roots":
        if eng.name != "local":
            print("Everything is the engine; manage folders in Everything's options.", file=sys.stderr)
            return 1
        if len(args) >= 3 and args[1] in ("add", "remove"):
            (eng.index.add_root if args[1] == "add" else eng.index.remove_root)(args[2])
        print(json.dumps(eng.index.roots(), ensure_ascii=False, indent=2))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
