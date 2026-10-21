"""Local, meaning-based search over the folder and document names Everything already indexes.

Everything matches substrings; it cannot find "quarterly sales review" when the folder is
called "Q3 매출 회의". This module embeds the last path segments of every folder plus
document-type files with a static multilingual embedding model (Model2Vec
``minishlab/potion-multilingual-128M``: a token-embedding lookup + mean, no GPU, no ONNX or
torch, no network after the first model download) and answers nearest-neighbour queries.

Two-stage ranking: a 64-d PCA projection (float32, resident) picks a candidate pool, the full
256-d int8 vectors (memory-mapped) rerank it. Measured on 1.17M names (i7-1255U):
recall@20 0.98 vs exhaustive 256-d, ~40-50 ms per query instead of ~1.8 s.

Requires the ``semantic`` extra: ``pip install "everything-semantic-mcp[semantic]"``.
"""

from __future__ import annotations

import contextlib
import gc
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

_SEP = re.compile(r"[\\/]")


def _is_dir(path: str) -> bool:
    return path.endswith(("\\", "/"))


def _parent(path: str) -> str:
    m = max(path.rfind("\\"), path.rfind("/"))
    return path[: m + 1]


def _base(path: str) -> str:
    return _SEP.split(path.rstrip("\\/"))[-1]


MODEL_NAME = os.environ.get("EVERYTHING_SEMANTIC_MODEL", "minishlab/potion-multilingual-128M")
PCA_DIMS = 64
RERANK_POOL = 200
PAGE = 100_000
IDLE_SEC = float(os.environ.get("EVERYTHING_SEMANTIC_IDLE_SEC", "900"))


def data_dir() -> Path:
    from .engine import data_home

    target = Path(os.environ.get("EVERYTHING_SEMANTIC_DATA", data_home())) / "index"
    # One-time move from the pre-findex location (everything-semantic-mcp 0.1.x).
    legacy = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "everything-semantic-mcp" / "index"
    if not target.exists() and legacy.is_dir() and "EVERYTHING_SEMANTIC_DATA" not in os.environ:
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            shutil.move(str(legacy), str(target))
    return target


# Folders that drown real work: caches, VCS, runtimes, OS, recycle bins, package stores.
DEFAULT_NOISE = [
    "$Recycle.Bin",
    "\\#recycle\\",
    "\\AppData\\",
    "\\.git\\",
    "\\node_modules\\",
    "C:\\Windows\\",
    "C:\\Program Files",
    "C:\\ProgramData\\",
    "\\site-packages\\",
    "\\.cache\\",
    "\\.venv\\",
    "\\venv\\",
    "\\__pycache__\\",
    "/.git/",
    "/node_modules/",
    "/.cache/",
    "/.venv/",
    "/site-packages/",
    "/Library/",
    "/__pycache__/",
]
DEFAULT_DOC_EXTS = "pdf;doc;docx;xls;xlsx;xlsm;ppt;pptx;hwp;hwpx;odt;ods;odp;rtf;csv;txt;md;ipynb"


def _noise_filter() -> str:
    extra = [p for p in os.environ.get("EVERYTHING_SEMANTIC_EXCLUDE", "").split(";") if p]
    return " ".join(f'!"{p}"' for p in DEFAULT_NOISE + extra)


def sources() -> dict[str, str]:
    exts = os.environ.get("EVERYTHING_SEMANTIC_EXTS", DEFAULT_DOC_EXTS)
    noise = _noise_filter()
    scope = os.environ.get("EVERYTHING_SEMANTIC_SCOPE", "").strip()  # optional Everything filter
    base = f"{scope} {noise}".strip()
    return {
        "folders": f"folder: {base}",
        "documents": f'file: ext:{exts} !"~$" {base}',
    }


_model = None
_model_used = 0.0
_model_lock = threading.Lock()
_index: dict | None = None
_index_lock = threading.Lock()
_build_state: dict = {"running": False}


def _np():
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            'Semantic search needs the optional extra: pip install "everything-semantic-mcp[semantic]"'
        ) from exc
    return numpy


def _embedder():
    """Return an object with ``.encode(texts, normalize=True, use_multiprocessing=False)``.

    In-process by default (tests, library use). The UI and MCP server set
    FINDEX_EMBED_PROCESS=1: the model then lives in a worker process, because building it
    holds the GIL for ~15 s (tokenizer from_file/__setstate__ in Rust) and measured name
    searches in the same process slowed from 0.4 s to 8 s while it loaded.
    """
    global _model, _model_used
    with _model_lock:
        if _model is None:
            if os.environ.get("FINDEX_EMBED_PROCESS") == "1":
                _model = _RemoteEmbedder()
            else:
                try:
                    from model2vec import StaticModel
                except ImportError as exc:
                    raise RuntimeError(
                        'Semantic search needs the optional extra: pip install "everything-semantic-mcp[semantic]"'
                    ) from exc
                _model = StaticModel.from_pretrained(MODEL_NAME)
        _model_used = time.monotonic()
        return _model


def _embed_worker(conn, model_name: str) -> None:  # runs in the child process
    from model2vec import StaticModel

    model = StaticModel.from_pretrained(model_name)
    conn.send("ready")
    while True:
        texts = conn.recv()
        if texts is None:
            return
        conn.send(model.encode(texts, normalize=True, use_multiprocessing=False))


class _RemoteEmbedder:
    """Model2Vec in a child process; encode() round-trips numpy arrays over a pipe."""

    def __init__(self) -> None:
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        self._conn, child = ctx.Pipe()
        self._proc = ctx.Process(target=_embed_worker, args=(child, MODEL_NAME), daemon=True)
        self._proc.start()
        self._lock = threading.Lock()
        msg = self._conn.recv()  # blocks this (background) thread only; the GIL is free meanwhile
        if msg != "ready":
            raise RuntimeError(f"embedding worker failed to start: {msg!r}")

    def encode(self, texts, normalize: bool = True, use_multiprocessing: bool = False):
        with self._lock:
            self._conn.send(list(texts))
            return self._conn.recv()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.send(None)
        self._proc.join(timeout=5)
        if self._proc.is_alive():
            self._proc.kill()


def warm_up() -> None:
    """Load the model in the background so the first query does not wait for it.

    Measured on an i7-1255U laptop: ~20 s, almost all inside StaticModel construction
    (tokenizer + weights files read in ~4 s; an int8 copy only saved ~10%). Only runs when a
    semantic index exists (otherwise nothing uses the model).
    """

    def run() -> None:
        if _load() is not None:
            with contextlib.suppress(Exception):
                _embedder()

    threading.Thread(target=run, daemon=True, name="semantic-warmup").start()


def release_idle_model() -> bool:
    """Free the model (the tokenizer alone is ~0.5 GB) after IDLE_SEC without use."""
    global _model
    with _model_lock:
        if (
            _model is not None
            and not _build_state.get("running")
            and time.monotonic() - _model_used > IDLE_SEC
        ):
            if isinstance(_model, _RemoteEmbedder):
                _model.close()
            _model = None
            gc.collect()
            return True
    return False


def text_for(path: str) -> str:
    """What a person reads: the last three path segments, extension and underscores removed."""
    parts = [p for p in _SEP.split(path) if p]
    tail = parts[-3:] or [path]
    last = tail[-1]
    dot = last.rfind(".")
    if not _is_dir(path) and 0 < dot >= len(last) - 6:  # files only; folders keep their dots
        tail[-1] = last[:dot]
    return " / ".join(t.replace("_", " ") for t in tail)


def _encode(texts: list[str]):
    np = _np()
    v = _embedder().encode(texts, normalize=True, use_multiprocessing=False).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
    return v


def _collect(query: str, engine) -> list[str]:
    """All paths matching *query* from the active engine (folders end with a separator)."""
    return list(engine.iter_paths(query))


def _skip_vendored(paths: list[str]) -> list[str]:
    """Drop everything below a folder that contains __init__.py/package.json (bundled code)."""
    roots = {_parent(p) for p in paths if _base(p) in ("__init__.py", "package.json")}
    if not roots:
        return paths
    ordered = sorted(roots)
    import bisect

    def inside(p: str) -> bool:
        i = bisect.bisect_right(ordered, p) - 1
        return i >= 0 and p.startswith(ordered[i]) and p != ordered[i]

    return [p for p in paths if not inside(p)]


def build(engine) -> dict:
    """(Re)build from the active engine (Everything or local index). Atomic: tmp dir + rename."""
    np = _np()
    started = time.perf_counter()
    state = _build_state
    state.update(running=True, error=None)
    try:
        paths: list[str] = []
        for name, q in sources().items():
            state["phase"] = f"listing {name}"
            paths.extend(_collect(q, engine))
        paths = _skip_vendored(paths)
        state.update(phase="embedding", total=len(paths), embedded=0)
        vectors = np.empty((len(paths), 256), dtype=np.int8)
        for start in range(0, len(paths), 16384):
            chunk = _encode([text_for(p) for p in paths[start : start + 16384]])
            vectors[start : start + len(chunk)] = np.clip(np.rint(chunk * 127), -127, 127).astype(np.int8)
            state["embedded"] = start + len(chunk)
        state["phase"] = "pca"
        rng = np.random.default_rng(0)
        sample = (
            vectors[rng.choice(len(vectors), min(len(vectors), 50_000), replace=False)].astype(np.float32)
            / 127
        )
        mean = sample.mean(axis=0)
        _, _, comps = np.linalg.svd(sample - mean, full_matrices=False)
        dims = min(PCA_DIMS, comps.shape[0])
        proj = comps[:dims].T.astype(np.float32)
        reduced = np.empty((len(vectors), dims), dtype=np.float16)
        for start in range(0, len(vectors), 262_144):
            part = (vectors[start : start + 262_144].astype(np.float32) / 127 - mean) @ proj
            part /= np.linalg.norm(part, axis=1, keepdims=True) + 1e-9
            reduced[start : start + len(part)] = part.astype(np.float16)

        target = data_dir()
        tmp = target.with_name(target.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        np.save(tmp / "vectors.npy", vectors)
        np.save(tmp / "reduced.npy", reduced)
        np.save(tmp / "pca.npy", np.vstack([mean[None, :], proj.T]).astype(np.float32))
        (tmp / "paths.txt").write_text("\n".join(paths), encoding="utf-8")
        meta = {
            "model": MODEL_NAME,
            "count": len(paths),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "build_seconds": round(time.perf_counter() - started, 1),
        }
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        old = target.with_name(target.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        global _index
        with _index_lock:
            _index = None
            if target.exists():
                target.rename(old)
            tmp.rename(target)
        shutil.rmtree(old, ignore_errors=True)
        state["last"] = meta
        return meta
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        state["running"] = False
        state.pop("phase", None)


def build_async(engine) -> dict:
    if _build_state.get("running"):
        return {"started": False, "reason": "a build is already running", **status()}

    def run() -> None:
        # build() records failures in status()["build"]["error"]; the caller already returned.
        with contextlib.suppress(Exception):
            build(engine)

    threading.Thread(target=run, daemon=True, name="semantic-build").start()
    return {"started": True}


def _load() -> dict | None:
    global _index
    with _index_lock:
        target = data_dir()
        if _index is None and (target / "meta.json").exists():
            np = _np()
            pca = np.load(target / "pca.npy")
            _index = {
                "vectors": np.load(target / "vectors.npy", mmap_mode="r"),
                "reduced": np.load(target / "reduced.npy").astype(np.float32),
                "mean": pca[0],
                "proj": pca[1:].T,
                "paths": (target / "paths.txt").read_text(encoding="utf-8").split("\n"),
                "meta": json.loads((target / "meta.json").read_text(encoding="utf-8")),
                "masks": {},
            }
        return _index


def status() -> dict:
    idx = _load()
    return {
        "index": idx["meta"] if idx else None,
        "index_dir": str(data_dir()),
        "model_loaded": _model is not None,
        "build": {k: v for k, v in _build_state.items() if k != "last"},
    }


def _mask(idx: dict, prefix: str, kind: str):
    np = _np()
    key = (prefix, kind)
    cache = idx["masks"]
    if key not in cache:
        if len(cache) >= 16:
            cache.pop(next(iter(cache)))
        paths = idx["paths"]
        cache[key] = np.fromiter(
            (
                (not prefix or p[: len(prefix)].lower() == prefix)
                and (kind == "any" or _is_dir(p) == (kind == "folder"))
                for p in paths
            ),
            dtype=bool,
            count=len(paths),
        )
    return cache[key]


def model_ready() -> bool:
    return _model is not None


def search(
    query: str, limit: int = 20, path_prefix: str = "", kind: str = "any", *, wait: bool = True
) -> dict:
    if not wait and (_model is None or _index is None):
        if not (data_dir() / "meta.json").exists():
            raise RuntimeError(
                "The semantic index has not been built yet: call everything_semantic_reindex first."
            )
        # Kick off loading if needed, but never make the caller wait for it.
        if _index is None and not _index_lock.locked():
            threading.Thread(target=_load, daemon=True).start()
        raise RuntimeError("The meaning model is still loading (about 20 s after start).")
    np = _np()
    idx = _load()
    if idx is None:
        raise RuntimeError(
            "The semantic index has not been built yet: call everything_semantic_reindex first."
        )
    q = _encode([query])[0]
    started = time.perf_counter()
    qr = (q - idx["mean"]) @ idx["proj"]
    qr /= np.linalg.norm(qr) + 1e-9
    coarse = idx["reduced"] @ qr
    prefix = path_prefix.lower().rstrip("\\/")
    if prefix or kind != "any":
        # Mask before top-k so narrow filters (one share, folders only) never starve the pool.
        coarse = np.where(_mask(idx, prefix, kind), coarse, -np.inf)
    pool = min(len(coarse), max(RERANK_POOL, limit * 4))
    cand = np.argpartition(-coarse, pool - 1)[:pool] if pool < len(coarse) else np.arange(len(coarse))
    cand.sort()  # sequential reads from the memory map
    exact = idx["vectors"][cand].astype(np.float32) @ q / 127.0
    results = []
    for j in np.argsort(-exact):
        i = cand[j]
        if not np.isfinite(coarse[i]):
            continue  # masked out by path_prefix/kind (the pool can include them when it spans the index)
        results.append({"path": idx["paths"][i], "score": round(float(exact[j]), 3)})
        if len(results) >= limit:
            break
    return {
        "query": query,
        "indexed": len(idx["paths"]),
        "built_at": idx["meta"]["built_at"],
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "results": results,
    }
