"""Start with Windows, as a setting the student can switch off.

A value in the current user's Run key: no administrator rights, removed as
easily as it is added. The app started this way goes straight to the tray.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MireaLectureAssistant"
FLAG = "--autostart"


def available() -> bool:
    """Only the built Windows program can be registered to start with the system."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def command(executable: Path) -> str:
    return f'"{executable}" {FLAG}'


def registered(winreg=None) -> str | None:
    """The command Windows runs at sign-in, or None."""
    winreg = winreg or _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except OSError:
        return None


def set_enabled(enabled: bool, executable: Path, winreg=None) -> None:
    winreg = winreg or _winreg()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command(executable))
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    log.info("autostart_set enabled=%s", enabled)


def launched_at_sign_in(argv: list[str]) -> bool:
    return FLAG in argv


def _winreg():
    import winreg

    return winreg
