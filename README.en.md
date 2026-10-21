# findex

[한국어](README.md) · **English**

**Find files even when you don't remember their names — a search window for people, an MCP server for AI agents.** One executable for Windows, macOS and Linux; no install, no Python.

**[⬇ Download the latest release](https://github.com/yazzang-homelab/everything-semantic-mcp/releases/latest)**

- **Name search** with Everything syntax (`ext:`, `path:`, `dm:`, `size:`, `folder:`, `!`, `|`, `*`).
- **Relaxed matching**: when no entry contains every word, results are ranked by how many words match (BM25 as tie-break) instead of returning nothing.
- **Meaning search**: folder and document names embedded with a multilingual static model (Model2Vec), fully local.

## Start

1. Download `findex-windows-x64.exe`, `findex-macos-arm64` (Apple Silicon; Intel Macs: `pip install git+https://github.com/yazzang-homelab/everything-semantic-mcp`) or `findex-linux-x64` from Releases.
2. Run it — a search page opens in your browser. (Windows SmartScreen: "More info → Run". macOS: `xattr -d com.apple.quarantine <file>; chmod +x <file>`.)
3. Click **"의미 색인 만들기" (build meaning index)** once.

**Engine**: on Windows with [voidtools Everything](https://www.voidtools.com/) running, findex uses Everything's index. Otherwise (macOS, Linux, Windows without Everything) it builds its own (home folder, plus fixed drives on Windows); change with `findex roots add|remove DIR` and `findex scan`.

## Connect an agent (MCP)

```bash
claude mcp add findex -- /path/to/findex mcp
```
Any MCP client: `"command": "/path/to/findex", "args": ["mcp"]`. Main tool: `everything_find` (one call for a natural-language request); `everything_search` for exact filters/sorting.

## Measured
Windows 11, i7-1255U, Everything 1.4.1.1032 x64, 6.1M entries: semantic index of 1.18M names built in 264 s; semantic query 45–58 ms; fused `find` ~440 ms warm; recall@20 0.98 for the two-stage rerank. On 780k real paths, queries with one word not in the target's name: strict search returns nothing for 89%, findex's relaxed ranking puts the target in the top 10 for 53%.

## Privacy
Names, paths, sizes and dates only — file contents are never read. Everything stays on your machine except the one-time model download. The search page listens on 127.0.0.1 only.

## License
MIT. Unofficial integration with voidtools Everything; "Everything" is a trademark of voidtools.
