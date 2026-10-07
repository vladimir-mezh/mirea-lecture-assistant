"""Unpacked copies of this app and its MCP that nothing will ever remove.

A onefile build unpacks itself into ``%TEMP%\\_MEI…`` (over 100 MB for the app)
and removes that folder when it exits normally. A copy that was killed instead
(Windows shutting down, the watchdog ending a hung copy, an AI client closing
the MCP launcher) leaves its folder behind for good.

Only folders that are surely ours and surely unused are removed. While a copy
runs, its Python DLL is loaded, and Windows refuses to delete a loaded DLL:
that refusal is the test, made before anything else in the folder is touched.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Bundled into both MCP programs (see the MCP repository's scripts/package.py).
MCP_MARKER = "mirea-assistant-mcp.runtime"
# A copy that is still unpacking has not loaded its DLL yet; it is never this old.
STALE_SECONDS = 15 * 60


def _ours(folder: Path) -> bool:
    if (folder / MCP_MARKER).is_file():
        return True
    assets = folder / "assets"
    return (
        (assets / "app_icon.ico").is_file()
        and (assets / "app_icon.png").is_file()
        and (folder / "PySide6").is_dir()
    )


def _python_dlls(folder: Path) -> list[Path]:
    # python312.dll, not the python3.dll forwarder that may never be loaded.
    return [dll for dll in folder.glob("python3*.dll") if dll.name.lower() != "python3.dll"]


def clean_runtime_folders(temp: Path | None = None, *, now: float | None = None) -> int:
    """Remove unused unpacked copies; returns how many folders went."""
    if sys.platform != "win32":
        return 0  # elsewhere a running program's files can be deleted under it
    temp = Path(temp or tempfile.gettempdir())
    now = time.time() if now is None else now
    own = Path(getattr(sys, "_MEIPASS", "")).name
    removed = 0
    for folder in temp.glob("_MEI*"):
        try:
            if (
                folder.name == own
                or not folder.is_dir()
                or now - folder.stat().st_mtime < STALE_SECONDS
                or not _ours(folder)
            ):
                continue
            for dll in _python_dlls(folder):
                dll.unlink()  # PermissionError while a copy runs: leave it whole
        except OSError:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed += not folder.exists()
    if removed:
        log.info("runtime_leftovers_removed count=%s", removed)
    return removed
