"""Semantic index build + search on a small generic corpus (needs the 'semantic' extra + model download)."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("model2vec")

from everything_semantic_mcp import semantic  # noqa: E402

CORPUS = [
    "D:\\Work\\Finance\\2026 Q3 매출 보고서\\",
    "D:\\Work\\Finance\\2026 Q3 매출 보고서\\summary.xlsx",
    "D:\\Work\\HR\\신입사원 온보딩 자료\\",
    "D:\\Work\\HR\\신입사원 온보딩 자료\\welcome guide.pdf",
    "D:\\Work\\Lab\\UV-VIS 반사율 측정\\",
    "D:\\Work\\Lab\\UV-VIS 반사율 측정\\sample_A_total_reflectance.csv",
    "D:\\Photos\\2025 가족 여행\\",
    "D:\\Code\\webapp\\src\\",
    "D:\\Code\\webapp\\vendor\\lib\\__init__.py",
    "D:\\Code\\webapp\\vendor\\lib\\core.py",
    "D:\\Work\\Finance\\~$summary.xlsx",
]


def test_text_for_uses_last_three_segments_without_extension():
    assert semantic.text_for("D:\\a\\b\\c\\d_e.xlsx") == "b / c / d e"
    assert semantic.text_for("D:\\a\\b\\folder.name.with.dots\\") == "a / b / folder.name.with.dots"


def test_skip_vendored_drops_package_subtree_keeps_folder():
    kept = semantic._skip_vendored(CORPUS)
    assert "D:\\Code\\webapp\\vendor\\lib\\core.py" not in kept
    assert "D:\\Code\\webapp\\vendor\\lib\\__init__.py" not in kept
    assert "D:\\Code\\webapp\\src\\" in kept


def test_build_and_search_on_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("EVERYTHING_SEMANTIC_DATA", str(tmp_path))
    corpus = [p for p in CORPUS if "~$" not in p]  # the real query excludes lock files via !"~$"
    monkeypatch.setattr(semantic, "_collect", lambda q, instance: corpus if q.startswith("folder:") else [])
    semantic._index = None
    meta = semantic.build()
    assert meta["count"] == len(semantic._skip_vendored(corpus))

    top = semantic.search("반사율 측정 결과", limit=2)["results"]
    assert "반사율" in top[0]["path"]

    top = semantic.search("new employee orientation", limit=3)["results"]
    assert any("온보딩" in r["path"] for r in top)

    folders = semantic.search("매출", limit=5, kind="folder")["results"]
    assert folders and all(r["path"].endswith("\\") for r in folders)

    scoped = semantic.search("자료", limit=10, path_prefix="D:\\Work\\HR")["results"]
    assert scoped and all(r["path"].startswith("D:\\Work\\HR") for r in scoped)
