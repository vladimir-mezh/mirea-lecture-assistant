"""Browsers installed on this Windows, for adding the extension to the right one.

Windows lists every installed browser under ``Clients\\StartMenuInternet`` (that
list is what its "Default apps" page shows), and records the person's default
one per protocol. Only Chromium browsers can take the extension.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Program file -> (name people know, its extensions page).
CHROMIUM = {
    "chrome.exe": ("Google Chrome", "chrome://extensions/"),
    "msedge.exe": ("Microsoft Edge", "edge://extensions/"),
    "browser.exe": ("Яндекс Браузер", "browser://extensions/"),
    "opera.exe": ("Opera", "opera://extensions/"),
    "brave.exe": ("Brave", "brave://extensions/"),
    "vivaldi.exe": ("Vivaldi", "vivaldi://extensions/"),
    "chromium.exe": ("Chromium", "chrome://extensions/"),
}
CLIENTS = r"SOFTWARE\Clients\StartMenuInternet"
USER_CHOICE = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"


@dataclass(frozen=True)
class Browser:
    name: str
    executable: Path
    extensions_page: str
    default: bool = False


def program_from_command(command: str) -> Path | None:
    """``"C:\\…\\chrome.exe" --single-argument %1`` -> the program's path."""
    command = command.strip()
    match = re.match(r'"([^"]+)"', command) or re.match(r"(\S+\.exe)", command, re.I)
    return Path(match.group(1)) if match else None


def _kind(executable: Path) -> tuple[str, str] | None:
    name = executable.name.casefold()
    if name == "launcher.exe" and "opera" in str(executable).casefold():
        name = "opera.exe"  # Opera registers its launcher, not opera.exe
    return CHROMIUM.get(name)


def _registry_commands() -> list[tuple[str, str]]:
    """(registered name, open command) of every browser Windows knows."""
    import winreg

    found = []
    roots = [
        (winreg.HKEY_CURRENT_USER, CLIENTS),
        (winreg.HKEY_LOCAL_MACHINE, CLIENTS),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"),
    ]
    for root, path in roots:
        try:
            with winreg.OpenKey(root, path) as clients:
                index = 0
                while True:
                    try:
                        key = winreg.EnumKey(clients, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(clients, key + r"\shell\open\command") as command:
                            found.append((key, winreg.QueryValue(command, None)))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _default_program() -> Path | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_CHOICE) as choice:
            prog_id = winreg.QueryValueEx(choice, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r"\shell\open\command") as key:
            return program_from_command(winreg.QueryValue(key, None))
    except OSError:
        return None


def installed() -> list[Browser]:
    """Chromium browsers on this PC, the default one first."""
    if sys.platform != "win32":
        return []
    default = _default_program()
    browsers: dict[str, Browser] = {}
    for _key, command in _registry_commands():
        executable = program_from_command(command)
        if executable is None or not executable.is_file():
            continue
        kind = _kind(executable)
        if kind is None:
            continue
        is_default = default is not None and str(default).casefold() == str(executable).casefold()
        browsers.setdefault(
            str(executable).casefold(), Browser(kind[0], executable, kind[1], is_default)
        )
    return sorted(browsers.values(), key=lambda browser: (not browser.default, browser.name))


def open_page(browser: Browser, url: str) -> bool:
    """Open a page in that browser: a new tab if it already runs."""
    try:
        subprocess.Popen([str(browser.executable), url], close_fds=True)
        return True
    except OSError:
        log.warning("browser_page_open_failed browser=%s", browser.name)
        return False


def show_in_explorer(folder: Path) -> None:
    """A File Explorer window with the folder selected, ready to drag."""
    try:
        subprocess.Popen(["explorer", f"/select,{folder}"], close_fds=True)
    except OSError:
        log.warning("explorer_open_failed")
