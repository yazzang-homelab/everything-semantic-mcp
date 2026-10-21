"""LIST2 parsing on synthetic bytes (any OS) + a live round trip when Everything is running."""

from __future__ import annotations

import struct
import sys

import pytest

from everything_semantic_mcp import ipc

FT = int((1768473000 + 11644473600) * 10_000_000)  # 2026-01-15 10:30:00 UTC


def list2(items, total=None, request=None) -> bytes:
    request = ipc.REQ_FULL_PATH | ipc.REQ_SIZE | ipc.REQ_DATE_MODIFIED if request is None else request
    header = struct.pack("<5I", len(items) if total is None else total, len(items), 0, request, 1)
    base = len(header) + 8 * len(items)
    table, data = b"", b""
    for path, is_dir, size in items:
        table += struct.pack("<2I", ipc.ITEM_FOLDER if is_dir else 0, base + len(data))
        if request & ipc.REQ_FULL_PATH:
            data += struct.pack("<I", len(path)) + path.encode("utf-16le") + b"\0\0"
        if request & ipc.REQ_SIZE:
            data += struct.pack("<q", size)
        if request & ipc.REQ_DATE_CREATED:
            data += struct.pack("<Q", FT)
        if request & ipc.REQ_DATE_MODIFIED:
            data += struct.pack("<Q", FT)
    return header + table + data


def test_parse_files_folders_korean_and_unc():
    total, offset, items = ipc.parse_list2(
        list2(
            [
                ("\\\\fileserver\\공유\\2026 예산\\요약.xlsx", False, 2048),
                ("C:\\Projects\\app\\src", True, -1),
            ],
            total=9,
        )
    )
    assert (total, offset) == (9, 0)
    assert items[0]["full_path"] == "\\\\fileserver\\공유\\2026 예산\\요약.xlsx"
    assert items[0]["type"] == "file" and items[0]["size"] == 2048
    assert items[0]["date_modified"].startswith("2026-01-15")
    assert items[1]["type"] == "folder" and "size" not in items[1]


def test_parse_count_only_reply():
    assert ipc.parse_list2(list2([], total=6_115_529))[0] == 6_115_529


def test_parse_path_only_request():
    _, _, items = ipc.parse_list2(list2([("C:\\a.txt", False, 5)], request=ipc.REQ_FULL_PATH))
    assert items == [{"type": "file", "full_path": "C:\\a.txt"}]


def test_parse_with_date_created_field_order():
    req = ipc.REQ_FULL_PATH | ipc.REQ_SIZE | ipc.REQ_DATE_CREATED | ipc.REQ_DATE_MODIFIED
    _, _, items = ipc.parse_list2(list2([("C:\\a.txt", False, 7)], request=req))
    assert items[0]["size"] == 7 and items[0]["date_created"] == items[0]["date_modified"]


def test_corrupt_filetime_is_dropped():
    buf = bytearray(list2([("C:\\a.txt", False, 1)]))
    buf[-8:] = struct.pack("<Q", 0x7FFFFFFFFFFFFFFF)  # far beyond datetime's range
    _, _, items = ipc.parse_list2(bytes(buf))
    assert "date_modified" not in items[0]


def test_window_class_for_named_instance():
    assert ipc.window_class() == "EVERYTHING_TASKBAR_NOTIFICATION"
    assert ipc.window_class("1.5a") == "EVERYTHING_TASKBAR_NOTIFICATION_(1.5a)"


def test_metadata_flags_follow_indexed_properties(monkeypatch):
    monkeypatch.setattr(
        ipc, "indexed", lambda instance="": ipc.Indexed(size=True, date_modified=True, date_created=False)
    )
    flags = ipc.metadata_flags()
    assert flags & ipc.REQ_SIZE and flags & ipc.REQ_DATE_MODIFIED
    assert not flags & ipc.REQ_DATE_CREATED  # unindexed: Everything would read it from disk per result


def test_unknown_sort_rejected():
    with pytest.raises(ipc.EverythingError):
        ipc.query("x", sort="random")


live = pytest.mark.skipif(sys.platform != "win32" or not ipc.available(), reason="Everything is not running")


@live
def test_live_query_returns_metadata():
    r = ipc.query("everything.exe", max_results=5)
    assert r.total >= 1
    assert any(i["full_path"].lower().endswith("everything.exe") for i in r.items)


@live
def test_live_status():
    s = ipc.status()
    assert s["db_loaded"] is True and s["version"].startswith("1.")
