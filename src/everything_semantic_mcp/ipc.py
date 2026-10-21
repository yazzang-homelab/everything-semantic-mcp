"""Pure-ctypes client for voidtools Everything's IPC (no es.exe, no SDK DLL).

Protocol, from the official SDK header ``ipc/everything_ipc.h``:

- Control: ``SendMessage(FindWindow(<ipc class>), WM_USER, cmd, lparam)``.
- Query:   ``WM_COPYDATA(dwData=EVERYTHING_IPC_COPYDATA_QUERY2W, QUERY2 header + UTF-16 text)``.
  Everything answers with ``WM_COPYDATA`` to our window carrying ``EVERYTHING_IPC_LIST2``
  + ``ITEM2[numitems]`` + packed per-item data. Size and dates arrive in that reply, so
  results need no ``os.stat()`` (one network round trip per result on SMB shares).

The IPC window class is ``EVERYTHING_TASKBAR_NOTIFICATION``; a named instance (e.g.
Everything 1.5 alpha's ``1.5a``) appends ``_(<name>)``.

Each query owns a private message-only window, so calls are independent and thread-safe.
:func:`parse_list2` is pure and importable on any OS (unit tests).
"""

from __future__ import annotations

import itertools
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

IPC_WNDCLASS = "EVERYTHING_TASKBAR_NOTIFICATION"
WM_USER = 0x0400
WM_COPYDATA = 0x004A
PM_REMOVE = 0x0001
SMTO_ABORTIFHUNG = 0x0002
COPYDATA_QUERY2W = 18

GET_MAJOR_VERSION, GET_MINOR_VERSION, GET_REVISION, GET_BUILD_NUMBER = 0, 1, 2, 3
GET_TARGET_MACHINE = 5
IS_NTFS_DRIVE_INDEXED = 400
IS_DB_LOADED = 401
IS_DB_BUSY = 402
IS_ADMIN = 403
REBUILD_DB = 405
UPDATE_ALL_FOLDER_INDEXES = 406
SAVE_DB = 407
IS_FAST_SORT = 410
IS_FILE_INFO_INDEXED = 411

MATCHCASE, MATCHWHOLEWORD, MATCHPATH, REGEX = 0x1, 0x2, 0x4, 0x8
ITEM_FOLDER, ITEM_ROOT = 0x1, 0x2

REQ_NAME = 0x1
REQ_PATH = 0x2
REQ_FULL_PATH = 0x4
REQ_EXTENSION = 0x8
REQ_SIZE = 0x10
REQ_DATE_CREATED = 0x20
REQ_DATE_MODIFIED = 0x40
# Field order inside each item's packed data block (everything_ipc.h, ITEM2).
_FIELD_ORDER: list[tuple[int, str, str]] = [
    (REQ_NAME, "name", "str"),
    (REQ_PATH, "path", "str"),
    (REQ_FULL_PATH, "full_path", "str"),
    (REQ_EXTENSION, "extension", "str"),
    (REQ_SIZE, "size", "i64"),
    (REQ_DATE_CREATED, "date_created", "ft"),
    (REQ_DATE_MODIFIED, "date_modified", "ft"),
]

SORTS = {
    "name": 1,
    "name_desc": 2,
    "path": 3,
    "path_desc": 4,
    "size": 5,
    "size_desc": 6,
    "extension": 7,
    "extension_desc": 8,
    "date_created": 11,
    "date_created_desc": 12,
    "date_modified": 13,
    "date_modified_desc": 14,
}
# EVERYTHING_IPC_FILE_INFO_* ids for IS_FILE_INFO_INDEXED.
FILE_INFO = {"size": 1, "date_created": 3, "date_modified": 4}

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_class_counter = itertools.count()
_class_lock = threading.Lock()


class EverythingError(RuntimeError):
    """Everything is absent, hung, busy, or rejected the request."""


@dataclass(frozen=True)
class QueryResult:
    total: int
    offset: int
    items: list[dict]
    elapsed_ms: float = 0.0


@dataclass
class Indexed:
    """Which per-file properties Everything keeps in its index (from IPC, not the ini)."""

    size: bool = True
    date_modified: bool = True
    date_created: bool = False
    _checked_at: float = field(default=0.0, repr=False)


def window_class(instance: str = "") -> str:
    """IPC window class for *instance* ('' = default instance)."""
    return f"{IPC_WNDCLASS}_({instance})" if instance else IPC_WNDCLASS


def _filetime(raw: int) -> str | None:
    if raw in (0, 0xFFFFFFFFFFFFFFFF):
        return None
    try:
        stamp = _FILETIME_EPOCH + timedelta(microseconds=raw // 10)
        return stamp.astimezone().isoformat(timespec="seconds")
    except (OverflowError, ValueError, OSError):  # corrupt timestamps exist on real disks
        return None


def parse_list2(buf: bytes) -> tuple[int, int, list[dict]]:
    """Parse an EVERYTHING_IPC_LIST2 reply into (total, offset, items)."""
    total, numitems, offset, request_flags, _sort = struct.unpack_from("<5I", buf, 0)
    items: list[dict] = []
    for i in range(numitems):
        flags, pos = struct.unpack_from("<2I", buf, 20 + i * 8)
        item: dict = {"type": "folder" if flags & ITEM_FOLDER else "file"}
        if flags & ITEM_ROOT:
            item["root"] = True
        for bit, key, kind in _FIELD_ORDER:
            if not request_flags & bit:
                continue
            if kind == "str":
                (length,) = struct.unpack_from("<I", buf, pos)
                pos += 4
                item[key] = buf[pos : pos + length * 2].decode("utf-16le")
                pos += (length + 1) * 2
            elif kind == "i64":
                (value,) = struct.unpack_from("<q", buf, pos)
                pos += 8
                if value >= 0 and item["type"] == "file":
                    item[key] = value
            else:
                (value,) = struct.unpack_from("<Q", buf, pos)
                pos += 8
                stamp = _filetime(value)
                if stamp:
                    item[key] = stamp
        items.append(item)
    return total, offset, items


# ── Win32 plumbing (Windows only; imported lazily so the module loads anywhere) ──

_api = None


def _win32():
    global _api
    if _api is not None:
        return _api
    if sys.platform != "win32":
        raise EverythingError("Everything IPC is only available on Windows")
    import ctypes
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lresult = ctypes.c_ssize_t
    wndproc_t = ctypes.WINFUNCTYPE(lresult, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [
            ("dwData", ctypes.c_size_t),
            ("cbData", w.DWORD),
            ("lpData", ctypes.c_void_p),
        ]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", w.UINT),
            ("style", w.UINT),
            ("lpfnWndProc", wndproc_t),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", w.HINSTANCE),
            ("hIcon", w.HICON),
            ("hCursor", w.HANDLE),
            ("hbrBackground", w.HBRUSH),
            ("lpszMenuName", w.LPCWSTR),
            ("lpszClassName", w.LPCWSTR),
            ("hIconSm", w.HICON),
        ]

    user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
    user32.FindWindowW.restype = w.HWND
    user32.SendMessageTimeoutW.argtypes = [
        w.HWND,
        w.UINT,
        w.WPARAM,
        w.LPARAM,
        w.UINT,
        w.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = lresult
    user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.DefWindowProcW.restype = lresult
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = w.ATOM
    user32.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
    user32.CreateWindowExW.argtypes = [
        w.DWORD,
        w.LPCWSTR,
        w.LPCWSTR,
        w.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        w.HWND,
        w.HMENU,
        w.HINSTANCE,
        w.LPVOID,
    ]
    user32.CreateWindowExW.restype = w.HWND
    user32.DestroyWindow.argtypes = [w.HWND]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    user32.MsgWaitForMultipleObjects.argtypes = [w.DWORD, ctypes.c_void_p, w.BOOL, w.DWORD, w.DWORD]
    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel32.GetModuleHandleW.restype = w.HMODULE
    _api = (ctypes, w, user32, kernel32, wndproc_t, COPYDATASTRUCT, WNDCLASSEXW)
    return _api


def _ipc_hwnd(instance: str) -> int:
    _, _, user32, *_ = _win32()
    hwnd = user32.FindWindowW(window_class(instance), None)
    if not hwnd:
        raise EverythingError(
            f"Everything is not running (IPC window for instance '{instance or 'default'}' not found)."
        )
    return hwnd


def available(instance: str = "") -> bool:
    try:
        _, _, user32, *_ = _win32()
    except EverythingError:
        return False
    return bool(user32.FindWindowW(window_class(instance), None))


def send_command(cmd: int, lparam: int = 0, timeout_ms: int = 3000, instance: str = "") -> int:
    """Send an EVERYTHING_WM_IPC control command; raises if Everything does not answer."""
    ctypes, _, user32, *_ = _win32()
    result = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(
        _ipc_hwnd(instance), WM_USER, cmd, lparam, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
    )
    if not ok:
        raise EverythingError(
            f"Everything did not answer IPC command {cmd} within {timeout_ms} ms (hung or busy)."
        )
    return ctypes.c_int32(result.value & 0xFFFFFFFF).value


_indexed_cache: dict[str, Indexed] = {}

# Everything answers QUERY2 by sending WM_COPYDATA back to the window named in the request.
# When two threads of one process query at the same time, a reply can be delivered while the
# other query's message pump runs and is lost to it (observed: the UI's status poll and a
# search overlapping -> 'no query reply within 30s'). One query in flight per process.
_query_lock = threading.Lock()


def indexed(instance: str = "", max_age: float = 60.0) -> Indexed:
    """Which properties Everything indexes, asked over IPC (cached for *max_age* s).

    Requesting an unindexed property makes Everything read it from disk for every
    result - on an SMB share that is seconds per hundred files - so queries only
    ask for what is indexed.
    """
    cached = _indexed_cache.get(instance)
    if cached and time.monotonic() - cached._checked_at < max_age:
        return cached
    info = Indexed(
        size=bool(send_command(IS_FILE_INFO_INDEXED, FILE_INFO["size"], instance=instance)),
        date_modified=bool(send_command(IS_FILE_INFO_INDEXED, FILE_INFO["date_modified"], instance=instance)),
        date_created=bool(send_command(IS_FILE_INFO_INDEXED, FILE_INFO["date_created"], instance=instance)),
        _checked_at=time.monotonic(),
    )
    _indexed_cache[instance] = info
    return info


def metadata_flags(instance: str = "") -> int:
    """QUERY2 request flags: full path + every *indexed* size/date property."""
    info = indexed(instance)
    flags = REQ_FULL_PATH
    flags |= REQ_SIZE if info.size else 0
    flags |= REQ_DATE_MODIFIED if info.date_modified else 0
    flags |= REQ_DATE_CREATED if info.date_created else 0
    return flags


def query(
    search: str,
    *,
    max_results: int = 100,
    offset: int = 0,
    sort: str = "name",
    match_case: bool = False,
    whole_word: bool = False,
    match_path: bool = False,
    regex: bool = False,
    request_flags: int | None = None,
    timeout_s: float = 30.0,
    instance: str = "",
) -> QueryResult:
    """Run one QUERY2 against the live Everything index and return parsed items.

    ``request_flags=None`` asks for the full path plus every indexed size/date property.
    """
    if sort not in SORTS:
        raise EverythingError(f"unknown sort {sort!r}; one of {sorted(SORTS)}")
    with _query_lock:
        return _query_locked(
            search,
            max_results,
            offset,
            sort,
            match_case,
            whole_word,
            match_path,
            regex,
            request_flags,
            timeout_s,
            instance,
        )


def _query_locked(
    search,
    max_results,
    offset,
    sort,
    match_case,
    whole_word,
    match_path,
    regex,
    request_flags,
    timeout_s,
    instance,
) -> QueryResult:
    ctypes, w, user32, kernel32, wndproc_t, cds_t, wndclass_t = _win32()
    if request_flags is None:
        request_flags = metadata_flags(instance)
    target = _ipc_hwnd(instance)
    reply: dict[str, bytes] = {}
    reply_id = 0x45534D31  # echoed back in COPYDATASTRUCT.dwData

    @wndproc_t
    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_COPYDATA:
            cds = ctypes.cast(lparam, ctypes.POINTER(cds_t)).contents
            if cds.dwData == reply_id and cds.lpData:
                reply["data"] = ctypes.string_at(cds.lpData, cds.cbData)
                return 1
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    hinst = kernel32.GetModuleHandleW(None)
    with _class_lock:
        class_name = f"EverythingSemanticMcp{next(_class_counter)}_{threading.get_ident()}"
    wc = wndclass_t(
        cbSize=ctypes.sizeof(wndclass_t), lpfnWndProc=wndproc, hInstance=hinst, lpszClassName=class_name
    )
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        raise EverythingError(f"RegisterClassExW failed ({ctypes.get_last_error()})")
    hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, w.HWND(-3), None, hinst, None)
    try:
        if not hwnd:
            raise EverythingError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        flags = (MATCHCASE if match_case else 0) | (MATCHWHOLEWORD if whole_word else 0)
        flags |= (MATCHPATH if match_path else 0) | (REGEX if regex else 0)
        header = struct.pack(
            "<7I", hwnd & 0xFFFFFFFF, reply_id, flags, offset, max_results, request_flags, SORTS[sort]
        )
        payload = header + search.encode("utf-16le") + b"\0\0"
        buf = ctypes.create_string_buffer(payload, len(payload))
        cds = cds_t(COPYDATA_QUERY2W, len(payload), ctypes.cast(buf, ctypes.c_void_p))
        started = time.perf_counter()
        sent = ctypes.c_size_t()
        if not user32.SendMessageTimeoutW(
            target,
            WM_COPYDATA,
            hwnd,
            ctypes.addressof(cds),
            SMTO_ABORTIFHUNG,
            int(timeout_s * 1000),
            ctypes.byref(sent),
        ):
            raise EverythingError(
                "Everything did not accept the query (hung, busy, or still loading its database)."
            )
        if not sent.value:
            raise EverythingError("Everything rejected the query (IPC disabled or database not loaded).")
        deadline = started + timeout_s
        msg = w.MSG()
        while "data" not in reply:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise EverythingError(f"no query reply within {timeout_s:.0f}s")
            user32.MsgWaitForMultipleObjects(0, None, False, int(min(remaining, 0.25) * 1000), 0x04FF)
            while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        total, got_offset, items = parse_list2(reply["data"])
        return QueryResult(total, got_offset, items, round((time.perf_counter() - started) * 1000, 1))
    finally:
        if hwnd:
            user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, hinst)


def status(instance: str = "") -> dict:
    """Version, DB state, indexed NTFS volumes and which sorts/properties are indexed."""

    def cmd(c: int, lp: int = 0) -> int:
        return send_command(c, lp, instance=instance)

    version = ".".join(
        str(cmd(c)) for c in (GET_MAJOR_VERSION, GET_MINOR_VERSION, GET_REVISION, GET_BUILD_NUMBER)
    )
    info = indexed(instance, max_age=0)
    return {
        "version": version,
        "instance": instance or "default",
        "target_machine": {1: "x86", 2: "x64", 3: "arm", 4: "arm64"}.get(cmd(GET_TARGET_MACHINE), "unknown"),
        "db_loaded": bool(cmd(IS_DB_LOADED)),
        "db_busy": bool(cmd(IS_DB_BUSY)),
        "admin": bool(cmd(IS_ADMIN)),
        "ntfs_indexed_drives": [f"{chr(65 + i)}:" for i in range(26) if cmd(IS_NTFS_DRIVE_INDEXED, i)],
        "fast_sort": {
            k: bool(cmd(IS_FAST_SORT, SORTS[k]))
            for k in ("name", "path", "size", "extension", "date_modified", "date_created")
        },
        "indexed": {
            "size": info.size,
            "date_modified": info.date_modified,
            "date_created": info.date_created,
        },
    }
