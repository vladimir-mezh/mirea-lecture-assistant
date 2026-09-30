from __future__ import annotations

import base64
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__, autostart
from .async_runtime import shutdown_async_runtime
from .database import Database
from .logging_setup import configure_logging
from .paths import SHOW_REQUEST_FILE, SHOW_RESPONSE_FILE, data_dir, resource_path
from .supervisor import STARTUP_FAILED
from .updater import version_tuple  # noqa: F401 - the second-launch check imports it from here


def _session_key(log) -> str:
    import keyring

    try:
        key = keyring.get_password("MireaLectureAssistant", "pymirea-encryption-key")
        if not key:
            key = base64.b64encode(os.urandom(32)).decode("ascii")
            keyring.set_password("MireaLectureAssistant", "pymirea-encryption-key", key)
        return key
    except Exception:
        # Only pymirea's own cache is keyed by it: a key for this run keeps the app
        # usable where it used to stop with a traceback before any window.
        log.warning("session_key_unavailable", exc_info=True)
        return base64.b64encode(os.urandom(32)).decode("ascii")


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


def ask_running_copy_to_show(root: Path, wait_seconds: float = 3.0) -> str | None:
    """Ask the copy that is already running, usually hidden in the tray, to step up.

    It answers "shown" (its window comes forward) or, when this launch is a newer
    version, "handover" (it quits and leaves the browser to this one). None: no
    answer, from a version that predates the request or from a hung copy.
    """
    if sys.platform == "win32":
        import ctypes

        # Windows lets a background process take the foreground only when allowed.
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except OSError:
            pass
    request, response = root / SHOW_REQUEST_FILE, root / SHOW_RESPONSE_FILE
    try:
        response.unlink(missing_ok=True)
        request.write_text(__version__, encoding="ascii")
    except OSError:
        return None
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not request.exists():
            try:
                answer = response.read_text(encoding="ascii").strip()
            except OSError:
                answer = ""
            try:
                # An antivirus may hold the fresh file; the answer is read already.
                response.unlink(missing_ok=True)
            except OSError:
                pass
            return "handover" if answer == "handover" else "shown"
        time.sleep(0.1)
    try:
        request.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def replace_running_copy(lock, log, ask) -> bool:
    """End a copy that cannot be asked, after the student agrees; True if the lock is ours.

    Closing the window only hides the app in the tray. A new version launched
    over it used to say "Приложение уже запущено" and quit, while the old one
    kept running with its old problems.
    """
    if not ask(
        "Уже запущена другая копия приложения — обычно это прошлая версия, спрятанная "
        "в трей у часов, или зависшая копия.\n\nЗакрыть её и запустить эту версию?"
    ):
        return False
    try:
        info = lock.getLockInfo()
    except Exception:  # noqa: BLE001 - an unreadable lock is dealt with below
        info = None
    pid = int(info[0]) if info else 0
    if pid and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
            log.info("running_copy_terminated pid=%s", pid)
        except OSError:
            log.warning("running_copy_not_terminated pid=%s", pid, exc_info=True)
    # The lock of an ended process is stale and taken over by tryLock.
    for _ in range(50):
        if lock.tryLock(100):
            return True
    return False


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
    # The "А" here is Cyrillic, as released. Nothing depends on it: QLockFile records
    # the process name, not this one.
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
        if autostart.launched_at_sign_in(sys.argv):
            return 0  # started with Windows while already running: nothing to do
        answer = ask_running_copy_to_show(root)
        if answer == "shown":
            log.info("second_instance_showed_running_copy")
            return 0
        if answer == "handover" and lock.tryLock(20_000):
            log.info("second_instance_took_over version=%s", __version__)
        elif not replace_running_copy(
            lock,
            log,
            lambda text: (
                QMessageBox.question(None, "MIREA Lecture Assistant", text)
                == QMessageBox.StandardButton.Yes
            ),
        ):
            log.warning("running_copy_kept")
            QMessageBox.information(
                None,
                "MIREA Lecture Assistant",
                "Эта копия не запущена. Если прежняя не отвечает, завершите "
                "MireaLectureAssistant.exe в диспетчере задач (Ctrl+Shift+Esc).",
            )
            return 0
    # Opened only once this copy is the one running: two launches used to migrate
    # the database side by side, and a keyring or database error ended in a
    # traceback box before any window.
    try:
        db = Database(root / "assistant.sqlite3")
        db.backup()
        MireaService.configure(_session_key(log))
        window = MainWindow(db)
    except Exception as exc:
        log.exception("startup_failed")
        QMessageBox.critical(
            None,
            "MIREA Lecture Assistant",
            f"Приложение не смогло запуститься: {exc}\n\nПодробности — в журнале:\n{log_path}",
        )
        return STARTUP_FAILED  # the watchdog does not start it again
    if autostart.launched_at_sign_in(sys.argv):
        # Started with Windows: straight to the tray, no window over the desktop.
        log.info("started_at_sign_in")
    else:
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
    try:
        shutdown_async_runtime()
    except Exception:
        log.warning("async_runtime_shutdown_failed", exc_info=True)
    log.info("application_exit code=%s", exit_code)
    return exit_code
