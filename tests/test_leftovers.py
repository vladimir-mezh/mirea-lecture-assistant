from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from mirea_lecture_assistant import leftovers

OLD = 10**9  # far older than STALE_SECONDS before NOW
NOW = OLD + 24 * 60 * 60


def unpacked(temp: Path, name: str, *, kind: str = "app", mtime: float = OLD) -> Path:
    folder = temp / name
    folder.mkdir()
    (folder / "python312.dll").write_bytes(b"MZ")
    (folder / "python3.dll").write_bytes(b"MZ")
    if kind == "app":
        (folder / "assets").mkdir()
        (folder / "assets" / "app_icon.ico").write_bytes(b"ico")
        (folder / "assets" / "app_icon.png").write_bytes(b"png")
        (folder / "PySide6").mkdir()
    elif kind == "mcp":
        (folder / leftovers.MCP_MARKER).write_text("mcp")
    os.utime(folder, (mtime, mtime))
    return folder


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(leftovers.sys, "platform", "win32")


def test_only_our_unused_old_copies_are_removed(tmp_path, windows, monkeypatch):
    app = unpacked(tmp_path, "_MEI1001")
    mcp = unpacked(tmp_path, "_MEI1002", kind="mcp")
    foreign = unpacked(tmp_path, "_MEI1003", kind="other")
    unpacking = unpacked(tmp_path, "_MEI1004", mtime=NOW - 60)
    own = unpacked(tmp_path, "_MEI1005")
    running = unpacked(tmp_path, "_MEI1006")
    monkeypatch.setattr(leftovers.sys, "_MEIPASS", str(own), raising=False)
    unlink = Path.unlink

    def locked(path, missing_ok=False):
        if path.parent == running:
            raise PermissionError("loaded")  # how Windows refuses a loaded DLL
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)

    assert leftovers.clean_runtime_folders(tmp_path, now=NOW) == 2

    assert not app.exists() and not mcp.exists()
    for kept in (foreign, unpacking, own):
        assert (kept / "python312.dll").exists()
    # The running copy is left whole, not half-deleted.
    assert sorted(p.name for p in running.rglob("*")) == sorted(
        ["python312.dll", "python3.dll", "assets", "app_icon.ico", "app_icon.png", "PySide6"]
    )


def test_a_half_removed_copy_goes_on_the_next_pass(tmp_path, windows):
    folder = unpacked(tmp_path, "_MEI2001")
    (folder / "python312.dll").unlink()
    os.utime(folder, (OLD, OLD))  # an earlier pass, long ago
    assert leftovers.clean_runtime_folders(tmp_path, now=NOW) == 1
    assert not folder.exists()


def test_nothing_is_touched_outside_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(leftovers.sys, "platform", "linux")
    folder = unpacked(tmp_path, "_MEI3001")
    assert leftovers.clean_runtime_folders(tmp_path, now=NOW) == 0
    assert folder.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="real DLL locking is Windows behaviour")
def test_a_loaded_dll_really_protects_a_running_copy(tmp_path):
    import ctypes

    source = Path(sys.base_prefix) / "DLLs" / "sqlite3.dll"
    if not source.exists():
        pytest.skip("no stand-in DLL in this Python")
    folder = unpacked(tmp_path, "_MEI4001")
    shutil.copy2(source, folder / "python312.dll")
    os.utime(folder, (OLD, OLD))
    library = ctypes.WinDLL(str(folder / "python312.dll"))
    try:
        assert leftovers.clean_runtime_folders(tmp_path, now=NOW) == 0
        assert (folder / "assets" / "app_icon.ico").exists()
    finally:
        ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(library._handle))
    assert leftovers.clean_runtime_folders(tmp_path, now=NOW) == 1
    assert not folder.exists()


def test_extension_copy_drops_files_a_newer_version_removed(tmp_path):
    from mirea_lecture_assistant.code_bridge import CodeBridge

    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "manifest.json").write_text('{"version": "1.0.0"}')
    (source / "bridge-config.json").write_text("{}")
    target.mkdir()
    (target / "old-script.js").write_text("old")
    (target / "old-folder").mkdir()
    (target / "old-folder" / "x.js").write_text("old")

    CodeBridge().prepare_extension(source, target)

    assert sorted(p.name for p in target.iterdir()) == ["bridge-config.json", "manifest.json"]


def test_backups_drop_old_program_copies_and_old_recoveries(tmp_path):
    from mirea_lecture_assistant.database import Database

    database = Database(tmp_path / "assistant.sqlite3")
    try:
        (tmp_path / "backups").mkdir()
        (tmp_path / "backups" / "MireaLectureAssistant-0.2.25.exe").write_bytes(b"MZ")
        for stamp in ("20260101-000000", "20260201-000000", "20260301-000000", "20260401-000000"):
            (tmp_path / f"db-backup-{stamp}").mkdir()
        assert database.backup() is not None
    finally:
        database.close()
    assert not list((tmp_path / "backups").glob("*.exe"))
    assert sorted(p.name for p in tmp_path.glob("db-backup-*")) == [
        "db-backup-20260201-000000",
        "db-backup-20260301-000000",
        "db-backup-20260401-000000",
    ]
