"""Everything configuration audit: finds settings that cause slow indexing, RAM bloat or freezes.

Each rule was found on a real machine (Everything 1.4, ~6M entries incl. NAS folder indexes)
where Everything hung "Not Responding" at 635 MB private memory; fixing these settings brought
it back to a responsive ~350-400 MB. Rules only read Everything.ini; nothing is changed.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path


def ini_path(instance: str = "") -> Path:
    """Everything stores settings in %APPDATA%\\Everything unless installed portable (app_data=0)."""
    name = f"Everything-{instance}.ini" if instance else "Everything.ini"
    return Path(os.environ.get("APPDATA", "")) / "Everything" / name


def db_path(instance: str = "") -> Path:
    name = f"Everything-{instance}.db" if instance else "Everything.db"
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Everything" / name


def parse_ini(text: str) -> dict[str, str]:
    return dict(re.findall(r"(?m)^([a-z0-9_]+)=(.*?)\r?$", text))


def quoted_list(value: str) -> list[str]:
    """Everything stores folder lists as comma-separated, quoted, backslash-escaped strings."""
    return [p.replace("\\\\", "\\") for p in re.findall(r'"((?:[^"\\]|\\.)*)"', value)]


def audit(ini: dict[str, str], *, target_machine: str = "", db_age_hours: float | None = None) -> list[dict]:
    """Return issues found in an Everything.ini mapping. Pure: testable without Everything."""
    folders = quoted_list(ini.get("folders", ""))
    monitors = [m.strip() for m in ini.get("folder_monitor_changes", "").split(",")]
    ntfs = {p.rstrip("\\").upper() for p in quoted_list(ini.get("ntfs_volume_paths", ""))}
    issues: list[dict] = []
    for idx, folder in enumerate(folders):
        root = folder.rstrip("\\").upper()
        is_volume_root = len(root) == 2 and root.endswith(":")
        if is_volume_root and root in ntfs:
            issues.append(
                {
                    "id": "duplicate-volume-index",
                    "issue": f"{folder} is folder-indexed although volume {root} is already NTFS-indexed",
                    "impact": "the whole drive is indexed twice (about 2x RAM) and rescanned recursively",
                    "fix": f"remove {folder} from Tools > Options > Indexes > Folders",
                }
            )
        monitored = idx < len(monitors) and monitors[idx] == "1"
        if monitored and (folder.startswith("\\\\") or root not in ntfs):
            issues.append(
                {
                    "id": "monitored-network-folder",
                    "issue": f"change monitoring is on for network/virtual folder {folder}",
                    "impact": "change notifications on SMB/cloud mounts overflow and force full rescans",
                    "fix": "turn off 'Monitor changes' for this folder and use a scheduled rescan",
                }
            )
    if folders and ini.get("folder_update_thread_mode_background") == "0":
        issues.append(
            {
                "id": "foreground-rescan",
                "issue": "folder rescans run at normal I/O priority",
                "impact": "the UI freezes ('Not Responding') during rescans",
                "fix": "set folder_update_thread_mode_background=1",
            }
        )
    if folders and ini.get("folder_update_rescan_asap") == "1":
        issues.append(
            {
                "id": "rescan-at-startup",
                "issue": "missed scheduled rescans run immediately at startup",
                "impact": "every start after the scheduled time triggers a full rescan of all folders",
                "fix": "set folder_update_rescan_asap=0",
            }
        )
    if db_age_hours is not None and db_age_hours > 72:
        issues.append(
            {
                "id": "stale-db",
                "issue": f"the index database was last saved {db_age_hours:.0f} h ago",
                "impact": "Everything is being killed instead of exiting, so each start rebuilds the index",
                "fix": "call everything_maintain(action='save_db') and exit Everything normally",
            }
        )
    if target_machine == "x86":
        issues.append(
            {
                "id": "x86-build",
                "issue": "32-bit Everything build (2 GB address space)",
                "impact": "large indexes (millions of entries, network folders) approach the limit and stall",
                "fix": "install the x64 build of the same version",
            }
        )
    return issues


def report(info: dict, instance: str = "") -> dict:
    """Audit the live installation: *info* is ipc.status()."""
    ini_file = ini_path(instance)
    ini = parse_ini(ini_file.read_text(encoding="utf-8-sig", errors="replace")) if ini_file.exists() else {}
    db = db_path(instance)
    age = round((time.time() - db.stat().st_mtime) / 3600, 1) if db.exists() else None
    folders = quoted_list(ini.get("folders", ""))
    monitors = [m.strip() for m in ini.get("folder_monitor_changes", "").split(",")]
    return {
        **info,
        "ini": str(ini_file) if ini_file.exists() else None,
        "folder_indexes": [
            {"path": f, "monitor": i < len(monitors) and monitors[i] == "1"} for i, f in enumerate(folders)
        ],
        "db_file_mb": round(db.stat().st_size / 2**20, 1) if db.exists() else None,
        "db_age_hours": age,
        "issues": audit(ini, target_machine=info.get("target_machine", ""), db_age_hours=age),
    }
