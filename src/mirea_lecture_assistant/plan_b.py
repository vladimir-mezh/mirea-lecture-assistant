"""Plan B: Windows itself starts the app before every pair.

If the app crashed, was closed, or the PC was only just turned on, nobody would
be there to join the pair. A task in the person's own Task Scheduler (no
administrator rights) starts it a couple of minutes before each online pair;
a copy that is already running makes the new one exit at once. The task is
rewritten whenever the schedule changes and removed when switched off.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

TASK_NAME = "MIREA Lecture Assistant - контроль пар"
FLAG = "--plan-b"
MAX_TRIGGERS = 48  # what one Task Scheduler task holds


def available() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def launched_by_plan_b(argv: list[str]) -> bool:
    return FLAG in argv


def task_xml(executable: Path, times: list[datetime]) -> str:
    triggers = "".join(
        "<TimeTrigger><StartBoundary>"
        + moment.strftime("%Y-%m-%dT%H:%M:%S")
        + "</StartBoundary><Enabled>true</Enabled></TimeTrigger>"
        for moment in times[:MAX_TRIGGERS]
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Запускает MIREA Lecture Assistant перед онлайн-парами, если он не запущен. \
Отключается в настройках приложения.</Description>
  </RegistrationInfo>
  <Triggers>{triggers}</Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(executable))}</Command>
      <Arguments>{FLAG}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(args: list[str]) -> bool:
    try:
        result = subprocess.run(
            ["schtasks.exe", *args],
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def install(executable: Path, times: list[datetime]) -> bool:
    if not times:
        return remove()
    with tempfile.TemporaryDirectory(prefix="mirea-plan-b-") as folder:
        path = Path(folder) / "task.xml"
        path.write_text(task_xml(executable, times), encoding="utf-16")
        done = _schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
    log.info("plan_b_installed ok=%s triggers=%s", done, min(len(times), MAX_TRIGGERS))
    return done


def remove() -> bool:
    done = _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    log.info("plan_b_removed ok=%s", done)
    return done
