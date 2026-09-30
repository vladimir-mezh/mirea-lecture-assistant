from __future__ import annotations

import base64
import logging
import os
import sys

from .async_runtime import shutdown_async_runtime
from .database import Database
from .logging_setup import configure_logging
from .paths import data_dir, resource_path


def _session_key() -> str:
    import keyring

    key = keyring.get_password("MireaLectureAssistant", "pymirea-encryption-key")
    if not key:
        key = base64.b64encode(os.urandom(32)).decode("ascii")
        keyring.set_password("MireaLectureAssistant", "pymirea-encryption-key", key)
    return key


def _app_icon(icon_factory, log):
    """Prefer the multi-size .ico: a 1024px PNG scaled to a 16px title bar is mush."""
    if sys.platform == "win32":
        import ctypes

        # Without an explicit AppUserModelID Windows may show a generic taskbar icon.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MireaLectureAssistant")
        except OSError:
            log.debug("app_user_model_id_not_set", exc_info=True)
    for name in ("assets/app_icon.ico", "assets/app_icon.png"):
        path = resource_path(name)
        if not path.exists():
            continue
        icon = icon_factory(str(path))
        if not icon.isNull():
            log.info("app_icon_loaded source=%s", name)
            return icon
    log.warning("app_icon_missing")
    return icon_factory()


def main() -> int:
    from PySide6.QtCore import QLockFile, QStandardPaths, QTimer, QtMsgType, qInstallMessageHandler
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .mirea_service import MireaService
    from .ui import MainWindow

    root = data_dir()
    log_path = configure_logging(root / "logs")
    log = logging.getLogger("app")
    log.info("data_directory_ready log_path=%s", log_path)
    db = Database(root / "assistant.sqlite3")
    MireaService.configure(_session_key())

    app = QApplication(sys.argv)

    def qt_message_handler(message_type, _context, message):
        levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }
        logging.getLogger("qt").log(levels.get(message_type, logging.INFO), "%s", message)

    qInstallMessageHandler(qt_message_handler)
    # The "А" here is Cyrillic. QLockFile records the application name and checks a
    # crashed owner's lock against it, so it stays as released to keep that working.
    app.setApplicationName("MIREА Lecture Assistant")
    app.setOrganizationName("MIREA Lecture Assistant")
    app.setStyle("Fusion")
    app.setWindowIcon(_app_icon(QIcon, log))
    lock_path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
    lock = QLockFile(lock_path + "/mirea-lecture-assistant.lock")
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        log.info("second_instance_blocked")
        if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") == "1":
            return 0
        QMessageBox.information(None, "MIREA Lecture Assistant", "Приложение уже запущено.")
        return 0
    window = MainWindow(db)
    window.show()
    if os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") == "1":
        window.force_exit = True
        QTimer.singleShot(500, app.quit)
    exit_code = app.exec()
    try:
        # Leaves Chrome and its СДО session alone; only frees the driver process.
        window.browser.disconnect()
    except Exception:
        log.warning("browser_disconnect_failed", exc_info=True)
    shutdown_async_runtime()
    log.info("application_exit code=%s", exit_code)
    return exit_code
