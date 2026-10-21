"""Everything (voidtools) MCP server with local semantic search and a configuration self-check."""

__version__ = "0.1.0"


def main() -> None:
    import sys

    from .__main__ import main as _main

    sys.exit(_main())
