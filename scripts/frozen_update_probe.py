"""Onefile regression: updater.start must survive the old bundle's cleanup.

Build with PyInstaller --onefile --paths src, then run this source script with
the resulting executable as its argument. No app profile, secrets or network
are used. The runner fails if the new process shares the old extraction or
cannot create a verified HTTPS transport after the old process has exited.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import certifi
import httpx

from mirea_lecture_assistant import updater

RESULT_ENV = "MIREA_UPDATE_PROBE_RESULT"
PARENT_ENV = "MIREA_UPDATE_PROBE_PARENT_CERT"


def frozen_probe() -> None:
    result = Path(os.environ[RESULT_ENV])
    parent_cert = os.environ.get(PARENT_ENV)
    if not parent_cert:
        os.environ[PARENT_ENV] = certifi.where()
        updater.start(Path(sys.executable))
        return  # the onefile bootloader now removes this process's bundle
    own_cert = certifi.where()
    deadline = time.monotonic() + 30
    while Path(parent_cert).exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    report = {
        "independent_bundle": own_cert != parent_cert,
        "parent_cleaned": not Path(parent_cert).exists(),
        "certificate_exists": Path(own_cert).is_file(),
    }
    try:
        with httpx.Client():
            report["https_transport_ok"] = True
    except Exception as exc:  # noqa: BLE001 - record any broken frozen-runtime failure
        report["https_transport_ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    result.write_text(json.dumps(report), encoding="utf-8")


def run(executable: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mirea-update-probe-") as folder:
        result = Path(folder) / "result.json"
        environment = dict(os.environ)
        environment[RESULT_ENV] = str(result)
        environment.pop(PARENT_ENV, None)
        process = subprocess.Popen([str(executable.resolve())], env=environment)
        process.wait(timeout=60)
        if process.returncode:
            raise RuntimeError(f"Parent failed: {process.returncode}")
        deadline = time.monotonic() + 60
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        report = json.loads(result.read_text(encoding="utf-8"))
        print(json.dumps(report, indent=2))
        if not all(report.get(key) for key in (
            "independent_bundle", "parent_cleaned", "certificate_exists", "https_transport_ok"
        )):
            raise RuntimeError("Updated process lost its runtime after parent exit")


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        frozen_probe()
    else:
        run(Path(sys.argv[1]))
