"""Cross-platform filename index (Windows, macOS, Linux) - the engine when Everything is absent.

Design follows what Everything does (checked with REA + Rizin on Everything 1.4.1.1032 x64:
FindFirstFileW/FindNextFileW folder walks, FindFirstChangeNotificationW change watching,
LCMapStringW name comparison), using portable equivalents that need no admin rights:

- crawl: ``os.scandir`` with a thread pool (one directory listing per call)
- store: SQLite with an FTS5 ``trigram`` index over the full path (substring match in any
  script incl. Korean/CJK), plus name/ext/size/mtime columns
- refresh: rescan per root on a schedule; generation swap so searches never see a half scan
- watch: optional filesystem events (watchdog) mark roots dirty for an early rescan

Query language: a practical subset of Everything's (see :func:`compile_query`).
"""

from __future__ import annotations

import os
import queue
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS roots(
  path TEXT PRIMARY KEY, gen INTEGER NOT NULL DEFAULT 0, entries INTEGER NOT NULL DEFAULT 0,
  scanned_at REAL, scan_seconds REAL, errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, root TEXT NOT NULL, gen INTEGER NOT NULL,
  path TEXT NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL, is_dir INTEGER NOT NULL,
  size INTEGER, mtime REAL
);
CREATE INDEX IF NOT EXISTS entries_root_gen ON entries(root, gen);
CREATE INDEX IF NOT EXISTS entries_mtime ON entries(mtime);
CREATE INDEX IF NOT EXISTS entries_size ON entries(size);
CREATE INDEX IF NOT EXISTS entries_path ON entries(path);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(path, content='entries', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, path) VALUES (new.id, new.path);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, path) VALUES ('delete', old.id, old.path);
END;
"""

SEP = os.sep

# Directory names never descended into (caches, VCS, system, trash).
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".cache",
    ".Trash",
    "$Recycle.Bin",
    "System Volume Information",
    "#recycle",
    "@eaDir",
    ".tox",
    ".mypy_cache",
    "Library/Caches",
    "proc",
    "sys",
    "dev",
    "run",
    "snap",
}
# Absolute paths never descended into (pseudo filesystems, OS internals).
SKIP_ROOTS = {
    "linux": ["/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker", "/tmp"],
    "darwin": ["/System", "/private/var/vm", "/Volumes/.timemachine", "/dev"],
    "win32": ["C:\\Windows\\WinSxS", "C:\\$Recycle.Bin"],
}.get(sys.platform, [])


def default_roots() -> list[str]:
    """Home on every OS; on Windows every fixed drive; on macOS/Linux also /Volumes and /mnt."""
    roots = [str(Path.home())]
    if sys.platform == "win32":
        import ctypes
        import string

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            drive = f"{letter}:\\"
            if mask & (1 << i) and ctypes.windll.kernel32.GetDriveTypeW(drive) == 3:  # DRIVE_FIXED
                roots.append(drive)
    else:
        for extra in ("/Volumes", "/mnt", "/media"):
            if os.path.isdir(extra):
                roots.append(extra)
    # drop roots contained in another root (home inside C:\)
    roots = sorted(set(roots), key=len)
    kept: list[str] = []
    for r in roots:
        if not any(r.startswith(k.rstrip(SEP) + SEP) for k in kept):
            kept.append(r)
    return kept


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1 :].lower() if 0 < dot < len(name) - 1 else ""


@dataclass
class ScanStats:
    root: str
    entries: int = 0
    errors: int = 0
    seconds: float = 0.0
    stalled: bool = False


STALL_SEC = float(os.environ.get("FINDEX_SCAN_STALL_SEC", "600"))


class LocalIndex:
    def __init__(self, db_path: Path, roots: list[str] | None = None, workers: int = 8) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workers = workers
        self._write_lock = threading.Lock()
        self.scanning: str | None = None
        with self.connect() as c:
            c.executescript(SCHEMA)
            if roots is not None:
                for r in roots:
                    c.execute("INSERT OR IGNORE INTO roots(path) VALUES (?)", (os.path.abspath(r),))
                c.commit()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.db_path, timeout=60, check_same_thread=False)
        c.execute("PRAGMA busy_timeout=60000")
        c.execute("PRAGMA cache_size=-32768")
        try:
            yield c
        finally:
            c.close()

    # ── roots ─────────────────────────────────────────────────────────

    def roots(self) -> list[dict]:
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM roots ORDER BY path")]

    def add_root(self, path: str) -> None:
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO roots(path) VALUES (?)", (os.path.abspath(path),))
            c.commit()

    def remove_root(self, path: str) -> None:
        path = os.path.abspath(path)
        with self._write_lock, self.connect() as c:
            c.execute("DELETE FROM entries WHERE root=?", (path,))
            c.execute("DELETE FROM roots WHERE path=?", (path,))
            c.commit()

    # ── scanning ──────────────────────────────────────────────────────

    def _walk(self, root: str, stats: ScanStats) -> Iterator[tuple]:
        """Parallel scandir; yields (path, name, ext, is_dir, size, mtime)."""
        work: queue.Queue[str | None] = queue.Queue()
        out: queue.Queue[list | None] = queue.Queue(maxsize=64)
        lock = threading.Lock()
        outstanding = [1]
        skip_roots = [s.lower() for s in SKIP_ROOTS]
        work.put(root)

        def worker() -> None:
            while True:
                d = work.get()
                if d is None:
                    return
                rows: list[tuple] = []
                subs: list[str] = []
                err = 0
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                is_dir = e.is_dir(follow_symlinks=False)
                                st = e.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            rows.append(
                                (
                                    e.path,
                                    e.name,
                                    "" if is_dir else _ext(e.name),
                                    int(is_dir),
                                    None if is_dir else st.st_size,
                                    st.st_mtime,
                                )
                            )
                            if (
                                is_dir
                                and not e.is_symlink()
                                and e.name not in SKIP_DIRS
                                and e.path.lower() not in skip_roots
                            ):
                                subs.append(e.path)
                except OSError:
                    err = 1
                with lock:
                    stats.entries += len(rows)
                    stats.errors += err
                    outstanding[0] += len(subs) - 1
                    done = outstanding[0] == 0
                if rows:
                    out.put(rows)
                for s in subs:
                    work.put(s)
                if done:
                    for _ in threads:
                        work.put(None)
                    out.put(None)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.workers)]
        for t in threads:
            t.start()
        while True:
            try:
                batch = out.get(timeout=STALL_SEC)
            except queue.Empty:
                # A network mount that stops answering leaves listing calls blocked in the kernel
                # (seen on SMB: hours in D state). Those threads cannot be killed; stop waiting
                # for them and keep what was collected. They are daemons and die with the process.
                stats.stalled = True
                stats.errors += 1
                return
            if batch is None:
                break
            yield from batch
        for t in threads:
            t.join()

    def scan(self, root: str) -> ScanStats:
        """Rescan one root into a fresh generation, then swap and drop the old one."""
        root = os.path.abspath(root)
        stats = ScanStats(root)
        started = time.perf_counter()
        self.scanning = root
        try:
            with self._write_lock, self.connect() as c:
                row = c.execute("SELECT gen FROM roots WHERE path=?", (root,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO roots(path) VALUES (?)", (root,))
                    row = (0,)
                gen = row[0] + 1
                batch: list[tuple] = []
                for r in self._walk(root, stats):
                    batch.append((root, gen, *r))
                    if len(batch) >= 20_000:
                        c.executemany(
                            "INSERT INTO entries(root, gen, path, name, ext, is_dir, size, mtime) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            batch,
                        )
                        c.commit()
                        batch.clear()
                if batch:
                    c.executemany(
                        "INSERT INTO entries(root, gen, path, name, ext, is_dir, size, mtime) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        batch,
                    )
                stats.seconds = time.perf_counter() - started
                c.execute(
                    "UPDATE roots SET gen=?, entries=?, scanned_at=?, scan_seconds=?, errors=? WHERE path=?",
                    (gen, stats.entries, time.time(), round(stats.seconds, 1), stats.errors, root),
                )
                c.commit()
                while c.execute(
                    "DELETE FROM entries WHERE id IN (SELECT id FROM entries WHERE root=? AND gen<? LIMIT 50000)",
                    (root, gen),
                ).rowcount:
                    c.commit()
                c.commit()
                # The delete trigger leaves FTS5 'delete' entries in index segments until merged;
                # measured +~650 B per live row per rescan without this (678 -> 1397 -> 1933 B/row).
                c.execute("INSERT INTO entries_fts(entries_fts) VALUES('optimize')")
                c.commit()
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                c.execute("ANALYZE")
        finally:
            self.scanning = None
        return stats

    def scan_all(self) -> list[ScanStats]:
        return [self.scan(r["path"]) for r in self.roots()]

    def total(self) -> int:
        with self.connect() as c:
            return c.execute("SELECT coalesce(sum(entries),0) FROM roots").fetchone()[0]


# ── query language (subset of Everything's) ─────────────────────────────

_TOKEN = re.compile(r'!?(?:[a-zA-Z]+:)?"[^"]*"|\S+')
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb)?$", re.I)
_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}


class QueryError(ValueError):
    pass


def _fts(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _like(s: str) -> str:
    return s.replace("|", "||").replace("%", "|%").replace("_", "|_")


def _size(s: str) -> int:
    m = _SIZE.match(s.strip())
    if not m:
        raise QueryError(f"bad size: {s!r} (e.g. 10mb)")
    return int(float(m.group(1)) * _UNITS[(m.group(2) or "b").lower()])


def _dates(token: str) -> tuple[float, float]:
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    t = token.lower()
    if t == "today":
        a, b = day, day + timedelta(days=1)
    elif t == "yesterday":
        a, b = day - timedelta(days=1), day
    elif m := re.fullmatch(r"last(\d+)days", t):
        a, b = day - timedelta(days=int(m.group(1)) - 1), day + timedelta(days=1)
    elif t == "thisweek":
        a = day - timedelta(days=day.weekday())
        b = a + timedelta(days=7)
    elif t == "thismonth":
        a = day.replace(day=1)
        b = (a + timedelta(days=32)).replace(day=1)
    elif re.fullmatch(r"\d{4}", t):
        a, b = datetime(int(t), 1, 1), datetime(int(t) + 1, 1, 1)
    elif re.fullmatch(r"\d{4}-\d{2}", t):
        a = datetime.strptime(t, "%Y-%m")
        b = (a + timedelta(days=32)).replace(day=1)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        a = datetime.strptime(t, "%Y-%m-%d")
        b = a + timedelta(days=1)
    else:
        raise QueryError(f"bad dm: value {token!r}")
    return a.timestamp(), b.timestamp()


def compile_query(q: str) -> tuple[list[str], list, list[str], bool]:
    """-> (where clauses, params, fts terms, has_range_filter)."""
    where: list[str] = []
    params: list = []
    fts: list[str] = []
    ranged = False
    for raw in _TOKEN.findall(q or ""):
        neg = raw.startswith("!")
        tok = raw[1:] if neg else raw
        key, sep, val = tok.partition(":")
        key = key.lower()
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            text = tok[1:-1]
            if len(text) >= 3 and not neg:
                fts.append(_fts(text))
            else:
                where.append(("NOT " if neg else "") + "e.path LIKE ? ESCAPE '|'")
                params.append(f"%{_like(text)}%")
        elif sep and key == "ext":
            exts = [x.strip(".").lower() for x in val.split(";") if x.strip(".")]
            where.append(("NOT " if neg else "") + f"e.ext IN ({','.join('?' * len(exts))})")
            params += exts
        elif sep and key in ("path", "parent"):
            p = os.path.abspath(val.strip('"')).rstrip(SEP)
            hi = p + chr(ord(SEP) + 1)
            if key == "path":
                where.append(("NOT " if neg else "") + "(e.path = ? OR (e.path >= ? AND e.path < ?))")
                params += [p, p + SEP, hi]
            else:
                where.append(
                    ("NOT " if neg else "")
                    + f"(e.path >= ? AND e.path < ? AND instr(substr(e.path, ?), '{SEP}') = 0)"
                )
                params += [p + SEP, hi, len(p) + 2]
            ranged |= not neg
        elif sep and key in ("folder", "file") and not val:
            where.append("e.is_dir = ?")
            params.append(int((key == "folder") != neg))
        elif sep and key == "dm":
            a, b = _dates(val)
            where.append(("NOT " if neg else "") + "(e.mtime >= ? AND e.mtime < ?)")
            params += [a, b]
            ranged |= not neg
        elif sep and key == "size":
            v = val.strip()
            if ".." in v:
                lo, hi_ = v.split("..", 1)
                clause, ps = "(e.size >= ? AND e.size <= ?)", [_size(lo), _size(hi_)]
            elif v[:2] in (">=", "<="):
                clause, ps = f"e.size {v[:2]} ?", [_size(v[2:])]
            elif v[:1] in "<>=":
                clause, ps = f"e.size {v[:1]} ?", [_size(v[1:])]
            else:
                clause, ps = "e.size = ?", [_size(v)]
            where.append(("NOT " if neg else "") + clause)
            params += ps
            ranged |= not neg
        elif "*" in tok or "?" in tok:
            where.append(("NOT " if neg else "") + "e.name GLOB ?")
            params.append(tok)
        elif "|" in tok and not sep:
            alts = [a for a in tok.split("|") if a]
            if not neg and all(len(a) >= 3 for a in alts):
                fts.append("(" + " OR ".join(_fts(a) for a in alts) + ")")
            else:
                where.append(
                    ("NOT " if neg else "")
                    + "("
                    + " OR ".join("e.path LIKE ? ESCAPE '|'" for _ in alts)
                    + ")"
                )
                params += [f"%{_like(a)}%" for a in alts]
        elif len(tok) >= 3 and not neg:
            fts.append(_fts(tok))
        elif tok:
            where.append(("NOT " if neg else "") + "e.path LIKE ? ESCAPE '|'")
            params.append(f"%{_like(tok)}%")
    return where, params, fts, ranged


SORTS = {
    "name": "e.name COLLATE NOCASE ASC",
    "name_desc": "e.name COLLATE NOCASE DESC",
    "path": "e.path COLLATE NOCASE ASC",
    "path_desc": "e.path COLLATE NOCASE DESC",
    "size": "e.size ASC",
    "size_desc": "e.size DESC",
    "date_modified": "e.mtime ASC",
    "date_modified_desc": "e.mtime DESC",
}


def _row(path: str, is_dir: int, size, mtime) -> dict:
    item = {"type": "folder" if is_dir else "file", "full_path": path}
    if size is not None:
        item["size"] = size
    if mtime:
        item["date_modified"] = datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
    return item


def query(
    idx: LocalIndex,
    q: str,
    *,
    max_results: int = 100,
    offset: int = 0,
    sort: str = "name",
    match_path: bool = True,
    **_ignored,
) -> tuple[int, list[dict]]:
    """Strict (AND) search -> (total, items). Paths always match (match_path is the only mode)."""
    if sort not in SORTS:
        sort = "name"
    where, params, fts, ranged = compile_query(q)
    joins = "JOIN roots r ON r.path = e.root AND r.gen = e.gen"
    if fts:
        joins += " JOIN entries_fts f ON f.rowid = e.id"
        where.insert(0, "entries_fts MATCH ?")
        params.insert(0, " AND ".join(fts))
    w = (" WHERE " + " AND ".join(where)) if where else ""
    order = ("+" if (fts or ranged) else "") + SORTS[sort]
    with idx.connect() as c:
        total = c.execute(f"SELECT count(*) FROM entries e {joins}{w}", params).fetchone()[0]
        if max_results == 0:
            return total, []
        rows = c.execute(
            f"SELECT e.path, e.is_dir, e.size, e.mtime FROM entries e {joins}{w} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            params + [max_results, offset],
        ).fetchall()
    return total, [_row(*r) for r in rows]


def plain_words(q: str) -> tuple[list[str], str]:
    """Bare words (relaxable) vs structured rest (always required)."""
    words, rest = [], []
    for raw in _TOKEN.findall(q or ""):
        bare = not (
            raw.startswith("!") or raw.startswith('"') or ":" in raw or "|" in raw or "*" in raw or "?" in raw
        )
        (words if bare else rest).append(raw)
    return list(dict.fromkeys(words)), " ".join(rest)


def relaxed(idx: LocalIndex, q: str, limit: int = 100) -> list[dict] | None:
    """Any-word retrieval ranked by word coverage, then FTS5 BM25. None if there are no words.

    Evaluated on 780k real paths: when a query has one word that is not in the target's name,
    strict AND finds nothing for 89% of queries; this puts the target in the top 10 for 53%.
    Words of 3+ characters use the trigram index; shorter ones (most Korean words are two
    syllables) are OR-ed in with LIKE so they still retrieve and count toward coverage.
    """
    words, rest = plain_words(q)
    long = [w for w in words if len(w) >= 3]
    short = [w for w in words if 2 <= len(w) < 3]
    if not long and not short:
        return None
    where, params, fts, _ = compile_query(rest)
    joins = "JOIN roots r ON r.path = e.root AND r.gen = e.gen"
    any_word: list[str] = []
    any_params: list = []
    if long:
        match = " OR ".join(_fts(w) for w in long)
        if fts:
            match = f"({match}) AND " + " AND ".join(fts)
        any_word.append("e.id IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)")
        any_params.append(match)
    elif fts:
        joins += " JOIN entries_fts f ON f.rowid = e.id"
        where.insert(0, "entries_fts MATCH ?")
        params.insert(0, " AND ".join(fts))
    for w in short:
        any_word.append("e.path LIKE ? ESCAPE '|'")
        any_params.append(f"%{_like(w)}%")
    clause = "(" + " OR ".join(any_word) + ")"
    # Noise dirs are excluded only *below* an indexed root (matched on the part after the root),
    # so a root that itself lives under e.g. Temp is never hidden.
    noise = [
        f"%{SEP}{d}{SEP}%" for d in ("AppData", "node_modules", ".git", ".cache", "site-packages", ".Trash")
    ]
    w_sql = " AND ".join([clause, *where, *["substr(e.path, length(e.root) + 1) NOT LIKE ?"] * len(noise)])
    params = params + noise
    # bm25() needs the FTS table in the FROM clause; LEFT JOIN keeps LIKE-only (short word) rows.
    bm = "0.0"
    if long:
        joins += " LEFT JOIN entries_fts b ON b.rowid = e.id AND entries_fts MATCH ?"
        bm = "coalesce(bm25(entries_fts), 0.0)"
    sql = (
        f"SELECT e.path, e.is_dir, e.size, e.mtime, e.name, {bm} FROM entries e {joins} "
        f"WHERE {w_sql} LIMIT 5000"
    )
    bind = ([any_params[0]] if long else []) + any_params + params
    with idx.connect() as c:
        try:
            rows = c.execute(sql, bind).fetchall()
        except sqlite3.OperationalError:  # bm25 unavailable for this plan: rank by coverage only
            rows = c.execute(
                f"SELECT e.path, e.is_dir, e.size, e.mtime, e.name, 0.0 FROM entries e "
                f"JOIN roots r ON r.path = e.root AND r.gen = e.gen WHERE {w_sql} LIMIT 5000",
                any_params + params,
            ).fetchall()
    low = [x.lower() for x in words]

    def cover(r) -> int:
        p, n = r[0].lower(), r[4].lower()
        return sum(x in p for x in low) * 2 + sum(x in n for x in low)

    # coverage first, then BM25 (lower = better in SQLite), then shorter path
    ranked = sorted(rows, key=lambda r: (-cover(r), r[5], len(r[0])))
    return [_row(*r[:4]) for r in ranked[:limit]]
