"""A small watchdog that starts the app again after it crashed.

The built program runs twice: this watchdog (no window, no Qt) starts the real
app as its child and waits. A crash brings it back; a deliberate end does not:
«Выход» in the tray, a handover to a newer version, a failed start, or being
closed from another copy or the Task Manager. Repeated crashes trigger a cooldown
instead of a hot restart loop; monitoring resumes afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .paths import data_dir

CHILD_ENV = "MIREA_ASSISTANT_CHILD"
# Makes a PyInstaller program started by another copy unpack into its own
# temporary folder. Without it, a copy started with the same exe path reuses the
# starter's folder and loses it (certificates, Qt files) when the starter quits:
# after an update the new version failed every HTTPS request with "[Errno 2]".
RESET_ENV = "PYINSTALLER_RESET_ENVIRONMENT"
# The app itself returns these; the watchdog stops on anything but a crash.
STARTUP_FAILED = 3
CRASHED = 70  # an uncaught Python exception (the launcher's code for it)
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 10 * 60
RESTART_DELAY_SECONDS = 3
HEARTBEAT_ENV = "MIREA_ASSISTANT_HEARTBEAT"
HEARTBEAT_TIMEOUT_SECONDS = 120
STARTUP_GRACE_SECONDS = 180
CRASH_COOLDOWN_SECONDS = 300
# The loop below wakes every few seconds; a much longer gap means the computer
# slept. The app had no chance to beat meanwhile and must not be killed for it.
SLEEP_GAP_SECONDS = 30


def monitored_call(command, env) -> int:
    """Watch only our own child; a living process is not proof of a living UI."""
    with tempfile.TemporaryDirectory(prefix="mirea-watchdog-") as folder:
        heartbeat = Path(folder) / "heartbeat"
        process = subprocess.Popen(command, env={**env, HEARTBEAT_ENV: str(heartbeat)})
        started = time.monotonic()
        last_seen = started
        last_stamp = None
        last_check = started
        while process.poll() is None:
            try:
                stamp = heartbeat.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            now = time.monotonic()
            if now - last_check > SLEEP_GAP_SECONDS:
                _log(f"system_resumed gap_seconds={now - last_check:.0f}")
                started = last_seen = now
            last_check = now
            if stamp is not None and stamp != last_stamp:
                last_seen = now
                last_stamp = stamp
            stale = (
                now - last_seen > HEARTBEAT_TIMEOUT_SECONDS
                if last_stamp is not None
                else now - started > STARTUP_GRACE_SECONDS
            )
            if stale:
                _log(f"app_unresponsive pid={process.pid} restarting")
                # PyInstaller's worker is a descendant of its bootloader: killing
                # only the bootloader leaves the Qt process and instance lock alive.
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=15,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    process.kill()
                process.wait(timeout=15)
                return CRASHED
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return process.returncode


def touch_heartbeat() -> None:
    path = os.environ.get(HEARTBEAT_ENV)
    if path:
        try:
            Path(path).touch()
        except OSError:
            pass


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
        folder = data_dir() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "supervisor.log").open("a", encoding="utf-8") as file:
            file.write(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


def run(argv: list[str], *, call=monitored_call, sleep=time.sleep, clock=time.monotonic) -> int:
    """Run the app as a child until it ends on purpose; returns its last exit code."""
    # The app gets a folder of its own too: this watchdog may itself be running
    # on a folder borrowed from the copy that started it (an update by 0.2.8/0.2.9).
    environment = {**os.environ, CHILD_ENV: "1", RESET_ENV: "1"}
    restarts: list[float] = []
    while True:
        code = call([sys.executable, *argv[1:]], env=environment)
        if not is_crash(code):
            _log(f"app_ended code={code}")
            return code
        now = clock()
        restarts = [moment for moment in restarts if now - moment < RESTART_WINDOW_SECONDS]
        if len(restarts) >= MAX_RESTARTS:
            _log(f"app_crashed code={code:#x} cooling_down seconds={CRASH_COOLDOWN_SECONDS}")
            sleep(CRASH_COOLDOWN_SECONDS)
            restarts.clear()
        restarts.append(now)
        _log(f"app_crashed code={code:#x} restarting")
        sleep(RESTART_DELAY_SECONDS)


def child_environment() -> dict[str, str]:
    """For starting a separate app (an update): it must get a watchdog of its own."""
    environment = dict(os.environ)
    environment.pop(CHILD_ENV, None)
    environment[RESET_ENV] = "1"
    return environment


def executable() -> Path:
    return Path(sys.executable).resolve()
