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


def _use_system_certificates(log) -> None:
    """Check HTTPS certificates against Windows' own store, as Chrome does.

    Only the bundled certifi list was trusted, which lacks the roots an
    antivirus that inspects HTTPS or the Russian national CA install into
    Windows: MIREA then opened in Chrome but "did not answer" in the app.
    The bundled list stays trusted as well.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # the bundled list alone still works
        log.warning("system_certificates_unavailable", exc_info=True)
    else:
        log.info("system_certificates_enabled")


def _smoke_check_https(log) -> int:
    """The build's own HTTPS stack (ssl, certificates, httpx) must reach a site.

    Everything MIREA-related goes through it, and the window alone says nothing
    about whether a trimmed build can still make a verified TLS connection.
    """
    url = os.environ.get("MIREA_ASSISTANT_SMOKE_URL")
    if not url:
        return 0
    import httpx

    from .async_runtime import run_async

    async def fetch() -> int:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            transport=httpx.AsyncHTTPTransport(retries=2),
        ) as client:
            return (await client.get(url)).status_code

    try:
        status = run_async(fetch(), timeout=60)
    except Exception:
        log.exception("smoke_https_failed")
        return 3
    log.info("smoke_https_ok status=%s", status)
    return 0


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
    from PySide6.QtCore import (
        QLibraryInfo,
        QLocale,
        QLockFile,
        QStandardPaths,
        QTimer,
        QtMsgType,
        QTranslator,
        qInstallMessageHandler,
    )
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .mirea_service import MireaService
    from .ui import MainWindow

    root = data_dir()
    log_path = configure_logging(root / "logs")
    log = logging.getLogger("app")
    log.info("data_directory_ready log_path=%s", log_path)
    _use_system_certificates(log)
    db = Database(root / "assistant.sqlite3")
    MireaService.configure(_session_key())

    app = QApplication(sys.argv)
    # Standard buttons (Cancel, Yes/No) and dialogs in Russian, like the rest of the UI.
    translator = QTranslator(app)
    if translator.load(
        QLocale(QLocale.Language.Russian),
        "qtbase",
        "_",
        QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath),
    ):
        app.installTranslator(translator)
    else:
        log.info("qt_translation_missing")

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
    smoke_test = os.environ.get("MIREA_ASSISTANT_SMOKE_TEST") == "1"
    if smoke_test:
        window.force_exit = True
        QTimer.singleShot(500, app.quit)
    exit_code = app.exec()
    if smoke_test and exit_code == 0:
        exit_code = _smoke_check_https(log)
    try:
        # Leaves Chrome and its СДО session alone; only frees the driver process.
        window.browser.disconnect()
    except Exception:
        log.warning("browser_disconnect_failed", exc_info=True)
    shutdown_async_runtime()
    log.info("application_exit code=%s", exit_code)
    return exit_code
