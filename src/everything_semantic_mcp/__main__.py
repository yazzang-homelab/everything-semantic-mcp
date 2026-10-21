"""Command line: ``everything-semantic-mcp [serve|reindex|status|health]``.

serve    run the MCP server over stdio (default; what MCP clients start)
reindex  build the semantic index now (for Task Scheduler / cron)
status   print Everything status + semantic index status as JSON
health   print the configuration self-check as JSON
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "serve"
    if cmd == "serve":
        from .server import serve

        serve()
        return 0
    from . import health, ipc, semantic
    from .server import INSTANCE

    if cmd == "reindex":
        if not ipc.send_command(ipc.IS_DB_LOADED, instance=INSTANCE):
            print("Everything's database is not loaded yet", file=sys.stderr)
            return 1
        print(json.dumps(semantic.build(INSTANCE), ensure_ascii=False))
        return 0
    if cmd == "status":
        print(
            json.dumps(
                {"everything": ipc.status(INSTANCE), "semantic": semantic.status()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if cmd == "health":
        print(json.dumps(health.report(ipc.status(INSTANCE), INSTANCE), ensure_ascii=False, indent=2))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
