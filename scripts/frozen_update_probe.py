"""Onefile regression: a copy started by another copy must keep its own files.

Build with PyInstaller --onefile --paths src, then run this source script with
the resulting executable as its argument. No app profile, secrets or network
are used. Two starts are checked, each after the starting copy has quit and its
temporary folder has been removed:

* update  — the new version started by ``updater.start`` (0.2.10 and later);
* legacy  — the new version started the way 0.2.8/0.2.9 did it (no reset), so it
  borrows the old copy's folder; its watchdog must still give the app a folder
  of its own.

The runner fails if the app's process shares the starter's folder or cannot
create a verified HTTPS client afterwards ("[Errno 2]" after an update).
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

from mirea_lecture_assistant import supervisor, updater

RESULT_ENV = "MIREA_UPDATE_PROBE_RESULT"
SCENARIO_ENV = "MIREA_UPDATE_PROBE_SCENARIO"
PARENT_ENV = "MIREA_UPDATE_PROBE_PARENT_CERT"
STAGE_ENV = "MIREA_UPDATE_PROBE_STAGE"


def frozen_probe() -> None:
    parent_cert = os.environ.get(PARENT_ENV)
    if not parent_cert:
        # The old copy: start the new one, then quit and let the bootloader clean up.
        os.environ[PARENT_ENV] = certifi.where()
        if os.environ[SCENARIO_ENV] == "update":
            updater.start(Path(sys.executable))
        else:
            environment = dict(os.environ)
            environment[STAGE_ENV] = "watchdog"
            subprocess.Popen([sys.executable], env=environment, close_fds=True)
        return
    if os.environ.pop(STAGE_ENV, None) == "watchdog":
        # The new copy on the borrowed folder: the watchdog starts the app.
        os.environ.pop(supervisor.CHILD_ENV, None)
        supervisor.run([sys.executable])
        return
    report(parent_cert)


def report(parent_cert: str) -> None:
    own_cert = certifi.where()
    deadline = time.monotonic() + 30
    while Path(parent_cert).exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    result = {
        "independent_bundle": own_cert != parent_cert,
        "parent_cleaned": not Path(parent_cert).exists(),
        "certificate_exists": Path(own_cert).is_file(),
    }
    try:
        with httpx.Client():
            result["https_transport_ok"] = True
    except Exception as exc:  # noqa: BLE001 - record any broken frozen-runtime failure
        result["https_transport_ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    Path(os.environ[RESULT_ENV]).write_text(json.dumps(result), encoding="utf-8")


def run(executable: Path, scenario: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="mirea-update-probe-") as folder:
        result = Path(folder) / "result.json"
        environment = dict(os.environ)
        environment[RESULT_ENV] = str(result)
        environment[SCENARIO_ENV] = scenario
        environment[supervisor.CHILD_ENV] = "1"  # as inside a supervised app
        for name in (PARENT_ENV, STAGE_ENV, supervisor.RESET_ENV):
            environment.pop(name, None)
        process = subprocess.Popen([str(executable.resolve())], env=environment)
        process.wait(timeout=60)
        if process.returncode:
            print(f"{scenario}: parent failed with {process.returncode}")
            return False
        deadline = time.monotonic() + 60
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not result.exists():
            print(f"{scenario}: the started copy never reported")
            return False
        outcome = json.loads(result.read_text(encoding="utf-8"))
        print(scenario, json.dumps(outcome, indent=2))
        required = ["independent_bundle", "certificate_exists", "https_transport_ok"]
        if scenario == "update":
            # In the legacy start the watchdog keeps some of the old folder's files
            # open, so Windows may leave them; the app's own folder is what counts.
            required.append("parent_cleaned")
        return all(outcome.get(key) for key in required)


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        frozen_probe()
    else:
        passed = [run(Path(sys.argv[1]), scenario) for scenario in ("update", "legacy")]
        sys.exit(0 if all(passed) else 1)
