"""PyInstaller entry point (absolute import keeps package context intact)."""

import logging
import os
import sys

from mirea_lecture_assistant import supervisor

if supervisor.should_supervise():
    # This copy only watches: the app runs as its child and comes back after a crash.
    os._exit(supervisor.run(sys.argv))

# Imported only here: the watchdog copy above never loads the app.
from mirea_lecture_assistant.app import main

try:
    code = main()
except Exception:
    logging.getLogger("startup").exception("fatal_startup_error")
    logging.shutdown()
    os._exit(supervisor.CRASHED)
# Everything worth keeping is saved by now. A worker still waiting (for an emailed
# code or a slow page) must not keep an invisible process alive after «Выход».
logging.shutdown()
os._exit(code)
