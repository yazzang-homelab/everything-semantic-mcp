"""Search engine selection: Everything (Windows, if running) or the built-in local index.

Both expose the same small surface used by the MCP server, semantic indexing and the web UI:
``search``, ``count``, ``iter_paths`` and ``status``. Result items are dicts with
``full_path``, ``type`` and optional ``size`` / ``date_modified``.

FINDEX_ENGINE=everything|local forces one; default is "auto".
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from . import ipc, localindex

# Excluded from the relaxed (any-word) fallback only: OR-ing common words would otherwise
# surface caches and temp files. Exact searches are never filtered.
NOISE_DIRS = (
    "\\AppData\\",
    "\\$Recycle.Bin\\",
    "\\node_modules\\",
    "\\.git\\",
    "\\site-packages\\",
    "C:\\Windows\\",
    "\\.cache\\",
)


def data_home() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return Path(os.environ.get("FINDEX_DATA", base / "findex"))


class EverythingEngine:
    name = "everything"

    def __init__(self, instance: str = "") -> None:
        self.instance = instance

    def search(
        self,
        q: str,
        *,
        max_results: int = 100,
        offset: int = 0,
        sort: str = "name",
        match_path: bool = False,
        details: bool = True,
        **kw,
    ) -> tuple[int, list[dict], float]:
        r = ipc.query(
            q,
            max_results=max_results,
            offset=offset,
            sort=sort,
            match_path=match_path,
            request_flags=None if details else ipc.REQ_FULL_PATH,
            instance=self.instance,
            **kw,
        )
        return r.total, r.items, r.elapsed_ms

    def relaxed(self, q: str, limit: int = 100) -> list[dict] | None:
        """Any-word fallback via Everything's OR syntax ("<a|b>"), ranked by word coverage."""
        words, rest = localindex.plain_words(q)
        words = [w for w in words if len(w) >= 2]  # 2-syllable Korean words count
        if not words:
            return None
        noise = " ".join(f'!path:"{p}"' for p in NOISE_DIRS)  # path: so they test full paths
        # Match words against names, not full paths. Measured on a 6.1M-entry index: an OR of
        # common Korean words took ~4.3 s with match_path vs ~0.6-1 s on names. Tradeoff:
        # entries whose words appear only in parent folders are not retrieved; the matching
        # folder itself is. Ranking below still uses full-path coverage (the evaluated one).
        r = ipc.query(
            f"{rest} <{'|'.join(words)}> {noise}".strip(),
            max_results=3000,
            match_path=False,
            instance=self.instance,
        )
        low = [w.lower() for w in words]

        def cover(i: dict) -> int:
            p = i["full_path"].lower()
            n = p.rsplit("\\", 1)[-1]
            return sum(w in p for w in low) * 2 + sum(w in n for w in low)

        return sorted(r.items, key=lambda i: (-cover(i), len(i["full_path"])))[:limit]

    def count(self, q: str, match_path: bool = False) -> int:
        return ipc.query(
            q, max_results=0, match_path=match_path, request_flags=ipc.REQ_FULL_PATH, instance=self.instance
        ).total

    def iter_paths(self, q: str, page: int = 100_000):
        offset = 0
        while True:
            r = ipc.query(
                q,
                max_results=page,
                offset=offset,
                match_path=True,
                request_flags=ipc.REQ_FULL_PATH,
                sort="path",
                instance=self.instance,
            )
            for i in r.items:
                yield i["full_path"] + ("\\" if i["type"] == "folder" else "")
            offset += len(r.items)
            if not r.items or offset >= r.total:
                return

    def status(self) -> dict:
        return {"engine": self.name, **ipc.status(self.instance)}


class LocalEngine:
    name = "local"

    def __init__(self, roots: list[str] | None = None) -> None:
        self.index = localindex.LocalIndex(data_home() / "names.db")
        self._scan_thread: threading.Thread | None = None
        self._default_roots = roots

    def ensure_roots(self) -> None:
        """First run only: index the defaults (home, fixed drives) unless roots were configured."""
        if not self.index.roots():
            for r in self._default_roots or localindex.default_roots():
                self.index.add_root(r)

    def search(
        self,
        q: str,
        *,
        max_results: int = 100,
        offset: int = 0,
        sort: str = "name",
        match_path: bool = True,
        details: bool = True,
        **kw,
    ) -> tuple[int, list[dict], float]:
        t = time.perf_counter()
        total, items = localindex.query(self.index, q, max_results=max_results, offset=offset, sort=sort)
        if not details:
            items = [{"type": i["type"], "full_path": i["full_path"]} for i in items]
        return total, items, round((time.perf_counter() - t) * 1000, 1)

    def relaxed(self, q: str, limit: int = 100) -> list[dict] | None:
        return localindex.relaxed(self.index, q, limit)

    def count(self, q: str, match_path: bool = True) -> int:
        return localindex.query(self.index, q, max_results=0)[0]

    def iter_paths(self, q: str, page: int = 100_000):
        offset = 0
        while True:
            total, items = localindex.query(self.index, q, max_results=page, offset=offset, sort="path")
            for i in items:
                yield i["full_path"] + (os.sep if i["type"] == "folder" else "")
            offset += len(items)
            if not items or offset >= total:
                return

    def rescan(self, background: bool = True) -> dict:
        self.ensure_roots()
        if self._scan_thread and self._scan_thread.is_alive():
            return {"started": False, "reason": f"already scanning {self.index.scanning}"}
        if not background:
            return {"scanned": [s.__dict__ for s in self.index.scan_all()]}
        self._scan_thread = threading.Thread(target=self.index.scan_all, daemon=True, name="findex-scan")
        self._scan_thread.start()
        return {"started": True}

    def status(self) -> dict:
        roots = self.index.roots()
        for r in roots:
            r.pop("gen", None)
            if r["scanned_at"]:
                r["scanned_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r["scanned_at"]))
        return {
            "engine": self.name,
            "entries": self.index.total(),
            "scanning": self.index.scanning,
            "roots": roots,
            "db": str(self.index.db_path),
        }


def select(roots: list[str] | None = None):
    forced = os.environ.get("FINDEX_ENGINE", "auto").lower()
    instance = os.environ.get("EVERYTHING_INSTANCE", "").strip()
    if forced == "everything" or (forced == "auto" and sys.platform == "win32" and ipc.available(instance)):
        return EverythingEngine(instance)
    return LocalEngine(roots)
