"""Configuration audit rules on fixture Everything.ini content (no Everything needed)."""

from everything_semantic_mcp import health

PROBLEM_INI = r"""
folders="\\\\fileserver\\share","C:\\","\\\\nas\\photos"
folder_monitor_changes=1,1,0
folder_update_thread_mode_background=0
folder_update_rescan_asap=1
ntfs_volume_paths="C:","D:"
"""

CLEAN_INI = r"""
folders="\\\\fileserver\\share"
folder_monitor_changes=0
folder_update_thread_mode_background=1
folder_update_rescan_asap=0
ntfs_volume_paths="C:","D:"
"""


def ids(issues):
    return sorted(i["id"] for i in issues)


def test_quoted_list_unescapes_unc():
    assert health.quoted_list(r'"\\\\srv\\a b","C:\\"') == ["\\\\srv\\a b", "C:\\"]


def test_problem_config_flags_every_rule():
    issues = health.audit(health.parse_ini(PROBLEM_INI), target_machine="x86", db_age_hours=1628)
    # C:\ is also NTFS-indexed (duplicate); only the network share is a monitored network folder
    # (\\nas\photos has monitoring off, C:\ is NTFS so its monitor flag is not the network rule).
    assert ids(issues) == sorted(
        [
            "duplicate-volume-index",
            "monitored-network-folder",
            "foreground-rescan",
            "rescan-at-startup",
            "stale-db",
            "x86-build",
        ]
    )
    monitored = [i for i in issues if i["id"] == "monitored-network-folder"]
    assert [m["issue"].rsplit(" ", 1)[-1] for m in monitored] == ["\\\\fileserver\\share"]
    assert all(i["fix"] for i in issues)


def test_clean_config_has_no_issues():
    assert health.audit(health.parse_ini(CLEAN_INI), target_machine="x64", db_age_hours=2) == []


def test_crlf_ini_is_parsed():
    ini = health.parse_ini('folder_update_rescan_asap=1\r\nfolders="\\\\\\\\s\\\\x"\r\n')
    assert ini["folder_update_rescan_asap"] == "1"
    assert ids(health.audit(ini)) == ["rescan-at-startup"]
