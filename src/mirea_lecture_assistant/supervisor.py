"""A small watchdog that starts the app again after it crashed.

The built program runs twice: this watchdog (no window, no Qt) starts the real
app as its child and waits. A crash brings it back; a deliberate end does not:
«Выход» in the tray, a handover to a newer version, a failed start, or being
closed from another copy or the Task Manager. Several crashes in a row mean
something is wrong that restarting will not fix, so it gives up.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CHILD_ENV = "MIREA_ASSISTANT_CHILD"
# The app itself returns these; the watchdog stops on anything but a crash.
STARTUP_FAILED = 3
CRASHED = 70  # an uncaught Python exception (the launcher's code for it)
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 10 * 60
RESTART_DELAY_SECONDS = 3


def should_supervise(environ=os.environ) -> bool:
    return (
        bool(getattr(sys, "frozen", False))
        and sys.platform == "win32"
        and environ.get(CHILD_ENV) != "1"
        and environ.get("MIREA_ASSISTANT_SMOKE_TEST") != "1"
    )


def is_crash(code: int) -> bool:
    """A Python crash, or a Windows fatal exception (NTSTATUS 0x8…/0xC…)."""
    return code == CRASHED or (code & 0xFFFFFFFF) >= 0x80000000


def _log(message: str) -> None:
    try:
        from .paths import data_dir

        folder = data_dir() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "supervisor.log").open("a", encoding="utf-8") as file:
            file.write(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


def run(argv: list[str], *, call=subprocess.call, sleep=time.sleep, clock=time.monotonic) -> int:
    """Run the app as a child until it ends on purpose; returns its last exit code."""
    environment = {**os.environ, CHILD_ENV: "1"}
    restarts: list[float] = []
    while True:
        code = call([sys.executable, *argv[1:]], env=environment)
        if not is_crash(code):
            _log(f"app_ended code={code}")
            return code
        now = clock()
        restarts = [moment for moment in restarts if now - moment < RESTART_WINDOW_SECONDS]
        if len(restarts) >= MAX_RESTARTS:
            _log(f"app_crashed code={code:#x} giving_up restarts={len(restarts)}")
            return code
        restarts.append(now)
        _log(f"app_crashed code={code:#x} restarting")
        sleep(RESTART_DELAY_SECONDS)


def child_environment() -> dict[str, str]:
    """Start an independent update, not a worker sharing our onefile extraction."""
    environment = dict(os.environ)
    environment.pop(CHILD_ENV, None)
    if getattr(sys, "frozen", False):
        # Updating replaces the archive at the SAME executable path. PyInstaller
        # otherwise treats the new process as our worker and reuses _MEIPASS.
        # Once we exit, our bootloader removes certificates/assets from under it.
        # Use the public bootloader switch; don't edit private _PYI_* variables.
        # The watchdog's own worker intentionally continues to share its bundle
        # (run() waits for it), so this reset belongs only to independent updates.
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def executable() -> Path:
    return Path(sys.executable).resolve()
