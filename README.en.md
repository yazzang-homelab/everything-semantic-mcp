# everything-semantic-mcp

[한국어](README.md) · **English**

An MCP server for [voidtools Everything](https://www.voidtools.com/) on Windows that adds:

1. **Local semantic search** over folder and document names — find "onboarding material" when the folder is called "new hire guide". Multilingual static embeddings ([Model2Vec](https://github.com/MinishLab/model2vec)), no GPU, no API key, nothing leaves the machine. Two-stage ranking (64-d PCA candidates, 256-d int8 rerank): recall@20 0.98 vs exhaustive search, ~50 ms per query.
2. **Configuration self-check** (`everything_health`) — detects the settings that make Everything hang or bloat: a drive indexed twice (NTFS + folder), change monitoring on network folders, foreground rescans, rescans at every startup, an index database that is never saved, the 32-bit build. Each finding comes with a fix; `everything_maintain` saves the DB or rescans.
3. **Pure-Python IPC** — talks to Everything's documented IPC (`WM_COPYDATA` / `EVERYTHING_IPC_QUERY2`) directly: no es.exe, no SDK DLL. Size and dates come back in Everything's reply (no per-file `os.stat()`, which matters on network shares), and only properties Everything has indexed are requested.

## Measured (one machine)

Windows 11, i7-1255U, Everything 1.4.1.1032 x64, 6,126,653 indexed entries (local + network folders):
name search 83–940 ms per 100 rows depending on the query; semantic index of 1,177,343 names built in 264 s; semantic query 45–58 ms (median). Reproduce with `python benchmarks/bench.py --reindex`.

## Install

Requires Windows with Everything running, and [uv](https://docs.astral.sh/uv/).

```powershell
uvx --from "everything-semantic-mcp[semantic] @ git+https://github.com/yazzang-homelab/everything-semantic-mcp" everything-semantic-mcp status
```

Claude Code:
```powershell
claude mcp add everything -- uvx --from "everything-semantic-mcp[semantic] @ git+https://github.com/yazzang-homelab/everything-semantic-mcp" everything-semantic-mcp
```

Build the semantic index once (`everything_semantic_reindex`, or `everything-semantic-mcp reindex`), and re-run it periodically.

## Tools

`everything_search`, `everything_count`, `everything_recent`, `everything_largest`, `everything_semantic_search`, `everything_semantic_reindex`, `everything_semantic_status`, `everything_health`, `everything_maintain`. See the Korean README for environment variables.

## Related projects

[elis132/everything-mcp](https://github.com/elis132/everything-mcp) (es.exe-based, strong slow-query guards) and [mamertofabian/mcp-everything-search](https://github.com/mamertofabian/mcp-everything-search) (SDK DLL, also macOS/Linux). This project was written independently and focuses on semantic search, the self-check, and DLL-free IPC.

## License

MIT. Unofficial extension of voidtools Everything; "Everything" is a trademark of voidtools.
