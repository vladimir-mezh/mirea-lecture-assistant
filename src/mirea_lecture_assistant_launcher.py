"""PyInstaller entry point (absolute import keeps package context intact)."""

import logging
import os

from mirea_lecture_assistant.app import main

try:
    code = main()
except Exception:
    logging.getLogger("startup").exception("fatal_startup_error")
    raise
# Everything worth keeping is saved by now. A worker still waiting (for an emailed
# code or a slow page) must not keep an invisible process alive after «Выход».
logging.shutdown()
os._exit(code)
