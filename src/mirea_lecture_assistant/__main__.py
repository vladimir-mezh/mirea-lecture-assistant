import logging
import os

from .app import main

code = main()
# See the PyInstaller launcher: stuck worker threads must not keep the process alive.
logging.shutdown()
os._exit(code)
