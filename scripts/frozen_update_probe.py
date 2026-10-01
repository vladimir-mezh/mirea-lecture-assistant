"""Onefile regressions for updating the program in place.

Run ``python scripts/frozen_update_probe.py <work folder>``: it builds two small
onefile programs from this script (two different builds, as two app versions
are) with PyInstaller and checks, with no app profile, secrets or network:

* update  — a copy started by ``updater.start`` unpacks into its own folder and
  keeps a working HTTPS client after the starting copy quits (0.2.10);
* legacy  — a copy started the way 0.2.8/0.2.9 did it borrows the starter's
  folder, yet its watchdog still gives the app a folder of its own;
* replace — the real update: build A runs as watchdog + app, the app puts build B
  in place of its own file and starts it, then quits. A's watchdog must finish
  cleanly although its file is now B (0.2.16: a late import read B's archive at
  A's offsets, "zlib.error: incorrect header check"), and B must remove A's
  ``.old`` file once A's processes are gone.
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import certifi
import httpx

from mirea_lecture_assistant import supervisor, updater

if getattr(sys, "frozen", False):
    # Generated per build and loaded at start, never late: A and B differ in it.
    import _probe_build

RESULT_ENV = "MIREA_UPDATE_PROBE_RESULT"
SCENARIO_ENV = "MIREA_UPDATE_PROBE_SCENARIO"
PARENT_ENV = "MIREA_UPDATE_PROBE_PARENT_CERT"
STAGE_ENV = "MIREA_UPDATE_PROBE_STAGE"
NAME = "FrozenUpdateProbe"


def frozen_probe() -> None:
    if os.environ.get(SCENARIO_ENV) == "replace":
        replace_stage()
        return
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


def replace_stage() -> None:
    results = Path(os.environ[RESULT_ENV])
    stage = os.environ.get(STAGE_ENV)
    current = Path(sys.executable)
    if stage is None:
        # Build A as the watchdog, the way the launcher runs it.
        os.environ[STAGE_ENV] = "app"
        os.environ.pop(supervisor.CHILD_ENV, None)
        code = supervisor.run([sys.executable])
        # Only for the report: a module this copy never loaded, read now that
        # its file is build B. It shows the hazard the watchdog must avoid.
        try:
            importlib.import_module("mirea_lecture_assistant.relative_time")
            late_import = "ok"
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            late_import = f"{type(exc).__name__}: {exc}"
        outcome = {"code": code, "build": _probe_build.BUILD, "late_import": late_import}
        (results / "watchdog.json").write_text(json.dumps(outcome), encoding="utf-8")
        os._exit(0)  # as the launcher does right after the watchdog returns
    if stage == "app":
        # Build A as the app: install B in place of itself and start it, as
        # MainWindow._update_downloaded does, then quit like the launcher.
        os.environ[STAGE_ENV] = "new"
        installed = updater.install(current.with_name(current.name + ".new"), current)
        updater.start(installed)
        os._exit(0)
    # Build B, started by A: it removes A's file once A's processes are gone.
    deadline = time.monotonic() + 60
    removed = updater.clean_leftovers(current)
    while not removed and time.monotonic() < deadline:
        time.sleep(0.5)
        removed = updater.clean_leftovers(current)
    old_left = current.with_name(current.name + ".old").exists()
    outcome = {"build": _probe_build.BUILD, "old_removed": removed and not old_left}
    (results / "new.json").write_text(json.dumps(outcome), encoding="utf-8")


def _bundled_but_never_loaded() -> None:
    """Puts a module in the archive that only the late import below loads."""
    import mirea_lecture_assistant.relative_time  # noqa: F401


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


def _environment(scenario: str, result: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment[RESULT_ENV] = str(result)
    environment[SCENARIO_ENV] = scenario
    environment[supervisor.CHILD_ENV] = "1"  # as inside a supervised app
    for name in (PARENT_ENV, STAGE_ENV, supervisor.RESET_ENV, supervisor.HEARTBEAT_ENV):
        environment.pop(name, None)
    return environment


def _wait_for(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    return path.exists()


def run(executable: Path, scenario: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="mirea-update-probe-") as folder:
        result = Path(folder) / "result.json"
        process = subprocess.Popen([str(executable.resolve())], env=_environment(scenario, result))
        process.wait(timeout=60)
        if process.returncode:
            print(f"{scenario}: parent failed with {process.returncode}")
            return False
        if not _wait_for(result, 60):
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


def run_replace(build_a: Path, build_b: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="mirea-update-replace-") as folder:
        root = Path(folder)
        results = root / "results"
        results.mkdir()
        app = root / "app" / f"{NAME}.exe"
        app.parent.mkdir()
        shutil.copy2(build_a, app)
        shutil.copy2(build_b, app.with_name(app.name + ".new"))  # the finished download
        process = subprocess.Popen([str(app)], env=_environment("replace", results))
        process.wait(timeout=120)
        watchdog_file, new_file = results / "watchdog.json", results / "new.json"
        if not _wait_for(new_file, 90):
            print("replace: the new build never reported")
            return False
        watchdog = (
            json.loads(watchdog_file.read_text(encoding="utf-8"))
            if watchdog_file.exists()
            else None
        )
        new = json.loads(new_file.read_text(encoding="utf-8"))
        print(
            "replace",
            json.dumps({"exit": process.returncode, "watchdog": watchdog, "new": new}, indent=2),
        )
        return (
            process.returncode == 0
            and watchdog is not None
            and watchdog["code"] == 0
            and watchdog["build"] == "A"
            and new["build"] == "B"
            and new["old_removed"]
            and not app.with_name(app.name + ".old").exists()
        )


def build(work: Path, build_id: str, padding: int) -> Path:
    """One onefile build; B carries a large incompressible module, so every offset moves."""
    generated = work / f"gen-{build_id}"
    generated.mkdir(parents=True, exist_ok=True)
    noise = secrets.token_hex(padding) if padding else ""
    (generated / "_probe_build.py").write_text(
        f'BUILD = "{build_id}"\nNOISE = "{noise}"\n', encoding="utf-8"
    )
    name = f"{NAME}{build_id}"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
            "--paths", str(Path("src").resolve()), "--paths", str(generated), "--name", name,
            "--distpath", str(work / "dist"), "--workpath", str(work / f"work-{build_id}"),
            "--specpath", str(work), str(Path(__file__).resolve()),
        ],
        check=True,
    )  # fmt: skip
    return work / "dist" / (f"{name}.exe" if sys.platform == "win32" else name)


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        frozen_probe()
    else:
        work = Path(sys.argv[1]).resolve()
        build_a = build(work, "A", 0)
        build_b = build(work, "B", 256 * 1024)
        passed = [run(build_a, scenario) for scenario in ("update", "legacy")]
        passed.append(run_replace(build_a, build_b))
        sys.exit(0 if all(passed) else 1)
