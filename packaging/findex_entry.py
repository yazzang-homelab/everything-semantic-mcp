"""Frozen entry point: `findex` (UI), `findex mcp`, `findex scan`, ... (see everything_semantic_mcp.__main__)."""

import multiprocessing
import sys

from everything_semantic_mcp.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
