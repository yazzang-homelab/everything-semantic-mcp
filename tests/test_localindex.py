"""Built-in index (all OSes): crawl a temp tree, query language, relaxed any-word fallback."""

from __future__ import annotations

import os
import time

import pytest

from everything_semantic_mcp import localindex as li

TREE = {
    "Finance/2026 Q3 매출 보고서/summary.xlsx": 2048,
    "Finance/2026 Q3 매출 보고서/~$summary.xlsx": 10,
    "HR/신입사원 온보딩 가이드/환영 안내서.pdf": 4096,
    "Lab/UV-VIS 반사율 측정/sample_A.csv": 300,
    "Media/video.mp4": 25_000_000,
    "code/app/node_modules/pkg/index.js": 10,
    "code/app/src/main.py": 10,
}


@pytest.fixture
def idx(tmp_path):
    root = tmp_path / "data"
    for rel, size in TREE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as fh:
            fh.truncate(size)
    index = li.LocalIndex(tmp_path / "names.db", roots=[str(root)], workers=4)
    stats = index.scan_all()
    assert stats[0].errors == 0
    return index, root


def names(items):
    return sorted(os.path.basename(i["full_path"]) for i in items)


def test_scan_counts_and_skips(idx):
    index, root = idx
    total, items = li.query(index, "", max_results=500)
    paths = [i["full_path"] for i in items]
    assert not any("node_modules" in p and p.endswith("index.js") for p in paths)  # not descended
    assert any(p.endswith("main.py") for p in paths)
    assert index.total() == total


def test_korean_substring_and_ext(idx):
    index, _ = idx
    assert names(li.query(index, "사율 측", max_results=10)[1]) == ["UV-VIS 반사율 측정", "sample_A.csv"]
    assert names(li.query(index, "ext:pdf", max_results=10)[1]) == ["환영 안내서.pdf"]
    assert names(li.query(index, 'ext:xlsx !"~$"', max_results=10)[1]) == ["summary.xlsx"]


def test_filters_size_folder_dm_parent(idx):
    index, root = idx
    assert names(li.query(index, "size:>10mb", max_results=10)[1]) == ["video.mp4"]
    folders = li.query(index, "folder: 보고서", max_results=10)[1]
    assert names(folders) == ["2026 Q3 매출 보고서"] and folders[0]["type"] == "folder"
    assert li.query(index, "file: dm:today", max_results=0)[0] >= 6
    kids = li.query(index, f'parent:"{root / "Finance"}"', max_results=10)[1]
    assert names(kids) == ["2026 Q3 매출 보고서"]
    assert li.query(index, "*.csv", max_results=10)[0] == 1


def test_sort_size_desc(idx):
    index, _ = idx
    items = li.query(index, "file:", max_results=2, sort="size_desc")[1]
    assert os.path.basename(items[0]["full_path"]) == "video.mp4"


def test_relaxed_fallback_ranks_by_coverage(idx):
    index, _ = idx
    assert li.query(index, "반사율 측정 결과폴더", max_results=10)[0] == 0  # strict AND: nothing
    hits = li.relaxed(index, "반사율 측정 결과폴더", 5)
    assert hits and "반사율" in hits[0]["full_path"]
    assert li.relaxed(index, "a", 5) is None  # only 1-char words: nothing to relax
    # two-syllable Korean words take part (no 3-char word needed)
    short = li.relaxed(index, "매출 보고 자료", 5)
    assert short and "매출" in short[0]["full_path"]


def test_plain_words_split():
    words, rest = li.plain_words('보고서 최신 ext:pptx "Rev.1" !Old a|b *.pdf')
    assert words == ["보고서", "최신"] and rest == 'ext:pptx "Rev.1" !Old a|b *.pdf'


def test_rescan_swaps_generation(idx):
    index, root = idx
    (root / "new_file_after_scan.txt").write_text("x")
    assert li.query(index, "new_file_after", max_results=0)[0] == 0
    index.scan_all()
    assert li.query(index, "new_file_after", max_results=0)[0] == 1
    with index.connect() as c:
        assert c.execute("SELECT count(DISTINCT gen) FROM entries").fetchone()[0] == 1


def test_bad_size_raises(idx):
    index, _ = idx
    with pytest.raises(li.QueryError):
        li.query(index, "size:>abc")


def test_explicit_roots_suppress_default_home_scan(tmp_path, monkeypatch):
    """`findex roots add X` before the first scan: only X is indexed, not the home folder."""
    from everything_semantic_mcp import engine as eng_mod

    monkeypatch.setenv("FINDEX_DATA", str(tmp_path / "fx"))
    tree = tmp_path / "only"
    (tree / "a").mkdir(parents=True)
    (tree / "a" / "x.txt").write_text("x")
    eng = eng_mod.LocalEngine()
    assert eng.index.roots() == []  # construction alone adds nothing
    eng.index.add_root(str(tree))
    eng.rescan(background=False)
    roots = [r["path"] for r in eng.index.roots()]
    assert roots == [str(tree)]
    assert eng.count("x.txt") == 1


def test_mcp_tools_on_local_engine(idx, monkeypatch, tmp_path):
    """The real tool functions, forced onto the built-in engine (what macOS/Linux users get)."""
    index, root = idx
    from everything_semantic_mcp import engine as eng_mod
    from everything_semantic_mcp import server

    monkeypatch.setenv("FINDEX_DATA", str(tmp_path))
    eng = eng_mod.LocalEngine.__new__(eng_mod.LocalEngine)
    eng.index, eng._scan_thread, eng._default_roots = index, None, None
    monkeypatch.setattr(server, "_engine", eng)

    r = server.everything_search("ext:pdf")
    assert r["engine"] == "local" and r["match"] == "exact" and r["total"] == 1
    r = server.everything_search("반사율 측정 결과폴더")
    assert r["match"] == "relaxed" and "반사율" in r["results"][0]["full_path"]
    f = server.everything_find("반사율 측정 결과폴더 위치", limit=5)
    assert f["engine"] == "local" and "반사율" in f["results"][0]["full_path"]
    assert server.everything_count(["ext:csv", "ext:pdf"])["counts"] == {"ext:csv": 1, "ext:pdf": 1}
    assert server.everything_largest(min_size="1mb")["results"][0]["full_path"].endswith("video.mp4")
    st = server.everything_status()["engine"]
    assert st["engine"] == "local" and st["entries"] >= 7
    assert server.everything_health()["engine"] == "local"
    time.sleep(0)  # keep linters quiet about unused import on some platforms
