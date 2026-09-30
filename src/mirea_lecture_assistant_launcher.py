"""PyInstaller entry point (absolute import keeps package context intact)."""

import logging

from mirea_lecture_assistant.app import main

try:
    raise SystemExit(main())
except Exception:
    logging.getLogger("startup").exception("fatal_startup_error")
    raise
