from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "MireaLectureAssistant"
# A second launch leaves this in the data directory; the running copy shows its window.
SHOW_REQUEST_FILE = "show-window.request"


def resource_path(relative_path: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return root / relative_path


def data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = root / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path
