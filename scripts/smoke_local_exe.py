"""Run the built executable without touching the real profile or singleton lock."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    executable = Path(sys.argv[1]).resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix="mirea-build-smoke-"))
    environment = {
        **os.environ,
        "LOCALAPPDATA": str(root / "data"),
        "TEMP": str(root),
        "TMP": str(root),
        "QT_QPA_PLATFORM": "offscreen",
        "MIREA_ASSISTANT_SMOKE_TEST": "1",
        "MIREA_ASSISTANT_SMOKE_URL": sys.argv[2]
        if len(sys.argv) > 2
        else "https://github.com/robots.txt",
        "PYINSTALLER_RESET_ENVIRONMENT": "1",
    }
    environment.pop("MIREA_ASSISTANT_CHILD", None)
    environment.pop("MIREA_ASSISTANT_HEARTBEAT", None)
    project = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = str(project / "src")
    subprocess.run(
        [sys.executable, str(project / "scripts" / "smoke_data.py")],
        env=environment,
        check=True,
    )
    result = subprocess.run(
        [str(executable)],
        env=environment,
        timeout=120,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    log_path = root / "data" / "MireaLectureAssistant" / "logs" / "app.log"
    log = log_path.read_text(encoding="utf-8")
    print("\n".join(log.splitlines()[-10:]))
    print(f"SMOKE_EXIT={result.returncode} LOG={log_path}")
    if result.returncode or "smoke_https_ok" not in log or "main_window_ready" not in log:
        raise RuntimeError("Executable smoke test failed")


if __name__ == "__main__":
    main()
